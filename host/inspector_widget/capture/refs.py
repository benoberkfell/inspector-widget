"""Refs and carry-over: stable node handles across captures of one lineage.

Spec "Capture and Walk", sections 3.4 and 3.9. A ref (``n23``) is store-global,
monotonic and never reused. When a capture is taken, its index (keyed by canonical
key, as ``index.build_index`` returns it) is matched against the latest capture of
the same lineage (keyed by ref). A matched node keeps its old ref; every other node
gets a fresh ref from the store's counter; every old ref left unmatched becomes a
tombstone in the lineage file. This module is pure: the store (C2) holds the lock,
the counter and the lineage file, and passes ``alloc`` in.

Matching runs these passes, each over the nodes the earlier ones left unmatched:

1. **Device identity**, only when the pid and the Compose generation are both
   unchanged: the canonical key is equal. ``view:``, ``sem:`` and ``a11y:`` keys
   are device identity. Slot keys and ID1 path keys are positional (an insertion
   can shift them onto another node), so they count only when the type, label and
   content agree as well.
   **Collection guard**: inside a collection item (a child of a RecyclerView,
   ListView, GridView, Lazy list or any node with CollectionInfo), the cell must
   still show the same item. It does when its identity label (the first label in
   the cell that no other cell of that collection has) is unchanged or absent on
   both sides, or when the cell sits at the same position and its data did not
   move to another cell. Otherwise the whole cell gets new refs with
   ``rebound_of`` (a recycled cell). When the cell root itself lost its identity
   match (re-minted), nothing inside it carries by id; the later passes decide.
2. **Unique locators** present once on each side: ``(window z, #rid)``, ``@tag``
   and the a11y uniqueId.
3. **Structure**: the parent is matched, and the kind, type, rid, tag and label
   are equal. A node that is the only such sibling on both sides matches even if
   its ordinal changed (so an insertion above keeps the refs below it). Look-alike
   siblings are told apart by their content (the first labels below them); true
   twins match by ordinal only when the whole twin group is unchanged (same count,
   sizes and child counts, in order). Otherwise they are ambiguous. Cells of a
   collection match by content only, never by ordinal.
4. **Geometry**: same kind and type, compatible label or content, IoU >= 0.8,
   one-to-one greedy within the matched parent (or the window when the parent is
   unmatched). A tie is never broken by guessing. Nodes inside collection cells
   are left out: in a scrolling list a position is not an identity.

Passes 3 and 4 repeat until nothing new matches, since a geometry match can make
its children matchable by structure. Ambiguous candidates get new refs, and their
subtrees are kept out of the geometry pass, so look-alikes are never swapped.

A first capture (``prev=None``) allocates every ref in pre-order: ui tree first
(windows in z order), then the slot tree. Allocation is one ``alloc(n)`` call per
capture and is deterministic for identical inputs.

``assign`` records provenance on the new index's nodes (``match``, ``since`` and
``rebound_of``) so ``apply_refs`` carries it into the published index.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .model import (
    Index,
    LineageState,
    OpError,
    UNode,
    is_capture_id,
    is_ref,
    parse_key,
    ref_num,
    ref_str,
)

TOMB_CAP = 5000
TOMB_LABEL_MAX = 40
GEOMETRY_MIN_IOU = 0.8
#: Upper bound on structure+geometry rounds (each round usually settles a level).
MAX_ROUNDS = 8
#: parse_key kinds that are device identity (stable while the process lives).
IDENTITY_KEY_KINDS = frozenset({"view", "sem", "a11y"})
#: Positional keys (slot anchors, ID1 a11y paths): an insertion can shift them onto
#: another node, so pass 1 accepts them only when type, label and content agree too.
WEAK_KEY_KINDS = frozenset({"slot", "a11y_path"})
#: View classes whose children are collection items (matched on the simple name).
COLLECTION_CLASSES = frozenset({
    "RecyclerView", "ListView", "GridView", "ExpandableListView", "HorizontalGridView",
    "VerticalGridView", "StaggeredGridView", "AbsListView",
})
#: Display types (composable or View names) whose children are collection items.
COLLECTION_TYPES = frozenset({
    "LazyColumn", "LazyRow", "LazyVerticalGrid", "LazyHorizontalGrid",
    "LazyVerticalStaggeredGrid", "LazyHorizontalStaggeredGrid", "RecyclerView", "ListView",
    "GridView",
})
#: How many labels identify a cell (the first distinguishing one is used).
_ITEM_LABELS = 8
#: How many labels make a node's content signature.
_CONTENT_LABELS = 3
_GRID = 64
_ITEM_INDEX_RE = re.compile(r"\[(\d+)\]")
_ROOT = ""  # the virtual parent of every root

Alloc = Callable[[int], int]


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #
@dataclass
class Assignment:
    """Everything one carry-over decided. ``assign()`` returns its ``refmap`` and
    ``tomb``; ``plan()`` returns the whole record (for diagnostics and tests)."""

    refmap: dict[str, str] = field(default_factory=dict)  # canonical key -> ref
    tomb: dict[str, list] = field(default_factory=dict)  # retired ref -> tombstone
    match: dict[str, str] = field(default_factory=dict)  # canonical key -> match kind
    since: dict[str, str | None] = field(default_factory=dict)  # canonical key -> capture id
    rebound: dict[str, str] = field(default_factory=dict)  # canonical key -> old ref
    ambiguous: int = 0  # new nodes left unmatched because candidates were ambiguous
    first_ref: int | None = None  # first number of the allocated block
    allocated: int = 0

    @property
    def stats(self) -> dict[str, int]:
        out = {k: 0 for k in ("id", "locator", "structure", "geometry", "new")}
        for how in self.match.values():
            out[how] = out.get(how, 0) + 1
        out["rebound"] = len(self.rebound)
        out["retired"] = len(self.tomb)
        out["ambiguous"] = self.ambiguous
        return out


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def identity_flags(new_meta: Any, prev_meta: Any) -> tuple[bool, bool]:
    """``(same_pid, same_generation)`` for two CaptureMetas (either may be None)."""
    if new_meta is None or prev_meta is None:
        return False, False
    np_, pp = getattr(new_meta, "pid", None), getattr(prev_meta, "pid", None)
    same_pid = np_ is not None and np_ == pp
    same_gen = (getattr(new_meta, "compose_generation", 0)
                == getattr(prev_meta, "compose_generation", 0))
    return same_pid, same_gen


def _cut(s: str | None, n: int) -> str | None:
    if s is None:
        return None
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def tomb_entry(node: UNode, capture_id: str | None) -> list:
    """``[type, label<=40, sel, last_capture]`` (spec 3.9). ``type`` falls back to the kind."""
    return [node.type or node.kind, _cut(node.label, TOMB_LABEL_MAX), node.sel or node.ref,
            capture_id]


def merge_tomb(tomb: Mapping[str, list], updates: Mapping[str, list],
               cap: int = TOMB_CAP) -> dict[str, list]:
    """A new tombstone dict: ``tomb`` plus ``updates`` (newest last), capped at
    ``cap`` entries by dropping the least recently used (the front)."""
    out = {k: list(v) for k, v in tomb.items() if k not in updates}
    for k, v in updates.items():
        out[k] = list(v)
    if cap >= 0 and len(out) > cap:
        for k in list(out)[: len(out) - cap]:
            del out[k]
    return out


def touch_tomb(tomb: dict[str, list], ref: str) -> list | None:
    """Look a retired ref up and mark it recently used (moves it to the end, in place)."""
    entry = tomb.pop(ref, None)
    if entry is not None:
        tomb[ref] = entry
    return entry


def apply_to_lineage(state: LineageState, tomb_updates: Mapping[str, list],
                     cap: int = TOMB_CAP) -> LineageState:
    """Fold one capture's tombstones into a lineage state (in place; returns it)."""
    state.tomb = merge_tomb(state.tomb, tomb_updates, cap)
    return state


