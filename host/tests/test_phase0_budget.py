"""Phase-0 size targets (spec section 2.6), through the real tool bodies.

Every call goes through ``mcp_server._call_tool_text`` (and the CLI's ``--json -``)
over the harness fake adb and agent, serving the recorded launcher and View-screen
replays and the 259-view wide scene. The sizes are the compact bytes an agent
receives with default arguments.
"""

from __future__ import annotations

import contextlib
import io
import json
import os

import pytest
import record_goldens as rg

#: (scene, tool, args, CLI argv, target bytes): spec section 2.6 / WP P0-2.
TARGETS = [
    ("launcher", "dump_compose", {}, ["compose"], 24_000),
    ("launcher", "dump_compose", {"include_slot_table": False}, ["compose", "--no-slot-table"],
     6_000),
    ("launcher", "inspect", {}, ["inspect"], 13_000),
    ("launcher", "dump_accessibility", {}, ["a11y"], 12_500),
    ("launcher", "a11y_lint", {}, ["a11y-lint"], 1_200),
    ("launcher", "dump_tree", {"include_properties": True}, ["dump", "--properties"], 8_000),
    ("launcher", "get_properties", {"view_id": 82}, ["get-properties", "--view-id", "82"], 3_500),
    ("viewscreen", "inspect", {}, ["inspect"], 18_000),
    ("viewscreen", "dump_tree", {"include_properties": True}, ["dump", "--properties"], 20_000),
    ("wide", "dump_tree", {"max_depth": 1}, ["dump", "--max-depth", "1"], 2_000),
]

#: Every tool with default arguments (and the ids it needs), per scene.
DEFAULT_CALLS = [(name, tool, args) for name, tool, args in rg.mcp_calls("default")
                 if name not in ("dump_tree_props", "detach")]


def _mcp_text(scene: str, tmp: str, calls) -> dict:
    import mcp_server

    out = {}
    with rg.harness(scene, tmp):
        for key, tool, args in calls:
            call = dict(args)
            if tool not in ("list_devices", "list_processes"):
                call.update(serial=rg.SERIAL, package=rg.PACKAGE)
            out[key] = mcp_server._call_tool_text(tool, call)
    return out


def _cli_text(scene: str, tmp: str, argvs) -> dict:
    import cli

    out = {}
    with rg.harness(scene, tmp):
        for key, argv in argvs:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                rc = cli.main(list(argv))
            out[key] = (buf.getvalue().rstrip("\n"), rc)
    return out


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


@pytest.mark.parametrize("scene", ["launcher", "viewscreen", "wide"])
def test_the_section_2_6_targets(scene, tmp_path):
    rows = [(i, t) for i, t in enumerate(TARGETS) if t[0] == scene]
    mcp = _mcp_text(scene, str(tmp_path / "mcp"), [(i, t[1], t[2]) for i, t in rows])
    cli = _cli_text(scene, str(tmp_path / "cli"),
                    [(i, [*t[3], "--json", "-"]) for i, t in rows])
    for i, (_, tool, args, argv, target) in rows:
        text, is_error = mcp[i]
        assert not is_error, text[:300]
        assert _size(text) <= target, (scene, tool, args, _size(text), target)
        assert not json.loads(text).get("truncated"), (scene, tool, args)  # the brief result
        cli_text, rc = cli[i]
        assert rc == 0 and _size(cli_text) <= target, (scene, argv, _size(cli_text), target)


@pytest.mark.parametrize("scene", rg.SCENES)
def test_every_default_response_is_within_32000_bytes(scene, tmp_path):
    calls = [(n, t, dict(a, **({"view_id": rg.VIEW_ID[scene]} if "view_id" in a else {})))
             for n, t, a in DEFAULT_CALLS]
    for name, (text, is_error) in _mcp_text(scene, str(tmp_path), calls).items():
        assert not is_error, (scene, name, text[:300])
        assert _size(text) <= 32_000, (scene, name, _size(text))
        if json.loads(text).get("truncated"):
            assert _size(text) <= 3_000, (scene, name, _size(text))


