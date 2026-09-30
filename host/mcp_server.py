#!/usr/bin/env python3
# Inspector Widget — MCP server.
#
# Exposes the Inspector Widget Android View Layout Inspector to an LLM agent over the
# Model Context Protocol (stdio transport).  It is a thin orchestration layer on
# top of the `inspector_widget` package (the Python host driver that pushes the
# native agent + dex/jar, attaches via `cmd activity attach-agent`, forwards the
# abstract socket and speaks the framed protobuf protocol from
# `proto/view_inspection.proto`).
#
# Two execution modes, selected at runtime:
#   1. The real `mcp` SDK (preferred — `pip install mcp`).  We register seven
#      tools via the low-level `mcp.server.Server` API and serve over stdio.
#   2. A self-contained JSON-RPC 2.0 / MCP-over-stdio fallback used when the
#      `mcp` package is not importable, so the server still runs with only
#      stdlib + protobuf present.  The fallback implements just enough of the
#      MCP handshake (`initialize`, `tools/list`, `tools/call`,
#      `notifications/initialized`) to drive the same tool surface.
#
# The host driver is the single source of truth for adb/injection/transport.
# This module owns only: tool schemas, argument validation, per-(serial,package)
# session caching, screenshot temp-file materialisation, and JSON shaping of the
# protobuf responses (string-table ids resolved to text) for the agent.
#
# See host/README.md for build + run + `claude mcp add` instructions.

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
import threading
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Make the host package importable when this file is launched directly
# (`python host/mcp_server.py`).  host/ is the parent dir of this file and it
# contains the `inspector_widget` package.
# --------------------------------------------------------------------------- #
_HOST_DIR = os.path.dirname(os.path.abspath(__file__))
if _HOST_DIR not in sys.path:
    sys.path.insert(0, _HOST_DIR)

log = logging.getLogger("inspector-widget.mcp")


# --------------------------------------------------------------------------- #
# Host-driver facade.
#
# We depend on the public surface of `inspector_widget` (built as a sibling
# module).  The facade below isolates the exact import shape in ONE place and
# adapts a couple of reasonable naming variants so the MCP layer stays stable
# even if the host module exposes its entry points slightly differently.
#
# Required `inspector_widget` public API (documented in host/README.md):
#
#   inspector_widget.list_devices() -> list[dict]
#       each: {"serial": str, "api": int|None, "abi": str|None,
#              "model": str|None, "state": str}
#
#   inspector_widget.list_processes(serial) -> list[dict]
#       each: {"package": str, "pid": int|None, "running": bool}
#       (debuggable packages only)
#
#   inspector_widget.attach(serial, package) -> Session
#       Injects the agent if not already attached, forwards the socket, performs
#       the HELLO handshake.  Returns a live Session.  Cheap/idempotent if the
#       agent is already attached for that (serial, package).
#
#   Session attributes / methods (all synchronous, blocking):
#       .serial: str
#       .package: str
#       .api_level: int
#       .abi: str
#       .agent_version: str
#       .get_windows() -> dict{"root_ids": list[int], "strings": {id:str}}
#       .dump_tree(root_id=0, include_properties=False,
#                  include_resolution_stack=False, include_screenshot=False,
#                  screenshot_scale=1.0)
#           -> ViewInspection.DumpTreeResponse  (raw proto message)
#       .get_properties(view_id, include_resolution_stack=False)
#           -> ViewInspection.GetPropertiesResponse  (raw proto message)
#       .screenshot(root_id=0, scale=1.0)
#           -> ViewInspection.ScreenshotResponse  (raw proto message)
#       .detach()  -> sends ShutdownCommand, tears down forward, closes socket.
#
# The proto module is `inspector_widget.proto.view_inspection_pb2` (generated;
# protobuf package `viewspector.proto`); we import it for enum names + screenshot
# decoding helpers. The self-check probes it; the tools reach it via the package.
# --------------------------------------------------------------------------- #


class HostUnavailableError(RuntimeError):
    """Raised when the inspector_widget package cannot be imported/used."""


def _import_host() -> Any:
    """Import and return the inspector_widget module (or raise)."""
    try:
        import inspector_widget  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on sibling build
        raise HostUnavailableError(
            "Could not import 'inspector_widget'. Build the host package first "
            "(scripts/build.sh) and run from the project's virtualenv. "
            f"Underlying error: {exc!r}"
        ) from exc
    return inspector_widget


def _import_proto() -> Any:
    """Import the generated protobuf module used to read response fields.

    The bindings live in the host package's ``proto`` subpackage
    (``inspector_widget/proto/view_inspection_pb2.py``) — the same module the
    rest of the package imports via ``from .proto import view_inspection_pb2``.
    The bare names are kept only as fallbacks for a flat protoc layout.
    """
    for name in (
        "inspector_widget.proto.view_inspection_pb2",
        "view_inspection_pb2",
        "inspector_widget.view_inspection_pb2",
    ):
        try:
            # fromlist non-empty -> __import__ returns the leaf submodule itself.
            return __import__(name, fromlist=["Request"])
        except Exception:
            continue
    raise HostUnavailableError(
        "Could not import the generated protobuf module "
        "'inspector_widget.proto.view_inspection_pb2'. "
        "Run scripts/build.sh to generate it."
    )


class HostFacade:
    """Single, lazily-initialised gateway to the inspector_widget driver."""

    def __init__(self) -> None:
        self._host: Any = None
        self._proto: Any = None
        self._lock = threading.Lock()

    @property
    def host(self) -> Any:
        if self._host is None:
            with self._lock:
                if self._host is None:
                    self._host = _import_host()
        return self._host

    @property
    def proto(self) -> Any:
        if self._proto is None:
            with self._lock:
                if self._proto is None:
                    self._proto = _import_proto()
        return self._proto

    # -- adb-level queries -------------------------------------------------- #
    def list_devices(self) -> List[Dict[str, Any]]:
        fn = _first_attr(self.host, "list_devices", "devices")
        return list(fn())

    def list_processes(self, serial: str) -> List[Dict[str, Any]]:
        fn = _first_attr(self.host, "list_processes", "processes", "list_packages")
        return list(fn(serial))

    def attach(self, serial: str, package: str) -> Any:
        fn = _first_attr(self.host, "attach", "connect", "inject_and_attach")
        return fn(serial, package)


def _first_attr(obj: Any, *names: str) -> Callable[..., Any]:
    for name in names:
        fn = getattr(obj, name, None)
        if callable(fn):
            return fn
    raise HostUnavailableError(
        f"inspector_widget is missing any of {names!r}; the host driver API does "
        "not match the version this MCP server expects."
    )


HOST = HostFacade()


# --------------------------------------------------------------------------- #
# Session cache, keyed by (serial, package).
# --------------------------------------------------------------------------- #
class SessionCache:
    def __init__(self) -> None:
        self._sessions: Dict[Tuple[str, str], Any] = {}
        self._lock = threading.Lock()

    def _key(self, serial: str, package: str) -> Tuple[str, str]:
        return (serial, package)

    def get_or_attach(self, serial: str, package: str) -> Any:
        key = self._key(serial, package)
        with self._lock:
            session = self._sessions.get(key)
            if session is not None and _session_alive(session):
                return session
            # Stale/closed session: drop it before re-attaching.
            if session is not None:
                _safe_detach(session)
                self._sessions.pop(key, None)
            session = HOST.attach(serial, package)
            self._sessions[key] = session
            return session

    def peek(self, serial: str, package: str) -> Optional[Any]:
        with self._lock:
            return self._sessions.get(self._key(serial, package))

    def drop(self, serial: str, package: str) -> Optional[Any]:
        with self._lock:
            return self._sessions.pop(self._key(serial, package), None)

    def all(self) -> List[Any]:
        with self._lock:
            return list(self._sessions.values())


SESSIONS = SessionCache()


def _session_alive(session: Any) -> bool:
    alive = getattr(session, "is_alive", None)
    if callable(alive):
        try:
            return bool(alive())
        except Exception:
            return False
    closed = getattr(session, "closed", None)
    if isinstance(closed, bool):
        return not closed
    return True


def _safe_detach(session: Any) -> None:
    for name in ("detach", "shutdown", "close"):
        fn = getattr(session, name, None)
        if callable(fn):
            try:
                fn()
                return
            except Exception:  # pragma: no cover - best-effort teardown
                log.debug("session.%s() failed", name, exc_info=True)
                return


# --------------------------------------------------------------------------- #
# Proto -> JSON shaping.  String-table ids are resolved to text here so the
# agent sees readable trees/properties rather than raw integer indices.
# --------------------------------------------------------------------------- #
def _strings_to_map(strings_msg: Any) -> Dict[int, str]:
    """ViewInspection.Strings -> {id: str}.  id 0 is implicitly "" (absent)."""
    table: Dict[int, str] = {0: ""}
    if strings_msg is None:
        return table
    for entry in getattr(strings_msg, "entries", []):
        table[entry.id] = entry.str
    return table


