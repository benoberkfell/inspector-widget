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

import re
from typing import Any, Dict, List, Optional, Tuple

from .proto import view_inspection_pb2 as pb
from .strings import StringResolver, _bounds_to_dict, window_info_to_dict

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
    # R.id-backed standard actions: the android.R.id.accessibilityAction* values,
    # generated from platforms/android-37.0/android.jar (`javap -constants 'android.R$id'`).
    # These ids are stable across releases; a new one only ever gets appended.
    0x01020036: "SHOW_ON_SCREEN",
    0x01020037: "SCROLL_TO_POSITION",
    0x01020038: "SCROLL_UP",
    0x01020039: "SCROLL_LEFT",
    0x0102003A: "SCROLL_DOWN",
    0x0102003B: "SCROLL_RIGHT",
    0x0102003C: "CONTEXT_CLICK",
    0x0102003D: "SET_PROGRESS",
    0x01020042: "MOVE_WINDOW",
    0x01020044: "SHOW_TOOLTIP",
    0x01020045: "HIDE_TOOLTIP",
    0x01020046: "PAGE_UP",
    0x01020047: "PAGE_DOWN",
    0x01020048: "PAGE_LEFT",
    0x01020049: "PAGE_RIGHT",
    0x0102004A: "PRESS_AND_HOLD",
    0x01020054: "IME_ENTER",
    0x01020055: "DRAG_START",
    0x01020056: "DRAG_DROP",
    0x01020057: "DRAG_CANCEL",
    0x01020058: "SHOW_TEXT_SUGGESTIONS",
    0x0102005E: "SCROLL_IN_DIRECTION",
    0x0102005F: "SET_EXTENDED_SELECTION",
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
    from ``virtual:<h>:<v>`` to ``compose:<h>:<v>``.

    A node whose backing View the agent could not resolve (``host_view_id == 0``)
    gets ``node_key`` None: it has no id another tool could look up.
    """
    compose_hosts = set()
    for n in _iter_nodes(roots):
        if int(n.get("virtual_id", HOST_VIEW_ID)) == HOST_VIEW_ID and _is_compose_provider(n):
            compose_hosts.add(int(n.get("host_view_id", 0)))
    for n in _iter_nodes(roots):
        h = int(n.get("host_view_id", 0))
        v = int(n.get("virtual_id", HOST_VIEW_ID))
        n["node_key"] = _typed_key(h, v, compose=h in compose_hosts) if h else None


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
        "node_key": (_typed_key(node.host_view_id, node.virtual_id, compose=False)
                     if node.host_view_id else None),
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

        {"windows": [{"root_view_id", "root": <node>|None,
                      "window_type"?, "window_flags"?, "modal"?, "covered_by"?,
                      "title"?, "layout_title"?, "frame"?, "z"?, "has_window_focus"?,
                      "display_id"?, "insets"?, "obscured"?}, ...],
         "focus_order": [{"order", "key", "id", "speak"}, ...],   # focus stops only
         "generation": "g...",                      # changes when Compose re-mints ids
         "summary": {"windows", "nodes", "focus_stops", "ignored_by_talkback"?},
         "diagnostics"?: str,                       # from the agent
         "reading_order_diagnostics"?: [...]}       # cycles / dangling linkage / covered windows

    Every node carries ``id`` (integer key) and ``node_key`` (typed key); focus
    stops also carry ``order`` so the tree and ``focus_order`` cross-reference
    without the list repeating the tree's data. A real View that TalkBack never
    sees carries ``ignored``: ``"not_important"`` (TalkBack reads its children in
    its place) or ``"hidden"`` (importantForAccessibility=noHideDescendants on it or
    an ancestor: its whole subtree is gone). A window under an open modal window
    (a dialog) carries ``covered_by`` (the modal window's root_view_id); TalkBack
    cannot reach it, so its nodes are not in ``focus_order``. Agents that send a
    ``WindowInfo`` per window add its fields (:func:`strings.window_info_to_dict`:
    the accessibility ``title``, ``frame``, ``z``, ``insets`` ...).
    """
    resolver = StringResolver(response.strings)
    windows: List[Dict[str, Any]] = []
    infos: Dict[int, Tuple[int, int]] = {}
    for w in response.windows:
        root = a11y_node_to_dict(w.root, resolver) if w.HasField("root") else None
        entry: Dict[str, Any] = {"root_view_id": w.root_view_id, "root": root}
        if w.HasField("info"):
            entry.update(window_info_to_dict(w.info, resolver))
            infos[int(w.root_view_id)] = (w.info.window_type, w.info.wm_flags & 0xFFFFFFFF)
        windows.append(entry)
    roots = [w["root"] for w in windows if w["root"]]
    assign_node_keys(roots)
    mark_talkback_ignored(roots)
    covered = apply_window_meta(windows, response.diagnostics or "", infos)

    root_windows = [w for w in windows if w["root"]]
    skip = {i for i, w in enumerate(root_windows) if w.get("covered_by") is not None}
    ro = reading_order(roots, skip=skip)
    by_id = {id(n): n for n in _iter_nodes(roots)}
    for entry, node in zip(ro["focus_order"], ro["_nodes"]):
        by_id[id(node)]["order"] = entry["order"]

    out: Dict[str, Any] = {"windows": windows, "focus_order": ro["focus_order"],
                           "generation": generation(roots)}
    out["summary"] = {
        "windows": len(windows),
        "nodes": len(by_id),
        "focus_stops": len(ro["focus_order"]),
    }
    ignored = sum(1 for n in by_id.values() if n.get("ignored"))
    if ignored:
        out["summary"]["ignored_by_talkback"] = ignored
    unresolved = sum(1 for n in by_id.values() if not n.get("host_view_id"))
    if unresolved:
        out["summary"]["unresolved_nodes"] = unresolved  # host_view_id 0: no node_key
    if response.diagnostics:
        out["diagnostics"] = response.diagnostics
    ro_diags = list(ro["diagnostics"])
    if covered:
        ro_diags.append(covered)
    unordered = _compose_order_unavailable(response.diagnostics or "")
    if unordered:
        ro_diags.append({
            "kind": "compose_order_unknown", "count": unordered,
            "message": (f"{unordered} ComposeView(s) served no traversal linkage (no "
                        "accessibility service ran and the agent could not compute "
                        "Compose's order), so their content is in composition order; "
                        "TalkBack may read it differently (e.g. a Scaffold's top bar first)."),
        })
    if ro_diags:
        out["reading_order_diagnostics"] = ro_diags
    return out


