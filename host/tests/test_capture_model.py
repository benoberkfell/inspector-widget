"""Offline tests for inspector_widget.capture.model and tests/capture_builders."""

from __future__ import annotations

import gzip
import json
import os
import re
import subprocess
import sys
import time

import pytest
from capture_builders import (
    CLIPPED_RULE,
    LAUNCHER_ITEMS,
    ROLE_RULE,
    STATE_RULE,
    TOUCH_RULE,
    IndexBuilder,
    big_index,
    launcher_index,
    wide_index,
)

from inspector_widget.capture import model as m

HOST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------- keys and ids
def test_canonical_keys_and_parse_round_trip():
    cases = {
        m.view_key(82): ("view", (82,)),
        m.sem_key(82, 448): ("sem", (82, 448)),
        m.a11y_key(1, -1): ("a11y", (1, -1)),
        m.a11y_path_key(1, [0, 2, 1]): ("a11y_path", (1, (0, 2, 1))),
        m.window_key(1): ("w", (1,)),
        m.compose_legacy_key(325): ("compose", (325,)),
    }
    assert list(cases) == ["view:82", "sem:82:448", "a11y:1:-1", "a11y:path:1:0.2.1", "w:1",
                           "compose:325"]
    for key, parsed in cases.items():
        assert m.is_key(key)
        assert m.parse_key(key) == parsed
    sk = m.slot_key(82, "ListItem@MainActivity.kt:150[heading]:0")
    assert re.fullmatch(r"slot:82:[0-9a-f]{8}", sk)
    assert m.parse_key(sk) == ("slot", (82, sk.split(":")[2]))
    assert sk == m.slot_key(82, "ListItem@MainActivity.kt:150[heading]:0")  # deterministic
    assert sk != m.slot_key(83, "ListItem@MainActivity.kt:150[heading]:0")
    for bad in ("", "view:", "view:x", "sem:1", "slot:1:xyz", "n23", "compose:", "w:1:2", None, 3):
        assert not m.is_key(bad)
        assert m.parse_key(bad) is None
    with pytest.raises(ValueError):
        m.a11y_path_key(1, [])


def test_refs_capture_ids_and_labels():
    assert m.ref_str(23) == "n23" and m.ref_num("n23") == 23
    assert m.is_ref("n1") and not m.is_ref("n0") and not m.is_ref("n01") and not m.is_ref("N1")
    with pytest.raises(ValueError):
        m.ref_str(0)
    with pytest.raises(ValueError):
        m.ref_num("x2")
    assert m.is_capture_id("c7h2kq") and m.is_capture_id("C7H2KQ")
    assert not m.is_capture_id("c7h2k") and not m.is_capture_id("c7h2ki")  # no 'i' in Crockford
    assert m.normalize_capture_id("C7H2KQ") == "c7h2kq"
    assert m.is_valid_label("before") and m.is_valid_label("after-tap_2")
    assert not m.is_valid_label("c7h2kq")  # looks like an id
    assert not m.is_valid_label("Before") and not m.is_valid_label("1st")
    assert not m.is_valid_label("x" * 33) and m.is_valid_label("x" * 32)
    assert set(m.CROCKFORD) == set("0123456789abcdefghjkmnpqrstvwxyz")


def test_op_error_codes_and_shape():
    e = m.OpError("ref_not_in_capture", "n99 is not in c7h2kq", hint="capture again",
                  candidates=["n98"])
    assert e.to_dict() == {"error": {"code": "ref_not_in_capture", "message": "n99 is not in c7h2kq",
                                     "hint": "capture again", "candidates": ["n98"]}}
    assert m.OpError("bad_args", "x").to_dict() == {
        "error": {"code": "bad_args", "message": "x", "hint": None}}
    with pytest.raises(ValueError):
        m.OpError("nope", "x")
    assert set(m.ERROR_CODES) == {
        "no_session", "device_lost", "agent_error", "capture_not_found", "ref_not_in_capture",
        "not_found", "ambiguous", "bad_selector", "bad_args", "facet_unavailable", "unsupported"}


