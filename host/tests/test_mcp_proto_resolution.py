"""Guard mcp_server's proto probe.

``_import_proto`` backs ``--self-check`` and the startup health line; it must
resolve to the package's generated bindings (there is no flat-layout fallback
any more). The tools themselves never import the proto module: they go through
``inspector_widget.strings`` and friends.
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


def test_the_parallel_decoder_and_compat_shims_are_gone():
    """E3 and the ledger's dead code: one decoder (strings), one PNG writer (png)."""
    for name in ("_node_to_json", "_property_to_json", "_property_group_to_json",
                 "_resource_to_json", "_bounds_to_json", "_color_hex", "_strings_to_map",
                 "_decode_screenshot_to_png", "_rgba_to_png", "_first_attr", "_lint_fn",
                 "_device_density"):
        assert not hasattr(mcp_server, name), name
