"""Integrated inspector: correlate the View tree, the Compose layer, and the
accessibility (AccessibilityNodeInfo) tree into one merged ``IntegratedNode`` tree, and
produce a full per-element "dossier" (all facets + a component image) for ``inspect_node``.

This implements integ.md PART 1 — INTEGRATED MODEL (§1.1 correlation keys, §1.2 unified
record, §1.3 ``inspect`` whole screen, §1.4 ``inspect_node`` one element).

Correlation keys (primary -> fallback), per integ.md §1.1:
  * View    : ``ViewNode.id`` = uniqueDrawingId               (exact)
  * Compose : ``ComposeNode.id`` = semanticsId; grafted under its host AndroidComposeView
  * A11Y    : ``host_view_id`` (== uniqueDrawingId of the backing View) and
              ``virtual_id`` (== Compose semanticsId for virtual nodes); else
              ``boundsInScreen`` IoU >= 0.6 (overlap) else none.

The merge walks the **View tree as the spine** (most stable ids + full nesting), grafts each
Compose subtree under its ``AndroidComposeView`` host node, and attaches a11y facets by key.

Inputs are the already-shaped JSON-ish dicts the host produces (``strings.dump_tree_to_dict`` /
``strings.dump_compose_to_dict`` and the a11y shaper). To stay decoupled from the exact a11y
shaper key names, :func:`_a11y_view_key` / :func:`_a11y_extract` accept both the proto field
names (``host_view_id``, ``virtual_id``, ``content_description``, boolean flags, ...) and the
simplified names from the design sketch (``view_id``, ``semantics_id``, ``flags`` list).
"""
from __future__ import annotations

import os
import tempfile
from typing import Any, Callable, Dict, List, Optional, Tuple

CONF_EXACT = "exact"
CONF_OVERLAP = "overlap"
CONF_NONE = "none"

# IoU threshold for the bounds-overlap fallback (integ.md §1.1 step 4).
_IOU_ACCEPT = 0.6
# Sentinel virtual id meaning "this a11y node is a real View, not a virtual/Compose node".
_HOST_VIEW_ID = -1


# --------------------------------------------------------------------------- geometry
def _rect(b: Optional[dict]) -> Optional[Dict[str, int]]:
    """Normalise a bounds dict to {x,y,w,h}. Accepts {layout:{...}} or a flat rect."""
    if not b:
        return None
    if "layout" in b and isinstance(b["layout"], dict):
        b = b["layout"]
    if not all(k in b for k in ("x", "y", "w", "h")):
        return None
    try:
        return {"x": int(b["x"]), "y": int(b["y"]), "w": int(b["w"]), "h": int(b["h"])}
    except (TypeError, ValueError):
        return None


def _iou(a: Optional[dict], b: Optional[dict]) -> float:
    if not a or not b:
        return 0.0
    ax2, ay2 = a["x"] + a["w"], a["y"] + a["h"]
    bx2, by2 = b["x"] + b["w"], b["y"] + b["h"]
    ix = max(0, min(ax2, bx2) - max(a["x"], b["x"]))
    iy = max(0, min(ay2, by2) - max(a["y"], b["y"]))
    inter = ix * iy
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union > 0 else 0.0


def _contains_point(outer: Optional[dict], px: int, py: int) -> bool:
    if not outer:
        return False
    return (outer["x"] <= px <= outer["x"] + outer["w"]
            and outer["y"] <= py <= outer["y"] + outer["h"])


def _area(r: Optional[dict]) -> int:
    return (r["w"] * r["h"]) if r else 0


# --------------------------------------------------------------------------- a11y keys
def _a11y_view_key(n: dict) -> Optional[int]:
    """uniqueDrawingId of the backing View for a real (non-virtual) a11y node, else None."""
    vid = n.get("host_view_id", n.get("view_id"))
    virtual = n.get("virtual_id")
    is_virtual = n.get("is_virtual")
    # A real-view a11y node: virtual_id is the HOST sentinel (-1) or is_virtual is false.
    if is_virtual is True:
        return None
    if virtual is not None and virtual != _HOST_VIEW_ID:
        # Has a meaningful virtual id => it's a virtual/Compose node, key by semantics.
        return None
    return int(vid) if vid else None


