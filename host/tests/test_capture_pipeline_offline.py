"""The capture pipeline end to end, offline: no device and no ops/surface wiring.

Every capture here goes the way the ops layer (S1) will take it, but through the
library modules only:

    fetch (C3) over an in-process scene session
      -> build_index (C4), analyze (C7) on the key-space index, prev.index()
      -> under the store's refs lock: refs.assign against the lineage's latest (C5),
         apply_refs, store.publish (C2)
      -> store.load

and is then walked with outline / find / node (C6), lint_view (C7), crop /
overlay / inline / pixel_diff (C9) and diff (C8). The scenes are the F1 fakes:
the 259-view wide screen, the recorded launcher and View-screen replays, and C4's
synthetic mixed hierarchy (a RecyclerView of ComposeView cells, an AndroidView
inside Compose holding a nested ComposeView, and a dialog window) served through
the same SceneData dispatcher.

Sizes are compact JSON bytes (``output.dumps``) and are checked against the spec's
budgets: section 7 (every default response within its tool's default
``max_bytes``), the section 13.2 per-scene targets, and the section 8 workflow
totals (tokens estimated at 3.5 B each, plus the image token estimate, within
+25%). ``capture()`` and ``captures(list)`` responses are rendered by S1; here
:meth:`Pipeline.summary` and :meth:`Pipeline.captures_list` stand in for them with
the fields of the spec examples (5.3, 5.4), so their sizes are estimates.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable, Iterator
from typing import Any

import capture_scenes as cs
import fakescenes as fs
import pytest
from loaded_fakes import screen_of

from inspector_widget.capture import analyzers, diff, fetch, images, index, lines, query, refs
from inspector_widget.capture.model import CaptureOptions, Index, OpError, UNode
from inspector_widget.capture.store import CaptureStore, LoadedCapture
from inspector_widget.output import dumps
from inspector_widget.proto import view_inspection_pb2 as pb

TOKEN_BYTES = 3.5  # spec section 8: compact JSON, about 3.5 B per token
PACKAGE = "com.oberkfell.a11yprobe"

#: Spec section 7: each tool's default max_bytes (a default response never exceeds it).
DEFAULT_BUDGET = {"capture": 3000, "outline": 6000, "find": 3000, "node": 3000,
                  "node_batch": 6000, "image": 600, "lint": 4000, "diff": 4000,
                  "captures": 2000}


def ui(ix: Index) -> list[UNode]:
    """The ui tree's nodes in pre-order (windows by z)."""
    return [n for n, _depth in ix.walk("ui")]


def nbytes(obj: Any) -> int:
    return len(dumps(obj).encode("utf-8"))


def tokens(*objs: Any, image_tokens: int = 0) -> int:
    return round(sum(nbytes(o) for o in objs) / TOKEN_BYTES) + image_tokens


