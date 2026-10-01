"""Resolve a walk's ``start`` or a scenario's ``target`` to the stop TalkBack reads (gap G4).

A selector names a node the way an agent knows it: a node key (``view:12``,
``compose:8:509``), a resource id (``#subject``), a test tag (``@topicTag:19``; Compose
reports it as the resource id with ``testTagsAsResourceId``), ``<n>th stop within <label>``,
``<label> within <label>``, or a label as spoken. It always resolves to a STOP of the model's
reading order (``talkback.order``): a node TalkBack really focuses. A key or text inside a
merged row climbs to the row (activating the Text inside a chip does nothing, the chip acts).

Labels are matched against each stop's own label, contentDescription and text, its composed
announcement (and each ``". "`` part of it), and the texts merged into it from its children.
Matches rank exact > whole word > substring ("Bookmark" never matches "Unbookmark" while a
"Bookmark" exists; "Wear OS" is a whole word of "Wear OS is not followed"). Within the best
rank several stops are a tie: a walk starts at the first of them in reading order, an
activation refuses and lists them (:func:`require`). An activation also refuses a match
inside a word ("Bookmark" in "Unbookmark", "follow" in "Unfollow": the opposite control).

Everything here is pure: it reads an :func:`inspector_widget.a11y.a11y_to_dict` dump.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from .walk import WalkError

Rect = Tuple[int, int, int, int]

#: How a label matched, best first.
RANKS = ("exact", "word", "substring")
#: The fields a label is matched against, in the order a match reports them.
FIELDS = ("label", "cd", "text", "speech", "child text")
#: Candidates an ambiguity error lists.
MAX_CANDIDATES = 5
#: A partial match (whole word or substring) that is less than this share of the words it
#: matched is too loose to activate: "Wear OS" in a card titled "The new Google Pixel Watch
#: is here: start building for Wear OS!" (t5c05ht opened Chrome that way).
WEAK = 0.5
HEAD = 40
#: What makes an activation leave the app: a web link, a URL, a ClickableSpan.
_URL = re.compile(r"\b(?:https?://|www\.)\S", re.I)
_NTH = re.compile(r"^(?:(?P<n>\d+)(?:st|nd|rd|th)?|(?P<w>first|second|third|fourth|fifth|last))"
                  r"\s+stop\s+(?:with)?in\s+(?P<scope>.+)$", re.I)
_WITHIN = re.compile(r"^(?P<sel>.+?)\s+within\s+(?P<scope>.+)$", re.I)
_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "last": -1}
_KEY = re.compile(r"^(?:view:\d+|(?:compose|virtual):-?\d+:-?\d+|legacy:.+)$")
_PUNCT = re.compile(r"[\s.,;:!?…\"“”‘’'()\[\]|·•–—-]+")


def norm(s: Any) -> str:
    """Lower case, punctuation and runs of space as one space: "Theme. Use" == "theme, use"."""
    return _PUNCT.sub(" ", str(s or "").lower()).strip()


def is_key(sel: str) -> bool:
    return bool(_KEY.match(sel.strip()))


def _rank(sel: str, value: str) -> Optional[int]:
    """0 exact, 1 whole word, 2 substring, None no match (both already :func:`norm`)."""
    if not sel or not value:
        return None
    if value == sel:
        return 0
    if re.search(r"(?<![a-z0-9])" + re.escape(sel) + r"(?![a-z0-9])", value):
        return 1
    if sel in value:
        return 2
    return None


@dataclass(eq=False)
class Stop:
    """One stop of the model's reading order, with what a selector can match on it."""

    key: str
    order: int
    speech: str
    label: str
    cd: str
    text: str
    cls: str
    bounds: Rect
    window: int
    node: Dict[str, Any]
    parts: List[str] = field(default_factory=list)   # texts merged in from its children
    inner: Set[str] = field(default_factory=set)     # keys of the nodes merged into it
    subtree: Set[str] = field(default_factory=set)   # every key under it (stops too)
    ancestors: Set[str] = field(default_factory=set)  # the keys of the nodes above it
    rid: str = ""
    link: Optional[str] = None                       # why activating it may leave the app
    custom: Dict[str, int] = field(default_factory=dict)   # custom action label -> id

    def head(self, n: int = HEAD) -> str:
        s = (self.speech or self.label or "").replace("\n", " ")
        return s if len(s) <= n else s[: n - 1] + "…"

    def line(self, n: int = HEAD) -> str:
        return f'{self.key} "{self.head(n)}"'

    def fields(self) -> Iterator[Tuple[str, str]]:
        """What a selector is matched against: what TalkBack speaks for the stop (a
        contentDescription replaces the text, so a text under one is not matched)."""
        yield "label", self.label
        yield "cd", self.cd
        if not self.cd:
            yield "text", self.text
        yield "speech", self.speech
        for seg in re.split(r"\.\s+|,\s+", self.speech or ""):
            yield "speech", seg
        for p in self.parts:
            yield "child text", p


