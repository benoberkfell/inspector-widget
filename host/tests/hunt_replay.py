"""Replay the real-app hunt's TalkBack walks offline (docs/realapp-findings.md).

Two kinds of evidence live under ``tests/data/realapps/``:

* ``talkback17_hunt_walks.json.gz``: per screen, the dump a walk started from (an
  ``a11y_to_dict`` fixture) and what TalkBack did at each press (``[moved, key, label,
  said]``). :func:`replay` turns one entry into the walk record :mod:`talkback.walk`
  saves: the dump goes back to a DumpA11yResponse (:func:`to_proto`), each step is bound
  to the node the dump holds under its key (a node TalkBack auto-scrolled in is not in
  the start dump: that step has a key and its speech, nothing else), and the record is
  built by the walk's own code (``walk._build_records``, ``walk.Model``), so
  :func:`talkback.diff.analyze` classifies it exactly as it would the live walk.
* ``talkback17_hunt_records.json.gz``: the walk records the hunt itself saved (``tb-walk``
  on emulator-5558, ``~/Library/Caches/inspector-widget/walks/<id>.json``), trimmed of
  their findings and timings. Every step carries the box and container it had when it
  was read, recaptures included, so the checks that need the scrolled-in screen
  (auto-scroll along a grid row, interleaved cards) run on them: :func:`record`.

:func:`agreement` counts the presses where the TalkBack model (``talkback.simulate`` from
the walk's start) lands where TalkBack did, and with the same words where TalkBack's
speech was logged.
"""

from __future__ import annotations

import copy
import gzip
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from fakescenes import Strings, _encode_a11y

from inspector_widget import talkback as tb
from inspector_widget.proto import view_inspection_pb2 as pb
from inspector_widget.talkback import diff, walk

DATA = Path(__file__).parent / "data" / "realapps"
HUNT = json.loads(gzip.decompress((DATA / "talkback17_hunt_walks.json.gz").read_bytes()))
WALKS = json.loads(gzip.decompress((DATA / "talkback17_walks.json.gz").read_bytes()))
_INSETS = ("status_bars", "navigation_bars", "ime", "display_cutout")


@lru_cache(maxsize=None)
def _dump_bytes(name: str) -> bytes:
    return gzip.decompress((DATA / f"{name}.a11y.json.gz").read_bytes())


def dump(name: str) -> Dict[str, Any]:
    """A fresh copy of one realapps dump (``a11y_to_dict`` output)."""
    return json.loads(_dump_bytes(name))


def to_proto(d: Mapping[str, Any]) -> pb.DumpA11yResponse:
    """An ``a11y_to_dict`` result back to the DumpA11yResponse it came from, window info
    (title, type, flags, frame, insets) included; what a11y_to_dict derives (ids,
    ``speakable``, ``ignored``, ``order``) is derived again on the way back."""
    st = Strings()
    resp = pb.DumpA11yResponse()
    for w in d.get("windows") or []:
        win = resp.windows.add()
        win.root_view_id = int(w.get("root_view_id") or 0)
        if w.get("root") is not None:
            _encode_a11y(st, w["root"], win.root)
        if "z" in w or "frame" in w:
            info = win.info
            info.root_view_id = win.root_view_id
            info.title = st.id(w.get("title"))
            info.layout_title = st.id(w.get("layout_title"))
            info.window_type = int(w.get("window_type") or 0)
            flags = w.get("window_flags")
            if isinstance(flags, str):
                v = int(flags, 16) & 0xFFFFFFFF
                info.wm_flags = v - (1 << 32) if v & 0x80000000 else v
            frame = w.get("frame")
            if isinstance(frame, Mapping):
                info.frame.layout.x, info.frame.layout.y = int(frame["x"]), int(frame["y"])
                info.frame.layout.w, info.frame.layout.h = int(frame["w"]), int(frame["h"])
            info.z = int(w.get("z") or 0)
            info.has_window_focus = bool(w.get("has_window_focus"))
            info.display_id = int(w.get("display_id") or 0)
            for name in _INSETS:
                ins = (w.get("insets") or {}).get(name)
                if isinstance(ins, Mapping):
                    m = getattr(info, name)
                    m.left, m.top = int(ins.get("left", 0)), int(ins.get("top", 0))
                    m.right, m.bottom = int(ins.get("right", 0)), int(ins.get("bottom", 0))
                    m.visible = bool(ins.get("visible"))
    if d.get("diagnostics"):
        resp.diagnostics = str(d["diagnostics"])
    st.fill(resp.strings)
    return resp


def entry(name: str) -> Dict[str, Any]:
    """A walk entry of the hunt (``talkback17_hunt_walks``) or of the earlier real-app run
    (``talkback17_walks``)."""
    return copy.deepcopy(HUNT[name] if name in HUNT else WALKS[name])


def autoscrolled(e: Mapping[str, Any]) -> set:
    """Indices into ``steps`` where TalkBack auto-scrolled."""
    a = e.get("autoscroll")
    if a is None:
        return set()
    return set(a) if isinstance(a, list) else {int(a)}