# --------------------------------------------------------------------------- options, meta, raw
def test_capture_options_defaults_validation_and_round_trip():
    o = m.CaptureOptions()
    assert o.to_dict() == {"props": True, "resolution_stack": False, "slots": "if_available",
                           "screenshot": True, "screenshot_scale": 1.0, "skp": False,
                           "a11y_rendering": False, "lint": "tree", "settle_ms": 0}
    assert m.CaptureOptions.from_dict(dict(o.to_dict(), future_flag=1)) == o
    for bad in ({"slots": "always"}, {"lint": "deep"}, {"screenshot_scale": 0},
                {"screenshot_scale": 1.5}, {"settle_ms": -1}):
        with pytest.raises(m.OpError) as exc:
            m.CaptureOptions(**bad).validate()
        assert exc.value.code == "bad_args"


def test_capture_meta_json_round_trip():
    meta = m.CaptureMeta(id="c7h2kq", lineage=("emulator-5554", "com.oberkfell.a11yprobe"),
                         pid=4312, api=37, abi="arm64-v8a", agent_version="viewspector-0.1",
                         device={"dpi": 480, "font_scale": 1.0, "screen": [1280, 2856]},
                         created_at=1790000000.25, took_ms=640,
                         options=m.CaptureOptions(slots="off"), label="before",
                         diagnostics=["a11y ids not unique (agent ID1)"])
    meta.set_facet("views", "ok", ms=40, nbytes=1234)
    meta.set_facet("slots", "off", reason="slots=\"off\"")
    with pytest.raises(ValueError):
        meta.set_facet("views", "great")
    d = json.loads(meta.to_json())
    assert d["lineage"] == {"serial": "emulator-5554", "package": "com.oberkfell.a11yprobe"}
    assert d["options"]["slots"] == "off" and d["schema"] == 1
    back = m.meta_from_json(m.meta_to_json(meta))
    assert back == meta
    assert back.serial == "emulator-5554" and back.package == "com.oberkfell.a11yprobe"
    assert back.facet_status("slots") == "off" and back.facet_status("skp") is None
    # unknown future keys are ignored
    d["future"] = 1
    assert m.CaptureMeta.from_dict(d) == meta


def test_raw_capture_files_round_trip():
    meta = m.CaptureMeta(id="c00000", lineage=("s", "p"))
    raw = m.RawCapture(meta=meta, windows=b"W", views=b"V" * 10, compose_sem=b"C",
                       a11y=b"A", shots={1: b"S1", 2203: b"S2"}, skp={1: b"K"})
    files = raw.files()
    assert set(files) == {"raw/windows.pb", "raw/views.pb", "raw/compose_sem.pb", "raw/a11y.pb",
                          "shot/w_1.pb", "shot/w_2203.pb", "raw/skp_1.bin"}
    assert m.shot_file(7) == "shot/w_7.pb" and m.skp_file(7) == "raw/skp_7.bin"
    back = m.RawCapture.from_files(meta, dict(files, **{"derived/x.json": b"ignored"}))
    assert back == raw
    assert back.slots is None and back.a11y_render is None
    assert raw.nbytes() == sum(len(v) for v in files.values())


def test_default_store_root_precedence(tmp_path):
    assert m.default_store_root({"INSPECTOR_WIDGET_CAPTURE_DIR": str(tmp_path / "s")}) == \
        str(tmp_path / "s")
    assert m.default_store_root({"XDG_CACHE_HOME": "/x/cache"}, home="/h") == \
        "/x/cache/inspector-widget"
    assert m.default_store_root({}, platform="darwin", home="/h") == \
        "/h/Library/Caches/inspector-widget"
    assert m.default_store_root({}, platform="linux", home="/h") == "/h/.cache/inspector-widget"


def test_lineage_state_round_trip():
    st = m.LineageState(latest="c2", history=["c2", "c1"], labels={"before": "c1"},
                        tomb={"n9": ["Text", "Gone", "@x", "c1"]})
    assert m.LineageState.from_dict(json.loads(json.dumps(st.to_dict()))) == st
    assert m.LineageState.from_dict(None) == m.LineageState()
    name = m.lineage_file_name("192.168.1.5:5555", "com.x")
    assert name.startswith("192.168.1.5_5555__com.x-") and name.endswith(".json")


def test_lineage_file_names_never_collide():
    names = [m.lineage_file_name(*lin) for lin in (
        ("192.168.1.7:5555", "com.x"), ("192.168.1.7_5555", "com.x"),
        ("emulator-5554", "com.Slack"), ("emulator-5554", "com.slack"))]
    # distinct even on a case-insensitive disk
    assert len({n.lower() for n in names}) == 4