def _compose_order_unavailable(diagnostics: str) -> int:
    """ComposeViews whose traversal order the agent could not produce (its
    ``compose-traversal ... unavailable=N`` tokens, one per window)."""
    return sum(int(m) for m in re.findall(r"compose-traversal[^;]*?unavailable=(\d+)", diagnostics))


# --------------------------------------------------------------------------- #
# Windows: modal dialogs hide the windows below them from accessibility services.
# --------------------------------------------------------------------------- #
FLAG_NOT_FOCUSABLE = 0x00000008
FLAG_NOT_TOUCHABLE = 0x00000010
FLAG_NOT_TOUCH_MODAL = 0x00000020
_WINDOW_TOKEN = re.compile(r"root#(-?\d+) window type=(-?\d+) flags=0x([0-9a-fA-F]+)")


def _window_meta(diagnostics: str) -> Dict[int, Tuple[int, int]]:
    """root_view_id -> (LayoutParams.type, LayoutParams.flags) from the agent's
    ``root#<id> window type=T flags=0xF`` diagnostics tokens."""
    return {int(m.group(1)): (int(m.group(2)), int(m.group(3), 16))
            for m in _WINDOW_TOKEN.finditer(diagnostics or "")}


def is_modal_window(flags: int) -> bool:
    """A window that takes every touch in its task: neither FLAG_NOT_TOUCH_MODAL nor
    FLAG_NOT_FOCUSABLE nor FLAG_NOT_TOUCHABLE (a Dialog, DialogFragment, Compose Dialog,
    or a focusable PopupWindow)."""
    return not flags & (FLAG_NOT_TOUCH_MODAL | FLAG_NOT_FOCUSABLE | FLAG_NOT_TOUCHABLE)


