"""Offline scenes: the 259-view wide screen and replays of real recorded screens.

A :class:`SceneData` answers agent ``Request`` protobufs with ``Response``
protobufs, the way the on-device payload does (Dispatcher.kt). The same scene can
be used three ways:

* ``scene.behaviour`` is a ``behaviour(req) -> (delay_s, pb.Response)`` hook for
  the offline e2e harness (``tests/fakeagent.py`` on improve/offline-e2e-harness):
  ``FakeAgent(behaviour=scene.behaviour)`` or ``FakeDevice.behaviour = ...``.
  The real adb, inject, framing and client code then runs over TCP.
* ``scene.session()`` returns a :class:`SceneSession`, an in-process stand-in
  for ``inspector_widget.Session`` with the same public methods and return types.
  Library code (capture fetch, correlate, the MCP tool bodies) can run on it with
  no sockets. Each request and response is serialized and parsed again, so what
  crosses is exactly what the wire would carry.
* ``scene.respond(req)`` for direct unit tests.

Scenes:

* :func:`wide_scene` reproduces the E6 fixture (``scratchpad/entrypoints/
  fakeagent.py``). It has one window: a LinearLayout tree of depth 0..3 with
  fan 6, giving 259 Views. The 216 TextView leaves are labelled "Label N"
  (``com.example:id/view_N``), with bounds ``x=N, y=3N, 300x60``. Each view has
  about 60 properties encoded like Properties.kt (10 real ones plus
  ``attr_0..attr_49`` INT32). The a11y tree mirrors the Views as clickable,
  focusable TextViews with actions 16/4/8/64/128. There is a single "Root"
  semantics node and an 8x8 screenshot.
* :func:`replay_scene` ``("launcher" | "viewscreen")`` rebuilds protobuf
  responses from the real outputs in ``tests/fixtures/live`` (see
  ``live_fixtures.py`` for provenance; pre-ID1 data). Screenshots come from the
  recorded PNGs (``screen.png``, 1280x2856), re-encoded as the agent sends them:
  ABGR_8888 (in-memory R,G,B,A), a 9-byte little-endian header, deflated
  (Capture.kt).

  - launcher: adb ``screencap`` of MainActivity, with views and properties from
    the legacy MCP ``dump_tree`` shape (E3 values kept as recorded), Compose
    semantics plus slot table, and a11y.
  - viewscreen: ViewScenarioActivity, with the strings.py shapes and no Compose.

  The slot table is served when ``include_slot_table`` is set and the scene's
  inspection is on: ``slots_populated=True`` (as recorded) or after a request
  with ``enable_inspection``. Unlike the device, enabling inspection does not
  re-mint semantics ids.

Converters (``views_to_pb``, ``compose_to_pb``, ``a11y_to_pb``,
``png_to_screenshot``) accept the strings.py and a11y.py dict shapes and the
legacy MCP ``dump_tree`` shape (flat bounds, ``render_quad``, ``resource.ref``,
list-of-groups properties with COLOR as ``#AARRGGBB``).
"""

from __future__ import annotations

import functools
import io
import itertools
import struct
import zlib
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import live_fixtures as lf

from inspector_widget.proto import view_inspection_pb2 as pb

P = pb.Property
AGENT_VERSION = "viewspector-0.1"
BITMAP_ABGR_8888 = 2
Behaviour = Callable[["pb.Request"], tuple]


class SceneError(RuntimeError):
    """Raised by SceneSession when the scene answers ERROR (like client.ClientError)."""


# --------------------------------------------------------------------------- #
# String table
# --------------------------------------------------------------------------- #
class Strings:
    """StringTable.kt: ids from 1; 0 = absent (never emitted)."""

    def __init__(self) -> None:
        self._ids: dict[str, int] = {}

    def id(self, s: Any) -> int:
        if s is None or s == "":
            return 0
        s = str(s)
        if s not in self._ids:
            self._ids[s] = len(self._ids) + 1
        return self._ids[s]

    def fill(self, msg: pb.Strings) -> None:
        msg.SetInParent()
        for s, i in self._ids.items():
            msg.entries.add(id=i, str=s)


