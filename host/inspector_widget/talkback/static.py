"""What the TalkBack model alone can tell is wrong with a screen (design part 2, basis "model").

:func:`findings` runs over one TalkBack view (:class:`~.order.Navigator` over a
:class:`~.tree.TbTree`) with no device and no walk: the static half of the ``tb.*``
catalogue. A live walk (:mod:`.diff`) confirms what it reports and finds what only a walk
can (traps, focus lost, restore). Precision comes first: a rule stays silent when the model
cannot tell, and every rule is checked against the A11yProbe TalkBack corpus (each BAD
variant raises its finding, no GOOD variant raises any) in tests/test_capture_tb_rules.py.

Codes, with what each looks at:

``tb.double_stop``
    A stop and a stop inside it (a TalkBack-view descendant) that are both clickable, or
    where the outer one already says most of the inner one's words: one item takes two
    swipes. Anchored on the outer stop; ``others`` are the inner ones.
``tb.ghost_stop``
    A stop with nothing useful to say or show: it names nothing ("Button", "Unlabelled";
    not a clipped item TalkBack first scrolls into view), speaks only through invisible
    children (UT/AccessibilityNodeInfoUtils.java:1109), or is under 4dp across.
``tb.out_of_order``
    Stops read against the visual reading order (:mod:`.visual`'s XY-cut, per window): the
    stops outside the longest run that follows it. A stop an app placed with an explicit,
    acyclic traversalBefore/After link is the author's choice and is left alone.
``tb.boundary_jump``
    The same, where the jump crosses a View/Compose boundary (an overlay View read after
    the Compose content under it, AndroidView content read out of place).
``tb.escape``
    A same-window scrim (a clickable node over most of the window) with stops drawn under
    it that TalkBack still reaches: focus walks out of the dialog or sheet. Needs to know
    what is drawn above what (``drawn_above``, from the capture's View tree); without it
    the rule says nothing.
``tb.window_order``
    A window TalkBack reads after the window under it although it sits over that window's
    content (a non-focusable popup sorted by its top edge, WindowTraversal.java:52).
``tb.wrong_announcement``
    A merged stop that reads its children in another order than they are shown ("$5.
    Socks"), or a list whose positions count an item TalkBack never stops on ("2 of 21" on
    the first row).
``tb.edge_stuck``
    Content past a container's edge that nothing TalkBack can scroll brings in (no scroll
    action), or a pager with more pages and no page controls (TalkBack never auto-scrolls a
    pager, FILTER_AUTO_SCROLL, UT:458).
``tb.skipped``
    Visible text TalkBack never gets because an ancestor hides it
    (importantForAccessibility=noHideDescendants), when no overlay covers it.

``tb.custom_action_missing`` needs the composables (the capture's slot table) and is
computed on the capture side (``capture/tb.py``).
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from . import rules as R
from .explain import ghost_reasons
from .order import Navigator
from .speech import Announcement, SpeechState, announce
from .tree import Rect, TbNode, TbWindow
from .visual import cut_order, order_items

CODES = ("tb.double_stop", "tb.ghost_stop", "tb.out_of_order", "tb.boundary_jump",
         "tb.escape", "tb.window_order", "tb.wrong_announcement", "tb.edge_stuck",
         "tb.skipped", "tb.custom_action_missing")
DOUBLE_STOP_OVERLAP = 0.6  # share of the inner stop's words the outer one already says
SCRIM_AREA = 0.6           # a scrim covers at least this share of its window
TINY_DP = 4                # a stop thinner than this has no visible area
MAX_OTHERS = 8
_WORD = re.compile(r"[\w$%]+")
#: words that are the role or state, not the name (they never make two stops "the same")
_ROLE_STATE = {"button", "switch", "check", "box", "checkbox", "image", "edit", "slider",
               "toggle", "radio", "on", "off", "checked", "not", "selected", "disabled",
               "heading", "link", "list", "in", "of", "unlabelled", "expanded", "collapsed"}

#: drawn_above(a, b): True when ``a`` is drawn over ``b``, False when under, None unknown.
DrawnAbove = Callable[[Any, Any], Optional[bool]]
#: view_chain(view id): the View's ancestors in the app's View hierarchy, nearest first, as
#: (view id, (x, y, w, h)); the accessibility dump leaves out the Views TalkBack never gets.
ViewChain = Callable[[int], List[Tuple[int, Tuple[int, int, int, int]]]]


@dataclass
class Finding:
    """One static finding. ``node`` is a TalkBack-view node (or an excluded dump node: it
    has ``.raw``); ``others`` the other nodes involved; ``evidence`` small JSON values."""

    code: str
    sev: str
    node: Any
    others: List[Any] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)
    conf: str = "exact"


class _Ctx:
    def __init__(self, nav: Navigator, density: int, drawn_above: Optional[DrawnAbove],
                 view_chain: Optional[ViewChain] = None):
        self.nav = nav
        self.rules = nav.rules
        self.tree = nav.tree
        self.density = max(1, int(density or 420))
        self.drawn_above = drawn_above
        self.view_chain = view_chain
        self.stops: List[TbNode] = nav.linear()
        self.stop_ids: Set[int] = {id(n) for n in self.stops}
        self._own: Dict[int, Announcement] = {}
        self._walk: Optional[Dict[int, Announcement]] = None
        self.by_key: Dict[str, TbNode] = {}
        for n in self.tree.nodes:
            self.by_key.setdefault(n.key, n)
        self._raw_parent: Dict[int, Optional[Dict[str, Any]]] = {}
        for w in self.tree.windows:
            for raw, parent in _raw_nodes(w):
                self._raw_parent[id(raw)] = parent

    def raw_ancestors(self, raw: Dict[str, Any]) -> Set[int]:
        out: Set[int] = set()
        p = self._raw_parent.get(id(raw))
        while p is not None and id(p) not in out:
            out.add(id(p))
            p = self._raw_parent.get(id(p))
        return out

    def own(self, n: TbNode) -> Announcement:
        a = self._own.get(id(n))
        if a is None:
            a = self._own[id(n)] = announce(self.nav, n, transitions=False)
        return a

    def walk(self, n: TbNode) -> Announcement:
        if self._walk is None:
            st = SpeechState()
            self._walk = {id(x): announce(self.nav, x, st) for x in self.stops}
        return self._walk.get(id(n)) or self.own(n)

    def by_window(self) -> Dict[int, List[TbNode]]:
        out: Dict[int, List[TbNode]] = {}
        for n in self.stops:
            out.setdefault(n.window.index, []).append(n)
        return out

    def dp(self, px: float) -> float:
        return px * 160.0 / self.density



def _words(s: Optional[str]) -> Set[str]:
    return {w.lower() for w in _WORD.findall(s or "") if len(w) > 1}


def _name_words(ann: Announcement) -> Set[str]:
    return _words(ann.text) - _ROLE_STATE


def _is_ancestor(a: TbNode, n: TbNode) -> bool:
    return any(x is a for x in n.ancestors())


def _area(r: Rect) -> int:
    return max(0, r.width) * max(0, r.height)


# ------------------------------------------------------------------------------------------
# tb.double_stop
# ------------------------------------------------------------------------------------------
def _double_stops(cx: _Ctx) -> Iterator[Finding]:
    for a in cx.stops:
        inner = [b for b in cx.stops if b is not a and _is_ancestor(a, b)]
        if not inner:
            continue
        said = _words(cx.own(a).text)
        hits: List[TbNode] = []
        why = ""
        for b in inner:
            if cx.rules.is_clickable(a) and cx.rules.is_clickable(b):
                hits.append(b)
                why = why or "both clickable"
                continue
            mine = _name_words(cx.own(b))
            if mine and len(mine & said) / len(mine) >= DOUBLE_STOP_OVERLAP:
                hits.append(b)
                why = why or "says the inner stop's words"
        if hits:
            yield Finding("tb.double_stop", "warn", a, hits[:MAX_OTHERS],
                          {"inner": len(hits), "why": why})


# ------------------------------------------------------------------------------------------
# tb.ghost_stop
# ------------------------------------------------------------------------------------------
def show_on_screen(nav: Navigator, n: TbNode, forward: bool = True) -> Optional[TbNode]:
    """The scrollable TalkBack asks to bring ``n`` fully into view before it speaks it
    (ensureOnScreen, arriving in that direction), or None."""
    return nav.show_on_screen_container(n, forward, nav.traversal(n.window))


def ghost(nav: Navigator, n: TbNode, density: int = 420) -> List[str]:
    """Ghost reasons of a stop: ``unlabelled``, ``invisible_children_only``, ``tiny`` (under
    4dp across), ``clipped:<scrollable key>`` (a sliver TalkBack cannot scroll into view). A
    clipped item at the edge of a list TalkBack auto-scrolls is not a ghost: TalkBack
    scrolls it fully into view first (ensureOnScreen) and speaks what it then shows."""
    shown_first = show_on_screen(nav, n) is not None
    out: List[str] = []
    for g in ghost_reasons(nav.rules, n):
        if shown_first and (g in ("unlabelled", "invisible_children_only")
                            or g.startswith("clipped:")):
            continue  # what it says once scrolled in is not in this dump
        out.append(g)
    r = n.rect
    if not r.is_empty() and min(r.width, r.height) * 160.0 / max(1, density) < TINY_DP:
        out.append("tiny")
    return out


def _ghosts(cx: _Ctx) -> Iterator[Finding]:
    for n in cx.stops:
        reasons = ghost(cx.nav, n, cx.density)
        if reasons:
            yield Finding("tb.ghost_stop", "warn", n, [],
                          {"why": ",".join(r.split(":")[0] for r in reasons),
                           "said": cx.own(n).text})


# ------------------------------------------------------------------------------------------
# tb.out_of_order / tb.boundary_jump
# ------------------------------------------------------------------------------------------
def _lis(seq: Sequence[int]) -> Set[int]:
    """Indices of one longest strictly increasing subsequence."""
    tails: List[int] = []
    tails_i: List[int] = []
    prev = [-1] * len(seq)
    for i, v in enumerate(seq):
        j = bisect.bisect_left(tails, v)
        if j == len(tails):
            tails.append(v)
            tails_i.append(i)
        else:
            tails[j], tails_i[j] = v, i
        prev[i] = tails_i[j - 1] if j > 0 else -1
    out: Set[int] = set()
    k = tails_i[-1] if tails_i else -1
    while k >= 0:
        out.add(k)
        k = prev[k]
    return out


def _link_cycle(cx: _Ctx, n: TbNode) -> bool:
    """Whether ``n``'s traversalBefore/After link is part of a cycle of such links."""
    seen: Set[int] = set()
    cur: Optional[TbNode] = n
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        nxt = cx.tree.resolve_link(cur.get("traversal_after")) or \
            cx.tree.resolve_link(cur.get("traversal_before"))
        if nxt is n:
            return True
        cur = nxt
    return False


