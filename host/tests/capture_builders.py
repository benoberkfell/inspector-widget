"""Hand-made capture Indexes for offline tests.

The capture work packages that consume an Index (refs, query, analyzers, diff,
images, ops) test against these builders so they can proceed before the real index
builder (capture/index.py) exists. Everything here is plain model objects.

    b = IndexBuilder()
    w = b.window("n1", "DecorView", (0, 0, 1280, 2856), udid=1)
    v = b.view(w, "n2", "LinearLayout", (0, 0, 1280, 2856), udid=78)
    c = b.compose(v, "n7", sem_id=150, acv=82, b=(0, 0, 1280, 2856))
    ix = b.build()

``launcher_index()`` reproduces the real a11yprobe launcher as used by the spec's
examples (sections 5.3-5.9): the refs, labels, bounds, trees, issues and reading
order of ``capture()``, ``outline(root="n10")``, ``find(text="state")``,
``node("n22")`` and ``lint()``. ``wide_index()`` mirrors the 259-view wide scene
and ``big_index()`` builds an arbitrarily large synthetic screen.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

# host/ is on sys.path via tests/conftest.py.
from inspector_widget.capture.model import (
    CaptureMeta,
    CaptureOptions,
    Index,
    Issue,
    Tree,
    UNode,
    a11y_key,
    is_ref,
    ref_num,
    sem_key,
    slot_key,
    view_key,
    window_key,
)

Box = Sequence[int]

LAUNCHER_PACKAGE = "com.oberkfell.a11yprobe"
LAUNCHER_SERIAL = "emulator-5554"


class IndexBuilder:
    """Incrementally declares nodes, then ``build()`` derives trees, depths,
    windows, keys, aliases, a simple ``sel`` and the meta."""

    def __init__(self, capture_id: str = "c7h2kq", serial: str = LAUNCHER_SERIAL,
                 package: str = LAUNCHER_PACKAGE, *, pid: int = 4312, api: int = 37,
                 abi: str = "arm64-v8a", dpi: int = 480, font_scale: float = 1.0,
                 screen: tuple[int, int] = (1280, 2856), created_at: float = 1_790_000_000.0,
                 compose_generation: int = 0) -> None:
        self.meta = CaptureMeta(
            id=capture_id, lineage=(serial, package), pid=pid, api=api, abi=abi,
            agent_version="viewspector-0.1",
            device={"dpi": dpi, "font_scale": font_scale, "screen": list(screen),
                    "orientation": "portrait" if screen[1] >= screen[0] else "landscape"},
            created_at=created_at, took_ms=640, options=CaptureOptions(),
            compose_generation=compose_generation,
        )
        self.screen = screen
        self._nodes: dict[str, UNode] = {}
        self._order: list[str] = []
        self._parent: dict[str, str | None] = {}  # primary-tree parent (ui or slots)
        self._reading: list[str] = []
        self._aliases: dict[str, str] = {}
        self._auto = 1
        self._udid = 10_000

    # ------------------------------------------------------------------ ids
    def _new_ref(self, ref: str | None) -> str:
        """An explicit ref (validated) or the next free ``n<k>``; nothing is reserved
        until the node is actually added."""
        if ref is not None:
            if not is_ref(ref):
                raise ValueError(f"not a ref: {ref!r}")
            if ref in self._nodes:
                raise ValueError(f"duplicate ref {ref}")
            return ref
        k = self._auto
        while f"n{k}" in self._nodes:
            k += 1
        return f"n{k}"

    def _next_udid(self) -> int:
        self._udid += 1
        return self._udid

    def _add(self, parent: str | None, node: UNode) -> str:
        if parent is not None and parent not in self._nodes:
            raise KeyError(f"unknown parent {parent}")
        self._nodes[node.ref] = node
        self._order.append(node.ref)
        self._parent[node.ref] = parent
        self._auto = max(self._auto, ref_num(node.ref) + 1)
        return node.ref

    @staticmethod
    def _box(b: Box | None) -> list[int] | None:
        return None if b is None else [int(v) for v in b]

    def _acv_of(self, parent: str | None) -> int:
        """udid of the nearest AndroidComposeView at or above ``parent``."""
        p = parent
        while p is not None:
            n = self._nodes[p]
            if n.kind == "view" and (n.facets.get("view") or {}).get("class") == "AndroidComposeView":
                return int(n.ids["view"])
            if n.kind == "compose" and "sem" in n.ids:
                return int(str(n.ids["sem"]).split(":")[0])
            p = self._parent.get(p)
        raise ValueError("compose node needs acv= or an AndroidComposeView ancestor")

    # ------------------------------------------------------------------ nodes
    def window(self, ref: str | None = None, type: str = "DecorView",
               b: Box | None = None, *, z: int | None = None, udid: int | None = None,
               cls: str | None = None, qualified: str | None = None, **kw: Any) -> str:
        """A window root View (z defaults to the number of windows so far)."""
        z = sum(1 for n in self._nodes.values() if n.is_window) if z is None else z
        b = b if b is not None else (0, 0, self.screen[0], self.screen[1])
        ref = self.view(None, ref, type, b, udid=udid, cls=cls, qualified=qualified, **kw)
        node = self._nodes[ref]
        node.z = z
        self._aliases[window_key(int(node.ids["view"]))] = ref
        return ref

    def view(self, parent: str | None, ref: str | None = None, type: str | None = None,
             b: Box | None = None, *, udid: int | None = None, rid: str | None = None,
             cls: str | None = None, qualified: str | None = None, **kw: Any) -> str:
        ref = self._new_ref(ref)
        udid = self._next_udid() if udid is None else int(udid)
        cls = cls or type or "View"
        facet = {"class": cls}
        if qualified:
            facet["qualified"] = qualified
        facets = kw.pop("facets", {})
        facets.setdefault("view", facet)
        conf = kw.pop("conf", {})
        conf.setdefault("view", "exact")
        ids = kw.pop("ids", {})
        ids.setdefault("view", udid)
        node = UNode(key=view_key(udid), kind="view", ref=ref, type=type, rid=rid,
                     b=self._box(b), ids=ids, facets=facets, conf=conf, **kw)
        return self._add(parent, node)

    def compose(self, parent: str, ref: str | None = None, *, sem_id: int,
                b: Box | None = None, acv: int | None = None, type: str | None = None,
                attrs: dict[str, str] | None = None, actions: Sequence[str] | None = None,
                **kw: Any) -> str:
        """A Compose semantics node grafted under ``parent`` (keyed sem:<acv>:<id>)."""
        ref = self._new_ref(ref)
        acv = self._acv_of(parent) if acv is None else int(acv)
        facets = kw.pop("facets", {})
        cf = facets.setdefault("compose", {})
        if attrs is not None:
            cf["attrs"] = dict(attrs)
        if actions is not None:
            cf["actions"] = list(actions)
        conf = kw.pop("conf", {})
        conf.setdefault("compose", "exact")
        ids = kw.pop("ids", {})
        ids.setdefault("sem", f"{acv}:{int(sem_id)}")
        node = UNode(key=sem_key(acv, sem_id), kind="compose", ref=ref, type=type,
                     b=self._box(b), ids=ids, facets=facets, conf=conf, **kw)
        return self._add(parent, node)

    def slot(self, parent: str | None, ref: str | None = None, *, name: str,
             src: str | None, b: Box | None = None, acv: int = 0,
             origin: str | None = None, params: dict[str, str] | None = None,
             anchor: str | None = None, **kw: Any) -> str:
        """A slot-table group (composable call). Lives in the ``slots`` tree."""
        ref = self._new_ref(ref)
        ordinal = sum(1 for r in self._order if self._parent.get(r) == parent
                      and self._nodes[r].kind == "slot")
        anchor = anchor or f"{name}@{src}:{ordinal}"
        origin = origin or "app"
        facets = kw.pop("facets", {})
        sf = facets.setdefault("slot", {"name": name})
        if params is not None:
            sf["params"] = dict(params)
        ids = kw.pop("ids", {})
        if src:
            ids.setdefault("slot", f"{src}#{ordinal}")
        node = UNode(key=slot_key(acv, anchor), kind="slot", ref=ref, type=kw.pop("type", name),
                     b=self._box(b), src=src, origin=origin, anchor=anchor, ids=ids,
                     facets=facets, **kw)
        return self._add(parent, node)

    def a11y(self, parent: str, ref: str | None = None, *, host: int, virt: int,
             b: Box | None = None, cls: str = "android.view.View", **kw: Any) -> str:
        """An a11y-only node (no View/Compose twin, e.g. WebView content)."""
        ref = self._new_ref(ref)
        facets = kw.pop("facets", {})
        facets.setdefault("a11y", {"class": cls})
        conf = kw.pop("conf", {})
        conf.setdefault("a11y", "exact")
        ids = kw.pop("ids", {})
        ids.setdefault("a11y", f"{host}:{virt}")
        node = UNode(key=a11y_key(host, virt), kind="a11y", ref=ref, b=self._box(b),
                     ids=ids, facets=facets, conf=conf, **kw)
        return self._add(parent, node)

    # ------------------------------------------------------------------ decoration
    def a11y_facet(self, ref: str, *, host: int | None = None, virt: int | None = None,
                   conf: str = "exact", **facet: Any) -> None:
        node = self._nodes[ref]
        node.facets["a11y"] = dict(facet)
        node.conf["a11y"] = conf
        if host is not None and virt is not None:
            node.ids["a11y"] = f"{host}:{virt}"

    def issue(self, ref: str, rule: str, sev: str = "warn",
              evidence: dict[str, Any] | None = None, conf: str = "exact") -> None:
        self._nodes[ref].issues.append(Issue(id=rule, sev=sev, evidence=dict(evidence or {}),
                                             conf=conf))

    def link_slots(self, sem_ref: str, slot_refs: Sequence[str], conf: str = "inferred") -> None:
        sem = self._nodes[sem_ref]
        sem.facets.setdefault("compose", {})["slots"] = list(slot_refs)
        sem.conf["slot"] = conf
        for s in slot_refs:
            self._nodes[s].facets.setdefault("slot", {}).setdefault("sem", []).append(sem_ref)

    def reading(self, refs: Iterable[str]) -> None:
        """TalkBack stops in order; sets ``stop`` 1..N."""
        self._reading = list(refs)
        for i, r in enumerate(self._reading, 1):
            self._nodes[r].stop = i

    def alias(self, key: str, ref: str) -> None:
        self._aliases[key] = ref

    def node(self, ref: str) -> UNode:
        return self._nodes[ref]

    # ------------------------------------------------------------------ build
    def build(self) -> Index:
        nodes = self._nodes
        order = self._order
        kids: dict[str | None, list[str]] = {}
        for r in order:
            kids.setdefault(self._parent[r], []).append(r)

        ui_roots = sorted((r for r in kids.get(None, []) if nodes[r].is_window),
                          key=lambda r: nodes[r].z)
        slot_roots = [r for r in kids.get(None, []) if nodes[r].kind == "slot"]
        stray = [r for r in kids.get(None, []) if r not in ui_roots and r not in slot_roots]
        if stray:
            raise ValueError(f"parentless nodes must be windows or slots: {stray}")

        ui = Tree(roots=list(ui_roots))
        slots = Tree(roots=list(slot_roots))
        pre: list[str] = []

        def walk(r: str, depth: int, window: str | None, tree: Tree) -> None:
            n = nodes[r]
            n.depth = depth
            n.window = window
            n.parent = self._parent[r]
            n.children = list(kids.get(r, []))
            if n.children:
                tree.children[r] = list(n.children)
            pre.append(r)
            for c in n.children:
                walk(c, depth + 1, window, tree)

        for w in ui_roots:
            walk(w, 0, w, ui)
        for s in slot_roots:
            walk(s, 0, None, slots)
        # slot nodes inherit the window of a linked semantics node, if any
        for r in pre:
            n = nodes[r]
            if n.kind == "slot" and n.window is None:
                sems = (n.facets.get("slot") or {}).get("sem") or []
                if sems:
                    n.window = nodes[sems[0]].window

        def projected(members: Iterable[str]) -> Tree:
            member_set = set(members)
            t = Tree()
            for r in pre:
                if r not in member_set:
                    continue
                p = nodes[r].parent
                while p is not None and p not in member_set:
                    p = nodes[p].parent
                if p is None:
                    t.roots.append(r)
                else:
                    t.children.setdefault(p, []).append(r)
            return t

        ui_members = [r for r in pre if nodes[r].kind != "slot"]
        trees = {
            "ui": ui,
            "views": projected(r for r in ui_members if nodes[r].kind == "view"),
            "compose": projected(r for r in ui_members if nodes[r].kind == "compose"),
            "slots": slots,
            "a11y": projected(r for r in ui_members
                              if nodes[r].kind == "a11y" or "a11y" in nodes[r].facets),
        }

        # a simple unique locator: @tag, #rid, else the ref (the real one is C4's job)
        tags: dict[str, int] = {}
        rids: dict[str, int] = {}
        for n in nodes.values():
            if n.tag:
                tags[n.tag] = tags.get(n.tag, 0) + 1
            if n.rid:
                rids[n.rid] = rids.get(n.rid, 0) + 1
        for n in nodes.values():
            if n.sel:
                continue
            if n.tag and tags[n.tag] == 1:
                n.sel = f"@{n.tag}"
            elif n.rid and rids[n.rid] == 1:
                n.sel = f"#{n.rid}"
            else:
                n.sel = n.ref

        ordered = {r: nodes[r] for r in pre}
        by_key = {n.key: r for r, n in ordered.items()}
        by_key.update(self._aliases)
        return Index(meta=self.meta, nodes=ordered, trees=trees,
                     reading=list(self._reading), by_key=by_key, diagnostics=[])


# --------------------------------------------------------------------------- #
# The spec's launcher example (sections 5.3-5.9)
# --------------------------------------------------------------------------- #
#: (ref, sem id, testTag, label, y, h) of the launcher list items.
LAUNCHER_ITEMS: tuple[tuple[str, int, str, str, int, int], ...] = (
    ("n11", 327, "launch_all",
     "▶ All scenarios (lint everything), every BAD/GOOD variant on one scrollable screen", 348, 216),
    ("n12", 338, "launch_icon_button", "Icon button label, MissingContentDescription", 567, 216),
    ("n13", 349, "launch_touch_target", "Touch target size, AccessibilityTouchTarget", 786, 216),
    ("n14", 360, "launch_text_contrast", "Text contrast, AccessibilityTextContrast", 1005, 216),
    ("n15", 371, "launch_toggle_state", "Switch state desc, MissingStateDescription", 1224, 216),
    ("n16", 382, "launch_checkbox_state", "Checkbox state desc, MissingStateDescription", 1443, 216),
    ("n17", 393, "launch_image_label", "Image label, MissingContentDescription", 1662, 216),
    ("n18", 404, "launch_decorative_image", "Decorative image, RedundantDecorativeLabel", 1881, 216),
    ("n19", 415, "launch_custom_role", "Custom clickable role, MissingRole", 2100, 216),
    ("n20", 426, "launch_traversal", "Traversal order, BrokenTraversalOrder", 2319, 216),
    ("n21", 437, "launch_lazy_list", "List item semantics, BrokenTraversalOrder", 2538, 216),
    ("n22", 448, "launch_heading", "Section heading, MissingHeading", 2757, 27),
)

ROLE_RULE = "a11y.role.missing_on_clickable"
STATE_RULE = "a11y.state.not_exposed"
TOUCH_RULE = "a11y.touch_target.small"
CLIPPED_RULE = "render.clipped"


def launcher_index(capture_id: str = "c7h2kq") -> Index:
    """The real a11yprobe launcher (API 37, 1280x2856, 480 dpi) as the spec's
    examples show it: 8 views, 17 semantics nodes, 5 slot groups, 13 reading stops,
    14 lint warnings (12 role, 1 state, 1 touch_target) and one clipped row (n22).
    Refs follow the spec: n1 DecorView ... n6 AndroidComposeView, n9 "A11yProbe",
    n10 @launcher_list, n11..n22 the rows, n24/n25 the system bar views, n301..n305
    the slot groups of the "Section heading" row."""
    b = IndexBuilder(capture_id)
    W, H = b.screen
    full = (0, 0, W, H)
    n1 = b.window("n1", "DecorView", full, udid=1, qualified="com.android.internal.policy.DecorView")
    b.a11y_facet(n1, host=1, virt=-1, **{"class": "android.widget.FrameLayout"})
    n2 = b.view(n1, "n2", "LinearLayout", full, udid=78)
    b.view(n2, "n3", "ViewStub", (0, 0, 0, 0), udid=79, rid="action_mode_bar_stub",
           flags=["hidden"])
    n4 = b.view(n2, "n4", "FrameLayout", full, udid=80, rid="content")
    n5 = b.view(n4, "n5", "ComposeView", full, udid=81)
    n6 = b.view(n5, "n6", "AndroidComposeView", full, udid=82,
                qualified="androidx.compose.ui.platform.AndroidComposeView")

    n7 = b.compose(n6, "n7", sem_id=150, b=full)
    n8 = b.compose(n7, "n8", sem_id=313, b=(0, 0, W, 348))
    b.compose(n8, "n9", sem_id=317, b=(48, 210, 322, 84), type="TextView", label="A11yProbe",
              text="A11yProbe", attrs={"Text": "A11yProbe"})
    b.a11y_facet("n9", host=82, virt=317, speakable="A11yProbe",
                 **{"class": "android.widget.TextView"}, flags=["focus"])
    n23 = b.compose(n7, "n23", sem_id=310, b=full, flags=["tgroup"],
                    attrs={"IsTraversalGroup": "true"})
    n10 = b.compose(n23, "n10", sem_id=325, b=(0, 348, W, 2436), tag="launcher_list",
                    flags=["scroll"],
                    attrs={"TestTag": "launcher_list", "IsTraversalGroup": "true",
                           "VerticalScrollAxisRange":
                               "ScrollAxisRange(value=0.0, maxValue=100.0, reverseScrolling=false)",
                           "CollectionInfo": "CollectionInfo"},
                    actions=["ScrollBy", "ScrollByOffset", "IndexForKey", "ScrollToIndex",
                             "GetScrollViewportLength"])
    b.a11y_facet(n10, host=82, virt=325, flags=["scroll"], actions=["SCROLL_FORWARD"],
                 collection={"rows": -1, "cols": 1}, **{"class": "android.view.View"})

    for ref, sid, tag, label, y, h in LAUNCHER_ITEMS:
        b.compose(n10, ref, sem_id=sid, b=(0, y, W, h), tag=tag, label=label, text=label,
                  flags=["click"],
                  attrs={"TestTag": tag, "Text": label, "IsTraversalGroup": "true",
                         "Focused": "false"},
                  actions=["OnClick", "RequestFocus", "GetTextLayoutResult"])
        b.a11y_facet(ref, host=82, virt=sid, speakable=label, role=None,
                     flags=["click", "focus"], actions=["CLICK"],
                     **{"class": "android.view.View"})
        b.issue(ref, ROLE_RULE, "warn", {"has_onclick": True, "has_role": False})

    b.issue("n11", STATE_RULE, "warn", {"label": LAUNCHER_ITEMS[0][3]})
    n22 = b.node("n22")
    n22.declared_b = [0, 2757, W, 216]
    n22.visible = 0.125
    n22.src = "MainActivity.kt:150"
    b.issue("n22", CLIPPED_RULE, "warn",
            {"visible_px": 27, "declared_px": 216, "clipped_by": "n10", "edge": "bottom"},
            conf="inferred")
    b.issue("n22", TOUCH_RULE, "warn", {"w_dp": 426.7, "h_dp": 9.0, "min_dp": 48,
                                        "note": "likely false positive: clipped at scroll edge"})

    b.view(n1, "n24", "View", (0, 2784, W, 72), udid=76, rid="navigationBarBackground")
    b.view(n1, "n25", "View", (0, 0, W, 156), udid=77, rid="statusBarBackground",
           flags=["hidden"])

    # slot groups of the "Section heading" row (spec 5.7)
    s301 = b.slot(None, "n301", name="ListItem", src="MainActivity.kt:150",
                  b=(0, 2757, W, 216), acv=82, origin="app",
                  params={"modifier": "clickable,testTag"})
    s303 = b.slot(s301, "n303", name="Surface", src="ListItem.kt:163", b=(0, 2757, W, 216),
                  acv=82, origin="library")
    b.slot(s303, "n302", name="Text", src="MainActivity.kt:151", b=(48, 2799, 365, 72), acv=82,
           origin="app", text="Section heading",
           params={"text": "Section heading", "style": "16sp/24sp w400 ls0.5sp",
                   "overflow": "Clip", "maxLines": "inf"})
    b.slot(s303, "n305", name="Text", src="MainActivity.kt:152", b=(48, 2871, 313, 60), acv=82,
           origin="app", text="MissingHeading",
           params={"text": "MissingHeading", "style": "14sp/20sp w400 ls0.2sp"})
    b.slot(None, "n304", name="HorizontalDivider", src="MainActivity.kt:157",
           b=(0, 2973, W, 3), acv=82, origin="app", params={"thickness": "1.0"})
    b.link_slots("n22", ["n301", "n302", "n305"], conf="inferred")

    b.reading(["n9"] + [item[0] for item in LAUNCHER_ITEMS])
    return b.build()


# --------------------------------------------------------------------------- #
# Synthetic screens
# --------------------------------------------------------------------------- #
def wide_index(fan: int = 6, depth: int = 3, capture_id: str = "cw1de0") -> Index:
    """The 259-view wide scene as an Index: LinearLayouts to ``depth``, TextView
    leaves "Label N" #view_N, bounds [N, 3N, 300, 60], every node clickable."""
    b = IndexBuilder(capture_id, package="com.example", screen=(1280, 2856))
    counter = [0]

    def make(parent: str | None, level: int) -> str:
        counter[0] += 1
        n = counter[0]
        is_leaf = level >= depth
        kw: dict[str, Any] = {"flags": ["click", "focus"]}
        if is_leaf:
            kw.update(label=f"Label {n}", text=f"Label {n}")
        box = (n, 3 * n, 300, 60)
        if parent is None:
            ref = b.window(f"n{n}", "LinearLayout", box, udid=1000 + n, rid=f"view_{n}", **kw)
        else:
            ref = b.view(parent, f"n{n}", "TextView" if is_leaf else "LinearLayout", box,
                         udid=1000 + n, rid=f"view_{n}", **kw)
        b.a11y_facet(ref, host=1000 + n, virt=-1, speakable=kw.get("label"),
                     flags=["click", "focus"], actions=["CLICK"],
                     **{"class": "android.widget.TextView"})
        if not is_leaf:
            for _ in range(fan):
                make(ref, level + 1)
        return ref

    make(None, 0)
    return b.build()


def big_index(n: int = 5000, fan: int = 8, capture_id: str = "cb1g00") -> Index:
    """A breadth-first synthetic screen with exactly ``n`` nodes (views)."""
    b = IndexBuilder(capture_id, package="com.example")
    root = b.window("n1", "FrameLayout", (0, 0, 1280, 2856), udid=1, rid="root")
    queue = [root]
    made = 1
    while made < n:
        parent = queue.pop(0)
        for _ in range(fan):
            if made >= n:
                break
            made += 1
            y = (made * 37) % 2800
            leafish = made % 3 == 0
            ref = b.view(parent, f"n{made}", "TextView" if leafish else "LinearLayout",
                         (made % 1000, y, 120 + made % 400, 40 + made % 60), udid=made,
                         rid=f"v{made}", label=f"Item {made}" if leafish else None,
                         flags=["click"] if made % 5 == 0 else [])
            queue.append(ref)
    return b.build()
