"""Analyzers: render signals, the accessibility lint adapter, reading order, lint view.

Spec "Capture and Walk", sections 3.8 and 5.9. Analyzers are the one place that
writes ``UNode.issues`` (``{id, sev, evidence, conf}``; the message and fix live
once in ``capture/rules.py``), ``UNode.stop`` and ``Index.reading``.

``analyze(ix, loaded, lint=..., density=..., font_scale=...)`` runs at capture
time, before or after refs are applied. ``loaded`` may be a store
``LoadedCapture``, a ``RawCapture`` (before publish) or None (render signals
only). It reads only the raw facets:

* **Render signals** (``render.*``), from the index geometry plus the stored View
  properties (``visibility``, ``alpha``):
  - ``render.clipped``: exact when the index knows the declared box
    (``declared_b``/``visible``) or a View's layout rect extends past a clipping
    ancestor; inferred when a node's visible rect touches the viewport edge of a
    scrollable ancestor and is under half the median extent of its same-type
    siblings. Evidence names ``clipped_by`` and the ``edge``.
  - ``render.hidden``: visibility invisible/gone, alpha 0, or hidden from
    accessibility.
  - ``render.offscreen``: laid out entirely outside its window or the screen.
  - ``render.zero_size``: has content or actions but no area.

  Only nodes that carry content (a label, text or an action) or contain such nodes
  are reported, and only the topmost node of a hidden or offscreen subtree.
  Content scrolled entirely out of a scroll container is normal and not reported.
* **Accessibility lint** (``a11y.*``): ``a11y_lint.run_lint`` over the stored
  unified a11y tree (``raw/a11y.pb``, Views and Compose alike) with the Compose
  semantics (``raw/compose_sem.pb``) joined for detail, exactly as the live
  ``a11y_lint`` tool runs it. Each finding maps back to its dump node and from
  there to an index node (``_A11yDump``). Contrast runs only for ``lint="full"``
  or ``lint_view(contrast=True)``, reads each window's own stored screenshot, and
  is cached in the capture's derived store.
* **Reading order**: the calibrated TalkBack model (``talkback.reading_order``)
  over the same stored a11y tree, mapped to node ids; sets ``stop`` and
  ``Index.reading``.

``lint_view(ix, loaded, ...)`` is the ``lint`` tool body: grouped by rule (with
template collapse of repeated findings in collection cells), by node, or flat,
under a byte budget with stateless cursors.
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .. import a11y_lint
from ..output import Budget, dumps, json_cost
from . import rules as R
from .model import (
    Index,
    Issue,
    OpError,
    RawCapture,
    UNode,
    a11y_key,
    a11y_path_key,
    sem_key,
    view_key,
    window_key,
)
from .rules import ALIASES, RULES

LINT_MODES = ("tree", "full", "none")
GROUPS = ("rule", "node", "none")
DEFAULT_DENSITY = 420
CONTRAST_RULE = "a11y.contrast.low"
TOUCH_RULE = "a11y.touch_target.small"
DUP_RULE = "a11y.duplicate.label"
CLIPPED = "render.clipped"
HIDDEN = "render.hidden"
OFFSCREEN = "render.offscreen"
ZERO_SIZE = "render.zero_size"
LIKELY_FP = "likely false positive: clipped at scroll edge"
#: bytes kept free for the truncation notice and the next hints
FOOTER_RESERVE = 280
#: lint() defaults (spec 5.9, 7).
LINT_MAX_BYTES = 4000
LINT_LIMIT = 30
PER_RULE = 3
#: bump when the cached lint shape or its input changes (derived/lint.<hash>.json);
#: 2: the unified a11y tree replaced Compose semantics as the lint input
#: 3: evidence ``covered_by`` (a finding under an open dialog) and R12's ``name``
#: 4: R19..R23, R9's section titles, R2 on clear Compose touch areas (lint-and-store)
#: 5: R22/R23 ids in groups of their own, R19 reads what TalkBack says (its review)
LINT_CACHE_VERSION = 5

_ACTION_FLAGS = frozenset({"click", "longclick", "edit", "checkable"})
_EDGE_SLOP = 1
_LABEL_CUT = 32
#: evidence the capture does not keep: what the node itself says (label, class),
#: how the lint worked it out (sampling, label sources searched, the bounds used,
#: the standard behind min_dp, clipped axes: render.clipped reports clipping), and
#: the lint's typed keys of other nodes (``node_ids`` names them as refs instead; R19..R23
#: keep the texts those nodes say: ``twin_label``, ``inner_label``)
_EVIDENCE_DROP = frozenset({"label", "class_name", "announceable_keys", "structural_keys",
                            "fg_lum", "bg_lum", "px_sampled", "fg_fraction", "sample",
                            "text_size_class", "checked", "bounds_source", "standard",
                            "floor_dp", "clipped_axes", "duplicates", "duplicate_of",
                            "carrier", "child", "container", "twin", "inner",
                            "touch_rivals"})
SLIVER_NOTE = "low confidence: only a sliver is visible at the scroll edge"


# --------------------------------------------------------------------------- #
# What the analyzers read from a capture
# --------------------------------------------------------------------------- #
class _Src:
    """A LoadedCapture (store), a RawCapture (before publish) or None, behind one
    read-only surface: ``meta``, ``raw(name)``, ``shot(root)``, ``derived(name)``,
    ``put_derived(name, bytes)``, plus the decoded ``a11y_dump()`` and
    ``compose_dict()``. Missing facets read as None."""

    def __init__(self, loaded: Any) -> None:
        self.obj = loaded
        self.meta = getattr(loaded, "meta", None)
        self._shots: dict[int, Any] = {}
        self._a11y: _A11yDump | None = None
        self._compose: dict[str, Any] | None = None
        self._decoded: set[str] = set()

    def a11y_dump(self) -> _A11yDump | None:
        """The stored accessibility tree, decoded once (None without the facet)."""
        if "a11y" not in self._decoded:
            self._decoded.add("a11y")
            data = self.raw("a11y")
            if data:
                from ..proto import view_inspection_pb2 as pb

                self._a11y = _A11yDump(pb.DumpA11yResponse.FromString(data))
        return self._a11y

    def compose_dict(self) -> dict[str, Any] | None:
        """The stored Compose semantics, shaped by ``strings.dump_compose_to_dict``
        (the lint joins its detail onto the a11y tree), or None."""
        if "compose" not in self._decoded:
            self._decoded.add("compose")
            data = self.raw("compose_sem")
            if data:
                from .. import strings
                from ..proto import view_inspection_pb2 as pb

                self._compose = strings.dump_compose_to_dict(
                    pb.DumpComposeResponse.FromString(data))
        return self._compose

    def raw(self, name: str) -> bytes | None:
        o = self.obj
        if o is None:
            return None
        try:
            v = getattr(o, name, None) if isinstance(o, RawCapture) else o.raw(name)
        except (AttributeError, KeyError, OSError, ValueError):
            return None
        return bytes(v) if v else None

    def shot(self, root: int) -> Any:
        """The window's ``Screenshot`` message, or None."""
        root = int(root)
        if root in self._shots:
            return self._shots[root]
        o = self.obj
        v: Any = None
        if o is not None:
            try:
                v = o.shots.get(root) if isinstance(o, RawCapture) else o.shot(root)
            except (AttributeError, KeyError, OSError, ValueError):
                v = None
        if v is not None and not hasattr(v, "SerializeToString"):
            from ..proto import view_inspection_pb2 as pb

            data = bytes(v)
            v = pb.Screenshot.FromString(data) if data else None
        if v is not None and not v.data:
            v = None
        self._shots[root] = v
        return v

    def derived(self, name: str) -> bytes | None:
        fn = getattr(self.obj, "derived", None)
        if fn is None or isinstance(self.obj, RawCapture):
            return None
        try:
            return fn(name)
        except (KeyError, OSError, ValueError):
            return None

    def put_derived(self, name: str, data: bytes) -> None:
        fn = getattr(self.obj, "put_derived", None)
        if fn is None or isinstance(self.obj, RawCapture):
            return
        try:
            fn(name, data)
        except OSError:
            pass  # a cache write failure only costs a recompute


def _density(ix: Index, src: _Src | None = None) -> int:
    for meta in (ix.meta, src.meta if src else None):
        dpi = ((getattr(meta, "device", None) or {}).get("dpi")) if meta else None
        if dpi:
            return int(dpi)
    return DEFAULT_DENSITY


def _font_scale(ix: Index) -> float:
    fs = ((getattr(ix.meta, "device", None) or {}).get("font_scale")) if ix.meta else None
    return float(fs) if fs else 1.0


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def _rect(b: Any) -> tuple[int, int, int, int] | None:
    if not b or len(b) < 4:
        return None
    return int(b[0]), int(b[1]), int(b[2]), int(b[3])


def _area(r: tuple[int, int, int, int] | None) -> int:
    return max(0, r[2]) * max(0, r[3]) if r else 0


def _inter(a: tuple[int, int, int, int], b: tuple[int, int, int, int]
           ) -> tuple[int, int, int, int]:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return x0, y0, max(0, x1 - x0), max(0, y1 - y0)


def _contains(outer: tuple[int, int, int, int], inner: tuple[int, int, int, int],
              slop: int = _EDGE_SLOP) -> bool:
    return (inner[0] >= outer[0] - slop and inner[1] >= outer[1] - slop
            and inner[0] + inner[2] <= outer[0] + outer[2] + slop
            and inner[1] + inner[3] <= outer[1] + outer[3] + slop)