@dataclass
class Match:
    """``node``: the stop; ``field``/``how``: what matched and how well; ``candidates``:
    every stop of that rank (more than one is a tie); ``via``: the inner node a key or
    tag named, or the scope of a ``within`` selector."""

    node: Stop
    field: str
    how: str
    candidates: List[Stop]
    selector: str
    via: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    cover: float = 1.0  # the share of the matched words the selector is (weak below WEAK)

    @property
    def key(self) -> str:
        return self.node.key

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1

    def describe(self) -> str:
        """``matched=label (exact) compose:8:509 "Bookmark. Check box"``."""
        out = f"matched={self.field} ({self.how}) {self.node.line(32)}"
        if self.via:
            out += f" via {self.via}"
        if self.ambiguous:
            out += f"; first of {len(self.candidates)}"
        return out


class SelectError(WalkError):
    """A selector that names no stop, or more than one for an activation. ``tried``: the
    candidates (what the error envelope lists)."""

    def __init__(self, code: str, message: str, hint: Optional[str] = None,
                 candidates: Sequence[str] = ()) -> None:
        super().__init__(code, message, hint)
        self.tried = list(candidates)


# --------------------------------------------------------------------------- #
# Stops of a dump
# --------------------------------------------------------------------------- #
def _iter(windows: Sequence[Dict[str, Any]]) -> Iterator[Tuple[Dict[str, Any], int]]:
    for w in windows:
        root = w.get("root")
        if not root:
            continue
        stack = [root]
        while stack:
            n = stack.pop()
            yield n, int(w.get("root_view_id") or 0)
            stack.extend(reversed(n.get("children") or []))


def _key_fn(windows: Sequence[Dict[str, Any]], legacy: bool) -> Callable[[Dict[str, Any], int], str]:
    """The walk's node identity for a dump node (talkback.walk.node_key)."""
    from .walk import HOST_VIEW_ID, _label, node_key
    hosts = {int(n.get("host_view_id") or 0) for n, _w in _iter(windows)
             if int(n.get("virtual_id", HOST_VIEW_ID)) == HOST_VIEW_ID
             and "compose" in ((n.get("provider_class") or "") + (n.get("class_name") or "")).lower()}

    def key(n: Dict[str, Any], win: int) -> str:
        if not legacy and n.get("node_key"):
            return str(n["node_key"])
        kids = [(c.get("text") or "", c.get("content_description") or "")
                for c in n.get("children") or []]
        return node_key(win, int(n.get("host_view_id") or 0), int(n.get("virtual_id", HOST_VIEW_ID)),
                        n.get("class_name") or "", _label(n.get("text") or "",
                                                         n.get("content_description") or "", kids),
                        legacy, hosts)
    return key


#: What :func:`talkback.order.reading_order` raises on a dump it cannot read (a malformed
#: or partial one): only then do the selectors fall back to the a11y model's numbering. A
#: TypeError, AttributeError or ImportError is a bug in the call and propagates.
_MODEL_ERRORS = (KeyError, ValueError, IndexError, RecursionError)


