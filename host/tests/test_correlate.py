"""Tests for inspector_widget.correlate — the integrated (view+compose+a11y) tree.

build_integrated_tree consumes the *already-shaped* dicts the host produces
(strings.dump_tree_to_dict roots, dump_compose_to_dict windows, a11y roots). We
build those dicts by hand and assert the correlation:
  * View by uniqueDrawingId == ViewNode.id (exact).
  * Compose grafted under its AndroidComposeView host; a11y matched by semantics id.
  * a11y by bounds-overlap (IoU) when no id match exists.

The final test runs a *shaped* DumpA11yResponse proto through a fake Session's
dump_a11y so _shaped_a11y's proto -> dict path is exercised end to end.
"""

from __future__ import annotations

from inspector_widget import correlate


# --------------------------------------------------------------------------- #
# Shaped-dict helpers.
# --------------------------------------------------------------------------- #
def view(node_id, cls, bounds, children=None):
    x, y, w, h = bounds
    return {
        "id": node_id,
        "class_name": cls,
        "qualified_name": cls,
        "bounds": {"layout": {"x": x, "y": y, "w": w, "h": h}},
        "children": children or [],
    }


def compose(cid, name, bounds, render_node_id=None, attrs=None, children=None):
    x, y, w, h = bounds
    d = {
        "id": cid,
        "name": name,
        "kind": "SEMANTICS",
        "attrs": attrs or {},
        "bounds": {"layout": {"x": x, "y": y, "w": w, "h": h}},
        "children": children or [],
    }
    if render_node_id is not None:
        d["render_node_id"] = render_node_id
    return d


def a11y_node(host_view_id, virtual_id, bounds, **kw):
    x, y, w, h = bounds
    d = {
        "host_view_id": host_view_id,
        "virtual_id": virtual_id,
        "bounds": {"layout": {"x": x, "y": y, "w": w, "h": h}},
    }
    d.update(kw)
    return d


# --------------------------------------------------------------------------- #
# View correlation by uniqueDrawingId.
# --------------------------------------------------------------------------- #
def test_view_a11y_exact_match_by_unique_drawing_id():
    v = view(100, "android.widget.Button", (0, 0, 100, 50))
    # Real-view a11y node: virtual_id == -1 sentinel keys it by host_view_id.
    a = a11y_node(100, -1, (0, 0, 100, 50), content_description="Press")
    merged = correlate.build_integrated_tree([v], [], [a])
    root = merged["roots"][0]
    assert root["node_key"] == "view:100"
    assert root["correlation_confidence"] == "exact"
    assert root["a11y"]["speakable"] == "Press"


def test_view_without_a11y_is_confidence_none():
    v = view(7, "android.widget.View", (0, 0, 10, 10))
    merged = correlate.build_integrated_tree([v], [], [])
    root = merged["roots"][0]
    assert root["correlation_confidence"] == "none"
    assert "a11y" not in root


# --------------------------------------------------------------------------- #
# Compose grafting + a11y by semantics id.
# --------------------------------------------------------------------------- #
def test_compose_grafted_under_host_view():
    host = view(200, "androidx.compose.ui.platform.AndroidComposeView", (0, 0, 400, 800))
    croot = compose(1, "Column", (0, 0, 400, 800),
                    children=[compose(2, "Button", (10, 10, 100, 48), render_node_id=555)])
    compose_windows = [{"view_id": 200, "root": croot}]
    merged = correlate.build_integrated_tree([host], compose_windows, [])
    root = merged["roots"][0]
    # The compose root is grafted as a child of the host view node.
    compose_child = root["children"][0]
    assert compose_child["node_key"] == "compose:1"
    assert compose_child["compose"]["name"] == "Column"
    # render_node_id becomes an SKP image_ref on the leaf.
    leaf = compose_child["children"][0]
    assert leaf["node_key"] == "compose:2"
    assert leaf["image_ref"] == {"source": "skp", "layer_id": 555}


