"""TalkBack first (G1): ``relaunch`` turns TalkBack on, then restarts the app from its
launcher, as a TalkBack user opens it; every walk and scenario says when TalkBack
started relative to the app's process (``talkback_started``); findings a walk cannot
vouch for are marked unverified.

Offline: the fake device force-stops and starts apps (``am force-stop``, ``cmd package
resolve-activity``, ``am start -W``), keeps process start times in ``/proc/<pid>/stat``
on an uptime clock (``/proc/uptime``), and the real Session / walk / capture code
re-attaches to the new process. Real-app evidence for the start order is in
tests/data/realapps/ (``thunderbird_list_compose_tb_first`` / ``_tb_later``).
"""

from __future__ import annotations

import gzip
import json
import os
import time

import pytest

import fakeagent
import mcp_server
from fakeagent import DEFAULT_PACKAGE as PKG
from fakeagent import DEFAULT_SERIAL as SERIAL
from fakeagent import TB_TITLE, tb_item

import inspector_widget as iw
from inspector_widget import ops, surface
from inspector_widget.capture import walks as W
from inspector_widget.output import dumps, utf8_len
from inspector_widget.talkback import device as tbdevice
from inspector_widget.talkback import walk as tbwalk

FAST = {"step_timeout_ms": 250, "settle_ms": 20}
DATA = os.path.join(os.path.dirname(__file__), "data", "realapps")


@pytest.fixture(autouse=True)
def _capture_listing(monkeypatch):
    monkeypatch.setenv(surface.ENV_TOOLSET, "capture,talkback")


@pytest.fixture
def tb(tb_env):
    tb_env.scene_factory = fakeagent.talkback_scene
    tb_env.talkback.order = [TB_TITLE] + [tb_item(i) for i in range(6)]
    tb_env.talkback.labels = {TB_TITLE: "Title",
                              **{tb_item(i): f"Item {i}. Button" for i in range(6)}}
    tb_env.original = dict(tb_env.secure)
    yield tb_env
    assert fakeagent.settings_changes(tb_env, tb_env.original) == {}
    assert fakeagent.key_safety_violations(tb_env) == []


def call(tool: str, **args):
    text, is_error = mcp_server._call_tool_text(tool, args)
    return json.loads(text), is_error


def ok(tool: str, **args) -> dict:
    doc, is_error = call(tool, **args)
    assert not is_error, doc
    return doc


def record(env, wid: str) -> dict:
    with open(os.path.join(str(env.store), "walks", f"{wid}.json")) as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# device.relaunch and the start order
# --------------------------------------------------------------------------- #
def test_relaunch_force_stops_and_starts_the_launcher_intent(tb_env):
    app = tb_env.apps[PKG]
    old_pid = app.pid
    out = tbdevice.relaunch(SERIAL, PKG)
    assert tb_env.force_stops == [PKG]
    comp = f"{PKG}/{PKG}.MainActivity"
    assert tb_env.starts == [comp] and out["activity"] == comp and out["launcher"] == comp
    assert out["pid"] == app.pid != old_pid
    assert abs(out["started_at"] - tb_env.uptime()) < 2.0
    shell = " | ".join(tb_env.shell_log())
    assert "cmd package resolve-activity --brief -a android.intent.action.MAIN -c " \
           "android.intent.category.LAUNCHER " + PKG in shell
    assert "am start -W -a android.intent.action.MAIN -c android.intent.category.LAUNCHER " \
           f"-p {PKG}" in shell


def test_an_alias_launcher_starts_by_its_intent(tb_env):
    """Thunderbird (emulator-5558): the launcher entry is an activity-alias;
    resolve-activity names it, but am start -n with its name fails ("does not exist").
    The launcher intent starts it."""
    app = tb_env.apps[PKG]
    app.launcher_alias, app.launcher = True, "net.example.app.common.MainActivity"
    alias = f"{PKG}/net.example.app.common.MainActivity"
    assert tbdevice.launcher_activity(SERIAL, PKG) == alias
    _rc, out, _err = tb_env.shell(f"am start -W -n {alias}")
    assert "does not exist" in out and tbdevice._start_error(out).startswith("Error: Activity")
    res = tbdevice.relaunch(SERIAL, PKG)
    assert res["launcher"] == alias and tbdevice.top_package(SERIAL) == PKG


