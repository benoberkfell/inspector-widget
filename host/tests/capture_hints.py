"""Run the ``next`` hints the capture tools return, exactly as written.

A hint is a call in the form ``tool(arg, key=value, ...)`` with JSON values
(``query.call``). ``parse_call`` reads one back; ``run_hint`` executes it through
the offline pipeline (``tests/test_capture_pipeline_offline.Pipeline``), so a test
can check that every hint an agent might copy works verbatim.
"""

from __future__ import annotations

import ast
import re
from typing import Any

_JSON_NAMES = {"true": True, "false": False, "null": None}


def parse_call(text: str) -> tuple[str, list[Any], dict[str, Any]]:
    """``(tool, positional args, keyword args)`` of a hint; ``in=`` becomes ``in_``
    (find's keyword) and ``from=`` stays ``from`` (outline's)."""
    src = re.sub(r"([(,])(in|from)=", r"\1\2_=", text)
    tree = ast.parse(src, mode="eval").body
    if not isinstance(tree, ast.Call) or not isinstance(tree.func, ast.Name):
        raise ValueError(f"not a call: {text!r}")

    def value(node: ast.AST) -> Any:
        if isinstance(node, ast.Name):
            return _JSON_NAMES[node.id]
        if isinstance(node, ast.List):
            return [value(x) for x in node.elts]
        return ast.literal_eval(node)

    return (tree.func.id, [value(a) for a in tree.args],
            {("from" if k.arg == "from_" else k.arg): value(k.value) for k in tree.keywords})


def run_hint(pipe: Any, loaded: Any, text: str) -> dict:
    """Execute a hint against ``loaded`` (or the capture it names) in ``pipe``."""
    tool, pos, kw = parse_call(text)
    cap = kw.pop("capture", None)
    lc = pipe.store.load(pipe.store.resolve(cap)) if cap else loaded
    if tool == "outline":
        return pipe.outline(lc, **kw)
    if tool == "find":
        return pipe.find(lc, **kw)
    if tool == "node":
        return pipe.node(lc, pos[0] if pos else kw.pop("refs"), **kw)
    if tool == "image":
        return pipe.image(lc, kw.pop("ref", None), **kw)
    if tool == "lint":
        return pipe.lint(lc, **kw)
    if tool == "diff":
        a = pipe.store.load(pipe.store.resolve(kw.pop("a", "prev")))
        b = pipe.store.load(pipe.store.resolve(kw.pop("b", "latest")))
        return pipe.diff(a, b, **kw)
    raise ValueError(f"no offline runner for {tool}(): {text}")
