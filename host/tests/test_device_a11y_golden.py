"""Golden accessibility expectations for the A11yProbe corpus, run on a live device.

A11yProbe (``testapps/a11yprobe``, package ``com.oberkfell.a11yprobe``) renders
matched GOOD/BAD pairs: Compose scenarios (``ScenarioRegistry.kt``), a classic-View
screen (``activity_view_scenarios.xml``) and mixed View/Compose hierarchies plus
dialog windows (``InteropFragment.kt``). Every BAD node is a deliberate defect.
For each scenario this module:

1. force-stops the app and launches the scenario directly by intent extra,
2. attaches through the host API (``inspector_widget.attach``) and dumps the
   unified a11y tree, the Compose layer and the View tree,
3. runs the lint the MCP ``a11y_lint`` tool runs (``mcp_server.tool_a11y_lint``),
4. asserts the golden expectations written from the scenario definitions:
   every BAD node is flagged with the right rule id, GOOD nodes are not, a11y
   node keys are unique and follow the ID contract, the Compose windows are
   exactly the AndroidComposeViews in the View tree, per-row repeated labels are
   not reported as duplicates, and the reading order of a known screen.

Nodes are located by resource name: View ids (``...:id/badImageButton``) and
Compose testTags (A11yProbe turns on ``testTagsAsResourceId``). A finding is
tied to a node by its node key when the finding carries one (``view:<id>`` /
``compose:<acv>:<semanticsId>``), otherwise by geometry (the same on-screen
element, allowing for touch-target expansion). The expectation tables below
mirror the app source; keep them in sync.

Marked ``device``; skipped unless ``$ANDROID_SERIAL`` names an attached device
with a current A11yProbe build installed and the agent artifacts in build-out/::

    scripts/build.sh && scripts/install-a11yprobe.sh "$ANDROID_SERIAL" --no-launch
    cd host && ANDROID_SERIAL=emulator-5556 .venv/bin/python -m pytest \\
        tests/test_device_a11y_golden.py -q -m device

The flawed list rows all sit in the first six rows, so a phone-sized display
(at least ~700dp tall) shows every expected node without scrolling.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pytest

pytestmark = pytest.mark.device

PACKAGE = "com.oberkfell.a11yprobe"
SERIAL = (os.environ.get("ANDROID_SERIAL") or "").strip() or None

# --------------------------------------------------------------------------- #
# Rule ids (inspector_widget.a11y_lint) and their R-number aliases.
# --------------------------------------------------------------------------- #
R1 = "a11y.label.missing"
R2 = "a11y.touch_target.small"
R3 = "a11y.contrast.low"
R4 = "a11y.label.redundant"
R5 = "a11y.role.missing_on_clickable"
R6 = "a11y.image.no_description"
R7 = "a11y.state.not_exposed"
R12 = "a11y.duplicate.label"
FORM_LABEL = "a11y.form.*"              # a dedicated form-label rule, if one is added
DUPLICATE_BOUNDS = "a11y.*duplicate*bound*"  # a DuplicateClickableBounds rule, if one is added

RULE_ALIASES = {
    "R1": R1, "R2": R2, "R3": R3, "R4": R4, "R5": R5, "R6": R6, "R7": R7,
    "R8": "a11y.node.empty_focusable", "R9": "a11y.heading.structure",
    "R10": "a11y.grouping.missing", "R11": "a11y.text.fixed_scaling", "R12": R12,
}


# --------------------------------------------------------------------------- #
# Expectation model.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Expect:
    """One node, found by resource name ``tag`` (optionally inside list row ``row``).

    In ``Golden.bad``: at least one finding whose rule matches one of ``rules``
    (exact ids or fnmatch patterns) must be tied to the node. In ``Golden.good``:
    no finding whose rule matches any of ``rules`` may be tied to it.
    """

    tag: str
    rules: Tuple[str, ...]
    row: Optional[str] = None

    def label(self) -> str:
        return f"{self.tag} in row {self.row!r}" if self.row else self.tag


def E(tag: str, *rules: str, row: Optional[str] = None) -> Expect:
    return Expect(tag, tuple(rules), row)


@dataclass(frozen=True)
class Golden:
    sid: str                              # pytest id
    activity: str                         # component class, relative to PACKAGE
    extra: Optional[str]                  # value for `--es scenario`, if any
    anchor: str                           # resource name or text that proves the screen is up
    bad: Tuple[Expect, ...] = ()
    good: Tuple[Expect, ...] = ()
    compose_views: Tuple[int, Optional[int]] = (0, None)  # min/max AndroidComposeViews
    min_windows: int = 1                  # a11y windows expected (dialogs add one)
    contrast: bool = False                # sample pixels for R3 (slow)
    checks: Tuple[Callable[["Capture"], None], ...] = ()
    per_row_labels: Tuple[str, ...] = ()  # repeated per-row labels (legit duplicates)


# --------------------------------------------------------------------------- #
# A captured scenario: every dump the assertions need, taken once per scenario.
# --------------------------------------------------------------------------- #
@dataclass
class Capture:
    golden: Golden
    a11y: Dict[str, Any]
    lint: Dict[str, Any]
    compose: Dict[str, Any]
    tree: Dict[str, Any]
    nodes: List[Dict[str, Any]] = field(default_factory=list)
    parent: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    window_of: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    by_key: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for w in self.a11y.get("windows") or []:
            root = w.get("root")
            if not root:
                continue
            stack = [(root, None)]
            while stack:
                n, p = stack.pop()
                self.nodes.append(n)
                self.window_of[id(n)] = root
                if p is not None:
                    self.parent[id(n)] = p
                for c in reversed(n.get("children") or []):
                    stack.append((c, n))
        for n in self.nodes:
            for k in node_keys(n):
                self.by_key.setdefault(k, n)

    @property
    def findings(self) -> List[Dict[str, Any]]:
        return list(self.lint.get("findings") or [])

    def ancestors(self, n: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = []
        p = self.parent.get(id(n))
        while p is not None:
            out.append(p)
            p = self.parent.get(id(p))
        return out

    def subtree(self, n: Dict[str, Any]) -> List[Dict[str, Any]]:
        out, stack = [], [n]
        while stack:
            m = stack.pop()
            out.append(m)
            stack.extend(reversed(m.get("children") or []))
        return out

    def focus_entry(self, n: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The focus_order entry for node ``n``, or None when ``n`` is not a focus stop.

        focus_order lists only the stops, compactly: {order, key, id, speak, unlabeled?,
        window?}; ``id`` is the node's host key, unique per dump (ID contract).
        """
        for e in self.a11y.get("focus_order") or []:
            if e.get("id") == n.get("id"):
                return e
        return None