# --------------------------------------------------------------------------- nodes and index
def test_unode_to_dict_omits_defaults_and_round_trips():
    n = m.UNode(key="view:1", ref="n1", z=0, window="n1", type="DecorView", b=[0, 0, 10, 10])
    d = n.to_dict()
    assert d == {"key": "view:1", "ref": "n1", "window": "n1", "z": 0, "type": "DecorView",
                 "b": [0, 0, 10, 10]}
    assert m.UNode.from_dict(d) == n
    assert n.id == "n1" and n.is_window
    bare = m.UNode(key="sem:82:1", kind="compose")
    assert bare.id == "sem:82:1" and not bare.is_window
    n.issues.append(m.Issue("render.clipped", "warn", {"visible_px": 27}, conf="inferred"))
    assert m.UNode.from_dict(json.loads(json.dumps(n.to_dict()))) == n


def test_tree_walk_and_depth_limit():
    t = m.Tree(roots=["a"], children={"a": ["b", "c"], "b": ["d"]})
    assert list(t.walk()) == [("a", 0), ("b", 1), ("d", 2), ("c", 1)]
    assert list(t.walk(max_depth=1)) == [("a", 0), ("b", 1), ("c", 1)]
    assert list(t.walk("b")) == [("b", 0), ("d", 1)]
    assert t.parents() == {"b": "a", "c": "a", "d": "b"}
    assert m.Tree.from_dict(t.to_dict()) == t


@pytest.mark.parametrize("compress", [False, True])
def test_index_jsonl_round_trip_is_lossless(compress):
    ix = launcher_index()
    ix.by_key["compose:448"] = "n22"  # an extra alias must survive too
    ix.diagnostics.append("a11y ids not unique (agent ID1); a11y facets matched by bounds")
    blob = m.index_to_jsonl(ix, compress=compress)
    assert (blob[:2] == b"\x1f\x8b") is compress
    back = m.index_from_jsonl(blob, ix.meta)
    assert back == ix
    assert list(back.nodes) == list(ix.nodes)  # pre-order preserved
    assert back.get("n11").label.startswith("▶ All scenarios")  # unicode survives
    text = (gzip.decompress(blob) if compress else blob).decode("utf-8")
    assert "▶" in text  # stored as utf-8, not \\u escapes
    assert len(text.strip().split("\n")) == len(ix) + 1


def test_index_from_jsonl_rejects_other_schema_and_truncation():
    ix = launcher_index()
    lines = m.index_to_jsonl(ix).decode().strip().split("\n")
    header = json.loads(lines[0])
    header["index"] = 99
    with pytest.raises(ValueError, match="schema"):
        m.index_from_jsonl(("\n".join([json.dumps(header)] + lines[1:])).encode(), ix.meta)
    with pytest.raises(ValueError, match="truncated"):
        m.index_from_jsonl(("\n".join(lines[:-3])).encode(), ix.meta)
    with pytest.raises(ValueError):
        m.index_from_jsonl(b"", ix.meta)


def test_remap_ids_moves_every_reference():
    ix = launcher_index()
    # Go back to key space (as build_index returns it), then forward again.
    to_keys = {nid: n.key for nid, n in ix.nodes.items()}
    kx = m.remap_ids(ix, to_keys, set_refs=False)
    for n in kx.nodes.values():
        n.ref = None
    assert set(kx.nodes) == {n.key for n in ix.nodes.values()}
    assert kx.nodes["sem:82:448"].parent == "sem:82:325"
    assert kx.nodes["sem:82:448"].facets["compose"]["slots"][0].startswith("slot:82:")
    assert kx.trees["ui"].roots == ["view:1"] and kx.reading[0] == "sem:82:317"
    assert kx.by_key["w:1"] == "view:1"

    refmap = {n.key: nid for nid, n in ix.nodes.items()}
    back = m.remap_ids(kx, refmap)
    assert back == ix
    # the input is not mutated, and the copy shares no mutable state with it
    assert kx.nodes["sem:82:448"].ref is None
    back.nodes["n22"].ids["x"] = 1
    back.nodes["n22"].facets["a11y"]["flags"].append("zzz")
    back.nodes["n22"].issues[0].evidence["x"] = 1
    back.nodes["n22"].b[0] = 99
    assert "x" not in ix.nodes["n22"].ids and "zzz" not in ix.nodes["n22"].facets["a11y"]["flags"]
    assert "x" not in ix.nodes["n22"].issues[0].evidence and ix.nodes["n22"].b[0] == 0


