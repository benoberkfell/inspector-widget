"""Render node trees as labeled boxes over a screenshot.

Three renderers share one drawing core (:class:`_Canvas`):

* :func:`render_compose_overlay` — Compose nodes (``strings.compose_node_to_dict``).
* :func:`render_a11y_overlay` — the a11y tree (``a11y.a11y_to_dict``) with TalkBack
  reading-order badges, colour-coded by lint severity.
* :func:`render_integrated_overlay` — the correlate-merged tree.

Node bounds are full-resolution screen px; the base PNG may be captured at ``scale < 1``,
so every coordinate and the font size are multiplied by ``scale``. Labels are placed to
avoid colliding with labels already drawn (nested nodes otherwise print on top of each
other), and label text is black or white — whichever reaches >= 4.5:1 on its badge.
Requires Pillow.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont  # type: ignore
    _HAVE_PIL = True
except Exception:  # pragma: no cover
    _HAVE_PIL = False

from .a11y import HOST_VIEW_ID
from .correlate import finding_key

Color = Tuple[int, int, int]

# A palette cycled per depth so nesting is visible.
_PALETTE: List[Color] = [
    (66, 133, 244), (219, 68, 55), (15, 157, 88), (244, 180, 0),
    (171, 71, 188), (0, 172, 193), (255, 112, 67), (124, 179, 66),
]

# Base font sizes at capture scale 1.0 (full-resolution screenshots).
_BASE_LABEL_PX = 20
_BASE_BADGE_PX = 18
_MIN_FONT_PX = 9
_MIN_TEXT_CONTRAST = 4.5

# Font files tried in order (Pillow also searches its font path for bare names).
_FONT_CANDIDATES = (
    "DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:\\Windows\\Fonts\\arial.ttf",
    "Arial.ttf",
    "arial.ttf",
)
_BOLD_FONT_CANDIDATES = (
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
    "Arial Bold.ttf",
    "arialbd.ttf",
)
_FONT_CACHE: Dict[Tuple[int, bool], Any] = {}


def load_font(size: int, bold: bool = False) -> Any:
    """A TrueType font of ``size`` px, falling back to Pillow's scalable default.

    Tries DejaVu/Liberation (Linux), Arial/Helvetica (macOS) and Arial (Windows); if none
    is installed, uses ``ImageFont.load_default(size=...)`` (Pillow >= 10.1), and only on
    older Pillow the fixed-size bitmap default.
    """
    size = max(_MIN_FONT_PX, int(size))
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    font = None
    for path in (_BOLD_FONT_CANDIDATES if bold else ()) + _FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(path, size)
            break
        except Exception:
            continue
    if font is None:
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:  # Pillow < 10.1
            font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def font_px(base: int, scale: float) -> int:
    """Font size for a capture ``scale`` (labels shrink with the screenshot)."""
    try:
        s = float(scale) if scale else 1.0
    except (TypeError, ValueError):
        s = 1.0
    return max(_MIN_FONT_PX, int(round(base * s)))


# --------------------------------------------------------------------------- colour
def _rel_lum(c: Color) -> float:
    def lin(v: float) -> float:
        v = v / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = c[:3]
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast_ratio(a: Color, b: Color) -> float:
    la, lb = _rel_lum(a), _rel_lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


_BLACK: Color = (0, 0, 0)
_WHITE: Color = (255, 255, 255)


def legible_pair(bg: Color) -> Tuple[Color, Color]:
    """(badge colour, text colour) with text contrast >= 4.5:1.

    Picks black or white text, whichever contrasts more; if neither reaches 4.5:1 the
    badge is darkened (white text) until it does.
    """
    bg = tuple(int(v) for v in bg[:3])  # type: ignore[assignment]
    best = max((_BLACK, _WHITE), key=lambda t: contrast_ratio(bg, t))
    if contrast_ratio(bg, best) >= _MIN_TEXT_CONTRAST:
        return bg, best
    cur = bg
    for _ in range(20):
        cur = tuple(int(v * 0.85) for v in cur)  # type: ignore[assignment]
        if contrast_ratio(cur, _WHITE) >= _MIN_TEXT_CONTRAST:
            return cur, _WHITE
    return _BLACK, _WHITE


# --------------------------------------------------------------------------- drawing core
Box = Tuple[int, int, int, int]  # x, y, w, h in base-image px


class _Canvas:
    """A transparent overlay over a base PNG with collision-aware label placement."""

    def __init__(self, base_png: str, scale: float):
        if not _HAVE_PIL:
            raise RuntimeError("Pillow is required for overlay rendering (pip install Pillow).")
        self.img = Image.open(base_png).convert("RGBA")
        self.overlay = Image.new("RGBA", self.img.size, (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.overlay)
        self.scale = float(scale) if scale else 1.0
        self.W, self.H = self.img.size
        self.placed: List[Tuple[int, int, int, int]] = []  # label rects x0,y0,x1,y1
        self.labels_skipped = 0
        self.line_w = 3 if self.scale >= 0.66 else 2

    def rect(self, x: int, y: int, w: int, h: int) -> Box:
        s = self.scale
        return int(x * s), int(y * s), int(w * s), int(h * s)

    def box(self, r: Box, color: Color, dashed: bool = False) -> None:
        x, y, w, h = r
        fill = color + (255,)
        if not dashed:
            self.draw.rectangle([x, y, x + w, y + h], outline=fill, width=self.line_w)
            return
        dash = max(4, self.line_w * 3)
        for x0 in range(x, x + w, dash * 2):
            x1 = min(x0 + dash, x + w)
            self.draw.line([x0, y, x1, y], fill=fill, width=self.line_w)
            self.draw.line([x0, y + h, x1, y + h], fill=fill, width=self.line_w)
        for y0 in range(y, y + h, dash * 2):
            y1 = min(y0 + dash, y + h)
            self.draw.line([x, y0, x, y1], fill=fill, width=self.line_w)
            self.draw.line([x + w, y0, x + w, y1], fill=fill, width=self.line_w)

    def _free(self, r: Tuple[int, int, int, int]) -> bool:
        for p in self.placed:
            if not (r[2] <= p[0] or p[2] <= r[0] or r[3] <= p[1] or p[3] <= r[1]):
                return False
        return True

    def _clamp(self, x: int, y: int, bw: int, bh: int) -> Tuple[int, int]:
        return max(0, min(x, self.W - bw)), max(0, min(y, self.H - bh))

    def label(self, text: str, anchor: Box, color: Color, font: Any, pad: int = 3,
              inside_first: bool = False, force: bool = False) -> bool:
        """Draw ``text`` on a badge near ``anchor`` at the first spot that does not collide
        with an earlier label (above, inside, below, right-aligned, then stepping down
        inside the box). Returns False (and counts it) when no spot was free, unless
        ``force`` draws it at the first spot anyway."""
        if not text:
            return False
        tb = self.draw.textbbox((0, 0), text, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        bw, bh = tw + 2 * pad + 2, th + 2 * pad
        x, y, w, h = anchor
        above = (x, y - bh - 1)
        inside = (x + 1, y + 1)
        spots = [inside, above] if inside_first else [above, inside]
        spots += [(x, y + h + 1), (x + w - bw, y - bh - 1), (x + w - bw, y + 1),
                  (x + 1, y + h - bh - 1)]
        spots += [(x + 1, y + 1 + k * (bh + 1)) for k in range(1, 8)]
        chosen = None
        for sx, sy in spots:
            cx, cy = self._clamp(int(sx), int(sy), bw, bh)
            r = (cx, cy, cx + bw, cy + bh)
            if self._free(r):
                chosen = r
                break
        if chosen is None:
            if not force:
                self.labels_skipped += 1
                return False
            cx, cy = self._clamp(int(spots[0][0]), int(spots[0][1]), bw, bh)
            chosen = (cx, cy, cx + bw, cy + bh)
        badge, fg = legible_pair(color)
        self.draw.rectangle(list(chosen), fill=badge + (255,))
        self.draw.text((chosen[0] + pad + 1 - tb[0], chosen[1] + pad - tb[1]), text,
                       fill=fg + (255,), font=font)
        self.placed.append(chosen)
        return True

    def save(self, out_png: str) -> None:
        Image.alpha_composite(self.img, self.overlay).convert("RGB").save(out_png)


def _clip(label: str, n: int = 40) -> str:
    return label if len(label) <= n else label[: n - 3] + "..."


# --------------------------------------------------------------------------- compose overlay
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
        bits.append('"' + _clip(str(text), 32) + '"')
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
                 font_size: int = _BASE_LABEL_PX, scale: float = 1.0) -> Dict[str, Any]:
    """Draw arbitrary labeled boxes. items: [{x,y,w,h,label,color_idx?|color?,dashed?}].

    ``scale`` maps full-resolution node bounds onto a base PNG that was
    captured at ``scale`` (<=1); every drawn coordinate and the font size are
    multiplied by it.
    """
    cv = _Canvas(base_png, scale)
    font = load_font(font_px(font_size, scale))
    # Boxes largest first; labels of the smaller (more specific) boxes placed first.
    ordered = sorted(enumerate(items), key=lambda t: -t[1]["w"] * t[1]["h"])
    colors: Dict[int, Color] = {}
    for i, it in ordered:
        if it.get("color"):
            colors[i] = tuple(it["color"])[:3]  # type: ignore[assignment]
        else:
            colors[i] = _PALETTE[it.get("color_idx", i) % len(_PALETTE)]
        cv.box(cv.rect(it["x"], it["y"], it["w"], it["h"]), colors[i],
               dashed=bool(it.get("dashed")))
    labels = 0
    for i, it in reversed(ordered):
        if it.get("label") and cv.label(it["label"], cv.rect(it["x"], it["y"], it["w"], it["h"]),
                                        colors[i], font):
            labels += 1
    cv.save(out_png)
    return {"path": out_png, "boxes": len(items), "labels": labels,
            "labels_skipped": cv.labels_skipped, "size": [cv.W, cv.H]}


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
    ``scale`` (<=1); every drawn coordinate and the font size are multiplied by it.
    """
    cv = _Canvas(base_png, scale)
    font = load_font(font_px(_BASE_LABEL_PX + 1, scale))
    items = flatten(nodes, labeled_only=labeled_only)
    # Boxes big to small; labels for content-bearing nodes, smallest first.
    items.sort(key=lambda it: -it["rect"][2] * it["rect"][3])
    for it in items:
        cv.box(cv.rect(*it["rect"]), _PALETTE[it["depth"] % len(_PALETTE)])
    drawn = 0
    for it in reversed(items):
        if drawn >= max_labels or not it["has_content"]:
            continue
        if cv.label(it["label"], cv.rect(*it["rect"]), _PALETTE[it["depth"] % len(_PALETTE)],
                    font):
            drawn += 1
    cv.save(out_png)
    return {"path": out_png, "boxes": len(items), "labels": drawn,
            "labels_skipped": cv.labels_skipped, "size": [cv.W, cv.H]}


