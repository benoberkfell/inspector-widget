"""Drive the real TalkBack through an app and record where accessibility focus lands.

A walk turns TalkBack on (snapshotting the settings first, see
:mod:`.device`), presses TalkBack's "next" (or "previous") shortcut through a
uinput keyboard (:mod:`.inject`), and after every press reads which node holds
accessibility focus. The walk (A, actual) is then compared with the model's
predicted order (P) and a visual reading order (V) by :mod:`.diff`.

Focus reading (:func:`make_reader`): with an agent that serves A11yFocus,
:class:`A11yFocusReader` long-polls ``Session.a11y_focus`` for TalkBack's next
VIEW_ACCESSIBILITY_FOCUSED event (the reply comes 6-13ms after the event) and
reads the agent's event tap, so a focus the app takes between presses
(stolen), a focus nobody holds (lost), auto-scroll (VIEW_SCROLLED) and window
changes are exact; a full ``dump_a11y`` is taken only when the tree changed or
focus reached a node the last dump lacks. Older agents get
:class:`DumpFocusReader`, which polls ``dump_a11y`` (10-50ms per read) every
10ms until the focused node changes and then stays put for a quiet period.

Node identity: bounds are NOT part of it (they change while a list scrolls).
With the agent's per-View ids the key is the typed node key (``view:<id>``,
``compose:<host>:<semanticsId>``); an agent that still reports the root's id
for every node (ledger A1) gets ``legacy:<window>:<host>:<virtual>:<class>:<label>``.
:func:`node_key` is the one place to change when captures supply refs. Compose
re-mints the semantics ids of lazy items it re-creates (scrolled away and
back), so a node the model knows under another key is matched by its
signature (class + label) where the boxes overlap (:func:`same_node`).

Utterances: TalkBack's verbose logcat (Developer settings > Log output level:
Verbose, set in TalkBack's own UI) carries the exact announcement
(``ttsOutput=``) and edge/auto-scroll lines. The walk reads it when it is
there (``utterance='auto'``) and falls back to the model announcement of the
node TalkBack actually focused, flagged per step (``utt``).
"""

from __future__ import annotations

import importlib
import json
import os
import random
import re
import string
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Iterator, List, Optional, Protocol, Sequence, Tuple

from .. import adb
from . import device, diff, inject

HOST_VIEW_ID = -1  # AccessibilityNodeProvider.HOST_VIEW_ID

DEFAULT_MAX_STEPS = 60
MAX_STEPS_CAP = 300
STEP_TIMEOUT_MS = 1500
SETTLE_MS = 120
INITIAL_FOCUS_S = 2.0   # TalkBack's first focus on a window it just started on
EDGE_SLICE_S = 0.25     # long-poll slice while TalkBack's log can report an edge
POLL_MS = 10
UNTIL = ("wrap", "edge", "loop", "steps")
DIRECTIONS = ("next", "prev")
UTTERANCE = ("auto", "model", "logcat")
RECAPTURE = ("on_unknown", "never")

Rect = Tuple[int, int, int, int]


# --------------------------------------------------------------------------- #
# Node index over one DumpA11yResponse
# --------------------------------------------------------------------------- #
_FLAG_FIELDS = ("clickable", "long_clickable", "focusable", "focused", "accessibility_focused",
                "scrollable", "visible_to_user", "enabled", "checkable", "checked", "heading",
                "screen_reader_focusable", "editable", "is_traversal_group")
# The scroll and page actions by id (android.R.id.accessibilityAction*, a11y.ACTION_NAMES).
_SCROLL_ACTIONS = {0x1000: "forward", 0x2000: "backward", 0x01020038: "up", 0x0102003A: "down",
                   0x01020039: "left", 0x0102003B: "right", 0x01020047: "page_down",
                   0x01020046: "page_up", 0x01020048: "page_left", 0x01020049: "page_right"}
_WEBVIEW = "android.webkit.WebView"
_ROLE_WORDS = {
    "Button": "Button", "ImageButton": "Button", "CheckBox": "Checkbox", "Switch": "Switch",
    "ToggleButton": "Toggle button", "RadioButton": "Radio button", "EditText": "Edit box",
    "ImageView": "Image", "SeekBar": "Slider", "Spinner": "Drop down list",
}


class Node:
    __slots__ = ("key", "window", "host", "virtual", "cls", "label", "text", "cd", "bounds",
                 "flags", "actions", "parent", "children", "drawing_order", "pane_title", "_item")

    def __init__(self) -> None:
        self.parent: Optional["Node"] = None
        self.children: List["Node"] = []

    @property
    def item(self) -> Tuple[Optional[str], bool]:
        """``(ctx, item_root)``: the list item the node sits in (:func:`item_context`)."""
        try:
            return self._item
        except AttributeError:
            self._item = item_context(self, lambda x: x.parent, lambda x: x.children,
                                      lambda x: x.cd or x.text, _node_scrolls)
            return self._item

    @property
    def ctx(self) -> Optional[str]:
        return self.item[0]

    @property
    def item_root(self) -> bool:
        return self.item[1]

    @property
    def simple_cls(self) -> str:
        return self.cls.rsplit(".", 1)[-1].rsplit("$", 1)[-1]

    @property
    def sig(self) -> str:
        return signature(self.cls, self.label)

    def ancestors(self) -> Iterator["Node"]:
        p = self.parent
        while p is not None:
            yield p
            p = p.parent

    def actionable(self) -> bool:
        return bool({"clickable", "long_clickable", "focusable"} & self.flags)

    def speech(self, limit: int = 160) -> str:
        """The model announcement: contentDescription, else the text of this node
        and its non-actionable descendants, then the role word."""
        parts: List[str] = []

        def add(n: "Node", top: bool) -> None:
            if sum(len(p) for p in parts) > limit:
                return
            if n.cd:
                parts.append(n.cd)
                return
            if n.text:
                parts.append(n.text)
            for c in n.children:
                if "visible_to_user" in c.flags and not c.actionable():
                    add(c, False)

        add(self, True)
        role = _ROLE_WORDS.get(self.simple_cls)
        if role and not any(role.lower() == p.lower() for p in parts):
            parts.append(role)
        return ", ".join(p for p in parts if p)[:limit]


def _label(text: str, cd: str, kids: Sequence[Tuple[str, str]]) -> str:
    """The identity label: text or contentDescription, else the direct children's."""
    own = text or cd
    if own:
        return own
    return " | ".join(t for t in ((kt or kc) for kt, kc in kids) if t)


def iou(a: Rect, b: Rect) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    w = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    h = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = w * h
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def signature(cls: str, label: str) -> str:
    """Identity without ids or bounds: the class and the label."""
    return f"{cls.rsplit('.', 1)[-1]}|{label}"


def _unlabelled(sig: str) -> bool:
    return not sig or sig.endswith("|")


CTX_LEN = 80


#: Scrollers that hold one page of mixed content, not a list of items (a View's
#: ScrollView / NestedScrollView, which a CoordinatorLayout page also reports): its children
#: are no items, so they give no item context (AntennaPod's feed: the toolbar in the
#: scrolling page header would change "item" as the header collapses).
_PAGE_SCROLLERS = ("android.widget.ScrollView", "android.widget.HorizontalScrollView",
                   "androidx.core.widget.NestedScrollView")


def _node_scrolls(n: "Node") -> bool:
    return n.cls != _WEBVIEW and n.cls not in _PAGE_SCROLLERS and (
        "scrollable" in n.flags or bool(n.actions & set(_SCROLL_ACTIONS)))


def item_context(n: Any, parent: Callable[[Any], Any], children: Callable[[Any], Any],
                 words: Callable[[Any], str], scrolls: Callable[[Any], bool]
                 ) -> Tuple[Optional[str], bool]:
    """``(ctx, item_root)`` of a node inside a scrolling list: ``ctx`` the first text of
    the innermost list item around it that has one outside the node itself (a card's title,
    a row's sender: what stays put while the item scrolls and its other texts come and go;
    for an unlabelled list item itself, its own first text; "" when none has; None when it
    is in no list), ``item_root`` whether the node is itself a list item (a child of the scrolling
    container). Two nodes alike in class, label and
    screen slot are told apart by it: the HEADLINES chips of two news cards (NiA, after a
    scroll put the second where the first was), a RecyclerView row View rebound to another
    item."""
    own: set = set()
    stack = [n]
    while stack:
        x = stack.pop()
        own.add(id(x))
        stack.extend(children(x) or ())
    item_root = False
    child, a = n, parent(n)
    first = True
    while a is not None:
        if scrolls(a):
            if first:
                item_root = child is n
                first = False
                if item_root and not words(n) and not any(words(c) for c in children(n) or ()):
                    # an unlabelled item (its label comes from its direct children only:
                    # AntennaPod's feed rows): its own first text says which item it shows
                    # now, a row View rebound to another episode
                    stack = [n]
                    while stack:
                        x = stack.pop()
                        w = words(x)
                        if w:
                            return w[:CTX_LEN], True
                        stack.extend(reversed(list(children(x) or ())))
            stack = [child]
            while stack:
                x = stack.pop()
                if id(x) in own:
                    continue
                w = words(x)
                if w:
                    return w[:CTX_LEN], item_root
                stack.extend(reversed(list(children(x) or ())))
        child, a = a, parent(a)
    return ("" if not first else None), item_root


def _ctx_ok(a: Optional[str], b: Optional[str]) -> bool:
    """Contexts that do not tell two nodes apart: one unknown, or the same."""
    return a is None or b is None or a == b


def _alone(a: Optional[str], b: Optional[str]) -> bool:
    """Both nodes sit in a list item that has no text but theirs (``ctx`` ""): their own
    text is all that says which item they show."""
    return a == "" and b == ""


def same_node(key_a: Optional[str], sig_a: str, box_a: Rect,
              key_b: Optional[str], sig_b: str, box_b: Rect, min_iou: float = 0.8,
              ctx_a: Optional[str] = None, ctx_b: Optional[str] = None) -> bool:
    """The same node: the same key, or (a re-minted Compose id) the same
    signature where the boxes overlap, in the same list item (``ctx``, when both
    are known: :func:`item_context`)."""
    if key_a is not None and key_a == key_b:
        return True
    labelled = bool(sig_a) and not sig_a.endswith("|")  # unlabelled nodes all look alike
    return labelled and sig_a == sig_b and iou(box_a, box_b) >= min_iou and _ctx_ok(ctx_a, ctx_b)


def node_key(window: int, host: int, virtual: int, cls: str, label: str,
             legacy: bool, compose_hosts: Sequence[int] = ()) -> str:
    """The walk's node identity (see the module docstring)."""
    if legacy:
        return f"legacy:{window}:{host}:{virtual}:{cls.rsplit('.', 1)[-1]}:{label[:60]}"
    if virtual == HOST_VIEW_ID:
        return f"view:{host}"
    return f"{'compose' if host in compose_hosts else 'virtual'}:{host}:{virtual}"


