"""The static TalkBack rules of a capture (talkback/static.py, capture/tb.py): the tb.*
issues ``lint(rules=["tb"])`` reports, from the TalkBack model alone (basis "model").

Precision first: no rule may fire on a GOOD variant of the A11yProbe TalkBack corpus.
Each corpus screen is the capture of the a11y dump its recorded TalkBack 17.0 walk started
from (tests/tb_capture_fixtures.py; the View hierarchy the dump leaves out is filled in from
the scenario source where an overlay's drawing order needs it: V5, H5).

Per-scenario expectations (BAD: what fires, GOOD: nothing):

=========  ===============================================  =================================
scenario   tb rules on the BAD capture                      note
=========  ===============================================  =================================
C1         double_stop x3, ghost_stop x3                    card + its own Checkbox, unlabelled
C2         out_of_order x2                                  two columns read zig-zag
C3         out_of_order                                     traversalIndex puts the title last
C4         ghost_stop                                       the unlabelled scrim (Compose drops
                                                            what it covers: no escape on 17.0)
C8         edge_stuck                                       a pager TalkBack never pages
C9         wrong_announcement x2                            "$5. Socks": composition order
C15        double_stop x6                                   a card and its bookmark button
                                                            (the loop is walk-only)
C16        edge_stuck                                       the pager again (Compose keeps the
                                                            offscreen WebView page out)
V1         out_of_order                                     a traversal link to a duplicated id
V2         out_of_order                                     a traversalAfter cycle
V4         double_stop, ghost_stop                          a row with a cd + its own Switch
V5         escape, ghost_stop                               a same-window scrim and card
V7         edge_stuck                                       rows past an edge nothing scrolls
V8         edge_stuck                                       a scroller without scroll actions
V9         ghost_stop x2                                    unlabelled ImageButton, silent row
V10        window_order                                     a non-focusable popup read last
V12        wrong_announcement                               an empty header counted: "2 of 21"
V13        edge_stuck                                       ViewPager2 (the web stops are walk-
                                                            only: web content is not modelled)
H1         ghost_stop, skipped x14                          noHideDescendants on every cell
H5         escape, double_stop                              a Compose "dialog" over Views
=========  ===============================================  =================================

Silent on purpose (BAD and GOOD alike): C5, C7, C10, C11, H3, H4, H6 (calibration: TalkBack
17 does not show the predicted defect); C6, V6, H2 (list updates), C12 (a focus trap), C13,
V14 (dialogs opened by an action), C14, V11 (restore): walk or tb_scenario only. V3 BAD needs
the author's intended order (tb_walk(expect=...)): the model alone cannot know it. C11's
swipe-to-dismiss rows need the slot table (capture(slots="enable")): see
test_custom_action_missing_needs_the_slot_table.
"""

from __future__ import annotations

import collections

import pytest
import tb_capture_fixtures as F

from inspector_widget import a11y
from inspector_widget.capture import analyzers, rules as R, tb as T
from inspector_widget.capture.index import apply_refs
from inspector_widget.capture.lines import issue_codes
from inspector_widget.capture.model import UNode
from inspector_widget.talkback import Navigator, build, static

#: tb.* issue counts on each BAD capture; every other corpus capture has none.
TABLE = {
    "tb_c1-bad": {"tb.double_stop": 3, "tb.ghost_stop": 3},
    "tb_c2-bad": {"tb.out_of_order": 2},
    "tb_c3-bad": {"tb.out_of_order": 1},
    "tb_c4-bad": {"tb.ghost_stop": 1},
    "tb_c8-bad": {"tb.edge_stuck": 1},
    "tb_c9-bad": {"tb.wrong_announcement": 2},
    "tb_c15-bad": {"tb.double_stop": 6},
    "tb_c16-bad": {"tb.edge_stuck": 1},
    "tb_v1-bad": {"tb.out_of_order": 1},
    "tb_v2-bad": {"tb.out_of_order": 1},
    "tb_v4-bad": {"tb.double_stop": 1, "tb.ghost_stop": 1},
    "tb_v5-bad": {"tb.escape": 1, "tb.ghost_stop": 1},
    "tb_v7-bad": {"tb.edge_stuck": 1},
    "tb_v8-bad": {"tb.edge_stuck": 1},
    "tb_v9-bad": {"tb.ghost_stop": 2},
    "tb_v10-bad": {"tb.window_order": 1},
    "tb_v12-bad": {"tb.wrong_announcement": 1},
    "tb_v13-bad": {"tb.edge_stuck": 1},
    "tb_h1-bad": {"tb.ghost_stop": 1, "tb.skipped": 14},
    "tb_h5-bad": {"tb.double_stop": 1, "tb.escape": 1},
}
#: corpus static expectations (tb_corpus_expected.json static_findings) the capture rules
#: cannot meet, and why
STATIC_EXCEPTIONS = {
    "tb_v3-bad-walk": "the author's intended order (expect) is not in the capture",
}
#: the tb.* issues on the recorded real captures: unlabelled controls (a11y.label.missing
#: says so too), rows or cards with their own inline controls, and Thunderbird's counted
#: empty header (the one default rule among them, confirmed by a real walk)
REAL = {
    "a11yprobe_all": {"tb.ghost_stop": 1},
    "a11yprobe_d1": {"tb.ghost_stop": 2},
    "a11yprobe_launcher": {},
    "a11yprobe_launcher_slots": {},
    "a11yprobe_s1_a": {"tb.double_stop": 13, "tb.ghost_stop": 1},
    "a11yprobe_s1_b": {"tb.double_stop": 13},
    "a11yprobe_view_defaults": {"tb.ghost_stop": 10},
    "a11yprobe_viewscreen": {"tb.ghost_stop": 3},
    "nia_foryou": {"tb.double_stop": 3},
    "nia_settings": {},
    "thunderbird_list_compose": {"tb.double_stop": 6, "tb.ghost_stop": 6},
    # its empty header item counts: TalkBack (on before the app) says "2 of 7" on the first
    # row, live on emulator-5556; the item is scrolled off the top (talkback/recycler.py)
    "thunderbird_list_views": {"tb.double_stop": 6, "tb.wrong_announcement": 1},
}


