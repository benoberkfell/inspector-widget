"""Compare a TalkBack walk (A, actual) with the model (P, predicted) and a visual
reading order (V), and classify what a walk can show (design part 2).

Pure: it works on the walk record :mod:`.walk` saves (steps, predicted stops,
how the walk ended), so a stored walk can be re-analysed offline.

Codes: ``tb.out_of_order``, ``tb.loop``, ``tb.edge_stuck``, ``tb.skipped``,
``tb.ghost_stop``, ``tb.double_stop``, ``tb.escape`` (basis ``walk``, or
``expect`` when the caller gave the order it expects), and ``model.mismatch``
(where the model disagrees with the walk: calibration data; the walk wins).
"""

from __future__ import annotations

import bisect
import importlib
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

Rect = Tuple[int, int, int, int]

MAX_REFS = 6
SLIVER_DP = 12      # a stop thinner than this is a scrolled-off sliver
TINY_DP = 4
DOUBLE_STOP_OVERLAP = 0.6
_ROLE_ONLY = {"button", "image", "checkbox", "switch", "edit box", "slider", "toggle button",
              "radio button", "drop down list", "unlabelled", "unlabeled", ""}
_UNLABELLED = re.compile(r"\bunlabell?ed\b", re.I)
_ROLE_STATE_WORDS = {"button", "switch", "checkbox", "check", "box", "image", "edit", "slider",
                     "toggle", "radio", "on", "off", "checked", "not", "selected", "disabled",
                     "heading", "link", "double", "tap", "to", "activate", "in", "list", "of"}

FIXES = {
    "tb.out_of_order": "Group each column/card: Compose Modifier.semantics { isTraversalGroup = true } "
                       "(+ traversalIndex inside the group); Views: accessibilityTraversalBefore/After "
                       "to a unique, important target, or restructure the layout.",
    "tb.loop": "Break the traversal cycle (traversalBefore/After), request input focus once "
               "(LaunchedEffect(Unit)), and make 'load more' a button.",
    "tb.edge_stuck": "Expose scrolling to accessibility: scroll semantics/actions and CollectionInfo "
                     "(RecyclerView/LazyColumn/verticalScroll); pagers need visible page buttons or "
                     "custom actions.",
    "tb.skipped": "Make the content visible to TalkBack (not clipped under a system bar, not hidden "
                  "by an ancestor, not covered), or fold it into a stop's label.",
    "tb.ghost_stop": "Label it, hide it (Compose hideFromAccessibility / clearAndSetSemantics {}, "
                     "View importantForAccessibility=no or GONE, not alpha 0), or keep partly "
                     "scrolled-off rows out of the focus order.",
    "tb.double_stop": "Make one of the two the stop: merge the child into the container "
                      "(Modifier.toggleable/clickable on the row, child onClick=null) or make the "
                      "container not focusable.",
    "tb.escape": "Use a real Dialog / ModalBottomSheet, or hide the content behind the overlay while "
                 "it is open (Compose hideFromAccessibility, View noHideDescendants) and give the "
                 "overlay a paneTitle.",
}


def _finding(code: str, sev: str, msg: str, steps: Sequence[Dict[str, Any]] = (),
             basis: str = "walk", refs: Optional[List[str]] = None,
             keys: Optional[List[str]] = None) -> Dict[str, Any]:
    f: Dict[str, Any] = {"code": code, "sev": sev, "basis": basis, "msg": msg}
    f["refs"] = (refs if refs is not None else _uniq([s.get("ref") for s in steps]))[:MAX_REFS]
    f["keys"] = (keys if keys is not None else _uniq([s.get("key") for s in steps]))[:MAX_REFS]
    f["steps"] = [s["i"] for s in steps][:MAX_REFS * 2]
    if code in FIXES:
        f["fix"] = FIXES[code]
    return f


def _uniq(xs: Sequence[Optional[str]]) -> List[str]:
    out: List[str] = []
    for x in xs:
        if x and x not in out:
            out.append(x)
    return out


