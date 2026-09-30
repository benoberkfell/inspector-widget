"""Analyzers (C7): render signals, the rule catalog, the lint adapter with ref
mapping and template collapse, reading order, and the lint view.

Everything runs offline on ``capture_builders`` indexes plus raw facets from the
real launcher recording (``tests/fixtures/live``) or built from the index itself
(``loaded_fakes``). The lint adapter tests run the installed ``a11y_lint``; the
lint_view mechanics (grouping, template collapse, cursors, budgets) run on the
spec's launcher example with its hand-made findings, so they do not move when
the lint's rules do.
"""

from __future__ import annotations

import json
import random
import re

import capture_builders as cb
import fakescenes as fs
import live_fixtures as lf
import pytest
from loaded_fakes import FakeLoaded, a11y_pb_from_index, compose_pb_from_index, screen_of

from inspector_widget import a11y_lint
from inspector_widget.capture import analyzers as an
from inspector_widget.capture import rules as R
from inspector_widget.capture.model import Issue, OpError, RawCapture
from inspector_widget.output import dumps
from inspector_widget.proto import view_inspection_pb2 as pb

ROLE = cb.ROLE_RULE
TOUCH = cb.TOUCH_RULE
STATE = cb.STATE_RULE


def _issues(ix, prefix: str = "") -> dict[str, list[tuple[str, str]]]:
    return {n.ref: sorted((i.id, i.sev) for i in n.issues if i.id.startswith(prefix))
            for n in ix.nodes.values() if any(i.id.startswith(prefix) for i in n.issues)}


def _clear(ix) -> None:
    for n in ix.nodes.values():
        n.issues = []


def _real_compose() -> bytes:
    return fs.compose_to_pb(fs._strip_mcp_keys(lf.load("launcher", "compose_sem"))
                            ).SerializeToString()


def _real_views() -> bytes:
    return fs.views_to_pb(lf.load("launcher", "views_props")).SerializeToString()


def _launcher_loaded(ix=None, **raw):
    ix = ix if ix is not None else cb.launcher_index()
    base = {"compose_sem": _real_compose(), "a11y": a11y_pb_from_index(cb.launcher_index())
            .SerializeToString(), "views": _real_views()}
    base.update(raw)
    return ix, FakeLoaded(ix, raw=base)


def _spec_launcher():
    """The spec's launcher example as ``capture_builders`` writes it, with its
    hand-made findings (14 warnings: 12 role, 1 state, 1 touch target, plus the
    clipped row n22): the lint_view mechanics fixture."""
    ix = cb.launcher_index()
    return ix, FakeLoaded(ix)


def _views_raw(props: dict[int, dict[str, object]]) -> bytes:
    """A views facet carrying only visibility/alpha properties per view udid."""
    data = {"roots": [], "properties": {
        vid: [{"name": k, "type": "INT_ENUM" if k == "visibility" else "FLOAT", "value": v}
              for k, v in p.items()] for vid, p in props.items()}}
    return fs.views_to_pb(data).SerializeToString()


# --------------------------------------------------------------------------- #
# Rule catalog
# --------------------------------------------------------------------------- #
def test_catalog_covers_every_rule_the_lint_emits():
    for rid in a11y_lint.ALL_RULE_IDS:
        assert rid in R.RULES, rid
    for rule in R.RULES.values():
        assert re.fullmatch(r"(a11y\.[a-z_]+\.[a-z_]+|render\.[a-z_]+)", rule.id)
        assert rule.sev in R.SEVERITIES and rule.msg
    assert [R.ALIASES[f"R{i}"] for i in range(1, 13)] == [
        "a11y.label.missing", "a11y.touch_target.small", "a11y.contrast.low",
        "a11y.label.redundant", "a11y.role.missing_on_clickable", "a11y.image.no_description",
        "a11y.state.not_exposed", "a11y.node.empty_focusable", "a11y.heading.structure",
        "a11y.grouping.missing", "a11y.text.fixed_scaling", "a11y.duplicate.label"]


def test_short_codes():
    assert R.short("a11y.touch_target.small") == "touch_target"
    assert R.short("a11y.role.missing_on_clickable") == "role"
    assert R.short("render.clipped") == "clipped"
    assert R.short("render.zero_size") == "zero_size"
    assert R.short("a11y.brand.new_rule") == "brand"  # unknown ids still get one


def test_one_short_code_never_names_two_rules():
    codes = [r.short for r in R.RULES.values()]
    assert len(codes) == len(set(codes))
    assert R.short("a11y.label.missing") == "label_missing"
    assert R.short("a11y.label.redundant") == "label_redundant"
    assert R.short("a11y.text.fixed_scaling") == "text_fixed_scaling"
    assert R.short("a11y.text.too_small") == "text_too_small"
    # the bare group still selects the whole group
    assert R.resolve("label") == ["a11y.label.missing", "a11y.label.redundant"]
    assert R.resolve("label_redundant") == ["a11y.label.redundant"]


def test_resolve_accepts_ids_aliases_shorts_families_and_atf_names():
    assert R.resolve(["R5"]) == [ROLE]
    assert R.resolve("r5") == [ROLE]
    assert R.resolve([ROLE]) == [ROLE]
    assert R.resolve("TouchTargetSize") == [TOUCH]
    assert R.resolve("clipped") == ["render.clipped"]
    assert set(R.resolve("label")) == {"a11y.label.missing", "a11y.label.redundant"}
    assert set(R.resolve("render.")) == {r for r in R.RULES if r.startswith("render.")}
    assert R.resolve("R5,R7") == [ROLE, STATE]
    assert R.resolve(None) is None and R.resolve([]) is None


def test_unknown_rule_is_bad_args():
    with pytest.raises(OpError) as e:
        R.resolve(["bogus"])
    assert e.value.code == "bad_args" and "bogus" in e.value.message
    assert "R5=" + ROLE in e.value.hint
    ix, loaded = _launcher_loaded()
    with pytest.raises(OpError) as e:
        an.lint_view(ix, loaded, rules=["bogus"])
    assert e.value.code == "bad_args"


