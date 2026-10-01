"""TalkBack walks and scenarios in the capture store (talkback-navigation.md parts 3.4, 4).

``tb_walk`` and ``tb_scenario`` (:mod:`inspector_widget.ops`) run against captures: the
one taken once TalkBack has started (the screen the walk begins on) and, during a walk,
recaptures when focus reaches nodes no capture holds yet (items TalkBack scrolled in; at
most one per three steps). This module does everything after the device part:

* **Binding** (:class:`Binding`): every step's accessibility node key becomes the
  capture's ref, through the capture's own dump (the same key), else (a Compose id
  re-minted when an item was re-created) the node of the same class and label whose box
  overlaps it (IoU >= 0.8). A walk therefore names the refs ``outline``, ``node`` and
  ``lint`` use, and carry-over keeps them across the recaptures.
* **Analysis** (:func:`bind_walk`): the walk record is re-analysed
  (:func:`inspector_widget.talkback.diff.analyze`) on the bound refs, so every finding
  names refs, and :func:`classify` sorts predicted vs actual into skip, double,
  out_of_order, loop, trap, escape, stuck, left_app ... by ref.
* **Results**: :func:`walk_result` (at most 5 KB at 60 steps) and
  :func:`scenario_result` (at most 1 KB), with their ``next`` hints.
* **Storage**: ``<store>/walks/<id>.json`` (``w`` + 6 for walks, ``t`` + 6 for
  scenarios), each with the capture ids it ran against; listed, read and dropped
  through ``captures(what="walks")`` / ``captures(action="show", id="w3f9ak")``, and
  drawn by ``image(overlay="walk")`` (:mod:`.images`).

Pure, apart from the files under ``<store>/walks``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..output import dumps, utf8_len
from .model import OpError

WALKS_DIR = "walks"
#: ``w3f9ak`` (a walk) or ``t3f9ak`` (a scenario).
ID_RE = re.compile(r"^[wt][0-9a-z]{6}$")
#: Newest records kept; older ones are deleted when a new one is saved.
KEEP = 100
KEEP_S = 7 * 86400
WALK_MAX_BYTES = 5000
SCENARIO_MAX_BYTES = 1000
WALK_MAX_LINES = 60
#: The steps of a walk between two recaptures (design 3.4: at most one per 3 steps).
RECAPTURE_EVERY = 3
SPEAK_LEN = 48
MAX_REFS = 6
NEXT_MAX_BYTES = 200

#: Finding code -> the class it counts under in a walk's ``diff``.
CLASSES = {
    "tb.skipped": "skip", "tb.double_stop": "double", "tb.out_of_order": "out_of_order",
    "tb.loop": "loop", "tb.trap": "trap", "tb.escape": "escape", "tb.edge_stuck": "stuck",
    "tb.focus_lost": "lost", "tb.ghost_stop": "ghost", "tb.revisit": "revisit",
    "tb.window_order": "window_order", "tb.wrong_announcement": "speech",
}

Rect = tuple[int, int, int, int]


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def is_walk_id(s: Any) -> bool:
    return isinstance(s, str) and bool(ID_RE.match(s))


def walks_dir(store: Any) -> str:
    return os.path.join(store.root, WALKS_DIR)


def _path(store: Any, wid: str) -> str:
    return os.path.join(walks_dir(store), f"{wid}.json")


def save(store: Any, record: Mapping[str, Any]) -> str:
    """Write ``record`` (its ``id`` names it) atomically; prune old records."""
    wid = str(record["id"])
    if not is_walk_id(wid):
        raise ValueError(f"not a walk id: {wid!r}")
    d = walks_dir(store)
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = _path(store, wid)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(dumps(record))
    os.replace(tmp, path)
    with contextlib.suppress(OSError):
        _prune(store)
    return path


def _entries(store: Any) -> list[tuple[float, str]]:
    """``(mtime, id)`` of every stored record, newest first."""
    d = walks_dir(store)
    try:
        names = os.listdir(d)
    except OSError:
        return []
    out = []
    for name in names:
        wid = name[:-5] if name.endswith(".json") else None
        if wid is None or not is_walk_id(wid):
            continue
        with contextlib.suppress(OSError):
            out.append((os.stat(os.path.join(d, name)).st_mtime, wid))
    out.sort(reverse=True)
    return out


def _prune(store: Any) -> None:
    now = time.time()
    for i, (mtime, wid) in enumerate(_entries(store)):
        if i >= KEEP or now - mtime > KEEP_S:
            with contextlib.suppress(OSError):
                os.remove(_path(store, wid))


def load(store: Any, wid: str) -> dict[str, Any]:
    try:
        with open(_path(store, wid), encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        raise OpError("walk_not_found", f"no stored walk {wid!r}",
                      hint="captures(what=\"walks\") lists the stored walks and scenarios."
                      ) from None
    if not isinstance(rec, dict):
        raise OpError("walk_not_found", f"walk {wid!r} is unreadable")
    return rec


def drop(store: Any, wid: str) -> None:
    try:
        os.remove(_path(store, wid))
    except FileNotFoundError:
        raise OpError("walk_not_found", f"no stored walk {wid!r}") from None


def wipe(store: Any) -> int:
    """Remove every stored walk (``captures(action="gc", all=true)``)."""
    n = 0
    for _mtime, wid in _entries(store):
        with contextlib.suppress(OSError):
            os.remove(_path(store, wid))
            n += 1
    return n


def _lineage_of(rec: Mapping[str, Any]) -> tuple[str, str]:
    return str(rec.get("serial") or ""), str(rec.get("package") or "")


def resolve(store: Any, spec: Any, lineage: tuple[str, str] | None = None,
            kind: str | None = "w") -> str:
    """A walk id, or ``latest`` / None: the newest walk (``kind`` "w") of ``lineage``."""
    s = spec.strip() if isinstance(spec, str) else None
    if s and s != "latest":
        if not is_walk_id(s):
            raise OpError("bad_args", f"{s!r} is not a walk id (w + 6 letters or digits)",
                          hint="captures(what=\"walks\") lists them.")
        if not os.path.exists(_path(store, s)):
            raise OpError("walk_not_found", f"no stored walk {s!r}",
                          hint="captures(what=\"walks\") lists the stored walks and scenarios.")
        return s
    for _mtime, wid in _entries(store):
        if kind and not wid.startswith(kind):
            continue
        if lineage is None:
            return wid
        with contextlib.suppress(OpError):
            if _lineage_of(load(store, wid)) == tuple(lineage):
                return wid
    what = f" of {lineage[1]}" if lineage else ""
    raise OpError("walk_not_found", f"no TalkBack walk{what} is stored",
                  hint="tb_walk() records one.")


def _ago(s: float) -> str:
    s = max(0, int(s))
    if s < 120:
        return f"{s}s ago"
    if s < 7200:
        return f"{s // 60}m ago"
    if s < 172800:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def listing(store: Any, lineage: tuple[str, str] | None, limit: int,
            now: float | None = None) -> tuple[list[str], int]:
    """One line per stored walk / scenario (newest first) and how many matched."""
    now = time.time() if now is None else now
    rows: list[str] = []
    total = 0
    for mtime, wid in _entries(store):
        try:
            rec = load(store, wid)
        except OpError:
            continue
        if lineage is not None and _lineage_of(rec) != tuple(lineage):
            continue
        total += 1
        if len(rows) >= limit:
            continue
        caps = rec.get("captures") or []
        serial, package = _lineage_of(rec)
        if wid.startswith("w"):
            n = sum(1 for s in rec.get("steps") or [] if s.get("i", 0) > 0)
            nf = sum(1 for f in rec.get("findings") or [] if f.get("code") != "model.mismatch")
            what = (f"tb_walk {n} steps ended={rec.get('ended')} "
                    f"{nf} finding{'s' if nf != 1 else ''}")
        else:
            what = f"tb_scenario {rec.get('kind')} verdict={rec.get('verdict')}"
        rows.append(f"{wid} {what} {serial}/{package} on {','.join(caps) or '-'} "
                    f"{_ago(now - mtime)}")
    return rows, total


# --------------------------------------------------------------------------- #
# Binding node keys to capture refs
# --------------------------------------------------------------------------- #
def _rect(b: Any) -> Rect | None:
    if isinstance(b, Mapping):
        b = b.get("layout") or b
        if isinstance(b, Mapping):
            try:
                return int(b.get("x", 0)), int(b.get("y", 0)), int(b.get("w", 0)), int(b.get("h", 0))
            except (TypeError, ValueError):
                return None
        return None
    if isinstance(b, (list, tuple)) and len(b) >= 4:
        return int(b[0]), int(b[1]), int(b[2]), int(b[3])
    return None


def iou(a: Rect, b: Rect) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    w = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    h = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = w * h
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def signature(cls: str | None, label: str | None) -> str:
    """The walk's identity without ids or bounds (talkback.walk.signature)."""
    return f"{(cls or '').rsplit('.', 1)[-1]}|{label or ''}"


