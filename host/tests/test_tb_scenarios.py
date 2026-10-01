"""tb_scenario: verdicts, the action grammar, the restore settle and what TalkBack said
(gaps G11-G14, G23), offline.

The verdict table runs on plain :class:`scenarios.Facts` shaped like the hunt's runs (NIA-5
moved_to_nav, NIA-6 reset_to_top, AP-9 returned_to_opener, t62wh1t nothing_happened, TB-4
not on_close_or_unlabeled). The rest drives the real scenario code against the fake device
(fakeagent: TalkBack moved by uinput keys, A11yAct, the verbose logcat), and checks that the
CLI and the MCP server give the same errors.
"""

from __future__ import annotations

import json
import time

import pytest

import fakeagent
from fakeagent import DEFAULT_PACKAGE as PKG
from fakeagent import DEFAULT_SERIAL as SERIAL
from fakeagent import TB_TITLE, ViewSpec, tb_item

import inspector_widget as iw
from inspector_widget.capture import walks
from inspector_widget.output import dumps, utf8_len
from inspector_widget.talkback import scenarios as S
from inspector_widget.talkback import walk as tbwalk

FAST = {"step_timeout_ms": 250, "settle_ms": 20}
ORDER = [TB_TITLE] + [tb_item(i) for i in range(6)]
TABS = [(1061, -1), (1062, -1)]


def _views(scene):
    return {v.id: v for r in scene.roots for v in r.walk()}


def nav_scene():
    """talkback_scene plus a bottom navigation bar (Home, Saved) as tabs."""
    scene = fakeagent.talkback_scene()
    tabs = [ViewSpec(1061 + i, "NavigationBarItemView", "com.google.android.material.navigation",
                     (i * 180, 560, 180, 80), text=t,
                     a11y={"class_name": "android.widget.FrameLayout", "text": t,
                           "role_description": "Tab", "clickable": True, "focusable": True,
                           "actions": [(0x10, None), (0x40, None)]})
            for i, t in enumerate(("Home", "Saved"))]
    bar = ViewSpec(1060, "BottomNavigationView", "com.google.android.material.bottomnavigation",
                   (0, 560, 360, 80),
                   a11y={"class_name": "com.google.android.material.bottomnavigation"
                                       ".BottomNavigationView"}, children=tabs)
    scene.roots[0].children[0].children.append(bar)
    return scene


@pytest.fixture
def probe(tb_env, monkeypatch):
    """The title + 6 items (+ a tab bar) scene, TalkBack's order over it, verbose logging,
    and a check that the device ends as it began, with no unsafe key."""
    monkeypatch.setattr(S, "WINDOW_QUIET_S", 0.1)
    tb_env.scene_factory = nav_scene
    tb = tb_env.talkback
    tb.order = list(ORDER) + TABS
    tb.labels = {TB_TITLE: "Title", **{tb_item(i): f"Item {i}. Button" for i in range(6)},
                 TABS[0]: "selected. Home. Tab", TABS[1]: "Saved. Tab"}
    tb.log_level = "VERBOSE"
    tb_env.original = dict(tb_env.secure)
    yield tb_env
    assert fakeagent.settings_changes(tb_env, tb_env.original) == {}
    assert fakeagent.key_safety_violations(tb_env) == []


def scenario(kind, **kw):
    session = iw.attach(SERIAL, PKG)
    try:
        return S.run_scenario(session, kind, **{**FAST, "wait_ms": 400, **kw})
    finally:
        session.disconnect()


def _remove(dev, vid):
    """The app removes View ``vid`` (and says so: a content change)."""
    scene = dev.live_scene(PKG)
    for v in _views(scene).values():
        v.children = [c for c in v.children if c.id != vid]
    dev.talkback.order = [t for t in dev.talkback.order if t[0] != vid]
    dev.agent(PKG).a11y_tap.record(fakeagent.TYPE_WINDOW_CONTENT_CHANGED, 1001, 1011, -1,
                                   content_change_types=1)


