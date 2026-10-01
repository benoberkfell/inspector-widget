"""Offline tests for inspector_widget.capture.index and capture.anchors (C4).

Scenes: the real launcher and View-screen replays (pre-ID1), the 259-view wide
scene, a port of the harness default scene (post-ID1 ids), and a synthetic mixed
hierarchy (three ComposeView cells and a View cell in a RecyclerView, an
AndroidView inside Compose holding a nested ComposeView, and a dialog window).
"""

from __future__ import annotations

import re
import statistics
import time

import capture_scenes as cs
import fakescenes as fs
import pytest

from inspector_widget.capture import anchors
from inspector_widget.capture import index as cx
from inspector_widget.capture.model import (
    CONF_VALUES,
    FLAGS,
    KINDS,
    CaptureMeta,
    OpError,
    RawCapture,
    index_from_jsonl,
    index_to_jsonl,
    is_key,
    is_ref,
)
from inspector_widget.proto import view_inspection_pb2 as pb


# --------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def launcher_raw():
    return cs.raw_from_scene(fs.replay_scene("launcher"))


@pytest.fixture(scope="module")
def launcher(launcher_raw):
    return cx.build_index(launcher_raw)


@pytest.fixture(scope="module")
def viewscreen_raw():
    return cs.raw_from_scene(fs.replay_scene("viewscreen"))


@pytest.fixture(scope="module")
def viewscreen(viewscreen_raw):
    return cx.build_index(viewscreen_raw)


@pytest.fixture(scope="module")
def wide():
    return cx.build_index(cs.raw_from_scene(fs.wide_scene()))


@pytest.fixture(scope="module")
def mixed_raw():
    return cs.mixed_scene()


@pytest.fixture(scope="module")
def mixed(mixed_raw):
    return cx.build_index(mixed_raw)


@pytest.fixture(scope="module")
def default_like():
    return cx.build_index(cs.default_like_scene())


SCENES = {
    "launcher": lambda: cs.raw_from_scene(fs.replay_scene("launcher")),
    "launcher_no_slots": lambda: cs.raw_from_scene(fs.replay_scene("launcher"), slots=False),
    "viewscreen": lambda: cs.raw_from_scene(fs.replay_scene("viewscreen")),
    "wide": lambda: cs.raw_from_scene(fs.wide_scene()),
    "mixed": cs.mixed_scene,
    "mixed_pre_id1": lambda: cs.mixed_scene(pre_id1=True),
    "mixed_screen_px": lambda: cs.mixed_scene(window_relative=False),
    "default_like": cs.default_like_scene,
    "default_like_pre_id1": lambda: cs.encode(cs.default_like_windows(), pre_id1=True),
}


def a11y_count(raw: RawCapture) -> int:
    msg = pb.DumpA11yResponse()
    msg.ParseFromString(raw.a11y)
    n = 0
    stack = [w.root for w in msg.windows]
    while stack:
        node = stack.pop()
        n += 1
        stack.extend(node.children)
    return n


def a11y_targets(raw: RawCapture, ix) -> dict:
    """``{(window, a11y path): node id}`` for every a11y node of a capture."""
    msg = pb.DumpA11yResponse()
    msg.ParseFromString(raw.a11y)
    out = {}
    for w in msg.windows:
        stack = [(w.root, (0,))]
        while stack:
            n, path = stack.pop()
            pkey = f"a11y:path:{w.root_view_id}:{'.'.join(map(str, path))}"
            out[(w.root_view_id, path)] = ix.resolve_id(pkey) or ix.resolve_id(
                f"a11y:{n.host_view_id}:{n.virtual_id}")
            stack.extend((c, path + (i,)) for i, c in enumerate(n.children))
    return out


def ui_ids(ix):
    return [n.id for n, _ in ix.walk("ui")]


# --------------------------------------------------------------------------- invariants
@pytest.mark.parametrize("name", sorted(SCENES))
def test_index_invariants(name):
    raw = SCENES[name]()
    ix = cx.build_index(raw)
    assert ix.meta is raw.meta
    # keys: valid, unique, and every node is in key space (no refs yet)
    keys = [n.key for n in ix.nodes.values()]
    assert len(keys) == len(set(keys))
    for nid, n in ix.nodes.items():
        assert is_key(n.key), n.key
        assert nid == n.key and n.ref is None
        assert n.kind in KINDS
        assert set(n.flags) <= set(FLAGS) and n.flags == [f for f in FLAGS if f in n.flags]
        assert set(n.conf.values()) <= set(CONF_VALUES)
        assert ix.by_key[n.key] == nid
    # primary trees: ui for view/compose/a11y, slots for slot nodes
    ui = ix.tree("ui")
    order = [n.id for n, _ in ix.walk("ui")] + [n.id for n, _ in ix.walk("slots")]
    assert order == list(ix.nodes), "Index.nodes is ui pre-order then slots pre-order"
    for n, depth in ix.walk("ui"):
        assert n.kind != "slot"
        assert n.depth == depth
        assert n.children == ui.children.get(n.id, [])
        for c in n.children:
            assert ix.nodes[c].parent == n.id
        assert ix.nodes[n.window].is_window
        if n.parent is None:
            assert n.is_window and n.id in ui.roots and n.window == n.id
    for n, depth in ix.walk("slots"):
        assert n.kind == "slot" and n.depth == depth and n.origin in ("app", "library")
    assert [w.id for w in ix.windows()] == ui.roots
    assert [w.z for w in ix.windows()] == list(range(len(ui.roots)))
    # the views tree holds exactly the view nodes; a11y tree nodes carry the facet
    view_ids = [nid for nid, _ in ix.tree("views").walk()]
    assert sorted(view_ids) == sorted(n.id for n in ix.nodes.values() if n.kind == "view")
    a11y_tree = [nid for nid, _ in ix.tree("a11y").walk()]
    assert len(a11y_tree) == len(set(a11y_tree)), "an a11y node attached twice"
    assert sorted(a11y_tree) == sorted(n.id for n in ix.nodes.values() if "a11y" in n.facets)
    if raw.a11y:
        assert len(a11y_tree) == a11y_count(raw)
    # anchors unique; sel unique and parseable; slots use their id as sel
    anchors_ = [n.anchor for n in ix.nodes.values()]
    assert all(anchors_) and len(anchors_) == len(set(anchors_))
    for n in ix.nodes.values():
        if n.kind == "slot":
            assert n.sel == n.id
        else:
            assert anchors.match_sel(ix, n.sel) == [n.id], (n.id, n.sel)
    # lossless and deterministic
    blob = index_to_jsonl(ix)
    again = index_from_jsonl(blob, ix.meta)
    assert index_to_jsonl(again) == blob
    assert index_to_jsonl(cx.build_index(raw)) == blob


