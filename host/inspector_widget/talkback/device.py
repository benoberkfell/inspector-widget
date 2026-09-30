"""Turn TalkBack on and off on a device, and always put the settings back.

TalkBack is DEVICE-WIDE: while it runs, every app on the device gets a screen
reader (Compose computes its traversal maps, AndroidView holders turn
invisible to dumps, touch exploration changes what a tap does), and other
tools or people using the same emulator see that. So every change here is
bracketed by a snapshot:

* :func:`enable` saves ``enabled_accessibility_services``,
  ``accessibility_enabled``, ``touch_exploration_enabled`` and
  ``touch_exploration_granted_accessibility_services`` to a crash-safe state
  file (``<store>/talkback/<serial>.json``) BEFORE it changes anything, then
  APPENDS the TalkBack component to the enabled services (other services such
  as Switch Access or the Accessibility Menu stay enabled).
* :func:`restore` writes the snapshot back exactly (``settings delete`` for a
  value that was unset), waits for the system to settle, checks the result and
  removes the state file only when every value matches.
* A state file that is still there (a crash, a killed process, ``leave_on``)
  makes :func:`status` report ``restore_pending``, and ``talkback restore``
  (CLI or MCP) finishes the job. A later :func:`enable` reuses that snapshot
  rather than saving the TalkBack-on state as "original".
* Snapshots this process created are restored by :func:`restore_owned`, which
  the MCP server runs at exit.

Nothing here uses UiAutomation (``uiautomator dump``/``events``): while
TalkBack runs, a UiAutomation connection suppresses it and clears its focus.
Nothing here runs ``adb root`` either (it restarts adbd and drops every
forward).

The long-lived processes the driver needs (``uinput -`` and ``logcat``) are
started through :func:`popen`, which the offline test harness replaces.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence

from .. import adb

TALKBACK_PACKAGE = "com.google.android.marvin.talkback"
TALKBACK_SERVICE = "com.google.android.marvin.talkback.TalkBackService"
TALKBACK_COMPONENT = f"{TALKBACK_PACKAGE}/{TALKBACK_SERVICE}"

SERVICES = "enabled_accessibility_services"
A11Y_ENABLED = "accessibility_enabled"
TOUCH_EXPLORATION = "touch_exploration_enabled"
TOUCH_GRANTED = "touch_exploration_granted_accessibility_services"
# Written back in this order by restore(): the service list first (TalkBack
# stops), then the switches the system may have flipped while it ran.
SNAPSHOT_KEYS = (SERVICES, A11Y_ENABLED, TOUCH_EXPLORATION, TOUCH_GRANTED)

# Activities TalkBack (or the permission controller, for the Accessibility
# Suite's notification permission) may put on top when it starts.
_DISMISSABLE_PACKAGES = (TALKBACK_PACKAGE, "com.google.android.permissioncontroller",
                         "com.android.permissioncontroller")

DEVICE_WIDE_WARNING = (
    "TalkBack is device-wide: while it is on, every app on this device (and anyone else "
    "using it) gets the screen reader, and a11y dumps differ (Compose traversal maps are "
    "live, AndroidView holders are hidden). Restore with talkback(action='restore') / "
    "`inspector-widget talkback restore`.")

STATE_VERSION = 1
ENABLE_WAIT_S = 5.0     # touch exploration comes on ~0.2-2.5s after the settings change
START_SETTLE_S = 1.0    # TalkBack's own start-up (tutorial, service connection)
RESTORE_WAIT_S = 5.0
DISMISS_WAIT_S = 0.6    # after BACK on TalkBack's tutorial
REFRONT_WAIT_S = 0.8    # after bringing the app back to the front

_FLAG_ACTIVITY_REORDER_TO_FRONT = "0x00020000"


class TalkBackError(RuntimeError):
    """A TalkBack control failure. ``code`` is one of: talkback_unavailable,
    enable_failed, restore_failed, busy, app_left_foreground."""

    def __init__(self, code: str, message: str, hint: Optional[str] = None,
                 detail: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint
        self.detail = detail or {}


# --------------------------------------------------------------------------- #
# Where state lives
# --------------------------------------------------------------------------- #
def store_root() -> str:
    """The inspector-widget store (shared with captures): $INSPECTOR_WIDGET_CAPTURE_DIR,
    else $XDG_CACHE_HOME/inspector-widget, else the platform cache directory."""
    try:  # the capture store's own resolution, once it is merged
        return importlib.import_module("inspector_widget.capture.model").default_store_root()
    except Exception:  # noqa: BLE001 - no capture store on this branch
        pass
    explicit = os.environ.get("INSPECTOR_WIDGET_CAPTURE_DIR")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return os.path.join(os.path.expanduser(xdg), "inspector-widget")
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Caches", "inspector-widget")
    return os.path.join(home, ".cache", "inspector-widget")


def _safe(serial: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", serial)


def state_path(serial: str) -> str:
    return os.path.join(store_root(), "talkback", f"{_safe(serial)}.json")


def _write_json_atomic(path: str, obj: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_snapshot(serial: str) -> Optional[Dict[str, Any]]:
    try:
        with open(state_path(serial)) as f:
            snap = json.load(f)
    except (OSError, ValueError):
        return None
    return snap if isinstance(snap, dict) and isinstance(snap.get("settings"), dict) else None


# Serials whose snapshot this process created (or adopted by changing the
# settings again); restore_owned() puts them back at exit.
_OWNED: set = set()
_OWNED_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Long-lived adb processes (patched by the offline harness)
# --------------------------------------------------------------------------- #
def _popen(argv: Sequence[str]) -> Any:
    return subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)


def popen(serial: str, args: Sequence[str]) -> Any:
    """``adb -s SERIAL <args>`` as a long-lived process with binary pipes."""
    return _popen(["adb", "-s", serial, *args])


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def get_setting(serial: str, key: str) -> Optional[str]:
    """``settings get secure KEY``; ``None`` when unset (Android prints "null")."""
    out = adb.shell(serial, f"settings get secure {key}").strip()
    return None if out in ("", "null") else out


def put_setting(serial: str, key: str, value: Optional[str]) -> None:
    """``settings put secure KEY VALUE``, or ``settings delete`` for ``None``."""
    if value is None:
        adb.shell(serial, f"settings delete secure {key}")
    else:
        adb.shell(serial, f"settings put secure {key} {shlex.quote(value)}")


def read_settings(serial: str) -> Dict[str, Optional[str]]:
    return {k: get_setting(serial, k) for k in SNAPSHOT_KEYS}


def _norm_component(c: str) -> str:
    pkg, _, cls = c.strip().partition("/")
    if cls.startswith("."):
        cls = pkg + cls
    return f"{pkg}/{cls}"


def services_list(value: Optional[str]) -> List[str]:
    return [s for s in (value or "").split(":") if s.strip()]


def talkback_in(value: Optional[str]) -> bool:
    return any(_norm_component(s) == TALKBACK_COMPONENT for s in services_list(value))


def _equivalent(key: str, a: Optional[str], b: Optional[str]) -> bool:
    """Settings equality the system can't disturb: an unset switch reads as 0
    once the system writes it, and an unset list as empty."""
    if key in (A11Y_ENABLED, TOUCH_EXPLORATION):
        return (a or "0") == (b or "0")
    if key in (SERVICES, TOUCH_GRANTED):
        return [_norm_component(s) for s in services_list(a)] == \
            [_norm_component(s) for s in services_list(b)]
    return a == b


def _on(settings: Dict[str, Optional[str]]) -> bool:
    return (talkback_in(settings.get(SERVICES)) and settings.get(A11Y_ENABLED) == "1"
            and settings.get(TOUCH_EXPLORATION) == "1")


# --------------------------------------------------------------------------- #
# Device facts
# --------------------------------------------------------------------------- #
def talkback_version(serial: str) -> Optional[str]:
    """TalkBack's versionName, or ``None`` when it is not installed."""
    listed = adb.shell(serial, f"pm list packages {TALKBACK_PACKAGE}", check=False)
    if f"package:{TALKBACK_PACKAGE}" not in {ln.strip() for ln in listed.splitlines()}:
        return None
    out = adb.shell(serial, f"dumpsys package {TALKBACK_PACKAGE} | grep -m1 versionName",
                    check=False)
    m = re.search(r"versionName=(\S+)", out)
    return m.group(1) if m else "unknown"


