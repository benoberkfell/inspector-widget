"""The surface registry (WP S2): one ToolSpec list generates the MCP tools and the
CLI subcommands of capture and walk.

* parity: every spec is exactly one MCP tool and one CLI subcommand, with the
  same parameter names (snake_case <-> --kebab-case) and defaults; the only
  transport-only parameters are allow-listed;
* validation: a bad type, enum value or range is ``bad_args`` on every
  transport (the mcp SDK 1.x server, a 2.x-shaped server, the JSON-RPC fallback);
* toolsets: what tools/list shows per INSPECTOR_WIDGET_TOOLSET, the byte budgets,
  and hidden tools staying callable by name;
* the MCP instructions (<= 900 B) in initialize on every transport;
* image(inline=true) as MCP ImageContent beside the text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import types

import pytest

import cli
import mcp_server
from inspector_widget import surface
from inspector_widget.capture.model import OpError
from inspector_widget.output import dumps

#: Parameters one transport has and the other has not, on purpose.
TRANSPORT_ONLY = {
    ("image", "inline"): "mcp",       # the pixels ride in the MCP reply (ImageContent)
    ("image", "out"): "cli",          # the CLI copies the PNG to a path instead
    ("capture", "build_out"): "cli",  # the MCP server takes $INSPECTOR_WIDGET_ARTIFACTS
}


def _subparsers() -> dict[str, argparse.ArgumentParser]:
    for action in cli.build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("no subcommands")


def _size(obj) -> int:
    return len(dumps(obj).encode("utf-8"))


# --------------------------------------------------------------------------- #
# Parity
# --------------------------------------------------------------------------- #
def test_every_spec_is_one_mcp_tool_and_one_cli_subcommand():
    subs = _subparsers()
    names = [s.name for s in surface.SPECS]
    assert names == list(surface.CAPTURE_TOOLS)
    for ts in surface.SPECS:
        assert ts.name in mcp_server.TOOLS, ts.name
        assert mcp_server.TOOLS[ts.name]["surface"] is ts
        assert ts.cli_name in subs, ts.cli_name
        ns = cli.build_parser().parse_args([ts.cli_name])
        assert ns.surface_tool == ts.name
    # nothing else claims those names on either surface
    assert len(set(names)) == len(names)


@pytest.mark.parametrize("ts", surface.SPECS, ids=lambda s: s.name)
def test_parameters_have_the_same_names_and_defaults(ts):
    schema = mcp_server.TOOLS[ts.name]["schema"]["properties"]
    sub = _subparsers()[ts.cli_name]
    by_dest = {a.dest: a for a in sub._actions}
    for p in ts.params:
        only = TRANSPORT_ONLY.get((ts.name, p.name))
        if only is not None:
            assert p.surfaces == (only,), (ts.name, p.name)
        else:
            assert p.surfaces == ("mcp", "cli"), (ts.name, p.name)
        if "mcp" in p.surfaces:
            assert p.name in schema, (ts.name, p.name)
            assert schema[p.name].get("default") == p.default, (ts.name, p.name)
        else:
            assert p.name not in schema
        if "cli" in p.surfaces:
            action = by_dest.get(p.name)
            assert action is not None, (ts.name, p.name)
            if p.positional:
                assert not action.option_strings
            else:
                assert p.flag in action.option_strings, (ts.name, p.flag)
            default = [] if p.positional and p.nargs == "*" else p.default
            if p.type == "boolean":
                default = bool(p.default)
            assert action.default == default, (ts.name, p.name, action.default)
        else:
            assert p.name not in by_dest
    # and no CLI flag that is not a parameter (besides the output flags)
    extra = {a.dest for a in sub._actions} - {p.name for p in ts.params} \
        - {"help", *surface.CLI_OUTPUT_FLAGS, "rules_one"}
    assert not extra, (ts.name, extra)
    unknown = set(schema) - {p.name for p in ts.params}
    assert not unknown


def test_cli_args_map_back_to_the_mcp_arguments():
    parser = cli.build_parser()
    cases = [
        (["find", "--flags", "click,focus", "--at", "10,20", "--count", "--in", "all"],
         {"flags": ["click", "focus"], "at": [10, 20], "count_only": True, "in": "all"}),
        (["node", "n1", "n2", "--props", "text,textSize"],
         {"refs": ["n1", "n2"], "props": ["text", "textSize"]}),
        (["node", "#badSwitch", "--props", "nondefault"],
         {"ref": "#badSwitch", "props": "nondefault"}),
        (["lint", "--rule", "R1", "--rule", "R2", "--severity", "warn"],
         {"rules": ["R1", "R2"], "severity": "warn"}),
        (["captures", "rm", "c7h2kq"], {"action": "drop", "id": "c7h2kq"}),
        (["captures", "label", "c7h2kq", "before"],
         {"action": "label", "id": "c7h2kq", "label": "before"}),
        (["capture", "--no-props", "--scale", "0.5", "-s", "emu", "-p", "pkg", "--label", "x"],
         {"props": False, "screenshot_scale": 0.5, "serial": "emu", "package": "pkg",
          "label": "x"}),
        (["diff", "before", "--include", "text,+props"],
         {"a": "before", "include": ["text", "+props"]}),
        (["outline"], {}),  # defaults are not passed: the call is the same as MCP's outline()
    ]
    for argv, want in cases:
        ns = parser.parse_args(argv)
        assert surface.cli_args(surface.spec(ns.surface_tool), ns) == want, argv


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
BAD_ARGS = [
    ("find", {"limit": "x"}),               # type
    ("find", {"limit": 0}),                 # range
    ("outline", {"view": "nope"}),          # enum
    ("outline", {"bogus": 1}),              # unknown name
    ("capture", {"screenshot_scale": 0}),   # exclusive minimum
    ("lint", {"rules": ["nope"]}),          # rule ids and aliases
    ("find", {"flags": [1]}),               # item type
    ("node", {"props": 3}),                 # string or list
]


@pytest.mark.parametrize("tool,args", BAD_ARGS)
def test_validator_rejects_with_bad_args(tool, args):
    with pytest.raises(surface.OpError) as e:
        surface.validate(surface.spec(tool), args)
    assert e.value.code == "bad_args" and e.value.hint


def test_validator_cleans_what_is_fine():
    ts = surface.spec("find")
    assert surface.validate(ts, {"limit": 20.0, "text": None, "flags": "click,focus"}) == {
        "limit": 20, "flags": ["click", "focus"]}
    assert surface.validate(surface.spec("lint"), {"rules": ["R5", "a11y."]})["rules"] == [
        "R5", "a11y."]


def _fallback_call(name: str, arguments: dict) -> dict:
    return mcp_server._fallback_handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                        "params": {"name": name, "arguments": arguments}})


@pytest.mark.parametrize("tool,args", BAD_ARGS)
def test_bad_args_on_the_fallback_transport(tool, args):
    res = _fallback_call(tool, args)["result"]
    assert res["isError"] is True
    assert json.loads(res["content"][0]["text"])["error"]["code"] == "bad_args"


@pytest.mark.parametrize("tool,args", BAD_ARGS)
def test_bad_args_on_the_mcp_sdk(tool, args):
    mcp_types = pytest.importorskip("mcp.types")
    server = mcp_server._build_mcp_server()
    if not hasattr(server, "request_handlers"):
        pytest.skip("not an mcp 1.x server")
    handler = server.request_handlers[mcp_types.CallToolRequest]
    req = mcp_types.CallToolRequest(method="tools/call", params=mcp_types.CallToolRequestParams(
        name=tool, arguments=args))
    res = asyncio.run(handler(req)).root
    assert res.isError is True
    assert json.loads(res.content[0].text)["error"]["code"] == "bad_args"


class _Model:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _mcp2_stub(monkeypatch):
    """Install a stub shaped like the mcp 2.x API (constructor handlers)."""
    fake_types = types.SimpleNamespace(Tool=_Model, ListToolsResult=_Model,
                                       CallToolResult=_Model, TextContent=_Model,
                                       ImageContent=_Model)

    class Server2:
        def __init__(self, name, *, version="", instructions=None, on_list_tools=None,
                     on_call_tool=None):
            self.name, self.instructions = name, instructions
            self.on_list_tools, self.on_call_tool = on_list_tools, on_call_tool

    mcp_pkg = types.ModuleType("mcp")
    mcp_pkg.types = fake_types
    server_mod = types.ModuleType("mcp.server")
    server_mod.Server = Server2
    monkeypatch.setitem(sys.modules, "mcp", mcp_pkg)
    monkeypatch.setitem(sys.modules, "mcp.types", fake_types)
    monkeypatch.setitem(sys.modules, "mcp.server", server_mod)
    return mcp_server._build_mcp_server()


def test_bad_args_on_an_mcp2_shaped_server(monkeypatch):
    server = _mcp2_stub(monkeypatch)
    for tool, args in BAD_ARGS:
        res = asyncio.run(server.on_call_tool(None, types.SimpleNamespace(name=tool,
                                                                          arguments=args)))
        assert res.isError is True, (tool, args)
        assert json.loads(res.content[0].text)["error"]["code"] == "bad_args"


# --------------------------------------------------------------------------- #
# Toolsets
# --------------------------------------------------------------------------- #
def _listing(monkeypatch, toolset: str | None) -> dict:
    if toolset is None:
        monkeypatch.delenv(surface.ENV_TOOLSET, raising=False)
    else:
        monkeypatch.setenv(surface.ENV_TOOLSET, toolset)
    return mcp_server._fallback_handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[
        "result"]


def test_the_default_listing_is_unchanged(monkeypatch):
    """Until the flip (S4) the server lists what it listed before the capture tools:
    the 15 legacy and 3 TalkBack tools, byte for byte (18,410 B compact; 18,337 before
    a11y_lint's description said where the findings under an open dialog go)."""
    listing = _listing(monkeypatch, None)
    names = [t["name"] for t in listing["tools"]]
    assert names == [n for n in mcp_server.TOOLS if n not in surface.CAPTURE_TOOLS]
    assert set(names) == set(surface.LEGACY_TOOLS) | set(surface.TALKBACK_TOOLS)
    assert _size(listing) == 18_410


@pytest.mark.parametrize("toolset,count,limit", [
    ("legacy", 15, 18_500), ("capture", 12, 12_000), ("talkback", 7, 18_500),
    ("capture,talkback", 15, 20_000), ("all", 26, 32_000)])
def test_toolset_listings(monkeypatch, toolset, count, limit):
    listing = _listing(monkeypatch, toolset)
    names = [t["name"] for t in listing["tools"]]
    assert len(names) == count and set(names) == set(surface.toolset_names(toolset))
    assert _size(listing) <= limit, _size(listing)
    for t in listing["tools"]:  # the capture tools say whether they only read
        if t["name"] in surface.CAPTURE_TOOLS:
            ro = t["annotations"]["readOnlyHint"]
            assert ro is surface.spec(t["name"]).read_only


def test_the_capture_toolset_is_the_session_tools_plus_the_eight():
    assert surface.toolset_names("capture") == surface.SESSION_TOOLS + surface.CAPTURE_TOOLS
    assert len(surface.toolset_names("legacy")) == 15


def test_an_unknown_toolset_lists_the_default(monkeypatch, caplog):
    listing = _listing(monkeypatch, "nonsense")
    assert len(listing["tools"]) == 18
    with pytest.raises(ValueError):
        surface.toolset_names("nonsense")


def test_hidden_tools_stay_callable_by_name(monkeypatch):
    listing = _listing(monkeypatch, "capture")
    assert "dump_tree" not in {t["name"] for t in listing["tools"]}
    # a legacy tool, hidden: still dispatched (validated, not "unknown tool"; no adb here)
    res = _fallback_call("dump_tree", {"bogus": 1})["result"]
    text = res["content"][0]["text"]
    assert "missing required argument: package" in text and "unknown tool" not in text
    monkeypatch.delenv(surface.ENV_TOOLSET)
    res = _fallback_call("outline", {})["result"]  # hidden in the default toolset
    err = json.loads(res["content"][0]["text"])["error"]
    assert err["code"] == "capture_not_found"


def test_self_check_prints_the_toolset(monkeypatch, capsys):
    monkeypatch.setenv(surface.ENV_TOOLSET, "capture")
    mcp_server._self_check()
    out = capsys.readouterr().out
    assert "toolset: capture (12 listed" in out
    assert "callable by name (14)" in out


# --------------------------------------------------------------------------- #
# Instructions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("toolset", [None, "legacy", "capture", "capture,talkback", "all"])
def test_instructions_are_small_and_fit_the_listing(monkeypatch, toolset):
    names = surface.toolset_names(toolset) if toolset else surface.toolset_names()
    text = surface.instructions(names)
    assert 0 < len(text.encode("utf-8")) <= surface.INSTRUCTIONS_MAX_BYTES
    if "capture" in names:
        assert text.startswith(surface.INSTRUCTIONS) and "capture()" in text
    else:
        assert "INSPECTOR_WIDGET_TOOLSET=capture" in text
    assert ("tb_walk" in text) == ("tb_walk" in names)


def test_initialize_carries_the_instructions_on_every_transport(monkeypatch):
    monkeypatch.setenv(surface.ENV_TOOLSET, "capture,talkback")
    want = surface.INSTRUCTIONS + surface.INSTRUCTIONS_TALKBACK
    init = mcp_server._fallback_handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                        "params": {}})
    assert init["result"]["instructions"] == want
    if _real_sdk():
        server = mcp_server._build_mcp_server()
        assert server.create_initialization_options().instructions == want
    server2 = _mcp2_stub(monkeypatch)
    assert server2.instructions == want


