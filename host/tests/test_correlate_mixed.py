"""correlate.py against the ID contract on a mixed View/Compose screen.

CO1  a11y joins Compose on (ComposeView, semantics id) and Views on (view id, -1).
CO2  lint findings attach by typed key only (never a bare int across id spaces).
CO3  the IoU fallback is one-to-one, restricted to the same window / ComposeView.
ID3  composite keys; a bare compose:<id> is accepted only when unambiguous.
ID4  a dump generation; stale Compose keys re-resolve from earlier fingerprints.
Plus attribution (window / list row / ComposeView / AndroidView host) and grafting.
"""

from __future__ import annotations

import copy

import pytest

from inspector_widget import a11y, correlate

import mixed_fixture as mf


def _a11y_roots():
    d = a11y.a11y_to_dict(mf.a11y_response())
    return [w["root"] for w in d["windows"]]


def _merged(compose=None, a11y_roots=None, views=None):
    return correlate.build_integrated_tree(
        views or mf.view_roots(), compose if compose is not None else mf.compose_windows(),
        a11y_roots if a11y_roots is not None else _a11y_roots())


def _by_key(merged):
    return {n["node_key"]: n for n in correlate.flatten(merged)}


# --------------------------------------------------------------------------- CO1
def test_compose_nodes_join_their_own_compose_view():
    idx = _by_key(_merged())
    for row, acv in mf.CELL_ACVS.items():
        cb = idx[f"compose:{acv}:3"]
        assert cb["correlation_confidence"] == "exact"
        assert (cb["a11y"]["host_view_id"], cb["a11y"]["virtual_id"]) == (acv, 3)
        assert ("checked" in cb["a11y"]["flags"]) == (row == 1)
        delete = idx[f"compose:{acv}:4"]
        assert delete["a11y"]["node_key"] == f"compose:{acv}:4"


def test_views_join_on_view_id_with_virtual_minus_one():
    idx = _by_key(_merged())
    assert idx["view:52"]["a11y"]["node_key"] == "view:52"
    assert idx["view:52"]["a11y"]["class_name"] == "android.widget.ImageButton"
    # A View whose id equals a semantics id elsewhere never picks up a Compose node.
    assert idx["view:11"]["a11y"]["node_key"] == "view:11"


def test_semantics_root_and_composeview_map_to_the_host_node():
    idx = _by_key(_merged())
    assert idx["composeview:22"]["a11y"]["node_key"] == "view:22"
    assert idx["compose:22:1"]["a11y"]["node_key"] == "view:22"
    assert idx["compose:22:1"]["correlation_confidence"] == "exact"


def test_every_compose_view_is_grafted_including_nested_ones():
    merged = _merged()
    idx = _by_key(merged)
    assert merged["summary"]["compose_views"] == 5
    for acv in (22, 32, 42, 61, 66):
        assert f"composeview:{acv}" in idx
    # No compose node is left without its a11y counterpart except the AndroidView
    # node, whose ANI Compose replaces with the embedded View.
    none = [k for k, n in idx.items()
            if k.startswith("compose") and n["correlation_confidence"] == "none"]
    assert none == ["compose:61:3"]


def test_android_view_holder_is_rehomed_under_its_compose_node():
    idx = _by_key(_merged())
    host = idx["compose:61:3"]
    assert [c["node_key"] for c in host["children"]] == ["view:63"]
    assert idx["view:63"]["interop"] == {"hosted_by": "compose:61:3", "view_parent": "view:62"}
    assert idx["view:62"]["children"] == []


def test_list_items_are_marked_with_rows():
    idx = _by_key(_merged())
    assert idx["view:31"]["list_item"] == {"list": "view:20", "index": 1, "row": 1}
    assert idx["view:50"]["list_item"]["row"] == 3
    assert idx["view:65"]["list_item"]["list"] == "view:64"


