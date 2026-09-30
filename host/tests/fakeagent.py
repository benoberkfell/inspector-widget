"""Offline fake of the on-device ViewSpector agent and of the ``adb`` CLI.

The point of this module is to let the device-only host code paths run for real
in the offline suite (AGENTS.md §6, hazard #1). Only the ``adb`` subprocess is
faked; everything above it is the shipped code:

    cli.py / mcp_server.py
      -> inspector_widget.attach / Session / correlate / a11y / overlay / png
        -> inject.inject_and_connect / _try_warm_connect / Injection
          -> client.Client -> framing  (real VWSPCT01 framing over real TCP)
            -> adb.pidof / socket_exists / forward / shell / ...  (real parsing)
              -> adb._run                           <- FakeAdb.run replaces this

``FakeAdb`` emulates the handful of ``adb`` invocations the host issues
(``devices``, ``shell``, ``push``, ``forward``) against one or more
``FakeDevice`` objects. ``adb forward tcp:P localabstract:N`` really binds
127.0.0.1:P, and each accepted connection is handed to the ``FakeAgent``
currently bound to the abstract socket ``N`` (or closed at once when nothing is
bound, which is what real adb does). ``cmd activity attach-agent`` starts a
``FakeAgent`` on the socket name from the option string, after checking the
host staged the three artifacts where the native agent expects them.

``FakeAgent`` mirrors the Kotlin payload (Server.kt, Dispatcher.kt,
Properties.kt, Capture.kt, ComposeInspector.kt, AccessibilityInspector.kt):

* one ``StringTable`` per response, ids from 1, id 0 = absent (never emitted);
* ``Response.id`` echoes ``Request.id``; handler failures become
  ``status=ERROR`` with the request id; an unset/unknown command is
  ``ERROR "No command set in request"``; an unparseable protobuf gets an ERROR
  with id 0 and the connection stays open; a bad magic drops the connection;
* property values are encoded exactly like ``PropAccumulator.build`` (GRAVITY
  and INT_FLAG as a ``|``-joined ``str_value``, DIMENSION/COLOR in
  ``int32_value``, object-ish types as the class name in ``str_value``);
* screenshots are ``bitmap_type=2`` (ABGR_8888: in-memory bytes R,G,B,A), a
  9-byte little-endian header, deflated (Capture.kt:291-294);
* SHUTDOWN replies, then calls ``stop()``, which closes the server socket and
  shuts down ALL client connections, so every client sees EOF at once
  (Server.stop()). Agents built before that fix only close()d the other
  clients' sockets, which on Linux doesn't wake a thread blocked reading one:
  those clients stayed connected (and listed in /proc/net/unix) until they
  next sent something; ``close_clients_on_stop = False`` models them. Agents
  built before the reply fix stopped before writing the reply, so the
  requester only saw EOF; ``reply_to_shutdown = False`` models them;
* device work is serialized across connections (Server.kt ``handleLock``),
  but Hello and SHUTDOWN are answered without waiting for it, so a client is
  never told the app is frozen just because another client's request is
  slow; ``hello_waits_for_other_clients = True`` models agents from before
  that fix;
* /proc/net/unix lists each bound name as a listening entry (Flags
  00010000) and each open client connection as a connected entry under the
  same name (Flags 0, St 03), as Linux does;
* a11y nodes carry the ids of the A1-fixed agent (improve/a11y-agent-identity):
  every node has its own View's ``host_view_id`` and Compose nodes their
  semantics id as ``virtual_id``. The agent on this branch still reports the
  root's id for every node and the low 32 bits of the packed child id
  (ledger A1); ``legacy_a11y_ids = True`` reproduces that, and a strict xfail
  in test_e2e_fake_agent.py shows what it breaks on the host;
* Hello reports ``viewspector-0.1+<sha256 of the payload.jar it was loaded
  from>`` (the build handshake). ``build_id=None`` models an agent from before
  the handshake, which reports plain ``viewspector-0.1``.

Every agent takes a ``behaviour(req) -> (delay_s, action)`` hook, where action
is a ``pb.Response`` (sent), ``None`` (no reply), ``"close"`` (drop the
connection), ``"hang"`` (never reply until the agent stops) or raw ``bytes``
(written verbatim). The default behaviour is ``(0, agent.dispatch(req))``.
"""

from __future__ import annotations

import atexit
import hashlib
import itertools
import json
import os
import re
import shlex
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

_HOST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HOST_DIR not in sys.path:
    sys.path.insert(0, _HOST_DIR)

from google.protobuf.message import DecodeError  # noqa: E402

from inspector_widget import adb as adbmod  # noqa: E402
from inspector_widget import framing  # noqa: E402
from inspector_widget import inject as injectmod  # noqa: E402
from inspector_widget.proto import view_inspection_pb2 as pb  # noqa: E402

P = pb.Property

DEFAULT_SERIAL = "emulator-5554"
DEFAULT_PACKAGE = "com.oberkfell.a11yprobe"
DEFAULT_PID = 4242
AGENT_VERSION = "viewspector-0.1"  # Dispatcher.kt AGENT_VERSION
HOST_VIEW_ID = -1  # AccessibilityInspector.kt HOST_VIEW_ID

_real_sleep = time.sleep


# --------------------------------------------------------------------------- #
# String table (StringTable.kt): ids from 1, 0 = absent and never emitted.
# --------------------------------------------------------------------------- #
class StringTable:
    def __init__(self) -> None:
        self._map: Dict[str, int] = {}

    def intern(self, s: Optional[str]) -> int:
        if not s:
            return 0
        if s not in self._map:
            self._map[s] = len(self._map) + 1
        return self._map[s]

    def fill(self, msg: "pb.Strings") -> None:
        msg.SetInParent()
        for s, i in self._map.items():
            msg.entries.add(id=i, str=s)


# --------------------------------------------------------------------------- #
# Scene model: the "live UI" the fake agent reports.
# --------------------------------------------------------------------------- #
@dataclass
class Prop:
    """One inspectable attribute, with the value in its Kotlin runtime type."""

    name: str
    type: int
    value: Any
    is_layout: bool = False
    # (source, [resolution stack]) reported when include_resolution_stack is set
    resolution: Optional[Tuple[str, List[str]]] = None


@dataclass
class ComposeNodeSpec:
    id: int
    kind: int  # pb.ComposeNode.SEMANTICS / COMPOSABLE
    bounds: Tuple[int, int, int, int]
    attrs: Dict[str, str] = field(default_factory=dict)
    name: Optional[str] = None  # semantics nodes: bestLabel(attrs)
    source: Optional[str] = None
    render_node_id: int = 0
    a11y: Optional[Dict[str, Any]] = None  # virtual AccessibilityNodeInfo fields
    children: List["ComposeNodeSpec"] = field(default_factory=list)

    def label(self) -> str:
        if self.name is not None:
            return self.name
        for k in ("Text", "ContentDescription", "EditableText", "InputText", "Role", "TestTag"):
            if self.attrs.get(k):
                return self.attrs[k]
        return "Node"  # ComposeInspector.bestLabel


@dataclass
class ViewSpec:
    id: int  # uniqueDrawingId
    class_name: str
    package_name: str
    bounds: Tuple[int, int, int, int]
    resource: Optional[Tuple[str, str, str]] = None  # (namespace, type, name)
    text: Optional[str] = None
    is_webview: bool = False
    props: List[Prop] = field(default_factory=list)
    a11y: Dict[str, Any] = field(default_factory=dict)
    semantics: Optional[ComposeNodeSpec] = None  # set on an AndroidComposeView
    slot_table: List[ComposeNodeSpec] = field(default_factory=list)
    children: List["ViewSpec"] = field(default_factory=list)

    def walk(self) -> Iterable["ViewSpec"]:
        yield self
        for c in self.children:
            yield from c.walk()


