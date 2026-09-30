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
* **Accessibility lint** (``a11y.*``), through a thin adapter over
  ``inspector_widget.a11y_lint``. ``_lint_windows`` is the single input choice:
  today it rebuilds Compose semantics dicts from ``raw/compose_sem.pb`` with
  ``strings.dump_compose_to_dict``, and each finding maps to a node through
  ``sem:<acv>:<id>``. Switching to the unified a11y tree (improve/a11y-lint-unified)
  changes that one function; findings that carry a typed ``node_key`` are already
  understood by ``_finding_key``. Contrast runs only for ``lint="full"`` or
  ``lint_view(contrast=True)``, reads each window's own stored screenshot, and is
  cached in the capture's derived store.
* **Reading order**: ``a11y.compute_traversal_order`` (or the newer
  ``a11y.reading_order``) over the stored a11y tree, mapped to node ids; sets
  ``stop`` and ``Index.reading``.

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
from collections.abc import Iterable, Mapping
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
)
from .rules import ALIASES, RULES

LINT_MODES = ("tree", "full", "none")
GROUPS = ("rule", "node", "none")
DEFAULT_DENSITY = 420
CONTRAST_RULE = "a11y.contrast.low"
TOUCH_RULE = "a11y.touch_target.small"
CLIPPED = "render.clipped"
HIDDEN = "render.hidden"
OFFSCREEN = "render.offscreen"
ZERO_SIZE = "render.zero_size"
LIKELY_FP = "likely false positive: clipped at scroll edge"
#: bytes kept free for the truncation notice and the next hints
FOOTER_RESERVE = 280
#: bump when the cached lint shape changes (derived/lint.<hash>.json)
LINT_CACHE_VERSION = 1

_ACTION_FLAGS = frozenset({"click", "longclick", "edit", "checkable"})
_EDGE_SLOP = 1
_LABEL_CUT = 32
_EVIDENCE_DROP = frozenset({"label", "announceable_keys", "structural_keys", "fg_lum",
                            "bg_lum", "px_sampled", "fg_fraction", "sample",
                            "text_size_class"})
SLIVER_NOTE = "low confidence: only a sliver is visible at the scroll edge"


# --------------------------------------------------------------------------- #
# What the analyzers read from a capture
# --------------------------------------------------------------------------- #
class _Src:
    """A LoadedCapture (store), a RawCapture (before publish) or None, behind one
    read-only surface: ``meta``, ``raw(name)``, ``shot(root)``, ``derived(name)``,
    ``put_derived(name, bytes)``. Missing facets read as None."""

    def __init__(self, loaded: Any) -> None:
        self.obj = loaded
        self.meta = getattr(loaded, "meta", None)
        self._shots: dict[int, Any] = {}

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
    for a in (n.facets.get("a11y") or {}).get("actions") or ():
        name = str(a.get("name") if isinstance(a, Mapping) else a)
        if name in ("SCROLL_UP", "SCROLL_DOWN"):
            axes.add("v")
        elif name in ("SCROLL_LEFT", "SCROLL_RIGHT"):
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
    """Touches a scroll viewport edge and is far smaller than its same-type siblings."""
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
        if c.id == n.id or c.type != n.type:
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
# The lint adapter
# --------------------------------------------------------------------------- #
@dataclass
class _LintWindow:
    """One Compose window's semantics tree, with surrogate node ids."""

    acv: int
    root: dict[str, Any]
    keys: dict[int, str]  # surrogate id -> canonical key


def _lint_windows(src: _Src) -> list[_LintWindow]:
    """THE lint input choice. Today: the stored Compose semantics
    (``raw/compose_sem.pb`` via ``strings.dump_compose_to_dict``), which is what
    ``a11y_lint.lint_tree`` consumes. Every node's ``id`` is replaced by a
    surrogate unique across windows (ComposeViews reuse semantics ids, ID3), and
    ``keys`` maps each surrogate back to ``sem:<acv>:<id>``; the synthetic window
    root, which carries the AndroidComposeView's own id, maps to ``view:<acv>``.

    When improve/a11y-lint-unified lands, return the unified a11y tree
    (``a11y.a11y_to_dict(raw/a11y.pb)``) here instead; ``_finding_key`` already
    maps its typed ``node_key`` findings."""
    data = src.raw("compose_sem")
    if not data:
        return []
    from .. import strings
    from ..proto import view_inspection_pb2 as pb

    decoded = strings.dump_compose_to_dict(pb.DumpComposeResponse.FromString(data))
    out: list[_LintWindow] = []
    counter = 0
    for w in decoded.get("windows") or []:
        root = w.get("root")
        if not root:
            continue
        acv = int(w.get("view_id") or 0)
        keys: dict[int, str] = {}
        stack = [(root, True)]
        while stack:
            node, is_root = stack.pop()
            counter += 1
            orig = int(node.get("id") or 0)
            if is_root and node.get("kind") != "SEMANTICS" and orig == acv:
                keys[counter] = view_key(acv)
            elif node.get("kind") == "SEMANTICS":
                keys[counter] = sem_key(acv, orig)
            node["id"] = counter
            stack.extend((c, False) for c in reversed(node.get("children") or []))
        out.append(_LintWindow(acv, root, keys))
    return out


