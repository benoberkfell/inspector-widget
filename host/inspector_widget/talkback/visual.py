"""The order a sighted reader would expect, from geometry alone (a heuristic).

A recursive XY-cut per window: split the stops into horizontal bands at the widest horizontal
whitespace gap and read the bands top to bottom; a band with no horizontal gap splits into
columns at the widest vertical gap, but only when every column holds at least two stops (else
the band is a row, read left to right). Stops that share an accessibility container (a
collection item, a traversal group, a focusable ancestor) are kept together as one block and
ordered inside it the same way. Windows are read in TalkBack's window order.

It is only a guess at intent (``conf: "heuristic"``); an ``expect`` list of keys or labels
replaces it (``conf: "expect"``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .rules import Rules
from .tree import Rect, TbNode, TbTree, build

_Item = Tuple[Rect, List[TbNode]]  # a block: its bounds and its stops, in reading order


def _bbox(rects: Sequence[Rect]) -> Rect:
    r = rects[0]
    for o in rects[1:]:
        r = r.union(o)
    return r


def _gaps(spans: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Whitespace gaps between the merged [start, end) spans: (gap start, gap size).

    Spans that merely touch (the usual LinearLayout / ScrollView case) yield a zero-size
    gap, so abutting rows and columns still get a cut.
    """
    spans = sorted(spans)
    out: List[Tuple[int, int]] = []
    end = spans[0][1]
    for s, e in spans[1:]:
        if s >= end:
            out.append((end, s - end))
        end = max(end, e)
    return out


def _cut(items: List[_Item]) -> List[_Item]:
    if len(items) <= 1:
        return items
    ygaps = _gaps([(r.top, r.bottom) for r, _ in items])
    if ygaps:
        at, _ = max(ygaps, key=lambda g: (g[1], -g[0]))
        top = [it for it in items if it[0].bottom <= at]
        rest = [it for it in items if it[0].bottom > at]
        if top and rest:  # a zero-size item on the boundary can make one side empty
            return _cut(top) + _cut(rest)
    xgaps = _gaps([(r.left, r.right) for r, _ in items])
    if xgaps:
        at, _ = max(xgaps, key=lambda g: (g[1], -g[0]))
        left = [it for it in items if it[0].right <= at]
        right = [it for it in items if it[0].right > at]
        if len(left) >= 2 and len(right) >= 2:
            return _cut(left) + _cut(right)
    side = _sidebar(items)
    if side is not None:
        it, first = side
        rest = [x for x in items if x is not it]
        return [it] + _cut(rest) if first else _cut(rest) + [it]
    return sorted(items, key=lambda it: (it[0].left, it[0].top))


def _sidebar(items: List[_Item]) -> Optional[Tuple[_Item, bool]]:
    """An item that spans the band's full height beside all the others (a leading strip or
    icon, a trailing button column): it blocks every horizontal cut, and a column cut leaves
    it alone on its side. ``(item, True)`` when it leads (read first), ``(item, False)`` when
    it trails (read last); None when there is none. AntennaPod's episode row puts its title
    in a full-height 12px strip's contentDescription, left of the date, size and duration:
    the band reads the strip, then the rest by its own cuts, not column by column."""
    top = min(r.top for r, _ in items)
    bottom = max(r.bottom for r, _ in items)
    for lead in (True, False):
        for it in items:
            r = it[0]
            if r.top > top or r.bottom < bottom:
                continue
            others = [x[0] for x in items if x is not it]
            if lead and all(o.left >= r.right for o in others):
                return it, True
            if not lead and all(o.right <= r.left for o in others):
                return it, False
    return None


def _container(rules: Rules, n: TbNode, stops: set) -> Optional[TbNode]:
    """The accessibility container that keeps ``n`` with its neighbours: the nearest ancestor
    that is a collection item, a traversal group, or a focusable node that is not a stop."""
    for a in n.ancestors():
        if a.get("collection_item_info") or a.has("is_traversal_group") \
                or a.get("is_traversal_group"):
            return a
        if id(a) not in stops and rules.is_focusable_or_clickable(a):
            return a
    return None


