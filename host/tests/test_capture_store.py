"""Offline tests for inspector_widget.capture.store (spec section 4, WP C2).

Covers ids and atomic publish, the shared resolver, labels, pins, drop, derived
artifacts, retention and GC with a fake clock, crash recovery, concurrency across
processes, memory-only mode, the memory LRU and ref-counter monotonicity.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import multiprocessing
import os
import random
import re
import stat
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from inspector_widget.capture import store as S
from inspector_widget.capture.model import (
    CaptureMeta,
    Index,
    OpError,
    RawCapture,
    Tree,
    UNode,
    index_to_jsonl,
    view_key,
)
from inspector_widget.capture.store import CaptureStore

HOST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERIAL = "emulator-5554"
APP = "com.oberkfell.a11yprobe"
OTHER = "com.example.other"
ID_RE = re.compile(r"^c[0-9abcdefghjkmnpqrstvwxyz]{5}$")
REFS_PER = 3


class Clock:
    """A settable wall clock that starts at real time (so real mtimes compare)."""

    def __init__(self) -> None:
        self.t = time.time()

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


# --------------------------------------------------------------------------- helpers
def payload(lineage=(SERIAL, APP), tag: str = "x", first_ref: int = 1, n: int = REFS_PER,
            views_size: int = 512, shots=(1,), **meta_kw):
    """A small capture: RawCapture with checksummed views, a chain index of ``n``
    refs starting at ``first_ref`` and its refmap."""
    views = hashlib.sha256(tag.encode()).digest() * (views_size // 32)
    meta = CaptureMeta(id="", lineage=lineage, pid=4312,
                       diagnostics=[hashlib.sha256(views).hexdigest()], **meta_kw)
    raw = RawCapture(meta=meta, windows=b"\x08\x01", views=views, compose_sem=b"c", a11y=b"a",
                     shots={r: b"shot-%d" % r for r in shots})
    nodes, children, refmap = {}, {}, {}
    prev = None
    for i in range(n):
        ref = f"n{first_ref + i}"
        key = view_key(1000 + i)
        nodes[ref] = UNode(key=key, ref=ref, parent=prev, depth=i, label=f"{tag} {i}",
                           z=0 if i == 0 else None, window=f"n{first_ref}")
        if prev:
            children[prev] = [ref]
            nodes[prev].children = [ref]
        refmap[key] = ref
        prev = ref
    roots = [f"n{first_ref}"] if n else []
    ix = Index(meta=meta, nodes=nodes, trees={"ui": Tree(roots=roots, children=children)},
               by_key=dict(refmap))
    return raw, ix, refmap


def publish(store: CaptureStore, lineage=(SERIAL, APP), tag: str = "x", n: int = REFS_PER,
            **kw) -> str:
    with store.refs_lock():
        first = store.next_refs(n)
        raw, ix, refmap = payload(lineage, tag, first, n, **kw)
        return store.publish(raw, ix, refmap)


def make_store(tmp_path, clock=None, **kw) -> CaptureStore:
    kw.setdefault("durable", False)
    kw.setdefault("gc_on_publish", False)
    return CaptureStore(root=str(tmp_path / "store"), persist=True, clock=clock or Clock(), **kw)


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def read_json(path):
    with open(path, "rb") as f:
        return json.loads(f.read())


def read_bytes(path) -> bytes:
    with open(path, "rb") as f:
        return f.read()


# --------------------------------------------------------------------------- publish / layout
def test_publish_layout_ids_and_modes(tmp_path):
    store = make_store(tmp_path, durable=True)
    cid = publish(store)
    assert ID_RE.match(cid)
    root = store.root
    cdir = os.path.join(root, "captures", cid)
    names = set(os.listdir(cdir))
    assert {"meta.json", "refmap.json", "index.jsonl.gz", ".used", ".complete", "raw",
            "shot"} <= names
    assert sorted(os.listdir(os.path.join(cdir, "raw"))) == \
        ["a11y.pb", "compose_sem.pb", "views.pb", "windows.pb"]
    assert os.listdir(os.path.join(cdir, "shot")) == ["w_1.pb"]
    assert os.listdir(os.path.join(root, ".staging")) == []
    for dirpath, dirs, files in os.walk(root):
        assert mode(dirpath) == 0o700, dirpath
        for f in files:
            assert mode(os.path.join(dirpath, f)) == 0o600, f
    assert read_json(os.path.join(root, "store.json")) == {"schema": 1, "next_ref": 1 + REFS_PER}
    meta = read_json(os.path.join(cdir, "meta.json"))
    assert meta["id"] == cid and meta["lineage"] == {"serial": SERIAL, "package": APP}
    with gzip.open(os.path.join(cdir, "index.jsonl.gz"), "rb") as f:
        assert json.loads(f.readline())["capture"] == cid


def test_publish_links_lineage_and_session(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    a = publish(store, tag="a")
    clock.advance(5)
    b = publish(store, tag="b")
    other = publish(store, (SERIAL, OTHER), tag="o")
    st = store.lineage_state(SERIAL, APP)
    assert st.latest == b and st.history == [b, a]
    assert store.load(b).meta.prev == a and store.load(a).meta.prev is None
    assert store.load(b).meta.created_at == clock.t
    assert store.default_session() == (SERIAL, OTHER)
    store.set_default_session(SERIAL, APP)
    assert store.default_session() == (SERIAL, APP)
    assert set(store.lineages()) == {(SERIAL, APP), (SERIAL, OTHER)}
    assert store.load(other).meta.prev is None


def test_publish_sets_meta_id_and_keeps_caller_prev(tmp_path):
    store = make_store(tmp_path)
    first = publish(store)
    with store.refs_lock():
        raw, ix, refmap = payload(tag="y", first_ref=store.next_refs(3), prev="c00000")
        cid = store.publish(raw, ix, refmap)
    assert raw.meta.id == cid
    assert store.load(cid).meta.prev == "c00000" != first


def test_load_reads_facets_index_and_counts(tmp_path):
    store = make_store(tmp_path)
    with store.refs_lock():
        raw, ix, refmap = payload(tag="L", first_ref=store.next_refs(4), n=4, shots=(1, 2))
        raw.skp = {1: b"skiapict" + b"\x6d\x00\x00\x00" + b"body"}
        cid = store.publish(raw, ix, refmap)
    loaded = store.load(cid.upper())
    assert loaded.id == cid and loaded.meta.id == cid
    assert loaded.raw("views") == raw.views and loaded.raw("slots") is None
    assert loaded.shot(2) == b"shot-2" and loaded.shot(9) is None
    assert loaded.shot_roots() == [1, 2] and loaded.skp_roots() == [1]
    assert loaded.skp(1) == raw.skp[1]
    assert loaded.refmap() == refmap
    assert loaded.node_count() == 4
    assert loaded.raw_capture().files() == raw.files()
    store2 = CaptureStore(root=store.root, persist=True, durable=False)  # cold: reads the file
    ix2 = store2.load(cid).index()
    assert index_to_jsonl(ix2) == index_to_jsonl(ix)
    assert store2.load(cid).node_count() == 4
    with pytest.raises(OpError) as e:
        loaded.raw("nope")
    assert e.value.code == "bad_args"
    with pytest.raises(OpError) as e:
        store.load("latest")
    assert e.value.code == "bad_args"


def test_used_marker_touched_at_most_once_a_minute(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    cid = publish(store)
    used = os.path.join(store.capture_dir(cid), ".used")
    t0 = os.stat(used).st_mtime
    assert t0 == pytest.approx(clock.t)
    clock.advance(30)
    store.load(cid)
    assert os.stat(used).st_mtime == t0
    clock.advance(31)
    store.load(cid)
    assert os.stat(used).st_mtime == pytest.approx(clock.t)
    meta_mtime = os.stat(os.path.join(store.capture_dir(cid), "meta.json")).st_mtime
    clock.advance(3600)
    store.load(cid)
    assert os.stat(os.path.join(store.capture_dir(cid), "meta.json")).st_mtime == meta_mtime


def test_id_collision_between_staging_and_rename_re_ids(tmp_path, monkeypatch):
    ids = iter(["c33333", "c44444", "c55555"])
    store = make_store(tmp_path, new_id=lambda: next(ids))
    real = CaptureStore._write_id_files
    calls = []

    def racing(self, staging, raw, ix):
        if not calls:  # another process publishes c33333 right now
            os.makedirs(os.path.join(self.root, "captures", "c33333", "raw"))
        calls.append(raw.meta.id)
        return real(self, staging, raw, ix)

    monkeypatch.setattr(CaptureStore, "_write_id_files", racing)
    cid = publish(store)
    assert cid == "c44444" and calls == ["c33333", "c44444"]
    assert read_json(os.path.join(store.capture_dir(cid), "meta.json"))["id"] == cid
    with gzip.open(os.path.join(store.capture_dir(cid), "index.jsonl.gz"), "rb") as f:
        assert json.loads(f.readline())["capture"] == cid
    assert os.listdir(os.path.join(store.root, "captures", "c33333")) == ["raw"]
    assert os.listdir(os.path.join(store.root, ".staging")) == []


def test_fresh_ids_skip_existing_captures(tmp_path):
    ids = iter(["c00001", "c00001", "c00002"])
    store = make_store(tmp_path, new_id=lambda: next(ids))
    assert publish(store) == "c00001"
    assert publish(store) == "c00002"


def test_new_capture_id_is_crockford():
    seen = {S.new_capture_id() for _ in range(2000)}
    assert all(ID_RE.match(c) for c in seen)
    assert len(seen) > 1990
    assert not set("".join(seen)) & set("ilou")


# --------------------------------------------------------------------------- resolve
def test_resolve_latest_prev_and_nth(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    a1 = publish(store, tag="a1")
    clock.advance(1)
    o1 = publish(store, (SERIAL, OTHER), tag="o1")
    clock.advance(1)
    a2 = publish(store, tag="a2")
    clock.advance(1)
    a3 = publish(store, tag="a3")
    app = (SERIAL, APP)
    assert store.resolve() == store.resolve("latest") == store.resolve(None) == a3
    assert store.resolve("latest", app) == a3
    assert store.resolve("prev", app) == store.resolve("latest~1", app) == a2
    assert store.resolve("latest~2", app) == a1
    assert store.resolve("latest~0", app) == a3
    assert store.resolve("latest", (SERIAL, OTHER)) == o1
    # store-wide: newest created_at
    assert store.resolve("prev") == a2 and store.resolve("latest~2") == o1
    assert store.resolve("latest~3") == a1
    with pytest.raises(OpError) as e:
        store.resolve("latest~3", app)
    assert e.value.code == "capture_not_found" and "has 3" in e.value.message
    with pytest.raises(OpError) as e:
        store.resolve("latest", ("emulator-5556", APP))
    assert e.value.code == "capture_not_found" and "no captures yet" in e.value.message


def test_resolve_ids_are_case_insensitive_and_unknown_ids_give_the_ttl(tmp_path):
    store = make_store(tmp_path, ttl_s=6 * 3600)
    cid = publish(store)
    assert store.resolve(cid.upper()) == cid
    assert store.resolve(f"  {cid} ") == cid
    with pytest.raises(OpError) as e:
        store.resolve("c00000")
    assert e.value.code == "capture_not_found"
    assert "6h" in e.value.hint
    for bad in ("", "latest~x", "Bad Label", "c0000", "@", "~1"):
        if bad == "":
            assert store.resolve(bad) == cid  # empty means latest
            continue
        with pytest.raises(OpError) as e:
            store.resolve(bad)
        assert e.value.code in ("bad_args", "capture_not_found"), bad


def test_resolve_on_an_empty_store(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(OpError) as e:
        store.resolve()
    assert e.value.code == "capture_not_found"
    assert store.list() == []


def test_resolve_is_the_same_for_mcp_and_cli(tmp_path):
    """Two stores (the MCP server's and a CLI process) resolve identically."""
    clock = Clock()
    mcp = make_store(tmp_path, clock)
    ids = []
    for i, lin in enumerate([(SERIAL, APP), (SERIAL, OTHER), (SERIAL, APP), (SERIAL, APP)]):
        clock.advance(1)
        ids.append(publish(mcp, lin, tag=str(i)))
    mcp.label(ids[0], "before")
    mcp.label(ids[1], "only")
    specs = ["latest", "prev", "latest~2", "before", "@before", "only", ids[3].upper()]
    lineages = [None, [SERIAL, APP], [SERIAL, OTHER]]
    expected = {}
    for spec in specs:
        for lin in lineages:
            try:
                expected[f"{spec}|{lin}"] = mcp.resolve(spec, tuple(lin) if lin else None)
            except OpError as exc:
                expected[f"{spec}|{lin}"] = exc.code
    cli = CaptureStore(root=mcp.root, persist=True)
    for key, want in expected.items():
        spec, lin = key.split("|")
        lin = None if lin == "None" else tuple(json.loads(lin.replace("'", '"')))
        try:
            got = cli.resolve(spec, lin)
        except OpError as exc:
            got = exc.code
        assert got == want, key
    script = textwrap.dedent(f"""
        import json, sys
        sys.path.insert(0, {HOST_DIR!r})
        from inspector_widget.capture.store import CaptureStore
        from inspector_widget.capture.model import OpError
        store = CaptureStore(root={mcp.root!r}, persist=True)
        out = {{}}
        for spec in {specs!r}:
            for lin in {lineages!r}:
                try:
                    out[f"{{spec}}|{{lin}}"] = store.resolve(spec, tuple(lin) if lin else None)
                except OpError as exc:
                    out[f"{{spec}}|{{lin}}"] = exc.code
        print(json.dumps(out))
    """)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          check=True, timeout=60)
    assert json.loads(proc.stdout) == expected
    assert expected[f"latest|{[SERIAL, APP]}"] == ids[3]
    assert expected["before|None"] == ids[0]