# --------------------------------------------------------------------------- #
# Node / finding helpers.
# --------------------------------------------------------------------------- #
def node_keys(n: Dict[str, Any]) -> Set[str]:
    """Host node keys of an a11y node (ID contract): view:<id> or compose:<acv>:<sem>."""
    hv, vid = n.get("host_view_id"), n.get("virtual_id")
    if hv is None or vid is None:
        return set()
    if vid == -1:
        return {f"view:{hv}"}
    return {f"compose:{hv}:{vid}"}


def resource_matches(name: Optional[str], tag: str) -> bool:
    return bool(name) and (name == tag or name.endswith(":id/" + tag) or name.endswith("/" + tag))


def rect(b: Any) -> Optional[Dict[str, int]]:
    """Normalise {x,y,w,h} | {layout:{...}} | None to a rect with positive area."""
    if not isinstance(b, dict):
        return None
    if "layout" in b and isinstance(b["layout"], dict):
        b = b["layout"]
    try:
        r = {k: int(b.get(k, 0)) for k in ("x", "y", "w", "h")}
    except (TypeError, ValueError):
        return None
    return r if r["w"] > 0 and r["h"] > 0 else None


def center(r: Dict[str, int]) -> Tuple[float, float]:
    return r["x"] + r["w"] / 2.0, r["y"] + r["h"] / 2.0


def contains_point(r: Dict[str, int], p: Tuple[float, float]) -> bool:
    return r["x"] <= p[0] <= r["x"] + r["w"] and r["y"] <= p[1] <= r["y"] + r["h"]


def contains_rect(outer: Dict[str, int], inner: Dict[str, int], slop: int = 2) -> bool:
    return (inner["x"] >= outer["x"] - slop and inner["y"] >= outer["y"] - slop
            and inner["x"] + inner["w"] <= outer["x"] + outer["w"] + slop
            and inner["y"] + inner["h"] <= outer["y"] + outer["h"] + slop)


def same_element(a: Optional[Dict[str, int]], b: Optional[Dict[str, int]]) -> bool:
    """Two rects describe the same on-screen element.

    Each centre lies inside the other rect and the areas are within 5x of each
    other, which accepts a 40dp layout box vs its 48dp touch bounds (or a 24dp
    Checkbox glyph vs its 48dp target) but rejects a row vs a label inside it.
    """
    if not a or not b:
        return False
    if not (contains_point(a, center(b)) and contains_point(b, center(a))):
        return False
    area_a, area_b = a["w"] * a["h"], b["w"] * b["h"]
    return min(area_a, area_b) / max(area_a, area_b) >= 0.2


def finding_rule(f: Dict[str, Any]) -> str:
    rule = str(f.get("rule") or f.get("rule_id") or f.get("id") or "")
    return RULE_ALIASES.get(rule, rule)


def rule_matches(rule: str, patterns: Iterable[str]) -> bool:
    return any(rule == p or fnmatch.fnmatchcase(rule, p) for p in patterns)


_KEY_RE = re.compile(r"^(view|compose|composeview):")


def finding_keys(f: Dict[str, Any]) -> Set[str]:
    node = f.get("node") if isinstance(f.get("node"), dict) else {}
    cands: List[Any] = [f.get("node_key"), f.get("key"), node.get("node_key"),
                        node.get("key"), node.get("id")]
    for extra in (f.get("keys"), node.get("keys")):
        if isinstance(extra, (list, tuple)):
            cands.extend(extra)
    out = set()
    for c in cands:
        if isinstance(c, str) and _KEY_RE.match(c):
            out.add(c.replace("composeview:", "view:", 1) if c.startswith("composeview:") else c)
    return out


