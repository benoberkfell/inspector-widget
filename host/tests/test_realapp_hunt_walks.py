"""The real-app hunt's TalkBack walks, replayed offline (docs/realapp-findings.md).

Each test pins, on the screen the hunt found a bug on, what the TalkBack model and the walk
checks now say: the model's press-for-press agreement with TalkBack 17.0, and the finding
that names the bug (or the false finding that is gone). Replays come from
``tests/hunt_replay.py``: a hunt entry (the dump the walk started from + TalkBack's presses)
rebuilt into the walk record ``tb_walk`` saves, or a walk record the hunt saved itself.
"""

from __future__ import annotations

import pytest

import hunt_replay as H
import tb_capture_fixtures as T

from inspector_widget import talkback as tb
from inspector_widget.capture import analyzers
from inspector_widget.output import dumps
from inspector_widget.proto import view_inspection_pb2 as pb
from inspector_widget.capture.rules import DEFAULT_TB
from inspector_widget.talkback import static


def _codes(rec, code=None):
    return [f for f in rec["findings"] if code is None or f["code"] == code]


def _capture(name, dpi=480):
    """A capture of one realapps dump (a View spine derived from it, no View properties)."""
    resp = H.to_proto(H.dump(name))
    raw = T.raw_from_a11y(resp, cid=("c" + name.replace("_", ""))[:12], dpi=dpi)
    return T.build(raw), raw


# --------------------------------------------------------------------------- model agreement
#: (presses where the model lands where TalkBack did, presses compared, presses whose words
#: agree, presses with logged speech): from the walk's start to its first auto-scroll (the
#: model does not scroll).
AGREEMENT = {
    "thunderbird_list_compose_tb_first": (22, 22, 21, 21),
    "thunderbird_list_compose_tb_later": (22, 22, 21, 21),
    "thunderbird_list_compose_no_banner": (19, 19, 18, 18),
    "thunderbird_selection_mode": (28, 28, 27, 27),
    "thunderbird_settings": (14, 14, 13, 13),
    "nia_onboarding_grid": (3, 3, 3, 3),
    "nia_onboarding_grid_backward": (15, 15, 14, 14),
    "nia_feed_two_column": (16, 16, 16, 16),
    "nia_interests": (9, 9, 9, 9),
    # G26: "•" is spoken "Bullet" (was 11 of 14 words)
    "antennapod_episode_details_tb_first": (14, 14, 14, 14),
    # G20: the page root is no stop when its item was bound before TalkBack started, so the
    # page's texts are stops of their own as TalkBack read them (was 2 of 10). The last two
    # presses move nothing: TalkBack cannot focus the WebView root after Download, which the
    # dump shows exactly as in the TalkBack-first one (tb.webview_block names it).
    "antennapod_episode_details_tb_later": (8, 10, 8, 8),
}


@pytest.mark.parametrize("name", sorted(AGREEMENT))
def test_the_model_walks_each_hunt_screen_as_talkback_did(name):
    assert H.agreement(name) == AGREEMENT[name]


# ------------------------------------------------------------------ TB-1: list counts
def test_tb1_static_the_empty_banner_shifts_the_positions_with_talkback_first():
    first = static.findings(tb.Navigator(tb.build(H.dump("thunderbird_list_compose_tb_first"))),
                            density=480, codes=DEFAULT_TB)
    assert [(f.code, f.node.key, f.evidence.get("said_pos")) for f in first] == [
        ("tb.wrong_announcement", "compose:67:51", "2 of 6")]
    assert [o.key for o in first[0].others] == ["view:57"]  # the 0x0 banner ComposeView
    later_tree = tb.build(H.dump("thunderbird_list_compose_tb_later"))
    assert static.findings(tb.Navigator(later_tree), density=480, codes=DEFAULT_TB) == []
    assert "recycler_bound_before_service" in [d["kind"] for d in later_tree.diagnostics]


