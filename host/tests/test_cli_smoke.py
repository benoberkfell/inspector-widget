"""Device-free smoke test of the CLI argument parser.

Asserts that ``build_parser()`` wires a ``.func`` handler for EVERY subcommand
(so no subparser is registered without a target), and that the integrated
subcommands added in this contract pass are present and resolvable. Nothing here
attaches to a device — we only parse argv and inspect the resulting Namespace.
"""

from __future__ import annotations

import argparse

import pytest

import cli


# (argv, expected handler attribute name) for every subcommand. Required options
# (--out, --view-id) and at least one selector are supplied so parsing succeeds.
_SUBCOMMANDS = [
    (["devices"], "cmd_devices"),
    (["packages"], "cmd_packages"),
    (["attach"], "cmd_attach"),
    (["dump"], "cmd_dump"),
    (["compose"], "cmd_compose"),
    (["a11y"], "cmd_a11y"),
    (["a11y-lint"], "cmd_a11y_lint"),
    (["inspect"], "cmd_inspect"),
    (["inspect-node", "--view-id", "5"], "cmd_inspect_node"),
    (["component-image", "--view-id", "5"], "cmd_component_image"),
    (["screenshot", "--out", "/tmp/out.png"], "cmd_screenshot"),
    (["get-properties", "--view-id", "5"], "cmd_get_properties"),
    (["detach"], "cmd_detach"),
]

# Subcommands introduced in this contract pass that MUST be present.
_NEW_SUBCOMMANDS = {
    "inspect", "inspect-node", "component-image", "screenshot",
    "get-properties", "detach",
}


def _subparser_names(parser: argparse.ArgumentParser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


@pytest.mark.parametrize("argv,handler_name", _SUBCOMMANDS)
def test_subcommand_resolves_a_func(argv, handler_name):
    parser = cli.build_parser()
    ns = parser.parse_args(argv)
    assert hasattr(ns, "func"), f"{argv[0]} did not set a .func handler"
    assert callable(ns.func)
    expected = getattr(cli, handler_name)
    assert ns.func is expected, (
        f"{argv[0]} resolved to {ns.func.__name__}, expected {handler_name}"
    )


# The capture-and-walk subcommands, generated from inspector_widget.surface: each
# resolves to the registry's runner for its tool (ns.surface_tool).
_GENERATED = [
    (["capture"], "capture"),
    (["captures", "ls"], "captures"),
    (["outline", "--view", "reading"], "outline"),
    (["find", "--text", "x", "--flags", "click"], "find"),
    (["node", "n1", "n2"], "node"),
    (["image", "n1"], "image"),
    (["lint", "--rule", "R1"], "lint"),
    (["diff", "before"], "diff"),
    (["talkback", "status"], "talkback"),
    (["tb-walk", "--prev", "--expect", "n3", "--expect", "n4,n5"], "tb_walk"),
    (["tb-scenario", "focus-after"], "tb_scenario"),
]


@pytest.mark.parametrize("argv,tool", _GENERATED)
def test_generated_subcommand_resolves_its_tool(argv, tool):
    ns = cli.build_parser().parse_args(argv)
    assert callable(ns.func) and ns.surface_tool == tool


def test_every_registered_subcommand_is_covered():
    """Every subparser the CLI registers has an entry in _SUBCOMMANDS (or
    _GENERATED), so a newly added subcommand can't slip past this smoke test
    unparsed."""
    parser = cli.build_parser()
    registered = _subparser_names(parser)
    covered = {argv[0] for argv, _ in _SUBCOMMANDS} | {argv[0] for argv, _ in _GENERATED}
    assert registered, "no subparsers registered on the parser"
    assert registered == covered, (
        f"subcommands not covered by the smoke test: {registered - covered}; "
        f"stale entries: {covered - registered}"
    )


def test_new_integrated_subcommands_present():
    parser = cli.build_parser()
    registered = _subparser_names(parser)
    missing = _NEW_SUBCOMMANDS - registered
    assert not missing, f"missing new subcommands: {sorted(missing)}"


def test_each_new_subcommand_parses_to_a_func():
    by_name = {argv[0]: handler for argv, handler in _SUBCOMMANDS}
    for name in _NEW_SUBCOMMANDS:
        argv = next(argv for argv, _ in _SUBCOMMANDS if argv[0] == name)
        ns = cli.build_parser().parse_args(argv)
        assert ns.func is getattr(cli, by_name[name])
