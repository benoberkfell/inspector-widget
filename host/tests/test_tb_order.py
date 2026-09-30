"""TalkBack 16.2's linear order (inspector_widget.talkback.order): OrderedTraversalController,
WorkingTree, ReorderedChildrenIterator, searchFocus, window traversal, edges and wrap.
UT = utils/src/main/java/com/google/android/accessibility/utils/ @229212f.

Where a test notes "a11y-core", inspector_widget.a11y.reading_order reads the same tree in a
different order; the difference is TalkBack's behaviour, which is what this model is for."""

from __future__ import annotations

import copy
import glob
import gzip
import json
import os

import pytest

from inspector_widget import a11y
from inspector_widget import talkback as tb
from inspector_widget.a11y import a11y_key
from inspector_widget.talkback import order as O
from inspector_widget.talkback import rules as R
from inspector_widget.talkback.explain import STOP_CODES

import mixed_fixture as mf
from test_tb_rules import FOCUS, SCROLL_FWD, SRF, VIS, c, compose_host, n, root, speech, stops


def chain(*nodes):
    """Link nodes as Compose does: each node's traversal_before is the next one."""
    for prev, nxt in zip(nodes, nodes[1:]):
        prev["traversal_before"] = nxt["id"]


def core_keys(*roots):
    return [e["key"] for e in a11y.reading_order(list(roots))["focus_order"]]


def btn(host, text, y, **kw):
    return n(host, cls="android.widget.Button", text=text, flags=FOCUS, b=(0, y, 500, 80), **kw)


def ctext(host, sid, text, y, flags=SRF, **kw):
    return c(host, sid, cls="android.widget.TextView", text=text, flags=flags,
             b=(0, y, 500, 80), **kw)


# ----------------------------------------------------------------------------------------- chains
def test_compose_chain_collapses_to_its_last_members_position():
    # moveNodeBefore (UT/traversal/OrderedTraversalController.java:152): a->b->c ends up at the
    # tree position of its LAST member, in chain order; x and y keep theirs.
    a, x, b, y, cc = (ctext(7, 1, "a", 0), ctext(7, 2, "x", 100), ctext(7, 3, "b", 200),
                      ctext(7, 4, "y", 300), ctext(7, 5, "c", 400))
    chain(a, b, cc)
    walk = tb.simulate(tb.build([root(compose_host(7, a, x, b, y, cc))]))
    assert walk.keys()[:5] == ["compose:7:2", "compose:7:4", "compose:7:1", "compose:7:3",
                               "compose:7:5"]
    assert [s["via"] for s in walk.stops[2:5]] == ["chain", "chain", "chain"]


def test_compose_chain_overrides_reversed_ani_order():
    third, second, first = ctext(7, 1, "3", 0), ctext(7, 2, "2", 100), ctext(7, 3, "1", 200)
    chain(first, second, third)
    assert speech(root(compose_host(7, third, second, first))) == ["1", "2", "3"]


def test_scaffold_chain_reads_the_top_bar_first():
    # The live IconButton screen: content composed before the top bar; the chain puts the
    # top bar first.
    back = c(7, 150, flags=FOCUS, b=(10, 100, 100, 100), children=[
        c(7, 151, cd="Back", b=(30, 120, 60, 60))])
    title = ctext(7, 153, "Screen title", 250, flags=SRF + ("heading",))
    body = ctext(7, 155, "Body", 400)
    content = c(7, 152, cls="android.widget.ScrollView", b=(0, 240, 1080, 1500),
                children=[title, body])
    top = c(7, 5, b=(0, 90, 1080, 140), children=[back])
    chain(back, title, body)
    assert speech(root(compose_host(7, content, top))) == [
        "Back", "Screen title. Heading", "Body"]


def test_move_node_before_lifts_the_parent_that_points_at_it():
    # getParentsThatAreMovedBeforeOrSameNode (:193): P.before = C (its own child: a no-op,
    # :157), then C.before = H moves P's whole subtree before H, and H becomes C's last child.
    # a11y-core moves C alone: C, H, P, E, D.
    h = btn(2, "H", 0)
    cc = btn(4, "C", 200)
    e = btn(5, "E", 300)
    p = n(3, cls="android.widget.Button", text="P", flags=FOCUS, b=(0, 100, 1080, 300),
          children=[cc, e])
    d = btn(6, "D", 500)
    p["traversal_before"] = cc["id"]
    cc["traversal_before"] = h["id"]
    tree = root(h, p, d)
    assert stops(tree) == ["view:3", "view:4", "view:2", "view:5", "view:6"]
    assert core_keys(tree) == ["view:4", "view:2", "view:3", "view:5", "view:6"]