def _real_sdk() -> bool:
    try:
        from mcp.server import Server
    except Exception:
        return False
    return hasattr(Server, "list_tools")


# --------------------------------------------------------------------------- #
# ImageContent
# --------------------------------------------------------------------------- #
def test_inline_images_ride_beside_the_text(monkeypatch):
    fake = surface.Result({"capture": "c7h2kq", "path": "/x.png"}, [("image/png", "AAAA")])
    monkeypatch.setitem(mcp_server.TOOLS["image"], "handler", lambda args: fake)
    res = _fallback_call("image", {"ref": "n1", "inline": True})["result"]
    assert [c["type"] for c in res["content"]] == ["text", "image"]
    assert res["content"][1] == {"type": "image", "data": "AAAA", "mimeType": "image/png"}
    server2 = _mcp2_stub(monkeypatch)
    out = asyncio.run(server2.on_call_tool(None, types.SimpleNamespace(
        name="image", arguments={"ref": "n1", "inline": True})))
    assert [c.type for c in out.content] == ["text", "image"]
    assert out.content[1].mimeType == "image/png"


def test_json_dash_is_json_on_the_capture_subcommands():
    """The legacy subcommands take --json OUT.json|-; on the capture ones (which
    always print to stdout) --json - must not be an argparse error."""
    assert surface.cli_argv(["capture", "--json", "-", "-s", "x"]) == ["capture", "--json",
                                                                         "-s", "x"]
    assert surface.cli_argv(["node", "n22", "--json", "-"]) == ["node", "n22", "--json"]
    assert surface.cli_argv(["dump", "--json", "-"]) == ["dump", "--json", "-"]  # legacy
    assert surface.cli_argv([]) == []


