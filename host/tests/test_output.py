"""Offline tests for inspector_widget.output (spec section 2: Phase-0 output hygiene)."""

from __future__ import annotations

import argparse
import copy
import json
import os
import random
import stat
import time

import live_fixtures as lf
import pytest

from inspector_widget import output as out

# (screen, fixture, tool, args, spec 2.6 target in bytes)
PHASE0_TARGETS = [
    ("launcher", "compose_slots", "dump_compose", {}, 24000),
    ("launcher", "compose_sem", "dump_compose", {"include_slot_table": False}, 6000),
    ("launcher", "inspect", "inspect", {}, 13000),
    ("launcher", "a11y", "dump_accessibility", {}, 12500),
    ("launcher", "a11y_lint", "a11y_lint", {}, 1200),
    ("launcher", "views_props", "dump_tree", {"include_properties": True}, 8000),
    ("launcher", "get_properties", "get_properties", {}, 3500),
    ("viewscreen", "inspect", "inspect", {}, 18000),
    ("viewscreen", "views_props", "dump_tree", {"include_properties": True}, 20000),
]

ALL_FIXTURES = [
    ("launcher", "views", "dump_tree"), ("launcher", "views_cli", "dump_tree"),
    ("launcher", "views_props", "dump_tree"), ("launcher", "compose_slots", "dump_compose"),
    ("launcher", "compose_sem", "dump_compose"), ("launcher", "a11y", "dump_accessibility"),
    ("launcher", "a11y_lint", "a11y_lint"), ("launcher", "inspect", "inspect"),
    ("launcher", "inspect_props", "inspect"), ("launcher", "get_properties", "get_properties"),
    ("launcher", "inspect_node", "inspect_node"),
    ("launcher", "compose_overlay", "compose_overlay"),
    ("viewscreen", "views_props", "dump_tree"), ("viewscreen", "a11y", "dump_accessibility"),
    ("viewscreen", "inspect", "inspect"), ("viewscreen", "get_properties", "get_properties"),
]


def size(obj) -> int:
    return out.utf8_len(out.dumps(obj))


def walk(node):
    yield node
    for c in node.get("children") or []:
        yield from walk(c)


def wide_tree_result(fan: int = 6, props: bool = True) -> dict:
    """A strings.py-shaped dump_tree of the 259-view wide scene (E6), with ~60
    properties per view encoded as strings.property_to_dict would."""
    counter = [0]

    def node(depth):
        counter[0] += 1
        n = counter[0]
        d = {"id": 1000 + n, "class_name": "LinearLayout" if depth < 3 else "TextView",
             "qualified_name": "android.widget." + ("LinearLayout" if depth < 3 else "TextView"),
             "bounds": {"layout": {"x": n, "y": 3 * n, "w": 300, "h": 60}},
             "package_name": "android.widget",
             "resource": {"namespace": "com.example", "type": "id", "name": f"view_{n}"},
             "view_id_name": f"view_{n}"}
        if depth >= 3:
            d["text"] = f"Label {n}"
        else:
            d["children"] = [node(depth + 1) for _ in range(fan)]
        return d

    root = node(0)
    result = {"roots": [root]}
    if props:
        plist = [{"name": "gravity", "type": "GRAVITY", "is_layout": False, "value": 0,
                  "label": "center_vertical|start"},
                 {"name": "layout_width", "type": "DIMENSION", "is_layout": True, "value": 1080},
                 {"name": "paddingStart", "type": "DIMENSION", "is_layout": False, "value": 42},
                 {"name": "textColor", "type": "COLOR", "is_layout": False, "value": -16777216},
                 {"name": "text", "type": "STRING", "is_layout": False, "value": "Hello world"},
                 {"name": "visibility", "type": "INT_ENUM", "is_layout": False, "value": "visible"},
                 {"name": "alpha", "type": "FLOAT", "is_layout": False, "value": 1.0},
                 {"name": "enabled", "type": "BOOLEAN", "is_layout": False, "value": True}]
        plist += [{"name": f"attr_{i}", "type": "INT32", "is_layout": False, "value": i}
                  for i in range(50)]
        result["properties"] = {1000 + i: copy.deepcopy(plist) for i in range(1, counter[0] + 1)}
    return result