@dataclass
class Scene:
    roots: List[ViewSpec]
    width: int = 360
    height: int = 640
    api_level: int = 36
    abi: str = "arm64-v8a"
    background: Tuple[int, int, int] = (250, 250, 250)
    paint: List[Tuple[Tuple[int, int, int, int], Tuple[int, int, int]]] = field(default_factory=list)
    skp: Optional[bytes] = None  # CaptureSkp payload; None -> "empty SKP"

    def all_views(self) -> List[ViewSpec]:
        return [v for r in self.roots for v in r.walk()]

    def find_view(self, view_id: int) -> Optional[ViewSpec]:
        for v in self.all_views():
            if v.id == view_id:
                return v
        return None

    def select_roots(self, root_id: int) -> List[ViewSpec]:
        if root_id == 0:
            return list(self.roots)
        return [r for r in self.roots if r.id == root_id]

    def compose_views(self, roots: List[ViewSpec]) -> List[ViewSpec]:
        out: List[ViewSpec] = []

        def collect(v: ViewSpec) -> None:
            if v.class_name == "AndroidComposeView":
                out.append(v)
                return
            for c in v.children:
                collect(c)

        for r in roots:
            collect(r)
        return out

    def pixel_at(self, x: int, y: int) -> Tuple[int, int, int]:
        color = self.background
        for (rx, ry, rw, rh), c in self.paint:
            if rx <= x < rx + rw and ry <= y < ry + rh:
                color = c
        return color

    # ---- screenshot -------------------------------------------------------- #
    def render(self, root: ViewSpec, scale: float) -> Tuple[int, int, bytes]:
        """RGBA8888 pixels of ``root``'s window region, scaled by ``scale``."""
        x0, y0, w, h = root.bounds
        sw, sh = max(1, int(w * scale)), max(1, int(h * scale))
        bg = bytes(self.background) + b"\xff"
        rows = [bytearray(bg * sw) for _ in range(sh)]
        for (rx, ry, rw, rh), c in self.paint:
            px = bytes(c) + b"\xff"
            ax, ay = int((rx - x0) * scale), int((ry - y0) * scale)
            bx, by = int((rx + rw - x0) * scale), int((ry + rh - y0) * scale)
            ax, bx = max(0, ax), min(sw, bx)
            ay, by = max(0, ay), min(sh, by)
            if bx <= ax or by <= ay:
                continue
            for yy in range(ay, by):
                rows[yy][ax * 4:bx * 4] = px * (bx - ax)
        return sw, sh, b"".join(bytes(r) for r in rows)


def _res(name: str, ns: str = DEFAULT_PACKAGE, type_: str = "id") -> Tuple[str, str, str]:
    return (ns, type_, name)


def _common_view_props(extra: Sequence[Prop] = ()) -> List[Prop]:
    """Attributes every android.view.View companion reports (View$InspectionCompanion)."""
    return [
        Prop("visibility", P.INT_ENUM, "visible"),
        Prop("alpha", P.FLOAT, 1.0),
        Prop("enabled", P.BOOLEAN, 1),
        Prop("importantForAccessibility", P.INT_ENUM, "auto"),
        Prop("minHeight", P.INT32, 0),
        Prop("paddingStart", P.DIMENSION, 42),
        Prop("scrollIndicators", P.INT_FLAG, set()),  # empty flag set -> str_value 0
        *extra,
    ]


def _text_resolution(style: str) -> Tuple[str, List[str]]:
    return (f"@style/{style}",
            ["@layout/activity_main", f"@style/{style}", "@android:style/Widget.Material.TextView"])


def default_scene() -> Scene:
    """A small but realistic two-window screen: Views + one AndroidComposeView."""
    title = ViewSpec(
        1003, "TextView", "android.widget", (16, 24, 328, 40),
        resource=_res("title"), text="Hello world",
        props=_common_view_props([
            Prop("text", P.STRING, "Hello world", resolution=_text_resolution("Widget.App.Title")),
            Prop("gravity", P.GRAVITY, ["center_vertical", "start"],
                 resolution=_text_resolution("Widget.App.Title")),
            Prop("inputType", P.INT_FLAG, ["text", "textCapSentences"]),
            Prop("textColor", P.COLOR, -14671580),  # 0xFF202124
            Prop("textSize", P.FLOAT, 42.0),
            Prop("id", P.RESOURCE, _res("title")),
            Prop("layout_gravity", P.GRAVITY, ["center_horizontal"], is_layout=True),
            Prop("layout_marginTop", P.DIMENSION, 16, is_layout=True),
            Prop("layout_weight", P.FLOAT, 1.0, is_layout=True),
        ]),
        a11y={"class_name": "android.widget.TextView", "text": "Hello world",
              "view_id_resource_name": f"{DEFAULT_PACKAGE}:id/title", "important_for_accessibility": 1},
    )
    ok = ViewSpec(
        1004, "Button", "android.widget", (16, 80, 120, 48),
        resource=_res("ok"), text="OK",
        props=_common_view_props([
            Prop("text", P.STRING, "OK"),
            Prop("gravity", P.GRAVITY, ["center"]),
            Prop("stateListAnimator", P.ANIMATOR, "android.animation.StateListAnimator"),
            Prop("background", P.DRAWABLE, "android.graphics.drawable.RippleDrawable"),
            Prop("id", P.RESOURCE, _res("ok")),
        ]),
        a11y={"class_name": "android.widget.Button", "text": "OK", "clickable": True,
              "focusable": True, "actions": [(0x10, None), (0x40, None)],
              "view_id_resource_name": f"{DEFAULT_PACKAGE}:id/ok"},
    )
    logo = ViewSpec(
        1005, "ImageView", "android.widget", (160, 80, 32, 32),
        resource=_res("logo"),
        props=_common_view_props([
            Prop("scaleType", P.INT_ENUM, "fitCenter"),
            Prop("src", P.DRAWABLE, "android.graphics.drawable.VectorDrawable"),
        ]),
        a11y={"class_name": "android.widget.ImageView"},
    )
    semantics = ComposeNodeSpec(1, pb.ComposeNode.SEMANTICS, (0, 160, 360, 400), children=[
        ComposeNodeSpec(2, pb.ComposeNode.SEMANTICS, (16, 176, 200, 56),
                        attrs={"Text": "Submit", "Role": "Button",
                               "OnClick": "AccessibilityAction(label=null, action=Function0<Boolean>)"},
                        a11y={"class_name": "android.widget.Button", "text": "Submit",
                              "clickable": True, "focusable": True,
                              "actions": [(0x10, None), (0x40, None)]}),
        ComposeNodeSpec(3, pb.ComposeNode.SEMANTICS, (240, 176, 96, 96),
                        attrs={"Role": "Image"},
                        a11y={"class_name": "android.widget.ImageView"}),
        ComposeNodeSpec(4, pb.ComposeNode.SEMANTICS, (16, 288, 328, 40),
                        attrs={"Text": "Settings", "Heading": "kotlin.Unit"},
                        a11y={"class_name": "android.widget.TextView", "text": "Settings",
                              "heading": True}),
        ComposeNodeSpec(5, pb.ComposeNode.SEMANTICS, (16, 340, 328, 56),
                        attrs={"Text": "Wi-Fi", "Role": "Switch", "ToggleableState": "On",
                               "OnClick": "AccessibilityAction(label=null, action=Function0<Boolean>)"},
                        a11y={"class_name": "android.widget.Switch", "text": "Wi-Fi",
                              "state_description": "On", "checkable": True, "checked": True,
                              "clickable": True, "focusable": True, "actions": [(0x10, None)]}),
        ComposeNodeSpec(6, pb.ComposeNode.SEMANTICS, (16, 440, 64, 64),
                        attrs={"OnClick": "AccessibilityAction(label=null, action=Function0<Boolean>)"},
                        a11y={"class_name": "android.view.View", "clickable": True,
                              "focusable": True, "actions": [(0x10, None)]}),
    ])
    slot_table = [
        ComposeNodeSpec(900001, pb.ComposeNode.COMPOSABLE, (0, 160, 360, 400), name="ProbeScreen",
                        source="MainActivity.kt:31", children=[
            ComposeNodeSpec(900002, pb.ComposeNode.COMPOSABLE, (16, 176, 200, 56),
                            name="SubmitButton", source="MainActivity.kt:42",
                            render_node_id=7002, attrs={"text": "Submit"}),
            ComposeNodeSpec(900003, pb.ComposeNode.COMPOSABLE, (240, 176, 96, 96),
                            name="Image", source="MainActivity.kt:57", render_node_id=7003),
        ]),
    ]
    compose_host = ViewSpec(
        1006, "AndroidComposeView", "androidx.compose.ui.platform", (0, 160, 360, 400),
        props=_common_view_props(),
        a11y={"class_name": "android.view.View",
              "provider_class": "androidx.compose.ui.platform.AndroidComposeView"},
        semantics=semantics, slot_table=slot_table,
    )
    content = ViewSpec(
        1002, "LinearLayout", "android.widget", (0, 0, 360, 640),
        resource=("android", "id", "content"),
        props=_common_view_props([
            Prop("orientation", P.INT_ENUM, "vertical"),
            Prop("background", P.DRAWABLE, "android.graphics.drawable.ColorDrawable"),
        ]),
        a11y={"class_name": "android.widget.LinearLayout"},
        children=[title, ok, logo, compose_host],
    )
    decor = ViewSpec(
        1001, "DecorView", "com.android.internal.policy", (0, 0, 360, 640),
        props=_common_view_props(), a11y={"class_name": "android.widget.FrameLayout"},
        children=[content],
    )
    toast_text = ViewSpec(
        2002, "TextView", "android.widget", (56, 576, 248, 32), text="Saved",
        props=_common_view_props([Prop("text", P.STRING, "Saved")]),
        a11y={"class_name": "android.widget.TextView", "text": "Saved"},
    )
    popup = ViewSpec(
        2001, "PopupDecorView", "android.widget", (40, 560, 280, 64),
        props=_common_view_props(), a11y={"class_name": "android.widget.FrameLayout"},
        children=[toast_text],
    )
    return Scene(
        roots=[decor, popup],
        paint=[
            ((16, 80, 120, 48), (30, 60, 200)),     # the OK button: R != B, so a swap shows
            ((16, 176, 200, 56), (98, 0, 238)),     # Compose "Submit"
            ((240, 176, 96, 96), (200, 120, 40)),   # Compose image
            ((40, 560, 280, 64), (50, 50, 50)),     # popup window
        ],
    )


