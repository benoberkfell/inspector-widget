"""RO1: the host reproduces TalkBack's reading order and announcements.

Covers the ID-contract keys on a11y nodes, traversal_before/after applied across the whole
tree (TalkBack's OrderedTraversalController), cycle / dangling-target diagnostics,
is_traversal_group, and TalkBack's focus-stop rules + speech composition — on hand-built
trees and on the mixed View/Compose fixture (3 ComposeView cells in a RecyclerView, a View
cell, an AndroidView hosting a RecyclerView of ComposeViews).
"""

from __future__ import annotations

import pytest

from inspector_widget import a11y
from inspector_widget.a11y import a11y_key

import mixed_fixture as mf

VIS = ["visible_to_user", "enabled"]
FOCUS = VIS + ["clickable", "focusable", "screen_reader_focusable"]


def n(host, virtual, *, cls="android.view.View", text=None, cd=None, flags=VIS,
      children=None, bounds=(0, 0, 10, 10), **kw):
    x, y, w, h = bounds
    d = {"host_view_id": host, "virtual_id": virtual, "id": a11y_key(host, virtual),
         "class_name": cls, "flags": list(flags),
         "bounds": {"layout": {"x": x, "y": y, "w": w, "h": h}}}
    if text:
        d["text"] = text
    if cd:
        d["content_description"] = cd
    d.update(kw)
    if children:
        d["children"] = children
    return d


def speech(roots):
    return [e["speak"] for e in a11y.reading_order(roots)["focus_order"]]


def keys(roots):
    return [e["id"] for e in a11y.reading_order(roots)["focus_order"]]


# --------------------------------------------------------------------------- keys
def test_node_keys_follow_the_contract():
    d = a11y.a11y_to_dict(mf.a11y_response())
    root = d["windows"][0]["root"]
    assert root["node_key"] == "view:2"
    assert root["id"] == a11y_key(2, -1)
    stops = {e["key"] for e in d["focus_order"]}
    # Compose virtual nodes are keyed by their own ComposeView (provider host) ...
    assert {"compose:22:3", "compose:32:3", "compose:42:3"} <= stops
    # ... real Views by their own uniqueDrawingId, not the window root's.
    assert {"view:11", "view:50", "view:52"} <= stops


def test_virtual_nodes_of_non_compose_provider_keep_virtual_prefix():
    web = n(70, -1, cls="android.webkit.WebView", provider_class="WebViewProvider",
            children=[n(70, 5, text="Link")])
    a11y.assign_node_keys([web])
    assert web["children"][0]["node_key"] == "virtual:70:5"


@pytest.mark.parametrize("key,expected", [
    ("view:12", ("view", 12)),
    ("composeview:87", ("composeview", 87)),
    ("compose:87:74", ("virt", 87, 74)),
    ("virtual:87:74", ("virt", 87, 74)),
    ("compose:74", ("bare", 74)),
])
def test_parse_node_key(key, expected):
    assert a11y.parse_node_key(key) == expected


@pytest.mark.parametrize("bad", ["", "view:", "view:x", "window:3", "compose:1:2:3", 12])
def test_parse_node_key_rejects_garbage(bad):
    with pytest.raises(ValueError):
        a11y.parse_node_key(bad)


# --------------------------------------------------------------------------- mixed fixture
def test_mixed_screen_speech_matches_talkback():
    d = a11y.a11y_to_dict(mf.a11y_response())
    assert [e["speak"] for e in d["focus_order"]] == mf.EXPECTED_SPEECH
    assert [e["order"] for e in d["focus_order"]] == list(range(1, len(mf.EXPECTED_SPEECH) + 1))
    assert d["summary"]["focus_stops"] == len(mf.EXPECTED_SPEECH)


