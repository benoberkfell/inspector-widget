"""Offline checks for the matching helpers of the device golden test.

``test_device_a11y_golden.py`` only runs against a device, so a bug in how it
ties findings to nodes would stay invisible offline (and could turn the golden
test into a silent pass). These tests pin that logic with synthetic dumps.
"""

from __future__ import annotations

import pytest

import test_device_a11y_golden as G


def _n(hv, vid, x, y, w, h, res=None, text=None, flags=(), children=()):
    n = {"host_view_id": hv, "virtual_id": vid, "id": (hv << 32) ^ (vid & 0xFFFFFFFF),
         "bounds": {"layout": {"x": x, "y": y, "w": w, "h": h}}, "flags": list(flags),
         "children": list(children)}
    if res:
        n["view_id_resource_name"] = res
    if text:
        n["text"] = n["speakable"] = text
    return n


def _capture(roots, findings=(), focus_order=()):
    a11y = {"windows": [{"root_view_id": 1, "root": r} for r in roots],
            "focus_order": list(focus_order)}
    return G.Capture(G.GOLDEN_BY_ID["icon_button"], a11y, {"findings": list(findings)}, {}, {})


def _f(rule, x, y, w, h, **extra):
    return {"rule": rule, "severity": "error", "node": {}, "bounds": {"x": x, "y": y, "w": w, "h": h}, **extra}


def test_same_element_accepts_touch_expansion_but_not_a_row_vs_its_label():
    btn48 = {"x": 0, "y": 0, "w": 144, "h": 144}
    btn40 = {"x": 12, "y": 12, "w": 120, "h": 120}
    glyph24 = {"x": 36, "y": 36, "w": 72, "h": 72}
    assert G.same_element(btn48, btn40)
    assert G.same_element(btn48, glyph24)
    row = {"x": 0, "y": 0, "w": 1080, "h": 168}
    label = {"x": 40, "y": 60, "w": 600, "h": 48}
    assert not G.same_element(row, label)
    assert not G.same_element(btn48, {"x": 300, "y": 0, "w": 144, "h": 144})


def test_finding_hits_prefers_the_node_key_over_geometry():
    inner = _n(7, 3, 0, 0, 100, 100, res="bad_merged_inner")
    outer = _n(7, 2, 0, 0, 100, 100, res="bad_merged", children=[inner])
    cap = _capture([_n(1, -1, 0, 0, 1080, 2000, children=[_n(7, -1, 0, 0, 1080, 2000, children=[outer])])])
    keyed_outer = _f(G.R1, 0, 0, 100, 100, node_key="compose:7:2")
    assert G.finding_hits(cap, keyed_outer, outer)
    assert not G.finding_hits(cap, keyed_outer, inner)       # tagged siblings are distinct
    unkeyed = _f(G.R1, 0, 0, 100, 100)
    assert G.finding_hits(cap, unkeyed, inner) and G.finding_hits(cap, unkeyed, outer)
    unresolved = _f(G.R1, 0, 0, 100, 100, node_key="compose:999:1")  # stale key -> geometry
    assert G.finding_hits(cap, unresolved, inner)


def test_finding_on_an_untagged_helper_node_counts_for_the_tagged_element():
    helper = _n(7, 5, 12, 12, 120, 120)                       # e.g. IconButton's 40dp Button node
    tagged = _n(7, 4, 0, 0, 144, 144, res="bad_icon_button", children=[helper])
    cap = _capture([_n(7, -1, 0, 0, 1080, 2000, children=[tagged])])
    assert G.finding_hits(cap, _f(G.R1, 0, 0, 1, 1, node={"key": "compose:7:5"}), tagged)


def test_row_scope_and_locate_find_the_control_inside_the_titled_row():
    def row(pos, kind, res):
        title = _n(9, -1 if kind == "view" else pos * 10 + 1, 0, pos * 200, 600, 60,
                   text=f"Item {pos} ({kind})")
        delete = _n(9, -1 if kind == "view" else pos * 10 + 2, 900, pos * 200, 144, 144,
                    res="com.oberkfell.a11yprobe:id/delete" if kind == "view" else "delete")
        return _n(9, -1 if kind == "view" else pos * 10, 0, pos * 200, 1080, 192, res=res,
                  children=[title, delete])
    r0 = row(0, "compose", "cell_0")
    r1 = row(1, "view", "com.oberkfell.a11yprobe:id/view_cell_root")
    cap = _capture([_n(1, -1, 0, 0, 1080, 2000, children=[r0, r1])])
    assert G.row_scope(cap, "Item 1 (view)") is r1
    assert G.locate(cap, G.E("delete", G.R1, row="Item 0 (compose)")) is r0["children"][1]
    assert G.locate(cap, G.E("delete", G.R1, row="Item 1 (view)")) is r1["children"][1]
    with pytest.raises(AssertionError):
        G.row_scope(cap, "Item 7 (view)")


def test_rule_aliases_and_patterns():
    assert G.finding_rule({"rule": "R1"}) == G.R1
    assert G.rule_matches("a11y.form.label_missing", (G.R1, G.FORM_LABEL))
    assert G.rule_matches("a11y.clickable.duplicate_bounds", (G.DUPLICATE_BOUNDS,))
    assert not G.rule_matches(G.R2, (G.R1, G.FORM_LABEL))


def test_row_expectations_mirror_the_flaw_tables():
    bad, good = G.row_expectations(G.ONE_OF_EACH, lambda p: "view")
    assert [(e.tag, e.rules, e.row) for e in bad] == [
        ("delete", (G.R1,), "Item 1 (view)"),
        ("info", (G.R2,), "Item 2 (view)"),
        ("notify", (G.R7,), "Item 3 (view)"),
    ]
    assert len(good) == 6 * 4 - 3            # 3 controls + checkbox per row, minus the flawed ones
    assert all(G.R1 in e.rules for e in good)


def test_traversal_check_uses_focus_stops_in_order():
    order = [{"speakable": s, "is_focus_stop": True} for s in G.TRAVERSAL_ORDER]
    G.check_traversal_order(_capture([], focus_order=order))
    swapped = order[:3] + [order[5], order[4], order[3]] + order[6:]
    with pytest.raises(AssertionError):
        G.check_traversal_order(_capture([], focus_order=swapped))


def test_golden_ids_are_unique_and_cover_every_scenario_family():
    ids = [g.sid for g in G.GOLDENS]
    assert len(ids) == len(set(ids))
    assert {"view_xml", "S1", "S2", "S3", "S4", "S5", "D1", "D2"} <= set(ids)
    assert all(g.bad or g.checks or g.sid == "traversal" for g in G.GOLDENS)