def _outside(outer: tuple[int, int, int, int], r: tuple[int, int, int, int]) -> bool:
    """``r`` has no overlap with ``outer`` (touching edges do not overlap)."""
    return (r[0] >= outer[0] + outer[2] or r[0] + r[2] <= outer[0]
            or r[1] >= outer[1] + outer[3] or r[1] + r[3] <= outer[1])


def _edges_past(outer: tuple[int, int, int, int], r: tuple[int, int, int, int]) -> list[str]:
    """Which sides of ``r`` stick out of ``outer``."""
    out = []
    if r[1] < outer[1] - _EDGE_SLOP:
        out.append("top")
    if r[1] + r[3] > outer[1] + outer[3] + _EDGE_SLOP:
        out.append("bottom")
    if r[0] < outer[0] - _EDGE_SLOP:
        out.append("left")
    if r[0] + r[2] > outer[0] + outer[2] + _EDGE_SLOP:
        out.append("right")
    return out


def _touching(outer: tuple[int, int, int, int], r: tuple[int, int, int, int],
              axes: set[str]) -> str | None:
    """The viewport edge of ``outer`` that ``r`` (inside it) touches along ``axes``."""
    if "v" in axes:
        if abs((r[1] + r[3]) - (outer[1] + outer[3])) <= _EDGE_SLOP:
            return "bottom"
        if abs(r[1] - outer[1]) <= _EDGE_SLOP:
            return "top"
    if "h" in axes:
        if abs((r[0] + r[2]) - (outer[0] + outer[2])) <= _EDGE_SLOP:
            return "right"
        if abs(r[0] - outer[0]) <= _EDGE_SLOP:
            return "left"
    return None


def _scroll_axes(n: UNode) -> set[str]:
    """Axes a scroll container scrolls on; both when nothing says."""
    axes: set[str] = set()
    attrs = (n.facets.get("compose") or {}).get("attrs") or {}
    for k in attrs:
        if k.startswith("VerticalScrollAxisRange"):
            axes.add("v")
        elif k.startswith("HorizontalScrollAxisRange"):
            axes.add("h")
    cls = str((n.facets.get("view") or {}).get("class") or n.type or "")
    if "Horizontal" in cls or cls in ("ViewPager", "ViewPager2"):
        axes.add("h")
    elif cls.endswith("ScrollView") or cls in ("ListView", "GridView", "ExpandableListView"):
        axes.add("v")
    af = n.facets.get("a11y") or {}
    for a in af.get("actions") or ():
        name = str(a.get("name") if isinstance(a, Mapping) else a)
        if name in ("SCROLL_UP", "SCROLL_DOWN"):
            axes.add("v")
        elif name in ("SCROLL_LEFT", "SCROLL_RIGHT"):
            axes.add("h")
    if not axes:  # a RecyclerView says only SCROLL_FORWARD; its CollectionInfo tells
        coll = af.get("collection") if isinstance(af.get("collection"), Mapping) else {}
        rows, cols = coll.get("rows"), coll.get("cols")
        if isinstance(rows, int) and isinstance(cols, int):
            if cols == 1 and rows != 1:
                axes.add("v")
            elif rows == 1 and cols != 1:
                axes.add("h")
    return axes or {"v", "h"}


def _has_content(n: UNode) -> bool:
    return bool(n.label or n.text or n.desc or (set(n.flags) & _ACTION_FLAGS))


def _node_rect(n: UNode) -> tuple[int, int, int, int] | None:
    return _rect(n.declared_b) or _rect(n.b)


# --------------------------------------------------------------------------- #
# Render signals
# --------------------------------------------------------------------------- #
def _visibility_props(src: _Src) -> dict[int, dict[str, Any]]:
    """``{view udid: {"visibility": str, "alpha": float}}`` from ``raw/views.pb``."""
    data = src.raw("views")
    if not data:
        return {}
    from ..proto import view_inspection_pb2 as pb

    try:
        resp = pb.DumpTreeResponse.FromString(data)
    except Exception:  # noqa: BLE001 - a corrupt facet must not stop the analysis
        return {}
    names = {e.id: e.str for e in resp.strings.entries}
    wanted = {i: s for i, s in names.items() if s in ("visibility", "alpha")}
    if not wanted:
        return {}
    out: dict[int, dict[str, Any]] = {}
    for g in resp.properties:
        for p in g.properties:
            name = wanted.get(p.name)
            if name == "visibility":
                if p.str_value:
                    v = names.get(p.str_value, "")
                else:
                    v = {0: "visible", 4: "invisible", 8: "gone"}.get(p.int32_value, "visible")
                out.setdefault(g.view_id, {})["visibility"] = str(v).lower()
            elif name == "alpha":
                out.setdefault(g.view_id, {})["alpha"] = float(p.float_value)
    return out


@dataclass
class _Geo:
    """Per-capture geometry shared by the render rules."""

    ix: Index
    screen: tuple[int, int, int, int] | None
    scroll_anc: dict[str, str | None] = field(default_factory=dict)
    subtree_content: dict[str, bool] = field(default_factory=dict)

    def window_rect(self, n: UNode) -> tuple[int, int, int, int] | None:
        w = self.ix.nodes.get(n.window) if n.window else None
        return _rect(w.b) if w is not None else None


def _geo(ix: Index) -> _Geo:
    screen = None
    dev = (getattr(ix.meta, "device", None) or {}) if ix.meta else {}
    if dev.get("screen") and len(dev["screen"]) >= 2:
        screen = (0, 0, int(dev["screen"][0]), int(dev["screen"][1]))
    g = _Geo(ix, screen)
    order = [n for n, _ in ix.walk("ui")]
    for n in order:
        p = ix.nodes.get(n.parent) if n.parent else None
        if p is None:
            g.scroll_anc[n.id] = None
        elif "scroll" in p.flags:
            g.scroll_anc[n.id] = p.id
        else:
            g.scroll_anc[n.id] = g.scroll_anc.get(p.id)
    for n in reversed(order):
        g.subtree_content[n.id] = _has_content(n) or any(
            g.subtree_content.get(c, False) for c in ix.tree("ui").children.get(n.id, ()))
    return g


def _scrolled_out(g: _Geo, n: UNode, r: tuple[int, int, int, int] | None) -> bool:
    """Inside a scroll container but entirely out of its viewport (or collapsed onto
    its edge): normal scrolled-away content, not an issue."""
    if r is None:
        return False
    s = g.scroll_anc.get(n.id)
    while s is not None:
        vp = _rect(g.ix.nodes[s].b)
        if vp is not None and (_outside(vp, r)
                               or (_area(r) == 0 and _touching(vp, r, {"v", "h"}))):
            return True
        s = g.scroll_anc.get(s)
    return False


def _clipped_exact(g: _Geo, n: UNode) -> Issue | None:
    """A known declared box (from the index) or a layout rect that sticks out of
    a clipping ancestor."""
    ix = g.ix
    declared = _rect(n.declared_b)
    visible_rect = _rect(n.b)
    if declared is not None and n.visible is not None and n.visible < 1.0:
        r = declared
    elif declared is None and visible_rect is not None and n.kind == "view":
        r = visible_rect
    else:
        return None
    if _area(r) == 0:
        return None
    clipper: UNode | None = None
    vis = r
    edges: list[str] = []
    for anc in ix.ancestors(n.id):
        ar = _rect(anc.b)
        if ar is None or _area(ar) == 0:
            continue
        if not _contains(ar, r) and clipper is None:
            clipper = anc
            edges = _edges_past(ar, r)
        vis = _inter(vis, ar)
    if clipper is None:
        return None
    if declared is not None and visible_rect is not None:
        vis = visible_rect
    frac = _area(vis) / _area(r)
    if frac >= 1.0:
        return None
    if frac <= 0.0:
        return None  # nothing visible: scrolled out or offscreen, not "clipped"
    scroll = "scroll" in clipper.flags
    axis_extent = r[3] if (edges and edges[0] in ("top", "bottom")) else r[2]
    vis_extent = vis[3] if (edges and edges[0] in ("top", "bottom")) else vis[2]
    ev: dict[str, Any] = {"visible_px": int(vis_extent), "declared_px": int(axis_extent),
                          "visible": round(frac, 3), "clipped_by": clipper.id,
                          "edge": edges[0] if edges else None}
    if scroll:
        ev["scroll"] = True
    conf = "exact" if (declared is not None or scroll) else "inferred"
    return Issue(CLIPPED, "info" if scroll else "warn", _clean(ev), conf)


def _clipped_inferred(g: _Geo, n: UNode) -> Issue | None:
    """Touches a scroll viewport edge and is far smaller than its look-alike
    siblings (the same type and #rid)."""
    ix = g.ix
    s = g.scroll_anc.get(n.id)
    r = _rect(n.b)
    if s is None or r is None or _area(r) == 0:
        return None
    sn = ix.nodes[s]
    vp = _rect(sn.b)
    if vp is None or not _contains(vp, r):
        return None
    edge = _touching(vp, r, _scroll_axes(sn))
    if edge is None:
        return None
    vertical = edge in ("top", "bottom")
    parent = ix.nodes.get(n.parent) if n.parent else None
    if parent is None:
        return None
    sizes = []
    for c in ix.children_of(parent.id):
        # look-alikes only: a #rid names a part, so a row's #star_click_area is not
        # compared with its #divider (seen live on Thunderbird's message rows)
        if c.id == n.id or c.type != n.type or c.rid != n.rid:
            continue
        cr = _rect(c.b)
        if cr is None or _area(cr) == 0:
            continue
        sizes.append(cr[3] if vertical else cr[2])
    if len(sizes) < 2:
        return None
    med = statistics.median(sizes)
    extent = r[3] if vertical else r[2]
    if extent >= 0.5 * med:
        return None
    ev = {"visible_px": int(extent), "declared_px": int(med), "est": "sibling median",
          "clipped_by": s, "edge": edge, "scroll": True}
    return Issue(CLIPPED, "info", ev, "inferred")


