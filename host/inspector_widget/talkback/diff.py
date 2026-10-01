"""Compare a TalkBack walk (A, actual) with the model (P, predicted) and a visual
reading order (V), and classify what a walk can show (design part 2).

Pure: it works on the walk record :mod:`.walk` saves (steps, predicted stops,
how the walk ended), so a stored walk can be re-analysed offline.

Codes: ``tb.out_of_order``, ``tb.loop``, ``tb.trap`` (the app took focus
between presses), ``tb.revisit`` (a stop read twice in one lap),
``tb.edge_stuck``, ``tb.skipped`` (predicted stops never reached, or text on
screen nobody read), ``tb.ghost_stop``, ``tb.double_stop``, ``tb.escape``,
``tb.focus_lost``, ``tb.wrong_announcement`` ("N of M" that counts an item
TalkBack never stops on; a merged row that reads its texts out of screen
order) (basis ``walk``, ``model`` for :func:`.walk.static_walk`,
or ``expect`` when the caller gave the order it expects), and
``model.mismatch`` (where the model disagrees with the walk: calibration data;
the walk wins).
"""

from __future__ import annotations

import bisect
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
    "tb.focus_lost": "Keep the focused item alive while it scrolls (stable keys / "
                     "LazyListState, no key churn); TalkBack re-focuses only after a scroll "
                     "event from the container.",
    "tb.trap": "Request input focus once (LaunchedEffect(Unit) / a one-off requestFocus), not "
               "on every recomposition or timer tick: each request pulls TalkBack's focus back.",
    "tb.revisit": "Give lazy items stable keys and one stop each (a traversal group per card); "
                  "avoid content that re-lays out while TalkBack scrolls it.",
    "tb.wrong_announcement": "Keep empty header/footer items out of the adapter (or mark them "
                             "with CollectionItemInfo that TalkBack can skip) so positions and "
                             "counts match the rows it reads.",
    "tb.window_order": "Make the popup focusable/modal (PopupWindow(focusable=true), "
                       "ListPopupWindow.setModal(true)) or show it in the layout flow, so it is read "
                       "where it appears.",
    "tb.escape": "Use a real Dialog / ModalBottomSheet, or hide the content behind the overlay while "
                 "it is open (Compose hideFromAccessibility, View noHideDescendants) and give the "
                 "overlay a paneTitle.",
}
# tb.wrong_announcement for a merged row read out of order (FIXES has the "N of M" one).
_FIX_SPEECH_ORDER = ("Compose the texts in reading order (a merged row reads its children in "
                     "composition order, not placement), or give the row one label in reading "
                     "order: clearAndSetSemantics { contentDescription = \"Title, $5\" }.")


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


def visual_order(items: List[Tuple[str, Rect]]) -> Tuple[List[str], str]:
    """V: talkback.visual's XY-cut over plain boxes (blocks by whitespace, top to
    bottom; columns only when each holds two or more stops, else a row)."""
    from .visual import order_items
    return order_items([{"key": k, "bounds": r, "window": 0} for k, r in items]), "talkback.visual"


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
        if s.get("edge") or s.get("via") in ("wrap", "left_app", "lost", "screen"):
            break
        if s.get("moved") and s.get("key") and (not out or out[-1]["key"] != s["key"]):
            out.append(s)
    return out