def test_tb1_walks_say_where_the_count_is_off():
    # wcw61ax (TalkBack first): "2 of 6" on the first message
    first = _codes(H.replay("thunderbird_list_compose_tb_first"), "tb.wrong_announcement")
    assert len(first) == 1 and '"2 of 6"' in first[0]["msg"]
    # wygfouz (TalkBack later): no positions, but "In list. 6 items" for the 5 rows a lap read
    later = _codes(H.replay("thunderbird_list_compose_tb_later"), "tb.wrong_announcement")
    assert len(later) == 1 and '"In list. 6 items"' in later[0]["msg"]
    assert "reached 5 item(s)" in later[0]["msg"] and "view:39" in later[0]["msg"]
    # wr43dka (the banner flag off): "1 of 4" ... "4 of 4", "4 items": nothing
    assert _codes(H.replay("thunderbird_list_compose_no_banner"), "tb.wrong_announcement") == []


def test_a_walk_that_stops_inside_a_list_does_not_judge_its_count():
    # wytgfpg (NIA-13) read 4 of the 19 topics: "20 items" cannot be checked from there
    assert _codes(H.record("wytgfpg"), "tb.wrong_announcement") == []


# ---------------------------------------------------------- TB-4: the covered toolbar
def test_tb4_the_walk_tags_the_toolbar_under_the_action_mode_bar():
    rec = H.replay("thunderbird_selection_mode")  # wdvjrk4
    cov = _codes(rec, "tb.covered_stop")
    assert len(cov) == 1 and cov[0]["steps"] == [0, 1, 2, 3, 4]
    assert cov[0]["overlay"] == "view:236" and cov[0]["sev"] == "warn"
    assert [s["covered_by"]["kind"] for s in rec["steps"][:5]] == ["bar"] * 5


def test_tb4_the_capture_flags_the_covered_toolbar_and_explains_the_bars_order():
    ix, raw = _capture("thunderbird_selection_mode")
    issues = {(n.ref or n.id, i.id): i for n in ix.nodes.values() for i in n.issues}
    cov = issues[("view:236", "tb.covered_stop")]  # the action-mode bar
    assert cov.evidence["covers"] == 5 and cov.evidence["kind"] == "bar"
    assert sorted(k[0] for k in issues if k[1] == "render.covered") == [
        "view:24", "view:297", "view:32", "view:82", "view:86"]
    order = [(k[0], i.evidence) for k, i in issues.items() if k[1] == "tb.out_of_order"]
    assert [(r, e.get("why"), e.get("stops")) for r, e in order] == [("view:237", "in_overlay", 6)]
    out = analyzers.lint_view(ix, raw, rules=["tb"])
    lines = {r["rule"]: r["nodes"] for r in out["rules"]}
    assert lines["tb.covered_stop"] == [
        "view:236 #action_mode_bar draws over 5 stop(s) TalkBack reads (bar): view:32 view:24 +3"]
    assert lines["tb.out_of_order"] == [
        'view:237 "Done" read 12 after view:35: 6 stop(s) of view:236, drawn over what is read '
        'before it']
    assert len(dumps(out).encode()) <= 1800  # Compose rows (ghost and double stops) too


# --------------------------------------------------------- AP-4: the false overlay is gone
def test_ap4_an_empty_loading_frame_is_no_overlay():
    rec = H.replay("antennapod_episode_details_tb_first")  # wzi9apx
    assert _codes(rec, "tb.escape") == [] and _codes(rec, "tb.covered_stop") == []
    assert not any("view:898" in f["msg"] for f in rec["findings"])
    assert not any(s.get("covered_by") for s in rec["steps"])


def test_ap4_stuck_before_the_pages_webview_is_named():
    # w9wtb7e: TalkBack started after the app, stuck after Download
    for rec in (H.replay("antennapod_episode_details_tb_later"), H.record("w9wtb7e")):
        block = _codes(rec, "tb.webview_block")
        assert len(block) == 1 and block[0]["webview"] == "virtual:897:278"
        assert "view:863" in block[0]["msg"]  # the pager's RecyclerView it sits in
        assert not any("Expose scrolling" in (f.get("fix") or "") for f in rec["findings"])
        assert block[0]["fix"].startswith("TalkBack cannot put focus")


# ------------------------------------------------------- AP-2: the sheet over Home
def test_ap2_the_capture_marks_what_the_expanded_player_covers():
    ix, _raw = _capture("antennapod_player_expanded")
    covered = [n for n in ix.nodes.values() if any(i.id == "render.covered" for i in n.issues)]
    assert len(covered) >= 10
    assert all(i.evidence["kind"] == "sheet" for n in covered for i in n.issues
               if i.id == "render.covered")
    # their lint findings are counted apart, as under a dialog
    assert "under an open dialog" in analyzers.lint_summary(ix)["lint"]


