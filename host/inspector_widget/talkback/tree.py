# Portions of this file are derived from google/talkback (https://github.com/google/talkback)
# at commit 229212f (TalkBack 16.2), licensed under the Apache License, Version 2.0.
# Reimplemented in Python and modified for Inspector Widget; see NOTICE.
"""The tree TalkBack sees ("the TalkBack view"), projected from our unified a11y dump.

Our agent reads the accessibility tree in-process through a DirectAccessibilityConnection whose
fetch flags include ``FLAG_INCLUDE_NOT_IMPORTANT_VIEWS`` (AOSP
DirectAccessibilityConnection.java:57), so the dump holds every VISIBLE View. TalkBack does not
request that flag (res/xml-v33/accessibilityservice.xml), so the platform serves it a smaller tree:

* A View that is not important for accessibility is left out and its children take its place,
  in order (AOSP ViewGroup.addChildrenForAccessibility, ViewGroup.java:2477). A View with
  importantForAccessibility=noHideDescendants takes its whole subtree with it
  (View.isImportantForAccessibility, View.java:15687). The decision is a11y-core's
  :func:`inspector_widget.a11y.talkback_exclusion`, consumed read-only here; it is exact when the
  agent reports resolved importance (its ``window type=`` diagnostics tokens say so) and a
  heuristic otherwise (``importance_conf``).
* A traversal link to a View that is not important is dropped by the platform
  (View.java:11440-11462), so link targets resolve only against the projected nodes.
* Provider (virtual) nodes are kept as the provider serves them.
* Compose marks every AndroidView holder invisible while an accessibility service runs
  (AndroidComposeView.addAndroidView: ``info.isVisibleToUser = false`` when the delegate
  ``isEnabled``). A dump taken with no service on shows the holder visible; the projection
  applies the correction so the model sees what TalkBack would (``services``). The spike
  compared dumps with TalkBack on and off on eight screens: this was the only difference (links,
  structure, bounds, text and importance are identical) until a RecyclerView screen showed
  another: the item info RecyclerView adds only while a service is on (:mod:`.recycler`).
* A node wholly outside its window's interactive region (the part of the window not covered by
  other windows, e.g. under the status bar of an edge-to-edge window) is served with
  isVisibleToUser=false (AOSP AccessibilityInteractionController.adjustIsVisibleToUserIfNeeded,
  from memory: not in the scratch AOSP copy). Our in-process connection passes no interactive
  region, so the dump says visible. Given the system bars (``obscured``), the projection
  applies the correction. [ASSUMED, TODO confirm: seen on TalkBack 17.0 / API 37, where
  "1. ImageButton contentDescription" under the status bar was never evaluated.]
* Windows: TalkBack gets no window below an open modal window (AOSP AccessibilityWindowManager
  drops covered windows) and no window that is neither touchable nor focusable
  (windowMattersToAccessibilityLocked). a11y-core's :func:`~inspector_widget.a11y.apply_window_meta`
  supplies the type, flags, modality and ``covered_by``.

Every node is tagged with the facet that produced it: ``view``, ``interop`` (a View a Compose
node added as its child: an AndroidView holder), ``compose``, ``fake_role`` / ``fake_cd``
(Compose's synthetic children, semantics id + 1e9 / + 2e9, SemanticsNode.kt:541-550) or
``virtual`` (another provider: WebView, ExploreByTouchHelper).
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from .. import a11y as _a11y

FAKE_ROLE_OFFSET = 1_000_000_000  # SemanticsNode.kt:541 RoleFakeNodeIdOffset
FAKE_CD_OFFSET = 2_000_000_000  # SemanticsNode.kt:542 ContentDescriptionFakeNodeIdOffset

# AccessibilityWindowInfo.TYPE_*.
WINDOW_APPLICATION = 1
WINDOW_INPUT_METHOD = 2
WINDOW_SYSTEM = 3
WINDOW_ACCESSIBILITY_OVERLAY = 4
WINDOW_SPLIT_SCREEN_DIVIDER = 5
WINDOW_MAGNIFICATION_OVERLAY = 6

# WindowManager.LayoutParams.type -> AccessibilityWindowInfo type
# (AccessibilityWindowManager.getTypeForWindowManagerWindowType, AOSP :986-1040).
_APPLICATION_LP_TYPES = {
    2, 1001, 1000, 3, 1002, 1005, 1, 4,  # APPLICATION, MEDIA, PANEL, STARTING, SUB_PANEL,
    # ABOVE_SUB_PANEL, BASE_APPLICATION, DRAWN_APPLICATION
    2002, 2007, 2005, 1003, 2012,  # PHONE, PRIORITY_PHONE, TOAST, ATTACHED_DIALOG, IME_DIALOG
}
_SYSTEM_LP_TYPES = {2009, 2019, 2024, 2001, 2000, 2040, 2041, 2017, 2020, 2003, 2008, 2010,
                    2006, 2038, 2036}

_SERVICES_TOKEN = re.compile(r"a11y-services=(on|off)")
_COMPOSE_UNAVAILABLE = re.compile(r"compose-traversal[^;]*?unavailable=(\d+)")
_RESOLVED_IMPORTANCE_TOKEN = re.compile(r"root#-?\d+ window type=")


def a11y_window_type(lp_type: Optional[int]) -> Optional[int]:
    """The AccessibilityWindowInfo type the system reports for a window of ``lp_type``."""
    if lp_type is None:
        return None
    if lp_type in _APPLICATION_LP_TYPES:
        return WINDOW_APPLICATION
    if lp_type == 2011:
        return WINDOW_INPUT_METHOD
    if lp_type in _SYSTEM_LP_TYPES:
        return WINDOW_SYSTEM
    if lp_type == 2034:
        return WINDOW_SPLIT_SCREEN_DIVIDER
    if lp_type == 2032:
        return WINDOW_ACCESSIBILITY_OVERLAY
    if lp_type == 2039:
        return WINDOW_MAGNIFICATION_OVERLAY
    return None


class Rect:
    """android.graphics.Rect: left/top/right/bottom in screen px."""

    __slots__ = ("left", "top", "right", "bottom")

    def __init__(self, left: int = 0, top: int = 0, right: int = 0, bottom: int = 0):
        self.left, self.top, self.right, self.bottom = int(left), int(top), int(right), int(bottom)

    @classmethod
    def of(cls, bounds: Any) -> "Rect":
        """From a dump ``bounds`` ({"layout": {x, y, w, h}} or {x, y, w, h})."""
        if not isinstance(bounds, dict):
            return cls()
        b = bounds.get("layout", bounds)
        if not isinstance(b, dict):
            return cls()
        x, y = int(b.get("x", 0)), int(b.get("y", 0))
        return cls(x, y, x + int(b.get("w", 0)), y + int(b.get("h", 0)))

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def is_empty(self) -> bool:
        return self.left >= self.right or self.top >= self.bottom

    def intersects(self, o: "Rect") -> bool:
        return (self.left < o.right and o.left < self.right
                and self.top < o.bottom and o.top < self.bottom)

    def contains(self, o: "Rect") -> bool:
        return (self.left <= o.left and self.top <= o.top
                and self.right >= o.right and self.bottom >= o.bottom)

    def intersect(self, o: "Rect") -> "Rect":
        return Rect(max(self.left, o.left), max(self.top, o.top),
                    min(self.right, o.right), min(self.bottom, o.bottom))

    def minus(self, o: "Rect") -> List["Rect"]:
        """The parts of this rect outside ``o`` (up to four rects)."""
        if self.is_empty() or not self.intersects(o):
            return [] if self.is_empty() else [self]
        out = [Rect(self.left, self.top, self.right, o.top),
               Rect(self.left, o.bottom, self.right, self.bottom),
               Rect(self.left, max(self.top, o.top), o.left, min(self.bottom, o.bottom)),
               Rect(o.right, max(self.top, o.top), self.right, min(self.bottom, o.bottom))]
        return [r for r in out if not r.is_empty()]

    def union(self, o: "Rect") -> "Rect":
        return Rect(min(self.left, o.left), min(self.top, o.top),
                    max(self.right, o.right), max(self.bottom, o.bottom))

    def tuple(self) -> Tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)

    def to_xywh(self) -> Dict[str, int]:
        return {"x": self.left, "y": self.top, "w": self.width, "h": self.height}

    def __eq__(self, o: object) -> bool:
        return isinstance(o, Rect) and self.tuple() == o.tuple()

    def __hash__(self) -> int:
        return hash(self.tuple())

    def __repr__(self) -> str:
        return "Rect(%d, %d - %d, %d)" % self.tuple()


class TbWindow:
    """One window of the dump, as TalkBack would get it (or not)."""

    def __init__(self, index: int, meta: Dict[str, Any]):
        self.index = index  # position in the dump: z-order, bottom first
        self.root_view_id = meta.get("root_view_id")
        self.lp_type: Optional[int] = meta.get("window_type")
        flags = meta.get("window_flags")
        self.lp_flags: Optional[int] = int(flags, 16) if isinstance(flags, str) else flags
        self.modal: Optional[bool] = meta.get("modal")
        self.covered_by = meta.get("covered_by")
        self.title: Optional[str] = meta.get("title") or None
        self.obscured: List[Rect] = [_rect(r) for r in meta.get("obscured") or ()]
        self.a11y_type = a11y_window_type(self.lp_type)
        self.root: Optional[TbNode] = None
        frame = meta.get("frame")
        self.bounds_source = "frame" if frame else "root"
        self.bounds = Rect.of(frame) if frame else Rect()
        self.dropped: Optional[str] = None
        if self.covered_by is not None:
            self.dropped = f"covered_by:{self.covered_by}"
        elif (self.lp_flags is not None and self.lp_flags & _a11y.FLAG_NOT_TOUCHABLE
              and self.lp_flags & _a11y.FLAG_NOT_FOCUSABLE):
            # Neither touchable nor focusable: never reported (windowMattersToAccessibilityLocked).
            self.dropped = "not_touchable"

    @property
    def reported(self) -> bool:
        """Whether AccessibilityService.getWindows() would list this window."""
        return self.dropped is None and self.root is not None

    @property
    def focusable(self) -> bool:
        return self.lp_flags is None or not self.lp_flags & _a11y.FLAG_NOT_FOCUSABLE

    def __repr__(self) -> str:
        return f"TbWindow({self.index}, root={self.root_view_id})"


class TbNode:
    """One node of the TalkBack view: the dump dict plus its TalkBack-view relations."""

    __slots__ = ("raw", "window", "parent", "children", "facet", "visible", "corrections",
                 "rect", "flags", "actions", "__weakref__")

    def __init__(self, raw: Dict[str, Any], window: TbWindow, parent: Optional["TbNode"],
                 facet: str):
        self.raw = raw
        self.window = window
        self.parent = parent
        self.children: List[TbNode] = []
        self.facet = facet
        self.flags = frozenset(raw.get("flags") or ())
        self.visible = "visible_to_user" in self.flags
        self.corrections: List[str] = []
        self.rect = Rect.of(raw.get("bounds"))
        self.actions = _action_ids(raw)

    # -- identity --------------------------------------------------------------------------
    @property
    def id(self) -> Optional[int]:
        k = self.raw.get("id")
        if k is not None:
            return int(k)
        h = self.raw.get("host_view_id")
        if h is None:
            return None
        return _a11y.a11y_key(h, self.raw.get("virtual_id", _a11y.HOST_VIEW_ID))

    @property
    def key(self) -> str:
        k = self.raw.get("node_key")
        if k:
            return k
        h = self.raw.get("host_view_id")
        v = int(self.raw.get("virtual_id", _a11y.HOST_VIEW_ID))
        if h:
            if v == _a11y.HOST_VIEW_ID:
                return f"view:{int(h)}"
            kind = "virtual" if self.facet == "virtual" else "compose"
            return f"{kind}:{int(h)}:{v}"
        return f"id:{self.id}"

    @property
    def signature(self) -> Tuple[str, str, str, str]:
        """Identity without ids or bounds: (facet, class, label, resource id). Compose re-mints
        semantics ids when lazy items scroll, and bounds change while scrolling, so a node seen in
        two captures (or a live walk) is matched by this, not by its key."""
        return (self.facet, self.class_name, self.content_description or self.text,
                self.raw.get("view_id_resource_name") or "")

    @property
    def virtual_id(self) -> int:
        return int(self.raw.get("virtual_id", _a11y.HOST_VIEW_ID))

    @property
    def semantics_id(self) -> Optional[int]:
        """A Compose node's semantics id, with the fake-child offsets stripped."""
        if self.facet not in ("compose", "fake_role", "fake_cd"):
            return None
        v = self.virtual_id
        if v >= FAKE_CD_OFFSET:
            return v - FAKE_CD_OFFSET
        if v >= FAKE_ROLE_OFFSET:
            return v - FAKE_ROLE_OFFSET
        return v

    @property
    def is_fake(self) -> bool:
        return self.facet in ("fake_role", "fake_cd")

    # -- AccessibilityNodeInfo getters ------------------------------------------------------
    def has(self, flag: str) -> bool:
        return flag in self.flags

    def supports(self, *action_ids: int) -> bool:
        return any(a in self.actions for a in action_ids)

    @property
    def class_name(self) -> str:
        return self.raw.get("class_name") or ""

    @property
    def text(self) -> str:
        return self.raw.get("text") or ""

    @property
    def content_description(self) -> str:
        return self.raw.get("content_description") or ""

    @property
    def hint_text(self) -> str:
        return self.raw.get("hint_text") or ""

    @property
    def state_description(self) -> str:
        return self.raw.get("state_description") or ""

    def get(self, field: str, default: Any = None) -> Any:
        return self.raw.get(field, default)

    def ancestors(self) -> Iterator["TbNode"]:
        p = self.parent
        while p is not None:
            yield p
            p = p.parent

    def iter(self) -> Iterator["TbNode"]:
        """Pre-order over this node and its TalkBack-view descendants."""
        stack = [self]
        while stack:
            n = stack.pop()
            yield n
            stack.extend(reversed(n.children))

    def __repr__(self) -> str:
        return f"TbNode({self.key})"