# --------------------------------------------------------------------------- CO3
def test_iou_fallback_is_one_to_one_with_text_tiebreak():
    # Recomposition re-minted the ids between the compose and a11y dumps: a Box and
    # the Text it wraps have identical bounds, the a11y dump has ONE orphan node.
    compose = [{"view_id": 200, "root": {
        "id": 200, "name": "AndroidComposeView", "bounds": {"layout": {"x": 0, "y": 0, "w": 400, "h": 400}},
        "children": [{"id": 150, "name": "Node", "bounds": {"layout": {"x": 0, "y": 0, "w": 400, "h": 400}},
                      "children": [
            {"id": 151, "name": "Box", "attrs": {}, "bounds": {"layout": {"x": 10, "y": 10, "w": 100, "h": 40}},
             "children": [
                {"id": 152, "name": "Hello", "attrs": {"Text": "Hello"},
                 "bounds": {"layout": {"x": 10, "y": 10, "w": 100, "h": 40}}}]}]}]}}]
    views = [{"id": 1, "class_name": "DecorView", "bounds": {"layout": {"x": 0, "y": 0, "w": 400, "h": 400}},
              "children": [{"id": 200, "class_name": "AndroidComposeView",
                            "bounds": {"layout": {"x": 0, "y": 0, "w": 400, "h": 400}}}]}]
    orphan = {"host_view_id": 200, "virtual_id": 17, "text": "Hello",
              "bounds": {"layout": {"x": 10, "y": 10, "w": 100, "h": 40}}}
    # Same bounds but in ANOTHER ComposeView: must not be considered.
    foreign = {"host_view_id": 999, "virtual_id": 18, "text": "Hello",
               "bounds": {"layout": {"x": 10, "y": 10, "w": 100, "h": 40}}}
    root = {"host_view_id": 1, "virtual_id": -1,
            "bounds": {"layout": {"x": 0, "y": 0, "w": 400, "h": 400}},
            "children": [{"host_view_id": 200, "virtual_id": -1,
                          "bounds": {"layout": {"x": 0, "y": 0, "w": 400, "h": 400}},
                          "children": [orphan, foreign]}]}
    idx = _by_key(correlate.build_integrated_tree(views, compose, [root]))
    assert idx["compose:200:152"]["correlation_confidence"] == "overlap"
    assert idx["compose:200:152"]["a11y"]["virtual_id"] == 17
    assert idx["compose:200:151"]["correlation_confidence"] == "none"  # one-to-one


def test_iou_fallback_never_steals_an_exactly_claimed_node():
    # The FrameLayout wrapping a Button has the Button's bounds but no a11y node.
    views = [{"id": 1, "class_name": "FrameLayout", "bounds": {"layout": {"x": 0, "y": 0, "w": 100, "h": 50}},
              "children": [{"id": 2, "class_name": "Button",
                            "bounds": {"layout": {"x": 0, "y": 0, "w": 100, "h": 50}}}]}]
    a = [{"host_view_id": 1, "virtual_id": -1, "bounds": {"layout": {"x": 0, "y": 0, "w": 100, "h": 50}},
          "children": [{"host_view_id": 2, "virtual_id": -1, "text": "OK",
                        "bounds": {"layout": {"x": 0, "y": 0, "w": 100, "h": 50}}}]}]
    idx = _by_key(correlate.build_integrated_tree(views, [], a))
    assert idx["view:2"]["correlation_confidence"] == "exact"
    assert idx["view:1"]["a11y"]["host_view_id"] == 1


# --------------------------------------------------------------------------- ID3
def test_composite_keys_resolve_exactly():
    merged = _merged()
    node = correlate.find_node(merged, node_key="compose:32:4")
    assert node["compose"]["acv"] == 32 and node["compose"]["id"] == 4
    assert correlate.find_node(merged, node_key="virtual:32:4") is node
    assert correlate.find_node(merged, node_key="composeview:22")["node_key"] == "composeview:22"


def test_bare_compose_key_is_ambiguous_across_compose_views():
    merged = _merged()
    with pytest.raises(correlate.NodeKeyError) as ei:
        correlate.find_node(merged, node_key="compose:3")
    msg = str(ei.value)
    for acv in (22, 32, 42):
        assert f"compose:{acv}:3" in msg
    with pytest.raises(correlate.NodeKeyError):
        correlate.find_node(merged, semantics_id=4)


def test_bare_compose_key_resolves_when_unambiguous():
    merged = _merged(compose=[w for w in mf.compose_windows() if w["view_id"] == 22])
    assert correlate.find_node(merged, node_key="compose:4")["node_key"] == "compose:22:4"


