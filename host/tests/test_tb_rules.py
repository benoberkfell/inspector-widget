"""TalkBack 16.2's focus rules (inspector_widget.talkback.rules / .tree), one hand-built tree per
rule. Each test names the TalkBack source it pins (google/talkback @229212f; UT =
utils/src/main/java/com/google/android/accessibility/utils/)."""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from inspector_widget import talkback as tb
from inspector_widget.a11y import a11y_key
from inspector_widget.talkback import rules as R
from inspector_widget.talkback.tree import FAKE_CD_OFFSET, FAKE_ROLE_OFFSET

VIS = ("visible_to_user", "enabled")
FOCUS = VIS + ("clickable", "focusable")
SRF = VIS + ("screen_reader_focusable",)
CLICK = {"id": R.ACTION_CLICK}
SCROLL_FWD = {"id": R.ACTION_SCROLL_FORWARD}
WINDOW = (0, 0, 1080, 2400)


def n(host: int, virtual: int = -1, *, cls: str = "android.view.View", text: Optional[str] = None,
      cd: Optional[str] = None, flags=VIS, b=(0, 0, 10, 10), children=None, actions=(),
      **kw: Any) -> Dict[str, Any]:
    """A dump node dict (the a11y_to_dict shape). ``b`` = (x, y, w, h)."""
    x, y, w, h = b
    d: Dict[str, Any] = {
        "host_view_id": host, "virtual_id": virtual, "id": a11y_key(host, virtual),
        "class_name": cls, "flags": list(flags), "important_for_accessibility": "YES",
        "bounds": {"layout": {"x": x, "y": y, "w": w, "h": h}}}
    if virtual == -1:
        d["node_key"] = f"view:{host}"
    if text:
        d["text"] = text
    if cd:
        d["content_description"] = cd
    if actions:
        d["actions"] = [dict(a) for a in actions]
    d.update(kw)
    if children:
        d["children"] = list(children)
    return d


def root(*children: Dict[str, Any], host: int = 1, b=WINDOW, **kw: Any) -> Dict[str, Any]:
    """A window root View (android:id/content-like FrameLayout) the size of the window."""
    return n(host, cls="android.widget.FrameLayout", b=b, children=children, **kw)


def compose_host(host: int, *children: Dict[str, Any], b=(0, 0, 1080, 2000)) -> Dict[str, Any]:
    """An AndroidComposeView host node with its virtual (semantics) children."""
    return n(host, cls="android.view.ViewGroup", b=b, provider_class="AndroidComposeView",
             children=children)


def c(host: int, sid: int, **kw: Any) -> Dict[str, Any]:
    """A Compose virtual node (semantics id ``sid`` under the AndroidComposeView ``host``)."""
    kw.setdefault("node_key", f"compose:{host}:{sid}")
    return n(host, sid, **kw)


def model(*roots: Dict[str, Any], **kw: Any):
    tree = tb.build(list(roots), **kw)
    return tree, R.Rules(tree)


def stops(*roots: Dict[str, Any], **kw: Any):
    return tb.simulate(tb.build(list(roots), **kw)).keys()[:-1]  # drop the wrap repeat


def speech(*roots: Dict[str, Any], **kw: Any):
    return [e["speak"] for e in tb.reading_order(tb.build(list(roots), **kw))["focus_order"]]


# -------------------------------------------------------------------------------------- isVisible
def test_invisible_nodes_are_not_stops():
    # shouldFocusNode (UT/AccessibilityNodeInfoUtils.java:857): !isVisible -> skip.
    hidden = n(2, cls="android.widget.Button", text="Hidden", flags=("enabled", "clickable"))
    shown = n(3, cls="android.widget.Button", text="Shown", flags=FOCUS)
    tree, rules = model(root(hidden, shown))
    assert rules.focus_decision(tree.node("view:2")) == (False, "not_visible")
    assert stops(root(hidden, shown)) == ["view:3"]