def _finding_key(f: Any, keys: Mapping[int, str]) -> str | None:
    """Canonical key of a finding's node: the surrogate id assigned by
    ``_lint_windows`` when the input carried surrogates, else a typed ``node_key``
    (the unified lint over the a11y tree). The surrogate wins: the unified lint's
    adapter for Compose-semantics input derives its typed keys from the node ids it
    was given (``compose:1:<surrogate>``), which name no real node."""
    node = getattr(f, "node", None) or {}
    if not isinstance(node, Mapping):
        return None
    sid = node.get("id")
    if isinstance(sid, int) and sid in keys:
        return keys[sid]
    typed = node.get("key")
    if isinstance(typed, str):
        parts = typed.split(":")
        try:
            if parts[0] == "view" and len(parts) == 2:
                return view_key(int(parts[1]))
            if parts[0] == "compose" and len(parts) == 3:
                return sem_key(int(parts[1]), int(parts[2]))
            if parts[0] == "virtual" and len(parts) == 3:
                return a11y_key(int(parts[1]), int(parts[2]))
        except ValueError:
            return None
    return None


def _evidence(f: Any, keys: Mapping[int, str], ix: Index) -> dict[str, Any]:
    ev: dict[str, Any] = {}
    for k, v in (getattr(f, "evidence", None) or {}).items():
        if (k in _EVIDENCE_DROP or k.startswith("has_") or v is None or v is False
                or v == [] or v == {}):
            continue
        if k in ("children_ids", "node_ids") and isinstance(v, list):
            v = [ix.resolve_id(keys.get(x, "")) or x for x in v]
        ev[k] = v
    return ev


@dataclass
class _LintResult:
    issues: list[tuple[str, Issue]] = field(default_factory=list)  # (node id, issue)
    unmapped: list[str] = field(default_factory=list)  # "rule key"
    status: str = "ok"


def _map_findings(findings: Iterable[Any], keys: Mapping[int, str], ix: Index,
                  res: _LintResult) -> None:
    for f in findings:
        key = _finding_key(f, keys)
        nid = ix.resolve_id(key) if key else None
        if nid is None:
            res.unmapped.append(f"{f.rule} {key or '?'}")
            continue
        ev = _evidence(f, keys, ix)
        conf = "inferred" if ev.pop("low_confidence", False) else "exact"
        res.issues.append((nid, Issue(f.rule, f.severity, ev, conf)))


def _run_tree_lint(src: _Src, ix: Index, *, density: int, font_scale: float,
                   wcag: bool = False) -> _LintResult:
    """Every tree rule (no pixels) over all windows at once, so cross-node rules
    (duplicate labels, headings) see the whole screen."""
    res = _LintResult()
    wins = _lint_windows(src)
    if not wins:
        res.status = "no compose semantics"
        return res
    keys: dict[int, str] = {}
    for w in wins:
        keys.update(w.keys)
    ctx = a11y_lint.LintContext(density=density, font_scale=font_scale, wcag_mode=wcag)
    findings = a11y_lint.lint_tree([w.root for w in wins], ctx)
    _map_findings((f for f in findings if f.rule != CONTRAST_RULE), keys, ix, res)
    return res


def _decode_shot(shot: Any) -> tuple[int, int, bytes] | None:
    from .. import png

    try:
        return png._decode_to_rgba(shot)
    except Exception:  # noqa: BLE001 - an undecodable screenshot skips contrast only
        return None


def _shift(node: dict[str, Any], dx: int, dy: int) -> None:
    stack = [node]
    while stack:
        n = stack.pop()
        lay = (n.get("bounds") or {}).get("layout")
        if lay:
            lay["x"] = lay.get("x", 0) - dx
            lay["y"] = lay.get("y", 0) - dy
        stack.extend(n.get("children") or [])


