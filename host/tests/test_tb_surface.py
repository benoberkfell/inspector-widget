"""The TalkBack tools on the capture surface (talkback-navigation.md part 4, T5),
offline: the real MCP server and CLI, the real Session / Client / walk code and
the capture pipeline against the fake agent and the fake TalkBack (adb, settings,
uinput). Every test checks that the device's settings came back.

What a walk adds to the capture flow: it captures the screen TalkBack walks (and
recaptures what it scrolls in), names every step by capture ref, classifies the
model's prediction against what TalkBack did by ref, stores the walk under
``<store>/walks/<id>.json`` with the capture ids, and draws it
(``image(overlay="walk")``). One implementation serves both surfaces and the
default (pre-capture) listing.
"""

from __future__ import annotations

import json
import os
import re

import pytest

import fakeagent
import mcp_server
from fakeagent import DEFAULT_PACKAGE as PKG
from fakeagent import DEFAULT_SERIAL as SERIAL
from fakeagent import TB_TITLE, ViewSpec, tb_item
from inspector_widget import ops, surface
from inspector_widget.capture import walks as W
from inspector_widget.output import dumps, utf8_len

FAST = {"step_timeout_ms": 250, "settle_ms": 20}


@pytest.fixture(autouse=True)
def _capture_listing(monkeypatch):
    """The capture loop's listing: its hints name capture tools. The default listing
    (legacy + TalkBack) has its own tests below."""
    monkeypatch.setenv(surface.ENV_TOOLSET, "capture,talkback")


@pytest.fixture
def tb(tb_env):
    """The title + 6 items scene, TalkBack's order over it, and the settings check."""
    tb_env.scene_factory = fakeagent.talkback_scene
    tb_env.talkback.order = [TB_TITLE] + [tb_item(i) for i in range(6)]
    tb_env.original = dict(tb_env.secure)
    yield tb_env
    assert fakeagent.settings_changes(tb_env, tb_env.original) == {}
    assert fakeagent.key_safety_violations(tb_env) == []


def call(tool: str, **args):
    """One MCP call as an agent sees it: (the JSON document, isError)."""
    text, is_error = mcp_server._call_tool_text(tool, args)
    return json.loads(text), is_error


def ok(tool: str, **args) -> dict:
    doc, is_error = call(tool, **args)
    assert not is_error, doc
    return doc


def walks_dir(env) -> str:
    return os.path.join(str(env.store), "walks")


def record(env, wid: str) -> dict:
    with open(os.path.join(walks_dir(env), f"{wid}.json")) as f:
        return json.load(f)


def size(doc) -> int:
    return utf8_len(dumps(doc))


# --------------------------------------------------------------------------- #
# tb_walk: captures, refs, storage, the result
# --------------------------------------------------------------------------- #
def test_a_walk_names_its_steps_by_capture_ref_and_is_stored(tb, run_cli):
    res = ok("tb_walk", serial=SERIAL, **FAST)
    assert re.match(r"^c[0-9a-z]{5}$", res["capture"]) and W.is_walk_id(res["walk"])
    assert res["ended"] == "wrap" and res["restore"] == "restored" and res["steps"] == 9
    assert res["lines"][:3] == ["0. (no accessibility focus)", '1. n3 "Title"',
                                '2. n6 "Item 0. Button"']
    assert res["lines"][-1] == '9. n3 "Title" via=wrap'
    assert res["diff"] == {"ended": "wrap", "model": "6 agree, 0 differ"}
    # the backward walk starts at the last stop: from the first it meets the edge at once
    assert res["next"] == [f'image(overlay="walk",walk="{res["walk"]}")',
                           'tb_walk(direction="prev",start="n11")']
    rec = record(tb, res["walk"])
    assert rec["captures"] == [res["capture"]] and rec["id"] == res["walk"]
    moved = [s for s in rec["steps"] if s.get("key")]
    assert all(re.match(r"^n\d+$", s["ref"]) and s["cap"] == res["capture"] for s in moved)
    assert [p["ref"] for p in rec["predicted"]] == ["n3", "n6", "n7", "n8", "n9", "n10", "n11"]
    # the walk's capture was taken with TalkBack on, props off; its refs are the walk's
    show = ok("captures", action="show", id=res["capture"])
    assert any("TalkBack on (tb_walk)" in d for d in show.get("diagnostics") or [])
    assert show["options"] == {"props": False}
    assert ok("node", ref="n6", facets="tb")["tb"]["stop"] == 2
    assert ok("outline", view="reading")["lines"][0].startswith('1. n3 TextView "Title"')