# --------------------------------------------------------------------------- #
# Proto encoders (the Kotlin payload's output, byte-for-byte in meaning).
# --------------------------------------------------------------------------- #
def _set_bounds(msg: "pb.Bounds", b: Tuple[int, int, int, int]) -> None:
    msg.layout.x, msg.layout.y, msg.layout.w, msg.layout.h = b


def _set_resource(st: StringTable, msg: "pb.Resource", res: Tuple[str, str, str]) -> None:
    ns, type_, name = res
    msg.namespace = st.intern(ns)
    msg.type = st.intern(type_)
    msg.name = st.intern(name)


def encode_view(st: StringTable, v: ViewSpec, out: "pb.ViewNode") -> None:
    """TreeBuilder: one ViewNode per View, children nested."""
    out.id = v.id
    out.class_name = st.intern(v.class_name)
    out.package_name = st.intern(v.package_name)
    _set_bounds(out.bounds, v.bounds)
    if v.resource:
        _set_resource(st, out.resource, v.resource)
        out.view_id_name = st.intern(v.resource[2])
    if v.text:
        out.text_value = st.intern(v.text)
    if v.is_webview:
        out.flags = pb.ViewNode.IS_WEBVIEW
    for c in v.children:
        encode_view(st, c, out.children.add())


def encode_property(st: StringTable, prop: Prop, include_stack: bool) -> "pb.Property":
    """Properties.kt PropAccumulator.build()."""
    b = pb.Property(name=st.intern(prop.name), type=prop.type, is_layout=prop.is_layout)
    t, v = prop.type, prop.value
    if t in (P.STRING, P.INT_ENUM):
        b.str_value = st.intern(v)
    elif t in (P.GRAVITY, P.INT_FLAG):
        flags = list(v)
        b.str_value = st.intern("|".join(flags) if flags else "")
    elif t in (P.INT32, P.INT16, P.BYTE, P.CHAR, P.COLOR, P.DIMENSION, P.BOOLEAN):
        b.int32_value = int(v)
    elif t == P.INT64:
        b.int64_value = int(v)
    elif t == P.DOUBLE:
        b.double_value = float(v)
    elif t == P.FLOAT:
        b.float_value = float(v)
    elif t == P.RESOURCE:
        _set_resource(st, b.resource_value, v)
    elif t in (P.OBJECT, P.DRAWABLE, P.ANIM, P.ANIMATOR, P.INTERPOLATOR):
        b.str_value = st.intern(v)  # the runtime class name
    else:  # pragma: no cover - mirrors the Kotlin "unhandled type; dropping"
        raise ValueError(f"unhandled property type {t}")
    # Resolution data is only read for VIEW-category attributes (readAny).
    if include_stack and not prop.is_layout and prop.resolution:
        source, stack = prop.resolution
        b.source = st.intern(source)
        for entry in stack:
            b.resolution_stack.append(st.intern(entry))
    return b


def encode_property_group(st: StringTable, v: ViewSpec, include_stack: bool) -> "pb.PropertyGroup":
    group = pb.PropertyGroup(view_id=v.id)
    for prop in v.props:
        group.properties.append(encode_property(st, prop, include_stack))
    return group


def encode_screenshot(scene: Scene, root: ViewSpec, scale: float) -> "pb.Screenshot":
    """Capture.screenshot(): ABGR_8888 (wire type 2), 9-byte LE header, deflated."""
    w, h, rgba = scene.render(root, scale)
    raw = struct.pack("<iiB", w, h, 2) + rgba  # ABGR_8888 in-memory order is R,G,B,A
    return pb.Screenshot(format=pb.Screenshot.BITMAP, width=w, height=h, bitmap_type=2,
                         data=zlib.compress(raw, 1), scale=scale)


def encode_compose(st: StringTable, n: ComposeNodeSpec, out: "pb.ComposeNode") -> None:
    out.id = n.id
    out.kind = n.kind
    out.name = st.intern(n.label())
    _set_bounds(out.bounds, n.bounds)
    for k, val in n.attrs.items():
        out.attrs.add(key=st.intern(k), value=st.intern(val))
    if n.source:
        out.source = st.intern(n.source)
    if n.render_node_id:
        out.render_node_id = n.render_node_id
    for c in n.children:
        encode_compose(st, c, out.children.add())


_A11Y_TEXT = ("text", "content_description", "hint_text", "state_description", "role_description",
              "class_name", "package_name", "view_id_resource_name", "provider_class")


def _fill_a11y(st: StringTable, out: "pb.A11yNode", spec: Dict[str, Any], bounds,
               include_extras: bool, include_rendering_info: bool, extras: Dict[str, str]) -> None:
    _set_bounds(out.bounds, bounds)
    out.visible_to_user = True
    out.enabled = True
    out.package_name = st.intern(DEFAULT_PACKAGE)
    for key, val in spec.items():
        if key in _A11Y_TEXT:
            setattr(out, key, st.intern(val))
        elif key == "actions":
            for aid, label in val:
                out.actions.add(id=aid, label=st.intern(label))
        else:
            setattr(out, key, val)
    if include_extras:
        for k, val in extras.items():
            out.extras.add(key=st.intern(k), value=st.intern(val))
    if include_rendering_info and spec.get("text"):
        out.text_size_px = 42.0
        out.text_size_unit = 2