def test_semantics_id_lookup_has_no_virtual_id_fallback():
    # compose:22:5 exists only in the a11y dump (a merged Text). The old fallback matched
    # a11y virtual ids; a semantics_id lookup must only see Compose nodes.
    assert correlate.find_node(_merged(), semantics_id=5) is None


def test_malformed_key_raises():
    with pytest.raises(correlate.NodeKeyError):
        correlate.find_node(_merged(), node_key="compose:x:y")


# --------------------------------------------------------------------------- ID4
def _reminted(windows, offset=100):
    out = copy.deepcopy(windows)
    for w in out:
        stack = [w["root"]]
        while stack:
            n = stack.pop()
            if n is not w["root"]:
                n["id"] += offset
            stack.extend(n.get("children", []))
    return out


def test_generation_changes_when_ids_are_reminted():
    a = _merged()
    b = _merged(compose=_reminted(mf.compose_windows()))
    assert a["generation"] == a["summary"]["generation"]
    assert a["generation"] != b["generation"]
    assert _merged()["generation"] == a["generation"]


def test_stale_key_is_reresolved_from_the_previous_generation():
    reg = correlate.KeyRegistry()
    old = _merged()
    reg.record(old)
    new = _merged(compose=_reminted(mf.compose_windows()))
    node = correlate.find_node(new, node_key="compose:32:4", registry=reg)
    assert node["node_key"] == "compose:32:104"
    assert node["resolved_from"]["stale_key"] == "compose:32:4"
    assert node["resolved_from"]["generation"] == old["generation"]
    assert "test_tag" in node["resolved_from"]["matched_on"]


def test_stale_key_without_history_explains_itself():
    new = _merged(compose=_reminted(mf.compose_windows()))
    with pytest.raises(correlate.NodeKeyError) as ei:
        correlate.find_node(new, node_key="compose:32:4", registry=correlate.KeyRegistry())
    assert "re-mints" in str(ei.value)


def test_stale_key_falls_back_to_bounds_when_given():
    new = _merged(compose=_reminted(mf.compose_windows()))
    node = correlate.find_node(new, node_key="compose:32:4",
                               bounds={"x": 950, "y": 470, "w": 100, "h": 100})
    assert node["node_key"] == "compose:32:104"


def test_recycled_cell_key_carries_a_note():
    reg = correlate.KeyRegistry()
    reg.record(_merged())
    # Same ids, but ComposeView 32 now shows another row (RecyclerView rebound it).
    wins = mf.compose_windows()
    for w in wins:
        if w["view_id"] == 32:
            row = w["root"]["children"][0]["children"][0]
            row["attrs"]["Text"] = "Item 7 (compose)"
            row["attrs"]["TestTag"] = "compose_cell_7"
            w["root"]["children"][0]["children"][0]["name"] = "Item 7 (compose)"
    merged = _merged(compose=wins)
    node = correlate.find_node(merged, node_key="compose:32:2", registry=reg)
    assert "key_note" in node and "test_tag" in node["key_note"]


# --------------------------------------------------------------------------- attribution
def test_attribution_names_row_compose_view_and_list():
    merged = _merged()
    node = correlate.find_node(merged, node_key="compose:32:4")
    att = correlate.attribution(merged, node)
    ctx = att["context"]
    assert ctx["window"] == "view:2"
    assert ctx["list"] == "view:20" and ctx["row"] == 1 and ctx["item"] == "view:31"
    assert ctx["compose_view"] == "composeview:32"
    where = att["where"]
    assert "view:20 RecyclerView" in where and "row 1: view:31" in where
    assert where.endswith("compose:32:4 Button 'Delete'")


def test_attribution_through_an_android_view():
    merged = _merged()
    att = correlate.attribution(merged, correlate.find_node(merged, node_key="compose:66:2"))
    ctx = att["context"]
    assert ctx["interop_host"] == "compose:61:3"
    assert ctx["list"] == "view:64" and ctx["row"] == 0
    assert ctx["compose_view"] == "composeview:66"


