"""Lifecycle polish, offline: no retry during exit cleanup or after a detach,
retries only for calls that are safe to repeat, the session note on every
session tool (CLI parity), argument normalization, JSON-RPC notifications,
bounded forward removal, CLI exit cleanup and overlay temp files, and lost
sessions inside lint rules.

Runs against the fake agent + fake adb in ``tests/fakeagent.py``.
"""

from __future__ import annotations

import io
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from fakeagent import DEFAULT_PACKAGE as PKG
from fakeagent import DEFAULT_PID as PID
from fakeagent import DEFAULT_SERIAL as SERIAL

import inspector_widget as iw
import mcp_server
from inspector_widget import a11y_lint as L
from inspector_widget import adb, client as clientmod, inject
from inspector_widget.client import NotSentError, SessionLostError
from inspector_widget.proto import view_inspection_pb2 as pb

needs_pil = pytest.mark.skipif(
    __import__("importlib").util.find_spec("PIL") is None, reason="Pillow not installed")


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return predicate()


def _in_thread(fn):
    out = {}
    worker = threading.Thread(target=lambda: out.update(fn()), daemon=True)
    worker.start()
    return worker, out


def _hang_on(agent, command):
    agent.behaviour = lambda req: ((0, "hang") if req.WhichOneof("command") == command
                                   else agent.default_behaviour(req))


def _drop_first(agent, command):
    """Close the connection instead of answering the first ``command``."""
    dropped = {"n": 0}

    def behaviour(req):
        if req.WhichOneof("command") == command and not dropped["n"]:
            dropped["n"] += 1
            return 0, "close"
        return agent.default_behaviour(req)

    agent.behaviour = behaviour


# =========================================================================== #
# 1. Exit cleanup and detach never let a retry re-attach
# =========================================================================== #
def test_exit_cleanup_mid_call_does_not_reattach(mcp, fake_device, monkeypatch):
    """The reviewer's exit race: cleanup aborts a call in flight; its retry
    used to re-attach behind the cleanup, leaving a cached session and a
    forward nobody removed."""
    monkeypatch.setenv(clientmod.TIMEOUT_ENV, "5")
    assert mcp("attach")["attached"]
    _hang_on(fake_device.agent(), "screenshot")
    worker, result = _in_thread(lambda: mcp("screenshot"))
    assert _wait_until(lambda: "screenshot" in fake_device.commands())
    mcp_server._cleanup_at_exit()
    worker.join(5)
    assert not worker.is_alive()
    assert "agent session lost" in result["error"], result
    assert fake_device.commands().count("hello") == 1  # no re-attach
    assert mcp_server.SESSIONS.all() == []
    assert fake_device.forward_names() == []


def test_nothing_attaches_once_exit_cleanup_began(mcp, fake_device):
    mcp_server._cleanup_at_exit()
    res = mcp("dump_tree")
    assert res["error"] == "the MCP server is shutting down; not attaching"
    assert fake_device.wire == [] and fake_device.forward_names() == []


class _FakeSession:
    """A host-Session stand-in (as in the reviewer's exit_race.py)."""

    def __init__(self, n):
        self.n = n
        self.gone = threading.Event()
        self.note = None
        self.disconnects = 0

    def is_alive(self):
        return not self.gone.is_set()

    def disconnect(self):
        self.disconnects += 1
        self.gone.set()

    def shutdown(self):
        self.gone.set()  # the agent stops: a request in flight loses its connection
        return True

    def screenshot(self, root_id=0, scale=1.0):
        if self.n == 0:
            self.gone.wait(5)
            raise SessionLostError("agent session lost: disconnected")

        class _NoShot:
            def HasField(self, _field):
                return False

        return _NoShot()


