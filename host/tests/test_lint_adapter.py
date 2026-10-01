"""Dict-oriented lint adapter (shared contract: mcp_server / correlate).

``a11y_lint.lint_a11y`` is the entry point that correlate.inspect_node and the
MCP server call with COMPOSE-SEMANTICS roots + a DPI int; it must return finding
DICTS (not Finding objects). ``summarize_dicts`` counts those dicts by severity.

We feed a tiny synthetic semantics root containing one actionable-but-unlabeled
button and assert the adapter surfaces the missing-label finding as a dict, then
that summarize_dicts tallies it correctly.
"""

from __future__ import annotations

from inspector_widget import a11y_lint as L

# Density 160 => 1px == 1dp, so a 48x48 px box is a comfortably-sized 48dp target
# (keeps the focus on the missing-label rule, not touch-target).
DENSITY_1TO1 = 160


def _node(*, nid, name, attrs, bounds=(0, 0, 48, 48), children=None):
    x, y, w, h = bounds
    return {
        "id": nid,
        "name": name,
        "kind": "SEMANTICS",
        "attrs": attrs,
        "bounds": {"layout": {"x": x, "y": y, "w": w, "h": h}},
        "children": children or [],
    }


def _semantics_root():
    """A root with one labelled child and one actionable-but-UNLABELED button."""
    labeled = _node(nid=2, name="Text", attrs={"Text": "Welcome"})
    unlabeled_button = _node(
        nid=3, name="Button",
        attrs={"OnClick": "{}", "Role": "Button"},  # actionable, no label
    )
    return _node(nid=1, name="Column", attrs={},
                 bounds=(0, 0, 200, 200),
                 children=[labeled, unlabeled_button])


def test_lint_a11y_returns_nonempty_list_of_dicts():
    findings = L.lint_a11y([_semantics_root()], density=DENSITY_1TO1)
    assert isinstance(findings, list)
    assert findings, "expected at least one finding"
    assert all(isinstance(f, dict) for f in findings)
    # Each dict carries the contract keys.
    for f in findings:
        assert "rule" in f and "severity" in f and "node" in f


def test_lint_a11y_flags_missing_label_on_actionable():
    findings = L.lint_a11y([_semantics_root()], density=DENSITY_1TO1)
    missing = [f for f in findings if f["rule"] == "a11y.label.missing"]
    assert len(missing) == 1
    assert missing[0]["severity"] == "error"
    # It targets the unlabeled button (node id 3), not the labelled text.
    assert missing[0]["node"].get("id") == 3


def test_lint_a11y_default_density_does_not_crash():
    # Default density (420) is the real-device default; ensure it still returns dicts.
    findings = L.lint_a11y([_semantics_root()])
    assert isinstance(findings, list)
    assert all(isinstance(f, dict) for f in findings)


def test_summarize_dicts_counts_match():
    findings = L.lint_a11y([_semantics_root()], density=DENSITY_1TO1)
    summary = L.summarize_dicts(findings)

    # Shape exactly matches the contract.
    assert set(summary) == {"error", "warn", "info", "total"}
    assert summary["total"] == len(findings)
    assert summary["total"] == summary["error"] + summary["warn"] + summary["info"]

    # Cross-check each bucket against an independent count over the dicts.
    for sev in ("error", "warn", "info"):
        assert summary[sev] == sum(1 for f in findings if f["severity"] == sev)

    # The missing-label finding is an error, so at least one error.
    assert summary["error"] >= 1


def test_summarize_dicts_empty():
    assert L.summarize_dicts([]) == {"error": 0, "warn": 0, "info": 0, "total": 0}


def test_summarize_dicts_counts_findings_under_a_dialog_apart():
    # As LintReport.summary does: a finding on a window under an open dialog is not live.
    live = {"severity": "error", "window": {"index": 1, "root_view_id": 9}}
    behind = {"severity": "warn", "window": {"index": 0, "root_view_id": 2, "covered_by": 9}}
    assert L.summarize_dicts([live, behind, behind]) == {
        "error": 1, "warn": 0, "info": 0, "total": 1,
        "covered": {"error": 0, "warn": 2, "info": 0, "total": 2, "windows": [9]}}