# --------------------------------------------------------------------------- encoding + budgets
def test_dumps_compact_pretty_unicode_and_default():
    obj = {"a": [1, 2], "t": "▶ All", "o": object.__new__(type("X", (), {"__str__": lambda s: "X"}))}
    assert out.dumps({"a": [1, 2], "t": "▶"}) == '{"a":[1,2],"t":"▶"}'
    assert out.dumps(obj).endswith(',"o":"X"}')
    assert out.dumps({"a": 1}, pretty=True) == '{\n  "a": 1\n}'
    assert out.utf8_len("▶") == 3 and out.json_cost("▶") == 5


def test_budget_accounting():
    b = out.Budget(1000)
    assert b.fits("x" * 800) and b.add("x" * 800)
    assert b.used == 800 and b.remaining == 0
    assert not b.fits("y") and not b.add("y")  # the 200-byte footer reserve is kept
    b2 = out.Budget(1000, reserve=0)
    assert b2.add("▶" * 333) and b2.used == 999 and not b2.add("ab")
    assert b2.add(1)  # int costs are accepted as bytes
    unlimited = out.Budget(0)
    assert unlimited.unlimited and unlimited.add("z" * 10**6) and unlimited.remaining is None


def test_max_bytes_resolution(monkeypatch):
    monkeypatch.delenv(out.ENV_MAX_BYTES, raising=False)
    assert out.resolve_max_bytes(None) == 32000
    assert out.resolve_max_bytes(0) == 0
    assert out.resolve_max_bytes(10) == 1000
    assert out.resolve_max_bytes(10**7) == 200000
    assert out.resolve_max_bytes("5000") == 5000
    monkeypatch.setenv(out.ENV_MAX_BYTES, "0")
    assert out.resolve_max_bytes(None) == 0
    monkeypatch.setenv(out.ENV_MAX_BYTES, "4000")
    assert out.resolve_max_bytes(None) == 4000
    monkeypatch.setenv(out.ENV_MAX_BYTES, "lots")
    assert out.resolve_max_bytes(None) == 32000


# --------------------------------------------------------------------------- finalize + spill
def test_finalize_under_budget_is_plain_compact_json(tmp_path):
    r = {"a": 1, "t": "▶"}
    assert out.finalize("dump_tree", r, max_bytes=None, spill_dir=str(tmp_path)) == \
        '{"a":1,"t":"▶"}'
    assert out.finalize("dump_tree", r, max_bytes=1000, spill_dir=str(tmp_path),
                        pretty=True) == out.dumps(r, pretty=True)
    assert os.listdir(tmp_path) == []


def test_finalize_spills_oversize_results_to_an_envelope(tmp_path):
    brief = out.slim("dump_tree", wide_tree_result(), {})
    full_text = out.dumps(brief)
    assert out.utf8_len(full_text) > 32000
    text = out.finalize("dump_tree", brief, max_bytes=None, spill_dir=str(tmp_path / "spill"))
    env = json.loads(text)
    assert out.utf8_len(text) <= 3000
    assert env["truncated"] is True and env["tool"] == "dump_tree"
    assert env["bytes"] == out.utf8_len(full_text) and env["max_bytes"] == 32000
    assert env["summary"]["windows"] == 1 and env["summary"]["nodes"] == 259
    assert env["summary"]["max_depth"] == 4
    assert 1 < len(env["preview"]) <= 25
    assert env["preview"][0] == "view:1001 LinearLayout #view_1 [1,3 300x60]"
    assert env["preview"][1] == "  view:1002 LinearLayout #view_2 [2,6 300x60] +42"
    assert "max_depth=2" in env["hint"] and "root=" in env["hint"]
    # the spill file holds the complete brief result, private to the user
    with open(env["spill_path"], encoding="utf-8") as f:
        assert json.load(f) == json.loads(full_text)
    assert stat.S_IMODE(os.stat(env["spill_path"]).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "spill").st_mode) == 0o700
    assert os.path.basename(env["spill_path"]).startswith("dump_tree-")


