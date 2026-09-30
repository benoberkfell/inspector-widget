"""Drive the real TalkBack through an app and record where accessibility focus lands.

A walk turns TalkBack on (snapshotting the settings first, see
:mod:`.device`), presses TalkBack's "next" (or "previous") shortcut through a
uinput keyboard (:mod:`.inject`), and after every press reads which node holds
accessibility focus. The walk (A, actual) is then compared with the model's
predicted order (P) and a visual reading order (V) by :mod:`.diff`.

Focus reading: :class:`DumpFocusReader` polls ``Session.dump_a11y`` (the
agent's in-process query reports ``accessibility_focused`` exactly as TalkBack
set it; 16-18ms per read on an emulator) every 10ms until the focused node
changes and then stays put (key and bounds) for a quiet period. The agent's
long-poll A11yFocus command (design T2) will replace it behind the same
:class:`FocusReader` interface; :func:`make_reader` picks it when the Session
has ``a11y_focus``.

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
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Protocol, Sequence, Tuple

from .. import adb
from . import device, diff, inject

HOST_VIEW_ID = -1  # AccessibilityNodeProvider.HOST_VIEW_ID

DEFAULT_MAX_STEPS = 60
MAX_STEPS_CAP = 300
STEP_TIMEOUT_MS = 1500
SETTLE_MS = 120
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
_SCROLL_ACTIONS = {0x1000: "forward", 0x2000: "backward", 0x0102003F: "up", 0x01020041: "down",
                   0x01020040: "left", 0x01020042: "right", 0x01020049: "page_down",
                   0x01020048: "page_up", 0x0102004A: "page_left", 0x0102004B: "page_right"}
_ROLE_WORDS = {
    "Button": "Button", "ImageButton": "Button", "CheckBox": "Checkbox", "Switch": "Switch",
    "ToggleButton": "Toggle button", "RadioButton": "Radio button", "EditText": "Edit box",
    "ImageView": "Image", "SeekBar": "Slider", "Spinner": "Drop down list",
}


class Node:
    __slots__ = ("key", "window", "host", "virtual", "cls", "label", "text", "cd", "bounds",
                 "flags", "actions", "parent", "children", "drawing_order", "pane_title")

    def __init__(self) -> None:
        self.parent: Optional["Node"] = None
        self.children: List["Node"] = []

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


def same_node(key_a: Optional[str], sig_a: str, box_a: Rect,
              key_b: Optional[str], sig_b: str, box_b: Rect, min_iou: float = 0.8) -> bool:
    """The same node: the same key, or (a re-minted Compose id) the same
    signature where the boxes overlap."""
    if key_a is not None and key_a == key_b:
        return True
    labelled = bool(sig_a) and not sig_a.endswith("|")  # unlabelled nodes all look alike
    return labelled and sig_a == sig_b and iou(box_a, box_b) >= min_iou


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
        for n in self.order:
            if n.window == window and n.parent is None:
                return n.bounds
        return None

    def scroll_container(self, n: Node) -> Optional[Node]:
        for a in n.ancestors():
            if "scrollable" in a.flags or a.actions & set(_SCROLL_ACTIONS):
                return a
        return None


def detect_scroll(prev: Optional[DumpIndex], cur: DumpIndex) -> Optional[Node]:
    """The scrollable container whose content moved between two dumps, if any."""
    if prev is None:
        return None
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
@dataclass
class Snapshot:
    index: DumpIndex
    t: float  # time.monotonic() when the read returned
    resp: Any = None  # the DumpA11yResponse (for the model)

    @property
    def focus(self) -> Optional[Node]:
        return self.index.focus

    @property
    def key(self) -> Optional[str]:
        f = self.index.focus
        return f.key if f is not None else None


@dataclass
class WaitResult:
    snap: Snapshot
    moved: bool
    first_ms: Optional[int]
    settled_ms: Optional[int]
    polls: int
    aborted: bool = False


class FocusReader(Protocol):
    """Where a walk gets TalkBack's focus from (the dump poller, or A11yFocus)."""

    kind: str

    def snapshot(self) -> Snapshot: ...

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

    def snapshot(self) -> Snapshot:
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
        """Poll until the focused key differs from ``prev_key`` and then keeps the
        same key and bounds for ``quiet_s``. ``moved`` False on timeout (or when
        ``abort`` says the press was an edge)."""
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
            if first is None and k != prev_key:
                first = now
            if first is not None:
                f = snap.focus
                cur_sig = (k, f.bounds if f is not None else None)
                if cur_sig != sig:
                    sig, stable_since = cur_sig, now
                elif now - stable_since >= quiet_s:
                    return WaitResult(snap, k != prev_key, _ms(first - t0), _ms(stable_since - t0), polls)
            elif abort is not None and abort():
                return WaitResult(snap, False, None, None, polls, aborted=True)
            if now - t0 >= timeout_s:
                moved = first is not None and k != prev_key
                return WaitResult(snap, moved, _ms(first - t0) if first else None, None, polls)
            time.sleep(self.poll_s)