def test_the_resolved_component_is_the_fallback(tb_env, monkeypatch):
    real = tb_env.shell

    def no_intent(cmd):
        if cmd.startswith("am start -W -a"):
            tb_env.adb_log.append(["shell", cmd])
            return 0, "Error: Activity not started, unable to resolve Intent\n", ""
        return real(cmd)

    monkeypatch.setattr(tb_env, "shell", no_intent)
    tbdevice.relaunch(SERIAL, PKG)
    assert tb_env.starts == [f"{PKG}/{PKG}.MainActivity"]
    assert any(c.startswith("am start -W -n ") for c in tb_env.shell_log())


def test_an_app_that_cannot_start_fails_at_once(tb_env, monkeypatch):
    monkeypatch.setattr(tbdevice, "LAUNCH_WAIT_S", 30.0)
    tb_env.apps[PKG].launcher_alias = True
    monkeypatch.setattr(tbdevice, "launcher_activity", lambda s, p: f"{PKG}/.Alias")
    real = tb_env.shell
    monkeypatch.setattr(tb_env, "shell", lambda cmd: (0, "Error type 3\nError: Activity "
                                                         "class {x} does not exist.\n", "")
                        if cmd.startswith("am start") else real(cmd))
    t0 = time.monotonic()
    with pytest.raises(tbdevice.TalkBackError) as err:
        tbdevice.relaunch(SERIAL, PKG)
    assert time.monotonic() - t0 < 5 and "does not exist" in str(err.value)


def test_an_app_that_never_comes_to_the_front_is_an_error(tb_env, monkeypatch):
    monkeypatch.setattr(tbdevice, "LAUNCH_WAIT_S", 0.3)
    monkeypatch.setattr(tb_env, "_launch", lambda comp, by_intent=False: (0, "Status: ok\n", ""))
    with pytest.raises(tbdevice.TalkBackError) as err:
        tbdevice.relaunch(SERIAL, PKG)
    assert err.value.code == "app_left_foreground" and "did not come to the front" in str(err.value)


def test_proc_stat_and_uptime_give_the_start_order(tb_env):
    app = tb_env.apps[PKG]
    start = tbdevice.process_start(SERIAL, app.pid)
    assert start is not None and abs(start - app.started) < 0.02
    up = tbdevice.uptime(SERIAL)
    assert up is not None and up > start
    order = tbdevice.talkback_started(SERIAL, PKG, on_uptime=up)
    assert order["talkback_started"] == "after_app" and order["app_start_s"] == start
    assert tbdevice.talkback_started(SERIAL, PKG, on_uptime=start - 5)["talkback_started"] \
        == "before_app"
    assert tbdevice.talkback_started(SERIAL, PKG, on_uptime=None)["talkback_started"] \
        == "before_walk"
    # a snapshot from before a reboot (uptime ahead of the device's) proves nothing
    assert tbdevice.talkback_started(SERIAL, PKG, on_uptime=up + 1e6)["talkback_started"] \
        == "before_walk"


def test_the_command_name_may_hold_spaces_and_parens(tb_env, monkeypatch):
    monkeypatch.setattr(tb_env, "_proc_stat", lambda pid: f"{pid} (a b) c) S " + " ".join(
        ["0"] * 18) + " 12345 9 9\n")
    assert tbdevice.process_start(SERIAL, 77) == 123.45


def test_enable_records_when_talkback_came_on(tb_env):
    out = tbdevice.enable(SERIAL, PKG)
    try:
        assert out["on_uptime"] is not None
        assert tbdevice.load_snapshot(SERIAL)["on_uptime"] == out["on_uptime"]
        again = tbdevice.enable(SERIAL, PKG)  # already on: the snapshot's time
        assert again["changed"] is False and again["on_uptime"] == out["on_uptime"]
    finally:
        tbdevice.restore(SERIAL)


# --------------------------------------------------------------------------- #
# Walks and scenarios
# --------------------------------------------------------------------------- #
def test_a_walk_without_relaunch_is_after_app(tb):
    res = ok("tb_walk", serial=SERIAL, **FAST)
    assert res["talkback_started"] == "after_app"
    rec = record(tb, res["walk"])
    assert rec["talkback_started"] == "after_app" and rec["injector_proven"] is True
    assert rec["app_start_s"] < rec["talkback_on_s"]
    assert tb.force_stops == []