class DumpIndex:
    """Every node of one a11y dump, keyed by :func:`node_key`, with parents."""

    def __init__(self, resp: Any, legacy: Optional[bool] = None) -> None:
        strings = {e.id: e.str for e in resp.strings.entries}
        s = strings.get
        self.diagnostics = resp.diagnostics or ""
        self.windows: List[int] = [w.root_view_id for w in resp.windows if w.HasField("root")]
        # WindowInfo (agents with A11yFocus): title, frame, obscured system-bar rects.
        self.window_meta: Dict[int, Dict[str, Any]] = {}
        for w in resp.windows:
            if w.HasField("info"):
                from ..strings import StringResolver, window_info_to_dict
                self.window_meta[w.root_view_id] = window_info_to_dict(
                    w.info, StringResolver(resp.strings))
        hosts_per_window: List[set] = []
        compose_hosts: set = set()
        raw: List[Tuple[Any, int, Optional[Any]]] = []
        for w in resp.windows:
            if not w.HasField("root"):
                continue
            hosts: set = set()
            stack = [(w.root, None)]
            while stack:
                pn, parent = stack.pop()
                raw.append((pn, w.root_view_id, parent))
                hosts.add(pn.host_view_id)
                if pn.virtual_id == HOST_VIEW_ID and "compose" in (
                        (s(pn.provider_class) or "") + (s(pn.class_name) or "")).lower():
                    compose_hosts.add(pn.host_view_id)
                stack.extend((c, pn) for c in reversed(pn.children))
            hosts_per_window.append(hosts)
        if legacy is None:
            # A1: the old agent reports the ROOT's host_view_id for every node.
            legacy = bool(raw) and all(len(h) <= 1 for h in hosts_per_window) and len(raw) > 1
        self.legacy = legacy
        self.compose_hosts = compose_hosts
        self.nodes: Dict[str, Node] = {}
        self.order: List[Node] = []
        self.focused: List[Node] = []
        by_pb: Dict[int, Node] = {}
        for pn, win, parent in raw:
            n = Node()
            n.window = win
            n.host = pn.host_view_id
            n.virtual = pn.virtual_id
            n.cls = s(pn.class_name) or ""
            n.text = s(pn.text) or ""
            n.cd = s(pn.content_description) or ""
            n.pane_title = s(pn.pane_title) or ""
            n.label = _label(n.text, n.cd, [(s(c.text) or "", s(c.content_description) or "")
                                            for c in pn.children])
            b = pn.bounds.layout
            n.bounds = (b.x, b.y, b.w, b.h)
            n.flags = {f for f in _FLAG_FIELDS if getattr(pn, f, False)}
            n.actions = {a.id for a in pn.actions}
            n.drawing_order = pn.drawing_order
            n.key = node_key(win, n.host, n.virtual, n.cls, n.label, legacy, compose_hosts)
            if parent is not None:
                n.parent = by_pb.get(id(parent))
                if n.parent is not None:
                    n.parent.children.append(n)
            by_pb[id(pn)] = n
            self.order.append(n)
            self.nodes.setdefault(n.key, n)
            if pn.accessibility_focused:
                self.focused.append(n)

    @property
    def focus(self) -> Optional[Node]:
        return self.focused[0] if self.focused else None

    def window_rect(self, window: int) -> Optional[Rect]:
        frame = (self.window_meta.get(window) or {}).get("frame")
        if frame:
            return (frame["x"], frame["y"], frame["w"], frame["h"])
        for n in self.order:
            if n.window == window and n.parent is None:
                return n.bounds
        return None

    def obscured(self, window: int) -> List[Rect]:
        """Screen rects the status bar, nav bar and IME cover over this window."""
        return [(r["x"], r["y"], r["w"], r["h"])
                for r in (self.window_meta.get(window) or {}).get("obscured") or []]

    def scroll_container(self, n: Node) -> Optional[Node]:
        """The nearest scrollable around ``n`` that TalkBack scrolls: not a WebView, which
        scrolls its own page as it moves focus through it (TalkBack never auto-scrolls web
        content, FocusProcessorForLogicalNavigation :2273)."""
        for a in n.ancestors():
            if a.cls == _WEBVIEW:
                continue
            if "scrollable" in a.flags or a.actions & set(_SCROLL_ACTIONS):
                return a
        return None


def _descendant_keys(n: Node) -> set:
    out: set = set()
    stack = list(n.children)
    while stack:
        c = stack.pop()
        out.add(c.key)
        stack.extend(c.children)
    return out


def detect_scroll(prev: Optional[DumpIndex], cur: DumpIndex) -> Optional[Node]:
    """The scrollable container whose content moved between two dumps, if any:
    nodes it holds in both moved, or (a lazy list re-creates its items as it
    scrolls by a page) it now holds mostly different nodes."""
    if prev is None:
        return None
    focus = cur.focus
    c = cur.scroll_container(focus) if focus is not None else None
    if c is not None and c.key in prev.nodes:
        before, after = _descendant_keys(prev.nodes[c.key]), _descendant_keys(c)
        if before and after and len(before & after) < 0.5 * min(len(before), len(after)):
            return c
    votes: Dict[str, int] = {}
    moved = 0
    for k, n in cur.nodes.items():
        p = prev.nodes.get(k)
        if p is None or p.bounds[:2] == n.bounds[:2] or p.bounds[2:] != n.bounds[2:]:
            continue
        c = cur.scroll_container(n)
        if c is None:
            continue
        moved += 1
        votes[c.key] = votes.get(c.key, 0) + 1
    if moved < 2 or not votes:
        return None
    return cur.nodes.get(max(votes, key=votes.get))


# --------------------------------------------------------------------------- #
# Focus reading
# --------------------------------------------------------------------------- #
_FROM_DUMP = object()
@dataclass
class Snapshot:
    index: DumpIndex
    t: float  # time.monotonic() when the read returned
    resp: Any = None  # the DumpA11yResponse (for the model)
    # The reader's own answer (A11yFocus) when it has one; else the dump's flag.
    focus_node: Any = field(default=_FROM_DUMP)
    events: List[Dict[str, Any]] = field(default_factory=list)  # event tap, since the last read
    uptime_ms: Optional[int] = None  # device uptime at the read (A11yFocus)

    @property
    def focus(self) -> Optional[Node]:
        return self.index.focus if self.focus_node is _FROM_DUMP else self.focus_node

    @property
    def key(self) -> Optional[str]:
        f = self.focus
        return f.key if f is not None else None


@dataclass
class WaitResult:
    snap: Snapshot
    moved: bool
    first_ms: Optional[int]
    settled_ms: Optional[int]
    polls: int
    aborted: bool = False
    lost: bool = False  # focus was cleared and did not land anywhere before the timeout


class FocusReader(Protocol):
    """Where a walk gets TalkBack's focus from (the dump poller, or A11yFocus)."""

    kind: str

    def snapshot(self, fresh: bool = False) -> Snapshot: ...

    def wait_change(self, prev_key: Optional[str], timeout_s: float, quiet_s: float,
                    abort: Optional[Callable[[], bool]] = None) -> WaitResult: ...


class DumpFocusReader:
    """Poll ``Session.dump_a11y(include_extras=False)`` for the focused node."""

    kind = "dump_poll"

    def __init__(self, session: Any, poll_s: Optional[float] = None) -> None:
        self.session = session
        self.poll_s = POLL_MS / 1000 if poll_s is None else poll_s
        self.legacy: Optional[bool] = None
        self.reads = 0
        self.read_ms: List[float] = []

    def snapshot(self, fresh: bool = False) -> Snapshot:
        t0 = time.monotonic()
        resp = self.session.dump_a11y(include_extras=False)
        idx = DumpIndex(resp, self.legacy)
        if self.legacy is None and idx.order:
            self.legacy = idx.legacy
        t = time.monotonic()
        self.reads += 1
        self.read_ms.append((t - t0) * 1000)
        return Snapshot(idx, t, resp)

    def wait_change(self, prev_key: Optional[str], timeout_s: float, quiet_s: float,
                    abort: Optional[Callable[[], bool]] = None) -> WaitResult:
        """Poll until focus lands on a node other than ``prev_key`` and keeps the
        same key and bounds for ``quiet_s``.

        No focus at all is not a landing: while TalkBack auto-scrolls, the focused
        item scrolls off, is disposed, and focus is gone for a few hundred ms
        before TalkBack puts it on the next item. ``moved`` False on timeout (or
        when ``abort`` says the press was an edge); ``lost`` when focus was gone
        at the timeout.
        """
        t0 = time.monotonic()
        first: Optional[float] = None
        sig: Any = object()
        stable_since = t0
        polls = 0
        while True:
            snap = self.snapshot()
            polls += 1
            now = snap.t
            k = snap.key
            if k is not None and k != prev_key:
                if first is None:
                    first = now
                f = snap.focus
                cur_sig = (k, f.bounds if f is not None else None)
                if cur_sig != sig:
                    sig, stable_since = cur_sig, now
                elif now - stable_since >= quiet_s:
                    return WaitResult(snap, True, _ms(first - t0), _ms(stable_since - t0), polls)
            else:
                sig = object()  # cleared, or back where it was: not landed
                if k == prev_key and abort is not None and abort():
                    return WaitResult(snap, False, None, None, polls, aborted=True)
            if now - t0 >= timeout_s:
                moved = k is not None and k != prev_key
                return WaitResult(snap, moved, _ms(first - t0) if first else None, None, polls,
                                  lost=k is None and prev_key is not None)
            time.sleep(self.poll_s)


# Events that do not change the tree: a plain step needs no new dump.
_FOCUS_EVENTS = frozenset({"VIEW_ACCESSIBILITY_FOCUSED", "VIEW_ACCESSIBILITY_FOCUS_CLEARED",
                           "VIEW_HOVER_ENTER", "VIEW_HOVER_EXIT", "VIEW_FOCUSED",
                           "TOUCH_INTERACTION_START", "TOUCH_INTERACTION_END",
                           "TOUCH_EXPLORATION_GESTURE_START", "TOUCH_EXPLORATION_GESTURE_END",
                           "ANNOUNCEMENT", "VIEW_SELECTED"})


def _node_from_focus(focus: Dict[str, Any], legacy: bool) -> Node:
    """A detached Node for a focus the last dump does not hold."""
    raw = focus.get("node") or {}
    n = Node()
    n.window = int(focus.get("root_view_id") or 0)
    n.host = int(focus.get("host_view_id") or 0)
    n.virtual = int(focus.get("virtual_id", HOST_VIEW_ID))
    n.cls = raw.get("class_name") or ""
    n.text = raw.get("text") or ""
    n.cd = raw.get("content_description") or ""
    n.pane_title = raw.get("pane_title") or ""
    n.label = _label(n.text, n.cd, [(c.get("text") or "", c.get("content_description") or "")
                                    for c in raw.get("children") or []])
    b = ((focus.get("bounds") or {}).get("layout") or {})
    n.bounds = (b.get("x", 0), b.get("y", 0), b.get("w", 0), b.get("h", 0))
    n.flags = set(raw.get("flags") or []) & set(_FLAG_FIELDS)
    n.actions = {a.get("id") for a in raw.get("actions") or []}
    n.drawing_order = int(raw.get("drawing_order") or 0)
    n.key = focus.get("node_key") or node_key(n.window, n.host, n.virtual, n.cls, n.label, legacy)
    return n


