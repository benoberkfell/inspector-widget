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


def test_linkage_in_the_wrong_key_space_is_called_out():
    # The pre-fix agent emitted getSourceNodeId-packed targets: nothing resolves.
    a = n(1, 1, text="A", flags=FOCUS, traversal_before=(7 << 32) | 99)
    b = n(1, 2, text="B", flags=FOCUS, traversal_after=(7 << 32) | 98)
    ro = a11y.reading_order([n(1, -1, children=[a, b])])
    diag = [d for d in ro["diagnostics"] if d["kind"] == "unresolved_target"][0]
    assert diag.get("suspect_key_space") is True
    # A single dangling target (the mixed fixture's holder View) is not flagged as such.
    d = a11y.a11y_to_dict(mf.a11y_response())
    assert "suspect_key_space" not in d["reading_order_diagnostics"][0]


def _chain(nodes):
    """Link nodes the way Compose does: prev.before = next and next.after = prev."""
    for prev, nxt in zip(nodes, nodes[1:]):
        prev["traversal_before"] = nxt["id"]
        nxt["traversal_after"] = prev["id"]


def test_compose_scaffold_chain_reads_top_bar_first():
    # The live IconButton screen: Compose composes the content group before the top
    # bar, so ANI order is content-first; its traversal chain puts the top bar first.
    back = n(7, 150, flags=FOCUS, children=[n(7, 151, cd="Back to scenario list")])
    label = n(7, 9, text="Icon button label", flags=FOCUS)
    top_bar = n(7, 5, is_traversal_group=True, children=[back, label])
    title = n(7, 153, text="IconButton contentDescription", flags=FOCUS + ["heading"])
    good = n(7, 155, text="GOOD", flags=FOCUS)
    fav = n(7, 157, flags=FOCUS, children=[n(7, 158, cd="Add to favorites")])
    content = n(7, 152, cls="android.widget.ScrollView", is_traversal_group=True,
                children=[title, good, fav])
    group = n(7, 2, is_traversal_group=True, children=[content, top_bar])
    _chain([group, top_bar, back, label, content, title, good, fav])
    host = n(7, -1, children=[group])
    ro = a11y.reading_order([host])
    assert [e["speak"] for e in ro["focus_order"]] == [
        "Back to scenario list", "Icon button label", "IconButton contentDescription, heading",
        "GOOD", "Add to favorites"]
    assert not [d for d in ro["diagnostics"] if d["kind"] != "traversal_group"]


def test_traversal_before_moves_the_node_not_its_target():
    # B (deep in C) asks to be read before header H: TalkBack moves B up to H; H and
    # everything between stay where they are.
    h = n(1, 10, text="H", flags=FOCUS)
    x = n(1, 11, text="X", flags=FOCUS)
    y = n(1, 21, text="Y", flags=FOCUS)
    b = n(1, 22, text="B", flags=FOCUS, traversal_before=a11y_key(1, 10))
    z = n(1, 23, text="Z", flags=FOCUS)
    c = n(1, 20, children=[y, b, z])
    assert speech([n(1, -1, children=[h, x, c])]) == ["B", "H", "X", "Y", "Z"]


def test_long_compose_chain_has_no_recursion_limit():
    items = [n(1, i, text=f"item {i}", flags=FOCUS) for i in range(1, 3001)]
    _chain(list(reversed(items)))  # ANI order is the reverse of the chain
    out = speech([n(1, -1, children=items)])
    assert out[0] == "item 3000" and out[-1] == "item 1" and len(out) == 3000


def test_unresolvable_views_get_no_key_and_no_duplicate_noise():
    # host_view_id 0 = the agent could not resolve the backing View.
    b = mf.A11yBuilder()
    root = b.node(2, -1, (0, 0, 100, 100), children=[
        b.node(0, -1, (0, 0, 50, 20), cls="android.widget.TextView", text="orphan one"),
        b.node(0, -1, (0, 30, 50, 20), cls="android.widget.TextView", text="orphan two")])
    d = a11y.a11y_to_dict(b.response([root]))
    assert [(e["key"], e["speak"]) for e in d["focus_order"]] == [
        (None, "orphan one"), (None, "orphan two")]
    assert d["summary"]["unresolved_nodes"] == 2
    assert "reading_order_diagnostics" not in d