def encode_a11y_view(st: StringTable, v: ViewSpec, out: "pb.A11yNode",
                     include_extras: bool, include_rendering_info: bool) -> None:
    """A real View's AccessibilityNodeInfo (virtual_id = HOST_VIEW_ID)."""
    out.host_view_id = v.id
    out.virtual_id = HOST_VIEW_ID
    _fill_a11y(st, out, v.a11y, v.bounds, include_extras, include_rendering_info, {})
    if v.semantics is not None:
        for sem in v.semantics.children:
            encode_a11y_virtual(st, v.id, sem, out.children.add(), include_extras,
                                include_rendering_info)
    for c in v.children:
        encode_a11y_view(st, c, out.children.add(), include_extras, include_rendering_info)


def encode_a11y_virtual(st: StringTable, host_id: int, n: ComposeNodeSpec, out: "pb.A11yNode",
                        include_extras: bool, include_rendering_info: bool) -> None:
    """A Compose virtual node: (host_view_id=AndroidComposeView, virtual_id=semanticsId)."""
    out.host_view_id = host_id
    out.virtual_id = n.id
    out.is_virtual = True
    extras = {"androidx.compose.ui.semantics.id": str(n.id)}
    _fill_a11y(st, out, n.a11y or {}, n.bounds, include_extras, include_rendering_info, extras)
    for c in n.children:
        encode_a11y_virtual(st, host_id, c, out.children.add(), include_extras,
                            include_rendering_info)


def legacy_a11y_ids(root: "pb.A11yNode") -> None:
    """Rewrite an encoded a11y tree to the ids the agent on this branch sends
    (ledger A1, AccessibilityInspector.kt walk()/virtualIdOf()): walk() never
    updates ``sourceView``, so every node carries the ROOT's host_view_id; a
    child's virtual_id is the LOW 32 bits of the packed child id, i.e. the
    accessibility view id of the View behind it (the AndroidComposeView, for a
    Compose node), so real View children are flagged virtual as well."""
    root_host = root.host_view_id

    def accessibility_view_id(view_id: int) -> int:
        return view_id % 100 + 10  # any small per-View int; the framework counts up

    def fix(node: "pb.A11yNode") -> None:
        for child in node.children:
            child.virtual_id = accessibility_view_id(child.host_view_id)
            child.is_virtual = True
            child.host_view_id = root_host
            fix(child)

    fix(root)


def error_response(req_id: int, message: str) -> "pb.Response":
    return pb.Response(id=req_id, status=pb.Response.ERROR, error=message)


def frame(resp: "pb.Response") -> bytes:
    """A complete VWSPCT01 frame carrying ``resp`` (for raw-bytes behaviours)."""
    payload = resp.SerializeToString()
    return framing.MAGIC + struct.pack(">I", len(payload)) + payload


# --------------------------------------------------------------------------- #
# The agent.
# --------------------------------------------------------------------------- #
Behaviour = Callable[["pb.Request"], Tuple[float, Any]]


