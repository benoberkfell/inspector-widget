"""CLI <-> MCP parity for the Phase-0 output layer (spec sections 2.3 and 11).

Until the surface registry exists (S2) this is the hand map: every output
parameter of ``output.OUTPUT_PARAMS`` is in the MCP tool's schema and is a
kebab-case flag with the same default on the subcommand ``output.CLI_SUBCOMMANDS``
maps the tool to; and for the same arguments, ``--json -`` prints the bytes the
MCP tool returns, over the harness fake adb and agent.

``detail="full"`` is the one exception by design: it is each surface's own
pre-Phase-0 document (the rollback, pinned by ``test_legacy_golden``), and those
differed (the MCP adds ``serial``/``package`` and wraps a few results).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re

import pytest
import record_goldens as rg

import cli
import mcp_server
from inspector_widget import output

#: Subcommands that print a JSON document (and so take --pretty).
JSON_SUBCOMMANDS = {"dump", "get-properties", "compose", "a11y", "a11y-lint", "inspect",
                    "inspect-node", "component-image", "talkback", "tb-walk", "tb-scenario"}


def _subparsers() -> dict:
    for action in cli.build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("no subcommands")


def _defaults(sub: argparse.ArgumentParser) -> dict:
    return {a.dest: a.default for a in sub._actions}


# --------------------------------------------------------------------------- params
def test_every_mcp_tool_maps_to_a_subcommand():
    """The legacy and TalkBack tools map through output.CLI_SUBCOMMANDS; the
    capture-and-walk tools come from inspector_widget.surface, whose registry
    generates both (test_surface.py checks their parameters)."""
    from inspector_widget import surface

    subs = _subparsers()
    generated = {s.name: s.cli_name for s in surface.SPECS}
    assert set(output.CLI_SUBCOMMANDS) | set(generated) == set(mcp_server.TOOLS)
    assert not set(output.CLI_SUBCOMMANDS) & set(generated)
    for tool, sub in {**output.CLI_SUBCOMMANDS, **generated}.items():
        assert sub in subs, (tool, sub)


@pytest.mark.parametrize("tool", sorted(output.OUTPUT_PARAMS))
def test_output_params_are_on_both_surfaces_with_the_same_defaults(tool):
    schema = mcp_server.TOOLS[tool]["schema"]["properties"]
    sub = _subparsers()[output.CLI_SUBCOMMANDS[tool]]
    flags = sub._option_string_actions
    for p in output.OUTPUT_PARAMS[tool]:
        assert p.name in schema, (tool, p.name)
        assert schema[p.name].get("default") == p.default_value(), (tool, p.name)
        flag = "--" + p.name.replace("_", "-")
        assert flag in flags, (tool, flag)
        action = flags[flag]
        assert action.dest == p.name and action.default == p.default_value(), (tool, flag)
        if p.enum:
            assert list(action.choices) == list(p.enum) == schema[p.name]["enum"], (tool, flag)
        if p.type == "boolean":  # both spellings on the CLI when the default is on
            assert ("--no-" + flag[2:] in flags) == bool(p.default_value()), (tool, flag)


def test_pretty_is_on_every_json_subcommand_and_nowhere_in_mcp():
    subs = _subparsers()
    for name in JSON_SUBCOMMANDS:
        assert "--pretty" in subs[name]._option_string_actions, name
    for tool, entry in mcp_server.TOOLS.items():
        assert "pretty" not in entry["schema"]["properties"], tool


def test_the_max_bytes_default_follows_the_environment(monkeypatch):
    monkeypatch.setenv("INSPECTOR_WIDGET_MAX_BYTES", "12345")
    sub = _subparsers()["inspect"]
    assert sub._option_string_actions["--max-bytes"].default == 12345
    assert output.P_MAX_BYTES.json_schema()["default"] == 12345


# --------------------------------------------------------------------------- bytes
#: (MCP tool, args, CLI argv): the same call on both surfaces (default scene
#: unless a scene is named in the case id).
CASES = [
    ("dump_tree", {}, ["dump"]),
    ("dump_tree", {"include_properties": True}, ["dump", "--properties"]),
    ("dump_tree", {"max_depth": 1}, ["dump", "--max-depth", "1"]),
    ("dump_tree", {"root": "1006", "max_depth": 2}, ["dump", "--root", "1006", "--max-depth", "2"]),
    ("dump_tree", {"root": "4040"}, ["dump", "--root", "4040"]),  # an error on both
    ("get_properties", {"view_id": 1003}, ["get-properties", "--view-id", "1003"]),
    ("get_properties", {"view_id": 1003, "filter": "nondefault"},
     ["get-properties", "--view-id", "1003", "--filter", "nondefault"]),
    ("dump_compose", {}, ["compose"]),
    ("dump_compose", {"include_slot_table": False}, ["compose", "--no-slot-table"]),
    ("dump_compose", {"enable_inspection": True, "user_code_only": False},
     ["compose", "--enable-inspection", "--no-user-code-only"]),
    ("dump_accessibility", {}, ["a11y"]),
    ("dump_accessibility", {"focus_order": "none", "max_depth": 3},
     ["a11y", "--focus-order", "none", "--max-depth", "3"]),
    ("dump_accessibility", {"root": "view:1006"}, ["a11y", "--root", "view:1006"]),
    ("a11y_lint", {}, ["a11y-lint"]),
    ("a11y_lint", {"group_by": "none", "include_contrast": False},
     ["a11y-lint", "--group-by", "none", "--no-contrast"]),
    ("inspect", {}, ["inspect"]),
    ("inspect", {"include_properties": True, "max_depth": 3},
     ["inspect", "--properties", "--max-depth", "3"]),
    ("inspect", {"root": "view:1006"}, ["inspect", "--root", "view:1006"]),
    ("inspect_node", {"view_id": 1004, "include_image": False},
     ["inspect-node", "--view-id", "1004", "--no-image"]),
    ("inspect_node", {"node_key": "compose:1006:2", "include_image": False},
     ["inspect-node", "--node-key", "compose:1006:2", "--no-image"]),
    ("inspect", {"max_bytes": 1000}, ["inspect", "--max-bytes", "1000"]),  # an envelope
]

#: What may differ, and only in these ways: the flag a note names on its own surface,
#: a spill file's name (each call writes its own; the contents are compared), and a
#: wall-clock measurement (the lint's elapsed_ms: 0, 1 or 2 ms from call to call).
_SPILL = re.compile(r'"spill_path":"[^"]*"')
_TIMING = re.compile(r'"(%s)":\d+' % "|".join(sorted(rg.VOLATILE_KEYS)))


def _masked(text: str) -> str:
    return _TIMING.sub(r'"\1":"<ms>"', _SPILL.sub('"spill_path":"<spill>"', text))


def _same_bytes(mcp_text: str, cli_text: str) -> None:
    a = _masked(mcp_text)
    b = _masked(cli_text.replace("--enable-inspection", "enable_inspection=true"))
    assert a == b, rg.diff(json.loads(a), json.loads(b))


def _run_mcp(scene, tmp, cases):
    out = []
    with rg.harness(scene, tmp):
        for tool, args, _ in cases:
            out.append(mcp_server._call_tool_text(tool, dict(args, serial=rg.SERIAL,
                                                                package=rg.PACKAGE)))
    return out


def _run_cli(scene, tmp, cases, extra=("--json", "-")):
    out = []
    with rg.harness(scene, tmp):
        for _, _, argv in cases:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                rc = cli.main([*argv, *extra])
            out.append((buf.getvalue().rstrip("\n"), rc))
    return out


@pytest.mark.parametrize("scene", ["default", "launcher"])
def test_cli_json_is_the_mcp_text(scene, tmp_path):
    cases = CASES if scene == "default" else [
        c for c in CASES if "root" not in c[1] and "view_id" not in c[1]
        and "node_key" not in c[1]] + [
        ("get_properties", {"view_id": 82}, ["get-properties", "--view-id", "82"])]
    mcp = _run_mcp(scene, str(tmp_path / "mcp"), cases)
    cli_out = _run_cli(scene, str(tmp_path / "cli"), cases)
    for (tool, args, argv), (text, is_error), (cli_text, rc) in zip(cases, mcp, cli_out):
        assert rc == (1 if is_error else 0), (tool, args, rc, cli_text[:200])
        _same_bytes(text, cli_text)
        env = json.loads(text)
        if env.get("truncated"):  # both spill files hold the same brief result
            with open(env["spill_path"], encoding="utf-8") as f:
                a = f.read()
            with open(json.loads(cli_text)["spill_path"], encoding="utf-8") as f:
                assert a == f.read()


def test_a_bad_root_is_an_error_on_both_surfaces(tmp_path):
    [(text, is_error)] = _run_mcp("default", str(tmp_path / "m"),
                                  [("inspect", {"root": "view:4040"}, None)])
    [(cli_text, rc)] = _run_cli("default", str(tmp_path / "c"),
                                [(None, None, ["inspect", "--root", "view:4040"])])
    assert is_error and rc == 1 and cli_text == text
    assert "not found" in json.loads(text)["error"]


def test_pretty_is_the_same_document_indented(tmp_path):
    [(compact, _)] = _run_cli("default", str(tmp_path / "a"), [(None, None, ["inspect"])])
    [(pretty, _)] = _run_cli("default", str(tmp_path / "b"),
                             [(None, None, ["inspect", "--pretty"])])
    assert "\n  " in pretty and "\n" not in compact
    assert json.loads(pretty) == json.loads(compact)


def test_talkback_status_is_the_same_document(tmp_path):
    [(text, is_error)] = _run_mcp("default", str(tmp_path / "m"),
                                  [("talkback", {"action": "status"}, None)])
    [(cli_text, rc)] = _run_cli("default", str(tmp_path / "c"),
                                [(None, None, ["talkback", "status"])], extra=())
    assert not is_error and rc == 0 and cli_text == text
