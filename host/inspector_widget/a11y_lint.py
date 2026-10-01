"""Host-side accessibility lint over the UNIFIED accessibility tree.

Input
=====
The primary input is the resolved a11y dump (``a11y.a11y_to_dict``): every
``AccessibilityNodeInfo`` TalkBack sees -- classic Views *and* Compose virtual
nodes, including AndroidView-in-Compose and ComposeView-in-RecyclerView -- in one
tree, with screen-space touch bounds, state flags, decoded actions, collection
info, linkage ids and (when requested) ``ExtraRenderingInfo`` text size.

The Compose semantics dump (``strings.dump_compose_to_dict``) is optional *extra
detail*, joined per node by ``(AndroidComposeView id, semantics id)``: exact
``Role``, ``TestTag``, the merged label and ``source`` when present. The rules
never depend on it, so a View-only screen lints exactly like a Compose one.

Identity (the host ID contract)
===============================
* ``A11yNode.host_view_id`` is the uniqueDrawingId of the node's own backing View
  (the provider host, e.g. the AndroidComposeView, for virtual nodes);
  ``virtual_id`` is -1 for real Views, otherwise the virtual descendant id (for
  Compose, the SemanticsNode id).
* The a11y node id is ``(host_view_id << 32) ^ (virtual_id & 0xFFFFFFFF)``;
  ``traversal_before/after`` and ``label_for/labeled_by(_list)`` use that id space.
* Findings carry a typed ``node_key``: ``view:<uniqueDrawingId>`` for real Views,
  ``compose:<acvId>:<semanticsId>`` for Compose nodes (and ``virtual:<host>:<id>``
  for non-Compose providers such as WebView). ``node.id`` stays the a11y id so the
  a11y overlay can colour boxes by finding.

Legacy input -- a list of Compose semantics root dicts -- is still accepted by
``lint_tree`` / ``lint_a11y`` and runs through the same rules via an adapter.

Rules
=====
R1..R18 (see ``RULE_SPECS`` and ``skill/inspector-widget-a11y/rules.md``). Rule ids
may be given as the canonical id (``a11y.label.missing``), the alias (``R1``) or
the ATF check name (``SpeakableTextPresent``); unknown ids raise
``UnknownRuleError``. A rule that raises on a node is reported in the
diagnostics, never silently swallowed.
"""

from __future__ import annotations

import math
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

DP_BASE = 160.0
DEFAULT_DENSITY = 420  # used (and reported) when the device density is unknown

SEVERITIES = ("error", "warn", "info")


# --------------------------------------------------------------------------- #
# Rule catalogue.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RuleSpec:
    id: str
    alias: str
    title: str
    severities: Tuple[str, ...]
    atf: Optional[str] = None
    needs_image: bool = False


RULE_SPECS: Tuple[RuleSpec, ...] = (
    RuleSpec("a11y.label.missing", "R1", "Actionable element has no accessible name",
             ("error",), "SpeakableTextPresent"),
    RuleSpec("a11y.touch_target.small", "R2", "Touch target below the minimum size",
             ("error", "warn", "info"), "TouchTargetSize"),
    RuleSpec("a11y.contrast.low", "R3", "Text contrast below WCAG 1.4.3",
             ("error", "warn"), "TextContrast", needs_image=True),
    RuleSpec("a11y.label.redundant", "R4", "contentDescription repeats the role, state or text",
             ("warn", "info"), "RedundantDescription"),
    RuleSpec("a11y.role.missing_on_clickable", "R5", "Clickable element exposes no role",
             ("warn", "info")),
    RuleSpec("a11y.image.no_description", "R6", "Image exposed to accessibility with no description",
             ("warn",), "ImageContentDescription"),
    RuleSpec("a11y.state.not_exposed", "R7", "Stateful-looking control exposes no state",
             ("warn", "info")),
    RuleSpec("a11y.node.empty_focusable", "R8", "Focusable element announces nothing",
             ("warn",)),
    RuleSpec("a11y.heading.structure", "R9", "Headings missing, empty or duplicated",
             ("warn", "info")),
    RuleSpec("a11y.grouping.missing", "R10", "Related text reads as separate focus stops",
             ("info",)),
    RuleSpec("a11y.text.fixed_scaling", "R11", "Text size ignores the user's font scale",
             ("warn",), "TextSize"),
    RuleSpec("a11y.duplicate.label", "R12", "Distinct actionable elements share one label",
             ("warn", "info"), "DuplicateSpeakableText"),
    RuleSpec("a11y.clickable.duplicate_bounds", "R13", "Clickable elements share identical bounds",
             ("warn",), "DuplicateClickableBounds"),
    RuleSpec("a11y.editable.content_description", "R14", "Editable field has a contentDescription",
             ("error",), "EditableContentDesc"),
    RuleSpec("a11y.link.purpose_unclear", "R15", "Link or action text does not describe its purpose",
             ("warn", "info"), "LinkPurposeUnclear"),
    RuleSpec("a11y.form.label_missing", "R16", "Form field has no label",
             ("error",)),
    RuleSpec("a11y.traversal.order", "R17", "Traversal order constraints form a cycle or dangle",
             ("error", "info"), "TraversalOrder"),
    RuleSpec("a11y.text.too_small", "R18", "Text rendered below 12sp",
             ("warn",)),
)

RULES_BY_ID: Dict[str, RuleSpec] = {s.id: s for s in RULE_SPECS}
ALL_RULE_IDS: List[str] = [s.id for s in RULE_SPECS]
# Every accepted spelling (canonical ids, R-aliases, ATF check names); used for the
# MCP schema enum and the CLI help. Lookups are case-insensitive.
RULE_CHOICES: List[str] = (
    ALL_RULE_IDS + [s.alias for s in RULE_SPECS] + [s.atf for s in RULE_SPECS if s.atf]
)
_RULE_LOOKUP: Dict[str, str] = {}
for _s in RULE_SPECS:
    _RULE_LOOKUP[_s.id.lower()] = _s.id
    _RULE_LOOKUP[_s.alias.lower()] = _s.id
    if _s.atf:
        _RULE_LOOKUP[_s.atf.lower()] = _s.id
        _RULE_LOOKUP[(_s.atf + "check").lower()] = _s.id


class UnknownRuleError(ValueError):
    """Raised by :func:`resolve_rule_ids` for ids that name no rule."""

    def __init__(self, unknown: Sequence[str]):
        self.unknown = list(unknown)
        valid = ", ".join(f"{s.alias}={s.id}" for s in RULE_SPECS)
        super().__init__(
            "unknown a11y lint rule id(s): " + ", ".join(repr(u) for u in self.unknown)
            + ". Valid rules (alias=id): " + valid
            + ". ATF check names (e.g. 'TouchTargetSize') are accepted too."
        )


def resolve_rule_ids(rules: Optional[Iterable[str]]) -> Optional[Set[str]]:
    """Map rule ids / aliases / ATF names to canonical ids. ``None``/empty -> None (all).

    Comma-separated values are split. Raises :class:`UnknownRuleError` listing the
    unknown ids and every valid one.
    """
    if rules is None:
        return None
    if isinstance(rules, str):
        rules = [rules]
    out: Set[str] = set()
    unknown: List[str] = []
    for r in rules:
        for part in str(r).split(","):
            p = part.strip()
            if not p:
                continue
            rid = _RULE_LOOKUP.get(p.lower())
            if rid is None:
                unknown.append(p)
            else:
                out.add(rid)
    if unknown:
        raise UnknownRuleError(unknown)
    return out or None


# --------------------------------------------------------------------------- #
# Context / result types.
# --------------------------------------------------------------------------- #
@dataclass
class WindowImage:
    """A decoded screenshot of one window root (row-major RGBA8888).

    ``origin_x/origin_y`` is the window root's on-screen position; node bounds
    (screen px) map to pixels as ``(x - origin_x) * scale``.
    """

    w: int
    h: int
    rgba: bytes
    scale: float = 1.0
    origin_x: int = 0
    origin_y: int = 0
    root_view_id: Optional[int] = None


@dataclass
class LintContext:
    """Everything a rule needs that is not in the tree itself."""

    density: int = DEFAULT_DENSITY  # device DPI; dp = px / (density/160)
    font_scale: float = 1.0         # system font scale (R11 / R18)
    # Legacy single screenshot (window-relative, origin 0,0).
    screenshot_rgba: Optional[bytes] = None
    screenshot_w: int = 0
    screenshot_h: int = 0
    screenshot_scale: float = 1.0
    # optional render_node_id -> (w, h, rgba) for a component-image contrast sample
    component_image_fn: Optional[Callable[[int], Optional[Tuple[int, int, bytes]]]] = None
    wcag_mode: bool = False         # WCAG 44dp target instead of Material 48dp
    # root_view_id -> screenshot of that window (preferred over the legacy image).
    window_images: Dict[int, WindowImage] = field(default_factory=dict)
    # When set, pixel rules (R3) run only on the nodes with these keys (inspect_node's
    # dossier lint: the same computation as a11y_lint, for one element).
    image_keys: Optional[Set[str]] = None
    # Filled by the lint: rule crashes, skipped work, identity problems.
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def has_image(self) -> bool:
        return bool(self.window_images) or (
            self.screenshot_rgba is not None and self.screenshot_w > 0)

    @property
    def dpi(self) -> float:
        return float(self.density) if self.density and self.density > 0 else float(DEFAULT_DENSITY)

    def legacy_image(self) -> Optional[WindowImage]:
        if self.screenshot_rgba is None or self.screenshot_w <= 0:
            return None
        return WindowImage(self.screenshot_w, self.screenshot_h, self.screenshot_rgba,
                           float(self.screenshot_scale or 1.0), 0, 0, None)

    def diag(self, code: str, message: str, level: str = "info", **extra: Any) -> None:
        d = {"level": level, "code": code, "message": message}
        d.update(extra)
        self.diagnostics.append(d)


@dataclass
class Finding:
    rule: str                # canonical rule id, e.g. "a11y.label.missing"
    severity: str            # "error" | "warn" | "info"
    node: Dict[str, Any]     # {id, key, name, role, label, class_name, source, ...}
    bounds: Dict[str, int]   # screen px
    bounds_dp: Dict[str, float]
    message: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    needs_image: bool = False
    window: Optional[Dict[str, Any]] = None
    collection: Optional[Dict[str, Any]] = None

    @property
    def node_key(self) -> Optional[str]:
        return self.node.get("key")

    @property
    def alias(self) -> Optional[str]:
        spec = RULES_BY_ID.get(self.rule)
        return spec.alias if spec else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule": self.rule,
            "alias": self.alias,
            "severity": self.severity,
            "node_key": self.node_key,
            "node": self.node,
            "bounds": self.bounds,
            "bounds_dp": self.bounds_dp,
            "window": self.window,
            "collection": self.collection,
            "message": self.message,
            "evidence": self.evidence,
            "needs_image": self.needs_image,
        }


# --------------------------------------------------------------------------- #
# Normalised node model shared by both inputs.
# --------------------------------------------------------------------------- #
_ROLE_BY_CLASS = {
    "Button": "Button", "MaterialButton": "Button", "AppCompatButton": "Button",
    "ImageButton": "Button", "AppCompatImageButton": "Button",
    "FloatingActionButton": "Button", "ExtendedFloatingActionButton": "Button",
    "CheckBox": "Checkbox", "AppCompatCheckBox": "Checkbox", "MaterialCheckBox": "Checkbox",
    "CheckedTextView": "Checkbox",
    "Switch": "Switch", "SwitchCompat": "Switch", "SwitchMaterial": "Switch",
    "MaterialSwitch": "Switch", "ToggleButton": "Switch", "CompoundButton": "Switch",
    "RadioButton": "RadioButton", "AppCompatRadioButton": "RadioButton",
    "MaterialRadioButton": "RadioButton",
    "ImageView": "Image", "AppCompatImageView": "Image", "ShapeableImageView": "Image",
    "Spinner": "DropdownList", "AppCompatSpinner": "DropdownList",
    "SeekBar": "Slider", "AppCompatSeekBar": "Slider", "RatingBar": "Slider", "Slider": "Slider",
}
# Compose Role -> the className Compose reports for it (compose-adapter only).
_CLASS_BY_ROLE = {
    "Button": "android.widget.Button", "Checkbox": "android.widget.CheckBox",
    "Switch": "android.widget.Switch", "RadioButton": "android.widget.RadioButton",
    "Image": "android.widget.ImageView", "DropdownList": "android.widget.Spinner",
    "Tab": "android.view.View",
}
_IMAGE_CLASSES = {"ImageView", "AppCompatImageView", "ShapeableImageView", "ImageButton",
                  "AppCompatImageButton", "FloatingActionButton"}
_EDIT_CLASSES = {"EditText", "AppCompatEditText", "TextInputEditText",
                 "AutoCompleteTextView", "AppCompatAutoCompleteTextView",
                 "MultiAutoCompleteTextView"}
_COLLECTION_CLASSES = {"RecyclerView", "ListView", "GridView", "ExpandableListView",
                       "ViewPager", "ViewPager2", "StaggeredGridView"}
_STATEFUL_ROLES = {"Switch", "Checkbox", "RadioButton", "Tab"}
_KNOWN_ROLE_DESC = {"tab": "Tab", "switch": "Switch", "checkbox": "Checkbox",
                    "check box": "Checkbox", "radio button": "RadioButton",
                    "button": "Button", "image": "Image", "link": "Link",
                    "dropdown list": "DropdownList", "slider": "Slider"}
