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
    # an excluded dump node (tb.skipped's hiding node) has no TalkBack key: its dump key
    return [(f.code, getattr(f.node, "key", None) or f.node.raw.get("node_key")) for f in fs]


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


def test_a_pager_with_page_buttons_or_custom_actions_is_not_stuck():
    def pager(**kw):
        return c(5, 2, b=(0, 300, 1080, 600), flags=("visible_to_user", "enabled", "scrollable"),
                 actions=[{"id": R.ACTION_SCROLL_FORWARD}, {"id": R.ACTION_PAGE_RIGHT},
                          *kw.pop("actions", ())],
                 children=[c(5, 3, cls="android.widget.TextView", text="Page 1", flags=SRF,
                             b=(0, 300, 1080, 100))], **kw)

    def button(sid, text):
        return c(5, sid, cls="android.widget.Button", text=text, flags=FOCUS, actions=[CLICK],
                 b=(0, 1000, 1080, 150))

    # a "Done" button turns no page: still stuck (A11yProbe C8 BAD)
    assert codes(findings(root(compose_host(5, pager(), button(9, "Done"))))) == [
        ("tb.edge_stuck", "compose:5:2")]
    # the fixes the rule names: a page button, or labelled custom actions on the pager
    assert findings(root(compose_host(5, pager(), button(9, "Next page")))) == []
    assert findings(root(compose_host(5, pager(), button(9, "›")))) == []
    paged = pager(actions=[{"id": 0x7f0a0001, "label": "Next page"},
                           {"id": 0x7f0a0002, "label": "Previous page"}])
    assert findings(root(compose_host(5, paged, button(9, "Done")))) == []


def _drawer(panel: bool):
    """DrawerLayout with its drawer open: updateChildrenImportantForAccessibility marks the
    content child noHideDescendants; the scrim is painted, not a node."""
    content = n(3, cls="android.widget.RelativeLayout", b=(0, 0, 1080, 2400),
                important_for_accessibility="NO_HIDE_DESCENDANTS", children=[
                    n(4, cls="android.widget.TextView", text="Inbox", b=(0, 100, 600, 100)),
                    n(5, cls="android.widget.Button", text="Compose", flags=FOCUS,
                      actions=[CLICK], b=(800, 2200, 200, 150))])
    drawer = n(6, cls="android.widget.LinearLayout", b=(0, 0, 800, 2400), children=[
        n(7, cls="android.widget.TextView", text="Folders", b=(0, 100, 800, 100)),
        n(8, cls="android.widget.Button", text="Archive", flags=FOCUS, actions=[CLICK],
          b=(0, 300, 800, 150))])
    kids = [content, drawer] if panel else [content]
    return root(n(2, cls="androidx.drawerlayout.widget.DrawerLayout", b=(0, 0, 1080, 2400),
                  children=kids))


def test_content_hidden_for_an_open_drawer_is_not_skipped():
    # Thunderbird's message list under its open navigation drawer: the hiding keeps focus
    # in the drawer (what tb.escape asks for), not text lost
    assert [f.code for f in findings(_drawer(panel=True))] == []
    # the same content hidden with no panel over it: TalkBack never reads it
    fs = findings(_drawer(panel=False))
    assert codes(fs) == [("tb.skipped", "view:3")]
    assert fs[0].evidence == {"texts": 2, "first": "Inbox", "why": "noHideDescendants"}


def test_siblings_hidden_for_a_modal_sheet_are_not_skipped():
    # BottomSheetBehavior(updateImportantForAccessibilityOnSiblings): every sibling of the
    # expanded sheet is hidden, also the app bar the sheet does not overlap
    hide = {"important_for_accessibility": "NO_HIDE_DESCENDANTS"}
    bar = n(3, cls="com.google.android.material.appbar.AppBarLayout", b=(0, 0, 1080, 200),
            children=[n(4, cls="android.widget.TextView", text="Library", b=(0, 50, 600, 100))],
            **hide)
    body = n(5, cls="android.widget.FrameLayout", b=(0, 200, 1080, 2200), children=[
        n(6, cls="android.widget.TextView", text="Episode 1", b=(0, 300, 1080, 100))], **hide)
    sheet = n(7, cls="android.widget.FrameLayout", b=(0, 1200, 1080, 1200), children=[
        n(8, cls="android.widget.Button", text="Play", flags=FOCUS, actions=[CLICK],
          b=(0, 1300, 1080, 150))])
    coord = n(2, cls="androidx.coordinatorlayout.widget.CoordinatorLayout",
              b=(0, 0, 1080, 2400), children=[bar, body, sheet])
    assert [f.code for f in findings(root(coord))] == []