def test_r5_alias_selects_its_rule_in_lint_view():
    ix, loaded = _launcher_loaded()
    out = an.lint_view(ix, loaded, rules=["R5"])
    assert [r["rule"] for r in out["rules"]] == [ROLE]
    assert out["rules"][0]["n"] == 12


# --------------------------------------------------------------------------- #
# Render signals
# --------------------------------------------------------------------------- #
def _render(ix, props=None):
    return {nid: [i.to_dict() for i in iss] for nid, iss in an.render_signals(ix, props).items()}


def test_clipped_exact_from_declared_box():
    ix = cb.launcher_index()
    out = _render(ix)
    assert list(out) == ["n22"]
    (iss,) = out["n22"]
    assert iss["id"] == "render.clipped" and iss["sev"] == "info" and "conf" not in iss
    assert iss["evidence"] == {"visible_px": 27, "declared_px": 216, "visible": 0.125,
                               "clipped_by": "n10", "edge": "bottom", "scroll": True}


def test_clipped_inferred_at_scroll_edge_without_declared_box():
    ix = cb.launcher_index()
    n22 = ix.get("n22")
    n22.declared_b, n22.visible = None, None
    (iss,) = _render(ix)["n22"]
    assert iss["conf"] == "inferred"
    assert iss["evidence"] == {"visible_px": 27, "declared_px": 216, "est": "sibling median",
                               "clipped_by": "n10", "edge": "bottom", "scroll": True}


def test_no_inferred_clip_without_enough_same_type_siblings():
    ix = cb.launcher_index()
    n22 = ix.get("n22")
    n22.declared_b, n22.visible = None, None
    for ref in ("n12", "n13", "n14", "n15", "n16", "n17", "n18", "n19", "n20", "n21"):
        ix.get(ref).type = "Other"
    assert _render(ix) == {}  # one same-type sibling is not a baseline


def _message_rows(collection=None):
    """Thunderbird's message list (live, emulator-5558): each row is a
    ConstraintLayout of parts, three of them plain Views (#divider,
    #star_click_area, #contact_picture_click_area). A RecyclerView says only
    SCROLL_FORWARD, so without its CollectionInfo nothing names the scroll axis."""
    b = cb.IndexBuilder("ctbrow", package="net.thunderbird.android.debug", screen=(1280, 2856))
    w = b.window("n1", "DecorView", (0, 0, 1280, 2856), udid=1)
    a11y = {"actions": ["SCROLL_FORWARD"]}
    if collection:
        a11y["collection"] = collection
    rv = b.view(w, "n2", "RecyclerView", (0, 348, 1280, 2436), udid=2, rid="message_list",
                flags=["scroll"], facets={"view": {"class": "RecyclerView"}, "a11y": a11y})
    for i, y in enumerate((348, 613)):
        row = b.view(rv, f"n{10 + 10 * i}", "ConstraintLayout", (0, y, 1280, 265),
                     udid=10 + 10 * i, label=f"Message {i}", flags=["click"])
        b.view(row, None, "View", (216, y + 262, 1064, 3), udid=11 + 10 * i, rid="divider")
        b.view(row, None, "View", (1136, y, 144, 265), udid=12 + 10 * i,
               rid="star_click_area", label="Add star", flags=["click"])
        b.view(row, None, "View", (0, y, 216, 265), udid=13 + 10 * i,
               rid="contact_picture_click_area", label="Select", flags=["click"])
    return b.build()


@pytest.mark.parametrize("collection", [None, {"rows": 50, "cols": 1}])
def test_a_rows_parts_are_not_clipped_by_the_median_of_other_parts(collection):
    """The star area touches the list's right edge and is far narrower than the
    row's other plain Views; they are other parts (another #rid), not look-alike
    siblings, so nothing is clipped (it was, 10 times, on the live screen)."""
    assert _render(_message_rows(collection)) == {}


def test_a_collections_orientation_names_its_scroll_axis():
    ix = _message_rows({"rows": 50, "cols": 1})
    assert an._scroll_axes(ix.get("n2")) == {"v"}
    ix = _message_rows({"rows": 1, "cols": 9})
    assert an._scroll_axes(ix.get("n2")) == {"h"}
    assert an._scroll_axes(_message_rows().get("n2")) == {"v", "h"}


def _scroll_screen():
    b = cb.IndexBuilder("cscr01", package="com.example", screen=(1080, 2000))
    w = b.window("n1", "DecorView", (0, 0, 1080, 2000), udid=1)
    sv = b.view(w, "n2", "ScrollView", (0, 200, 1080, 1000), udid=2, rid="scroller",
                flags=["scroll"])
    col = b.view(sv, "n3", "LinearLayout", (0, 200, 1080, 3000), udid=3)
    b.view(col, "n4", "Button", (0, 200, 1080, 400), udid=4, label="Top", flags=["click"])
    b.view(col, "n5", "Button", (0, 1000, 1080, 400), udid=5, label="Cut", flags=["click"])
    b.view(col, "n6", "Button", (0, 2000, 1080, 400), udid=6, label="Below", flags=["click"])
    return b


def test_view_layout_rect_past_a_scroll_viewport_is_clipped_exactly():
    ix = _scroll_screen().build()
    out = _render(ix)
    assert set(out) == {"n5"}  # n6 is scrolled out entirely: normal, not reported
    (iss,) = out["n5"]
    assert "conf" not in iss and iss["sev"] == "info"
    assert iss["evidence"] == {"visible_px": 200, "declared_px": 400, "visible": 0.5,
                               "clipped_by": "n2", "edge": "bottom", "scroll": True}