def make_reader(session: Any) -> FocusReader:
    """The best focus reader the agent supports (A11yFocus long-poll once T2 lands)."""
    return DumpFocusReader(session)


# --------------------------------------------------------------------------- #
# TalkBack's verbose logcat (optional utterance / edge / auto-scroll source)
# --------------------------------------------------------------------------- #
_RE_TTS = re.compile(r"TYPE_VIEW_ACCESSIBILITY_FOCUSED:\s+ttsOutput=\s?(.*?)(?:\s{2,}queueMode|$)")
_RE_EDGE = re.compile(r"Reach edge")
_RE_SCROLL = re.compile(r"AutoScrollActor|ScrollAction=ACTION_SCROLL|ACTION_SHOW_ON_SCREEN")


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
        res = tborder.reading_order(d)  # the whole dump: windows, modality, importance
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
    stops: List[PStop] = []
    for n, sp in zip(nodes, speaks, strict=False):
        kids = [(c.get("text") or "", c.get("content_description") or "") for c in n.get("children") or []]
        label = _label(n.get("text") or "", n.get("content_description") or "", kids)
        cls = n.get("class_name") or ""
        b = (n.get("bounds") or {}).get("layout") or {}
        win = win_of.get(id(n), 0)
        key = node_key(win, int(n.get("host_view_id") or 0), int(n.get("virtual_id", HOST_VIEW_ID)),
                       cls, label, legacy, compose_hosts)
        stops.append(PStop(key, label, sp or _dict_speech(n), (b.get("x", 0), b.get("y", 0),
                                                              b.get("w", 0), b.get("h", 0)),
                           win, cls.rsplit(".", 1)[-1]))
    covered = {w["root_view_id"]: w["covered_by"] for w in windows if w.get("covered_by") is not None}
    return stops, source, {"covered_windows": covered}


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
        right after the stop the new order puts before it."""
        new, _source, meta = predict(resp, legacy)
        self.remodels += 1
        self.covered_windows.update(meta["covered_windows"])
        keys = [s.key for s in self.stops]
        prev: Optional[str] = None
        for s in new:
            known = self.match(s.key, s.sig, s.bounds)
            if known is not None:
                if known.key != s.key:
                    self.aliases[s.key] = known.key
                prev = known.key
                continue
            at = keys.index(prev) + 1 if prev in keys else len(keys)
            keys.insert(at, s.key)
            self.stops.insert(at, s)
            prev = s.key

    def match(self, key: Optional[str], sig: str, box: Rect) -> Optional[PStop]:
        """The model's stop for a node: by key (or alias), else by signature + overlap."""
        key = self.aliases.get(key, key) if key else key
        for s in self.stops:
            if s.key == key:
                return s
        for s in self.stops:
            if same_node(None, sig, box, None, s.sig, s.bounds):
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
             max_bytes: int = 5000, timeout_s: float = 300.0, save: bool = True) -> Dict[str, Any]:
    """Walk TalkBack through the app on ``session`` and return the compact result.

    ``start``: ``current`` (where focus is now), ``first`` (TalkBack's "first"
    shortcut) or a node key / part of a label (pressed "next" until focus gets
    there). ``until``: ``wrap`` (one full lap: stop when focus comes back to the
    first stop after an edge), ``edge``, ``loop`` (the first repeated move) or
    ``steps`` (exactly ``max_steps`` presses). A walk also ends on ``stuck`` (two
    presses in a row that move nothing), ``loop`` (a move repeats with no edge in
    between: TalkBack would never reach the end), ``left_app`` and ``timeout``.

    The full record (every step, the predicted order, findings) is saved under
    ``<store>/walks/<walk id>.json``; the returned dict is at most ``max_bytes``
    of JSON with one line per step.
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
    with drv:
        cur = drv.reader.snapshot()
        legacy = bool(cur.index.legacy)
        model.build(cur.resp, legacy)
        if cur.focus is None and not drv.foreground_ok():
            raise WalkError("app_left_foreground", f"{drv.package} is not in the foreground",
                            hint=f"Open {drv.package} on the device, then retry.")
        cur = _seek_start(drv, cur, start, direction, max_steps)
        steps.append(Step(0, cur.key, via="start", node=cur.focus, t=cur.t))
        prev_idx = cur.index
        transitions: Dict[Tuple[Optional[str], Optional[str]], int] = {}
        last_edge_at = -1
        first: Optional[Node] = cur.focus  # the lap is complete when focus is back here
        no_moves = 0
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
                elif cur.key is not None:
                    steps.append(Step(len(steps), pre.key, via="stolen", node=pre.focus, t=pre.t))
                cur, prev_idx = pre, pre.index
                first = first or cur.focus
            t_sent, _send_ms = drv.press(direction)
            w = drv.wait(cur.key, t_sent)
            new = w.snap
            if w.moved and direction == "next":
                drv.inj.mark_proven()  # type: ignore[union-attr]
            if new.key is None and cur.key is not None:
                ended = "left_app"
                steps.append(Step(len(steps), None, via="left_app", t=new.t,
                                  extra={"top": device.top_activity(drv.serial)}))
                break
            if not w.moved:
                no_moves += 1
                steps.append(Step(len(steps), cur.key, moved=False, edge=True, via="edge",
                                  node=cur.focus, t=t_sent))
                if no_moves >= 2 and not drv.inj.proven and drv.try_other_keymap():  # type: ignore[union-attr]
                    del steps[-2:]  # those presses were not edges: the keymap was wrong
                    no_moves = 0
                    continue
                if edge_info is None and cur.focus is not None:
                    edge_info = _edge_info(cur.index, cur.focus, direction)
                last_edge_at = len(steps) - 1
                if until == "edge":
                    ended = "edge"
                    break
                if no_moves >= 2:
                    ended = "stuck"
                    break
                continue
            via = "wrap" if no_moves else "next"
            if via == "next" and new.focus is not None and cur.focus is not None \
                    and new.focus.window != cur.focus.window:
                via = "window"
            scrolled = detect_scroll(prev_idx, new.index)
            if scrolled is None and new.focus is not None and drv.log is not None \
                    and drv.log.since(t_sent, "scroll"):
                scrolled = new.index.scroll_container(new.focus)
            if scrolled is not None and via == "next":
                via = "autoscroll"
            no_moves = 0
            st = Step(len(steps), new.key, via=via, ms=w.first_ms, node=new.focus, t=t_sent,
                      scrolled=scrolled.key if scrolled is not None else None)
            steps.append(st)
            if recapture == "on_unknown" and new.key not in model.keys():
                model.remodel(new.resp, legacy)
                st.extra["remodel"] = True
            tr = (cur.key, new.key)
            seen_at = transitions.get(tr)
            transitions[tr] = len(steps) - 1
            cur, prev_idx = new, new.index
            first = first or cur.focus
            if until == "wrap" and last_edge_at >= 0 and first is not None and new.focus is not None \
                    and same_node(first.key, first.sig, first.bounds,
                                  new.key, new.focus.sig, new.focus.bounds):
                ended = "wrap"
                break
            if seen_at is not None and until != "steps":
                if last_edge_at > seen_at:
                    ended = "wrap"
                else:
                    ended = "loop"
                    cycle = [s.key for s in steps[seen_at - 1:len(steps) - 2] if s.key]
                break
        if drv.log is not None and drv.log.verbose:
            time.sleep(0.3)  # the last announcement lands ~100ms after its press
            tts = _attribute_tts(steps, drv.log)
    return _finish(drv, steps, model, ended=ended, cycle=cycle, edge_info=edge_info, start=start,
                   direction=direction, until=until, expect=expect, tts=tts, t_start=t_start,
                   max_lines=max_lines, max_bytes=max_bytes, save=save)


def _seek_start(drv: Driver, cur: Snapshot, start: str, direction: str, max_presses: int) -> Snapshot:
    """Put focus where the walk starts. Anything but ``current`` + ``next`` first
    proves the keymap with "next" (see :class:`.inject.KeyGuard`)."""
    if start == "current" and direction == "next":
        return cur
    before = cur
    w = drv.prove(cur.key)
    snap = w.snap
    if start == "current":
        if before.key is not None and snap.key != before.key:
            t, _ = drv.press("prev")  # back to where the user was
            snap = drv.wait(snap.key, t).snap
            drv.seek_presses += 1
        return snap
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


def _edge_info(idx: DumpIndex, n: Node, direction: str) -> Optional[Dict[str, Any]]:
    """At an edge: the scrollable container around the last stop, and whether it
    still advertises scrolling in the walk's direction (TalkBack should have)."""
    c = idx.scroll_container(n)
    if c is None:
        return None
    fwd = {"forward", "down", "right", "page_down", "page_right"}
    back = {"backward", "up", "left", "page_up", "page_left"}
    want = fwd if direction == "next" else back
    can = sorted(_SCROLL_ACTIONS[a] for a in c.actions if a in _SCROLL_ACTIONS and _SCROLL_ACTIONS[a] in want)
    return {"container": c.key, "container_cls": c.simple_cls, "can_scroll": can}


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