def test_walks_are_listed_shown_exported_and_dropped(tb, run_cli):
    res = ok("tb_walk", serial=SERIAL, **FAST)
    wid = res["walk"]
    listed = ok("captures", what="walks")
    assert listed["lines"][0].startswith(f"{wid} tb_walk 9 steps ended=wrap 0 findings "
                                         f"{SERIAL}/{PKG} on {res['capture']}")
    assert listed["next"] == [f'captures(action="show",id="{wid}")']
    shown = ok("captures", action="show", id=wid)
    assert shown["lines"] == res["lines"] and shown["walk"] == wid
    # the CLI reads the same stored walk, byte for byte
    r = run_cli("captures", "show", wid, "--json")
    assert r.rc == 0 and r.out.rstrip("\n") == dumps(shown)
    r = run_cli("captures", "--what", "walks", "--json")
    assert r.rc == 0 and json.loads(r.out)["lines"] == listed["lines"]
    exported = ok("captures", action="export", id=wid)
    assert os.path.isfile(exported["path"]) and exported["captures"] == [res["capture"]]
    assert ok("captures", action="drop", id=wid) == {"dropped": wid}
    gone, is_error = call("captures", action="show", id=wid)
    assert is_error and gone["error"]["code"] == "capture_not_found"  # no longer a walk id
    bad, is_error = call("captures", action="pin", id=ok("tb_walk", **FAST)["walk"])
    assert is_error and bad["error"]["code"] == "bad_args"


def test_the_cli_runs_the_same_tool(tb, run_cli):
    r = run_cli("tb-walk", "--serial", SERIAL, "--prev", "--step-timeout-ms", 250,
                "--settle-ms", 20, "--json")
    assert r.rc == 0, r
    doc = json.loads(r.out)
    assert doc["walk"] and doc["capture"] and any(re.match(r"^\d+\. n\d+ ", ln)
                                                  for ln in doc["lines"])
    assert record(tb, doc["walk"])["direction"] == "prev"
    r = run_cli("tb-walk", "--serial", SERIAL, "--until", "edge", "--step-timeout-ms", 250,
                "--settle-ms", 20)
    assert r.rc == 0 and '  1. n3 "Title"' in r.out and "diff: " in r.out
    assert "next: inspector-widget image --overlay walk --walk w" in r.out


def test_start_by_ref_focuses_that_node(tb):
    cap = ok("capture", serial=SERIAL, package=PKG)
    assert cap["capture"]
    res = ok("tb_walk", start="n8", until="edge", **FAST)
    assert res["start"] == "n8" and res["lines"][0] == '0. n8 "Item 2. Button"'
    assert res["lines"][1:4] == ['1. n9 "Item 3. Button"', '2. n10 "Item 4. Button"',
                                 '3. n11 "Item 5. Button"']
    assert record(tb, res["walk"])["start_via"] == "a11y_act"
    # a selector resolves in the walk's capture too, a label goes to TalkBack as spoken
    res = ok("tb_walk", start='Button"Item 4"', until="edge", **FAST)
    assert res["start"] == "n10"
    res = ok("tb_walk", start="Item 5", until="edge", **FAST)
    assert res["start"] == "n11"


def test_a_start_that_matches_nothing_is_an_error_and_restores(tb):
    # a selector that matches nothing is taken as the label as spoken, which no stop says
    doc, is_error = call("tb_walk", start='Button"Nope"', **FAST)
    assert is_error and doc["error"]["code"] == "start_not_found"
    assert "Nope" in doc["error"]["message"]


def test_a_ref_no_capture_holds_fails_before_talkback_is_touched(tb):
    presses = len(tb.talkback.presses)
    doc, is_error = call("tb_walk", serial=SERIAL, package=PKG, start="n40", **FAST)
    assert is_error and doc["error"]["code"] == "capture_not_found"
    assert tb.secure == tb.original  # TalkBack never turned on
    ok("capture", serial=SERIAL, package=PKG)
    doc, is_error = call("tb_walk", start="n999", **FAST)
    assert is_error and doc["error"]["code"] == "ref_not_in_capture"
    doc, is_error = call("tb_walk", expect=["n3", "n999"], **FAST)
    assert is_error and doc["error"]["code"] == "ref_not_in_capture"
    assert len(tb.talkback.presses) == presses  # no key ever pressed


def test_labels_that_look_like_selectors_are_matched_as_spoken(tb):
    # "@alice", "#general", 'Say "Hi"', "Settings > Display" are common labels: they no
    # longer fail as selectors, and never after the walk ran (its record was lost)
    ok("capture", serial=SERIAL, package=PKG)
    res = ok("tb_walk", expect=["Title", 'Item 0 "Button"', "@alice", "Settings > Display"],
             **FAST)
    assert res["walk"] and res["expect"]["ok"] is False
    assert os.path.isfile(os.path.join(walks_dir(tb), f"{res['walk']}.json"))
    doc, is_error = call("tb_walk", start="#1 Item", **FAST)
    assert is_error and doc["error"]["code"] == "start_not_found"