# --------------------------------------------------------------------------- the grammar
def test_the_action_grammar_parses_sequences_and_names_itself_when_wrong():
    steps = S.parse_action("activate; walk:8")
    assert [(s.kind, s.arg) for s in steps] == [("activate", ""), ("walk", "8")]
    steps = S.parse_action("pre:pane=Saved; long-press; custom:Delete; expect:Undo; wait:200")
    assert [s.kind for s in steps] == ["pre", "long_press", "custom", "expect", "wait"]
    assert S.parse_action("tap:n12")[0].arg == "n12"
    for bad in ("swipe", "walk:0", "walk:x", "custom:", "pre:color=red", "activate:x", ""):
        with pytest.raises(ValueError) as err:
            S.parse_action(bad)
        assert S.GRAMMAR in str(err.value), bad
    assert S.unparse(S.parse_action(" activate ;walk:3 ")) == "activate; walk:3"


# --------------------------------------------------------------------------- verdicts (G12)
def F(**kw):
    base = {"f0_key": "compose:8:1706", "f1_key": "compose:8:19"}
    return S.Facts(**{**base, **kw})


def test_verdicts_on_the_hunts_shapes():
    # NIA-5: Unbookmark removed the card; focus on the Saved tab (labelled)
    v, why = S.classify(F(f1_label="Saved", f1_speech="Saved. Tab", f1_nav=True, target_gone=True,
                          tree_changed=True, first_key="compose:8:57"))
    assert v == "moved_to_nav" and "target was removed" in why
    # NIA-6: Done removed the onboarding; focus reset to Search, the first stop (not initial_ok)
    v, _ = S.classify(F(f1_key="compose:8:57", f1_label="Search", target_gone=True,
                        tree_changed=True, first_key="compose:8:57", model_initial="compose:8:57"))
    assert v == "reset_to_top"
    # TB-3/TB-4/TB-5: "Navigate up" is no close button
    v, _ = S.classify(F(f1_key="view:32", f1_label="Navigate up", target_gone=True,
                        tree_changed=True, first_key="view:32"))
    assert v == "reset_to_top"
    assert S.classify(F(f1_key="view:32", f1_label="Navigate up", target_gone=True,
                        f1_covered="ActionBarContextView view:1855"))[0] == "behind_overlay"
    # AP-9: back closed the filter sheet, focus back on Filter (not the window's first stop)
    v, _ = S.classify(F(f1_key="view:51", f1_label="Filter", closed=True, f1_was_there=True,
                        tree_changed=True, first_key="view:12"))
    assert v == "returned_to_opener"
    # t62wh1t: activate on a node that cannot act: nothing changed
    v, why = S.classify(F(f1_key="compose:8:1706", same_node=True, target_clicks=False))
    assert v == "nothing_happened" and "no click action" in why
    assert S.classify(F(f1_key="compose:8:1706", same_node=True, tree_changed=True))[0] \
        == "stayed_on_opener"
    # a sequence that opened a screen and came back to the target
    assert S.classify(F(f1_key="compose:8:1706", same_node=True, tree_changed=True,
                        opened_between=True))[0] == "returned_to_opener"
    # initial_ok only on a new screen
    assert S.classify(F(f1_label="Back", new_screen=True, first_key="compose:8:19"))[0] == "initial_ok"
    assert S.classify(F(f1_label="Back", new_screen=False, first_key="compose:8:19"))[0] == "reset_to_top"
    assert S.classify(F(f1_label="Close", new_screen=True))[0] == "on_close_or_unlabeled"
    assert S.classify(F(f1_label="", f1_unlabelled=True))[0] == "on_close_or_unlabeled"
    assert S.classify(F(f1_key=None))[0] == "none"
    assert S.classify(F(left_app=True))[0] == "left_app"
    # a navigation rail's first tab is the screen's first stop: a reset, not a move to nav
    assert S.classify(F(f1_label="For you", f1_nav=True, target_gone=True, tree_changed=True,
                        first_key="compose:8:19"))[0] == "reset_to_top"