# --------------------------------------------------------------------------- launcher (real, pre-ID1)
def test_launcher_one_window_view_spine_and_grafted_semantics(launcher):
    assert [w.id for w in launcher.windows()] == ["view:1"]
    views = [n for n in launcher.nodes.values() if n.kind == "view"]
    assert len(views) == 9
    sem = [n for n in launcher.nodes.values() if n.kind == "compose"]
    assert len(sem) == 17
    assert all(re.fullmatch(r"sem:82:\d+", n.key) for n in sem)
    # the synthetic root that reuses the ACV id (ID3) is folded into the ACV View
    assert "sem:82:82" not in launcher.nodes
    assert launcher.tree("compose").roots == ["sem:82:150"]
    assert launcher.nodes["sem:82:150"].parent == "view:82"
    assert all(n.window == "view:1" for n in sem)
    acv = launcher.nodes["view:82"]
    assert acv.children[0] == "sem:82:150" and acv.children[-1] == "view:151"
    assert launcher.by_key["compose:82"] == "view:82"
    assert launcher.by_key["w:1"] == "view:1"
    assert cx.resolve_key(launcher, "compose:448") == "sem:82:448"


def test_launcher_id1_detector_infers_every_a11y_facet(launcher, launcher_raw):
    assert cx.ID1_DUPLICATES in launcher.diagnostics
    with_a11y = [n for n in launcher.nodes.values() if "a11y" in n.facets]
    assert len(with_a11y) == a11y_count(launcher_raw) == 40
    assert {n.conf["a11y"] for n in with_a11y} == {"inferred"}
    # every a11y node is its own path key or an alias, never an a11y:<host>:<virt> pair
    assert not any(re.fullmatch(r"a11y:-?\d+:-?\d+", k) for k in launcher.by_key)
    targets = list(a11y_targets(launcher_raw, launcher).values())
    assert None not in targets and len(targets) == len(set(targets))
    # the 12 rows, the list, the title and the two traversal groups have twins
    for sid in (150, 310, 313, 317, 325, *range(327, 449, 11)):
        assert launcher.nodes[f"sem:82:{sid}"].conf.get("a11y") == "inferred", sid
    assert launcher.nodes["view:1"].conf["a11y"] == "inferred"
    assert "a11y" not in launcher.nodes["view:76"].facets  # the nav bar is not a row
    # merged text children of each row become a11y nodes under the row
    only = [n for n in launcher.nodes.values() if n.kind == "a11y"]
    assert len(only) == 22
    assert all(launcher.nodes[n.parent].kind == "compose" for n in only)


def test_launcher_clipped_heading_row(launcher):
    n = launcher.nodes["sem:82:448"]
    assert n.b == [0, 2757, 1280, 27]
    assert n.declared_b == [0, 2757, 1280, 216] and n.visible == 0.125
    assert n.src == "MainActivity.kt:150" and n.type == "ListItem"
    assert n.tag == "launch_heading" and n.sel == "@launch_heading"
    assert n.label == "Section heading, MissingHeading"
    assert n.flags == ["click", "tgroup"]
    # the a11y twin is clipped by the screen, not the list: matched by containment
    assert n.facets["a11y"]["b"] == [0, 2757, 1280, 99]
    slots = [launcher.nodes[s] for s in n.facets["compose"]["slots"]]
    assert [(s.type, s.src) for s in slots] == [
        ("ListItem", "MainActivity.kt:150"), ("Text", "MainActivity.kt:151"),
        ("Text", "MainActivity.kt:152")]
    assert [s.text for s in slots[1:]] == ["Section heading", "MissingHeading"]
    assert all(s.facets["slot"]["sem"] == ["sem:82:448"] for s in slots)
    assert n.conf["slot"] == "inferred"


def test_launcher_types_labels_and_flags(launcher):
    row = launcher.nodes["sem:82:327"]
    # a focusable row speaks its non-focusable children (RO1), like Compose's merged Text
    assert row.label == row.text == (
        "▶ All scenarios (lint everything), every BAD/GOOD variant on one scrollable screen")
    assert row.facets["a11y"]["speakable"] == row.label
    assert row.src == "MainActivity.kt:140" and row.type == "ListItem"
    assert "RequestFocus" in row.facets["compose"]["actions"]
    lst = launcher.nodes["sem:82:325"]
    assert (lst.type, lst.tag, lst.flags) == ("LazyColumn", "launcher_list", ["scroll", "tgroup"])
    assert launcher.nodes["sem:82:317"].type == "Text"
    assert launcher.nodes["sem:82:317"].label == "A11yProbe"
    assert launcher.nodes["view:79"].flags == ["hidden"]  # ViewStub: visibility gone
    assert launcher.nodes["view:77"].flags == ["hidden"]  # status bar: invisible
    assert launcher.nodes["view:80"].rid == "content"
    assert launcher.nodes["view:1"].type == "DecorView"
    # anchors of the lazy list rows carry collection indexes; templates collapse them
    rows = [launcher.nodes[f"sem:82:{sid}"] for sid in range(327, 449, 11)]
    assert [anchors.collection_index(r.anchor) for r in rows] == list(range(12))
    assert rows[0].anchor.endswith("/@launcher_list/@launch_all[0]")