def _q(s: Optional[str], n: int = 32) -> str:
    s = (s or "").strip().replace("\n", " ")
    return f"'{s[:n - 1]}…'" if len(s) > n else f"'{s}'"


def _name(s: Dict[str, Any]) -> str:
    return f"{s.get('ref')} {_q(s.get('label') or s.get('speak'))}"


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
def _rect(s: Dict[str, Any]) -> Optional[Rect]:
    b = s.get("bounds")
    return tuple(b) if b and len(b) == 4 else None  # type: ignore[return-value]


def _contains(outer: Rect, inner: Rect, frac: float = 0.9) -> bool:
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    ax, ay = max(ox, ix), max(oy, iy)
    bx, by = min(ox + ow, ix + iw), min(oy + oh, iy + ih)
    inter = max(0, bx - ax) * max(0, by - ay)
    return iw * ih > 0 and inter >= frac * iw * ih and ow * oh >= iw * ih


def _intersects(a: Rect, b: Rect) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def _bands(items: List[Tuple[str, Rect]], axis: int) -> List[List[Tuple[str, Rect]]]:
    """Split items into bands separated by whitespace along ``axis`` (1 = y, 0 = x)."""
    ordered = sorted(items, key=lambda it: (it[1][axis], it[1][1 - axis]))
    bands: List[List[Tuple[str, Rect]]] = []
    end = None
    for it in ordered:
        lo, hi = it[1][axis], it[1][axis] + it[1][axis + 2]
        if end is None or lo >= end:
            bands.append([it])
            end = hi
        else:
            bands[-1].append(it)
            end = max(end, hi)
    return bands


def xy_cut(items: List[Tuple[str, Rect]]) -> List[str]:
    """A heuristic visual reading order: blocks by horizontal whitespace, top to
    bottom; inside a block, columns (only when each has 2+ stops) left to right,
    else a row read left to right."""
    if len(items) <= 1:
        return [k for k, _ in items]
    rows = _bands(items, 1)
    if len(rows) > 1:
        return [k for band in rows for k in xy_cut(band)]
    cols = _bands(items, 0)
    if len(cols) > 1 and all(len(c) >= 2 for c in cols):
        return [k for col in cols for k in xy_cut(col)]
    return [k for k, _ in sorted(items, key=lambda it: (it[1][0], it[1][1]))]


_T1_VISUAL: List[Any] = []


def _t1_order_items() -> Optional[Any]:
    """T1's talkback.visual.order_items, when it reads a grid of touching rows
    row by row (walk boxes from Views usually touch: row n ends where n+1 starts)."""
    if not _T1_VISUAL:
        fn = None
        try:
            fn = importlib.import_module("inspector_widget.talkback.visual").order_items
            grid = [{"key": k, "bounds": b, "window": 0} for k, b in (
                ("a", (0, 0, 100, 50)), ("b", (150, 0, 100, 50)),
                ("c", (0, 50, 100, 50)), ("d", (150, 50, 100, 50)))]
            if fn(grid) != ["a", "b", "c", "d"]:
                fn = None
        except Exception:  # noqa: BLE001 - not merged, or broken: use xy_cut
            fn = None
        _T1_VISUAL.append(fn)
    return _T1_VISUAL[0]


def visual_order(items: List[Tuple[str, Rect]]) -> Tuple[List[str], str]:
    """V: T1's talkback.visual XY-cut when it is merged and sound, else :func:`xy_cut`."""
    fn = _t1_order_items()
    if fn is not None:
        try:
            keys = fn([{"key": k, "bounds": r, "window": 0} for k, r in items])
            if isinstance(keys, list) and set(keys) == {k for k, _ in items}:
                return keys, "talkback.visual"
        except Exception:  # noqa: BLE001
            pass
    return xy_cut(items), "xy_cut"