def test_moved_to_nav_on_the_device_quotes_the_spoken_label(probe):
    def unbookmark(tb, target):
        _remove(tb.device, 1022)
        tb.set_focus(TABS[1])

    probe.talkback.on_click = unbookmark
    res = scenario("focus_after", target="Item 2", action="activate")
    assert res["verdict"] == "moved_to_nav", res
    assert res["why"] == "same screen; focus thrown to the navigation bar (the target was removed)"
    assert res["finding"]["code"] == "tb.focus_reset"
    assert res["finding"]["msg"].endswith('to the navigation bar (view:1062 "Saved. Tab")')
    assert "previous one" in res["finding"]["fix"] and "announce" in res["finding"]["fix"]
    assert res["speak_after"] == "Saved. Tab" and res["speak_before"] == "Item 2. Button"


def test_reset_to_top_when_the_focused_item_goes(probe):
    def done(tb, target):
        _remove(tb.device, 1022)
        tb.set_focus(TB_TITLE)

    probe.talkback.on_click = done
    res = scenario("focus_after", target="Item 2", action="activate")
    assert res["verdict"] == "reset_to_top" and res["new_screen"] is False
    assert res["finding"]["code"] == "tb.focus_reset"


def test_nothing_happened_when_nothing_changed(probe):
    res = scenario("focus_after", target="Item 2", action="activate")
    assert res["verdict"] == "nothing_happened", res
    assert res["flags"] == ["activated, nothing spoken"]
    assert "finding" not in res


def test_a_dialog_that_opens_on_its_first_stop_is_initial_ok(probe):
    def open_dialog(tb, target):
        scene = tb.device.live_scene(PKG)
        ok = ViewSpec(2011, "Button", "android.widget", (40, 200, 120, 48), text="OK",
                      a11y={"class_name": "android.widget.Button", "text": "OK",
                            "clickable": True, "focusable": True})
        scene.roots.append(ViewSpec(2001, "DecorView", "com.android.internal.policy",
                                    (20, 180, 320, 200),
                                    a11y={"class_name": "android.widget.FrameLayout"},
                                    children=[ok]))
        tb.set_focus((2011, -1))

    probe.talkback.on_click = open_dialog
    res = scenario("focus_after", target="Item 1", action="activate")
    assert res["verdict"] == "initial_ok" and res["new_screen"] is True
    assert "finding" not in res


def test_a_drawer_opening_in_the_same_window_is_a_new_screen(probe):
    """Thunderbird's navigation drawer: no new window, no pane title, the opener (Navigate
    up) stays; focus goes to the first of the drawer's entries: initial_ok, no finding."""
    def open_drawer(tb, target):
        scene = tb.device.live_scene(PKG)
        entries = [ViewSpec(1100 + i, "TextView", "android.widget", (0, 100 + 60 * i, 200, 48),
                            text=t, a11y={"class_name": "android.widget.TextView", "text": t,
                                          "clickable": True, "focusable": True})
                   for i, t in enumerate(("Account", "Inbox", "Outbox", "Settings"))]
        drawer = ViewSpec(1099, "NavigationView", "com.google.android.material.navigation",
                          (0, 0, 220, 640), a11y={"class_name": "android.widget.FrameLayout"},
                          children=entries)
        scene.roots[0].children[0].children.append(drawer)
        tb.order = [(1100 + i, -1) for i in range(4)] + tb.order
        tb.device.agent(PKG).a11y_tap.record(fakeagent.TYPE_WINDOW_CONTENT_CHANGED, 1001, 1002, -1,
                                             content_change_types=1)
        tb.set_focus((1100, -1))

    probe.talkback.on_click = open_drawer
    res = scenario("focus_after", target="Item 1", action="activate")
    assert res["verdict"] == "initial_ok" and res["new_screen"] is True, res
    assert "finding" not in res


def test_a_destination_that_replaces_the_list_in_the_same_window_is_a_new_screen(probe):
    """Thunderbird's General settings > Display: a fragment replaces the list in the same
    window, the top bar gets another title, and TalkBack puts focus on its first stop."""
    def open_display(tb, target):
        views = _views(tb.device.live_scene(PKG))
        views[1003].text = views[1003].a11y["text"] = "Display"
        _remove(tb.device, 1021)
        tb.set_focus(TB_TITLE)

    probe.talkback.on_click = open_display
    res = scenario("focus_after", target="Item 1", action="activate")
    assert res["verdict"] == "initial_ok" and res["new_screen"] is True, res
    assert "finding" not in res