# --------------------------------------------------------------------------- labels
def test_label_moves_and_reports_moved_from(tmp_path):
    store = make_store(tmp_path)
    a = publish(store, tag="a")
    b = publish(store, tag="b")
    assert store.label(a, "before") is None
    assert store.load(a).meta.label == "before"
    assert store.label(b, "@before") == a  # moved
    assert store.load(a).meta.label is None and store.load(b).meta.label == "before"
    assert store.label(b, "before") is None  # already there
    assert store.label(b, "after") is None  # one label per capture: 'before' goes
    assert store.lineage_state(SERIAL, APP).labels == {"after": b}
    assert store.label(b, None) is None
    assert store.lineage_state(SERIAL, APP).labels == {}
    assert [m.label for m in store.list()] == [None, None]


@pytest.mark.parametrize("name", ["c7h2kq", "cabcde", "latest", "prev", "Bad", "1st", "_x",
                                  "a" * 33, "has space", "a.b", ""])
def test_invalid_labels_are_rejected(tmp_path, name):
    store = make_store(tmp_path)
    cid = publish(store)
    if name == "":
        assert store.label(cid, name) is None  # empty removes
        return
    with pytest.raises(OpError) as e:
        store.label(cid, name)
    assert e.value.code == "bad_args"
    before = sorted(os.listdir(os.path.join(store.root, "captures")))
    with pytest.raises(OpError):
        publish(store, label=name)
    assert sorted(os.listdir(os.path.join(store.root, "captures"))) == before
    assert os.listdir(os.path.join(store.root, ".staging")) == []