def _run_contrast(src: _Src, ix: Index, *, density: int, font_scale: float,
                  wcag: bool = False) -> _LintResult:
    """The contrast rule per Compose window, sampling that window's own stored
    screenshot (window-relative pixels at the capture scale)."""
    res = _LintResult()
    wins = _lint_windows(src)
    if not wins:
        res.status = "no compose semantics"
        return res
    decoded: dict[int, tuple[int, int, bytes, float] | None] = {}
    sampled = 0
    for w in wins:
        acv_node = ix.get(view_key(w.acv))
        win = ix.nodes.get(acv_node.window) if acv_node is not None and acv_node.window else None
        if win is None or "view" not in win.ids:
            continue
        root_udid = int(win.ids["view"])
        if root_udid not in decoded:
            shot = src.shot(root_udid)
            px = _decode_shot(shot) if shot is not None else None
            if px is None:
                decoded[root_udid] = None
            else:
                wr = _rect(win.b)
                scale = float(shot.scale or 0.0)
                if scale <= 0.0:
                    scale = px[0] / wr[2] if wr and wr[2] > 0 else 1.0
                decoded[root_udid] = (px[0], px[1], px[2], scale)
        img = decoded[root_udid]
        if img is None:
            continue
        wr = _rect(win.b) or (0, 0, 0, 0)
        lay = (w.root.get("bounds") or {}).get("layout") or {}
        # Screen-space semantics sit inside the window; pre-CO4 dialog trees are
        # already window-relative and must not be shifted twice.
        if (wr[0] or wr[1]) and lay.get("x", 0) >= wr[0] and lay.get("y", 0) >= wr[1]:
            _shift(w.root, wr[0], wr[1])
        ctx = a11y_lint.LintContext(density=density, font_scale=font_scale, wcag_mode=wcag,
                                    screenshot_rgba=img[2], screenshot_w=img[0],
                                    screenshot_h=img[1], screenshot_scale=img[3])
        findings = a11y_lint.lint_tree([w.root], ctx, enabled={CONTRAST_RULE})
        _map_findings((f for f in findings if f.rule == CONTRAST_RULE), w.keys, ix, res)
        sampled += 1
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


def _annotate_touch_fp(pairs: Iterable[tuple[str, Issue]],
                       render: Mapping[str, Iterable[Issue]]) -> None:
    """Findings on a node clipped at a scroll edge measure the visible sliver, not
    the node: a touch target there is a likely false positive (L2), and a contrast
    sample is low confidence."""
    for nid, iss in pairs:
        if iss.id not in (TOUCH_RULE, CONTRAST_RULE):
            continue
        for r in render.get(nid, ()):
            if r.id == CLIPPED and r.evidence.get("scroll"):
                if iss.id == TOUCH_RULE:
                    iss.evidence["note"] = LIKELY_FP
                else:
                    iss.evidence["note"] = SLIVER_NOTE
                    iss.conf = "inferred"
                break


# --------------------------------------------------------------------------- #
# Reading order
# --------------------------------------------------------------------------- #
def _iter_dicts(root: dict[str, Any]) -> Iterable[tuple[dict[str, Any], tuple[int, ...]]]:
    stack: list[tuple[dict[str, Any], tuple[int, ...]]] = [(root, (0,))]
    while stack:
        n, path = stack.pop()
        yield n, path
        kids = n.get("children") or []
        for i in range(len(kids) - 1, -1, -1):
            stack.append((kids[i], path + (i,)))


def _ordered_stops(roots: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any] | None]]:
    """``[(order, node dict)]`` for every TalkBack stop, via whichever reading-order
    API ``inspector_widget.a11y`` has (``reading_order`` returns the node dicts;
    the older ``compute_traversal_order`` returns entries whose ``bounds`` is the
    node's own layout dict)."""
    from .. import a11y

    ro = getattr(a11y, "reading_order", None)
    if callable(ro):
        res = ro(roots)
        return [(e.get("order"), n) for e, n in zip(res.get("focus_order") or [],
                                                    res.get("_nodes") or [])
                if e.get("order") is not None]
    by_layout: dict[int, dict[str, Any]] = {}
    for r in roots:
        for n, _ in _iter_dicts(r):
            lay = (n.get("bounds") or {}).get("layout")
            if lay is not None:
                by_layout[id(lay)] = n
    out = []
    for e in a11y.compute_traversal_order(roots):
        if e.get("order") is None:
            continue
        out.append((e["order"], by_layout.get(id(e.get("bounds")))))
    return out