# --------------------------------------------------------------------------- #
# JSON -> protobuf converters
# --------------------------------------------------------------------------- #
def _set_bounds(st: Strings, b: Any, out: pb.Bounds) -> None:
    out.SetInParent()
    if not isinstance(b, Mapping):
        return
    layout = b.get("layout", b)
    out.layout.x = int(layout.get("x", 0))
    out.layout.y = int(layout.get("y", 0))
    out.layout.w = int(layout.get("w", 0))
    out.layout.h = int(layout.get("h", 0))
    quad = b.get("render")
    if isinstance(quad, Mapping):
        for k in ("x0", "y0", "x1", "y1", "x2", "y2", "x3", "y3"):
            setattr(out.render, k, int(quad.get(k, 0)))
    elif isinstance(b.get("render_quad"), list):  # legacy [[x0,y0],...,[x3,y3]]
        for i, (x, y) in enumerate(b["render_quad"][:4]):
            setattr(out.render, f"x{i}", int(x))
            setattr(out.render, f"y{i}", int(y))


def _set_resource(st: Strings, res: Any, out: pb.Resource) -> None:
    if not isinstance(res, Mapping):
        return
    out.type = st.id(res.get("type"))
    out.namespace = st.id(res.get("namespace"))
    out.name = st.id(res.get("name"))


def _signed32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


def encode_property(st: Strings, p: Mapping[str, Any], out: pb.Property) -> None:
    """One property dict -> Property, encoded like PropAccumulator.build."""
    type_name = p.get("type") or "STRING"
    t = P.Type.Value(type_name) if type_name in P.Type.DESCRIPTOR.values_by_name else P.STRING
    out.name = st.id(p.get("name"))
    out.type = t
    out.is_layout = bool(p.get("is_layout", False))
    v = p.get("value")
    if t in (P.STRING, P.OBJECT, P.INT_ENUM):
        out.str_value = st.id(v)
    elif t == P.BOOLEAN:
        out.int32_value = 1 if v else 0
    elif t == P.COLOR:
        out.int32_value = _signed32(int(v[1:], 16)) if isinstance(v, str) else int(v or 0)
    elif t in (P.GRAVITY, P.INT_FLAG):
        if isinstance(v, str):  # strings.py: the flag string is the value
            out.str_value = st.id(v)
        else:  # older shapes: an int (0 as legacy MCP recorded it) plus a label
            out.int32_value = int(v or 0)
            out.str_value = st.id(p.get("label"))
    elif t in (P.BYTE, P.CHAR, P.INT16, P.INT32, P.DIMENSION):
        out.int32_value = round(v) if isinstance(v, float) else int(v or 0)
        if p.get("label") is not None:
            out.str_value = st.id(p["label"])
    elif t == P.INT64:
        out.int64_value = int(v or 0)
    elif t == P.DOUBLE:
        out.double_value = float(v or 0.0)
    elif t == P.FLOAT:
        out.float_value = float(v or 0.0)
    elif t in (P.RESOURCE, P.DRAWABLE, P.ANIM, P.ANIMATOR, P.INTERPOLATOR):
        if isinstance(v, Mapping):
            _set_resource(st, v, out.resource_value)
        elif v is not None:
            out.str_value = st.id(v)
    if p.get("source"):
        out.source = st.id(p["source"])
    for s in p.get("resolution_stack") or []:
        out.resolution_stack.append(st.id(s))


def _encode_view(st: Strings, n: Mapping[str, Any], out: pb.ViewNode) -> None:
    out.id = int(n["id"])
    cls = n.get("class_name") or ""
    pkg = n.get("package_name")
    q = n.get("qualified_name")
    if pkg is None and q and q.endswith("." + cls):
        pkg = q[: -len(cls) - 1]
    out.class_name = st.id(cls)
    out.package_name = st.id(pkg)
    _set_bounds(st, n.get("bounds"), out.bounds)
    _set_resource(st, n.get("resource"), out.resource)
    _set_resource(st, n.get("layout_resource"), out.layout_resource)
    out.view_id_name = st.id(n.get("view_id_name"))
    out.text_value = st.id(n.get("text"))
    if "IS_WEBVIEW" in (n.get("flags") or []):
        out.flags = pb.ViewNode.IS_WEBVIEW
    for c in n.get("children") or []:
        _encode_view(st, c, out.children.add())


def _groups(props: Any) -> list[tuple[int, list]]:
    if isinstance(props, Mapping):
        return [(int(k), list(v or [])) for k, v in props.items()]
    return [(int(g["view_id"]), list(g.get("properties") or [])) for g in props or []]


