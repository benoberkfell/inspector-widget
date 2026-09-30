"""Real input that reaches TalkBack: a uinput keyboard (default) or touchscreen.

``adb shell input keyevent|keycombination|swipe`` never reaches TalkBack:
injected events skip the accessibility input filter, and ``input
keycombination META_LEFT DPAD_LEFT`` hits the system shortcut (BACK). A uinput
device produces real kernel events, which go through InputReader, the
accessibility input filter and TalkBack's ``onKeyEvent`` / gesture detector.

The keyboard is one long-lived ``adb -s SERIAL shell uinput -`` fed JSON
commands on stdin. It declares only modifier / arrow / Space / Enter / H / F /
R keys: without KEY_Q Android does not classify it as alphabetic, so the
hardKeyboard configuration does not change and the IME stays. A press is
``updateTimeBase`` + key-down/up events + a ``sync`` whose token uinput echoes
on stdout once the events are delivered.

SAFETY (TalkBack KeyComboManager; enforced by :class:`KeyGuard`):

* never send a modifier on its own: a lone Meta tap opens All apps 300ms later;
* never send any Meta combo except the probe (Meta+Right, "next") until the
  probe has been SEEN to move accessibility focus: an unconsumed Meta+Left is
  system BACK, and an unconsumed Meta+Ctrl+Left/Right is split-screen
  navigation. The caller proves the keymap with :meth:`Injector.mark_proven`.

The touchscreen fallback (single-finger swipes right/left = next/previous,
double tap = activate) is timing-sensitive but is exactly the user's path.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .. import adb
from . import device

KEYBOARD_ID = 1
TOUCH_ID = 2
KEYBOARD_NAME = "InspectorWidget TalkBack Keys"
TOUCH_NAME = "InspectorWidget TalkBack Touch"
VID = 0x18D1
KEYBOARD_PID = 0x4E49
TOUCH_PID = 0x4E4A

KEYS = (
    "KEY_LEFTMETA", "KEY_LEFTCTRL", "KEY_LEFTSHIFT", "KEY_LEFTALT",
    "KEY_LEFT", "KEY_RIGHT", "KEY_UP", "KEY_DOWN",
    "KEY_SPACE", "KEY_ENTER", "KEY_H", "KEY_F", "KEY_R",
)
MODIFIERS = frozenset({"KEY_LEFTMETA", "KEY_LEFTCTRL", "KEY_LEFTSHIFT", "KEY_LEFTALT"})
_ALIASES = {"META": "KEY_LEFTMETA", "SEARCH": "KEY_LEFTMETA", "CTRL": "KEY_LEFTCTRL",
            "CONTROL": "KEY_LEFTCTRL", "SHIFT": "KEY_LEFTSHIFT", "ALT": "KEY_LEFTALT"}

Combo = Tuple[Tuple[str, ...], str]

# TalkBack 16.2+ enhanced keymap (the TalkBack key is Meta); 17.0 default.
ENHANCED: Dict[str, Combo] = {
    "next": (("KEY_LEFTMETA",), "KEY_RIGHT"),
    "prev": (("KEY_LEFTMETA",), "KEY_LEFT"),
    "first": (("KEY_LEFTMETA", "KEY_LEFTCTRL"), "KEY_LEFT"),
    "last": (("KEY_LEFTMETA", "KEY_LEFTCTRL"), "KEY_RIGHT"),
    "click": (("KEY_LEFTMETA",), "KEY_SPACE"),
}
# The classic keymap of TalkBack before 16.1 (Alt is the modifier).
CLASSIC: Dict[str, Combo] = {
    "next": (("KEY_LEFTALT",), "KEY_RIGHT"),
    "prev": (("KEY_LEFTALT",), "KEY_LEFT"),
}
KEYMAPS = {"enhanced": ENHANCED, "classic": CLASSIC}
PROBE_ACTION = "next"
# The only modifier combos allowed before TalkBack has been seen to consume one:
# unconsumed, Meta+Right and Alt+Right just move input focus in the app.
PROBES = frozenset({ENHANCED[PROBE_ACTION], CLASSIC[PROBE_ACTION]})

HOLD_MS = 30      # key down -> up
GAP_MS = 15       # between modifier and key transitions
REGISTER_WAIT_S = 5.0
SYNC_TIMEOUT_S = 3.0


class InjectorError(RuntimeError):
    """No injector could be opened, or one stopped working (``code`` injector_failed)."""

    code = "injector_failed"

    def __init__(self, message: str, tried: Optional[List[str]] = None,
                 hint: Optional[str] = None) -> None:
        super().__init__(message)
        self.tried = tried or []
        self.hint = hint


class UnsafeKeyError(ValueError):
    """A key press the TalkBack safety rules forbid (see the module docstring)."""


def parse_combo(spec: str) -> Combo:
    """``"META+SPACE"`` / ``"KEY_LEFTMETA+KEY_H"`` / ``"ENTER"`` -> (mods, key)."""
    parts = [p.strip().upper() for p in re.split(r"[+\s]+", spec or "") if p.strip()]
    if not parts:
        raise ValueError("empty key combo")
    names = []
    for p in parts:
        name = _ALIASES.get(p) or (p if p.startswith("KEY_") else f"KEY_{p}")
        if name not in KEYS:
            raise ValueError(f"key {p!r} is not on the TalkBack keyboard (have: "
                             + ", ".join(k[4:] for k in KEYS) + ")")
        names.append(name)
    *mods, key = names
    return tuple(mods), key


class KeyGuard:
    """Refuses the presses that can do damage when TalkBack does not consume them."""

    def __init__(self, keymap: str = "enhanced") -> None:
        self.keymap = keymap
        self.proven = False

    def check(self, mods: Sequence[str], key: str, action: Optional[str] = None) -> None:
        if key in MODIFIERS:
            raise UnsafeKeyError(f"refusing to send {key} on its own: a lone Meta opens "
                                 f"All apps, and a modifier must accompany a key")
        if not mods or self.proven or (tuple(mods), key) in PROBES:
            return
        raise UnsafeKeyError(
            f"refusing {'+'.join(list(mods) + [key])} before TalkBack has been seen to "
            f"consume the probe (next) combo: unconsumed, Meta+Left is system BACK and "
            f"Meta+Ctrl+Left/Right switches split-screen apps")


class Injector:
    """Base: ``press(action)`` for next/prev/first/last/click (+ ``combo`` on keyboards)."""

    kind = "none"
    actions: Tuple[str, ...] = ()

    def __init__(self, serial: str, keymap: str = "enhanced") -> None:
        self.serial = serial
        self.guard = KeyGuard(keymap)
        self.registered_ms: Optional[int] = None
        self.presses: List[Tuple[str, int]] = []  # (action, send+ack ms)

    @property
    def keymap(self) -> str:
        return self.guard.keymap

    @keymap.setter
    def keymap(self, value: str) -> None:
        self.guard.keymap = value

    @property
    def proven(self) -> bool:
        return self.guard.proven

    def mark_proven(self) -> None:
        """TalkBack consumed the probe combo (focus moved): the rest are safe now."""
        self.guard.proven = True

    def open(self) -> "Injector":
        return self

    def press(self, action: str) -> int:
        raise NotImplementedError

    def combo(self, spec: str) -> int:
        raise InjectorError(f"the {self.kind} injector cannot send key combos")

    def describe(self) -> str:
        return f"{self.kind}/{self.keymap}"

    def close(self) -> None:
        pass

    def __enter__(self) -> "Injector":
        return self.open()

    def __exit__(self, *exc: Any) -> None:
        self.close()


class _UinputProcess:
    """One ``uinput -`` process: JSON commands in, sync acks out."""

    def __init__(self, serial: str, dev_id: int, name: str) -> None:
        self.serial = serial
        self.dev_id = dev_id
        self.name = name
        self.proc: Any = None
        self._q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._n = 0
        self._lock = threading.Lock()

    def start(self, register: Dict[str, Any], wait_s: float = REGISTER_WAIT_S) -> int:
        t0 = time.monotonic()
        try:
            self.proc = device.popen(self.serial, ["shell", "uinput", "-"])
        except OSError as exc:
            raise InjectorError(f"could not start adb for uinput: {exc}") from exc
        threading.Thread(target=self._reader, name=f"uinput-{self.dev_id}", daemon=True).start()
        self.send([register])
        # uinput does not wait for InputReader to add the device: poll for it.
        needle = self.name.replace("'", "")
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise InjectorError(f"uinput exited (code {self.proc.poll()}): {self._stderr()}")
            out = adb.shell(self.serial, f"dumpsys input | grep -F -m1 '{needle}'", check=False)
            if self.name in out:
                return int((time.monotonic() - t0) * 1000)
            time.sleep(0.05)
        raise InjectorError(f"the uinput device {self.name!r} did not appear in dumpsys input "
                            f"within {wait_s:.0f}s: {self._stderr()}")

    def _stderr(self) -> str:
        err = getattr(self.proc, "stderr", None)
        if err is None or self.proc.poll() is None:
            return "(no error output)"
        try:
            return (err.read() or b"").decode("utf-8", "replace").strip()[-300:] or "(no error output)"
        except Exception:  # noqa: BLE001 - diagnostic only
            return "(no error output)"

    def _reader(self) -> None:
        buf = ""
        dec = json.JSONDecoder()
        out = self.proc.stdout
        read = getattr(out, "read1", None) or out.read
        while True:
            try:
                raw = read(4096)
            except (OSError, ValueError):
                return
            if not raw:
                return
            buf += raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            while True:
                s = buf.lstrip()
                if not s:
                    buf = ""
                    break
                try:
                    obj, end = dec.raw_decode(s)
                except ValueError:
                    buf = s
                    break
                if isinstance(obj, dict):
                    self._q.put(obj)
                buf = s[end:]

    def send(self, cmds: Iterable[Dict[str, Any]]) -> None:
        """Write every command in one go, so a combo is never half-sent."""
        data = "".join(json.dumps(c) + "\n" for c in cmds).encode()
        with self._lock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise InjectorError(f"uinput is gone: {exc}") from exc

    def sync(self, timeout: float = SYNC_TIMEOUT_S) -> None:
        self._n += 1
        tok = f"s{self._n}"
        self.send([{"id": self.dev_id, "command": "sync", "syncToken": tok}])
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                obj = self._q.get(timeout=left)
            except queue.Empty:
                break
            if obj.get("syncToken") == tok:
                return
        raise InjectorError(f"uinput did not acknowledge sync {tok} within {timeout:.0f}s")

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()  # uinput exits and the device is removed
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass


def _inject(dev_id: int, events: List[Any]) -> Dict[str, Any]:
    return {"id": dev_id, "command": "inject", "events": events}


def _delay(dev_id: int, ms: int) -> Dict[str, Any]:
    return {"id": dev_id, "command": "delay", "duration": int(ms)}


_SYN = ["EV_SYN", "SYN_REPORT", 0]


class UinputKeyboard(Injector):
    """The default injector: TalkBack keyboard shortcuts from a virtual keyboard."""

    kind = "uinput"
    actions = ("next", "prev", "first", "last", "click")

    def __init__(self, serial: str, keymap: str = "enhanced",
                 hold_ms: int = HOLD_MS, gap_ms: int = GAP_MS) -> None:
        super().__init__(serial, keymap)
        self.hold_ms = hold_ms
        self.gap_ms = gap_ms
        self._dev = _UinputProcess(serial, KEYBOARD_ID, KEYBOARD_NAME)

    def open(self) -> "UinputKeyboard":
        self.registered_ms = self._dev.start({
            "id": KEYBOARD_ID, "command": "register", "name": KEYBOARD_NAME,
            "vid": VID, "pid": KEYBOARD_PID, "bus": "usb",
            "configuration": [
                {"type": "UI_SET_EVBIT", "data": ["EV_KEY"]},
                {"type": "UI_SET_KEYBIT", "data": list(KEYS)},
            ],
        })
        return self

    def _send_combo(self, mods: Sequence[str], key: str, action: Optional[str]) -> int:
        self.guard.check(mods, key, action)
        t0 = time.monotonic()
        d = KEYBOARD_ID
        cmds: List[Dict[str, Any]] = [{"id": d, "command": "updateTimeBase"}]
        for m in mods:
            cmds += [_inject(d, ["EV_KEY", m, 1] + _SYN), _delay(d, self.gap_ms)]
        cmds += [_inject(d, ["EV_KEY", key, 1] + _SYN), _delay(d, self.hold_ms),
                 _inject(d, ["EV_KEY", key, 0] + _SYN), _delay(d, self.gap_ms)]
        for m in reversed(mods):
            cmds += [_inject(d, ["EV_KEY", m, 0] + _SYN), _delay(d, self.gap_ms)]
        self._dev.send(cmds)
        self._dev.sync()
        ms = int((time.monotonic() - t0) * 1000)
        self.presses.append((action or "+".join(list(mods) + [key]), ms))
        return ms

    def press(self, action: str) -> int:
        table = KEYMAPS.get(self.keymap, ENHANCED)
        if action not in table:
            raise InjectorError(f"the {self.keymap} keymap has no {action!r} shortcut")
        mods, key = table[action]
        return self._send_combo(mods, key, action)

    def combo(self, spec: str) -> int:
        mods, key = parse_combo(spec)
        return self._send_combo(mods, key, None)

    def close(self) -> None:
        self._dev.close()


class UinputTouch(Injector):
    """Fallback: TalkBack gestures from a virtual touchscreen (swipe right/left =
    next/previous, double tap = activate). 500px over 150ms in 16 samples."""

    kind = "touch"
    actions = ("next", "prev", "click")

    def __init__(self, serial: str, keymap: str = "gestures") -> None:
        super().__init__(serial, keymap)
        self.guard.proven = True  # gestures have no BACK/lone-Meta hazard
        self._dev = _UinputProcess(serial, TOUCH_ID, TOUCH_NAME)
        size = display_size(serial)
        self.w = size[0]
        self.h = size[1]
        self._tid = 100

    def open(self) -> "UinputTouch":
        def ax(code: str, mx: int) -> Dict[str, Any]:
            return {"code": code, "info": {"value": 0, "minimum": 0, "maximum": mx,
                                           "fuzz": 0, "flat": 0, "resolution": 0}}
        self.registered_ms = self._dev.start({
            "id": TOUCH_ID, "command": "register", "name": TOUCH_NAME,
            "vid": VID, "pid": TOUCH_PID, "bus": "usb",
            "configuration": [
                {"type": "UI_SET_EVBIT", "data": ["EV_KEY", "EV_ABS"]},
                {"type": "UI_SET_KEYBIT", "data": ["BTN_TOUCH", "BTN_TOOL_FINGER"]},
                {"type": "UI_SET_ABSBIT", "data": ["ABS_MT_SLOT", "ABS_MT_TRACKING_ID",
                                                   "ABS_MT_POSITION_X", "ABS_MT_POSITION_Y",
                                                   "ABS_MT_TOUCH_MAJOR", "ABS_MT_PRESSURE"]},
                {"type": "UI_SET_PROPBIT", "data": ["INPUT_PROP_DIRECT"]},
            ],
            "abs_info": [
                ax("ABS_MT_SLOT", 9), ax("ABS_MT_TRACKING_ID", 65535),
                ax("ABS_MT_POSITION_X", self.w - 1), ax("ABS_MT_POSITION_Y", self.h - 1),
                ax("ABS_MT_TOUCH_MAJOR", 255), ax("ABS_MT_PRESSURE", 255),
            ],
        })
        return self

    def _stroke(self, points: List[Tuple[int, int]], dur_ms: int) -> List[Dict[str, Any]]:
        self._tid += 1
        d = TOUCH_ID
        x0, y0 = points[0]
        cmds = [_inject(d, ["EV_ABS", "ABS_MT_SLOT", 0, "EV_ABS", "ABS_MT_TRACKING_ID", self._tid,
                            "EV_ABS", "ABS_MT_POSITION_X", x0, "EV_ABS", "ABS_MT_POSITION_Y", y0,
                            "EV_ABS", "ABS_MT_TOUCH_MAJOR", 10, "EV_ABS", "ABS_MT_PRESSURE", 60,
                            "EV_KEY", "BTN_TOUCH", 1, "EV_KEY", "BTN_TOOL_FINGER", 1] + _SYN)]
        step = dur_ms / max(1, len(points) - 1)
        for x, y in points[1:]:
            cmds += [_delay(d, round(step)),
                     _inject(d, ["EV_ABS", "ABS_MT_POSITION_X", x, "EV_ABS", "ABS_MT_POSITION_Y", y] + _SYN)]
        cmds += [_delay(d, 5), _inject(d, ["EV_ABS", "ABS_MT_TRACKING_ID", -1, "EV_KEY", "BTN_TOUCH", 0,
                                           "EV_KEY", "BTN_TOOL_FINGER", 0] + _SYN)]
        return cmds

    def swipe(self, x0: int, y0: int, x1: int, y1: int, dur_ms: int = 150, n: int = 16) -> int:
        t0 = time.monotonic()
        pts = [(round(x0 + (x1 - x0) * i / n), round(y0 + (y1 - y0) * i / n)) for i in range(n + 1)]
        self._dev.send([{"id": TOUCH_ID, "command": "updateTimeBase"}] + self._stroke(pts, dur_ms))
        self._dev.sync()
        return int((time.monotonic() - t0) * 1000)

    def press(self, action: str) -> int:
        cx, y, dist = self.w // 2, self.h // 2, min(500, self.w * 3 // 5)
        t0 = time.monotonic()
        if action == "next":
            self.swipe(cx - dist // 2, y, cx + dist // 2, y)
        elif action == "prev":
            self.swipe(cx + dist // 2, y, cx - dist // 2, y)
        elif action == "click":
            tap = self._stroke([(cx, y), (cx, y)], 40)
            self._dev.send([{"id": TOUCH_ID, "command": "updateTimeBase"}] + tap
                           + [_delay(TOUCH_ID, 90)] + self._stroke([(cx, y), (cx, y)], 40))
            self._dev.sync()
        else:
            raise InjectorError(f"the touch injector has no {action!r} gesture")
        ms = int((time.monotonic() - t0) * 1000)
        self.presses.append((action, ms))
        return ms

    def close(self) -> None:
        self._dev.close()


def display_size(serial: str) -> Tuple[int, int]:
    """``wm size`` (the override when set)."""
    out = adb.shell(serial, "wm size", check=False)
    sizes = dict(re.findall(r"(Physical|Override) size:\s*(\d+x\d+)", out))
    raw = sizes.get("Override") or sizes.get("Physical") or "1080x2400"
    w, h = raw.split("x")
    return int(w), int(h)


INJECTORS = ("auto", "uinput", "touch")


def open_injector(serial: str, kind: str = "auto", keymap: str = "enhanced") -> Injector:
    """Open the requested injector; ``auto`` = the uinput keyboard, else the touchscreen.

    Raises :class:`InjectorError` (``tried`` lists what failed and why).
    """
    if kind not in INJECTORS:
        raise ValueError(f"unknown injector {kind!r}; expected one of {', '.join(INJECTORS)}")
    order = ["uinput", "touch"] if kind == "auto" else [kind]
    tried: List[str] = []
    status = device.INJECTOR_STATUS.setdefault(serial, {})
    for name in order:
        injector: Injector = UinputKeyboard(serial, keymap) if name == "uinput" else UinputTouch(serial)
        try:
            injector.open()
        except Exception as exc:  # noqa: BLE001 - try the next injector
            injector.close()
            tried.append(f"{name}: {exc}")
            status[name] = f"failed: {exc}"
            continue
        status[name] = f"ok (registered in {injector.registered_ms}ms)"
        return injector
    raise InjectorError("no injector could reach TalkBack on " + serial, tried=tried,
                        hint="uinput needs /system/bin/uinput and the shell user's uhid "
                             "group (user builds have both on API 30+).")