def test_launcher_slot_nodes(launcher):
    slots = [n for n in launcher.nodes.values() if n.kind == "slot"]
    assert len(slots) == 381
    assert all(re.fullmatch(r"slot:82:[0-9a-f]{8}", n.key) for n in slots)
    app = [n for n in slots if n.origin == "app"]
    assert {n.src.split(":")[0] for n in app} == {"MainActivity.kt"}
    # every lazy item keeps its key in the anchor
    items = [n for n in slots if n.type == "SkippableItem"]
    assert len(items) == 12 and all("[" in n.anchor for n in items)
    assert any(n.anchor.endswith("SkippableItem@LazyLayoutItemContentFactory.kt:101[heading]:0")
               for n in items)
    li = next(n for n in slots if n.type == "ListItem" and n.facets["slot"].get("sem") ==
              ["sem:82:448"])
    assert li.facets["slot"]["mods"] == "composed,testTag"
    assert li.facets["slot"]["params"]["headlineContent"] == "λ"
    assert li.window == "view:1"
    assert li.ids["slot"].startswith("MainActivity.kt:150#")
    raw_params = cx.FacetReader(launcher_raw_for_reader()).slot_params(li.ids["slot_path"])
    assert "TestTagElement" in raw_params["modifier"]
    assert any(n.ids.get("layer") for n in slots)


def launcher_raw_for_reader():
    return cs.raw_from_scene(fs.replay_scene("launcher"))


def test_launcher_slot_subcompositions_are_grafted_in_screen_order(launcher):
    """The recording has 29 top-level slot groups in the device's hash order (the
    main composition plus one per Lazy item and Scaffold slot). The slots tree is
    one tree: the main composition first, each subcomposition under the group whose
    box holds it, the list's items top down; zero-size effects stay roots, last."""
    tree = launcher.tree("slots")
    roots = [launcher.nodes[r] for r in tree.roots]
    sized = [n for n in roots if n.b and n.b[2] > 0 and n.b[3] > 0]
    assert [n.type for n in sized] == ["ProvideAndroidCompositionLocals"]
    assert all(not (n.b and n.b[2] and n.b[3]) for n in roots[1:])

    def find(t, src=None):
        return next(n for n in launcher.nodes.values() if n.kind == "slot" and n.type == t
                    and (src is None or n.src == src))

    top_bar, lazy = find("TopAppBar"), find("LazyColumn")
    content = find("Box", "MainActivity.kt:96")
    for n in (top_bar, content):
        assert launcher.nodes[n.parent].b == [0, 0, 1280, 2856]  # a Scaffold-sized group
        assert n.conf.get("slots") == "inferred"
    items = [n for n in launcher.nodes.values() if n.type == "SkippableItem"]
    assert len(items) == 12 and all(n.conf.get("slots") == "inferred" for n in items)
    anc = {n.id: {a.id for a in launcher.ancestors(n.id)} for n in items}
    assert all(lazy.id in a for a in anc.values())  # every item is under the list
    parent = launcher.nodes[items[0].parent]
    kids = [launcher.nodes[c] for c in parent.children if launcher.nodes[c].type == "SkippableItem"]
    assert [k.b[1] for k in kids] == sorted(k.b[1] for k in kids)  # screen order
    assert kids[-1].b[1] == 2757  # the clipped last row still found its list
    # nodes are stored in the slot tree's pre-order, and keys are deterministic
    again = cx.build_index(launcher_raw_for_reader())
    assert [n.key for n in again.nodes.values()] == [n.key for n in launcher.nodes.values()]


def test_launcher_build_time(launcher_raw):
    times = []
    for _ in range(5):
        t = time.perf_counter()
        cx.build_index(launcher_raw)
        times.append(time.perf_counter() - t)
    assert min(times) <= 0.100, times
    assert statistics.median(times) <= 0.150, times


def test_launcher_without_slot_table(launcher_raw):
    raw = RawCapture(meta=launcher_raw.meta, windows=launcher_raw.windows,
                     views=launcher_raw.views, compose_sem=launcher_raw.compose_sem,
                     a11y=launcher_raw.a11y)
    ix = cx.build_index(raw)
    assert not any(n.kind == "slot" for n in ix.nodes.values())
    n = ix.nodes["sem:82:448"]
    assert n.src is None and n.declared_b is None and "slots" not in n.facets["compose"]
    assert n.type is None  # a11y class android.view.View is generic: no type invented
    assert ix.nodes["sem:82:317"].type == "TextView"


# --------------------------------------------------------------------------- View screen (real, pre-ID1)
def test_viewscreen_joins_every_view(viewscreen, viewscreen_raw):
    kinds = {n.kind for n in viewscreen.nodes.values()}
    assert kinds == {"view"}
    assert len(viewscreen.nodes) == 40
    assert cx.ID1_IMPLAUSIBLE in viewscreen.diagnostics
    joined = [n for n in viewscreen.nodes.values() if "a11y" in n.facets]
    assert len(joined) == a11y_count(viewscreen_raw) == 34
    assert {n.conf["a11y"] for n in joined} == {"inferred"}
    sw = viewscreen.get("#badSwitch") or viewscreen.nodes[
        next(nid for nid, n in viewscreen.nodes.items() if n.rid == "badSwitch")]
    assert (sw.type, sw.label, sw.sel) == ("Switch", "Notifications", "#badSwitch")
    assert sw.flags == ["click", "checkable", "checked"]
    assert (sw.facets["view"]["class"], sw.facets["a11y"]["class"]) == (
        "SwitchMaterial", "android.widget.Switch")
    ib = viewscreen.nodes["view:6"]
    assert (ib.type, ib.rid, ib.label) == ("ImageButton", "badImageButton", "Like this photo")
    assert viewscreen.nodes["view:1"].type == "ScrollView"
    assert viewscreen.nodes["view:1"].label is None  # a ScrollView does not speak its content
    assert viewscreen.nodes["view:19"].type == "Image"
    assert viewscreen.nodes["view:24"].type == "Button"  # accessibility class via a delegate
    assert viewscreen.nodes["view:25"].type == "MaterialTextView"
    assert viewscreen.nodes["view:32"].flags == ["click", "longclick", "edit"]
    assert viewscreen.nodes["view:40"].facets["view"]["layout_res"] == \
        "@com.oberkfell.a11yprobe:layout/abc_screen_content_include"