def _action_ids(raw: Dict[str, Any]) -> frozenset:
    ids = {a.get("id") for a in (raw.get("actions") or []) if isinstance(a, dict)}
    mask = int(raw.get("actions_bitmask") or 0)
    bit = 1
    while mask and bit <= 0x00200000:  # the legacy getActions() bitmask (ACTION_FOCUS..SET_TEXT)
        if mask & bit:
            ids.add(bit)
        bit <<= 1
    ids.discard(None)
    return frozenset(ids)


class Excluded:
    """A dump node TalkBack never gets (not important, or hidden by noHideDescendants)."""

    __slots__ = ("raw", "reason", "hidden_by", "parent", "window")

    def __init__(self, raw, reason, hidden_by, parent, window):
        self.raw = raw
        self.reason = reason  # "not_important" | "hidden"
        self.hidden_by = hidden_by  # the noHideDescendants dump node, for "hidden"
        self.parent = parent  # the TalkBack-view node its children were hoisted into
        self.window = window


class TbTree:
    """The TalkBack view of one dump: windows, projected nodes, what was left out and why."""

    def __init__(self) -> None:
        self.windows: List[TbWindow] = []
        self.nodes: List[TbNode] = []
        self.excluded: List[Excluded] = []
        self.by_id: Dict[int, TbNode] = {}
        self.by_raw: Dict[int, TbNode] = {}  # id(dump dict) -> node
        self.raw_by_id: Dict[int, Dict[str, Any]] = {}  # every dump node, TalkBack's or not
        self.excluded_by_raw: Dict[int, Excluded] = {}
        self.services: Optional[str] = None  # "on" | "off" | None (unknown)
        self.importance_conf = "heuristic"  # "agent" when the agent resolved importance
        self.diagnostics: List[Dict[str, Any]] = []

    def node(self, ref: Any) -> Optional[TbNode]:
        """Look a node up by TbNode, dump dict, int key or typed key string."""
        if isinstance(ref, TbNode):
            return ref
        if isinstance(ref, dict):
            return self.by_raw.get(id(ref))
        if isinstance(ref, int):
            return self.by_id.get(ref)
        if isinstance(ref, str):
            for n in self.nodes:
                if n.key == ref:
                    return n
        return None

    def resolve_raw(self, key: Any) -> Optional[Dict[str, Any]]:
        """Any dump node by packed key, including the Views TalkBack's tree leaves out (the
        platform still serves a node fetched by id, e.g. a labeledBy target)."""
        return self.raw_by_id.get(int(key)) if key else None

    def resolve_link(self, key: Any) -> Optional[TbNode]:
        """What getTraversalBefore/After (or getLabeledBy) returns for a packed key: the
        TalkBack-view node, or None when TalkBack would get nothing (0, absent, or a View
        that is not important, whose link the platform drops)."""
        if not key:
            return None
        return self.by_id.get(int(key))