_TEXT_UNITS = {0: "px", 1: "dp", 2: "sp", 3: "pt", 4: "in", 5: "mm"}
_HIDE_SELF = {"NO", 2}
_HIDE_TREE = {"NO_HIDE_DESCENDANTS", 4}


class _Win:
    __slots__ = ("index", "root_view_id", "x", "y", "w", "h", "root", "covered_by")

    def __init__(self, index: int, root_view_id: Optional[int], rect: Dict[str, int]):
        self.index = index
        self.root_view_id = root_view_id
        self.x = int(rect.get("x", 0))
        self.y = int(rect.get("y", 0))
        self.w = int(rect.get("w", 0))
        self.h = int(rect.get("h", 0))
        self.root: Optional["_Node"] = None
        # root_view_id of the modal window (dialog) above this one: TalkBack cannot reach
        # this window while it is open. Findings here are still real, but not reachable now.
        self.covered_by: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"index": self.index, "root_view_id": self.root_view_id}
        if self.covered_by is not None:
            d["covered_by"] = self.covered_by
        return d


class _Node:
    """One accessibility node, normalised from either input shape."""

    __slots__ = (
        "raw", "origin", "key", "a11y_id", "host_view_id", "virtual_id", "win",
        "x", "y", "w", "h", "class_name", "simple_class", "compose_role", "role_description",
        "text", "cd", "hint", "state", "flags", "actions", "extras", "important",
        "hidden", "ignored", "collection_info", "collection_item_info", "labeled_by", "label_for",
        "traversal_before", "traversal_after", "text_size_px", "text_size_unit",
        "test_tag", "source", "compose", "compose_label", "provider_class",
        "render_node_id", "range_info", "children", "parent", "depth", "order",
        "focus_ancestor", "collection_ctx", "clip", "layout_w", "layout_h",
    )

    def __init__(self) -> None:
        self.raw: Dict[str, Any] = {}
        self.origin = "a11y"
        self.key = ""
        self.a11y_id: Optional[int] = None
        self.host_view_id: Optional[int] = None
        self.virtual_id: Optional[int] = None
        self.win: Optional[_Win] = None
        self.x = self.y = self.w = self.h = 0
        self.class_name = ""
        self.simple_class = ""
        self.compose_role: Optional[str] = None
        self.role_description = ""
        self.text = self.cd = self.hint = self.state = ""
        self.flags: Set[str] = set()
        self.actions: Set[str] = set()
        self.extras: Dict[str, str] = {}
        self.important: Any = None
        # hidden: TalkBack never sees this node or its subtree (noHideDescendants on it or
        # an ancestor, InvisibleToUser). ignored: a real View that is not important for
        # accessibility; TalkBack skips it but reads its children in its place.
        self.hidden = False
        self.ignored = False
        self.collection_info: Optional[Dict[str, Any]] = None
        self.collection_item_info: Optional[Dict[str, Any]] = None
        self.labeled_by: List[int] = []
        self.label_for = 0
        self.traversal_before = 0
        self.traversal_after = 0
        self.text_size_px = 0.0
        self.text_size_unit: Optional[int] = None
        self.test_tag: Optional[str] = None
        self.source: Optional[str] = None
        self.compose: Optional[Dict[str, Any]] = None
        self.compose_label = ""
        self.provider_class: Optional[str] = None
        self.render_node_id: Optional[int] = None
        self.range_info: Optional[Dict[str, Any]] = None
        self.children: List["_Node"] = []
        self.parent: Optional["_Node"] = None
        self.depth = 0
        self.order = 0
        self.focus_ancestor: Optional["_Node"] = None
        self.collection_ctx: Optional[Tuple["_Node", "_Node"]] = None
        self.clip: Optional["_Node"] = None
        # Compose virtual nodes: the LayoutNode's measured size (px) from the agent's
        # layout_size; 0 = unknown. Their bounds are touch bounds (widened to 48dp).
        self.layout_w = 0
        self.layout_h = 0

    # -- geometry ---------------------------------------------------------- #
    @property
    def rect(self) -> Dict[str, int]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @property
    def own_label(self) -> str:
        return self.cd or self.text or self.state

    @property
    def kind(self) -> str:
        k = self.key or ""
        if k.startswith("view:"):
            return "view"
        if k.startswith("virtual:"):
            return "virtual"
        return "compose"


def _clean(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if s.lower() in ("null", "[]"):
        return ""
    return s


def _simple(cls: str) -> str:
    return (cls or "").rsplit(".", 1)[-1].split("$")[-1]


def _rect_of(d: Dict[str, Any]) -> Dict[str, int]:
    b = (d.get("bounds") or {}).get("layout") or {}
    return {"x": int(b.get("x", 0) or 0), "y": int(b.get("y", 0) or 0),
            "w": int(b.get("w", 0) or 0), "h": int(b.get("h", 0) or 0)}


def a11y_node_id(host_view_id: int, virtual_id: int) -> int:
    """The host a11y node id: ``(host_view_id << 32) ^ (virtual_id & 0xFFFFFFFF)``."""
    return (int(host_view_id) << 32) ^ (int(virtual_id) & 0xFFFFFFFF)


# --------------------------------------------------------------------------- #
# Builders.
# --------------------------------------------------------------------------- #
def _compose_index(compose_data: Optional[Dict[str, Any]]
                   ) -> Tuple[Dict[Tuple[int, int], Dict[str, Any]], Set[int], Dict[int, int]]:
    """(acv, semId) -> semantics node dict; the set of AndroidComposeView ids; and
    semId -> acv for semantics ids that are unique across windows."""
    index: Dict[Tuple[int, int], Dict[str, Any]] = {}
    hosts: Set[int] = set()
    sem_owner: Dict[int, int] = {}
    sem_dupes: Set[int] = set()
    if not compose_data:
        return index, hosts, {}
    for w in compose_data.get("windows") or []:
        acv = int(w.get("view_id") or 0)
        root = w.get("root")
        if not root:
            continue
        hosts.add(acv)
        first_sem = None
        stack = [root]
        while stack:
            n = stack.pop()
            if n.get("kind") == "SEMANTICS" and n.get("id") is not None:
                sid = int(n["id"])
                index[(acv, sid)] = n
                if first_sem is None:
                    first_sem = n
                if sid in sem_owner and sem_owner[sid] != acv:
                    sem_dupes.add(sid)
                sem_owner.setdefault(sid, acv)
            stack.extend(reversed(n.get("children") or []))
        if first_sem is not None:
            index.setdefault((acv, -1), first_sem)
    unique = {s: a for s, a in sem_owner.items() if s not in sem_dupes}
    return index, hosts, unique


def _compose_label(attrs: Dict[str, Any], with_state: bool = True) -> str:
    keys = ("ContentDescription", "Text", "StateDescription") if with_state else (
        "ContentDescription", "Text")
    for k in keys:
        v = _clean(attrs.get(k))
        if v:
            return v
    return ""


def _apply_compose_detail(n: _Node, cnode: Dict[str, Any]) -> None:
    attrs = cnode.get("attrs") or {}
    n.compose = cnode
    role = _clean(attrs.get("Role"))
    if role:
        n.compose_role = role
    tag = _clean(attrs.get("TestTag"))
    if tag:
        n.test_tag = tag
    if cnode.get("source"):
        n.source = cnode.get("source")
    n.compose_label = _compose_label(attrs)


def _virtual_prefix(parent: Optional[_Node], host: int, compose_hosts: Set[int]) -> str:
    if host in compose_hosts:
        return "compose"
    p = parent
    while p is not None:
        if p.host_view_id == host and p.virtual_id == -1:
            marker = (p.provider_class or "") + " " + p.class_name
            if "Compose" in marker:
                return "compose"
            if p.provider_class or "WebView" in marker:
                return "virtual"
            break
        p = p.parent
    return "compose"  # Compose is by far the most common virtual-node provider


def _build_a11y(a11y_data: Dict[str, Any], compose_data: Optional[Dict[str, Any]],
                ctx: LintContext) -> Tuple[List[_Node], List[_Win], Dict[str, int]]:
    index, hosts, _ = _compose_index(compose_data)
    stats = {"compose_joined": 0, "virtual_nodes": 0}
    windows: List[_Win] = []
    roots: List[_Node] = []

    def mk(d: Dict[str, Any], parent: Optional[_Node], win: _Win, hidden_tree: bool) -> _Node:
        n = _Node()
        n.raw = d
        n.origin = "a11y"
        n.parent = parent
        n.win = win
        hv = int(d.get("host_view_id") or 0)
        vid_raw = d.get("virtual_id")
        vid = -1 if vid_raw is None else int(vid_raw)
        n.host_view_id, n.virtual_id = hv, vid
        n.a11y_id = int(d["id"]) if d.get("id") is not None else a11y_node_id(hv, vid)
        r = _rect_of(d)
        n.x, n.y, n.w, n.h = r["x"], r["y"], r["w"], r["h"]
        n.class_name = d.get("class_name") or ""
        n.simple_class = _simple(n.class_name)
        n.provider_class = d.get("provider_class")
        n.role_description = _clean(d.get("role_description"))
        n.text = _clean(d.get("text"))
        n.cd = _clean(d.get("content_description"))
        n.hint = _clean(d.get("hint_text"))
        n.state = _clean(d.get("state_description"))
        n.flags = set(d.get("flags") or ())
        n.actions = {str(a.get("name")) for a in (d.get("actions") or []) if isinstance(a, dict)}
        n.extras = dict(d.get("extras") or {})
        n.important = d.get("important_for_accessibility")
        why = _talkback_exclusion(d, parent.raw if parent is not None else None)
        n.hidden = hidden_tree or why == "hidden"
        n.ignored = why == "not_important"
        n.collection_info = d.get("collection_info")
        n.collection_item_info = d.get("collection_item_info")
        n.range_info = d.get("range_info")
        lb = [int(x) for x in (d.get("labeled_by_list") or []) if x]
        if d.get("labeled_by"):
            lb.insert(0, int(d["labeled_by"]))
        n.labeled_by = lb
        n.label_for = int(d.get("label_for") or 0)
        n.traversal_before = int(d.get("traversal_before") or 0)
        n.traversal_after = int(d.get("traversal_after") or 0)
        if vid != -1:
            ls = d.get("layout_size") or {}
            n.layout_w, n.layout_h = int(ls.get("w") or 0), int(ls.get("h") or 0)
        tsp = d.get("text_size_px")
        n.text_size_px = float(tsp) if tsp else 0.0
        if n.text_size_px > 0:
            n.text_size_unit = int(d.get("text_size_unit") or 0)  # 0 (px) is omitted
        # typed key
        given = d.get("node_key") or d.get("key")
        if isinstance(given, str) and ":" in given:
            n.key = given
        elif vid == -1:
            n.key = f"view:{hv}"
        else:
            stats["virtual_nodes"] += 1
            n.key = f"{_virtual_prefix(parent, hv, hosts)}:{hv}:{vid}"
        # Compose extra detail
        cnode = index.get((hv, vid)) if index else None
        if cnode is not None and (vid != -1 or hv in hosts):
            _apply_compose_detail(n, cnode)
            stats["compose_joined"] += 1
        n.children = [mk(c, n, win, n.hidden) for c in (d.get("children") or [])]
        if vid != -1 and n.children:
            _fold_compose_fakes(n)
        return n

    for i, w in enumerate(a11y_data.get("windows") or []):
        root = w.get("root")
        if not root:
            continue
        win = _Win(i, w.get("root_view_id"), _rect_of(root))
        if w.get("covered_by") is not None:
            win.covered_by = int(w["covered_by"])
        rn = mk(root, None, win, False)
        win.root = rn
        windows.append(win)
        roots.append(rn)
    return roots, windows, stats


# Compose (ui 1.7+) serves a merging node's Role and contentDescription on synthetic
# children, ids semId + 1e9 and semId + 2e9, so TalkBack reads the description before the
# children. They name/type their parent; they are not elements of their own.
_FAKE_ROLE_OFFSET = 1_000_000_000
_FAKE_CD_OFFSET = 2_000_000_000


def _fold_compose_fakes(n: _Node) -> None:
    """Move a Compose node's synthetic role/contentDescription children into the node."""
    kept = []
    for c in n.children:
        if c.host_view_id == n.host_view_id and not c.children:
            if c.virtual_id == n.virtual_id + _FAKE_CD_OFFSET:
                n.cd = n.cd or c.cd
                continue
            if c.virtual_id == n.virtual_id + _FAKE_ROLE_OFFSET:
                # The role rides on the class name (Button, Checkbox, ...) or, for Tab and
                # Switch, on roleDescription with class android.view.View.
                n.compose_role = (n.compose_role or _ROLE_BY_CLASS.get(c.simple_class)
                                  or _KNOWN_ROLE_DESC.get(c.role_description.lower()))
                continue
        kept.append(c)
    n.children = kept


def _build_compose(roots_in: List[Dict[str, Any]], ctx: LintContext
                   ) -> Tuple[List[_Node], List[_Win], Dict[str, int]]:
    """Legacy adapter: Compose (merged) semantics roots -> normalised nodes."""
    roots: List[_Node] = []
    windows: List[_Win] = []

    def flags_of(a: Dict[str, Any]) -> Set[str]:
        f: Set[str] = set()
        if "InvisibleToUser" not in a and "HideFromAccessibility" not in a:
            f.add("visible_to_user")
        if "Disabled" not in a:
            f.add("enabled")
        if "OnClick" in a:
            f.update(("clickable", "focusable"))
        if "OnLongClick" in a:
            f.add("long_clickable")
        if "SetText" in a or "EditableText" in a:
            f.add("editable")
        if "RequestFocus" in a or "Focused" in a:
            f.add("focusable")
        if "ToggleableState" in a:
            f.add("checkable")
            if _clean(a.get("ToggleableState")).lower() == "on":
                f.add("checked")
        if _clean(a.get("Selected")).lower() == "true":
            f.add("selected")
        if "Heading" in a:
            f.add("heading")
        if any(k in a for k in ("ScrollBy", "VerticalScrollAxisRange", "HorizontalScrollAxisRange")):
            f.add("scrollable")
        if _compose_label(a):
            # In the merged tree every node still carrying a label is its own stop.
            f.add("screen_reader_focusable")
        return f

    def mk(d: Dict[str, Any], parent: Optional[_Node], win: _Win, acv: Optional[int]) -> _Node:
        n = _Node()
        n.raw = d
        n.origin = "compose"
        n.parent = parent
        n.win = win
        a = d.get("attrs") or {}
        sid = d.get("id")
        n.a11y_id = int(sid) if sid is not None else None
        n.virtual_id = n.a11y_id
        n.host_view_id = acv
        n.key = f"compose:{acv}:{sid}" if acv else f"compose:{sid}"
        r = _rect_of(d)
        n.x, n.y, n.w, n.h = r["x"], r["y"], r["w"], r["h"]
        role = _clean(a.get("Role")) or None
        n.compose_role = role
        editable = "SetText" in a or "EditableText" in a
        if role:
            n.class_name = _CLASS_BY_ROLE.get(role, "android.view.View")
        elif editable:
            n.class_name = "android.widget.EditText"
        elif _clean(a.get("Text")):
            n.class_name = "android.widget.TextView"
        else:
            n.class_name = "android.view.View"
        n.simple_class = _simple(n.class_name)
        n.text = _clean(a.get("Text"))
        if editable and _clean(a.get("EditableText")) and not n.text:
            n.text = _clean(a.get("EditableText"))
        n.cd = _clean(a.get("ContentDescription"))
        n.state = _clean(a.get("StateDescription"))
        n.flags = flags_of(a)
        n.hidden = "InvisibleToUser" in a or "HideFromAccessibility" in a
        if "CollectionInfo" in a:
            n.collection_info = {"raw": a.get("CollectionInfo")}
        if "CollectionItemInfo" in a:
            n.collection_item_info = {"raw": a.get("CollectionItemInfo")}
        if "ProgressBarRangeInfo" in a:
            n.range_info = {"raw": a.get("ProgressBarRangeInfo")}
        n.test_tag = _clean(a.get("TestTag")) or None
        n.source = d.get("source")
        n.compose = d
        n.render_node_id = d.get("render_node_id")
        kids: List[_Node] = []
        for c in d.get("children") or []:
            if c.get("kind") == "COMPOSABLE":
                # slot-table / window wrapper: lift its semantics descendants
                kids.extend(lift(c, n, win, acv))
            else:
                kids.append(mk(c, n, win, acv))
        n.children = kids
        return n

    def lift(d: Dict[str, Any], parent: Optional[_Node], win: _Win, acv: Optional[int]) -> List[_Node]:
        out: List[_Node] = []
        for c in d.get("children") or []:
            if c.get("kind") == "COMPOSABLE":
                out.extend(lift(c, parent, win, acv))
            else:
                out.append(mk(c, parent, win, acv))
        return out

    for i, r in enumerate(roots_in):
        if not r:
            continue
        acv: Optional[int] = None
        if r.get("kind") == "COMPOSABLE" and r.get("name") == "AndroidComposeView":
            acv = int(r.get("id") or 0) or None
        win = _Win(i, acv, _rect_of(r))
        if r.get("kind") == "COMPOSABLE":
            lifted = lift(r, None, win, acv)
            roots.extend(lifted)
            win.root = lifted[0] if lifted else None
        else:
            rn = mk(r, None, win, acv)
            roots.append(rn)
            win.root = rn
        windows.append(win)
    return roots, windows, {"compose_joined": 0, "virtual_nodes": 0}


# --------------------------------------------------------------------------- #
# Predicates.
# --------------------------------------------------------------------------- #
def _on_screen(n: _Node) -> bool:
    """Visible to the user and not hidden from TalkBack with its subtree."""
    return (not n.hidden) and ("visible_to_user" in n.flags) and n.w > 0 and n.h > 0


def _visible(n: _Node) -> bool:
    """On screen and a node TalkBack sees (not a View that is not important for a11y)."""
    return _on_screen(n) and not n.ignored


def _talkback_exclusion(d: Dict[str, Any], parent: Optional[Dict[str, Any]]) -> Optional[str]:
    from .a11y import talkback_exclusion
    return talkback_exclusion(d, parent)


def _enabled(n: _Node) -> bool:
    return "enabled" in n.flags


def _actionable(n: _Node) -> bool:
    return bool({"clickable", "long_clickable"} & n.flags) or bool({"CLICK", "LONG_CLICK"} & n.actions)


def _editable(n: _Node) -> bool:
    return "editable" in n.flags or n.simple_class in _EDIT_CLASSES


def _focus_candidate(n: _Node) -> bool:
    """Would TalkBack give this node its own accessibility focus (if it has content)?

    Mirrors TalkBack's "accessibility focusable": actionable (clickable, long-
    clickable, focusable) or screenReaderFocusable. A checkable that is none of
    these (a Compose Checkbox inside a toggleable Row) is read as part of its row."""
    return _actionable(n) or bool(
        {"focusable", "screen_reader_focusable", "editable"} & n.flags)


def _has_state(n: _Node) -> bool:
    if {"checkable", "checked", "selected"} & n.flags:
        return True
    if n.state or n.range_info or n.raw.get("checked_state"):
        return True
    a = (n.compose or {}).get("attrs") or {}
    return "ToggleableState" in a or "Selected" in a or "StateDescription" in a


def _is_collection(n: _Node) -> bool:
    return n.collection_info is not None or n.simple_class in _COLLECTION_CLASSES


def _is_image(n: _Node, role: Optional[str]) -> bool:
    return role == "Image" or n.simple_class in _IMAGE_CLASSES


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (s or "").lower()).split())


