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


def test_an_unnamed_scrim_is_not_asked_for_a_content_description():
    # A11yProbe V5 BAD: a clickable #99000000 View over 90% of the window behind a hand-made
    # dialog. R1 told the agent to set android:contentDescription on it, which keeps the
    # scrim a stop and the escape; it now says what a scrim needs.
    import tb_capture_fixtures as F

    from inspector_widget import a11y

    _rec, resp = F.load_walk("tb_v5-bad-walk")
    rep = L.lint_unified(a11y.a11y_to_dict(resp), L.LintContext(density=390))
    f = next(x for x in rep.findings if x.rule == "a11y.label.missing" and x.node_key == "view:12")
    assert f.severity == "error" and f.evidence["covers_window_pct"] == 90
    assert "scrim" in f.message and "importantForAccessibility=no" in f.message
    assert "android:contentDescription" not in f.message
    # a plain unlabelled button keeps the label fix
    _rec, resp = F.load_walk("tb_v9-bad-walk")
    rep = L.lint_unified(a11y.a11y_to_dict(resp), L.LintContext(density=390))
    f = next(x for x in rep.findings if x.rule == "a11y.label.missing")
    assert "covers_window_pct" not in f.evidence and "contentDescription" in f.message


def test_asking_the_a11y_lint_for_tb_rules_says_where_they_are():
    import pytest as _pytest

    with _pytest.raises(L.UnknownRuleError) as err:
        L.resolve_rule_ids(["tb"])
    assert "tb_walk" in str(err.value) and 'lint(rules=["tb"])' in str(err.value)
    with _pytest.raises(L.UnknownRuleError) as err:
        L.resolve_rule_ids(["R99"])
    assert "tb_walk" not in str(err.value)


# --------------------------------------------------------------------------- #
# R19..R23 and R9's section titles -- the real-app hunt's static defects, on the unified
# a11y tree (tests/test_realapp_hunt_lint.py runs them on the recorded real screens).
# --------------------------------------------------------------------------- #
from test_a11y_lint_unified import CLICK, comp, decor, of, screen, view  # noqa: E402
from test_a11y_lint_unified import lint as ulint  # noqa: E402

RV = "androidx.recyclerview.widget.RecyclerView"


def _list(rows, *, hv=20, row_count=None, b=(0, 200, 1080, 2000)):
    info = {"row_count": row_count if row_count is not None else len(rows), "column_count": 1}
    return view(hv, RV, flags=("scrollable",), b=b, kids=rows, collection_info=info)


def _row(i, *kids, h=150, y0=200, **kw):
    return view(100 + i, "android.widget.FrameLayout", flags=CLICK + ("long_clickable",),
                b=(0, y0 + h * i, 1080, h), kids=kids,
                collection_item_info={"row_index": i, "column_index": 0}, **kw)


def _icon(hv, cd=None, *, b=(24, 0, 120, 120)):
    return view(hv, "android.widget.ImageView", cd=cd, b=b)


def _text(hv, text, *, b=(150, 0, 800, 100)):
    return view(hv, "android.widget.TextView", text=text, b=b)


def test_r19_placeholder_tokens_are_flagged_once_per_token_with_their_rows():
    rows = [_row(0, _text(300, "Photo. [attachment_icon] sent")),
            _row(1, _text(301, "[attachment_icon] Report.pdf")),
            _row(2, _text(302, "%s unread"), _icon(400, "ic_star_border", b=(900, 500, 72, 72))),
            # not placeholders: a bracketed word, an e-mail, a user name, a percentage
            _row(3, _text(303, "[Draft] Re: first_last@example.com, john_doe, 50% sold"))]
    f = of(ulint(screen(decor(1, _list(rows)))), "a11y.label.placeholder_token")
    got = {x.evidence["token"]: (x.severity, x.evidence["rows"], x.node_key) for x in f}
    assert got == {"[attachment_icon]": ("warn", 2, "view:100"),
                   "%s": ("warn", 1, "view:102"),
                   "ic_star_border": ("warn", 1, "view:102")}
    assert f[0].alias == "R19" and "TalkBack reads" in f[0].message


