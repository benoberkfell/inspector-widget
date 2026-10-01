"""talkback/static.py on hand-built TalkBack views: one tree per behaviour the corpus does not
pin on its own (the corpus table is tests/test_capture_tb_rules.py)."""

from __future__ import annotations

from inspector_widget import talkback as tb
from inspector_widget.talkback import rules as R
from inspector_widget.talkback import occlusion, static

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


def test_a_row_read_out_of_order_quotes_where_the_two_orders_part():
    # Two cut heads of 60 characters (the long title both begin with) read the same; the
    # evidence names the first part TalkBack reads out of place and what the screen shows there
    title = "A title long enough that two quotes cut at sixty characters read alike"
    row = n(4, cls="android.widget.LinearLayout", flags=FOCUS, actions=[CLICK],
            b=(0, 0, 1080, 300), children=[
                n(5, cls="android.widget.TextView", text=title, b=(0, 0, 1080, 100)),
                n(6, cls="android.widget.TextView", text="$5", b=(800, 150, 200, 100)),
                n(7, cls="android.widget.TextView", text="Socks", b=(0, 150, 400, 100))])
    fs = [f for f in findings(root(row)) if f.code == "tb.wrong_announcement"]
    assert len(fs) == 1
    assert fs[0].evidence["reads"] == "$5" and fs[0].evidence["before"] == "Socks"


def test_antennapod_episode_rows_read_in_screen_order():
    # The title is a full-height strip's contentDescription left of the date, size and
    # duration; TalkBack 17 says title, date, size, duration, as the screen shows them
    # (walk wnq20pl). The XY-cut once fell back to column order and flagged every row.
    fs = static.findings(tb.Navigator(_realapp("antennapod_episodes")), density=480)
    assert "tb.wrong_announcement" not in [f.code for f in fs]


def _past_edge_screen(cls):
    shown = [n(2 + i, cls="android.widget.TextView", text=t, b=(0, 500 * i, 1080, 400))
             for i, t in enumerate(("One", "Two"))]
    cut = [n(5 + i, cls="android.widget.TextView", text=t, flags=("enabled",),
             b=(0, 1000 + 200 * i, 1080, 200)) for i, t in enumerate(("\n", "Rows below"))]
    return root(n(9, cls=cls, b=(0, 0, 1080, 1000), children=shown + cut))


def test_content_past_an_edge_nothing_scrolls_quotes_its_first_words():
    fs = [f for f in findings(_past_edge_screen("android.widget.LinearLayout"))
          if f.code == "tb.edge_stuck"]
    assert [f.node.key for f in fs] == ["view:9"]
    assert fs[0].evidence["first"] == "Rows below"  # not the bare newline before it


def test_a_web_page_past_its_edge_is_not_stuck():
    # Chromium scrolls the page itself as TalkBack moves through it: AntennaPod's expanded
    # player has 45 show-notes nodes past its edge, and TalkBack read all of them (wp8mw23)
    assert [f for f in findings(_past_edge_screen("android.webkit.WebView"))
            if f.code == "tb.edge_stuck"] == []
    for name in ("antennapod_player_expanded", "antennapod_player_expanded_tb_on"):
        fs = static.findings(tb.Navigator(_realapp(name)), density=480)
        assert "tb.edge_stuck" not in [f.code for f in fs], name


def test_text_said_through_its_own_description_is_not_skipped():
    # AntennaPod's position: the text "00:04:21" carries the description "Position: 4
    # minutes", which TalkBack says in its place inside the row (walk wnq20pl, step 8)
    row = n(4, cls="android.widget.LinearLayout", flags=FOCUS, actions=[CLICK],
            b=(0, 0, 1080, 300), children=[
                n(5, cls="android.widget.TextView", text="Episode title", b=(0, 0, 1080, 100)),
                n(6, cls="android.widget.TextView", text="00:04:21", cd="Position: 4 minutes",
                  b=(0, 150, 400, 100))])
    assert "tb.skipped" not in [f.code for f in findings(root(row))]


