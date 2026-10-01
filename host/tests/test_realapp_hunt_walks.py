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
