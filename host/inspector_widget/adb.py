"""Thin wrappers over ``adb -s <serial>`` via subprocess.

Every function shells out to the ``adb`` binary on PATH with an explicit serial
so it works against a specific device. :func:`resolve_serial` picks the device
when the caller doesn't name one (``$ANDROID_SERIAL``, else the only attached
device). Errors from adb are captured and re-raised as :class:`AdbError` with
the full stderr/stdout for debuggability; a missing, offline or unauthorized
device is reported as a :class:`DeviceError` with a one-line explanation.

This is a clean-room re-implementation of the device-control surface the
ui-inspector ``InjectionManager`` performs over adblib, expressed in terms of
the plain ``adb`` CLI.
"""

from __future__ import annotations

import math
import os
import re
import shlex
import socket
import subprocess
import threading
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# Kept for callers that still import it; nothing defaults to it any more. Use
# resolve_serial(None) to pick $ANDROID_SERIAL or the single attached device.
DEFAULT_SERIAL = "emulator-5554"

# adb's own variable for "the device to talk to when -s is not given".
SERIAL_ENV = "ANDROID_SERIAL"

# A generously large default; pushes of a few-MB jar/so finish well within this.
DEFAULT_TIMEOUT = 60.0


class AdbError(RuntimeError):
    """Raised when an adb invocation fails (non-zero exit) or times out."""

    def __init__(self, argv: Sequence[str], returncode: int, stdout: str, stderr: str):
        self.argv = list(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        cmd = " ".join(shlex.quote(a) for a in argv)
        super().__init__(
            f"adb command failed (exit {returncode}): {cmd}\n"
            f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
        )


class DeviceError(RuntimeError):
    """No usable device: none attached, several and none chosen, the named
    serial isn't attached, or it is offline / unauthorized."""


@dataclass(frozen=True)
class Device:
    serial: str
    state: str  # "device", "offline", "unauthorized", ...


def _run(
    argv: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    check: bool = True,
    binary: bool = False,
) -> subprocess.CompletedProcess:
    """Run an adb argv, capturing output. Raises :class:`AdbError` on failure."""
    try:
        proc = subprocess.run(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        out = e.stdout or (b"" if binary else "")
        err = e.stderr or (b"" if binary else "")
        if isinstance(out, bytes) and not binary:
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes) and not binary:
            err = err.decode("utf-8", "replace")
        raise AdbError(argv, -1, str(out), f"timed out after {timeout}s\n{err}") from e
    except FileNotFoundError as e:
        raise AdbError(argv, -1, "", f"adb binary not found on PATH: {e}") from e

    if not binary:
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
    else:
        stdout = proc.stdout
        stderr = proc.stderr.decode("utf-8", "replace")

    if check and proc.returncode != 0:
        raise AdbError(
            argv,
            proc.returncode,
            stdout if isinstance(stdout, str) else "<binary>",
            stderr,
        )
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def _adb(serial: Optional[str], *args: str, **kwargs) -> subprocess.CompletedProcess:
    base: List[str] = ["adb"]
    if serial:
        base += ["-s", serial]
    base += list(args)
    return _run(base, **kwargs)


# --------------------------------------------------------------------------- #
# Device discovery
# --------------------------------------------------------------------------- #
def devices() -> List[Device]:
    """Return the attached devices and their states (parses ``adb devices``)."""
    proc = _run(["adb", "devices"])
    out = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            out.append(Device(serial=parts[0].strip(), state=parts[1].strip()))
    return out


def _describe(devs: Sequence[Device]) -> str:
    return ", ".join(f"{d.serial} ({d.state})" for d in devs) or "none"


_STATE_ADVICE = {
    "unauthorized": "accept the 'Allow USB debugging' prompt on the device, then retry",
    "offline": "reconnect it (adb reconnect) or restart the emulator, then retry",
    "authorizing": "wait for the device to finish authorizing, then retry",
    "no permissions": "fix the host's USB permissions (udev rules) for this device",
}


def resolve_serial(serial: Optional[str] = None) -> str:
    """The device serial to use, checked against ``adb devices``.

    ``serial`` wins, then ``$ANDROID_SERIAL``, then the only device in the
    ``device`` state. Raises :class:`DeviceError` with the attached devices
    listed when there is none, several, or the chosen one isn't usable.
    """
    chosen = serial or os.environ.get(SERIAL_ENV) or None
    devs = devices()
    if chosen:
        ensure_device(chosen, devs)
        return chosen
    ready = [d for d in devs if d.state == "device"]
    if len(ready) == 1:
        return ready[0].serial
    if not ready:
        if devs:
            raise DeviceError(
                f"no usable Android device: attached are {_describe(devs)}. "
                + (_STATE_ADVICE.get(devs[0].state, "") if len(devs) == 1 else "")
            )
        raise DeviceError(
            "no Android device attached (adb devices lists none). Start an emulator "
            "or connect a device with USB debugging enabled."
        )
    raise DeviceError(
        f"more than one device attached ({_describe(ready)}); choose one with the "
        f"serial argument (CLI: --serial) or set ${SERIAL_ENV}."
    )


def ensure_device(serial: str, devs: Optional[Sequence[Device]] = None) -> None:
    """Raise :class:`DeviceError` unless ``serial`` is attached and in the ``device`` state."""
    devs = devices() if devs is None else devs
    match = next((d for d in devs if d.serial == serial), None)
    if match is None:
        raise DeviceError(
            f"device '{serial}' not found; attached: {_describe(devs)}. "
            f"Pick a serial from `adb devices` (CLI: --serial, or set ${SERIAL_ENV})."
        )
    if match.state != "device":
        advice = _STATE_ADVICE.get(match.state, "wait until `adb devices` shows it as 'device'")
        raise DeviceError(f"device '{serial}' is {match.state}: {advice}.")


def wait_for_device(serial: Optional[str] = None, timeout: float = 30.0) -> None:
    """Block until ``serial`` (or the only device) reaches the ``device`` state."""
    _adb(serial, "wait-for-device", timeout=timeout)


# --------------------------------------------------------------------------- #
# Shell
# --------------------------------------------------------------------------- #
def shell(
    serial: str,
    cmd: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    check: bool = True,
) -> str:
    """Run ``adb shell <cmd>`` and return stdout (text)."""
    proc = _adb(serial, "shell", cmd, timeout=timeout, check=check)
    return proc.stdout


def list_debuggable_packages(serial: str) -> List[str]:
    """Return installed packages that are debuggable (i.e. run-as-able).

    There is no direct adb query for the debuggable flag, so we enumerate
    installed third-party packages and probe each with ``run-as <pkg> true``,
    which only succeeds for debuggable apps signed for this device/user.
    """
    # `-3` limits to third-party (non-system) packages, which is what we can
    # inspect; debuggable system apps are rare and out of scope here.
    raw = shell(serial, "pm list packages -3")
    pkgs = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            pkgs.append(line[len("package:"):].strip())
    return [pkg for pkg in pkgs if run_as_probe(serial, pkg)[0]]


def run_as_probe(serial: str, pkg: str) -> "tuple[bool, str]":
    """``run-as <pkg> true``: ``(True, "")`` if the app is debuggable (run-as-able),
    else ``(False, what run-as printed)``."""
    # run-as returns non-zero for non-debuggable apps; suppress the raise.
    proc = _adb(serial, "shell", f"run-as {shlex.quote(pkg)} true", check=False)
    said = f"{proc.stdout or ''}{proc.stderr or ''}".strip()
    if proc.returncode == 0 and "not debuggable" not in said.lower():
        return True, ""
    return False, said or f"exit {proc.returncode}"


def pidof(serial: str, pkg: str) -> Optional[int]:
    """Return the (primary) PID of ``pkg`` if running, else ``None``.

    toybox ``pidof`` exits 1 with no output when nothing matches, which is
    ``None``. Any other failure (the device vanished, adb itself errored) says
    so on stderr and raises :class:`AdbError`, so a bad serial is never
    mistaken for "the app isn't running".
    """
    argv_cmd = f"pidof {shlex.quote(pkg)}"
    proc = _adb(serial, "shell", argv_cmd, check=False)
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 and (proc.stderr or "").strip():
        raise AdbError(["adb", "-s", serial, "shell", argv_cmd], proc.returncode,
                       proc.stdout, proc.stderr)
    if not out:
        return None
    # pidof may return several space-separated PIDs for multi-process apps;
    # the first is the main process (mirrors InjectionManager.getPid).
    first = out.split()[0]
    try:
        return int(first)
    except ValueError:
        return None


def app_data_dir(serial: str, pkg: str) -> str:
    """Return the app's private data dir via ``run-as <pkg> pwd``.

    Queried at runtime so multi-user paths (e.g. /data/user/10/...) work, just
    like ui-inspector's ``queryAppDataDir``.
    """
    out = shell(serial, f"run-as {shlex.quote(pkg)} pwd").strip()
    if not out:
        raise AdbError(
            ["adb", "shell", f"run-as {pkg} pwd"],
            0,
            out,
            "empty app data dir; is the package debuggable and installed?",
        )
    # pwd may print a trailing component list; take the first line.
    return out.splitlines()[0].strip()


def device_abi(serial: str) -> str:
    """The device's primary ABI (``ro.product.cpu.abi``), e.g. ``arm64-v8a``.

    Unlike ``ro.product.cpu.abilist``, this never names an ABI the device only
    runs through native-bridge translation (an x86_64 emulator image with ARM
    translation lists ``arm64-v8a`` there, but its processes are x86_64).
    """
    return shell(serial, "getprop ro.product.cpu.abi").strip()


def process_bitness(serial: str, pkg: str, pid: int) -> Optional[int]:
    """64 or 32: whether ``pid`` runs ``app_process64`` or ``app_process32``.

    Reads ``/proc/<pid>/exe`` as the app's own uid (``run-as``), since the
    shell user may not read another uid's exe link. ``None`` when it can't be
    read or doesn't say (a bare ``app_process``, a non-debuggable app).
    """
    cmd = f"run-as {shlex.quote(pkg)} readlink /proc/{int(pid)}/exe"
    try:
        proc = _adb(serial, "shell", cmd, check=False)
    except AdbError:
        return None
    target = (proc.stdout or "").strip().splitlines()
    name = target[0].rsplit("/", 1)[-1] if proc.returncode == 0 and target else ""
    if name == "app_process64":
        return 64
    if name == "app_process32":
        return 32
    return None


def api_level(serial: str) -> int:
    out = shell(serial, "getprop ro.build.version.sdk").strip()
    try:
        return int(out)
    except ValueError:
        return 0


def display_density(serial: str) -> int:
    """Return the device's display density in raw DPI (e.g. 420).

    Parses ``adb shell wm density``, whose output may contain a
    "Physical density: <n>" line and an optional "Override density: <n>"
    line. The override density is what the UI is actually rendered at, so
    it is preferred over the physical density when present.

    Returns ``420`` on any failure (missing/empty output, unparseable
    values, or adb error).
    """
    try:
        out = shell(serial, "wm density", check=False).strip()
        physical: Optional[int] = None
        override: Optional[int] = None
        for line in out.splitlines():
            if ":" not in line:
                continue
            label, _, value = line.partition(":")
            try:
                dpi = int(value.strip())
            except ValueError:
                continue
            low = label.lower()
            if "override" in low:
                override = dpi
            elif "physical" in low:
                physical = dpi
            elif physical is None and override is None:
                # Fallback for any other "<label>: <n>" form (e.g. bare value).
                physical = dpi
        dpi = override if override is not None else physical
        if dpi:
            return dpi
    except Exception:
        pass
    return 420


def font_scale(serial: str) -> float:
    """Return the system font scale (e.g. 1.0, 1.3) as a float.

    Parses ``adb shell settings get system font_scale``. Returns ``1.0`` on
    any failure, including the literal ``"null"`` Android prints when the
    setting is unset.
    """
    try:
        out = shell(serial, "settings get system font_scale", check=False).strip()
        if not out or out.lower() == "null":
            return 1.0
        return float(out)
    except Exception:
        return 1.0


# --------------------------------------------------------------------------- #
# File transfer
# --------------------------------------------------------------------------- #
def push(serial: str, local: str, remote: str) -> str:
    """``adb push`` a local file to ``remote`` on the device. Returns ``remote``."""
    _adb(serial, "push", local, remote, timeout=120.0)
    return remote


def run_as_cp(serial: str, pkg: str, src: str, dst_name: str, mode: Optional[str] = None) -> str:
    """Copy ``src`` (a /data/local/tmp path) into the app's private dir as ``dst_name``.

    Uses ``run-as <pkg> sh -c 'cat src > dst && chmod ...'`` because run-as runs
    as the app uid and can write the app-private dir; a plain ``cp`` is not always
    available on all images, so ``cat >`` is used (mirrors InjectionManager).

    ``dst_name`` is relative to the app's cwd (its data dir). Returns the absolute
    on-device path of the copied file.
    """
    quoted_dst = shlex.quote(dst_name)
    quoted_src = shlex.quote(src)
    parts = [
        f"rm -f {quoted_dst}",
        f"cat {quoted_src} > {quoted_dst}",
    ]
    if mode:
        parts.append(f"chmod {mode} {quoted_dst}")
    inner = " && ".join(parts)
    # Wrap the inner script in single quotes for the outer `sh -c`.
    cmd = f"run-as {shlex.quote(pkg)} sh -c {shlex.quote(inner)}"
    shell(serial, cmd)
    data_dir = app_data_dir(serial, pkg)
    return f"{data_dir}/{dst_name}"


# --------------------------------------------------------------------------- #
# Port forwarding
# --------------------------------------------------------------------------- #
# Forwards this process created and has not removed yet: (serial, port) -> name.
# remove_own_forwards() clears whatever is left at exit, including a forward made
# by an attach that a signal or Ctrl-C interrupted before it returned a session.
_OWN_FORWARDS: Dict[Tuple[str, int], str] = {}
_FORWARDS_LOCK = threading.Lock()


def forward(serial: str, local_port: int, abstract_name: str) -> int:
    """Forward ``tcp:<local_port>`` to ``localabstract:<abstract_name>``.

    If ``local_port`` is 0, adb picks a free port and we parse+return it. Asking
    adb for the port avoids the race of picking one on the host first, where
    another forward can take it in between.
    """
    local_spec = f"tcp:{local_port}"
    remote_spec = f"localabstract:{abstract_name}"
    proc = _adb(serial, "forward", local_spec, remote_spec)
    port = local_port
    if local_port == 0:
        out = proc.stdout.strip()
        try:
            port = int(out)
        except ValueError as e:
            raise AdbError(
                ["adb", "forward", local_spec, remote_spec],
                0,
                proc.stdout,
                f"could not parse allocated port from adb forward output: {out!r}",
            ) from e
    with _FORWARDS_LOCK:
        _OWN_FORWARDS[(serial, port)] = abstract_name
    return port


def remove_forward(serial: str, local_port: int) -> None:
    """Remove a single tcp forward (``adb forward --remove tcp:<port>``)."""
    with _FORWARDS_LOCK:
        _OWN_FORWARDS.pop((serial, local_port), None)
    _adb(serial, "forward", "--remove", f"tcp:{local_port}", check=False)


def remove_own_forwards() -> int:
    """Remove every forward this process created and still holds. For exit
    cleanup; returns how many were removed."""
    with _FORWARDS_LOCK:
        leftover = list(_OWN_FORWARDS)
    for serial, port in leftover:
        try:
            remove_forward(serial, port)
        except Exception:  # noqa: BLE001 - best-effort at exit
            pass
    return len(leftover)


def attach_agent(serial: str, pkg: str, so_path: str, options: str) -> str:
    """Attach the JVMTI agent: ``cmd activity attach-agent <pkg> <so>=<options>``.

    ``options`` is the native agent's option string, which for ViewSpector is
    ``bootstrapDexPath:payloadPath:socketName`` (colon-separated). The agent
    parses everything after ``=`` (see CONTRACT.md section 2 and the native
    Agent_OnAttach option parser). Returns the shell output.
    """
    spec = f"{so_path}={options}"
    cmd = f"cmd activity attach-agent {shlex.quote(pkg)} {shlex.quote(spec)}"
    return shell(serial, cmd)


def free_local_port() -> int:
    """Pick a free TCP port on the host by binding to port 0 and reading it back.

    Racy (the port is free only until someone else binds it); prefer
    ``forward(serial, 0, name)``, which lets adb pick the port.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


# /proc/net/unix columns: Num RefCount Protocol Flags Type St Inode Path. A
# listening socket has __SO_ACCEPTCON in Flags. A connection accepted from it
# (or still queued for accept) shows the same @name with Flags 0, and it stays
# listed for as long as either end keeps it open, even after the listener has
# gone, so only the listening entry says an agent is there.
_SO_ACCEPTCON = 0x00010000


def _unix_socket_entries(serial: str, abstract_name: str) -> List[Tuple[int, int]]:
    """``(flags, state)`` for each /proc/net/unix entry whose path is exactly
    ``@abstract_name``."""
    out = shell(
        serial,
        f"cat /proc/net/unix | grep {shlex.quote(abstract_name)} || true",
        check=False,
    )
    wanted = "@" + abstract_name
    entries = []
    for line in out.splitlines():
        parts = line.split()
        # grep only narrows the output; the match is exact on the path column
        # (@viewspector_42 must not match @viewspector_421).
        if len(parts) < 8 or parts[-1] != wanted:
            continue
        try:
            entries.append((int(parts[3], 16), int(parts[5], 16)))
        except ValueError:
            continue
    return entries


def socket_exists(serial: str, abstract_name: str) -> bool:
    """Return True if something LISTENS on the abstract unix socket ``abstract_name``.

    Mirrors ui-inspector's ``waitForAgentSocket`` which greps /proc/net/unix.
    Connections to the name don't count: another client's connection outlives
    a stopped agent, and must not look like an agent still holding the name.
    """
    return any(flags & _SO_ACCEPTCON for flags, _state in _unix_socket_entries(serial, abstract_name))


def socket_connections(serial: str, abstract_name: str) -> int:
    """How many connections to ``abstract_name`` exist (accepted or queued).

    The agent's end of every client connection is listed under its name, so
    this counts the clients connected to the agent, including the caller's own.
    """
    return sum(1 for flags, _state in _unix_socket_entries(serial, abstract_name)
               if not flags & _SO_ACCEPTCON)


def process_frozen(serial: str, pid: int) -> Optional[bool]:
    """Whether Android's cached-apps freezer has frozen ``pid`` (cgroup v2
    ``cgroup.events``: ``frozen 1``). ``None`` when the state can't be read.

    A frozen app runs nothing, including a queued ``attach-agent``, until it is
    brought back to the foreground.
    """
    cmd = (f"cat /sys/fs/cgroup$(sed -n 's/^0:://p' /proc/{int(pid)}/cgroup)/cgroup.events "
           f"2>/dev/null || true")
    try:
        out = shell(serial, cmd, check=False)
    except Exception:  # noqa: BLE001 - diagnostic only
        return None
    for line in out.splitlines():
        key, _, value = line.strip().partition(" ")
        if key == "frozen" and value.strip() in ("0", "1"):
            return value.strip() == "1"
    return None


# --------------------------------------------------------------------------- #
# Device clock + logcat (agent start-up diagnostics)
# --------------------------------------------------------------------------- #
def device_time(serial: str) -> Optional[float]:
    """The device's wall clock in seconds since the epoch (the clock ``logcat -v
    epoch`` stamps lines with), or ``None`` if it can't be read.

    Asks for nanoseconds (``date +%s.%N``); a ``date`` without ``%N`` still
    yields whole seconds, which only makes a ``since`` filter start up to a
    second early.
    """
    try:
        out = shell(serial, "date +%s.%N", check=False, timeout=10.0).strip()
    except AdbError:
        return None
    m = re.match(r"(\d+)(?:\.(\d+))?", out)
    if not m:
        return None
    return float(f"{m.group(1)}.{m.group(2)}") if m.group(2) else float(m.group(1))


@dataclass(frozen=True)
class LogEntry:
    """One logcat line. A multi-line message (a Log.e with a stack trace) is
    one entry per line, all with the same ``time``, ``tid`` and ``tag``."""

    time: float  # seconds since the epoch, device clock
    pid: int
    tid: int
    level: str  # V D I W E F
    tag: str
    message: str


# threadtime with the epoch + usec modifiers:
#   "  1727706579.756123 10709 10720 E ViewSpector: initialize: error invoking ..."
_LOGCAT_LINE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s+(\d+)\s+(\d+)\s+([VDIWEFS])\s+(.*?)\s*: ?(.*)$")


def parse_logcat(text: str) -> List[LogEntry]:
    """Parse ``logcat -v threadtime -v epoch -v usec`` output; other lines
    (``--------- beginning of main``) are skipped."""
    entries = []
    for line in text.splitlines():
        m = _LOGCAT_LINE.match(line)
        if m:
            entries.append(LogEntry(float(m.group(1)), int(m.group(2)), int(m.group(3)),
                                    m.group(4), m.group(5), m.group(6)))
    return entries


def logcat(serial: str, *, pid: Optional[int] = None, since: Optional[float] = None,
           specs: Iterable[str] = ("*:V",), timeout: float = 10.0) -> Optional[List[LogEntry]]:
    """Dump (``logcat -d``) the lines matching the filterspecs ``specs`` (e.g.
    ``("ViewSpector:E", "AndroidRuntime:E")``), optionally only ``pid``'s and
    only those stamped at or after ``since`` (device clock, see
    :func:`device_time`).

    For diagnostics, so it never raises: ``None`` when adb or logcat failed
    (as opposed to ``[]``, nothing logged).
    """
    specs = list(specs)
    argv = ["logcat", "-d", "-v", "threadtime", "-v", "epoch", "-v", "usec"]
    if pid is not None:
        argv.append(f"--pid={int(pid)}")
    floor = None
    if since is not None:
        # logcat stamps to the microsecond; "-t <secs>.<frac>" is a start time
        # (pure digits would be a line count, so the fraction is always there).
        floor = math.floor(since * 1000) / 1000
        argv += ["-t", f"{floor:.3f}"]
    if not any(s.startswith("*:") for s in specs):
        argv.append("-s")
    argv += specs
    try:
        proc = _adb(serial, "shell", " ".join(shlex.quote(a) for a in argv),
                    check=False, timeout=timeout)
    except AdbError:
        return None
    if proc.returncode != 0:
        return None
    entries = parse_logcat(proc.stdout or "")
    if pid is not None:
        entries = [e for e in entries if e.pid == pid]
    if floor is not None:
        entries = [e for e in entries if e.time >= floor]
    return entries