def test_clipped_by_a_non_scrolling_parent_is_a_warning():
    b = cb.IndexBuilder("cclip1", screen=(1080, 2000))
    w = b.window("n1", "DecorView", (0, 0, 1080, 2000), udid=1)
    box = b.view(w, "n2", "FrameLayout", (0, 100, 1080, 100), udid=2)
    b.view(box, "n3", "TextView", (0, 100, 1080, 100), udid=3, label="Long text",
           declared_b=[0, 100, 1080, 180], visible=0.556)
    (iss,) = _render(b.build())["n3"]
    assert iss["sev"] == "warn" and "conf" not in iss
    assert iss["evidence"]["clipped_by"] == "n2" and iss["evidence"]["edge"] == "bottom"
    assert "scroll" not in iss["evidence"]


def test_hidden_from_props_reports_the_topmost_content_node_only():
    b = cb.IndexBuilder("chid01", screen=(1080, 2000))
    w = b.window("n1", "DecorView", (0, 0, 1080, 2000), udid=1)
    panel = b.view(w, "n2", "LinearLayout", (0, 0, 1080, 500), udid=2)
    b.view(panel, "n3", "Button", (0, 0, 1080, 200), udid=3, label="Pay", flags=["click"])
    b.view(panel, "n4", "TextView", (0, 200, 1080, 200), udid=4, label="Total")
    b.view(w, "n5", "View", (0, 1900, 1080, 100), udid=5, rid="navigationBarBackground")
    b.view(w, "n6", "Button", (0, 600, 1080, 200), udid=6, label="Ghost", flags=["click"])
    ix = b.build()
    props = {2: {"visibility": "gone"}, 5: {"visibility": "invisible"}, 6: {"alpha": 0.0}}
    out = _render(ix, props)
    assert set(out) == {"n2", "n6"}  # n5 has no content; n3/n4 sit under n2
    assert out["n2"] == [{"id": "render.hidden", "sev": "info",
                          "evidence": {"why": "visibility=gone", "hides": 2}}]
    assert out["n6"][0]["evidence"] == {"why": "alpha=0"}


def test_hidden_from_the_stored_views_facet():
    ix = cb.launcher_index()
    _clear(ix)
    ix.get("n22").declared_b = None
    ix.get("n22").visible = None
    ix.get("n22").b = [0, 2757, 1280, 216]  # not clipped, so only visibility matters
    # the launcher's own bars are invisible but carry no content: not reported
    an.analyze(ix, FakeLoaded(ix, raw={"views": _real_views()}), lint="none")
    assert _issues(ix) == {}
    b = cb.IndexBuilder("chid02")
    w = b.window("n1", "DecorView", udid=1)
    b.view(w, "n2", "TextView", (0, 0, 500, 100), udid=77, label="Status")
    ix2 = b.build()
    an.analyze(ix2, FakeLoaded(ix2, raw={"views": _real_views()}), lint="none")
    assert _issues(ix2) == {"n2": [("render.hidden", "info")]}
    assert ix2.get("n2").issues[0].evidence == {"why": "visibility=invisible"}


def test_hidden_from_accessibility_is_inferred():
    b = cb.IndexBuilder("chid03")
    w = b.window("n1", "DecorView", udid=1)
    v = b.view(w, "n2", "TextView", (0, 0, 500, 100), udid=2, label="Secret")
    b.a11y_facet(v, host=2, virt=-1, flags=["hidden"])
    (iss,) = _render(b.build())["n2"]
    assert iss == {"id": "render.hidden", "sev": "info",
                   "evidence": {"why": "not visible to accessibility"}, "conf": "inferred"}


def test_offscreen_and_zero_size():
    b = cb.IndexBuilder("coff01", screen=(1080, 2000))
    w = b.window("n1", "DecorView", (0, 0, 1080, 2000), udid=1)
    drawer = b.view(w, "n2", "LinearLayout", (-900, 0, 900, 2000), udid=2)
    b.view(drawer, "n3", "TextView", (-900, 0, 900, 100), udid=3, label="Menu")
    b.view(w, "n4", "Button", (100, 100, 0, 0), udid=4, label="Tiny", flags=["click"])
    b.view(w, "n5", "ViewStub", (0, 0, 0, 0), udid=5, rid="stub")
    b.view(w, "n6", "FrameLayout", (0, 0, 0, 0), udid=6)
    out = _render(b.build())
    assert set(out) == {"n2", "n4"}  # the drawer's text is under the reported drawer
    assert out["n2"] == [{"id": "render.offscreen", "sev": "info",
                          "evidence": {"rect": [-900, 0, 900, 2000], "outside": "window"}}]
    assert out["n4"] == [{"id": "render.zero_size", "sev": "info", "evidence": {"w": 0, "h": 0}}]


def test_one_issue_per_subtree_on_the_wide_scene():
    # The E6 geometry puts children outside the 300x60 root window: n2 overlaps it
    # (clipped), the other depth-1 subtrees lie entirely outside (offscreen). Nothing
    # below a reported node is reported again.
    out = _render(cb.wide_index())
    assert set(out) == {"n2", "n45", "n88", "n131", "n174", "n217"}
    assert out["n2"][0]["id"] == "render.clipped" and out["n2"][0]["conf"] == "inferred"
    assert out["n2"][0]["sev"] == "warn" and out["n2"][0]["evidence"]["clipped_by"] == "n1"
    assert {out[r][0]["id"] for r in ("n45", "n88", "n131", "n174", "n217")} == {
        "render.offscreen"}