def apply_window_meta(windows: List[Dict[str, Any]], diagnostics: str,
                      infos: Optional[Dict[int, Tuple[int, int]]] = None) -> Optional[Dict[str, Any]]:
    """Annotate ``windows`` (z-ordered, bottom first) with their type/flags/modality and
    mark the ones below the topmost modal window ``covered_by`` it.

    Type and flags come from ``infos`` (root_view_id -> (type, flags), the agent's
    ``WindowInfo``) and otherwise from the ``root#<id> window ...`` diagnostics tokens.

    The system reports no window below a modal window of the same task to accessibility
    services (AccessibilityWindowManager: a modal window's touchable region is the whole
    task, so everything under it counts as covered), so TalkBack cannot reach the activity
    while a dialog is open. Returns a reading-order diagnostic when a window is covered.
    """
    meta = _window_meta(diagnostics)
    meta.update(infos or {})
    top_modal = None
    for i, w in enumerate(windows):
        m = meta.get(int(w.get("root_view_id") or 0))
        if m is None or not w.get("root"):
            continue
        w["window_type"], w["window_flags"] = m[0], "0x%x" % m[1]
        w["modal"] = is_modal_window(m[1])
        if w["modal"] and i > 0:
            top_modal = i
    if top_modal is None:
        return None
    by = int(windows[top_modal].get("root_view_id") or 0)
    covered = []
    for w in windows[:top_modal]:
        if w.get("root"):
            w["covered_by"] = by
            covered.append(int(w.get("root_view_id") or 0))
    if not covered:
        return None
    return {
        "kind": "covered_windows", "windows": covered, "modal_window": by,
        "message": (f"{len(covered)} window(s) lie under the modal window {by} (a dialog or a "
                    "focusable popup); TalkBack cannot reach them while it is open, so their "
                    "nodes have no reading order. The lint still checks them."),
    }


# --------------------------------------------------------------------------- #
# What TalkBack sees: not-important Views.
#
# TalkBack does not request FLAG_INCLUDE_NOT_IMPORTANT_VIEWS, so the platform leaves every
# View that is not important for accessibility out of the tree it serves and lists that
# View's children in its place (ViewGroup.addChildrenForAccessibility); a View with
# importantForAccessibility=noHideDescendants takes its whole subtree with it. The agent's
# in-process connection fetches not-important Views too, so the host drops them here.
# --------------------------------------------------------------------------- #
_NOT_IMPORTANT = {"NO", "NO_HIDE_DESCENDANTS", 2, 4}
_HIDE_DESCENDANTS = {"NO_HIDE_DESCENDANTS", 4}
_ITEM_PARENTS = {"RecyclerView", "WearableRecyclerView"}


def view_is_important(n: Dict[str, Any]) -> bool:
    """View.isImportantForAccessibility() of a real View node, from its own mode.

    The agent reports AUTO only for a View that resolved to not important (AUTO that
    resolves important is reported as YES). An older agent or a hand-built dict reports
    the raw mode, so AUTO is resolved here with View's rules on what the node exposes:
    actionable (clickable, long-clickable, context-clickable, focusable), a provider,
    a live region, a pane title, a heading; TextView and setContentDescription promote
    AUTO to YES, so text or a contentDescription counts too.
    """
    mode = n.get("important_for_accessibility")
    if mode in _NOT_IMPORTANT:
        return False
    if mode in ("YES", 1):
        return True
    fl = _flags(n)
    if {"clickable", "long_clickable", "context_clickable", "focusable", "heading"} & fl:
        return True
    if n.get("provider_class") or n.get("pane_title"):
        return True
    if n.get("live_region") not in (None, "NONE", 0):
        return True
    return bool(n.get("text") or n.get("content_description"))


def talkback_exclusion(n: Dict[str, Any], raw_parent: Optional[Dict[str, Any]]) -> Optional[str]:
    """Why TalkBack does not see node ``n`` (given its parent in the dump), else None.

    ``"hidden"``: a real View with importantForAccessibility=noHideDescendants; its
    subtree goes with it. ``"not_important"``: a real View that is not important for
    accessibility; its children take its place. Never for virtual nodes, window roots,
    Views a provider added as children (Compose's AndroidView holders) or a
    RecyclerView's item Views (RecyclerView marks them important whenever a service
    is on).
    """
    if raw_parent is None or int(n.get("virtual_id", HOST_VIEW_ID)) != HOST_VIEW_ID:
        return None
    if n.get("important_for_accessibility") in _HIDE_DESCENDANTS:
        return "hidden"
    if int(raw_parent.get("virtual_id", HOST_VIEW_ID)) != HOST_VIEW_ID:
        return None
    if _simple_class(raw_parent) in _ITEM_PARENTS:
        return None
    return None if view_is_important(n) else "not_important"