def test_viewscreen_props_are_decoded_only_on_access(viewscreen_raw, monkeypatch):
    calls = []
    real = cx.property_to_dict

    def counting(resolver, prop):
        calls.append(prop)
        return real(resolver, prop)

    monkeypatch.setattr(cx, "property_to_dict", counting)
    ix = cx.build_index(viewscreen_raw)
    assert calls == [] and len(ix.nodes) == 40
    reader = cx.FacetReader(viewscreen_raw)
    assert reader.decoded == 0 and reader.has_props(16)
    props = reader.props(16)
    assert reader.decoded == 1 and len(calls) == len(reader.prop_list(16))
    assert props["text"] == "Notifications" and props["checked"] is True
    reader.props(16)
    assert reader.decoded == 1  # cached
    assert reader.props(999_999) is None and reader.decoded == 1


# --------------------------------------------------------------------------- wide scene
def test_wide_scene_joins_exact(wide):
    views = [n for n in wide.nodes.values() if n.kind == "view"]
    assert len(views) == 259
    assert wide.diagnostics == []
    assert all(n.conf["a11y"] == "exact" for n in views)
    assert all(n.sel == f"#{n.rid}" for n in views)
    leaf = wide.nodes["view:1259"]
    assert (leaf.type, leaf.label, leaf.b) == ("TextView", "Label 259", [259, 777, 300, 60])
    assert leaf.flags == ["click"]
    assert wide.by_key["a11y:1259:-1"] == "view:1259"
    # the zero-size "Root" semantics node does not trigger a coordinate shift
    assert wide.nodes["sem:1001:1"].b == [0, 0, 0, 0]


# --------------------------------------------------------------------------- default scene (post-ID1)
def test_default_like_scene_joins_exact(default_like):
    ix = default_like
    assert ix.diagnostics == []
    joined = [n for n in ix.nodes.values() if "a11y" in n.facets]
    assert {n.conf["a11y"] for n in joined} == {"exact"}
    assert not any(n.kind == "a11y" for n in ix.nodes.values())
    assert [(w.id, w.z) for w in ix.windows()] == [("view:1001", 0), ("view:2001", 1)]
    assert ix.nodes["view:2002"].window == "view:2001"
    types = {k: ix.nodes[f"sem:1006:{k}"].type for k in range(2, 7)}
    assert types == {2: "Button", 3: "Image", 4: "TextView", 5: "Switch", 6: None}
    assert ix.nodes["sem:1006:4"].flags == ["heading"]
    assert ix.nodes["sem:1006:5"].flags == ["click", "checkable", "checked"]
    assert ix.nodes["sem:1006:5"].state == "On"
    assert ix.nodes["sem:1006:2"].src == "MainActivity.kt:42"
    submit = ix.nodes[ix.nodes["sem:1006:2"].facets["compose"]["slots"][0]]
    assert (submit.type, submit.ids["layer"]) == ("SubmitButton", 7002)
    assert ix.nodes["view:1004"].sel == "#ok" and ix.nodes["view:1004"].type == "Button"
    assert ix.nodes["view:1006"].facets["a11y"]["provider"].endswith("AndroidComposeView")
    assert ix.by_key["a11y:1006:5"] == "sem:1006:5"


def test_harness_default_scene_joins_exact_when_available():
    fakeagent = pytest.importorskip("fakeagent")
    agent = fakeagent.FakeAgent()
    try:
        def call(**cmd):
            req = pb.Request(id=1, **cmd)
            resp = agent.dispatch(req)
            return getattr(resp, resp.WhichOneof("payload"))
        raw = RawCapture(meta=CaptureMeta(id="charn1", lineage=("emulator-5554", "pkg")))
        raw.windows = call(get_windows=pb.GetWindowsCommand()).SerializeToString()
        raw.views = call(dump_tree=pb.DumpTreeCommand(include_properties=True)).SerializeToString()
        raw.compose_sem = call(dump_compose=pb.DumpComposeCommand(
            include_semantics=True)).SerializeToString()
        raw.a11y = call(dump_a11y=pb.DumpA11yCommand(include_extras=True)).SerializeToString()
    finally:
        agent.stop()
    ix = cx.build_index(raw)
    joined = [n for n in ix.nodes.values() if "a11y" in n.facets]
    assert joined and {n.conf["a11y"] for n in joined} == {"exact"}
    assert not any("ID1" in d for d in ix.diagnostics)


# --------------------------------------------------------------------------- mixed hierarchy
def test_mixed_composite_keys_are_distinct_across_compose_views(mixed):
    for acv in (12, 22, 32):
        assert mixed.nodes[f"sem:{acv}:2"].label == f"Item {(acv - 2) // 10}"
        assert mixed.nodes[f"sem:{acv}:4"].parent == f"sem:{acv}:2"
    with pytest.raises(OpError) as err:
        cx.resolve_key(mixed, "compose:2")
    assert err.value.code == "ambiguous"
    assert err.value.candidates == ["sem:12:2", "sem:22:2", "sem:32:2", "sem:58:2", "sem:103:2"]
    assert cx.legacy_candidates(mixed, "compose:4") == ["sem:12:4", "sem:22:4", "sem:32:4",
                                                         "sem:103:4"]
    assert "compose:2" not in mixed.by_key
    assert cx.resolve_key(mixed, "compose:12") == "view:12"  # the folded synthetic root
    assert cx.resolve_key(mixed, "compose:5") == "sem:52:5"  # unique: an alias
    assert cx.resolve_key(mixed, "compose:22:4") == "sem:22:4"  # agent contract spelling
    assert cx.resolve_key(mixed, "composeview:52") == "view:52"
    assert cx.resolve_key(mixed, "w:100") == "view:100"
    with pytest.raises(OpError) as err:
        cx.resolve_key(mixed, "view:424242")
    assert err.value.code == "not_found"
    slot_keys = {n.key for n in mixed.nodes.values() if n.kind == "slot"}
    cells = [{n.key for n in mixed.nodes.values() if n.kind == "slot" and
              n.key.startswith(f"slot:{acv}:")} for acv in (12, 22, 32)]
    assert all(len(c) == 7 for c in cells) and len(slot_keys) >= 21


