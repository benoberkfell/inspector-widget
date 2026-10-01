"""Web content in the TalkBack model (talkback.rules web_elements / order's web navigation),
calibrated on TalkBack 17.0 walks: Thunderbird's message body, A11yProbe V13 (a WebView on
an offscreen ViewPager2 page) and AntennaPod's player, where a WebView whose page is off
screen swallows every "next" (REALAPP_RESULTS B7).

TB = talkback/src/main/java/com/google/android/accessibility/talkback/ @229212f."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from inspector_widget import a11y
from inspector_widget import talkback as tb
from inspector_widget.proto import view_inspection_pb2 as pb
from inspector_widget.talkback import diff
from inspector_widget.talkback import rules as R

from test_tb_rules import FOCUS, VIS, n, root

HTML = ({"id": R.ACTION_NEXT_HTML_ELEMENT}, {"id": R.ACTION_PREVIOUS_HTML_ELEMENT})
WEBVIEW = "android.webkit.WebView"
WALKS = Path(__file__).parent / "data" / "tb_walks"


def web(host, vid, cls="android.view.View", **kw):
    """A node of a WebView's virtual tree (Chromium gives every one the HTML actions)."""
    kw.setdefault("node_key", f"virtual:{host}:{vid}")
    return n(host, vid, cls=cls, actions=HTML, **kw)


def webview(host, *elements, b=(0, 600, 1080, 800), root_b=None, host_flags=VIS):
    """A WebView as the agent dumps it: the View itself (no HTML actions) holding Chromium's
    root (class WebView, focusable, the HTML actions), which holds the page."""
    page_root = web(host, 4, cls=WEBVIEW, flags=VIS + ("focusable",), b=root_b or b,
                    children=elements)
    return n(host, cls=WEBVIEW, b=b, flags=host_flags, provider_class="com.example.NotesWebView",
             children=[page_root])


def button(host, text, y):
    return n(host, cls="android.widget.Button", text=text, flags=FOCUS, b=(0, y, 1080, 120))


def message_screen():
    """Thunderbird's message view, reduced: a button, the body WebView (a paragraph and an
    inline image), a button."""
    body = webview(30,
                   web(30, 7, cls="android.widget.TextView", text="Inline image below:",
                       b=(47, 620, 1000, 50)),
                   web(30, 2, cls="android.widget.Image", text="cid:part1@example",
                       b=(47, 700, 377, 380)))
    return [root(button(10, "Reply", 300), body, button(11, "Archive", 1500))]


# ------------------------------------------------------------------------- reading web content
def test_forward_reads_the_webview_root_then_its_elements_then_leaves():
    # findTargetFromNativeElement (TB/focusmanagement/FocusProcessorForLogicalNavigation.java
    # :1351): "returns WebView if find it first"; going forward the root is the target
    # ("Webview"), then ACTION_NEXT_HTML_ELEMENT moves through the page, then
    # navigateToHtmlTargetWithFallBack (:1541) leaves from the root. TalkBack 17 on
    # Thunderbird: "Webview", the paragraph, "cid:part1@example. Image".
    walk = tb.simulate(tb.build(message_screen()))
    assert walk.keys() == ["view:10", "virtual:30:4", "virtual:30:7", "virtual:30:2",
                           "view:11", "view:10"]
    assert walk.speech()[1:4] == ["Webview", "Inline image below:", "cid:part1@example. Image"]
    assert [s["via"] for s in walk.stops[2:4]] == ["web", "web"]
    assert [s["why"] for s in walk.stops[1:4]] == ["web", "web", "web"]


def test_backward_goes_to_the_last_element_and_skips_the_root():
    # findTargetFromMiddlePivot (:1459): going back into a WebView, PREVIOUS_HTML_ELEMENT on
    # its root lands on the last element; going back from the root (:1524) leaves it.
    walk = tb.simulate(tb.build(message_screen()), start="view:11", direction="prev",
                       until="steps", max_steps=3)
    assert walk.keys() == ["virtual:30:2", "virtual:30:7", "view:10"]


def test_reading_order_lists_the_web_elements_after_their_root():
    keys = [e["key"] for e in tb.reading_order(tb.build(message_screen()))["focus_order"]]
    assert keys == ["view:10", "virtual:30:4", "virtual:30:7", "virtual:30:2", "view:11"]