def _player_over_feed(sheet_kw=None):
    """AntennaPod's expanded player: a persistent bottom sheet (no scrim, not modal) over the
    feed, whose toolbar and rows TalkBack goes on to read after the player's last button."""
    feed = [n(2, cls="android.widget.ImageButton", cd="Back", flags=FOCUS, actions=[CLICK],
              b=(0, 168, 168, 168)),
            n(3, cls="android.widget.TextView", text="Episode 1", flags=FOCUS, actions=[CLICK],
              b=(0, 400, 1080, 200)),
            n(4, cls="android.widget.TextView", text="Episode 2", flags=FOCUS, actions=[CLICK],
              b=(0, 600, 1080, 200))]
    sheet = n(9, cls="android.widget.FrameLayout", b=(0, 0, 1080, 2200), children=[
        n(10, cls="android.widget.ImageButton", cd="Pause", flags=FOCUS, actions=[CLICK],
          b=(400, 1800, 200, 200)),
        n(11, cls="android.widget.ImageButton", cd="Skip episode", flags=FOCUS,
          actions=[CLICK], b=(700, 1800, 200, 200))], **(sheet_kw or {}))
    return root(*feed, sheet), sheet


def test_focus_walking_out_of_an_expanded_sheet_with_no_scrim_is_an_escape():
    r, sheet = _player_over_feed()

    def above(a, b):
        return True if a is sheet else (False if b is sheet else None)

    fs = findings(r, drawn_above=above, codes=["tb.escape"])
    assert [(f.code, f.node.key) for f in fs] == [("tb.escape", "view:9")]
    assert [o.key for o in fs[0].others] == ["view:2", "view:3", "view:4"]
    nav = tb.Navigator(tb.build([r]))
    assert set(static.covered(nav, above)) == {"view:2", "view:3", "view:4"}
    # without the View tree nothing is known about what is drawn above what
    assert findings(r, codes=["tb.escape"]) == []
    # a Compose host over Views is often a transparent overlay (a snackbar host): left alone
    r2, sheet2 = _player_over_feed({"provider_class": "AndroidComposeView"})
    assert findings(r2, drawn_above=lambda a, b: True if a is sheet2 else None,
                    codes=["tb.escape"]) == []


# ------------------------------------------------------------------- G5: one occlusion model
def _action_mode(bar_kw=None):
    """Thunderbird in selection mode (TB-4): AppCompat's ActionBarContextView (a11y class
    ViewGroup) drawn over the MaterialToolbar (windowActionModeOverlay), the list below."""
    toolbar = n(22, cls="android.view.ViewGroup", b=(0, 156, 1280, 192), children=[
        n(32, cls="android.widget.ImageButton", cd="Navigate up", flags=FOCUS, actions=[CLICK],
          b=(0, 168, 168, 168)),
        n(24, cls="android.widget.TextView", text="Inbox", b=(168, 208, 162, 88)),
        n(86, cls="android.widget.ImageView", cd="Search", flags=FOCUS, actions=[CLICK],
          b=(872, 180, 144, 144))])
    toolbar.pop("important_for_accessibility")
    rows = [n(40 + i, cls="android.widget.TextView", text=f"Message {i}", flags=FOCUS,
              actions=[CLICK], b=(0, 400 + 200 * i, 1280, 200)) for i in range(5)]
    content = n(20, cls="android.widget.RelativeLayout", b=(0, 0, 1280, 2856),
                children=[toolbar, *rows], drawing_order=1)
    bar = n(236, cls="android.view.ViewGroup", b=(0, 156, 1280, 192), drawing_order=2,
            children=[n(237, cls="android.widget.ImageView", cd="Done", flags=FOCUS,
                        actions=[CLICK], b=(0, 180, 168, 144)),
                      n(255, cls="android.widget.TextView", text="1 selected",
                        b=(216, 208, 302, 88)),
                      n(244, cls="android.widget.Button", cd="Delete", flags=FOCUS,
                        actions=[CLICK], b=(872, 180, 144, 144))], **(bar_kw or {}))
    return root(content, bar, b=(0, 0, 1280, 2856)), bar


def test_the_toolbar_under_an_action_mode_bar_is_covered():
    # TB-4: TalkBack reads the toolbar the action-mode bar hides first and the bar last
    r, bar = _action_mode()

    def above(a, b):
        return True if a is bar else (False if b is bar else None)

    fs = findings(r, drawn_above=above, codes=["tb.covered_stop", "tb.out_of_order"])
    cov = [f for f in fs if f.code == "tb.covered_stop"]
    assert [f.node.key for f in cov] == ["view:32", "view:24", "view:86"]
    assert all(f.others[0].key == "view:236" and f.evidence["kind"] == "bar" for f in cov)
    # the bar's stops are read last: one out-of-order finding for the whole bar, which says why
    order = [f for f in fs if f.code == "tb.out_of_order"]
    assert [(f.node.key, f.evidence.get("why"), f.evidence.get("stops")) for f in order] == [
        ("view:237", "in_overlay", 3)]
    assert order[0].others[-1].key == "view:236"
    # the same from the dump's own drawing order, as a walk sees it (no View tree)
    occ, _roots = occlusion.for_dump({"windows": [{"root_view_id": 1, "root": r}]})
    assert occ.covered_by(r["children"][0]["children"][0]["children"][0]).key == "view:236"


