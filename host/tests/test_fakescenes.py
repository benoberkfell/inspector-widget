"""Offline tests for tests/fakescenes.py: the wide scene and the real-data replays."""

from __future__ import annotations

import importlib.util
import json
import os
import zlib

import fakescenes as fs
import live_fixtures as lf
import pytest

import inspector_widget as iw
from inspector_widget import a11y as a11ymod
from inspector_widget import correlate, output, png
from inspector_widget import strings as st
from inspector_widget.proto import view_inspection_pb2 as pb

T = {"serial": "emulator-5554", "package": "com.oberkfell.a11yprobe"}


def compact(obj) -> int:
    return len(json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str).encode())


def walk(n):
    yield n
    for c in n.get("children") or []:
        yield from walk(c)


@pytest.fixture
def mcp(monkeypatch):
    """mcp_server with a fresh session cache and ``inspector_widget.attach`` bound
    to a scene (set ``mcp.use(scene)``)."""
    import mcp_server

    monkeypatch.setattr(mcp_server, "SESSIONS", mcp_server.SessionCache())

    class Driver:
        module = mcp_server

        def use(self, scene):
            monkeypatch.setattr(iw, "attach", fs.fake_attach(scene))
            return self

        def text(self, tool, **args):
            return mcp_server._call_tool_text(tool, dict(T, **args))

        def run(self, tool, **args):
            return mcp_server._run_tool(tool, dict(T, **args))

    return Driver()


# --------------------------------------------------------------------------- wide scene
def test_wide_scene_shape():
    scene = fs.wide_scene()
    s = scene.session()
    tree = st.dump_tree_to_dict(s.dump_tree(include_properties=True))
    nodes = list(walk(tree["roots"][0]))
    assert len(nodes) == 259
    leaves = [n for n in nodes if n["class_name"] == "TextView"]
    assert len(leaves) == 216 and leaves[0]["text"] == f"Label {leaves[0]['id'] - 1000}"
    assert nodes[1]["bounds"]["layout"] == {"x": 2, "y": 6, "w": 300, "h": 60}
    assert len(tree["properties"]) == 259
    assert all(len(p) == 60 for p in tree["properties"].values())
    gravity = next(p for p in tree["properties"][1001] if p["name"] == "gravity")
    assert gravity == {"name": "gravity", "type": "GRAVITY", "is_layout": False, "value": 0,
                       "label": "center_vertical|start"}
    a = a11ymod.a11y_to_dict(s.dump_a11y())
    anodes = list(walk(a["windows"][0]["root"]))
    assert len(anodes) == 259 and anodes[0]["flags"][:2] == ["clickable", "focusable"]
    assert [x["name"] for x in anodes[0]["actions"]] == [
        "CLICK", "SELECT", "CLEAR_SELECTION", "ACCESSIBILITY_FOCUS", "CLEAR_ACCESSIBILITY_FOCUS"]
    assert list(s.get_windows().root_ids) == [1001]
    w, h, _ = png._decode_to_rgba(s.screenshot().screenshot)
    assert (w, h) == (8, 8)


@pytest.mark.parametrize("tool,args,e6_bytes", [
    ("dump_tree", {}, 149_000),
    ("dump_tree", {"include_properties": True}, 1_740_000),
    ("dump_accessibility", {}, 488_000),
    ("inspect", {}, 547_000),
    ("inspect", {"include_properties": True}, 3_850_000),
])
def test_wide_scene_reproduces_the_e6_sizes_through_mcp(mcp, tool, args, e6_bytes):
    text, is_error = mcp.use(fs.wide_scene()).text(tool, **args)
    assert not is_error, text[:300]
    assert abs(len(text.encode()) - e6_bytes) <= 0.25 * e6_bytes


@pytest.mark.parametrize("tool,args", [
    ("dump_tree", {}), ("dump_tree", {"include_properties": True}),
    ("dump_accessibility", {}), ("inspect", {}), ("inspect", {"include_properties": True}),
    ("dump_compose", {}), ("get_properties", {"view_id": 1001}),
])
def test_wide_scene_through_the_output_layer_stays_in_budget(mcp, tmp_path, tool, args):
    mcp.use(fs.wide_scene())
    result = mcp.run(tool, **args)
    brief = output.slim(tool, result, args)
    text = output.finalize(tool, brief, max_bytes=None, spill_dir=str(tmp_path))
    assert len(text.encode()) <= 32000
    env = json.loads(text)
    if env.get("truncated"):
        assert len(text.encode()) <= 3000
        with open(env["spill_path"], encoding="utf-8") as f:
            assert json.load(f) == json.loads(output.dumps(brief))
    windows = output.slim("dump_tree", mcp.run("dump_tree"), {"max_depth": 1})
    assert compact(windows) <= 2000