def test_an_attach_that_finishes_after_cleanup_is_released_not_cached(monkeypatch):
    monkeypatch.setattr(mcp_server, "SESSIONS", mcp_server.SessionCache())
    monkeypatch.setattr(mcp_server, "_closing", threading.Event())
    monkeypatch.setattr(adb, "remove_own_forwards", lambda *a, **k: 0)
    entered, release, made = threading.Event(), threading.Event(), []

    def slow_attach(serial, package, force_reinject=False):
        entered.set()
        release.wait(5)
        made.append(_FakeSession(1))
        return made[-1]

    monkeypatch.setattr(mcp_server.HOST, "attach", slow_attach)
    worker, result = _in_thread(lambda: mcp_server._run_tool(
        "screenshot", {"serial": SERIAL, "package": PKG}))
    assert entered.wait(5)
    mcp_server._cleanup_at_exit()  # empties the cache while the attach still runs
    release.set()
    worker.join(5)
    assert result["error"] == "the MCP server is shutting down; not attaching"
    assert made[0].disconnects == 1 and mcp_server.SESSIONS.all() == []


def test_a_call_that_loses_its_session_to_detach_does_not_reinject(monkeypatch):
    monkeypatch.setattr(mcp_server, "SESSIONS", mcp_server.SessionCache())
    monkeypatch.setattr(mcp_server, "_closing", threading.Event())
    attaches = []

    def fake_attach(serial, package, force_reinject=False):
        attaches.append(_FakeSession(len(attaches)))
        return attaches[-1]

    monkeypatch.setattr(mcp_server.HOST, "attach", fake_attach)
    worker, result = _in_thread(lambda: mcp_server._run_tool(
        "screenshot", {"serial": SERIAL, "package": PKG}))
    assert _wait_until(lambda: attaches)
    res = mcp_server._run_tool("detach", {"serial": SERIAL, "package": PKG})
    assert res["agent_stopped"] is True
    worker.join(5)
    assert "agent session lost" in result["error"], result
    assert len(attaches) == 1  # the retry did not attach (inject) again
    assert mcp_server.SESSIONS.all() == []
    # The next call is a new request: it attaches as usual.
    assert mcp_server._run_tool("screenshot", {"serial": SERIAL, "package": PKG})["error"] \
        == "agent returned no screenshot"
    assert len(attaches) == 2


def test_detach_waits_for_an_attach_to_the_same_app(mcp, fake_device, monkeypatch):
    """detach holds the app's session lock: it can't run between an attach
    and the moment that attach caches its session."""
    entered, release = threading.Event(), threading.Event()
    real_attach = mcp_server.HOST.attach

    def slow_attach(*args, **kwargs):
        entered.set()
        release.wait(5)
        return real_attach(*args, **kwargs)

    monkeypatch.setattr(mcp_server.HOST, "attach", slow_attach)
    attaching, attached = _in_thread(lambda: mcp("attach"))
    assert entered.wait(5)
    detaching, detached = _in_thread(lambda: mcp("detach"))
    time.sleep(0.2)
    assert detaching.is_alive()  # queued behind the attach
    release.set()
    attaching.join(5)
    detaching.join(5)
    assert attached["attached"] and detached["agent_stopped"] is True
    assert mcp_server.SESSIONS.all() == [] and fake_device.agent() is None


# =========================================================================== #
# 2. Only calls that are safe to repeat are retried
# =========================================================================== #
def test_a_destructive_dump_compose_is_not_resent(mcp, fake_device, warm_agent):
    _drop_first(warm_agent, "dump_compose")
    res = mcp("dump_compose", enable_inspection=True)
    assert "agent session lost" in res["error"], res
    assert fake_device.commands().count("dump_compose") == 1  # hot-reloaded once, at most


def test_a_read_only_dump_compose_is_retried(mcp, fake_device, warm_agent):
    _drop_first(warm_agent, "dump_compose")
    res = mcp("dump_compose")
    assert "error" not in res, res
    assert fake_device.commands().count("dump_compose") == 2