def test_expect_takes_refs_selectors_and_labels(tb):
    ok("capture", serial=SERIAL, package=PKG)
    res = ok("tb_walk", expect=["n3", 'Button"Item 1"', "Item 0"], **FAST)
    assert res["expect"]["ok"] is False and res["expect"]["first_mismatch"]
    assert res["diff"]["out_of_order"]
    good = ok("tb_walk", expect=["n3", "n6", 'Button"Item 1"'], **FAST)
    assert good["expect"] == {"ok": True, "matched": 3, "of": 3}


def test_findings_and_the_diff_are_keyed_by_refs(tb):
    tb.talkback.order = [TB_TITLE, tb_item(0), tb_item(3), tb_item(1), tb_item(4), tb_item(5)]
    res = ok("tb_walk", **FAST)
    assert res["diff"]["skip"] == ["n8"] and res["diff"]["unvisited"] == ["n8"]
    assert res["diff"]["out_of_order"][0] in ("n7", "n9")
    assert "differ: step 3: model n7 'Item 1', actual n9" in res["diff"]["model"]
    skipped = next(f for f in res["findings"] if f["code"] == "tb.skipped")
    assert skipped["refs"] == ["n8"] and "n8 'Item 2'" in skipped["msg"]
    assert any("!out_of_order" in ln for ln in res["lines"])
    assert re.match(r'^node\("n\d+",facets="tb"\)$', res["next"][0])  # the first finding's


def _scene_with(*children):
    content = ViewSpec(1002, "LinearLayout", "android.widget", (0, 0, 360, 640),
                       a11y={"class_name": "android.widget.LinearLayout"}, children=list(children))
    decor = ViewSpec(1001, "DecorView", "com.android.internal.policy", (0, 0, 360, 640),
                     a11y={"class_name": "android.widget.FrameLayout"}, children=[content])
    return fakeagent.Scene(roots=[decor])


def test_a_double_stop_is_classified_by_ref(tb_env):
    switch = ViewSpec(1051, "Switch", "android.widget", (250, 140, 90, 60), text="Wi-Fi",
                      a11y={"class_name": "android.widget.Switch", "text": "Wi-Fi",
                            "clickable": True, "checkable": True})
    row = ViewSpec(1050, "LinearLayout", "android.widget", (0, 120, 360, 100),
                   a11y={"class_name": "android.widget.LinearLayout", "content_description": "Wi-Fi",
                         "clickable": True, "focusable": True}, children=[switch])
    tb_env.scene_factory = lambda: _scene_with(row)
    tb_env.talkback.order = [(1050, -1), (1051, -1)]
    original = dict(tb_env.secure)
    res = ok("tb_walk", serial=SERIAL, **FAST)
    refs = res["diff"]["double"]
    assert len(refs) == 2 and all(re.match(r"^n\d+$", r) for r in refs)
    rec = record(tb_env, res["walk"])
    by_key = {s["key"]: s["ref"] for s in rec["steps"] if s.get("key")}
    assert refs == [by_key["view:1050"], by_key["view:1051"]]
    assert fakeagent.settings_changes(tb_env, original) == {}


def _views(scene):
    out = {}
    for r in scene.roots:
        for v in r.walk():
            out[v.id] = v
    return out


def _scroll_item6_in(tb_env) -> None:
    """A list of 6 whose next swipe past Item 5 scrolls Item 6 in (a recapture)."""
    tb_env.scene_factory = lambda: fakeagent.talkback_scene(n_items=6, visible=4,
                                                            scroll_forward=True)
    tb_env.talkback.order = [TB_TITLE] + [tb_item(i) for i in range(7)]

    def scroll(t, action):
        if action == "next" and t.focus == tb_item(5):
            views = _views(tb_env.live_scene(PKG))
            column = views[1011]
            column.children.append(ViewSpec(1026, "Button", "android.widget", (16, 600, 328, 64),
                                            text="Item 6", a11y={"class_name": "android.widget.Button",
                                                                 "text": "Item 6", "clickable": True}))
            for v in column.children:
                x, y, w, h = v.bounds
                v.bounds = (x, y - 240, w, h)
                v.a11y.pop("visible_to_user", None)
            tap = tb_env.agent(PKG).a11y_tap
            tap.record(fakeagent.TYPE_VIEW_SCROLLED, 1001, 1010, -1, scroll_delta_y=240)
            tap.record(fakeagent.TYPE_WINDOW_CONTENT_CHANGED, 1001, 1011, -1)
            t.set_focus(tb_item(6))
            return True
        return False

    tb_env.talkback.on_press = scroll