# ------------------------------------------------------------- the drawer's texts
def test_texts_behind_an_open_drawer_are_not_skipped():
    rec = H.replay("thunderbird_drawer")  # a full lap of the drawer
    assert rec["ended"] == "wrap" and rec["orphans"] == []
    assert _codes(rec, "tb.skipped") == []
    ix, raw = _capture("thunderbird_drawer")
    marked = [n for n in ix.nodes.values() if any(i.id == "render.covered" for i in n.issues)]
    assert {i.evidence["kind"] for n in marked for i in n.issues if i.id == "render.covered"} \
        == {"drawer"}
    inbox = next(n for n in marked if n.label == "Inbox")
    from inspector_widget.capture import tb as ctb
    facet = ctb.TbCapture.of(ix, raw).facet(inbox.id)
    assert facet["covered_by"].endswith("(drawer)")


# ------------------------------------------------------------------ G22: false positives
def test_g22_alike_nodes_of_two_cards_are_no_revisit():
    assert _codes(H.record("wems6uy"), "tb.revisit") == []
    # NIA-11: the same card read again after its Bookmark (same key, other text)
    rev = _codes(H.record("wtt0adx"), "tb.revisit")
    assert [f["steps"] for f in rev] == [[58, 60]]


def test_g22_a_text_field_and_its_clear_button_are_no_double_stop():
    dbl = _codes(H.record("wahyghw"), "tb.double_stop")
    assert dbl and all("Clear search text" not in f["msg"].split("first:")[-1][:80]
                       or f["steps"][0] != 1 for f in dbl)
    assert 1 not in [i for f in dbl for i in f["steps"]]


def test_g22_a_row_with_another_action_inside_is_info():
    for w in ("ws888m4", "wmaadkk"):
        assert [f["sev"] for f in _codes(H.record(w), "tb.double_stop")] == ["info"], w


def test_g22_a_partial_walk_claims_no_full_lap():
    for w in ("ws888m4", "w2z0qwg"):
        assert not any("full lap" in f["msg"] for f in H.record(w)["findings"]), w
    # stuck: the presses that moved nothing are no edge, so nothing past it is "skipped"
    sk = _codes(H.record("w9wtb7e"), "tb.skipped")
    assert len(sk) == 1 and "between the stops it did reach" in sk[0]["msg"]


# ---------------------------------------------------------------------------- G7: web
def test_g7_no_app_named_in_the_models_web_hints():
    import inspect

    from inspector_widget.talkback import order
    assert "AntennaPod" not in inspect.getsource(order.web_hidden_page)


def test_g7_web_stops_nobody_sees_are_in_the_capture_summary():
    ix, _raw = _capture("antennapod_home_player_collapsed")
    issues = analyzers.lint_summary(ix)["issues"]
    assert "offscreen" in issues  # AP-1: the collapsed player's show notes, 0 px, below
    off = [n for n in ix.nodes.values() for i in n.issues if i.id == "render.offscreen"]
    assert len(off) >= 20 and all(n.id.startswith("a11y:695:") for n in off)
    # the current page's notes on the episode screen are not: the WebView scrolls them in
    ix, _raw = _capture("antennapod_episode_details_tb_first")
    off = {n.id.split(":")[1] for n in ix.nodes.values() for i in n.issues
           if i.id == "render.offscreen"}
    assert "897" not in off and off <= {"925", "805"}  # the next page's and the player's


# ------------------------------------------------------------------------------- L4: remodel
def test_a_remodel_marks_the_stops_it_adds():
    from inspector_widget.talkback import walk

    model = walk.Model()
    model.build(H.to_proto(H.dump("nia_onboarding_grid")), False)
    assert all(p.added is None for p in model.stops)
    model.remodel(H.to_proto(H.dump("nia_interests")), False)
    added = [p for p in model.stops if p.added]
    assert added and {p.added for p in added} == {1}