def test_a_destructive_call_that_was_never_sent_is_retried(mcp, fake_device, warm_agent,
                                                           monkeypatch):
    """NotSentError proves the agent never saw the request, so sending it on a
    fresh connection is its first delivery, not a repeat."""
    real_write = clientmod.framing.write_message
    failed = {"n": 0}

    def write_fails_once(sock, payload):
        command = pb.Request.FromString(payload).WhichOneof("command")
        if command == "dump_compose" and not failed["n"]:
            failed["n"] += 1
            raise BrokenPipeError("broken pipe")
        return real_write(sock, payload)

    monkeypatch.setattr(clientmod.framing, "write_message", write_fails_once)
    res = mcp("dump_compose", enable_inspection=True)
    assert failed["n"] == 1 and "error" not in res, res
    assert fake_device.commands().count("dump_compose") == 1
    assert fake_device.commands().count("hello") == 2  # reconnected (warm) for the retry


def test_capture_slots_enable_is_not_rerun_after_a_later_request_fails(
        mcp, fake_device, warm_agent, monkeypatch):
    """capture(slots="enable") sends DumpCompose(enable_inspection) first; when
    a LATER request of the same capture fails to send, the hot reload already
    ran, so the capture must not run again (two hot reloads)."""
    real_write = clientmod.framing.write_message
    state = {"enabled": False, "failed": 0}

    def write(sock, payload):
        req = pb.Request.FromString(payload)
        command = req.WhichOneof("command")
        if command == "dump_compose" and req.dump_compose.enable_inspection:
            state["enabled"] = True
        elif state["enabled"] and command == "get_windows" and not state["failed"]:
            state["failed"] += 1
            raise BrokenPipeError("broken pipe")
        return real_write(sock, payload)

    monkeypatch.setattr(clientmod.framing, "write_message", write)
    res = mcp("capture", slots="enable")
    reloads = [r.enable_inspection for r in fake_device.requests("dump_compose")]
    assert state["failed"] == 1, (state, fake_device.commands())
    assert reloads.count(True) == 1, reloads
    assert "error" in res


def test_retry_policy_table():
    call = mcp_server._CallState()
    lost, not_sent = SessionLostError("lost"), NotSentError("not sent")
    refusal = mcp_server._retry_refusal
    assert refusal("dump_tree", {}, lost, call) is None
    assert refusal("dump_compose", {"enable_inspection": False}, lost, call) is None
    assert refusal("dump_compose", {"enable_inspection": True}, lost, call) is not None
    assert refusal("dump_compose", {"enable_inspection": True}, not_sent, call) is None
    for tool in ("attach", "detach"):
        assert refusal(tool, {}, not_sent, call) is not None
    # capture reads the app, unless slots="enable" hot-reloads it first
    assert refusal("capture", {}, lost, call) is None
    assert refusal("capture", {"slots": "enable"}, lost, call) is not None
    # its hot reload is one of several requests: a later one's NotSentError
    # does not prove the reload was never delivered
    assert refusal("capture", {"slots": "enable"}, not_sent, call) is not None
    # Every tool is classified: read-only, or one that is not simply repeated.
    unlisted = set(mcp_server.TOOLS) - mcp_server._READ_ONLY_TOOLS - mcp_server._NO_RETRY
    assert unlisted == {"dump_compose", "capture"}


# =========================================================================== #
# 3. Every session tool carries the session's note, as the CLI prints it
# =========================================================================== #
_SESSION_TOOLS = [
    ("attach", {}),
    ("dump_tree", {}),
    ("get_properties", {"view_id": 1003}),
    ("screenshot", {}),
    ("dump_compose", {"include_slot_table": False}),
    pytest.param("compose_overlay", {}, marks=needs_pil),
    ("dump_accessibility", {}),
    ("a11y_lint", {"include_contrast": False}),
    pytest.param("a11y_overlay", {"include_contrast": False}, marks=needs_pil),
    ("inspect", {}),
    ("inspect_node", {"view_id": 1004, "include_image": False}),
    pytest.param("component_image", {"view_id": 1004}, marks=needs_pil),
]


