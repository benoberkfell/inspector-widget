"""TalkBack device control + injectors (talkback/device.py, talkback/inject.py),
offline against the fake device's TalkBack (tests/fakeagent.py FakeTalkBack).

The safety rules are mutation-tested: each guard has a test that turns it off
and shows the device-side check then catches the damage, so a regression in
the guard cannot pass silently.
"""

from __future__ import annotations

import json
import os

import pytest

import fakeagent
from fakeagent import DEFAULT_SERIAL as SERIAL
from inspector_widget.talkback import device as tbdevice
from inspector_widget.talkback import inject as tbinject

TB = fakeagent.FakeTalkBack.COMPONENT
SVC = "enabled_accessibility_services"
OTHER = "com.example.switchaccess/com.example.switchaccess.SwitchAccessService"


def _state_file(dev):
    return os.path.join(str(dev.store), "talkback", f"{SERIAL}.json")


# --------------------------------------------------------------------------- #
# status / on / off / restore
# --------------------------------------------------------------------------- #
def test_status_reports_installed_disabled_and_nothing_pending(tb_env):
    st = tbdevice.action(SERIAL, "status")
    assert st["talkback"] == {"installed": "17.0.0", "enabled": False, "running": False}
    assert st["touch_exploration"] is False and st["services"] == []
    assert st["restore_pending"] is False
    assert st["injectors"]["uinput"].startswith("available")
    assert "warning" not in st
    assert tb_env.secure == {}  # status is read-only


def test_on_snapshots_before_the_first_change(tb_env, monkeypatch):
    seen = []
    real_put = tbdevice.put_setting

    def put(serial, key, value):
        seen.append((key, os.path.exists(_state_file(tb_env))))
        real_put(serial, key, value)

    monkeypatch.setattr(tbdevice, "put_setting", put)
    out = tbdevice.action(SERIAL, "on")
    assert out["changed"] is True and out["talkback"] == "on"
    assert seen and all(existed for _key, existed in seen), seen
    snap = json.load(open(_state_file(tb_env)))
    assert snap["settings"] == {SVC: None, "accessibility_enabled": None,
                                "touch_exploration_enabled": None,
                                "touch_exploration_granted_accessibility_services": None}
    assert "device-wide" in out["warning"]
    assert tbdevice.status(SERIAL)["restore_pending"] is True


def test_on_appends_talkback_and_keeps_other_services(tb_env):
    tb_env.secure.update({SVC: OTHER, "accessibility_enabled": "1"})
    tbdevice.action(SERIAL, "on")
    assert tb_env.secure[SVC] == f"{OTHER}:{TB}"
    assert tb_env.talkback.running and tb_env.secure["touch_exploration_enabled"] == "1"


def test_on_then_off_restores_the_exact_settings(tb_env):
    tb_env.secure.update({SVC: OTHER, "accessibility_enabled": "1",
                          "touch_exploration_enabled": "0"})
    original = dict(tb_env.secure)
    tbdevice.action(SERIAL, "on")
    out = tbdevice.action(SERIAL, "off")
    assert out["restored"] is True and out["talkback"] == "off"
    assert fakeagent.settings_changes(tb_env, original) == {}
    assert not os.path.exists(_state_file(tb_env))
    assert out["restore_pending"] is False


def test_restore_puts_back_unset_values_and_what_the_system_granted(tb_env):
    tb_env.talkback.grant_on_start = True
    tbdevice.action(SERIAL, "on")
    assert tb_env.secure["touch_exploration_granted_accessibility_services"] == TB
    out = tbdevice.action(SERIAL, "restore")
    assert out["restored"] is True
    # every value was unset before, so every one is deleted again (the granted list too)
    assert fakeagent.settings_changes(tb_env, {}) == {}


def test_a_pending_snapshot_survives_a_crash_and_is_not_overwritten(tb_env, monkeypatch):
    tbdevice.action(SERIAL, "on")
    monkeypatch.setattr(tbdevice, "_OWNED", set())  # the process that turned it on died
    assert tbdevice.restore_owned() == []           # a new process owns nothing
    st = tbdevice.status(SERIAL)
    assert st["restore_pending"] is True and st["saved"]["settings"][SVC] is None
    # Turning it on again keeps the ORIGINAL snapshot (TalkBack off), not the current state.
    tbdevice.action(SERIAL, "on")
    assert json.load(open(_state_file(tb_env)))["settings"][SVC] is None
    assert tbdevice.action(SERIAL, "restore")["restored"] is True
    assert not tb_env.talkback.running