def test_index_helpers():
    ix = launcher_index()
    assert ix.get("n22") is ix.get("sem:82:448")
    assert ix.get("sem:82:448").ref == "n22"
    assert ix.get("w:1").ref == "n1" and ix.resolve_id("view:82") == "n6"
    assert ix.get("nope") is None
    assert [w.ref for w in ix.windows()] == ["n1"]
    assert [a.ref for a in ix.ancestors("n15")][:3] == ["n10", "n23", "n7"]
    assert [n.ref for n in ix.children_of("n10")] == [it[0] for it in LAUNCHER_ITEMS]
    assert [n.ref for n, d in ix.walk(root="n10", max_depth=0)] == ["n10"]
    assert list(ix.walk(root="nope")) == []
    ix.by_key = {}
    ix.rebuild_by_key()
    assert ix.by_key["sem:82:448"] == "n22"


# --------------------------------------------------------------------------- builders
def test_launcher_builder_matches_the_spec_examples():
    ix = launcher_index()
    assert ix.meta.id == "c7h2kq" and ix.meta.lineage == ("emulator-5554",
                                                           "com.oberkfell.a11yprobe")
    assert ix.meta.device["dpi"] == 480 and ix.meta.device["screen"] == [1280, 2856]
    refs = {f"n{i}" for i in range(1, 26)} | {"n301", "n302", "n303", "n304", "n305"}
    assert set(ix.nodes) == refs
    kinds = {k: sum(1 for n in ix.nodes.values() if n.kind == k) for k in m.KINDS}
    assert kinds == {"view": 8, "compose": 17, "slot": 5, "a11y": 0}  # capture(): views 8, compose 17

    n1 = ix.nodes["n1"]
    assert n1.is_window and n1.z == 0 and n1.type == "DecorView" and n1.b == [0, 0, 1280, 2856]
    assert ix.trees["ui"].roots == ["n1"]
    assert ix.trees["ui"].children["n1"] == ["n2", "n24", "n25"]
    assert [ix.nodes[r].type for r in ("n2", "n4", "n5", "n6")] == [
        "LinearLayout", "FrameLayout", "ComposeView", "AndroidComposeView"]
    assert ix.nodes["n4"].rid == "content"
    assert ix.nodes["n9"].type == "TextView" and ix.nodes["n9"].label == "A11yProbe"
    assert ix.nodes["n9"].b == [48, 210, 322, 84]

    n10 = ix.nodes["n10"]
    assert n10.tag == "launcher_list" and n10.flags == ["scroll"] and n10.b == [0, 348, 1280, 2436]
    assert n10.children == [it[0] for it in LAUNCHER_ITEMS]
    for ref, sid, tag, label, y, h in LAUNCHER_ITEMS:
        n = ix.nodes[ref]
        assert (n.tag, n.label, n.b, n.flags) == (tag, label, [0, y, 1280, h], ["click"])
        assert n.key == f"sem:82:{sid}" and n.sel == f"@{tag}" and n.window == "n1"

    n22 = ix.nodes["n22"]
    assert n22.declared_b == [0, 2757, 1280, 216] and n22.visible == 0.125
    assert n22.ids == {"sem": "82:448", "a11y": "82:448"}
    assert n22.conf == {"compose": "exact", "a11y": "exact", "slot": "inferred"}
    assert n22.stop == 13 and n22.facets["a11y"]["flags"] == ["click", "focus"]
    assert n22.facets["a11y"]["actions"] == ["CLICK"]
    assert n22.facets["compose"]["actions"] == ["OnClick", "RequestFocus", "GetTextLayoutResult"]
    assert n22.facets["compose"]["slots"] == ["n301", "n302", "n305"]
    assert [i.id for i in n22.issues] == [ROLE_RULE, CLIPPED_RULE, TOUCH_RULE]
    assert ix.nodes["n24"].rid == "navigationBarBackground"
    assert ix.nodes["n25"].rid == "statusBarBackground" and "hidden" in ix.nodes["n25"].flags

    # find(text="state", flags=["click"]) -> n15, n16 in n10; path n1 > ... > n10 > n15
    hits = [n.ref for n in ix.nodes.values()
            if "click" in n.flags and "state" in (n.label or "").lower()]
    assert hits == ["n15", "n16"]
    assert {"n1", "n6", "n10"} <= {a.ref for a in ix.ancestors("n15")}

    # lint(): 14 warnings = 12 role + 1 state + 1 touch_target; issues "1 clipped: n22"
    by_rule = {}
    for n in ix.nodes.values():
        for i in n.issues:
            by_rule.setdefault(i.id, []).append(n.ref)
    assert {k: len(v) for k, v in by_rule.items()} == {
        ROLE_RULE: 12, STATE_RULE: 1, TOUCH_RULE: 1, CLIPPED_RULE: 1}
    assert by_rule[STATE_RULE] == ["n11"] and by_rule[CLIPPED_RULE] == ["n22"]

    # reading order: 13 stops, "A11yProbe" first, n22 last
    assert ix.reading == ["n9"] + [it[0] for it in LAUNCHER_ITEMS]

    # slot tree (spec 5.7): ListItem > Surface > Text, Text; divider; app vs library origin
    slots = ix.trees["slots"]
    assert slots.roots == ["n301", "n304"]
    assert slots.children == {"n301": ["n303"], "n303": ["n302", "n305"]}
    assert ix.nodes["n303"].origin == "library" and ix.nodes["n302"].origin == "app"
    assert ix.nodes["n302"].src == "MainActivity.kt:151" and ix.nodes["n302"].window == "n1"
    assert ix.nodes["n302"].facets["slot"]["sem"] == ["n22"]

    views = ix.trees["views"]
    assert [r for r, _ in views.walk()] == ["n1", "n2", "n3", "n4", "n5", "n6", "n24", "n25"]
    assert "n6" not in views.children  # the ACV's children are semantics nodes, not views
    assert sum(1 for _ in ix.trees["compose"].walk()) == 17
    assert ix.trees["compose"].roots == ["n7"]