def _ordered(d: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """(stop dicts in reading order, their announcements): talkback.order, else (a dump
    the TalkBack model cannot read) the a11y model's numbering."""
    from .order import reading_order
    try:
        res = reading_order(d, keyboard=True)
        return list(res["_nodes"]), [str(e.get("speak") or "") for e in res["focus_order"]]
    except _MODEL_ERRORS:
        pass
    stops: List[Tuple[int, Dict[str, Any]]] = []
    for n, _w in _iter(d.get("windows") or []):
        if isinstance(n.get("order"), int):
            stops.append((n["order"], n))
    stops.sort(key=lambda t: t[0])
    speak = {e.get("order"): e.get("speak") for e in d.get("focus_order") or []}
    return [n for _o, n in stops], [str(speak.get(o) or "") for o, _n in stops]


def custom_actions(n: Dict[str, Any]) -> Dict[str, int]:
    """A node's custom accessibility actions, label -> id: the labelled actions whose id is
    neither one of AccessibilityNodeInfo's own (a power of two) nor a framework id
    (``android.R.id.accessibilityAction*``, 0x0102....)."""
    out: Dict[str, int] = {}
    for a in n.get("actions") or []:
        if not isinstance(a, dict) or not a.get("label"):
            continue
        aid = int(a.get("id") or 0)
        if aid <= 0 or (aid & (aid - 1)) == 0 or (aid >> 16) == 0x0102:
            continue
        out.setdefault(str(a["label"]), aid)
    return out


def _words(n: Dict[str, Any]) -> List[str]:
    """What TalkBack speaks of a node: its contentDescription, else its text."""
    w = n.get("content_description") or n.get("text")
    return [str(w)] if w else []


def _link(n: Dict[str, Any]) -> Optional[str]:
    role = str(n.get("role_description") or "").lower()
    extras = n.get("extras") or {}
    if role == "link" or str(extras.get("AccessibilityNodeInfo.chromeRole") or "") == "link":
        return "a web link"
    if any(_URL.search(str(w)) for w in (n.get("content_description"), n.get("text")) if w):
        return "a URL"
    spans = next((v for k, v in extras.items() if "SPANS" in k and "START" in k), None)
    if spans not in (None, "", "[]"):
        return "a clickable span (a link)"
    return None


def _shown(n: Dict[str, Any]) -> bool:
    """Whether a window root shows anything: visible itself, or a visible node under it."""
    stack = [n]
    while stack:
        x = stack.pop()
        if "visible_to_user" in (x.get("flags") or ()):
            return True
        stack.extend(x.get("children") or [])
    return False


def _blank(windows: Sequence[Dict[str, Any]]) -> Set[int]:
    """Root ids of the windows that show nothing (their whole tree invisible)."""
    return {int(w.get("root_view_id") or 0) for w in windows
            if w.get("root") and not _shown(w["root"])}


def live_windows(d: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The windows of a dump TalkBack can reach: not under a modal window that shows
    something (a blank one left on top does not cover, see :func:`stops_from_dump`)."""
    windows = d.get("windows") or []
    blank = _blank(windows)
    return [w for w in windows if w.get("covered_by") is None or w.get("covered_by") in blank]


def covering_window(d: Dict[str, Any], key: str, *, legacy: bool = False) -> Optional[int]:
    """The root id of the modal window that covers node ``key``'s window (a dialog over the
    activity: a TalkBack user cannot reach the node), or None."""
    windows = d.get("windows") or []
    key_of = _key_fn(windows, legacy)
    blank = _blank(windows)
    for w in windows:
        by = w.get("covered_by")
        if by is None or by in blank or not w.get("root"):
            continue
        win = int(w.get("root_view_id") or 0)
        if any(key_of(n, win) == key for n, _w in _iter([w])):
            return int(by)
    return None


def stops_from_dump(d: Dict[str, Any], *, legacy: bool = False) -> List[Stop]:
    """The model's stops of one :func:`~inspector_widget.a11y.a11y_to_dict` dump.

    A window that shows nothing (its whole tree invisible: a dismissed dialog's leftover
    window, seen on Now in Android after the 16 KB dialog) is not let to hide the windows
    under it: their stops are what TalkBack reads (the walk reaches them)."""
    windows = d.get("windows") or []
    key_of = _key_fn(windows, legacy)
    blank = _blank(windows)
    if blank and any(w.get("covered_by") in blank for w in windows):
        d = dict(d, windows=[{k: v for k, v in w.items() if k != "covered_by"}
                             if w.get("covered_by") in blank else w for w in windows])
        windows = d["windows"]
    nodes, speaks = _ordered(d)
    parent: Dict[int, Dict[str, Any]] = {}
    win_of: Dict[int, int] = {}
    for n, w in _iter(windows):
        win_of[id(n)] = w
        for c in n.get("children") or []:
            parent[id(c)] = n
    stop_ids = {id(n) for n in nodes}
    out: List[Stop] = []
    by_id: Dict[int, Stop] = {}
    for i, (n, sp) in enumerate(zip(nodes, speaks, strict=False)):
        win = win_of.get(id(n), 0)
        b = (n.get("bounds") or {}).get("layout") or {}
        text, cd = str(n.get("text") or ""), str(n.get("content_description") or "")
        kids = [str(c.get("content_description") or c.get("text") or "") for c in n.get("children") or []]
        s = Stop(key=key_of(n, win), order=i + 1, speech=sp, label=cd or text or " | ".join(k for k in kids if k),
                 cd=cd, text=text, cls=str(n.get("class_name") or "").rsplit(".", 1)[-1],
                 bounds=(int(b.get("x", 0)), int(b.get("y", 0)), int(b.get("w", 0)), int(b.get("h", 0))),
                 window=win, node=n, rid=str(n.get("view_id_resource_name") or ""), link=_link(n),
                 custom=custom_actions(n))
        out.append(s)
        by_id[id(n)] = s
    # each node belongs to the nearest stop at or above it: its texts are that stop's
    for n, w in _iter(windows):
        a: Optional[Dict[str, Any]] = n
        while a is not None and id(a) not in stop_ids:
            a = parent.get(id(a))
        k = key_of(n, w)
        up: Optional[Dict[str, Any]] = parent.get(id(n))
        while up is not None:
            if id(up) in by_id:
                by_id[id(up)].subtree.add(k)
            if id(n) in by_id:
                by_id[id(n)].ancestors.add(key_of(up, w))
            up = parent.get(id(up))
        if a is None or a is n:
            continue
        s = by_id[id(a)]
        s.inner.add(k)
        s.parts.extend(_words(n))
        if s.link is None:
            s.link = _link(n)
    return out


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def _by_key(stops: Sequence[Stop], key: str) -> Tuple[Optional[Stop], Optional[str]]:
    """(the stop for ``key``, how it got there): the stop itself; the stop a node is
    merged into (``inner``: a Text inside a row); the first stop inside a container
    (``within``: a list, a card that is no stop)."""
    for s in stops:
        if s.key == key:
            return s, None
    inner = [s for s in stops if key in s.inner]
    if inner:
        return inner[0], "inner"
    within = [s for s in stops if key in s.ancestors]
    if within:
        return within[0], "within"
    return None, None


def _label_match(stops: Sequence[Stop], sel: str
                 ) -> Tuple[Optional[int], List[Tuple[Stop, str, float]]]:
    """(best rank, [(stop, field, cover)] at that rank, in reading order); ``cover``: the
    share of the matched words the selector is (1 for an exact match)."""
    want = norm(sel)
    n_want = len(want.split())
    best: Optional[int] = None
    hits: List[Tuple[Stop, str, float]] = []
    for s in stops:
        r_best, f_best, c_best = None, "", 0.0
        for f, v in s.fields():
            nv = norm(v)
            r = _rank(want, nv)
            if r is None:
                continue
            cov = 1.0 if r == 0 else n_want / max(1, len(nv.split()))
            if r_best is None or r < r_best or (r == r_best and cov > c_best):
                r_best, f_best, c_best = r, f, cov
                if r == 0:
                    break
        if r_best is None:
            continue
        if best is None or r_best < best:
            best, hits = r_best, [(s, f_best, c_best)]
        elif r_best == best:
            hits.append((s, f_best, c_best))
    return best, hits


def _scope(stops: Sequence[Stop], label: str) -> Tuple[List[Stop], Optional[Stop]]:
    """The stops inside the one ``label`` names (it included), and that container."""
    rank, hits = _label_match(stops, label)
    if rank is None or not hits:
        return [], None
    box = hits[0][0]
    inside = [s for s in stops if s is box or s.key in box.subtree]
    return inside, box


def resolve(stops: Sequence[Stop], selector: str, *,
            speech: Optional[Callable[[Stop], str]] = None) -> Optional[Match]:
    """The stop ``selector`` names among ``stops`` (see the module docstring), or None.
    ``speech`` overrides each stop's announcement (a calibrated one, a logged one)."""
    sel = selector.strip()
    if not sel or not stops:
        return None
    if speech is not None:
        for s in stops:
            s.speech = speech(s) or s.speech
    if is_key(sel):
        s, how = _by_key(stops, sel)
        if s is None:
            return None
        return Match(s, "key", how or "key", [s], sel, via=sel if how else None)
    if sel.startswith(("#", "@")) and len(sel) > 1:
        want = sel[1:]
        how = "rid" if sel[0] == "#" else "tag"
        hits: List[Stop] = []
        via: Optional[str] = None
        for s in stops:
            if _rid_ok(s.node, want):
                hits.append(s)
                continue
            for n in _inner_nodes(s):
                k = str(n.get("node_key") or "")
                if _rid_ok(n, want) and k in s.inner:  # merged into s, not a stop inside it
                    hits.append(s)
                    via = via or k
                    break
        if hits:
            return Match(hits[0], how, how, hits, sel, via=via if len(hits) == 1 else None)
        if sel[0] == "#" or not sel[1:].strip():
            return None
        # "@alice": a label that starts with "@"
    m = _NTH.match(sel)
    if m:
        inside, box = _scope(stops, m.group("scope"))
        n = int(m.group("n")) if m.group("n") else _WORDS[m.group("w").lower()]
        if not inside or abs(n) > len(inside) or n == 0:
            return None
        s = inside[n - 1] if n > 0 else inside[n]
        return Match(s, "position", "nth", [s], sel, via=box.key if box else None)
    m = _WITHIN.match(sel)
    if m:
        inside, box = _scope(stops, m.group("scope"))
        if inside:
            got = resolve(inside, m.group("sel"))
            if got is not None:
                got.selector, got.via = sel, f"within {box.key}" if box else None
                return got
    rank, hits = _label_match(stops, sel)
    if rank is None:
        return None
    first, fld, cover = hits[0]
    return Match(first, fld, RANKS[rank], [s for s, _f, _c in hits], sel,
                 cover=max(c for _s, _f, c in hits))


def _inner_nodes(s: Stop) -> Iterator[Dict[str, Any]]:
    stack = list(s.node.get("children") or [])
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.get("children") or [])