def test_containers_and_the_texts_inside_a_link_are_not_stops():
    # A11yProbe V13's page: a heading, a paragraph and two links, each link inside a <div>
    # with its text in a child. TalkBack 17 read the heading, the paragraph and the two links.
    def link(vid, div, text, y):
        return web(31, div, b=(0, y, 1080, 49), children=[
            web(31, vid, flags=FOCUS, role_description="link", text=text, b=(0, y, 151, 49),
                children=[web(31, vid + 100, cls="android.widget.TextView", text=text,
                              b=(0, y, 151, 49))])])
    page = webview(31,
                   web(31, 8, cls="android.widget.TextView", text="Show notes",
                       role_description="heading 2", flags=VIS + ("heading",),
                       b=(0, 610, 1080, 73)),
                   web(31, 7, cls="android.widget.TextView", text="We test screen readers.",
                       b=(0, 700, 1080, 51)),
                   link(2, 10, "Link one", 780), link(12, 9, "Link two", 860))
    walk = tb.simulate(tb.build([root(page)]))
    assert walk.keys()[:-1] == ["virtual:31:4", "virtual:31:8", "virtual:31:7", "virtual:31:2",
                                "virtual:31:12"]
    assert walk.speech()[1:5] == ["Show notes. heading 2", "We test screen readers.",
                                  "Link one. link", "Link two. link"]
    rules = R.Rules(tb.build([root(page)]))
    tree = rules.tree
    assert rules.focus_decision(tree.node("virtual:31:10")) == (False, "web_part")
    assert rules.focus_decision(tree.node("virtual:31:102")) == (False, "web_part")


def test_zero_size_elements_are_never_stops_and_are_counted_when_passed():
    # AntennaPod's show notes below the WebView's visible part: Chromium reports them at zero
    # height. Never a stop; the step after them says how many TalkBack reads there first (the
    # WebView scrolls them in; the dump cannot say what they are).
    page = webview(32,
                   web(32, 5, cls="android.widget.TextView", text="Visible paragraph",
                       b=(0, 610, 1080, 100)),
                   web(32, 6, cls="android.widget.TextView", text="Below the fold",
                       b=(0, 1400, 1080, 0)),
                   web(32, 7, flags=FOCUS, role_description="link", text="A link",
                       b=(0, 1400, 300, 0)),
                   web(32, 8, cls="android.widget.TextView", text="\n", b=(0, 700, 0, 57)))
    tree = tb.build([root(button(10, "Play", 300), page, button(11, "Next", 1500))])
    rules = R.Rules(tree)
    assert rules.focus_decision(tree.node("virtual:32:6")) == (False, "web_empty")
    walk = tb.simulate(tree)
    assert walk.keys()[:-1] == ["view:10", "virtual:32:4", "virtual:32:5", "view:11"]
    assert walk.stops[3]["web_unseen"] == 2
    assert walk.ended == "wrap"


def test_an_offscreen_webview_page_is_still_walked():
    # nodeFilterOrWebView accepts the root with no visibility check. TalkBack 17 on A11yProbe
    # V13: the WebView on the next (offscreen) page of a ViewPager2, its elements to the right
    # of the screen, was read in full.
    page = webview(33, web(33, 5, cls="android.widget.TextView", text="Offscreen notes",
                           b=(1100, 620, 1000, 60)),
                   b=(1080, 600, 0, 800), root_b=(1080, 600, 1080, 800), host_flags=("enabled",))
    walk = tb.simulate(tb.build([root(button(10, "Play", 300), page)],
                                obscured=[(0, 0, 1080, 100)]))
    assert walk.keys()[:-1] == ["view:10", "virtual:33:4", "virtual:33:5"]


# ------------------------------------------------------------------------- the AntennaPod trap
def pager_with_hidden_notes(in_pager=True):
    """AntennaPod's expanded player at walk time (walk_player.json): a vertical ViewPager2
    whose second page, the show notes WebView, peeks in at the bottom (its root 2164..2856)
    while every web element is clipped to zero height at the screen's bottom edge."""
    links = [web(40, vid, flags=FOCUS, role_description="link", text=t, b=(96, 2856, 600, 0))
             for vid, t in ((160, "Planet Money newsletter"), (161, "Instagram"))]
    notes = webview(40, web(40, 12, b=(24, 2856, 1233, 0), children=links),
                    b=(0, 2164, 1280, 692))
    handle = n(37, cls="android.widget.LinearLayout", cd="swipe up to read shownotes",
               flags=FOCUS, b=(415, 2032, 450, 108))
    page1 = n(36, cls="android.widget.FrameLayout", b=(0, 348, 1280, 2508), children=[
        n(35, cls="android.widget.TextView", text="Who's gonna pay for Social Security?",
          b=(24, 1867, 1232, 69)), handle, notes])
    if not in_pager:
        return [root(page1, b=(0, 0, 1280, 2856))]
    pager = n(34, cls="androidx.viewpager.widget.ViewPager", b=(0, 348, 1280, 2508),
              collection_info={"row_count": 2, "column_count": 1},
              actions=({"id": R.ACTION_SCROLL_FORWARD}, {"id": R.ACTION_PAGE_DOWN}),
              children=[page1])
    return [root(pager, b=(0, 0, 1280, 2856))]