def test_back_from_a_sheet_to_its_opener_is_returned_to_opener(probe):
    """AP-9: the filter sheet closes on back and focus goes back to Filter."""
    clear = ViewSpec(2011, "Button", "android.widget", (40, 400, 120, 48), text="Clear",
                     a11y={"class_name": "android.widget.Button", "text": "Clear",
                           "clickable": True, "focusable": True})
    sheet = ViewSpec(2001, "DecorView", "com.android.internal.policy", (0, 380, 360, 260),
                     a11y={"class_name": "android.widget.FrameLayout"}, children=[clear])

    def scene():
        s = nav_scene()
        s.roots.append(sheet)
        return s

    probe.scene_factory = scene
    probe.talkback.order = [(2011, -1)]

    def on_input(args):
        # (TalkBack's start sends a BACK too, for its permission dialog: not that one)
        if args == ["keyevent", "KEYCODE_BACK"] and probe.talkback.focus == (2011, -1):
            live = probe.live_scene(PKG)
            live.roots = [r for r in live.roots if r.id != 2001]
            probe.talkback.order = list(ORDER) + TABS
            probe.talkback.set_focus(tb_item(3))  # the opener

    probe.on_input = on_input
    res = scenario("focus_after", target="Clear", action="back")
    assert res["verdict"] == "returned_to_opener", res
    assert "finding" not in res


# --------------------------------------------------------------------------- what was said (G11)
def test_timeline_carries_speech_reasons_and_announcements(probe):
    def select(tb, target):
        tb.log("TalkBackFeedbackProvider:  TYPE_WINDOW_STATE_CHANGED:  ttsOutput= 1 selected    "
               "queueMode=1")
        _remove(tb.device, 1021)
        tb.log("EventTypeViewAccessibilityFocusedFeedbackRule:  viewAccessibilityFocused: (7) , "
               "ttsOutput={Title}, isInitialFocus=true, isRestoreFocusOrEnsureOnScreen=false, "
               "isEventNavigateByUser=false, isDeviceScreenNoTouch=false,")
        tb.set_focus(TB_TITLE)

    probe.talkback.on_click = select
    res = scenario("focus_after", target="Item 1", action="activate")
    said = [e for e in res["timeline"] if "said" in e]
    assert said and said[-1]["said"] == "Title" and said[-1]["why"] == "initial"
    assert any(e.get("announced") == "1 selected" for e in res["timeline"])
    assert res["announced"] == ["1 selected"] and res["speak_after"] == "Title"
    assert "flags" not in res


def test_without_the_verbose_log_the_speech_is_not_made_up(probe):
    probe.talkback.log_level = "ERROR"
    res = scenario("focus_after", target="Item 2", action="activate")
    assert res["speech"].startswith("not logged") and "flags" not in res
    assert "speak_after" not in res