def stale_ref_error(ref: str, capture_id: str | None,
                    tomb: Mapping[str, list] | None = None) -> OpError:
    """``ref_not_in_capture`` with the last-seen info a tombstone holds."""
    entry = (tomb or {}).get(ref)
    where = f" {capture_id}" if capture_id else ""
    if not entry:
        return OpError("ref_not_in_capture", f"{ref} is not in capture{where}",
                       "Capture again, or find the node with find(text=...).")
    typ, label, sel, last = (list(entry) + [None] * 4)[:4]
    seen = f"{typ}" + (f' "{label}"' if label else "")
    hint = f"Last seen in {last} as {seen}" if last else f"Last seen as {seen}"
    if sel and sel != ref:
        hint += f"; try {sel}"
    return OpError("ref_not_in_capture", f"{ref} is not in capture{where}", hint + ".",
                   candidates=[sel] if sel and sel != ref else None)


# --------------------------------------------------------------------------- #
# Per-index precomputation
# --------------------------------------------------------------------------- #
def preorder(ix: Index) -> list[str]:
    """Primary-tree pre-order: ui roots (z order), slot roots, then any other
    parentless or unreachable node, in ``ix.nodes`` order."""
    nodes = ix.nodes
    seen: set[str] = set()
    out: list[str] = []

    def visit(root: str) -> None:
        stack = [root]
        while stack:
            nid = stack.pop()
            if nid in seen or nid not in nodes:
                continue
            seen.add(nid)
            out.append(nid)
            stack.extend(reversed(nodes[nid].children))

    roots = list(ix.tree("ui").roots) + list(ix.tree("slots").roots)
    roots += [nid for nid, n in nodes.items() if n.parent is None or n.parent not in nodes]
    for r in roots:
        visit(r)
    for nid in nodes:
        visit(nid)
    return out


