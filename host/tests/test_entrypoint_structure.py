"""Entry-point structure guard: nothing may be DEFINED after the
``if __name__ == "__main__":`` guard in cli.py / mcp_server.py.

This catches a bug class that import-based tests cannot: a top-level ``def`` (or
``class``) placed *below* the ``__main__`` guard is defined fine on import (so the
whole test suite stays green), but when the script is actually RUN, the guard
calls ``main()`` before that ``def`` has executed — so a function ``main()`` calls
NameErrors at runtime. That is exactly how ``_log_startup_health`` (defined after
the guard, called inside ``main``'s serve path) made ``mcp_server.py`` crash on
every real start while ``--self-check`` (which returns before the call) looked
clean.
"""

from __future__ import annotations

import ast
import os

import pytest

_HOST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = ("cli.py", "mcp_server.py")


def _is_main_guard(node: ast.stmt) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


@pytest.mark.parametrize("script", _SCRIPTS)
def test_no_definitions_after_main_guard(script: str) -> None:
    path = os.path.join(_HOST_DIR, script)
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)

    guard_idx = next(
        (i for i, n in enumerate(tree.body) if _is_main_guard(n)), None
    )
    assert guard_idx is not None, f"{script}: no `if __name__ == '__main__':` guard found"

    defined_after = [
        f"{type(n).__name__} {getattr(n, 'name', '?')} @ line {n.lineno}"
        for n in tree.body[guard_idx + 1:]
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assert not defined_after, (
        f"{script}: these are defined AFTER the __main__ guard, so they are "
        f"undefined when the script runs and main() executes: {defined_after}. "
        f"Move them above the guard."
    )
