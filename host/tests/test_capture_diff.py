"""Offline tests for capture/diff.py: comparing two captures by ref (spec 5.10)."""

from __future__ import annotations

import copy
import json
import random
import re

import pytest
from capture_builders import IndexBuilder, wide_index
from capture_scenes import C, Chain, S, V, scene

from inspector_widget.capture import diff as d
from inspector_widget.capture import model as m
from inspector_widget.output import dumps

LINE_RE = re.compile(r'^[~+\->] (n\d+|\d+ nodes in )')


def size(obj) -> int:
    return len(dumps(obj).encode("utf-8"))


# --------------------------------------------------------------------------- the spec example
def view_screen(cid: str, *, checked: bool, created_at: float, label: str | None = None):
    """The View screen as the spec's diff example sees it: 40 views, n63 is
    Switch #badSwitch "Notifications"."""
    b = IndexBuilder(cid, created_at=created_at)
    n1 = b.window("n1", "DecorView", udid=1)
    n2 = b.view(n1, "n2", "LinearLayout", (0, 0, 1280, 2856), udid=2)
    y = 0
    for k in range(40, 78):
        ref = f"n{k}"
        if ref == "n63":
            flags = ["click", "focus", "checkable"] + (["checked"] if checked else [])
            b.view(n2, ref, "Switch", (0, y, 1280, 144), udid=k, rid="badSwitch",
                   label="Notifications", text="Notifications", flags=flags)
        else:
            b.view(n2, ref, "TextView", (0, y, 1280, 60), udid=k, label=f"Row {k}",
                   text=f"Row {k}")
        y += 70
    ix = b.build()
    ix.meta.label = label
    return ix


def switch_props(checked: bool, pressed: bool):
    props = {"n63": {"checked": checked, "text": "Notifications", "pressed": pressed,
                     "textSize": {"value": 42.0, "source": "@style/Body"}}}
    return lambda n: props.get(n.id, {"text": n.label})


def test_bad_switch_toggle_matches_the_spec_example():
    a = view_screen("c8m2pa", checked=False, created_at=1_790_000_000.0, label="before")
    b = view_screen("c9q4tz", checked=True, created_at=1_790_000_002.4)
    out = d.diff(a, b, props_a=switch_props(False, False), props_b=switch_props(True, True))
    assert out == {
        "a": "c8m2pa @before", "b": "c9q4tz", "dt_s": 2.4, "same_pid": True,
        "summary": {"changed": 1, "added": 0, "removed": 0, "moved": 0, "unchanged": 39},
        "lines": ['~ n63 Switch #badSwitch "Notifications": unchecked -> checked',
                  "~ n63 props checked false -> true"],
        "issues": {"resolved": [], "new": []},
        "next": ['image(ref="n63",capture="c9q4tz")'],
    }
    assert size(out) <= 1000
    # without properties the state change is still there, and nothing else
    out2 = d.diff(a, b)
    assert out2["lines"] == ['~ n63 Switch #badSwitch "Notifications": unchecked -> checked']
    # an unchanged pair: nothing to report
    same = d.diff(a, view_screen("c9q4ta", checked=False, created_at=1_790_000_005.0))
    assert same["lines"] == [] and same["summary"]["unchanged"] == 40 and same["next"] == []


