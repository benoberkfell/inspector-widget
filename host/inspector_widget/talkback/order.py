"""TalkBack's linear navigation (swipe right / left, or Meta+Right / Meta+Left), ported from
google/talkback @229212f (TalkBack 16.2).

``UT`` = ``utils/src/main/java/com/google/android/accessibility/utils/``;
``TB`` = ``talkback/src/main/java/com/google/android/accessibility/talkback/``.

Per step TalkBack rebuilds the order from the pivot's window root
(TraversalStrategyUtils.getTraversalStrategy -> OrderedTraversalStrategy ->
OrderedTraversalController.initOrder, UT/traversal/OrderedTraversalController.java:66):

1. A WorkingTree mirrors the node tree. Children come in ANI order through a
   ReorderedChildrenIterator: a child that TalkBack would not focus gets "effective bounds" (its
   focusable descendants' union, clipped to its own; NodeCachedBoundsCalculator.fetchBound) and
   moves later among its siblings when a STRIPE-like compare of the effective bounds asks for a
   swap that the real bounds do not (needSwapNodeOrder). Only TalkBack does this.
2. reorderTree visits the nodes in that pre-order. A resolvable traversalBefore moves the node's
   subtree (lifted to any ancestor whose own traversalBefore is this node) into the target's
   slot, and the target becomes the node's LAST child. Otherwise a resolvable traversalAfter
   makes the node's subtree the target's last child. Moves that would loop are no-ops. So a
   Compose chain a->b->c ends at c's position, read a, b, c.
3. Next / previous are the pre-order successor / predecessor; searchFocus steps until
   shouldFocusNode accepts, and a repeated node ends the search (an edge).
4. With nothing left in the window: the next accepted window in geometric order
   (WindowTraversal), a pause at the last window (reachEdge), then the wrap.

The model is static: scrolling is reported, never invented. ``autoscroll`` marks a step where
TalkBack first scrolls the container the pivot is at the edge of (autoScrollAtEdge, SCROLL_FORWARD
/ BACKWARD); ``show_on_screen`` marks a target TalkBack first brings into view because it sits on
the scrollable's edge (ensureOnScreen, ACTION_SHOW_ON_SCREEN). Both are 16.2 behaviour, and both
were seen on TalkBack 17.0.
"""

from __future__ import annotations

import functools
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

from .rules import (ACTION_SCROLL_BACKWARD, ACTION_SCROLL_DOWN, ACTION_SCROLL_FORWARD,
                    ACTION_SCROLL_LEFT, ACTION_SCROLL_RIGHT, ACTION_SCROLL_UP,
                    ROLE_EDIT_TEXT as R_EDIT_TEXT, ROLE_FLOATING_ACTION_BUTTON,
                    ROLE_GRID as R_GRID, ROLE_LIST as R_LIST, ROLE_WEB_VIEW, TB_RULES_REV, Rules)
from .speech import DEFAULT_VERSION
from .tree import (WINDOW_APPLICATION, WINDOW_INPUT_METHOD, WINDOW_MAGNIFICATION_OVERLAY,
                   WINDOW_SPLIT_SCREEN_DIVIDER, WINDOW_SYSTEM, Rect, TbNode, TbTree, TbWindow,
                   build)

_EMPTY = Rect()

# --------------------------------------------------------------------------------------------
# ReorderedChildrenIterator + NodeCachedBoundsCalculator
# --------------------------------------------------------------------------------------------


class BoundsCalculator:
    """NodeCachedBoundsCalculator (UT/traversal/NodeCachedBoundsCalculator.java): a node
    TalkBack focuses keeps its bounds; any other node gets the union of its children's
    effective bounds, clipped to its own (the clip can invert the rect, as in the Java)."""

    def __init__(self, rules: Rules):
        self.rules = rules
        self._bounds: Dict[int, Rect] = {}
        self._calculating: Set[int] = set()

    def get_bounds(self, n: Optional[TbNode]) -> Optional[Rect]:
        """getBounds (:49): None for an empty rect."""
        b = self._internal(n)
        return None if b == _EMPTY else b

    def _internal(self, n: Optional[TbNode]) -> Rect:
        if n is None or id(n) in self._calculating:
            return _EMPTY
        b = self._bounds.get(id(n))
        if b is None:
            self._calculating.add(id(n))
            b = self._bounds[id(n)] = self._fetch(n)
            self._calculating.discard(id(n))
        return b

    def _fetch(self, n: TbNode) -> Rect:
        """fetchBound (:79)."""
        if not self.rules.is_visible(n):
            return _EMPTY
        if self.rules.should_focus_node(n):
            return n.rect
        child_rects = [r for r in (self._internal(c) for c in n.children) if r != _EMPTY]
        own = n.rect
        if not child_rects:
            return own
        return Rect(max(min(r.left for r in child_rects), own.left),
                    max(min(r.top for r in child_rects), own.top),
                    min(max(r.right for r in child_rects), own.right),
                    min(max(r.bottom for r in child_rects), own.bottom))

    def uses_children_bounds(self, n: TbNode) -> bool:
        """usesChildrenBounds (:140)."""
        b = self.get_bounds(n)
        return b is not None and n.rect != b


