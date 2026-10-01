"""The static TalkBack rules on LIVE captures of the A11yProbe TalkBack corpus.

tests/fixtures/tb_captures/ holds one real capture per corpus screen (every scenario and
variant of tb_corpus_expected.json, plus C11 again with ``capture(slots="enable")``), taken
over MCP on emulator-5556 (API 37, 2076x2152, 390 dpi, TalkBack 17.0 installed and OFF)
with ``capture(props=false)`` and recorded without screenshots: the agent's View tree,
Compose semantics, slot table (C11) and accessibility tree, as an agent's capture holds
them. test_capture_tb_rules.py runs the same rules on captures rebuilt from the recorded
walks' TalkBack-on a11y dumps; these are the real thing, TalkBack off, hidden View
containers and Compose overlays included.

Per scenario: the rule a capture should raise, whether it fires on BAD, and whether GOOD
stays silent (``-``: nothing static to find: walk or tb_scenario only, or calibrated away).

=========  ==============================  ======  ======  =================================
scenario   expected static rule(s)         BAD     GOOD    note
=========  ==============================  ======  ======  =================================
C1         double_stop, ghost_stop         yes     silent
C2         out_of_order                    yes     silent
C3         out_of_order                    yes     silent
C4         ghost_stop                      yes     silent
C5-C7      -                               -       silent  calibrated / list updates
C8         edge_stuck                      yes     silent
C9         wrong_announcement              yes     silent
C10        -                               -       silent  calibrated
C11        custom_action_missing (slots)   yes     FIRES   GOOD's customActions sit on the
                                                           SwipeToDismissBox node, which
                                                           TalkBack never focuses (see below)
C12-C14    - (trap / dialogs / restore)    -       silent  walk or tb_scenario
C15        double_stop                     yes     silent  (loop: walk only)
C16        edge_stuck                      yes     silent
V1, V2     out_of_order                    yes     silent
V3         out_of_order                    NO      silent  needs tb_walk(expect=...)
V4, V5     double+ghost / escape+ghost     yes     silent  V5: the real drawing order
V6         -                               -       silent  list updates
V7, V8     edge_stuck                      yes     silent
V9         ghost_stop                      yes     silent
V10        window_order                    yes     silent
V11        -                               -       silent  restore
V12        wrong_announcement              yes     silent  RecyclerView item info (recycler.py)
V13        edge_stuck (+ghost, ooo: web)   part    silent  web content: walk only
V14        -                               -       silent  dialog by action
H1         ghost_stop, skipped             yes     silent
H2-H4      -                               -       silent  calibrated / list updates
H5         escape, double_stop             yes     silent  a real Compose overlay
H6         -                               -       silent  calibrated
=========  ==============================  ======  ======  =================================

Precision and recall over (screen, rule) pairs, against the corpus's walk-confirmed findings
(walk-only codes tb.trap and tb.loop left out) plus C11's swipe-only delete: 26 true, 3
missed (V3 out_of_order, V13 ghost_stop and out_of_order on web content), 1 on a GOOD
screen (C11). Recall 26/29; precision 26/27 by the corpus's labels, 27/27 if C11 GOOD is
judged as TalkBack sees it: the real walk of C11 GOOD stops only on each row's Text, which
has no action, so its "Delete" is as unreachable as BAD's.
"""

from __future__ import annotations

import collections
import json

import pytest
import tb_capture_fixtures as F
from test_capture_tb_rules import TABLE

from inspector_widget.capture import analyzers
from inspector_widget.capture import tb as T
from inspector_widget.capture.model import RawCapture
from inspector_widget.proto import view_inspection_pb2 as pb

#: codes only a walk can raise: never expected of a capture
WALK_ONLY = {"tb.trap", "tb.loop"}
#: the static defects a corpus scenario has beyond its walk findings
EXTRA_TRUTH = {"tb_c11_bad_slots": {"tb.custom_action_missing"}}
#: tb.* counts on each live capture: the derived corpus table, plus C11 with slots
LIVE_TABLE = {f"{k.replace('-', '_')}": v for k, v in TABLE.items()}
#: live captures of real apps beside the corpus
REAL_APPS = {"thunderbird_list_tb_off"}
LIVE_TABLE.update({
    "thunderbird_list_tb_off": {"tb.double_stop": 6, "tb.wrong_announcement": 1},
    "tb_c11_bad_slots": {"tb.custom_action_missing": 3},
    # GOOD's fix is unreachable for TalkBack (module docstring): the evidence says so
    "tb_c11_good_slots": {"tb.custom_action_missing": 3},
})


def _tb(ix) -> dict[str, int]:
    return dict(collections.Counter(i.id for n in ix.nodes.values() for i in n.issues
                                    if i.id.startswith("tb.")))


def _entry(name: str) -> dict | None:
    for e in F.WALK_ENTRIES:
        if name in (f"{e['scenario']}_{e['variant']}", f"{e['scenario']}_{e['variant']}_slots"):
            return e
    return None


def test_every_corpus_screen_has_a_live_capture():
    want = {f"{e['scenario']}_{e['variant']}" for e in F.WALK_ENTRIES}
    names = set(F.live_names())
    assert want <= names
    assert names - want == {"tb_c11_bad_slots", "tb_c11_good_slots"} | REAL_APPS
    for name in names:  # real captures: the View tree and the a11y tree, Compose's too
        _ix, raw = F.live_capture(name)
        assert raw.views and raw.a11y
        assert raw.meta.lineage[1] == F.PACKAGE or name in REAL_APPS
        assert not raw.shots  # recorded without screenshots: kept small


@pytest.mark.parametrize("name", F.live_names())
def test_each_live_capture_raises_exactly_its_tb_rules(name):
    ix, _raw = F.live_capture(name)
    assert _tb(ix) == LIVE_TABLE.get(name, {}), name