def _s(table: Dict[int, str], sid: int) -> Optional[str]:
    """Resolve a string-table id to text; id 0 -> None (absent)."""
    if not sid:
        return None
    return table.get(sid)


_PROPERTY_TYPE_NAMES = {
    0: "STRING", 1: "BOOLEAN", 2: "BYTE", 3: "CHAR", 4: "DOUBLE", 5: "FLOAT",
    6: "INT16", 7: "INT32", 8: "INT64", 9: "OBJECT", 10: "COLOR", 11: "GRAVITY",
    12: "INT_ENUM", 13: "INT_FLAG", 14: "RESOURCE", 15: "DRAWABLE", 16: "ANIM",
    17: "ANIMATOR", 18: "INTERPOLATOR", 19: "DIMENSION",
}

# Types whose payload travels in str_value (a string-table id).
_STR_VALUE_TYPES = {0, 9, 12, 15, 16, 17, 18}  # STRING, OBJECT, INT_ENUM, DRAWABLE, ANIM, ANIMATOR, INTERPOLATOR
# Types whose payload travels in int32_value.
_INT32_VALUE_TYPES = {2, 3, 6, 7, 10, 11, 13}  # BYTE, CHAR, INT16, INT32, COLOR, GRAVITY, INT_FLAG


def _resource_to_json(table: Dict[int, str], res: Any) -> Optional[Dict[str, Any]]:
    if res is None:
        return None
    type_name = _s(table, res.type)
    name = _s(table, res.name)
    namespace = _s(table, res.namespace)
    if type_name is None and name is None and namespace is None:
        return None
    out: Dict[str, Any] = {}
    if namespace is not None:
        out["namespace"] = namespace
    if type_name is not None:
        out["type"] = type_name
    if name is not None:
        out["name"] = name
    # Convenience @type/name string, e.g. "@id/my_button".
    if type_name is not None and name is not None:
        out["ref"] = f"@{type_name}/{name}"
    return out


def _bounds_to_json(bounds: Any) -> Optional[Dict[str, Any]]:
    if bounds is None:
        return None
    # Bounds.layout is a sub-message; in proto3 reading it always yields a value
    # (a default all-zero Rect if unset), which we surface as-is.
    layout = bounds.layout
    out: Dict[str, Any] = {
        "x": layout.x, "y": layout.y, "w": layout.w, "h": layout.h,
    }
    if bounds.HasField("render"):
        q = bounds.render
        out["render_quad"] = [
            [q.x0, q.y0], [q.x1, q.y1], [q.x2, q.y2], [q.x3, q.y3],
        ]
    return out


def _node_to_json(table: Dict[int, str], node: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id": node.id,
        "class_name": _s(table, node.class_name),
        "package_name": _s(table, node.package_name),
    }
    bounds = _bounds_to_json(node.bounds if node.HasField("bounds") else None)
    if bounds is not None:
        out["bounds"] = bounds
    resource = _resource_to_json(table, node.resource if node.HasField("resource") else None)
    if resource is not None:
        out["resource"] = resource
    layout_resource = _resource_to_json(
        table, node.layout_resource if node.HasField("layout_resource") else None
    )
    if layout_resource is not None:
        out["layout_resource"] = layout_resource
    view_id_name = _s(table, node.view_id_name)
    if view_id_name:
        out["view_id_name"] = view_id_name
    text = _s(table, node.text_value)
    if text:
        out["text"] = text
    if node.flags:
        flag_names = []
        # Flag.IS_WEBVIEW == 1 (bitmask) per proto.
        if node.flags & 1:
            flag_names.append("IS_WEBVIEW")
        out["flags"] = flag_names
    children = [_node_to_json(table, c) for c in node.children]
    if children:
        out["children"] = children
    return out


def _property_to_json(table: Dict[int, str], prop: Any, include_stack: bool) -> Dict[str, Any]:
    ptype = int(prop.type)
    out: Dict[str, Any] = {
        "name": _s(table, prop.name),
        "type": _PROPERTY_TYPE_NAMES.get(ptype, str(ptype)),
    }
    if prop.is_layout:
        out["is_layout"] = True

    # Pull the single populated value slot per type.
    if ptype == 1:  # BOOLEAN
        out["value"] = bool(prop.int32_value)
    elif ptype in (10,):  # COLOR -> #AARRGGBB
        out["value"] = _color_hex(prop.int32_value)
    elif ptype in _INT32_VALUE_TYPES:
        out["value"] = prop.int32_value
    elif ptype == 8:  # INT64
        out["value"] = prop.int64_value
    elif ptype == 4:  # DOUBLE
        out["value"] = prop.double_value
    elif ptype in (5, 19):  # FLOAT, DIMENSION
        out["value"] = prop.float_value
    elif ptype in _STR_VALUE_TYPES:
        out["value"] = _s(table, prop.str_value)
    elif ptype == 14:  # RESOURCE
        out["value"] = _resource_to_json(
            table, prop.resource_value if prop.HasField("resource_value") else None
        )
    else:
        # Unknown / future type: surface whatever non-empty slot we can.
        if prop.str_value:
            out["value"] = _s(table, prop.str_value)
        elif prop.int64_value:
            out["value"] = prop.int64_value
        elif prop.int32_value:
            out["value"] = prop.int32_value

    source = _s(table, prop.source)
    if source:
        out["source"] = source
    if include_stack and prop.resolution_stack:
        stack = [_s(table, sid) for sid in prop.resolution_stack]
        out["resolution_stack"] = [s for s in stack if s]
    return out


def _color_hex(argb: int) -> str:
    # int32 may be negative (sign bit set); mask to 32 bits.
    v = argb & 0xFFFFFFFF
    return "#{:08X}".format(v)


def _property_group_to_json(
    table: Dict[int, str], group: Any, include_stack: bool
) -> Dict[str, Any]:
    return {
        "view_id": group.view_id,
        "properties": [
            _property_to_json(table, p, include_stack) for p in group.properties
        ],
    }


# --------------------------------------------------------------------------- #
# Screenshot decoding: the wire `Screenshot.data` is a Deflate(BEST_SPEED)
# buffer of [9-byte header | raw pixels].  The host driver is expected to expose
# a decoder that yields a PNG; if it does not, we decode here using stdlib
# (zlib) + a tiny BMP/PNG writer fallback.  We prefer the driver's decoder.
# --------------------------------------------------------------------------- #
def _save_screenshot_png(session: Any, screenshot_msg: Any, dest_path: str) -> Dict[str, Any]:
    """Materialise a Screenshot proto to a PNG file at dest_path.

    Returns metadata {path, width, height, bytes}.
    """
    # 1. Prefer a host-driver decoder that returns PNG bytes (it already owns the
    #    Deflate + header + pixel-config decoding to stay byte-exact with the agent).
    png_bytes: Optional[bytes] = None
    decoder = None
    for name in ("screenshot_to_png", "decode_screenshot_png", "to_png"):
        fn = getattr(session, name, None) or getattr(HOST.host, name, None)
        if callable(fn):
            decoder = fn
            break
    if decoder is not None:
        try:
            png_bytes = decoder(screenshot_msg)
        except Exception:  # pragma: no cover - fall back to our own decoder
            log.debug("host screenshot decoder failed; using stdlib decoder", exc_info=True)
            png_bytes = None
    if png_bytes is None:
        # 2. Self-contained stdlib decode (zlib + minimal PNG writer).
        png_bytes = _decode_screenshot_to_png(screenshot_msg)

    with open(dest_path, "wb") as fh:
        fh.write(png_bytes)
    return {
        "path": dest_path,
        "width": int(screenshot_msg.width),
        "height": int(screenshot_msg.height),
        "bytes": len(png_bytes),
        "scale": float(screenshot_msg.scale) if screenshot_msg.scale else 1.0,
    }


def _decode_screenshot_to_png(screenshot_msg: Any) -> bytes:
    """Decode the Inspector Widget BITMAP wire format -> PNG bytes.

    Decoding is delegated to ``inspector_widget.png._decode_to_rgba`` (the single
    source of truth for the Deflate + 9-byte header + pixel-config handling, incl.
    the ARGB_8888/type-3 R/B swap), then encoded to PNG with our stdlib encoder.
    """
    from inspector_widget import png as pngmod  # type: ignore

    width, height, rgba = pngmod._decode_to_rgba(screenshot_msg)
    return _rgba_to_png(rgba, width, height)


def _rgba_to_png(rgba: bytearray, width: int, height: int) -> bytes:
    """Minimal RGBA8888 -> PNG encoder (stdlib zlib, no Pillow)."""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    # IHDR: width, height, bit depth 8, colour type 6 (RGBA), no interlace.
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)

    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 (None) per scanline
        start = y * stride
        raw += rgba[start : start + stride]
    idat = zlib.compress(bytes(raw), 9)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