def test_traversal_after_makes_the_node_the_targets_last_child():
    # moveNodeAfter (:220): C.after = A makes C A's LAST child. A.before = B already made B
    # A's last child, so C follows B. a11y-core reads C straight after A: X, A, C, B.
    a, x, b, cc = btn(2, "A", 0), btn(3, "X", 100), btn(4, "B", 200), btn(5, "C", 300)
    a["traversal_before"] = b["id"]
    cc["traversal_after"] = a["id"]
    tree = root(a, x, b, cc)
    walk = tb.simulate(tb.build([tree]))
    assert walk.keys()[:4] == ["view:3", "view:2", "view:4", "view:5"]
    assert walk.stops[1]["via"] == "before:view:4" and walk.stops[3]["via"] == "after:view:2"
    assert core_keys(tree) == ["view:3", "view:2", "view:5", "view:4"]


def test_traversal_before_wins_even_when_its_target_is_elsewhere():
    # reorderTree (:137-147): a resolvable before-link is used even if its target is not in
    # this window's tree; the after-link is then never looked at.
    other = n(9, cls="android.widget.TextView", text="Other window", b=(0, 0, 500, 80))
    a = btn(2, "A", 0, traversal_before=other["id"])
    b = btn(3, "B", 100)
    a["traversal_after"] = b["id"]
    w1, w2 = root(a, b), root(other, host=8, b=(0, 1600, 1080, 800))
    keys = [e["key"] for e in tb.reading_order([w1, w2])["focus_order"]]
    assert keys == ["view:2", "view:3", "view:9"]


def test_traversal_group_is_unknown_to_talkback():
    # TalkBack has no traversal groups: g2.before = x splits the group (g2, x, g1). a11y-core
    # moves the group as a unit (g1, g2, x). Compose uses isTraversalGroup only to decide the
    # links it publishes.
    x = btn(2, "x", 0)
    g1, g2 = btn(4, "g1", 200), btn(5, "g2", 300)
    g2["traversal_before"] = x["id"]
    group = n(3, b=(0, 200, 1080, 300), is_traversal_group=True, children=[g1, g2])
    tree = root(x, group)
    assert stops(tree) == ["view:5", "view:2", "view:4"]
    assert core_keys(tree) == ["view:4", "view:5", "view:2"]


def test_before_cycle_detaches_both_nodes_and_traps_focus():
    # A.before = B, B.before = A: the second move lifts A (A.before == B), detaches it, and the
    # swap into A's old parent finds nothing (WorkingTree.swapChild: "swap child not found"), so
    # A and B loop off the root: A's child is B and B's child is A. A swipe from the top cannot
    # reach them; once focus is on one (by touch), next alternates A, B, A, ... for ever (each
    # searchFocus starts afresh, so its duplicate check never fires). a11y-core keeps ANI order
    # and reports the cycle.
    a, b = btn(2, "A", 0), btn(3, "B", 100)
    a["traversal_before"] = b["id"]
    b["traversal_before"] = a["id"]
    tree = tb.build([root(a, b)])
    walk = tb.simulate(tree)
    assert walk.stops == [] and walk.ended == "empty"
    assert any(d["kind"] == "detached" for d in walk.diagnostics)
    trapped = tb.simulate(tree, start="view:2")
    assert trapped.keys() == ["view:3", "view:2"] and trapped.ended == "loop"
    assert core_keys(root(a, b)) == ["view:2", "view:3"]


def test_after_cycle_resolves_to_one_order():
    a, b = btn(2, "A", 0), btn(3, "B", 100)
    a["traversal_after"] = b["id"]
    b["traversal_after"] = a["id"]
    assert stops(root(a, b)) == ["view:3", "view:2"]


def test_search_focus_duplicate_is_an_edge():
    class Loop:
        def find(self, node, forward):
            return {"a": "b", "b": "c", "c": "b"}[node]
    assert O.search_focus(Loop(), "a", True, lambda node: False) == (None, True)
    assert O.search_focus(Loop(), "a", True, lambda node: node == "c") == ("c", False)


