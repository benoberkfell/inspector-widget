"""Raw captures for the capture index tests (and later capture work packages).

* :func:`raw_from_scene` fetches a :class:`~.model.RawCapture` from an F1 scene
  (``fakescenes.wide_scene`` / ``replay_scene``) through its in-process session,
  the way the capture fetcher will: GetWindows, DumpTree with properties,
  DumpCompose (semantics) and DumpCompose (slot table) separately, DumpA11y.
* :class:`V`, :class:`C` and :class:`S` describe a screen (Views, Compose
  semantics nodes, slot-table groups); :func:`encode` turns windows of them into
  the agent's protobuf responses. Accessibility nodes follow the agent's
  post-ID1 contract (CONTRACT.md section 9): a View is ``(udid, -1)``, a Compose
  node ``(acv, semanticsId)``, and the ACV's own node stands for its root
  semantics node. ``pre_id1=True`` reproduces the broken agent instead (every
  host is the window root, virtual ids are accessibility view ids shared by all
  Compose nodes of a ComposeView), and ``window_relative`` reproduces the pre-CO4
  window-relative Compose bounds.
* :func:`mixed_scene`: a RecyclerView with three ComposeView cells (equal
  semantics ids) and one View cell, a ComposeView hosting an AndroidView that
  itself contains a nested ComposeView, and a dialog window.
* :func:`default_like_scene`: a port of the harness ``fakeagent.default_scene``.
* :func:`big_scene`: 13 index nodes per cell (6,502 for 500 cells), for build timing.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from inspector_widget.capture.model import CaptureMeta, CaptureOptions, RawCapture
from inspector_widget.proto import view_inspection_pb2 as pb

P = pb.Property
SERIAL = "emulator-5554"
PACKAGE = "com.oberkfell.a11yprobe"


# --------------------------------------------------------------------------- #
# F1 scenes
# --------------------------------------------------------------------------- #
def meta_for(session: Any, capture_id: str = "ctest1", **kw: Any) -> CaptureMeta:
    return CaptureMeta(id=capture_id, lineage=(session.serial, session.package),
                       pid=getattr(session, "pid", None), api=getattr(session, "api_level", None),
                       abi=getattr(session, "abi", None),
                       agent_version=getattr(session, "agent_version", None),
                       options=CaptureOptions(), **kw)


def raw_from_scene(scene: Any, *, slots: bool = True, props: bool = True,
                   capture_id: str = "ctest1") -> RawCapture:
    """A RawCapture of an F1 SceneData (no screenshots)."""
    s = scene.session()
    raw = RawCapture(meta=meta_for(s, capture_id))
    raw.windows = s.get_windows().SerializeToString()
    raw.views = s.dump_tree(0, include_properties=props).SerializeToString()
    raw.compose_sem = s.dump_compose(0, include_semantics=True,
                                     include_slot_table=False).SerializeToString()
    if slots:
        raw.slots = s.dump_compose(0, include_semantics=False,
                                   include_slot_table=True).SerializeToString()
    raw.a11y = s.dump_a11y(0, include_extras=True).SerializeToString()
    return raw


# --------------------------------------------------------------------------- #
# Scene description
# --------------------------------------------------------------------------- #
Box = tuple[int, int, int, int]


@dataclass
class S:
    """A slot-table group (COMPOSABLE node)."""

    name: str
    src: str | None
    b: Box
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[S] = field(default_factory=list)
    render_node_id: int = 0


@dataclass
class C:
    """A Compose semantics node. ``a11y`` holds its virtual node's fields (None:
    no accessibility twin); ``unmerged`` adds virtual children that are not in the
    (merged) semantics dump, as ``(semantics id, fields)``."""

    id: int
    b: Box
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[C] = field(default_factory=list)
    a11y: dict[str, Any] | None = field(default_factory=lambda: {"class_name": "android.view.View"})
    unmerged: list[tuple[int, Box, dict[str, Any]]] = field(default_factory=list)


@dataclass
class V:
    """A View. ``a11y`` holds its node's fields (None: not important for
    accessibility, children are lifted). An AndroidComposeView carries ``sem``
    (its root semantics node) and ``slots``."""

    id: int
    cls: str
    b: Box
    pkg: str = "android.widget"
    rid: str | None = None
    text: str | None = None
    children: list[V] = field(default_factory=list)
    a11y: dict[str, Any] | None = None
    props: dict[str, Any] = field(default_factory=dict)
    webview: bool = False
    sem: C | None = None
    slots: list[S] = field(default_factory=list)

    def walk(self) -> Iterator[V]:
        yield self
        for c in self.children:
            yield from c.walk()


class _St:
    def __init__(self) -> None:
        self._ids: dict[str, int] = {}

    def id(self, s: Any) -> int:
        if s is None or s == "":
            return 0
        s = str(s)
        if s not in self._ids:
            self._ids[s] = len(self._ids) + 1
        return self._ids[s]

    def fill(self, msg: Any) -> None:
        msg.SetInParent()
        for s, i in self._ids.items():
            msg.entries.add(id=i, str=s)


def _set_rect(msg: Any, b: Sequence[int]) -> None:
    msg.layout.x, msg.layout.y, msg.layout.w, msg.layout.h = (int(v) for v in b)


def _shift(b: Box, dx: int, dy: int) -> Box:
    return (b[0] - dx, b[1] - dy, b[2], b[3])


_A11Y_TEXT = ("text", "content_description", "hint_text", "state_description", "class_name",
              "package_name", "view_id_resource_name", "provider_class", "role_description",
              "pane_title", "unique_id")
_A11Y_BOOL = {f.name for f in pb.A11yNode.DESCRIPTOR.fields if f.type == f.TYPE_BOOL}


def _fill_a11y(st: _St, out: Any, host: int, virt: int, b: Box, spec: Mapping[str, Any]) -> None:
    out.host_view_id = host
    out.virtual_id = virt
    out.is_virtual = virt != -1
    _set_rect(out.bounds, b)
    out.enabled = True
    out.visible_to_user = True
    out.package_name = st.id(PACKAGE)
    for k, v in spec.items():
        if k in _A11Y_TEXT:
            setattr(out, k, st.id(v))
        elif k in _A11Y_BOOL:
            setattr(out, k, bool(v))
        elif k == "actions":
            for aid in v:
                out.actions.add(id=int(aid))
        elif k == "collection":
            out.collection_info.row_count, out.collection_info.column_count = v
        elif k == "item":
            out.collection_item_info.row_index, out.collection_item_info.column_index = v
        elif k in ("labeled_by", "label_for", "traversal_before", "traversal_after"):
            setattr(out, k, int(v))
        else:
            raise KeyError(f"unknown a11y field {k}")


def hkey(host: int, virt: int) -> int:
    """The agent's host key space (linkage ids)."""
    return (host << 32) ^ (virt & 0xFFFFFFFF)