def test_focus_on_a_node_no_capture_holds_recaptures(tb_env):
    """TalkBack scrolls Item 6 in: the walk recaptures (carry-over keeps the refs)
    and names the new item by a ref of its own."""
    _scroll_item6_in(tb_env)
    original = dict(tb_env.secure)
    res = ok("tb_walk", serial=SERIAL, until="edge", **FAST)
    assert res["recaptured"] and len(res["recaptured"]) == 1
    rec = record(tb_env, res["walk"])
    assert rec["captures"] == [res["capture"]] + res["recaptured"]
    new = next(s for s in rec["steps"] if s.get("key") == "view:1026")
    assert re.match(r"^n\d+$", new["ref"]) and new["cap"] == res["recaptured"][0]
    old = next(s for s in rec["steps"] if s.get("key") == "view:1020")
    assert old["cap"] == res["capture"] and old["ref"] == "n6"
    # the stored record names the recapture (not the engine's pre-store "model only")
    assert rec["recapture"] == f"step {new['i']}: {res['recaptured'][0]}"
    # recapture="never": the scrolled-in item has no ref, and says so
    res2 = ok("tb_walk", recapture="never", until="edge", **FAST)
    assert "recaptured" not in res2
    assert fakeagent.settings_changes(tb_env, original) == {}


def test_a_full_lineage_never_evicts_a_walks_captures_before_it_is_stored(tb_env):
    """Live, emulator-5556: an app whose lineage held 50 labeled captures lost the walk's
    start capture (unlabeled: evicted first) to the recapture's retention pass, and the
    walk failed with capture_not_found. The walk now holds its captures until stored."""
    _scroll_item6_in(tb_env)
    original = dict(tb_env.secure)
    ok("capture", serial=SERIAL, package=PKG, label="a")
    ok("capture", label="b")
    store = mcp_server._ops_context().store
    store.lineage_cap = 2  # the lineage is full of labeled captures
    res = ok("tb_walk", until="edge", **FAST)
    assert res["recaptured"] and len(res["recaptured"]) == 1
    assert store.held == set()  # released once the walk is stored
    rec = record(tb_env, res["walk"])
    assert rec["captures"] == [res["capture"]] + res["recaptured"]
    assert next(s for s in rec["steps"] if s.get("key") == "view:1026")["cap"] == \
        res["recaptured"][0]
    assert store.exists(res["capture"])
    # afterwards retention applies again: unlabeled captures go first
    ok("capture", label="c")
    assert not store.exists(res["capture"])
    assert fakeagent.settings_changes(tb_env, original) == {}


def test_sixty_steps_fit_the_budget_on_the_surface(tb_env):
    tb_env.scene_factory = lambda: fakeagent.talkback_scene(n_items=70)
    tb_env.talkback.order = [TB_TITLE] + [tb_item(i) for i in range(70)]
    res = ok("tb_walk", serial=SERIAL, max_steps=60, until="steps", **FAST)
    assert res["ended"] == "max_steps" and res["steps"] == 60
    assert size(res) <= 5000, size(res)
    assert len(record(tb_env, res["walk"])["steps"]) == 61


# --------------------------------------------------------------------------- #
# image(overlay="walk")
# --------------------------------------------------------------------------- #
def test_the_walk_overlay_draws_a_stored_walk(tb):
    pytest.importorskip("PIL")
    tb.talkback.order = [TB_TITLE, tb_item(0), tb_item(3), tb_item(1), tb_item(4), tb_item(5)]
    res = ok("tb_walk", serial=SERIAL, **FAST)
    img = ok("image", overlay="walk")
    assert img["walk"] == res["walk"] and img["capture"] == res["capture"]
    assert os.path.isfile(img["path"]) and img["path"].endswith(".png")
    assert img["steps"] == 7 and img["arrows"] >= 6 and img["window"] == "n1"
    assert size(img) <= 600
    from PIL import Image
    with Image.open(img["path"]) as im:
        px = im.convert("RGB").tobytes()
    red = sum(1 for i in range(0, len(px), 3) if px[i] > 180 and px[i + 1] < 100)
    assert red > 50  # the mismatches (and the skipped stop) are red
    again = ok("image", walk=res["walk"])  # walk alone means overlay="walk"
    assert again["path"] == img["path"]
    doc, is_error = call("image", overlay="walk", ref="n3")
    assert is_error and doc["error"]["code"] == "bad_args"


def test_the_walk_overlay_covers_several_windows(tb_env):
    """The default scene: the activity (Views + Compose) and a popup window."""
    pytest.importorskip("PIL")
    tb_env.talkback.order = [(1003, -1), (1004, -1), (1006, 2), (1006, 5), (2002, -1)]
    original = dict(tb_env.secure)
    res = ok("tb_walk", serial=SERIAL, until="edge", **FAST)
    assert any("via=window" in ln for ln in res["lines"])
    screen = ok("image", overlay="walk")
    assert screen["window"] == "screen" and screen["steps"] == 5
    rec = record(tb_env, res["walk"])
    popup = next(s for s in rec["steps"] if s.get("key") == "view:2002")
    one = ok("image", overlay="walk", window=popup["ref"])  # a node selects its window
    assert one["steps"] == 1 and one["omitted"] == 4 and "not on this capture" in one["note"]
    assert fakeagent.settings_changes(tb_env, original) == {}