# --------------------------------------------------------------------------- converters
def test_cli_shape_tree_round_trips():
    for screen, name in (("launcher", "views_cli"), ("viewscreen", "views_props")):
        data = lf.load(screen, name)
        back = json.loads(json.dumps(st.dump_tree_to_dict(fs.views_to_pb(data))))
        assert back == data, f"{screen}/{name}"


def test_legacy_mcp_tree_converts_with_e3_values_as_recorded():
    data = lf.load("launcher", "views_props")
    resp = fs.views_to_pb(data)
    back = st.dump_tree_to_dict(resp)
    legacy_nodes = list(walk(data["roots"][0]))
    new_nodes = list(walk(back["roots"][0]))
    assert [n["id"] for n in legacy_nodes] == [n["id"] for n in new_nodes]
    for a, b in zip(legacy_nodes, new_nodes):
        assert a["class_name"] == b["class_name"] and a.get("text") == b.get("text")
        assert [a["bounds"][k] for k in "xywh"] == [b["bounds"]["layout"][k] for k in "xywh"]
    props = {p["name"]: p for p in back["properties"][82]}
    assert props["foregroundGravity"]["value"] == 0 and "label" not in props["foregroundGravity"]
    assert props["outlineAmbientShadowColor"]["value"] == -16777216  # "#FF000000" re-encoded
    legacy_82 = next(g for g in data["properties"] if g["view_id"] == 82)
    assert len(back["properties"][82]) == len(legacy_82["properties"])


def test_compose_and_a11y_round_trip():
    for name in ("compose_sem", "compose_slots"):
        data = {k: v for k, v in lf.load("launcher", name).items()
                if k not in ("serial", "package", "note")}
        assert st.dump_compose_to_dict(fs.compose_to_pb(data)) == data
    for screen in ("launcher", "viewscreen"):
        data = lf.load(screen, "a11y")
        back = a11ymod.a11y_to_dict(fs.a11y_to_pb(data))
        assert back["windows"] == data["windows"]
        assert back.get("diagnostics") == data.get("diagnostics")


# --------------------------------------------------------------------------- replay scenes
def test_launcher_replay_serves_the_recorded_screen():
    scene = fs.replay_scene("launcher")
    s = scene.session()
    assert s.hello().api_level == 37 and list(s.get_windows().root_ids) == [1]
    comp = st.dump_compose_to_dict(s.dump_compose())
    assert abs(compact(comp) - 243_378) <= 0.05 * 243_378
    assert st.compose_slot_table_populated(comp)
    sem = st.dump_compose_to_dict(s.dump_compose(include_slot_table=False))
    assert not st.compose_slot_table_populated(sem)
    slots_only = st.dump_compose_to_dict(s.dump_compose(include_semantics=False))
    kids = slots_only["windows"][0]["root"]["children"]
    assert kids and all(k["kind"] == "COMPOSABLE" for k in kids)
    a = a11ymod.a11y_to_dict(s.dump_a11y())
    assert a["windows"] == lf.load("launcher", "a11y")["windows"]
    no_extras = a11ymod.a11y_to_dict(s.dump_a11y(include_extras=False))
    assert not any("extras" in n for n in walk(no_extras["windows"][0]["root"]))
    tree = st.dump_tree_to_dict(s.dump_tree())
    assert "properties" not in tree and [r["id"] for r in tree["roots"]] == [1]
    props = st.dump_tree_to_dict(s.dump_tree(include_properties=True))
    assert not any("resolution_stack" in p for pl in props["properties"].values() for p in pl)
    stacks = st.dump_tree_to_dict(s.dump_tree(include_properties=True,
                                              include_resolution_stack=True))
    assert any("resolution_stack" in p for pl in stacks["properties"].values() for p in pl)
    assert s.dump_tree(root_id=999).roots == []
    assert scene.requests.count("dump_compose") == 3


def test_slot_table_only_after_inspection_when_not_populated():
    scene = fs.replay_scene("launcher", slots_populated=False)
    s = scene.session()
    assert not st.compose_slot_table_populated(st.dump_compose_to_dict(s.dump_compose()))
    assert st.compose_slot_table_populated(
        st.dump_compose_to_dict(s.dump_compose(enable_inspection=True)))
    assert st.compose_slot_table_populated(st.dump_compose_to_dict(s.dump_compose()))  # sticky