# --------------------------------------------------------------------- G16: NIA-3, NIA-1
def test_nia3_the_side_by_side_cards_are_read_interleaved():
    rec = H.record("wg0mhts")  # wm density 280: two feed columns
    f = _codes(rec, "tb.interleaved")
    assert len(f) == 1 and f[0]["sev"] == "warn"
    assert {19, 20, 21, 22}.issubset(f[0]["steps"]) and {26, 27, 28, 29}.issubset(f[0]["steps"])
    assert "compose:8:219 'Bookmark' of compose:8:206" in f[0]["msg"]
    assert f[0]["fix"].startswith("Make each card one traversal group")
    # one column: each card is read whole
    assert _codes(H.record("wtt0adx"), "tb.interleaved") == []


def test_nia1_auto_scroll_along_the_bottom_row_names_the_topics_it_passes_over():
    rec = H.record("wvq4h1u")
    f = _codes(rec, "tb.autoscroll_row_skip")
    assert len(f) == 1
    assert f[0]["missed"] == ["Performance", "Kotlin", "New APIs & Libraries",
                              "Platform & Releases", "Privacy & Security", "Accessibility"]
    assert f[0]["steps"][:3] == [8, 9, 11] and "compose:8:85" in f[0]["msg"]
    # tb.skipped keeps only what the row skip does not explain, with its own advice
    sk = _codes(rec, "tb.skipped")
    assert len(sk) == 1 and "Performance" not in sk[0]["msg"]
    # backward from Done: no auto-scroll at the start of the grid, nothing to say
    assert _codes(H.record("wox59ex"), "tb.autoscroll_row_skip") == []


# ---------------------------------------------------------------- L1: recycled rows
def _row_tree(title, key=5):
    """A list (scrollable) > a row (the item) > its only text: V6 BAD_B's row title."""
    from inspector_widget.talkback import walk

    def node(k, cls, label="", text="", flags=("visible_to_user",), b=(0, 0, 1080, 2000)):
        n = walk.Node()
        n.key, n.window, n.host, n.virtual, n.cls = k, 1, int(k.split(":")[1]), -1, cls
        n.label, n.text, n.cd, n.bounds, n.flags = label, text, "", b, set(flags)
        n.actions, n.drawing_order, n.pane_title = set(), 0, ""
        return n

    lst = node("view:2", "androidx.recyclerview.widget.RecyclerView",
               flags=("visible_to_user", "scrollable"))
    row = node("view:3", "android.widget.LinearLayout", b=(0, 300, 1080, 150))
    t = node(f"view:{key}", "android.widget.TextView", title, title, b=(40, 320, 600, 80))
    for p, c in ((lst, row), (row, t)):
        c.parent = p
        p.children.append(c)
    return t


def test_l1_a_recycled_rows_only_text_showing_another_mail_is_another_stop():
    from types import SimpleNamespace

    from inspector_widget.talkback import walk

    old, new = _row_tree("Mail 3"), _row_tree("Mail 31")
    assert (old.ctx, old.item_root) == ("", False)  # in a list item, no other text
    model = walk.Model()
    model.stops = [walk.PStop(old.key, old.label, old.label, old.bounds, 1, "TextView",
                              old.ctx, old.item_root)]
    assert model.match(new.key, new.sig, new.bounds, ctx=new.ctx, item_root=False) is None
    assert model.match(old.key, old.sig, old.bounds, ctx=old.ctx).key == "view:5"
    snap = SimpleNamespace(focus=new, key=new.key, index=SimpleNamespace(nodes={}))
    assert walk._seen_again(walk.Step(3, old.key, node=old), snap) is False  # no false wrap
    snap = SimpleNamespace(focus=old, key=old.key, index=SimpleNamespace(nodes={}))
    assert walk._seen_again(walk.Step(3, old.key, node=old), snap) is True
    # outside any list a label that changes in place is the same node ("Play" -> "Pause")
    a, b = _row_tree("Play"), _row_tree("Pause")
    for n in (a, b):
        n.parent.parent.flags.discard("scrollable")
    assert a.ctx is None
    model.stops = [walk.PStop(a.key, a.label, a.label, a.bounds, 1, "TextView", a.ctx)]
    assert model.match(b.key, b.sig, b.bounds, ctx=b.ctx).key == "view:5"


# ------------------------------------------------------------- G9 / L5: system dialogs
NIA = "com.google.samples.apps.nowinandroid.demo.debug"