def _norm_label(s: str) -> str:
    return " ".join((s or "").lower().split())


# --------------------------------------------------------------------------- #
# One lint run over normalised nodes.
# --------------------------------------------------------------------------- #
class _Run:
    def __init__(self, roots: List[_Node], windows: List[_Win], ctx: LintContext,
                 enabled: Optional[Set[str]], mode: str):
        self.roots = roots
        self.windows = windows
        self.ctx = ctx
        self.enabled = enabled
        self.mode = mode
        self.nodes: List[_Node] = []
        self.by_id: Dict[int, _Node] = {}
        self.label_for_targets: Set[int] = set()
        self._label_memo: Dict[Tuple[int, bool], Tuple[str, str]] = {}
        self._stop_memo: Dict[int, bool] = {}
        self._ro_stops: Optional[Set[int]] = None
        self._cand_desc: Dict[int, bool] = {}
        self.stats: Dict[str, int] = {}
        self.identity_ok = True
        self._index()

    def stats_inc(self, key: str, by: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + by

    # -- indexing ---------------------------------------------------------- #
    def _index(self) -> None:
        order = 0
        stack: List[Tuple[_Node, int]] = [(r, 0) for r in reversed(self.roots)]
        while stack:
            n, depth = stack.pop()
            n.depth = depth
            n.order = order
            order += 1
            self.nodes.append(n)
            p = n.parent
            if p is not None:
                n.focus_ancestor = (p if (_focus_candidate(p) and not p.hidden and not p.ignored)
                                    else p.focus_ancestor)
                if _is_collection(p):
                    n.collection_ctx = (p, n)
                else:
                    n.collection_ctx = p.collection_ctx
                n.clip = p if ("scrollable" in p.flags or _is_collection(p)) else p.clip
            else:
                n.clip = None
            for c in reversed(n.children):
                stack.append((c, depth + 1))
        seen: Dict[int, int] = {}
        dup = 0
        for n in self.nodes:
            if n.a11y_id is None:
                continue
            if n.a11y_id in seen:
                dup += 1
            else:
                seen[n.a11y_id] = 1
                self.by_id[n.a11y_id] = n
            if n.label_for:
                self.label_for_targets.add(n.label_for)
        # bottom-up: does any visible descendant take its own focus?
        for n in reversed(self.nodes):
            self._cand_desc[id(n)] = any(
                (not c.hidden and not c.ignored and "visible_to_user" in c.flags
                 and _focus_candidate(c))
                or self._cand_desc.get(id(c), False) for c in n.children)
        if self.mode == "a11y" and len(self.nodes) > 4 and dup > len(self.nodes) * 0.1:
            self.identity_ok = False
            joined = sum(1 for n in self.nodes if n.compose is not None)
            for n in self.nodes:  # a join on colliding ids would attach the wrong detail
                n.compose, n.compose_role, n.compose_label = None, None, ""
                n.test_tag = None
            self.ctx.diag(
                "identity.degenerate",
                f"{dup} of {len(self.nodes)} a11y nodes share a (host_view_id, virtual_id) with "
                "another node: the agent predates the a11y identity fix, so node keys are not "
                "unique. Select findings by bounds; the Compose detail join"
                f"{' (' + str(joined) + ' nodes)' if joined else ''} was dropped and "
                "linkage-based checks (R17 dangling targets) are skipped.",
                level="warn")

    def on(self, rid: str) -> bool:
        return self.enabled is None or rid in self.enabled

    # -- derived facts ------------------------------------------------------ #
    def dp(self, px: float) -> float:
        return round(px / (self.ctx.dpi / DP_BASE), 1)

    def bounds_pair(self, n: _Node) -> Tuple[Dict[str, int], Dict[str, float]]:
        bx = n.rect
        return bx, {k: self.dp(v) for k, v in bx.items()}

    def role(self, n: _Node) -> Optional[str]:
        if n.compose_role:
            return n.compose_role
        if n.role_description:
            return _KNOWN_ROLE_DESC.get(n.role_description.lower(), n.role_description)
        return _ROLE_BY_CLASS.get(n.simple_class)

    def owner(self, n: _Node) -> _Node:
        """The node TalkBack focuses to speak ``n`` (itself or its focusable ancestor)."""
        if (_focus_candidate(n) and not n.ignored) or n.focus_ancestor is None:
            return n
        return n.focus_ancestor

    def _collect_desc(self, n: _Node, parts: List[str], depth: int = 0,
                      include_offscreen: bool = False, with_state: bool = True) -> None:
        if depth > 64:
            return
        for c in n.children:
            if c.hidden or ("visible_to_user" not in c.flags and not include_offscreen):
                continue
            if c.ignored:  # not important for a11y: TalkBack reads its children instead
                self._collect_desc(c, parts, depth + 1, include_offscreen, with_state)
                continue
            if _focus_candidate(c):
                continue  # its own focus stop; TalkBack does not fold it into the parent
            if c.cd:
                parts.append(c.cd)
                continue  # a contentDescription replaces the subtree
            if c.text:
                parts.append(c.text)
            if c.state and with_state:
                parts.append(c.state)
            self._collect_desc(c, parts, depth + 1, include_offscreen, with_state)

    def effective_label(self, n: _Node, with_state: bool = True) -> Tuple[str, str]:
        """(label, source) as TalkBack would compute it for a focused ``n``.

        ``with_state=False`` gives the accessible *name* only: a stateDescription
        ("On", "Checked", Compose's computed toggle state) says how a control is, not
        what it is, so a bare Switch that speaks only "On, switch" has no name (R1).
        """
        k = (id(n), with_state)
        if k in self._label_memo:
            return self._label_memo[k]
        res: Tuple[str, str] = ("", "")
        own = n.own_label if with_state else (n.cd or n.text)
        if own:
            res = (own, "own")
        else:
            parts: List[str] = []
            self._collect_desc(n, parts, with_state=with_state)
            if parts:
                res = (", ".join(parts), "descendants")
            else:
                for t in n.labeled_by:
                    tgt = self.by_id.get(t)
                    tgt_label = (tgt.own_label if with_state else (tgt.cd or tgt.text)) if tgt else ""
                    if tgt is not None and (tgt_label or tgt.hint):
                        res = (tgt_label or tgt.hint, "labeled_by")
                        break
                else:
                    compose_label = n.compose_label if with_state else _compose_label(
                        (n.compose or {}).get("attrs") or {}, with_state=False)
                    if n.labeled_by:
                        res = ("<labeledBy target not in tree>", "labeled_by_unresolved")
                    elif compose_label:
                        res = (compose_label, "compose_merged")
                    else:
                        # Children scrolled/clipped out of view still name the node
                        # once TalkBack scrolls it in; don't call it unlabelled.
                        off: List[str] = []
                        self._collect_desc(n, off, include_offscreen=True, with_state=with_state)
                        if off:
                            res = (", ".join(off), "offscreen_descendants")
        self._label_memo[k] = res
        return res

    def desc_text(self, n: _Node) -> str:
        """The visible text ``n``'s non-focusable descendants contribute (no descriptions)."""
        parts: List[str] = []
        stack = list(reversed(n.children))
        while stack:
            c = stack.pop()
            if c.hidden or "visible_to_user" not in c.flags:
                continue
            if c.ignored:
                stack.extend(reversed(c.children))
                continue
            if _focus_candidate(c):
                continue
            if c.text:
                parts.append(c.text)
            if not c.cd:
                stack.extend(reversed(c.children))
        return " ".join(parts)

    def has_candidate_descendant(self, n: _Node) -> bool:
        return self._cand_desc.get(id(n), False)

    def desc_has_text(self, n: _Node) -> bool:
        """Does a non-focusable visible descendant contribute visible *text* (not just a
        contentDescription) to ``n``'s label?"""
        stack = list(n.children)
        while stack:
            c = stack.pop()
            if c.hidden or "visible_to_user" not in c.flags:
                continue
            if c.ignored:
                stack.extend(c.children)
                continue
            if _focus_candidate(c):
                continue
            if c.text:
                return True
            if not c.cd:
                stack.extend(c.children)
        return False

    def is_stop(self, n: _Node) -> bool:
        """Is ``n`` a TalkBack focus stop? On the unified a11y tree this is exactly the
        reading order's stop set (:func:`a11y.reading_order`: the tree TalkBack sees,
        top-level scroll items, focusable containers that do not swallow their content);
        the legacy Compose-semantics input keeps the approximation below."""
        if self.mode == "a11y":
            if self._ro_stops is None:
                from .a11y import reading_order
                ro = reading_order([r.raw for r in self.roots])
                self._ro_stops = {id(x) for x in ro["_nodes"]}
            return id(n.raw) in self._ro_stops
        k = id(n)
        if k in self._stop_memo:
            return self._stop_memo[k]
        if not _visible(n):
            r = False
        elif _focus_candidate(n):
            r = True
            if not _actionable(n) and not n.own_label and not _editable(n):
                # A focusable container whose content lives in focusable children
                # (e.g. a keyboard-focusable ScrollView) is not a TalkBack stop.
                if "scrollable" in n.flags or _is_collection(n) or self.has_candidate_descendant(n):
                    r = False
        else:
            r = bool(n.own_label) and n.focus_ancestor is None
        self._stop_memo[k] = r
        return r

    def collection_ref(self, n: _Node) -> Optional[Dict[str, Any]]:
        cc = n.collection_ctx
        if cc is None:
            return None
        container, row = cc
        info = row.collection_item_info or n.collection_item_info or {}
        idx = info.get("row_index")
        if idx is None:
            vis = [c for c in container.children if not c.hidden]
            idx = vis.index(row) if row in vis else None
        return {"container": container.key, "row": row.key, "row_index": idx,
                "column_index": info.get("column_index")}

    def node_ref(self, n: _Node) -> Dict[str, Any]:
        label, _ = self.effective_label(n)
        ref: Dict[str, Any] = {
            "id": n.a11y_id,
            "key": n.key,
            "name": n.test_tag or n.simple_class or None,
            "role": self.role(n),
            "label": label or None,
            "class_name": n.class_name or None,
            "source": n.source,
        }
        if n.host_view_id is not None:
            ref["host_view_id"] = n.host_view_id
        if n.virtual_id is not None:
            ref["virtual_id"] = n.virtual_id
        if n.test_tag:
            ref["test_tag"] = n.test_tag
        return ref

    def finding(self, rule: str, severity: str, n: _Node, message: str,
                evidence: Optional[Dict[str, Any]] = None, needs_image: bool = False) -> Finding:
        bx, bdp = self.bounds_pair(n)
        return Finding(rule, severity, self.node_ref(n), bx, bdp, message, evidence or {},
                       needs_image, n.win.to_dict() if n.win else None, self.collection_ref(n))


# --------------------------------------------------------------------------- #
# R1 -- missing label on an actionable element.
# --------------------------------------------------------------------------- #
def rule_missing_label(n: _Node, run: _Run) -> List[Finding]:
    if not _visible(n) or not _actionable(n) or _editable(n):
        return []
    label, _ = run.effective_label(n, with_state=False)
    if label:
        return []
    role = run.role(n)
    what = role or n.simple_class or "element"
    toggle = role in ("Checkbox", "Switch", "RadioButton") or "checkable" in n.flags
    if n.kind == "view":
        fix = ("give it android:text, or point its visible label at it with android:labelFor"
               if toggle else
               "set android:contentDescription (icon-only controls such as ImageButton) or give "
               "it visible android:text")
    else:
        fix = ("make the Row holding its visible label toggleable/selectable (and pass "
               "onCheckedChange = null to the control), or add "
               "Modifier.semantics { contentDescription = \"...\" }"
               if toggle else
               "pass a contentDescription to the Icon/Image inside it, or add "
               "Modifier.semantics { contentDescription = \"...\" }")
    reason = sorted({"clickable", "long_clickable"} & n.flags) or sorted({"CLICK", "LONG_CLICK"} & n.actions)
    return [run.finding(
        "a11y.label.missing", "error", n,
        f"Actionable {what} has no accessible name (no text, contentDescription, "
        f"labeledBy or labelled non-focusable descendant; a stateDescription such as "
        f"\"On\" says its state, not what it is); TalkBack announces it only as "
        f"\"{(role or 'unlabelled').lower()}\". Fix: {fix}.",
        {"role": role, "class_name": n.class_name, "actionable_reason": reason,
         "checked": ["own", "descendants", "labeled_by", "compose_merged"]},
    )]


# --------------------------------------------------------------------------- #
# R2 -- touch target too small (on the a11y touch bounds).
# --------------------------------------------------------------------------- #
_EDGE_TOL = 2


_VSCROLL_CLASSES = {"ScrollView", "NestedScrollView", "ListView", "ExpandableListView"}
_HSCROLL_CLASSES = {"HorizontalScrollView"}


def _scroll_axes(c: _Node) -> Set[str]:
    """Dimensions a scroll container can clip: "h" for vertical, "w" for horizontal."""
    v = bool({"SCROLL_UP", "SCROLL_DOWN"} & c.actions)
    h = bool({"SCROLL_LEFT", "SCROLL_RIGHT"} & c.actions)
    if v != h:
        return {"h"} if v else {"w"}
    ci = c.collection_info or {}
    if ci.get("column_count") == 1:
        return {"h"}
    if ci.get("row_count") == 1:
        return {"w"}
    if c.simple_class in _VSCROLL_CLASSES:
        return {"h"}
    if c.simple_class in _HSCROLL_CLASSES:
        return {"w"}
    return {"h", "w"}


def _clipped_axes(n: _Node, run: _Run) -> Set[str]:
    """Dimensions in which ``n`` touches the edge of a scroll container (along its
    scroll axis) or runs into the window's right/bottom edge -- i.e. its bounds are
    probably clipped."""
    axes: Set[str] = set()
    c = n.clip
    while c is not None:
        can = _scroll_axes(c)
        if "h" in can and (n.y <= c.y + _EDGE_TOL or n.y + n.h >= c.y + c.h - _EDGE_TOL):
            axes.add("h")
        if "w" in can and (n.x <= c.x + _EDGE_TOL or n.x + n.w >= c.x + c.w - _EDGE_TOL):
            axes.add("w")
        c = c.clip
    win = n.win
    if win is not None and win.w > 0 and win.root is not n:
        if n.y + n.h >= win.y + win.h - _EDGE_TOL:
            axes.add("h")
        if n.x + n.w >= win.x + win.w - _EDGE_TOL:
            axes.add("w")
    return axes


def rule_touch_target(n: _Node, run: _Run) -> List[Finding]:
    if not _visible(n) or not _actionable(n) or not _enabled(n):
        return []
    role = run.role(n)
    # WCAG 2.5.8 inline exception: an unroled link inside a run of text.
    p = n.parent
    if role is None and p is not None and p.text and n.own_label and n.own_label in p.text:
        return []
    bx, bdp = run.bounds_pair(n)
    min_dp = 44 if run.ctx.wcag_mode else 48
    w_dp, h_dp = bdp["w"], bdp["h"]
    # A 48dp target lands on 116-118px at a fractional density (edges are rounded
    # separately), so allow 1px before calling a dimension small.
    min_px = min_dp * run.ctx.dpi / DP_BASE
    small = {ax for ax in ("w", "h") if bx[ax] + 1 < min_px}
    std = "wcag" if run.ctx.wcag_mode else "material"
    ev = {"w_dp": w_dp, "h_dp": h_dp, "min_dp": min_dp, "floor_dp": 24, "standard": std,
          "bounds_source": "a11y boundsInScreen (touch bounds)"}
    if not small and n.kind == "compose" and n.layout_w and n.layout_h:
        # Compose widens the a11y (touch) bounds of every clickable to 48dp and extends
        # hit-testing to match, but only a layout of that size reserves the area: without
        # it a neighbouring target or a clip takes the extra, and the visible control stays
        # small. Material controls reserve it (minimumInteractiveComponentSize), so their
        # LayoutNode is 48dp; a bare Modifier.size(24.dp).clickable is not.
        lay = {"w": n.layout_w, "h": n.layout_h}
        small = {ax for ax in ("w", "h") if lay[ax] + 1 < min_px}
        if not small:
            return []
        w_dp, h_dp = run.dp(lay["w"]), run.dp(lay["h"])
        ev.update({"w_dp": w_dp, "h_dp": h_dp, "touch_w_dp": bdp["w"], "touch_h_dp": bdp["h"],
                   "bounds_source": "Compose layout size (touch bounds are widened to 48dp)"})
        return [run.finding(
            "a11y.touch_target.small", "warn", n,
            f"Laid out at {w_dp}x{h_dp}dp. Compose widens its touch bounds to "
            f"{bdp['w']}x{bdp['h']}dp for hit-testing, but the layout does not reserve that "
            f"area, so a neighbouring target or a clip can take it and the visible control "
            f"stays small. Reserve {min_dp}dp: Modifier.minimumInteractiveComponentSize() or "
            f"Modifier.sizeIn(minWidth = {min_dp}.dp, minHeight = {min_dp}.dp) on the "
            f"clickable element.", ev)]
    if not small:
        return []
    clipped = _clipped_axes(n, run)
    if small <= clipped:
        ev["clipped_axes"] = sorted(clipped)
        return [run.finding(
            "a11y.touch_target.small", "info", n,
            f"Touch target measures {w_dp}x{h_dp}dp but touches the edge of its scroll "
            f"container/window, so it is probably clipped. Scroll it fully into view and "
            f"re-lint before acting on this.", ev)]
    real = {"w": w_dp, "h": h_dp}
    unclipped_small = [real[ax] for ax in small - clipped]
    sev = "error" if any(v < 24 for v in unclipped_small) else "warn"
    floor = " and below the 24dp WCAG 2.5.8 floor" if sev == "error" else ""
    if small & clipped:
        ev["clipped_axes"] = sorted(clipped)
        floor += (" (its " + "/".join("width" if a == "w" else "height" for a in sorted(small & clipped))
                  + " is clipped by a scroll edge and was not judged)")
    if n.kind == "view":
        fix = ("give the clickable View android:minWidth/android:minHeight of "
               f"{min_dp}dp or more padding on the view itself (a TouchDelegate also works "
               "for users but is not reflected in accessibility bounds)")
    else:
        fix = ("use Modifier.minimumInteractiveComponentSize() or "
               f"Modifier.sizeIn(minWidth = {min_dp}.dp, minHeight = {min_dp}.dp); padding only "
               "grows the target when it is applied after (inside) the clickable modifier")
    return [run.finding(
        "a11y.touch_target.small", sev, n,
        f"Touch target is {w_dp}x{h_dp}dp (< {min_dp}dp{floor}). Make the touchable area at "
        f"least {min_dp}x{min_dp}dp: {fix}.", ev)]


# --------------------------------------------------------------------------- #
# R3 -- text contrast (the one pixel rule).
# --------------------------------------------------------------------------- #
_LIN = [((c / 255.0) / 12.92) if (c / 255.0) <= 0.03928 else (((c / 255.0) + 0.055) / 1.055) ** 2.4
        for c in range(256)]
_MAX_SAMPLES = 24000
_LITTLE = sys.byteorder == "little"


def _rel_lum(rgb: Tuple[int, int, int]) -> float:
    r, g, b = rgb
    return 0.2126 * _LIN[r] + 0.7152 * _LIN[g] + 0.0722 * _LIN[b]


def _contrast(l1: float, l2: float) -> float:
    hi, lo = (l1, l2) if l1 >= l2 else (l2, l1)
    return (hi + 0.05) / (lo + 0.05)


def _unpack(v: int) -> Tuple[int, int, int]:
    if _LITTLE:
        return v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF
    return (v >> 24) & 0xFF, (v >> 16) & 0xFF, (v >> 8) & 0xFF


def _hex(c: Tuple[int, int, int]) -> str:
    return "#%02X%02X%02X" % c


def _sample(img: WindowImage, rect: Dict[str, int]) -> Optional[Tuple[Counter, Counter, int]]:
    """(all-pixel counts, perimeter counts, step) of packed RGBA ints inside ``rect``."""
    s = img.scale or 1.0
    x0 = int(math.floor((rect["x"] - img.origin_x) * s))
    y0 = int(math.floor((rect["y"] - img.origin_y) * s))
    x1 = int(math.ceil((rect["x"] + rect["w"] - img.origin_x) * s))
    y1 = int(math.ceil((rect["y"] + rect["h"] - img.origin_y) * s))
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(img.w, x1), min(img.h, y1)
    if x1 - x0 < 2 or y1 - y0 < 2 or len(img.rgba) < img.w * img.h * 4:
        return None
    mv = memoryview(img.rgba)[: img.w * img.h * 4].cast("I")
    area = (x1 - x0) * (y1 - y0)
    step = max(1, int(math.ceil(math.sqrt(area / float(_MAX_SAMPLES)))))
    W = img.w
    counts: Counter = Counter()
    for yy in range(y0, y1, step):
        base = yy * W
        counts.update(mv[base + x0: base + x1: step])
    perim: Counter = Counter()
    perim.update(mv[y0 * W + x0: y0 * W + x1])
    perim.update(mv[(y1 - 1) * W + x0: (y1 - 1) * W + x1])
    for yy in range(y0 + 1, y1 - 1):
        perim[mv[yy * W + x0]] += 1
        perim[mv[yy * W + x1 - 1]] += 1
    return counts, perim, step


def _fold_rgb(c: Counter) -> Dict[Tuple[int, int, int], int]:
    out: Dict[Tuple[int, int, int], int] = {}
    for v, k in c.items():
        rgb = _unpack(v)
        out[rgb] = out.get(rgb, 0) + k
    return out


def _dominant_colors(counts: Counter, perim: Counter) -> Optional[Dict[str, Any]]:
    """Background = dominant colour (perimeter mode, else global quantised mode);
    foreground = the most frequent colour among the most-contrasting quarter of the
    non-background ("ink") pixels. Anti-aliased edge pixels sit between the two and
    never define either colour."""
    colors = _fold_rgb(counts)
    total = sum(colors.values())
    if total < 16 or len(colors) < 2:
        return None
    pcolors = _fold_rgb(perim)
    ptotal = sum(pcolors.values()) or 1
    prgb, pcount = max(pcolors.items(), key=lambda kv: kv[1]) if pcolors else ((0, 0, 0), 0)
    bins: Dict[Tuple[int, int, int], int] = {}
    for (r, g, b), k in colors.items():
        q = (r >> 4, g >> 4, b >> 4)
        bins[q] = bins.get(q, 0) + k
    mode_q = max(bins.items(), key=lambda kv: kv[1])[0]

    def best_in(q: Tuple[int, int, int]) -> Tuple[int, int, int]:
        return max((c for c in colors if (c[0] >> 4, c[1] >> 4, c[2] >> 4) == q),
                   key=lambda c: colors[c])

    if pcount / ptotal >= 0.5:
        bg = prgb
        bg_q = (bg[0] >> 4, bg[1] >> 4, bg[2] >> 4)
        bg_source = "perimeter"
        if mode_q != bg_q and bins[mode_q] / total >= 0.5:
            # The rim is the window showing through the node's insets (a MaterialButton's
            # touch bounds are taller than its fill): the text sits on the fill.
            bg_q, bg, bg_source = mode_q, best_in(mode_q), "fill"
    else:
        bg_q, bg, bg_source = mode_q, best_in(mode_q), "mode"
    bg_frac = bins.get(bg_q, 0) / total
    lb = _rel_lum(bg)
    ink = []
    for rgb, k in colors.items():
        ratio = _contrast(_rel_lum(rgb), lb)
        if ratio >= 1.25:
            ink.append((ratio, k, rgb))
    ink_count = sum(k for _, k, _ in ink)
    ink_frac = ink_count / total
    if ink_frac < 0.01:
        return None
    ink.sort(key=lambda t: -t[0])
    acc = 0
    top: List[Tuple[float, int, Tuple[int, int, int]]] = []
    for t in ink:
        top.append(t)
        acc += t[1]
        if acc >= 0.25 * ink_count:
            break
    fg = max(top, key=lambda t: (t[1], t[0]))[2]
    if bg_source == "fill":
        # Ink here is text plus the lower-contrast rim, which can outnumber the glyphs;
        # take the most contrasting colour with real support (anti-aliased pixels sit
        # between text and fill, so they never win).
        support = max(3, int(0.02 * ink_count))
        fg = max((t for t in ink if t[1] >= support), key=lambda t: t[0], default=top[0])[2]
    lf = _rel_lum(fg)
    return {"fg": fg, "bg": bg, "ratio": round(_contrast(lf, lb), 2), "fg_lum": lf, "bg_lum": lb,
            "bg_fraction": round(bg_frac, 3), "ink_fraction": round(ink_frac, 3),
            "px_sampled": total, "bg_source": bg_source, "distinct_colors": len(colors)}


def _image_for(n: _Node, run: _Run) -> Optional[WindowImage]:
    ctx = run.ctx
    if n.win is not None and n.win.root_view_id is not None:
        img = ctx.window_images.get(n.win.root_view_id)
        if img is not None:
            return img
    if len(ctx.window_images) == 1 and len(run.windows) <= 1:
        return next(iter(ctx.window_images.values()))
    if ctx.screenshot_rgba is not None and (
            run.mode == "compose" or len(run.windows) <= 1 or (n.win is not None and n.win.index == 0)):
        return ctx.legacy_image()
    return None


def _inactive(n: _Node) -> bool:
    """``n`` is disabled or part of a disabled control. WCAG 1.4.3 exempts inactive UI
    components, and Compose puts a Button's or TextField's label on a child Text whose
    own node reports enabled while the control is disabled, so look up to the nearest
    actionable / focusable ancestor too."""
    if not _enabled(n):
        return True
    p = n.parent
    while p is not None:
        if not p.ignored and (_actionable(p) or _focus_candidate(p)):
            return not _enabled(p)
        p = p.parent
    return False


def rule_contrast(n: _Node, run: _Run) -> List[Finding]:
    if not n.text or not _visible(n) or _inactive(n) or "password" in n.flags:
        return []
    ctx = run.ctx
    sample_src = None
    colors = None
    if ctx.component_image_fn and n.render_node_id:
        out = ctx.component_image_fn(int(n.render_node_id))
        if out:
            w, h, rgba = out
            img = WindowImage(w, h, rgba, 1.0, n.x, n.y)
            s = _sample(img, n.rect)
            if s:
                colors = _dominant_colors(s[0], s[1])
                sample_src = "component"
    img = None
    if colors is None:
        img = _image_for(n, run)
        if img is None:
            run.stats_inc("contrast_no_image")
            return []
        s = _sample(img, n.rect)
        if not s:
            return []
        colors = _dominant_colors(s[0], s[1])
        sample_src = ("window:%s" % img.root_view_id) if img.root_view_id is not None else "screenshot"
    run.stats_inc("contrast_checked")
    if not colors:
        return []
    ratio = colors["ratio"]
    size_dp = run.dp(n.text_size_px) if n.text_size_px > 0 else None
    if size_dp is None:
        size_class = "unknown"
        required = 4.5
    elif size_dp >= 24.0:  # WCAG large text: 18pt == 24 CSS px ~= 24dp
        size_class, required = "large", 3.0
    else:
        size_class, required = "normal", 4.5
    if ratio >= required:
        return []
    low_conf = colors["bg_fraction"] < 0.35 or colors["ink_fraction"] < 0.02
    if size_class == "unknown" and ratio >= 3.0:
        sev = "warn"   # would pass if the text is large (>= 24dp); size unknown
    else:
        sev = "error"
    if low_conf and sev == "error":
        sev = "warn"
    fg, bg = colors["fg"], colors["bg"]
    size_note = (f" Text size is unknown (no rendering info for this node), so the 4.5:1 "
                 f"normal-text threshold was applied; large text (>= 24dp) needs only 3:1."
                 if size_class == "unknown" else "")
    return [run.finding(
        "a11y.contrast.low", sev, n,
        f"Measured contrast {ratio}:1 (text {_hex(fg)} on {_hex(bg)}) is below the "
        f"{required}:1 WCAG 1.4.3 minimum for {size_class if size_class != 'unknown' else 'normal'} "
        f"text. Darken the text or lighten the background (sampled from rendered pixels, so "
        f"it reflects the real theme).{size_note}",
        {"ratio": ratio, "required": required, "fg_hex": _hex(fg), "bg_hex": _hex(bg),
         "fg_lum": round(colors["fg_lum"], 4), "bg_lum": round(colors["bg_lum"], 4),
         "sample": sample_src, "scale": (img.scale if img else 1.0),
         "px_sampled": colors["px_sampled"], "bg_fraction": colors["bg_fraction"],
         "ink_fraction": colors["ink_fraction"], "bg_source": colors["bg_source"],
         "text_size_class": size_class, "text_size_dp": size_dp,
         "low_confidence": low_conf},
        needs_image=True,
    )]


# --------------------------------------------------------------------------- #
# R4 -- redundant contentDescription.
# --------------------------------------------------------------------------- #
_ROLE_WORDS = {
    "Button": ("button", "btn"),
    "Checkbox": ("checkbox", "check box"),
    "Switch": ("switch", "toggle"),
    "RadioButton": ("radio button", "radio"),
    "Tab": ("tab",),
    "DropdownList": ("dropdown", "drop down", "spinner", "combo box"),
    "Image": ("image", "img", "graphic"),
    "Slider": ("slider", "seek bar", "seekbar"),
    "Link": ("link",),
}
_STATE_WORDS = ("not checked", "unchecked", "checked", "not selected", "unselected", "selected")


def rule_redundant_label(n: _Node, run: _Run) -> List[Finding]:
    if not n.cd or not _visible(n):
        return []
    norm = _norm(n.cd)
    if not norm:
        return []
    owner = run.owner(n)
    # The role TalkBack announces with this description: the node's own when it is the
    # stop, else its focus owner's. A merged Compose Icon keeps Role.Image in the
    # semantics, but Compose does not expose it and TalkBack says "Upload image, button".
    role = run.role(n) if owner is n else run.role(owner)
    padded = f" {norm} "
    for w in _ROLE_WORDS.get(role or "", ()):
        if f" {w} " in padded:
            return [run.finding(
                "a11y.label.redundant", "warn", n,
                f"contentDescription \"{n.cd}\" contains the role word '{w}', but TalkBack "
                f"already announces the {role} role (\"{n.cd}, {role.lower()}\"). Drop the word: "
                f"describe the purpose only.",
                {"label": n.cd, "role": role, "reason": "type_noun", "matched_word": w})]
    if {"checkable", "selected"} & owner.flags or {"checkable", "selected"} & n.flags:
        for w in _STATE_WORDS:
            if f" {w} " in padded:
                return [run.finding(
                    "a11y.label.redundant", "info", n,
                    f"contentDescription \"{n.cd}\" contains the state word '{w}'; TalkBack "
                    f"announces the checked/selected state itself, so the label goes stale "
                    f"or is read twice. Use stateDescription for custom state text.",
                    {"label": n.cd, "role": role, "reason": "state_word", "matched_word": w})]
    # The text the node shows: its own, or (a Button whose Text child carries it; Compose
    # serves the description on a synthetic child, so TalkBack reads both) its
    # non-focusable descendants'.
    shown = n.text or (run.desc_text(n) if _focus_candidate(n) else "")
    if shown and not _editable(n) and _norm(shown) == norm:
        return [run.finding(
            "a11y.label.redundant", "info", n,
            "contentDescription duplicates the visible text; remove it so the visible text is "
            "used (and stays in sync when the text changes).",
            {"label": n.cd, "role": role, "reason": "equals_text"})]
    return []


# --------------------------------------------------------------------------- #
# R5 -- clickable without a role.
# --------------------------------------------------------------------------- #
def _is_row_like(n: _Node) -> bool:
    """``n`` is a collection row, or fills (>=80%) the row it sits in."""
    cc = n.collection_ctx
    if cc is None:
        return False
    row = cc[1]
    if row is n:
        return True
    return row.w > 0 and row.h > 0 and n.w * n.h >= 0.8 * row.w * row.h


def rule_role_missing(n: _Node, run: _Run) -> List[Finding]:
    if not _visible(n) or ("clickable" not in n.flags and "CLICK" not in n.actions):
        return []
    if _editable(n) or run.role(n) is not None or _has_state(n):
        return []
    label, src = run.effective_label(n)
    if not label:
        return []  # R1 reports the unlabelled case
    if _is_row_like(n):
        return []  # a clickable list row: conventional, announced as a list item
    # A non-focusable descendant that carries the role (e.g. a Compose IconButton
    # whose Role sits on an inner node) makes the intent clear enough.
    stack = list(n.children)
    while stack:
        c = stack.pop()
        if _focus_candidate(c):
            continue
        if run.role(c) in ("Button", "Checkbox", "Switch", "RadioButton", "Tab", "DropdownList"):
            return []
        stack.extend(c.children)
    has_visible_text = bool(n.text) or run.desc_has_text(n)
    sev = "info" if has_visible_text else "warn"
    if n.kind == "view":
        fix = ("use a Button/ImageButton, or set the role via "
               "ViewCompat.setAccessibilityDelegate (AccessibilityNodeInfoCompat.setClassName / "
               "setRoleDescription)")
    else:
        fix = ("pass role = Role.Button to Modifier.clickable/selectable, or add "
               "Modifier.semantics { role = Role.Button }")
    return [run.finding(
        "a11y.role.missing_on_clickable", sev, n,
        f"Clickable element \"{label}\" exposes no role. TalkBack reads the label and "
        f"\"double-tap to activate\", but not what kind of control it is, and it is missing "
        f"from control/button navigation. If it behaves like a button, {fix}.",
        {"label": label, "label_source": src, "has_role": False,
         "class_name": n.class_name})]


# --------------------------------------------------------------------------- #
# R6 -- image with no description.
# --------------------------------------------------------------------------- #
def rule_image_no_desc(n: _Node, run: _Run) -> List[Finding]:
    if not _visible(n) or not _is_image(n, run.role(n)):
        return []
    if _actionable(n):
        return []  # an actionable image with no name is R1 (error)
    if n.cd or n.text or n.labeled_by:
        return []
    owner = n.focus_ancestor
    if owner is not None and _actionable(owner) and run.effective_label(owner)[0]:
        return []  # part of a labelled control; TalkBack reads the control's label
    if n.kind == "view":
        fix = ("set android:contentDescription if it conveys meaning; if it is decorative set "
               "android:importantForAccessibility=\"no\"")
    else:
        fix = ("pass a contentDescription if it conveys meaning; for a decorative image use "
               "contentDescription = null (it then emits no semantics)")
    return [run.finding(
        "a11y.image.no_description", "warn", n,
        f"Image is exposed to accessibility services with no contentDescription and is not "
        f"marked decorative; {fix}.",
        {"class_name": n.class_name, "important_for_accessibility": n.important or "AUTO",
         "focusable": bool({"focusable", "screen_reader_focusable"} & n.flags)})]


# --------------------------------------------------------------------------- #
# R7 -- state not exposed.
# --------------------------------------------------------------------------- #
_PHRASAL = {"sign", "log", "logged", "signed", "kick", "drop", "cut", "back", "show",
            "take", "carry", "hold", "set", "turn", "switch", "check", "tip", "brush"}
_TOGGLE_NOUNS = {"favorite", "favourite", "like", "bookmark", "star", "mute", "unmute",
                 "follow", "subscribe", "pin", "unpin", "heart"}


def rule_state_not_exposed(n: _Node, run: _Run) -> List[Finding]:
    if not _visible(n) or not _actionable(n) or _has_state(n):
        return []
    role = run.role(n)
    label, src = run.effective_label(n)
    words = _norm(label).split()
    reason = None
    sev = "warn"
    if role in _STATEFUL_ROLES:
        if role == "Tab" and _tab_state_known(n):
            return []
        reason = "stateful_role"
    elif "toggle" in words:
        reason = "label_mentions_toggle"
    elif words and words[-1] in ("on", "off", "enabled", "disabled") and len(words) >= 2 \
            and words[-2] not in _PHRASAL:
        reason = "label_encodes_state"
    elif (not n.text and src in ("own", "descendants") and not run.desc_has_text(n)
          and _TOGGLE_NOUNS & set(words)):
        reason, sev = "possible_toggle", "info"  # icon-only control named like a toggle
    if reason is None:
        return []
    if role == "Tab":
        fix = ("mark the selected tab selected (View.setSelected(true), as TabLayout, "
               "BottomNavigationView and NavigationRailView do)" if n.kind == "view" else
               "use Tab(selected = ...) / NavigationBarItem(selected = ...), or "
               "Modifier.selectable(selected = ..., role = Role.Tab)")
    elif n.kind == "view":
        fix = ("use a CompoundButton (Switch/CheckBox/ToggleButton), or set "
               "AccessibilityNodeInfo checkable/checked or stateDescription "
               "(ViewCompat.setStateDescription)")
    else:
        fix = ("use Modifier.toggleable(value = ...) / selectable(selected = ...), or "
               "Modifier.semantics { stateDescription = if (on) \"On\" else \"Off\" }")
    lead = ("If this control toggles, it" if reason == "possible_toggle"
            else "This control looks stateful but")
    return [run.finding(
        "a11y.state.not_exposed", sev, n,
        f"{lead} exposes no state (checked/selected/stateDescription), so TalkBack cannot "
        f"announce whether it is on. Fix: {fix}.",
        {"role": role, "label": label, "reason": reason, "has_checkable": False,
         "has_selected": False, "has_statedesc": False})]


def _tab_state_known(n: _Node) -> bool:
    """A tab's state is its selection: TalkBack says "selected" on the selected tab and
    "Tab, 2 of 3" from CollectionItemInfo; an unselected tab carries no flag of its own.
    Material TabLayout / BottomNavigationView and Compose Tab set exactly that."""
    if n.collection_item_info is not None:
        return True
    p = n.parent
    if p is None:
        return False
    return any(("selected" in s.flags or (s.collection_item_info or {}).get("selected"))
               for s in p.children if s is not n)


# --------------------------------------------------------------------------- #
# R8 -- focusable but announces nothing.
# --------------------------------------------------------------------------- #
def rule_empty_focusable(n: _Node, run: _Run) -> List[Finding]:
    if not _visible(n) or _actionable(n) or _editable(n):
        return []  # actionable/editable are R1 / R16
    if not ({"focusable", "screen_reader_focusable"} & n.flags):
        return []
    if "scrollable" in n.flags or _is_collection(n) or run.has_candidate_descendant(n):
        return []
    label, _ = run.effective_label(n)
    if label or run.role(n) or n.range_info or n.hint:
        return []
    if n.kind == "view":
        fix = "give it content, or set android:importantForAccessibility=\"no\" / focusable=false"
    else:
        fix = ("give it content, or remove it with Modifier.clearAndSetSemantics {} (or drop the "
               "focusable/mergeDescendants modifier)")
    return [run.finding(
        "a11y.node.empty_focusable", "warn", n,
        f"This element takes accessibility focus but announces nothing; {fix}.",
        {"flags": sorted({"focusable", "screen_reader_focusable"} & n.flags),
         "test_tag": n.test_tag})]


# --------------------------------------------------------------------------- #
# R11 -- non-scalable text size; R18 -- tiny text.
# --------------------------------------------------------------------------- #
def rule_fixed_scaling(n: _Node, run: _Run) -> List[Finding]:
    if not n.text or n.text_size_px <= 0 or not _visible(n):
        return []
    unit = n.text_size_unit
    if unit is None or unit < 0 or unit == 2:
        return []
    uname = _TEXT_UNITS.get(unit, str(unit))
    size_dp = run.dp(n.text_size_px)
    fs = run.ctx.font_scale or 1.0
    return [run.finding(
        "a11y.text.fixed_scaling", "warn", n,
        f"Text size is set in {uname} ({round(n.text_size_px, 1)}px = {size_dp}dp), so it "
        f"ignores the user's font size setting (current font_scale {fs}); at that scale sp "
        f"text of the same nominal size would render at {round(size_dp * fs, 1)}dp. Set "
        f"android:textSize (or TextView.setTextSize) in sp.",
        {"unit": uname, "text_size_px": round(n.text_size_px, 2), "text_size_dp": size_dp,
         "font_scale": fs})]


def rule_text_too_small(n: _Node, run: _Run) -> List[Finding]:
    if not n.text or n.text_size_px <= 0 or not _visible(n):
        return []
    rendered_dp = run.dp(n.text_size_px)
    fs = run.ctx.font_scale or 1.0
    nominal = rendered_dp / fs if n.text_size_unit == 2 else rendered_dp
    if nominal >= 12.0:
        return []
    unit = "sp" if n.text_size_unit == 2 else "dp"
    return [run.finding(
        "a11y.text.too_small", "warn", n,
        f"Text is {round(nominal, 1)}{unit} at the default font scale (rendered at "
        f"{rendered_dp}dp with font_scale {fs}); body text below 12sp is hard to read. Use "
        f"at least 12sp (14-16sp for body text).",
        {"text_size_px": round(n.text_size_px, 2), "rendered_dp": rendered_dp,
         "nominal": round(nominal, 1), "font_scale": fs})]


# --------------------------------------------------------------------------- #
# R14 -- editable with contentDescription; R16 -- form field without label.
# --------------------------------------------------------------------------- #
def rule_editable_content_desc(n: _Node, run: _Run) -> List[Finding]:
    if not _visible(n) or not _editable(n) or not n.cd:
        return []
    ev = {"content_description": n.cd, "text": n.text or None, "hint": n.hint or None}
    why = (f"Editable field has contentDescription \"{n.cd}\". TalkBack reads it only while "
           f"the field is empty, in place of the hint or label, and drops it once text is "
           f"entered, so the field loses its name when the user reviews what they typed "
           f"(ATF EditableContentDescCheck).")
    if n.kind != "view" and _norm(n.cd) == "search":
        ev["stock_component"] = "material3 SearchBarDefaults.InputField"
        return [run.finding(
            "a11y.editable.content_description", "info", n,
            f"{why} Material3's SearchBar input field (SearchBarDefaults.InputField) sets this "
            f"contentDescription itself; if this is that stock component there is nothing to "
            f"change. If you set it yourself, remove it and use the placeholder.", ev)]
    if n.kind == "view":
        fix = ("remove android:contentDescription and label the field with android:hint, "
               "android:labelFor on a visible TextView, or TextInputLayout")
    else:
        fix = ("if you set this contentDescription yourself (Modifier.semantics), remove it and "
               "use the TextField label/placeholder parameters")
    return [run.finding(
        "a11y.editable.content_description", "error", n, f"{why} Fix: {fix}.", ev)]


def rule_form_label(n: _Node, run: _Run) -> List[Finding]:
    if not _visible(n) or not _editable(n):
        return []
    parts: List[str] = []
    run._collect_desc(n, parts)
    labelled = bool(
        n.text or n.hint or n.cd or n.labeled_by or (n.a11y_id in run.label_for_targets)
        or parts or n.compose_label)
    if labelled:
        return []
    if n.kind == "view":
        fix = ("add android:hint, point a visible TextView at it with android:labelFor, or wrap "
               "it in a TextInputLayout with a hint")
    else:
        fix = "pass label = { Text(...) } (or a placeholder) to the TextField"
    return [run.finding(
        "a11y.form.label_missing", "error", n,
        f"Form field has no label (no text, hint, labeledBy/labelFor or label child); TalkBack "
        f"announces only \"edit box\". Fix: {fix}.",
        {"class_name": n.class_name, "checked": ["text", "hint", "labeled_by", "label_for",
                                                  "descendants", "compose_merged"]})]


# --------------------------------------------------------------------------- #
# R15 -- link purpose unclear.
# --------------------------------------------------------------------------- #
_VAGUE = {"click here", "tap here", "here", "click", "tap", "read more", "learn more", "more",
          "link", "this link", "this", "more info", "more information", "go", "details",
          "see more", "continue reading"}


def _span_texts(n: _Node) -> List[str]:
    """Link texts from the compat span extras (SPANS_START_KEY / SPANS_END_KEY)."""
    starts = ends = None
    for k, v in n.extras.items():
        if k.endswith("SPANS_START_KEY"):
            starts = v
        elif k.endswith("SPANS_END_KEY"):
            ends = v
    if not starts or not ends or not n.text:
        return []
    try:
        s = [int(x) for x in re.findall(r"-?\d+", starts)]
        e = [int(x) for x in re.findall(r"-?\d+", ends)]
    except ValueError:
        return []
    out = []
    for a, b in zip(s, e):
        if 0 <= a < b <= len(n.text):
            out.append(n.text[a:b])
    return out


def rule_link_purpose(n: _Node, run: _Run) -> List[Finding]:
    if not _visible(n):
        return []
    out: List[Finding] = []
    for span in _span_texts(n):
        if _norm(span) in _VAGUE:
            out.append(run.finding(
                "a11y.link.purpose_unclear", "warn", n,
                f"Link text \"{span}\" does not say where it goes; screen-reader users often "
                f"list links out of context. Use descriptive link text (e.g. \"Read the privacy "
                f"policy\").", {"link_text": span, "kind": "span"}))
    if out or not _actionable(n):
        return out
    role = run.role(n)
    label, _ = run.effective_label(n)
    if _norm(label) not in _VAGUE:
        return []
    is_link = role == "Link" or "link" in n.simple_class.lower() or "URLSpan" in n.class_name
    if not is_link and n.collection_ctx is not None:
        # A per-row action ("More info" on every list row) takes its purpose from the
        # row it sits in (WCAG 2.4.4, "in context"), the way R12 accepts per-row repeats.
        return []
    sev = "warn" if is_link else "info"
    return [run.finding(
        "a11y.link.purpose_unclear", sev, n,
        f"{'Link' if is_link else 'Action'} text \"{label}\" does not describe its purpose out "
        f"of context. Name the destination/action (e.g. \"Read more about pricing\"), or give "
        f"it a contentDescription that does.",
        {"link_text": label, "kind": "link" if is_link else "action", "role": role})]


# --------------------------------------------------------------------------- #
# Per-node registry.
# --------------------------------------------------------------------------- #
NODE_RULES: List[Tuple[str, Callable[[_Node, _Run], List[Finding]]]] = [
    ("a11y.label.missing", rule_missing_label),
    ("a11y.touch_target.small", rule_touch_target),
    ("a11y.contrast.low", rule_contrast),
    ("a11y.label.redundant", rule_redundant_label),
    ("a11y.role.missing_on_clickable", rule_role_missing),
    ("a11y.image.no_description", rule_image_no_desc),
    ("a11y.state.not_exposed", rule_state_not_exposed),
    ("a11y.node.empty_focusable", rule_empty_focusable),
    ("a11y.text.fixed_scaling", rule_fixed_scaling),
    ("a11y.editable.content_description", rule_editable_content_desc),
    ("a11y.link.purpose_unclear", rule_link_purpose),
    ("a11y.form.label_missing", rule_form_label),
    ("a11y.text.too_small", rule_text_too_small),
]


# --------------------------------------------------------------------------- #
# Screen (cross-node) rules.
# --------------------------------------------------------------------------- #
def rule_headings(run: _Run) -> List[Finding]:
    """R9 -- per window: no headings on a long screen, empty or duplicate headings."""
    out: List[Finding] = []
    by_win: Dict[int, List[_Node]] = {}
    for n in run.nodes:
        if n.win is not None:
            by_win.setdefault(n.win.index, []).append(n)
    for win in run.windows:
        nodes = by_win.get(win.index, [])
        headings = [n for n in nodes if _visible(n) and (
            "heading" in n.flags or (n.collection_item_info or {}).get("heading"))]
        text_stops = [n for n in nodes if run.is_stop(n) and n.text]
        scrolls = [n for n in nodes if _visible(n) and "scrollable" in n.flags]
        can_scroll = any({"SCROLL_FORWARD", "SCROLL_BACKWARD", "SCROLL_DOWN", "SCROLL_UP"} & s.actions
                         for s in scrolls)
        long_screen = len(text_stops) > 12 or (can_scroll and len(text_stops) >= 8)
        if not headings and long_screen and win.root is not None:
            out.append(run.finding(
                "a11y.heading.structure", "info", win.root,
                "Long content screen has no headings. Mark section titles as headings "
                "(Compose: Modifier.semantics { heading() }; View: android:accessibilityHeading="
                "\"true\") so TalkBack users can jump between sections.",
                {"reason": "no_headings", "heading_count": 0,
                 "text_stop_count": len(text_stops), "scrollable": bool(scrolls)}))
        prev: Optional[str] = None
        for n in sorted(headings, key=lambda h: (h.y, h.x)):
            lbl, _ = run.effective_label(n)
            if not lbl:
                out.append(run.finding(
                    "a11y.heading.structure", "warn", n,
                    "Heading has no label; an empty heading is a confusing stop in TalkBack's "
                    "heading navigation.", {"reason": "empty_heading"}))
                continue
            if prev is not None and _norm_label(lbl) == prev:
                out.append(run.finding(
                    "a11y.heading.structure", "info", n,
                    f"Duplicate adjacent heading \"{lbl}\"; heading navigation stops on the "
                    f"same text twice.", {"reason": "duplicate_adjacent", "label": lbl}))
            prev = _norm_label(lbl)
    return out


def rule_grouping(run: _Run) -> List[Finding]:
    """R10 -- short, tightly stacked text stops inside one row/card read separately."""
    out: List[Finding] = []
    for p in run.nodes:
        # A not-important container (a View screen's LinearLayout) still groups its texts.
        if (not _on_screen(p) or (_focus_candidate(p) and not p.ignored)
                or "scrollable" in p.flags or _is_collection(p)):
            continue
        # Texts folded into a focusable ancestor are not stops, so they never count below.
        leaves = [c for c in p.children
                  if run.is_stop(c) and not _actionable(c) and not _editable(c)
                  and c.own_label and len(c.own_label) <= 60
                  and run.dp(c.h) <= 40 and not c.children]
        is_row = p.collection_ctx is not None and p.collection_ctx[1] is p
        if len(leaves) < (2 if is_row else 3):
            continue
        if run.dp(p.h) > 160:
            continue
        ys = sorted(leaves, key=lambda c: c.y)
        ok = True
        for a, b in zip(ys, ys[1:]):
            gap = b.y - (a.y + a.h)
            overlap = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
            if gap < -2 or run.dp(max(gap, 0)) > 8 or overlap < 0.5 * min(a.w, b.w):
                ok = False
                break
        if not ok:
            continue
        if _any_outside(leaves, p):
            continue
        if p.kind == "view":
            fix = ("make the container focusable (android:focusable=\"true\" / "
                   "android:screenReaderFocusable=\"true\") so its children are read together")
        else:
            fix = "wrap the group in Modifier.semantics(mergeDescendants = true) {}"
        out.append(run.finding(
            "a11y.grouping.missing", "info", p,
            f"{len(leaves)} short texts in one {'list row' if is_row else 'group'} are "
            f"separate TalkBack stops; users must swipe through each. {fix[0].upper()}{fix[1:]}.",
            {"group_size": len(leaves), "children": [c.key for c in leaves],
             "labels": [c.own_label for c in leaves]}))
    return out


def _any_outside(leaves: List[_Node], p: _Node, slop: int = 2) -> bool:
    for c in leaves:
        if c.x < p.x - slop or c.y < p.y - slop or c.x + c.w > p.x + p.w + slop \
                or c.y + c.h > p.y + p.h + slop:
            return True
    return False


def rule_duplicate_label(run: _Run) -> List[Finding]:
    """R12 -- the same label on distinct actionable stops. Per-row repeats inside a
    RecyclerView / LazyColumn / ListView ("Delete" in every row) are fine; repeats
    under the same parent are not."""
    out: List[Finding] = []
    groups: Dict[str, List[_Node]] = {}
    for n in run.nodes:
        if not _visible(n) or not _actionable(n) or _editable(n):
            continue
        lbl, _ = run.effective_label(n)
        if not lbl or lbl.startswith("<"):
            continue
        groups.setdefault(_norm_label(lbl), []).append(n)

    def per_row_ok(a: _Node, b: _Node) -> bool:
        ca, cb = a.collection_ctx, b.collection_ctx
        if ca is None or cb is None or ca[0] is not cb[0] or ca[1] is cb[1]:
            return False
        # Compose lazy items often have no semantics node of their own, so two
        # buttons of ONE visual row can surface as sibling "rows" of the list.
        # For a one-dimensional list, also require different visual slots.
        info = ca[0].collection_info or {}
        cols, rows = info.get("column_count"), info.get("row_count")
        ra, rb = ca[1], cb[1]
        if cols == 1:     # vertical list: rows must not share a horizontal band
            overlap = min(ra.y + ra.h, rb.y + rb.h) - max(ra.y, rb.y)
            return overlap <= 0.5 * min(ra.h, rb.h)
        if rows == 1:     # horizontal list
            overlap = min(ra.x + ra.w, rb.x + rb.w) - max(ra.x, rb.x)
            return overlap <= 0.5 * min(ra.w, rb.w)
        return True

    for norm, members in groups.items():
        if len(members) < 2:
            continue
        for m in members:
            others = [o for o in members if o is not m and not per_row_ok(m, o)]
            if not others:
                continue
            same_parent = [o for o in others if o.parent is m.parent]
            sev = "warn" if same_parent else "info"
            lbl = run.effective_label(m)[0]
            out.append(run.finding(
                "a11y.duplicate.label", sev, m,
                f"{len(others) + 1} actionable elements are all announced as \"{lbl}\""
                f"{' in the same container' if same_parent else ''}; users cannot tell them "
                f"apart. Disambiguate the labels (e.g. \"Delete photo\" vs \"Delete album\").",
                {"label": lbl, "same_parent": bool(same_parent),
                 "duplicates": [o.key for o in others][:20],
                 "node_ids": [m.a11y_id] + [o.a11y_id for o in others][:20]}))
    return out


def rule_duplicate_bounds(run: _Run) -> List[Finding]:
    """R13 -- two clickable nodes with identical bounds in the same window."""
    out: List[Finding] = []
    seen: Dict[Tuple[int, int, int, int, int], _Node] = {}
    for n in run.nodes:
        if not _visible(n) or not _actionable(n):
            continue
        k = (n.win.index if n.win else 0, n.x, n.y, n.w, n.h)
        first = seen.get(k)
        if first is None:
            seen[k] = n
            continue
        out.append(run.finding(
            "a11y.clickable.duplicate_bounds", "warn", n,
            f"This clickable element has exactly the same bounds as another clickable element "
            f"({first.key}); TalkBack focuses both and a double-tap activates only one. Make only "
            f"one of them clickable.",
            {"duplicate_of": first.key, "duplicate_of_id": first.a11y_id}))
    return out


def rule_traversal(run: _Run) -> List[Finding]:
    """R17 -- traversalBefore/After cycles (error) and dangling targets (info)."""
    out: List[Finding] = []
    edges: Dict[int, Set[int]] = {}
    nodes_by_id = run.by_id
    for n in run.nodes:
        if n.a11y_id is None:
            continue
        for tgt, forward in ((n.traversal_before, True), (n.traversal_after, False)):
            if not tgt:
                continue
            if tgt not in nodes_by_id:
                if run.identity_ok:
                    out.append(run.finding(
                        "a11y.traversal.order", "info", n,
                        f"traversal{'Before' if forward else 'After'} points at a node that is "
                        f"not in the accessibility tree (id {tgt}); TalkBack ignores it.",
                        {"reason": "dangling", "target_id": tgt}))
                continue
            a, b = (n.a11y_id, tgt) if forward else (tgt, n.a11y_id)
            if a != b:
                edges.setdefault(a, set()).add(b)
            else:
                edges.setdefault(a, set()).add(a)
    # Tarjan SCC (iterative) -> cycles.
    index: Dict[int, int] = {}
    low: Dict[int, int] = {}
    on_stack: Set[int] = set()
    stack: List[int] = []
    counter = [0]
    sccs: List[List[int]] = []
    for start in list(edges):
        if start in index:
            continue
        work = [(start, iter(sorted(edges.get(start, ()))))]
        index[start] = low[start] = counter[0]
        counter[0] += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(sorted(edges.get(w, ())))))
                    advanced = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1 or v in edges.get(v, ()):
                    sccs.append(comp)
    for comp in sccs:
        members = sorted((nodes_by_id[i] for i in comp if i in nodes_by_id), key=lambda m: m.order)
        if not members:
            continue
        head = members[0]
        out.append(run.finding(
            "a11y.traversal.order", "error", head,
            f"traversalBefore/traversalAfter constraints form a cycle through {len(members)} "
            f"node(s); TalkBack's reading order becomes undefined (it may skip or loop). Remove "
            f"one of the constraints.",
            {"reason": "cycle", "cycle": [m.key for m in members]}))
    return out


