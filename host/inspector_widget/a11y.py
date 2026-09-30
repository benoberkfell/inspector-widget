"""Resolve a ``DumpA11yResponse`` to a nested dict and compute TalkBack order.

The agent (``AccessibilityInspector.kt``) emits one ``A11yNode`` proto per
``AccessibilityNodeInfo`` (Views + Compose virtual nodes, unified via
``setQueryFromAppProcessEnabled`` / the provider path — see extraction.md §1).
Every ``CharSequence`` field is interned through the shared ``StringTable`` and
travels as an int32 string-table id; this module resolves those ids back to text
and decodes the structured sub-messages (actions / collection / range / extras)
into a plain JSON-friendly dict.

Identity (the ID contract shared with the agent, correlate.py and overlay.py):

* ``host_view_id`` is the uniqueDrawingId of the node's OWN backing View (the
  real View for View nodes; the provider host, e.g. the AndroidComposeView, for
  virtual nodes).
* ``virtual_id`` is ``-1`` (``HOST_VIEW_ID``) for real Views, otherwise the
  virtual descendant id (for Compose it equals the SemanticsNode id).
* The integer node key is ``(host_view_id << 32) ^ (virtual_id & 0xFFFFFFFF)``
  (:func:`a11y_key`); ``traversal_before/after``, ``label_for``, ``labeled_by``
  and ``labeled_by_list`` arrive from the agent in that same key space (0 = none).
* The typed key (``node_key``) is ``view:<id>`` for real Views,
  ``compose:<acv>:<semanticsId>`` for Compose virtual nodes and
  ``virtual:<host>:<virtualId>`` for other providers (WebView, ExploreByTouchHelper).

It also re-implements, host-side, the TalkBack reading order (see
:func:`reading_order`): the ANI child order the platform already sorted,
``traversal_before`` / ``traversal_after`` applied across the whole tree the way
TalkBack's ``OrderedTraversalController`` does, and TalkBack's focusability rules
(``shouldFocusNode``) to decide which nodes are focus stops and what each one says.
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


# --------------------------------------------------------------------------- #
# Node keys (the ID contract; see the module docstring).
# --------------------------------------------------------------------------- #
HOST_VIEW_ID = -1  # AccessibilityNodeProvider.HOST_VIEW_ID: virtual_id of a real View


def a11y_key(host_view_id: int, virtual_id: int) -> int:
    """Integer node key: ``(host_view_id << 32) ^ (virtual_id & 0xFFFFFFFF)``.

    The agent emits traversal/label linkage targets in this same space, so a
    linkage value can be looked up directly against the nodes' ``id``.
    """
    return (int(host_view_id) << 32) ^ (int(virtual_id) & 0xFFFFFFFF)


def _typed_key(host_view_id: int, virtual_id: int, compose: bool) -> str:
    if int(virtual_id) == HOST_VIEW_ID:
        return f"view:{int(host_view_id)}"
    prefix = "compose" if compose else "virtual"
    return f"{prefix}:{int(host_view_id)}:{int(virtual_id)}"


def parse_node_key(key: Any) -> Tuple[Any, ...]:
    """Parse a typed node key into a canonical tuple.

    * ``view:<id>``                    -> ``("view", id)``
    * ``composeview:<acv>``            -> ``("composeview", acv)``
    * ``compose:<acv>:<semanticsId>``  -> ``("virt", acv, semanticsId)``
    * ``virtual:<host>:<virtualId>``   -> ``("virt", host, virtualId)``
    * ``compose:<semanticsId>`` (bare) -> ``("bare", semanticsId)`` — ambiguous
      across ComposeViews; callers must resolve it against a dump.

    ``compose:`` and ``virtual:`` share one canonical form because both name a
    (host View, virtual id) pair; which prefix is displayed only depends on
    whether the host is known to be a Compose provider. Raises ``ValueError``
    for anything else.
    """
    if not isinstance(key, str):
        raise ValueError(f"node key must be a string, got {key!r}")
    parts = key.strip().split(":")
    try:
        nums = [int(p) for p in parts[1:]]
    except ValueError:
        raise ValueError(f"malformed node key {key!r}: ids must be integers") from None
    kind = parts[0].lower()
    if kind == "view" and len(nums) == 1:
        return ("view", nums[0])
    if kind == "composeview" and len(nums) == 1:
        return ("composeview", nums[0])
    if kind in ("compose", "virtual") and len(nums) == 2:
        return ("virt", nums[0], nums[1])
    if kind == "compose" and len(nums) == 1:
        return ("bare", nums[0])
    raise ValueError(
        f"malformed node key {key!r}: expected view:<id>, compose:<acvId>:<semanticsId>, "
        f"composeview:<acvId>, virtual:<hostId>:<virtualId> or compose:<semanticsId>")


def node_key_tuple(node: Dict[str, Any]) -> Optional[Tuple[Any, ...]]:
    """Canonical key tuple of a shaped a11y node dict (``None`` if it has no ids)."""
    h = node.get("host_view_id")
    if h is None:
        return None
    v = node.get("virtual_id")
    if v is None or int(v) == HOST_VIEW_ID:
        return ("view", int(h))
    return ("virt", int(h), int(v))


_COMPOSE_HINTS = ("compose",)


def _is_compose_provider(node: Dict[str, Any]) -> bool:
    """Whether a real-View a11y node is a Compose host (AndroidComposeView).

    The agent sets ``provider_class`` on provider hosts; the host View's own
    class name is a second hint.
    """
    for field in ("provider_class", "class_name"):
        v = (node.get(field) or "").lower()
        if any(h in v for h in _COMPOSE_HINTS):
            return True
    return False


def assign_node_keys(roots: List[Dict[str, Any]]) -> None:
    """Set ``node_key`` on every node, upgrading virtual nodes of Compose hosts
    from ``virtual:<h>:<v>`` to ``compose:<h>:<v>``."""
    compose_hosts = set()
    for n in _iter_nodes(roots):
        if int(n.get("virtual_id", HOST_VIEW_ID)) == HOST_VIEW_ID and _is_compose_provider(n):
            compose_hosts.add(int(n.get("host_view_id", 0)))
    for n in _iter_nodes(roots):
        h = int(n.get("host_view_id", 0))
        v = int(n.get("virtual_id", HOST_VIEW_ID))
        n["node_key"] = _typed_key(h, v, compose=h in compose_hosts)


def _iter_nodes(roots: List[Dict[str, Any]]):
    stack = list(reversed([r for r in roots if r]))
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.get("children") or []))


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

    The canonical node key is the (host_view_id, virtual_id) pair (see the module
    docstring); it ties the a11y node back to a ViewNode.id (``virtual_id == -1``)
    or to a ComposeNode (``host_view_id`` = its AndroidComposeView, ``virtual_id``
    = its semantics id).
    """
    out: Dict[str, Any] = {
        "host_view_id": node.host_view_id,
        "virtual_id": node.virtual_id,
        # Integer key used by linkage/findings/overlay (same space as the agent's
        # traversal/label linkage ids).
        "id": a11y_key(node.host_view_id, node.virtual_id),
        # Typed key; a11y_to_dict upgrades virtual:<h>:<v> to compose:<h>:<v>
        # once it knows which hosts are Compose providers.
        "node_key": _typed_key(node.host_view_id, node.virtual_id, compose=False),
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
         "focus_order": [{"order", "key", "id", "speak"}, ...],   # focus stops only
         "summary": {"windows", "nodes", "focus_stops"},
         "diagnostics"?: str,                       # from the agent
         "reading_order_diagnostics"?: [...]}       # cycles / dangling linkage

    Every node carries ``id`` (integer key) and ``node_key`` (typed key); focus
    stops also carry ``order`` so the tree and ``focus_order`` cross-reference
    without the list repeating the tree's data.
    """
    resolver = StringResolver(response.strings)
    windows: List[Dict[str, Any]] = []
    for w in response.windows:
        root = a11y_node_to_dict(w.root, resolver) if w.HasField("root") else None
        windows.append({"root_view_id": w.root_view_id, "root": root})
    roots = [w["root"] for w in windows if w["root"]]
    assign_node_keys(roots)

    ro = reading_order(roots)
    by_id = {id(n): n for n in _iter_nodes(roots)}
    for entry, node in zip(ro["focus_order"], ro["_nodes"]):
        by_id[id(node)]["order"] = entry["order"]

    out: Dict[str, Any] = {"windows": windows, "focus_order": ro["focus_order"]}
    out["summary"] = {
        "windows": len(windows),
        "nodes": len(by_id),
        "focus_stops": len(ro["focus_order"]),
    }
    if response.diagnostics:
        out["diagnostics"] = response.diagnostics
    if ro["diagnostics"]:
        out["reading_order_diagnostics"] = ro["diagnostics"]
    return out


# --------------------------------------------------------------------------- #
# TalkBack reading order.
#
# Mirrors how TalkBack linearises the screen (OrderedTraversalController +
# AccessibilityNodeInfoUtils.shouldFocusNode):
#
# 1. Start from the ANI child order. The platform has already sorted it (Views:
#    ViewGroup's ChildListForAccessibility; Compose: the delegate's geometric
#    grouping), so the host must not re-sort by geometry.
# 2. Apply traversal_before / traversal_after across the WHOLE tree (not per
#    sibling group), as TalkBack's OrderedTraversalController does: the node with
#    the attribute moves, its target stays; traversal_before=T reads the node (and
#    its subtree) immediately before T, traversal_after=T immediately after T's
#    subtree. Constraints compose, so Compose's traversal chain (every focusable
#    node linked to the next) is realised wherever its nodes sit in the ANI tree.
#    Constraints that form a cycle are skipped and reported. is_traversal_group is
#    honoured: a node inside a traversal group that is ordered relative to a node
#    outside that group moves together with the group.
# 3. Walk the re-arranged tree depth-first; a node is a focus stop when TalkBack
#    would focus it: it is visible and actionable/focusable (clickable,
#    long-clickable, focusable, screen-reader-focusable, or a top-level list/
#    scroll item that speaks), and either has no visible children or has
#    something to speak; or it is not focusable itself but has text and no
#    focusable ancestor.
# 4. A focus stop's announcement is composed like TalkBack's: its
#    contentDescription, else its text plus the descriptions of its visible,
#    non-focusable descendants, then role and state ("Add to favorites, button").
# --------------------------------------------------------------------------- #
_ACTION_FOCUS = 0x00000001
_ACTION_CLICK = 0x00000010
_ACTION_LONG_CLICK = 0x00000020

# Class simple name -> the role word TalkBack speaks.
_ROLE_BY_CLASS = {
    "Button": "button", "AppCompatButton": "button", "MaterialButton": "button",
    "ImageButton": "button", "AppCompatImageButton": "button",
    "FloatingActionButton": "button", "ExtendedFloatingActionButton": "button",
    "CheckBox": "checkbox", "AppCompatCheckBox": "checkbox", "MaterialCheckBox": "checkbox",
    "Switch": "switch", "SwitchCompat": "switch", "SwitchMaterial": "switch",
    "MaterialSwitch": "switch", "ToggleButton": "toggle button",
    "RadioButton": "radio button", "AppCompatRadioButton": "radio button",
    "MaterialRadioButton": "radio button",
    "EditText": "edit box", "AppCompatEditText": "edit box", "TextInputEditText": "edit box",
    "AutoCompleteTextView": "edit box", "MultiAutoCompleteTextView": "edit box",
    "ImageView": "image", "AppCompatImageView": "image", "ShapeableImageView": "image",
    "SeekBar": "slider", "AppCompatSeekBar": "slider", "Slider": "slider",
    "RangeSlider": "slider", "RatingBar": "slider",
    "Spinner": "drop down list", "AppCompatSpinner": "drop down list",
    "NumberPicker": "picker", "WebView": "web view",
}
# Parents whose direct children TalkBack treats as top-level scroll items.
_SCROLL_CONTAINER_CLASSES = {
    "ListView", "GridView", "ExpandableListView", "AbsListView", "RecyclerView",
    "ScrollView", "HorizontalScrollView", "NestedScrollView", "WearableRecyclerView",
}
_PAGER_CLASSES = {"ViewPager", "ViewPager2"}


def _simple_class(n: Dict[str, Any]) -> str:
    cls = n.get("class_name") or ""
    return cls.rsplit(".", 1)[-1].rsplit("$", 1)[-1]


def _flags(n: Dict[str, Any]) -> set:
    return set(n.get("flags") or [])


def _action_ids(n: Dict[str, Any]) -> set:
    return {a.get("id") for a in (n.get("actions") or []) if isinstance(a, dict)}


def _node_int_key(n: Dict[str, Any]) -> Optional[int]:
    k = n.get("id")
    if k is not None:
        return int(k)
    if n.get("host_view_id") is not None:
        return a11y_key(n["host_view_id"], n.get("virtual_id", HOST_VIEW_ID))
    return None


def _node_label_key(n: Dict[str, Any]) -> str:
    k = n.get("node_key")
    if k:
        return k
    ik = _node_int_key(n)
    return f"id:{ik}" if ik is not None else "?"


class _Focus:
    """TalkBack focusability predicates over the ORIGINAL (un-reordered) tree."""

    def __init__(self, roots: List[Dict[str, Any]]):
        self.parent: Dict[int, Optional[Dict[str, Any]]] = {}
        for r in roots:
            self.parent[id(r)] = None
            for n in _iter_nodes([r]):
                for c in n.get("children") or []:
                    self.parent[id(c)] = n
        self._speaking: Dict[int, bool] = {}
        self._focusable: Dict[int, bool] = {}
        self._stop: Dict[int, bool] = {}

    # -- primitive predicates -------------------------------------------------
    @staticmethod
    def visible(n: Dict[str, Any]) -> bool:
        if "InvisibleToUser" in (n.get("extras") or {}):
            return False
        return "visible_to_user" in _flags(n)

    @staticmethod
    def actionable(n: Dict[str, Any]) -> bool:
        if {"clickable", "long_clickable", "focusable", "screen_reader_focusable"} & _flags(n):
            return True
        return bool({_ACTION_FOCUS, _ACTION_CLICK, _ACTION_LONG_CLICK} & _action_ids(n))

    @staticmethod
    def has_text(n: Dict[str, Any]) -> bool:
        return bool(n.get("text") or n.get("content_description"))

    def top_level_scroll_item(self, n: Dict[str, Any]) -> bool:
        parent = self.parent.get(id(n))
        if parent is None:
            return False
        # Role.getRole(parent) in LIST/GRID/SCROLL_VIEW/HORIZONTAL_SCROLL_VIEW; a
        # parent carrying CollectionInfo (RecyclerView, Compose lazy lists) counts
        # as a list.
        if _simple_class(parent) in _SCROLL_CONTAINER_CLASSES or parent.get("collection_info"):
            return True
        grand = self.parent.get(id(parent))
        return grand is not None and _simple_class(grand) in _PAGER_CLASSES

    # -- TalkBack's recursive predicates --------------------------------------
    def speaking(self, n: Dict[str, Any]) -> bool:
        """isSpeakingNode: speaks itself, or has visible non-focusable speaking children."""
        k = id(n)
        if k in self._speaking:
            return self._speaking[k]
        self._speaking[k] = False  # cycle guard
        res = bool(self.has_text(n) or n.get("state_description")
                   or "checkable" in _flags(n))
        if not res:
            for c in n.get("children") or []:
                if self.visible(c) and not self.focusable(c) and self.speaking(c):
                    res = True
                    break
        self._speaking[k] = res
        return res

    def focusable(self, n: Dict[str, Any]) -> bool:
        """isAccessibilityFocusable: visible and actionable, or a speaking top-level scroll item."""
        k = id(n)
        if k in self._focusable:
            return self._focusable[k]
        self._focusable[k] = False  # cycle guard
        res = self.visible(n) and (
            self.actionable(n) or (self.top_level_scroll_item(n) and self.speaking(n)))
        self._focusable[k] = res
        return res

    def has_focusable_ancestor(self, n: Dict[str, Any]) -> bool:
        p = self.parent.get(id(n))
        while p is not None:
            if self.focusable(p):
                return True
            p = self.parent.get(id(p))
        return False

    def is_stop(self, n: Dict[str, Any]) -> bool:
        """shouldFocusNode."""
        k = id(n)
        if k in self._stop:
            return self._stop[k]
        if not self.visible(n):
            res = False
        elif self.focusable(n):
            kids = [c for c in (n.get("children") or []) if self.visible(c)]
            res = (not kids) or self.speaking(n)
        else:
            res = self.has_text(n) and not self.has_focusable_ancestor(n)
        self._stop[k] = res
        return res


def _role_word(n: Dict[str, Any]) -> Optional[str]:
    rd = n.get("role_description")
    if rd:
        return rd
    return _ROLE_BY_CLASS.get(_simple_class(n))


def _state_words(n: Dict[str, Any], role: Optional[str]) -> List[str]:
    fl = _flags(n)
    out: List[str] = []
    sd = n.get("state_description")
    if sd:
        out.append(sd)
    elif "checkable" in fl:
        if n.get("checked_state") == "PARTIAL":
            out.append("partially checked")
        elif role == "switch":
            out.append("on" if "checked" in fl else "off")
        else:
            out.append("checked" if "checked" in fl else "not checked")
    elif n.get("range_info"):
        ri = n["range_info"]
        lo, hi, cur = ri.get("min", 0.0), ri.get("max", 0.0), ri.get("current", 0.0)
        if ri.get("type") == "PERCENT":
            out.append(f"{round(cur)} percent")
        elif hi > lo:
            out.append(f"{round((cur - lo) * 100.0 / (hi - lo))} percent")
    if "selected" in fl:
        out.append("selected")
    exp = n.get("expanded_state")
    if exp == "COLLAPSED":
        out.append("collapsed")
    elif exp in ("FULL", "PARTIAL"):
        out.append("expanded")
    if "heading" in fl:
        out.append("heading")
    actionable = {"clickable", "long_clickable", "checkable", "editable"} & fl
    if actionable and fl and "enabled" not in fl:
        out.append("disabled")
    if n.get("error"):
        out.append(f"error: {n['error']}")
    return out


def _describe(n: Dict[str, Any], focus: _Focus, by_key: Dict[int, Dict[str, Any]],
              is_root: bool, depth: int = 0) -> Tuple[List[str], bool]:
    """TalkBack-style description of ``n``: (parts, has_label)."""
    parts: List[str] = []
    has_label = False
    cd = n.get("content_description")
    fl = _flags(n)
    if cd:
        parts.append(cd)
        has_label = True
    else:
        text = n.get("text") if "password" not in fl else None
        if text:
            parts.append(text)
            has_label = True
        elif "editable" in fl and n.get("hint_text"):
            parts.append(n["hint_text"])
            has_label = True
        if not has_label and n.get("labeled_by"):
            labeler = by_key.get(int(n["labeled_by"]))
            lab = labeler and (labeler.get("content_description") or labeler.get("text"))
            if lab:
                parts.append(lab)
                has_label = True
        if depth < 64:
            for c in n.get("children") or []:
                if not focus.visible(c) or focus.focusable(c):
                    continue
                cparts, clab = _describe(c, focus, by_key, False, depth + 1)
                parts.extend(cparts)
                has_label = has_label or clab
    role = _role_word(n)
    if role:
        parts.append(role)
    parts.extend(_state_words(n, role))
    return parts, has_label


def announcement(n: Dict[str, Any], focus: _Focus,
                 by_key: Dict[int, Dict[str, Any]]) -> Tuple[str, bool]:
    """What TalkBack would say for focus stop ``n``; (text, unlabeled)."""
    parts, has_label = _describe(n, focus, by_key, True)
    parts = [p.strip() for p in parts if p and p.strip()]
    unlabeled = not has_label
    if unlabeled:
        parts.insert(0, "Unlabeled")
    # Collapse immediate duplicates ("Delete, Delete" from a cd echoed by a child).
    dedup: List[str] = []
    for p in parts:
        if not dedup or dedup[-1].casefold() != p.casefold():
            dedup.append(p)
    return ", ".join(dedup), unlabeled


def _find_cycles(edges: List[Tuple[int, int]]) -> List[List[int]]:
    """Strongly connected components with >1 member (or a self-loop), via Tarjan."""
    graph: Dict[int, List[int]] = {}
    for a, b in edges:
        graph.setdefault(a, []).append(b)
        graph.setdefault(b, [])
    index: Dict[int, int] = {}
    low: Dict[int, int] = {}
    on_stack: set = set()
    stack: List[int] = []
    out: List[List[int]] = []
    counter = [0]

    for start in list(graph):
        if start in index:
            continue
        # Iterative Tarjan.
        work = [(start, 0)]
        while work:
            v, i = work.pop()
            if i == 0:
                index[v] = low[v] = counter[0]
                counter[0] += 1
                stack.append(v)
                on_stack.add(v)
            recurse = False
            succ = graph[v]
            while i < len(succ):
                w = succ[i]
                i += 1
                if w not in index:
                    work.append((v, i))
                    work.append((w, 0))
                    recurse = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if recurse:
                continue
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1 or v in graph.get(v, []):
                    out.append(list(reversed(comp)))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
    return out


def reading_order(roots: List[Dict[str, Any]], include_structural: bool = False) -> Dict[str, Any]:
    """Compute the TalkBack reading order over the given window roots.

    Returns ``{"focus_order": [...], "diagnostics": [...], "_nodes": [...]}``:
    ``focus_order`` entries are ``{"order", "key", "id", "speak"}`` (+
    ``"unlabeled": True`` when TalkBack would say "Unlabeled", + ``"window"`` when
    there are several windows); ``_nodes`` are the node dicts behind each entry
    (not JSON; for callers that annotate the tree). With ``include_structural``
    the list also holds non-stop nodes (``order`` None, ``is_focus_stop`` False)
    in walk order, for debugging.
    """
    roots = [r for r in roots if r]
    focus = _Focus(roots)
    parent = focus.parent
    diagnostics: List[Dict[str, Any]] = []

    # ---- 1. index the ANI tree -------------------------------------------------
    preorder: List[Dict[str, Any]] = []
    window_of: Dict[int, int] = {}
    for wi, r in enumerate(roots):
        for node in _iter_nodes([r]):
            preorder.append(node)
            window_of[id(node)] = wi
    by_key: Dict[int, Dict[str, Any]] = {}
    dupes: List[str] = []
    for node in preorder:
        k = _node_int_key(node)
        if k is None:
            continue
        if k in by_key:
            dupes.append(_node_label_key(node))
            continue
        by_key[k] = node
    if dupes:
        diagnostics.append({
            "kind": "duplicate_key", "count": len(dupes), "keys": sorted(set(dupes))[:10],
            "message": (f"{len(dupes)} a11y nodes share a node key with an earlier node; "
                        "linkage to them is ambiguous (the agent's ids are not unique)."),
        })

    def is_ancestor(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
        p = parent.get(id(b))
        while p is not None:
            if p is a:
                return True
            p = parent.get(id(p))
        return False

    # ---- 2. effective constraints ----------------------------------------------
    # TalkBack uses a node's traversal_before when it resolves, else its
    # traversal_after. An edge (pred, succ) means pred is read immediately before
    # succ; Compose states each link twice (prev.before = next, next.after = prev),
    # so an after-edge that repeats a before-edge is dropped.
    edges: Dict[Tuple[int, int], Tuple[Dict[str, Any], str, Dict[str, Any]]] = {}
    unresolved: List[Dict[str, Any]] = []
    linked = 0
    has_before: set = set()
    for field, kind in (("traversal_before", "before"), ("traversal_after", "after")):
        for node in preorder:
            tv = node.get(field)
            if not tv:
                continue
            linked += 1
            if kind == "after" and id(node) in has_before:
                continue
            target = by_key.get(int(tv))
            if target is None:
                # TalkBack's getTraversalBefore() returns null; it falls through to after.
                unresolved.append({"key": _node_label_key(node), "field": field,
                                   "target": int(tv)})
                continue
            if kind == "before":
                has_before.add(id(node))
            if target is node:
                continue
            pred, succ = (node, target) if kind == "before" else (target, node)
            edges.setdefault((id(pred), id(succ)), (node, kind, target))
    if unresolved:
        diag: Dict[str, Any] = {
            "kind": "unresolved_target", "count": len(unresolved), "items": unresolved[:10],
            "message": (f"{len(unresolved)} traversal_before/after targets are not in this dump "
                        "(off-screen or not important for accessibility); TalkBack ignores them."),
        }
        if len(unresolved) == linked and linked >= 2:
            # Nothing resolved at all: the agent's linkage ids are almost certainly not in
            # the node-key space, so the order below may differ from TalkBack's.
            diag["suspect_key_space"] = True
            diag["message"] = (
                f"none of the {linked} traversal_before/after targets resolve to a node in "
                "this dump: the agent's linkage ids do not match the node keys, so this "
                "reading order ignores them and may differ from TalkBack's.")
        diagnostics.append(diag)

    # Cycles among the constraints: ignore them and say so.
    node_by_pyid = {id(node): node for node in preorder}
    scc_of: Dict[int, int] = {}
    for ci, comp in enumerate(_find_cycles(list(edges))):
        for member in comp:
            scc_of[member] = ci
        keys = [_node_label_key(node_by_pyid[i]) for i in comp]
        diagnostics.append({
            "kind": "cycle", "keys": keys,
            "message": ("traversal_before/after constraints form a cycle ("
                        + " -> ".join(keys + keys[:1]) + "); those constraints were ignored, "
                        "so TalkBack's order among them is undefined."),
        })

    def group_unit(m: Dict[str, Any], t: Dict[str, Any]) -> Dict[str, Any]:
        """Outermost traversal-group ancestor of m (else m) that does not contain t."""
        t_anc = set()
        p: Optional[Dict[str, Any]] = t
        while p is not None:
            t_anc.add(id(p))
            p = parent.get(id(p))
        unit = m
        p = parent.get(id(m))
        while p is not None and id(p) not in t_anc:
            if p.get("is_traversal_group") or "is_traversal_group" in _flags(p):
                unit = p
            p = parent.get(id(p))
        return unit

    # ---- 3. placement ------------------------------------------------------------
    # The node carrying the attribute moves; its target stays put. traversal_before=T
    # places the node (with its subtree) immediately before T; traversal_after=T
    # immediately after T's subtree. Constraints compose, so a Compose chain
    # a->b->c is read a, b, c wherever it starts, and an ancestor already precedes
    # its descendants (no move needed).
    before_att: Dict[int, List[Dict[str, Any]]] = {}
    after_att: Dict[int, List[Dict[str, Any]]] = {}
    moved: set = set()
    unsatisfiable: List[Dict[str, Any]] = []
    grouped_moves = 0
    pos = {id(node): i for i, node in enumerate(preorder)}
    for (pk, sk), (holder, kind, target) in sorted(
            edges.items(), key=lambda kv: pos[id(kv[1][0])]):
        if pk in scc_of and scc_of.get(pk) == scc_of.get(sk):
            continue
        if is_ancestor(holder, target):
            if kind == "after":
                unsatisfiable.append({"key": _node_label_key(holder),
                                      "target": _node_label_key(target)})
            continue  # before: pre-order already reads an ancestor first
        unit = group_unit(holder, target)
        if unit is not holder:
            grouped_moves += 1
        if id(unit) in moved:
            continue  # already placed by another constraint (e.g. its group's)
        moved.add(id(unit))
        (before_att if kind == "before" else after_att).setdefault(id(target), []).append(unit)
    if unsatisfiable:
        diagnostics.append({
            "kind": "unsatisfiable", "count": len(unsatisfiable), "items": unsatisfiable[:10],
            "message": ("traversal_after targets a descendant of the node (a node cannot be read "
                        "after its own child); ignored."),
        })
    if grouped_moves:
        diagnostics.append({
            "kind": "traversal_group", "count": grouped_moves,
            "message": (f"{grouped_moves} traversal constraints crossed a traversal-group "
                        "boundary; the enclosing group was moved as a unit."),
        })

    # ---- 4. walk (iterative: Compose chains can be long) -------------------------
    walk: List[Dict[str, Any]] = []
    emitted: set = set()
    active: set = set()
    stack: List[Tuple[str, Dict[str, Any]]] = [
        ("visit", r) for r in reversed(roots) if id(r) not in moved]
    while stack:
        op, node = stack.pop()
        k = id(node)
        if op == "visit":
            if k in emitted or k in active:
                continue
            active.add(k)
            tasks = [("visit", u) for u in before_att.get(k, [])]
            tasks.append(("emit", node))
            tasks += [("visit", c) for c in (node.get("children") or []) if id(c) not in moved]
            tasks += [("visit", u) for u in after_att.get(k, [])]
            tasks.append(("done", node))
            stack.extend(reversed(tasks))
        elif op == "emit":
            emitted.add(k)
            walk.append(node)
        else:
            active.discard(k)
    lost = [node for node in preorder if id(node) not in emitted]
    if lost:
        diagnostics.append({
            "kind": "placement_cycle", "count": len(lost),
            "keys": [_node_label_key(node) for node in lost[:10]],
            "message": ("traversal constraints place nodes inside each other's subtrees; "
                        f"{len(lost)} nodes were appended at the end in tree order."),
        })
        walk.extend(lost)

    # ---- 5. focus stops ------------------------------------------------------------
    multi_window = len(roots) > 1
    entries: List[Dict[str, Any]] = []
    nodes: List[Dict[str, Any]] = []
    counter = 0
    for node in walk:
        stop = focus.is_stop(node)
        if not stop and not include_structural:
            continue
        entry: Dict[str, Any] = {"order": None,
                                 "key": node.get("node_key") or _node_label_key(node),
                                 "id": _node_int_key(node)}
        if stop:
            counter += 1
            entry["order"] = counter
            speak, unlabeled = announcement(node, focus, by_key)
            entry["speak"] = speak
            if unlabeled:
                entry["unlabeled"] = True
        else:
            entry["speak"] = node.get("speakable")
        if include_structural:
            entry["is_focus_stop"] = stop
        if multi_window:
            entry["window"] = window_of.get(id(node), 0)
        entries.append(entry)
        nodes.append(node)
    return {"focus_order": entries, "diagnostics": diagnostics, "_nodes": nodes}


def compute_traversal_order(roots: List[Dict[str, Any]],
                            include_structural: bool = True) -> List[Dict[str, Any]]:
    """The TalkBack reading order as a list (see :func:`reading_order`).

    Kept for callers of the old API: by default it includes structural nodes
    (``is_focus_stop`` False, ``order`` None) so the full walk is inspectable.
    """
    return reading_order(roots, include_structural=include_structural)["focus_order"]
