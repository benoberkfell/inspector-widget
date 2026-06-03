"""Render a Compose node tree as labeled boxes over a screenshot.

Input nodes are the *resolved* dict form produced by strings.compose_node_to_dict:
    {"id", "name", "bounds": {"layout": {"x","y","w","h"}, "render"?}, "attrs": {..},
     "source"?, "kind", "children": [...]}

`render_compose_overlay` draws a rectangle + label for each node that has a
non-empty label and real bounds, skewed toward leaf/labeled nodes so the image
stays legible. Requires Pillow.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont  # type: ignore
    _HAVE_PIL = True
except Exception:  # pragma: no cover
    _HAVE_PIL = False


# A palette cycled per depth so nesting is visible.
_PALETTE = [
    (66, 133, 244), (219, 68, 55), (15, 157, 88), (244, 180, 0),
    (171, 71, 188), (0, 172, 193), (255, 112, 67), (124, 179, 66),
]


def _label_for(node: Dict[str, Any]) -> str:
    name = node.get("name") or ""
    attrs = node.get("attrs") or {}
    # Prefer a concrete on-screen string for the caption.
    text = attrs.get("Text") or attrs.get("EditableText") or attrs.get("ContentDescription")
    role = attrs.get("Role")
    bits = []
    if name and name not in ("Node", "Layout", "Box"):
        bits.append(name)
    if role:
        bits.append(f"[{role}]")
    if text:
        t = text if len(text) <= 32 else text[:31] + "…"
        bits.append(f"“{t}”")
    return " ".join(bits) or name or "Node"


def _layout_rect(node: Dict[str, Any]) -> Optional[Tuple[int, int, int, int]]:
    b = (node.get("bounds") or {}).get("layout") or {}
    if not b:
        return None
    x, y, w, h = b.get("x", 0), b.get("y", 0), b.get("w", 0), b.get("h", 0)
    if w <= 0 or h <= 0:
        return None
    return int(x), int(y), int(w), int(h)


def flatten(nodes: List[Dict[str, Any]], labeled_only: bool = True) -> List[Dict[str, Any]]:
    """Depth-first flatten to drawable items {rect, label, depth}."""
    items: List[Dict[str, Any]] = []

    def walk(node: Dict[str, Any], depth: int) -> None:
        rect = _layout_rect(node)
        label = _label_for(node)
        attrs = node.get("attrs") or {}
        has_content = bool(
            attrs.get("Text") or attrs.get("ContentDescription")
            or attrs.get("EditableText") or attrs.get("Role")
        )
        if rect is not None and (not labeled_only or has_content or node.get("name")):
            items.append({"rect": rect, "label": label, "depth": depth,
                          "has_content": has_content})
        for ch in node.get("children", []) or []:
            walk(ch, depth + 1)

    for n in nodes:
        walk(n, 0)
    return items


def render_items(base_png: str, items: List[Dict[str, Any]], out_png: str,
                 font_size: int = 20, scale: float = 1.0) -> Dict[str, Any]:
    """Draw arbitrary labeled boxes. items: [{x,y,w,h,label,color_idx?}].

    ``scale`` maps full-resolution node bounds onto a base PNG that was
    captured at ``scale`` (<=1); every drawn coordinate is multiplied by it.
    """
    if not _HAVE_PIL:
        raise RuntimeError("Pillow is required (pip install Pillow).")
    img = Image.open(base_png).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", font_size)
    except Exception:
        font = ImageFont.load_default()
    # Larger boxes first so small labels land on top.
    items = sorted(items, key=lambda it: -it["w"] * it["h"])
    for i, it in enumerate(items):
        color = _PALETTE[it.get("color_idx", i) % len(_PALETTE)]
        x = int(it["x"] * scale)
        y = int(it["y"] * scale)
        w = int(it["w"] * scale)
        h = int(it["h"] * scale)
        draw.rectangle([x, y, x + w, y + h], outline=color + (255,), width=3)
        label = it.get("label")
        if label:
            tb = draw.textbbox((0, 0), label, font=font)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
            ly = max(0, y - th - 6)
            draw.rectangle([x, ly, x + tw + 10, ly + th + 6], fill=color + (235,))
            draw.text((x + 5, ly + 3), label, fill=(255, 255, 255, 255), font=font)
    out = Image.alpha_composite(img, overlay).convert("RGB")
    out.save(out_png)
    return {"path": out_png, "boxes": len(items), "size": list(img.size)}


def render_compose_overlay(
    base_png: str,
    nodes: List[Dict[str, Any]],
    out_png: str,
    labeled_only: bool = True,
    max_labels: int = 120,
    scale: float = 1.0,
) -> Dict[str, Any]:
    """Draw `nodes` over `base_png` -> `out_png`. Returns a small summary.

    ``scale`` maps full-resolution node bounds onto a base PNG captured at
    ``scale`` (<=1); every drawn coordinate is multiplied by it.
    """
    if not _HAVE_PIL:
        raise RuntimeError("Pillow is required for overlay rendering (pip install Pillow).")

    img = Image.open(base_png).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 22)
    except Exception:
        font = ImageFont.load_default()

    items = flatten(nodes, labeled_only=labeled_only)
    # Draw content-bearing nodes last (on top); cap labels for legibility.
    items.sort(key=lambda it: (it["has_content"], -it["rect"][2] * it["rect"][3]))
    drawn = 0
    for it in items:
        rx, ry, rw, rh = it["rect"]
        x = int(rx * scale)
        y = int(ry * scale)
        w = int(rw * scale)
        h = int(rh * scale)
        color = _PALETTE[it["depth"] % len(_PALETTE)]
        draw.rectangle([x, y, x + w, y + h], outline=color + (255,), width=3)
        if drawn < max_labels and it["has_content"]:
            label = it["label"]
            tb = draw.textbbox((0, 0), label, font=font)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
            ly = max(0, y - th - 6)
            draw.rectangle([x, ly, x + tw + 10, ly + th + 6], fill=color + (235,))
            draw.text((x + 5, ly + 3), label, fill=(255, 255, 255, 255), font=font)
            drawn += 1

    out = Image.alpha_composite(img, overlay).convert("RGB")
    out.save(out_png)
    return {"path": out_png, "boxes": len(items), "labels": drawn,
            "size": list(img.size)}




# --------------------------------------------------------------------------- #
# Accessibility overlay: box every a11y node with its speakable label and the
# host-computed TalkBack reading-order number; color-code by lint severity when
# findings are passed. Input a11y_dict is the resolved dict from a11y.a11y_to_dict
# ({"windows":[{"root":<node>}...], "focus_order":[...]}); each node carries
# {id, speakable/text/content_description, bounds:{layout:{x,y,w,h}}, children}.
# --------------------------------------------------------------------------- #
_SEVERITY_COLOR = {
    "error": (219, 68, 55),
    "warn": (244, 180, 0),
    "info": (66, 133, 244),
}
_A11Y_BOX_COLOR = (15, 157, 88)  # green for clean nodes
_SEVERITY_RANK = {"error": 3, "warn": 2, "info": 1}


def _a11y_collect_items(roots: List[Dict[str, Any]],
                        focus_order: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Flatten a11y nodes to drawable items, attaching reading-order numbers."""
    order_by_id: Dict[Any, int] = {}
    for e in focus_order or []:
        nid = e.get("id")
        if nid is not None and e.get("order") is not None:
            order_by_id[nid] = e["order"]
    items: List[Dict[str, Any]] = []

    def walk(n: Dict[str, Any]) -> None:
        b = (n.get("bounds") or {}).get("layout") or {}
        x, y, w, h = b.get("x", 0), b.get("y", 0), b.get("w", 0), b.get("h", 0)
        if w > 0 and h > 0:
            label = (n.get("speakable") or n.get("text")
                     or n.get("content_description") or n.get("role_description") or "")
            items.append({
                "id": n.get("id"), "x": int(x), "y": int(y), "w": int(w), "h": int(h),
                "label": label, "order": order_by_id.get(n.get("id")),
            })
        for c in n.get("children", []) or []:
            walk(c)

    for r in roots:
        walk(r)
    return items