# --------------------------------------------------------------------------------- window wrapper
def test_window_sized_container_with_children_is_skipped_even_with_text():
    # areBoundsIdenticalToWindow (:2428) + :870. a11y-core's reading_order makes this node a
    # stop (text, no focusable ancestor); TalkBack skips it: same bounds as the window, has
    # children, neither focusable nor clickable.
    wrapper = n(2, cls="android.widget.FrameLayout", cd="Main content", b=WINDOW,
                children=[n(3, cls="android.widget.TextView", text="Hello", b=(0, 100, 500, 60))])
    tree, rules = model(root(wrapper))
    assert rules.focus_decision(tree.node("view:2")) == (False, "window_wrapper")
    assert stops(root(wrapper)) == ["view:3"]
    from inspector_widget import a11y
    core = [e["key"] for e in a11y.reading_order([root(wrapper)])["focus_order"]]
    assert core == ["view:2", "view:3"]  # a11y-core reads the wrapper too


def test_window_sized_focusable_node_is_not_a_wrapper():
    btn = n(2, cls="android.widget.Button", text="Full screen", flags=FOCUS, b=WINDOW,
            children=[n(3, text="x", b=(0, 0, 5, 5))])
    tree, rules = model(root(btn))
    assert rules.focus_decision(tree.node("view:2")) == (True, "speaking")


# ------------------------------------------------------------------------ leaf / silent container
def test_focusable_leaf_is_always_a_stop():
    # :909: an accessibility-focusable node with no children is focused even with nothing to
    # say (the unlabeled-button path).
    blank = n(2, flags=FOCUS, b=(0, 0, 100, 100))
    tree, rules = model(root(blank))
    assert rules.focus_decision(tree.node("view:2")) == (True, "leaf")
    assert tb.why_stop(rules, tree.node("view:2")) == "leaf"


def test_silent_container_hands_focus_to_its_children():
    # :913-924: focusable with children but nothing to speak -> skipped; its focusable
    # children are the stops.
    row = n(2, flags=FOCUS, b=(0, 0, 1080, 200), children=[
        n(3, cls="android.widget.Button", text="Edit", flags=FOCUS, b=(0, 0, 500, 200)),
        n(4, cls="android.widget.Button", text="Delete", flags=FOCUS, b=(500, 0, 500, 200))])
    tree, rules = model(root(row))
    assert rules.focus_decision(tree.node("view:2")) == (False, "silent_container")
    assert stops(root(row)) == ["view:3", "view:4"]
    ex = tb.explain(tree, "view:2")
    assert ex["why"] == "silent_container" and "view:3, view:4" in ex["detail"]


def test_double_stop_container_speaking_through_a_text_child_plus_a_focusable_child():
    # A focusable container is a stop exactly when it speaks via NON-focusable descendants;
    # its focusable children are separate stops (the double-stop mechanism).
    card = n(2, flags=FOCUS, b=(0, 0, 1080, 200), children=[
        n(3, cls="android.widget.TextView", text="Wi-Fi", b=(0, 0, 800, 200)),
        n(4, cls="android.widget.Switch", cd="Wi-Fi", flags=FOCUS + ("checkable",),
          b=(900, 50, 150, 100))])
    assert stops(root(card)) == ["view:2", "view:4"]
    assert speech(root(card)) == ["Wi-Fi", "off, Wi-Fi, Switch"]


# ----------------------------------------------------------------------------------- text orphans
def test_text_without_focusable_ancestor_is_a_stop_and_merges_otherwise():
    # :938: not focusable, no focusable ancestor, has text (or a state) -> focus.
    label = n(2, cls="android.widget.TextView", text="Title", b=(0, 0, 500, 60))
    inner = n(4, cls="android.widget.TextView", text="Inner", b=(0, 100, 500, 60))
    btn = n(3, flags=FOCUS, b=(0, 100, 500, 60), children=[inner])
    tree, rules = model(root(label, btn))
    assert rules.focus_decision(tree.node("view:2")) == (True, "text_orphan")
    assert rules.focus_decision(tree.node("view:4")) == (False, "focusable_ancestor")
    assert tb.explain(tree, "view:4")["why"] == "merged_into:view:3"


def test_state_description_alone_makes_an_orphan_a_stop():
    status = n(2, state_description="Connected", b=(0, 0, 300, 60))
    assert stops(root(status)) == ["view:2"]


