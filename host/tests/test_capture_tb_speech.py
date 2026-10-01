"""What a capture says TalkBack speaks: the calibrated model (capture/tb.py), checked
against what TalkBack 17.0 really said.

The capture index speaks every TalkBack stop with the TalkBack model's announcement
(``a11y.speakable``, ``speak_src: "tb"``; the label is the name inside it), not with the
index's own rule (RO1: contentDescription > text > stateDescription, else the non-focusable
descendants), which lost the state, the role and, when a merged row has a state
description, the row's name ("Selected" for the Now in Android radio row TalkBack reads as
"Selected. Default. Radio button").

Ground truth: the recorded corpus walks (tests/data/tb_walks: TalkBack 17.0's logged
ttsOutput, press by press, ``utt: "logcat"``). On the screen each walk started from:

* the capture's speakable is the utterance, apart from the collection / container /
  window transitions TalkBack appends on arrival ("1 of 20. In list. 20 items"):
  RO1 matched 348 of 594 utterances (58.6%), the model matches 592 (99.7%; 588 before
  TalkBack 17's web roles reached the model);
* the model's announcement along the walk's path (the reading view's text, transitions
  included) equals the utterance character for character in 593 of 594 (99.8%; 584
  before its pager and web wording).

The few that differ are pinned below with the reason.
"""

from __future__ import annotations

import re

import pytest
import tb_capture_fixtures as F

from inspector_widget.capture import tb as ctb
from inspector_widget.capture.model import a11y_key
from inspector_widget.talkback.speech import SpeechState, announce

#: Parts TalkBack appends on arrival, not part of a node's own announcement.
TRANSITION = re.compile(r"^(\d+ of \d+|(In|Out of) .+|\d+ items?|Row \d+|Column \d+|"
                        r"\d+ (rows?|columns?)|Window .+)$")

#: (entry, step) whose recorded utterance the capture's own speakable does not give, and why.
OWN_EXCEPTIONS = {
    ("tb_c12-bad-walk", 6): "focus stolen and the screen recomposed: the key names another "
                            "node in the start dump",
    ("tb_v9-bad-walk", 4): "TalkBack 17 focuses the silent container (says nothing); the "
                           "model (16.2) skips it: a pinned model delta",
}
#: (entry, step) whose utterance the in-walk announcement does not give exactly. The pager
#: transitions ("In horizontal pager", "Out of grid pager") and the web roles ("heading 2",
#: "link") the model once missed are TalkBack 17's words since the a11y-accuracy merge.
WALK_EXCEPTIONS = {
    ("tb_c12-bad-walk", 6): OWN_EXCEPTIONS[("tb_c12-bad-walk", 6)],
}


def _first_screen(steps):
    """The steps before the screen changed (scroll, another window, a stolen focus)."""
    out = []
    for s in steps:
        if out and s.get("via") in ("autoscroll", "window", "stolen", "lost"):
            break
        out.append(s)
    return out


def _node_for(ix, key):
    kind, _, rest = key.partition(":")
    host, _, virt = rest.partition(":")
    try:
        h, v = int(host), (int(virt) if virt else -1)
    except ValueError:
        return None
    return ix.get(a11y_key(h, v)) or ix.get(f"view:{h}" if v == -1 else f"sem:{h}:{v}")


def own_part_matches(speak, utterance):
    """The utterance is ``speak`` plus only arrival transitions."""
    if not speak:
        return False
    if utterance == speak:
        return True
    if not utterance.startswith(speak + ". "):
        return False
    return all(TRANSITION.match(p) for p in utterance[len(speak) + 2:].split(". "))


def _recorded(build):
    """(entry id, step, the capture's speakable, the utterance) for every logged utterance
    on the start screen of every corpus walk; ``build(eid)`` gives the index."""
    for e in F.WALK_ENTRIES:
        eid = F.entry_id(e)
        rec, _resp = F.load_walk(eid)
        ix = build(eid)
        for s in _first_screen(rec["steps"]):
            if s.get("utt") != "logcat" or not s.get("moved") or not s.get("key"):
                continue
            n = _node_for(ix, s["key"])
            if n is None:
                continue
            yield eid, s["i"], (n.facets.get("a11y") or {}).get("speakable"), s["speak"]


def test_the_capture_speaks_every_stop_as_talkback_does():
    total = matched = 0
    misses = {}
    for eid, i, speak, said in _recorded(lambda eid: F.corpus_capture(eid)[0]):
        total += 1
        if own_part_matches(speak, said):
            matched += 1
        else:
            misses[(eid, i)] = (speak, said)
    assert total == 594
    assert set(misses) == set(OWN_EXCEPTIONS), {k: misses.get(k) for k in set(misses) ^
                                                set(OWN_EXCEPTIONS)}
    assert matched == 592, matched


