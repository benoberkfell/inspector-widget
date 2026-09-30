"""Offline tests for the capture query engine (capture/query.py) and the line
grammar (capture/lines.py): spec "Capture and Walk" sections 5.1, 5.5-5.7, 6 and 8.

Indexes come from tests/capture_builders.py (the spec's launcher example, the
259-view wide screen, big synthetic screens) plus a View-screen index built here
from the real recorded fixtures (tests/fixtures/live/viewscreen).
"""

from __future__ import annotations

import copy
import gc
import json
import random
import re
import time
from typing import Any

import live_fixtures as lf
import pytest
from capture_builders import (
    CLIPPED_RULE,
    ROLE_RULE,
    IndexBuilder,
    big_index,
    launcher_index,
    wide_index,
)

from inspector_widget import normalize as nz
from inspector_widget.capture import lines as L
from inspector_widget.capture import query as q
from inspector_widget.capture.model import Issue, OpError
from inspector_widget.output import dumps


def nbytes(obj: Any) -> int:
    return len(dumps(obj).encode("utf-8"))


def assert_grammar(lines: list[str]) -> None:
    for line in lines:
        assert L.is_line(line), f"line does not match grammar v1: {line!r}"


def refs_of(lines: list[str]) -> list[str]:
    """Every ref on the lines (chain members included), in order."""
    out = []
    for line in lines:
        out.extend(s["ref"] for s in L.parse_line(line)["segs"])
    return out


def walk_pages(fn, ix, **kw) -> tuple[list[dict], list[str]]:
    """Follow cursors until the end; returns (pages, all lines)."""
    pages, lines = [], []
    cursor = None
    for _ in range(10_000):
        page = fn(ix, **kw, cursor=cursor) if cursor else fn(ix, **kw)
        pages.append(page)
        lines.extend(page.get("lines") or [])
        tr = page.get("truncated")
        if not tr:
            return pages, lines
        assert page["shown"] >= 1, "a page must always advance"
        cursor = tr["cursor"]
    raise AssertionError("cursor walk did not terminate")


@pytest.fixture(scope="module")
def launcher():
    return launcher_index()


@pytest.fixture(scope="module")
def wide():
    return wide_index()


@pytest.fixture(scope="module")
def big():
    return big_index(5000)


# --------------------------------------------------------------------------- #
# Real-data View screen (tests/fixtures/live/viewscreen) as an Index
# --------------------------------------------------------------------------- #
_A11Y_FLAGS = {"clickable": "click", "long_clickable": "longclick", "focusable": "focus",
               "checkable": "checkable", "checked": "checked", "heading": "heading",
               "editable": "edit", "scrollable": "scroll"}


def viewscreen_index() -> tuple[Any, dict[int, list]]:
    """The recorded ViewScenarioActivity (40 Views) as an Index, with its a11y
    nodes joined by bounds and class (a stand-in for C4), plus the raw props."""
    views = lf.load("viewscreen", "views_props")
    a11y = lf.load("viewscreen", "a11y")
    pool: list[dict] = []

    def collect(n: dict) -> None:
        pool.append(n)
        for c in n.get("children") or ():
            collect(c)

    for w in a11y["windows"]:
        collect(w["root"])
    stops = {f["id"]: f["order"] for f in a11y.get("focus_order") or () if f.get("is_focus_stop")}
    used: set[int] = set()

    def box(b: dict) -> tuple[int, int, int, int]:
        r = b.get("layout", b)
        return (r["x"], r["y"], r["w"], r["h"])

    def match(v: dict) -> dict | None:
        vb = box(v["bounds"])
        cls = v["class_name"]
        best = None
        for a in pool:
            if id(a) in used or box(a["bounds"]) != vb:
                continue
            if a["host_view_id"] == v["id"] and a["virtual_id"] == -1:
                best = a
                break
            simple = (a.get("class_name") or "").rsplit(".", 1)[-1]
            if best is None and simple and simple in cls:
                best = a
        if best is not None:
            used.add(id(best))
        return best

    b = IndexBuilder("cvs0a1", screen=(1280, 2856))
    stop_refs: list[tuple[int, str]] = []

    def add(v: dict, parent: str | None) -> None:
        a = match(v)
        rid = (v.get("resource") or {}).get("name")
        flags = sorted({_A11Y_FLAGS[f] for f in (a or {}).get("flags", ()) if f in _A11Y_FLAGS},
                       key=L.FLAGS.index) if a else []
        kw: dict[str, Any] = {"rid": rid, "flags": flags, "udid": v["id"],
                              "qualified": v.get("qualified_name")}
        label = (a or {}).get("speakable") or v.get("text")
        if label:
            kw["label"] = label
        if v.get("text"):
            kw["text"] = v["text"]
        if a and a.get("state_description"):
            kw["state"] = a["state_description"]
        if a and a.get("hint_text"):
            kw["hint"] = a["hint_text"]
        if parent is None:
            ref = b.window(None, v["class_name"], box(v["bounds"]), **kw)
        else:
            ref = b.view(parent, None, v["class_name"], box(v["bounds"]), **kw)
        if a is not None:
            b.a11y_facet(ref, host=a["host_view_id"], virt=a["virtual_id"], conf="inferred",
                         speakable=a.get("speakable"), flags=flags,
                         actions=[x.get("name") for x in a.get("actions") or () if x.get("name")],
                         **{"class": a.get("class_name")})
            if a.get("id") in stops:
                stop_refs.append((stops[a["id"]], ref))
        for c in v.get("children") or ():
            add(c, ref)

    for r in views["roots"]:
        add(r, None)
    b.reading([ref for _, ref in sorted(stop_refs)])
    b.issue(b.build().by_key["view:6"], "a11y.label.duplicate", "warn")
    ix = b.build()
    props = {int(k): v for k, v in views["properties"].items()}
    return ix, props


@pytest.fixture(scope="module")
def viewscreen():
    return viewscreen_index()


def wide_props(udid: int) -> list[dict]:
    """The wide scene's E6 properties (fakescenes._e6_props) in the strings.py shape."""
    props = [
        {"name": "gravity", "type": "GRAVITY", "value": 0, "label": "center_vertical|start"},
        {"name": "importantForAccessibility", "type": "INT_FLAG", "value": 0, "label": "auto"},
        {"name": "layout_width", "type": "DIMENSION", "value": 1080, "is_layout": True},
        {"name": "paddingStart", "type": "DIMENSION", "value": 42},
        {"name": "textColor", "type": "COLOR", "value": -16777216},
        {"name": "text", "type": "STRING", "value": "Hello world"},
        {"name": "visibility", "type": "INT_ENUM", "value": "visible"},
        {"name": "background", "type": "DRAWABLE",
         "value": "android.graphics.drawable.ColorDrawable"},
        {"name": "alpha", "type": "FLOAT", "value": 1.0},
        {"name": "enabled", "type": "BOOLEAN", "value": True},
    ]
    props += [{"name": f"attr_{i}", "type": "INT32", "value": i} for i in range(50)]
    if udid % 7 == 0:  # a few views differ from their class majority
        props[3] = {"name": "paddingStart", "type": "DIMENSION", "value": 7}
    return props


# =========================================================================== #
# Line grammar
# =========================================================================== #
def test_short_codes():
    assert L.short_code("a11y.role.missing_on_clickable") == "role"
    assert L.short_code("a11y.touch_target.small") == "touch_target"
    assert L.short_code("render.clipped") == "clipped"
    assert L.short_code("render.text_overflow") == "text_overflow"
    assert L.short_code("custom.rule") == "custom_rule"