def views_to_pb(data: Mapping[str, Any]) -> pb.DumpTreeResponse:
    """A dump_tree result (strings.py or legacy MCP shape) -> DumpTreeResponse.

    Properties are included when present; the screenshot summary is not (use
    :func:`png_to_screenshot`)."""
    st = Strings()
    resp = pb.DumpTreeResponse()
    for r in data.get("roots") or []:
        _encode_view(st, r, resp.roots.add())
    for vid, plist in _groups(data.get("properties")):
        g = resp.properties.add()
        g.view_id = vid
        for p in plist:
            encode_property(st, p, g.properties.add())
    st.fill(resp.strings)
    return resp


def _encode_compose(st: Strings, n: Mapping[str, Any], out: pb.ComposeNode) -> None:
    out.id = int(n.get("id", 0))
    out.name = st.id(n.get("name"))
    out.kind = pb.ComposeNode.SEMANTICS if n.get("kind") == "SEMANTICS" else pb.ComposeNode.COMPOSABLE
    if n.get("render_node_id"):
        out.render_node_id = int(n["render_node_id"])
    _set_bounds(st, n.get("bounds"), out.bounds)
    out.source = st.id(n.get("source"))
    for k, v in (n.get("attrs") or {}).items():
        out.attrs.add(key=st.id(k), value=st.id(v))
    for c in n.get("children") or []:
        _encode_compose(st, c, out.children.add())


def compose_to_pb(data: Mapping[str, Any]) -> pb.DumpComposeResponse:
    """A dump_compose result (strings.dump_compose_to_dict shape) -> DumpComposeResponse."""
    st = Strings()
    resp = pb.DumpComposeResponse()
    for w in data.get("windows") or []:
        win = resp.windows.add()
        win.view_id = int(w.get("view_id") or 0)
        if w.get("root") is not None:
            _encode_compose(st, w["root"], win.root)
    if data.get("diagnostics"):
        resp.diagnostics = str(data["diagnostics"])
    st.fill(resp.strings)
    return resp


# a11y.py decode tables, reversed (kept local: a11y.py is being reworked elsewhere)
_A11Y_TEXT = ("text", "content_description", "hint_text", "state_description", "error",
              "tooltip_text", "pane_title", "container_title", "supplemental_description",
              "role_description", "class_name", "package_name", "view_id_resource_name",
              "unique_id", "provider_class")
_A11Y_BOOL = tuple(f.name for f in pb.A11yNode.DESCRIPTOR.fields if f.type == f.TYPE_BOOL)
_A11Y_INT = ("input_type", "movement_granularities", "max_text_length", "drawing_order",
             "text_selection_start", "text_selection_end", "actions_bitmask")
_A11Y_ENUMS = {
    "checked_state": {"FALSE": 0, "TRUE": 1, "PARTIAL": 2},
    "live_region": {"NONE": 0, "POLITE": 1, "ASSERTIVE": 2},
    "important_for_accessibility": {"AUTO": 0, "YES": 1, "NO": 2, "NO_HIDE_DESCENDANTS": 4},
    "expanded_state": {"UNDEFINED": 0, "COLLAPSED": 1, "PARTIAL": 2, "FULL": 3},
}
_RANGE_TYPES = {"INT": 0, "FLOAT": 1, "PERCENT": 2, "INDETERMINATE": 3}


