"""Host-side accessibility lint over the integrated Inspector Widget model.

Consumes the Compose **semantics** tree (the dict shape from
``strings.compose_node_to_dict``: ``{id, name, kind, render_node_id?, bounds,
source?, attrs:{<SemanticsKey>: <stringified value>}, children:[...]}``) — the
merged-root tree TalkBack actually consumes — plus a decoded screenshot RGBA
buffer for the one pixel-based rule (contrast). Emits ``Finding`` records.

Implements rules R1..R12 from lint.md. Tree-only rules run with no image;
the image rule (R3 contrast) auto-skips when no screenshot is supplied. ``attrs``
keys are raw ``SemanticsPropertyKey.getName()`` strings (``Text``, ``Role``,
``OnClick``, ``ToggleableState``, ``Heading``, ``StateDescription``, ...); values
are ``toString()``-stringified, so ``Role`` reads ``"Button"`` and
``ToggleableState`` reads ``"On"/"Off"/"Indeterminate"`` (see lint.md §0).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

DP_BASE = 160.0

# --- shared vocabulary (lint.md §1) ----------------------------------------- #
ACTION_KEYS = {"OnClick", "OnLongClick", "SetText", "RequestFocus"}
INTERACTIVE_ROLES = {"Button", "Switch", "Checkbox", "RadioButton", "Tab", "DropdownList"}
TYPE_NOUNS = {"button", "btn", "image", "img", "icon", "graphic", "link", "tab", "checkbox"}
IMAGE_ROLES = {"Image"}
IMAGE_NAMES = {"Image", "Icon", "AsyncImage"}
IMAGE_VIEW_CLASSES = {"ImageView", "ImageButton"}
STATEFUL_ROLES = {"Switch", "Checkbox", "RadioButton", "Tab"}
TOGGLE_WORD_HINTS = ("on", "off", "enabled", "disabled", "mute", "unmute", "toggle")


@dataclass
class LintContext:
    """Everything a rule needs that is not in the node dict itself."""

    density: int = 420            # device dpi; dp = px / (density/160)
    font_scale: float = 1.0       # system font scale (for R11)
    screenshot_rgba: Optional[bytes] = None  # row-major RGBA8888, or None
    screenshot_w: int = 0
    screenshot_h: int = 0
    screenshot_scale: float = 1.0  # capture scale; bounds*scale -> pixel index
    # optional render_node_id -> (w, h, rgba) for the higher-fidelity contrast path
    component_image_fn: Optional[Callable[[int], Optional[Tuple[int, int, bytes]]]] = None
    wcag_mode: bool = False        # use WCAG 44dp / 24dp floors instead of Material 48dp

    @property
    def has_image(self) -> bool:
        return self.screenshot_rgba is not None and self.screenshot_w > 0


@dataclass
class Finding:
    rule: str                # rule id, e.g. "a11y.label.missing"
    severity: str            # "error" | "warn" | "info"
    node: Dict[str, Any]     # {id, name, role, source}
    bounds: Dict[str, int]   # px (window/screenshot space)
    bounds_dp: Dict[str, float]
    message: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    needs_image: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "node": self.node,
            "bounds": self.bounds,
            "bounds_dp": self.bounds_dp,
            "message": self.message,
            "evidence": self.evidence,
            "needs_image": self.needs_image,
        }


# --------------------------------------------------------------------------- #
# Helpers (lint.md §1 "Helper predicates")
# --------------------------------------------------------------------------- #
def _attrs(n: Dict[str, Any]) -> Dict[str, Any]:
    return n.get("attrs") or {}


def _layout(n: Dict[str, Any]) -> Dict[str, Any]:
    return (n.get("bounds") or {}).get("layout") or {}


def _dp(px: float, ctx: LintContext) -> float:
    if ctx.density <= 0:
        return float(px)
    return round(px / (ctx.density / DP_BASE), 1)


def _get(attrs: Dict[str, Any], key: str) -> str:
    v = attrs.get(key)
    if v is None:
        return ""
    return str(v).strip()


def label_of(n: Dict[str, Any]) -> str:
    """First non-empty real label key (NOT ``name``, which may be a Role/TestTag)."""
    a = _attrs(n)
    for k in ("ContentDescription", "Text", "EditableText", "StateDescription"):
        v = _get(a, k)
        if v and v.lower() not in ("null", "[]"):
            return v
    return ""


def role_of(n: Dict[str, Any]) -> Optional[str]:
    r = _get(_attrs(n), "Role")
    return r or None


def is_actionable(n: Dict[str, Any]) -> bool:
    a = _attrs(n)
    if ACTION_KEYS & set(a.keys()):
        return True
    return role_of(n) in INTERACTIVE_ROLES


def is_invisible(n: Dict[str, Any]) -> bool:
    a = _attrs(n)
    b = _layout(n)
    if "InvisibleToUser" in a:
        return True
    if b.get("w", 0) <= 0 or b.get("h", 0) <= 0:
        return True
    return False


def is_disabled(n: Dict[str, Any]) -> bool:
    return "Disabled" in _attrs(n)


def _has_labeled_descendant(n: Dict[str, Any]) -> bool:
    """A child whose Text/ContentDescription merges up into this node's name."""
    for c in n.get("children", []) or []:
        if label_of(c):
            return True
        if _has_labeled_descendant(c):
            return True
    return False