def test_live_captures_agree_with_the_captures_derived_from_the_walks():
    """The offline corpus (captures rebuilt from TalkBack-on a11y dumps, with hand-built
    View spines for V5 and H5) raises what the live captures raise, screen by screen."""
    for e in F.WALK_ENTRIES:
        name = f"{e['scenario']}_{e['variant']}"
        derived, _ = F.corpus_capture(F.entry_id(e))
        live, _ = F.live_capture(name)
        assert _tb(live) == _tb(derived), name


def test_precision_and_recall_on_the_corpus():
    tp = fn = fp_good = fp_bad = 0
    for name in sorted(set(F.live_names()) - REAL_APPS):
        e = _entry(name)
        got = set(_tb(F.live_capture(name)[0]))
        if e["variant"] != "bad":
            fp_good += len(got)
            continue
        if name == "tb_c11_bad":
            continue  # C11 BAD without the slot table: counted once, with it
        truth = set(e.get("expected_findings") or []) - WALK_ONLY
        truth |= EXTRA_TRUTH.get(name, set())
        tp += len(truth & got)
        fn += len(truth - got)
        fp_bad += len(got - truth)
    assert (tp, fn, fp_bad, fp_good) == (26, 3, 0, 1)
    # recall 26/29; precision 26/27 by the corpus labels (C11 GOOD: see the docstring)


def test_c11_good_names_the_container_talkback_never_focuses():
    ix, raw = F.live_capture("tb_c11_good_slots")
    out = analyzers.lint_view(ix, raw, rules=["tb"], group="none")
    iss = [i for n in ix.nodes.values() for i in n.issues if i.id == "tb.custom_action_missing"]
    assert len(iss) == 3
    assert all(i.evidence["why"] == "action_on_a_node_talkback_never_focuses"
               and len(i.evidence["node_ids"]) == 1 for i in iss)
    assert all("Delete" in str(ix.nodes[i.evidence["node_ids"][0]].facets.get("a11y"))
               for i in iss)
    bad, _ = F.live_capture("tb_c11_bad_slots")
    assert all("why" not in i.evidence for n in bad.nodes.values() for i in n.issues
               if i.id == "tb.custom_action_missing")
    assert out["lines"][0].startswith('sem:7:35 "Water plants" tb.custom_action_missing warn '
                                      'SwipeToDismissBox: its custom action is on sem:7:29, '
                                      'never focused')


def test_v12_speaks_the_position_a_talkback_user_hears():
    """TalkBack off: no item info in the dump; the model adds what RecyclerView adds
    while TalkBack runs (talkback/recycler.py), so the empty header counts: "2 of 21"."""
    ix, raw = F.live_capture("tb_v12_bad")
    tbc = T.TbCapture.of(ix, raw)
    first = next(n for n in ix.nodes.values() if n.label == "Message 1")
    assert tbc.walk_speech(tbc.node(first.id)).text == "Message 1. 2 of 21. In list. 21 items"
    assert not [d for d in ix.diagnostics if d.startswith("tb: ")]


def test_a_capture_with_talkback_on_over_a_list_bound_before_says_why_positions_are_gone():
    """tb_walk turns TalkBack on over a list already drawn: its capture has the service on
    and still no item info, as the real TalkBack (V12 BAD live: "Message 1. In list. 21
    items"). The capture says so; nothing is invented."""
    _ix, raw = F.live_capture("tb_v12_bad")
    resp = pb.DumpA11yResponse.FromString(raw.a11y)
    assert "a11y-services=off" in resp.diagnostics
    resp.diagnostics = resp.diagnostics.replace("a11y-services=off", "a11y-services=on")
    on = RawCapture(meta=raw.meta, windows=raw.windows, views=raw.views,
                    compose_sem=raw.compose_sem, a11y=resp.SerializeToString())
    ix = F.build(on)
    tbc = T.TbCapture.of(ix, on)
    first = next(n for n in ix.nodes.values() if n.label == "Message 1")
    assert tbc.walk_speech(tbc.node(first.id)).text == "Message 1. In list. 21 items"
    notes = [d for d in ix.diagnostics if d.startswith("tb: ")]
    assert len(notes) == 1 and "bound before it started" in notes[0], notes
    assert "tb.wrong_announcement" not in _tb(ix)


def test_thunderbird_counts_its_empty_header_scrolled_off_the_top():
    """Thunderbird's message list with TalkBack off: no item info, and the list can scroll
    back (its empty header item sits above the top). All 7 items are children, so the
    positions are exact: TalkBack on before the app said "... Star. 2 of 7. In list. 7
    items" on the first row (live walk, emulator-5556), and so does the model."""
    ix, raw = F.live_capture("thunderbird_list_tb_off")
    tbc = T.TbCapture.of(ix, raw)
    first = next(n for n in ix.nodes.values()
                 if n.stop is not None and (n.label or "").startswith("unread, Localpart"))
    said = tbc.walk_speech(tbc.node(first.id)).text
    assert said.endswith("Star. 2 of 7. In list. 7 items"), said
    wa = [i for n in ix.nodes.values() for i in n.issues if i.id == "tb.wrong_announcement"]
    assert len(wa) == 1 and wa[0].evidence["why"] == "position_counts_silent_item"
    assert not [d for d in ix.diagnostics if d.startswith("tb: ")]


def test_the_fixtures_stay_small():
    total = sum(f.stat().st_size for f in F.LIVE.rglob("*") if f.is_file())
    assert total < 350_000, total
    meta = json.loads((F.LIVE / "tb_v5_bad" / "meta.json").read_text())
    assert meta["options"]["props"] is False and meta["lineage"]["serial"] == "emulator-5556"