# --------------------------------------------------------------------------- every change class
def settings(cid: str, *, after: bool, pid: int = 100, lint: str = "tree"):
    """A small screen; ``after=True`` applies one change of every class."""
    W = 1080

    def tv(udid, rid, label, y, **kw):
        return V("TextView", udid, rid=rid, label=label, b=(0, y, W, 100), **kw)

    title = tv(3, "title", "Preferences" if after else "Settings", 2 if after else 0)
    wifi = V("Switch", 4, rid="wifi", label="Wi-Fi", b=(0, 100, W, 100),
             flags=["click", "checkable"] + (["checked"] if after else []),
             a11y={"speakable": "Wi-Fi", "actions": ["CLICK"]})
    save = V("Button", 5, rid="save", label="Save", b=(0, 200, W, 100),
             flags=["click", "disabled"] if after else ["click"],
             a11y={"speakable": "Save", "actions": [] if after else ["CLICK"]},
             role="Button", stop=None if after else 3)
    hint = tv(6, "hint", "Tip", 300, flags=["hidden"] if after else [])
    alpha, beta, gamma = (tv(8, None, "Alpha", 400), tv(9, None, "Beta", 500),
                          tv(10, None, "Gamma", 600))
    box = V("LinearLayout", 7, *([gamma, alpha, beta] if after else [alpha, beta, gamma]),
            rid="box", b=(0, 400, W, 300))
    img = V("ImageView", 13, V("TextView", 14, rid="cap", label="Caption",
                               b=(0, 1000, 100, 20)), rid="img", b=(0, 1000, 100, 100))
    other = V("LinearLayout", 11, *([img] if after else []), rid="other", b=(0, 700, W, 300))
    panel = V("FrameLayout", 12, *([] if after else [img]), rid="panel",
              b=(0, 1000, W, 260 if after else 200))
    gone = tv(15, "gone", "Old", 1200, kids=[tv(16, None, "Old child", 1250)])
    new = tv(17, "new", "New", 1200, kids=[tv(18, None, "New child", 1250)])
    kids = [title, wifi, save, hint, box, other, panel] + ([new] if after else [gone])
    return scene(V("DecorView", 1, V("LinearLayout", 2, *kids, rid="content",
                                     b=(0, 0, W, 2000)), b=(0, 0, W, 2000)),
                 cid=cid, pid=pid, lint=lint)


def settings_pair():
    chain = Chain()
    a = chain.publish(settings("c00001", after=False))
    b = chain.publish(settings("c00002", after=True))
    return a, b


def test_every_change_class_is_reported():
    a, b = settings_pair()
    r = {k: b.by_key[k] for k in b.by_key if k.startswith("view:")}
    out = d.diff(a, b, max_bytes=0)
    lines = out["lines"]
    for ln in lines:
        assert LINE_RE.match(ln), ln

    def ref(u: int) -> str:
        return r[f"view:{u}"]

    assert f'~ {ref(3)} TextView #title: label "Settings" -> "Preferences"' \
        in lines  # the 2px move is under min_move_px
    assert f'~ {ref(4)} Switch #wifi "Wi-Fi": unchecked -> checked' in lines
    assert f'~ {ref(5)} Button #save "Save": enabled -> disabled' in lines
    assert f"~ {ref(5)} actions -CLICK" in lines
    assert f"~ {ref(5)} stop 3 -> -" in lines
    assert f'~ {ref(6)} TextView #hint "Tip": shown -> hidden' in lines
    assert f'> {ref(10)} TextView "Gamma": reordered in {ref(7)} (2 -> 0)' in lines
    assert not any(ln.startswith((f"> {ref(8)} ", f"> {ref(9)} "))
                   for ln in lines)  # the LIS keeps Alpha and Beta in place
    assert f"> {ref(13)} ImageView #img: {ref(12)} -> {ref(11)} LinearLayout #other" in lines
    assert f"~ {ref(12)} FrameLayout #panel: bounds [0,1000 1080x200] -> [0,1000 1080x260]" \
        in lines
    assert f'+ {ref(17)} TextView #new "New" [0,1200 1080x100] +1' in lines
    assert f'- {a.by_key["view:15"]} TextView #gone "Old" [0,1200 1080x100] +1' in lines
    assert out["summary"] == {"changed": 5, "added": 2, "removed": 2, "moved": 2,
                              "unchanged": 7}
    # removals come last; everything else follows b's pre-order
    assert lines[-1].startswith("- ")
    assert out["next"][0] == f'image(ref="{ref(3)}",capture="c00002")'