def test_r20_a_childs_description_read_first_in_most_rows():
    # Thunderbird's settings (TB-11): a decorative icon labelled "Account settings" starts
    # every row; the section titles in between are plain text rows.
    rows = [_row(0, _icon(200 + i, "Account settings"), _text(300 + i, f"Item {i}"))
            for i in range(4)]
    title = view(150, "android.widget.TextView", text="Backup", flags=CLICK, b=(0, 900, 1080, 80),
                 collection_item_info={"row_index": 4, "column_index": 0})
    rep = ulint(screen(decor(1, _list(rows + [title]))))
    f = of(rep, "a11y.label.shared_prefix")
    assert [(x.severity, x.node_key) for x in f] == [("warn", "view:100")]
    ev = f[0].evidence
    assert (ev["prefix"], ev["child"], ev["child_class"], ev["rows"], ev["of"]) == (
        "Account settings", "view:200", "ImageView", 4, 5)
    assert f[0].message.startswith('Every row with an ImageView starts "Account settings"')
    assert of(rep, "a11y.label.decorative_merged") == []  # read first: R20's, not R21's
    # an icon without a description, or the same visible text first, is no finding
    plain = [_row(i, _icon(200 + i), _text(300 + i, f"Item {i}")) for i in range(4)]
    texts = [_row(i, _text(200 + i, "Inbox", b=(24, 0, 100, 60)), _text(300 + i, f"Item {i}"))
             for i in range(4)]
    for good in (plain, texts):
        assert of(ulint(screen(decor(1, _list(good)))), "a11y.label.shared_prefix") == []


def test_r21_a_decorative_description_merged_into_every_row():
    # Thunderbird's View rows (TB-12): each ends "Star", while the row's own star button says
    # "Add star".
    def row(i, cd="Star", twin=True):
        kids = [_text(300 + i, f"Message {i}"), _icon(400 + i, cd, b=(900, 200 + 150 * i, 72, 72))]
        if twin:
            kids.append(view(500 + i, "android.view.View", cd="Add star", flags=CLICK,
                             b=(960, 200 + 150 * i, 120, 150)))
        return _row(i, *kids)

    f = of(ulint(screen(decor(1, _list([row(i) for i in range(4)])))),
           "a11y.label.decorative_merged")
    assert [(x.severity, x.node_key) for x in f] == [("info", "view:100")]
    assert (f[0].evidence["merged"], f[0].evidence["rows"], f[0].evidence["twin"]) == (
        "Star", 4, "view:500")
    # a description that changes with the row is content; one with no control repeating it
    # and no picture word may be content too ("Verified")
    varied = [row(i, cd="Starred" if i % 2 else "Not starred") for i in range(4)]
    alone = [row(i, cd="Verified", twin=False) for i in range(4)]
    picture = [row(i, cd="Chevron icon", twin=False) for i in range(4)]
    assert of(ulint(screen(decor(1, _list(varied)))), "a11y.label.decorative_merged") == []
    assert of(ulint(screen(decor(1, _list(alone)))), "a11y.label.decorative_merged") == []
    assert len(of(ulint(screen(decor(1, _list(picture)))), "a11y.label.decorative_merged")) == 1


def test_r22_a_toggle_label_naming_the_action_contradicts_its_state():
    def toggle(hv, label, *flags):
        return comp(9, hv, "android.view.View", flags=CLICK + ("checkable",) + flags,
                    b=(100, 100 * hv, 144, 144),
                    kids=[comp(9, hv + 1000, cd=label, b=(130, 100 * hv + 30, 72, 72))])

    host = view(9, "androidx.compose.ui.platform.AndroidComposeView", b=(0, 0, 1080, 2400),
                kids=[toggle(1, "Unbookmark", "checked"), toggle(3, "Bookmark", "checked"),
                      toggle(5, "Unfollow interest"), toggle(7, "Bookmark"),
                      comp(9, 9, "android.widget.Button", cd="Unfollow", flags=CLICK,
                           b=(100, 900, 144, 144))])
    f = of(ulint(screen(decor(1, host))), "a11y.state.label_contradicts")
    assert [(x.node_key, x.evidence["said"], x.severity) for x in f] == [
        ("compose:9:1", "checked", "warn"), ("compose:9:5", "not checked", "warn")]
    assert 'TalkBack says "Checked. Unbookmark"' in f[0].message