def test_replayed_screenshot_decodes_to_the_recorded_pixels():
    from PIL import Image

    s = fs.replay_scene("launcher").session()
    shot = s.screenshot().screenshot
    assert shot.bitmap_type == 2 and shot.scale == 1.0
    raw = zlib.decompress(shot.data)
    assert raw[8] == 2 and len(raw) == 9 + 1280 * 2856 * 4
    w, h, rgba = png._decode_to_rgba(shot)
    assert (w, h) == (1280, 2856)
    img = Image.open(lf.path("launcher", "screen.png")).convert("RGBA")
    for x, y in ((0, 0), (640, 1428), (48, 210), (1279, 2855), (100, 2800), (700, 400)):
        o = 4 * (y * w + x)
        assert tuple(rgba[o:o + 4]) == img.getpixel((x, y))
    half = s.dump_tree(include_screenshot=True, screenshot_scale=0.5).screenshot
    assert (half.width, half.height, half.scale) == (640, 1428, 0.5)
    vs = fs.replay_scene("viewscreen").session()
    assert png._decode_to_rgba(vs.screenshot().screenshot)[:2] == (1280, 2856)


def test_viewscreen_replay_and_errors():
    s = fs.replay_scene("viewscreen").session()
    tree = st.dump_tree_to_dict(s.dump_tree(include_properties=True))
    assert json.loads(json.dumps(tree)) == lf.load("viewscreen", "views_props")
    props = st.get_properties_to_dict(s.get_properties(13))
    assert props["view_id"] == 13 and len(props["properties"]) > 100
    with pytest.raises(fs.SceneError, match="No view found with id 999999"):
        s.get_properties(999999)
    assert st.dump_compose_to_dict(s.dump_compose())["windows"] == []
    assert s.capture_skp().supported is False
    bad = pb.Request(id=7)
    resp = fs.replay_scene("viewscreen").respond(bad)
    assert resp.status == pb.Response.ERROR and resp.error == "No command set in request"
    with pytest.raises(ValueError):
        fs.replay_scene("nope")


def test_behaviour_hook_shape():
    beh = fs.replay_behaviour("launcher")
    req = pb.Request(id=3)
    req.hello.SetInParent()
    delay, resp = beh(req)
    assert delay == 0.0 and resp.id == 3 and resp.hello.agent_version == fs.AGENT_VERSION
    assert fs.replay_behaviour("wide")(req)[1].hello.api_level == 36


def test_existing_shapers_run_on_the_replays():
    """The host shapers (strings, a11y, correlate) consume a SceneSession unchanged."""
    s = fs.replay_scene("launcher").session()
    merged = correlate.inspect_tree(s)
    assert merged["summary"] == lf.load("launcher", "inspect")["summary"]
    vs = fs.replay_scene("viewscreen").session()
    assert correlate.inspect_tree(vs)["summary"] == lf.load("viewscreen", "inspect")["summary"]


def test_mcp_tools_on_the_launcher_replay_meet_the_phase0_targets(mcp):
    """What P0-2 will assert over the harness: brief sizes of the live tools on
    the launcher replay (compact, default args)."""
    mcp.use(fs.replay_scene("launcher"))
    targets = [("dump_compose", {}, 24000),
               ("dump_compose", {"include_slot_table": False}, 6000),
               ("inspect", {}, 13000), ("dump_accessibility", {}, 12500),
               ("dump_tree", {"include_properties": True}, 8000),
               ("get_properties", {"view_id": 82}, 3500)]
    for tool, args, target in targets:
        result = mcp.run(tool, **args)
        assert "error" not in result, result
        brief = output.slim(tool, result, args)
        assert compact(brief) <= target, (tool, args, compact(brief))


def test_screen_pngs_keep_the_fixture_small():
    total = 0
    for root, _, files in os.walk(lf.FIXTURE_DIR):
        total += sum(os.path.getsize(os.path.join(root, f)) for f in files)
    assert total < 1_500_000


@pytest.mark.skipif(importlib.util.find_spec("fakeagent") is None,
                    reason="the offline e2e harness (tests/fakeagent.py) is not on this branch")
def test_wide_scene_over_the_harness_fake_adb(monkeypatch, tmp_path):  # pragma: no cover
    import fakeagent

    import mcp_server

    monkeypatch.setattr(mcp_server, "SESSIONS", mcp_server.SessionCache())
    dev = fakeagent.default_device()
    dev.behaviour = fs.replay_behaviour("wide")
    fakeagent.install(monkeypatch, dev, build_out=str(tmp_path / "build-out"))
    text, is_error = mcp_server._call_tool_text("dump_tree", dict(T))
    assert not is_error and abs(len(text.encode()) - 149_000) <= 0.25 * 149_000