def _windows_of(dump: Any) -> Tuple[List[Dict[str, Any]], str]:
    if isinstance(dump, dict) and "windows" in dump:
        return [dict(w) for w in dump["windows"]], dump.get("diagnostics") or ""
    if isinstance(dump, dict):
        return [{"root_view_id": dump.get("host_view_id"), "root": dump}], ""
    return [{"root_view_id": (r or {}).get("host_view_id"), "root": r} for r in dump or []], ""


def _is_compose_host(raw: Dict[str, Any]) -> bool:
    for field in ("provider_class", "class_name"):
        if "compose" in (raw.get(field) or "").lower():
            return True
    return False


def build(dump: Any, *, services: Optional[str] = None, diagnostics: Optional[str] = None,
          skip: Optional[set] = None, obscured: Optional[List[Any]] = None) -> TbTree:
    """Project a dump onto the tree TalkBack sees.

    ``dump`` is :func:`inspector_widget.a11y.a11y_to_dict` output (``{"windows": [...]}``), a
    list of window root dicts, or one root dict. ``services`` ("on"/"off") overrides the
    dump's ``a11y-services=`` diagnostics token; with "off" the service-on corrections are
    applied. ``skip`` holds window indices to treat as unreachable (a11y.reading_order's
    parameter of the same name). ``obscured``: screen rects other windows cover in every window
    (the system bars: e.g. ``[(0, 0, 1280, 156)]`` as x, y, w, h), added to each window's own
    ``obscured`` meta; nodes wholly inside them are hidden as TalkBack would get them. The dump
    is not modified.
    """
    windows_meta, diag = _windows_of(dump)
    if diagnostics is not None:
        diag = diagnostics
    tree = TbTree()
    m = _SERVICES_TOKEN.search(diag)
    tree.services = services or (m.group(1) if m else None)
    if _RESOLVED_IMPORTANCE_TOKEN.search(diag):
        tree.importance_conf = "agent"
    if diag and not any("window_type" in w for w in windows_meta):
        _a11y.apply_window_meta(windows_meta, diag)

    compose_hosts = set()
    for w in windows_meta:
        stack = [w.get("root")] if w.get("root") else []
        while stack:
            n = stack.pop()
            if n.get("id") is not None and n.get("host_view_id"):
                tree.raw_by_id.setdefault(int(n["id"]), n)
            if int(n.get("virtual_id", _a11y.HOST_VIEW_ID)) == _a11y.HOST_VIEW_ID \
                    and _is_compose_host(n):
                compose_hosts.add(int(n.get("host_view_id") or 0))
            stack.extend(n.get("children") or ())

    skip = set(skip or ())
    for wi, meta in enumerate(windows_meta):
        win = TbWindow(wi, meta)
        win.obscured.extend(_rect(r) for r in obscured or ())
        if wi in skip and win.dropped is None:
            win.dropped = "skipped"
        tree.windows.append(win)
        root_raw = meta.get("root")
        if not root_raw:
            continue
        win.root = _project(tree, win, root_raw, compose_hosts)
        if win.bounds_source == "root":
            win.bounds = win.root.rect

    dupes = []
    for n in tree.nodes:
        k = n.id
        if k is None or not n.raw.get("host_view_id"):
            continue  # host 0: the agent could not resolve the View, not addressable
        if k in tree.by_id:
            dupes.append(n.key)
            continue
        tree.by_id[k] = n
    if dupes:
        tree.diagnostics.append({
            "kind": "duplicate_key", "count": len(dupes), "keys": sorted(set(dupes))[:10],
            "message": (f"{len(dupes)} nodes share a node key with an earlier node; links to "
                        "them resolve to the first one."),
        })
    unavailable = sum(int(x) for x in _COMPOSE_UNAVAILABLE.findall(diag))
    if unavailable:
        # With "computed=N" (no service) or "service=N" the agent's links equal TalkBack's; with
        # "unavailable" Compose served none, so its content is in composition order.
        tree.diagnostics.append({
            "kind": "compose_order_unknown", "count": unavailable,
            "message": (f"{unavailable} ComposeView(s) served no traversal links; their order "
                        "here is composition order and TalkBack's may differ."),
        })
    _apply_interactive_region(tree)
    if tree.services == "off":
        holders = [n for n in tree.nodes if n.facet == "interop" and n.visible]
        for n in holders:
            n.visible = False
            n.corrections.append("holder_invisible_with_service")
        if holders:
            tree.diagnostics.append({
                "kind": "service_off_corrections", "count": len(holders),
                "message": (f"The dump was taken with no accessibility service on; {len(holders)} "
                            "AndroidView holder(s) are shown invisible, as Compose reports them "
                            "while TalkBack runs."),
            })
    from .recycler import apply_item_info  # RecyclerView item info (service on/off)

    apply_item_info(tree)
    return tree


