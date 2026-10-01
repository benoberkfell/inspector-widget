"""TalkBack scenarios: where does focus go after an action, after back, after an update?

* ``focus_after``: act (``activate`` = TalkBack's own click on the focused node,
  Meta+Space, the same path as a double tap; ``back``; ``tap`` = an injected tap
  that bypasses TalkBack; ``key:<combo>``) and classify where focus lands:
  ``initial_ok`` | ``on_close_or_unlabeled`` | ``behind_overlay`` |
  ``stayed_on_opener`` | ``none`` | ``elsewhere``.
* ``restore``: focus a target, activate it, wait for the new screen, go back,
  and classify: ``restored`` | ``near`` | ``top`` | ``none`` | ``elsewhere``.
* ``survive``: focus a target, mutate the screen (``tap:<selector>`` |
  ``activate`` | ``key:<combo>`` | ``broadcast:<am broadcast args>`` |
  ``probe:<action>`` for the a11yprobe TB_PROBE receiver), sample the focus
  timeline and classify: ``kept`` | ``drifted`` | ``restored`` | ``reset_top`` |
  ``lost`` | ``moved``.

All three share :class:`.walk.Driver` (TalkBack on and restored, the key
guard, the per-device lock). Selectors are node keys or part of a label; the
target is reached by pressing "next" until focus gets there (A11yAct, design
T2, will set it directly).
"""

from __future__ import annotations

import os
import re
import shlex
import time
from typing import Any, Dict, List, Optional, Tuple

from .. import adb
from . import device
from .walk import (Driver, Node, Snapshot, STEP_TIMEOUT_MS, SETTLE_MS, WalkError, _match,
                   _seek_start, _walk_id, predict, predict_initial, walks_dir)

KINDS = ("focus_after", "restore", "survive")
ACTIONS = ("activate", "back", "tap")
PROBE_ACTION = "com.oberkfell.a11yprobe.TB_PROBE"
WINDOW_QUIET_S = 0.7   # TalkBack speaks a new window 550ms after it appears
SAMPLE_S = 0.03
_CLOSE = re.compile(r"\b(close|dismiss|cancel|navigate up|back)\b|^[x×✕]$", re.I)

FIXES = {
    "tb.initial_focus": "Put the content first (or give the close button a label and place it "
                        "last), give the window/pane a title (Compose DialogProperties / "
                        "paneTitle), and drop stray FocusRequester.requestFocus() calls.",
    "tb.restore_failed": "Put accessibility focus back yourself when the screen returns. On "
                         "TalkBack 17 a paneTitle per destination does not restore it, and "
                         "neither does input focus (View.requestFocus / Compose "
                         "FocusRequester.requestFocus: TalkBack does not follow it), measured on "
                         "A11yProbe C14 and Now in Android. Keep the list state "
                         "(rememberSaveable / LazyListState, stable keys) so the row is still "
                         "there, then, once it is laid out, send it "
                         "ACTION_ACCESSIBILITY_FOCUS: "
                         "View.performAccessibilityAction(AccessibilityNodeInfo"
                         ".ACTION_ACCESSIBILITY_FOCUS, null), or for Compose the host view's "
                         "accessibilityNodeProvider.performAction(semanticsNodeId, "
                         "ACTION_ACCESSIBILITY_FOCUS, null) (A11yProbe C14 GOOD).",
    "tb.focus_reset": "Keep the focused item's identity stable: items(key = { it.id }), "
                      "setHasStableIds + DiffUtil with payloads, supportsChangeAnimations = false, "
                      "ViewCompositionStrategy.DisposeOnViewTreeLifecycleDestroyed for ComposeView cells.",
    "tb.focus_lost": "Same as tb.focus_reset: the focused node was removed or re-created.",
    "tb.focus_drift": "The focused View was rebound to other content (notifyDataSetChanged): use "
                      "DiffUtil / stable ids so the View keeps its item.",
}


