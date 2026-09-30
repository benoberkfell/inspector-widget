"""Real captures replayed offline (WP L1): budgets, correlation quality, windows,
carry-over, and the live bugs found while recording them.

Each fixture in ``tests/fixtures/captures`` (see ``capture_replay.py``) is served
over the harness fake adb and agent as the recording device served it (package,
pid, density), and the whole capture pipeline and every query tool run over it.
"""

from __future__ import annotations

import os
import re

import capture_harness as ch
import capture_replay as cr
import pytest
from capture_harness import nbytes, ok
from test_capture_budgets import BUDGET, _calls

import fakeagent
from inspector_widget import ops
from inspector_widget.capture import index as cindex
from inspector_widget.proto import view_inspection_pb2 as pb

NAMES = cr.names()
#: The two-window fixtures: (name, the dialog window's DecorView class).
DIALOGS = ("a11yprobe_d1", "nia_settings")


def run(ctx: ops.OpContext, tool: str, **args) -> dict:
    return ok(ops.run(ctx, tool, args))


class Replay:
    """``with Replay(name, tmp) as r``: the harness serving fixture ``name``."""

    def __init__(self, name: str, tmp: str) -> None:
        self.rec = cr.load(name)
        self._cm = ch.harness(cr.scene(self.rec), tmp)

    def __enter__(self) -> Replay:
        self.dev, self.data = self._cm.__enter__()
        cr.replay_device(self.dev, self.rec)
        self.ctx = ch.ops_context()
        return self

    def __exit__(self, *exc) -> None:
        self.ctx.sessions.close_all()
        self._cm.__exit__(*exc)

    def capture(self, **args) -> dict:
        return run(self.ctx, "capture", serial=ch.SERIAL, package=self.rec.package, **args)

    def index(self, cid: str):
        return self.ctx.store.load(cid).index()


def test_every_fixture_is_recorded_whole():
    assert len(NAMES) >= 10
    for name in NAMES:
        rec = cr.load(name)
        assert {"windows", "views", "a11y"} <= set(rec.raw), name
        assert rec.meta["api"] == 37 and rec.dpi == 480, name
        assert set(rec.window_ids()) == set(rec.shots), name  # one screenshot per window
        assert rec.source.get("app") and rec.source.get("how"), name
        # each fixture stays small; screenshots are the recorded full-resolution ones
        size = sum(os.path.getsize(os.path.join(dp, f))
                   for dp, _d, fs in os.walk(rec.path) for f in fs)
        assert size < 3 << 20, (name, size)
        for data in rec.shots.values():
            shot = pb.Screenshot.FromString(data)
            assert shot.width == 1280 or shot.height == 2856 or shot.width < 1280, name


def test_from_capture_serves_the_recorded_replies():
    agent = fakeagent.FakeAgent.from_capture("a11yprobe_viewscreen")
    try:
        _delay, resp = agent.behaviour(pb.Request(id=7, dump_tree=pb.DumpTreeCommand(
            include_properties=True)))
        rec = cr.load("a11yprobe_viewscreen")
        want = pb.DumpTreeResponse.FromString(rec.raw["views"])
        assert resp.id == 7 and resp.dump_tree.roots[0].id == want.roots[0].id
        assert len(resp.dump_tree.properties) == len(want.properties)
        _delay, shot = agent.behaviour(pb.Request(id=8, screenshot=pb.ScreenshotCommand()))
        assert shot.screenshot.screenshot.data == pb.Screenshot.FromString(
            rec.shots[rec.window_ids()[0]]).data
    finally:
        agent.stop()


@pytest.mark.parametrize("name", NAMES)
def test_replayed_real_captures_meet_the_budgets(tmp_path, name):
    """Spec section 7 on real data: capture and every query tool, default
    arguments, within the tool's budget; at most 3 next hints of <= 200 B."""
    pytest.importorskip("PIL")  # the overlays
    with Replay(name, str(tmp_path)) as r:
        first = r.capture()
        assert nbytes(first) <= BUDGET["capture"], nbytes(first)
        again = r.capture(diff_from="prev")
        assert nbytes(again) <= BUDGET["capture"], nbytes(again)
        for key, tool, args in _calls(r.ctx, again["capture"]):
            doc = run(r.ctx, tool, **args)
            assert nbytes(doc) <= BUDGET[key], (name, tool, args, nbytes(doc))
            nxt = doc.get("next") or []
            assert len(nxt) <= 3 and len(str(nxt)) <= 260, (tool, args, nxt)


@pytest.mark.parametrize("name", NAMES)
def test_post_id1_fixtures_correlate_exactly(tmp_path, name):
    """Spec 13.5 / WP L1: on post-ID1 data at least 95% of a11y facets are
    conf exact, and the ID1 fallback diagnostic is absent."""
    with Replay(name, str(tmp_path)) as r:
        ix = r.index(r.capture()["capture"])
        a11y = [n for n in ix.nodes.values() if "a11y" in n.conf]
        exact = [n for n in a11y if n.conf["a11y"] == "exact"]
        assert a11y and len(exact) / len(a11y) >= 0.95, (name, len(exact), len(a11y))
        assert cindex.ID1_DUPLICATES not in ix.diagnostics
        assert cindex.ID1_IMPLAUSIBLE not in ix.diagnostics