def _win(name):
    from inspector_widget.talkback import windows

    return windows.parse((H.DATA / f"windows_{name}.txt").read_text())


def test_g9_the_window_list_names_the_system_dialog_over_the_app():
    from inspector_widget.talkback import windows

    # NiA cold start on emulator-5554 (API 37): the "Android App Compatibility" (16 KB)
    # dialog, a window of the system (package android) over the app, with input focus
    wins, focus = _win("nia_16kb_dialog")
    c = windows.covering(wins, NIA, focus)
    assert (c.package, c.type, c.title) == ("android", "APPLICATION_OVERLAY", "android")
    # then the notification permission request: another app's activity on top
    wins, focus = _win("nia_permission_dialog")
    c = windows.covering(wins, NIA, focus)
    assert c.package == "com.google.android.permissioncontroller"
    cover = {"package": c.package, "window": c.title, "type": c.type}
    assert windows.name(cover) == ("com.google.android.permissioncontroller/"
                                   "…GrantPermissionsActivity")
    # the status bar, the navigation bar, the IME and the app's own splash are no cover
    assert windows.covering(wins, "com.oberkfell.a11yprobe", focus) is None  # not shown
    assert windows.covering([w for w in wins if w.package in (NIA, "com.android.systemui")],
                            NIA) is None


def test_g9_a_capture_under_a_system_dialog_shows_no_stop_of_the_app():
    from inspector_widget.capture import fetch
    from inspector_widget.talkback import windows

    cover = {"package": "com.google.android.permissioncontroller", "type": "BASE_APPLICATION",
             "window": "com.google.android.permissioncontroller/com.android.permissioncontroller"
                       ".permission.ui.GrantPermissionsActivity", "frame": [32, 1001, 1216, 937]}
    resp = H.to_proto(H.dump("nia_for_you"))
    raw = T.raw_from_a11y(resp, cid="cforeign", dpi=480)
    fetch._mark_foreign(raw, dict(cover), NIA)
    assert raw.meta.device["foreign_window"]["package"] == cover["package"]
    assert raw.meta.diagnostics[-1].startswith(
        "covered by another app's window: com.google.android.permissioncontroller/"
        "…GrantPermissionsActivity; TalkBack reads it")
    ix = T.build(raw)
    assert ix.reading == []  # "on screen" lists nothing: TalkBack reads the dialog
    stored = pb.DumpA11yResponse.FromString(raw.a11y).diagnostics
    assert windows.from_token(stored)["package"] == cover["package"]
    tree = tb.build(H.dump("nia_for_you"), diagnostics=windows.token(cover))
    assert tb.Navigator(tree).linear() == []
    assert [d["kind"] for d in tree.diagnostics if d["kind"] == "foreign_window"] == [
        "foreign_window"]


def test_g9_tb_walk_fails_fast_naming_the_dialog(monkeypatch):
    from inspector_widget.output import dumps as js
    from inspector_widget.talkback import device, windows

    cover = {"package": "android", "type": "APPLICATION_OVERLAY", "window": "android",
             "frame": [32, 789, 1216, 1361], "focused": True}
    monkeypatch.setattr(windows, "foreign_cover", lambda serial, package: dict(cover))
    monkeypatch.setattr(device, "top_activity", lambda serial: f"{NIA}/.MainActivity")
    with pytest.raises(device.TalkBackError) as e:
        device.ensure_foreground("emulator-5554", NIA)
    assert e.value.code == "app_left_foreground"
    env = {"error": {"code": e.value.code, "message": str(e.value), "hint": e.value.hint}}
    assert "android (APPLICATION_OVERLAY)" in str(e.value) and "BACK" in e.value.hint
    assert len(js(env).encode()) <= 300
    # an activity of another app on top of the app's window (it is on top): no overlay
    cover["window"] = "com.example/.Other"
    assert device.ensure_foreground("emulator-5554", NIA) == {}


# ------------------------------------------------- the round's live walks (emulator-5554)
def test_live_tb4_walk_tags_the_covered_toolbar_and_explains_the_bar():
    rec = H.record("wv2tq6m")
    cov = _codes(rec, "tb.covered_stop")
    assert len(cov) == 1 and cov[0]["steps"] == [0, 1, 2, 3, 4]
    order = _codes(rec, "tb.out_of_order")
    assert "the overlay over the stops read first, added last" in order[0]["msg"]