# ------------------------------------------------------------------------------ bounds reordering
def test_wrapper_whose_focusable_content_sits_lower_moves_after_its_sibling():
    # ReorderedChildrenIterator (UT/traversal/ReorderedChildrenIterator.java:115-187): W is not
    # a stop, so its effective bounds are its focusable child's (y 400..500, clipped to W's own,
    # NodeCachedBoundsCalculator.fetchBound). Against B (y 100..200) the STRIPE compare of the
    # effective bounds wants a swap and the real bounds (W starts at y 0) agree with ANI order,
    # so W moves after B. Only TalkBack does this; a11y-core reads K first.
    k = btn(3, "K", 400)
    w = n(2, cls="android.widget.FrameLayout", b=(0, 0, 1080, 600), children=[k])
    b = btn(4, "B", 100)
    tree = root(w, b)
    walk = tb.simulate(tb.build([tree]))
    assert walk.keys()[:2] == ["view:4", "view:3"]
    assert walk.stops[1]["via"] == "bounds_swap"
    assert core_keys(tree) == ["view:3", "view:4"]


def test_no_swap_when_the_real_bounds_already_disagree():
    # needSwapNodeOrder (:149): if the real bounds also say "swap", the compare is not trusted.
    k = btn(3, "K", 400)
    w = n(2, cls="android.widget.FrameLayout", b=(0, 300, 1080, 300), children=[k])
    b = btn(4, "B", 100)
    assert stops(root(w, b)) == ["view:3", "view:4"]


def test_speech_children_follow_the_reordered_order_too():
    # TreeNodesDescription iterates children through a ReorderedChildrenIterator as well.
    lower = n(3, cls="android.widget.FrameLayout", b=(0, 0, 500, 300), children=[
        n(4, cls="android.widget.TextView", text="second", b=(0, 200, 500, 100),
          flags=VIS + ("focusable",))])
    upper = n(5, cls="android.widget.TextView", text="first", b=(0, 50, 500, 100))
    card = n(2, flags=FOCUS, b=(0, 0, 500, 300), children=[lower, upper])
    tree = tb.build([root(card)])
    kids, moved = O.reordered_children(R.Rules(tree), tree.node("view:2"))
    assert [k.key for k in kids] == ["view:5", "view:3"] and moved


# --------------------------------------------------------------------------- edges, wrap, windows
def test_edge_then_wrap_in_one_window():
    # findTargetAcrossWindows (TB/focusmanagement/FocusProcessorForLogicalNavigation.java:1620):
    # the first press past the last stop only sets reachEdge; the second wraps (:1203).
    walk = tb.simulate(tb.build([root(btn(2, "A", 0), btn(3, "B", 100))]))
    assert [(s.get("key"), s.get("edge", False), s.get("via")) for s in walk.steps] == [
        ("view:2", False, "tree"), ("view:3", False, "tree"), ("view:3", True, None),
        ("view:2", False, "wrap")]
    assert walk.ended == "wrap"


def test_previous_from_a_start_node_walks_backwards_and_wraps_to_the_end():
    tree = tb.build([root(btn(2, "A", 0), btn(3, "B", 100), btn(4, "C", 200))])
    walk = tb.simulate(tree, start="view:3", direction="prev")
    assert [s.get("key") for s in walk.steps] == ["view:2", "view:2", "view:4", "view:3",
                                                   "view:2"]
    assert walk.steps[1]["edge"] and walk.steps[2]["via"] == "wrap"


def test_windows_are_read_in_geometric_order_with_one_pause_at_the_end():
    # WindowTraversal (TB/focusmanagement/WindowTraversal.java:52): windows sort by top, then
    # left, not by z-order. A non-modal popup lower on screen is read after the activity; no
    # pause between windows, a pause after the last one, then the first window again.
    activity = root(btn(2, "Main", 100), host=1)
    popup = root(btn(11, "Popup item", 1600), host=10, b=(100, 1500, 800, 400))
    dump = {"windows": [{"root_view_id": 1, "root": activity}, {"root_view_id": 10, "root": popup}],
            "diagnostics": "root#1 window type=1 flags=0x0; root#10 window type=1000 flags=0x28"}
    walk = tb.simulate(tb.build(dump))
    assert [(s.get("key"), s.get("via")) for s in walk.steps] == [
        ("view:2", "tree"), ("view:11", "window:1"), ("view:11", None), ("view:2", "window:0")]


def test_a_popup_at_the_same_top_left_as_the_activity_is_read_first():
    # WindowOrderComparator ties (same top, same left, different size) keep getWindows()
    # order, which lists the top-most window first.
    activity = root(btn(2, "Main", 300), host=1)
    popup = root(btn(11, "Menu item", 50), host=10, b=(0, 0, 600, 400))
    dump = {"windows": [{"root_view_id": 1, "root": activity}, {"root_view_id": 10, "root": popup}],
            "diagnostics": "root#1 window type=1 flags=0x0; root#10 window type=1000 flags=0x20"}
    assert tb.reading_order(tb.build(dump))["focus_order"][0]["key"] == "view:11"