SCREEN_RULES: List[Tuple[str, Callable[[_Run], List[Finding]]]] = [
    ("a11y.heading.structure", rule_headings),
    ("a11y.grouping.missing", rule_grouping),
    ("a11y.duplicate.label", rule_duplicate_label),
    ("a11y.clickable.duplicate_bounds", rule_duplicate_bounds),
    ("a11y.traversal.order", rule_traversal),
]


# --------------------------------------------------------------------------- #
# Driver.
# --------------------------------------------------------------------------- #
def _dedupe(findings: List[Finding]) -> List[Finding]:
    seen = set()
    out: List[Finding] = []
    for f in findings:
        key = (f.rule, f.node.get("key"), f.node.get("id"), f.bounds.get("x"), f.bounds.get("y"),
               f.evidence.get("matched_word"), f.evidence.get("reason"),
               f.evidence.get("link_text"))
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


_SEV_RANK = {"error": 0, "warn": 1, "info": 2}


def _execute(run: _Run) -> List[Finding]:
    """Run every enabled rule. A rule that raises becomes a ``rule.error``
    diagnostic, except for a lost or timed-out session (``TransportError``,
    e.g. from ``ctx.component_image_fn``): that propagates, so the caller
    re-attaches or reports it instead of returning a lint that silently
    skipped rules."""
    from .client import TransportError
    ctx = run.ctx
    out: List[Finding] = []
    errors: Dict[str, int] = {}
    for n in run.nodes:
        for rid, fn in NODE_RULES:
            if not run.on(rid):
                continue
            if RULES_BY_ID[rid].needs_image and (
                    (not ctx.has_image and not ctx.component_image_fn)
                    or (ctx.image_keys is not None and n.key not in ctx.image_keys)):
                continue
            try:
                out.extend(fn(n, run))
            except TransportError:
                raise  # a lost session is not a rule bug
            except Exception as e:  # never abort the lint; always report
                errors[rid] = errors.get(rid, 0) + 1
                if errors[rid] <= 3:
                    ctx.diag("rule.error", f"{rid} raised {type(e).__name__}: {e}", level="error",
                             rule=rid, node_key=n.key)
    for rid, fn in SCREEN_RULES:
        if not run.on(rid):
            continue
        try:
            out.extend(fn(run))
        except TransportError:
            raise
        except Exception as e:
            errors[rid] = errors.get(rid, 0) + 1
            ctx.diag("rule.error", f"{rid} raised {type(e).__name__}: {e}", level="error", rule=rid)
    for rid, k in errors.items():
        if k > 3:
            ctx.diag("rule.error", f"{rid} raised on {k} nodes in total (first 3 reported)",
                     level="error", rule=rid)
    out = _dedupe(out)
    out.sort(key=lambda f: (_SEV_RANK.get(f.severity, 3), f.rule,
                            f.bounds.get("y", 0), f.bounds.get("x", 0)))
    return out