def _authored(cx: _Ctx, n: TbNode) -> bool:
    """Placed by an app's own traversalBefore/After link that resolves to a View with a
    unique id and loops nowhere: the author's order, not a defect. A link to an id that
    several Views share (an <include> copied it) is not: the platform follows the nearest
    copy (View.findViewInsideOutShouldExist), which may not be the one meant."""
    via = cx.nav.traversal(n.window).via(n)
    if not via.startswith(("before:", "after:", "before_of:")):
        return False
    if n.facet not in ("view", "interop"):
        return False  # Compose's own chain says "chain", not before/after
    for x in [n, *n.ancestors()]:
        link = x.get("traversal_after") or x.get("traversal_before")
        if not link:
            continue
        if _link_cycle(cx, x):
            return False
        target = cx.tree.resolve_link(link)
        rid = target.get("view_id_resource_name") if target is not None else None
        if rid and sum(1 for m in cx.tree.nodes if m.window is x.window
                       and m.get("view_id_resource_name") == rid) > 1:
            return False
    return True


def _group(n: TbNode) -> str:
    return "view" if n.facet in ("view", "interop") else (
        "web" if n.facet == "virtual" else "compose")


def _layers(cx: _Ctx, w: TbWindow, stops: List[TbNode]) -> Optional[List[List[TbNode]]]:
    """The window's stops split by what a same-window scrim covers (under it / over it), so
    each layer is ordered on its own; None when a scrim is there and what is drawn above
    what is unknown (a reading order across layers means nothing)."""
    scrims = _scrims(cx, w)
    if not scrims:
        return [stops]
    sc = max(scrims, key=lambda r: _area(_rect_of(r)))
    inside = _subtree_ids(sc)
    over: List[TbNode] = []
    under: List[TbNode] = []
    for s in stops:
        if id(s.raw) in inside:
            over.append(s)
            continue
        above = cx.drawn_above(sc, s.raw) if cx.drawn_above is not None else None
        if above is None:
            return None
        (under if above else over).append(s)
    return [x for x in (over, under) if x]