def mark_talkback_ignored(roots: List[Dict[str, Any]]) -> None:
    """Set ``ignored`` ("not_important" / "hidden") on the nodes TalkBack never sees."""
    stack: List[Tuple[Dict[str, Any], Optional[Dict[str, Any]], bool]] = [
        (r, None, False) for r in reversed([r for r in roots if r])]
    while stack:
        n, parent, hidden = stack.pop()
        why = "hidden" if hidden else talkback_exclusion(n, parent)
        if why:
            n["ignored"] = why
        else:
            n.pop("ignored", None)
        for c in reversed(n.get("children") or []):
            stack.append((c, n, why == "hidden"))


def generation(roots: List[Dict[str, Any]]) -> str:
    """Fingerprint of the Compose ids in an a11y dump; changes when Compose re-mints
    semantics ids (recomposition, recycling, navigation). The same UI state gives the
    same value in dump_accessibility, a11y_lint and inspect."""
    import hashlib
    per_acv: Dict[int, List[int]] = {}
    views: List[int] = []
    for n in _iter_nodes(roots):
        h = int(n.get("host_view_id") or 0)
        v = int(n.get("virtual_id", HOST_VIEW_ID))
        if v == HOST_VIEW_ID:
            views.append(h)
        elif (n.get("node_key") or "").startswith("compose:") and 0 <= v < 1_000_000_000:
            per_acv.setdefault(h, []).append(v)
    h = hashlib.sha1()
    if per_acv:
        for acv in sorted(per_acv):
            h.update(f"{acv}:{sorted(per_acv[acv])};".encode())
    else:
        h.update(str(sorted(views)).encode())
    return "g" + h.hexdigest()[:10]


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
    """TalkBack focusability predicates over the tree TalkBack sees (un-reordered).

    That tree is the dump minus the Views TalkBack never gets (see
    :func:`talkback_exclusion`): a not-important View is replaced by its children, a
    noHideDescendants View is dropped with its subtree. ``kids``/``parent`` give it.
    """

    def __init__(self, roots: List[Dict[str, Any]]):
        self.parent: Dict[int, Optional[Dict[str, Any]]] = {}
        self._kids: Dict[int, List[Dict[str, Any]]] = {}
        for r in roots:
            self.parent[id(r)] = None
            stack = [r]
            while stack:
                n = stack.pop()
                kept: List[Dict[str, Any]] = []
                todo = [(c, n) for c in reversed(n.get("children") or [])]
                while todo:
                    c, raw_parent = todo.pop()
                    why = talkback_exclusion(c, raw_parent)
                    if why == "hidden":
                        continue
                    if why == "not_important":
                        todo.extend((g, c) for g in reversed(c.get("children") or []))
                        continue
                    kept.append(c)
                self._kids[id(n)] = kept
                for c in kept:
                    self.parent[id(c)] = n
                stack.extend(reversed(kept))
        self._speaking: Dict[int, bool] = {}
        self._focusable: Dict[int, bool] = {}
        self._stop: Dict[int, bool] = {}

    def kids(self, n: Dict[str, Any]) -> List[Dict[str, Any]]:
        """``n``'s children as TalkBack gets them (not-important Views hoisted through)."""
        return self._kids.get(id(n), [])

    def seen(self, n: Dict[str, Any]) -> bool:
        return id(n) in self.parent

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
            for c in self.kids(n):
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
            kids = [c for c in self.kids(n) if self.visible(c)]
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
    if {"clickable", "long_clickable", "checkable", "editable"} & fl and "enabled" not in fl:
        out.append("disabled")
    if n.get("error"):
        out.append(f"error: {n['error']}")
    return out


