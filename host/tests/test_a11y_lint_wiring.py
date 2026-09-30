"""Device-free checks of the a11y-lint wiring in cli.py and mcp_server.py.

The lint engine itself is covered by test_a11y_lint_unified.py; here we assert
the entry points validate rule ids before touching a device, expose the rule
enum in the MCP schema, and pass the unified report through unchanged.
"""

from __future__ import annotations

import pytest

import cli
import mcp_server
from inspector_widget import a11y_lint as L
from inspector_widget.proto import view_inspection_pb2 as pb


def _explode(*a, **k):
    raise AssertionError("must not touch the device")


def test_cli_a11y_lint_rejects_unknown_rule_before_injecting(monkeypatch, capsys):
    monkeypatch.setattr(cli.injectmod, "inject_and_connect", _explode)
    args = cli.build_parser().parse_args(["a11y-lint", "--rule", "R1", "--rule", "a11y.nope"])
    assert args.func(args) == 2
    err = capsys.readouterr().err
    assert "a11y.nope" in err and "R1=a11y.label.missing" in err


def test_cli_a11y_lint_accepts_aliases_and_rendering_info_flag():
    args = cli.build_parser().parse_args(["a11y-lint", "--rule", "R2", "--no-rendering-info"])
    assert args.rules == ["R2"] and args.include_rendering_info is False
    assert cli.build_parser().parse_args(["a11y-lint"]).include_rendering_info is True


def test_mcp_a11y_lint_unknown_rule_is_a_tool_error(monkeypatch):
    monkeypatch.setattr(mcp_server.SESSIONS, "get_or_attach", _explode)
    with pytest.raises(mcp_server.ToolError) as ei:
        mcp_server.tool_a11y_lint("emulator-5556", "com.example", rules=["R1", "bogus"])
    assert "bogus" in str(ei.value)
    with pytest.raises(mcp_server.ToolError):
        mcp_server.tool_a11y_lint("emulator-5556", "com.example", rules=5)


def test_mcp_schema_enumerates_rule_ids_and_aliases():
    props = mcp_server.TOOLS["a11y_lint"]["schema"]["properties"]
    enum = props["rules"]["items"]["enum"]
    assert set(L.ALL_RULE_IDS) <= set(enum)
    assert {"R1", "R12", "R18", "TouchTargetSize"} <= set(enum)
    assert props["include_rendering_info"]["default"] is True


class _FakeSession:
    def __init__(self):
        self.calls = []

    def dump_a11y(self, **kw):
        self.calls.append(("dump_a11y", kw))
        return pb.DumpA11yResponse()

    def dump_compose(self, **kw):
        self.calls.append(("dump_compose", kw))
        return pb.DumpComposeResponse()

    def screenshot(self, **kw):
        self.calls.append(("screenshot", kw))
        return pb.ScreenshotResponse()


def test_mcp_a11y_lint_returns_the_unified_report(monkeypatch):
    sess = _FakeSession()
    monkeypatch.setattr(mcp_server.SESSIONS, "get_or_attach", lambda s, p: sess)
    monkeypatch.setattr(mcp_server, "_a11y_device_metrics", lambda serial: (420, 1.15))
    out = mcp_server.tool_a11y_lint("emulator-5556", "com.example", rules=["R1", "R2"])
    assert out["density"] == 420 and out["font_scale"] == 1.15
    assert out["summary"]["total"] == 0 and out["findings"] == []
    assert out["stats"]["rules"] == ["a11y.label.missing", "a11y.touch_target.small"]
    assert out["contrast_sampled"] is False
    assert ("dump_a11y", {"root_id": 0, "include_extras": True,
                          "include_rendering_info": True}) in sess.calls
    # rule subset without R3 -> no screenshot requested
    assert not [c for c in sess.calls if c[0] == "screenshot"]


class _FakeInjection:
    sock = None

    def close(self):
        pass


class _FakeClient(_FakeSession):
    def __init__(self, *a, **k):
        super().__init__()

    def hello(self):
        return pb.HelloResponse()


def _fake_device(monkeypatch):
    from inspector_widget import adb
    monkeypatch.setattr(cli.injectmod, "inject_and_connect", lambda **k: _FakeInjection())
    monkeypatch.setattr(cli, "Client", _FakeClient)
    monkeypatch.setattr(adb, "display_density", lambda serial: 420)
    monkeypatch.setattr(adb, "font_scale", lambda serial: 1.0)


def test_cli_a11y_lint_runs_device_free(monkeypatch, capsys):
    import json
    _fake_device(monkeypatch)
    args = cli.build_parser().parse_args(["a11y-lint", "--rule", "R1", "--json", "-"])
    assert args.func(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["summary"]["total"] == 0 and out["stats"]["rules"] == ["a11y.label.missing"]


def test_cli_a11y_with_lint_adds_lint_to_json(monkeypatch, capsys):
    import json
    _fake_device(monkeypatch)
    args = cli.build_parser().parse_args(["a11y", "--lint", "--no-contrast", "--json", "-"])
    assert args.func(args) == 0
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["lint"]["summary"]["total"] == 0
    assert "a11y lint: 0 error, 0 warn, 0 info" in captured.err
