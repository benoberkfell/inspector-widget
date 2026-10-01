"""The TalkBack corpus, offline: recorded walks replayed through the model and the diff.

tests/data/tb_walks/ holds one recording per entry of tb_corpus_expected.json:
the full walk record TalkBack 17.0 produced on emulator-5556
(<scenario>-<variant>-walk.json.gz) and the a11y dump it started from
(.a11y.pb.gz). Refresh them with test_device_talkback.py and
INSPECTOR_WIDGET_TB_RECORD. Without a device this checks that:

* diff.analyze over the recorded walk still raises the entry's expected findings
  (and nothing for a clean GOOD variant) and holds its expected speech: the
  classifiers match real walks;
* walk.static_walk over the recorded dump raises the entry's static findings
  (nothing for a GOOD variant): the model alone sees the defect;
* the model's predicted order agrees with what TalkBack did, apart from the
  pinned calibration deltas (model.mismatch);
* every scenario the app defines has an expectation, and every entry is recorded.
"""

from __future__ import annotations

import gzip
import json
import re
from pathlib import Path

import pytest

from inspector_widget import a11y, a11y_lint
from inspector_widget.proto import view_inspection_pb2 as pb
from inspector_widget.talkback import diff, walk

DATA = Path(__file__).parent / "data"
WALKS = DATA / "tb_walks"
EXPECTED = json.loads((DATA / "tb_corpus_expected.json").read_text())
APP = Path(__file__).resolve().parents[2] / "testapps" / "a11yprobe" / "app" / "src" / "main" / "kotlin" \
    / "com" / "oberkfell" / "a11yprobe"
WALK_ENTRIES = [e for e in EXPECTED["entries"] if e.get("kind", "walk") == "walk"]


def _id(e):
    return f"{e['scenario']}-{e['variant']}-{e.get('kind', 'walk')}"


def _load(e):
    base = WALKS / _id(e)
    rec = json.loads(gzip.decompress((base.with_name(base.name + ".json.gz")).read_bytes()))
    resp = pb.DumpA11yResponse()
    resp.ParseFromString(gzip.decompress(base.with_name(base.name + ".a11y.pb.gz").read_bytes()))
    return rec, resp


def _codes(findings):
    return {f["code"] for f in findings}


def _expect(entry):
    return (entry.get("walk") or {}).get("expect")


def test_every_app_scenario_and_variant_has_an_expectation():
    defined = set()
    for f in ("TalkBackScenarios.kt", "TbViewActivity.kt", "TbHybrid.kt"):
        src = (APP / f).read_text()
        for m in re.finditer(r'"(tb_[chv]\d+)"(?:, "[^"]*", "[^"]*"(?:, listOf\(([^)]*)\))?)?', src):
            variants = re.findall(r'"(\w+)"', m.group(2)) if m.group(2) else ["bad", "good"]
            for v in variants:
                defined.add((m.group(1), v))
    covered = {(e["scenario"], e["variant"]) for e in EXPECTED["entries"]}
    assert defined - covered == set(), sorted(defined - covered)


@pytest.mark.parametrize("entry", WALK_ENTRIES, ids=_id)
def test_recorded_walk_still_classifies_the_same(entry):
    rec, _resp = _load(entry)
    found = _codes(diff.analyze(rec, expect=_expect(entry))["findings"])
    want = set(entry.get("expected_findings") or [])
    assert want <= found, (want, found)
    if entry["variant"] == "good" and not want:
        assert found - {"model.mismatch"} == set(), found
    if (entry.get("walk_expect") or {}).get("ended"):
        assert rec["ended"] == entry["walk_expect"]["ended"]
    said = " | ".join(s.get("speak") or "" for s in rec["steps"] if s.get("moved"))
    for words in entry.get("expected_speech") or []:
        assert words in said, (words, said)


@pytest.mark.parametrize("entry", [e for e in WALK_ENTRIES if e.get("static_findings") is not None],
                         ids=_id)
def test_the_model_alone_sees_the_defect(entry):
    # Exactly the pinned codes, BAD variants included: a model change that adds or drops a
    # finding shows here (tb_v13-bad's tb.out_of_order once went unnoticed under a subset check).
    _rec, resp = _load(entry)
    found = _codes(walk.static_walk(resp, expect=_expect(entry))["findings"])
    assert found == set(entry["static_findings"]), (entry["static_findings"], found)


def _first_screen(steps):
    """The steps before the screen changed (a scroll, another window, a stolen
    focus): what the model predicts from the dump the walk started from."""
    out = []
    for s in steps:
        if out and s.get("via") in ("autoscroll", "window", "stolen", "lost"):
            break
        out.append(s)
    return out


@pytest.mark.parametrize("entry", WALK_ENTRIES, ids=_id)
def test_model_agrees_with_talkback_or_the_delta_is_pinned(entry):
    """The model, re-run on the start dump, against the first screen of the walk."""
    rec, resp = _load(entry)
    stops, _source, _meta = walk.predict(resp, bool(rec.get("legacy_ids")))
    rec = dict(rec, steps=_first_screen(rec["steps"]), orphans=[],
               predicted=[{"key": p.key, "ref": p.key, "label": p.label, "speak": p.speak,
                           "bounds": list(p.bounds), "window": p.window, "cls": p.cls}
                          for p in stops])
    vs = diff.analyze(rec, expect=_expect(entry))["vs_model"]
    pinned = entry.get("model_mismatch") or {}
    assert vs.get("differ", 0) <= pinned.get("differ", 0), (vs, pinned)


@pytest.mark.parametrize("entry", WALK_ENTRIES, ids=_id)
def test_a_stop_talkback_called_unlabelled_is_an_r1_error(entry):
    """Every stop of the start screen that TalkBack 17 announced as "Unlabelled" is an R1
    error, not the clipped-maybe info (tb_c4's full-window scrim reaches the window's
    bottom edge only because it spans the window)."""
    rec, resp = _load(entry)
    rep = a11y_lint.lint_unified(a11y.a11y_to_dict(resp), a11y_lint.LintContext(density=420))
    r1 = {f.node_key: f.severity for f in rep.findings if f.rule == "a11y.label.missing"}
    unlabelled = [s["key"] for s in _first_screen(rec["steps"])
                  if s.get("key") and "Unlabelled" in (s.get("speak") or "")]
    assert {k: r1.get(k) for k in unlabelled} == {k: "error" for k in unlabelled}
