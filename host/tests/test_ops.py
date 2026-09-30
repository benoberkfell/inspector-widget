"""The ops layer (WP S1): the capture pipeline and the tool functions, over the
harness fake adb and agent (``tests/capture_harness.py``), with a provider that
attaches through ``inspector_widget.attach`` as the CLI does.

Sizes are checked in ``test_capture_budgets.py``; this module checks behaviour:
the capture order and publish, carry-over across captures, diff after a UI
change, ``if_changed_since``, session and capture resolution, staleness
markers, error codes, ``next`` hints, and that the CLI and the MCP get the same
bytes from the same store.
"""

from __future__ import annotations

import json
import os
import re

import capture_harness as ch
import pytest
from capture_harness import PACKAGE, SERIAL, nbytes, ok

from inspector_widget import ops
from inspector_widget.capture import lines, query
from inspector_widget.capture.model import OpError
from inspector_widget.capture.store import CaptureStore
from inspector_widget.output import dumps


def run(ctx, tool, **args):
    return ops.run(ctx, tool, args)


def capture(ctx, **args):
    args.setdefault("serial", SERIAL)
    args.setdefault("package", PACKAGE)
    return ok(run(ctx, "capture", **args))


# --------------------------------------------------------------------------- #
# The capture pipeline
# --------------------------------------------------------------------------- #
def test_capture_publishes_and_summarizes(tmp_path):
    with ch.harness("launcher", str(tmp_path)) as (dev, _scene):
        ctx = ch.ops_context()
        doc = capture(ctx)
        cid = doc["capture"]
        lc = ctx.store.load(cid)
        assert lc.meta.lineage == (SERIAL, PACKAGE) and lc.meta.pid == ch.PID
        assert doc["session"] == f"{SERIAL}/{PACKAGE}" and doc["pid"] == ch.PID
        assert doc["facets"]["views"] == 9 and doc["facets"]["slots"] == 381
        assert doc["consistency"] == "settled"
        assert doc["windows"] == ["n1 DecorView [0,0 1280x2856] z0"]
        assert doc["lint"].endswith("(contrast not run)")
        heading = query.resolve_selector(lc.index(), "@launch_heading").id
        assert doc["issues"] == f"1 clipped: {heading}"
        # the preview is outline lines (plus a count of what it cut), no duplicates in on_screen
        body = [ln for ln in doc["outline"] if not ln.startswith("…")]
        assert body and all(lines.is_line(ln) for ln in body)
        shown = set(re.findall(r"\bn\d+\b", " ".join(body)))
        assert not shown & {e.split()[0] for e in doc["on_screen"] if not e.startswith("…")}
        assert doc["next"] == ["outline()", "lint()", f'node("{heading}")']
        # it is the default session now, and the ref space starts at n1
        assert ctx.store.default_session() == (SERIAL, PACKAGE)
        assert "n1" in lc.index().nodes
        # capture order: the device saw the facet requests once, no SHUTDOWN
        assert "shutdown" not in dev.commands()
        ctx.sessions.close_all()


def test_refs_are_unchanged_across_captures_of_an_unchanged_scene(tmp_path):
    with ch.harness("viewscreen", str(tmp_path)):
        ctx = ch.ops_context()
        a = ctx.store.load(capture(ctx)["capture"]).index()
        b_doc = capture(ctx, diff_from="prev")
        b = ctx.store.load(b_doc["capture"]).index()
        assert set(a.nodes) == set(b.nodes)
        assert {n.match for n in b.nodes.values()} == {"id"}
        assert b_doc["diff"]["summary"]["changed"] == 0 and b_doc["diff"]["lines"] == []
        ctx.sessions.close_all()