def test_compose_order_unknown_is_reported_when_the_agent_could_not_compute_it():
    from inspector_widget import a11y as A
    from inspector_widget.proto import view_inspection_pb2 as pb
    resp = pb.DumpA11yResponse(diagnostics=(
        "roots=2; api=37; ids=host-key; a11y-services=off; root#2 compose-traversal computed=1,"
        "unavailable=2; root#2 query-from-app-process; root#9 compose-traversal unavailable=1"))
    d = A.a11y_to_dict(resp)
    kinds = {x["kind"]: x for x in d.get("reading_order_diagnostics", [])}
    assert kinds["compose_order_unknown"]["count"] == 3
    ok = A.a11y_to_dict(pb.DumpA11yResponse(diagnostics="root#2 compose-traversal computed=12"))
    assert "reading_order_diagnostics" not in ok


# --------------------------------------------------------------------------- what TalkBack sees
# TalkBack does not request not-important Views; the agent's in-process connection does.
def test_scroll_view_content_container_is_not_one_stop():
    # ScrollView > LinearLayout (AUTO, not important) > TextViews + Button: TalkBack reads
    # each TextView as its own top-level scroll item, not the whole screen as one stop.
    texts = n(3, -1, cls="android.widget.LinearLayout", children=[
        n(4, -1, cls="android.widget.TextView", text="Account", flags=VIS + ["heading"]),
        n(5, -1, cls="android.widget.TextView", text="Signed in as ada@example.com"),
        n(6, -1, cls="android.widget.Button", text="Sign out", flags=FOCUS),
        n(7, -1, cls="android.widget.TextView", text="Privacy", flags=VIS + ["heading"])])
    scroll = n(2, -1, cls="android.widget.ScrollView", flags=VIS + ["focusable", "scrollable"],
               important_for_accessibility="YES", children=[texts])
    assert speech([n(1, -1, children=[scroll])]) == [
        "Account, heading", "Signed in as ada@example.com", "Sign out, button",
        "Privacy, heading"]


def test_not_important_and_hidden_views_are_not_stops():
    root = n(1, -1, children=[
        n(2, -1, cls="android.widget.TextView", text="Visible title", important_for_accessibility="YES"),
        n(3, -1, cls="android.widget.TextView", text="Decorative duplicate",
          important_for_accessibility="NO"),
        n(4, -1, cls="android.widget.FrameLayout", important_for_accessibility="NO_HIDE_DESCENDANTS",
          children=[n(5, -1, cls="android.widget.TextView", text="Hidden subtree text"),
                    n(6, -1, cls="android.widget.Button", text="Hidden button", flags=FOCUS)])])
    assert speech([root]) == ["Visible title"]


def test_not_important_icon_adds_nothing_to_its_row():
    row = n(3, -1, cls="android.widget.LinearLayout", flags=FOCUS, children=[
        n(4, -1, cls="android.widget.ImageView"),  # decorative: AUTO, no description
        n(5, -1, cls="android.widget.TextView", text="Wi-Fi", important_for_accessibility="YES")])
    assert speech([n(1, -1, children=[row])]) == ["Wi-Fi"]
    # An icon that IS important (TalkBack sees it) still adds its role, as TalkBack does.
    row["children"][0]["important_for_accessibility"] = "YES"
    assert speech([n(1, -1, children=[row])]) == ["image, Wi-Fi"]


def test_recycler_view_items_stay_items_when_auto():
    # RecyclerView marks its item Views important once a service is on, so an AUTO row
    # is still one top-level list item (not hoisted into separate texts).
    rows = [n(10 + i, -1, cls="android.widget.LinearLayout", children=[
        n(20 + i, -1, cls="android.widget.TextView", text=f"Title {i}"),
        n(30 + i, -1, cls="android.widget.TextView", text=f"Subtitle {i}")]) for i in range(2)]
    rv = n(2, -1, cls="androidx.recyclerview.widget.RecyclerView",
           flags=VIS + ["focusable", "scrollable"], children=rows,
           collection_info={"row_count": 2, "column_count": 1})
    assert speech([n(1, -1, children=[rv])]) == ["Title 0, Subtitle 0", "Title 1, Subtitle 1"]


def test_a_view_hosted_by_a_compose_node_is_kept():
    # Compose adds an AndroidView's holder as a child of a virtual node (it forces the
    # holder important); its own not-important children are still hoisted.
    holder = n(40, -1, cls="android.widget.FrameLayout", children=[
        n(41, -1, cls="android.widget.LinearLayout", children=[
            n(42, -1, cls="android.widget.TextView", text="From a View")])])
    acv = n(30, -1, provider_class="androidx.compose.ui.platform.AndroidComposeView",
            children=[n(30, 5, children=[holder])])
    fo = a11y.reading_order([n(1, -1, children=[acv])])
    assert [e["speak"] for e in fo["focus_order"]] == ["From a View"]