# --------------------------------------------------------------------------- #
# Review fixes: instructions name listed tools only, destructive hints, find's
# window z index, dash-leading CLI values, CLI-form next hints
# --------------------------------------------------------------------------- #
_NAMED_TOOLS = ("outline", "inspect_node", "dump_accessibility", "a11y_lint", "tb_walk",
                "tb_scenario", "list_devices", "list_processes", "capture()")


@pytest.mark.parametrize("toolset", [None, "legacy", "talkback", "capture", "capture,talkback",
                                     "legacy,talkback", "all"])
def test_instructions_name_only_listed_tools(toolset):
    names = set(surface.toolset_names(toolset) if toolset else surface.toolset_names())
    text = surface.instructions(names)
    for tool in _NAMED_TOOLS:
        if tool in text:
            assert tool.rstrip("()") in names, f"{tool} named but not listed ({toolset})"
    assert ("inspect gives" in text) == ("inspect" in names and "capture" not in names)


def test_default_instructions_predict_talkback_with_a_listed_tool():
    text = surface.instructions(surface.toolset_names(surface.DEFAULT_TOOLSET))
    assert 'outline(view="reading")' not in text
    assert "focus_order predicts" in text and "tb_walk" in text
    full = surface.instructions(surface.toolset_names("all"))
    assert full.endswith(surface.INSTRUCTIONS_TALKBACK)


