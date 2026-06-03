"""Tests for inspector_widget.strings — string-table resolution + tree shaping.

Builds hand-rolled protos (via the conftest builders / view_inspection_pb2) and
checks that every "*_id" int32 is correctly resolved against the Strings table and
that the nested-dict shapes match what the MCP tools / CLI emit.
"""

from __future__ import annotations

from inspector_widget import strings as st
from inspector_widget.proto import view_inspection_pb2 as pb

from conftest import make_a11y_node, make_compose_node, make_view_node


# --------------------------------------------------------------------------- #
# StringResolver
# --------------------------------------------------------------------------- #
def test_string_resolver_id_zero_is_empty(strings_builder):
    strings_builder.intern("hello")
    r = st.StringResolver(strings_builder.build())
    assert r.get(0) == ""
    assert r.opt(0) is None


def test_string_resolver_resolves_and_opt(strings_builder):
    sid = strings_builder.intern("Submit")
    r = st.StringResolver(strings_builder.build())
    assert r.get(sid) == "Submit"
    assert r.opt(sid) == "Submit"
    # Unknown id -> "" / None.
    assert r.get(9999) == ""
    assert r.opt(9999) is None


# --------------------------------------------------------------------------- #
# dump_tree_to_dict
# --------------------------------------------------------------------------- #
def test_dump_tree_to_dict_resolves_ids(strings_builder):
    sb = strings_builder
    child = make_view_node(
        sb, node_id=2, class_name="TextView", package_name="android.widget",
        bounds=(0, 10, 100, 20), text="Hi",
    )
    root = make_view_node(
        sb, node_id=1, class_name="FrameLayout", package_name="android.widget",
        bounds=(0, 0, 200, 400), view_id_name="root",
        flags=pb.ViewNode.IS_WEBVIEW, children=[child],
    )
    resp = pb.DumpTreeResponse(roots=[root], strings=sb.build())

    out = st.dump_tree_to_dict(resp)
    r0 = out["roots"][0]
    assert r0["id"] == 1
    assert r0["class_name"] == "FrameLayout"
    assert r0["qualified_name"] == "android.widget.FrameLayout"
    assert r0["view_id_name"] == "root"
    assert r0["bounds"]["layout"] == {"x": 0, "y": 0, "w": 200, "h": 400}
    assert "IS_WEBVIEW" in r0["flags"]
    c0 = r0["children"][0]
    assert c0["id"] == 2
    assert c0["text"] == "Hi"
    assert c0["class_name"] == "TextView"


def test_dump_tree_to_dict_screenshot_summary(strings_builder):
    sb = strings_builder
    root = make_view_node(sb, node_id=1, class_name="View")
    resp = pb.DumpTreeResponse(roots=[root], strings=sb.build())
    resp.screenshot.width = 100
    resp.screenshot.height = 200
    resp.screenshot.bitmap_type = 2
    resp.screenshot.scale = 1.0
    resp.screenshot.data = b"compressedbytes"
    out = st.dump_tree_to_dict(resp)
    assert out["screenshot"] == {
        "width": 100, "height": 200, "bitmap_type": 2, "scale": 1.0,
        "compressed_bytes": len(b"compressedbytes"),
    }


def test_dump_tree_to_dict_properties_keyed_by_view_id(strings_builder):
    sb = strings_builder
    root = make_view_node(sb, node_id=1, class_name="Button")
    resp = pb.DumpTreeResponse(roots=[root], strings=sb.build())
    # Build a property group with a STRING property after we know the string ids.
    grp = resp.properties.add()
    grp.view_id = 1
    p = grp.properties.add()
    p.name = sb.intern("text")
    p.type = pb.Property.STRING
    p.str_value = sb.intern("Click me")
    # Rebuild strings so the newly interned ids are present.
    resp.strings.CopyFrom(sb.build())

    out = st.dump_tree_to_dict(resp)
    assert 1 in out["properties"]
    prop = out["properties"][1][0]
    assert prop["name"] == "text"
    assert prop["type"] == "STRING"
    assert prop["value"] == "Click me"


# --------------------------------------------------------------------------- #
# property_to_dict value-field selection
# --------------------------------------------------------------------------- #
def test_property_to_dict_boolean_and_color(strings_builder):
    sb = strings_builder
    resolver_seed = sb.build()  # noqa: F841 (ensure table exists)
    pbool = pb.Property(name=sb.intern("enabled"), type=pb.Property.BOOLEAN, int32_value=1)
    pcolor = pb.Property(name=sb.intern("bgColor"), type=pb.Property.COLOR, int32_value=0xFF0000)
    resolver = st.StringResolver(sb.build())
    db = st.property_to_dict(resolver, pbool)
    dc = st.property_to_dict(resolver, pcolor)
    assert db["value"] is True
    assert dc["value"] == 0xFF0000


# --------------------------------------------------------------------------- #
# compose_node_to_dict / dump_compose_to_dict
# --------------------------------------------------------------------------- #
def test_compose_node_to_dict_attrs_and_kind(strings_builder):
    sb = strings_builder
    node = make_compose_node(
        sb, node_id=10, name="Button", kind=pb.ComposeNode.SEMANTICS,
        bounds=(5, 5, 48, 48), source="Foo.kt:12", render_node_id=777,
        attrs={"Role": "Button", "Text": "OK"},
    )
    resolver = st.StringResolver(sb.build())
    out = st.compose_node_to_dict(node, resolver)
    assert out["id"] == 10
    assert out["name"] == "Button"
    assert out["kind"] == "SEMANTICS"
    assert out["render_node_id"] == 777
    assert out["source"] == "Foo.kt:12"
    assert out["attrs"] == {"Role": "Button", "Text": "OK"}
    assert out["bounds"]["layout"] == {"x": 5, "y": 5, "w": 48, "h": 48}


def test_dump_compose_to_dict_windows(strings_builder):
    sb = strings_builder
    root = make_compose_node(sb, node_id=1, name="Column", kind=pb.ComposeNode.COMPOSABLE)
    resp = pb.DumpComposeResponse(strings=sb.build())
    w = resp.windows.add()
    w.view_id = 55
    w.root.CopyFrom(root)
    resp.strings.CopyFrom(sb.build())
    out = st.dump_compose_to_dict(resp)
    assert out["windows"][0]["view_id"] == 55
    assert out["windows"][0]["root"]["name"] == "Column"
    assert out["windows"][0]["root"]["kind"] == "COMPOSABLE"


# --------------------------------------------------------------------------- #
# a11y_to_dict id resolution (deeper action/order behavior lives in test_a11y)
# --------------------------------------------------------------------------- #
def test_a11y_to_dict_resolves_text_ids(strings_builder):
    sb = strings_builder
    from inspector_widget import a11y as a11ymod
    node = make_a11y_node(
        sb, host_view_id=100, virtual_id=0, bounds=(0, 0, 48, 48),
        content_description="Close", class_name="android.widget.ImageButton",
        bool_flags=["clickable", "visible_to_user"],
    )
    resp = pb.DumpA11yResponse(strings=sb.build())
    w = resp.windows.add()
    w.root_view_id = 100
    w.root.CopyFrom(node)
    resp.strings.CopyFrom(sb.build())

    out = a11ymod.a11y_to_dict(resp)
    root = out["windows"][0]["root"]
    assert root["host_view_id"] == 100
    assert root["content_description"] == "Close"
    assert root["speakable"] == "Close"
    assert root["class_name"] == "android.widget.ImageButton"
    assert "clickable" in root["flags"]