class FakeAgent:
    """One payload Server bound to one abstract socket name.

    Connections arrive either through a FakeDevice forward listener or, for
    direct unit tests, through ``self.port`` (a real TCP listener).
    """

    def __init__(self, scene: Optional[Scene] = None, behaviour: Optional[Behaviour] = None,
                 socket_name: str = "", generation: int = 1,
                 on_request: Optional[Callable[["FakeAgent", int, Any], None]] = None,
                 on_stop: Optional[Callable[["FakeAgent"], None]] = None,
                 build_id: Optional[str] = None) -> None:
        self.scene = scene or default_scene()
        self.behaviour: Behaviour = behaviour or self.default_behaviour
        self.socket_name = socket_name
        self.generation = generation
        self.on_request = on_request
        self.on_stop = on_stop
        self.build_id = build_id  # None: an agent from before the build handshake
        self.reply_to_shutdown = True  # False: an agent from before the reply fix
        # False: an agent from before the stop fix, whose other clients stayed
        # connected (unreadable) after it stopped. See the module docstring.
        self.close_clients_on_stop = True
        # True: an agent from before Hello/SHUTDOWN skipped handleLock.
        self.hello_waits_for_other_clients = False
        # True: the A1 id encoding of the agent on this branch (module docstring).
        self.legacy_a11y_ids = False
        self._handle_lock = threading.Lock()  # Server.kt handleLock
        # True: an agent from before the accept fix. Server.stop() closed the
        # LocalServerSocket, but the thread blocked in accept() kept the name
        # bound until one more connection arrived (seen live on API 37).
        self.linger_after_stop = False
        self.lingering = False
        self.inspection_enabled = False  # ComposeInspector.enableInspection is sticky
        self.running = True
        self.requests: List["pb.Request"] = []
        self.log: List[Tuple[int, str]] = []  # (connection id, command)
        self._conns: Dict[int, socket.socket] = {}
        self._conn_ids = itertools.count(1)
        self._lock = threading.RLock()
        self._release = threading.Event()
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(16)
        self.port = self._srv.getsockname()[1]
        threading.Thread(target=self._accept, name="fakeagent-accept", daemon=True).start()

    # ---- connections ------------------------------------------------------- #
    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            self.serve(conn)

    def serve(self, conn: socket.socket) -> int:
        with self._lock:
            if not self.running:
                _close(conn)
                return 0
            cid = next(self._conn_ids)
            self._conns[cid] = conn
        threading.Thread(target=self._serve, args=(cid, conn),
                         name=f"fakeagent-conn-{cid}", daemon=True).start()
        return cid

    @property
    def open_connections(self) -> int:
        with self._lock:
            return len(self._conns)

    def commands(self) -> List[str]:
        return [c for _cid, c in self.log]

    def _record(self, cid: int, req: Any, command: str) -> None:
        with self._lock:
            if req is not None:
                self.requests.append(req)
            self.log.append((cid, command))
        if self.on_request is not None:
            self.on_request(self, cid, req)

    def _serve(self, cid: int, conn: socket.socket) -> None:
        try:
            while self.running:
                try:
                    raw = framing.read_message(conn)
                except Exception:
                    return  # EOF, reset, bad magic: Server.kt drops the connection
                req = pb.Request()
                try:
                    req.ParseFromString(raw)
                except DecodeError as exc:
                    self._record(cid, None, "<malformed>")
                    self._send(conn, error_response(0, f"Malformed request: {exc}"))
                    continue
                if not self.running:
                    return  # Server.stop() raced this read: the session is over
                command = req.WhichOneof("command") or "<unset>"
                self._record(cid, req, command)
                locked = (command not in ("hello", "shutdown")
                          or self.hello_waits_for_other_clients)
                if locked:
                    self._handle_lock.acquire()
                try:
                    delay, action = self.behaviour(req)
                    if delay:
                        _real_sleep(delay)
                    if action == "hang":
                        self._release.wait()
                        return
                finally:
                    if locked:
                        self._handle_lock.release()
                if action == "close":
                    return
                if action is None:
                    continue
                if isinstance(action, (bytes, bytearray)):
                    if not self._sendall(conn, bytes(action)):
                        return
                    continue
                if command == "shutdown" and action.status == pb.Response.OK:
                    # Server.serveConnection writes the reply, then Server.stop()
                    # closes every client. Older agents stopped first, so the
                    # reply was lost (reply_to_shutdown = False).
                    if self.reply_to_shutdown:
                        self._send(conn, action)
                    self.stop()
                    return
                if not self._send(conn, action):
                    return
        finally:
            with self._lock:
                self._conns.pop(cid, None)
            _close(conn)

    def _send(self, conn: socket.socket, resp: "pb.Response") -> bool:
        try:
            framing.write_message(conn, resp.SerializeToString())
            return True
        except OSError:
            return False

    def _sendall(self, conn: socket.socket, data: bytes) -> bool:
        try:
            conn.sendall(data)
            return True
        except OSError:
            return False

    # ---- lifecycle --------------------------------------------------------- #
    def kill_clients(self) -> None:
        """Drop every client connection; the server stays up (socket still bound)."""
        with self._lock:
            conns = list(self._conns.values())
            self._conns.clear()
        for c in conns:
            _close(c)

    def stop(self) -> None:
        """Server.stop(): close the server socket and shut down ALL client
        connections (or, with ``close_clients_on_stop = False``, leave them
        connected until each client next sends something, as older agents did)."""
        with self._lock:
            if not self.running:
                return
            self.running = False
        self._release.set()
        _close(self._srv)
        if self.close_clients_on_stop:
            self.kill_clients()
        if self.on_stop is not None:
            self.on_stop(self)

    close = stop

    # ---- dispatcher -------------------------------------------------------- #
    def default_behaviour(self, req: "pb.Request") -> Tuple[float, Any]:
        return 0, self.dispatch(req)

    def dispatch(self, req: "pb.Request") -> "pb.Response":
        """Dispatcher.handle(): exactly one Response per request."""
        command = req.WhichOneof("command")
        try:
            if command is None:
                return error_response(req.id, "No command set in request")
            return getattr(self, f"_h_{command}")(req.id, getattr(req, command))
        except Exception as exc:  # handler failure -> ERROR with the request id
            return error_response(req.id, f"{type(exc).__name__}: {exc}")

    def _ok(self, req_id: int) -> "pb.Response":
        return pb.Response(id=req_id, status=pb.Response.OK)

    def _h_hello(self, req_id, cmd):
        resp = self._ok(req_id)
        resp.hello.agent_version = AGENT_VERSION + (f"+{self.build_id}" if self.build_id else "")
        resp.hello.api_level = self.scene.api_level
        resp.hello.abi = self.scene.abi
        return resp

    def _h_get_windows(self, req_id, cmd):
        resp = self._ok(req_id)
        resp.get_windows.root_ids.extend(r.id for r in self.scene.roots)
        resp.get_windows.strings.SetInParent()
        return resp

    def _h_dump_tree(self, req_id, cmd):
        st = StringTable()
        roots = self.scene.select_roots(cmd.root_id)
        resp = self._ok(req_id)
        out = resp.dump_tree
        for r in roots:
            encode_view(st, r, out.roots.add())
        if cmd.include_properties:
            for r in roots:
                for v in r.walk():
                    out.properties.append(
                        encode_property_group(st, v, cmd.include_resolution_stack))
        if cmd.include_screenshot and roots:
            out.screenshot.CopyFrom(
                encode_screenshot(self.scene, roots[0], _effective_scale(cmd.screenshot_scale)))
        st.fill(out.strings)
        return resp

    def _h_get_properties(self, req_id, cmd):
        view = self.scene.find_view(cmd.view_id)
        if view is None:
            return error_response(req_id, f"No view found with id {cmd.view_id}")
        st = StringTable()
        resp = self._ok(req_id)
        resp.get_properties.group.CopyFrom(
            encode_property_group(st, view, cmd.include_resolution_stack))
        st.fill(resp.get_properties.strings)
        return resp

    def _h_screenshot(self, req_id, cmd):
        roots = self.scene.select_roots(cmd.root_id)
        if not roots:
            return error_response(req_id, f"No root view found for id {cmd.root_id}")
        resp = self._ok(req_id)
        resp.screenshot.screenshot.CopyFrom(
            encode_screenshot(self.scene, roots[0], _effective_scale(cmd.scale)))
        return resp

    def _h_shutdown(self, req_id, cmd):
        resp = self._ok(req_id)
        resp.shutdown.SetInParent()
        return resp

    def _h_dump_compose(self, req_id, cmd):
        neither = not cmd.include_semantics and not cmd.include_slot_table
        include_semantics = cmd.include_semantics or neither
        include_slot = cmd.include_slot_table or neither
        roots = self.scene.select_roots(cmd.root_view_id)
        compose_views = self.scene.compose_views(roots)
        if cmd.enable_inspection and include_slot and compose_views:
            self.inspection_enabled = True
        st = StringTable()
        diag = [f"found {len(compose_views)} AndroidComposeView(s)"]
        resp = self._ok(req_id)
        for cv in compose_views:
            w = resp.dump_compose.windows.add()
            w.view_id = cv.id
            root = w.root
            root.id = cv.id
            root.name = st.intern("AndroidComposeView")
            root.kind = pb.ComposeNode.COMPOSABLE
            _set_bounds(root.bounds, cv.bounds)
            produced = False
            if include_semantics and cv.semantics is not None:
                encode_compose(st, cv.semantics, root.children.add())
                produced = True
            if include_slot:
                if not self.inspection_enabled or not cv.slot_table:
                    diag.append("slot table empty (inspection_slot_table_set not populated)")
                else:
                    for n in cv.slot_table:
                        encode_compose(st, n, root.children.add())
                        produced = True
            if not produced:
                diag.append(f"view#{cv.id} produced no compose nodes")
        st.fill(resp.dump_compose.strings)
        resp.dump_compose.diagnostics = "; ".join(diag)
        return resp

    def _h_dump_a11y(self, req_id, cmd):
        roots = self.scene.select_roots(cmd.root_id)
        st = StringTable()
        diag = [f"roots={len(roots)}; api={self.scene.api_level}"]
        resp = self._ok(req_id)
        for r in roots:
            w = resp.dump_a11y.windows.add()
            w.root_view_id = r.id
            encode_a11y_view(st, r, w.root, cmd.include_extras, cmd.include_rendering_info)
            if self.legacy_a11y_ids:
                legacy_a11y_ids(w.root)
            diag.append(f"root#{r.id} query-from-app-process")
        st.fill(resp.dump_a11y.strings)
        resp.dump_a11y.diagnostics = "; ".join(diag)
        return resp

    def _h_capture_skp(self, req_id, cmd):
        resp = self._ok(req_id)
        skp = resp.capture_skp
        roots = self.scene.select_roots(cmd.root_id)
        if not roots:
            skp.supported = True
            skp.error = f"no root view found for id {cmd.root_id}"
        elif self.scene.api_level <= 32:
            skp.supported = False
            skp.error = f"SKP capture needs API 33+ (have {self.scene.api_level})"
        elif not self.scene.skp:
            skp.supported = True
            skp.error = "empty SKP"
        else:
            skp.supported = True
            skp.skp = self.scene.skp
            skp.version = struct.unpack_from("<I", self.scene.skp, 8)[0] if len(self.scene.skp) >= 12 else 0
        return resp


def _effective_scale(raw: float) -> float:
    """Dispatcher.effectiveScale: 0 (unset) -> 1.0, clamp to <= 1.0."""
    if raw <= 0:
        return 1.0
    return min(raw, 1.0)


def _close(s: Optional[socket.socket]) -> None:
    if s is None:
        return
    try:
        s.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        s.close()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# The device + adb emulation.
# --------------------------------------------------------------------------- #
@dataclass
class FakeApp:
    package: str
    pid: Optional[int]
    debuggable: bool = True
    # Frozen by the cached-apps freezer (in the background): attach-agent is
    # only queued, and cgroup.events says "frozen 1".
    frozen: bool = False

    @property
    def data_dir(self) -> str:
        return f"/data/user/0/{self.package}"


@dataclass
class WireRecord:
    generation: int
    conn: int
    command: str
    request: Any  # pb.Request, or None for a malformed frame


