"""TalkBack over a capture: the calibrated model run on the stored accessibility tree.

The capture keeps the accessibility tree (``raw/a11y.pb``) verbatim, so everything the
TalkBack model (:mod:`inspector_widget.talkback`) predicts can be asked of a capture
without the device: which nodes are stops and why, what TalkBack says at each, in what
order a swipe visits them, and what the model sees wrong (the ``tb.*`` rules). This module
ties the model's nodes to the capture's nodes (refs), so every answer names refs.

:class:`TbCapture` is that binding. ``TbCapture.of(ix, loaded)`` builds it once per index
(cached) from the capture's a11y facet; it is None when the capture has none. Node dicts of
the a11y dump map to index nodes through the same mapper the lint and the reading order use
(:meth:`analyzers._A11yDump.mapper`), so a duplicated (host, virtual) pair is never guessed.

What it serves:

* **Speech** (:func:`stop_speech`, used by the index builder): the announcement of every
  stop, as the TalkBack 17.0 wording of the model gives it for a first focus (no collection
  or window transition): the text ``tb_walk``'s model column shows. ``speak_src`` says it
  came from the model ("tb"); the index falls back to its own RO1 text ("ro1") when the
  model cannot run.
* **Explanations** (:meth:`TbCapture.explain`): why a node is a stop (``click``,
  ``focusable``, ``text_orphan``, ``leaf`` ...) or why not (``merged_into:<ref>`` (the
  stop reads it), ``silenced_by:<ref>`` (the stop above it does not: its contentDescription
  replaces the text), ``inside_silent:<ref>`` (under a focusable container that is no
  stop), ``hidden_by:<ref>``, ``silent_container``, ``covered_by:<ref>``,
  ``offscreen`` (outside its window or scrolled out of its scroller), ``zero_size``,
  ``invisible``, ``not_important`` ...), with ghost reasons for stops that say nothing
  useful.
* **Reading walks** (:meth:`TbCapture.reading`): the stops in swipe order with what TalkBack
  says on arrival (collection and window transitions included), per granularity (default,
  heading, control), from any node, forward or backward, optionally with the nodes the walk
  passes over and why.
* **The tb facet** of ``node()`` (:meth:`TbCapture.facet`).
* **Static rules** (:func:`issues`): :mod:`inspector_widget.talkback.static` findings as
  capture issues (``tb.*``), the other nodes involved named in ``node_ids``; what is drawn
  above what comes from the capture's View tree (:func:`drawn_above`).

Everything here is pure (no device, no protobuf beyond decoding the stored facet).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..talkback import Navigator, build as tb_build
from ..talkback.explain import why_stop
from ..talkback.speech import Announcement, SpeechState, announce
from ..talkback.tree import Excluded, TbNode
from .model import Index, Issue

#: Parts of an announcement that name the node (as opposed to its state, role, position).
NAME_KINDS = frozenset({"name", "child", "name(fake)", "event"})
GRANULARITIES = ("default", "heading", "control")
DIRECTIONS = ("next", "prev")
#: Model wording echoed in responses (speech.DEFAULT_VERSION; rules.TB_RULES_REV).
SPEAK_SRC_MODEL = "tb"
SPEAK_SRC_FALLBACK = "ro1"

#: Model diagnostics a capture reports (``tb: ...``): why the model's "N of M" for RecyclerView
#: items is missing or differs from a walk's (talkback/recycler.py).
SURFACED_DIAGNOSTICS = frozenset({"recycler_bound_before_service", "recycler_positions_unknown",
                                  "recycler_layout_unknown"})

#: The attribute an Index carries its binding under (Index is unhashable, so no weak map).
_CACHE_ATTR = "_tb_capture"


@dataclass
class StopSpeech:
    """What TalkBack says at one stop on a first focus, and the name in it."""

    text: str
    name: str | None
    unlabelled: bool


@dataclass
class ReadItem:
    """One line of a reading walk: a stop (``stop`` set) or a node passed over."""

    nid: str
    stop: int | None = None
    speak: str | None = None
    why: str | None = None  # a stop's reason, or a skipped node's code
    ref_key: str | None = None  # a skipped node's code that names a node: merged_into ...
    ref: str | None = None  # ... and that node
    via: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def name_of(ann: Announcement) -> str | None:
    """The naming parts of an announcement, joined: the node's name without its state,
    role, position or heading words ("Default" in "Selected. Default. Radio button")."""
    parts = [p["text"] for p in ann.parts if p.get("kind") in NAME_KINDS and p.get("text")]
    return ", ".join(parts) or None


def _decode(resp: Any) -> Any:
    from .analyzers import _A11yDump

    if isinstance(resp, _A11yDump):
        return resp
    if isinstance(resp, (bytes, bytearray)):
        from ..proto import view_inspection_pb2 as pb

        resp = pb.DumpA11yResponse.FromString(bytes(resp))
    return _A11yDump(resp)


def _iter_paths(root: dict[str, Any]):
    stack: list[tuple[dict[str, Any], tuple[int, ...]]] = [(root, (0,))]
    while stack:
        n, path = stack.pop()
        yield n, path
        kids = n.get("children") or []
        for i in range(len(kids) - 1, -1, -1):
            stack.append((kids[i], path + (i,)))


class TbCapture:
    """The TalkBack view of one capture's accessibility tree, bound to its nodes.

    ``resp``: the stored DumpA11yResponse (bytes, message, or an analyzers ``_A11yDump``);
    ``ix``: the index to name nodes in (None: speech by dump position only)."""

    def __init__(self, resp: Any, ix: Index | None = None) -> None:
        self.dump = _decode(resp)
        self.tree = tb_build(self.dump.data)
        self.nav = Navigator(self.tree)
        self.rules = self.nav.rules
        self.ix = ix
        self._lookup = self.dump.mapper(ix) if ix is not None else None
        self._nid_of_raw: dict[int, str | None] = {}
        self._by_nid: dict[str, TbNode | Excluded] | None = None
        self._by_key: dict[str, TbNode] = {}
        for n in self.tree.nodes:
            self._by_key.setdefault(n.key, n)
        dpi = ((getattr(getattr(ix, "meta", None), "device", None) or {}).get("dpi")
               if ix is not None else None)
        self.density = int(dpi) if dpi else 420  # the dpi the lint sizes ghosts with
        self._own: dict[int, Announcement] = {}
        self._linear: list[TbNode] | None = None
        self._stop_no: dict[int, int] | None = None
        self._walk_speech: dict[int, Announcement] | None = None

    # ------------------------------------------------------------------ binding
    @classmethod
    def of(cls, ix: Index, loaded: Any) -> TbCapture | None:
        """The (cached) binding for ``ix``, built from ``loaded``'s a11y facet; None when
        the capture has no accessibility tree or the model cannot read it."""
        hit = getattr(ix, _CACHE_ATTR, None)
        if hit is not None:
            return hit or None
        tbc: TbCapture | None = None
        try:
            from .analyzers import _Src

            src = loaded if isinstance(loaded, _Src) else _Src(loaded)
            dump = src.a11y_dump() if loaded is not None else None
            if dump is not None and dump.has_roots:
                tbc = cls(dump, ix)
        except Exception:  # noqa: BLE001 - the queries degrade to the stored reading order
            tbc = None
        try:
            object.__setattr__(ix, _CACHE_ATTR, tbc if tbc is not None else False)
        except AttributeError:  # pragma: no cover - an index without a __dict__
            pass
        return tbc

    def nid_of_raw(self, raw: dict[str, Any] | None) -> str | None:
        if raw is None or self._lookup is None:
            return None
        k = id(raw)
        if k not in self._nid_of_raw:
            self._nid_of_raw[k] = self._lookup(raw)
        return self._nid_of_raw[k]

    def nid(self, n: TbNode | Excluded | None) -> str | None:
        return None if n is None else self.nid_of_raw(n.raw)

    def node(self, nid: str) -> TbNode | Excluded | None:
        """The TalkBack-view node (or the excluded dump node) behind an index node."""
        if self._by_nid is None:
            self._by_nid = {}
            for w in self.dump.data.get("windows") or []:
                if not w.get("root"):
                    continue
                for raw, _path in _iter_paths(w["root"]):
                    x = self.tree.by_raw.get(id(raw)) or self.tree.excluded_by_raw.get(id(raw))
                    i = self.nid_of_raw(raw)
                    if x is not None and i is not None:
                        self._by_nid.setdefault(i, x)
        return self._by_nid.get(nid)

    def by_key(self, key: str) -> TbNode | None:
        return self._by_key.get(key)

    def window_ref(self, root_view_id: Any) -> str | None:
        if self.ix is None or root_view_id is None:
            return None
        return self.ix.resolve_id(f"w:{int(root_view_id)}") or \
            self.ix.resolve_id(f"view:{int(root_view_id)}")

    def _window_nid(self, n: TbNode) -> str | None:
        return self.window_ref(n.window.root_view_id) or self.nid(n.window.root)

    # ------------------------------------------------------------------ speech
    def own(self, n: TbNode) -> Announcement:
        """What TalkBack says when ``n`` takes focus first (no transitions)."""
        a = self._own.get(id(n))
        if a is None:
            a = self._own[id(n)] = announce(self.nav, n, transitions=False)
        return a

    def linear(self) -> list[TbNode]:
        """Every stop, window by window, in swipe order (the capture's reading order)."""
        if self._linear is None:
            self._linear = self.nav.linear()
            self._stop_no = {id(n): i + 1 for i, n in enumerate(self._linear)}
        return self._linear

    def stop_no(self, n: TbNode) -> int | None:
        self.linear()
        return (self._stop_no or {}).get(id(n))

    def walk_speech(self, n: TbNode) -> Announcement:
        """What TalkBack says when a forward swipe from the previous stop lands on ``n``:
        the collection position and transitions included."""
        if self._walk_speech is None:
            st = SpeechState()
            self._walk_speech = {id(x): announce(self.nav, x, st) for x in self.linear()}
        return self._walk_speech.get(id(n)) or self.own(n)

    def stop_speech(self) -> dict[tuple[int, tuple[int, ...]], StopSpeech]:
        """``{(root_view_id, path): StopSpeech}`` for every stop: the index builder's key."""
        out: dict[tuple[int, tuple[int, ...]], StopSpeech] = {}
        stops = {id(n) for n in self.linear()}
        for w in self.dump.data.get("windows") or []:
            if not w.get("root"):
                continue
            rid = int(w.get("root_view_id") or 0)
            for raw, path in _iter_paths(w["root"]):
                n = self.tree.by_raw.get(id(raw))
                if n is None or id(n) not in stops:
                    continue
                a = self.own(n)
                out[(rid, path)] = StopSpeech(a.text, name_of(a), a.unlabelled)
        return out

    # ------------------------------------------------------------------ explain
    def explain(self, nid: str) -> dict[str, Any]:
        """``{"stop": int|None, "why"|"why_not": code, ...}`` for one index node, codes in
        ref space (see the module docstring)."""
        x = self.node(nid)
        if x is None:
            return {"stop": None, "why_not": "not_in_a11y_tree", "reachable": "not"}
        if isinstance(x, Excluded):
            if x.reason == "hidden":
                by = self.nid_of_raw(x.hidden_by) or "?"
                return {"stop": None, "why_not": f"hidden_by:{by}", "reachable": "not",
                        "detail": ("importantForAccessibility=noHideDescendants"
                                   + (" (on this node)" if by == nid else "")
                                   + " removes the subtree from what TalkBack gets")}
            parent = self.nid(x.parent)
            return {"stop": None, "why_not": "not_important", "reachable": "not",
                    "detail": "not important for accessibility: TalkBack never gets it; its "
                              "children are read in its place"
                              + (f" (under {parent})" if parent else "")}
        return self._explain_tb(x)

    def _explain_tb(self, n: TbNode) -> dict[str, Any]:
        if not n.window.reported:
            dropped = n.window.dropped or "skipped"
            if dropped.startswith("covered_by:"):
                by = self.window_ref(dropped.split(":", 1)[1])
                return {"stop": None, "why_not": f"covered_by:{by or dropped.split(':', 1)[1]}",
                        "reachable": "not",
                        "detail": "its window is under a modal window TalkBack reads instead"}
            return {"stop": None, "why_not": dropped, "reachable": "not"}
        stop = why_stop(self.rules, n)
        if stop is not None:
            out: dict[str, Any] = {"stop": self.stop_no(n), "why": stop}
            ghost = self.ghost(n)
            if ghost:
                out["ghost"] = ghost
            out["reachable"] = "swipe" if self.stop_no(n) is not None else "not"
            return out
        code, ref, detail = self.why_not(n)
        out = {"stop": None, "why_not": f"{code}:{ref}" if ref else code}
        if detail:
            out["detail"] = detail
        out["reachable"] = self.reachable(n, code)
        return out

    def why_not(self, n: TbNode) -> tuple[str, str | None, str | None]:
        """(code, the node it names or None, a short detail) for a TalkBack-view non-stop."""
        ok, branch = self.rules.focus_decision(n)
        if ok:
            return "stop", None, None
        if branch == "not_visible":
            if "holder_invisible_with_service" in n.corrections:
                return "hidden_holder", None, "an AndroidView holder is invisible while a " \
                                              "screen reader runs"
            if "obscured_by_system_bar" in n.corrections:
                return "under_system_bar", None, None
            if self._offscreen(n):
                return "offscreen", None, None
            if n.rect.is_empty():
                return "zero_size", None, None
            return "invisible", None, "not visible to the user (alpha 0, hidden, or clipped)"
        if branch == "focusable_ancestor":
            anc = self.rules.focusable_ancestor(n)
            if anc is None:
                return "no_speech", None, None
            if not self.rules.should_focus_node(anc):
                # a focusable container TalkBack never stops on (silent_container)
                return "inside_silent", self.nid(anc), (
                    "under a focusable container TalkBack does not stop on, which reads "
                    "nothing")
            if not self._read_by(anc, n):
                return "silenced_by", self.nid(anc), (
                    "its contentDescription replaces its children's text"
                    if anc.content_description else
                    "TalkBack reads that stop without this node's text")
            return "merged_into", self.nid(anc), None
        if branch == "silent_container":
            kids = [self.nid(c) for c in n.children if self.rules.should_focus_node(c)]
            kids = [k for k in kids if k]
            return "silent_container", None, (
                "focusable but nothing of its own to say; the stops are "
                + (",".join(kids[:6]) if kids else "its descendants"))
        if branch == "window_wrapper":
            return "window_wrapper", None, "the size of its window, has children, not focusable"
        return branch or "no_speech", None, None

    def _offscreen(self, n: TbNode) -> bool:
        """Laid out outside its window or outside the nearest scrollable above it: a View
        scrolled out of its ScrollView has its bounds clipped to an empty rect at the
        scroller's edge (not a zero-size View)."""
        r = n.rect
        if not r.is_empty() and r.intersects(n.window.bounds):
            return False
        if _outside(r, n.window.bounds):
            return True
        sc = next((a for a in n.ancestors() if self.rules.is_scrollable(a)), None)
        return sc is not None and _outside(r, sc.rect)

    def _read_by(self, anc: TbNode, n: TbNode) -> bool:
        """Whether the stop ``anc`` says something of ``n`` (or of a node inside it), or
        ``n`` has nothing to say: a View container's contentDescription silences its
        children (design 1(c)), so a child under it is not merged into it."""
        keys = {x.key for x in n.iter()}
        ann = self.own(anc)
        if any(p.get("from") in keys for p in ann.parts if p.get("from") != anc.key):
            return True
        texts = [t for x in n.iter() for t in (x.text, x.content_description,
                                                x.state_description) if t]
        if not texts:
            return True  # nothing of its own to say: merged, silently
        said = ann.text.lower()
        return any(t.strip().lower() in said for t in texts)

    def reachable(self, n: TbNode, code: str) -> str:
        if code in ("merged_into", "stop"):
            return "swipe"
        if code in ("offscreen", "zero_size", "invisible"):
            for a in n.ancestors():
                if self.rules.filter_auto_scroll(a):
                    return "scroll"
        return "not"

    def ghost(self, n: TbNode) -> list[str]:
        """Ghost reasons of a stop (talkback.static.ghost: not for a clipped item TalkBack
        scrolls into view first), refs for the scrollable a clipped sliver sits in."""
        from ..talkback.static import ghost

        out = []
        for g in ghost(self.nav, n, self.density):
            if g.startswith("clipped:"):
                sc = self.by_key(g.split(":", 1)[1])
                g = f"clipped:{self.nid(sc) or g.split(':', 1)[1]}"
            out.append(g)
        return out

    def edge_in(self, n: TbNode, prev: TbNode | None) -> str:
        """How a forward swipe reaches ``n``: ``tree``, ``bounds_swap``, ``chain``,
        ``before:<ref>``, ``after:<ref>``, ``window:<ref>`` (the first stop of another
        window) or ``first``."""
        if prev is None:
            return "first"
        if prev.window is not n.window:
            return f"window:{self._window_nid(n)}"
        via = self.nav.traversal(n.window).via(n)
        if ":" in via:
            kind, key = via.split(":", 1)
            other = self.by_key(key)
            return f"{kind}:{self.nid(other) or key}"
        return via

    # ------------------------------------------------------------------ reading
    def reading(self, *, granularity: str = "default", start: str | None = None,
                direction: str = "next", include_skipped: bool = False,
                limit: int = 2000) -> tuple[list[ReadItem], dict[str, Any]]:
        """The stops in swipe order (``ReadItem``\\ s) and facts about the walk.

        Without ``start`` every stop of the screen in reading order (``direction="prev"``:
        backwards from the last); with ``start`` (a node id) the stops a swipe reaches from
        that node until the edge, the start itself first when it is a stop the granularity
        keeps (a container TalkBack never gets starts at its first stop, ``moved_to`` in the
        facts). ``include_skipped`` adds the nodes the walk passes over that carry content
        or actions, each with its reason."""
        forward = direction != "prev"
        accept = self.rules.node_filter(granularity)
        meta: dict[str, Any] = {}
        items: list[ReadItem] = []
        if start is None:
            seq = [n for n in self.linear() if accept(n)]
            if not forward:
                seq.reverse()
            st = SpeechState()
            stops = {id(n) for n in seq}
            if include_skipped:
                order = self._traversal_all(forward)
            else:
                order = seq
            prev: TbNode | None = None
            for x in order:
                if isinstance(x, TbNode) and id(x) in stops:
                    item = self._stop_item(x, announce(self.nav, x, st), prev, forward=forward)
                    prev = x
                    if item is not None:
                        items.append(item)
                elif include_skipped:
                    item = self._skip_item(x)
                    if item is not None:
                        items.append(item)
                if len(items) >= limit:
                    break
            if include_skipped:
                items.extend(self._unreported_windows())
            meta["ended"] = "edge"
            return items, meta
        x = self.node(start)
        if x is None or isinstance(x, Excluded):
            inner = self.first_stop_within(start)
            if inner is None:
                meta["start"] = "not a TalkBack node: " + \
                    self.explain(start).get("why_not", "?")
                meta["ended"] = "empty"
                return items, meta
            # a container TalkBack never gets (not important, a ComposeView host): the walk
            # starts at the first stop inside it
            meta["moved_to"] = self.nid(inner)
            x = inner
        pivot: TbNode = x
        st = SpeechState()
        seen = {id(pivot)}
        if self.rules.should_focus_node(pivot) and accept(pivot) and pivot.window.reported:
            item = self._stop_item(pivot, announce(self.nav, pivot, st), None)
            if item is not None:
                item.via = "start"
                items.append(item)
        reach_edge = False
        ended = "edge"
        while len(items) < limit:
            res = self.nav.step(pivot, forward, granularity, reach_edge)
            target = res["target"]
            if target is None or res["via"] == "wrap":
                break
            if id(target) in seen:
                ended = "loop"
                break
            seen.add(id(target))
            if include_skipped and target.window is pivot.window:
                for s in self._between(pivot, target, forward):
                    item = self._skip_item(s)
                    if item is not None:
                        items.append(item)
            item = self._stop_item(target, announce(self.nav, target, st), pivot,
                                   via=res["via"], forward=forward)
            if item is not None:
                if res.get("autoscroll") is not None:
                    item.extra["autoscroll"] = self.nid(res["autoscroll"]) or "?"
                items.append(item)
            pivot = target
            reach_edge = res["reach_edge"]
        meta["ended"] = ended
        return items, meta

    def first_stop_within(self, nid: str) -> TbNode | None:
        """The first stop (in swipe order) inside the index node ``nid``: its dump subtree
        when TalkBack never gets it, else its subtree in the capture's trees."""
        order = {id(n): i for i, n in enumerate(self.linear())}
        cands: list[TbNode] = []
        x = self.node(nid)
        if isinstance(x, Excluded):
            if x.reason == "hidden":
                return None
            stack = [x.raw]
            while stack:
                raw = stack.pop()
                n = self.tree.by_raw.get(id(raw))
                if n is not None and id(n) in order:
                    cands.append(n)
                stack.extend(raw.get("children") or ())
        elif x is None and self.ix is not None:
            for tree in ("ui", "views"):
                for u, _d in self.ix.walk(tree, nid):
                    n = self.node(u.id)
                    if isinstance(n, TbNode) and id(n) in order:
                        cands.append(n)
                if cands:
                    break
        return min(cands, key=lambda n: order[id(n)]) if cands else None

    def _stop_item(self, n: TbNode, ann: Announcement, prev: TbNode | None,
                   via: str | None = None, forward: bool = True) -> ReadItem | None:
        from ..talkback.static import show_on_screen

        nid = self.nid(n)
        if nid is None:
            return None
        item = ReadItem(nid=nid, stop=self.stop_no(n), speak=ann.text,
                        why=why_stop(self.rules, n))
        if via is None or not via.startswith("window"):
            item.via = self.edge_in(n, prev)
        else:
            item.via = f"window:{self._window_nid(n)}"
        sos = show_on_screen(self.nav, n, forward)
        if sos is not None:
            item.extra["show_on_screen"] = self.nid(sos) or "?"
            if ann.unlabelled:
                # a sliver at the edge: TalkBack scrolls it in first and says what it shows
                # then, which this dump does not hold; the line keeps the node's label
                item.speak = None
                item.extra["speak"] = "after_scroll"
        ghost = self.ghost(n)
        if ghost:
            item.extra["ghost"] = ",".join(ghost)
        return item

    def _skip_item(self, x: TbNode | Excluded) -> ReadItem | None:
        nid = self.nid(x)
        if nid is None or not _has_content(self.rules, x):
            return None
        if isinstance(x, Excluded):
            ex = self.explain(nid)
            code = ex["why_not"]
        else:
            code, ref, _detail = self.why_not(x)
            if code == "stop":
                return None
            code = f"{code}:{ref}" if ref else code
        kind, _, ref = code.partition(":")
        if ref:
            return ReadItem(nid=nid, ref_key=kind, ref=ref)
        return ReadItem(nid=nid, why=kind)

    def _unreported_windows(self) -> list[ReadItem]:
        """One ``- `` line per window TalkBack never gets: its root, with the modal window
        that covers it (``covered_by=<ref>``) or why it is dropped."""
        out: list[ReadItem] = []
        for w in self.tree.windows:
            if w.root is None or w.reported:
                continue
            nid = self.window_ref(w.root_view_id) or self.nid(w.root)
            if nid is None:
                continue
            dropped = w.dropped or "skipped"
            if dropped.startswith("covered_by:"):
                by = self.window_ref(dropped.split(":", 1)[1])
                out.append(ReadItem(nid=nid, ref_key="covered_by", ref=by or "?"))
            else:
                out.append(ReadItem(nid=nid, why=dropped))
        return out

    def _traversal_all(self, forward: bool) -> list[TbNode | Excluded]:
        """Every node TalkBack's traversal passes, window by window, with the dump nodes it
        never gets placed after the node they were hoisted into (or hidden under)."""
        hoisted: dict[int, list[Excluded]] = {}
        for ex in self.tree.excluded:
            if ex.reason == "hidden" and ex.raw is not ex.hidden_by:
                continue  # the top of a hidden subtree stands for it (its count is shown)
            hoisted.setdefault(id(ex.parent), []).append(ex)
        out: list[TbNode | Excluded] = []
        for w in self.nav.windows:
            if not self.nav.accepts_window(w):
                continue
            for n in self.nav.traversal(w).order:
                out.append(n)
                out.extend(hoisted.get(id(n), ()))
        if not forward:
            out.reverse()
        return out

    def _between(self, a: TbNode, b: TbNode, forward: bool) -> list[TbNode]:
        trav = self.nav.traversal(a.window)
        i, j = trav.pos.get(id(a)), trav.pos.get(id(b))
        if i is None or j is None:
            return []
        if forward and i < j:
            return trav.order[i + 1:j]
        if not forward and j < i:
            return list(reversed(trav.order[j + 1:i]))
        return []

    # ------------------------------------------------------------------ node facet
    def facet(self, nid: str) -> dict[str, Any]:
        """The ``tb`` facet of ``node()``: stop, why / why_not, what TalkBack says and from
        which nodes, the neighbouring stops, how focus arrives, whether it is reachable."""
        x = self.node(nid)
        ex = self.explain(nid)
        out: dict[str, Any] = {"stop": ex.get("stop")}
        if ex.get("why"):
            out["why"] = ex["why"]
        if ex.get("why_not"):
            wn = ex["why_not"]
            out["why_not"] = f"{wn}: {ex['detail']}" if ex.get("detail") else wn
        if ex.get("ghost"):
            out["ghost"] = ex["ghost"]
        if isinstance(x, TbNode) and ex.get("stop") is not None:
            lin = self.linear()
            i = (self.stop_no(x) or 1) - 1
            ann = self.walk_speech(x)
            out["speak"] = ann.text
            out["parts"] = [self._part(p) for p in ann.parts][:8]
            prev = lin[i - 1] if i > 0 else None
            nxt = lin[i + 1] if i + 1 < len(lin) else None
            out["prev"] = self.nid(prev) if prev is not None else None
            out["next"] = self.nid(nxt) if nxt is not None else None
            out["edge_in"] = self.edge_in(x, prev)
        elif isinstance(x, TbNode):
            own = self.own(x)
            if own.text and ex.get("why_not", "").startswith("merged_into"):
                out["speak_in"] = own.text  # what this node adds to the stop that reads it
        out["reachable"] = ex.get("reachable", "not")
        return out

    def _part(self, p: Mapping[str, Any]) -> dict[str, Any]:
        src = self.by_key(p.get("from") or "")
        return {"t": p.get("text"), "from": self.nid(src) or p.get("from"), "k": p.get("kind")}


def _outside(r: Any, o: Any) -> bool:
    """Whether rect ``r`` (possibly empty) lies wholly past an edge of ``o``."""
    return (r.top >= o.bottom or r.bottom <= o.top or r.left >= o.right
            or r.right <= o.left)


def _has_content(rules: Any, x: TbNode | Excluded) -> bool:
    """A node worth a line among the skipped: it has text, a description, a hint or a
    state, or it acts (click, long-click, focus, checkable)."""
    raw = x.raw
    if any(raw.get(k) for k in ("text", "content_description", "hint_text",
                                "state_description")):
        return True
    fl = set(raw.get("flags") or ())
    if isinstance(x, Excluded):
        return bool(fl & {"clickable", "long_clickable", "checkable"}) or (
            x.reason == "hidden" and x.raw is x.hidden_by)
    return bool(fl & {"clickable", "long_clickable", "checkable", "screen_reader_focusable"}) \
        or rules.is_actionable_for_accessibility(x)


def stop_speech(resp: Any) -> dict[tuple[int, tuple[int, ...]], StopSpeech]:
    """``{(root_view_id, child-index path): StopSpeech}`` for every TalkBack stop of a
    DumpA11yResponse (message or bytes)."""
    return TbCapture(resp).stop_speech()


#: View classes apps commonly lift above their siblings with elevation (Material's defaults):
#: without the capture's properties, such a View drawn earlier may still be drawn later.
ELEVATED_CLASSES = frozenset({
    "AppBarLayout", "FloatingActionButton", "ExtendedFloatingActionButton", "CardView",
    "MaterialCardView", "BottomNavigationView", "BottomAppBar", "NavigationRailView",
    "NavigationView", "SnackbarLayout", "SnackbarContentLayout", "MaterialToolbar"})


def props_of(loaded: Any) -> Any:
    """The capture's View properties accessor ``props(view udid) -> {name: value}`` (a
    stored capture's, or decoded from a RawCapture's views facet), or None."""
    from .model import RawCapture

    fn = getattr(loaded, "props", None)
    if callable(fn):
        return fn
    obj = getattr(loaded, "obj", None)  # analyzers._Src
    if obj is not None and obj is not loaded:
        return props_of(obj)
    if isinstance(loaded, RawCapture) and loaded.views:
        from .index import FacetReader

        fr = FacetReader(loaded)
        try:
            return fr.props if fr.has_props() else None
        except Exception:  # noqa: BLE001 - no properties: the class heuristic decides
            return None
    return None


def drawn_above(ix: Index, props: Any = None) -> Any:
    """``drawn_above(a, b)`` for two dump nodes of one capture, from its View tree: True when
    ``a``'s View is drawn over ``b``'s, False when under, None when the capture cannot tell
    (the same View, e.g. two Compose nodes of one ComposeView; one View inside the other;
    another window; a View the capture lacks).

    At the two Views' lowest common ancestor a ViewGroup draws (and dispatches touches to)
    its children by Z (elevation + translationZ), then child order (buildOrderedChildList).
    ``props`` (:func:`props_of`) gives each View's Z; without it, child order decides,
    except when the earlier child is a View apps commonly elevate (``ELEVATED_CLASSES``: an
    AppBarLayout, a FAB, a CardView): then it is unknown."""
    tree = ix.tree("views")
    pos: dict[str, int] = {}
    parent: dict[str, str] = {}
    for i, r in enumerate(tree.roots):
        pos[r] = i
    for p, kids in tree.children.items():
        for i, c in enumerate(kids):
            pos[c] = i
            parent[c] = p
    paths: dict[str, list[str] | None] = {}
    zs: dict[str, float | None] = {}

    def path(nid: str) -> list[str] | None:
        """The View ids from the root down to ``nid``."""
        if nid in paths:
            return paths[nid]
        out: list[str] = []
        cur: str | None = nid
        seen: set[str] = set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            if cur not in pos:
                paths[nid] = None
                return None
            out.append(cur)
            cur = parent.get(cur)
        paths[nid] = out[::-1]
        return paths[nid]

    def z(nid: str) -> float | None:
        if props is None:
            return None
        if nid not in zs:
            zs[nid] = None
            node = ix.nodes.get(nid)
            udid = node.ids.get("view") if node is not None else None
            try:
                p = props(int(udid)) if udid is not None else None
                if p and ("elevation" in p or "translationZ" in p):
                    zs[nid] = float(p.get("elevation") or 0) + float(p.get("translationZ") or 0)
            except (TypeError, ValueError, OSError):
                zs[nid] = None
        return zs[nid]

    def view_of(raw: dict[str, Any]) -> str | None:
        host = raw.get("host_view_id")
        return ix.resolve_id(f"view:{int(host)}") if host else None

    def above(a: dict[str, Any], b: dict[str, Any]) -> bool | None:
        va, vb = view_of(a), view_of(b)
        if va is None or vb is None or va == vb:
            return None
        pa, pb = path(va), path(vb)
        if not pa or not pb or pa[0] != pb[0]:
            return None
        for x, y in zip(pa, pb):
            if x == y:
                continue
            later = pos[x] > pos[y]
            za, zb = z(x), z(y)
            if za is not None and zb is not None:
                return za > zb if za != zb else later
            earlier = ix.nodes.get(y if later else x)
            if earlier is not None and str(earlier.type or "") in ELEVATED_CLASSES:
                return None  # its elevation may draw it over the later one
            return later
        return None  # one View holds the other

    return above


def view_chain(ix: Index) -> Any:
    """``view_chain(view id)``: the View's ancestors in the capture's View tree, nearest
    first, as ``(view id, (x, y, w, h))`` (its visible rect)."""
    parents = ix.tree("views").parents()

    def chain(vid: int) -> list[tuple[int, tuple[int, int, int, int]]]:
        out: list[tuple[int, tuple[int, int, int, int]]] = []
        cur = ix.resolve_id(f"view:{int(vid)}")
        seen: set[str] = set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            cur = parents.get(cur)
            node = ix.nodes.get(cur) if cur is not None else None
            if node is None or node.kind != "view":
                break
            udid = node.ids.get("view")
            if udid is None or not node.b:
                break
            out.append((int(udid), tuple(int(v) for v in node.b[:4])))  # type: ignore[misc]
        return out

    return chain


#: Composables (or modifiers) that act on a gesture TalkBack cannot perform: without a
#: labelled custom action the action is unreachable with a screen reader.
GESTURE_COMPOSABLES = ("SwipeToDismissBox", "SwipeToDismiss", "SwipeableActionsBox")
GESTURE_MODIFIERS = ("anchoreddraggable", "swipeable", "swipetodismiss")


def _labelled_actions(n: Any) -> list[dict[str, Any]]:
    return [a for a in n.raw.get("actions") or () if isinstance(a, dict) and a.get("label")]


def _custom_actions_missing(ix: Index, tbc: TbCapture) -> list[tuple[str, Issue]]:
    """tb.custom_action_missing: a stop emitted inside a swipe-to-dismiss (or anchored
    draggable) composable that offers no labelled custom action. Needs the slot table
    (``capture(slots="enable")``); silent without it."""
    out: list[tuple[str, Issue]] = []
    stops = {id(x) for x in tbc.linear()}
    for n in tbc.linear():
        nid = tbc.nid(n)
        node = ix.nodes.get(nid) if nid else None
        slots = ((node.facets.get("compose") or {}).get("slots") or []) if node else []
        if not slots:
            continue
        gesture = None
        seen: set[str] = set()
        frontier = list(slots)
        hops = 0
        while frontier and gesture is None and hops < 8:
            nxt = []
            for sid in frontier:
                s = ix.nodes.get(sid)
                if s is None or sid in seen:
                    continue
                seen.add(sid)
                name = (s.facets.get("slot") or {}).get("name") or s.type or ""
                mods = str((s.facets.get("slot") or {}).get("mods") or "").lower()
                if name in GESTURE_COMPOSABLES or any(m in mods for m in GESTURE_MODIFIERS):
                    gesture = (name, s)
                    break
                if s.parent:
                    nxt.append(s.parent)
            frontier = nxt
            hops += 1
        if gesture is None:
            continue
        if _labelled_actions(n):
            continue
        name, s = gesture
        ev: dict[str, Any] = {"gesture": name}
        if s.src:
            ev["src"] = s.src
        # The action on a container TalkBack never focuses (A11yProbe C11 GOOD: customActions
        # on the SwipeToDismissBox, the stop is the Text inside): still out of reach.
        holder = next((a for a in list(n.ancestors())[:6] if _labelled_actions(a)), None)
        if holder is not None:
            if id(holder) in stops:
                continue  # TalkBack focuses the holder too: the action is in its menu
            ev["why"] = "action_on_a_node_talkback_never_focuses"
            hid = tbc.nid(holder)
            if hid:
                ev["node_ids"] = [hid]  # named by ref, as the other rules' other nodes
        out.append((nid, Issue("tb.custom_action_missing", "warn", ev, "inferred")))
    return out


def issues(ix: Index, loaded: Any, *, density: int | None = None,
           tbc: TbCapture | None = None) -> tuple[list[tuple[str, Issue]], list[str]]:
    """The static ``tb.*`` findings of a capture (:mod:`inspector_widget.talkback.static`,
    plus tb.custom_action_missing from the slot table) as ``(node id, Issue)`` pairs, and
    diagnostics. A finding's other nodes go in the issue's ``node_ids`` evidence; a
    heuristic finding (the visual order) is ``conf: inferred``."""
    from ..talkback import static

    tbc = tbc or TbCapture.of(ix, loaded)
    if tbc is None:
        return [], []
    out: list[tuple[str, Issue]] = []
    unmapped = 0
    for f in static.findings(tbc.nav, density=density or 420,
                             drawn_above=drawn_above(ix, props_of(loaded)),
                             view_chain=view_chain(ix)):
        nid = tbc.nid(f.node)
        if nid is None:
            unmapped += 1
            continue
        ev = dict(f.evidence)
        others = [o for o in (tbc.nid(x) for x in f.others) if o and o != nid]
        if others:
            ev["node_ids"] = others
        out.append((nid, Issue(f.code, f.sev, ev,
                               "inferred" if f.conf == "heuristic" else "exact")))
    out.extend(_custom_actions_missing(ix, tbc))
    diags = [f"tb: {unmapped} TalkBack findings not mapped to nodes"] if unmapped else []
    diags.extend(f"tb: {d['message']}" for d in tbc.tree.diagnostics
                 if d.get("kind") in SURFACED_DIAGNOSTICS)
    return out, diags


def fallback_reading(ix: Index, *, granularity: str = "default", start: str | None = None,
                     direction: str = "next") -> list[ReadItem]:
    """The stored reading order when the model cannot run (no raw a11y facet): sliced,
    reversed and filtered by the node's flags."""
    nodes = [ix.nodes[r] for r in ix.reading if r in ix.nodes]
    if granularity == "heading":
        nodes = [n for n in nodes if "heading" in n.flags]
    elif granularity == "control":
        nodes = [n for n in nodes if set(n.flags) & {"click", "longclick", "checkable", "edit"}]
    if direction == "prev":
        nodes.reverse()
    if start is not None:
        ids = [n.id for n in nodes]
        nodes = nodes[ids.index(start):] if start in ids else []
    return [ReadItem(nid=n.id, stop=n.stop,
                     speak=(n.facets.get("a11y") or {}).get("speakable") or n.label)
            for n in nodes]


def tb_of(ix: Index, loaded: Any) -> TbCapture | None:
    return TbCapture.of(ix, loaded)


__all__ = ["DIRECTIONS", "GESTURE_COMPOSABLES", "GRANULARITIES", "NAME_KINDS", "ReadItem",
           "StopSpeech", "TbCapture", "drawn_above", "fallback_reading", "issues", "name_of",
           "stop_speech", "tb_of"]
