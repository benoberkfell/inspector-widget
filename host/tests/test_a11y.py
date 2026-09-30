"""Tests for inspector_widget.a11y — action-id decoding + TalkBack traversal order.

Covers:
  * action_name() decoding of standard + R.id-backed + custom action ids.
  * a11y_node_to_dict() action shaping (id -> name) over a real proto.
  * compute_traversal_order(): geometry order, traversal_before/after constraints,
    and focus-stop numbering on synthetic nodes.
"""

from __future__ import annotations

from inspector_widget import a11y
from inspector_widget.proto import view_inspection_pb2 as pb

from conftest import make_a11y_node, packed_a11y_id


# --------------------------------------------------------------------------- #
# action_name / action-id decoding
# --------------------------------------------------------------------------- #
def test_action_name_standard_ids():
    assert a11y.action_name(0x00000010) == "CLICK"
    assert a11y.action_name(0x00000020) == "LONG_CLICK"
    assert a11y.action_name(0x00001000) == "SCROLL_FORWARD"
    assert a11y.action_name(0x00002000) == "SCROLL_BACKWARD"
    assert a11y.action_name(0x00000040) == "ACCESSIBILITY_FOCUS"


def test_action_name_rid_backed_ids():
    # android.R.id.accessibilityAction* values (from the SDK's android.jar).
    assert a11y.action_name(0x01020036) == "SHOW_ON_SCREEN"
    assert a11y.action_name(0x01020038) == "SCROLL_UP"
    assert a11y.action_name(0x0102003A) == "SCROLL_DOWN"
    assert a11y.action_name(0x0102005E) == "SCROLL_IN_DIRECTION"


def test_action_name_custom_prefers_label():
    # Unknown id with a label -> label; without -> CUSTOM_0x....
    assert a11y.action_name(0x7F010001, "Mark as read") == "Mark as read"
    assert a11y.action_name(0x7F010001) == "CUSTOM_0x7F010001"


def test_a11y_node_to_dict_decodes_actions(strings_builder):
    sb = strings_builder
    node = make_a11y_node(
        sb, host_view_id=1, virtual_id=0, bounds=(0, 0, 48, 48),
        actions=[(0x00000010, None), (0x00001000, None), (0x7F010002, "Archive")],
    )
    from inspector_widget import strings as st
    resolver = st.StringResolver(sb.build())
    out = a11y.a11y_node_to_dict(node, resolver)
    names = [a["name"] for a in out["actions"]]
    assert names == ["CLICK", "SCROLL_FORWARD", "Archive"]
    # Custom action keeps its label too.
    assert out["actions"][2]["label"] == "Archive"


def test_a11y_node_to_dict_int_enum_decode(strings_builder):
    sb = strings_builder
    node = make_a11y_node(
        sb, host_view_id=1, bounds=(0, 0, 10, 10),
        int_fields={"checked_state": 2, "live_region": 1},  # PARTIAL / POLITE
    )
    from inspector_widget import strings as st
    resolver = st.StringResolver(sb.build())
    out = a11y.a11y_node_to_dict(node, resolver)
    assert out["checked_state"] == "PARTIAL"
    assert out["live_region"] == "POLITE"


# --------------------------------------------------------------------------- #
# Traversal order — ANI child order (the platform already sorted it)
# --------------------------------------------------------------------------- #
def _stop(node_id, host, virtual, x, y, w=40, h=40, speakable="t"):
    """A focus-stop node dict (visible + screen_reader_focusable + text)."""
    return {
        "id": node_id,
        "host_view_id": host,
        "virtual_id": virtual,
        "text": speakable,
        "speakable": speakable,
        "bounds": {"layout": {"x": x, "y": y, "w": w, "h": h}},
        "flags": ["visible_to_user", "screen_reader_focusable"],
    }