def test_label_escaping_cutting_and_quoted_idents():
    b = IndexBuilder()
    w = b.window("n1", "DecorView")
    b.view(w, "n2", "TextView", (0, 0, 10, 10), label='say "hi"\nnow\\', tag="my tag",
           rid="ok_rid")
    b.view(w, "n3", "TextView", (0, 10, 10, 10), label="x" * 60)
    ix = b.build()
    out = q.outline(ix)
    assert_grammar(out["lines"])
    l2 = next(x for x in out["lines"] if "n2" in x)
    assert '"say \\"hi\\"\\nnow\\\\"' in l2
    assert '@"my tag"' in l2 and "#ok_rid" in l2
    seg = L.parse_line(l2)["segs"][0]
    assert seg["label"] == 'say "hi"\nnow\\' and seg["tag"] == "my tag" and seg["rid"] == "ok_rid"
    l3 = next(x for x in out["lines"] if "n3" in x)
    lab = L.parse_line(l3)["segs"][0]["label"]
    assert len(lab) == 48 and lab.endswith("…")
    # the quoted tag is also a valid selector
    assert q.resolve_selector(ix, '@"my tag"').id == "n2"


def test_every_rendered_line_matches_the_grammar(launcher, wide, viewscreen):
    vs, props = viewscreen
    small_big = big_index(400)
    for ix in (launcher, wide, vs, small_big):
        for view in q.OUTLINE_VIEWS:
            for detail in q.DETAILS:
                out = q.outline(ix, view=view, detail=detail, depth=99, max_lines=400,
                                max_bytes=32000)
                assert_grammar(out["lines"])
        assert_grammar(q.find(ix, fields="+src,+dp,+ids,+conf,+sel,+visible,+key,+kind,+stop,"
                                         "+text,+role,+window,+declared,+anchor",
                              limit=200, max_bytes=32000)["lines"])
    assert_grammar(q.find(launcher, in_="all", fields="+params:*", limit=200)["lines"])
    assert_grammar(q.outline(vs, fields="+props:text*,visibility", props_fn=props.get,
                             max_bytes=32000)["lines"])
    d = q.node(launcher, None, ["n22"], children=True, ancestors=True)
    assert_grammar(d["compose"]["slots"])
    s = q.node(launcher, None, ["n10"], children=True, max_bytes=32000)
    assert_grammar(s["children"])
    for bad in ("View", "@launch_heading_x", '"Section headin"'):  # ambiguous / not_found
        with pytest.raises(OpError) as e:
            q.resolve_selector(launcher, bad)
        assert e.value.candidates
        assert_grammar(e.value.candidates)


def test_collapsed_chain_and_capture_preview(launcher):
    out = q.outline(launcher, depth=2)
    assert out["lines"] == [
        "n1 DecorView [0,0 1280x2856]",
        ("  n2 LinearLayout > n4 FrameLayout #content > n5 ComposeView > n6 AndroidComposeView"
         " [0,0 1280x2856]"),
        '    n9 TextView "A11yProbe" [48,210 322x84]',
        "    n10 @launcher_list scroll [0,348 1280x2436] +12",
        "  n24 View #navigationBarBackground [0,2784 1280x72]",
        "  n25 View #statusBarBackground hidden [0,0 1280x156]",
    ]
    assert out["hidden"] == {"zero_size": 1, "collapsed": 3}  # n3 ViewStub; n7, n8, n23
    assert out["next"][0] == 'outline(root="n10")'
    rows = q.outline(launcher, depth=2, format="json")["rows"]
    chain = rows[1]
    assert chain["ref"] == "n6" and chain["type"] == "AndroidComposeView"  # the last member
    assert [s["ref"] for s in chain["chain"]] == ["n2", "n4", "n5", "n6"]


def test_every_node_is_shown_collapsed_hidden_or_counted(launcher, wide):
    """Partition: each node in scope is on a line, collapsed, zero-size hidden, or
    counted in exactly one +N."""
    small = big_index(300)
    for ix in (launcher, wide, small):
        scope = sum(1 for n in ix.nodes.values() if n.kind != "slot")
        for depth in (0, 1, 2, 3, 99):
            for mc in (1, 3, 12):
                pages, lines = walk_pages(q.outline, ix, depth=depth, max_children=mc,
                                          max_lines=400, max_bytes=32000)
                parsed = [L.parse_line(x) for x in lines]
                shown = sum(len(p["segs"]) for p in parsed)
                plus = sum(p.get("hidden", 0) for p in parsed)
                hidden = sum((pages[0].get("hidden") or {}).values())
                assert shown + plus + hidden == scope, (ix.meta.id, depth, mc)


def test_long_chains_are_capped():
    b = IndexBuilder()
    p = b.window("n1", "FrameLayout")
    for i in range(2, 602):  # 600 single-child wrappers with identical bounds
        p = b.view(p, f"n{i}", "FrameLayout", (0, 0, 1280, 2856))
    b.view(p, "n602", "TextView", (10, 10, 100, 50), label="deep")
    ix = b.build()
    out = q.outline(ix)
    head = " > ".join(f"n{i} FrameLayout" for i in range(1, q.CHAIN_MAX + 1))
    assert out["lines"] == [head + " [0,0 1280x2856]", '  n602 TextView "deep" [10,10 100x50]']
    assert out["hidden"] == {"collapsed": 601 - q.CHAIN_MAX}
    assert q.find(ix, text="deep")["path"] == "n1 / n602"
    with pytest.raises(OpError) as e:
        q.resolve_selector(ix, "n999")
    assert e.value.message.startswith("n999 is not in capture c7h2kq")


# =========================================================================== #
# outline
# =========================================================================== #
def test_semantic_collapse_launcher(launcher):
    ui_nodes = [n for n in launcher.nodes.values() if n.kind != "slot"]
    assert len(ui_nodes) == 25
    out = q.outline(launcher)
    assert_grammar(out["lines"])
    # 25 nodes -> 18 lines: the chain line, 3 collapsed, 1 ViewStub hidden (the spec's
    # "about 15" was an estimate; its own capture example implies these 18).
    assert out["total"] == out["shown"] == 18
    assert nbytes(out) <= 2500
    full = q.outline(launcher, detail="all", depth=99)
    assert full["total"] == 25 and "hidden" not in full


def test_outline_root_example(launcher):
    out = q.outline(launcher, root="n10")
    assert out["root"] == "n10" and out["shown"] == out["total"] == 13
    assert out["lines"][0] == "n10 @launcher_list scroll [0,348 1280x2436]"
    assert out["lines"][-1] == ('  n22 @launch_heading "Section heading, MissingHeading" click '
                                "[0,2757 1280x27] !clipped !role !touch_target")
    assert out["lines"][1].endswith("!role !state")
    assert nbytes(out) <= 2000
    assert q.outline(launcher, root="@launcher_list")["lines"] == out["lines"]


def test_outline_depth_counts_display_levels(launcher):
    assert q.outline(launcher, depth=0)["lines"] == ["n1 DecorView [0,0 1280x2856] +23"]
    one = q.outline(launcher, depth=1)["lines"]
    assert one[1].endswith("+14")  # n9 and n10's 13 (collapsed n7, n8, n23 are counted apart)


