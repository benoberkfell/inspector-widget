"""Offline tests for inspector_widget.capture.fetch (spec 3.1-3.2, WP C3).

Runs fetch against in-process scenes (tests/fakescenes.py): a two-window scene
shaped like the offline harness's ``default_scene`` (roots 1001 and 2001, one
AndroidComposeView), the recorded launcher and View screens, and the 259-view
wide screen. Every request is logged so the order and flags can be checked.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import zlib

import fakescenes as fs
import pytest

from inspector_widget.capture import fetch as F
from inspector_widget.capture.model import (
    CaptureMeta,
    CaptureOptions,
    Index,
    OpError,
    RawCapture,
)
from inspector_widget.capture.store import CaptureStore
from inspector_widget.proto import view_inspection_pb2 as pb

PX = bytes([10, 20, 30, 255])


# --------------------------------------------------------------------------- scenes
def _b(x, y, w, h):
    return {"layout": {"x": x, "y": y, "w": w, "h": h}}


def _res(name):
    return {"type": "id", "namespace": "com.example", "name": name}


def two_window_scene(*, slots_populated: bool = False) -> fs.SceneData:
    """Activity window 1001 (a TextView and an AndroidComposeView) plus a dialog
    window 2001 (a Button), each with its own screenshot."""
    views = {"roots": [
        {"id": 1001, "class_name": "DecorView", "package_name": "com.android.internal.policy",
         "bounds": _b(0, 0, 360, 640), "children": [
             {"id": 1002, "class_name": "LinearLayout", "package_name": "android.widget",
              "bounds": _b(0, 0, 360, 640), "children": [
                  {"id": 1003, "class_name": "TextView", "package_name": "android.widget",
                   "bounds": _b(16, 24, 328, 40), "resource": _res("title"), "text": "Hello world"},
                  {"id": 1006, "class_name": "AndroidComposeView",
                   "package_name": "androidx.compose.ui.platform", "bounds": _b(0, 160, 360, 400)},
              ]}]},
        {"id": 2001, "class_name": "DecorView", "package_name": "com.android.internal.policy",
         "bounds": _b(40, 200, 280, 200), "children": [
             {"id": 2002, "class_name": "Button", "package_name": "android.widget",
              "bounds": _b(56, 320, 120, 48), "resource": _res("ok"), "text": "OK"}]},
    ], "properties": {
        1003: [{"name": "text", "type": "STRING", "value": "Hello world"},
               {"name": "textSize", "type": "FLOAT", "value": 42.0},
               {"name": "gravity", "type": "GRAVITY", "value": 8388627,
                "label": "center_vertical|start", "source": "Widget.App.Title",
                "resolution_stack": ["Widget.App.Title"]}],
        2002: [{"name": "text", "type": "STRING", "value": "OK"},
               {"name": "enabled", "type": "BOOLEAN", "value": True}],
    }}
    acv = {"id": 1006, "name": "AndroidComposeView", "kind": "COMPOSABLE",
           "bounds": _b(0, 160, 360, 400)}
    on_click = "AccessibilityAction(label=null, action=Function0<Boolean>)"
    sem = {"windows": [{"view_id": 1006, "root": {**acv, "children": [
        {"id": 1, "kind": "SEMANTICS", "bounds": _b(0, 160, 360, 400), "children": [
            {"id": 2, "kind": "SEMANTICS", "bounds": _b(16, 176, 200, 56),
             "attrs": {"Text": "Submit", "Role": "Button", "Focused": "false",
                       "OnClick": on_click}},
            {"id": 5, "kind": "SEMANTICS", "bounds": _b(16, 340, 328, 56),
             "attrs": {"Text": "Wi-Fi", "ToggleableState": "On"}},
        ]}]}}]}
    slots = {"windows": [{"view_id": 1006, "root": {**acv, "children": [
        {"id": 0, "name": "ProbeScreen", "kind": "COMPOSABLE", "source": "MainActivity.kt:31",
         "bounds": _b(0, 160, 360, 400), "children": [
             {"id": 0, "name": "SubmitButton", "kind": "COMPOSABLE",
              "source": "MainActivity.kt:42", "bounds": _b(16, 176, 200, 56)}]}]}}]}

    def node(host, virt, bounds, cls, flags, **kw):
        return {"host_view_id": host, "virtual_id": virt, "bounds": bounds,
                "class_name": f"android.widget.{cls}", "flags": flags, **kw}

    a11y = {"windows": [
        {"root_view_id": 1001, "root": node(1001, -1, _b(0, 0, 360, 640), "FrameLayout",
                                            ["enabled"], children=[
            node(1003, -1, _b(16, 24, 328, 40), "TextView", ["enabled", "visible_to_user"],
                 text="Hello world"),
            node(1006, 2, _b(16, 176, 200, 56), "Button", ["clickable", "enabled"],
                 text="Submit", actions=[{"id": 16}],
                 extras={"androidx.compose.ui.semantics.testTag": "submit"}),
        ])},
        {"root_view_id": 2001, "root": node(2002, -1, _b(56, 320, 120, 48), "Button",
                                            ["clickable", "enabled"], text="OK")},
    ]}
    return fs.SceneData(
        "two", views=fs.views_to_pb(views), a11y=fs.a11y_to_pb(a11y),
        compose_sem=fs.compose_to_pb(sem), compose_slots=fs.compose_to_pb(slots),
        screens={1001: lambda s: fs.rgba_to_screenshot(6, 10, PX * 60, s),
                 2001: lambda s: fs.rgba_to_screenshot(4, 3, bytes([200, 0, 0, 255]) * 12, s)},
        window_ids=[1001, 2001], api_level=36, slots_populated=slots_populated)


class Log:
    """Wraps a scene's dispatcher: logs (command, copy of its message) and lets a
    hook replace or tamper with responses."""

    def __init__(self, scene: fs.SceneData, hook=None) -> None:
        self.scene = scene
        self.calls: list[tuple[str, object]] = []
        self.hook = hook
        self._respond = scene.respond
        scene.respond = self.respond

    def respond(self, req):
        cmd = req.WhichOneof("command")
        msg = type(getattr(req, cmd))()
        msg.CopyFrom(getattr(req, cmd))
        self.calls.append((cmd, msg))
        if self.hook is not None:
            out = self.hook(self, cmd, msg, req)
            if out is not None:
                return out
        return self._respond(req)

    def commands(self) -> list[str]:
        return [c for c, _m in self.calls]

    def count(self, cmd: str) -> int:
        return sum(1 for c, _m in self.calls if c == cmd)


def error(req, text="boom"):
    return pb.Response(id=req.id, status=pb.Response.ERROR, error=text)


def shift_title(scene: fs.SceneData, dx: int = 1) -> None:
    """Move the TextView 1003 (changes the fingerprint)."""
    title = scene.views.roots[0].children[0].children[0]
    assert title.id == 1003
    title.bounds.layout.x += dx


class FakeClock:
    def __init__(self) -> None:
        self.t = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(round(s, 6))
        self.t += s


def run(session, opts=None, **kw) -> RawCapture:
    kw.setdefault("sleep", lambda s: None)
    return F.fetch(session, opts or CaptureOptions(), **kw)


# --------------------------------------------------------------------------- the capture
def test_two_window_capture_contents():
    scene = two_window_scene()
    raw = run(scene.session(pid=4312), compose_generation=0)
    meta = raw.meta
    assert pb.GetWindowsResponse.FromString(raw.windows).root_ids == [1001, 2001]
    views = pb.DumpTreeResponse.FromString(raw.views)
    assert [r.id for r in views.roots] == [1001, 2001]
    assert not views.HasField("screenshot")  # split out into shots
    assert {g.view_id for g in views.properties} == {1003, 2002}
    assert set(raw.shots) == {1001, 2001}
    for root, (w, h) in {1001: (6, 10), 2001: (4, 3)}.items():
        shot = pb.Screenshot.FromString(raw.shots[root])
        assert (shot.width, shot.height, shot.bitmap_type) == (w, h, 2)
        data = zlib.decompress(shot.data)  # still deflated as the agent sent it
        assert len(data) == 9 + w * h * 4
    sem = pb.DumpComposeResponse.FromString(raw.compose_sem)
    assert [w.view_id for w in sem.windows] == [1006]
    assert [c.kind for c in sem.windows[0].root.children] == [pb.ComposeNode.SEMANTICS]
    a11y = pb.DumpA11yResponse.FromString(raw.a11y)
    assert [w.root_view_id for w in a11y.windows] == [1001, 2001]
    assert raw.slots is None and raw.a11y_render is None and raw.skp == {}
    status = {k: v["status"] for k, v in meta.facets.items()}
    assert status == {"windows": "ok", "views": "ok", "props": "ok", "shots": "ok", "compose": "ok",
                      "slots": "unavailable", "a11y": "ok", "skp": "off", "fingerprint": "ok"}
    assert meta.facets["slots"]["reason"] == F.SLOTS_NOT_POPULATED
    assert meta.facets["views"]["bytes"] == len(raw.views)
    assert meta.facets["shots"]["bytes"] == sum(len(b) for b in raw.shots.values())
    assert meta.lineage == ("emulator-5554", "com.oberkfell.a11yprobe")
    assert (meta.pid, meta.api, meta.abi, meta.agent_version) == (4312, 36, "arm64-v8a",
                                                                  "viewspector-0.1")
    assert meta.id == "" and meta.consistency == "settled" and meta.diagnostics == []
    assert meta.device == {"screen": [360, 640], "orientation": "portrait"}
    assert len(meta.fingerprint) == 32 and meta.compose_generation == 0
    assert meta.options == CaptureOptions()


def test_request_order_and_flags():
    scene = two_window_scene()
    log = Log(scene)
    run(scene.session(), CaptureOptions(screenshot_scale=0.5))
    assert log.commands() == ["get_windows", "dump_tree", "screenshot", "dump_compose",
                              "dump_compose", "dump_a11y", "dump_tree", "dump_compose"]
    _, tree = log.calls[1]
    assert (tree.root_id, tree.include_properties, tree.include_resolution_stack,
            tree.include_screenshot, tree.screenshot_scale) == (0, True, False, True, 0.5)
    _, shot = log.calls[2]
    assert (shot.root_id, shot.scale) == (2001, 0.5)
    _, sem = log.calls[3]
    assert (sem.include_semantics, sem.include_slot_table, sem.enable_inspection) == \
        (True, False, False)
    _, slots = log.calls[4]
    assert (slots.include_semantics, slots.include_slot_table, slots.enable_inspection) == \
        (False, True, False)
    _, a11y = log.calls[5]
    assert (a11y.root_id, a11y.include_extras, a11y.include_rendering_info) == (0, True, False)
    _, recheck = log.calls[6]
    assert (recheck.include_properties, recheck.include_screenshot) == (False, False)
    _, recheck_sem = log.calls[7]
    assert (recheck_sem.include_semantics, recheck_sem.include_slot_table,
            recheck_sem.enable_inspection) == (True, False, False)


def test_option_flags_reach_the_wire():
    scene = two_window_scene()
    log = Log(scene)
    raw = run(scene.session(), CaptureOptions(resolution_stack=True, a11y_rendering=True))
    _, tree = log.calls[1]
    assert tree.include_resolution_stack
    assert [m.include_rendering_info for c, m in log.calls if c == "dump_a11y"] == [True]
    views = pb.DumpTreeResponse.FromString(raw.views)
    stacks = [p.resolution_stack for g in views.properties for p in g.properties
              if p.resolution_stack]
    assert stacks  # the fake keeps source/stack only when asked
    assert raw.meta.facets["a11y"]["reason"] == "with rendering info"

    scene2 = two_window_scene()
    log2 = Log(scene2)
    raw2 = run(scene2.session(), CaptureOptions(props=False, screenshot=False, slots="off"))
    assert log2.commands() == ["get_windows", "dump_tree", "dump_compose", "dump_a11y",
                               "dump_tree", "dump_compose"]
    _, tree2 = log2.calls[1]
    assert (tree2.include_properties, tree2.include_screenshot) == (False, False)
    assert raw2.shots == {} and raw2.slots is None
    assert {k: v["status"] for k, v in raw2.meta.facets.items() if v["status"] == "off"} == \
        {"props": "off", "shots": "off", "slots": "off", "skp": "off"}
    assert raw2.meta.facets["shots"]["reason"] == "screenshot=false"
    assert not pb.DumpTreeResponse.FromString(raw2.views).properties


def test_enable_inspection_only_for_slots_enable_and_first():
    scene = two_window_scene()
    log = Log(scene)
    raw = run(scene.session(), CaptureOptions(slots="enable"), compose_generation=3)
    cmd, first = log.calls[0]
    assert cmd == "dump_compose" and first.enable_inspection
    assert (first.include_semantics, first.include_slot_table) == (False, True)
    assert [m.enable_inspection for c, m in log.calls if c == "dump_compose"].count(True) == 1
    assert log.commands()[1:] == ["get_windows", "dump_tree", "screenshot", "dump_compose",
                                  "dump_a11y", "dump_tree", "dump_compose"]
    assert raw.meta.facets["slots"]["status"] == "ok"
    slots = pb.DumpComposeResponse.FromString(raw.slots)
    kids = slots.windows[0].root.children
    assert [c.kind for c in kids] == [pb.ComposeNode.COMPOSABLE]
    assert raw.meta.compose_generation == 4  # ids re-minted by the hot reload
    assert raw.meta.options.slots == "enable"


@pytest.mark.parametrize("populated", [False, True])
def test_slots_if_available_never_enables(populated):
    scene = two_window_scene(slots_populated=populated)
    log = Log(scene)
    raw = run(scene.session(), CaptureOptions(slots="if_available"), compose_generation=2)
    assert not any(m.enable_inspection for c, m in log.calls if c == "dump_compose")
    slot_reqs = [m for c, m in log.calls if c == "dump_compose" and m.include_slot_table]
    assert len(slot_reqs) == 1 and not slot_reqs[0].include_semantics
    assert raw.meta.compose_generation == 2
    assert not scene.inspection_enabled or populated
    if populated:
        assert raw.meta.facets["slots"]["status"] == "ok" and raw.slots
    else:
        assert raw.meta.facets["slots"] == {"status": "unavailable", "ms": 0, "bytes": 0,
                                            "reason": F.SLOTS_NOT_POPULATED}
        assert raw.slots is None


def test_registry_and_plan():
    assert list(F.FACETS) == ["windows", "views", "shots", "compose", "slots", "a11y", "skp"]
    for f in F.FACETS.values():
        assert f.request and f.policy and f.stored
    assert [n for n, f in F.FACETS.items() if f.required] == ["windows", "views"]
    assert F.plan(CaptureOptions()) == ["windows", "views", "shots", "compose", "slots", "a11y"]
    assert F.plan(CaptureOptions(slots="enable", skp=True)) == \
        ["slots", "windows", "views", "shots", "compose", "a11y", "skp"]
    assert F.plan(CaptureOptions(slots="off", screenshot=False)) == ["windows", "views", "compose",
                                                                      "a11y"]
    with pytest.raises(OpError) as e:
        F.fetch(two_window_scene().session(), CaptureOptions(slots="always"))
    assert e.value.code == "bad_args"


# --------------------------------------------------------------------------- consistency
def test_fingerprint_is_stable_and_ignores_focus_and_properties():
    scene = two_window_scene()
    session = scene.session()
    a = run(session)
    b = run(session)
    assert a.meta.fingerprint == b.meta.fingerprint == F.fingerprint_now(session)
    assert F.fingerprint_raw(a) == a.meta.fingerprint
    with_props = session.dump_tree(include_properties=True, include_resolution_stack=True)
    bare = session.dump_tree()
    sem = session.dump_compose(include_slot_table=False)
    assert F.fingerprint_of(with_props, sem) == F.fingerprint_of(bare, sem)
    # Focused is not part of it; Text is
    node = scene.compose_sem.windows[0].root.children[0].children[0]
    st = {e.str: e.id for e in scene.compose_sem.strings.entries}
    focused = next(at for at in node.attrs if at.key == st["Focused"])
    scene.compose_sem.strings.entries.add(id=999, str="true")
    focused.value = 999
    assert F.fingerprint_now(session) == a.meta.fingerprint
    text = next(at for at in node.attrs if at.key == st["Text"])
    text.value = 999
    assert F.fingerprint_now(session) != a.meta.fingerprint
    # window roots, view bounds and view text each count
    for mutate in (lambda s: s.views.roots[1].bounds.layout.__setattr__("y", 9),
                   lambda s: shift_title(s)):
        s2 = two_window_scene()
        before = F.fingerprint_now(s2.session())
        mutate(s2)
        assert F.fingerprint_now(s2.session()) != before


def test_changing_ui_is_unsettled_after_two_retries():
    scene = two_window_scene()

    def always_moving(log, cmd, msg, req):
        if cmd == "dump_tree":
            shift_title(scene)

    log = Log(scene, always_moving)
    clock = FakeClock()
    raw = F.fetch(scene.session(), CaptureOptions(), clock=clock, sleep=clock.sleep)
    assert raw.meta.consistency == "unsettled"
    assert log.count("dump_tree") == 6  # 3 attempts x (fetch + re-check)
    assert log.count("get_windows") == 3
    assert clock.sleeps == [0.15, 0.15]
    assert any("3 attempts" in d for d in raw.meta.diagnostics)
    assert raw.meta.fingerprint == F.fingerprint_raw(raw)  # the stored data's own fingerprint


def test_a_single_change_settles_on_retry():
    scene = two_window_scene()
    seen = []

    def once(log, cmd, msg, req):
        if cmd == "dump_tree":
            seen.append(1)
            if len(seen) == 2:  # the first re-check sees a moved view
                shift_title(scene)

    log = Log(scene, once)
    raw = run(scene.session())
    assert raw.meta.consistency == "settled"
    assert log.count("dump_tree") == 4
    assert raw.meta.diagnostics == ["the UI changed during capture; settled after 2 attempts"]
    assert raw.meta.fingerprint == F.fingerprint_now(scene.session())


def test_retries_never_re_enable_inspection():
    scene = two_window_scene()

    def always_moving(log, cmd, msg, req):
        if cmd == "dump_tree":
            shift_title(scene)

    log = Log(scene, always_moving)
    raw = run(scene.session(), CaptureOptions(slots="enable"), compose_generation=0)
    enables = [m for c, m in log.calls if c == "dump_compose" and m.enable_inspection]
    assert len(enables) == 1 and log.calls[0][0] == "dump_compose"
    assert raw.meta.compose_generation == 1 and raw.meta.facets["slots"]["status"] == "ok"


def test_settle_stops_after_two_equal_fingerprints():
    scene = two_window_scene()
    polls = []

    def moving_twice(log, cmd, msg, req):
        if cmd == "dump_tree":
            polls.append(1)
            if len(polls) in (2, 3):
                shift_title(scene)

    log = Log(scene, moving_twice)
    clock = FakeClock()
    assert F.settle(scene.session(), 800, clock=clock, sleep=clock.sleep) is True
    assert log.count("dump_tree") == 4  # A, B, C, C
    assert clock.sleeps == [0.1, 0.1, 0.1]


def test_settle_gives_up_at_its_budget_and_caps_at_three_seconds():
    scene = two_window_scene()
    Log(scene, lambda log, cmd, msg, req: shift_title(scene) if cmd == "dump_tree" else None)
    clock = FakeClock()
    assert F.settle(scene.session(), 500, clock=clock, sleep=clock.sleep) is False
    assert sum(clock.sleeps) == pytest.approx(0.5)
    clock2 = FakeClock()
    assert F.settle(scene.session(), 60_000, clock=clock2, sleep=clock2.sleep) is False
    assert sum(clock2.sleeps) == pytest.approx(3.0)
    assert F.settle(scene.session(), 0) is True


def test_fetch_settles_first_and_notes_an_unsettled_screen():
    scene = two_window_scene()
    log = Log(scene)
    clock = FakeClock()
    raw = F.fetch(scene.session(), CaptureOptions(settle_ms=300), clock=clock, sleep=clock.sleep)
    assert log.commands()[:4] == ["dump_tree", "dump_compose", "dump_tree", "dump_compose"]
    assert log.commands()[4] == "get_windows"
    assert raw.meta.diagnostics == [] and raw.meta.options.settle_ms == 300

    moving = two_window_scene()
    Log(moving, lambda log, cmd, msg, req: shift_title(moving) if cmd == "dump_tree" else None)
    clock = FakeClock()
    raw = F.fetch(moving.session(), CaptureOptions(settle_ms=300), clock=clock, sleep=clock.sleep)
    assert raw.meta.diagnostics[0] == "settle: the UI was still changing after 300 ms"
    assert raw.meta.consistency == "unsettled"


def test_unchanged_since():
    scene = two_window_scene()
    session = scene.session(pid=4312)
    raw = run(session, wall_clock=lambda: 1000.0)
    raw.meta.id = "c7h2kq"
    assert F.unchanged_since(session, raw.meta, now=lambda: 1095.2) == \
        {"capture": "c7h2kq", "unchanged": True, "age_s": 95}
    assert F.unchanged_since(scene.session(pid=9999), raw.meta) is None  # app restarted
    assert F.unchanged_since(scene.session(package="com.other"), raw.meta) is None
    shift_title(scene)
    assert F.unchanged_since(session, raw.meta) is None
    no_fp = CaptureMeta(id="c1", lineage=raw.meta.lineage)
    assert F.unchanged_since(session, no_fp) is None


# --------------------------------------------------------------------------- failures
def test_an_a11y_error_is_isolated():
    scene = two_window_scene()
    Log(scene, lambda log, cmd, msg, req:
        error(req, "a11y walk failed") if cmd == "dump_a11y" else None)
    raw = run(scene.session())
    facets = raw.meta.facets
    assert facets["a11y"]["status"] == "error" and "a11y walk failed" in facets["a11y"]["reason"]
    assert raw.a11y == b""
    assert all(facets[n]["status"] == "ok"
               for n in ("windows", "views", "props", "shots", "compose", "fingerprint"))
    assert raw.meta.consistency == "settled"


def test_a_required_facet_error_is_an_agent_error():
    scene = two_window_scene()
    Log(scene, lambda log, cmd, msg, req:
        error(req, "no windows") if cmd == "get_windows" else None)
    with pytest.raises(OpError) as e:
        run(scene.session())
    assert e.value.code == "agent_error" and "windows" in e.value.message


def test_a_property_failure_keeps_the_tree():
    scene = two_window_scene()
    log = Log(scene, lambda log, cmd, msg, req:
              error(req, "getter threw") if cmd == "dump_tree" and msg.include_properties else None)
    raw = run(scene.session())
    assert raw.meta.facets["views"]["status"] == "ok"
    assert raw.meta.facets["props"]["status"] == "error"
    assert "getter threw" in raw.meta.facets["props"]["reason"]
    views = pb.DumpTreeResponse.FromString(raw.views)
    assert len(views.roots) == 2 and not views.properties
    assert set(raw.shots) == {1001, 2001}
    assert log.count("dump_tree") == 3


def test_transport_errors_propagate():
    scene = two_window_scene()

    def drop(log, cmd, msg, req):
        if cmd == "dump_a11y":
            raise ConnectionResetError("socket closed")

    Log(scene, drop)
    with pytest.raises(ConnectionResetError):
        run(scene.session())


def test_a_missing_window_screenshot_is_noted():
    scene = two_window_scene()
    Log(scene, lambda log, cmd, msg, req:
        error(req, "window gone") if cmd == "screenshot" and msg.root_id == 2001 else None)
    raw = run(scene.session())
    shots = raw.meta.facets["shots"]
    assert shots["status"] == "ok"
    assert "2001" in shots["reason"] and "window gone" in shots["reason"]
    assert set(raw.shots) == {1001}


def test_first_window_screenshot_falls_back_to_a_screenshot_request():
    scene = two_window_scene()

    def no_inline(log, cmd, msg, req):
        if cmd == "dump_tree" and msg.include_screenshot:
            resp = log._respond(req)
            resp.dump_tree.ClearField("screenshot")
            return resp

    log = Log(scene, no_inline)
    raw = run(scene.session())
    assert [m.root_id for c, m in log.calls if c == "screenshot"] == [1001, 2001]
    assert set(raw.shots) == {1001, 2001}


class SkpSession:
    """Delegates to a SceneSession and answers CaptureSkp with a given version."""

    def __init__(self, inner, version: int, supported: bool = True):
        self._inner = inner
        self.version = version
        self.supported = supported
        self.skp_roots: list[int] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def capture_skp(self, root_id: int = 0):
        self.skp_roots.append(root_id)
        body = b"skiapict" + self.version.to_bytes(4, "little") + b"picture"
        return pb.CaptureSkpResponse(supported=self.supported, skp=body if self.supported else b"",
                                     version=0, error="" if self.supported else "API <= 32")


def test_skp_is_opt_in_and_version_checked():
    scene = two_window_scene()
    raw = run(scene.session(), CaptureOptions(skp=True))  # the fake agent has no SKP
    assert raw.meta.facets["skp"]["status"] == "unsupported" and raw.skp == {}
    ok = SkpSession(two_window_scene().session(), 109)
    raw = run(ok, CaptureOptions(skp=True))
    assert raw.meta.facets["skp"]["status"] == "ok" and set(raw.skp) == {1001, 2001}
    assert ok.skp_roots == [1001, 2001] and F.skp_version(raw.skp[1001]) == 109
    too_new = SkpSession(two_window_scene().session(), 110)
    raw = run(too_new, CaptureOptions(skp=True))
    assert raw.meta.facets["skp"]["status"] == "unsupported" and raw.skp == {}
    assert "v110 > skiaparser's v109" in raw.meta.facets["skp"]["reason"]
    raw = run(SkpSession(two_window_scene().session(), 110), CaptureOptions(skp=True),
              skp_max_version=110)
    assert raw.meta.facets["skp"]["status"] == "ok"
    old = SkpSession(two_window_scene().session(), 0, supported=False)
    raw = run(old, CaptureOptions(skp=True))
    assert raw.meta.facets["skp"]["status"] == "unsupported" and "API <= 32" in \
        raw.meta.facets["skp"]["reason"]
    assert F.skp_version(b"nope") == 0


class MinimalSession:
    """Only the five methods (main's Session has no api_level, abi, capture_skp)."""

    def __init__(self, inner):
        self._s = inner
        self.serial, self.package, self.pid = inner.serial, inner.package, inner.pid

    def get_windows(self):
        return self._s.get_windows()

    def dump_tree(self, **kw):
        return self._s.dump_tree(**kw)

    def screenshot(self, **kw):
        return self._s.screenshot(**kw)

    def dump_compose(self, **kw):
        return self._s.dump_compose(**kw)

    def dump_a11y(self, **kw):
        return self._s.dump_a11y(**kw)


def test_metadata_is_read_with_getattr():
    raw = run(MinimalSession(two_window_scene().session(pid=77)), CaptureOptions(skp=True),
              device={"dpi": 420, "font_scale": 1.3})
    meta = raw.meta
    assert (meta.pid, meta.api, meta.abi, meta.agent_version, meta.agent_build) == \
        (77, None, None, None, None)
    assert meta.facets["skp"] == {"status": "unsupported", "ms": 0, "bytes": 0,
                                  "reason": "the session has no capture_skp"}
    assert meta.device == {"dpi": 420, "font_scale": 1.3, "screen": [360, 640],
                           "orientation": "portrait"}


def test_timing_uses_the_injected_clocks():
    scene = two_window_scene()
    clock = FakeClock()

    def slow(log, cmd, msg, req):
        clock.t += 0.25 if cmd == "dump_a11y" else 0.01

    Log(scene, slow)
    raw = F.fetch(scene.session(), CaptureOptions(), clock=clock, sleep=clock.sleep,
                  wall_clock=lambda: 1_790_000_000.0)
    assert raw.meta.created_at == 1_790_000_000.0
    assert raw.meta.facets["a11y"]["ms"] == 250
    assert raw.meta.facets["fingerprint"]["ms"] == 20
    assert raw.meta.took_ms == 8 * 10 + 240


# --------------------------------------------------------------------------- real data
@pytest.mark.parametrize("name", ["launcher", "viewscreen", "wide"])
def test_real_replays(name):
    scene = fs.scene(name)
    log = Log(scene)
    session = scene.session()
    t0 = time.perf_counter()
    raw = run(session)
    assert time.perf_counter() - t0 < 1.0
    meta = raw.meta
    assert meta.consistency == "settled"
    assert meta.fingerprint == F.fingerprint_raw(raw) == F.fingerprint_now(session)
    views = pb.DumpTreeResponse.FromString(raw.views)
    assert not views.HasField("screenshot") and views.properties
    assert set(raw.shots) == {views.roots[0].id}
    assert zlib.decompress(pb.Screenshot.FromString(raw.shots[views.roots[0].id]).data)
    assert log.commands().count("screenshot") == 0  # one window: the DumpTree shot
    expect_slots = "ok" if name == "launcher" else "unavailable"
    assert meta.facets["slots"]["status"] == expect_slots
    if name == "viewscreen":
        assert meta.facets["compose"]["reason"] == F.NO_COMPOSE
        assert meta.facets["slots"]["reason"] == F.NO_COMPOSE


def test_a_fetched_capture_round_trips_through_the_store(tmp_path):
    scene = two_window_scene()
    raw = run(scene.session())
    store = CaptureStore(root=str(tmp_path / "store"), persist=True, durable=False)
    cid = store.publish(raw, Index(), {})
    loaded = store.load(cid)
    assert loaded.raw_capture().files() == raw.files()
    assert loaded.shot_roots() == [1001, 2001]
    assert loaded.meta.facets == raw.meta.facets
    assert loaded.meta.fingerprint == raw.meta.fingerprint
    assert F.fingerprint_raw(loaded.raw_capture()) == raw.meta.fingerprint


def test_importing_fetch_does_not_import_protobuf():
    host = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = ("import sys; import inspector_widget.capture.fetch; "
            "print(any(m.startswith(('google.protobuf', 'PIL')) for m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         cwd=host, timeout=60)
    assert out.stdout.strip() == "False"


def test_a_failed_enable_still_bumps_the_generation():
    scene = two_window_scene()
    Log(scene, lambda log, cmd, msg, req:
        error(req, "hot reload threw") if cmd == "dump_compose" and msg.enable_inspection else None)
    raw = run(scene.session(), CaptureOptions(slots="enable"), compose_generation=5)
    assert raw.meta.facets["slots"]["status"] == "error"
    assert "hot reload threw" in raw.meta.facets["slots"]["reason"]
    assert raw.meta.compose_generation == 6
    assert raw.meta.facets["views"]["status"] == "ok"