def _compare(left: Optional[Rect], right: Optional[Rect]) -> int:
    """ReorderedChildrenIterator.compare (UT/traversal/ReorderedChildrenIterator.java:199): the
    STRIPE order, LTR. Never 0."""
    if left is None or right is None:
        return -1
    if left.bottom - right.top <= 0:
        return -1
    if left.top - right.bottom >= 0:
        return 1
    d = left.left - right.left
    if d:
        return d
    d = left.top - right.top
    if d:
        return d
    d = left.height - right.height
    if d:
        return -d
    d = left.width - right.width
    if d:
        return -d
    return -1


def reordered_children(rules: Rules, n: TbNode,
                       calc: Optional[BoundsCalculator] = None) -> Tuple[List[TbNode], Set[int]]:
    """ReorderedChildrenIterator.createAscendingIterator: ``n``'s children in the order TalkBack
    walks them, plus the ids of the children the bounds reordering moved."""
    calc = calc or BoundsCalculator(rules)
    nodes = list(n.children)
    moved: Set[int] = set()
    web = rules.role(n) == ROLE_WEB_VIEW or rules.supports_web_actions(n)
    if web or len(nodes) <= 1 or not any(calc.uses_children_bounds(c) for c in nodes):
        return nodes, moved
    # reorder (:115): from the second-to-last child backwards, bubble each child that uses its
    # children's bounds towards the end while needSwapNodeOrder says so (moveNodeIfNecessary).
    for i in range(len(nodes) - 2, -1, -1):
        current = nodes[i]
        if not calc.uses_children_bounds(current):
            continue
        j = i + 1
        while j < len(nodes) and _need_swap(calc, current, nodes[j]):
            nodes[j - 1], nodes[j] = nodes[j], current
            moved.add(id(current))
            j += 1
    return nodes, moved


def _need_swap(calc: BoundsCalculator, left: TbNode, right: TbNode) -> bool:
    """needSwapNodeOrder (:149): swap only when the effective bounds want it and the real bounds
    agree with the current order."""
    if _compare(calc.get_bounds(left), calc.get_bounds(right)) > 0:
        return _compare(left.rect, right.rect) < 0
    return False


# --------------------------------------------------------------------------------------------
# WorkingTree + OrderedTraversalController
# --------------------------------------------------------------------------------------------


class _WT:
    """WorkingTree (UT/traversal/WorkingTree.java)."""

    __slots__ = ("node", "parent", "children", "moved", "swapped")

    def __init__(self, node: TbNode, parent: Optional["_WT"]):
        self.node = node
        self.parent = parent
        self.children: List[_WT] = []
        self.moved: Optional[Tuple[str, TbNode]] = None
        self.swapped = False

    def ancestors_have_loop(self, limit: int) -> bool:
        """ancestorsHaveLoop (:88), bounded by the node count instead of a visited set."""
        steps, w = 0, self
        while w is not None:
            steps += 1
            if steps > limit:
                return True
            w = w.parent
        return False

    def has_descendant(self, tree: Optional["_WT"], limit: int) -> bool:
        """hasDescendant (:64): ``tree`` or one of its ancestors is this node."""
        if self.ancestors_have_loop(limit):
            return False
        t, steps = tree, 0
        while t is not None and steps <= limit:
            if t is self:
                return True
            t, steps = t.parent, steps + 1
        return False

    def last_node(self, limit: int) -> "_WT":
        """getLastNode (:183), bounded (a before-cycle can make the WorkingTree loop)."""
        w, steps = self, 0
        while w.children and steps <= limit:
            w, steps = w.children[-1], steps + 1
        return w

    def sibling(self, delta: int) -> Optional["_WT"]:
        """getNextSibling / getPreviousSibling (:130 / :161)."""
        p = self.parent
        if p is None:
            return None
        try:
            i = p.children.index(self) + delta
        except ValueError:
            return None  # "swap child not found"
        return p.children[i] if 0 <= i < len(p.children) else None

    def next(self, limit: int) -> Optional["_WT"]:
        """getNext (:112): first child, else the next sibling of the nearest ancestor-or-self."""
        if self.children:
            return self.children[0]
        w, steps = self, 0
        while w is not None and steps <= limit:
            sib = w.sibling(1)
            if sib is not None:
                return sib
            w, steps = w.parent, steps + 1
        return None

    def previous(self, limit: int) -> Optional["_WT"]:
        """getPrevious (:152): the previous sibling's last node, else the parent."""
        sib = self.sibling(-1)
        return sib.last_node(limit) if sib is not None else self.parent


