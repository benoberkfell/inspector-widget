"""TalkBack scenarios: where does focus go after an action, after back, after an update, and
what did TalkBack say?

* ``focus_after``: act and classify where focus lands (:func:`classify`):
  ``initial_ok`` (a new screen, focus on its first stop) | ``returned_to_opener`` (a window
  or screen closed, focus back on what opened it) | ``reset_to_top`` (same screen, focus
  thrown to its first stop: the target was removed) | ``moved_to_nav`` (same screen, focus
  thrown to a navigation bar or tab) | ``nothing_happened`` (focus stayed and the
  accessibility tree did not change) | ``stayed_on_opener`` | ``behind_overlay`` |
  ``on_close_or_unlabeled`` (an unlabelled node, or a close / dismiss / cancel control) |
  ``left_app`` | ``none`` | ``elsewhere``.
* ``restore``: focus a target, activate it, wait for the opened screen to settle (a window,
  pane or screen change, then focus still for a while; else "opened screen not settled"),
  go back, and classify: ``restored`` | ``near`` | ``top`` | ``none`` | ``elsewhere``.
* ``survive``: focus a target, mutate the screen, sample the focus timeline and classify:
  ``kept`` | ``drifted`` | ``restored`` | ``reset_top`` | ``lost`` | ``moved``.

The action (``action``; ``mutate`` for survive) is a sequence of steps joined by ``;``
(:data:`GRAMMAR`): ``activate`` (TalkBack's own click, Meta+Space, as a double tap),
``long_press[:<sel>]`` (ACTION_LONG_CLICK through the agent, as double-tap-and-hold),
``custom:<label>`` (the focused stop's custom action, as TalkBack's actions menu runs it),
``back``, ``tap[:<sel>]`` (an injected tap that bypasses TalkBack), ``key:<combo>``,
``broadcast:<am args>``, ``probe:<action>`` (A11yProbe's TB_PROBE receiver); then
``walk:<n>`` (press "next" up to n times and list what TalkBack read), ``expect:<label>``
(is focus on, or did the walk reach, the stop <label> names), ``wait:<ms>``; and the
preconditions ``pre:activity=<class>`` / ``pre:pane=<title>``, checked before anything is
pressed. The verdict is about the focus after the last acting step.

Every timeline carries what TalkBack said (its verbose log: ``said``), what it announced
(window titles, announcements, live regions, click feedback) and why focus went where it did
(``initial``: TalkBack's own initial focus on a window; ``restore``: put back or kept on
screen; ``user``: a navigation press; ``app``: an accessibility action). ``speak_before`` /
``speak_after`` are the target's and the landing node's utterances, and ``flags`` says
"activated, nothing spoken" or "changed, nothing spoken" when the action changed the screen
silently. Without TalkBack's verbose log (``talkback(action="on", verbose_log=true)``) the
speech is unknown, and the result says so.

All three share :class:`.walk.Driver` (TalkBack on and restored, the key guard, the
per-device lock). Targets are resolved among the model's stops (:mod:`.select`) before
TalkBack is touched, so a label no stop speaks or one several stops speak fails at once.
"""

from __future__ import annotations

import os
import re
import shlex
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .. import adb
from . import device
from . import select as tbselect
from .walk import (Driver, Node, Snapshot, STEP_TIMEOUT_MS, SETTLE_MS, WalkError, _seek_start,
                   _walk_id, predict_initial, walks_dir)

KINDS = ("focus_after", "restore", "survive")
ACTIONS = ("activate", "back", "tap")
#: Steps that act on the app (the verdict is about the focus after the last one).
ACTING = ("activate", "long_press", "custom", "back", "tap", "key", "broadcast", "probe")
#: Steps that only look, after the action.
OBSERVING = ("walk", "expect", "wait")
GRAMMAR = ("activate | long_press[:<sel>] | custom:<label> | back | tap[:<sel>] | key:<combo> | "
           "broadcast:<am args> | probe:<action> | walk:<n> | expect:<label> | wait:<ms> | "
           "pre:activity=<class> | pre:pane=<title>; chain steps with ';', e.g. "
           "'activate; walk:8' or 'pre:pane=Saved; activate; expect:Undo'")
PROBE_ACTION = "com.oberkfell.a11yprobe.TB_PROBE"
WINDOW_QUIET_S = 0.7   # TalkBack speaks a new window 550ms after it appears
SAMPLE_S = 0.03
WALK_MAX = 20          # walk:<n> presses at most
WAIT_MAX_MS = 10000
_CLOSE = re.compile(r"\b(close|dismiss|cancel)\b|^[x×✕]$", re.I)
#: Navigation bars, rails, tab rows and drawers: where a "moved_to_nav" focus lands.
_NAV_CLS = re.compile(r"BottomNavigation|NavigationBar|NavigationRail|TabLayout|TabWidget|TabView"
                      r"|NavigationView|NavigationMenu", re.I)
_ROLE_TAB = re.compile(r"\bTab$")

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
    # an action that removed the focused node on a screen that stays (a list mutation)
    "tb.focus_reset:mutation": "Once the list is laid out again, send "
                               "ACTION_ACCESSIBILITY_FOCUS to the item that took the removed "
                               "one's place, else the previous one, else the empty-state text "
                               "(View.performAccessibilityAction / the Compose host's "
                               "accessibilityNodeProvider.performAction), and announce the result "
                               "(a Snackbar with Undo, or announceForAccessibility).",
}


# --------------------------------------------------------------------------- #
# The action grammar
# --------------------------------------------------------------------------- #
@dataclass
class Step:
    kind: str
    arg: str = ""

    def __str__(self) -> str:
        return f"{self.kind}:{self.arg}" if self.arg else self.kind


def parse_action(spec: Optional[str], what: str = "action") -> List[Step]:
    """``activate; walk:8`` -> [Step(activate), Step(walk, 8)]; a ValueError naming the
    grammar for anything else (before the device is touched)."""
    steps: List[Step] = []
    for raw in str(spec or "").split(";"):
        x = raw.strip()
        if not x:
            continue
        head, _, arg = x.partition(":")
        head, arg = head.strip().lower().replace("-", "_"), arg.strip()
        ok = True
        if head in ("activate", "back"):
            ok = not arg
        elif head in ("tap", "long_press", "longpress"):
            head = "long_press" if head == "longpress" else head
        elif head in ("key", "broadcast", "probe", "custom", "expect"):
            ok = bool(arg)
        elif head == "walk":
            ok = arg.isdigit() and 1 <= int(arg) <= WALK_MAX
        elif head == "wait":
            ok = arg.isdigit() and int(arg) <= WAIT_MAX_MS
        elif head == "pre":
            k, _, v = arg.partition("=")
            ok = k.strip().lower() in ("activity", "pane") and bool(v.strip())
        else:
            ok = False
        if not ok:
            raise ValueError(f"unknown {what} step {x!r}; {what}: {GRAMMAR}")
        steps.append(Step(head, arg))
    if not steps:
        raise ValueError(f"empty {what}; {what}: {GRAMMAR}")
    return steps