def talkback_pid(serial: str) -> Optional[int]:
    try:
        return adb.pidof(serial, TALKBACK_PACKAGE)
    except Exception:  # noqa: BLE001 - diagnostic only
        return None


_RESUMED = re.compile(r"(?:topResumedActivity|mResumedActivity|ResumedActivity)[:=]\s*"
                      r"ActivityRecord\{\S+ u\d+ ([\w.]+)/([\w.$]+)")


def top_activity(serial: str) -> Optional[str]:
    """The resumed activity on top, as ``package/.Class`` (``dumpsys activity``)."""
    out = adb.shell(serial, "dumpsys activity activities | "
                            "grep -E 'topResumedActivity|mResumedActivity'", check=False)
    m = _RESUMED.search(out)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def top_package(serial: str) -> Optional[str]:
    top = top_activity(serial)
    return top.split("/", 1)[0] if top else None


def uinput_available(serial: str) -> bool:
    out = adb.shell(serial, "ls /system/bin/uinput", check=False)
    return "/system/bin/uinput" in out and "No such file" not in out


# Last injector outcome per serial in this process ("ok ...", "failed: ...").
INJECTOR_STATUS: Dict[str, Dict[str, str]] = {}


# --------------------------------------------------------------------------- #
# Locks: one TalkBack driver per device at a time (in-process and across processes)
# --------------------------------------------------------------------------- #
_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