def _tb(ix) -> dict[str, int]:
    return dict(collections.Counter(i.id for n in ix.nodes.values() for i in n.issues
                                    if i.id.startswith("tb.")))


@pytest.mark.parametrize("entry", F.WALK_ENTRIES, ids=F.entry_id)
def test_each_corpus_screen_raises_exactly_its_tb_rules(entry):
    eid = F.entry_id(entry)
    ix, _raw = F.corpus_capture(eid)
    got = _tb(ix)
    assert got == TABLE.get(eid[: -len("-walk")], {}), (eid, got)
    if entry["variant"] == "good":
        assert got == {}  # precision: a GOOD variant never raises a TalkBack finding


@pytest.mark.parametrize("entry", [e for e in F.WALK_ENTRIES if e.get("static_findings")],
                         ids=F.entry_id)
def test_the_capture_sees_what_the_corpus_says_the_model_sees(entry):
    eid = F.entry_id(entry)
    ix, _raw = F.corpus_capture(eid)
    missing = set(entry["static_findings"]) - set(_tb(ix))
    if eid in STATIC_EXCEPTIONS:
        assert missing, f"{eid} now passes: drop it from STATIC_EXCEPTIONS"
        return
    assert not missing, (eid, missing)


@pytest.mark.parametrize("name", sorted(REAL))
def test_real_captures_raise_only_the_known_tb_findings(name):
    ix, raw = F.fixture_capture(name)
    assert _tb(ix) == REAL[name]
    # the default lint shows only the precise rules: Thunderbird's "2 of 7", nothing else
    out = analyzers.lint_view(ix, raw, max_bytes=0)
    assert {r["rule"] for r in out["rules"] if r["rule"].startswith("tb.")} == \
        set(REAL[name]) & set(R.DEFAULT_TB)


def test_the_default_lint_reports_the_precise_tb_rules():
    assert R.DEFAULT_TB == ("tb.escape", "tb.window_order", "tb.wrong_announcement",
                            "tb.edge_stuck", "tb.skipped")
    ix, raw = F.corpus_capture("tb_v5-bad-walk")
    rules = {r["rule"]: r for r in analyzers.lint_view(ix, raw)["rules"]}
    assert "tb.escape" in rules and "tb.ghost_stop" not in rules
    esc = rules["tb.escape"]
    assert esc["sev"] == "error" and esc["fix"].startswith("A real Dialog")
    assert esc["nodes"] == ["view:12 View 9 stops under it (90% of the window), e.g. view:3 view:4"]
    assert analyzers.lint_summary(ix)["lint"] == (
        "2 error 10 warn: 10 touch_target, 1 label_missing, 1 escape (contrast not run)")


def test_lint_rules_tb_lists_every_tb_rule_with_template_collapse():
    ix, raw = F.fixture_capture("thunderbird_list_compose")
    out = analyzers.lint_view(ix, raw, rules=["tb"])
    by = {r["rule"]: r for r in out["rules"]}
    assert set(by) == {"tb.double_stop", "tb.ghost_stop"}
    assert by["tb.double_stop"]["nodes"] == [
        "×6 in #message_list cells: sem:785:838 sem:795:861 sem:805:886 +3"]
    assert by["tb.ghost_stop"]["nodes"] == [
        "×6 in #message_list cells (@MessageItem_FavouriteButtonIcon): sem:785:855 "
        "sem:795:880 sem:805:903 +3"]
    assert out["next"][-1] == 'node("sem:785:838",facets="tb,issues")'
    assert R.resolve(["tb"]) == [r.id for r in R.RULES.values() if r.family == "tb"]
    assert R.resolve("double_stop") == ["tb.double_stop"]