def unparse(steps: List[Step]) -> str:
    return "; ".join(str(s) for s in steps)


def _activates(kind: str, steps: List[Step]) -> bool:
    """Whether the target itself gets activated (a tie must then be refused)."""
    if kind == "restore":
        return True
    first = next((s for s in steps if s.kind in ACTING), None)
    return first is not None and (first.kind in ("activate", "custom")
                                  or (first.kind in ("tap", "long_press") and not first.arg))


# --------------------------------------------------------------------------- #
# What TalkBack said: its verbose log
# --------------------------------------------------------------------------- #
_HEADER = re.compile(r"^\s*\d+\.\d+\s+\d+\s+\d+\s+[VDIWEFA]\s+[\w.-]+\s*:\s?")
#: Where an utterance ends: the flags TalkBack logs after it (queueMode=0, ttsAddToHistory,
#: forceFeedbackEvenIf..., haptic=, earcon= ...), two spaces in.
_FLAG_WORDS = (r"(?:queueMode|tts[A-Z]\w*|force\w+|advance\w+|prevent\w+|haptic=|earcon="
               r"|refresh\w+|inline\w*|interrupt\w*|skip\w*|speech\w*|flush\w*)")
_FEEDBACK = re.compile(r"TalkBackFeedbackProvider:\s+(\w+):\s+ttsOutput=\s?(.*?)"
                       r"(?=\s{2,}" + _FLAG_WORDS + r"|\s*$)")
_ENDED = re.compile(r"\s{2,}" + _FLAG_WORDS)
_REASON = re.compile(r"viewAccessibilityFocused:.*?isInitialFocus=(\w+),\s*"
                     r"isRestoreFocusOrEnsureOnScreen=(\w+),\s*isEventNavigateByUser=(\w+)")
_EDGE = re.compile(r"FocusProcessor-LogicalNav: Reach edge")


def parse_line(line: str) -> Optional[Tuple[str, str]]:
    """One TalkBack log line as (kind, value): ``tts`` (a focus utterance), ``announce``
    (``TYPE`` + tab + text: window titles, announcements, live regions, click feedback),
    ``hint`` (usage hints), ``reason`` (initial | restore | user | app), ``edge``; None for
    anything else."""
    m = _FEEDBACK.search(line)
    if m:
        etype, text = m.group(1), m.group(2).strip()
        if etype == "TYPE_VIEW_ACCESSIBILITY_FOCUSED":
            return ("tts", text) if text else None
        if etype == "EVENT_SPEAK_HINT":
            return ("hint", text) if text else None
        if etype == "TYPE_VIEW_CLICKED":
            return ("announce", f"{etype}\t{text}")  # an empty click feedback still counts
        return ("announce", f"{etype}\t{text}") if text else None
    m = _REASON.search(line)
    if m:
        initial, restore, user = (g == "true" for g in m.groups())
        return ("reason", "initial" if initial else "restore" if restore else "user" if user
                else "app")
    if _EDGE.search(line):
        return ("edge", "")
    return None


class SpeechLog:
    """``adb logcat --pid=<TalkBack>`` read for the scenario: (t, kind, value) events, t on
    the host's monotonic clock. A continuation line of a long utterance is joined to it."""

    def __init__(self, serial: str) -> None:
        self.serial = serial
        self.proc: Any = None
        self.events: List[Tuple[float, str, str]] = []
        self.lines = 0
        self._lock = threading.Lock()
        self._open: Optional[int] = None  # index of an utterance whose line was cut

    def start(self) -> bool:
        pid = device.talkback_pid(self.serial)
        if pid is None:
            return False
        try:
            self.proc = device.popen(self.serial, ["logcat", "-v", "epoch", f"--pid={pid}", "-T", "1"])
        except OSError:
            return False
        threading.Thread(target=self._read, name="tb-scenario-logcat", daemon=True).start()
        return True

    def feed(self, raw: Any, now: Optional[float] = None) -> None:
        """One line of the log. An utterance with a line break goes on over the next lines
        (with or without logcat's header) until TalkBack's flags end it: those lines are
        joined to it."""
        line = raw.decode("utf-8", "replace").rstrip("\n") if isinstance(raw, bytes) \
            else str(raw).rstrip("\n")
        now = time.monotonic() if now is None else now
        with self._lock:
            self.lines += 1
            ev = parse_line(line)
            if ev is None and self._open is not None:
                head = _HEADER.match(line)
                more = line[head.end():] if head else line
                if not (head and ":" in more.split(" ", 1)[0]):  # not another tag's line
                    t, k, v = self.events[self._open]
                    m = _ENDED.search(more)
                    piece = (more[:m.start()] if m else more).strip()
                    self.events[self._open] = (t, k, f"{v} {piece}".strip())
                    if m:
                        self._open = None
                    return
            self._open = None
            if ev is None:
                return
            self.events.append((now, ev[0], ev[1]))
            m = _FEEDBACK.search(line)
            if ev[0] in ("tts", "announce") and m is not None and not _ENDED.search(line, m.end(2)):
                self._open = len(self.events) - 1  # cut at a line break: more lines follow

    def _read(self) -> None:
        out = self.proc.stdout
        while True:
            try:
                raw = out.readline()
            except (OSError, ValueError):
                return
            if not raw:
                return
            self.feed(raw)

    def since(self, t: float, kind: Optional[str] = None) -> List[Tuple[float, str, str]]:
        with self._lock:
            return [e for e in self.events if e[0] >= t and (kind is None or e[1] == kind)]

    def before(self, t: float, kind: str) -> Optional[Tuple[float, str, str]]:
        with self._lock:
            hits = [e for e in self.events if e[0] < t and e[1] == kind]
        return hits[-1] if hits else None

    @property
    def verbose(self) -> bool:
        with self._lock:
            return any(e[1] in ("tts", "reason", "hint") for e in self.events)

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        for fn in ("terminate", "kill"):
            try:
                getattr(proc, fn)()
                proc.wait(timeout=2)
                return
            except Exception:  # noqa: BLE001
                continue