def _orders(cx: _Ctx) -> Iterator[Finding]:
    by_index = {w.index: w for w in cx.tree.windows}
    for wi, all_stops in cx.by_window().items():
        for stops in _layers(cx, by_index[wi], all_stops) or []:
            yield from _order_layer(cx, stops)


def _group_chain(cx: _Ctx, n: TbNode, root: Optional[Dict[str, Any]]
                 ) -> List[Tuple[Any, Rect, bool]]:
    """The groups above ``n``, nearest first: ``(key, rect, forced)``. Its dump ancestors
    (semantics nodes and the Views the dump holds) and, between two Views of that chain,
    the Views the dump left out (from ``view_chain``, the app's View hierarchy: the
    not-important containers TalkBack never gets, such as a LinearLayout column). A View is
    keyed by its id. ``forced``: a collection item or traversal group, a group whatever its
    size."""
    raws: List[Dict[str, Any]] = []
    x = cx._raw_parent.get(id(n.raw))
    while x is not None and x is not root:
        raws.append(x)
        x = cx._raw_parent.get(id(x))

    def view_id(r: Optional[Dict[str, Any]]) -> Optional[int]:
        if r is None or int(r.get("virtual_id", -1)) != -1 or not r.get("host_view_id"):
            return None
        return int(r["host_view_id"])

    out: List[Tuple[Any, Rect, bool]] = []
    seen: Set[Any] = set()

    def add(key: Any, r: Rect, forced: bool) -> None:
        if key not in seen:
            seen.add(key)
            out.append((key, r, forced))

    below: Optional[Dict[str, Any]] = n.raw
    for x in raws + [root]:
        vb, vx = view_id(below), view_id(x)
        if cx.view_chain is not None and vb is not None and vx is not None:
            hidden = []
            for vid, (gx, gy, gw, gh) in cx.view_chain(vb):
                if vid == vx:
                    for v, r in hidden:
                        add(("v", v), r, False)
                    break
                hidden.append((vid, Rect(gx, gy, gx + gw, gy + gh)))
        if x is None or x is root:
            break
        fl = set(x.get("flags") or ())
        add(("v", vx) if vx is not None else ("r", id(x)), _rect_of(x),
            bool(x.get("collection_item_info")) or "is_traversal_group" in fl)
        below = x
    return out


