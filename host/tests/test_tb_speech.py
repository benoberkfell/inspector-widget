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


def test_a_lone_symbol_is_spoken_by_its_name():
    # G26: SpeechCleanupUtils.cleanUp names a text that is a single symbol. TalkBack 17.0 on
    # AntennaPod's show notes: "• " -> "Bullet. 1 of 19. In list. 19 items"; a "." -> "Period".
    assert say(n(2, cls="android.widget.TextView", text="• ", flags=FOCUS)).text == "Bullet"
    assert say(n(2, cls="android.widget.TextView", text=".", flags=FOCUS)).text == "Period"
    row = n(2, flags=FOCUS, b=(0, 0, 1080, 200), children=[
        n(3, cls="android.widget.TextView", text="•", b=(0, 0, 40, 80)),
        n(4, cls="android.widget.TextView", text="Item", b=(60, 0, 500, 80))])
    assert say(row).text == "Bullet. Item"  # a child's lone symbol too
    # more than one character is read as it is
    assert say(n(2, cls="android.widget.TextView", text="••", flags=FOCUS)).text == "••"


def test_symbol_names_are_talkbacks_own():
    # TALKBACK_PUNCTUATION_AND_SYMBOL (SpeechCleanupUtils) with strings_symbols.xml's English
    # names; the table had made-up names for these, and a "+" TalkBack does not name
    from inspector_widget.talkback.speech import SYMBOL_NAMES, spoken_text

    assert {c: spoken_text(c) for c in "$=|£°§¶×÷"} == {
        "$": "Dollar sign", "=": "Equal sign", "|": "Vertical line",
        "£": "Pound currency sign", "°": "Degree sign", "§": "Section sign",
        "¶": "Paragraph mark", "×": "Multiplication sign", "÷": "Division sign"}
    assert spoken_text("+") == "+" and "+" not in SYMBOL_NAMES
    # keys it has that the table lacked
    assert [spoken_text(c) for c in "\"()[]{}<>¢`✓"] == [
        "Quote", "Left paren", "Right paren", "Left square bracket", "Right square bracket",
        "Left curly bracket", "Right curly bracket", "Less than sign", "Greater than sign",
        "Cent sign", "Grave accent", "Check mark"]
    assert len(SYMBOL_NAMES) == 150
    # cleanUp: trimmed of Java whitespace; whitespace only is the name of its first
    # character; a no-break space is no Java whitespace, so alone it is a symbol
    assert spoken_text(" ") == "Space" and spoken_text("\n") == "New line"
    assert spoken_text("\u00a0") == "Space" and spoken_text(" . ") == "Period"
    assert spoken_text("\u00a0.") == "\u00a0." and spoken_text("Go") == "Go"


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


def test_versions_differ_in_the_separator_and_the_pager_words():
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


@pytest.mark.parametrize("name", ["traversal", "S1", "S4", "D1", "D2", "launcher"])
def test_tb17_p6_utterances_are_exact(name):
    # Every press whose node is in the dump: the model's 17.0 announcement at the node TalkBack
    # actually focused equals TalkBack's ttsOutput, character for character.
    from test_tb_order import TB17_WALKS, tb17_tree, walk_node

    walk = TB17_WALKS[name]
    tree = tb17_tree(name)
    nav = tb.Navigator(tree)
    state = S.SpeechState()
    start, _ = walk_node(tree, walk[0])
    S.announce(nav, start, state)  # focus was there before the first press
    exact = compared = 0
    pre_scroll = []
    for entry in walk[1:]:
        if entry.get("edge"):
            continue
        node, moved = walk_node(tree, entry)
        if node is None:
            continue  # scrolled in (launcher): not in the dump
        said = S.announce(nav, node, state).text
        if moved:
            pre_scroll.append((said, entry["said"]))
            continue
        compared += 1
        exact += said == entry["said"]
        assert said == entry["said"]
    assert exact == compared > 0
    if name == "launcher":
        # "Section heading" was a clipped sliver when the walk began; TalkBack scrolled it into
        # view (SHOW_ON_SCREEN) and Compose composed its text before TalkBack spoke it.
        assert pre_scroll == [("Unlabelled", "Section heading. MissingHeading")]
        walk_model = tb.simulate(tree, start=start, until="steps", max_steps=12)
        assert walk_model.stops[11].get("speak_conf") == "pre_scroll"
    else:
        assert pre_scroll == []


# ------------------------------------------------------------- TalkBack 17.0, real-app walks (B10)
def _rows(host0, n_rows, text, y0=0, **kw):
    return [n(host0 + i, cls="android.widget.TextView", text=f"{text} {i}", flags=FOCUS,
              b=(0, y0 + 100 * i, 1080, 100), **kw) for i in range(n_rows)]


def test_a_flat_list_holding_another_flat_list_is_not_announced():
    # CollectionState.shouldEnter (UT/monitor/CollectionState.java:966): "the innermost
    # collection" only. Now in Android's For-you feed (a LazyColumn) holds the topic grid:
    # TalkBack 17 said "What are you interested in?" with no "In list", then "Not selected.
    # Headlines. In list" in the grid.
    chips = n(30, cls="android.view.View", b=(0, 200, 1080, 300),
              collection_info={"row_count": -1, "column_count": -1},
              children=_rows(40, 2, "Topic", 200))
    header = n(20, cls="android.widget.TextView", text="What are you interested in?",
               flags=FOCUS, b=(0, 0, 1080, 100))
    feed = n(10, cls="android.view.View", b=(0, 0, 1080, 1000),
             collection_info={"row_count": -1, "column_count": -1}, children=[header, chips])
    assert tb.simulate(tb.build([root(feed)])).speech()[:3] == [
        "What are you interested in?", "Topic 0. In list", "Topic 1"]
    # A hierarchical outer collection is still announced.
    feed["collection_info"]["hierarchical"] = True
    assert tb.simulate(tb.build([root(feed)])).speech()[0] == \
        "What are you interested in?. In list"