def encode(windows: Sequence[V], *, pre_id1: bool = False, window_relative: bool = False,
           compose_diag: str | None = None, capture_id: str = "cmixd0",
           slots: bool = True) -> RawCapture:
    """Protobuf responses of a screen, as a RawCapture."""
    meta = CaptureMeta(id=capture_id, lineage=(SERIAL, PACKAGE), pid=4242, api=36,
                       abi="arm64-v8a", agent_version="viewspector-0.1",
                       device={"dpi": 420, "font_scale": 1.0, "screen": [1080, 2400]},
                       options=CaptureOptions())
    raw = RawCapture(meta=meta)
    gw = pb.GetWindowsResponse()
    gw.root_ids.extend(w.id for w in windows)
    gw.strings.SetInParent()
    raw.windows = gw.SerializeToString()

    # views + a few flag properties
    st = _St()
    dt = pb.DumpTreeResponse()

    def enc_view(v: V, out: Any) -> None:
        out.id = v.id
        out.class_name = st.id(v.cls)
        out.package_name = st.id(v.pkg)
        _set_rect(out.bounds, v.b)
        if v.rid:
            out.resource.namespace = st.id(PACKAGE)
            out.resource.type = st.id("id")
            out.resource.name = st.id(v.rid)
            out.view_id_name = st.id(v.rid)
        if v.text:
            out.text_value = st.id(v.text)
        if v.webview:
            out.flags = pb.ViewNode.IS_WEBVIEW
        for c in v.children:
            enc_view(c, out.children.add())

    for w in windows:
        enc_view(w, dt.roots.add())
    for w in windows:
        for v in w.walk():
            g = dt.properties.add(view_id=v.id)
            props = {"visibility": "visible", "enabled": True, "alpha": 1.0, **v.props}
            for name, val in props.items():
                if isinstance(val, bool):
                    g.properties.add(name=st.id(name), type=P.BOOLEAN, int32_value=int(val))
                elif isinstance(val, float):
                    g.properties.add(name=st.id(name), type=P.FLOAT, float_value=val)
                elif isinstance(val, int):
                    g.properties.add(name=st.id(name), type=P.INT32, int32_value=val)
                else:
                    g.properties.add(name=st.id(name), type=P.INT_ENUM, str_value=st.id(val))
    st.fill(dt.strings)
    raw.views = dt.SerializeToString()

    # compose: one window per AndroidComposeView, synthetic root folded host-side
    acvs = [(w, v) for w in windows for v in w.walk() if v.sem is not None or v.slots]
    sem_resp, slot_resp = pb.DumpComposeResponse(), pb.DumpComposeResponse()
    sst, lst = _St(), _St()
    for w, acv in acvs:
        dx, dy = (w.b[0], w.b[1]) if window_relative else (0, 0)
        for resp, table, want in ((sem_resp, sst, "sem"), (slot_resp, lst, "slots")):
            win = resp.windows.add(view_id=acv.id)
            root = win.root
            root.id = acv.id
            root.name = table.id("AndroidComposeView")
            root.kind = pb.ComposeNode.COMPOSABLE
            _set_rect(root.bounds, acv.b)
            if want == "sem" and acv.sem is not None:
                _enc_sem(table, acv.sem, root.children.add(), dx, dy)
            if want == "slots":
                for s in acv.slots:
                    _enc_slot(table, s, root.children.add(), dx, dy)
    diag = compose_diag if compose_diag is not None else (
        f"found {len(acvs)} AndroidComposeView(s)" + ("" if window_relative else "; bounds=screen"))
    sem_resp.diagnostics = slot_resp.diagnostics = diag
    sst.fill(sem_resp.strings)
    lst.fill(slot_resp.strings)
    raw.compose_sem = sem_resp.SerializeToString()
    raw.slots = slot_resp.SerializeToString() if slots else None

    # accessibility
    ast = _St()
    a11y = pb.DumpA11yResponse()
    for w in windows:
        win = a11y.windows.add(root_view_id=w.id)
        counter = iter(range(2, 1_000_000))
        _enc_a11y_view(ast, w, win.root, w, pre_id1, counter, {})
    ast.fill(a11y.strings)
    raw.a11y = a11y.SerializeToString()
    return raw


