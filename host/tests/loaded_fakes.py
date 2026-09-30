"""A stand-in for the store's ``LoadedCapture`` plus raw facets derived from indexes.

The capture store (capture/store.py, C2) is being built in parallel; the analyzers
(C7) and images (C9) code against the section 10 surface only:

    loaded.meta  loaded.index()  loaded.raw(name)  loaded.shot(root)
    loaded.derived(name)  loaded.put_derived(name, bytes) -> path
    loaded.derived_path(name) -> path   (see capture/CONTRACT_NOTES.md)

``FakeLoaded`` implements exactly that over a temporary capture directory laid out
like spec section 4.1 (``img/`` for images, ``derived/`` for everything else),
and counts reads and writes so tests can prove caching.

``a11y_pb_from_index`` and ``compose_pb_from_index`` build the raw a11y and Compose
semantics facets that a real capture of a hand-built Index would carry, so the
analyzers can be tested end to end on ``capture_builders`` indexes.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from typing import Any

import fakescenes as fs

from inspector_widget.capture.model import Index, parse_key
from inspector_widget.proto import view_inspection_pb2 as pb


class FakeLoaded:
    """In-memory raw facets and screenshots over an on-disk derived store."""

    def __init__(self, ix: Index | None = None, *, raw: Mapping[str, bytes] | None = None,
                 shots: Mapping[int, Any] | None = None, root: str | None = None,
                 meta: Any = None) -> None:
        self._ix = ix
        self.meta = meta if meta is not None else (ix.meta if ix is not None else None)
        self._raw = dict(raw or {})
        self._shots: dict[int, bytes] = {}
        for k, v in (shots or {}).items():
            self._shots[int(k)] = v.SerializeToString() if hasattr(v, "SerializeToString") else v
        cid = getattr(self.meta, "id", None) or "cfake0"
        self.path = os.path.join(root or tempfile.mkdtemp(prefix="iwcap-"), cid)
        os.makedirs(self.path, exist_ok=True)
        self.shot_calls: list[int] = []
        self.puts: list[str] = []
        self.gets: list[str] = []

    # ---- section 10 surface ------------------------------------------------ #
    def index(self) -> Index:
        return self._ix

    def raw(self, name: str) -> bytes | None:
        return self._raw.get(name)

    def shot(self, root: int) -> bytes | None:
        self.shot_calls.append(int(root))
        return self._shots.get(int(root))

    def derived_path(self, name: str) -> str:
        sub = "" if name.startswith("img/") else "derived"
        return os.path.join(self.path, sub, name)

    def derived(self, name: str) -> bytes | None:
        self.gets.append(name)
        p = self.derived_path(name)
        if not os.path.exists(p):
            return None
        with open(p, "rb") as f:
            return f.read()

    def put_derived(self, name: str, data: bytes) -> str:
        self.puts.append(name)
        p = self.derived_path(name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = f"{p}.tmp{os.getpid()}"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
        return p


# --------------------------------------------------------------------------- #
# Raw facets from a hand-built Index
# --------------------------------------------------------------------------- #
def _box(b: Any) -> dict[str, int]:
    x, y, w, h = (list(b) + [0, 0, 0, 0])[:4] if b else (0, 0, 0, 0)
    return {"layout": {"x": x, "y": y, "w": w, "h": h}}


_A11Y_FLAG = {"click": "clickable", "longclick": "long_clickable", "focus": "focusable",
              "checkable": "checkable", "checked": "checked", "scroll": "scrollable",
              "heading": "heading", "edit": "editable", "selected": "selected"}


def a11y_pb_from_index(ix: Index) -> pb.DumpA11yResponse:
    """The a11y tree a capture of ``ix`` would carry: every node with an ``a11y``
    facet (or of kind a11y) in ui-tree order, with ``(host, virtual)`` from
    ``ids.a11y``, visible to the user unless flagged hidden."""
    def a11y_dict(n) -> dict[str, Any]:
        fac = n.facets.get("a11y") or {}
        host, virt = (int(x) for x in str(n.ids["a11y"]).split(":"))
        flags = {_A11Y_FLAG[f] for f in (list(fac.get("flags") or []) + n.flags)
                 if f in _A11Y_FLAG}
        if "hidden" not in n.flags and "hidden" not in (fac.get("flags") or []):
            flags.add("visible_to_user")
        flags.add("enabled")
        d: dict[str, Any] = {"host_view_id": host, "virtual_id": virt, "bounds": _box(n.b),
                             "class_name": fac.get("class") or "android.view.View",
                             "flags": sorted(flags), "children": []}
        label = fac.get("speakable") or n.label
        if label:
            d["text"] = label
        return d

    def build(nid: str) -> list[dict[str, Any]]:
        n = ix.nodes[nid]
        kids: list[dict[str, Any]] = []
        for c in ix.tree("ui").children.get(nid, ()):
            kids.extend(build(c))
        if "a11y" in n.ids:
            d = a11y_dict(n)
            d["children"] = kids
            return [d]
        return kids

    windows = []
    for w in ix.windows():
        roots = build(w.id)
        if roots:
            windows.append({"root_view_id": int(w.ids["view"]), "root": roots[0]})
    return fs.a11y_to_pb({"windows": windows})


def compose_pb_from_index(ix: Index) -> pb.DumpComposeResponse:
    """The Compose semantics facet of ``ix``: one window per AndroidComposeView,
    rooted at a synthetic COMPOSABLE node that reuses the ACV's id (as the agent
    sends it), with each semantics node's ``compose.attrs`` and bounds."""
    windows = []
    for acv in ix.nodes.values():
        if acv.kind != "view" or (acv.facets.get("view") or {}).get("class") != \
                "AndroidComposeView":
            continue
        udid = int(acv.ids["view"])

        def sem(nid: str) -> list[dict[str, Any]]:
            n = ix.nodes[nid]
            kids: list[dict[str, Any]] = []
            for c in ix.tree("ui").children.get(nid, ()):
                kids.extend(sem(c))
            if n.kind == "compose":
                _kind, parts = parse_key(n.key)
                attrs = dict((n.facets.get("compose") or {}).get("attrs") or {})
                for a in (n.facets.get("compose") or {}).get("actions") or ():
                    attrs.setdefault(a, "AccessibilityAction(label=null, action=Function0)")
                return [{"id": parts[1], "name": n.label or "Node", "kind": "SEMANTICS",
                         "bounds": _box(n.b), "attrs": attrs, "children": kids}]
            return kids

        children = []
        for c in ix.tree("ui").children.get(acv.id, ()):
            children.extend(sem(c))
        windows.append({"view_id": udid, "root": {
            "id": udid, "name": "AndroidComposeView", "kind": "COMPOSABLE",
            "bounds": _box(acv.b), "children": children}})
    return fs.compose_to_pb({"windows": windows})


def screen_of(window: tuple[int, int, int, int], background: tuple[int, int, int],
              paint: list[tuple[tuple[int, int, int, int], tuple[int, int, int]]],
              scale: float = 1.0) -> pb.Screenshot:
    """One window's Screenshot, rendered like the harness Scene: ``paint`` rects
    (screen px) over ``background``, window-relative, at ``scale``."""
    x0, y0, w, h = window
    sw, sh = max(1, int(w * scale)), max(1, int(h * scale))
    bg = bytes(background) + b"\xff"
    rows = [bytearray(bg * sw) for _ in range(sh)]
    for (rx, ry, rw, rh), c in paint:
        px = bytes(c) + b"\xff"
        ax, ay = int((rx - x0) * scale), int((ry - y0) * scale)
        bx, by = int((rx + rw - x0) * scale), int((ry + rh - y0) * scale)
        ax, bx = max(0, ax), min(sw, bx)
        ay, by = max(0, ay), min(sh, by)
        for yy in range(ay, by):
            if bx > ax:
                rows[yy][ax * 4:bx * 4] = px * (bx - ax)
    return fs.rgba_to_screenshot(sw, sh, b"".join(bytes(r) for r in rows), scale)
