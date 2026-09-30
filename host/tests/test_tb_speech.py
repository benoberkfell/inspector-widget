"""What TalkBack says on focus (inspector_widget.talkback.speech), with provenance: the 16.2
source's composition (TB = talkback/src/main/java/com/google/android/accessibility/talkback/
@229212f), worded as TalkBack 17.0 speaks it by default (". " between parts; see
speech.VERSIONS), and checked against 17.0's logged utterances at the end."""

from __future__ import annotations

import gzip
import json
import os

import pytest

from inspector_widget import talkback as tb
from inspector_widget.talkback import speech as S
from inspector_widget.talkback.tree import FAKE_CD_OFFSET, FAKE_ROLE_OFFSET

from test_tb_order import DATA, OBSERVED, with_window_meta
from test_tb_rules import FOCUS, c, compose_host, n, root, speech


def say(node_dict, *others, key=None):
    """The announcement for one node (the first stop unless ``key`` names another)."""
    tree = tb.build([root(node_dict, *others)])
    nav = tb.Navigator(tree)
    target = tree.node(key or node_dict["node_key"])
    return S.announce(nav, target, transitions=False)


# -------------------------------------------------------------------------------- STATE NAME ROLE
def test_state_name_role_order_for_a_checked_checkbox():
    # TreeNodesDescription.getAppendedTreeDescription (:192): status prepended; then
    # RoleDescriptionExtractor (:92) STATE, NAME, ROLE (res/values/donottranslate.xml:1242).
    a = say(n(2, cls="android.widget.CheckBox", text="Done",
              flags=FOCUS + ("checkable", "checked")))
    assert a.text == "checked. Done. Check box"
    assert [(p["text"], p["kind"]) for p in a.parts] == [
        ("checked", "state"), ("Done", "name"), ("Check box", "role")]


def test_unchecked_checkbox_without_state_description_says_no_state():
    # nodeStatusDescription (:265): "checked"/"not checked" only when there is no
    # stateDescription AND (checked OR a selection-mode collection). An unchecked Checkbox
    # without a stateDescription (every Compose Checkbox, and a View CheckBox never toggled)
    # is announced without "not checked" in 16.2.
    a = say(n(2, cls="android.widget.CheckBox", text="Done", flags=FOCUS + ("checkable",)))
    assert a.text == "Done. Check box"
    explicit = say(n(2, cls="android.widget.CheckBox", text="Done", flags=FOCUS + ("checkable",),
                     state_description="Not checked"))
    assert explicit.text == "Not checked. Done. Check box"


def test_selection_mode_collection_announces_not_checked():
    item = n(3, cls="android.widget.CheckedTextView", text="Option", flags=FOCUS + ("checkable",),
             b=(0, 0, 1080, 100), collection_item_info={"row_index": 0, "column_index": 0})
    lst = n(2, cls="android.widget.ListView", b=(0, 0, 1080, 300), children=[item],
            collection_info={"row_count": 3, "column_count": 1, "selection_mode": 1})
    walk = tb.simulate(tb.build([root(lst)]))
    assert walk.stops[0]["speak"].startswith("not checked. Option. Radio button")


def test_switch_without_state_description():
    # SwitchDescription: the state falls back to the text, then to on/off.
    assert say(n(2, cls="android.widget.Switch", cd="Notify",
                 flags=FOCUS + ("checkable",))).text == "off. Notify. Switch"
    assert say(n(2, cls="android.widget.Switch", text="Wi-Fi",
                 flags=FOCUS + ("checkable", "checked"))).text == "checked. Wi-Fi. Switch"


def test_compose_switch_with_state_description_and_fake_role_child():
    # Compose sets a stateDescription for Switch and puts roleDescription "Switch" on the fake
    # child (id + 1e9); the icon's cd comes from a child.
    sw = c(11, 10, flags=FOCUS + ("screen_reader_focusable", "checkable", "checked"),
           state_description="On", b=(0, 0, 120, 120), children=[
               c(11, 11, cd="Notify", b=(30, 30, 60, 60)),
               c(11, 10 + FAKE_ROLE_OFFSET, role_description="Switch", b=(0, 0, 120, 120))])
    tree = tb.build([root(compose_host(11, sw))])
    a = S.announce(tb.Navigator(tree), tree.node("compose:11:10"), transitions=False)
    assert a.text == "On. Notify. Switch"
    assert [(p["from"], p["kind"]) for p in a.parts] == [
        ("compose:11:10", "state"), ("compose:11:11", "child"),
        (f"compose:11:{10 + FAKE_ROLE_OFFSET}", "role(fake)")]