def test_the_index_rule_alone_missed_four_in_ten(monkeypatch):
    """The baseline: the index's own rule (RO1), which the model replaced."""
    def broken(_msg):
        raise RuntimeError("model off")

    monkeypatch.setattr(ctb, "stop_speech", broken)
    matched = total = 0
    for eid, _i, speak, said in _recorded(
            lambda eid: F.build(F.raw_from_a11y(F.load_walk(eid)[1]))):
        total += 1
        matched += own_part_matches(speak, said)
    assert total == 594 and matched == 348  # 58.6%


def test_the_reading_walk_speech_is_talkbacks_word_for_word():
    """The model's announcement along each recorded path (what the reading view prints,
    transitions included) against the utterance."""
    total = exact = 0
    misses = {}
    for e in F.WALK_ENTRIES:
        eid = F.entry_id(e)
        ix, raw = F.corpus_capture(eid)
        tbc = ctb.TbCapture.of(ix, raw)
        rec, _ = F.load_walk(eid)
        st = SpeechState()
        for s in _first_screen(rec["steps"]):
            if not s.get("key") or not s.get("moved"):
                continue
            n = tbc.by_key(s["key"])
            if n is None:
                continue
            said = announce(tbc.nav, n, st).text
            if s.get("utt") == "logcat":
                total += 1
                if said == s["speak"]:
                    exact += 1
                else:
                    misses[(eid, s["i"])] = (said, s["speak"])
    assert set(misses) == set(WALK_EXCEPTIONS), {k: misses.get(k) for k in set(misses) ^
                                                 set(WALK_EXCEPTIONS)}
    assert total == 594 and exact == 593


def test_now_in_android_settings_radio_rows():
    ix, _raw = F.fixture_capture("nia_settings")
    rows = [ix.nodes[r] for r in ix.reading if ix.nodes[r].type == "RadioButton"]
    said = [(n.label, n.facets["a11y"]["speakable"], n.facets["a11y"]["speak_src"])
            for n in rows]
    assert said[:2] == [("Default", "Selected. Default. Radio button", "tb"),
                        ("Android", "Not selected. Android. Radio button", "tb")]
    assert len(rows) == 7 and all(s == "tb" for _l, _s, s in said)


@pytest.mark.parametrize("name", ["a11yprobe_launcher", "a11yprobe_viewscreen", "a11yprobe_d1",
                                  "nia_settings", "nia_foryou", "thunderbird_list_views",
                                  "thunderbird_list_compose"])
def test_every_stop_of_a_real_capture_is_spoken_by_the_model(name):
    ix, _raw = F.fixture_capture(name)
    assert ix.reading
    for r in ix.reading:
        a = ix.nodes[r].facets.get("a11y") or {}
        assert a.get("speak_src") == "tb" and a.get("speakable"), (r, a)
    assert not any(d.startswith("speech:") for d in ix.diagnostics)


def test_without_the_model_the_index_speaks_by_its_own_rule_and_says_so(monkeypatch):
    def broken(_msg):
        raise RuntimeError("boom")

    monkeypatch.setattr(ctb, "stop_speech", broken)
    _ix, raw = F.fixture_capture("nia_settings")
    ix = F.build(raw)
    row = ix.get("sem:80:191")
    assert row.label == "Selected" and row.facets["a11y"]["speakable"] == "Selected"
    assert row.facets["a11y"]["speak_src"] == "ro1"
    assert any(d.startswith("speech: TalkBack model failed (RuntimeError: boom)")
               for d in ix.diagnostics)


def test_a_state_change_is_reported_once_not_again_as_speech():
    """The speakable says the state now ("ON. Notifications. Switch"): a capture diff that
    reports the state flip does not report the speakable again."""
    from inspector_widget.capture import diff as cdiff

    _ix, raw = F.fixture_capture("nia_settings")
    a = F.build(raw)
    b = F.build(raw)
    x, y = a.get("sem:80:191"), b.get("sem:80:191")
    y.flags = [f for f in y.flags if f not in ("checked", "selected")]
    y.state = "Not selected"
    y.facets["a11y"]["speakable"] = "Not selected. Default. Radio button"
    x.state = "Selected"
    out = cdiff.diff(a, b, max_bytes=0)
    lines = [ln for ln in out["lines"] if "sem:80:191" in ln]
    assert lines and not any("speakable" in ln for ln in lines), lines