def test_capture_and_captures_are_marked_destructive():
    assert surface.annotations(surface.spec("capture"))["destructiveHint"] is True
    assert surface.annotations(surface.spec("captures"))["destructiveHint"] is True
    assert surface.annotations(surface.spec("outline")) == {"readOnlyHint": True}
    assert mcp_server.TOOLS["captures"]["annotations"]["destructiveHint"] is True


def test_find_window_takes_a_z_index():
    ts = surface.spec("find")
    assert surface.validate(ts, {"window": 1}) == {"window": 1}
    assert surface.validate(ts, {"window": 1.0}) == {"window": 1}
    assert surface.validate(ts, {"window": "n3"}) == {"window": "n3"}
    with pytest.raises(OpError) as e:
        surface.validate(ts, {"window": True})
    assert e.value.code == "bad_args" and "integer" in (e.value.hint or "")
    schema = surface.json_schema(ts)["properties"]["window"]
    assert schema["type"] == ["string", "integer"] and "z index" in schema["description"]


def test_cli_takes_dash_leading_values():
    """Spec 6.4's "-bounds" removes a field; the CLI must accept it as written."""
    assert surface.cli_argv(["outline", "--fields", "-bounds", "--json"]) == [
        "outline", "--fields=-bounds", "--json"]
    assert surface.cli_argv(["find", "--at", "-5,10"]) == ["find", "--at=-5,10"]
    # a real flag after a value-taking flag is left for argparse to report
    assert surface.cli_argv(["find", "--serial", "-p", "x"]) == ["find", "--serial", "-p", "x"]
    import cli
    ns = cli.build_parser().parse_args(surface.cli_argv(["outline", "--fields", "-bounds"]))
    assert ns.fields == "-bounds"