def test_min_move_px_and_include_filter():
    a, b = settings_pair()
    title = b.by_key["view:3"]
    loose = d.diff(a, b, min_move_px=1, max_bytes=0)
    assert loose["lines"][:2] == [
        f'~ {title} TextView #title: label "Settings" -> "Preferences"',
        f"~ {title} shifted by 0,2 to [0,2 1080x100]"]
    only_text = d.diff(a, b, include=["text"], max_bytes=0)
    assert [ln for ln in only_text["lines"] if ln.startswith("~")] == [
        f'~ {title} TextView #title: label "Settings" -> "Preferences"']
    assert "issues" not in only_text
    plus = d.diff(a, b, include="+props", props_a=lambda n: {"x": 1},
                  props_b=lambda n: {"x": 2 if n.rid == "wifi" else 1}, max_bytes=0)
    assert f'~ {b.by_key["view:4"]} props x 1 -> 2' in plus["lines"]
    noted = d.diff(a, b, include=["props", "text"])
    assert "props not compared: not captured in both" in noted["notes"]
    with pytest.raises(m.OpError) as e:
        d.diff(a, b, include=["text", "colour"])
    assert e.value.code == "bad_args"
    for bad in ({"limit": 0}, {"limit": 201}, {"min_move_px": -1}, {"max_bytes": 100},
                {"max_bytes": "x"}):
        with pytest.raises(m.OpError):
            d.diff(a, b, **bad)


def test_state_text_a11y_and_visibility_details():
    def one(cid, **kw):
        base = {"label": "Mute", "text": "Mute", "flags": ["click", "checkable"],
                "state": "Off", "role": "Switch", "visible": 1.0,
                "a11y": {"speakable": "Mute, Off", "actions": ["CLICK"]}}
        base.update(kw)
        return scene(V("DecorView", 1, V("Switch", 2, rid="mute", b=(0, 0, 100, 50), **base),
                       b=(0, 0, 100, 100)), cid=cid)

    chain = Chain()
    a = chain.publish(one("c00001"))
    b = chain.publish(one("c00002", flags=["click", "checkable", "checked", "selected",
                                           "longclick"],
                          state="On", role="Toggle", visible=0.25, hint="Tap",
                          a11y={"speakable": "Mute, On", "actions": ["CLICK", "LONG_CLICK"]}))
    ref = b.by_key["view:2"]
    out = d.diff(a, b, max_bytes=0)
    assert out["lines"] == [
        f'~ {ref} Switch #mute "Mute": unchecked, unselected -> checked, selected',
        f'~ {ref} state "Off" -> "On"',
        f'~ {ref} hint - -> "Tap"',
        f"~ {ref} visible 100% -> 25%",
        f"~ {ref} role Switch -> Toggle",
        f'~ {ref} speakable "Mute, Off" -> "Mute, On"',
        f"~ {ref} actions +LONG_CLICK",
        f"~ {ref} flags +longclick",
    ]
    # a label change that the speakable text simply follows is reported once
    c = chain.publish(one("c00003", label="Sound", text="Sound",
                          a11y={"speakable": "Sound", "actions": ["CLICK"]}))
    b2 = chain.publish(one("c00004", label="Sound 2", text="Sound 2",
                           a11y={"speakable": "Sound 2", "actions": ["CLICK"]}))
    assert d.diff(c, b2)["lines"] == [f'~ {ref} Switch #mute: label "Sound" -> "Sound 2"']


def test_long_and_odd_labels_are_escaped_and_cut():
    assert d.quote('say "hi"\nnow') == '"say \\"hi\\"\\nnow"'
    assert d.quote("x" * 60) == '"' + "x" * 47 + '…"'
    assert d.quote(None) == "-"
    assert d.box([1, 2, 3, 4]) == "[1,2 3x4]" and d.box(None) == "-"


# --------------------------------------------------------------------------- collections
def feed(cells, *, cid, dy=0):
    rows = []
    for i, (u, label) in enumerate(cells):
        y = 50 + 100 * i + dy
        rows.append(V("LinearLayout", u,
                      V("TextView", u + 1, rid="title", label=label, b=(0, y, 300, 50)),
                      V("ImageButton", u + 2, rid="delete", label="Delete", b=(300, y, 100, 50)),
                      b=(0, y, 400, 100)))
    return scene(V("DecorView", 1,
                   V("TextView", 2, rid="header", label="Inbox", b=(0, 0, 400, 50)),
                   V("RecyclerView", 3, *rows, rid="feed", b=(0, 50, 400, 500)),
                   b=(0, 0, 400, 600)), cid=cid)