def _enc_sem(st: _St, c: C, out: Any, dx: int, dy: int) -> None:
    out.id = c.id
    out.kind = pb.ComposeNode.SEMANTICS
    label = c.attrs.get("Text") or c.attrs.get("ContentDescription") or c.attrs.get("TestTag")
    out.name = st.id(label or "Node")
    _set_rect(out.bounds, _shift(c.b, dx, dy))
    for k, v in c.attrs.items():
        out.attrs.add(key=st.id(k), value=st.id(v))
    for ch in c.children:
        _enc_sem(st, ch, out.children.add(), dx, dy)


def _enc_slot(st: _St, s: S, out: Any, dx: int, dy: int) -> None:
    out.id = 0
    out.kind = pb.ComposeNode.COMPOSABLE
    out.name = st.id(s.name)
    out.source = st.id(s.src)
    out.render_node_id = s.render_node_id
    _set_rect(out.bounds, _shift(s.b, dx, dy))
    for k, v in s.attrs.items():
        out.attrs.add(key=st.id(k), value=st.id(v))
    for ch in s.children:
        _enc_slot(st, ch, out.children.add(), dx, dy)


def _ids(pre_id1: bool, window: V, host: int, virt: int, counter: Iterator[int],
         acv_ids: dict[int, int]) -> tuple[int, int]:
    """Post-ID1 ids, or the pre-ID1 agent's: every host is the window root and the
    virtual id is an accessibility view id (one per View, one per ComposeView)."""
    if not pre_id1:
        return host, virt
    if virt == -1 and host == window.id:
        return host, -1
    if virt == -1:
        return window.id, next(counter)
    if host not in acv_ids:
        acv_ids[host] = next(counter)
    return window.id, acv_ids[host]


