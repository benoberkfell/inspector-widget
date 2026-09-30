"""Deep trees on the wire: the agent's depth cap versus protobuf's parse limit.

Protobuf runtimes (upb, pure Python) stop at about 100 nested messages and then
reject the WHOLE message, so one very deep View/a11y/Compose branch used to make
an entire dump unreadable. The agent now cuts every tree at
``WireLimits.MAX_TREE_DEPTH`` levels (80) and flags the cut. These tests pin:

* the host parses (through the real framing + Client) and converts responses
  whose trees are 85 levels deep, with every nested sub-message populated at
  every level, i.e. more than the agent will ever send;
* the agent's cap leaves a real margin under the runtime's limit;
* the agent code actually applies that one constant to all three trees;
* the truncation markers come through the host's dict converters.
"""

from __future__ import annotations

import re
import socket
import threading
from pathlib import Path

import pytest
from google.protobuf.message import DecodeError

from inspector_widget import a11y, framing, strings
from inspector_widget.client import Client
from inspector_widget.proto import view_inspection_pb2 as pb

PAYLOAD_DIR = (Path(__file__).resolve().parents[2] / "agent" / "src" / "main" / "kotlin"
               / "com" / "oberkfell" / "viewspector" / "agent" / "payload")

HOST_DEPTH = 85  # deeper than the agent's cap, still well inside the parse limit


def agent_depth_cap() -> int:
    src = (PAYLOAD_DIR / "WireLimits.kt").read_text()
    m = re.search(r"const val MAX_TREE_DEPTH = (\d+)", src)
    assert m, "WireLimits.MAX_TREE_DEPTH not found"
    return int(m.group(1))


# --------------------------------------------------------------------------- #
# Deep responses, every per-node sub-message populated (the deepest nesting)
# --------------------------------------------------------------------------- #
def _fill_bounds(b: "pb.Bounds", i: int) -> None:
    b.layout.x, b.layout.y, b.layout.w, b.layout.h = i, i, 100 + i, 50 + i
    b.render.x0, b.render.y0, b.render.x2, b.render.y2 = i, i, 100 + i, 50 + i


def deep_view_response(depth: int) -> "pb.Response":
    r = pb.Response(id=1)
    st = r.dump_tree.strings.entries
    st.add(id=1, str="FrameLayout")
    st.add(id=2, str="leaf")
    node = r.dump_tree.roots.add()
    for i in range(depth):
        node.id = i + 1
        node.class_name = 1
        _fill_bounds(node.bounds, i)
        node.resource.type = node.resource.name = 1
        node.layout_resource.type = 1
        if i == depth - 1:
            node.text_value = 2
            break
        node = node.children.add()
    return r


def deep_a11y_response(depth: int) -> "pb.Response":
    r = pb.Response(id=1)
    r.dump_a11y.strings.entries.add(id=1, str="leaf")
    node = r.dump_a11y.windows.add(root_view_id=1).root
    for i in range(depth):
        node.host_view_id = 1000 + i
        node.virtual_id = -1
        node.visible_to_user = True
        _fill_bounds(node.bounds, i)
        node.collection_info.row_count = 1
        node.collection_item_info.row_index = i
        node.range_info.max = 1.0
        node.actions.add(id=16)
        node.extras.add(key=1, value=1)
        if i == depth - 1:
            node.text = 1
            break
        node = node.children.add()
    return r


def deep_compose_response(depth: int) -> "pb.Response":
    r = pb.Response(id=1)
    r.dump_compose.strings.entries.add(id=1, str="Text")
    r.dump_compose.strings.entries.add(id=2, str="leaf")
    node = r.dump_compose.windows.add(view_id=1).root
    for i in range(depth):
        node.id = i + 1
        node.name = 1
        _fill_bounds(node.bounds, i)
        node.attrs.add(key=1, value=2)
        if i == depth - 1:
            break
        node = node.children.add()
    return r


BUILDERS = {
    "view": deep_view_response,
    "a11y": deep_a11y_response,
    "compose": deep_compose_response,
}


def parses(raw: bytes) -> bool:
    try:
        pb.Response().ParseFromString(raw)
        return True
    except DecodeError:
        return False


def _depth_of(d: dict) -> int:
    n = 0
    while d is not None:
        n += 1
        kids = d.get("children") or []
        d = kids[0] if kids else None
    return n


def _serve_once(sock: socket.socket, response: "pb.Response") -> None:
    """A one-request agent stand-in: read the request, answer with ``response``."""
    req = pb.Request()
    req.ParseFromString(framing.read_message(sock))
    response.id = req.id
    framing.write_message(sock, response.SerializeToString())


def _through_client(response: "pb.Response", call):
    host_end, agent_end = socket.socketpair()
    t = threading.Thread(target=_serve_once, args=(agent_end, response), daemon=True)
    t.start()
    client = Client(host_end, timeout=10)
    try:
        return call(client)
    finally:
        client.close()
        t.join(5)
        agent_end.close()


