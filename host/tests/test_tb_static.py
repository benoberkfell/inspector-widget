"""talkback/static.py on hand-built TalkBack views: one tree per behaviour the corpus does not
pin on its own (the corpus table is tests/test_capture_tb_rules.py)."""

from __future__ import annotations

from inspector_widget import talkback as tb
from inspector_widget.talkback import rules as R
from inspector_widget.talkback import static

from test_tb_rules import CLICK, FOCUS, SRF, c, compose_host, n, root


def findings(*roots, **kw):
    return static.findings(tb.Navigator(tb.build(list(roots))), **kw)


def codes(fs):
    return [(f.code, f.node.key) for f in fs]


def test_a_view_overlay_read_after_the_compose_content_under_it_is_a_boundary_jump():
    # A View banner floating over a full-height ComposeView, added after it: TalkBack reads
    # the whole Compose list first, then the banner it sees halfway down.
    rows = [c(5, 10 + i, cls="android.widget.TextView", text=f"Product {i}", flags=SRF,
              b=(0, 400 + 150 * i, 1080, 150)) for i in range(4)]
    banner = n(9, cls="android.widget.TextView", text="Offline: changes will sync later",
               b=(0, 600, 1080, 200))
    fs = findings(root(compose_host(5, *rows, b=(0, 0, 1080, 2000)), banner))
    assert codes(fs) == [("tb.boundary_jump", "view:9")]
    assert fs[0].evidence == {"read": 5, "visual": 3, "of": 5} and fs[0].conf == "heuristic"
    assert [o.key for o in fs[0].others] == ["compose:5:13"]


def test_a_banner_over_the_top_of_a_composeview_is_read_first():
    # H4 as TalkBack 17 reads it (and the model, through the bounds swap): no finding.
    rows = [c(5, 10 + i, cls="android.widget.TextView", text=f"Product {i}", flags=SRF,
              b=(0, 400 + 150 * i, 1080, 150)) for i in range(4)]
    banner = n(9, cls="android.widget.TextView", text="Offline", b=(0, 0, 1080, 200))
    assert findings(root(compose_host(5, *rows, b=(0, 0, 1080, 2000)), banner)) == []


def test_an_outer_stop_that_repeats_the_inner_ones_words_is_a_double_stop():
    # semantics { contentDescription = "Profile" } on a Box, and an Icon with the same cd
    box = c(5, 2, cd="Profile picture", flags=SRF, b=(0, 0, 400, 400), children=[
        c(5, 3, cls="android.widget.ImageView", cd="Profile picture", flags=SRF,
          b=(100, 100, 200, 200))])
    fs = findings(root(compose_host(5, box)))
    assert codes(fs) == [("tb.double_stop", "compose:5:2")]
    assert fs[0].evidence == {"inner": 1, "why": "says the inner stop's words"}


def test_a_stop_without_area_is_a_ghost():
    fs = findings(root(n(2, cls="android.widget.Button", text="Go", flags=FOCUS,
                         actions=[CLICK], b=(0, 0, 1080, 2))))
    assert codes(fs) == [("tb.ghost_stop", "view:2")]
    assert fs[0].evidence["why"] == "tiny"
    assert static.ghost(tb.Navigator(tb.build([root(n(3, text="ok", b=(0, 0, 9, 9)))])),
                        tb.build([root(n(3, text="ok", b=(0, 0, 9, 9)))]).node("view:3")) \
        == ["tiny"]


def test_an_authors_traversal_link_is_their_order_unless_its_target_is_ambiguous():
    # V3 GOOD: "Summary" asks to be read before "Step 1": out of visual order on purpose
    step1 = n(4, cls="android.widget.TextView", text="Step 1", b=(0, 300, 1080, 100))
    step2 = n(5, cls="android.widget.TextView", text="Step 2", b=(0, 400, 1080, 100))
    summary = n(6, cls="android.widget.TextView", text="Summary", b=(0, 500, 1080, 100),
                traversal_before=step1["id"])
    title = n(2, cls="android.widget.TextView", text="Title", b=(0, 100, 1080, 100))
    assert findings(root(title, step1, step2, summary)) == []
    # V1 BAD: the target's id is shared by another View (<include>): no longer the author's
    step1["view_id_resource_name"] = step2["view_id_resource_name"] = "app:id/field"
    assert codes(findings(root(title, step1, step2, summary))) == [
        ("tb.out_of_order", "view:6")]


def test_escape_waits_for_what_is_drawn_above_what():
    background = [n(10 + i, cls="android.widget.Button", text=f"Background {i}", flags=FOCUS,
                    actions=[CLICK], b=(0, 200 + 120 * i, 1080, 120)) for i in range(3)]
    scrim = n(20, flags=FOCUS, actions=[CLICK], b=(0, 0, 1080, 2400))
    card = n(30, cls="android.widget.Button", text="Option A", flags=FOCUS, actions=[CLICK],
             b=(300, 1000, 480, 120))
    tree_roots = [root(*background, scrim, card)]
    assert "tb.escape" not in [f.code for f in findings(*tree_roots)]  # unknown: silent
    later = {20: 1, 30: 2}  # what the capture's View tree says: scrim over the buttons

    def drawn_above(a, b):
        return later.get(a["host_view_id"], 0) > later.get(b["host_view_id"], 0)

    esc = [f for f in findings(*tree_roots, drawn_above=drawn_above) if f.code == "tb.escape"]
    assert len(esc) == 1 and esc[0].node.key == "view:20"
    assert sorted(o.key for o in esc[0].others) == ["view:10", "view:11", "view:12"]
    assert esc[0].evidence == {"under": 3, "area_pct": 100}


def test_a_scrim_with_nothing_over_it_is_not_a_dialog():
    background = [n(10 + i, cls="android.widget.Button", text=f"B{i}", flags=FOCUS,
                    actions=[CLICK], b=(0, 200 + 120 * i, 1080, 120)) for i in range(3)]
    scrim = n(20, flags=FOCUS, actions=[CLICK], b=(0, 0, 1080, 2400))
    fs = findings(root(*background, scrim), drawn_above=lambda a, b: a["host_view_id"] == 20)
    assert "tb.escape" not in [f.code for f in fs]


def test_a_pager_with_page_tabs_is_not_stuck():
    pages = c(5, 2, b=(0, 300, 1080, 600), flags=("visible_to_user", "enabled", "scrollable"),
              actions=[{"id": R.ACTION_SCROLL_FORWARD}, {"id": R.ACTION_PAGE_RIGHT}],
              children=[c(5, 3, cls="android.widget.TextView", text="Page 1", flags=SRF,
                          b=(0, 300, 1080, 100))])
    stuck = findings(root(compose_host(5, pages)))
    assert codes(stuck) == [("tb.edge_stuck", "compose:5:2")]
    tabs = [c(5, 10 + i, text=f"Tab {i}", flags=FOCUS + (("selected",) if i == 0 else ()),
              actions=[CLICK], b=(360 * i, 100, 360, 150)) for i in range(3)]
    assert findings(root(compose_host(5, *tabs, pages))) == []
