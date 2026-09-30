"""A mixed View/Compose screen that follows the ID contract, for the a11y host tests.

Layout (uniqueDrawingIds in brackets; every AndroidComposeView is its own Compose window
and every window reuses semantics ids 1..6, the worst case for key collisions)::

    DecorView [2]
      LinearLayout [10]                      (not important for a11y)
        TextView "Inbox" [11]                (heading)
        RecyclerView [20]                    (CollectionInfo 4x1)
          ComposeView [21] row 0 -> AndroidComposeView [22] -> AndroidViewsHandler [23]
          ComposeView [31] row 1 -> AndroidComposeView [32] -> AndroidViewsHandler [33]
          ComposeView [41] row 2 -> AndroidComposeView [42] -> AndroidViewsHandler [43]
            each cell: sem 2 clickable row "Item N (compose)"
                         sem 3 Checkbox (no label)     sem 4 IconButton "Delete"
          LinearLayout [50] row 3 (a View cell)
            TextView "Item 3 (view)" [51]    ImageButton [52] (no label)
        ComposeView [60] footer -> AndroidComposeView [61]
          sem 2 Text "Compose footer"; sem 3 an AndroidView hosting:
          AndroidViewsHandler [62] -> ViewFactoryHolder [63] -> RecyclerView [64]
            -> ComposeView [65] -> AndroidComposeView [66]: sem 2 "Nested item"

The a11y tree mirrors what the fixed agent emits: host_view_id is the node's own backing
View (the ACV for Compose virtual nodes), virtual_id is -1 for real Views, Compose's root
semantics node is the ACV host node itself, merged children and Compose's fake role node
are present, and linkage ids are in the host key space.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from inspector_widget.a11y import a11y_key
from inspector_widget.proto import view_inspection_pb2 as pb

from conftest import StringTableBuilder, make_bounds

W = 1080
CELL_H = 200
CELL_ACVS = {0: 22, 1: 32, 2: 42}  # row -> AndroidComposeView id
CELL_Y = {0: 220, 1: 420, 2: 620}
FAKE_ROLE_ID = 1_000_000_004  # Compose's fake Role child of the IconButton


def _b(x, y, w, h):
    return {"layout": {"x": x, "y": y, "w": w, "h": h}}


# --------------------------------------------------------------------------- views
def _view(vid, cls, bounds, text=None, children=None):
    d = {"id": vid, "class_name": cls, "qualified_name": cls, "bounds": _b(*bounds)}
    if text:
        d["text"] = text
    if children:
        d["children"] = children
    return d


def view_roots() -> List[dict]:
    cells = []
    for row, acv in CELL_ACVS.items():
        y = CELL_Y[row]
        cells.append(_view(acv - 1, "ComposeView", (0, y, W, CELL_H), children=[
            _view(acv, "AndroidComposeView", (0, y, W, CELL_H), children=[
                _view(acv + 1, "AndroidViewsHandler", (0, y, W, CELL_H))])]))
    view_cell = _view(50, "LinearLayout", (0, 820, W, CELL_H), children=[
        _view(51, "TextView", (40, 860, 800, 60), text="Item 3 (view)"),
        _view(52, "ImageButton", (940, 860, 120, 120))])
    footer = _view(60, "ComposeView", (0, 1300, W, 600), children=[
        _view(61, "AndroidComposeView", (0, 1300, W, 600), children=[
            _view(62, "AndroidViewsHandler", (0, 1300, W, 600), children=[
                _view(63, "ViewFactoryHolder", (40, 1500, 1000, 300), children=[
                    _view(64, "RecyclerView", (40, 1500, 1000, 300), children=[
                        _view(65, "ComposeView", (40, 1500, 1000, 150), children=[
                            _view(66, "AndroidComposeView", (40, 1500, 1000, 150), children=[
                                _view(67, "AndroidViewsHandler", (40, 1500, 1000, 150))])])])])])])])
    content = _view(10, "LinearLayout", (0, 0, W, 2400), children=[
        _view(11, "TextView", (0, 100, W, 120), text="Inbox"),
        _view(20, "RecyclerView", (0, 220, W, 1000), children=cells + [view_cell]),
        footer])
    return [_view(2, "DecorView", (0, 0, W, 2400), children=[content])]


# --------------------------------------------------------------------------- compose
def _cnode(cid, name, bounds, attrs=None, children=None):
    d = {"id": cid, "name": name, "kind": "SEMANTICS", "bounds": _b(*bounds),
         "attrs": attrs or {}}
    if children:
        d["children"] = children
    return d


def _cell_window(row: int) -> dict:
    acv, y = CELL_ACVS[row], CELL_Y[row]
    label = f"Item {row} (compose)"
    row_node = _cnode(2, label, (0, y, W, CELL_H),
                      {"Text": label, "OnClick": "AccessibilityAction(...)",
                       "TestTag": f"compose_cell_{row}"}, children=[
        _cnode(3, "Checkbox", (840, y + 60, 80, 80),
               {"Role": "Checkbox", "OnClick": "AccessibilityAction(...)",
                "ToggleableState": "On" if row == 1 else "Off"}),
        _cnode(4, "Delete", (950, y + 50, 100, 100),
               {"Role": "Button", "OnClick": "AccessibilityAction(...)",
                "ContentDescription": "Delete", "TestTag": "delete"})])
    root = _cnode(acv, "AndroidComposeView", (0, y, W, CELL_H), children=[
        _cnode(1, "Node", (0, y, W, CELL_H), children=[row_node])])
    return {"view_id": acv, "root": root}


def compose_windows() -> List[dict]:
    wins = [_cell_window(r) for r in CELL_ACVS]
    wins.append({"view_id": 61, "root": _cnode(61, "AndroidComposeView", (0, 1300, W, 600),
                                                children=[
        _cnode(1, "Column", (0, 1300, W, 600), children=[
            _cnode(2, "Compose footer", (40, 1320, 600, 80), {"Text": "Compose footer"}),
            _cnode(3, "Node", (40, 1500, 1000, 300))])])})
    wins.append({"view_id": 66, "root": _cnode(66, "AndroidComposeView", (40, 1500, 1000, 150),
                                                children=[
        _cnode(1, "Node", (40, 1500, 1000, 150), children=[
            _cnode(2, "Nested item", (40, 1500, 1000, 150),
                   {"Text": "Nested item", "OnClick": "AccessibilityAction(...)",
                    "TestTag": "nested_0"})])])})
    return wins


# --------------------------------------------------------------------------- a11y
class A11yBuilder:
    """Builds contract-following A11yNode protos."""

    def __init__(self) -> None:
        self.sb = StringTableBuilder()

    def node(self, host: int, virtual: int, bounds, *, cls: str = "android.view.View",
             text: Optional[str] = None, cd: Optional[str] = None,
             flags=("visible_to_user", "enabled"), provider: Optional[str] = None,
             children=None, **fields: Any) -> "pb.A11yNode":
        n = pb.A11yNode(host_view_id=host, virtual_id=virtual, is_virtual=virtual != -1,
                        bounds=make_bounds(*bounds), class_name=self.sb.intern(cls),
                        text=self.sb.intern(text), content_description=self.sb.intern(cd),
                        provider_class=self.sb.intern(provider))
        for f in flags:
            setattr(n, f, True)
        for k, v in fields.items():
            if k == "collection_info":
                n.collection_info.CopyFrom(pb.A11yCollectionInfo(**v))
            elif k == "collection_item_info":
                n.collection_item_info.CopyFrom(pb.A11yCollectionItemInfo(**v))
            elif k in ("hint_text", "state_description", "role_description", "error"):
                setattr(n, k, self.sb.intern(v))
            else:
                setattr(n, k, v)
        for c in children or []:
            n.children.add().CopyFrom(c)
        return n

    def response(self, roots) -> "pb.DumpA11yResponse":
        resp = pb.DumpA11yResponse()
        for r in roots:
            w = resp.windows.add()
            w.root_view_id = r.host_view_id
            w.root.CopyFrom(r)
        resp.strings.CopyFrom(self.sb.build())
        return resp


VIS = ("visible_to_user", "enabled")
FOCUS = ("visible_to_user", "enabled", "clickable", "focusable", "screen_reader_focusable")


def _cell_a11y(b: A11yBuilder, row: int) -> "pb.A11yNode":
    acv, y = CELL_ACVS[row], CELL_Y[row]
    checkbox_flags = FOCUS + (("checkable", "checked") if row == 1 else ("checkable",))
    icon = b.node(acv, 4, (944, y + 44, 112, 112), flags=FOCUS, children=[
        b.node(acv, 6, (976, y + 76, 48, 48), cd="Delete"),
        b.node(acv, FAKE_ROLE_ID, (950, y + 50, 100, 100), cls="android.widget.Button")])
    row_node = b.node(acv, 2, (0, y, W, CELL_H), flags=FOCUS, children=[
        b.node(acv, 5, (40, y + 70, 700, 60), cls="android.widget.TextView",
               text=f"Item {row} (compose)"),
        b.node(acv, 3, (820, y + 40, 120, 120), cls="android.widget.CheckBox",
               flags=checkbox_flags),
        icon])
    host = b.node(acv, -1, (0, y, W, CELL_H), provider="AndroidComposeView",
                  children=[row_node])
    return b.node(acv - 1, -1, (0, y, W, CELL_H), cls="androidx.compose.ui.platform.ComposeView",
                  collection_item_info={"row_index": row, "row_span": 1, "column_span": 1},
                  children=[host])


def a11y_response() -> "pb.DumpA11yResponse":
    b = A11yBuilder()
    cells = [_cell_a11y(b, r) for r in CELL_ACVS]
    view_cell = b.node(50, -1, (0, 820, W, CELL_H), cls="android.widget.LinearLayout",
                       collection_item_info={"row_index": 3, "row_span": 1, "column_span": 1},
                       children=[
        b.node(51, -1, (40, 860, 800, 60), cls="android.widget.TextView", text="Item 3 (view)"),
        b.node(52, -1, (940, 860, 120, 120), cls="android.widget.ImageButton",
               flags=VIS + ("clickable", "focusable"))])
    recycler = b.node(20, -1, (0, 220, W, 1000), cls="androidx.recyclerview.widget.RecyclerView",
                      flags=VIS + ("focusable", "scrollable"),
                      collection_info={"row_count": 4, "column_count": 1},
                      children=cells + [view_cell])
    nested = b.node(65, -1, (40, 1500, 1000, 150), cls="androidx.compose.ui.platform.ComposeView",
                    collection_item_info={"row_index": 0, "row_span": 1, "column_span": 1},
                    children=[
        b.node(66, -1, (40, 1500, 1000, 150), provider="AndroidComposeView", children=[
            b.node(66, 2, (40, 1500, 1000, 150), flags=FOCUS, children=[
                b.node(66, 5, (60, 1540, 400, 60), cls="android.widget.TextView",
                       text="Nested item")])])])
    footer = b.node(61, -1, (0, 1300, W, 600), provider="AndroidComposeView", children=[
        # Compose chains its traversal order into the AndroidView holder [63], which is
        # not important for accessibility, so the target is absent from the dump.
        b.node(61, 2, (40, 1320, 600, 80), cls="android.widget.TextView",
               text="Compose footer", flags=VIS + ("screen_reader_focusable",),
               traversal_before=a11y_key(63, -1)),
        b.node(64, -1, (40, 1500, 1000, 300), cls="androidx.recyclerview.widget.RecyclerView",
               flags=VIS + ("focusable", "scrollable"),
               collection_info={"row_count": 1, "column_count": 1}, children=[nested])])
    root = b.node(2, -1, (0, 0, W, 2400), cls="android.widget.FrameLayout", children=[
        b.node(11, -1, (0, 100, W, 120), cls="android.widget.TextView", text="Inbox",
               flags=VIS + ("heading",)),
        recycler, footer])
    return b.response([root])


EXPECTED_SPEECH = [
    "Inbox, heading",
    "Item 0 (compose)", "Unlabeled, checkbox, not checked", "Delete, button",
    "Item 1 (compose)", "Unlabeled, checkbox, checked", "Delete, button",
    "Item 2 (compose)", "Unlabeled, checkbox, not checked", "Delete, button",
    "Item 3 (view)", "Unlabeled, button",
    "Compose footer",
    "Nested item",
]


# --------------------------------------------------------------------------- findings
def untyped_compose_findings() -> List[dict]:
    """Findings as the Compose-semantics lint emits them today: node.id is a bare
    semantics id (repeated in every cell), bounds in screen px."""
    out = []
    for row in CELL_ACVS:
        y = CELL_Y[row]
        out.append({"rule": "a11y.label.missing", "severity": "error",
                    "node": {"id": 3, "name": "Checkbox", "role": "Checkbox", "source": None},
                    "bounds": {"x": 840, "y": y + 60, "w": 80, "h": 80}})
    out.append({"rule": "a11y.touch_target.small", "severity": "warn",
                "node": {"id": 4, "name": "Delete", "role": "Button", "source": None},
                "bounds": {"x": 950, "y": CELL_Y[1] + 50, "w": 100, "h": 100}})
    return out


def typed_findings() -> List[dict]:
    """Findings carrying typed keys (the unified-tree lint's contract)."""
    return [
        {"rule": "a11y.image.no_description", "severity": "error",
         "node": {"node_key": "view:52"}, "bounds": {"x": 940, "y": 860, "w": 120, "h": 120}},
        {"rule": "a11y.grouping.missing", "severity": "info",
         "node": {"host_view_id": 66, "virtual_id": 2},
         "bounds": {"x": 40, "y": 1500, "w": 1000, "h": 150}},
    ]
