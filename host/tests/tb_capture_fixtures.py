"""Captures of the TalkBack corpus, built offline from the recorded walks' start dumps.

``tests/data/tb_walks/<scenario>-<variant>-<kind>.a11y.pb.gz`` is the accessibility tree
each recorded walk started from (TalkBack 17.0 on emulator-5556). A capture needs a View
spine too, which the recordings do not hold, so :func:`raw_from_a11y` derives one from the
accessibility tree: every real View in it (``virtual_id == -1``; the agent sends every
VISIBLE View, important for accessibility or not) becomes a ViewNode with its class, bounds
and resource id, in the same nesting. A View under a Compose node (an AndroidView holder)
hangs under the nearest View above it, as the Compose host's AndroidViewsHandler would hold
it. There is no Compose semantics facet, so Compose nodes enter the index as accessibility
nodes, keyed by their (host, virtual) ids exactly as in a live capture.

What this cannot stand in for: View properties (visibility, alpha), the slot table (no
``src``), screenshots. The TalkBack model reads only the accessibility tree, so every
``tb.*`` rule and the reading view run on these exactly as on a live capture.

:func:`corpus_capture` returns ``(index, raw)`` for one corpus entry; :func:`fixture_capture`
the same for a recorded live capture fixture (``tests/fixtures/captures``).
"""

from __future__ import annotations

import gzip
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from fakescenes import Strings

from inspector_widget.capture import analyzers, index as cindex
from inspector_widget.capture.model import CaptureMeta, Index, RawCapture
from inspector_widget.proto import view_inspection_pb2 as pb
from inspector_widget.strings import StringResolver

DATA = Path(__file__).parent / "data"
WALKS = DATA / "tb_walks"
EXPECTED = json.loads((DATA / "tb_corpus_expected.json").read_text())
WALK_ENTRIES = [e for e in EXPECTED["entries"] if e.get("kind", "walk") == "walk"]
PACKAGE = "com.oberkfell.a11yprobe"


def entry_id(e: dict[str, Any]) -> str:
    return f"{e['scenario']}-{e['variant']}-{e.get('kind', 'walk')}"


def load_walk(e: dict[str, Any] | str) -> tuple[dict[str, Any], pb.DumpA11yResponse]:
    """The recorded walk record and the a11y dump it started from."""
    base = WALKS / (e if isinstance(e, str) else entry_id(e))
    rec = json.loads(gzip.decompress(base.with_name(base.name + ".json.gz").read_bytes()))
    resp = pb.DumpA11yResponse.FromString(
        gzip.decompress(base.with_name(base.name + ".a11y.pb.gz").read_bytes()))
    return rec, resp


#: The View hierarchy the accessibility dump leaves out, from the scenario source: the
#: containers a not-important View hoists away hold the drawing order a same-window overlay
#: depends on. ``{entry prefix: {parent View id: [kept View id | [View ids of a synthetic,
#: not-important container], ...]}}``, children in drawing (child) order.
SPINES: dict[str, dict[int, list[Any]]] = {
    # TbViewActivity.v5Scrim: FrameLayout[background column, scrim, card]
    "tb_v5-bad": {17: [[3, 4, 5, 6, 7, 8, 9, 10, 11], 12, [14, 15, 16]]},
    # TbHybrid.h5FragmentOverlay: FrameLayout[the Fragment's View column, ComposeView]
    "tb_h5-bad": {2: [[10, 11, 12, 13, 14, 15, 16, 17, 18], 22]},
}
_SYNTHETIC = 900_000


def _view_kids(a: Any) -> list[Any]:
    """The Views under ``a`` (through virtual nodes: AndroidView holders hang on the View
    above them, as the host's AndroidViewsHandler holds them)."""
    out = []
    stack = list(reversed(a.children))
    while stack:
        c = stack.pop()
        if int(c.virtual_id) == -1 and int(c.host_view_id):
            out.append(c)
        else:
            stack.extend(reversed(c.children))
    return out