def render_a11y_overlay(base_png: str, a11y_dict: Dict[str, Any], out_png: str,
                        findings: Optional[List[Dict[str, Any]]] = None,
                        max_labels: int = 160, scale: float = 1.0) -> Dict[str, Any]:
    """Draw the a11y tree over ``base_png`` -> ``out_png``.

    Each node gets a box + its speakable label and (when known) its TalkBack
    reading-order number. If ``findings`` (a list of dicts from
    a11y_lint.Finding.to_dict) is provided, boxes are recolored to the worst
    severity that targets that node id; otherwise nodes are drawn green.
    ``scale`` maps full-resolution node bounds onto a base PNG captured at
    ``scale`` (<=1); every drawn coordinate is multiplied by it.
    Returns a small summary.
    """
    if not _HAVE_PIL:
        raise RuntimeError("Pillow is required for overlay rendering (pip install Pillow).")

    roots = [w["root"] for w in (a11y_dict.get("windows") or []) if w.get("root")]
    items = _a11y_collect_items(roots, a11y_dict.get("focus_order"))

    # Map node id -> worst severity from findings.
    worst: Dict[Any, str] = {}
    for f in findings or []:
        nid = (f.get("node") or {}).get("id")
        sev = f.get("severity")
        if nid is None or sev is None:
            continue
        if _SEVERITY_RANK.get(sev, 0) > _SEVERITY_RANK.get(worst.get(nid, ""), 0):
            worst[nid] = sev

    img = Image.open(base_png).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 20)
        num_font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 18)
    except Exception:
        font = ImageFont.load_default()
        num_font = font

    # Largest boxes first so small labels land on top.
    items.sort(key=lambda it: -it["w"] * it["h"])
    drawn = 0
    flagged = 0
    for it in items:
        sev = worst.get(it["id"])
        color = _SEVERITY_COLOR[sev] if sev else _A11Y_BOX_COLOR
        if sev:
            flagged += 1
        x = int(it["x"] * scale)
        y = int(it["y"] * scale)
        w = int(it["w"] * scale)
        h = int(it["h"] * scale)
        draw.rectangle([x, y, x + w, y + h], outline=color + (255,), width=3)
        # Reading-order badge in the top-left corner of the node.
        if it.get("order") is not None:
            tag = str(it["order"])
            tb = draw.textbbox((0, 0), tag, font=num_font)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
            draw.rectangle([x, y, x + tw + 8, y + th + 6], fill=color + (235,))
            draw.text((x + 4, y + 2), tag, fill=(255, 255, 255, 255), font=num_font)
        # Speakable label above the box.
        if drawn < max_labels and it["label"]:
            label = it["label"]
            label = label if len(label) <= 40 else label[:39] + "…"
            tb = draw.textbbox((0, 0), label, font=font)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
            ly = max(0, y - th - 6)
            lx = min(x, max(0, img.size[0] - tw - 10))
            draw.rectangle([lx, ly, lx + tw + 10, ly + th + 6], fill=color + (235,))
            draw.text((lx + 5, ly + 3), label, fill=(255, 255, 255, 255), font=font)
            drawn += 1

    out = Image.alpha_composite(img, overlay).convert("RGB")
    out.save(out_png)
    return {"path": out_png, "boxes": len(items), "labels": drawn,
            "flagged": flagged, "size": list(img.size)}