def _a11y_sem_key(n: dict) -> Optional[int]:
    """Compose semantics id for a virtual a11y node, else None."""
    sem = n.get("semantics_id")
    if sem:
        return int(sem)
    virtual = n.get("virtual_id")
    is_virtual = n.get("is_virtual")
    if (is_virtual is True or n.get("provider_class")) and virtual not in (None, _HOST_VIEW_ID):
        return int(virtual)
    if virtual not in (None, _HOST_VIEW_ID, 0):
        return int(virtual)
    return None


def _flat(trees: List[dict]) -> List[dict]:
    out: List[dict] = []

    def walk(n: Optional[dict]) -> None:
        if not n:
            return
        out.append(n)
        for c in n.get("children", []) or []:
            walk(c)

    for t in trees or []:
        walk(t)
    return out


# --------------------------------------------------------------------------- a11y facet
# Boolean attributes worth surfacing on the a11y facet (proto field name -> output key).
_A11Y_BOOL_FIELDS = (
    "clickable", "long_clickable", "checkable", "checked", "focusable", "focused",
    "accessibility_focused", "selected", "enabled", "scrollable", "visible_to_user",
    "heading", "screen_reader_focusable", "editable", "password", "dismissable",
    "content_invalid", "multi_line",
)
_A11Y_TEXT_FIELDS = (
    "text", "content_description", "hint_text", "state_description", "error",
    "tooltip_text", "pane_title", "role_description",
)


def _a11y_facet(n: dict) -> dict:
    """Project a shaped a11y node dict into the compact facet stored on IntegratedNode."""
    facet: Dict[str, Any] = {}
    # Stable ids.
    for src, dst in (("host_view_id", "host_view_id"), ("virtual_id", "virtual_id"),
                     ("view_id", "view_id"), ("semantics_id", "semantics_id"),
                     ("source_node_id", "source_node_id"), ("class_name", "class_name")):
        if n.get(src) is not None:
            facet[dst] = n[src]
    # Speakable composition (TalkBack-ish): contentDescription > text > stateDescription.
    facet["speakable"] = (
        n.get("content_description") or n.get("text") or n.get("state_description")
    )
    for f in _A11Y_TEXT_FIELDS:
        if n.get(f):
            facet[f] = n[f]
    role = n.get("role_description") or n.get("role")
    if role:
        facet["role"] = role
    # Flags: support both a precomputed list and individual booleans.
    flags = list(n.get("flags") or [])
    for f in _A11Y_BOOL_FIELDS:
        if n.get(f):
            label = f.upper()
            if label not in flags:
                flags.append(label)
        if n.get(f) is not None:
            facet[f] = bool(n[f])
    ifa = n.get("important_for_accessibility", n.get("important_for_a11y"))
    if ifa is not None:
        facet["important_for_accessibility"] = ifa
        # Normalise to a boolean "important" too (0 AUTO, 1 YES, 2 NO, 4 NO_HIDE_DESC).
        if isinstance(ifa, int):
            facet["important"] = ifa in (0, 1)
            if facet["important"] and "IMPORTANT" not in flags:
                flags.append("IMPORTANT")
        elif isinstance(ifa, str):
            facet["important"] = ifa.upper() in ("AUTO", "YES", "IMPORTANT")
    if flags:
        facet["flags"] = flags
    # Actions, traversal/labelling, collection/range.
    if n.get("actions"):
        facet["actions"] = n["actions"]
    for f in ("traversal_before", "traversal_after", "label_for", "labeled_by",
              "drawing_order", "reading_order", "collection_info", "collection_item_info",
              "range_info"):
        if n.get(f) is not None:
            facet[f] = n[f]
    return facet


