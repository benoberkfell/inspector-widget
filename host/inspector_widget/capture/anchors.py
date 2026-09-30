"""Anchors, template anchors and ``sel`` locators for capture index nodes (spec 3.4).

Pure Python over the model objects (no protobuf), so the carry-over matcher (C5),
the analyzers (C7) and the query engine (C6) can use the same definitions.

**Anchor**: a semantic path from the window, ``w<z>/`` followed by one segment per
level of the ``ui`` tree (slot nodes continue from their ComposeView's anchor down
the ``slots`` tree):

* View: ``Class#rid``, or ``Class:k`` (k = ordinal among siblings with the same
  segment base).
* Semantics and a11y-only nodes: ``@tag``, else ``Type"label"`` (label cut to 24
  chars), else ``Type:k`` / ``:k``. A duplicate among siblings gets ``:k``.
* Slot: ``Name@File.kt:line[key]:k`` (``[key]`` only when the group has a ``key``
  parameter, e.g. a lazy list item).
* Children of a collection (a11y CollectionInfo, a RecyclerView/ListView/GridView,
  a Compose lazy list) get ``[i]`` in place of ``:k``: the CollectionItemInfo
  position (the row of a list, the column of a horizontal list, ``row * columns +
  column`` in a grid; see :func:`item_positions`), else the sibling position.

Labels, tags and rids are escaped (``\\``, ``/``, ``"``, ``[`` and ``]`` get a
backslash) so ``/`` only ever separates segments and ``[i]`` is unambiguous. The
**template anchor** replaces every ``[i]`` with ``[*]``: the same element in each
cell of a list shares one template.

**sel**: the shortest locator that is unique among the ui nodes (view, compose and
a11y kinds) of the capture, tried in the order ``#rid``, ``@tag``, ``Type"label"``,
``"label"``, ``<parent sel> > Type"label"``, ``<parent sel> > Type``, and finally the
node's id (its key, or its ref once refs are applied). Every sel parses with the
selector grammar of spec 6.1; :func:`match_sel` is a reference evaluator of that
grammar over an Index (the query engine's resolver must agree with it).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .model import Index, UNode, is_key, is_ref

ANCHOR_LABEL_MAX = 24
SEL_LABEL_MAX = 40
SEL_MAX_ATOMS = 3
SEL_MAX_LEN = 120

#: View classes whose children are collection items.
COLLECTION_VIEW_CLASSES = frozenset({
    "RecyclerView", "ListView", "GridView", "ExpandableListView", "ViewPager",
    "HorizontalGridView", "VerticalGridView", "StackView", "AdapterViewFlipper",
})
#: Compose semantics that mark a lazy list / grid / pager.
COLLECTION_COMPOSE_KEYS = ("CollectionInfo", "IndexForKey", "ScrollToIndex")

_ESC = str.maketrans({"\\": "\\\\", "/": "\\/", '"': '\\"', "[": "\\[", "]": "\\]",
                      ":": "\\:"})
_ESC_SRC = str.maketrans({"\\": "\\\\", "/": "\\/", '"': '\\"', "[": "\\[", "]": "\\]"})
_INDEX_RE = re.compile(r"(?<!\\)\[(\d+)\]")
_ORDINAL_RE = re.compile(r"(?<!\\):(\d+)(?=/|$)")

RID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
TAG_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
TYPE_RE = re.compile(r"^[A-Z][A-Za-z0-9_]*$")


# --------------------------------------------------------------------------- #
# Segments
# --------------------------------------------------------------------------- #
def esc(s: str) -> str:
    """Escape a label, tag, rid or class for use inside an anchor segment."""
    return s.translate(_ESC)


def esc_src(s: str) -> str:
    """Escape a ``File.kt:line`` source (its colon is kept as is)."""
    return s.translate(_ESC_SRC)


def short_label(label: str | None, n: int = ANCHOR_LABEL_MAX) -> str | None:
    """Whitespace-collapsed label cut to ``n`` chars (None when empty)."""
    if not label:
        return None
    s = " ".join(str(label).split())
    return s[:n] if s else None


def template(anchor: str | None) -> str | None:
    """The template anchor: every collection index ``[i]`` becomes ``[*]``."""
    return _INDEX_RE.sub("[*]", anchor) if anchor else anchor


def without_ordinals(anchor: str | None) -> str | None:
    """The anchor with collection indexes and sibling ordinals removed (the
    carry-over collection guard compares these)."""
    if not anchor:
        return anchor
    return _ORDINAL_RE.sub("", _INDEX_RE.sub("", anchor))


def collection_index(anchor: str | None) -> int | None:
    """The last collection index ``[i]`` in an anchor, if any."""
    if not anchor:
        return None
    found = _INDEX_RE.findall(anchor)
    return int(found[-1]) if found else None


def is_collection(node: UNode) -> bool:
    """Whether ``node``'s ui children are collection items."""
    a11y = node.facets.get("a11y") or {}
    if a11y.get("collection"):
        return True
    view = node.facets.get("view") or {}
    if view.get("class") in COLLECTION_VIEW_CLASSES:
        return True
    comp = node.facets.get("compose") or {}
    attrs = comp.get("attrs") or {}
    if any(k in attrs for k in COLLECTION_COMPOSE_KEYS):
        return True
    return any(a in COLLECTION_COMPOSE_KEYS for a in comp.get("actions") or ())


