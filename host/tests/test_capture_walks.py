"""capture/walks.py, device-free: the stored TalkBack walks and scenarios, their
classification by ref, and the compact tool results within their budgets
(talkback-navigation.md part 4 B: a walk at most 5 KB at 60 steps, a scenario at
most 1 KB). The device path (tb_walk / tb_scenario through both surfaces against
the fake TalkBack) is tests/test_tb_surface.py.
"""

from __future__ import annotations

import json
import os
import re

import pytest

from inspector_widget.capture import walks as W
from inspector_widget.capture.model import OpError
from inspector_widget.output import dumps, utf8_len


class _Store:
    def __init__(self, root):
        self.root = str(root)


def _size(doc) -> int:
    return utf8_len(dumps(doc))


def _step(i, ref, speak="Item", via="next", **kw):
    return {"i": i, "key": f"view:{1000 + i}", "ref": ref, "via": via, "moved": True,
            "label": speak, "speak": speak, "cls": "Button", "bounds": [0, i * 80, 360, 64],
            "window": 1, **kw}


def _record(n_steps=9, findings=(), ended="wrap", **kw):
    steps = [_step(0, "n3", "Title", via="start")]
    steps += [_step(i, f"n{3 + i}", f"Item {i}, Button") for i in range(1, n_steps)]
    rec = {"id": "w3f9ak1", "serial": "emulator-5554", "package": "com.example",
           "talkback": "17.0.0 uinput/enhanced", "direction": "next", "until": "wrap",
           "ended": ended, "steps": steps, "predicted": [{"key": s["key"], "ref": s["ref"]}
                                                         for s in steps],
           "findings": list(findings), "vs_model": {"agree": n_steps - 1, "differ": 0},
           "ms": {"p50": 210, "p95": 470, "total": 4300}, "restore": "restored",
           "captures": ["c7h2kq"]}
    rec.update(kw)
    return rec


FIX = "Group each column: Modifier.semantics { isTraversalGroup = true } " * 3


# --------------------------------------------------------------------------- #
# classify and the walk result
# --------------------------------------------------------------------------- #
def test_classify_sorts_findings_by_class_and_ref():
    rec = _record(findings=[
        {"code": "tb.skipped", "sev": "warn", "refs": ["n31", "n32"], "steps": []},
        {"code": "tb.double_stop", "sev": "warn", "refs": ["n20", "n21"], "steps": [4, 5]},
        {"code": "tb.escape", "sev": "error", "refs": ["n9"], "steps": [6]},
        {"code": "tb.loop", "sev": "error", "refs": ["n4", "n5"], "steps": [2]},
        {"code": "model.mismatch", "sev": "info", "refs": ["n99"], "steps": [3]},
    ], vs_model={"agree": 6, "differ": 1, "first": "step 3: model n5, actual n6",
                 "unvisited": ["n31"]})
    rec["steps"].append({"i": 9, "key": None, "via": "left_app", "moved": False,
                         "top": "com.android.launcher/.Home"})
    d = W.classify(rec)
    assert d == {"ended": "wrap", "model": "6 agree, 1 differ: step 3: model n5, actual n6",
                 "unvisited": ["n31"], "skip": ["n31", "n32"], "double": ["n20", "n21"],
                 "escape": ["n9"], "loop": ["n4", "n5"],
                 "left_app": "com.android.launcher/.Home"}


@pytest.mark.parametrize("step,line", [
    ({"i": 0, "key": None, "via": "start"}, "0. (no accessibility focus)"),
    ({"i": 8, "key": "view:1", "edge": True, "moved": False}, "8. — edge"),
    ({"i": 4, "key": None, "via": "left_app", "top": "x/.Y"}, "4. — left the app (top: x/.Y)"),
    ({"i": 5, "key": None, "via": "lost", "scrolled": "n10"}, "5. — focus lost after n10 scrolled"),
    (_step(7, "n30", "Socks, $5", via="autoscroll", scrolled="n10"),
     '7. n30 "Socks, $5" via=autoscroll(n10)'),
    (_step(9, "n3", "Title", via="wrap"), '9. n3 "Title" via=wrap'),
    (dict(_step(3, "view:1003", "Gone"), unbound=True), '3. ?view:1003 Button "Gone"'),
    (dict(_step(2, "n5", "OK"), tags=["double_stop"]), '2. n5 "OK" !double_stop'),
])
def test_step_lines(step, line):
    assert W.step_line(step) == line