def _view_from(a: Any, res: StringResolver, st: Strings, out: pb.ViewNode,
               spine: dict[int, list[Any]] | None = None) -> None:
    out.id = int(a.host_view_id)
    cls = res.opt(a.class_name) or "android.view.View"
    out.class_name = st.id(cls.rsplit(".", 1)[-1])
    if "." in cls:
        out.package_name = st.id(cls.rsplit(".", 1)[0])
    b = a.bounds.layout
    out.bounds.layout.x, out.bounds.layout.y = b.x, b.y
    out.bounds.layout.w, out.bounds.layout.h = b.w, b.h
    rid = res.opt(a.view_id_resource_name)
    if rid:
        out.view_id_name = st.id(rid.rsplit("/", 1)[-1])
    if "WebView" in cls:
        out.flags |= pb.ViewNode.IS_WEBVIEW
    kids = _view_kids(a)
    order = (spine or {}).get(int(a.host_view_id))
    if not order:
        for c in kids:
            _view_from(c, res, st, out.children.add(), spine)
        return
    by_id = {int(c.host_view_id): c for c in kids}
    placed: set[int] = set()
    for item in order:
        if isinstance(item, list):
            box = out.children.add()
            box.id = _SYNTHETIC + item[0]
            box.class_name = st.id("LinearLayout")
            members = [by_id[i] for i in item if i in by_id]
            if members:
                x0 = min(m.bounds.layout.x for m in members)
                y0 = min(m.bounds.layout.y for m in members)
                x1 = max(m.bounds.layout.x + m.bounds.layout.w for m in members)
                y1 = max(m.bounds.layout.y + m.bounds.layout.h for m in members)
                box.bounds.layout.x, box.bounds.layout.y = x0, y0
                box.bounds.layout.w, box.bounds.layout.h = x1 - x0, y1 - y0
            for m in members:
                _view_from(m, res, st, box.children.add(), spine)
                placed.add(int(m.host_view_id))
        elif item in by_id:
            _view_from(by_id[item], res, st, out.children.add(), spine)
            placed.add(item)
    for c in kids:
        if int(c.host_view_id) not in placed:
            _view_from(c, res, st, out.children.add(), spine)


def raw_from_a11y(resp: pb.DumpA11yResponse, *, cid: str = "ctbfix",
                  package: str = PACKAGE, dpi: int = 420,
                  spine: dict[int, list[Any]] | None = None) -> RawCapture:
    """A RawCapture holding ``resp`` plus a View spine and window list derived from it
    (``spine``: the containers the dump leaves out, see :data:`SPINES`)."""
    res = StringResolver(resp.strings)
    st = Strings()
    views = pb.DumpTreeResponse()
    wins = pb.GetWindowsResponse()
    for w in resp.windows:
        if not w.HasField("root"):
            continue
        wins.root_ids.append(int(w.root_view_id))
        _view_from(w.root, res, st, views.roots.add(), spine)
    st.fill(views.strings)
    meta = CaptureMeta(id=cid, lineage=("emulator-5556", package),
                       device={"dpi": dpi, "font_scale": 1.0})
    return RawCapture(meta=meta, windows=wins.SerializeToString(),
                      views=views.SerializeToString(), a11y=resp.SerializeToString())


def build(raw: RawCapture, lint: str = "tree") -> Index:
    ix = cindex.build_index(raw)
    analyzers.analyze(ix, raw, lint=lint)
    return ix


@lru_cache(maxsize=None)
def corpus_capture(eid: str) -> tuple[Index, RawCapture]:
    """``(index, raw)`` of one corpus entry's start screen (key space, analyzed)."""
    _rec, resp = load_walk(eid)
    spine = next((v for k, v in SPINES.items() if eid.startswith(k + "-")), None)
    raw = raw_from_a11y(resp, cid=("c" + eid.replace("_", "").replace("-", ""))[:12],
                        spine=spine)
    return build(raw), raw


#: Live captures of the corpus screens (TalkBack off, props off, no screenshots), recorded on
#: emulator-5556 by the live stage: ``tb_<scenario>_<variant>[_slots]``.
LIVE = Path(__file__).parent / "fixtures" / "tb_captures"


def live_names() -> list[str]:
    return sorted(p.name for p in LIVE.iterdir() if (p / "meta.json").is_file())


@lru_cache(maxsize=None)
def live_capture(name: str) -> tuple[Index, RawCapture]:
    """``(index, raw)`` of a live corpus capture (``tests/fixtures/tb_captures/<name>``)."""
    return fixture_capture(str(LIVE / name))


@lru_cache(maxsize=None)
def fixture_capture(name: str) -> tuple[Index, RawCapture]:
    """``(index, raw)`` of a recorded live capture (``tests/fixtures/captures/<name>``, or
    a fixture directory)."""
    import capture_replay as cr

    rec = cr.load(name)
    meta = CaptureMeta.from_dict(rec.meta)
    raw = RawCapture(meta=meta, windows=rec.raw.get("windows", b""),
                     views=rec.raw.get("views", b""),
                     compose_sem=rec.raw.get("compose_sem", b""), slots=rec.raw.get("slots"),
                     a11y=rec.raw.get("a11y", b""), shots=dict(rec.shots))
    return build(raw), raw