def test_mixed_dialog_is_its_own_window_in_screen_px(mixed):
    assert [(w.id, w.z) for w in mixed.windows()] == [("view:1", 0), ("view:100", 1)]
    title = mixed.nodes["sem:103:2"]
    assert title.b == [164, 924, 500, 60] and title.window == "view:100"
    assert mixed.nodes["sem:103:4"].b == [740, 1400, 180, 72]
    assert any("view:103" in d and "(+140,+900)" in d for d in mixed.diagnostics)
    assert title.anchor.startswith("w1/")
    # the dialog's slot boxes get the same shift, so the emitter links still hold
    assert title.src == "Dialogs.kt:33"
    ok_slot = mixed.nodes[mixed.nodes["sem:103:4"].facets["compose"]["slots"][0]]
    assert ok_slot.b == [740, 1400, 180, 72] and ok_slot.window == "view:100"


def test_mixed_screen_px_compose_is_not_shifted():
    ix = cx.build_index(cs.mixed_scene(window_relative=False))
    assert not any("CO4" in d for d in ix.diagnostics)
    assert ix.nodes["sem:103:2"].b == [164, 924, 500, 60]


def test_slot_only_compose_view_is_shifted_too():
    windows = cs.mixed_windows()
    dialog_acv = next(v for v in windows[1].walk() if v.id == 103)
    dialog_acv.sem = None  # slot table only: the shift is detected from the slot roots
    ix = cx.build_index(cs.encode(windows, window_relative=True))
    confirm = next(n for n in ix.nodes.values() if n.kind == "slot" and n.type == "ConfirmDialog")
    assert confirm.b == [140, 900, 800, 600] and confirm.window == "view:100"
    assert any("view:103" in d and "CO4" in d for d in ix.diagnostics)


def test_mixed_android_view_is_reparented_under_its_semantics_node(mixed):
    holder = mixed.nodes["view:54"]
    assert holder.parent == "sem:52:6" and "interop" in holder.flags
    assert holder.conf["ui"] == "inferred"
    assert mixed.nodes["view:53"].children == []  # AndroidViewsHandler stays, emptied
    assert mixed.tree("views").children["view:53"] == ["view:54"]  # raw View tree untouched
    assert mixed.nodes["sem:52:6"].type == "AndroidView"  # emitted by AndroidView(...)
    # the nested ComposeView inside the AndroidView is grafted too (ID2)
    zoom = mixed.nodes["sem:58:2"]
    assert zoom.parent == "sem:58:1" and mixed.nodes["sem:58:1"].parent == "view:58"
    assert "sem:52:6" in [a.id for a in mixed.ancestors("sem:58:2")]
    assert zoom.conf["a11y"] == "exact" and zoom.label == "Zoom"
    assert any("AndroidView" in d for d in mixed.diagnostics)


def test_mixed_a11y_joins_exact_and_links(mixed):
    joined = [n for n in mixed.nodes.values() if "a11y" in n.facets]
    assert {n.conf["a11y"] for n in joined} == {"exact"}
    for acv, n in ((12, 1), (22, 2), (32, 3)):
        text = mixed.nodes[f"a11y:{acv}:3"]  # unmerged Text: no semantics twin
        assert (text.kind, text.parent, text.label) == ("a11y", f"sem:{acv}:2", f"Item {n}")
        assert mixed.nodes[f"sem:{acv}:2"].facets["a11y"]["speakable"] == f"Item {n}"
    delete = mixed.nodes["view:43"]
    assert delete.facets["a11y"]["labeled_by"] == "view:42"
    assert mixed.nodes["view:10"].facets["a11y"]["collection"] == {"rows": 5, "cols": 1}
    assert mixed.by_key["a11y:43:-1"] == "view:43"
    assert mixed.nodes["view:90"].flags == ["hidden"]


def test_mixed_slot_links_per_cell(mixed):
    for acv in (12, 22, 32):
        card = mixed.nodes[f"sem:{acv}:2"]
        assert (card.type, card.src) == ("Card", "FeedRow.kt:44")
        linked = [mixed.nodes[s] for s in card.facets["compose"]["slots"]]
        assert [(s.type, s.src) for s in linked] == [("Card", "FeedRow.kt:44"),
                                                    ("Text", "FeedRow.kt:47")]
        button = mixed.nodes[f"sem:{acv}:4"]
        assert (button.type, button.src, button.role) == ("Button", "FeedRow.kt:52", "Button")
        assert [mixed.nodes[s].src for s in button.facets["compose"]["slots"]] == [
            "FeedRow.kt:52", "FeedRow.kt:53"]
        assert all(mixed.nodes[s].window == "view:1" for s in button.facets["compose"]["slots"])