def test_walk_result_names_refs_and_gives_each_fix_once():
    findings = [{"code": "tb.out_of_order", "sev": "warn", "refs": [f"n{i}"], "basis": "walk",
                 "msg": f"stop n{i} is out of order", "fix": FIX, "steps": [i - 3],
                 "keys": [f"view:{i}"]} for i in (5, 6)]
    res = W.walk_result(_record(findings=findings))
    assert res["capture"] == "c7h2kq" and res["walk"] == "w3f9ak1" and res["start"] == "n3"
    assert res["steps"] == 8 and res["ended"] == "wrap"
    assert res["lines"][2] == '2. n5 "Item 2, Button" !out_of_order'
    assert [f.get("fix") is not None for f in res["findings"]] == [True, False]
    assert "keys" not in res["findings"][0] and "steps" not in res["findings"][0]
    assert res["diff"]["out_of_order"] == ["n5", "n6"]
    assert res["next"][:2] == ['node("n5",facets="tb")', 'image(overlay="walk",walk="w3f9ak1")']
    assert utf8_len(dumps(res["next"])) <= 200


def test_sixty_steps_with_findings_fit_five_kilobytes():
    findings = [{"code": c, "sev": "warn", "refs": ["n10", "n11"], "basis": "walk",
                 "msg": "x" * 180, "fix": FIX, "steps": [10, 11]}
                for c in ("tb.out_of_order", "tb.double_stop", "tb.ghost_stop", "tb.skipped",
                          "tb.escape", "tb.edge_stuck", "tb.revisit", "tb.wrong_announcement")]
    rec = _record(n_steps=61, findings=findings, notes=["a note " * 10],
                  vs_model={"agree": 50, "differ": 10, "first": "step 7: model n9, actual n10"})
    for s in rec["steps"][1:]:
        s["speak"] = "A rather long announcement for this list item, Button, double tap"
    res = W.walk_result(rec)
    assert _size(res) <= W.WALK_MAX_BYTES, _size(res)
    assert res["steps"] == 60 and res["findings"] and res["diff"]["model"].startswith("50 agree")
    rec2 = _record(n_steps=61)
    res2 = W.walk_result(rec2)
    assert len(res2["lines"]) == 61 and _size(res2) <= W.WALK_MAX_BYTES  # all 60 steps fit
    short = W.walk_result(rec2, max_lines=20)
    assert len(short["lines"]) == 21 and any("steps omitted" in ln for ln in short["lines"])


def test_a_walk_left_on_hints_the_restore():
    res = W.walk_result(_record(restore="left on (talkback restore to undo)"))
    assert 'talkback(action="restore")' in res["next"]


# --------------------------------------------------------------------------- #
# scenarios
# --------------------------------------------------------------------------- #
def _scenario(**kw):
    rec = {"id": "t3f9ak1", "kind": "survive", "serial": "e", "package": "p",
           "captures": ["c1aaaa", "c2bbbb"],
           "target": {"ref": "n47", "cls": "Button", "speak": "Item 7, Button"},
           "mutate": "tap Button 'Favourite' (injected: bypasses TalkBack)",
           "timeline": [{"t": 0, "focus": "n47"}, {"t": 35, "windows": 2},
                        {"t": 140, "focus": None}, {"t": 310, "focus": "n12"}] * 3,
           "focus": {"ref": "n12", "cls": "TextView", "speak": "Products, Heading"},
           "verdict": "reset_top", "lost_midway": True,
           "finding": {"code": "tb.focus_reset", "sev": "warn", "basis": "walk",
                       "msg": "after tap, focus reset top to n12 (was n47; 12 nodes removed, "
                              "12 added) " * 2, "fix": FIX * 2},
           "cause_text": "n47 rebound as n103 (a recycled cell or a re-created item); "
                         "12 removed, 12 added, 1 rebound",
           "restore": "restored", "notes": ["a long note " * 10]}
    rec.update(kw)
    return rec


