"""Value normalization for brief output and the capture index (spec section 3.7).

Pure functions over the dict shapes the host already produces (``strings.py``,
``a11y.py``, ``correlate.py`` and the legacy MCP ``dump_tree`` shape). There is no
protobuf and no device I/O here. Shared by the Phase-0 output layer
(:mod:`inspector_widget.output`) and the capture index builder.

* Compose values: :func:`compose_value`, :func:`compose_attrs_brief`,
  :func:`is_action_attr`, :func:`textstyle_brief`, :func:`modifier_brief`.
* Library vs app code: :func:`is_library_source`, :func:`origin_of`.
* Accessibility: :func:`a11y_node_brief`.
* View properties: :func:`prop_value`, :func:`props_to_map`,
  :func:`nondefault_props`, :func:`class_family`, :data:`KEY_PROPS`.
* Formatting: :func:`color_hex`, :func:`packed_color`, :func:`rect_list`,
  :func:`resource_str`, :func:`cap`.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from .normalize_defaults import (
    DERIVED_PROPS,
    FAMILY_PARENTS,
    LIBRARY_FILES,
    MAJORITY_EXEMPT,
    STATIC_VIEW_DEFAULTS,
)

VALUE_CAP = 120
INT_MAX = 2147483647
TEXTUNIT_UNSPECIFIED = "2143289344"  # TextUnit.Unspecified packed as float bits (NaN)
COLOR_UNSPECIFIED = "16"  # Color.Unspecified packed ULong (colour space id 16)
LAMBDA = "λ"

# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #


def _bump(counts: dict[str, int] | None, key: str, n: int = 1) -> None:
    if counts is not None and n:
        counts[key] = counts.get(key, 0) + n


def cap(s: str, n: int = VALUE_CAP) -> str:
    """Cap a string at ``n`` chars, marking the cut as ``…(+N)``."""
    if len(s) <= n:
        return s
    return f"{s[:n]}…(+{len(s) - n})"


def color_hex(argb: int, short: bool = False) -> str:
    """A signed/unsigned 32-bit ARGB int as ``#AARRGGBB`` (``#RRGGBB`` if ``short``
    and fully opaque)."""
    v = int(argb) & 0xFFFFFFFF
    if short and (v >> 24) == 0xFF:
        return f"#{v & 0xFFFFFF:06X}"
    return f"#{v:08X}"


def packed_color(raw: Any, short: bool = False) -> str | None:
    """A Compose ``Color`` packed ULong (as printed, possibly negative) to hex.

    sRGB colours pack ARGB in the high 32 bits and colour space id 0 in the low
    6 bits. Other colour spaces (half-float encoding) return None; so does
    Color.Unspecified (16).
    """
    try:
        v = int(str(raw).strip()) & 0xFFFFFFFFFFFFFFFF
    except ValueError:
        return None
    if v == 16 or (v & 0x3F) != 0 or (v & 0xFFFFFFFF) != 0:
        return None
    return color_hex(v >> 32, short=short)


_COLOR_CALL = re.compile(
    r"Color\(\s*(-?[\d.]+(?:E-?\d+)?),\s*(-?[\d.]+(?:E-?\d+)?),\s*(-?[\d.]+(?:E-?\d+)?),"
    r"\s*(-?[\d.]+(?:E-?\d+)?),\s*([^)]*)\)")


def _color_call_hex(m: re.Match, short: bool = False) -> str:
    r, g, b, a = (float(m.group(i)) for i in range(1, 5))
    if m.group(5).strip() in ("None", "Unspecified") and a == 0.0:
        return "Unspecified"

    def c(x: float) -> int:
        return max(0, min(255, round(x * 255)))

    return color_hex((c(a) << 24) | (c(r) << 16) | (c(g) << 8) | c(b), short=short)


def rect_list(b: Any) -> list[int] | None:
    """Any bounds shape to ``[x, y, w, h]``: ``{"layout": {x,y,w,h}}`` (strings.py),
    flat ``{x,y,w,h}`` (legacy MCP / correlate), or an existing list."""
    if b is None:
        return None
    if isinstance(b, (list, tuple)):
        return [int(v) for v in b[:4]]
    if isinstance(b, Mapping):
        layout = b.get("layout", b)
        if isinstance(layout, Mapping) and "x" in layout:
            return [int(layout.get("x", 0)), int(layout.get("y", 0)),
                    int(layout.get("w", 0)), int(layout.get("h", 0))]
    return None


def render_quad(b: Any) -> Any:
    """The transformed-view quad of a bounds dict, if any (``render`` or ``render_quad``)."""
    if isinstance(b, Mapping):
        return b.get("render") or b.get("render_quad")
    return None


def resource_str(res: Any) -> Any:
    """``{namespace, type, name}`` -> ``@ns:type/name`` (``@type/name`` without ns)."""
    if not isinstance(res, Mapping):
        return res
    typ, name, ns = res.get("type"), res.get("name"), res.get("namespace")
    if not name:
        return res.get("ref") or None
    body = f"{typ}/{name}" if typ else name
    return f"@{ns}:{body}" if ns else f"@{body}"


# --------------------------------------------------------------------------- #
# Compose values
# --------------------------------------------------------------------------- #
_DROP_VALUES = frozenset({
    "", "null", "Modifier", "NaN", "Unspecified", "TextUnit.Unspecified", "Color.Unspecified",
    "property value (Kotlin reflection is not available)",
})
_TMP_RCVR = re.compile(r"^tmp\d+_rcvr$")
_LAMBDA_VALUE = re.compile(
    r"^(?:(?:[\w$]+\.)*ComposableLambda(?:Impl|NImpl)?@[0-9a-fA-F]+"
    r"|(?:kotlin\.jvm\.functions\.)?Function\d*<.*>"
    r"|(?:[\w$]+\.)+[\w$]*\$\$ExternalSyntheticLambda\d+@[0-9a-fA-F]+"
    r"|(?:[\w$]+\.)+[\w$]*Kt\$[\w$]*@[0-9a-fA-F]+)$", re.DOTALL)
_ACTION_ATTR = re.compile(
    r"^(?:AccessibilityAction\(|CustomAccessibilityAction\(|(?:kotlin\.jvm\.functions\.)?"
    r"Function\d*<)")
_CLASS_HASH = re.compile(r"\b(?:[a-z_][\w]*\.)+(?:[\w$]+\$)?([A-Z][\w]*)(?:\$[\w$]*)?@[0-9a-fA-F]+")
_VIEW_TOSTRING = re.compile(r"\b(?:[a-z_][\w]*\.)+([A-Z][\w$]*)\{[^}]*\}")
_BARE_HASH = re.compile(r"\b([A-Z][\w]*)@[0-9a-fA-F]{5,}\b")
_UNITS = re.compile(r"(-?\d+(?:\.\d+)?)\.(sp|dp|em)\b")
_THEME_OBJECTS = ("Typography(", "ColorScheme(", "Shapes(")
_INT_ENUMS = {
    "overflow": {"1": "Clip", "2": "Ellipsis", "3": "Visible", "4": "StartEllipsis",
                 "5": "MiddleEllipsis"},
    "textAlign": {"1": "Left", "2": "Right", "3": "Center", "4": "Justify", "5": "Start",
                  "6": "End"},
    "textDirection": {"1": "Ltr", "2": "Rtl", "3": "Content", "4": "ContentOrLtr",
                      "5": "ContentOrRtl"},
}


def _is_color_key(key: str) -> bool:
    k = key.lower()
    return "color" in k or k == "tint"


def _is_slot_name(key: str) -> bool:
    """``on*`` callbacks and content slots keep a λ marker; other lambdas are dropped."""
    return (key.startswith("on") and key[2:3].isupper()) or key == "content" \
        or key.endswith("Content")


def _num(s: str) -> str:
    f = float(s)
    return str(int(f)) if f.is_integer() else s


def _units(v: str) -> str:
    return _UNITS.sub(lambda m: f"{_num(m.group(1))}{m.group(2)}", v)


def _split_top(s: str, sep: str = ",") -> list[str]:
    """Split on ``sep`` at bracket depth 0."""
    out, depth, cur = [], 0, []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if depth == 0 and s.startswith(sep, i):
            out.append("".join(cur).strip())
            cur = []
            i += len(sep)
            continue
        cur.append(ch)
        i += 1
    if cur:
        out.append("".join(cur).strip())
    return [p for p in out if p]


def _call_args(s: str) -> tuple[str, dict[str, str]]:
    """``Name(k=v, k2=v2)`` -> (``Name``, {k: v}); positional args get keys ``_0..``."""
    m = re.match(r"^([\w.$]+)\((.*)\)$", s.strip(), re.DOTALL)
    if not m:
        return s.strip(), {}
    args: dict[str, str] = {}
    for i, part in enumerate(_split_top(m.group(2))):
        k, eq, v = part.partition("=")
        if eq and re.match(r"^\s*[\w.$]+\s*$", k):
            args[k.strip()] = v.strip()
        else:
            args[f"_{i}"] = part.strip()
    return m.group(1), args


def _clean(v: str) -> str:
    """Generic value clean-up: class@hash -> Class, View{...} -> View, units, colours."""
    v = _VIEW_TOSTRING.sub(lambda m: m.group(1), v)
    v = _CLASS_HASH.sub(lambda m: m.group(1), v)
    v = _BARE_HASH.sub(lambda m: m.group(1), v)
    v = _COLOR_CALL.sub(_color_call_hex, v)
    return _units(v)


def textstyle_brief(raw: str) -> str:
    """``TextStyle(...)`` -> only the fields that differ from the default, e.g.
    ``16sp/24sp w400 ls0.5sp #1D1B20`` (size/lineHeight, weight, style, letter
    spacing, colour, then alignment and decoration when set)."""
    name, args = _call_args(raw)
    if name != "TextStyle":
        return cap(_clean(raw))

    def unit(key: str) -> str | None:
        v = args.get(key, "")
        if not v or "Unspecified" in v or v in ("null", "NaN"):
            return None
        return _units(v)

    parts: list[str] = []
    fs, lh = unit("fontSize"), unit("lineHeight")
    if fs and lh:
        parts.append(f"{fs}/{lh}")
    elif fs:
        parts.append(fs)
    elif lh:
        parts.append(f"lh{lh}")
    fw = re.search(r"weight=(\d+)", args.get("fontWeight", ""))
    if fw:
        parts.append(f"w{fw.group(1)}")
    if "Italic" in args.get("fontStyle", ""):
        parts.append("italic")
    ff = args.get("fontFamily", "")
    if ff and ff != "null" and not ff.endswith(("SansSerif", "Default")):
        parts.append(_clean(ff.replace("FontFamily.", "")))
    ls = unit("letterSpacing")
    if ls:
        parts.append(f"ls{ls}")
    m = _COLOR_CALL.search(args.get("color", ""))
    if m:
        c = _color_call_hex(m, short=True)
        if c != "Unspecified":
            parts.append(c)
    align = args.get("textAlign", "")
    if align and align not in ("null", "Unspecified"):
        parts.append(f"align={align}")
    deco = args.get("textDecoration", "")
    if deco and deco not in ("null", "None"):
        parts.append(_clean(deco))
    bg = _COLOR_CALL.search(args.get("background", ""))
    if bg and _color_call_hex(bg) != "Unspecified":
        parts.append(f"bg{_color_call_hex(bg, short=True)}")
    return " ".join(parts) if parts else "TextStyle()"


# Modifier elements whose arguments are worth keeping, and how to render them.
_MOD_KEEP_ARGS = ("padding", "absolutePadding", "size", "requiredSize", "width", "height",
                  "requiredWidth", "requiredHeight", "sizeIn", "widthIn", "heightIn",
                  "defaultMinSize", "offset", "absoluteOffset", "alpha", "zIndex", "weight",
                  "aspectRatio")


def _mod_element(el: str) -> str:
    name, args = _call_args(el)
    short = name.rsplit(".", 1)[-1]
    if short == "testTag":
        return f"testTag({args.get('tag', args.get('_0', ''))})"
    if short == "semantics":
        return "semantics(merge)" if args.get("mergeDescendants") == "true" else "semantics"
    if short in ("clickable", "combinedClickable", "toggleable", "selectable"):
        extra = []
        if args.get("enabled") == "false":
            extra.append("disabled")
        role = args.get("role", "")
        if role and role not in ("null", ""):
            extra.append(_clean(role))
        return f"{short}({','.join(extra)})" if extra else short
    if short == "background":
        m = _COLOR_CALL.search(args.get("color", ""))
        return f"background({_color_call_hex(m)})" if m else "background"
    if short.startswith("fillMax"):
        frac = args.get("fraction", "1.0")
        return short if frac in ("1.0", "1") else f"{short}({frac})"
    if short in _MOD_KEEP_ARGS and args:
        inner = args.get("paddingValues")
        if inner:
            _, args = _call_args(inner)
        kept = [f"{k}={_units(v)}" if not k.startswith("_") else _units(v)
                for k, v in args.items()
                if not re.fullmatch(r"-?0(?:\.0)?\.(?:dp|sp)|Unspecified|NaN", v.strip())]
        return f"{short}({','.join(kept)})" if kept else short
    return short


def modifier_brief(raw: str) -> str:
    """A modifier chain to element names plus key arguments.

    Handles the inspector's chain form ``a(args) → b → c(args)`` and the
    ``modifier`` parameter's list form ``[pkg.TestTagElement@..., ...]``
    (-> ``testTag``, ``fill``, ``composed``)."""
    v = raw.strip()
    if v.startswith("[") and v.endswith("]"):
        names = []
        for el in _split_top(v[1:-1]):
            m = re.search(r"([A-Z][\w]*?)(?:ModifierElement|Element|Modifier)?(?:@[0-9a-f]+)?$",
                          el.strip())
            if m:
                n = m.group(1)
                names.append(n[:1].lower() + n[1:])
        return ",".join(names)
    return ",".join(_mod_element(el) for el in _split_top(v, " → "))


def is_action_attr(raw: Any) -> bool:
    """A semantics attr whose value is a lambda (AccessibilityAction(... Function...)
    or a bare Function<>): it becomes an ``actions`` entry instead of a value."""
    return isinstance(raw, str) and _ACTION_ATTR.match(raw.strip()) is not None


def compose_value(key: str, raw: Any) -> str | None:
    """Normalize one Compose attr/param value; None means "drop it".

    Drops null/empty, ``Modifier``, TextUnit.Unspecified, Color.Unspecified (colour
    keys), NaN, ``tmpN_rcvr`` receivers and lambdas (kept as ``λ`` only for ``on*``
    callbacks and content slots). ``pkg.Class@hash`` becomes ``Class``;
    ``maxLines`` 2147483647 becomes ``inf``; int enums (``overflow`` 1) are decoded;
    TextStyle and modifier chains are briefed; packed colours become ``#AARRGGBB``;
    theme objects (Typography, ColorScheme, Shapes) become their type name. The
    result is capped at 120 chars.
    """
    if raw is None:
        return None
    v = str(raw).strip()
    if v in _DROP_VALUES or _TMP_RCVR.match(key):
        return None
    if v == TEXTUNIT_UNSPECIFIED:
        return None
    if _is_color_key(key):
        if v == COLOR_UNSPECIFIED:
            return None
        c = packed_color(v)
        if c:
            return c
    if _LAMBDA_VALUE.match(v):
        return LAMBDA if _is_slot_name(key) else None
    if key == "maxLines" and v == str(INT_MAX):
        return "inf"
    enum = _INT_ENUMS.get(key)
    if enum and v in enum:
        return enum[v]
    if v.startswith("TextStyle("):
        return cap(textstyle_brief(v))
    if v.startswith(_THEME_OBJECTS):
        return v.split("(", 1)[0]
    if key == "modifiers" or " → " in v or (key == "modifier" and v.startswith("[")):
        return cap(modifier_brief(v)) or None
    out = _clean(v)
    return cap(out) if out not in _DROP_VALUES else None


#: Semantics actions every Compose text node carries (text substitution plumbing).
BOILERPLATE_COMPOSE_ACTIONS = frozenset({
    "SetTextSubstitution", "ShowTextSubstitution", "ClearTextSubstitution",
})


def compose_attrs_brief(attrs: Mapping[str, Any] | None,
                        counts: dict[str, int] | None = None
                        ) -> tuple[dict[str, str], list[str]]:
    """Split and normalize a Compose node's attrs: ``(values, action_names)``.

    Lambda-valued semantics attrs go to ``action_names``, except the text
    substitution boilerplate (counted in ``counts["actions"]``). Values go through
    :func:`compose_value`; dropped ones are counted in ``counts["attrs"]``.
    """
    values: dict[str, str] = {}
    actions: list[str] = []
    for k, raw in (attrs or {}).items():
        if is_action_attr(raw):
            if k in BOILERPLATE_COMPOSE_ACTIONS:
                _bump(counts, "actions")
            else:
                actions.append(k)
            continue
        v = compose_value(k, raw)
        if v is None:
            _bump(counts, "attrs")
            continue
        values[k] = v
    return values, actions


# --------------------------------------------------------------------------- #
# Library vs app code
# --------------------------------------------------------------------------- #
def is_library_source(src: str | None) -> bool:
    """True when a slot's ``File.kt[:line]`` is in a Compose library file.

    An unknown source (None or empty) counts as library: it cannot be attributed
    to app code. The agent's package hash (R3) will replace this heuristic.
    """
    if not src:
        return True
    base = str(src).rsplit("/", 1)[-1].split(":", 1)[0]
    base = base.removesuffix(".kt")
    return base in LIBRARY_FILES


def origin_of(name: str | None, src: str | None) -> str:
    """``app`` when the source file is not a library file and the composable name
    starts with an upper-case letter, else ``library``."""
    if name and name[:1].isupper() and not is_library_source(src):
        return "app"
    return "library"


# --------------------------------------------------------------------------- #
# Accessibility
# --------------------------------------------------------------------------- #
#: Focus, selection and granularity actions every text node carries.
BOILERPLATE_ACTIONS = frozenset({
    "ACCESSIBILITY_FOCUS", "CLEAR_ACCESSIBILITY_FOCUS", "FOCUS", "CLEAR_FOCUS", "SELECT",
    "CLEAR_SELECTION", "NEXT_AT_MOVEMENT_GRANULARITY", "PREVIOUS_AT_MOVEMENT_GRANULARITY",
    "SET_SELECTION",
})
_SENTINEL_FIELDS = ("max_text_length", "text_selection_start", "text_selection_end",
                    "drawing_order")
_DEFAULT_TRUE_FLAGS = {"enabled": "disabled", "visible_to_user": "hidden"}


def a11y_node_brief(d: Mapping[str, Any], app_package: str | None,
                    counts: dict[str, int] | None = None) -> dict[str, Any]:
    """Brief form of one accessibility node (a11y_to_dict node or correlate facet).

    Non-recursive: ``children`` is not copied. Drops None values, ``-1``
    sentinels, ``package_name`` when it is the app's, ``actions_bitmask``, action
    ids (names kept), boilerplate focus/selection/granularity actions,
    ``movement_granularities``, empty ``SPANS_START_KEY`` extras, ``important``,
    ``important_for_accessibility`` when ``YES``, the ``is_virtual`` flag (implied
    by ``virtual_id``), unknown (-1) collection counts, and the default-true flags
    ``enabled``/``visible_to_user``, adding ``disabled``/``hidden`` instead. Bounds
    become ``[x, y, w, h]``. Dropped actions, extras and defaults are counted in
    ``counts``.
    """
    out: dict[str, Any] = {}
    raw_flags = [str(f) for f in (d.get("flags") or [])]
    lower = {f.lower() for f in raw_flags}
    for k, v in d.items():
        if v is None or k in ("children", "actions_bitmask", "important",
                              "movement_granularities", "flags"):
            continue
        if k in _SENTINEL_FIELDS and v == -1:
            continue
        if k == "package_name" and app_package and v == app_package:
            continue
        if k == "important_for_accessibility" and v in ("YES", 1):
            _bump(counts, "defaults")
            continue
        if k == "bounds":
            out["bounds"] = rect_list(v)
        elif k == "actions":
            names = []
            for a in v or []:
                name = a.get("name") if isinstance(a, Mapping) else str(a)
                if name in BOILERPLATE_ACTIONS:
                    _bump(counts, "actions")
                    continue
                names.append(name)
            if names:
                out["actions"] = names
        elif k == "extras":
            kept = {}
            for ek, ev in (v or {}).items():
                if str(ek).endswith("SPANS_START_KEY") and ev in ("[]", "", None):
                    _bump(counts, "extras")
                    continue
                kept[ek] = ev
            if kept:
                out["extras"] = kept
        elif k in ("collection_info", "collection_item_info") and isinstance(v, Mapping):
            out[k] = {ck: cv for ck, cv in v.items() if cv != -1 and cv is not False}
        else:
            out[k] = v
    flags = [f for f in raw_flags
             if f.lower() not in _DEFAULT_TRUE_FLAGS and f.lower() != "is_virtual"]
    for true_flag, marker in _DEFAULT_TRUE_FLAGS.items():
        if true_flag not in lower:
            flags.append(marker)
    if flags:
        out["flags"] = flags
    return out


# --------------------------------------------------------------------------- #
# View properties
# --------------------------------------------------------------------------- #
#: Curated "key" properties per class family (walks FAMILY_PARENTS like the defaults).
KEY_PROPS: dict[str, tuple[str, ...]] = {
    "View": ("visibility", "alpha", "enabled", "clickable", "longClickable", "focusable",
             "contentDescription", "importantForAccessibility", "paddingLeft", "paddingTop",
             "paddingRight", "paddingBottom", "minWidth", "minHeight", "layout_width",
             "layout_height", "layout_marginLeft", "layout_marginTop", "layout_marginRight",
             "layout_marginBottom", "layout_weight", "layout_gravity", "background"),
    "ViewGroup": ("clipChildren", "clipToPadding"),
    "LinearLayout": ("orientation", "gravity"),
    "FrameLayout": (),
    "ScrollView": ("fillViewport",),
    "TextView": ("text", "textSize", "textColor", "maxLines", "ellipsize", "hint", "gravity",
                 "lineHeight", "singleLine"),
    "Button": (),
    "EditText": ("inputType", "labelFor"),
    "CompoundButton": ("checked",),
    "ImageView": ("scaleType", "src", "adjustViewBounds"),
}

_NAME_FAMILIES = (
    (re.compile(r"(EditText|AutoCompleteTextView)$"), "EditText"),
    (re.compile(r"(Switch\w*|SwitchCompat|SwitchMaterial|CheckBox|RadioButton|ToggleButton|"
                r"CompoundButton|Chip)$"), "CompoundButton"),
    (re.compile(r"(ImageButton|ImageView|FloatingActionButton)$"), "ImageView"),
    (re.compile(r"Button$"), "Button"),
    (re.compile(r"TextView$"), "TextView"),
    (re.compile(r"(NestedScrollView|HorizontalScrollView|ScrollView)$"), "ScrollView"),
    (re.compile(r"LinearLayout$"), "LinearLayout"),
    (re.compile(r"(FrameLayout|DecorView)$"), "FrameLayout"),
    (re.compile(r"(Layout|ViewGroup|RecyclerView|ViewPager\d?|ComposeView|AndroidViewsHandler"
                r"|ViewFlipper|ViewAnimator)$"), "ViewGroup"),
)


def class_family(class_name: str | None, prop_names: Iterable[str] = ()) -> str:
    """Map a View to a defaults family, by its property set first (it reflects the
    real class hierarchy, custom classes included) and then its class name."""
    names = set(prop_names)
    simple = (class_name or "").rsplit(".", 1)[-1]
    by_name = next((fam for rx, fam in _NAME_FAMILIES if rx.search(simple)), None)
    if names:
        if "textSize" in names:
            if "checked" in names or "switchMinWidth" in names:
                return "CompoundButton"
            if by_name in ("EditText", "Button", "CompoundButton"):
                return by_name
            return "TextView"
        if {"scaleType", "adjustViewBounds", "cropToPadding"} & names:
            return "ImageView"
        if {"baselineAligned", "weightSum", "measureWithLargestChild"} & names:
            return "LinearLayout"
        if "fillViewport" in names:
            return "ScrollView"
        if "measureAllChildren" in names:
            return "FrameLayout"
        if "clipChildren" in names:
            return "ViewGroup"
        if by_name in (None, "TextView", "Button", "EditText", "CompoundButton", "ImageView"):
            return "View"
    return by_name or "View"


def _family_chain(family: str) -> list[str]:
    chain = []
    f: str | None = family
    while f is not None and f not in chain:
        chain.append(f)
        f = FAMILY_PARENTS.get(f)
    return chain


def family_group(family: str) -> str:
    """The widest defaults family below ``View`` that ``family`` belongs to
    (CompoundButton -> TextView, ScrollView -> ViewGroup): the peer group for a
    per-capture majority when a class alone is too rare to have one."""
    chain = _family_chain(family)
    return chain[-2] if len(chain) >= 2 else chain[0]


def static_default(family: str, name: str) -> tuple[bool, Any]:
    """``(found, default)`` for property ``name`` in ``family`` (walks parents)."""
    for fam in _family_chain(family):
        table = STATIC_VIEW_DEFAULTS.get(fam) or {}
        if name in table:
            return True, table[name]
    return False, None


def _eq(value: Any, default: Any) -> bool:
    alts = default if isinstance(default, tuple) else (default,)
    for alt in alts:
        if isinstance(alt, bool) or isinstance(value, bool):
            if isinstance(alt, bool) and isinstance(value, bool) and alt == value:
                return True
            continue
        if alt == value:
            return True
    return False


def is_static_default(family: str, name: str, value: Any,
                      bounds: list[int] | None = None) -> bool:
    """True when ``value`` is the family default of ``name`` (pivots: the centre of
    ``bounds``; derived font metrics always count as default)."""
    if name in DERIVED_PROPS:
        return True
    if name in ("transformPivotX", "transformPivotY") and bounds:
        centre = bounds[2] / 2.0 if name == "transformPivotX" else bounds[3] / 2.0
        try:
            return abs(float(value) - centre) < 0.51
        except (TypeError, ValueError):
            return False
    found, default = static_default(family, name)
    return found and _eq(value, default)


def key_props_for(family: str) -> tuple[str, ...]:
    out: list[str] = []
    for fam in reversed(_family_chain(family)):
        out.extend(p for p in KEY_PROPS.get(fam, ()) if p not in out)
    return tuple(out)


_CLASS_VALUE_TYPES = ("DRAWABLE", "ANIM", "ANIMATOR", "INTERPOLATOR", "OBJECT")
_QUALIFIED = re.compile(r"^(?:[a-z_][\w]*\.)+[A-Z][\w$]*$")


def prop_value(p: Mapping[str, Any]) -> Any:
    """One property dict (strings.py or legacy MCP shape) to its normalized value.

    COLOR ints -> ``#AARRGGBB``; GRAVITY/INT_FLAG -> their label when the agent
    sent one; resource dicts -> ``@ns:type/name``; floats rounded to float32
    precision; drawable/animator class names -> simple names. With a ``source`` or
    ``resolution_stack`` the value becomes ``{"value", "source"?, "stack"?}``.
    """
    t = p.get("type")
    v = p.get("value")
    if t == "COLOR" and isinstance(v, int) and not isinstance(v, bool):
        v = color_hex(v)
    elif t in ("GRAVITY", "INT_FLAG") and p.get("label") is not None:
        v = p["label"]
    elif isinstance(v, Mapping):
        v = resource_str(v)
    elif isinstance(v, float):
        v = float(f"{v:.7g}")  # the agent sends float32: 7 significant digits are exact
    elif t in _CLASS_VALUE_TYPES and isinstance(v, str) and _QUALIFIED.match(v):
        v = v.rsplit(".", 1)[-1]
    src, stack = p.get("source"), p.get("resolution_stack")
    if src or stack:
        out: dict[str, Any] = {"value": v}
        if src:
            out["source"] = src
        if stack:
            out["stack"] = list(stack)
        return out
    return v


def props_to_map(plist: Iterable[Mapping[str, Any]] | None) -> dict[str, Any]:
    """A property list to ``{name: normalized value}`` (first occurrence wins)."""
    out: dict[str, Any] = {}
    for p in plist or ():
        name = p.get("name")
        if name and name not in out:
            out[name] = prop_value(p)
    return out


def _plain(v: Any) -> Any:
    return v.get("value") if isinstance(v, Mapping) and "value" in v else v


def _vkey(v: Any) -> str:
    return json.dumps(_plain(v), sort_keys=True, default=str)


def _majorities(props: Mapping[int, Mapping[str, Any]], members: Mapping[str, list[int]],
                majority_min: int) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for group, vids in members.items():
        if len(vids) < majority_min:
            continue
        per_name: dict[str, Counter] = {}
        for vid in vids:
            for name, v in props[vid].items():
                if name not in MAJORITY_EXEMPT:
                    per_name.setdefault(name, Counter())[_vkey(v)] += 1
        out[group] = {name: c.most_common(1)[0][0] for name, c in per_name.items()
                      if c.most_common(1)[0][1] * 2 > len(vids)}
    return out


def nondefault_props(props: Mapping[int, Mapping[str, Any]], classes: Mapping[int, str], *,
                     bounds: Mapping[int, list[int]] | None = None,
                     majority_min: int = 3,
                     groups: Mapping[int, str] | None = None) -> tuple[dict, dict]:
    """Keep only non-default property values, per view.

    ``props`` is ``{view_id: {name: normalized value}}`` (see :func:`props_to_map`)
    and ``classes`` is ``{view_id: class name}``. A value is dropped when it equals
    the static default for the view's class family (pivots are checked against
    ``bounds``). It is also dropped when at least ``majority_min`` views share the
    class and more than half of them hold that value, unless the property is in
    ``MAJORITY_EXEMPT``.

    ``groups`` (optional, ``{view_id: group}``, e.g. :func:`family_group` of each
    view's family) is the fallback peer group: a view whose class has fewer than
    ``majority_min`` views uses the majority of its group instead, so values the
    theme gives every text view (hint and highlight colours, autofill flags) do
    not make a rare widget look customised. Without ``groups`` nothing changes.

    Returns ``(values, omitted)``: ``{view_id: {name: value}}`` and
    ``{view_id: number of dropped properties}``.
    """
    bounds = bounds or {}
    by_class: dict[str, list[int]] = {}
    for vid in props:
        by_class.setdefault(classes.get(vid) or "", []).append(vid)
    majority = _majorities(props, by_class, majority_min)
    group_majority: dict[str, dict[str, str]] = {}
    if groups:
        by_group: dict[str, list[int]] = {}
        for vid in props:
            if groups.get(vid):
                by_group.setdefault(groups[vid], []).append(vid)
        group_majority = _majorities(props, by_group, majority_min)
    values: dict = {}
    omitted: dict = {}
    for vid, pmap in props.items():
        cls = classes.get(vid) or ""
        family = class_family(cls, pmap.keys())
        maj = majority.get(cls)
        if maj is None:
            maj = group_majority.get((groups or {}).get(vid) or "", {})
        kept: dict[str, Any] = {}
        dropped = 0
        for name, v in pmap.items():
            plain = _plain(v)
            if is_static_default(family, name, plain, bounds.get(vid)) or \
                    maj.get(name) == _vkey(v):
                dropped += 1
                continue
            kept[name] = v
        values[vid] = kept
        omitted[vid] = dropped
    return values, omitted


__all__ = [
    "BOILERPLATE_ACTIONS",
    "BOILERPLATE_COMPOSE_ACTIONS",
    "KEY_PROPS",
    "LAMBDA",
    "VALUE_CAP",
    "a11y_node_brief",
    "cap",
    "class_family",
    "color_hex",
    "compose_attrs_brief",
    "compose_value",
    "family_group",
    "is_action_attr",
    "is_library_source",
    "is_static_default",
    "key_props_for",
    "modifier_brief",
    "nondefault_props",
    "origin_of",
    "packed_color",
    "prop_value",
    "props_to_map",
    "rect_list",
    "render_quad",
    "resource_str",
    "static_default",
    "textstyle_brief",
]