def test_finding_details_name_the_other_nodes():
    ix, raw = F.corpus_capture("tb_c9-bad-walk")
    out = analyzers.lint_view(ix, raw, rules=["tb"], group="none")
    assert out["lines"][0] == ('a11y:7:5 "$5, Socks" tb.wrong_announcement warn says '
                               '"$5. Socks", shown "Socks … $5"')
    ix, raw = F.corpus_capture("tb_v12-bad-walk")
    line = analyzers.lint_view(ix, raw, rules=["tb"], group="none")["lines"][0]
    # the wrong "N of M" itself (it ends the speech, past what a cut quote keeps) and the
    # silent item it counts
    assert line == ('view:13 "Message 1" tb.wrong_announcement warn says "2 of 21": counts '
                    '1 silent item(s), e.g. view:12')
    ix, raw = F.corpus_capture("tb_c2-bad-walk")
    lines = analyzers.lint_view(ix, raw, rules=["tb"], group="none")["lines"]
    assert lines[0] == ('a11y:7:17 "Product B1" tb.out_of_order warn read 3 after a11y:7:7, '
                        'seen 5 of 7')


def test_findings_name_refs_once_refs_are_applied():
    ix, _raw = F.corpus_capture("tb_h5-bad-walk")
    refmap = {n.key: f"n{i + 1}" for i, n in enumerate(ix.nodes.values())}
    rx = apply_refs(ix, refmap)
    esc = [(n.id, i) for n in rx.nodes.values() for i in n.issues if i.id == "tb.escape"]
    assert len(esc) == 1
    nid, iss = esc[0]
    assert nid.startswith("n") and all(x.startswith("n") for x in iss.evidence["node_ids"])
    assert iss.evidence["under"] == 9


def test_escape_needs_to_know_what_is_drawn_above_what():
    """Without the View hierarchy the scrim's place among its siblings is unknown: no
    escape, and no reading-order finding across what may be two layers."""
    _rec, resp = F.load_walk("tb_v5-bad-walk")
    ix = F.build(F.raw_from_a11y(resp))  # the dump's own nesting only
    assert _tb(ix) == {"tb.ghost_stop": 1}
    nav = Navigator(build(a11y.a11y_to_dict(resp)))
    assert [f.code for f in static.findings(nav)] == ["tb.ghost_stop"]


def test_tree_views_show_the_default_tb_codes_and_the_reading_view_all():
    ix, _raw = F.corpus_capture("tb_v5-bad-walk")
    scrim = ix.nodes["view:12"]
    assert {i.id for i in scrim.issues} >= {"tb.escape", "tb.ghost_stop"}
    assert "escape" in issue_codes(scrim) and "ghost_stop" not in issue_codes(scrim)
    assert {"escape", "ghost_stop"} <= set(issue_codes(scrim, all_tb=True))


def test_custom_action_missing_needs_the_slot_table():
    """C11: swipe-to-dismiss rows. The a11y tree cannot tell a swipeable row from a plain
    one; the slot table can (the SwipeToDismissBox composable around the row). Here the slot
    links a capture(slots="enable") would make are added by hand."""
    def with_slots(eid, rows):
        _ix, raw = F.corpus_capture(eid)
        ix = F.build(raw)  # a fresh copy to add slots to
        for i, row in enumerate(rows):
            sid = f"slot:7:{i:08x}"
            ix.nodes[sid] = UNode(key=sid, kind="slot", type="SwipeToDismissBox",
                                  src=f"TalkBackScenarios.kt:{300 + i}",
                                  facets={"slot": {"name": "SwipeToDismissBox"}})
            ix.nodes[row].facets.setdefault("compose", {})["slots"] = [sid]
        return T.issues(ix, raw, tbc=T.TbCapture(F.load_walk(eid)[1], ix))[0]

    # BAD: each row's stop is its text (the row itself is not focusable)
    bad = with_slots("tb_c11-bad-walk", ["a11y:7:11", "a11y:7:18", "a11y:7:25"])
    assert [(n, i.id, i.evidence["gesture"], i.conf) for n, i in bad
            if i.id == "tb.custom_action_missing"] == [
        ("a11y:7:11", "tb.custom_action_missing", "SwipeToDismissBox", "inferred"),
        ("a11y:7:18", "tb.custom_action_missing", "SwipeToDismissBox", "inferred"),
        ("a11y:7:25", "tb.custom_action_missing", "SwipeToDismissBox", "inferred")]
    # GOOD puts customActions on the SwipeToDismissBox node (a11y:7:5 ...), which TalkBack
    # never focuses: the stop is still the Text, whose menu has no "Delete" (the live
    # capture with the real slot table says the same: test_capture_tb_corpus_live.py)
    good = with_slots("tb_c11-good-walk", ["a11y:7:11", "a11y:7:18", "a11y:7:25"])
    assert [(n, i.evidence["node_ids"]) for n, i in good
            if i.id == "tb.custom_action_missing"] == [
        ("a11y:7:11", ["a11y:7:5"]), ("a11y:7:18", ["a11y:7:12"]), ("a11y:7:25", ["a11y:7:19"])]


