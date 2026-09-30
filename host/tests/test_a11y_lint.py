"""Tests for inspector_widget.a11y_lint (legacy Compose-semantics input).

Feeds synthetic *resolved* semantics-node dicts (the shape from
strings.compose_node_to_dict) to lint_tree -- the legacy input, still accepted via
an adapter -- and checks the emitted Finding rule ids and severities. The unified
a11y-tree input (Views + Compose) is covered by test_a11y_lint_unified.py. The one pixel rule (contrast) is driven by a tiny in-memory RGBA
buffer built so the k-means fg/bg split is deterministic.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from inspector_widget import a11y_lint as L


# --------------------------------------------------------------------------- #
# Node helper (compose semantics dict shape).
# --------------------------------------------------------------------------- #
def node(
    *,
    nid: int = 1,
    name: str = "Node",
    attrs: Optional[Dict[str, str]] = None,
    bounds=(0, 0, 48, 48),
    children: Optional[List[dict]] = None,
    render_node_id: Optional[int] = None,
    **extra,
) -> dict:
    x, y, w, h = bounds
    d = {
        "id": nid,
        "name": name,
        "kind": "SEMANTICS",
        "attrs": attrs or {},
        "bounds": {"layout": {"x": x, "y": y, "w": w, "h": h}},
        "children": children or [],
    }
    if render_node_id is not None:
        d["render_node_id"] = render_node_id
    d.update(extra)
    return d


def rules(findings) -> List[str]:
    return [f.rule for f in findings]


def by_rule(findings, rule_id):
    return [f for f in findings if f.rule == rule_id]


# Density 160 => 1px == 1dp, so bounds in px == bounds in dp (easy assertions).
DENSITY_1TO1 = 160


# --------------------------------------------------------------------------- #
# R1 — missing label on actionable
# --------------------------------------------------------------------------- #
def test_missing_label_on_clickable():
    n = node(attrs={"OnClick": "{}", "Role": "Button"}, bounds=(0, 0, 48, 48))
    findings = L.lint_tree([n], L.LintContext(density=DENSITY_1TO1))
    f = by_rule(findings, "a11y.label.missing")
    assert len(f) == 1
    assert f[0].severity == "error"


def test_labeled_actionable_has_no_missing_label():
    n = node(attrs={"OnClick": "{}", "Role": "Button", "Text": "Submit"},
             bounds=(0, 0, 48, 48))
    findings = L.lint_tree([n], L.LintContext(density=DENSITY_1TO1))
    assert by_rule(findings, "a11y.label.missing") == []


# --------------------------------------------------------------------------- #
# R2 — touch target too small (fake density via 1:1 mapping)
# --------------------------------------------------------------------------- #
def test_touch_target_small_warns_between_32_and_48():
    # 40x40dp < 48dp but >= 32 -> warn.
    n = node(attrs={"OnClick": "{}", "Role": "Button", "Text": "X"}, bounds=(0, 0, 40, 40))
    findings = L.lint_tree([n], L.LintContext(density=DENSITY_1TO1))
    f = by_rule(findings, "a11y.touch_target.small")
    assert len(f) == 1
    assert f[0].severity == "warn"
    assert f[0].evidence["min_dp"] == 48


def test_touch_target_small_errors_below_the_24dp_floor():
    # < 24dp in either dimension is below the WCAG 2.5.8 hard floor -> error.
    n = node(attrs={"OnClick": "{}", "Role": "Button", "Text": "X"}, bounds=(0, 0, 20, 20))
    findings = L.lint_tree([n], L.LintContext(density=DENSITY_1TO1))
    f = by_rule(findings, "a11y.touch_target.small")
    assert len(f) == 1
    assert f[0].severity == "error"


def test_touch_target_at_the_24dp_floor_is_warn():
    n = node(attrs={"OnClick": "{}", "Role": "Button", "Text": "X"}, bounds=(0, 0, 24, 24))
    findings = L.lint_tree([n], L.LintContext(density=DENSITY_1TO1))
    f = by_rule(findings, "a11y.touch_target.small")
    assert [x.severity for x in f] == ["warn"]


def test_touch_target_ok_at_48():
    n = node(attrs={"OnClick": "{}", "Role": "Button", "Text": "X"}, bounds=(0, 0, 48, 48))
    findings = L.lint_tree([n], L.LintContext(density=DENSITY_1TO1))
    assert by_rule(findings, "a11y.touch_target.small") == []


# --------------------------------------------------------------------------- #
# R4 — redundant / duplicate label
# --------------------------------------------------------------------------- #
def test_redundant_label_equals_text_is_info():
    n = node(attrs={"OnClick": "{}", "Role": "Button",
                    "Text": "Save", "ContentDescription": "Save"},
             bounds=(0, 0, 48, 48))
    findings = L.lint_tree([n], L.LintContext(density=DENSITY_1TO1))
    f = by_rule(findings, "a11y.label.redundant")
    assert len(f) == 1
    assert f[0].severity == "info"
    assert f[0].evidence["reason"] == "equals_text"


def test_redundant_label_type_noun_is_warn():
    # Role present + label contains a type noun ("button") -> warn.
    n = node(attrs={"OnClick": "{}", "Role": "Button", "ContentDescription": "Submit button"},
             bounds=(0, 0, 48, 48))
    findings = L.lint_tree([n], L.LintContext(density=DENSITY_1TO1))
    f = by_rule(findings, "a11y.label.redundant")
    assert len(f) == 1
    assert f[0].severity == "warn"
    assert f[0].evidence["reason"] == "type_noun"


# --------------------------------------------------------------------------- #
# R5 — clickable without a role
# --------------------------------------------------------------------------- #
def test_role_missing_on_clickable_with_text_is_info():
    # TalkBack still says "double-tap to activate"; a missing role on a node with
    # visible text is a quality nudge, not a defect.
    n = node(attrs={"OnClick": "{}", "Text": "Tap"}, bounds=(0, 0, 48, 48))
    findings = L.lint_tree([n], L.LintContext(density=DENSITY_1TO1))
    f = by_rule(findings, "a11y.role.missing_on_clickable")
    assert len(f) == 1
    assert f[0].severity == "info"


def test_role_missing_skips_text_fields():
    n = node(attrs={"OnClick": "", "SetText": "", "EditableText": "", "Text": "Email"},
             bounds=(0, 0, 300, 60))
    findings = L.lint_tree([n], L.LintContext(density=DENSITY_1TO1))
    assert by_rule(findings, "a11y.role.missing_on_clickable") == []


def test_role_present_no_role_missing():
    n = node(attrs={"OnClick": "{}", "Role": "Button", "Text": "Tap"}, bounds=(0, 0, 48, 48))
    findings = L.lint_tree([n], L.LintContext(density=DENSITY_1TO1))
    assert by_rule(findings, "a11y.role.missing_on_clickable") == []


# --------------------------------------------------------------------------- #
# R3 — contrast (the one image rule) using a tiny in-memory RGBA buffer.
# --------------------------------------------------------------------------- #
def _solid_block_rgba(w: int, h: int, fg, bg, fg_rows: int):
    """RGBA buffer: top ``fg_rows`` rows are ``fg``, the rest ``bg``."""
    buf = bytearray(w * h * 4)
    for y in range(h):
        color = fg if y < fg_rows else bg
        for x in range(w):
            o = (y * w + x) * 4
            buf[o], buf[o + 1], buf[o + 2], buf[o + 3] = (*color, 255)
    return bytes(buf)


def test_contrast_low_is_error_for_normal_text():
    # Light-grey text on white -> contrast well below 4.5:1 -> error (normal size).
    W = H = 16
    fg = (200, 200, 200)   # light grey
    bg = (255, 255, 255)   # white
    rgba = _solid_block_rgba(W, H, fg, bg, fg_rows=4)  # 25% fg -> above 1.5% floor
    ctx = L.LintContext(
        density=DENSITY_1TO1, screenshot_rgba=rgba,
        screenshot_w=W, screenshot_h=H, screenshot_scale=1.0,
    )
    # 16x16 px so height 16 dp < 24 -> "normal" text class -> requires 4.5:1.
    n = node(attrs={"Text": "Hello"}, bounds=(0, 0, 16, 16))
    findings = L.lint_tree([n], ctx)
    f = by_rule(findings, "a11y.contrast.low")
    assert len(f) == 1
    assert f[0].severity == "error"
    assert f[0].evidence["ratio"] < 4.5
    assert f[0].needs_image is True


def test_contrast_ok_for_black_on_white():
    W = H = 16
    fg = (0, 0, 0)
    bg = (255, 255, 255)
    rgba = _solid_block_rgba(W, H, fg, bg, fg_rows=4)
    ctx = L.LintContext(
        density=DENSITY_1TO1, screenshot_rgba=rgba,
        screenshot_w=W, screenshot_h=H, screenshot_scale=1.0,
    )
    n = node(attrs={"Text": "Hello"}, bounds=(0, 0, 16, 16))
    findings = L.lint_tree([n], ctx)
    assert by_rule(findings, "a11y.contrast.low") == []


def test_contrast_skipped_without_image():
    # No screenshot in ctx => contrast auto-skips even for a Text node.
    n = node(attrs={"Text": "Hello"}, bounds=(0, 0, 16, 16))
    findings = L.lint_tree([n], L.LintContext(density=DENSITY_1TO1))
    assert by_rule(findings, "a11y.contrast.low") == []


# --------------------------------------------------------------------------- #
# R6 — image without description (missing role surfaces here too)
# --------------------------------------------------------------------------- #
def test_image_no_description_warns():
    n = node(name="Image", attrs={"Role": "Image"}, bounds=(0, 0, 48, 48))
    findings = L.lint_tree([n], L.LintContext(density=DENSITY_1TO1))
    f = by_rule(findings, "a11y.image.no_description")
    assert len(f) == 1
    assert f[0].severity == "warn"


def test_clickable_image_no_description_is_r1_error_not_r6():
    # An actionable image with no name is the missing-label rule (R1, error); R6 is
    # for non-actionable images, so the defect is reported exactly once.
    n = node(name="Image", attrs={"Role": "Image", "OnClick": "{}"}, bounds=(0, 0, 48, 48))
    findings = L.lint_tree([n], L.LintContext(density=DENSITY_1TO1))
    assert by_rule(findings, "a11y.image.no_description") == []
    f = by_rule(findings, "a11y.label.missing")
    assert [x.severity for x in f] == ["error"]


# --------------------------------------------------------------------------- #
# Zero-area nodes are skipped (the lint walker guards on w>0 and h>0).
# --------------------------------------------------------------------------- #
def test_zero_area_node_is_skipped():
    # A clickable, label-less, zero-sized node would normally fire several rules,
    # but the walker skips per-node rules entirely for zero-area bounds.
    n = node(attrs={"OnClick": "{}", "Role": "Button"}, bounds=(10, 10, 0, 0))
    findings = L.lint_tree([n], L.LintContext(density=DENSITY_1TO1))
    assert findings == []


def test_zero_area_parent_still_lints_measured_child():
    child = node(nid=2, attrs={"OnClick": "{}", "Role": "Button"}, bounds=(0, 0, 20, 20))
    parent = node(nid=1, attrs={}, bounds=(0, 0, 0, 0), children=[child])
    findings = L.lint_tree([parent], L.LintContext(density=DENSITY_1TO1))
    # Child fires missing-label (error) + small touch target.
    assert "a11y.label.missing" in rules(findings)
    assert "a11y.touch_target.small" in rules(findings)