def finding_label(f: Dict[str, Any]) -> str:
    ev = f.get("evidence") or {}
    node = f.get("node") if isinstance(f.get("node"), dict) else {}
    return str(ev.get("label") or node.get("label") or node.get("name") or "")


def describe(n: Dict[str, Any]) -> str:
    return (f"{sorted(node_keys(n)) or n.get('id')} {n.get('class_name')} "
            f"res={n.get('view_id_resource_name')!r} speakable={n.get('speakable')!r} "
            f"bounds={rect(n.get('bounds'))}")


def describe_finding(f: Dict[str, Any]) -> str:
    return (f"{finding_rule(f)}/{f.get('severity')} keys={sorted(finding_keys(f))} "
            f"node={f.get('node')} bounds={rect(f.get('bounds'))}")


# --------------------------------------------------------------------------- #
# Locating nodes.
# --------------------------------------------------------------------------- #
def find_all(nodes: Iterable[Dict[str, Any]], tag: str) -> List[Dict[str, Any]]:
    return [n for n in nodes if resource_matches(n.get("view_id_resource_name"), tag)]


def find_text(nodes: Iterable[Dict[str, Any]], text: str) -> List[Dict[str, Any]]:
    return [n for n in nodes if text in (n.get("text"), n.get("content_description"))]


_ROW_ROOTS = ("view_cell_root", "hybrid_cell_root")


def row_scope(cap: Capture, title: str) -> Dict[str, Any]:
    """The list-row node whose title text is ``title`` ("Item 3 (view)")."""
    hits = find_text(cap.nodes, title)
    assert hits, f"row {title!r} is not in the a11y tree (off screen, or the list did not bind)"
    m = re.match(r"Item (\d+) ", title)
    compose_tag = f"cell_{m.group(1)}" if m else None
    for anc in cap.ancestors(hits[0]):
        res = anc.get("view_id_resource_name")
        if any(resource_matches(res, t) for t in _ROW_ROOTS) or (compose_tag and resource_matches(res, compose_tag)):
            return anc
    raise AssertionError(f"row {title!r}: no row container (cell_<n> / view_cell_root / "
                         f"hybrid_cell_root) above {describe(hits[0])}")


def locate(cap: Capture, e: Expect) -> Dict[str, Any]:
    scope = cap.subtree(row_scope(cap, e.row)) if e.row else cap.nodes
    hits = find_all(scope, e.tag)
    assert hits, (f"{e.label()}: no a11y node with resource name {e.tag!r} "
                  f"(is testTagsAsResourceId on / is it on screen?)")
    return hits[0]


