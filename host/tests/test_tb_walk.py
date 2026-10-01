"""TalkBack walks and scenarios, offline: the real Session / Client / walk code
against the fake agent (which marks the node the fake TalkBack focused) and the
fake adb (settings, uinput keyboard, logcat). Every walk also checks that the
device's settings came back and that no unsafe key reached it.
"""

from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import pytest

import fakeagent
from fakeagent import DEFAULT_PACKAGE as PKG
from fakeagent import DEFAULT_SERIAL as SERIAL
from fakeagent import TB_TITLE, ComposeNodeSpec, Scene, ViewSpec, tb_item

import inspector_widget as iw
import mcp_server
from inspector_widget.talkback import device as tbdevice
from inspector_widget.talkback import scenarios as tbscenarios
from inspector_widget.talkback import walk as tbwalk

FAST = {"step_timeout_ms": 250, "settle_ms": 20}
ORDER = [TB_TITLE] + [tb_item(i) for i in range(6)]


@pytest.fixture
def probe(tb_env):
    """The title + 6 items scene, TalkBack's order over it, and a pre-walk baseline."""
    tb_env.scene_factory = fakeagent.talkback_scene
    tb = tb_env.talkback
    tb.order = list(ORDER)
    tb.labels = {TB_TITLE: "Title", **{tb_item(i): f"Item {i}. Button" for i in range(6)}}
    tb_env.original = dict(tb_env.secure)
    yield tb_env
    # Whatever a test did, the device must end as it started, with no unsafe key.
    assert fakeagent.settings_changes(tb_env, tb_env.original) == {}
    assert fakeagent.key_safety_violations(tb_env) == []


def walk(dev, before=None, **kw):
    session = iw.attach(SERIAL, PKG)
    try:
        if before is not None:
            before(dev)
        return tbwalk.run_walk(session, **{**FAST, **kw})
    finally:
        session.disconnect()


def saved(res):
    with open(res["saved"]) as f:
        return json.load(f)


def keys(rec):
    return [s["key"] for s in rec["steps"]]


# --------------------------------------------------------------------------- #
# Basic laps
# --------------------------------------------------------------------------- #
def test_walk_one_full_lap_from_no_focus(probe):
    res = walk(probe)
    assert res["ended"] == "wrap", res
    assert res["lines"][0] == "0. (no accessibility focus)"
    assert res["lines"][1] == '1. view:1003 TextView "Title"'
    assert res["lines"][2] == '2. view:1020 Button "Item 0. Button"'
    assert res["lines"][8] == "8. — edge"
    assert res["lines"][9] == '9. view:1003 TextView "Title" via=wrap'
    assert res["vs_model"]["agree"] == 6 and res["vs_model"]["differ"] == 0
    assert res["findings"] == []
    assert res["restore"] == "restored"
    assert res["talkback"] == "17.0.0 uinput/enhanced"
    assert len(json.dumps(res)) <= 5000
    rec = saved(res)
    assert rec["model"] == "a11y.compute_traversal_order" or rec["model"].startswith(("a11y", "talkback"))
    assert [p["key"] for p in rec["predicted"]] == ["view:1003"] + [f"view:{1020 + i}" for i in range(6)]
    assert probe.talkback.presses == ["next"] * 9
    assert not os.path.exists(os.path.join(str(probe.store), "talkback", f"{SERIAL}.json"))


def test_walk_until_edge_stops_at_the_first_edge(probe):
    res = walk(probe, until="edge")
    assert res["ended"] == "edge" and res["lines"][-1] == "8. — edge"


def test_walk_from_first_proves_next_before_the_ctrl_shortcut(probe):
    probe.talkback.focus = tb_item(3)
    res = walk(probe, start="first")
    assert res["ended"] == "wrap"
    assert probe.talkback.presses[:2] == ["next", "first"]
    assert res["lines"][0] == '0. view:1003 TextView "Title"'
    assert probe.key_log[0] == ("LEFTMETA", "RIGHT", True)


def test_walk_backwards_returns_to_the_start_before_walking(probe):
    probe.talkback.focus = tb_item(2)
    res = walk(probe, direction="prev")
    assert probe.talkback.presses[:2] == ["next", "prev"]  # prove, then back to Item 2
    rec = saved(res)
    assert keys(rec)[:4] == ["view:1022", "view:1021", "view:1020", "view:1003"]
    assert res["ended"] == "wrap" and keys(rec)[-1] == "view:1022"
    assert res["vs_model"]["differ"] == 0


def test_walk_seeks_a_start_by_label(probe):
    res = walk(probe, start="Item 4", until="edge")
    assert res["lines"][0] == '0. view:1024 Button "Item 4. Button"'
    assert res["lines"][1:] == ['1. view:1025 Button "Item 5. Button"', "2. — edge"]


def test_walk_start_not_found_is_an_error_and_still_restores(probe):
    with pytest.raises(tbwalk.WalkError) as err:
        walk(probe, start="No such thing", max_steps=10)
    assert err.value.code == "start_not_found"


def test_sixty_steps_fit_the_budget(tb_env):
    tb_env.scene_factory = lambda: fakeagent.talkback_scene(n_items=70)
    tb_env.talkback.order = [TB_TITLE] + [tb_item(i) for i in range(70)]
    res = walk(tb_env, max_steps=60, until="steps")
    assert res["ended"] == "max_steps" and res["steps"] == 60
    assert len(json.dumps(res, ensure_ascii=False).encode()) <= 5000
    assert len(res["lines"]) <= 60 and any("omitted" in ln for ln in res["lines"])
    assert len(saved(res)["steps"]) == 61  # the saved walk keeps every step


def _legacy_a11y_ids(root):
    """Rewrite an encoded a11y tree to the ids agents from before the A1 fix send
    (AccessibilityInspector.kt walk()/virtualIdOf()): every node carries the ROOT's
    host_view_id, and a child's virtual_id is the low 32 bits of the packed child id
    (the accessibility view id of the View behind it), so real View children are
    flagged virtual as well."""
    root_host = root.host_view_id

    def fix(node):
        for child in node.children:
            child.virtual_id = child.host_view_id % 100 + 10  # any small per-View int
            child.is_virtual = True
            child.host_view_id = root_host
            fix(child)

    fix(root)


def test_legacy_agent_ids_still_walk(probe):
    def legacy(dev):
        agent = dev.agent(PKG)
        current = agent.behaviour

        def behaviour(req):
            if req.WhichOneof("command") in ("a11y_focus", "a11y_act"):
                # An A1-era agent predates A11yFocus/A11yAct: the field is unknown to it.
                return 0, fakeagent.error_response(req.id, "No command set in request")
            delay, action = current(req)
            if req.WhichOneof("command") == "dump_a11y" and action is not None \
                    and not isinstance(action, (str, bytes)):
                for w in action.dump_a11y.windows:
                    _legacy_a11y_ids(w.root)
            return delay, action

        agent.behaviour = behaviour

    res = walk(probe, before=legacy)
    assert res["ended"] == "wrap" and res["findings"] == []
    assert res["lines"][1].startswith("1. s1 TextView")
    rec = saved(res)
    assert rec["legacy_ids"] is True and rec["refs"]["s1"].startswith("legacy:")