def test_a_capture_never_fails_on_the_tb_rules(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("model")

    monkeypatch.setattr(T, "issues", boom)
    _rec, resp = F.load_walk("tb_c1-bad-walk")
    ix = F.build(F.raw_from_a11y(resp))
    assert _tb(ix) == {} and ix.reading
    assert "tb: not run (RuntimeError: model)" in ix.diagnostics


def test_a_sliver_talkback_scrolls_in_first_is_no_ghost_not_even_tiny():
    # V12 GOOD with its last attached row scrolled to a 6 px sliver at the list's bottom
    # edge: TalkBack scrolls it fully into view before speaking it (ensureOnScreen)
    from inspector_widget.capture.model import RawCapture
    from inspector_widget.proto import view_inspection_pb2 as pb

    _ix, raw = F.live_capture("tb_v12_good")
    resp = pb.DumpA11yResponse.FromString(raw.a11y)

    def walk(x):
        yield x
        for k in x.children:
            yield from walk(k)

    last = next(x for w in resp.windows if w.HasField("root") for x in walk(w.root)
                if x.bounds.layout.y == 2037 and x.bounds.layout.h == 37)
    last.bounds.layout.y, last.bounds.layout.h = 2068, 6
    mod = RawCapture(meta=raw.meta, windows=raw.windows, views=raw.views,
                     compose_sem=raw.compose_sem, a11y=resp.SerializeToString())
    ix = F.build(mod)
    tbc = T.TbCapture.of(ix, mod)
    sliver = next(x for x in tbc.linear() if x.rect.height == 6)
    assert static.show_on_screen(tbc.nav, sliver) is not None
    assert static.ghost(tbc.nav, sliver, 390) == [] and tbc.ghost(sliver) == []
    assert not [n for n in ix.nodes.values() for i in n.issues if i.id == "tb.ghost_stop"]


def test_elevation_decides_what_is_drawn_above_before_child_order():
    # Thunderbird's message list (live): CoordinatorLayout[FloatingActionButton (elevation
    # 18 px), SwipeRefreshLayout (the list)]. The list is the later child, but a ViewGroup
    # draws by Z first (buildOrderedChildList): the FAB is over the list.
    ix, raw = F.fixture_capture("tests/fixtures/captures/thunderbird_list_views")

    def dump_node(ref):
        return {"host_view_id": ix.nodes[ref].ids["view"]}

    fab, row = dump_node("view:222"), dump_node("view:309")
    exact = T.drawn_above(ix, T.props_of(raw))
    assert exact(fab, row) is True and exact(row, fab) is False
    # no properties: child order, except that a View apps commonly elevate drawn earlier
    # may be over the later one
    guess = T.drawn_above(ix)
    assert guess(fab, row) is None and guess(row, fab) is None
    # equal Z: child order (the RelativeLayout content under the drawer's ComposeView)
    drawer, content = dump_node("view:218"), dump_node("view:207")
    assert exact(drawer, content) is True and guess(drawer, content) is True


def test_the_hints_lead_where_opt_in_tb_codes_are_listed():
    # C2 BAD (live): the reading view shows !out_of_order, which lint() does not list
    from inspector_widget.capture import query as q

    ix, raw = F.live_capture("tb_c2_bad")
    out = q.outline(ix, view="reading", loaded=raw)
    assert any("!out_of_order" in x for x in out["lines"])
    assert out["next"][0] == 'lint(rules=["tb"])'
    # a node found for a TalkBack rule shows that rule's code
    found = q.find(ix, issue="tb.out_of_order")["lines"]
    assert found and all("!out_of_order" in x for x in found)
    assert not any("!out_of_order" in x for x in q.find(ix, flags="click")["lines"])


def test_an_empty_tb_lint_points_at_a_walk():
    # C12 BAD (live): a focus trap, which only a walk shows
    ix, raw = F.live_capture("tb_c12_bad")
    out = analyzers.lint_view(ix, raw, rules=["tb"])
    assert out["rules"] == [] and "walk" in out["note"]
    assert out["next"] == ['outline(view="reading",explain=true)', "tb_walk()"]
    # the default lint says nothing of the kind
    assert "note" not in analyzers.lint_view(ix, raw)