def _facet(raw: Dict[str, Any], raw_parent: Optional[Dict[str, Any]], compose_hosts: set) -> str:
    v = int(raw.get("virtual_id", _a11y.HOST_VIEW_ID))
    if v == _a11y.HOST_VIEW_ID:
        if raw_parent is not None and \
                int(raw_parent.get("virtual_id", _a11y.HOST_VIEW_ID)) != _a11y.HOST_VIEW_ID:
            return "interop"  # a View a provider added as its child (Compose's AndroidView holder)
        return "view"
    compose = (int(raw.get("host_view_id") or 0) in compose_hosts
               or (raw.get("node_key") or "").startswith("compose:"))
    if not compose:
        return "virtual"
    if v >= FAKE_CD_OFFSET:
        return "fake_cd"
    if v >= FAKE_ROLE_OFFSET:
        return "fake_role"
    return "compose"


def _project(tree: TbTree, win: TbWindow, root_raw: Dict[str, Any], compose_hosts: set) -> TbNode:
    root = TbNode(root_raw, win, None, _facet(root_raw, None, compose_hosts))
    tree.nodes.append(root)
    tree.by_raw[id(root_raw)] = root
    # (dump node, its dump parent, the TalkBack-view node it attaches to)
    stack: List[Tuple[Dict[str, Any], Dict[str, Any], TbNode]] = [
        (c, root_raw, root) for c in reversed(root_raw.get("children") or [])]
    while stack:
        raw, raw_parent, tb_parent = stack.pop()
        why = _a11y.talkback_exclusion(raw, raw_parent)
        if why == "hidden":
            _exclude_subtree(tree, win, raw, tb_parent)
            continue
        if why == "not_important":
            ex = Excluded(raw, "not_important", None, tb_parent, win)
            tree.excluded.append(ex)
            tree.excluded_by_raw[id(raw)] = ex
            # Its children take its place in the parent's list, in order.
            stack.extend((c, raw, tb_parent) for c in reversed(raw.get("children") or []))
            continue
        node = TbNode(raw, win, tb_parent, _facet(raw, raw_parent, compose_hosts))
        tb_parent.children.append(node)
        tree.nodes.append(node)
        tree.by_raw[id(raw)] = node
        stack.extend((c, raw, node) for c in reversed(raw.get("children") or []))
    return root