def _rid_ok(n: Dict[str, Any], want: str) -> bool:
    rid = str(n.get("view_id_resource_name") or "")
    return bool(rid) and (rid == want or rid.endswith((":id/" + want, "/" + want)))


def candidates(m: Match, n: int = MAX_CANDIDATES) -> List[str]:
    return [s.line(HEAD) for s in m.candidates[:n]]


def ambiguity_error(m: Match, where: str = "") -> SelectError:
    more = len(m.candidates) - MAX_CANDIDATES
    return SelectError(
        "ambiguous",
        f"{m.selector!r} matches {len(m.candidates)} stops ({m.how} {m.field}){where}"
        + (f"; {more} more not listed" if more > 0 else ""),
        hint="Name one: its key (from candidates), '<n>th stop within <label>', "
             "'<label> within <card label>', or more of the words it speaks.",
        candidates=candidates(m))


def not_found_error(selector: str, n_stops: int, where: str, *, searched: str = "") -> SelectError:
    return SelectError(
        "start_not_found",
        f"label {selector!r} not found among {n_stops} stops on {where or 'the screen'}"
        + (f" ({searched})" if searched else ""),
        hint="Pass the words as TalkBack speaks them, a node key or ref, '#resource_id' or "
             "'@testTag'; open the screen that has it first.")