def _raw_label(raw: Mapping[str, Any]) -> str:
    own = raw.get("text") or raw.get("content_description") or ""
    if own:
        return str(own)
    kids = [(c.get("text") or c.get("content_description") or "")
            for c in raw.get("children") or []]
    return " | ".join(str(k) for k in kids if k)


class _CaptureKeys:
    """One capture's node keys -> refs, and its labelled nodes for signature matching."""

    def __init__(self, lc: Any) -> None:
        self.id = lc.id
        self.ix = lc.index()
        self.keys: dict[str, str] = {}
        self.sigs: list[tuple[str, Rect, str]] = []
        self.windows: dict[int, str] = {}
        from .tb import TbCapture, _iter_paths

        tbc = TbCapture.of(self.ix, lc)
        if tbc is None:
            return
        for w in tbc.dump.data.get("windows") or []:
            root = w.get("root")
            if not root:
                continue
            for raw, _path in _iter_paths(root):
                nid = tbc.nid_of_raw(raw)
                if nid is None:
                    continue
                k = raw.get("node_key")
                if k and k not in self.keys:
                    self.keys[str(k)] = nid
                label = _raw_label(raw)
                r = _rect(raw.get("bounds"))
                if label and r is not None:
                    self.sigs.append((signature(raw.get("class_name"), label), r, nid))
            rv = w.get("root_view_id")
            wref = tbc.window_ref(rv)
            if rv is not None and wref:
                self.windows[int(rv)] = wref

    def ref(self, key: str | None, sig: str | None = None, bounds: Any = None) -> str | None:
        if key:
            hit = self.keys.get(key)
            if hit is not None:
                return hit
            hit = self.ix.resolve_id(key)
            if hit is None and key.startswith("compose:"):
                _c, h, v = (key.split(":") + ["", ""])[:3]
                hit = self.ix.resolve_id(f"sem:{h}:{v}") or self.ix.resolve_id(f"a11y:{h}:{v}")
            if hit is not None:
                return hit
        r = _rect(bounds)
        if sig and not sig.endswith("|") and r is not None:
            best = max(((iou(r, r2), nid) for s2, r2, nid in self.sigs if s2 == sig),
                       default=(0.0, None))
            if best[0] >= 0.8:
                return best[1]
        return None