def test_off_when_the_user_had_talkback_on_keeps_their_setup_for_restore(tb_env):
    tb_env.secure.update({SVC: f"{OTHER}:{TB}", "accessibility_enabled": "1"})
    tb_env.talkback.sync()
    original = dict(tb_env.secure)
    out = tbdevice.action(SERIAL, "off")
    assert out["changed"] is True and out["restore_pending"] is True
    assert tb_env.secure[SVC] == OTHER and not tb_env.talkback.running
    tbdevice.action(SERIAL, "restore")
    assert fakeagent.settings_changes(tb_env, original) == {}
    assert tb_env.talkback.running


def test_on_when_already_on_changes_nothing(tb_env):
    tb_env.secure.update({SVC: TB, "accessibility_enabled": "1"})
    tb_env.talkback.sync()
    tb_env.clear_logs()
    out = tbdevice.action(SERIAL, "on")
    assert out["changed"] is False and out["restore_pending"] is False
    assert not [c for c in tb_env.shell_log() if c.startswith("settings put")]


def test_restore_that_does_not_stick_keeps_the_snapshot(tb_env, monkeypatch):
    tbdevice.action(SERIAL, "on")
    real_put = tbdevice.put_setting
    monkeypatch.setattr(tbdevice, "put_setting", lambda s, k, v: None if k == "accessibility_enabled"
                        else real_put(s, k, v))
    with pytest.raises(tbdevice.TalkBackError) as err:
        tbdevice.action(SERIAL, "restore")
    assert err.value.code == "restore_failed" and "accessibility_enabled" in str(err.value)
    assert os.path.exists(_state_file(tb_env))


def test_tutorial_is_dismissed_with_back(tb_env):
    tb_env.talkback.training_on_start = True
    out = tbdevice.enable(SERIAL, fakeagent.DEFAULT_PACKAGE)
    # the POST_NOTIFICATIONS dialog (every start) sits on the tutorial (first start)
    assert out["dismissed"] == [fakeagent.FakeTalkBack.PERMISSION, fakeagent.FakeTalkBack.TRAINING]
    assert tb_env.top.startswith(fakeagent.DEFAULT_PACKAGE + "/")
    assert ["keyevent", "KEYCODE_BACK"] in tb_env.input_log


def test_app_not_in_front_after_enable_rolls_back(tb_env):
    tb_env.talkback.training_on_start = True
    tb_env.activity_stack.append("com.example.other/.Main")  # something else on top already
    with pytest.raises(tbdevice.TalkBackError) as err:
        tbdevice.enable(SERIAL, fakeagent.DEFAULT_PACKAGE)
    assert err.value.code == "app_left_foreground"
    # enable failed after changing settings: they are back as they were
    assert not tb_env.talkback.running and not os.path.exists(_state_file(tb_env))


def test_app_covered_during_enable_is_brought_back_to_front(tb_env, monkeypatch):
    def covered(serial, top_before=None):
        tb_env.activity_stack.append("com.example.popup/.Nag")
        return []

    monkeypatch.setattr(tbdevice, "dismiss_talkback_activities", covered)
    out = tbdevice.enable(SERIAL, fakeagent.DEFAULT_PACKAGE)
    assert out["refronted"] == f"{fakeagent.DEFAULT_PACKAGE}/.MainActivity"
    assert tb_env.top == f"{fakeagent.DEFAULT_PACKAGE}/.MainActivity"
    assert any(c.startswith("am start -n") and "0x00020000" in c for c in tb_env.shell_log())


def test_talkback_missing_is_reported_and_nothing_changes(tb_env):
    tb_env.talkback.installed = False
    with pytest.raises(tbdevice.TalkBackError) as err:
        tbdevice.action(SERIAL, "on")
    assert err.value.code == "talkback_unavailable"
    assert tb_env.secure == {} and not os.path.exists(_state_file(tb_env))


def test_enable_failure_rolls_the_settings_back(tb_env, monkeypatch):
    monkeypatch.setattr(tb_env.talkback, "sync", lambda: None)  # touch exploration never comes
    monkeypatch.setattr(tbdevice, "ENABLE_WAIT_S", 0.2)
    with pytest.raises(tbdevice.TalkBackError) as err:
        tbdevice.action(SERIAL, "on")
    assert err.value.code == "enable_failed"
    assert tb_env.secure.get(SVC) is None and tb_env.secure.get("accessibility_enabled") is None
    assert not os.path.exists(_state_file(tb_env))


def test_restore_owned_restores_what_this_process_turned_on(tb_env):
    tbdevice.action(SERIAL, "on")
    res = tbdevice.restore_owned()
    assert [r["restored"] for r in res] == [True]
    assert not tb_env.talkback.running