def vet(m: Match, *, activate: bool, where: str = "",
        covered: Optional[Callable[[Stop], Optional[str]]] = None) -> Match:
    """Check a match for what it is used for. An activation (``activate``) refuses a tie
    (``ambiguous``, listing the candidates) and a stop ``covered`` says lies under an open
    drawer, sheet or dialog (``start_not_found``), and notes a stop that may leave the app
    (a link). A walk start keeps the first of a tie, with a note."""
    if activate and m.ambiguous:
        raise ambiguity_error(m, f" on {where}" if where else "")
    if activate and m.how == "substring":
        # "Bookmark" inside "Unbookmark", "follow" inside "Unfollow": the opposite control
        raise SelectError(
            "ambiguous",
            f"{m.selector!r} is only part of a word in what {len(m.candidates)} stop(s) say "
            f"(their {m.field}; no stop says it as a word): too loose to activate"
            + (f" on {where}" if where else ""),
            hint="Pass the whole word as the stop speaks it, its key or ref, or "
                 "'<label> within <card label>'.",
            candidates=candidates(m))
    if activate and m.how == "word" and m.cover < WEAK:
        raise SelectError(
            "ambiguous",
            f"{m.selector!r} is only part of what {len(m.candidates)} stop(s) say ({m.how} match "
            f"in their {m.field}, at most {round(m.cover * 100)}% of its words): too loose to "
            f"activate" + (f" on {where}" if where else ""),
            hint="Pass the words the stop speaks (more of them), its key or ref, or "
                 "'<label> within <card label>'.",
            candidates=candidates(m))
    if m.how == "substring":
        m.notes.append(f"{m.selector!r} is only part of a word of {m.node.line(32)}: no stop "
                       f"says it as a word")
    if m.ambiguous:
        m.notes.append(f"{m.selector!r}: {len(m.candidates)} stops match ({m.how} {m.field}); "
                       f"took the first, {m.node.key}")
    by = covered(m.node) if covered is not None else None
    if by and activate:
        raise SelectError("start_not_found",
                          f"{m.node.line(32)} lies under {by}: a TalkBack user cannot "
                          f"activate it there",
                          hint="Close the drawer/sheet/dialog first, or target a stop on it.")
    if by:
        m.notes.append(f"{m.node.key} lies under {by}")
    if activate and m.node.link:
        m.notes.append(f"activating {m.node.key} may leave the app: it is {m.node.link}")
    return m


