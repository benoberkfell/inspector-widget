"""enable_inspection (a HotReloader recomposition that resets remember{} state in
every composition) must be opt-in on every surface: Session, CLI and MCP."""

from __future__ import annotations

import inspect

import cli
import mcp_server
from inspector_widget import Session, strings


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
