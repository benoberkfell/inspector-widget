"""RecyclerView items as TalkBack gets them: the item info a service-off dump lacks.

RecyclerView gives an item View its accessibility delegate
(``RecyclerViewAccessibilityDelegate.ItemDelegate``) only when it binds the item while
accessibility is enabled (``RecyclerView.attachAccessibilityDelegateOnBind``, guarded by
``isAccessibilityEnabled()``). That delegate is what adds the item's CollectionItemInfo
(``LayoutManager.onInitializeAccessibilityNodeInfoForItem``: the adapter position as the row
of a vertical list or the column of a horizontal one; GridLayoutManager: span group and span
index), which TalkBack speaks as "2 of 21". Measured on TalkBack 17.0 / API 37
(emulator-5556, A11yProbe V12 BAD):

* A dump taken with no service on holds no item info at all. A TalkBack user (TalkBack on
  before the list was bound) hears "Message 1. 2 of 21. In list. 21 items"; the dump alone
  says "Message 1. In list. 21 items". :func:`apply_item_info` adds the item info the
  delegate would, so the model speaks and lints what the TalkBack user gets.
* A dump taken WITH a service on whose items still have none: the service started after the
  items were bound (``tb_walk`` turning TalkBack on over a list already on screen). The real
  TalkBack then gets no positions for those items either (the V12 walk said "Message 1. In
  list. 21 items") until they are rebound. Nothing is added; a diagnostic says why a walk
  misses the "N of M" a TalkBack user hears, and how to see it (start the app with TalkBack
  on).

Positions count from the first child. They are exact when the list is at its start (it
offers no backward scroll) or holds every item as a child (as many as its count); any other
list scrolled away from its start gets no item info and a diagnostic instead (the adapter
position of its first child is not in the dump). A list whose
row or column count is unknown (-1, as A11yProbe S1 reports with TalkBack on or off) is left
alone. A grid (rows and columns both over 1) is modelled only when its geometry is clearly
a vertical GridLayoutManager's: no horizontal scroll action, no child past its left or right
edge, rows stacked (each row band ends before the next begins, which a
StaggeredGridLayoutManager's columns do not, whose items TalkBack hears with no row at all)
and each band's cells on its column grid; anything else (a horizontal grid, whose rows are
the span index and columns the span group; a staggered grid) gets no item info and a
``recycler_layout_unknown`` diagnostic. A reverse layout cannot be told from the dump (its
children are in visual order but count down); at rest at its start it offers backward scroll
and so gets no positions, unless it holds every item.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

ITEM_PARENTS = ("RecyclerView", "WearableRecyclerView")  # a11y._ITEM_PARENTS
_BACKWARD = (0x00002000, 0x01020038, 0x01020039, 0x01020046, 0x01020048)
#: SCROLL_LEFT, SCROLL_RIGHT, PAGE_LEFT, PAGE_RIGHT: a list that scrolls sideways
_SIDEWAYS = (0x01020039, 0x0102003B, 0x01020048, 0x01020049)
_TOL = 2  # px
#: ``TbNode.corrections`` mark on an item given the delegate's info
CORRECTION = "recycler_item_info"


def _simple(cls: str) -> str:
    return (cls or "").rsplit(".", 1)[-1]


def _lists(tree: Any, unknown: Optional[List[str]] = None) -> List[Any]:
    """``(RecyclerView, its item nodes, their item info)`` for every RecyclerView whose
    layout is modelled (row and column counts known) and whose items carry no item info;
    ``unknown`` collects the keys of grids whose layout is not one modelled."""
    out = []
    for n in tree.nodes:
        if n.facet not in ("view", "interop") or _simple(n.class_name) not in ITEM_PARENTS:
            continue
        if not n.get("collection_info"):
            continue
        kids = [c for c in n.children if c.facet in ("view", "interop")]
        if not kids or any(c.get("collection_item_info") for c in kids):
            continue
        infos = _infos(n, kids)
        if infos is not None:
            out.append((n, kids, infos))
        elif unknown is not None and _is_grid(n):
            unknown.append(n.key)
    return out


def _is_grid(rv: Any) -> bool:
    ci = rv.get("collection_info") or {}
    return int(ci.get("row_count", -1)) > 1 and int(ci.get("column_count", -1)) > 1


def _vertical_grid(rv: Any, kids: List[Any], cols: int, cell: float,
                   band: Dict[int, int]) -> bool:
    """Whether the attached items lie as a vertical GridLayoutManager lays them out."""
    if rv.supports(*_SIDEWAYS):
        return False  # it scrolls sideways: a horizontal grid
    left, right = rv.rect.left, rv.rect.right
    if any(k.rect.left < left - _TOL or k.rect.right > right + _TOL for k in kids):
        return False  # a child past a side edge: the grid scrolls sideways
    rows: Dict[int, List[Any]] = {}
    for k in kids:
        rows.setdefault(band[k.rect.top], []).append(k)
    order = sorted(rows)
    for i, b in enumerate(order[:-1]):
        nxt = min(k.rect.top for k in rows[order[i + 1]])
        if any(k.rect.bottom > nxt + _TOL for k in rows[b]):
            return False  # columns not in rows: a staggered grid
    for b in order:
        spans = 0
        for k in rows[b]:
            w = k.rect.width / cell
            edge = k.rect.left <= left + _TOL or k.rect.right >= right - _TOL
            if not edge and abs(w - round(w)) > 0.25:
                return False  # a cell off the column grid
            spans += max(1, int(round(w)))
        if spans > cols:
            return False
    return True


def _bands(vals: List[int], tol: int = 2) -> Dict[int, int]:
    """Each distinct coordinate's band index (coordinates within ``tol`` px share one)."""
    out: Dict[int, int] = {}
    band, last = -1, None
    for v in sorted(set(vals)):
        if last is None or v - last > tol:
            band += 1
        out[v] = band
        last = v
    return out