def test_a_webview_whose_page_shows_nothing_swallows_next_a_trap():
    # navigateToHtmlTargetWithFallBack: Chromium finds an element (of zero size) and reports
    # ACTION_NEXT_HTML_ELEMENT done, so TalkBack keeps focus ("Return and reset reachEdge, web
    # element focus will be handled by the framework", :1172). AntennaPod, TalkBack 17.0: 19
    # presses never left the show notes.
    tree = tb.build(pager_with_hidden_notes())
    walk = tb.simulate(tree)
    assert walk.ended == "trap"
    assert walk.keys()[-2:] == ["view:37", "virtual:40:4"]
    last = walk.steps[-1]
    assert last["swallowed"] and last["web_root"] == "virtual:40:4"
    trap = next(d for d in walk.diagnostics if d["kind"] == "web_trap")
    assert trap["container"] == "view:34" and trap["elements"] == 2
    assert "vertical" not in trap["message"] and "pager" in trap["message"]
    assert walk.hints == [trap]
    assert tb.Navigator(tree).web_traps() == [dict(trap, at="virtual:40:4", before="view:37")]


def test_outside_a_pager_zero_size_web_content_is_scrolled_in_not_a_trap():
    # Without a pager, Chromium's focus request scrolls the page into view: not a trap.
    tree = tb.build(pager_with_hidden_notes(in_pager=False))
    assert tb.simulate(tree).ended == "wrap"
    assert tb.Navigator(tree).web_traps() == []


def test_a_stuck_walk_at_a_trapping_webview_is_a_trap_not_an_edge():
    tree = tb.build(pager_with_hidden_notes())
    traps = tb.Navigator(tree).web_traps()
    steps = [{"i": 0, "key": "view:35", "ref": "view:35", "moved": True, "via": "start"},
             {"i": 1, "key": "view:37", "ref": "view:37", "moved": True, "via": "next"},
             {"i": 2, "key": "view:37", "ref": "view:37", "moved": False, "via": "next"},
             {"i": 3, "key": "view:37", "ref": "view:37", "moved": False, "via": "next"}]
    walk = {"steps": steps, "ended": "stuck", "predicted": [], "direction": "next"}
    codes = {f["code"] for f in diff.analyze(dict(walk, web_traps=traps))["findings"]}
    assert "tb.trap" in codes and "tb.edge_stuck" not in codes
    trap = next(f for f in diff.analyze(dict(walk, web_traps=traps))["findings"]
                if f["code"] == "tb.trap")
    assert trap["sev"] == "error" and "noHideDescendants" in trap["fix"]
    plain = {f["code"] for f in diff.analyze(walk)["findings"]}
    assert "tb.edge_stuck" in plain


# ------------------------------------------------------------------- no web content in the dump
def test_a_webview_without_its_tree_is_diagnosed_not_unlabelled():
    # Chromium builds its accessibility tree only once a service queries it: in a dump taken
    # with TalkBack off the WebView is an empty box (a11y-core said "Unlabeled, web view").
    bare = n(50, cls=WEBVIEW, flags=FOCUS + ("long_clickable",), b=(0, 600, 1080, 800),
             provider_class="com.example.NotesWebView")
    tree = tb.build({"windows": [{"root_view_id": 1, "root": root(bare)}],
                     "diagnostics": "roots=1; a11y-services=off"})
    diag = next(d for d in tree.diagnostics if d["kind"] == "web_content_not_exposed")
    assert diag["keys"] == ["view:50"] and "TalkBack" in diag["message"]
    walk = tb.simulate(tree)
    stop = walk.stops[0]
    assert stop["speak"] == "Webview" and "unlabelled" not in stop
    on = tb.build({"windows": [{"root_view_id": 1, "root": root(bare)}],
                   "diagnostics": "roots=1; a11y-services=on"})
    assert "still loading" in next(d for d in on.diagnostics
                                   if d["kind"] == "web_content_not_exposed")["message"]


def test_a_webview_with_its_tree_is_not_diagnosed():
    assert not [d for d in tb.build(message_screen()).diagnostics
                if d["kind"] == "web_content_not_exposed"]


# ------------------------------------------------------------- calibration: A11yProbe V13 walk
def test_v13_model_walks_the_offscreen_webview_page_press_for_press():
    # The recorded TalkBack 17.0 walk (tests/data/tb_walks/tb_v13-bad-walk): the model used to
    # stop at the pager page and miss all five web stops (model.mismatch differ 5).
    rec = json.loads(gzip.decompress((WALKS / "tb_v13-bad-walk.json.gz").read_bytes()))
    resp = pb.DumpA11yResponse()
    resp.ParseFromString(gzip.decompress((WALKS / "tb_v13-bad-walk.a11y.pb.gz").read_bytes()))
    walk = tb.simulate(tb.build(a11y.a11y_to_dict(resp)), start=rec["steps"][0]["key"],
                       until="steps", max_steps=len(rec["steps"]) - 1, keyboard=True)
    actual = [s["key"] if s["moved"] else "edge" for s in rec["steps"][1:]]
    model = ["edge" if s.get("edge") else s["key"] for s in walk.steps]
    assert model == actual
    said = [s["speak"] for s in rec["steps"][1:] if s["moved"] and s.get("utt") == "logcat"]
    assert [s["speak"] for s in walk.stops][:len(said)] == said