def _visual(cx: _Ctx, items: List[TbNode]) -> List[TbNode]:
    """The order a sighted reader expects (heuristic): an XY-cut (:mod:`.visual`) applied
    level by level down the groups that hold two or more of ``items``, so a card, a list
    row, a column of Views, a traversal group or a list reads as one block. A group is any
    node of the dump (View or semantics node, important or not) under the window root that
    is not most of the window, or that is a collection item or traversal group. A group
    that another box at its level overlaps opens up: what floats over it is read among
    what it covers."""
    win = items[0].window
    area = max(1, _area(win.bounds))
    root = win.root.raw if win.root is not None else None
    chains: Dict[int, List[Tuple[Any, Rect, bool]]] = {}
    count: Dict[Any, int] = {}
    for n in items:
        chain = _group_chain(cx, n, root)
        chains[id(n)] = chain
        for key, _r, _f in chain:
            count[key] = count.get(key, 0) + 1

    def is_group(key: Any, r: Rect, forced: bool) -> bool:
        if count.get(key, 0) < 2:
            return False
        return forced or _area(r.intersect(win.bounds)) < 0.9 * area

    tree: Dict[Any, List[Any]] = {0: []}
    rects: Dict[Any, Rect] = {}
    for n in items:
        groups = [g for g in reversed(chains[id(n)]) if is_group(*g)]  # outermost first
        parent: Any = 0
        for key, r, _f in groups:
            if key not in tree:
                tree[key] = []
                rects[key] = r
                tree[parent].append(("g", key))
            parent = key
        tree[parent].append(("n", n))

    def expand(key: Any) -> List[TbNode]:
        entries = list(tree[key])
        # a group another box overlaps is no visual block: a View floating over a list
        # or a ComposeView is read among what it covers, so the group opens up
        while True:
            boxes = [(rects[x] if kind == "g" else x.rect, (kind, x)) for kind, x in entries]
            hit = next((i for i, (r, (kind, _x)) in enumerate(boxes) if kind == "g" and any(
                j != i and not o.is_empty() and r.intersects(o)
                for j, (o, _p) in enumerate(boxes))), None)
            if hit is None:
                break
            entries[hit:hit + 1] = tree[entries[hit][1]]
        out: List[TbNode] = []
        for kind, x in cut_order(boxes):
            out.extend(expand(x) if kind == "g" else [x])
        return out

    return expand(0)