# --------------------------------------------------------------------------- #
# The pipeline (what S1's capture() does, minus session resolution and rendering)
# --------------------------------------------------------------------------- #
class Pipeline:
    """Capture, publish and query through the library modules only."""

    def __init__(self, root: str) -> None:
        self.now = 1_790_000_000.0  # a fake wall clock: captures are 2 s apart
        self.store = CaptureStore(root=root, persist=True, clock=self.clock)

    def clock(self) -> float:
        return self.now

    def tick(self, s: float) -> None:
        self.now += s

    # ---- capture ------------------------------------------------------- #
    def _latest(self, lineage: tuple[str, str]) -> LoadedCapture | None:
        st = self.store.lineage_state(*lineage)
        if st.latest and self.store.exists(st.latest):
            return self.store.load(st.latest)
        return None

    def capture(self, session: Any, opts: CaptureOptions | None = None, *,
                label: str | None = None, device: dict | None = None) -> LoadedCapture:
        lineage = (session.serial, session.package)
        prev = self._latest(lineage)
        gen = 0
        if prev is not None and prev.meta.pid == getattr(session, "pid", None):
            gen = prev.meta.compose_generation  # the lineage's generation for this pid
        raw = fetch.fetch(session, opts, compose_generation=gen, device=device,
                          sleep=lambda _s: None, wall_clock=self.clock)
        ix = index.build_index(raw)
        # Outside the store lock: analyze the key-space index (lint="full" runs
        # contrast, ~4 s; apply_refs carries issues, stops, reading and evidence
        # links over) and hydrate prev's index (a rebuild can be slow too).
        analyzers.analyze(ix, raw, lint=raw.meta.options.lint)
        pix = prev.index() if prev is not None else None
        with self.store.refs_lock():  # held for assign + apply + publish only
            latest = self.store.lineage_state(*lineage).latest
            if latest != (prev.id if prev is not None else None):
                prev = self._latest(lineage)  # another process published meanwhile
                pix = prev.index() if prev is not None else None
            same_pid, same_gen = refs.identity_flags(raw.meta, pix.meta if pix else None)
            refmap, tomb = refs.assign(ix, pix, same_pid=same_pid, same_generation=same_gen,
                                       alloc=self.store.next_refs)
            ix = index.apply_refs(ix, refmap)
            raw.meta.label = label
            cid = self.store.publish(raw, ix, refmap, tomb=tomb)
        self.tick(2.0)
        return self.store.load(cid)

    # ---- accessors the ops layer injects --------------------------------- #
    @staticmethod
    def props_fn(loaded: LoadedCapture) -> Callable[[UNode], dict | None]:
        def get(n: UNode) -> dict | None:
            if n.kind != "view" or "view" not in n.ids:
                return None
            return loaded.props(int(n.ids["view"]))
        return get

    def tomb(self, loaded: LoadedCapture) -> dict:
        return self.store.lineage_state(*loaded.meta.lineage).tomb

    def outline(self, loaded: LoadedCapture, **kw: Any) -> dict:
        return query.outline(loaded.index(), loaded=loaded, tomb=self.tomb(loaded), **kw)

    def find(self, loaded: LoadedCapture, **kw: Any) -> dict:
        return query.find(loaded.index(), loaded=loaded, tomb=self.tomb(loaded), **kw)

    def node(self, loaded: LoadedCapture, sels: Any, **kw: Any) -> dict:
        return query.node(loaded.index(), loaded, sels, tomb=self.tomb(loaded), **kw)

    def lint(self, loaded: LoadedCapture, **kw: Any) -> dict:
        return analyzers.lint_view(loaded.index(), loaded, **kw)

    def image(self, loaded: LoadedCapture, ref: str | None = None, *, overlay: str = "none",
              **kw: Any) -> dict:
        ix = loaded.index()
        if overlay == "none" and ref is not None:
            return images.crop(loaded, query.resolve_selector(ix, ref), **kw)
        return images.overlay(loaded, ix, overlay, ref=ref, **kw)

    def diff(self, a: LoadedCapture, b: LoadedCapture, **kw: Any) -> dict:
        ia, ib = a.index(), b.index()

        def preview(ix: Index, root: str | None) -> list[str]:
            return query.outline(ix, root=root, depth=2, max_lines=20)["lines"]

        def pixels(xa: Index, xb: Index, changed: Any) -> dict:
            return images.pixel_diff(a, b, xa, xb, refs=changed)

        return diff.diff(ia, ib, props_a=self.props_fn(a), props_b=self.props_fn(b),
                         resolve=query.resolve_selector, preview=preview, pixel_diff=pixels,
                         **kw)

    # ---- stand-ins for S1's renderers ------------------------------------- #
    def summary(self, loaded: LoadedCapture, *, preview_lines: int = 20,
                on_screen: int = 3) -> dict:
        """The capture() response of spec 5.3: id, session, device, facets,
        windows, lint and issue one-liners, a depth-2 preview, the labelled text
        hidden under the preview's ``+N`` (``on_screen``) and ``next``."""
        ix, m = loaded.index(), loaded.meta
        pv = query.outline(ix, depth=2, max_lines=preview_lines)
        shown = set(re.findall(r"\bn\d+\b", " ".join(pv["lines"])))
        hidden = [n for n in ui(ix) if n.kind != "slot" and n.label and n.b
                  and n.b[2] > 0 and n.b[3] > 0 and n.ref not in shown
                  and n.stop is not None]
        facets: dict[str, Any] = {
            "views": sum(1 for n in ix.nodes.values() if n.kind == "view"),
            "compose": sum(1 for n in ix.nodes.values() if n.kind == "compose"),
            "a11y": sum(1 for n in ix.nodes.values() if "a11y" in n.ids),
            "slots": sum(1 for n in ix.nodes.values() if n.kind == "slot")
            if m.facet_status("slots") == "ok" else (m.facets.get("slots") or {}).get("reason")
            or m.facet_status("slots"),
            "shots": len(loaded.shot_roots()),
            "skp": m.facet_status("skp"),
        }
        dev = m.device or {}
        screen = dev.get("screen") or [0, 0]
        out: dict[str, Any] = {
            "capture": loaded.id, "session": f"{m.serial}/{m.package}", "pid": m.pid,
            "device": f"API {m.api} {screen[0]}x{screen[1]} {dev.get('dpi')}dpi "
                      f"font {dev.get('font_scale', 1.0)}",
            "took_ms": m.took_ms, "consistency": m.consistency, "facets": facets,
            "windows": [f"{lines.crumb(w)} {lines.fmt_bounds(w.b)} z{w.z}"
                        for w in ix.windows()],
            **analyzers.lint_summary(ix), "outline": pv["lines"]}
        if hidden:
            out["on_screen"] = [f'{n.ref} {lines.jstr(lines.cut(n.label, 48))}'
                                for n in hidden[:on_screen]]
            if len(hidden) > on_screen:
                out["on_screen"].append(f"…{len(hidden) - on_screen} more: outline()")
        issue_ref = next((n.ref for n in ui(ix) if n.issues), None)
        out["next"] = query.next_hints(["outline()" if hidden else None, "lint()",
                                        query.call("node", issue_ref) if issue_ref else None])
        return out

    def captures_list(self, limit: int = 20) -> dict:
        """The captures(action="list") response of spec 5.4."""
        rows = []
        for m in self.store.list(limit=limit):
            lc = self.store.load(m.id)
            lab = f" @{m.label}" if m.label else ""
            pin = " pinned" if m.pinned else ""
            rows.append(f"{m.id}{lab} {m.serial}/{m.package} {lc.node_count()} nodes "
                        f"{lc.nbytes() / 1e6:.1f}MB {round(lc.age_s())}s ago{pin}")
        s = self.store.summary()
        return {"lines": rows, "store": f"{self.store.root} "
                                        f"{s.get('bytes', 0) / 1e6:.1f}MB ttl 24h"}