def _is_a11y_shape(roots: Sequence[Dict[str, Any]]) -> bool:
    for r in roots:
        if isinstance(r, dict):
            return "host_view_id" in r or "virtual_id" in r
    return False


def _prepare(roots_or_data: Any, ctx: LintContext,
             compose_data: Optional[Dict[str, Any]] = None
             ) -> Tuple[List[_Node], List[_Win], str, Dict[str, int]]:
    if isinstance(roots_or_data, dict) and "windows" in roots_or_data:
        wins = roots_or_data.get("windows") or []
        sample = [w.get("root") for w in wins if w.get("root")]
        if _is_a11y_shape(sample):
            roots, windows, stats = _build_a11y(roots_or_data, compose_data, ctx)
            return roots, windows, "a11y", stats
        roots, windows, stats = _build_compose(sample, ctx)
        return roots, windows, "compose", stats
    roots_list = list(roots_or_data or [])
    if _is_a11y_shape(roots_list):
        data = {"windows": [{"root_view_id": None, "root": r} for r in roots_list]}
        roots, windows, stats = _build_a11y(data, compose_data, ctx)
        return roots, windows, "a11y", stats
    roots, windows, stats = _build_compose(roots_list, ctx)
    return roots, windows, "compose", stats


def lint_tree(roots: Any, ctx: LintContext, enabled: Optional[Iterable[str]] = None,
              compose_data: Optional[Dict[str, Any]] = None) -> List[Finding]:
    """Run the enabled rules and return deduplicated findings (errors first).

    ``roots`` is either the ``a11y.a11y_to_dict`` output, a list of its window root
    node dicts, or (legacy) a list of Compose semantics root dicts. ``enabled`` is
    any iterable of rule ids/aliases (None == all). Diagnostics accumulate on
    ``ctx.diagnostics``.
    """
    enabled_ids = resolve_rule_ids(enabled) if enabled else None
    nodes, windows, mode, _ = _prepare(roots, ctx, compose_data)
    run = _Run(nodes, windows, ctx, enabled_ids, mode)
    return _execute(run)