def test_launcher_targets_on_the_real_capture(tmp_path):
    """Spec 13.2's launcher targets, on the recorded post-ID1 launcher."""
    with Replay("a11yprobe_launcher", str(tmp_path)) as r:
        assert nbytes(r.capture()) <= 2500
        assert nbytes(run(r.ctx, "outline")) <= 2500
        listing = run(r.ctx, "outline", root="@launcher_list")
        assert nbytes(listing) <= 2000 and listing["shown"] == listing["total"]
        assert nbytes(run(r.ctx, "outline", view="reading")) <= 2000
        found = run(r.ctx, "find", text="state", flags=["click"])
        assert nbytes(found) <= 600 and found["total"] == 2
        assert nbytes(run(r.ctx, "node", ref="@launch_heading")) <= 1500
        assert nbytes(run(r.ctx, "lint")) <= 1200
        assert nbytes(run(r.ctx, "captures")) <= 400
        assert nbytes(run(r.ctx, "image", ref="@launch_heading")) <= 400


def test_launcher_slot_table_fits(tmp_path):
    with Replay("a11yprobe_launcher_slots", str(tmp_path)) as r:
        cap = r.capture()
        assert cap["facets"]["slots"] > 100
        slots = run(r.ctx, "outline", view="slots")
        assert nbytes(slots) <= 6000
        assert any(re.search(r"src=MainActivity\.kt:\d+", ln) for ln in slots["lines"])


def test_viewscreen_targets_on_the_real_capture(tmp_path):
    with Replay("a11yprobe_viewscreen", str(tmp_path)) as r:
        cid = r.capture()["capture"]
        out = run(r.ctx, "outline")
        assert nbytes(out) <= 3000 and "truncated" not in out
        ix = r.index(cid)
        views = {n.id for n in ix.nodes.values() if n.kind == "view"}
        shown = set(re.findall(r"\bn\d+\b", " ".join(out["lines"]))) & views
        hidden = sum((out.get("hidden") or {}).values())
        assert len(views) == 47 and len(shown) + hidden >= len(views)
        node = run(r.ctx, "node", ref="#badSwitch", props="nondefault")
        assert nbytes(node) <= 1200 and "checked" in node["props"]["values"]


@pytest.mark.parametrize("name", DIALOGS)
def test_a_dialog_is_its_own_window_with_its_own_crop(tmp_path, name):
    """The dialog window is z1 with its own screenshot; a crop of a node inside
    it comes from that screenshot, and the main window under the modal dialog
    has no TalkBack stops."""
    with Replay(name, str(tmp_path)) as r:
        cap = r.capture()
        assert len(cap["windows"]) == 2 and cap["windows"][1].endswith("z1")
        assert cap["facets"]["shots"] == 2
        ix = r.index(cap["capture"])
        top = max(ix.windows(), key=lambda w: w.z)
        inside = [n for n in ix.nodes.values()
                  if n.window == top.id and n.id != top.id and "click" in n.flags
                  and n.b and n.b[2] > 0 and n.b[3] > 0]
        assert inside
        node = inside[0]
        img = run(r.ctx, "image", ref=node.id, pad=0)
        assert img["window"] == top.id and img["from"] == "screenshot"
        assert img["px"] == [node.b[2], node.b[3]]
        stops = [n for n in ix.nodes.values() if n.stop is not None]
        assert stops and all(n.window == top.id for n in stops)


def test_scrolled_recyclerview_cells_are_rebound_never_silently_reused(tmp_path):
    """S1 before and after a fling, same process: recycled cells come back with
    new refs and rebound_of; a ref that stays names the same item."""
    with Replay("a11yprobe_s1_a", str(tmp_path)) as r:
        a = r.capture()["capture"]
        r.dev.behaviour = ch._with_build_id(cr.scene("a11yprobe_s1_b"),
                                            r.dev.default_build_id)
        for agent in r.dev.agents:
            agent.behaviour = r.dev.behaviour
        b_doc = r.capture(diff_from="prev")
        assert b_doc["diff"]["summary"]["rebound"] > 0
        ia, ib = r.index(a), r.index(b_doc["capture"])
        rebound = [n for n in ib.nodes.values() if n.rebound_of]
        assert rebound and all(n.rebound_of in ia.nodes for n in rebound)
        assert all(n.id not in ia.nodes for n in rebound)
        kept = set(ia.nodes) & set(ib.nodes)
        assert len(kept) > 50
        moved_items = [x for x in kept if ia.nodes[x].label and ib.nodes[x].label
                       and ia.nodes[x].label != ib.nodes[x].label]
        assert moved_items == []


