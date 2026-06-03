"""AST symbol-parity guard: every attribute the CLI / MCP server reads off a
host submodule must actually exist on that imported module.

This is the highest-leverage regression guard for the whole bug class that this
test pass targets: it statically scans ``cli.py`` and ``mcp_server.py`` for every
``<module>.<attr>`` access on the inspector_widget submodules they import
(adb / a11y / a11y_lint / overlay / png / correlate / strings) and asserts the
referenced attribute is a real member of the imported module.

It would have caught, in one shot:
  * ``adb.display_density`` / ``adb.font_scale`` missing from adb.py
  * ``a11y.lint_a11y`` (the lint moved to a11y_lint.lint_a11y)
  * ``overlay.render_integrated_overlay`` missing from overlay.py

The scan is import-alias aware: it handles ``from inspector_widget import adb``,
``... import overlay as ovmod``, grouped/parenthesised function-scope imports
(``from inspector_widget import (a11y as a11ymod, strings as st, ...)``), and the
bare ``import inspector_widget`` package handle.
"""

from __future__ import annotations

import ast
import importlib
import os
from typing import Dict, List, Set, Tuple

import pytest

# host/ is conftest's _HOST_DIR (already on sys.path); these two scripts live there.
_HOST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The inspector_widget submodules whose attribute access we police. Anything
# imported from inspector_widget that is NOT in this set is ignored (e.g.
# ``inject``, ``client``) so the guard stays focused on the shared-contract
# surface without being brittle about unrelated helpers.
_POLICED_SUBMODULES = {
    "adb", "a11y", "a11y_lint", "overlay", "png", "correlate", "strings",
}

_SCRIPTS = ("cli.py", "mcp_server.py")


def _load_module_members(submodule: str) -> Set[str]:
    mod = importlib.import_module(f"inspector_widget.{submodule}")
    return set(dir(mod))


class _AliasResolver(ast.NodeVisitor):
    """Walk a module AST, tracking which local names are bound to a policed
    inspector_widget submodule, and collect every attribute access on them.

    Alias bindings are collected module-wide first (a single pass), which keeps
    the resolver simple and is sufficient here because these scripts never rebind
    a module alias to something else. ``import inspector_widget [as iw]`` is
    tracked separately so ``iw.attach`` etc. is NOT flagged (the package, not a
    policed submodule).
    """

    def __init__(self) -> None:
        # local alias name -> policed submodule name (e.g. "ovmod" -> "overlay")
        self.alias_to_submodule: Dict[str, str] = {}
        # local names bound to the inspector_widget package itself
        self.package_aliases: Set[str] = set()
        # (local_alias, attr, submodule, lineno)
        self.accesses: List[Tuple[str, str, str, int]] = []

    # -- binding collection ------------------------------------------------- #
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.name
            local = alias.asname or name.split(".")[0]
            if name == "inspector_widget":
                self.package_aliases.add(local)
            elif name.startswith("inspector_widget."):
                leaf = name.split(".")[-1]
                if leaf in _POLICED_SUBMODULES:
                    self.alias_to_submodule[alias.asname or leaf] = leaf
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "inspector_widget":
            for alias in node.names:
                if alias.name in _POLICED_SUBMODULES:
                    local = alias.asname or alias.name
                    self.alias_to_submodule[local] = alias.name
        self.generic_visit(node)

    # -- access collection -------------------------------------------------- #
    def visit_Attribute(self, node: ast.Attribute) -> None:
        # Only direct ``Name.attr`` accesses; nested ``a.b.c`` recurse via
        # generic_visit so the inner ``a.b`` is examined on its own.
        if isinstance(node.value, ast.Name):
            self.accesses.append((node.value.id, node.attr, "", node.lineno))
        self.generic_visit(node)


def _scan_script(path: str) -> Tuple[_AliasResolver, List[Tuple[str, str, str, int]]]:
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    resolver = _AliasResolver()
    resolver.visit(tree)
    # Keep only accesses whose Name is a policed-submodule alias.
    resolved = [
        (alias, attr, resolver.alias_to_submodule[alias], lineno)
        for (alias, attr, _sub, lineno) in resolver.accesses
        if alias in resolver.alias_to_submodule
    ]
    return resolver, resolved


@pytest.mark.parametrize("script", _SCRIPTS)
def test_every_module_attr_access_exists(script: str) -> None:
    path = os.path.join(_HOST_DIR, script)
    assert os.path.isfile(path), f"expected to find {path}"
    _resolver, accesses = _scan_script(path)

    members_cache: Dict[str, Set[str]] = {}
    missing: List[str] = []
    for alias, attr, submodule, lineno in accesses:
        if submodule not in members_cache:
            members_cache[submodule] = _load_module_members(submodule)
        if attr not in members_cache[submodule]:
            missing.append(
                f"{script}:{lineno}: {alias}.{attr} -> "
                f"inspector_widget.{submodule} has no attribute '{attr}'"
            )

    assert not missing, (
        "Attribute(s) referenced in the script do not exist on the imported "
        "inspector_widget submodule:\n  " + "\n  ".join(missing)
    )


def test_scan_actually_finds_contract_symbols() -> None:
    """Guard the guard: prove the scan resolves the exact symbols the shared
    contract introduced, so a future refactor can't silently neuter this test
    into asserting over an empty access list.
    """
    found: Set[Tuple[str, str]] = set()
    for script in _SCRIPTS:
        _resolver, accesses = _scan_script(os.path.join(_HOST_DIR, script))
        for _alias, attr, submodule, _lineno in accesses:
            found.add((submodule, attr))

    expected = {
        ("adb", "display_density"),
        ("adb", "font_scale"),
        ("a11y_lint", "lint_a11y"),
        ("overlay", "render_integrated_overlay"),
        ("png", "_decode_to_rgba"),
    }
    not_seen = expected - found
    assert not not_seen, (
        "symbol-parity scan failed to even observe these contract symbols "
        f"(scanner regression?): {sorted(not_seen)}"
    )