def _bounds_pair(n: Dict[str, Any], ctx: LintContext) -> Tuple[Dict[str, int], Dict[str, float]]:
    b = _layout(n)
    bx = {
        "x": int(b.get("x", 0)),
        "y": int(b.get("y", 0)),
        "w": int(b.get("w", 0)),
        "h": int(b.get("h", 0)),
    }
    bdp = {k: _dp(v, ctx) for k, v in bx.items()}
    return bx, bdp


def _node_ref(n: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": n.get("id"),
        "name": n.get("name"),
        "role": role_of(n),
        "source": n.get("source"),
    }


def _is_image(n: Dict[str, Any]) -> bool:
    if role_of(n) in IMAGE_ROLES:
        return True
    name = (n.get("name") or "")
    if name in IMAGE_NAMES:
        return True
    # View path: class name carries ImageView/ImageButton.
    cls = (n.get("class_name") or n.get("qualified_name") or "")
    return any(c in cls for c in IMAGE_VIEW_CLASSES)


# --------------------------------------------------------------------------- #
# R1 — missing label on actionable
# --------------------------------------------------------------------------- #
def rule_missing_label(n, chain, ctx) -> List[Finding]:
    if not is_actionable(n) or label_of(n):
        return []
    if _has_labeled_descendant(n):
        return []
    bx, bdp = _bounds_pair(n, ctx)
    r = role_of(n) or "element"
    return [Finding(
        "a11y.label.missing", "error", _node_ref(n), bx, bdp,
        f"Actionable {r} has no text/contentDescription; add "
        f"`Modifier.semantics{{ contentDescription = ... }}` (or visible Text). "
        f"TalkBack will announce it as an unlabeled {r}.",
        {"role": r, "actionable_reason": _actionable_reason(n),
         "has_text": False, "has_contentdesc": False},
    )]


def _actionable_reason(n: Dict[str, Any]) -> str:
    a = _attrs(n)
    for k in ACTION_KEYS:
        if k in a:
            return k
    return f"Role={role_of(n)}"


# --------------------------------------------------------------------------- #
# R2 — touch target too small
# --------------------------------------------------------------------------- #
def rule_touch_target(n, chain, ctx) -> List[Finding]:
    if not is_actionable(n) or is_invisible(n):
        return []
    # WCAG 2.5.8 inline-text-link exception: Role absent and parent is a Text node.
    if role_of(n) is None and chain:
        parent = chain[-1]
        if _get(_attrs(parent), "Text"):
            return []
    bx, bdp = _bounds_pair(n, ctx)
    min_dp = 44 if ctx.wcag_mode else 48
    if bdp["w"] >= min_dp and bdp["h"] >= min_dp:
        return []
    sev = "error" if (bdp["w"] < 32 and bdp["h"] < 32) else "warn"
    std = "wcag" if ctx.wcag_mode else "material"
    return [Finding(
        "a11y.touch_target.small", sev, _node_ref(n), bx, bdp,
        f"Touch target is {bdp['w']}x{bdp['h']}dp (< {min_dp}dp); enlarge to "
        f">={min_dp}x{min_dp}dp via `Modifier.sizeIn(minWidth={min_dp}.dp,"
        f"minHeight={min_dp}.dp)` or `minimumInteractiveComponentSize()`. "
        f"Padding alone does not grow the target. (24dp is the WCAG hard floor.)",
        {"w_dp": bdp["w"], "h_dp": bdp["h"], "min_dp": min_dp, "standard": std},
    )]