def test_the_log_parser_reads_talkback_17_lines():
    lines = [
        "         1790846354.711 23067 23067 V talkback: EventTypeViewAccessibilityFocusedFeedbackRule:"
        "  viewAccessibilityFocused: (614316) , ttsOutput={Webview. In horizontal pager}, "
        "isInitialFocus=false, isRestoreFocusOrEnsureOnScreen=true, isEventNavigateByUser=false, "
        "isDeviceScreenNoTouch=false,",
        "         1790846354.711 23067 23067 V talkback: TalkBackFeedbackProvider:  "
        "TYPE_VIEW_ACCESSIBILITY_FOCUSED:  ttsOutput= Webview. In horizontal pager    queueMode=0  "
        "ttsAddToHistory",
        "         1790846356.017 23067 23067 V talkback: TalkBackFeedbackProvider:  EVENT_SPEAK_HINT:  "
        "ttsOutput= Press select to activate    queueMode=0",
        "         1790846306.564 23067 23067 V talkback: TalkBackFeedbackProvider:  "
        "TYPE_WINDOW_CONTENT_CHANGED:  ttsOutput=     queueMode=0  ttsAddToHistory",
        "         1790846356.019 23067 23067 V talkback: TalkBackFeedbackProvider:  "
        "TYPE_WINDOW_STATE_CHANGED:  ttsOutput= Thunderbird Debug    queueMode=1",
        "         1790846357.000 23067 23067 V talkback: TalkBackFeedbackProvider:  "
        "TYPE_VIEW_CLICKED:  ttsOutput=     queueMode=0",
        # a window state with nothing to say logs no queueMode: its flags end it
        "         1790848978.692 23067 23067 V talkback: TalkBackFeedbackProvider:  "
        "TYPE_WINDOW_STATE_CHANGED:  ttsOutput=     ttsAddToHistory  "
        "forceFeedbackEvenIfAudioPlaybackActive  forceFeedbackEvenIfPhoneCallActive",
        "         1790848978.694 23067 23067 V talkback: TalkBackFeedbackProvider:  "
        "TYPE_WINDOW_STATE_CHANGED:  ttsOutput= 1 selected  ttsAddToHistory",
        # what the speech controller spoke: an action mode's title (TB-3, emulator-5556),
        # whose feedback-provider line had an empty ttsOutput
        '         1790849879.599 23067 23393 V talkback: SpeechControllerImpl: Speaking fragment '
        'text="1 selected", utteranceId=talkback_156, TtsSpan=null, locale=null, '
        'event=type:EVENT_TYPE_ACCESSIBILITY subtype:TYPE_WINDOW_STATE_CHANGED displayId:0 '
        'time:55259546',
        '         1790849879.551 23067 23393 V talkback: SpeechControllerImpl: Speaking fragment '
        'text="SE", utteranceId=talkback_155, TtsSpan=null, locale=null, '
        'event=type:EVENT_TYPE_ACCESSIBILITY subtype:TYPE_VIEW_ACCESSIBILITY_FOCUSED displayId:0',
    ]
    got = [S.parse_line(x) for x in lines]
    assert got == [("reason", "restore"), ("tts", "Webview. In horizontal pager"),
                   ("hint", "Press select to activate"), None,
                   ("announce", "TYPE_WINDOW_STATE_CHANGED\tThunderbird Debug"),
                   ("announce", "TYPE_VIEW_CLICKED\t"), None,
                   ("announce", "TYPE_WINDOW_STATE_CHANGED\t1 selected"),
                   ("announce", "TYPE_WINDOW_STATE_CHANGED\t1 selected"), None]
    # a long utterance logcat split over two lines (G24's shape) is joined
    log = S.SpeechLog(SERIAL)
    log.feed("         1.0 1 1 V talkback: TalkBackFeedbackProvider:  TYPE_VIEW_ACCESSIBILITY_FOCUSED:"
             "  ttsOutput= Message details demo. Alice – multiple addresses in the", now=1.0)
    log.feed(" To: header. 5 of 6. In list    queueMode=0", now=1.0)
    log.feed("         2.0 1 1 V talkback: unrelated", now=2.0)
    assert log.since(0, "tts") == [(1.0, "tts", "Message details demo. Alice – multiple addresses "
                                                "in the To: header. 5 of 6. In list")]
    # logcat prints each line of a multi-line message with its own header (wtvdjj3 step 10)
    log = S.SpeechLog(SERIAL)
    log.feed("         1.0 1 1 V talkback: TalkBackFeedbackProvider:  TYPE_VIEW_ACCESSIBILITY_FOCUSED:"
             "  ttsOutput= Release notes", now=1.0)
    log.feed("         1.0 1 1 V talkback: Fixed a crash. 3 of 9    queueMode=0  ttsAddToHistory",
             now=1.0)
    log.feed("         1.1 1 1 V talkback: Compositor: eventInterpretation= x", now=1.1)
    assert log.since(0, "tts") == [(1.0, "tts", "Release notes Fixed a crash. 3 of 9")]


# --------------------------------------------------------------------------- selectors (G4)
def test_an_ambiguous_target_is_refused_before_talkback_is_touched(probe):
    starts = probe.talkback.starts
    with pytest.raises(Exception) as err:
        scenario("focus_after", target="Item", action="activate")
    assert getattr(err.value, "code", None) == "ambiguous"
    assert len(err.value.tried) == 5 and err.value.tried[0].startswith('view:1020 "Item 0')
    assert probe.talkback.presses == [] and probe.talkback.starts == starts