def test_focus_order_is_compact_and_cross_referenced_in_tree():
    d = a11y.a11y_to_dict(mf.a11y_response())
    for e in d["focus_order"]:
        # keys + short labels only; the tree carries bounds and everything else.
        assert set(e) <= {"order", "key", "id", "speak", "unlabeled", "window"}
    by_key = {}
    stack = [w["root"] for w in d["windows"]]
    while stack:
        node = stack.pop()
        by_key[node["node_key"]] = node
        stack.extend(node.get("children", []))
    for e in d["focus_order"]:
        assert by_key[e["key"]]["order"] == e["order"]
    # Non-stops carry no order.
    assert "order" not in by_key["view:20"]  # the RecyclerView itself says nothing


def test_dangling_traversal_target_is_diagnosed():
    d = a11y.a11y_to_dict(mf.a11y_response())
    diags = d["reading_order_diagnostics"]
    unresolved = [x for x in diags if x["kind"] == "unresolved_target"]
    assert unresolved and unresolved[0]["items"][0]["key"] == "compose:61:2"


# --------------------------------------------------------------------------- focus stops
def test_merged_icon_button_is_one_stop_with_role():
    # The live IconButton shape: a clickable node, its Icon's contentDescription on a
    # non-focusable child, and Compose's fake Role node appended last.
    btn = n(7, 157, flags=FOCUS, children=[
        n(7, 158, cd="Add to favorites"),
        n(7, 1_000_000_157, cls="android.widget.Button"),
    ])
    root = n(7, -1, children=[btn])
    order = a11y.reading_order([root])["focus_order"]
    assert [e["speak"] for e in order] == ["Add to favorites, button"]
    assert order[0]["id"] == a11y_key(7, 157)


def test_text_inside_focusable_is_not_a_separate_stop():
    btn = n(1, 2, cls="android.widget.Button", flags=FOCUS,
            children=[n(1, 3, text="Continue", cls="android.widget.TextView")])
    assert speech([n(1, -1, children=[btn])]) == ["Continue, button"]


def test_text_without_focusable_ancestor_is_a_stop():
    assert speech([n(1, -1, children=[n(2, -1, text="Hello")])]) == ["Hello"]


def test_focusable_with_visible_children_but_nothing_to_speak_is_skipped():
    empty = n(1, 2, flags=FOCUS, children=[n(1, 3)])
    assert speech([n(1, -1, children=[empty])]) == []


def test_focusable_leaf_without_label_is_unlabeled():
    order = a11y.reading_order([n(1, -1, children=[
        n(5, -1, cls="android.widget.ImageButton", flags=FOCUS)])])["focus_order"]
    assert order[0]["speak"] == "Unlabeled, button"
    assert order[0]["unlabeled"] is True


def test_invisible_nodes_are_not_stops():
    hidden = n(2, -1, text="Hidden", flags=["enabled"])
    assert speech([n(1, -1, children=[hidden])]) == []


def test_content_description_wins_over_children():
    card = n(1, 2, flags=FOCUS, cd="Album, 12 songs",
             children=[n(1, 3, text="Album"), n(1, 4, text="12 songs")])
    assert speech([n(1, -1, children=[card])]) == ["Album, 12 songs"]


def test_labeled_by_names_an_edit_box():
    label = n(10, -1, text="Email", cls="android.widget.TextView")
    field = n(11, -1, cls="android.widget.EditText", flags=VIS + ["focusable", "editable"],
              labeled_by=a11y_key(10, -1))
    out = speech([n(1, -1, children=[label, field])])
    assert out == ["Email", "Email, edit box"]


def test_top_level_list_item_view_cell_reads_its_texts():
    cell = n(50, -1, cls="android.widget.LinearLayout", children=[
        n(51, -1, text="Title", cls="android.widget.TextView"),
        n(52, -1, text="Subtitle", cls="android.widget.TextView"),
        n(53, -1, cls="android.widget.ImageButton", flags=FOCUS, cd="More")])
    rv = n(20, -1, cls="androidx.recyclerview.widget.RecyclerView",
           flags=VIS + ["focusable", "scrollable"], children=[cell])
    assert speech([n(1, -1, children=[rv])]) == ["Title, Subtitle", "More, button"]