# --------------------------------------------------------------------------- a11y-only
def test_virtual_children_of_other_providers_are_grafted():
    views = [{"id": 1, "class_name": "FrameLayout", "bounds": {"layout": {"x": 0, "y": 0, "w": 400, "h": 400}},
              "children": [{"id": 70, "class_name": "ChipGroup",
                            "bounds": {"layout": {"x": 0, "y": 0, "w": 400, "h": 100}}}]}]
    a = [{"host_view_id": 1, "virtual_id": -1, "bounds": {"layout": {"x": 0, "y": 0, "w": 400, "h": 400}},
          "children": [{"host_view_id": 70, "virtual_id": -1, "provider_class": "ExploreByTouchHelper",
                        "bounds": {"layout": {"x": 0, "y": 0, "w": 400, "h": 100}},
                        "children": [{"host_view_id": 70, "virtual_id": 0, "node_key": "virtual:70:0",
                                      "text": "Chip A",
                                      "bounds": {"layout": {"x": 0, "y": 0, "w": 100, "h": 100}}}]}]}]
    idx = _by_key(correlate.build_integrated_tree(views, [], a))
    chip = idx["virtual:70:0"]
    assert chip["a11y_only"] is True and chip["a11y"]["text"] == "Chip A"
    assert correlate.find_node({"roots": list(idx.values())[:1]}, node_key="virtual:70:0") is chip


# --------------------------------------------------------------------------- CO2
class _FakeSession:
    def get_properties(self, **kw):
        raise RuntimeError("not needed")


@pytest.fixture
def fake_session(monkeypatch):
    monkeypatch.setattr(correlate, "_shaped_view_tree", lambda s, props: (mf.view_roots(), {}))
    monkeypatch.setattr(correlate, "_shaped_compose", lambda s: mf.compose_windows())
    monkeypatch.setattr(correlate, "_shaped_a11y", lambda s: _a11y_roots())
    return _FakeSession()


def _lint(roots, density):
    return mf.untyped_compose_findings() + mf.typed_findings() + [
        # A bare int that happens to equal a View id (view:50) must not attach to it.
        {"rule": "a11y.x", "severity": "info", "node": {"id": 50},
         "bounds": {"x": 0, "y": 0, "w": 1, "h": 1}}]


def test_inspect_node_attaches_only_its_own_findings(fake_session):
    d = correlate.inspect_node(fake_session, node_key="compose:32:3", include_image=False,
                               lint_fn=_lint)
    assert [f["rule"] for f in d["lint"]] == ["a11y.label.missing"]
    assert d["lint"][0]["bounds"]["y"] == mf.CELL_Y[1] + 60  # row 1's, not rows 0/2
    assert d["context"]["row"] == 1
    assert d["generation"]


def test_inspect_node_typed_findings_and_no_cross_space_ints(fake_session):
    d = correlate.inspect_node(fake_session, node_key="view:52", include_image=False,
                               lint_fn=_lint)
    assert [f["rule"] for f in d["lint"]] == ["a11y.image.no_description"]
    d50 = correlate.inspect_node(fake_session, node_key="view:50", include_image=False,
                                 lint_fn=_lint)
    assert d50["lint"] == []


def test_inspect_node_reresolves_across_calls(fake_session, monkeypatch):
    correlate.inspect_tree(fake_session)  # the agent saw compose:32:4 here
    monkeypatch.setattr(correlate, "_shaped_compose",
                        lambda s: _reminted(mf.compose_windows()))
    d = correlate.inspect_node(fake_session, node_key="compose:32:4", include_image=False)
    assert d["node_key"] == "compose:32:104"
    assert d["resolved_from"]["stale_key"] == "compose:32:4"


def test_tag_compose_findings_disambiguates_by_bounds():
    fs = correlate.tag_compose_findings(mf.untyped_compose_findings(), mf.compose_windows())
    assert [f["node"]["node_key"] for f in fs] == [
        "compose:22:3", "compose:32:3", "compose:42:3", "compose:32:4"]


def test_attribution_of_a_list_item_itself():
    merged = _merged()
    att = correlate.attribution(merged, correlate.find_node(merged, node_key="view:50"))
    assert att["context"]["row"] == 3 and att["context"]["list"] == "view:20"
    assert att["where"].endswith("row 3: view:50 LinearLayout")
