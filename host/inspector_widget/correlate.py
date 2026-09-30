"""Integrated inspector: correlate the View tree, the Compose layer, and the
accessibility (AccessibilityNodeInfo) tree into one merged ``IntegratedNode`` tree, and
produce a full per-element "dossier" (all facets + a component image) for ``inspect_node``.

This implements integ.md PART 1 — INTEGRATED MODEL (§1.1 correlation keys, §1.2 unified
record, §1.3 ``inspect`` whole screen, §1.4 ``inspect_node`` one element).

Keys and joins follow the ID contract shared with the agent and ``a11y.py``:

* View nodes are ``view:<uniqueDrawingId>``. Their a11y facet is the a11y node with
  ``host_view_id == id`` and ``virtual_id == -1``.
* Every AndroidComposeView (including ones nested inside an AndroidView, at any depth)
  is its own Compose window, keyed by the ACV's uniqueDrawingId. Its synthetic root is
  ``composeview:<acvId>``; each semantics node is ``compose:<acvId>:<semanticsId>``.
  Semantics ids are only unique within one ComposeView, so a bare ``compose:<semId>``
  is accepted only when exactly one window has that id.
* A Compose node's a11y facet is the a11y node with ``host_view_id == acvId`` and
  ``virtual_id == semanticsId``; the semantics root maps to the ACV's own a11y node
  (Compose reports it as the provider host, ``virtual_id == -1``).
* Nodes without an exact join fall back to a one-to-one bounds (IoU >= 0.6) assignment
  restricted to the same window / ComposeView and to a11y nodes that no dumped View or
  Compose node claims, tie-broken by text, class/role and depth.

Compose also serves children it merges into a focusable parent (the Text inside a
clickable row, the Icon under an IconButton) and its synthetic role / contentDescription
nodes (semantics id + 1e9 / + 2e9) as a11y nodes the semantics dump does not have. They
are grafted under the node their a11y parent joined (``a11y_only``, ``a11y_parent``), so
every node key dump_accessibility / a11y_lint hands out resolves here.

Compose keys are valid only until recomposition re-mints the semantics ids. Every merged
tree carries a ``generation`` (a fingerprint of the Compose ids in the a11y dump, the same
value dump_accessibility and a11y_lint return for that UI state), and ``find_node``
re-resolves a stale ``compose:`` key by (ComposeView, test tag, label, list row, bounds)
from the fingerprints recorded for earlier generations of the same session (by inspect,
inspect_node, dump_accessibility and a11y_lint). The registry of a live Session is also
kept on disk per (serial, package, pid), so separate CLI invocations share it.

The merge walks the **View tree as the spine** (most stable ids + full nesting), grafts
each Compose window under its AndroidComposeView, re-parents AndroidView holders under the
Compose node that hosts them, and attaches a11y facets by key. List containers
(RecyclerView/ListView/... or any node with a11y CollectionInfo) mark their items with
``list_item`` so a node can be attributed as "row 3 -> ComposeView -> Button 'Delete'".
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import weakref
from collections import OrderedDict
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .a11y import HOST_VIEW_ID as _HOST_VIEW_ID
from .a11y import generation as _a11y_generation
from .a11y import parse_node_key

CONF_EXACT = "exact"
CONF_OVERLAP = "overlap"
CONF_NONE = "none"

# IoU threshold for the bounds-overlap fallback (integ.md §1.1 step 4).
_IOU_ACCEPT = 0.6

# View classes (simple names) whose children are list items.
_LIST_CLASSES = {
    "RecyclerView", "ListView", "GridView", "ExpandableListView", "ViewPager", "ViewPager2",
    "WearableRecyclerView", "HorizontalGridView", "VerticalGridView",
}
# Compose Role -> the android.widget class name the a11y delegate reports for it.
_ROLE_CLASS = {
    "Button": "Button", "Checkbox": "CheckBox", "Switch": "Switch",
    "RadioButton": "RadioButton", "Image": "ImageView", "DropdownList": "Spinner",
    "ValuePicker": "NumberPicker",
}


class NodeKeyError(LookupError):
    """A selector that cannot be resolved unambiguously (ambiguous bare ``compose:<id>``,
    a malformed key, or a stale Compose key that could not be re-resolved)."""

    def __init__(self, message: str, candidates: Optional[List[Dict[str, Any]]] = None):
        super().__init__(message)
        self.candidates = candidates or []


# --------------------------------------------------------------------------- geometry
def _rect(b: Optional[dict]) -> Optional[Dict[str, int]]:
    """Normalise a bounds dict to {x,y,w,h}. Accepts {layout:{...}} or a flat rect."""
    if not b:
        return None
    if "layout" in b and isinstance(b["layout"], dict):
        b = b["layout"]
    if not all(k in b for k in ("x", "y", "w", "h")):
        return None
    try:
        return {"x": int(b["x"]), "y": int(b["y"]), "w": int(b["w"]), "h": int(b["h"])}
    except (TypeError, ValueError):
        return None


def _iou(a: Optional[dict], b: Optional[dict]) -> float:
    if not a or not b:
        return 0.0
    ax2, ay2 = a["x"] + a["w"], a["y"] + a["h"]
    bx2, by2 = b["x"] + b["w"], b["y"] + b["h"]
    ix = max(0, min(ax2, bx2) - max(a["x"], b["x"]))
    iy = max(0, min(ay2, by2) - max(a["y"], b["y"]))
    inter = ix * iy
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union > 0 else 0.0


def _contains_point(outer: Optional[dict], px: int, py: int) -> bool:
    if not outer:
        return False
    return (outer["x"] <= px <= outer["x"] + outer["w"]
            and outer["y"] <= py <= outer["y"] + outer["h"])


def _contains_rect(outer: Optional[dict], inner: Optional[dict], slop: int = 1) -> bool:
    if not outer or not inner:
        return False
    return (outer["x"] - slop <= inner["x"] and outer["y"] - slop <= inner["y"]
            and inner["x"] + inner["w"] <= outer["x"] + outer["w"] + slop
            and inner["y"] + inner["h"] <= outer["y"] + outer["h"] + slop)


def _area(r: Optional[dict]) -> int:
    return (r["w"] * r["h"]) if r else 0


def _simple(cls: Optional[str]) -> str:
    return (cls or "").rsplit(".", 1)[-1].rsplit("$", 1)[-1]


def _norm(s: Any) -> str:
    return " ".join(str(s).split()).casefold() if s else ""


# --------------------------------------------------------------------------- keys
def view_key(view_id: int) -> str:
    return f"view:{int(view_id)}"


def compose_key(acv: int, semantics_id: int) -> str:
    return f"compose:{int(acv)}:{int(semantics_id)}"


def composeview_key(acv: int) -> str:
    return f"composeview:{int(acv)}"


def _a11y_host(n: dict) -> int:
    v = n.get("host_view_id", n.get("view_id"))
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _a11y_virtual(n: dict) -> int:
    v = n.get("virtual_id")
    try:
        return _HOST_VIEW_ID if v is None else int(v)
    except (TypeError, ValueError):
        return _HOST_VIEW_ID


def _a11y_tuple(n: dict) -> Tuple[Any, ...]:
    h, v = _a11y_host(n), _a11y_virtual(n)
    return ("view", h) if v == _HOST_VIEW_ID else ("virt", h, v)


def _flat(trees: List[dict]) -> List[dict]:
    out: List[dict] = []
    stack = list(reversed([t for t in (trees or []) if t]))
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(reversed(n.get("children", []) or []))
    return out


# --------------------------------------------------------------------------- a11y facet
# Boolean attributes worth surfacing on the a11y facet (proto field name -> output key).
_A11Y_BOOL_FIELDS = (
    "clickable", "long_clickable", "checkable", "checked", "focusable", "focused",
    "accessibility_focused", "selected", "enabled", "scrollable", "visible_to_user",
    "heading", "screen_reader_focusable", "editable", "password", "dismissable",
    "content_invalid", "multi_line",
)
_A11Y_TEXT_FIELDS = (
    "text", "content_description", "hint_text", "state_description", "error",
    "tooltip_text", "pane_title", "role_description",
)


def _a11y_facet(n: dict) -> dict:
    """Project a shaped a11y node dict into the compact facet stored on IntegratedNode."""
    facet: Dict[str, Any] = {}
    # Stable ids (+ the a11y node's own typed key, which several integrated nodes can
    # share: the ACV View, its composeview: root and its semantics root are one ANI).
    for src in ("host_view_id", "virtual_id", "node_key", "provider_class", "class_name"):
        if n.get(src) is not None:
            facet[src] = n[src]
    if "node_key" not in facet and n.get("host_view_id"):
        t = _a11y_tuple(n)
        facet["node_key"] = (view_key(t[1]) if t[0] == "view" else f"virtual:{t[1]}:{t[2]}")
    # Speakable composition (TalkBack-ish): contentDescription > text > stateDescription.
    facet["speakable"] = (
        n.get("content_description") or n.get("text") or n.get("state_description")
    )
    for f in _A11Y_TEXT_FIELDS:
        if n.get(f):
            facet[f] = n[f]
    role = n.get("role_description") or n.get("role")
    if role:
        facet["role"] = role
    # Flags: support both a precomputed list and individual booleans.
    flags = list(n.get("flags") or [])
    for f in _A11Y_BOOL_FIELDS:
        if n.get(f):
            label = f.upper()
            if label not in flags:
                flags.append(label)
        if n.get(f) is not None:
            facet[f] = bool(n[f])
    ifa = n.get("important_for_accessibility", n.get("important_for_a11y"))
    if ifa is not None:
        facet["important_for_accessibility"] = ifa
        # Normalise to a boolean "important" too (0 AUTO, 1 YES, 2 NO, 4 NO_HIDE_DESC).
        if isinstance(ifa, int):
            facet["important"] = ifa in (0, 1)
            if facet["important"] and "IMPORTANT" not in flags:
                flags.append("IMPORTANT")
        elif isinstance(ifa, str):
            facet["important"] = ifa.upper() in ("AUTO", "YES", "IMPORTANT")
    if flags:
        facet["flags"] = flags
    # Actions, traversal/labelling, collection/range, reading order.
    if n.get("actions"):
        facet["actions"] = n["actions"]
    for f in ("traversal_before", "traversal_after", "label_for", "labeled_by",
              "drawing_order", "order", "collection_info", "collection_item_info",
              "range_info"):
        if n.get(f) is not None:
            facet[f] = n[f]
    return facet


def _a11y_label(n: dict) -> str:
    return _norm(n.get("content_description") or n.get("text") or n.get("speakable"))


def _node_label(node: dict) -> str:
    """Best human label of an IntegratedNode (view text / compose Text / ContentDescription)."""
    comp = node.get("compose") or {}
    attrs = comp.get("attrs") or {}
    for k in ("ContentDescription", "Text", "EditableText"):
        v = attrs.get(k)
        if v and str(v).strip() not in ("null", "[]"):
            return str(v).strip().strip("[]")
    view = node.get("view") or {}
    if view.get("text"):
        return str(view["text"])
    a = node.get("a11y") or {}
    return a.get("speakable") or ""


def _node_class(node: dict) -> str:
    view = node.get("view") or {}
    if view.get("class_name"):
        return _simple(view["class_name"])
    attrs = (node.get("compose") or {}).get("attrs") or {}
    role = attrs.get("Role")
    return _ROLE_CLASS.get(str(role), "") if role else ""


# --------------------------------------------------------------------------- merge core
class _Merger:
    def __init__(self, view_tree: List[dict], compose_windows: List[dict],
                 a11y_tree: List[dict], props: Optional[Dict[int, list]] = None):
        self.props = props or {}
        self.view_tree = view_tree or []
        # ---- compose windows: acv -> root (every ACV, nested ones included) ----
        self.compose: Dict[int, dict] = {}
        for w in compose_windows or []:
            try:
                acv = int(w.get("view_id") or 0)
            except (TypeError, ValueError):
                continue
            if acv and w.get("root") and acv not in self.compose:
                self.compose[acv] = w["root"]
        self.compose_ids: Dict[int, set] = {
            acv: {int(n.get("id", 0)) for n in _flat([root])} for acv, root in self.compose.items()
        }
        # ---- view ids present in the dump (to find orphan a11y nodes) ----
        self.view_ids = {int(v.get("id", 0)) for v in _flat(self.view_tree)}
        view_roots = [int(v.get("id", 0)) for v in self.view_tree]

        # ---- a11y index ----
        self.a11y_real: Dict[int, dict] = {}
        self.a11y_virt: Dict[Tuple[int, int], dict] = {}
        self.a11y_window: Dict[int, int] = {}
        self.a11y_depth: Dict[int, int] = {}
        self.a11y_parent: Dict[int, dict] = {}
        self.a11y_all: List[dict] = []
        # id(a11y node) -> the IntegratedNode that took it as its a11y facet.
        self.owner: Dict[int, dict] = {}
        roots = [r for r in (a11y_tree or []) if r]
        for r in roots:
            win = self._align_window(r, view_roots, len(roots))
            stack: List[Tuple[dict, int]] = [(r, 0)]
            while stack:
                n, depth = stack.pop()
                self.a11y_all.append(n)
                self.a11y_window[id(n)] = win
                self.a11y_depth[id(n)] = depth
                h, v = _a11y_host(n), _a11y_virtual(n)
                if v == _HOST_VIEW_ID:
                    self.a11y_real.setdefault(h, n)
                else:
                    self.a11y_virt.setdefault((h, v), n)
                for c in reversed(n.get("children", []) or []):
                    self.a11y_parent[id(c)] = n
                    stack.append((c, depth + 1))
        self.claimed: set = set()
        # (node, group, depth) awaiting the IoU fallback
        self.pending: List[Tuple[dict, Tuple[Any, ...], int]] = []

    def _align_window(self, a11y_root: dict, view_roots: List[int], n_a11y_windows: int) -> int:
        """Window id of an a11y root = the view root it belongs to."""
        h = _a11y_host(a11y_root)
        if h in view_roots:
            return h
        if len(view_roots) == 1 and n_a11y_windows == 1:
            return view_roots[0]
        rb = _rect(a11y_root.get("bounds"))
        best, best_iou = h, 0.0
        for v in self.view_tree:
            s = _iou(rb, _rect(v.get("bounds")))
            if s > best_iou:
                best, best_iou = int(v.get("id", 0)), s
        return best

    # ---- a11y attachment ----
    def _attach(self, node: dict, exact: Optional[dict], group: Tuple[Any, ...],
                depth: int) -> None:
        if exact is not None:
            node["a11y"] = _a11y_facet(exact)
            node["correlation_confidence"] = CONF_EXACT
            self.claimed.add(id(exact))
            self.owner.setdefault(id(exact), node)
            return
        node["correlation_confidence"] = CONF_NONE
        if node.get("bounds"):
            self.pending.append((node, group, depth))

    def _candidates(self, group: Tuple[Any, ...]) -> List[dict]:
        """Unclaimed ORPHAN a11y nodes of a window / ComposeView (no dumped node owns their key)."""
        out = []
        for n in self.a11y_all:
            if id(n) in self.claimed or not _rect(n.get("bounds")):
                continue
            h, v = _a11y_host(n), _a11y_virtual(n)
            if group[0] == "acv":
                if h == group[1] and v != _HOST_VIEW_ID and v not in self.compose_ids.get(h, ()):
                    out.append(n)
            elif group[0] == "win":
                if (v == _HOST_VIEW_ID and self.a11y_window.get(id(n)) == group[1]
                        and h not in self.view_ids):
                    out.append(n)
        return out

    def _fallback(self) -> None:
        """One-to-one IoU assignment per window / ComposeView (CO3)."""
        groups: Dict[Tuple[Any, ...], List[Tuple[dict, int]]] = {}
        for node, group, depth in self.pending:
            groups.setdefault(group, []).append((node, depth))
        for group, members in groups.items():
            cands = self._candidates(group)
            if not cands:
                continue
            base_depth = min(self.a11y_depth.get(id(c), 0) for c in cands)
            node_base = min(d for _, d in members)
            pairs = []
            for ni, (node, depth) in enumerate(members):
                nb = node.get("bounds")
                label = _norm(_node_label(node))
                cls = _node_class(node)
                for ci, cand in enumerate(cands):
                    score = _iou(nb, _rect(cand.get("bounds")))
                    if score < _IOU_ACCEPT:
                        continue
                    text_match = 1 if label and label == _a11y_label(cand) else 0
                    class_match = 1 if cls and cls == _simple(cand.get("class_name")) else 0
                    depth_gap = abs((depth - node_base)
                                    - (self.a11y_depth.get(id(cand), 0) - base_depth))
                    pairs.append(((round(score, 2), text_match, class_match, -depth_gap,
                                   -ni, -ci), ni, ci, score))
            pairs.sort(key=lambda p: p[0], reverse=True)
            used_n: set = set()
            used_c: set = set()
            for _, ni, ci, score in pairs:
                if ni in used_n or ci in used_c:
                    continue
                used_n.add(ni)
                used_c.add(ci)
                node = members[ni][0]
                node["a11y"] = _a11y_facet(cands[ci])
                node["correlation_confidence"] = CONF_OVERLAP
                node["a11y_iou"] = round(score, 3)
                self.claimed.add(id(cands[ci]))
                self.owner.setdefault(id(cands[ci]), node)

    # ---- view spine ----
    def merge_view(self, v: dict, window: int, depth: int = 0,
                   list_parent: Optional[dict] = None, index: int = 0) -> dict:
        vid = int(v.get("id", 0))
        bounds = _rect(v.get("bounds"))
        node: Dict[str, Any] = {
            "node_key": view_key(vid),
            "bounds": bounds,
            "view": {k: v.get(k) for k in ("id", "class_name", "qualified_name",
                                           "resource", "view_id_name", "text")
                     if v.get(k) is not None},
            "children": [],
        }
        if (v.get("bounds") or {}).get("render"):
            node["render_quad"] = v["bounds"]["render"]
        if vid in self.props:
            node["view"]["properties"] = self.props[vid]
        # image-ref by uniqueDrawingId (SKP draw-node) — populated lazily by callers.
        node["image_ref"] = {"source": "view", "view_id": vid}
        self._attach(node, self.a11y_real.get(vid), ("win", window), depth)
        self._mark_list_item(node, list_parent, index)
        is_list = (_simple(v.get("class_name")) in _LIST_CLASSES
                   or bool((node.get("a11y") or {}).get("collection_info")))
        croot = None
        if vid in self.compose:
            croot = self.merge_compose_window(vid, self.compose[vid], depth + 1)
            node["children"].append(croot)
        elif vid in self.a11y_real:
            self._graft_virtual(node, self.a11y_real[vid], vid)
        for i, c in enumerate(v.get("children", []) or []):
            child = self.merge_view(c, window, depth + 1, node if is_list else None, i)
            node["children"].append(child)
            if croot is not None and _simple(c.get("class_name")) == "AndroidViewsHandler":
                self._rehome_interop(child, croot)
        return node

    def _mark_list_item(self, node: dict, list_parent: Optional[dict], index: int) -> None:
        cii = (node.get("a11y") or {}).get("collection_item_info")
        if list_parent is None and not cii:
            return
        item: Dict[str, Any] = {}
        if list_parent is not None:
            item["list"] = list_parent["node_key"]
            item["index"] = index  # position among the list's current child views
        if cii:
            item["row"] = cii.get("row_index")
            if cii.get("column_index"):
                item["column"] = cii.get("column_index")
        node["list_item"] = item

    def _rehome_interop(self, handler: dict, croot: dict) -> None:
        """Move AndroidView holders from AndroidViewsHandler under the Compose node hosting them."""
        compose_nodes = [n for n in _flat([croot]) if n.get("bounds")]
        kept = []
        for holder in handler.get("children", []):
            hb = holder.get("bounds")
            host = None
            exact = [n for n in compose_nodes if n.get("bounds") == hb]
            if exact:
                host = exact[-1]  # deepest in pre-order
            else:
                best = None
                for n in compose_nodes:
                    if _contains_rect(n.get("bounds"), hb):
                        if best is None or _area(n["bounds"]) <= _area(best["bounds"]):
                            best = n
                host = best if best is not None and best is not croot else None
            if host is None:
                kept.append(holder)
                continue
            holder["interop"] = {"hosted_by": host["node_key"], "view_parent": handler["node_key"]}
            host["children"].append(holder)
        handler["children"] = kept

    def _graft_virtual(self, host_node: dict, a11y_host: dict, host_id: int) -> None:
        """a11y-only nodes for virtual children of a non-Compose provider (WebView, chips, ...)
        or of an ACV whose Compose dump is missing, so they are not lost from the tree."""
        def convert(a: dict) -> dict:
            h, v = _a11y_host(a), _a11y_virtual(a)
            key = a.get("node_key") or f"virtual:{h}:{v}"
            n = {"node_key": key, "bounds": _rect(a.get("bounds")), "a11y": _a11y_facet(a),
                 "correlation_confidence": CONF_EXACT, "a11y_only": True, "children": []}
            self.claimed.add(id(a))
            self.owner.setdefault(id(a), n)
            for c in a.get("children", []) or []:
                if _a11y_host(c) == host_id and _a11y_virtual(c) != _HOST_VIEW_ID:
                    n["children"].append(convert(c))
            self._mark_list_item(n, None, 0)
            return n

        for c in a11y_host.get("children", []) or []:
            if _a11y_host(c) == host_id and _a11y_virtual(c) != _HOST_VIEW_ID:
                host_node["children"].append(convert(c))

    # ---- compose window ----
    def merge_compose_window(self, acv: int, root: dict, depth: int) -> dict:
        synthetic = int(root.get("id", -1)) == acv or root.get("name") == "AndroidComposeView"

        def rec(c: dict, d: int, is_sem_root: bool) -> dict:
            cid = int(c.get("id", 0))
            is_syn = c is root and synthetic
            node: Dict[str, Any] = {
                "node_key": composeview_key(acv) if is_syn else compose_key(acv, cid),
                "bounds": _rect(c.get("bounds")),
                "compose": {
                    "id": cid,
                    "acv": acv,
                    "name": c.get("name"),
                    "source": c.get("source"),
                    "attrs": c.get("attrs", {}) or {},
                    "render_node_id": c.get("render_node_id"),
                    "kind": c.get("kind"),
                },
                "children": [],
            }
            if (c.get("bounds") or {}).get("render"):
                node["render_quad"] = c["bounds"]["render"]
            rnid = c.get("render_node_id")
            if rnid:
                node["image_ref"] = {"source": "skp", "layer_id": int(rnid)}
            if is_syn:
                exact = self.a11y_real.get(acv)
            else:
                exact = self.a11y_virt.get((acv, cid))
                if exact is None and is_sem_root:
                    # Compose reports its root semantics node as the host View itself.
                    exact = self.a11y_real.get(acv)
            self._attach(node, exact, ("acv", acv), d)
            if node.get("a11y", {}).get("collection_item_info"):
                self._mark_list_item(node, None, 0)
            for ch in c.get("children", []) or []:
                node["children"].append(rec(ch, d + 1, is_syn))
            return node

        return rec(root, depth, not synthetic)

    def _graft_compose_a11y_only(self) -> None:
        """Graft the a11y nodes of a ComposeView that no Compose semantics node joined: the
        children Compose merges into a focusable parent (a clickable row's Text, an
        IconButton's Icon) and its synthetic role / contentDescription nodes. Each goes
        under the IntegratedNode its nearest a11y ancestor joined, so its key resolves
        (a11y facet, bounds, crop) instead of looking stale."""
        for a in self.a11y_all:  # pre-order: an a11y parent is placed before its children
            if id(a) in self.claimed:
                continue
            h, v = _a11y_host(a), _a11y_virtual(a)
            if v == _HOST_VIEW_ID or h not in self.compose:
                continue
            p = self.a11y_parent.get(id(a))
            while p is not None and id(p) not in self.owner:
                p = self.a11y_parent.get(id(p))
            if p is None:
                continue
            host = self.owner[id(p)]
            n: Dict[str, Any] = {
                "node_key": a.get("node_key") or compose_key(h, v),
                "bounds": _rect(a.get("bounds")), "a11y": _a11y_facet(a),
                "correlation_confidence": CONF_EXACT, "a11y_only": True,
                "a11y_parent": host.get("node_key"), "children": []}
            if v >= _FAKE_CD_OFFSET:
                n["compose_synthetic"] = "content_description"
            elif v >= _FAKE_ROLE_OFFSET:
                n["compose_synthetic"] = "role"
            host.setdefault("children", []).append(n)
            self.claimed.add(id(a))
            self.owner[id(a)] = n

    def build(self) -> List[dict]:
        roots = [self.merge_view(r, int(r.get("id", 0))) for r in self.view_tree]
        self._fallback()
        self._graft_compose_a11y_only()
        _link_list_items(roots)
        return roots

    def unmatched_a11y(self) -> int:
        return sum(1 for n in self.a11y_all if id(n) not in self.claimed)


# Slot-table (composable) node ids are <= -2 (CONTRACT §9); -1 is "no id".
_SLOT_ID_MAX = -2

_FAKE_ROLE_OFFSET = 1_000_000_000
_FAKE_CD_OFFSET = 2_000_000_000


def _link_list_items(roots: List[dict]) -> None:
    """Give list items that only know their row (Compose lazy items) their list container."""
    def walk(n: dict, lists: List[str]) -> None:
        a = n.get("a11y") or {}
        li = n.get("list_item")
        if li is not None and "list" not in li and lists:
            li["list"] = lists[-1]
        here = lists + [n["node_key"]] if (a.get("collection_info") or _simple(
            (n.get("view") or {}).get("class_name")) in _LIST_CLASSES) else lists
        for c in n.get("children", []) or []:
            walk(c, here)

    for r in roots:
        walk(r, [])


def _generation(compose_windows: List[dict], view_tree: List[dict]) -> str:
    """Fingerprint of the ids in a dump. Changes when recomposition re-mints ids."""
    h = hashlib.sha1()
    for w in sorted(compose_windows or [], key=lambda w: int(w.get("view_id") or 0)):
        ids = sorted(int(n.get("id", 0)) for n in _flat([w.get("root")]) if n)
        h.update(f"{w.get('view_id')}:{ids};".encode())
    if not compose_windows:
        h.update(str(sorted(int(v.get("id", 0)) for v in _flat(view_tree))).encode())
    return "g" + h.hexdigest()[:10]


class MergedTree(dict):
    """The merged-tree dict (JSON-serialisable as-is) plus the session's key registry,
    so :func:`find_node` can re-resolve stale Compose keys."""

    registry: Optional["KeyRegistry"] = None


def build_integrated_tree(view_tree: List[dict], compose: List[dict], a11y: List[dict],
                          props: Optional[Dict[int, list]] = None) -> Dict[str, Any]:
    """Merge the three trees into one IntegratedNode tree rooted at each window.

    ``view_tree``  : list of ViewNode dicts (``strings.dump_tree_to_dict``["roots"]).
    ``compose``    : list of compose-window dicts ({view_id, root}) from dump_compose_to_dict.
    ``a11y``       : list of a11y window-root dicts (the shaped a11y tree roots).
    ``props``      : optional {view_id: [property dicts]} to inline under ``view.properties``.
    """
    merger = _Merger(view_tree, compose, a11y, props)
    roots = merger.build()
    # The a11y dump's Compose ids (a11y.generation): the value dump_accessibility and
    # a11y_lint report for the same UI state. Without an a11y dump, the Compose dump's.
    generation = (_a11y_generation([r for r in a11y if r]) if a11y
                  else _generation(compose, view_tree))
    summary = summarize(roots)
    summary["compose_views"] = len(merger.compose)
    summary["a11y_nodes"] = len(merger.a11y_all)
    summary["a11y_unmatched"] = merger.unmatched_a11y()
    summary["generation"] = generation
    out = MergedTree(roots=roots, summary=summary, generation=generation)
    return out


# --------------------------------------------------------------------------- summary
def summarize(roots: List[dict]) -> Dict[str, Any]:
    counts = {"nodes": 0, "view": 0, "compose": 0, "a11y": 0,
              "exact": 0, "overlap": 0, "none": 0, "with_image_ref": 0,
              "a11y_only": 0, "list_items": 0}
    for n in _flat(roots):
        counts["nodes"] += 1
        if n.get("view"):
            counts["view"] += 1
        if n.get("compose"):
            counts["compose"] += 1
        if n.get("a11y"):
            counts["a11y"] += 1
        conf = n.get("correlation_confidence")
        if conf in counts:
            counts[conf] += 1
        if n.get("image_ref"):
            counts["with_image_ref"] += 1
        if n.get("a11y_only"):
            counts["a11y_only"] += 1
        if n.get("list_item"):
            counts["list_items"] += 1
    return counts


# --------------------------------------------------------------------------- key registry (ID4)
_MAX_GENERATIONS = 8


def _fingerprint(node: dict, row: Optional[int]) -> Dict[str, Any]:
    comp = node.get("compose") or {}
    attrs = comp.get("attrs") or {}
    return {
        "acv": comp.get("acv"),
        "label": _norm(_node_label(node)),
        "test_tag": attrs.get("TestTag"),
        "role": attrs.get("Role"),
        "row": row,
        "bounds": node.get("bounds"),
        "src": "merged",
    }


# a11y class name (simple) / roleDescription -> the Compose Role name the merged tree uses.
_ROLE_OF_CLASS = {v: k for k, v in _ROLE_CLASS.items()}
_ROLE_OF_DESC = {"tab": "Tab", "switch": "Switch"}


def _a11y_label(a: dict) -> str:
    """contentDescription / text of an a11y node, else its non-focusable descendants' (the
    way a merged Compose node carries its children's text)."""
    own = a.get("content_description") or a.get("text")
    if own:
        return _norm(own)
    parts: List[str] = []
    stack = list(reversed(a.get("children") or []))
    while stack:
        c = stack.pop()
        fl = set(c.get("flags") or [])
        if {"clickable", "focusable", "screen_reader_focusable"} & fl:
            continue
        t = c.get("content_description") or c.get("text")
        if t:
            parts.append(str(t))
            continue
        stack.extend(reversed(c.get("children") or []))
    return _norm(", ".join(parts))


def a11y_fingerprints(a11y_roots: List[dict],
                      compose_windows: Optional[List[dict]] = None) -> Dict[str, Dict[str, Any]]:
    """Fingerprints of the Compose nodes of an a11y dump, keyed by their node_key (see
    :func:`record_a11y`). ``compose_windows`` adds each node's TestTag and Role."""
    attrs_of: Dict[Tuple[int, int], dict] = {}
    for w in compose_windows or []:
        acv = int(w.get("view_id") or 0)
        for n in _flat([w.get("root")]):
            if n and n.get("id") is not None:
                attrs_of[(acv, int(n["id"]))] = n.get("attrs") or {}
    out: Dict[str, Dict[str, Any]] = {}
    stack: List[Tuple[dict, Optional[int]]] = [(r, None) for r in reversed(a11y_roots or []) if r]
    while stack:
        a, row = stack.pop()
        cii = a.get("collection_item_info") or {}
        if cii.get("row_index") is not None:
            row = cii.get("row_index")
        key = a.get("node_key") or ""
        if key.startswith("compose:"):
            h, v = _a11y_host(a), _a11y_virtual(a)
            attrs = attrs_of.get((h, v), {})
            res = a.get("view_id_resource_name") or ""
            role = (attrs.get("Role") or _ROLE_OF_DESC.get(str(a.get("role_description") or "").lower())
                    or _ROLE_OF_CLASS.get(_simple(a.get("class_name"))))
            out[key] = {
                "acv": h, "label": _a11y_label(a),
                "test_tag": attrs.get("TestTag") or (res if res and ":id/" not in res else None),
                "role": role, "row": row, "bounds": _rect(a.get("bounds")), "src": "a11y",
            }
        for c in reversed(a.get("children") or []):
            stack.append((c, row))
    return out


def _compose_nodes_with_rows(roots: List[dict]) -> Iterable[Tuple[dict, Optional[int]]]:
    """Yield (compose node, row of the nearest enclosing list item) in pre-order."""
    stack: List[Tuple[dict, Optional[int]]] = [(r, None) for r in reversed(roots)]
    while stack:
        n, row = stack.pop()
        li = n.get("list_item") or {}
        if li:
            row = li.get("row", li.get("index", row))
        if n.get("compose") and n.get("node_key", "").startswith("compose:"):
            yield n, row
        for c in reversed(n.get("children", []) or []):
            stack.append((c, row))


class KeyRegistry:
    """Fingerprints of the Compose keys minted by recent dumps of one session.

    With a ``path`` the registry is loaded from and saved to that JSON file, so separate
    processes (CLI invocations) on the same app process share it; ``pid`` guards against
    reusing the fingerprints of a process that has since died.
    """

    def __init__(self, path: Optional[str] = None, pid: Optional[int] = None) -> None:
        self.generations: "OrderedDict[str, Dict[str, Dict[str, Any]]]" = OrderedDict()
        self.path = path
        self.pid = pid
        if path:
            self._load()

    def record(self, merged: Dict[str, Any]) -> None:
        fps = {n["node_key"]: _fingerprint(n, row)
               for n, row in _compose_nodes_with_rows(merged.get("roots", []))}
        self.record_fingerprints(merged.get("generation") or "", fps)

    def record_fingerprints(self, gen: str, fps: Dict[str, Dict[str, Any]]) -> None:
        """Merge ``fps`` into generation ``gen`` (the same ids: an a11y dump and an
        inspect of one UI state add to each other, keeping the fields each one knows)."""
        cur = self.generations.pop(gen, {})
        for k, fp in fps.items():
            old = cur.get(k) or {}
            cur[k] = {f: (fp.get(f) if fp.get(f) not in (None, "") else old.get(f))
                      for f in set(old) | set(fp)}
        self.generations[gen] = cur
        while len(self.generations) > _MAX_GENERATIONS:
            self.generations.popitem(last=False)
        self._save()

    def _load(self) -> None:
        try:
            with open(self.path) as f:  # type: ignore[arg-type]
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or (self.pid is not None and data.get("pid") != self.pid):
            return
        for gen, fps in data.get("generations") or []:
            if isinstance(fps, dict):
                self.generations[str(gen)] = fps

    def _save(self) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"pid": self.pid, "generations": list(self.generations.items())}, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def lookup(self, key: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Newest recorded (generation, fingerprint) that knew ``key``."""
        for gen in reversed(self.generations):
            fp = self.generations[gen].get(key)
            if fp is not None:
                return gen, fp
        return None


_REGISTRIES: "weakref.WeakKeyDictionary[Any, KeyRegistry]" = weakref.WeakKeyDictionary()
_REGISTRIES_BY_ID: Dict[int, KeyRegistry] = {}


def _registry_path(serial: Any, package: Any, pid: Any) -> Optional[str]:
    """Where the key registry of (serial, package) lives on disk, or None when the app
    process is not known (fakes, raw objects). ``$INSPECTOR_WIDGET_KEY_CACHE`` names the
    directory; set it to "0" to keep registries in memory only."""
    if not (isinstance(serial, str) and isinstance(package, str) and isinstance(pid, int)):
        return None
    root = os.environ.get("INSPECTOR_WIDGET_KEY_CACHE")
    if root == "0":
        return None
    root = root or os.path.join(tempfile.gettempdir(), "inspector-widget-keys")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{serial}_{package}")
    return os.path.join(root, safe + ".json")


def registry_for(session: Any, serial: Optional[str] = None, package: Optional[str] = None,
                 pid: Optional[int] = None) -> KeyRegistry:
    """The key registry of a session (kept outside the Session object). A Session that
    knows its serial, package and pid (or a raw connection given them) gets one backed by
    a file, shared by every process that inspects the same app process."""
    serial = serial if serial is not None else getattr(session, "serial", None)
    package = package if package is not None else getattr(session, "package", None)
    pid = pid if pid is not None else getattr(session, "pid", None)
    try:
        reg = _REGISTRIES.get(session)
        if reg is None:
            reg = KeyRegistry(_registry_path(serial, package, pid), pid)
            _REGISTRIES[session] = reg
        return reg
    except TypeError:  # not weak-referenceable
        reg = _REGISTRIES_BY_ID.get(id(session))
        if reg is None:
            if len(_REGISTRIES_BY_ID) > 32:
                _REGISTRIES_BY_ID.clear()
            reg = _REGISTRIES_BY_ID[id(session)] = KeyRegistry(
                _registry_path(serial, package, pid), pid)
        return reg


def record_a11y(session: Any, a11y_data: Any, compose_windows: Optional[List[dict]] = None,
                serial: Optional[str] = None, package: Optional[str] = None,
                pid: Optional[int] = None) -> Optional[str]:
    """Record the Compose keys of an a11y dump (dump_accessibility / a11y_lint) in the
    session's registry, so inspect_node can re-resolve them after recomposition and flag
    a recycled cell. Returns the dump's generation."""
    roots = _a11y_roots(a11y_data)
    if not roots:
        return None
    gen = (a11y_data.get("generation") if isinstance(a11y_data, dict) else None) \
        or _a11y_generation(roots)
    registry_for(session, serial, package, pid).record_fingerprints(
        gen, a11y_fingerprints(roots, compose_windows))
    return gen


def _score_candidate(fp: Dict[str, Any], node: dict, row: Optional[int]) -> Tuple[int, List[str]]:
    """Similarity of a current Compose node to an old fingerprint; -1 = incompatible."""
    cur = _fingerprint(node, row)
    score, why = 0, []
    if fp.get("test_tag") and cur.get("test_tag"):
        if fp["test_tag"] != cur["test_tag"]:
            return -1, []
        score += 4
        why.append("test_tag")
    if fp.get("label") and cur.get("label"):
        if fp["label"] == cur["label"]:
            score += 3
            why.append("label")
        else:
            score -= 2
    if fp.get("acv") is not None and fp.get("acv") == cur.get("acv"):
        score += 2
        why.append("compose_view")
    if fp.get("row") is not None and cur.get("row") is not None:
        if fp["row"] == cur["row"]:
            score += 2
            why.append("row")
        else:
            score -= 3
    if fp.get("role") and fp.get("role") == cur.get("role"):
        score += 1
        why.append("role")
    iou = _iou(_rect(fp.get("bounds")), node.get("bounds"))
    if iou >= 0.9:
        score += 2
        why.append("bounds")
    elif iou >= 0.5:
        score += 1
        why.append("bounds~")
    return score, why


def _reresolve(merged: Dict[str, Any], key: str, fp: Dict[str, Any],
               old_gen: str) -> Tuple[Optional[dict], List[Dict[str, Any]]]:
    """Find the current node for a stale Compose key; (node or None, ranked candidates)."""
    ranked = []
    for node, row in _compose_nodes_with_rows(merged.get("roots", [])):
        s, why = _score_candidate(fp, node, row)
        if s > 0:
            ranked.append((s, why, node))
    ranked.sort(key=lambda t: t[0], reverse=True)
    cands = [{"node_key": n["node_key"], "label": _node_label(n), "score": s, "matched_on": w}
             for s, w, n in ranked[:5]]
    if not ranked:
        return None, cands
    best_s, best_why, best = ranked[0]
    unique = len(ranked) == 1 or ranked[1][0] < best_s
    strong = best_s >= 5 and ({"test_tag", "label"} & set(best_why))
    if unique and strong:
        best["resolved_from"] = {"stale_key": key, "generation": old_gen,
                                 "matched_on": best_why}
        return best, cands
    return None, cands


# --------------------------------------------------------------------------- hit-test / lookup
def _nodes_by_key(merged: Dict[str, Any]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for n in _flat(merged.get("roots", [])):
        out.setdefault(n.get("node_key"), n)
    return out


def _bare_candidates(merged: Dict[str, Any], sem_id: int) -> List[dict]:
    return [n for n in _flat(merged.get("roots", []))
            if n.get("compose") and (n.get("node_key") or "").startswith("compose:")
            and int((n.get("compose") or {}).get("id", -1)) == sem_id]


def resolve_key(merged: Dict[str, Any], key: str) -> Optional[dict]:
    """Resolve a typed key against a merged tree (no stale-key re-resolution).

    Raises :class:`NodeKeyError` for a malformed key or an ambiguous bare
    ``compose:<semanticsId>``.
    """
    try:
        t = parse_node_key(key)
    except ValueError as exc:
        raise NodeKeyError(str(exc)) from None
    idx = _nodes_by_key(merged)
    if t[0] == "view":
        return idx.get(view_key(t[1]))
    if t[0] == "composeview":
        return idx.get(composeview_key(t[1])) or idx.get(view_key(t[1]))
    if t[0] == "virt":
        return (idx.get(compose_key(t[1], t[2])) or idx.get(f"virtual:{t[1]}:{t[2]}"))
    # bare compose:<semId>
    cands = _bare_candidates(merged, t[1])
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1:
        rows = {id(n): r for n, r in _compose_nodes_with_rows(merged.get("roots", []))}
        listed = [{"node_key": n["node_key"], "label": _node_label(n), "row": rows.get(id(n))}
                  for n in cands]
        raise NodeKeyError(
            f"compose:{t[1]} is ambiguous: semantics id {t[1]} exists in {len(cands)} "
            "ComposeViews; use one of: " + "; ".join(
                _describe(n) + (f" (row {rows[id(n)]})" if rows.get(id(n)) is not None else "")
                for n in cands), listed)
    return None


def find_node(merged: Dict[str, Any], *, node_key: Optional[str] = None,
              view_id: Optional[int] = None, semantics_id: Optional[int] = None,
              bounds: Optional[dict] = None,
              registry: Optional[KeyRegistry] = None) -> Optional[dict]:
    """Resolve one IntegratedNode by key/id, or hit-test the deepest node covering ``bounds``.

    ``node_key``: ``view:<id>``, ``compose:<acvId>:<semanticsId>``, ``composeview:<acvId>``,
    ``virtual:<hostId>:<virtualId>``, or a bare ``compose:<semanticsId>`` (only when a single
    ComposeView has that id; otherwise :class:`NodeKeyError` lists the candidates).
    ``semantics_id`` is the bare form; ``view_id`` is ``view:<id>``.

    A Compose key missing from this dump is stale (recomposition re-minted the ids): it is
    re-resolved from the fingerprint an earlier generation recorded in ``registry`` (default:
    the registry attached by :func:`inspect_tree`); the returned node then carries
    ``resolved_from``. If that fails, ``bounds`` (when given) is hit-tested; otherwise a
    :class:`NodeKeyError` explains that the key is stale.

    For bounds, the deepest (most-nested) node whose bounds contain the box's centre wins;
    ties broken by smallest area (integ.md §1.4: "smallest IntegratedNode ... deepest match").
    """
    key = node_key
    if key is None and view_id is not None:
        key = view_key(view_id)
    if key is None and semantics_id is not None:
        key = f"compose:{int(semantics_id)}"
    registry = registry if registry is not None else getattr(merged, "registry", None)

    if key is not None:
        found = resolve_key(merged, key)  # raises NodeKeyError: malformed / ambiguous
        if found is not None:
            _note_key_drift(merged, found, registry)
            return found
        t = parse_node_key(key)
        if t[0] == "virt":
            cands: List[Dict[str, Any]] = []
            last_seen = ""
            if registry is not None:
                hit = registry.lookup(compose_key(t[1], t[2]))
                if hit is not None:
                    node, cands = _reresolve(merged, key, hit[1], hit[0])
                    if node is not None:
                        return node
                    last_seen = f"; last seen in generation {hit[0]}"
            if bounds is None:
                if t[1] in _known_acvs(merged) and t[2] <= _SLOT_ID_MAX:
                    # The agent gives slot-table composables negative ids (stable across
                    # dumps); the integrated tree holds semantics nodes only.
                    why = (f"{key} is a slot-table composable (negative id, from compose / "
                           "dump_compose with the slot table), and the integrated tree holds "
                           "only semantics nodes. Pass the node's bounds as well to hit-test "
                           "the element at that spot, or use a key from inspect, "
                           "dump_accessibility or a11y_lint")
                elif t[1] in _known_acvs(merged):
                    why = (f"{key} is not in the current dump (generation "
                           f"{merged.get('generation')}{last_seen}): Compose re-mints "
                           "semantics ids on recomposition. Re-run inspect, "
                           "dump_accessibility or a11y_lint and use a fresh key, or pass the "
                           "node's bounds as well")
                else:
                    why = (f"{key}: no ComposeView {t[1]} in the current dump (generation "
                           f"{merged.get('generation')}{last_seen}); it was detached, "
                           "recycled or navigated away")
                if cands:
                    why += "; closest current nodes: " + ", ".join(
                        c["node_key"] + (f" ({c['label']!r})" if c["label"] else "")
                        for c in cands)
                raise NodeKeyError(why + ".", cands)
        if bounds is None:
            return None

    if bounds is None:
        return None
    rect = _rect(bounds)
    if rect is None:
        return None
    cx = rect["x"] + rect["w"] // 2
    cy = rect["y"] + rect["h"] // 2
    best: Optional[Tuple[dict, int, int]] = None  # (node, depth, area)
    stack: List[Tuple[dict, int]] = [(r, 0) for r in merged.get("roots", [])]
    while stack:
        n, depth = stack.pop()
        # A child Compose merged into its parent (a11y_parent) is part of that element;
        # the hit-test names the element, as TalkBack's focus does.
        if not n.get("a11y_parent") and _contains_point(n.get("bounds"), cx, cy):
            area = _area(n.get("bounds"))
            if best is None or depth > best[1] or (depth == best[1] and area < best[2]):
                best = (n, depth, area)
        for c in n.get("children", []) or []:
            stack.append((c, depth + 1))
    return best[0] if best else None


def _known_acvs(merged: Dict[str, Any]) -> set:
    return {int((n.get("compose") or {}).get("acv") or 0)
            for n in _flat(merged.get("roots", [])) if n.get("compose")}


def _note_key_drift(merged: Dict[str, Any], node: dict, registry: Optional[KeyRegistry]) -> None:
    """Flag a Compose key that still exists but now names different content (e.g. a
    RecyclerView cell rebound to another row)."""
    if registry is None or not node.get("compose"):
        return
    hit = registry.lookup(node.get("node_key", ""))
    if hit is None:
        return
    old = hit[1]
    row = None
    for n, r in _compose_nodes_with_rows(merged.get("roots", [])):
        if n is node:
            row = r
            break
    cur = _fingerprint(node, row)
    fields = ("label", "test_tag", "row") if old.get("src", "merged") == "merged" else ("test_tag", "row")
    changed = [f for f in fields
               if old.get(f) is not None and cur.get(f) is not None and old[f] != cur[f]]
    if changed:
        node["key_note"] = (f"{node['node_key']} now names different content than when it "
                            f"was last dumped (generation {hit[0]}; changed: "
                            f"{', '.join(changed)}); the ComposeView was likely recycled or "
                            "rebound to other data")


# --------------------------------------------------------------------------- attribution
def node_path(merged: Dict[str, Any], node: dict) -> List[dict]:
    """Root-to-node list of IntegratedNodes (empty if ``node`` is not in the tree)."""
    stack: List[Tuple[dict, List[dict]]] = [(r, [r]) for r in merged.get("roots", [])]
    while stack:
        n, path = stack.pop()
        if n is node:
            return path
        for c in n.get("children", []) or []:
            stack.append((c, path + [c]))
    return []


def _describe(n: dict) -> str:
    key = n.get("node_key", "?")
    comp = n.get("compose") or {}
    attrs = comp.get("attrs") or {}
    if key.startswith("composeview:"):
        return key
    kind = (_simple((n.get("view") or {}).get("class_name"))
            or attrs.get("Role") or (comp.get("name") if not _node_label(n) else "") or "")
    label = _node_label(n)
    bits = [key]
    if kind:
        bits.append(str(kind))
    if label:
        bits.append(repr(label if len(label) <= 40 else label[:39] + "..."))
    return " ".join(bits)


def attribution(merged: Dict[str, Any], node: dict) -> Dict[str, Any]:
    """Where a node lives: window, list/row, ComposeView, AndroidView host.

    Returns ``{"where": "<breadcrumb>", "context": {...}}`` where the breadcrumb keeps
    only the salient ancestors (window root, lists, list items, ComposeViews, AndroidView
    hosts) — e.g. ``view:2 DecorView > view:20 RecyclerView > row 3: view:41 ComposeView >
    composeview:42 > compose:42:4 Button 'Delete'``.
    """
    path = node_path(merged, node)
    if not path:
        return {}
    ctx: Dict[str, Any] = {"window": path[0].get("node_key")}
    crumbs: List[str] = []
    for i, n in enumerate(path):
        key = n.get("node_key", "")
        last = i == len(path) - 1
        crumb = _describe(n)
        li = n.get("list_item")
        if li:
            ctx["list"] = li.get("list")
            ctx["item"] = key
            ctx.pop("row", None)
            ctx.pop("index", None)
            if "row" in li:
                ctx["row"] = li["row"]
                crumb = f"row {li['row']}: {crumb}"
            else:
                ctx["index"] = li.get("index")
                crumb = f"item {li.get('index')}: {crumb}"
        elif key.startswith("composeview:"):
            ctx["compose_view"] = key
        elif n.get("interop"):
            ctx["interop_host"] = n["interop"].get("hosted_by")
            crumb = f"AndroidView {crumb}"
        elif not (i == 0 or last or (n.get("a11y") or {}).get("collection_info")
                  or _simple((n.get("view") or {}).get("class_name")) in _LIST_CLASSES):
            continue  # not a salient ancestor
        crumbs.append(crumb)
    if node.get("compose"):
        ctx["compose_view"] = composeview_key(node["compose"].get("acv") or 0)
    return {"where": " > ".join(crumbs), "context": ctx}


# --------------------------------------------------------------------------- lint findings (CO2)
def finding_key(finding: dict) -> Optional[Tuple[Any, ...]]:
    """Canonical typed key a lint finding targets, or None if it is untyped.

    Accepts ``finding["node_key"]``, ``finding["node"]["node_key"|"key"]`` (typed
    strings) or an a11y id pair (``host_view_id`` + ``virtual_id``) on the node. A bare
    integer id is never used: it does not say which id space (View, ComposeView,
    a11y) it belongs to.
    """
    if not isinstance(finding, dict):
        return None
    node = finding.get("node") if isinstance(finding.get("node"), dict) else {}
    for k in (finding.get("node_key"), node.get("node_key"), node.get("key")):
        if isinstance(k, str):
            try:
                t = parse_node_key(k)
            except ValueError:
                continue
            if t[0] != "bare":
                return t
    if node.get("host_view_id") is not None:
        return _a11y_tuple(node)
    return None


def tag_compose_findings(findings: List[dict], compose_windows: List[dict]) -> List[dict]:
    """Give untyped findings from a Compose-semantics lint a typed ``node_key``.

    The lint's ``node.id`` is a semantics id (or the synthetic root's ACV id) with no
    ComposeView; resolve it against the windows, disambiguating ids that exist in several
    ComposeViews by the finding's bounds. Findings that cannot be placed stay untyped.
    """
    index: Dict[int, List[Tuple[int, dict, bool]]] = {}
    for w in compose_windows or []:
        acv = int(w.get("view_id") or 0)
        root = w.get("root")
        if not root:
            continue
        synthetic = int(root.get("id", -1)) == acv or root.get("name") == "AndroidComposeView"
        for n in _flat([root]):
            index.setdefault(int(n.get("id", 0)), []).append((acv, n, synthetic and n is root))
    for f in findings or []:
        if not isinstance(f, dict) or finding_key(f) is not None:
            continue
        node = f.get("node")
        if not isinstance(node, dict) or node.get("id") is None:
            continue
        try:
            nid = int(node["id"])
        except (TypeError, ValueError):
            continue
        cands = index.get(nid, [])
        if len(cands) > 1:
            fb = _rect(f.get("bounds"))
            scored = sorted(((_iou(fb, _rect(n.get("bounds"))), acv, n, syn)
                             for acv, n, syn in cands), key=lambda t: t[0], reverse=True)
            if fb and scored[0][0] > 0 and (len(scored) == 1 or scored[1][0] < scored[0][0]):
                cands = [(scored[0][1], scored[0][2], scored[0][3])]
            else:
                continue
        if len(cands) == 1:
            acv, _, syn = cands[0]
            node["node_key"] = composeview_key(acv) if syn else compose_key(acv, nid)
    return findings


def node_typed_keys(node: dict) -> set:
    """Every typed key an IntegratedNode answers to (its own + its a11y node's)."""
    keys = set()
    try:
        keys.add(parse_node_key(node.get("node_key", "")))
    except ValueError:
        pass
    a = node.get("a11y") or {}
    if a.get("host_view_id") is not None:
        keys.add(_a11y_tuple(a))
    return keys


def _finding_matches(finding: dict, keys: set) -> bool:
    t = finding_key(finding)
    return t is not None and t in keys


# --------------------------------------------------------------------------- session glue
def _shaped_view_tree(session: Any, props: bool) -> Tuple[List[dict], Dict[int, list]]:
    from . import strings as st
    resp = session.dump_tree(root_id=0, include_properties=props,
                             include_resolution_stack=False,
                             include_screenshot=False)
    data = st.dump_tree_to_dict(resp)
    prop_map: Dict[int, list] = {}
    for vid, plist in (data.get("properties") or {}).items():
        try:
            prop_map[int(vid)] = plist
        except (TypeError, ValueError):
            pass
    return data.get("roots", []), prop_map


def _shaped_compose(session: Any) -> List[dict]:
    from . import strings as st
    from .client import TransportError
    try:
        resp = session.dump_compose(include_semantics=True, include_slot_table=False,
                                    enable_inspection=False)
        data = st.dump_compose_to_dict(resp)
        return data.get("windows", []) or []
    except TransportError:
        raise  # a lost session is not "no Compose on screen"
    except Exception:
        return []


def _shaped_a11y_data(session: Any, rendering: bool = False) -> Any:
    """Fetch + shape the a11y dump (the ``a11y.a11y_to_dict`` dict; lists/dicts a fake
    returns pass through). ``rendering`` asks for ExtraRenderingInfo (text sizes), which
    the lint needs. None when the session has no a11y dump."""
    from .client import TransportError
    fn = getattr(session, "dump_a11y", None)
    if not callable(fn):
        return None
    try:
        data = (fn(root_id=0, include_extras=True, include_rendering_info=True)
                if rendering else fn())
    except TransportError:
        raise  # a lost session is not "no accessibility tree"
    except Exception:
        return None
    # Session.dump_a11y() returns the raw DumpA11yResponse proto; shape it to the
    # resolved dict the correlator expects (lists/dicts pass straight through).
    if not isinstance(data, (list, dict)):
        try:
            from inspector_widget import a11y as _a11ymod
            data = _a11ymod.a11y_to_dict(data)
        except Exception:
            return None
    return data


def _shaped_a11y(session: Any) -> List[dict]:
    """Fetch + shape the a11y tree as a list of window-root dicts. Degrades to [] if absent."""
    return _a11y_roots(_shaped_a11y_data(session))


def _a11y_roots(data: Any) -> List[dict]:
    """Normalise whatever the a11y shaper returns into a list of window-root node dicts."""
    if data is None:
        return []
    if isinstance(data, list):
        return [d for d in data if d]
    if isinstance(data, dict):
        if "windows" in data:
            roots = []
            for w in data.get("windows") or []:
                root = w.get("root") if isinstance(w, dict) else None
                if root:
                    roots.append(root)
            return roots
        if "roots" in data:
            return [r for r in (data.get("roots") or []) if r]
        if "root" in data and data["root"]:
            return [data["root"]]
    return []


def _merge_session(session: Any, props: bool, rendering: bool = False
                   ) -> Tuple[MergedTree, List[dict], Any]:
    """Fetch the three trees and merge them; returns (merged, compose windows, a11y data
    as ``a11y.a11y_to_dict`` shaped it, or a bare list of roots from a fake)."""
    view_roots, prop_map = _shaped_view_tree(session, props)
    compose_windows = _shaped_compose(session)
    a11y_data = _shaped_a11y_data(session, rendering)
    a11y_roots = _a11y_roots(a11y_data)
    merged = build_integrated_tree(view_roots, compose_windows, a11y_roots,
                                   props=prop_map if props else None)
    merged["sources"] = {
        "view": bool(view_roots),
        "compose": bool(compose_windows),
        "a11y": bool(a11y_roots),
    }
    merged.registry = registry_for(session)
    return merged, compose_windows, a11y_data


def inspect_tree(session: Any, include_properties: bool = False) -> Dict[str, Any]:
    """Whole-screen integrated tree (integ.md §1.3). Fetches view+compose+a11y, merges, returns.

    Returns ``{roots, summary, sources, generation}`` where ``sources`` records which facets
    were available and ``summary.generation`` identifies the Compose ids in this dump. The
    Compose keys are recorded in the session's registry so a later :func:`find_node` /
    :func:`inspect_node` can re-resolve them after recomposition.
    """
    merged, _, _ = _merge_session(session, include_properties)
    merged.registry.record(merged)
    return merged

# --------------------------------------------------------------------------- component image
def _capture_skp(session: Any) -> Optional[Tuple[bytes, int]]:
    fn = getattr(session, "capture_skp", None)
    if not callable(fn):
        client = getattr(session, "client", None)
        fn = getattr(client, "capture_skp", None) if client else None
    if not callable(fn):
        return None
    from .client import TransportError
    try:
        resp = fn(root_id=0)
    except TransportError:
        raise
    except Exception:
        return None
    if not getattr(resp, "supported", False):
        return None
    skp = bytes(getattr(resp, "skp", b"") or b"")
    if not skp:
        return None
    return skp, int(getattr(resp, "version", 0) or 0)


def _full_screenshot_png(session: Any, dest: str, root_id: int = 0
                         ) -> Optional[Tuple[str, float]]:
    """Capture window ``root_id`` (0 = the first, bottom window), write to ``dest`` PNG.
    Returns (path, scale) or None."""
    from . import png as pngmod
    from .client import TransportError
    try:
        resp = session.screenshot(root_id=int(root_id or 0), scale=1.0)
    except TransportError:
        raise
    except Exception:
        return None
    if not resp.HasField("screenshot"):
        return None
    pngmod.write_png(resp.screenshot, dest)
    scale = float(resp.screenshot.scale) if resp.screenshot.scale else 1.0
    return dest, scale


def node_window(merged: Optional[Dict[str, Any]], node: dict) -> Tuple[int, int, int]:
    """(root_view_id, origin x, origin y) of the window holding ``node``: the root View of
    its path in the merged tree. (0, 0, 0) when unknown (the first window)."""
    path = node_path(merged, node) if merged else []
    if not path:
        return 0, 0, 0
    root = path[0]
    rid = int((root.get("view") or {}).get("id") or 0)
    b = root.get("bounds") or {}
    return rid, int(b.get("x", 0) or 0), int(b.get("y", 0) or 0)


def window_origins(merged: Dict[str, Any]) -> List[Tuple[int, int, int]]:
    """[(root_view_id, screen x, screen y)] of the merged tree's windows, bottom first,
    for :func:`overlay.write_windows_png` (the integrated overlay shows dialogs too)."""
    out = []
    for r in merged.get("roots", []) or []:
        b = r.get("bounds") or {}
        out.append((int((r.get("view") or {}).get("id") or 0), int(b.get("x", 0) or 0),
                    int(b.get("y", 0) or 0)))
    return out


def _crop_png(src_png: str, rect: dict, scale: float, dest: str,
              origin: Tuple[int, int] = (0, 0)) -> Optional[str]:
    """Crop ``rect`` (absolute screen px) from a window PNG whose top-left sits at screen
    ``origin``, dividing by ``scale``."""
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        return None
    try:
        img = Image.open(src_png).convert("RGBA")
    except Exception:
        return None
    s = scale or 1.0
    x = int((rect["x"] - origin[0]) * s)
    y = int((rect["y"] - origin[1]) * s)
    w = max(1, int(rect["w"] * s))
    h = max(1, int(rect["h"] * s))
    x2 = min(img.width, x + w)
    y2 = min(img.height, y + h)
    x = max(0, min(x, img.width - 1))
    y = max(0, min(y, img.height - 1))
    if x2 <= x or y2 <= y:
        return None
    img.crop((x, y, x2, y2)).save(dest)
    return dest


def component_image(session: Any, node: dict, out_path: Optional[str] = None,
                    scale: float = 1.0, merged: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Cut a per-component image for one IntegratedNode (integ.md §1.4 / §1.5 / tool #15).

    Strategy: if the node has a Compose ``render_node_id`` (graphicsLayer layerId), capture an
    SKP and cut that layer via :mod:`skia_client`. Otherwise (or on any SKP failure) BITMAP-crop
    the node's bounds from a screenshot of the node's OWN window (a dialog or popup node is
    cut from the dialog, not from the activity behind it; ``merged`` locates the window).
    Returns ``{path, source: "skp"|"bitmap_crop", layer_id?, scale, window?, note?}``
    (``path`` None on failure).
    """
    out_path = out_path or _tmp_png("component")
    rect = _rect(node.get("bounds"))
    image_ref = node.get("image_ref") or {}
    layer_id = image_ref.get("layer_id")
    if layer_id is None:
        compose = node.get("compose") or {}
        layer_id = compose.get("render_node_id")
    note: Optional[str] = None

    # ---- Path A: SKP per-layer cut (Compose graphicsLayer) ----
    if layer_id and rect:
        captured = _capture_skp(session)
        if captured is not None:
            skp, version = captured
            try:
                from . import skia_client
                images = skia_client.per_component_images(
                    skp,
                    [(int(layer_id), rect["x"], rect["y"], rect["w"], rect["h"])],
                    version=version, scale=scale,
                )
                img = images.get(int(layer_id))
                if img is not None:
                    img.save_png(out_path)
                    return {"path": out_path, "source": "skp",
                            "layer_id": int(layer_id), "scale": scale}
                note = "skiaparser returned no image for that layer; fell back to crop"
            except Exception as exc:  # SkiaClientError or anything unexpected
                note = f"SKP path unavailable ({exc}); fell back to BITMAP crop"
        else:
            note = "SKP capture unsupported on this device; using BITMAP crop"

    # ---- Path B: BITMAP crop from a full screenshot ----
    if not rect:
        return {"path": None, "source": "bitmap_crop", "scale": scale,
                "error": "node has no bounds to crop"}
    base = _tmp_png("component_base")
    root_id, ox, oy = node_window(merged, node)
    try:
        shot = _full_screenshot_png(session, base, root_id)
    except BaseException:
        _safe_remove(base)
        raise
    if shot is None:
        _safe_remove(base)
        return {"path": None, "source": "bitmap_crop", "scale": scale,
                "error": "screenshot capture failed", "note": note}
    base_path, shot_scale = shot
    cropped = _crop_png(base_path, rect, shot_scale, out_path, (ox, oy))
    _safe_remove(base_path)
    if cropped is None:
        return {"path": None, "source": "bitmap_crop", "scale": shot_scale,
                "error": "crop failed (Pillow required for bitmap crop)", "note": note}
    result = {"path": cropped, "source": "bitmap_crop", "scale": shot_scale}
    if root_id:
        result["window"] = root_id
    if note:
        result["note"] = note
    return result


# --------------------------------------------------------------------------- inspect_node
def inspect_node(session: Any, *, node_key: Optional[str] = None,
                 view_id: Optional[int] = None, semantics_id: Optional[int] = None,
                 bounds: Optional[dict] = None, include_image: bool = True,
                 image_path: Optional[str] = None,
                 lint_fn: Optional[Callable[[Any, int], List[dict]]] = None,  # (roots, density_dpi) -> List[dict]
                 density: float = 0.0, lint: bool = False, font_scale: float = 1.0,
                 include_contrast: bool = True) -> Optional[Dict[str, Any]]:
    """Full dossier for ONE element (integ.md §1.4).

    Builds the merged tree (with view properties), resolves the target node by
    key/view_id/semantics_id/bounds (re-resolving a stale Compose key when the session saw
    it before), fully populates its facets (``view.properties`` via ``get_properties`` if
    missing, full a11y, full compose attrs), attributes it to its window / list row /
    ComposeView (``where`` + ``context``), cuts its component image from its own window, and
    attaches the lint findings that target it by typed key.

    ``lint=True`` runs exactly the a11y_lint report (:func:`a11y_lint.run_lint` on the same
    a11y dump, with Compose detail, rendering info and, with ``include_contrast``, the
    contrast sample of the node's window) and keeps the findings on this node and on the
    a11y nodes merged into it; ``lint_summary`` / ``lint_diagnostics`` say what ran (e.g.
    "contrast not sampled"). ``density`` is the device DPI.

    ``lint_fn(roots, density_dpi) -> [finding dicts]`` is the older hook (tests, custom
    lints): it is first given the unified a11y tree (``{"windows": [...]}``), and a lint
    that only understands Compose-semantics roots (it raises on that input) is called
    again with those.

    Raises :class:`NodeKeyError` for an ambiguous bare ``compose:<id>`` / ``semantics_id``
    or a stale Compose key that cannot be re-resolved.
    """
    # Build with properties so view.properties is available for the target; the lint
    # needs rendering info (text sizes) from the same a11y dump the keys come from.
    merged, compose_windows, a11y_data = _merge_session(session, props=True, rendering=lint)
    a11y_roots = _a11y_roots(a11y_data)
    registry = merged.registry
    try:
        node = find_node(merged, node_key=node_key, view_id=view_id,
                         semantics_id=semantics_id, bounds=bounds, registry=registry)
    finally:
        registry.record(merged)
    if node is None:
        return None

    # Ensure view.properties present (fetch on demand if not inlined).
    view = node.get("view") or {}
    vid = view.get("id")
    if vid and not view.get("properties"):
        props = _fetch_properties(session, int(vid))
        if props is not None:
            view["properties"] = props
            node["view"] = view

    dossier: Dict[str, Any] = {
        "node_key": node.get("node_key"),
        "bounds": node.get("bounds"),
        "correlation_confidence": node.get("correlation_confidence"),
        "generation": merged.get("generation"),
    }
    for facet in ("view", "compose", "a11y", "render_quad", "a11y_iou", "list_item",
                  "interop", "a11y_only", "a11y_parent", "compose_synthetic",
                  "resolved_from", "key_note"):
        if node.get(facet) is not None:
            dossier[facet] = node[facet]
    dossier.update(attribution(merged, node))

    # Component image.
    if include_image:
        img_dest = image_path or _tmp_png("dossier")
        dossier["component_image"] = component_image(session, node, out_path=img_dest,
                                                     merged=merged)

    # Focused lint: keep the findings whose TYPED key is one of this node's keys (never
    # a bare int across id spaces), or one of the a11y nodes Compose merged into it.
    keys = node_typed_keys(node)
    for c in _flat(node.get("children") or []):
        if c.get("a11y_only"):
            keys |= node_typed_keys(c)
    if lint and isinstance(a11y_data, dict) and a11y_roots:
        from . import a11y_lint
        report = a11y_lint.run_lint(
            session, density=int(density) if density else None, font_scale=font_scale,
            include_contrast=include_contrast, a11y_data=a11y_data,
            compose_data={"windows": compose_windows}, focus_keys=_key_strings(keys))
        out = report.to_dict()
        dossier["lint"] = [f for f in out["findings"] if _finding_matches(f, keys)]
        dossier["lint_summary"] = a11y_lint.summarize_dicts(dossier["lint"])
        dossier["lint_diagnostics"] = out["diagnostics"]
    elif lint_fn is not None:
        all_findings = _run_lint_fn(lint_fn, a11y_roots, compose_windows, density)
        tag_compose_findings(all_findings, compose_windows)
        dossier["lint"] = [f for f in all_findings if _finding_matches(f, keys)]

    return dossier


def _key_strings(keys: set) -> set:
    """Typed key tuples -> the node_key strings the lint uses (both compose/virtual)."""
    out = set()
    for t in keys:
        if t[0] == "view":
            out.add(view_key(t[1]))
        elif t[0] == "virt":
            out.update({compose_key(t[1], t[2]), f"virtual:{t[1]}:{t[2]}"})
        elif t[0] == "composeview":
            out.add(view_key(t[1]))
    return out


def _run_lint_fn(lint_fn: Callable[[Any, int], List[dict]], a11y_roots: List[dict],
                 compose_windows: List[dict], density: float) -> List[dict]:
    """Lint the unified a11y tree, else (a Compose-only lint) the Compose-semantics roots."""
    if a11y_roots:
        unified = {"windows": [{"root_view_id": _a11y_host(r), "root": r} for r in a11y_roots]}
        try:
            out = lint_fn(unified, density)
            if isinstance(out, list):
                return out
        except Exception:
            pass
    compose_roots = [w.get("root") for w in compose_windows if w.get("root")]
    if not compose_roots:
        return []
    try:
        return lint_fn(compose_roots, density) or []
    except Exception:
        return []


def _fetch_properties(session: Any, view_id: int) -> Optional[list]:
    from . import strings as st
    from .client import TransportError
    try:
        resp = session.get_properties(view_id=view_id, include_resolution_stack=False)
    except TransportError:
        raise
    except Exception:
        return None
    try:
        data = st.get_properties_to_dict(resp)
        return data.get("properties")
    except Exception:
        return None


# --------------------------------------------------------------------------- misc
def _tmp_png(tag: str) -> str:
    fd, path = tempfile.mkstemp(prefix=f"viewspector_{tag}_", suffix=".png")
    os.close(fd)
    return path


def _safe_remove(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def flatten(merged: Dict[str, Any]) -> List[dict]:
    """Flatten the merged tree to a list of IntegratedNodes (for overlays / iteration)."""
    return _flat(merged.get("roots", []))
