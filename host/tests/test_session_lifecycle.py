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