# --------------------------------------------------------------------------- #
# Scenes
# --------------------------------------------------------------------------- #
MAIN_BG = (245, 245, 245)
DIALOG_BG = (255, 255, 255)
OK_BLUE = (30, 60, 200)
DELETE_RED = (200, 40, 40)


def mixed_scene_data() -> fs.SceneData:
    """C4's mixed hierarchy served by the F1 dispatcher, with per-window screenshots
    (window-relative, like the agent's): the main window paints each cell's Delete
    button red, the dialog paints its OK button blue."""
    windows = cs.mixed_windows()
    raw = cs.mixed_scene()
    views = pb.DumpTreeResponse.FromString(raw.views)
    screens = {}
    for w in windows:
        paint = []
        for v in w.walk():
            if v.sem is not None:
                stack = [v.sem]
                while stack:
                    c = stack.pop()
                    if c.attrs.get("Text") == "Delete":
                        paint.append((c.b, DELETE_RED))
                    if c.attrs.get("Text") == "OK":
                        paint.append((c.b, OK_BLUE))
                    stack.extend(c.children)
        bg = MAIN_BG if w.id == 1 else DIALOG_BG
        screens[w.id] = (lambda scale, w=w, bg=bg, paint=paint:
                         screen_of(w.b, bg, paint, scale))
    return fs.SceneData(
        "mixed", views=views, a11y=pb.DumpA11yResponse.FromString(raw.a11y),
        compose_sem=pb.DumpComposeResponse.FromString(raw.compose_sem),
        compose_slots=pb.DumpComposeResponse.FromString(raw.slots) if raw.slots else None,
        window_ids=[w.id for w in windows], screens=screens, api_level=36,
        slots_populated=True)


#: name -> (scene factory, serial, pid, device)
SCENES: dict[str, tuple[Callable[[], fs.SceneData], str, int, dict]] = {
    "launcher": (lambda: fs.replay_scene("launcher"), "emulator-5554", 4312,
                 {"dpi": 480, "font_scale": 1.0}),
    "viewscreen": (lambda: fs.replay_scene("viewscreen"), "emulator-5556", 4313,
                   {"dpi": 480, "font_scale": 1.0}),
    "wide": (fs.wide_scene, "emulator-5558", 4314, {"dpi": 420, "font_scale": 1.0}),
    "mixed": (mixed_scene_data, "emulator-5560", 4315, {"dpi": 420, "font_scale": 1.0}),
}


class Captured:
    def __init__(self, pipe: Pipeline, scene: fs.SceneData, loaded: LoadedCapture,
                 serial: str, pid: int, device: dict) -> None:
        self.pipe, self.scene, self.loaded = pipe, scene, loaded
        self.serial, self.pid, self.device = serial, pid, device

    @property
    def ix(self) -> Index:
        return self.loaded.index()

    def session(self, pid: int | None = None) -> fs.SceneSession:
        return self.scene.session(serial=self.serial, package=PACKAGE, pid=pid or self.pid)

    def recapture(self, *, pid: int | None = None, label: str | None = None,
                  **opts: Any) -> LoadedCapture:
        return self.pipe.capture(self.session(pid), CaptureOptions(**opts) if opts else None,
                                 label=label, device=self.device)

    def ref(self, sel: str) -> str:
        return query.resolve_selector(self.ix, sel).id


def take(pipe: Pipeline, name: str, *, label: str | None = None) -> Captured:
    make, serial, pid, device = SCENES[name]
    scene = make()
    loaded = pipe.capture(scene.session(serial=serial, package=PACKAGE, pid=pid),
                          label=label, device=device)
    return Captured(pipe, scene, loaded, serial, pid, device)


@pytest.fixture(scope="module")
def world() -> Iterator[dict[str, Captured]]:
    """One store holding one capture of every scene (each its own lineage)."""
    with tempfile.TemporaryDirectory(prefix="iw-pipeline-") as root:
        pipe = Pipeline(root)
        yield {name: take(pipe, name) for name in SCENES}
        pipe.store.close()


@pytest.fixture()
def pipe(tmp_path: Any) -> Iterator[Pipeline]:
    p = Pipeline(str(tmp_path / "store"))
    yield p
    p.store.close()


def view_lines(result: dict) -> list[str]:
    return list(result.get("lines") or [])


def refs_in(lines_: list[str]) -> list[str]:
    return [r for line in lines_ for r in re.findall(r"(?:^|[\s>])(n\d+)\b", line)]


