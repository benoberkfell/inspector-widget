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

TalkBack's log level (Developer settings > Log output level; VERBOSE puts the
exact announcements in logcat) is not an adb-settable preference on a user
build. :func:`set_log_level` changes it through TalkBack's own settings screen,
finding the rows with ``uiautomator dump``, which is ONLY safe while TalkBack
is off (a UiAutomation connection suppresses TalkBack and clears its focus), so
it refuses to run while TalkBack is on. The previous level goes into the same
snapshot, before the change, and :func:`restore` puts it back after TalkBack is
off again. Nothing here runs ``adb root`` (it restarts adbd and drops every
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
DISMISS_WAIT_S = 0.6    # after BACK on TalkBack's tutorial / permission dialog
DISMISS_POLL_S = 0.4
DISMISS_WINDOW_S = 3.0  # the permission dialog can come a second or two after start
REFRONT_WAIT_S = 0.8    # after bringing the app back to the front
UI_WAIT_S = 0.8         # after opening/tapping TalkBack's settings screens

TB_PREFS_ACTIVITY = f"{TALKBACK_PACKAGE}/com.android.talkback.TalkBackPreferencesActivity"
TB_DEV_FRAGMENT = "com.google.android.accessibility.talkback.preference.base.DeveloperPrefFragment"
LOG_LEVELS = ("NONE", "ASSERT", "ERROR", "WARN", "INFO", "DEBUG", "VERBOSE")
_UI_DUMP = "/sdcard/iw_tb_ui.xml"

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


def _update_snapshot(serial: str, **fields: Any) -> None:
    snap = load_snapshot(serial)
    if snap is not None:
        snap.update(fields)
        _write_json_atomic(state_path(serial), snap)


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
# When TalkBack started, relative to the app (G1), and a TalkBack-first relaunch
# --------------------------------------------------------------------------- #
#: ``/proc/<pid>/stat`` starttime is in clock ticks (USER_HZ, 100 on Android).
CLK_TCK = 100
#: talkback_started: TalkBack was on before the app's process started (what a TalkBack
#: user gets: RecyclerView rows carry "N of M", WebViews build their tree), after it
#: (the walk turned it on: rows bound before it have no item info), or already on with
#: no record of when.
STARTED = ("before_app", "after_app", "before_walk")
LAUNCH_WAIT_S = 15.0    # am start -W, then the app in front with a process
LAUNCH_POLL_S = 0.25
LAUNCHER = ("-a android.intent.action.MAIN -c android.intent.category.LAUNCHER")


def uptime(serial: str) -> Optional[float]:
    """The device's uptime in seconds (``/proc/uptime``), the clock /proc/<pid>/stat uses."""
    out = adb.shell(serial, "cat /proc/uptime", check=False)
    try:
        return float(out.split()[0])
    except (IndexError, ValueError):
        return None


def process_start(serial: str, pid: Optional[int]) -> Optional[float]:
    """When process ``pid`` started, in device uptime seconds (``/proc/<pid>/stat`` field
    22, starttime, after the parenthesised command name, which may hold spaces)."""
    if not pid:
        return None
    out = adb.shell(serial, f"cat /proc/{int(pid)}/stat", check=False)
    if ")" not in out:
        return None
    fields = out.rsplit(")", 1)[1].split()  # field 3 (state) onwards
    try:
        return int(fields[19]) / CLK_TCK
    except (IndexError, ValueError):
        return None


def verbose_in(snap: Optional[Dict[str, Any]]) -> bool:
    """Whether a pending snapshot says TalkBack's log level is VERBOSE now: set by
    inspector-widget when it turned TalkBack on, or VERBOSE already before that."""
    if not snap:
        return False
    if snap.get("log_level_now"):
        return snap["log_level_now"] == "VERBOSE"
    return snap.get("log_level") == "VERBOSE"


def talkback_started(serial: str, package: str, on_uptime: Optional[float],
                     pid: Optional[int] = None) -> Dict[str, Any]:
    """``{"talkback_started": before_app | after_app | before_walk, "app_start_s",
    "talkback_on_s"}``: the app's process start (``/proc/<pid>/stat``) against the uptime
    at which TalkBack came on (``on_uptime``: :func:`enable`'s, or its snapshot's). Unknown
    when TalkBack was turned on (by someone else) is ``before_walk``."""
    if pid is None:
        try:
            pid = adb.pidof(serial, package)
        except Exception:  # noqa: BLE001 - diagnostic only
            pid = None
    app = process_start(serial, pid)
    out: Dict[str, Any] = {"talkback_started": "before_walk", "app_start_s": app,
                           "talkback_on_s": on_uptime}
    if on_uptime is None or app is None:
        return out
    now = uptime(serial)
    if now is not None and on_uptime > now + 1:
        return out  # a snapshot from before the device rebooted
    out["talkback_started"] = "before_app" if on_uptime <= app else "after_app"
    return out


def launcher_activity(serial: str, package: str) -> Optional[str]:
    """``package``'s launcher component as the package manager resolves it
    (``cmd package resolve-activity --brief``): alias-safe, so an app whose launcher entry
    is an activity-alias (Thunderbird) starts with ``am start -n`` like any other."""
    out = adb.shell(serial, f"cmd package resolve-activity --brief {LAUNCHER} "
                            f"{shlex.quote(package)}", check=False)
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith(package + "/"):
            return line
    return None


def relaunch(serial: str, package: str, wait_s: Optional[float] = None) -> Dict[str, Any]:
    """Restart ``package`` from its launcher, the way a TalkBack user opens it with TalkBack
    already running: ``am force-stop``, then ``am start -W`` with the launcher intent
    (``-a MAIN -c LAUNCHER -p <package>``: alias-safe, where ``am start -n`` with an
    activity-alias's name fails, as Thunderbird's does), else with the component the
    package manager resolves (:func:`launcher_activity`); then wait until the app is in
    front with a live process. Returns ``{"activity", "launcher", "pid", "started_at"
    (device uptime s), "took_ms"}``; raises ``TalkBackError('app_left_foreground')`` when
    it does not come up. The caller turns TalkBack on first and re-attaches afterwards
    (the old process and its agent are gone)."""
    wait_s = LAUNCH_WAIT_S if wait_s is None else wait_s
    t0 = time.monotonic()
    adb.shell(serial, f"am force-stop {shlex.quote(package)}", check=False)
    comp = launcher_activity(serial, package)
    out = adb.shell(serial, f"am start -W {LAUNCHER} -p {shlex.quote(package)}", check=False)
    if _start_error(out) and comp is not None:
        out = adb.shell(serial, f"am start -W -n {shlex.quote(comp)}", check=False)
    err = _start_error(out)
    if err:
        raise TalkBackError("app_left_foreground",
                            f"{package} could not be started on {serial}: {err}",
                            hint=f"Check {package} is installed and has a launcher activity "
                                 f"(cmd package resolve-activity), then retry.")
    m = re.search(r"^Activity:\s*(\S+)", out, re.M)
    activity = m.group(1) if m else None
    deadline = time.monotonic() + wait_s
    pid: Optional[int] = None
    while True:
        try:
            pid = adb.pidof(serial, package)
        except Exception:  # noqa: BLE001 - polled again
            pid = None
        top = top_package(serial)
        if pid and top == package:
            break
        if time.monotonic() >= deadline:
            raise TalkBackError(
                "app_left_foreground",
                f"{package} did not come to the front after a relaunch on {serial} "
                f"(top: {top_activity(serial) or 'unknown'})",
                hint=f"Dismiss what covers it (BACK), or start {package} by hand "
                     f"(am start {LAUNCHER} -p {package}), then walk without relaunch.")
        time.sleep(LAUNCH_POLL_S)
    return {"activity": activity or top_activity(serial), "launcher": comp, "pid": pid,
            "started_at": process_start(serial, pid), "took_ms": _ms(t0)}


def _start_error(out: str) -> Optional[str]:
    """``am start``'s failure line ("Error: Activity class {...} does not exist."), if any."""
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Error:") or line.startswith("Error type"):
            nxt = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("Error:")]
            return (nxt or [line])[0]
    return None