def test_one_relaunch_call_walks_what_a_talkback_user_gets(tb, run_cli):
    """TB-1's hunt took 5 calls (talkback on --verbose-log, force-stop, launcher start,
    tb-walk --leave-on, talkback restore): one tb_walk(relaunch=true) does it."""
    old_pid = tb.apps[PKG].pid
    res = ok("tb_walk", serial=SERIAL, package=PKG, relaunch=True, start="first", **FAST)
    assert res["talkback_started"] == "before_app", res
    assert res["restore"] == "restored" and res["ended"] == "wrap"
    assert res["utterance"] == "logcat 8/8"  # TalkBack's words, set VERBOSE and back
    assert tb.talkback.log_level == "ERROR"
    assert tb.force_stops == [PKG] and tb.apps[PKG].pid != old_pid
    rec = record(tb, res["walk"])
    assert rec["relaunch"]["pid"] == tb.apps[PKG].pid
    assert rec["app_start_s"] >= rec["talkback_on_s"]
    # the walk captured the relaunched process (its agent, not the dead one's)
    assert tb.agent(PKG) is not None and res["capture"]
    assert 'tb_walk(relaunch=true' not in dumps(res.get("next") or [])
    # the CLI flag is the same call
    r = run_cli("tb-walk", "--serial", SERIAL, "--package", PKG, "--relaunch",
                "--step-timeout-ms", 250, "--settle-ms", 20, "--json")
    assert r.rc == 0, r.err
    cli = json.loads(r.out)
    assert cli["talkback_started"] == "before_app" and len(tb.force_stops) == 2


def test_relaunch_needs_no_running_app(tb):
    tb.force_stop(PKG)  # not running: a relaunch starts it
    res = ok("tb_walk", serial=SERIAL, package=PKG, relaunch=True, **FAST)
    assert res["talkback_started"] == "before_app" and tb.apps[PKG].pid is not None


def test_relaunch_with_talkback_already_on_is_still_before_app(tb):
    on = ok("talkback", action="on", serial=SERIAL, package=PKG)
    assert on["changed"] is True
    try:
        res = ok("tb_walk", serial=SERIAL, package=PKG, relaunch=True, leave_on=True, **FAST)
        assert res["talkback_started"] == "before_app"
        plain = ok("tb_walk", serial=SERIAL, package=PKG, leave_on=True, **FAST)
        assert plain["talkback_started"] == "before_app"  # the same process since
    finally:
        ok("talkback", action="restore", serial=SERIAL)


def test_talkback_on_by_someone_else_is_before_walk(tb):
    tb.talkback.permission_on_start = False
    tb.secure.update({"enabled_accessibility_services": fakeagent.FakeTalkBack.COMPONENT,
                      "accessibility_enabled": "1"})
    tb.talkback.sync()
    try:
        res = ok("tb_walk", serial=SERIAL, package=PKG, **FAST)
        assert res["talkback_started"] == "before_walk"
        assert res["restore"].startswith("unchanged")
    finally:
        tb.secure.pop("enabled_accessibility_services")
        tb.secure["accessibility_enabled"] = "0"
        tb.talkback.sync()
        tb.secure.pop("touch_exploration_enabled", None)
        tb.secure.pop("accessibility_enabled", None)


def test_a_scenario_relaunches_and_says_when_talkback_started(tb, run_cli):
    res = ok("tb_scenario", kind="focus_after", serial=SERIAL, package=PKG, relaunch=True,
             target="Item 2", action="activate", wait_ms=400, **FAST)
    assert res["talkback_started"] == "before_app" and tb.force_stops == [PKG]
    plain = ok("tb_scenario", kind="focus_after", serial=SERIAL, package=PKG,
               target="Item 2", action="activate", wait_ms=400, **FAST)
    # the first scenario put TalkBack back off: on again now, after the relaunched app
    assert plain["talkback_started"] == "after_app"
    r = run_cli("tb-scenario", "focus-after", "--serial", SERIAL, "--package", PKG,
                "--relaunch", "--target", "Item 1", "--wait-ms", 400, "--step-timeout-ms",
                250, "--settle-ms", 20, "--json")
    assert r.rc == 0, r.err
    assert json.loads(r.out)["talkback_started"] == "before_app" and len(tb.force_stops) == 2


def test_relaunch_and_show_do_not_mix(tb):
    doc, is_error = call("tb_walk", serial=SERIAL, show="w3f9ak1", relaunch=True)
    assert is_error and doc["error"]["code"] == "bad_args"


# --------------------------------------------------------------------------- #
# What a walk cannot vouch for
# --------------------------------------------------------------------------- #
def _rec(started="after_app", proven=True, findings=()):
    steps = [{"i": 0, "key": "view:1", "via": "start"}, {"i": 1, "key": "view:2", "via": "next"},
             {"i": 2, "key": "view:3", "via": "stolen"}]
    return {"talkback_started": started, "injector_proven": proven, "steps": steps,
            "findings": [dict(f) for f in findings]}


STUCK = {"code": "tb.edge_stuck", "sev": "error", "basis": "walk", "steps": [1],
         "msg": "TalkBack stopped at view:2 'Download': two presses in a row moved nothing"}