def _area(b: list[int] | None) -> int:
    if not b or len(b) < 4:
        return 0
    return max(0, int(b[2])) * max(0, int(b[3]))


def iou(a: list[int] | None, b: list[int] | None) -> float:
    """Intersection over union of two ``[x, y, w, h]`` boxes (0 when either is empty)."""
    aa, ab = _area(a), _area(b)
    if not aa or not ab:
        return 0.0
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
    return inter / float(aa + ab - inter) if inter else 0.0


def _is_container(n: UNode) -> bool:
    vf = n.facets.get("view") or {}
    simple = str(vf.get("class") or "").rsplit(".", 1)[-1]
    if simple in COLLECTION_CLASSES or simple.endswith(("RecyclerView", "ListView", "GridView")):
        return True
    if n.type in COLLECTION_TYPES:
        return True
    if (n.facets.get("a11y") or {}).get("collection"):
        return True
    attrs = (n.facets.get("compose") or {}).get("attrs") or {}
    return "CollectionInfo" in attrs


def _anchor_index(anchor: str | None) -> int | None:
    """The collection index ``[i]`` of an anchor's last segment, if any."""
    if not anchor:
        return None
    found = _ITEM_INDEX_RE.findall(anchor.rsplit("/", 1)[-1])
    return int(found[-1]) if found else None


