"""talkback/recycler.py: the item info RecyclerView adds only while accessibility is on.

Measured live on TalkBack 17.0 / API 37 (A11yProbe V12 BAD, emulator-5556): a capture taken
with TalkBack off holds no CollectionItemInfo on the RecyclerView's items, while a TalkBack
user (TalkBack on before the list was bound) hears "Message 1. 2 of 21. In list. 21 items".
A walk that turns TalkBack on over the drawn list hears no position at all.
"""

from __future__ import annotations

from inspector_widget import talkback as tb
from inspector_widget.talkback import recycler
from inspector_widget.talkback import speech as S
from inspector_widget.talkback import static

from test_tb_rules import CLICK, FOCUS, SCROLL_FWD, n, root

RV = "androidx.recyclerview.widget.RecyclerView"
BACK = {"id": 0x00002000}


def _list(rows, *, cols=1, actions=(SCROLL_FWD,), items=None, header=True):
    kids = []
    if header:  # an empty item at position 0: V12's (and Thunderbird's) header
        kids.append(n(10, b=(0, 200, 1080, 1)))
        kids[0].pop("important_for_accessibility")
    kids += items if items is not None else [
        n(11 + i, cls="android.widget.TextView", text=f"Message {i + 1}", flags=FOCUS,
          actions=[CLICK], b=(0, 201 + 140 * i, 1080, 140)) for i in range(3)]
    return n(3, cls=RV, flags=("visible_to_user", "scrollable", "focusable"), actions=actions,
             b=(0, 200, 1080, 2000), children=kids,
             collection_info={"row_count": rows, "column_count": cols, "hierarchical": False,
                              "selection_mode": 0})


def _said(tree, key):
    return S.announce(tb.Navigator(tree), tree.node(key)).text


def test_a_service_off_dump_gets_the_positions_a_talkback_user_hears():
    tree = tb.build([root(_list(21))], services="off")
    assert _said(tree, "view:11") == "Message 1. 2 of 21. In list. 21 items"
    assert tree.node("view:12").get("collection_item_info")["row_index"] == 2
    assert recycler.CORRECTION in tree.node("view:12").corrections
    assert [d["kind"] for d in tree.diagnostics] == ["recycler_item_info"]
    # the empty header item counts: the position rule sees it (V12 BAD)
    fs = static.findings(tb.Navigator(tree))
    assert [(f.code, f.node.key, f.evidence["why"]) for f in fs] == [
        ("tb.wrong_announcement", "view:11", "position_counts_silent_item")]


def test_the_dump_itself_is_not_modified():
    rv = _list(21)
    tb.build([root(rv)], services="off")
    assert not any("collection_item_info" in k for k in rv["children"])


def test_a_service_on_dump_is_taken_as_is_and_says_why_positions_are_missing():
    # tb_walk turned TalkBack on over a list already bound: TalkBack gets no positions either
    tree = tb.build([root(_list(21))], services="on")
    assert _said(tree, "view:11") == "Message 1. In list. 21 items"
    assert [d["kind"] for d in tree.diagnostics] == ["recycler_bound_before_service"]
    assert "restart the app with TalkBack on" in tree.diagnostics[0]["message"]


def test_items_that_already_carry_item_info_are_left_alone():
    rv = _list(21)
    for i, k in enumerate(rv["children"]):
        k["collection_item_info"] = {"row_index": 5 + i, "column_index": 0}
    for services in ("on", "off"):
        tree = tb.build([root(rv)], services=services)
        assert tree.diagnostics == []
        assert tree.node("view:11").get("collection_item_info")["row_index"] == 6


def test_a_list_scrolled_from_its_start_gets_no_guessed_positions():
    tree = tb.build([root(_list(21, actions=(SCROLL_FWD, BACK)))], services="off")
    assert _said(tree, "view:11") == "Message 1. In list. 21 items"
    assert [d["kind"] for d in tree.diagnostics] == ["recycler_positions_unknown"]


def test_a_list_holding_every_item_is_exact_even_when_it_can_scroll_back():
    # Thunderbird's message list (live, emulator-5556): 7 items, all attached, the empty
    # header scrolled off the top; TalkBack (on before the app) says "2 of 7" on the first row
    rv = _list(4, actions=(SCROLL_FWD, BACK))
    rv["children"][0]["bounds"]["layout"]["y"] = 190
    tree = tb.build([root(rv)], services="off")
    assert _said(tree, "view:11") == "Message 1. 2 of 4. In list. 4 items"
    assert [d["kind"] for d in tree.diagnostics] == ["recycler_item_info"]