def test_outline_views(launcher, viewscreen):
    views = q.outline(launcher, view="views", depth=99)
    assert views["total"] == 8 and all(L.parse_line(x)["segs"][0]["ref"] in
                                       ("n1", "n2", "n3", "n4", "n5", "n6", "n24", "n25")
                                       for x in views["lines"])
    comp = q.outline(launcher, view="compose", depth=99)
    assert set(refs_of(comp["lines"])) <= {n.id for n in launcher.nodes.values()
                                           if n.kind == "compose"}
    assert "n22" in refs_of(comp["lines"])
    a11y = q.outline(launcher, view="a11y", depth=99)
    assert "n22" in refs_of(a11y["lines"]) and "n2" not in refs_of(a11y["lines"])
    slots = q.outline(launcher, view="slots", depth=99)
    assert slots["lines"] == [
        "n301 ListItem [0,2757 1280x216] src=MainActivity.kt:150",
        '  n302 Text "Section heading" [48,2799 365x72] src=MainActivity.kt:151',
        '  n305 Text "MissingHeading" [48,2871 313x60] src=MainActivity.kt:152',
        "n304 HorizontalDivider [0,2973 1280x3] src=MainActivity.kt:157",
    ]
    assert slots["hidden"] == {"library": 1}  # n303 Surface (ListItem.kt)
    assert q.outline(launcher, root="n301")["lines"] == slots["lines"][:3]  # view follows root
    slots_all = q.outline(launcher, view="slots", origin="all", depth=99)
    assert "n303" in refs_of(slots_all["lines"]) and "hidden" not in slots_all
    reading = q.outline(launcher, view="reading")
    assert reading["total"] == 13
    assert reading["lines"][0] == '1. n9 TextView "A11yProbe" [48,210 322x84]'
    assert reading["lines"][12].startswith("13. n22 @launch_heading")
    assert nbytes(reading) <= 2000
    vs, _ = viewscreen
    r = q.outline(vs, view="reading")
    assert_grammar(r["lines"])
    assert any("#badImageButton" in x for x in r["lines"])


def test_outline_max_children_and_hint(launcher):
    out = q.outline(launcher, root="n10", max_children=5)
    assert out["total"] == 6
    assert out["lines"][0].endswith("+7")
    assert out["next"][0] == 'outline(root="n10",max_children=1000)'


def test_outline_argument_errors(launcher):
    with pytest.raises(OpError) as e:
        q.outline(launcher, view="slots", root="n10")
    assert e.value.code == "bad_args"
    with pytest.raises(OpError) as e:
        q.outline(launcher, view="nope")
    assert e.value.code == "bad_args"
    with pytest.raises(OpError) as e:
        q.outline(launcher, depth=-1)
    assert e.value.code == "bad_args"
    with pytest.raises(OpError) as e:
        q.outline(launcher, bogus=1)
    assert e.value.code == "bad_args" and "bogus" in e.value.message
    with pytest.raises(OpError) as e:
        q.outline(launcher, root="n10 >")
    assert e.value.code == "bad_selector"
    with pytest.raises(OpError) as e:
        q.outline(launcher, fields="+nope")
    assert e.value.code == "bad_args"
    with pytest.raises(OpError) as e:
        q.outline(launcher, fields="+props:textSize")
    assert e.value.code == "facet_unavailable"


def test_view_screen_outline_real_data(viewscreen):
    vs, _ = viewscreen
    views = [n for n in vs.nodes.values() if n.kind == "view"]
    assert len(views) == 40
    out = q.outline(vs)
    assert_grammar(out["lines"])
    assert "truncated" not in out
    # every View is on a line (the window chain holds 7) or is one of the 2 ViewStubs
    shown = refs_of(out["lines"])
    assert len(set(shown)) == len(shown) == 38 and out["hidden"] == {"zero_size": 2}
    assert out["lines"][0].startswith("n1 DecorView > n2 LinearLayout > n4 FrameLayout > "
                                      "n5 FitWindowsLinearLayout #action_bar_root > ")
    assert out["lines"][0].endswith("> n9 LinearLayout [0,0 1280x2856]")
    assert nbytes(out) <= 3000, nbytes(out)


# =========================================================================== #
# Cursors and budgets
# =========================================================================== #
def test_cursor_completeness_on_5000_nodes(big):
    pre = [n.id for n, _ in big.walk("ui")]
    assert len(pre) == 5000
    pages, lines = walk_pages(q.outline, big, depth=999, detail="all", max_children=1000,
                              max_lines=400)
    got = refs_of(lines)
    assert got == pre, "pages must form the pre-order with no duplicates or gaps"
    assert all(nbytes(p) <= 6000 for p in pages)
    # semantic mode, byte-limited pages
    _, sem_lines = walk_pages(q.outline, big, depth=999, max_children=1000, max_lines=400,
                              max_bytes=2000)
    one_shot = q.outline(big, depth=999, max_children=1000, max_lines=400, max_bytes=32000)
    assert refs_of(sem_lines)[: len(refs_of(one_shot["lines"]))] == refs_of(one_shot["lines"])
    assert len(set(refs_of(sem_lines))) == len(refs_of(sem_lines))
    # find pages
    fpages, flines = walk_pages(q.find, big, flags=["click"], limit=200)
    want = [n.id for n in big.nodes.values() if "click" in n.flags]
    assert refs_of(flines) == want and len(fpages) > 1


def test_cursor_misuse(launcher, wide):
    out = q.outline(wide)
    cur = out["truncated"]["cursor"]
    assert cur.startswith("cw1de0:o:") and cur.endswith(":80")
    assert q.cursor_capture(cur) == "cw1de0"
    page2 = q.outline(wide, cursor=cur)
    assert page2["offset"] == 80 and page2["shown"] >= 1
    for bad_kw in ({"depth": 2}, {"view": "views"}, {"fields": "+src"}):
        with pytest.raises(OpError) as e:
            q.outline(wide, cursor=cur, **bad_kw)
        assert e.value.code == "bad_args"
    with pytest.raises(OpError) as e:
        q.find(wide, cursor=cur)
    assert e.value.code == "bad_args" and "another tool" in e.value.message
    with pytest.raises(OpError) as e:
        q.outline(launcher, cursor=cur)
    assert e.value.code == "bad_args" and "cw1de0" in e.value.message
    with pytest.raises(OpError) as e:
        q.outline(wide, cursor="garbage")
    assert e.value.code == "bad_args"
    # page size is not part of the cursor identity
    assert q.outline(wide, cursor=cur, max_lines=10)["shown"] == 10


def test_budgets_never_exceeded_for_random_max_bytes(launcher, wide, viewscreen):
    vs, props = viewscreen
    small_big = big_index(1500)
    rng = random.Random(1234)
    calls = [
        lambda ix, mb: q.outline(ix, max_bytes=mb),
        lambda ix, mb: q.outline(ix, depth=99, detail="all", max_bytes=mb, max_lines=400),
        lambda ix, mb: q.outline(ix, view="reading", max_bytes=mb),
        lambda ix, mb: q.outline(ix, format="json", depth=99, max_bytes=mb, max_lines=400),
        lambda ix, mb: q.find(ix, max_bytes=mb, limit=200, fields="+src,+dp,+sel"),
        lambda ix, mb: q.find(ix, max_bytes=mb, limit=200, format="json"),
        lambda ix, mb: q.node(ix, None, [n.id for n in list(ix.nodes.values())[:10]],
                              max_bytes=mb, facets="all", children=True, ancestors=True),
        lambda ix, mb: q.node(ix, None, [list(ix.nodes)[-1]], max_bytes=mb, facets="all",
                              ancestors=True),
    ]
    checked = 0
    for _ in range(300):
        ix = rng.choice([launcher, wide, vs, small_big])
        mb = rng.randint(500, 32000)
        fn = rng.choice(calls)
        out = fn(ix, mb)
        assert nbytes(out) <= mb, (ix.meta.id, mb, nbytes(out))
        if "lines" in out or "rows" in out:
            entries = out.get("lines") or out.get("rows")
            assert entries or out["total"] == 0
        checked += 1
    # node props with a tight budget still fits and lists what it left out
    out = q.node(vs, None, ["#badSwitch"], props="all", props_fn=props.get, max_bytes=800)
    assert nbytes(out) <= 800 and out["omitted"]
    assert checked == 300