# ----------------------------------------------------------------- contentDescription vs children
def test_content_description_on_a_view_container_silences_its_children():
    # treeNodesDescription (:306): children are appended only when the node has no
    # contentDescription.
    row = n(2, cls="android.widget.LinearLayout", cd="Settings row", flags=FOCUS,
            b=(0, 0, 1080, 200), children=[
                n(3, cls="android.widget.TextView", text="Bluetooth", b=(0, 0, 800, 200))])
    assert say(row).text == "Settings row"


def test_compose_fake_content_description_child_is_spoken_with_the_other_children():
    # A merging Compose node with children gets no contentDescription; a fake child (id + 2e9)
    # carries it (SemanticsNode.emitFakeNodes), so its siblings are read too: "Profile, Jane".
    card = c(7, 5, flags=FOCUS, b=(0, 0, 1080, 200), children=[
        c(7, 5 + FAKE_CD_OFFSET, cd="Profile", b=(0, 0, 1080, 200)),
        c(7, 6, cls="android.widget.TextView", text="Jane", b=(20, 20, 400, 80))])
    tree = tb.build([root(compose_host(7, card))])
    a = S.announce(tb.Navigator(tree), tree.node("compose:7:5"), transitions=False)
    assert a.text == "Profile. Jane"
    assert [p["kind"] for p in a.parts] == ["name(fake)", "child"]


def test_merged_icon_button_reads_the_icon_cd_and_the_fake_role():
    btn = c(7, 157, flags=FOCUS + ("screen_reader_focusable",), b=(0, 0, 120, 120), children=[
        c(7, 158, cd="Add to favorites", b=(30, 30, 60, 60)),
        c(7, 157 + FAKE_ROLE_OFFSET, cls="android.widget.Button", b=(0, 0, 120, 120))])
    assert speech(root(compose_host(7, btn))) == ["Add to favorites. Button"]


def test_non_focusable_children_only():
    card = n(2, flags=FOCUS, b=(0, 0, 1080, 200), children=[
        n(3, cls="android.widget.TextView", text="Title", b=(0, 0, 500, 80)),
        n(4, cls="android.widget.Button", text="Action", flags=FOCUS, b=(600, 0, 300, 80))])
    assert say(card).text == "Title"


# ------------------------------------------------------------------------------------- Unlabelled
def test_unlabelled_leaf_says_its_role_or_unlabelled():
    # getUnlabelledNodeDescription (TB/compositor/AccessibilityNodeFeedbackUtils.java:300):
    # a clickable leaf with no text/cd/state/hint/label says its role word, else "Unlabelled".
    img = say(n(2, cls="android.widget.ImageButton", flags=FOCUS))
    assert (img.text, img.unlabelled) == ("Button", True)
    blank = say(n(2, flags=FOCUS))
    assert (blank.text, blank.unlabelled) == ("Unlabelled", True)
    assert blank.parts == [{"text": "Unlabelled", "from": "view:2", "kind": "unlabelled"}]


def test_a_node_with_children_is_never_unlabelled_by_that_rule():
    # needsLabel requires childCount == 0: a clickable container reads its children instead.
    box = n(2, flags=FOCUS, b=(0, 0, 500, 200), children=[
        n(3, cls="android.widget.TextView", text="Inner", b=(0, 0, 500, 100))])
    assert say(box).text == "Inner"


def test_unchecked_unlabelled_checkbox_says_only_its_role():
    # A checkable node is exempt from "Unlabelled"; unchecked without a state it says only
    # "Check box" (a11y-core: "Unlabeled, checkbox, not checked").
    a = say(n(2, cls="android.widget.CheckBox", flags=FOCUS + ("checkable",)))
    assert (a.text, a.unlabelled) == ("Check box", True)


# ---------------------------------------------------------- edit text, disabled, selected, labels
def test_edit_text_text_hint_and_password():
    assert say(n(2, cls="android.widget.EditText", text="ada@example.com",
                 flags=FOCUS + ("editable",))).text == "ada@example.com. Edit box"
    # Empty field: the hint comes first (getHintDescription, :547).
    assert say(n(2, cls="android.widget.EditText", hint_text="Email",
                 flags=FOCUS + ("editable",))).text == "Email. Edit box"
    pw = say(n(2, cls="android.widget.EditText", text="hunter",
               flags=FOCUS + ("editable", "password"), actions=[{"id": 0x20000}]))
    assert pw.text == "password. 6 characters. Edit box"
    ro = say(n(2, cls="android.widget.EditText", text="Fixed", flags=FOCUS))
    assert ro.text == "Fixed. Edit box. read only"