def _enc_a11y_view(st: _St, v: V, out: Any, window: V, pre_id1: bool, counter: Iterator[int],
                   acv_ids: dict[int, int]) -> bool:
    """Encode ``v`` into ``out`` if it has a11y fields; else lift its children into
    ``out`` (returns whether ``out`` was used for ``v``)."""
    host, virt = _ids(pre_id1, window, v.id, -1, counter, acv_ids)
    _fill_a11y(st, out, host, virt, v.b, v.a11y or {"class_name": "android.view.View"})
    if v.sem is not None:
        for c in v.sem.children:
            _enc_a11y_sem(st, v, c, out, window, pre_id1, counter, acv_ids)
    for c in v.children:
        _enc_a11y_children(st, c, out, window, pre_id1, counter, acv_ids)
    return True


def _enc_a11y_children(st: _St, v: V, parent: Any, window: V, pre_id1: bool,
                       counter: Iterator[int], acv_ids: dict[int, int]) -> None:
    if v.a11y is None and v.sem is None:
        for c in v.children:
            _enc_a11y_children(st, c, parent, window, pre_id1, counter, acv_ids)
        return
    _enc_a11y_view(st, v, parent.children.add(), window, pre_id1, counter, acv_ids)


def _enc_a11y_sem(st: _St, acv: V, c: C, parent: Any, window: V, pre_id1: bool,
                  counter: Iterator[int], acv_ids: dict[int, int]) -> None:
    if c.a11y is None:
        for ch in c.children:
            _enc_a11y_sem(st, acv, ch, parent, window, pre_id1, counter, acv_ids)
        return
    out = parent.children.add()
    host, virt = _ids(pre_id1, window, acv.id, c.id, counter, acv_ids)
    _fill_a11y(st, out, host, virt, c.b, c.a11y)
    for sid, b, spec in c.unmerged:
        h2, v2 = _ids(pre_id1, window, acv.id, sid, counter, acv_ids)
        _fill_a11y(st, out.children.add(), h2, v2, b, spec)
    for ch in c.children:
        _enc_a11y_sem(st, acv, ch, out, window, pre_id1, counter, acv_ids)


# --------------------------------------------------------------------------- #
# Scenes
# --------------------------------------------------------------------------- #
CLICK = {"clickable": True, "focusable": True, "actions": [16]}
ON_CLICK = "AccessibilityAction(label=null, action=Function0<java.lang.Boolean>)"