@dataclass
class LintReport:
    findings: List[Finding]
    diagnostics: List[Dict[str, Any]]
    stats: Dict[str, Any]
    density: int = DEFAULT_DENSITY
    font_scale: float = 1.0
    wcag_mode: bool = False
    a11y_data: Optional[Dict[str, Any]] = None
    compose_data: Optional[Dict[str, Any]] = None

    @property
    def summary(self) -> Dict[str, Any]:
        s = summarize(self.findings)
        s["rule_errors"] = sum(1 for d in self.diagnostics if d.get("code") == "rule.error")
        return s

    def to_dict(self) -> Dict[str, Any]:
        out = {
            "density": self.density,
            "font_scale": self.font_scale,
            "wcag_mode": self.wcag_mode,
            "summary": self.summary,
            "findings": [f.to_dict() for f in self.findings],
            "diagnostics": self.diagnostics,
            "stats": self.stats,
        }
        if (self.a11y_data or {}).get("generation"):
            # The generation of the dump the finding keys belong to (see a11y.generation).
            out["generation"] = self.a11y_data["generation"]
        return out


def lint_unified(a11y_data: Dict[str, Any], ctx: LintContext,
                 compose_data: Optional[Dict[str, Any]] = None,
                 enabled: Optional[Iterable[str]] = None) -> LintReport:
    """Lint the unified a11y dump (Views + Compose) with optional Compose detail."""
    t0 = time.time()
    enabled_ids = resolve_rule_ids(enabled) if enabled else None
    nodes, windows, mode, bstats = _prepare(a11y_data, ctx, compose_data)
    run = _Run(nodes, windows, ctx, enabled_ids, mode)
    findings = _execute(run)
    rstats = run.stats
    text_nodes = [n for n in run.nodes if n.text and _visible(n)]
    sized = [n for n in text_nodes if n.text_size_px > 0]
    stats: Dict[str, Any] = {
        "mode": mode,
        "nodes": len(run.nodes),
        "windows": [w.to_dict() for w in windows],
        "virtual_nodes": bstats.get("virtual_nodes", 0),
        "compose_joined": bstats.get("compose_joined", 0),
        "text_nodes": len(text_nodes),
        "text_nodes_with_size": len(sized),
        "contrast_windows": sorted(k for k in ctx.window_images if k is not None),
        "contrast_checked": rstats.get("contrast_checked", 0),
        "rules": sorted(enabled_ids) if enabled_ids else ALL_RULE_IDS,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }
    if not run.nodes:
        ctx.diag("tree.empty", "The accessibility dump has no nodes; nothing was linted.",
                 level="warn")
    covered = [f for f in findings if (f.window or {}).get("covered_by") is not None]
    if covered:
        ctx.diag("window.covered",
                 f"{len(covered)} finding(s) are on window(s) under an open modal window (a "
                 f"dialog); TalkBack cannot reach them until it closes (finding.window."
                 f"covered_by names the dialog's root_view_id).", level="info")
    if rstats.get("contrast_no_image"):
        ctx.diag("contrast.no_image",
                 f"{rstats['contrast_no_image']} text node(s) were not contrast-checked because "
                 f"their window was not captured.", level="info")
    wants_size = enabled_ids is None or {"a11y.text.fixed_scaling", "a11y.text.too_small"} & enabled_ids
    if wants_size and text_nodes and not sized:
        ctx.diag("text_size.unavailable",
                 "No node carries ExtraRenderingInfo text size (request include_rendering_info; "
                 "Compose text never reports it), so R11/R18 did not run and contrast used the "
                 "normal-text threshold.", level="info")
    return LintReport(findings, ctx.diagnostics, stats, ctx.density, ctx.font_scale,
                      ctx.wcag_mode, a11y_data, compose_data)


