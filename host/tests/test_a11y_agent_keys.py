"""Cross-language check of the a11y node-key contract (agent A11yIds.kt <-> host a11y.py).

The agent emits A11yNode.traversal_before / traversal_after / label_for / labeled_by /
labeled_by_list as HOST NODE KEYS, computed on-device by A11yIds.hostKey:

    (host_view_id shl 32) xor (virtual_id.toLong() and 0xFFFFFFFF)

and the host keys every node with (host_view_id << 32) ^ (virtual_id & 0xFFFFFFFF).
The golden values below were produced by running the Kotlin A11yIds.hostKey on the JVM;
if either side changes its formula, this test catches the drift before a device does.
"""

from __future__ import annotations

import pytest

from inspector_widget import a11y
from inspector_widget import strings as st
from inspector_widget.proto import view_inspection_pb2 as pb

from conftest import make_a11y_node

# (host_view_id, virtual_id) -> A11yIds.hostKey(host_view_id, virtual_id), computed in Kotlin.
KOTLIN_HOST_KEYS = [
    (34, -1, 150323855359),      # real View: virtual_id = HOST_VIEW_ID
    (902, 12, 3874060501004),    # Compose virtual node: semantics id 12 under view 902
    (5, -7, 25769803769),        # negative virtual id (never HOST_VIEW_ID) keeps its low bits
]


@pytest.mark.parametrize("host_view_id,virtual_id,kotlin_key", KOTLIN_HOST_KEYS)
def test_host_key_matches_agent(strings_builder, host_view_id, virtual_id, kotlin_key):
    node = make_a11y_node(strings_builder, host_view_id=host_view_id, virtual_id=virtual_id)
    out = a11y.a11y_node_to_dict(node, st.StringResolver(strings_builder.build()))
    assert out["id"] == kotlin_key


def test_linkage_key_survives_the_wire_and_names_its_target(strings_builder):
    sb = strings_builder
    target = make_a11y_node(sb, host_view_id=902, virtual_id=12, bounds=(0, 0, 10, 10))
    source = make_a11y_node(
        sb, host_view_id=902, virtual_id=11, bounds=(0, 20, 10, 10),
        traversal_before=3874060501004,
    )
    source.label_for = 150323855359
    source.labeled_by_list.extend([3874060501004])
    root = make_a11y_node(sb, host_view_id=902, virtual_id=-1, children=[source, target])
    resp = pb.DumpA11yResponse(
        windows=[pb.DumpA11yResponse.Window(root_view_id=1, root=root)],
        strings=sb.build(),
    )
    wire = pb.DumpA11yResponse.FromString(resp.SerializeToString())
    out = a11y.a11y_to_dict(wire)
    kids = out["windows"][0]["root"]["children"]
    by_id = {k["id"]: k for k in kids}
    src = by_id[(902 << 32) ^ 11]
    assert src["traversal_before"] in by_id
    assert by_id[src["traversal_before"]]["virtual_id"] == 12
    assert src["label_for"] == 150323855359
    assert src["labeled_by_list"] == [3874060501004]