# --------------------------------------------------------------------------- #
# The lint adapter on the real launcher
# --------------------------------------------------------------------------- #
def test_launcher_lint_maps_every_finding_to_its_node():
    """The capture's lint is ``a11y_lint.run_lint`` over the stored unified a11y tree,
    so it finds what the live a11y_lint tool finds on the same dump, and every
    finding lands on its node. The launcher's rows sit in a collection, so the
    lint no longer asks them for a role (R5); the screen has no heading (R9), and
    the last row, clipped at the list's edge, is a small touch target."""
    from inspector_widget import a11y

    ix, loaded = _launcher_loaded()
    _clear(ix)
    an.analyze(ix, loaded, lint="tree", density=480, font_scale=1.0)
    assert not any(d.startswith("lint:") for d in ix.diagnostics), ix.diagnostics
    assert _issues(ix) == {"n1": [("a11y.heading.structure", "info")],
                           "n22": [(TOUCH, "info"), ("render.clipped", "info")]}
    live = a11y_lint.run_lint(
        None, density=480, include_contrast=False,
        a11y_data=a11y.a11y_to_dict(pb.DumpA11yResponse.FromString(loaded.raw("a11y"))),
        compose_data=an._Src(loaded).compose_dict())
    assert sorted((f.rule, f.severity) for f in live.findings) == sorted(
        (i.id, i.sev) for n in ix.nodes.values() for i in n.issues if i.id.startswith("a11y."))


def test_view_screens_are_linted_too():
    """The unified a11y tree covers Views: the recorded View screen (no Compose at
    all) gets findings, each on a View node."""
    import capture_scenes as cs

    from inspector_widget.capture import index as cx

    raw = cs.raw_from_scene(fs.replay_scene("viewscreen"))
    ix = cx.build_index(raw)
    an.analyze(ix, raw, lint="tree")
    assert not any(d.startswith("lint:") for d in ix.diagnostics), ix.diagnostics
    found = [(n, i) for n in ix.nodes.values() for i in n.issues if i.id.startswith("a11y.")]
    assert found and all(n.kind == "view" for n, _ in found)
    # the same-label group names the other nodes by node id, never by the lint's keys
    groups = [(n, i) for n, i in found if i.id == "a11y.duplicate.label"]
    assert groups and all("duplicates" not in i.evidence for _, i in groups)
    assert all(i.evidence["node_ids"] and n.id not in i.evidence["node_ids"]
               and all(x in ix.nodes for x in i.evidence["node_ids"]) for n, i in groups)


def test_touch_target_on_scroll_clipped_node_is_a_likely_false_positive():
    ix, loaded = _launcher_loaded()
    _clear(ix)
    an.analyze(ix, loaded)
    touch = [i for i in ix.get("n22").issues if i.id == TOUCH]
    assert touch[0].evidence["note"] == "likely false positive: clipped at scroll edge"
    assert touch[0].evidence["w_dp"] == 426.7 and touch[0].evidence["h_dp"] == 9.0


def test_analyze_is_idempotent_and_keeps_foreign_issues():
    ix, loaded = _launcher_loaded()
    ix.get("n9").issues.append(Issue("custom.note", "info"))
    an.analyze(ix, loaded)
    first = {n.ref: [i.to_dict() for i in n.issues] for n in ix.nodes.values()}
    an.analyze(ix, loaded)
    assert {n.ref: [i.to_dict() for i in n.issues] for n in ix.nodes.values()} == first
    assert [i.id for i in ix.get("n9").issues] == ["custom.note"]


def test_lint_none_writes_render_issues_and_reading_only():
    ix, loaded = _launcher_loaded()
    an.analyze(ix, loaded, lint="none")
    assert _issues(ix) == {"n22": [("render.clipped", "info")]}
    assert ix.reading[0] == "n9" and len(ix.reading) == 13
    with pytest.raises(OpError):
        an.analyze(ix, loaded, lint="bogus")


def test_analyze_accepts_a_raw_capture_before_publish():
    ix = cb.launcher_index()
    _clear(ix)
    raw = RawCapture(meta=ix.meta, compose_sem=_real_compose(),
                     a11y=a11y_pb_from_index(cb.launcher_index()).SerializeToString())
    an.analyze(ix, raw, lint="tree")
    assert sorted(_issues(ix, "a11y.")) == ["n1", "n22"]


def test_no_a11y_tree_means_no_lint_and_says_so():
    ix = cb.launcher_index()
    _clear(ix)
    an.analyze(ix, FakeLoaded(ix, raw={"compose_sem": _real_compose()}))
    assert _issues(ix, "a11y.") == {}
    assert "lint: not run: no accessibility tree" in ix.diagnostics


def test_analyze_without_facets_runs_render_signals_only():
    ix = cb.launcher_index()
    reading = list(ix.reading)
    _clear(ix)
    an.analyze(ix, None)
    assert _issues(ix) == {"n22": [("render.clipped", "info")]}
    assert ix.reading == reading  # no a11y facet: the reading order is left alone


ACV_CLASS = "androidx.compose.ui.platform.AndroidComposeView"


def _raw_of(ix) -> dict[str, bytes]:
    """The a11y and Compose semantics facets a capture of ``ix`` would carry."""
    return {"a11y": a11y_pb_from_index(ix).SerializeToString(),
            "compose_sem": compose_pb_from_index(ix).SerializeToString()}


def _two_compose_views():
    """Two ComposeViews whose semantics ids collide (ID3): each has a clickable,
    role-less node with semantics id 5."""
    b = cb.IndexBuilder("cid301", screen=(1080, 2000))
    w = b.window("n1", "DecorView", (0, 0, 1080, 2000), udid=1)
    b.a11y_facet(w, host=1, virt=-1, **{"class": "android.widget.FrameLayout"})
    for i, (acv, y) in enumerate(((82, 0), (182, 1000))):
        host = b.view(w, f"n{2 + i}", "AndroidComposeView", (0, y, 1080, 1000), udid=acv,
                      cls="AndroidComposeView")
        b.a11y_facet(host, host=acv, virt=-1, **{"class": ACV_CLASS})
        b.compose(host, f"n{10 + i}", sem_id=5, b=(0, y + 100, 1080, 200), acv=acv,
                  label=f"Row {i}", flags=["click"],
                  attrs={"Text": f"Row {i}", "OnClick": "AccessibilityAction"})
        b.a11y_facet(f"n{10 + i}", host=acv, virt=5, flags=["click", "focus"],
                     **{"class": "android.view.View"})
    return b.build()