def feed_cell(cv_id: int, acv_id: int, y: int, n: int) -> V:
    """A RecyclerView cell holding a ComposeView: a clickable card (merged text)
    with a Delete button. Semantics ids are the same in every cell."""
    card = (16, y + 16, 1048, 268)
    button = (900, y + 200, 150, 64)
    title = (32, y + 32, 300, 48)
    sem = C(1, (0, y, 1080, 300), children=[
        C(2, card, attrs={"TestTag": "card", "Text": f"Item {n}", "OnClick": ON_CLICK},
          a11y={"class_name": "android.view.View", **CLICK},
          unmerged=[(3, title, {"class_name": "android.widget.TextView", "text": f"Item {n}"})],
          children=[
              C(4, button, attrs={"Role": "Button", "Text": "Delete", "OnClick": ON_CLICK},
                a11y={"class_name": "android.widget.Button", "text": "Delete", **CLICK}),
          ]),
    ], a11y=None)
    slots = [S("FeedRow", "FeedRow.kt:42", card, {"item": f"Item {n}"}, children=[
        S("Card", "FeedRow.kt:44", card, {"modifier": "Modifier"}, children=[
            S("Surface", "Card.kt:88", card, children=[
                S("Text", "FeedRow.kt:47", title, {"text": f"Item {n}", "maxLines": "1",
                                                   "overflow": "2"}),
                S("Button", "FeedRow.kt:52", button, {"onClick": "Function0"}, children=[
                    S("Surface", "Button.kt:120", button, children=[
                        S("Text", "FeedRow.kt:53", (920, y + 216, 110, 32), {"text": "Delete"}),
                    ]),
                ]),
            ]),
        ]),
    ])]
    acv = V(acv_id, "AndroidComposeView", (0, y, 1080, 300), "androidx.compose.ui.platform",
            a11y={"class_name": "android.view.View",
                  "provider_class": "androidx.compose.ui.platform.AndroidComposeView"},
            sem=sem, slots=slots)
    return V(cv_id, "ComposeView", (0, y, 1080, 300), "androidx.compose.ui.platform",
             children=[acv], a11y=None)