def render_signals(ix: Index, props: Mapping[int, Mapping[str, Any]] | None = None
                   ) -> dict[str, list[Issue]]:
    """``{node id: [render issues]}`` for the ui tree (see the module docstring)."""
    props = props or {}
    g = _geo(ix)
    out: dict[str, list[Issue]] = {}
    # A reported node (or a scrolled-out hidden/offscreen one) covers its subtree:
    # one issue tells the story, so descendants are not reported again.
    suppressed: set[str] = set()
    for n, _depth in ix.walk("ui"):
        if n.parent in suppressed:
            suppressed.add(n.id)
            continue
        if not g.subtree_content.get(n.id):
            continue
        r = _node_rect(n)
        issue: Issue | None = None

        # hidden
        why = None
        conf = "exact"
        vprops = props.get(int(n.ids["view"])) if "view" in n.ids else None
        if vprops:
            if vprops.get("visibility") in ("invisible", "gone"):
                why = f"visibility={vprops['visibility']}"
            elif vprops.get("alpha") == 0.0:
                why = "alpha=0"
        if why is None and "hidden" in n.flags:
            why = "hidden"
        if why is None and "hidden" in ((n.facets.get("a11y") or {}).get("flags") or ()):
            why, conf = "not visible to accessibility", "inferred"
        if why is not None and not _scrolled_out(g, n, r):
            hides = sum(1 for d, _ in ix.walk("ui", n.id) if d.id != n.id and _has_content(d))
            ev: dict[str, Any] = {"why": why}
            if hides:
                ev["hides"] = hides
            issue = Issue(HIDDEN, "info", ev, conf)
        elif why is not None:
            suppressed.add(n.id)
            continue

        # clipped
        if issue is None and _has_content(n):
            issue = _clipped_exact(g, n) or _clipped_inferred(g, n)

        # offscreen
        if issue is None and r is not None and _area(r) > 0 and not n.is_window:
            wr = g.window_rect(n)
            where = None
            if wr is not None and _area(wr) > 0 and _outside(wr, r):
                where = "window"
            elif g.screen is not None and _outside(g.screen, r):
                where = "screen"
            if where and not _scrolled_out(g, n, r):
                issue = Issue(OFFSCREEN, "info", {"rect": list(r), "outside": where})
            elif where:
                suppressed.add(n.id)
                continue

        # zero size
        if (issue is None and r is not None and _area(r) == 0 and _has_content(n)
                and (n.facets.get("view") or {}).get("class") != "ViewStub"
                and not _scrolled_out(g, n, r)):
            issue = Issue(ZERO_SIZE, "info", {"w": r[2], "h": r[3]})

        if issue is not None:
            out.setdefault(n.id, []).append(issue)
            suppressed.add(n.id)
    return out


def _clean(ev: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in ev.items() if v is not None and v != [] and v != {}}


# --------------------------------------------------------------------------- #
# The stored accessibility tree (the input of the lint and the reading order)
# --------------------------------------------------------------------------- #
def _iter_dicts(root: dict[str, Any]) -> Iterable[tuple[dict[str, Any], tuple[int, ...]]]:
    stack: list[tuple[dict[str, Any], tuple[int, ...]]] = [(root, (0,))]
    while stack:
        n, path = stack.pop()
        yield n, path
        kids = n.get("children") or []
        for i in range(len(kids) - 1, -1, -1):
            stack.append((kids[i], path + (i,)))


class _A11yDump:
    """``raw/a11y.pb`` as ``a11y.a11y_to_dict`` shapes it (typed node keys, window
    meta, TalkBack exclusions): the one input of both the lint
    (``a11y_lint.run_lint``) and the reading order (``talkback.reading_order``).
    Neither modifies it, so one decode serves both.

    ``mapper(ix)`` maps one of its node dicts to an index node through, in order:
    the ``a11y:path:`` key (the index builder's alias when a11y ids are not
    unique, ID1), then, only when the ``(host, virtual)`` pair is unique in the
    dump, the ``a11y:`` key, a node whose ``ids.a11y`` names the pair, and
    ``view:<host>`` / ``sem:<host>:<id>``. A duplicated pair is never guessed."""

    def __init__(self, resp: Any) -> None:
        from .. import a11y

        self.data = a11y.a11y_to_dict(resp)
        self.where: dict[int, tuple[int, tuple[int, ...]]] = {}
        self.pairs: Counter = Counter()
        self.by_pair: dict[tuple[Any, Any], list[tuple[int, dict[str, Any]]]] = {}
        self.by_packed: dict[int, dict[str, Any] | None] = {}
        self.has_roots = False
        for wi, w in enumerate(self.data.get("windows") or []):
            root = w.get("root")
            if not root:
                continue
            self.has_roots = True
            root_udid = int(w.get("root_view_id") or root.get("host_view_id") or 0)
            for n, path in _iter_dicts(root):
                self.where[id(n)] = (root_udid, path)
                pair = (n.get("host_view_id"), n.get("virtual_id"))
                self.pairs[pair] += 1
                self.by_pair.setdefault(pair, []).append((wi, n))
                if n.get("id") is not None:
                    packed = int(n["id"])
                    self.by_packed[packed] = None if packed in self.by_packed else n

    def mapper(self, ix: Index) -> Callable[[dict[str, Any]], str | None]:
        a11y_ids: dict[str, str | None] = {}
        for nid, node in ix.nodes.items():
            aid = node.ids.get("a11y")
            if aid is not None:
                a11y_ids[str(aid)] = None if str(aid) in a11y_ids else nid

        def lookup(n: dict[str, Any]) -> str | None:
            root, path = self.where.get(id(n), (0, ()))
            if path:
                nid = ix.resolve_id(a11y_path_key(root, path))
                if nid is not None:
                    return nid
            h, v = n.get("host_view_id"), n.get("virtual_id")
            if h is None or v is None or self.pairs[(h, v)] > 1:
                return None
            nid = ix.resolve_id(a11y_key(h, v)) or a11y_ids.get(f"{h}:{v}")
            if nid is not None:
                return nid
            return ix.resolve_id(view_key(h) if v == -1 else sem_key(h, v))

        return lookup

    def finding_dict(self, f: Any) -> dict[str, Any] | None:
        """The dump node a lint finding is about. A pair the dump repeats (ID1) is
        told apart by the finding's window and bounds, or not at all."""
        node = getattr(f, "node", None) or {}
        cands = self.by_pair.get((node.get("host_view_id"), node.get("virtual_id"))) or []
        if len(cands) > 1:
            win = (getattr(f, "window", None) or {}).get("index")
            bounds = getattr(f, "bounds", None)
            cands = [(wi, n) for wi, n in cands
                     if (win is None or wi == win) and a11y_lint._rect_of(n) == bounds]
        return cands[0][1] if len(cands) == 1 else None


# --------------------------------------------------------------------------- #
# The lint adapter
# --------------------------------------------------------------------------- #
def _evidence(f: Any, nid: str, dump: _A11yDump,
              lookup: Callable[[dict[str, Any]], str | None]) -> dict[str, Any]:
    """The finding's evidence as the capture keeps it. ``node_ids`` names the other
    nodes involved (R12's same-label group, R13's twin), as node ids."""
    ev: dict[str, Any] = {}
    for k, v in (getattr(f, "evidence", None) or {}).items():
        if (k in _EVIDENCE_DROP or k.startswith("has_") or v is None or v is False
                or v == [] or v == {}):
            continue
        if k == "duplicate_of_id":  # R13's other node, named like R12's
            k, v = "node_ids", [v]
        if k in ("children_ids", "node_ids") and isinstance(v, list):
            # packed a11y ids -> node ids (a repeated or unknown id stays as it is)
            v = [(lookup(d) if (d := dump.by_packed.get(x)) is not None else None) or x
                 for x in v]
            v = [x for x in v if x != nid]
            if not v:
                continue
        ev[k] = v
    if f.rule == DUP_RULE and (getattr(f, "evidence", None) or {}).get("label"):
        # the name the nodes share: the node's own label can be its state ("Not selected")
        ev["name"] = f.evidence["label"]
    return ev


@dataclass
class _LintResult:
    issues: list[tuple[str, Issue]] = field(default_factory=list)  # (node id, issue)
    unmapped: list[str] = field(default_factory=list)  # "rule key"
    errors: list[str] = field(default_factory=list)  # rules that raised
    status: str = "ok"