def test_truncation_is_explicit(wide):
    out = q.outline(wide, max_bytes=1500)
    tr = out["truncated"]
    assert tr["why"] == "max_bytes" and tr["omitted"] == 259 - out["shown"]
    # the cursor hint repeats the page size, so page 2 is the size of page 1
    assert out["next"][0] == f'outline(max_bytes=1500,cursor="{tr["cursor"]}")'
    assert nbytes(out["next"]) <= 200 and len(out["next"]) <= 3
    f = q.find(wide, flags=["click"], limit=5)
    assert f["truncated"]["why"] == "limit" and f["shown"] == 5 and f["total"] == 259
    assert f["next"][0] == f'find(flags=["click"],limit=5,cursor="{f["truncated"]["cursor"]}")'


# =========================================================================== #
# find filters (spec 6.2), one test each
# =========================================================================== #
def fids(out: dict) -> list[str]:
    return [L.parse_line(x)["segs"][0]["ref"] for x in out["lines"]]


def test_find_text(launcher):
    out = q.find(launcher, text="STATE")  # case-insensitive substring
    assert fids(out) == ["n15", "n16"]
    assert q.find(launcher, text="Section heading", in_="all")["total"] == 2  # n22 + slot n302


def test_find_text_re(launcher):
    assert fids(q.find(launcher, text_re=r"^(Switch|Checkbox) ")) == ["n15", "n16"]
    with pytest.raises(OpError) as e:
        q.find(launcher, text_re="(")
    assert e.value.code == "bad_args"


def test_find_type_glob_across_names(launcher, viewscreen):
    assert fids(q.find(launcher, type="*Layout")) == ["n1", "n2", "n4"]  # n1: a11y FrameLayout
    assert fids(q.find(launcher, type="textview")) == ["n9"]  # display type, any case
    assert fids(q.find(launcher, type="DecorView")) == ["n1"]
    assert "n302" in fids(q.find(launcher, type="Text", in_="all"))  # composable name
    vs, _ = viewscreen
    assert fids(q.find(vs, type="Switch")) == fids(q.find(vs, rid="*Switch"))  # a11y class


def test_find_rid_tag_globs(launcher):
    assert fids(q.find(launcher, rid="*BarBackground")) == ["n24", "n25"]
    assert fids(q.find(launcher, tag="launch_*_state")) == ["n15", "n16"]
    assert q.find(launcher, rid="CONTENT")["total"] == 0  # rids are case-sensitive


def test_find_src_implies_all(launcher):
    assert fids(q.find(launcher, src="MainActivity.kt")) == ["n22", "n301", "n302", "n305",
                                                             "n304"]
    assert fids(q.find(launcher, src="MainActivity.kt:15?")) == ["n22", "n301", "n302",
                                                                 "n305", "n304"]
    assert fids(q.find(launcher, src="*.kt:157")) == ["n304"]
    assert fids(q.find(launcher, src="MainActivity.kt", in_="ui")) == ["n22"]


def test_find_role():
    b = IndexBuilder()
    w = b.window("n1")
    b.view(w, "n2", "Button", (0, 0, 10, 10), role="Button")
    b.view(w, "n3", "View", (0, 10, 10, 10))
    b.a11y_facet("n3", role="Switch")
    ix = b.build()
    assert fids(q.find(ix, role="button")) == ["n2"]
    assert fids(q.find(ix, role="Sw*")) == ["n3"]


def test_find_flags_all_and_any(launcher):
    assert q.find(launcher, flags=["click"])["total"] == 12
    assert q.find(launcher, flags="click,scroll")["total"] == 0
    assert q.find(launcher, any_flags=["click", "scroll"])["total"] == 13
    assert fids(q.find(launcher, flags=["hidden"])) == ["n3", "n25"]
    with pytest.raises(OpError) as e:
        q.find(launcher, flags=["clickable"])
    assert e.value.code == "bad_args" and "clickable" in e.value.message


def test_find_has_and_missing(launcher):
    assert q.find(launcher, has=["stop"])["total"] == 13
    assert fids(q.find(launcher, has=["slots"])) == ["n22"]
    assert fids(q.find(launcher, has=["label"], missing=["stop"])) == []
    assert q.find(launcher, has=["props"])["total"] == 8  # the Views
    assert q.find(launcher, has=["issues"])["total"] == 12
    assert "n24" in fids(q.find(launcher, missing=["a11y", "compose"]))
    with pytest.raises(OpError) as e:
        q.find(launcher, has=["colour"])
    assert e.value.code == "bad_args"


def test_find_issue(launcher):
    assert fids(q.find(launcher, issue="render.clipped")) == ["n22"]
    assert fids(q.find(launcher, issue="clipped")) == ["n22"]
    assert q.find(launcher, issue="a11y.")["total"] == 12
    assert fids(q.find(launcher, issue="state")) == ["n11"]
    assert q.find(launcher, issue="warn")["total"] == 12
    assert q.find(launcher, issue="error")["total"] == 0


def test_find_within(launcher):
    assert q.find(launcher, within="@launcher_list", flags=["click"])["total"] == 12
    assert fids(q.find(launcher, within="#content", has=["label"], missing=["issues"])) == ["n9"]
    with pytest.raises(OpError) as e:
        q.find(launcher, within="@nope")
    assert e.value.code == "not_found"


def test_find_at_and_overlaps(launcher):
    assert fids(q.find(launcher, at=[640, 2770], kind="compose")) == ["n7", "n23", "n10", "n22"]
    assert fids(q.find(launcher, at="640,2770", flags=["click"])) == ["n22"]
    assert fids(q.find(launcher, overlaps=[0, 1300, 10, 200], flags=["click"])) == ["n15",
                                                                                     "n16"]
    with pytest.raises(OpError):
        q.find(launcher, at=[1])


def test_find_min_max_dp(launcher):
    assert fids(q.find(launcher, flags=["click"], max_dp=47)) == ["n22"]
    small = q.find(launcher, max_dp=30)
    assert "n22" in fids(small) and "n9" in fids(small)  # 84 px / 3 = 28 dp
    assert "n22" not in fids(q.find(launcher, flags=["click"], min_dp=48))


def test_find_kind_and_window():
    b = IndexBuilder()
    w0 = b.window("n1", "DecorView")
    b.view(w0, "n2", "Button", (0, 0, 100, 100), label="Main", flags=["click"])
    w1 = b.window("n3", "PopupDecorView", (100, 100, 300, 300))
    b.view(w1, "n4", "Button", (110, 110, 100, 100), label="Popup", flags=["click"])
    ix = b.build()
    assert fids(q.find(ix, flags=["click"], window="n3")) == ["n4"]
    assert fids(q.find(ix, flags=["click"], window=0)) == ["n2"]
    assert fids(q.find(ix, flags=["click"], window="w:10003")) == ["n4"]
    with pytest.raises(OpError) as e:
        q.find(ix, window="n4")
    assert e.value.code == "bad_args"
    assert q.find(ix, kind="compose")["total"] == 0
    assert q.find(ix, kind=["view"])["total"] == 4
    # point selector: the top window wins
    assert q.resolve_selector(ix, "150,150").id == "n4"
    assert q.resolve_selector(ix, "50,50").id == "n2"


