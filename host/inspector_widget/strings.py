"""String-table resolution and tree-to-dict conversion for agent consumption.

All "string-table id" fields in the protocol are int32 indices into a
``Strings`` message (id 0 == empty/absent). These helpers resolve those ids and
flatten a ``DumpTreeResponse`` / ``PropertyGroup`` into a plain nested dict that
serialises cleanly to JSON.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .proto import view_inspection_pb2 as pb

# Reverse lookup tables for enum int -> readable label.
_PROPERTY_TYPE_NAMES = {v: k for k, v in pb.Property.Type.items()}
_RESPONSE_STATUS_NAMES = {v: k for k, v in pb.Response.Status.items()}


class StringResolver:
    """Resolves interned string ids against a ``Strings`` message."""

    def __init__(self, strings: "pb.Strings"):
        # id 0 is reserved as empty/absent.
        self._table: Dict[int, str] = {0: ""}
        for entry in strings.entries:
            self._table[entry.id] = entry.str

    def get(self, sid: int) -> str:
        """Resolve a string id to text. Unknown ids resolve to ""."""
        return self._table.get(sid, "")

    def opt(self, sid: int) -> Optional[str]:
        """Resolve a string id, returning None for absent (id 0 / empty)."""
        if sid == 0:
            return None
        text = self._table.get(sid)
        return text if text else None


def _resource_to_dict(resolver: StringResolver, res: "pb.Resource") -> Optional[Dict[str, Any]]:
    """Convert a Resource into {namespace, type, name} or None if empty."""
    type_ = resolver.opt(res.type)
    name = resolver.opt(res.name)
    namespace = resolver.opt(res.namespace)
    if type_ is None and name is None and namespace is None:
        return None
    out: Dict[str, Any] = {}
    if namespace is not None:
        out["namespace"] = namespace
    if type_ is not None:
        out["type"] = type_
    if name is not None:
        out["name"] = name
    return out


def _rect_to_dict(rect: "pb.Rect") -> Dict[str, int]:
    return {"x": rect.x, "y": rect.y, "w": rect.w, "h": rect.h}


def _quad_to_dict(quad: "pb.Quad") -> Dict[str, int]:
    return {
        "x0": quad.x0, "y0": quad.y0,
        "x1": quad.x1, "y1": quad.y1,
        "x2": quad.x2, "y2": quad.y2,
        "x3": quad.x3, "y3": quad.y3,
    }


def _bounds_to_dict(bounds: "pb.Bounds") -> Dict[str, Any]:
    out: Dict[str, Any] = {"layout": _rect_to_dict(bounds.layout)}
    # render Quad is present only for transformed views.
    if bounds.HasField("render"):
        out["render"] = _quad_to_dict(bounds.render)
    return out


def property_to_dict(resolver: StringResolver, prop: "pb.Property") -> Dict[str, Any]:
    """Convert a single Property to a readable dict, picking the right value field."""
    type_name = _PROPERTY_TYPE_NAMES.get(prop.type, str(prop.type))
    out: Dict[str, Any] = {
        "name": resolver.get(prop.name),
        "type": type_name,
        "is_layout": prop.is_layout,
    }

    t = prop.type
    P = pb.Property
    # Pick the populated value representation per type.
    if t in (P.STRING, P.OBJECT, P.INT_ENUM):
        out["value"] = resolver.opt(prop.str_value)
    elif t == P.BOOLEAN:
        out["value"] = bool(prop.int32_value)
    elif t in (P.GRAVITY, P.INT_FLAG):
        # The agent sends the decoded flag set as one "|"-joined string in
        # str_value (Properties.kt; id 0 = the empty set) and leaves int32_value 0.
        # A raw int32 is only kept when there is no string at all (never sent by
        # the payload in this repo, but not worth losing).
        flags = resolver.opt(prop.str_value)
        out["value"] = prop.int32_value if flags is None and prop.int32_value else flags or ""
    elif t in (P.BYTE, P.CHAR, P.INT16, P.INT32, P.COLOR, P.DIMENSION):
        out["value"] = prop.int32_value
        # For flag/enum-like ints a decoded label may also be interned.
        label = resolver.opt(prop.str_value)
        if label is not None:
            out["label"] = label
    elif t == P.INT64:
        out["value"] = prop.int64_value
    elif t == P.DOUBLE:
        out["value"] = prop.double_value
    elif t == P.FLOAT:
        out["value"] = prop.float_value
    elif t in (P.RESOURCE, P.DRAWABLE, P.ANIM, P.ANIMATOR, P.INTERPOLATOR):
        res = _resource_to_dict(resolver, prop.resource_value)
        if res is not None:
            out["value"] = res
        else:
            label = resolver.opt(prop.str_value)
            if label is not None:
                out["value"] = label
    else:
        # Fallback: surface whatever scalar is non-default.
        out["value"] = resolver.opt(prop.str_value) or prop.int32_value

    source = resolver.opt(prop.source)
    if source is not None:
        out["source"] = source
    if prop.resolution_stack:
        out["resolution_stack"] = [
            resolver.get(sid) for sid in prop.resolution_stack
        ]
    return out


def property_group_to_dict(
    group: "pb.PropertyGroup", resolver: StringResolver
) -> Dict[str, Any]:
    return {
        "view_id": group.view_id,
        "properties": [property_to_dict(resolver, p) for p in group.properties],
    }


def node_to_dict(node: "pb.ViewNode", resolver: StringResolver) -> Dict[str, Any]:
    """Convert a ViewNode (recursively) to a friendly nested dict."""
    class_name = resolver.get(node.class_name)
    package_name = resolver.opt(node.package_name)
    qualified = f"{package_name}.{class_name}" if package_name else class_name

    out: Dict[str, Any] = {
        "id": node.id,
        "class_name": class_name,
        "qualified_name": qualified,
        "bounds": _bounds_to_dict(node.bounds),
    }
    if package_name is not None:
        out["package_name"] = package_name

    resource = _resource_to_dict(resolver, node.resource)
    if resource is not None:
        out["resource"] = resource
    layout_resource = _resource_to_dict(resolver, node.layout_resource)
    if layout_resource is not None:
        out["layout_resource"] = layout_resource

    view_id_name = resolver.opt(node.view_id_name)
    if view_id_name is not None:
        out["view_id_name"] = view_id_name
    text = resolver.opt(node.text_value)
    if text is not None:
        out["text"] = text

    flags: List[str] = []
    if node.flags & pb.ViewNode.IS_WEBVIEW:
        flags.append("IS_WEBVIEW")
    if flags:
        out["flags"] = flags

    if node.children:
        out["children"] = [node_to_dict(c, resolver) for c in node.children]
    return out


def dump_tree_to_dict(response: "pb.DumpTreeResponse") -> Dict[str, Any]:
    """Convert a full DumpTreeResponse to a JSON-friendly dict.

    Resolves all interned ids; inlines properties keyed by view id when present;
    summarises the screenshot (without the raw bytes).
    """
    resolver = StringResolver(response.strings)
    out: Dict[str, Any] = {
        "roots": [node_to_dict(root, resolver) for root in response.roots],
    }
    if response.properties:
        out["properties"] = {
            group.view_id: [
                property_to_dict(resolver, p) for p in group.properties
            ]
            for group in response.properties
        }
    if response.HasField("screenshot"):
        s = response.screenshot
        out["screenshot"] = {
            "width": s.width,
            "height": s.height,
            "bitmap_type": s.bitmap_type,
            "scale": s.scale,
            "compressed_bytes": len(s.data),
        }
    return out


def get_properties_to_dict(response: "pb.GetPropertiesResponse") -> Dict[str, Any]:
    resolver = StringResolver(response.strings)
    return property_group_to_dict(response.group, resolver)


def get_windows_to_dict(response: "pb.GetWindowsResponse") -> Dict[str, Any]:
    return {"root_ids": list(response.root_ids)}


_INSET_TYPES = ("status_bars", "navigation_bars", "ime", "display_cutout")


def _inset_rects(frame: Optional[Dict[str, int]], i: "pb.WindowInsetsInfo") -> List[Dict[str, int]]:
    """The screen-px strips an inset covers along each edge of ``frame``."""
    if not frame:
        return []
    x, y, w, h = frame["x"], frame["y"], frame["w"], frame["h"]
    out = []
    if i.top > 0:
        out.append({"x": x, "y": y, "w": w, "h": i.top})
    if i.bottom > 0:
        out.append({"x": x, "y": y + h - i.bottom, "w": w, "h": i.bottom})
    if i.left > 0:
        out.append({"x": x, "y": y, "w": i.left, "h": h})
    if i.right > 0:
        out.append({"x": x + w - i.right, "y": y, "w": i.right, "h": h})
    return out


# Insets drawn by windows above the app: where it is not interactive while they show.
_OBSCURING_INSETS = ("status_bars", "navigation_bars", "ime")


def window_info_to_dict(info: "pb.WindowInfo", resolver: StringResolver) -> Dict[str, Any]:
    """A ``WindowInfo`` as a flat dict: ``title``? (what accessibility services call the
    window), ``layout_title``?, ``window_type``, ``window_flags`` ("0x..."), ``frame``
    (screen px), ``z`` (0 = bottom), ``has_window_focus``, ``display_id``, ``insets``?:
    ``{status_bars|navigation_bars|ime|display_cutout: {left, top, right, bottom, visible,
    rects}}`` (amounts in px within the window, ``rects`` the strips they cover in screen
    px; only the types the agent reported, API 30+), and ``obscured``?: the screen-px rects
    of the visible status bar, navigation bar and IME, which sit in windows above this one
    (a node wholly inside them is outside the window's interactive region)."""
    out: Dict[str, Any] = {}
    title = resolver.opt(info.title)
    if title is not None:
        out["title"] = title
    layout_title = resolver.opt(info.layout_title)
    if layout_title is not None:
        out["layout_title"] = layout_title
    out["window_type"] = info.window_type
    out["window_flags"] = "0x%x" % (info.wm_flags & 0xFFFFFFFF)
    if info.HasField("frame"):
        out["frame"] = _rect_to_dict(info.frame.layout)
    out["z"] = info.z
    out["has_window_focus"] = info.has_window_focus
    out["display_id"] = info.display_id
    insets = {}
    obscured = []
    for name in _INSET_TYPES:
        if info.HasField(name):
            i = getattr(info, name)
            rects = _inset_rects(out.get("frame"), i)
            insets[name] = {"left": i.left, "top": i.top, "right": i.right, "bottom": i.bottom,
                            "visible": i.visible, "rects": rects}
            if i.visible and name in _OBSCURING_INSETS:
                obscured.extend(rects)
    if insets:
        out["insets"] = insets
    if obscured:
        out["obscured"] = obscured
    return out


# --------------------------------------------------------------------------- #
# Compose
# --------------------------------------------------------------------------- #
def compose_node_to_dict(node: "pb.ComposeNode", resolver: StringResolver) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id": node.id,
        "name": resolver.get(node.name),
        "kind": "SEMANTICS" if node.kind == pb.ComposeNode.SEMANTICS else "COMPOSABLE",
    }
    if node.render_node_id:
        out["render_node_id"] = node.render_node_id
    bounds = _bounds_to_dict(node.bounds)
    if bounds:
        out["bounds"] = bounds
    src = resolver.opt(node.source)
    if src is not None:
        out["source"] = src
    if node.attrs:
        out["attrs"] = {resolver.get(a.key): resolver.get(a.value) for a in node.attrs}
    if node.children:
        out["children"] = [compose_node_to_dict(c, resolver) for c in node.children]
    return out