def test_disabled_and_selected_and_heading():
    disabled = say(n(2, cls="android.widget.Button", text="Send",
                     flags=("visible_to_user", "clickable", "focusable")))
    assert disabled.text == "Send. Button. disabled"
    assert say(n(2, cls="android.widget.TextView", text="Tab A", flags=FOCUS + ("selected",),
                 role_description="Tab")).text == "selected. Tab A. Tab"
    heading = say(n(2, cls="android.widget.TextView", text="Account",
                    flags=("visible_to_user", "heading")))
    assert heading.text == "Account. Heading"  # a disabled heading is not "disabled"


def test_labeled_by_uses_the_for_template():
    label = n(3, cls="android.widget.TextView", text="Email", b=(0, 0, 500, 80))
    field = n(2, cls="android.widget.EditText", flags=FOCUS + ("editable",), b=(0, 100, 500, 80),
              labeled_by=label["id"])
    a = say(field, label)
    assert a.text == "Edit box for Email"
    assert a.parts[-1] == {"text": "for Email", "from": "view:2", "kind": "label"}


def test_seek_bar_value():
    a = say(n(2, cls="android.widget.SeekBar", cd="Volume", flags=FOCUS,
              range_info={"type": "PERCENT", "min": 0.0, "max": 100.0, "current": 50.0}))
    assert a.text == "50.0 percent. Volume. Slider"


def test_error_and_tooltip():
    a = say(n(2, cls="android.widget.EditText", text="x", flags=FOCUS + ("editable",
                                                                         "content_invalid"),
              error="Too short", tooltip_text="Your name"))
    assert a.text == "Error: Too short. x. Edit box. Your name"


# ------------------------------------------------------------------------------------ collections
def test_list_position_and_transitions():
    # EventTypeViewAccessibilityFocusedFeedbackRule (:181) + CollectionStateFeedbackUtils:
    # "1 of 3" and "In list, 3 items" on entering, "Out of list" on leaving.
    rows = [n(10 + i, cls="android.widget.LinearLayout", b=(0, 100 * i, 1080, 100),
              collection_item_info={"row_index": i, "column_index": 0}, children=[
                  n(20 + i, cls="android.widget.TextView", text=f"Row {i}",
                    b=(0, 100 * i, 1080, 100))]) for i in range(3)]
    lst = n(2, cls="androidx.recyclerview.widget.RecyclerView", b=(0, 0, 1080, 300),
            collection_info={"row_count": 3, "column_count": 1}, children=rows)
    after = n(3, cls="android.widget.Button", text="Done", flags=FOCUS, b=(0, 400, 500, 100))
    walk = tb.simulate(tb.build([root(lst, after)]))
    assert walk.speech()[:4] == ["Row 0. 1 of 3. In list. 3 items", "Row 1. 2 of 3",
                                 "Row 2. 3 of 3", "Done. Button. Out of list"]
    kinds = [p["kind"] for p in walk.stops[0]["parts"]]
    assert kinds == ["child", "collection", "collection"]  # the row speaks through its text child