def test_composite_keys_keep_colliding_semantics_ids_apart():
    ix = _two_compose_views()
    an.analyze(ix, FakeLoaded(ix, raw=_raw_of(ix)))
    # a clickable row with visible text and no role: info (R5 warns only without text)
    assert _issues(ix, "a11y.role") == {"n10": [(ROLE, "info")], "n11": [(ROLE, "info")]}


def test_findings_on_nodes_missing_from_the_index_are_reported_not_dropped():
    ix = _two_compose_views()
    raw = _raw_of(ix)
    del ix.nodes["n11"]
    ix.rebuild_by_key()
    an.analyze(ix, FakeLoaded(ix, raw=raw))
    (diag,) = [d for d in ix.diagnostics if d.startswith("lint:")]
    assert diag.startswith("lint: 1 findings not mapped to nodes") and "compose:182:5" in diag


def test_repeated_a11y_ids_are_told_apart_by_window_and_bounds_or_not_at_all():
    """A pre-ID1 agent repeats one (host, virtual) pair across a whole window. A
    finding still lands on its node when its bounds single out one dump node (the
    index registers that node's path key), and is reported, never guessed,
    otherwise."""
    class F:
        def __init__(self, x, y, window=0):
            self.node = {"host_view_id": 1, "virtual_id": 11}
            self.bounds = {"x": x, "y": y, "w": 10, "h": 10}
            self.window = {"index": window}

    def a11y(y, kids=()):
        return {"host_view_id": 1, "virtual_id": 11, "bounds": {"layout": {
            "x": 0, "y": y, "w": 10, "h": 10}}, "children": list(kids)}

    resp = fs.a11y_to_pb({"windows": [{"root_view_id": 1, "root": {
        "host_view_id": 1, "virtual_id": -1, "bounds": {"layout": {"x": 0, "y": 0, "w": 10,
                                                                    "h": 100}},
        "children": [a11y(0), a11y(20), a11y(20)]}}]})
    dump = an._A11yDump(resp)
    ix = cb.launcher_index()
    ix.by_key["a11y:path:1:0.0"] = "n9"
    lookup = dump.mapper(ix)
    assert lookup(dump.finding_dict(F(0, 0))) == "n9"
    assert dump.finding_dict(F(0, 20)) is None  # two nodes with these bounds
    assert dump.finding_dict(F(0, 0, window=1)) is None  # not in that window


# --------------------------------------------------------------------------- #
# Reading order
# --------------------------------------------------------------------------- #
def test_reading_order_maps_talkback_stops_to_refs():
    ix, loaded = _launcher_loaded()
    expected = list(ix.reading)
    ix.reading = []
    for n in ix.nodes.values():
        n.stop = None
    an.analyze(ix, loaded)
    assert ix.reading == expected
    assert [ix.get(r).stop for r in ix.reading] == list(range(1, 14))


def _tb_node(name, cls, text=None, flags=(), kids=(), y=0, actions=()):
    """A View a11y node named ``<what>_<host view id>``."""
    d = {"host_view_id": int(name.rsplit("_", 1)[1]), "virtual_id": -1,
         "class_name": f"android.widget.{cls}",
         "flags": ["visible_to_user", "enabled", *flags], "name": name,
         "bounds": {"layout": {"x": 0, "y": y, "w": 100, "h": 40}}, "children": list(kids)}
    if text:
        d["text"] = text
    if actions:
        d["actions"] = [{"id": a} for a in actions]
    return d


def test_merged_row_text_is_not_a_stop_of_its_own():
    """TalkBack (talkback.reading_order) reads a clickable row's non-focusable text as
    part of the row's stop (RO1). A ScrollView does not merge its children: they
    are top-level scroll items, each a stop. TalkBack's isScrollable reads the
    scroll actions, not the scrollable flag, so the ScrollView here has one."""
    row = _tb_node("row_2", "LinearLayout", flags=("clickable", "focusable"), y=0,
                   kids=[_tb_node("t_3", "TextView", "Title", y=0),
                         _tb_node("t_4", "TextView", "Subtitle", y=20)])
    scroll = _tb_node("scroll_5", "ScrollView", flags=("focusable", "scrollable"), y=100,
                      actions=[0x1000],  # ACTION_SCROLL_FORWARD
                      kids=[_tb_node("t_6", "TextView", "One", y=100),
                            _tb_node("t_7", "TextView", "Two", y=140)])
    stops = an._ordered_stops([_tb_node("root_1", "FrameLayout", kids=[row, scroll])])
    assert [n.get("text") or n["name"] for _, n in stops] == ["row_2", "One", "Two"]
    assert [o for o, _ in stops] == [1, 2, 3]


def test_a_focusable_container_that_does_not_scroll_speaks_its_text_as_one_stop():
    """A focusable layout with plain text children (no scroll action) is one stop
    that speaks them, as TalkBack's shouldFocusNode decides for any focusable node
    with non-focusable speaking children."""
    box = _tb_node("box_2", "LinearLayout", flags=("focusable",), y=100,
                   kids=[_tb_node("t_3", "TextView", "One", y=100),
                         _tb_node("t_4", "TextView", "Two", y=140)])
    stops = an._ordered_stops([_tb_node("root_1", "FrameLayout", kids=[box])])
    assert [n["name"] for _, n in stops] == ["box_2"]