def test_a11y_to_dict_marks_what_talkback_ignores():
    b = mf.A11yBuilder()
    root = b.node(2, -1, (0, 0, 1080, 2400), children=[
        b.node(3, -1, (0, 0, 1080, 2400), cls="android.widget.LinearLayout", children=[
            b.node(4, -1, (0, 100, 1080, 80), cls="android.widget.TextView", text="Title"),
            b.node(5, -1, (0, 200, 100, 100), cls="android.widget.ImageView"),
            b.node(6, -1, (0, 300, 1080, 300), cls="android.widget.FrameLayout",
                   important_for_accessibility=4, children=[
                b.node(7, -1, (0, 300, 1080, 80), cls="android.widget.TextView", text="gone")])])])
    d = a11y.a11y_to_dict(b.response([root]))
    by_key = {x["node_key"]: x for x in a11y._iter_nodes([d["windows"][0]["root"]])}
    assert by_key["view:3"]["ignored"] == "not_important"
    assert by_key["view:5"]["ignored"] == "not_important"
    assert by_key["view:6"]["ignored"] == "hidden" and by_key["view:7"]["ignored"] == "hidden"
    assert "ignored" not in by_key["view:4"] and "ignored" not in by_key["view:2"]
    assert d["summary"]["ignored_by_talkback"] == 4
    assert [e["speak"] for e in d["focus_order"]] == ["Title"]


# --------------------------------------------------------------------------- modal windows
def _two_window_response(dialog_flags: int):
    from inspector_widget.proto import view_inspection_pb2 as pb
    b = mf.A11yBuilder()
    activity = b.node(2, -1, (0, 0, 1080, 2400), children=[
        b.node(3, -1, (0, 100, 1080, 80), cls="android.widget.TextView", text="Under the dialog"),
        b.node(4, -1, (0, 300, 400, 140), cls="android.widget.Button", text="Open dialog",
               flags=mf.VIS + ("clickable", "focusable"))])
    dialog = b.node(9, -1, (100, 800, 880, 600), children=[
        b.node(10, -1, (140, 840, 800, 80), cls="android.widget.TextView", text="Dialog title",
               flags=mf.VIS + ("heading",)),
        b.node(11, -1, (140, 1200, 300, 140), cls="android.widget.Button", text="Close",
               flags=mf.VIS + ("clickable", "focusable"))])
    resp = b.response([activity, dialog])
    resp.diagnostics = (f"roots=2; api=37; root#2 window type=1 flags=0x81810100; "
                        f"root#9 window type=2 flags=0x{dialog_flags:x}")
    return resp


def test_a_modal_dialog_hides_the_activity_from_the_reading_order():
    d = a11y.a11y_to_dict(_two_window_response(0x1820002))  # Dialog: DIM_BEHIND, no NOT_TOUCH_MODAL
    assert [(e["speak"], e["window"]) for e in d["focus_order"]] == [
        ("Dialog title, heading", 1), ("Close, button", 1)]
    w0, w1 = d["windows"]
    assert w0["covered_by"] == 9 and w1["modal"] is True and "covered_by" not in w1
    diag = [x for x in d["reading_order_diagnostics"] if x["kind"] == "covered_windows"][0]
    assert diag["windows"] == [2] and diag["modal_window"] == 9


def test_a_non_modal_popup_leaves_the_activity_reachable():
    # FLAG_NOT_TOUCH_MODAL | FLAG_NOT_FOCUSABLE: a tooltip-like popup.
    d = a11y.a11y_to_dict(_two_window_response(0x28))
    assert [e["speak"] for e in d["focus_order"]] == [
        "Under the dialog", "Open dialog, button", "Dialog title, heading", "Close, button"]
    assert all("covered_by" not in w for w in d["windows"])


def test_generation_changes_only_when_compose_ids_change():
    g1 = a11y.a11y_to_dict(mf.a11y_response())["generation"]
    assert g1 == a11y.a11y_to_dict(mf.a11y_response())["generation"]
    resp = mf.a11y_response()
    resp.windows[0].root.children[1].children[0].children[0].children[0].virtual_id = 902
    assert a11y.a11y_to_dict(resp)["generation"] != g1