def test_grid_position():
    cells = [n(10 + i, cls="android.widget.TextView", text=f"Cell {i}", flags=FOCUS,
               b=(300 * (i % 2), 100 * (i // 2), 300, 100),
               collection_item_info={"row_index": i // 2, "column_index": i % 2})
             for i in range(4)]
    grid = n(2, cls="android.widget.GridView", b=(0, 0, 600, 200),
             collection_info={"row_count": 2, "column_count": 2}, children=cells)
    walk = tb.simulate(tb.build([root(grid)]))
    assert walk.speech()[:3] == ["Cell 0. Row 1. Column 1. In grid. 2 rows. 2 columns",
                                 "Cell 1. Column 2", "Cell 2. Row 2. Column 1"]


def test_container_title_transition():
    pane = n(2, cls="android.widget.LinearLayout", container_title="Filters",
             b=(0, 0, 1080, 300), children=[
                 n(3, cls="android.widget.Button", text="Price", flags=FOCUS, b=(0, 0, 500, 100))])
    out = n(4, cls="android.widget.Button", text="Apply", flags=FOCUS, b=(0, 400, 500, 100))
    assert tb.simulate(tb.build([root(pane, out)])).speech()[:2] == [
        "Price. Button. In Filters", "Apply. Button. Out of Filters"]


def test_window_transition_speaks_the_title():
    w1 = root(n(2, cls="android.widget.Button", text="Open", flags=FOCUS, b=(0, 0, 500, 100)))
    w2 = root(n(11, cls="android.widget.Button", text="OK", flags=FOCUS, b=(0, 1600, 500, 100)),
              host=10, b=(0, 1500, 1080, 400))
    dump = {"windows": [{"root_view_id": 1, "root": w1},
                        {"root_view_id": 10, "root": w2, "title": "Confirm"}],
            "diagnostics": "root#1 window type=1 flags=0x0; root#10 window type=1000 flags=0x28"}
    assert tb.simulate(tb.build(dump)).speech()[:2] == [
        "Open. Button", "OK. Button. Window Confirm"]


# ------------------------------------------------------------------ calibration: TalkBack 17.0
# The utterances TalkBack 17.0.0 logged (ttsOutput) on emulator-5554, press by press, against
# the model's announcements for the same a11yprobe screens (tests/data/tb/tb17_observed.json).
def _fixture_tree(name):
    with gzip.open(os.path.join(DATA, f"a11yprobe_{name}.json.gz"), "rt") as f:
        return tb.build(with_window_meta(json.load(f), name))


@pytest.mark.parametrize("name,start", [
    ("S1", "view:10"),  # the walk started on the heading ("first"), then pressed next
    ("S4", "initial"),
    ("D2", "initial"),
])
def test_tb17_utterances_match_word_for_word(name, start):
    expected = OBSERVED["utterances"][name]
    walk = tb.simulate(_fixture_tree(name), start=start, until="steps",
                       max_steps=len(expected) + 1)
    assert walk.speech()[: len(expected)] == expected


def test_tb17_d1_utterances_apart_from_the_carried_over_list_exit():
    expected = list(OBSERVED["utterances"]["D1"])
    # The first one, "Button. Out of list": TalkBack's collection state still held the list the
    # focus was in before the dialog opened; the model starts without that history.
    assert expected[0] == "Button. Out of list"
    expected[0] = "Button"
    walk = tb.simulate(_fixture_tree("D1"), start="initial", until="steps", max_steps=6)
    assert walk.speech()[: len(expected)] == expected


def test_versions_differ_only_in_the_separator_so_far():
    node = n(2, cls="android.widget.CheckBox", text="Done", flags=FOCUS + ("checkable", "checked"))
    tree = tb.build([root(node)])
    target = tree.node("view:2")
    nav = tb.Navigator(tree)
    assert S.announce(nav, target, transitions=False, version="16.2").text == \
        "checked, Done, Check box"
    assert S.announce(nav, target, transitions=False, version="17.0").text == \
        "checked. Done. Check box"
    assert S.DEFAULT_VERSION == "17.0" and set(S.VERSIONS) == {"16.2", "17.0"}
    assert tb.simulate(tree, version="16.2").to_dict()["version"] == "16.2"
    with pytest.raises(ValueError):
        S.announce(nav, target, version="18.0")


def test_empty_description_falls_back_to_the_events_text():
    # viewAccessibilityFocusedDescription: nothing from the tree (the focusable ScrollView's only
    # speaking child is invisible, its visible children are stops of their own), so TalkBack speaks
    # the focus event's text: everything the View subtree populates, visible to the user or not.
    scroll = n(2, cls="android.widget.ScrollView", flags=FOCUS, b=(0, 0, 1080, 2000), children=[
        n(3, cls="android.widget.LinearLayout", b=(0, 0, 1080, 2000),
          important_for_accessibility="NO", children=[
              n(4, cls="android.widget.TextView", text="Hidden intro", flags=("enabled",),
                b=(0, 0, 1080, 100)),
              n(5, cls="android.widget.TextView", text="Body", b=(0, 200, 1080, 100)),
              n(6, cls="android.widget.ImageView", cd="Chart", b=(0, 400, 500, 500))])])
    a = say(scroll)
    assert a.text == "Hidden intro, Body, Chart" and a.parts[0]["kind"] == "event"
    assert not a.unlabelled