def reading_order(ix: Index, loaded: Any) -> tuple[list[tuple[int, str]], list[str]] | None:
    """``([(stop, node id)], diagnostics)`` from the stored a11y tree, or None when
    the capture has no a11y facet.

    Each TalkBack stop maps to a node through, in order: the ``a11y:path:`` key
    (the index builder's fallback when a11y ids are not unique), then, only when
    the ``(host, virtual)`` pair is unique in the a11y tree, the ``a11y:`` key, a
    node whose ``ids.a11y`` names the pair, and ``view:<host>`` / ``sem:<host>:<id>``.
    A duplicated pair is never guessed."""
    src = loaded if isinstance(loaded, _Src) else _Src(loaded)
    data = src.raw("a11y")
    if not data:
        return None
    from .. import a11y, strings
    from ..proto import view_inspection_pb2 as pb

    resp = pb.DumpA11yResponse.FromString(data)
    resolver = strings.StringResolver(resp.strings)
    roots: list[dict[str, Any]] = []
    where: dict[int, tuple[int, tuple[int, ...]]] = {}
    pairs: Counter = Counter()
    for w in resp.windows:
        if not w.HasField("root"):
            continue
        d = a11y.a11y_node_to_dict(w.root, resolver)
        roots.append(d)
        root_udid = int(w.root_view_id or d.get("host_view_id") or 0)
        for n, path in _iter_dicts(d):
            where[id(n)] = (root_udid, path)
            pairs[(n.get("host_view_id"), n.get("virtual_id"))] += 1
    if not roots:
        return [], []
    a11y_ids: dict[str, str | None] = {}
    for nid, node in ix.nodes.items():
        aid = node.ids.get("a11y")
        if aid is not None:
            a11y_ids[str(aid)] = None if str(aid) in a11y_ids else nid

    def lookup(n: dict[str, Any]) -> str | None:
        root, path = where.get(id(n), (0, ()))
        if path:
            nid = ix.resolve_id(a11y_path_key(root, path))
            if nid is not None:
                return nid
        h, v = n.get("host_view_id"), n.get("virtual_id")
        if h is None or v is None or pairs[(h, v)] > 1:
            return None
        nid = ix.resolve_id(a11y_key(h, v)) or a11y_ids.get(f"{h}:{v}")
        if nid is not None:
            return nid
        return ix.resolve_id(view_key(h) if v == -1 else sem_key(h, v))

    out: list[tuple[int, str]] = []
    seen: set[str] = set()
    missing = 0
    stops = _ordered_stops(roots)
    for order, n in stops:
        nid = lookup(n) if n is not None else None
        if nid is None or nid in seen:
            missing += nid is None
            continue
        seen.add(nid)
        out.append((int(order), nid))
    diags = []
    if missing:
        dup = sum(1 for c in pairs.values() if c > 1)
        why = "; a11y ids not unique (agent ID1)" if dup else ""
        diags.append(f"reading: {missing} of {len(stops)} TalkBack stops not mapped to nodes{why}")
    return out, diags


# --------------------------------------------------------------------------- #
# analyze()
# --------------------------------------------------------------------------- #
_DIAG_PREFIXES = ("lint:", "contrast:", "reading:")


def _owned(issue_id: str) -> bool:
    return issue_id.startswith(("render.", "a11y."))


def _issue_sort_key(iss: Issue) -> tuple:
    return (iss.id, -R.SEV_RANK.get(iss.sev, 0))