# --------------------------------------------------------------------------- merge core
class _Merger:
    def __init__(self, view_tree: List[dict], compose_windows: List[dict],
                 a11y_tree: List[dict], props: Optional[Dict[int, list]] = None):
        self.props = props or {}
        flat = _flat(a11y_tree)
        self.a11y_flat = flat
        self.a11y_by_view: Dict[int, dict] = {}
        self.a11y_by_sem: Dict[int, dict] = {}
        for n in flat:
            vk = _a11y_view_key(n)
            if vk is not None and vk not in self.a11y_by_view:
                self.a11y_by_view[vk] = n
            sk = _a11y_sem_key(n)
            if sk is not None and sk not in self.a11y_by_sem:
                self.a11y_by_sem[sk] = n
        # host AndroidComposeView id -> compose root subtree
        self.compose_by_host: Dict[int, dict] = {
            int(w["view_id"]): w["root"]
            for w in (compose_windows or [])
            if w.get("view_id") and w.get("root")
        }
        self.view_tree = view_tree or []

    # ---- a11y attachment ----
    def _attach_a11y(self, node: dict, exact: Optional[dict]) -> None:
        if exact:
            node["a11y"] = _a11y_facet(exact)
            node["correlation_confidence"] = CONF_EXACT
            return
        nb = node.get("bounds")
        if nb:
            best, best_iou = None, 0.0
            for cand in self.a11y_flat:
                cb = _rect(cand.get("bounds"))
                score = _iou(nb, cb)
                if score > best_iou:
                    best, best_iou = cand, score
            if best is not None and best_iou >= _IOU_ACCEPT:
                node["a11y"] = _a11y_facet(best)
                node["correlation_confidence"] = CONF_OVERLAP
                node["a11y_iou"] = round(best_iou, 3)
                return
        node["correlation_confidence"] = CONF_NONE

    # ---- view spine ----
    def merge_view(self, v: dict) -> dict:
        vid = int(v.get("id", 0))
        bounds = _rect(v.get("bounds"))
        node: Dict[str, Any] = {
            "node_key": f"view:{vid}",
            "bounds": bounds,
            "view": {k: v.get(k) for k in ("id", "class_name", "qualified_name",
                                           "resource", "view_id_name", "text")
                     if v.get(k) is not None},
            "children": [],
        }
        if v.get("bounds", {}).get("render"):
            node["render_quad"] = v["bounds"]["render"]
        if vid in self.props:
            node["view"]["properties"] = self.props[vid]
        # image-ref by uniqueDrawingId (SKP draw-node) — populated lazily by callers.
        node["image_ref"] = {"source": "view", "view_id": vid}
        self._attach_a11y(node, self.a11y_by_view.get(vid))
        # graft compose under its host AndroidComposeView
        croot = self.compose_by_host.get(vid)
        if croot:
            node["children"].append(self.merge_compose(croot))
        for c in v.get("children", []) or []:
            node["children"].append(self.merge_view(c))
        return node

    # ---- compose subtree ----
    def merge_compose(self, c: dict) -> dict:
        cid = int(c.get("id", 0))
        bounds = _rect(c.get("bounds"))
        node: Dict[str, Any] = {
            "node_key": f"compose:{cid}",
            "bounds": bounds,
            "compose": {
                "id": cid,
                "name": c.get("name"),
                "source": c.get("source"),
                "attrs": c.get("attrs", {}) or {},
                "render_node_id": c.get("render_node_id"),
                "kind": c.get("kind"),
            },
            "children": [],
        }
        if c.get("bounds", {}).get("render"):
            node["render_quad"] = c["bounds"]["render"]
        rnid = c.get("render_node_id")
        if rnid:
            node["image_ref"] = {"source": "skp", "layer_id": int(rnid)}
        self._attach_a11y(node, self.a11y_by_sem.get(cid))
        for ch in c.get("children", []) or []:
            node["children"].append(self.merge_compose(ch))
        return node

    def build(self) -> List[dict]:
        return [self.merge_view(r) for r in self.view_tree]