def _family(cap: Capture, a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """a is b, or an untagged parent/child helper node of the same element."""
    if a is b:
        return True
    related = any(x is b for x in cap.ancestors(a)) or any(x is a for x in cap.ancestors(b))
    if not related:
        return False
    if a.get("view_id_resource_name") and b.get("view_id_resource_name"):
        return False
    return same_element(rect(a.get("bounds")), rect(b.get("bounds")))


def finding_hits(cap: Capture, f: Dict[str, Any], n: Dict[str, Any]) -> bool:
    """Is finding ``f`` about node ``n``? By node key when it resolves, else by geometry."""
    resolved = [cap.by_key[k] for k in finding_keys(f) if k in cap.by_key]
    if resolved:
        return any(_family(cap, r, n) for r in resolved)
    return same_element(rect(f.get("bounds")), rect(n.get("bounds")))


# --------------------------------------------------------------------------- #
# Device driving.
# --------------------------------------------------------------------------- #
def _adb_shell(cmd: str, timeout: float = 60.0) -> str:
    from inspector_widget import adb
    return adb.shell(SERIAL, cmd, timeout=timeout)


def _launch(g: Golden) -> None:
    _adb_shell(f"am force-stop {PACKAGE}")
    cmd = f"am start -W -n {PACKAGE}/{g.activity}"
    if g.extra:
        cmd += f" --es scenario {g.extra}"
    out = _adb_shell(cmd)
    assert "Error" not in out, f"launch failed: {cmd}\n{out}"


def _attach():
    import inspector_widget
    deadline, last = time.time() + 30, None
    while time.time() < deadline:
        try:
            return inspector_widget.attach(SERIAL, PACKAGE)
        except Exception as e:  # the process may still be starting
            last = e
            time.sleep(1.0)
    raise AssertionError(f"could not attach to {PACKAGE} on {SERIAL}: {last}")


def _dump_a11y(session) -> Dict[str, Any]:
    from inspector_widget import a11y as a11ymod
    return a11ymod.a11y_to_dict(session.dump_a11y(root_id=0, include_extras=True))


def _settle(session, g: Golden) -> None:
    """Wait until the anchor is in the a11y tree and two dumps agree on the node count."""
    deadline, prev, last = time.time() + 20, None, None
    while time.time() < deadline:
        cap = Capture(g, _dump_a11y(session), {}, {}, {})
        last = cap
        present = find_all(cap.nodes, g.anchor) or find_text(cap.nodes, g.anchor)
        windows = [w for w in cap.a11y.get("windows") or [] if w.get("root")]
        if present and len(windows) >= g.min_windows:
            if prev == len(cap.nodes):
                return
            prev = len(cap.nodes)
        time.sleep(0.5)
    shown = sorted({n.get("view_id_resource_name") or n.get("text") or "" for n in (last.nodes if last else [])})
    raise AssertionError(f"{g.sid}: anchor {g.anchor!r} never appeared (or the tree kept changing). "
                         f"Seen: {shown[:40]}")


def _forget_mcp_session() -> None:
    import mcp_server
    try:
        mcp_server.tool_detach(SERIAL, PACKAGE)
    except Exception:
        pass


def capture(g: Golden) -> Capture:
    import mcp_server
    from inspector_widget import strings as st

    _forget_mcp_session()
    _launch(g)
    session = _attach()
    try:
        _settle(session, g)
        # Lint first: the MCP tool's Compose dump may recompose (re-minting
        # semantics ids), so every dump below sees the ids the findings refer to.
        lint = mcp_server.tool_a11y_lint(SERIAL, PACKAGE, include_contrast=g.contrast, scale=1.0)
        compose = st.dump_compose_to_dict(session.dump_compose(
            include_semantics=True, include_slot_table=False, enable_inspection=False))
        tree = st.dump_tree_to_dict(session.dump_tree())
        a11y = _dump_a11y(session)
    finally:
        _forget_mcp_session()
        try:
            session.detach()
        except Exception:
            pass
    return Capture(g, a11y, lint, compose, tree)


# --------------------------------------------------------------------------- #
# Scenario-specific dump checks (defects no R1..R12 rule can see).
# --------------------------------------------------------------------------- #
def _node(cap: Capture, tag: str) -> Dict[str, Any]:
    return locate(cap, E(tag))


def _is_stop(cap: Capture, n: Dict[str, Any]) -> bool:
    return cap.focus_entry(n) is not None


def check_heading(good: str, bad: str) -> Callable[[Capture], None]:
    def check(cap: Capture) -> None:
        assert "heading" in (_node(cap, good).get("flags") or []), f"{good} is not a heading"
        assert "heading" not in (_node(cap, bad).get("flags") or []), f"{bad} should not be a heading"
    check.__name__ = f"check_heading_{good}"
    return check


def check_decorative(cap: Capture) -> None:
    bad = _node(cap, "bad_decorative")
    assert bad.get("content_description") == "Decorative divider", describe(bad)
    assert _is_stop(cap, bad), f"the labeled decorative divider should be a (noisy) focus stop: {describe(bad)}"
    for good in find_all(cap.nodes, "good_decorative"):
        assert not good.get("speakable"), f"cleared decorative divider speaks: {describe(good)}"
        assert not _is_stop(cap, good), f"cleared decorative divider is a focus stop: {describe(good)}"


def check_disabled(cap: Capture) -> None:
    good, bad = _node(cap, "good_disabled"), _node(cap, "bad_disabled")
    assert "enabled" not in (good.get("flags") or []), f"good_disabled should be disabled: {describe(good)}"
    flags = set(bad.get("flags") or [])
    assert {"enabled", "clickable"} <= flags, f"bad_disabled should look disabled but stay enabled+clickable: {describe(bad)}"


def check_live_region(cap: Capture) -> None:
    assert _node(cap, "good_live_region").get("live_region") == "POLITE"
    assert not _node(cap, "bad_live_region").get("live_region")


def check_tiny_text(cap: Capture) -> None:
    for tag in ("good_tiny_text", "bad_tiny_text"):
        assert _node(cap, tag).get("text"), f"{tag} has no text"


def check_merged_focus(cap: Capture) -> None:
    row = _node(cap, "good_merged_focus")
    assert _is_stop(cap, row), f"the merged row should be one focus stop: {describe(row)}"
    entry = cap.focus_entry(row) or {}
    assert "Play episode" in str(entry.get("speak") or ""), (
        f"the merged row should speak its children: {entry}")
    for n in cap.subtree(row)[1:]:
        assert not _is_stop(cap, n), f"a child of the merged row is its own stop: {describe(n)}"
    child = _node(cap, "bad_merged_focus_child")
    assert _is_stop(cap, child), f"the nested clickable icon should steal its own stop: {describe(child)}"


TRAVERSAL_ORDER = [
    "1. First", "2. Second", "3. Third",
    "1. First (reordered)", "2. Second (reordered)", "3. Third (reordered)",
    "3. Third (bad)", "2. Second (bad)", "1. First (bad)",
]


def check_traversal_order(cap: Capture) -> None:
    wanted = set(TRAVERSAL_ORDER)
    seen = [e.get("speak") for e in cap.a11y.get("focus_order") or []
            if e.get("speak") in wanted]
    assert seen == TRAVERSAL_ORDER, (
        "reading order of the traversal scenario is wrong\n"
        f"  expected: {TRAVERSAL_ORDER}\n  actual:   {seen}")


def check_label_for(cap: Capture) -> None:
    label, fld = _node(cap, "goodEmailLabel"), _node(cap, "goodEditText")
    linked = fld.get("labeled_by") == label.get("id") or label.get("id") in (fld.get("labeled_by_list") or [])
    assert linked, (f"goodEditText.labeled_by should be goodEmailLabel's key {label.get('id')}: "
                    f"labeled_by={fld.get('labeled_by')} list={fld.get('labeled_by_list')}")
    assert label.get("label_for") == fld.get("id"), (
        f"goodEmailLabel.label_for should be goodEditText's key {fld.get('id')}: {label.get('label_for')}")


VIEW_SECTION_TITLES = [
    "1. ImageButton contentDescription", "2. Touch target size", "3. Text contrast",
    "4. ImageView label", "5. Custom clickable role", "6. EditText label (labelFor)",
    "7. Switch stateDescription", "8. Heading semantics",
]


def check_view_reading_order(cap: Capture) -> None:
    """The classic-View screen is ScrollView > LinearLayout (not important for a11y) >
    TextViews: TalkBack reads each text as its own stop, never the whole screen as one."""
    speak = [str(e.get("speak") or "") for e in cap.a11y.get("focus_order") or []]
    glued = [x for x in speak if sum(t in x for t in VIEW_SECTION_TITLES) > 1]
    assert not glued, f"several section titles read as one stop: {glued[:2]}"
    titles = [x for x in speak if x in VIEW_SECTION_TITLES]
    assert titles == VIEW_SECTION_TITLES, f"section titles as stops: {titles}\n  order: {speak}"
    assert "Account, heading" in speak, speak
    deco = _node(cap, "goodDecorativeImage")  # importantForAccessibility="no"
    assert deco.get("ignored") and not _is_stop(cap, deco), describe(deco)
    assert "4. ImageView label" in speak, "the decorative image must add nothing to its section"


def check_dialog_reading_order(cap: Capture) -> None:
    """A modal dialog hides its activity from TalkBack: every stop is in the dialog."""
    wins = [w for w in cap.a11y.get("windows") or [] if w.get("root")]
    dialog = next(i for i, w in enumerate(wins) if w.get("modal") and i > 0)
    assert wins[0].get("covered_by") == wins[dialog]["root_view_id"], wins[0].get("covered_by")
    order = cap.a11y.get("focus_order") or []
    assert order and {e.get("window") for e in order} == {dialog}, order


def _compose_node_by_tag(cap: Capture, tag: str) -> Optional[Dict[str, Any]]:
    stack = [w["root"] for w in cap.compose.get("windows") or [] if w.get("root")]
    while stack:
        n = stack.pop()
        if (n.get("attrs") or {}).get("TestTag") == tag:
            return n
        stack.extend(n.get("children") or [])
    return None


def check_dialog(require_offset: bool) -> Callable[[Capture], None]:
    def check(cap: Capture) -> None:
        roots = [w["root"] for w in cap.a11y.get("windows") or [] if w.get("root")]
        assert len(roots) >= 2, f"expected the activity window plus the dialog window, got {len(roots)}"
        node = _node(cap, "dialog_compose_unlabeled")
        win = rect(cap.window_of[id(node)].get("bounds"))
        nb = rect(node.get("bounds"))
        assert win and nb and contains_rect(win, nb), f"dialog node {nb} is outside its window {win}"
        if require_offset:
            assert win["x"] > 0 or win["y"] > 0, f"the dialog window should not sit at 0,0: {win}"
        # Compose bounds are screen coordinates (ID contract): the semantics node
        # and the a11y node of the same button share a centre, whatever the
        # dialog window's offset.
        cn = _compose_node_by_tag(cap, "dialog_compose_unlabeled")
        assert cn is not None, "dialog_compose_unlabeled is missing from the Compose dump"
        cb = rect(cn.get("bounds"))
        assert cb, f"dialog_compose_unlabeled has no Compose bounds: {cn.get('bounds')}"
        (cx, cy), (ax, ay) = center(cb), center(nb)
        assert abs(cx - ax) <= 4 and abs(cy - ay) <= 4, (
            f"Compose bounds {cb} are not in screen coordinates (a11y {nb})")
    check.__name__ = "check_dialog"
    return check


# --------------------------------------------------------------------------- #
# The golden tables.
# --------------------------------------------------------------------------- #
MAIN, VIEWS, INTEROP = ".MainActivity", ".ViewScenarioActivity", ".InteropActivity"
GOOD_BASE = (R1, R2)


def compose_scenario(sid: str, anchor: str, **kw: Any) -> Golden:
    kw.setdefault("compose_views", (1, 1))
    return Golden(sid, MAIN, sid, anchor, **kw)


COMPOSE_GOLDENS = [
    compose_scenario("icon_button", "good_icon_button",
                     bad=(E("bad_icon_button", R1),), good=(E("good_icon_button", *GOOD_BASE),)),
    compose_scenario("touch_target", "good_touch_target",
                     bad=(E("bad_touch_target", R2),), good=(E("good_touch_target", *GOOD_BASE),)),
    compose_scenario("text_contrast", "good_contrast", contrast=True,
                     bad=(E("bad_contrast", R3),), good=(E("good_contrast", R3),)),
    compose_scenario("toggle_state", "good_switch",
                     bad=(E("bad_switch", R1),), good=(E("good_switch", R1, R2, R7),)),
    compose_scenario("checkbox_state", "good_checkbox",
                     bad=(E("bad_checkbox", R1),), good=(E("good_checkbox", R1, R2, R7),)),
    compose_scenario("image_label", "good_image",
                     bad=(E("bad_image", R6),), good=(E("good_image", R1, R6),)),
    compose_scenario("decorative_image", "bad_decorative", checks=(check_decorative,)),
    compose_scenario("custom_role", "good_role",
                     bad=(E("bad_role", R5),), good=(E("good_role", R1, R2, R5),)),
    compose_scenario("traversal", "good_trav_1"),
    compose_scenario("lazy_list", "good_lazy_item_0",
                     bad=tuple(E(f"bad_lazy_icon_{i}", R1) for i in range(3)),
                     good=tuple(E(f"good_lazy_item_{i}", *GOOD_BASE) for i in range(3))),
    compose_scenario("heading", "good_heading", checks=(check_heading("good_heading", "bad_heading"),)),
    compose_scenario("redundant_label", "good_redundant",
                     bad=(E("bad_redundant", R4),), good=(E("good_redundant", R1, R4),)),
    compose_scenario("dup_text_desc", "good_dup_text",
                     bad=(E("bad_dup_text", R4),), good=(E("good_dup_text", R1, R4),)),
    compose_scenario("form_field", "good_form_field",
                     bad=(E("bad_form_field", R1, FORM_LABEL),),
                     good=(E("good_form_field", R1, FORM_LABEL),)),
    compose_scenario("tiny_text", "good_tiny_text", checks=(check_tiny_text,)),
    compose_scenario("merged_focus", "good_merged_focus",
                     bad=(E("bad_merged_focus_child", R2),), good=(E("good_merged_focus", *GOOD_BASE),),
                     checks=(check_merged_focus,)),
    compose_scenario("disabled", "good_disabled", checks=(check_disabled,)),
    compose_scenario("live_region", "good_live_region", checks=(check_live_region,)),
    compose_scenario("dup_label", "good_merged",
                     bad=(E("bad_merged_inner", R1, DUPLICATE_BOUNDS),), good=(E("good_merged", *GOOD_BASE),)),
    compose_scenario("custom_toggle", "good_custom_toggle",
                     bad=(E("bad_custom_toggle", R7),), good=(E("good_custom_toggle", R1, R2, R7),)),
]

VIEW_GOLDEN = Golden(
    "view_xml", VIEWS, None, "badEditText", contrast=True, compose_views=(0, 0),
    bad=(E("badImageButton", R1, R6), E("badTouchTarget", R2), E("badContrast", R3),
         E("badImage", R6, R1), E("badCustomClickable", R5), E("badEditText", R1, FORM_LABEL)),
    good=(E("goodImageButton", R1, R6), E("goodTouchTarget", R1, R2), E("goodContrast", R3),
          E("goodImage", R1, R6), E("goodCustomClickable", R1, R2, R5),
          E("goodEditText", R1, FORM_LABEL)),
    checks=(check_label_for, check_view_reading_order),
)

# --- interop: mirrors InteropFragment.kt / InteropCells.kt ------------------ #
UNLABELED, SMALL, STATELESS = "UNLABELED", "SMALL_TARGET", "STATELESS"
ONE_OF_EACH = {1: {UNLABELED}, 2: {SMALL}, 3: {STATELESS}}
ALTERNATING = {1: {UNLABELED}, 2: {UNLABELED}, 3: {SMALL, STATELESS}, 4: {SMALL, STATELESS}}
MIXED = {0: {UNLABELED}, 1: {UNLABELED}, 2: {UNLABELED},
         3: {SMALL, STATELESS}, 4: {SMALL, STATELESS}, 5: {SMALL, STATELESS}}
PER_ROW_LABELS = ("Delete", "More info", "Notify", "Done")
CHECKBOX_TAG = {"compose": "done", "view": "check", "hybrid": "hybrid_check"}


def row_expectations(flaws: Dict[int, Set[str]], kind_at: Callable[[int], str],
                     rows: Sequence[int] = range(6), checkbox: bool = True):
    bad: List[Expect] = []
    good: List[Expect] = []
    for pos in rows:
        kind = kind_at(pos)
        title = f"Item {pos} ({kind})"
        f = flaws.get(pos, set())
        (bad if UNLABELED in f else good).append(
            E("delete", R1, row=title) if UNLABELED in f else E("delete", R1, R2, row=title))
        (bad if SMALL in f else good).append(
            E("info", R2, row=title) if SMALL in f else E("info", R1, R2, row=title))
        (bad if STATELESS in f else good).append(
            E("notify", R7, row=title) if STATELESS in f else E("notify", R1, R2, R7, row=title))
        if checkbox:
            good.append(E(CHECKBOX_TAG[kind], R1, R2, row=title))
    return tuple(bad), tuple(good)


def interop_scenario(sid: str, flaws, kind_at, compose_views, checkbox: bool = True) -> Golden:
    bad, good = row_expectations(flaws, kind_at, checkbox=checkbox)
    return Golden(sid, INTEROP, sid, f"Item 5 ({kind_at(5)})", bad=bad, good=good,
                  compose_views=compose_views, per_row_labels=PER_ROW_LABELS)


def _even_compose_odd_view(pos: int) -> str:
    return "compose" if pos % 2 == 0 else "view"


INTEROP_GOLDENS = [
    # Every visible row is a ComposeView (6+ on any phone).
    interop_scenario("S1", ONE_OF_EACH, lambda p: "compose", (6, None)),
    interop_scenario("S2", ONE_OF_EACH, lambda p: "view", (0, 0)),
    # compose, view, hybrid rows: each compose and hybrid row has its own ComposeView.
    interop_scenario("S3", MIXED, lambda p: ("compose", "view", "hybrid")[p % 3], (4, None)),
    # One LazyColumn; the AndroidView rows hold no Compose.
    interop_scenario("S4", ALTERNATING, _even_compose_odd_view, (1, 1)),
    # The outer ComposeView plus the nested compose cells (rows 0, 2, 4 at least).
    interop_scenario("S5", ALTERNATING, _even_compose_odd_view, (4, None)),
    interop_scenario("S6", ONE_OF_EACH, lambda p: "view", (0, 0), checkbox=False),
    Golden("D1", INTEROP, "D1", "dialog_view_unlabeled", min_windows=2, compose_views=(1, 1),
           bad=(E("dialog_compose_unlabeled", R1), E("dialog_view_unlabeled", R1, R6)),
           good=(E("dialog_compose_labeled", *GOOD_BASE), E("dialog_view_labeled", *GOOD_BASE),
                 E("dialog_close", *GOOD_BASE)),
           checks=(check_dialog(require_offset=True), check_dialog_reading_order)),
    Golden("D2", INTEROP, "D2", "dialog_compose_unlabeled", min_windows=2, compose_views=(2, 2),
           bad=(E("dialog_compose_unlabeled", R1),),
           good=(E("dialog_compose_labeled", *GOOD_BASE), E("dialog_close", *GOOD_BASE)),
           checks=(check_dialog(require_offset=False), check_dialog_reading_order)),
]

GOLDENS: List[Golden] = COMPOSE_GOLDENS + [VIEW_GOLDEN] + INTEROP_GOLDENS
GOLDEN_BY_ID = {g.sid: g for g in GOLDENS}
assert len(GOLDEN_BY_ID) == len(GOLDENS), "duplicate golden scenario id"


def _ids(pred: Callable[[Golden], Any]) -> List[str]:
    return [g.sid for g in GOLDENS if pred(g)]


# --------------------------------------------------------------------------- #
# Fixtures.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def device():
    if not SERIAL:
        pytest.skip("set ANDROID_SERIAL to run the A11yProbe golden test on that device")
    if shutil.which("adb") is None:
        pytest.skip("adb is not on PATH")
    from inspector_widget import adb, inject

    states = {d.serial: d.state for d in adb.devices()}
    if states.get(SERIAL) != "device":
        pytest.skip(f"{SERIAL} is not an attached device (adb devices: {states})")
    if PACKAGE not in adb.list_debuggable_packages(SERIAL):
        pytest.skip(f"{PACKAGE} is not installed/debuggable on {SERIAL}; "
                    f"run scripts/install-a11yprobe.sh {SERIAL}")
    # InteropActivity has no intent filter, so `dumpsys package` does not list it;
    # resolving the explicit component does.
    resolved = adb.shell(SERIAL, f"cmd package resolve-activity --brief -n {PACKAGE}/.InteropActivity",
                         check=False)
    if "InteropActivity" not in resolved:
        pytest.skip(f"the A11yProbe build on {SERIAL} predates the interop corpus; "
                    f"reinstall it with scripts/install-a11yprobe.sh {SERIAL}")
    missing = [a for a in (inject.NATIVE_SO_NAME, inject.BOOTSTRAP_DEX_NAME, inject.PAYLOAD_JAR_NAME)
               if not os.path.isfile(os.path.join(inject.DEFAULT_BUILD_OUT, a))]
    if missing:
        pytest.skip(f"agent artifacts missing from build-out/ ({missing}); run scripts/build.sh")
    adb.shell(SERIAL, "input keyevent KEYCODE_WAKEUP", check=False)
    adb.shell(SERIAL, "wm dismiss-keyguard", check=False)
    yield SERIAL
    adb.shell(SERIAL, f"am force-stop {PACKAGE}", check=False)


@pytest.fixture(scope="module")
def captures(device):
    cache: Dict[str, Any] = {}

    def get(sid: str) -> Capture:
        if sid not in cache:
            try:
                cache[sid] = capture(GOLDEN_BY_ID[sid])
            except BaseException as e:  # don't re-launch a scenario that already failed
                cache[sid] = e
        got = cache[sid]
        if isinstance(got, BaseException):
            raise got
        return got

    return get


# --------------------------------------------------------------------------- #
# Tests.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sid", _ids(lambda g: True))
def test_a11y_node_keys_are_unique_and_follow_the_id_contract(captures, sid):
    cap = captures(sid)
    assert cap.nodes, "empty a11y dump"
    dupes = [k for k, c in Counter(n.get("id") for n in cap.nodes).items() if c > 1]
    assert not dupes, (f"{len(dupes)} a11y node keys are shared by several nodes, e.g. "
                       + "; ".join(describe(n) for n in cap.nodes if n.get("id") in set(dupes[:3])))

    views: Dict[int, Dict[str, Any]] = {}
    stack = list(cap.tree.get("roots") or [])
    while stack:
        v = stack.pop()
        views[v["id"]] = v
        stack.extend(v.get("children") or [])
    problems = []
    for n in cap.nodes:
        virtual = n.get("virtual_id") != -1
        if virtual != ("is_virtual" in (n.get("flags") or [])):
            problems.append(f"is_virtual disagrees with virtual_id: {describe(n)}")
        host = views.get(n.get("host_view_id"))
        if host is None:
            problems.append(f"host_view_id is not a View in the tree: {describe(n)}")
            continue
        if virtual and not str(host.get("qualified_name") or host.get("class_name")).endswith("AndroidComposeView"):
            problems.append(f"virtual node hosted by a {host.get('class_name')}, not an AndroidComposeView: {describe(n)}")
        res = n.get("view_id_resource_name") or ""
        if not virtual and ":id/" in res:
            want = res.split(":id/", 1)[1]
            got = (host.get("resource") or {}).get("name")
            if got != want:
                problems.append(f"View a11y node {res} maps to View {host.get('class_name')} @id/{got}")
    assert not problems, f"{len(problems)} ID-contract violations:\n  " + "\n  ".join(problems[:15])


@pytest.mark.parametrize("sid", _ids(lambda g: True))
def test_compose_windows_are_exactly_the_compose_views(captures, sid):
    cap = captures(sid)
    acvs = set()
    stack = list(cap.tree.get("roots") or [])
    while stack:
        v = stack.pop()
        if str(v.get("qualified_name") or v.get("class_name")).endswith("AndroidComposeView"):
            acvs.add(v["id"])
        stack.extend(v.get("children") or [])
    windows = [w.get("view_id") for w in cap.compose.get("windows") or []]
    assert len(windows) == len(set(windows)), f"a ComposeView is reported twice: {windows}"
    assert set(windows) == acvs, (
        f"Compose windows {sorted(windows)} != AndroidComposeViews in the View tree {sorted(acvs)} "
        f"(diagnostics: {cap.compose.get('diagnostics')!r})")
    lo, hi = cap.golden.compose_views
    assert lo <= len(acvs) and (hi is None or len(acvs) <= hi), (
        f"{len(acvs)} AndroidComposeViews on screen, expected {lo}..{hi if hi is not None else 'n'}")


@pytest.mark.parametrize("sid", _ids(lambda g: g.bad))
def test_bad_nodes_are_flagged_with_the_right_rule(captures, sid):
    cap = captures(sid)
    misses = []
    for e in cap.golden.bad:
        n = locate(cap, e)
        on_node = [f for f in cap.findings if finding_hits(cap, f, n)]
        if not any(rule_matches(finding_rule(f), e.rules) for f in on_node):
            misses.append(f"{e.label()} [{describe(n)}] expected {' | '.join(e.rules)}; "
                          f"findings on it: {[describe_finding(f) for f in on_node] or 'none'}")
    assert not misses, (f"{len(misses)}/{len(cap.golden.bad)} BAD nodes not flagged "
                        f"(lint summary {cap.lint.get('summary')}):\n  " + "\n  ".join(misses))


@pytest.mark.parametrize("sid", _ids(lambda g: g.good))
def test_good_nodes_are_not_flagged(captures, sid):
    cap = captures(sid)
    wrong = []
    for e in cap.golden.good:
        n = locate(cap, e)
        hits = [f for f in cap.findings
                if rule_matches(finding_rule(f), e.rules) and finding_hits(cap, f, n)]
        if hits:
            wrong.append(f"{e.label()} [{describe(n)}]: {[describe_finding(f) for f in hits]}")
    assert not wrong, f"{len(wrong)} GOOD nodes flagged:\n  " + "\n  ".join(wrong)


@pytest.mark.parametrize("sid", _ids(lambda g: g.per_row_labels))
def test_per_row_labels_are_not_reported_as_duplicates(captures, sid):
    cap = captures(sid)
    legit = {s.lower() for s in cap.golden.per_row_labels}
    dup = [f for f in cap.findings
           if finding_rule(f) == R12 and finding_label(f).strip().lower() in legit]
    assert not dup, ("repeated per-row labels in a list are legitimate, not duplicates: "
                     + "; ".join(describe_finding(f) for f in dup))


def test_reading_order_of_the_traversal_screen(captures):
    """GOOD 1,2,3; GOOD laid out 3,2,1 but traversalIndex'd back to 1,2,3; BAD 3,2,1."""
    check_traversal_order(captures("traversal"))


@pytest.mark.parametrize("sid", _ids(lambda g: g.checks))
def test_scenario_dump_checks(captures, sid):
    cap = captures(sid)
    failures = []
    for check in cap.golden.checks:
        try:
            check(cap)
        except AssertionError as e:
            failures.append(f"{check.__name__}: {e}")
    assert not failures, "\n".join(failures)