class A11yFocusReader:
    """Long-poll the agent's A11yFocus: exact focus moves from its event tap.

    Every read passes the previous reply's ``seq`` as ``after_seq``, so no focus
    event between two reads is missed (the pre-press guard sees a stolen focus
    exactly). A full dump is taken only when the events say the tree changed
    (anything but focus/hover events) or focus is on a node the last dump lacks.
    """

    kind = "a11y_focus"

    def __init__(self, session: Any) -> None:
        self.session = session
        self.legacy: Optional[bool] = None
        self.seq = 0
        self.reads = 0
        self.dumps = 0
        self.read_ms: List[float] = []
        self.uptime_ms: Optional[int] = None  # device uptime at the last read
        self._index: Optional[DumpIndex] = None
        self._resp: Any = None

    def _dump(self) -> None:
        resp = self.session.dump_a11y(include_extras=False)
        self._index = DumpIndex(resp, self.legacy)
        if self.legacy is None and self._index.order:
            self.legacy = self._index.legacy
        self._resp = resp
        self.dumps += 1

    def _read(self, wait_ms: int = 0, quiet_ms: int = 0) -> Dict[str, Any]:
        from .. import a11y
        t0 = time.monotonic()
        d = a11y.a11y_focus_to_dict(self.session.a11y_focus(
            after_seq=self.seq, wait_ms=wait_ms, quiet_ms=quiet_ms, max_events=256))
        self.reads += 1
        # The read itself, without the time the long-poll spent waiting for TalkBack.
        self.read_ms.append((time.monotonic() - t0) * 1000 - (d.get("waited_ms") or 0))
        return d

    def _snap(self, d: Dict[str, Any]) -> Snapshot:
        focus = d.get("a11y")
        key = focus.get("node_key") if focus else None
        events = d.get("events") or []
        if self._index is None or any(e.get("type") not in _FOCUS_EVENTS for e in events) \
                or (key is not None and key not in self._index.nodes):
            self._dump()
        assert self._index is not None
        node = None
        if focus is not None and not focus.get("stale"):
            node = self._index.nodes.get(key) if key else None
            if node is None:
                node = _node_from_focus(focus, bool(self.legacy))
        self.seq = int(d.get("seq") or self.seq)
        self.uptime_ms = d.get("read_uptime_ms") or None
        return Snapshot(self._index, time.monotonic(), self._resp, focus_node=node,
                        events=events, uptime_ms=self.uptime_ms)

    def snapshot(self, fresh: bool = False) -> Snapshot:
        """The focus now; ``fresh`` re-dumps even when no event said the tree
        changed (Compose sends no content-change event for a rebound lazy item)."""
        d = self._read()
        if fresh:
            self._index = None
        return self._snap(d)

    def wait_change(self, prev_key: Optional[str], timeout_s: float, quiet_s: float,
                    abort: Optional[Callable[[], bool]] = None) -> WaitResult:
        """One long-poll: returns once TalkBack focused a node (then ``quiet_s``
        with no events), or at the timeout (an edge when focus stayed put)."""
        before = self.uptime_ms
        if abort is None:
            d = self._read(int(timeout_s * 1000), int(quiet_s * 1000))
        else:
            # TalkBack's log says "Reach edge" within a few ms: poll in slices so an
            # edge does not cost the whole timeout; events are kept across slices.
            deadline = time.monotonic() + timeout_s
            events: List[Dict[str, Any]] = []
            while True:
                left = max(0.0, deadline - time.monotonic())
                d = self._read(int(min(left, EDGE_SLICE_S) * 1000), int(quiet_s * 1000))
                events += d.get("events") or []
                if d.get("focus_event") or left <= EDGE_SLICE_S or abort():
                    break
                self.seq = int(d.get("seq") or self.seq)
            d["events"] = events
        snap = self._snap(d)
        k = snap.key
        focused = [e for e in snap.events if e.get("type") == "VIEW_ACCESSIBILITY_FOCUSED"]
        first_ms = None
        if focused and before is not None:
            first_ms = max(0, int(focused[0].get("uptime_ms", before)) - int(before))
        moved = k is not None and k != prev_key
        return WaitResult(snap, moved, first_ms if moved else None, None, 1,
                          lost=k is None and prev_key is not None)


def make_reader(session: Any) -> FocusReader:
    """A11yFocusReader when the agent serves A11yFocus, else DumpFocusReader."""
    if callable(getattr(session, "a11y_focus", None)):
        try:
            session.a11y_focus()
            return A11yFocusReader(session)
        except Exception:  # noqa: BLE001 - an agent from before A11yFocus: poll dumps
            pass
    return DumpFocusReader(session)


# --------------------------------------------------------------------------- #
# TalkBack's verbose logcat (optional utterance / edge / auto-scroll source)
# --------------------------------------------------------------------------- #
_RE_TTS = re.compile(r"TYPE_VIEW_ACCESSIBILITY_FOCUSED:\s+ttsOutput=\s?(.*?)(?:\s{2,}queueMode|$)")
_RE_EDGE = re.compile(r"FocusProcessor-LogicalNav: Reach edge")
# Only the actor's own lines: node dumps and pipeline lines list SHOW_ON_SCREEN too.
_RE_SCROLL = re.compile(r"AutoScrollActor: (?:ScrollAction=ACTION_SCROLL|Perform ACTION_SHOW_ON_SCREEN"
                        r"|Perform scroll action)")


class TalkBackLog:
    """``adb logcat --pid=<TalkBack>`` parsed into (t, kind, value) events."""

    def __init__(self, serial: str) -> None:
        self.serial = serial
        self.proc: Any = None
        self.events: List[Tuple[float, str, str]] = []
        self._lock = threading.Lock()
        self.lines = 0

    def start(self) -> bool:
        pid = device.talkback_pid(self.serial)
        if pid is None:
            return False
        try:
            self.proc = device.popen(self.serial, ["logcat", "-v", "epoch", f"--pid={pid}", "-T", "1"])
        except OSError:
            return False
        threading.Thread(target=self._reader, name="tb-logcat", daemon=True).start()
        return True

    def _reader(self) -> None:
        out = self.proc.stdout
        while True:
            try:
                raw = out.readline()
            except (OSError, ValueError):
                return
            if not raw:
                return
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            now = time.monotonic()
            ev = None
            m = _RE_TTS.search(line)
            if m:
                ev = ("tts", m.group(1).strip())
            elif _RE_EDGE.search(line):
                ev = ("edge", "")
            elif _RE_SCROLL.search(line):
                ev = ("scroll", "")
            with self._lock:
                self.lines += 1
                if ev:
                    self.events.append((now, ev[0], ev[1]))

    def since(self, t: float, kind: Optional[str] = None) -> List[Tuple[float, str, str]]:
        with self._lock:
            return [e for e in self.events if e[0] >= t and (kind is None or e[1] == kind)]

    @property
    def verbose(self) -> bool:
        with self._lock:
            return any(e[1] == "tts" for e in self.events)

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        for fn in ("terminate", "kill"):
            try:
                getattr(proc, fn)()
                proc.wait(timeout=2)
                return
            except Exception:  # noqa: BLE001
                continue


# --------------------------------------------------------------------------- #
# Predicted order (P): T1's talkback.order when present, else a11y's model
# --------------------------------------------------------------------------- #
@dataclass
class PStop:
    key: str
    label: str
    speak: str
    bounds: Rect
    window: int
    cls: str
    ctx: Optional[str] = None  # item_context: the list item it sits in
    item_root: bool = False
    #: the re-model (1 = the first) that added it: the model learned of it only then (L4)
    added: Optional[int] = None

    @property
    def sig(self) -> str:
        return signature(self.cls, self.label)


def _window_of(roots: List[Tuple[int, Dict[str, Any]]]) -> Dict[int, int]:
    out: Dict[int, int] = {}
    for win, root in roots:
        stack = [root]
        while stack:
            n = stack.pop()
            out[id(n)] = win
            stack.extend(n.get("children") or [])
    return out


def _ordered_nodes(windows: List[Dict[str, Any]], d: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Optional[str]], str]:
    """(stop node dicts in reading order, their model announcements, the source)."""
    roots = [w["root"] for w in windows if w.get("root")]
    try:
        # T1's literal port of TalkBack's traversal, when it is merged.
        tborder = importlib.import_module("inspector_widget.talkback.order")
        # The whole dump (windows, modality, importance), spoken as for a key-driven walk.
        res = tborder.reading_order(d, keyboard=True)
        return list(res["_nodes"]), [e.get("speak") for e in res["focus_order"]], "talkback.order"
    except Exception:  # noqa: BLE001 - not merged yet, or it could not model this dump
        pass
    # a11y.a11y_to_dict numbers the stops on the nodes (a11y.reading_order, which
    # leaves out windows under a modal one and Views TalkBack never sees).
    stops: List[Tuple[int, Dict[str, Any]]] = []
    stack = list(roots)
    while stack:
        n = stack.pop()
        if isinstance(n.get("order"), int):
            stops.append((n["order"], n))
        stack.extend(n.get("children") or [])
    stops.sort(key=lambda t: t[0])
    speak = {e.get("order"): e.get("speak") for e in d.get("focus_order") or []}
    return [n for _o, n in stops], [speak.get(o) for o, _n in stops], "a11y.reading_order"


def predict(resp: Any, legacy: bool) -> Tuple[List[PStop], str, Dict[str, Any]]:
    """The model's reading order for one dump, keyed like the walk."""
    from .. import a11y
    d = a11y.a11y_to_dict(resp)
    windows = d.get("windows") or []
    win_of = _window_of([(w["root_view_id"], w["root"]) for w in windows if w.get("root")])
    compose_hosts = {int(n.get("host_view_id") or 0) for n in _iter_dicts(windows)
                     if int(n.get("virtual_id", HOST_VIEW_ID)) == HOST_VIEW_ID
                     and "compose" in ((n.get("provider_class") or "") + (n.get("class_name") or "")).lower()}
    nodes, speaks, source = _ordered_nodes(windows, d)
    parent_of: Dict[int, Dict[str, Any]] = {}
    for m in _iter_dicts(windows):
        for c in m.get("children") or []:
            parent_of[id(c)] = m
    stops: List[PStop] = []
    for n, sp in zip(nodes, speaks, strict=False):
        kids = [(c.get("text") or "", c.get("content_description") or "") for c in n.get("children") or []]
        label = _label(n.get("text") or "", n.get("content_description") or "", kids)
        cls = n.get("class_name") or ""
        b = (n.get("bounds") or {}).get("layout") or {}
        win = win_of.get(id(n), 0)
        key = node_key(win, int(n.get("host_view_id") or 0), int(n.get("virtual_id", HOST_VIEW_ID)),
                       cls, label, legacy, compose_hosts)
        ctx, item_root = item_context(
            n, lambda x: parent_of.get(id(x)), lambda x: x.get("children") or [],
            lambda x: x.get("content_description") or x.get("text") or "", _dict_scrolls)
        stops.append(PStop(key, label, sp or _dict_speech(n), (b.get("x", 0), b.get("y", 0),
                                                              b.get("w", 0), b.get("h", 0)),
                           win, cls.rsplit(".", 1)[-1], ctx, item_root))
    covered = {w["root_view_id"]: w["covered_by"] for w in windows if w.get("covered_by") is not None}
    return stops, source, {"covered_windows": covered, "dump": d}