def test_a_label_on_no_stop_fails_at_once_with_the_stop_count(probe):
    t = time.monotonic()
    with pytest.raises(Exception) as err:
        scenario("focus_after", target="Theme", action="activate")
    assert getattr(err.value, "code", None) == "start_not_found"
    assert str(err.value) == "label 'Theme' not found among 9 stops on MainActivity"
    assert probe.talkback.presses == [] and time.monotonic() - t < 3


def test_a_walk_seek_has_its_own_budget(probe):
    """The seek no longer spends max_steps: a walk of 2 steps still starts at Item 4."""
    session = iw.attach(SERIAL, PKG)
    try:
        res = tbwalk.run_walk(session, start="Item 4", max_steps=2, until="steps", **FAST)
    finally:
        session.disconnect()
    assert res["lines"][0].startswith("0. view:1024")
    assert any(n.startswith("start matched=label (exact) view:1024") for n in res["notes"])


def test_a_key_of_a_node_that_is_no_stop_is_still_focused_as_given(probe):
    scene = nav_scene()
    content = scene.roots[0].children[0]
    content.children.append(ViewSpec(1050, "ImageView", "android.widget", (300, 30, 40, 40),
                                     a11y={"class_name": "android.widget.ImageView"}))
    probe.scene_factory = lambda: scene
    session = iw.attach(SERIAL, PKG)
    try:
        res = tbwalk.run_walk(session, start="view:1050", max_steps=1, until="steps", **FAST)
    finally:
        session.disconnect()
    assert res["lines"][0].startswith("0. view:1050")
    assert "start view:1050 is no stop the model reads; focused as given" in res["notes"]


def test_a_stale_key_is_re_resolved_or_named(probe):
    session = iw.attach(SERIAL, PKG)
    try:
        with pytest.raises(tbwalk.WalkError) as err:
            tbwalk.run_walk(session, start="view:4242", max_steps=2, until="steps", **FAST)
    finally:
        session.disconnect()
    assert err.value.code == "start_not_found" and "not on the screen any more" in str(err.value)


# --------------------------------------------------------------------------- sequences (G14)
def test_activate_then_walk_lists_what_talkback_read(probe):
    def unbookmark(tb, target):
        _remove(tb.device, 1022)
        tb.set_focus(TABS[1])

    probe.talkback.on_click = unbookmark
    res = scenario("focus_after", target="Item 2", action="activate; walk:3; expect:Item 0")
    assert res["verdict"] == "moved_to_nav"
    # from Saved, the last stop: the edge, then the wrap to the title, then Item 0
    assert [w.get("ref") or "edge" for w in res["walk"]] == ["edge", "view:1003", "view:1020"]
    assert res["expect"] == {"label": "Item 0", "reached": True, "at": "focus"}
    probe.talkback.on_click = None
    res = scenario("focus_after", target="Item 3", action="activate; walk:2; expect:Title")
    assert res["expect"] == {"label": "Title", "reached": False, "focus": "view:1025",
                             "within": "2 presses"}


def test_long_press_and_custom_actions_go_through_the_agent(probe):
    agent_actions = []

    def on_action(host, virtual, action_id):
        agent_actions.append((host, action_id))

    scene = nav_scene()
    _views(scene)[1022].a11y["actions"] = [(0x10, None), (0x40, None), (0x20, None),
                                           (0x7F0A0012, "Delete")]
    probe.scene_factory = lambda: scene
    session = iw.attach(SERIAL, PKG)
    probe.agent(PKG).on_a11y_action = on_action
    try:
        res = S.run_scenario(session, "focus_after", target="Item 2", action="long_press",
                             wait_ms=400, **FAST)
        assert res["action"].startswith("long_press view:1022")
        res = S.run_scenario(session, "focus_after", target="Item 2", action="custom:delete",
                             wait_ms=400, **FAST)
        assert res["action"] == "custom 'delete' on view:1022 (as TalkBack's actions menu)"
        with pytest.raises(tbwalk.WalkError) as err:
            S.run_scenario(session, "focus_after", target="Item 3", action="custom:Delete",
                           wait_ms=400, **FAST)
        assert "offers no custom action 'Delete'" in str(err.value)
        assert "view:1022 has it" in str(err.value)
    finally:
        session.disconnect()
    assert [a for a in agent_actions if a[1] != 0x40] == [(1022, 0x20), (1022, 0x7F0A0012)]
    assert probe.talkback.clicks == []