def test_oversize_results_become_envelopes_whose_spill_file_is_the_brief_result(tmp_path):
    """The 259-view screen: dump_tree, dump_accessibility and inspect do not fit."""
    import mcp_server
    from inspector_widget import output

    tools = ("dump_tree", "dump_accessibility", "inspect")
    with rg.harness("wide", str(tmp_path)):
        for tool in tools:
            args = {"serial": rg.SERIAL, "package": rg.PACKAGE}
            text, is_error = mcp_server._call_tool_text(tool, args)
            env = json.loads(text)
            assert not is_error and env["truncated"] is True, tool
            assert _size(text) <= 3_000 and env["tool"] == tool
            assert env["summary"]["nodes"] >= 259 and env["preview"], tool
            assert env["spill_path"].startswith(str(tmp_path / "store" / "spill")), env
            with open(env["spill_path"], encoding="utf-8") as f:
                spilled = json.load(f)
            full = mcp_server._run_tool(tool, args)
            assert spilled == json.loads(output.dumps(output.slim(tool, full, args)))
            assert env["bytes"] == os.path.getsize(env["spill_path"])


@pytest.mark.parametrize("max_bytes", [1_000, 1_500, 3_000, 8_000])
def test_an_envelope_never_exceeds_max_bytes(max_bytes, tmp_path):
    import mcp_server

    with rg.harness("launcher", str(tmp_path)):
        for tool in ("dump_compose", "inspect", "dump_accessibility"):
            text, is_error = mcp_server._call_tool_text(
                tool, {"serial": rg.SERIAL, "package": rg.PACKAGE, "max_bytes": max_bytes})
            assert not is_error and _size(text) <= max_bytes, (tool, max_bytes, _size(text))
            assert json.loads(text)["truncated"] is True


def test_cli_json_file_never_spills_and_stdout_does(tmp_path):
    out = tmp_path / "whole.json"
    res = _cli_text("wide", str(tmp_path / "h"), [
        ("file", ["a11y", "--json", str(out)]), ("stdout", ["a11y", "--json", "-"])])
    whole = json.loads(out.read_text(encoding="utf-8"))
    assert res["file"] == ("", 0) and "truncated" not in whole
    assert out.stat().st_size > 32_000 and len(whole["windows"][0]["root"]["children"]) == 6
    env = json.loads(res["stdout"][0])
    assert env["truncated"] is True and _size(res["stdout"][0]) <= 3_000
    with open(env["spill_path"], encoding="utf-8") as f:
        assert json.load(f) == whole  # the spill file is the same brief document


def test_max_bytes_env_and_zero(tmp_path, monkeypatch):
    import mcp_server

    with rg.harness("launcher", str(tmp_path)):
        args = {"serial": rg.SERIAL, "package": rg.PACKAGE}
        monkeypatch.setenv("INSPECTOR_WIDGET_MAX_BYTES", "5000")
        text, _ = mcp_server._call_tool_text("inspect", args)
        assert json.loads(text)["truncated"] and json.loads(text)["max_bytes"] == 5000
        text, _ = mcp_server._call_tool_text("inspect", dict(args, max_bytes=0))
        assert "truncated" not in json.loads(text) and _size(text) > 5000


def test_tools_list_stays_under_18500_bytes():
    """Spec section 2.4: the output parameters must not blow up every session's
    tool list (compact tools/list, as the fallback transport sends it)."""
    import mcp_server

    tools = mcp_server._fallback_handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    text = json.dumps(tools["result"], separators=(",", ":"), ensure_ascii=False)
    assert _size(text) <= 18_500, _size(text)
    # the default toolset lists the 15 legacy and 3 TalkBack tools; the 8
    # capture-and-walk tools are registered too (callable by name) but not listed
    assert len(tools["result"]["tools"]) == 18 and len(mcp_server.TOOLS) == 26