def _ref(n: Optional[Node], legacy: bool) -> Optional[str]:
    if n is None:
        return None
    return n.key if not legacy else f"{n.simple_cls}:{n.label[:40]}"


def _desc(n: Optional[Node], legacy: bool) -> Optional[Dict[str, Any]]:
    if n is None:
        return None
    return {"ref": _ref(n, legacy), "cls": n.simple_cls, "speak": n.speech(80),
            "bounds": list(n.bounds), "window": n.window}


def _windows(s: Snapshot) -> Tuple[int, ...]:
    return tuple(s.index.windows)


def _panes(s: Snapshot) -> List[str]:
    return sorted({n.pane_title for n in s.index.order if n.pane_title})


def timeline(drv: Driver, t0: float, wait_s: float, stop_when_quiet: bool,
             legacy: bool) -> Tuple[List[Dict[str, Any]], Snapshot]:
    """Sample focus and windows until ``wait_s`` (or, with ``stop_when_quiet``,
    until something changed and focus then stayed on a node for WINDOW_QUIET_S)."""
    events: List[Dict[str, Any]] = []
    last: Any = None
    changed_at: Optional[float] = None
    snap = drv.reader.snapshot()
    while True:
        sig = (snap.key, _windows(snap))
        if sig != last:
            ev: Dict[str, Any] = {"t": int((snap.t - t0) * 1000)}
            if last is not None and sig[1] != last[1]:
                ev["windows"] = len(sig[1])
            if last is None or sig[0] != last[0]:
                ev["focus"] = _ref(snap.focus, legacy) if snap.focus else None
            events.append(ev)
            if last is not None:
                changed_at = snap.t
            last = sig
        now = time.monotonic()
        if now - t0 >= wait_s:
            break
        # Quiet means focus has landed and stayed: no focus at all is the gap
        # before TalkBack focuses a new window (550ms or more), not an answer.
        if stop_when_quiet and changed_at is not None and snap.key is not None \
                and now - changed_at >= WINDOW_QUIET_S:
            break
        time.sleep(SAMPLE_S)
        snap = drv.reader.snapshot()
    # The answer reads the tree as it is now, not as the last event left it.
    return events, drv.reader.snapshot(fresh=True)


def _ensure_proven(drv: Driver, cur: Snapshot) -> Snapshot:
    """Prove the keymap without moving: next, then back to where we were (the
    proof may have wrapped past an edge)."""
    assert drv.inj is not None
    if drv.inj.proven:
        return cur
    return drv.return_to(cur.key, drv.prove(cur.key).snap)


def _do_action(drv: Driver, cur: Snapshot, action: str) -> Tuple[str, Snapshot]:
    """Perform ``action``; returns (what was done, the snapshot it started from)."""
    if action == "activate":
        cur = _ensure_proven(drv, cur)
        drv.press("click")
        return "activate (TalkBack click, Meta+Space)", cur
    if action == "back":
        adb.shell(drv.serial, "input keyevent KEYCODE_BACK")
        return "back (system BACK)", cur
    if action == "tap" or action.startswith("tap:"):
        sel = action.split(":", 1)[1] if ":" in action else None
        node = cur.focus if sel is None else next((n for n in cur.index.order if _match(sel, n)), None)
        if node is None:
            raise WalkError("start_not_found", f"no node to tap for {action!r}")
        x, y, w, h = node.bounds
        adb.shell(drv.serial, f"input tap {x + w // 2} {y + h // 2}")
        return f"tap {node.simple_cls} {node.label[:30]!r} (injected: bypasses TalkBack)", cur
    if action.startswith("key:"):
        spec = action[4:]
        assert drv.inj is not None
        if "META" in spec.upper() or "ALT" in spec.upper() or "CTRL" in spec.upper():
            cur = _ensure_proven(drv, cur)
        drv.inj.combo(spec)
        return f"key {spec}", cur
    if action.startswith("broadcast:"):
        args = action.split(":", 1)[1].strip()
        adb.shell(drv.serial, f"am broadcast {args}")
        return f"broadcast {args}", cur
    if action.startswith("probe:"):
        what = action.split(":", 1)[1].strip()
        adb.shell(drv.serial, f"am broadcast -a {PROBE_ACTION} -p {shlex.quote(drv.package)} "
                              f"--es action {shlex.quote(what)}")
        return f"probe {what}", cur
    raise ValueError(f"unknown action {action!r}; expected activate, back, tap[:<selector>], "
                     f"key:<combo>, broadcast:<args> or probe:<action>")