def _order_layer(cx: _Ctx, stops: List[TbNode]) -> Iterator[Finding]:
    # a stop that holds other stops has no place of its own in a reading order
    items = [s for s in stops if not s.rect.is_empty() and not any(
        o is not s and s.rect.contains(o.rect) and not o.rect.is_empty() for o in stops)]
    if len(items) < 3:
        return
    visual = _visual(cx, items)
    rank = {id(n): i for i, n in enumerate(visual)}
    keep = _lis([rank[id(n)] for n in items])
    for i, n in enumerate(items):
        if i in keep or _authored(cx, n):
            continue
        prev = items[i - 1] if i > 0 else None
        nxt = items[i + 1] if i + 1 < len(items) else None
        crosses = any(o is not None and _group(o) != _group(n) for o in (prev, nxt))
        yield Finding("tb.boundary_jump" if crosses else "tb.out_of_order", "warn", n,
                      [prev] if prev is not None else [],
                      {"read": i + 1, "visual": rank[id(n)] + 1, "of": len(items)},
                      conf="heuristic")


# ------------------------------------------------------------------------------------------
# tb.escape / tb.skipped (same-window overlays)
# ------------------------------------------------------------------------------------------
def _raw_nodes(w: TbWindow) -> Iterator[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]]:
    """(dump node, its dump parent) over a window's whole dump subtree."""
    if w.root is None:
        return
    stack: List[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]] = [(w.root.raw, None)]
    while stack:
        n, p = stack.pop()
        yield n, p
        stack.extend((c, n) for c in reversed(n.get("children") or ()))


def _rect_of(raw: Dict[str, Any]) -> Rect:
    return Rect.of(raw.get("bounds"))