def test_checkbox_and_switch_states():
    cb = n(1, 2, cls="android.widget.CheckBox", flags=FOCUS + ["checkable", "checked"],
           text="Accept")
    sw = n(1, 3, cls="android.widget.Switch", flags=FOCUS + ["checkable"], text="Wi-Fi")
    assert speech([n(1, -1, children=[cb, sw])]) == [
        "Accept, checkbox, checked", "Wi-Fi, switch, off"]


# --------------------------------------------------------------------------- traversal
def test_compose_traversal_chain_is_realised():
    # Visual/ANI order Third, Second, First; Compose chains First -> Second -> Third
    # through traversal_before (the auditor's exp #2).
    third = n(1, 201, text="3. Third", flags=FOCUS)
    second = n(1, 202, text="2. Second", flags=FOCUS)
    first = n(1, 203, text="1. First", flags=FOCUS)
    first["traversal_before"] = second["id"]
    second["traversal_before"] = third["id"]
    col = n(1, -1, children=[third, second, first])
    assert speech([col]) == ["1. First", "2. Second", "3. Third"]


def test_traversal_before_applies_across_parents():
    l_title = n(1, 302, text="L-title", flags=FOCUS)
    l_body = n(1, 303, text="L-body", flags=FOCUS)
    r_title = n(1, 305, text="R-title", flags=FOCUS)
    l_body["traversal_after"] = r_title["id"]
    left = n(1, 301, children=[l_title, l_body])
    right = n(1, 304, children=[r_title])
    root = n(1, -1, children=[left, right])
    assert speech([root]) == ["L-title", "R-title", "L-body"]


def test_cycle_is_reported_and_ignored():
    a = n(1, 1, text="A", flags=FOCUS)
    b = n(1, 2, text="B", flags=FOCUS)
    a["traversal_before"] = b["id"]
    b["traversal_before"] = a["id"]
    ro = a11y.reading_order([n(1, -1, children=[a, b])])
    assert [e["speak"] for e in ro["focus_order"]] == ["A", "B"]  # ANI order kept
    cycles = [d for d in ro["diagnostics"] if d["kind"] == "cycle"]
    assert len(cycles) == 1 and set(cycles[0]["keys"]) == {"id:" + str(a["id"]),
                                                          "id:" + str(b["id"])}


def test_traversal_after_a_descendant_is_unsatisfiable():
    child = n(1, 2, text="child", flags=FOCUS)
    parent = n(1, 1, text="parent", flags=VIS, children=[child])
    parent["traversal_after"] = child["id"]
    ro = a11y.reading_order([n(1, -1, children=[parent])])
    assert any(d["kind"] == "unsatisfiable" for d in ro["diagnostics"])


def test_traversal_group_moves_as_a_unit():
    # Group G = [g1, g2]; x is ANI-first. g2 asks to be read before x: TalkBack-without-
    # groups would split the group (g2, x, g1); honouring the group reads g1, g2, x.
    x = n(1, 10, text="x", flags=FOCUS)
    g1 = n(1, 21, text="g1", flags=FOCUS)
    g2 = n(1, 22, text="g2", flags=FOCUS)
    g2["traversal_before"] = x["id"]
    group = n(1, 20, is_traversal_group=True, children=[g1, g2])
    ro = a11y.reading_order([n(1, -1, children=[x, group])])
    assert [e["speak"] for e in ro["focus_order"]] == ["g1", "g2", "x"]
    assert any(d["kind"] == "traversal_group" for d in ro["diagnostics"])


def test_windows_keep_agent_order_and_are_tagged():
    w1 = n(1, -1, children=[n(2, -1, text="main")])
    w2 = n(9, -1, bounds=(0, 0, 5, 5), children=[n(8, -1, text="dialog")])
    order = a11y.reading_order([w1, w2])["focus_order"]
    assert [(e["speak"], e["window"]) for e in order] == [("main", 0), ("dialog", 1)]


def test_duplicate_keys_are_diagnosed():
    a = n(1, 5, text="a")
    b = n(1, 5, text="b")
    ro = a11y.reading_order([n(1, -1, children=[a, b])])
    assert any(d["kind"] == "duplicate_key" for d in ro["diagnostics"])