def test_autoscroll_is_reported_at_the_edge_of_a_scrolling_list():
    # autoScrollAtEdge (:2264): leaving a FILTER_AUTO_SCROLL container that can scroll forward
    # first scrolls it. The model reports the scroll on that step; it cannot know what scrolls in.
    rows = [n(10 + i, cls="android.widget.LinearLayout", flags=FOCUS, b=(0, 200 * i, 1080, 200),
              children=[n(20 + i, cls="android.widget.TextView", text=f"Row {i}",
                          b=(0, 200 * i, 1080, 200))]) for i in range(3)]
    lst = n(2, cls="androidx.recyclerview.widget.RecyclerView", flags=VIS + ("scrollable",),
            actions=[SCROLL_FWD], collection_info={"row_count": 30, "column_count": 1},
            b=(0, 0, 1080, 600), children=rows)
    after = btn(3, "After the list", 700)
    walk = tb.simulate(tb.build([root(lst, after)]))
    assert walk.keys()[:4] == ["view:10", "view:11", "view:12", "view:3"]
    assert [s.get("autoscroll") for s in walk.stops[:4]] == [None, None, None, "view:2"]
    pager = n(4, cls="androidx.viewpager.widget.ViewPager", flags=VIS + ("scrollable",),
              actions=[SCROLL_FWD], b=(0, 0, 1080, 600), children=[btn(5, "Page 1", 100)])
    walk = tb.simulate(tb.build([root(pager, btn(6, "Next", 700))]))
    assert "autoscroll" not in walk.stops[1]  # pagers are not auto-scrolled


# -------------------------------------------------------------------------------- mixed hierarchy
def _mixed_with_dialog(modal: bool):
    resp = mf.a11y_response()
    b = mf.A11yBuilder()
    b.sb = mf.StringTableBuilder()
    dialog = b.node(90, -1, (140, 900, 800, 500), cls="android.widget.FrameLayout", children=[
        b.node(91, -1, (180, 940, 700, 80), cls="android.widget.TextView", text="Delete item?"),
        b.node(92, -1, (600, 1260, 300, 120), cls="android.widget.Button", text="Delete",
               flags=mf.FOCUS)])
    extra = b.response([dialog])
    # Merge the dialog window into the mixed response (re-interning its strings).
    merged = mf.A11yBuilder()
    merged_root = _copy_node(merged, resp.windows[0].root, resp.strings)
    merged_dialog = _copy_node(merged, extra.windows[0].root, extra.strings)
    out = merged.response([merged_root, merged_dialog])
    flags = 0x1820002 if modal else 0x28
    out.diagnostics = (f"roots=2; api=37; a11y-services=on; root#2 window type=1 flags=0x81810100; "
                       f"root#90 window type=2 flags=0x{flags:x}")
    return a11y.a11y_to_dict(out)


def _copy_node(b, node, strings):
    from inspector_widget.strings import StringResolver
    r = StringResolver(strings)
    new = type(node)()
    new.CopyFrom(node)
    for f in ("text", "content_description", "class_name", "provider_class", "hint_text",
              "state_description", "role_description", "error"):
        setattr(new, f, b.sb.intern(r.opt(getattr(node, f))))
    del new.children[:]
    for ch in node.children:
        new.children.add().CopyFrom(_copy_node(b, ch, strings))
    return new


def test_mixed_hierarchy_recycler_of_compose_cells_and_androidview_in_compose():
    # A RecyclerView of ComposeView cells, a View cell, and an AndroidView (a RecyclerView of
    # ComposeViews) inside a Compose footer: TalkBack's stops match a11y-core's; only the words
    # differ (TalkBack's own strings, no "not checked" for an unchecked Compose Checkbox).
    d = a11y.a11y_to_dict(mf.a11y_response())
    ro = tb.reading_order(d)
    assert [e["key"] for e in ro["focus_order"]] == [e["key"] for e in d["focus_order"]]
    assert [e["speak"] for e in ro["focus_order"]] == [
        "Inbox. Heading",
        "Item 0 (compose)", "Check box", "Delete. Button",
        "Item 1 (compose)", "checked. Check box", "Delete. Button",
        "Item 2 (compose)", "Check box", "Delete. Button",
        "Item 3 (view)", "Button",
        "Compose footer",
        "Nested item",
    ]
    walk = tb.simulate(d)
    assert walk.stops[1]["speak"] == "Item 0 (compose). 1 of 4. In list. 4 items"
    assert walk.stops[12]["speak"] == "Compose footer. Out of list"


