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

Positions count from the first child. They are exact only when the list is at its start
(it offers no backward scroll); a list scrolled away from its start gets no item info and a
diagnostic instead (the adapter position of its first child is not in the dump). A list whose
row or column count is unknown (-1, as A11yProbe S1 reports with TalkBack on or off) is left
alone, as is a horizontal grid.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

ITEM_PARENTS = ("RecyclerView", "WearableRecyclerView")  # a11y._ITEM_PARENTS
_BACKWARD = (0x00002000, 0x01020038, 0x01020039, 0x01020046, 0x01020048)
#: ``TbNode.corrections`` mark on an item given the delegate's info
CORRECTION = "recycler_item_info"


def _simple(cls: str) -> str:
    return (cls or "").rsplit(".", 1)[-1]


def _lists(tree: Any) -> List[Any]:
    """``(RecyclerView, its item nodes, their item info)`` for every RecyclerView whose
    layout is modelled (row and column counts known) and whose items carry no item info."""
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
    return out


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
        out = []
        for k in kids:
            col = max(0, min(cols - 1, int((k.rect.left - rv.rect.left + cell / 2) // cell)))
            span = max(1, min(cols - col, int(round(k.rect.width / cell)) or 1))
            out.append(info(band[k.rect.top], col, span))
        return out
    return None


def apply_item_info(tree: Any) -> None:
    """Give RecyclerView items the item info TalkBack gets (``tree.services == "off"``), or
    say why a service-on dump has none. The dump is not modified: a corrected item's
    ``raw`` becomes a copy (``tree.by_raw`` knows it under both)."""
    found = _lists(tree)
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
        if rv.supports(*_BACKWARD):
            scrolled.append(rv.key)
            continue
        for k, inf in zip(kids, infos):
            orig = k.raw
            k.raw = dict(orig, collection_item_info=inf)
            tree.by_raw[id(k.raw)] = k
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