# --------------------------------------------------------------------------- #
# Integrated overlay: draw the merged View/Compose/a11y tree produced by
# correlate.py. ``merged["roots"]`` is a list of IntegratedNode dicts; each has
# bounds.layout {x,y,w,h}, node_key, optional correlation_confidence, and
# view/compose/a11y facets. We flatten to drawable items and reuse render_items.
# --------------------------------------------------------------------------- #
def _integrated_label(node: Dict[str, Any]) -> str:
    key = node.get("node_key") or ""
    conf = node.get("correlation_confidence")
    if conf is not None:
        try:
            return f"{key} ({float(conf):.2f})" if key else f"({float(conf):.2f})"
        except (TypeError, ValueError):
            pass
    return str(key)


def _integrated_collect_items(roots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Depth-first flatten IntegratedNode dicts to render_items items."""
    items: List[Dict[str, Any]] = []

    def walk(node: Dict[str, Any]) -> None:
        # correlate's merged IntegratedNodes carry FLAT bounds {x,y,w,h}; tolerate
        # the nested {layout:{...}} shape too for safety.
        b = node.get("bounds") or {}
        if isinstance(b.get("layout"), dict):
            b = b["layout"]
        x, y, w, h = b.get("x", 0), b.get("y", 0), b.get("w", 0), b.get("h", 0)
        if w > 0 and h > 0:
            items.append({
                "x": int(x), "y": int(y), "w": int(w), "h": int(h),
                "label": _integrated_label(node),
            })
        for ch in node.get("children", []) or []:
            walk(ch)

    for r in roots:
        walk(r)
    return items


def render_integrated_overlay(base_png: str, merged: Dict[str, Any], out_png: str,
                              scale: float = 1.0) -> Dict[str, Any]:
    """Draw the correlate-merged tree over ``base_png`` -> ``out_png``.

    Flattens ``merged["roots"]`` (IntegratedNode dicts) by their flat ``bounds``,
    labels each from node_key (plus correlation_confidence when present), and
    draws via the shared box/scale logic in :func:`render_items`. ``scale``
    maps full-resolution node bounds onto a base PNG captured at ``scale``.
    Returns ``{"path","boxes","size"}``.
    """
    items = _integrated_collect_items(merged.get("roots") or [])
    return render_items(base_png, items, out_png, scale=scale)