#: Why ``enable_inspection`` is opt-in. Shared by the CLI and MCP so both warn identically;
#: ``%s`` is the surface's spelling of the flag (``--enable-inspection`` / ``enable_inspection=true``).
ENABLE_INSPECTION_WARNING = (
    "%s hot-reloads every composition in the app process, which resets plain remember{} state (open dialogs, text input, scroll position, toggles). Only use it when you need slot-table detail "
    "(composable names, parameters, file:line), and capture any state you care about first."
)


def compose_slot_table_populated(data: Dict[str, Any]) -> bool:
    """True if any window in a ``dump_compose_to_dict`` result carries slot-table
    (COMPOSABLE) nodes below its synthetic window root."""
    def walk(node: Dict[str, Any]) -> bool:
        for child in node.get("children") or []:
            if child.get("kind") == "COMPOSABLE" or walk(child):
                return True
        return False
    return any(walk(w["root"]) for w in data.get("windows", []) if w.get("root"))


def dump_compose_to_dict(response: "pb.DumpComposeResponse") -> Dict[str, Any]:
    resolver = StringResolver(response.strings)
    out: Dict[str, Any] = {
        "windows": [
            {
                "view_id": w.view_id,
                "root": compose_node_to_dict(w.root, resolver) if w.HasField("root") else None,
            }
            for w in response.windows
        ],
    }
    if response.diagnostics:
        out["diagnostics"] = response.diagnostics
    return out