def test_find_domains(launcher):
    assert q.find(launcher, in_="slots")["total"] == 5
    assert q.find(launcher)["total"] == 25
    assert q.find(launcher, **{"in": "all"})["total"] == 30
    with pytest.raises(OpError):
        q.find(launcher, in_="everything")


def test_find_sorts(launcher):
    reading = fids(q.find(launcher, has=["label"], sort="reading", limit=3))
    assert reading == ["n9", "n11", "n12"]
    top = fids(q.find(launcher, rid="*", sort="top"))
    assert top == ["n3", "n4", "n25", "n24"]  # y, then x, then tree order
    area = fids(q.find(launcher, flags=["click"], sort="area", limit=1))
    assert area == ["n22"]


def test_find_count_only_limit_and_path(launcher):
    assert q.find(launcher, flags=["click"], count_only=True) == {"capture": "c7h2kq", "total": 12}
    one = q.find(launcher, tag="launch_toggle_state")
    assert one["path"] == "n1 / n4 / n10 / n15"  # skips levels: not a selector
    assert one["next"] == ['node("n15")', 'image(ref="n15")']
    assert one["lines"][0].endswith(" in n10 @launcher_list < n4 FrameLayout #content")


def test_find_example_size(launcher):
    out = q.find(launcher, text="state", flags=["click"])
    assert out["total"] == 2 and nbytes(out) <= 600  # spec: 360 B
    assert all(" in n10 @launcher_list" in x for x in out["lines"])


def test_find_fields_projection(launcher):
    out = q.find(launcher, issue="clipped", fields="+src,+params:maxLines,overflow,+visible")
    line = out["lines"][0]
    tail = L.parse_line(line)["tail"]
    assert tail == {"src": "MainActivity.kt:150", "maxLines": "inf", "overflow": "Clip",
                    "visible": "0.125"}
    minus = q.find(launcher, tag="launch_heading", fields="-bounds,-issues,-flags")["lines"][0]
    assert minus.startswith('n22 @launch_heading "Section heading, MissingHeading" in ')
    only = q.find(launcher, tag="launch_heading", fields="label,sel")["lines"][0]
    assert only.startswith('n22 "Section heading, MissingHeading" sel=@launch_heading in ')
    with pytest.raises(OpError):
        q.find(launcher, fields="-ref")


# =========================================================================== #
# JSON rows equal line fields
# =========================================================================== #
def test_json_rows_equal_line_fields(launcher, wide, viewscreen):
    vs, props = viewscreen
    cases = [
        (launcher, q.outline, {}),
        (launcher, q.outline, {"depth": 2}),
        (launcher, q.outline, {"view": "slots", "fields": "+params:*"}),
        (launcher, q.outline, {"view": "reading", "fields": "+stop,+sel"}),
        (launcher, q.find, {"in_": "all", "fields": "+src,+dp,+ids,+conf,+visible,+declared"}),
        (wide, q.outline, {"depth": 99}),
        (vs, q.outline, {"fields": "+props:text,visibility,+hint,+state", "props_fn": props.get}),
        (vs, q.find, {"has": ["label"], "fields": "+text,+role"}),
        # a projection named like a field is namespaced, so no key repeats
        (vs, q.outline, {"fields": "+props:hint,text,+hint,+text", "props_fn": props.get}),
        (launcher, q.find, {"in_": "all", "fields": "+text,+params:text"}),
    ]
    for ix, fn, kw in cases:
        lines = fn(ix, **kw, max_bytes=32000)
        rows = fn(ix, **kw, max_bytes=32000, format="json")
        assert len(lines["lines"]) == len(rows["rows"]) > 0
        for line, row in zip(lines["lines"], rows["rows"], strict=True):
            assert L.row_matches_line(row, line), (line, row)
            assert L.format_line(row) == line
            keys = re.findall(r" ([A-Za-z_][\w.:-]*)=", line.split(" in ")[0])
            assert len(keys) == len(set(keys)), line


def test_projections_named_like_fields_are_namespaced(launcher, viewscreen):
    vs, props = viewscreen
    hint = q.find(vs, has=["label"], fields="+hint,+props:hint", props_fn=props.get,
                  text="example.com")["lines"][0]
    assert " hint=" in hint and " props.hint=" in hint
    text = q.find(launcher, in_="all", fields="+text,+params:text", text="Section heading",
                  kind="slot")["lines"][0]
    assert " params.text=" in text
    assert L.proj_key("props", "textSize") == "textSize"  # the spec's bare form stays


def test_find_issue_by_group_or_by_rule(launcher):
    ix = copy.deepcopy(launcher)
    n = ix.nodes["n12"]
    n.issues.append(Issue("a11y.label.redundant", "warn"))
    assert "!label_redundant" in q.find(ix, issue="label_redundant")["lines"][0]
    assert q.find(ix, issue="label")["total"] == q.find(ix, issue="a11y.label.")["total"] >= 1
    assert q.find(ix, issue="label_missing")["total"] == 0


# =========================================================================== #
# node
# =========================================================================== #
def test_node_n22_dossier(launcher):
    d = q.node(launcher, None, ["n22"])
    assert nbytes(d) <= 1500, nbytes(d)  # spec ~1.2 KB
    assert d["capture"] == "c7h2kq" and d["ref"] == "n22" and d["sel"] == "@launch_heading"
    assert d["b"] == [0, 2757, 1280, 27] and d["dp"] == [0, 919, 426.7, 9]
    assert d["tap_xy"] == [640, 2770]
    assert d["layout"] == {"declared": [0, 2757, 1280, 216], "visible": 0.125,
                           "clipped_by": "n10 @launcher_list (bottom 2784)"}
    assert d["a11y"]["stop"] == 13 and d["a11y"]["role"] is None
    assert d["compose"]["slots"][0].startswith("n301 ListItem [0,2757 1280x216] "
                                               "src=MainActivity.kt:150")
    assert d["issues"][0].startswith(ROLE_RULE)
    assert any(s.startswith(CLIPPED_RULE) for s in d["issues"])
    assert d["parent"] == "n10 @launcher_list" and d["children"] == 0
    assert d["next"] == ['image(ref="n22")']
    assert "omitted" not in d
    # the same node through other selectors
    path = '#content > n5 > n6 > n7 > n23 > @launcher_list > "Section heading, MissingHeading"'
    for sel in ("@launch_heading", "sem:82:448", "640,2770", path):
        assert q.node(launcher, None, [sel])["ref"] == "n22"


def test_node_facet_priority_and_omitted(launcher):
    # 683: the node's core is 17 B smaller since node() stopped repeating the key
    # its ids already spell ("key":"sem:82:448"); the packing below is unchanged
    d = q.node(launcher, None, ["n22"], facets="all", ancestors=True, children=True,
               max_bytes=683)
    assert nbytes(d) <= 683
    # issues come first (cut to fit, with a "+N more" marker), then a11y; the rest
    # is listed in omitted (short form here, since the long one does not fit)
    assert d["issues"][-1].endswith("more") and "a11y" in d
    assert not any(k in d for k in ("layout", "compose", "text", "ancestors"))
    assert [x.split("(")[0].split(":")[0] for x in d["omitted"]] == [
        "issues", "layout", "compose", "text", "ancestors"]
    roomy = q.node(launcher, None, ["n22"], facets="all", ancestors=True, max_bytes=1300)
    assert roomy["omitted"] == ['compose: node("n22",facets="compose")']
    assert roomy["next"][-1] == 'node("n22",facets="compose")'
    big_budget = q.node(launcher, None, ["n22"], facets="all", ancestors=True)
    assert "omitted" not in big_budget
    assert big_budget["ancestors"][0] == "n1 DecorView"