# --------------------------------------------------------------------------- #
# Endings: loop, stuck, left_app, stolen
# --------------------------------------------------------------------------- #
def test_loop_is_detected_with_its_cycle(probe):
    succ = {None: TB_TITLE, TB_TITLE: tb_item(0), tb_item(0): tb_item(1),
            tb_item(1): tb_item(2), tb_item(2): tb_item(0)}

    def cycle(tb, action):
        if action == "next":
            tb.set_focus(succ[tb.focus])
            return True
        return False

    probe.talkback.on_press = cycle
    res = walk(probe)
    assert res["ended"] == "loop"
    assert res["cycle"] == ["view:1020", "view:1021", "view:1022"]
    loop = [f for f in res["findings"] if f["code"] == "tb.loop"]
    assert loop and loop[0]["sev"] == "error"


def test_two_presses_that_move_nothing_are_stuck(probe):
    def stuck(tb, action):
        return tb.focus == tb_item(1)  # swallow every press once at Item 1

    probe.talkback.on_press = stuck
    res = walk(probe)
    assert res["ended"] == "stuck"
    assert res["lines"][-2:] == ["4. — edge", "5. — edge"]
    assert [f["code"] for f in res["findings"]][:1] == ["tb.edge_stuck"]


def test_leaving_the_app_ends_the_walk(probe):
    def leave(tb, action):
        if len(tb.presses) == 3:
            tb.set_focus(None)
            tb.device.activity_stack.append("com.android.launcher3/.Launcher")
            return True
        return False

    probe.talkback.on_press = leave
    res = walk(probe)
    assert res["ended"] == "left_app"
    assert res["lines"][-1] == "3. — left the app (top: com.android.launcher3/.Launcher)"
    probe.activity_stack.pop()


def test_focus_taken_between_presses_is_recorded_as_stolen(probe):
    """A11yFocus: the pre-press read (no wait) right after press 3's long-poll sees the
    app's focus move as a FOCUSED event newer than the step's seq."""
    state = {"armed": False}

    def arm(tb, action):
        if len(tb.presses) == 3:
            state["armed"] = True
        return False

    def behaviour(req):
        agent = probe.agent(PKG)
        cmd = req.WhichOneof("command")
        if cmd == "a11y_focus" and state["armed"] and req.a11y_focus.wait_ms == 0:
            state["armed"] = False
            probe.talkback.set_focus(tb_item(5))  # the app pulled focus (input focus sync)
        return 0, agent.dispatch(req)

    probe.behaviour = behaviour
    probe.talkback.on_press = arm
    res = walk(probe)
    rec = saved(res)
    assert rec["reader"] == "a11y_focus"
    stolen = [s for s in rec["steps"] if s["via"] == "stolen"]
    assert stolen and stolen[0]["key"] == "view:1025" and stolen[0]["i"] == 4
    assert any("via=stolen" in ln for ln in res["lines"])


def test_focus_taken_back_before_a_press_settles_is_stolen_too(probe):
    """TalkBack focuses the next item, then (inside the same wait) the app pulls
    focus back up the list: the press's move is the first, then a steal."""
    def press(tb, action):
        if action != "next" or len(tb.presses) != 3:
            return False
        tb.set_focus(tb.order[tb.order.index(tb.focus) + 1])
        tb.set_focus(tb_item(0))  # the app's timer takes focus back
        return True

    probe.talkback.on_press = press
    rec = saved(walk(probe, until="edge"))
    moves = [(s["via"], s["key"]) for s in rec["steps"][:6]]
    assert moves[3] == ("next", "view:1021") and moves[4] == ("stolen", "view:1020"), moves
    codes = [f["code"] for f in rec["findings"]]
    assert "tb.trap" in codes and "tb.revisit" not in codes


def test_focus_taken_back_just_before_a_press_is_stolen_then_the_press_moves_on(probe):
    def press(tb, action):
        if action != "next" or len(tb.presses) != 5:
            return False
        tb.set_focus(tb_item(0))  # the app's timer, a moment before the press lands
        tb.set_focus(tb.order[tb.order.index(tb.focus) + 1])
        return True

    probe.talkback.on_press = press
    rec = saved(walk(probe, until="edge"))
    moves = [(s["via"], s["key"]) for s in rec["steps"][:7]]
    assert moves[5:7] == [("stolen", "view:1020"), ("next", "view:1021")], moves
    assert "model.mismatch" not in [f["code"] for f in rec["findings"]]


def test_dump_reader_records_stolen_focus_too(probe, monkeypatch):
    # With a 30ms poll and a 20ms settle, a step's wait reads exactly twice after
    # its press, so the third read after press 3 is the next step's pre-check.
    monkeypatch.setattr(tbwalk, "POLL_MS", 30)
    monkeypatch.setattr(tbwalk, "make_reader", tbwalk.DumpFocusReader)
    state = {"dumps": None}

    def arm(tb, action):
        if len(tb.presses) == 3:
            state["dumps"] = 0
        return False

    def behaviour(req):
        agent = probe.agent(PKG)
        if req.WhichOneof("command") == "dump_a11y" and state["dumps"] is not None:
            state["dumps"] += 1
            if state["dumps"] == 3:
                probe.talkback.set_focus(tb_item(5))
                state["dumps"] = None
        return 0, agent.dispatch(req)

    probe.behaviour = behaviour
    probe.talkback.on_press = arm
    res = walk(probe)
    rec = saved(res)
    assert rec["reader"] == "dump_poll"
    stolen = [s for s in rec["steps"] if s["via"] == "stolen"]
    assert stolen and stolen[0]["key"] == "view:1025" and stolen[0]["i"] == 4


# --------------------------------------------------------------------------- #
# Screen changes: auto-scroll and re-modelling
# --------------------------------------------------------------------------- #
def _views(scene):
    out = {}
    for r in scene.roots:
        for v in r.walk():
            out[v.id] = v
    return out