class Traversal:
    """OrderedTraversalController (UT/traversal/OrderedTraversalController.java) for one window.

    ``order`` is the reordered WorkingTree in pre-order: WorkingTree.getNext is the pre-order
    successor (:112) and getPrevious the predecessor (:152), so findNext / findPrevious are
    neighbours in that list.
    """

    def __init__(self, rules: Rules, root: TbNode):
        self.rules = rules
        self.root = root
        self.map: Dict[int, _WT] = {}
        self.initial_focus: Optional[TbNode] = None
        self.diagnostics: List[Dict[str, Any]] = []
        self._limit = 0
        self._create(root)
        self._limit = len(self.map) + 1
        self._reorder()
        self.order: List[TbNode] = []
        self.pos: Dict[int, int] = {}
        self._linearize()

    # createWorkingTree (:90), iteratively; the map keeps its pre-order insertion order.
    def _create(self, root: TbNode) -> None:
        calc = BoundsCalculator(self.rules)
        stack: List[Tuple[TbNode, Optional[_WT], bool]] = [(root, None, False)]
        while stack:
            node, parent, swapped = stack.pop()
            if id(node) in self.map:
                continue  # "creating node tree with looped nodes - break the loop edge"
            wt = _WT(node, parent)
            wt.swapped = swapped
            self.map[id(node)] = wt
            if parent is not None:
                parent.children.append(wt)
            if self.rules.supports_web_actions(node):
                continue  # web content navigates itself; its descendants are not ordered here
            kids, moved = reordered_children(self.rules, node, calc)
            stack.extend((c, wt, id(c) in moved) for c in reversed(kids))

    def resolve(self, key: Any) -> Optional[TbNode]:
        """getTraversalBefore/After: the linked node if TalkBack can fetch it at all."""
        return self.rules.tree.resolve_link(key)

    # reorderTree (:126).
    def _reorder(self) -> None:
        for wt in list(self.map.values()):
            node = wt.node
            if node.has("request_initial_focus"):
                self.initial_focus = node
            before = self.resolve(node.get("traversal_before"))
            if before is not None:
                # A resolvable before-link wins, even when its target is outside this window.
                self._move_before(wt, self.map.get(id(before)))
                continue
            after = self.resolve(node.get("traversal_after"))
            if after is not None:
                self._move_after(wt, self.map.get(id(after)))

    def _lifted(self, moving: _WT) -> _WT:
        """getParentsThatAreMovedBeforeOrSameNode (:193), iteratively: climb while the parent's
        own traversalBefore is the node being moved."""
        cur, steps = moving, 0
        while cur.parent is not None and steps <= self._limit:
            if self.resolve(cur.parent.node.get("traversal_before")) is not cur.node:
                break
            cur, steps = cur.parent, steps + 1
        return cur

    def _move_before(self, moving: Optional[_WT], target: Optional[_WT]) -> None:
        """moveNodeBefore (:152)."""
        if moving is None or target is None:
            return
        if moving.has_descendant(target, self._limit):
            return  # no operation if move child before parent
        lifted = self._lifted(moving)
        parent = target.parent
        if lifted.has_descendant(parent, self._limit):
            return  # would move the subtree under its own descendant
        _detach(lifted)
        if parent is not None:
            try:
                parent.children[parent.children.index(target)] = lifted
            except ValueError:
                pass  # "swap child not found"
        lifted.parent = parent
        moving.children.append(target)  # the target becomes the moving node's LAST child
        target.parent = moving
        lifted.moved = ("before", target.node)
        target.moved = ("before_of", moving.node)

    def _move_after(self, moving: Optional[_WT], target: Optional[_WT]) -> None:
        """moveNodeAfter (:220)."""
        if moving is None or target is None:
            return
        if moving.has_descendant(target, self._limit):
            return
        moving = self._lifted(moving)
        if moving.has_descendant(target, self._limit):
            return
        _detach(moving)
        target.children.append(moving)
        moving.parent = target
        moving.moved = ("after", target.node)

    def _linearize(self) -> None:
        top = self.map[id(self.root)]
        steps = 0
        while top.parent is not None and steps <= self._limit:  # WorkingTree.getRoot (:192)
            top, steps = top.parent, steps + 1
        stack = [top]
        seen: Set[int] = set()
        while stack:
            w = stack.pop()
            if id(w) in seen:
                continue
            seen.add(id(w))
            self.pos[id(w.node)] = len(self.order)
            self.order.append(w.node)
            stack.extend(reversed(w.children))
        lost = [w.node for w in self.map.values() if id(w) not in seen]
        if lost:
            self.diagnostics.append({
                "kind": "detached", "count": len(lost), "keys": [n.key for n in lost[:10]],
                "message": (f"traversalBefore links form a cycle: {len(lost)} node(s) left "
                            "TalkBack's traversal tree and cannot be reached by swiping.")})

    # findNext / findPrevious / findFirst / findLast (:237-326).
    def find(self, n: TbNode, forward: bool) -> Optional[TbNode]:
        i = self.pos.get(id(n))
        if i is not None:
            j = i + 1 if forward else i - 1
            return self.order[j] if 0 <= j < len(self.order) else None
        wt = self.map.get(id(n))
        if wt is None:
            return None  # "can't find WorkingTree for AccessibilityNodeInfo"
        # Detached from the root (a before-cycle): walk the WorkingTree itself, as the Java does.
        w = wt.next(self._limit) if forward else wt.previous(self._limit)
        return w.node if w is not None else None

    def find_first(self, root: TbNode, forward: bool) -> Optional[TbNode]:
        wt = self.map.get(id(root))
        if wt is None:
            return None
        return wt.node if forward else wt.last_node(self._limit).node

    def working_parent(self, n: TbNode) -> Optional[TbNode]:
        wt = self.map.get(id(n))
        return wt.parent.node if wt is not None and wt.parent is not None else None

    def via(self, n: TbNode) -> str:
        """Why ``n`` sits where it does in the order: the nearest reorder that placed it or an
        ancestor. ``chain`` for Compose's traversal links, ``before:<key>`` (moved before that
        node), ``before_of:<key>`` (made the last child of the node whose traversalBefore it is),
        ``after:<key>``, ``bounds_swap`` or ``tree`` (plain ANI order)."""
        wt = self.map.get(id(n))
        swapped = False
        steps = 0
        while wt is not None and steps <= self._limit:
            if wt.moved is not None:
                kind, other = wt.moved
                if kind in ("before", "before_of") and wt.node.facet in ("compose", "interop") \
                        and other.facet in ("compose", "interop"):
                    return "chain"
                return f"{kind}:{other.key}"
            swapped = swapped or wt.swapped
            wt, steps = wt.parent, steps + 1
        return "bounds_swap" if swapped else "tree"