# ----------------------------------------------------------------------- hasText / CollectionInfo
def test_collection_info_nodes_have_no_text():
    # hasText (:1879): a node with CollectionInfo never "has text" (its text is for collection
    # transitions). a11y-core counts the contentDescription and makes the list a stop.
    items = [n(10 + i, cls="android.widget.TextView", text=f"Item {i}",
               b=(0, 200 + 100 * i, 1080, 100)) for i in range(3)]
    lst = n(2, cls="androidx.recyclerview.widget.RecyclerView", cd="Inbox",
            b=(0, 200, 1080, 1000), collection_info={"row_count": 3, "column_count": 1},
            children=items)
    tree, rules = model(root(lst))
    assert not rules.has_text(tree.node("view:2"))
    assert rules.focus_decision(tree.node("view:2")) == (False, "no_speech")
    assert stops(root(lst)) == ["view:10", "view:11", "view:12"]
    from inspector_widget import a11y
    core = [e["key"] for e in a11y.reading_order([root(lst)])["focus_order"]]
    assert core[0] == "view:2"  # a11y-core stops on the list itself


# --------------------------------------------------------------- hasNonActionableSpeakingChildren
def test_invisible_text_children_make_a_focusable_container_speak():
    # hasInvisibleNonActionableSpeakingChildren (:1097-1127): a focusable container whose text
    # children are all INVISIBLE still counts as speaking -> a ghost stop. a11y-core ignores
    # invisible children and skips it.
    card = n(2, flags=FOCUS, b=(0, 0, 1080, 200), children=[
        n(3, cls="android.widget.TextView", text="Scrolled away", flags=("enabled",),
          b=(0, -300, 1080, 100)),
        n(4, cls="android.widget.ImageButton", cd="More", flags=FOCUS, b=(900, 50, 100, 100))])
    tree, rules = model(root(card))
    assert rules.focus_decision(tree.node("view:2")) == (True, "speaking")
    assert stops(root(card)) == ["view:2", "view:4"]
    assert "invisible children view:3" in tb.explain(tree, "view:2")["detail"]
    from inspector_widget import a11y
    assert [e["key"] for e in a11y.reading_order([root(card)])["focus_order"]] == ["view:4"]


def test_invisible_text_children_do_not_count_for_an_auto_scroll_container():
    # ... unless the node itself passes FILTER_AUTO_SCROLL (:1101).
    lst = n(2, cls="android.widget.ScrollView", flags=FOCUS, actions=[SCROLL_FWD],
            b=(0, 0, 1080, 500), children=[
                n(3, cls="android.widget.TextView", text="Below the fold", flags=("enabled",),
                  b=(0, 900, 1080, 100)),
                n(4, cls="android.widget.Button", text="Visible", flags=FOCUS,
                  b=(0, 0, 500, 100))])
    tree, rules = model(root(lst))
    assert rules.filter_auto_scroll(tree.node("view:2"))
    assert rules.focus_decision(tree.node("view:2")) == (False, "silent_container")


def test_speaking_top_level_scroll_item_is_ignored_under_a_non_clickable_parent():
    # :1077-1084: a speaking child of a scrolling parent is its own stop, so it does not make
    # the (non-clickable) parent speak.
    scroll = n(2, cls="android.widget.ScrollView", flags=VIS + ("focusable",),
               actions=[SCROLL_FWD], b=(0, 0, 1080, 1000), children=[
                   n(3, cls="android.widget.TextView", text="Paragraph", b=(0, 0, 1080, 200))])
    tree, rules = model(root(scroll))
    assert rules.is_top_level_scroll_item(tree.node("view:3"))
    assert rules.focus_decision(tree.node("view:2")) == (False, "silent_container")
    assert stops(root(scroll)) == ["view:3"]


# --------------------------------------------------------------------------- isTopLevelScrollItem
def test_top_level_scroll_item_rules():
    # isTopLevelScrollItem (:1920) / isScrollItem (:1933): a visible child of a parent that
    # scrolls or is a List/Grid/ScrollView/HorizontalScrollView; never of a Spinner.
    row = n(3, cls="android.widget.LinearLayout", b=(0, 0, 1080, 100), children=[
        n(4, cls="android.widget.TextView", text="Row text", b=(0, 0, 1080, 100))])
    lst = n(2, cls="android.widget.ListView", b=(0, 0, 1080, 800), children=[row])
    tree, rules = model(root(lst))
    assert rules.role(tree.node("view:2")) == R.ROLE_LIST
    assert tb.why_stop(rules, tree.node("view:3")) == "scroll_item"
    spinner_item = n(6, cls="android.widget.TextView", text="Choice", b=(0, 0, 300, 100))
    spinner = n(5, cls="android.widget.Spinner", flags=FOCUS, actions=[SCROLL_FWD],
                b=(0, 0, 300, 100), children=[spinner_item])
    tree, rules = model(root(spinner))
    assert not rules.is_top_level_scroll_item(tree.node("view:6"))