def test_node_facets_selection(launcher):
    d = q.node(launcher, None, ["n22"], facets="core,a11y")
    assert "a11y" in d and "issues" not in d and "compose" not in d and "layout" not in d
    t = q.node(launcher, None, ["n22"], facets="text")
    assert t["text"] == {"text": "Section heading, MissingHeading"}
    with pytest.raises(OpError):
        q.node(launcher, None, ["n22"], facets="core,nope")


def test_node_params_raw_vs_brief(launcher):
    brief = q.node(launcher, None, ["n302"])
    assert brief["slot"]["params"]["style"] == "16sp/24sp w400 ls0.5sp"
    raw = q.node(launcher, None, ["n22"], params="raw")
    assert raw["compose"]["slots"][1] == {"ref": "n302", "name": "Text",
                                          "src": "MainActivity.kt:151",
                                          "params": {"text": "Section heading",
                                                     "style": "16sp/24sp w400 ls0.5sp",
                                                     "overflow": "Clip", "maxLines": "inf"}}
    assert brief["slot"]["sem"] == ['n22 @launch_heading']


def test_node_without_slot_table():
    b = IndexBuilder()
    w = b.window("n1", "DecorView")
    acv = b.view(w, "n2", "AndroidComposeView", (0, 0, 1280, 2856), udid=82)
    b.compose(acv, "n3", sem_id=5, b=(0, 0, 100, 100), label="Hi", flags=["click"])
    ix = b.build()
    d = q.node(ix, None, ["n3"])
    assert d["compose"]["slots"] == q.SLOTS_NOT_CAPTURED  # the warning, with its cost
    # the recompose is destructive: it is never offered as a follow-up call
    assert not any(h.startswith("capture(") for h in d.get("next", []))
    bare = q.node(ix, None, ["n3"], facets="core,a11y,issues")
    assert not any(h.startswith("capture(") for h in bare.get("next", []))


def test_missing_facets_are_reported(launcher):
    b = IndexBuilder()
    w = b.window("n1", "DecorView")
    acv = b.view(w, "n2", "AndroidComposeView", (0, 0, 1280, 2856), udid=82)
    b.compose(acv, "n3", sem_id=5, b=(0, 0, 100, 100), label="Hi")
    no_slots = b.build()
    with pytest.raises(OpError) as e:
        q.outline(no_slots, view="slots")
    assert e.value.code == "facet_unavailable" and 'slots="enable"' in e.value.hint
    views_only = IndexBuilder()
    views_only.view(views_only.window("n1"), "n2", "TextView", (0, 0, 10, 10), label="x")
    assert q.outline(views_only.build(), view="slots")["total"] == 0  # no Compose at all
    no_a11y = IndexBuilder()
    no_a11y.view(no_a11y.window("n1"), "n2", "TextView", (0, 0, 10, 10), label="x")
    no_a11y.meta.set_facet("a11y", "error", reason="agent timeout")
    with pytest.raises(OpError) as e:
        q.outline(no_a11y.build(), view="a11y")
    assert e.value.code == "facet_unavailable" and "error" in e.value.message


def test_node_props_modes_wide(wide):
    def props_fn(udid):
        return wide_props(udid)

    nd = q.node(wide, None, ["#view_47"], props="nondefault", props_fn=props_fn)
    assert nbytes(nd) <= 1500, nbytes(nd)  # spec W6 / acceptance: <=1,500
    p = nd["props"]
    # 60 properties; the static defaults and the TextView class majority are dropped
    assert p == {"mode": "nondefault", "n": 2, "of": 60,
                 "values": {"layout_width": 1080, "text": "Hello world"}}
    odd = q.node(wide, None, ["n8"], props="nondefault", props_fn=props_fn)["props"]
    assert odd["values"]["paddingStart"] == 7  # udid 1008 % 7 == 0: not the class majority
    key = q.node(wide, None, ["n47"], props="key", props_fn=props_fn)["props"]
    names60 = [p["name"] for p in wide_props(1047)]
    fam = nz.class_family("TextView", names60)
    assert key["mode"] == "key"
    assert list(key["values"]) == [k for k in nz.key_props_for(fam) if k in names60]
    assert "attr_3" not in key["values"] and key["values"]["visibility"] == "visible"
    names = q.node(wide, None, ["n47"], props=["attr_1?", "alpha"], props_fn=props_fn)["props"]
    assert names["mode"] == "names" and list(names["values"]) == [
        "attr_10", "attr_11", "attr_12", "attr_13", "attr_14", "attr_15", "attr_16", "attr_17",
        "attr_18", "attr_19", "alpha"]
    full = q.node(wide, None, ["n47"], props="all", props_fn=props_fn, max_bytes=32000)["props"]
    assert full["n"] == full["of"] == 60
    tight = q.node(wide, None, ["n47"], props="all", props_fn=props_fn)
    assert nbytes(tight) <= 3000
    assert tight["props"]["n"] == 60 or tight["props"].get("more") or "props" not in tight
    unavailable = q.node(wide, None, ["n47"], props="all")
    assert unavailable["props"].startswith("unavailable")

    class Loaded:  # the LoadedCapture side of the accessor contract
        def props(self, udid):
            return wide_props(udid)

    via_loaded = q.node(wide, Loaded(), ["n47"], props="key")
    assert via_loaded["props"] == key


def test_node_props_view_screen_real(viewscreen):
    vs, props = viewscreen
    d = q.node(vs, None, ["#badSwitch"], props="nondefault", props_fn=props.get)
    assert nbytes(d) <= 1200, nbytes(d)
    p = d["props"]
    assert p["mode"] == "nondefault" and p["of"] > 90 and 0 < p["n"] < 40
    assert p["values"]["text"] == "Notifications"
    assert "checked" in p["values"] or "checked" in json.dumps(d)


def test_node_batch(launcher):
    d = q.node(launcher, None, ["n15", "n16", "@nope"])
    assert [x.get("ref") for x in d["nodes"]] == ["n15", "n16", None]
    assert d["nodes"][2]["error"]["code"] == "not_found"
    assert nbytes(d) <= 6000
    many = q.node(launcher, None, [f"n{i}" for i in range(11, 21)])
    assert len(many["nodes"]) == 10 and nbytes(many) <= 6000
    assert all("ref" in x for x in many["nodes"])
    with pytest.raises(OpError) as e:
        q.node(launcher, None, [f"n{i}" for i in range(1, 12)])
    assert e.value.code == "bad_args"
    with pytest.raises(OpError) as e:
        q.node(launcher, None, ["@nope"])
    assert e.value.code == "not_found"
    assert q.node(launcher, None, "n22")["ref"] == "n22"


def test_node_image_hook_and_issue_format(launcher):
    d = q.node(launcher, None, ["n22"], image=True, image_fn=lambda n: {"path": f"/x/{n.id}.png"},
               issue_fmt=lambda i: i.id)
    assert d["image"] == {"path": "/x/n22.png"}
    assert d["issues"] == ["a11y.role.missing_on_clickable", "a11y.touch_target.small",
                           "render.clipped"]