def test_a_ui_change_is_exactly_what_diff_reports(tmp_path):
    with ch.harness("viewscreen", str(tmp_path)) as (_dev, scene):
        ctx = ch.ops_context()
        before = capture(ctx, label="before")
        assert before["label"] == "before"
        switch = ok(run(ctx, "node", ref="#badSwitch"))
        assert switch["tap_xy"] == [242, 1254]
        ch.tap_switch(scene)  # what tapping the switch changes on the device
        after = capture(ctx, diff_from="before")
        ref = switch["ref"]
        assert after["diff"]["a"] == f"{before['capture']} @before"
        assert after["diff"]["summary"]["changed"] == 1
        assert after["diff"]["lines"] == [
            f'~ {ref} Switch #badSwitch "Notifications": checked -> unchecked',
            f'~ {ref} state "ON" -> "OFF"', f"~ {ref} props checked true -> false"]
        # the same comparison through diff(): by label, and with the defaults
        d = ok(run(ctx, "diff", a="before"))
        assert d["b"] == after["capture"] and d["lines"] == after["diff"]["lines"]
        assert ok(run(ctx, "diff"))["lines"] == d["lines"]  # a=prev: b's predecessor
        ctx.sessions.close_all()


def test_if_changed_since_answers_without_writing_a_capture(tmp_path):
    with ch.harness("launcher", str(tmp_path)) as (_dev, scene):
        ctx = ch.ops_context()
        first = capture(ctx, label="base")
        before = ctx.store.summary()
        same = capture(ctx, if_changed_since="base")
        assert same == {"capture": first["capture"], "unchanged": True, "age_s": same["age_s"]}
        assert nbytes(same) <= 60
        assert ctx.store.summary() == before  # nothing written
        # a changed UI captures anew
        node = scene.views.roots[0]
        node.bounds.layout.w -= 10
        again = capture(ctx, if_changed_since="base")
        assert "unchanged" not in again and again["capture"] != first["capture"]
        ctx.sessions.close_all()


def test_slots_enable_warns_and_bumps_the_compose_generation(tmp_path):
    with ch.harness(ch.fakescenes.replay_scene("launcher", slots_populated=False),
                    str(tmp_path)) as (dev, _scene):
        ctx = ch.ops_context()
        plain = capture(ctx)
        assert plain["facets"]["slots"].startswith("not populated")
        enabled = capture(ctx, slots="enable")
        assert enabled["warning"].startswith("slots=enable hot-reloaded")
        meta = ctx.store.load(enabled["capture"]).meta
        assert meta.compose_generation == 1 and isinstance(enabled["facets"]["slots"], int)
        # the destructive call is never a hint
        assert not any(h.startswith("capture(") for h in enabled.get("next", []))
        # tagged nodes keep their refs through the hot reload (locator/structure)
        a = ctx.store.load(plain["capture"]).index()
        b = ctx.store.load(enabled["capture"]).index()
        tagged = [n for n in a.nodes.values() if n.tag]
        assert tagged and all(n.id in b.nodes for n in tagged)
        ctx.sessions.close_all()


def test_a_hot_reload_outside_capture_disables_id_carry(tmp_path):
    with ch.harness("launcher", str(tmp_path)):
        ctx = ch.ops_context()
        capture(ctx)
        ctx.bump_generation(SERIAL, PACKAGE, ch.PID)  # the legacy dump_compose(enable_inspection)
        b = ctx.store.load(capture(ctx)["capture"])
        assert b.meta.compose_generation == 1
        assert "id" not in {n.match for n in b.index().nodes.values() if n.kind == "compose"}
        ctx.sessions.close_all()


# --------------------------------------------------------------------------- #
# Resolution: sessions, captures, cursors
# --------------------------------------------------------------------------- #
def test_session_defaults_to_the_last_capture_then_the_only_running_app(tmp_path):
    with ch.harness("launcher", str(tmp_path)):
        ctx = ch.ops_context()
        # no session yet: the only running debuggable app on the only device
        doc = ok(run(ctx, "capture"))
        assert doc["session"] == f"{SERIAL}/{PACKAGE}"
        # queries resolve through the default session, with no device I/O
        assert ok(run(ctx, "outline"))["capture"] == doc["capture"]
        assert ok(run(ctx, "find", text="state", flags=["click"], package=PACKAGE))["total"] == 2
        ctx.sessions.close_all()


def test_no_session_names_the_candidates(tmp_path):
    with ch.harness("launcher", str(tmp_path)) as (dev, _scene):
        dev.add_app("com.example.other", 777)  # two debuggable apps running now
        ctx = ch.ops_context()
        err = run(ctx, "capture")["error"]
        assert err["code"] == "no_session"
        assert set(err["candidates"]) == {PACKAGE, "com.example.other"}