def test_recycled_cell_is_one_rebound_line():
    chain = Chain()
    a = chain.publish(feed([(100, "Item 0"), (200, "Item 1"), (300, "Item 2")], cid="c00001"))
    b = chain.publish(feed([(200, "Item 1"), (300, "Item 2"), (100, "Item 3")], cid="c00002",
                           dy=-100))
    cell_new, cell_old = b.by_key["view:100"], a.by_key["view:100"]
    out = d.diff(a, b, max_bytes=0)
    assert out["summary"]["rebound"] == 3
    assert out["summary"]["added"] == 0 and out["summary"]["removed"] == 0
    assert f"~ {cell_new} LinearLayout: rebound, was {cell_old} (+2 inside)" in out["lines"]
    # the two cells that stayed shifted up together with their children
    feed_ref = b.by_key["view:3"]
    assert any(ln.startswith(f"~ {b.by_key['view:200']} LinearLayout: shifted by 0,-200")
               for ln in out["lines"])
    assert all("rebound" in ln or "shifted" in ln for ln in out["lines"]), out["lines"]
    assert feed_ref not in " ".join(out["lines"])  # the RecyclerView itself did not change


def test_a_scroll_collapses_into_one_shift_line():
    cells = [(100 * k, f"Item {k}") for k in range(1, 6)]
    chain = Chain()
    a = chain.publish(feed(cells, cid="c00001"))
    b = chain.publish(feed(cells, cid="c00002", dy=-40))
    feed_ref = b.by_key["view:3"]
    cell_refs = [b.by_key[f"view:{100 * k}"] for k in range(1, 6)]
    out = d.diff(a, b)
    assert out["lines"] == [
        (f"~ 5 nodes in {feed_ref} RecyclerView #feed shifted by 0,-40: "
         f"{cell_refs[0]} {cell_refs[1]} {cell_refs[2]} +2 (+10 inside)")]
    assert out["summary"]["changed"] == 15 and out["summary"]["unchanged"] == 3


def test_lazycolumn_reminting_shows_only_real_changes():
    def lazy(items, cid):
        rows = [C(sid, label=lb, b=(0, 100 * i, 400, 90)) for i, (sid, lb) in enumerate(items)]
        return scene(V("DecorView", 1, V("AndroidComposeView", 82,
                                          C(40, *rows, tag="feed",
                                            attrs={"CollectionInfo": "CollectionInfo"},
                                            b=(0, 0, 400, 800)), b=(0, 0, 400, 800)),
                       b=(0, 0, 400, 800)), cid=cid)

    chain = Chain()
    a = chain.publish(lazy([(10, "Row 0"), (11, "Row 1")], "c00001"))
    b = chain.publish(lazy([(20, "Row 0"), (21, "Row 1"), (22, "Row 2")], "c00002"))
    out = d.diff(a, b)
    assert out["summary"] == {"changed": 0, "added": 1, "removed": 0, "moved": 0,
                              "unchanged": 5}
    assert out["lines"] == [f'+ {b.by_key["sem:82:22"]} "Row 2" [0,200 400x90]']


# --------------------------------------------------------------------------- verdicts, scope
def pair_with_shared(n_a: int, n_b: int, shared: int):
    """Two captures of one lineage whose ui trees share exactly ``shared`` refs."""
    def screen(udids, cid):
        return scene(V("DecorView", 1, *[V("TextView", u, label=f"T{u}", b=(0, 10 * u, 50, 10))
                                         for u in udids], b=(0, 0, 400, 4000)), cid=cid)

    chain = Chain()
    a = chain.publish(screen(range(2, n_a + 1), "c00001"))
    keep = list(range(2, shared + 1))
    b = chain.publish(screen(keep + list(range(1000, 1000 + n_b - shared)), "c00002"))
    return a, b