def build_integrated_tree(view_tree: List[dict], compose: List[dict], a11y: List[dict],
                          props: Optional[Dict[int, list]] = None) -> Dict[str, Any]:
    """Merge the three trees into one IntegratedNode tree rooted at each window.

    ``view_tree``  : list of ViewNode dicts (``strings.dump_tree_to_dict``["roots"]).
    ``compose``    : list of compose-window dicts ({view_id, root}) from dump_compose_to_dict.
    ``a11y``       : list of a11y window-root dicts (the shaped a11y tree roots).
    ``props``      : optional {view_id: [property dicts]} to inline under ``view.properties``.
    """
    merger = _Merger(view_tree, compose, a11y, props)
    roots = merger.build()
    return {"roots": roots, "summary": summarize(roots)}


# --------------------------------------------------------------------------- summary
def summarize(roots: List[dict]) -> Dict[str, Any]:
    counts = {"nodes": 0, "view": 0, "compose": 0, "a11y": 0,
              "exact": 0, "overlap": 0, "none": 0, "with_image_ref": 0}

    def walk(n: dict) -> None:
        counts["nodes"] += 1
        if n.get("view"):
            counts["view"] += 1
        if n.get("compose"):
            counts["compose"] += 1
        if n.get("a11y"):
            counts["a11y"] += 1
        conf = n.get("correlation_confidence")
        if conf in counts:
            counts[conf] += 1
        if n.get("image_ref"):
            counts["with_image_ref"] += 1
        for c in n.get("children", []) or []:
            walk(c)

    for r in roots:
        walk(r)
    return counts


# --------------------------------------------------------------------------- hit-test
def find_node(merged: Dict[str, Any], *, node_key: Optional[str] = None,
              view_id: Optional[int] = None, semantics_id: Optional[int] = None,
              bounds: Optional[dict] = None) -> Optional[dict]:
    """Resolve one IntegratedNode by key/id, or hit-test the deepest node covering ``bounds``.

    For bounds, the deepest (most-nested) node whose bounds contain the box's centre wins;
    ties broken by smallest area (integ.md §1.4: "smallest IntegratedNode ... deepest match").
    """
    target_key = node_key
    if target_key is None and view_id is not None:
        target_key = f"view:{int(view_id)}"
    sem_key = f"compose:{int(semantics_id)}" if semantics_id is not None else None

    if target_key or sem_key:
        found = [None]

        def walk_key(n: dict) -> None:
            if found[0] is not None:
                return
            if n.get("node_key") == target_key or n.get("node_key") == sem_key:
                found[0] = n
                return
            # also match a11y facet semantics/view ids
            if semantics_id is not None:
                a = n.get("a11y") or {}
                if a.get("semantics_id") == semantics_id or a.get("virtual_id") == semantics_id:
                    found[0] = n
                    return
            for c in n.get("children", []) or []:
                walk_key(c)

        for r in merged.get("roots", []):
            walk_key(r)
        if found[0] is not None or bounds is None:
            return found[0]

    if bounds is None:
        return None
    rect = _rect(bounds)
    if rect is None:
        return None
    cx = rect["x"] + rect["w"] // 2
    cy = rect["y"] + rect["h"] // 2
    best: List[Optional[Tuple[dict, int, int]]] = [None]  # (node, depth, area)

    def walk_hit(n: dict, depth: int) -> None:
        if _contains_point(n.get("bounds"), cx, cy):
            area = _area(n.get("bounds"))
            cur = best[0]
            if (cur is None or depth > cur[1]
                    or (depth == cur[1] and area < cur[2])):
                best[0] = (n, depth, area)
        for c in n.get("children", []) or []:
            walk_hit(c, depth + 1)

    for r in merged.get("roots", []):
        walk_hit(r, 0)
    return best[0][0] if best[0] else None


# --------------------------------------------------------------------------- session glue
def _shaped_view_tree(session: Any, props: bool) -> Tuple[List[dict], Dict[int, list]]:
    from . import strings as st
    resp = session.dump_tree(root_id=0, include_properties=props,
                             include_resolution_stack=False,
                             include_screenshot=False)
    data = st.dump_tree_to_dict(resp)
    prop_map: Dict[int, list] = {}
    for vid, plist in (data.get("properties") or {}).items():
        try:
            prop_map[int(vid)] = plist
        except (TypeError, ValueError):
            pass
    return data.get("roots", []), prop_map