class Binding:
    """The captures a walk ran against, in the order they were taken: ``taken`` is
    ``[(step index, loaded capture)]`` (a capture taken at step i shows the screen from
    step i on). :meth:`ref` prefers the latest capture taken at or before a step, then
    the earliest one after it: refs carry over, so a node has one ref in all of them."""

    def __init__(self, taken: Sequence[tuple[int, Any]]) -> None:
        self.taken = sorted(((int(at), lc) for at, lc in taken), key=lambda t: t[0])
        self._caps = [(at, _CaptureKeys(lc)) for at, lc in self.taken]

    @property
    def ids(self) -> list[str]:
        out: list[str] = []
        for _at, c in self._caps:
            if c.id not in out:
                out.append(c.id)
        return out

    def _order(self, at: int | None) -> list[_CaptureKeys]:
        if at is None:
            return [c for _a, c in self._caps]
        before = [c for a, c in self._caps if a <= at]
        after = [c for a, c in self._caps if a > at]
        return list(reversed(before)) + after

    def ref(self, key: str | None, *, at: int | None = None, sig: str | None = None,
            bounds: Any = None) -> tuple[str | None, str | None]:
        """``(ref, capture id)`` of a node key (else its signature and box), or
        ``(None, None)`` when no capture holds it."""
        order = self._order(at)
        for c in order:
            r = c.ref(key)
            if r is not None:
                return r, c.id
        if sig:
            for c in order:
                r = c.ref(None, sig, bounds)
                if r is not None:
                    return r, c.id
        return None, None

    def has(self, key: str | None) -> bool:
        return bool(key) and any(key in c.keys for _a, c in self._caps)

    def window_ref(self, root_view_id: Any) -> str | None:
        with contextlib.suppress(TypeError, ValueError):
            for _a, c in reversed(self._caps):
                hit = c.windows.get(int(root_view_id))
                if hit:
                    return hit
        return None