def test_scenario_result_fits_one_kilobyte_and_keeps_the_verdict():
    res = W.scenario_result(_scenario())
    assert _size(res) <= W.SCENARIO_MAX_BYTES, _size(res)
    assert res["verdict"] == "reset_top" and res["finding"]["code"] == "tb.focus_reset"
    assert res["target"] == 'n47 "Item 7, Button"' and res["focus"] == 'n12 "Products, Heading"'
    assert res["capture"] == "c1aaaa" and res["after"] == "c2bbbb"
    assert res["cause"].startswith("n47 rebound as n103")
    small = W.scenario_result(_scenario(timeline=[{"t": 0, "focus": "n47"}], notes=[]))
    assert small["timeline"] == ["0 n47"] and small["finding"]["fix"].startswith("Group each")
    assert small["next"][0] == 'diff(a="c1aaaa",b="c2bbbb")'


def test_focus_after_result_says_what_the_window_did():
    rec = _scenario(kind="focus_after", action="activate (TalkBack click, Meta+Space)",
                    before={"windows": 1, "panes": []}, after={"windows": 2, "panes": ["Filters"]},
                    new_screen=True, model={"initial": "n50"}, verdict="initial_ok",
                    finding=None, cause_text=None, timeline=[], notes=[])
    res = W.scenario_result(rec)
    assert res["windows"] == "1->2" and res["panes"] == ["Filters"]
    assert res["model_initial"] == "n50" and res["did"].startswith("activate")


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
def test_save_list_resolve_drop_and_wipe(tmp_path):
    store = _Store(tmp_path)
    a = W.save(store, _record(id="waaaaa1"))
    b = W.save(store, _record(id="wbbbbb1", package="com.other"))
    os.utime(a, (1, os.path.getmtime(b) - 10))
    W.save(store, _scenario(id="tccccc1", package="com.example", serial="emulator-5554"))
    assert W.load(store, "waaaaa1")["id"] == "waaaaa1"
    assert W.resolve(store, None, ("emulator-5554", "com.example")) == "waaaaa1"
    assert W.resolve(store, "latest", ("emulator-5554", "com.other")) == "wbbbbb1"
    assert W.resolve(store, None, None, kind=None) == "tccccc1"
    rows, total = W.listing(store, ("emulator-5554", "com.example"), 10)
    assert total == 2 and rows[0].startswith("tccccc1 tb_scenario survive verdict=")
    assert "tb_walk 8 steps ended=wrap 0 findings" in rows[1]
    with pytest.raises(OpError) as e:
        W.resolve(store, "wzzzzz9")
    assert e.value.code == "walk_not_found"
    with pytest.raises(OpError) as e:
        W.resolve(store, "c7h2kq")
    assert e.value.code == "bad_args"
    with pytest.raises(OpError) as e:
        W.resolve(store, None, ("x", "y"))
    assert e.value.code == "walk_not_found" and "tb_walk()" in e.value.hint
    W.drop(store, "waaaaa1")
    with pytest.raises(OpError):
        W.load(store, "waaaaa1")
    assert W.wipe(store) == 2 and W.listing(store, None, 10) == ([], 0)


def test_old_walks_are_pruned(tmp_path, monkeypatch):
    store = _Store(tmp_path)
    monkeypatch.setattr(W, "KEEP", 3)
    now = __import__("time").time()
    for i in range(5):
        path = W.save(store, _record(id=f"w{i:06d}"))
        os.utime(path, (now - 100 + i, now - 100 + i))
    W.save(store, _record(id="wnewest"))
    left = sorted(os.listdir(W.walks_dir(store)))
    assert len(left) == 3 and "wnewest.json" in left


def test_a_stored_walk_shows_every_step(tmp_path):
    rec = _record(n_steps=80)
    out = W.stored_result(rec)
    assert len(out["lines"]) == 80 and _size(out) <= 16000
    assert W.stored_result(_scenario())["verdict"] == "reset_top"
    assert json.loads(dumps(out)) == out


# --------------------------------------------------------------------------- #
# What an overlay covers: the capture decides (live V5, emulator-5556)
# --------------------------------------------------------------------------- #
class _Loaded:
    """A capture as the store serves it, from an index and its raw facets."""

    def __init__(self, ix, raw):
        self.id, self.meta, self._ix, self._raw = raw.meta.id, raw.meta, ix, raw

    def index(self):
        return self._ix

    def raw(self, name):
        return getattr(self._raw, name)


def _escapes(rec):
    return [(f["msg"].split(":")[0], f["steps"]) for f in rec["findings"]
            if f["code"] == "tb.escape"]