# ------------------------------------------------------------------- isActionableForAccessibility
def test_action_focus_counts_but_accessibility_focus_does_not():
    # isActionableForAccessibility (:1159): ACTION_FOCUS counts (input focus);
    # ACTION_ACCESSIBILITY_FOCUS (on every Compose node) does not.
    via_focus = n(2, actions=[{"id": R.ACTION_FOCUS}], b=(0, 0, 500, 100), children=[
        n(3, cls="android.widget.TextView", text="Focusable by action", b=(0, 0, 500, 100))])
    a11y_only = n(4, actions=[{"id": R.ACTION_ACCESSIBILITY_FOCUS}], b=(0, 200, 500, 100),
                  children=[n(5, cls="android.widget.TextView", text="Plain",
                              b=(0, 200, 500, 100))])
    tree, rules = model(root(via_focus, a11y_only))
    assert rules.is_actionable_for_accessibility(tree.node("view:2"))
    assert not rules.is_actionable_for_accessibility(tree.node("view:4"))
    assert stops(root(via_focus, a11y_only)) == ["view:2", "view:5"]


# ----------------------------------------------------------------------------------- Role.getRole
@pytest.mark.parametrize("cls,extra,role", [
    ("android.widget.Button", {}, R.ROLE_BUTTON),
    ("android.widget.CheckBox", {}, R.ROLE_CHECK_BOX),  # CompoundButton subclass
    ("android.widget.Switch", {}, R.ROLE_SWITCH),
    ("android.widget.AutoCompleteTextView", {}, R.ROLE_EDIT_TEXT),  # EditText subclass
    ("android.widget.ImageView", {}, R.ROLE_IMAGE),
    ("android.widget.ImageView", {"flags": FOCUS}, R.ROLE_IMAGE_BUTTON),  # clickable image
    ("android.widget.ImageButton", {"flags": VIS}, R.ROLE_IMAGE),  # ImageView subclass, flag
    ("android.widget.SeekBar", {}, R.ROLE_SEEK_CONTROL),
    ("android.view.View", {"range_info": {"min": 0, "max": 10, "current": 3},
                           "actions": [{"id": R.ACTION_SET_PROGRESS}]}, R.ROLE_SEEK_CONTROL),
    ("android.view.View", {"range_info": {"min": 0, "max": 10, "current": 3}},
     R.ROLE_PROGRESS_BAR),
    ("android.widget.HorizontalScrollView", {}, R.ROLE_HORIZONTAL_SCROLL_VIEW),
    ("android.widget.HorizontalScrollView", {"collection_info": {"row_count": 1,
                                                                 "column_count": 5}},
     R.ROLE_LIST),  # with CollectionInfo it is a list
    ("android.widget.ScrollView", {}, R.ROLE_SCROLL_VIEW),
    ("androidx.viewpager.widget.ViewPager", {}, R.ROLE_PAGER),
    ("androidx.viewpager2.widget.ViewPager2", {"actions": [{"id": R.ACTION_PAGE_RIGHT}]},
     R.ROLE_PAGER),
    ("androidx.recyclerview.widget.RecyclerView",
     {"collection_info": {"row_count": 4, "column_count": 3}}, R.ROLE_GRID),
    ("androidx.recyclerview.widget.RecyclerView",
     {"collection_info": {"row_count": 4, "column_count": 1}}, R.ROLE_LIST),
    ("androidx.recyclerview.widget.RecyclerView", {}, R.ROLE_VIEW_GROUP),
    ("android.widget.LinearLayout", {}, R.ROLE_VIEW_GROUP),
    ("android.widget.TextView", {}, R.ROLE_NONE),
    ("com.example.FancyButton", {}, R.ROLE_NONE),  # TalkBack cannot load app classes
    ("android.widget.Spinner", {}, R.ROLE_DROP_DOWN_LIST),
    ("android.widget.GridView", {}, R.ROLE_GRID),
])
def test_role_is_class_based(cls, extra, role):
    # Role.getRole (UT/Role.java:772).
    tree, rules = model(root(n(2, cls=cls, **extra)))
    assert rules.role(tree.node("view:2")) == role