def _map_findings(report: Any, dump: _A11yDump, ix: Index, res: _LintResult,
                  keep: Callable[[str], bool]) -> None:
    lookup = dump.mapper(ix)
    for f in report.findings:
        if not keep(f.rule):
            continue
        d = dump.finding_dict(f)
        nid = lookup(d) if d is not None else None
        if nid is None:
            res.unmapped.append(f"{f.rule} {f.node_key or '?'}")
            continue
        ev = _evidence(f, nid, dump, lookup)
        conf = "inferred" if ev.pop("low_confidence", False) else "exact"
        n = ix.nodes[nid]
        if ev.get("name") is not None and ev["name"] == (n.label or n.text or n.desc):
            ev.pop("name")  # R12's shared name is the node's own label: nothing to add
        cov = (getattr(f, "window", None) or {}).get("covered_by")
        if cov is not None:
            # on a window under an open dialog: the dialog's window, counted apart (_covered)
            ev["covered_by"] = ix.resolve_id(window_key(int(cov))) or window_key(int(cov))
        res.issues.append((nid, Issue(f.rule, f.severity, ev, conf)))
    res.errors.extend(str(d.get("message")) for d in report.diagnostics
                      if d.get("code") == "rule.error")


def _run_lint(src: _Src, conn: Any = None, **kw: Any) -> Any:
    """``a11y_lint.run_lint`` over the stored trees: the unified a11y tree (Views and
    Compose, every window at once, so cross-node rules see the whole screen) with
    the Compose semantics joined for detail. ``conn`` is only asked for window
    screenshots."""
    compose = src.compose_dict()
    return a11y_lint.run_lint(conn, a11y_data=src.a11y_dump().data, compose_data=compose,
                              include_compose=compose is not None, **kw)


def _run_tree_lint(src: _Src, ix: Index, *, density: int, font_scale: float,
                   wcag: bool = False) -> _LintResult:
    """Every tree rule (no pixels)."""
    res = _LintResult()
    dump = src.a11y_dump()
    if dump is None or not dump.has_roots:
        res.status = "no accessibility tree"
        return res
    report = _run_lint(src, density=density, font_scale=font_scale, wcag_mode=wcag,
                       include_contrast=False)
    _map_findings(report, dump, ix, res, lambda rule: rule != CONTRAST_RULE)
    return res


class _StoredShots:
    """The ``conn`` that ``a11y_lint.run_lint`` screenshots windows through: it
    serves each window's own stored screenshot, so contrast samples exactly the
    pixels of the capture. A screenshot without a scale gets its width over the
    window's."""

    def __init__(self, src: _Src, dump: _A11yDump) -> None:
        self.src = src
        self.width = {int(w.get("root_view_id") or 0): a11y_lint._rect_of(w["root"])["w"]
                      for w in dump.data.get("windows") or [] if w.get("root")}

    def screenshot(self, root_id: int = 0, scale: float = 1.0) -> Any:
        from ..proto import view_inspection_pb2 as pb

        resp = pb.ScreenshotResponse()
        shot = self.src.shot(root_id)
        if shot is not None:
            resp.screenshot.CopyFrom(shot)
            w = self.width.get(int(root_id), 0)
            if resp.screenshot.scale <= 0.0 and w > 0:
                resp.screenshot.scale = resp.screenshot.width / w
        return resp


def _run_contrast(src: _Src, ix: Index, *, density: int, font_scale: float,
                  wcag: bool = False) -> _LintResult:
    """The contrast rule, sampling each window's own stored screenshot."""
    res = _LintResult()
    dump = src.a11y_dump()
    if dump is None or not dump.has_roots:
        res.status = "no accessibility tree"
        return res
    report = _run_lint(src, _StoredShots(src, dump), density=density, font_scale=font_scale,
                       wcag_mode=wcag, rules=[CONTRAST_RULE])
    _map_findings(report, dump, ix, res, lambda rule: rule == CONTRAST_RULE)
    sampled = len(report.stats.get("contrast_windows") or ())
    res.status = f"sampled {sampled} window{'s' if sampled != 1 else ''}" if sampled \
        else "unavailable: no screenshot"
    return res


def _lint_cache_name(**opts: Any) -> str:
    blob = json.dumps({"v": LINT_CACHE_VERSION, **opts}, sort_keys=True).encode()
    return f"lint.{hashlib.blake2s(blob, digest_size=4).hexdigest()}.json"


def _cached_lint(src: _Src, ix: Index, kind: str, *, density: int, font_scale: float,
                 wcag: bool) -> _LintResult:
    """A tree or contrast lint run, cached in the capture's derived store by options.
    Cached issues are stored by canonical key, so any id space can read them."""
    name = _lint_cache_name(kind=kind, density=density, font_scale=font_scale, wcag=wcag)
    blob = src.derived(name)
    if blob:
        try:
            d = json.loads(blob)
            res = _LintResult(status=d.get("status", "ok"), unmapped=list(d.get("unmapped") or []))
            for row in d.get("issues") or []:
                nid = ix.resolve_id(row["key"])
                if nid is None:
                    res.unmapped.append(f"{row['issue']['id']} {row['key']}")
                else:
                    res.issues.append((nid, Issue.from_dict(row["issue"])))
            return res
        except (ValueError, KeyError, TypeError):
            pass  # a stale or corrupt cache entry is recomputed
    run = _run_contrast if kind == "contrast" else _run_tree_lint
    res = run(src, ix, density=density, font_scale=font_scale, wcag=wcag)
    rows = [{"key": ix.nodes[nid].key, "issue": iss.to_dict()} for nid, iss in res.issues]
    src.put_derived(name, dumps({"v": LINT_CACHE_VERSION, "status": res.status,
                                 "unmapped": res.unmapped, "issues": rows}).encode("utf-8"))
    return res


def _clip_axes(clipped: Iterable[Issue]) -> set[str]:
    """The axes ``render.clipped`` cuts: its ``edge`` top/bottom is the height, left/right
    the width; an issue that names no edge may cut either."""
    axes: set[str] = set()
    for r in clipped:
        edge = r.evidence.get("edge")
        if edge in ("top", "bottom"):
            axes.add("h")
        elif edge in ("left", "right"):
            axes.add("w")
        else:
            axes |= {"w", "h"}
    return axes


def _small_axes(ev: Mapping[str, Any]) -> set[str]:
    """The axes R2 found below its minimum (``w_dp``/``h_dp`` against ``min_dp``, with
    R2's 1px slack at any density down to 160dpi)."""
    try:
        min_dp = float(ev.get("min_dp") or 48)
    except (TypeError, ValueError):
        min_dp = 48.0
    out: set[str] = set()
    for ax in ("w", "h"):
        v = ev.get(f"{ax}_dp")
        if isinstance(v, (int, float)) and v < min_dp - 0.3:
            out.add(ax)
    return out


def _annotate_touch_fp(pairs: Iterable[tuple[str, Issue]],
                       render: Mapping[str, Iterable[Issue]]) -> list[tuple[str, Issue]]:
    """``pairs`` without the touch-target findings that a ``render.clipped`` node only
    has because of the clip: its visible part is not its size (a 9dp sliver of a 72dp row
    at a scroll edge, Now in Android's Unbookmark half under the bottom bar), so R2 is not
    judged there (G18; it used to be kept as a likely false positive). That is R2's own
    ``info`` (every small axis clipped), or a small axis set that the clip covers; a
    warn/error on an axis the clip leaves whole is kept, as the live lint keeps it. A
    contrast sample on a node clipped at a scroll edge is kept, low confidence: the sliver
    may not show the text."""
    out: list[tuple[str, Issue]] = []
    for nid, iss in pairs:
        clipped = [r for r in render.get(nid, ()) if r.id == CLIPPED]
        if iss.id == TOUCH_RULE and clipped:
            small = _small_axes(iss.evidence)
            if iss.sev == "info" or not small or small <= _clip_axes(clipped):
                continue
        if iss.id == CONTRAST_RULE and any(r.evidence.get("scroll") for r in clipped):
            iss.evidence["note"] = SLIVER_NOTE
            iss.conf = "inferred"
        out.append((nid, iss))
    return out


# --------------------------------------------------------------------------- #
# Reading order
# --------------------------------------------------------------------------- #
def _ordered_stops(dump: Any) -> list[tuple[int, dict[str, Any]]]:
    """``[(order, node dict)]`` for every TalkBack stop, from the calibrated TalkBack
    model (``talkback.reading_order``) over an ``a11y.a11y_to_dict`` dump (window
    order, windows a dialog covers) or a list of window roots."""
    from ..talkback import reading_order as talkback_order

    res = talkback_order(dump)
    return [(e["order"], n) for e, n in zip(res.get("focus_order") or [],
                                            res.get("_nodes") or [])
            if e.get("order") is not None]


def reading_order(ix: Index, loaded: Any) -> tuple[list[tuple[int, str]], list[str]] | None:
    """``([(stop, node id)], diagnostics)`` from the stored a11y tree, or None when
    the capture has no a11y facet. Each TalkBack stop maps to a node through
    ``_A11yDump.mapper``; a duplicated ``(host, virtual)`` pair is never guessed."""
    src = loaded if isinstance(loaded, _Src) else _Src(loaded)
    dump = src.a11y_dump()
    if dump is None:
        return None
    if not dump.has_roots:
        return [], []
    lookup = dump.mapper(ix)
    out: list[tuple[int, str]] = []
    seen: set[str] = set()
    missing = 0
    stops = _ordered_stops(dump.data)
    for order, n in stops:
        nid = lookup(n)
        if nid is None or nid in seen:
            missing += nid is None
            continue
        seen.add(nid)
        out.append((int(order), nid))
    diags = []
    if missing:
        dup = sum(1 for c in dump.pairs.values() if c > 1)
        why = "; a11y ids not unique (agent ID1)" if dup else ""
        diags.append(f"reading: {missing} of {len(stops)} TalkBack stops not mapped to nodes{why}")
    return out, diags