def test_compose_a11y_matched_by_semantics_id():
    host = view(200, "androidx.compose.ui.platform.AndroidComposeView", (0, 0, 400, 800))
    croot = compose(42, "Button", (10, 10, 100, 48))
    compose_windows = [{"view_id": 200, "root": croot}]
    # Virtual a11y node whose virtual_id == compose semantics id 42.
    a = a11y_node(200, 42, (10, 10, 100, 48), is_virtual=True,
                  content_description="Save", clickable=True)
    merged = correlate.build_integrated_tree([host], compose_windows, [a])
    compose_child = merged["roots"][0]["children"][0]
    assert compose_child["node_key"] == "compose:42"
    assert compose_child["correlation_confidence"] == "exact"
    assert compose_child["a11y"]["speakable"] == "Save"


# --------------------------------------------------------------------------- #
# a11y overlap (IoU) fallback.
# --------------------------------------------------------------------------- #
def test_a11y_overlap_fallback():
    # View id 9 has no id-matched a11y node, but an a11y node with no usable id
    # overlaps its bounds heavily -> matched by IoU as "overlap".
    v = view(9, "android.widget.TextView", (0, 0, 100, 100))
    # virtual_id 0 + no is_virtual -> _a11y_view_key returns int(vid). Use a
    # different host id so the id key does NOT match view 9, forcing IoU.
    a = a11y_node(0, 0, (1, 1, 99, 99), text="Overlap me")
    merged = correlate.build_integrated_tree([v], [], [a])
    root = merged["roots"][0]
    assert root["correlation_confidence"] == "overlap"
    assert "a11y_iou" in root
    assert root["a11y_iou"] >= 0.6


# --------------------------------------------------------------------------- #
# summary counts.
# --------------------------------------------------------------------------- #
def test_summary_counts():
    host = view(200, "androidx.compose.ui.platform.AndroidComposeView", (0, 0, 400, 800),
                children=[view(201, "android.widget.Button", (0, 0, 100, 50))])
    croot = compose(1, "Text", (0, 0, 400, 800))
    a = a11y_node(201, -1, (0, 0, 100, 50), content_description="x")
    merged = correlate.build_integrated_tree([host], [{"view_id": 200, "root": croot}], [a])
    s = merged["summary"]
    assert s["view"] == 2          # host + button
    assert s["compose"] == 1       # grafted Text
    assert s["a11y"] == 1          # button matched exactly
    assert s["exact"] == 1


# --------------------------------------------------------------------------- #
# _shaped_a11y: a shaped DumpA11yResponse proto flows through to roots.
# --------------------------------------------------------------------------- #
class _FakeSession:
    def __init__(self, a11y_response):
        self._a11y = a11y_response

    def dump_a11y(self):
        return self._a11y


def test_shaped_a11y_proto_to_roots(strings_builder):
    from inspector_widget.proto import view_inspection_pb2 as pb
    from conftest import make_a11y_node

    sb = strings_builder
    root = make_a11y_node(sb, host_view_id=5, virtual_id=-1, bounds=(0, 0, 50, 50),
                          content_description="Hello", bool_flags=["visible_to_user"])
    resp = pb.DumpA11yResponse(strings=sb.build())
    w = resp.windows.add()
    w.root_view_id = 5
    w.root.CopyFrom(root)
    resp.strings.CopyFrom(sb.build())

    session = _FakeSession(resp)
    roots = correlate._shaped_a11y(session)
    assert len(roots) == 1
    assert roots[0]["host_view_id"] == 5
    assert roots[0]["content_description"] == "Hello"


def test_shaped_a11y_proto_feeds_build_integrated_tree(strings_builder):
    """End-to-end: the proto a11y root correlates against a matching view spine."""
    from inspector_widget.proto import view_inspection_pb2 as pb
    from conftest import make_a11y_node

    sb = strings_builder
    a_root = make_a11y_node(sb, host_view_id=300, virtual_id=-1, bounds=(0, 0, 80, 40),
                            content_description="Tap", bool_flags=["visible_to_user", "clickable"])
    resp = pb.DumpA11yResponse(strings=sb.build())
    win = resp.windows.add()
    win.root_view_id = 300
    win.root.CopyFrom(a_root)
    resp.strings.CopyFrom(sb.build())

    a11y_roots = correlate._shaped_a11y(_FakeSession(resp))
    v = view(300, "android.widget.Button", (0, 0, 80, 40))
    merged = correlate.build_integrated_tree([v], [], a11y_roots)
    root = merged["roots"][0]
    assert root["correlation_confidence"] == "exact"
    assert root["a11y"]["speakable"] == "Tap"