def test_autoscroll_is_detected_and_new_nodes_are_modelled(tb_env):
    tb_env.scene_factory = lambda: fakeagent.talkback_scene(n_items=6, visible=4, scroll_forward=True)
    tb = tb_env.talkback
    tb.order = [TB_TITLE] + [tb_item(i) for i in range(7)]
    tb_env.original = dict(tb_env.secure)

    def scroll(t, action):
        if action == "next" and t.focus == tb_item(3):
            views = _views(tb_env.live_scene(PKG))
            column = views[1011]
            column.children.append(ViewSpec(1026, "Button", "android.widget", (16, 600, 328, 64),
                                             text="Item 6", a11y={"class_name": "android.widget.Button",
                                                                  "text": "Item 6", "clickable": True}))
            for v in column.children:
                x, y, w, h = v.bounds
                v.bounds = (x, y - 240, w, h)
                v.a11y.pop("visible_to_user", None)
            tap = tb_env.agent(PKG).a11y_tap  # what the platform sends while it scrolls
            tap.record(fakeagent.TYPE_VIEW_SCROLLED, 1001, 1010, -1, scroll_delta_y=240)
            tap.record(fakeagent.TYPE_WINDOW_CONTENT_CHANGED, 1001, 1011, -1)
            t.set_focus(tb_item(4))
            return True
        return False

    tb.on_press = scroll
    res = walk(tb_env, until="edge")
    rec = saved(res)
    step = next(s for s in rec["steps"] if s["key"] == "view:1024")
    assert step["via"] == "autoscroll" and step["scrolled"] == "view:1010"
    assert rec["remodels"] >= 1 and "view:1026" in [p["key"] for p in rec["predicted"]]
    assert any("via=autoscroll" in ln for ln in res["lines"])
    assert fakeagent.settings_changes(tb_env, tb_env.original) == {}


def test_edge_inside_a_list_that_can_still_scroll_is_edge_stuck(tb_env):
    tb_env.scene_factory = lambda: fakeagent.talkback_scene(n_items=6, scroll_forward=True)
    tb_env.talkback.order = [TB_TITLE] + [tb_item(i) for i in range(6)]
    res = walk(tb_env, until="edge")
    f = [f for f in res["findings"] if f["code"] == "tb.edge_stuck"]
    assert f and "can still scroll forward" in f[0]["msg"]


# --------------------------------------------------------------------------- #
# Findings from a walk
# --------------------------------------------------------------------------- #
def test_skipped_and_out_of_order_and_model_mismatch(probe):
    probe.talkback.order = [TB_TITLE, tb_item(0), tb_item(3), tb_item(1), tb_item(4), tb_item(5)]
    res = walk(probe)
    codes = [f["code"] for f in res["findings"]]
    assert "tb.skipped" in codes and "tb.out_of_order" in codes and "model.mismatch" in codes
    skipped = next(f for f in res["findings"] if f["code"] == "tb.skipped")
    assert skipped["refs"] == ["view:1022"]
    ooo = next(f for f in res["findings"] if f["code"] == "tb.out_of_order")
    assert ooo["refs"] in (["view:1023"], ["view:1021"])
    assert res["vs_model"]["differ"] >= 1 and res["vs_model"]["unvisited"] == ["view:1022"]
    assert res["vs_model"]["first"].startswith("step 3: model view:1021")


def test_expect_order_mismatch_is_reported(probe):
    res = walk(probe, expect=["Title", "Item 1", "Item 0"])
    assert res["expect"]["ok"] is False
    assert [f["basis"] for f in res["findings"] if f["code"] == "tb.out_of_order"] == ["expect"]


def _scene_with(*children):
    content = ViewSpec(1002, "LinearLayout", "android.widget", (0, 0, 360, 640),
                       a11y={"class_name": "android.widget.LinearLayout"}, children=list(children))
    decor = ViewSpec(1001, "DecorView", "com.android.internal.policy", (0, 0, 360, 640),
                     a11y={"class_name": "android.widget.FrameLayout"}, children=[content])
    return Scene(roots=[decor])


def _button(vid, bounds, text=None, **a11y):
    spec = {"class_name": "android.widget.Button", "clickable": True, "focusable": True, **a11y}
    if text:
        spec["text"] = text
    return ViewSpec(vid, "Button", "android.widget", bounds, text=text, a11y=spec)


def test_unlabelled_sliver_is_a_ghost_stop(tb_env):
    title = ViewSpec(1003, "TextView", "android.widget", (16, 24, 328, 64), text="Title",
                     a11y={"class_name": "android.widget.TextView", "text": "Title"})
    tb_env.scene_factory = lambda: _scene_with(title, _button(1040, (0, 100, 360, 8)),
                                               _button(1041, (16, 120, 328, 64), "OK"))
    tb_env.talkback.order = [TB_TITLE, (1040, -1), (1041, -1)]
    res = walk(tb_env)
    ghost = [f for f in res["findings"] if f["code"] == "tb.ghost_stop"]
    assert ghost and ghost[0]["refs"] == ["view:1040"]
    assert "unlabelled" in ghost[0]["msg"] and "sliver" in ghost[0]["msg"]
    assert any(ln.startswith("2. view:1040") and "!ghost_stop" in ln for ln in res["lines"])


def test_text_that_no_stop_read_is_skipped_but_not_text_under_a_dialog(tb_env):
    title = ViewSpec(1003, "TextView", "android.widget", (16, 24, 328, 64), text="Title",
                     a11y={"class_name": "android.widget.TextView", "text": "Title"})
    note = ViewSpec(1004, "TextView", "android.widget", (16, 200, 328, 40), text="Unsaved changes",
                    a11y={"class_name": "android.widget.TextView", "text": "Unsaved changes"})
    tb_env.scene_factory = lambda: _scene_with(title, _button(1041, (16, 120, 328, 64), "OK"), note)
    tb_env.talkback.order = [TB_TITLE, (1041, -1)]
    res = walk(tb_env)
    skipped = [f for f in res["findings"] if f["code"] == "tb.skipped" and "on screen" in f["msg"]]
    assert skipped and "Unsaved changes" in skipped[0]["msg"]

    # Text in a window the lap never entered (the activity under a modal dialog) is not skipped.
    class Idx:
        order = [SimpleNamespace(text="Unsaved changes", cd="", flags={"visible_to_user"}, window=1,
                                 bounds=(16, 200, 328, 40), key="view:1004"),
                 SimpleNamespace(text="Discard", cd="", flags={"visible_to_user"}, window=9,
                                 bounds=(56, 410, 120, 64), key="view:2002")]

        def window_rect(self, win):
            return (0, 0, 360, 640)

        def obscured(self, win):
            return []

    lap = [{"window": 9, "speak": "Discard. Button", "label": "Discard"}]
    assert tbwalk.orphan_text(Idx(), lap) == []
    assert [o["text"] for o in tbwalk.orphan_text(Idx(), lap + [{"window": 1, "speak": "Title"}])] \
        == ["Unsaved changes"]