@pytest.mark.parametrize("tool,args", _SESSION_TOOLS)
def test_every_session_tool_notes_a_stale_build_agent(mcp, fake_device, tool, args):
    fake_device.start_agent(PKG, build_id="0" * 64)
    other = iw.connect_existing(SERIAL, PKG)  # kept: another client uses it
    try:
        res = mcp(tool, **args)
        assert "error" not in res, res
        assert "other client(s)" in res["note"] and "force=true" in res["note"]
    finally:
        other.disconnect()


@pytest.mark.parametrize("tool,args", _SESSION_TOOLS)
def test_every_session_tool_notes_a_payload_rebuilt_since_attach(mcp, fake_device, tmp_path,
                                                                 tool, args):
    assert "note" not in mcp("attach")
    (tmp_path / "build-out" / inject.PAYLOAD_JAR_NAME).write_bytes(b"payload, rebuilt")
    res = mcp(tool, **args)
    assert "error" not in res, res
    assert "different build" in res["note"] and "force=true" in res["note"]


def test_the_session_note_follows_a_tools_own_note(mcp, fake_device):
    fake_device.start_agent(PKG, build_id="0" * 64)
    other = iw.connect_existing(SERIAL, PKG)
    try:
        res = mcp("dump_compose")  # slot table not populated: a note of its own
        own, _, session = res["note"].partition(" Also: ")
        assert "slot table not populated" in own and "other client(s)" in session
    finally:
        other.disconnect()


def test_an_error_after_attach_carries_the_session_note(mcp, fake_device):
    fake_device.start_agent(PKG, build_id="0" * 64)
    other = iw.connect_existing(SERIAL, PKG)
    try:
        res = mcp("inspect_node", view_id=999, include_image=False)
        assert "no matching element" in res["error"] and "other client(s)" in res["note"]
    finally:
        other.disconnect()


def test_detach_and_the_device_tools_carry_no_session_note(mcp, fake_device, tmp_path):
    assert mcp("attach")["attached"]
    (tmp_path / "build-out" / inject.PAYLOAD_JAR_NAME).write_bytes(b"payload, rebuilt")
    assert "note" not in mcp("list_devices")
    assert "note" not in mcp("detach")  # as the CLI's detach prints none


def test_cli_and_mcp_print_the_same_note(mcp, fake_device, run_cli):
    fake_device.start_agent(PKG, build_id="0" * 64)
    other = iw.connect_existing(SERIAL, PKG)
    try:
        res = run_cli("dump", "--json", "-")  # first: each sees one other client
        note = mcp("dump_tree")["note"]
        assert res.rc == 0 and f"warning: {note}\n" in res.err
    finally:
        other.disconnect()


# =========================================================================== #
# 4. Argument normalization and validation (both validators)
# =========================================================================== #
@pytest.fixture(params=["jsonschema", "minimal"])
def validator(request, monkeypatch):
    if request.param == "minimal":
        monkeypatch.setitem(sys.modules, "jsonschema", None)
    return request.param


def test_a_whole_number_float_is_an_integer(mcp, fake_device, validator):
    res = mcp("get_properties", view_id=1003.0)
    assert "error" not in res and res["view_id"] == 1003
    assert fake_device.requests("get_properties")[-1].view_id == 1003
    res = mcp("inspect_node", bounds={"x": 20.0, "y": 90.0, "w": 4.0, "h": 4.0},
              include_image=False)
    assert res["node_key"] == "view:1004", res
    res = mcp("get_properties", view_id=1003.5)
    assert res["error"] == "invalid argument view_id: 1003.5 is not of type 'integer'"


def test_a_null_optional_argument_means_the_default(mcp, fake_device, validator):
    res = mcp("dump_tree", include_properties=None, root_id=None, scale=None, serial=None)
    assert "error" not in res and res["serial"] == SERIAL, res
    req = fake_device.requests("dump_tree")[-1]
    assert (req.root_id, req.include_properties) == (0, False)
    assert "error" in mcp("dump_tree", package=None)  # a required one is still required