def _describe(n: Dict[str, Any], focus: _Focus, by_key: Dict[int, Dict[str, Any]],
              depth: int = 0) -> Tuple[List[str], bool]:
    """TalkBack-style description of ``n``: (parts, has_label).

    Like TalkBack's compositor (description_for_tree_nodes), every visible,
    non-focusable descendant TalkBack sees adds its own text, role and state; Views
    TalkBack never sees (not important for accessibility) add nothing.
    """
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
            for c in focus.kids(n):
                if not focus.visible(c) or focus.focusable(c):
                    continue
                cparts, clab = _describe(c, focus, by_key, depth + 1)
                parts.extend(cparts)
                has_label = has_label or clab
    role = _role_word(n)
    if role:
        parts.append(role)
    parts.extend(_state_words(n, role))
    return parts, has_label


def announcement(n: Dict[str, Any], focus: _Focus,
                 by_key: Dict[int, Dict[str, Any]]) -> Tuple[str, bool]:
    """What TalkBack would say for focus stop ``n``; (text, unlabeled).

    An approximation of TalkBack's default verbosity: label parts, then role, then
    state. ``unlabeled`` means no text/contentDescription/hint/labeled-by reached the
    announcement (TalkBack says "Unlabeled" or only the role/state).
    """
    parts, has_label = _describe(n, focus, by_key)
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