def keys_of(lc: Any) -> set[str]:
    """The node keys a capture's accessibility dump holds."""
    return set(_CaptureKeys(lc).keys)


# --------------------------------------------------------------------------- #
# Walk records: bind, re-analyse, classify
# --------------------------------------------------------------------------- #
def _bind_key(binding: Binding, key: Any, at: int | None) -> str | None:
    if not isinstance(key, str) or not key:
        return key
    ref, _cid = binding.ref(key, at=at)
    return ref or key


def bind_walk(record: dict[str, Any], binding: Binding,
              expect: Sequence[str] | None = None) -> dict[str, Any]:
    """Name every step, predicted stop and finding of a walk record by capture ref
    (in place; returns it). Steps keep their ``key``; ``ref`` becomes the capture's
    ref (the key itself, with ``unbound``, when no capture holds the node) and
    ``cap`` the capture it was bound in. The findings are recomputed on the refs."""
    from ..talkback import diff as tbdiff

    for s in record.get("steps") or []:
        at = int(s.get("i") or 0)
        key = s.get("key")
        if key:
            ref, cid = binding.ref(key, at=at, sig=s.get("sig"), bounds=s.get("bounds"))
            if ref is None and s.get("pkey"):
                ref, cid = binding.ref(s["pkey"], at=at)
            if ref is not None:
                s["ref"], s["cap"] = ref, cid
                s.pop("unbound", None)
            else:
                s["ref"], s["unbound"] = key, True
        for k in ("scrolled", "container"):
            if s.get(k):
                s[k] = _bind_key(binding, s[k], at)
        if s.get("ancestors"):
            s["ancestors"] = [_bind_key(binding, a, at) for a in s["ancestors"]]
        cov = s.get("covered_by")
        if isinstance(cov, dict) and cov.get("overlay"):
            cov["ref"] = _bind_key(binding, cov["overlay"], at)
        wcov = s.get("window_covered_by")
        if wcov is not None and binding.window_ref(wcov):
            s["window_covered_by"] = binding.window_ref(wcov)
    for p in record.get("predicted") or []:
        ref, _cid = binding.ref(p.get("key"), at=0, sig=signature(p.get("cls"), p.get("label")),
                                bounds=p.get("bounds"))
        p["ref"] = ref or p.get("key")
    if record.get("cycle"):
        # the cycle was recorded by key (the walk's own refs are its keys)
        record["cycle"] = [_bind_key(binding, k, None) for k in record["cycle"]]
    edge = record.get("edge")
    if isinstance(edge, dict) and edge.get("container"):
        edge["container_ref"] = _bind_key(binding, edge["container"], None)
    for o in record.get("orphans") or []:
        if o.get("key"):
            o["ref"] = _bind_key(binding, o["key"], None)
    old_vs = record.get("vs_model") or {}
    analysis = tbdiff.analyze(record, expect=expect)
    findings = analysis["findings"]
    for f in findings:  # refs a check took from keys (orphan text)
        f["refs"] = [_bind_key(binding, r, None) for r in f.get("refs") or []]
    record["findings"] = findings
    vs = analysis["vs_model"]
    if old_vs.get("initial"):
        vs["initial"] = old_vs["initial"]
    record["vs_model"] = vs
    if analysis.get("expect") is not None:
        record["expect"] = analysis["expect"]
    record["captures"] = binding.ids
    return record