def _clickable_raw(raw: Dict[str, Any]) -> bool:
    fl = set(raw.get("flags") or ())
    if "clickable" in fl:
        return True
    return any(isinstance(a, dict) and a.get("id") == R.ACTION_CLICK
               for a in raw.get("actions") or ())


def _scrims(cx: _Ctx, w: TbWindow) -> List[Dict[str, Any]]:
    """Clickable, visible, non-scrolling dump nodes over most of the window (not its root)
    with no text of their own: what a sheet or a hand-made dialog puts between itself and
    the screen behind (a Compose scrim may hold the dialog: its children)."""
    area = max(1, _area(w.bounds))
    out = []
    for raw, parent in _raw_nodes(w):
        if parent is None or "visible_to_user" not in (raw.get("flags") or ()):
            continue
        if not _clickable_raw(raw) or "scrollable" in (raw.get("flags") or ()):
            continue
        if raw.get("text") or raw.get("content_description"):
            continue
        if _area(_rect_of(raw).intersect(w.bounds)) >= SCRIM_AREA * area:
            out.append(raw)
    return out


def _subtree_ids(raw: Dict[str, Any]) -> Set[int]:
    out: Set[int] = set()
    stack = [raw]
    while stack:
        n = stack.pop()
        out.add(id(n))
        stack.extend(n.get("children") or ())
    return out


def _center_in(r: Rect, o: Rect) -> bool:
    cx, cy = (r.left + r.right) / 2, (r.top + r.bottom) / 2
    return o.left <= cx < o.right and o.top <= cy < o.bottom


def _under(cx: _Ctx, scrim: Dict[str, Any], raws: Sequence[Dict[str, Any]]) -> List[Any]:
    """The dump nodes of ``raws`` drawn under ``scrim`` (their centre inside it), as far as
    ``drawn_above`` can tell; empty without it."""
    if cx.drawn_above is None:
        return []
    inside = _subtree_ids(scrim)
    above_it = cx.raw_ancestors(scrim)
    sr = _rect_of(scrim)
    out = []
    for raw in raws:
        if id(raw) in inside or id(raw) in above_it or not _center_in(_rect_of(raw), sr):
            continue
        if cx.drawn_above(scrim, raw) is True:
            out.append(raw)
    return out


def _escapes(cx: _Ctx) -> Iterator[Finding]:
    for w in cx.nav.windows:
        if not w.reported:
            continue
        stops = [n for n in cx.stops if n.window is w]
        for scrim in _scrims(cx, w):
            under = _under(cx, scrim, [n.raw for n in stops])
            if not under:
                continue
            inside = _subtree_ids(scrim)
            if not any(n.raw is not scrim and (id(n.raw) in inside
                                               or cx.drawn_above(n.raw, scrim) is True)
                       for n in stops):
                continue  # nothing over the scrim: no dialog or sheet to walk out of
            node = cx.tree.by_raw.get(id(scrim)) or cx.tree.excluded_by_raw.get(id(scrim))
            if node is None:
                continue
            others = [cx.tree.by_raw[id(r)] for r in under]
            pct = round(100 * _area(_rect_of(scrim).intersect(w.bounds))
                        / max(1, _area(w.bounds)))
            yield Finding("tb.escape", "error", node, others[:MAX_OTHERS],
                          {"under": len(others), "area_pct": pct})