def test_unknown_counts_and_unknown_service_state_are_not_modelled():
    # A11yProbe S1 reports row and column counts of -1, with TalkBack on or off
    assert tb.build([root(_list(-1, cols=-1))], services="off").diagnostics == []
    assert tb.build([root(_list(-1, cols=-1))], services="on").diagnostics == []
    assert tb.build([root(_list(21))]).diagnostics == []  # no a11y-services token


def test_a_grid_gets_span_group_rows_and_span_index_columns():
    cells = [n(11 + i, cls="android.widget.TextView", text=f"Cell {i}", flags=FOCUS,
               actions=[CLICK], b=((i % 2) * 540, 200 + (i // 2) * 300, 540, 300))
             for i in range(4)]
    tree = tb.build([root(_list(2, cols=2, items=cells, header=False))], services="off")
    got = [(tree.node(f"view:{11 + i}").get("collection_item_info")["row_index"],
            tree.node(f"view:{11 + i}").get("collection_item_info")["column_index"])
           for i in range(4)]
    assert got == [(0, 0), (0, 1), (1, 0), (1, 1)]


def test_a_horizontal_list_counts_columns():
    cells = [n(11 + i, cls="android.widget.TextView", text=f"Chip {i}", flags=FOCUS,
               actions=[CLICK], b=(300 * i, 200, 300, 100)) for i in range(3)]
    tree = tb.build([root(_list(1, cols=12, items=cells, header=False))], services="off")
    assert [tree.node(f"view:{11 + i}").get("collection_item_info")["column_index"]
            for i in range(3)] == [0, 1, 2]


def _info(tree, i):
    return tree.node(f"view:{11 + i}").get("collection_item_info")


def test_a_horizontal_grid_gets_no_invented_positions():
    # GridLayoutManager(2, HORIZONTAL), 5 span groups, 2.5 on screen: its item info is
    # (row = span index, column = span group), which x / (width / 5) does not give
    cells = [n(11 + i, cls="android.widget.TextView", text=f"Tile {i}", flags=FOCUS,
               actions=[CLICK], b=((i // 2) * 432, 200 + (i % 2) * 1000, 432, 1000))
             for i in range(6)]
    tree = tb.build([root(_list(2, cols=5, items=cells, header=False))], services="off")
    assert [_info(tree, i) for i in range(6)] == [None] * 6
    assert [d["kind"] for d in tree.diagnostics] == ["recycler_layout_unknown"]
    assert _said(tree, "view:13") == "Tile 2. In grid. 2 rows. 5 columns"
    # the same with the bounds clipped to the list, as a dump has them: its sideways scroll
    # action gives it away
    cells[4]["bounds"]["layout"]["w"] = cells[5]["bounds"]["layout"]["w"] = 216
    sideways = (SCROLL_FWD, {"id": 0x0102003B})  # ACTION_SCROLL_RIGHT
    tree = tb.build([root(_list(2, cols=5, items=cells, header=False, actions=sideways))],
                    services="off")
    assert [_info(tree, i) for i in range(6)] == [None] * 6


def test_a_staggered_grid_gets_no_invented_rows():
    # StaggeredGridLayoutManager(2, VERTICAL): rows = item count, columns = spans; the
    # columns do not form rows, and its real item info has no row (-1)
    tops, cells = [200, 200], []
    for i, h in enumerate([300, 500, 450, 250, 400, 350]):
        col = 0 if tops[0] <= tops[1] else 1
        cells.append(n(11 + i, cls="android.widget.TextView", text=f"Photo {i}", flags=FOCUS,
                       actions=[CLICK], b=(col * 540, tops[col], 540, h)))
        tops[col] += h
    tree = tb.build([root(_list(20, cols=2, items=cells, header=False))], services="off")
    assert [_info(tree, i) for i in range(6)] == [None] * 6
    assert [d["kind"] for d in tree.diagnostics] == ["recycler_layout_unknown"]


def test_a_vertical_grid_with_margins_and_a_full_span_header_is_still_modelled():
    cells = [n(11, cls="android.widget.TextView", text="Header", flags=FOCUS,
               actions=[CLICK], b=(16, 216, 1048, 100))]
    cells += [n(12 + i, cls="android.widget.TextView", text=f"Cell {i}", flags=FOCUS,
                actions=[CLICK], b=(16 + (i % 2) * 540, 332 + (i // 2) * 300, 508, 284))
              for i in range(4)]
    tree = tb.build([root(_list(3, cols=2, items=cells, header=False))], services="off")
    got = [(_info(tree, i)["row_index"], _info(tree, i)["column_index"],
            _info(tree, i)["column_span"]) for i in range(5)]
    assert got == [(0, 0, 2), (1, 0, 1), (1, 1, 1), (2, 0, 1), (2, 1, 1)]