_TMP_PREFIX = "viewspector_"


def _tmp_png_path(serial: str, package: str, tag: str) -> str:
    safe_pkg = package.replace("/", "_").replace(":", "_")
    safe_serial = serial.replace("/", "_").replace(":", "_")
    fd, path = tempfile.mkstemp(
        prefix=f"{_TMP_PREFIX}{safe_serial}_{safe_pkg}_{tag}_", suffix=".png"
    )
    os.close(fd)
    return path


# --------------------------------------------------------------------------- #
# Tool implementations.  Each returns a JSON-serialisable dict.  They are sync;
# the MCP layer (real or fallback) runs them in a worker thread because the host
# driver does blocking adb + socket I/O.
# --------------------------------------------------------------------------- #
def _device_to_json(d: Any) -> Dict[str, Any]:
    if isinstance(d, dict):
        return {
            "serial": d.get("serial"),
            "api": d.get("api"),
            "abi": d.get("abi"),
            "model": d.get("model"),
            "state": d.get("state", "device"),
        }
    return {
        "serial": getattr(d, "serial", None),
        "api": getattr(d, "api", None),
        "abi": getattr(d, "abi", None),
        "model": getattr(d, "model", None),
        "state": getattr(d, "state", "device"),
    }


def _process_to_json(p: Any) -> Dict[str, Any]:
    if isinstance(p, dict):
        return {
            "package": p.get("package"),
            "pid": p.get("pid"),
            "running": bool(p.get("pid")) if p.get("running") is None else bool(p.get("running")),
        }
    pid = getattr(p, "pid", None)
    return {
        "package": getattr(p, "package", None),
        "pid": pid,
        "running": bool(getattr(p, "running", pid is not None)),
    }


def tool_list_devices() -> Dict[str, Any]:
    devices = [_device_to_json(d) for d in HOST.list_devices()]
    return {"devices": devices, "count": len(devices)}


def tool_list_processes(serial: str) -> Dict[str, Any]:
    _require(serial, "serial")
    procs = [_process_to_json(p) for p in HOST.list_processes(serial)]
    procs.sort(key=lambda p: (not p["running"], p["package"] or ""))
    return {"serial": serial, "processes": procs, "count": len(procs)}


def tool_attach(serial: str, package: str) -> Dict[str, Any]:
    _require(serial, "serial")
    _require(package, "package")
    session = SESSIONS.get_or_attach(serial, package)
    # Window count is cheap and confirms the agent is live + responsive.
    window_count = None
    try:
        windows = _session_get_windows(session)
        window_count = len(windows.get("root_ids", []))
    except Exception as exc:  # pragma: no cover - device-dependent
        log.warning("get_windows after attach failed: %r", exc)
    return {
        "serial": serial,
        "package": package,
        "attached": True,
        "api_level": getattr(session, "api_level", None),
        "abi": getattr(session, "abi", None),
        "agent_version": getattr(session, "agent_version", None),
        "window_count": window_count,
        "session": f"{serial}/{package}",
    }


def tool_dump_tree(
    serial: str,
    package: str,
    include_properties: bool = False,
    include_resolution_stack: bool = False,
    include_screenshot: bool = False,
    scale: float = 1.0,
    root_id: int = 0,
) -> Dict[str, Any]:
    _require(serial, "serial")
    _require(package, "package")
    scale = _clamp_scale(scale)
    root_id = _as_int(root_id, "root_id")
    session = SESSIONS.get_or_attach(serial, package)
    resp = session.dump_tree(
        root_id=root_id,
        include_properties=bool(include_properties),
        include_resolution_stack=bool(include_resolution_stack),
        include_screenshot=bool(include_screenshot),
        screenshot_scale=scale,
    )
    table = _strings_to_map(resp.strings if resp.HasField("strings") else None)
    result: Dict[str, Any] = {
        "serial": serial,
        "package": package,
        "roots": [_node_to_json(table, r) for r in resp.roots],
        "root_count": len(resp.roots),
    }
    if include_properties:
        result["properties"] = [
            _property_group_to_json(table, g, include_resolution_stack)
            for g in resp.properties
        ]
    if include_screenshot and resp.HasField("screenshot"):
        dest = _tmp_png_path(serial, package, "tree")
        result["screenshot"] = _save_screenshot_png(session, resp.screenshot, dest)
    return result


def tool_get_properties(
    serial: str,
    package: str,
    view_id: int,
    include_resolution_stack: bool = False,
) -> Dict[str, Any]:
    _require(serial, "serial")
    _require(package, "package")
    view_id = _as_int(view_id, "view_id")
    session = SESSIONS.get_or_attach(serial, package)
    resp = session.get_properties(
        view_id=view_id, include_resolution_stack=bool(include_resolution_stack)
    )
    table = _strings_to_map(resp.strings if resp.HasField("strings") else None)
    group = resp.group if resp.HasField("group") else None
    return {
        "serial": serial,
        "package": package,
        "view_id": view_id,
        "group": (
            _property_group_to_json(table, group, include_resolution_stack)
            if group is not None
            else None
        ),
    }


def tool_screenshot(serial: str, package: str, scale: float = 1.0) -> Dict[str, Any]:
    _require(serial, "serial")
    _require(package, "package")
    scale = _clamp_scale(scale)
    session = SESSIONS.get_or_attach(serial, package)
    resp = session.screenshot(root_id=0, scale=scale)
    if not resp.HasField("screenshot"):
        raise ToolError("agent returned no screenshot")
    dest = _tmp_png_path(serial, package, "shot")
    meta = _save_screenshot_png(session, resp.screenshot, dest)
    meta.update({"serial": serial, "package": package})
    return meta


def tool_detach(serial: str, package: str) -> Dict[str, Any]:
    _require(serial, "serial")
    _require(package, "package")
    session = SESSIONS.drop(serial, package)
    if session is None:
        return {
            "serial": serial,
            "package": package,
            "detached": False,
            "note": "no active session for this (serial, package)",
        }
    _safe_detach(session)
    return {"serial": serial, "package": package, "detached": True}


def tool_dump_compose(
    serial: str, package: str,
    include_semantics: bool = True, include_slot_table: bool = True,
    enable_inspection: bool = False,
) -> Dict[str, Any]:
    """Dump the Compose layer (semantics tree + slot table) of the app's UI."""
    _require(serial, "serial")
    _require(package, "package")
    from inspector_widget import strings as st
    session = SESSIONS.get_or_attach(serial, package)
    resp = session.dump_compose(include_semantics=include_semantics,
                                include_slot_table=include_slot_table,
                                enable_inspection=enable_inspection)
    data = st.dump_compose_to_dict(resp)
    data.update({"serial": serial, "package": package})
    if include_slot_table and not enable_inspection and not st.compose_slot_table_populated(data):
        data["note"] = ("slot table not populated (semantics only). Pass enable_inspection=true for "
                        "composable names/params/file:line. WARNING: "
                        + st.ENABLE_INSPECTION_WARNING % "enable_inspection=true")
    return data


def tool_compose_overlay(serial: str, package: str, scale: float = 1.0,
                         labeled_only: bool = True) -> Dict[str, Any]:
    """Screenshot the app and draw every on-screen Compose element (text/role/bounds)
    as a labeled box over it. Returns the annotated PNG path + the on-screen text list."""
    _require(serial, "serial")
    _require(package, "package")
    from inspector_widget import strings as st, overlay as ov, png as pngmod
    scale = _clamp_scale(scale)
    session = SESSIONS.get_or_attach(serial, package)
    # Overlay uses the semantics tree only (clean, on-screen text labels; no recompose).
    data = st.dump_compose_to_dict(
        session.dump_compose(include_semantics=True, include_slot_table=False))
    roots = [w["root"] for w in data.get("windows", []) if w.get("root")]
    shot = session.screenshot(root_id=0, scale=scale)
    if not shot.HasField("screenshot"):
        raise ToolError("agent returned no screenshot")
    base = _tmp_png_path(serial, package, "compose_base")
    pngmod.write_png(shot.screenshot, base)
    base_scale = float(shot.screenshot.scale) or scale
    out = _tmp_png_path(serial, package, "compose_overlay")
    summary = ov.render_compose_overlay(base, roots, out, labeled_only=labeled_only,
                                        scale=base_scale)
    try:
        os.remove(base)
    except OSError:
        pass
    on_screen: List[Dict[str, Any]] = []

    def _collect(n: Dict[str, Any]) -> None:
        a = n.get("attrs", {}) or {}
        t = a.get("Text") or a.get("ContentDescription")
        b = (n.get("bounds") or {}).get("layout")
        if t and b:
            on_screen.append({"text": t, "role": a.get("Role"), "bounds": b})
        for ch in n.get("children", []) or []:
            _collect(ch)

    for r in roots:
        _collect(r)
    return {
        "serial": serial, "package": package, "path": out, "overlay_path": out,
        "diagnostics": data.get("diagnostics"), "boxes": summary["boxes"],
        "labels": summary["labels"], "size": summary["size"], "on_screen": on_screen,
    }