def test_scale_zero_is_out_of_range(mcp, fake_device, validator):
    res = mcp("a11y_overlay", scale=0)
    assert res["error"] == "invalid argument scale: 0 is less than or equal to the minimum of 0"
    assert fake_device.wire == []


def test_every_scale_schema_excludes_zero():
    scales = [entry["schema"]["properties"]["scale"] for entry in mcp_server.TOOLS.values()
              if "scale" in entry["schema"]["properties"]]
    assert scales and all(s.get("exclusiveMinimum") == 0 and "minimum" not in s for s in scales)


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "x"])
def test_cli_scale_takes_the_mcp_range(run_cli, fake_device, value, capsys):
    with pytest.raises(SystemExit):
        run_cli("screenshot", "--out", "x.png", "--scale", value)
    assert "--scale" in capsys.readouterr().err
    assert fake_device.wire == []


# =========================================================================== #
# 5. The JSON-RPC fallback never answers a notification
# =========================================================================== #
@pytest.mark.parametrize("message", [
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3}},
    {"jsonrpc": "2.0", "method": "ping"},
    {"jsonrpc": "2.0", "method": "no/such/method"},
    {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "list_devices"}},
    {"jsonrpc": "2.0", "method": "tools/list", "params": "not an object"},
])
def test_a_notification_gets_no_reply_and_runs_nothing(message, monkeypatch):
    monkeypatch.setattr(mcp_server, "_call_tool_text",
                        lambda *a: pytest.fail("a notification ran a tool"))
    assert mcp_server._fallback_handle(message) is None


def test_requests_with_an_id_are_still_answered():
    assert mcp_server._fallback_handle({"jsonrpc": "2.0", "id": 0, "method": "ping"}) == \
        {"jsonrpc": "2.0", "id": 0, "result": {}}
    bad = mcp_server._fallback_handle({"jsonrpc": "2.0", "id": None, "method": "nope"})
    assert bad["error"]["code"] == -32601 and bad["id"] is None
    params = mcp_server._fallback_handle({"jsonrpc": "2.0", "id": 4, "method": "tools/list",
                                          "params": []})
    assert params["error"]["code"] == -32602
    as_request = mcp_server._fallback_handle({"jsonrpc": "2.0", "id": 5,
                                              "method": "notifications/initialized"})
    assert as_request == {"jsonrpc": "2.0", "id": 5, "result": {}}  # a request is answered
    # Not a valid request object at all (JSON-RPC 2.0 section 5.1): answered with id null.
    invalid = mcp_server._fallback_handle({"jsonrpc": "2.0", "method": 1})
    assert invalid["error"]["code"] == -32600 and invalid["id"] is None


