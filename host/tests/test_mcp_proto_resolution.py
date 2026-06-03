"""Guard mcp_server's lazy proto + lint resolution.

These two helpers are pure indirection layers that have silently broken before:
``_import_proto`` must resolve to the package's generated bindings (not a stale
flat-layout fallback), and ``_lint_fn`` must hand back the real callable lint
adapter (it used to return None when the lint moved modules).
"""

from __future__ import annotations

import mcp_server


def test_import_proto_resolves_to_package_bindings():
    proto = mcp_server._import_proto()
    assert proto.__name__ == "inspector_widget.proto.view_inspection_pb2"


def test_import_proto_exposes_core_messages():
    proto = mcp_server._import_proto()
    for symbol in ("Request", "Response", "A11yNode"):
        assert hasattr(proto, symbol), f"proto missing {symbol}"


def test_lint_fn_is_callable():
    fn = mcp_server._lint_fn()
    assert fn is not None, "_lint_fn() returned None (lint adapter not importable)"
    assert callable(fn)


def test_lint_fn_is_the_a11y_lint_adapter():
    from inspector_widget import a11y_lint

    assert mcp_server._lint_fn() is a11y_lint.lint_a11y