def test_real_launcher_reading_order_has_one_stop_per_row():
    """The recorded launcher came from a pre-ID1 agent with no accessibility service,
    so its a11y tree carries no traversal links and is in composition order: the
    TalkBack model reads the 12 rows (their Text merged), then the top bar's title.
    A live dump from a current agent carries Compose's links, and there the title
    comes first, as TalkBack 17 read it (tests/data/tb/tb17_walks.json)."""
    import gzip
    import json
    import os

    import capture_scenes as cs

    from inspector_widget.capture import index as cx

    raw = cs.raw_from_scene(fs.replay_scene("launcher"))
    ix = cx.build_index(raw)
    an.analyze(ix, raw, lint="none")
    stops = [ix.nodes[r] for r in ix.reading]
    assert len(stops) == 13
    assert all(n.type == "ListItem" and "click" in n.flags for n in stops[:12])
    assert stops[12].label == "A11yProbe"
    here = os.path.dirname(__file__)
    with gzip.open(os.path.join(here, "data", "tb", "tb17_launcher_on.json.gz")) as f:
        live = json.load(f)
    assert an._ordered_stops(live)[0][1].get("text") == "A11yProbe"


def test_reading_order_never_guesses_duplicate_pre_id1_ids():
    ix = cb.launcher_index()
    real = fs.a11y_to_pb(fs._strip_mcp_keys(lf.load("launcher", "a11y"))).SerializeToString()
    an.analyze(ix, FakeLoaded(ix, raw={"a11y": real}), lint="none")
    assert ix.reading == [] and all(n.stop is None for n in ix.nodes.values())
    (diag,) = [d for d in ix.diagnostics if d.startswith("reading:")]
    assert "not mapped" in diag and "ID1" in diag


def test_reading_order_uses_path_keys_when_ids_repeat():
    ix = cb.launcher_index()
    resp = a11y_pb_from_index(ix)
    rows = [resp.windows[0].root.children[0]] + list(resp.windows[0].root.children[1].children)
    for n in rows:  # make every compose pair collide, as pre-ID1 agents do
        n.host_view_id, n.virtual_id = 1, 11
    # path keys, as the index builder registers them under ID1: root 0, n9 0.0, n10 0.1
    ix.by_key["a11y:path:1:0.0"] = "n9"
    for i, (ref, *_rest) in enumerate(cb.LAUNCHER_ITEMS):
        ix.by_key[f"a11y:path:1:0.1.{i}"] = ref
    an.analyze(ix, FakeLoaded(ix, raw={"a11y": resp.SerializeToString()}), lint="none")
    assert ix.reading == ["n9"] + [item[0] for item in cb.LAUNCHER_ITEMS]


# --------------------------------------------------------------------------- #
# lint_view
# --------------------------------------------------------------------------- #
def _analyzed_launcher():
    ix, loaded = _launcher_loaded()
    _clear(ix)
    an.analyze(ix, loaded, lint="tree", density=480, font_scale=1.0)
    return ix, loaded


def test_launcher_grouped_lint_fits_1200_bytes():
    ix, loaded = _spec_launcher()
    out = an.lint_view(ix, loaded)
    text = dumps(out)
    assert len(text.encode("utf-8")) <= 1200, len(text.encode("utf-8"))
    assert out["counts"] == {"error": 0, "warn": 14, "info": 0}
    assert out["contrast"].startswith("not run")
    assert [(r["rule"], r["n"]) for r in out["rules"]] == [(ROLE, 12), (STATE, 1), (TOUCH, 1)]
    role = out["rules"][0]
    assert role["msg"] == R.RULES[ROLE].msg and role["fix"] == R.RULES[ROLE].fix
    assert role["nodes"][:3] == ['n11 "▶ All scenarios (lint everythin…"',
                                 'n12 "Icon button label, MissingConte…"',
                                 'n13 "Touch target size, Accessibilit…"']
    assert role["nodes"][3] == '+9 more: lint(rules=["R5"],group="node")'
    assert out["rules"][2]["nodes"] == [
        ('n22 "Section heading, MissingHeading" 426.7x9dp '
         "(likely false positive: clipped at scroll edge)")]
    assert out["next"] == ['image(overlay="lint")', 'node("n22")', 'find(issue="render.")']


def test_lint_view_groups_by_node_and_flat():
    ix, loaded = _spec_launcher()
    by_node = an.lint_view(ix, loaded, group="node")["lines"]
    assert len(by_node) == 12
    assert by_node[0] == 'n11 "▶ All scenarios (lint everythin…" !role warn; !state warn'
    flat = an.lint_view(ix, loaded, group="none")["lines"]
    assert len(flat) == 14 and flat[0].startswith("n11 ") and " R5 warn" in flat[0]


def test_lint_view_filters_by_rule_severity_and_within():
    ix, loaded = _spec_launcher()
    render = an.lint_view(ix, loaded, rules=["render."])
    assert [r["rule"] for r in render["rules"]] == ["render.clipped"]
    assert render["rules"][0]["nodes"] == ['n22 "Section heading, MissingHeading" 27 of 216px']
    assert "contrast" not in render
    assert an.lint_view(ix, loaded, severity="error")["counts"] == {"error": 0, "warn": 0,
                                                                   "info": 0}
    assert an.lint_view(ix, loaded, within="n9")["rules"] == []
    within = an.lint_view(ix, loaded, within="n22")
    assert within["counts"]["warn"] == 2 and within["within"] == "n22"
    with pytest.raises(OpError):
        an.lint_view(ix, loaded, severity="loud")
    with pytest.raises(OpError):
        an.lint_view(ix, loaded, group="rows")


def _feed(with_src: bool = True):
    """A RecyclerView #feed of 8 cells, each a ComposeView with an unlabeled icon button."""
    b = cb.IndexBuilder("cfeed1", screen=(1080, 2400))
    w = b.window("n1", "DecorView", (0, 0, 1080, 2400), udid=1)
    rv = b.view(w, "n2", "RecyclerView", (0, 0, 1080, 2400), udid=2, rid="feed",
                flags=["scroll"], anchor="w0/DecorView/RecyclerView#feed")
    for i in range(8):
        cell = b.view(rv, None, "ComposeView", (0, i * 300, 1080, 300), udid=100 + i,
                      anchor=f"w0/DecorView/RecyclerView#feed/[{i}]")
        acv = b.view(cell, None, "AndroidComposeView", (0, i * 300, 1080, 300), udid=200 + i,
                     cls="AndroidComposeView", anchor=f"w0/DecorView/RecyclerView#feed/[{i}]/ACV")
        btn = b.compose(acv, None, sem_id=7, b=(960, i * 300 + 100, 96, 96), type="IconButton",
                        flags=["click"], src="FeedRow.kt:42" if with_src else None,
                        anchor=f"w0/DecorView/RecyclerView#feed/[{i}]/ACV/IconButton:0")
        b.issue(btn, "a11y.label.missing", "error", {"role": "Button"})
    return b.build()