# --------------------------------------------------------------------------- #
# analyze()
# --------------------------------------------------------------------------- #
_DIAG_PREFIXES = ("lint:", "contrast:", "reading:", "tb:")


def _owned(issue_id: str) -> bool:
    return issue_id.startswith(("render.", "a11y.", "tb."))


def _tb_issues(ix: Index, src: _Src, density: int) -> tuple[list[tuple[str, Issue]], list[str]]:
    """The static TalkBack rules (capture/tb.py) over the stored a11y tree; a failure costs
    only these issues and says so."""
    from . import tb

    dump = src.a11y_dump()
    if dump is None or not dump.has_roots:
        return [], []
    try:
        return tb.issues(ix, src.obj, density=density, tbc=tb.TbCapture(dump, ix))
    except Exception as e:  # noqa: BLE001 - a capture never fails on the TalkBack rules
        return [], [f"tb: not run ({type(e).__name__}: {str(e)[:80]})"]


def _issue_sort_key(iss: Issue) -> tuple:
    return (iss.id, -R.SEV_RANK.get(iss.sev, 0))


def analyze(ix: Index, loaded: Any, *, lint: str = "tree", density: int | None = None,
            font_scale: float | None = None) -> None:
    """Write render, lint and TalkBack issues, TalkBack stops and ``Index.reading`` into
    ``ix``.

    ``lint``: ``"tree"`` (tree rules and the static ``tb.*`` rules), ``"full"`` (plus
    contrast from the stored screenshots) or ``"none"``. Issues the analyzers own
    (``render.*``, ``a11y.*`` and ``tb.*``) are replaced, so running it again is
    idempotent. Diagnostics (unmapped findings or stops, contrast status) go to
    ``ix.diagnostics``.
    """
    if lint not in LINT_MODES:
        raise OpError("bad_args", f"lint must be one of {', '.join(LINT_MODES)}")
    src = _Src(loaded)
    density = int(density or _density(ix, src))
    font_scale = float(font_scale or _font_scale(ix))
    diags: list[str] = []

    ro = reading_order(ix, src)
    if ro is not None:
        stops, rdiags = ro
        diags.extend(rdiags)
        for n in ix.nodes.values():
            n.stop = None
        for order, nid in stops:
            ix.nodes[nid].stop = order
        ix.reading = [nid for _, nid in stops]

    render = render_signals(ix, _visibility_props(src))
    lint_pairs: list[tuple[str, Issue]] = []
    if lint != "none":
        res = _run_tree_lint(src, ix, density=density, font_scale=font_scale)
        lint_pairs.extend(res.issues)
        unmapped = list(res.unmapped)
        if res.status != "ok":
            diags.append(f"lint: not run: {res.status}")
        if res.errors:
            diags.append(f"lint: {len(res.errors)} rule errors: {res.errors[0]}")
        if lint == "full":
            name = _lint_cache_name(kind="contrast", density=density, font_scale=font_scale,
                                    wcag=False)
            cres = _run_contrast(src, ix, density=density, font_scale=font_scale)
            lint_pairs.extend(cres.issues)
            unmapped.extend(cres.unmapped)
            diags.append(f"contrast: {cres.status}")
            rows = [{"key": ix.nodes[nid].key, "issue": i.to_dict()} for nid, i in cres.issues]
            src.put_derived(name, dumps({"v": LINT_CACHE_VERSION, "status": cres.status,
                                         "unmapped": cres.unmapped,
                                         "issues": rows}).encode("utf-8"))
        if unmapped:
            diags.append(f"lint: {len(unmapped)} findings not mapped to nodes: "
                         + ", ".join(unmapped[:3]) + (" …" if len(unmapped) > 3 else ""))
        lint_pairs = _annotate_touch_fp(lint_pairs, render)
        tb_pairs, tb_diags = _tb_issues(ix, src, density)
        lint_pairs.extend(tb_pairs)
        diags.extend(tb_diags)
        from .tb import cover_lint

        cover_lint(lint_pairs)  # findings under a same-window overlay: counted apart

    for n in ix.nodes.values():
        if n.issues:
            n.issues = [i for i in n.issues if not _owned(i.id)]
    for nid, issues in render.items():
        ix.nodes[nid].issues.extend(issues)
    for nid, iss in lint_pairs:
        ix.nodes[nid].issues.append(iss)
    for n in ix.nodes.values():
        if len(n.issues) > 1:
            n.issues.sort(key=_issue_sort_key)
    ix.diagnostics = [d for d in ix.diagnostics if not d.startswith(_DIAG_PREFIXES)] + diags


# --------------------------------------------------------------------------- #
# Summaries (for capture())
# --------------------------------------------------------------------------- #
def _node_label(ix: Index, nid: str, cut: int = _LABEL_CUT) -> str:
    n = ix.nodes[nid]
    ref = n.ref or n.id
    label = n.label or n.text or n.desc
    if not label:
        sel = n.sel if n.sel and n.sel != ref else None
        return f"{ref} {sel}" if sel else (f"{ref} {n.type}" if n.type else ref)
    return f"{ref} {_quote(label, cut)}"


def _quote(s: str, cut: int = _LABEL_CUT) -> str:
    s = str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    if len(s) > cut:
        s = s[: cut - 1] + "…"
    return f'"{s}"'


def lint_summary(ix: Index) -> dict[str, str]:
    """One-line summaries for capture(): ``{"lint": "14 warn: 12 role, 1 state, 1
    touch_target (contrast not run)", "issues": "1 clipped: n22"}`` (keys present
    only when there is something to say)."""
    a11y_by: Counter = Counter()
    sev_by: Counter = Counter()
    render_by: dict[str, list[str]] = {}
    for n in ix.nodes.values():
        for iss in n.issues:
            if iss.id.startswith("a11y.") or iss.id in R.DEFAULT_TB:
                a11y_by[R.short(iss.id)] += 1
                sev_by[iss.sev] += 1
            elif iss.id.startswith("render."):
                render_by.setdefault(R.short(iss.id), []).append(n.ref or n.id)
    # findings under an open dialog (or a same-window overlay) are counted apart, as
    # a11y_lint's summary does
    covered = [(n.id, i) for n in ix.nodes.values() for i in n.issues if _covered(i)]
    for _nid, iss in covered:
        a11y_by[R.short(iss.id)] -= 1
        sev_by[iss.sev] -= 1
    a11y_by = +a11y_by
    under = _under(ix, covered)
    out: dict[str, str] = {}
    contrast = "contrast sampled" if _contrast_ran(ix) else "contrast not run"
    if a11y_by:
        sevs = " ".join(f"{sev_by[s]} {s}" for s in R.SEVERITIES if sev_by[s] > 0)
        rules = ", ".join(f"{c} {k}" for k, c in a11y_by.most_common())
        out["lint"] = f"{sevs}: {rules}{under} ({contrast})"
    else:
        out["lint"] = f"no findings{under} ({contrast})"
    if render_by:
        parts = []
        for k, refs in sorted(render_by.items(), key=lambda kv: -len(kv[1])):
            shown = " ".join(refs[:3]) + (f" +{len(refs) - 3}" if len(refs) > 3 else "")
            parts.append(f"{len(refs)} {k}: {shown}")
        out["issues"] = "; ".join(parts)
    return out


def _contrast_ran(ix: Index) -> bool:
    return any(d.startswith("contrast: sampled") for d in ix.diagnostics)


def _covered(iss: Issue) -> bool:
    """A lint finding on a window under an open dialog or sheet (evidence ``covered_by``,
    the dialog's window), or on a node a same-window overlay draws over (``covered_by``,
    the overlay: capture/tb.py ``cover_lint``): hidden until it closes."""
    return bool((iss.evidence or {}).get("covered_by"))


def _overlay(ix: Index, nid: str, iss: Issue) -> tuple[str, str] | None:
    """``(overlay id, kind)`` when ``iss`` is counted apart because a same-window overlay
    (a scrim, a sheet, a drawer, an action-mode bar) draws over its node: the node's
    ``render.covered`` names the same overlay. None for a window under a dialog."""
    by = str((iss.evidence or {}).get("covered_by") or "")
    n = ix.nodes.get(nid)
    for i in n.issues if n is not None else ():
        ev = i.evidence or {}
        if i.id == "render.covered" and by in (ev.get("node_ids") or ()):
            return by, str(ev.get("kind") or "overlay")
    return None


def _under(ix: Index, covered: list[tuple[str, Issue]]) -> str:
    """The summary's ``; +N under …``: the findings under an open dialog (a window over
    theirs), and those under a same-window overlay by the overlay (``+2 under n236 (bar)``;
    ``+N under K overlays`` for several)."""
    dialog = 0
    over: dict[str, str] = {}
    n_over = 0
    for nid, iss in covered:
        ov = _overlay(ix, nid, iss)
        if ov is None:
            dialog += 1
        else:
            over.setdefault(*ov)
            n_over += 1
    parts = [f"+{dialog} under an open dialog"] if dialog else []
    if len(over) == 1:
        oid, kind = next(iter(over.items()))
        on = ix.get(oid)
        parts.append(f"+{n_over} under {_ref(on) if on is not None else oid} ({kind})")
    elif over:
        parts.append(f"+{n_over} under {len(over)} overlays")
    return "; " + ", ".join(parts) if parts else ""