def test_container_and_child_with_the_same_words_are_a_double_stop(tb_env):
    switch = ViewSpec(1051, "Switch", "android.widget", (250, 140, 90, 60), text="Wi-Fi",
                      a11y={"class_name": "android.widget.Switch", "text": "Wi-Fi", "clickable": True,
                            "checkable": True})
    row = ViewSpec(1050, "LinearLayout", "android.widget", (0, 120, 360, 100),
                   a11y={"class_name": "android.widget.LinearLayout", "content_description": "Wi-Fi",
                         "clickable": True, "focusable": True}, children=[switch])
    tb_env.scene_factory = lambda: _scene_with(row)
    tb_env.talkback.order = [(1050, -1), (1051, -1)]
    res = walk(tb_env)
    dbl = [f for f in res["findings"] if f["code"] == "tb.double_stop"]
    assert dbl and dbl[0]["refs"] == ["view:1050", "view:1051"]


def test_leaving_a_same_window_overlay_is_an_escape(tb_env):
    background = ViewSpec(1065, "LinearLayout", "android.widget", (0, 100, 360, 300),
                          a11y={"class_name": "android.widget.LinearLayout"},
                          children=[_button(1060, (20, 110, 320, 60), "Back 0"),
                                    _button(1061, (20, 250, 320, 60), "Back 1")])
    overlay = ViewSpec(1070, "FrameLayout", "android.widget", (0, 80, 360, 400),
                       a11y={"class_name": "android.widget.FrameLayout"},
                       children=[_button(1071, (20, 100, 320, 60), "Sheet OK"),
                                 _button(1072, (20, 180, 320, 60), "Sheet Cancel")])
    tb_env.scene_factory = lambda: _scene_with(background, overlay)
    tb_env.talkback.order = [(1071, -1), (1072, -1), (1061, -1)]
    res = walk(tb_env, until="edge")
    esc = [f for f in res["findings"] if f["code"] == "tb.escape"]
    assert esc and esc[0]["refs"] == ["view:1072", "view:1061"]
    assert "view:1070" in esc[0]["msg"]


def test_a_compose_host_with_a_scrim_over_views_is_an_overlay(tb_env):
    # Views report their drawing order; the Compose host reports 0 (Compose builds
    # its node itself) and sorts first by position, though it is drawn last.
    background = [_button(1060 + i, (20, 110 + 140 * i, 320, 60), f"Account {i}", drawing_order=2 + i)
                  for i in range(2)]
    scrim = ViewSpec(1071, "View", "android.view", (0, 0, 360, 640),
                     a11y={"class_name": "android.view.View", "clickable": True},
                     children=[_button(1072, (40, 300, 280, 60), "Stay")])
    host = ViewSpec(1070, "AndroidComposeView", "androidx.compose.ui.platform", (0, 0, 360, 640),
                    a11y={"class_name": "android.view.View"}, children=[scrim])
    tb_env.scene_factory = lambda: _scene_with(host, *background)
    tb_env.talkback.order = [(1072, -1), (1060, -1)]
    res = walk(tb_env, until="edge")
    esc = [f for f in res["findings"] if f["code"] == "tb.escape"]
    assert esc and esc[0]["refs"] == ["view:1072", "view:1060"]
    # Without the scrim the host is plain content drawn under the Views: no overlay.
    scrim.a11y["clickable"] = False
    res = walk(tb_env, until="edge")
    assert "tb.escape" not in [f["code"] for f in res["findings"]]


def test_compose_nodes_are_keyed_by_semantics_id(tb_env):
    sem = ComposeNodeSpec(1, fakeagent.pb.ComposeNode.SEMANTICS, (0, 0, 360, 640), children=[
        ComposeNodeSpec(5, fakeagent.pb.ComposeNode.SEMANTICS, (16, 40, 328, 64),
                        a11y={"class_name": "android.widget.Button", "text": "Pay",
                              "clickable": True, "focusable": True}),
        ComposeNodeSpec(6, fakeagent.pb.ComposeNode.SEMANTICS, (16, 140, 328, 64),
                        a11y={"class_name": "android.widget.Button", "text": "Cancel",
                              "clickable": True, "focusable": True}),
    ])
    acv = ViewSpec(1006, "AndroidComposeView", "androidx.compose.ui.platform", (0, 0, 360, 640),
                   a11y={"class_name": "android.view.View",
                         "provider_class": "androidx.compose.ui.platform.AndroidComposeView"},
                   semantics=sem)
    tb_env.scene_factory = lambda: _scene_with(acv)
    tb_env.talkback.order = [(1006, 5), (1006, 6)]
    res = walk(tb_env)
    assert res["lines"][1] == '1. compose:1006:5 Button "Pay. Button"'
    assert res["ended"] == "wrap" and res["vs_model"]["differ"] == 0


# --------------------------------------------------------------------------- #
# Utterances and edges from TalkBack's verbose logcat
# --------------------------------------------------------------------------- #
def test_logcat_utterances_and_fast_edges(probe):
    probe.talkback.verbose_log = True
    t0 = time.monotonic()
    res = walk(probe, step_timeout_ms=3000)
    took = time.monotonic() - t0
    assert res["ended"] == "wrap"
    assert took < 3.0, "the edge waited out the timeout instead of using 'Reach edge'"
    assert res["utterance"].startswith("logcat ")
    assert res["lines"][2] == '2. view:1020 Button "Item 0. Button"'


def test_model_utterance_when_asked(probe):
    probe.talkback.verbose_log = True
    res = walk(probe, utterance="model")
    assert res["utterance"] == "model" and res["lines"][2].endswith('"Item 0. Button"')


# --------------------------------------------------------------------------- #
# Safety around a walk
# --------------------------------------------------------------------------- #
def test_leave_on_keeps_talkback_and_restore_undoes_it(probe):
    res = walk(probe, leave_on=True)
    assert res["restore"].startswith("left on")
    assert probe.talkback.running and tbdevice.status(SERIAL)["restore_pending"]
    assert tbdevice.action(SERIAL, "restore")["restored"] is True


def test_a_walk_holds_the_device_lock(probe):
    seen = {}

    def try_on(tb, action):
        try:
            tbdevice.action(SERIAL, "off")
        except tbdevice.TalkBackError as exc:
            seen["code"] = exc.code
        return False

    probe.talkback.on_press = try_on
    walk(probe, max_steps=1, until="steps")
    assert seen["code"] == "busy"


