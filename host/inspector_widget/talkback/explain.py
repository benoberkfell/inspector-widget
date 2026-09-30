"""Why a node is, or is not, a TalkBack focus stop, and how focus reaches it.

Reason codes (stable strings; a ``:<key>`` suffix names the node responsible):

Stops (:func:`why_stop`)
    ``click`` / ``longclick`` / ``focusable`` / ``srf`` (screen-reader-focusable) /
    ``scroll_item`` (a speaking direct child of a list or scroll view): what makes it
    accessibility-focusable; ``leaf``: accessibility-focusable with no children and nothing to
    say (TalkBack focuses it anyway and says "Unlabelled" or the role word);
    ``text_orphan``: not focusable, but has text or a state and no focusable ancestor;
    ``web``: web content, focused by the WebView itself.

Non-stops (:func:`why_not`)
    ``not_important`` (a View TalkBack never gets; its children are read in its place),
    ``hidden_by:<key>`` (importantForAccessibility=noHideDescendants on it or an ancestor),
    ``covered_by:<window root id>`` / ``not_touchable`` / ``skipped`` (its window is not
    reported), ``hidden`` (not visible to the user: alpha, visibility, hideFromAccessibility,
    or ``hidden(holder)`` for an AndroidView holder while a service runs), ``offscreen`` (not
    visible and outside its window; ``reachable`` says whether auto-scroll can bring it in),
    ``window_wrapper`` (the size of its window, has children, not focusable),
    ``silent_container`` (focusable, but only its focusable children speak: they are the stops),
    ``merged_into:<key>`` (read as part of that focusable ancestor), ``no_speech``.

Edges (the ``via`` of a :func:`~.order.simulate` step)
    ``tree``, ``bounds_swap``, ``chain``, ``before:<key>``, ``before_of:<key>``,
    ``after:<key>``, ``window:<index>``, ``wrap``; an edge step is the pause at the end.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .rules import Rules
from .tree import Excluded, TbNode, TbTree, build

STOP_CODES = ("click", "longclick", "focusable", "srf", "scroll_item", "leaf", "text_orphan",
              "web", "pip")
NON_STOP_CODES = ("not_important", "hidden_by", "covered_by", "not_touchable", "skipped",
                  "hidden", "offscreen", "window_wrapper", "silent_container", "merged_into",
                  "no_speech")
EDGE_CODES = ("tree", "bounds_swap", "chain", "before", "before_of", "after", "window", "wrap",
              "autoscroll", "edge")


def why_stop(rules: Rules, n: TbNode) -> Optional[str]:
    """The reason code of a stop, or None when ``n`` is not one."""
    ok, branch = rules.focus_decision(n)
    if not ok:
        return None
    if branch in ("web", "pip", "text_orphan"):
        return branch
    if branch == "leaf" and not rules.is_speaking_node(n, rules.cache, set()):
        return "leaf"
    if rules.is_clickable(n):
        return "click"
    if rules.is_long_clickable(n):
        return "longclick"
    if rules.is_actionable_for_accessibility(n):
        return "focusable"
    if n.has("screen_reader_focusable"):
        return "srf"
    return "scroll_item"


def why_not(rules: Rules, n: TbNode) -> Optional[str]:
    """The reason code of a node TalkBack walks over, or None when it is a stop."""
    if not n.window.reported:
        return n.window.dropped or "skipped"
    ok, branch = rules.focus_decision(n)
    if ok:
        return None
    if branch == "not_visible":
        if "holder_invisible_with_service" in n.corrections:
            return "hidden(holder)"
        w = n.window.bounds
        if n.rect.is_empty() or not n.rect.intersects(w):
            return "offscreen"
        return "hidden"
    if branch == "focusable_ancestor":
        anc = rules.focusable_ancestor(n)
        return f"merged_into:{anc.key}" if anc is not None else "no_speech"
    return branch  # window_wrapper, silent_container, no_speech


def _offscreen_reach(rules: Rules, n: TbNode) -> str:
    for a in n.ancestors():
        if rules.filter_auto_scroll(a):
            return f"autoscroll:{a.key}"
    return "no"


def explain(tree: Any, ref: Any, rules: Optional[Rules] = None) -> Dict[str, Any]:
    """Explain one node: ``{"key", "stop", "why"}`` plus details.

    ``tree``: a :class:`~.tree.TbTree`, a :class:`~.order.Navigator` or a dump; ``ref``: a
    node, dump dict, int key or typed key (including the key of a View TalkBack never gets).
    """
    nav_rules = getattr(tree, "rules", None)
    tb: TbTree = getattr(tree, "tree", None) or (tree if isinstance(tree, TbTree) else build(tree))
    rules = rules or nav_rules or Rules(tb)
    n = tb.node(ref)
    if n is None:
        ex = _find_excluded(tb, ref)
        if ex is None:
            return {"key": str(ref), "stop": False, "why": "unknown"}
        key = ex.raw.get("node_key") or str(ex.raw.get("id"))
        if ex.reason == "hidden":
            by = ex.hidden_by.get("node_key") or str(ex.hidden_by.get("id"))
            return {"key": key, "stop": False, "why": f"hidden_by:{by}",
                    "detail": "importantForAccessibility=noHideDescendants removes the subtree"}
        return {"key": key, "stop": False, "why": "not_important",
                "detail": ("not important for accessibility: TalkBack reads its children in "
                           f"its place (under {ex.parent.key})"),
                "importance_conf": tb.importance_conf}
    if not n.window.reported:
        return {"key": n.key, "stop": False, "why": n.window.dropped or "skipped"}
    stop = why_stop(rules, n)
    if stop is not None:
        out: Dict[str, Any] = {"key": n.key, "stop": True, "why": stop}
        ghosts = rules.invisible_speaking_children(n)
        if ghosts and not any(rules.is_visible(c) and not rules.is_focusable_or_clickable(c)
                              and rules.is_speaking_node(c, rules.cache, set())
                              for c in n.children):
            out["detail"] = ("speaks only through invisible children "
                             + ", ".join(c.key for c in ghosts[:5]))
        return out
    why = why_not(rules, n) or "no_speech"
    out = {"key": n.key, "stop": False, "why": why}
    if why == "silent_container":
        stops = [c.key for c in n.children if rules.should_focus_node(c)]
        out["detail"] = "focusable but has nothing of its own to speak; the stops are " + (
            ", ".join(stops[:8]) if stops else "its descendants")
    elif why == "offscreen":
        out["reachable"] = _offscreen_reach(rules, n)
    elif why == "window_wrapper":
        out["detail"] = "same bounds as its window, has children, neither focusable nor clickable"
    if n.corrections:
        out["corrections"] = list(n.corrections)
    return out


def _find_excluded(tb: TbTree, ref: Any) -> Optional[Excluded]:
    if isinstance(ref, dict):
        return tb.excluded_by_raw.get(id(ref))
    for ex in tb.excluded:
        if (isinstance(ref, int) and ex.raw.get("id") == ref) or \
                (isinstance(ref, str) and ex.raw.get("node_key") == ref):
            return ex
    return None