@pytest.mark.parametrize("hint,want", [
    ('find(issue="render.")', "inspector-widget find --issue render."),
    ('outline(fields="-bounds",max_lines=2,cursor="c1:o:ab:20")',
     "inspector-widget outline --fields=-bounds --max-lines 2 --cursor c1:o:ab:20"),
    ('lint(rules=["R1"],group="node",within="n38")',
     "inspector-widget lint --rules R1 --group node --within n38"),
    ('node("n41")', "inspector-widget node n41"),
    ('captures(action="show",id="c1")', "inspector-widget captures show c1"),
    ('find(in="all",text="Delete me")', "inspector-widget find --in all --text 'Delete me'"),
    ('find(count_only=true)', "inspector-widget find --count-only"),
    ('tb_walk(start="first")', 'tb_walk(start="first")'),  # no generated subcommand
])
def test_cli_hints_are_cli_commands(hint, want):
    assert surface.cli_hint(hint) == want


def test_cli_hints_parse_back_to_the_mcp_arguments():
    import shlex

    import cli
    parser = cli.build_parser()
    for hint in ('outline(fields="-bounds",max_lines=2,cursor="c1:o:ab:20")',
                 'lint(rules=["R1","R2"],group="node",within="n38",severity="warn")',
                 'find(in="all",text="Delete me",flags=["click","focus"])',
                 'node("n41")', 'diff(a="before")'):
        tool, pos, kw = surface._parse_call(hint)
        argv = surface.cli_argv(shlex.split(surface.cli_hint(hint))[1:])
        args = surface.cli_args(surface.spec(tool), parser.parse_args(argv))
        want = dict(kw)
        if pos:
            want[[p.name for p in surface.spec(tool).params if p.positional][0]] = pos[0]
        assert args == want, hint


def test_human_output_prints_cli_hints():
    doc = {"capture": "c1", "lines": ["n1 View"], "next": ['outline(root="n15")']}
    assert surface._render_lines(doc)[-1] == "next: inspector-widget outline --root n15"
    out = surface._render_json({"ref": "n1", "next": ['node("n2")']})
    assert out[-1] == "next: inspector-widget node n2" and '"next"' not in out[0]
    cap = surface._render_capture({"capture": "c1", "next": ["lint()"]})
    assert cap[-1] == "next: inspector-widget lint"
