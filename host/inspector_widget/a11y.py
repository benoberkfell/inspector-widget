"""Resolve a ``DumpA11yResponse`` to a nested dict and compute TalkBack order.

The agent (``AccessibilityInspector.kt``) emits one ``A11yNode`` proto per
``AccessibilityNodeInfo`` (Views + Compose virtual nodes, unified via
``setQueryFromAppProcessEnabled`` / the provider path — see extraction.md §1).
Every ``CharSequence`` field is interned through the shared ``StringTable`` and
travels as an int32 string-table id; this module resolves those ids back to text
and decodes the structured sub-messages (actions / collection / range / extras)
into a plain JSON-friendly dict.

It also re-implements, host-side, the TalkBack reading order (extraction.md §3):
per sibling group, apply ``traversal_before`` / ``traversal_after`` topological
constraints, otherwise fall back to geometry (top-to-bottom, then left-to-right),
and number every node in the resulting linear order. Keeping the ordering on the
host means we don't reimplement framework ordering in Kotlin and it stays
testable against UiAutomator dumps.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .proto import view_inspection_pb2 as pb
from .strings import StringResolver, _bounds_to_dict

# --------------------------------------------------------------------------- #
# AccessibilityNodeInfo action-id constants (API 36 platform values).
#
# The agent emits raw action ids in A11yAction.id (extraction.md §2 "Actions").
# The standard actions are the ACTION_* int constants on
# android.view.accessibility.AccessibilityNodeInfo; custom actions carry an
# R.id-based id (>= 0x01000000 / the aapt resource-id space) and a label string.
# These literal values are stable platform constants.
# --------------------------------------------------------------------------- #
ACTION_NAMES: Dict[int, str] = {
    0x00000001: "FOCUS",
    0x00000002: "CLEAR_FOCUS",
    0x00000004: "SELECT",
    0x00000008: "CLEAR_SELECTION",
    0x00000010: "CLICK",
    0x00000020: "LONG_CLICK",
    0x00000040: "ACCESSIBILITY_FOCUS",
    0x00000080: "CLEAR_ACCESSIBILITY_FOCUS",
    0x00000100: "NEXT_AT_MOVEMENT_GRANULARITY",
    0x00000200: "PREVIOUS_AT_MOVEMENT_GRANULARITY",
    0x00000400: "NEXT_HTML_ELEMENT",
    0x00000800: "PREVIOUS_HTML_ELEMENT",
    0x00001000: "SCROLL_FORWARD",
    0x00002000: "SCROLL_BACKWARD",
    0x00004000: "COPY",
    0x00008000: "PASTE",
    0x00010000: "CUT",
    0x00020000: "SET_SELECTION",
    0x00040000: "EXPAND",
    0x00080000: "COLLAPSE",
    0x00100000: "DISMISS",
    0x00200000: "SET_TEXT",
    # R.id-backed standard actions (android.R.id.accessibilityAction*).
    0x0102003D: "SHOW_ON_SCREEN",
    0x0102003E: "SCROLL_TO_POSITION",
    0x0102003F: "SCROLL_UP",
    0x01020040: "SCROLL_LEFT",
    0x01020041: "SCROLL_DOWN",
    0x01020042: "SCROLL_RIGHT",
    0x01020043: "CONTEXT_CLICK",
    0x01020044: "SET_PROGRESS",
    0x01020045: "MOVE_WINDOW",
    0x01020046: "SHOW_TOOLTIP",
    0x01020047: "HIDE_TOOLTIP",
    0x01020048: "PAGE_UP",
    0x01020049: "PAGE_DOWN",
    0x0102004A: "PAGE_LEFT",
    0x0102004B: "PAGE_RIGHT",
    0x0102004C: "PRESS_AND_HOLD",
    0x0102004D: "IME_ENTER",
    0x0102004E: "DRAG_START",
    0x0102004F: "DRAG_DROP",
    0x01020050: "DRAG_CANCEL",
    0x01020051: "SHOW_TEXT_SUGGESTIONS",
    0x01020055: "SCROLL_IN_DIRECTION",
}

# Enum decodes (small, stable platform enums).
_CHECKED_STATE = {0: "FALSE", 1: "TRUE", 2: "PARTIAL"}
_LIVE_REGION = {0: "NONE", 1: "POLITE", 2: "ASSERTIVE"}
_IMPORTANT_FOR_A11Y = {0: "AUTO", 1: "YES", 2: "NO", 4: "NO_HIDE_DESCENDANTS"}
_RANGE_TYPE = {0: "INT", 1: "FLOAT", 2: "PERCENT", 3: "INDETERMINATE"}
_EXPANDED_STATE = {0: "UNDEFINED", 1: "COLLAPSED", 2: "PARTIAL", 3: "FULL"}

# Booleans on A11yNode worth surfacing as a compact flag list (only when true).
_BOOL_FLAGS = (
    "clickable", "long_clickable", "context_clickable", "checkable", "checked",
    "focusable", "focused", "accessibility_focused", "selected", "enabled",
    "password", "scrollable", "visible_to_user", "heading",
    "screen_reader_focusable", "dismissable", "editable", "multi_line",
    "content_invalid", "showing_hint_text", "text_entry_key", "text_selectable",
    "field_required", "can_open_popup", "a11y_data_sensitive",
    "request_initial_focus", "is_virtual", "is_traversal_group",
)

# String-table-id text fields -> output key.
_TEXT_FIELDS = (
    ("text", "text"),
    ("content_description", "content_description"),
    ("hint_text", "hint_text"),
    ("state_description", "state_description"),
    ("error", "error"),
    ("tooltip_text", "tooltip_text"),
    ("pane_title", "pane_title"),
    ("container_title", "container_title"),
    ("supplemental_description", "supplemental_description"),
    ("role_description", "role_description"),
    ("class_name", "class_name"),
    ("package_name", "package_name"),
    ("view_id_resource_name", "view_id_resource_name"),
    ("unique_id", "unique_id"),
    ("provider_class", "provider_class"),
)

# int32 numeric fields surfaced only when non-zero, with optional enum decode.
_INT_FIELDS = (
    ("input_type", "input_type", None),
    ("movement_granularities", "movement_granularities", None),
    ("max_text_length", "max_text_length", None),
    ("drawing_order", "drawing_order", None),
    ("text_selection_start", "text_selection_start", None),
    ("text_selection_end", "text_selection_end", None),
    ("actions_bitmask", "actions_bitmask", None),
    ("checked_state", "checked_state", _CHECKED_STATE),
    ("live_region", "live_region", _LIVE_REGION),
    ("important_for_accessibility", "important_for_accessibility", _IMPORTANT_FOR_A11Y),
    ("expanded_state", "expanded_state", _EXPANDED_STATE),
)


def action_name(action_id: int, label: Optional[str] = None) -> str:
    """Decode a raw AccessibilityAction id to a name (CLICK/SCROLL_FORWARD/...).

    Falls back to the custom-action label when present, else CUSTOM_0x... so the
    finding/overlay still has something to show.
    """
    name = ACTION_NAMES.get(action_id)
    if name is not None:
        return name
    if label:
        return label
    return "CUSTOM_0x%08X" % (action_id & 0xFFFFFFFF)


def _collection_info_to_dict(ci: "pb.A11yCollectionInfo") -> Dict[str, Any]:
    return {
        "row_count": ci.row_count,
        "column_count": ci.column_count,
        "hierarchical": ci.hierarchical,
        "selection_mode": ci.selection_mode,
        "item_count": ci.item_count,
        "important_item_count": ci.important_item_count,
    }


def _collection_item_info_to_dict(
    it: "pb.A11yCollectionItemInfo", resolver: StringResolver
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "row_index": it.row_index,
        "column_index": it.column_index,
        "row_span": it.row_span,
        "column_span": it.column_span,
        "heading": it.heading,
        "selected": it.selected,
    }
    row_title = resolver.opt(it.row_title)
    if row_title is not None:
        out["row_title"] = row_title
    col_title = resolver.opt(it.column_title)
    if col_title is not None:
        out["column_title"] = col_title
    return out


def _range_info_to_dict(ri: "pb.A11yRangeInfo") -> Dict[str, Any]:
    return {
        "type": _RANGE_TYPE.get(ri.type, ri.type),
        "min": ri.min,
        "max": ri.max,
        "current": ri.current,
    }


def a11y_node_to_dict(node: "pb.A11yNode", resolver: StringResolver) -> Dict[str, Any]:
    """Resolve one ``A11yNode`` (recursively) to a plain dict.

    The canonical node key is the (host_view_id, virtual_id) pair (extraction.md
    §2 "Identity"); it ties the a11y node back to a ViewNode.id / ComposeNode.id.
    """
    out: Dict[str, Any] = {
        "host_view_id": node.host_view_id,
        "virtual_id": node.virtual_id,
        # Synthetic stable key used by linkage/findings/overlay.
        "id": (node.host_view_id << 32) ^ (node.virtual_id & 0xFFFFFFFF),
        "bounds": _bounds_to_dict(node.bounds),
    }

    # Text / identity string fields (only when present).
    for proto_field, out_key in _TEXT_FIELDS:
        sid = getattr(node, proto_field)
        text = resolver.opt(sid)
        if text is not None:
            out[out_key] = text

    # A compact "best label" mirroring what TalkBack would speak first.
    label = (
        out.get("content_description")
        or out.get("text")
        or out.get("state_description")
        or out.get("hint_text")
    )
    if label:
        out["speakable"] = label

    # Boolean flags -> list (only the true ones, to keep the dict compact).
    flags: List[str] = [f for f in _BOOL_FLAGS if getattr(node, f, False)]
    if flags:
        out["flags"] = flags

    # Numeric / enum fields (only when non-zero).
    for proto_field, out_key, enum in _INT_FIELDS:
        v = getattr(node, proto_field)
        if v:
            out[out_key] = enum.get(v, v) if enum else v

    # Structured sub-messages.
    if node.HasField("collection_info"):
        out["collection_info"] = _collection_info_to_dict(node.collection_info)
    if node.HasField("collection_item_info"):
        out["collection_item_info"] = _collection_item_info_to_dict(
            node.collection_item_info, resolver
        )
    if node.HasField("range_info"):
        out["range_info"] = _range_info_to_dict(node.range_info)

    # Actions: decode each id to a name; keep custom labels.
    if node.actions:
        actions: List[Dict[str, Any]] = []
        for a in node.actions:
            label_str = resolver.opt(a.label)
            actions.append(
                {
                    "id": a.id,
                    "name": action_name(a.id, label_str),
                    **({"label": label_str} if label_str is not None else {}),
                }
            )
        out["actions"] = actions

    # Extras: a {key: value} map (string ids both sides).
    if node.extras:
        extras: Dict[str, str] = {}
        for e in node.extras:
            key = resolver.get(e.key)
            if key:
                extras[key] = resolver.get(e.value)
        if extras:
            out["extras"] = extras

    # Linkage (target packed ids; 0 == none).
    for proto_field, out_key in (
        ("traversal_before", "traversal_before"),
        ("traversal_after", "traversal_after"),
        ("label_for", "label_for"),
        ("labeled_by", "labeled_by"),
    ):
        v = getattr(node, proto_field)
        if v:
            out[out_key] = v
    if node.labeled_by_list:
        out["labeled_by_list"] = list(node.labeled_by_list)

    # ExtraRenderingInfo (only if the agent populated it).
    if node.text_size_px:
        out["text_size_px"] = node.text_size_px
    if node.text_size_unit:
        out["text_size_unit"] = node.text_size_unit
    if node.layout_size_w or node.layout_size_h:
        out["layout_size"] = {"w": node.layout_size_w, "h": node.layout_size_h}

    if node.children:
        out["children"] = [a11y_node_to_dict(c, resolver) for c in node.children]
    return out


def a11y_to_dict(response: "pb.DumpA11yResponse") -> Dict[str, Any]:
    """Resolve a full ``DumpA11yResponse`` to a JSON-friendly dict.

    Output shape::

        {"windows": [{"root_view_id", "root": <node>|None}, ...],
         "diagnostics"?: str,
         "focus_order"?: [ ... see compute_traversal_order ... ]}
    """
    resolver = StringResolver(response.strings)
    windows: List[Dict[str, Any]] = []
    for w in response.windows:
        root = a11y_node_to_dict(w.root, resolver) if w.HasField("root") else None
        windows.append({"root_view_id": w.root_view_id, "root": root})
    out: Dict[str, Any] = {"windows": windows}
    if response.diagnostics:
        out["diagnostics"] = response.diagnostics

    # Attach the computed TalkBack reading order across all windows.
    roots = [w["root"] for w in windows if w["root"]]
    out["focus_order"] = compute_traversal_order(roots)
    return out


# --------------------------------------------------------------------------- #
# TalkBack reading order (extraction.md §3).
# --------------------------------------------------------------------------- #
def _node_center(node: Dict[str, Any]) -> Tuple[int, int]:
    b = (node.get("bounds") or {}).get("layout") or {}
    x = b.get("x", 0)
    y = b.get("y", 0)
    w = b.get("w", 0)
    h = b.get("h", 0)
    return x + w // 2, y + h // 2


def _geometry_sort(children: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Top-to-bottom then left-to-right, grouping into rows by vertical overlap.

    Implements TalkBack's "same line" heuristic: two nodes are on the same row
    when their vertical overlap exceeds ~50% of the shorter height; within a row
    sort left-to-right, rows themselves sort by top edge. ``drawing_order`` is a
    stable tiebreaker.
    """

    def top(n: Dict[str, Any]) -> int:
        return ((n.get("bounds") or {}).get("layout") or {}).get("y", 0)

    def height(n: Dict[str, Any]) -> int:
        return ((n.get("bounds") or {}).get("layout") or {}).get("h", 0)

    def left(n: Dict[str, Any]) -> int:
        return ((n.get("bounds") or {}).get("layout") or {}).get("x", 0)

    def same_row(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
        ay, ah = top(a), height(a)
        by, bh = top(b), height(b)
        if ah <= 0 or bh <= 0:
            return ay == by
        overlap = min(ay + ah, by + bh) - max(ay, by)
        return overlap > 0.5 * min(ah, bh)

    ordered = sorted(children, key=lambda n: (top(n), left(n), n.get("drawing_order", 0)))
    rows: List[List[Dict[str, Any]]] = []
    for n in ordered:
        placed = False
        for row in rows:
            if same_row(row[0], n):
                row.append(n)
                placed = True
                break
        if not placed:
            rows.append([n])
    rows.sort(key=lambda r: min(top(x) for x in r))
    out: List[Dict[str, Any]] = []
    for row in rows:
        row.sort(key=lambda n: (left(n), n.get("drawing_order", 0)))
        out.extend(row)
    return out


def _apply_traversal_constraints(children: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Order a sibling group honouring traversal_before / traversal_after.

    Geometry gives the baseline order; ``traversal_before = X`` pulls a node to
    just before X and ``traversal_after = Y`` pushes it to just after Y. We build
    a directed-constraint graph over the siblings and topologically sort it,
    breaking ties (and cycles) with the geometric order (extraction.md §3.2a).
    """
    base = _geometry_sort(children)
    if len(base) < 2:
        return base

    index = {id(n): i for i, n in enumerate(base)}
    by_key: Dict[int, Dict[str, Any]] = {}
    for n in base:
        k = n.get("id")
        if k is not None:
            by_key[k] = n

    # Build edges: edge a -> b means "a must come before b".
    successors: Dict[int, set] = {id(n): set() for n in base}
    indeg: Dict[int, int] = {id(n): 0 for n in base}

    def add_edge(a: Dict[str, Any], b: Dict[str, Any]) -> None:
        if a is b:
            return
        if id(b) not in successors[id(a)]:
            successors[id(a)].add(id(b))
            indeg[id(b)] += 1

    for n in base:
        tb = n.get("traversal_before")
        if tb and tb in by_key:
            add_edge(n, by_key[tb])  # n before its traversal_before target
        ta = n.get("traversal_after")
        if ta and ta in by_key:
            add_edge(by_key[ta], n)  # n after its traversal_after target

    # Kahn topo-sort, picking the geometrically-earliest ready node each step.
    import heapq

    ready = [index[id(n)] for n in base if indeg[id(n)] == 0]
    heapq.heapify(ready)
    pos_of = {index[id(n)]: n for n in base}
    ordered: List[Dict[str, Any]] = []
    seen: set = set()
    while ready:
        i = heapq.heappop(ready)
        n = pos_of[i]
        ordered.append(n)
        seen.add(id(n))
        for sid in successors[id(n)]:
            indeg[sid] -= 1
            if indeg[sid] == 0:
                # Find the base index of the successor node.
                for m in base:
                    if id(m) == sid:
                        heapq.heappush(ready, index[id(m)])
                        break
    # Cycle fallback: append any nodes the topo-sort couldn't place, in geometry order.
    if len(ordered) != len(base):
        for n in base:
            if id(n) not in seen:
                ordered.append(n)
    return ordered


def _is_focus_stop(node: Dict[str, Any]) -> bool:
    """Whether TalkBack would stop on this node (a focus stop) vs. structural.

    A node is a stop when it is screen-reader-focusable, OR it carries
    announceable content (text/contentDescription/role) and is visible & not
    explicitly excluded (extraction.md §3.4).
    """
    flags = set(node.get("flags") or [])
    extras = node.get("extras") or {}
    if "InvisibleToUser" in extras:
        return False
    if "visible_to_user" not in flags:
        return False
    if "screen_reader_focusable" in flags:
        return True
    has_content = bool(
        node.get("text")
        or node.get("content_description")
        or node.get("state_description")
        or node.get("role_description")
    )
    actionable = bool(
        {"clickable", "long_clickable", "checkable", "editable"} & flags
    )
    return has_content or actionable


def compute_traversal_order(roots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Produce the linear TalkBack reading order across the given root nodes.

    Returns a list of ``{"order": int, "id", "host_view_id", "virtual_id",
    "speakable", "bounds", "is_focus_stop"}`` — the sequence TalkBack would walk.
    ``order`` numbers only the actual focus stops (1-based); structural nodes are
    still listed with ``is_focus_stop=false`` and ``order=None`` so the full
    walk is inspectable.

    Per sibling group we apply traversal_before/after constraints, else geometry
    (extraction.md §3). isTraversalGroup nodes are visited as an atomic unit
    because the recursion descends fully into a node before its next sibling.
    """
    walk: List[Dict[str, Any]] = []

    def recurse(node: Dict[str, Any]) -> None:
        stop = _is_focus_stop(node)
        entry = {
            "id": node.get("id"),
            "host_view_id": node.get("host_view_id"),
            "virtual_id": node.get("virtual_id"),
            "speakable": node.get("speakable"),
            "bounds": (node.get("bounds") or {}).get("layout"),
            "is_focus_stop": stop,
            "order": None,
        }
        walk.append(entry)
        children = node.get("children") or []
        if children:
            for child in _apply_traversal_constraints(list(children)):
                recurse(child)

    # Order the roots among themselves geometrically too.
    for root in _geometry_sort(list(roots)):
        recurse(root)

    counter = 0
    for entry in walk:
        if entry["is_focus_stop"]:
            counter += 1
            entry["order"] = counter
    return walk