class FakeDevice:
    """One adb device: apps, abstract sockets, forwards, and the agents on it."""

    def __init__(self, serial: str = DEFAULT_SERIAL, scene_factory: Callable[[], Scene] = default_scene,
                 api_level: int = 36, abi: str = "arm64-v8a", model: str = "sdk_gphone64_arm64",
                 physical_density: int = 420, override_density: Optional[int] = 280,
                 font_scale: str = "1.3", log_path: Optional[str] = None) -> None:
        self.serial = serial
        self.state = "device"
        self.scene_factory = scene_factory
        self.api_level, self.abi, self.model = api_level, abi, model
        self.physical_density, self.override_density = physical_density, override_density
        self.font_scale = font_scale
        self.apps: Dict[str, FakeApp] = {}
        self.sockets: Dict[str, FakeAgent] = {}  # abstract name -> bound agent
        self.foreign_sockets: List[str] = []     # names bound by other processes
        self.forwards: Dict[int, Tuple[str, socket.socket]] = {}
        self.agents: List[FakeAgent] = []
        self.wire: List[WireRecord] = []
        self.adb_log: List[List[str]] = []
        self.pushed: Dict[str, str] = {}
        self.pushed_sha: Dict[str, str] = {}  # remote path -> sha256 of what was pushed
        # The build a pre-existing agent (start_agent) runs; install() sets it
        # to the build-out payload.jar's hash, i.e. "injected by an earlier run
        # of this same build".
        self.default_build_id: Optional[str] = None
        self.staged: Dict[Tuple[str, str], Tuple[str, Optional[str]]] = {}
        self.attach_calls: List[Dict[str, str]] = []
        self.settings: Dict[str, str] = {}
        self.unexpected: List[str] = []
        self.behaviour: Optional[Behaviour] = None  # for agents started later
        self.log_path = log_path
        self._lock = threading.RLock()

    # ---- setup ------------------------------------------------------------- #
    def add_app(self, package: str, pid: Optional[int], debuggable: bool = True) -> FakeApp:
        app = FakeApp(package, pid, debuggable)
        self.apps[package] = app
        return app

    _SAME_BUILD = object()

    def start_agent(self, package: str = DEFAULT_PACKAGE, socket_name: Optional[str] = None,
                    build_id: Any = _SAME_BUILD) -> FakeAgent:
        """Bind an agent as if an earlier run had injected it (the warm path).

        ``build_id`` defaults to the local build (``default_build_id``); pass a
        different string for a stale build, or ``None`` for an agent from before
        the build handshake.
        """
        app = self.apps[package]
        name = socket_name or f"viewspector_{app.pid}"
        if build_id is FakeDevice._SAME_BUILD:
            build_id = self.default_build_id
        with self._lock:
            existing = self.sockets.get(name)
            if existing is not None and existing.running:
                return existing
            agent = FakeAgent(scene=self.scene_factory(), behaviour=self.behaviour,
                              socket_name=name, generation=len(self.agents) + 1,
                              on_request=self._on_request, on_stop=self._on_stop,
                              build_id=build_id)
            agent.package = package  # type: ignore[attr-defined]
            self.agents.append(agent)
            self.sockets[name] = agent
        return agent

    def agent(self, package: str = DEFAULT_PACKAGE) -> Optional[FakeAgent]:
        """The agent currently bound for ``package``'s live pid, if any."""
        app = self.apps.get(package)
        if app is None or app.pid is None:
            return None
        agent = self.sockets.get(f"viewspector_{app.pid}")
        return agent if agent is not None and agent.running else None

    # ---- scenario controls ------------------------------------------------- #
    def kill_clients(self, package: str = DEFAULT_PACKAGE) -> None:
        agent = self.agent(package)
        if agent is not None:
            agent.kill_clients()

    def idle_timeout(self, package: str = DEFAULT_PACKAGE) -> None:
        """The payload idle watchdog fired: Server.stop()."""
        agent = self.agent(package)
        if agent is not None:
            agent.stop()

    def restart_app(self, package: str = DEFAULT_PACKAGE, new_pid: Optional[int] = None) -> None:
        """The app process died (taking its agent with it) and was relaunched."""
        agent = self.agent(package)
        if agent is not None:
            agent.stop()
        app = self.apps[package]
        app.pid = new_pid if new_pid is not None else (app.pid or 0) + 1000

    # ---- wire / adb observation -------------------------------------------- #
    def _on_request(self, agent: FakeAgent, cid: int, req: Any) -> None:
        command = (req.WhichOneof("command") or "<unset>") if req is not None else "<malformed>"
        with self._lock:
            self.wire.append(WireRecord(agent.generation, cid, command, req))
        if self.log_path:
            from google.protobuf.json_format import MessageToDict
            body = MessageToDict(req, preserving_proto_field_name=True) if req is not None else None
            self._log({"event": "request", "generation": agent.generation, "conn": cid,
                       "command": command, "request": body})

    def _on_stop(self, agent: FakeAgent) -> None:
        with self._lock:
            if self.sockets.get(agent.socket_name) is agent:
                if agent.linger_after_stop:
                    agent.lingering = True  # still bound until a connection arrives
                else:
                    del self.sockets[agent.socket_name]

    def _log(self, record: Dict[str, Any]) -> None:
        with self._lock, open(self.log_path, "a") as f:  # type: ignore[arg-type]
            f.write(json.dumps(record) + "\n")

    def commands(self, generation: Optional[int] = None) -> List[str]:
        return [w.command for w in self.wire if generation is None or w.generation == generation]

    def requests(self, command: str) -> List[Any]:
        return [getattr(w.request, command) for w in self.wire if w.command == command]

    def shell_log(self) -> List[str]:
        return [argv[1] for argv in self.adb_log if argv[:1] == ["shell"]]

    def verbs(self) -> List[str]:
        """A compact view of the adb traffic: 'shell:<first word>', 'push', 'forward', ..."""
        out = []
        for argv in self.adb_log:
            if argv[0] == "shell":
                toks = shlex.split(argv[1])
                word = toks[0]
                if word in ("run-as", "cmd", "settings", "getprop", "pm", "wm") and len(toks) > 1:
                    word = f"{word} {toks[2] if word == 'run-as' and len(toks) > 2 else toks[1]}"
                out.append(f"shell:{word}")
            elif argv[0] == "forward":
                out.append("forward --remove" if argv[1] == "--remove" else "forward")
            else:
                out.append(argv[0])
        return out

    def clear_logs(self) -> None:
        with self._lock:
            self.wire.clear()
            self.adb_log.clear()
            self.attach_calls.clear()

    # ---- adb dispatch ------------------------------------------------------ #
    def handle(self, args: List[str]) -> Tuple[int, str, str]:
        self.adb_log.append(list(args))
        if not args:
            return 1, "", "adb: usage"
        verb = args[0]
        if verb == "shell" and len(args) == 2:
            return self.shell(args[1])
        if verb == "push" and len(args) == 3:
            local, remote = args[1], args[2]
            if not os.path.isfile(local):
                return 1, "", f"adb: error: cannot stat '{local}': No such file or directory"
            self.pushed[remote] = local
            with open(local, "rb") as f:
                self.pushed_sha[remote] = hashlib.sha256(f.read()).hexdigest()
            return 0, f"{local}: 1 file pushed, 0 skipped.\n", ""
        if verb == "forward":
            return self.forward(args[1:])
        if verb == "wait-for-device":
            return 0, "", ""
        self.unexpected.append(" ".join(args))
        return 1, "", f"adb: unknown command {verb}"

    def shell(self, cmd: str) -> Tuple[int, str, str]:
        frozen_probe = re.search(r"/proc/(\d+)/cgroup\)/cgroup\.events", cmd)
        if frozen_probe:
            pid = int(frozen_probe.group(1))
            app = next((a for a in self.apps.values() if a.pid == pid), None)
            if app is None:
                return 0, "", ""
            return 0, f"populated 1\nfrozen {int(app.frozen)}\n", ""
        toks = shlex.split(cmd)
        if not toks:
            return 0, "", ""
        if toks[:2] == ["getprop", "ro.build.version.sdk"]:
            return 0, f"{self.api_level}\n", ""
        if toks[:2] == ["getprop", "ro.product.cpu.abi"]:
            return 0, f"{self.abi}\n", ""
        if toks[:2] == ["getprop", "ro.product.model"]:
            return 0, f"{self.model}\n", ""
        if toks[0] == "pidof" and len(toks) == 2:
            app = self.apps.get(toks[1])
            if app is None or app.pid is None:
                return 1, "", ""
            return 0, f"{app.pid}\n", ""
        if toks == ["wm", "density"]:
            out = f"Physical density: {self.physical_density}\n"
            if self.override_density:
                out += f"Override density: {self.override_density}\n"
            return 0, out, ""
        if toks == ["settings", "get", "system", "font_scale"]:
            return 0, f"{self.font_scale}\n", ""
        if toks[:3] == ["settings", "put", "global"] and len(toks) == 5:
            self.settings[toks[3]] = toks[4]
            return 0, "", ""
        if toks == ["pm", "list", "packages", "-3"]:
            return 0, "".join(f"package:{p}\n" for p in self.apps), ""
        if toks[0] == "run-as" and len(toks) >= 3:
            return self._run_as(toks[1], toks[2:])
        if toks[:3] == ["cmd", "activity", "attach-agent"] and len(toks) == 5:
            return self._attach_agent(toks[3], toks[4])
        if toks[:2] == ["cat", "/proc/net/unix"] and len(toks) >= 4 and toks[2] == "|" \
                and toks[3] == "grep":
            return 0, self._proc_net_unix(grep=toks[4]), ""
        self.unexpected.append(cmd)
        return 127, "", f"/system/bin/sh: {toks[0]}: inaccessible or not found"

    def _proc_net_unix(self, grep: str) -> str:
        # Num RefCount Protocol Flags Type St Inode Path: a listener has Flags
        # 00010000 (__SO_ACCEPTCON); the agent's end of each client connection
        # is listed under the same name with Flags 0 and St 03 (connected), for
        # as long as it is open, even after the listener has gone.
        with self._lock:
            names = [n for n, a in self.sockets.items() if a.running or a.lingering] \
                + list(self.foreign_sockets)
            agents = list(self.agents)
        entries = [(name, True) for name in ["jdwp-control", "adbd", *names]]
        for agent in agents:
            entries += [(agent.socket_name, False)] * agent.open_connections
        lines = []
        for i, (name, listening) in enumerate(entries):
            if listening:
                line = f"0000000000000000: 00000002 00000000 00010000 0001 01 {40000 + i} @{name}"
            else:
                line = f"0000000000000000: 00000003 00000000 00000000 0001 03 {40000 + i} @{name}"
            if grep in line:  # grep is a substring match
                lines.append(line)
        return "".join(line + "\n" for line in lines)

    def _run_as(self, package: str, rest: List[str]) -> Tuple[int, str, str]:
        app = self.apps.get(package)
        if app is None:
            return 1, "", f"run-as: unknown package: {package}"
        if not app.debuggable:
            return 1, "", f"run-as: package not debuggable: {package}"
        if rest == ["true"]:
            return 0, "", ""
        if rest == ["pwd"]:
            return 0, f"{app.data_dir}\n", ""
        if rest[:2] == ["sh", "-c"] and len(rest) == 3:
            for step in rest[2].split(" && "):
                parts = shlex.split(step)
                if parts[:2] == ["rm", "-f"]:
                    self.staged.pop((package, parts[2]), None)
                elif parts[:1] == ["cat"] and len(parts) == 4 and parts[2] == ">":
                    src, dst = parts[1], parts[3]
                    if src not in self.pushed:
                        return 1, "", f"cat: {src}: No such file or directory"
                    self.staged[(package, dst)] = (src, None)
                elif parts[:1] == ["chmod"] and len(parts) == 3:
                    key = (package, parts[2])
                    if key in self.staged:
                        self.staged[key] = (self.staged[key][0], parts[1])
                else:
                    self.unexpected.append(f"run-as {package} sh -c {step}")
                    return 1, "", f"sh: unsupported step {step!r}"
            return 0, "", ""
        self.unexpected.append(f"run-as {package} {' '.join(rest)}")
        return 1, "", "run-as: unsupported"

    def _attach_agent(self, package: str, spec: str) -> Tuple[int, str, str]:
        app = self.apps.get(package)
        if app is None or app.pid is None:
            return 1, "", f"Unknown process: {package}"
        so_path, _, options = spec.partition("=")
        boot, payload, socket_name = (options.split(":") + ["", "", ""])[:3]
        call = {"package": package, "so": so_path, "bootstrap": boot, "payload": payload,
                "socket_name": socket_name}
        self.attach_calls.append(call)
        if app.frozen:
            call["result"] = "queued until the app is unfrozen"
            return 0, "", ""

        def staged(path: str, name: str, mode: str) -> bool:
            entry = self.staged.get((package, name))
            return path == f"{app.data_dir}/{name}" and entry is not None and entry[1] == mode

        # The native agent only comes up when every artifact is where it expects
        # it (the .so owner-executable, the dex/jar read-only); otherwise the
        # attach "succeeds" and the failure is only visible in logcat.
        if not (staged(so_path, injectmod.NATIVE_SO_NAME, "700")
                and staged(boot, injectmod.BOOTSTRAP_DEX_NAME, "444")
                and staged(payload, injectmod.PAYLOAD_JAR_NAME, "444")):
            call["error"] = "artifacts not staged"
            return 0, "", ""
        existing = self.sockets.get(socket_name)
        if existing is not None and (existing.running or existing.lingering):
            # Server.kt can't bind a name another server holds: the new payload
            # logs the error and exits, the old server keeps serving (H4).
            call["result"] = "already-bound"
            return 0, "", ""
        # The payload hashes the jar it was loaded from (Payload.kt buildId).
        payload_src = self.staged[(package, injectmod.PAYLOAD_JAR_NAME)][0]
        build_id = self.pushed_sha.get(payload_src)
        with self._lock:
            agent = FakeAgent(scene=self.scene_factory(), behaviour=self.behaviour,
                              socket_name=socket_name, generation=len(self.agents) + 1,
                              on_request=self._on_request, on_stop=self._on_stop,
                              build_id=build_id)
            agent.package = package  # type: ignore[attr-defined]
            self.agents.append(agent)
            self.sockets[socket_name] = agent
        call["result"] = f"started generation {agent.generation}"
        return 0, "", ""

    def forward(self, args: List[str]) -> Tuple[int, str, str]:
        if args[:1] == ["--remove"] and len(args) == 2:
            port = int(args[1].split(":", 1)[1])
            with self._lock:
                entry = self.forwards.pop(port, None)
            if entry is None:
                return 1, "", f"adb: error: listener 'tcp:{port}' not found"
            _close(entry[1])
            return 0, "", ""
        if len(args) == 2 and args[0].startswith("tcp:") and args[1].startswith("localabstract:"):
            port = int(args[0].split(":", 1)[1])
            name = args[1].split(":", 1)[1]
            lsock = socket.socket()
            lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with self._lock:
                old = self.forwards.pop(port, None)
            if old is not None:
                _close(old[1])
            try:
                lsock.bind(("127.0.0.1", port))
            except OSError as exc:
                lsock.close()
                return 1, "", f"adb: error: cannot bind listener: {exc}"
            lsock.listen(16)
            actual = lsock.getsockname()[1]
            with self._lock:
                self.forwards[actual] = (name, lsock)
            threading.Thread(target=self._forward_accept, args=(lsock, name),
                             name=f"fakeadb-forward-{actual}", daemon=True).start()
            return 0, (f"{actual}\n" if port == 0 else ""), ""
        self.unexpected.append("forward " + " ".join(args))
        return 1, "", "adb: usage: forward"

    def _forward_accept(self, lsock: socket.socket, name: str) -> None:
        while True:
            try:
                conn, _ = lsock.accept()
            except OSError:
                return
            agent = self.sockets.get(name)
            if agent is not None and agent.lingering:
                # The old accept() returns, sees the server stopped, drops the
                # connection and exits: only now is the name released.
                with self._lock:
                    if self.sockets.get(name) is agent:
                        del self.sockets[name]
                    agent.lingering = False
                _close(conn)
                continue
            if agent is None or not agent.running:
                _close(conn)  # adb accepts, fails the device-side connect, closes
                continue
            agent.serve(conn)

    def forward_names(self) -> List[str]:
        with self._lock:
            return [name for name, _s in self.forwards.values()]

    def close(self) -> None:
        for agent in list(self.agents):
            agent.stop()
        with self._lock:
            forwards = list(self.forwards.values())
            self.forwards.clear()
        for _name, s in forwards:
            _close(s)