def test_tap_xy_is_the_visible_centre():
    b = IndexBuilder()
    w = b.window("n1")
    b.view(w, "n2", "Button", (10, 20, 101, 51), declared_b=[10, 20, 101, 300])
    b.view(w, "n3", "Button", (0, 0, 0, 0))
    ix = b.build()
    assert q.node(ix, None, ["n2"])["tap_xy"] == [60, 45]
    assert "tap_xy" not in q.node(ix, None, ["n3"])


def test_issue_text():
    i = Issue("render.clipped", "warn", {"visible_px": 27, "clipped_by": "n10",
                                         "note": "scroll edge"}, "inferred")
    assert q.issue_text(i) == ("render.clipped warn (inferred): visible_px=27 clipped_by=n10 "
                               "(scroll edge)")


# =========================================================================== #
# Selectors (spec 6.1)
# =========================================================================== #
@pytest.mark.parametrize("sel,ref", [
    ("n22", "n22"),
    ("view:82", "n6"),
    ("w:1", "n1"),
    ("sem:82:448", "n22"),
    ("compose:448", "n22"),  # legacy, unique across ComposeViews
    ("#content", "n4"),
    ("@launch_heading", "n22"),
    ("DecorView", "n1"),
    ('TextView"A11yProbe"', "n9"),
    ('"A11yProbe"', "n9"),
    ('"a11yprobe"i', "n9"),
    ('"Section heading, Missing…"', "n22"),  # a label cut by a line
    ("640,2770", "n22"),
    ("48,210", "n9"),
    ("#content > n5", "n5"),
    ('@launcher_list > "Switch state desc, MissingStateDescription"', "n15"),
    ("n10 > @launch_all", "n11"),
    ('"Section heading"', "n302"),  # no ui match: falls back to slot groups
])
def test_selector_valid_forms(launcher, sel, ref):
    assert q.resolve_selector(launcher, sel).id == ref


@pytest.mark.parametrize("sel,column", [
    ('#feed  > "Item 3"', 6),
    ('#feed >"Item 3"', 6),
    ('#feed>"Item 3"', 6),
    ('Button "Play"', 7),
    (" #feed", 1),
    ("#feed ", 6),
    ("#feed > ", 7),
    ("#feed >  @x", 9),
    ("640, 2770", 5),
    ("640,2770 > n1", 9),
    ('"abc', 1),
    ("#", 2),
    ("@ x", 2),
    ("nav", 1),
    ("n0", 1),
    ('Text"x"y', 8),
    ("view:x", 1),
    ("", 1),
    ('#a > "b\\q"', 6),
])
def test_selector_errors_report_the_exact_column(launcher, sel, column):
    with pytest.raises(OpError) as e:
        q.parse_selector(sel)
    err = e.value
    assert err.code == "bad_selector"
    assert err.column == column, (sel, err.message)
    assert err.message.startswith(f"column {column}:")
    assert err.candidates == list(q.SELECTOR_EXAMPLES)
    assert err.to_dict()["error"]["hint"]


def test_selector_not_found_gives_three_nearest(launcher):
    with pytest.raises(OpError) as e:
        q.resolve_selector(launcher, '"Section headin"')
    err = e.value
    assert err.code == "not_found" and 1 <= len(err.candidates) <= 3
    assert err.candidates[0].startswith("n22 @launch_heading")
    assert "sel=@launch_heading" in err.candidates[0]
    with pytest.raises(OpError) as e:
        q.resolve_selector(launcher, "@launch_toggle")
    assert len(e.value.candidates) == 3 and "n15" in e.value.candidates[0]
    with pytest.raises(OpError) as e:
        q.resolve_selector(launcher, "view:9999")
    assert e.value.code == "not_found"
    with pytest.raises(OpError) as e:
        q.resolve_selector(launcher, "5000,5000")
    assert e.value.code == "not_found"


def test_selector_ambiguous_lists_top_five_with_sel(launcher):
    with pytest.raises(OpError) as e:
        q.resolve_selector(launcher, "View")
    err = e.value
    assert err.code == "ambiguous" and len(err.candidates) == 5
    assert all(" sel=" in c for c in err.candidates)
    assert_grammar(err.candidates)
    assert "not listed" in err.message
    with pytest.raises(OpError) as e:
        q.resolve_selector(launcher, "#content > n5 > n6 > n7 > n23 > @launcher_list > View")
    assert e.value.code == "ambiguous"


def test_selector_ref_not_in_capture_with_tombstone(launcher):
    with pytest.raises(OpError) as e:
        q.resolve_selector(launcher, "n999")
    assert e.value.code == "ref_not_in_capture"
    tomb = {"n999": ["Switch", "Notifications", "#badSwitch", "c8m2pa"]}
    with pytest.raises(OpError) as e:
        q.resolve_selector(launcher, "n999", tomb=tomb)
    assert "c8m2pa" in e.value.message and 'Switch "Notifications"' in e.value.message
    assert e.value.candidates == ["#badSwitch"]


def test_ref_errors_say_why_the_ref_is_missing(launcher):
    # newer than every ref of this capture: it may come from a later capture
    with pytest.raises(OpError) as e:
        q.resolve_selector(launcher, "n99999", tomb={})
    assert "newer than every ref" in e.value.message and 'capture="latest"' in e.value.hint
    # older than the capture's refs, and the lineage has no record: another app or a typo
    with pytest.raises(OpError) as e:
        q.resolve_selector(launcher, "n100", tomb={})
    assert "no record" in e.value.message and "another app" in e.value.hint
    assert "capture again" not in e.value.hint


def test_a_direct_child_path_that_should_be_a_descendant_says_so(launcher):
    # @launch_heading is a grandchild of #content's subtree, not a direct child
    with pytest.raises(OpError) as e:
        q.resolve_selector(launcher, '#content > "Section heading, MissingHeading"')
    err = e.value
    assert err.code == "not_found" and "direct child" in err.message
    assert 'find(within="#content",text="Section heading, MissingHeading")' in err.hint
    assert err.candidates and err.candidates[0].startswith("n22 ")


def test_a_sel_pasted_with_its_outer_quotes_is_recognized(launcher):
    sel = '@launcher_list > "Section heading, MissingHeading"'
    assert q.resolve_selector(launcher, sel).id == "n22"
    with pytest.raises(OpError) as e:
        q.resolve_selector(launcher, json.dumps(sel))
    assert e.value.code == "not_found" and e.value.candidates == [sel]
    assert sel in e.value.hint


def test_legacy_compose_key_ambiguous_across_compose_views():
    b = IndexBuilder()
    w = b.window("n1")
    a1 = b.view(w, "n2", "AndroidComposeView", (0, 0, 100, 100), udid=82)
    a2 = b.view(w, "n3", "AndroidComposeView", (0, 100, 100, 100), udid=90)
    b.compose(a1, "n4", sem_id=7, b=(0, 0, 10, 10), label="A")
    b.compose(a2, "n5", sem_id=7, b=(0, 100, 10, 10), label="B")
    ix = b.build()
    with pytest.raises(OpError) as e:
        q.resolve_selector(ix, "compose:7")
    assert e.value.code == "ambiguous" and len(e.value.candidates) == 2
    assert q.resolve_selector(ix, "sem:90:7").id == "n5"
    assert [n.id for n in q.select(ix, "compose:7")] == ["n4", "n5"]


