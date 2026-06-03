"""Tests for the generated protobuf surface (view_inspection_pb2).

Builds each Request command envelope, round-trips it through
SerializeToString/ParseFromString, and verifies the Request ``command`` oneof and
the Response ``payload`` oneof discriminate correctly. This guards against a
gencode/runtime version mismatch silently breaking the wire format.
"""

from __future__ import annotations

import pytest

from inspector_widget.proto import view_inspection_pb2 as pb


# (request command field name, callable that populates the command on a Request)
COMMAND_BUILDERS = [
    ("hello", lambda r: r.hello.SetInParent()),
    ("get_windows", lambda r: r.get_windows.SetInParent()),
    ("dump_tree", lambda r: setattr(r.dump_tree, "root_id", 0)),
    ("get_properties", lambda r: setattr(r.get_properties, "view_id", 42)),
    ("screenshot", lambda r: setattr(r.screenshot, "scale", 0.5)),
    ("shutdown", lambda r: r.shutdown.SetInParent()),
    ("dump_compose", lambda r: setattr(r.dump_compose, "include_semantics", True)),
    ("capture_skp", lambda r: setattr(r.capture_skp, "root_id", 0)),
    ("dump_a11y", lambda r: setattr(r.dump_a11y, "include_extras", True)),
]


@pytest.mark.parametrize("field,populate", COMMAND_BUILDERS, ids=[c[0] for c in COMMAND_BUILDERS])
def test_request_command_roundtrip(field, populate):
    req = pb.Request(id=99)
    populate(req)
    assert req.WhichOneof("command") == field

    raw = req.SerializeToString()
    parsed = pb.Request()
    parsed.ParseFromString(raw)

    assert parsed.id == 99
    assert parsed.WhichOneof("command") == field
    assert parsed == req


def test_dump_tree_command_fields_survive_roundtrip():
    req = pb.Request(id=1)
    cmd = req.dump_tree
    cmd.root_id = 7
    cmd.include_properties = True
    cmd.include_resolution_stack = True
    cmd.include_screenshot = True
    cmd.screenshot_scale = 0.25

    parsed = pb.Request()
    parsed.ParseFromString(req.SerializeToString())
    out = parsed.dump_tree
    assert out.root_id == 7
    assert out.include_properties is True
    assert out.include_resolution_stack is True
    assert out.include_screenshot is True
    assert out.screenshot_scale == pytest.approx(0.25)


def test_response_payload_oneof_discriminates():
    """Each Response payload type sets exactly one oneof arm."""
    resp = pb.Response(id=5, status=pb.Response.OK)
    resp.hello.agent_version = "1.2.3"
    resp.hello.api_level = 36
    resp.hello.abi = "arm64-v8a"

    parsed = pb.Response()
    parsed.ParseFromString(resp.SerializeToString())
    assert parsed.id == 5
    assert parsed.status == pb.Response.OK
    assert parsed.WhichOneof("payload") == "hello"
    assert parsed.hello.agent_version == "1.2.3"
    assert parsed.hello.api_level == 36
    assert parsed.hello.abi == "arm64-v8a"


def test_response_error_status_roundtrip():
    resp = pb.Response(id=8, status=pb.Response.ERROR, error="boom")
    parsed = pb.Response()
    parsed.ParseFromString(resp.SerializeToString())
    assert parsed.status == pb.Response.ERROR
    assert parsed.error == "boom"
    # No payload arm should be set on a bare error.
    assert parsed.WhichOneof("payload") is None


def test_setting_second_command_clears_first():
    """A oneof keeps only the last-set arm."""
    req = pb.Request()
    req.dump_tree.root_id = 3
    assert req.WhichOneof("command") == "dump_tree"
    req.screenshot.scale = 1.0
    assert req.WhichOneof("command") == "screenshot"
    assert not req.HasField("dump_tree")


def test_nested_window_messages_roundtrip(strings_builder):
    """DumpA11yResponse + DumpComposeResponse carry distinct nested Window types."""
    a11y = pb.DumpA11yResponse()
    w = a11y.windows.add()
    w.root_view_id = 11
    w.root.host_view_id = 11
    a11y.strings.CopyFrom(strings_builder.build())

    parsed = pb.DumpA11yResponse()
    parsed.ParseFromString(a11y.SerializeToString())
    assert parsed.windows[0].root_view_id == 11
    assert parsed.windows[0].HasField("root")

    compose = pb.DumpComposeResponse()
    cw = compose.windows.add()
    cw.view_id = 22
    cw.root.id = 100
    parsed_c = pb.DumpComposeResponse()
    parsed_c.ParseFromString(compose.SerializeToString())
    assert parsed_c.windows[0].view_id == 22
    assert parsed_c.windows[0].root.id == 100


def test_screenshot_response_roundtrip():
    resp = pb.ScreenshotResponse()
    s = resp.screenshot
    s.width = 320
    s.height = 640
    s.bitmap_type = 2
    s.scale = 1.0
    s.data = b"\x00\x01\x02"
    parsed = pb.ScreenshotResponse()
    parsed.ParseFromString(resp.SerializeToString())
    assert parsed.screenshot.width == 320
    assert parsed.screenshot.height == 640
    assert parsed.screenshot.bitmap_type == 2
    assert parsed.screenshot.data == b"\x00\x01\x02"
