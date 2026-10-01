"""TalkBack's walk over a capture: outline(view="reading") with explain, granularity,
from, direction and include_skipped, and node()'s tb facet (capture/tb.py over the stored
accessibility tree). Offline, on real captures and the TalkBack corpus captures; through
both entry points (MCP and the CLI) at the end."""

from __future__ import annotations

import contextlib
import io
import json

import capture_harness as ch
import capture_replay as cr
import pytest
import tb_capture_fixtures as F
from capture_builders import launcher_index

import cli
import mcp_server
from inspector_widget.capture import lines as L
from inspector_widget.capture import query as q
from inspector_widget.capture.model import OpError


def nbytes(doc) -> int:
    return len(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode())


def reading(name, **args):
    ix, raw = F.fixture_capture(name) if not name.startswith("tb_") else F.corpus_capture(name)
    return q.outline(ix, view="reading", loaded=raw, **args)


# --------------------------------------------------------------------------- outline reading
def test_launcher_reading_with_explain_fits_2500_bytes():
    out = reading("a11yprobe_launcher", explain=True)
    assert nbytes(out) <= 2500 and out["total"] == 13 and "truncated" not in out
    assert all(L.is_line(x) for x in out["lines"])
    assert out["lines"][0] == '1. sem:7:10 TextView "A11yProbe" [48,210 322x84] why=srf'
    assert out["lines"][1].startswith(
        '2. sem:7:20 @launch_all "▶ All scenarios (lint everything). every BAD/GO…" click')
    assert out["lines"][1].endswith(" why=click")
    # the sliver at the list's edge: TalkBack scrolls it in first, then says what it shows
    assert out["lines"][12] == (
        '13. sem:7:141 @launch_heading "Section heading, MissingHeading" click tgroup '
        '[0,2757 1280x27] !clipped !touch_target why=leaf show_on_screen=sem:7:18 '
        'speak=after_scroll')


def test_the_default_reading_view_is_unchanged():
    out = reading("a11yprobe_launcher")
    assert out["lines"][0] == '1. sem:7:10 TextView "A11yProbe" [48,210 322x84]'
    assert "why=" not in " ".join(out["lines"])


def test_explain_quotes_what_talkback_says_on_arrival():
    out = reading("nia_settings", explain=True)
    assert out["lines"][2] == (
        '3. sem:80:191 RadioButton "Selected. Default. Radio button. 1 of 2. In lis…" click '
        'checkable checked selected [191,699 897x144] why=focusable')
    assert out["lines"][3].startswith(
        '4. sem:80:195 RadioButton "Not selected. Android. Radio button. 2 of 2" click')


def test_granularity_keeps_talkbacks_heading_and_control_stops():
    heads = reading("a11yprobe_all", granularity="heading")
    assert heads["granularity"] == "heading"
    assert [x.split(" ")[0] for x in heads["lines"]] == ["3.", "8.", "13.", "18."]
    assert all(" heading " in x for x in heads["lines"])
    ctrl = reading("nia_settings", granularity="control")
    types = [L.parse_line(x)["segs"][0].get("type") for x in ctrl["lines"]]
    assert set(types) == {"RadioButton", "Button"} and len(types) == 12


def test_from_a_node_backwards():
    out = reading("nia_settings", explain=True, **{"from": "sem:80:203"}, direction="prev")
    assert (out["from"], out["direction"], out["ended"]) == ("sem:80:203", "prev", "edge")
    assert [x.split(" ")[0] for x in out["lines"]] == ["6.", "5.", "4.", "3.", "2.", "1."]
    fwd = reading("nia_settings", **{"from": "Button\"Licenses\""})
    assert [x.split(" ")[0] for x in fwd["lines"]] == ["13.", "14.", "15.", "16."]
    assert fwd["ended"] == "edge"


def test_include_skipped_says_why_a_node_is_not_a_stop():
    merged = reading("tb_c9-bad-walk", include_skipped=True)["lines"]
    assert merged[1:4] == [
        '2. a11y:7:5 #tb_c9_row "$5, Socks" click [39,264 1998x137] !role !wrong_announcement',
        '- a11y:7:6 TextView "$5" [1951,303 47x59] merged_into=a11y:7:5',
        '- a11y:7:7 TextView "Socks" [78,303 111x59] merged_into=a11y:7:5']
    hidden = reading("tb_h1-bad-walk", include_skipped=True, max_lines=3)
    assert hidden["lines"][2] == ('- view:13 ComposeView [0,260 2076x137] !skipped '
                                  'hidden_by=view:13')
    covered = reading("nia_settings", include_skipped=True)["lines"]
    assert covered[-1] == "- view:1 DecorView [0,0 1280x2856] covered_by=view:76"
    silent = reading("tb_v9-bad-walk", include_skipped=True)["lines"]
    assert ('- view:4 LinearLayout click [0,380 2076x117] !label_missing !touch_target '
            'why=silent_container') in silent
    assert all(L.is_line(x) for x in merged + hidden["lines"] + silent + covered)