def test_select_returns_all_matches(launcher):
    assert len(q.select(launcher, "View")) == 15  # 2 Views + 13 a11y android.view.View
    assert q.select(launcher, "@nope") == []
    assert [n.id for n in q.select(launcher, "n22")] == ["n22"]


# =========================================================================== #
# Performance (spec 13.4: p95 <= 20 ms on a 5,000-node synthetic)
# =========================================================================== #
def test_p95_per_call_on_5000_nodes(big):
    calls = {
        "outline()": lambda: q.outline(big),
        "outline(all, page)": lambda: q.outline(big, depth=999, detail="all", max_lines=400,
                                                max_bytes=32000),
        "outline(root)": lambda: q.outline(big, root="#v9"),
        "find(text)": lambda: q.find(big, text="Item 49"),
        "find(flags,type)": lambda: q.find(big, flags=["click"], type="Text*", sort="area"),
        "node(#rid)": lambda: q.node(big, None, ["#v4999"]),
        "node(batch)": lambda: q.node(big, None, [f"n{i}" for i in range(4990, 5000)]),
    }
    # The engine's own latency: a cyclic-GC pass over the whole test session's
    # heap is not part of a query, so collect first and pause the collector.
    for name, fn in calls.items():
        fn()
        gc.collect()
        gc.disable()
        try:
            ts = []
            for _ in range(21):
                t = time.perf_counter()
                fn()
                ts.append((time.perf_counter() - t) * 1000)
        finally:
            gc.enable()
        ts.sort()
        p95 = ts[19]
        assert p95 <= 20.0, f"{name}: p95 {p95:.1f} ms"


# =========================================================================== #
# Section 8 workflows (token targets on the builder and real-data scenes)
# =========================================================================== #
def test_workflow_sizes(launcher, wide, viewscreen):
    vs, props = viewscreen
    sizes = {
        # W1 "why is the Section heading row cut off": node(n22) (spec ~1.2 KB)
        "W1 node(n22)": (nbytes(q.node(launcher, None, ["n22"])), 1500),
        # W2 "is the checkout button accessible": find + node(core,a11y,issues)
        "W2 find": (nbytes(q.find(launcher, text="state", flags=["click"])), 600),
        "W2 node": (nbytes(q.node(launcher, None, ["n15"], facets="core,a11y,issues")), 1000),
        # W3: node(#badSwitch) gives tap_xy
        "W3 node(#badSwitch)": (nbytes(q.node(vs, None, ["#badSwitch"])), 1200),
        # W4 audit: reading order
        "W4 outline(reading)": (nbytes(q.outline(launcher, view="reading")), 2000),
        # capture-level previews and outlines (acceptance 13.2)
        "launcher outline()": (nbytes(q.outline(launcher)), 2500),
        "launcher outline(root=list)": (nbytes(q.outline(launcher, root="n10")), 2000),
        "launcher outline(slots)": (nbytes(q.outline(launcher, view="slots")), 6000),
        "viewscreen outline()": (nbytes(q.outline(vs)), 3000),
        "viewscreen node(props=nondefault)": (nbytes(q.node(vs, None, ["#badSwitch"],
                                                          props="nondefault",
                                                          props_fn=props.get)), 1200),
        # W6 259-view list screen
        "W6 find(Label 4)": (nbytes(q.find(wide, text="Label 4", limit=20)), 3000),
        "W6 node(props=nondefault)": (nbytes(q.node(wide, None, ["n47"], props="nondefault",
                                                    props_fn=wide_props)), 1500),
        "W6 outline(root=n3,depth=1)": (nbytes(q.outline(wide, root="n3", depth=1)), 1200),
    }
    over = {k: v for k, v in sizes.items() if v[0] > v[1]}
    assert not over, over
    # W6: walking everything is a few pages of <=6 KB with exactly 259 View lines
    pages, lines = walk_pages(q.outline, wide, depth=99)
    assert len(pages) <= 4 and all(nbytes(p) <= 6000 for p in pages)
    assert len(lines) == 259 and len(set(refs_of(lines))) == 259
    total = sum(v[0] for k, v in sizes.items() if k.startswith(("W1", "W2", "W6")))
    assert total / 3.5 < 2500  # the spec's ~2.4k-token W6 walk and ~1k-token W1/W2


# --------------------------------------------------------------------------- integration
def _row_screen() -> Any:
    """A clickable Compose row whose two Text children are a11y-only nodes (how the
    index builder keeps text that Compose merged into the row), plus a plain text."""
    b = IndexBuilder("crow00", screen=(1080, 800), dpi=420)
    w = b.window("n1", "DecorView", (0, 0, 1080, 800), udid=1)
    acv = b.view(w, "n2", "AndroidComposeView", (0, 0, 1080, 800), udid=2,
                 cls="AndroidComposeView")
    row = b.compose(acv, "n3", sem_id=5, b=(0, 0, 1080, 200), type="ListItem",
                    label="Title, Subtitle", flags=["click"],
                    attrs={"Focused": "false", "TestTag": "row"})
    b.a11y(row, "n4", host=2, virt=6, b=(40, 20, 400, 60), label="Title")
    b.a11y(row, "n5", host=2, virt=7, b=(40, 90, 400, 50), label="Subtitle")
    b.a11y(acv, "n6", host=2, virt=8, b=(40, 300, 400, 50), label="Footer")
    b.a11y_facet("n3", host=2, virt=5, flags=["click", "focus"],
                 res="com.example:id/row_view")
    slot = b.slot(None, "n7", name="Text", src="Row.kt:12", b=(40, 20, 400, 60),
                  params={"text": "Title", "softWrap": "true", "maxLines": "2147483647",
                          "minLines": "1", "overflow": "2",
                          "content": "androidx.compose.runtime.internal.ComposableLambdaImpl@3b2c1a"})
    b.link_slots("n3", [slot])
    return b.build()


def test_outline_folds_merged_a11y_text_into_its_clickable_row() -> None:
    ix = _row_screen()
    out = q.outline(ix)
    refs = [line.split()[0] for line in out["lines"]]
    assert "n3" in refs and "n4" not in refs and "n5" not in refs
    assert "n6" in refs  # not under a clickable parent: its own line
    assert out["hidden"]["collapsed"] >= 2
    everything = q.outline(ix, detail="all")
    assert {"n4", "n5"} <= {line.split()[0] for line in everything["lines"]}


def test_node_leaves_out_what_other_fields_already_say() -> None:
    ix = _row_screen()
    ix.nodes["n3"].since = "crow00"  # minted in this capture: match="new" says so
    ix.nodes["n3"].match = "new"
    d = q.node(ix, None, ["n3"])
    assert d["match"] == "new" and "since" not in d
    assert "Focused" not in d["compose"]["sem"] and d["compose"]["sem"]["TestTag"] == "row"
    (slot_line,) = d["compose"]["slots"]
    assert slot_line.endswith("src=Row.kt:12 overflow=Ellipsis")  # defaults left out
    ix.nodes["n3"].since = "cother"
    assert q.node(ix, None, ["n3"])["since"] == "cother"
    ix.nodes["n3"].rid = "row_view"
    assert "res" not in q.node(ix, None, ["n3"])["a11y"]  # it only repeats the rid
    ix.nodes["n3"].rid = "other"
    assert q.node(ix, None, ["n3"])["a11y"]["res"] == "com.example:id/row_view"
    ix.nodes["n3"].facets["compose"]["attrs"]["Focused"] = "true"
    assert q.node(ix, None, ["n3"])["compose"]["sem"]["Focused"] == "true"
