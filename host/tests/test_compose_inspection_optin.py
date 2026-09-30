"""enable_inspection (a HotReloader recomposition that resets remember{} state in
every composition) must be opt-in on every surface: Session, CLI and MCP."""

from __future__ import annotations

import inspect

import cli
import mcp_server
from inspector_widget import Session, strings
from inspector_widget.proto import view_inspection_pb2 as pb


def test_session_default_is_off():
    assert inspect.signature(Session.dump_compose).parameters["enable_inspection"].default is False


def test_mcp_default_is_off_and_described_as_destructive():
    assert inspect.signature(mcp_server.tool_dump_compose).parameters["enable_inspection"].default is False
    prop = mcp_server.TOOLS["dump_compose"]["schema"]["properties"]["enable_inspection"]
    assert prop["default"] is False
    assert "DESTRUCTIVE" in prop["description"]
    assert "state is preserved" not in prop["description"]


def test_cli_flag_is_opt_in_and_old_flag_still_parses():
    parser = cli.build_parser()
    args = parser.parse_args(["compose", "--package", "p"])
    assert args.enable_inspection is False
    assert parser.parse_args(["compose", "--package", "p", "--enable-inspection"]).enable_inspection is True
    parser.parse_args(["compose", "--package", "p", "--no-enable-inspection"])  # deprecated no-op


def test_slot_table_populated_helper():
    sem_only = {"windows": [{"root": {"kind": "COMPOSABLE", "children": [
        {"kind": "SEMANTICS", "children": [{"kind": "SEMANTICS"}]}]}}]}
    with_slots = {"windows": [{"root": {"kind": "COMPOSABLE", "children": [
        {"kind": "SEMANTICS"}, {"kind": "COMPOSABLE", "name": "Button"}]}}]}
    assert strings.compose_slot_table_populated(sem_only) is False
    assert strings.compose_slot_table_populated(with_slots) is True
    assert strings.compose_slot_table_populated({"windows": []}) is False


def test_mcp_semantics_only_dump_adds_note(monkeypatch):
    """The default call on a fresh process (slot table empty) must succeed and carry the
    opt-in note. Regression: the warning was %-formatted without a placeholder -> TypeError."""
    resp = pb.DumpComposeResponse()
    window = resp.windows.add()
    window.root.id = 1
    window.root.kind = pb.ComposeNode.COMPOSABLE
    sem = window.root.children.add()
    sem.id = 2
    sem.kind = pb.ComposeNode.SEMANTICS

    class FakeSession:
        def dump_compose(self, **kwargs):
            assert kwargs["enable_inspection"] is False
            return resp

    monkeypatch.setattr(mcp_server.SESSIONS, "get_or_attach", lambda serial, package: FakeSession())
    result = mcp_server._run_tool("dump_compose", {"serial": "s", "package": "p"})
    assert "error" not in result, result
    assert "enable_inspection=true hot-reloads" in result["note"]
    assert "--enable-inspection hot-reloads" in strings.ENABLE_INSPECTION_WARNING % "--enable-inspection"