def covered_windows(ix: Index, loaded: Any) -> dict[str, str | None]:
    """``{window id: the id of the modal window over it}`` for every window the stored
    a11y tree puts under an open dialog or sheet (``covered_by``); empty without one."""
    src = loaded if isinstance(loaded, _Src) else _Src(loaded)
    dump = src.a11y_dump()
    out: dict[str, str | None] = {}
    for w in (dump.data.get("windows") or []) if dump is not None else []:
        if w.get("covered_by") is None or w.get("root_view_id") is None:
            continue
        nid = ix.resolve_id(window_key(int(w["root_view_id"])))
        if nid is not None:
            out[nid] = ix.resolve_id(window_key(int(w["covered_by"])))
    return out


def _covered_out(ix: Index, covered: list[tuple[str, Issue]]) -> dict[str, Any]:
    """lint()'s ``covered``: how many findings sit under an open dialog, on which windows,
    under which dialog; a same-window overlay is named with its kind (``n236 (bar)``) and
    adds no window (its own window is the screen the lint already covers)."""
    wins: list[str] = []
    by: list[str] = []
    for nid, iss in covered:
        ov = _overlay(ix, nid, iss)
        w = ix.nodes[nid].window
        wn = ix.nodes.get(w) if w and ov is None else None
        if wn is not None and _ref(wn) not in wins:
            wins.append(_ref(wn))
        dn = ix.get(str(iss.evidence.get("covered_by")))
        d = _ref(dn) if dn is not None else str(iss.evidence.get("covered_by"))
        if ov is not None:
            d = f"{d} ({ov[1]})"
        if d not in by:
            by.append(d)
    return {"n": len(covered), "windows": wins, "by": by}


# --------------------------------------------------------------------------- #
# lint_view(): the lint tool
# --------------------------------------------------------------------------- #
def _cursor_hash(args: Mapping[str, Any]) -> str:
    blob = json.dumps(args, sort_keys=True, default=str).encode()
    return hashlib.blake2s(blob, digest_size=4).hexdigest()


def _make_cursor(cid: str, h: str, offset: int) -> str:
    return f"{cid}:l:{h}:{offset}"


def _parse_cursor(cursor: str, cid: str, h: str) -> int:
    parts = str(cursor).split(":")
    if len(parts) != 4 or parts[1] != "l" or not parts[3].isdigit():
        raise OpError("bad_args", f"not a lint cursor: {cursor!r}")
    if parts[0].lower() != (cid or "").lower() or parts[2] != h:
        raise OpError("bad_args", "the cursor belongs to another capture or other lint arguments",
                      hint="Repeat the lint call with the same arguments as the first page.")
    return int(parts[3])


def _resolve_within(ix: Index, sel: str) -> UNode:
    try:
        from .query import resolve_selector  # C6; the fallback below handles ids and keys
    except ImportError:
        resolve_selector = None
    if resolve_selector is not None:
        return resolve_selector(ix, sel)
    node = ix.get(sel)
    if node is None:
        raise OpError("not_found", f"no node {sel!r} in this capture")
    return node


def _subtree(ix: Index, node: UNode) -> set[str]:
    tree = "slots" if node.kind == "slot" else "ui"
    return {n.id for n, _ in ix.walk(tree, node.id)}


_SEG_SPLIT = re.compile(r"(?<!\\)/")
_LABEL_IN_SEG = re.compile(r'"(?:[^"\\]|\\.)*"')


def _template(anchor: str | None, own: bool = False) -> str | None:
    """The collapse key of an anchor inside a collection: every item index
    ``[i]`` becomes ``[*]``, and so does every label from the item segment down
    to (not including) the node's own segment: a Compose list row's merged
    label (an email subject) differs per row, while the unlabelled button inside
    it is the same composable in every row. ``own``: the node's own label too (a
    TalkBack finding on the row itself: every row has it, whatever it says). None
    outside a collection."""
    if not anchor or "[" not in anchor:
        return None
    t = re.sub(r"(?<!\\)\[\d+\]", "[*]", anchor)
    if t == anchor:
        return None
    segs = _SEG_SPLIT.split(t)
    first = next((i for i, seg in enumerate(segs) if "[*]" in seg), None)
    if first is None:
        return t
    last = len(segs) - 1
    for i in range(first, len(segs)):
        if i < last or i == first or own:
            segs[i] = _LABEL_IN_SEG.sub('"*"', segs[i])
    return "/".join(segs)


def _lca(ix: Index, ids: list[str]) -> UNode | None:
    paths = []
    for nid in ids:
        chain = [nid] + [a.id for a in ix.ancestors(nid)]
        paths.append(list(reversed(chain)))
    common = None
    for level in zip(*paths):
        if all(x == level[0] for x in level):
            common = level[0]
        else:
            break
    return ix.nodes.get(common) if common else None


def _ref(n: UNode) -> str:
    return n.ref or n.id


def _sel_of(n: UNode) -> str:
    if n.sel and n.sel != _ref(n):
        return n.sel
    if n.rid:
        return f"#{n.rid}"
    if n.tag:
        return f"@{n.tag}"
    return _ref(n)