def test_mixed_anchors_templates_and_sels(mixed):
    deletes = [mixed.nodes[f"sem:{acv}:4"] for acv in (12, 22, 32)]
    assert len({d.anchor for d in deletes}) == 3
    assert len({anchors.template(d.anchor) for d in deletes}) == 1
    assert [anchors.collection_index(mixed.nodes[f"view:{v}"].anchor)
            for v in (11, 21, 31, 41, 51)] == [0, 1, 2, 3, 4]
    assert mixed.nodes["view:41"].anchor.endswith("/RecyclerView#feed/LinearLayout#row[3]")
    assert mixed.nodes["view:43"].anchor.endswith("/MaterialButton#delete")
    assert deletes[1].sel == 'Card"Item 2" > Button"Delete"'
    assert mixed.nodes["view:43"].sel == "#delete"
    assert mixed.nodes["view:3"].sel == "view:3"  # #content is also the dialog's
    assert mixed.nodes["sem:52:6"].sel == "@map"
    assert mixed.nodes["view:54"].sel == "@map > ViewFactoryHolder"
    assert anchors.without_ordinals(deletes[0].anchor) == anchors.without_ordinals(
        deletes[2].anchor)


def test_mixed_pre_id1_matches_by_bounds():
    post_raw, pre_raw = cs.mixed_scene(), cs.mixed_scene(pre_id1=True)
    post, pre = cx.build_index(post_raw), cx.build_index(pre_raw)
    assert cx.ID1_DUPLICATES in pre.diagnostics
    assert {n.conf["a11y"] for n in pre.nodes.values() if "a11y" in n.facets} == {"inferred"}
    want, got = a11y_targets(post_raw, post), a11y_targets(pre_raw, pre)
    assert None not in got.values() and len(set(got.values())) == len(got)
    wrong = {}
    for path, key in want.items():
        same = got[path] == key or (post.nodes[key].kind == "a11y" and
                                    pre.nodes[got[path]].kind == "a11y")
        if not same:
            wrong[key] = got[path]
    # a ComposeView's own a11y node stands for its root semantics node (CONTRACT.md
    # section 9), so bounds may pick either; nothing else may differ
    assert all(k.startswith("view:") and v == f"sem:{k[5:]}:1" for k, v in wrong.items()), wrong
    assert len(wrong) <= 6 and len(want) - len(wrong) >= 22


def test_default_like_pre_id1_matches_by_bounds():
    post_raw = cs.default_like_scene()
    pre_raw = cs.encode(cs.default_like_windows(), pre_id1=True)
    post, pre = cx.build_index(post_raw), cx.build_index(pre_raw)
    want, got = a11y_targets(post_raw, post), a11y_targets(pre_raw, pre)
    wrong = {k: got[p] for p, k in want.items() if got[p] != k}
    assert wrong == {"view:1006": "sem:1006:1"}


# --------------------------------------------------------------------------- refs
def test_apply_refs_moves_to_ref_space(mixed):
    before = index_to_jsonl(mixed)
    refmap = {n.key: f"n{i}" for i, n in enumerate(mixed.nodes.values(), 1)}
    ix = cx.apply_refs(mixed, refmap)
    assert index_to_jsonl(mixed) == before, "apply_refs must not mutate its input"
    assert all(is_ref(nid) and n.ref == nid for nid, n in ix.nodes.items())
    assert ix.by_key["view:43"] == refmap["view:43"]
    assert ix.by_key["w:100"] == refmap["view:100"]
    delete = ix.get("view:43")
    assert delete.facets["a11y"]["labeled_by"] == refmap["view:42"]
    card = ix.get("sem:12:2")
    assert all(is_ref(s) for s in card.facets["compose"]["slots"])
    assert ix.nodes[card.facets["compose"]["slots"][0]].facets["slot"]["sem"] == [card.id]
    # fallback sels (the key) become the ref; semantic sels are kept
    assert ix.get("view:3").sel == refmap["view:3"]
    assert ix.get("view:43").sel == "#delete"
    for n in ix.nodes.values():
        if n.kind != "slot":
            assert anchors.match_sel(ix, n.sel) == [n.id]
    assert ix.tree("ui").roots == [refmap["view:1"], refmap["view:100"]]


# --------------------------------------------------------------------------- scale and robustness
def test_big_scene_build_time():
    raw = cs.big_scene(500)
    times = []
    for _ in range(3):
        t = time.perf_counter()
        ix = cx.build_index(raw)
        times.append(time.perf_counter() - t)
    assert len(ix) >= 5000
    assert min(times) <= 0.5, times
    assert len({n.key for n in ix.nodes.values()}) == len(ix)
    assert len({anchors.template(ix.nodes[f"sem:{10_001 + 2 * i}:4"].anchor)
                for i in range(500)}) == 1


def test_empty_and_corrupt_facets():
    meta = CaptureMeta(id="cempty", lineage=("s", "p"))
    ix = cx.build_index(RawCapture(meta=meta))
    assert len(ix) == 0 and ix.diagnostics == []
    raw = cs.mixed_scene()
    raw.a11y = b"\xff\xff\xff not a protobuf"
    ix = cx.build_index(raw)
    assert any(d.startswith("a11y: could not be parsed") for d in ix.diagnostics)
    assert "sem:12:2" in ix.nodes and "a11y" not in ix.nodes["sem:12:2"].facets
    raw = cs.mixed_scene()
    raw.windows = b""  # no GetWindows: DumpTree root order is the z order
    ix = cx.build_index(raw)
    assert [w.id for w in ix.windows()] == ["view:1", "view:100"]


def test_compose_window_without_its_view_is_kept():
    windows = cs.default_like_windows()
    content = windows[0].children[0]
    content.children = [c for c in content.children if c.id != 1006]
    raw = cs.encode(windows)
    # re-add a compose window for the removed ACV
    full = cs.encode(cs.default_like_windows())
    raw.compose_sem, raw.slots = full.compose_sem, full.slots
    ix = cx.build_index(raw)
    assert any("view:1006 has no View" in d for d in ix.diagnostics)
    assert ix.nodes["sem:1006:2"].window == "view:1001"