def _lis(seq: List[int]) -> List[int]:
    """Indices of one longest strictly increasing subsequence of ``seq``."""
    tails: List[int] = []
    tails_idx: List[int] = []
    prev = [-1] * len(seq)
    for i, v in enumerate(seq):
        j = bisect.bisect_left(tails, v)
        if j == len(tails):
            tails.append(v)
            tails_idx.append(i)
        else:
            tails[j] = v
            tails_idx[j] = i
        prev[i] = tails_idx[j - 1] if j > 0 else -1
    out: List[int] = []
    k = tails_idx[-1] if tails_idx else -1
    while k >= 0:
        out.append(k)
        k = prev[k]
    return out[::-1]


# --------------------------------------------------------------------------- #
# The walk's first lap
# --------------------------------------------------------------------------- #
def _pk(s: Dict[str, Any]) -> Optional[str]:
    """The model's key for a step: ``pkey`` when Compose re-minted the node's id."""
    return s.get("pkey") or s.get("key")


def _moves(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [s for s in steps if s.get("moved") and s.get("key") and not s.get("edge")
            and s.get("via") != "left_app"]


def first_lap(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Moves up to the first edge or wrap (the lap the order checks look at)."""
    out: List[Dict[str, Any]] = []
    for s in steps:
        if s.get("edge") or s.get("via") in ("wrap", "left_app"):
            break
        if s.get("moved") and s.get("key") and (not out or out[-1]["key"] != s["key"]):
            out.append(s)
    return out


def _lap_complete(walk: Dict[str, Any]) -> bool:
    return walk.get("ended") in ("wrap", "edge") or any(s.get("edge") for s in walk["steps"])


def _dp(px: float, density: int) -> float:
    return px * 160.0 / max(1, density)


def _tokens(s: Optional[str]) -> set:
    return {t for t in re.findall(r"[\w$%]+", (s or "").lower()) if len(t) > 1}


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def _check_model(walk: Dict[str, Any], lap: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    P = [p["key"] for p in walk.get("predicted") or []]
    pref = {p["key"]: p for p in walk.get("predicted") or []}
    if not P:
        return {"agree": 0, "differ": 0, "model": walk.get("model") or "none (no predicted stops)"}, []
    step = 1 if walk.get("direction", "next") == "next" else -1
    agree = differ = 0
    first = None
    pos: Optional[int] = P.index(_pk(lap[0])) if lap and _pk(lap[0]) in P else None
    for s in lap[1:]:
        k = _pk(s)
        exp_i = pos + step if pos is not None else None
        expected = P[exp_i] if exp_i is not None and 0 <= exp_i < len(P) else None
        if expected is not None and k == expected:
            agree += 1
        elif expected is not None or k not in P:
            differ += 1
            if first is None:
                exp = pref.get(expected) if expected else None
                first = (f"step {s['i']}: model " + (f"{exp['ref']} {_q(exp['label'])}" if exp else "(end)")
                         + f", actual {_name(s)}")
        pos = P.index(k) if k in P else None
    visited = {_pk(s) for s in _moves(walk["steps"])}
    unpredicted = [s for s in lap if _pk(s) not in P]
    covered = {s.get("window") for s in walk["steps"] if s.get("window_covered_by") is not None}
    unvisited = _unvisited(walk, P, visited, covered)
    vs = {"agree": agree, "differ": differ, "model": walk.get("model")}
    if first:
        vs["first"] = first
    if unpredicted:
        vs["unpredicted"] = [s["ref"] for s in unpredicted][:MAX_REFS]
    if unvisited:
        vs["unvisited"] = [pref[k]["ref"] for k in unvisited][:MAX_REFS]
    findings = []
    if differ or unpredicted or unvisited:
        parts = []
        if differ:
            parts.append(f"{differ} of {agree + differ} moves differ ({first})")
        if unpredicted:
            parts.append(f"{len(unpredicted)} stops the model does not predict: "
                         + ", ".join(_name(s) for s in unpredicted[:3]))
        if unvisited:
            parts.append(f"{len(unvisited)} predicted stops TalkBack never reached")
        f = _finding("model.mismatch", "info", "; ".join(parts) + " (calibration data: the walk "
                     "is ground truth)", unpredicted)
        f["refs"] = (f["refs"] + [pref[k]["ref"] for k in unvisited])[:MAX_REFS]
        findings.append(f)
    return vs, findings


def _unvisited(walk: Dict[str, Any], P: List[str], visited: set, covered_windows: set) -> List[str]:
    """Predicted stops never visited, where the walk covered them: all of P after a
    full lap, else only those between the first and last predicted stops reached."""
    pwin = {p["key"]: p.get("window") for p in walk.get("predicted") or []}
    idx = [i for i, k in enumerate(P) if k in visited]
    if not idx:
        return []
    lo, hi = (0, len(P) - 1) if _lap_complete(walk) else (min(idx), max(idx))
    return [k for k in P[lo:hi + 1] if k not in visited and pwin.get(k) not in covered_windows]


def _check_skipped(walk: Dict[str, Any]) -> List[Dict[str, Any]]:
    P = [p["key"] for p in walk.get("predicted") or []]
    pref = {p["key"]: p for p in walk.get("predicted") or []}
    visited = {_pk(s) for s in _moves(walk["steps"])}
    covered = {s.get("window") for s in walk["steps"] if s.get("window_covered_by") is not None}
    miss = _unvisited(walk, P, visited, covered)
    if not miss:
        return []
    names = ", ".join(f"{pref[k]['ref']} {_q(pref[k]['label'])}" for k in miss[:3])
    more = f" (+{len(miss) - 3} more)" if len(miss) > 3 else ""
    scope = "in a full lap" if _lap_complete(walk) else "between the stops it did reach"
    return [_finding("tb.skipped", "warn",
                     f"TalkBack never reached {len(miss)} predicted stop(s) {scope}: {names}{more}",
                     refs=[pref[k]["ref"] for k in miss], keys=miss)]


def _ghost_reasons(s: Dict[str, Any], density: int) -> List[str]:
    reasons = []
    speak = (s.get("speak") or "").strip()
    if s.get("utt") == "logcat":
        if not speak or _UNLABELLED.search(speak):
            reasons.append("unlabelled")
    elif not (s.get("label") or "").strip() and speak.lower().split(",")[0].strip() in _ROLE_ONLY:
        reasons.append("unlabelled")
    r = _rect(s)
    if r is not None:
        x, y, w, h = r
        win = s.get("window_rect")
        if w <= 0 or h <= 0 or (win and not _intersects(r, tuple(win))):  # type: ignore[arg-type]
            reasons.append("offscreen")
        elif _dp(min(w, h), density) < TINY_DP:
            reasons.append("tiny")
        elif _dp(min(w, h), density) < SLIVER_DP:
            reasons.append("sliver")
    cov = s.get("covered_by")
    if cov and not s.get("_escape"):
        reasons.append(f"occluded by {cov.get('ref') or cov.get('overlay')}")
    return reasons


def _check_ghosts(walk: Dict[str, Any]) -> List[Dict[str, Any]]:
    density = int(walk.get("density") or 420)
    out = []
    for s in _moves(walk["steps"]):
        reasons = _ghost_reasons(s, density)
        if reasons:
            r = _rect(s) or (0, 0, 0, 0)
            out.append(_finding("tb.ghost_stop", "warn",
                                f"step {s['i']}: {_name(s)} is a stop but is "
                                + " + ".join(reasons) + f" ({r[2]}x{r[3]}px"
                                + (f", said {_q(s.get('speak'), 40)}" if s.get("utt") == "logcat" else "")
                                + ")", [s]))
    return out[:5]


def _check_double(walk: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    prev: Optional[Dict[str, Any]] = None
    for s in walk["steps"]:
        if not s.get("moved") or s.get("edge") or not s.get("key"):
            prev = None
            continue
        if prev is not None and s.get("via") in ("next", "autoscroll", "start"):
            a, b = _rect(prev), _rect(s)
            if a and b and a != b and (_contains(a, b) or _contains(b, a)):
                outer, inner = (prev, s) if _contains(a, b) else (s, prev)
                # The inner stop's own words (its label; role and state words aside)
                # against everything the outer stop says.
                ti = _tokens(inner.get("label") or inner.get("speak")) - _ROLE_STATE_WORDS
                to = _tokens(f"{outer.get('speak') or ''} {outer.get('label') or ''}")
                overlap = len(ti & to) / len(ti) if ti else 0.0
                if overlap >= DOUBLE_STOP_OVERLAP:
                    out.append(_finding(
                        "tb.double_stop", "warn",
                        f"steps {prev['i']}-{s['i']}: {_name(outer)} and {_name(inner)} inside it are "
                        f"both stops, and {int(overlap * 100)}% of the inner one's words are already "
                        f"spoken at the outer one", [prev, s]))
        prev = s
    return out[:5]


def _check_escape(walk: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    moves = _moves(walk["steps"])
    for j, s in enumerate(moves):
        if s.get("window_covered_by") is not None:
            s["_escape"] = True
            out.append(_finding("tb.escape", "error",
                                f"step {s['i']}: focus reached {_name(s)} in a window under the modal "
                                f"window {s['window_covered_by']}", [s]))
            continue
        cov = s.get("covered_by")
        if not cov or not cov.get("rect"):
            continue
        orect = tuple(cov["rect"])
        inside = [m for m in moves[:j] if not m.get("covered_by") and _rect(m)
                  and _contains(orect, _rect(m))]  # type: ignore[arg-type]
        if inside:
            s["_escape"] = True
            out.append(_finding("tb.escape", "error",
                                f"step {s['i']}: focus left the overlay {cov.get('ref') or cov.get('overlay')} "
                                f"({cov.get('cls')}, {int(100 * cov.get('area', 0))}% of the window) and "
                                f"landed on {_name(s)} behind it", [inside[-1], s]))
    return out[:5]


def _check_order(walk: Dict[str, Any], lap: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Out-of-order stops per screen state: the lap is split where the screen
    changed (auto-scroll, another window, a stolen focus); inside each segment
    the stops outside the longest run that follows V are out of order."""
    segments: List[List[Dict[str, Any]]] = [[]]
    for s in lap:
        if s.get("via") in ("autoscroll", "window", "stolen") and segments[-1]:
            segments.append([])
        segments[-1].append(s)
    out = []
    sources = set()
    backwards = walk.get("direction") == "prev"
    for seg in segments:
        items = [(s["key"], _rect(s)) for s in seg if _rect(s)]
        # A container that holds other stops of this segment has no place in a
        # reading order of its own (the full-screen ScrollView stop).
        items = [(k, r) for k, r in items
                 if not any(k2 != k and _contains(r, r2) for k2, r2 in items)]  # type: ignore[arg-type]
        if len(items) < 3:
            continue
        v, src = visual_order(items)  # type: ignore[arg-type]
        sources.add(src)
        rank = {k: i for i, k in enumerate(v)}
        seq_steps = [s for s in seg if s["key"] in rank]
        seq = [rank[s["key"]] for s in seq_steps]
        if backwards:
            seq = [-x for x in seq]
        keep = set(_lis(seq))
        bad = [s for i, s in enumerate(seq_steps) if i not in keep]
        if bad:
            first = bad[0]
            out.append(_finding(
                "tb.out_of_order", "warn",
                f"{len(bad)} of {len(seq_steps)} stops are read out of visual order; first: step "
                f"{first['i']} {_name(first)} (visual position {rank[first['key']] + 1} of {len(v)})",
                bad))
    return out, {"visual": "+".join(sorted(sources)) or None}


def _check_end(walk: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    steps = walk["steps"]
    if walk.get("ended") == "loop" and walk.get("cycle"):
        cyc = walk["cycle"]
        out.append(_finding("tb.loop", "error",
                            f"focus cycles through {len(cyc)} stops without reaching an edge: "
                            + " > ".join(cyc[:8]) + (" > …" if len(cyc) > 8 else ""),
                            [s for s in steps if s.get("ref") in cyc][:MAX_REFS], refs=cyc))
    last = next((s for s in reversed(steps) if s.get("moved") and s.get("key")), None)
    if walk.get("ended") == "stuck" and last is not None:
        out.append(_finding("tb.edge_stuck", "error",
                            f"TalkBack stopped at {_name(last)}: two presses in a row moved nothing "
                            f"(no edge wrap)", [last]))
    edge = walk.get("edge") or {}
    if edge.get("can_scroll") and last is not None:
        out.append(_finding("tb.edge_stuck", "warn",
                            f"TalkBack hit the edge at {_name(last)} while its container "
                            f"{edge.get('container_ref') or edge.get('container')} "
                            f"({edge.get('container_cls')}) can still scroll "
                            f"{'/'.join(edge['can_scroll'])}: the rest is unreachable by swipe/keys",
                            [last]))
    return out


def _check_expect(lap: List[Dict[str, Any]], expect: Sequence[str]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    def hit(e: str, s: Dict[str, Any]) -> bool:
        e2 = e.strip().lower()
        if not e2:
            return False
        if e2 in ((s.get("key") or "").lower(), (s.get("ref") or "").lower()):
            return True
        return e2 == (s.get("label") or "").lower() or e2 in (s.get("label") or "").lower() \
            or e2 in (s.get("speak") or "").lower()

    positions: List[Tuple[str, int]] = []
    missing: List[str] = []
    for e in expect:
        at = next((j for j, s in enumerate(lap) if hit(e, s)), None)
        if at is None:
            missing.append(e)
        else:
            positions.append((e, at))
    res: Dict[str, Any] = {"ok": True, "matched": len(positions), "of": len(expect)}
    findings = []
    if missing:
        res["ok"] = False
        res["missing"] = missing[:MAX_REFS]
        findings.append(_finding("tb.skipped", "warn", "expected stops never reached: "
                                 + ", ".join(_q(m) for m in missing[:4]), basis="expect",
                                 refs=[], keys=[]))
    seq = [at for _e, at in positions]
    keep = set(_lis(seq))
    bad = [positions[i] for i in range(len(positions)) if i not in keep]
    if bad:
        res["ok"] = False
        e, at = bad[0]
        res["first_mismatch"] = f"{_q(e)} was reached at step {lap[at]['i']}, out of the expected order"
        findings.append(_finding("tb.out_of_order", "warn",
                                 f"{len(bad)} expected stop(s) came out of the expected order; first: "
                                 + res["first_mismatch"], [lap[at] for _e, at in bad], basis="expect"))
    return res, findings


def analyze(walk: Dict[str, Any], expect: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Findings + the model comparison for one walk record (see :mod:`.walk`)."""
    steps = walk.get("steps") or []
    for s in steps:
        s.pop("_escape", None)
    lap = first_lap(steps)
    findings: List[Dict[str, Any]] = []
    findings += _check_end(walk)
    findings += _check_escape(walk)
    findings += _check_skipped(walk)
    findings += _check_ghosts(walk)
    findings += _check_double(walk)
    order, vmeta = _check_order(walk, lap)
    findings += order
    exp_res = None
    if expect:
        exp_res, exp_findings = _check_expect(lap, expect)
        findings += exp_findings
    vs, model_findings = _check_model(walk, lap)
    if vmeta.get("visual"):
        vs["visual"] = vmeta["visual"]
    findings += model_findings
    for s in steps:
        s.pop("_escape", None)
    sev = {"error": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: (sev.get(f["sev"], 3), f["steps"][0] if f.get("steps") else 1 << 30))
    return {"findings": findings, "vs_model": vs, "expect": exp_res}