def test_an_internal_error_on_a_notification_is_not_answered(monkeypatch):
    def broken(message):
        raise RuntimeError("boom")

    monkeypatch.setattr(mcp_server, "_fallback_handle", broken)
    lines = [{"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 7, "method": "ping"}]
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(json.dumps(m) + "\n" for m in lines)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    mcp_server._serve_fallback()
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [(r["id"], r["error"]["code"]) for r in replies] == [(7, -32603)]


# =========================================================================== #
# 6. Forward removal is bounded; the CLI removes its forwards at exit
# =========================================================================== #
def _record_adb_timeouts(monkeypatch):
    seen = []
    real = adb._run

    def run(argv, **kwargs):
        seen.append((list(argv), kwargs.get("timeout", adb.DEFAULT_TIMEOUT)))
        return real(argv, **kwargs)

    monkeypatch.setattr(adb, "_run", run)
    return seen


def test_exit_cleanup_bounds_every_forward_removal(mcp, fake_device, monkeypatch):
    assert mcp("attach")["attached"]
    adb.forward(SERIAL, 0, f"viewspector_{PID}")  # an attach cut short
    seen = _record_adb_timeouts(monkeypatch)
    mcp_server._cleanup_at_exit()
    removals = [t for argv, t in seen if "--remove" in argv]
    assert len(removals) == 2 and all(t <= adb.FORWARD_REMOVE_TIMEOUT for t in removals)
    assert fake_device.forward_names() == []


def test_a_wedged_adb_stops_the_exit_forward_sweep(monkeypatch):
    monkeypatch.setattr(adb, "_OWN_FORWARDS", {(SERIAL, 1): "a", (SERIAL, 2): "b", (SERIAL, 3): "c"})
    calls = []

    def wedged(argv, **kwargs):
        calls.append(kwargs.get("timeout"))
        raise adb.AdbError(argv, -1, "", f"timed out after {kwargs.get('timeout')}s")

    monkeypatch.setattr(adb, "_run", wedged)
    assert adb.remove_own_forwards() == 0
    assert calls == [adb.FORWARD_REMOVE_TIMEOUT]  # one bounded wait, not one per forward
    assert adb._OWN_FORWARDS == {(SERIAL, 2): "b", (SERIAL, 3): "c"}


def test_the_cli_removes_its_leftover_forwards_at_exit(run_cli, fake_device):
    adb.forward(SERIAL, 0, f"viewspector_{PID}")  # e.g. an attach cut short
    assert run_cli("devices").rc == 0
    assert fake_device.forward_names() == []


def test_the_cli_removes_its_forwards_when_a_subcommand_fails(run_cli, fake_device, monkeypatch):
    def interrupted(self, timeout=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(clientmod.Client, "hello", interrupted)
    removed = []
    real = adb.remove_own_forwards
    monkeypatch.setattr(adb, "remove_own_forwards", lambda *a, **k: removed.append(1) or real())
    with pytest.raises(KeyboardInterrupt):
        run_cli("dump")
    assert removed == [1] and fake_device.forward_names() == []


# =========================================================================== #
# 7. CLI overlays never touch a file next to the output
# =========================================================================== #
@needs_pil
@pytest.mark.parametrize("argv", [
    ("compose", "--overlay"),
    ("a11y", "--overlay"),
    ("a11y-lint", "--no-contrast", "--overlay"),
    ("inspect", "--overlay"),
])
def test_an_overlay_leaves_a_users_base_png_alone(run_cli, fake_device, tmp_path, argv):
    out = tmp_path / "shot.png"
    mine = Path(f"{out}.base.png")
    mine.write_bytes(b"the user's file")
    res = run_cli(*argv, out)
    assert res.rc == 0, res
    assert out.is_file() and mine.read_bytes() == b"the user's file"
    assert list(fake_device.tmpdir.glob("inspector-widget-base-*")) == []


# =========================================================================== #
# 8. A lost session inside a lint rule propagates
# =========================================================================== #
def _text_node():
    return {"id": 1, "name": "Node", "kind": "SEMANTICS", "attrs": {"Text": "Hello"},
            "bounds": {"layout": {"x": 0, "y": 0, "w": 100, "h": 40}}, "children": [],
            "render_node_id": 7}


def test_a_lost_session_in_component_image_fn_propagates():
    def lost(_render_node_id):
        raise SessionLostError("agent session lost: disconnected")

    ctx = L.LintContext(density=160, component_image_fn=lost)
    with pytest.raises(SessionLostError):
        L.lint_tree([_text_node()], ctx)


def test_any_other_rule_failure_is_still_a_rule_error():
    def broken(_render_node_id):
        raise ValueError("bad pixels")

    ctx = L.LintContext(density=160, component_image_fn=broken)
    L.lint_tree([_text_node()], ctx)
    errors = [d for d in ctx.diagnostics if d.get("code") == "rule.error"]
    assert errors and "ValueError: bad pixels" in errors[0]["message"]