def _h_dump_compose(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_dump_compose(
        args.get("serial"), args.get("package"),
        include_semantics=args.get("include_semantics", True),
        include_slot_table=args.get("include_slot_table", True),
        enable_inspection=args.get("enable_inspection", False),
    )


def _h_compose_overlay(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_compose_overlay(
        args.get("serial"), args.get("package"),
        scale=args.get("scale", 1.0),
        labeled_only=not args.get("all_boxes", False),
    )


def _session_get_windows(session: Any) -> Dict[str, Any]:
    """Normalise session.get_windows() to {root_ids, strings} regardless of
    whether the host returns a proto message or a plain dict."""
    raw = session.get_windows()
    if isinstance(raw, dict):
        return {"root_ids": list(raw.get("root_ids", [])), "strings": raw.get("strings", {})}
    # Proto GetWindowsResponse
    table = _strings_to_map(raw.strings if raw.HasField("strings") else None)
    return {"root_ids": list(raw.root_ids), "strings": table}


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
class ToolError(Exception):
    """Raised for invalid arguments / tool-level failures; surfaced to the agent."""


def _require(value: Any, name: str) -> None:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        raise ToolError(f"missing required argument: {name}")


def _clamp_scale(scale: Any) -> float:
    try:
        s = float(scale)
    except (TypeError, ValueError):
        raise ToolError(f"scale must be a number, got {scale!r}")
    if s <= 0:
        s = 1.0
    # Contract: capture scale <= 1.0.
    return min(s, 1.0)


def _as_int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be an integer, got {value!r}")


# --------------------------------------------------------------------------- #
# Tool registry — shared by both the real-MCP and fallback servers.
# Each entry: name -> (handler, json-schema, description).
# Handlers take a single `args: dict` and return a JSON-serialisable dict.
# --------------------------------------------------------------------------- #
def _h_list_devices(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_list_devices()


# Handlers use args.get(...) (not indexing) so the tool functions' own _require()
# validators raise clean ToolError messages instead of a raw KeyError when a
# client omits a required argument.
def _h_list_processes(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_list_processes(args.get("serial"))


def _h_attach(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_attach(args.get("serial"), args.get("package"))


def _h_dump_tree(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_dump_tree(
        args.get("serial"),
        args.get("package"),
        include_properties=args.get("include_properties", False),
        include_resolution_stack=args.get("include_resolution_stack", False),
        include_screenshot=args.get("include_screenshot", False),
        scale=args.get("scale", 1.0),
        root_id=args.get("root_id", 0),
    )


def _h_get_properties(args: Dict[str, Any]) -> Dict[str, Any]:
    if "view_id" not in args:
        raise ToolError("missing required argument: view_id")
    return tool_get_properties(
        args.get("serial"),
        args.get("package"),
        args.get("view_id"),
        include_resolution_stack=args.get("include_resolution_stack", False),
    )


def _h_screenshot(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_screenshot(args.get("serial"), args.get("package"), scale=args.get("scale", 1.0))


def _h_detach(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_detach(args.get("serial"), args.get("package"))


_SERIAL = {"type": "string", "description": "ADB device serial (from list_devices), e.g. 'emulator-5554'."}
_PACKAGE = {
    "type": "string",
    "description": "Target app package name (must be debuggable + installed), e.g. 'com.example.app'.",
}

# --------------------------------------------------------------------------- #
# Accessibility tools (dump / lint / overlay).
# --------------------------------------------------------------------------- #
def _a11y_device_metrics(serial: str):
    """(density_dpi, font_scale) for the lint, probed once per serial and cached."""
    from inspector_widget import adb
    cache = _a11y_device_metrics.__dict__.setdefault("_cache", {})
    if serial not in cache:
        try:
            density = adb.display_density(serial)
        except Exception:
            density = None  # the lint assumes 420dpi and says so in its diagnostics
        try:
            fscale = adb.font_scale(serial)
        except Exception:
            fscale = 1.0
        cache[serial] = (density, fscale)
    return cache[serial]


def _a11y_lint_rules(rules: Any):
    """Validate rule ids/aliases up front so a typo is a clear tool error."""
    from inspector_widget import a11y_lint
    if rules is not None and not isinstance(rules, (list, tuple, str)):
        raise ToolError(f"rules must be a list of rule ids, got {type(rules).__name__}")
    try:
        return a11y_lint.resolve_rule_ids(rules)
    except a11y_lint.UnknownRuleError as e:
        raise ToolError(str(e))


def tool_dump_accessibility(
    serial: str, package: str,
    include_extras: bool = True, include_rendering_info: bool = False,
) -> Dict[str, Any]:
    """Dump the unified AccessibilityNodeInfo tree (Views + Compose virtual nodes)
    exactly as TalkBack/UiAutomator see it, plus the host-computed reading order."""
    _require(serial, "serial")
    _require(package, "package")
    from inspector_widget import a11y as a11ymod
    session = SESSIONS.get_or_attach(serial, package)
    resp = session.dump_a11y(root_id=0, include_extras=bool(include_extras),
                             include_rendering_info=bool(include_rendering_info))
    data = a11ymod.a11y_to_dict(resp)
    data.update({"serial": serial, "package": package})
    return data


def tool_a11y_lint(
    serial: str, package: str,
    include_contrast: bool = True, scale: float = 1.0,
    wcag_mode: bool = False, rules: Optional[List[str]] = None,
    include_rendering_info: bool = True,
) -> Dict[str, Any]:
    """Run the host-side accessibility lint (R1..R18) over the unified a11y tree
    (Views + Compose, joined with Compose semantics detail). Returns findings with
    typed node keys plus a summary and diagnostics. Contrast samples each window."""
    _require(serial, "serial")
    _require(package, "package")
    from inspector_widget import a11y_lint
    enabled = _a11y_lint_rules(rules)
    scale = _clamp_scale(scale)
    session = SESSIONS.get_or_attach(serial, package)
    density, fscale = _a11y_device_metrics(serial)
    report = a11y_lint.run_lint(
        session, density=density, font_scale=fscale,
        include_contrast=bool(include_contrast), scale=scale, wcag_mode=bool(wcag_mode),
        rules=enabled, include_rendering_info=bool(include_rendering_info))
    out = report.to_dict()
    out.update({"serial": serial, "package": package,
                "contrast_sampled": bool(out["stats"].get("contrast_windows"))})
    return out


def tool_a11y_overlay(
    serial: str, package: str, scale: float = 1.0,
    include_contrast: bool = True, wcag_mode: bool = False,
) -> Dict[str, Any]:
    """Screenshot the app and draw every accessibility node (box + speakable label
    + TalkBack reading-order number), color-coded by lint severity. Returns the
    annotated PNG path plus the lint summary."""
    _require(serial, "serial")
    _require(package, "package")
    from inspector_widget import a11y as a11ymod, a11y_lint, overlay as ov
    scale = _clamp_scale(scale)
    session = SESSIONS.get_or_attach(serial, package)
    # One a11y dump feeds both the boxes/reading order and the lint.
    a11y_data = a11ymod.a11y_to_dict(session.dump_a11y(
        root_id=0, include_extras=True, include_rendering_info=True))
    density, fscale = _a11y_device_metrics(serial)
    report = a11y_lint.run_lint(
        session, density=density, font_scale=fscale,
        include_contrast=bool(include_contrast), scale=scale, wcag_mode=bool(wcag_mode),
        a11y_data=a11y_data)
    lint_out = report.to_dict()
    findings = lint_out["findings"]
    base = _tmp_png_path(serial, package, "a11y_base")
    try:
        base_scale = ov.write_screen_png(session, a11y_data, base, scale=scale)
    except RuntimeError as exc:
        raise ToolError(str(exc)) from None
    out = _tmp_png_path(serial, package, "a11y_overlay")
    summary = ov.render_a11y_overlay(base, a11y_data, out, findings=findings,
                                     scale=base_scale)
    try:
        os.remove(base)
    except OSError:
        pass
    return {
        "serial": serial, "package": package,
        "path": out, "overlay_path": out,
        "boxes": summary["boxes"], "labels": summary["labels"],
        "flagged": summary["flagged"], "flagged_by_bounds": summary.get("flagged_by_bounds"),
        "size": summary["size"],
        "finding_count": len(findings),
        "summary": lint_out["summary"],
        "lint_diagnostics": lint_out["diagnostics"],
        "diagnostics": a11y_data.get("diagnostics"),
    }


def _h_dump_accessibility(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_dump_accessibility(
        args.get("serial"), args.get("package"),
        include_extras=args.get("include_extras", True),
        include_rendering_info=args.get("include_rendering_info", False),
    )


def _h_a11y_lint(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_a11y_lint(
        args.get("serial"), args.get("package"),
        include_contrast=args.get("include_contrast", True),
        scale=args.get("scale", 1.0),
        wcag_mode=args.get("wcag_mode", False),
        rules=args.get("rules"),
        include_rendering_info=args.get("include_rendering_info", True),
    )


def _h_a11y_overlay(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_a11y_overlay(
        args.get("serial"), args.get("package"),
        scale=args.get("scale", 1.0),
        include_contrast=args.get("include_contrast", True),
        wcag_mode=args.get("wcag_mode", False),
    )


def _load_a11y_rule_choices() -> List[str]:
    try:
        from inspector_widget import a11y_lint
        return list(a11y_lint.RULE_CHOICES)
    except Exception:  # keep the server importable even if the lint cannot load
        return []


_A11Y_RULE_CHOICES = _load_a11y_rule_choices()
_A11Y_RULE_ITEMS: Dict[str, Any] = (
    {"type": "string", "enum": _A11Y_RULE_CHOICES} if _A11Y_RULE_CHOICES else {"type": "string"})


TOOLS: Dict[str, Dict[str, Any]] = {
    "list_devices": {
        "handler": _h_list_devices,
        "description": (
            "List connected Android devices/emulators visible to adb, with each device's "
            "serial, API level, and primary ABI. Call this first to discover targets."
        ),
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "list_processes": {
        "handler": _h_list_processes,
        "description": (
            "List debuggable application packages installed on a device. Each entry includes "
            "the package name and, if the app is currently running, its pid. Only debuggable "
            "apps can be inspected. Provide a serial from list_devices."
        ),
        "schema": {
            "type": "object",
            "properties": {"serial": _SERIAL},
            "required": ["serial"],
            "additionalProperties": False,
        },
    },
    "attach": {
        "handler": _h_attach,
        "description": (
            "Attach the Inspector Widget agent to a running, debuggable app and open an inspection "
            "session. Injects the native agent + dex/jar and forwards the socket if not already "
            "attached (idempotent per serial+package). The app MUST be running. Returns the "
            "device API level, ABI, agent version, and the current number of inspectable windows. "
            "Sessions are cached; subsequent dump_tree/get_properties/screenshot reuse them."
        ),
        "schema": {
            "type": "object",
            "properties": {"serial": _SERIAL, "package": _PACKAGE},
            "required": ["serial", "package"],
            "additionalProperties": False,
        },
    },
    "dump_tree": {
        "handler": _h_dump_tree,
        "description": (
            "Capture the live Android View hierarchy for an app as a JSON tree. Auto-attaches if "
            "needed. Each node has: id (uniqueDrawingId, stable per view instance — use it for "
            "get_properties), class_name, package_name, absolute on-screen bounds {x,y,w,h} (plus "
            "render_quad for rotated/transformed views), the view's @id resource (resource.ref like "
            "'@id/my_button'), best-effort text for TextViews, and IS_WEBVIEW flag. "
            "Set include_properties=true to inline every view's attributes (larger payload); "
            "include_resolution_stack=true to also include where each attribute value was resolved "
            "from (style/layout chain). Set include_screenshot=true to also capture a PNG of the UI "
            "(saved to a temp file; its path is returned under 'screenshot.path'). scale (0<scale<=1) "
            "shrinks the screenshot."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "include_properties": {
                    "type": "boolean",
                    "default": False,
                    "description": "Inline all view attributes for every node (use get_properties for a single view instead if you only need one).",
                },
                "include_resolution_stack": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include the style/layout resolution chain for each property (only meaningful with include_properties).",
                },
                "include_screenshot": {
                    "type": "boolean",
                    "default": False,
                    "description": "Also capture a screenshot PNG; the saved file path is returned in result.screenshot.path.",
                },
                "scale": {
                    "type": "number",
                    "default": 1.0,
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Screenshot scale factor in (0, 1]. Only used when include_screenshot=true.",
                },
                "root_id": {
                    "type": "integer",
                    "default": 0,
                    "description": "Which window/root to dump (a root id from get_windows). 0 == all roots.",
                },
            },
            "required": ["serial", "package"],
            "additionalProperties": False,
        },
    },
    "get_properties": {
        "handler": _h_get_properties,
        "description": (
            "Fetch the full attribute set for a single view, identified by its view_id (the 'id' "
            "field from dump_tree — a uniqueDrawingId). Returns typed properties (name, type, value), "
            "marking layout-param attributes with is_layout, decoding colors to #AARRGGBB and "
            "gravity/flags to ints/labels, and resolving resource references. Set "
            "include_resolution_stack=true to also get, per property, the source file/style and the "
            "ordered chain of styles/layouts considered."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "view_id": {
                    "type": "integer",
                    "description": "The view's uniqueDrawingId, i.e. the 'id' field of a node from dump_tree.",
                },
                "include_resolution_stack": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include per-property source + style/layout resolution chain.",
                },
            },
            "required": ["serial", "package", "view_id"],
            "additionalProperties": False,
        },
    },
    "screenshot": {
        "handler": _h_screenshot,
        "description": (
            "Capture a screenshot of the app's current UI and save it as a PNG file on the host. "
            "Auto-attaches if needed. Returns the saved file path plus width/height. Use scale "
            "(0<scale<=1) to reduce size. For UI structure use dump_tree; use this when you need the "
            "rendered pixels."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "scale": {
                    "type": "number",
                    "default": 1.0,
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Scale factor in (0, 1]; 1.0 = full resolution.",
                },
            },
            "required": ["serial", "package"],
            "additionalProperties": False,
        },
    },
    "dump_compose": {
        "handler": _h_dump_compose,
        "description": (
            "Dump the Jetpack COMPOSE layer of the app's UI — the part that dump_tree cannot see "
            "(dump_tree bottoms out at AndroidComposeView). Returns a tree of compose nodes from the "
            "live semantics tree (every on-screen element with its Text, ContentDescription, Role, "
            "state, and on-screen bounds) and, when available, slot-table composables with file:line. "
            "Auto-attaches. Use this to read the actual on-screen content of a Compose app."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "include_semantics": {"type": "boolean", "default": True,
                    "description": "Include the semantics tree (on-screen text/role/bounds)."},
                "include_slot_table": {"type": "boolean", "default": True,
                    "description": "Also include the slot table (composable hierarchy, parameters, file:line)."},
                "enable_inspection": {"type": "boolean", "default": False,
                    "description": "Populate the slot table (composable names, parameters, file:line) by "
                                   "hot-reloading. DESTRUCTIVE: resets remember{} state in every composition "
                                   "(open dialogs, text input, scroll, toggles). Off by default; the semantics "
                                   "tree needs no hot-reload. Also re-mints Compose node ids once."},
            },
            "required": ["serial", "package"],
            "additionalProperties": False,
        },
    },
    "compose_overlay": {
        "handler": _h_compose_overlay,
        "description": (
            "Screenshot the app and draw EVERY on-screen Compose element as a labeled box "
            "(text/role + bounds) over the rendered pixels. Returns the annotated PNG path plus a "
            "flat list of on-screen text. The single best tool to 'show what's on screen' for a "
            "Compose app: combines the screenshot with the semantics data."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "scale": {"type": "number", "default": 1.0, "minimum": 0.0, "maximum": 1.0,
                          "description": "Screenshot scale in (0, 1]."},
                "all_boxes": {"type": "boolean", "default": False,
                              "description": "Box every node, not just text/role-bearing ones."},
            },
            "required": ["serial", "package"],
            "additionalProperties": False,
        },
    },
    "dump_accessibility": {
        "handler": _h_dump_accessibility,
        "description": (
            "Dump the UNIFIED accessibility tree (AccessibilityNodeInfo) for the app — exactly "
            "what TalkBack/UiAutomator see, covering both classic Views AND Compose virtual nodes "
            "in one tree. Each node has its host_view_id+virtual_id key (ties back to dump_tree/"
            "dump_compose), text/contentDescription/stateDescription/role, all a11y state flags, "
            "on-screen bounds, decoded actions (CLICK/SCROLL_FORWARD/SET_PROGRESS/...), collection/"
            "range info and extras. Every node has a typed node_key (view:<id> | "
            "compose:<acvId>:<semanticsId>) usable with inspect_node. Also returns the "
            "host-computed TalkBack reading order (focus_order: [{order, key, speak}] — what "
            "TalkBack announces at each stop, e.g. 'Delete, button'), built from the ANI child "
            "order + traversal_before/after, with reading_order_diagnostics for cycles and "
            "dangling targets. Auto-attaches."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "include_extras": {"type": "boolean", "default": True,
                    "description": "Iterate each node's extras bundle (roleDescription, compose testTag/id)."},
                "include_rendering_info": {"type": "boolean", "default": False,
                    "description": "Per-node refreshWithExtraData for layout size / text size (costly)."},
            },
            "required": ["serial", "package"],
            "additionalProperties": False,
        },
    },
    "a11y_lint": {
        "handler": _h_a11y_lint,
        "description": (
            "Run an accessibility LINT (rules R1..R18) over the app's UNIFIED accessibility tree -- "
            "classic Views, Compose, RecyclerView cells, AndroidView-in-Compose, all in one pass -- and "
            "report violations: missing labels, <48dp touch targets (touch bounds, real density), low "
            "text contrast sampled per window, redundant/duplicate labels (per-row list repeats are "
            "fine), clickable without a role, images without descriptions, toggles without state, "
            "empty focus stops, heading/grouping structure, non-scalable or tiny text, duplicate "
            "clickable bounds, contentDescription on text fields, unlabeled form fields, unclear link "
            "text and traversal-order cycles. Each finding has rule + alias (R#), severity "
            "(error/warn/info), node_key (view:<id> or compose:<acvId>:<semId>, usable with "
            "inspect_node), node, bounds (px and dp), window, collection position, a remediation "
            "message and evidence. Also returns summary and diagnostics. set include_contrast=false "
            "to skip the one pixel rule. Auto-attaches."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "include_contrast": {"type": "boolean", "default": True,
                    "description": "Sample a screenshot to run the color-contrast rule (R3)."},
                "scale": {"type": "number", "default": 1.0, "minimum": 0.0, "maximum": 1.0,
                    "description": "Screenshot scale in (0,1] for the contrast sample."},
                "wcag_mode": {"type": "boolean", "default": False,
                    "description": "Use WCAG target sizes (44dp) instead of Material (48dp)."},
                "rules": {"type": "array", "items": _A11Y_RULE_ITEMS,
                    "description": "Optional subset of rules to run: canonical ids (e.g. "
                                   "'a11y.label.missing'), aliases 'R1'..'R18', or ATF names "
                                   "(e.g. 'TouchTargetSize'). Omit for all."},
                "include_rendering_info": {"type": "boolean", "default": True,
                    "description": "Request per-node ExtraRenderingInfo (View text size/unit) for the "
                                   "text-size rules R11/R18 and text-size-aware contrast."},
            },
            "required": ["serial", "package"],
            "additionalProperties": False,
        },
    },
    "a11y_overlay": {
        "handler": _h_a11y_overlay,
        "description": (
            "Screenshot the app and draw EVERY accessibility node as a labeled box with its "
            "speakable label and the computed TalkBack reading-order number, color-coded by lint "
            "severity (red=error, amber=warn, blue=info, green=clean). Returns the annotated PNG "
            "path plus the lint summary. The single best 'show me the a11y problems on screen' tool."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "scale": {"type": "number", "default": 1.0, "minimum": 0.0, "maximum": 1.0,
                    "description": "Screenshot scale in (0,1]."},
                "include_contrast": {"type": "boolean", "default": True,
                    "description": "Also run the contrast rule so contrast issues are colored in."},
                "wcag_mode": {"type": "boolean", "default": False,
                    "description": "Use WCAG target sizes (44dp) instead of Material (48dp)."},
            },
            "required": ["serial", "package"],
            "additionalProperties": False,
        },
    },
    "detach": {
        "handler": _h_detach,
        "description": (
            "Shut down the Inspector Widget agent session for an app: sends the agent a shutdown command, "
            "tears down the forwarded socket, and drops the cached session. Call when finished "
            "inspecting an app to free device resources. Safe to call even if not attached."
        ),
        "schema": {
            "type": "object",
            "properties": {"serial": _SERIAL, "package": _PACKAGE},
            "required": ["serial", "package"],
            "additionalProperties": False,
        },
    },
}


# --------------------------------------------------------------------------- #
# Integrated inspector tools (integ.md PART 1 / tool list #13,#14,#15):
#   inspect          — whole-screen merged tree (view + compose + a11y, correlated)
#   inspect_node     — full dossier for ONE element + its component image + focused lint
#   component_image  — cut a per-component image (SKP by layerId else BITMAP crop)
# Defined here (before _run_tool) and registered via TOOLS.update(...).
# --------------------------------------------------------------------------- #
def _device_density(serial: str) -> int:
    """Best-effort device density as raw DPI (e.g. 420) for dp-based a11y lint.

    Returned as DPI (NOT a px/dp ratio): it is forwarded to inspect_node's
    density=, which feeds the lint as a DPI int (matching LintContext.density).
    Default 420 on any failure.
    """
    try:
        from inspector_widget import adb  # type: ignore
        out = adb.shell(serial, "wm density").strip()
        # "Physical density: 420" (and maybe an Override line); prefer override.
        dpi = None
        for line in out.splitlines():
            if ":" in line:
                try:
                    dpi = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
        if dpi:
            return dpi
    except Exception:
        pass
    return 420


def _lint_fn():
    """Return inspector_widget.a11y_lint.lint_a11y if importable, else None.

    correlate.inspect_node calls this as lint_fn(roots, density_dpi) — the unified
    a11y tree, or Compose-semantics roots for a lint that only takes those — and
    expects a list[dict] of findings.
    """
    try:
        from inspector_widget import a11y_lint  # type: ignore
        fn = getattr(a11y_lint, "lint_a11y", None)
        return fn if callable(fn) else None
    except Exception:
        return None


def tool_inspect(serial: str, package: str, include_properties: bool = False,
                 include_overlay: bool = False) -> Dict[str, Any]:
    """Whole-screen integrated tree: each node carries view/compose/a11y/image-ref + correlation."""
    _require(serial, "serial")
    _require(package, "package")
    from inspector_widget import correlate
    session = SESSIONS.get_or_attach(serial, package)
    merged = correlate.inspect_tree(session, include_properties=bool(include_properties))
    result: Dict[str, Any] = {
        "serial": serial, "package": package,
        "roots": merged.get("roots", []),
        "summary": merged.get("summary", {}),
        "sources": merged.get("sources", {}),
    }
    if include_overlay:
        from inspector_widget import overlay as ov
        shot = session.screenshot(root_id=0, scale=1.0)
        if shot.HasField("screenshot"):
            base = _tmp_png_path(serial, package, "integrated_base")
            _save_screenshot_png(session, shot.screenshot, base)
            out = _tmp_png_path(serial, package, "integrated_overlay")
            base_scale = float(shot.screenshot.scale) or 1.0
            try:
                summary = ov.render_integrated_overlay(base, merged, out, scale=base_scale)
                result["overlay"] = summary
            except (RuntimeError, AttributeError) as exc:
                result["overlay_error"] = str(exc)
            finally:
                try:
                    os.remove(base)
                except OSError:
                    pass
    return result


def tool_inspect_node(serial: str, package: str, node_key: Optional[str] = None,
                      view_id: Optional[int] = None, semantics_id: Optional[int] = None,
                      bounds: Optional[Dict[str, Any]] = None,
                      include_image: bool = True) -> Dict[str, Any]:
    """Full dossier for ONE element (by node_key | view_id | semantics_id | bounds)."""
    _require(serial, "serial")
    _require(package, "package")
    from inspector_widget import correlate
    if not any(v is not None for v in (node_key, view_id, semantics_id, bounds)):
        raise ToolError("inspect_node needs one of: node_key, view_id, semantics_id, bounds")
    vid = _as_int(view_id, "view_id") if view_id is not None else None
    sid = _as_int(semantics_id, "semantics_id") if semantics_id is not None else None
    session = SESSIONS.get_or_attach(serial, package)
    image_path = _tmp_png_path(serial, package, "dossier") if include_image else None
    try:
        dossier = correlate.inspect_node(
            session, node_key=node_key, view_id=vid, semantics_id=sid, bounds=bounds,
            include_image=bool(include_image), image_path=image_path,
            lint_fn=_lint_fn(), density=_device_density(serial),
        )
    except correlate.NodeKeyError as exc:
        raise ToolError(str(exc)) from None
    if dossier is None:
        raise ToolError("no matching element found for the given selector")
    dossier.update({"serial": serial, "package": package})
    return dossier


def tool_component_image(serial: str, package: str, node_key: Optional[str] = None,
                         view_id: Optional[int] = None, semantics_id: Optional[int] = None,
                         bounds: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Cut a per-component image for one element (SKP by graphicsLayer layerId, else BITMAP crop)."""
    _require(serial, "serial")
    _require(package, "package")
    from inspector_widget import correlate
    if not any(v is not None for v in (node_key, view_id, semantics_id, bounds)):
        raise ToolError("component_image needs one of: node_key, view_id, semantics_id, bounds")
    vid = _as_int(view_id, "view_id") if view_id is not None else None
    sid = _as_int(semantics_id, "semantics_id") if semantics_id is not None else None
    session = SESSIONS.get_or_attach(serial, package)
    merged = correlate.inspect_tree(session, include_properties=False)
    try:
        node = correlate.find_node(merged, node_key=node_key, view_id=vid,
                                   semantics_id=sid, bounds=bounds)
    except correlate.NodeKeyError as exc:
        raise ToolError(str(exc)) from None
    if node is None:
        raise ToolError("no matching element found for the given selector")
    out = _tmp_png_path(serial, package, "component")
    img = correlate.component_image(session, node, out_path=out)
    img.update({"serial": serial, "package": package, "node_key": node.get("node_key")})
    return img


def _h_inspect(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_inspect(
        args.get("serial"), args.get("package"),
        include_properties=args.get("include_properties", False),
        include_overlay=args.get("include_overlay", False),
    )


def _h_inspect_node(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_inspect_node(
        args.get("serial"), args.get("package"),
        node_key=args.get("node_key"), view_id=args.get("view_id"),
        semantics_id=args.get("semantics_id"), bounds=args.get("bounds"),
        include_image=args.get("include_image", True),
    )


def _h_component_image(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_component_image(
        args.get("serial"), args.get("package"),
        node_key=args.get("node_key"), view_id=args.get("view_id"),
        semantics_id=args.get("semantics_id"), bounds=args.get("bounds"),
    )


_BOUNDS_SCHEMA = {
    "type": "object",
    "description": "Absolute screen-px box {x,y,w,h}; resolves to the deepest covering element.",
    "properties": {
        "x": {"type": "integer"}, "y": {"type": "integer"},
        "w": {"type": "integer"}, "h": {"type": "integer"},
    },
    "required": ["x", "y", "w", "h"],
    "additionalProperties": False,
}

TOOLS.update({
    "inspect": {
        "handler": _h_inspect,
        "description": (
            "The integrated merged tree for the whole screen: walks the View hierarchy as the "
            "spine, grafts every ComposeView (RecyclerView cells and ones nested in AndroidView "
            "included) under its AndroidComposeView, re-homes AndroidView content under the "
            "Compose node hosting it, and attaches accessibility facets joined on (View id) / "
            "(ComposeView id, semantics id), with a same-window one-to-one bounds fallback. "
            "Keys: view:<id>, compose:<acvId>:<semanticsId>, composeview:<acvId>. Each node "
            "carries optional view{}, compose{}, a11y{} (incl. its TalkBack order), list_item{} "
            "(list + row), image_ref{} and a correlation_confidence (exact|overlap|none); the "
            "summary carries counts and a generation that changes when Compose re-mints ids. "
            "Set include_overlay=true to also render a labelled, color-coded overlay PNG."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "include_properties": {"type": "boolean", "default": False,
                    "description": "Inline full view properties under each node's view.properties."},
                "include_overlay": {"type": "boolean", "default": False,
                    "description": "Also render a labelled overlay PNG (path under result.overlay.path)."},
            },
            "required": ["serial", "package"],
            "additionalProperties": False,
        },
    },
    "inspect_node": {
        "handler": _h_inspect_node,
        "description": (
            "Full dossier for ONE element, selected by node_key ('view:<id>' | "
            "'compose:<acvId>:<semanticsId>' | 'composeview:<acvId>'), view_id (uniqueDrawingId), "
            "semantics_id (only when a single ComposeView has it), or bounds {x,y,w,h} (deepest "
            "covering element). A Compose key from an earlier dump is re-resolved after "
            "recomposition (resolved_from). Returns all facets fully populated (view "
            "attributes+properties, full a11y, compose attrs/source), where/context (window > "
            "list row > ComposeView > node), its component image (SKP cut by graphicsLayer "
            "layerId, else BITMAP crop) saved to a PNG path, and the lint findings for this node."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "node_key": {"type": "string",
                    "description": "'view:<uniqueDrawingId>', 'compose:<acvId>:<semanticsId>' or "
                                   "'composeview:<acvId>' (node_key from inspect / "
                                   "dump_accessibility)."},
                "view_id": {"type": "integer",
                    "description": "A view's uniqueDrawingId (the 'id' from dump_tree/inspect)."},
                "semantics_id": {"type": "integer",
                    "description": "A Compose node's semantics id (the 'id' from dump_compose); "
                                   "ambiguous when several ComposeViews use it, so prefer "
                                   "node_key."},
                "bounds": _BOUNDS_SCHEMA,
                "include_image": {"type": "boolean", "default": True,
                    "description": "Cut and save the component image (result.component_image.path)."},
            },
            "required": ["serial", "package"],
            "additionalProperties": False,
        },
    },
    "component_image": {
        "handler": _h_component_image,
        "description": (
            "Cut a per-component image for one element and save it as a PNG. Uses the SKP path "
            "(skiaparser GetViewTree by the Compose graphicsLayer render-node id) when available, "
            "else a BITMAP crop of the element's bounds from a full screenshot. Returns the PNG "
            "path and which path produced it (source: 'skp' | 'bitmap_crop')."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "node_key": {"type": "string",
                    "description": "'view:<id>' | 'compose:<acvId>:<semanticsId>' | "
                                   "'composeview:<acvId>'."},
                "view_id": {"type": "integer", "description": "A view's uniqueDrawingId."},
                "semantics_id": {"type": "integer", "description": "A Compose node's semantics id."},
                "bounds": _BOUNDS_SCHEMA,
            },
            "required": ["serial", "package"],
            "additionalProperties": False,
        },
    },
})


def _run_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Invoke a tool handler, converting any failure into a structured error dict.

    Every tool returns a plain dict — including failures ({"error": ..., "tool": ...}) —
    so a bad argument, a missing device, an unavailable skiaparser/Pillow/grpcio, or an
    agent ERROR never raises a raw exception (or hangs) at the transport layer.
    """
    entry = TOOLS.get(name)
    if entry is None:
        return {"error": f"unknown tool: {name}", "tool": name,
                "available_tools": sorted(TOOLS.keys())}
    try:
        return entry["handler"](args or {})
    except ToolError as exc:
        return {"error": str(exc), "tool": name}
    except Exception as exc:  # never leak a stack trace through the transport
        log.exception("tool %s failed", name)
        return {"error": f"{type(exc).__name__}: {exc}", "tool": name}


# --------------------------------------------------------------------------- #
# Real MCP SDK path.
# --------------------------------------------------------------------------- #
def _call_tool_text(name: str, arguments: Dict[str, Any]) -> Tuple[str, bool]:
    """Run a tool and render its MCP text payload. Returns ``(text, is_error)``.

    Shared by the SDK transport and the JSON-RPC fallback so both report
    failures identically (``isError: true`` + a JSON ``{"error": ...}`` body).
    ``_run_tool`` converts handler failures into a top-level ``{"error": ...}``
    dict rather than raising, so that key is what marks a failed call.
    """
    try:
        result = _run_tool(name, arguments or {})
        is_error = isinstance(result, dict) and "error" in result
        return json.dumps(result, indent=2, default=str), is_error
    except (ToolError, HostUnavailableError) as exc:
        return json.dumps({"error": str(exc)}), True
    except Exception as exc:  # surfaced to the agent, not raised
        log.exception("tool %s failed", name)
        return json.dumps({"error": f"{type(exc).__name__}: {exc}", "tool": name}), True


def _build_mcp_server() -> Any:
    """Construct the `mcp` SDK server, or return None if the SDK is absent.

    Supports both SDK API generations:
      - mcp 1.x: ``Server(name)`` + ``@server.list_tools()`` / ``@server.call_tool()``
        decorators.
      - mcp 2.x: the decorators are gone; handlers are passed to the constructor
        as ``on_list_tools`` / ``on_call_tool`` and receive ``(ctx, params)``.
    Raises if the SDK is importable but neither API shape fits, so
    ``--self-check`` can report it instead of the server dying at startup.
    """
    try:
        import mcp.types as types
        from mcp.server import Server
    except Exception as exc:
        log.info("mcp SDK not available (%r); using JSON-RPC fallback.", exc)
        return None

    def tool_list() -> List[Any]:
        return [
            types.Tool(name=name, description=entry["description"], inputSchema=entry["schema"])
            for name, entry in TOOLS.items()
        ]

    async def run_tool(name: str, arguments: Optional[Dict[str, Any]]) -> Any:
        text, is_error = await asyncio.to_thread(_call_tool_text, name, arguments or {})
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)], isError=is_error
        )

    if hasattr(Server, "list_tools"):  # mcp 1.x decorator API
        server = Server("inspector-widget", version=_SERVER_INFO["version"])

        @server.list_tools()
        async def list_tools() -> List[Any]:  # type: ignore[misc]
            return tool_list()

        @server.call_tool()
        async def call_tool(name: str, arguments: Dict[str, Any]) -> Any:  # type: ignore[misc]
            return await run_tool(name, arguments)

        return server

    async def on_list_tools(ctx: Any, params: Any) -> Any:  # mcp 2.x handler API
        return types.ListToolsResult(tools=tool_list())

    async def on_call_tool(ctx: Any, params: Any) -> Any:
        return await run_tool(params.name, params.arguments)

    return Server(
        "inspector-widget",
        version=_SERVER_INFO["version"],
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def _serve_with_mcp() -> bool:
    """Try to serve using the real `mcp` SDK. Returns True if it ran."""
    server = _build_mcp_server()
    if server is None:
        return False
    from mcp.server.stdio import stdio_server

    async def _main() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    asyncio.run(_main())
    return True


# --------------------------------------------------------------------------- #
# Fallback: minimal MCP-over-stdio (JSON-RPC 2.0). Used only when `mcp` is
# missing. Implements initialize / tools/list / tools/call and ignores
# notifications. Messages are newline-delimited JSON on stdin/stdout (line
# framing), which is what `claude mcp` and most MCP stdio clients accept.
# --------------------------------------------------------------------------- #
_PROTOCOL_VERSION = "2024-11-05"
_SERVER_INFO = {"name": "inspector-widget", "version": "1.0.0"}


def _jsonrpc_result(req_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _fallback_handle(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = message.get("method")
    req_id = message.get("id")
    params = message.get("params") or {}

    # Notifications (no id) get no response.
    if method == "notifications/initialized" or (method and method.startswith("notifications/")):
        return None

    if method == "initialize":
        return _jsonrpc_result(
            req_id,
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": _SERVER_INFO,
            },
        )

    if method == "ping":
        return _jsonrpc_result(req_id, {})

    if method == "tools/list":
        tools = [
            {
                "name": name,
                "description": entry["description"],
                "inputSchema": entry["schema"],
            }
            for name, entry in TOOLS.items()
        ]
        return _jsonrpc_result(req_id, {"tools": tools})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        text, is_error = _call_tool_text(name, arguments)
        return _jsonrpc_result(
            req_id, {"content": [{"type": "text", "text": text}], "isError": is_error}
        )

    if req_id is not None:
        return _jsonrpc_error(req_id, -32601, f"method not found: {method}")
    return None


def _serve_fallback() -> None:
    """Newline-delimited JSON-RPC 2.0 loop on stdin/stdout."""
    log.info("Serving Inspector Widget MCP via JSON-RPC stdio fallback (mcp SDK absent).")
    stdin = sys.stdin
    stdout = sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            err = _jsonrpc_error(None, -32700, "parse error")
            stdout.write(json.dumps(err) + "\n")
            stdout.flush()
            continue
        try:
            response = _fallback_handle(message)
        except Exception as exc:  # pragma: no cover
            response = _jsonrpc_error(
                message.get("id"), -32603, f"internal error: {exc}", traceback.format_exc()
            )
        if response is not None:
            stdout.write(json.dumps(response, default=str) + "\n")
            stdout.flush()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _self_check() -> int:
    """Print tool surface + dependency status; for diagnostics.

    Exits non-zero when something the server needs to start is broken (host
    package, proto gencode, or an installed-but-incompatible mcp SDK). Missing
    optional deps are reported with the tools they degrade, but don't fail.
    """
    failed = False
    print("Inspector Widget MCP server — self check")
    print(f"  tools ({len(TOOLS)}): {', '.join(TOOLS)}")
    try:
        HOST.host  # noqa: B018 - trigger import
        print("  inspector_widget: OK")
    except Exception as exc:
        failed = True
        print(f"  inspector_widget: UNAVAILABLE ({exc})")
    try:
        HOST.proto  # noqa: B018
        print("  view_inspection_pb2: OK")
    except Exception as exc:
        failed = True
        print(f"  view_inspection_pb2: UNAVAILABLE ({exc})")
    try:
        server = _build_mcp_server()
    except Exception as exc:
        failed = True
        print(f"  mcp SDK: INCOMPATIBLE ({type(exc).__name__}: {exc})")
    else:
        if server is None:
            print("  mcp SDK: absent (will use JSON-RPC stdio fallback)")
        else:
            print(f"  mcp SDK: {_dist_version('mcp')} OK (real MCP transport)")
    for dist, problem, degraded in _probe_optional_deps():
        if problem is None:
            print(f"  {dist}: {_dist_version(dist)} OK")
        else:
            print(f"  {dist}: {problem} — degrades: {degraded}")
    return 1 if failed else 0


def _probe_optional_deps() -> List[Tuple[str, Optional[str], str]]:
    """``[(dist, problem_or_None, what_it_degrades)]`` for the optional deps.

    grpcio is probed by loading the SKP gRPC stubs, not just ``import grpc``:
    the stubs refuse to load on a grpcio older than the one they were generated
    with, so a bare import would report an unusable grpcio as OK.
    """
    out: List[Tuple[str, Optional[str], str]] = []
    try:
        import PIL  # noqa: F401

        pil_problem = None
    except Exception:
        pil_problem = "MISSING (pip install Pillow)"
    out.append(("Pillow", pil_problem,
                "compose_overlay, a11y_overlay, inspect overlay, component_image crop fallback"))
    try:
        from inspector_widget import skia_client

        skia_client._import_skia_grpc()
        grpc_problem = None
    except Exception as exc:
        grpc_problem = f"UNUSABLE ({exc})"
    out.append(("grpcio", grpc_problem,
                "component_image SKP rendering (falls back to a screenshot crop)"))
    return out


def _dist_version(dist: str) -> str:
    try:
        from importlib.metadata import version

        return version(dist)
    except Exception:
        return "unknown version"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="inspector-widget-mcp",
        description="Inspector Widget MCP server (Android View Layout Inspector for LLM agents).",
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="Print the tool surface and import status, then exit.",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("INSPECTOR_WIDGET_LOG") or os.environ.get("VIEWSPECTOR_LOG", "WARNING"),
        help="Logging level (DEBUG/INFO/WARNING/ERROR). Logs go to stderr.",
    )
    args = parser.parse_args(argv)

    # All logging MUST go to stderr — stdout is the MCP transport.
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.WARNING),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.self_check:
        rc = _self_check()
        # Artifact status is informational: a missing artifact warns, never fails.
        print("\n".join(_artifact_report()))
        return rc

    _log_startup_health()

    if not _serve_with_mcp():
        _serve_fallback()
    return 0