def test_reading_pages_carry_the_reading_arguments():
    first = reading("nia_settings", explain=True, max_lines=4)
    cur = first["truncated"]["cursor"]
    assert first["next"][0] == f'outline(view="reading",explain=true,max_lines=4,cursor="{cur}")'
    ix, raw = F.fixture_capture("nia_settings")
    second = q.outline(ix, view="reading", explain=True, max_lines=4, cursor=cur, loaded=raw)
    assert second["lines"][0].startswith("5. sem:80:201 ") and second["offset"] == 4
    with pytest.raises(OpError) as e:  # the cursor belongs to explain=true
        q.outline(ix, view="reading", max_lines=4, cursor=cur, loaded=raw)
    assert e.value.code == "bad_args"


def test_reading_arguments_need_the_reading_view():
    ix, raw = F.fixture_capture("nia_settings")
    for bad in ({"explain": True}, {"granularity": "heading"}, {"direction": "prev"},
                {"include_skipped": True}, {"from": "sem:80:191"}):
        with pytest.raises(OpError) as e:
            q.outline(ix, loaded=raw, **bad)
        assert e.value.code == "bad_args", bad
    with pytest.raises(OpError) as e:
        q.outline(ix, view="reading", granularity="link", loaded=raw)
    assert e.value.code == "bad_args"


def test_without_an_accessibility_tree_the_stored_order_is_used():
    ix = launcher_index()  # no raw facets behind it
    out = q.outline(ix, view="reading", explain=True, direction="prev")
    assert out["model"].startswith("stored order") and out["total"] == 13
    assert out["lines"][0].startswith("13. n22 ")


# --------------------------------------------------------------------------- node tb facet
def tb_facet(name, ref):
    ix, raw = F.fixture_capture(name) if not name.startswith("tb_") else F.corpus_capture(name)
    out = q.node(ix, raw, ref, facets="tb")
    return out["tb"], out


def test_tb_facet_of_a_stop():
    tb, out = tb_facet("nia_settings", "sem:80:191")
    assert tb == {
        "stop": 3, "why": "focusable",
        "speak": "Selected. Default. Radio button. 1 of 2. In list. 2 items",
        "parts": [{"t": "Selected", "from": "sem:80:191", "k": "state"},
                  {"t": "Default", "from": "a11y:80:194", "k": "child"},
                  {"t": "Radio button", "from": "a11y:80:1000000191", "k": "role(fake)"},
                  {"t": "1 of 2", "from": "sem:80:191", "k": "collection"},
                  {"t": "In list. 2 items", "from": "sem:80:190", "k": "collection"}],
        "prev": "sem:80:189", "next": "sem:80:195", "edge_in": "chain", "reachable": "swipe"}
    assert nbytes(tb) <= 500


def test_tb_facet_of_nodes_talkback_does_not_stop_on():
    merged, _ = tb_facet("tb_c9-bad-walk", "a11y:7:6")
    assert merged == {"stop": None, "why_not": "merged_into:a11y:7:5", "speak_in": "$5",
                      "reachable": "swipe"}
    hidden, _ = tb_facet("tb_h1-bad-walk", "view:13")
    assert hidden["why_not"].startswith("hidden_by:view:13: importantForAccessibility="
                                        "noHideDescendants (on this node)")
    silent, _ = tb_facet("tb_v9-bad-walk", "view:4")
    assert silent["why_not"] == ("silent_container: focusable but nothing of its own to "
                                 "say; the stops are view:5,view:6")
    ghost, _ = tb_facet("tb_c1-bad-walk", "a11y:7:9")
    assert ghost["ghost"] == ["unlabelled"] and ghost["speak"] == "Check box"
    reorder, _ = tb_facet("tb_v3-good-walk", "view:6")
    assert reorder["edge_in"] == "before:view:4"


def test_a_child_its_rows_description_silences_is_not_merged_into_it():
    # V4 BAD (live): the row's contentDescription "Settings row" replaces "Wi-Fi", so
    # TalkBack never says it (the walk's tb.skipped); not "read inside the row"
    ix, raw = F.live_capture("tb_v4_bad")
    tb = q.node(ix, raw, "view:5", facets="tb")["tb"]
    assert tb == {"stop": None, "reachable": "not",
                  "why_not": "silenced_by:view:4: its contentDescription replaces its "
                             "children's text"}
    out = q.outline(ix, view="reading", loaded=raw, include_skipped=True)
    assert '- view:5 TextView "Wi-Fi" [0,280 1959x107] silenced_by=view:4' in out["lines"]
    # a merged child the stop does read keeps merged_into and its share of the speech
    merged, _ = tb_facet("tb_c9-bad-walk", "a11y:7:6")
    assert merged["why_not"] == "merged_into:a11y:7:5" and merged["reachable"] == "swipe"