def _encode_a11y(st: Strings, n: Mapping[str, Any], out: pb.A11yNode) -> None:
    out.host_view_id = int(n.get("host_view_id", 0))
    out.virtual_id = int(n.get("virtual_id", -1))
    _set_bounds(st, n.get("bounds"), out.bounds)
    for f in _A11Y_TEXT:
        setattr(out, f, st.id(n.get(f)))
    flags = set(n.get("flags") or [])
    for f in _A11Y_BOOL:
        if f in flags:
            setattr(out, f, True)
    for f in _A11Y_INT:
        if n.get(f) is not None:
            setattr(out, f, int(n[f]))
    for f, table in _A11Y_ENUMS.items():
        v = n.get(f)
        if v is not None:
            setattr(out, f, table.get(v, v) if isinstance(v, str) else int(v))
    ci = n.get("collection_info")
    if isinstance(ci, Mapping):
        out.collection_info.SetInParent()
        for k, v in ci.items():
            setattr(out.collection_info, k, v)
    it = n.get("collection_item_info")
    if isinstance(it, Mapping):
        out.collection_item_info.SetInParent()
        for k, v in it.items():
            if k in ("row_title", "column_title"):
                setattr(out.collection_item_info, k, st.id(v))
            else:
                setattr(out.collection_item_info, k, v)
    ri = n.get("range_info")
    if isinstance(ri, Mapping):
        out.range_info.SetInParent()
        t = ri.get("type")
        out.range_info.type = _RANGE_TYPES.get(t, t) if isinstance(t, str) else int(t or 0)
        for k in ("min", "max", "current"):
            setattr(out.range_info, k, float(ri.get(k, 0.0)))
    for a in n.get("actions") or []:
        out.actions.add(id=int(a["id"]), label=st.id(a.get("label")))
    for k, v in (n.get("extras") or {}).items():
        out.extras.add(key=st.id(k), value=st.id(v))
    for f in ("traversal_before", "traversal_after", "label_for", "labeled_by"):
        if n.get(f):
            setattr(out, f, int(n[f]))
    out.labeled_by_list.extend(int(x) for x in n.get("labeled_by_list") or [])
    if n.get("text_size_px"):
        out.text_size_px = float(n["text_size_px"])
    if n.get("text_size_unit"):
        out.text_size_unit = int(n["text_size_unit"])
    size = n.get("layout_size")
    if isinstance(size, Mapping):
        out.layout_size_w = int(size.get("w", 0))
        out.layout_size_h = int(size.get("h", 0))
    for c in n.get("children") or []:
        _encode_a11y(st, c, out.children.add())


def a11y_to_pb(data: Mapping[str, Any]) -> pb.DumpA11yResponse:
    """An a11y_to_dict result -> DumpA11yResponse (``id``/``speakable``/
    ``focus_order`` are derived host-side and not encoded)."""
    st = Strings()
    resp = pb.DumpA11yResponse()
    for w in data.get("windows") or []:
        win = resp.windows.add()
        win.root_view_id = int(w.get("root_view_id") or 0)
        if w.get("root") is not None:
            _encode_a11y(st, w["root"], win.root)
    if data.get("diagnostics"):
        resp.diagnostics = str(data["diagnostics"])
    st.fill(resp.strings)
    return resp


def rgba_to_screenshot(width: int, height: int, rgba: bytes, scale: float = 1.0
                       ) -> pb.Screenshot:
    """RGBA8888 pixels -> Screenshot as Capture.kt sends it: ABGR_8888 (in-memory
    R,G,B,A), a 9-byte LE header [w int32, h int32, type byte], deflated."""
    raw = struct.pack("<iiB", width, height, BITMAP_ABGR_8888) + bytes(rgba)
    return pb.Screenshot(format=pb.Screenshot.BITMAP, width=width, height=height,
                         bitmap_type=BITMAP_ABGR_8888, data=zlib.compress(raw, 1),
                         scale=float(scale))


def png_to_screenshot(png: bytes | str, scale: float = 1.0) -> pb.Screenshot:
    """A PNG (bytes or path) -> Screenshot; ``scale`` < 1 downsamples like the agent."""
    from PIL import Image

    if isinstance(png, bytes):
        data = png
    else:
        with open(png, "rb") as f:
            data = f.read()
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    if 0 < scale < 1.0:
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                         Image.BILINEAR)
    return rgba_to_screenshot(img.width, img.height, img.tobytes(), scale)


# --------------------------------------------------------------------------- #
# The scene: requests in, responses out
# --------------------------------------------------------------------------- #
def _error(req_id: int, message: str) -> pb.Response:
    return pb.Response(id=req_id, status=pb.Response.ERROR, error=message)


def _view_ids(roots: Iterable[pb.ViewNode]) -> set[int]:
    out: set[int] = set()
    stack = list(roots)
    while stack:
        n = stack.pop()
        out.add(n.id)
        stack.extend(n.children)
    return out


