"""Web content in the TalkBack model (talkback.rules web_elements / order's web navigation),
calibrated on TalkBack 17.0 walks: Thunderbird's message body, A11yProbe V13 (a WebView on
an offscreen ViewPager2 page, read in full), AntennaPod's home (the collapsed player's show
notes, read though every element is 0px tall off screen) and AntennaPod's expanded player,
where TalkBack cannot focus that WebView and never gets past it (REALAPP_RESULTS B7).

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


def test_zero_size_elements_are_stops_too():
    # Chromium moves through the page in document order whether an element is on screen or
    # not. Live on TalkBack 17.0, AntennaPod's home: TalkBack read the collapsed player's show
    # notes element by element, every one reported 0px tall below the screen.
    page = webview(32,
                   web(32, 5, cls="android.widget.TextView", text="Visible paragraph",
                       b=(0, 610, 1080, 100)),
                   web(32, 6, cls="android.widget.TextView", text="Below the fold",
                       b=(0, 1400, 1080, 0)),
                   web(32, 7, flags=FOCUS, role_description="link", text="A link",
                       b=(0, 1400, 300, 0)),
                   web(32, 8, cls="android.widget.TextView", text="\n", b=(0, 700, 0, 57)))
    tree = tb.build([root(button(10, "Play", 300), page, button(11, "Next", 1500))])
    walk = tb.simulate(tree)
    assert walk.keys()[:-1] == ["view:10", "virtual:32:4", "virtual:32:5", "virtual:32:6",
                                "virtual:32:7", "view:11"]
    assert R.Rules(tree).focus_decision(tree.node("virtual:32:8")) == (False, "web_part")


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
def player_with_notes_page(hidden=True, root_on_screen=True):
    """AntennaPod's player (live on emulator-5554, TalkBack 17.0): a vertical ViewPager2 whose
    second page, the show notes WebView, is clipped to nothing below the first (the WebView
    View is not visible to the user). Expanded, Chromium's root still reports itself on screen
    (2164..2856); collapsed, on the home screen, it reports 0px tall below the screen."""
    root_b = (0, 2164, 1280, 692) if root_on_screen else (0, 4564, 1280, 0)
    notes = webview(40,
                    web(40, 10, cls="android.widget.TextView", text="Very soon, Social Security",
                        b=(96, root_b[1] + 96, 1017, 228 if root_on_screen else 0)),
                    web(40, 160, flags=FOCUS, role_description="link", text="Instagram",
                        b=(120, 2856 if root_on_screen else 4564, 219, 0)),
                    b=(0, root_b[1], 1280, 0) if hidden else (0, 2164, 1280, 692),
                    root_b=root_b, host_flags=("enabled",) if hidden else VIS)
    handle = n(37, cls="android.widget.LinearLayout", cd="swipe up to read shownotes",
               flags=FOCUS, b=(415, 2032, 450, 108))
    page1 = n(36, cls="android.widget.FrameLayout", b=(0, 348, 1280, 1816), children=[
        n(35, cls="android.widget.TextView", text="Who's gonna pay for Social Security?",
          b=(24, 1867, 1232, 69)), handle])
    page2 = n(39, cls="android.widget.FrameLayout", b=(0, 2164, 1280, 0), flags=("enabled",),
              children=[notes])
    pager = n(34, cls="androidx.viewpager.widget.ViewPager", b=(0, 348, 1280, 1816),
              collection_info={"row_count": 2, "column_count": 1},
              actions=({"id": R.ACTION_SCROLL_FORWARD}, {"id": R.ACTION_PAGE_DOWN}),
              children=[page1, page2])
    seek = n(41, cls="android.widget.SeekBar", flags=FOCUS, b=(0, 2200, 1280, 60),
             range_info={"min": 0, "max": 100, "current": 6, "type": "PERCENT"})
    return [root(pager, seek, b=(0, 0, 1280, 2856))]


def test_an_offscreen_webview_whose_root_claims_the_screen_traps_talkback():
    # nodeFilterOrWebView (:1357) checks no visibility, so TalkBack targets the root of a WebView
    # whose page is clipped away. On AntennaPod's expanded player TalkBack 17.0 logged "perform
    # action=64=ACTION_ACCESSIBILITY_FOCUS returns true" on it, no focus event followed, and the
    # next press targeted it again: focus never left "Shownotes" (19 presses; again live).
    tree = tb.build(player_with_notes_page())
    walk = tb.simulate(tree)
    assert walk.ended == "trap"
    last = walk.steps[-1]
    assert last["stuck"] and last["key"] == "view:37" and last["web_root"] == "virtual:40:4"
    assert walk.keys()[-1] == "view:37"
    hint = next(d for d in walk.hints if d["kind"] == "web_hidden_page")
    assert hint["trap"] is True and hint["before"] == "view:37" and hint["container"] == "view:34"
    assert "returns true" in hint["message"]
    assert tb.Navigator(tree).hidden_web_pages() == [hint]


def test_an_offscreen_webview_off_screen_too_is_read_nobody_sees_it():
    # The same page with the player collapsed (AntennaPod's home, live): the root is 0px tall
    # below the screen, and TalkBack 17.0 read "Webview" and every element of it.
    tree = tb.build(player_with_notes_page(root_on_screen=False))
    walk = tb.simulate(tree)
    keys = walk.keys()
    at = keys.index("view:37")
    assert keys[at:at + 4] == ["view:37", "virtual:40:4", "virtual:40:10", "virtual:40:160"]
    assert walk.stops[at + 1]["hidden_page"] is True and walk.ended == "wrap"
    hint = next(d for d in walk.hints if d["kind"] == "web_hidden_page")
    assert hint["trap"] is False and "nobody can see" in hint["message"]
    ghost = diff.web_trap_finding(hint, basis="model")
    assert (ghost["code"], ghost["sev"]) == ("tb.ghost_stop", "warn")


def test_a_webview_on_screen_is_not_flagged():
    tree = tb.build(player_with_notes_page(hidden=False))
    walk = tb.simulate(tree)
    assert not any(s.get("hidden_page") for s in walk.stops) and walk.ended == "wrap"
    assert tb.Navigator(tree).hidden_web_pages() == []


def test_a_walk_stuck_before_a_trapping_webview_is_a_trap_not_an_edge():
    traps = tb.Navigator(tb.build(player_with_notes_page())).hidden_web_pages()
    steps = [{"i": 0, "key": "view:35", "ref": "view:35", "moved": True, "via": "start"},
             {"i": 1, "key": "view:37", "ref": "view:37", "moved": True, "via": "next"},
             {"i": 2, "key": "view:37", "ref": "view:37", "moved": False, "via": "next"},
             {"i": 3, "key": "view:37", "ref": "view:37", "moved": False, "via": "next"}]
    walk = {"steps": steps, "ended": "stuck", "predicted": [], "direction": "next"}
    found = diff.analyze(dict(walk, web_traps=traps))["findings"]
    assert "tb.edge_stuck" not in {f["code"] for f in found}
    trap = next(f for f in found if f["code"] == "tb.trap")
    assert trap["sev"] == "error" and "noHideDescendants" in trap["fix"]
    assert trap["msg"].startswith("TalkBack stopped at view:37")
    assert "tb.edge_stuck" in {f["code"] for f in diff.analyze(walk)["findings"]}


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
    # Its WebView sits on the pager's off-screen page: the model flags the risk.
    assert next(s for s in walk.stops if s["key"] == "virtual:17:4")["hidden_page"] is True


def test_v13_static_walk_reports_the_hidden_page():
    resp = pb.DumpA11yResponse()
    resp.ParseFromString(gzip.decompress((WALKS / "tb_v13-bad-walk.a11y.pb.gz").read_bytes()))
    from inspector_widget.talkback import walk as tbwalk
    static = tbwalk.static_walk(resp)
    ghost = next(f for f in static["findings"]
                 if f["code"] == "tb.ghost_stop" and "virtual:17:4" in f["keys"])
    assert ghost["basis"] == "model" and "noHideDescendants" in ghost["fix"]
    assert any("off-screen page" in h for h in static["hints"])
    assert "tb.trap" not in {f["code"] for f in static["findings"]}
    good = pb.DumpA11yResponse()
    good.ParseFromString(gzip.decompress((WALKS / "tb_v13-good-walk.a11y.pb.gz").read_bytes()))
    assert "tb.ghost_stop" not in {f["code"] for f in tbwalk.static_walk(good)["findings"]}