def test_unresolved_a11y_host_falls_back_to_bounds():
    raw = cs.default_like_scene()
    msg = pb.DumpA11yResponse()
    msg.ParseFromString(raw.a11y)
    button = msg.windows[0].root.children[0].children[1]  # the OK Button (1004, -1)
    assert button.host_view_id == 1004
    button.host_view_id = 0  # the agent could not resolve the backing View
    raw.a11y = msg.SerializeToString()
    ix = cx.build_index(raw)
    assert ix.nodes["view:1004"].conf["a11y"] == "inferred"
    assert not any("ID1" in d for d in ix.diagnostics)
    assert {n.conf["a11y"] for n in ix.nodes.values() if "a11y" in n.facets} == {
        "exact", "inferred"}


def test_facet_reader_semantics_attrs(mixed_raw):
    reader = cx.FacetReader(mixed_raw)
    assert reader.sem_attrs(22, 4)["Text"] == "Delete"
    assert reader.sem_attrs(22, 99) is None
    assert reader.slot_params("12/0.0.0.0") == {"text": "Item 1", "maxLines": "1",
                                                 "overflow": "2"}
    assert reader.slot_params("12/9") is None and reader.slot_params("junk") is None


# --------------------------------------------------------------------------- anchors unit tests
def test_anchor_escaping_templates_and_ordinals():
    assert anchors.esc('a/b"c[1]:d\\') == 'a\\/b\\"c\\[1\\]\\:d\\\\'
    a = 'w0/DecorView:0/RecyclerView#feed/ComposeView[12]/:0/Button"Item \\[3\\]"'
    assert anchors.template(a) == 'w0/DecorView:0/RecyclerView#feed/ComposeView[*]/:0/Button"Item \\[3\\]"'
    assert anchors.collection_index(a) == 12
    # a bare ordinal segment strips to an empty one, so the depth is kept
    assert anchors.without_ordinals(a) == 'w0/DecorView/RecyclerView#feed/ComposeView//Button"Item \\[3\\]"'
    slot = "w0/X:0/SkippableItem@LazyLayoutItemContentFactory.kt:101[heading]:0"
    assert anchors.without_ordinals(slot).endswith("LazyLayoutItemContentFactory.kt:101[heading]")
    assert anchors.slot_base("Text", "Main.kt:12", "k/1") == "Text@Main.kt:12[k\\/1]"
    assert anchors.slot_segments(["A", "B", "A"]) == ["A:0", "B:0", "A:1"]


def test_ui_segments_rules():
    from inspector_widget.capture.model import UNode

    def view(cls, rid=None, **kw):
        return UNode(key="view:1", kind="view", rid=rid, facets={"view": {"class": cls}}, **kw)

    def sem(tag=None, type=None, label=None, **kw):
        return UNode(key="sem:1:1", kind="compose", tag=tag, type=type, label=label, **kw)

    segs = anchors.ui_segments([view("TextView", "a"), view("TextView"), view("TextView", "b"),
                                view("TextView"), view("Button", "x"), view("Button", "x")],
                               False)
    assert segs == ["TextView#a", "TextView:0", "TextView#b", "TextView:1", "Button:0",
                    "Button:1"]
    segs = anchors.ui_segments([sem("t"), sem(type="Button", label="OK"), sem(),
                                sem(label="A very long label that goes on and on"),
                                sem(type="Button", label="OK"), sem()], False)
    assert segs == ["@t", 'Button"OK":0', ":0", '"A very long label that g"', 'Button"OK":1',
                    ":1"]
    # collection children: CollectionItemInfo rows when distinct, else positions
    items = [sem(label="x", facets={"a11y": {"item": {"row": r, "col": 0}}}) for r in (7, 8, 9)]
    assert anchors.ui_segments(items, True) == ['"x"[7]', '"x"[8]', '"x"[9]']
    items[2].facets["a11y"]["item"]["row"] = 7
    assert anchors.ui_segments(items, True) == ['"x"[0]', '"x"[1]', '"x"[2]']


def test_selector_atoms_and_reference_matcher(mixed):
    assert anchors.parse_atom('Button"OK"i') == {"rid": None, "tag": None, "type": "Button",
                                                 "label": "OK", "ci": True}
    assert anchors.parse_atom('"say \\"hi\\""')["label"] == 'say "hi"'
    for bad in ("", "button", "#", '"unterminated', "#a b"):
        with pytest.raises(ValueError):
            anchors.parse_atom(bad)
    assert anchors.match_sel(mixed, 'Button"delete"i') == ["sem:12:4", "sem:22:4", "sem:32:4",
                                                         "view:43"]
    assert anchors.match_sel(mixed, "#feed > LinearLayout > #delete") == ["view:43"]
    assert anchors.match_sel(mixed, "w:100") == ["view:100"]
    assert anchors.match_sel(mixed, "n99999") == []
    assert anchors.quote('a"b\\c') == '"a\\"b\\\\c"'


# --------------------------------------------------------------------------- integration
def test_a11y_facet_flags_use_the_node_vocabulary(default_like, launcher):
    """The a11y facet's flags are UNode flag words (C6 folds them into the node's
    flags, C7 reads ``hidden``); other booleans worth keeping go under ``more``."""
    for ix in (default_like, launcher):
        for n in ix.nodes.values():
            fa = n.facets.get("a11y") or {}
            assert set(fa.get("flags") or ()) <= set(FLAGS), (n.key, fa.get("flags"))
    wifi = default_like.nodes["sem:1006:5"].facets["a11y"]
    assert wifi["flags"] == ["click", "focus", "checkable", "checked"]
    heading = next(n for n in launcher.nodes.values() if n.tag == "launch_heading")
    assert heading.facets["a11y"]["flags"] == ["click", "focus"]
    assert "more" not in heading.facets["a11y"]  # screen_reader_focusable is noise


def test_a11y_unique_id_is_kept_as_the_carry_over_locator():
    windows = cs.default_like_windows()
    ok = next(v for v in cs.all_views(windows) if v.rid == "ok")
    ok.a11y = {**ok.a11y, "unique_id": "ok-button"}
    ix = cx.build_index(cs.encode(windows))
    assert ix.nodes["view:1004"].facets["a11y"]["unique_id"] == "ok-button"