def test_template_collapse_of_collection_cells():
    ix = _feed()
    refs = [n.ref for n in ix.nodes.values() if n.issues]
    out = an.lint_view(ix, FakeLoaded(ix))
    (rule,) = out["rules"]
    assert rule["rule"] == "a11y.label.missing" and rule["n"] == 8 and rule["sev"] == "error"
    assert rule["nodes"] == [(f"×8 in #feed cells (FeedRow.kt:42 IconButton): "
                              f"{refs[0]} {refs[1]} {refs[2]} +5")]
    # without src the template anchor names the cells' shared part
    ix2 = _feed(with_src=False)
    (rule2,) = an.lint_view(ix2, FakeLoaded(ix2))["rules"]
    assert rule2["nodes"][0].startswith("×8 in #feed cells (IconButton:0): ")
    # grouping by node lists every cell separately
    assert len(an.lint_view(ix, FakeLoaded(ix), group="node")["lines"]) == 8


def test_distinct_nodes_are_never_collapsed():
    ix, loaded = _spec_launcher()
    role = an.lint_view(ix, loaded, rules=["R5"], per_rule=20)["rules"][0]
    assert len(role["nodes"]) == 12 and not any(s.startswith("×") for s in role["nodes"])


def test_lint_view_budgets_hold_for_random_max_bytes():
    ix, loaded = _spec_launcher()
    rnd = random.Random(7)
    for _ in range(60):
        mb = rnd.randint(500, 4000)
        for group in ("rule", "node", "none"):
            out = an.lint_view(ix, loaded, group=group, max_bytes=mb)
            assert len(dumps(out).encode("utf-8")) <= mb, (mb, group)


def test_cursors_page_through_every_finding_once():
    ix, loaded = _spec_launcher()
    seen: list[str] = []
    cursor = None
    for _ in range(20):
        out = an.lint_view(ix, loaded, group="none", limit=5, cursor=cursor)
        seen.extend(out["lines"])
        if "truncated" not in out:
            break
        cursor = out["truncated"]["cursor"]
        assert re.fullmatch(r"c7h2kq:l:[0-9a-f]{8}:\d+", cursor)
    assert len(seen) == 14 and len(set(seen)) == 14
    assert seen == an.lint_view(ix, loaded, group="none", limit=200)["lines"]
    with pytest.raises(OpError) as e:
        an.lint_view(ix, loaded, group="node", limit=5, cursor=cursor)
    assert e.value.code == "bad_args"


def test_lint_summary_for_capture():
    ix, _ = _analyzed_launcher()
    assert an.lint_summary(ix) == {
        "lint": "2 info: 1 heading, 1 touch_target (contrast not run)",
        "issues": "1 clipped: n22"}
    ix, _ = _spec_launcher()
    assert an.lint_summary(ix)["lint"] == ("14 warn: 12 role, 1 state, 1 touch_target "
                                           "(contrast not run)")


# --------------------------------------------------------------------------- #
# Contrast and WCAG mode
# --------------------------------------------------------------------------- #
LOW = (200, 200, 200)
BG = (250, 250, 250)


def _contrast_scene():
    """A main window and a popup window, each a ComposeView with one Text node. The
    popup's text is faint grey on white (low contrast); where the popup sits, the
    main window's own screenshot is plain background."""
    b = cb.IndexBuilder("ccon01", screen=(360, 640), dpi=420)
    for win, acv, text, udid, typ, box, text_box, label in (
            ("n1", "n2", "n3", 1001, "DecorView", (0, 0, 360, 640), (16, 40, 200, 40), "Title"),
            ("n4", "n5", "n6", 2001, "PopupDecorView", (40, 400, 280, 200), (60, 420, 200, 40),
             "Faint")):
        b.window(win, typ, box, udid=udid)
        b.a11y_facet(win, host=udid, virt=-1, **{"class": "android.widget.FrameLayout"})
        b.view(win, acv, "AndroidComposeView", box, udid=udid + 5, cls="AndroidComposeView")
        b.a11y_facet(acv, host=udid + 5, virt=-1, **{"class": ACV_CLASS})
        b.compose(acv, text, sem_id=2, b=text_box, label=label, attrs={"Text": label})
        b.a11y_facet(text, host=udid + 5, virt=2, **{"class": "android.widget.TextView"})
    ix = b.build()
    strokes = [((70 + 12 * k, 428, 5, 24), LOW) for k in range(12)]
    title = [((20 + 12 * k, 48, 5, 24), (0, 0, 0)) for k in range(12)]
    shots = {1001: screen_of((0, 0, 360, 640), BG, title),
             2001: screen_of((40, 400, 280, 200), BG, strokes)}
    return ix, _raw_of(ix), shots


def test_contrast_samples_each_windows_own_screenshot_and_is_cached(monkeypatch):
    ix, raw, shots = _contrast_scene()
    loaded = FakeLoaded(ix, raw=raw, shots=shots)
    an.analyze(ix, loaded, lint="tree")
    calls = []
    real = an._run_contrast
    monkeypatch.setattr(an, "_run_contrast", lambda *a, **k: calls.append(1) or real(*a, **k))
    first = an.lint_view(ix, loaded, contrast=True)
    assert calls == [1]
    assert first["contrast"] == "sampled"
    (rule,) = [r for r in first["rules"] if r["rule"] == "a11y.contrast.low"]
    assert rule["nodes"][0].startswith('n6 "Faint" ')  # the popup text, not the black title
    assert re.search(r" 1\.\d+:1$", rule["nodes"][0])
    assert 2001 in loaded.shot_calls
    second = an.lint_view(ix, loaded, contrast=True)
    assert calls == [1] and second == first  # served from the derived cache
    assert len([p for p in loaded.puts if p.startswith("lint.")]) == 1
    # a fresh view of the same capture reads the cache too
    ix2, _, _ = _contrast_scene()
    an.analyze(ix2, FakeLoaded(ix2, raw=raw))
    assert an.lint_view(ix2, loaded, contrast=True) == first and calls == [1]