def test_live_drawer_lap_skips_nothing_and_the_sheet_walk_escapes():
    assert _codes(H.record("wp73dl7"), "tb.skipped") == []
    esc = _codes(H.record("wf5aizc"), "tb.escape")
    assert len(esc) == 1 and esc[0]["steps"] == [1, 2, 3, 4, 5, 6]


def test_live_ap4_walk_names_the_webview_it_cannot_enter():
    rec = H.record("wumcrfa")
    assert [f["code"] for f in rec["findings"] if f["sev"] == "error"] == ["tb.webview_block"]
    assert _codes(rec, "tb.escape") == [] and _codes(rec, "tb.covered_stop") == []


def test_live_nia1_names_every_item_the_bottom_row_scroll_passed_over():
    # this walk started on Compose (column 0, the bottom row) and auto-scrolled straight to
    # Testing, so the rows above it in column 1 (Architecture, Android Studio & Tools) were
    # passed over too; stops the re-models added count wherever the model put them
    f = _codes(H.record("w68v048"), "tb.autoscroll_row_skip")
    assert len(f) == 1 and f[0]["missed"] == [
        "Architecture", "Performance", "Android Studio & Tools", "Kotlin",
        "New APIs & Libraries", "Platform & Releases", "Privacy & Security", "Accessibility"]


def test_live_nia3_two_column_feed_is_interleaved():
    assert len(_codes(H.record("w5k5fwg"), "tb.interleaved")) == 1


def test_live_nia13_a_lap_through_the_whole_list_counts_the_spacer():
    f = _codes(H.record("wwv5qru"), "tb.wrong_announcement")
    assert len(f) == 1 and '"In list. 20 items"' in f[0]["msg"] and "reached 19" in f[0]["msg"]
    # the same list scrolled to its middle: the lap starts there and cannot tell
    assert _codes(H.record("wdz81d8"), "tb.wrong_announcement") == []


def test_live_l1_recycled_rows_are_neither_a_wrap_nor_interleaved():
    # A11yProbe V6 BAD_B: 50 mails in a RecyclerView, row Views rebound as TalkBack
    # auto-scrolls; 10 runs on emulator-5554 gave this same walk
    rec = H.record("wfx9awc")
    assert rec["ended"] == "wrap" and len(rec["steps"]) == 53
    assert rec["findings"] == []
    assert (rec["vs_model"]["agree"], rec["vs_model"]["differ"]) == (50, 0)


def test_a_remodel_puts_a_new_top_stop_at_the_top():
    # AntennaPod's feed: the collapsing toolbar's title appears once the list scrolls; it
    # comes first in the new order, so it goes before the stops the model knows, not after
    # the bottom navigation (where a lap would call everything between "skipped")
    from inspector_widget.talkback import walk
    from test_tb_rules import CLICK, FOCUS, n, root

    def screen(*top):
        rows = [n(2, cls="android.widget.Button", text="Play", flags=FOCUS, actions=[CLICK],
                  b=(0, 400, 1080, 120)),
                n(3, cls="android.widget.Button", text="Next", flags=FOCUS, actions=[CLICK],
                  b=(0, 600, 1080, 120))]
        return {"windows": [{"root_view_id": 1, "root": root(*top, *rows)}]}

    model = walk.Model()
    model.build(H.to_proto(screen()), False)
    title = n(9, cls="android.widget.TextView", text="Planet Money", b=(0, 160, 1080, 100))
    model.remodel(H.to_proto(screen(title)), False)
    assert [(p.key, p.added) for p in model.stops] == [
        ("view:9", 1), ("view:2", None), ("view:3", None)]


def test_live_l4_a_collapsing_title_the_model_learned_of_late_is_no_skip():
    rec = H.record("w9h3ogw")
    sk = _codes(rec, "tb.skipped")
    assert [(f["sev"], f["basis"]) for f in sk] == [("info", "model")]
    assert "Back | Planet Money" in sk[0]["msg"]
    assert _codes(rec, "tb.revisit") == []  # rows rebound to other episodes are not re-reads