def test_consecutive_steps_behind_an_overlay_are_one_escape():
    """H5 BAD: TalkBack walks out of the Compose "dialog" and reads all nine Views behind
    it; one finding names every step (the walk overlay marks them all)."""
    import copy

    import tb_capture_fixtures as F

    from inspector_widget.talkback import diff

    rec, _ = F.load_walk("tb_h5-bad-walk")
    found = diff.analyze(copy.deepcopy(rec))["findings"]
    esc = [f for f in found if f["code"] == "tb.escape"]
    assert len(esc) == 1 and esc[0]["msg"].startswith("steps 2-10: focus left the overlay")
    assert "read 9 stops behind it" in esc[0]["msg"]
    assert esc[0]["steps"] == list(range(2, 11))  # the nine behind it, not the dialog's own


def test_a_bound_walk_takes_what_an_overlay_covers_from_its_capture():
    """V5 BAD: the walk's own guess compares drawing orders across Views the a11y dump
    hoists, so it put the card's heading "behind" the scrim drawn under it and missed the
    eight buttons the scrim covers. Bound to a capture with the View tree, the walk reads
    what tb.escape reads: the title and the eight buttons, not the card."""
    import copy

    import tb_capture_fixtures as F

    rec, _ = F.load_walk("tb_v5-bad-walk")
    ix, raw = F.corpus_capture("tb_v5-bad-walk")
    unbound = copy.deepcopy(rec)
    from inspector_widget.talkback import diff

    unbound["findings"] = diff.analyze(unbound)["findings"]
    assert _escapes(unbound) == [("step 5", [5]), ("step 14", [14])]
    bound = W.bind_walk(copy.deepcopy(rec), W.Binding([(0, _Loaded(ix, raw))]))
    assert _escapes(bound) == [("steps 5-13", [5, 6, 7, 8, 9, 10, 11, 12, 13])]
    covered = [s["i"] for s in bound["steps"] if s.get("covered_by")]
    assert covered == list(range(5, 14))
    assert all(s["covered_by"]["overlay"] == "view:12" for s in bound["steps"]
               if s.get("covered_by"))


def test_a_walks_notes_name_nodes_by_ref():
    import copy

    import tb_capture_fixtures as F
    from inspector_widget.capture.index import apply_refs

    rec, _ = F.load_walk("tb_v5-bad-walk")
    ix, raw = F.corpus_capture("tb_v5-bad-walk")
    rx = apply_refs(ix, {n.key: f"n{i + 1}" for i, n in enumerate(ix.nodes.values())})
    rec = copy.deepcopy(rec)
    rec["notes"] = ["A11yAct could not focus view:12: the node refused the action",
                    "compose:99:4 is not in any capture"]
    W.bind_walk(rec, W.Binding([(0, _Loaded(rx, raw))]))
    assert rec["notes"][0].startswith("A11yAct could not focus n") and "view:" not in rec["notes"][0]
    assert rec["notes"][1] == "compose:99:4 is not in any capture"  # unknown: kept as is


def test_a_screen_replaced_mid_walk_is_not_read_as_talkback_navigation():
    """Live, emulator-5556: another activity of the app started 14 s into a walk of the V12
    list. The walk went on in the new window; the checks read the switch as TalkBack's
    doing (window_order, edge_stuck on the list, its unread rows "skipped"). The step's
    capture no longer holds the list's window: it is a screen change, and the model's
    prediction is compared with the first screen only."""
    import tb_capture_fixtures as F

    def loaded(name):
        ix, raw = F.live_capture(name)
        return _Loaded(ix, raw)

    def step(i, key, label, b, window, via="next", **kw):
        return {"i": i, "key": key, "label": label, "speak": label, "cls": "TextView",
                "bounds": b, "window": window, "via": via, "moved": True,
                "window_rect": [0, 0, 2076, 2152], **kw}

    v12 = [("view:2", "V12 BAD: A list with an empty header item", [0, 136, 2076, 120])] + [
        (f"view:{13 + i}", f"Message {i + 1}", [0, 257 + 137 * i, 2076, 137]) for i in range(4)]
    c2 = [("sem:7:4", "C2 BAD: products", [39, 175, 416, 69]),
          ("sem:7:7", "Product A1", [39, 264, 989, 293]),
          ("sem:7:17", "Product B1", [1048, 264, 989, 244])]
    steps = [step(0, *v12[0], 4, via="start")]
    steps += [step(i, *v12[i], 4, container="view:3", container_can=["forward"],
                   container_cls="RecyclerView", container_rect=[0, 256, 2076, 1818])
              for i in range(1, 5)]
    steps += [step(5, *c2[0], 1, via="window"), step(6, *c2[1], 1), step(7, *c2[2], 1),
              dict(step(8, *c2[2], 1), edge=True), step(9, *c2[0], 1, via="wrap")]
    predicted = [{"key": f"view:{k}", "label": f"Message {k - 12}", "window": 4,
                  "bounds": [0, 257 + 137 * (k - 13), 2076, 137]} for k in range(13, 22)]
    predicted.insert(0, {"key": "view:2", "label": v12[0][1], "window": 4,
                         "bounds": v12[0][2]})
    # the engine re-predicts the new screen and appends its stops (as live)
    predicted += [{"key": k, "label": lab, "window": 1, "bounds": b} for k, lab, b in c2]
    rec = _record()
    rec.update(steps=steps, predicted=predicted, ended="wrap", findings=[])
    W.bind_walk(rec, W.Binding([(0, loaded("tb_v12_bad")), (5, loaded("tb_c2_bad"))]))
    assert rec["steps"][5]["via"] == "screen"
    codes = {f["code"] for f in rec["findings"]}
    assert not codes & {"tb.window_order", "tb.edge_stuck", "tb.skipped"}, rec["findings"]
    assert rec["vs_model"]["differ"] == 0
    assert any("the screen changed under the walk" in n for n in rec["notes"])
    assert W.step_line(rec["steps"][5]).endswith('"C2 BAD: products" via=screen')


