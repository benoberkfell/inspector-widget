"""TalkBack walks and scenarios, offline: the real Session / Client / walk code
against the fake agent (which marks the node the fake TalkBack focused) and the
fake adb (settings, uinput keyboard, logcat). Every walk also checks that the
device's settings came back and that no unsafe key reached it.
"""

from __future__ import annotations

import json
import os
import time

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


def test_legacy_agent_ids_still_walk(probe):
    def legacy(dev):
        dev.agent(PKG).legacy_a11y_ids = True

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


def test_focus_taken_between_presses_is_recorded_as_stolen(probe, monkeypatch):
    # With a 30ms poll and a 20ms settle, a step's wait reads exactly twice after
    # its press, so the third read after press 3 is the next step's pre-check.
    monkeypatch.setattr(tbwalk, "POLL_MS", 30)
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
                probe.talkback.set_focus(tb_item(5))  # the app pulled focus (input focus sync)
                state["dumps"] = None
        return 0, agent.dispatch(req)

    probe.behaviour = behaviour
    probe.talkback.on_press = arm
    res = walk(probe)
    rec = saved(res)
    stolen = [s for s in rec["steps"] if s["via"] == "stolen"]
    assert stolen and stolen[0]["key"] == "view:1025" and stolen[0]["i"] == 4
    assert any("via=stolen" in ln for ln in res["lines"])


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

    probe.on_broadcast = notify_all
    res = scenario(probe, "survive", target="Item 2", mutate="probe:notify_all")
    assert res["verdict"] == "drifted" and res["finding"]["code"] == "tb.focus_drift"
    assert any("TB_PROBE" in b and "notify_all" in b for b in probe.broadcasts)


def test_survive_kept(probe):
    res = scenario(probe, "survive", target="Item 1", mutate="broadcast:-a com.example.NOOP")
    assert res["verdict"] == "kept" and "finding" not in res


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


def test_utterance_logcat_turns_verbose_logging_on_and_back_off(probe):
    res = walk(probe, utterance="logcat")
    assert res["utterance"].startswith("logcat ")
    assert probe.talkback.log_level == "ERROR" and probe.uiautomator_while_on == 0
    assert res["lines"][2] == '2. view:1020 Button "Item 0. Button"'