def _exclude_subtree(tree: TbTree, win: TbWindow, top: Dict[str, Any], parent: TbNode) -> None:
    stack = [top]
    while stack:
        raw = stack.pop()
        ex = Excluded(raw, "hidden", top, parent, win)
        tree.excluded.append(ex)
        tree.excluded_by_raw[id(raw)] = ex
        stack.extend(raw.get("children") or ())


def iter_reported(tree: TbTree) -> Iterator[TbNode]:
    for w in tree.windows:
        if w.reported:
            yield from w.root.iter()


NodePredicate = Callable[[TbNode], bool]


def _rect(r: Any) -> Rect:
    """A Rect from a Rect, an (x, y, w, h) tuple or a bounds dict."""
    if isinstance(r, Rect):
        return r
    if isinstance(r, (tuple, list)):
        x, y, w, h = r
        return Rect(x, y, x + w, y + h)
    return Rect.of(r)


def _apply_interactive_region(tree: TbTree) -> None:
    """Hide the nodes outside their window's interactive region (see the module docstring).

    The platform tests Region.quickReject(boundsInScreen), which is conservative: it rejects a
    node only when it misses the region's bounding box. So a node under a system bar along the
    window's edge is hidden, but one inside a hole in the middle of the region (under a
    floating window) is not. Only runs where the covered parts are known."""
    hidden = 0
    for w in tree.windows:
        if w.root is None or not w.obscured:
            continue
        region = [w.bounds]
        for o in w.obscured:
            region = [p for part in region for p in part.minus(o)]
        if not region:
            continue
        box = region[0]
        for part in region[1:]:
            box = box.union(part)
        for n in w.root.iter():
            if n.visible and not n.rect.intersects(box):
                n.visible = False
                n.corrections.append("obscured_by_system_bar")
                hidden += 1
    if hidden:
        tree.diagnostics.append({
            "kind": "obscured_by_system_bar", "count": hidden, "conf": "assumed",
            "message": (f"{hidden} node(s) lie wholly under a system bar; the platform serves "
                        "them to TalkBack as not visible, so TalkBack skips them."),
        })