class SceneData:
    """A replayable screen: full responses kept once, request-specific views derived."""

    def __init__(self, name: str, *, views: pb.DumpTreeResponse, a11y: pb.DumpA11yResponse,
                 compose_sem: pb.DumpComposeResponse | None = None,
                 compose_slots: pb.DumpComposeResponse | None = None,
                 screens: Mapping[int, Callable[[float], pb.Screenshot]] | None = None,
                 window_ids: list[int] | None = None, api_level: int = 37,
                 abi: str = "arm64-v8a", slots_populated: bool = False) -> None:
        self.name = name
        self.views = views
        self.a11y = a11y
        self.compose_sem = compose_sem or pb.DumpComposeResponse()
        self.compose_slots = compose_slots
        self.window_ids = list(window_ids) if window_ids else [r.id for r in views.roots]
        self._screens = dict(screens or {})
        self.api_level = api_level
        self.abi = abi
        self.inspection_enabled = slots_populated
        self.requests: list[str] = []

    # ---- helpers ---------------------------------------------------------- #
    @property
    def behaviour(self) -> Behaviour:
        """``behaviour(req) -> (0.0, response)`` for the harness FakeAgent."""
        return lambda req: (0.0, self.respond(req))

    def session(self, serial: str = "emulator-5554", package: str = "com.oberkfell.a11yprobe",
                pid: int = 4312) -> SceneSession:
        return SceneSession(self, serial=serial, package=package, pid=pid)

    def screenshot(self, root_id: int = 0, scale: float = 1.0) -> pb.Screenshot | None:
        root = root_id or (self.window_ids[0] if self.window_ids else 0)
        make = self._screens.get(root)
        return make(scale) if make else None

    # ---- dispatcher ------------------------------------------------------- #
    def respond(self, req: pb.Request) -> pb.Response:
        command = req.WhichOneof("command")
        self.requests.append(command or "<unset>")
        if command is None:
            return _error(req.id, "No command set in request")
        try:
            return getattr(self, f"_h_{command}")(req.id, getattr(req, command))
        except Exception as exc:  # noqa: BLE001 - the agent turns handler failures into ERROR
            return _error(req.id, f"{type(exc).__name__}: {exc}")

    def _ok(self, req_id: int) -> pb.Response:
        return pb.Response(id=req_id, status=pb.Response.OK)

    def _h_hello(self, req_id: int, cmd: Any) -> pb.Response:
        resp = self._ok(req_id)
        resp.hello.agent_version = AGENT_VERSION
        resp.hello.api_level = self.api_level
        resp.hello.abi = self.abi
        return resp

    def _h_get_windows(self, req_id: int, cmd: Any) -> pb.Response:
        resp = self._ok(req_id)
        resp.get_windows.root_ids.extend(self.window_ids)
        resp.get_windows.strings.SetInParent()
        return resp

    def _h_dump_tree(self, req_id: int, cmd: pb.DumpTreeCommand) -> pb.Response:
        resp = self._ok(req_id)
        out = resp.dump_tree
        out.strings.CopyFrom(self.views.strings)
        roots = [r for r in self.views.roots if not cmd.root_id or r.id == cmd.root_id]
        for r in roots:
            out.roots.add().CopyFrom(r)
        if cmd.include_properties:
            keep = _view_ids(roots)
            for g in self.views.properties:
                if g.view_id in keep:
                    g2 = out.properties.add()
                    g2.CopyFrom(g)
                    if not cmd.include_resolution_stack:
                        for p in g2.properties:
                            p.ClearField("source")
                            del p.resolution_stack[:]
        if cmd.include_screenshot:
            shot = self.screenshot(roots[0].id if roots else 0, cmd.screenshot_scale or 1.0)
            if shot is not None:
                out.screenshot.CopyFrom(shot)
        return resp

    def _h_get_properties(self, req_id: int, cmd: pb.GetPropertiesCommand) -> pb.Response:
        for g in self.views.properties:
            if g.view_id == cmd.view_id:
                resp = self._ok(req_id)
                resp.get_properties.group.CopyFrom(g)
                if not cmd.include_resolution_stack:
                    for p in resp.get_properties.group.properties:
                        p.ClearField("source")
                        del p.resolution_stack[:]
                resp.get_properties.strings.CopyFrom(self.views.strings)
                return resp
        return _error(req_id, f"No view found with id {cmd.view_id}")

    def _h_screenshot(self, req_id: int, cmd: pb.ScreenshotCommand) -> pb.Response:
        shot = self.screenshot(cmd.root_id, cmd.scale or 1.0)
        if shot is None:
            return _error(req_id, f"No window with root id {cmd.root_id}")
        resp = self._ok(req_id)
        resp.screenshot.screenshot.CopyFrom(shot)
        return resp

    def _h_dump_compose(self, req_id: int, cmd: pb.DumpComposeCommand) -> pb.Response:
        if cmd.enable_inspection:
            self.inspection_enabled = True
        want_slots = cmd.include_slot_table and self.inspection_enabled and \
            self.compose_slots is not None
        src = self.compose_slots if want_slots else self.compose_sem
        resp = self._ok(req_id)
        out = resp.dump_compose
        out.strings.CopyFrom(src.strings)
        out.diagnostics = src.diagnostics
        for w in src.windows:
            if cmd.root_view_id and w.view_id != cmd.root_view_id:
                continue
            w2 = out.windows.add()
            w2.CopyFrom(w)
            if not cmd.include_semantics and w2.HasField("root"):
                kept = [c for c in w2.root.children if c.kind != pb.ComposeNode.SEMANTICS]
                del w2.root.children[:]
                for c in kept:
                    w2.root.children.add().CopyFrom(c)
        return resp

    def _h_dump_a11y(self, req_id: int, cmd: pb.DumpA11yCommand) -> pb.Response:
        resp = self._ok(req_id)
        out = resp.dump_a11y
        out.strings.CopyFrom(self.a11y.strings)
        out.diagnostics = self.a11y.diagnostics
        for w in self.a11y.windows:
            if cmd.root_id and w.root_view_id != cmd.root_id:
                continue
            w2 = out.windows.add()
            w2.CopyFrom(w)
            if not cmd.include_extras:
                stack = [w2.root]
                while stack:
                    n = stack.pop()
                    del n.extras[:]
                    stack.extend(n.children)
        return resp

    def _h_capture_skp(self, req_id: int, cmd: Any) -> pb.Response:
        resp = self._ok(req_id)
        resp.capture_skp.supported = False
        resp.capture_skp.error = "no SKP recorded for this scene"
        return resp

    def _h_shutdown(self, req_id: int, cmd: Any) -> pb.Response:
        resp = self._ok(req_id)
        resp.shutdown.SetInParent()
        return resp


