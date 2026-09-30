"""The unified capture index: one node per logical UI element (spec 3.3-3.7).

:func:`build_index` decodes a capture's raw protobuf facets and returns a
**key-space** :class:`~.model.Index` (every ``ref`` is None; ids are canonical
keys). :func:`apply_refs` moves it to ref space once the carry-over matcher has
produced a refmap.

How the index is built:

1. **Views are the spine.** Window roots come from GetWindows (``z`` is their
   order; dialogs and popups are separate roots). Each View is ``view:<udid>``;
   its ``b`` is its layout rect clipped by its ancestors, and ``declared_b`` keeps
   the unclipped rect when they differ.
2. **Compose** is grafted per AndroidComposeView (ACV). The synthetic window root
   that reuses the ACV's id (the ID3 collision) is folded into the ACV's View
   node; its semantics subtree hangs under that node as ``sem:<acv>:<id>``, so
   equal semantics ids in different ComposeViews (RecyclerView cells) stay
   distinct. Window-relative semantics (a dialog before the agent's CO4 fix) are
   shifted to screen px, with a diagnostic. The View subtree of each
   ``AndroidView`` (children of ``AndroidViewsHandler``) is re-parented under the
   smallest semantics node that contains it (``interop`` flag, ``conf.ui``
   inferred).
3. **Accessibility** attaches as a facet. The join is exact on
   ``(host_view_id, virtual_id)``: ``(udid, -1)`` is the View, ``(acv, id)`` the
   semantics node. The **ID1 detector** switches a window to one-to-one matching
   by bounds when the agent's ids cannot be trusted there (duplicate pairs, or
   virtual nodes hosted by a plain View with children). Those facets are
   ``conf.a11y = inferred`` and the capture gets a diagnostic. Accessibility nodes
   with no View/Compose twin become ``kind=a11y`` nodes under their a11y parent's
   node.
4. **Slot-table groups** become ``slot`` nodes keyed ``slot:<acv>:<hash of
   anchor>``. Each semantics node is linked (inferred) to the app-code group that
   emitted it (bounds, then testTag) and to the app-code groups inside that one;
   the emitter gives it ``src``, ``declared_b`` and its display type.
5. ``type``, ``label``, ``flags``, the raw text fields, ``anchor`` and ``sel``
   are derived (see :mod:`.anchors`).

View properties are **not** decoded here (only four flag properties are peeked by
string id); :class:`FacetReader` decodes them per view on demand.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .. import normalize as nz
from ..a11y import action_name
from ..proto import view_inspection_pb2 as pb
from ..strings import StringResolver, property_to_dict
from . import anchors
from .model import (
    FLAGS,
    Index,
    OpError,
    RawCapture,
    Tree,
    UNode,
    a11y_key,
    a11y_path_key,
    anchor_hash,
    compose_legacy_key,
    remap_ids,
    sem_key,
    view_key,
    window_key,
)

#: One-to-one bounds matching threshold (spec 3.6).
IOU_MIN = 0.6
#: Containment fallback: a clipped twin (e.g. a list row cut by the viewport).
CONTAIN_MIN = 0.9
#: Slot box vs semantics bounds agreement for the emitter link.
SLOT_IOU_MIN = 0.9
EDGE_TOL = 2
TEXT_CAP = 1000

ID1_DUPLICATES = "a11y ids not unique (agent ID1); a11y facets matched by bounds"
ID1_IMPLAUSIBLE = ("a11y ids implausible (agent ID1: virtual nodes on a plain View); "
                   "a11y facets matched by bounds")

ACV_CLASS = "AndroidComposeView"
VIEWS_HANDLER_CLASS = "AndroidViewsHandler"

#: a11y class simple name -> display role.
ROLE_BY_A11Y_CLASS = {
    "Button": "Button", "ImageButton": "ImageButton", "CheckBox": "Checkbox",
    "Switch": "Switch", "RadioButton": "RadioButton", "ToggleButton": "ToggleButton",
    "EditText": "EditText", "SeekBar": "SeekBar", "ProgressBar": "ProgressBar",
    "Spinner": "Spinner", "ImageView": "Image",
}
_A11Y_CLASS_BY_ROLE = {"Button": "Button", "Checkbox": "CheckBox", "Switch": "Switch",
                       "RadioButton": "RadioButton", "Image": "ImageView",
                       "DropdownList": "Spinner"}
_ROLE_WORD = re.compile(r"^[A-Z][A-Za-z0-9_]*$")

_A11Y_BOOLS = (
    "clickable", "long_clickable", "context_clickable", "checkable", "checked",
    "focusable", "focused", "accessibility_focused", "selected", "enabled", "password",
    "scrollable", "visible_to_user", "heading", "screen_reader_focusable", "dismissable",
    "editable", "multi_line", "content_invalid", "showing_hint_text", "text_entry_key",
    "text_selectable", "field_required", "can_open_popup", "a11y_data_sensitive",
    "request_initial_focus", "is_virtual", "is_traversal_group",
)
_A11Y_TEXTS = (
    "text", "content_description", "hint_text", "state_description", "error",
    "tooltip_text", "pane_title", "container_title", "supplemental_description",
    "role_description", "class_name", "package_name", "view_id_resource_name",
    "unique_id", "provider_class",
)
_A11Y_FACET_TEXTS = (("hint_text", "hint"), ("error", "error"), ("tooltip_text", "tooltip"),
                     ("pane_title", "pane"), ("container_title", "container"),
                     ("supplemental_description", "supplemental"))
_LIVE = {1: "polite", 2: "assertive"}
_RANGE_TYPES = {0: "int", 1: "float", 2: "percent", 3: "indeterminate"}
_FLAG_PROPS = ("visibility", "enabled", "clickable", "longClickable")
_TESTTAG_RE = re.compile(r"testTag\(tag=([^,)]*)")


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
def _rect(bounds: Any) -> list[int]:
    lay = bounds.layout
    return [int(lay.x), int(lay.y), int(lay.w), int(lay.h)]


def _area(r: Sequence[int] | None) -> int:
    return max(0, r[2]) * max(0, r[3]) if r else 0


def _inter(a: Sequence[int], b: Sequence[int]) -> list[int] | None:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    if x1 <= x0 or y1 <= y0:
        return None
    return [x0, y0, x1 - x0, y1 - y0]


def _iou(a: Sequence[int], b: Sequence[int]) -> float:
    i = _inter(a, b)
    if i is None:
        return 0.0
    ia = _area(i)
    return ia / float(_area(a) + _area(b) - ia)


def _containment(a: Sequence[int], b: Sequence[int]) -> float:
    """Intersection over the smaller area."""
    i = _inter(a, b)
    small = min(_area(a), _area(b))
    return _area(i) / float(small) if i is not None and small else 0.0


def _contains(outer: Sequence[int], inner: Sequence[int], tol: int = EDGE_TOL) -> bool:
    return (inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol
            and inner[0] + inner[2] <= outer[0] + outer[2] + tol
            and inner[1] + inner[3] <= outer[1] + outer[3] + tol)


def _clip(r: Sequence[int], clip: Sequence[int] | None) -> list[int]:
    """``r`` clipped to ``clip``; an empty result keeps a clamped 0-size origin."""
    if clip is None:
        return list(r)
    i = _inter(r, clip)
    if i is not None:
        return i
    x = min(max(r[0], clip[0]), clip[0] + clip[2])
    y = min(max(r[1], clip[1]), clip[1] + clip[3])
    return [x, y, 0, 0]


def _shift(r: Sequence[int], dx: int, dy: int) -> list[int]:
    return [r[0] + dx, r[1] + dy, r[2], r[3]]


class _Grid:
    """Rect centers bucketed in 128 px cells: which rects have their center inside a
    query rect (IoU >= 0.5 implies that, so the bounds matchers only look there)."""

    CELL = 128

    def __init__(self) -> None:
        self._cells: dict[tuple[int, int], list[tuple[str, float, float]]] = defaultdict(list)

    def add(self, key: str, r: Sequence[int]) -> None:
        cx, cy = r[0] + r[2] / 2.0, r[1] + r[3] / 2.0
        self._cells[(int(cx) // self.CELL, int(cy) // self.CELL)].append((key, cx, cy))

    def centers_in(self, r: Sequence[int]) -> Iterator[str]:
        c = self.CELL
        x0, y0, x1, y1 = r[0], r[1], r[0] + r[2], r[1] + r[3]
        for gx in range(int(x0) // c, int(x1) // c + 1):
            for gy in range(int(y0) // c, int(y1) // c + 1):
                for key, cx, cy in self._cells.get((gx, gy), ()):
                    if x0 <= cx <= x1 and y0 <= cy <= y1:
                        yield key


# --------------------------------------------------------------------------- #
# Decoding helpers
# --------------------------------------------------------------------------- #
def _parse(cls: Any, data: bytes | None, name: str, diags: list[str]) -> Any:
    if not data:
        return None
    msg = cls()
    try:
        msg.ParseFromString(bytes(data))
    except Exception as exc:  # noqa: BLE001 - a bad facet must not sink the others
        diags.append(f"{name}: could not be parsed ({type(exc).__name__}); facet skipped")
        return None
    return msg


def _simple(name: str | None) -> str | None:
    if not name:
        return None
    return name.rsplit(".", 1)[-1] or None


def _cap(s: str | None, n: int = TEXT_CAP) -> str | None:
    if s is None:
        return None
    s = str(s)
    if not s:
        return None
    return s if len(s) <= n else s[: n - 1] + "…"


def _order_flags(flags: Iterable[str]) -> list[str]:
    fs = set(flags)
    if fs & {"click", "longclick"}:
        fs.discard("focus")  # clickable implies focusable
    return [f for f in FLAGS if f in fs]


def _is_true(v: str | None) -> bool:
    return v is not None and v.strip().lower() == "true"


# --------------------------------------------------------------------------- #
# Intermediate records
# --------------------------------------------------------------------------- #
@dataclass
class _A:
    """One accessibility node, flattened."""

    idx: int
    host: int
    virt: int
    rect: list[int]
    parent: int | None
    path: tuple[int, ...]
    root_view: int
    txt: dict[str, str]
    bools: set[str]
    msg: Any
    res: Any
    children: list[int] = field(default_factory=list)
    target: str | None = None
    conf: str = "exact"
    spoken: str | None = None

    @property
    def packed(self) -> int:
        return (self.host << 32) ^ (self.virt & 0xFFFFFFFF)

    @property
    def focusable(self) -> bool:
        """TalkBack-focusable (a stop that speaks for its descendants); plain input
        focusability (a ScrollView) does not count."""
        return bool(self.bools & {"screen_reader_focusable", "clickable", "long_clickable"})


@dataclass
class _S:
    """One slot-table group."""

    idx: int
    acv: int
    name: str
    src: str | None
    box: list[int]
    raw: dict[str, str]
    path: tuple[int, ...]
    parent: int | None
    depth: int
    render_node_id: int
    children: list[int] = field(default_factory=list)
    origin: str = "library"
    tag: str | None = None
    key: str | None = None
    sem: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Lazy facet decoding
# --------------------------------------------------------------------------- #
class FacetReader:
    """On-demand decoding of the bulky parts of a capture's raw facets.

    ``props(udid)`` / ``prop_list(udid)`` decode one View's PropertyGroup from
    ``views.pb`` (``decoded`` counts the groups decoded so far);
    ``slot_params(slot_path)`` returns a slot group's raw parameter strings from
    ``slots.pb`` (``UNode.ids["slot_path"]``); ``sem_attrs(acv, id)`` returns a
    semantics node's raw attrs from ``compose_sem.pb``. Nothing is parsed until
    first use.
    """

    def __init__(self, raw: RawCapture) -> None:
        self._raw = raw
        self._views: Any = None
        self._resolver: StringResolver | None = None
        self._groups: dict[int, Any] | None = None
        self._cache: dict[int, list[dict[str, Any]]] = {}
        self.decoded = 0

    def _load_views(self) -> None:
        if self._groups is not None:
            return
        diags: list[str] = []
        self._views = _parse(pb.DumpTreeResponse, self._raw.views, "views", diags)
        self._groups = {}
        if self._views is not None:
            self._resolver = StringResolver(self._views.strings)
            for g in self._views.properties:
                self._groups.setdefault(int(g.view_id), g)

    def has_props(self, udid: int | None = None) -> bool:
        self._load_views()
        assert self._groups is not None
        return bool(self._groups) if udid is None else int(udid) in self._groups

    def prop_list(self, udid: int) -> list[dict[str, Any]] | None:
        """strings.property_to_dict for every property of one View (None if absent)."""
        self._load_views()
        assert self._groups is not None
        udid = int(udid)
        if udid in self._cache:
            return self._cache[udid]
        g = self._groups.get(udid)
        if g is None:
            return None
        out = [property_to_dict(self._resolver, p) for p in g.properties]
        self._cache[udid] = out
        self.decoded += 1
        return out

    def props(self, udid: int) -> dict[str, Any] | None:
        """``{name: normalized value}`` for one View (normalize.props_to_map)."""
        plist = self.prop_list(udid)
        return None if plist is None else nz.props_to_map(plist)

    def _compose_window(self, data: bytes | None, acv: int) -> tuple[Any, StringResolver] | None:
        msg = _parse(pb.DumpComposeResponse, data, "compose", [])
        if msg is None:
            return None
        for w in msg.windows:
            if int(w.view_id) == int(acv) and w.HasField("root"):
                return w.root, StringResolver(msg.strings)
        return None

    def slot_params(self, slot_path: str) -> dict[str, str] | None:
        """Raw parameter strings of the slot group at ``"<acv>/<i.j.k>"``."""
        try:
            acv_s, path_s = slot_path.split("/", 1)
            path = [int(i) for i in path_s.split(".")]
        except ValueError:
            return None
        for data in (self._raw.slots, self._raw.compose_sem):
            found = self._compose_window(data, int(acv_s))
            if found is None:
                continue
            node, res = found
            try:
                for i in path:
                    node = node.children[i]
            except IndexError:
                continue
            if node.kind == pb.ComposeNode.COMPOSABLE:
                return {res.get(a.key): res.get(a.value) for a in node.attrs}
        return None

    def sem_attrs(self, acv: int, sem_id: int) -> dict[str, str] | None:
        """Raw semantics attrs of ``sem:<acv>:<id>``."""
        for data in (self._raw.compose_sem, self._raw.slots):
            found = self._compose_window(data, acv)
            if found is None:
                continue
            root, res = found
            stack = [root]
            while stack:
                n = stack.pop()
                if n.kind == pb.ComposeNode.SEMANTICS and int(n.id) == int(sem_id):
                    return {res.get(a.key): res.get(a.value) for a in n.attrs}
                stack.extend(n.children)
        return None


# --------------------------------------------------------------------------- #
# The builder
# --------------------------------------------------------------------------- #
class _Builder:
    def __init__(self, raw: RawCapture) -> None:
        self.raw = raw
        self.diags: list[str] = []
        self.nodes: dict[str, UNode] = {}
        self.kids: dict[str, list[str]] = defaultdict(list)  # ui tree
        self.parent: dict[str, str | None] = {}
        self.view_kids: dict[str, list[str]] = defaultdict(list)
        self.view_roots: list[str] = []
        self.sem_kids: dict[str, list[str]] = defaultdict(list)
        self.sem_roots: list[str] = []
        self.acv_sems: dict[int, list[str]] = defaultdict(list)
        self.sem_raw: dict[str, dict[str, str]] = {}
        self.view_text: dict[str, str] = {}
        self.slots: list[_S] = []
        self.acv_slot_roots: dict[int, list[int]] = defaultdict(list)
        self.primary: dict[str, int] = {}  # sem key -> slot idx (emitter link)
        self.a11y: list[_A] = []
        self.a11y_roots: list[int] = []
        self.a11y_by_target: dict[str, _A] = {}
        self.aliases: dict[str, str] = {}
        self.acvs: set[int] = set()
        self.webviews: set[int] = set()
        self.window_of_root: dict[int, str] = {}

    # ------------------------------------------------------------------ views
    def build_views(self) -> None:
        wins = _parse(pb.GetWindowsResponse, self.raw.windows, "windows", self.diags)
        views = _parse(pb.DumpTreeResponse, self.raw.views, "views", self.diags)
        if views is None:
            return
        res = StringResolver(views.strings)
        flag_props = self._flag_props(views, res)
        order = [int(r) for r in wins.root_ids] if wins is not None else []
        roots = list(views.roots)
        z_of: dict[int, int] = {rid: z for z, rid in enumerate(order)}
        extra = len(order)
        for r in roots:
            if int(r.id) not in z_of:
                z_of[int(r.id)] = extra
                extra += 1
        roots.sort(key=lambda r: z_of[int(r.id)])
        for r in roots:
            key = self._add_view(r, None, None, res, flag_props)
            if key is None:
                continue
            node = self.nodes[key]
            node.z = z_of[int(r.id)]
            self.view_roots.append(key)
            self.window_of_root[int(r.id)] = key
            self.aliases[window_key(int(r.id))] = key

    def _flag_props(self, views: Any, res: StringResolver) -> dict[int, dict[str, Any]]:
        """visibility/enabled/clickable/longClickable per view, peeked by string id."""
        want = {}
        for e in views.strings.entries:
            if e.str in _FLAG_PROPS:
                want[e.id] = e.str
        out: dict[int, dict[str, Any]] = {}
        if not want:
            return out
        P = pb.Property
        for g in views.properties:
            vals: dict[str, Any] = {}
            for p in g.properties:
                name = want.get(p.name)
                if name is None:
                    continue
                if p.type in (P.STRING, P.INT_ENUM, P.OBJECT):
                    vals[name] = res.get(p.str_value)
                elif p.type == P.BOOLEAN:
                    vals[name] = bool(p.int32_value)
            if vals:
                out[int(g.view_id)] = vals
        return out

    def _add_view(self, n: Any, parent: str | None, clip: list[int] | None,
                  res: StringResolver, flag_props: Mapping[int, Mapping[str, Any]]) -> str | None:
        udid = int(n.id)
        key = view_key(udid)
        if key in self.nodes:
            self.diags.append(f"duplicate View id {udid}; the second copy was merged into the first")
            for c in n.children:
                self._add_view(c, key, self.nodes[key].b, res, flag_props)
            return None
        cls = res.get(n.class_name) or "View"
        pkg = res.opt(n.package_name)
        declared = _rect(n.bounds)
        b = _clip(declared, clip)
        facet: dict[str, Any] = {"class": cls}
        if pkg:
            facet["qualified"] = f"{pkg}.{cls}"
        lres = nz.resource_str(_res_dict(res, n.layout_resource))
        if lres:
            facet["layout_res"] = lres
        if n.bounds.HasField("render"):
            q = n.bounds.render
            facet["quad"] = [q.x0, q.y0, q.x1, q.y1, q.x2, q.y2, q.x3, q.y3]
        flags: set[str] = set()
        if n.flags & pb.ViewNode.IS_WEBVIEW:
            flags.add("webview")
            self.webviews.add(udid)
        fp = flag_props.get(udid) or {}
        vis = fp.get("visibility")
        if vis is not None and str(vis).lower() not in ("visible", "0"):
            flags.add("hidden")
        if fp.get("enabled") is False:
            flags.add("disabled")
        if fp.get("clickable"):
            flags.add("click")
        if fp.get("longClickable"):
            flags.add("longclick")
        if cls == ACV_CLASS:
            self.acvs.add(udid)
        node = UNode(key=key, kind="view", ids={"view": udid}, b=b, facets={"view": facet},
                     conf={"view": "exact"}, flags=sorted(flags))
        if declared != b:
            node.declared_b = declared
        node.rid = res.opt(n.resource.name) or res.opt(n.view_id_name)
        text = res.opt(n.text_value)
        if text:
            self.view_text[key] = text
        self.nodes[key] = node
        self.parent[key] = parent
        if parent is not None:
            self.kids[parent].append(key)
            self.view_kids[parent].append(key)
        for c in n.children:
            self._add_view(c, key, b, res, flag_props)
        return key

    # ------------------------------------------------------------------ compose
    def build_compose(self) -> None:
        sem_msg = _parse(pb.DumpComposeResponse, self.raw.compose_sem, "compose_sem", self.diags)
        slot_msg = _parse(pb.DumpComposeResponse, self.raw.slots, "slots", self.diags)
        sem_src = sem_msg if sem_msg is not None and len(sem_msg.windows) else slot_msg
        screen_px = any("bounds=screen" in (m.diagnostics or "")
                        for m in (sem_msg, slot_msg) if m is not None)
        sem_windows = self._compose_windows(sem_src, pb.ComposeNode.SEMANTICS)
        slot_src = slot_msg if slot_msg is not None else sem_msg
        slot_windows = self._compose_windows(slot_src, pb.ComposeNode.COMPOSABLE)
        acv_order = list(dict.fromkeys(list(sem_windows) + list(slot_windows)))
        for acv in acv_order:
            self.acvs.add(acv)
            host = view_key(acv)
            if host not in self.nodes:
                if not self.view_roots:
                    self.diags.append(f"compose window of view:{acv} dropped: no View tree")
                    continue
                self.diags.append(f"compose window of view:{acv} has no View; "
                                  f"grafted under {self.view_roots[0]}")
                host = self.view_roots[0]
            sem_nodes, sem_res = sem_windows.get(acv, ([], None))
            slot_nodes, slot_res = slot_windows.get(acv, ([], None))
            probes = [_rect(n.bounds) for n in sem_nodes] + [_rect(n.bounds) for n, _ in slot_nodes]
            dx, dy = self._compose_shift(acv, host, probes, screen_px)
            clip = self.nodes[host].b
            new_roots = []
            for s in sem_nodes:
                k = self._add_sem(s, acv, host, None, clip, sem_res, dx, dy)
                if k is not None:
                    new_roots.append(k)
            # semantics roots come first under the ACV, before its View children
            if new_roots:
                self.kids[host] = new_roots + [k for k in self.kids[host] if k not in new_roots]
            self.sem_roots.extend(new_roots)
            for s, path in slot_nodes:
                idx = self._add_slot(s, acv, None, path, 0, slot_res, dx, dy)
                self.acv_slot_roots[acv].append(idx)

    def _compose_windows(self, msg: Any, kind: int) -> dict[int, tuple[list, StringResolver]]:
        """``{acv: ([(node, path)] or [node], resolver)}``: the ``kind`` children of
        each window's synthetic root (the root itself is folded into the ACV)."""
        out: dict[int, tuple[list, StringResolver]] = {}
        if msg is None:
            return out
        res = StringResolver(msg.strings)
        for w in msg.windows:
            if not w.HasField("root"):
                continue
            acv = int(w.view_id)
            root = w.root
            synthetic = root.kind == pb.ComposeNode.COMPOSABLE and (
                int(root.id) == acv or res.get(root.name) == ACV_CLASS)
            if synthetic:
                picked = [(c, (i,)) for i, c in enumerate(root.children) if c.kind == kind]
            else:
                picked = [(root, ())] if root.kind == kind else []
            if kind == pb.ComposeNode.SEMANTICS:
                out.setdefault(acv, ([], res))[0].extend(n for n, _ in picked)
            else:
                out.setdefault(acv, ([], res))[0].extend(picked)
        return out

    def _compose_shift(self, acv: int, host: str, probes: Sequence[list[int]],
                       screen_px: bool) -> tuple[int, int]:
        """(dx, dy) that turns this ACV's window-relative Compose bounds into screen
        px (CO4): the window's origin, when the first sized root (semantics, else
        slot group) lies outside the ACV but inside it once shifted. (0, 0) when the
        agent says it already reports screen px."""
        r = next((p for p in probes if _area(p)), None)
        if screen_px or r is None:
            return 0, 0
        root_key = self._window_root(host)
        if root_key is None:
            return 0, 0
        ox, oy = self.nodes[root_key].b[0], self.nodes[root_key].b[1]
        if ox == 0 and oy == 0:
            return 0, 0
        acv_box = self.nodes[host].declared_b or self.nodes[host].b
        if _contains(acv_box, r) or not _contains(acv_box, _shift(r, ox, oy)):
            return 0, 0
        self.diags.append(f"compose bounds of view:{acv} were window-relative; "
                          f"shifted by ({ox:+d},{oy:+d}) to screen px (CO4)")
        return ox, oy

    def _window_root(self, key: str) -> str | None:
        seen = set()
        while key is not None and key not in seen:
            seen.add(key)
            p = self.parent.get(key)
            if p is None:
                return key if key in self.view_roots else None
            key = p
        return None

    def _add_sem(self, n: Any, acv: int, parent: str, sem_parent: str | None,
                 clip: list[int] | None, res: StringResolver, dx: int, dy: int) -> str | None:
        sid = int(n.id)
        key = sem_key(acv, sid)
        if key in self.nodes:
            self.diags.append(f"duplicate semantics id {acv}:{sid}; the second copy was "
                              f"merged into the first")
            for c in n.children:
                if c.kind == pb.ComposeNode.SEMANTICS:
                    self._add_sem(c, acv, key, key, self.nodes[key].b, res, dx, dy)
            return None
        attrs = {res.get(a.key): res.get(a.value) for a in n.attrs}
        values, actions = nz.compose_attrs_brief(attrs)
        facet: dict[str, Any] = {}
        if values:
            facet["attrs"] = values
        if actions:
            facet["actions"] = actions
        b = _shift(_rect(n.bounds), dx, dy)
        node = UNode(key=key, kind="compose", ids={"sem": f"{acv}:{sid}"}, b=b,
                     facets={"compose": facet}, conf={"compose": "exact"})
        tag = attrs.get("TestTag")
        if tag:
            node.tag = tag
        self.sem_raw[key] = attrs
        self.nodes[key] = node
        self.parent[key] = parent
        self.kids[parent].append(key)
        if sem_parent is not None:
            self.sem_kids[sem_parent].append(key)
        self.acv_sems[acv].append(key)
        for c in n.children:
            if c.kind == pb.ComposeNode.SEMANTICS:
                self._add_sem(c, acv, key, key, b, res, dx, dy)
        return key

    def _add_slot(self, n: Any, acv: int, parent: int | None, path: tuple[int, ...],
                  depth: int, res: StringResolver, dx: int, dy: int) -> int:
        idx = len(self.slots)
        raw = {res.get(a.key): res.get(a.value) for a in n.attrs}
        name = res.get(n.name) or "?"
        src = res.opt(n.source)
        s = _S(idx=idx, acv=acv, name=name, src=src, box=_shift(_rect(n.bounds), dx, dy),
               raw=raw, path=path, parent=parent, depth=depth,
               render_node_id=int(n.render_node_id))
        s.origin = nz.origin_of(name, src)
        mods = raw.get("modifiers") or raw.get("modifier") or ""
        m = _TESTTAG_RE.search(mods)
        if m:
            s.tag = m.group(1).strip()
        if "key" in raw:
            k = nz.compose_value("key", raw["key"])
            if k:
                s.key = anchors.short_label(k)
        self.slots.append(s)
        for i, c in enumerate(n.children):
            if c.kind != pb.ComposeNode.COMPOSABLE:
                continue
            cidx = self._add_slot(c, acv, idx, path + (i,), depth + 1, res, dx, dy)
            s.children.append(cidx)
        return idx

    # ------------------------------------------------------------------ interop
    def reparent_interop(self) -> None:
        moved = 0
        for acv in sorted(self.acvs):
            host = view_key(acv)
            if host not in self.nodes or not self.acv_sems.get(acv):
                continue
            for handler in list(self.view_kids.get(host, ())):
                hnode = self.nodes[handler]
                if (hnode.facets.get("view") or {}).get("class") != VIEWS_HANDLER_CLASS:
                    continue
                for holder in list(self.view_kids.get(handler, ())):
                    hb = self.nodes[holder].declared_b or self.nodes[holder].b
                    best = None
                    for sk in self.acv_sems[acv]:
                        sb = self.nodes[sk].b
                        if _area(sb) and _contains(sb, hb) and (
                                best is None or _area(sb) < _area(self.nodes[best].b)):
                            best = sk
                    node = self.nodes[holder]
                    node.flags = sorted(set(node.flags) | {"interop"})
                    if best is None:
                        continue
                    self.kids[handler].remove(holder)
                    self.kids[best].append(holder)
                    self.parent[holder] = best
                    node.conf["ui"] = "inferred"
                    moved += 1
        if moved:
            self.diags.append(f"{moved} AndroidView subtree(s) placed under the semantics node "
                              f"that contains them (interop, inferred)")

    # ------------------------------------------------------------------ a11y
    def build_a11y(self) -> None:
        msg = _parse(pb.DumpA11yResponse, self.raw.a11y, "a11y", self.diags)
        if msg is None:
            return
        res = StringResolver(msg.strings)
        for w in msg.windows:
            if not w.HasField("root"):
                continue
            root_view = int(w.root_view_id)
            self.a11y_roots.append(self._flatten(w.root, None, (0,), root_view, res))
        for a in self.a11y:
            a.spoken = self._spoken(a)
        self._join()

    def _flatten(self, n: Any, parent: int | None, path: tuple[int, ...], root_view: int,
                 res: StringResolver) -> int:
        idx = len(self.a11y)
        txt = {f: v for f in _A11Y_TEXTS if (v := res.opt(getattr(n, f)))}
        bools = {f for f in _A11Y_BOOLS if getattr(n, f)}
        a = _A(idx=idx, host=int(n.host_view_id), virt=int(n.virtual_id), rect=_rect(n.bounds),
               parent=parent, path=path, root_view=root_view, txt=txt, bools=bools, msg=n,
               res=res)
        self.a11y.append(a)
        for i, c in enumerate(n.children):
            a.children.append(self._flatten(c, idx, path + (i,), root_view, res))
        return idx

    @staticmethod
    def _own(a: _A) -> str | None:
        return a.txt.get("content_description") or a.txt.get("text") or \
            a.txt.get("state_description")

    def _spoken(self, a: _A) -> str | None:
        """What TalkBack speaks (RO1): contentDescription > text > stateDescription,
        and a focusable node without its own is spoken from its non-focusable
        descendants."""
        own = self._own(a)
        if own or not a.focusable:
            return own
        parts: list[str] = []

        def collect(i: int) -> None:
            for c in self.a11y[i].children:
                child = self.a11y[c]
                if child.focusable:
                    continue
                o = self._own(child)
                if o:
                    parts.append(o)
                else:
                    collect(c)
        collect(a.idx)
        return ", ".join(parts) or None

    def _fallback_windows(self) -> tuple[set[int], bool, set[int]]:
        """The ID1 detector: windows whose a11y ids cannot be trusted, whether
        duplicate pairs were seen, and the windows with virtual nodes on plain
        Views (where the virtual flag itself is wrong)."""
        count: dict[tuple[int, int], int] = defaultdict(int)
        for a in self.a11y:
            if a.host:
                count[(a.host, a.virt)] += 1
        dup = {p for p, c in count.items() if c > 1}
        providers = {a.host for a in self.a11y if a.virt == -1 and a.txt.get("provider_class")}
        bad: set[int] = set()
        implausible: set[int] = set()
        for a in self.a11y:
            if a.host and (a.host, a.virt) in dup:
                bad.add(a.root_view)
            if a.virt != -1 and a.host and self._implausible_host(a.host, providers):
                bad.add(a.root_view)
                implausible.add(a.root_view)
        return bad, bool(dup), implausible

    def _implausible_host(self, host: int, providers: set[int]) -> bool:
        key = view_key(host)
        if key not in self.nodes or host in self.acvs or host in self.webviews:
            return False
        if host in providers:
            return False
        return bool(self.view_kids.get(key))

    def _join(self) -> None:
        bad, dup, self.implausible = self._fallback_windows()
        pair_count: dict[tuple[int, int], int] = defaultdict(int)
        for a in self.a11y:
            pair_count[(a.host, a.virt)] += 1
        if bad:
            self.diags.append(ID1_DUPLICATES if dup else ID1_IMPLAUSIBLE)
        self._index_ui()
        for r in self.a11y_roots:
            root = self.a11y[r]
            if root.root_view in bad:
                self._match_by_bounds(root)
            else:
                self._match_exact(root)
        # a11y-only nodes, in a11y pre-order (parents first)
        for a in self._a11y_preorder():
            if a.target is not None:
                continue
            fallback = a.root_view in bad
            unique = a.host != 0 and pair_count[(a.host, a.virt)] == 1
            if fallback or not unique:
                key = a11y_path_key(a.root_view, a.path)
            else:
                key = a11y_key(a.host, a.virt)
            if key in self.nodes:  # cannot happen with path keys; be safe
                key = a11y_path_key(a.root_view, a.path)
            parent = self._a11y_parent_target(a)
            node = UNode(key=key, kind="a11y", ids={"a11y": f"{a.host}:{a.virt}"},
                         b=list(a.rect))
            # in an ID1 window even an a11y-only node's identity is a guess
            a.conf = "inferred" if fallback else "exact"
            if parent in self.nodes and "webview" in self.nodes[parent].flags:
                node.flags = ["webview"]
            self.nodes[key] = node
            self.parent[key] = parent
            if parent is not None:
                self.kids[parent].append(key)
            a.target = key
            self.a11y_by_target[key] = a
        # aliases and facets
        packed_to_target = {}
        for a in self.a11y:
            packed_to_target.setdefault(a.packed, a.target)
        for a in self.a11y:
            node = self.nodes[a.target]
            if node.kind != "a11y":
                if a.root_view in bad:
                    self.aliases[a11y_path_key(a.root_view, a.path)] = a.target
                elif a.host and pair_count[(a.host, a.virt)] == 1:
                    self.aliases[a11y_key(a.host, a.virt)] = a.target
            node.facets["a11y"] = self._a11y_facet(
                a, node, packed_to_target if a.root_view not in bad else None)
            node.conf["a11y"] = a.conf
            node.ids["a11y"] = f"{a.host}:{a.virt}"

    def _a11y_preorder(self) -> Iterator[_A]:
        stack = list(reversed(self.a11y_roots))
        while stack:
            i = stack.pop()
            yield self.a11y[i]
            stack.extend(reversed(self.a11y[i].children))

    def _a11y_parent_target(self, a: _A) -> str | None:
        if a.parent is not None and self.a11y[a.parent].target is not None:
            return self.a11y[a.parent].target
        host = view_key(a.host)
        if host in self.nodes:
            return host
        root = self.window_of_root.get(a.root_view)
        if root is not None:
            return root
        return self.view_roots[0] if self.view_roots else None

    def _assign(self, a: _A, key: str, conf: str) -> None:
        a.target = key
        a.conf = conf
        self.a11y_by_target[key] = a

    def _match_exact(self, root: _A) -> None:
        stack = [root.idx]
        unresolved: list[_A] = []
        while stack:
            a = self.a11y[stack.pop()]
            stack.extend(reversed(a.children))
            if not a.host:
                unresolved.append(a)
                continue
            key = view_key(a.host) if a.virt == -1 else sem_key(a.host, a.virt)
            if key in self.nodes and key not in self.a11y_by_target:
                self._assign(a, key, "exact")
        for a in unresolved:  # host 0: the agent could not resolve the backing View
            win = self.window_of_root.get(a.root_view)
            best = None
            for k in self._grid_for(win).centers_in(a.rect) if win else ():
                if k in self.a11y_by_target:
                    continue
                iou = _iou(a.rect, self.nodes[k].b)
                if iou >= IOU_MIN and (best is None or iou > best[0]):
                    best = (iou, k)
            if best is not None:
                self._assign(a, best[1], "inferred")

    # ---- bounds matching (ID1 fallback) ------------------------------------ #
    def _index_ui(self) -> None:
        """Pre-order intervals, depths and per-window grids of the ui tree so far."""
        self.tin: dict[str, int] = {}
        self.tout: dict[str, int] = {}
        self.udepth: dict[str, int] = {}
        self.grids: dict[str, _Grid] = {}
        t = 0
        for root in self.view_roots:
            grid = self.grids.setdefault(root, _Grid())
            stack: list[tuple[str, int, bool]] = [(root, 0, False)]
            while stack:
                key, depth, done = stack.pop()
                if done:
                    self.tout[key] = t
                    continue
                self.tin[key] = t
                t += 1
                self.udepth[key] = depth
                node = self.nodes[key]
                if node.kind in ("view", "compose") and _area(node.b):
                    grid.add(key, node.b)
                stack.append((key, depth, True))
                for c in reversed(self.kids.get(key, ())):
                    stack.append((c, depth + 1, False))

    def _grid_for(self, win: str | None) -> _Grid:
        return self.grids.get(win) if win is not None and win in self.grids else _Grid()

    def _under(self, anchor: str | None, key: str) -> bool:
        if anchor is None:
            return True
        ta, tk = self.tin.get(anchor), self.tin.get(key)
        return ta is not None and tk is not None and ta < tk < self.tout[anchor]

    def _compat(self, a: _A, key: str, virt_ok: bool) -> int:
        node = self.nodes[key]
        cls = _simple(a.txt.get("class_name")) or ""
        spoken = a.txt.get("text") or a.txt.get("content_description")
        score = 0
        clickable = "clickable" in a.bools
        if node.kind == "view":
            if a.virt == -1 and a.host == node.ids.get("view"):
                score += 3
            vcls = (node.facets.get("view") or {}).get("class") or ""
            if cls and (cls == vcls or (cls != "View" and cls in vcls)):
                score += 1
            vt = self.view_text.get(key)
            if spoken and vt:
                score += 2 if vt == spoken else -2
            if virt_ok and a.virt == -1:
                score += 1
            node_click = "click" in node.flags
        else:
            attrs = self.sem_raw.get(key) or {}
            if node.ids.get("sem") == f"{a.host}:{a.virt}":
                score += 3
            role = attrs.get("Role")
            if role and _A11Y_CLASS_BY_ROLE.get(role) == cls:
                score += 1
            elif cls == "View" and not role:
                score += 1
            elif cls == "TextView" and attrs.get("Text"):
                score += 1
            text = attrs.get("Text") or attrs.get("ContentDescription")
            if spoken and text:
                score += 2 if text == spoken else (1 if spoken in text else -2)
            if virt_ok and a.virt != -1:
                score += 1
            node_click = "OnClick" in attrs
        if clickable == node_click:
            score += 1 if clickable else 0
        else:
            score -= 1
        return score

    def _match_by_bounds(self, root: _A) -> None:
        win = self.window_of_root.get(root.root_view)
        grid = self._grid_for(win)
        # virtual flags are trustworthy only when hosts are (not the View-screen ID1 shape)
        virt_ok = root.root_view not in self.implausible

        def group(members: list[int], anchor: str | None) -> None:
            pairs = []
            for i in members:
                a = self.a11y[i]
                if not _area(a.rect):
                    continue
                for k in grid.centers_in(a.rect):
                    # the a11y tree nests like the ui tree: stay inside the parent's match
                    if k in self.a11y_by_target or not self._under(anchor, k):
                        continue
                    iou = _iou(a.rect, self.nodes[k].b)
                    if iou < IOU_MIN:
                        continue
                    rank = (round(iou * 20), self._compat(a, k, virt_ok),
                            -self.udepth.get(k, 0), -self.tin.get(k, 0), -i)
                    pairs.append((rank, i, k))
            pairs.sort(reverse=True)
            for _, i, k in pairs:
                a = self.a11y[i]
                if a.target is None and k not in self.a11y_by_target:
                    self._assign(a, k, "inferred")
            for i in members:  # clipped twins: containment within the anchor's subtree
                a = self.a11y[i]
                if a.target is not None or not _area(a.rect) or anchor is None:
                    continue
                best = None
                for k in grid.centers_in(a.rect):
                    if k in self.a11y_by_target or not self._under(anchor, k):
                        continue
                    cont = _containment(a.rect, self.nodes[k].b)
                    compat = self._compat(a, k, virt_ok)
                    if cont >= CONTAIN_MIN and compat >= 1:
                        cand = (cont, compat, _iou(a.rect, self.nodes[k].b), k)
                        if best is None or cand[:3] > best[:3]:
                            best = cand
                if best is not None:
                    self._assign(a, best[3], "inferred")
            for i in members:
                a = self.a11y[i]
                if a.children:
                    group(a.children, a.target or anchor)

        group([root.idx], None)

    # ---- the a11y facet ---------------------------------------------------- #
    def _a11y_facet(self, a: _A, node: UNode,
                    links: Mapping[int, str | None] | None) -> dict[str, Any]:
        n = a.msg
        f: dict[str, Any] = {}
        if a.txt.get("class_name"):
            f["class"] = a.txt["class_name"]
        if a.spoken:
            f["speakable"] = _cap(a.spoken)
        role = a.txt.get("role_description")
        if role:
            f["role"] = role
        flags = [x for x in _A11Y_BOOLS if x in a.bools and x not in
                 ("enabled", "visible_to_user", "is_virtual")]
        if "enabled" not in a.bools:
            flags.append("disabled")
        if "visible_to_user" not in a.bools:
            flags.append("hidden")
        if flags:
            f["flags"] = flags
        acts = []
        for act in n.actions:
            label = a.res.opt(act.label)
            name = action_name(int(act.id), label)
            if name not in nz.BOILERPLATE_ACTIONS:
                acts.append(name)
        if acts:
            f["actions"] = acts
        if a.txt.get("state_description"):
            f["state"] = a.txt["state_description"]
        for src, dst in _A11Y_FACET_TEXTS:
            if a.txt.get(src):
                f[dst] = _cap(a.txt[src], 200)
        if n.HasField("collection_info"):
            ci = n.collection_info
            col: dict[str, Any] = {"rows": ci.row_count, "cols": ci.column_count}
            if ci.item_count >= 0 and ci.item_count != 0:
                col["items"] = ci.item_count
            if ci.hierarchical:
                col["hierarchical"] = True
            if ci.selection_mode:
                col["selection"] = ci.selection_mode
            f["collection"] = col
        if n.HasField("collection_item_info"):
            it = n.collection_item_info
            item: dict[str, Any] = {"row": it.row_index, "col": it.column_index}
            if it.row_span > 1:
                item["row_span"] = it.row_span
            if it.column_span > 1:
                item["col_span"] = it.column_span
            if it.heading:
                item["heading"] = True
            if it.selected:
                item["selected"] = True
            f["item"] = item
        if n.HasField("range_info"):
            ri = n.range_info
            f["range"] = {"type": _RANGE_TYPES.get(ri.type, ri.type), "min": ri.min,
                          "max": ri.max, "cur": ri.current}
        if n.live_region:
            f["live"] = _LIVE.get(n.live_region, n.live_region)
        if n.checked_state == 2:
            f["checked"] = "partial"
        if n.input_type:
            f["input_type"] = n.input_type
        if n.max_text_length > 0:
            f["max_text_length"] = n.max_text_length
        if n.text_size_px:
            f["text_size_px"] = round(float(n.text_size_px), 2)
        if a.txt.get("view_id_resource_name"):
            f["res"] = a.txt["view_id_resource_name"]
        pkg = a.txt.get("package_name")
        if pkg and pkg != self.raw.meta.package:
            f["package"] = pkg
        if a.txt.get("provider_class"):
            f["provider"] = a.txt["provider_class"]
        if node.b != a.rect:
            f["b"] = list(a.rect)
        extras = {}
        for e in n.extras:
            k = a.res.get(e.key)
            v = a.res.get(e.value)
            if not k or (k.endswith("SPANS_START_KEY") and v in ("", "[]")):
                continue
            extras[k] = _cap(v, 120)
        if extras:
            f["extras"] = extras
        if links is not None:
            for field_name, value in (("labeled_by", n.labeled_by), ("label_for", n.label_for),
                                      ("traversal_before", n.traversal_before),
                                      ("traversal_after", n.traversal_after)):
                if value and links.get(int(value)):
                    f[field_name] = links[int(value)]
            if "labeled_by" not in f:
                for value in n.labeled_by_list:
                    if links.get(int(value)):
                        f["labeled_by"] = links[int(value)]
                        break
        return f

    # ------------------------------------------------------------------ slots
    def link_slots(self) -> None:
        """Emitter links (inferred): each semantics node gets the app-code group whose
        box it fills, and the app-code groups nested in that one."""
        by_acv: dict[int, list[_S]] = defaultdict(list)
        for s in self.slots:
            by_acv[s.acv].append(s)
        for acv, slots in by_acv.items():
            sems = self.acv_sems.get(acv) or []
            if not sems:
                continue
            grid = _Grid()
            for k in sems:
                if _area(self.nodes[k].b):
                    grid.add(k, self.nodes[k].b)
            clip = {}
            for k in sems:
                p = self.parent.get(k)
                clip[k] = self.nodes[p].b if p in self.nodes else None
            cands: dict[str, list[_S]] = defaultdict(list)
            for s in slots:
                if not _area(s.box):
                    continue
                for k in grid.centers_in(s.box):
                    sb = self.nodes[k].b
                    if not _contains(s.box, sb):
                        continue
                    vis = _inter(s.box, clip[k]) if clip[k] is not None else s.box
                    if vis is None or _iou(sb, vis) < SLOT_IOU_MIN:
                        continue
                    cands[k].append(s)
            sem_depth = {k: self._sem_depth(k) for k in sems}
            sem_pos = {k: i for i, k in enumerate(sems)}
            used: set[int] = set()
            for k in sorted(cands, key=lambda k: (-sem_depth[k], sem_pos[k])):
                tag = self.nodes[k].tag
                options = sorted(cands[k], key=lambda s: (
                    0 if tag and s.tag == tag else 1, -s.depth, s.idx))
                emitter = next((s for s in options if s.idx not in used), None)
                if emitter is None:
                    continue
                ok = {s.idx for s in cands[k]}
                cur: int | None = emitter.idx
                primary = None
                while cur is not None:
                    s = self.slots[cur]
                    if s.idx not in ok:
                        break
                    if s.origin == "app" and s.idx not in used:
                        primary = s
                        break
                    cur = s.parent
                if primary is None:
                    continue
                used.add(emitter.idx)
                used.add(primary.idx)
                self.primary[k] = primary.idx
                primary.sem.append(k)
        # content links: app groups nested in an emitter link (nearest one wins)
        primary_sem = {idx: k for k, idx in self.primary.items()}
        for s in self.slots:
            if s.origin != "app" or s.idx in primary_sem or not _area(s.box):
                continue
            cur = s.parent
            while cur is not None and cur not in primary_sem:
                cur = self.slots[cur].parent
            if cur is not None:
                s.sem.append(primary_sem[cur])

    def _sem_depth(self, key: str) -> int:
        d = 0
        p = self.parent.get(key)
        while p is not None and self.nodes[p].kind == "compose":
            d += 1
            p = self.parent.get(p)
        return d

    # ------------------------------------------------------------------ derive
    def derive(self) -> None:
        for key, node in self.nodes.items():
            a = self.a11y_by_target.get(key)
            if node.kind == "view" and a is not None and a.conf == "exact" and _area(a.rect):
                # the framework's own visible rect (getBoundsOnScreen clips to parents)
                declared = node.declared_b or node.b
                if _contains(declared, a.rect):
                    node.b = list(a.rect)
                    node.declared_b = None if a.rect == declared else declared
            attrs = self.sem_raw.get(key) if node.kind == "compose" else None
            primary = self.slots[self.primary[key]] if key in self.primary else None
            self._derive_type(node, a, attrs, primary)
            self._derive_text(node, a, attrs)
            self._derive_flags(node, a, attrs)
            if primary is not None:
                node.src = primary.src
                node.conf["slot"] = "inferred"
                if primary.box != node.b and _contains(primary.box, node.b):
                    node.declared_b = list(primary.box)
            if node.declared_b is not None and _area(node.declared_b):
                node.visible = round(_area(node.b) / float(_area(node.declared_b)), 3)
            elif node.declared_b is not None:
                node.visible = None

    def _derive_type(self, node: UNode, a: _A | None, attrs: Mapping[str, str] | None,
                     primary: _S | None) -> None:
        role = None
        if attrs and attrs.get("Role") and _ROLE_WORD.match(attrs["Role"]):
            role = attrs["Role"]
        if role is None and a is not None:
            rd = a.txt.get("role_description")
            if rd and _ROLE_WORD.match(rd):
                role = rd
            else:
                role = ROLE_BY_A11Y_CLASS.get(_simple(a.txt.get("class_name")) or "")
        node.role = role
        typ = role
        if typ is None and primary is not None and primary.origin == "app":
            typ = primary.name if _ROLE_WORD.match(primary.name) else None
        if typ is None and node.kind == "view":
            typ = (node.facets.get("view") or {}).get("class")
        if typ is None and a is not None:
            cls = _simple(a.txt.get("class_name"))
            if cls and cls != "View" and _ROLE_WORD.match(cls):
                typ = cls
        node.type = typ

    def _derive_text(self, node: UNode, a: _A | None, attrs: Mapping[str, str] | None) -> None:
        at = attrs or {}
        ax = a.txt if a is not None else {}
        vt = self.view_text.get(node.key)
        if node.kind == "compose":
            node.text = _cap(at.get("Text") or at.get("EditableText") or ax.get("text"))
        else:
            node.text = _cap(vt or ax.get("text"))
        node.desc = _cap(at.get("ContentDescription") or ax.get("content_description"))
        node.state = _cap(at.get("StateDescription") or ax.get("state_description"))
        node.hint = _cap(ax.get("hint_text"))
        if node.kind == "a11y":
            rid = ax.get("view_id_resource_name")
            node.rid = rid.rsplit("/", 1)[-1] if rid else None
        label = a.spoken if a is not None else None
        if not label:
            label = at.get("ContentDescription") or at.get("Text") or at.get("EditableText") \
                or at.get("StateDescription")
        if not label:
            label = vt
        node.label = _cap(label)

    def _derive_flags(self, node: UNode, a: _A | None, attrs: Mapping[str, str] | None) -> None:
        fs = set(node.flags)
        if a is not None:
            b = a.bools
            if "clickable" in b:
                fs.add("click")
            if "long_clickable" in b:
                fs.add("longclick")
            if "focusable" in b:
                fs.add("focus")
            if "focused" in b:
                fs.add("focused")
            if "scrollable" in b:
                fs.add("scroll")
            if "checkable" in b:
                fs.add("checkable")
            if "checked" in b or a.msg.checked_state == 1:
                fs.add("checked")
            if a.msg.checked_state == 2:
                fs.add("partial")
            if "selected" in b:
                fs.add("selected")
            if "enabled" not in b:
                fs.add("disabled")
            if "heading" in b:
                fs.add("heading")
            if "editable" in b:
                fs.add("edit")
            if "password" in b:
                fs.add("password")
            if "visible_to_user" not in b:
                fs.add("hidden")
            if a.msg.live_region:
                fs.add("live")
            if "is_traversal_group" in b:
                fs.add("tgroup")
        if attrs:
            if "OnClick" in attrs:
                fs.add("click")
            if "OnLongClick" in attrs:
                fs.add("longclick")
            if "Focused" in attrs:
                fs.add("focus")
                if _is_true(attrs.get("Focused")):
                    fs.add("focused")
            if {"ScrollBy", "VerticalScrollAxisRange", "HorizontalScrollAxisRange"} & attrs.keys():
                fs.add("scroll")
            ts = attrs.get("ToggleableState")
            if ts is not None:
                fs.add("checkable")
                if ts.strip().lower() == "on":
                    fs.add("checked")
                elif ts.strip().lower() == "indeterminate":
                    fs.add("partial")
            if _is_true(attrs.get("Selected")):
                fs.add("selected")
            if "Disabled" in attrs:
                fs.add("disabled")
            if "Heading" in attrs:
                fs.add("heading")
            if {"EditableText", "SetText"} & attrs.keys():
                fs.add("edit")
            if "Password" in attrs:
                fs.add("password")
            if {"InvisibleToUser", "HideFromAccessibility"} & attrs.keys():
                fs.add("hidden")
            if "LiveRegion" in attrs:
                fs.add("live")
            if _is_true(attrs.get("IsTraversalGroup")):
                fs.add("tgroup")
        node.flags = _order_flags(fs)

    # ------------------------------------------------------------------ assemble
    def assemble(self) -> Index:
        ui = Tree(roots=list(self.view_roots))
        order: list[str] = []
        for root in self.view_roots:
            stack: list[tuple[str, int]] = [(root, 0)]
            while stack:
                key, depth = stack.pop()
                node = self.nodes[key]
                node.depth = depth
                node.window = root
                node.parent = self.parent.get(key)
                node.children = list(self.kids.get(key, ()))
                if node.children:
                    ui.children[key] = list(node.children)
                order.append(key)
                for c in reversed(node.children):
                    stack.append((c, depth + 1))
        placed = set(order)
        stray = [k for k in self.nodes if k not in placed]
        if stray:
            self.diags.append(f"{len(stray)} node(s) not reachable from a window root were dropped")
            for k in stray:
                self.nodes.pop(k)
        trees = {
            "ui": ui,
            "views": Tree(roots=list(self.view_roots),
                          children={k: list(v) for k, v in self.view_kids.items() if v}),
            "compose": Tree(roots=list(self.sem_roots),
                            children={k: list(v) for k, v in self.sem_kids.items() if v}),
            "a11y": self._a11y_tree(),
        }
        nodes = {k: self.nodes[k] for k in order}
        ix = Index(meta=self.raw.meta, nodes=nodes, trees=trees, reading=[],
                   by_key={}, diagnostics=self.diags)
        anchors.assign_ui_anchors(ix)
        slot_tree = self._slot_nodes(ix)
        trees["slots"] = slot_tree
        by_key = {n.key: nid for nid, n in ix.nodes.items()}
        for alias, target in self.aliases.items():
            if target in ix.nodes and alias not in by_key:
                by_key[alias] = target
        for alias, target in self._legacy_aliases(ix).items():
            by_key.setdefault(alias, target)
        ix.by_key = by_key
        anchors.assign_sels(ix)
        return ix

    def _a11y_tree(self) -> Tree:
        t = Tree()
        for r in self.a11y_roots:
            a = self.a11y[r]
            if a.target is None or a.target not in self.nodes:
                continue
            t.roots.append(a.target)
            stack = [r]
            while stack:
                i = stack.pop()
                cur = self.a11y[i]
                kids = [self.a11y[c].target for c in cur.children
                        if self.a11y[c].target in self.nodes]
                if kids:
                    t.children[cur.target] = kids
                stack.extend(reversed(cur.children))
        return t

    def _slot_nodes(self, ix: Index) -> Tree:
        tree = Tree()
        if not self.slots:
            return tree
        keys: dict[int, str] = {}
        anchor_of: dict[int, str] = {}

        def place(indices: list[int], parent_anchor: str) -> None:
            groups = [self.slots[i] for i in indices]
            segs = anchors.slot_segments([anchors.slot_base(s.name, s.src, s.key)
                                          for s in groups])
            for s, seg in zip(groups, segs):
                anchor_of[s.idx] = f"{parent_anchor}/{seg}"
                place(s.children, anchor_of[s.idx])

        for acv, roots in self.acv_slot_roots.items():
            host = ix.nodes.get(view_key(acv))
            base = host.anchor if host is not None and host.anchor else f"composeview:{acv}"
            place(roots, base)
        seen: set[str] = set()
        for s in self.slots:
            anchor = anchor_of[s.idx]
            key = f"slot:{s.acv}:{anchor_hash(anchor)}"
            n = 1
            while key in seen:  # a 32-bit hash collision; keep keys unique
                key = f"slot:{s.acv}:{anchor_hash(f'{anchor}~{n}')}"
                n += 1
            seen.add(key)
            keys[s.idx] = key
        by_src: dict[tuple[int, str], int] = defaultdict(int)
        for s in self.slots:
            key = keys[s.idx]
            params: dict[str, str] = {}
            mods = None
            for k, v in s.raw.items():
                nv = nz.compose_value(k, v)
                if nv is None:
                    continue
                if k in ("modifier", "modifiers"):
                    mods = nv
                else:
                    params[k] = nv
            facet: dict[str, Any] = {"name": s.name}
            if params:
                facet["params"] = params
            if mods:
                facet["mods"] = mods
            if s.sem:
                facet["sem"] = list(s.sem)
            ids: dict[str, Any] = {"slot_path": f"{s.acv}/" + ".".join(str(i) for i in s.path)}
            if s.src:
                ids["slot"] = f"{s.src}#{by_src[(s.acv, s.src)]}"
                by_src[(s.acv, s.src)] += 1
            if s.render_node_id:
                ids["layer"] = s.render_node_id
            window = None
            if s.sem and s.sem[0] in ix.nodes:
                window = ix.nodes[s.sem[0]].window
            elif view_key(s.acv) in ix.nodes:
                window = ix.nodes[view_key(s.acv)].window
            node = UNode(key=key, kind="slot", window=window, type=s.name, ids=ids,
                         b=list(s.box), src=s.src, origin=s.origin, anchor=anchor_of[s.idx],
                         facets={"slot": facet}, conf={"slot": "exact"},
                         parent=keys[s.parent] if s.parent is not None else None,
                         children=[keys[c] for c in s.children], depth=s.depth)
            text = s.raw.get("text")
            if text:
                node.text = _cap(nz.compose_value("text", text) or text)
            if s.sem:
                node.conf["sem"] = "inferred"
            ix.nodes[key] = node
        for acv, roots in self.acv_slot_roots.items():
            tree.roots.extend(keys[i] for i in roots)
        for s in self.slots:
            if s.children:
                tree.children[keys[s.idx]] = [keys[c] for c in s.children]
        # semantics -> slots links, primary first then nested groups in slot order
        nested: dict[str, list[int]] = defaultdict(list)
        for s in self.slots:
            for k in s.sem:
                if self.primary.get(k) != s.idx:
                    nested[k].append(s.idx)
        for k, idx in self.primary.items():
            if k in ix.nodes:
                linked = [idx] + nested.get(k, [])
                ix.nodes[k].facets.setdefault("compose", {})["slots"] = [keys[i] for i in linked]
        return tree

    def _legacy_aliases(self, ix: Index) -> dict[str, str]:
        """``compose:<id>`` for ids that name exactly one node (ID3-safe)."""
        cands = _legacy_candidates(ix)
        return {k: v[0] for k, v in cands.items() if len(v) == 1}