def classify(record: Mapping[str, Any]) -> dict[str, Any]:
    """Predicted vs actual, by class and ref: ``{"ended": ..., "model": "13 agree, 1
    differ: step 7 ...", "skip": [refs], "double": [refs], ...}``."""
    out: dict[str, Any] = {"ended": record.get("ended")}
    vs = record.get("vs_model") or {}
    agree, differ = vs.get("agree"), vs.get("differ")
    if agree is not None:
        text = f"{agree} agree, {differ or 0} differ"
        if vs.get("first"):
            text += f": {vs['first']}"
        out["model"] = text
    for k in ("unpredicted", "unvisited"):
        if vs.get(k):
            out[k] = list(vs[k])[:MAX_REFS]
    for f in record.get("findings") or []:
        cls = CLASSES.get(f.get("code") or "")
        if cls is None:
            continue
        bucket = out.setdefault(cls, [])
        for r in f.get("refs") or []:
            if r and r not in bucket and len(bucket) < MAX_REFS:
                bucket.append(r)
    left = next((s for s in record.get("steps") or [] if s.get("via") == "left_app"), None)
    if left is not None:
        out["left_app"] = left.get("top") or "?"
    return {k: v for k, v in out.items() if v not in (None, [], "")}


# --------------------------------------------------------------------------- #
# The tb_walk result
# --------------------------------------------------------------------------- #
def _speak(s: Mapping[str, Any], n: int) -> str:
    sp = str(s.get("speak") or s.get("label") or "").replace("\n", " ")
    return sp if len(sp) <= n else sp[: n - 1] + "…"


def step_line(s: Mapping[str, Any], speak_len: int = SPEAK_LEN) -> str:
    """``3. n14 "Add to favorites, Button" via=autoscroll(n10) !double_stop``."""
    i = s.get("i")
    if s.get("via") == "start" and not s.get("key"):
        return f"{i}. (no accessibility focus)"
    if s.get("edge"):
        return f"{i}. — edge"
    if s.get("via") == "left_app":
        return f"{i}. — left the app (top: {s.get('top') or '?'})"
    if s.get("via") == "lost":
        return f"{i}. — focus lost" + (f" after {s['scrolled']} scrolled" if s.get("scrolled")
                                       else "")
    ref = s.get("ref") or "-"
    head = f"?{ref} {s.get('cls') or '?'}" if s.get("unbound") else ref
    out = f'{i}. {head} "{_speak(s, speak_len)}"'
    via = s.get("via")
    if via == "autoscroll" and s.get("scrolled"):
        out += f" via=autoscroll({s['scrolled']})"
    elif via not in ("next", "start", None):
        out += f" via={via}"
    for tag in s.get("tags") or []:
        out += f" !{tag}"
    return out


def _tags(record: Mapping[str, Any]) -> dict[int, list[str]]:
    tags: dict[int, list[str]] = {}
    for f in record.get("findings") or []:
        if f.get("code") == "model.mismatch":
            continue
        short = (f.get("code") or "").split(".", 1)[-1]
        for i in f.get("steps") or []:
            if short not in tags.setdefault(i, []):
                tags[i].append(short)
    return tags