@contextlib.contextmanager
def device_lock(serial: str, what: str = "TalkBack") -> Iterator[None]:
    """Hold the per-serial TalkBack lock, or raise ``TalkBackError('busy')`` at once.

    Two walks on one device would press keys into each other's TalkBack, and
    turning TalkBack off under a walk breaks it, so both take this lock. It is
    a thread lock plus an ``flock`` on ``<store>/talkback/<serial>.lock``, so a
    CLI walk and an MCP walk on the same device exclude each other too.
    """
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(serial, threading.Lock())
    if not lock.acquire(blocking=False):
        raise TalkBackError("busy", f"another {what} run is using TalkBack on {serial} "
                                    f"in this process; wait for it to finish")
    fd = None
    try:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - not POSIX
            fcntl = None
        if fcntl is not None:
            path = os.path.join(store_root(), "talkback", f"{_safe(serial)}.lock")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise TalkBackError("busy", f"another inspector-widget process is driving "
                                            f"TalkBack on {serial}; wait for it to finish") from None
        yield
    finally:
        if fd is not None:
            os.close(fd)  # closing the descriptor releases the flock
        lock.release()


# --------------------------------------------------------------------------- #
# Snapshot / enable / restore
# --------------------------------------------------------------------------- #
def _snapshot(serial: str, settings: Dict[str, Optional[str]]) -> Dict[str, Any]:
    """Save ``settings`` as the state to restore, unless a snapshot is already
    pending (it holds the state from before inspector-widget changed anything)."""
    existing = load_snapshot(serial)
    if existing is not None:
        return existing
    snap = {"version": STATE_VERSION, "serial": serial, "saved_at": time.time(),
            "saved_by_pid": os.getpid(), "settings": dict(settings),
            "talkback_component": TALKBACK_COMPONENT}
    _write_json_atomic(state_path(serial), snap)
    return snap


def _wait(pred, timeout_s: float, interval_s: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if pred():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval_s)


def enable(serial: str, package: Optional[str] = None) -> Dict[str, Any]:
    """Turn TalkBack on (snapshot first; append the component; wait for touch
    exploration; dismiss TalkBack's tutorial). Returns what it did.

    ``changed`` False means TalkBack was already on and nothing was touched.
    With ``package``, checks that app is still on top afterwards and brings it
    back to the front (without recreating it) if TalkBack covered it.
    """
    t0 = time.monotonic()
    before = read_settings(serial)
    version = talkback_version(serial)
    if version is None:
        raise TalkBackError("talkback_unavailable",
                            f"TalkBack ({TALKBACK_PACKAGE}) is not installed on {serial}",
                            hint="Use a Google APIs / Play system image, or install the "
                                 "Android Accessibility Suite.")
    out: Dict[str, Any] = {"serial": serial, "talkback": "on", "version": version}
    if _on(before):
        out.update(changed=False, took_ms=_ms(t0))
        return out
    top_before = top_activity(serial)
    # Crash-safe: the state file is on disk before the first setting changes.
    _snapshot(serial, before)
    with _OWNED_LOCK:
        _OWNED.add(serial)
    services = before.get(SERVICES)
    if not talkback_in(services):
        services = ":".join(services_list(services) + [TALKBACK_COMPONENT])
    try:
        put_setting(serial, SERVICES, services)
        put_setting(serial, A11Y_ENABLED, "1")
        if not _wait(lambda: get_setting(serial, TOUCH_EXPLORATION) == "1", ENABLE_WAIT_S):
            raise TalkBackError("enable_failed",
                                f"TalkBack did not turn on touch exploration within "
                                f"{ENABLE_WAIT_S:.0f}s on {serial}; the settings were restored",
                                hint="Check `adb shell dumpsys accessibility` and that TalkBack "
                                     "is not disabled in Settings > Apps.")
        out["touch_exploration_ms"] = _ms(t0)
        time.sleep(START_SETTLE_S)
        out["dismissed"] = dismiss_talkback_activities(serial, top_before)
        if package:
            out.update(ensure_foreground(serial, package, top_before))
    except BaseException:
        # Whatever failed after the first change, leave the device as it was.
        with contextlib.suppress(Exception):
            restore(serial)
        raise
    out.update(changed=True, took_ms=_ms(t0), saved=state_path(serial),
               warning=DEVICE_WIDE_WARNING)
    return out