def test_the_walk_overlay_stays_on_the_walks_capture(tb):
    pytest.importorskip("PIL")
    res = ok("tb_walk", serial=SERIAL, until="edge", **FAST)
    later = ok("capture")["capture"]
    img = ok("image", overlay="walk", capture="latest")  # the schema default, sent explicitly
    assert img["capture"] == res["capture"] != later
    other = ok("image", overlay="walk", capture=later)  # an explicit capture is honoured
    assert other["capture"] == later and other["steps"] == img["steps"]


def test_a_walk_that_leaves_the_app_does_not_capture_the_launcher(tb):
    """Pressing on TalkBack took the user out of the app: no capture of a
    backgrounded (maybe frozen) app, and the walk still reports by ref."""
    def leave(t, action):
        if len(t.presses) == 3:
            t.set_focus(None)
            t.device.activity_stack.append("com.android.launcher3/.Launcher")
            return True
        return False

    tb.talkback.on_press = leave
    try:
        res = ok("tb_walk", serial=SERIAL, **FAST)
        assert res["ended"] == "left_app" and res["diff"]["left_app"].startswith("com.android")
        assert res["lines"][1] == '1. n3 "Title"' and "recaptured" not in res
    finally:
        tb.activity_stack.pop()
    tb.talkback.on_press = None
    # a scenario whose action opens another app: no capture after it
    tb.on_broadcast = lambda args: tb.activity_stack.append("com.android.launcher3/.Launcher")
    try:
        sc = ok("tb_scenario", kind="focus_after", target="n6", action="broadcast:-a home",
                wait_ms=300, **FAST)
        assert "after" not in sc and any("is in front" in n for n in sc["notes"])
    finally:
        tb.on_broadcast = None
        tb.activity_stack.pop()


# --------------------------------------------------------------------------- #
# tb_scenario
# --------------------------------------------------------------------------- #
def test_a_scenario_names_target_and_landing_by_ref(tb):
    ok("capture", serial=SERIAL, package=PKG)
    res = ok("tb_scenario", kind="survive", target="n7", mutate="broadcast:-a x", wait_ms=400,
             **FAST)
    assert res["verdict"] == "kept" and res["target"].startswith('n7 "Item 1')
    assert res["focus"].startswith('n7 "') and res["cause"].startswith("n7 is still there")
    assert size(res) <= 1000 and res["scenario"].startswith("t")
    rec = record(tb, res["scenario"])
    assert rec["captures"] == [res["capture"], res["after"]]
    listed = ok("captures", what="walks")["lines"]
    assert listed[0].startswith(f"{res['scenario']} tb_scenario survive verdict=kept")


def test_a_scenario_taps_a_ref(tb, run_cli):
    ok("capture", serial=SERIAL, package=PKG)
    res = ok("tb_scenario", kind="focus_after", target="n6", action="tap:n8", wait_ms=400, **FAST)
    assert "Item 2" in res["did"] and res["verdict"] and size(res) <= 1000
    r = run_cli("tb-scenario", "focus-after", "--target", "n6", "--action", "activate",
                "--wait-ms", 400, "--step-timeout-ms", 250, "--settle-ms", 20, "--json")
    assert r.rc == 0 and json.loads(r.out)["kind"] == "focus_after"


def test_scenario_and_talkback_errors_are_envelopes(tb_env):
    doc, is_error = call("tb_scenario", serial=SERIAL)
    assert is_error and doc["error"]["code"] == "bad_args" and "kind" in doc["error"]["message"]
    doc, is_error = call("tb_scenario", serial=SERIAL, kind="survive")
    assert is_error and doc["error"]["code"] == "bad_args" and "mutate" in doc["error"]["message"]
    tb_env.talkback.installed = False
    doc, is_error = call("tb_walk", serial=SERIAL, **FAST)
    assert is_error and doc["error"]["code"] == "talkback_unavailable" and doc["error"]["hint"]
    doc, is_error = call("walk_nonsense")
    assert is_error


def test_talkback_on_points_at_the_walk_and_the_restore(tb):
    on = ok("talkback", action="on", serial=SERIAL, package=PKG)
    assert on["changed"] is True and on["next"] == ["tb_walk()", 'talkback(action="restore")']
    off = ok("talkback", action="restore")  # the serial comes from the default session
    assert off["restored"] is True and "next" not in off