_A11Y_FOCUSED_WINDOW = re.compile(r"Accessibility Focused Window Id\s*=\s*(-?\d+)")
_A11Y_WINDOW = re.compile(r"AccessibilityWindowInfo\[title=(.*?), displayId=-?\d+, id=(-?\d+),"
                          r".*?type=(TYPE_\w+)")


def a11y_focus_window(serial: str) -> Optional[Dict[str, Any]]:
    """The window that holds accessibility focus, from ``dumpsys accessibility``:
    ``{"id", "type" (TYPE_APPLICATION, TYPE_INPUT_METHOD ...), "title"}``, or None when no
    window does (or the dump does not say)."""
    out = adb.shell(serial, "dumpsys accessibility", check=False)
    m = _A11Y_FOCUSED_WINDOW.search(out)
    if m is None or int(m.group(1)) < 0:
        return None
    wid = int(m.group(1))
    for w in _A11Y_WINDOW.finditer(out):
        if int(w.group(2)) == wid:
            title = w.group(1)
            return {"id": wid, "type": w.group(3), "title": None if title == "null" else title}
    return {"id": wid, "type": None, "title": None}


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


def enable(serial: str, package: Optional[str] = None,
           verbose_log: bool = False, verbose_required: bool = True) -> Dict[str, Any]:
    """Turn TalkBack on (snapshot first; append the component; wait for touch
    exploration; dismiss TalkBack's tutorial and permission dialog). Returns
    what it did.

    ``changed`` False means TalkBack was already on and nothing was touched.
    With ``package``, checks that app is still on top afterwards and brings it
    back to the front (without recreating it) if TalkBack covered it. With
    ``verbose_log``, TalkBack's log level is set to VERBOSE first (TalkBack
    reads it when it binds); that needs TalkBack off, so an already-running
    TalkBack keeps its level (``log_level`` says so: ``{"kept": "VERBOSE"}`` when
    the pending snapshot shows it was set, else ``{"unchanged": why}``). A level
    that cannot be set fails the call when ``verbose_required``, else TalkBack
    comes on without it (``log_level`` ``{"failed": why}``).

    ``on_uptime`` is the device uptime (s) at which TalkBack came on (this call, or
    the pending snapshot's for a TalkBack that was already on; None when unknown):
    :func:`talkback_started` compares it with the app's process start.
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
        snap = load_snapshot(serial) or {}
        out["on_uptime"] = snap.get("on_uptime")
        if verbose_log:
            out["log_level"] = ({"kept": "VERBOSE"} if verbose_in(snap) else
                                {"unchanged": "TalkBack was already on (the level can only "
                                              "change while it is off)"})
        out.update(changed=False, took_ms=_ms(t0))
        return out
    top_before = top_activity(serial)
    # Crash-safe: the state file is on disk before the first setting changes.
    _snapshot(serial, before)
    with _OWNED_LOCK:
        _OWNED.add(serial)
    if verbose_log and not talkback_in(before.get(SERVICES)):
        try:
            out["log_level"] = set_log_level(serial, "VERBOSE", record=True)
        except Exception as exc:
            if verbose_required:
                with contextlib.suppress(Exception):
                    restore(serial)
                raise
            # best effort (utterance auto): TalkBack still comes on, its words from the model
            out["log_level"] = {"failed": f"{type(exc).__name__}: {exc}"[:120]}
            with contextlib.suppress(Exception):
                _close_prefs(serial)
        top_before = top_activity(serial) if top_before is None else top_before
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
        # when TalkBack came on, on the device's clock (talkback_started compares it
        # with an app's process start); kept in the snapshot for later walks
        out["on_uptime"] = uptime(serial)
        _update_snapshot(serial, on_uptime=out["on_uptime"])
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


def dismiss_talkback_activities(serial: str, top_before: Optional[str] = None) -> List[str]:
    """Send BACK while TalkBack's tutorial (first start) or the Accessibility
    Suite's POST_NOTIFICATIONS dialog (EVERY service start; BACK leaves the
    permission flags unchanged) is on top. The dialog can come a second or two
    after touch exploration is on, so this watches for DISMISS_WINDOW_S and
    stops early once one was dismissed and the top has stayed put. An injected
    BACK reaches the activity (it is not a TalkBack key).

    BACK goes only to an activity that is on top on two reads DISMISS_POLL_S
    apart: the dialog sits on TalkBack's PermissionRequestActivity, which
    finishes by itself once the dialog is gone, and a BACK sent as it leaves
    lands on the app below and closes it (seen on API 37: the app came back
    recreated, without the screen it had been started on)."""
    dismissed: List[str] = []
    deadline = time.monotonic() + DISMISS_WINDOW_S
    calm = 0
    while time.monotonic() < deadline:
        top = top_activity(serial)
        if top and top != top_before and top.startswith(
                tuple(p + "/" for p in _DISMISSABLE_PACKAGES)):
            time.sleep(DISMISS_POLL_S)
            if top_activity(serial) != top:
                continue  # it left by itself
            adb.shell(serial, "input keyevent KEYCODE_BACK")
            dismissed.append(top)
            calm = 0
            time.sleep(DISMISS_WAIT_S)
            continue
        calm += 1
        if dismissed and calm >= 2:
            break
        time.sleep(DISMISS_POLL_S)
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
    other = top.split("/", 1)[0] if top else None
    hint = f"Open {package} on the device, then retry."
    if other and other != package and not other.startswith(("com.android.", "android")):
        # "this screen" may mean the app that is on it (list_processes marks it foreground)
        hint = (f"The app on screen is {other}: tb_walk(package=\"{other}\") walks it (if "
                f"it is debuggable). Or open {package} on the device, then retry.")
    raise TalkBackError("app_left_foreground", f"{package} is not in the foreground on "
                                               f"{serial} (top: {top or 'unknown'})",
                        hint=hint)


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
    if not mismatch and snap.get("log_level_changed") and snap.get("log_level"):
        # TalkBack is off again, so its settings screen can be driven safely.
        try:
            set_log_level(serial, snap["log_level"])
        except Exception as exc:  # noqa: BLE001 - reported; the snapshot stays for a retry
            raise TalkBackError("restore_failed",
                                f"settings restored on {serial}, but TalkBack's log level could "
                                f"not be put back to {snap['log_level']}: {exc}",
                                hint="Run talkback restore again (TalkBack must stay off).") from exc
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


# --------------------------------------------------------------------------- #
# TalkBack's log level, through its own settings screen (TalkBack OFF only)
# --------------------------------------------------------------------------- #
def _require_off(serial: str) -> None:
    now = read_settings(serial)
    on = (talkback_in(now.get(SERVICES)) and now.get(A11Y_ENABLED) == "1") \
        or now.get(TOUCH_EXPLORATION) == "1"
    if on:
        raise TalkBackError("talkback_on", "TalkBack's log level can only be changed while "
                                           "TalkBack is off (uiautomator would suppress it)",
                            hint="talkback off first, or run the walk with utterance='auto'.")


def _ui_nodes(serial: str) -> List[Dict[str, Any]]:
    """The current screen from ``uiautomator dump`` (TalkBack must be off)."""
    import xml.etree.ElementTree as ET
    adb.shell(serial, f"uiautomator dump {_UI_DUMP}", check=False)
    raw = adb.shell(serial, f"cat {_UI_DUMP}", check=False)
    adb.shell(serial, f"rm -f {_UI_DUMP}", check=False)
    start = raw.find("<")
    if start < 0:
        return []
    try:
        root = ET.fromstring(raw[start:])
    except ET.ParseError:
        return []
    out: List[Dict[str, Any]] = []

    def visit(el: Any, parent: Optional[int]) -> None:
        idx = None
        if el.tag == "node":
            nums = [int(v) for v in re.findall(r"-?\d+", el.get("bounds", ""))]
            x1, y1, x2, y2 = (nums + [0, 0, 0, 0])[:4]
            idx = len(out)
            out.append({"text": el.get("text", ""), "checked": el.get("checked") == "true",
                        "center": ((x1 + x2) // 2, (y1 + y2) // 2), "parent": parent})
        for child in el:
            visit(child, idx if idx is not None else parent)

    visit(root, None)
    return out


def _find(nodes: List[Dict[str, Any]], pattern: str) -> Optional[Dict[str, Any]]:
    rx = re.compile(pattern)
    return next((n for n in nodes if rx.search(n["text"])), None)


def _tap(serial: str, node: Dict[str, Any]) -> None:
    x, y = node["center"]
    adb.shell(serial, f"input tap {x} {y}")
    time.sleep(UI_WAIT_S)


def _row_level(nodes: List[Dict[str, Any]]) -> Optional[str]:
    """The summary under the 'Log output level' row (its sibling text)."""
    title = _find(nodes, r"^Log output level$")
    if title is None:
        return None
    for n in nodes:
        if n is not title and n["parent"] == title["parent"] and n["text"] in LOG_LEVELS:
            return n["text"]
    return None


def _close_prefs(serial: str) -> None:
    for _ in range(4):
        top = top_activity(serial) or ""
        if not top.startswith(TALKBACK_PACKAGE + "/"):
            return
        adb.shell(serial, "input keyevent KEYCODE_BACK")
        time.sleep(DISMISS_WAIT_S)


def set_log_level(serial: str, level: str, record: bool = False) -> Dict[str, Any]:
    """Set TalkBack's Log output level through its Developer settings screen.

    TalkBack must be off (see the module docstring). With ``record``, the level
    it had is written into the pending snapshot BEFORE it changes, so a crash
    leaves enough for ``talkback restore``. Returns ``{"before", "after"}``.
    """
    level = level.upper()
    if level not in LOG_LEVELS:
        raise ValueError(f"log level must be one of {', '.join(LOG_LEVELS)}")
    _require_off(serial)
    adb.shell(serial, f"am start -n {TB_PREFS_ACTIVITY} --es FragmentName {TB_DEV_FRAGMENT}")
    time.sleep(UI_WAIT_S)
    try:
        nodes = _ui_nodes(serial)
        for _ in range(3):  # the row sits at the bottom of the list
            if _find(nodes, r"^Log output level$") is not None:
                break
            adb.shell(serial, "input swipe 500 1500 500 600 300")
            time.sleep(UI_WAIT_S)
            nodes = _ui_nodes(serial)
        before = _row_level(nodes)
        row = _find(nodes, r"^Log output level$")
        if row is None or before is None:
            raise TalkBackError("log_level_failed",
                                "could not find 'Log output level' in TalkBack's developer settings")
        if record:
            _update_snapshot(serial, log_level=before, log_level_changed=before != level)
        if before != level:
            _tap(serial, row)
            option = _find(_ui_nodes(serial), rf"^{level}$")
            if option is None:
                raise TalkBackError("log_level_failed", f"no {level} option in the log level list")
            _tap(serial, option)
            if level == "VERBOSE":
                # The confirmation button, not the dialog message that also says
                # "enable verbose logging".
                confirm = _find(_ui_nodes(serial), r"^Yes, enable verbose")
                if confirm is not None:
                    _tap(serial, confirm)
        after = _row_level(_ui_nodes(serial))
        if after != level:
            raise TalkBackError("log_level_failed", f"TalkBack's log level is {after}, not {level}")
        if record:
            _update_snapshot(serial, log_level_now=after)
        return {"before": before, "after": after}
    finally:
        _close_prefs(serial)


def status(serial: str) -> Dict[str, Any]:
    """Read-only: TalkBack install/enabled state, settings, pending restore, injectors."""
    settings = read_settings(serial)
    version = talkback_version(serial)
    snap = load_snapshot(serial)
    enabled = talkback_in(settings.get(SERVICES)) and settings.get(A11Y_ENABLED) == "1"
    out: Dict[str, Any] = {
        "serial": serial,
        # The TalkBack process outlives the service, so "running" needs both.
        "talkback": {"installed": version, "enabled": enabled,
                     "running": enabled and version is not None and talkback_pid(serial) is not None},
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
        if snap.get("log_level_changed"):
            out["saved"]["log_level"] = snap.get("log_level")
    if enabled:
        out["warning"] = DEVICE_WIDE_WARNING
    return out


ACTIONS = ("status", "on", "off", "restore")


def action(serial: str, what: str, package: Optional[str] = None,
           verbose_log: bool = False, verbose_required: bool = True) -> Dict[str, Any]:
    """The ``talkback`` tool / subcommand: status | on | off | restore.

    ``verbose_log`` (on): set TalkBack's log level to VERBOSE first, so walks get
    the exact announcements from logcat, also walks chained with leave_on (the level
    stays until restore, which puts the old one back). ``verbose_required`` False: a
    level that cannot be set is reported, not a failure."""
    if what == "status":
        return status(serial)
    if what not in ACTIONS:
        raise ValueError(f"unknown talkback action {what!r}; expected one of {', '.join(ACTIONS)}")
    with device_lock(serial, f"talkback {what}"):
        if what == "on":
            out = enable(serial, package, verbose_log=verbose_log,
                         verbose_required=verbose_required)
        elif what == "off":
            out = disable(serial)
        else:
            out = restore(serial)
    out["restore_pending"] = load_snapshot(serial) is not None
    return out


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)