def test_new_screen_verdict_below_forty_percent_shared():
    a, b = pair_with_shared(13, 13, 6)  # 6 shared of 20 refs: 30%
    out = d.diff(a, b)
    assert out["verdict"] == "new screen"
    assert out["shared"] == "6 of 20 refs (30%)"
    assert out["summary"] == {"added": 7, "removed": 7, "kept": 6}
    assert "lines" not in out
    assert out["outline"][0].startswith(f'{b.by_key["view:1"]} DecorView')
    assert out["next"] == ['outline(capture="c00002")']
    assert size(out) <= d.DEFAULT_MAX_BYTES
    near = d.diff(*pair_with_shared(13, 13, 9))  # 9 of 17: 53%
    assert "verdict" not in near and near["summary"]["unchanged"] == 9
    custom = d.diff(a, b, preview=lambda ix, root: ["n1 custom"])
    assert custom["outline"] == ["n1 custom"]


def test_cross_lineage_is_bad_args():
    a = view_screen("c8m2pa", checked=False, created_at=1.0)
    b = view_screen("c9q4tz", checked=True, created_at=2.0)
    b.meta.lineage = ("emulator-5554", "com.other.app")
    with pytest.raises(m.OpError) as e:
        d.diff(a, b)
    assert e.value.code == "bad_args" and "lineage" in e.value.message


def test_within_limits_the_scope():
    a, b = settings_pair()
    box = b.by_key["view:7"]
    out = d.diff(a, b, within=box, max_bytes=0)
    assert out["within"] == box
    assert out["summary"] == {"changed": 0, "added": 0, "removed": 0, "moved": 1,
                              "unchanged": 3}
    assert d.diff(a, b, within="view:7")["within"] == box  # keys work too
    gone = a.by_key["view:15"]  # only in a
    assert d.diff(a, b, within=gone, max_bytes=0)["summary"]["removed"] == 2
    with pytest.raises(m.OpError) as e:
        d.diff(a, b, within="n999")
    assert e.value.code == "not_found"

    def resolve(ix, sel):
        if sel == "#box":
            return ix.get("view:7")
        raise m.OpError("not_found", sel)

    assert d.diff(a, b, within="#box", resolve=resolve)["within"] == box


# --------------------------------------------------------------------------- issues, props, params
def test_issue_deltas_by_ref_and_rule():
    def screen(cid, issues, lint="tree"):
        return scene(V("DecorView", 1,
                       *[V("Button", u, rid=f"b{u}", label=f"B{u}", b=(0, 50 * u, 100, 40),
                           issues=issues.get(u, ())) for u in range(2, 8)],
                       b=(0, 0, 400, 800)), cid=cid, lint=lint)

    role, touch, contrast = ("a11y.role.missing_on_clickable", "a11y.touch_target.small",
                             "a11y.contrast.text")
    chain = Chain()
    a = chain.publish(screen("c00001", {u: [role] for u in range(2, 8)} | {7: [role, contrast]},
                             lint="full"))
    b = chain.publish(screen("c00002", {2: [role], 3: [touch]}))
    out = d.diff(a, b)
    r = [a.by_key[f"view:{u}"] for u in range(2, 8)]
    assert out["issues"] == {
        "resolved": [f"{role} ×5: {r[1]} {r[2]} {r[3]} +2"],
        "new": [f"{touch} ×1: {r[1]}"],
    }
    assert "contrast issues not compared (lint=full in only one capture)" in out["notes"]
    assert out["summary"]["changed"] == 0  # an issue change alone is not a node change
    assert 'lint(capture="c00002")' in out["next"]
    c = chain.publish(screen("c00003", {}, lint="none"))
    out2 = d.diff(b, c)
    assert "issues" not in out2 and "issues not compared: lint=none in b" in out2["notes"]


def test_params_are_compared_when_both_captures_have_slots():
    def comp(cid, text, with_slots=True):
        slots = (S("Column", "Column@Main.kt:10:0",
                   S("Text", "Text@Main.kt:11:0", params={"text": text, "maxLines": "1"},
                     b=(0, 0, 100, 20)),
                   acv=82, b=(0, 0, 100, 100)),) if with_slots else ()
        return scene(V("DecorView", 1, V("AndroidComposeView", 82, C(5, label=text,
                                                                      b=(0, 0, 100, 20)),
                                          b=(0, 0, 100, 100)), b=(0, 0, 100, 100)),
                     slots=slots, cid=cid)

    chain = Chain()
    a = chain.publish(comp("c00001", "Hello"))
    b = chain.publish(comp("c00002", "Hello there"))
    out = d.diff(a, b, max_bytes=0)
    slot_ref = next(r for r, n in b.nodes.items() if n.kind == "slot" and n.type == "Text")
    assert f'~ {slot_ref} Text: params text Hello -> "Hello there"' in out["lines"]
    c = chain.publish(comp("c00003", "Hello there", with_slots=False))
    out2 = d.diff(b, c, include=["text", "params"])
    assert "slot table only in a; slot nodes not compared" in out2["notes"]
    assert "params not compared: slot table not captured in both" in out2["notes"]
    assert out2["summary"]["removed"] == 0