class SceneSession:
    """In-process stand-in for ``inspector_widget.Session`` over a SceneData.

    Same method names, keywords and return types (the response payload
    messages); an ERROR raises :class:`SceneError`. ``close()``/``detach()``
    only mark it closed (``closed`` is what mcp_server's liveness probe reads).
    """

    def __init__(self, scene: SceneData, *, serial: str, package: str, pid: int) -> None:
        self.scene = scene
        self.serial = serial
        self.package = package
        self.pid = pid
        self.api_level = scene.api_level
        self.abi = scene.abi
        self.agent_version = AGENT_VERSION
        self.build_id = None
        self.closed = False
        self._ids = itertools.count(1)

    def _send(self, req: pb.Request) -> pb.Response:
        req.id = next(self._ids)
        wire = pb.Request()
        wire.ParseFromString(req.SerializeToString())
        resp = pb.Response()
        resp.ParseFromString(self.scene.respond(wire).SerializeToString())
        if resp.status == pb.Response.ERROR:
            raise SceneError(f"agent error (request id {resp.id}): {resp.error}")
        return resp

    def hello(self) -> pb.HelloResponse:
        req = pb.Request()
        req.hello.SetInParent()
        return self._send(req).hello

    def get_windows(self) -> pb.GetWindowsResponse:
        req = pb.Request()
        req.get_windows.SetInParent()
        return self._send(req).get_windows

    def dump_tree(self, root_id: int = 0, include_properties: bool = False,
                  include_resolution_stack: bool = False, include_screenshot: bool = False,
                  screenshot_scale: float = 1.0) -> pb.DumpTreeResponse:
        req = pb.Request()
        c = req.dump_tree
        c.root_id = root_id
        c.include_properties = include_properties
        c.include_resolution_stack = include_resolution_stack and include_properties
        c.include_screenshot = include_screenshot
        c.screenshot_scale = screenshot_scale
        return self._send(req).dump_tree

    def get_properties(self, view_id: int, include_resolution_stack: bool = False
                       ) -> pb.GetPropertiesResponse:
        req = pb.Request()
        req.get_properties.view_id = view_id
        req.get_properties.include_resolution_stack = include_resolution_stack
        return self._send(req).get_properties

    def screenshot(self, root_id: int = 0, scale: float = 1.0) -> pb.ScreenshotResponse:
        req = pb.Request()
        req.screenshot.root_id = root_id
        req.screenshot.scale = scale
        return self._send(req).screenshot

    def dump_compose(self, root_view_id: int = 0, include_semantics: bool = True,
                     include_slot_table: bool = True, enable_inspection: bool = False
                     ) -> pb.DumpComposeResponse:
        req = pb.Request()
        c = req.dump_compose
        c.root_view_id = root_view_id
        c.include_semantics = include_semantics
        c.include_slot_table = include_slot_table
        c.enable_inspection = enable_inspection
        return self._send(req).dump_compose

    def dump_a11y(self, root_id: int = 0, include_extras: bool = True,
                  include_rendering_info: bool = False) -> pb.DumpA11yResponse:
        req = pb.Request()
        c = req.dump_a11y
        c.root_id = root_id
        c.include_extras = include_extras
        c.include_rendering_info = include_rendering_info
        return self._send(req).dump_a11y

    def capture_skp(self, root_id: int = 0) -> pb.CaptureSkpResponse:
        req = pb.Request()
        req.capture_skp.root_id = root_id
        return self._send(req).capture_skp

    def close(self) -> None:
        self.closed = True

    detach = close
    shutdown = close