def test_a_failed_precondition_presses_nothing(probe):
    with pytest.raises(tbwalk.WalkError) as err:
        scenario("focus_after", target="Item 2", action="pre:activity=.SettingsActivity; activate")
    assert err.value.code == "not_found" and "nothing was pressed" in str(err.value)
    assert probe.talkback.presses == []


# --------------------------------------------------------------------------- restore settle (G13)
def test_restore_waits_for_the_opened_screen_before_back(probe):
    order = []

    def open_detail(tb, target):
        tb.device.activity_stack.append(f"{PKG}/.DetailActivity")
        _remove(tb.device, 1023)  # the list is gone behind the detail
        tb.set_focus(TB_TITLE)
        order.append(("opened", time.monotonic()))

    def on_input(args):
        if args == ["keyevent", "KEYCODE_BACK"]:
            order.append(("back", time.monotonic()))
            probe.talkback.set_focus(TB_TITLE)

    probe.talkback.on_click = open_detail
    probe.on_input = on_input
    res = scenario("restore", target="Item 3", wait_ms=1500)
    assert res["opened"]["settled"] is True
    assert order[1][1] - order[0][1] >= S.WINDOW_QUIET_S
    assert "notes" not in res or not any("not settled" in n for n in res["notes"])


def test_restore_says_when_the_opened_screen_never_settled(probe):
    res = scenario("restore", target="Item 3", wait_ms=400)  # the click changes nothing
    assert res["opened"]["settled"] is False
    assert any("opened screen not settled" in n for n in res["notes"])


# --------------------------------------------------------------------------- G23
def test_the_touch_injector_starts_at_first(probe):
    probe.talkback.focus = tb_item(3)
    session = iw.attach(SERIAL, PKG)
    try:
        res = tbwalk.run_walk(session, start="first", injector="touch", until="edge", **FAST)
    finally:
        session.disconnect()
    assert res["lines"][0].startswith("0. view:1003"), res["lines"]


def test_the_touch_injector_starts_at_first_by_swiping_back_without_a11y_act(probe, monkeypatch):
    monkeypatch.setattr(tbwalk, "make_reader", lambda session: tbwalk.DumpFocusReader(session))
    probe.talkback.focus = tb_item(2)
    session = iw.attach(SERIAL, PKG)
    try:
        res = tbwalk.run_walk(session, start="first", injector="touch", until="edge", **FAST)
    finally:
        session.disconnect()
    assert res["lines"][0].startswith("0. view:1003"), res["lines"]
    assert "previous-swipes" in " ".join(res.get("notes") or [])