def _detach(w: _WT) -> None:
    if w.parent is not None:
        try:
            w.parent.children.remove(w)
        except ValueError:
            pass
    w.parent = None


def search_focus(trav: Traversal, start: TbNode, forward: bool,
                 accept: Callable[[TbNode], bool]) -> Tuple[Optional[TbNode], bool]:
    """TraversalStrategyUtils.searchFocus (UT/traversal/TraversalStrategyUtils.java:351): step
    until ``accept``. Returns (node or None, whether a duplicate ended the search)."""
    seen = {id(start)}
    n: Optional[TbNode] = start
    while True:
        n = trav.find(n, forward)  # type: ignore[arg-type]
        if n is None:
            return None, False
        if id(n) in seen:
            return None, True  # "Found duplicate during traversal"
        seen.add(id(n))
        if accept(n):
            return n, False


def find_first_focus_in_tree(trav: Traversal, root: TbNode, forward: bool,
                             accept: Callable[[TbNode], bool]) -> Optional[TbNode]:
    """findFirstFocusInNodeTree (:396)."""
    first = trav.find_first(root, forward)
    if first is None:
        return None
    if accept(first):
        return first
    return search_focus(trav, first, forward, accept)[0]


# --------------------------------------------------------------------------------------------
# Windows, pivot, edges, the navigation step
# --------------------------------------------------------------------------------------------


def _window_cmp(a: TbWindow, b: TbWindow) -> int:
    """WindowTraversal.WindowOrderComparator (TB/focusmanagement/WindowTraversal.java:52):
    TYPE_SYSTEM last, then top, then left (LTR). Equal bounds fall back to the window id, for
    which the dump index stands in (both grow as windows are added)."""
    if a.a11y_type != b.a11y_type:
        if a.a11y_type == WINDOW_SYSTEM:
            return 1
        if b.a11y_type == WINDOW_SYSTEM:
            return -1
    ra, rb = a.bounds, b.bounds
    if ra == rb:
        return a.index - b.index
    if ra.top != rb.top:
        return ra.top - rb.top
    return ra.left - rb.left