def reading_order(roots: List[Dict[str, Any]], include_structural: bool = False,
                  skip: Optional[set] = None) -> Dict[str, Any]:
    """Compute the TalkBack reading order over the given window roots.

    ``skip`` holds indices of ``roots`` TalkBack cannot reach (windows under an open
    modal dialog); their nodes get no order. Views TalkBack never sees (not important
    for accessibility, or hidden by noHideDescendants) are left out and their children
    read in their place.

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
    skip = set(skip or ())
    preorder: List[Dict[str, Any]] = []
    window_of: Dict[int, int] = {}
    for wi, r in enumerate(roots):
        if wi in skip:
            continue
        stack = [r]
        while stack:
            node = stack.pop()
            preorder.append(node)
            window_of[id(node)] = wi
            stack.extend(reversed(focus.kids(node)))
    by_key: Dict[int, Dict[str, Any]] = {}
    dupes: List[str] = []
    for node in preorder:
        k = _node_int_key(node)
        if k is None or node.get("host_view_id") == 0:
            continue  # host 0 = the agent could not resolve the View: not addressable
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
        ("visit", r) for wi, r in reversed(list(enumerate(roots)))
        if id(r) not in moved and wi not in skip]
    while stack:
        op, node = stack.pop()
        k = id(node)
        if op == "visit":
            if k in emitted or k in active:
                continue
            active.add(k)
            tasks = [("visit", u) for u in before_att.get(k, [])]
            tasks.append(("emit", node))
            tasks += [("visit", c) for c in focus.kids(node) if id(c) not in moved]
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
        key = node.get("node_key")
        if key is None and "node_key" not in node:
            key = _node_label_key(node)  # hand-built dicts without typed keys
        entry: Dict[str, Any] = {"order": None, "key": key, "id": _node_int_key(node)}
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


# --------------------------------------------------------------------------- #
# Accessibility focus and the agent's event tap (A11yFocusCommand / A11yActCommand).
# --------------------------------------------------------------------------- #
#: AccessibilityEvent.TYPE_* -> name (without the TYPE_ prefix).
EVENT_TYPE_NAMES: Dict[int, str] = {
    0x00000001: "VIEW_CLICKED",
    0x00000002: "VIEW_LONG_CLICKED",
    0x00000004: "VIEW_SELECTED",
    0x00000008: "VIEW_FOCUSED",
    0x00000010: "VIEW_TEXT_CHANGED",
    0x00000020: "WINDOW_STATE_CHANGED",
    0x00000040: "NOTIFICATION_STATE_CHANGED",
    0x00000080: "VIEW_HOVER_ENTER",
    0x00000100: "VIEW_HOVER_EXIT",
    0x00000200: "TOUCH_EXPLORATION_GESTURE_START",
    0x00000400: "TOUCH_EXPLORATION_GESTURE_END",
    0x00000800: "WINDOW_CONTENT_CHANGED",
    0x00001000: "VIEW_SCROLLED",
    0x00002000: "VIEW_TEXT_SELECTION_CHANGED",
    0x00004000: "ANNOUNCEMENT",
    0x00008000: "VIEW_ACCESSIBILITY_FOCUSED",
    0x00010000: "VIEW_ACCESSIBILITY_FOCUS_CLEARED",
    0x00020000: "VIEW_TEXT_TRAVERSED_AT_MOVEMENT_GRANULARITY",
    0x00040000: "GESTURE_DETECTION_START",
    0x00080000: "GESTURE_DETECTION_END",
    0x00100000: "TOUCH_INTERACTION_START",
    0x00200000: "TOUCH_INTERACTION_END",
    0x00400000: "WINDOWS_CHANGED",
    0x00800000: "VIEW_CONTEXT_CLICKED",
    0x01000000: "ASSIST_READING_CONTEXT",
    0x02000000: "SPEECH_STATE_CHANGE",
    0x04000000: "VIEW_TARGETED_BY_SCROLL",
}

#: AccessibilityEvent.CONTENT_CHANGE_TYPE_* bits -> name.
CONTENT_CHANGE_NAMES: Dict[int, str] = {
    0x00000001: "SUBTREE",
    0x00000002: "TEXT",
    0x00000004: "CONTENT_DESCRIPTION",
    0x00000008: "PANE_TITLE",
    0x00000010: "PANE_APPEARED",
    0x00000020: "PANE_DISAPPEARED",
    0x00000040: "STATE_DESCRIPTION",
    0x00000080: "DRAG_STARTED",
    0x00000100: "DRAG_DROPPED",
    0x00000200: "DRAG_CANCELLED",
    0x00000400: "CONTENT_INVALID",
    0x00000800: "ERROR",
    0x00001000: "ENABLED",
    0x00002000: "CHECKED",
    0x00004000: "EXPANDED",
    0x00008000: "SUPPLEMENTAL_DESCRIPTION",
}

EVENT_ACCESSIBILITY_FOCUSED = 0x00008000


def event_type_name(event_type: int) -> str:
    return EVENT_TYPE_NAMES.get(event_type, "0x%x" % event_type)


def _node_ref(host_view_id: int, virtual_id: int, host_class: Optional[str]) -> Dict[str, Any]:
    """``host_view_id``/``virtual_id`` plus the integer ``id`` and the typed ``node_key``
    (``compose:`` for a virtual node whose host View is a ComposeView, as in a dump)."""
    out: Dict[str, Any] = {"host_view_id": host_view_id, "virtual_id": virtual_id,
                           "id": a11y_key(host_view_id, virtual_id)}
    compose = any(h in (host_class or "").lower() for h in _COMPOSE_HINTS)
    out["node_key"] = _typed_key(host_view_id, virtual_id, compose=compose) if host_view_id else None
    return out


def a11y_event_to_dict(ev: "pb.A11yEventRecord", resolver: StringResolver) -> Dict[str, Any]:
    """One event-tap record: ``seq``, ``uptime_ms``, ``type`` (name), ``root_view_id``, the
    source's ``host_view_id``/``virtual_id``/``id``/``node_key``, and whichever of
    ``content_changes`` (names), scroll fields, ``text``, ``pane_title``, ``class_name``,
    ``action`` the event carries (zero / empty ones are left out)."""
    out: Dict[str, Any] = {"seq": ev.seq, "uptime_ms": ev.uptime_ms,
                           "type": event_type_name(ev.type), "root_view_id": ev.root_view_id}
    out.update(_node_ref(ev.host_view_id, ev.virtual_id, resolver.opt(ev.host_class)))
    if ev.content_change_types:
        out["content_changes"] = [name for bit, name in CONTENT_CHANGE_NAMES.items()
                                  if ev.content_change_types & bit] or [ev.content_change_types]
    for field in ("scroll_delta_x", "scroll_delta_y", "from_index", "to_index", "item_count",
                  "scroll_x", "scroll_y", "max_scroll_x", "max_scroll_y", "action"):
        v = getattr(ev, field)
        if v:
            out[field] = v
    for field in ("text", "pane_title", "class_name"):
        v = resolver.opt(getattr(ev, field))
        if v is not None:
            out[field] = v
    return out


def a11y_focus_node_to_dict(focus: "pb.A11yFocus", resolver: StringResolver) -> Dict[str, Any]:
    """One ``A11yFocus``: the node reference (``host_view_id``, ``virtual_id``, ``id``,
    ``node_key``), ``root_view_id``, ``bounds``, ``stale``, ``source``, ``window`` (see
    :func:`strings.window_info_to_dict`) and ``node`` (the shaped node, children to the
    requested depth). A11yFocus nodes carry no ``is_traversal_group`` / ``layout_size``
    (take those from a dump)."""
    host_class = resolver.opt(focus.host_class)
    ref = _node_ref(focus.host_view_id, focus.virtual_id, host_class)
    node = a11y_node_to_dict(focus.node, resolver) if focus.HasField("node") else None
    if node is not None:
        assign_node_keys([node])
        # The subtree lacks its host's own node, so key its virtual nodes like the focus.
        prefix = "compose:" if str(ref["node_key"] or "").startswith("compose:") else None
        for n in _iter_nodes([node]):
            if prefix and str(n.get("node_key") or "").startswith("virtual:"):
                n["node_key"] = prefix + n["node_key"][len("virtual:"):]
    out: Dict[str, Any] = {"root_view_id": focus.root_view_id}
    out.update(ref)
    if host_class:
        out["host_class"] = host_class
    out["bounds"] = _bounds_to_dict(focus.bounds)
    out["stale"] = focus.stale
    if focus.source:
        out["source"] = focus.source
    if focus.HasField("window"):
        out["window"] = window_info_to_dict(focus.window, resolver)
    out["node"] = node
    return out


def a11y_focus_to_dict(response: "pb.A11yFocusResponse") -> Dict[str, Any]:
    """Shape an ``A11yFocusResponse``::

        {"a11y": <focus>|None,        # None = no accessibility focus in the app's windows
         "input"?: <focus>|None,      # when include_input_focus was set
         "seq": int,                  # pass as the next after_seq
         "focus_event": bool, "timed_out": bool, "waited_ms": int, "read_us": int,
         "read_uptime_ms": int,       # device uptime at the read (events carry uptime_ms)
         "touch_exploration": bool, "services_enabled": bool,
         "events": [<event>, ...],    # seq > after_seq, oldest first
         "dropped"?: int, "diagnostics"?: str}

    Each ``<focus>`` is :func:`a11y_focus_node_to_dict`, each ``<event>``
    :func:`a11y_event_to_dict`. Node keys match :func:`a11y_to_dict`'s, so a focus can be
    looked up in a dump by ``id`` / ``node_key`` (while the dump's ids are current).
    """
    resolver = StringResolver(response.strings)
    out: Dict[str, Any] = {
        "a11y": a11y_focus_node_to_dict(response.a11y, resolver) if response.HasField("a11y") else None,
    }
    if response.HasField("input"):
        out["input"] = a11y_focus_node_to_dict(response.input, resolver)
    out.update({
        "seq": response.seq,
        "focus_event": response.focus_event,
        "timed_out": response.timed_out,
        "waited_ms": response.waited_ms,
        "read_us": response.read_us,
        "read_uptime_ms": response.read_uptime_ms,
        "touch_exploration": response.touch_exploration,
        "services_enabled": response.services_enabled,
        "events": [a11y_event_to_dict(e, resolver) for e in response.events],
    })
    if response.dropped:
        out["dropped"] = response.dropped
    if response.diagnostics:
        out["diagnostics"] = response.diagnostics
    return out


def a11y_act_to_dict(response: "pb.A11yActResponse") -> Dict[str, Any]:
    """Shape an ``A11yActResponse``: ``performed``, ``error``?, ``action_id``,
    ``seq_before`` (long-poll A11yFocus from here to see what the action caused), ``seq``,
    ``after`` (the accessibility focus right after, or None) and ``diagnostics``?."""
    resolver = StringResolver(response.strings)
    out: Dict[str, Any] = {"performed": response.performed}
    if response.error:
        out["error"] = response.error
    out.update({
        "action_id": response.action_id,
        "action": action_name(response.action_id) if response.action_id else None,
        "seq_before": response.seq_before,
        "seq": response.seq,
        "after": (a11y_focus_node_to_dict(response.after, resolver)
                  if response.HasField("after") else None),
    })
    if response.diagnostics:
        out["diagnostics"] = response.diagnostics
    return out