def _compact_findings(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Findings without keys/steps; a fix is given once per code."""
    out = []
    fixed: set[str] = set()
    for f in record.get("findings") or []:
        d = {k: f[k] for k in ("code", "sev", "refs", "basis", "msg") if f.get(k) not in (None, [])}
        if f.get("fix") and f["code"] not in fixed:
            d["fix"] = f["fix"]
            fixed.add(f["code"])
        out.append(d)
    return out


def walk_hints(record: Mapping[str, Any]) -> list[str]:
    """At most 3 next calls (<= 200 B): the first finding's node, the overlay, the
    reverse walk or a restore."""
    from .query import call

    hints: list[str] = []
    for f in record.get("findings") or []:
        if f.get("code") == "model.mismatch":
            continue
        ref = next((r for r in f.get("refs") or [] if re.match(r"^n\d+$", str(r))), None)
        if ref:
            hints.append(call("node", ref, facets="tb"))
            break
    if record.get("id"):
        hints.append(call("image", overlay="walk", walk=record["id"]))
    if str(record.get("restore") or "").startswith("left on"):
        hints.append(call("talkback", action="restore"))
    elif record.get("ended") in ("wrap", "edge") and record.get("direction") == "next":
        hints.append(call("tb_walk", direction="prev"))
    vs = record.get("vs_model") or {}
    if vs.get("differ") and len(hints) < 3:
        first = next((s for s in record.get("steps") or [] if s.get("i") == 0), None)
        frm = (first or {}).get("ref")
        hints.append(call("outline", view="reading", explain=True,
                          **({"from": frm} if frm and not (first or {}).get("unbound") else {})))
    out: list[str] = []
    for h in hints:
        if len(out) < 3 and utf8_len(dumps(out + [h])) <= NEXT_MAX_BYTES:
            out.append(h)
    return out


def walk_result(record: Mapping[str, Any], *, max_lines: int = WALK_MAX_LINES,
                max_bytes: int = WALK_MAX_BYTES) -> dict[str, Any]:
    """The tb_walk response for a bound record, within ``max_bytes`` of compact JSON:
    one line per step (``i. ref "speak" via=... !finding``), the classified ``diff``,
    the findings (fix once per code) and ``next``."""
    tags = _tags(record)
    steps = [dict(s, tags=sorted(tags.get(s.get("i"), []))) for s in record.get("steps") or []]
    caps = list(record.get("captures") or [])
    first = steps[0] if steps else {}
    head: dict[str, Any] = {"capture": caps[0] if caps else None, "walk": record.get("id")}
    if len(caps) > 1:
        head["recaptured"] = caps[1:]
    head["talkback"] = record.get("talkback")
    head["start"] = (first.get("ref") if first.get("key") else "(no focus)") if first else None
    head["steps"] = sum(1 for s in steps if (s.get("i") or 0) > 0)
    head["ended"] = record.get("ended")
    ms = record.get("ms") or {}
    head["ms"] = {k: ms[k] for k in ("p50", "p95", "total") if ms.get(k) is not None}
    if str(record.get("utterance") or "model") != "model":
        head["utterance"] = record["utterance"]
    diff = classify(record)
    findings = _compact_findings(record)
    tail: dict[str, Any] = {}
    if record.get("expect") is not None:
        tail["expect"] = record["expect"]
    if record.get("notes"):
        tail["notes"] = list(record["notes"])
    tail["restore"] = record.get("restore")
    hints = walk_hints(record)
    # max_lines counts the steps; the start line (step 0) comes on top
    speak_len, n_findings, n_lines = SPEAK_LEN, 8, max(5, int(max_lines)) + 1
    while True:
        lines = [step_line(s, speak_len) for s in steps]
        if len(lines) > n_lines:
            keep = n_lines - 1
            half = keep // 2
            lines = lines[:half] + [f"… {len(lines) - keep} steps omitted: captures("
                                    f"action=\"show\",id=\"{record.get('id')}\") …"] \
                + lines[len(lines) - (keep - half):]
        out = dict(head, lines=lines, diff=diff, findings=findings[:n_findings])
        if len(findings) > n_findings:
            out["findings_omitted"] = len(findings) - n_findings
        out.update(tail)
        if hints:
            out["next"] = hints
        out = {k: v for k, v in out.items() if v is not None}
        if utf8_len(dumps(out)) <= max_bytes:
            return out
        if speak_len > 24:
            speak_len -= 8
        elif n_findings > 3:
            n_findings -= 1
        elif tail.get("notes"):
            tail.pop("notes")
        elif n_lines > 13:
            n_lines = max(13, n_lines - 8)
        elif hints:
            hints = hints[:-1]
        elif n_findings > 1:
            n_findings -= 1
        else:
            return out


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #
def _named(ref: str | None, d: Mapping[str, Any] | None, n: int = 32) -> str | None:
    if d is None:
        return None
    sp = str(d.get("speak") or "").replace("\n", " ")
    sp = sp if len(sp) <= n else sp[: n - 1] + "…"
    return f'{ref or d.get("ref") or "?"} "{sp}"'


def bind_scenario(out: dict[str, Any], binding: Binding, before_at: int,
                  after_at: int) -> dict[str, Any]:
    """Name a scenario's nodes by capture ref (in place): the target in the capture
    taken before the action, the landing focus in the one taken after it, the
    timeline in either."""
    for key, at in (("target", before_at), ("focus", after_at)):
        d = out.get(key)
        if isinstance(d, dict) and d.get("ref"):
            ref, _cid = binding.ref(d["ref"], at=at, sig=signature(d.get("cls"), d.get("speak")),
                                    bounds=d.get("bounds"))
            d["key"] = d["ref"]
            d["ref"] = ref or d["ref"]
    for ev in out.get("timeline") or []:
        if ev.get("focus"):
            ev["focus"] = _bind_key(binding, ev["focus"], after_at)
    model = out.get("model")
    if isinstance(model, dict) and model.get("initial"):
        model["initial"] = _bind_key(binding, model["initial"], after_at)
    f = out.get("finding")
    if isinstance(f, dict) and f.get("msg"):
        msg = f["msg"]
        for key in re.findall(r"\b(?:view|compose|virtual):-?\d+(?::-?\d+)?", msg):
            ref = _bind_key(binding, key, after_at)
            if ref and ref != key:
                msg = msg.replace(key, ref)
        f["msg"] = msg
    out["captures"] = binding.ids
    return out


def survive_cause(before: Any, after: Any, target_ref: str | None) -> str | None:
    """What a mutation did to the screen and to the target, from two captures:
    ``n47 rebound as n103 (a recycled cell); 12 removed, 12 added``."""
    if before is None or after is None:
        return None
    a, b = before.index(), after.index()
    ids_a, ids_b = set(a.nodes), set(b.nodes)
    rebound = {nid: n.rebound_of for nid, n in b.nodes.items() if n.rebound_of}
    old = set(rebound.values())
    added = len(ids_b - ids_a - set(rebound))
    removed = len(ids_a - ids_b - old)
    parts = []
    if target_ref and re.match(r"^n\d+$", target_ref):
        new = next((nid for nid, o in rebound.items() if o == target_ref), None)
        if new is not None:
            parts.append(f"{target_ref} rebound as {new} (a recycled cell or a re-created item)")
        elif target_ref not in ids_b:
            parts.append(f"{target_ref} was removed")
        else:
            la, lb = a.nodes.get(target_ref), b.nodes.get(target_ref)
            if la is not None and lb is not None and (la.label or "") != (lb.label or ""):
                parts.append(f"{target_ref} now reads {_quote(lb.label)} (was {_quote(la.label)})")
            else:
                parts.append(f"{target_ref} is still there")
    parts.append(f"{removed} removed, {added} added"
                 + (f", {len(rebound)} rebound" if rebound else ""))
    return "; ".join(parts)


def _quote(s: Any, n: int = 24) -> str:
    s = str(s or "").replace("\n", " ")
    return '"' + (s if len(s) <= n else s[: n - 1] + "…") + '"'


def scenario_hints(rec: Mapping[str, Any]) -> list[str]:
    from .query import call

    hints: list[str] = []
    target = (rec.get("target") or {}).get("ref") if isinstance(rec.get("target"), dict) else None
    focus = (rec.get("focus") or {}).get("ref") if isinstance(rec.get("focus"), dict) else None
    caps = rec.get("captures") or []
    if rec.get("finding") and len(caps) > 1:
        hints.append(call("diff", a=caps[0], b=caps[-1]))
    if target and re.match(r"^n\d+$", str(target)) and target != focus:
        hints.append(call("node", target, facets="tb"))
    if focus and re.match(r"^n\d+$", str(focus)):
        hints.append(call("node", focus, facets="tb"))
    if str(rec.get("restore") or "").startswith("left on"):
        hints.append(call("talkback", action="restore"))
    out: list[str] = []
    for h in hints:
        if len(out) < 3 and utf8_len(dumps(out + [h])) <= NEXT_MAX_BYTES:
            out.append(h)
    return out


def _cut(s: Any, n: int) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def scenario_result(rec: Mapping[str, Any], *, max_bytes: int = SCENARIO_MAX_BYTES
                    ) -> dict[str, Any]:
    """The tb_scenario response (at most ``max_bytes``, 1 KB by default)."""
    caps = list(rec.get("captures") or [])
    tgt = rec.get("target") if isinstance(rec.get("target"), dict) else None
    foc = rec.get("focus") if isinstance(rec.get("focus"), dict) else None
    out: dict[str, Any] = {"scenario": rec.get("id"), "kind": rec.get("kind"),
                           "capture": caps[0] if caps else None}
    if len(caps) > 1:
        out["after"] = caps[-1]
    out["target"] = _named(tgt.get("ref"), tgt) if tgt else None
    did = rec.get("action") if rec.get("kind") != "survive" else rec.get("mutate")
    out["did"] = _cut(did, 60) if did else None
    if rec.get("kind") == "focus_after":
        b, a = rec.get("before") or {}, rec.get("after") or {}
        out["windows"] = f"{b.get('windows')}->{a.get('windows')}"
        if a.get("panes"):
            out["panes"] = list(a["panes"])[:3]
        out["new_screen"] = rec.get("new_screen")
        model = rec.get("model") or {}
        if model.get("initial"):
            out["model_initial"] = model["initial"]
    elif rec.get("kind") == "restore":
        back = rec.get("back") or {}
        out["windows"] = f"{(rec.get('opened') or {}).get('windows')}->{back.get('windows')}"
    timeline = [f"{e.get('t')} " + ("windows " + str(e["windows"]) if "windows" in e and
                                    "focus" not in e else
                                    str(e.get("focus") if e.get("focus") is not None else "-")
                                    + (f" w{e['windows']}" if "windows" in e else ""))
                for e in rec.get("timeline") or []]
    out["timeline"] = timeline
    out["focus"] = _named(foc.get("ref"), foc) if foc else None
    out["verdict"] = rec.get("verdict")
    f = rec.get("finding")
    finding = {k: f[k] for k in ("code", "sev", "msg", "fix") if f.get(k)} if f else None
    out["finding"] = finding
    out["cause"] = rec.get("cause_text")
    if rec.get("notes"):
        out["notes"] = list(rec["notes"])
    out["restore"] = rec.get("restore")
    hints = scenario_hints(rec)
    if hints:
        out["next"] = hints
    out = {k: v for k, v in out.items() if v not in (None, [], "")}

    def size() -> int:
        return utf8_len(dumps(out))

    shrink: list[Callable[[], bool]] = [
        lambda: _pop_list(out, "notes"),
        lambda: _trim_list(out, "timeline", 6),
        lambda: _trim_text(out.get("finding"), "fix", 160),
        lambda: _trim_list(out, "timeline", 3),
        lambda: _trim_text(out.get("finding"), "msg", 120),
        lambda: _trim_list(out, "next", 1),
        lambda: _trim_text(out, "cause", 80),
        lambda: _trim_text(out.get("finding"), "fix", 80),
        lambda: _pop_list(out, "panes"),
        lambda: _pop_list(out, "timeline"),
        lambda: _pop_list(out, "next"),
    ]
    for step in shrink:
        if size() <= max_bytes:
            break
        step()
    return out


def _pop_list(d: dict[str, Any], key: str) -> bool:
    return d.pop(key, None) is not None


def _trim_list(d: dict[str, Any], key: str, n: int) -> bool:
    v = d.get(key)
    if isinstance(v, list) and len(v) > n:
        d[key] = v[:n] + ([f"+{len(v) - n}"] if key == "timeline" else [])
        return True
    return False


def _trim_text(d: Any, key: str, n: int) -> bool:
    if isinstance(d, dict) and isinstance(d.get(key), str) and len(d[key]) > n:
        d[key] = d[key][: n - 1] + "…"
        return True
    return False


def stored_result(rec: Mapping[str, Any], max_bytes: int | None = None) -> dict[str, Any]:
    """``captures(action="show", id=<walk id>)``: the stored record as its tool
    returned it (a walk's lines are not cut by count, only by ``max_bytes``)."""
    if str(rec.get("id") or "").startswith("t"):
        return scenario_result(rec, max_bytes=max_bytes or 2000)
    n = sum(1 for _ in rec.get("steps") or [])
    return walk_result(rec, max_lines=max(WALK_MAX_LINES, n + 1),
                       max_bytes=max_bytes or 16000)


__all__ = [
    "Binding",
    "CLASSES",
    "bind_scenario",
    "bind_walk",
    "classify",
    "drop",
    "is_walk_id",
    "keys_of",
    "listing",
    "load",
    "resolve",
    "save",
    "scenario_result",
    "stored_result",
    "survive_cause",
    "walk_hints",
    "walk_result",
    "walks_dir",
    "wipe",
]