def test_props_diff_caps_per_node_and_skips_volatile():
    a = view_screen("c8m2pa", checked=False, created_at=1.0)
    b = view_screen("c9q4tz", checked=False, created_at=2.0)
    pa = {f"p{i}": i for i in range(10)} | {"pressed": False}
    pb = {f"p{i}": i + 1 for i in range(10)} | {"pressed": True}
    out = d.diff(a, b, props_a=lambda n: pa if n.id == "n40" else {},
                 props_b=lambda n: pb if n.id == "n40" else {}, max_bytes=0)
    assert out["lines"][0] == '~ n40 TextView "Row 40": props p0 0 -> 1'
    assert len(out["lines"]) == d.PROPS_PER_NODE + 1
    assert out["lines"][-1] == "~ n40 props +4 more"
    assert not any("pressed" in ln for ln in out["lines"])


def test_pixel_diff_is_injected():
    a, b = settings_pair()
    calls = []

    def fake(ia, ib, refs):
        calls.append(refs)
        return {"path": "/tmp/x.png", "changed_px": 12}

    out = d.diff(a, b, image=True, pixel_diff=fake)
    assert out["image"] == {"path": "/tmp/x.png", "changed_px": 12}
    assert calls and calls[0][0] == b.by_key["view:3"]
    out2 = d.diff(a, b, include="+pixels")
    assert "pixels not compared: no image backend here" in out2["notes"]


# --------------------------------------------------------------------------- budgets, cursors
def wide_pair():
    a = wide_index(capture_id="cw1de0")
    b = wide_index(capture_id="cw1de1")
    for n in b.nodes.values():
        if n.label:
            n.label = n.label + " (new)"
            n.text = n.label
    return a, b


def all_pages(a, b, **kw):
    lines, cursor, pages = [], None, 0
    while True:
        out = d.diff(a, b, cursor=cursor, **kw)
        pages += 1
        lines.extend(out["lines"])
        tr = out.get("truncated")
        if not tr:
            return lines, pages, out
        assert tr["omitted"] > 0
        cursor = tr["cursor"]
        assert cursor in out["next"][0]
        assert pages < 1000


def test_budgets_are_respected_at_random_max_bytes():
    a, b = wide_pair()
    full, pages, _ = all_pages(a, b, max_bytes=0, limit=200)
    assert len(full) == 216 and pages == 2  # 216 labelled leaves changed
    rng = random.Random(7)
    for _ in range(40):
        mb = rng.randint(500, 32000)
        lim = rng.randint(1, 200)
        out = d.diff(a, b, max_bytes=mb, limit=lim)
        assert size(out) <= mb, (mb, lim, size(out))
        assert len(out["lines"]) <= lim
        if out.get("truncated"):
            assert out["truncated"]["why"] in ("max_lines", "max_bytes")
    for mb in (500, 1200, 4000):
        got, pages, last = all_pages(a, b, max_bytes=mb)
        assert got == full  # every line exactly once, in order, across pages
        assert size(last) <= mb
    default = d.diff(a, b)
    assert size(default) <= d.DEFAULT_MAX_BYTES
    assert len(default["lines"]) == d.DEFAULT_LIMIT
    assert default["truncated"] == {"omitted": 176, "why": "max_lines",
                                    "cursor": default["truncated"]["cursor"]}
    assert d.diff(a, b, max_bytes=2000)["truncated"]["why"] == "max_bytes"
    assert d._max_bytes(10**6) == d.HARD_MAX_BYTES and d._max_bytes(0) == 0