def _first_stop(snap: Snapshot, legacy: bool, window: Optional[int] = None) -> Optional[str]:
    try:
        stops, _src, _meta = predict(snap.resp, legacy)
    except Exception:  # noqa: BLE001 - classification degrades to "elsewhere"
        return None
    for s in stops:
        if window is None or s.window == window:
            return s.key
    return None


def _covered(snap: Snapshot, n: Node) -> bool:
    from .walk import _covered_by
    return _covered_by(n) is not None


def run_scenario(session: Any, kind: str, *, target: Optional[str] = None,
                 action: str = "activate", mutate: Optional[str] = None, wait_ms: int = 2000,
                 injector: str = "auto", leave_on: bool = False,
                 step_timeout_ms: int = STEP_TIMEOUT_MS, settle_ms: int = SETTLE_MS,
                 save: bool = True, hook: Optional[Any] = None, relaunch: bool = False,
                 attach: Optional[Any] = None) -> Dict[str, Any]:
    """Run one scenario (see the module docstring) and return a compact verdict.

    ``hook`` (the capture surface's) is told when TalkBack has settled
    (``hook.start(snapshot)``, then ``hook.resolve(target)`` and
    ``hook.resolve_action`` for ``tap:<ref>``), when the target
    has focus (``hook.ready(snapshot, pressed)``: ``pressed`` says keys moved
    focus there, which may have scrolled) and when the scenario is over, with
    TalkBack still on (``hook.finish(snapshot)``)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if kind == "survive" and not mutate:
        raise ValueError("survive needs mutate (tap:<selector> | activate | key:<combo> | "
                         "broadcast:<args> | probe:<action>)")
    wait_s = max(0.3, wait_ms / 1000)
    drv = Driver(session, injector=injector, utterance="auto", leave_on=leave_on,
                 step_timeout_ms=step_timeout_ms, settle_ms=settle_ms, what="tb_scenario",
                 relaunch=relaunch, attach=attach)
    out: Dict[str, Any] = {"kind": kind, "serial": session.serial, "package": session.package}
    with drv:
        # TalkBack that just started puts its own initial focus on the window ~550ms
        # later; let it, or it lands after (and over) the target we focus.
        cur = drv.settle_initial()
        legacy = bool(cur.index.legacy)
        if hook is not None:
            hook.start(cur)
            target = hook.resolve(target) if target else target
            action, mutate = hook.resolve_action(action), hook.resolve_action(mutate)
        if target:
            cur = _seek_start(drv, cur, target, "next", 60)
        if hook is not None:
            hook.ready(cur, drv.seek_presses > 0)
        out["target"] = _desc(cur.focus, legacy)
        if kind == "focus_after":
            out.update(_focus_after(drv, cur, action, wait_s, legacy))
        elif kind == "restore":
            out.update(_restore(drv, cur, wait_s, legacy))
        else:
            out.update(_survive(drv, cur, mutate or "", wait_s, legacy))
        if hook is not None:
            hook.finish(None)  # still with TalkBack on: the screen the scenario left
    out["talkback_started"] = drv.talkback_started
    out["talkback"] = f"{drv.enabled.get('version', '?')} {drv.inj.describe() if drv.inj else '?'}"
    out["restore"] = ("FAILED: " + drv.restore_error + " (run talkback restore)") if drv.restore_error \
        else "restored" if drv.restored is not None \
        else "left on (talkback restore to undo)" if drv.turned_on \
        else "unchanged (TalkBack was already on)"
    if drv.notes:
        out["notes"] = drv.notes
    if save:
        sid = "t" + _walk_id()[1:]
        path = os.path.join(walks_dir(), f"{sid}.json")
        try:
            device._write_json_atomic(path, dict(out, id=sid))
            out["saved"] = path
        except OSError:
            pass
    return out


def _focus_after(drv: Driver, cur: Snapshot, action: str, wait_s: float, legacy: bool) -> Dict[str, Any]:
    top0 = device.top_activity(drv.serial)
    what, cur = _do_action(drv, cur, action)
    t0 = time.monotonic()
    events, after = timeline(drv, t0, wait_s, True, legacy)
    top1 = device.top_activity(drv.serial)
    new_windows = [w for w in _windows(after) if w not in _windows(cur)]
    new_screen = bool(new_windows) or top1 != top0
    f0, f1 = cur.focus, after.focus
    # The model's initial-focus rule for the new window (it skips a first stop that
    # reads as the window's title), else the new window's first predicted stop.
    model = predict_initial(after.resp, new_windows[-1] if new_windows else None)
    first = (model or {}).get("key") or _first_stop(after, legacy,
                                                    new_windows[-1] if new_windows else None)
    if f1 is None:
        verdict = "none"
    elif f0 is not None and f1.key == f0.key:
        verdict = "stayed_on_opener"
    elif _covered(after, f1):
        verdict = "behind_overlay"
    elif not f1.label.strip() or _CLOSE.search(f1.label.strip()):
        verdict = "on_close_or_unlabeled"
    elif first is not None and f1.key == first:
        verdict = "initial_ok"
    else:
        verdict = "elsewhere"
    res: Dict[str, Any] = {
        "action": what, "new_screen": new_screen,
        "before": {"top": top0, "windows": len(_windows(cur)), "panes": _panes(cur)},
        "after": {"top": top1, "windows": len(_windows(after)), "panes": _panes(after)},
        "timeline": events[:12], "focus": _desc(f1, legacy), "verdict": verdict,
    }
    if model is not None:
        res["model"] = {"initial": model.get("key"), "how": model.get("how"),
                        "title": model.get("title"), "skipped": model.get("skipped") or []}
    bad = verdict in ("none", "on_close_or_unlabeled", "behind_overlay") or \
        (new_screen and verdict == "stayed_on_opener")
    if bad:
        res["finding"] = {"code": "tb.initial_focus", "sev": "warn", "basis": "walk",
                          "msg": f"after {what.split(' ')[0]}, focus is {verdict.replace('_', ' ')}"
                                 + (f" ({_ref(f1, legacy)})" if f1 is not None else ""),
                          "fix": FIXES["tb.initial_focus"]}
    return res


def _restore(drv: Driver, cur: Snapshot, wait_s: float, legacy: bool) -> Dict[str, Any]:
    f0 = cur.focus
    if f0 is None:
        raise WalkError("start_not_found", "restore needs a focused target (pass target)")
    top0 = device.top_activity(drv.serial)
    c0 = cur.index.scroll_container(f0)
    what, cur = _do_action(drv, cur, "activate")
    t0 = time.monotonic()
    ev1, opened = timeline(drv, t0, wait_s, True, legacy)
    top1 = device.top_activity(drv.serial)
    adb.shell(drv.serial, "input keyevent KEYCODE_BACK")
    t1 = time.monotonic()
    ev2, back = timeline(drv, t1, wait_s, True, legacy)
    top2 = device.top_activity(drv.serial)
    f2 = back.focus
    c2 = back.index.scroll_container(f2) if f2 is not None else None
    first = _first_stop(back, legacy)
    if f2 is None:
        verdict = "none"
    elif f2.key == f0.key or (f2.label == f0.label and f2.simple_cls == f0.simple_cls):
        verdict = "restored"
    elif first is not None and f2.key == first:
        verdict = "top"
    elif c0 is not None and c2 is not None and c0.key == c2.key:
        verdict = "near"
    else:
        verdict = "elsewhere"
    res: Dict[str, Any] = {
        "action": what,
        "opened": {"top": top1, "windows": len(_windows(opened)), "panes": _panes(opened),
                   "timeline": ev1[:6]},
        "back": {"top": top2, "windows": len(_windows(back)), "panes": _panes(back),
                 "timeline": ev2[:8]},
        "window_identity": {"before": {"top": top0, "roots": list(_windows(cur)), "panes": _panes(cur)},
                            "after": {"top": top2, "roots": list(_windows(back)), "panes": _panes(back)}},
        "focus": _desc(f2, legacy), "verdict": verdict,
    }
    if verdict not in ("restored", "near"):
        same_window = _windows(cur) == _windows(back) and top0 == top2
        why = ("single-activity navigation: the window (root, title, pane titles) did not change, so "
               "TalkBack had no per-window record to restore (a paneTitle per destination does "
               "not change that on TalkBack 17)" if same_window and top1 == top0
               else "the screen came back as a new window/activity instance")
        res["finding"] = {"code": "tb.restore_failed", "sev": "warn", "basis": "walk",
                          "msg": f"after back, focus went {verdict} instead of {_ref(f0, legacy)}; {why}",
                          "fix": FIXES["tb.restore_failed"]}
    return res


def _same_item(before: str, after: str) -> bool:
    """The same item, maybe updated: equal labels, or one extends the other at a
    word boundary ("Track 8" -> "Track 8 (played)", not "Message 1" -> "Message 10")."""
    if before == after:
        return True
    short, long_ = sorted((before, after), key=len)
    return bool(short) and long_.startswith(short) and not long_[len(short)].isalnum()


def _survive(drv: Driver, cur: Snapshot, mutate: str, wait_s: float, legacy: bool) -> Dict[str, Any]:
    f0 = cur.focus
    if f0 is None:
        raise WalkError("start_not_found", "survive needs a focused target (pass target)")
    what, cur = _do_action(drv, cur, mutate)
    t0 = time.monotonic()
    events, after = timeline(drv, t0, wait_s, False, legacy)
    f2 = after.focus
    lost_midway = any("focus" in e and e["focus"] is None for e in events[1:])
    first = _first_stop(after, legacy)
    same_ids = f2 is not None and (f2.host, f2.virtual, f2.window) == (f0.host, f0.virtual, f0.window)
    same_content = f2 is not None and _same_item(f0.label, f2.label) and f2.simple_cls == f0.simple_cls
    if f2 is None:
        verdict = "lost"
    elif same_ids and same_content:
        verdict = "kept"
    elif same_ids:
        verdict = "drifted"
    elif same_content:
        verdict = "restored"
    elif first is not None and f2.key == first:
        verdict = "reset_top"
    else:
        verdict = "moved"
    before_keys = set(cur.index.nodes)
    after_keys = set(after.index.nodes)
    cause = {"removed": len(before_keys - after_keys), "added": len(after_keys - before_keys),
             "target_still_there": f0.key in after.index.nodes}
    if f0.key in after.index.nodes and after.index.nodes[f0.key].label != f0.label:
        cause["target_rebound_to"] = after.index.nodes[f0.key].label[:40]
    res: Dict[str, Any] = {"mutate": what, "timeline": events[:12], "focus": _desc(f2, legacy),
                           "verdict": verdict, "lost_midway": lost_midway, "cause": cause}
    code = {"reset_top": "tb.focus_reset", "lost": "tb.focus_lost", "drifted": "tb.focus_drift",
            "moved": "tb.focus_reset"}.get(verdict)
    if code:
        res["finding"] = {"code": code, "sev": "warn", "basis": "walk",
                          "msg": f"after {what}, focus {verdict.replace('_', ' ')}"
                                 + (f" to {_ref(f2, legacy)}" if f2 is not None and verdict != "drifted" else "")
                                 + f" (was {_ref(f0, legacy)}; {cause['removed']} nodes removed, "
                                   f"{cause['added']} added)",
                          "fix": FIXES[code]}
    return res