class Navigator:
    """TalkBack's linear navigation over one TalkBack view."""

    def __init__(self, tree: TbTree, rules: Optional[Rules] = None):
        self.tree = tree
        self.rules = rules or Rules(tree)
        self._trav: Dict[int, Traversal] = {}
        # getWindows() lists windows top-most first; the sort is stable.
        reported = [w for w in reversed(tree.windows) if w.reported]
        self.windows: List[TbWindow] = sorted(reported, key=functools.cmp_to_key(_window_cmp))

    def traversal(self, window: TbWindow) -> Traversal:
        t = self._trav.get(window.index)
        if t is None:
            t = self._trav[window.index] = Traversal(self.rules, window.root)
        return t

    def accepts_window(self, w: TbWindow) -> bool:
        """DirectionalNavigationWindowFilter (TB/focusmanagement/
        FocusProcessorForLogicalNavigation.java:3271), search UI hidden. The dump cannot tell a
        system bar from other system windows; app dumps hold none."""
        if w.a11y_type in (None, WINDOW_APPLICATION, WINDOW_SPLIT_SCREEN_DIVIDER, WINDOW_SYSTEM,
                           WINDOW_MAGNIFICATION_OVERLAY, WINDOW_INPUT_METHOD):
            return True
        return self.rules.role(w.root) == ROLE_FLOATING_ACTION_BUTTON

    def active_window(self) -> Optional[TbWindow]:
        """The window getRootInActiveWindow() would pick: the top-most focusable app window."""
        for w in sorted(self.windows, key=lambda w: -w.index):
            if w.focusable and w.a11y_type in (None, WINDOW_APPLICATION):
                return w
        return max(self.windows, key=lambda w: w.index) if self.windows else None

    def _need_pause(self, w: TbWindow, forward: bool) -> bool:
        """needPauseWhenTraverseAcrossWindow (:1788): no accepted window after (before) ``w``."""
        i = self.windows.index(w) if w in self.windows else -1
        if i < 0:
            return True
        rest = self.windows[i + 1:] if forward else self.windows[:i]
        return not any(self.accepts_window(x) for x in rest)

    def _next_window(self, w: TbWindow, forward: bool) -> Optional[TbWindow]:
        """getNextWindow / getPreviousWindow (WindowTraversal.java:193): wraps around."""
        if w not in self.windows:
            return None
        i = self.windows.index(w) + (1 if forward else -1)
        return self.windows[i % len(self.windows)]

    def _scrollable_for(self, n: TbNode, forward: bool, include_self: bool) -> Optional[TbNode]:
        """ScrollableNodeInfo.findScrollableNodeForDirection (UT/ScrollableNodeInfo.java:175)."""
        for a in ([n] if include_self else []) + list(n.ancestors()):
            if self.rules.filter_auto_scroll(a):
                action = _supported_scroll_action(a, forward)
                if action is not None and a.supports(action):
                    return a
        return None

    def _edge_item(self, pivot: TbNode, container: TbNode, forward: bool,
                   trav: Traversal) -> bool:
        """TraversalStrategyUtils.isAutoScrollEdgeListItem / isMatchingEdgeListItem (:229/:267):
        the next stop from ``pivot`` is none, the container itself, or outside it."""
        if not container.has("scrollable") or not (
                pivot is container or container in pivot.ancestors()):
            return False
        nxt, _ = search_focus(trav, pivot, forward, self.rules.should_focus_node)
        if nxt is None or nxt is container:
            return True
        for a in nxt.ancestors():
            if self.rules.filter_auto_scroll(a) and a is container:
                return False
        return True

    def autoscroll_container(self, pivot: TbNode, forward: bool,
                             trav: Traversal) -> Optional[TbNode]:
        """autoScrollAtEdge (TB/focusmanagement/FocusProcessorForLogicalNavigation.java:2264)
        without the scroll: the container TalkBack scrolls (SCROLL_FORWARD / BACKWARD) before
        moving on from ``pivot``, or None."""
        container = self._scrollable_for(pivot, forward, include_self=True)
        if container is None or not self._edge_item(pivot, container, forward, trav):
            return None
        return container

    def show_on_screen_container(self, target: TbNode, forward: bool,
                                 trav: Traversal) -> Optional[TbNode]:
        """ensureOnScreen (:2000): the scrollable TalkBack asks to show ``target``
        (ACTION_SHOW_ON_SCREEN) before focusing it, because the target is its edge item or
        reaches its edge in the direction of travel (isPositionAtEdge, :2045), or reaches the
        edge of the scrollable around it."""
        container = self._scrollable_for(target, forward, include_self=False)
        if container is None:
            return None
        if self._edge_item(target, container, forward, trav) \
                or _position_at_edge(target, container, forward):
            return container
        outer = self._scrollable_for(container, forward, include_self=False)
        if outer is not None and _position_at_edge(target, outer, forward):
            return container
        return None

    def step(self, pivot: TbNode, forward: bool, granularity: str,
             reach_edge: bool) -> Dict[str, Any]:
        """navigateToDefaultOrMacroGranularityTarget (:1105) for native elements, with
        scroll=wrap=true. Returns {"target", "via", "reach_edge", "autoscroll",
        "show_on_screen", "duplicate"}."""
        win = pivot.window
        trav = self.traversal(win)
        out: Dict[str, Any] = {"target": None, "via": None, "reach_edge": reach_edge,
                               "autoscroll": None, "show_on_screen": None, "duplicate": False}
        out["autoscroll"] = self.autoscroll_container(pivot, forward, trav)
        self._search(pivot, forward, granularity, out, win, trav)
        if out["target"] is not None:
            t = out["target"]
            out["show_on_screen"] = self.show_on_screen_container(
                t, forward, self.traversal(t.window))
        return out

    def _search(self, pivot: TbNode, forward: bool, granularity: str, out: Dict[str, Any],
                win: TbWindow, trav: Traversal) -> None:
        """The in-window search, then findTargetAcrossWindows, then the wrap; fills ``out``."""
        accept = self.rules.node_filter(granularity, pivot)
        target, dup = search_focus(trav, pivot, forward, accept)
        out["duplicate"] = dup
        if target is not None:
            out.update(target=target, via=trav.via(target), reach_edge=False)
            return
        # findTargetAcrossWindows (:1620).
        accepted = self.accepts_window(win)
        if not out["reach_edge"] and (not accepted or self._need_pause(win, forward)):
            out.update(reach_edge=True, via="edge")
            return
        if accepted:
            before = out["reach_edge"]
            target = self._search_windows(win, forward, accept, out)
            if before != out["reach_edge"]:
                out["via"] = "edge"
                return
            if target is not None:
                out.update(target=target, via=f"window:{target.window.index}", reach_edge=False)
                return
        # findTargetForWrapAround (:1689) inside the current window.
        if out["reach_edge"]:
            target = find_first_focus_in_tree(trav, win.root, forward, accept)
            if target is not None:
                out.update(target=target, via="wrap", reach_edge=False)
                return
        out["via"] = "none"

    def _search_windows(self, current: TbWindow, forward: bool,
                        accept: Callable[[TbNode], bool],
                        out: Dict[str, Any]) -> Optional[TbNode]:
        """searchTargetInNextOrPreviousWindow (:1911)."""
        w = current
        for _ in range(len(self.windows) + 1):
            if not out["reach_edge"] and self._need_pause(w, forward):
                out["reach_edge"] = True
                return None
            w = self._next_window(w, forward)
            if w is None or w is current:
                return None
            if not self.accepts_window(w):
                continue
            focus = find_first_focus_in_tree(self.traversal(w), w.root, forward, accept)
            if focus is not None:
                return focus
        return None

    def initial_focus(self, window: Optional[TbWindow] = None) -> Dict[str, Any]:
        """Where TalkBack puts accessibility focus when ``window`` (default: the active window)
        appears, with no focus history to restore (FocusFeedbackMapper
        .mapWindowChangeToFocusAction, TB/focusmanagement/FocusFeedbackMapper.java:61-86):

        1. ``input_focus``: the input-focused node, if it is editable (or an Edit box) and
           TalkBack would focus it, else its selected or first focusable descendant
           (FocusActorForScreenStateChange.getNodeForFocusSync, TB/actor/...:296).
        2. ``requested``: the node with requestInitialAccessibilityFocus (Compose never sets it).
        3. ``first_content``: the first stop whose simple description is not the window title
           (focusOnRequestInitialNodeOrFirstFocusableNonTitleNode, :365-440), unless it or an
           ancestor is clickable or a list/grid. The title is the window's own, else the first
           text of the window's View tree (:func:`.tree.window_title`).

        Returns ``{"node", "key", "how", "skipped", "title", "title_source"}``; ``skipped``
        lists the stops passed over because they read as the window title.
        """
        from .tree import window_title

        win = window or self.active_window()
        out: Dict[str, Any] = {"node": None, "key": None, "how": None, "skipped": [],
                               "title": None, "title_source": None}
        if win is None:
            return out
        rules = self.rules
        # 1. Follow input focus (editable only, on a phone).
        focused = next((n for n in win.root.iter() if n.has("focused")), None)
        if focused is not None and (focused.has("editable")
                                    or rules.role(focused) == R_EDIT_TEXT):
            pick = focused if rules.should_focus_node(focused) else _replacement(rules, focused)
            if pick is not None:
                out.update(node=pick, key=pick.key, how="input_focus")
                return out
        trav = self.traversal(win)
        # 2. requestInitialAccessibilityFocus.
        req = trav.initial_focus
        if req is not None and rules.should_focus_node(req):
            out.update(node=req, key=req.key, how="requested")
            return out
        # 3. The first focusable node that is not the window title.
        title, source = window_title(self.tree, win)
        out.update(title=title, title_source=source)
        skipped: List[TbNode] = []

        def not_title(n: TbNode) -> bool:
            if not rules.should_focus_node(n):
                return False
            if not title:
                return True
            desc = _simple_description(rules, n)
            if desc is None or desc.lower() != title.lower() or _illegal_title_node(rules, n):
                return True
            skipped.append(n)
            return False

        first = find_first_focus_in_tree(trav, win.root, True, not_title)
        out["skipped"] = [n.key for n in skipped]
        if first is not None:
            out.update(node=first, key=first.key, how="first_content")
        return out

    def linear(self, granularity: str = "default") -> List[TbNode]:
        """Every stop, window by window in traversal order (the order a forward walk from the
        top of the first window visits them, without the edge pause)."""
        out: List[TbNode] = []
        for w in self.windows:
            if not self.accepts_window(w):
                continue
            accept = self.rules.node_filter(granularity)
            out.extend(n for n in self.traversal(w).order if accept(n))
        return out