def test_device_lock_is_exclusive_in_and_across_processes(tb_env):
    with tbdevice.device_lock(SERIAL):
        with pytest.raises(tbdevice.TalkBackError) as err:
            tbdevice.action(SERIAL, "on")
        assert err.value.code == "busy"
    fcntl = pytest.importorskip("fcntl")
    path = os.path.join(str(tb_env.store), "talkback", f"{SERIAL}.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        # A second open file description behaves like another process's flock.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(tbdevice.TalkBackError) as err:
            with tbdevice.device_lock(SERIAL):
                pass
        assert "another inspector-widget process" in str(err.value)
    finally:
        os.close(fd)
    with tbdevice.device_lock(SERIAL):
        pass  # released again


def test_unknown_action_is_rejected():
    with pytest.raises(ValueError):
        tbdevice.action(SERIAL, "toggle")


# --------------------------------------------------------------------------- #
# injectors
# --------------------------------------------------------------------------- #
def _on(dev):
    tbdevice.enable(SERIAL)
    dev.talkback.order = [fakeagent.TB_TITLE, fakeagent.tb_item(0), fakeagent.tb_item(1)]


def test_uinput_keyboard_registers_and_next_reaches_talkback(tb_env):
    _on(tb_env)
    with tbinject.open_injector(SERIAL) as inj:
        assert inj.kind == "uinput" and tbinject.KEYBOARD_NAME in tb_env.input_devices
        inj.press("next")
    assert tb_env.key_log == [("LEFTMETA", "RIGHT", True)]
    assert tb_env.talkback.focus == fakeagent.TB_TITLE
    assert len(tb_env.uinputs) == 1  # one uinput process per injector
    assert tbinject.KEYBOARD_NAME not in tb_env.input_devices  # closed: device removed
    assert tbdevice.INJECTOR_STATUS[SERIAL]["uinput"].startswith("ok")
    assert fakeagent.key_safety_violations(tb_env) == []


def test_guard_refuses_meta_combos_until_next_is_proven(tb_env):
    _on(tb_env)
    with tbinject.open_injector(SERIAL) as inj:
        for action in ("prev", "first", "last", "click"):
            with pytest.raises(tbinject.UnsafeKeyError):
                inj.press(action)
        with pytest.raises(tbinject.UnsafeKeyError):
            inj.combo("META+SPACE")
        assert tb_env.key_log == []  # nothing was sent
        inj.press("next")
        inj.mark_proven()
        inj.press("prev")
        inj.combo("ENTER")  # no modifier: never guarded
    assert [k for _m, k, _c in tb_env.key_log] == ["RIGHT", "LEFT", "ENTER"]
    assert fakeagent.key_safety_violations(tb_env) == []


def test_guard_never_sends_a_modifier_alone(tb_env):
    _on(tb_env)
    with tbinject.open_injector(SERIAL) as inj:
        inj.mark_proven()
        for spec in ("META", "CTRL", "META+ALT"):
            with pytest.raises(tbinject.UnsafeKeyError):
                inj.combo(spec)
    assert tb_env.lone_meta == 0 and tb_env.key_log == []


def test_parse_combo():
    assert tbinject.parse_combo("meta+space") == (("KEY_LEFTMETA",), "KEY_SPACE")
    assert tbinject.parse_combo("KEY_LEFTALT + KEY_RIGHT") == (("KEY_LEFTALT",), "KEY_RIGHT")
    assert tbinject.parse_combo("ENTER") == ((), "KEY_ENTER")
    with pytest.raises(ValueError):
        tbinject.parse_combo("META+Q")  # KEY_Q would make the keyboard alphabetic


def test_touch_injector_swipes_reach_talkback(tb_env):
    _on(tb_env)
    with tbinject.open_injector(SERIAL, "touch") as inj:
        assert inj.kind == "touch" and inj.proven
        inj.press("next")
        inj.press("next")
        inj.press("prev")
        inj.press("click")
    assert tb_env.talkback.presses == ["next", "next", "prev", "click"]
    assert tb_env.talkback.focus == fakeagent.TB_TITLE


def test_no_uinput_reports_every_injector_tried(tb_env):
    tb_env.uinput_available = False
    with pytest.raises(tbinject.InjectorError) as err:
        tbinject.open_injector(SERIAL)
    assert [t.split(":")[0] for t in err.value.tried] == ["uinput", "touch"]
    assert tbdevice.status(SERIAL)["injectors"]["uinput"].startswith("failed")


# --------------------------------------------------------------------------- #
# Mutation tests: the device-side checks catch a disabled guard
# --------------------------------------------------------------------------- #
def test_mutation_unguarded_meta_left_is_caught(tb_env, monkeypatch):
    """With the guard off and TalkBack NOT consuming Meta keys, an early Meta+Left
    is a system BACK: the safety check must see it."""
    tb_env.talkback.keymap = "classic"  # Meta combos go unconsumed
    _on(tb_env)
    monkeypatch.setattr(tbinject.KeyGuard, "check", lambda self, mods, key, action=None: None)
    with tbinject.open_injector(SERIAL) as inj:
        inj.press("prev")
    assert tb_env.system_backs == 1
    assert fakeagent.key_safety_violations(tb_env), "the safety check missed a system BACK"


def test_mutation_lone_meta_is_caught(tb_env, monkeypatch):
    _on(tb_env)
    monkeypatch.setattr(tbinject.KeyGuard, "check", lambda self, mods, key, action=None: None)
    with tbinject.open_injector(SERIAL) as inj:
        inj.mark_proven()
        inj._send_combo((), "KEY_LEFTMETA", None)
    assert fakeagent.key_safety_violations(tb_env) == ["1 lone Meta tap(s)"]


# --------------------------------------------------------------------------- #
# uinput protocol details seen on the device
# --------------------------------------------------------------------------- #
def test_a_press_is_one_zero_gap_inject_and_syncs_on_the_device_id(tb_env):
    _on(tb_env)
    with tbinject.open_injector(SERIAL) as inj:
        inj.press("next")
    [u] = tb_env.uinputs
    injects = [c for c in u.commands if c["command"] == "inject"]
    assert injects == [{"id": 1, "command": "inject", "events": [
        "EV_KEY", "KEY_LEFTMETA", 1, "EV_SYN", "SYN_REPORT", 0,
        "EV_KEY", "KEY_RIGHT", 1, "EV_SYN", "SYN_REPORT", 0,
        "EV_KEY", "KEY_RIGHT", 0, "EV_SYN", "SYN_REPORT", 0,
        "EV_KEY", "KEY_LEFTMETA", 0, "EV_SYN", "SYN_REPORT", 0]}]
    assert not [c for c in u.commands if c["command"] == "delay"]
    assert all(c["id"] == 1 for c in u.commands if c["command"] == "sync")
    assert ["popen", "shell", "-T", "uinput", "-"] in tb_env.adb_log


# --------------------------------------------------------------------------- #
# TalkBack's log level, through its settings screen (TalkBack off only)
# --------------------------------------------------------------------------- #
def test_on_with_verbose_log_sets_the_level_first_and_restore_puts_it_back(tb_env, monkeypatch):
    seen = {}
    real_tap = tbdevice._tap

    def tap(serial, node):
        seen.setdefault("snap_at_first_tap", json.load(open(_state_file(tb_env))))
        seen.setdefault("running_at_first_tap", tb_env.talkback.running)
        real_tap(serial, node)

    monkeypatch.setattr(tbdevice, "_tap", tap)
    out = tbdevice.action(SERIAL, "on", verbose_log=True)
    assert out["log_level"] == {"before": "ERROR", "after": "VERBOSE"}
    # crash-safe: the old level is on disk before the first tap, with TalkBack still off
    assert seen["snap_at_first_tap"]["log_level"] == "ERROR"
    assert seen["running_at_first_tap"] is False
    assert tb_env.talkback.verbose_log is True        # read when the service bound
    assert tb_env.uiautomator_while_on == 0
    assert tb_env.top.startswith(fakeagent.DEFAULT_PACKAGE + "/")
    assert tbdevice.status(SERIAL)["saved"]["log_level"] == "ERROR"
    assert tbdevice.action(SERIAL, "restore")["restored"] is True
    assert tb_env.talkback.log_level == "ERROR" and tb_env.uiautomator_while_on == 0
    assert not os.path.exists(_state_file(tb_env))


def test_log_level_is_never_changed_while_talkback_runs(tb_env):
    tbdevice.action(SERIAL, "on")
    with pytest.raises(tbdevice.TalkBackError) as err:
        tbdevice.set_log_level(SERIAL, "VERBOSE")
    assert err.value.code == "talkback_on"
    out = tbdevice.enable(SERIAL, verbose_log=True)  # already on: level left alone
    assert out["changed"] is False and "unchanged" in out["log_level"]
    assert tb_env.uiautomator_while_on == 0 and tb_env.talkback.log_level == "ERROR"
    tbdevice.action(SERIAL, "restore")


def test_verbose_already_set_is_left_as_it_was(tb_env):
    tb_env.talkback.log_level = "VERBOSE"
    out = tbdevice.action(SERIAL, "on", verbose_log=True)
    assert out["log_level"] == {"before": "VERBOSE", "after": "VERBOSE"}
    tbdevice.action(SERIAL, "restore")
    assert tb_env.talkback.log_level == "VERBOSE"