TRAP = {"code": "tb.trap", "sev": "error", "basis": "walk", "steps": [1], "msg": "trap"}
WEB = {"code": "tb.webview_block", "sev": "error", "basis": "walk", "steps": [1], "msg": "web"}
STOLEN = {"code": "tb.trap", "sev": "warn", "basis": "walk", "steps": [2],
          "msg": "the app pulled accessibility focus"}
OTHER = {"code": "tb.double_stop", "sev": "warn", "basis": "walk", "steps": [1], "msg": "x"}


def test_stuck_after_app_is_unverified_with_the_relaunch_hint():
    rec = _rec(findings=[STUCK, TRAP, WEB, STOLEN, OTHER])
    tbwalk.mark_unverified(rec)
    by = {(f["code"], f["msg"][:5]): f for f in rec["findings"]}
    for f in (by[("tb.edge_stuck", "TalkB")], by[("tb.trap", "trap ")], by[("tb.webview_block", "web (")]):
        assert f["sev"] == "info" and f["basis"] == "unverified: after_app"
        assert "rerun with relaunch=true" in f["msg"]
    assert by[("tb.trap", "the a")]["sev"] == "warn"  # the app's own focus pulls stand
    assert by[("tb.double_stop", "x")]["sev"] == "warn"
    assert [f["sev"] for f in rec["findings"]] == ["warn", "warn", "info", "info", "info"]
    once = json.dumps(rec)
    tbwalk.mark_unverified(rec)  # idempotent (a bound walk is re-analysed, then marked)
    assert json.dumps(rec) == once


def test_an_unproven_keyboard_makes_stuck_unverified_whatever_the_start_order():
    rec = _rec(started="before_app", proven=False, findings=[STUCK])
    tbwalk.mark_unverified(rec)
    assert rec["findings"][0]["basis"] == "unverified: injector"
    assert rec["findings"][0]["sev"] == "info"
    rec = _rec(started="before_app", proven=True, findings=[STUCK])
    tbwalk.mark_unverified(rec)
    assert rec["findings"][0]["sev"] == "error" and rec["findings"][0]["basis"] == "walk"


def test_the_result_and_hints_point_at_relaunch():
    rec = {"id": "w3f9ak1", "talkback_started": "after_app", "injector_proven": True,
           "ended": "stuck", "start": "first", "steps": [
               {"i": 0, "key": "view:1", "ref": "n1", "via": "start", "speak": "Back"},
               {"i": 1, "key": "view:2", "ref": "n2", "via": "next", "speak": "Download"},
               {"i": 2, "key": "view:2", "ref": "n2", "edge": True, "moved": False},
               {"i": 3, "key": "view:2", "ref": "n2", "edge": True, "moved": False}],
           "findings": [dict(STUCK, refs=["n2"])], "captures": ["c7h2kq"],
           "restore": "restored", "vs_model": {"agree": 1, "differ": 0}}
    tbwalk.mark_unverified(rec)
    out = W.walk_result(rec)
    assert out["talkback_started"] == "after_app"
    assert out["findings"][0]["basis"] == "unverified: after_app"
    assert 'tb_walk(relaunch=true,start="first")' in out["next"]


# --------------------------------------------------------------------------- #
# Rows bound before TalkBack started (TB-1, AP-4)
# --------------------------------------------------------------------------- #
def _dump(name: str) -> dict:
    with gzip.open(os.path.join(DATA, f"{name}.a11y.json.gz"), "rt") as f:
        return json.load(f)


def test_the_bound_before_note_names_relaunch_on_an_after_app_walk():
    from inspector_widget.talkback.tree import build
    later = [d.get("kind") for d in build(_dump("thunderbird_list_compose_tb_later")).diagnostics]
    first = [d.get("kind") for d in build(_dump("thunderbird_list_compose_tb_first")).diagnostics]
    assert "recycler_bound_before_service" in later
    assert "recycler_bound_before_service" not in first
    notes = tbwalk.start_notes("after_app", later)
    assert notes == [tbwalk.BOUND_BEFORE_NOTE] and "relaunch=true" in notes[0]
    assert len(notes[0].encode()) <= 200
    assert tbwalk.start_notes("before_app", later) == []
    assert tbwalk.start_notes("after_app", first) == []


