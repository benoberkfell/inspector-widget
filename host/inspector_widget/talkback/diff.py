"""Compare a TalkBack walk (A, actual) with the model (P, predicted) and a visual
reading order (V), and classify what a walk can show (design part 2).

Pure: it works on the walk record :mod:`.walk` saves (steps, predicted stops,
how the walk ended), so a stored walk can be re-analysed offline.

Codes: ``tb.out_of_order``, ``tb.loop``, ``tb.trap`` (the app took focus
between presses, or a WebView on an off-screen page keeps it), ``tb.revisit`` (a stop read
twice in one lap),
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
                      "(Compose: Modifier.toggleable/clickable on the row, child onClick=null; "
                      "View: the inner control as an AccessibilityAction on the row, "
                      "ViewCompat.addAccessibilityAction, with importantForAccessibility=no on "
                      "it) or make the container not focusable.",
    "tb.focus_lost": "Keep the focused item alive while it scrolls (stable keys / "
                     "LazyListState, no key churn); TalkBack re-focuses only after a scroll "
                     "event from the container.",
    "tb.trap": "Move accessibility focus once (on arrival: LaunchedEffect(Unit)), not on every "
               "recomposition or timer tick: each ACTION_ACCESSIBILITY_FOCUS (or "
               "TYPE_VIEW_ACCESSIBILITY_FOCUSED) the app sends pulls TalkBack back. (TalkBack 17 "
               "does not follow requestFocus() input focus, so the steal is an explicit one.)",
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
    "tb.webview_block": "TalkBack cannot put focus on the WebView's page (its focus action "
                        "brings no focus event). A WebView created before TalkBack started can "
                        "stay closed to it until it is created again: rerun with TalkBack "
                        "started first (relaunch) to see what a TalkBack user gets; and give the "
                        "page a native way in (a button that focuses the WebView).",
    "tb.interleaved": "Make each card one traversal group (Compose: Modifier.semantics { "
                      "isTraversalGroup = true } on the card; Views: a focusable card, or "
                      "accessibilityTraversalBefore/After), or one stop whose controls are "
                      "custom actions, so a card's controls are read with it.",
    "tb.autoscroll_row_skip": "Lay the items out so the reading order follows the data and "
                              "scrolling is vertical (a FlowRow, or rows in a vertical list), "
                              "or make the grid a traversal group with traversalIndex = index "
                              "per item, so TalkBack reads column by column as it scrolls.",
    "tb.covered_stop": "While the overlay is shown, hide what it covers from accessibility "
                       "(View: importantForAccessibility=noHideDescendants on the covered "
                       "View, restored when it goes; Compose: hideFromAccessibility), and move "
                       "accessibility focus into the overlay; or let the overlay replace it in "
                       "the hierarchy (an action mode without windowActionModeOverlay).",
}
#: the overlays focus can be inside of and walk out of (tb.escape); the rest only cover
#: (tb.covered_stop: an action-mode bar over the toolbar)
ESCAPE_KINDS = ("scrim", "sheet", "drawer", None)
# tb.double_stop where both stops are Views / Compose nodes (FIXES names both).
FIX_DOUBLE_VIEW = ("Make one of the two the stop: expose the inner control as an "
                   "AccessibilityAction on the row (ViewCompat.addAccessibilityAction) and set "
                   "importantForAccessibility=no on it, or make the row not focusable.")
FIX_DOUBLE_COMPOSE = ("Make one of the two the stop: merge the child into the container "
                      "(Modifier.toggleable/clickable on the row, child onClick=null; secondary "
                      "actions as customActions) or make the container not focusable.")
# tb.loop driven by TalkBack's own auto-scroll (FIXES has the traversal-cycle one).
FIX_AUTOSCROLL_LOOP = ("Make each item one stop: its inner controls as custom actions (Compose: "
                       "customActions on the item + clearAndSetSemantics {} on the child; Views: "
                       "ViewCompat.addAccessibilityAction on the row + importantForAccessibility=no "
                       "on the child), so TalkBack never scrolls back to show a control of a "
                       "partly visible item.")
# tb.trap for a WebView on an off-screen page (FIXES has the focus-stealing one).
FIX_WEB_TRAP = ("Keep pages that are not on screen out of the accessibility tree: "
                "importantForAccessibility=noHideDescendants on the pager pages that are not "
                "current (AUTO on the current one), or keep the WebView GONE/INVISIBLE until its "
                "page is shown. TalkBack never scrolls a pager, so it cannot bring that page into "
                "view itself.")
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


def web_trap_finding(hidden: Dict[str, Any], steps: Sequence[Dict[str, Any]] = (),
                     basis: str = "walk") -> Dict[str, Any]:
    """The finding for talkback.order's ``web_hidden_page`` diagnostic: tb.trap (error) when
    TalkBack cannot focus that WebView (``trap``: a walk stuck there, or the model's
    prediction), else tb.ghost_stop (warn): TalkBack reads a page nobody can see."""
    keys = _uniq([hidden.get("before"), hidden.get("web_root")])
    msg = hidden["message"]
    if steps:
        msg = (f"TalkBack stopped at {_name(steps[-1])}: the next stop is the WebView "
               f"{hidden.get('web_root')}, whose page is off screen. " + msg)
    code, sev = ("tb.trap", "error") if hidden.get("trap") or steps else ("tb.ghost_stop", "warn")
    f = _finding(code, sev, msg, steps, basis=basis,
                 refs=[s.get("ref") for s in steps] or keys, keys=keys)
    f["fix"] = FIX_WEB_TRAP
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
    return f"{s.get('ref')} {_q((s.get('label') or '').strip() or s.get('speak'))}"


COLLAPSE_AT = 3  # more findings of one pattern than this are reported as one


def _collapse(found: List[Dict[str, Any]], what: str) -> List[Dict[str, Any]]:
    """Repeats of one pattern (a double stop on every row) as one finding: the first one's
    message, how many there are and where the others are; every step is kept, so each line
    of the walk is tagged (no row looks fine because a cap dropped its finding)."""
    if len(found) <= COLLAPSE_AT:
        return found
    f = dict(found[0])

    def at(x: Dict[str, Any]) -> str:
        st = x.get("steps") or []
        return f"step {st[0]}" if len(st) == 1 else f"steps {st[0]}-{st[-1]}"

    rest = found[1:]
    also = ", ".join(at(x) for x in rest[:5]) + (f" +{len(rest) - 5}" if len(rest) > 5 else "")
    f["msg"] = f"{len(found)} {what}, the same pattern; first: {f['msg']}; also {also}"
    f["count"] = len(found)
    f["refs"] = _uniq([r for x in found for r in x.get("refs") or []])[:MAX_REFS]
    f["keys"] = _uniq([k for x in found for k in x.get("keys") or []])[:MAX_REFS]
    f["steps"] = sorted({i for x in found for i in x.get("steps") or []})
    return [f]


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
    """Moves up to the first edge or wrap (the lap the order checks look at). A walk that
    meets the edge before any move (backwards from the first stop) compares the lap after
    it: from the stop the wrap lands on."""
    out: List[Dict[str, Any]] = []
    for s in steps:
        if s.get("edge") or s.get("via") in ("wrap", "left_app", "lost", "screen"):
            if len(out) <= 1 and s.get("via") not in ("left_app", "lost", "screen"):
                out = [s] if s.get("via") == "wrap" and s.get("moved") and s.get("key") else []
                continue
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


def with_abbreviations(spoken: set) -> set:
    """``spoken`` (lower-case words) plus every prefix of 3 or more letters of each: a text
    that abbreviates a word said was read ("Aug 5" under a row that says "August 5, 2026":
    AntennaPod). The static orphan check (static._unspoken) and the walk's (walk.orphan_text)
    share it, so the two agree."""
    return spoken | {w[:k] for w in spoken for k in range(3, len(w))}


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
    visited = _visited(walk)
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


def _visited(walk: Dict[str, Any]) -> set:
    """Every key the walk read on its first screen: the node's own and the model's for it."""
    out: set = set()
    for s in _moves(_first_screen(walk["steps"])):
        out.add(s.get("key"))
        out.add(_pk(s))
    return out


def _full_lap(walk: Dict[str, Any]) -> bool:
    """A full lap, really: the walk ended on the wrap (back on a stop it read), or after a
    wrap it read again a stop it had read before it (a walk that wrapped on its last press,
    or two presses before its end, went over only part of the screen twice)."""
    steps = _first_screen(walk["steps"])
    if len(steps) == len(walk["steps"]) and walk.get("ended") == "wrap":
        return True
    before: set = set()
    wrapped = False
    for s in steps:
        if s.get("via") == "wrap":
            wrapped = True
        if not s.get("moved") or not s.get("key") or s.get("edge"):
            continue
        if wrapped and (s.get("key") in before or _pk(s) in before) and s.get("via") != "wrap":
            return True
        if not wrapped:
            before |= {s.get("key"), _pk(s)}
    return False


def _coverage(walk: Dict[str, Any], P: List[str], visited: set
              ) -> Tuple[List[Tuple[int, int]], str]:
    """The parts of P the walk went over (``[(lo, hi)]``, inclusive) and how to say it: all
    of it after a full lap (:func:`_full_lap`); from where it started to the edge it reached
    (and, after a wrap, from the top to where it stopped); else between the stops it reached.
    The presses of a stuck walk that moved nothing are no edge."""
    steps = _first_screen(walk["steps"])
    if not any(k in visited for k in P):
        return [], ""
    if _full_lap(walk):
        return [(0, len(P) - 1)], "in a full lap"
    fwd = walk.get("direction", "next") == "next"
    tail = len(steps)
    if walk.get("ended") == "stuck":
        while tail and (steps[tail - 1].get("edge") or not steps[tail - 1].get("moved")):
            tail -= 1  # the presses that moved nothing at the end: stuck, not an edge
    segs: List[List[int]] = [[]]
    edges: List[bool] = [False]
    for s in steps[:tail]:
        if s.get("via") == "wrap":
            segs.append([])
            edges.append(False)
        if s.get("edge"):
            edges[-1] = True
            continue
        if s.get("moved") and s.get("key"):
            k = _pk(s) if _pk(s) in P else s.get("key")
            if k in P:
                segs[-1].append(P.index(k))
    spans: List[Tuple[int, int]] = []
    for j, (seg, edge) in enumerate(zip(segs, edges, strict=True)):
        if not seg:
            continue
        lo, hi = min(seg), max(seg)
        if edge:  # went on to the edge
            if fwd:
                hi = len(P) - 1
            else:
                lo = 0
        if j > 0:  # came round from the other end after the wrap
            if fwd:
                lo = 0
            else:
                hi = len(P) - 1
        spans.append((lo, hi))
    if not spans:
        return [], ""
    if len(spans) > 1:
        scope = "from the start to the edge and round again to where it stopped"
    elif any(edges):
        scope = "from the start to the edge" if fwd else "from the start back to the edge"
    else:
        scope = "between the stops it did reach"
    return spans, scope


def _unvisited(walk: Dict[str, Any], P: List[str], visited: set, covered_windows: set) -> List[str]:
    """Predicted stops never visited, where the walk went over them (:func:`_coverage`)."""
    pwin = {p["key"]: p.get("window") for p in walk.get("predicted") or []}
    spans, _scope = _coverage(walk, P, visited)
    seen: set = set()
    out = []
    for lo, hi in spans:
        for k in P[lo:hi + 1]:
            if k not in visited and k.split("#")[0] not in visited and k not in seen \
                    and pwin.get(k) not in covered_windows:
                seen.add(k)
                out.append(k)
    return out


def _check_skipped(walk: Dict[str, Any], explained: Optional[set] = None) -> List[Dict[str, Any]]:
    """Predicted stops the walk went past and never read (tb.skipped, warn). A stop the model
    added on a re-model (TalkBack scrolled something new in) where the walk had already been
    (a collapsing toolbar's title that appears once the list scrolls) is the model's late
    knowledge, not a skip: basis model, info (L4)."""
    P = [p["key"] for p in walk.get("predicted") or []]
    pref = {p["key"]: p for p in walk.get("predicted") or []}
    visited = _visited(walk)
    covered = {s.get("window") for s in walk["steps"] if s.get("window_covered_by") is not None}
    miss = [k for k in _unvisited(walk, P, visited, covered) if k not in (explained or ())]
    if not miss:
        return []
    remodels = [s for s in walk["steps"] if s.get("remodel")]
    fwd = walk.get("direction", "next") == "next"
    late = []
    for k in miss:
        a = pref[k].get("added")
        if not a or a > len(remodels):
            continue
        at = remodels[a - 1]  # the step whose node the model learned the stop from
        here = _pk(at) if _pk(at) in P else at.get("key")
        if here in P and (P.index(k) < P.index(here) if fwd else P.index(k) > P.index(here)):
            late.append(k)
    real = [k for k in miss if k not in late]
    scope = _coverage(walk, P, visited)[1]
    out = []

    def names(ks: List[str]) -> str:
        more = f" (+{len(ks) - 3} more)" if len(ks) > 3 else ""
        return ", ".join(f"{pref[k]['ref']} {_q(pref[k]['label'])}" for k in ks[:3]) + more

    if real:
        out.append(_finding("tb.skipped", "warn",
                            f"TalkBack never reached {len(real)} predicted stop(s) {scope}: "
                            f"{names(real)}", refs=[pref[k]["ref"] for k in real], keys=real))
    if late:
        f = _finding("tb.skipped", "info",
                     f"{len(late)} stop(s) the model learned of only after the walk had passed "
                     f"where they go (a re-model when TalkBack scrolled: e.g. a collapsing "
                     f"toolbar's title): {names(late)}; not judged", basis="model",
                     refs=[pref[k]["ref"] for k in late], keys=late)
        f.pop("fix", None)
        out.append(f)
    return out


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
        box = s.get("container_rect")  # the scrollable or pager it sits in: its viewport
        if w <= 0 or h <= 0 or (win and not _intersects(r, tuple(win))) \
                or (box and box[2] > 0 and box[3] > 0
                    and not _intersects(r, tuple(box))):  # type: ignore[arg-type]
            # outside the window, or past the edge of the pager or list it is in (a WebView's
            # page clipped away with the pager page holding it: AntennaPod's player, AP-3)
            reasons.append("offscreen")
        elif _dp(min(w, h), density) < TINY_DP:
            reasons.append("tiny")
        elif _dp(min(w, h), density) < SLIVER_DP:
            reasons.append("sliver")
    if s.get("under_system_bar"):
        reasons.append("under a system bar")  # outside the window's interactive region
    # drawn under an overlay: tb.covered_stop (or tb.escape) says so, with the overlay's fix
    return reasons


def _check_ghosts(walk: Dict[str, Any]) -> List[Dict[str, Any]]:
    density = int(walk.get("density") or 420)
    out = []
    seen: set = set()  # a stop read again (the lap wrapped back to it) is reported once
    for s in _moves(walk["steps"]):
        k = _pk(s)
        if k in seen:
            continue
        reasons = _ghost_reasons(s, density)
        if reasons:
            seen.add(k)
            r = _rect(s) or (0, 0, 0, 0)
            out.append(_finding("tb.ghost_stop", "warn",
                                f"step {s['i']}: {_name(s)} is a stop but is "
                                + " + ".join(reasons) + f" ({r[2]}x{r[3]}px"
                                + (f", said {_q(s.get('speak'), 40)}" if s.get("utt") == "logcat" else "")
                                + ")", [s]))
    return _collapse(out, "ghost stops")


#: Controls that hold a state of their own (checked, on): inside a clickable row or card,
#: the row should be the one toggleable stop.
_STATE_CLASSES = ("CheckBox", "Switch", "RadioButton", "ToggleButton", "CompoundButton",
                  "SwitchCompat", "SwitchMaterial")
_STATE_ROLE = re.compile(r"(?:^|\. )(?:check box|switch|radio button|toggle button)(?:\.|$)")


def _state_control(s: Dict[str, Any]) -> bool:
    """Whether walk step ``s`` is a state control: its class (a CheckBox, a Switch, a
    RadioButton: Compose reports its role as one of them), ``checkable``, or the role
    TalkBack spoke ("ON. Switch", "checked. Check box")."""
    cls = str(s.get("cls") or "").rsplit(".", 1)[-1]
    if any(cls.endswith(c) for c in _STATE_CLASSES) or "checkable" in (s.get("flags") or ()):
        return True
    return bool(_STATE_ROLE.search(str(s.get("speak") or "").lower()))


def _check_double(walk: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    prev: Optional[Dict[str, Any]] = None
    for s in walk["steps"]:
        if not s.get("moved") or s.get("edge") or not s.get("key"):
            prev = None
            continue
        if prev is not None and s.get("via") in ("next", "autoscroll", "start", "late"):
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
                if outer.get("cls") == "EditText" or "edit box" in (outer.get("speak") or "").lower():
                    prev = s  # a text field and its clear button: two things to do, not a defect
                    continue
                f = None
                if overlap >= DOUBLE_STOP_OVERLAP:
                    f = _finding(
                        "tb.double_stop", "warn",
                        f"steps {prev['i']}-{s['i']}: {_name(outer)} and {_name(inner)} inside it are "
                        f"both stops, and {int(overlap * 100)}% of the inner one's words are already "
                        f"spoken at the outer one", [prev, s])
                elif both:
                    # an inner Checkbox / Switch is the canonical double stop: the row should
                    # be the one toggleable stop (warn, as the capture lint says); an inner
                    # control that does something else (play, download, follow) is a row with
                    # a secondary action, worth a custom action, not a defect (info)
                    f = _finding(
                        "tb.double_stop", "warn" if _state_control(inner) else "info",
                        f"steps {prev['i']}-{s['i']}: {_name(outer)} and {_name(inner)} inside it are "
                        f"both clickable stops: one item takes two swipes, and activating the outer "
                        f"one may not do what the inner control does", [prev, s])
                if f is not None:
                    kinds = {str(x.get("key") or "").split(":", 1)[0] for x in (outer, inner)}
                    if kinds == {"view"}:
                        f["fix"] = FIX_DOUBLE_VIEW
                    elif kinds == {"compose"}:
                        f["fix"] = FIX_DOUBLE_COMPOSE
                    out.append(f)
        prev = s
    return _collapse(out, "double stops")


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
            if not cov or not cov.get("rect") or cov.get("kind") not in ESCAPE_KINDS:
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
            f = _finding("tb.escape", "error", msg, steps)
            f["overlay"] = str(why[1])
            out.append(f)
            continue
        cov = a["covered_by"]
        msg = (f"{at}: focus left the overlay {cov.get('ref') or cov.get('overlay')} "
               f"({cov.get('cls')}, {int(100 * cov.get('area', 0))}% of the window) after "
               f"{_name(first)} and read {n} behind it, first {_name(a)}")
        # the escaped stops only: the last stop read inside the overlay did nothing wrong
        f = _finding("tb.escape", "error", msg, steps)
        f["overlay"] = cov.get("ref") or cov.get("overlay")
        f["from"] = first.get("ref")
        out.append(f)
    return out[:5]


def _check_covered(walk: Dict[str, Any], escapes: Sequence[Dict[str, Any]] = ()
                   ) -> List[Dict[str, Any]]:
    """tb.covered_stop: stops something in their own window draws over (an action-mode bar
    over the toolbar, a sheet the walk never was inside of) that TalkBack read anyway: one
    finding per overlay, every step named. An overlay focus escaped (tb.escape) is that
    finding's: the stops behind it read before going in are the same defect."""
    escaped = {f.get("overlay") for f in escapes}
    groups: Dict[Any, List[Dict[str, Any]]] = {}
    for s in _moves(walk["steps"]):
        cov = s.get("covered_by")
        if not cov or s.get("_escape") or s.get("window_covered_by") is not None:
            continue
        over = cov.get("ref") or cov.get("overlay")
        if over in escaped:
            continue
        groups.setdefault(over, []).append(s)
    out = []
    for overlay, steps in groups.items():
        cov = steps[0]["covered_by"]
        seen: List[Dict[str, Any]] = []
        for s in steps:  # a stop read again (a wrap) is the same covered stop
            if _pk(s) not in [_pk(x) for x in seen]:
                seen.append(s)
        a, b = seen[0], seen[-1]
        at = f"step {a['i']}" if a is b else f"steps {a['i']}-{b['i']}"
        what = cov.get("kind") or "overlay"
        f = _finding("tb.covered_stop", "warn",
                     f"{at}: TalkBack read {len(seen)} stop(s) that {overlay} ({cov.get('cls')}, "
                     f"a {what}) draws over, first {_name(a)}: hidden on screen, reachable "
                     f"only by swiping", seen)
        f["overlay"] = overlay
        out.append(f)
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
            why = ""
            overlays = {(x.get("covered_by") or {}).get("ref") or (x.get("covered_by") or {}).get(
                "overlay") for x in walk["steps"] if x.get("covered_by")}
            inside = next((o for o in overlays if o and o in (first.get("ancestors") or [])), None)
            if inside is not None:
                # the overlay drawn over the stops read first (an action-mode bar over the
                # toolbar) is added at the end of the View tree: its own stops come last
                why = f": inside {inside}, the overlay over the stops read first, added last"
            out.append(_finding(
                "tb.out_of_order", "warn",
                f"{len(bad)} of {len(seq_steps)} stops are read out of visual order; first: step "
                f"{first['i']} {_name(first)} (visual position {rank[first['key']] + 1} of "
                f"{len(v)}){why}", bad))
    return out, {"visual": "+".join(sorted(sources)) or None}


def _check_end(walk: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    steps = walk["steps"]
    if walk.get("ended") == "loop" and walk.get("cycle"):
        cyc = walk["cycle"]
        in_cycle = [s for s in steps if s.get("ref") in cyc]
        path = " > ".join(cyc[:8]) + (" > …" if len(cyc) > 8 else "")
        auto = next((s for s in in_cycle if s.get("via") == "autoscroll"), None)
        if auto is not None:
            # TalkBack's own auto-scroll drives the cycle (C15, NiA's For you grid): it scrolls
            # back to show a control of a partly visible item, then forward again
            box = auto.get("scrolled") or auto.get("container") or "its list"
            f = _finding("tb.loop", "error",
                         f"TalkBack auto-scrolls {box} back to show a control of a partly "
                         f"visible item, then forward again: focus cycles through {len(cyc)} "
                         f"stops without reaching an edge: {path}",
                         in_cycle[:MAX_REFS], refs=cyc)
            f["fix"] = FIX_AUTOSCROLL_LOOP
            out.append(f)
        else:
            out.append(_finding("tb.loop", "error",
                                f"focus cycles through {len(cyc)} stops without reaching an "
                                f"edge: {path}", in_cycle[:MAX_REFS], refs=cyc))
    last = next((s for s in reversed(steps) if s.get("moved") and s.get("key")), None)
    # where the first edge was hit (walk["edge"] describes it): the stop focus sat on, not
    # where it went after a wrap
    edge_i = next((j for j, s in enumerate(steps) if s.get("edge")), None)
    at_edge = None if edge_i is None else next(
        (s for s in reversed(steps[:edge_i]) if s.get("moved") and s.get("key")), None)
    at_edge = at_edge or last
    if walk.get("ended") == "stuck" and last is not None:
        # Stuck right before (or on) a WebView whose page is off screen: that is why.
        trap = next((t for t in walk.get("web_traps") or ()
                     if last.get("key") in (t.get("before"), t.get("web_root"))), None)
        block = _web_ahead(walk, last) if trap is None else None
        if trap is not None:
            out.append(web_trap_finding(trap, [last]))
        elif block is not None:
            web, holder = block
            where = f" (on a page of {holder})" if holder else ""
            f = _finding("tb.webview_block", "error",
                         f"TalkBack stopped at {_name(last)}: two presses in a row moved nothing, "
                         f"and the next stop is the WebView {web.get('ref') or web['key']}"
                         f"{where}: TalkBack cannot move focus into it", [last],
                         refs=[last.get("ref"), web.get("ref") or web["key"]],
                         keys=[last.get("key"), web["key"]])
            f["webview"] = web.get("ref") or web["key"]
            out.append(f)
        else:
            out.append(_finding("tb.edge_stuck", "error",
                                f"TalkBack stopped at {_name(last)}: two presses in a row moved "
                                f"nothing (no edge wrap)", [last]))
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
    if any(f["code"] in ("tb.webview_block", "tb.trap") for f in out) \
            and at_edge is not None and last is not None and at_edge.get("key") == last.get("key"):
        edge = {}  # stuck before a WebView: that is why, not a container left unscrolled
    where = (f" (step {edge_i})" if edge_i is not None else "")
    if edge.get("hidden_after") and at_edge is not None and not edge.get("can_scroll"):
        out.append(_finding("tb.edge_stuck", "warn",
                            f"TalkBack hit the edge{where} at {_name(at_edge)} with "
                            f"{edge['hidden_after']} hidden item(s) after it "
                            f"({_q(edge.get('hidden_first'))}…): they are clipped, and nothing "
                            f"TalkBack can scroll brings them in", [at_edge]))
    if edge.get("can_scroll") and at_edge is not None:
        tries = _edge_presses(steps, at_edge)
        f = _finding("tb.edge_stuck", "warn",
                     f"TalkBack hit the edge{where} at {_name(at_edge)}{tries} while its container "
                     f"{edge.get('container_ref') or edge.get('container')} "
                     f"({edge.get('container_cls')}) can still scroll "
                     f"{'/'.join(edge['can_scroll'])}: the rest is unreachable by swipe/keys",
                     [at_edge])
        if _pager(edge.get("container_cls"), edge["can_scroll"]):
            f["msg"] = (f"TalkBack hit the edge{where} at {_name(at_edge)}{tries}: the pager "
                        f"{edge.get('container_ref') or edge.get('container')} has more pages "
                        f"({'/'.join(edge['can_scroll'])}), and TalkBack never turns a page")
            f["fix"] = FIX_PAGER
        out.append(f)
    return out


#: tb.edge_stuck at a pager's edge (FIXES has the scrolling one): TalkBack never auto-scrolls
#: a pager (FILTER_AUTO_SCROLL), so its other pages need a way in.
FIX_PAGER = ("Give the pager a way to its other pages that TalkBack reaches: tabs, visible "
             "Next/Previous page buttons, or custom actions on the pager (\"Next page\").")


def _pager(cls: Optional[str], can: Sequence[str]) -> bool:
    """A pager by its class, or a container that only pages or scrolls sideways."""
    c = (cls or "").lower()
    if "pager" in c:
        return True
    return bool(can) and set(can) <= {"left", "right", "page_left", "page_right"}


def _edge_presses(steps: List[Dict[str, Any]], at: Dict[str, Any]) -> str:
    """`` (n of m presses)`` when the walk pressed more than once at the edge stop and some
    of those presses moved focus on: an edge TalkBack does not always hit (a flaky
    end-of-grid auto-scroll), said as such; else nothing."""
    key = at.get("key")
    presses = [s for s in steps if s.get("i", -1) > at.get("i", -1) and s.get("key") == key
               and not s.get("moved")]
    later = [s for s in steps if s.get("i", -1) > at.get("i", -1) and s.get("moved")
             and s.get("key") and s.get("via") != "wrap"]
    if len(presses) > 1 or (presses and later):
        return f" ({len(presses)} of {len(presses) + len(later[:1])} presses moved nothing)"
    return ""


def _web_ahead(walk: Dict[str, Any], last: Dict[str, Any]
               ) -> Optional[Tuple[Dict[str, Any], Optional[str]]]:
    """``(the WebView's predicted stop, the pager or list it sits in)`` when the model's next
    stop after ``last`` is a WebView's root, or a page holding one (its box holds the
    WebView's), whatever the geometry says; else None (G7: AntennaPod's episode page, stuck
    after Download with TalkBack started after the app, w9wtb7e)."""
    pred = walk.get("predicted") or []
    keys = [p["key"] for p in pred]
    k = _pk(last)
    if k not in keys:
        return None
    step = 1 if walk.get("direction", "next") == "next" else -1
    i = keys.index(k) + step
    if not 0 <= i < len(pred):
        return None
    nxt = pred[i]
    web = nxt if nxt.get("cls") == "WebView" else None
    if web is None and nxt.get("bounds"):
        ahead = pred[i + step:i + 6 * step:step] if step > 0 else pred[max(0, i - 5):i][::-1]
        web = next((p for p in ahead if p.get("cls") == "WebView" and p.get("bounds")
                    and p.get("window") == nxt.get("window")
                    and _contains(tuple(nxt["bounds"]), tuple(p["bounds"]), 0.5)), None)
    if web is None:
        return None
    holder = None
    if last.get("container"):
        holder = f"{last['container']} ({last.get('container_cls')})"
    return web, holder


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


def _same_stop(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Whether two steps read the same node: the same key (a Compose semantics id names one
    node; a View key, recycled by a list, only with the same label), or a node Compose
    re-minted (the model's key for both) in the same place of the tree (the same parent).
    Two alike nodes of two cards (NiA's "Bookmark" and "HEADLINES" of the card a scroll put
    in the slot of the one read before) are not, whatever the model matched them to."""
    ka, kb = a.get("key"), b.get("key")
    if ka and ka == kb:
        if _pk(a) == _pk(b):
            return True
        # the model told them apart (a "#n" stop): a View a list rebound to another item, or
        # a node of a ComposeView cell a list rebound; a Compose node of a host no list
        # recycles is one node, whatever its text says now (NIA-11, wtt0adx: the card read
        # in full, then its Bookmark, then the same card again as its other text)
        if str(ka).startswith("view:"):
            return False
        host = "view:" + str(ka).split(":")[1]
        anc = list(b.get("ancestors") or [])
        return not (host in anc and b.get("container") in anc[anc.index(host) + 1:])
    if _pk(a) != _pk(b) or not a.get("ancestors") or not b.get("ancestors"):
        return False
    return a["ancestors"][0] == b["ancestors"][0]


def _check_revisit(walk: Dict[str, Any], lap: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: List[Dict[str, Any]] = []
    out = []
    for s in lap:
        if s.get("via") == "stolen":
            seen = []  # the app sent focus back: what follows is read again because of that (tb.trap)
        hit = next((x for x in seen if _same_stop(x, s)), None)
        if hit is not None and hit["i"] != s["i"] - 1:
            out.append(_finding("tb.revisit", "warn",
                                f"step {s['i']}: {_name(s)} was already read at step {hit['i']} "
                                f"in this lap", [hit, s]))
        if hit is None:
            seen.append(s)
    return _collapse(out, "stops read again")


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
        if prev is not None and s.get("via") in ("next", "autoscroll", "window", "late") \
                and prev.get("container") \
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
            f = _finding(
                "tb.wrong_announcement", "warn",
                f"step {s['i']}: the first row of {c} is announced \"{n} of {total}\": "
                f"{n - 1} item(s) before it count but TalkBack never stops on them", [s])
            f["container"] = c
            out.append(f)
    return out[:3]


_IN_LIST = re.compile(r"\bIn (list|grid)\b[.,]?\s*(\d+) (items?|rows?)(?:[.,]\s*(\d+) columns?)?",
                      re.I)


def _item_of(s: Dict[str, Any], container: str) -> str:
    """The item of ``container`` a step's stop sits in: its ancestor right below the
    container (the stop itself when it is the item)."""
    anc = list(s.get("ancestors") or [])
    if container in anc:
        i = anc.index(container)
        return anc[i - 1] if i > 0 else (s.get("ref") or s.get("key") or "")
    return s.get("ref") or s.get("key") or ""


def _item_instance(s: Dict[str, Any], c: str) -> Tuple[str, str]:
    """The item of list ``c`` a step's stop sits in, told apart from another item a list
    rebinds the same item View to as it scrolls (the model's "#n" stop says which)."""
    it = _item_of(s, c)
    pk = str(_pk(s) or "")
    # only a rebound item View (or ComposeView cell) gets a "#n" stop (talkback/walk.py)
    return it, (pk.split("#", 1)[1] if "#" in pk else "")


def _check_list_count(walk: Dict[str, Any], skip: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """TalkBack's "In list. N items" (CollectionInfo) against the items a walk that went
    all the way through the list reached: an empty header or a spacer item counts, though
    TalkBack never stops on it (Thunderbird: "6 items" for 5 messages, wygfouz; Now in
    Android's Interests: "20 items" for 19 topics, a bottom Spacer item). Only for a list the
    first lap entered and left again (or a full lap): a walk that stopped inside it cannot
    tell. ``skip``: containers a "N of M" finding already names."""
    if walk.get("direction", "next") != "next":
        return []
    lap = [s for s in first_lap(walk["steps"]) if s.get("moved") and s.get("key")]
    full = _lap_complete(walk)
    out = []
    done: set = set(skip)
    back = {"backward", "up", "left", "page_up", "page_left"}
    fwd = {"forward", "down", "right", "page_down", "page_right"}
    for j, s in enumerate(lap):
        m = _IN_LIST.search(s.get("speak") or "") if s.get("utt") == "logcat" else None
        c = s.get("container")
        if m is None or not c or c in done or m.group(1).lower() != "list":
            continue
        done.add(c)
        run = [x for x in lap[j:] if x.get("container") == c]
        after = [x for x in lap[j:] if x.get("container") != c]
        before = [x for x in lap[:j] if x.get("container") != c]
        if not full and not (before and after):
            continue  # the lap did not go all the way through it
        n = int(m.group(2))
        held = s.get("container_items")  # the items the capture shows attached
        if held is None or held < n:
            # some items are not attached: only a lap from the list's start to its end
            # reached them all (a list scrolled to its middle starts the lap there)
            if set(run[0].get("container_can") or ()) & back \
                    or set(run[-1].get("container_can") or ()) & fwd:
                continue
        items: List[Any] = []
        for x in run:
            key = _item_instance(x, c)
            if key not in items:
                items.append(key)
        if n > len(items):
            out.append(_finding(
                "tb.wrong_announcement", "warn",
                f"step {s['i']}: {_name(s)} says \"In list. {n} items\" for {c}, but the lap "
                f"reached {len(items)} item(s) in it: {n - len(items)} item(s) count that "
                f"TalkBack never stops on (an empty header, footer or spacer item)", [s],
                refs=[s.get("ref"), c], keys=[s.get("key")]))
    return out[:3]


def _check_interleaved(walk: Dict[str, Any], lap: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """tb.interleaved: a stop read after another item's stops, though its own item (its card)
    was read before them: the controls of side-by-side cards read in turn (NIA-3, wg0mhts:
    the right card, the left card, the left card's Bookmark, then the right card's). Per
    container; a stop read again (tb.revisit) is not one."""
    out = []
    by_c: Dict[str, List[Dict[str, Any]]] = {}
    for s in lap:
        if s.get("container"):
            by_c.setdefault(s["container"], []).append(s)
    inst = _item_instance
    for c, run in by_c.items():
        last_of: Dict[Tuple[str, str], int] = {}  # item -> its last step's index in run
        read: List[Dict[str, Any]] = []
        for j, s in enumerate(run):
            it_i = inst(s, c)
            it = it_i[0]
            again = any(_same_stop(x, s) for x in read)
            read.append(s)
            if it_i in last_of and not again:
                a = last_of[it_i]
                between = [x for x in run[a + 1:j] if inst(x, c) != it_i]
                if between:
                    first = next(x for x in run if inst(x, c) == it_i)
                    other = _item_of(between[0], c)
                    out.append(_finding(
                        "tb.interleaved", "warn",
                        f"steps {first['i']}-{s['i']}: {_name(s)} of {it} is read after "
                        f"{len(between)} stop(s) of {other} (from step {between[0]['i']}), though "
                        f"{it} was read before them: the cards' stops are interleaved",
                        [first, *between, s], refs=[s.get("ref"), it, other]))
            last_of[it_i] = j
    return _collapse(out, "stops read apart from their card")


def _band(r: Sequence[int]) -> int:
    return int((r[1] + r[3] / 2) // max(1, r[3] * 0.4))


def _check_row_skip(walk: Dict[str, Any], lap: List[Dict[str, Any]]
                    ) -> Tuple[List[Dict[str, Any]], set]:
    """tb.autoscroll_row_skip: in a grid that scrolls sideways with more than one row,
    TalkBack's auto-scroll moves along the bottom row (each landing in it), so the rows above
    in every column it scrolls in are passed over (NIA-1, wvq4h1u: a 3-row
    LazyHorizontalGrid, 6 of 19 topics never reached). Names the items never reached in
    those rows; returns their keys too (tb.skipped leaves them to this finding)."""
    out: List[Dict[str, Any]] = []
    gone: set = set()
    pred = walk.get("predicted") or []
    P = [p["key"] for p in pred]
    steps = _first_screen(walk["steps"])
    visited = _visited(walk)
    labels_read = {(s.get("label") or "").strip() for s in _moves(steps)}
    by_c: Dict[str, List[Dict[str, Any]]] = {}
    for s in _moves(steps):
        if s.get("container") and _rect(s):
            by_c.setdefault(s["container"], []).append(s)
    for c, run in by_c.items():
        can = set().union(*(set(x.get("container_can") or ()) for x in run))
        if not can & {"left", "right", "page_left", "page_right"}:
            continue
        auto = [x for x in run if x.get("via") == "autoscroll"]
        centers = sorted({_rect(x)[1] + _rect(x)[3] / 2 for x in run})  # type: ignore[index]
        bands: List[float] = []
        for y in centers:
            if not bands or y - bands[-1] > 60:
                bands.append(y)
        if len(bands) < 2 or len(auto) < 2:
            continue
        bottom = bands[-1]

        def row(x: Dict[str, Any]) -> float:
            r = _rect(x)
            return r[1] + r[3] / 2  # type: ignore[index]

        along = [x for x in auto if abs(row(x) - bottom) <= 60]
        if len(along) < 2 or 2 * len(along) < len(auto):
            continue  # TalkBack does not scroll along the bottom row (a scroll to show a
            # control of the item it is on may land elsewhere)
        auto = along
        box = next((tuple(x["container_rect"]) for x in run if x.get("container_rect")), None)
        start = next((P.index(_pk(x)) for x in _moves(steps) if _pk(x) in P), 0)
        names: List[str] = []
        keys: List[str] = []
        refs: List[str] = []
        for i, p in enumerate(pred):
            r = p.get("bounds")
            lab = (p.get("label") or "").strip()
            if (i < start and not p.get("added")) or p["key"] in visited or not r or not box \
                    or not _contains(box, tuple(r), 0.5):  # type: ignore[arg-type]
                continue
            if abs((r[1] + r[3] / 2) - bottom) <= 60:
                continue  # the row TalkBack scrolls along
            keys.append(p["key"])
            refs.append(p.get("ref") or p["key"])
            if lab and lab not in labels_read and lab not in names:
                names.append(lab)
        if not names:
            continue
        gone |= set(keys)
        at = ", ".join(str(x["i"]) for x in auto[:6]) + (" …" if len(auto) > 6 else "")
        listed = ", ".join(_q(n, 28) for n in names[:6]) + (
            f" (+{len(names) - 6} more)" if len(names) > 6 else "")
        f = _finding("tb.autoscroll_row_skip", "warn",
                     f"TalkBack auto-scrolls {c} along its bottom row (steps {at}): the "
                     f"{len(bands) - 1} row(s) above in every column it scrolls in are passed "
                     f"over; {len(names)} item(s) never reached: {listed}", auto,
                     refs=[c] + refs[:MAX_REFS - 1], keys=keys)
        f["missed"] = names
        out.append(f)
    return out, gone


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
    return _collapse(out, "rows read out of screen order")


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
    n_of_m = _check_n_of_m(walk)
    findings += n_of_m
    findings += _check_list_count(walk, [f["container"] for f in n_of_m])
    findings += _check_speech_order(walk)
    findings += _check_cut_off_end(walk)
    findings += _check_window_order(walk)
    findings += _check_orphans(walk)
    escapes = _check_escape(walk)
    findings += escapes
    findings += _check_covered(walk, escapes)
    row_skip, passed = _check_row_skip(walk, lap)
    findings += row_skip
    findings += _check_skipped(walk, passed)
    findings += _check_interleaved(walk, lap)
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