class _Side:
    """Derived data of one index: order, sibling lists, content signatures,
    collection items and their identity labels, locators."""

    def __init__(self, ix: Index) -> None:
        self.ix = ix
        self.nodes = ix.nodes
        self.order = preorder(ix)
        self.pos = {nid: i for i, nid in enumerate(self.order)}
        self.parent: dict[str, str] = {}
        self.kids: dict[str, list[str]] = {_ROOT: []}
        for nid in self.order:
            n = self.nodes[nid]
            p = n.parent if n.parent in self.nodes else _ROOT
            self.parent[nid] = p
            self.kids.setdefault(p, []).append(nid)
        # content signature: the first labels of the subtree (own label first)
        labels: dict[str, list[str]] = {}
        for nid in reversed(self.order):
            n = self.nodes[nid]
            acc = [n.label] if n.label else []
            for c in self.kids.get(nid, ()):
                if len(acc) >= _CONTENT_LABELS:
                    break
                acc.extend(labels.get(c, ()))
            labels[nid] = acc[:_CONTENT_LABELS]
        self.csig = {nid: tuple(v) for nid, v in labels.items()}
        self.z: dict[str, int | None] = {}
        for nid in self.order:
            w = self.nodes[nid].window
            wn = self.nodes.get(w) if w is not None else None
            self.z[nid] = wn.z if wn is not None else None
        self._items()

    # ---------------------------------------------------------------- collections
    def _items(self) -> None:
        self.item_of: dict[str, str] = {}  # node -> nearest enclosing item root
        self.item_container: dict[str, str] = {}  # item root -> its container
        self.item_pos: dict[str, int | None] = {}
        self.containers = containers = {nid for nid in self.order
                                        if _is_container(self.nodes[nid])}
        child_idx = {c: i for kids in self.kids.values() for i, c in enumerate(kids)}
        for nid in self.order:
            n = self.nodes[nid]
            p = self.parent[nid]
            if p != _ROOT and (p in containers or _anchor_index(n.anchor) is not None):
                self.item_of[nid] = nid
                self.item_container[nid] = p
                if n.adapter_pos is not None:
                    pos: int | None = int(n.adapter_pos)
                else:
                    pos = _anchor_index(n.anchor)
                    if pos is None:
                        pos = child_idx[nid]
                self.item_pos[nid] = pos
            elif p in self.item_of:
                self.item_of[nid] = self.item_of[p]
        item_labels: dict[str, list[str]] = {i: [] for i in self.item_container}
        for nid in self.order:
            label = self.nodes[nid].label
            item = self.item_of.get(nid)
            if (label and item is not None and len(item_labels[item]) < _ITEM_LABELS
                    and label not in item_labels[item]):
                item_labels[item].append(label)
        counts: dict[tuple[str, str], int] = {}
        for item, labels in item_labels.items():
            for label in labels:
                key = (self.item_container[item], label)
                counts[key] = counts.get(key, 0) + 1
        self.item_ident: dict[str, str | None] = {}
        self.ident_items: dict[str, dict[str, str]] = {}  # container -> identity -> item
        for item, labels in item_labels.items():
            c = self.item_container[item]
            ident = next((lb for lb in labels if counts[(c, lb)] == 1), None)
            self.item_ident[item] = ident
            if ident is not None:
                self.ident_items.setdefault(c, {})[ident] = item

    # ---------------------------------------------------------------- locators
    def locators(self, nid: str) -> list[tuple]:
        n = self.nodes[nid]
        out: list[tuple] = []
        if n.rid:
            out.append(("rid", self.z.get(nid), n.rid))
        if n.tag:
            out.append(("tag", n.tag))
        a11y = n.facets.get("a11y") or {}
        uid = a11y.get("unique_id") or a11y.get("uniqueId")
        if uid:
            out.append(("uid", str(uid)))
        return out

    def subtree(self, nid: str) -> Iterable[str]:
        stack = [nid]
        while stack:
            cur = stack.pop()
            yield cur
            stack.extend(self.kids.get(cur, ()))

    def sec(self, nid: str) -> tuple:
        """Secondary signature that tells twins apart (size, child count)."""
        b = self.nodes[nid].b
        size = (round(b[2] / 4.0), round(b[3] / 4.0)) if b and len(b) >= 4 else None
        return size, len(self.kids.get(nid, ()))

    def group_key(self, nid: str) -> tuple:
        n = self.nodes[nid]
        return n.kind, n.type, n.rid, n.tag, n.label