def test_mixed_hierarchy_with_a_modal_dialog_hides_the_activity():
    # AOSP AccessibilityWindowManager drops windows under a touch-modal window: TalkBack only
    # reaches the dialog.
    d = _mixed_with_dialog(modal=True)
    walk = tb.simulate(d)
    assert walk.keys() == ["view:91", "view:92", "view:91"]
    tree = tb.build(d)
    assert tb.explain(tree, "view:11")["why"] == "covered_by:90"
    assert all(e.get("window") == 1 for e in tb.reading_order(d)["focus_order"])


def test_mixed_hierarchy_with_a_non_modal_popup_reads_both_windows():
    d = _mixed_with_dialog(modal=False)
    keys = [e["key"] for e in tb.reading_order(d)["focus_order"]]
    assert keys[0] == "view:11" and keys[-2:] == ["view:91", "view:92"]


# ------------------------------------------------------------------------------------ the adapter
def test_reading_order_has_the_a11y_core_shape():
    tree = root(n(2, cls="android.widget.TextView", text="Title", b=(0, 0, 500, 80)),
                n(3, flags=FOCUS, b=(0, 100, 100, 100)))
    ro = tb.reading_order([tree])
    assert set(ro) == {"focus_order", "diagnostics", "_nodes"}
    assert ro["focus_order"] == [
        {"order": 1, "key": "view:2", "id": a11y_key(2, -1), "speak": "Title"},
        {"order": 2, "key": "view:3", "id": a11y_key(3, -1), "speak": "Unlabelled",
         "unlabeled": True}]
    assert ro["_nodes"][0] is tree["children"][0]
    structural = tb.reading_order([tree], include_structural=True)["focus_order"]
    assert structural[0] == {"order": None, "key": "view:1", "id": a11y_key(1, -1),
                             "speak": None, "is_focus_stop": False}


def test_reading_order_skip_matches_a11y_core():
    w1, w2 = root(btn(2, "A", 0)), root(btn(11, "B", 0), host=10, b=(0, 500, 1080, 500))
    ro = tb.reading_order([w1, w2], skip={0})
    assert [(e["key"], e["window"]) for e in ro["focus_order"]] == [("view:11", 1)]


def test_granularity_control_skips_list_rows():
    rows = [n(10 + i, cls="android.widget.LinearLayout", flags=FOCUS, b=(0, 100 * i, 1080, 100),
              children=[n(20 + i, cls="android.widget.TextView", text=f"Row {i}",
                          b=(0, 100 * i, 1080, 100))]) for i in range(2)]
    lst = n(2, cls="android.widget.ListView", b=(0, 0, 1080, 200), children=rows)
    sw = n(3, cls="android.widget.Switch", text="Airplane mode", flags=FOCUS + ("checkable",),
           b=(0, 300, 1080, 100))
    walk = tb.simulate(tb.build([root(lst, sw)]), granularity="control")
    assert walk.keys()[0] == "view:3"


@pytest.mark.parametrize("direction", ["sideways", "up"])
def test_bad_direction_is_rejected(direction):
    with pytest.raises(ValueError):
        tb.simulate(tb.build([root()]), direction=direction)


# ------------------------------------------------------------------------- acceptance: live dumps
# host/tests/data/tb/a11yprobe_*.json.gz: the a11yprobe interop scenarios (S1 RecyclerView of
# ComposeView cells, S2 View cells, S3 mixed cells, S4 LazyColumn with AndroidView rows, S5
# Compose > AndroidView > RecyclerView, S6 a RecyclerView grid, D1/D2 dialogs over View and
# Compose screens), the IconButton screen and the View-XML screen, dumped live on
# emulator-5554 (API 37, no accessibility service) with the host-key ids, and reduced to the
# fields the models read.
DATA = os.path.join(os.path.dirname(__file__), "data", "tb")
FIXTURES = sorted(glob.glob(os.path.join(DATA, "a11yprobe_*.json.gz")))

# Fixture -> the stops where TalkBack's model and a11y-core's reading_order may differ, with
# the reason. Empty: on these screens the TalkBack-only rules (bounds reordering, window
# wrappers, invisible-text children, CollectionInfo text, traversal groups, before-cycles) do
# not fire, and both models read the same stops in the same order. Their differences are
# pinned by the hand-built tests above.
KNOWN_DIFFERENCES: dict = {}