def test_view_a11y_rect_is_not_repeated_in_its_facet(wide, mixed):
    for ix in (wide, mixed):
        for n in ix.nodes.values():
            fa = n.facets.get("a11y") or {}
            assert "b" not in fa or fa["b"] != n.b, n.key


# --------------------------------------------------------------------------- agent cuts
def _cut_raw(raw: RawCapture) -> tuple[RawCapture, int, int]:
    """``raw`` as a hardened agent reports a cut: depth-truncated / Compose / a11y
    tokens in the facet diagnostics, CHILDREN_TRUNCATED on a View, TEXT_REDACTED
    on another, children_truncated on an a11y node. Returns (raw, cut view id,
    redacted view id)."""
    import copy

    out = copy.deepcopy(raw)
    views = pb.DumpTreeResponse.FromString(raw.views)
    views.diagnostics = ("depth-truncated=3 (children below 80 levels not sent); "
                         "properties-failed=2")
    root = views.roots[0]
    cut, red = root.children[0], root.children[0].children[0] if root.children[0].children \
        else root
    cut.flags |= pb.ViewNode.CHILDREN_TRUNCATED
    red.flags |= pb.ViewNode.TEXT_REDACTED
    out.views = views.SerializeToString()
    comp = pb.DumpComposeResponse.FromString(raw.compose_sem)
    comp.diagnostics = (comp.diagnostics + "; semantics_truncated: view#7 depth>80 subtrees=2; "
                        "semantics_failed: view#9 owner_unreachable; "
                        "redaction_unverified: view#9 (password fields cannot be identified)")
    out.compose_sem = comp.SerializeToString()
    a11y = pb.DumpA11yResponse.FromString(raw.a11y)
    a11y.diagnostics = (a11y.diagnostics or "nodes=1") + "; depth-truncated=4 (children below " \
                                                         "80 levels not sent)"
    out.a11y = a11y.SerializeToString()
    return out, int(cut.id), int(red.id)


def test_the_index_reports_what_the_agent_cut(viewscreen_raw):
    """A capture never presents a cut tree as complete (backlog: agent-hardening):
    the agent's tokens lead ix.diagnostics, and the flagged nodes carry
    truncated / redacted (flags and view facet)."""
    raw, cut, red = _cut_raw(viewscreen_raw)
    ix = cx.build_index(raw)
    assert ix.diagnostics[:7] == [
        "views: depth-truncated=3 (children below 80 levels not sent)",
        "views: properties-failed=2",
        "compose: semantics_truncated: view#7 depth>80 subtrees=2",
        "compose: semantics_failed: view#9 owner_unreachable",
        "compose: redaction_unverified: view#9 (password fields cannot be identified)",
        "a11y: depth-truncated=4 (children below 80 levels not sent)",
        'views: 1 View(s) have children the agent did not send (depth cap): '
        'find(flags=["truncated"])']
    cut_node, red_node = ix.nodes[f"view:{cut}"], ix.nodes[f"view:{red}"]
    assert "truncated" in cut_node.flags and cut_node.facets["view"]["children_truncated"]
    assert "redacted" in red_node.flags and red_node.facets["view"]["text_redacted"]
    assert "truncated" in FLAGS and "redacted" in FLAGS
    # an untouched capture says nothing of the kind
    clean = cx.build_index(viewscreen_raw)
    assert not any(d.startswith(("views: depth", "compose: sem")) for d in clean.diagnostics)
    assert not any("truncated" in n.flags for n in clean.nodes.values())


def test_a11y_children_truncated_is_a_flag(viewscreen_raw):
    import copy

    raw = copy.deepcopy(viewscreen_raw)
    a11y = pb.DumpA11yResponse.FromString(raw.a11y)
    node = a11y.windows[0].root
    node.children_truncated = True
    raw.a11y = a11y.SerializeToString()
    ix = cx.build_index(raw)
    flagged = [n for n in ix.nodes.values() if "truncated" in n.flags]
    assert flagged and all("truncated" in (n.facets.get("a11y") or {}).get("flags", [])
                           for n in flagged if "a11y" in n.facets)


def test_unlabelled_controls_in_list_cells_get_a_durable_sel():
    """An unlabelled Button @delete repeated in every cell (its tag is not unique,
    and a sibling is another Button) is selected through its cell, not its ref,
    so a stale-ref error can send the agent somewhere (review: anchors)."""
    from capture_builders import IndexBuilder

    b = IndexBuilder()
    w = b.window("n1")
    lst = b.view(w, "n2", "RecyclerView", (0, 0, 400, 800), rid="list")
    for k in range(3):
        cell = b.view(lst, f"n{10 + 10 * k}", "ComposeView", (0, 100 * k, 400, 100),
                      tag=f"cell_{k}")
        b.view(cell, f"n{11 + 10 * k}", "Button", (300, 100 * k, 50, 50), tag="delete")
        b.view(cell, f"n{12 + 10 * k}", "Button", (350, 100 * k, 50, 50), tag="archive")
        row = b.view(cell, f"n{13 + 10 * k}", "Row", (0, 100 * k, 300, 50))
        b.view(row, f"n{14 + 10 * k}", "Icon", (0, 100 * k, 50, 50), tag="star")
        b.view(row, f"n{15 + 10 * k}", "Icon", (50, 100 * k, 50, 50), tag="flag")
    ix = b.build()
    anchors.assign_sels(ix)
    assert ix.nodes["n21"].sel == "@cell_1 > @delete"
    assert ix.nodes["n22"].sel == "@cell_1 > @archive"
    # through an unlabelled row: the grandparent, the row's unique atom, the tag
    assert ix.nodes["n24"].sel == "@cell_1 > Row > @star"
    for n in ix.nodes.values():
        if n.sel != n.id:
            assert anchors.match_sel(ix, n.sel) == [n.id], n.sel