def test_hidden_text_a_stop_already_says_is_not_skipped():
    # a decorative copy hidden from TalkBack while the button's description says it
    label = n(3, cls="android.widget.TextView", text="Delete", b=(0, 100, 600, 100),
              important_for_accessibility="NO_HIDE_DESCENDANTS")
    btn = n(4, cls="android.widget.ImageButton", cd="Delete message", flags=FOCUS,
            actions=[CLICK], b=(600, 100, 200, 200))
    assert findings(root(label, btn)) == []
    label["text"] = "Archive"
    assert codes(findings(root(label, btn))) == [("tb.skipped", "view:3")]


def test_text_its_rows_description_replaces_is_skipped():
    # A11yProbe V4: the row's contentDescription "Settings row" replaces "Wi-Fi"
    row = n(4, cls="android.widget.LinearLayout", cd="Settings row", flags=FOCUS,
            actions=[CLICK], b=(0, 256, 1080, 156), children=[
                n(5, cls="android.widget.TextView", text="Wi-Fi", b=(0, 280, 900, 107))])
    fs = findings(root(row))
    assert codes(fs) == [("tb.skipped", "view:5")]
    assert fs[0].evidence == {"texts": 1, "first": "Wi-Fi", "why": "not_spoken"}
    assert [o.key for o in fs[0].others] == ["view:4"]
    # a row that reads it (no description of its own) is fine
    row.pop("content_description")
    assert findings(root(row)) == []


def test_a_stop_on_a_page_nobody_sees_is_a_ghost():
    # A11yProbe V13 (TalkBack on): the off-screen pager page's WebView content is read
    web = n(7, cls="android.widget.TextView", text="Show notes", flags=FOCUS,
            actions=[CLICK], b=(1100, 300, 900, 100))
    fs = findings(root(web, n(8, cls="android.widget.Button", text="Play", flags=FOCUS,
                              actions=[CLICK], b=(0, 300, 1080, 100)), b=(0, 0, 1080, 2400)))
    assert codes(fs) == [("tb.ghost_stop", "view:7")] and fs[0].evidence["why"] == "offscreen"


def _realapp(name):
    import gzip
    import json
    import os

    path = os.path.join(os.path.dirname(__file__), "data", "realapps", f"{name}.a11y.json.gz")
    with gzip.open(path) as f:
        return tb.build(json.load(f))


def test_real_apps_open_drawer_and_abbreviated_dates_are_not_skipped():
    # Thunderbird with its navigation drawer open (live, emulator-5554): the content is
    # noHideDescendants while the drawer's ComposeView holds the stops
    fs = static.findings(tb.Navigator(_realapp("thunderbird_drawer")))
    assert "tb.skipped" not in [f.code for f in fs]
    # AntennaPod rows say "August 5, 2026" in their description over the visible "Aug 5"
    for name in ("antennapod_home", "antennapod_episodes"):
        fs = static.findings(tb.Navigator(_realapp(name)))
        assert "tb.skipped" not in [f.code for f in fs], name


def test_the_real_off_screen_show_notes_are_ghost_stops():
    # AntennaPod's player (live): ViewPager2 keeps the show notes' WebView page in the tree
    fs = static.findings(tb.Navigator(_realapp("antennapod_player")))
    off = [f.node.key for f in fs if f.code == "tb.ghost_stop" and f.evidence["why"] == "offscreen"]
    assert off and all(k.startswith("virtual:") for k in off)