def _is_pos(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def item_positions(children: Sequence[UNode], cols: int | None = None) -> list[int] | None:
    """Collection positions of ``children`` from their CollectionItemInfo, or None
    when they carry none.

    When every child reports both a row and a column and the (row, column) pairs
    are distinct, the position is ``row * width + column``, where ``width`` is the
    collection's column count (``cols``) or the widest column seen plus one. That
    is the row of a vertical list, the column of a horizontal one and the adapter
    position of a grid (whose rows repeat across columns and columns across rows).
    Otherwise the row (or the column) is used when every child has a distinct one."""
    rows, columns = [], []
    for c in children:
        item = (c.facets.get("a11y") or {}).get("item") or {}
        rows.append(item.get("row"))
        columns.append(item.get("col"))
    rows_ok = all(_is_pos(v) for v in rows)
    cols_ok = all(_is_pos(v) for v in columns)
    if rows_ok and cols_ok and children:
        pairs = list(zip(rows, columns))
        if len(set(pairs)) != len(pairs):
            return None
        width = max(int(cols) if _is_pos(cols) else 0, max(columns) + 1)
        return [r * width + c for r, c in pairs]
    for ok, axis in ((rows_ok, rows), (cols_ok, columns)):
        if ok and len(set(axis)) == len(axis):
            return list(axis)
    return None


def _item_indexes(children: Sequence[UNode], cols: int | None = None) -> list[int]:
    """Collection positions of ``children`` (:func:`item_positions`), else the
    sibling position."""
    pos = item_positions(children, cols)
    return pos if pos is not None else list(range(len(children)))


def collection_cols(node: UNode) -> int | None:
    """The column count a collection node reports (a11y CollectionInfo), if any."""
    col = (node.facets.get("a11y") or {}).get("collection") or {}
    v = col.get("cols") if isinstance(col, Mapping) else None
    return v if _is_pos(v) else None


def _base(node: UNode) -> tuple[str, str]:
    """``(form, base)`` of a ui node's segment before ordinals: form is ``id`` for
    ``#rid``/``@tag`` forms, ``label`` for a label form, ``plain`` otherwise."""
    if node.kind == "view":
        cls = (node.facets.get("view") or {}).get("class") or node.type or "View"
        return ("plain", esc(cls)) if not node.rid else ("id", f"{esc(cls)}#{esc(node.rid)}")
    if node.tag:
        return "id", f"@{esc(node.tag)}"
    typ = esc(node.type) if node.type else ""
    lab = short_label(node.label)
    if lab:
        return "label", f'{typ}"{esc(lab)}"'
    return "plain", typ


def ui_segments(children: Sequence[UNode], collection: bool,
                cols: int | None = None) -> list[str]:
    """Anchor segments of one sibling group (in ui order). ``cols`` is the
    collection's column count (a grid's positions use it)."""
    bases = [_base(c) for c in children]
    if collection:
        idx = _item_indexes(children, cols)
        return [f"{base}[{i}]" for (_, base), i in zip(bases, idx)]
    counts = Counter(b for _, b in bases)
    # a view whose rid is shared with a sibling falls back to Class:k
    fixed: list[tuple[str, str]] = []
    for c, (form, base) in zip(children, bases):
        if form == "id" and counts[base] > 1 and c.kind == "view":
            fixed.append(("plain", base.split("#", 1)[0]))
        else:
            fixed.append((form, base))
    counts = Counter(b for _, b in fixed)
    seen: Counter = Counter()
    out = []
    for form, base in fixed:
        k = seen[base]
        seen[base] += 1
        if form == "plain" or counts[base] > 1:
            out.append(f"{base}:{k}")
        else:
            out.append(base)
    return out


def slot_base(name: str | None, src: str | None, key: str | None = None) -> str:
    base = esc(name or "?")
    if src:
        base += "@" + esc_src(src)
    if key:
        base += f"[{esc(key)}]"
    return base


def slot_segments(bases: Sequence[str]) -> list[str]:
    """``base:k`` for one group of sibling slot bases (always with the ordinal)."""
    seen: Counter = Counter()
    out = []
    for base in bases:
        out.append(f"{base}:{seen[base]}")
        seen[base] += 1
    return out


def assign_ui_anchors(ix: Index) -> None:
    """Set ``anchor`` on every node of the ``ui`` tree of ``ix``."""
    ui = ix.tree("ui")
    nodes = ix.nodes
    roots = [nodes[r] for r in ui.roots if r in nodes]
    for pos, root in enumerate(roots):
        z = root.z if root.z is not None else pos
        root.anchor = f"w{z}/" + ui_segments([root], False)[0]
        stack = [root]
        while stack:
            parent = stack.pop()
            kids = [nodes[c] for c in ui.children.get(parent.id, ()) if c in nodes]
            if not kids:
                continue
            segs = ui_segments(kids, is_collection(parent), collection_cols(parent))
            for kid, seg in zip(kids, segs):
                kid.anchor = f"{parent.anchor}/{seg}"
            stack.extend(reversed(kids))


# --------------------------------------------------------------------------- #
# sel
# --------------------------------------------------------------------------- #
def quote(label: str) -> str:
    return '"' + label.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _usable_label(label: str | None) -> str | None:
    if not label or len(label) > SEL_LABEL_MAX or "\n" in label or "\r" in label:
        return None
    return label


def _usable_type(t: str | None) -> str | None:
    return t if t and TYPE_RE.match(t) else None


def _atoms(n: UNode) -> dict[str, str | None]:
    rid = n.rid if n.rid and RID_RE.match(n.rid) else None
    tag = n.tag if n.tag and TAG_RE.match(n.tag) else None
    typ = _usable_type(n.type)
    lab = _usable_label(n.label)
    return {
        "rid": f"#{rid}" if rid else None,
        "tag": f"@{tag}" if tag else None,
        "typelabel": f"{typ}{quote(lab)}" if typ and lab else None,
        "label": quote(lab) if lab else None,
        "type": typ,
    }


def _is_atom_path(sel: str | None) -> bool:
    return bool(sel) and not is_ref(sel) and not is_key(sel)


def assign_sels(ix: Index) -> None:
    """Set ``sel`` on every node: the first unique locator in the spec order, else
    the node's id. Slot nodes always get their id (selectors resolve ui nodes)."""
    ui_nodes = [n for _, n in _ui_preorder(ix)]
    atoms = {n.id: _atoms(n) for n in ui_nodes}
    counts: dict[str, Counter] = {k: Counter() for k in ("rid", "tag", "typelabel", "label")}
    for a in atoms.values():
        if a["rid"]:
            counts["rid"][a["rid"]] += 1
        if a["tag"]:
            counts["tag"][a["tag"]] += 1
        # "label" matches any node with that label, typed or not
        if a["typelabel"]:
            counts["typelabel"][a["typelabel"]] += 1
        if a["label"]:
            counts["label"][a["label"]] += 1
    ui = ix.tree("ui")
    parents = {c: p for p, kids in ui.children.items() for c in kids}
    sib_counts: dict[str, dict[str, Counter]] = {}
    for pid, kids in ui.children.items():
        per: dict[str, Counter] = {"typelabel": Counter(), "type": Counter()}
        for c in kids:
            a = atoms.get(c)
            if a is None:
                continue
            for kind in per:
                if a[kind]:
                    per[kind][a[kind]] += 1
        sib_counts[pid] = per
    for n in ui_nodes:
        a = atoms[n.id]
        n.sel = None
        for kind in ("rid", "tag", "typelabel", "label"):
            v = a[kind]
            if v and counts[kind][v] == 1:
                n.sel = v
                break
        if n.sel is None:
            p = ix.nodes.get(parents[n.id]) if n.id in parents else None
            if p is not None and _is_atom_path(p.sel) and p.sel.count(" > ") < SEL_MAX_ATOMS - 1:
                for kind in ("typelabel", "type"):
                    v = a[kind]
                    if v and sib_counts[p.id][kind][v] == 1:
                        cand = f"{p.sel} > {v}"
                        if len(cand) <= SEL_MAX_LEN:
                            n.sel = cand
                        break
        if n.sel is None:
            n.sel = n.id
    for n in ix.nodes.values():
        if n.kind == "slot":
            n.sel = n.id


def _ui_preorder(ix: Index) -> Iterable[tuple[str, UNode]]:
    ui = ix.tree("ui")
    stack = list(reversed(ui.roots))
    while stack:
        nid = stack.pop()
        node = ix.nodes.get(nid)
        if node is None:
            continue
        yield nid, node
        stack.extend(reversed(ui.children.get(nid, ())))


# --------------------------------------------------------------------------- #
# Reference selector evaluator (spec 6.1 path grammar)
# --------------------------------------------------------------------------- #
_ATOM_RE = re.compile(
    r'^(?:#(?P<rid>[A-Za-z_][A-Za-z0-9_.]*)'
    r'|@(?P<tag>[A-Za-z0-9_.:-]+)'
    r'|(?P<type>[A-Z][A-Za-z0-9_]*)?(?:"(?P<label>(?:[^"\\]|\\.)*)"(?P<ci>i)?)?)$'
)


def parse_atom(atom: str) -> dict[str, Any]:
    """One path atom to ``{rid|tag|type|label, ci}``; ValueError when invalid."""
    m = _ATOM_RE.match(atom)
    if not m or not any(m.group(g) is not None for g in ("rid", "tag", "type", "label")):
        raise ValueError(f"bad selector atom {atom!r}")
    out: dict[str, Any] = {k: m.group(k) for k in ("rid", "tag", "type")}
    lab = m.group("label")
    out["label"] = re.sub(r"\\(.)", r"\1", lab) if lab is not None else None
    out["ci"] = bool(m.group("ci"))
    return out


def _atom_matches(n: UNode, atom: Mapping[str, Any]) -> bool:
    if atom["rid"] is not None:
        return n.rid == atom["rid"]
    if atom["tag"] is not None:
        return n.tag == atom["tag"]
    if atom["type"] is not None and n.type != atom["type"]:
        return False
    if atom["label"] is not None:
        if n.label is None:
            return False
        if atom["ci"]:
            return n.label.lower() == atom["label"].lower()
        return n.label == atom["label"]
    return True


def match_sel(ix: Index, sel: str) -> list[str]:
    """Ids of the ui nodes a selector resolves to (refs, keys and aliases resolve
    directly; a path resolves its last atom, each atom a direct ui child of the
    previous). Point selectors are not handled here."""
    if is_ref(sel) or is_key(sel) or sel in ix.by_key or sel in ix.nodes:
        nid = ix.resolve_id(sel)
        return [nid] if nid is not None else []
    parts = sel.split(" > ")
    atoms = [parse_atom(p) for p in parts]
    ui = ix.tree("ui")
    current = [nid for nid, n in _ui_preorder(ix) if _atom_matches(n, atoms[0])]
    for atom in atoms[1:]:
        nxt = []
        for p in current:
            for c in ui.children.get(p, ()):
                n = ix.nodes.get(c)
                if n is not None and _atom_matches(n, atom):
                    nxt.append(c)
        current = nxt
    return current


__all__ = [
    "ANCHOR_LABEL_MAX",
    "COLLECTION_COMPOSE_KEYS",
    "COLLECTION_VIEW_CLASSES",
    "assign_sels",
    "assign_ui_anchors",
    "collection_cols",
    "collection_index",
    "esc",
    "is_collection",
    "item_positions",
    "match_sel",
    "parse_atom",
    "quote",
    "short_label",
    "slot_base",
    "slot_segments",
    "template",
    "ui_segments",
    "without_ordinals",
]
