"""The real-app hunt's static defects, linted (docs/realapp-findings.md, gaps G17, G18, L3).

TalkBack on Thunderbird and Now in Android said things wrong that no lint rule read (TB-7,
TB-11, TB-12, NIA-9, NIA-10). R19..R23 read them now, and R9 reports section titles that are
not headings. Each finding is pinned here on the recorded real screen it was found on
(tests/data/realapps, 480dpi), the precision claim with it: these rules fire on exactly the
screens and nodes in ``HUNT`` and on no A11yProbe screen, GOOD or BAD. Then the same through
a capture's ``lint()`` (what an agent reads), with its byte cost.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from inspector_widget import a11y
from inspector_widget import a11y_lint as L
from inspector_widget.proto import view_inspection_pb2 as pb

DATA = Path(__file__).parent / "data" / "realapps"
TB_WALKS = Path(__file__).parent / "data" / "tb_walks"
FIXTURES = Path(__file__).parent / "fixtures"
DENSITY = 480  # emulator-5554 and emulator-5558 alike

NEW = ("a11y.label.placeholder_token", "a11y.label.shared_prefix",
       "a11y.label.decorative_merged", "a11y.toggle.label_contradicts",
       "a11y.selection.uniform_unselected")
TITLE = ("a11y.heading.structure", "section_title")

#: Every finding of R19..R23 and every R9 section title on the recorded real screens.
HUNT = {
    # TB-7: inline-content placeholders read aloud, one per token
    "thunderbird_list_compose": [("a11y.label.placeholder_token", "compose:763:1172"),
                                 ("a11y.label.placeholder_token", "compose:783:1220")],
    "thunderbird_list_compose_tb_first": [("a11y.label.placeholder_token", "compose:87:97")],
    "thunderbird_list_compose_tb_later": [("a11y.label.placeholder_token", "compose:127:97")],
    "thunderbird_list_compose_no_banner": [("a11y.label.placeholder_token", "compose:117:57")],
    "thunderbird_selection_mode": [("a11y.label.placeholder_token", "compose:127:97")],
    # TB-11: "Account settings" first on every icon row; the section titles are list items
    "thunderbird_settings": [TITLE + ("view:402",), TITLE + ("view:414",),
                             TITLE + ("view:423",), ("a11y.label.shared_prefix", "view:398")],
    # TB-12: "Star" merged into every View row
    "thunderbird_list_views": [("a11y.label.decorative_merged", "view:352")],
    # NIA-9: "Not selected" on every Interests row
    "nia_interests": [("a11y.selection.uniform_unselected", "compose:8:168")],
    # L3: the settings dialog's section titles
    "nia_settings_dialog": [TITLE + ("compose:205:598",), TITLE + ("compose:205:610",),
                            TITLE + ("compose:205:620",)],
}


def dump(name):
    return json.loads(gzip.decompress((DATA / f"{name}.a11y.json.gz").read_bytes()))


def lint(data, **kw):
    return L.lint_unified(data, L.LintContext(density=DENSITY), **kw)


def hunt_findings(report):
    out = []
    for f in report.findings:
        if f.rule in NEW:
            out.append((f.rule, f.node_key))
        elif f.evidence.get("reason") == "section_title":
            out.append(TITLE + (f.node_key,))
    return sorted(out)


def of(report, rule):
    return [f for f in report.findings if f.rule == rule]


@pytest.mark.parametrize("name", sorted(p.name.split(".")[0] for p in DATA.glob("*.a11y.json.gz")))
def test_the_hunt_rules_fire_on_exactly_these_real_screens(name):
    assert hunt_findings(lint(dump(name))) == sorted(HUNT.get(name, []))


def _a11yprobe_dumps():
    for p in sorted(TB_WALKS.glob("*.a11y.pb.gz")):
        yield p.name.split(".")[0], p
    for p in sorted((FIXTURES / "tb_captures").glob("tb_*/raw/a11y.pb.gz")):
        yield p.parent.parent.name, p
    for p in sorted((FIXTURES / "captures").glob("a11yprobe_*/raw/a11y.pb.gz")):
        yield p.parent.parent.name, p


@pytest.mark.parametrize("name,path", list(_a11yprobe_dumps()), ids=lambda x: str(x)[-40:])
def test_no_hunt_rule_fires_on_an_a11yprobe_screen(name, path):
    # The corpus's GOOD variants hold the stable label, the decorative icon and the
    # semantic heading the real apps lacked: none of R19..R23 nor an R9 section title.
    resp = pb.DumpA11yResponse.FromString(gzip.decompress(path.read_bytes()))
    assert hunt_findings(lint(a11y.a11y_to_dict(resp))) == [], name


# ------------------------------------------------------------------------------------ TB-7
def test_tb7_placeholder_tokens_one_finding_per_token_with_the_rows():
    rep = lint(dump("thunderbird_list_compose"))
    f = {x.evidence["token"]: x for x in of(rep, "a11y.label.placeholder_token")}
    assert set(f) == {"[attachment_icon]", "[conversation_counter]"}
    for tok, x in f.items():
        assert (x.severity, x.evidence["rows"], x.evidence["kind"]) == (
            "warn", 1, "a bracketed identifier"), tok
        assert x.message.startswith(f'TalkBack reads "{tok}" aloud')
        assert "alternateText" in x.message
    first = of(lint(dump("thunderbird_list_compose_tb_first")), "a11y.label.placeholder_token")
    assert [x.evidence["token"] for x in first] == ["[conversation_counter]"]
    assert first[0].node["label"].startswith("Re: Thread")  # the row TalkBack reads it in


# ----------------------------------------------------------------------------------- TB-11
def test_tb11_settings_rows_share_a_decorative_prefix_and_titles_are_not_headings():
    rep = lint(dump("thunderbird_settings"))
    pre = of(rep, "a11y.label.shared_prefix")
    assert len(pre) == 1
    ev = pre[0].evidence
    assert (ev["prefix"], ev["child"], ev["child_class"], ev["rows"], ev["of"]) == (
        "Account settings", "view:400", "ImageView", 8, 11)
    assert 'starts "Account settings"' in pre[0].message
    assert 'importantForAccessibility="no"' in pre[0].message
    titles = [f for f in of(rep, "a11y.heading.structure")
              if f.evidence["reason"] == "section_title"]
    assert [(f.node["label"], f.severity, f.evidence["position"]) for f in titles] == [
        ("Accounts", "warn", "2 of 11"), ("Backup", "warn", "5 of 11"),
        ("Miscellaneous", "warn", "8 of 11")]
    # before this round: only the 3 touch-target warnings on those titles
    assert rep.summary["by_rule"] == {"a11y.touch_target.small": 3,
                                      "a11y.heading.structure": 3,
                                      "a11y.label.shared_prefix": 1}


# ----------------------------------------------------------------------------------- TB-12
def test_tb12_star_is_merged_into_every_view_row():
    f = of(lint(dump("thunderbird_list_views")), "a11y.label.decorative_merged")
    assert len(f) == 1 and f[0].severity == "info"
    ev = f[0].evidence
    assert (ev["merged"], ev["rows"], ev["of"], ev["twin_label"]) == ("Star", 6, 6, "Add star")
    assert '"Star" is read in 6 of 6 rows' in f[0].message


# ------------------------------------------------------------------------- NIA-9 / NIA-10
def test_nia9_interests_is_no_longer_silent():
    rep = lint(dump("nia_interests"))
    s = rep.summary
    assert (s["error"], s["warn"], s["info"]) == (0, 0, 1)  # was 0/0/0
    f = of(rep, "a11y.selection.uniform_unselected")[0]
    assert (f.evidence["state"], f.evidence["rows"], f.evidence["row_count"],
            f.evidence["inner_label"]) == ("Not selected", 10, 20, "Follow interest")


def _bookmarked(data, *, checked=True, label="Unbookmark"):
    """Now in Android's feed with its first card bookmarked: the toggle (compose:8:509)
    checked, its icon (compose:8:511) saying "Unbookmark" (NewsResourceCard.kt)."""
    def walk(n):
        yield n
        for c in n.get("children") or []:
            yield from walk(c)

    for w in data["windows"]:
        for n in walk(w["root"]) if w.get("root") else ():
            if n.get("node_key") == "compose:8:509" and checked:
                n["flags"] = list(n["flags"]) + ["checked"]
            if n.get("node_key") == "compose:8:511":
                n["content_description"] = label
    return data


def test_nia10_a_checked_unbookmark_contradicts_its_state():
    plain = lint(dump("nia_feed"))
    assert of(plain, "a11y.toggle.label_contradicts") == []  # "Bookmark", not checked
    f = of(lint(_bookmarked(dump("nia_feed"))), "a11y.toggle.label_contradicts")
    assert [(x.node_key, x.severity, x.evidence["said"], x.evidence["undo"]) for x in f] == [
        ("compose:8:509", "warn", "checked", "Unbookmark")]
    assert 'TalkBack says "checked. Unbookmark. Check box"' in f[0].message
    on = lint(_bookmarked(dump("nia_feed"), label="Bookmark"))  # a stable label: fine
    assert of(on, "a11y.toggle.label_contradicts") == []


# ------------------------------------------------------------------------------------ G18
def test_g18_compose_stars_with_a_clear_touch_area_are_info_the_overflow_stays():
    for name in ("thunderbird_list_compose_tb_first", "thunderbird_list_compose_tb_later",
                 "thunderbird_list_compose_no_banner", "thunderbird_list_compose"):
        touch = of(lint(dump(name)), "a11y.touch_target.small")
        stars = [f for f in touch if f.node_key.startswith("compose:")]
        views = [f for f in touch if f.node_key.startswith("view:")]
        assert stars and all(f.severity == "info" and f.evidence["touch_area_clear"]
                             and (f.evidence["w_dp"], f.evidence["h_dp"]) == (48.0, 24.0)
                             for f in stars), name
        assert [(f.severity, f.evidence["w_dp"]) for f in views] == [("warn", 40.0)], name


# ------------------------------------------------------------------------------------- L3
def test_l3_r9_reports_the_nia_settings_section_titles_in_a_capture():
    import tb_capture_fixtures as F

    from inspector_widget.capture import analyzers as an
    from inspector_widget.output import dumps

    ix, raw = F.fixture_capture("nia_settings")
    out = an.lint_view(ix, raw, rules=["R9"])
    assert out["counts"] == {"error": 0, "warn": 0, "info": 3}
    nodes = out["rules"][0]["nodes"]
    assert [n.split('"')[1] for n in nodes] == ["Theme", "Use Dynamic Color",
                                                "Dark mode preference"]
    assert all("section title, not a heading" in n for n in nodes)
    assert len(dumps(out).encode()) <= 900


def _capture(name, package="app"):
    import fakescenes as fs
    import tb_capture_fixtures as F

    raw = F.raw_from_a11y(fs.a11y_to_pb(dump(name)), cid="chuntl01", package=package,
                          dpi=DENSITY)
    return F.build(raw), raw


@pytest.mark.parametrize("name,rules,limit,want", [
    # (the capture lint an agent reads: rules, its byte cap, the lines it must show)
    ("thunderbird_list_compose_tb_first", None, 1500,
     ['reads \\"[conversation_counter]\\" in 1 row(s)']),
    ("thunderbird_list_compose", ["R19"], 800,
     ['reads \\"[attachment_icon]\\" in 1 row(s)', 'reads \\"[conversation_counter]\\"']),
    ("thunderbird_settings", None, 1500,
     ['8 of 11 rows start \\"Account settings\\", the description of ImageView in each',
      # the three section titles are one template line: the same cell shape
      '"rule":"a11y.heading.structure","sev":"warn","n":3',
      "in #settings_list cells (TextView#text[*]): view:402 view:414 view:423"]),
    ("thunderbird_list_views", ["R21"], 800,
     ['\\"Star\\" merged into 6 of 6 rows, from ImageView; \\"Add star\\" says it']),
    ("nia_interests", None, 800,
     ['10 rows say \\"Not selected\\", none selected; each has \\"Follow interest\\"']),
])
def test_the_capture_lint_shows_each_hunt_finding_within_budget(name, rules, limit, want):
    from inspector_widget.capture import analyzers as an
    from inspector_widget.output import dumps

    ix, raw = _capture(name)
    text = dumps(an.lint_view(ix, raw, rules=rules))
    assert len(text.encode()) <= limit, len(text.encode())
    for w in want:
        assert w in text, (w, text)