def _res_dict(res: StringResolver, r: Any) -> dict[str, Any] | None:
    out = {"type": res.opt(r.type), "name": res.opt(r.name), "namespace": res.opt(r.namespace)}
    return out if out["name"] else None


def _legacy_candidates(ix: Index) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for nid, n in ix.nodes.items():
        if n.kind == "compose" and n.ids.get("sem"):
            out[compose_legacy_key(int(str(n.ids["sem"]).split(":")[1]))].append(nid)
        elif n.kind == "view" and (n.facets.get("view") or {}).get("class") == ACV_CLASS:
            out[compose_legacy_key(int(n.ids["view"]))].append(nid)  # the folded root (ID3)
    return out


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def build_index(raw: RawCapture) -> Index:
    """Build the unified, key-space index of one capture (every ``ref`` is None)."""
    b = _Builder(raw)
    b.build_views()
    b.build_compose()
    b.reparent_interop()
    b.build_a11y()
    b.link_slots()
    b.derive()
    return b.assemble()


def apply_refs(ix: Index, refmap: Mapping[str, str]) -> Index:
    """A copy of ``ix`` in ref space: ids rewritten through ``refmap`` (canonical
    key -> ref), ``UNode.ref`` set, and fallback ``sel`` values (a node's key)
    replaced by its ref."""
    out = remap_ids(ix, refmap)
    for n in out.nodes.values():
        if n.ref and n.sel == n.key:
            n.sel = n.ref
    return out


