"""Golden sizes of the capture-and-walk tools with the REAL renderers (WP S1).

Every call goes through ``ops.run`` over the harness fake adb and agent, serving
the recorded launcher and View-screen replays, the 259-view wide scene and C4's
mixed View/Compose/dialog hierarchy. Sizes are compact JSON bytes:

* spec section 13.2 / WP S1: the per-scene targets;
* spec section 7: every default response is within its tool's default budget;
* spec section 8: the workflow token totals (3.5 B per token plus the inline
  image estimate) within +25% of the spec's figures.

``python tests/test_capture_budgets.py`` prints the measured sizes.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from typing import Any

import capture_harness as ch
import pytest
from capture_harness import nbytes, ok

from inspector_widget import ops
from inspector_widget.capture import images

#: Spec section 7: each tool's default max_bytes.
BUDGET = {"capture": 3000, "captures": 2000, "outline": 6000, "find": 3000, "node": 3000,
          "node_batch": 6000, "image": 600, "lint": 4000, "diff": 4000}
TOKEN_BYTES = 3.5


def run(ctx: ops.OpContext, tool: str, **args: Any) -> dict:
    return ok(ops.run(ctx, tool, args))


def take(ctx: ops.OpContext, **args: Any) -> dict:
    return run(ctx, "capture", serial=ch.SERIAL, package=ch.PACKAGE, **args)


def ref(ctx: ops.OpContext, sel: str) -> str:
    return run(ctx, "node", ref=sel, facets="core")["ref"]


# --------------------------------------------------------------------------- #
# Section 13.2 / WP S1 targets
# --------------------------------------------------------------------------- #
def test_launcher_targets(tmp_path):
    with ch.harness("launcher", str(tmp_path)):
        ctx = ch.ops_context()
        assert nbytes(take(ctx)) <= 2500
        assert nbytes(run(ctx, "outline")) <= 2500
        listing = run(ctx, "outline", root="@launcher_list")
        assert nbytes(listing) <= 2000 and listing["shown"] == listing["total"] == 13
        assert nbytes(run(ctx, "outline", view="slots")) <= 6000
        assert nbytes(run(ctx, "outline", view="reading")) <= 2000
        found = run(ctx, "find", text="state", flags=["click"])
        assert nbytes(found) <= 600 and found["total"] == 2
        assert nbytes(run(ctx, "node", ref="@launch_heading")) <= 1500
        assert nbytes(run(ctx, "lint")) <= 1200
        assert nbytes(run(ctx, "captures")) <= 400
        assert nbytes(run(ctx, "image", ref="@launch_heading")) <= 400
        ctx.sessions.close_all()


def test_viewscreen_targets(tmp_path):
    with ch.harness("viewscreen", str(tmp_path)):
        ctx = ch.ops_context()
        cid = take(ctx)["capture"]
        out = run(ctx, "outline")
        assert nbytes(out) <= 3000 and "truncated" not in out
        ix = ctx.store.load(cid).index()
        views = {n.id for n in ix.nodes.values() if n.kind == "view"}
        shown = set(re.findall(r"\bn\d+\b", " ".join(out["lines"]))) & views
        assert len(views) == 40 and len(shown) + out["hidden"]["zero_size"] == 40
        node = run(ctx, "node", ref="#badSwitch", props="nondefault")
        assert nbytes(node) <= 1200 and node["props"]["values"]["checked"] is True
        # the unified lint covers View screens (the C7 adapter runs a11y_lint.run_lint)
        assert sum(run(ctx, "lint")["counts"].values()) == 14
        ctx.sessions.close_all()


def test_wide_targets(tmp_path):
    with ch.harness("wide", str(tmp_path)):
        ctx = ch.ops_context()
        cid = take(ctx)["capture"]
        doc = ops.run(ctx, "outline", {})
        seen: list[str] = []
        while True:
            assert nbytes(doc) <= 6000
            seen.extend(line.split()[0] for line in doc["lines"])
            cursor = (doc.get("truncated") or {}).get("cursor")
            if not cursor:
                break
            doc = run(ctx, "outline", cursor=cursor)
        ix = ctx.store.load(cid).index()
        views = [n.id for n, _d in ix.walk("ui") if n.kind == "view"]
        assert seen == views and len(views) == 259
        found = run(ctx, "find", text="Label 4", limit=20)
        assert nbytes(found) <= 3000 and found["total"] == 11
        assert nbytes(run(ctx, "node", ref="#view_47", props="nondefault")) <= 1500
        ctx.sessions.close_all()


def test_wide_capture_is_within_3000(tmp_path):
    with ch.harness("wide", str(tmp_path)):
        ctx = ch.ops_context()
        assert nbytes(take(ctx)) <= 3000
        ctx.sessions.close_all()


# --------------------------------------------------------------------------- #
# Section 7: every tool x every scene, default arguments
# --------------------------------------------------------------------------- #
def _calls(ctx: ops.OpContext, cid: str) -> list[tuple[str, str, dict]]:
    ix = ctx.store.load(cid).index()
    ui = [n for n, _d in ix.walk("ui")]
    issue = next((n.id for n in ui if n.issues and n.b and n.b[2] > 0 and n.b[3] > 0), None)
    some = issue or next(n.id for n in ui if n.b and n.b[2] > 0 and n.b[3] > 0)
    batch = [n.id for n in ui][:10]
    calls = [("outline", "outline", {}), ("outline", "outline", {"view": "views"}),
             ("outline", "outline", {"view": "compose"}), ("outline", "outline", {"view": "a11y"}),
             ("outline", "outline", {"view": "reading"}),
             ("outline", "outline", {"detail": "all", "depth": 99}),
             ("find", "find", {"flags": ["click"]}), ("find", "find", {"in": "all"}),
             ("node", "node", {"ref": some}),
             ("node", "node", {"ref": some, "facets": "all", "props": "all", "params": "raw"}),
             ("node_batch", "node", {"refs": batch, "facets": "all"}),
             ("image", "image", {"ref": some}), ("image", "image", {"overlay": "marks"}),
             ("image", "image", {"overlay": "lint"}),
             ("lint", "lint", {}), ("lint", "lint", {"group": "none"}),
             ("lint", "lint", {"rules": ["render."], "group": "node"}),
             ("diff", "diff", {}), ("captures", "captures", {}),
             ("captures", "captures", {"action": "show"})]
    if ctx.store.load(cid).meta.facet_status("slots") == "ok":
        calls.append(("outline", "outline", {"view": "slots", "origin": "all", "depth": 99}))
    return calls


@pytest.mark.parametrize("scene", ch.SCENES)
def test_every_default_response_is_within_budget(tmp_path, scene):
    pytest.importorskip("PIL")  # the overlays
    with ch.harness(scene, str(tmp_path)):
        ctx = ch.ops_context()
        first = take(ctx)
        assert nbytes(first) <= BUDGET["capture"], nbytes(first)
        again = take(ctx, diff_from="prev")
        assert nbytes(again) <= BUDGET["capture"], nbytes(again)
        for key, tool, args in _calls(ctx, again["capture"]):
            doc = run(ctx, tool, **args)
            assert nbytes(doc) <= BUDGET[key], (scene, tool, args, nbytes(doc))
            nxt = doc.get("next") or []
            assert len(nxt) <= 3 and nbytes(nxt) <= 200, (tool, args, nxt)
        ctx.sessions.close_all()


# --------------------------------------------------------------------------- #
# Section 8: workflow token totals
# --------------------------------------------------------------------------- #
def tokens(*docs: dict, image_tokens: int = 0) -> int:
    return round(sum(nbytes(d) for d in docs) / TOKEN_BYTES) + image_tokens


def _inline(doc: dict, max_side: int = 1024) -> int:
    return images.inline(doc["path"], max_side)[2]


def workflows(tmp: str) -> dict[str, int]:
    got: dict[str, int] = {}
    with ch.harness("launcher", os.path.join(tmp, "l")):
        ctx = ch.ops_context()
        # W1 "Why is the Section heading row cut off?": capture, node, inline crop
        cap = take(ctx)
        node = run(ctx, "node", ref="@launch_heading")
        crop = run(ctx, "image", ref="@launch_heading", pad=48)
        got["W1"] = tokens(cap, node, crop, image_tokens=_inline(crop))
        # W2 "Is the ... button accessible?": capture, find, node(core,a11y,issues)
        hit = run(ctx, "find", text="checkbox", flags=["click"])
        n = run(ctx, "node", ref=hit["lines"][0].split()[0], facets="core,a11y,issues")
        got["W2"] = tokens(cap, hit, n)
        # W4 audit: capture(lint=full), lint, reading, lint overlay (inline), node x2
        if images._pil() is not None:
            full = take(ctx, lint="full")
            ov = run(ctx, "image", overlay="lint")
            ix = ctx.store.load(full["capture"]).index()
            two = [x.id for x, _d in ix.walk("ui") if x.issues][:2]
            got["W4"] = tokens(full, run(ctx, "lint"), run(ctx, "outline", view="reading"), ov,
                               *[run(ctx, "node", ref=r) for r in two],
                               image_tokens=_inline(ov))
        ctx.sessions.close_all()
    with ch.harness("viewscreen", os.path.join(tmp, "v")) as (_dev, scene):
        ctx = ch.ops_context()
        # W3 "What changed after I tapped X?": capture(label), node, capture(diff_from)
        before = take(ctx, label="before")
        n3 = run(ctx, "node", ref="#badSwitch")
        ch.tap_switch(scene)
        after = take(ctx, diff_from="before", settle_ms=800)
        got["W3"] = tokens(before, n3, after)
        ctx.sessions.close_all()
    with ch.harness("wide", os.path.join(tmp, "w")):
        ctx = ch.ops_context()
        # W6 large list: capture, find, node(props=nondefault), outline(root, depth=1)
        cap = take(ctx)
        got["W6"] = tokens(cap, run(ctx, "find", text="Label 4", limit=20),
                           run(ctx, "node", ref="#view_47", props="nondefault"),
                           run(ctx, "outline", root="#view_3", depth=1))
        ctx.sessions.close_all()
    with ch.harness("mixed", os.path.join(tmp, "m")):
        ctx = ch.ops_context()
        # W7 mixed hierarchy: capture, find within the feed, lint within it, node
        cap = take(ctx)
        hits = run(ctx, "find", text="Delete", flags=["click"], within="#feed")
        got["W7"] = tokens(cap, hits, run(ctx, "lint", within="#feed"),
                           run(ctx, "node", ref=hits["lines"][2].split()[0]))
        ctx.sessions.close_all()
    return got


#: Spec section 8's totals (tokens).
SPEC_TOKENS = {"W1": 1100, "W2": 1100, "W3": 1200, "W4": 2800, "W6": 2400, "W7": 1600}


def test_workflow_token_totals(tmp_path):
    got = workflows(str(tmp_path))
    over = {k: (got[k], SPEC_TOKENS[k]) for k in got if got[k] > SPEC_TOKENS[k] * 1.25}
    assert not over, over
    # W3 (tap, then capture with a diff) was 1,462 tokens with the stand-in
    # renderer; the real one trims the View-screen preview to fit the spec itself.
    assert got["W3"] <= SPEC_TOKENS["W3"], got


# --------------------------------------------------------------------------- #
# Measurement report (not a test)
# --------------------------------------------------------------------------- #
def measure(tmp: str) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for scene in ch.SCENES:
        with ch.harness(scene, os.path.join(tmp, scene)):
            ctx = ch.ops_context()
            cap = take(ctx)
            sizes = {"capture": nbytes(cap)}
            sizes["capture_diff"] = nbytes(take(ctx, diff_from="prev"))
            for key, tool, args in [
                    ("outline", "outline", {}), ("outline_reading", "outline", {"view": "reading"}),
                    ("find_click", "find", {"flags": ["click"]}), ("lint", "lint", {}),
                    ("captures_list", "captures", {}), ("diff", "diff", {})]:
                sizes[key] = nbytes(run(ctx, tool, **args))
            ix = ctx.store.load(cap["capture"]).index()
            some = next((n.id for n, _d in ix.walk("ui") if n.issues and n.b and n.b[2] > 0),
                        None) or "n1"
            sizes["node"] = nbytes(run(ctx, "node", ref=some))
            sizes["image"] = nbytes(run(ctx, "image", ref=some))
            if scene == "launcher":
                sizes["outline_root_list"] = nbytes(run(ctx, "outline", root="@launcher_list"))
                sizes["outline_slots"] = nbytes(run(ctx, "outline", view="slots"))
                sizes["find_state"] = nbytes(run(ctx, "find", text="state", flags=["click"]))
                sizes["node_heading"] = nbytes(run(ctx, "node", ref="@launch_heading"))
            if scene == "viewscreen":
                sizes["node_badSwitch_nondefault"] = nbytes(
                    run(ctx, "node", ref="#badSwitch", props="nondefault"))
            if scene == "wide":
                sizes["find_label4"] = nbytes(run(ctx, "find", text="Label 4", limit=20))
                sizes["node_view47_nondefault"] = nbytes(
                    run(ctx, "node", ref="#view_47", props="nondefault"))
            out[scene] = sizes
            ctx.sessions.close_all()
    return out


if __name__ == "__main__":  # PYTHONPATH=. python tests/test_capture_budgets.py
    import json

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    with tempfile.TemporaryDirectory(prefix="iw-budgets-") as _tmp:
        for _scene, _sizes in measure(_tmp).items():
            print(_scene, json.dumps(_sizes))
        print("workflows (tokens)", json.dumps(workflows(os.path.join(_tmp, "wf"))))