def test_labels_are_unique_per_lineage(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    a = publish(store, tag="a", label="base")
    clock.advance(1)
    o = publish(store, (SERIAL, OTHER), tag="o", label="base")
    assert store.load(a).meta.label == "base" and store.load(o).meta.label == "base"
    assert store.resolve("base", (SERIAL, APP)) == a
    assert store.resolve("@base", (SERIAL, OTHER)) == o
    with pytest.raises(OpError) as e:
        store.resolve("base")
    assert e.value.code == "ambiguous"
    assert sorted(e.value.candidates) == sorted([f"{a} {SERIAL}/{APP}", f"{o} {SERIAL}/{OTHER}"])
    with pytest.raises(OpError) as e:
        store.resolve("base", ("emulator-5556", APP))  # not in that lineage: must be unique
    assert e.value.code == "ambiguous"
    clock.advance(1)
    a2 = publish(store, tag="a2", label="base")  # publish moves it within the lineage
    assert store.resolve("base", (SERIAL, APP)) == a2
    assert store.load(a).meta.label is None
    with pytest.raises(OpError) as e:
        store.resolve("nolabel")
    assert e.value.code == "capture_not_found"
    store.drop(o)
    assert store.resolve("base") == a2  # unique again


# --------------------------------------------------------------------------- pins / drop
def test_pin_cap_and_overlay(tmp_path):
    store = make_store(tmp_path, max_pinned=2)
    ids = [publish(store, tag=str(i)) for i in range(3)]
    store.pin(ids[0])
    store.pin(ids[1], True)
    store.pin(ids[1], True)  # idempotent
    with pytest.raises(OpError) as e:
        store.pin(ids[2])
    assert e.value.code == "bad_args" and sorted(e.value.candidates) == sorted(ids[:2])
    with pytest.raises(OpError):
        publish(store, pinned=True)
    store.pin(ids[0], False)
    store.pin(ids[2])
    assert {m.id: m.pinned for m in store.list()} == {ids[0]: False, ids[1]: True, ids[2]: True}
    assert store.load(ids[2]).meta.pinned


def test_drop_updates_lineage_and_is_final(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    a = publish(store, tag="a", label="keep")
    clock.advance(1)
    b = publish(store, tag="b")
    store.pin(b)
    store.drop(b.upper())  # pinned captures can be dropped explicitly
    assert not os.path.exists(store.capture_dir(b))
    assert os.listdir(os.path.join(store.root, ".trash")) == []
    st = store.lineage_state(SERIAL, APP)
    assert st.latest == a and st.history == [a]
    assert store.resolve("latest", (SERIAL, APP)) == a
    store.drop(a)
    st = store.lineage_state(SERIAL, APP)
    assert st.latest is None and st.labels == {} and st.history == []
    for bad in (a, "c00000"):
        with pytest.raises(OpError) as e:
            store.drop(bad)
        assert e.value.code == "capture_not_found"


def test_deleting_under_a_reader_raises_capture_not_found(tmp_path):
    writer = make_store(tmp_path)
    cid = publish(writer)
    reader = CaptureStore(root=writer.root, persist=True, durable=False)
    loaded = reader.load(cid)
    writer.drop(cid)
    for read in (lambda: loaded.raw("views"), lambda: loaded.shot(1), loaded.index,
                 loaded.refmap, loaded.shot_roots, lambda: loaded.derived("x.json"),
                 lambda: loaded.put_derived("x.json", b"{}"), lambda: reader.load(cid)):
        with pytest.raises(OpError) as e:
            read()
        assert e.value.code == "capture_not_found"
    # also when the index was hydrated before the drop
    cid2 = publish(writer)
    loaded2 = reader.load(cid2)
    loaded2.index()
    writer.drop(cid2)
    with pytest.raises(OpError) as e:
        loaded2.index()
    assert e.value.code == "capture_not_found"


def test_derived_artifacts(tmp_path):
    store = make_store(tmp_path)
    loaded = store.load(publish(store))
    p = loaded.put_derived("lint.3f9a12bc.json", b'{"n":1}')
    assert p == os.path.join(loaded.path, "derived", "lint.3f9a12bc.json")
    assert loaded.derived("lint.3f9a12bc.json") == b'{"n":1}'
    assert loaded.derived("derived/lint.3f9a12bc.json") == b'{"n":1}'
    img = loaded.put_derived("img/n22-p16.png", b"\x89PNG")
    assert img == loaded.derived_path("img/n22-p16.png") and read_bytes(img) == b"\x89PNG"
    loaded.put_derived("img/n22-p16.png", b"\x89PNG2")  # replaced atomically
    assert loaded.derived("img/n22-p16.png") == b"\x89PNG2"
    assert loaded.derived("out/missing.json") is None
    assert mode(os.path.join(loaded.path, "img")) == 0o700 and mode(img) == 0o600
    for bad in ("../x", "raw/views.pb", "img/../../x", "/abs", "img/", ".lock", "img/.hidden",
                "shot/w_1.pb"):
        with pytest.raises(OpError) as e:
            loaded.put_derived(bad, b"")
        assert e.value.code == "bad_args", bad
    assert not [n for n in os.listdir(os.path.join(loaded.path, "img")) if ".tmp-" in n]


# --------------------------------------------------------------------------- GC
def ids_on_disk(store) -> set[str]:
    return {m.id for m in store.list(limit=None)}


def test_gc_ttl_with_pin_exemption(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    a, b, c, p = (publish(store, tag=t) for t in "abcp")
    store.pin(p)
    clock.advance(23 * 3600)
    store.load(b)  # a use resets b's TTL
    clock.advance(2 * 3600)
    report = store.gc()
    assert sorted(r["id"] for r in report["removed"]) == sorted([a, c])
    assert {r["why"] for r in report["removed"]} == {"expired"}
    assert ids_on_disk(store) == {b, p}
    st = store.lineage_state(SERIAL, APP)
    assert set(st.history) == {b, p} and st.latest == p
    assert report["captures"] == 2


def test_gc_ttl_from_the_environment(tmp_path):
    clock = Clock()
    store = CaptureStore(root=str(tmp_path / "s"), persist=True, clock=clock, durable=False,
                         gc_on_publish=False, env={S.ENV_TTL: "6h"})
    assert store.ttl_s == 6 * 3600
    cid = publish(store)
    clock.advance(5 * 3600)
    assert store.gc()["removed"] == []
    clock.advance(1.1 * 3600)
    assert [r["id"] for r in store.gc()["removed"]] == [cid]
    assert S.env_ttl_s({S.ENV_TTL: "0"}) == S.MIN_TTL_S
    assert S.env_ttl_s({S.ENV_TTL: "junk"}) == S.DEFAULT_TTL_S
    assert S.env_ttl_s({S.ENV_TTL: "90m"}) == 5400
    assert S.env_max_captures({S.ENV_MAX: "12"}) == 12
    assert S.env_max_captures({S.ENV_MAX: "-1"}) == 200
    assert S.env_max_bytes({S.ENV_MAX_MB: "3"}) == 3 << 20
    assert S.env_max_bytes({}) == 1 << 30
    assert S.parse_duration("2d") == 172800 and S.parse_duration("45") == 45
    assert S.parse_duration("6 H") == 21600 and S.parse_duration("x") is None


def test_gc_per_lineage_cap_evicts_unlabeled_lru_first(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock, lineage_cap=3)
    ids = []
    for i in range(5):
        clock.advance(10)
        ids.append(publish(store, tag=str(i)))
    other = publish(store, (SERIAL, OTHER))
    store.label(ids[0], "oldest")  # the oldest is labeled, so it outlives unlabeled ones
    clock.advance(100)
    store.load(ids[1])  # recently used (a use older than a minute is recorded)
    report = store.gc()
    removed = [r["id"] for r in report["removed"]]
    assert sorted(removed) == sorted([ids[2], ids[3]])
    assert {r["why"] for r in report["removed"]} == {"lineage_cap"}
    assert ids_on_disk(store) == {ids[0], ids[1], ids[4], other}


def test_gc_total_cap_counts_pinned_but_evicts_unpinned(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock, max_captures=4)
    ids = []
    for i, lin in enumerate([(SERIAL, APP), (SERIAL, OTHER)] * 3):
        clock.advance(10)
        ids.append(publish(store, lin, tag=str(i)))
    store.pin(ids[0])
    store.label(ids[1], "keepme")
    report = store.gc()
    assert sorted(r["id"] for r in report["removed"]) == sorted([ids[2], ids[3]])
    assert {r["why"] for r in report["removed"]} == {"count_cap"}
    assert ids_on_disk(store) == {ids[0], ids[1], ids[4], ids[5]}


def _heavy(store, cid, img=40_000, skp=20_000):
    loaded = store.load(cid)
    loaded.put_derived("img/full.png", os.urandom(img))
    loaded.put_derived("out/export.jsonl", os.urandom(img // 4))
    loaded.put_derived("lint.aa.json", b"{}")
    with open(os.path.join(loaded.path, "raw", "skp_1.bin"), "wb") as f:
        f.write(os.urandom(skp))


def test_gc_byte_cap_strips_heavy_files_first(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    ids = []
    for i in range(4):
        clock.advance(10)
        ids.append(publish(store, tag=str(i), views_size=4096))
        _heavy(store, ids[-1])
    sizes = {c: store.load(c).nbytes() for c in ids}
    total = sum(sizes.values())
    heavy = 40_000 + 10_000 + 2 + 20_000
    store.max_bytes = total - heavy - 1000  # stripping two captures is enough
    store.pin(ids[0])  # pinned: never stripped
    report = store.gc()
    assert report["removed"] == []
    assert report["stripped"] == [ids[1], ids[2]]
    assert report["bytes"] <= store.max_bytes
    for cid in ids[1:3]:
        loaded = store.load(cid)
        assert loaded.stripped and loaded.skp(1) is None and loaded.derived("img/full.png") is None
        assert loaded.raw("views") is not None and loaded.shot(1) == b"shot-1"
    for cid in (ids[0], ids[3]):
        loaded = store.load(cid)
        assert not loaded.stripped and loaded.skp(1) is not None


def test_gc_byte_cap_deletes_after_stripping(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    ids = []
    for i in range(5):
        clock.advance(10)
        ids.append(publish(store, tag=str(i), views_size=8192))
        _heavy(store, ids[-1], img=4000, skp=2000)
    store.pin(ids[0])
    store.label(ids[1], "labeled")
    one = store.load(ids[4]).nbytes() - 4000 - 1000 - 2 - 2000
    store.max_bytes = int(2.5 * one)
    report = store.gc(keep={ids[4]})
    removed = [r["id"] for r in report["removed"]]
    assert {r["why"] for r in report["removed"]} == {"bytes"}
    # unlabeled LRU go first; the pinned and the kept one never go; the labeled one last
    assert removed == [ids[2], ids[3], ids[1]]
    assert ids_on_disk(store) == {ids[0], ids[4]}
    assert report["stripped"] == [ids[2], ids[3], ids[1]]  # stripping came first
    assert not store.load(ids[0]).stripped and not store.load(ids[4]).stripped


def test_gc_on_publish_runs_after_the_outermost_lock(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock, lineage_cap=2, gc_on_publish=True)
    a = publish(store, tag="a")
    clock.advance(1)
    b = publish(store, tag="b")
    clock.advance(1)
    with store.refs_lock():
        c = publish(store, tag="c")
        assert ids_on_disk(store) == {a, b, c}  # nothing collected under the lock
    assert ids_on_disk(store) == {b, c}
    # the fresh capture is never the victim, even when everything else is pinned
    store.pin(b)
    store.lineage_cap = 1
    clock.advance(1)
    d = publish(store, tag="d")
    assert d in ids_on_disk(store) and b in ids_on_disk(store)


def test_gc_lock_is_non_blocking(tmp_path):
    store = make_store(tmp_path)
    publish(store)
    lock = S._lock_for(os.path.join(store.root, "gc.lock"))
    held, release = threading.Event(), threading.Event()

    def holder():
        with lock:
            held.set()
            release.wait(10)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(10)
    try:
        assert store.gc() == {"skipped": "another gc is running"}
    finally:
        release.set()
        t.join()
    assert "removed" in store.gc()


def test_gc_purges_spill_trash_and_stale_staging(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    publish(store)
    spill = store.spill_dir()
    for name in ("old.json", "new.json"):
        with open(os.path.join(spill, name), "w") as f:
            f.write("{}")
    os.utime(os.path.join(spill, "old.json"), (clock.t - 7200, clock.t - 7200))
    os.mkdir(os.path.join(store.root, ".trash", "cxxxxx.1.aa"))
    fresh = os.path.join(store.root, ".staging", "cyyyyy.99999")
    os.mkdir(fresh)
    report = store.gc()
    assert report["spill_purged"] == 1 and os.listdir(spill) == ["new.json"]
    assert report["trash_purged"] == 1 and report["staging_purged"] == 0
    assert os.path.isdir(fresh)
    clock.advance(3601)
    assert store.gc()["staging_purged"] == 1 and not os.path.exists(fresh)


def test_publish_killed_before_rename_leaves_only_staging(tmp_path):
    root = str(tmp_path / "store")
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {HOST_DIR!r})
        from inspector_widget.capture import store as S
        from inspector_widget.capture.model import CaptureMeta, Index, RawCapture
        store = S.CaptureStore(root={root!r}, persist=True)
        real = os.rename
        def rename(src, dst):
            if ".staging" in src and "captures" in dst:
                os._exit(3)  # killed right before publishing
            return real(src, dst)
        S.os.rename = rename
        with store.refs_lock():
            store.next_refs(3)
            raw = RawCapture(meta=CaptureMeta(id="", lineage=("emulator-5554", "com.x")),
                             views=b"v" * 4096, shots={{1: b"s"}})
            store.publish(raw, Index(), {{}})
    """)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          timeout=60, check=False)
    assert proc.returncode == 3, proc.stderr
    assert os.listdir(os.path.join(root, "captures")) == []
    staging = os.listdir(os.path.join(root, ".staging"))
    assert len(staging) == 1 and re.match(r"^c[0-9a-z]{5}\.\d+$", staging[0])
    assert os.path.exists(os.path.join(root, ".staging", staging[0], ".complete"))
    clock = Clock()
    store = CaptureStore(root=root, persist=True, clock=clock)
    assert store.list() == [] and store.lineages() == []
    with pytest.raises(OpError):
        store.resolve()
    clock.advance(30 * 60)
    assert store.gc()["staging_purged"] == 0
    clock.advance(31 * 60)
    assert store.gc()["staging_purged"] == 1
    assert os.listdir(os.path.join(root, ".staging")) == []
    assert store.next_refs(0) == 4  # the refs it took stay taken


def test_gc_all_wipes_everything_including_the_counter(tmp_path):
    store = make_store(tmp_path)
    publish(store, label="x")
    publish(store, (SERIAL, OTHER))
    assert store.next_refs(0) == 7
    report = store.gc(all=True)
    assert report["all"] is True and report["removed"] == 2 and "n1" in report["note"]
    assert store.list() == [] and store.lineages() == [] and store.default_session() is None
    assert store.next_refs(1) == 1
    assert publish(store)


# --------------------------------------------------------------------------- refs
def test_refs_are_never_reused_without_gc_all(tmp_path):
    """Property test: across 1,000 random publish/drop/gc/pin/label/time steps the
    counter only ever moves forward and hands out each ref once."""
    rng = random.Random(20260930)
    clock = Clock()
    store = make_store(tmp_path, clock, lineage_cap=3, max_captures=6, ttl_s=3600,
                       gc_on_publish=True, max_pinned=2)
    handed: set[int] = set()
    expect_next = 1
    live_refs: dict[str, list[str]] = {}
    lineages = [(SERIAL, APP), (SERIAL, OTHER), ("emulator-5556", APP)]
    for step in range(1000):
        op = rng.choice(["publish"] * 6 + ["drop", "gc", "time", "pin", "unpin", "label", "load",
                                           "lose_counter_cache"])
        ids = sorted(ids_on_disk(store))
        if op == "publish":
            lin = rng.choice(lineages)
            n = rng.randint(0, 6)
            with store.refs_lock():
                first = store.next_refs(n)
                assert first == expect_next, step
                new = set(range(first, first + n))
                assert not new & handed, step
                handed |= new
                expect_next = first + n
                prev_id = store.lineage_state(*lin).latest
                carried = live_refs.get(prev_id, [])[: rng.randint(0, 3)]
                refs = carried + [f"n{i}" for i in range(first, first + n)]
                raw, ix, _ = payload(lin, str(step), 1, 0)
                refmap = {view_key(i): r for i, r in enumerate(refs)}
                cid = store.publish(raw, ix, refmap)
            live_refs[cid] = refs
        elif op == "drop" and ids:
            store.drop(rng.choice(ids))
        elif op == "gc":
            store.gc()
        elif op == "time":
            clock.advance(rng.choice([30, 600, 2400, 4000]))
        elif op == "pin" and ids:
            try:
                store.pin(rng.choice(ids))
            except OpError as e:
                assert e.code == "bad_args"
        elif op == "unpin" and ids:
            store.pin(rng.choice(ids), False)
        elif op == "label" and ids:
            store.label(rng.choice(ids), rng.choice(["a", "b", "base"]))
        elif op == "load" and ids:
            store.load(rng.choice(ids))
        elif op == "lose_counter_cache":
            # a second store instance (another process) sees the same counter
            other = CaptureStore(root=store.root, persist=True, durable=False, gc_on_publish=False)
            assert other.next_refs(0) == expect_next
    assert len(handed) == expect_next - 1
    json_next = read_json(os.path.join(store.root, "store.json"))["next_ref"]
    assert json_next == expect_next


def test_counter_recovers_above_every_ref_on_disk(tmp_path):
    store = make_store(tmp_path)
    publish(store, n=5)  # n1..n5
    with store.refs_lock():
        first = store.next_refs(3)  # n6..n8
        raw, ix, refmap = payload(tag="t", first_ref=first, n=3)
        store.publish(raw, ix, refmap, tomb={"n40": ["Text", "gone", "@x", "c00000"]})
    os.remove(os.path.join(store.root, "store.json"))
    assert store.next_refs(1) == 41
    with open(os.path.join(store.root, "store.json"), "w") as f:
        f.write("{not json")
    assert store.next_refs(0) >= 41


def test_publish_merges_tombstones(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "TOMB_CAP", 3)
    store = make_store(tmp_path)
    with store.refs_lock():
        raw, ix, refmap = payload(tag="a", first_ref=store.next_refs(3))
        store.publish(raw, ix, refmap, tomb={"n90": ["A", "a", "#a", "c0"],
                                             "n91": ["B", "b", "#b", "c0"]})
    st = store.lineage_state(SERIAL, APP)
    assert list(st.tomb) == ["n90", "n91"]
    with store.refs_lock():
        raw, ix, refmap = payload(tag="b", first_ref=store.next_refs(3))
        refmap["view:9"] = "n90"  # n90 is back (a carried ref leaves the tomb)
        store.publish(raw, ix, refmap, tomb={"n92": ["C", "c", "#c", "c1"],
                                             "n93": ["D", "", "", "c1"],
                                             "n94": ["E", "", "", "c1"]})
    st = store.lineage_state(SERIAL, APP)
    assert list(st.tomb) == ["n92", "n93", "n94"]  # capped, oldest first out
    st.tomb["n95"] = ["F", "", "", "c2"]
    with store.refs_lock():
        store.save_lineage_state(st)
    assert list(store.lineage_state(SERIAL, APP).tomb) == ["n93", "n94", "n95"]
    with pytest.raises(ValueError):
        store.save_lineage_state(S.LineageState())


# --------------------------------------------------------------------------- memory
def test_memory_only_mode_writes_nothing_under_the_configured_root(tmp_path):
    configured = tmp_path / "configured"
    env = {S.ENV_PERSIST: "0", "INSPECTOR_WIDGET_CAPTURE_DIR": str(configured)}
    store = CaptureStore(env=env, durable=False)
    assert store.memory_only and not store.persist
    assert store.configured_root == str(configured)
    cid = publish(store)
    store.load(cid).put_derived("img/x.png", b"png")
    assert store.spill_dir().startswith(store.root)
    assert not configured.exists()
    assert mode(store.root) == 0o700
    assert store.resolve() == cid
    root = store.root
    store.close()
    assert not os.path.exists(root)
    with pytest.raises(RuntimeError):
        store.refs_lock().__enter__()
    explicit = CaptureStore(root=str(configured), persist=False)
    publish(explicit)
    assert not configured.exists()
    explicit.close()
    assert S.env_persist({S.ENV_PERSIST: "off"}) is False and S.env_persist({}) is True


def test_memory_lru_keeps_eight_indexes(tmp_path):
    store = make_store(tmp_path)
    ids = [publish(store, tag=str(i)) for i in range(10)]
    assert store.cached_ids() == ids[2:]
    first = store.load(ids[0]).index()
    assert len(first.nodes) == REFS_PER
    assert store.cached_ids()[-1] == ids[0] and len(store.cached_ids()) == 8
    assert store.load(ids[0]).index() is first  # served from memory
    small = make_store(tmp_path / "small", mem_bytes=2 * REFS_PER * S.NODE_BYTES_EST)
    sids = [publish(small, tag=str(i)) for i in range(4)]
    assert small.cached_ids() == sids[2:]


def test_unreadable_index_is_rebuilt(tmp_path):
    calls = []

    def rebuild(raw, refmap):
        calls.append((raw.views[:4], dict(refmap)))
        _raw, ix, _ = payload(tag="rebuilt", n=2)
        return ix

    store = make_store(tmp_path, rebuild=rebuild)
    cid = publish(store)
    path = os.path.join(store.capture_dir(cid), "index.jsonl.gz")
    good = read_bytes(path)
    with open(path, "wb") as f:
        f.write(good[: len(good) // 2])  # truncated
    cold = CaptureStore(root=store.root, persist=True, rebuild=rebuild)
    ix = cold.load(cid).index()
    assert len(ix.nodes) == 2 and calls and calls[0][1] == store.load(cid).refmap()
    assert ix.meta.id == cid
    again = CaptureStore(root=store.root, persist=True, rebuild=rebuild)
    assert len(again.load(cid).index().nodes) == 2 and len(calls) == 1  # file rewritten
    def broken(raw, refmap):
        raise RuntimeError("no index builder")

    no_builder = CaptureStore(root=store.root, persist=True, rebuild=broken)
    with open(path, "wb") as f:
        f.write(b"garbage")
    with pytest.raises(OpError) as e:
        no_builder.load(cid).index()
    assert e.value.code == "capture_not_found" and "rebuilt" in e.value.message


def test_list_order_filter_limit_and_summary(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    ids = []
    for i in range(4):
        clock.advance(1)
        ids.append(publish(store, (SERIAL, APP) if i % 2 == 0 else (SERIAL, OTHER), tag=str(i)))
    store.label(ids[2], "mark")
    metas = store.list()
    assert [m.id for m in metas] == ids[::-1]
    assert [m.id for m in store.list((SERIAL, APP))] == [ids[2], ids[0]]
    assert [m.id for m in store.list(limit=2)] == [ids[3], ids[2]]
    assert metas[1].label == "mark"
    summary = store.summary()
    assert summary["captures"] == 4 and summary["bytes"] > 0 and summary["persist"]
    assert summary["ttl_s"] == S.DEFAULT_TTL_S


def test_file_lock_is_reentrant_and_exclusive_between_threads(tmp_path):
    store = make_store(tmp_path)
    order = []
    with store.refs_lock():
        with store.refs_lock():
            store.next_refs(1)

        def other():
            with store.refs_lock():
                order.append("other")

        t = threading.Thread(target=other)
        t.start()
        time.sleep(0.05)
        order.append("owner")
    t.join(5)
    assert order == ["owner", "other"]
    lock = S.FileLock(str(tmp_path / "x.lock"))
    with pytest.raises(RuntimeError):
        lock.release()


# --------------------------------------------------------------------------- concurrency
def _mp_publisher(root: str, proc_no: int, count: int, out_q) -> None:
    try:
        store = CaptureStore(root=root, persist=True, lineage_cap=10**6, max_captures=10**6,
                             gc_on_publish=False)
        lineage = (SERIAL, f"com.example.app{proc_no % 2}")
        results = []
        for i in range(count):
            with store.refs_lock():
                first = store.next_refs(REFS_PER)
                raw, ix, refmap = payload(lineage, f"{proc_no}:{i}", first)
                cid = store.publish(raw, ix, refmap)
            results.append((cid, first))
        out_q.put(("pub", proc_no, results))
    except BaseException as exc:  # noqa: BLE001 - reported to the parent
        out_q.put(("error", proc_no, repr(exc)))


def _mp_reader(root: str, stop, out_q) -> None:
    try:
        store = CaptureStore(root=root, persist=True, gc_on_publish=False)
        cdir = os.path.join(root, "captures")
        seen: set[str] = set()
        problems: list[str] = []
        scans = 0
        while True:
            done = stop.is_set()
            scans += 1
            try:
                names = os.listdir(cdir)
            except FileNotFoundError:
                names = []
            for name in names:
                if not os.path.exists(os.path.join(cdir, name, ".complete")):
                    problems.append(f"{name} visible without .complete")
                    continue
                if name in seen:
                    continue
                loaded = store.load(name)
                views = loaded.raw("views")
                if hashlib.sha256(views).hexdigest() != loaded.meta.diagnostics[0]:
                    problems.append(f"{name}: views checksum mismatch")
                if len(loaded.index().nodes) != REFS_PER or loaded.meta.id != name:
                    problems.append(f"{name}: index or meta mismatch")
                if loaded.shot(1) != b"shot-1":
                    problems.append(f"{name}: shot missing")
                seen.add(name)
            if done:
                break
        out_q.put(("read", len(seen), problems, scans))
    except BaseException as exc:  # noqa: BLE001 - reported to the parent
        out_q.put(("error", "reader", repr(exc)))


def test_four_processes_publish_concurrently(tmp_path):
    root = str(tmp_path / "store")
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    stop = ctx.Event()
    reader = ctx.Process(target=_mp_reader, args=(root, stop, q))
    reader.start()
    pubs = [ctx.Process(target=_mp_publisher, args=(root, i, 50, q)) for i in range(4)]
    for p in pubs:
        p.start()
    results = [q.get(timeout=120) for _ in pubs]
    for p in pubs:
        p.join(60)
    stop.set()
    read = q.get(timeout=60)
    reader.join(60)
    assert all(r[0] == "pub" for r in results), results
    assert read[0] == "read", read
    ids = [cid for r in results for cid, _first in r[2]]
    assert len(ids) == 200 and len(set(ids)) == 200
    ranges = sorted(first for r in results for _cid, first in r[2])
    assert ranges == list(range(1, 1 + 200 * REFS_PER, REFS_PER))  # disjoint and contiguous
    _tag, nseen, problems, scans = read
    assert problems == [] and nseen > 0 and scans > 1
    store = CaptureStore(root=root, persist=True)
    assert ids_on_disk(store) == set(ids)
    assert store.next_refs(0) == 1 + 200 * REFS_PER
    for lin in store.lineages():
        st = store.lineage_state(*lin)
        assert len(st.history) == S.HISTORY_CAP and len(set(st.history)) == S.HISTORY_CAP
        assert st.latest == st.history[0]
        times = [store.load(c).meta.created_at for c in st.history]
        assert times == sorted(times, reverse=True)
    assert os.listdir(os.path.join(root, ".staging")) == []


def test_a_failing_gc_never_fails_a_publish(tmp_path, monkeypatch):
    store = make_store(tmp_path, gc_on_publish=True)

    def broken(self, keep):
        raise PermissionError("read-only cache")

    monkeypatch.setattr(CaptureStore, "_gc", broken)
    cid = publish(store)
    assert store.exists(cid)
    with pytest.raises(PermissionError):
        store.gc()


def test_strip_and_put_derived_on_a_vanished_capture(tmp_path):
    store = make_store(tmp_path)
    cid = publish(store)
    loaded = store.load(cid)
    S._rmtree(store.capture_dir(cid))
    assert store._strip(cid) == 0
    with pytest.raises(OpError) as e:
        loaded.put_derived("img/x.png", b"png")
    assert e.value.code == "capture_not_found"


def test_importing_the_store_stays_light():
    code = ("import sys; import inspector_widget.capture.store; "
            "print(any(m.startswith(('google.protobuf', 'PIL')) for m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         cwd=HOST_DIR, timeout=60)
    assert out.stdout.strip() == "False"


# --------------------------------------------------------------------------- integration
def _real_capture(serial: str = SERIAL):
    """A fetched and indexed capture of the recorded View screen (C3 + C4)."""
    sys.path.insert(0, os.path.join(HOST_DIR, "tests"))
    import fakescenes as fs

    from inspector_widget.capture import fetch, index, refs

    session = fs.replay_scene("viewscreen").session(serial=serial, package=APP)
    raw = fetch.fetch(session, sleep=lambda _s: None)
    ix = index.build_index(raw)
    return raw, ix, index, refs


def test_loaded_capture_reads_view_properties_lazily(tmp_path):
    raw, ix, index, refs = _real_capture()
    store = make_store(tmp_path)
    with store.refs_lock():
        refmap, tomb = refs.assign(ix, None, same_pid=False, same_generation=False,
                                   alloc=store.next_refs)
        cid = store.publish(raw, index.apply_refs(ix, refmap), refmap, tomb=tomb)
    lc = CaptureStore(root=store.root, persist=True).load(cid)
    switch = next(n for n in lc.index().nodes.values() if n.rid == "badSwitch")
    props = lc.props(int(switch.ids["view"]))
    assert props["checked"] is True and props["text"] == "Notifications"
    assert lc.facet_reader().decoded == 1  # one PropertyGroup decoded, on demand
    assert lc.props(987654) is None


def test_publish_names_the_capture_that_minted_a_ref(tmp_path):
    raw, ix, index, refs = _real_capture()
    store = make_store(tmp_path)
    with store.refs_lock():
        refmap, tomb = refs.assign(ix, None, same_pid=False, same_generation=False,
                                   alloc=store.next_refs)
        assert {n.since for n in ix.nodes.values()} == {None}  # no id before publish
        cid = store.publish(raw, index.apply_refs(ix, refmap), refmap, tomb=tomb)
    cold = CaptureStore(root=store.root, persist=True).load(cid).index()
    assert {n.since for n in cold.nodes.values()} == {cid}


def test_default_rebuild_runs_the_analyzers(tmp_path):
    raw, ix, index, refs = _real_capture()
    from inspector_widget.capture import analyzers

    store = make_store(tmp_path)
    with store.refs_lock():
        refmap, tomb = refs.assign(ix, None, same_pid=False, same_generation=False,
                                   alloc=store.next_refs)
        ix = index.apply_refs(ix, refmap)
        analyzers.analyze(ix, raw)
        cid = store.publish(raw, ix, refmap, tomb=tomb)
    os.remove(os.path.join(store.capture_dir(cid), "index.jsonl.gz"))
    rebuilt = CaptureStore(root=store.root, persist=True).load(cid).index()
    assert rebuilt.reading == ix.reading and rebuilt.reading
    assert {k: n.issues for k, n in rebuilt.nodes.items()} == \
        {k: n.issues for k, n in ix.nodes.items()}