def test_a_popup_read_last_is_named_by_ref():
    """V10 BAD (live: "window 21 (from y=925) is read only after ..."): the window's root
    id is not something the agent can query; its ref is."""
    import copy

    import tb_capture_fixtures as F
    from inspector_widget.capture.index import apply_refs

    rec, _ = F.load_walk("tb_v10-bad-walk")
    ix, raw = F.corpus_capture("tb_v10-bad-walk")
    rx = apply_refs(ix, {n.key: f"n{i + 1}" for i, n in enumerate(ix.nodes.values())})
    rec = W.bind_walk(copy.deepcopy(rec), W.Binding([(0, _Loaded(rx, raw))]))
    wo = [f for f in rec["findings"] if f["code"] == "tb.window_order"]
    assert len(wo) == 1
    assert re.match(r"step \d+: window n\d+ \(from y=\d+\) is read only after", wo[0]["msg"]), \
        wo[0]["msg"]


def test_a_navigation_rail_is_in_order_as_the_capture_reads_it():
    """Live, emulator-5556 (Now in Android on a 2076x2152 foldable): TalkBack reads the
    NavigationRail's three tabs, then the top app bar, then the feed. The walk's own guess
    (one XY-cut over the steps' boxes) read the first tab with the app bar and called
    "Saved" and "Interests" out of order; the lint never did. A walk bound to its capture
    orders its steps the way tb.out_of_order does (groups, the View tree), so both agree.
    tests/data/tb_walks_live/ holds that walk and the refs of its capture
    (tests/fixtures/tb_captures/nia_foryou_rail)."""
    import copy
    import gzip
    from pathlib import Path

    import tb_capture_fixtures as F
    from inspector_widget.capture.index import apply_refs
    from inspector_widget.talkback import diff

    data = Path(__file__).parent / "data" / "tb_walks_live"
    rec = json.loads(gzip.decompress((data / "nia_foryou_rail-walk.json.gz").read_bytes()))
    refmap = json.loads(gzip.decompress((data / "nia_foryou_rail-refmap.json.gz").read_bytes()))
    ix, raw = F.live_capture("nia_foryou_rail")
    flat = {f["code"]: f["refs"] for f in diff.analyze(copy.deepcopy(rec))["findings"]}
    assert flat["tb.out_of_order"] == ["n1544", "n1551"]  # the rail's tabs, by the boxes alone
    bound = W.bind_walk(copy.deepcopy(rec), W.Binding([(0, _Loaded(apply_refs(ix, refmap),
                                                                   raw))]))
    assert {f["code"]: f["refs"] for f in bound["findings"]} == {
        "tb.double_stop": ["n2551", "n2552"]}  # the topic chip and its checkbox: real
    assert bound["vs_model"]["agree"] == 9
    assert [s["vrank"][3] for s in bound["steps"][:4]] == [0, 1, 2, 3]