def _pager(rows, cols):
    page = n(11, cls="android.widget.FrameLayout", b=(0, 100, 1080, 800),
             collection_item_info={"row_index": 0, "column_index": 0}, children=[
                 n(12, cls="android.widget.TextView", text="Subject", flags=FOCUS,
                   b=(0, 100, 1080, 100))])
    pager = n(10, cls="androidx.viewpager.widget.ViewPager", b=(0, 100, 1080, 800),
              collection_info={"row_count": rows, "column_count": cols},
              actions=({"id": 0x01020047},), children=[page])
    up = n(9, cls="android.widget.ImageButton", cd="Navigate up", flags=FOCUS, b=(0, 0, 120, 100))
    return [root(up, pager)]


def test_talkback_17_words_a_pager_by_its_orientation():
    # Measured: "Inline image attachment. In horizontal pager" (Thunderbird's message pager,
    # rows 1 x 6), "In vertical pager" (AntennaPod's player), and "Navigate up. Button. Out of
    # grid pager" on the way out (Thunderbird, A11yProbe V13 and C16). 16.2 says "In pager".
    h = tb.simulate(tb.build(_pager(1, 6)), start="view:9")
    assert h.speech()[:2] == ["Subject. In horizontal pager", "Navigate up. Button. Out of grid pager"]
    v = tb.simulate(tb.build(_pager(2, 1)), start="view:9")
    assert v.speech()[0] == "Subject. In vertical pager"
    old = tb.simulate(tb.build(_pager(1, 6)), start="view:9", version="16.2")
    assert old.speech()[:2] == ["Subject, In pager", "Navigate up, Button, Out of pager"]


def test_an_edit_box_reached_by_keyboard_says_editing():
    # EditTextDescription.stateDescription (TB/compositor/roledescription/EditTextDescription
    # .java:126): "Editing" when the box has input focus and a keyboard is up. TalkBack 17 on
    # Thunderbird's onboarding, walked with a hardware keyboard: "Editing. Every 15 minutes.
    # Edit box. Check frequency. Drop down list. read only".
    box = n(2, cls="android.widget.EditText", text="Every 15 minutes",
            flags=FOCUS, b=(0, 0, 1080, 150))
    tree = tb.build([root(box)])
    nav, node = tb.Navigator(tree), tree.node("view:2")
    assert S.announce(nav, node, transitions=False).text == "Every 15 minutes. Edit box. read only"
    assert S.announce(nav, node, transitions=False, keyboard=True).text == \
        "Editing. Every 15 minutes. Edit box. read only"
    # Input focus alone is not enough: TalkBack needs a keyboard up too, which a dump cannot
    # show (EditTextDescription: isFocused() && isKeyBoardActive()).
    box["flags"] = list(box["flags"]) + ["focused", "editable"]
    tree = tb.build([root(box)])
    nav, node = tb.Navigator(tree), tree.node("view:2")
    assert S.announce(nav, node, transitions=False).text == "Every 15 minutes. Edit box"
    assert S.announce(nav, node, transitions=False, keyboard=True).text == \
        "Editing. Every 15 minutes. Edit box"


def test_a_walk_from_a_focused_node_starts_inside_its_collection():
    # The focus was already on a list row when the walk started: TalkBack had said "In list"
    # there, so the next row says only its position (Thunderbird's onboarding walk:
    # "Thunderbird. In list. 6 items" on the wrap, then "Sync options").
    rows = [n(10 + i, cls="android.widget.TextView", text=f"Row {i}", flags=FOCUS,
              b=(0, 100 * i, 1080, 100), collection_item_info={"row_index": i, "column_index": 0})
            for i in range(3)]
    lst = n(2, cls="android.view.View", b=(0, 0, 1080, 300),
            collection_info={"row_count": 3, "column_count": 1}, children=rows)
    walk = tb.simulate(tb.build([root(lst)]), start="view:10", until="steps", max_steps=1)
    assert walk.speech() == ["Row 1. 2 of 3"]


def test_clickable_image_unlabelled_control_and_single_choice_radio():
    # B10 on TalkBack 17: a clickable ImageView is "Search. Button" (Role IMAGE_BUTTON), an
    # unlabelled control says only its role ("Button"), a single-choice CheckedTextView says
    # "not checked. Light. Radio button. 1 of 3. In list. 3 items".
    search = n(2, cls="android.widget.ImageView", cd="Search", flags=FOCUS, b=(0, 0, 120, 120))
    blank = n(3, cls="android.widget.ImageView", flags=FOCUS, b=(200, 0, 120, 120))
    assert say(search).text == "Search. Button"
    assert say(blank).text == "Button" and say(blank).unlabelled
    opts = [n(10 + i, cls="android.widget.CheckedTextView", text=t,
              flags=FOCUS + ("checkable",) + (("checked",) if t == "System" else ()),
              b=(0, 200 + 100 * i, 1080, 100),
              collection_item_info={"row_index": i, "column_index": 0})
            for i, t in enumerate(("Light", "Dark", "System"))]
    lst = n(4, cls="android.widget.ListView", b=(0, 200, 1080, 300),
            collection_info={"row_count": 3, "column_count": 1, "selection_mode": 1},
            children=opts)
    assert tb.simulate(tb.build([root(lst)])).speech()[:3] == [
        "not checked. Light. Radio button. 1 of 3. In list. 3 items",
        "not checked. Dark. Radio button. 2 of 3", "checked. System. Radio button. 3 of 3"]