def test_the_captures_long_diagnostic_gives_way_to_the_short_note(monkeypatch):
    long = ("7 RecyclerView item(s) have no row/column info although TalkBack is on: they "
            "were bound before it started, ...")

    class _Tree:
        diagnostics = [{"kind": "recycler_bound_before_service", "message": long}]

    class _Tbc:
        tree = _Tree()

    class _Lc:
        def index(self):
            return None

    class _Hook:
        notes = [long, "capture at step 3 failed: x"]
        taken = [(0, _Lc())]

    monkeypatch.setattr(ops, "_tb_capture", lambda ix, lc: _Tbc())
    got = ops._tb_capture_notes({"talkback_started": "after_app", "notes": []}, _Hook())
    assert got == ["capture at step 3 failed: x", tbwalk.BOUND_BEFORE_NOTE]
    # already there from the walk's own model: not twice
    got = ops._tb_capture_notes({"talkback_started": "after_app",
                                 "notes": [tbwalk.BOUND_BEFORE_NOTE]}, _Hook())
    assert got == ["capture at step 3 failed: x"]
    # TalkBack first: the capture's words stay (rows bound before it would be news)
    assert ops._tb_capture_notes({"talkback_started": "before_app", "notes": []},
                                 _Hook()) == _Hook.notes


def test_a_relaunched_walk_in_the_default_listing(tb, monkeypatch):
    monkeypatch.delenv(surface.ENV_TOOLSET)
    listing = mcp_server._fallback_handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    props = {t["name"]: t["inputSchema"]["properties"] for t in listing["result"]["tools"]}
    assert props["tb_walk"]["relaunch"]["default"] is False
    assert props["tb_scenario"]["relaunch"]["default"] is False
    res = ok("tb_walk", serial=SERIAL, package=PKG, relaunch=True, **FAST)
    assert res["talkback_started"] == "before_app"
    assert utf8_len(dumps(res)) <= 5000
    session = iw.connect_existing(SERIAL, PKG)  # the agent of the new process answers
    assert session is not None
    session.disconnect()


# --------------------------------------------------------------------------- #
# The evidence: AntennaPod walked in both start orders (emulator-5558)
# --------------------------------------------------------------------------- #
def _relaunch_walks() -> dict:
    with gzip.open(os.path.join(DATA, "talkback17_relaunch_walks.json.gz"), "rt") as f:
        return json.load(f)


def test_the_relaunch_evidence_is_complete_and_self_consistent():
    walks = _relaunch_walks()
    for name, w in walks.items():
        want = "before_app" if "_tb_first_" in name else "after_app"
        assert w["talkback_started"] == want, name
        if "dump" not in w:  # another process of the same screen: the same words
            same = walks[w["same_screen_as"]]
            assert [s for ok, _k, _l, s in w["steps"][:5] if ok] == \
                [s for ok, _k, _l, s in same["steps"][:5] if ok], name
            continue
        keys = {n.get("node_key") for win in w["dump"]["windows"] for n in _nodes(win.get("root"))}
        moved = [k for ok, k, _l, _s in w["steps"] if ok]
        assert w["start"]["key"] in keys and set(moved[:4]) <= keys, name


def _nodes(n):
    if not n:
        return
    yield n
    for c in n.get("children") or []:
        yield from _nodes(c)


def test_the_expanded_player_has_no_trap_in_either_start_order():
    """emulator-5554's AntennaPod player trap (every press after "Shownotes" stayed put)
    came in none of 6 walks on emulator-5558, TalkBack first or later: TalkBack reads on
    into the show notes. Navigator.traps is to be re-judged on this (deferred)."""
    walks = _relaunch_walks()
    player = {n: w for n, w in walks.items() if n.startswith("antennapod_player_expanded_")}
    assert len(player) == 6
    for name, w in player.items():
        said = [s for ok, _k, _l, s in w["steps"] if ok]
        at = next(i for i, s in enumerate(said) if s and s.endswith("Shownotes"))
        assert said[at + 1] == "Webview" and w["ended"] != "stuck", name
        assert all(ok for ok, *_ in w["steps"]), name


def test_ap4_sticks_only_after_the_app_and_says_so():
    walks = _relaunch_walks()
    first = [w for n, w in walks.items() if n.startswith("antennapod_episode_details_relaunch_tb_first")]
    later = [w for n, w in walks.items() if n.startswith("antennapod_episode_details_relaunch_tb_later")]
    for w in first:  # the page root is a stop, and TalkBack goes on into the notes
        said = [s or "" for ok, _k, _l, s in w["steps"] if ok]
        assert any(s.startswith("Page.") for s in said) and w["ended"] != "stuck"
    assert not any(s and s.startswith("Page.") for w in later for ok, _k, _l, s in w["steps"])
    stuck = [w for w in later if w["ended"] == "stuck"]
    assert len(stuck) == 1 and "tb.edge_stuck (unverified: after_app)" in stuck[0]["findings"]