def test_an_empty_frame_drawn_last_covers_nothing():
    # AP-4: AntennaPod's loading FrameLayout (no background, its only child GONE) over the
    # episode page was taken for an overlay (tb.escape, ghost "occluded by view:898")
    page = [n(876, cls="android.widget.ImageView", cd="Open podcast", flags=FOCUS,
              actions=[CLICK], b=(48, 410, 168, 168)),
            n(885, cls="android.widget.TextView", text="Stream", flags=FOCUS, actions=[CLICK],
              b=(48, 641, 592, 144))]
    frame = n(898, cls="android.widget.FrameLayout", b=(0, 348, 1280, 2052), drawing_order=3)
    frame.pop("important_for_accessibility")
    r = root(*page, frame, b=(0, 0, 1280, 2856))
    occ, _roots = occlusion.for_dump({"windows": [{"root_view_id": 1, "root": r}]})
    assert [occ.covered_by(p) for p in page] == [None, None]
    # with a background (the capture's View properties) it does cover them
    occ, _roots = occlusion.for_dump(
        {"windows": [{"root_view_id": 1, "root": r}]},
        props=lambda vid: {"background": "#FFFFFFFF"} if vid == 898 else {})
    assert occ.covered_by(page[0]).kind == "sheet"
    assert occlusion.paints("#00000000") is False and occlusion.paints("RippleDrawable") is False


def test_a_touch_area_over_its_icon_covers_nothing():
    # Thunderbird's star_click_area: a transparent clickable View over the star image
    star = n(463, cls="android.widget.ImageView", cd="Star", b=(1160, 1737, 72, 72))
    area = n(470, cd="Add star", flags=FOCUS, actions=[CLICK], b=(1136, 1618, 144, 279))
    r = root(n(450, cls="android.widget.FrameLayout", b=(0, 1618, 1280, 279),
               children=[star, area]), b=(0, 0, 1280, 2856))
    occ, _roots = occlusion.for_dump({"windows": [{"root_view_id": 1, "root": r}]})
    assert occ.covered_by(star) is None


def test_an_open_drawer_covers_all_of_the_content():
    # DrawerLayout's scrim covers the content where the drawer does not reach too (the
    # strip right of a 1080px drawer); the content is hidden from accessibility as well, so
    # its texts are no text TalkBack skipped (Thunderbird's drawer, wmuvqax)
    content = n(250, cls="android.widget.RelativeLayout", b=(0, 0, 1280, 2856),
                important_for_accessibility="NO_HIDE_DESCENDANTS",
                children=[n(254, cls="android.widget.TextView", text="Inbox",
                            b=(168, 208, 162, 88)),
                          n(312, cls="android.widget.ImageView", cd="More options",
                            flags=FOCUS, actions=[CLICK], b=(1160, 180, 120, 144))])
    drawer = n(261, cls="androidx.compose.ui.platform.ComposeView", b=(0, 0, 1080, 2856),
               children=[n(262, cls="android.widget.TextView", text="Outbox", flags=FOCUS,
                           actions=[CLICK], b=(36, 591, 1008, 168))])
    dl = n(249, cls="androidx.drawerlayout.widget.DrawerLayout", b=(0, 0, 1280, 2856),
           children=[content, drawer])
    nav = tb.Navigator(tb.build([root(dl, b=(0, 0, 1280, 2856))]))
    cov = static.covers(nav)
    kinds = {c.overlay["node_key"]: c.kind for c in cov.values()}
    assert kinds == {"view:261": "drawer"} and len(cov) == 2  # "Inbox" and "More options"
    # nothing to report: the app hides the content (that is the fix)
    assert findings(root(dl, b=(0, 0, 1280, 2856)), drawn_above=lambda a, b: None,
                    codes=["tb.covered_stop", "tb.escape", "tb.skipped"]) == []