def _first_screen(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The steps before the screen was replaced under the walk (``via="screen"``: another
    activity or window took the place of the one it was in; capture/walks.py marks it).
    What the model predicted is about that first screen only."""
    out: List[Dict[str, Any]] = []
    for s in steps:
        if s.get("via") == "screen":
            break
        out.append(s)
    return out


def _lap_complete(walk: Dict[str, Any]) -> bool:
    """A full lap: the walk wrapped (past an edge and back onto a stop it had read)."""
    steps = _first_screen(walk["steps"])
    if len(steps) < len(walk["steps"]):
        return any(s.get("via") == "wrap" for s in steps)
    return walk.get("ended") == "wrap" or any(s.get("via") == "wrap" for s in steps)


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
        if s.get("via") == "screen":  # another screen: the prediction was for the first one
            break
        if s.get("via") == "stolen":  # the app moved focus, not TalkBack: nothing to predict
            pos = P.index(k) if k in P else None
            continue
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
    visited = {_pk(s) for s in _moves(_first_screen(walk["steps"]))}
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


def _coverage(walk: Dict[str, Any], P: List[str], visited: set) -> Tuple[int, int, str]:
    """The part of P the walk went over: all of it after a full lap; from where it
    started to the end it reached (an edge); else between the stops it reached."""
    idx = [i for i, k in enumerate(P) if k in visited]
    if not idx:
        return 0, -1, ""
    if _lap_complete(walk):
        return 0, len(P) - 1, "in a full lap"
    first = next((_pk(s) for s in _moves(walk["steps"]) if _pk(s) in P), None)
    start = P.index(first) if first is not None else min(idx)
    if any(s.get("edge") for s in _first_screen(walk["steps"])):
        if walk.get("direction", "next") == "next":
            return start, len(P) - 1, "from the start to the edge"
        return 0, start, "from the start back to the edge"
    return min(idx), max(idx), "between the stops it did reach"


def _unvisited(walk: Dict[str, Any], P: List[str], visited: set, covered_windows: set) -> List[str]:
    """Predicted stops never visited, where the walk went over them (:func:`_coverage`)."""
    pwin = {p["key"]: p.get("window") for p in walk.get("predicted") or []}
    lo, hi, _scope = _coverage(walk, P, visited)
    return [k for k in P[lo:hi + 1] if k not in visited and pwin.get(k) not in covered_windows]


def _check_skipped(walk: Dict[str, Any]) -> List[Dict[str, Any]]:
    P = [p["key"] for p in walk.get("predicted") or []]
    pref = {p["key"]: p for p in walk.get("predicted") or []}
    visited = {_pk(s) for s in _moves(_first_screen(walk["steps"]))}
    covered = {s.get("window") for s in walk["steps"] if s.get("window_covered_by") is not None}
    miss = _unvisited(walk, P, visited, covered)
    if not miss:
        return []
    names = ", ".join(f"{pref[k]['ref']} {_q(pref[k]['label'])}" for k in miss[:3])
    more = f" (+{len(miss) - 3} more)" if len(miss) > 3 else ""
    scope = _coverage(walk, P, visited)[2]
    return [_finding("tb.skipped", "warn",
                     f"TalkBack never reached {len(miss)} predicted stop(s) {scope}: {names}{more}",
                     refs=[pref[k]["ref"] for k in miss], keys=miss)]


def _ghost_reasons(s: Dict[str, Any], density: int) -> List[str]:
    if s.get("show_on_screen"):
        # A model stop TalkBack first scrolls fully into view: what it shows and says
        # (its clipped text) is known only after the scroll.
        return []
    reasons = []
    speak = (s.get("speak") or "").strip()
    # TalkBack 17 says just the role ("Button") for an unlabelled control, 16.2 "Unlabelled".
    words_beyond_role = _tokens(speak) - _ROLE_STATE_WORDS
    if _UNLABELLED.search(speak) or (not (s.get("label") or "").strip() and not words_beyond_role):
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
    if s.get("under_system_bar"):
        reasons.append("under a system bar")  # outside the window's interactive region
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
                if "ancestors" in inner and outer.get("ref") not in inner["ancestors"]:
                    prev = s  # only overlapping (a scrim over a sheet): not a container
                    continue
                # The inner stop's own words (its label; role and state words aside)
                # against what the outer stop says (its label joins its children's
                # texts, which TalkBack does not speak when they are stops of their own).
                ti = _tokens(inner.get("label") or inner.get("speak")) - _ROLE_STATE_WORDS
                to = _tokens(outer.get("speak") or outer.get("label"))
                overlap = len(ti & to) / len(ti) if ti else 0.0
                both = "clickable" in (outer.get("flags") or []) and \
                    "clickable" in (inner.get("flags") or [])
                if overlap >= DOUBLE_STOP_OVERLAP:
                    out.append(_finding(
                        "tb.double_stop", "warn",
                        f"steps {prev['i']}-{s['i']}: {_name(outer)} and {_name(inner)} inside it are "
                        f"both stops, and {int(overlap * 100)}% of the inner one's words are already "
                        f"spoken at the outer one", [prev, s]))
                elif both:
                    out.append(_finding(
                        "tb.double_stop", "warn",
                        f"steps {prev['i']}-{s['i']}: {_name(outer)} and {_name(inner)} inside it are "
                        f"both clickable stops: one item takes two swipes, and activating the outer "
                        f"one may not do what the inner control does", [prev, s]))
        prev = s
    return out[:5]


def _check_escape(walk: Dict[str, Any]) -> List[Dict[str, Any]]:
    """tb.escape: a step in a window under a modal one, or behind a same-window overlay the
    walk was inside before. Consecutive escaped steps out of the same overlay are one finding
    (every step named, so a drawing marks them all)."""
    runs: List[Tuple[Any, Dict[str, Any], List[Dict[str, Any]]]] = []  # (why, cover, steps)
    moves = _moves(walk["steps"])
    for j, s in enumerate(moves):
        why: Any = None
        if s.get("window_covered_by") is not None:
            why, first = ("window", s["window_covered_by"]), s
        else:
            cov = s.get("covered_by")
            if not cov or not cov.get("rect"):
                continue
            orect = tuple(cov["rect"])
            inside = [m for m in moves[:j] if not m.get("covered_by") and _rect(m)
                      and _contains(orect, _rect(m))]  # type: ignore[arg-type]
            if not inside:
                continue
            why, first = ("overlay", orect), inside[-1]
        s["_escape"] = True
        prev = moves[j - 1] if j else None
        if runs and runs[-1][0] == why and prev is not None and prev is runs[-1][2][-1]:
            runs[-1][2].append(s)
        else:
            runs.append((why, first, [s]))
    out = []
    for why, first, steps in runs:
        a, b = steps[0], steps[-1]
        at = f"step {a['i']}" if a is b else f"steps {a['i']}-{b['i']}"
        n = f"{len(steps)} stops" if len(steps) > 1 else "1 stop"
        if why[0] == "window":
            msg = (f"{at}: focus read {n} in a window under the modal window {why[1]}, "
                   f"first {_name(a)}")
            out.append(_finding("tb.escape", "error", msg, steps))
            continue
        cov = a["covered_by"]
        msg = (f"{at}: focus left the overlay {cov.get('ref') or cov.get('overlay')} "
               f"({cov.get('cls')}, {int(100 * cov.get('area', 0))}% of the window) and read "
               f"{n} behind it, first {_name(a)}")
        out.append(_finding("tb.escape", "error", msg, [first, *steps]))
    return out[:5]


def _capture_order(seg: List[Dict[str, Any]], keys: set) -> Optional[Tuple[List[str], str]]:
    """V from the walk's capture (``vrank``: capture/walks.py binds each step to the place
    tb.out_of_order gives it, from the View tree and the semantics groups) when every stop
    of the segment has one, in one capture, window and layer; else None (the XY-cut over
    the steps' boxes decides)."""
    ranks: Dict[str, Any] = {}
    for s in seg:
        if s["key"] in keys and s["key"] not in ranks:
            ranks[s["key"]] = s.get("vrank")
    if not ranks or any(r is None for r in ranks.values()):
        return None
    if len({tuple(r[:3]) for r in ranks.values()}) != 1:
        return None
    return sorted(ranks, key=lambda k: ranks[k][3]), "capture"


def _check_order(walk: Dict[str, Any], lap: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Out-of-order stops per screen state: the lap is split where the screen
    changed (auto-scroll, another window, a stolen focus) or focus escaped an
    overlay (tb.escape says that); inside each segment the stops outside the
    longest run that follows V are out of order."""
    segments: List[List[Dict[str, Any]]] = [[]]
    for s in lap:
        escaped = bool(s.get("_escape")) != bool(segments[-1] and segments[-1][-1].get("_escape"))
        if (s.get("via") in ("autoscroll", "window", "stolen", "screen") or escaped) \
                and segments[-1]:
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
        v, src = _capture_order(seg, {k for k, _r in items}) or \
            visual_order(items)  # type: ignore[arg-type]
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
    for s in steps:
        if s.get("via") == "lost":
            prev = next((p for p in reversed(steps[:s["i"]]) if p.get("key")), None)
            out.append(_finding(
                "tb.focus_lost", "warn",
                f"step {s['i']}: after {_name(prev) if prev else 'the start'}, no node held "
                f"accessibility focus within the step timeout"
                + (f" ({s['scrolled']} scrolled: the focused item was disposed)" if s.get("scrolled")
                   else "") + "; the next swipe starts over from the top", [prev] if prev else []))
    edge = walk.get("edge") or {}
    if edge.get("hidden_after") and last is not None and not edge.get("can_scroll"):
        out.append(_finding("tb.edge_stuck", "warn",
                            f"TalkBack hit the edge at {_name(last)} with {edge['hidden_after']} "
                            f"hidden item(s) after it ({_q(edge.get('hidden_first'))}…): they are "
                            f"clipped, and nothing TalkBack can scroll brings them in", [last]))
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


def _check_trap(walk: Dict[str, Any]) -> List[Dict[str, Any]]:
    stolen = [s for s in walk["steps"] if s.get("via") == "stolen"]
    if not stolen:
        return []
    firsts = _uniq([s.get("ref") for s in stolen])
    return [_finding("tb.trap", "warn",
                     f"the app pulled accessibility focus to {_name(stolen[0])} between presses "
                     f"{len(stolen)} time(s) (it moves input or accessibility focus on its own)", stolen,
                     refs=firsts)]


def _check_revisit(walk: Dict[str, Any], lap: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Dict[str, Dict[str, Any]] = {}
    out = []
    for s in lap:
        if s.get("via") == "stolen":
            seen = {}  # the app sent focus back: what follows is read again because of that (tb.trap)
        k = _pk(s)
        if k in seen and seen[k]["i"] != s["i"] - 1:
            out.append(_finding("tb.revisit", "warn",
                                f"step {s['i']}: {_name(s)} was already read at step {seen[k]['i']} "
                                f"in this lap", [seen[k], s]))
        seen.setdefault(k, s)
    return out[:3]


_FORWARD = {"forward", "down", "right", "page_down", "page_right"}
_BACKWARD = {"backward", "up", "left", "page_up", "page_left"}


def _check_leave_scrollable(walk: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Focus left a scrollable that can still scroll the way the walk goes: TalkBack
    did not (or could not: a pager) scroll it, so the rest is unreachable by swipe."""
    want = _FORWARD if walk.get("direction", "next") == "next" else _BACKWARD
    out = []
    prev: Optional[Dict[str, Any]] = None
    for s in walk["steps"]:
        if not s.get("moved") or not s.get("key") or s.get("edge"):
            prev = None if s.get("via") == "left_app" else prev
            continue
        if prev is not None and s.get("via") in ("next", "autoscroll", "window") and prev.get("container") \
                and s.get("container") != prev.get("container") \
                and set(prev.get("container_can") or []) & want:
            inside = s.get("container_rect") and prev.get("container_rect") and _rect(s) and \
                _contains(tuple(prev["container_rect"]), _rect(s))  # type: ignore[arg-type]
            if not inside:
                out.append(_finding(
                    "tb.edge_stuck", "warn",
                    f"steps {prev['i']}-{s['i']}: focus left {prev['container']} "
                    f"({prev.get('container_cls')}) while it can still scroll "
                    f"{'/'.join(sorted(set(prev['container_can']) & want))}: TalkBack did not scroll "
                    f"it, so the rest of it is unreachable by swipe", [prev, s],
                    refs=[prev["container"]], keys=[prev["container"]]))
        prev = s
    return out[:3]


_N_OF_M = re.compile(r"\b(\d+) of (\d+)\b")


def _check_n_of_m(walk: Dict[str, Any]) -> List[Dict[str, Any]]:
    """TalkBack's "N of M" (CollectionItemInfo) on the first row of a list says N > 1:
    the position counts an item it never stops on (an empty header)."""
    out = []
    firsts: Dict[str, Dict[str, Any]] = {}
    for s in _moves(walk["steps"]):
        m = _N_OF_M.search(s.get("speak") or "") if s.get("utt") == "logcat" else None
        c = s.get("container")
        if m is None or not c or c in firsts or s.get("via") == "autoscroll":
            continue
        firsts[c] = s
        r, cr = _rect(s), s.get("container_rect")
        n, total = int(m.group(1)), int(m.group(2))
        if n > 1 and r is not None and cr and r[1] - cr[1] < r[3]:
            out.append(_finding(
                "tb.wrong_announcement", "warn",
                f"step {s['i']}: the first row of {c} is announced \"{n} of {total}\": "
                f"{n - 1} item(s) before it count but TalkBack never stops on them", [s]))
    return out[:3]


def _check_speech_order(walk: Dict[str, Any]) -> List[Dict[str, Any]]:
    """A stop that joins its children's texts (a merged row) reads them out of
    their visual order: "$5, Socks" for a row showing "Socks ... $5"."""
    out = []
    for s in _moves(walk["steps"]):
        parts = s.get("parts") or []
        speak = (s.get("speak") or "").lower()
        if len(parts) < 2 or not speak:
            continue
        at = [speak.find(p["text"].lower()) for p in parts]
        if min(at) < 0 or len(set(at)) < len(at):
            continue  # a part TalkBack did not say (or said inside another): no order to compare
        spoken = [p["text"] for _i, p in sorted(zip(at, parts, strict=True), key=lambda t: t[0])]
        seen, _src = visual_order([(p["text"], tuple(p["rect"])) for p in parts])  # type: ignore[misc]
        if spoken != seen:
            f = _finding("tb.wrong_announcement", "warn",
                         f"step {s['i']}: {_name(s)} reads {_q(spoken[0])} before {_q(seen[0])}, "
                         f"though {_q(seen[0])} comes first on screen (the row joins its texts in "
                         f"child order: {' / '.join(_q(t) for t in spoken[:4])})", [s])
            f["fix"] = _FIX_SPEECH_ORDER
            out.append(f)
    return out[:3]


def _check_cut_off_end(walk: Dict[str, Any]) -> List[Dict[str, Any]]:
    """At the edge, the last stop is cut off by its parent (shorter than its
    siblings, flush with the parent's bottom) and nothing around it scrolls: more
    content continues below that TalkBack cannot reach."""
    steps = walk["steps"]
    edge_at = next((i for i, s in enumerate(steps) if s.get("edge")), None)
    if edge_at is None or walk.get("direction", "next") != "next":
        return []
    last = next((s for s in reversed(steps[:edge_at]) if s.get("moved") and s.get("key")), None)
    if last is None or last.get("container") or not last.get("parent_rect") or not _rect(last):
        return []
    x, y, w, h = _rect(last)  # type: ignore[misc]
    px, py, pw, ph = last["parent_rect"]
    siblings = sorted(_rect(s)[3] for s in _moves(steps[:edge_at])  # type: ignore[index]
                      if s.get("parent_rect") == last["parent_rect"] and s is not last and _rect(s))
    if len(siblings) < 2:
        return []
    typical = siblings[len(siblings) // 2]
    if y + h >= py + ph - 2 and h < 0.9 * typical:
        return [_finding("tb.edge_stuck", "warn",
                         f"step {last['i']}: {_name(last)} is cut off at the bottom of its parent "
                         f"({h}px of a usual {typical}px) and nothing scrolls: the content below it "
                         f"is unreachable", [last])]
    return []


def _check_window_order(walk: Dict[str, Any]) -> List[Dict[str, Any]]:
    """A window TalkBack reads after the content it sits over (a non-focusable popup
    sorted by its top edge): focus reached it only after the stops below its top."""
    out = []
    read: List[Dict[str, Any]] = []
    for s in first_lap(walk["steps"]) + [s for s in walk["steps"] if s.get("via") == "window"]:
        if s.get("via") == "window" and s.get("window_rect") and read:
            top = s["window_rect"][1]
            below = [r for r in read if r.get("window") != s.get("window") and _rect(r)
                     and _rect(r)[1] >= top]  # type: ignore[index]
            if below:
                out.append(_finding(
                    "tb.window_order", "warn",
                    f"step {s['i']}: window {s.get('window_ref') or s.get('window')} (from "
                    f"y={top}) is read only after "
                    f"{len(below)} stop(s) of the window under it that sit lower on screen, e.g. "
                    f"{_name(below[0])}", [below[0], s]))
                break
        if s.get("moved") and s.get("key"):
            read.append(s)
    return out


def _check_orphans(walk: Dict[str, Any]) -> List[Dict[str, Any]]:
    orphans = walk.get("orphans") or []
    if not orphans:
        return []
    names = ", ".join(_q(o["text"]) for o in orphans[:3])
    more = f" (+{len(orphans) - 3} more)" if len(orphans) > 3 else ""
    return [_finding("tb.skipped", "warn",
                     f"{len(orphans)} text(s) on screen that no stop of the lap read: {names}{more}",
                     refs=[o["key"] for o in orphans if o.get("key")],
                     keys=[o["key"] for o in orphans if o.get("key")])]


def analyze(walk: Dict[str, Any], expect: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Findings + the model comparison for one walk record (see :mod:`.walk`)."""
    steps = walk.get("steps") or []
    for s in steps:
        s.pop("_escape", None)
    lap = first_lap(steps)
    findings: List[Dict[str, Any]] = []
    findings += _check_end(walk)
    findings += _check_trap(walk)
    findings += _check_revisit(walk, lap)
    findings += _check_leave_scrollable(walk)
    findings += _check_n_of_m(walk)
    findings += _check_speech_order(walk)
    findings += _check_cut_off_end(walk)
    findings += _check_window_order(walk)
    findings += _check_orphans(walk)
    findings += _check_escape(walk)
    findings += _check_skipped(walk)
    findings += _check_ghosts(walk)
    findings += _check_double(walk)
    exp_res = None
    vmeta: Dict[str, Any] = {}
    if not expect:  # an expected order replaces the visual heuristic
        order, vmeta = _check_order(walk, lap)
        findings += order
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