def test_cursor_is_bound_to_its_arguments():
    a, b = wide_pair()
    cur = d.diff(a, b)["truncated"]["cursor"]
    assert re.fullmatch(r"cw1de1:d:[0-9a-f]{8}:\d+", cur)
    assert d.diff(a, b, cursor=cur)["lines"]
    for bad_kw in ({"include": ["text"]}, {"min_move_px": 9}, {"within": "n2"}):
        with pytest.raises(m.OpError) as e:
            d.diff(a, b, cursor=cur, **bad_kw)
        assert e.value.code == "bad_args"
    for bad in ("junk", "cw1de1:o:12345678:3", cur.replace("cw1de1", "cw1de2"),
                cur.rsplit(":", 1)[0] + ":-1", cur.rsplit(":", 1)[0] + ":x"):
        with pytest.raises(m.OpError):
            d.diff(a, b, cursor=bad)
    # the page size is not part of the arguments
    assert d.diff(a, b, cursor=cur, limit=5, max_bytes=2000)["lines"]


def test_new_screen_and_tiny_budgets_still_fit():
    a, b = pair_with_shared(60, 60, 2)
    for mb in (500, 700, 1500):
        out = d.diff(a, b, max_bytes=mb)
        assert out["verdict"] == "new screen" and size(out) <= mb


def test_diff_does_not_mutate_its_inputs():
    a, b = settings_pair()
    before = (json.dumps([n.to_dict() for n in a.nodes.values()], sort_keys=True),
              json.dumps([n.to_dict() for n in b.nodes.values()], sort_keys=True))
    d.diff(a, b, max_bytes=600)
    d.diff(a, b, max_bytes=0, within=b.by_key["view:7"])
    after = (json.dumps([n.to_dict() for n in a.nodes.values()], sort_keys=True),
             json.dumps([n.to_dict() for n in b.nodes.values()], sort_keys=True))
    assert before == after
    snapshot = copy.deepcopy(a.trees["ui"].to_dict())
    d.diff(a, a)
    assert a.trees["ui"].to_dict() == snapshot


def test_outline_preview_and_lines_follow_the_grammar():
    b = view_screen("c9q4tz", checked=True, created_at=1.0)
    lines = d.outline_preview(b)
    assert lines[0] == "n1 DecorView [0,0 1280x2856]"
    assert lines[1] == "  n2 LinearLayout [0,0 1280x2856] +38"
    n = b.nodes["n63"]
    n.issues.append(m.Issue("a11y.role.missing_on_clickable"))
    n.issues.append(m.Issue("render.clipped"))
    assert d.node_line(n, 1, 3) == ('  n63 Switch #badSwitch "Notifications" click focus '
                                    "checkable checked [0,1610 1280x144] !role !clipped +3")
    assert d.issue_short("a11y.touch_target.small") == "touch_target"
    assert d.issue_short("render.text_overflow") == "text_overflow"


def test_diff_of_5000_nodes_is_fast():
    import time

    from capture_builders import big_index

    a = big_index(5000)
    b = big_index(5000, capture_id="cb1g01")
    for n in b.nodes.values():
        if n.label:
            n.label += "!"
        n.b = [n.b[0], n.b[1] + 10, n.b[2], n.b[3]]
    t0 = time.perf_counter()
    out = d.diff(a, b)
    assert time.perf_counter() - t0 < 0.5
    assert out["summary"]["changed"] == 5000 and size(out) <= d.DEFAULT_MAX_BYTES
    # everything shifted by the same amount: one line for the root, the rest inside
    assert out["lines"][0] == "~ n1 FrameLayout #root: shifted by 0,10 to [0,10 1280x2856] " \
                              "(+4999 inside)"


def test_refs_and_diff_import_without_protobuf_or_pillow():
    import os
    import subprocess
    import sys

    host = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = ("import sys, inspector_widget.capture.refs, inspector_widget.capture.diff; "
            "bad=[m for m in sys.modules if m.startswith(('google.protobuf','PIL'))]; "
            "print(bad); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-c", code], cwd=host, capture_output=True, text=True,
                       env=dict(os.environ, PYTHONPATH=host), check=False)
    assert r.returncode == 0, r.stdout + r.stderr