def test_envelope_never_exceeds_max_bytes(tmp_path):
    rng = random.Random(7)
    brief = out.slim("dump_tree", wide_tree_result(props=False), {})
    long_dir = tmp_path / ("d" * 120)
    for _ in range(40):
        limit = rng.randint(1000, 40000)
        text = out.finalize("dump_tree", brief, max_bytes=limit, spill_dir=str(long_dir))
        assert out.utf8_len(text) <= limit
        env = json.loads(text)
        if env.get("truncated"):
            assert out.utf8_len(text) <= 3000
            with open(env["spill_path"], encoding="utf-8") as f:
                assert json.load(f) == brief


def test_envelope_carries_capture_and_survives_an_unwritable_spill_dir(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    result = dict(out.slim("dump_tree", wide_tree_result(props=False), {}), capture="c7h2kq")
    env = json.loads(out.finalize("dump_tree", result, max_bytes=2000,
                                  spill_dir=str(blocker / "spill")))
    assert env["truncated"] and env["capture"] == "c7h2kq"
    assert "spill_path" not in env and env["spill_error"]


def test_purge_spill_removes_files_older_than_the_ttl(tmp_path):
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    keep = tmp_path / "notes.txt"
    for p in (old, new, keep):
        p.write_text("{}")
    past = time.time() - 7200
    os.utime(old, (past, past))
    os.utime(keep, (past, past))
    assert out.purge_spill(str(tmp_path)) == 1
    assert sorted(os.listdir(tmp_path)) == ["new.json", "notes.txt"]
    assert out.purge_spill(str(tmp_path / "missing")) == 0


def test_default_spill_dir_follows_the_store_root(monkeypatch, tmp_path):
    monkeypatch.setenv("INSPECTOR_WIDGET_CAPTURE_DIR", str(tmp_path))
    assert out.default_spill_dir() == os.path.join(str(tmp_path), "spill")


def test_preview_lines_cover_every_tree_shape():
    a11y = lf.load("launcher", "a11y")
    lines = out.preview_lines(a11y)
    assert lines[0].startswith("a11y:1:-1 FrameLayout [0,0 1280x2856]")
    comp = lf.load("launcher", "compose_sem")
    assert out.preview_lines(comp)[:2] == ["compose:82 AndroidComposeView [0,0 1280x2856]",
                                           "  compose:150 Node [0,0 1280x2856] +16"]
    ins = lf.load("launcher", "inspect")
    assert out.preview_lines(ins)[:2] == ["view:1 DecorView [0,0 1280x2856]",
                                          "  view:78 LinearLayout [0,0 1280x2856] +23"]
    many = out.preview_lines(out.slim("dump_tree", wide_tree_result(props=False), {}),
                             max_lines=5, depth=3)
    assert len(many) == 5 and many[-1].strip().startswith("…") and "more lines" in many[-1]
    assert out.preview_lines({"findings": []}) == []


def test_summarize_counts_lists_and_facets():
    s = out.summarize(lf.load("launcher", "inspect"))
    assert s["windows"] == 1 and s["nodes"] == 27 and s["view"] == 9 and s["compose"] == 18
    assert s["a11y"] == 24
    assert out.summarize(lf.load("launcher", "a11y_lint"))["findings"] == 14


# --------------------------------------------------------------------------- slim: generic
@pytest.mark.parametrize("screen,name,tool", ALL_FIXTURES)
def test_detail_full_is_the_identity_and_brief_never_mutates(screen, name, tool):
    data = lf.load(screen, name)
    assert out.slim(tool, data, {"detail": "full"}) is data
    assert json.loads(out.dumps(out.slim(tool, data, {"detail": "full"}))) == data
    before = copy.deepcopy(data)
    brief = out.slim(tool, data, {})
    assert data == before
    assert size(brief) < size(data)
    json.loads(out.dumps(brief))  # always serializable


@pytest.mark.parametrize("screen,name,tool,args,target", PHASE0_TARGETS)
def test_phase0_byte_targets_on_real_outputs(screen, name, tool, args, target):
    brief = out.slim(tool, lf.load(screen, name), args)
    assert size(brief) <= target, f"{screen} {tool}: {size(brief)} > {target}"


def test_passthrough_for_errors_compact_only_and_unknown_tools():
    err = {"error": "boom", "tool": "dump_tree"}
    assert out.slim("dump_tree", err, {}) is err
    for tool in out.COMPACT_ONLY_TOOLS + ("no_such_tool",):
        r = {"path": "/tmp/x.png", "width": 1}
        assert out.slim(tool, r, {}) is r
    assert out.slim("dump_tree", [1, 2], {}) == [1, 2]


# --------------------------------------------------------------------------- slim: dump_tree
def test_dump_tree_brief_node_shape_from_both_legacy_shapes():
    legacy = out.slim("dump_tree", lf.load("launcher", "views"), {})
    cli = out.slim("dump_tree", lf.load("launcher", "views_cli"), {})
    lnodes = list(walk(legacy["roots"][0]))
    cnodes = {n["id"]: n for n in walk(cli["roots"][0])}
    common = [n for n in lnodes if n["id"] in cnodes]
    assert len(common) >= 8  # recorded moments apart: AndroidViewsHandler came and went
    for a in common:  # the legacy MCP and strings.py shapes converge
        b = cnodes[a["id"]]
        assert {k: v for k, v in a.items() if k != "children"} == \
            {k: v for k, v in b.items() if k != "children"}
    content = next(n for n in lnodes if n["id"] == 80)
    assert content == {"id": 80, "class_name": "FrameLayout", "package_name": "android.widget",
                       "bounds": [0, 0, 1280, 2856],
                       "resource": {"namespace": "android", "type": "id", "name": "content"},
                       "children": content["children"]}
    assert "qualified_name" not in content and "view_id_name" not in content
    ll = next(n for n in lnodes if n["id"] == 78)
    assert ll["layout_resource"] == {"namespace": "android", "type": "layout",
                                     "name": "screen_simple"}
    by_id = {n["id"]: n for n in lnodes}
    assert "layout_resource" not in by_id[80]  # inherited from 78
    assert by_id[79]["layout_resource"] is None  # the ViewStub was not inflated from it
    assert legacy["serial"] == "emulator-5554" and legacy["root_count"] == 1


def test_dump_tree_properties_become_non_default_maps():
    data = lf.load("launcher", "views_props")
    brief = out.slim("dump_tree", data, {"include_properties": True})
    assert set(brief["properties"]) == {str(g["view_id"]) for g in data["properties"]}
    assert set(brief["omitted_defaults"]) == set(brief["properties"])
    total = sum(len(g["properties"]) for g in data["properties"])
    kept = sum(len(v) for v in brief["properties"].values())
    assert kept + brief["omitted"]["defaults"] + brief["omitted"].get("duplicates", 0) == total
    assert brief["omitted"]["defaults"] == sum(brief["omitted_defaults"].values())
    decor = brief["properties"]["1"]
    assert decor["background"] == "#FFFEF7FF" and "alpha" not in decor
    # resolution stacks survive as {value, source?, stack}
    assert brief["properties"]["78"]["fitsSystemWindows"] == {
        "value": True, "source": "@android:layout/screen_simple",
        "stack": ["@android:layout/screen_simple"]}
    assert brief["screenshot"]["scale"] == 0.5


def test_dump_tree_duplicate_id_property_is_dropped_and_counted():
    brief = out.slim("dump_tree", lf.load("viewscreen", "views_props"), {})
    assert brief["omitted"]["duplicates"] == 22
    assert all("id" not in v for v in brief["properties"].values())


def test_dump_tree_max_depth_one_lists_the_windows():
    brief = out.slim("dump_tree", wide_tree_result(), {"max_depth": 1})
    assert size(brief) <= 2000
    assert len(brief["roots"]) == 1 and "children" not in brief["roots"][0]
    assert brief["roots"][0]["hidden_descendants"] == 258
    assert brief["omitted"]["depth"] == 258
    assert brief["omitted"]["properties_views"] == 258
    assert list(brief["properties"]) == ["1001"]
    two = out.slim("dump_tree", wide_tree_result(props=False), {"max_depth": 2})
    assert [n.get("hidden_descendants") for n in two["roots"][0]["children"]] == [42] * 6


def test_dump_tree_root_reroots_or_errors():
    data = wide_tree_result(props=False)
    for spec in ("1002", "view:1002", 1002):
        brief = out.slim("dump_tree", data, {"root": spec})
        assert [r["id"] for r in brief["roots"]] == [1002] and brief["root"] == str(spec)
        assert sum(1 for _ in walk(brief["roots"][0])) == 43
    err = out.slim("dump_tree", data, {"root": "99999"})
    assert err["error"].startswith("root '99999' not found") and err["tool"] == "dump_tree"


# --------------------------------------------------------------------------- slim: properties
def test_get_properties_brief_maps_all_properties():
    data = lf.load("launcher", "get_properties")
    brief = out.slim("get_properties", data, {})
    assert brief["view_id"] == 82 and brief["serial"] == "emulator-5554"
    assert len(brief["properties"]) == len(data["group"]["properties"])
    assert brief["properties"]["enabled"] is True
    nd = out.slim("get_properties", data, {"filter": "nondefault"})
    assert len(nd["properties"]) + nd["omitted"]["defaults"] == len(brief["properties"])
    assert "enabled" not in nd["properties"] and nd["properties"]["focusable"] == "true"
    cli = out.slim("get_properties", lf.load("viewscreen", "get_properties"), {})
    assert cli["view_id"] == 13 and cli["properties"]["text"] == "Hard to read text (1.6:1)"
    assert cli["properties"]["textColor"].startswith("#") and "group" not in cli


# --------------------------------------------------------------------------- slim: compose
def test_dump_compose_hides_library_composables_and_normalizes():
    data = lf.load("launcher", "compose_slots")
    brief = out.slim("dump_compose", data, {})
    assert brief["hidden"] == {"library_composables": 325}
    root = brief["windows"][0]["root"]
    nodes = list(walk(root))
    assert len(nodes) == 399 - 325
    assert root["name"] == "AndroidComposeView" and root["kind"] == "COMPOSABLE"
    heading = next(n for n in nodes if n.get("source") == "MainActivity.kt:151"
                   and n["attrs"].get("text") == "Section heading")
    assert heading["attrs"] == {"text": "Section heading", "overflow": "Clip", "softWrap": "true",
                                "maxLines": "inf", "minLines": "1",
                                "style": "16sp/24sp w400 ls0.5sp"}
    sem = next(n for n in nodes if n["id"] == 448)
    assert "kind" not in sem and sem["actions"] == ["OnClick", "RequestFocus",
                                                     "GetTextLayoutResult"]
    assert all(len(v) <= 130 for n in nodes for v in (n.get("attrs") or {}).values())
    assert brief["omitted"]["boilerplate_actions"] == 39
    everything = out.slim("dump_compose", data, {"user_code_only": False})
    assert sum(1 for _ in walk(everything["windows"][0]["root"])) == 399
    assert "hidden" not in everything
    assert out.slim("dump_compose", data, {"user_code_only": "false"})["windows"] == \
        everything["windows"]


def test_dump_compose_depth_and_root():
    data = lf.load("launcher", "compose_sem")
    top = out.slim("dump_compose", data, {"max_depth": 2})
    root = top["windows"][0]["root"]
    assert [c["id"] for c in root["children"]] == [150]
    assert root["children"][0]["hidden_descendants"] == 16
    sub = out.slim("dump_compose", data, {"root": "compose:325"})
    assert sub["windows"][0]["view_id"] == 82 and sub["windows"][0]["root"]["id"] == 325
    assert len(sub["windows"][0]["root"]["children"]) == 12
    assert out.slim("dump_compose", data, {"root": "sem:82:448"})["windows"][0]["root"]["id"] \
        == 448
    amb = out.slim("dump_compose", lf.load("launcher", "compose_slots"), {"root": "0"})
    assert "ambiguous" in amb["error"] and len(amb["candidates"]) == 5


def test_compose_overlay_caps_on_screen():
    data = lf.load("launcher", "compose_overlay")
    many = dict(data, on_screen=data["on_screen"] * 5)
    brief = out.slim("compose_overlay", many, {})
    assert len(brief["on_screen"]) == 30 and brief["on_screen_total"] == len(many["on_screen"])
    assert "overlay_path" not in brief and brief["path"] == data["path"]
    assert brief["on_screen"][0] == {"text": data["on_screen"][0]["text"],
                                     "bounds": [0, 348, 1280, 216]}


# --------------------------------------------------------------------------- slim: a11y
def test_dump_accessibility_brief_and_focus_order_modes():
    data = lf.load("launcher", "a11y")
    brief = out.slim("dump_accessibility", data, {})
    nodes = list(walk(brief["windows"][0]["root"]))
    assert len(nodes) == 40
    assert not any("package_name" in n or "actions_bitmask" in n or "max_text_length" in n
                   for n in nodes)
    row = nodes[4]
    assert row["actions"] == ["CLICK"] and row["flags"] == [
        "clickable", "focusable", "screen_reader_focusable"]
    stops = [e for e in data["focus_order"] if e["is_focus_stop"]]
    assert brief["focus_order"] == [{"order": e["order"], "id": e["id"],
                                     "speakable": e["speakable"]} for e in stops]
    assert brief["omitted"]["focus_order_non_stops"] == len(data["focus_order"]) - len(stops)
    assert brief["omitted"]["boilerplate_actions"] > 0 and brief["omitted"]["empty_extras"] > 0
    full = out.slim("dump_accessibility", data, {"focus_order": "full"})
    assert full["focus_order"] == data["focus_order"]
    none = out.slim("dump_accessibility", data, {"focus_order": "none"})
    assert "focus_order" not in none and none["omitted"]["focus_order"] == len(data["focus_order"])


def test_dump_accessibility_root_and_package_inference():
    data = lf.load("launcher", "a11y")
    # pre-ID1: every Compose node is 1:11, so that key is ambiguous; the packed id is not
    amb = out.slim("dump_accessibility", data, {"root": "a11y:1:11"})
    assert "ambiguous" in amb["error"]
    one = out.slim("dump_accessibility", data, {"root": "1:-1", "max_depth": 1})
    assert one["windows"][0]["root"]["host_view_id"] == 1
    assert one["windows"][0]["root"]["hidden_descendants"] == 39
    cli = lf.load("viewscreen", "a11y")  # CLI shape: no "package" key
    brief = out.slim("dump_accessibility", cli, {})
    assert not any("package_name" in n for n in walk(brief["windows"][0]["root"]))


# --------------------------------------------------------------------------- slim: lint
def test_a11y_lint_group_by_rule():
    data = lf.load("launcher", "a11y_lint")
    brief = out.slim("a11y_lint", data, {})
    assert brief["summary"] == {"error": 0, "warn": 14, "info": 0, "total": 14}
    role = brief["by_rule"]["a11y.role.missing_on_clickable"]
    assert role["sev"] == "warn" and role["n"] == 12 and role["nodes"] == [327, 338, 349]
    assert role["more"] == 9 and role["msg"].startswith("Clickable node has no Role")
    assert brief["by_rule"]["a11y.touch_target.small"] == {
        "sev": "warn", "n": 1, "msg": data["findings"][-2]["message"]
        if data["findings"][-2]["rule"] == "a11y.touch_target.small"
        else brief["by_rule"]["a11y.touch_target.small"]["msg"], "nodes": [448]}
    assert "findings" not in brief and brief["density"] == 480
    listed = out.slim("a11y_lint", data, {"group_by": "none"})
    assert listed["findings"] == data["findings"]


# --------------------------------------------------------------------------- slim: inspect
def test_inspect_brief_node_shape():
    data = lf.load("launcher", "inspect")
    brief = out.slim("inspect", data, {})
    nodes = list(walk(brief["roots"][0]))
    assert len(nodes) == 27 and brief["summary"] == data["summary"]
    decor = nodes[0]
    assert decor["node_key"] == "view:1" and decor["bounds"] == [0, 0, 1280, 2856]
    assert decor["view"] == {"id": 1, "class_name": "DecorView"}
    assert "conf" not in decor and "image_ref" not in decor
    assert "host_view_id" not in decor["a11y"]  # exact join: ids are implied
    row = next(n for n in nodes if n["node_key"] == "compose:338")
    assert row["conf"] == "overlap" and row["a11y"]["virtual_id"] == 11
    assert row["compose"] == {"name": "Icon button label, MissingContentDescription",
                              "attrs": {"IsTraversalGroup": "true",
                                        "TestTag": "launch_icon_button", "Focused": "false",
                                        "Text": "Icon button label, MissingContentDescription"},
                              "actions": ["OnClick", "RequestFocus", "GetTextLayoutResult"]}
    clipped = next(n for n in nodes if n["node_key"] == "compose:448")
    assert clipped["conf"] == "none" and "a11y" not in clipped  # 27 px visible: no IoU match
    content = next(n for n in nodes if n["node_key"] == "view:80")
    assert content["view"]["resource"] == "@android:id/content"


def test_inspect_properties_depth_and_root():
    data = lf.load("launcher", "inspect_props")
    brief = out.slim("inspect", data, {"include_properties": True})
    views = [n["view"] for n in walk(brief["roots"][0]) if "view" in n]
    assert all("properties" in v and "omitted_defaults" in v for v in views)
    assert brief["omitted"]["defaults"] == sum(v["omitted_defaults"] for v in views)
    top = out.slim("inspect", lf.load("launcher", "inspect"), {"max_depth": 1})
    assert top["roots"][0]["hidden_descendants"] == 26 and "children" not in top["roots"][0]
    sub = out.slim("inspect", lf.load("launcher", "inspect"), {"root": "compose:325"})
    assert sub["roots"][0]["node_key"] == "compose:325" and len(sub["roots"][0]["children"]) == 12
    # a bare 82 is both view:82 and the synthetic compose:82 window root (ID3)
    amb = out.slim("inspect", lf.load("launcher", "inspect"), {"root": "82"})
    assert "ambiguous" in amb["error"] and amb["candidates"] == ["view:82", "compose:82"]
    assert out.slim("inspect", lf.load("launcher", "inspect"), {"root": "view:82"})["roots"][0][
        "node_key"] == "view:82"


def test_inspect_node_brief():
    data = lf.load("launcher", "inspect_node")
    brief = out.slim("inspect_node", data, {})
    assert brief["bounds"] == [0, 0, 1280, 2856]
    view = brief["view"]
    assert view["qualified_name"] == "androidx.compose.ui.platform.AndroidComposeView"
    assert view["omitted_defaults"] + len(view["properties"]) == len(data["view"]["properties"])
    assert brief["a11y"]["actions"] == ["CUSTOM_0x01020036"]
    assert brief["lint"] == [] and brief["correlation_confidence"] == "overlap"


# --------------------------------------------------------------------------- params: MCP + CLI
def _tools():
    return {name: {"description": name, "schema": {"type": "object", "properties": {
        "serial": {"type": "string"}}, "additionalProperties": False}}
            for name in out.CLI_SUBCOMMANDS}


def test_augment_schemas_is_generated_from_the_table_and_idempotent(monkeypatch):
    monkeypatch.delenv(out.ENV_MAX_BYTES, raising=False)
    tools = _tools()
    out.augment_schemas(tools)
    snapshot = copy.deepcopy(tools)
    out.augment_schemas(tools)
    assert tools == snapshot
    for name, entry in tools.items():
        props = entry["schema"]["properties"]
        params = out.OUTPUT_PARAMS.get(name, [])
        assert set(props) == {"serial"} | {p.name for p in params}
        for p in params:
            assert props[p.name] == p.json_schema()
    dt = tools["dump_tree"]["schema"]["properties"]
    assert dt["detail"] == {"type": "string", "enum": ["brief", "full"], "default": "brief",
                            "description": "full = legacy"}
    assert dt["max_bytes"]["default"] == 32000 and "default" not in dt["max_depth"]
    # an existing property is never overwritten
    t2 = _tools()
    t2["inspect"]["schema"]["properties"]["detail"] = {"type": "string"}
    out.augment_schemas(t2)
    assert t2["inspect"]["schema"]["properties"]["detail"] == {"type": "string"}
    monkeypatch.setenv(out.ENV_MAX_BYTES, "9000")
    t3 = _tools()
    out.augment_schemas(t3)
    assert t3["inspect"]["schema"]["properties"]["max_bytes"]["default"] == 9000


def test_cli_flags_mirror_the_schema(monkeypatch):
    monkeypatch.delenv(out.ENV_MAX_BYTES, raising=False)
    tools = _tools()
    out.augment_schemas(tools)
    for tool, params in out.OUTPUT_PARAMS.items():
        sp = argparse.ArgumentParser()
        out.add_cli_flags(sp, tool)
        ns = sp.parse_args([])
        schema = tools[tool]["schema"]["properties"]
        for p in params:
            assert p.flag == "--" + p.name.replace("_", "-")
            assert getattr(ns, p.name) == schema[p.name].get("default")
        assert ns.pretty is False
        assert out.tool_args_from_cli(ns, tool) == {p.name: p.default_value() for p in params}
    sp = argparse.ArgumentParser()
    out.add_cli_flags(sp, "dump_compose")
    out.add_cli_flags(sp, "compose_overlay")  # same subcommand: no duplicate flags
    ns = sp.parse_args(["--no-user-code-only", "--max-depth", "2", "--root", "compose:325",
                        "--detail", "full", "--max-bytes", "0", "--pretty"])
    assert (ns.user_code_only, ns.max_depth, ns.root, ns.detail, ns.max_bytes, ns.pretty) == (
        False, 2, "compose:325", "full", 0, True)
    with pytest.raises(SystemExit):
        sp.parse_args(["--detail", "verbose"])
    sp2 = argparse.ArgumentParser()
    out.add_cli_flags(sp2, "a11y_lint")
    assert sp2.parse_args(["--group-by", "none"]).group_by == "none"
    sp3 = argparse.ArgumentParser()
    out.add_cli_flags(sp3, "list_devices")  # compact-only: just --pretty
    assert vars(sp3.parse_args([])) == {"pretty": False}
    assert out.full_args("inspect", {"serial": "s"})["detail"] == "brief"


def test_cli_subcommand_map_matches_the_real_cli_and_mcp():
    import cli
    import mcp_server

    assert set(out.CLI_SUBCOMMANDS) == set(mcp_server.TOOLS)
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert set(out.CLI_SUBCOMMANDS.values()) <= set(sub.choices)
    assert set(out.OUTPUT_PARAMS) <= set(mcp_server.TOOLS)
    # every generated flag can be added to the real subcommand without a clash
    for tool in out.OUTPUT_PARAMS:
        sp = copy.deepcopy(sub.choices[out.CLI_SUBCOMMANDS[tool]])
        out.add_cli_flags(sp, tool)


def test_tools_list_stays_within_budget():
    import mcp_server

    tools = copy.deepcopy(mcp_server.TOOLS)
    out.augment_schemas(tools)
    listing = {"tools": [{"name": n, "description": e["description"], "inputSchema": e["schema"]}
                         for n, e in tools.items()]}
    assert size(listing) <= 18500


def test_invalid_output_params_become_error_dicts():
    data = lf.load("launcher", "views")
    for args in ({"detail": "verbose"}, {"max_depth": "deep"}, {"focus_order": "all"}):
        tool = "dump_accessibility" if "focus_order" in args else "dump_tree"
        err = out.slim(tool, lf.load("launcher", "a11y") if tool != "dump_tree" else data, args)
        assert err["tool"] == tool and "must be" in err["error"]
    assert out.slim("a11y_lint", lf.load("launcher", "a11y_lint"), {"group_by": "x"})["error"]
    zero = out.slim("dump_tree", data, {"max_depth": 0})  # below 1 means roots only
    assert "children" not in zero["roots"][0] and zero["roots"][0]["hidden_descendants"] == 8


def test_layout_resource_null_marks_a_view_not_inflated_from_its_parents_layout():
    data = {"roots": [{"id": 1, "class_name": "FrameLayout", "bounds": {"layout": {}},
                       "layout_resource": {"type": "layout", "name": "main"},
                       "children": [{"id": 2, "class_name": "View", "bounds": {"layout": {}}},
                                    {"id": 3, "class_name": "View", "bounds": {"layout": {}},
                                     "layout_resource": {"type": "layout", "name": "main"}}]}]}
    root = out.slim("dump_tree", data, {})["roots"][0]
    assert root["layout_resource"] == {"type": "layout", "name": "main"}
    assert root["children"][0]["layout_resource"] is None
    assert "layout_resource" not in root["children"][1]