# --------------------------------------------------------------------------- a11y overlay
# Box every a11y node with its speakable label and the host-computed TalkBack
# reading-order number; colour-code by lint severity when findings are passed.
# Input a11y_dict is the resolved dict from a11y.a11y_to_dict
# ({"windows":[{"root":<node>}...], "focus_order":[...]}).
_SEVERITY_COLOR: Dict[str, Color] = {
    "error": (219, 68, 55),
    "warn": (244, 180, 0),
    "info": (66, 133, 244),
}
_A11Y_BOX_COLOR: Color = (15, 157, 88)  # green for clean nodes
_SEVERITY_RANK = {"error": 3, "warn": 2, "info": 1}


def _a11y_collect_items(roots: List[Dict[str, Any]],
                        focus_order: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Flatten a11y nodes to drawable items, attaching reading-order numbers.

    When the reading order carries announcements (``speak``), focus stops are labelled
    with what TalkBack says there and other nodes stay unlabelled (their text is part of
    a stop's announcement); otherwise every node shows its own speakable text.
    """
    order_by_id: Dict[Any, int] = {}
    speak_by_id: Dict[Any, str] = {}
    for e in focus_order or []:
        nid = e.get("id")
        if nid is not None and e.get("order") is not None:
            order_by_id[nid] = e["order"]
            if e.get("speak"):
                speak_by_id[nid] = e["speak"]
    speech_mode = bool(speak_by_id)
    items: List[Dict[str, Any]] = []
    stack = list(reversed(roots))
    while stack:
        n = stack.pop()
        b = (n.get("bounds") or {}).get("layout") or {}
        x, y, w, h = b.get("x", 0), b.get("y", 0), b.get("w", 0), b.get("h", 0)
        if w > 0 and h > 0:
            if speech_mode:
                label = speak_by_id.get(n.get("id"), "")
            else:
                label = (n.get("speakable") or n.get("text")
                         or n.get("content_description") or n.get("role_description") or "")
            order = n.get("order")
            if order is None:
                order = order_by_id.get(n.get("id"))
            items.append({
                "id": n.get("id"), "x": int(x), "y": int(y), "w": int(w), "h": int(h),
                "label": label, "order": order,
                "host": n.get("host_view_id"), "virtual": n.get("virtual_id"),
            })
        stack.extend(reversed(n.get("children", []) or []))
    return items


def _item_iou(it: Dict[str, Any], b: Dict[str, Any]) -> float:
    ax2, ay2 = it["x"] + it["w"], it["y"] + it["h"]
    bx2, by2 = b["x"] + b["w"], b["y"] + b["h"]
    ix = max(0, min(ax2, bx2) - max(it["x"], b["x"]))
    iy = max(0, min(ay2, by2) - max(it["y"], b["y"]))
    inter = ix * iy
    union = it["w"] * it["h"] + b["w"] * b["h"] - inter
    return inter / union if union > 0 else 0.0


def _finding_bounds(f: Dict[str, Any]) -> Optional[Dict[str, int]]:
    b = f.get("bounds") or {}
    if isinstance(b.get("layout"), dict):
        b = b["layout"]
    try:
        r = {k: int(b[k]) for k in ("x", "y", "w", "h")}
    except (KeyError, TypeError, ValueError):
        return None
    return r if r["w"] > 0 and r["h"] > 0 else None


def _map_finding(f: Dict[str, Any], by_pair: Dict[Tuple[int, int], Dict[str, Any]],
                 by_virtual: Dict[int, List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """The a11y item a finding targets, keyed via the ID contract.

    Typed keys map directly: ``view:<id>`` -> (id, -1); ``compose:<acv>:<sem>`` ->
    (acv, sem); ``composeview:<acv>`` -> (acv, -1); an a11y id pair on the finding's node
    maps as-is. An untyped Compose-semantics finding (``node.id`` = semantics id) maps to
    the a11y virtual node with that id whose bounds overlap the finding's (semantics ids
    repeat across ComposeViews, so the geometry picks the window). Never matches a bare
    id against View ids or packed a11y ids.
    """
    t = finding_key(f)
    if t is not None:
        if t[0] in ("view", "composeview"):
            return by_pair.get((t[1], HOST_VIEW_ID))
        if t[0] == "virt":
            return by_pair.get((t[1], t[2]))
        return None
    node = f.get("node") if isinstance(f.get("node"), dict) else {}
    try:
        sem = int(node.get("id"))
    except (TypeError, ValueError):
        return None
    cands = by_virtual.get(sem, [])
    fb = _finding_bounds(f)
    if fb is None:
        return cands[0] if len(cands) == 1 else None
    scored = sorted(((_item_iou(c, fb), c) for c in cands), key=lambda t: t[0], reverse=True)
    if scored and scored[0][0] >= 0.3 and (len(scored) == 1 or scored[1][0] < scored[0][0]):
        return scored[0][1]
    return None


def write_screen_png(conn: Any, a11y_dict: Dict[str, Any], out_png: str,
                     scale: float = 1.0) -> float:
    """Screenshot every a11y window and composite them into ``out_png``; return its scale.

    ``conn.screenshot(root_id=...)`` captures one window root, so an overlay drawn on the
    first window alone shows an empty area where a dialog or popup is. The windows of
    ``a11y_dict`` (``a11y.a11y_to_dict``; z-ordered, bottom first) are alpha-composited
    at their on-screen origins (their root bounds); see :func:`write_windows_png`.
    """
    wins = []
    for w in a11y_dict.get("windows") or []:
        if not w.get("root"):
            continue
        b = (w["root"].get("bounds") or {}).get("layout") or {}
        wins.append((int(w.get("root_view_id") or 0), int(b.get("x", 0) or 0),
                     int(b.get("y", 0) or 0)))
    return write_windows_png(conn, wins, out_png, scale)


def write_windows_png(conn: Any, windows: List[Tuple[int, int, int]], out_png: str,
                      scale: float = 1.0) -> float:
    """Composite the windows ``[(root_view_id, screen x, screen y), ...]`` (z-ordered,
    bottom first) into ``out_png`` on a canvas the size of the bottom window; return the
    PNG's scale. Falls back to ``screenshot(root_id=0)`` when there is one window, a
    window cannot be captured or Pillow is missing. The window dim behind a dialog is not
    reproduced. Raises RuntimeError when not even the first window can be captured.
    """
    from . import png as pngmod
    from .client import TransportError

    def fallback() -> float:
        shot = conn.screenshot(root_id=0, scale=scale)
        if not shot.HasField("screenshot") or not shot.screenshot.width:
            raise RuntimeError("the agent returned no screenshot")
        pngmod.write_png(shot.screenshot, out_png)
        return float(shot.screenshot.scale) or scale

    if len(windows) < 2 or not _HAVE_PIL:
        return fallback()
    canvas = None
    base_xy = (0, 0)
    got_scale = scale
    for rid, x, y in windows:
        try:
            shot = conn.screenshot(root_id=int(rid or 0), scale=scale)
            if not shot.HasField("screenshot") or not shot.screenshot.width:
                return fallback()
            iw, ih, rgba = pngmod._decode_to_rgba(shot.screenshot)
        except TransportError:
            raise  # the fallback would only fail again, without the real reason
        except Exception:
            return fallback()
        img = Image.frombytes("RGBA", (iw, ih), bytes(rgba))
        if canvas is None:
            canvas, base_xy = img, (x, y)
            got_scale = float(shot.screenshot.scale) or scale
            continue
        s = float(shot.screenshot.scale) or scale
        canvas.alpha_composite(img, (int(round((x - base_xy[0]) * s)),
                                     int(round((y - base_xy[1]) * s))))
    canvas.save(out_png)
    return got_scale


def render_a11y_overlay(base_png: str, a11y_dict: Dict[str, Any], out_png: str,
                        findings: Optional[List[Dict[str, Any]]] = None,
                        max_labels: int = 160, scale: float = 1.0) -> Dict[str, Any]:
    """Draw the a11y tree over ``base_png`` -> ``out_png``.

    Each node gets a box; each TalkBack focus stop gets its reading-order badge and what
    TalkBack announces there (without a computed reading order, every node shows its own
    speakable text). With ``findings`` (dicts from ``a11y_lint.Finding.to_dict``), a
    node is recoloured to the worst severity of the findings that target it (see
    :func:`_map_finding`); a finding that maps to no a11y node is drawn as a dashed box
    at its own bounds. ``scale`` maps full-resolution bounds onto a base PNG captured at
    ``scale`` (<=1).

    Returns ``{path, boxes, labels, flagged, flagged_nodes, flagged_by_bounds,
    findings, findings_unplaced, labels_skipped, size}``; ``flagged`` counts every box
    drawn in a severity colour.
    """
    cv = _Canvas(base_png, scale)
    roots = [w["root"] for w in (a11y_dict.get("windows") or []) if w.get("root")]
    items = _a11y_collect_items(roots, a11y_dict.get("focus_order"))

    by_pair: Dict[Tuple[int, int], Dict[str, Any]] = {}
    by_virtual: Dict[int, List[Dict[str, Any]]] = {}
    for it in items:
        if it.get("host") is None:
            continue
        v = HOST_VIEW_ID if it.get("virtual") is None else int(it["virtual"])
        by_pair.setdefault((int(it["host"]), v), it)
        if v != HOST_VIEW_ID:
            by_virtual.setdefault(v, []).append(it)

    worst: Dict[int, str] = {}  # id(item) -> severity
    orphans: List[Dict[str, Any]] = []
    unplaced = 0
    for f in findings or []:
        sev = f.get("severity")
        if sev not in _SEVERITY_RANK:
            continue
        it = _map_finding(f, by_pair, by_virtual)
        if it is not None:
            if _SEVERITY_RANK[sev] > _SEVERITY_RANK.get(worst.get(id(it), ""), 0):
                worst[id(it)] = sev
            continue
        fb = _finding_bounds(f)
        if fb is None:
            unplaced += 1
            continue
        orphans.append({**fb, "severity": sev, "rule": f.get("rule") or ""})

    label_font = load_font(font_px(_BASE_LABEL_PX, scale))
    badge_font = load_font(font_px(_BASE_BADGE_PX, scale), bold=True)

    # Boxes: largest first so smaller ones sit on top.
    items.sort(key=lambda it: -it["w"] * it["h"])
    for it in items:
        sev = worst.get(id(it))
        it["color"] = _SEVERITY_COLOR[sev] if sev else _A11Y_BOX_COLOR
        cv.box(cv.rect(it["x"], it["y"], it["w"], it["h"]), it["color"])
    for o in orphans:
        cv.box(cv.rect(o["x"], o["y"], o["w"], o["h"]), _SEVERITY_COLOR[o["severity"]],
               dashed=True)

    # Reading-order badges first (the most important text), inside the node's box.
    for it in sorted((i for i in items if i.get("order") is not None), key=lambda i: i["order"]):
        cv.label(str(it["order"]), cv.rect(it["x"], it["y"], it["w"], it["h"]), it["color"],
                 badge_font, pad=2, inside_first=True, force=True)
    # Then speakable labels, smallest (most specific) boxes first.
    drawn = 0
    for it in reversed(items):
        if drawn >= max_labels or not it["label"]:
            continue
        if cv.label(_clip(it["label"]), cv.rect(it["x"], it["y"], it["w"], it["h"]),
                    it["color"], label_font):
            drawn += 1
    for o in orphans:
        rule = o["rule"][5:] if o["rule"].startswith("a11y.") else (o["rule"] or o["severity"])
        cv.label(_clip(rule, 32), cv.rect(o["x"], o["y"], o["w"], o["h"]),
                 _SEVERITY_COLOR[o["severity"]], label_font)

    cv.save(out_png)
    flagged_nodes = len(worst)
    return {"path": out_png, "boxes": len(items) + len(orphans), "labels": drawn,
            "flagged": flagged_nodes + len(orphans), "flagged_nodes": flagged_nodes,
            "flagged_by_bounds": len(orphans), "findings": len(findings or []),
            "findings_unplaced": unplaced, "labels_skipped": cv.labels_skipped,
            "size": [cv.W, cv.H]}


# --------------------------------------------------------------------------- integrated overlay
# Draw the merged View/Compose/a11y tree produced by correlate.py. ``merged["roots"]`` is a
# list of IntegratedNode dicts; each has bounds {x,y,w,h}, node_key, correlation_confidence
# ("exact" | "overlap" | "none", plus a11y_iou for overlaps) and view/compose/a11y facets.
_CONF_COLOR: Dict[str, Color] = {
    "exact": (15, 157, 88),
    "overlap": (244, 180, 0),
    "none": (120, 120, 120),
}


def _integrated_label(node: Dict[str, Any]) -> str:
    key = str(node.get("node_key") or "")
    conf = node.get("correlation_confidence")
    if conf == "overlap":
        iou = node.get("a11y_iou")
        suffix = f"a11y~{float(iou):.2f}" if isinstance(iou, (int, float)) else "a11y~"
        return f"{key} {suffix}".strip()
    if conf is not None and conf not in ("exact", "none"):
        try:  # a numeric confidence from another producer
            return f"{key} ({float(conf):.2f})".strip()
        except (TypeError, ValueError):
            return f"{key} ({conf})".strip()
    return key


def _integrated_collect_items(roots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Depth-first flatten IntegratedNode dicts to render_items items."""
    items: List[Dict[str, Any]] = []
    stack = list(reversed(roots))
    while stack:
        node = stack.pop()
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
                "color": _CONF_COLOR.get(str(node.get("correlation_confidence")),
                                         _PALETTE[0]),
            })
        stack.extend(reversed(node.get("children", []) or []))
    return items


def render_integrated_overlay(base_png: str, merged: Dict[str, Any], out_png: str,
                              scale: float = 1.0) -> Dict[str, Any]:
    """Draw the correlate-merged tree over ``base_png`` -> ``out_png``.

    Flattens ``merged["roots"]`` (IntegratedNode dicts) by their flat ``bounds``, labels
    each with its node_key (plus ``a11y~<iou>`` for bounds-overlap correlations) and
    colours boxes by correlation (green exact, amber overlap, grey none). ``scale`` maps
    full-resolution node bounds onto a base PNG captured at ``scale``.
    Returns ``{"path","boxes","labels","labels_skipped","size"}``.
    """
    items = _integrated_collect_items(merged.get("roots") or [])
    return render_items(base_png, items, out_png, scale=scale)