def require(stops: Sequence[Stop], selector: str, *, activate: bool, where: str = "",
            covered: Optional[Callable[[Stop], Optional[str]]] = None) -> Match:
    """:func:`resolve` then :func:`vet`; ``start_not_found`` when nothing matches."""
    m = resolve(stops, selector)
    if m is None:
        raise not_found_error(selector, len(stops), where)
    return vet(m, activate=activate, where=where, covered=covered)


def can_bring_more(d: Dict[str, Any]) -> Optional[str]:
    """What could bring a stop that is not in the dump onto the screen: a list or scroll
    view that can still scroll (TalkBack auto-scrolls it), or a WebView whose tree is empty
    (it builds one only while a screen reader runs), in a window TalkBack reaches (not one
    under an open dialog). None: nothing can."""
    from .walk import _SCROLL_ACTIONS, _WEBVIEW
    for n, _w in _iter(live_windows(d)):
        cls = str(n.get("class_name") or "")
        if cls == _WEBVIEW and not n.get("children"):
            return "an empty WebView"
        if cls == _WEBVIEW:
            continue
        ids = {a.get("id") for a in n.get("actions") or [] if isinstance(a, dict)}
        if ids & set(_SCROLL_ACTIONS):
            b = (n.get("bounds") or {}).get("layout") or {}
            return f"{n.get('node_key') or cls.rsplit('.', 1)[-1]} [{b.get('w', 0)}x{b.get('h', 0)}] scrolls"
    return None