def test_walk_restores_even_when_it_fails(probe, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("diff exploded")

    monkeypatch.setattr(tbwalk, "_attribute_tts", boom)
    probe.talkback.verbose_log = True
    with pytest.raises(RuntimeError):
        walk(probe)
    assert not probe.talkback.running


def test_mutation_skipped_restore_is_caught(tb_env, monkeypatch):
    """Guard the guard: with restore disabled the settings check must fail."""
    tb_env.scene_factory = fakeagent.talkback_scene
    tb_env.talkback.order = list(ORDER)
    original = dict(tb_env.secure)
    monkeypatch.setattr(tbdevice, "restore", lambda serial, wait_s=None: {"restored": True})
    walk(tb_env)
    assert fakeagent.settings_changes(tb_env, original) != {}


def test_mutation_unguarded_prev_walk_is_caught(tb_env, monkeypatch):
    """With the key guard off, a backwards walk on a TalkBack that does not
    consume Meta sends Meta+Left first: the device-side check must see it."""
    from inspector_widget.talkback import inject as tbinject
    tb_env.scene_factory = fakeagent.talkback_scene
    tb_env.talkback.order = list(ORDER)
    tb_env.talkback.keymap = "classic"
    monkeypatch.setattr(tbinject.KeyGuard, "check", lambda self, mods, key, action=None: None)
    monkeypatch.setattr(tbwalk, "_seek_start", lambda drv, cur, start, direction, n: cur)
    walk(tb_env, direction="prev", max_steps=2, until="steps")
    assert fakeagent.key_safety_violations(tb_env)


def test_classic_keymap_is_found_without_sending_meta_left(tb_env):
    tb_env.scene_factory = fakeagent.talkback_scene
    tb_env.talkback.order = list(ORDER)
    tb_env.talkback.keymap = "classic"
    res = walk(tb_env, direction="prev")
    assert res["talkback"].endswith("uinput/classic")
    assert "classic" in " ".join(res.get("notes") or [])
    assert fakeagent.key_safety_violations(tb_env) == []
    assert tb_env.system_backs == 0


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #
def scenario(dev, kind, **kw):
    session = iw.attach(SERIAL, PKG)
    try:
        return tbscenarios.run_scenario(session, kind, **{**FAST, "wait_ms": 400, **kw})
    finally:
        session.disconnect()


def test_focus_after_activate_lands_on_an_unlabelled_close_button(probe, monkeypatch):
    monkeypatch.setattr(tbscenarios, "WINDOW_QUIET_S", 0.1)

    def open_dialog(tb, target):
        scene = tb.device.live_scene(PKG)
        close = _button(2010, (40, 200, 48, 48))
        ok = _button(2011, (120, 300, 120, 48), "OK")
        scene.roots.append(ViewSpec(2001, "DecorView", "com.android.internal.policy",
                                    (20, 180, 320, 200), a11y={"class_name": "android.widget.FrameLayout"},
                                    children=[close, ok]))
        tb.set_focus((2010, -1))

    probe.talkback.on_click = open_dialog
    res = scenario(probe, "focus_after", target="Item 2", action="activate")
    assert res["target"]["ref"] == "view:1022"
    assert res["verdict"] == "on_close_or_unlabeled" and res["new_screen"] is True
    assert res["finding"]["code"] == "tb.initial_focus"
    assert probe.talkback.clicks == [tb_item(2)]
    assert res["restore"] == "restored"


def test_restore_after_back_that_goes_to_the_top_fails(probe, monkeypatch):
    monkeypatch.setattr(tbscenarios, "WINDOW_QUIET_S", 0.1)

    def open_detail(tb, target):
        tb.device.activity_stack.append(f"{PKG}/.DetailActivity")
        tb.set_focus(None)

    def on_input(args):
        if args == ["keyevent", "KEYCODE_BACK"]:
            probe.talkback.set_focus(TB_TITLE)  # no per-window record: back to the top

    probe.talkback.on_click = open_detail
    probe.on_input = on_input
    res = scenario(probe, "restore", target="Item 3")
    assert res["verdict"] == "top" and res["finding"]["code"] == "tb.restore_failed"
    assert res["opened"]["top"] == f"{PKG}/.DetailActivity"


def test_survive_detects_a_rebound_row_as_drift(probe, monkeypatch):
    def notify_all(args):
        views = _views(probe.live_scene(PKG))
        views[1022].a11y["text"] = "Item 9"  # the View now shows another item
        probe.agent(PKG).a11y_tap.record(fakeagent.TYPE_WINDOW_CONTENT_CHANGED, 1001, 1022, -1,
                                         content_change_types=1)

    probe.on_broadcast = notify_all
    res = scenario(probe, "survive", target="Item 2", mutate="probe:notify_all")
    assert res["verdict"] == "drifted" and res["finding"]["code"] == "tb.focus_drift"
    assert any("TB_PROBE" in b and "notify_all" in b for b in probe.broadcasts)


def test_survive_sees_a_rebound_row_that_sent_no_event(probe):
    def rebind(args):  # Compose rebinds a keyless lazy item without a content-change event
        _views(probe.live_scene(PKG))[1022].a11y["text"] = "Item 9"

    probe.on_broadcast = rebind
    res = scenario(probe, "survive", target="Item 2", mutate="probe:insert_top")
    assert res["verdict"] == "drifted" and res["finding"]["code"] == "tb.focus_drift"


def test_survive_kept(probe):
    res = scenario(probe, "survive", target="Item 1", mutate="broadcast:-a com.example.NOOP")
    assert res["verdict"] == "kept" and "finding" not in res


def test_survive_an_updated_row_is_kept_not_drift(probe):
    def change_item(args):
        _views(probe.live_scene(PKG))[1022].a11y["text"] = "Item 2 (played)"
        probe.agent(PKG).a11y_tap.record(fakeagent.TYPE_WINDOW_CONTENT_CHANGED, 1001, 1022, -1,
                                         content_change_types=1)

    probe.on_broadcast = change_item
    res = scenario(probe, "survive", target="Item 2", mutate="probe:change_item")
    assert res["verdict"] == "kept" and "finding" not in res
    assert tbscenarios._same_item("Track 8", "Track 8 (played)")
    assert not tbscenarios._same_item("Message 1", "Message 10")


# --------------------------------------------------------------------------- #
# CLI and MCP (parity)
# --------------------------------------------------------------------------- #
def test_mcp_talkback_on_is_restored_at_server_exit(tb_env, mcp):
    original = dict(tb_env.secure)
    st = mcp("talkback", action="status")
    assert st["talkback"]["installed"] == "17.0.0" and "error" not in st
    on = mcp("talkback", action="on")
    assert on["changed"] is True and on["restore_pending"] is True
    mcp_server._cleanup_at_exit()
    assert fakeagent.settings_changes(tb_env, original) == {}


def test_mcp_tb_walk_and_scenario(probe, mcp):
    res = mcp("tb_walk", **FAST)
    assert res["ended"] == "wrap" and res["restore"] == "restored"
    sc = mcp("tb_scenario", kind="survive", target="Item 1", mutate="broadcast:-a x", wait_ms=400, **FAST)
    assert sc["verdict"] == "kept"


def test_mcp_tb_errors_carry_a_code(probe, mcp):
    probe.talkback.installed = False
    res = mcp("tb_walk", **FAST)
    assert res["code"] == "talkback_unavailable" and "hint" in res


def test_mcp_tools_say_they_are_device_wide():
    for name in ("talkback", "tb_walk", "tb_scenario"):
        entry = mcp_server.TOOLS[name]
        assert entry["description"].startswith("DEVICE-WIDE")
        assert entry["annotations"]["destructiveHint"] is True
        assert entry["annotations"]["readOnlyHint"] is False
    listed = mcp_server._fallback_handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in listed["result"]["tools"]}
    assert tools["tb_walk"]["annotations"]["destructiveHint"] is True