def analyze(ix: Index, loaded: Any, *, lint: str = "tree", density: int | None = None,
            font_scale: float | None = None) -> None:
    """Write render and lint issues, TalkBack stops and ``Index.reading`` into ``ix``.

    ``lint``: ``"tree"`` (tree rules), ``"full"`` (tree rules plus contrast from the
    stored screenshots) or ``"none"``. Issues the analyzers own (``render.*`` and
    ``a11y.*``) are replaced, so running it again is idempotent. Diagnostics
    (unmapped findings or stops, contrast status) go to ``ix.diagnostics``.
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
        _annotate_touch_fp(lint_pairs, render)

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
            if iss.id.startswith("a11y."):
                a11y_by[R.short(iss.id)] += 1
                sev_by[iss.sev] += 1
            elif iss.id.startswith("render."):
                render_by.setdefault(R.short(iss.id), []).append(n.ref or n.id)
    out: dict[str, str] = {}
    contrast = "contrast sampled" if _contrast_ran(ix) else "contrast not run"
    if a11y_by:
        sevs = " ".join(f"{sev_by[s]} {s}" for s in R.SEVERITIES if sev_by[s])
        rules = ", ".join(f"{c} {k}" for k, c in a11y_by.most_common())
        out["lint"] = f"{sevs}: {rules} ({contrast})"
    else:
        out["lint"] = f"no findings ({contrast})"
    if render_by:
        parts = []
        for k, refs in sorted(render_by.items(), key=lambda kv: -len(kv[1])):
            shown = " ".join(refs[:3]) + (f" +{len(refs) - 3}" if len(refs) > 3 else "")
            parts.append(f"{len(refs)} {k}: {shown}")
        out["issues"] = "; ".join(parts)
    return out


def _contrast_ran(ix: Index) -> bool:
    return any(d.startswith("contrast: sampled") for d in ix.diagnostics)


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


def _template(anchor: str | None) -> str | None:
    if not anchor or "[" not in anchor:
        return None
    t = re.sub(r"\[\d+\]", "[*]", anchor)
    return t if t != anchor else None


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


def _detail(iss: Issue) -> str:
    ev = iss.evidence or {}
    bits = []
    if iss.id == TOUCH_RULE and "w_dp" in ev:
        bits.append(f"{_num(ev['w_dp'])}x{_num(ev.get('h_dp'))}dp")
    elif iss.id == CONTRAST_RULE and "ratio" in ev:
        bits.append(f"{ev['ratio']}:1")
    elif iss.id == CLIPPED and "visible_px" in ev:
        bits.append(f"{ev['visible_px']} of {ev.get('declared_px', '?')}px")
    elif iss.id == HIDDEN and ev.get("why"):
        bits.append(str(ev["why"]))
    elif iss.id == OFFSCREEN and ev.get("outside"):
        bits.append(f"outside {ev['outside']}")
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
        _annotate_touch_fp(pairs, render)
        out = [(nid, i) for nid, i in out
               if not i.id.startswith("a11y.") or i.id == CONTRAST_RULE] + pairs
        unmapped.extend(res.unmapped)
    if contrast and not ran:
        res = _cached_lint(src, ix, "contrast", density=density, font_scale=font_scale,
                           wcag=False)
        pairs = [(nid, Issue(i.id, i.sev, dict(i.evidence), i.conf)) for nid, i in res.issues]
        _annotate_touch_fp(pairs, render)
        out = [(nid, i) for nid, i in out if i.id != CONTRAST_RULE] + pairs
        unmapped.extend(res.unmapped)
        status = "sampled" if res.status.startswith("sampled") else res.status
    return out, status, unmapped


def lint_view(ix: Index, loaded: Any, *, rules: Any = None, severity: str = "info",
              within: str | None = None, contrast: bool = False, wcag: bool = False,
              group: str = "rule", per_rule: int = 3, limit: int = 30,
              cursor: str | None = None, max_bytes: int = 4000) -> dict[str, Any]:
    """The ``lint`` tool over one capture (spec 5.9). Default rules are every
    ``a11y.*`` rule; ``render.*`` issues are reported when asked for
    (``rules=["render."]``) and pointed to otherwise."""
    if severity not in R.SEVERITIES:
        raise OpError("bad_args", f"severity must be one of {', '.join(R.SEVERITIES)}")
    if group not in GROUPS:
        raise OpError("bad_args", f"group must be one of {', '.join(GROUPS)}")
    try:
        per_rule = max(1, min(20, int(per_rule)))
        limit = max(1, min(200, int(limit)))
        max_bytes = int(max_bytes)
    except (TypeError, ValueError) as e:
        raise OpError("bad_args", f"per_rule, limit and max_bytes are integers ({e})") from None
    max_bytes = 0 if max_bytes <= 0 else max(500, min(32000, max_bytes))
    selected = R.resolve(rules)
    explicit = selected is not None
    if selected is None:
        selected = [r.id for r in RULES.values() if r.family == "a11y" and not r.planned]
    selected_set = set(selected)

    src = _Src(loaded)
    density = _density(ix, src)
    cid = ix.meta.id if ix.meta else ""
    norm = {"rules": sorted(selected_set) if explicit else None, "severity": severity,
            "within": within, "contrast": bool(contrast), "wcag": bool(wcag), "group": group,
            "per_rule": per_rule, "limit": limit}
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
    counts = {s: 0 for s in R.SEVERITIES}
    for _, iss in kept:
        counts[iss.sev] = counts.get(iss.sev, 0) + 1

    out: dict[str, Any] = {"capture": cid, "counts": counts}
    if any(r.startswith("a11y.") for r in selected_set):
        out["contrast"] = contrast_status
    if within:
        out["within"] = within
    if unmapped:
        out["unmapped"] = len(unmapped)
    available = set(getattr(a11y_lint, "ALL_RULE_IDS", ())) | {
        r.id for r in RULES.values() if r.family == "render" and not r.planned}
    unavailable = [RULES[r].label for r in selected if explicit and r in RULES
                   and r not in available]
    if unavailable:
        out["unavailable"] = unavailable

    budget = Budget(max_bytes, reserve=FOOTER_RESERVE)
    budget.add(json_cost(out) + 40)  # the list key and the closing brace
    items, total, truncated_why = _render_groups(ix, kept, group, per_rule, limit, offset,
                                                 budget)
    out["rules" if group == "rule" else "lines"] = items
    shown_end = offset + len(items)
    nxt: list[str] = []
    if shown_end < total:
        out["truncated"] = {"omitted": total - shown_end, "why": truncated_why,
                            "cursor": _make_cursor(cid, h, shown_end)}
        nxt.append(f'lint(cursor="{out["truncated"]["cursor"]}")')
    if kept:
        nxt.append('image(overlay="lint")')
        focus = _focus_node(ix, kept)
        if focus:
            nxt.append(f'node("{focus}")')
    if not explicit and any(i.id.startswith("render.") for n in ix.nodes.values()
                            for i in n.issues):
        nxt.append('find(issue="render.")')
    if nxt:
        out["next"] = nxt[:3]
    return out


def _focus_node(ix: Index, kept: list[tuple[str, Issue]]) -> str | None:
    """The node most worth a closer look: a likely false positive, else the worst."""
    for nid, iss in kept:
        if iss.evidence.get("note"):
            return _ref(ix.nodes[nid])
    best = max(kept, key=lambda p: R.SEV_RANK.get(p[1].sev, 0))
    return _ref(ix.nodes[best[0]])


def _render_groups(ix: Index, kept: list[tuple[str, Issue]], group: str, per_rule: int,
                   limit: int, offset: int, budget: Budget) -> tuple[list[Any], int, str]:
    """(items for this page, total items, why truncated)."""
    if group == "rule":
        items = _rule_items(ix, kept, per_rule)
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
            if not page and isinstance(item, dict):
                small = dict(item, nodes=item["nodes"][:1])
                if budget.add(json_cost(small) + 1):
                    page.append(small)
            break
        page.append(item)
    return page, total, why


def _line(ix: Index, nid: str, iss: Issue) -> str:
    d = _detail(iss)
    return _node_label(ix, nid) + (f" {d}" if d else "")


def _rule_items(ix: Index, kept: list[tuple[str, Issue]], per_rule: int
                ) -> list[dict[str, Any]]:
    by_rule: dict[str, list[tuple[str, Issue]]] = {}
    for nid, iss in kept:
        by_rule.setdefault(iss.id, []).append((nid, iss))
    items = []
    for rid, members in by_rule.items():
        rule = R.get(rid, members[0][1].sev)
        sev = R.worst(i.sev for _, i in members) or rule.sev
        lines = _collapsed(ix, members)
        shown = lines[:per_rule]
        rest = sum(n for _, n in lines[per_rule:])
        nodes = [s for s, _ in shown]
        if rest:
            nodes.append(f'+{rest} more: lint(rules=["{rule.label}"],group="node")')
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
    for nid, iss in members:
        n = ix.nodes[nid]
        tpl = _template(n.anchor)
        key: tuple = ("src", n.src) if n.src else (("tpl", tpl) if tpl else ("node", nid))
        groups.setdefault(key, []).append((nid, iss))
    out: list[tuple[str, int]] = []
    for key, grp in groups.items():
        if len(grp) == 1 or key[0] == "node":
            out.extend((_line(ix, nid, iss), 1) for nid, iss in grp)
            continue
        ids = [nid for nid, _ in grp]
        first = ix.nodes[ids[0]]
        tpl = _template(first.anchor)
        container = _lca(ix, ids)
        where = f"in {_sel_of(container)} cells" if (container is not None and tpl) else (
            f"under {_sel_of(container)}" if container is not None else "")
        # src plus type ("FeedRow.kt:42 IconButton"), else the template's last segment
        what = (" ".join(x for x in (first.src, first.type) if x) if first.src
                else (tpl.rsplit("/", 1)[-1] if tpl else first.type or ""))
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