def _log_startup_health() -> None:
    """Emit one INFO health line at startup: tool count, host/proto + optional
    deps (Pillow, grpcio, mcp SDK) and the transport that will be used. Goes to
    stderr (stdout is the MCP transport). Best-effort; never raises."""
    def _ok(fn) -> str:
        try:
            fn()
            return "ok"
        except Exception as exc:  # noqa: BLE001
            return f"UNAVAILABLE({type(exc).__name__})"

    host_status = _ok(lambda: HOST.host)
    proto_status = _ok(lambda: HOST.proto)

    def _present(mod: str) -> str:
        try:
            __import__(mod)
            return "present"
        except Exception:
            return "absent"

    deps = {dist: ("present" if problem is None else "unusable")
            for dist, problem, _ in _probe_optional_deps()}
    pillow = deps["Pillow"]
    grpcio = deps["grpcio"]
    mcp_sdk = _present("mcp")
    transport = "mcp-sdk" if mcp_sdk == "present" else "jsonrpc-fallback"
    log.info(
        "Inspector Widget MCP starting: %d tools | host=%s proto=%s | "
        "Pillow(overlays)=%s grpcio(SKP images)=%s mcp-sdk=%s | transport=%s",
        len(TOOLS), host_status, proto_status, pillow, grpcio, mcp_sdk, transport,
    )
    try:
        from inspector_widget import inject

        st = inject.artifact_status()
        if st["missing"]:
            log.warning(
                "on-device artifacts missing from %s (%s): %s. Injecting will fail; "
                "set %s to a directory built by scripts/build.sh.",
                st["dir"], st["source"], ", ".join(st["missing"]), inject.ARTIFACTS_ENV,
            )
    except Exception:  # noqa: BLE001 - health logging must never block startup
        pass


def _artifact_report() -> List[str]:
    """Self-check lines: where the on-device artifacts are looked up, and which exist.

    The directory comes from ``$INSPECTOR_WIDGET_ARTIFACTS``, then the legacy
    ``$VIEWSPECTOR_ARTIFACTS``, then the checkout's ``build-out/``. A missing
    artifact is only a warning: re-attaching to an app that already has the
    agent loaded does not need them.
    """
    try:
        from inspector_widget import inject

        st = inject.artifact_status()
    except Exception as exc:  # noqa: BLE001
        return [f"  artifacts: UNKNOWN (inspector_widget.inject unavailable: {exc})"]
    lines = [f"  artifacts: {st['dir']} (from {st['source']})"]
    lines += [f"    {name}: {'OK' if ok else 'MISSING'}" for name, ok in st["present"].items()]
    if st["missing"]:
        lines.append(
            f"  WARNING: {len(st['missing'])} of {len(st['present'])} artifacts missing; "
            f"injecting an app will fail. Run scripts/build.sh and set "
            f"{inject.ARTIFACTS_ENV}=<checkout>/build-out."
        )
    return lines


if __name__ == "__main__":
    raise SystemExit(main())