def test_cli_talkback_round_trip(tb_env, run_cli):
    original = dict(tb_env.secure)
    r = run_cli("talkback", "status", "--serial", SERIAL)
    assert r.rc == 0 and r.json()["restore_pending"] is False
    r = run_cli("talkback", "on", "--serial", SERIAL)
    assert r.rc == 0 and r.json()["changed"] is True and "stays on" in r.err
    r = run_cli("talkback", "restore", "--serial", SERIAL)
    assert r.rc == 0 and r.json()["restored"] is True
    assert fakeagent.settings_changes(tb_env, original) == {}


def test_cli_tb_walk_and_scenario(probe, run_cli):
    r = run_cli("tb-walk", "--serial", SERIAL, "--step-timeout-ms", 250, "--settle-ms", 20,
                "--json", "-")
    assert r.rc == 0, r
    assert r.json()["ended"] == "wrap"
    r = run_cli("tb-walk", "--serial", SERIAL, "--step-timeout-ms", 250, "--settle-ms", 20,
                "--until", "edge")
    assert r.rc == 0 and '  1. view:1003 TextView "Title"' in r.out
    r = run_cli("tb-scenario", "survive", "--serial", SERIAL, "--target", "Item 1",
                "--mutate", "broadcast:-a x", "--wait-ms", 400, "--step-timeout-ms", 250,
                "--settle-ms", 20, "--json", "-")
    assert r.rc == 0 and r.json()["verdict"] == "kept"


def test_cli_reports_talkback_errors(tb_env, run_cli):
    tb_env.talkback.installed = False
    r = run_cli("talkback", "on", "--serial", SERIAL)
    assert r.rc == 1 and "not installed" in r.err and "hint:" in r.err


def test_cli_and_mcp_expose_the_same_options():
    import cli
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if a.dest == "command" or hasattr(a, "choices") and
               isinstance(a.choices, dict) and "tb-walk" in a.choices)
    cli_opts = {}
    for name in ("talkback", "tb-walk", "tb-scenario"):
        sp = sub.choices[name]
        cli_opts[name] = {a.dest for a in sp._actions}
    mapping = {"direction": "prev"}
    for tool, sub_name in (("tb_walk", "tb-walk"), ("tb_scenario", "tb-scenario"),
                           ("talkback", "talkback")):
        props = set(mcp_server.TOOLS[tool]["schema"]["properties"])
        missing = {p for p in props if mapping.get(p, p) not in cli_opts[sub_name]}
        assert not missing, f"{tool} options without a CLI flag: {missing}"