# --------------------------------------------------------------------------- #
# Scenes
# --------------------------------------------------------------------------- #
def _e6_props(st: Strings, group: pb.PropertyGroup, vid: int) -> None:
    """The E6 fixture's ~60 properties per view (encoded like Properties.kt)."""
    group.view_id = vid

    def add(name: str, typ: int, **kw: Any) -> None:
        p = group.properties.add(name=st.id(name), type=typ)
        for k, v in kw.items():
            setattr(p, k, st.id(v) if k == "str_value" else v)

    add("gravity", P.GRAVITY, str_value="center_vertical|start")
    add("importantForAccessibility", P.INT_FLAG, str_value="auto")
    add("layout_width", P.DIMENSION, int32_value=1080, is_layout=True)
    add("paddingStart", P.DIMENSION, int32_value=42)
    add("textColor", P.COLOR, int32_value=-16777216)
    add("text", P.STRING, str_value="Hello world")
    add("visibility", P.INT_ENUM, str_value="visible")
    add("background", P.DRAWABLE, str_value="android.graphics.drawable.ColorDrawable")
    add("alpha", P.FLOAT, float_value=1.0)
    add("enabled", P.BOOLEAN, int32_value=1)
    for i in range(50):
        add(f"attr_{i}", P.INT32, int32_value=i)


def wide_scene(fan: int = 6, depth: int = 3) -> SceneData:
    """The E6 wide screen: (fan^(depth+1)-1)/(fan-1) Views (259 for fan 6)."""
    st = Strings()
    views = pb.DumpTreeResponse()
    counter = [0]

    def build(node: pb.ViewNode, level: int) -> None:
        counter[0] += 1
        n = counter[0]
        node.id = 1000 + n
        node.class_name = st.id("LinearLayout" if level < depth else "TextView")
        node.package_name = st.id("android.widget")
        node.bounds.layout.x, node.bounds.layout.y = n, 3 * n
        node.bounds.layout.w, node.bounds.layout.h = 300, 60
        node.resource.type = st.id("id")
        node.resource.name = st.id(f"view_{n}")
        node.resource.namespace = st.id("com.example")
        if level >= depth:
            node.text_value = st.id(f"Label {n}")
        else:
            for _ in range(fan):
                build(node.children.add(), level + 1)

    build(views.roots.add(), 0)
    for i in range(counter[0]):
        _e6_props(st, views.properties.add(), 1001 + i)
    st.fill(views.strings)

    ast = Strings()
    a11y = pb.DumpA11yResponse()
    win = a11y.windows.add()
    win.root_view_id = 1001
    acounter = [0]

    def build_a11y(node: pb.A11yNode, level: int) -> None:
        acounter[0] += 1
        n = acounter[0]
        node.host_view_id, node.virtual_id = 1000 + n, -1
        node.bounds.layout.x, node.bounds.layout.y = n, 3 * n
        node.bounds.layout.w, node.bounds.layout.h = 300, 60
        node.class_name = ast.id("android.widget.TextView")
        node.package_name = ast.id("com.example")
        node.text = ast.id(f"Label {n}")
        node.view_id_resource_name = ast.id(f"com.example:id/view_{n}")
        node.clickable = node.focusable = node.enabled = node.visible_to_user = True
        node.important_for_accessibility = 1
        for aid in (16, 4, 8, 64, 128):
            node.actions.add(id=aid)
        if level < depth:
            for _ in range(fan):
                build_a11y(node.children.add(), level + 1)

    build_a11y(win.root, 0)
    ast.fill(a11y.strings)

    cst = Strings()
    comp = pb.DumpComposeResponse()
    cw = comp.windows.add()
    cw.view_id = 1001
    cw.root.id = 1
    cw.root.name = cst.id("Root")
    cw.root.kind = pb.ComposeNode.SEMANTICS
    cst.fill(comp.strings)

    def shot(scale: float) -> pb.Screenshot:
        # one flat colour over the whole root window (300x60 at (1, 3)), so every
        # node the window shows has pixels under it
        w, h = max(1, int(300 * scale)), max(1, int(60 * scale))
        return rgba_to_screenshot(w, h, bytes([10, 20, 30, 255]) * (w * h), scale)

    return SceneData("wide", views=views, a11y=a11y, compose_sem=comp, api_level=36,
                     screens={1001: shot})