def test_compose_role_lives_on_the_fake_child():
    # Compose puts a merging Button's className on a fake child (id + 1e9; D:631), so the
    # clickable parent has no role and the fake child has ROLE_BUTTON.
    host = compose_host(7, c(7, 5, flags=FOCUS + ("screen_reader_focusable",),
                             b=(0, 0, 200, 100), children=[
                                 c(7, 6, cls="android.widget.TextView", text="OK",
                                   b=(10, 10, 100, 50)),
                                 c(7, 5 + FAKE_ROLE_OFFSET, cls="android.widget.Button",
                                   b=(0, 0, 200, 100))]))
    tree, rules = model(root(host))
    parent, fake = tree.node("compose:7:5"), tree.node(f"compose:7:{5 + FAKE_ROLE_OFFSET}")
    assert rules.role(parent) == R.ROLE_NONE and rules.role(fake) == R.ROLE_BUTTON
    assert fake.facet == "fake_role" and fake.semantics_id == 5 and parent.semantics_id == 5
    cd = c(7, 5 + FAKE_CD_OFFSET, cd="Profile")
    tree, _ = model(root(compose_host(7, c(7, 5, flags=FOCUS, children=[cd]))))
    assert tree.node(f"compose:7:{5 + FAKE_CD_OFFSET}").facet == "fake_cd"
    assert tree.node(f"compose:7:{5 + FAKE_CD_OFFSET}").semantics_id == 5


# ----------------------------------------------------------------------------- FILTER_AUTO_SCROLL
def test_filter_auto_scroll_excludes_pagers():
    # FILTER_AUTO_SCROLL (:458): scrollable + visible + List/Grid/ScrollView/HSV/Spinner.
    # A pager is NOT auto-scrolled (the next page is unreachable by swiping).
    sv = n(2, cls="android.widget.ScrollView", actions=[SCROLL_FWD])
    pager = n(3, cls="androidx.viewpager.widget.ViewPager", actions=[SCROLL_FWD])
    still = n(4, cls="android.widget.ScrollView")
    flag_only = n(5, cls="android.widget.ScrollView", flags=VIS + ("scrollable",))
    tree, rules = model(root(sv, pager, still, flag_only))
    assert rules.filter_auto_scroll(tree.node("view:2"))
    assert not rules.filter_auto_scroll(tree.node("view:3"))
    assert not rules.filter_auto_scroll(tree.node("view:4"))
    assert not rules.filter_auto_scroll(tree.node("view:5"))  # the flag alone is not a scroll


# ---------------------------------------------------------------------------- granularity filters
def test_heading_control_and_container_filters():
    heading = n(2, cls="android.widget.TextView", text="Section", flags=VIS + ("heading",),
                b=(0, 0, 500, 60))
    card = n(3, flags=FOCUS, b=(0, 100, 1080, 200), children=[
        n(4, cls="android.widget.TextView", text="Card title", flags=VIS + ("heading",),
          b=(0, 100, 500, 60))])
    sw = n(5, cls="android.widget.Switch", text="Airplane mode", flags=FOCUS + ("checkable",),
           b=(0, 400, 1080, 100))
    row = n(7, cls="android.widget.LinearLayout", flags=FOCUS, b=(0, 600, 1080, 100),
            children=[n(8, cls="android.widget.TextView", text="Clickable row",
                        b=(0, 600, 1080, 100))])
    lst = n(6, cls="android.widget.ListView", b=(0, 600, 1080, 300), children=[row])
    tree, rules = model(root(heading, card, sw, lst))
    headings = rules.node_filter("heading")
    # FILTER_HEADING, or a focusable node with a non-focusable heading leaf (:273).
    assert headings(tree.node("view:2")) and headings(tree.node("view:3"))
    assert not headings(tree.node("view:5"))
    controls = rules.node_filter("control")
    # FILTER_CONTROL (:374): the Switch is a control; a clickable ListView row is not.
    assert controls(tree.node("view:5")) and not controls(tree.node("view:7"))
    assert rules.filter_container(tree.node("view:6"))
    order = tb.simulate(tree, granularity="heading")
    assert order.keys()[:2] == ["view:2", "view:3"]