# --------------------------------------------------------------------------- #
# The pipeline itself
# --------------------------------------------------------------------------- #
def test_every_scene_publishes_a_settled_capture(world: dict[str, Captured]) -> None:
    for name, c in world.items():
        m = c.loaded.meta
        assert m.consistency == "settled", name
        assert m.id == c.loaded.id and m.lineage == (c.serial, PACKAGE)
        for facet in ("windows", "views", "props", "compose", "a11y", "shots"):
            assert m.facet_status(facet) in ("ok", "unavailable"), (name, facet, m.facets)
        ix = c.ix
        # ref space, every node has a ref, and every ref is in the refmap on disk
        assert all(n.ref == nid and query.is_ref(nid) for nid, n in ix.nodes.items())
        assert set(c.loaded.refmap().values()) == set(ix.nodes)
        # a fresh ref's `since` names the capture that minted it
        assert {n.since for n in ix.nodes.values() if n.match == "new"} == {c.loaded.id}
        # one screenshot per window, cut from that window
        assert c.loaded.shot_roots() == sorted(int(w.ids["view"]) for w in ix.windows())


def test_the_index_round_trips_through_the_store(world: dict[str, Captured]) -> None:
    """The published index.jsonl.gz (not the memory cache) reads back identical."""
    for c in world.values():
        fresh = CaptureStore(root=c.pipe.store.root, persist=True)
        again = fresh.load(c.loaded.id).index()
        assert again.nodes.keys() == c.ix.nodes.keys()
        assert [n.issues for n in again.nodes.values()] == [n.issues for n in c.ix.nodes.values()]
        assert again.reading == c.ix.reading
        assert query.outline(again)["lines"] == query.outline(c.ix)["lines"]


def test_a_lost_index_is_rebuilt_with_its_analysis(pipe: Pipeline) -> None:
    c = take(pipe, "launcher")
    want = query.outline(c.ix)["lines"]
    os.remove(os.path.join(c.loaded.path, "index.jsonl.gz"))
    fresh = CaptureStore(root=pipe.store.root, persist=True)
    ix = fresh.load(c.loaded.id).index()
    assert query.outline(ix)["lines"] == want  # issues, stops and refs all back
    assert ix.reading == c.ix.reading


# --------------------------------------------------------------------------- #
# Launcher (real replay, pre-ID1 ids)
# --------------------------------------------------------------------------- #
def test_launcher_walk(world: dict[str, Captured]) -> None:
    c = world["launcher"]
    p, lc = c.pipe, c.loaded
    assert "a11y ids not unique (agent ID1); a11y facets matched by bounds" in c.ix.diagnostics

    summary = p.summary(lc)
    assert nbytes(summary) <= 2500
    assert summary["lint"] == "14 warn: 12 role, 1 state, 1 touch_target (contrast not run)"
    heading = c.ref("@launch_heading")
    assert summary["issues"] == f"1 clipped: {heading}"

    out = p.outline(lc)
    assert nbytes(out) <= 2500 and "truncated" not in out
    listing = p.outline(lc, root="@launcher_list")
    assert nbytes(listing) <= 2000
    assert listing["shown"] == listing["total"] == 13  # the list and its 12 rows

    # the slot tree is one tree: subcompositions (the list's items, the Scaffold's
    # TopAppBar and content) are grafted under their groups, rows in screen order
    slots = p.outline(lc, view="slots")
    assert nbytes(slots) <= 6000
    assert [ln.split(" [")[0].strip().split(" ", 1)[1] for ln in slots["lines"]] == [
        "MaterialTheme", "AppRoot", "Scaffold", "TopAppBar", "Box"]
    walk = p.outline(lc, view="slots", depth=99, max_children=1000, max_lines=400)
    assert nbytes(walk) <= 6000 and walk["total"] == 56  # spec 5.5: 56 app lines
    assert all("src=MainActivity.kt:" in line for line in walk["lines"])
    lazy = p.find(lc, in_="slots", type="LazyColumn")["lines"][0].split()[0]
    rows = p.outline(lc, root=lazy, depth=1, max_children=1000)["lines"][1:]
    tops = [int(re.search(r"\[\d+,(\d+) ", ln).group(1)) for ln in rows]
    assert len(rows) == 24 and tops == sorted(tops)  # 12 ListItems + dividers, top down

    reading = p.outline(lc, view="reading")
    assert nbytes(reading) <= 2000 and reading["total"] == 13  # 13 TalkBack stops
    assert reading["lines"][0].startswith("1. ") and '"A11yProbe"' in reading["lines"][0]

    found = p.find(lc, text="state", flags=["click"])
    assert nbytes(found) <= 600 and found["total"] == 2
    assert all(" in " in line and "@launcher_list" in line for line in found["lines"])

    node = p.node(lc, heading)
    assert nbytes(node) <= 1500
    assert node["sel"] == "@launch_heading" and node["b"] == [0, 2757, 1280, 27]
    assert node["layout"]["declared"] == [0, 2757, 1280, 216]
    assert node["layout"]["visible"] == 0.125
    assert node["tap_xy"] == [640, 2770]
    assert any("likely false positive" in i for i in node["issues"])
    assert [s.split(" src=")[1].split()[0] for s in node["compose"]["slots"]] == [
        "MainActivity.kt:150", "MainActivity.kt:151", "MainActivity.kt:152"]

    lint = p.lint(lc)
    assert nbytes(lint) <= 1200 and lint["counts"] == {"error": 0, "warn": 14, "info": 0}

    crop = p.image(lc, heading)
    assert nbytes(crop) <= 400 and crop["px"][1] > 0 and os.path.exists(crop["path"])
    assert crop["path"].startswith(lc.path + os.sep + "img" + os.sep)


