"""Session lifecycle, offline: deadlines, poisoning, liveness, retry, detach
semantics, serial resolution, cleanup and the JSON-RPC fallback's robustness.

Runs against the fake agent + fake adb in ``tests/fakeagent.py`` (see
``test_e2e_fake_agent.py`` for the per-tool coverage). Ledger ids: H1, H3, E2,
E4/H5, E5, E7, E9, E11, E12, H9, H10.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import fakeagent
from fakeagent import DEFAULT_PACKAGE as PKG
from fakeagent import DEFAULT_PID as PID
from fakeagent import DEFAULT_SERIAL as SERIAL

import inspector_widget as iw
import mcp_server
from inspector_widget import adb, client as clientmod, inject
from inspector_widget.client import (AgentTimeoutError, Client, ClientError, SessionLostError,
                                     TransportError)
from inspector_widget.proto import view_inspection_pb2 as pb

HOST_DIR = Path(__file__).resolve().parents[1]


def _request(command, **fields):
    req = pb.Request()
    getattr(req, command).SetInParent()
    for k, v in fields.items():
        setattr(getattr(req, command), k, v)
    return req


@pytest.fixture
def agent():
    a = fakeagent.FakeAgent()
    socks = []

    def connect():
        s = socket.create_connection(("127.0.0.1", a.port), timeout=5)
        socks.append(s)
        return Client(s)

    a.connect = connect
    yield a
    a.stop()
    for s in socks:
        s.close()


# =========================================================================== #
# H1: deadlines
# =========================================================================== #
def test_request_deadlines_by_command(monkeypatch):
    monkeypatch.delenv(clientmod.TIMEOUT_ENV, raising=False)
    monkeypatch.delenv(clientmod.LEGACY_TIMEOUT_ENV, raising=False)
    t = clientmod.request_timeout
    assert t(_request("get_properties")) == 30.0
    assert t(_request("dump_tree")) == 30.0
    assert t(_request("hello")) == 10.0
    for slow in ("screenshot", "capture_skp", "dump_compose", "dump_a11y"):
        assert t(_request(slow)) == 120.0, slow
    assert t(_request("dump_tree", include_screenshot=True)) == 120.0
    assert t(_request("dump_tree", include_properties=True)) == 120.0
    monkeypatch.setenv(clientmod.TIMEOUT_ENV, "2")
    assert (t(_request("get_windows")), t(_request("screenshot")), t(_request("hello"))) == (2, 8, 2)
    monkeypatch.setenv(clientmod.TIMEOUT_ENV, "0")
    assert t(_request("screenshot")) is None  # 0 disables the deadline
    monkeypatch.setenv(clientmod.TIMEOUT_ENV, "soon")
    assert t(_request("get_windows")) == 30.0  # garbage falls back to the default
    monkeypatch.delenv(clientmod.TIMEOUT_ENV)
    monkeypatch.setenv(clientmod.LEGACY_TIMEOUT_ENV, "3")
    assert t(_request("get_windows")) == 3.0


def test_a_hung_request_times_out_and_poisons_the_client(agent):
    agent.behaviour = lambda req: ((0, "hang") if req.WhichOneof("command") == "get_windows"
                                   else agent.default_behaviour(req))
    client = agent.connect()
    client._timeout = 0.3
    started = time.monotonic()
    with pytest.raises(AgentTimeoutError, match="did not reply to get_windows within 0.3s"):
        client.get_windows()
    assert time.monotonic() - started < 2
    assert client.broken and not client.is_open()
    sent = len(agent.requests)
    with pytest.raises(SessionLostError, match="no reply to get_windows"):
        client.hello()  # refused locally: the stream may still carry the late reply
    assert len(agent.requests) == sent


def test_a_slow_trickle_still_meets_the_whole_frame_deadline(agent):
    resp = agent.dispatch(_request("hello"))
    frame = fakeagent.frame(resp)
    agent.behaviour = lambda req: (0, frame[:6])  # half a header, then nothing
    client = agent.connect()
    with pytest.raises(AgentTimeoutError):
        client.hello(timeout=0.3)


# =========================================================================== #
# H3: poisoning + id-0 errors
# =========================================================================== #
def test_eof_poisons_the_client(agent):
    client = agent.connect()
    client.hello()
    agent.kill_clients()
    with pytest.raises(SessionLostError, match="agent session lost"):
        client.get_windows()
    assert client.broken
    with pytest.raises(SessionLostError):
        client.get_windows()


def test_is_open_sees_eof_and_stray_bytes_without_a_round_trip(agent):
    client = agent.connect()
    client.hello()
    assert client.is_open()
    agent.kill_clients()
    deadline = time.monotonic() + 2
    while client.is_open() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not client.is_open() and "closed the connection" in client.broken

    other = agent.connect()
    agent.behaviour = lambda req: (0, fakeagent.frame(agent.dispatch(req)) * 2)  # a duplicate reply
    other.hello()
    time.sleep(0.05)
    assert not other.is_open() and "out of sync" in other.broken


def test_an_agent_error_with_id_zero_keeps_the_connection(agent):
    agent.behaviour = lambda req: (
        (0, fakeagent.error_response(0, "Malformed request: boom"))
        if req.WhichOneof("command") == "get_windows" else agent.default_behaviour(req))
    client = agent.connect()
    with pytest.raises(ClientError, match="Malformed request: boom"):
        client.get_windows()
    assert not client.broken
    assert client.hello().agent_version.startswith("viewspector-")


def test_a_mismatched_reply_id_poisons_the_client(agent):
    agent.behaviour = lambda req: (0, pb.Response(id=req.id + 7, status=pb.Response.OK))
    client = agent.connect()
    with pytest.raises(SessionLostError, match="expected 1, got 8"):
        client.hello()
    assert client.broken


def test_shutdown_on_a_closed_client_says_nothing_was_sent(agent):
    client = agent.connect()
    client.hello()  # the connection is registered with the agent
    agent.kill_clients()
    with pytest.raises(SessionLostError):
        client.get_windows()
    with pytest.raises(SessionLostError):
        client.shutdown()
    assert agent.running


# =========================================================================== #
# E4/H5 + E9: Session lifecycle and metadata
# =========================================================================== #
def test_session_context_manager_disconnects_and_keeps_the_agent(fake_device):
    with iw.attach(SERIAL, PKG) as session:
        assert session.info() == {
            "serial": SERIAL, "package": PKG, "pid": PID, "warm": False,
            "agent_version": "viewspector-0.1", "build_id": inject.local_build_id(),
            "api_level": 36, "abi": "arm64-v8a"}
        assert session.is_alive()
        agent = fake_device.agent()
    assert session.closed and not session.is_alive()
    assert agent.running and "shutdown" not in fake_device.commands()
    assert fake_device.forward_names() == []
    session.disconnect()  # idempotent


def test_session_shutdown_stops_the_agent_for_everyone(fake_device):
    first = iw.attach(SERIAL, PKG)
    second = iw.attach(SERIAL, PKG)
    assert second.warm
    assert first.shutdown() is True
    assert fake_device.agent() is None
    assert not second.is_alive()
    second.disconnect()
    assert fake_device.forward_names() == []


def test_session_shutdown_after_the_connection_broke_uses_a_fresh_one(fake_device):
    session = iw.attach(SERIAL, PKG)
    agent = fake_device.agent()
    agent.kill_clients()
    with pytest.raises(SessionLostError):
        session.get_windows()
    assert session.shutdown() is True
    assert not agent.running and fake_device.forward_names() == []


def test_is_alive_notices_an_app_restart(fake_device):
    session = iw.attach(SERIAL, PKG)
    try:
        assert session.is_alive()
        fake_device.apps[PKG].pid = PID + 1  # same socket still open, but a new process
        assert not session.is_alive()
    finally:
        session.disconnect()


def test_connect_existing_never_injects(fake_device):
    assert iw.connect_existing(SERIAL, PKG) is None
    assert not fake_device.pushed and not fake_device.attach_calls
    fake_device.start_agent(PKG)
    session = iw.connect_existing(SERIAL, PKG)
    try:
        assert session is not None and session.warm
    finally:
        session.disconnect()


# =========================================================================== #
# E2/H2 + E11: the MCP session cache
# =========================================================================== #
def test_mcp_retries_once_when_the_connection_drops_mid_call(mcp, fake_device):
    assert mcp("attach")["attached"]
    agent = fake_device.agent()
    dropped = {"n": 0}

    def drop_first_dump(req):
        if req.WhichOneof("command") == "dump_tree" and not dropped["n"]:
            dropped["n"] += 1
            return 0, "close"
        return agent.default_behaviour(req)

    agent.behaviour = drop_first_dump
    res = mcp("dump_tree")
    assert "error" not in res and res["root_count"] == 2
    assert fake_device.commands().count("dump_tree") == 2
    assert len(fake_device.attach_calls) == 1  # the retry reconnected warm


def test_mcp_does_not_retry_a_timeout_and_gives_a_hint(mcp, fake_device, monkeypatch):
    monkeypatch.setenv(clientmod.TIMEOUT_ENV, "0.3")
    assert mcp("attach")["attached"]
    agent = fake_device.agent()
    agent.behaviour = lambda req: ((0, "hang") if req.WhichOneof("command") == "get_properties"
                                   else agent.default_behaviour(req))
    try:
        res = mcp("get_properties", view_id=1003)
        assert res["tool"] == "get_properties" and "within 0.3s" in res["error"]
        assert clientmod.TIMEOUT_ENV in res["hint"]
        assert fake_device.commands().count("get_properties") == 1
    finally:
        fake_device.close()


def test_mcp_errors_for_a_lost_session_carry_the_logcat_hint(mcp, fake_device, warm_agent):
    warm_agent.behaviour = lambda req: (
        (0, "close") if req.WhichOneof("command") == "screenshot"
        else warm_agent.default_behaviour(req))
    res = mcp("screenshot")
    assert "agent session lost" in res["error"]
    assert "adb logcat -s ViewSpector" in res["hint"]
    assert fake_device.commands().count("screenshot") == 2  # tried, re-attached, tried once more


def test_mcp_attach_fails_when_the_agent_cannot_list_windows(mcp, fake_device, warm_agent):
    warm_agent.behaviour = lambda req: (
        (0, fakeagent.error_response(req.id, "IllegalStateException: no windows"))
        if req.WhichOneof("command") == "get_windows" else warm_agent.default_behaviour(req))
    res = mcp("attach")
    assert "no windows" in res["error"] and "attached" not in res


def test_mcp_attach_reports_reuse_warmth_and_root_ids(mcp, fake_device):
    first = mcp("attach")
    assert (first["warm"], first["reused"], first["pid"]) == (False, False, PID)
    assert first["root_ids"] == [1001, 2001]
    again = mcp("attach")
    assert (again["warm"], again["reused"]) == (False, True)


def test_mcp_detach_without_shutdown_leaves_the_agent_running(mcp, fake_device):
    assert mcp("attach")["attached"]
    res = mcp("detach", shutdown=False)
    assert res == {"serial": SERIAL, "package": PKG, "detached": True, "agent_stopped": False}
    assert fake_device.agent() is not None and fake_device.forward_names() == []
    assert mcp_server.SESSIONS.peek(SERIAL, PKG) is None
    assert mcp("detach", shutdown=False)["detached"] is False


def test_mcp_detach_stops_an_agent_it_did_not_attach(mcp, fake_device, warm_agent):
    res = mcp("detach")
    assert (res["detached"], res["agent_stopped"]) == (True, True)
    assert not warm_agent.running and not fake_device.attach_calls


def test_session_cache_locks_per_app(fake_device, monkeypatch):
    """A slow cold attach to one app must not block tools on another (E11)."""
    cache = mcp_server.SESSIONS
    release = threading.Event()
    entered = threading.Event()
    real_attach = mcp_server.HOST.attach

    def attach(serial, package, force_reinject=False):
        if package == "com.example.slow":
            entered.set()
            release.wait(5)
            raise RuntimeError("slow app gave up")
        return real_attach(serial, package, force_reinject=force_reinject)

    monkeypatch.setattr(mcp_server.HOST, "attach", attach)
    slow = threading.Thread(target=lambda: pytest.raises(RuntimeError, cache.get_or_attach,
                                                         SERIAL, "com.example.slow"))
    slow.start()
    try:
        assert entered.wait(5)
        started = time.monotonic()
        session = cache.get_or_attach(SERIAL, PKG)
        assert session.package == PKG and time.monotonic() - started < 2
    finally:
        release.set()
        slow.join(5)


# =========================================================================== #
# E7 / H9 / H10: devices and serials
# =========================================================================== #
@pytest.fixture
def two_devices(fake_device, monkeypatch, tmp_path):
    other = fakeagent.default_device(serial="emulator-5556")
    fakeagent.install(monkeypatch, fake_device, other, build_out=str(tmp_path / "build-out"))
    yield other
    other.close()


def test_the_only_device_is_used_when_no_serial_is_given(fake_device, run_cli, monkeypatch):
    monkeypatch.delenv(adb.SERIAL_ENV, raising=False)
    assert adb.resolve_serial(None) == SERIAL
    res = run_cli("attach")
    assert res.rc == 0, res


def test_two_devices_without_a_serial_is_a_clear_error(fake_device, two_devices, run_cli,
                                                       monkeypatch):
    monkeypatch.delenv(adb.SERIAL_ENV, raising=False)
    res = run_cli("dump")
    assert res.rc == 1
    assert "more than one device" in res.err and "emulator-5556" in res.err
    out = mcp_server._run_tool("dump_tree", {"package": PKG})
    assert "more than one device" in out["error"] and "serial" in out["error"]
    monkeypatch.setenv(adb.SERIAL_ENV, "emulator-5556")
    assert mcp_server._run_tool("dump_tree", {"package": PKG})["serial"] == "emulator-5556"
    assert two_devices.attach_calls and not fake_device.attach_calls


def test_an_offline_or_unauthorized_device_says_so(fake_device, run_cli):
    fake_device.state = "unauthorized"
    res = run_cli("attach", "--serial", SERIAL)
    assert res.rc == 1 and "is unauthorized" in res.err and "Allow USB debugging" in res.err
    fake_device.state = "offline"
    assert "is offline" in run_cli("attach", "--serial", SERIAL).err


def test_no_device_at_all(fake_device, monkeypatch, tmp_path, run_cli):
    fakeagent.install(monkeypatch, build_out=str(tmp_path / "build-out"))
    res = run_cli("screenshot", "--out", tmp_path / "x.png")
    assert res.rc == 1 and "no Android device attached" in res.err


def test_pidof_raises_when_adb_itself_fails(fake_device):
    with pytest.raises(adb.AdbError, match="not found"):
        adb.pidof("emulator-9999", PKG)
    assert adb.pidof(SERIAL, "com.example.idle") is None  # toybox: no match, no stderr


def test_socket_exists_ignores_longer_names(fake_device):
    fake_device.foreign_sockets.append(f"viewspector_{PID}1")
    assert not adb.socket_exists(SERIAL, f"viewspector_{PID}")
    fake_device.foreign_sockets.append(f"viewspector_{PID}")
    assert adb.socket_exists(SERIAL, f"viewspector_{PID}")


# =========================================================================== #
# E5: argument validation and the JSON-RPC fallback
# =========================================================================== #
@pytest.mark.parametrize("args,message", [
    ({"package": PKG, "view_id": 1, "bogus": 1}, "unknown argument(s): bogus (allowed: "),
    ({"package": PKG, "view_id": "1003"}, "invalid argument view_id: '1003' is not of type 'integer'"),
    ({"view_id": 1003}, "missing required argument: package"),
    ({"package": PKG, "view_id": True}, "invalid argument view_id: True is not of type 'integer'"),
])
@pytest.mark.parametrize("with_jsonschema", [True, False])
def test_arguments_are_validated_against_the_schema(fake_device, monkeypatch, args, message,
                                                    with_jsonschema):
    if not with_jsonschema:
        monkeypatch.setitem(sys.modules, "jsonschema", None)  # import fails -> minimal checks
    res = mcp_server._run_tool("get_properties", dict(args, serial=SERIAL))
    assert set(res) == {"error", "tool"} and res["tool"] == "get_properties"
    assert res["error"].startswith(message), res["error"]
    assert fake_device.adb_log == []


def test_minimal_validator_matches_jsonschema_on_the_basics():
    schema = mcp_server.TOOLS["inspect_node"]["schema"]
    for bad in ({"package": 5}, {"package": "p", "bounds": {"x": 1}}, {"package": "p", "zzz": 1},
                {}, {"package": "p", "view_id": 1.5}):
        with pytest.raises(mcp_server.ToolError):
            mcp_server._minimal_validate(schema, bad)
    mcp_server._minimal_validate(schema, {"package": "p", "bounds": {"x": 1, "y": 2, "w": 3, "h": 4}})


@pytest.mark.parametrize("message,code", [
    ([{"jsonrpc": "2.0", "id": 1, "method": "ping"}], -32600),
    ("tools/list", -32600),
    (7, -32600),
    ({"jsonrpc": "2.0", "id": 1, "params": {}}, -32600),
    ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": "x"}, -32602),
    ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": 3}}, -32602),
])
def test_fallback_rejects_malformed_messages_without_tracebacks(message, code):
    resp = mcp_server._fallback_handle(message)
    assert resp["error"]["code"] == code
    assert "data" not in resp["error"]


def test_fallback_tools_call_with_non_object_arguments_is_a_tool_error():
    resp = mcp_server._fallback_handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                        "params": {"name": "attach", "arguments": [1]}})
    assert resp["result"]["isError"] is True
    assert "arguments must be a JSON object" in resp["result"]["content"][0]["text"]


def _block_mcp_script(script):
    return ("import sys, runpy\n"
            "class _Block:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'mcp' or name.startswith('mcp.'):\n"
            "            raise ImportError('mcp blocked for test')\n"
            "sys.meta_path.insert(0, _Block())\n"
            f"sys.argv = [{script!r}]\n"
            "runpy.run_path(sys.argv[0], run_name='__main__')\n")


def test_fallback_server_survives_batches_and_scalars():
    script = str(HOST_DIR / "mcp_server.py")
    lines = ['[{"jsonrpc":"2.0","id":1,"method":"ping"}]', '"hello"', "42", "not json",
             json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"})]
    proc = subprocess.run([sys.executable, "-c", _block_mcp_script(script)],
                          input="\n".join(lines) + "\n", capture_output=True, text=True,
                          env=dict(os.environ, PYTHONPATH=str(HOST_DIR)), cwd=str(HOST_DIR),
                          timeout=60)
    assert proc.returncode == 0, proc.stderr[-2000:]
    replies = [json.loads(l) for l in proc.stdout.splitlines() if l.startswith("{")]
    assert [r.get("error", {}).get("code") for r in replies] == [-32600, -32600, -32600, -32700, None]
    assert replies[-1] == {"jsonrpc": "2.0", "id": 9, "result": {}}
    assert "Traceback" not in proc.stdout


# =========================================================================== #
# E12: temp files
# =========================================================================== #
def test_mcp_pngs_live_in_one_per_process_directory(mcp, fake_device):
    a = Path(mcp("screenshot")["path"])
    b = Path(mcp("screenshot", scale=0.5)["path"])
    assert a.parent == b.parent and a.parent.parent == fake_device.tmpdir
    assert a.parent.name.startswith(f"inspector-widget-{os.getpid()}-")
    mcp_server._cleanup_at_exit()
    assert not a.parent.exists()
    assert fake_device.forward_names() == []


# =========================================================================== #
# H4: build handshake
# =========================================================================== #
def test_local_build_id_is_the_payload_sha256(fake_device, tmp_path):
    import hashlib
    payload = tmp_path / "build-out" / inject.PAYLOAD_JAR_NAME
    assert inject.local_build_id() == hashlib.sha256(payload.read_bytes()).hexdigest()
    assert inject.artifact_status()["build_id"] == inject.local_build_id()
    assert inject.split_agent_version("viewspector-0.1+abc") == ("viewspector-0.1", "abc")
    assert inject.split_agent_version("viewspector-0.1") == ("viewspector-0.1", None)
    assert inject.build_matches("unknown", "abc") and inject.build_matches(None, None)
    assert not inject.build_matches(None, "abc") and not inject.build_matches("x", "abc")


def test_mcp_attach_force_replaces_the_agent(mcp, fake_device):
    first = mcp("attach")
    assert first["build_id"] == inject.local_build_id()
    old = fake_device.agent()
    again = mcp("attach", force=True)
    assert "error" not in again, again
    assert not old.running and fake_device.agent().generation == 2
    assert again["reused"] is False and again["warm"] is False


def test_mcp_attach_notes_a_cached_session_on_an_old_build(mcp, fake_device, tmp_path):
    assert "note" not in mcp("attach")
    (tmp_path / "build-out" / inject.PAYLOAD_JAR_NAME).write_bytes(b"payload, rebuilt")
    res = mcp("attach")
    assert res["reused"] is True and "force=true" in res["note"]
    assert "different build" in res["note"]


# =========================================================================== #
# Review round: stopping an agent other clients are connected to
# =========================================================================== #
def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return predicate()


def _hang_on(agent, command):
    agent.behaviour = lambda req: ((0, "hang") if req.WhichOneof("command") == command
                                   else agent.default_behaviour(req))


def test_client_connections_are_listed_under_the_agent_name_but_are_not_an_agent(fake_device):
    first = iw.attach(SERIAL, PKG)
    second = iw.attach(SERIAL, PKG)
    name = first.injection.socket_name
    assert adb.socket_exists(SERIAL, name) and adb.socket_connections(SERIAL, name) == 2
    fake_device.agent().close_clients_on_stop = False  # an agent from before the stop fix
    fake_device.agent().stop()
    # `first` and `second` are still connected, so /proc/net/unix still lists
    # their connections under the name; nothing listens on it any more.
    assert adb.socket_connections(SERIAL, name) == 2
    assert not adb.socket_exists(SERIAL, name)
    first.disconnect()
    second.disconnect()
    assert _wait_until(lambda: adb.socket_connections(SERIAL, name) == 0)


def test_shutdown_with_another_client_connected_returns_at_once(fake_device):
    other = iw.attach(SERIAL, PKG)
    fake_device.agent().close_clients_on_stop = False
    session = iw.attach(SERIAL, PKG)
    started = time.monotonic()
    try:
        assert session.shutdown() is True
        assert time.monotonic() - started < 2
    finally:
        other.disconnect()


def test_force_reinject_while_another_client_is_connected(fake_device):
    other = iw.attach(SERIAL, PKG)  # e.g. an MCP server's cached session
    fake_device.agent().close_clients_on_stop = False
    started = time.monotonic()
    fresh = iw.attach(SERIAL, PKG, force_reinject=True)
    try:
        assert time.monotonic() - started < 2
        assert not fresh.warm and fake_device.agent().generation == 2
    finally:
        fresh.disconnect()
        other.disconnect()


def test_mcp_session_recovers_after_a_cli_force_reinject(mcp, fake_device, run_cli):
    assert mcp("attach")["attached"]
    fake_device.agent().close_clients_on_stop = False  # the MCP connection lingers, unreadable
    res = run_cli("attach", "--force")
    assert res.rc == 0, res
    out = mcp("dump_tree")
    assert "error" not in out and out["root_count"] == 2
    assert fake_device.agent().generation == 2 and len(fake_device.attach_calls) == 2


def test_a_stopped_agent_disconnects_every_client(fake_device):
    """Server.stop() shuts every client connection down: the other session
    sees EOF at once instead of on its next request."""
    other = iw.attach(SERIAL, PKG)
    session = iw.attach(SERIAL, PKG)
    assert session.shutdown() is True
    assert _wait_until(lambda: not other.client.is_open())
    assert not other.is_alive()
    other.disconnect()


# =========================================================================== #
# Review round: a stale-build agent that other clients use is kept
# =========================================================================== #
def test_a_stale_build_agent_other_clients_use_is_kept_with_a_warning(fake_device, run_cli):
    stale = fake_device.start_agent(PKG, build_id="0" * 64)
    other = iw.connect_existing(SERIAL, PKG)  # e.g. an MCP server from another checkout
    try:
        res = run_cli("attach")
        assert res.rc == 0, res
        assert "warning:" in res.err and "1 other client(s)" in res.err and "--force" in res.err
        assert stale.running and "shutdown" not in fake_device.commands()
        assert not fake_device.attach_calls and other.is_alive()
        forced = run_cli("attach", "--force")
        assert forced.rc == 0 and not stale.running and fake_device.agent().generation == 2
    finally:
        other.disconnect()


def test_mcp_attach_keeps_a_stale_build_agent_another_client_uses(mcp, fake_device):
    stale = fake_device.start_agent(PKG, build_id="0" * 64)
    other = iw.connect_existing(SERIAL, PKG)
    try:
        res = mcp("attach")
        assert res["attached"] and res["build_id"] == "0" * 64
        assert "other client(s)" in res["note"] and "force=true" in res["note"]
        assert stale.running and not fake_device.attach_calls
    finally:
        other.disconnect()


# =========================================================================== #
# Review round: detach tells the truth
# =========================================================================== #
def test_mcp_detach_after_an_app_restart_stops_the_agent_that_runs_now(mcp, fake_device, run_cli):
    assert mcp("attach")["attached"]
    fake_device.restart_app(new_pid=5353)
    assert run_cli("attach").rc == 0  # another client injects into the new process
    assert fake_device.agent() is not None
    res = mcp("detach")
    assert (res["detached"], res["agent_stopped"]) == (True, True)
    assert fake_device.agent() is None  # the agent on pid 5353 is gone too


def test_detach_reports_an_agent_that_does_not_stop(mcp, fake_device, warm_agent, run_cli,
                                                   monkeypatch):
    monkeypatch.setattr(inject, "STOP_WAIT", 0.2)
    warm_agent.behaviour = lambda req: (
        (0, fakeagent.frame(warm_agent.dispatch(req)))  # replies OK, but never stops
        if req.WhichOneof("command") == "shutdown" else warm_agent.default_behaviour(req))
    res = mcp("detach")
    assert res["agent_stopped"] is False and "still there" in res["note"]
    cli_res = run_cli("detach")
    assert cli_res.rc == 1 and "(agent stopped)" not in cli_res.err
    assert "still there" in cli_res.err and "am force-stop" in cli_res.err
    assert warm_agent.running


def test_detach_during_a_request_in_flight_really_sends_shutdown(mcp, fake_device, monkeypatch):
    monkeypatch.setenv(clientmod.TIMEOUT_ENV, "1")
    assert mcp("attach")["attached"]
    agent = fake_device.agent()
    _hang_on(agent, "get_properties")
    result = {}
    worker = threading.Thread(target=lambda: result.update(mcp("get_properties", view_id=1003)),
                              daemon=True)
    worker.start()
    try:
        assert _wait_until(lambda: "get_properties" in fake_device.commands())
        res = mcp("detach")
        assert "shutdown" in fake_device.commands()
        assert res["agent_stopped"] is True and not agent.running
    finally:
        fake_device.close()
        worker.join(10)


def test_shutdown_refuses_locally_on_a_closed_client_with_not_sent(agent):
    client = agent.connect()
    client.hello()
    agent.kill_clients()
    with pytest.raises(SessionLostError):
        client.get_windows()
    with pytest.raises(clientmod.NotSentError):
        client.shutdown()


# =========================================================================== #
# Review round: disconnect wakes a request in flight
# =========================================================================== #
def test_disconnect_wakes_a_request_in_flight(fake_device, monkeypatch):
    monkeypatch.setenv(clientmod.TIMEOUT_ENV, "3")
    session = iw.attach(SERIAL, PKG)
    agent = fake_device.agent()
    agent.behaviour = lambda req: ((1.0, agent.dispatch(req)) if req.WhichOneof("command") ==
                                   "dump_tree" else agent.default_behaviour(req))
    outcome = {}

    def run():
        started = time.monotonic()
        try:
            session.dump_tree()
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            outcome["exc"] = exc
        outcome["elapsed"] = time.monotonic() - started

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert _wait_until(lambda: "dump_tree" in fake_device.commands())
    session.disconnect()
    worker.join(10)
    assert isinstance(outcome.get("exc"), SessionLostError), outcome
    assert outcome["elapsed"] < 0.9


def test_mcp_detach_without_shutdown_mid_call_lets_the_call_retry(mcp, fake_device, monkeypatch):
    monkeypatch.setenv(clientmod.TIMEOUT_ENV, "3")
    assert mcp("attach")["attached"]
    agent = fake_device.agent()
    slow = {"n": 0}

    def slow_first_dump(req):
        if req.WhichOneof("command") == "dump_tree" and not slow["n"]:
            slow["n"] += 1
            return 1.0, agent.dispatch(req)
        return agent.default_behaviour(req)

    agent.behaviour = slow_first_dump
    result = {}
    worker = threading.Thread(target=lambda: result.update(mcp("dump_tree")), daemon=True)
    worker.start()
    assert _wait_until(lambda: "dump_tree" in fake_device.commands())
    started = time.monotonic()
    assert mcp("detach", shutdown=False)["detached"] is True
    worker.join(10)
    assert "error" not in result and result["root_count"] == 2, result
    # The retry queues behind the first dump (device work is serialized) but
    # does not wait out the 3s deadline.
    assert time.monotonic() - started < 2.5


# =========================================================================== #
# Review round: forwards
# =========================================================================== #
def test_adb_picks_the_forward_port(fake_device, monkeypatch):
    """Picking a free port on the host and then forwarding it races with every
    other forward; `adb forward tcp:0` lets adb pick."""
    monkeypatch.setattr(adb, "free_local_port", lambda: pytest.fail("host-picked port (racy)"))
    with iw.attach(SERIAL, PKG) as session:
        assert session.injection.local_port in fake_device.forwards
    made = [a for a in fake_device.adb_log if a[0] == "forward" and a[1] != "--remove"]
    assert made and all(a[1] == "tcp:0" for a in made)
    assert fake_device.forward_names() == []


@pytest.mark.parametrize("warm", [True, False])
def test_an_interrupted_attach_leaves_no_forward(fake_device, monkeypatch, warm):
    if warm:
        fake_device.start_agent(PKG)

    def interrupted(self, timeout=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(clientmod.Client, "hello", interrupted)
    with pytest.raises(KeyboardInterrupt):
        iw.attach(SERIAL, PKG)
    assert fake_device.forward_names() == []
    assert bool(fake_device.attach_calls) is not warm


def test_exit_cleanup_removes_a_forward_no_session_owns(fake_device):
    adb.forward(SERIAL, 0, f"viewspector_{PID}")  # an attach cut short before it made a session
    assert fake_device.forward_names() == [f"viewspector_{PID}"]
    mcp_server._cleanup_at_exit()
    assert fake_device.forward_names() == []


def _mcp_child(tmp_path, block_mcp):
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    log = tmp_path / "wire.jsonl"
    cmd = [sys.executable, str(Path(fakeagent.__file__)), "--mcp-child", str(log),
           str(tmp_path / "build-out")] + (["--block-mcp"] if block_mcp else [])
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, cwd=str(HOST_DIR),
                            env=dict(os.environ, PYTHONPATH=str(HOST_DIR), TMPDIR=str(tmp)))
    return proc, log, tmp


@pytest.mark.parametrize("transport", ["sdk", "fallback"])
def test_sigterm_with_stdin_open_cleans_up_and_exits(tmp_path, transport):
    if transport == "sdk":
        pytest.importorskip("mcp")
    proc, log, tmp = _mcp_child(tmp_path, transport == "fallback")
    try:
        for msg in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "pytest", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "screenshot", "arguments": {"serial": SERIAL, "package": PKG}}},
        ):
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
            while "id" in msg:
                line = proc.stdout.readline()
                assert line, proc.stderr.read()[-2000:]
                if line.startswith("{") and json.loads(line).get("id") == msg["id"]:
                    break
        assert list(tmp.glob("inspector-widget-*"))  # the screenshot's PNG directory
        proc.terminate()  # SIGTERM, stdin still open
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail(f"{transport}: still running 10s after SIGTERM with stdin open")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for f in (proc.stdin, proc.stdout, proc.stderr):
            f.close()
    records = [json.loads(line) for line in log.read_text().splitlines()]
    [exit_record] = [r for r in records if r["event"] == "exit"]
    assert exit_record["forwards"] == []
    assert list(tmp.glob("inspector-widget-*")) == []


# =========================================================================== #
# Review round: timeouts are reported, not retried or swallowed
# =========================================================================== #
def test_mcp_attach_does_not_retry_a_timeout(mcp, fake_device, warm_agent, monkeypatch):
    monkeypatch.setenv(clientmod.TIMEOUT_ENV, "0.3")
    _hang_on(warm_agent, "get_windows")
    try:
        res = mcp("attach")
        assert "AgentTimeoutError" in res["error"], res
        assert fake_device.commands().count("get_windows") == 1
    finally:
        fake_device.close()


def test_mcp_attach_reattaches_once_when_the_connection_drops(mcp, fake_device, warm_agent):
    dropped = {"n": 0}

    def drop_first_get_windows(req):
        if req.WhichOneof("command") == "get_windows" and not dropped["n"]:
            dropped["n"] += 1
            return 0, "close"
        return warm_agent.default_behaviour(req)

    warm_agent.behaviour = drop_first_get_windows
    res = mcp("attach")
    assert res["attached"] and res["window_count"] == 2, res
    assert fake_device.commands().count("get_windows") == 2
    assert fake_device.commands().count("hello") == 2


@pytest.mark.parametrize("tool", ["a11y_lint", "a11y_overlay"])
def test_a11y_tools_report_a_screenshot_timeout(mcp, fake_device, monkeypatch, tool):
    monkeypatch.setenv(clientmod.TIMEOUT_ENV, "0.3")  # a screenshot gets 4x: 1.2s
    assert mcp("attach")["attached"]
    _hang_on(fake_device.agent(), "screenshot")
    try:
        res = mcp(tool)
        assert "AgentTimeoutError" in res.get("error", ""), res
        assert "screenshot" in res["error"]
        assert fake_device.commands().count("screenshot") == 1
        assert fake_device.commands().count("dump_compose") == 1
    finally:
        fake_device.close()


# =========================================================================== #
# Review round: each error's hint is its own next step
# =========================================================================== #
def test_errors_carry_the_hint_for_their_own_next_step(mcp, fake_device, run_cli):
    res = mcp("dump_tree", package="com.example.idle")
    assert "is not running" in res["error"]
    assert "monkey -p com.example.idle" in res["hint"] and "logcat" not in res["hint"]
    res = mcp("dump_tree", package="com.example.release")
    assert "not debuggable" in res["error"] and "debug build" in res["hint"]
    assert "logcat" not in res["hint"]
    cli_res = run_cli("dump", "--package", "com.example.idle")
    assert cli_res.rc == 1 and "\nhint: Launch it, e.g." in cli_res.err


def test_the_timeout_hint_says_where_the_mcp_server_reads_it():
    assert clientmod.TIMEOUT_ENV in AgentTimeoutError.hint
    assert "MCP server" in AgentTimeoutError.hint


def test_a_frozen_app_is_reported_instead_of_queuing_an_attach(mcp, fake_device):
    fake_device.apps[PKG].frozen = True
    res = mcp("attach")
    assert "is frozen" in res["error"] and "foreground" in res["hint"]
    assert not fake_device.attach_calls  # nothing queued to fire later


def test_the_socket_wait_timeout_names_a_frozen_app(fake_device, fast_sleep):
    fake_device.apps[PKG].frozen = True
    with pytest.raises(inject.InjectionError) as frozen:
        inject._wait_for_socket(SERIAL, f"viewspector_{PID}", package=PKG, pid=PID)
    assert "is frozen" in str(frozen.value) and "foreground" in frozen.value.hint
    fake_device.apps[PKG].frozen = False
    with pytest.raises(inject.InjectionError) as other:
        inject._wait_for_socket(SERIAL, f"viewspector_{PID}", package=PKG, pid=PID)
    assert "adb logcat -s ViewSpector" in other.value.hint


def test_a_warm_agent_that_never_answers_hello_is_an_error_not_a_reinject(fake_device, warm_agent,
                                                                           monkeypatch):
    monkeypatch.setenv(clientmod.TIMEOUT_ENV, "0.3")
    _hang_on(warm_agent, "hello")
    try:
        with pytest.raises(inject.InjectionError, match="did not answer Hello") as err:
            iw.attach(SERIAL, PKG)
        assert "busy with another client" in str(err.value)
        assert not fake_device.attach_calls and fake_device.forward_names() == []
        fake_device.apps[PKG].frozen = True
        with pytest.raises(inject.InjectionError, match="is frozen"):
            iw.attach(SERIAL, PKG)
    finally:
        fake_device.close()


def test_an_older_agent_busy_with_another_client_is_not_just_called_frozen(fake_device, warm_agent,
                                                                            monkeypatch):
    """Agents from before the fix answered Hello only between other clients'
    requests (handleLock)."""
    monkeypatch.setenv(clientmod.TIMEOUT_ENV, "0.3")
    warm_agent.hello_waits_for_other_clients = True
    other = iw.connect_existing(SERIAL, PKG)
    warm_agent.behaviour = lambda req: ((1.5, warm_agent.dispatch(req))
                                        if req.WhichOneof("command") == "get_windows"
                                        else warm_agent.default_behaviour(req))
    busy = threading.Thread(target=lambda: pytest.raises(AgentTimeoutError, other.get_windows),
                            daemon=True)
    busy.start()
    try:
        assert _wait_until(lambda: "get_windows" in fake_device.commands())
        with pytest.raises(inject.InjectionError, match="busy with another client"):
            iw.attach(SERIAL, PKG)
    finally:
        busy.join(10)
        other.disconnect()


def test_a_timeout_on_a_frozen_app_says_so(mcp, fake_device, run_cli, monkeypatch):
    """The likeliest cause of a timeout on a cached session is that the user
    switched apps and Android froze this one; the generic hint doesn't say so."""
    monkeypatch.setenv(clientmod.TIMEOUT_ENV, "0.3")
    assert mcp("attach")["attached"]
    _hang_on(fake_device.agent(), "get_properties")
    try:
        res = mcp("get_properties", view_id=1003)
        assert "within 0.3s" in res["error"] and "is frozen" not in res["error"]
        fake_device.apps[PKG].frozen = True
        res = mcp("get_properties", view_id=1003)
        assert "within 0.3s" in res["error"] and "is frozen" in res["error"], res
        assert res["hint"] == inject.FROZEN_HINT
        cli_res = run_cli("get-properties", "--view-id", "1003")
        assert cli_res.rc == 1 and "is frozen" in cli_res.err
        assert f"hint: {inject.FROZEN_HINT}" in cli_res.err
    finally:
        fake_device.close()


def test_abort_from_another_thread_never_leaves_the_reader_waiting():
    """Closing the socket under a thread that is about to wait for the reply
    makes it poll a closed descriptor until its deadline; abort() only shuts
    the socket down and lets that thread close it."""
    for _ in range(50):
        host, peer = socket.socketpair()
        client = Client(host, timeout=2)
        got = {}

        def run():
            started = time.monotonic()
            try:
                client.hello()
            except TransportError as exc:
                got["exc"] = exc
            got["elapsed"] = time.monotonic() - started

        reader = threading.Thread(target=run, daemon=True)
        reader.start()
        fakeagent.framing.read_message(peer)  # the request is out; the reader waits
        client.abort()
        reader.join(5)
        peer.close()
        assert isinstance(got.get("exc"), SessionLostError) and got["elapsed"] < 1.0, got
        assert host.fileno() == -1  # closed by the reader on its way out