def _supported_scroll_action(n: TbNode, forward: bool) -> Optional[int]:
    """ScrollableNodeInfo.getSupportedScrollDirection (UT/ScrollableNodeInfo.java:104) as the
    scroll action: FORWARD/BACKWARD natively, else the one axis the node scrolls on."""
    native = ACTION_SCROLL_FORWARD if forward else ACTION_SCROLL_BACKWARD
    if n.supports(native):
        return native
    up_down = n.supports(ACTION_SCROLL_UP, ACTION_SCROLL_DOWN)
    left_right = n.supports(ACTION_SCROLL_LEFT, ACTION_SCROLL_RIGHT)
    if up_down and left_right:
        return None
    if up_down:
        a = ACTION_SCROLL_DOWN if forward else ACTION_SCROLL_UP
        return a if n.supports(a) else None
    if left_right:
        a = ACTION_SCROLL_RIGHT if forward else ACTION_SCROLL_LEFT
        return a if n.supports(a) else None
    return None


def _position_at_edge(n: TbNode, scrollable: TbNode, forward: bool) -> bool:
    """isPositionAtEdge (TB/focusmanagement/FocusProcessorForLogicalNavigation.java:2045): the
    node reaches the scrollable's far edge in the direction of travel. The axis comes from the
    CollectionInfo, else from two children lined up vertically or horizontally (LTR)."""
    horizontal = False
    ci = scrollable.get("collection_info") or {}
    rows, cols = int(ci.get("row_count", -1)), int(ci.get("column_count", -1))
    if ci and rows > 0 and cols > 0:
        horizontal = rows < cols
    else:
        kids = scrollable.children
        for a, b in zip(kids, kids[1:]):
            if a.rect.is_empty() or b.rect.is_empty():
                continue
            if (a.rect.left + a.rect.right) // 2 == (b.rect.left + b.rect.right) // 2:
                break
            if (a.rect.top + a.rect.bottom) // 2 == (b.rect.top + b.rect.bottom) // 2:
                horizontal = True
                break
    s, r = scrollable.rect, n.rect
    if forward:
        return s.right <= r.right if horizontal else s.bottom <= r.bottom
    return s.left >= r.left if horizontal else s.top >= r.top


def _replacement(rules: Rules, bad: TbNode) -> Optional[TbNode]:
    """findReplacementForBadCandidate: a selected descendant TalkBack would focus, else the first
    descendant it would focus."""
    desc = list(bad.iter())[1:]
    sel = next((n for n in desc if n.has("selected")), None)
    if sel is not None and rules.should_focus_node(sel):
        return sel
    return next((n for n in desc if rules.should_focus_node(n)), None)


