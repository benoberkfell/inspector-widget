"""What a same-window overlay covers: one occlusion model for the static rules, the walk, the
scenarios and the capture.

A node is covered when something drawn after it, in its own window, draws over it: the
TalkBack user hears a stop nobody can see (tb.covered_stop), or walks out of a dialog or sheet
into the screen behind it (tb.escape). Windows under a modal window are a11y-core's business
(``covered_by`` on the window); this is the same-window half.

Drawn after: at the lowest common ancestor of the node and the overlay, the overlay's branch
comes later in drawing order (``drawing_order``, which the platform derives from Z and child
order, AccessibilityNodeInfo.getDrawingOrder), else in child order; the caller may know better
(``drawn_above``, the capture's View tree with each View's elevation). A Compose host reports
drawing order 0 (Compose builds the host's node itself), so it counts as drawn later than a
sibling with a known order only when it holds a clickable scrim over the node (a ComposeView
"dialog" over Views).

Draws: a View occludes only where it draws. It draws its whole box when the capture's View
properties give it a background or foreground (``draws``; a transparent colour or a ripple
does not count), when it takes touches (clickable, long-clickable or focusable: a scrim, a
sheet's root), or when it is a surface: most of its window (``SURFACE_AREA``) holding content
of its own (two or more nodes with text or actions: a bottom sheet, a fragment added over
another). Anything else draws only where its visible children do (text, an image, a control):
AntennaPod's empty loading FrameLayout (no background, its only child GONE) covers nothing,
while Thunderbird's action-mode bar covers the toolbar under it with its Done button, title
and actions.

What covers what (:class:`Cover` ``kind``):

``scrim``
    A clickable node over most of the window with no text of its own (a hand-made dialog's
    or a modal sheet's scrim).
``sheet``
    A surface (above) drawn over the node.
``drawer``
    An open DrawerLayout drawer: the DrawerLayout's scrim covers its whole content, also where
    the drawer does not reach (modal; DrawerLayout hides the content from accessibility too).
``bar``
    Anything smaller that draws over most of the node (``COVER_FRACTION`` of its box, its
    centre included): an ActionBarContextView over the toolbar (windowActionModeOverlay).

Nodes are read through an accessor (:class:`Access`), so the same code runs over the dump's
dicts (:class:`DictAccess`, the static rules and the capture) and over the walk's nodes
(:class:`NodeAccess`). Pure: no device.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

Rect = Tuple[int, int, int, int]  # x, y, w, h

SCRIM_AREA = 0.6      # a scrim covers at least this share of its window (static.SCRIM_AREA)
SURFACE_AREA = 0.4    # a surface (a sheet, a fragment over another) covers at least this
COVER_FRACTION = 0.6  # an overlay smaller than that covers a node only over this share of it
_SAMPLES = 5          # per axis: how a node's box is sampled against what an overlay draws
DRAWER = ("DrawerLayout",)
#: Drawables that draw nothing at rest (a ripple shows only while pressed).
_NOT_OPAQUE = ("RippleDrawable", "UnprojectedRipple", "RippleForeground", "RippleBackground")
_ACT = ("clickable", "long_clickable", "focusable")
#: Parents that lay their children out one after another (or are widgets, not containers):
#: their children never stack, so an overlap there is a transient, not an overlay.
_FLOW = frozenset({"RecyclerView", "ListView",
                   "GridView", "ScrollView", "HorizontalScrollView", "NestedScrollView",
                   "TableLayout", "TableRow", "RadioGroup", "ViewPager", "ViewPager2",
                   "TextView", "Button", "ImageView", "ImageButton", "CheckBox", "EditText",
                   "Switch", "ToggleButton", "RadioButton", "ProgressBar", "SeekBar"})
#: Classes that paint nothing of their own (a container, a plain View: a touch area).
_PLAIN = frozenset({"View", "ViewGroup", "FrameLayout", "LinearLayout", "RelativeLayout",
                    "ConstraintLayout", "CoordinatorLayout", "LinearLayoutCompat", "Space",
                    "ViewStub", "ComposeView", "AndroidComposeView", "AndroidViewsHandler"})
WEBVIEW = "android.webkit.WebView"


@dataclass
class Cover:
    """What covers one node: the overlay (the accessor's node), its key, what kind of overlay
    (``scrim``, ``sheet``, ``drawer``, ``bar``), its class (simple name), box, share of the
    window, pane title and resource id."""

    overlay: Any
    key: str
    kind: str
    cls: str
    rect: Rect
    area: float
    pane_title: Optional[str] = None
    rid: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"overlay": self.key, "kind": self.kind, "cls": self.cls,
                             "area": round(self.area, 2), "rect": list(self.rect)}
        if self.pane_title:
            d["pane_title"] = self.pane_title
        if self.rid:
            d["rid"] = self.rid
        return d


# ------------------------------------------------------------------------------- accessors
class Access:
    """How occlusion reads a node. Subclasses implement the getters."""

    def parent(self, n: Any) -> Any: raise NotImplementedError  # noqa: E704
    def children(self, n: Any) -> Sequence[Any]: raise NotImplementedError  # noqa: E704
    def rect(self, n: Any) -> Rect: raise NotImplementedError  # noqa: E704
    def flags(self, n: Any) -> frozenset: raise NotImplementedError  # noqa: E704
    def order(self, n: Any) -> int: raise NotImplementedError  # noqa: E704
    def cls(self, n: Any) -> str: raise NotImplementedError  # noqa: E704
    def key(self, n: Any) -> str: raise NotImplementedError  # noqa: E704
    def is_view(self, n: Any) -> bool: raise NotImplementedError  # noqa: E704
    def words(self, n: Any) -> str: raise NotImplementedError  # noqa: E704
    def text(self, n: Any) -> str: raise NotImplementedError  # noqa: E704
    def pane_title(self, n: Any) -> Optional[str]: return None  # noqa: E704
    def rid(self, n: Any) -> Optional[str]: return None  # noqa: E704
    def provider(self, n: Any) -> bool: return False  # noqa: E704

    def draws(self, n: Any) -> Optional[bool]:
        """Whether the View paints its box (a background or foreground), when known."""
        return None

    def ancestors(self, n: Any) -> Iterator[Any]:
        p = self.parent(n)
        while p is not None:
            yield p
            p = self.parent(p)

    def simple(self, n: Any) -> str:
        return self.cls(n).rsplit(".", 1)[-1].rsplit("$", 1)[-1]

    def visible(self, n: Any) -> bool:
        x, y, w, h = self.rect(n)
        return "visible_to_user" in self.flags(n) and w > 0 and h > 0


class DictAccess(Access):
    """The dump's own dicts (``a11y_to_dict`` nodes), every window root given, with an
    optional ``props(view id) -> {name: value}`` (the capture's View properties)."""

    def __init__(self, roots: Sequence[Dict[str, Any]],
                 props: Optional[Callable[[int], Any]] = None) -> None:
        self._parent: Dict[int, Optional[Dict[str, Any]]] = {}
        for r in roots:
            if not r:
                continue
            self._parent[id(r)] = None
            stack = [r]
            while stack:
                n = stack.pop()
                for c in n.get("children") or ():
                    self._parent[id(c)] = n
                    stack.append(c)
        self._props = props
        self._draws: Dict[int, Optional[bool]] = {}

    def parent(self, n: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._parent.get(id(n))

    def children(self, n: Dict[str, Any]) -> Sequence[Dict[str, Any]]:
        return n.get("children") or ()

    def rect(self, n: Dict[str, Any]) -> Rect:
        b = (n.get("bounds") or {}).get("layout") or n.get("bounds") or {}
        return (int(b.get("x", 0)), int(b.get("y", 0)), int(b.get("w", 0)), int(b.get("h", 0)))

    def flags(self, n: Dict[str, Any]) -> frozenset:
        return frozenset(n.get("flags") or ())

    def order(self, n: Dict[str, Any]) -> int:
        return int(n.get("drawing_order") or 0)

    def cls(self, n: Dict[str, Any]) -> str:
        return str(n.get("class_name") or "")

    def key(self, n: Dict[str, Any]) -> str:
        k = n.get("node_key")
        if k:
            return str(k)
        return f"view:{int(n.get('host_view_id') or 0)}"

    def is_view(self, n: Dict[str, Any]) -> bool:
        return int(n.get("virtual_id", -1)) == -1

    def words(self, n: Dict[str, Any]) -> str:
        return str(n.get("content_description") or n.get("text") or "")

    def text(self, n: Dict[str, Any]) -> str:
        return str(n.get("text") or "")

    def pane_title(self, n: Dict[str, Any]) -> Optional[str]:
        return n.get("pane_title") or None

    def rid(self, n: Dict[str, Any]) -> Optional[str]:
        r = n.get("view_id_resource_name")
        return str(r).rsplit("/", 1)[-1] if r else None

    def provider(self, n: Dict[str, Any]) -> bool:
        return bool(n.get("provider_class"))

    def draws(self, n: Dict[str, Any]) -> Optional[bool]:
        if self._props is None or not self.is_view(n) or not n.get("host_view_id"):
            return None
        k = id(n)
        if k not in self._draws:
            self._draws[k] = None
            try:
                p = self._props(int(n["host_view_id"]))
            except Exception:  # noqa: BLE001 - no properties for it: unknown
                p = None
            if p:
                self._draws[k] = paints(p.get("background")) or paints(p.get("foreground"))
        return self._draws[k]


class NodeAccess(Access):
    """The walk's nodes (:class:`.walk.Node`: ``parent``, ``children``, ``bounds``, ``flags``,
    ``drawing_order``, ``cls``, ``key``, ``virtual``, ``text``, ``cd``, ``pane_title``)."""

    def parent(self, n: Any) -> Any:
        return n.parent

    def children(self, n: Any) -> Sequence[Any]:
        return n.children

    def rect(self, n: Any) -> Rect:
        return tuple(n.bounds)  # type: ignore[return-value]

    def flags(self, n: Any) -> frozenset:
        return frozenset(n.flags)

    def order(self, n: Any) -> int:
        return int(n.drawing_order or 0)

    def cls(self, n: Any) -> str:
        return str(n.cls or "")

    def key(self, n: Any) -> str:
        return str(n.key)

    def is_view(self, n: Any) -> bool:
        return int(n.virtual) == -1

    def words(self, n: Any) -> str:
        return str(n.cd or n.text or "")

    def text(self, n: Any) -> str:
        return str(n.text or "")

    def pane_title(self, n: Any) -> Optional[str]:
        return n.pane_title or None

    def provider(self, n: Any) -> bool:
        return "compose" in str(n.cls or "").lower() or any(
            int(c.virtual) != -1 for c in n.children)


def paints(value: Any) -> bool:
    """Whether a background/foreground property value paints something: a colour with some
    alpha (``#AARRGGBB``), or a drawable that is not a bare ripple. Absent: no."""
    if value is None or value == "" or value is False:
        return False
    s = str(value)
    if s.startswith("#"):
        hexs = s[1:]
        if len(hexs) == 8:
            try:
                return int(hexs[:2], 16) > 0
            except ValueError:
                return True
        return True
    if isinstance(value, int):
        return (value >> 24) & 0xFF > 0
    return not any(s.endswith(r) for r in _NOT_OPAQUE)


# ------------------------------------------------------------------------------- geometry
def _area(r: Rect) -> int:
    return max(0, r[2]) * max(0, r[3])


def _inter(a: Rect, b: Rect) -> int:
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return max(0, w) * max(0, h)


def _clip(a: Rect, b: Rect) -> Rect:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return (x0, y0, max(0, x1 - x0), max(0, y1 - y0))


def _has(r: Rect, x: float, y: float) -> bool:
    return r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3]


def _samples(r: Rect) -> List[Tuple[float, float]]:
    x, y, w, h = r
    if w <= 0 or h <= 0:
        return [(x, y)]
    return [(x + w * (i + 0.5) / _SAMPLES, y + h * (j + 0.5) / _SAMPLES)
            for i in range(_SAMPLES) for j in range(_SAMPLES)]


# ------------------------------------------------------------------------------- the model
class Occlusion:
    """Same-window occlusion over one dump, through ``acc``. ``window_rect(node)`` gives the
    node's window box (default: its root's box). ``drawn_above(a, b)``: True when ``a`` is
    drawn over ``b``, False when under, None when the caller cannot tell (then drawing order
    and child order decide)."""

    def __init__(self, acc: Access, *, window_rect: Optional[Callable[[Any], Rect]] = None,
                 drawn_above: Optional[Callable[[Any, Any], Optional[bool]]] = None) -> None:
        self.acc = acc
        self._window_rect = window_rect
        self.drawn_above = drawn_above
        self._region: Dict[int, List[Rect]] = {}
        self._content: Dict[int, int] = {}
        # the last siblings compared repeat drawing orders: a dump that leaves not-important
        # Views out (an older agent) lists Views of several parents as siblings
        self._pruned = False

    # -- what a subtree draws ----------------------------------------------------------------
    def root(self, n: Any) -> Any:
        r = n
        for a in self.acc.ancestors(n):
            r = a
        return r

    def window(self, n: Any) -> Rect:
        if self._window_rect is not None:
            r = self._window_rect(n)
            if r is not None:
                return r
        return self.acc.rect(self.root(n))

    def content(self, n: Any) -> int:
        """How many visible nodes with text or actions the subtree holds (itself included)."""
        k = id(n)
        if k not in self._content:
            c = 0
            stack = [n]
            while stack:
                m = stack.pop()
                fl = self.acc.flags(m)
                if "visible_to_user" in fl and (self.acc.words(m) or set(_ACT) & fl):
                    c += 1
                stack.extend(self.acc.children(m))
            self._content[k] = c
        return self._content[k]

    def whole(self, o: Any) -> Optional[str]:
        """Why View ``o`` draws its whole box, or None: ``drawable`` (a background or
        foreground, from the View properties), ``surface`` (most of the window, holding
        content of its own) or ``touch`` (it takes touches over most of the window: a scrim,
        a sheet's root). A small clickable View with no drawable is a touch area over a
        control (Thunderbird's star_click_area), transparent: not whole."""
        acc = self.acc
        if not acc.visible(o) or acc.cls(o) == WEBVIEW:
            return None
        d = acc.draws(o)
        if d:
            return "drawable"
        big = _area(acc.rect(o)) >= SURFACE_AREA * max(1, _area(self.window(o)))
        if not big:
            return None
        if acc.is_view(o) and not acc.provider(o) and "scrollable" not in acc.flags(o) \
                and self.content(o) >= 2:
            return "surface"
        if set(_ACT) & acc.flags(o) and d is not False:
            return "touch"
        return None

    def region(self, o: Any) -> List[Rect]:
        """The boxes subtree ``o`` draws over: its own box when it draws whole
        (:meth:`whole`), else what its visible children draw. A leaf draws its box when it
        shows something: text, an image or a widget (a button, a box); a plain View or
        container with only a description (a touch area) draws nothing."""
        k = id(o)
        if k in self._region:
            return self._region[k]
        acc = self.acc
        out: List[Rect] = []
        if acc.visible(o):
            if self.whole(o):
                out = [acc.rect(o)]
            else:
                kids = acc.children(o)
                for c in kids:
                    out.extend(self.region(c))
                if self._shows(o, bool(kids)):
                    out.append(acc.rect(o))
        self._region[k] = out
        return out

    def _shows(self, n: Any, parent: bool) -> bool:
        """Whether node ``n`` paints its own box: text (a View's or a Compose node's), an
        image, or a widget class; not a container or a plain View (a description alone
        says what it is, not that it draws)."""
        acc = self.acc
        if acc.text(n):
            return True
        simple = acc.simple(n)
        if not acc.is_view(n):
            return not parent and bool(acc.words(n))  # a Compose leaf with a description
        if parent or simple in _PLAIN:
            return False
        return True

    # -- drawn after -------------------------------------------------------------------------
    def _later(self, child: Any, sibs: Sequence[Any], n: Any) -> List[Any]:
        """The siblings of ``child`` drawn after it (``n``: the covered candidate). Drawing
        orders decide when the siblings' are distinct; a dump that leaves not-important Views
        out (an older agent) lists Views of several parents as siblings, whose orders repeat
        and do not compare: then child order does. A Compose host reports order 0 (Compose
        builds its node itself): it is drawn over Views when it holds a dialog's scrim (a
        ComposeView "dialog" over Views), under them otherwise."""
        acc = self.acc
        pos = next((i for i, c in enumerate(sibs) if c is child), len(sibs))
        orders = [acc.order(c) for c in sibs if acc.order(c)]
        self._pruned = len(set(orders)) != len(orders)
        och = acc.order(child)
        out = []
        for i, c in enumerate(sibs):
            if c is child:
                continue
            above = self.drawn_above(c, child) if self.drawn_above is not None else None
            if above is None:
                oc = acc.order(c)
                if not oc and och:
                    above = self._holds_scrim(c, n)
                elif oc and not och and orders:
                    above = not self._dialog(child)
                elif orders:
                    above = oc > och
                else:
                    above = i > pos
            if above:
                out.append(c)
        return out

    def _dialog(self, o: Any) -> bool:
        """Whether ``o``'s subtree holds a clickable node over most of the window (a scrim)."""
        win = max(1, _area(self.window(o)))
        stack = [o]
        while stack:
            m = stack.pop()
            if "clickable" in self.acc.flags(m) and self.acc.visible(m) \
                    and _area(self.acc.rect(m)) >= SURFACE_AREA * win:
                return True
            stack.extend(self.acc.children(m))
        return False

    def _holds_scrim(self, o: Any, n: Any) -> bool:
        """A clickable node in ``o``'s subtree over most of the window and over ``n``'s
        centre (a Compose host's dialog scrim)."""
        x, y, w, h = self.acc.rect(n)
        cx, cy = x + w / 2, y + h / 2
        win = max(1, _area(self.window(n)))
        stack = [o]
        while stack:
            m = stack.pop()
            if "clickable" in self.acc.flags(m) and self.acc.visible(m) \
                    and _area(self.acc.rect(m)) >= SURFACE_AREA * win \
                    and _has(self.acc.rect(m), cx, cy):
                return True
            stack.extend(self.acc.children(m))
        return False

    # -- the answer --------------------------------------------------------------------------
    def covered_by(self, n: Any) -> Optional[Cover]:
        """What covers node ``n`` in its window, or None."""
        acc = self.acc
        nr = self.visible_box(n)
        if _area(nr) <= 0:
            return None  # nothing of it shows: a ghost (offscreen, zero size), not covered
        x, y, w, h = nr
        cx, cy = x + w / 2, y + h / 2
        win = self.window(n)
        child = n
        for parent in acc.ancestors(n):
            sibs = list(acc.children(parent))
            if acc.simple(parent) in DRAWER and sibs and child is sibs[0]:
                drawer = self._open_drawer(parent, sibs)
                if drawer is not None:
                    return self._cover(drawer, "drawer", win)
            if acc.is_view(parent) and acc.simple(parent) in _FLOW:
                child = parent  # laid out one after another: siblings never stack there
                continue
            # quick reject: a View draws inside its own box (it clips its children)
            near = [o for o in sibs if o is not child and _has(acc.rect(o), cx, cy)]
            if not near:
                child = parent
                continue
            for o in self._later(child, sibs, n):
                if not any(o is m for m in near):
                    continue
                kind = self._covers(o, n, nr, cx, cy, win)
                if kind == "bar" and self._pruned and self.drawn_above is None:
                    continue  # orders of Views from several parents: only a surface is sure
                if kind is not None:
                    return self._cover(o, kind, win)
            child = parent
        return None

    def visible_box(self, n: Any) -> Rect:
        """``n``'s box clipped to the WebView it is web content of (Chromium reports the whole
        page, its off-screen part included) and to its window."""
        acc = self.acc
        r = acc.rect(n)
        for a in acc.ancestors(n):
            if acc.cls(a) == WEBVIEW and acc.is_view(a):
                r = _clip(r, acc.rect(a))
                break
        return _clip(r, self.window(n))

    def _open_drawer(self, dl: Any, sibs: Sequence[Any]) -> Optional[Any]:
        acc = self.acc
        r = acc.rect(dl)
        for d in sibs[1:]:
            if acc.visible(d) and _inter(acc.rect(d), r) >= 0.1 * max(1, _area(r)) \
                    and self.content(d) >= 1:
                return d
        return None

    def _covers(self, o: Any, n: Any, nr: Rect, cx: float, cy: float,
                win: Rect) -> Optional[str]:
        acc = self.acc
        if not acc.visible(o):
            return None
        if not acc.is_view(o):
            # virtual content (Compose, a web page) over its siblings: only a scrim covers;
            # web elements overlap one another all the time (inline links)
            return "scrim" if self._holds_scrim(o, n) else None
        warea = max(1, _area(win))
        why = self.whole(o)
        boxes = self.region(o)
        if not boxes:
            return None
        pts = _samples(nr)
        hit = sum(1 for p in pts if any(_has(b, *p) for b in boxes))
        if hit < COVER_FRACTION * len(pts) or not any(_has(b, cx, cy) for b in boxes):
            return None
        orect = acc.rect(o)
        share = _area(orect) / warea
        if why == "touch" and share >= SCRIM_AREA and not acc.words(o):
            return "scrim"
        if why in ("surface", "drawable", "touch") and share >= SURFACE_AREA:
            return "sheet"
        if why is None and self._holds_scrim(o, n):
            return "scrim"
        return "bar"

    def _cover(self, o: Any, kind: str, win: Rect) -> Cover:
        acc = self.acc
        r = acc.rect(o)
        return Cover(o, acc.key(o), kind, acc.simple(o), r,
                     _area(r) / max(1, _area(win)), acc.pane_title(o), acc.rid(o))


def for_dump(dump: Any, *, props: Optional[Callable[[int], Any]] = None,
             drawn_above: Optional[Callable[[Any, Any], Optional[bool]]] = None
             ) -> Tuple[Occlusion, List[Tuple[int, Dict[str, Any]]]]:
    """An :class:`Occlusion` over an ``a11y_to_dict`` dump (``{"windows": [...]}``) and its
    ``[(window index, root dict)]``; each window's box is its frame, else its root's box."""
    wins = [(i, w) for i, w in enumerate(dump.get("windows") or []) if w.get("root")]
    roots = [w["root"] for _i, w in wins]
    acc = DictAccess(roots, props)
    frames: Dict[int, Rect] = {}
    for _i, w in wins:
        f = w.get("frame")
        if isinstance(f, dict):
            frames[id(w["root"])] = (int(f["x"]), int(f["y"]), int(f["w"]), int(f["h"]))

    occ = Occlusion(acc, drawn_above=drawn_above)

    def window_rect(n: Any) -> Rect:
        r = occ.root(n)
        return frames.get(id(r)) or acc.rect(r)

    occ._window_rect = window_rect
    return occ, [(i, w["root"]) for i, w in wins]


__all__ = ["COVER_FRACTION", "Cover", "DictAccess", "NodeAccess", "Occlusion", "SCRIM_AREA",
           "SURFACE_AREA", "for_dump", "paints"]
