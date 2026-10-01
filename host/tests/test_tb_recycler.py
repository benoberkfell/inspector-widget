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


def test_a_service_off_list_inside_a_scrim_still_escapes_it():
    # A hand-made dialog: a full-window clickable scrim holds the list, a button lies under
    # it. The item info a service-off dump gets must not cost the items their place in the
    # dump (the scrim's subtree is keyed on the dump's own dicts): tb.escape either way.
    def screen():
        behind = n(4, cls="android.widget.Button", text="Compose", flags=FOCUS, actions=[CLICK],
                   b=(0, 2300, 1080, 100))
        scrim = n(9, flags=("visible_to_user", "clickable", "focusable"), actions=[CLICK],
                  b=(0, 0, 1080, 2400), children=[_list(21)])
        return root(n(2, cls="android.widget.TextView", text="Inbox", b=(0, 0, 1080, 150)),
                    behind, scrim), scrim

    for services in ("on", "off"):
        r, scrim = screen()

        def above(a, b, scrim=scrim):
            return True if a is scrim else (False if b is scrim else None)

        tree = tb.build([r], services=services)
        fs = static.findings(tb.Navigator(tree), drawn_above=above, codes=["tb.escape"])
        assert [(f.code, f.node.key) for f in fs] == [("tb.escape", "view:9")], services
        assert {o.key for o in fs[0].others} == {"view:2", "view:4"}
    # and the corrected items keep the dump's own dicts
    assert tree.node("view:11").raw is r["children"][2]["children"][0]["children"][1]


# ------------------------------------------------------------- G20: ViewPager2 and late binding
VP = "androidx.viewpager.widget.ViewPager"


def _pager():
    """ViewPager2 as the dump holds it: the pager (its node reports ViewPager's class and the
    RecyclerView's CollectionInfo), its RecyclerView not important, two page roots (plain
    FrameLayouts, AUTO) each holding a title and a button."""
    def page(host, x):
        kids = [n(host + 1, cls="android.widget.TextView", text=f"Title {host}",
                  b=(x + 10, 300, 500, 80)),
                n(host + 2, cls="android.widget.Button", text="Play", flags=FOCUS,
                  actions=[CLICK], b=(x + 10, 400, 300, 120))]
        p = n(host, cls="android.widget.FrameLayout", b=(x, 200, 1080, 1800), children=kids)
        p.pop("important_for_accessibility")  # AUTO: nothing makes it important by itself
        return p

    ci = {"row_count": 1, "column_count": 4, "hierarchical": False, "selection_mode": 0}
    rv = n(5, cls=RV, flags=("visible_to_user", "scrollable", "focusable"),
           actions=[SCROLL_FWD], b=(0, 200, 1080, 1800), children=[page(20, 0), page(30, 1080)],
           collection_info=ci, important_for_accessibility="NO")
    return n(4, cls=VP, flags=("visible_to_user", "scrollable"), actions=[SCROLL_FWD],
             b=(0, 200, 1080, 1800), children=[rv], collection_info=dict(ci))


def test_viewpager2_pages_get_the_item_info_a_service_adds():
    # A service-off dump: ViewPager2's RecyclerView is not important, so TalkBack gets the
    # pages under the pager; they still are its items (column = page index)
    tree = tb.build([root(_pager())], services="off")
    assert tree.node("view:20").get("collection_item_info")["column_index"] == 0
    assert tree.node("view:30").get("collection_item_info")["column_index"] == 1
    assert [d["kind"] for d in tree.diagnostics] == ["recycler_item_info"]
    # the page root is a stop of its own ("Page" ...) as for a TalkBack-first user (AP-4)
    assert "view:20" in tb.simulate(tree).keys()


def test_viewpager2_pages_bound_before_the_service_are_no_stops_and_say_why():
    # AntennaPod's episode pager with TalkBack turned on after the app (G20, w9wtb7e): the
    # pages carry no item info and stay not important, so TalkBack reads their children
    tree = tb.build([root(_pager())], services="on")
    assert tree.node("view:20") is None  # hoisted: its children take its place
    assert [x.key for x in tree.node("view:4").children][:2] == ["view:21", "view:22"]
    assert "view:20" not in tb.simulate(tree).keys()
    d = next(d for d in tree.diagnostics if d["kind"] == "recycler_bound_before_service")
    assert d["keys"] == ["view:5"] and d["count"] == 1


def test_the_episode_pager_both_ways_round():
    # AP-4 fixtures: the same screen with TalkBack first and later
    import hunt_replay as H

    first = tb.build(H.dump("antennapod_episode_details_tb_first"))
    later = tb.build(H.dump("antennapod_episode_details_tb_later"))
    assert "recycler_bound_before_service" not in [d["kind"] for d in first.diagnostics]
    assert "recycler_bound_before_service" in [d["kind"] for d in later.diagnostics]
    assert "view:872" in tb.simulate(first).keys()
    assert "view:872" not in tb.simulate(later).keys()
    # press for press: every move of w9wtb7e (the page's texts are stops of their own); the
    # two presses after Download that move nothing are TalkBack failing to focus a WebView
    # root that the dump shows exactly as in the TalkBack-first one (G7: tb.webview_block)
    assert H.agreement("antennapod_episode_details_tb_later") == (8, 10, 8, 8)
    assert H.agreement("antennapod_episode_details_tb_first") == (14, 14, 14, 14)