def test_builder_rejects_bad_structure():
    b = IndexBuilder()
    w = b.window("n1")
    with pytest.raises(ValueError):
        b.view(w, "n1", "View")  # duplicate ref
    with pytest.raises(KeyError):
        b.view("n77", "n2", "View")
    with pytest.raises(ValueError):
        b.compose(w, sem_id=1)  # no AndroidComposeView above
    auto = b.view(w, None, "View")
    assert auto == "n2"


def test_wide_and_big_indexes():
    ix = wide_index()
    assert len(ix) == 259
    leaves = [n for n in ix.nodes.values() if n.type == "TextView"]
    assert len(leaves) == 216 and all(n.label == f"Label {n.ref[1:]}" for n in leaves)
    assert ix.nodes["n1"].is_window and ix.nodes["n2"].b == [2, 6, 300, 60]
    t0 = time.perf_counter()
    big = big_index(5000)
    assert len(big) == 5000 and time.perf_counter() - t0 < 5
    assert sum(1 for _ in big.walk()) == 5000
    assert m.index_from_jsonl(m.index_to_jsonl(big, compress=True), big.meta) == big


# --------------------------------------------------------------------------- import + packaging
def test_import_does_not_pull_protobuf_or_pillow():
    code = ("import sys, inspector_widget.capture as c; "
            "bad=[m for m in sys.modules if m.startswith(('google.protobuf','PIL'))]; "
            "print(bad); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-c", code], cwd=HOST_DIR, capture_output=True,
                       text=True, env=dict(os.environ, PYTHONPATH=HOST_DIR), check=False)
    assert r.returncode == 0, r.stdout + r.stderr


def _pyproject_packages():
    path = os.path.join(HOST_DIR, "pyproject.toml")
    try:
        import tomllib  # Python 3.11+
        with open(path, "rb") as f:
            return tomllib.load(f)["tool"]["setuptools"]["packages"]
    except ModuleNotFoundError:  # Python 3.10
        with open(path, encoding="utf-8") as f:
            text = f.read()
        block = re.search(r"^packages\s*=\s*\[(.*?)\]", text, re.MULTILINE | re.DOTALL).group(1)
        return re.findall(r'"([^"]+)"', block)


def test_every_subpackage_ships_in_the_wheel():
    listed = set(_pyproject_packages())
    pkg_root = os.path.join(HOST_DIR, "inspector_widget")
    found = set()
    for dirpath, dirnames, filenames in os.walk(pkg_root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        if "__init__.py" in filenames:
            rel = os.path.relpath(dirpath, HOST_DIR).replace(os.sep, ".")
            found.add(rel)
    assert "inspector_widget.capture" in found
    assert found <= listed, f"packages missing from pyproject: {sorted(found - listed)}"