# --------------------------------------------------------------------------- #
# One implementation, three listings
# --------------------------------------------------------------------------- #
def _listing(monkeypatch, toolset):
    if toolset is None:
        monkeypatch.delenv(surface.ENV_TOOLSET, raising=False)
    else:
        monkeypatch.setenv(surface.ENV_TOOLSET, toolset)
    return {t["name"]: t for t in mcp_server._fallback_handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}


def test_the_default_listing_keeps_the_pre_capture_shape_and_runs_the_surface(tb, monkeypatch):
    legacy = _listing(monkeypatch, None)["tb_walk"]
    assert legacy["inputSchema"] == mcp_server._TB_LEGACY_LISTING["tb_walk"]["schema"]
    assert "required" in legacy["inputSchema"]  # package was required before
    res = mcp_server._fallback_handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                       "params": {"name": "tb_walk", "arguments": dict(
                                           FAST, serial=SERIAL, package=PKG)}})["result"]
    doc = json.loads(res["content"][0]["text"])
    assert res["isError"] is False and doc["walk"] and doc["capture"]
    for toolset in ("capture,talkback", "talkback", "all"):
        shaped = _listing(monkeypatch, toolset)["tb_walk"]
        assert shaped["inputSchema"] == surface.json_schema(surface.spec("tb_walk"))
        # the capture loop only where its tools are listed
        assert shaped["description"] == (surface.D_TB_WALK_ALONE if toolset == "talkback"
                                         else surface.D_TB_WALK)


def test_the_loop_is_in_the_instructions_and_tb_walk():
    text = surface.instructions(surface.toolset_names("capture,talkback"))
    assert text.endswith(f" TalkBack: {surface.TB_LOOP}.")
    assert len(text.encode("utf-8")) <= surface.INSTRUCTIONS_MAX_BYTES
    assert surface.TB_LOOP in surface.D_TB_WALK
    for step in ('lint(rules=["tb"])', 'outline(view="reading",explain=true)',
                 'node(ref,facets="tb")', "tb_walk(start=ref)", 'image(overlay="walk")'):
        assert step in surface.TB_LOOP


@pytest.mark.parametrize("sel,is_sel", [
    ("n12", True), ("#ok", True), ("@tag", True), ('Button"OK"', True), ("view:1003", True),
    ("#feed > n3", True), ("Item 4", False), ("compose:1006:2", False), ("first", False),
])
def test_what_resolves_in_the_capture(sel, is_sel):
    assert ops._looks_like_selector(sel) is is_sel


def test_a_busy_device_says_what_to_do(tb):
    """Live, emulator-5556: a second walk while a CLI walk drove TalkBack got
    {"code": "busy", ..., "hint": null}. A TalkBack error without its own hint now carries
    its code's default one."""
    from inspector_widget.capture.model import ERROR_CODES
    from inspector_widget.talkback import device as tbdevice

    with tbdevice.device_lock(SERIAL, "tb_walk"):
        doc, is_error = call("tb_walk", serial=SERIAL, **FAST)
    assert is_error and doc["error"]["code"] == "busy"
    assert doc["error"]["hint"] == ERROR_CODES["busy"]


# --------------------------------------------------------------------------- #
# Stage 5 review: what each listing can follow up, sessions, storage, errors
# --------------------------------------------------------------------------- #
def _out_of_order(env):
    env.talkback.order = [TB_TITLE, tb_item(0), tb_item(3), tb_item(1), tb_item(4), tb_item(5)]


def test_the_default_listing_names_node_keys_and_hints_only_listed_tools(tb, monkeypatch):
    # Clients such as Claude Code expose only listed tools: the default listing has
    # inspect_node (node keys), not node/image/outline (refs)
    monkeypatch.delenv(surface.ENV_TOOLSET, raising=False)
    _out_of_order(tb)
    res = ok("tb_walk", serial=SERIAL, package=PKG, **FAST)
    listed = set(mcp_server._listed_tools())
    assert all(h.split("(", 1)[0] in listed for h in res["next"]), res["next"]
    refs = set(re.findall(r"\bn\d+\b", json.dumps(res["lines"])))
    assert refs and refs <= set(res["keys"])
    key = res["keys"]["n9"]
    assert res["next"][0] == f'inspect_node(node_key="{key}")'
    node = ok("inspect_node", serial=SERIAL, package=PKG, node_key=key)
    assert "Item 3" in json.dumps(node)
    legacy = mcp_server._listed_tools()["tb_walk"]["description"]
    assert "vs_model" not in legacy and "keys maps refs to node keys" in legacy