def _strip_mcp_keys(d: Mapping[str, Any]) -> dict:
    return {k: v for k, v in d.items() if k not in ("serial", "package", "note")}


@functools.cache
def _png_bytes(screen: str) -> bytes:
    with open(lf.path(screen, "screen.png"), "rb") as f:
        return f.read()


@functools.lru_cache(maxsize=8)
def _cached_shot(screen: str, scale: float) -> bytes:
    return png_to_screenshot(_png_bytes(screen), scale).SerializeToString()


def _shot_factory(screen: str) -> Callable[[float], pb.Screenshot]:
    def make(scale: float) -> pb.Screenshot:
        shot = pb.Screenshot()
        shot.ParseFromString(_cached_shot(screen, round(float(scale), 4)))
        return shot
    return make


def replay_scene(name: str, *, slots_populated: bool = True) -> SceneData:
    """A recorded real screen (``launcher`` or ``viewscreen``) as a SceneData."""
    if name == "launcher":
        views = views_to_pb(lf.load("launcher", "views_props"))
        compose_sem = compose_to_pb(_strip_mcp_keys(lf.load("launcher", "compose_sem")))
        compose_slots = compose_to_pb(_strip_mcp_keys(lf.load("launcher", "compose_slots")))
        a11y = a11y_to_pb(_strip_mcp_keys(lf.load("launcher", "a11y")))
    elif name == "viewscreen":
        views = views_to_pb(lf.load("viewscreen", "views_props"))
        compose_sem = pb.DumpComposeResponse(diagnostics="found 0 AndroidComposeView(s)")
        compose_slots = None
        a11y = a11y_to_pb(lf.load("viewscreen", "a11y"))
    else:
        raise ValueError(f"unknown scene {name!r} (launcher | viewscreen)")
    roots = [r.id for r in views.roots]
    return SceneData(name, views=views, a11y=a11y, compose_sem=compose_sem,
                     compose_slots=compose_slots, window_ids=roots,
                     screens={roots[0]: _shot_factory(name)}, api_level=37,
                     slots_populated=slots_populated)


def replay_behaviour(name: str, build_id: str | None = None) -> Behaviour:
    """The harness behaviour hook for a scene name: ``launcher``, ``viewscreen`` or
    ``wide``. ``build_id`` is the build its Hello reports (``viewspector-0.1+<id>``,
    the build handshake): pass the harness device's ``default_build_id`` so the
    host accepts the agent as the one it injected. Without it Hello answers as an
    agent from before the handshake, which the host refuses to use."""
    scene = wide_scene() if name == "wide" else replay_scene(name)
    if not build_id:
        return scene.behaviour

    def behaviour(req: pb.Request) -> tuple:
        delay, resp = scene.behaviour(req)
        if req.WhichOneof("command") == "hello" and resp.HasField("hello"):
            resp.hello.agent_version = f"{AGENT_VERSION}+{build_id}"
        return delay, resp
    return behaviour


def scene(name: str, **kw: Any) -> SceneData:
    return wide_scene(**kw) if name == "wide" else replay_scene(name, **kw)


def fake_attach(scene_data: SceneData) -> Callable[..., SceneSession]:
    """An ``inspector_widget.attach`` replacement serving ``scene_data`` (for
    monkeypatching the public facade the MCP server and the CLI call)."""
    def attach(serial: str, package: str, *args: Any, **kwargs: Any) -> SceneSession:
        return scene_data.session(serial=serial, package=package)
    return attach


__all__ = [
    "SceneData",
    "SceneError",
    "SceneSession",
    "Strings",
    "a11y_to_pb",
    "compose_to_pb",
    "encode_property",
    "fake_attach",
    "png_to_screenshot",
    "replay_behaviour",
    "replay_scene",
    "rgba_to_screenshot",
    "scene",
    "views_to_pb",
    "wide_scene",
]