class FakeAdb:
    """Drop-in for ``inspector_widget.adb._run``: routes ``adb ...`` to FakeDevices."""

    def __init__(self, *devices: FakeDevice) -> None:
        self.devices: Dict[str, FakeDevice] = {d.serial: d for d in devices}
        self.log: List[List[str]] = []

    def run(self, argv: Sequence[str], *, timeout: float = adbmod.DEFAULT_TIMEOUT,
            check: bool = True, binary: bool = False) -> subprocess.CompletedProcess:
        argv = list(argv)
        self.log.append(argv)
        rc, out, err = self._dispatch(argv)
        if check and rc != 0:
            raise adbmod.AdbError(argv, rc, out, err)
        return subprocess.CompletedProcess(argv, rc, out.encode() if binary else out, err)

    def _dispatch(self, argv: List[str]) -> Tuple[int, str, str]:
        if not argv or argv[0] != "adb":
            return 1, "", f"not an adb command: {argv!r}"
        rest = argv[1:]
        serial: Optional[str] = None
        if rest[:1] == ["-s"]:
            serial, rest = rest[1], rest[2:]
        if rest == ["devices"]:
            body = "".join(f"{d.serial}\t{d.state}\n" for d in self.devices.values())
            return 0, "List of devices attached\n" + body + "\n", ""
        if serial is None:
            if len(self.devices) != 1:
                return 1, "", "adb: more than one device/emulator"
            device = next(iter(self.devices.values()))
        else:
            device = self.devices.get(serial)
            if device is None:
                return 1, "", f"adb: device '{serial}' not found"
        return device.handle(rest)