def test_r23_every_row_says_not_selected_and_none_is():
    # Now in Android's Interests (NIA-9): each row opens its topic and holds its own follow
    # toggle, yet every row says "Not selected".
    def row(i, state="Not selected", flags=(), inner="Follow interest"):
        toggle = comp(8, 500 + i, flags=CLICK + ("checkable",), b=(900, 300 + 200 * i, 144, 144),
                      kids=[comp(8, 600 + i, cd=inner, b=(930, 330 + 200 * i, 72, 72))])
        return comp(8, 100 + i, flags=CLICK + ("checkable",) + tuple(flags), state=state,
                    b=(0, 300 + 200 * i, 1080, 200),
                    kids=[comp(8, 200 + i, "android.widget.TextView", text=f"Topic {i}",
                               b=(150, 330 + 200 * i, 500, 60)), toggle])

    def lazy(rows):
        return view(8, "androidx.compose.ui.platform.AndroidComposeView", b=(0, 0, 1080, 2400),
                    kids=[comp(8, 1, flags=("scrollable",), b=(0, 300, 1080, 2000), kids=rows,
                               collection_info={"row_count": 20, "column_count": 1})])

    f = of(ulint(screen(decor(1, lazy([row(i) for i in range(6)])))),
           "a11y.state.uniform_unselected")
    assert [(x.severity, x.node_key) for x in f] == [("info", "compose:8:100")]
    assert (f[0].evidence["rows"], f[0].evidence["row_count"],
            f[0].evidence["inner_label"]) == (6, 20, "Follow interest")
    one_on = [row(i, *(("Selected", ("checked",)) if i == 2 else ())) for i in range(6)]
    same_name = [row(i, inner=f"Topic {i}") for i in range(6)]  # the row toggles: onboarding
    few = [row(i) for i in range(4)]
    for good in (one_on, same_name, few):
        assert of(ulint(screen(decor(1, lazy(good)))), "a11y.state.uniform_unselected") == []


def test_r9_section_titles_between_groups_of_rows_are_not_headings():
    def title(hv, text, y, heading=False):
        return view(hv, "android.widget.TextView", text=text, b=(0, y, 1080, 80),
                    flags=("heading",) if heading else ())

    def rows(base, y):
        return [view(base + i, "android.widget.LinearLayout", flags=CLICK,
                     b=(0, y + 150 * i, 1080, 150),
                     kids=[_text(base + 50 + i, f"Setting {base + i}",
                                 b=(48, y + 150 * i + 30, 600, 60))]) for i in range(2)]

    def page(*, heading=False, sections=2):
        kids, y = [], 200
        for k in range(sections):
            kids.append(title(10 + k, f"Section {k}", y, heading))
            kids.extend(rows(100 + 10 * k, y + 80))
            y += 80 + 300
        return screen(decor(1, view(2, "android.widget.LinearLayout", b=(0, 0, 1080, 2400),
                                    kids=kids)))

    f = [x for x in of(ulint(page()), "a11y.heading.structure")
         if x.evidence.get("reason") == "section_title"]
    assert [(x.node_key, x.severity, x.evidence["rows"]) for x in f] == [
        ("view:10", "info", 2), ("view:11", "info", 2)]
    assert of(ulint(page(heading=True)), "a11y.heading.structure") == []
    assert of(ulint(page(sections=1)), "a11y.heading.structure") == []  # one caption: no