def _load(path):
    with gzip.open(path, "rt") as f:
        return json.load(f)


def _core_keys(dump):
    d = copy.deepcopy(dump)
    roots = [w["root"] for w in d["windows"] if w.get("root")]
    a11y.assign_node_keys(roots)
    a11y.mark_talkback_ignored(roots)
    a11y.apply_window_meta(d["windows"], d.get("diagnostics") or "")
    with_root = [w for w in d["windows"] if w.get("root")]
    skip = {i for i, w in enumerate(with_root) if w.get("covered_by") is not None}
    return [e["key"] for e in a11y.reading_order(roots, skip=skip)["focus_order"]]


@pytest.mark.parametrize("path", FIXTURES, ids=[os.path.basename(p) for p in FIXTURES])
def test_live_fixture_stops_match_a11y_core(path):
    dump = _load(path)
    mine = [e["key"] for e in tb.reading_order(dump)["focus_order"]]
    assert mine == KNOWN_DIFFERENCES.get(os.path.basename(path), _core_keys(dump))


@pytest.mark.parametrize("path", FIXTURES, ids=[os.path.basename(p) for p in FIXTURES])
def test_live_fixture_walks_to_a_wrap_and_explains_every_stop(path):
    tree = tb.build(_load(path))
    walk = tb.simulate(tree)
    assert walk.ended == "wrap"
    assert walk.stops[-1]["key"] == walk.stops[0]["key"]
    stops = walk.stops[:-1]
    assert len({s["key"] for s in stops}) == len(stops)  # every stop once per lap
    assert all(s["why"] in STOP_CODES for s in stops)
    assert all(s["speak"] for s in stops)
    for s in stops:  # explain agrees with the walk
        assert tb.explain(tree, s["key"])["stop"] is True
    # The dumps were taken with no service on: Compose's AndroidView holders are corrected.
    holders = [n for n in tree.nodes if n.facet == "interop"]
    assert all(not h.visible for h in holders)



# ------------------------------------------------------------------ calibration: TalkBack 17.0
# host/tests/data/tb/tb17_observed.json: what TalkBack 17.0.0 did on emulator-5554 (the spike's
# second pass, TALKBACK_DESIGN.md part 9): each walk's focus sequence (from the a11y-core agent;
# keys match the a11yprobe fixtures, which came from the same app build), the utterances from
# the verbose log, the initial focus each window got, and the activity titles
# (InteropActivity: title = scenario.title).
OBSERVED = json.load(open(os.path.join(DATA, "tb17_observed.json"), encoding="utf-8"))


def with_window_meta(dump, name):
    """The fixture plus what the current agent reports and these dumps predate: window types and
    flags (the activity; a modal dialog above it) and the activity's title."""
    d = copy.deepcopy(dump)
    tokens = []
    for i, w in enumerate(d["windows"]):
        if i == 0:
            w["title"] = OBSERVED["window_titles"][name]
            tokens.append(f"root#{w['root_view_id']} window type=1 flags=0x81810100")
        else:
            tokens.append(f"root#{w['root_view_id']} window type=2 flags=0x1820002")
    d["diagnostics"] = (d.get("diagnostics") or "") + "; " + "; ".join(tokens)
    return d


def as_sequence(steps):
    return ["<edge>" if s.get("edge") else s["key"] for s in steps]


@pytest.mark.parametrize("name", ["S1", "S4", "D1", "D2", "view_xml"])
def test_tb17_walks_match_the_model_press_for_press(name):
    # a11y-core matched these at every stop; this model must too. The walk starts where the
    # spike put focus and presses Meta+Right; an edge press is "<edge>".
    walk = OBSERVED["walks"][name]
    dump = _load(os.path.join(DATA, f"a11yprobe_{name}.json.gz"))
    if name != "view_xml":
        dump = with_window_meta(dump, name)
    tree = tb.build(dump)
    # The fixtures were dumped on a 2076x2152 screen, the walks ran on 1280x2856: a stop the
    # fixture has below the fold (invisible there) cannot be one in the model.
    below_fold = {k.key for k in tree.nodes if not k.visible}
    observed = ["<edge>" if s.get("edge") else s["key"] for s in walk]
    assert [k for k in observed if k in below_fold] == (
        ["view:33"] if name == "view_xml" else [])  # "Manage your account settings."
    observed = [k for k in observed if k not in below_fold]
    model = tb.simulate(tree, start=walk[0]["key"], until="steps", max_steps=len(observed) - 1)
    assert [walk[0]["key"]] + as_sequence(model.steps) == observed