# --------------------------------------------------------------------------- #
# The host copes with an 85-deep tree (end to end)
# --------------------------------------------------------------------------- #
def test_host_parses_85_deep_view_tree_through_the_client():
    resp = _through_client(deep_view_response(HOST_DEPTH), lambda c: c.dump_tree())
    out = strings.dump_tree_to_dict(resp)
    assert _depth_of(out["roots"][0]) == HOST_DEPTH
    leaf = out["roots"][0]
    while leaf.get("children"):
        leaf = leaf["children"][0]
    assert leaf["text"] == "leaf"


def test_host_parses_85_deep_a11y_tree_through_the_client():
    resp = _through_client(deep_a11y_response(HOST_DEPTH), lambda c: c.dump_a11y())
    out = a11y.a11y_to_dict(resp)
    assert _depth_of(out["windows"][0]["root"]) == HOST_DEPTH


def test_host_parses_85_deep_compose_tree_through_the_client():
    resp = _through_client(deep_compose_response(HOST_DEPTH), lambda c: c.dump_compose())
    out = strings.dump_compose_to_dict(resp)
    assert _depth_of(out["windows"][0]["root"]) == HOST_DEPTH


# --------------------------------------------------------------------------- #
# The agent's cap vs the runtime's limit
# --------------------------------------------------------------------------- #
def _deepest_parseable(kind: str, ceiling: int = 400) -> int:
    lo, hi = 1, ceiling  # parses(lo) holds; find the last depth that parses
    if parses(BUILDERS[kind](hi).SerializeToString()):
        return ceiling
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if parses(BUILDERS[kind](mid).SerializeToString()):
            lo = mid
        else:
            hi = mid
    return lo


@pytest.mark.parametrize("kind", sorted(BUILDERS))
def test_agent_cap_leaves_margin_under_the_parse_limit(kind):
    cap = agent_depth_cap()
    assert parses(BUILDERS[kind](cap).SerializeToString())
    deepest = _deepest_parseable(kind)
    if deepest >= 400:
        pytest.skip("this protobuf runtime has no recursion limit")
    # The measured limit (about 96 nodes under the envelopes) must clear the cap by a
    # margin, so a sub-message added to a node later does not break every deep dump.
    assert deepest >= cap + 10, (kind, deepest, cap)
    # And a tree past the limit really is unparseable as a whole: why the cap exists.
    assert not parses(BUILDERS[kind](deepest + 1).SerializeToString())


def test_the_agent_applies_the_cap_to_every_tree():
    cap = agent_depth_cap()
    assert 40 <= cap <= HOST_DEPTH
    # View tree: TreeBuilder stops at the cap (roots are depth 1).
    tree = (PAYLOAD_DIR / "TreeBuilder.kt").read_text()
    assert "depth >= WireLimits.MAX_TREE_DEPTH" in tree
    # a11y walk: 0-based depth, children walked while depth < MAX_DEPTH.
    a11y_src = (PAYLOAD_DIR / "AccessibilityInspector.kt").read_text()
    assert "MAX_DEPTH = WireLimits.MAX_TREE_DEPTH - 1" in a11y_src
    # Compose: semantics nodes sit under the synthetic AndroidComposeView root.
    compose = (PAYLOAD_DIR / "ComposeInspector.kt").read_text()
    assert "SEMANTICS_MAX_DEPTH = WireLimits.MAX_TREE_DEPTH - 2" in compose
    # The slot table has its own, shallower cap (raw groups, under the same root).
    slot = re.search(r"SLOT_MAX_DEPTH = (\d+)", compose)
    if slot:
        assert int(slot.group(1)) + 2 <= cap


# --------------------------------------------------------------------------- #
# Truncation markers reach the host's dicts
# --------------------------------------------------------------------------- #
def test_view_truncation_flag_and_diagnostics_surface():
    resp = deep_view_response(3).dump_tree
    last = resp.roots[0].children[0].children[0]
    last.flags |= pb.ViewNode.CHILDREN_TRUNCATED
    resp.diagnostics = "depth-truncated=1 (children below 80 levels not sent)"
    out = strings.dump_tree_to_dict(resp)
    leaf = out["roots"][0]["children"][0]["children"][0]
    assert "CHILDREN_TRUNCATED" in leaf["flags"]
    assert out["diagnostics"].startswith("depth-truncated=1")


def test_a11y_truncation_flag_surfaces():
    resp = deep_a11y_response(2).dump_a11y
    resp.windows[0].root.children[0].children_truncated = True
    out = a11y.a11y_to_dict(resp)
    child = out["windows"][0]["root"]["children"][0]
    assert "children_truncated" in child["flags"]