# --------------------------------------------------------------------------- #
# Installation.
# --------------------------------------------------------------------------- #
def make_build_out(path: str) -> str:
    """Placeholder on-device artifacts so the cold path's existence checks pass
    (only written if absent, so a test can "rebuild" by rewriting one)."""
    os.makedirs(path, exist_ok=True)
    for name in (injectmod.NATIVE_SO_NAME, injectmod.BOOTSTRAP_DEX_NAME, injectmod.PAYLOAD_JAR_NAME):
        target = os.path.join(path, name)
        if not os.path.exists(target):
            with open(target, "wb") as f:
                f.write(b"fake " + name.encode())
    return path


def _patched_defaults(fn: Callable[..., Any], old: Any, new: Any) -> Optional[tuple]:
    defaults = getattr(fn, "__defaults__", None)
    if not defaults or old not in defaults:
        return None
    return tuple(new if d == old else d for d in defaults)


def build_id_of(build_out: str) -> str:
    """sha256 of build_out/payload.jar: the build id its agent reports."""
    with open(os.path.join(build_out, injectmod.PAYLOAD_JAR_NAME), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def install(monkeypatch, *devices: FakeDevice, build_out: str) -> FakeAdb:
    """Route every adb subprocess to ``devices`` for the duration of a test.

    ``monkeypatch``-scoped: everything is undone at teardown. Also points the
    cold path at a placeholder build-out directory (the real one is a build
    product and may not exist in a fresh checkout).
    """
    fake = FakeAdb(*devices)
    # The artifacts env vars outrank DEFAULT_BUILD_OUT (inject.resolve_build_out);
    # a developer who exported one must not steer the fake cold path elsewhere.
    for var in (injectmod.ARTIFACTS_ENV, injectmod.LEGACY_ARTIFACTS_ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(adbmod, "_run", fake.run)
    make_build_out(build_out)
    for d in devices:
        d.default_build_id = build_id_of(build_out)
    old = injectmod.DEFAULT_BUILD_OUT
    monkeypatch.setattr(injectmod, "DEFAULT_BUILD_OUT", build_out)
    new_defaults = _patched_defaults(injectmod.inject_and_connect, old, build_out)
    if new_defaults is not None:
        monkeypatch.setattr(injectmod.inject_and_connect, "__defaults__", new_defaults)
    return fake


def install_global(*devices: FakeDevice, build_out: str) -> FakeAdb:
    """Like :func:`install` but permanent; for a child process (the stdio tests)."""
    fake = FakeAdb(*devices)
    for var in (injectmod.ARTIFACTS_ENV, injectmod.LEGACY_ARTIFACTS_ENV):
        os.environ.pop(var, None)
    adbmod._run = fake.run  # type: ignore[assignment]
    make_build_out(build_out)
    for d in devices:
        d.default_build_id = build_id_of(build_out)
    old = injectmod.DEFAULT_BUILD_OUT
    injectmod.DEFAULT_BUILD_OUT = build_out
    new_defaults = _patched_defaults(injectmod.inject_and_connect, old, build_out)
    if new_defaults is not None:
        injectmod.inject_and_connect.__defaults__ = new_defaults
    return fake


def default_device(**kwargs: Any) -> FakeDevice:
    """emulator-5554 with the probe app running (pid 4242), a non-debuggable
    running app, and a debuggable app that is installed but not running."""
    dev = FakeDevice(**kwargs)
    dev.add_app(DEFAULT_PACKAGE, DEFAULT_PID)
    dev.add_app("com.example.release", 5151, debuggable=False)
    dev.add_app("com.example.idle", None)
    return dev


# --------------------------------------------------------------------------- #
# Child-process entry point: run the MCP server over stdio against a fake device.
#   python tests/fakeagent.py --mcp-child LOG BUILD_OUT [--block-mcp]
# Every wire request is appended to LOG as JSON; at exit a final record lists the
# adb forwards still open and the agents still running.
# --------------------------------------------------------------------------- #
def _mcp_child(log_path: str, build_out: str, block_mcp: bool) -> None:
    import runpy

    if block_mcp:
        class _Block:
            def find_spec(self, name, path=None, target=None):
                if name == "mcp" or name.startswith("mcp."):
                    raise ImportError("mcp blocked for test")
                return None
        sys.meta_path.insert(0, _Block())

    dev = default_device(log_path=log_path)
    install_global(dev, build_out=build_out)

    def _exit_record() -> None:
        dev._log({"event": "exit", "forwards": dev.forward_names(),
                  "running_agents": [a.generation for a in dev.agents if a.running],
                  "adb": [" ".join(a) for a in dev.adb_log]})

    atexit.register(_exit_record)  # registered first -> runs after any server atexit hook
    script = os.path.join(_HOST_DIR, "mcp_server.py")
    sys.argv = [script]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--mcp-child":
        _mcp_child(sys.argv[2], sys.argv[3], "--block-mcp" in sys.argv[4:])
    else:
        print(__doc__)