def _shaped_compose(session: Any) -> List[dict]:
    from . import strings as st
    from .client import TransportError
    try:
        resp = session.dump_compose(include_semantics=True, include_slot_table=False,
                                    enable_inspection=False)
        data = st.dump_compose_to_dict(resp)
        return data.get("windows", []) or []
    except TransportError:
        raise  # a lost session is not "no Compose on screen"
    except Exception:
        return []


def _shaped_a11y(session: Any) -> List[dict]:
    """Fetch + shape the a11y tree as a list of window-root dicts. Degrades to [] if absent."""
    from .client import TransportError
    fn = getattr(session, "dump_a11y", None)
    if not callable(fn):
        return []
    try:
        data = fn()
    except TransportError:
        raise  # a lost session is not "no accessibility tree"
    except Exception:
        return []
    # Session.dump_a11y() returns the raw DumpA11yResponse proto; shape it to the
    # resolved dict the correlator expects (lists/dicts pass straight through).
    if not isinstance(data, (list, dict)):
        try:
            from inspector_widget import a11y as _a11ymod
            data = _a11ymod.a11y_to_dict(data)
        except Exception:
            return []
    return _a11y_roots(data)


def _a11y_roots(data: Any) -> List[dict]:
    """Normalise whatever the a11y shaper returns into a list of window-root node dicts."""
    if data is None:
        return []
    if isinstance(data, list):
        return [d for d in data if d]
    if isinstance(data, dict):
        if "windows" in data:
            roots = []
            for w in data.get("windows") or []:
                root = w.get("root") if isinstance(w, dict) else None
                if root:
                    roots.append(root)
            return roots
        if "roots" in data:
            return [r for r in (data.get("roots") or []) if r]
        if "root" in data and data["root"]:
            return [data["root"]]
    return []


def inspect_tree(session: Any, include_properties: bool = False) -> Dict[str, Any]:
    """Whole-screen integrated tree (integ.md §1.3). Fetches view+compose+a11y, merges, returns.

    Returns ``{roots, summary, sources}`` where ``sources`` records which facets were available.
    """
    view_roots, prop_map = _shaped_view_tree(session, include_properties)
    compose_windows = _shaped_compose(session)
    a11y_roots = _shaped_a11y(session)
    merged = build_integrated_tree(view_roots, compose_windows, a11y_roots,
                                   props=prop_map if include_properties else None)
    merged["sources"] = {
        "view": bool(view_roots),
        "compose": bool(compose_windows),
        "a11y": bool(a11y_roots),
    }
    return merged


# --------------------------------------------------------------------------- component image
def _capture_skp(session: Any) -> Optional[Tuple[bytes, int]]:
    fn = getattr(session, "capture_skp", None)
    if not callable(fn):
        client = getattr(session, "client", None)
        fn = getattr(client, "capture_skp", None) if client else None
    if not callable(fn):
        return None
    from .client import TransportError
    try:
        resp = fn(root_id=0)
    except TransportError:
        raise
    except Exception:
        return None
    if not getattr(resp, "supported", False):
        return None
    skp = bytes(getattr(resp, "skp", b"") or b"")
    if not skp:
        return None
    return skp, int(getattr(resp, "version", 0) or 0)


def _full_screenshot_png(session: Any, dest: str) -> Optional[Tuple[str, float]]:
    """Capture a full screenshot, write to ``dest`` PNG. Returns (path, scale) or None."""
    from . import png as pngmod
    from .client import TransportError
    try:
        resp = session.screenshot(root_id=0, scale=1.0)
    except TransportError:
        raise
    except Exception:
        return None
    if not resp.HasField("screenshot"):
        return None
    pngmod.write_png(resp.screenshot, dest)
    scale = float(resp.screenshot.scale) if resp.screenshot.scale else 1.0
    return dest, scale


