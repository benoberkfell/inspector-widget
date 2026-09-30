"""Offline end-to-end tests: every CLI subcommand and every MCP tool, run against
the fake agent + fake adb in ``tests/fakeagent.py``.

Only the adb subprocess is faked. The real inject / Session / Client / framing /
correlate / overlay / png code runs, over real TCP, against an agent that
encodes its replies the way the Kotlin payload does. Each test asserts on BOTH
what went over the wire (``fake_device.wire`` / ``fake_device.requests(...)``)
and what the user or agent gets back.

Tests for bugs that are still open in the audit ledger (FINDINGS.md) assert the
CORRECT behaviour and are ``xfail(strict=True)`` with the ledger id, so the fix
flips them to XPASS and strict mode forces the marker to be removed. Ids marked
NEW were found by this harness and are not in the ledger yet.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import fakeagent
from fakeagent import DEFAULT_PACKAGE as PKG
from fakeagent import DEFAULT_PID as PID
from fakeagent import DEFAULT_SERIAL as SERIAL

import cli
import mcp_server
from inspector_widget import adb, framing, inject
from inspector_widget import overlay as overlaymod
from inspector_widget.client import Client, ClientError
from inspector_widget.proto import view_inspection_pb2 as pb

HOST_DIR = Path(__file__).resolve().parents[1]
ARTIFACTS = {inject.NATIVE_SO_NAME: "700", inject.BOOTSTRAP_DEX_NAME: "444",
             inject.PAYLOAD_JAR_NAME: "444"}
OK_BUTTON_RGB = (30, 60, 200)  # the fake paints the OK button (16,80 120x48) this colour


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    return struct.unpack(">II", head[16:24])


def png_pixel(path, x, y):
    image = pytest.importorskip("PIL.Image")
    with image.open(path) as im:
        return im.convert("RGB").getpixel((x, y))


def walk(nodes):
    for n in nodes or []:
        yield n
        yield from walk(n.get("children"))


def find(nodes, **match):
    for n in walk(nodes):
        if all(n.get(k) == v for k, v in match.items()):
            return n
    raise AssertionError(f"no node matching {match}")


def props_by_name(props):
    return {p["name"]: p for p in props}


def decoded(prop):
    """The human value of a property dict, from either JSON shape."""
    return prop.get("label", prop.get("value")) if prop.get("value") in (0, None) else prop["value"]


def cold_injected(dev):
    return bool(dev.attach_calls)


def hang_on(agent, command):
    """Make ``agent`` never answer ``command`` (a frozen main thread)."""
    agent.behaviour = lambda req: ((0, "hang") if req.WhichOneof("command") == command
                                   else agent.default_behaviour(req))


def leftovers(directory, needle):
    """Files under ``directory`` (recursively: the MCP server keeps its PNGs in a
    per-process subdirectory) whose name contains ``needle``."""
    return sorted(p.name for p in Path(directory).rglob("*") if needle in p.name and p.is_file())


needs_pil = pytest.mark.skipif(
    __import__("importlib").util.find_spec("PIL") is None, reason="Pillow not installed")


# =========================================================================== #
# 1. The fake itself speaks the agent's wire semantics.
# =========================================================================== #
_OPEN_SOCKETS = []


@pytest.fixture
def agent():
    a = fakeagent.FakeAgent()
    yield a
    a.stop()
    while _OPEN_SOCKETS:
        _OPEN_SOCKETS.pop().close()


def _connect(agent):
    sock = socket.create_connection(("127.0.0.1", agent.port), timeout=5)
    _OPEN_SOCKETS.append(sock)
    return sock


def _client(agent):
    return Client(_connect(agent))


def test_wire_string_tables_are_one_based_and_zero_is_absent(agent):
    resp = _client(agent).dump_tree(properties=True)
    ids = [e.id for e in resp.strings.entries]
    assert ids == list(range(1, len(ids) + 1))
    decor = resp.roots[0]
    assert decor.text_value == 0 and decor.resource.name == 0  # absent -> 0
    title = [p for g in resp.properties if g.view_id == 1003 for p in g.properties]
    empty_flags = [p for p in title if resp.strings.entries[p.name - 1].str == "scrollIndicators"][0]
    assert empty_flags.str_value == 0  # an empty flag set interns "" -> 0


def test_wire_properties_are_encoded_like_properties_kt(agent):
    client = _client(agent)
    resp = client.get_properties(1003, resolution_stack=True)
    table = {e.id: e.str for e in resp.strings.entries}
    props = {table[p.name]: p for p in resp.group.properties}
    assert table[props["gravity"].str_value] == "center_vertical|start"
    assert props["gravity"].int32_value == 0
    assert table[props["inputType"].str_value] == "text|textCapSentences"
    assert props["paddingStart"].int32_value == 42 and props["paddingStart"].float_value == 0
    assert props["textColor"].int32_value == -14671580
    assert props["textSize"].float_value == 42.0
    assert table[props["id"].resource_value.name] == "title"
    assert props["layout_gravity"].is_layout
    assert table[props["text"].source] == "@style/Widget.App.Title"
    assert [table[s] for s in props["text"].resolution_stack][0] == "@layout/activity_main"
    assert props["layout_gravity"].source == 0  # layout attrs never carry resolution data
    plain = client.get_properties(1003)
    assert all(p.source == 0 and not p.resolution_stack for p in plain.group.properties)


def test_wire_unset_or_unknown_command_is_an_error_with_the_request_id(agent):
    client = _client(agent)
    with pytest.raises(ClientError, match="No command set in request"):
        client.send(pb.Request())
    sock = _connect(agent)
    # id=7 plus an unknown field 99 (a command this agent doesn't know).
    framing.write_message(sock, pb.Request(id=7).SerializeToString() + b"\x98\x06\x01")
    resp = pb.Response.FromString(framing.read_message(sock))
    assert (resp.id, resp.status, resp.error) == (7, pb.Response.ERROR, "No command set in request")


def test_wire_malformed_request_gets_id_zero_error_and_connection_survives(agent):
    sock = _connect(agent)
    framing.write_message(sock, b"\xff\xff\xff")
    resp = pb.Response.FromString(framing.read_message(sock))
    assert resp.id == 0 and resp.status == pb.Response.ERROR
    assert resp.error.startswith("Malformed request")
    hello = pb.Request(id=1)
    hello.hello.SetInParent()
    framing.write_message(sock, hello.SerializeToString())
    assert pb.Response.FromString(framing.read_message(sock)).hello.agent_version == "viewspector-0.1"


def test_wire_bad_magic_drops_the_connection(agent):
    sock = _connect(agent)
    sock.sendall(b"XXXXXXXX" + struct.pack(">I", 2) + b"\x10\x01")
    assert sock.recv(64) == b""


@pytest.mark.parametrize("replies", [True, False], ids=["current", "before-reply-fix"])
def test_wire_shutdown_stops_the_server_and_closes_every_client(agent, replies):
    agent.reply_to_shutdown = replies
    first, second = _client(agent), _client(agent)
    second.hello()
    # Current agents reply, then stop. Older ones stopped before writing the
    # reply, so the requester saw EOF; Client.shutdown() counts that as done too.
    assert first.shutdown() == pb.ShutdownResponse()
    assert first.broken  # the client closes itself after a shutdown either way
    with pytest.raises((framing.FramingError, OSError)):
        second.hello()
    assert not agent.running
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", agent.port), timeout=2)


def test_wire_screenshot_is_abgr_8888(agent):
    from inspector_widget import png as pngmod
    shot = _client(agent).screenshot(scale=1.0).screenshot
    assert shot.bitmap_type == 2 and (shot.width, shot.height) == (360, 640)
    w, h, rgba = pngmod._decode_to_rgba(shot)
    i = (90 * w + 20) * 4
    assert tuple(rgba[i:i + 3]) == OK_BUTTON_RGB


# =========================================================================== #
# 2. Injection: the real inject_and_connect over the fake adb.
# =========================================================================== #
def test_cold_inject_pushes_stages_attaches_and_pings(fake_device):
    inj = inject.inject_and_connect(serial=SERIAL, package=PKG)
    try:
        assert (inj.warm, inj.pid, inj.socket_name) == (False, PID, f"viewspector_{PID}")
        assert set(fake_device.pushed) == {f"/data/local/tmp/{n}" for n in ARTIFACTS}
        for name, mode in ARTIFACTS.items():
            assert fake_device.staged[(PKG, name)] == (f"/data/local/tmp/{name}", mode)
        [call] = fake_device.attach_calls
        data = f"/data/user/0/{PKG}"
        assert call["so"] == f"{data}/{inject.NATIVE_SO_NAME}"
        assert (call["bootstrap"], call["payload"]) == (
            f"{data}/{inject.BOOTSTRAP_DEX_NAME}", f"{data}/{inject.PAYLOAD_JAR_NAME}")
        assert call["socket_name"] == f"viewspector_{PID}"
        assert call["result"] == "started generation 1"
        assert fake_device.settings == {"debug_view_attributes": "1"}
        assert fake_device.forward_names() == [f"viewspector_{PID}"]
        assert fake_device.commands() == ["hello"]
    finally:
        inj.close()
    assert fake_device.forward_names() == []
    assert not fake_device.unexpected


def test_warm_reconnect_reuses_the_running_agent(fake_device, warm_agent):
    inj = inject.inject_and_connect(serial=SERIAL, package=PKG)
    try:
        assert inj.warm and inj.package == PKG
        assert not fake_device.pushed and not fake_device.attach_calls
        assert fake_device.commands(generation=1) == ["hello"]
        assert warm_agent.open_connections == 1
    finally:
        inj.close()


def test_stale_socket_entry_falls_back_to_cold_inject(fake_device):
    # /proc/net/unix still lists the name, but nothing accepts on it.
    fake_device.foreign_sockets.append(f"viewspector_{PID}")
    inj = inject.inject_and_connect(serial=SERIAL, package=PKG)
    try:
        assert not inj.warm and len(fake_device.attach_calls) == 1
        assert fake_device.forward_names() == [f"viewspector_{PID}"]  # the probe's was removed
        assert fake_device.commands() == ["hello"]
    finally:
        inj.close()


def test_app_not_running_is_reported(fake_device):
    with pytest.raises(inject.InjectionError, match="'com.example.idle' is not running"):
        inject.inject_and_connect(serial=SERIAL, package="com.example.idle")
    assert not fake_device.pushed


def test_non_debuggable_app_is_reported(fake_device):
    with pytest.raises(inject.InjectionError, match="not debuggable"):
        inject.inject_and_connect(serial=SERIAL, package="com.example.release")


def test_non_debuggable_app_fails_fast_with_a_clear_error(fake_device, run_cli):
    res = run_cli("attach", "--package", "com.example.release")
    assert res.rc == 1
    assert "not debuggable" in res.err.splitlines()[0]
    assert not fake_device.pushed


def test_bad_serial_is_reported_as_a_missing_device(fake_device, run_cli):
    res = run_cli("attach", "--serial", "emulator-9999")
    assert res.rc == 1
    assert "emulator-9999" in res.err and "not running" not in res.err
    assert "not found" in res.err or "no device" in res.err.lower()


def test_socket_exists_is_an_exact_name_match(fake_device):
    fake_device.foreign_sockets.append(f"viewspector_{PID}1")
    assert adb.socket_exists(SERIAL, f"viewspector_{PID}") is False


def test_force_reinject_replaces_a_running_agent(fake_device, warm_agent, run_cli):
    assert run_cli("dump", "--force").rc == 0
    assert fake_device.wire[-1].generation == 2
    assert not warm_agent.running
    assert fake_device.commands(generation=1) == ["hello", "shutdown"]


def test_a_rebuilt_payload_replaces_the_running_agent(fake_device, warm_agent, run_cli, tmp_path):
    (tmp_path / "build-out" / inject.PAYLOAD_JAR_NAME).write_bytes(b"payload, rebuilt")
    res = run_cli("dump")
    assert res.rc == 0, res
    assert not warm_agent.running and fake_device.commands(generation=1) == ["hello", "shutdown"]
    new = fake_device.agent()
    assert new.generation == 2 and new.build_id == inject.local_build_id()
    assert fake_device.commands(generation=2) == ["hello", "dump_tree"]


def test_an_agent_from_before_the_build_handshake_is_replaced(fake_device, run_cli):
    legacy = fake_device.start_agent(PKG, build_id=None)
    legacy.reply_to_shutdown = False  # it also predates the shutdown-reply fix...
    legacy.linger_after_stop = True   # ...and the accept fix: stopping leaves the name bound
    res = run_cli("attach")
    assert res.rc == 0, res
    assert not legacy.running and not legacy.lingering and "(build " in res.out
    assert fake_device.agent().generation == 2
    assert [c.get("result") for c in fake_device.attach_calls] == ["started generation 2"]


def test_without_a_local_payload_any_running_agent_is_reused(fake_device, run_cli, tmp_path):
    fake_device.start_agent(PKG, build_id=None)
    (tmp_path / "build-out" / inject.PAYLOAD_JAR_NAME).unlink()
    res = run_cli("attach")
    assert res.rc == 0 and "(warm/reused)" in res.out
    assert not fake_device.attach_calls


def test_an_agent_that_ignores_shutdown_is_reported(fake_device, run_cli, monkeypatch, tmp_path):
    stubborn = fake_device.start_agent(PKG, build_id="0" * 64)
    stubborn.behaviour = lambda req: (
        (0, fakeagent.frame(stubborn.dispatch(req)))  # replies, but never stops
        if req.WhichOneof("command") == "shutdown" else stubborn.default_behaviour(req))
    monkeypatch.setattr(inject, "STOP_WAIT", 0.2)
    res = run_cli("dump")
    assert res.rc == 1 and "did not stop after SHUTDOWN" in res.err
    assert "am force-stop" in res.err and not fake_device.pushed


def test_a_stale_agent_still_holding_the_socket_after_inject_is_reported(fake_device, run_cli):
    stale = fake_device.start_agent(PKG, build_id="0" * 64)
    hellos = {"n": 0}

    def unreachable_first(req):  # the warm connect can't reach it, so it isn't stopped
        if req.WhichOneof("command") == "hello" and not hellos["n"]:
            hellos["n"] += 1
            return 0, "close"
        return stale.default_behaviour(req)

    stale.behaviour = unreachable_first
    res = run_cli("dump")
    assert res.rc == 1 and "is not the one just injected" in res.err
    assert [c.get("result") for c in fake_device.attach_calls] == ["already-bound"]


# =========================================================================== #
# 3. Every CLI subcommand.
# =========================================================================== #
def test_cli_devices(fake_device, run_cli):
    res = run_cli("devices")
    assert (res.rc, res.out) == (0, f"{SERIAL}\tdevice\n")
    assert fake_device.wire == []


def test_cli_packages_lists_only_debuggable_apps(fake_device, run_cli):
    res = run_cli("packages")
    assert res.rc == 0
    assert res.out.splitlines() == [PKG, "com.example.idle"]
    assert fake_device.wire == []


def test_cli_attach_cold(fake_device, run_cli):
    res = run_cli("attach")
    assert res.rc == 0, res
    build = fake_device.default_build_id[:12]
    assert (f"attached to {PKG} pid={PID}: agent viewspector-0.1 (build {build}), API 36, "
            f"abi arm64-v8a" in res.out)
    assert "warm" not in res.out and f"socket=@viewspector_{PID}" in res.out
    assert fake_device.commands() == ["hello"]  # the inject's Hello carries the metadata
    assert cold_injected(fake_device)
    assert fake_device.agent().running  # attach leaves the agent up...
    assert fake_device.forward_names() == []  # ...and removes its forward


def test_cli_attach_warm(fake_device, warm_agent, run_cli):
    res = run_cli("attach")
    assert res.rc == 0 and "(warm/reused)" in res.out
    assert not cold_injected(fake_device)


def test_cli_dump_text(fake_device, run_cli):
    res = run_cli("dump")
    assert res.rc == 0, res
    lines = res.out.splitlines()
    assert lines[0] == "DecorView (0,0 360x640) id=1001"
    assert '    TextView @id/title "Hello world" (16,24 328x40) id=1003' in lines
    assert "PopupDecorView (40,560 280x64) id=2001" in lines
    assert fake_device.commands() == ["hello", "dump_tree"]
    req = fake_device.requests("dump_tree")[-1]
    assert (req.root_id, req.include_properties, req.include_screenshot) == (0, False, False)


def test_cli_dump_json_with_properties_resolution_and_screenshot(fake_device, run_cli, tmp_path):
    shot = tmp_path / "tree.png"
    res = run_cli("dump", "--json", "-", "--resolution-stack", "--screenshot", shot,
                  "--scale", "0.5", "--root-id", "1001")
    assert res.rc == 0, res
    req = fake_device.requests("dump_tree")[-1]
    assert (req.root_id, req.include_properties, req.include_resolution_stack,
            req.include_screenshot) == (1001, True, True, True)
    assert req.screenshot_scale == 0.5
    data = res.json()
    assert [r["id"] for r in data["roots"]] == [1001]
    assert set(data["properties"]) == {str(i) for i in range(1001, 1007)}
    title = props_by_name(data["properties"]["1003"])
    assert title["text"]["source"] == "@style/Widget.App.Title"
    assert title["text"]["resolution_stack"][-1] == "@android:style/Widget.Material.TextView"
    assert data["screenshot"]["bitmap_type"] == 2
    assert png_size(shot) == (180, 320)


def test_cli_compose_json(fake_device, run_cli):
    res = run_cli("compose", "--json", "-", "--enable-inspection")
    assert res.rc == 0, res
    req = fake_device.requests("dump_compose")[-1]
    assert (req.include_semantics, req.include_slot_table, req.enable_inspection) == (True, True, True)
    [window] = res.json()["windows"]
    assert window["view_id"] == 1006
    assert find([window["root"]], id=2)["attrs"]["Role"] == "Button"
    assert find([window["root"]], name="SubmitButton")["render_node_id"] == 7002
    assert "found 1 AndroidComposeView(s)" in res.err


def test_cli_compose_text_without_slot_table(fake_device, run_cli):
    res = run_cli("compose", "--no-slot-table", "--no-enable-inspection")
    assert res.rc == 0, res
    req = fake_device.requests("dump_compose")[-1]
    assert (req.include_slot_table, req.enable_inspection) == (False, False)
    assert '"Submit" [Button] (16,176 200x56)' in res.out
    assert '"Wi-Fi" [Switch] (16,340 328x56)' in res.out


@needs_pil
def test_cli_compose_overlay(fake_device, run_cli, tmp_path):
    out = tmp_path / "compose.png"
    res = run_cli("compose", "--overlay", out, "--scale", "0.5")
    assert res.rc == 0, res
    assert fake_device.commands()[-2:] == ["dump_compose", "screenshot"]
    assert fake_device.requests("screenshot")[-1].scale == 0.5
    assert png_size(out) == (180, 320)
    assert not Path(f"{out}.base.png").exists()
    assert "(7 boxes, 4 labels)" in res.err


def test_cli_a11y_json(fake_device, run_cli):
    res = run_cli("a11y", "--json", "-")
    assert res.rc == 0, res
    req = fake_device.requests("dump_a11y")[-1]
    assert (req.root_id, req.include_extras, req.include_rendering_info) == (0, True, False)
    data = res.json()
    assert [w["root_view_id"] for w in data["windows"]] == [1001, 2001]
    submit = find([w["root"] for w in data["windows"]], virtual_id=2)
    assert submit["host_view_id"] == 1006 and "is_virtual" in submit["flags"]
    assert submit["extras"] == {"androidx.compose.ui.semantics.id": "2"}
    stops = [e.get("speakable") for e in data["focus_order"] if e.get("is_focus_stop")]
    assert stops[:3] == ["Hello world", "OK", "Submit"]


def test_cli_a11y_reading_order_text_and_flags(fake_device, run_cli):
    res = run_cli("a11y", "--no-extras", "--rendering-info")
    assert res.rc == 0, res
    req = fake_device.requests("dump_a11y")[-1]
    assert (req.include_extras, req.include_rendering_info) == (False, True)
    assert "  3. Submit (16,176 200x56)" in res.out.splitlines()


@needs_pil
def test_cli_a11y_overlay_with_lint(fake_device, run_cli, tmp_path):
    out = tmp_path / "a11y.png"
    res = run_cli("a11y", "--overlay", out, "--lint")
    assert res.rc == 0, res
    assert fake_device.commands()[1:] == ["dump_a11y", "dump_compose", "screenshot", "screenshot"]
    assert {"wm density", "settings get system font_scale"} <= set(fake_device.shell_log())
    assert png_size(out) == (360, 640)
    assert not Path(f"{out}.base.png").exists()


def test_cli_a11y_lint_json(fake_device, run_cli):
    res = run_cli("a11y-lint", "--json", "-")
    assert res.rc == 0, res
    req = fake_device.requests("dump_compose")[-1]
    assert (req.include_semantics, req.include_slot_table) == (True, False)
    assert fake_device.requests("screenshot")[-1].scale == 1.0
    data = res.json()
    assert (data["density"], data["font_scale"]) == (280, 1.3)  # override density wins
    by_rule = data["summary"]["by_rule"]
    assert by_rule["a11y.touch_target.small"] >= 1 and by_rule["a11y.label.missing"] == 1
    missing = [f for f in data["findings"] if f["rule"] == "a11y.label.missing"]
    assert [f["node"]["id"] for f in missing] == [6]


def test_cli_a11y_lint_rule_filter_without_contrast(fake_device, run_cli):
    res = run_cli("a11y-lint", "--json", "-", "--rule", "a11y.label.missing", "--no-contrast")
    assert res.rc == 0, res
    assert "screenshot" not in fake_device.commands()
    assert {f["rule"] for f in res.json()["findings"]} == {"a11y.label.missing"}


@needs_pil
def test_cli_a11y_lint_overlay(fake_device, run_cli, tmp_path):
    out = tmp_path / "lint.png"
    res = run_cli("a11y-lint", "--overlay", out)
    assert res.rc == 0, res
    assert "error," in res.out and png_size(out) == (360, 640)
    assert not Path(f"{out}.base.png").exists()


def test_cli_inspect_json(fake_device, run_cli):
    res = run_cli("inspect", "--json", "-")
    assert res.rc == 0, res
    assert fake_device.commands()[:4] == ["hello", "dump_tree", "dump_compose", "dump_a11y"]
    compose_req = fake_device.requests("dump_compose")[-1]
    assert (compose_req.include_slot_table, compose_req.enable_inspection) == (False, False)
    data = res.json()
    assert data["sources"] == {"view": True, "compose": True, "a11y": True}
    assert data["summary"]["nodes"] == 15 and data["summary"]["view"] == 8
    ok = find(data["roots"], node_key="view:1004")
    assert ok["correlation_confidence"] == "exact" and ok["a11y"]["text"] == "OK"
    host = find(data["roots"], node_key="view:1006")
    submit = find(host["children"], node_key="compose:2")
    assert submit["a11y"]["virtual_id"] == 2 and submit["correlation_confidence"] == "exact"


def test_cli_inspect_with_properties(fake_device, run_cli):
    res = run_cli("inspect", "--properties", "--json", "-")
    assert res.rc == 0, res
    assert fake_device.requests("dump_tree")[-1].include_properties
    title = find(res.json()["roots"], node_key="view:1003")
    assert props_by_name(title["view"]["properties"])["text"]["value"] == "Hello world"


@needs_pil
def test_cli_inspect_overlay(fake_device, run_cli, tmp_path):
    out = tmp_path / "integrated.png"
    res = run_cli("inspect", "--overlay", out)
    assert res.rc == 0, res
    assert png_size(out) == (360, 640) and "(15 boxes)" in res.err
    assert not Path(f"{out}.base.png").exists()
    assert json.loads(res.out)["nodes"] == 15


def test_cli_inspect_node(fake_device, run_cli):
    res = run_cli("inspect-node", "--node-key", "compose:5", "--no-image", "--json", "-")
    assert res.rc == 0, res
    dossier = res.json()
    assert dossier["compose"]["attrs"]["Role"] == "Switch"
    assert dossier["a11y"]["virtual_id"] == 5 and dossier["a11y"]["state_description"] == "On"
    assert {f["rule"] for f in dossier["lint"]} == {"a11y.touch_target.small"}
    assert "component_image" not in dossier
    assert fake_device.requests("dump_tree")[-1].include_properties
    assert "wm density" in fake_device.shell_log()


@needs_pil
def test_cli_inspect_node_by_bounds_with_image(fake_device, run_cli):
    res = run_cli("inspect-node", "--bounds", "20,90,4,4")
    assert res.rc == 0, res
    dossier = json.loads(res.out)
    assert dossier["node_key"] == "view:1004"
    img = dossier["component_image"]
    assert img["source"] == "bitmap_crop" and png_size(img["path"]) == (120, 48)


def test_cli_inspect_node_without_a_match(fake_device, run_cli):
    res = run_cli("inspect-node", "--view-id", "999999", "--no-image")
    assert res.rc == 1 and "no matching element" in res.err


@needs_pil
def test_cli_component_image(fake_device, run_cli, tmp_path):
    out = tmp_path / "ok.png"
    res = run_cli("component-image", "--view-id", "1004", "--out", out)
    assert res.rc == 0, res
    assert json.loads(res.out)["source"] == "bitmap_crop"
    assert png_size(out) == (120, 48)
    assert png_pixel(out, 5, 5) == OK_BUTTON_RGB  # ABGR decoded without an R/B swap


def test_cli_screenshot(fake_device, run_cli, tmp_path):
    out = tmp_path / "shot.png"
    res = run_cli("screenshot", "--out", out, "--scale", "0.5")
    assert res.rc == 0, res
    req = fake_device.requests("screenshot")[-1]
    assert (req.root_id, req.scale) == (0, 0.5)
    assert png_size(out) == (180, 320) and "wrote screenshot 180x320" in res.err
    if __import__("importlib").util.find_spec("PIL"):
        assert png_pixel(out, 20, 50) == OK_BUTTON_RGB


def test_cli_get_properties(fake_device, run_cli):
    res = run_cli("get-properties", "--view-id", "1003", "--json", "-")
    assert res.rc == 0, res
    req = fake_device.requests("get_properties")[-1]
    assert (req.view_id, req.include_resolution_stack) == (1003, False)
    data = res.json()
    assert data["view_id"] == 1003
    props = props_by_name(data["properties"])
    assert props["text"]["value"] == "Hello world"
    assert props["layout_gravity"]["is_layout"] is True
    assert props["id"]["value"] == {"namespace": PKG, "type": "id", "name": "title"}


def test_cli_get_properties_decodes_gravity_flags_and_dimensions(fake_device, run_cli):
    props = props_by_name(run_cli("get-properties", "--view-id", "1003", "--json", "-")
                          .json()["properties"])
    assert props["gravity"]["label"] == "center_vertical|start"
    assert props["inputType"]["label"] == "text|textCapSentences"
    assert props["paddingStart"]["value"] == 42
    assert props["layout_marginTop"]["value"] == 16


def test_cli_get_properties_unknown_view_surfaces_the_agent_error(fake_device, run_cli):
    res = run_cli("get-properties", "--view-id", "999")
    assert res.rc == 1 and "No view found with id 999" in res.err


def test_cli_detach_shuts_down_a_running_agent(fake_device, warm_agent, run_cli):
    res = run_cli("detach")
    assert res.rc == 0 and f"detached {PKG} on {SERIAL}" in res.err
    assert fake_device.commands(generation=1) == ["hello", "shutdown"]
    assert not warm_agent.running and fake_device.agent() is None
    assert fake_device.forward_names() == []


def test_cli_detach_without_an_agent_does_not_inject(fake_device, run_cli):
    assert run_cli("detach").rc == 0
    assert not fake_device.pushed and not cold_injected(fake_device)


CLI_SUBCOMMAND_TESTS = {
    "devices": test_cli_devices,
    "packages": test_cli_packages_lists_only_debuggable_apps,
    "attach": test_cli_attach_cold,
    "dump": test_cli_dump_text,
    "compose": test_cli_compose_json,
    "a11y": test_cli_a11y_json,
    "a11y-lint": test_cli_a11y_lint_json,
    "inspect": test_cli_inspect_json,
    "inspect-node": test_cli_inspect_node,
    "component-image": test_cli_component_image,
    "screenshot": test_cli_screenshot,
    "get-properties": test_cli_get_properties,
    "detach": test_cli_detach_shuts_down_a_running_agent,
}


def test_every_cli_subcommand_has_an_e2e_test():
    registered = next(a.choices for a in cli.build_parser()._actions
                      if isinstance(getattr(a, "choices", None), dict))
    assert set(registered) == set(CLI_SUBCOMMAND_TESTS), (
        f"no e2e test: {set(registered) - set(CLI_SUBCOMMAND_TESTS)}; "
        f"stale: {set(CLI_SUBCOMMAND_TESTS) - set(registered)}")


# =========================================================================== #
# 4. Every MCP tool, through mcp_server._run_tool.
# =========================================================================== #
def test_mcp_list_devices(mcp, fake_device):
    assert mcp("list_devices") == {"devices": [{
        "serial": SERIAL, "api": 36, "abi": "arm64-v8a", "model": "sdk_gphone64_arm64",
        "state": "device"}], "count": 1}
    assert fake_device.wire == []


def test_mcp_list_processes(mcp, fake_device):
    res = mcp("list_processes")
    assert res["processes"] == [
        {"package": PKG, "pid": PID, "running": True},
        {"package": "com.example.idle", "pid": None, "running": False},
    ]
    assert fake_device.wire == []


def test_mcp_attach(mcp, fake_device):
    res = mcp("attach")
    assert res["attached"] is True and res["window_count"] == 2
    assert res["session"] == f"{SERIAL}/{PKG}"
    assert fake_device.commands() == ["hello", "get_windows"]
    assert cold_injected(fake_device)
    assert mcp_server.SESSIONS.peek(SERIAL, PKG) is not None


def test_mcp_attach_reports_agent_metadata(mcp, fake_device):
    res = mcp("attach")
    assert (res["api_level"], res["abi"], res["agent_version"]) == (36, "arm64-v8a",
                                                                    "viewspector-0.1")


def test_mcp_session_is_cached_and_reused(mcp, fake_device):
    for tool in ("attach", "dump_tree", "screenshot", "get_properties"):
        res = mcp(tool, **({"view_id": 1003} if tool == "get_properties" else {}))
        assert "error" not in res, res
    assert fake_device.commands() == ["hello", "get_windows", "dump_tree", "screenshot",
                                      "get_properties"]
    assert len(fake_device.attach_calls) == 1 and fake_device.agent().open_connections == 1


def test_mcp_dump_tree(mcp, fake_device):
    res = mcp("dump_tree")
    assert res["root_count"] == 2 and "properties" not in res and "screenshot" not in res
    title = find(res["roots"], id=1003)
    assert title["resource"]["ref"] == "@id/title" and title["text"] == "Hello world"
    assert title["bounds"] == {"x": 16, "y": 24, "w": 328, "h": 40}
    req = fake_device.requests("dump_tree")[-1]
    assert (req.root_id, req.include_properties, req.include_screenshot) == (0, False, False)


def test_mcp_dump_tree_with_properties_and_screenshot(mcp, fake_device):
    res = mcp("dump_tree", include_properties=True, include_screenshot=True, scale=0.5,
              root_id=1001)
    assert "error" not in res, res
    req = fake_device.requests("dump_tree")[-1]
    assert (req.root_id, req.include_properties, req.include_screenshot) == (1001, True, True)
    assert req.screenshot_scale == 0.5
    assert [g["view_id"] for g in res["properties"]] == list(range(1001, 1007))
    shot = res["screenshot"]
    assert (shot["width"], shot["height"], shot["scale"]) == (180, 320, 0.5)
    assert fake_device.tmpdir in Path(shot["path"]).parents and png_size(shot["path"]) == (180, 320)


def test_mcp_get_properties(mcp, fake_device):
    res = mcp("get_properties", view_id=1003)
    req = fake_device.requests("get_properties")[-1]
    assert (req.view_id, req.include_resolution_stack) == (1003, False)
    props = props_by_name(res["group"]["properties"])
    assert props["text"]["value"] == "Hello world"
    assert props["textColor"]["value"] == "#FF202124"
    assert props["textSize"]["value"] == 42.0
    assert props["enabled"]["value"] is True
    assert props["visibility"]["value"] == "visible"
    assert props["id"]["value"]["ref"] == "@id/title"
    assert props["layout_gravity"]["is_layout"] is True


@pytest.mark.xfail(strict=True, reason="E3: the MCP property decoder reads GRAVITY/INT_FLAG as "
                   "int32 and DIMENSION as float (the agent sends str_value / int32_value)")
def test_mcp_get_properties_decodes_gravity_flags_and_dimensions(mcp, fake_device):
    props = props_by_name(mcp("get_properties", view_id=1003)["group"]["properties"])
    assert "center_vertical|start" in (props["gravity"].get("value"), props["gravity"].get("label"))
    assert "text|textCapSentences" in (props["inputType"].get("value"),
                                       props["inputType"].get("label"))
    assert props["paddingStart"]["value"] == 42
    assert props["layout_marginTop"]["value"] == 16


def test_mcp_get_properties_unknown_view(mcp, fake_device):
    res = mcp("get_properties", view_id=999)
    assert res["tool"] == "get_properties" and "No view found with id 999" in res["error"]


def test_mcp_get_properties_needs_a_view_id_before_touching_the_device(mcp, fake_device):
    assert mcp("get_properties") == {"error": "missing required argument: view_id",
                                     "tool": "get_properties"}
    assert fake_device.adb_log == [] and fake_device.wire == []


def test_mcp_screenshot(mcp, fake_device):
    res = mcp("screenshot", scale=0.5)
    assert (res["width"], res["height"], res["scale"]) == (180, 320, 0.5)
    req = fake_device.requests("screenshot")[-1]
    assert (req.root_id, req.scale) == (0, 0.5)
    assert png_size(res["path"]) == (180, 320)
    if __import__("importlib").util.find_spec("PIL"):
        assert png_pixel(res["path"], 20, 50) == OK_BUTTON_RGB


@pytest.mark.parametrize("scale,wire", [(0, 1.0), (0.25, 0.25), (1, 1.0)])
def test_mcp_screenshot_scale_is_clamped(mcp, fake_device, scale, wire):
    assert "error" not in mcp("screenshot", scale=scale)
    assert fake_device.requests("screenshot")[-1].scale == wire


def test_mcp_scale_out_of_schema_range_is_rejected_before_touching_the_device(mcp, fake_device):
    res = mcp("screenshot", scale=5)
    assert res == {"error": "invalid argument scale: 5 is greater than the maximum of 1.0",
                   "tool": "screenshot"}
    assert fake_device.adb_log == [] and fake_device.wire == []


def test_mcp_dump_compose(mcp, fake_device):
    res = mcp("dump_compose", enable_inspection=True)
    req = fake_device.requests("dump_compose")[-1]
    assert (req.include_semantics, req.include_slot_table, req.enable_inspection) == (True, True, True)
    [window] = res["windows"]
    assert window["view_id"] == 1006
    assert find([window["root"]], name="SubmitButton")["source"] == "MainActivity.kt:42"


def test_mcp_dump_compose_without_inspection_reports_an_empty_slot_table(mcp, fake_device):
    res = mcp("dump_compose", enable_inspection=False)
    assert "slot table empty" in res["diagnostics"]
    assert fake_device.requests("dump_compose")[-1].enable_inspection is False


@needs_pil
def test_mcp_compose_overlay(mcp, fake_device):
    res = mcp("compose_overlay")
    assert "error" not in res, res
    assert fake_device.commands()[-2:] == ["dump_compose", "screenshot"]
    assert fake_device.requests("dump_compose")[-1].include_slot_table is False
    assert (res["boxes"], res["labels"], res["size"]) == (7, 4, [360, 640])
    assert [s["text"] for s in res["on_screen"]] == ["Submit", "Settings", "Wi-Fi"]
    assert png_size(res["path"]) == (360, 640)
    assert leftovers(fake_device.tmpdir, "compose_base") == []


def test_mcp_dump_accessibility(mcp, fake_device):
    res = mcp("dump_accessibility", include_rendering_info=True)
    req = fake_device.requests("dump_a11y")[-1]
    assert (req.include_extras, req.include_rendering_info) == (True, True)
    assert [w["root_view_id"] for w in res["windows"]] == [1001, 2001]
    wifi = find([w["root"] for w in res["windows"]], virtual_id=5)
    assert wifi["state_description"] == "On" and wifi["text_size_px"] == 42.0
    assert res["focus_order"]


def test_mcp_a11y_lint(mcp, fake_device):
    res = mcp("a11y_lint")
    assert (res["density"], res["font_scale"], res["contrast_sampled"]) == (280, 1.3, True)
    assert fake_device.commands()[-2:] == ["dump_compose", "screenshot"]
    assert res["summary"]["by_rule"]["a11y.label.missing"] == 1
    assert {f["node"]["id"] for f in res["findings"]} >= {2, 5, 6}


def test_mcp_a11y_lint_rule_subset_without_contrast(mcp, fake_device):
    res = mcp("a11y_lint", rules=["a11y.label.missing"], include_contrast=False)
    assert res["contrast_sampled"] is False and "screenshot" not in fake_device.commands()
    assert {f["rule"] for f in res["findings"]} == {"a11y.label.missing"}


@needs_pil
def test_mcp_a11y_overlay(mcp, fake_device):
    res = mcp("a11y_overlay")
    assert "error" not in res, res
    assert fake_device.commands()[1:4] == ["dump_a11y", "dump_compose", "screenshot"]
    assert res["finding_count"] == res["summary"]["total"] > 0
    assert png_size(res["path"]) == (360, 640)
    assert leftovers(fake_device.tmpdir, "a11y_base") == []


@needs_pil
@pytest.mark.xfail(strict=True, reason="A2: overlay severity colours never show (findings carry "
                   "semantics ids, the overlay looks nodes up by packed a11y id)")
def test_mcp_a11y_overlay_colours_the_flagged_nodes(mcp, fake_device):
    res = mcp("a11y_overlay")
    assert res["flagged"] > 0


def test_mcp_inspect(mcp, fake_device):
    res = mcp("inspect")
    assert fake_device.commands()[1:] == ["dump_tree", "dump_compose", "dump_a11y"]
    assert res["summary"]["nodes"] == 15 and res["sources"]["a11y"] is True
    host = find(res["roots"], node_key="view:1006")
    assert find(host["children"], node_key="compose:6")["a11y"]["virtual_id"] == 6


@needs_pil
def test_mcp_inspect_overlay(mcp, fake_device):
    res = mcp("inspect", include_overlay=True)
    assert "overlay_error" not in res, res
    assert res["overlay"]["boxes"] == 15 and png_size(res["overlay"]["path"]) == (360, 640)
    assert leftovers(fake_device.tmpdir, "integrated_base") == []


@needs_pil
def test_mcp_inspect_node(mcp, fake_device):
    res = mcp("inspect_node", view_id=1004)
    assert res["node_key"] == "view:1004" and res["correlation_confidence"] == "exact"
    assert props_by_name(res["view"]["properties"])["text"]["value"] == "OK"
    assert res["a11y"]["text"] == "OK"
    assert res["component_image"]["source"] == "bitmap_crop"
    assert png_size(res["component_image"]["path"]) == (120, 48)
    assert fake_device.requests("dump_tree")[-1].include_properties


@pytest.mark.parametrize("selector,key", [
    ({"node_key": "compose:2"}, "compose:2"),
    ({"semantics_id": 5}, "compose:5"),
    ({"bounds": {"x": 20, "y": 90, "w": 4, "h": 4}}, "view:1004"),
])
def test_mcp_inspect_node_selectors(mcp, fake_device, selector, key):
    res = mcp("inspect_node", include_image=False, **selector)
    assert res["node_key"] == key, res


def test_mcp_inspect_node_focused_lint(mcp, fake_device):
    res = mcp("inspect_node", node_key="compose:2", include_image=False)
    assert {f["rule"] for f in res["lint"]} == {"a11y.touch_target.small"}
    assert all(f["node"]["id"] == 2 for f in res["lint"])


def test_mcp_inspect_node_needs_a_selector_before_touching_the_device(mcp, fake_device):
    res = mcp("inspect_node")
    assert "needs one of" in res["error"] and fake_device.wire == []


def test_mcp_inspect_node_without_a_match(mcp, fake_device):
    assert "no matching element" in mcp("inspect_node", view_id=999, include_image=False)["error"]


@needs_pil
def test_mcp_component_image(mcp, fake_device):
    res = mcp("component_image", view_id=1004)
    assert (res["source"], res["node_key"]) == ("bitmap_crop", "view:1004")
    assert png_size(res["path"]) == (120, 48)
    assert png_pixel(res["path"], 5, 5) == OK_BUTTON_RGB


@pytest.mark.xfail(strict=True, reason="NEW-SKP-UNREACHABLE: correlate fetches Compose with "
                   "include_slot_table=False and only slot-table nodes carry render_node_id, so "
                   "component_image never takes the SKP path (CAPTURE_SKP is never sent)")
def test_mcp_component_image_of_a_compose_layer_tries_skp(mcp, fake_device):
    mcp("component_image", node_key="compose:2")
    assert "capture_skp" in fake_device.commands()


def test_mcp_detach(mcp, fake_device):
    assert mcp("attach")["attached"]
    agent = fake_device.agent()
    assert mcp("detach") == {"serial": SERIAL, "package": PKG, "detached": True,
                             "agent_stopped": True}
    assert fake_device.commands()[-1] == "shutdown" and not agent.running
    assert fake_device.forward_names() == [] and mcp_server.SESSIONS.peek(SERIAL, PKG) is None
    assert mcp("detach")["detached"] is False
    # The next call re-injects a fresh agent.
    assert mcp("dump_tree")["root_count"] == 2
    assert len(fake_device.attach_calls) == 2 and fake_device.wire[-1].generation == 2


MCP_TOOL_TESTS = {
    "list_devices": test_mcp_list_devices,
    "list_processes": test_mcp_list_processes,
    "attach": test_mcp_attach,
    "dump_tree": test_mcp_dump_tree,
    "get_properties": test_mcp_get_properties,
    "screenshot": test_mcp_screenshot,
    "dump_compose": test_mcp_dump_compose,
    "compose_overlay": test_mcp_compose_overlay,
    "dump_accessibility": test_mcp_dump_accessibility,
    "a11y_lint": test_mcp_a11y_lint,
    "a11y_overlay": test_mcp_a11y_overlay,
    "inspect": test_mcp_inspect,
    "inspect_node": test_mcp_inspect_node,
    "component_image": test_mcp_component_image,
    "detach": test_mcp_detach,
}


def test_every_mcp_tool_has_an_e2e_test():
    assert set(MCP_TOOL_TESTS) == set(mcp_server.TOOLS), (
        f"no e2e test: {set(mcp_server.TOOLS) - set(MCP_TOOL_TESTS)}")


# =========================================================================== #
# 5. Scenarios.
# =========================================================================== #
def test_scenario_cold_then_warm_across_cli_runs(fake_device, run_cli):
    assert run_cli("attach").rc == 0
    assert run_cli("dump").rc == 0
    assert len(fake_device.attach_calls) == 1
    assert {w.generation for w in fake_device.wire} == {1}


def test_scenario_agent_drops_clients_then_cli_reconnects(fake_device, warm_agent, run_cli):
    assert run_cli("dump").rc == 0
    fake_device.kill_clients()
    res = run_cli("dump")
    assert res.rc == 0 and "id=1003" in res.out
    assert not cold_injected(fake_device)


def test_scenario_agent_drops_clients_then_mcp_recovers(mcp, fake_device):
    assert mcp("attach")["attached"]
    fake_device.kill_clients()
    res = mcp("dump_tree")
    assert "error" not in res and res["root_count"] == 2


def test_scenario_idle_timeout_then_mcp_reattaches(mcp, fake_device):
    assert mcp("attach")["attached"]
    fake_device.idle_timeout()
    res = mcp("screenshot")
    assert "error" not in res, res
    assert len(fake_device.attach_calls) == 2


def test_scenario_mcp_attach_after_the_agent_dropped_is_live(mcp, fake_device):
    assert mcp("attach")["window_count"] == 2
    fake_device.kill_clients()
    assert mcp("attach")["window_count"] == 2


def test_scenario_app_restart_then_mcp_reinjects(mcp, fake_device):
    assert mcp("attach")["attached"]
    fake_device.restart_app(new_pid=5353)
    res = mcp("dump_tree")
    assert "error" not in res, res
    assert fake_device.attach_calls[-1]["socket_name"] == "viewspector_5353"


def test_scenario_hung_agent_times_out(mcp, fake_device, monkeypatch):
    monkeypatch.setenv("INSPECTOR_WIDGET_TIMEOUT", "1")
    assert mcp("attach")["attached"]
    hang_on(fake_device.agent(), "get_properties")
    result = {}
    threads = [threading.Thread(target=lambda: result.update(mcp("get_properties", view_id=1003)),
                                daemon=True)]
    threads[0].start()
    try:
        threads[0].join(3)
        assert not threads[0].is_alive(), "get_properties against a hung agent never returned"
        assert "error" in result
        threads.append(threading.Thread(target=lambda: mcp("detach"), daemon=True))
        threads[1].start()
        threads[1].join(3)
        assert not threads[1].is_alive(), "detach blocked behind the hung request"
    finally:
        # Unwedge the host threads while the adb patch is still active, so no
        # straggler reaches the real adb after teardown.
        fake_device.close()
        for t in threads:
            t.join(10)


def test_scenario_force_on_a_raw_client_subcommand_reinjects(fake_device, warm_agent, run_cli):
    res = run_cli("dump", "--force")
    assert res.rc == 0, res
    assert set(fake_device.pushed) == {f"/data/local/tmp/{n}" for n in ARTIFACTS}
    assert [c.get("result") for c in fake_device.attach_calls] == ["started generation 2"]
    assert not warm_agent.running


def test_scenario_force_on_an_integrated_subcommand_reinjects(fake_device, warm_agent, run_cli,
                                                              tmp_path):
    assert run_cli("screenshot", "--force", "--out", tmp_path / "s.png").rc == 0
    assert fake_device.pushed and fake_device.attach_calls


def test_scenario_raw_cli_command_leaves_a_live_mcp_session_alone(mcp, fake_device, run_cli):
    assert mcp("attach")["attached"]
    assert run_cli("dump").rc == 0
    res = mcp("dump_tree")
    assert "error" not in res and res["root_count"] == 2
    assert fake_device.agent().generation == 1


def test_scenario_integrated_cli_command_leaves_a_live_mcp_session_alone(mcp, fake_device,
                                                                         run_cli, tmp_path):
    assert mcp("attach")["attached"]
    assert run_cli("screenshot", "--out", tmp_path / "s.png").rc == 0
    res = mcp("dump_tree")
    assert "error" not in res, res


@pytest.mark.xfail(strict=True, reason="E3: CLI and MCP decode the same properties differently "
                   "(GRAVITY/INT_FLAG/DIMENSION)")
def test_scenario_property_decoding_parity_cli_vs_mcp(mcp, fake_device, run_cli):
    cli_props = props_by_name(run_cli("get-properties", "--view-id", "1003", "--json", "-")
                              .json()["properties"])
    mcp_props = props_by_name(mcp("get_properties", view_id=1003)["group"]["properties"])
    for name in ("gravity", "inputType", "layout_gravity", "paddingStart", "layout_marginTop",
                 "textSize", "text", "visibility", "enabled"):
        assert decoded(cli_props[name]) == decoded(mcp_props[name]), name


@pytest.mark.xfail(strict=True, reason="E3: dump --properties (CLI) and dump_tree "
                   "include_properties (MCP) disagree on GRAVITY/DIMENSION values")
def test_scenario_dump_tree_property_parity_cli_vs_mcp(mcp, fake_device, run_cli):
    cli_props = props_by_name(run_cli("dump", "--properties", "--json", "-")
                              .json()["properties"]["1003"])
    groups = mcp("dump_tree", include_properties=True)["properties"]
    mcp_props = props_by_name(next(g for g in groups if g["view_id"] == 1003)["properties"])
    for name in ("gravity", "paddingStart", "layout_marginTop"):
        assert decoded(cli_props[name]) == decoded(mcp_props[name]), name


def test_scenario_duplicate_reply_does_not_desync_forever(mcp, fake_device, warm_agent):
    sent = {"dup": False}

    def duplicate_first_dump(req):
        resp = warm_agent.dispatch(req)
        if req.WhichOneof("command") == "dump_tree" and not sent["dup"]:
            sent["dup"] = True
            return 0, fakeagent.frame(resp) * 2
        return 0, resp

    warm_agent.behaviour = duplicate_first_dump
    assert mcp("dump_tree")["root_count"] == 2
    mcp("screenshot")  # may fail: it reads the stale duplicate
    res = mcp("screenshot")
    assert "error" not in res, res


def test_scenario_agent_error_with_id_zero_surfaces_its_message(mcp, fake_device, warm_agent):
    warm_agent.behaviour = lambda req: (
        (0, fakeagent.error_response(0, "Malformed request: boom"))
        if req.WhichOneof("command") == "get_properties" else warm_agent.default_behaviour(req))
    assert "Malformed request: boom" in mcp("get_properties", view_id=1003)["error"]


def test_scenario_garbage_from_the_agent_is_a_tool_error_not_a_crash(mcp, fake_device, warm_agent):
    warm_agent.behaviour = lambda req: (
        (0, b"NOTMAGIC" + struct.pack(">I", 0))
        if req.WhichOneof("command") == "screenshot" else warm_agent.default_behaviour(req))
    res = mcp("screenshot")
    assert res["tool"] == "screenshot" and "bad framing magic" in res["error"]


def test_scenario_second_window_is_addressable_by_root_id(mcp, fake_device):
    res = mcp("dump_tree", root_id=2001)
    assert [r["id"] for r in res["roots"]] == [2001]
    assert find(res["roots"], id=2002)["text"] == "Saved"


def test_scenario_failed_cli_overlay_leaves_no_base_png(fake_device, run_cli, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("overlay renderer failed")

    monkeypatch.setattr(overlaymod, "render_compose_overlay", boom)
    out = tmp_path / "compose.png"
    res = run_cli("compose", "--overlay", out)
    assert res.rc == 1 and "overlay renderer failed" in res.err
    assert not Path(f"{out}.base.png").exists()


def test_scenario_failed_mcp_overlay_leaves_no_temp_files(mcp, fake_device, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("overlay renderer failed")

    monkeypatch.setattr(overlaymod, "render_compose_overlay", boom)
    assert "overlay renderer failed" in mcp("compose_overlay")["error"]
    assert leftovers(fake_device.tmpdir, "viewspector_") == []


# =========================================================================== #
# 6. The MCP stdio transport, end to end, in a child process.
# =========================================================================== #
def _stdio_session(tmp_path, block_mcp, calls):
    log = tmp_path / "wire.jsonl"
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    cmd = [sys.executable, str(Path(__file__).with_name("fakeagent.py")), "--mcp-child",
           str(log), str(tmp_path / "build-out")] + (["--block-mcp"] if block_mcp else [])
    env = dict(os.environ, PYTHONPATH=str(HOST_DIR), TMPDIR=str(tmp))
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env, cwd=str(HOST_DIR))
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ] + [{"jsonrpc": "2.0", "id": i, "method": "tools/call",
          "params": {"name": name, "arguments": dict(args, serial=SERIAL, package=PKG)}}
         for i, (name, args) in enumerate(calls, start=2)]
    wanted = {m["id"] for m in messages if "id" in m}
    timer = threading.Timer(60, proc.kill)
    timer.start()
    by_id = {}
    try:
        # One call at a time, like an agent, so the wire order is deterministic.
        for m in messages:
            proc.stdin.write(json.dumps(m) + "\n")
            proc.stdin.flush()
            while "id" in m and m["id"] not in by_id:
                line = proc.stdout.readline()
                if not line:
                    break
                if line.strip().startswith("{"):
                    msg = json.loads(line)
                    if "id" in msg:
                        by_id[msg["id"]] = msg
    finally:
        timer.cancel()
        proc.stdin.close()
        proc.wait(timeout=30)
        stderr = proc.stderr.read()
        proc.stderr.close()
        proc.stdout.close()
    assert wanted <= set(by_id), f"missing responses; stderr:\n{stderr[-3000:]}"
    records = [json.loads(line) for line in log.read_text().splitlines()]
    results = {i: by_id[i]["result"] for i in range(2, len(calls) + 2)}
    return results, [r for r in records if r["event"] == "request"], \
        [r for r in records if r["event"] == "exit"]


def _payload(result):
    return json.loads(result["content"][0]["text"])


@pytest.mark.parametrize("transport", ["sdk", "fallback"])
def test_stdio_invalid_arguments_get_the_same_error_on_every_transport(tmp_path, transport):
    if transport == "sdk":
        pytest.importorskip("mcp")
    results, wire, _exits = _stdio_session(tmp_path, transport == "fallback", [
        ("screenshot", {"scale": 5}),
        ("get_properties", {"view_id": "1003"}),
        ("attach", {"bogus": True}),
    ])
    assert [r["isError"] for r in results.values()] == [True, True, True]
    assert [_payload(r)["error"] for r in results.values()] == [
        "invalid argument scale: 5 is greater than the maximum of 1.0",
        "invalid argument view_id: '1003' is not of type 'integer'",
        "unknown argument(s): bogus (allowed: force, package, serial)",
    ]
    assert wire == []  # rejected before anything reached the device


@pytest.mark.parametrize("transport", ["sdk", "fallback"])
def test_stdio_transport_end_to_end(tmp_path, transport):
    if transport == "sdk":
        pytest.importorskip("mcp")
    results, wire, exits = _stdio_session(tmp_path, transport == "fallback", [
        ("attach", {}),
        ("dump_tree", {"include_properties": True}),
        ("get_properties", {"view_id": 1003}),
        ("get_properties", {"view_id": 999}),
        ("detach", {}),
    ])
    attach, tree, props, missing, detach = (results[i] for i in range(2, 7))
    assert _payload(attach)["window_count"] == 2 and attach["isError"] is False
    assert _payload(tree)["root_count"] == 2 and len(_payload(tree)["properties"]) == 8
    assert _payload(props)["group"]["view_id"] == 1003
    assert missing["isError"] is True
    assert "No view found with id 999" in _payload(missing)["error"]
    assert _payload(detach)["detached"] is True
    assert [r["command"] for r in wire] == ["hello", "get_windows", "dump_tree", "get_properties",
                                           "get_properties", "shutdown"]
    assert wire[2]["request"]["dump_tree"]["include_properties"] is True
    assert wire[3]["request"]["get_properties"]["view_id"] == "1003"  # int64 -> JSON string
    [exit_record] = exits
    assert exit_record["forwards"] == [] and exit_record["running_agents"] == []


def test_stdio_server_exit_removes_its_adb_forwards(tmp_path):
    pytest.importorskip("mcp")
    results, wire, exits = _stdio_session(tmp_path, False, [("attach", {})])
    assert _payload(results[2])["attached"] is True
    [exit_record] = exits
    assert exit_record["forwards"] == []