def dismiss_talkback_activities(serial: str, top_before: Optional[str] = None,
                                tries: int = 3) -> List[str]:
    """Send BACK while TalkBack's tutorial (or the permission dialog it raises)
    is on top. An injected BACK reaches the activity (it is not a TalkBack key)."""
    dismissed: List[str] = []
    for _ in range(tries):
        top = top_activity(serial)
        if not top or top == top_before or not top.startswith(
                tuple(p + "/" for p in _DISMISSABLE_PACKAGES)):
            break
        adb.shell(serial, "input keyevent KEYCODE_BACK")
        dismissed.append(top)
        time.sleep(DISMISS_WAIT_S)
    return dismissed


def ensure_foreground(serial: str, package: str, top_before: Optional[str] = None) -> Dict[str, Any]:
    """``{}`` when ``package`` is on top; else bring ``top_before`` (its activity
    from before TalkBack started) back to the front and say so."""
    top = top_activity(serial)
    if top and top.startswith(package + "/"):
        return {}
    if top_before and top_before.startswith(package + "/"):
        adb.shell(serial, f"am start -n {shlex.quote(top_before)} "
                          f"-f {_FLAG_ACTIVITY_REORDER_TO_FRONT}", check=False)
        time.sleep(REFRONT_WAIT_S)
        top = top_activity(serial)
        if top and top.startswith(package + "/"):
            return {"refronted": top_before,
                    "warning_foreground": f"{package} was covered; brought {top_before} "
                                          f"back to the front"}
    raise TalkBackError("app_left_foreground", f"{package} is not in the foreground on "
                                               f"{serial} (top: {top or 'unknown'})",
                        hint=f"Open {package} on the device, then retry.")


def restore(serial: str, wait_s: Optional[float] = None) -> Dict[str, Any]:
    """Write the pending snapshot back and verify it; the state file is removed
    only when every value matches. ``restored`` False with ``reason`` when there
    is nothing to restore; raises ``TalkBackError('restore_failed')`` when the
    device keeps a different value (the state file stays for a retry)."""
    wait_s = RESTORE_WAIT_S if wait_s is None else wait_s
    snap = load_snapshot(serial)
    if snap is None:
        return {"serial": serial, "restored": False, "reason": "nothing to restore"}
    want: Dict[str, Optional[str]] = {k: snap["settings"].get(k) for k in SNAPSHOT_KEYS}

    def apply() -> None:
        for k in SNAPSHOT_KEYS:
            if not _equivalent(k, get_setting(serial, k), want[k]):
                put_setting(serial, k, want[k])

    t0 = time.monotonic()
    for k in SNAPSHOT_KEYS:
        put_setting(serial, k, want[k])
    # The system rewrites touch_exploration_enabled as TalkBack unbinds; settle
    # and re-apply once if it raced us.
    ok = _wait(lambda: all(_equivalent(k, v, want[k])
                           for k, v in read_settings(serial).items()), wait_s / 2, 0.2)
    if not ok:
        apply()
        ok = _wait(lambda: all(_equivalent(k, v, want[k])
                               for k, v in read_settings(serial).items()), wait_s / 2, 0.2)
    now = read_settings(serial)
    mismatch = {k: {"want": want[k], "have": now[k]} for k in SNAPSHOT_KEYS
                if not _equivalent(k, now[k], want[k])}
    if mismatch:
        raise TalkBackError("restore_failed",
                            f"could not restore the accessibility settings on {serial}: "
                            + ", ".join(f"{k}={v['have']!r} (want {v['want']!r})"
                                        for k, v in mismatch.items()),
                            hint="Run talkback restore again; the snapshot is kept in "
                                 + state_path(serial), detail={"mismatch": mismatch})
    try:
        os.remove(state_path(serial))
    except OSError:
        pass
    with _OWNED_LOCK:
        _OWNED.discard(serial)
    return {"serial": serial, "restored": True, "settings": now, "took_ms": _ms(t0),
            "talkback": "on" if _on(now) else "off"}