def _crop_png(src_png: str, rect: dict, scale: float, dest: str) -> Optional[str]:
    """Crop ``rect`` (absolute screen px) from a full-screen PNG, dividing by ``scale``."""
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        return None
    try:
        img = Image.open(src_png).convert("RGBA")
    except Exception:
        return None
    s = scale or 1.0
    x = int(rect["x"] * s)
    y = int(rect["y"] * s)
    w = max(1, int(rect["w"] * s))
    h = max(1, int(rect["h"] * s))
    x2 = min(img.width, x + w)
    y2 = min(img.height, y + h)
    x = max(0, min(x, img.width - 1))
    y = max(0, min(y, img.height - 1))
    if x2 <= x or y2 <= y:
        return None
    img.crop((x, y, x2, y2)).save(dest)
    return dest


def component_image(session: Any, node: dict, out_path: Optional[str] = None,
                    scale: float = 1.0) -> Dict[str, Any]:
    """Cut a per-component image for one IntegratedNode (integ.md §1.4 / §1.5 / tool #15).

    Strategy: if the node has a Compose ``render_node_id`` (graphicsLayer layerId), capture an
    SKP and cut that layer via :mod:`skia_client`. Otherwise (or on any SKP failure) BITMAP-crop
    the node's bounds from a full screenshot. Returns
    ``{path, source: "skp"|"bitmap_crop", layer_id?, scale, note?}`` (``path`` None on failure).
    """
    out_path = out_path or _tmp_png("component")
    rect = _rect(node.get("bounds"))
    image_ref = node.get("image_ref") or {}
    layer_id = image_ref.get("layer_id")
    if layer_id is None:
        compose = node.get("compose") or {}
        layer_id = compose.get("render_node_id")
    note: Optional[str] = None

    # ---- Path A: SKP per-layer cut (Compose graphicsLayer) ----
    if layer_id and rect:
        captured = _capture_skp(session)
        if captured is not None:
            skp, version = captured
            try:
                from . import skia_client
                images = skia_client.per_component_images(
                    skp,
                    [(int(layer_id), rect["x"], rect["y"], rect["w"], rect["h"])],
                    version=version, scale=scale,
                )
                img = images.get(int(layer_id))
                if img is not None:
                    img.save_png(out_path)
                    return {"path": out_path, "source": "skp",
                            "layer_id": int(layer_id), "scale": scale}
                note = "skiaparser returned no image for that layer; fell back to crop"
            except Exception as exc:  # SkiaClientError or anything unexpected
                note = f"SKP path unavailable ({exc}); fell back to BITMAP crop"
        else:
            note = "SKP capture unsupported on this device; using BITMAP crop"

    # ---- Path B: BITMAP crop from a full screenshot ----
    if not rect:
        return {"path": None, "source": "bitmap_crop", "scale": scale,
                "error": "node has no bounds to crop"}
    base = _tmp_png("component_base")
    try:
        shot = _full_screenshot_png(session, base)
    except BaseException:
        _safe_remove(base)
        raise
    if shot is None:
        _safe_remove(base)
        return {"path": None, "source": "bitmap_crop", "scale": scale,
                "error": "screenshot capture failed", "note": note}
    base_path, shot_scale = shot
    cropped = _crop_png(base_path, rect, shot_scale, out_path)
    _safe_remove(base_path)
    if cropped is None:
        return {"path": None, "source": "bitmap_crop", "scale": shot_scale,
                "error": "crop failed (Pillow required for bitmap crop)", "note": note}
    result = {"path": cropped, "source": "bitmap_crop", "scale": shot_scale}
    if note:
        result["note"] = note
    return result