def mixed_windows(*, dialog: bool = True) -> list[V]:
    cells = [feed_cell(11, 12, 200, 1), feed_cell(21, 22, 500, 2), feed_cell(31, 32, 800, 3)]
    view_cell = V(41, "LinearLayout", (0, 1100, 1080, 300), rid="row",
                  a11y={"class_name": "android.widget.LinearLayout"}, children=[
                      V(42, "TextView", (32, 1116, 400, 48), rid="title", text="Item 4 (view)",
                        a11y={"class_name": "android.widget.TextView", "text": "Item 4 (view)",
                              "view_id_resource_name": f"{PACKAGE}:id/title"}),
                      V(43, "MaterialButton", (900, 1300, 150, 64), "com.google.android.material.button",
                        rid="delete", text="Delete", props={"clickable": True},
                        a11y={"class_name": "android.widget.Button", "text": "Delete",
                              "view_id_resource_name": f"{PACKAGE}:id/delete", **CLICK,
                              "labeled_by": hkey(42, -1)}),
                  ])
    # a ComposeView whose AndroidView holds a TextView and a nested ComposeView
    nested_acv = V(58, "AndroidComposeView", (600, 1600, 300, 100), "androidx.compose.ui.platform",
                   a11y={"class_name": "android.view.View"},
                   sem=C(1, (600, 1600, 300, 100), a11y=None, children=[
                       C(2, (600, 1600, 300, 100),
                         attrs={"Role": "Button", "Text": "Zoom", "OnClick": ON_CLICK},
                         a11y={"class_name": "android.widget.Button", "text": "Zoom", **CLICK}),
                   ]))
    interop = V(54, "ViewFactoryHolder", (0, 1500, 1080, 400), "androidx.compose.ui.viewinterop",
                children=[V(55, "FrameLayout", (0, 1500, 1080, 400), children=[
                    V(56, "TextView", (100, 1600, 300, 50), text="Map tile",
                      a11y={"class_name": "android.widget.TextView", "text": "Map tile"}),
                    V(57, "ComposeView", (600, 1600, 300, 100), "androidx.compose.ui.platform",
                      children=[nested_acv]),
                ])])
    map_acv = V(52, "AndroidComposeView", (0, 1400, 1080, 600), "androidx.compose.ui.platform",
                a11y={"class_name": "android.view.View"},
                sem=C(1, (0, 1400, 1080, 600), a11y=None, children=[
                    C(5, (32, 1416, 200, 48), attrs={"Text": "Map"},
                      a11y={"class_name": "android.widget.TextView", "text": "Map"}),
                    C(6, (0, 1480, 1080, 500), attrs={"TestTag": "map"}),
                ]),
                slots=[S("MapScreen", "MapScreen.kt:20", (0, 1400, 1080, 600), children=[
                    S("Text", "MapScreen.kt:22", (32, 1416, 200, 48), {"text": "Map"}),
                    S("AndroidView", "MapScreen.kt:25", (0, 1480, 1080, 500),
                      {"modifier": "[TestTagElement@1]", "modifiers": "testTag(tag=map)"}),
                ])],
                children=[V(53, "AndroidViewsHandler", (0, 1400, 1080, 600),
                            "androidx.compose.ui.platform", children=[interop])])
    map_cell = V(51, "ComposeView", (0, 1400, 1080, 600), "androidx.compose.ui.platform",
                 children=[map_acv])
    feed = V(10, "RecyclerView", (0, 200, 1080, 2000), "androidx.recyclerview.widget", rid="feed",
             a11y={"class_name": "androidx.recyclerview.widget.RecyclerView", "scrollable": True,
                   "collection": (5, 1), "view_id_resource_name": f"{PACKAGE}:id/feed"},
             children=[*cells, view_cell, map_cell])
    main = V(1, "DecorView", (0, 0, 1080, 2400), "com.android.internal.policy",
             a11y={"class_name": "android.widget.FrameLayout"}, children=[
                 V(2, "LinearLayout", (0, 0, 1080, 2400), children=[
                     V(3, "FrameLayout", (0, 0, 1080, 2400), rid="content", children=[feed]),
                 ]),
                 V(90, "View", (0, 0, 1080, 100), "android.view", rid="statusBarBackground",
                   props={"visibility": "invisible"}),
             ])
    if not dialog:
        return [main]
    dialog_acv = V(103, "AndroidComposeView", (140, 900, 800, 600), "androidx.compose.ui.platform",
                   a11y={"class_name": "android.view.View"},
                   sem=C(1, (140, 900, 800, 600), a11y=None, children=[
                       C(2, (164, 924, 500, 60), attrs={"Text": "Delete item?", "Heading": "kotlin.Unit"},
                         a11y={"class_name": "android.widget.TextView", "text": "Delete item?",
                               "heading": True}),
                       C(3, (540, 1400, 180, 72),
                         attrs={"Role": "Button", "Text": "Cancel", "OnClick": ON_CLICK},
                         a11y={"class_name": "android.widget.Button", "text": "Cancel", **CLICK}),
                       C(4, (740, 1400, 180, 72),
                         attrs={"Role": "Button", "Text": "OK", "OnClick": ON_CLICK},
                         a11y={"class_name": "android.widget.Button", "text": "OK", **CLICK}),
                   ]),
                   slots=[S("ConfirmDialog", "Dialogs.kt:30", (140, 900, 800, 600), children=[
                       S("Text", "Dialogs.kt:33", (164, 924, 500, 60), {"text": "Delete item?"}),
                       S("TextButton", "Dialogs.kt:36", (540, 1400, 180, 72)),
                       S("TextButton", "Dialogs.kt:39", (740, 1400, 180, 72)),
                   ])])
    dialog_root = V(100, "DecorView", (140, 900, 800, 600), "com.android.internal.policy",
                    a11y={"class_name": "android.widget.FrameLayout"}, children=[
                        V(101, "FrameLayout", (140, 900, 800, 600), rid="content", children=[
                            V(102, "ComposeView", (140, 900, 800, 600),
                              "androidx.compose.ui.platform", children=[dialog_acv]),
                        ]),
                    ])
    return [main, dialog_root]


def mixed_scene(*, pre_id1: bool = False, window_relative: bool = True,
                dialog: bool = True) -> RawCapture:
    return encode(mixed_windows(dialog=dialog), pre_id1=pre_id1,
                  window_relative=window_relative)