def test_query_errors_are_envelopes(tmp_path):
    with ch.harness("launcher", str(tmp_path)):
        ctx = ch.ops_context()
        assert run(ctx, "outline")["error"]["code"] == "capture_not_found"
        cid = capture(ctx)["capture"]
        assert run(ctx, "node", ref="n99999")["error"]["code"] == "ref_not_in_capture"
        assert run(ctx, "node", ref="#nope")["error"]["code"] == "not_found"
        bad = run(ctx, "node", ref="#a  #b")["error"]
        assert bad["code"] == "bad_selector" and bad["message"].startswith("column ")
        assert run(ctx, "outline", capture="czzzzz")["error"]["code"] == "capture_not_found"
        assert run(ctx, "lint", rules=["bogus"])["error"]["code"] == "bad_args"
        assert run(ctx, "captures", action="label", id=cid, label="Bad Label")["error"][
            "code"] == "bad_args"
        env = run(ctx, "outline", view="nope")
        assert set(env) == {"error"} and set(env["error"]) >= {"code", "message", "hint"}
        ctx.sessions.close_all()


def test_a_cursor_resolves_its_own_capture(tmp_path):
    with ch.harness("wide", str(tmp_path)):
        ctx = ch.ops_context()
        first = capture(ctx)["capture"]
        page = ok(run(ctx, "outline"))
        cursor = page["truncated"]["cursor"]
        capture(ctx)  # latest moves on; the cursor still names the first capture
        nxt = ok(run(ctx, "outline", cursor=cursor))
        assert nxt["capture"] == first and "stale" in nxt
        ctx.sessions.close_all()


def test_staleness_markers(tmp_path):
    with ch.harness("launcher", str(tmp_path)):
        ctx = ch.ops_context()
        clock = [1_800_000_000.0]
        ctx.store.clock = lambda: clock[0]
        old = capture(ctx)["capture"]
        clock[0] += 40
        new = capture(ctx)["capture"]
        clock[0] += 200
        doc = ok(run(ctx, "outline", capture=old))
        assert doc["capture"] == old and doc["stale"] == f"{new} is newer (40s)"
        assert doc["age_s"] == 240
        assert "stale" not in ok(run(ctx, "outline"))
        ctx.sessions.close_all()


def test_pid_changed_after_an_app_restart(tmp_path):
    with ch.harness("launcher", str(tmp_path)) as (dev, _scene):
        ctx = ch.ops_context()
        old = capture(ctx)["capture"]
        ctx.sessions.close_all()
        dev.restart_app(PACKAGE, new_pid=5555)
        ctx = ch.ops_context(store=ctx.store)
        new = capture(ctx)
        assert new["pid"] == 5555
        doc = ok(run(ctx, "node", ref="@launch_heading", capture=old))
        assert doc["pid_changed"] is True and doc["stale"].startswith(new["capture"])
        # refs carried by locator and structure across the restart
        a, b = (ctx.store.load(c).index() for c in (old, new["capture"]))
        assert query.resolve_selector(a, "@launch_heading").id == \
            query.resolve_selector(b, "@launch_heading").id
        ctx.sessions.close_all()