def _skipped(cx: _Ctx) -> Iterator[Finding]:
    """Visible text an ancestor hides from TalkBack (noHideDescendants), one finding per
    hiding node. Hidden content under a scrim of its window is left alone: hiding what an
    overlay covers is the fix for tb.escape (when what is drawn above what is unknown, any
    scrim over it counts)."""
    groups: Dict[int, Tuple[Any, List[Dict[str, Any]]]] = {}
    for ex in cx.tree.excluded:
        if ex.reason != "hidden" or not ex.window.reported:
            continue
        raw = ex.raw
        text = raw.get("text") or raw.get("content_description")
        if not text or "visible_to_user" not in (raw.get("flags") or ()):
            continue
        r = _rect_of(raw)
        if r.is_empty() or not r.intersects(ex.window.bounds):
            continue
        groups.setdefault(id(ex.hidden_by), (ex, []))[1].append(raw)
    scrims: Dict[int, List[Dict[str, Any]]] = {}
    for _top, (ex, raws) in groups.items():
        w = ex.window
        if w.index not in scrims:
            scrims[w.index] = _scrims(cx, w)
        top = ex.hidden_by
        covered = False
        for sc in scrims[w.index]:
            if not _center_in(_rect_of(top), _rect_of(sc)) or id(top) in _subtree_ids(sc):
                continue
            above = cx.drawn_above(sc, top) if cx.drawn_above is not None else None
            if above is not False:
                covered = True
                break
        if covered:
            continue
        node = cx.tree.excluded_by_raw.get(id(top))
        if node is None:
            continue
        first = raws[0].get("text") or raws[0].get("content_description") or ""
        yield Finding("tb.skipped", "warn", node, [],
                      {"texts": len(raws), "first": first[:40], "why": "noHideDescendants"})


# ------------------------------------------------------------------------------------------
# tb.window_order
# ------------------------------------------------------------------------------------------
def _window_orders(cx: _Ctx) -> Iterator[Finding]:
    wins = [w for w in cx.nav.windows if w.reported and cx.nav.accepts_window(w)]
    for i, w in enumerate(wins):
        if i == 0 or w.focusable or w.root is None:
            continue
        below = [m for m in wins[:i] if m.index < w.index]  # read before, drawn under
        lower = [s for s in cx.stops if any(s.window is m for m in below)
                 and s.rect.top >= w.bounds.top]
        first = next((s for s in cx.stops if s.window is w), None)
        if lower and first is not None:
            yield Finding("tb.window_order", "warn", first, lower[:MAX_OTHERS],
                          {"read_after": len(lower), "window_top": w.bounds.top})


# ------------------------------------------------------------------------------------------
# tb.wrong_announcement
# ------------------------------------------------------------------------------------------
def _speech_order(cx: _Ctx) -> Iterator[Finding]:
    for n in cx.stops:
        ann = cx.own(n)
        parts = []
        for p in ann.parts:
            if p.get("kind") != "child":
                continue
            src = cx.by_key.get(p.get("from") or "")
            if src is None or src is n or src.rect.is_empty() or not src.visible:
                continue
            parts.append((p["text"], src))
        if len(parts) < 2 or len({id(s) for _t, s in parts}) < len(parts):
            continue
        rects = [s.rect for _t, s in parts]
        if any(a.intersects(b) for i, a in enumerate(rects) for b in rects[i + 1:]):
            continue  # overlapping texts: no reading order to compare
        shown = order_items([{"key": str(i), "bounds": (r.left, r.top, r.width, r.height)}
                             for i, r in enumerate(rects)])
        if shown != [str(i) for i in range(len(parts))]:
            yield Finding("tb.wrong_announcement", "warn", n,
                          [parts[int(k)][1] for k in shown][:MAX_OTHERS],
                          {"said": ann.text[:60],
                           "shown": " … ".join(parts[int(k)][0] for k in shown)[:60],
                           "why": "speech_order"})


def _positions(cx: _Ctx) -> Iterator[Finding]:
    for c in cx.tree.nodes:
        ci = c.get("collection_info")
        if not ci or not c.window.reported or not cx.rules.filter_collection(c):
            continue
        items = [k for k in c.children if k.get("collection_item_info")]
        stops = [k for k in items if id(k) in cx.stop_ids]
        if not stops:
            continue
        silent = [k for k in items if id(k) not in cx.stop_ids and k.visible
                  and not any(id(d) in cx.stop_ids for d in k.iter())
                  and not any(d.text or d.content_description for d in k.iter())]
        if not silent:
            continue
        first = stops[0]
        said = cx.walk(first).text
        yield Finding("tb.wrong_announcement", "warn", first, silent[:MAX_OTHERS],
                      {"said": said[:60], "silent_items": len(silent),
                       "why": "position_counts_silent_item"})