# --------------------------------------------------------------------------- #
# Device flow (shared by cli.py and mcp_server.py so both stay thin).
# --------------------------------------------------------------------------- #
def _window_has_text(root: Dict[str, Any]) -> bool:
    stack = [root]
    while stack:
        n = stack.pop()
        if n.get("text"):
            return True
        stack.extend(n.get("children") or [])
    return False


def _window_has_key(root: Dict[str, Any], keys: Set[str]) -> bool:
    stack = [root]
    while stack:
        n = stack.pop()
        if n.get("node_key") in keys:
            return True
        stack.extend(n.get("children") or [])
    return False


def capture_window_images(conn: Any, a11y_data: Dict[str, Any], ctx: LintContext,
                          scale: float = 1.0, only_keys: Optional[Set[str]] = None) -> List[int]:
    """Screenshot every window that has text (``conn.screenshot(root_id=...)``) into
    ``ctx.window_images`` (with ``only_keys``: only the windows holding those nodes).
    Returns the captured root_view_ids."""
    from . import png as pngmod
    from .client import TransportError
    got: List[int] = []
    for i, w in enumerate(a11y_data.get("windows") or []):
        root = w.get("root")
        rvid = w.get("root_view_id")
        if not root or not _window_has_text(root):
            continue
        if only_keys is not None and not _window_has_key(root, only_keys):
            continue
        try:
            resp = conn.screenshot(root_id=int(rvid or 0), scale=scale)
            if not resp.HasField("screenshot") or not resp.screenshot.width:
                ctx.diag("contrast.capture_failed",
                         f"window {rvid}: the agent returned no screenshot", level="warn")
                continue
            iw, ih, rgba = pngmod._decode_to_rgba(resp.screenshot)
        except TransportError:
            raise  # a lost or timed-out session is not "no screenshot"; don't lint half-blind
        except Exception as e:
            ctx.diag("contrast.capture_failed", f"window {rvid}: {type(e).__name__}: {e}",
                     level="warn")
            continue
        r = _rect_of(root)
        key = int(rvid) if rvid is not None else 0
        ctx.window_images[key] = WindowImage(iw, ih, rgba, float(resp.screenshot.scale) or scale,
                                             r["x"], r["y"], key)
        got.append(key)
    return got