def default_like_windows() -> list[V]:
    """The harness ``fakeagent.default_scene``: Views, one ComposeView, a popup."""
    sem = C(1, (0, 160, 360, 400), a11y=None, children=[
        C(2, (16, 176, 200, 56), attrs={"Text": "Submit", "Role": "Button", "OnClick": ON_CLICK},
          a11y={"class_name": "android.widget.Button", "text": "Submit", **CLICK}),
        C(3, (240, 176, 96, 96), attrs={"Role": "Image"},
          a11y={"class_name": "android.widget.ImageView"}),
        C(4, (16, 288, 328, 40), attrs={"Text": "Settings", "Heading": "kotlin.Unit"},
          a11y={"class_name": "android.widget.TextView", "text": "Settings", "heading": True}),
        C(5, (16, 340, 328, 56), attrs={"Text": "Wi-Fi", "Role": "Switch", "ToggleableState": "On",
                                        "OnClick": ON_CLICK},
          a11y={"class_name": "android.widget.Switch", "text": "Wi-Fi", "state_description": "On",
                "checkable": True, "checked": True, **CLICK}),
        C(6, (16, 440, 64, 64), attrs={"OnClick": ON_CLICK},
          a11y={"class_name": "android.view.View", **CLICK}),
    ])
    slots = [S("ProbeScreen", "MainActivity.kt:31", (0, 160, 360, 400), children=[
        S("SubmitButton", "MainActivity.kt:42", (16, 176, 200, 56), {"text": "Submit"},
          render_node_id=7002),
        S("Image", "MainActivity.kt:57", (240, 176, 96, 96), render_node_id=7003),
    ])]
    acv = V(1006, "AndroidComposeView", (0, 160, 360, 400), "androidx.compose.ui.platform",
            a11y={"class_name": "android.view.View",
                  "provider_class": "androidx.compose.ui.platform.AndroidComposeView"},
            sem=sem, slots=slots)
    content = V(1002, "LinearLayout", (0, 0, 360, 640), rid="content",
                a11y={"class_name": "android.widget.LinearLayout"}, children=[
                    V(1003, "TextView", (16, 24, 328, 40), rid="title", text="Hello world",
                      a11y={"class_name": "android.widget.TextView", "text": "Hello world",
                            "view_id_resource_name": f"{PACKAGE}:id/title"}),
                    V(1004, "Button", (16, 80, 120, 48), rid="ok", text="OK",
                      a11y={"class_name": "android.widget.Button", "text": "OK", **CLICK,
                            "view_id_resource_name": f"{PACKAGE}:id/ok"}),
                    V(1005, "ImageView", (160, 80, 32, 32), rid="logo",
                      a11y={"class_name": "android.widget.ImageView"}),
                    acv,
                ])
    decor = V(1001, "DecorView", (0, 0, 360, 640), "com.android.internal.policy",
              a11y={"class_name": "android.widget.FrameLayout"}, children=[content])
    popup = V(2001, "PopupDecorView", (40, 560, 280, 64),
              a11y={"class_name": "android.widget.FrameLayout"}, children=[
                  V(2002, "TextView", (56, 576, 248, 32), text="Saved",
                    a11y={"class_name": "android.widget.TextView", "text": "Saved"}),
              ])
    return [decor, popup]


def default_like_scene() -> RawCapture:
    return encode(default_like_windows(), capture_id="cdeflt")


def big_scene(cells: int = 500) -> RawCapture:
    """A RecyclerView of ``cells`` ComposeView cells (13 index nodes per cell)."""
    rows = [feed_cell(10_000 + 2 * i, 10_001 + 2 * i, 200 + 300 * i, i + 1) for i in range(cells)]
    feed = V(10, "RecyclerView", (0, 200, 1080, 300 * cells), "androidx.recyclerview.widget",
             rid="feed", a11y={"class_name": "androidx.recyclerview.widget.RecyclerView",
                               "scrollable": True, "collection": (cells, 1)},
             children=rows)
    decor = V(1, "DecorView", (0, 0, 1080, 300 * cells + 400), "com.android.internal.policy",
              a11y={"class_name": "android.widget.FrameLayout"}, children=[feed])
    return encode([decor], capture_id="cbig00")


def all_views(windows: Iterable[V]) -> list[V]:
    return [v for w in windows for v in w.walk()]


__all__ = [
    "C",
    "S",
    "V",
    "all_views",
    "big_scene",
    "default_like_scene",
    "default_like_windows",
    "encode",
    "feed_cell",
    "hkey",
    "meta_for",
    "mixed_scene",
    "mixed_windows",
    "raw_from_scene",
]