def _infos(rv: Any, kids: List[Any]) -> Optional[List[Dict[str, Any]]]:
    """The CollectionItemInfo of each item, or None when the layout is not one modelled."""
    ci = rv.get("collection_info") or {}
    rows, cols = int(ci.get("row_count", -1)), int(ci.get("column_count", -1))

    def info(r: int, c: int, cspan: int = 1) -> Dict[str, Any]:
        return {"row_index": r, "column_index": c, "row_span": 1, "column_span": cspan,
                "heading": False, "selected": False}

    if cols == 1 or (rows > 1 and cols <= 0):  # LinearLayoutManager, vertical
        return [info(i, 0) for i in range(len(kids))]
    if rows == 1 and cols > 1:  # LinearLayoutManager, horizontal
        return [info(0, i) for i in range(len(kids))]
    if rows > 1 and cols > 1:  # GridLayoutManager, vertical: span group and span index
        width = rv.rect.width
        if width <= 0:
            return None
        cell = width / cols
        band = _bands([k.rect.top for k in kids])
        if not _vertical_grid(rv, kids, cols, cell, band):
            return None  # horizontal or staggered: not modelled
        out = []
        for k in kids:
            col = max(0, min(cols - 1, int((k.rect.left - rv.rect.left + cell / 2) // cell)))
            span = max(1, min(cols - col, int(round(k.rect.width / cell)) or 1))
            out.append(info(band[k.rect.top], col, span))
        return out
    return None


def _all_attached(rv: Any, kids: List[Any]) -> bool:
    """Every adapter item is a child (a list as long as its count): the first child is
    position 0 even though the list can scroll back (Thunderbird's message list, whose
    empty header item sits scrolled off its top)."""
    ci = rv.get("collection_info") or {}
    rows, cols = int(ci.get("row_count", -1)), int(ci.get("column_count", -1))
    count = rows if cols == 1 else cols if rows == 1 else -1
    return count > 0 and len(kids) == count


def apply_item_info(tree: Any) -> None:
    """Give RecyclerView items the item info TalkBack gets (``tree.services == "off"``), or
    say why a service-on dump has none. The dump is not modified: a corrected item carries
    the info in ``TbNode.extra`` (``TbNode.get`` reads it), and its ``raw`` stays the dump's
    own dict, so what is keyed on the dump's identity (a scrim's subtree, the dump parents
    talkback/static.py walks) still finds the item."""
    unknown: List[str] = []
    found = _lists(tree, unknown)
    if unknown and tree.services == "off":
        tree.diagnostics.append({
            "kind": "recycler_layout_unknown", "count": len(unknown), "keys": unknown[:10],
            "message": (f"{len(unknown)} RecyclerView grid(s) not laid out as a vertical "
                        "grid (a horizontal or staggered one): the model gives their items "
                        "no row/column (\"Row 1. Column 2\"), which TalkBack would hear "
                        "while it runs."),
        })
    if not found:
        return
    if tree.services == "on":
        n_items = sum(len(k) for _rv, k, _i in found)
        tree.diagnostics.append({
            "kind": "recycler_bound_before_service", "count": len(found),
            "keys": [rv.key for rv, _k, _i in found][:10],
            "message": (f"{n_items} RecyclerView item(s) have no row/column info although "
                        "TalkBack is on: they were bound before it started, so TalkBack says "
                        "no \"N of M\" for them until they are rebound; a user who had "
                        "TalkBack on before the app hears it. To walk that, restart the app "
                        "with TalkBack on."),
        })
        return
    if tree.services != "off":
        return
    added, scrolled = 0, []
    for rv, kids, infos in found:
        if rv.supports(*_BACKWARD) and not _all_attached(rv, kids):
            scrolled.append(rv.key)
            continue
        for k, inf in zip(kids, infos):
            k.extra["collection_item_info"] = inf
            k.corrections.append(CORRECTION)
            added += 1
    if added:
        tree.diagnostics.append({
            "kind": "recycler_item_info", "count": added,
            "message": (f"The dump was taken with no accessibility service on; {added} "
                        "RecyclerView item(s) are given the row/column info RecyclerView adds "
                        "while TalkBack runs (\"N of M\")."),
        })
    if scrolled:
        tree.diagnostics.append({
            "kind": "recycler_positions_unknown", "count": len(scrolled), "keys": scrolled[:10],
            "message": (f"{len(scrolled)} RecyclerView(s) scrolled away from their start in a "
                        "dump taken with no service on: their items' positions (\"N of M\") "
                        "are not in the dump, so the model says none."),
        })


def rows_bound(tree: Any) -> Optional[str]:
    """When a service-on dump's RecyclerView rows were bound, relative to the service:
    ``"before"`` it started (a list with CollectionInfo with a row that has no item info:
    bound before, or not rebound since), ``"after"`` (every row of every such list has
    it: the screen's lists were bound with TalkBack running, as a TalkBack user gets
    them), or None (no such list with rows, or a dump with no service on, whose item
    info the model adds itself)."""
    if tree.services == "off":
        return None
    before = after = False
    for n in tree.nodes:
        if n.facet not in ("view", "interop") or _simple(n.class_name) not in ITEM_PARENTS:
            continue
        if not n.get("collection_info"):
            continue
        kids = [c for c in n.children if c.facet in ("view", "interop")]
        if not kids:
            continue
        if all(c.get("collection_item_info") for c in kids):
            after = True
        else:
            before = True
    return "before" if before else "after" if after else None