# ------------------------------------------------------------------------------------------
# tb.edge_stuck
# ------------------------------------------------------------------------------------------
_FORWARD_ACTIONS = (R.ACTION_SCROLL_FORWARD, R.ACTION_SCROLL_DOWN, R.ACTION_SCROLL_RIGHT,
                    R.ACTION_PAGE_DOWN, R.ACTION_PAGE_RIGHT)


def _past_edge(cx: _Ctx) -> Iterator[Finding]:
    """Content clipped at a container's edge (no area there), in a container nothing
    TalkBack scrolls: rows moved by translation, a scroller without scroll actions."""
    for p in cx.tree.nodes:
        if not p.window.reported or not p.visible or p.rect.is_empty():
            continue
        kids = p.children
        shown = [k for k in kids if k.visible and not k.rect.is_empty()
                 and (k.text or k.content_description or id(k) in cx.stop_ids)]
        cut = [k for k in kids if not k.visible and (k.text or k.content_description)
               and (k.rect.is_empty() or not k.rect.intersects(p.rect))
               and k.rect.top >= p.rect.bottom - 2]
        if len(cut) < 2 or len(shown) < 2:
            continue
        if any(cx.rules.filter_auto_scroll(a) for a in [p, *p.ancestors()]):
            continue  # TalkBack scrolls it in
        yield Finding("tb.edge_stuck", "warn", p, [], {
            "past_edge": len(cut), "first": (cut[0].text or cut[0].content_description)[:40],
            "why": "no_scroll_action"})


def _pagers(cx: _Ctx) -> Iterator[Finding]:
    for p in cx.tree.nodes:
        if not p.window.reported or not p.visible or cx.rules.role(p) != R.ROLE_PAGER:
            continue
        if not p.supports(*_FORWARD_ACTIONS):
            continue  # nothing further to page to (or the app pages it some other way)
        inside = {id(x) for x in p.iter()}
        controls = [s for s in cx.stops if s.window is p.window and id(s) not in inside
                    and (s.has("selected") or cx.rules.role(s) == R.ROLE_TAB_BAR
                         or any(a.has("selected") for a in s.ancestors()))]
        if controls:
            continue  # tabs or page buttons reach the other pages
        last = [s for s in cx.stops if id(s) in inside]
        yield Finding("tb.edge_stuck", "warn", p, last[-1:], {"why": "pager"})


# ------------------------------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------------------------------
_CHECKS = (_double_stops, _ghosts, _orders, _escapes, _skipped, _window_orders, _speech_order,
           _positions, _past_edge, _pagers)


def findings(nav: Navigator, *, density: int = 420,
             drawn_above: Optional[DrawnAbove] = None,
             view_chain: Optional[ViewChain] = None,
             codes: Optional[Sequence[str]] = None) -> List[Finding]:
    """Every static finding over ``nav``'s TalkBack view.

    ``density``: the device dpi (sizes in dp). ``drawn_above(a, b)``: True when dump node
    ``a`` is drawn over dump node ``b`` (the capture's View tree knows; see
    ``capture/tb.py``), else False/None; without it ``tb.escape`` stays silent.
    ``view_chain(view id)``: the View's ancestors (the capture's View tree), so the visual
    order keeps a column or row of Views together even when the dump leaves the
    container out. ``codes``: only these rules."""
    cx = _Ctx(nav, density, drawn_above, view_chain)
    want = set(codes) if codes else None
    out: List[Finding] = []
    for check in _CHECKS:
        for f in check(cx):
            if want is None or f.code in want:
                out.append(f)
    return out


__all__ = ["CODES", "Finding", "findings", "ghost", "show_on_screen"]