_COMPOSE_LEGACY = re.compile(r"^compose:(-?\d+)$")
_COMPOSE_PAIR = re.compile(r"^compose:(-?\d+):(-?\d+)$")
_COMPOSE_VIEW = re.compile(r"^composeview:(-?\d+)$")


def resolve_key(ix: Index, key: str) -> str:
    """A ref, canonical key or alias to a node id. Also accepts the agent contract's
    spellings ``compose:<acv>:<id>`` and ``composeview:<acv>``, and legacy
    ``compose:<id>``, which must name exactly one node.

    Raises OpError ``ambiguous`` (with the candidate ids) or ``not_found``.
    """
    nid = ix.resolve_id(key)
    if nid is not None:
        return nid
    m = _COMPOSE_PAIR.match(key)
    if m:
        nid = ix.resolve_id(sem_key(int(m.group(1)), int(m.group(2))))
        if nid is not None:
            return nid
    m = _COMPOSE_VIEW.match(key)
    if m:
        nid = ix.resolve_id(view_key(int(m.group(1))))
        if nid is not None:
            return nid
    if _COMPOSE_LEGACY.match(key):
        cands = _legacy_candidates(ix).get(key, [])
        if len(cands) == 1:
            return cands[0]
        if cands:
            raise OpError("ambiguous", f"{key} matches {len(cands)} nodes (one per ComposeView)",
                          hint="Use sem:<acv>:<id> or one of the candidates.",
                          candidates=list(cands))
    raise OpError("not_found", f"no node {key} in this capture")


def legacy_candidates(ix: Index, key: str) -> list[str]:
    """Every node a legacy ``compose:<id>`` could mean (semantics nodes with that id
    in any ComposeView, plus an AndroidComposeView with that View id)."""
    return list(_legacy_candidates(ix).get(key, []))


__all__ = [
    "CONTAIN_MIN",
    "ID1_DUPLICATES",
    "ID1_IMPLAUSIBLE",
    "IOU_MIN",
    "ROLE_BY_A11Y_CLASS",
    "FacetReader",
    "apply_refs",
    "build_index",
    "legacy_candidates",
    "resolve_key",
]
