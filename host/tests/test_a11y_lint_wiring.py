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


class _FakeCliSession:
    """What cli._session() gets from inspector_widget.attach: the fake connection as
    ``client``, and a context manager (leaving it disconnects)."""
    note = None
    pid = None

    def __init__(self, client=None):
        self.client = client

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def _fake_attach(monkeypatch, client):
    import inspector_widget as iw
    monkeypatch.setattr(iw, "attach", lambda *a, **k: _FakeCliSession(client))


def test_cli_a11y_lint_rejects_unknown_rule_before_injecting(monkeypatch, capsys):
    import inspector_widget as iw
    monkeypatch.setattr(iw, "attach", _explode)
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


def test_mcp_rule_ids_are_checked_before_the_device(monkeypatch):
    """The schema takes strings (an enum of all 47 ids and aliases would cost ~900 B
    of tools/list); the handler checks them first and names the valid ones."""
    props = mcp_server.TOOLS["a11y_lint"]["schema"]["properties"]
    assert props["rules"]["items"] == {"type": "string"}
    assert "R1..R18" in props["rules"]["description"]
    assert props["include_rendering_info"]["default"] is True
    monkeypatch.setattr(mcp_server.SESSIONS, "get_or_attach", _explode)
    res = mcp_server._run_tool("a11y_lint", {"package": "com.example", "serial": "s",
                                             "rules": ["R1", "TouchTargetSize", "bogus"]})
    assert "bogus" in res["error"]
    for rule in ("R18", "TouchTargetSize", L.ALL_RULE_IDS[0]):
        assert mcp_server._a11y_lint_rules([rule])


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


def _fake_device(monkeypatch):
    from inspector_widget import adb
    _fake_attach(monkeypatch, _FakeSession())
    monkeypatch.setattr(adb, "display_density", lambda serial: 420)
    monkeypatch.setattr(adb, "font_scale", lambda serial: 1.0)


def test_cli_a11y_lint_runs_device_free(monkeypatch, capsys):
    import json
    _fake_device(monkeypatch)
    args = cli.build_parser().parse_args(["a11y-lint", "--rule", "R1", "--json", "-",
                                          "--detail", "full"])
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


# --------------------------------------------------------------------------- #
# Entry-point wiring the unit tests of the engines cannot see (mutation-tested:
# each test below fails if its entry point drops the argument it checks).
# --------------------------------------------------------------------------- #
def _shot(w, h, rgba=(255, 255, 255, 255), scale=0.5):
    import struct
    import zlib
    raw = struct.pack("<ii", w, h) + bytes([2]) + bytes(rgba) * (w * h)
    return pb.ScreenshotResponse(screenshot=pb.Screenshot(
        width=w, height=h, bitmap_type=2, data=zlib.compress(raw), scale=scale))


class _ScreenSession(_FakeSession):
    """A fake device showing the mixed View/Compose fixture (it has R1 findings)."""

    def dump_a11y(self, **kw):
        import mixed_fixture as mf
        self.calls.append(("dump_a11y", kw))
        return mf.a11y_response()

    def screenshot(self, **kw):
        self.calls.append(("screenshot", kw))
        return _shot(540, 1200)

    def hello(self):
        return pb.HelloResponse()


def test_mcp_a11y_overlay_colours_the_lint_findings(monkeypatch):
    pytest.importorskip("PIL")
    sess = _ScreenSession()
    monkeypatch.setattr(mcp_server.SESSIONS, "get_or_attach", lambda s, p: sess)
    monkeypatch.setattr(mcp_server, "_a11y_device_metrics", lambda serial: (420, 1.0))
    out = mcp_server.tool_a11y_overlay("emulator-5556", "com.example", include_contrast=False)
    assert out["finding_count"] > 0
    assert out["flagged"] > 0, "the overlay drew no severity colour for the lint findings"


def test_cli_a11y_overlay_colours_the_lint_findings_and_asks_for_rendering_info(
        monkeypatch, capsys, tmp_path):
    pytest.importorskip("PIL")
    from inspector_widget import adb
    client = _ScreenSession()
    _fake_attach(monkeypatch, client)
    monkeypatch.setattr(adb, "display_density", lambda serial: 420)
    monkeypatch.setattr(adb, "font_scale", lambda serial: 1.0)
    args = cli.build_parser().parse_args(
        ["a11y", "--lint", "--no-contrast", "--overlay", str(tmp_path / "o.png")])
    assert args.func(args) == 0
    err = capsys.readouterr().err
    import re
    flagged = int(re.search(r"(\d+) flagged", err).group(1))
    assert flagged > 0, err
    # --lint implies ExtraRenderingInfo (R11/R18 text sizes).
    assert ("dump_a11y", {"root_id": 0, "include_extras": True,
                          "include_rendering_info": True}) in client.calls


def test_mcp_a11y_lint_forwards_include_rendering_info(monkeypatch):
    sess = _FakeSession()
    monkeypatch.setattr(mcp_server.SESSIONS, "get_or_attach", lambda s, p: sess)
    monkeypatch.setattr(mcp_server, "_a11y_device_metrics", lambda serial: (420, 1.0))
    mcp_server.tool_a11y_lint("emulator-5556", "com.example", include_rendering_info=False)
    dumps = [kw for name, kw in sess.calls if name == "dump_a11y"]
    assert dumps == [{"root_id": 0, "include_extras": True, "include_rendering_info": False}]


def test_mcp_inspect_node_turns_a_key_error_into_a_tool_error(monkeypatch):
    from inspector_widget import correlate

    def raise_key(*a, **k):
        raise correlate.NodeKeyError("compose:4 is ambiguous: use one of compose:22:4, compose:32:4")

    monkeypatch.setattr(mcp_server.SESSIONS, "get_or_attach", lambda s, p: _FakeSession())
    monkeypatch.setattr(mcp_server, "_a11y_device_metrics", lambda serial: (420, 1.0))
    monkeypatch.setattr(correlate, "inspect_node", raise_key)
    with pytest.raises(mcp_server.ToolError) as ei:
        mcp_server.tool_inspect_node("emulator-5556", "com.example", node_key="compose:4",
                                     include_image=False)
    assert "ambiguous" in str(ei.value)


def test_cli_inspect_node_exits_1_on_a_key_error(monkeypatch, capsys):
    import inspector_widget as iw
    from inspector_widget import adb, correlate

    def raise_key(*a, **k):
        raise correlate.NodeKeyError("compose:32:4 is not in the current dump")

    monkeypatch.setattr(iw, "attach", lambda *a, **k: _FakeCliSession())
    monkeypatch.setattr(adb, "display_density", lambda serial: 420)
    monkeypatch.setattr(adb, "font_scale", lambda serial: 1.0)
    monkeypatch.setattr(correlate, "inspect_node", raise_key)
    args = cli.build_parser().parse_args(["inspect-node", "--node-key", "compose:32:4"])
    assert args.func(args) == 1
    assert "not in the current dump" in capsys.readouterr().err