# --------------------------------------------------------------------------- #
# captures
# --------------------------------------------------------------------------- #
def test_captures_manage_the_store(tmp_path):
    with ch.harness("viewscreen", str(tmp_path)):
        ctx = ch.ops_context()
        a = capture(ctx, label="before")["capture"]
        b = capture(ctx)["capture"]
        listing = ok(run(ctx, "captures"))
        assert [ln.split()[0] for ln in listing["lines"]] == [b, a]
        assert "@before" in listing["lines"][1] and listing["store"].endswith("ttl 24h")
        show = ok(run(ctx, "captures", action="show", id="before"))
        assert show["capture"] == a and show["label"] == "before" and show["nodes"] == 40
        assert ok(run(ctx, "captures", action="pin", id=a)) == {"capture": a, "pinned": True}
        assert "pinned" in ok(run(ctx, "captures"))["lines"][1]
        moved = ok(run(ctx, "captures", action="label", id=b, label="before"))
        assert moved == {"capture": b, "label": "before", "moved_from": a}
        exp = ok(run(ctx, "captures", action="export", id=b, what="nodes"))
        assert exp["rows"] == 40 and os.path.isfile(exp["path"])
        with open(exp["path"], encoding="utf-8") as f:
            assert len(f.read().splitlines()) == 41  # header + 40 nodes
        legacy = ok(run(ctx, "captures", action="export", id=b, what="a11y", format="legacy"))
        with open(legacy["path"], encoding="utf-8") as f:
            assert json.load(f)["windows"]
        assert ok(run(ctx, "captures", action="drop", id=a)) == {"dropped": a}
        assert run(ctx, "outline", capture=a)["error"]["code"] == "capture_not_found"
        assert ok(run(ctx, "captures", action="gc"))["removed"] == 0
        ctx.sessions.close_all()


# --------------------------------------------------------------------------- #
# Both callers, one store: the same bytes
# --------------------------------------------------------------------------- #
QUERIES = [
    ("outline", {}), ("outline", {"view": "reading"}), ("outline", {"root": "#badSwitch"}),
    ("find", {"flags": ["click"]}), ("find", {"text": "Notifications", "count_only": True}),
    ("node", {"ref": "#badSwitch", "props": "nondefault"}), ("node", {"refs": ["n1", "n2"]}),
    ("lint", {}), ("lint", {"group": "node"}), ("image", {"ref": "#badSwitch"}),
    ("diff", {"a": "before"}), ("captures", {}), ("captures", {"action": "show"}),
]


def test_cli_and_mcp_callers_get_identical_envelopes(tmp_path):
    with ch.harness("viewscreen", str(tmp_path)):
        cli_ctx = ch.ops_context("cli")
        capture(cli_ctx, label="before")
        capture(cli_ctx)
        mcp_ctx = ops.OpContext(CaptureStore(), None, "mcp")  # another process's store object
        for tool, args in QUERIES:
            a, b = run(cli_ctx, tool, **args), run(mcp_ctx, tool, **args)
            assert not ops.is_error(a), (tool, args, a)
            assert ch.same_moment(dumps(a)) == ch.same_moment(dumps(b)), (tool, args)
        cli_ctx.sessions.close_all()


def test_every_next_hint_is_small_and_runs(tmp_path):
    from capture_hints import parse_call

    with ch.harness("launcher", str(tmp_path)):
        ctx = ch.ops_context()
        docs = [capture(ctx)]
        for tool, args in [("outline", {}), ("outline", {"max_lines": 3}), ("find",
                           {"flags": ["click"], "limit": 2}), ("node", {"ref": "@launch_heading"}),
                           ("lint", {}), ("lint", {"limit": 1}), ("image", {"overlay": "lint"})]:
            docs.append(ok(run(ctx, tool, **args)))
        hints = []
        for d in docs:
            nxt = d.get("next") or []
            assert len(nxt) <= 3 and len(dumps(nxt).encode()) <= 200, nxt
            hints.extend(nxt)
        assert hints
        for h in dict.fromkeys(hints):
            tool, pos, kw = parse_call(h)
            assert tool != "capture", h
            if pos:
                kw["ref"] = pos[0]
            if "in_" in kw:
                kw["in"] = kw.pop("in_")
            ok(run(ctx, tool, **kw))
        ctx.sessions.close_all()


def test_memory_only_store_says_the_cli_cannot_see_it(tmp_path, monkeypatch):
    with ch.harness("launcher", str(tmp_path)):
        store = CaptureStore(persist=False)
        try:
            ctx = ch.ops_context(store=store)
            doc = capture(ctx)
            assert doc["store"].startswith("memory-only")
            assert not os.path.exists(os.path.join(str(tmp_path), "store", "captures",
                                                   doc["capture"]))
            ctx.sessions.close_all()
        finally:
            store.close()