def test_traversal_order_keeps_ani_child_order():
    # Children arrive in ANI order a, b, c although b is geometrically first: the host
    # must not re-sort (the framework / Compose delegate already ordered them).
    a = _stop(1, 1, 1, x=0, y=200)
    b = _stop(2, 1, 2, x=0, y=0)
    c = _stop(3, 1, 3, x=0, y=100)
    root = {
        "id": 0, "host_view_id": 1, "virtual_id": -1,
        "bounds": {"layout": {"x": 0, "y": 0, "w": 200, "h": 400}},
        "flags": ["visible_to_user"],
        "children": [a, b, c],
    }
    walk = a11y.compute_traversal_order([root])
    stops = [e for e in walk if e["is_focus_stop"]]
    assert [e["id"] for e in stops] == [1, 2, 3]
    assert [e["order"] for e in stops] == [1, 2, 3]


def test_traversal_order_rtl_row_is_not_reversed():
    # An RTL row: ANI order puts the right-hand node first; geometry would flip it.
    right = _stop(1, 1, 1, x=100, y=0)
    left = _stop(2, 1, 2, x=0, y=5)
    root = {
        "id": 0, "host_view_id": 1, "virtual_id": -1,
        "bounds": {"layout": {"x": 0, "y": 0, "w": 200, "h": 100}},
        "flags": ["visible_to_user"],
        "children": [right, left],
    }
    walk = a11y.compute_traversal_order([root])
    stops = [e["id"] for e in walk if e["is_focus_stop"]]
    assert stops == [1, 2]


# --------------------------------------------------------------------------- #
# Traversal order — traversal_before / traversal_after
# --------------------------------------------------------------------------- #
def test_traversal_before_pulls_node_earlier():
    # ANI order is a, b. traversal_before on b targeting a flips it.
    a = _stop(11, 1, 11, x=0, y=0)
    b = _stop(12, 1, 12, x=0, y=100)
    b["traversal_before"] = a["id"]  # b must come before a
    root = {
        "id": 0, "host_view_id": 1, "virtual_id": -1,
        "bounds": {"layout": {"x": 0, "y": 0, "w": 200, "h": 400}},
        "flags": ["visible_to_user"],
        "children": [a, b],
    }
    walk = a11y.compute_traversal_order([root])
    stops = [e["id"] for e in walk if e["is_focus_stop"]]
    assert stops == [12, 11]


def test_traversal_after_pushes_node_later():
    a = _stop(21, 1, 21, x=0, y=0)
    b = _stop(22, 1, 22, x=0, y=100)
    # a.traversal_after = b  => a must come AFTER b, flipping geometry order.
    a["traversal_after"] = b["id"]
    root = {
        "id": 0, "host_view_id": 1, "virtual_id": -1,
        "bounds": {"layout": {"x": 0, "y": 0, "w": 200, "h": 400}},
        "flags": ["visible_to_user"],
        "children": [a, b],
    }
    walk = a11y.compute_traversal_order([root])
    stops = [e["id"] for e in walk if e["is_focus_stop"]]
    assert stops == [22, 21]


def test_traversal_packed_ids_match_proto_synthetic_id(strings_builder):
    """The packed-id helper matches a11y_node_to_dict's synthetic 'id' so that a
    traversal_before referencing host/virtual resolves to a real sibling."""
    sb = strings_builder
    # virtual_id chosen to exercise the XOR/mask packing.
    node = make_a11y_node(sb, host_view_id=3, virtual_id=9,
                          bounds=(0, 0, 10, 10), content_description="x")
    from inspector_widget import strings as st
    resolver = st.StringResolver(sb.build())
    out = a11y.a11y_node_to_dict(node, resolver)
    assert out["id"] == packed_a11y_id(3, 9)


def test_structural_node_not_counted_as_stop():
    # A pure container (no content, not screen-reader-focusable) is in the walk
    # but is_focus_stop=False and order=None.
    container = {
        "id": 100, "host_view_id": 1, "virtual_id": -1,
        "bounds": {"layout": {"x": 0, "y": 0, "w": 200, "h": 200}},
        "flags": ["visible_to_user"],
        "children": [_stop(1, 1, 1, x=0, y=0)],
    }
    walk = a11y.compute_traversal_order([container])
    by_id = {e["id"]: e for e in walk}
    assert by_id[100]["is_focus_stop"] is False
    assert by_id[100]["order"] is None
    assert by_id[1]["is_focus_stop"] is True
    assert by_id[1]["order"] == 1
