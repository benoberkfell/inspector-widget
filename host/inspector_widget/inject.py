"""ViewSpector injection sequence (host side), per CONTRACT.md section 2.

Pushes the three artifacts to /data/local/tmp, copies them into the app's
private data dir via run-as with the right permissions, attaches the native
JVMTI agent with ``cmd activity attach-agent``, forwards a TCP port to the
agent's abstract LocalServerSocket, and returns a connected socket.

Handles the warm path: if the agent is already attached for the running pid we
simply connect to the existing socket and PING it with a Hello.
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass
from typing import Optional

from . import adb
from .client import Client

# --------------------------------------------------------------------------- #
# Artifact names (CONTRACT.md section 3 / section 7). The host reads these from
# the build-out directory and pushes them to the device.
# --------------------------------------------------------------------------- #
NATIVE_SO_NAME = "libviewspector.so"
BOOTSTRAP_DEX_NAME = "bootstrap.dex"
PAYLOAD_JAR_NAME = "payload.jar"

DEVICE_TMP_DIR = "/data/local/tmp"

# Default build-out location relative to the repo root (host/.. = repo root).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_BUILD_OUT = os.path.join(_REPO_ROOT, "build-out")


def socket_name_for_pid(pid: int) -> str:
    """The abstract socket name the payload binds: ``viewspector_<pid>``."""
    return f"viewspector_{pid}"


@dataclass
class Injection:
    """Result of a successful inject/connect: an open socket plus metadata."""

    serial: str
    package: str
    pid: int
    socket_name: str
    local_port: int
    sock: socket.socket
    warm: bool  # True if we connected to an already-attached agent

    def close(self) -> None:
        try:
            self.sock.close()
        finally:
            adb.remove_forward(self.serial, self.local_port)


class InjectionError(RuntimeError):
    pass


def _artifact_path(build_out: str, name: str) -> str:
    path = os.path.join(build_out, name)
    if not os.path.isfile(path):
        raise InjectionError(
            f"required artifact not found: {path}\n"
            f"Run scripts/build.sh to produce {NATIVE_SO_NAME}, "
            f"{BOOTSTRAP_DEX_NAME} and {PAYLOAD_JAR_NAME} into build-out/."
        )
    return path


def _connect_forward(serial: str, socket_name: str, local_port: int,
                     connect_timeout: float = 5.0) -> socket.socket:
    """Set up the adb forward and open a TCP socket to the agent."""
    port = adb.forward(serial, local_port, socket_name)
    sock = socket.create_connection(("127.0.0.1", port), timeout=connect_timeout)
    sock.settimeout(None)  # blocking for synchronous request/response
    return sock


def _try_warm_connect(serial: str, pid: int) -> Optional[Injection]:
    """Attempt to connect to an already-running agent for ``pid``.

    Returns an :class:`Injection` if the abstract socket exists and a Hello
    round-trips; otherwise ``None`` (and any partial forward is cleaned up).
    """
    socket_name = socket_name_for_pid(pid)
    if not adb.socket_exists(serial, socket_name):
        return None
    local_port = adb.free_local_port()
    try:
        sock = _connect_forward(serial, socket_name, local_port)
    except OSError:
        adb.remove_forward(serial, local_port)
        return None
    # PING via Hello to confirm the agent is live and speaks our protocol.
    client = Client(sock, owns_socket=False)
    try:
        client.hello()
    except Exception:
        sock.close()
        adb.remove_forward(serial, local_port)
        return None
    return Injection(
        serial=serial,
        package="",  # filled in by caller
        pid=pid,
        socket_name=socket_name,
        local_port=local_port,
        sock=sock,
        warm=True,
    )


def _push_and_stage(serial: str, package: str, build_out: str):
    """Push the three artifacts and copy them into the app private dir.

    Returns (app_so_path, app_bootstrap_path, app_payload_path).
    Permissions per the task spec: .so 700, dex/jar 444.
    """
    so_local = _artifact_path(build_out, NATIVE_SO_NAME)
    boot_local = _artifact_path(build_out, BOOTSTRAP_DEX_NAME)
    payload_local = _artifact_path(build_out, PAYLOAD_JAR_NAME)

    tmp_so = f"{DEVICE_TMP_DIR}/{NATIVE_SO_NAME}"
    tmp_boot = f"{DEVICE_TMP_DIR}/{BOOTSTRAP_DEX_NAME}"
    tmp_payload = f"{DEVICE_TMP_DIR}/{PAYLOAD_JAR_NAME}"

    adb.push(serial, so_local, tmp_so)
    adb.push(serial, boot_local, tmp_boot)
    adb.push(serial, payload_local, tmp_payload)

    # Copy into the app's private dir via run-as with the required modes.
    # The .so must be executable-by-owner (700); dex/jar are read-only (444).
    app_so = adb.run_as_cp(serial, package, tmp_so, NATIVE_SO_NAME, mode="700")
    app_boot = adb.run_as_cp(serial, package, tmp_boot, BOOTSTRAP_DEX_NAME, mode="444")
    app_payload = adb.run_as_cp(serial, package, tmp_payload, PAYLOAD_JAR_NAME, mode="444")
    return app_so, app_boot, app_payload


def _wait_for_socket(serial: str, socket_name: str,
                     max_attempts: int = 15, initial_delay: float = 0.1) -> None:
    """Poll /proc/net/unix until the agent's abstract socket appears.

    Exponential backoff capped at 1s (mirrors ui-inspector waitForAgentSocket).
    """
    delay = initial_delay
    for _ in range(max_attempts):
        if adb.socket_exists(serial, socket_name):
            return
        time.sleep(delay)
        delay = min(delay * 2, 1.0)
    raise InjectionError(
        f"timed out waiting for agent socket '{socket_name}'. "
        f"Check 'adb logcat -s ViewSpector' for agent-side errors."
    )


def inject_and_connect(
    serial: str = adb.DEFAULT_SERIAL,
    package: str = "com.oberkfell.a11yprobe",
    build_out: str = DEFAULT_BUILD_OUT,
    force_reinject: bool = False,
) -> Injection:
    """Full inject + connect, returning a connected :class:`Injection`.

    Sequence (CONTRACT.md section 2):
      1. resolve pid (app must be running)
      2. warm path: if the agent socket already exists and Hello succeeds, reuse it
      3. push .so + bootstrap.dex + payload.jar to /data/local/tmp
      4. run-as cp into the app private dir (.so 700, dex/jar 444)
      5. cmd activity attach-agent <pkg> <so>=<bootstrap>:<payload>:viewspector_<pid>
      6. wait for the abstract socket, adb forward, connect, return socket
    """
    pid = adb.pidof(serial, package)
    if pid is None:
        raise InjectionError(
            f"package '{package}' is not running on {serial}. "
            f"Launch the app, then retry."
        )

    # Enable attribute resolution-stack tracking, exactly as Android Studio's
    # Layout Inspector does. Best-effort: stacks only populate for views inflated
    # while this global is set, so a freshly-launched app benefits most.
    try:
        adb.shell(serial, "settings put global debug_view_attributes 1")
    except Exception:
        pass

    if not force_reinject:
        warm = _try_warm_connect(serial, pid)
        if warm is not None:
            warm.package = package
            return warm

    app_so, app_boot, app_payload = _push_and_stage(serial, package, build_out)

    socket_name = socket_name_for_pid(pid)
    # Native agent option string: bootstrapDexPath:payloadPath:socketName.
    # The native Agent_OnAttach reads everything after '=' and splits on ':'.
    options = f"{app_boot}:{app_payload}:{socket_name}"
    adb.attach_agent(serial, package, app_so, options)

    _wait_for_socket(serial, socket_name)

    local_port = adb.free_local_port()
    # Give the LocalServerSocket a brief moment to start accept()-ing.
    last_err: Optional[Exception] = None
    sock: Optional[socket.socket] = None
    for attempt in range(10):
        try:
            sock = _connect_forward(serial, socket_name, local_port)
            break
        except OSError as e:
            last_err = e
            adb.remove_forward(serial, local_port)
            time.sleep(0.2)
    if sock is None:
        raise InjectionError(
            f"could not connect to agent socket '{socket_name}': {last_err}"
        )

    inj = Injection(
        serial=serial,
        package=package,
        pid=pid,
        socket_name=socket_name,
        local_port=local_port,
        sock=sock,
        warm=False,
    )
    # Sanity PING so callers get a live connection or a clear failure.
    client = Client(sock, owns_socket=False)
    try:
        client.hello()
    except Exception as e:
        inj.close()
        raise InjectionError(f"agent attached but Hello failed: {e}") from e
    return inj