def disable(serial: str) -> Dict[str, Any]:
    """Turn TalkBack off. When the pending snapshot had it off, that is an exact
    restore. Otherwise (no snapshot, or TalkBack was on before we started) only
    the TalkBack component is removed, after snapshotting, so ``restore`` can
    bring the user's own setup back."""
    snap = load_snapshot(serial)
    if snap is not None and not talkback_in(snap["settings"].get(SERVICES)):
        out = restore(serial)
        out["talkback"] = "off"
        return out
    now = read_settings(serial)
    if not talkback_in(now.get(SERVICES)):
        return {"serial": serial, "talkback": "off", "changed": False,
                "restore_pending": snap is not None}
    _snapshot(serial, now)
    with _OWNED_LOCK:
        _OWNED.add(serial)
    rest = [s for s in services_list(now.get(SERVICES)) if _norm_component(s) != TALKBACK_COMPONENT]
    put_setting(serial, SERVICES, ":".join(rest) if rest else None)
    if not rest:
        put_setting(serial, A11Y_ENABLED, "0")
    _wait(lambda: get_setting(serial, TOUCH_EXPLORATION) != "1", ENABLE_WAIT_S)
    return {"serial": serial, "talkback": "off", "changed": True, "restore_pending": True,
            "saved": state_path(serial),
            "note": "TalkBack was on before inspector-widget changed it; talkback restore "
                    "turns it back on"}


def restore_owned() -> List[Dict[str, Any]]:
    """Restore every snapshot this process created (for MCP exit). Best-effort:
    a failure leaves the state file for ``talkback restore``."""
    with _OWNED_LOCK:
        serials = sorted(_OWNED)
    results = []
    for serial in serials:
        try:
            results.append(restore(serial))
        except Exception as exc:  # noqa: BLE001 - exit cleanup must not raise
            results.append({"serial": serial, "restored": False, "error": str(exc)})
    return results


def status(serial: str) -> Dict[str, Any]:
    """Read-only: TalkBack install/enabled state, settings, pending restore, injectors."""
    settings = read_settings(serial)
    version = talkback_version(serial)
    snap = load_snapshot(serial)
    enabled = talkback_in(settings.get(SERVICES)) and settings.get(A11Y_ENABLED) == "1"
    out: Dict[str, Any] = {
        "serial": serial,
        "talkback": {"installed": version, "enabled": enabled,
                     "running": talkback_pid(serial) is not None if version else False},
        "accessibility_enabled": settings.get(A11Y_ENABLED) == "1",
        "touch_exploration": settings.get(TOUCH_EXPLORATION) == "1",
        "services": services_list(settings.get(SERVICES)),
        "restore_pending": snap is not None,
        "top": top_activity(serial),
        "injectors": dict(INJECTOR_STATUS.get(serial) or {}),
    }
    out["injectors"].setdefault("uinput", "available (untested)" if uinput_available(serial)
                                else "missing")
    if snap is not None:
        out["saved"] = {"path": state_path(serial), "at": snap.get("saved_at"),
                        "by_pid": snap.get("saved_by_pid"), "settings": snap.get("settings")}
    if enabled:
        out["warning"] = DEVICE_WIDE_WARNING
    return out


ACTIONS = ("status", "on", "off", "restore")


def action(serial: str, what: str, package: Optional[str] = None) -> Dict[str, Any]:
    """The ``talkback`` tool / subcommand: status | on | off | restore."""
    if what == "status":
        return status(serial)
    if what not in ACTIONS:
        raise ValueError(f"unknown talkback action {what!r}; expected one of {', '.join(ACTIONS)}")
    with device_lock(serial, f"talkback {what}"):
        if what == "on":
            out = enable(serial, package)
        elif what == "off":
            out = disable(serial)
        else:
            out = restore(serial)
    out["restore_pending"] = load_snapshot(serial) is not None
    return out


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)