def _order(rules: Rules, stops: List[TbNode]) -> List[TbNode]:
    ids = {id(n) for n in stops}
    groups: Dict[int, List[TbNode]] = {}
    anchor: Dict[int, TbNode] = {}
    for n in stops:
        c = _container(rules, n, ids)
        k = id(c) if c is not None else id(n)
        groups.setdefault(k, []).append(n)
        anchor.setdefault(k, n)
    items: List[_Item] = []
    for k, members in groups.items():
        inner = members if len(members) == 1 else [
            m for _, ms in _cut([(m.rect, [m]) for m in members]) for m in ms]
        items.append((_bbox([m.rect for m in members]), inner))
    return [n for _, ns in _cut(items) for n in ns]


def cut_order(boxes: Sequence[Tuple[Rect, Any]]) -> List[Any]:
    """The XY-cut reading order of ``(rect, payload)`` boxes (the cut :func:`visual_order`
    makes), returning the payloads: for callers that group the boxes themselves."""
    return [p for _r, ps in _cut([(r, [p]) for r, p in boxes]) for p in ps]


def visual_order(tree: Any, stops: Optional[Sequence[Any]] = None,
                 expect: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """The heuristic reading-intent order of the stops.

    ``tree``: a :class:`~.tree.TbTree`, a :class:`~.order.Navigator` or a dump. ``stops``:
    the nodes to order (default: every stop of the TalkBack view). ``expect``: keys or labels
    (matched against the stop's key, then a case-insensitive substring of its text or
    contentDescription) giving the intended order instead. Returns ``{"order": [keys],
    "conf": "heuristic" | "expect"}`` (+ ``"unmatched"`` for expectations that matched no stop).
    """
    from .order import Navigator

    nav = tree if isinstance(tree, Navigator) else Navigator(
        tree if isinstance(tree, TbTree) else build(tree))
    if stops is None:
        nodes = nav.linear()
    else:
        nodes = [x for x in (nav.tree.node(s) for s in stops) if x is not None]
    if expect is not None:
        order: List[TbNode] = []
        unmatched: List[str] = []
        pool = list(nodes)
        for label in expect:
            hit = next((n for n in pool if n.key == label), None) or next(
                (n for n in pool if label.lower() in (n.text + " " + n.content_description).lower()
                 or label.lower() in _spoken(nav, n).lower()), None)
            if hit is None:
                unmatched.append(label)
                continue
            pool.remove(hit)
            order.append(hit)
        out: Dict[str, Any] = {"order": [n.key for n in order], "conf": "expect"}
        if unmatched:
            out["unmatched"] = unmatched
        return out
    by_window: Dict[int, List[TbNode]] = {}
    for n in nodes:
        by_window.setdefault(n.window.index, []).append(n)
    ordered: List[TbNode] = []
    for w in nav.windows:
        ordered.extend(_order(nav.rules, by_window.pop(w.index, [])))
    for rest in by_window.values():
        ordered.extend(_order(nav.rules, rest))
    return {"order": [n.key for n in ordered], "conf": "heuristic"}


def _spoken(nav: Any, n: TbNode) -> str:
    from .speech import announce

    return announce(nav, n, transitions=False).text


def order_items(items: Sequence[Dict[str, Any]]) -> List[str]:
    """The same XY-cut over plain boxes, for callers without a TalkBack view (a live walk).

    ``items``: ``[{"key": str, "bounds": (x, y, w, h), "window": int}]``, plus an optional
    ``"container"`` (any hashable) to keep items together. Windows are read top to bottom, then
    left to right, by the box around their items. Returns the keys in reading order.
    """
    by_window: Dict[Any, List[Tuple[Rect, Dict[str, Any]]]] = {}
    for it in items:
        x, y, w, h = it["bounds"]
        by_window.setdefault(it.get("window", 0), []).append((Rect(x, y, x + w, y + h), it))
    boxes = {w: _bbox([r for r, _ in members]) for w, members in by_window.items()}
    out: List[str] = []
    for w in sorted(by_window, key=lambda w: (boxes[w].top, boxes[w].left)):
        groups: Dict[Any, List[Tuple[Rect, Dict[str, Any]]]] = {}
        for r, it in by_window[w]:
            groups.setdefault(it.get("container", id(it)), []).append((r, it))
        blocks = []
        for members in groups.values():
            inner = [it for _, its in _cut([(r, [it]) for r, it in members]) for it in its]
            blocks.append((_bbox([r for r, _ in members]), inner))
        out.extend(it["key"] for _, its in _cut(blocks) for it in its)
    return out
