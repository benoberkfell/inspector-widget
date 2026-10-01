"""enable_inspection (a HotReloader recomposition that resets remember{} state in
every composition) must be opt-in on every surface: Session, CLI and MCP."""

from __future__ import annotations

import inspect

import pytest

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


def _compose_resp(windows: int, diagnostics: str):
    resp = pb.DumpComposeResponse()
    for i in range(windows):
        window = resp.windows.add()
        window.root.id = 100 + i
        window.root.kind = pb.ComposeNode.COMPOSABLE
    resp.diagnostics = diagnostics
    return resp


@pytest.mark.parametrize("windows,diag,suggests", [
    (0, "found 0 AndroidComposeView(s); bounds=screen", False),
    (1, "found 1 AndroidComposeView(s); bounds=screen; compose_obfuscated: Compose present "
        "but classes are renamed (AndroidComposeView is a.b), semantics/slot table "
        "unavailable, a11y still works; view#100 produced no compose nodes", False),
    (1, "found 1 AndroidComposeView(s); bounds=screen; semantics_failed: view#100 "
        "owner_unreachable", False),
    (1, "found 1 AndroidComposeView(s); bounds=screen; slot table empty "
        "(inspection_slot_table_set not populated) for 1/1 view(s)", True),
])
def test_the_hot_reload_is_suggested_only_where_it_can_help(monkeypatch, windows, diag,
                                                            suggests):
    """No ComposeView, an obfuscated Compose or a failed semantics read: the slot
    table cannot be populated, so neither surface recommends the destructive
    enable_inspection; both say why instead (backlog: agent-hardening)."""
    resp = _compose_resp(windows, diag)

    class FakeSession:
        def dump_compose(self, **kwargs):
            return resp

    monkeypatch.setattr(mcp_server.SESSIONS, "get_or_attach", lambda serial, package: FakeSession())
    note = mcp_server._run_tool("dump_compose", {"serial": "s", "package": "p"})["note"]
    assert ("enable_inspection=true hot-reloads" in note) is suggests, note
    assert ("Pass enable_inspection" in note) is suggests
    if not suggests:
        assert note.startswith("no slot table")
    from inspector_widget import results
    data = strings.dump_compose_to_dict(resp)
    cli_note = results.compose_note(data, "--enable-inspection",
                                    strings.ENABLE_INSPECTION_WARNING)
    assert ("--enable-inspection hot-reloads" in cli_note) is suggests
    if not suggests:
        assert cli_note == note  # the same words on both surfaces