# --------------------------------------------------------------------------- #
# The matcher
# --------------------------------------------------------------------------- #
class _Matcher:
    def __init__(self, new: Index, prev: Index, *, same_pid: bool, same_generation: bool) -> None:
        self.new = _Side(new)
        self.prev = _Side(prev)
        self.use_ids = bool(same_pid and same_generation)
        self.n2p: dict[str, str] = {}
        self.p2n: dict[str, str] = {}
        self.how: dict[str, str] = {}
        self.rebound: dict[str, str] = {}  # new id -> prev ref it replaced
        self.blocked: set[str] = set()  # new ids kept out of passes 2-4 (rebound cells)
        self.amb_new: set[str] = set()
        self.amb_prev: set[str] = set()
        self.nogeo_new: set[str] = set()
        self.nogeo_prev: set[str] = set()

    # ---------------------------------------------------------------- bookkeeping
    def _pair(self, n: str, p: str, how: str) -> None:
        self.n2p[n] = p
        self.p2n[p] = n
        self.how[n] = how

    def _free_new(self, n: str) -> bool:
        return n not in self.n2p and n not in self.blocked and n not in self.amb_new

    def _free_prev(self, p: str) -> bool:
        return p not in self.p2n and p not in self.amb_prev

    def _ambiguous(self, news: Iterable[str], prevs: Iterable[str]) -> None:
        for n in news:
            self.amb_new.add(n)
            self.nogeo_new.update(self.new.subtree(n))
        for p in prevs:
            self.amb_prev.add(p)
            self.nogeo_prev.update(self.prev.subtree(p))

    def run(self) -> None:
        if self.use_ids:
            self._identity()
        self._locators()
        for _ in range(MAX_ROUNDS):
            if self._structure() + self._geometry() == 0:
                break

    # ---------------------------------------------------------------- pass 1
    def _identity(self) -> None:
        new, prev = self.new, self.prev
        tent: dict[str, str] = {}
        for nid in new.order:
            n = new.nodes[nid]
            parsed = parse_key(n.key)
            if parsed is None or parsed[0] not in IDENTITY_KEY_KINDS | WEAK_KEY_KINDS:
                continue
            pid = prev.ix.by_key.get(n.key)
            if pid is None or pid not in prev.nodes:
                continue
            p = prev.nodes[pid]
            if p.key != n.key or p.kind != n.kind:
                continue
            if parsed[0] in WEAK_KEY_KINDS and (
                    p.type != n.type or p.label != n.label or prev.csig[pid] != new.csig[nid]):
                continue
            tent[nid] = pid  # canonical keys are unique per index, so this is 1:1
        memo: dict[str, bool | None] = {}

        def item_state(item: str) -> bool | None:
            """True: same item; False: the cell was rebound; None: the cell root
            has no identity match (e.g. re-minted), so nothing inside carries by id."""
            if item in memo:
                return memo[item]
            state = self._item_agrees(item, tent) if item in tent else None
            outer = new.item_of.get(new.parent.get(item, _ROOT))
            if state is not False and outer is not None and item_state(outer) is False:
                state = False  # a rebound outer cell rebinds everything inside it
            memo[item] = state
            return state

        for nid in new.order:
            pid = tent.get(nid)
            item = new.item_of.get(nid)
            state = item_state(item) if item is not None else True
            if state is False:
                self.blocked.add(nid)
                if pid is not None:
                    self.rebound[nid] = pid
            elif state and pid is not None:
                self._pair(nid, pid, "id")

    def _item_agrees(self, item: str, tent: Mapping[str, str]) -> bool:
        new, prev = self.new, self.prev
        p_item = tent[item]
        if p_item not in prev.item_container:
            return True  # not a collection item before: nothing to compare
        ln, lp = new.item_ident.get(item), prev.item_ident.get(p_item)
        if ln == lp:
            # the same identity label, or none on either side (e.g. image-only cells):
            # nothing contradicts the device identity
            return True
        if new.item_pos.get(item) != prev.item_pos.get(p_item):
            return False
        # same position, different identity: an in-place update, unless the data
        # moved to another cell (a rebinding without scrolling)
        other_new = new.ident_items.get(new.item_container[item], {})
        other_prev = prev.ident_items.get(prev.item_container[p_item], {})
        if lp is not None and other_new.get(lp, item) != item:
            return False
        return not (ln is not None and other_prev.get(ln, p_item) != p_item)

    # ---------------------------------------------------------------- pass 2
    def _locators(self) -> None:
        new, prev = self.new, self.prev
        cnt_new: dict[tuple, int] = {}
        cnt_prev: dict[tuple, int] = {}
        by_prev: dict[tuple, str] = {}
        for nid in new.order:
            for loc in new.locators(nid):
                cnt_new[loc] = cnt_new.get(loc, 0) + 1
        for pid in prev.order:
            for loc in prev.locators(pid):
                cnt_prev[loc] = cnt_prev.get(loc, 0) + 1
                by_prev[loc] = pid
        for nid in new.order:
            if not self._free_new(nid):
                continue
            for loc in new.locators(nid):
                if cnt_new.get(loc) != 1 or cnt_prev.get(loc) != 1:
                    continue
                pid = by_prev[loc]
                if self._free_prev(pid) and prev.nodes[pid].kind == new.nodes[nid].kind:
                    self._pair(nid, pid, "locator")
                    break

    # ---------------------------------------------------------------- pass 3
    def _structure(self) -> int:
        new, prev = self.new, self.prev
        made = 0
        for pn in [_ROOT] + new.order:
            pp = _ROOT if pn == _ROOT else self.n2p.get(pn)
            if pp is None:
                continue
            nk = [c for c in new.kids.get(pn, ()) if self._free_new(c)]
            if not nk:
                continue
            pk = [c for c in prev.kids.get(pp, ()) if self._free_prev(c)]
            if not pk:
                continue
            g_new: dict[tuple, list[str]] = {}
            g_prev: dict[tuple, list[str]] = {}
            for c in nk:
                g_new.setdefault(new.group_key(c), []).append(c)
            for c in pk:
                g_prev.setdefault(prev.group_key(c), []).append(c)
            # in a scrolling collection a slot is not an identity: cells carry by
            # content only (no ordinals, no leftovers)
            strict = pn in new.containers or pp in prev.containers
            for key, gn in g_new.items():
                gp = g_prev.get(key)
                if gp:
                    made += self._match_group(gn, gp, strict)
        return made

    def _match_group(self, gn: list[str], gp: list[str], strict: bool = False) -> int:
        new, prev = self.new, self.prev
        if not strict and len(gn) == 1 and len(gp) == 1:
            self._pair(gn[0], gp[0], "structure")
            return 1
        made = 0
        s_new: dict[tuple, list[str]] = {}
        s_prev: dict[tuple, list[str]] = {}
        for c in gn:
            s_new.setdefault(new.csig[c], []).append(c)
        for c in gp:
            s_prev.setdefault(prev.csig[c], []).append(c)
        left_new: list[str] = []
        for sig, sn in s_new.items():
            sp = s_prev.get(sig)
            if not sp:
                left_new.extend(sn)
                continue
            if len(sn) == 1 and len(sp) == 1:
                self._pair(sn[0], sp[0], "structure")
                made += 1
            elif (not strict and len(sn) == len(sp)
                  and [new.sec(c) for c in sn] == [prev.sec(c) for c in sp]):
                for a, b in zip(sn, sp):
                    self._pair(a, b, "structure")
                    made += 1
            else:
                self._ambiguous(sn, sp)
        if strict:
            return made
        left_prev = [c for sig, sp in s_prev.items() if sig not in s_new for c in sp]
        if len(left_new) == 1 and len(left_prev) == 1:
            self._pair(left_new[0], left_prev[0], "structure")
            made += 1
        elif left_new and left_prev:
            self._ambiguous(left_new, left_prev)
        return made

    # ---------------------------------------------------------------- pass 4
    def _compatible(self, n: str, p: str) -> bool:
        a, b = self.new.nodes[n], self.prev.nodes[p]
        if a.label or b.label:
            return a.label == b.label
        ca, cb = self.new.csig[n], self.prev.csig[p]
        return ca == cb or not ca or not cb

    def _geometry(self) -> int:
        new, prev = self.new, self.prev
        # a position inside a scrolling collection is not an identity: cells and
        # their contents never carry by geometry
        cand_new = [n for n in new.order if self._free_new(n) and n not in self.nogeo_new
                    and n not in new.item_of and _area(new.nodes[n].b)]
        if not cand_new:
            return 0
        free_prev = [p for p in prev.order if self._free_prev(p) and p not in self.nogeo_prev
                     and p not in prev.item_of and _area(prev.nodes[p].b)]
        if not free_prev:
            return 0
        free_prev_set = set(free_prev)
        # window scope: prev window id -> (kind, type) -> grid cell -> [prev ids]
        grids: dict[Any, dict[tuple, dict[tuple[int, int], list[str]]]] = {}
        prev_windows_by_z: dict[Any, Any] = {}
        for w in prev.ix.windows():
            prev_windows_by_z.setdefault(w.z, w.id)
        for p in free_prev:
            node = prev.nodes[p]
            b = node.b
            cell = (int(b[0]) // _GRID, int(b[1]) // _GRID)
            grids.setdefault(node.window, {}).setdefault((node.kind, node.type), {}) \
                .setdefault(cell, []).append(p)

        pairs: list[tuple[float, int, int, str, str]] = []
        for n in cand_new:
            node = new.nodes[n]
            pn = new.parent[n]
            pp = _ROOT if pn == _ROOT else self.n2p.get(pn)
            if pp is not None:
                cands: Iterable[str] = [c for c in prev.kids.get(pp, ()) if c in free_prev_set]
            else:
                cands = self._window_candidates(node, grids, prev_windows_by_z)
            for p in cands:
                pnode = prev.nodes[p]
                if pnode.kind != node.kind or pnode.type != node.type:
                    continue
                v = iou(node.b, pnode.b)
                if v >= GEOMETRY_MIN_IOU and self._compatible(n, p):
                    pairs.append((v, new.pos[n], prev.pos[p], n, p))
        if not pairs:
            return 0
        pairs.sort(key=lambda t: (-t[0], t[1], t[2]))
        by_n: dict[str, list[tuple[float, str]]] = {}
        by_p: dict[str, list[tuple[float, str]]] = {}
        for v, _, _, n, p in pairs:
            by_n.setdefault(n, []).append((v, p))
            by_p.setdefault(p, []).append((v, n))
        made = 0
        for v, _, _, n, p in pairs:
            if n in self.n2p or p in self.p2n:
                continue
            if any(q != p and q not in self.p2n and abs(w - v) < 1e-9 for w, q in by_n[n]):
                continue  # tie: never guess
            if any(m != n and m not in self.n2p and abs(w - v) < 1e-9 for w, m in by_p[p]):
                continue
            self._pair(n, p, "geometry")
            made += 1
        return made

    def _window_candidates(self, node: UNode, grids: Mapping, prev_windows_by_z: Mapping
                           ) -> list[str]:
        new = self.new
        wn = node.window
        if wn is None:
            pw = None
        elif wn in self.n2p:
            pw = self.n2p[wn]
        else:
            wnode = new.nodes.get(wn)
            pw = prev_windows_by_z.get(wnode.z if wnode is not None else None)
            if pw is None:
                return []
        cells = grids.get(pw, {}).get((node.kind, node.type))
        if not cells:
            return []
        b = node.b
        # IoU >= t bounds the corner offset by (1 - t) of the smaller side
        rx = math.ceil((1 - GEOMETRY_MIN_IOU) * b[2] / _GRID) + 1
        ry = math.ceil((1 - GEOMETRY_MIN_IOU) * b[3] / _GRID) + 1
        cx, cy = int(b[0]) // _GRID, int(b[1]) // _GRID
        out: list[str] = []
        for gx in range(cx - rx, cx + rx + 1):
            for gy in range(cy - ry, cy + ry + 1):
                out.extend(cells.get((gx, gy), ()))
        return out


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def _check_inputs(new: Index, prev: Index | None) -> None:
    if prev is None:
        return
    for nid, n in prev.nodes.items():
        if not is_ref(nid) or n.ref != nid:
            raise ValueError(f"prev must be a ref-space index (node {nid!r} has no ref)")
    nm, pm = new.meta, prev.meta
    if nm is not None and pm is not None and tuple(nm.lineage) != tuple(pm.lineage):
        raise OpError("bad_args",
                      f"captures {pm.id} and {nm.id} belong to different lineages "
                      f"({'/'.join(pm.lineage)} vs {'/'.join(nm.lineage)})",
                      "Refs carry only within one serial + package.")


def plan(new: Index, prev: Index | None, *, same_pid: bool, same_generation: bool,
         alloc: Alloc) -> Assignment:
    """Decide every node's ref without touching ``new`` (see the module docstring).

    ``new`` is a key-space index (``build_index`` output); ``prev`` is the latest
    published index of the same lineage (ref-space) or None. ``alloc(n)`` reserves
    ``n`` consecutive fresh ref numbers and returns the first one (the store's
    ``next_refs`` under its refs lock); it is called at most once, and not at all
    when every node carries over.
    """
    _check_inputs(new, prev)
    out = Assignment()
    new_id = new.meta.id if new.meta is not None and is_capture_id(new.meta.id) else None
    order = preorder(new)
    if prev is None:
        fresh = order
        matcher = None
    else:
        matcher = _Matcher(new, prev, same_pid=same_pid, same_generation=same_generation)
        matcher.run()
        prev_id = prev.meta.id if prev.meta is not None else None
        for nid in order:
            pid = matcher.n2p.get(nid)
            if pid is None:
                continue
            key = new.nodes[nid].key
            pnode = prev.nodes[pid]
            out.refmap[key] = pid
            out.match[key] = matcher.how[nid]
            out.since[key] = pnode.since or prev_id
        fresh = [nid for nid in order if nid not in matcher.n2p]
        out.ambiguous = sum(1 for nid in fresh if nid in matcher.amb_new)
    if fresh:
        start = int(alloc(len(fresh)))
        if start < 1:
            raise ValueError(f"alloc returned {start}; ref numbers start at 1")
        out.first_ref = start
        out.allocated = len(fresh)
        carried = set(out.refmap.values())
        for i, nid in enumerate(fresh):
            ref = ref_str(start + i)
            if ref in carried:
                raise RuntimeError(f"ref allocator returned {ref}, which is still in use")
            key = new.nodes[nid].key
            out.refmap[key] = ref
            out.match[key] = "new"
            out.since[key] = new_id
            if matcher is not None and nid in matcher.rebound:
                out.rebound[key] = matcher.rebound[nid]
    if prev is not None and matcher is not None:
        prev_id = prev.meta.id if prev.meta is not None else None
        for pid in matcher.prev.order:
            if pid not in matcher.p2n:
                out.tomb[pid] = tomb_entry(prev.nodes[pid], prev_id)
    return out


def assign(new: Index, prev: Index | None, *, same_pid: bool, same_generation: bool,
           alloc: Alloc) -> tuple[dict[str, str], dict]:
    """Carry refs from ``prev`` to ``new`` (spec 3.9; contract in section 10).

    Returns ``(refmap, tomb_updates)``: ``refmap`` maps every canonical key of
    ``new`` to its ref (pass it to ``apply_refs``); ``tomb_updates`` maps each old
    ref that found no node to ``[type, label<=40, sel, last_capture]`` (fold it into
    the lineage with ``merge_tomb``). Also records ``match``, ``since`` and
    ``rebound_of`` on ``new``'s nodes, which is the only change made to ``new``.
    """
    result = plan(new, prev, same_pid=same_pid, same_generation=same_generation, alloc=alloc)
    annotate(new, result)
    return dict(result.refmap), dict(result.tomb)


def annotate(new: Index, result: Assignment) -> None:
    """Write an Assignment's provenance (match, since, rebound_of) onto ``new``'s nodes."""
    for n in new.nodes.values():
        n.match = result.match.get(n.key)
        n.since = result.since.get(n.key)
        n.rebound_of = result.rebound.get(n.key)


def max_ref(refs: Iterable[str]) -> int:
    """The largest ref number among ``refs`` (0 when there is none)."""
    return max((ref_num(r) for r in refs if is_ref(r)), default=0)


__all__ = [
    "COLLECTION_CLASSES",
    "COLLECTION_TYPES",
    "GEOMETRY_MIN_IOU",
    "IDENTITY_KEY_KINDS",
    "TOMB_CAP",
    "TOMB_LABEL_MAX",
    "WEAK_KEY_KINDS",
    "Assignment",
    "LineageState",
    "annotate",
    "apply_to_lineage",
    "assign",
    "identity_flags",
    "iou",
    "max_ref",
    "merge_tomb",
    "plan",
    "preorder",
    "stale_ref_error",
    "tomb_entry",
    "touch_tomb",
]