def _simple_description(rules: Rules, n: TbNode) -> Optional[str]:
    """getSimpleNodeTreeDescription (TB/actor/FocusActorForScreenStateChange.java:474): the
    node's contentDescription or text, else the first visible, non-focusable descendant with
    one, in traversal order."""
    own = (n.content_description or "").strip() and n.content_description \
        or (n.text or "").strip() and n.text
    if own:
        return own
    trav = Traversal(rules, n)
    hit, _ = search_focus(trav, n, True, lambda c: (
        rules.is_visible(c) and not rules.is_accessibility_focusable(c)
        and bool((c.content_description or c.text or "").strip())))
    if hit is None:
        return None
    return hit.content_description or hit.text


def _illegal_title_node(rules: Rules, n: TbNode) -> bool:
    """isOrHasMatchingAncestor(getFilterIllegalTitleNodeAncestor) (UT :417): clickable or
    long-clickable, or a List or Grid, on the node or an ancestor: it cannot be the title."""
    for a in [n, *n.ancestors()]:
        if rules.is_clickable(a) or rules.is_long_clickable(a) \
                or rules.role(a) in (R_LIST, R_GRID):
            return True
    return False


# --------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------


class Order:
    """The result of :func:`simulate`: the steps of a walk, TalkBack-style."""

    def __init__(self, steps: List[Dict[str, Any]], ended: str, *, direction: str,
                 granularity: str, start: Optional[str], diagnostics: List[Dict[str, Any]],
                 nodes: List[Optional[TbNode]]):
        self.steps = steps
        self.ended = ended
        self.direction = direction
        self.granularity = granularity
        self.start = start
        self.diagnostics = diagnostics
        self._nodes = nodes  # the TbNode behind each step (None for an edge)
        self.version = DEFAULT_VERSION

    @property
    def stops(self) -> List[Dict[str, Any]]:
        return [s for s in self.steps if not s.get("edge")]

    def keys(self) -> List[str]:
        return [s["key"] for s in self.stops]

    def speech(self) -> List[str]:
        return [s["speak"] for s in self.stops]

    def nodes(self) -> List[TbNode]:
        return [n for n in self._nodes if n is not None]

    def to_dict(self) -> Dict[str, Any]:
        out = {"rules": TB_RULES_REV, "version": self.version, "direction": self.direction,
               "granularity": self.granularity, "start": self.start, "steps": self.steps,
               "ended": self.ended}
        if self.diagnostics:
            out["diagnostics"] = self.diagnostics
        return out


def _as_tree(tree: Any) -> TbTree:
    return tree if isinstance(tree, TbTree) else build(tree)


def simulate(tree: Any, start: Any = None, direction: str = "next",
             granularity: str = "default", max_steps: Optional[int] = None,
             until: str = "wrap", version: Optional[str] = None) -> Order:
    """Walk the TalkBack view the way repeated swipes would.

    ``tree``: a :class:`TbTree` or anything :func:`.tree.build` takes. ``start``: None (no
    accessibility focus: the pivot is the active window's root, so "next" lands on the first
    stop), "initial" (the focus TalkBack gives a window that just appeared,
    :meth:`Navigator.initial_focus`, reported as step 0), or the node that holds accessibility
    focus (TbNode, dump dict, int or typed key).
    ``direction``: "next" | "prev". ``granularity``: "default" | "heading" | "control" |
    "container". ``until``: "wrap" (stop when the first stop comes round again), "edge" (stop at
    the first edge) or "steps" (run ``max_steps``).

    Every stop step is ``{"i", "key", "id", "window", "why", "via", "speak", "parts"}`` plus,
    when they apply, ``"unlabelled"``, ``"autoscroll"`` (the container TalkBack scrolls before
    this move; the content it would scroll in is not modelled) and ``"show_on_screen"`` (the
    scrollable TalkBack asks to bring this target fully into view). ``version`` picks the
    wording (:data:`.speech.VERSIONS`). ``via`` is how focus got
    there: ``tree``, ``bounds_swap``, ``chain``, ``before:<key>``, ``before_of:<key>``,
    ``after:<key>``, ``window:<index>`` or ``wrap``. An edge step is ``{"i", "edge": True,
    "key", "window"}``: the press only sets TalkBack's reachEdge; the next one wraps.
    """
    from .explain import why_stop
    from .speech import SpeechState, announce

    tb = _as_tree(tree)
    nav = Navigator(tb)
    forward = direction in ("next", "forward")
    if direction not in ("next", "forward", "prev", "previous", "backward"):
        raise ValueError(f"direction must be 'next' or 'prev', got {direction!r}")
    initial: Optional[Dict[str, Any]] = None
    if start == "initial":
        initial = nav.initial_focus()
        if initial["node"] is None:
            return Order([], "empty", direction=direction, granularity=granularity, start=None,
                         diagnostics=list(tb.diagnostics), nodes=[])
        pivot = initial["node"]
        start_key = pivot.key
    elif start is None:
        win = nav.active_window()
        if win is None:
            return Order([], "empty", direction=direction, granularity=granularity, start=None,
                         diagnostics=list(tb.diagnostics), nodes=[])
        pivot = win.root
        start_key = None
    else:
        pivot = tb.node(start)
        if pivot is None:
            raise KeyError(f"start node {start!r} is not in the TalkBack view")
        start_key = pivot.key
    if max_steps is None:
        # Enough for one lap: every node once, the edge pause and the wrap.
        max_steps = sum(1 for w in nav.windows for _ in w.root.iter()) + 3

    steps: List[Dict[str, Any]] = []
    nodes: List[Optional[TbNode]] = []
    state = SpeechState()
    reach_edge = False
    first: Optional[TbNode] = None
    ended = "max_steps"
    idle = 0
    last_seen: Dict[int, int] = {}  # id(node) -> step of its latest visit
    last_edge = 0
    if initial is not None:
        ann = announce(nav, pivot, state, version=version)
        step0: Dict[str, Any] = {
            "i": 0, "key": pivot.key, "id": pivot.id, "window": pivot.window.index,
            "why": why_stop(nav.rules, pivot), "via": f"initial:{initial['how']}",
            "speak": ann.text, "parts": ann.parts}
        if initial["skipped"]:
            step0["skipped_title"] = initial["skipped"]
        steps.append(step0)
        nodes.append(pivot)
        last_seen[id(pivot)] = 0
        first = pivot
    for i in range(1, max_steps + 1):
        res = nav.step(pivot, forward, granularity, reach_edge)
        reach_edge = res["reach_edge"]
        target = res["target"]
        if target is None:
            steps.append({"i": i, "edge": True, "key": pivot.key, "window": pivot.window.index,
                          **({"duplicate": True} if res["duplicate"] else {})})
            nodes.append(None)
            last_edge = i
            idle += 1
            if until == "edge" or idle >= 2:
                ended = "edge" if first is not None else "empty"
                break
            continue
        idle = 0
        if until != "steps" and id(target) in last_seen and last_edge < last_seen[id(target)]:
            ended = "loop"  # a stop came round again without an edge or a wrap in between
            break
        ann = announce(nav, target, state, version=version)
        step: Dict[str, Any] = {
            "i": i, "key": target.key, "id": target.id, "window": target.window.index,
            "why": why_stop(nav.rules, target), "via": res["via"], "speak": ann.text,
            "parts": ann.parts,
        }
        if ann.unlabelled:
            step["unlabelled"] = True
        if res["autoscroll"] is not None:
            step["autoscroll"] = res["autoscroll"].key
        if res["show_on_screen"] is not None:
            step["show_on_screen"] = res["show_on_screen"].key
        steps.append(step)
        nodes.append(target)
        last_seen[id(target)] = i
        pivot = target
        if first is None:
            first = target
        elif target is first and until == "wrap":
            ended = "wrap"
            break
    diags = list(tb.diagnostics)
    for t in nav._trav.values():
        diags.extend(t.diagnostics)
    order = Order(steps, ended, direction=direction, granularity=granularity, start=start_key,
                  diagnostics=diags, nodes=nodes)
    order.version = version or DEFAULT_VERSION
    return order


