"""Accessibility lint and the TalkBack model against real apps (REALAPP_RESULTS B7, B8, B10).

tests/data/realapps/ holds a11y dumps of Thunderbird, Now in Android and AntennaPod screens and
the TalkBack 17.0 walks over them (see its README). Each lint false positive the walks proved
is pinned here as gone, each real finding as kept; and the model's walk is held to TalkBack's,
press for press, order and words.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from inspector_widget import a11y_lint as L
from inspector_widget import output
from inspector_widget import talkback as tb
from inspector_widget.talkback import rules as R

DATA = Path(__file__).parent / "data" / "realapps"
WALKS = json.loads(gzip.decompress((DATA / "talkback17_walks.json.gz").read_bytes()))
DENSITY = 480  # emulator-5554


def dump(name):
    return json.loads(gzip.decompress((DATA / f"{name}.a11y.json.gz").read_bytes()))


def lint(name, **kw):
    return L.lint_unified(dump(name), L.LintContext(density=DENSITY), **kw)


def on(report, rule, key=None, findings=None):
    return [f for f in (report.findings if findings is None else findings)
            if f.rule == rule and (key is None or f.node_key == key)]


# --------------------------------------------------------------------------------- B8 lint
def test_long_clickable_lists_talkback_never_stops_on_are_not_unlabelled():
    # AntennaPod's long-clickable RecyclerViews (actionable_reason long_clickable) had an R1
    # error each; TalkBack's walk over home (28/28) and episodes never stopped on them.
    for name, keys in (("antennapod_home", ("view:253", "view:261")),
                       ("antennapod_episodes", ("view:2183",))):
        rep = lint(name)
        assert not [f for f in on(rep, "a11y.label.missing") if f.node_key in keys], name
        assert any(d["code"] == "label.silent_containers" for d in rep.diagnostics)


def test_real_unlabelled_controls_are_still_errors():
    # The ones TalkBack 17 announced as "Unlabelled" / only "Button" stay R1 errors.
    for name, key in (("thunderbird_drawer", "compose:285:836"),      # "Unlabelled"
                      ("thunderbird_message", "view:849"),            # "Button"
                      ("thunderbird_list_compose", "compose:753:1166"),  # "Button" (star)
                      ("thunderbird_onboarding", "compose:51:647")):  # "On. Switch"
        assert [f.severity for f in on(lint(name), "a11y.label.missing", key)] == ["error"], name


def test_unselected_tabs_in_lazy_items_are_not_missing_their_state():
    # Thunderbird's drawer: each folder is a Tab in its own LazyColumn item; the selected one
    # ("selected. Inbox. 7. Tab") is in another item, and the account actions are a second
    # list. 11 R7 warnings before; TalkBack reads "Outbox. Tab" exactly as Material intends.
    assert on(lint("thunderbird_drawer"), "a11y.state.not_exposed") == []


def test_a_40dp_button_at_the_screen_edge_is_judged_not_excused_as_clipped():
    # The overflow button sits flush with the right edge (x 1160..1280) in every toolbar:
    # 120px at 480dpi is a real 40dp target, not a clipped one.
    for name, key in (("antennapod_home", "view:223"), ("thunderbird_message", "view:715"),
                      ("thunderbird_list_views", "view:312")):
        f = on(lint(name), "a11y.touch_target.small", key)
        assert [x.severity for x in f] == ["warn"], name
        assert f[0].evidence["w_dp"] == 40.0 and "clipped_axes" not in f[0].evidence


def test_a_page_of_a_pager_at_rest_is_not_clipped_at_its_edges():
    # Thunderbird's message pager can page right, but the header's More options button on the
    # current page is wholly shown: judged, not "clipped".
    f = on(lint("thunderbird_message"), "a11y.touch_target.small", "view:859")
    assert [x.severity for x in f] == ["warn"]


def test_targets_clipped_by_a_scrolled_list_stay_info():
    # Now in Android's feed, scrolled: the topic chips at the list's top edge really are cut
    # off (the list scrolls back), so their size, and their label, are not known yet.
    rep = lint("nia_feed")
    for key in ("compose:8:469", "compose:8:473"):
        assert [f.severity for f in on(rep, "a11y.touch_target.small", key)] == ["info"]
        r1 = on(rep, "a11y.label.missing", key)
        assert [f.severity for f in r1] == ["info"] and r1[0].evidence["clipped_axes"] == ["h"]
    assert rep.summary["error"] == 0


def test_findings_under_an_open_sheet_are_counted_apart():
    # AntennaPod with the filter sheet open: all but one finding sit on the activity behind
    # the sheet. They no longer fill the summary, and the overlay leaves them out.
    rep = lint("antennapod_filter_sheet")
    out = rep.to_dict()
    assert out["summary"]["total"] == len(out["findings"]) == 1
    assert out["summary"]["covered"]["total"] == 9 == len(rep.covered)
    assert all(f.window["covered_by"] is not None for f in rep.covered)
    # every covered finding is in the JSON: in full, and grouped by rule when brief
    assert [f["node_key"] for f in out["covered_findings"]] == [f.node_key for f in rep.covered]
    brief = output.slim("a11y_lint", out, {})
    assert sum(r["n"] for r in brief["covered_by_rule"].values()) == 9
    listed = output.slim("a11y_lint", out, {"group_by": "none"})
    assert {f["node_key"] for f in listed["covered_findings"]} == {f.node_key for f in rep.covered}
    text = L.format_text(rep)
    assert "(+9 under an open dialog)" in text.splitlines()[0]
    assert "-- 9 finding(s) on window(s) under an open dialog" in text


def test_web_links_get_web_advice_and_the_inline_exception():
    # AntennaPod's show notes: links inside lines of text (WCAG 2.5.8's inline exception), and
    # never Compose advice for web content.
    rep = lint("antennapod_player")
    assert not [f for f in on(rep, "a11y.touch_target.small") if f.node_key.startswith("virtual:")]
    assert not [f for f in rep.findings if f.node_key.startswith("virtual:")
                and "Modifier." in f.message]


def test_real_app_lint_counts_now():
    # (error, warn, info, under a dialog) at 480dpi without contrast; before this round, on
    # the same dumps, in the comments. Every change is one of the B8 fixes above, or a rule
    # of the real-app hunt round (lint-and-store; tests/test_realapp_hunt_lint.py pins each).
    names = ("antennapod_home", "antennapod_episodes", "antennapod_filter_sheet",
             "antennapod_player", "thunderbird_drawer", "thunderbird_message", "nia_feed",
             "nia_for_you", "nia_settings_dialog", "thunderbird_settings", "nia_interests",
             "thunderbird_list_views", "thunderbird_list_compose_tb_first")
    counts = {name: (s["error"], s["warn"], s["info"], (s.get("covered") or {}).get("total", 0))
              for name in names for s in [lint(name).summary]}
    assert counts == {
        "antennapod_home": (0, 7, 6, 0),          # was 2, 6, 7: 2 R1 lists, 1 edge R2 excused
        "antennapod_episodes": (0, 9, 0, 0),      # was 1, 8, 1
        "antennapod_filter_sheet": (0, 0, 1, 9),  # was 1, 8, 2 with 10 behind the sheet
        "antennapod_player": (1, 1, 14, 0),       # was 7, 1, 15: 6 R2 errors on inline web links;
                                                  # +1 R9 no_headings: 39 text stops of show
                                                  # notes (web paragraphs count by their text)
        "thunderbird_drawer": (1, 0, 2, 0),       # was 1, 11, 2: 11 R7 on unselected tabs
        "thunderbird_message": (1, 4, 4, 0),      # was 1, 1, 7: 3 real 40dp buttons excused
        "nia_feed": (0, 0, 15, 0),                # was 5, 0, 9: R1 on chips scrolled half away;
                                                  # +1 R9 no_headings (8 text stops, scrolls)
        "nia_for_you": (0, 0, 6, 0),              # was 0, 0, 0: R12 now names rows by their
                                                  # name, not their state: each topic row and
                                                  # its own Checkbox are both stops named
                                                  # "Headlines" (TalkBack 17 stops on both)
        "nia_settings_dialog": (0, 0, 3, 15),     # was 5, 0, 13 with 14 behind the dialog,
                                                  # and 4 R12 on rows saying "Not selected";
                                                  # +3 R9 section titles on the dialog (Theme,
                                                  # Use Dynamic Color, Dark mode preference),
                                                  # +1 R9 no_headings behind it (For you)
        "thunderbird_settings": (0, 7, 0, 0),     # was 0, 3, 0 (3 R2 on the section titles):
                                                  # +3 R9 section titles that are list items,
                                                  # +1 R20 "Account settings" on 8 rows (TB-11)
        "nia_interests": (0, 0, 1, 0),            # was 0, 0, 0: +1 R23 "Not selected" on every
                                                  # row (NIA-9)
        "thunderbird_list_views": (0, 13, 7, 0),  # was 0, 13, 6: +1 R21 "Star" in every row
                                                  # while its own button says "Add star" (TB-12)
        "thunderbird_list_compose_tb_first": (5, 2, 10, 0),  # was 5, 6, 5: 5 Compose stars
                                                  # with a clear 48dp touch area are R2 info,
                                                  # +1 R19 "[conversation_counter]" (TB-7)
    }


# ------------------------------------------------------------------- B10: the model vs TalkBack
# How each walk lines up with its dump: "label" when the dump came from another visit (the
# ids differ), aliases for toolbar menu items re-created between the dump and the walk, and
# where the comparison stops (TalkBack auto-scrolls, the model does not).
WALK_CASES = {
    "antennapod_player_expanded": {},  # the walk ends stuck before the show notes WebView
    "antennapod_home_player_collapsed": {"upto": 8},  # TalkBack auto-scrolls at step 9
    "antennapod_home": {},
    "antennapod_episodes": {},
    "antennapod_filter_sheet": {},
    "thunderbird_drawer": {},
    "thunderbird_theme_dialog": {"match": "label"},
    "thunderbird_list_views": {"aliases": {"view:503": "view:326"}},
    "thunderbird_list_compose": {"aliases": {"view:834": "view:729"}},
    "thunderbird_message": {},
    "thunderbird_onboarding": {},
    "nia_for_you": {"upto": 13},
    "nia_settings_dialog": {"match": "label"},
}


def _start(tree, case, walk):
    if case.get("match") != "label":
        return tree.node(walk["start"]["key"])
    rules = R.Rules(tree)
    return next(n for n in tree.nodes if (n.content_description or n.text) == walk["start"]["label"]
                and rules.should_focus_node(n))


@pytest.mark.parametrize("name", sorted(WALK_CASES))
def test_model_walk_equals_talkback_17_press_for_press(name):
    case, walk = WALK_CASES[name], WALKS[name]
    tree = tb.build(dump(name))
    steps = walk["steps"][:case.get("upto", len(walk["steps"]))]
    order = tb.simulate(tree, start=_start(tree, case, walk), until="steps",
                        max_steps=len(steps), keyboard=True)
    aliases = case.get("aliases", {})
    if order.ended == "trap":  # TalkBack stays put from here: every press is the same no-move
        stuck = order.steps[-1]
        order.steps.extend(dict(stuck) for _ in range(len(steps) - len(order.steps)))
    for (moved, key, _label, said), m in zip(steps, order.steps, strict=True):
        if not moved:
            assert m.get("edge") or m.get("stuck"), (name, key, m)
            continue
        if case.get("match") != "label":
            assert m["key"] == aliases.get(key, key), (name, key, m["key"])
        assert m["speak"] == said, (name, key)


def test_the_message_body_webview_is_walked_like_talkback_does():
    # B7a: TalkBack stops on the WebView ("Webview") and then on the web content (a paragraph,
    # "cid:part1@example. Image"); the model used to stop once and edge out.
    keys = tb.simulate(tb.build(dump("thunderbird_message"))).keys()
    at = keys.index("virtual:894:4")
    assert keys[at:at + 3] == ["virtual:894:4", "virtual:894:7", "virtual:894:2"]


def test_zero_size_web_content_is_read_like_talkback_17_does():
    # Live, AntennaPod's home: after "You can download any episode..." TalkBack read the
    # collapsed player's show notes, every element 0px tall below the screen (its WebView sits on
    # the player pager's off-screen page): "Webview. In vertical pager", then each paragraph,
    # heading and link in document order. The model reads the same, flags the page as hidden, and
    # the lint of that walk calls it a ghost.
    walk = WALKS["antennapod_home_player_collapsed"]
    read = [(key, said) for moved, key, _label, said in walk["steps"]
            if moved and key.startswith("virtual:695:")]
    tree = tb.build(dump("antennapod_home_player_collapsed"))
    order = tb.simulate(tree, keyboard=True)
    keys = order.keys()
    at = keys.index("virtual:695:23")
    assert keys[at:at + len(read)] == [k for k, _ in read]
    stops = order.stops[at:at + len(read)]
    assert [s["speak"] for s in stops[1:]] == [said for _, said in read[1:]]
    assert stops[0]["speak"].startswith("Webview")
    assert stops[0]["hidden_page"] is True
    hint = next(h for h in order.hints if h["kind"] == "web_hidden_page")
    assert hint["trap"] is False and hint["web_root"] == "virtual:695:23"


def test_going_back_into_a_webview_lands_on_its_last_element_and_skips_its_root():
    # Live, Thunderbird's message body walked backwards (Meta+Left) on TalkBack 17.0: the wrap
    # lands on the image (the page's last element), then the paragraph, then the native button
    # before the WebView: the root "Webview" is never a stop going back.
    walk = WALKS["thunderbird_message_backward"]
    tree = tb.build(dump(walk["dump"]))
    order = tb.simulate(tree, start=walk["start"]["key"], direction="prev", until="steps",
                        max_steps=len(walk["steps"]), keyboard=True)
    for (moved, key, _label, said), m in zip(walk["steps"], order.steps, strict=True):
        assert (m["key"] if not m.get("edge") else None) == (key if moved else None), (key, m)
        if moved:
            assert m["speak"] == said
    assert "virtual:314:4" not in order.keys()


def test_the_antennapod_player_trap_is_named_and_explained():
    # Live on TalkBack 17.0: after "Shownotes" every press stayed put. The next stop is the show
    # notes WebView on the vertical pager's off-screen page: the model flags it, and a walk stuck
    # there is a tb.trap (it used to be a bare tb.edge_stuck).
    from inspector_widget.talkback import diff

    tree = tb.build(dump("antennapod_player_expanded"))
    hidden = tb.Navigator(tree).hidden_web_pages()
    assert [(h["before"], h["web_root"]) for h in hidden] == [("view:350", "virtual:359:23")]
    walk = WALKS["antennapod_player_expanded"]
    steps = [{"i": 0, "key": walk["start"]["key"], "ref": walk["start"]["key"], "moved": True,
              "via": "start"}]
    for i, (moved, key, label, said) in enumerate(walk["steps"], 1):
        steps.append({"i": i, "key": key, "ref": key, "label": label, "moved": moved,
                      "via": "next" if moved else "edge", **({} if moved else {"edge": True})})
    record = {"steps": steps, "ended": "stuck", "predicted": [], "direction": "next",
              "web_traps": hidden}
    codes = [f["code"] for f in diff.analyze(record)["findings"]]
    assert codes[0] == "tb.trap"


def test_going_back_into_a_page_nobody_can_see_reads_it_and_says_so():
    # Live, AntennaPod's expanded player walked backwards (Meta+Left) on TalkBack 17.0 from the
    # position slider: TalkBack goes into the show notes on the vertical pager's off-screen page
    # through its last element and reads on, 6/6 presses as the model has it. No trap going back
    # (forward, the same page traps). The model's walk says so: a web_hidden_page without a trap,
    # so the walk's lint is a tb.ghost_stop; it used to say nothing going back.
    from inspector_widget.talkback import diff

    walk = WALKS["antennapod_player_expanded_backward"]
    tree = tb.build(dump(walk["dump"]))
    back = tb.simulate(tree, start=walk["start"]["key"], direction="prev", until="steps",
                       max_steps=len(walk["steps"]), keyboard=True)
    assert [m.get("key") for m in back.steps] == [key for moved, key, _l, _s in walk["steps"]
                                                  if moved]
    said = [s for _m, _k, _l, s in walk["steps"]]
    model = [m["speak"] for m in back.steps]
    assert model == said  # a lone "." is spoken by its name: "Period" (speech.SYMBOL_NAMES)
    hidden = [h for h in back.hints if h["kind"] == "web_hidden_page"]
    assert [(h["web_root"], h["trap"], h["before"]) for h in hidden] == [
        ("virtual:695:23", False, "view:289")]
    assert diff.web_trap_finding(hidden[0], basis="model")["code"] == "tb.ghost_stop"
    fwd = tb.simulate(tree, keyboard=True)
    assert fwd.ended == "trap"
    assert [h["trap"] for h in fwd.hints if h["kind"] == "web_hidden_page"] == [True]


# ---------------------------------------------------------------- auto-scroll ahead (item 4)
def test_the_model_says_what_auto_scroll_reads_before_the_next_control():
    # Now in Android's For-you grid: TalkBack auto-scrolled through ~16 more topics and was
    # still in the grid at step 30, far from Done and the bottom bar. The grid reports no size.
    fy = tb.simulate(tb.build(dump("nia_for_you")))
    hint = next(h for h in fy.hints if h["kind"] == "autoscroll_ahead")
    assert hint["container"] == "compose:8:85" and hint["offscreen"] is None
    assert hint["next"] == "compose:8:90" and hint["next_speak"].startswith("Done. Button")
    assert "does not report how many" in hint["message"]
    # AntennaPod's episode list knows its size: 150 episodes, 8 on screen.
    eps = tb.simulate(tb.build(dump("antennapod_episodes")))
    hint = next(h for h in eps.hints if h["kind"] == "autoscroll_ahead")
    assert hint["container"] == "view:2183" and hint["offscreen"] == 142
    assert "142 more item(s)" in hint["message"]


# ------------------------------------------------------------- the default tb.* rules
#: what the default tb.* rules (capture/rules.py DEFAULT_TB) report on each recorded real
#: screen: only Thunderbird's counted empty header ("2 of 7"), which a real walk confirmed,
#: on View rows and (TB-1, wcw61ax "2 of 6": the stops are inside each row's ComposeView cell)
#: on Compose rows; with TalkBack started after the app (_tb_later) the rows have no
#: positions to get wrong. AntennaPod's rows (speech_order), its show notes (past an edge) and
#: Thunderbird's message pager were false positives the walks disproved.
REAL_DEFAULT_TB = {"thunderbird_list_views": {"tb.wrong_announcement": 1},
                   "thunderbird_list_compose": {"tb.wrong_announcement": 1},
                   "thunderbird_list_compose_tb_first": {"tb.wrong_announcement": 1}}


@pytest.mark.parametrize("name", sorted(p.name.split(".")[0] for p in DATA.glob("*.a11y.json.gz")))
def test_the_default_tb_rules_are_precise_on_every_recorded_real_screen(name):
    from collections import Counter

    from inspector_widget.capture.rules import DEFAULT_TB
    from inspector_widget.talkback import static

    fs = static.findings(tb.Navigator(tb.build(dump(name))), density=DENSITY, codes=DEFAULT_TB)
    assert dict(Counter(f.code for f in fs)) == REAL_DEFAULT_TB.get(name, {}), \
        [(f.code, f.node.key, f.evidence) for f in fs]