def run_lint(conn: Any, *, density: Optional[int], font_scale: float = 1.0,
             include_contrast: bool = True, scale: float = 1.0, wcag_mode: bool = False,
             rules: Optional[Iterable[str]] = None, include_rendering_info: bool = True,
             a11y_data: Optional[Dict[str, Any]] = None,
             include_compose: bool = True,
             compose_data: Optional[Dict[str, Any]] = None,
             focus_keys: Optional[Iterable[str]] = None) -> LintReport:
    """Dump (unless ``a11y_data`` is given), join Compose detail (dumped unless
    ``compose_data`` is given), capture window screenshots for contrast, and lint.
    ``conn`` is a ``Client`` or ``Session``. ``focus_keys`` limits the pixel work to the
    windows and nodes with those keys (inspect_node's dossier); every other rule runs on
    the whole tree, so the findings on those nodes equal a full run's.

    Raises :class:`UnknownRuleError` for bad rule ids before touching the device.
    """
    from .a11y import a11y_to_dict
    from .client import TransportError
    from .strings import dump_compose_to_dict

    enabled = resolve_rule_ids(rules) if rules else None
    ctx = LintContext(density=int(density) if density else DEFAULT_DENSITY,
                      font_scale=float(font_scale or 1.0), wcag_mode=bool(wcag_mode))
    if not density:
        ctx.diag("density.unknown", f"Device density unknown; assumed {DEFAULT_DENSITY}dpi.",
                 level="warn")
    if a11y_data is None:
        a11y_data = a11y_to_dict(conn.dump_a11y(
            root_id=0, include_extras=True, include_rendering_info=bool(include_rendering_info)))
    if a11y_data.get("diagnostics"):
        ctx.diag("a11y.dump", str(a11y_data["diagnostics"]), level="info")
    if compose_data is None and include_compose:
        try:
            compose_data = dump_compose_to_dict(conn.dump_compose(
                include_semantics=True, include_slot_table=False, enable_inspection=False))
        except TransportError:
            raise  # a lost session is not "no Compose on screen"
        except Exception as e:
            ctx.diag("compose.unavailable",
                     f"Compose semantics detail unavailable ({type(e).__name__}: {e}); linted "
                     f"the a11y tree alone.", level="info")
    only = set(focus_keys) if focus_keys is not None else None
    if only is not None:
        ctx.image_keys = only
    want_img = include_contrast and (enabled is None or "a11y.contrast.low" in enabled)
    if want_img:
        capture_window_images(conn, a11y_data, ctx, scale, only_keys=only)
    elif enabled is None or "a11y.contrast.low" in enabled:
        ctx.diag("contrast.skipped", "Contrast (R3) was not sampled (include_contrast off).",
                 level="info")
    return lint_unified(a11y_data, ctx, compose_data=compose_data, enabled=enabled)


def format_text(report: LintReport) -> str:
    """Human-readable lint report for the CLI."""
    s = report.summary
    lines = [f"density={report.density}dpi font_scale={report.font_scale} "
             f"-> {s['error']} error, {s['warn']} warn, {s['info']} info"]
    for f in report.findings:
        bdp = f.bounds_dp
        lbl = f.node.get("label")
        lines.append(
            f"[{f.severity.upper():5}] {f.alias or ''} {f.rule} {f.node_key} "
            f"({bdp['x']},{bdp['y']} {bdp['w']}x{bdp['h']}dp)"
            + (f" \"{lbl}\"" if lbl else "") + f": {f.message}")
    for d in report.diagnostics:
        lines.append(f"({d.get('level', 'info')}) {d.get('code')}: {d.get('message')}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Summaries + dict-oriented entry points (shared contract: mcp_server / correlate).
# --------------------------------------------------------------------------- #
def summarize(findings: List[Finding]) -> Dict[str, Any]:
    """Counts by severity + by rule, for the tool/CLI response."""
    by_sev = {"error": 0, "warn": 0, "info": 0}
    by_rule: Dict[str, int] = {}
    for f in findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
        by_rule[f.rule] = by_rule.get(f.rule, 0) + 1
    return {**by_sev, "total": len(findings), "by_rule": by_rule}


def lint_a11y(
    roots: Any,
    density: int = DEFAULT_DENSITY,
    font_scale: float = 1.0,
    enabled: Optional[Iterable[str]] = None,
    compose_data: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Lint and return finding DICTS (no screenshot, so R3 auto-skips).

    ``roots`` may be the ``a11y.a11y_to_dict`` output, a list of a11y root dicts,
    or (legacy) Compose semantics root dicts. ``density`` is the device DPI; a
    non-positive value falls back to ``DEFAULT_DENSITY``.
    """
    ctx = LintContext(density=int(density) if density and density > 0 else DEFAULT_DENSITY,
                      font_scale=font_scale)
    return [f.to_dict() for f in lint_tree(roots, ctx, enabled, compose_data=compose_data)]


def summarize_dicts(findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Severity counts over finding DICTS (uses ``f["severity"]``)."""
    counts = {"error": 0, "warn": 0, "info": 0}
    for f in findings:
        sev = f.get("severity")
        if sev in counts:
            counts[sev] += 1
    return {**counts, "total": len(findings)}