def _num(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _tb_detail(iss: Issue) -> str:
    """A tb.* finding's evidence in a few words (the other nodes are refs)."""
    ev = iss.evidence or {}
    others = [str(x) for x in ev.get("node_ids") or []]
    rid = iss.id
    if rid == "tb.double_stop":
        inner = " ".join(others[:2]) + (f" +{len(others) - 2}" if len(others) > 2 else "")
        return f"and {inner} inside ({ev.get('why', '')})".replace(" ()", "")
    if rid == "tb.ghost_stop":
        return f"{ev.get('why', '')}: says {_quote(ev.get('said') or '', 24)}"
    if rid in ("tb.out_of_order", "tb.boundary_jump"):
        after = f" after {others[0]}" if others else ""
        if ev.get("why") == "in_overlay" and len(others) > 1:  # talkback/static.py
            return (f"read {ev.get('read')}{after}: {ev.get('stops', 1)} stop(s) of "
                    f"{others[-1]}, drawn over what is read before it")
        return f"read {ev.get('read')}{after}, seen {ev.get('visual')} of {ev.get('of')}"
    if rid == "tb.covered_stop":  # talkback/occlusion.py
        ex = " ".join(others[:2]) + (f" +{len(others) - 2}" if len(others) > 2 else "")
        return f"draws over {ev.get('covers')} stop(s) TalkBack reads ({ev.get('kind')}): {ex}"
    if rid == "tb.escape":
        ex = " ".join(others[:2])
        return (f"{ev.get('under')} stops under it ({ev.get('area_pct')}% of the window), "
                f"e.g. {ex}")
    if rid == "tb.window_order":
        return f"read after {ev.get('read_after')} stops below its top, e.g. " + \
            (others[0] if others else "?")
    if rid == "tb.wrong_announcement":
        if ev.get("why") == "speech_order":
            if ev.get("reads") and ev.get("before"):  # where the two orders part
                return (f"reads {_quote(ev['reads'], 24)} before {_quote(ev['before'], 24)}, "
                        "which comes first on screen")
            return (f"says {_quote(ev.get('said') or '', 24)}, "
                    f"shown {_quote(ev.get('shown') or '', 24)}")
        k = ev.get("silent_items")
        eg = f", e.g. {others[0]}" if others else ""
        if ev.get("said_pos"):  # the wrong "N of M" itself, not the cut head of the speech
            return f"says {_quote(ev['said_pos'], 24)}: counts {k} silent item(s){eg}"
        return f"says {_quote(ev.get('said') or '', 24)}: {k} silent item(s) counted{eg}"
    if rid == "tb.edge_stuck":
        if ev.get("why") == "pager":
            return "a pager: TalkBack never scrolls to the next page"
        return (f"{ev.get('past_edge')} past its edge ({_quote(ev.get('first') or '', 20)}), "
                "nothing scrolls")
    if rid == "tb.skipped":
        if ev.get("why") == "not_spoken":  # e.g. a row's contentDescription replaces it
            over = f": {others[0]} says something else" if others else ""
            return f"no stop says {_quote(ev.get('first') or '', 24)}{over}"
        return f"hides {ev.get('texts')} text(s): {_quote(ev.get('first') or '', 24)}"
    if rid == "tb.custom_action_missing":
        src = f" ({ev['src']})" if ev.get("src") else ""
        if others:  # the action sits on a container TalkBack never focuses
            return f"{ev.get('gesture')}: its custom action is on {others[0]}, never focused{src}"
        return f"{ev.get('gesture')} without a custom action{src}"
    return ""


def _rows_of(ev: Mapping[str, Any]) -> str:
    n, of = ev.get("rows"), ev.get("of")
    return f"{n} of {of} rows" if of else f"{n} row(s)"


def _heading_detail(ev: Mapping[str, Any]) -> str:
    if ev.get("reason") != "section_title":
        return ""
    at = f", list item {ev['position']}" if ev.get("position") else (
        ", a list item" if ev.get("list_item") else "")
    return f"section title, not a heading ({ev.get('rows')} rows below{at})"


#: A few words of evidence on the lint lines of R9's section titles and R19..R23.
_NEW_DETAIL: dict[str, Callable[[Mapping[str, Any]], str]] = {
    "a11y.heading.structure": _heading_detail,
    "a11y.label.placeholder_token": lambda ev: (
        f"reads {_quote(ev.get('token') or '', 40)} in {ev.get('rows')} row(s)"),
    "a11y.label.shared_prefix": lambda ev: (
        f"{_rows_of(ev)} start {_quote(ev.get('prefix') or '', 32)}, the description of "
        f"{ev.get('child_class') or 'a child'} in each"),
    "a11y.label.decorative_merged": lambda ev: (
        f"{_quote(ev.get('merged') or '', 24)} merged into {_rows_of(ev)}, from "
        f"{ev.get('child_class') or 'a child'}"
        + (f"; {_quote(ev['twin_label'], 24)} says it" if ev.get("twin_label") else "")),
    "a11y.toggle.label_contradicts": lambda ev: (
        # "said": the state TalkBack speaks; none for an unchecked checkable node
        f"{ev.get('said') or 'not checked'}, named for the action "
        f"{_quote(ev.get('undo') or '', 24)}"),
    "a11y.selection.uniform_unselected": lambda ev: (
        f"{ev.get('rows')} rows say {_quote(ev.get('state') or '', 20)}, none selected"
        + (f"; each has {_quote(ev['inner_label'], 24)}" if ev.get("inner_label") else "")),
}


def _detail(iss: Issue) -> str:
    ev = iss.evidence or {}
    bits = []
    if iss.id.startswith("tb."):
        bits.append(_tb_detail(iss))
    elif iss.id in _NEW_DETAIL:
        bits.append(_NEW_DETAIL[iss.id](ev))
    elif iss.id == TOUCH_RULE and "w_dp" in ev:
        bits.append(f"{_num(ev['w_dp'])}x{_num(ev.get('h_dp'))}dp")
    elif iss.id == CONTRAST_RULE and "ratio" in ev:
        bits.append(f"{ev['ratio']}:1")
    elif iss.id == CLIPPED and "visible_px" in ev:
        bits.append(f"{ev['visible_px']} of {ev.get('declared_px', '?')}px")
    elif iss.id == HIDDEN and ev.get("why"):
        bits.append(str(ev["why"]))
    elif iss.id == OFFSCREEN and ev.get("outside"):
        bits.append(f"outside {ev['outside']}")
    elif iss.id == DUP_RULE and (ev.get("name") or ev.get("node_ids")):
        others = [str(x) for x in ev.get("node_ids") or []]
        like = " ".join(others[:2]) + (f" +{len(others) - 2}" if len(others) > 2 else "")
        named = f"named {_quote(ev['name'], 24)}" if ev.get("name") else ""
        bits.append(" ".join(x for x in (named, f"like {like}" if like else "") if x))
    note = ev.get("note")
    out = " ".join(bits)
    if note:
        out = f"{out} ({note})" if out else f"({note})"
    return out


def _effective(ix: Index, src: _Src, *, contrast: bool, wcag: bool, density: int,
               font_scale: float) -> tuple[list[tuple[str, Issue]], str, list[str]]:
    """Every issue lint_view reports: the stored ones, with the tree lint re-run for
    ``wcag`` and contrast added for ``contrast`` (both cached per capture)."""
    stored = [(nid, iss) for nid, n in ix.nodes.items() for iss in n.issues]
    render = {}
    for nid, iss in stored:
        if iss.id.startswith("render."):
            render.setdefault(nid, []).append(iss)
    unmapped: list[str] = []
    ran = _contrast_ran(ix)
    status = "sampled" if ran else "not run (lint(contrast=true) ~4s)"
    out = stored
    if wcag:
        res = _cached_lint(src, ix, "tree", density=density, font_scale=font_scale, wcag=True)
        pairs = [(nid, Issue(i.id, i.sev, dict(i.evidence), i.conf)) for nid, i in res.issues]
        pairs = _annotate_touch_fp(pairs, render)
        out = [(nid, i) for nid, i in out
               if not i.id.startswith("a11y.") or i.id == CONTRAST_RULE] + pairs
        unmapped.extend(res.unmapped)
    if contrast and not ran:
        res = _cached_lint(src, ix, "contrast", density=density, font_scale=font_scale,
                           wcag=False)
        pairs = [(nid, Issue(i.id, i.sev, dict(i.evidence), i.conf)) for nid, i in res.issues]
        pairs = _annotate_touch_fp(pairs, render)
        out = [(nid, i) for nid, i in out if i.id != CONTRAST_RULE] + pairs
        unmapped.extend(res.unmapped)
        status = "sampled" if res.status.startswith("sampled") else res.status
    return out, status, unmapped


def lint_view(ix: Index, loaded: Any, *, rules: Any = None, severity: str = "info",
              within: str | None = None, contrast: bool = False, wcag: bool = False,
              group: str = "rule", per_rule: int = PER_RULE, limit: int = LINT_LIMIT,
              cursor: str | None = None, max_bytes: int | None = None) -> dict[str, Any]:
    """The ``lint`` tool over one capture (spec 5.9). Default rules are every
    ``a11y.*`` rule; ``render.*`` issues are reported when asked for
    (``rules=["render."]``) and pointed to otherwise.

    ``max_bytes`` works as in every query tool (``query.resolve_max_bytes``):
    None is the 4,000 default, 0 the 32,000 ceiling, anything else is clamped to
    500..32,000. A page always shows at least one finding (in its smallest form
    when nothing else fits), so following ``truncated.cursor`` always advances;
    the cursor's ``next`` hint repeats every non-default argument."""
    from .query import cursor_call, next_hints, resolve_max_bytes  # C6

    if severity not in R.SEVERITIES:
        raise OpError("bad_args", f"severity must be one of {', '.join(R.SEVERITIES)}")
    if group not in GROUPS:
        raise OpError("bad_args", f"group must be one of {', '.join(GROUPS)}")
    try:
        per_rule = max(1, min(20, int(per_rule)))
        limit = max(1, min(200, int(limit)))
    except (TypeError, ValueError) as e:
        raise OpError("bad_args", f"per_rule and limit are integers ({e})") from None
    max_bytes_arg = max_bytes
    max_bytes = resolve_max_bytes(max_bytes, LINT_MAX_BYTES)
    selected = R.resolve(rules)
    explicit = selected is not None
    if selected is None:
        selected = [r.id for r in RULES.values() if r.family == "a11y" and not r.planned]
        selected += list(R.DEFAULT_TB)
    selected_set = set(selected)

    src = _Src(loaded)
    density = _density(ix, src)
    cid = ix.meta.id if ix.meta else ""
    # the page size (limit, max_bytes) is not part of the cursor: it may change
    norm = {"rules": sorted(selected_set) if explicit else None, "severity": severity,
            "within": within, "contrast": bool(contrast), "wcag": bool(wcag), "group": group,
            "per_rule": per_rule}
    h = _cursor_hash(norm)
    offset = _parse_cursor(cursor, cid, h) if cursor else 0

    scope = _subtree(ix, _resolve_within(ix, within)) if within else None
    pairs, contrast_status, unmapped = _effective(
        ix, src, contrast=bool(contrast), wcag=bool(wcag), density=density,
        font_scale=_font_scale(ix))
    order = {nid: i for i, nid in enumerate(ix.nodes)}
    def wanted(rule_id: str) -> bool:
        # A rule a newer lint emits and the catalog lacks still shows by default.
        return rule_id in selected_set or (
            not explicit and not R.is_known(rule_id) and rule_id.startswith("a11y."))

    kept = [(nid, iss) for nid, iss in pairs
            if wanted(iss.id) and R.at_least(iss.sev, severity)
            and (scope is None or nid in scope)]
    kept.sort(key=lambda p: (order.get(p[0], 1 << 30), p[1].id))
    # Findings under an open dialog are counted apart (``covered``), as a11y_lint does, unless
    # ``within`` asks for that part of the screen.
    covered = [p for p in kept if _covered(p[1])]
    if scope is None:
        kept = [p for p in kept if not _covered(p[1])]
    counts = {s: 0 for s in R.SEVERITIES}
    for _, iss in kept:
        counts[iss.sev] = counts.get(iss.sev, 0) + 1

    out: dict[str, Any] = {"capture": cid, "counts": counts}
    if any(r.startswith("a11y.") for r in selected_set):
        out["contrast"] = contrast_status
    if within:
        out["within"] = within
    if covered:
        out["covered"] = dict(_covered_out(ix, covered), listed=scope is not None)
    if unmapped:
        out["unmapped"] = len(unmapped)
    available = set(getattr(a11y_lint, "ALL_RULE_IDS", ())) | {
        r.id for r in RULES.values() if r.family in ("render", "tb") and not r.planned}
    unavailable = [RULES[r].label for r in selected if explicit and r in RULES
                   and r not in available]
    if unavailable:
        out["unavailable"] = unavailable

    budget = Budget(max_bytes, reserve=FOOTER_RESERVE)
    budget.add(json_cost(out) + 40)  # the list key and the closing brace
    more_scope = {k: v for k, v, default in (
        ("within", within, None), ("severity", severity, "info"),
        ("contrast", bool(contrast), False), ("wcag", bool(wcag), False)) if v != default}
    items, total, truncated_why = _render_groups(ix, kept, group, per_rule, limit, offset,
                                                 budget, more_scope)
    out["rules" if group == "rule" else "lines"] = items
    shown_end = offset + len(items)
    nxt: list[str] = []
    if shown_end < total:
        out["truncated"] = {"omitted": total - shown_end, "why": truncated_why,
                            "cursor": _make_cursor(cid, h, shown_end)}
        args: dict[str, Any] = {}
        if explicit:
            args["rules"] = rules
        for k, v, default in (("severity", severity, "info"), ("within", within, None),
                              ("contrast", bool(contrast), False), ("wcag", bool(wcag), False),
                              ("group", group, "rule"), ("per_rule", per_rule, PER_RULE)):
            if v != default:
                args[k] = v
        page: dict[str, Any] = {}
        if limit != LINT_LIMIT:
            page["limit"] = limit
        if max_bytes_arg is not None and max_bytes != LINT_MAX_BYTES:
            page["max_bytes"] = max_bytes_arg
        nxt.append(cursor_call("lint", args, page, out["truncated"]["cursor"]))
    if kept:
        nxt.append('image(overlay="lint")')
        focus = _focus_node(ix, kept)
        if focus:
            fn = ix.get(focus)
            tb_only = fn is not None and any(i.id.startswith("tb.") for nid, i in kept
                                             if nid == fn.id) and all(
                i.id.startswith("tb.") for _nid, i in kept)
            # a TalkBack finding: the node's TalkBack account (why, speech, neighbours)
            nxt.append(f'node("{focus}",facets="tb,issues")' if tb_only
                       else f'node("{focus}")')
    if not explicit and any(i.id.startswith("render.") for n in ix.nodes.values()
                            for i in n.issues):
        nxt.append('find(issue="render.")')
    if covered and scope is None:
        # ahead of find(issue="render."): what a dialog hides matters more (3 hints at most);
        # what a same-window overlay draws over is listed by its render.covered marks (the
        # window it is in is the whole screen again)
        render = 'find(issue="render.")'
        wins = out["covered"]["windows"]
        hint = f'lint(within="{wins[0]}")' if wins else 'find(issue="render.covered")'
        nxt.insert(nxt.index(render) if render in nxt else len(nxt), hint)
    if explicit and not kept and any(r.startswith("tb.") for r in selected_set):
        # nothing static: traps, loops and focus after an action or a list update show
        # only on the device
        out["note"] = ("no static TalkBack finding; a trap, a loop, or focus lost after an "
                       "action or a list update shows only in a walk (tb_walk, tb_scenario)")
        nxt += ['outline(view="reading",explain=true)', "tb_walk()"]
    nxt = next_hints(nxt)
    if nxt:
        out["next"] = nxt
    return _fit_lint(out, max_bytes)


def _fit_lint(out: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """Shed optional parts when a forced single-finding page runs over the budget:
    the non-cursor hints, then the bookkeeping counts."""
    if json_cost(out) <= max_bytes:
        return out
    if out.get("next"):
        keep = [x for x in out["next"] if x.startswith("lint(") and "cursor=" in x]
        if keep:
            out["next"] = keep
        else:
            out.pop("next")
    for key in ("unavailable", "unmapped", "contrast"):
        if json_cost(out) <= max_bytes:
            break
        out.pop(key, None)
    return out


def _focus_node(ix: Index, kept: list[tuple[str, Issue]]) -> str | None:
    """The node most worth a closer look: a likely false positive, else the worst."""
    for nid, iss in kept:
        if iss.evidence.get("note"):
            return _ref(ix.nodes[nid])
    best = max(kept, key=lambda p: R.SEV_RANK.get(p[1].sev, 0))
    return _ref(ix.nodes[best[0]])


def _render_groups(ix: Index, kept: list[tuple[str, Issue]], group: str, per_rule: int,
                   limit: int, offset: int, budget: Budget,
                   scope: Mapping[str, Any] | None = None) -> tuple[list[Any], int, str]:
    """(items for this page, total items, why truncated)."""
    if group == "rule":
        items = _rule_items(ix, kept, per_rule, scope)
    elif group == "node":
        items = _node_lines(ix, kept)
    else:
        items = [_finding_line(ix, nid, iss) for nid, iss in kept]
    total = len(items)
    page: list[Any] = []
    why = "limit"
    for item in items[offset:]:
        if len(page) >= limit:
            why = "limit"
            break
        if not budget.add(json_cost(item) + 1):
            why = "max_bytes"
            if not page:
                # a page always advances: the smallest form of this finding, even
                # when the footer reserve says it does not fit (_fit_lint trims)
                page.append(_minimal_item(item, budget))
            break
        page.append(item)
    return page, total, why


def _minimal_item(item: Any, budget: Budget) -> Any:
    """``item`` with its first example node only, else without msg/fix and with a
    short example; a finding line is cut."""
    if isinstance(item, dict):
        small = dict(item, nodes=item["nodes"][:1])
        if budget.add(json_cost(small) + 1):
            return small
        first = str(item["nodes"][0]) if item.get("nodes") else ""
        return {"rule": item["rule"], "sev": item["sev"], "n": item["n"],
                "nodes": [first if len(first) <= 60 else first[:59] + "…"]}
    s = str(item)
    return s if len(s) <= 100 else s[:99] + "…"


def _line(ix: Index, nid: str, iss: Issue) -> str:
    d = _detail(iss)
    return _node_label(ix, nid) + (f" {d}" if d else "")


def _rule_items(ix: Index, kept: list[tuple[str, Issue]], per_rule: int,
                scope: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """One item per rule. ``scope``: the caller's non-default filter arguments
    (within, severity, contrast, wcag), repeated in each "+N more" hint so
    following it lists the rest of the same findings, not the whole capture's."""
    from .query import call  # C6

    by_rule: dict[str, list[tuple[str, Issue]]] = {}
    for nid, iss in kept:
        by_rule.setdefault(iss.id, []).append((nid, iss))
    items = []
    for rid, members in by_rule.items():
        rule = R.get(rid, members[0][1].sev)
        sev = R.worst(i.sev for _, i in members) or rule.sev
        if rid == DUP_RULE:  # one collapse per shared name (a row's own label can be its state)
            by_name: dict[Any, list[tuple[str, Issue]]] = {}
            for nid, iss in members:
                n = ix.nodes[nid]
                name = iss.evidence.get("name") or n.label or n.text or n.desc
                by_name.setdefault(name, []).append((nid, iss))
            lines = [ln for grp in by_name.values() for ln in _collapsed(ix, grp)]
        else:
            lines = _collapsed(ix, members)
        shown = lines[:per_rule]
        rest = sum(n for _, n in lines[per_rule:])
        nodes = [s for s, _ in shown]
        if rest:
            nodes.append(f"+{rest} more: "
                         + call("lint", rules=[rule.label], group="node", **(scope or {})))
        item: dict[str, Any] = {"rule": rid, "sev": sev, "n": len(members), "msg": rule.msg}
        if rule.fix:
            item["fix"] = rule.fix
        item["nodes"] = nodes
        items.append(item)
    items.sort(key=lambda it: (-R.SEV_RANK.get(it["sev"], 0), -it["n"], it["rule"]))
    return items


def _collapsed(ix: Index, members: list[tuple[str, Issue]]) -> list[tuple[str, int]]:
    """Template collapse (spec 3.8): findings of one rule on nodes that share a
    ``src`` or a template anchor (collection ordinals wildcarded) become one line.
    Returns ``[(line, findings it covers)]`` in first-occurrence order."""
    groups: dict[tuple, list[tuple[str, Issue]]] = {}
    own = bool(members) and members[0][1].id.startswith("tb.")
    for nid, iss in members:
        n = ix.nodes[nid]
        tpl = _template(n.anchor, own)
        key: tuple = ("src", n.src) if n.src else (("tpl", tpl) if tpl else ("node", nid))
        groups.setdefault(key, []).append((nid, iss))
    out: list[tuple[str, int]] = []
    for key, grp in groups.items():
        if len(grp) == 1 or key[0] == "node":
            out.extend((_line(ix, nid, iss), 1) for nid, iss in grp)
            continue
        ids = [nid for nid, _ in grp]
        first = ix.nodes[ids[0]]
        tpl = _template(first.anchor, own)
        container = _lca(ix, ids)
        where = f"in {_sel_of(container)} cells" if (container is not None and tpl) else (
            f"under {_sel_of(container)}" if container is not None else "")
        # src plus type ("FeedRow.kt:42 IconButton"), else the template's last segment
        what = (" ".join(x for x in (first.src, first.type) if x) if first.src
                else (tpl.rsplit("/", 1)[-1] if tpl else first.type or ""))
        if what.strip('"*:0123456789') == "":  # only a wildcard label or an ordinal
            what = first.type or ""
        examples = " ".join(_ref(ix.nodes[i]) for i in ids[:3])
        more = f" +{len(ids) - 3}" if len(ids) > 3 else ""
        head = f"×{len(ids)} {where}".rstrip()
        out.append((f"{head} ({what}): {examples}{more}" if what else
                    f"{head}: {examples}{more}", len(ids)))
    return out


def _node_lines(ix: Index, kept: list[tuple[str, Issue]]) -> list[str]:
    by_node: dict[str, list[Issue]] = {}
    for nid, iss in kept:
        by_node.setdefault(nid, []).append(iss)
    lines = []
    for nid, issues in by_node.items():
        issues.sort(key=lambda i: (-R.SEV_RANK.get(i.sev, 0), i.id))
        parts = []
        for i in issues:
            d = _detail(i)
            parts.append(f"!{R.short(i.id)} {i.sev}" + (f" {d}" if d else ""))
        lines.append(f"{_node_label(ix, nid)} " + "; ".join(parts))
    return lines


def _finding_line(ix: Index, nid: str, iss: Issue) -> str:
    rule = R.get(iss.id)
    d = _detail(iss)
    return f"{_node_label(ix, nid)} {rule.label} {iss.sev}" + (f" {d}" if d else "")


__all__ = [
    "ALIASES", "LINT_MODES", "RULES", "analyze", "lint_summary", "lint_view",
    "reading_order", "render_signals",
]