# ------------------------------------------------------------------------------ the TalkBack view
def test_not_important_views_are_hoisted_and_hidden_subtrees_dropped():
    # TalkBack does not ask for not-important Views: their children take their place, in
    # order; noHideDescendants removes the whole subtree.
    wrapper = n(3, cls="android.widget.LinearLayout", b=(0, 0, 1080, 300),
                important_for_accessibility="NO", children=[
                    n(4, cls="android.widget.TextView", text="A", b=(0, 0, 500, 100)),
                    n(5, cls="android.widget.TextView", text="B", b=(0, 100, 500, 100))])
    hidden = n(6, cls="android.widget.LinearLayout", b=(0, 400, 1080, 300),
               important_for_accessibility="NO_HIDE_DESCENDANTS", children=[
                   n(7, cls="android.widget.Button", text="Gone", flags=FOCUS,
                     b=(0, 400, 500, 100))])
    tail = n(8, cls="android.widget.TextView", text="C", b=(0, 800, 500, 100))
    tree, rules = model(root(wrapper, hidden, tail))
    assert [ch.key for ch in tree.node("view:1").children] == ["view:4", "view:5", "view:8"]
    assert tb.explain(tree, "view:3")["why"] == "not_important"
    assert tb.explain(tree, "view:7")["why"] == "hidden_by:view:6"
    assert stops(root(wrapper, hidden, tail)) == ["view:4", "view:5", "view:8"]


def test_link_to_a_not_important_view_is_dropped_and_after_applies():
    # The platform drops traversalBefore to a View that is not important (View.java:11440);
    # getTraversalBefore() is then null and TalkBack falls back to traversalAfter.
    spacer = n(5, cls="android.view.View", b=(0, 900, 10, 10), important_for_accessibility="NO")
    a = n(2, cls="android.widget.Button", text="A", flags=FOCUS, b=(0, 0, 500, 100),
          traversal_before=a11y_key(5, -1), traversal_after=a11y_key(4, -1))
    b_ = n(3, cls="android.widget.Button", text="B", flags=FOCUS, b=(0, 200, 500, 100))
    c_ = n(4, cls="android.widget.Button", text="C", flags=FOCUS, b=(0, 400, 500, 100))
    assert stops(root(a, b_, c_, spacer)) == ["view:3", "view:4", "view:2"]


def test_service_off_dump_hides_the_androidview_holder():
    # AndroidComposeView.addAndroidView: the holder is isVisibleToUser=false while a service
    # runs. A dump taken with services off shows it visible; the model corrects it.
    holder = n(20, cls="androidx.compose.ui.viewinterop.ViewFactoryHolder", b=(0, 500, 1080, 200),
               children=[n(21, cls="android.widget.TextView", text="Inside the AndroidView",
                           b=(0, 500, 1080, 200))])
    host = compose_host(7, c(7, 2, cls="android.widget.TextView", text="Compose text",
                             flags=SRF, b=(0, 0, 1080, 100)),
                        c(7, 3, b=(0, 500, 1080, 200), children=[holder]))
    off = tb.build([root(host)], services="off")
    assert off.node("view:20").facet == "interop"
    assert not off.node("view:20").visible
    assert off.node("view:20").corrections == ["holder_invisible_with_service"]
    assert tb.explain(off, "view:20")["why"] == "hidden(holder)"
    on = tb.build([root(host)], services="on")
    assert on.node("view:20").visible  # a dump with TalkBack on already reports it
    assert tb.simulate(off).keys()[:2] == ["compose:7:2", "view:21"]


def test_services_state_is_read_from_the_dump_diagnostics():
    dump = {"windows": [{"root_view_id": 1, "root": root()}],
            "diagnostics": "roots=1; api=37; a11y-services=off; root#1 window type=1 flags=0x0"}
    tree = tb.build(dump)
    assert tree.services == "off" and tree.importance_conf == "agent"
    assert tree.windows[0].lp_type == 1 and tree.windows[0].a11y_type == 1


def test_rules_revision_is_exposed():
    assert tb.TB_RULES_REV == "talkback@229212f (16.2)"
    assert tb.simulate(tb.build([root()])).to_dict()["rules"] == tb.TB_RULES_REV