# View classes whose onPopulateAccessibilityEvent adds text: TextView (and every subclass) its
# text, else its hint; ImageView (and subclasses) its contentDescription.
def populated_text(raw: Dict[str, Any]) -> List[str]:
    """The text a View subtree puts into an AccessibilityEvent it sends
    (ViewGroup.dispatchPopulateAccessibilityEventInternal: every VISIBLE descendant, important
    for accessibility or not, in accessibility child order). Views only: a provider host (a
    ComposeView, a WebView) adds nothing and is not descended into."""
    from .rules import is_instance

    out: List[str] = []
    stack = [raw]
    while stack:
        n = stack.pop()
        if int(n.get("virtual_id", _a11y.HOST_VIEW_ID)) != _a11y.HOST_VIEW_ID:
            continue
        cls = n.get("class_name") or ""
        if is_instance(cls, "android.widget.TextView"):
            t = n.get("text") or n.get("hint_text")
            if t and "password" not in (n.get("flags") or ()):
                out.append(t)
        elif is_instance(cls, "android.widget.ImageView") and n.get("content_description"):
            out.append(n["content_description"])
        if n.get("provider_class"):
            continue
        stack.extend(reversed(n.get("children") or ()))
    return out


def window_title(tree: TbTree, window: TbWindow) -> Tuple[Optional[str], Optional[str]]:
    """The title TalkBack keeps for a window, and where it came from.

    WindowEventInterpreter.getWindowTitleInternal (UT/input/WindowEventInterpreter.java:385):
    the window's own title (AccessibilityWindowInfo.getTitle; from the dump's ``title`` meta)
    when it has one, else the first text of the window's TYPE_WINDOW_STATE_CHANGED event
    (getTextFromWindowStateChange, :990), which is the first populated text of its View tree.
    The activity window's title (its label) is not in the dump, so only a dialog or popup
    window gets a derived title.
    """
    if window.title:
        return window.title, "window"
    base_activity = window.lp_type == 1 or (window.lp_type is None and window.index == 0)
    if window.root is None or base_activity:
        return None, None
    texts = populated_text(window.root.raw)
    return (texts[0], "event") if texts else (None, None)