def test_errors_map_to_codes():
    from inspector_widget import adb
    from inspector_widget.client import AgentTimeoutError, ClientError, SessionLostError
    from inspector_widget.inject import InjectionError

    cases = [(OpError("not_found", "x"), "not_found"),
             (adb.DeviceError("no devices"), "no_session"),
             (SessionLostError("gone"), "device_lost"),
             (AgentTimeoutError("slow"), "agent_error"),
             (ClientError("agent said no"), "agent_error"),
             (InjectionError("could not connect to agent socket 'x'"), "agent_error"),
             (InjectionError("package 'p' is not running on s.", hint="start it"), "no_session"),
             (InjectionError("package 'p' is not debuggable, so ..."), "no_session"),
             (KeyError("oops"), ops.INTERNAL)]
    for exc, code in cases:
        env = ops.error_envelope(exc)
        assert env["error"]["code"] == code, exc
        assert set(env["error"]) >= {"code", "message", "hint"}
    with pytest.raises(OpError):
        ops.call(ops.OpContext(CaptureStore(), None), "nope")


def test_a_bad_diff_from_fails_before_touching_the_device(tmp_path):
    with ch.harness("launcher", str(tmp_path)) as (dev, _scene):
        ctx = ch.ops_context()
        err = run(ctx, "capture", serial=SERIAL, package=PACKAGE, diff_from="nolabel")["error"]
        assert err["code"] == "capture_not_found"
        assert dev.commands() == [] and ctx.store.summary()["captures"] == 0
        first = capture(ctx)["capture"]
        err = run(ctx, "capture", diff_from="latest")["error"]
        assert err["code"] == "bad_args" and 'diff_from="prev"' in err["hint"]
        # prev is the capture before the new one; latest~1 too
        assert capture(ctx, diff_from="prev")["diff"]["a"] == first
        assert capture(ctx, diff_from="latest~2")["diff"]["a"] == first
        # the first poll with if_changed_since has nothing to compare with: it captures
        ctx2 = ch.ops_context(store=CaptureStore(root=str(tmp_path / "other")))
        assert "unchanged" not in capture(ctx2, if_changed_since="latest")
        ctx.sessions.close_all()
        ctx2.sessions.close_all()


def test_a_small_budget_cuts_the_summary_explicitly(tmp_path):
    with ch.harness("wide", str(tmp_path)):
        ctx = ch.ops_context()
        for budget in (700, 1000, 1500):
            doc = capture(ctx, max_bytes=budget)
            assert nbytes(doc) <= budget, (budget, nbytes(doc))
            for key in ("outline", "on_screen"):
                if doc.get(key):
                    assert doc[key][-1].startswith("…"), (budget, key, doc[key])
        ctx.sessions.close_all()


def test_export_format_picks_the_form(tmp_path):
    """format="raw" copies the protobufs and format="legacy" regenerates the old
    dump JSON whatever ``what`` says (live: export --format raw wrote nodes.jsonl)."""
    with ch.harness("viewscreen", str(tmp_path)):
        ctx = ch.ops_context()
        cid = capture(ctx)["capture"]
        raw = ok(run(ctx, "captures", action="export", id=cid, format="raw"))
        assert raw["path"].endswith("out") and raw["files"] >= 3
        assert os.path.isfile(os.path.join(raw["path"], "raw", "views.pb"))
        assert os.path.isfile(os.path.join(raw["path"], "meta.json"))
        assert "protobuf" in raw["hint"]
        one = ok(run(ctx, "captures", action="export", id=cid, what="a11y", format="raw"))
        assert one["path"].endswith(os.path.join("raw", "a11y.pb")) and "files" not in one
        legacy = ok(run(ctx, "captures", action="export", id=cid, format="legacy"))
        assert legacy["files"] >= 2 and os.path.isfile(os.path.join(legacy["path"], "a11y.json"))
        with open(os.path.join(legacy["path"], "views.json"), encoding="utf-8") as f:
            assert json.load(f)["roots"]
        bad = run(ctx, "captures", action="export", id=cid, what="lint", format="raw")
        assert bad["error"]["code"] == "bad_args"
        bad = run(ctx, "captures", action="export", id=cid, what="props", format="legacy")
        assert bad["error"]["code"] == "bad_args"
        ctx.sessions.close_all()