@pytest.mark.parametrize("name,key,skipped", [
    # S1: the heading reads as the window title (InteropActivity's title), so it is skipped.
    ("S1", "compose:14:3", ["view:10"]),
    # S4: the same heading, but inside the LazyColumn: a list ancestor exempts it.
    ("S4", "compose:11:5", []),
    # D1: the dialog has no title of its own; TalkBack takes the first text of its
    # TYPE_WINDOW_STATE_CHANGED event ("Share item") and skips the heading that says it. The
    # unlabelled button gets the focus: a real tb.initial_focus bug.
    ("D1", "compose:26:4", ["view:14"]),
    # D2: a Compose Dialog populates no View text, so it has no title and the heading is kept.
    ("D2", "compose:17:14", []),
])
def test_tb17_initial_focus_follows_the_window_title_rule(name, key, skipped):
    tree = tb.build(with_window_meta(_load(os.path.join(DATA, f"a11yprobe_{name}.json.gz")), name))
    got = tb.Navigator(tree).initial_focus()
    assert (got["key"], got["how"], got["skipped"]) == (key, "first_content", skipped)
    walk = tb.simulate(tree, start="initial", max_steps=1)
    spoken = OBSERVED["initial_focus"][name]
    if name == "D1":
        # TalkBack also said "Out of list": its collection state still held the list the focus
        # was in before the dialog opened. The model starts without that history.
        spoken = spoken.replace(". Out of list", "")
    assert walk.steps[0]["speak"] == spoken
    assert walk.steps[0]["via"] == "initial:first_content"


# (1) + (2): main's older View screen (no fitsSystemWindows). "1. ImageButton contentDescription"
# sits under the status bar (y 48-149 of 0-156): outside the window's interactive region, the
# platform serves it to TalkBack as not visible (ASSUMED; the in-process dump says visible). The
# focusable, window-sized ScrollView has no scroll action (its content fits), so it fails
# FILTER_AUTO_SCROLL, and that invisible text child makes it speak (UT:1109): a whole-screen stop,
# first in order, reading the entire screen through the focus event's text.
def _statusbar_fixture():
    with gzip.open(os.path.join(DATA, "spike_view_statusbar.json.gz"), "rt") as f:
        return json.load(f)


def test_tb17_scrollview_is_a_whole_screen_stop_when_its_first_text_is_under_the_status_bar():
    dump = _statusbar_fixture()
    tree = tb.build(dump, obscured=[tuple(dump["status_bar"])])
    # The observed list starts with focus already on the ScrollView; from no focus, the first
    # press lands there.
    walk = tb.simulate(tree, until="steps", max_steps=len(dump["observed"]))
    observed = ["<edge>" if s.get("edge") else s["key"] for s in dump["observed"]]
    assert [s.get("key") if not s.get("edge") else "<edge>" for s in walk.steps] == observed
    first = walk.stops[0]
    assert (first["key"], first["why"]) == ("view:12", "focusable")
    assert first["parts"][0]["kind"] == "event"
    assert first["speak"].startswith("1. ImageButton contentDescription, Like this photo, ")
    assert first["speak"].endswith("(labelFor) Email address, name@example.com, name@example.com")
    ex = tb.explain(tree, "view:12")
    assert ex["ghost"] == ["invisible_children_only"] and "view:14" in ex["detail"]
    assert tb.explain(tree, "view:14")["why"] == "obscured_by_system_bar"
    assert any(d["kind"] == "obscured_by_system_bar" and d["conf"] == "assumed"
               for d in tree.diagnostics)


def test_without_the_status_bar_the_model_gives_the_old_a11y_core_answer():
    # The two mismatches the spike found in a11y-core's order on this screen: it stops on
    # "1. ImageButton ..." and never on the ScrollView. Without knowing the system bars, this
    # model says the same; the agent should report the window insets (T2).
    dump = _statusbar_fixture()
    keys = [e["key"] for e in tb.reading_order(tb.build(dump))["focus_order"]]
    assert keys[0] == "view:14" and "view:12" not in keys
    assert keys == _core_keys(dump)