# --------------------------------------------------------------------------- #
# R3 — low contrast (the one image rule)
# --------------------------------------------------------------------------- #
def _rel_lum(rgb: Tuple[int, int, int]) -> float:
    def lin(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _crop_rgba(ctx: LintContext, bx: Dict[str, int]) -> List[Tuple[int, int, int]]:
    s = ctx.screenshot_scale
    x0, y0 = int(bx["x"] * s), int(bx["y"] * s)
    x1, y1 = int((bx["x"] + bx["w"]) * s), int((bx["y"] + bx["h"]) * s)
    W, H = ctx.screenshot_w, ctx.screenshot_h
    buf = ctx.screenshot_rgba
    px: List[Tuple[int, int, int]] = []
    if buf is None:
        return px
    y0 = max(0, y0); y1 = min(H, y1)
    x0 = max(0, x0); x1 = min(W, x1)
    for yy in range(y0, y1):
        row = (yy * W) * 4
        for xx in range(x0, x1):
            o = row + xx * 4
            px.append((buf[o], buf[o + 1], buf[o + 2]))
    return px


def _component_px(ctx: LintContext, rid: int) -> List[Tuple[int, int, int]]:
    if not ctx.component_image_fn:
        return []
    out = ctx.component_image_fn(rid)
    if not out:
        return []
    w, h, rgba = out
    return [(rgba[i], rgba[i + 1], rgba[i + 2]) for i in range(0, w * h * 4, 4)]


def _fg_bg_split(pixels):
    """1-D k-means(2) on relative luminance -> (fg_rgb, bg_rgb, fg_fraction)."""
    if len(pixels) < 64:
        return None
    lums = [_rel_lum(p) for p in pixels]
    lo, hi = min(lums), max(lums)
    if hi - lo < 1e-3:
        return None
    c0, c1 = lo, hi
    g0: List[int] = []
    g1: List[int] = []
    for _ in range(12):
        g0 = [i for i, L in enumerate(lums) if abs(L - c0) <= abs(L - c1)]
        g0set = set(g0)
        g1 = [i for i in range(len(lums)) if i not in g0set]
        if not g0 or not g1:
            break
        c0 = sum(lums[i] for i in g0) / len(g0)
        c1 = sum(lums[i] for i in g1) / len(g1)
    if not g0 or not g1:
        return None
    big, small = (g0, g1) if len(g0) >= len(g1) else (g1, g0)  # bg=big, fg=small

    def mean(idx: List[int]) -> Tuple[int, int, int]:
        k = len(idx)
        return (
            sum(pixels[i][0] for i in idx) // k,
            sum(pixels[i][1] for i in idx) // k,
            sum(pixels[i][2] for i in idx) // k,
        )

    frac = len(small) / len(pixels)
    if frac < 0.015:  # no real text strokes sampled
        return None
    return mean(small), mean(big), frac


def _hex(c: Tuple[int, int, int]) -> str:
    return "#%02X%02X%02X" % c


def rule_contrast(n, chain, ctx) -> List[Finding]:
    a = _attrs(n)
    if not (_get(a, "Text") or _get(a, "EditableText")):
        return []
    if not ctx.has_image or is_invisible(n) or is_disabled(n):
        return []
    bx, bdp = _bounds_pair(n, ctx)
    pixels: List[Tuple[int, int, int]] = []
    rid = n.get("render_node_id")
    if ctx.component_image_fn and rid:
        pixels = _component_px(ctx, int(rid))
        sample_src = "component"
    if not pixels:
        pixels = _crop_rgba(ctx, bx)
        sample_src = "screenshot"
    split = _fg_bg_split(pixels)
    if not split:
        return []
    fg, bg, frac = split
    Lf, Lb = _rel_lum(fg), _rel_lum(bg)
    ratio = round((max(Lf, Lb) + 0.05) / (min(Lf, Lb) + 0.05), 2)
    large = bdp["h"] >= 24  # ~18pt
    required = 3.0 if large else 4.5
    if ratio >= required:
        return []
    sev = "error" if (not large and ratio < 3.0) else "warn"
    return [Finding(
        "a11y.contrast.low", sev, _node_ref(n), bx, bdp,
        f"Measured contrast {ratio}:1 (fg {_hex(fg)} vs bg {_hex(bg)}) needs "
        f">={required}:1. Darken the text or lighten the background; this is "
        f"sampled from rendered pixels so it reflects the actual theme.",
        {"ratio": ratio, "required": required, "fg_hex": _hex(fg), "bg_hex": _hex(bg),
         "fg_lum": round(Lf, 4), "bg_lum": round(Lb, 4), "sample": sample_src,
         "px_sampled": len(pixels), "fg_fraction": round(frac, 3),
         "text_size_class": "large" if large else "normal",
         "low_confidence": frac < 0.05},
        needs_image=True,
    )]


# --------------------------------------------------------------------------- #
# R4 — redundant / duplicate label
# --------------------------------------------------------------------------- #
def rule_redundant_label(n, chain, ctx) -> List[Finding]:
    a = _attrs(n)
    role = role_of(n)
    text = _get(a, "Text")
    desc = _get(a, "ContentDescription")
    if not (desc or text):
        return []
    bx, bdp = _bounds_pair(n, ctx)
    if desc and text and desc.lower() == text.lower():
        return [Finding(
            "a11y.label.redundant", "info", _node_ref(n), bx, bdp,
            "contentDescription duplicates the visible Text; remove the "
            "contentDescription so the visible text is used.",
            {"label": desc, "role": role, "reason": "equals_text"},
        )]
    # Type-noun match only makes sense when a Role is present (TalkBack adds it).
    if role:
        label = (desc or text).lower()
        padded = f" {label} "
        for w in TYPE_NOUNS:
            if f" {w} " in padded or f" {w}" == padded.rstrip() or label.endswith(" " + w) or label == w:
                return [Finding(
                    "a11y.label.redundant", "warn", _node_ref(n), bx, bdp,
                    f"Label contains the type word '{w}'; TalkBack already "
                    f"announces the Role. Use the label without '{w}' "
                    f"(e.g. 'Submit', not 'Submit {w}').",
                    {"label": label, "role": role, "reason": "type_noun", "matched_word": w},
                )]
    return []


# --------------------------------------------------------------------------- #
# R5 — clickable without a role
# --------------------------------------------------------------------------- #
def rule_role_missing(n, chain, ctx) -> List[Finding]:
    a = _attrs(n)
    if "OnClick" not in a:
        return []
    if role_of(n) is not None:
        return []
    bx, bdp = _bounds_pair(n, ctx)
    return [Finding(
        "a11y.role.missing_on_clickable", "warn", _node_ref(n), bx, bdp,
        "Clickable node has no Role; add `Modifier.semantics{ role = "
        "Role.Button }` (or use Button/IconButton). Without a role TalkBack "
        "cannot tell users it is actionable.",
        {"has_onclick": True, "has_role": False, "label": label_of(n)},
    )]


# --------------------------------------------------------------------------- #
# R6 — image without description
# --------------------------------------------------------------------------- #
def rule_image_no_desc(n, chain, ctx) -> List[Finding]:
    if not _is_image(n):
        return []
    a = _attrs(n)
    if _get(a, "ContentDescription"):
        return []
    if "InvisibleToUser" in a:  # explicitly decorative
        return []
    bx, bdp = _bounds_pair(n, ctx)
    actionable = is_actionable(n)
    # Clickable image with no description is unambiguously broken -> error;
    # otherwise null-vs-missing is indistinguishable post-hoc -> warn.
    sev = "error" if actionable else "warn"
    return [Finding(
        "a11y.image.no_description", sev, _node_ref(n), bx, bdp,
        "Image has no contentDescription. Set `contentDescription` if "
        "meaningful, or mark it decorative (`contentDescription = null` and "
        "ensure it is not focusable, or `Modifier.semantics{ invisibleToUser() }`)."
        + (" This image is also clickable, so a missing label is a definite bug." if actionable else ""),
        {"kind": "image", "actionable": actionable, "marked_decorative": False},
    )]


# --------------------------------------------------------------------------- #
# R7 — state not exposed
# --------------------------------------------------------------------------- #
def rule_state_not_exposed(n, chain, ctx) -> List[Finding]:
    a = _attrs(n)
    role = role_of(n)
    has_toggleable = "ToggleableState" in a
    has_selected = "Selected" in a
    has_statedesc = bool(_get(a, "StateDescription"))
    # ToggleableState/Selected ARE exposed by TalkBack; only flag a *custom*
    # toggle that looks stateful but carries no semantic state.
    if has_toggleable or has_selected or has_statedesc:
        return []
    if not is_actionable(n):
        return []
    label = label_of(n).lower()
    looks_toggle = role == "Switch" or any(w in label.split() for w in TOGGLE_WORD_HINTS)
    if not looks_toggle:
        return []
    bx, bdp = _bounds_pair(n, ctx)
    return [Finding(
        "a11y.state.not_exposed", "warn", _node_ref(n), bx, bdp,
        "This control looks stateful but exposes no semantic state. Use "
        "`Modifier.toggleable(value=...)` / `selectable(...)`, or "
        "`Modifier.semantics{ stateDescription = if (on) \"On\" else \"Off\" }`.",
        {"role": role, "has_toggleable": False, "has_selected": False,
         "has_statedesc": False},
    )]


# --------------------------------------------------------------------------- #
# R8 — focusable but empty
# --------------------------------------------------------------------------- #
def rule_empty_focusable(n, chain, ctx) -> List[Finding]:
    a = _attrs(n)
    focusable = ("OnClick" in a) or ("RequestFocus" in a) or ("Focused" in a)
    if not focusable:
        return []
    if is_invisible(n):
        return []
    announceable = bool(
        label_of(n) or role_of(n) or _get(a, "StateDescription")
        or _has_labeled_descendant(n)
    )
    if announceable:
        return []
    bx, bdp = _bounds_pair(n, ctx)
    structural_only = [k for k in a.keys() if k in ("TestTag",)]
    return [Finding(
        "a11y.node.empty_focusable", "warn", _node_ref(n), bx, bdp,
        "This element takes accessibility focus but announces nothing. Give it "
        "content/label, or remove it from the a11y tree with "
        "`Modifier.clearAndSetSemantics{}` / `importantForAccessibility=no`.",
        {"focusable": True, "announceable_keys": [],
         "structural_keys": structural_only},
    )]


# --------------------------------------------------------------------------- #
# Per-node rule registry (lint.md §3 table).
# --------------------------------------------------------------------------- #
RULES: List[Tuple[str, bool, Callable]] = [
    # (id, needs_image, fn)
    ("a11y.label.missing", False, rule_missing_label),
    ("a11y.touch_target.small", False, rule_touch_target),
    ("a11y.contrast.low", True, rule_contrast),
    ("a11y.label.redundant", False, rule_redundant_label),
    ("a11y.role.missing_on_clickable", False, rule_role_missing),
    ("a11y.image.no_description", False, rule_image_no_desc),
    ("a11y.state.not_exposed", False, rule_state_not_exposed),
    ("a11y.node.empty_focusable", False, rule_empty_focusable),
]

# Ids of every rule (per-node + structural), exposed for callers / CLI.
ALL_RULE_IDS = [rid for rid, _, _ in RULES] + [
    "a11y.heading.structure",
    "a11y.grouping.missing",
    "a11y.text.fixed_scaling",
    "a11y.duplicate.label",
]


# --------------------------------------------------------------------------- #
# Cross-node / structural rules (R9, R10, R11, R12).
# --------------------------------------------------------------------------- #
def _flatten(roots: List[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
    """DFS flatten to [(node, parent_chain)] in traversal (visual) order."""
    out: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]] = []

    def walk(n: Dict[str, Any], chain: List[Dict[str, Any]]) -> None:
        out.append((n, chain))
        for c in n.get("children", []) or []:
            walk(c, chain + [n])

    for r in roots:
        walk(r, [])
    return out


def _norm_label(s: str) -> str:
    return " ".join(s.lower().split())


def _content_height(flat) -> int:
    bottom = 0
    for n, _ in flat:
        b = _layout(n)
        bottom = max(bottom, b.get("y", 0) + b.get("h", 0))
    return bottom


def rule_headings(flat, ctx: LintContext, screen_h: int) -> List[Finding]:
    """R9 — heading structure (cross-node)."""
    out: List[Finding] = []
    headings = [(n, c) for (n, c) in flat if "Heading" in _attrs(n)]
    text_nodes = [n for (n, _) in flat if _get(_attrs(n), "Text")]
    content_h = _content_height(flat)
    long_screen = (
        len(text_nodes) > 12
        and screen_h > 0
        and content_h > 1.5 * screen_h
    )
    # (a) no headings on a long content screen -> info.
    if not headings and long_screen:
        n0 = flat[0][0] if flat else {"id": None, "name": None}
        bx, bdp = _bounds_pair(n0, ctx)
        out.append(Finding(
            "a11y.heading.structure", "info", _node_ref(n0), bx, bdp,
            "Long content screen has no headings. Mark section titles with "
            "`Modifier.semantics{ heading() }` so TalkBack users can jump "
            "between sections.",
            {"heading_count": 0, "text_node_count": len(text_nodes),
             "content_height_px": content_h},
        ))
    # (b) empty heading label / (c) duplicate adjacent identical headings.
    prev_label: Optional[str] = None
    for n, _ in sorted(headings, key=lambda nc: _layout(nc[0]).get("y", 0)):
        bx, bdp = _bounds_pair(n, ctx)
        lbl = label_of(n)
        if not lbl:
            out.append(Finding(
                "a11y.heading.structure", "warn", _node_ref(n), bx, bdp,
                "Heading has no label; an empty heading is a confusing stop "
                "for TalkBack's heading navigation.",
                {"reason": "empty_heading"},
            ))
        elif prev_label is not None and _norm_label(lbl) == prev_label:
            out.append(Finding(
                "a11y.heading.structure", "info", _node_ref(n), bx, bdp,
                f"Duplicate adjacent heading '{lbl}'.",
                {"reason": "duplicate_adjacent", "label": lbl},
            ))
        prev_label = _norm_label(lbl) if lbl else prev_label
    return out


def rule_grouping(flat, ctx: LintContext) -> List[Finding]:
    """R10 — missing grouping (cross-node): co-located, separately-focusable leaves."""
    out: List[Finding] = []
    gap_dp = 8
    for n, _ in flat:
        if "OnClick" in _attrs(n):  # parent already merges/handles the click
            continue
        children = n.get("children", []) or []
        # Leaf, separately-labeled, not-merged children.
        leaves = [
            c for c in children
            if not (c.get("children"))
            and label_of(c)
            and "OnClick" not in _attrs(c)
        ]
        if len(leaves) < 3:
            continue
        pb = _layout(n)
        if pb.get("w", 0) <= 0 or pb.get("h", 0) <= 0:
            continue
        # All leaves must sit tightly within the parent rect.
        if not all(_within(_layout(c), pb) for c in leaves):
            continue
        # Vertically stacked with small gaps -> a list-row shape.
        ys = sorted(leaves, key=lambda c: _layout(c).get("y", 0))
        gaps_ok = True
        for a, b in zip(ys, ys[1:]):
            la, lb = _layout(a), _layout(b)
            gap = lb.get("y", 0) - (la.get("y", 0) + la.get("h", 0))
            if _dp(max(gap, 0), ctx) > gap_dp:
                gaps_ok = False
                break
        if not gaps_ok:
            continue
        bx, bdp = _bounds_pair(n, ctx)
        out.append(Finding(
            "a11y.grouping.missing", "info", _node_ref(n), bx, bdp,
            "These elements read as separate focus stops; wrap the row in "
            "`Modifier.semantics(mergeDescendants = true){}` (or a clickable "
            "parent) so TalkBack announces them together.",
            {"group_size": len(leaves),
             "parent_bounds": bx,
             "children_ids": [c.get("id") for c in leaves]},
        ))
    return out


def _within(inner: Dict[str, Any], outer: Dict[str, Any], slop: int = 2) -> bool:
    ix, iy = inner.get("x", 0), inner.get("y", 0)
    iw, ih = inner.get("w", 0), inner.get("h", 0)
    ox, oy = outer.get("x", 0), outer.get("y", 0)
    ow, oh = outer.get("w", 0), outer.get("h", 0)
    return (
        ix >= ox - slop and iy >= oy - slop
        and ix + iw <= ox + ow + slop and iy + ih <= oy + oh + slop
    )


def rule_fixed_scaling(flat, ctx: LintContext) -> List[Finding]:
    """R11 — fixed (non-sp) text scaling (best-effort, tree-only)."""
    out: List[Finding] = []
    for n, _ in flat:
        a = _attrs(n)
        if not _get(a, "Text"):
            continue
        # View path: a textSize attribute whose unit resolves to px/dp not sp.
        unit = None
        size = None
        for key in ("textSize", "TextSize", "fontSize"):
            if key in a:
                raw = str(a.get(key))
                size = raw
                low = raw.lower()
                if "sp" in low:
                    unit = "sp"
                elif "dp" in low or "px" in low:
                    unit = "px"
                break
        if unit == "px":
            bx, bdp = _bounds_pair(n, ctx)
            out.append(Finding(
                "a11y.text.fixed_scaling", "info", _node_ref(n), bx, bdp,
                "Text size appears fixed (dp/px, not sp); it will not honor the "
                "user's font-scale setting. Use `sp` (`fontSize = 16.sp`).",
                {"unit": unit, "size": size, "scaled_under_fontscale": None},
            ))
    return out


def rule_duplicate_label(flat, ctx: LintContext) -> List[Finding]:
    """R12 — duplicate labels on distinct actionable nodes (cross-node)."""
    out: List[Finding] = []
    groups: Dict[str, List[Tuple[Dict[str, Any], List[Dict[str, Any]]]]] = {}
    for n, chain in flat:
        if not is_actionable(n):
            continue
        lbl = label_of(n)
        if not lbl:
            continue
        groups.setdefault(_norm_label(lbl), []).append((n, chain))
    for norm, members in groups.items():
        if len(members) < 2:
            continue
        # Skip obvious uniform repeating lists: same parent + uniform spacing.
        parents = {id(chain[-1]) if chain else None for _, chain in members}
        if len(parents) == 1:
            continue  # likely a real list row; ambiguity is expected there
        node_ids = [n.get("id") for n, _ in members]
        sources = [n.get("source") for n, _ in members if n.get("source")]
        n0 = members[0][0]
        bx, bdp = _bounds_pair(n0, ctx)
        out.append(Finding(
            "a11y.duplicate.label", "info", _node_ref(n0), bx, bdp,
            f"{len(members)} actionable nodes share the label '{label_of(n0)}'. "
            f"Disambiguate them (e.g. 'Open settings' vs 'Open profile') or "
            f"group them so context is clear.",
            {"label": label_of(n0), "node_ids": node_ids, "sources": sources},
        ))
    return out


def _structural_rules(
    roots: List[Dict[str, Any]], ctx: LintContext, enabled: Optional[set], screen_h: int
) -> List[Finding]:
    flat = _flatten(roots)
    out: List[Finding] = []

    def on(rid: str) -> bool:
        return enabled is None or rid in enabled

    if on("a11y.heading.structure"):
        out += rule_headings(flat, ctx, screen_h)
    if on("a11y.grouping.missing"):
        out += rule_grouping(flat, ctx)
    if on("a11y.text.fixed_scaling"):
        out += rule_fixed_scaling(flat, ctx)
    if on("a11y.duplicate.label"):
        out += rule_duplicate_label(flat, ctx)
    return out


# --------------------------------------------------------------------------- #
# Driver (lint.md §1)
# --------------------------------------------------------------------------- #
def _dedupe(findings: List[Finding]) -> List[Finding]:
    seen = set()
    out: List[Finding] = []
    for f in findings:
        key = (f.rule, f.node.get("id"),
               f.bounds.get("x"), f.bounds.get("y"),
               f.evidence.get("matched_word"), f.evidence.get("reason"))
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def lint_tree(
    roots: List[Dict[str, Any]],
    ctx: LintContext,
    enabled: Optional[set] = None,
) -> List[Finding]:
    """Run all enabled rules over the semantics tree; return deduped findings.

    ``enabled`` is a set of rule ids (None == all). Image rules auto-skip when
    no screenshot is in the context, so a caller can run "tree-only" simply by
    not supplying pixels.
    """
    out: List[Finding] = []
    screen_h = ctx.screenshot_h or _content_height(_flatten(roots))

    def walk(n: Dict[str, Any], chain: List[Dict[str, Any]]) -> None:
        # Skip nodes with no real on-screen bounds (off-screen / unmeasured —
        # e.g. below the fold of a tall scroll). They can't be meaningfully
        # linted (no touch target, no pixels) and would only add noise. Still
        # recurse so any measured descendants are checked.
        b = (n.get("bounds") or {}).get("layout") or {}
        if b.get("w", 0) > 0 and b.get("h", 0) > 0:
            for rid, needs_image, fn in RULES:
                if enabled is not None and rid not in enabled:
                    continue
                if needs_image and not ctx.has_image:
                    continue
                try:
                    out.extend(fn(n, chain, ctx))
                except Exception:
                    # A single malformed node must never abort the whole lint.
                    continue
        for c in n.get("children", []) or []:
            walk(c, chain + [n])

    for r in roots:
        walk(r, [])

    try:
        out.extend(_structural_rules(roots, ctx, enabled, screen_h))
    except Exception:
        pass
    return _dedupe(out)


def summarize(findings: List[Finding]) -> Dict[str, Any]:
    """Counts by severity + by rule, for the tool/CLI response."""
    by_sev = {"error": 0, "warn": 0, "info": 0}
    by_rule: Dict[str, int] = {}
    for f in findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
        by_rule[f.rule] = by_rule.get(f.rule, 0) + 1
    return {**by_sev, "total": len(findings), "by_rule": by_rule}


# --------------------------------------------------------------------------- #
# Dict-oriented entry points (shared contract: mcp_server / correlate).
# --------------------------------------------------------------------------- #
def lint_a11y(
    roots: List[Dict[str, Any]],
    density: int = 420,
    font_scale: float = 1.0,
    enabled: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Lint Compose-semantics root dicts and return finding DICTS.

    ``roots`` are the COMPOSE-SEMANTICS root dicts that ``lint_tree`` consumes.
    ``density`` is the device DPI (int) and feeds ``LintContext.density``;
    ``font_scale`` feeds ``LintContext.font_scale``. ``enabled`` is an optional
    set of rule ids (None == all). No screenshot is supplied here, so the image
    rule (R3 contrast) auto-skips.
    """
    ctx = LintContext(density=density, font_scale=font_scale)
    return [f.to_dict() for f in lint_tree(roots, ctx, enabled)]


def summarize_dicts(findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Severity counts over finding DICTS (uses ``f["severity"]``)."""
    counts = {"error": 0, "warn": 0, "info": 0}
    for f in findings:
        sev = f.get("severity")
        if sev in counts:
            counts[sev] += 1
    return {**counts, "total": len(findings)}