def test_the_talkback_toolset_hints_only_its_own_tools(tb, monkeypatch):
    monkeypatch.setenv(surface.ENV_TOOLSET, "talkback")
    _out_of_order(tb)
    res = ok("tb_walk", serial=SERIAL, package=PKG, **FAST)
    # no inspect_node in this listing: node keys would be bytes nothing can use
    assert res["next"] == ['tb_walk(direction="prev",start="n11")'] and "keys" not in res
    desc = mcp_server._listed_tools()["tb_walk"]["description"]
    assert "Loop:" not in desc and "selector" not in desc and "w3f9ak1" not in desc


def test_a_backward_walk_from_the_first_stop_compares_the_lap_after_the_wrap(tb):
    _out_of_order(tb)
    res = ok("tb_walk", serial=SERIAL, package=PKG, **FAST)
    assert res["diff"]["out_of_order"] == ["n9"]
    # from the first stop the edge comes at once: the lap after the wrap is compared
    back = ok("tb_walk", direction="prev", **FAST)
    assert back["lines"][:3] == ['0. n3 "Title"', "1. — edge",
                                 '2. n11 "Item 5. Button" via=wrap']
    assert back["diff"]["model"].startswith("2 agree, 3 differ")
    assert back["diff"]["out_of_order"] == ["n7"]


def test_device_wide_tools_never_follow_another_callers_default(tb, monkeypatch, run_cli):
    from inspector_widget.capture.store import CaptureStore

    # another agent's server (same store) last captured on its own emulator
    CaptureStore().set_default_session("emulator-9999", "com.other.app")
    monkeypatch.setenv("ANDROID_SERIAL", SERIAL)
    ctx = ops.OpContext(CaptureStore(), ops.AttachProvider(), "cli")
    assert ops._tb_device(ctx, None, None) == (SERIAL, None)
    r = run_cli("talkback", "status", "--json")
    assert r.rc == 0 and json.loads(r.out)["serial"] == SERIAL, r.err
    res = ok("tb_walk", until="edge", **FAST)
    assert res["session"] == f"{SERIAL}/{PKG}" and res["walk"]
    # naming the package lets the shared default complete the serial
    assert ops._tb_device(ctx, None, "com.other.app") == (SERIAL, "com.other.app")


def test_a_walk_says_when_its_default_app_is_gone(tb):
    ok("capture", serial=SERIAL, package=PKG)
    mcp_server._ops_context().session = (SERIAL, "com.example.gone")
    res = ok("tb_walk", until="edge", **FAST)
    assert res["session"] == f"{SERIAL}/{PKG}"
    assert any("com.example.gone (the default session) is not running; drove "
               f"{PKG}" in n for n in res["notes"])


def test_talkback_on_keeps_only_a_named_app_in_front(tb):
    ok("capture", serial=SERIAL, package=PKG)  # the default session: a11yprobe
    tb.activity_stack.append("com.android.settings/.Settings")
    on = ok("talkback", action="on")
    assert on["changed"] is True and on["serial"] == SERIAL
    assert ok("talkback", action="restore")["restored"] is True
    doc, is_error = call("talkback", action="on", package=PKG)
    assert is_error and doc["error"]["code"] == "app_left_foreground"


def test_dropping_the_latest_walk_drops_the_walk_not_its_capture(tb):
    res = ok("tb_walk", serial=SERIAL, package=PKG, until="edge", **FAST)
    assert ok("captures", action="show", what="walks", id="latest")["walk"] == res["walk"]
    assert ok("captures", action="drop", what="walks", id="latest") == {"dropped": res["walk"]}
    assert ok("captures", action="show", id=res["capture"])["capture"] == res["capture"]
    doc, is_error = call("captures", action="drop", what="walks", id="latest")
    assert is_error and doc["error"]["code"] == "walk_not_found"
    doc, is_error = call("captures", action="pin", what="walks", id=res["capture"])
    assert is_error and doc["error"]["code"] == "bad_args"


def test_talkback_failures_keep_their_codes_and_point_at_talkback():
    from inspector_widget.talkback import device as tbdevice

    for code in ("log_level_failed", "talkback_on", "something_new"):
        err = ops._tb_error(tbdevice.TalkBackError(code, "could not find Log output level"))
        d = err.to_dict()["error"]
        assert d["code"] == (code if code != "something_new" else "talkback_error")
        assert "ViewSpector" not in d["hint"] and "talkback" in d["hint"].lower()