# (3) ensureOnScreen: TalkBack shows a target that reaches the list's edge before focusing it
# (ACTION_SHOW_ON_SCREEN), then scrolls the list at its edge (SCROLL_FORWARD). The spike logged
# both on the launcher: "Section heading" (SHOW_ON_SCREEN) > "Redundant label" (SCROLL_FORWARD).
# Both are in the 16.2 source (FocusProcessorForLogicalNavigation.java:1238 ensureOnScreen and
# :2264 autoScrollAtEdge), so this is not a 17.x difference.
def test_a_target_on_the_lists_edge_is_shown_first_then_the_list_scrolls():
    rows = [n(10 + i, cls="android.widget.LinearLayout", flags=FOCUS,
              b=(0, 348 + 219 * i, 1280, 216),
              children=[n(20 + i, cls="android.widget.TextView", text=f"Row {i}",
                          b=(0, 348 + 219 * i, 1280, 216))]) for i in range(3)]
    rows[-1]["bounds"]["layout"]["h"] = 100  # the last row is cut off by the list's bottom
    rows[-1]["children"][0]["bounds"]["layout"]["h"] = 100
    lst = n(2, cls="androidx.recyclerview.widget.RecyclerView", flags=VIS + ("scrollable",),
            actions=[SCROLL_FWD], collection_info={"row_count": 30, "column_count": 1},
            b=(0, 348, 1280, 538), children=rows)
    walk = tb.simulate(tb.build([root(lst, btn(3, "After", 1000))]))
    assert walk.keys()[:4] == ["view:10", "view:11", "view:12", "view:3"]
    assert [s.get("show_on_screen") for s in walk.stops[:3]] == [None, None, "view:2"]
    assert walk.stops[3].get("autoscroll") == "view:2"


# (4) A 27 px sliver of a scrolled-off Compose row became an "Unlabelled. In list" stop after the
# wrap (the launcher, walk_main_log.json). Compose drops the offscreen children of the partly
# visible row, so the clickable row is a leaf: always a stop, with nothing to say.
def _sliver_screen(row_children=()):
    title = c(7, 4, cls="android.widget.TextView", text="A11yProbe", flags=SRF,
              b=(48, 210, 322, 84))
    sliver = c(7, 20, flags=FOCUS + ("screen_reader_focusable",), b=(0, 348, 1280, 27),
               children=list(row_children))
    row = c(7, 31, flags=FOCUS + ("screen_reader_focusable",), b=(0, 375, 1280, 216), children=[
        c(7, 32, cls="android.widget.TextView", text="Icon button label", b=(48, 400, 600, 80)),
        c(7, 33, cls="android.widget.TextView", text="MissingContentDescription",
          b=(48, 480, 800, 60))])
    lst = c(7, 3, b=(0, 348, 1280, 2508), actions=[SCROLL_FWD, {"id": R.ACTION_SCROLL_BACKWARD}],
            flags=VIS + ("scrollable",), collection_info={"row_count": -1, "column_count": 1},
            children=[sliver, row])
    return tb.build([root(compose_host(7, title, lst, b=(0, 0, 1280, 2856)), b=(0, 0, 1280, 2856))])


def test_tb17_a_clipped_row_sliver_is_an_unlabelled_ghost_stop():
    tree = _sliver_screen()
    walk = tb.simulate(tree)
    assert walk.speech()[:3] == ["A11yProbe", "Unlabelled. In list",
                                 "Icon button label. MissingContentDescription"]
    ex = tb.explain(tree, "compose:7:20")
    assert ex["why"] == "leaf" and ex["ghost"] == ["unlabelled", "clipped:compose:7:3"]
    assert "ghost" not in tb.explain(tree, "compose:7:31")


def test_a_sliver_whose_children_are_only_invisible_is_a_ghost_too():
    # Compose 1.11+ keeps some offscreen children and marks them invisible instead: then the row
    # speaks only through them (UT:1109) and says nothing useful.
    tree = _sliver_screen([c(7, 21, cls="android.widget.TextView", text="Scrolled away",
                             flags=("enabled",), b=(48, 200, 600, 80))])
    ex = tb.explain(tree, "compose:7:20")
    assert ex["stop"] and "invisible_children_only" in ex["ghost"]


# (5) Node identity across captures excludes ids and bounds.
def test_signature_ignores_ids_and_bounds():
    a = tb.build([root(n(2, cls="android.widget.Button", text="Play", flags=FOCUS,
                         b=(0, 0, 100, 50)))])
    b = tb.build([root(n(9, cls="android.widget.Button", text="Play", flags=FOCUS,
                         b=(0, 700, 100, 50)))])
    assert a.node("view:2").signature == b.node("view:9").signature == (
        "view", "android.widget.Button", "Play", "")
