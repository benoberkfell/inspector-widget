"""Findings under an open dialog, on every surface that reports the lint.

The recorded Now in Android Settings dialog (``tests/fixtures/captures/nia_settings``) is a
Compose Dialog window over For you: 6 lint findings (R12 on the For you topic rows) sit on the
window the dialog covers. The dialog's own 3 are R9's section titles that are not headings
("Theme", "Use Dynamic Color", "Dark mode preference"). Each surface counts the covered ones
apart from the reachable ones (``summary.covered``), lists them where the CLI text does, and
never counts them as live: the legacy a11y_lint (CLI and MCP, brief and full), inspect_node's
dossier, the overlays' counters, and the capture lint.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout

import capture_harness as ch
import pytest
from test_capture_replay import Replay, run

NAME = "nia_settings"
COVERED = 6
ON_DIALOG = 3  # R9: the dialog's section titles are not headings
DIALOG = 76  # the dialog window's root view id


@pytest.fixture
def nia(tmp_path):
    with Replay(NAME, str(tmp_path)) as r:
        yield r


def _cli(*argv: str) -> tuple[int, str, str]:
    import cli

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


def _mcp(tool: str, r: Replay, **args) -> dict:
    import mcp_server

    text, is_error = mcp_server._call_tool_text(
        tool, {"serial": ch.SERIAL, "package": r.rec.package, "include_contrast": False, **args})
    assert not is_error, text
    return json.loads(text)


def _target(r: Replay) -> list[str]:
    return ["--serial", ch.SERIAL, "--package", r.rec.package, "--no-contrast"]


def test_a11y_lint_lists_every_covered_finding_in_json_as_the_cli_text_does(nia):
    brief = _mcp("a11y_lint", nia)
    assert brief["summary"]["total"] == ON_DIALOG
    assert list(brief["by_rule"]) == ["a11y.heading.structure"]
    assert brief["summary"]["covered"] == {"error": 0, "warn": 0, "info": COVERED,
                                           "total": COVERED, "windows": [DIALOG]}
    dup = brief["covered_by_rule"]["a11y.duplicate.label"]
    assert set(dup) == {"sev", "n", "msg", "nodes", "more"}  # by_rule's shape
    assert (dup["sev"], dup["n"], len(dup["nodes"]), dup["more"]) == ("info", COVERED, 3, 3)
    for args in ({"group_by": "none"}, {"detail": "full", "max_bytes": 0}):
        full = _mcp("a11y_lint", nia, **args)
        assert [(f["rule"], f["evidence"]["reason"], f["window"].get("covered_by"))
                for f in full["findings"]] == [
            ("a11y.heading.structure", "section_title", None)] * ON_DIALOG
        cov = full["covered_findings"]
        assert len(cov) == COVERED and all(f["window"]["covered_by"] == DIALOG for f in cov)
        assert all(f["message"] and f["bounds"] for f in cov)
    keys = sorted(f["node_key"] for f in full["covered_findings"])
    rc, out, _err = _cli("a11y-lint", *_target(nia))
    assert rc == 0
    assert sorted(k for k in keys if k in out) == keys  # the text lists the same nodes
    rc, out, _err = _cli("a11y-lint", *_target(nia), "--json", "-")
    assert rc == 0 and json.loads(out)["covered_by_rule"] == brief["covered_by_rule"]


def test_cli_summaries_and_overlays_count_the_covered_findings(nia, tmp_path):
    rc, _out, err = _cli("a11y", *_target(nia), "--lint")
    assert rc == 0
    assert "a11y lint: 0 error, 0 warn, 3 info (+6 under an open dialog)" in err
    rc, _out, err = _cli("a11y-lint", *_target(nia), "--overlay", str(tmp_path / "ov.png"))
    assert rc == 0 and "3 flagged; 0 error, 0 warn, 3 info (+6 under an open dialog)" in err
    ov = _mcp("a11y_overlay", nia)
    assert (ov["finding_count"], ov["flagged"]) == (ON_DIALOG, ON_DIALOG)
    assert (ov["findings_covered"], ov["covered_windows"]) == (COVERED, 1)
    assert ov["summary"]["covered"]["total"] == COVERED


def test_inspect_node_counts_a_covered_finding_apart(nia):
    import mcp_server

    full = mcp_server._run_tool("a11y_lint", {"serial": ch.SERIAL, "package": nia.rec.package,
                                              "include_contrast": False})
    key = full["covered_findings"][0]["node_key"]
    d = mcp_server._run_tool("inspect_node", {"serial": ch.SERIAL, "package": nia.rec.package,
                                              "node_key": key, "include_image": False})
    assert d["lint"] and all(f["window"]["covered_by"] == DIALOG for f in d["lint"])
    s = d["lint_summary"]
    assert (s["error"], s["warn"], s["info"], s["total"]) == (0, 0, 0, 0)
    assert s["covered"] == {"error": 0, "warn": 0, "info": len(d["lint"]),
                            "total": len(d["lint"]), "windows": [DIALOG]}


# ------------------------------------------------------------------------- the capture lint
def test_the_capture_lint_counts_findings_under_the_dialog_apart(nia):
    cap = nia.capture()
    assert cap["lint"] == (f"{ON_DIALOG} info: {ON_DIALOG} heading; +{COVERED} under an open "
                           f"dialog (contrast not run)")
    ix = nia.index(cap["capture"])
    dialog = ix.get(f"w:{DIALOG}")
    behind = [(n, i) for n in ix.nodes.values() for i in n.issues
              if i.id.startswith("a11y.") and i.evidence.get("covered_by")]
    assert len(behind) == COVERED
    assert all(i.evidence["covered_by"] == dialog.id for _n, i in behind)
    win = ix.nodes[behind[0][0].window]
    assert all(n.window == win.id for n, _i in behind)
    for args in ({}, {"wcag": True}, {"group": "none"}):  # wcag re-lints through the cache
        lint = run(nia.ctx, "lint", **args)
        assert lint["counts"] == {"error": 0, "warn": 0, "info": ON_DIALOG}, args
        assert lint["covered"] == {"n": COVERED, "windows": [win.ref], "by": [dialog.ref],
                                   "listed": False}
        listed = lint.get("rules") or lint.get("lines")
        assert sum(r["n"] for r in listed) == ON_DIALOG if lint.get("rules") \
            else len(listed) == ON_DIALOG
        # the node( hint names a node on the dialog, never one behind it; one hint lints
        # that window instead (at most 3 hints are kept)
        focus = next(h for h in lint["next"] if h.startswith("node("))
        assert ix.get(focus[6:-2]).window == dialog.id
        assert lint["next"][-1] == f'lint(within="{win.ref}")'
    inside = run(nia.ctx, "lint", within=win.ref, group="none")
    assert inside["counts"]["info"] == COVERED and len(inside["lines"]) == COVERED
    assert inside["covered"]["listed"] is True


def test_the_capture_lint_overlay_does_not_draw_the_window_under_the_dialog(nia):
    pytest.importorskip("PIL")
    cap = nia.capture()
    ix = nia.index(cap["capture"])
    dialog = ix.get(f"w:{DIALOG}")
    screen = run(nia.ctx, "image", overlay="lint")
    only_dialog = run(nia.ctx, "image", overlay="lint", window=dialog.ref)
    assert screen["window"] == "screen" and screen["marks"] == only_dialog["marks"]
    win = next(w for w in ix.windows() if w.id != dialog.id)
    behind = run(nia.ctx, "image", overlay="lint", window=win.ref)  # asked for: drawn
    assert behind["marks"] > 0