def test_the_cli_says_talkback_stays_on_only_when_it_does(tb_env, run_cli):
    from inspector_widget.talkback import device as tbdevice

    tb_env.scene_factory = fakeagent.talkback_scene
    r = run_cli("talkback", "on", "--serial", SERIAL)
    assert r.rc == 0 and "stays on" in r.err
    assert run_cli("talkback", "restore", "--serial", SERIAL).rc == 0
    # the user had TalkBack on: off saves a snapshot that restore turns back on
    tb_env.secure.update({"enabled_accessibility_services": tbdevice.TALKBACK_COMPONENT,
                          "accessibility_enabled": "1"})
    tb_env.talkback.sync()
    r = run_cli("talkback", "off", "--serial", SERIAL)
    assert r.rc == 0 and "stays on" not in r.err and "talkback restore` puts back" in r.err
    r = run_cli("talkback", "status", "--serial", SERIAL, "--json")
    assert r.rc == 0 and json.loads(r.out)["restore_pending"] is True and "stays on" not in r.err
    r = run_cli("talkback", "restore", "--serial", SERIAL)
    assert r.rc == 0 and "note:" not in r.err and tb_env.talkback.running


def test_error_hints_name_only_listed_tools(tb, monkeypatch):
    # The talkback listing has no capture(), outline() or find(): an error's hint must not
    # send the agent there, but to what it has (a tb_walk captures the screen itself).
    monkeypatch.setenv(surface.ENV_TOOLSET, "talkback")
    listed = set(mcp_server._listed_tools())
    doc, is_error = call("tb_walk", serial=SERIAL, package=PKG, start="n40", **FAST)
    assert is_error and doc["error"]["code"] == "capture_not_found"
    assert doc["error"]["hint"].startswith("Run tb_walk once")
    ok("tb_walk", serial=SERIAL, package=PKG, **FAST)  # captures the screen
    doc, is_error = call("tb_walk", start="n99999", **FAST)
    assert is_error and doc["error"]["code"] == "ref_not_in_capture"
    hint = doc["error"]["hint"]
    assert set(re.findall(r"\b([a-z_]+)\(", hint)) <= listed and "tb_walk" in hint, hint
    # with the capture tools listed the hint is left as it is
    monkeypatch.setenv(surface.ENV_TOOLSET, "all")
    doc, _ = call("tb_walk", start="n99999", **FAST)
    assert "outline()" in doc["error"]["hint"]
    # the filter itself: a sentence naming an unlisted tool goes, the rest stays
    env = {"error": {"code": "not_found", "message": "m",
                     "hint": "Retry later. Or find(text=\"x\") in a capture."}}
    assert surface.listed_hint(env, {"tb_walk"})["error"]["hint"] == "Retry later."
    assert surface.listed_hint(env, None) is env


# --------------------------------------------------------------------------- #
# Paging a stored walk (G3) and TalkBack first (G1): both surfaces, the same call
# --------------------------------------------------------------------------- #
def test_a_stored_walk_is_paged_with_no_device_on_both_surfaces(tb, run_cli):
    res = ok("tb_walk", serial=SERIAL, **FAST)
    wid = res["walk"]
    tb.clear_logs()
    page = ok("tb_walk", show=wid, steps="2-4", speech="full")
    assert page["walk"] == wid and page["shown"] == "steps 2-4"
    assert [ln.split(".", 1)[0] for ln in page["lines"]] == ["2", "3", "4"]
    assert page["lines"][0] == '2. n6 "Item 0. Button"'
    latest = ok("tb_walk", show="latest", serial=SERIAL, package=PKG, steps="2-4",
                speech="full")
    assert latest == page
    r = run_cli("tb-walk", "--show", wid, "--steps", "2-4", "--speech", "full", "--json")
    assert r.rc == 0 and r.out.rstrip("\n") == dumps(page)
    r = run_cli("tb-walk", "--show", wid, "--steps", "2-4")
    assert r.rc == 0 and '  2. n6 "Item 0. Button"' in r.out
    # no device I/O: no adb command (the CLI's exit only drops this process's forwards),
    # no agent request, TalkBack untouched
    assert [a for a in tb.adb_log if a[0] != "forward"] == [] and tb.wire == []
    assert not tb.talkback.running
    bad, is_error = call("tb_walk", show=wid, steps="2..4")
    assert is_error and bad["error"]["code"] == "bad_args"
    gone, is_error = call("tb_walk", show="wzzzzzz")
    assert is_error and gone["error"]["code"] == "walk_not_found"


def test_relaunch_is_one_argument_on_both_surfaces(tb, run_cli):
    doc = ok("tb_walk", serial=SERIAL, package=PKG, relaunch=True, **FAST)
    r = run_cli("tb-walk", "--serial", SERIAL, "--package", PKG, "--relaunch",
                "--step-timeout-ms", 250, "--settle-ms", 20, "--json")
    assert r.rc == 0, r.err
    cli = json.loads(r.out)
    assert doc["talkback_started"] == cli["talkback_started"] == "before_app"
    assert len(tb.force_stops) == 2
    sc = ok("tb_scenario", kind="restore", serial=SERIAL, package=PKG, relaunch=True,
            target="Item 1", wait_ms=400, **FAST)
    assert sc["talkback_started"] == "before_app" and len(tb.force_stops) == 3