def test_lint_full_stores_contrast_at_capture_time():
    ix, raw, shots = _contrast_scene()
    loaded = FakeLoaded(ix, raw=raw, shots=shots)
    an.analyze(ix, loaded, lint="full")
    assert [i.id for i in ix.get("n6").issues] == ["a11y.contrast.low"]
    assert ix.get("n3").issues == []
    assert "contrast: sampled 2 windows" in ix.diagnostics
    out = an.lint_view(ix, loaded)
    assert out["contrast"] == "sampled" and out["counts"]["error"] == 1
    assert an.lint_summary(ix)["lint"] == "1 error: 1 contrast (contrast sampled)"


def test_contrast_without_screenshots_says_so():
    ix, raw, _ = _contrast_scene()
    loaded = FakeLoaded(ix, raw=raw)
    an.analyze(ix, loaded)
    out = an.lint_view(ix, loaded, contrast=True)
    assert out["contrast"] == "unavailable: no screenshot"


def test_wcag_mode_reruns_the_tree_lint_with_44dp_targets():
    b = cb.IndexBuilder("cwcag1", screen=(1080, 2000), dpi=160)
    w = b.window("n1", "DecorView", (0, 0, 1080, 2000), udid=1)
    b.a11y_facet(w, host=1, virt=-1, **{"class": "android.widget.FrameLayout"})
    acv = b.view(w, "n2", "AndroidComposeView", (0, 0, 1080, 2000), udid=82,
                 cls="AndroidComposeView")
    b.a11y_facet(acv, host=82, virt=-1, **{"class": ACV_CLASS})
    b.compose(acv, "n3", sem_id=4, b=(0, 0, 46, 46), label="Send", flags=["click"],
              attrs={"Text": "Send", "OnClick": "x", "Role": "Button"})
    b.a11y_facet("n3", host=82, virt=4, flags=["click", "focus"],
                 **{"class": "android.widget.Button"})
    ix = b.build()
    loaded = FakeLoaded(ix, raw=_raw_of(ix))
    an.analyze(ix, loaded)
    assert [i.id for i in ix.get("n3").issues] == [TOUCH]
    assert an.lint_view(ix, loaded)["counts"]["warn"] == 1
    assert an.lint_view(ix, loaded, wcag=True)["counts"] == {"error": 0, "warn": 0, "info": 0}
    assert [i.id for i in ix.get("n3").issues] == [TOUCH]  # the index is not changed


def test_lint_view_output_is_json_serializable_and_compact():
    ix, loaded = _analyzed_launcher()
    out = an.lint_view(ix, loaded)
    assert json.loads(dumps(out)) == out


def test_unknown_rule_from_a_newer_lint_is_kept():
    ix, loaded = _spec_launcher()
    ix.get("n9").issues.append(Issue("a11y.future.rule", "warn", {"x": 1}))
    out = an.lint_view(ix, loaded, per_rule=1)
    (fut,) = [r for r in out["rules"] if r["rule"] == "a11y.future.rule"]
    assert fut["n"] == 1 and fut["nodes"] == ['n9 "A11yProbe"']


def test_analyzers_import_is_light():
    import subprocess
    import sys

    code = ("import sys; import inspector_widget.capture.analyzers, "
            "inspector_widget.capture.rules; "
            "print('google.protobuf' in sys.modules, 'PIL' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=__file__.rsplit("/tests/", 1)[0], check=True).stdout.split()
    assert out == ["False", "False"]


def test_screenshot_pb_accepted_from_a_loaded_capture():
    ix, raw, shots = _contrast_scene()
    as_bytes = FakeLoaded(ix, raw=raw, shots={k: v.SerializeToString() for k, v in shots.items()})
    src = an._Src(as_bytes)
    shot = src.shot(2001)
    assert isinstance(shot, pb.Screenshot) and shot.width == 280
    assert src.shot(9999) is None


def test_contrast_on_a_scroll_clipped_sliver_is_low_confidence():
    clipped = Issue("render.clipped", "info", {"clipped_by": "n10", "scroll": True})
    pairs = [("n22", Issue(an.CONTRAST_RULE, "error", {"ratio": 1.25})),
             ("n22", Issue(TOUCH, "warn", {"w_dp": 426.7, "h_dp": 9.0})),
             ("n11", Issue(an.CONTRAST_RULE, "error", {"ratio": 2.0}))]
    an._annotate_touch_fp(pairs, {"n22": [clipped]})
    assert pairs[0][1].conf == "inferred" and "sliver" in pairs[0][1].evidence["note"]
    assert pairs[1][1].evidence["note"] == an.LIKELY_FP and pairs[1][1].conf == "exact"
    assert "note" not in pairs[2][1].evidence and pairs[2][1].conf == "exact"


def test_rules_the_installed_lint_cannot_produce_are_flagged(monkeypatch):
    ix, loaded = _spec_launcher()
    monkeypatch.setattr(a11y_lint, "ALL_RULE_IDS",
                        [r for r in a11y_lint.ALL_RULE_IDS if r != "a11y.duplicate.label"])
    out = an.lint_view(ix, loaded, rules=["R12", "R5"])
    assert out["unavailable"] == ["R12"] and [r["rule"] for r in out["rules"]] == [ROLE]
    assert "unavailable" not in an.lint_view(ix, loaded, rules=["render.clipped"])