def test_a_view_scrolled_out_of_its_scrollview_is_offscreen_not_zero_size():
    # its bounds are clipped to an empty rect at the window's bottom edge: TalkBack
    # auto-scrolls the ScrollView to it
    tb, _ = tb_facet("a11yprobe_viewscreen", "view:37")
    assert tb == {"stop": None, "why_not": "offscreen", "reachable": "scroll"}
    lines = reading("a11yprobe_viewscreen", include_skipped=True)["lines"]
    assert '- view:37 MaterialTextView #pinLabel "PIN" hidden [48,2856 0x0] why=offscreen' \
        in lines
    assert not any("zero_size" in x for x in lines)


def test_from_a_container_talkback_never_gets_starts_at_its_first_stop():
    # V5 BAD: the dialog card is a LinearLayout that is not important for accessibility
    ix, raw = F.live_capture("tb_v5_bad")
    out = q.outline(ix, view="reading", loaded=raw, **{"from": "view:13"})
    assert out["from"] == "view:13 -> view:14" and out["ended"] == "edge"
    assert [x.split()[1] for x in out["lines"]] == ["view:14", "view:15", "view:16"]
    # a node with no stop inside still says why
    out = q.outline(ix, view="reading", loaded=raw, **{"from": "view:19"})
    assert out["total"] == 0 and out["ended"].startswith("empty: not a TalkBack node")


def test_the_ghost_facet_sizes_tiny_at_the_captures_dpi():
    from inspector_widget.capture import tb as T

    ix, raw = F.live_capture("tb_v12_good")
    tbc = T.TbCapture(raw.a11y, ix)
    assert tbc.density == ix.meta.device["dpi"] != 420


def test_facets_all_has_tb_only_with_a_model():
    _tb, out = tb_facet("nia_settings", "sem:80:191")
    ix, raw = F.fixture_capture("nia_settings")
    assert "tb" in q.node(ix, raw, "sem:80:191", facets="all", max_bytes=6000)
    assert "tb" not in q.node(launcher_index(), None, "n22", facets="all", max_bytes=6000)
    assert q.node(launcher_index(), None, "n22", facets="tb")["tb"].startswith("unavailable")


# --------------------------------------------------------------------------- MCP and CLI
def _mcp(tool, **args):
    text, is_error = mcp_server._call_tool_text(tool, args)
    assert not is_error, text
    return text


def _cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main([str(a) for a in argv] + ["--json"])
    assert rc == 0, err.getvalue()
    return out.getvalue().rstrip("\n")


def test_mcp_and_cli_give_the_same_talkback_answers(tmp_path):
    rec = cr.load("nia_settings")
    with ch.harness(cr.scene(rec), str(tmp_path), toolset="capture") as (dev, _scene):
        cr.replay_device(dev, rec)
        cap = json.loads(_mcp("capture", serial=ch.SERIAL, package=rec.package))
        cid = cap["capture"]
        pairs = [
            (("outline", {"view": "reading", "explain": True, "from": "sem:80:203",
                          "direction": "prev", "include_skipped": True}),
             ("outline", "--view", "reading", "--explain", "--from", "sem:80:203",
              "--direction", "prev", "--include-skipped")),
            (("outline", {"view": "reading", "granularity": "control", "max_lines": 3}),
             ("outline", "--view", "reading", "--granularity", "control", "--max-lines", 3)),
            (("node", {"ref": "sem:80:191", "facets": "tb"}),
             ("node", "sem:80:191", "--facets", "tb")),
            (("lint", {"rules": ["tb"]}), ("lint", "--rule", "tb")),
        ]
        for (tool, args), argv in pairs:
            mcp_text = _mcp(tool, capture=cid, **args)
            assert _cli(*argv, "-c", cid) == mcp_text, (tool, args)
        doc = json.loads(_mcp("outline", capture=cid, view="reading", explain=True))
        third = L.parse_line(doc["lines"][2])
        assert third["order"] == 3 and third["segs"][0]["label"] == (
            "Selected. Default. Radio button. 1 of 2. In lis…")
        nxt = json.loads(_mcp("outline", capture=cid, view="reading", granularity="control",
                              max_lines=3))["next"][0]
        assert nxt.startswith('outline(view="reading",granularity="control",max_lines=3,')