def replay(name: str, *, analyze: bool = True) -> Dict[str, Any]:
    """The walk record of one hunt entry, as ``tb_walk`` saves it, analysed by
    :func:`diff.analyze` (``findings``, ``vs_model``) unless ``analyze`` is False."""
    e = entry(name)
    d = dump(e.get("dump", name))
    resp = to_proto(d)
    idx = walk.DumpIndex(resp)
    legacy = bool(idx.legacy)
    model = walk.Model()
    model.build(resp, legacy)
    direction = e.get("direction", "next")
    auto = autoscrolled(e)
    start_key = e["start"]["key"]
    steps: List[walk.Step] = [walk.Step(0, start_key, via="start", node=idx.nodes.get(start_key),
                                        index=idx)]
    tts: Dict[int, str] = {}
    no_moves = 0
    prev_node = steps[0].node
    for j, (moved, key, _label, said) in enumerate(e["steps"]):
        i = j + 1
        node = idx.nodes.get(key)
        if not moved:
            no_moves += 1
            steps.append(walk.Step(i, key, moved=False, edge=True, via="edge", node=node,
                                   index=idx))
            continue
        via = "wrap" if no_moves else ("autoscroll" if j in auto else "next")
        if via == "next" and node is not None and prev_node is not None \
                and node.window != prev_node.window:
            via = "window"
        no_moves = 0
        steps.append(walk.Step(i, key, via=via, node=node, index=idx if node else None,
                               scrolled="?" if via == "autoscroll" else None))
        if said is not None:
            tts[i] = said
        prev_node = node if node is not None else prev_node
    records = walk._build_records(steps, model, tts, lambda k: k if k is not None else "-")
    for s in records:
        if s.get("scrolled") == "?":
            s["scrolled"] = s.get("container") or "?"
    predicted = [{"key": p.key, "ref": p.key, "label": p.label, "speak": p.speak,
                  "bounds": list(p.bounds), "window": p.window, "cls": p.cls}
                 for p in model.stops]
    edge_at = next((s for s in steps if s.edge), None)
    last = None
    if edge_at is not None:
        last = next((s for s in reversed(steps[:edge_at.i]) if s.moved and s.node is not None),
                    None)
    rec: Dict[str, Any] = {
        "steps": records, "predicted": predicted, "model": model.source,
        "ended": e.get("ended") or _ended(e["steps"]), "direction": direction, "until": "steps",
        "cycle": [], "edge": walk._edge_info(idx, last.node, direction) if last else None,
        "density": int(e.get("density") or 480), "legacy_ids": legacy,
        "walk": e.get("walk") or name, "package": None,
    }
    if rec["ended"] == "wrap":
        rec["orphans"] = walk.orphan_text(idx, records, legacy)
    hints = walk.model_hints(resp)
    rec["hints"], rec["web_traps"] = hints["hints"], hints["web_traps"]
    if analyze:
        res = diff.analyze(rec)
        rec["findings"], rec["vs_model"] = res["findings"], res["vs_model"]
    return rec


def _ended(steps: List[Any]) -> str:
    """How an entry without ``ended`` ended: a wrap when a move after an edge reads a stop
    read before, else at its last press."""
    seen, edge = set(), False
    for moved, key, _label, _said in steps:
        if not moved:
            edge = True
        elif edge and key in seen:
            return "wrap"
        seen.add(key)
    return "max_steps"


@lru_cache(maxsize=None)
def _records() -> Dict[str, Any]:
    return json.loads(gzip.decompress((DATA / "talkback17_hunt_records.json.gz").read_bytes()))


def record(walk_id: str, *, analyze: bool = True) -> Dict[str, Any]:
    """A walk record the hunt saved (``talkback17_hunt_records``), re-analysed by this
    tree's :func:`diff.analyze` unless ``analyze`` is False."""
    rec = copy.deepcopy(_records()[walk_id])
    if analyze:
        res = diff.analyze(rec)
        rec["findings"], rec["vs_model"] = res["findings"], res["vs_model"]
    return rec


def record_ids() -> List[str]:
    return sorted(_records())


def codes(rec: Mapping[str, Any]) -> Dict[str, int]:
    """``{finding code: count}`` of an analysed record."""
    out: Dict[str, int] = {}
    for f in rec.get("findings") or []:
        out[f["code"]] = out.get(f["code"], 0) + 1
    return out


def agreement(name: str, upto: Optional[int] = None) -> Tuple[int, int, int, int]:
    """``(presses where the model lands where TalkBack did, presses compared, presses whose
    words agree, presses with logged speech compared)`` for one hunt entry, from the walk's
    start, up to the first auto-scroll (the model does not scroll) or ``upto``."""
    e = entry(name)
    tree = tb.build(dump(e.get("dump", name)))
    steps = e["steps"]
    auto = autoscrolled(e)
    stop = min(auto) if auto else len(steps)
    if upto is not None:
        stop = min(stop, upto)
    order = tb.simulate(tree, start=e["start"]["key"], direction=e.get("direction", "next"),
                        until="steps", max_steps=len(steps), keyboard=True)
    ms = list(order.steps)
    if order.ended == "trap" and ms:  # TalkBack stays put from here: every press a no-move
        ms += [dict(ms[-1]) for _ in range(len(steps) - len(ms))]
    keys = words = n = said_n = 0
    for (moved, key, _label, said), m in zip(steps[:stop], ms):
        n += 1
        mk = None if (m.get("edge") or m.get("stuck")) else m.get("key")
        ok = (mk == key) if moved else mk is None
        keys += ok
        if moved and said is not None:
            said_n += 1
            words += ok and m.get("speak") == said
    return keys, n, words, said_n


__all__ = ["DATA", "HUNT", "WALKS", "agreement", "autoscrolled", "codes", "dump", "entry",
           "record", "record_ids", "replay", "to_proto"]