def reading_order(roots: Any, include_structural: bool = False,
                  skip: Optional[set] = None, version: Optional[str] = None) -> Dict[str, Any]:
    """TalkBack's reading order in the shape of :func:`inspector_widget.a11y.reading_order`.

    ``roots``: the window roots (``skip`` holds indices of the windows TalkBack cannot reach),
    or a whole :func:`~inspector_widget.a11y.a11y_to_dict` dump / :class:`TbTree`. Returns
    ``{"focus_order": [{"order", "key", "id", "speak"} (+ "unlabeled", + "window" when there are
    several windows)], "diagnostics": [...], "_nodes": [the dump dicts]}``; with
    ``include_structural`` the non-stops TalkBack walks over are listed too (``order`` None,
    ``is_focus_stop`` False). Windows come in TalkBack's geometric order.
    """
    from .speech import SpeechState, announce

    if isinstance(roots, TbTree):
        tb = roots
    elif isinstance(roots, dict):
        tb = build(roots, skip=skip)
    else:
        tb = build([r for r in roots if r], skip=skip)
    nav = Navigator(tb)
    with_root = [w for w in tb.windows if w.root is not None]
    window_pos = {w.index: i for i, w in enumerate(with_root)}
    multi = len(with_root) > 1
    entries: List[Dict[str, Any]] = []
    nodes: List[Dict[str, Any]] = []
    counter = 0
    for w in nav.windows:
        if not nav.accepts_window(w):
            continue
        for n in nav.traversal(w).order:
            stop = nav.rules.should_focus_node(n)
            if not stop and not include_structural:
                continue
            key = n.raw.get("node_key")
            if key is None and "node_key" not in n.raw:
                key = n.key
            entry: Dict[str, Any] = {"order": None, "key": key, "id": n.id}
            if stop:
                counter += 1
                entry["order"] = counter
                ann = announce(nav, n, SpeechState(), transitions=False, version=version)
                entry["speak"] = ann.text
                if ann.unlabelled:
                    entry["unlabeled"] = True
            else:
                entry["speak"] = n.raw.get("speakable")
            if include_structural:
                entry["is_focus_stop"] = stop
            if multi:
                entry["window"] = window_pos.get(w.index, 0)
            entries.append(entry)
            nodes.append(n.raw)
    diags = list(tb.diagnostics)
    for t in nav._trav.values():
        diags.extend(t.diagnostics)
    covered = [w for w in tb.windows if w.root is not None and not w.reported]
    if covered:
        diags.append({"kind": "unreachable_windows",
                      "windows": [w.root_view_id for w in covered],
                      "reasons": [w.dropped for w in covered]})
    return {"focus_order": entries, "diagnostics": diags, "_nodes": nodes}


def iter_steps(order: Order) -> Iterator[Tuple[Dict[str, Any], Optional[TbNode]]]:
    """(step, node) pairs of a walk."""
    return zip(order.steps, order._nodes)