def test_launcher_lint_overlay_marks_findings_by_ref(world: dict[str, Captured]) -> None:
    c = world["launcher"]
    ov = c.pipe.image(c.loaded, overlay="lint")
    assert nbytes(ov) <= DEFAULT_BUDGET["image"]
    assert os.path.exists(ov["path"]) and ov["marks"] >= 13


# --------------------------------------------------------------------------- #
# View screen (real replay)
# --------------------------------------------------------------------------- #
def test_viewscreen_walk(world: dict[str, Captured]) -> None:
    c = world["viewscreen"]
    p, lc = c.pipe, c.loaded
    out = p.outline(lc)
    assert nbytes(out) <= 3000 and "truncated" not in out
    views = [n.id for n in c.ix.nodes.values() if n.kind == "view"]
    assert len(views) == 40
    # all 40 Views: 38 on lines (some share a chain line), 2 ViewStubs counted hidden
    shown = set(refs_in(out["lines"])) & set(views)
    assert len(shown) == 38 and out["hidden"] == {"zero_size": 2}
    assert {c.ix.nodes[v].type for v in set(views) - shown} == {"ViewStub", "ViewStubCompat"}

    node = p.node(lc, "#badSwitch", props="nondefault")
    assert nbytes(node) <= 1200
    assert node["tap_xy"] == [242, 1254]
    assert node["props"]["mode"] == "nondefault"
    values = node["props"]["values"]
    assert values["text"] == "Notifications" and values["checked"] is True
    # theme-wide text colours are defaults once rare classes use their family group
    assert "textColorHint" not in values and "textColorHighlight" not in values

    reading = p.outline(lc, view="reading")
    assert nbytes(reading) <= 3000
    assert any("#badImageButton" in line for line in reading["lines"])


# --------------------------------------------------------------------------- #
# The 259-view wide screen
# --------------------------------------------------------------------------- #
def test_wide_walk(world: dict[str, Captured]) -> None:
    c = world["wide"]
    p, lc = c.pipe, c.loaded
    assert nbytes(p.summary(lc)) <= 3000

    # the default outline pages hold exactly the 259 Views, each page <= 6,000 B
    seen: list[str] = []
    page = p.outline(lc)
    pages = 1
    while True:
        assert nbytes(page) <= 6000
        seen.extend(line.split()[0] for line in page["lines"])
        cursor = (page.get("truncated") or {}).get("cursor")
        if not cursor:
            break
        page = p.outline(lc, cursor=cursor)
        pages += 1
    views = [n.id for n in ui(c.ix) if n.kind == "view"]
    assert seen == views and len(views) == 259 and pages <= 4

    found = p.find(lc, text="Label 4", limit=20)
    assert nbytes(found) <= 3000 and found["total"] == 11

    node = p.node(lc, "#view_47", props="nondefault")
    assert nbytes(node) <= 1500
    assert node["props"]["values"]["text"] == "Hello world"

    third = c.ref("#view_3")
    sub = p.outline(lc, root=third, depth=1)
    assert nbytes(sub) <= 1200 and sub["total"] == 7