def _covered_by(n: Node) -> Optional[Dict[str, Any]]:
    """A later-drawn sibling subtree (of the node or an ancestor) that covers the
    node's centre and a large part of the window: a same-window overlay."""
    x, y, w, h = n.bounds
    cx, cy = x + w / 2, y + h / 2
    root = n
    for a in n.ancestors():
        root = a
    rx, ry, rw, rh = root.bounds
    win_area = max(1, rw * rh)
    child = n
    for parent in n.ancestors():
        sibs = parent.children
        try:
            pos = next(i for i, c in enumerate(sibs) if c is child)
        except StopIteration:
            pos = len(sibs)
        later = sibs[pos + 1:]
        if any(c.drawing_order for c in sibs):
            later = [c for c in sibs if c is not child and c.drawing_order > child.drawing_order]
        for o in later:
            ox, oy, ow, oh = o.bounds
            if ow * oh >= 0.4 * win_area and ox <= cx < ox + ow and oy <= cy < oy + oh \
                    and "visible_to_user" in o.flags:
                return {"overlay": o.key, "cls": o.simple_cls, "pane_title": o.pane_title or None,
                        "area": round(ow * oh / win_area, 2), "rect": list(o.bounds)}
        child = parent
    return None


def _finish(drv: Driver, steps: List[Step], model: Model, *, ended: str, cycle: List[str],
            edge_info: Optional[Dict[str, Any]], start: str, direction: str, until: str,
            expect: Optional[Sequence[str]], tts: Dict[int, str], t_start: float,
            max_lines: int, max_bytes: int, save: bool) -> Dict[str, Any]:
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

    win_rects: Dict[int, Rect] = {}
    records = []
    for s in steps:
        speak, utt = "", "model"
        if s.i in tts:
            speak, utt = tts[s.i], "logcat"
        elif s.key is not None:
            p = model.match(s.key, s.node.sig, s.node.bounds) if s.node else model.get(s.key)
            speak = (p.speak if p is not None else "") or (s.node.speech() if s.node else "")
        rect = None
        if s.node is not None:
            if s.node.window not in win_rects:
                root = s.node
                for a in s.node.ancestors():
                    root = a
                win_rects[s.node.window] = root.bounds
            rect = win_rects[s.node.window]
        rec = _step_record(s, ref_of(s.key), speak, utt, rect)
        if s.node is not None:
            rec["sig"] = s.node.sig
            known = model.match(s.key, s.node.sig, s.node.bounds)
            if known is not None and known.key != s.key:
                rec["pkey"] = known.key  # the model's key for this node (a re-minted id)
        if rec.get("covered_by"):
            rec["covered_by"]["ref"] = ref_of(rec["covered_by"]["overlay"])
        if s.node is not None and s.node.window in model.covered_windows:
            rec["window_covered_by"] = model.covered_windows[s.node.window]
        records.append(rec)
    predicted = [{"key": p.key, "ref": ref_of(p.key), "label": p.label, "speak": p.speak,
                  "bounds": list(p.bounds), "window": p.window, "cls": p.cls} for p in model.stops]
    density = _density(drv.serial)
    walk: Dict[str, Any] = {
        "serial": drv.serial, "package": drv.package, "start": start, "direction": direction,
        "until": until, "ended": ended, "steps": records, "predicted": predicted,
        "model": model.source, "cycle": [ref_of(k) for k in cycle], "edge": edge_info,
        "density": density, "legacy_ids": legacy,
    }
    if edge_info and edge_info.get("container"):
        edge_info["container_ref"] = ref_of(edge_info["container"])
    analysis = diff.analyze(walk, expect=expect)
    walk["findings"] = analysis["findings"]
    walk["vs_model"] = analysis["vs_model"]
    if analysis.get("expect") is not None:
        walk["expect"] = analysis["expect"]
    moves = [r["ms"] for r in records if r.get("ms") is not None]
    reader = drv.reader
    walk["ms"] = {"p50": _pct(moves, 0.5), "p95": _pct(moves, 0.95),
                  "total": _ms(time.monotonic() - t_start),
                  "read_p50": _pct([int(x) for x in getattr(reader, "read_ms", [])], 0.5)}
    keys = drv.inj
    walk["talkback"] = f"{drv.enabled.get('version', '?')} {keys.describe() if keys else '?'}"
    walk["reader"] = getattr(reader, "kind", "?")
    walk["seek_presses"] = drv.seek_presses
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
            device._write_json_atomic(path, walk)
            walk["saved"] = path
        except OSError as exc:
            walk["notes"].append(f"could not save the walk: {exc}")
    return compact(walk, max_lines=max_lines, max_bytes=max_bytes)


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
    head = {k: walk.get(k) for k in ("walk", "serial", "package", "talkback", "start", "direction",
                                      "until", "ended", "ms", "utterance", "vs_model", "restore")}
    head["steps"] = sum(1 for r in walk["steps"] if r["i"] > 0)
    if walk.get("expect") is not None:
        head["expect"] = walk["expect"]
    if walk.get("cycle"):
        head["cycle"] = walk["cycle"]
    if walk.get("notes"):
        head["notes"] = walk["notes"]
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