def scrollables(d: Dict[str, Any]) -> List[Tuple[str, Set[int]]]:
    """(key, scroll action ids) of each scrolling container in a window TalkBack reaches
    (never a list under an open dialog: scrolling it would change the screen behind the
    dialog for nothing), those in the window that holds accessibility focus first, then
    largest first (a list before the chip rows in its cards)."""
    from .walk import _SCROLL_ACTIONS, _WEBVIEW
    live = live_windows(d)
    focused = next((w for n, w in _iter(live) if "accessibility_focused" in (n.get("flags") or ())),
                   None)
    out: List[Tuple[int, int, str, Set[int]]] = []
    for n, w in _iter(live):
        if str(n.get("class_name") or "") == _WEBVIEW or not n.get("node_key"):
            continue
        ids = {int(a.get("id") or 0) for a in n.get("actions") or [] if isinstance(a, dict)}
        if ids & set(_SCROLL_ACTIONS):
            b = (n.get("bounds") or {}).get("layout") or {}
            out.append((0 if focused is None or w == focused else 1,
                        -int(b.get("w", 0)) * int(b.get("h", 0)), str(n["node_key"]), ids))
    out.sort(key=lambda t: (t[0], t[1]))
    return [(k, ids) for _f, _a, k, ids in out]


#: The share of a window's height its top bar (a toolbar, an action mode) takes at most.
TOP_BAND = 0.15


#: A node whose texts are a control's or a list's, not a title (a toolbar may be long-clickable).
_CONTROL = frozenset({"clickable", "focusable", "scrollable"})


def titles(d: Dict[str, Any]) -> List[str]:
    """What names the screen, as ``pre:pane=<title>`` checks it: pane titles, the windows'
    accessibility titles, headings, and the texts in the top band of each window that sit
    in no control and no list (a toolbar's or an action mode's title). Never a navigation
    bar's or rail's labels, which every destination shows, nor a list row's texts."""
    from .walk import _SCROLL_ACTIONS
    out: List[str] = []

    def add(t: Any) -> None:
        t = str(t or "").strip()
        if t and t not in out:
            out.append(t)

    def control(n: Dict[str, Any]) -> bool:
        ids = {a.get("id") for a in n.get("actions") or [] if isinstance(a, dict)}
        return bool(set(n.get("flags") or ()) & _CONTROL or ids & set(_SCROLL_ACTIONS))

    for w in live_windows(d):
        root = w.get("root")
        if not root:
            continue
        add(w.get("title"))
        frame = w.get("frame") or (root.get("bounds") or {}).get("layout") or {}
        wy, wh = int(frame.get("y", 0)), int(frame.get("h", 0))
        stack: List[Tuple[Dict[str, Any], bool]] = [(root, False)]
        while stack:
            n, inside = stack.pop()
            fl = set(n.get("flags") or ())
            add(n.get("pane_title"))
            if "heading" in fl:
                add(n.get("content_description") or n.get("text")
                    or " ".join(_words(c)[0] for c in n.get("children") or [] if _words(c)))
            b = (n.get("bounds") or {}).get("layout") or {}
            if (wh > 0 and n.get("text") and not inside and not control(n)
                    and "visible_to_user" in fl
                    and int(b.get("y", 0)) < wy + TOP_BAND * wh
                    and int(b.get("h", 0)) < TOP_BAND * wh):
                add(n.get("text"))
            under = inside or control(n)
            stack.extend((c, under) for c in reversed(n.get("children") or []))
    return out


__all__ = ["FIELDS", "Match", "RANKS", "SelectError", "WEAK", "Stop", "ambiguity_error", "can_bring_more",
           "candidates", "covering_window", "is_key", "live_windows", "norm", "not_found_error",
           "require", "resolve", "scrollables", "stops_from_dump", "titles", "vet"]