def predict_initial(resp: Any, window: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Where TalkBack puts focus when ``window`` (root_view_id; default the active
    window) appears: talkback.order's initial-focus rule, which skips a first stop
    that reads as the window title (the WindowInfo title, else the window's first
    text). ``{"key", "how", "title", "title_source", "skipped"}``, or None when the
    model cannot say."""
    try:
        from .. import a11y
        from .order import Navigator
        from .tree import build
        nav = Navigator(build(a11y.a11y_to_dict(resp)))
        win = None
        if window is not None:
            win = next((w for w in nav.windows if w.root_view_id == window), None)
        init = nav.initial_focus(win)
    except Exception:  # noqa: BLE001 - the walk reports without it
        return None
    return {k: init.get(k) for k in ("key", "how", "title", "title_source", "skipped")}


def _dict_scrolls(n: Dict[str, Any]) -> bool:
    if (n.get("class_name") or "") == _WEBVIEW or (n.get("class_name") or "") in _PAGE_SCROLLERS:
        return False
    return "scrollable" in (n.get("flags") or ()) or any(
        isinstance(a, dict) and a.get("id") in _SCROLL_ACTIONS for a in n.get("actions") or ())


def _iter_dicts(windows: List[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
    stack = [w["root"] for w in windows if w.get("root")]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.get("children") or [])


def _dict_speech(n: Dict[str, Any]) -> str:
    parts: List[str] = []

    def add(m: Dict[str, Any]) -> None:
        if m.get("content_description"):
            parts.append(m["content_description"])
            return
        if m.get("text"):
            parts.append(m["text"])
        for c in m.get("children") or []:
            fl = set(c.get("flags") or [])
            if "visible_to_user" in fl and not {"clickable", "long_clickable", "focusable"} & fl:
                add(c)

    add(n)
    role = _ROLE_WORDS.get((n.get("class_name") or "").rsplit(".", 1)[-1])
    if role:
        parts.append(role)
    return ", ".join(parts)[:160]


class Model:
    """P, merged across re-models as auto-scroll brings new nodes in."""

    def __init__(self) -> None:
        self.stops: List[PStop] = []
        self.source = ""
        self.remodels = 0
        self.covered_windows: Dict[int, int] = {}
        self.aliases: Dict[str, str] = {}  # a re-minted key -> the key the model has

    def build(self, resp: Any, legacy: bool) -> None:
        self.stops, self.source, meta = predict(resp, legacy)
        self.covered_windows = meta["covered_windows"]

    def remodel(self, resp: Any, legacy: bool) -> None:
        """Merge the order predicted on a newer dump: each unknown stop goes in
        right after the stop the new order puts before it, past the stops that
        followed that one and are gone now (scrolled off: what the scroll
        revealed comes after them)."""
        new, _source, meta = predict(resp, legacy)
        self.remodels += 1
        self.covered_windows.update(meta["covered_windows"])
        known_of = [self.match(s.key, s.sig, s.bounds, ctx=s.ctx, item_root=s.item_root)
                    for s in new]
        present = {k.key for k in known_of if k is not None}
        keys = [s.key for s in self.stops]
        prev: Optional[str] = None
        for j, (s, known) in enumerate(zip(new, known_of, strict=True)):
            if known is not None:
                if known.key != s.key and "#" not in known.key:
                    self.aliases[s.key] = known.key
                prev = known.key
                continue
            if s.key in keys:  # a View (or ComposeView cell) rebound to another item
                s = replace(s, key=f"{s.key}#{sum(k.split('#')[0] == s.key for k in keys)}")
            s = replace(s, added=self.remodels)
            if prev is None:
                # nothing known before it in the new order: it goes before the first known
                # stop after it (the top of a page), not after everything the model knows
                nxt = next((k for k in known_of[j + 1:] if k is not None), None)
                at = keys.index(nxt.key) if nxt is not None and nxt.key in keys else len(keys)
            else:
                at = keys.index(prev) + 1 if prev in keys else len(keys)
            while prev in keys and at < len(keys) and keys[at] not in present:
                at += 1
            keys.insert(at, s.key)
            self.stops.insert(at, s)
            prev = s.key

    def match(self, key: Optional[str], sig: str, box: Rect, *, ctx: Optional[str] = None,
              item_root: bool = False) -> Optional[PStop]:
        """The model's stop for a node: by key (or alias), else by signature +
        overlap. A RecyclerView rebinds a View (a ComposeView cell too) to other
        items as it scrolls: the same key with another label elsewhere is another
        stop (``<key>#<n>``, made by :meth:`remodel`), and so is a list item View
        that shows another label in the very slot it had (V6: the row View that
        showed "Mail 3" scrolled back into that slot showing "Mail 31"), or the same
        key in another list item (``ctx``, :func:`item_context`). A label that changed
        in place outside a list item, or went empty (clipped), is the same node."""
        key = self.aliases.get(key, key) if key else key
        if key:
            # a View key can be a recycled View (another item: its list item tells); a
            # Compose key names one node (its semantics id is not reused)
            view = key.startswith("view:")
            same = [s for s in self.stops if s.key.split("#")[0] == key]
            for s in same:
                if s.sig == sig and (not view or _ctx_ok(s.ctx, ctx)):
                    return s
            for s in same:
                if s.key != key or (view and not _ctx_ok(s.ctx, ctx)):
                    continue
                if not (iou(s.bounds, box) >= 0.5 or _unlabelled(sig) or _unlabelled(s.sig)):
                    continue
                if (item_root or s.item_root or _alone(ctx, s.ctx)) and key.startswith("view:") \
                        and s.sig != sig and not _unlabelled(sig) and not _unlabelled(s.sig):
                    # a recycled item View showing another item; or a View inside a list item
                    # whose only text is its own (the item has nothing else to tell them apart
                    # by: V6 BAD_B's row title), showing another item's text in its old slot
                    continue
                return s
            if same:
                return None
        for s in self.stops:
            if same_node(None, sig, box, None, s.sig, s.bounds, ctx_a=ctx, ctx_b=s.ctx):
                return s
        return None

    def keys(self) -> List[str]:
        return [s.key for s in self.stops] + list(self.aliases)

    def get(self, key: str) -> Optional[PStop]:
        key = self.aliases.get(key, key)
        for s in self.stops:
            if s.key == key:
                return s
        return None


# --------------------------------------------------------------------------- #
# The driver: TalkBack on, an injector, a focus reader, and the safety rules
# --------------------------------------------------------------------------- #
class WalkError(RuntimeError):
    """A walk that could not run. ``code``: focus_unreadable, keymap_unknown,
    start_not_found, app_left_foreground (plus device/injector codes)."""

    def __init__(self, code: str, message: str, hint: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


class Driver:
    """TalkBack on (restored on exit unless ``leave_on``), a key injector and a
    focus reader, under the per-device lock. Use as a context manager."""

    def __init__(self, session: Any, *, injector: str = "auto", utterance: str = "auto",
                 leave_on: bool = False, step_timeout_ms: int = STEP_TIMEOUT_MS,
                 settle_ms: int = SETTLE_MS, what: str = "tb_walk") -> None:
        self.session = session
        self.serial = session.serial
        self.package = session.package
        self.injector_kind = injector
        self.utterance = utterance
        self.leave_on = leave_on
        self.timeout_s = step_timeout_ms / 1000
        self.quiet_s = settle_ms / 1000
        self.what = what
        self.inj: Optional[inject.Injector] = None
        self.reader: FocusReader = make_reader(session)
        self.log: Optional[TalkBackLog] = None
        self.enabled: Dict[str, Any] = {}
        self.turned_on = False
        self.restored: Optional[Dict[str, Any]] = None
        self.restore_error: Optional[str] = None
        self.notes: List[str] = []
        self.seek_presses = 0
        self.start_via = "keys"
        self._keymaps_tried: List[str] = []
        self._lock_cm: Any = None

    # ---- lifecycle ------------------------------------------------------- #
    def __enter__(self) -> "Driver":
        self._lock_cm = device.device_lock(self.serial, self.what)
        self._lock_cm.__enter__()
        try:
            self.enabled = device.enable(self.serial, self.package,
                                         verbose_log=self.utterance == "logcat")
            self.turned_on = bool(self.enabled.get("changed"))
            if isinstance(self.enabled.get("log_level"), str):
                self.notes.append(f"log level {self.enabled['log_level']}")
            if self.enabled.get("warning_foreground"):
                self.notes.append(self.enabled["warning_foreground"])
            self._wait_services_on()
            self.inj = inject.open_injector(self.serial, self.injector_kind)
            if self.utterance in ("auto", "logcat"):
                self.log = TalkBackLog(self.serial)
                if not self.log.start():
                    self.log = None
                    if self.utterance == "logcat":
                        self.notes.append("logcat: TalkBack's process was not found; "
                                          "utterances come from the model")
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if self.log is not None:
                self.log.stop()
            if self.inj is not None:
                self.inj.close()
            if self.turned_on and not self.leave_on:
                try:
                    self.restored = device.restore(self.serial)
                except Exception as err:  # noqa: BLE001 - reported in the result
                    self.restore_error = f"{type(err).__name__}: {err}"
        finally:
            if self._lock_cm is not None:
                self._lock_cm.__exit__(None, None, None)
                self._lock_cm = None

    def settle_initial(self, timeout_s: Optional[float] = None) -> Snapshot:
        """After TalkBack has just started it puts focus on the window (550ms after
        the window change); wait for that, then for it to stay put, so a walk
        starts where a user would."""
        snap = self.reader.snapshot()
        if not self.turned_on or snap.key is not None:
            return snap
        return self.reader.wait_change(None, INITIAL_FOCUS_S if timeout_s is None else timeout_s,
                                       0.3).snap

    def _wait_services_on(self, timeout_s: float = 5.0) -> None:
        """An agent that reports ``a11y-services=`` waits until it says on."""
        deadline = time.monotonic() + timeout_s
        while True:
            snap = self.reader.snapshot()
            diag = snap.index.diagnostics
            if "a11y-services=off" not in diag or time.monotonic() >= deadline:
                return
            time.sleep(0.2)

    # ---- keys ------------------------------------------------------------ #
    def press(self, action: str) -> Tuple[float, int]:
        """Press ``action``; returns (monotonic time sent, send+ack ms)."""
        assert self.inj is not None
        t = time.monotonic()
        return t, self.inj.press(action)

    def wait(self, prev_key: Optional[str], since: float) -> WaitResult:
        abort = None
        if self.log is not None:
            log = self.log
            abort = lambda: bool(log.since(since, "edge"))  # noqa: E731
        return self.reader.wait_change(prev_key, self.timeout_s, self.quiet_s, abort)

    def prove(self, cur_key: Optional[str]) -> WaitResult:
        """Press "next" (the only combo allowed unproven) until focus moves (the
        first press may only reach an edge), then allow the rest. Tries the classic
        Alt keymap when Meta+Right does nothing."""
        assert self.inj is not None
        while True:
            self._keymaps_tried.append(self.inj.keymap)
            for _ in range(2):
                t, _ms_ = self.press("next")
                self.seek_presses += 1
                w = self.wait(cur_key, t)
                if w.moved:
                    self.inj.mark_proven()
                    return w
            if not self.try_other_keymap():
                break
        raise WalkError("keymap_unknown", "TalkBack did not move accessibility focus on Meta+Right "
                                          "or Alt+Right; no other key was sent",
                        hint="Check TalkBack is running (talkback status) and that the app has "
                             "focusable content.")

    def return_to(self, key: Optional[str], snap: Snapshot) -> Snapshot:
        """Put focus back on ``key`` after the keymap proof moved it, with TalkBack's
        own "prev" (up to three presses: the proof's "next" may have hit an edge and
        wrapped). TalkBack records only its own focus moves, and restores focus from
        that record after back, so A11yAct is the last resort here."""
        if key is None or snap.key == key:
            return snap
        for _ in range(3):
            t, _ = self.press("prev")
            self.seek_presses += 1
            snap = self.wait(snap.key, t).snap
            if snap.key == key:
                return snap
        if isinstance(self.reader, A11yFocusReader):
            from .. import a11y
            d = a11y.a11y_act_to_dict(self.session.a11y_act(node_key=key, action="accessibility_focus"))
            if d.get("performed"):
                snap = self.reader.wait_change(snap.key, self.timeout_s, self.quiet_s).snap
        return snap

    def try_other_keymap(self) -> bool:
        """Switch an unproven keyboard to the classic (Alt) keymap once."""
        keys = self.inj
        if not isinstance(keys, inject.UinputKeyboard) or keys.proven or "classic" in self._keymaps_tried:
            return False
        if keys.keymap not in self._keymaps_tried:
            self._keymaps_tried.append(keys.keymap)
        keys.keymap = "classic"
        self._keymaps_tried.append("classic")
        self.notes.append("Meta+Right did not move focus; using TalkBack's classic Alt keymap")
        return True

    def foreground_ok(self) -> bool:
        top = device.top_package(self.serial)
        return top == self.package


# --------------------------------------------------------------------------- #
# The walk
# --------------------------------------------------------------------------- #
@dataclass
class Step:
    i: int
    key: Optional[str]
    moved: bool = True
    via: str = "next"
    edge: bool = False
    ms: Optional[int] = None
    t: float = 0.0
    node: Optional[Node] = None
    scrolled: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)
    index: Optional[DumpIndex] = None  # the dump the node came from (window meta)


def _match(sel: str, n: Optional[Node]) -> bool:
    if n is None:
        return False
    if ":" in sel and n.key == sel:
        return True
    s = sel.strip().lower()
    return bool(s) and (s == n.label.lower() or s in n.label.lower() or s == (n.cd or "").lower())


def run_walk(session: Any, *, start: str = "current", direction: str = "next",
             max_steps: int = DEFAULT_MAX_STEPS, until: str = "wrap",
             expect: Optional[Sequence[str]] = None, step_timeout_ms: int = STEP_TIMEOUT_MS,
             settle_ms: int = SETTLE_MS, recapture: str = "on_unknown", utterance: str = "auto",
             injector: str = "auto", leave_on: bool = False, max_lines: int = 60,
             max_bytes: int = 5000, timeout_s: float = 300.0, save: bool = True,
             hook: Optional[Any] = None, full: bool = False) -> Dict[str, Any]:
    """Walk TalkBack through the app on ``session`` and return the compact result.

    ``start``: ``current`` (where focus is now), ``first`` (TalkBack's "first"
    shortcut) or a node key / part of a label (pressed "next" until focus gets
    there). ``until``: ``wrap`` (one full lap: stop when focus comes back to the
    first stop after an edge), ``edge``, ``loop`` (the first repeated move) or
    ``steps`` (exactly ``max_steps`` presses). A lap is full once focus, past an
    edge, lands on a stop the walk has already read (the first one it reads after
    the wrap may be new: TalkBack's initial focus skips a title that repeats the
    window title). A walk also ends on ``stuck`` (two
    presses in a row that move nothing), ``loop`` (a move repeats with no edge in
    between: TalkBack would never reach the end), ``left_app``, ``lost`` (no node held
    focus after a press twice in a row, or again at the same place after
    starting over) and ``timeout``.

    The full record (every step, the predicted order, findings) is saved under
    ``<store>/walks/<walk id>.json``; the returned dict is at most ``max_bytes``
    of JSON with one line per step (``full``: the record itself).

    ``hook`` (the capture surface's, :mod:`inspector_widget.ops`) is told when
    TalkBack has settled (``hook.start(snapshot)``; ``hook.resolve(start)`` may
    then turn a capture ref into a node key), after every move
    (``hook.step(step, snapshot)``: it recaptures when focus reached a node no
    capture holds) and before TalkBack is restored (``hook.finish(snapshot,
    ended)``).
    """
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}")
    if until not in UNTIL:
        raise ValueError(f"until must be one of {UNTIL}")
    if utterance not in UTTERANCE:
        raise ValueError(f"utterance must be one of {UTTERANCE}")
    if recapture not in RECAPTURE:
        raise ValueError(f"recapture must be one of {RECAPTURE}")
    max_steps = max(1, min(int(max_steps), MAX_STEPS_CAP))
    t_start = time.monotonic()
    deadline = t_start + timeout_s
    drv = Driver(session, injector=injector, utterance=utterance, leave_on=leave_on,
                 step_timeout_ms=step_timeout_ms, settle_ms=settle_ms)
    steps: List[Step] = []
    model = Model()
    ended = "max_steps"
    cycle: List[str] = []
    edge_info: Optional[Dict[str, Any]] = None
    tts: Dict[int, str] = {}
    initial: Optional[Dict[str, Any]] = None
    with drv:
        cur = drv.settle_initial()
        start_resp, start_idx = cur.resp, cur.index
        legacy = bool(cur.index.legacy)
        model.build(cur.resp, legacy)
        if drv.turned_on and cur.key is not None:
            initial = predict_initial(cur.resp)
            if initial is not None:
                initial["actual"] = cur.key
        if cur.focus is None and not drv.foreground_ok():
            raise WalkError("app_left_foreground", f"{drv.package} is not in the foreground",
                            hint=f"Open {drv.package} on the device, then retry.")
        if hook is not None:
            hook.start(cur)
            start = hook.resolve(start)
        cur = _seek_start(drv, cur, start, direction, max_steps)
        steps.append(Step(0, cur.key, via="start", node=cur.focus, t=cur.t, index=cur.index))
        prev_idx = cur.index
        transitions: Dict[Tuple[Optional[str], ...], int] = {}
        last_edge_at = -1
        edge_at: Optional[int] = None  # the step edge_info was taken at
        no_moves = 0
        lost = 0
        last_lost_at = -1
        for i in range(max_steps):
            if time.monotonic() >= deadline:
                ended = "timeout"
                break
            if i and i % 10 == 0 and not drv.foreground_ok():
                # Covered by another app's window while focus stayed on our node.
                ended = "left_app"
                steps.append(Step(len(steps), None, via="left_app", t=time.monotonic(),
                                  extra={"top": device.top_activity(drv.serial)}))
                break
            # Guard: re-read before every press. Focus that moved on its own was
            # taken by the app; focus that vanished may mean another app is on top.
            pre = drv.reader.snapshot()
            if pre.key != cur.key:
                if pre.key is None:
                    if not drv.foreground_ok():
                        ended = "left_app"
                        steps.append(Step(len(steps), None, via="left_app", t=pre.t,
                                          extra={"top": device.top_activity(drv.serial)}))
                        break
                else:
                    last = steps[-1] if steps else None
                    if last is not None and last.i > 0 and (last.edge or last.via == "lost"):
                        # The last press timed out (no move, or no focus) and its move came
                        # after the wait: TalkBack's own (a slow auto-scroll: NiA's took
                        # 900-1200ms), not the app's. The press moved after all.
                        scrolled = _scrolled_key(drv, last.index or prev_idx, pre, last.t)
                        st = Step(last.i, pre.key, via="autoscroll" if scrolled else "late",
                                  node=pre.focus, t=last.t, index=pre.index, scrolled=scrolled,
                                  extra={"late": True, "wall_ms": _ms(pre.t - last.t)})
                        steps[-1] = st
                        no_moves = lost = 0
                        last_edge_at = max((j for j, x in enumerate(steps) if x.edge), default=-1)
                        last_lost_at = max((j for j, x in enumerate(steps) if x.via == "lost"),
                                           default=-1)
                        if edge_at is not None and not steps[edge_at].edge:
                            edge_info, edge_at = None, None  # it was no edge
                        if hook is not None:
                            hook.step(st, pre)
                        if recapture == "on_unknown" and pre.focus is not None and model.match(
                                pre.key, pre.focus.sig, pre.focus.bounds, ctx=pre.focus.ctx,
                                item_root=pre.focus.item_root) is None:
                            model.remodel(pre.resp, legacy)
                            st.extra["remodel"] = True
                    else:
                        # None -> focus: TalkBack's own initial focus; else the app took it.
                        via = "stolen" if cur.key is not None else "initial"
                        steps.append(Step(len(steps), pre.key, via=via, node=pre.focus, t=pre.t,
                                          index=pre.index))
                        no_moves = 0  # the next press moves on from where the app put focus
                cur, prev_idx = pre, pre.index
            t_sent, _send_ms = drv.press(direction)
            w = drv.wait(cur.key, t_sent)
            if not w.moved and not w.aborted and _scroll_in_flight(drv, w.snap, t_sent):
                # TalkBack is still auto-scrolling: its move lands once the scroll settles
                events = list(w.snap.events)
                w = drv.wait(cur.key, t_sent)
                w.snap.events = events + list(w.snap.events)
            stolen, new = _press_target(w.snap, cur.key, direction)
            if stolen is not None:
                steps.append(Step(len(steps), stolen.key, via="stolen", node=stolen.focus, t=t_sent,
                                  index=stolen.index))
                cur, prev_idx = stolen, stolen.index
                w = replace(w, moved=new.key != cur.key)
            elif new is not w.snap:
                w = replace(w, snap=new, moved=new.key != cur.key, lost=False)
            if w.moved and direction == "next":
                drv.inj.mark_proven()  # type: ignore[union-attr]
            if new.key is None and cur.key is not None:
                top = device.top_activity(drv.serial)
                if not (top or "").startswith(drv.package + "/"):
                    ended = "left_app"
                    steps.append(Step(len(steps), None, via="left_app", t=new.t, extra={"top": top}))
                    break
                # Still in the app, but no node holds focus (cleared and not restored):
                # the next press starts again from the top of the window.
                steps.append(Step(len(steps), None, via="lost", t=t_sent,
                                  scrolled=_key_of(detect_scroll(prev_idx, new.index))))
                lost += 1
                last_lost_at = len(steps) - 1
                cur, prev_idx = new, new.index
                if lost >= 2:
                    ended = "lost"
                    break
                continue
            if not w.moved:
                no_moves += 1
                steps.append(Step(len(steps), cur.key, moved=False, edge=True, via="edge",
                                  node=cur.focus, t=t_sent, index=cur.index))
                if no_moves >= 2 and not drv.inj.proven and drv.try_other_keymap():  # type: ignore[union-attr]
                    del steps[-2:]  # those presses were not edges: the keymap was wrong
                    no_moves = 0
                    continue
                if edge_info is None and cur.focus is not None:
                    edge_info = _edge_info(cur.index, cur.focus, direction)
                    edge_at = len(steps) - 1
                last_edge_at = len(steps) - 1
                if until == "edge":
                    ended = "edge"
                    break
                if no_moves >= 2:
                    ended = "stuck"
                    break
                continue
            lost = 0
            via = "wrap" if no_moves else "next"
            if via == "next" and new.focus is not None and cur.focus is not None \
                    and new.focus.window != cur.focus.window:
                via = "window"
            scrolled = _scrolled_key(drv, prev_idx, new, t_sent)
            if scrolled is not None and via == "next":
                via = "autoscroll"
            no_moves = 0
            st = Step(len(steps), new.key, via=via, ms=w.first_ms, node=new.focus, t=t_sent,
                      index=new.index,
                      scrolled=scrolled, extra={"wall_ms": _ms(time.monotonic() - t_sent)})
            steps.append(st)
            if hook is not None:
                hook.step(st, new)
            if recapture == "on_unknown" and (
                    new.key not in model.keys() if new.focus is None
                    else model.match(new.key, new.focus.sig, new.focus.bounds, ctx=new.focus.ctx,
                                     item_root=new.focus.item_root) is None):
                model.remodel(new.resp, legacy)
                st.extra["remodel"] = True
            # A move is the same move only with the same content: RecyclerView rebinds
            # the same Views to other items as it scrolls.
            tr = (cur.key, cur.focus.sig if cur.focus is not None else None,
                  new.key, new.focus.sig if new.focus is not None else None)
            seen_at = transitions.get(tr)
            transitions[tr] = len(steps) - 1
            cur, prev_idx = new, new.index
            if until == "wrap" and last_edge_at >= 0 and new.focus is not None and any(
                    _seen_again(s, new) for s in steps[:last_edge_at]):
                # Past the edge and back on a stop this walk already read: a full lap.
                ended = "wrap"
                break
            if seen_at is not None and until != "steps":
                if last_lost_at > seen_at:
                    ended = "lost"  # focus is lost at the same place every lap
                elif last_edge_at > seen_at:
                    ended = "wrap"
                else:
                    ended = "loop"
                    cycle = [s.key for s in steps[seen_at - 1:len(steps) - 2] if s.key]
                break
        if drv.log is not None and drv.log.verbose:
            time.sleep(0.3)  # the last announcement lands ~100ms after its press
            tts = _attribute_tts(steps, drv.log)
        if hook is not None:
            hook.finish(cur, ended)  # still with TalkBack on: the screen it walked
    return _finish(drv, steps, model, ended=ended, cycle=cycle, edge_info=edge_info, start=start,
                   initial=initial, start_resp=start_resp, start_idx=start_idx,
                   direction=direction, until=until, expect=expect, tts=tts, t_start=t_start,
                   max_lines=max_lines, max_bytes=max_bytes, save=save, full=full)


def _seen_again(s: "Step", new: Snapshot) -> bool:
    """Whether ``new``'s focus is the node step ``s`` read: the same key, or a re-minted
    Compose id (the same signature where the boxes overlap, in the same list item) whose
    old key is gone from the dump. A node still in the dump under its own key is another
    node, however alike (NiA: the HEADLINES chip of the card a scroll put in the slot of the
    one read before)."""
    f = new.focus
    if s.node is None or f is None:
        return False
    if s.key is not None and s.key == new.key:
        # a View a list rebound to another item (another label in a list item, or another
        # item around it) is another stop: no false wrap on a recycled row (L1, V6 BAD_B)
        if s.key.startswith("view:") and f.ctx is not None and s.node.ctx is not None \
                and not _unlabelled(f.sig) and not _unlabelled(s.node.sig) \
                and (not _ctx_ok(s.node.ctx, f.ctx) or (s.node.sig != f.sig and (
                    f.item_root or s.node.item_root or _alone(s.node.ctx, f.ctx)))):
            return False
        return True
    if s.key is not None and s.key in new.index.nodes:
        return False
    return same_node(s.key, s.node.sig, s.node.bounds, new.key, f.sig, f.bounds,
                     ctx_a=s.node.ctx, ctx_b=f.ctx)


def _seek_start(drv: Driver, cur: Snapshot, start: str, direction: str, max_presses: int) -> Snapshot:
    """Put focus where the walk starts. Anything but ``current`` + ``next`` first
    proves the keymap with "next" (see :class:`.inject.KeyGuard`)."""
    if start == "current" and direction == "next":
        return cur
    if start not in ("current", "first"):
        acted = _act_focus(drv, cur, start)
        if acted is not None:
            if direction == "prev":  # prove the keymap, then back onto the target
                return drv.return_to(acted.key, drv.prove(acted.key).snap)
            return acted
    before = cur
    w = drv.prove(cur.key)
    snap = w.snap
    if start == "current":
        return drv.return_to(before.key, snap)  # back to where the user was
    if start == "first":
        t, _ = drv.press("first")
        w2 = drv.wait(snap.key, t)
        drv.seek_presses += 1
        return w2.snap if w2.moved or w2.snap.key else snap
    if _match(start, before.focus):
        t, _ = drv.press("prev")
        drv.seek_presses += 1
        return drv.wait(snap.key, t).snap
    while not _match(start, snap.focus):
        if drv.seek_presses >= max_presses:
            raise WalkError("start_not_found", f"no focus stop matching {start!r} within "
                                               f"{drv.seek_presses} presses",
                            hint="Pass a node key or part of the label as spoken.")
        t, _ = drv.press("next")
        snap = drv.wait(snap.key, t).snap
        drv.seek_presses += 1
    return snap


def _key_of(n: Optional[Node]) -> Optional[str]:
    return n.key if n is not None else None


def _scroll_in_flight(drv: Driver, snap: Snapshot, t_sent: float) -> bool:
    """Whether a container scrolled since the press (a VIEW_SCROLLED event, or TalkBack's
    AutoScrollActor in its log) while focus has not landed yet."""
    if any(e.get("type") == "VIEW_SCROLLED" for e in snap.events):
        return True
    return drv.log is not None and bool(drv.log.since(t_sent, "scroll"))


def _scrolled_key(drv: Driver, prev_idx: DumpIndex, new: Snapshot, t_sent: float) -> Optional[str]:
    """The container that scrolled during a step: a VIEW_SCROLLED event (A11yFocus),
    else nodes that moved between the dumps, else TalkBack's AutoScrollActor log."""
    for e in new.events:
        if e.get("type") == "VIEW_SCROLLED" and e.get("node_key"):
            return e["node_key"]
    moved = detect_scroll(prev_idx, new.index)
    if moved is not None:
        return moved.key
    if new.focus is not None and drv.log is not None and drv.log.since(t_sent, "scroll"):
        return _key_of(new.index.scroll_container(new.focus))
    return None


def _act_focus(drv: Driver, cur: Snapshot, start: str) -> Optional[Snapshot]:
    """Put TalkBack's focus straight on the node ``start`` names (A11yAct
    ACTION_ACCESSIBILITY_FOCUS; TalkBack's next press continues from there).
    None when the agent cannot, so the caller presses "next" instead."""
    if not isinstance(drv.reader, A11yFocusReader):
        return None
    target: Optional[str] = None
    if ":" in start:
        from .. import a11y
        try:
            a11y.parse_node_key(start)
            target = start
        except ValueError:
            target = None
    if target is None:
        # A label: prefer a node the model reads as a stop (not a Text inside a row).
        stops = {s.key for s in predict(cur.resp, bool(cur.index.legacy))[0]} if cur.resp else set()
        found = [n for n in cur.index.order if _match(start, n)]
        found.sort(key=lambda n: (n.key not in stops, not n.actionable()))
        target = found[0].key if found else None
    if target is None:
        return None
    from .. import a11y
    d = a11y.a11y_act_to_dict(drv.session.a11y_act(node_key=target, action="accessibility_focus"))
    if not d.get("performed"):
        drv.notes.append(f"A11yAct could not focus {target}: {d.get('error')}")
        return None
    snap = drv.reader.wait_change(cur.key, drv.timeout_s, drv.quiet_s).snap
    if snap.key == target or _match(start, snap.focus):
        drv.start_via = "a11y_act"
        return snap
    drv.notes.append(f"A11yAct focused {target} but TalkBack's focus is on {snap.key}")
    return None


def _press_target(snap: Snapshot, cur_key: Optional[str],
                  direction: str) -> Tuple[Optional[Snapshot], Snapshot]:
    """(a steal, where the press took focus) when the app moved focus inside the
    press's wait. TalkBack focused A and then focus went back against the walk to
    B: the press's move is A, and the next guard read reports B as stolen. Or the
    app took focus back to A just before the press, which then moved on from A:
    A is the steal, and the press's move goes from A to where focus is now."""
    focused = [e["node_key"] for e in snap.events
               if e.get("type") == "VIEW_ACCESSIBILITY_FOCUSED" and e.get("node_key")]
    last = snap.focus
    if len(focused) < 2 or last is None:
        return None, snap
    order = snap.index.order
    first = snap.index.nodes.get(focused[0])
    prev = snap.index.nodes.get(cur_key) if cur_key else None
    if first is None or first.key == last.key or first not in order or last not in order:
        return None, snap

    def behind(a: Node, b: Node) -> bool:
        return order.index(a) < order.index(b) if direction == "next" else order.index(a) > order.index(b)

    at_first = Snapshot(snap.index, snap.t, snap.resp, focus_node=first, events=snap.events,
                        uptime_ms=snap.uptime_ms)
    if behind(last, first):
        return None, at_first
    if prev is not None and prev in order and behind(first, prev):
        return at_first, snap
    return None, snap


def _edge_info(idx: DumpIndex, n: Node, direction: str) -> Optional[Dict[str, Any]]:
    """At an edge: the scrollable container around the last stop, whether it still
    advertises scrolling in the walk's direction (TalkBack should have), and how
    many texts after the stop, in that container (else in its parent), are hidden
    (clipped or scrolled away): content the walk could not reach."""
    c = idx.scroll_container(n)
    scope = c if c is not None else n.parent
    out: Dict[str, Any] = {}
    if c is not None:
        fwd = {"forward", "down", "right", "page_down", "page_right"}
        back = {"backward", "up", "left", "page_up", "page_left"}
        want = fwd if direction == "next" else back
        can = sorted(_SCROLL_ACTIONS[a] for a in c.actions if a in _SCROLL_ACTIONS and _SCROLL_ACTIONS[a] in want)
        out.update(container=c.key, container_cls=c.simple_cls, can_scroll=can)
    if scope is not None and direction == "next":
        members = {id(x) for x in _descendants(scope)}
        after = idx.order[idx.order.index(n) + 1:] if n in idx.order else []
        hidden = [x for x in after if id(x) in members and (x.text or x.cd)
                  and "visible_to_user" not in x.flags]
        if hidden:
            out["hidden_after"] = len(hidden)
            out["hidden_first"] = (hidden[0].text or hidden[0].cd)[:40]
    return out or None


def _descendants(n: Node) -> List[Node]:
    out: List[Node] = []
    stack = list(n.children)
    while stack:
        c = stack.pop()
        out.append(c)
        stack.extend(c.children)
    return out


def _attribute_tts(steps: List[Step], log: TalkBackLog) -> Dict[int, str]:
    """ttsOutput lines -> the step whose press window [t_i, t_{i+1}) holds them."""
    out: Dict[int, str] = {}
    events = log.since(0.0, "tts")
    bounds = [s.t for s in steps] + [float("inf")]
    for t, _k, text in events:
        for j in range(len(steps)):
            if bounds[j] <= t < bounds[j + 1]:
                if steps[j].moved and not steps[j].edge:
                    out[j] = text  # the last announcement in the window wins
                break
    return out


# --------------------------------------------------------------------------- #
# Result: record, diff, compact rendering, storage
# --------------------------------------------------------------------------- #
def _ms(seconds: float) -> int:
    return int(round(seconds * 1000))


def _pct(values: List[int], p: float) -> Optional[int]:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(round(p * (len(v) - 1))))]


def _walk_id() -> str:
    return "w" + "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(6))


def walks_dir() -> str:
    return os.path.join(device.store_root(), "walks")


def _step_record(s: Step, ref: str, speak: str, utt: str, idx_rect: Optional[Rect]) -> Dict[str, Any]:
    n = s.node
    rec: Dict[str, Any] = {"i": s.i, "key": s.key, "ref": ref, "via": s.via, "moved": s.moved}
    if s.edge:
        rec["edge"] = True
    if n is not None:
        rec.update(label=n.label, cls=n.simple_cls, bounds=list(n.bounds), window=n.window,
                   speak=speak, utt=utt, flags=sorted(n.flags & {"focused", "clickable", "focusable",
                                                                 "visible_to_user", "scrollable"}))
        if n.pane_title:
            rec["pane_title"] = n.pane_title
        if idx_rect is not None:
            rec["window_rect"] = list(idx_rect)
        cov = _covered_by(n)
        if cov is not None:
            rec["covered_by"] = cov
    if s.ms is not None:
        rec["ms"] = s.ms
    if s.scrolled:
        rec["scrolled"] = s.scrolled
    rec.update(s.extra)
    return rec


def _inside(r: Rect, o: Rect) -> bool:
    x, y, w, h = r
    ox, oy, ow, oh = o
    return w > 0 and h > 0 and ox <= x and oy <= y and x + w <= ox + ow and y + h <= oy + oh


def _covered_by(n: Node) -> Optional[Dict[str, Any]]:
    """What draws over the node in its own window (:mod:`.occlusion`: a scrim, a sheet, an
    open drawer, or a bar such as the action-mode bar over the toolbar), as the step record
    keeps it: ``{"overlay", "kind", "cls", "pane_title", "area", "rect"}``; None when nothing
    does. Only what draws counts: an empty full-screen FrameLayout drawn last (AntennaPod's
    loading frame, its only child GONE) covers nothing. The walk's dump has no View
    properties, so a View draws its whole box when it takes touches over most of the window
    or is a surface (most of the window, with content of its own), else only where its
    children draw."""
    from .occlusion import NodeAccess, Occlusion

    c = Occlusion(NodeAccess()).covered_by(n)
    if c is None:
        return None
    return {"overlay": c.key, "kind": c.kind, "cls": c.cls, "pane_title": c.pane_title,
            "area": round(c.area, 2), "rect": list(c.rect)}


def _finish(drv: Driver, steps: List[Step], model: Model, *, ended: str, cycle: List[str],
            initial: Optional[Dict[str, Any]] = None, start_resp: Any = None,
            start_idx: Optional[DumpIndex] = None,
            edge_info: Optional[Dict[str, Any]], start: str, direction: str, until: str,
            expect: Optional[Sequence[str]], tts: Dict[int, str], t_start: float,
            max_lines: int, max_bytes: int, save: bool, full: bool = False) -> Dict[str, Any]:
    legacy = bool(getattr(drv.reader, "legacy", False))
    refs: Dict[str, str] = {}

    def ref_of(key: Optional[str]) -> str:
        if key is None:
            return "-"
        if not legacy:
            return key
        if key not in refs:
            refs[key] = f"s{len(refs) + 1}"
        return refs[key]

    records = _build_records(steps, model, tts, ref_of)
    predicted = [{"key": p.key, "ref": ref_of(p.key), "label": p.label, "speak": p.speak,
                  "bounds": list(p.bounds), "window": p.window, "cls": p.cls,
                  **({"added": p.added} if p.added else {})} for p in model.stops]
    density = _density(drv.serial)
    walk: Dict[str, Any] = {
        "serial": drv.serial, "package": drv.package, "start": start, "direction": direction,
        "until": until, "ended": ended, "steps": records, "predicted": predicted,
        "model": model.source, "cycle": [ref_of(k) for k in cycle], "edge": edge_info,
        "density": density, "legacy_ids": legacy,
    }
    if edge_info and edge_info.get("container"):
        edge_info["container_ref"] = ref_of(edge_info["container"])
    if ended == "wrap" and start_idx is not None:
        last_idx = next((s.index for s in reversed(steps) if s.index is not None), start_idx)
        walk["orphans"] = orphan_text(last_idx, records, legacy)
    if start_resp is not None:
        # What the model knows lies ahead (auto-scrolled content, a WebView on an off-screen
        # page): diff names a walk stuck at such a WebView a tb.trap.
        known = model_hints(start_resp)
        walk["hints"] = known["hints"]
        walk["web_traps"] = known["web_traps"]
    analysis = diff.analyze(walk, expect=expect)
    walk["findings"] = analysis["findings"]
    walk["vs_model"] = analysis["vs_model"]
    if initial is not None and initial.get("key"):
        # TalkBack just started and put focus here itself: the model's initial-focus rule.
        walk["initial"] = dict(initial, model=initial["key"])
        walk["vs_model"]["initial"] = ("agree" if initial["key"] == initial["actual"] else
                                       f"model {ref_of(initial['key'])}, actual {ref_of(initial['actual'])}")
    if analysis.get("expect") is not None:
        walk["expect"] = analysis["expect"]
    moves = [r["ms"] for r in records if r.get("ms") is not None]
    reader = drv.reader
    walls = [r["wall_ms"] for r in records if r.get("wall_ms") is not None]
    walk["ms"] = {"p50": _pct(moves, 0.5), "p95": _pct(moves, 0.95),
                  "step_p50": _pct(walls, 0.5), "total": _ms(time.monotonic() - t_start),
                  "read_p50": _pct([int(x) for x in getattr(reader, "read_ms", [])], 0.5),
                  "reads": getattr(reader, "reads", None), "dumps": getattr(reader, "dumps", None)}
    keys = drv.inj
    walk["talkback"] = f"{drv.enabled.get('version', '?')} {keys.describe() if keys else '?'}"
    walk["reader"] = getattr(reader, "kind", "?")
    walk["seek_presses"] = drv.seek_presses
    walk["start_via"] = drv.start_via
    walk["remodels"] = model.remodels
    walk["recapture"] = "model only (no capture store on this branch)" if model.remodels else None
    n_logcat = sum(1 for r in records if r.get("utt") == "logcat")
    walk["utterance"] = (f"logcat {n_logcat}/{sum(1 for r in records if r.get('speak') is not None)}"
                         if n_logcat else "model")
    walk["notes"] = list(drv.notes)
    if drv.log is not None and not drv.log.verbose and drv.utterance == "logcat":
        walk["notes"].append("logcat: no TalkBack verbose lines; set TalkBack Settings > Advanced "
                             "> Developer settings > Log output level: Verbose")
    walk["restore"] = _restore_state(drv)
    walk["refs"] = {v: k for k, v in refs.items()}
    wid = _walk_id()
    walk["walk"] = wid
    if save:
        path = os.path.join(walks_dir(), f"{wid}.json")
        try:
            if start_resp is not None:
                # The dump the walk started from, for static checks and offline replay.
                os.makedirs(walks_dir(), exist_ok=True)
                dump_path = os.path.join(walks_dir(), f"{wid}.a11y.pb")
                with open(dump_path, "wb") as f:
                    f.write(start_resp.SerializeToString())
                walk["dump"] = dump_path
            device._write_json_atomic(path, walk)
            walk["saved"] = path
        except OSError as exc:
            walk["notes"].append(f"could not save the walk: {exc}")
    if full:
        return walk
    return compact(walk, max_lines=max_lines, max_bytes=max_bytes)


def _build_records(steps: List[Step], model: Model, tts: Dict[int, str],
                   ref_of: Callable[[Optional[str]], str]) -> List[Dict[str, Any]]:
    """The saved form of each step (what :mod:`.diff` classifies)."""
    win_rects: Dict[int, Rect] = {}
    records = []
    for s in steps:
        speak, utt = "", "model"
        if s.i in tts:
            speak, utt = tts[s.i], "logcat"
        elif s.extra.get("speak") is not None:
            speak = s.extra.pop("speak")
        elif s.key is not None:
            p = model.match(s.key, s.node.sig, s.node.bounds, ctx=s.node.ctx,
                            item_root=s.node.item_root) if s.node else model.get(s.key)
            speak = (p.speak if p is not None else "") or (s.node.speech() if s.node else "")
        rect = None
        if s.node is not None:
            if s.node.window not in win_rects:
                idx = s.index if s.index is not None else None
                wr = idx.window_rect(s.node.window) if idx is not None else None
                if wr is None:
                    root = s.node
                    for a in s.node.ancestors():
                        root = a
                    wr = root.bounds
                win_rects[s.node.window] = wr
            rect = win_rects[s.node.window]
        rec = _step_record(s, ref_of(s.key), speak, utt, rect)
        if s.node is not None and s.index is not None:
            bar = next((o for o in s.index.obscured(s.node.window)
                        if _inside(s.node.bounds, o)), None)
            if bar is not None:
                rec["under_system_bar"] = list(bar)
            c = s.index.scroll_container(s.node)
            if c is not None:
                # The scrollable around the stop, and where it can still scroll.
                rec["container"] = ref_of(c.key)
                rec["container_rect"] = list(c.bounds)
                rec["container_cls"] = c.simple_cls
                rec["container_can"] = sorted({_SCROLL_ACTIONS[a] for a in c.actions if a in _SCROLL_ACTIONS})
        if s.node is not None:
            rec["sig"] = s.node.sig
            if not (s.node.text or s.node.cd):
                parts = _spoken_parts(s.node)
                if len(parts) >= 2:
                    rec["parts"] = parts  # the texts a merged row joins, with where they are
            # Who holds whom: a double stop is a container and a node inside it.
            rec["ancestors"] = [ref_of(a.key) for a in s.node.ancestors()][:16]
            if s.node.parent is not None:
                rec["parent_rect"] = list(s.node.parent.bounds)
            known = model.match(s.key, s.node.sig, s.node.bounds, ctx=s.node.ctx,
                                item_root=s.node.item_root)
            if known is not None and known.key != s.key:
                rec["pkey"] = known.key  # the model's key for this node (a re-minted id)
        if rec.get("covered_by"):
            rec["covered_by"]["ref"] = ref_of(rec["covered_by"]["overlay"])
        if s.node is not None and s.node.window in model.covered_windows:
            rec["window_covered_by"] = model.covered_windows[s.node.window]
        records.append(rec)
    return records


def _spoken_parts(n: Node, limit: int = 8) -> List[Dict[str, Any]]:
    """The texts inside a stop that are not stops of their own, in child order:
    what TalkBack joins into the stop's announcement."""
    out: List[Dict[str, Any]] = []
    stack = list(reversed(n.children))
    while stack and len(out) < limit:
        c = stack.pop()
        if c.actionable() or {"focusable", "screen_reader_focusable"} & c.flags:
            continue
        words = c.text or c.cd
        if words:
            if "visible_to_user" in c.flags:
                out.append({"text": words[:60], "rect": list(c.bounds)})
            continue
        stack.extend(reversed(c.children))
    return out


def orphan_text(idx: DumpIndex, records: List[Dict[str, Any]], legacy: bool = False,
                limit: int = 20) -> List[Dict[str, Any]]:
    """Text on screen that no stop of a full lap spoke: in a window the lap went
    through (not one under a modal dialog), visible, inside its window and not
    under a system bar, and none of its words in what the walk read."""
    spoken: set = set()
    for r in records:
        spoken |= diff._tokens(f"{r.get('speak') or ''} {r.get('label') or ''}")
    spoken = diff.with_abbreviations(spoken)
    windows = {r.get("window") for r in records}
    out: List[Dict[str, Any]] = []
    for n in idx.order:
        words = n.cd or n.text  # a contentDescription is said in place of the text
        if not words or "visible_to_user" not in n.flags or n.window not in windows:
            continue
        x, y, w, h = n.bounds
        win = idx.window_rect(n.window)
        if w <= 0 or h <= 0 or (win is not None and not _intersects(n.bounds, win)):
            continue
        if any(_inside(n.bounds, o) for o in idx.obscured(n.window)):
            continue
        toks = diff._tokens(words)
        if toks and not (toks & spoken):
            cov = _covered_by(n) if isinstance(n, Node) else None
            if cov is not None and cov.get("kind") != "bar":
                continue  # behind a drawer's scrim, a sheet or a dialog: nobody sees it
            out.append({"key": n.key if not legacy else None, "text": words[:60], "bounds": list(n.bounds)})
            if len(out) >= limit:
                break
    return out


def _intersects(a: Rect, b: Rect) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def static_walk(resp: Any, *, direction: str = "next", until: str = "wrap",
                expect: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """The model's own walk over one dump, classified like a live one (basis
    ``model``): what :mod:`.diff` would report if TalkBack did exactly what
    talkback.order predicts. No device needed; ``resp`` is a DumpA11yResponse.

    The model does not scroll: the walk ends (``ended`` "autoscroll") where
    TalkBack would auto-scroll a list, so what it would scroll in is not judged;
    ``hints`` says what lies there (talkback.order's ``autoscroll_ahead``). A
    pager is not auto-scrolled, so leaving one is still reported. A WebView on
    an off-screen page that the walk reaches is a tb.ghost_stop (TalkBack reads
    what nobody sees), or a tb.trap when TalkBack cannot focus it (``ended``
    "trap", see talkback.order.Navigator.traps)."""
    from .. import a11y
    from .order import simulate
    from .tree import build
    idx = DumpIndex(resp)
    model = Model()
    model.build(resp, bool(idx.legacy))
    order = simulate(build(a11y.a11y_to_dict(resp)), start=None, direction=direction, until=until,
                     keyboard=True)
    ended = order.ended
    steps: List[Step] = []
    for st in order.steps:
        key = st.get("key")
        node = idx.nodes.get(key) if key else None
        if st.get("stuck"):
            break
        if st.get("autoscroll"):
            ended = "autoscroll"
            break
        if st.get("edge"):
            steps.append(Step(len(steps), key, moved=False, edge=True, via="edge", node=node, index=idx))
            continue
        via = "wrap" if st.get("via") == "wrap" else "next"
        extra: Dict[str, Any] = {"speak": st.get("speak") or ""}
        if st.get("show_on_screen"):
            extra["show_on_screen"] = True  # TalkBack scrolls it fully into view first
        steps.append(Step(len(steps), key, via="start" if not steps else via, node=node, index=idx,
                          extra=extra))
    ref_of: Callable[[Optional[str]], str] = lambda k: k if k is not None else "-"  # noqa: E731
    records = _build_records(steps, model, {}, ref_of)
    walk: Dict[str, Any] = {
        "steps": records, "predicted": [], "ended": ended, "direction": direction,
        "until": until, "model": "talkback.order", "cycle": [], "edge": None,
        "density": 420, "legacy_ids": bool(idx.legacy), "basis": "model",
    }
    edge_at = next((i for i, s in enumerate(steps) if s.edge), None)
    last = next((s for s in reversed(steps[:edge_at]) if s.node is not None and s.moved), None) \
        if edge_at is not None else None
    if last is not None:
        walk["edge"] = _edge_info(idx, last.node, direction)
    if ended == "wrap":
        walk["orphans"] = orphan_text(idx, records, bool(idx.legacy))
    analysis = diff.analyze(walk, expect=expect)
    findings = [dict(f, basis="model" if f.get("basis") == "walk" else f.get("basis"))
                for f in analysis["findings"] if f["code"] != "model.mismatch"]
    hidden = [d for d in order.diagnostics if d.get("kind") == "web_hidden_page"]
    walk["findings"] = [diff.web_trap_finding(d, basis="model") for d in hidden] + findings
    walk["hints"] = [h["message"] for h in order.hints]
    return walk


def model_hints(resp: Any) -> Dict[str, Any]:
    """What the model knows about a screen that a walk over it runs into: ``hints`` (the
    autoscroll_ahead and web_hidden_page messages of a forward lap) and ``web_traps`` (the
    WebViews on an off-screen page that TalkBack cannot focus,
    :meth:`.order.Navigator.hidden_web_pages` with ``trap``). Empty when the model cannot read
    the dump."""
    try:
        from .. import a11y
        from .order import Navigator, simulate
        from .tree import build
        tb = build(a11y.a11y_to_dict(resp))
        order = simulate(tb, start=None, until="wrap", keyboard=True)
        return {"hints": [h["message"] for h in order.hints],
                "web_traps": [d for d in Navigator(tb).hidden_web_pages() if d["trap"]]}
    except Exception:  # noqa: BLE001 - the walk reports without them
        return {"hints": [], "web_traps": []}


def _restore_state(drv: Driver) -> str:
    if drv.restore_error:
        return f"FAILED: {drv.restore_error} (run talkback restore)"
    if drv.restored is not None:
        return "restored"
    if drv.turned_on and drv.leave_on:
        return "left on (talkback restore to undo)"
    return "unchanged (TalkBack was already on)"


def _density(serial: str) -> int:
    try:
        return adb.display_density(serial)
    except Exception:  # noqa: BLE001
        return 420


def _line(r: Dict[str, Any], speak_len: int) -> str:
    if r.get("via") == "start" and r.get("key") is None:
        return f"{r['i']}. (no accessibility focus)"
    if r.get("edge"):
        return f"{r['i']}. — edge"
    if r.get("via") == "left_app":
        return f"{r['i']}. — left the app (top: {r.get('top') or '?'})"
    if r.get("via") == "lost":
        return f"{r['i']}. — focus lost (no node holds it)" + (
            f" after scrolling {r['scrolled']}" if r.get("scrolled") else "")
    sp = (r.get("speak") or "").replace("\n", " ")
    if len(sp) > speak_len:
        sp = sp[:speak_len - 1] + "…"
    out = f"{r['i']}. {r['ref']} {r.get('cls') or '?'} \"{sp}\""
    if r.get("via") not in ("next", "start", None):
        out += f" via={r['via']}"
    for tag in r.get("tags") or []:
        out += f" !{tag}"
    return out


def compact(walk: Dict[str, Any], max_lines: int = 60, max_bytes: int = 5000) -> Dict[str, Any]:
    """The tool result: header + one line per step + findings, within ``max_bytes``."""
    tags: Dict[int, List[str]] = {}
    for f in walk.get("findings") or []:
        for i in f.get("steps") or []:
            tags.setdefault(i, []).append(f["code"].split(".", 1)[-1])
    for r in walk["steps"]:
        r["tags"] = sorted(set(tags.get(r["i"], [])))
    head = {k: walk.get(k) for k in ("walk", "serial", "package", "talkback", "reader", "start",
                                      "direction", "until", "ended", "ms", "utterance", "vs_model",
                                      "restore")}
    head["steps"] = sum(1 for r in walk["steps"] if r["i"] > 0)
    if walk.get("expect") is not None:
        head["expect"] = walk["expect"]
    if walk.get("cycle"):
        head["cycle"] = walk["cycle"]
    if walk.get("notes"):
        head["notes"] = walk["notes"]
    if walk.get("hints"):
        head["hints"] = [h if len(h) <= 240 else h[:239] + "…" for h in walk["hints"][:3]]
    findings = [{k: f[k] for k in ("code", "sev", "refs", "basis", "msg", "fix") if f.get(k) is not None}
                for f in walk.get("findings") or []]
    hints = next_hints(walk)
    speak_len, n_findings = 48, 8
    lines_all = walk["steps"]
    while True:
        lines = [_line(r, speak_len) for r in lines_all]
        if len(lines) > max_lines:
            keep = max_lines - 1
            half = keep // 2
            lines = lines[:half] + [f"… {len(lines) - keep} lines omitted (full walk in 'saved') …"] \
                + lines[len(lines) - (keep - half):]
        out = dict(head, lines=lines, findings=findings[:n_findings])
        if len(findings) > n_findings:
            out["findings_omitted"] = len(findings) - n_findings
        if hints:
            out["next"] = hints
        if walk.get("saved"):
            out["saved"] = walk["saved"]
        size = len(json.dumps(out, ensure_ascii=False).encode())
        if size <= max_bytes:
            return out
        if speak_len > 24:
            speak_len -= 8
        elif n_findings > 3:
            n_findings -= 1
        elif hints:
            hints = hints[:-1]
        elif max_lines > 12:
            max_lines = max(12, max_lines - 8)
        else:
            return out


def next_hints(walk: Dict[str, Any]) -> List[str]:
    hints: List[str] = []
    codes = {f["code"] for f in walk.get("findings") or []}
    legacy = walk.get("legacy_ids")
    for f in walk.get("findings") or []:
        ref = (f.get("keys") or [None])[0]
        if ref and not legacy and f["code"] != "model.mismatch":
            hints.append(f"inspect_node(node_key='{ref}') for {f['code']}")
            break
    if walk.get("ended") in ("wrap", "edge") and walk.get("direction") == "next" and len(hints) < 3:
        hints.append("tb_walk(direction='prev') to check the reverse order matches")
    if "tb.edge_stuck" in codes or walk.get("ended") == "stuck":
        hints.append("tb_walk(until='steps', max_steps=5) from the last stop to see if TalkBack "
                     "recovers")
    if walk.get("restore", "").startswith("left on"):
        hints.append("talkback(action='restore') when done")
    return hints[:3]


def load_walk(walk_id: str) -> Dict[str, Any]:
    path = os.path.join(walks_dir(), f"{walk_id}.json")
    with open(path) as f:
        return json.load(f)