def test_thunderbird_message_rows_have_no_false_clips(tmp_path):
    """Live: every row's #star_click_area was 'clipped' by the median of the row's
    other plain Views (fixed in analyzers._clipped_inferred)."""
    with Replay("thunderbird_list_views", str(tmp_path)) as r:
        ix = r.index(r.capture()["capture"])
        clipped = [n for n in ix.nodes.values()
                   if any(i.id == "render.clipped" for i in n.issues)]
        assert [n.rid for n in clipped if n.rid == "star_click_area"] == []
        rows = run(r.ctx, "find", rid="star_click_area")
        assert rows["total"] >= 6


def test_hardened_agent_actions_are_actions_not_values(tmp_path):
    """Live on Thunderbird's Compose rows: SafeString's "<action>" values must
    become the compose facet's actions list (normalize.is_action_attr)."""
    with Replay("thunderbird_list_compose", str(tmp_path)) as r:
        ix = r.index(r.capture()["capture"])
        facets = [n.facets["compose"] for n in ix.nodes.values() if "compose" in n.facets]
        assert facets
        assert not any("<action>" in str(f.get("attrs") or {}) for f in facets)
        assert any("OnClick" in (f.get("actions") or []) for f in facets)
        star = run(r.ctx, "find", tag="MessageItem_FavouriteButtonIcon")
        assert star["total"] >= 6
        node = run(r.ctx, "node", ref=star["lines"][0].split()[0])
        assert "OnClick" in node["compose"]["actions"]


#: What a bare widget still shows as non-default: set by the theme (colours, text
#: size, drawables, letter spacing) or in dp (sizes, paddings), so not static.
THEME_OR_DP = frozenset({
    "layout_width", "layout_height", "lineHeight", "textSize", "textColor",
    "textColorHighlight", "textColorHint", "textColorLink", "background", "backgroundTint",
    "elevation", "stateListAnimator", "minHeight", "minWidth", "maxWidth", "paddingTop",
    "paddingBottom", "paddingLeft", "paddingRight", "drawablePadding", "iconPadding",
    "letterSpacing", "button", "buttonTint", "thumb", "track", "textOn", "textOff",
})


def test_bare_widgets_show_only_theme_and_density_values(tmp_path):
    """normalize_defaults.STATIC_VIEW_DEFAULTS, re-recorded live (WP L1): on bare
    widgets (framework and AppCompat/Material classes) every other property is
    at its static default."""
    from inspector_widget import normalize as nz

    with Replay("a11yprobe_view_defaults", str(tmp_path)) as r:
        cid = r.capture()["capture"]
        lc = r.ctx.store.load(cid)
        ix = lc.index()
        widgets = [c for col in ("defaults_bare", "defaults_inflated")
                   for n in ix.nodes.values() if n.rid == col for c in n.children]
        assert len(widgets) == 20
        classes = set()
        for ref in widgets:
            n = ix.nodes[ref]
            cls = n.facets["view"].get("qualified") or n.facets["view"]["class"]
            classes.add(cls.rsplit(".", 1)[-1])
            props = lc.props(int(n.ids["view"]))
            kept, _omitted = nz.nondefault_props({1: props}, {1: cls}, bounds={1: n.b},
                                                 majority_min=10 ** 9)
            assert set(kept[1]) <= THEME_OR_DP, (cls, sorted(set(kept[1]) - THEME_OR_DP))
        assert {"TextView", "MaterialTextView", "Button", "MaterialButton", "Switch",
                "ImageView", "AppCompatImageView", "EditText", "CheckBox"} <= classes


def test_empty_compose_plumbing_takes_no_outline_line(tmp_path):
    """Live on Thunderbird's ComposeView rows: each row's empty
    AndroidViewsHandler took an outline line (6 of 29); it collapses now, and
    detail="all" still lists it."""
    with Replay("thunderbird_list_compose", str(tmp_path)) as r:
        r.capture()
        out = run(r.ctx, "outline", root="#message_list", depth=4, max_lines=80)
        assert out["lines"] and not any("AndroidViewsHandler" in ln for ln in out["lines"])
        assert any("@MessageItem_FavouriteButtonIcon" in ln for ln in out["lines"])
        full = run(r.ctx, "outline", root="#message_list", depth=4, detail="all",
                   max_lines=400)
        assert any("AndroidViewsHandler" in ln for ln in full["lines"])


def test_the_screen_composite_shows_the_window_under_a_dialogs_transparent_edge(tmp_path):
    """Live on Now in Android's Settings dialog: the dialog window is transparent
    outside its card, and the composited screen showed black there instead of
    the For you screen under it."""
    pytest.importorskip("PIL")
    from PIL import Image

    with Replay("nia_settings", str(tmp_path)) as r:
        r.capture()
        doc = run(r.ctx, "image", overlay="none", max_side=4096)
        img = Image.open(doc["path"]).convert("RGBA")
        f = img.width / 1280
        x, y = int(40 * f), int(1500 * f)  # left of the card, inside the dialog window
        assert img.getpixel((x, y))[:3] != (0, 0, 0)
        main = cr.load("nia_settings")
        shot = pb.Screenshot.FromString(main.shots[main.window_ids()[0]])
        assert shot.width == 1280  # the main window's full-screen screenshot