def _said(log: Optional[SpeechLog], t0: float, t1: Optional[float] = None) -> List[Dict[str, Any]]:
    """What TalkBack said in [t0, t1) as timeline entries (ms from t0): each utterance with
    the focus reason logged just before it (``why``), and each announcement."""
    out: List[Dict[str, Any]] = []
    if log is None:
        return out
    reason: Optional[str] = None
    for t, kind, v in log.since(t0):
        if t1 is not None and t >= t1:
            break
        ms = int((t - t0) * 1000)
        if kind == "reason":
            reason = v
        elif kind == "tts":
            e: Dict[str, Any] = {"t": ms, "said": v}
            if reason:
                e["why"] = reason
            reason = None
            out.append(e)
        elif kind == "announce":
            etype, _, text = v.partition("\t")
            if text:
                out.append({"t": ms, "announced": text,
                            "event": etype.replace("TYPE_", "").lower()})
    return out


def _merge(events: List[Dict[str, Any]], said: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The focus timeline with what TalkBack said, in time order."""
    return sorted(events + said, key=lambda e: (e.get("t", 0), "said" in e or "announced" in e))


def _speech(log: Optional[SpeechLog], t0: float, wait_s: float, acted: str,
            changed: bool) -> Dict[str, Any]:
    """speak_before / speak_after / announced / flags around the action at ``t0``."""
    out: Dict[str, Any] = {}
    if log is None or not log.verbose:
        out["speech"] = ("not logged: TalkBack's log level is not VERBOSE (talkback(action='on', "
                         "verbose_log=true) first)")
        return out
    before = log.before(t0, "tts")
    if before is not None:
        out["speak_before"] = before[2]
    after = [e for e in log.since(t0) if e[0] < t0 + wait_s + 1.0]
    tts = [v for _t, k, v in after if k == "tts"]
    ann = [v.partition("\t")[2] for _t, k, v in after if k == "announce"]
    ann = [a for a in ann if a]
    if tts:
        out["speak_after"] = tts[-1]
    if ann:
        out["announced"] = list(dict.fromkeys(ann))[:4]
    flags = []
    if not tts and not ann:
        if acted in ("activate", "long_press", "custom", "tap"):
            flags.append("activated, nothing spoken")
        if changed:
            flags.append("changed, nothing spoken")
    if flags:
        out["flags"] = flags
    return out


# --------------------------------------------------------------------------- #
# Focus and window sampling
# --------------------------------------------------------------------------- #
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


def settle(drv: Driver, before: Snapshot, t0: float, wait_s: float, legacy: bool,
           quiet_s: Optional[float] = None) -> Tuple[List[Dict[str, Any]], Snapshot, bool]:
    """Wait for the screen an action opened to settle (gap G13): a change of screen first
    (a window or pane appeared or went, or the target left the tree or the screen), then
    focus on one node, with windows and panes unchanged, for ``quiet_s`` (at least the
    window-change quiet TalkBack itself waits). Bounded by ``wait_s``; (timeline, snapshot,
    settled)."""
    quiet = max(drv.quiet_s, WINDOW_QUIET_S) if quiet_s is None else quiet_s
    target = before.key
    base = (_windows(before), tuple(_panes(before)))
    events: List[Dict[str, Any]] = []
    last: Any = None
    moved_at: Optional[float] = None
    stable_since: Optional[float] = None
    snap = drv.reader.snapshot()
    while True:
        screen = (_windows(snap), tuple(_panes(snap)))
        sig = (snap.key, screen)
        if sig != last:
            ev: Dict[str, Any] = {"t": int((snap.t - t0) * 1000)}
            if last is not None and screen[0] != last[1][0]:
                ev["windows"] = len(screen[0])
            if last is None or sig[0] != last[0]:
                ev["focus"] = _ref(snap.focus, legacy) if snap.focus else None
            events.append(ev)
            last, stable_since = sig, snap.t
        node = snap.index.nodes.get(target) if target is not None else None
        gone = target is not None and (node is None or "visible_to_user" not in node.flags)
        if moved_at is None and (screen != base or gone):
            moved_at = snap.t
        now = time.monotonic()
        if moved_at is not None and snap.key is not None and stable_since is not None \
                and now - max(stable_since, moved_at) >= quiet:
            return events, drv.reader.snapshot(fresh=True), True
        if now - t0 >= wait_s:
            return events, drv.reader.snapshot(fresh=True), False
        time.sleep(SAMPLE_S)
        snap = drv.reader.snapshot()


def _ensure_proven(drv: Driver, cur: Snapshot) -> Snapshot:
    """Prove the keymap without moving: next, then back to where we were (the
    proof may have wrapped past an edge)."""
    assert drv.inj is not None
    if drv.inj.proven:
        return cur
    return drv.return_to(cur.key, drv.prove(cur.key).snap)


# --------------------------------------------------------------------------- #
# Acting
# --------------------------------------------------------------------------- #
def _dump(snap: Snapshot) -> Tuple[Dict[str, Any], List[tbselect.Stop]]:
    from .. import a11y
    if snap.resp is None:
        return {}, []
    d = a11y.a11y_to_dict(snap.resp)
    return d, tbselect.stops_from_dump(d, legacy=bool(snap.index.legacy))


def _screen(drv: Driver) -> str:
    try:
        top = device.top_activity(drv.serial) or ""
    except Exception:  # noqa: BLE001 - only for a message
        top = ""
    return top.rsplit("/", 1)[-1].rsplit(".", 1)[-1] or "the screen"


def _stop_for(drv: Driver, cur: Snapshot, sel: Optional[str]) -> tbselect.Stop:
    """The stop ``sel`` names on the screen now (an activation: a tie is refused), or the
    focused stop."""
    _d, stops = _dump(cur)
    if sel is None:
        key = cur.key
        hit = next((s for s in stops if s.key == key or (key and key in s.inner)), None)
        if hit is None and cur.focus is not None:
            n = cur.focus
            hit = tbselect.Stop(key=n.key, order=0, speech=n.speech(80), label=n.label, cd=n.cd,
                                text=n.text, cls=n.simple_cls, bounds=n.bounds, window=n.window,
                                node={})
        if hit is None:
            raise WalkError("start_not_found", "nothing has accessibility focus to act on",
                            hint="Pass target, or tap:<sel> / long_press:<sel>.")
        return hit
    return tbselect.require(stops, sel, activate=True, where=_screen(drv)).node


def _a11y_act(drv: Driver, key: str, action: str, raw: int = 0) -> None:
    from .. import a11y
    try:
        d = a11y.a11y_act_to_dict(drv.session.a11y_act(node_key=key, action=action,
                                                       raw_action_id=raw))
    except ValueError as exc:
        raise WalkError("talkback_error", f"{action} on {key}: {exc}") from None
    if not d.get("performed"):
        raise WalkError("talkback_error", f"{action} on {key} was refused: {d.get('error')}",
                        hint="An agent with A11yAct performs it; the node may be gone.")


def _custom(drv: Driver, cur: Snapshot, label: str) -> Tuple[str, int]:
    """(the focused stop's key, the id of its custom action ``label``). The C11 trap, an
    action on a node TalkBack never focuses, is named, not used."""
    d, stops = _dump(cur)
    key = cur.key
    stop = next((s for s in stops if s.key == key or (key and key in s.inner)), None)
    want = tbselect.norm(label)
    if stop is not None:
        for lab, aid in stop.custom.items():
            if tbselect.norm(lab) == want:
                return stop.key, aid
    offered = sorted(stop.custom) if stop is not None else []
    elsewhere = None
    for n, _w in tbselect._iter(d.get("windows") or []):
        if any(tbselect.norm(lab) == want for lab in tbselect.custom_actions(n)):
            elsewhere = n.get("node_key")
            break
    msg = (f"the focused stop {key} offers no custom action {label!r}"
           + (f" (it offers {', '.join(repr(o) for o in offered[:4])})" if offered else
              " (it offers none)"))
    if elsewhere:
        msg += f"; {elsewhere} has it, a node TalkBack never focuses: put the action on the stop"
    raise WalkError("not_found", msg, hint="custom:<label> runs a custom action of the focused "
                                           "stop, as TalkBack's actions menu lists them.")


def _do_step(drv: Driver, cur: Snapshot, step: Step) -> Tuple[str, Snapshot]:
    """Perform one acting step; returns (what was done, the snapshot it started from).
    ``drv.acted_at`` is when the act itself was sent (after any keymap proof): what
    TalkBack says from then on is the action's."""
    drv.acted_at = time.monotonic()
    k, arg = step.kind, step.arg
    if k == "activate":
        cur = _ensure_proven(drv, cur)
        drv.acted_at = time.monotonic()
        drv.press("click")
        return "activate (TalkBack click, Meta+Space)", cur
    if k == "back":
        adb.shell(drv.serial, "input keyevent KEYCODE_BACK")
        return "back (system BACK)", cur
    if k == "tap":
        node = cur.index.nodes.get(arg) if arg and tbselect.is_key(arg) else None
        if node is not None:  # a node key (a ref the capture resolved): that very node
            (x, y, w, h), cls, words = node.bounds, node.simple_cls, node.label
        else:
            stop = _stop_for(drv, cur, arg or None)
            (x, y, w, h), cls, words = stop.bounds, stop.cls, stop.head(30)
        adb.shell(drv.serial, f"input tap {x + w // 2} {y + h // 2}")
        return f"tap {cls} {words[:30]!r} (injected: bypasses TalkBack)", cur
    if k == "long_press":
        stop = _stop_for(drv, cur, arg or None)
        _a11y_act(drv, stop.key, "long_click")
        return f"long_press {stop.key} (ACTION_LONG_CLICK, as double-tap-and-hold)", cur
    if k == "custom":
        key, aid = _custom(drv, cur, arg)
        _a11y_act(drv, key, "raw", aid)
        return f"custom {arg!r} on {key} (as TalkBack's actions menu)", cur
    if k == "key":
        assert drv.inj is not None
        if "META" in arg.upper() or "ALT" in arg.upper() or "CTRL" in arg.upper():
            cur = _ensure_proven(drv, cur)
        drv.acted_at = time.monotonic()
        drv.inj.combo(arg)
        return f"key {arg}", cur
    if k == "broadcast":
        adb.shell(drv.serial, f"am broadcast {arg}")
        return f"broadcast {arg}", cur
    if k == "probe":
        adb.shell(drv.serial, f"am broadcast -a {PROBE_ACTION} -p {shlex.quote(drv.package)} "
                              f"--es action {shlex.quote(arg)}")
        return f"probe {arg}", cur
    raise ValueError(f"unknown action step {str(step)!r}; action: {GRAMMAR}")


def _do_action(drv: Driver, cur: Snapshot, action: str) -> Tuple[str, Snapshot]:
    """Perform ``action`` (its acting steps); returns (what was done, the snapshot it
    started from)."""
    whats: List[str] = []
    start: Optional[Snapshot] = None
    for st in (s for s in parse_action(action) if s.kind in ACTING):
        w, at = _do_step(drv, cur, st)
        start = start or at
        whats.append(w)
    return "; ".join(whats), start or cur


def _check_pre(drv: Driver, cur: Snapshot, pre: List[Step]) -> None:
    """``pre:activity=<cls>`` / ``pre:pane=<title>``: fail before anything is pressed."""
    for st in pre:
        k, _, v = st.arg.partition("=")
        k, v = k.strip().lower(), v.strip()
        if k == "activity":
            top = device.top_activity(drv.serial) or "?"
            cls = top.split("/", 1)[-1]
            if not (cls == v or cls.endswith(("." + v.lstrip("."), "$" + v)) or top == v):
                raise WalkError("not_found", f"precondition pre:activity={v} failed: {top} is in "
                                             f"front; nothing was pressed",
                                hint="Open that screen first, or drop the pre: step.")
        else:
            panes = _panes(cur)
            titles = set(panes) | {n.label for n in cur.index.order if n.label and (
                "heading" in n.flags or n.simple_cls in ("TextView", "Toolbar"))}
            if not any(tbselect.norm(v) == tbselect.norm(p) for p in titles):
                raise WalkError("not_found", f"precondition pre:pane={v} failed: panes on screen: "
                                             f"{', '.join(panes) or 'none'}; nothing was pressed",
                                hint="Open that pane first, or drop the pre: step.")


# --------------------------------------------------------------------------- #
# Observing: walk:<n>, expect:<label>
# --------------------------------------------------------------------------- #
def _walk_n(drv: Driver, cur: Snapshot, n: int, log: Optional[SpeechLog],
            legacy: bool) -> Tuple[List[Dict[str, Any]], Snapshot]:
    """Press "next" up to ``n`` times: [{i, ref, key, speak, utt} | {i, edge}] (speech from
    the log when it has the press's utterance, else the model's)."""
    if drv.inj is not None and not drv.inj.proven:
        cur = _ensure_proven(drv, cur)
    out: List[Dict[str, Any]] = []
    no_moves = 0
    for i in range(1, n + 1):
        t, _ = drv.press("next")
        w = drv.wait(cur.key, t)
        if not w.moved:
            out.append({"i": i, "edge": True})
            no_moves += 1
            if no_moves >= 2:
                break
            continue
        no_moves = 0
        cur = w.snap
        said = [v for _t, _k, v in (log.since(t, "tts") if log is not None else [])]
        f = cur.focus
        out.append({"i": i, "ref": _ref(f, legacy), "key": cur.key,
                    "speak": said[-1] if said else (f.speech(80) if f is not None else ""),
                    "utt": "logcat" if said else "model"})
    return out, cur


def _expect(cur: Snapshot, label: str, walked: List[Dict[str, Any]],
            legacy: bool) -> Dict[str, Any]:
    """Is focus on the stop ``label`` names, or did the walk before reach it?"""
    _d, stops = _dump(cur)
    m = tbselect.resolve(stops, label)
    keys = {c.key for c in m.candidates} if m is not None else set()
    if tbselect.is_key(label):
        keys.add(label)
    want = tbselect.norm(label)
    if cur.key in keys:
        return {"label": label, "reached": True, "at": "focus"}
    for w in walked:
        if w.get("key") in keys or (want and tbselect._rank(want, tbselect.norm(w.get("speak"))) in (0, 1)):
            return {"label": label, "reached": True, "at": f"walk press {w['i']}"}
    out: Dict[str, Any] = {"label": label, "reached": False, "focus": _ref(cur.focus, legacy)}
    if walked:
        out["within"] = f"{len(walked)} presses"
    if m is None:
        out["on_screen"] = False
    return out


def _observe(drv: Driver, cur: Snapshot, steps: List[Step], log: Optional[SpeechLog],
             legacy: bool) -> Tuple[Dict[str, Any], Snapshot]:
    out: Dict[str, Any] = {}
    walked: List[Dict[str, Any]] = []
    expects: List[Dict[str, Any]] = []
    for st in steps:
        if st.kind == "wait":
            time.sleep(int(st.arg) / 1000)
        elif st.kind == "walk":
            got, cur = _walk_n(drv, cur, int(st.arg), log, legacy)
            walked += got
            out["walk"] = walked
        elif st.kind == "expect":
            expects.append(_expect(cur, st.arg, walked, legacy))
    if expects:
        out["expect"] = expects[0] if len(expects) == 1 else expects
    return out, cur


# --------------------------------------------------------------------------- #
# The scenario
# --------------------------------------------------------------------------- #
def _first_stop(snap: Snapshot, legacy: bool, window: Optional[int] = None) -> Optional[str]:
    """The model's first stop (of ``window``): :mod:`.select`'s stops, which a blank window
    left over on top does not hide."""
    try:
        stops = _dump(snap)[1]
    except Exception:  # noqa: BLE001 - classification degrades to "elsewhere"
        return None
    for s in stops:
        if window is None or s.window == window:
            return s.key
    return None


def _covered(snap: Snapshot, n: Node) -> Optional[Dict[str, Any]]:
    from .walk import _covered_by
    return _covered_by(n)


_CAPTURE_SEL = re.compile(r'^n\d+$|"| > |^(?:view|sem|a11y|w):|^[#@]')


def _precheck(session: Any, target: Optional[str], steps: List[Step], activate: bool) -> None:
    """Resolve plain-label selectors on a dump taken before TalkBack is touched: a label no
    stop speaks (with nothing that could scroll it in) and an activation several stops would
    match fail here, in a fraction of a second, with nothing pressed. Keys, refs and capture
    selectors are resolved later (the capture, a fresh dump)."""
    sels: List[Tuple[str, bool]] = []
    if target and not tbselect.is_key(target) and not _CAPTURE_SEL.search(target):
        sels.append((target, activate))
    for st in steps:
        if st.kind in ("tap", "long_press") and st.arg and not tbselect.is_key(st.arg) \
                and not _CAPTURE_SEL.search(st.arg):
            sels.append((st.arg, True))
    if not sels or not callable(getattr(session, "dump_a11y", None)):
        return
    from .. import a11y
    try:
        d = a11y.a11y_to_dict(session.dump_a11y(include_extras=True))
    except Exception:  # noqa: BLE001 - the scenario resolves it again with TalkBack on
        return
    stops = tbselect.stops_from_dump(d)
    if not stops:
        return
    for sel, act in sels:
        m = tbselect.resolve(stops, sel)
        if m is None:
            if tbselect.can_bring_more(d) is None:
                try:
                    top = device.top_activity(session.serial) or ""
                except Exception:  # noqa: BLE001
                    top = ""
                raise tbselect.not_found_error(sel, len(stops),
                                               top.rsplit("/", 1)[-1].rsplit(".", 1)[-1])
            continue
        if act:
            tbselect.vet(m, activate=True)  # a tie, or a match too loose to activate


def run_scenario(session: Any, kind: str, *, target: Optional[str] = None,
                 action: str = "activate", mutate: Optional[str] = None, wait_ms: int = 2000,
                 injector: str = "auto", leave_on: bool = False,
                 step_timeout_ms: int = STEP_TIMEOUT_MS, settle_ms: int = SETTLE_MS,
                 save: bool = True, hook: Optional[Any] = None) -> Dict[str, Any]:
    """Run one scenario (see the module docstring) and return a compact verdict.

    ``hook`` (the capture surface's) is told when TalkBack has settled
    (``hook.start(snapshot)``, then ``hook.resolve(target)`` and
    ``hook.resolve_action`` for the selectors in the action), when the target
    has focus (``hook.ready(snapshot, pressed)``: ``pressed`` says keys moved
    focus there, or a search scrolled, either of which may have moved the screen)
    and when the scenario is over, with TalkBack still on (``hook.finish(snapshot)``)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if kind == "survive" and not mutate:
        raise ValueError("survive needs mutate (tap:<selector> | activate | key:<combo> | "
                         "broadcast:<args> | probe:<action>; mutate: " + GRAMMAR + ")")
    what = "mutate" if kind == "survive" else "action"
    spec = mutate if kind == "survive" else action
    steps = parse_action(spec, what)
    if kind != "restore" and not any(s.kind in ACTING for s in steps):
        raise ValueError(f"{what} {spec!r} has no step that acts; {what}: {GRAMMAR}")
    activates = _activates(kind, steps)
    _precheck(session, target, steps, activates)
    wait_s = max(0.3, wait_ms / 1000)
    drv = Driver(session, injector=injector, utterance="model", leave_on=leave_on,
                 step_timeout_ms=step_timeout_ms, settle_ms=settle_ms, what="tb_scenario")
    out: Dict[str, Any] = {"kind": kind, "serial": session.serial, "package": session.package}
    log = SpeechLog(session.serial)
    with drv:
        lg = log if log.start() else None
        try:
            # TalkBack that just started puts its own initial focus on the window ~550ms
            # later; let it, or it lands after (and over) the target we focus.
            cur = drv.settle_initial()
            legacy = bool(cur.index.legacy)
            pre = [s for s in steps if s.kind == "pre"]
            steps = [s for s in steps if s.kind != "pre"]
            _check_pre(drv, cur, pre)
            if hook is not None:
                hook.start(cur)
                target = hook.resolve(target) if target else target
                steps = parse_action(hook.resolve_action(unparse(steps)), what)
            if target:
                n_notes = len(drv.notes)
                cur = _seek_start(drv, cur, target, "next", 60, activate=activates)
                matched = [x for x in drv.notes[n_notes:]
                           if x.startswith(("target matched=", "start matched="))]
                if matched:  # "target matched=label (exact) view:12 ..." -> "label (exact) ..."
                    out["matched"] = matched[-1].split("=", 1)[1]
                    drv.notes.remove(matched[-1])
            if hook is not None:
                hook.ready(cur, drv.seek_presses > 0 or any("scrolled" in x for x in drv.notes))
            out["target"] = _desc(cur.focus, legacy)
            if kind == "focus_after":
                out.update(_focus_after(drv, cur, steps, wait_s, legacy, lg))
            elif kind == "restore":
                out.update(_restore(drv, cur, steps, wait_s, legacy, lg))
            else:
                out.update(_survive(drv, cur, steps, wait_s, legacy, lg))
            if hook is not None:
                hook.finish(None)  # still with TalkBack on: the screen the scenario left
        finally:
            log.stop()
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


def _act_all(drv: Driver, cur: Snapshot, acting: List[Step], wait_s: float, legacy: bool,
             quiet: bool = True
             ) -> Tuple[List[str], Snapshot, Snapshot, List[Dict[str, Any]], float, bool]:
    """Run the acting steps, each followed by its focus timeline. (what was done, the
    snapshot before the first, the one after the last, the last step's timeline, its start
    time, whether a screen opened in between)."""
    whats: List[str] = []
    start: Optional[Snapshot] = None
    events: List[Dict[str, Any]] = []
    t0 = time.monotonic()
    opened = False
    for i, st in enumerate(acting):
        what, at = _do_step(drv, cur, st)
        if start is None:
            start = at
        whats.append(what)
        t0 = getattr(drv, "acted_at", None) or time.monotonic()
        events, cur = timeline(drv, t0, wait_s, quiet, legacy)
        if i < len(acting) - 1 and (len(_windows(cur)) > len(_windows(start))
                                    or any(_screen_pane(cur, p) for p in
                                           set(_panes(cur)) - set(_panes(start)))
                                    or (start.key is not None and start.key not in cur.index.nodes)):
            opened = True
    assert start is not None
    return whats, start, cur, events, t0, opened


@dataclass
class Facts:
    """What :func:`classify` decides from (plain values, so it is testable offline)."""

    f0_key: Optional[str]
    f1_key: Optional[str]
    f1_label: str = ""
    f1_speech: str = ""
    f1_unlabelled: bool = False
    f1_nav: bool = False
    f0_nav: bool = False
    f1_covered: Optional[str] = None
    same_node: bool = False
    target_gone: bool = False
    tree_changed: bool = False
    new_screen: bool = False
    closed: bool = False
    f1_was_there: bool = False
    left_app: bool = False
    first_key: Optional[str] = None
    model_initial: Optional[str] = None
    opened_between: bool = False
    target_clicks: bool = True


def classify(f: Facts) -> Tuple[str, str]:
    """(verdict, why) for focus_after (the module docstring lists the verdicts)."""
    gone = " (the target was removed)" if f.target_gone else ""
    if f.f1_key is None:
        return "none", "no node holds accessibility focus" + gone
    if f.left_app:
        return "left_app", "the action left the app"
    if f.same_node:
        if f.opened_between:
            return "returned_to_opener", "a screen opened and closed; focus is back on the target"
        if not f.tree_changed and not f.new_screen and not f.closed:
            return "nothing_happened", ("focus stayed and nothing changed in the accessibility "
                                        "tree" + ("" if f.target_clicks else
                                                  "; the target offers no click action"))
        return "stayed_on_opener", "focus stayed on the target"
    if f.f1_covered:
        return "behind_overlay", f"focus is under {f.f1_covered}"
    if f.f1_unlabelled or _CLOSE.search(f.f1_label.strip()):
        return "on_close_or_unlabeled", ("focus is on an unlabelled node" if f.f1_unlabelled
                                         else "focus is on a close / dismiss control")
    if f.closed and f.f1_was_there and f.f1_key != f.first_key:
        return "returned_to_opener", "a window closed; focus went back to a node under it"
    if f.new_screen:
        if f.f1_key in (f.model_initial, f.first_key):
            return "initial_ok", "a new screen; focus on its first stop"
        return "elsewhere", "a new screen; focus not on its first stop"
    if f.f1_key == f.first_key:  # (a navigation rail's first tab is the first stop too)
        return "reset_to_top", "same screen; focus thrown to its first stop" + gone
    if f.f1_nav and not f.f0_nav:
        return "moved_to_nav", "same screen; focus thrown to the navigation bar" + gone
    return "elsewhere", "same screen; focus somewhere else" + gone


def _is_nav(n: Optional[Node], stop: Optional[tbselect.Stop]) -> bool:
    if n is None:
        return False
    if stop is not None and (_ROLE_TAB.search(stop.speech or "")
                             or str(stop.node.get("role_description") or "").lower() == "tab"):
        return True
    return any(_NAV_CLS.search(a.cls or "") for a in [n, *list(n.ancestors())[:4]])


def _screen_pane(s: Snapshot, title: str) -> bool:
    """Whether the pane ``title`` is a screen (a destination, a sheet), not a snackbar or a
    banner ("Alert"): it covers at least 40% of its window."""
    for n in s.index.order:
        if n.pane_title != title:
            continue
        win = s.index.window_rect(n.window)
        if win is None or win[2] * win[3] <= 0:
            return True
        if n.bounds[2] * n.bounds[3] >= 0.4 * win[2] * win[3]:
            return True
    return False


def _sig(s: Snapshot) -> set:
    return {(n.key, n.label, frozenset(n.flags & {"checked", "visible_to_user"}))
            for n in s.index.order}


def facts(drv: Driver, before: Snapshot, after: Snapshot, top0: Optional[str],
          top1: Optional[str], legacy: bool, opened_between: bool = False) -> Facts:
    """The :class:`Facts` of a focus_after from the snapshots before and after the action."""
    f0, f1 = before.focus, after.focus
    _d1, stops1 = _dump(after)
    by_key = {s.key: s for s in stops1}
    s1 = by_key.get(after.key or "") or next(
        (s for s in stops1 if after.key and after.key in s.inner), None)
    _d0, stops0 = _dump(before)
    s0 = next((s for s in stops0 if s.key == before.key), None)
    new_windows = [w for w in _windows(after) if w not in _windows(before)]
    closed = any(w not in _windows(after) for w in _windows(before))
    left = bool(top1) and not str(top1).startswith(drv.package + "/")
    new_panes = {p for p in set(_panes(after)) - set(_panes(before)) if _screen_pane(after, p)}
    c0 = before.index.scroll_container(f0) if f0 is not None else None
    container_gone = c0 is not None and c0.key not in after.index.nodes
    kept = sum(1 for s in stops0 if s.key in by_key)
    new_screen = bool(new_windows) or (top0 != top1 and not left) or bool(new_panes) or (
        container_gone and kept < len(stops0) / 2)
    node0 = after.index.nodes.get(f0.key) if f0 is not None else None
    target_gone = f0 is not None and (node0 is None or "visible_to_user" not in node0.flags)
    same = f0 is not None and f1 is not None and (f1.key == f0.key or (
        not target_gone and f1.label == f0.label and f1.simple_cls == f0.simple_cls
        and f1.window == f0.window))
    model = predict_initial(after.resp, new_windows[-1] if new_windows else None)
    cov = _covered(after, f1) if f1 is not None else None
    return Facts(
        f0_key=before.key, f1_key=after.key, f1_label=(f1.label if f1 is not None else ""),
        f1_speech=(s1.speech if s1 is not None else (f1.speech(80) if f1 is not None else "")),
        f1_unlabelled=f1 is not None and not (f1.label.strip() or (s1 is not None and s1.parts)),
        f1_nav=_is_nav(f1, s1), f0_nav=_is_nav(f0, s0),
        f1_covered=(f"{cov.get('cls')} {cov.get('overlay')}" if cov else None),
        same_node=same, target_gone=target_gone, tree_changed=_sig(before) != _sig(after),
        new_screen=new_screen, closed=closed,
        f1_was_there=f1 is not None and f1.key in before.index.nodes, left_app=left,
        first_key=_first_stop(after, legacy, f1.window if f1 is not None else None),
        model_initial=(model or {}).get("key"), opened_between=opened_between,
        target_clicks=f0 is None or bool(f0.actions & {0x10}) or "clickable" in f0.flags)


_WENT = {"reset_to_top": "to the top", "moved_to_nav": "to the navigation bar",
         "none": "nowhere (no node holds it)"}


def _quote(s: str, n: int = 40) -> str:
    s = (s or "").replace("\n", " ")
    return '"' + (s if len(s) <= n else s[: n - 1] + "…") + '"'


def _focus_after(drv: Driver, cur: Snapshot, steps: List[Step], wait_s: float, legacy: bool,
                 log: Optional[SpeechLog] = None) -> Dict[str, Any]:
    acting = [s for s in steps if s.kind in ACTING]
    last = max(i for i, s in enumerate(steps) if s.kind in ACTING)
    post = [s for s in steps[last + 1:] if s.kind in OBSERVING]
    top0 = device.top_activity(drv.serial)
    whats, start, after, events, t0, opened = _act_all(drv, cur, acting, wait_s, legacy)
    top1 = device.top_activity(drv.serial)
    fx = facts(drv, start, after, top0, top1, legacy, opened)
    verdict, why = classify(fx)
    new_windows = [w for w in _windows(after) if w not in _windows(start)]
    model = predict_initial(after.resp, new_windows[-1] if new_windows else None)
    ref = _ref(after.focus, legacy)
    spoken = _quote(fx.f1_speech)
    res: Dict[str, Any] = {
        "action": "; ".join(whats), "new_screen": fx.new_screen,
        "before": {"top": top0, "windows": len(_windows(start)), "panes": _panes(start)},
        "after": {"top": top1, "windows": len(_windows(after)), "panes": _panes(after)},
        "timeline": _merge(events[:12], _said(log, t0)[:8]), "focus": _desc(after.focus, legacy),
        "verdict": verdict, "why": why,
    }
    res.update(_speech(log, t0, wait_s, acting[-1].kind, fx.tree_changed))
    if model is not None:
        res["model"] = {"initial": model.get("key"), "how": model.get("how"),
                        "title": model.get("title"), "skipped": model.get("skipped") or []}
    did = whats[0].split(" ")[0]
    went = _WENT.get(verdict, verdict.replace("_", " "))
    if fx.target_gone and not fx.new_screen and verdict in (
            "reset_to_top", "moved_to_nav", "elsewhere", "none"):
        res["finding"] = {"code": "tb.focus_reset", "sev": "warn", "basis": "walk",
                          "msg": f"after {did}, the focused target was removed and focus went "
                                 f"{went}" + (f" ({ref} {spoken})" if ref else ""),
                          "fix": FIXES["tb.focus_reset:mutation"]}
    elif verdict in ("reset_to_top", "moved_to_nav"):
        res["finding"] = {"code": "tb.focus_reset", "sev": "warn", "basis": "walk",
                          "msg": f"after {did}, focus went {went} on the same screen "
                                 f"({ref} {spoken})",
                          "fix": FIXES["tb.focus_reset:mutation"]}
    elif verdict in ("none", "on_close_or_unlabeled", "behind_overlay") or \
            (fx.new_screen and verdict in ("stayed_on_opener", "elsewhere")):
        res["finding"] = {"code": "tb.initial_focus", "sev": "warn", "basis": "walk",
                          "msg": f"after {did}, focus is {verdict.replace('_', ' ')}"
                                 + (f" ({ref} {spoken})" if ref else ""),
                          "fix": FIXES["tb.initial_focus"]}
    if post:
        obs, _cur = _observe(drv, after, post, log, legacy)
        res.update(obs)
    return res


def _restore(drv: Driver, cur: Snapshot, steps: List[Step], wait_s: float, legacy: bool,
             log: Optional[SpeechLog] = None) -> Dict[str, Any]:
    f0 = cur.focus
    if f0 is None:
        raise WalkError("start_not_found", "restore needs a focused target (pass target)")
    acting = [s for s in steps if s.kind in ACTING and s.kind != "back"] or [Step("activate")]
    post = [s for s in steps if s.kind in OBSERVING]
    top0 = device.top_activity(drv.serial)
    c0 = cur.index.scroll_container(f0)
    start = cur
    whats = []
    t0 = time.monotonic()
    for st in acting:
        what, _at = _do_step(drv, cur, st)
        whats.append(what)
        t0 = getattr(drv, "acted_at", None) or time.monotonic()
    ev1, opened, settled = settle(drv, start, t0, wait_s, legacy)
    top1 = device.top_activity(drv.serial)
    t1 = time.monotonic()
    adb.shell(drv.serial, "input keyevent KEYCODE_BACK")
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
    said1 = _said(log, t0, t1)
    said2 = _said(log, t1)
    reason = next((e.get("why") for e in reversed(said2) if e.get("said") and e.get("why")), None)
    res: Dict[str, Any] = {
        "action": "; ".join(whats),
        "opened": {"top": top1, "windows": len(_windows(opened)), "panes": _panes(opened),
                   "settled": settled, "timeline": _merge(ev1[:6], said1[:4])},
        "back": {"top": top2, "windows": len(_windows(back)), "panes": _panes(back),
                 "timeline": ev2[:8]},
        "timeline": _merge(ev2[:8], said2[:6]),
        "window_identity": {"before": {"top": top0, "roots": list(_windows(cur)), "panes": _panes(cur)},
                            "after": {"top": top2, "roots": list(_windows(back)), "panes": _panes(back)}},
        "focus": _desc(f2, legacy), "verdict": verdict,
    }
    if not settled:
        drv.notes.append(f"opened screen not settled within wait_ms={int(wait_s * 1000)}: back "
                         f"may have come too early; raise wait_ms")
    if reason:
        res["focus_reason"] = reason
    if log is not None and log.verbose:
        last = next((e["said"] for e in reversed(said2) if e.get("said")), None)
        if last:
            res["speak_after"] = last
        before = log.before(t0, "tts")
        if before is not None:
            res["speak_before"] = before[2]
    else:
        res["speech"] = ("not logged: TalkBack's log level is not VERBOSE (talkback(action='on', "
                         "verbose_log=true) first)")
    if verdict not in ("restored", "near"):
        same_window = _windows(cur) == _windows(back) and top0 == top2
        why = ("single-activity navigation: the window (root, title, pane titles) did not change, so "
               "TalkBack had no per-window record to restore (a paneTitle per destination does "
               "not change that on TalkBack 17)" if same_window and top1 == top0
               else "the screen came back as a new window/activity instance")
        if reason == "initial":
            why += "; TalkBack placed its initial focus (isInitialFocus): it restored nothing"
        res["finding"] = {"code": "tb.restore_failed", "sev": "warn", "basis": "walk",
                          "msg": f"after back, focus went {verdict} instead of {_ref(f0, legacy)}; {why}",
                          "fix": FIXES["tb.restore_failed"]}
    if post:
        obs, _cur = _observe(drv, back, post, log, legacy)
        res.update(obs)
    return res


def _same_item(before: str, after: str) -> bool:
    """The same item, maybe updated: equal labels, or one extends the other at a
    word boundary ("Track 8" -> "Track 8 (played)", not "Message 1" -> "Message 10")."""
    if before == after:
        return True
    short, long_ = sorted((before, after), key=len)
    return bool(short) and long_.startswith(short) and not long_[len(short)].isalnum()


def _survive(drv: Driver, cur: Snapshot, steps: List[Step], wait_s: float, legacy: bool,
             log: Optional[SpeechLog] = None) -> Dict[str, Any]:
    f0 = cur.focus
    if f0 is None:
        raise WalkError("start_not_found", "survive needs a focused target (pass target)")
    acting = [s for s in steps if s.kind in ACTING]
    last = max(i for i, s in enumerate(steps) if s.kind in ACTING)
    post = [s for s in steps[last + 1:] if s.kind in OBSERVING]
    whats, cur, after, events, t0, _opened = _act_all(drv, cur, acting, wait_s, legacy,
                                                      quiet=False)
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
    what = "; ".join(whats)
    res: Dict[str, Any] = {"mutate": what, "timeline": _merge(events[:12], _said(log, t0)[:8]),
                           "focus": _desc(f2, legacy), "verdict": verdict,
                           "lost_midway": lost_midway, "cause": cause}
    res.update(_speech(log, t0, wait_s, acting[-1].kind, _sig(cur) != _sig(after)))
    code = {"reset_top": "tb.focus_reset", "lost": "tb.focus_lost", "drifted": "tb.focus_drift",
            "moved": "tb.focus_reset"}.get(verdict)
    if code:
        res["finding"] = {"code": code, "sev": "warn", "basis": "walk",
                          "msg": f"after {what}, focus {verdict.replace('_', ' ')}"
                                 + (f" to {_ref(f2, legacy)}" if f2 is not None and verdict != "drifted" else "")
                                 + f" (was {_ref(f0, legacy)}; {cause['removed']} nodes removed, "
                                   f"{cause['added']} added)",
                          "fix": FIXES[code]}
    if post:
        obs, _cur = _observe(drv, after, post, log, legacy)
        res.update(obs)
    return res


__all__ = ["ACTING", "Facts", "GRAMMAR", "KINDS", "SpeechLog", "Step", "classify", "facts",
           "parse_action", "parse_line", "run_scenario", "settle", "timeline"]