# --------------------------------------------------------------------------- the result
def test_a_scenario_result_with_everything_fits_its_budget():
    rec = {"id": "tabcdef", "kind": "focus_after", "captures": ["c1", "c2"],
           "target": {"ref": "n12", "speak": "Unbookmark. Check box. checked"},
           "matched": 'label (exact) compose:8:1706 "Unbookmark. Check box"',
           "action": "activate (TalkBack click, Meta+Space)", "new_screen": False,
           "before": {"windows": 1}, "after": {"windows": 1, "panes": ["Alert"]},
           "speak_before": "Unbookmark. Check box. checked. " * 3,
           "timeline": [{"t": 0, "focus": "n12"}, {"t": 82, "focus": None},
                        {"t": 164, "focus": "n19"}, {"t": 170, "said": "Saved. Tab", "why": "restore"},
                        {"t": 300, "announced": "Bookmark removed", "event": "window_state_changed"}],
           "focus": {"ref": "n19", "speak": "Saved, Tab"}, "speak_after": "Saved. Tab",
           "announced": ["Bookmark removed"], "verdict": "moved_to_nav",
           "why": 'same screen; focus thrown to the navigation bar (the target was removed): n19 "Saved. Tab"',
           "walk": [{"i": i, "ref": f"n{20 + i}", "speak": f"Stop {i}. Button"} for i in range(1, 9)],
           "expect": {"label": "Undo", "reached": True, "at": "walk press 6"},
           "finding": {"code": "tb.focus_reset", "sev": "warn", "msg": "after activate, " * 8,
                       "fix": S.FIXES["tb.focus_reset:mutation"]},
           "notes": ["a note " * 10], "restore": "restored"}
    out = walks.scenario_result(rec)
    assert utf8_len(dumps(out)) <= walks.SCENARIO_MAX_BYTES
    for key in ("verdict", "why", "focus", "expect", "finding", "lines"):
        assert key in out, key
    assert "speak_after" not in out  # what the focus line already quotes
    assert out["expect"] == '"Undo" reached (walk press 6)'
    assert any(t.startswith('170 said "Saved. Tab" (restore)') for t in out["timeline"])
    full = walks.scenario_result(rec, max_bytes=4000)
    assert full["lines"][0] == '1 n21 "Stop 1. Button"'


# --------------------------------------------------------------------------- CLI = MCP
def test_cli_and_mcp_give_the_same_ambiguity_and_grammar_errors(probe, run_cli, mcp):
    m = mcp("tb_scenario", kind="focus_after", target="Item", action="activate", **FAST)
    r = run_cli("tb-scenario", "focus-after", "--serial", SERIAL, "--package", PKG,
                "--target", "Item", "--action", "activate", "--json", "-")
    c = json.loads(r.err)
    assert r.rc == 1 and m["error"]["code"] == c["error"]["code"] == "ambiguous"
    assert m["error"]["candidates"] == c["error"]["candidates"] and len(c["error"]["candidates"]) == 5
    assert utf8_len(dumps(m)) <= 1000
    m = mcp("tb_scenario", kind="focus_after", target="Item 1", action="swipe", **FAST)
    r = run_cli("tb-scenario", "focus-after", "--serial", SERIAL, "--package", PKG,
                "--target", "Item 1", "--action", "swipe", "--json", "-")
    c = json.loads(r.err)
    assert m["error"]["code"] == c["error"]["code"] == "bad_args"
    assert m["error"]["message"] == c["error"]["message"] and S.GRAMMAR in c["error"]["message"]
    assert probe.talkback.presses == []


def test_refs_inside_an_action_sequence_resolve_in_the_capture(tb_env, monkeypatch):
    """On the capture surface: ``long_press:<ref>``, ``expect:<ref>`` and ``tap:<ref>`` inside
    a sequence name capture refs, and one no capture holds fails before TalkBack is touched."""
    import mcp_server
    from inspector_widget import surface
    monkeypatch.setenv(surface.ENV_TOOLSET, "capture,talkback")
    monkeypatch.setattr(S, "WINDOW_QUIET_S", 0.1)
    tb_env.scene_factory = fakeagent.talkback_scene
    tb_env.talkback.order = list(ORDER)
    original = dict(tb_env.secure)

    def call(tool, **args):
        text, is_error = mcp_server._call_tool_text(tool, args)
        return json.loads(text), is_error

    doc, err = call("capture", serial=SERIAL, package=PKG)
    assert not err, doc
    res, err = call("tb_scenario", kind="focus_after", serial=SERIAL, package=PKG, target="n7",
                    action="activate; walk:1; expect:n8", wait_ms=400, **FAST)
    assert not err, res
    assert res["target"].startswith('n7 "Item 1') and res["lines"][0].startswith('1 n8 "Item 2')
    assert res["expect"] == '"n8" reached (focus)'
    doc, err = call("tb_scenario", kind="focus_after", serial=SERIAL, package=PKG, target="n7",
                    action="activate; expect:n999", **FAST)
    assert err and doc["error"]["code"] == "ref_not_in_capture"
    assert tb_env.talkback.presses.count("click") == 1  # the second one pressed nothing
    assert fakeagent.settings_changes(tb_env, original) == {}