# --------------------------------------------------------------------------- #
# Mixed hierarchy (synthetic, post-ID1 ids)
# --------------------------------------------------------------------------- #
def test_mixed_walk(world: dict[str, Captured]) -> None:
    c = world["mixed"]
    p, lc, ix = c.pipe, c.loaded, c.ix
    wins = ix.windows()
    assert [w.z for w in wins] == [0, 1]  # the dialog is its own window, z1
    summary = p.summary(lc)
    assert nbytes(summary) <= 3000 and len(summary["windows"]) == 2

    hits = p.find(lc, text="Delete", flags=["click"], within="#feed")
    assert hits["total"] == 4 and nbytes(hits) <= 1200  # three Compose cells + a View cell
    # composite semantics keys keep the three cells' equal ids apart
    keys = [ix.nodes[r].key for r in refs_in(hits["lines"]) if r in ix.nodes
            and ix.nodes[r].kind == "compose" and ix.nodes[r].label == "Delete"]
    assert len(set(keys)) == len(keys) == 3
    assert all(" in " in line for line in hits["lines"])  # breadcrumbs name the cell

    lint = p.lint(lc, within="#feed")
    assert nbytes(lint) <= DEFAULT_BUDGET["lint"]

    ok = query.find(ix, text="OK", flags=["click"])
    assert ok["total"] == 1
    ok_ref = ok["lines"][0].split()[0]
    assert ix.nodes[ok_ref].window == wins[1].id
    node = p.node(lc, ok_ref)
    assert nbytes(node) <= 1500 and "window" in node  # multi-window: names its window

    # the crop comes from the dialog's own screenshot: its pixels are the OK blue
    crop = p.image(lc, ok_ref, pad=0)
    assert crop["window"] == wins[1].id and nbytes(crop) <= 400
    with open(crop["path"], "rb") as f:
        w, h, rgba = images.decode_png(f.read())
    px = rgba[((h // 2) * w + w // 2) * 4:((h // 2) * w + w // 2) * 4 + 3]
    assert tuple(px) == OK_BLUE

    # the AndroidView inside Compose is re-parented under its semantics node
    interop = [n for n in ix.nodes.values() if "interop" in n.flags]
    assert interop and all(n.conf.get("ui") == "inferred" for n in interop)


# --------------------------------------------------------------------------- #
# Section 7: every default response within its tool's default budget
# --------------------------------------------------------------------------- #
def _default_calls(c: Captured) -> list[tuple[str, Callable[[], dict]]]:
    p, lc, ix = c.pipe, c.loaded, c.ix
    first_issue = next((n.id for n in ui(ix) if n.issues), None)
    some = first_issue or next(n.id for n in ui(ix) if n.b and n.b[2] > 0 and n.b[3] > 0)
    batch = [n.id for n in ui(ix)][:10]
    calls: list[tuple[str, Callable[[], dict]]] = [
        ("capture", lambda: p.summary(lc)),
        ("outline", lambda: p.outline(lc)),
        ("outline", lambda: p.outline(lc, view="views")),
        ("outline", lambda: p.outline(lc, view="compose")),
        ("outline", lambda: p.outline(lc, view="a11y")),
        ("outline", lambda: p.outline(lc, view="reading")),
        ("outline", lambda: p.outline(lc, detail="all", depth=99)),
        ("find", lambda: p.find(lc, flags=["click"])),
        ("find", lambda: p.find(lc, in_="all")),
        ("node", lambda: p.node(lc, some)),
        ("node", lambda: p.node(lc, some, facets="all", props="all", params="raw")),
        ("node_batch", lambda: p.node(lc, batch, facets="all")),
        ("image", lambda: p.image(lc, some)),
        ("image", lambda: p.image(lc, overlay="marks")),
        ("lint", lambda: p.lint(lc)),
        ("lint", lambda: p.lint(lc, group="none")),
        ("lint", lambda: p.lint(lc, rules=["render."], group="node")),
        ("captures", lambda: p.captures_list()),
    ]
    if c.loaded.meta.facet_status("slots") == "ok":
        calls.append(("outline", lambda: p.outline(lc, view="slots", origin="all", depth=99)))
    return calls


@pytest.mark.parametrize("scene", list(SCENES))
def test_every_default_response_is_within_budget(world: dict[str, Captured], scene: str
                                                 ) -> None:
    c = world[scene]
    for tool, call in _default_calls(c):
        r = call()
        assert nbytes(r) <= DEFAULT_BUDGET[tool], (scene, tool, nbytes(r))
        if tool in ("outline", "find") and r.get("truncated"):
            assert r["truncated"]["cursor"].startswith(c.loaded.id + ":")


# --------------------------------------------------------------------------- #
# Recapture: carry-over, diff, tombstones, if_changed_since
# --------------------------------------------------------------------------- #
def _a11y_node(scene: fs.SceneData, rid_suffix: str) -> Any:
    strings = {e.id: e.str for e in scene.a11y.strings.entries}
    stack = [w.root for w in scene.a11y.windows]
    while stack:
        n = stack.pop()
        if strings.get(n.view_id_resource_name, "").endswith(rid_suffix):
            return n
        stack.extend(n.children)
    raise AssertionError(rid_suffix)


def _intern(strings: Any, s: str) -> int:
    for e in strings.entries:
        if e.str == s:
            return e.id
    new = max((e.id for e in strings.entries), default=0) + 1
    strings.entries.add(id=new, str=s)
    return new


def tap_switch(scene: fs.SceneData, rid: str = "badSwitch") -> None:
    """What tapping a Switch changes on the device: its a11y checked state and
    state description, and the View's ``checked`` property."""
    a = _a11y_node(scene, "id/" + rid)
    a.checked = not a.checked
    a.checked_state = 1 if a.checked else 0
    a.state_description = _intern(scene.a11y.strings, "ON" if a.checked else "OFF")
    names = {e.id: e.str for e in scene.views.strings.entries}
    stack = list(scene.views.roots)
    udid = None
    while stack and udid is None:
        v = stack.pop()
        if names.get(v.resource.name) == rid:
            udid = v.id
        stack.extend(v.children)
    for g in scene.views.properties:
        if g.view_id == udid:
            for p in g.properties:
                if names.get(p.name) == "checked":
                    p.int32_value = 1 if a.checked else 0
            return
    raise AssertionError("no property group for " + rid)


def test_recapture_after_a_tap_diffs_exactly_the_tap(pipe: Pipeline) -> None:
    c = take(pipe, "viewscreen", label="before")
    before = c.loaded
    switch = c.ref("#badSwitch")
    node = pipe.node(before, "#badSwitch")
    assert node["tap_xy"] == [242, 1254]

    tap_switch(c.scene)
    after = c.recapture()
    ia, ib = before.index(), after.index()
    # refs carried: same pid, same generation, so every node matches by device id
    assert set(ib.nodes) == set(ia.nodes)
    assert {n.match for n in ib.nodes.values()} == {"id"}
    assert all(n.since == before.id for n in ib.nodes.values())
    assert after.meta.prev == before.id
    assert "checked" not in ib.nodes[switch].flags

    d = pipe.diff(before, after)
    assert nbytes(d) <= 600
    assert d["a"] == f"{before.id} @before" and d["b"] == after.id and d["same_pid"] is True
    assert d["summary"]["changed"] == 1 and d["summary"]["unchanged"] == 39
    assert d["lines"] == [f'~ {switch} Switch #badSwitch "Notifications": checked -> unchecked',
                          f'~ {switch} state "ON" -> "OFF"',
                          f"~ {switch} props checked true -> false"]
    assert d["issues"] == {"resolved": [], "new": []}
    assert any(f'ref="{switch}"' in h for h in d["next"])

    # the same diff through labels, as the store resolves them for both surfaces
    assert pipe.store.resolve("before", (c.serial, PACKAGE)) == before.id
    assert pipe.store.resolve("prev", (c.serial, PACKAGE)) == before.id
    assert pipe.store.resolve("latest", (c.serial, PACKAGE)) == after.id

    # a pixel diff comes back with the diff when asked for
    d_img = pipe.diff(before, after, image=True)
    assert os.path.exists(d_img["image"]["path"]) and nbytes(d_img) <= DEFAULT_BUDGET["diff"]


def test_unchanged_recapture_keeps_every_ref_and_if_changed_since_short_circuits(
        pipe: Pipeline) -> None:
    c = take(pipe, "launcher")
    stored = pipe.store.summary()
    unchanged = fetch.unchanged_since(c.session(), c.loaded.meta, now=pipe.clock)
    assert unchanged == {"capture": c.loaded.id, "unchanged": True, "age_s": 2}
    assert nbytes(unchanged) <= 60
    assert pipe.store.summary() == stored  # one fingerprint, nothing written
    # a different pid is a different UI, whatever the fingerprint says
    assert fetch.unchanged_since(c.session(pid=c.pid + 1), c.loaded.meta) is None

    again = c.recapture()
    assert set(again.index().nodes) == set(c.ix.nodes)
    d = pipe.diff(c.loaded, again)
    assert d["lines"] == [] and d["summary"]["changed"] == 0


def test_app_restart_carries_refs_by_locator_and_structure(pipe: Pipeline) -> None:
    c = take(pipe, "launcher")
    old = c.ix
    restarted = c.recapture(pid=c.pid + 100)  # a new process: every udid is untrusted
    ix = restarted.index()
    matches = {n.match for n in ix.nodes.values()}
    assert "id" not in matches
    ui_nodes = ui(ix)
    carried = [n for n in ui_nodes if n.id in old.nodes]
    assert len(carried) == len(ui_nodes)  # the same screen keeps every ui ref
    assert ix.nodes[c.ref("@launch_heading")].match in ("locator", "structure")
    d = c.pipe.diff(c.loaded, restarted)
    assert d["same_pid"] is False and d["summary"]["added"] == 0


def dismiss(scene: fs.SceneData, root: int) -> None:
    """Close a window: its View root, its a11y window and its ComposeViews' dumps go."""
    gone = set()
    for i, r in enumerate(scene.views.roots):
        if r.id == root:
            stack = [r]
            while stack:
                v = stack.pop()
                gone.add(v.id)
                stack.extend(v.children)
            del scene.views.roots[i]
            break
    scene.window_ids = [w for w in scene.window_ids if w != root]
    for resp, key in ((scene.a11y, "root_view_id"), (scene.compose_sem, "view_id"),
                      (scene.compose_slots, "view_id")):
        if resp is None:
            continue
        keep = [w for w in resp.windows if getattr(w, key) not in gone]
        del resp.windows[:]
        for w in keep:
            resp.windows.add().CopyFrom(w)


def test_removed_node_leaves_a_tombstone(pipe: Pipeline) -> None:
    c = take(pipe, "mixed")
    dialog = c.ix.windows()[1]
    ok_ref = query.find(c.ix, text="OK", flags=["click"])["lines"][0].split()[0]
    dismiss(c.scene, int(dialog.ids["view"]))
    after = c.recapture()
    assert dialog.id not in after.index().nodes
    with pytest.raises(OpError) as err:
        pipe.node(after, ok_ref)
    assert err.value.code == "ref_not_in_capture"
    assert c.loaded.id in err.value.message
    d = pipe.diff(c.loaded, after)
    assert d["summary"]["removed"] >= 1
    assert any(line.startswith(f"- {dialog.id} ") for line in d["lines"])


# --------------------------------------------------------------------------- #
# Section 8: workflow token totals (within +25% of the spec's figures)
# --------------------------------------------------------------------------- #
def _inline_tokens(result: dict, max_side: int = 1024) -> int:
    return images.inline(result["path"], max_side)[2]


def test_workflow_token_totals(pipe: Pipeline) -> None:
    got: dict[str, int] = {}

    # W1 "Why is the Section heading row cut off?": capture, node, inline crop
    c = take(pipe, "launcher")
    heading = c.ref("@launch_heading")
    crop = pipe.image(c.loaded, heading, pad=48)
    got["W1"] = tokens(pipe.summary(c.loaded), pipe.node(c.loaded, heading), crop,
                       image_tokens=_inline_tokens(crop))

    # W2 "Is the ... button accessible?": capture, find, node(core,a11y,issues)
    hit = pipe.find(c.loaded, text="checkbox", flags=["click"])
    ref = hit["lines"][0].split()[0]
    got["W2"] = tokens(pipe.summary(c.loaded), hit,
                       pipe.node(c.loaded, ref, facets="core,a11y,issues"))

    # W4 audit: capture(lint=full), lint, reading, lint overlay (inline), node x2
    full = c.recapture(lint="full")
    ov = pipe.image(full, overlay="lint")
    two = [n.id for n in ui(full.index()) if n.issues][:2]
    got["W4"] = tokens(pipe.summary(full), pipe.lint(full), pipe.outline(full, view="reading"),
                       ov, *[pipe.node(full, r) for r in two],
                       image_tokens=_inline_tokens(ov))

    # W3 "What changed after I tapped X?": capture(label), node, capture + diff
    v = take(pipe, "viewscreen", label="before")
    n3 = pipe.node(v.loaded, "#badSwitch")
    tap_switch(v.scene)
    after = v.recapture()
    got["W3"] = tokens(pipe.summary(v.loaded), n3,
                       {**pipe.summary(after), "diff": pipe.diff(v.loaded, after)})

    # W6 large list: capture, find, node(props=nondefault), outline(root, depth=1)
    w = take(pipe, "wide")
    hit = pipe.find(w.loaded, text="Label 4", limit=20)
    got["W6"] = tokens(pipe.summary(w.loaded), hit,
                       pipe.node(w.loaded, "#view_47", props="nondefault"),
                       pipe.outline(w.loaded, root=w.ref("#view_3"), depth=1))

    # W7 mixed hierarchy: capture, find within the feed, lint within it, node
    m = take(pipe, "mixed")
    hits = pipe.find(m.loaded, text="Delete", flags=["click"], within="#feed")
    third = hits["lines"][2].split()[0]
    got["W7"] = tokens(pipe.summary(m.loaded), hits, pipe.lint(m.loaded, within="#feed"),
                       pipe.node(m.loaded, third))

    spec = {"W1": 1100, "W2": 1100, "W3": 1200, "W4": 2800, "W6": 2400, "W7": 1600}
    over = {k: (got[k], spec[k]) for k in spec if got[k] > spec[k] * 1.25}
    assert not over, over


# --------------------------------------------------------------------------- #
# Measurement report (used by the integration summary; not a test)
# --------------------------------------------------------------------------- #
def measure(root: str) -> dict[str, dict[str, int]]:
    """Compact bytes of each tool's default call per scene, for reporting."""
    pipe = Pipeline(root)
    out: dict[str, dict[str, int]] = {}
    try:
        for name in SCENES:
            c = take(pipe, name)
            p, lc = pipe, c.loaded
            ix = c.ix
            issue = next((n.id for n in ui(ix) if n.issues), None) or \
                next(n.id for n in ui(ix) if n.b and n.b[2] > 0)
            sizes = {"capture": nbytes(p.summary(lc)), "outline": nbytes(p.outline(lc)),
                     "outline_reading": nbytes(p.outline(lc, view="reading")),
                     "find_click": nbytes(p.find(lc, flags=["click"], limit=20)),
                     "node": nbytes(p.node(lc, issue)), "lint": nbytes(p.lint(lc)),
                     "image": nbytes(p.image(lc, issue)),
                     "captures_list": nbytes(p.captures_list())}
            if lc.meta.facet_status("slots") == "ok":
                sizes["outline_slots"] = nbytes(p.outline(lc, view="slots"))
            if name == "launcher":
                sizes["outline_root_list"] = nbytes(p.outline(lc, root="@launcher_list"))
                sizes["find_state"] = nbytes(p.find(lc, text="state", flags=["click"]))
                sizes["node_heading"] = nbytes(p.node(lc, "@launch_heading"))
            if name == "viewscreen":
                sizes["node_badSwitch_nondefault"] = nbytes(
                    p.node(lc, "#badSwitch", props="nondefault"))
            if name == "wide":
                sizes["find_label4"] = nbytes(p.find(lc, text="Label 4", limit=20))
                sizes["node_view47_nondefault"] = nbytes(
                    p.node(lc, "#view_47", props="nondefault"))
                sizes["outline_root_depth1"] = nbytes(
                    p.outline(lc, root=c.ref("#view_3"), depth=1))
            out[name] = sizes
    finally:
        pipe.store.close()
    return out


if __name__ == "__main__":  # PYTHONPATH=. python tests/test_capture_pipeline_offline.py
    import json

    with tempfile.TemporaryDirectory(prefix="iw-measure-") as _root:
        for _scene, _sizes in measure(_root).items():
            print(_scene, json.dumps(_sizes))