# --------------------------------------------------------------------------- #
# The MCP stdio transport, in a child process (restored at server exit)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("transport", ["sdk", "fallback"])
def test_stdio_talkback_tools_and_restore_at_exit(tmp_path, monkeypatch, transport):
    if transport == "sdk":
        pytest.importorskip("mcp")
    import test_e2e_fake_agent as e2e
    monkeypatch.setenv("INSPECTOR_WIDGET_CAPTURE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("FAKEAGENT_TB_ORDER", "[[1003,-1],[1004,-1]]")
    results, _wire, exits = e2e._stdio_session(tmp_path, transport == "fallback", [
        ("talkback", {"action": "status"}),
        ("tb_walk", {"step_timeout_ms": 250, "settle_ms": 20}),
        ("talkback", {"action": "on"}),
    ])
    status, walked, on = (e2e._payload(results[i]) for i in (2, 3, 4))
    assert status["talkback"]["enabled"] is False and results[2]["isError"] is False
    assert walked["ended"] == "wrap" and walked["restore"] == "restored", walked
    assert walked["lines"][1] == '1. view:1003 TextView "Hello world"'
    assert on["changed"] is True
    [exit_record] = exits
    # `talkback on` left it on; the server's exit hook restored the snapshot.
    assert exit_record["talkback_running"] is False and exit_record["secure"] == {}


# --------------------------------------------------------------------------- #
# Identity: the walk keys nodes exactly like a11y's node_key
# --------------------------------------------------------------------------- #
def test_walk_keys_are_the_a11y_node_keys():
    from inspector_widget import a11y
    agent = fakeagent.FakeAgent()  # the default scene: Views, a ComposeView, a popup window
    try:
        req = fakeagent.pb.Request(id=1)
        req.dump_a11y.SetInParent()
        resp = agent.dispatch(req).dump_a11y
    finally:
        agent.stop()
    idx = tbwalk.DumpIndex(resp)
    assert idx.legacy is False
    d = a11y.a11y_to_dict(resp)
    want = []
    stack = [w["root"] for w in reversed(d["windows"]) if w.get("root")]
    while stack:
        n = stack.pop()
        want.append(n["node_key"])
        stack.extend(reversed(n.get("children") or []))
    assert [n.key for n in idx.order] == want
    assert any(k.startswith("compose:") for k in want)


def test_remodel_matches_a_reminted_id_by_signature():
    m = tbwalk.Model()
    m.stops = [tbwalk.PStop("compose:7:141", "Row 3", "Row 3", (0, 100, 300, 80), 1, "View")]
    assert m.match("compose:7:759", "View|Row 3", (0, 101, 300, 80)).key == "compose:7:141"
    assert m.match("compose:7:760", "View|Row 3", (0, 900, 300, 80)) is None


def test_remodel_puts_what_a_scroll_revealed_after_what_it_scrolled_off(monkeypatch):
    def stop(key, label, y):
        return tbwalk.PStop(key, label, label, (0, y, 300, 80), 1, "View")
    m = tbwalk.Model()
    m.stops = [stop("h", "Heading", 0)] + [stop(f"r{i}", f"Row {i}", 100 * i) for i in (1, 2, 3)] \
        + [stop("f", "Footer", 900)]
    # Rows 1-2 scrolled off, 4-5 came in; the heading and footer stayed.
    new = [stop("h", "Heading", 0), stop("r3", "Row 3", 100), stop("r4", "Row 4", 200),
           stop("r5", "Row 5", 300), stop("f", "Footer", 900)]
    monkeypatch.setattr(tbwalk, "predict", lambda resp, legacy: (new, "", {"covered_windows": {}}))
    m.remodel(None, False)
    assert [s.key for s in m.stops] == ["h", "r1", "r2", "r3", "r4", "r5", "f"]
    # Everything under the heading scrolled: the new rows follow the ones that left.
    new = [stop("h", "Heading", 0), stop("r6", "Row 6", 100), stop("r7", "Row 7", 200)]
    m.remodel(None, False)
    assert [s.key for s in m.stops] == ["h", "r1", "r2", "r3", "r4", "r5", "f", "r6", "r7"]


def test_remodel_keeps_a_rebound_recyclerview_row_as_a_stop_of_its_own(monkeypatch):
    def stop(key, label, y):
        return tbwalk.PStop(key, label, label, (0, y, 300, 80), 1, "TextView")
    m = tbwalk.Model()
    m.stops = [stop("view:2", "Heading", 0), stop("view:13", "Mail 2", 100), stop("view:14", "Mail 3", 200)]
    # A page scroll: view:13 now shows Mail 20 further down; view:14 was updated in place.
    new = [stop("view:2", "Heading", 0), stop("view:14", "Mail 3 (read)", 200), stop("view:13", "Mail 20", 500)]
    monkeypatch.setattr(tbwalk, "predict", lambda resp, legacy: (new, "", {"covered_windows": {}}))
    m.remodel(None, False)
    assert [s.key for s in m.stops] == ["view:2", "view:13", "view:14", "view:13#1"]
    assert m.match("view:13", "TextView|Mail 20", (0, 500, 300, 80)).key == "view:13#1"
    assert m.match("view:13", "TextView|Mail 2", (0, 100, 300, 80)).key == "view:13"
    assert m.match("view:14", "TextView|Mail 3 (read)", (0, 200, 300, 80)).key == "view:14"
    # A card scrolled half off the top loses its (clipped) text: still the same node.
    m.stops.append(stop("compose:7:39", "News 5", 900))
    assert m.match("compose:7:39", "TextView|", (0, 20, 300, 30)).key == "compose:7:39"


def test_utterance_logcat_turns_verbose_logging_on_and_back_off(probe):
    res = walk(probe, utterance="logcat")
    assert res["utterance"].startswith("logcat ")
    assert probe.talkback.log_level == "ERROR" and probe.uiautomator_while_on == 0
    assert res["lines"][2] == '2. view:1020 Button "Item 0. Button"'


def test_walk_starts_at_talkbacks_initial_focus(probe, monkeypatch):
    monkeypatch.setattr(tbwalk, "INITIAL_FOCUS_S", 1.0)
    probe.talkback.initial_focus = tb_item(0)  # the title equals the window title: skipped
    res = walk(probe, until="edge")
    assert res["lines"][0] == '0. view:1020 Button "Item 0. Button"'
    assert res["lines"][1] == '1. view:1021 Button "Item 1. Button"'


def test_focus_cleared_by_a_scroll_is_waited_out_then_reported_lost(probe):
    def scroll_away(tb, action):
        if action == "next" and tb.focus == tb_item(1):
            tb.set_focus(None)  # the focused item scrolled off and was disposed
            return True
        return False

    probe.talkback.on_press = scroll_away
    res = walk(probe)
    assert res["ended"] == "lost"  # lost again at the same place after starting over
    assert "4. — focus lost (no node holds it)" in res["lines"]
    assert res["lines"][5] == '5. view:1003 TextView "Title"'  # the next press starts over
    lost = [f for f in res["findings"] if f["code"] == "tb.focus_lost"]
    assert lost and lost[0]["refs"] == ["view:1021"]


def _index(scene, focus):
    agent = fakeagent.FakeAgent(scene)
    agent.a11y_focus = focus
    try:
        req = fakeagent.pb.Request(id=1)
        req.dump_a11y.SetInParent()
        return tbwalk.DumpIndex(agent.dispatch(req).dump_a11y)
    finally:
        agent.stop()


def test_a_page_scroll_that_recreates_every_item_is_a_scroll():
    before = _index(fakeagent.talkback_scene(), tb_item(5))
    after_scene = fakeagent.talkback_scene()
    column = next(v for r in after_scene.roots for v in r.walk() if v.id == 1011)
    for v in column.children:
        v.id += 100  # the lazy list re-created its items (new ids) one page further
    after = _index(after_scene, (1120, -1))
    assert tbwalk.detect_scroll(before, after).key == "view:1010"
    assert tbwalk.detect_scroll(before, before) is None


def test_talkback_log_lines_are_parsed_strictly():
    edge = "V talkback: FocusProcessor-LogicalNav: Reach edge before searchTargetInNextOrPreviousWindow in:"
    assert tbwalk._RE_EDGE.search(edge)
    assert tbwalk._RE_SCROLL.search("D talkback: AutoScrollActor: ScrollAction=ACTION_SCROLL_FORWARD")
    assert tbwalk._RE_SCROLL.search("D talkback: AutoScrollActor: Perform ACTION_SHOW_ON_SCREEN:result=true")
    # a node dump or pipeline line that merely lists the action is not a scroll
    assert not tbwalk._RE_SCROLL.search(
        "V talkback: Pipeline: execute() target= AccessibilityNodeInfoCompat actions=[ACTION_SHOW_ON_SCREEN]")
    m = tbwalk._RE_TTS.search("I talkback: TalkBackFeedbackProvider:  TYPE_VIEW_ACCESSIBILITY_FOCUSED:  "
                              "ttsOutput= Like this photo. Button    queueMode=0")
    assert m and m.group(1) == "Like this photo. Button"


def test_focus_after_waits_out_the_gap_before_a_new_window_is_focused(probe, monkeypatch):
    """Like the D2 Compose Dialog live: focus is gone for ~800ms before TalkBack
    focuses the new window's heading. No focus is not an answer."""
    import threading
    monkeypatch.setattr(tbscenarios, "WINDOW_QUIET_S", 0.1)

    def open_dialog(tb, target):
        scene = tb.device.live_scene(PKG)
        heading = ViewSpec(2010, "TextView", "android.widget", (40, 200, 280, 48), text="Share item",
                           a11y={"class_name": "android.widget.TextView", "text": "Share item",
                                 "heading": True})
        scene.roots.append(ViewSpec(2001, "DecorView", "com.android.internal.policy",
                                    (20, 180, 320, 200), a11y={"class_name": "android.widget.FrameLayout"},
                                    children=[heading]))
        tb.set_focus(None)
        threading.Timer(0.4, tb.set_focus, args=[(2010, -1)]).start()

    probe.talkback.on_click = open_dialog
    res = scenario(probe, "focus_after", target="Item 2", action="activate", wait_ms=2000)
    assert res["verdict"] == "initial_ok" and res["focus"]["ref"] == "view:2010"
    assert [e.get("focus") for e in res["timeline"] if "focus" in e][-2:] == [None, "view:2010"]


# --------------------------------------------------------------------------- #
# A11yFocus (T2): the long-poll reader, A11yAct starts, WindowInfo in the model
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("reader", ["a11y_focus", "dump_poll"])
def test_both_readers_walk_the_same_lap(probe, monkeypatch, reader):
    if reader == "dump_poll":
        monkeypatch.setattr(tbwalk, "make_reader", tbwalk.DumpFocusReader)
    res = walk(probe)
    rec = saved(res)
    assert rec["reader"] == reader and res["reader"] == reader
    assert res["ended"] == "wrap" and res["findings"] == []
    assert keys(rec) == [None, "view:1003"] + [f"view:{1020 + i}" for i in range(6)] \
        + ["view:1025", "view:1003"]
    if reader == "a11y_focus":
        # a plain lap needs one dump (the start): moves come from the event tap
        assert res["ms"]["dumps"] == 1 and res["ms"]["reads"] >= 2 * res["steps"]


def test_start_by_node_key_uses_a11y_act_without_pressing(probe):
    res = walk(probe, start="view:1023", until="edge")
    rec = saved(res)
    assert rec["start_via"] == "a11y_act" and rec["seek_presses"] == 0
    assert res["lines"][0] == '0. view:1023 Button "Item 3. Button"'
    assert res["lines"][1] == '1. view:1024 Button "Item 4. Button"'
    assert probe.talkback.presses[0] == "next"  # no presses before the walk's own
    assert not [a for a in probe.talkback.presses if a != "next"]


def test_start_by_label_prefers_the_stop_and_walks_backwards(probe):
    res = walk(probe, start="Item 2", direction="prev", until="edge")
    rec = saved(res)
    assert rec["start_via"] == "a11y_act"
    assert probe.talkback.presses[:2] == ["next", "prev"]  # prove, then back onto the target
    assert keys(rec)[:3] == ["view:1022", "view:1021", "view:1020"]


def test_a11y_act_that_fails_falls_back_to_pressing(probe):
    with pytest.raises(tbwalk.WalkError) as err:
        walk(probe, start="view:9999", max_steps=8)
    assert err.value.code == "start_not_found"


def test_initial_focus_is_checked_against_the_models_rule(tb_env, monkeypatch):
    monkeypatch.setattr(tbwalk, "INITIAL_FOCUS_S", 1.0)

    def scene():
        s = fakeagent.talkback_scene()
        s.windows[1001] = {"title": "Title"}  # the title stop repeats the window title
        return s

    tb_env.scene_factory = scene
    tb = tb_env.talkback
    tb.order = list(ORDER)
    tb.initial_focus = tb_item(0)  # TalkBack skips the title, as the model predicts
    res = walk(tb_env, until="edge")
    rec = saved(res)
    assert rec["initial"]["model"] == "view:1020" and rec["initial"]["skipped"] == ["view:1003"]
    assert res["vs_model"]["initial"] == "agree"
    tb.initial_focus = TB_TITLE
    res = walk(tb_env, until="edge")
    assert res["vs_model"]["initial"] == "model view:1020, actual view:1003"


def test_a_stop_under_the_status_bar_is_a_ghost(tb_env):
    title = ViewSpec(1003, "TextView", "android.widget", (16, 30, 328, 64), text="Title",
                     a11y={"class_name": "android.widget.TextView", "text": "Title"})
    hidden = _button(1045, (16, 2, 100, 20), "Behind the bar")  # the fake status bar is 24px
    tb_env.scene_factory = lambda: _scene_with(title, hidden)
    tb_env.talkback.order = [TB_TITLE, (1045, -1)]
    res = walk(tb_env, until="edge")
    ghost = [f for f in res["findings"] if f["code"] == "tb.ghost_stop"]
    assert ghost and ghost[0]["refs"] == ["view:1045"] and "under a system bar" in ghost[0]["msg"]
    assert saved(res)["steps"][2]["under_system_bar"] == [0, 0, 360, 24]


def test_focus_after_reports_the_models_initial_focus(probe, monkeypatch):
    monkeypatch.setattr(tbscenarios, "WINDOW_QUIET_S", 0.1)

    def open_dialog(tb, target):
        scene = tb.device.live_scene(PKG)
        heading = ViewSpec(2010, "TextView", "android.widget", (40, 200, 280, 48), text="Share item",
                           a11y={"class_name": "android.widget.TextView", "text": "Share item",
                                 "heading": True})
        scene.roots.append(ViewSpec(2001, "DecorView", "com.android.internal.policy",
                                    (20, 180, 320, 200), a11y={"class_name": "android.widget.FrameLayout"},
                                    children=[heading]))
        scene.windows[2001] = {"title": "Share", "window_type": 2}
        tb.set_focus((2010, -1))

    probe.talkback.on_click = open_dialog
    res = scenario(probe, "focus_after", target="Item 2", action="activate")
    assert res["verdict"] == "initial_ok"
    assert res["model"]["initial"] == "view:2010" and res["model"]["title"] == "Share"


def test_scenario_target_is_focused_after_talkbacks_own_initial_focus(probe, monkeypatch):
    """TalkBack's initial focus comes ~550ms after it starts: a target focused
    before that would be overwritten, so the scenario waits for it first."""
    import threading
    monkeypatch.setattr(tbwalk, "INITIAL_FOCUS_S", 1.0)
    tb = probe.talkback
    real_sync = tb.sync

    def late_initial_focus():
        was = tb.was_running
        real_sync()
        if tb.running and not was:
            threading.Timer(0.3, tb.set_focus, args=[TB_TITLE]).start()

    monkeypatch.setattr(tb, "sync", late_initial_focus)
    res = scenario(probe, "survive", target="Item 3", mutate="broadcast:-a x")
    assert res["target"]["ref"] == "view:1023" and res["verdict"] == "kept"


def test_proving_the_keymap_on_the_last_stop_comes_back_to_it(probe, monkeypatch):
    """Live D1: the target is the last stop, so the proof's "next" hits the edge and
    wraps; focus must come back to the target before it is activated."""
    monkeypatch.setattr(tbscenarios, "WINDOW_QUIET_S", 0.1)
    res = scenario(probe, "focus_after", target="Item 5", action="activate")
    assert probe.talkback.clicks == [tb_item(5)]
    assert res["target"]["ref"] == "view:1025"


def test_recycled_views_showing_other_items_are_not_a_loop(probe):
    """RecyclerView rebinds the same Views to other items as it scrolls: the same
    pair of keys with other content is a new move, not a loop."""
    succ = {None: TB_TITLE, TB_TITLE: tb_item(0), tb_item(0): tb_item(1), tb_item(1): tb_item(0)}
    texts = iter([f"Item {i}" for i in range(2, 30)])

    def recycle(tb, action):
        if action != "next":
            return False
        target = succ[tb.focus]
        if tb.focus in (tb_item(0), tb_item(1)):  # the next row reuses the other View
            view = next(v for r in tb.device.live_scene(PKG).roots for v in r.walk() if v.id == target[0])
            view.a11y["text"] = next(texts, None) or "Item x"
            tb.device.agent(PKG).a11y_tap.record(fakeagent.TYPE_WINDOW_CONTENT_CHANGED, 1001, target[0], -1)
        tb.set_focus(target)
        return True

    probe.talkback.on_press = recycle
    res = walk(probe, max_steps=12)
    assert res["ended"] == "max_steps", res["lines"]