# --------------------------------------------------------------------------- inspect_node
def inspect_node(session: Any, *, node_key: Optional[str] = None,
                 view_id: Optional[int] = None, semantics_id: Optional[int] = None,
                 bounds: Optional[dict] = None, include_image: bool = True,
                 image_path: Optional[str] = None,
                 lint_fn: Optional[Callable[[List[dict], int], List[dict]]] = None,  # (compose_roots, density_dpi) -> List[dict]
                 density: float = 0.0) -> Optional[Dict[str, Any]]:
    """Full dossier for ONE element (integ.md §1.4).

    Builds the merged tree (with view properties), resolves the target node by
    key/view_id/semantics_id/bounds, fully populates its facets (``view.properties`` via
    ``get_properties`` if missing, full a11y, full compose attrs), cuts its component image,
    and attaches focused lint findings (if ``lint_fn`` provided).
    """
    # Build with properties so view.properties is available for the target.
    view_roots, prop_map = _shaped_view_tree(session, props=True)
    compose_windows = _shaped_compose(session)
    a11y_roots = _shaped_a11y(session)
    merged = build_integrated_tree(view_roots, compose_windows, a11y_roots, props=prop_map)

    node = find_node(merged, node_key=node_key, view_id=view_id,
                     semantics_id=semantics_id, bounds=bounds)
    if node is None:
        return None

    # Ensure view.properties present (fetch on demand if not inlined).
    view = node.get("view") or {}
    vid = view.get("id")
    if vid and not view.get("properties"):
        props = _fetch_properties(session, int(vid))
        if props is not None:
            view["properties"] = props
            node["view"] = view

    dossier: Dict[str, Any] = {
        "node_key": node.get("node_key"),
        "bounds": node.get("bounds"),
        "correlation_confidence": node.get("correlation_confidence"),
    }
    for facet in ("view", "compose", "a11y", "render_quad", "a11y_iou"):
        if node.get(facet) is not None:
            dossier[facet] = node[facet]

    # Component image.
    if include_image:
        img_dest = image_path or _tmp_png("dossier")
        dossier["component_image"] = component_image(session, node, out_path=img_dest)

    # Focused lint for this node (run lint over the compose-semantics tree, keep this node's findings).
    compose_roots = [w.get("root") for w in compose_windows if w.get("root")]
    if lint_fn is not None and compose_roots:
        try:
            all_findings = lint_fn(compose_roots, density) or []
        except Exception:
            all_findings = []
        keys = _node_a11y_keys(node)
        focused = [f for f in all_findings if _finding_matches(f, keys)]
        dossier["lint"] = focused

    return dossier


def _node_a11y_keys(node: dict) -> set:
    a = node.get("a11y") or {}
    keys = set()
    for k in ("source_node_id", "host_view_id", "view_id", "semantics_id", "virtual_id"):
        if a.get(k) is not None:
            keys.add(a[k])
    view = node.get("view") or {}
    if view.get("id") is not None:
        keys.add(view["id"])
    compose = node.get("compose") or {}
    if compose.get("id") is not None:
        keys.add(compose["id"])
    return keys


def _finding_matches(finding: dict, keys: set) -> bool:
    if not isinstance(finding, dict):
        return False
    # a11y_lint findings carry node as a dict {id, name, role, source}; match on its id.
    node = finding.get("node")
    if isinstance(node, dict) and node.get("id") in keys:
        return True
    # Tolerate scalar selectors too; skip any unhashable (dict/list) value so a
    # `v in keys` set membership test can never raise TypeError.
    for k in ("node", "node_id", "source_node_id", "view_id", "semantics_id"):
        v = finding.get(k)
        if v is None or isinstance(v, (dict, list)):
            continue
        if v in keys:
            return True
    for v in finding.get("nodes", []) or []:
        if not isinstance(v, (dict, list)) and v in keys:
            return True
    return False


def _fetch_properties(session: Any, view_id: int) -> Optional[list]:
    from . import strings as st
    from .client import TransportError
    try:
        resp = session.get_properties(view_id=view_id, include_resolution_stack=False)
    except TransportError:
        raise
    except Exception:
        return None
    try:
        data = st.get_properties_to_dict(resp)
        return data.get("properties")
    except Exception:
        return None


# --------------------------------------------------------------------------- misc
def _tmp_png(tag: str) -> str:
    fd, path = tempfile.mkstemp(prefix=f"viewspector_{tag}_", suffix=".png")
    os.close(fd)
    return path


def _safe_remove(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def flatten(merged: Dict[str, Any]) -> List[dict]:
    """Flatten the merged tree to a list of IntegratedNodes (for overlays / iteration)."""
    return _flat(merged.get("roots", []))
