"""ViewSpector injection sequence (host side), per CONTRACT.md section 2.

Pushes the three artifacts to /data/local/tmp, copies them into the app's
private data dir via run-as with the right permissions, attaches the native
JVMTI agent with ``cmd activity attach-agent``, forwards a TCP port to the
agent's abstract LocalServerSocket, and returns a connected socket.

Handles the warm path: if the agent is already attached for the running pid we
simply connect to the existing socket and PING it with a Hello.
"""

from __future__ import annotations

import hashlib
import os
import socket
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from . import adb
from .client import LOGCAT_HINT, AgentTimeoutError, Client, TransportError

# --------------------------------------------------------------------------- #
# Artifact names (CONTRACT.md section 3 / section 7). The host reads these from
# the build-out directory and pushes them to the device.
# --------------------------------------------------------------------------- #
NATIVE_SO_NAME = "libviewspector.so"
BOOTSTRAP_DEX_NAME = "bootstrap.dex"
PAYLOAD_JAR_NAME = "payload.jar"

DEVICE_TMP_DIR = "/data/local/tmp"

ARTIFACT_NAMES = (NATIVE_SO_NAME, BOOTSTRAP_DEX_NAME, PAYLOAD_JAR_NAME)

# Where the artifacts are read from, in precedence order (see resolve_build_out):
#   1. an explicit directory (the CLI's --build-out DIR)
#   2. $INSPECTOR_WIDGET_ARTIFACTS
#   3. $VIEWSPECTOR_ARTIFACTS          (legacy name, still honoured)
#   4. DEFAULT_BUILD_OUT               (<repo>/build-out, for a source checkout)
# The default only makes sense when running from the checkout: after a wheel
# install this file lives in site-packages, so point the env var (or
# --build-out) at the checkout's build-out/ instead.
ARTIFACTS_ENV = "INSPECTOR_WIDGET_ARTIFACTS"
LEGACY_ARTIFACTS_ENV = "VIEWSPECTOR_ARTIFACTS"

# Default build-out location relative to the repo root (host/.. = repo root).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_BUILD_OUT = os.path.join(_REPO_ROOT, "build-out")


def _resolve_build_out(build_out: Optional[str] = None) -> Tuple[str, str]:
    """Return ``(directory, source)``; ``source`` says which setting chose it."""
    if build_out:
        chosen, source = build_out, "--build-out"
    elif os.environ.get(ARTIFACTS_ENV):
        chosen, source = os.environ[ARTIFACTS_ENV], f"${ARTIFACTS_ENV}"
    elif os.environ.get(LEGACY_ARTIFACTS_ENV):
        chosen, source = os.environ[LEGACY_ARTIFACTS_ENV], f"${LEGACY_ARTIFACTS_ENV}"
    else:
        chosen, source = DEFAULT_BUILD_OUT, "default"
    return os.path.abspath(os.path.expanduser(chosen)), source


def resolve_build_out(build_out: Optional[str] = None) -> str:
    """The directory holding the three on-device artifacts.

    ``build_out`` (e.g. from ``--build-out``) wins, then ``$INSPECTOR_WIDGET_ARTIFACTS``,
    then the legacy ``$VIEWSPECTOR_ARTIFACTS``, then the repo's ``build-out/``.
    Read at call time, so an env var set after import still applies.
    """
    return _resolve_build_out(build_out)[0]


def artifact_status(build_out: Optional[str] = None) -> Dict[str, object]:
    """Where the artifacts are looked up and which are present (no device needed).

    Returns ``{"dir", "source", "present": {name: bool}, "missing": [name, ...],
    "build_id"}``; ``build_id`` is :func:`local_build_id` (``None`` without a
    payload.jar).
    """
    directory, source = _resolve_build_out(build_out)
    present = {n: os.path.isfile(os.path.join(directory, n)) for n in ARTIFACT_NAMES}
    return {
        "dir": directory,
        "source": source,
        "present": present,
        "missing": [n for n, ok in present.items() if not ok],
        "build_id": local_build_id(directory),
    }


# --------------------------------------------------------------------------- #
# Build handshake (H4). The payload hashes the payload.jar it was loaded from
# and reports it in Hello as "<agent_version>+<sha256>"; scripts/build.sh writes
# the same hash to build-out/BUILD_ID. A running agent whose build differs from
# the local payload.jar is stale (e.g. after a rebuild) and gets replaced.
# --------------------------------------------------------------------------- #
BUILD_ID_NAME = "BUILD_ID"
UNKNOWN_BUILD = "unknown"  # a payload that couldn't hash its own jar
_BUILD_ID_CACHE: Dict[Tuple[str, int, int], str] = {}


def local_build_id(build_out: Optional[str] = None) -> Optional[str]:
    """sha256 (hex) of the payload.jar that an inject would push, or ``None``
    when there is none. The same value build.sh writes to ``BUILD_ID``."""
    path = os.path.join(resolve_build_out(build_out), PAYLOAD_JAR_NAME)
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime_ns, st.st_size)
    cached = _BUILD_ID_CACHE.get(key)
    if cached is None:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                digest.update(chunk)
        cached = _BUILD_ID_CACHE[key] = digest.hexdigest()
    return cached


def split_agent_version(agent_version: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """``"viewspector-0.1+<sha256>"`` -> ``("viewspector-0.1", "<sha256>")``.

    Agents built before the handshake report no ``+`` part: ``(version, None)``.
    """
    if not agent_version:
        return None, None
    base, sep, build = agent_version.partition("+")
    return base, (build or None) if sep else None


def build_matches(agent_build: Optional[str], local_build: Optional[str]) -> bool:
    """Whether a running agent can be reused for the local artifacts.

    No local payload.jar (e.g. a wheel install that only re-attaches): any agent
    will do. An agent from before the handshake (no build id) is stale. An agent
    that couldn't hash its jar is given the benefit of the doubt.
    """
    if local_build is None or agent_build == UNKNOWN_BUILD:
        return True
    return agent_build == local_build


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
    hello: Any = None  # the agent's HelloResponse (agent_version / api_level / abi)
    closed: bool = False
    # Set when the connection works but something about it deserves a warning,
    # e.g. a stale-build agent kept because other clients are using it.
    note: Optional[str] = None

    @property
    def agent_version(self) -> Optional[str]:
        """The agent's version without the build id, e.g. ``viewspector-0.1``."""
        return split_agent_version(getattr(self.hello, "agent_version", None))[0]

    @property
    def build_id(self) -> Optional[str]:
        """sha256 of the payload.jar the agent runs; ``None`` for pre-handshake agents."""
        return split_agent_version(getattr(self.hello, "agent_version", None))[1]

    def close(self, close_socket: bool = True) -> None:
        """Close the socket and remove the adb forward. Idempotent; the agent
        keeps running.

        Not for a socket a request may be in flight on: closing it under the
        reading thread can leave that thread waiting out its deadline. End such
        a connection with ``Client.abort`` (``Session.disconnect`` does), which
        shuts it down and leaves the close to the reader, then call this with
        ``close_socket=False`` if the abort couldn't close it.
        """
        if self.closed:
            return
        self.closed = True
        try:
            if close_socket:
                _shutdown_and_close(self.sock)
        finally:
            adb.remove_forward(self.serial, self.local_port)


def _shutdown_and_close(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


# Where to look when the agent itself misbehaved (it attached, or should have).
AGENT_LOG_HINT = f"Check `{LOGCAT_HINT}` for agent-side errors."


class InjectionError(RuntimeError):
    """Injecting or connecting failed. ``hint`` is the next step to take, or
    ``None`` when the message already says it."""

    def __init__(self, message: str, hint: Optional[str] = None) -> None:
        super().__init__(message)
        self.hint = hint


def _restart_hint(serial: str, package: str) -> str:
    return (f"Restart the app: `adb -s {serial} shell am force-stop {package}`, launch it "
            f"again, then retry.")


def _launch_hint(serial: str, package: str) -> str:
    return (f"Launch it, e.g. `adb -s {serial} shell monkey -p {package} "
            f"-c android.intent.category.LAUNCHER 1`, then retry.")


FROZEN_HINT = "Bring the app to the foreground, then retry."


def frozen_note(serial: Optional[str], package: str) -> Optional[str]:
    """Why a request to ``package`` may have gone unanswered: a sentence saying
    the app is frozen in the background, or ``None``. Best-effort (for errors)."""
    try:
        serial = adb.resolve_serial(serial)
        pid = adb.pidof(serial, package)
    except Exception:  # noqa: BLE001 - diagnostic only
        return None
    return _frozen_message(serial, package, pid) if pid is not None else None


def _frozen_message(serial: str, package: str, pid: int) -> Optional[str]:
    """A sentence explaining that ``pid`` is frozen, or ``None`` if it isn't
    (or the state can't be read)."""
    if not adb.process_frozen(serial, pid):
        return None
    app = f"'{package}' (pid {pid})" if package else f"pid {pid}"
    return (f"{app} is frozen: Android freezes apps in the background, and a frozen app "
            f"runs nothing, the agent included, until it is in the foreground again.")


def _artifact_path(build_out: str, name: str) -> str:
    path = os.path.join(build_out, name)
    if not os.path.isfile(path):
        raise InjectionError(
            f"required artifact not found: {path}\n"
            f"Run scripts/build.sh to produce {NATIVE_SO_NAME}, "
            f"{BOOTSTRAP_DEX_NAME} and {PAYLOAD_JAR_NAME} into build-out/, then "
            f"point Inspector Widget at that directory with --build-out DIR or "
            f"{ARTIFACTS_ENV}=DIR (needed when running from an installed wheel)."
        )
    return path


def _connect_forward(serial: str, socket_name: str,
                     connect_timeout: float = 5.0) -> Tuple[socket.socket, int]:
    """Forward a local port (adb picks it) to the agent's socket and connect.

    Returns ``(socket, local_port)``; on failure the forward is removed again.
    The Client sets a per-request deadline on the socket before each request.
    """
    port = adb.forward(serial, 0, socket_name)
    try:
        return socket.create_connection(("127.0.0.1", port), timeout=connect_timeout), port
    except BaseException:
        adb.remove_forward(serial, port)
        raise


def _try_warm_connect(serial: str, pid: int, package: str = "") -> Optional[Injection]:
    """Attempt to connect to an already-running agent for ``pid``.

    Returns an :class:`Injection` if the abstract socket exists and a Hello
    round-trips; ``None`` if nothing is listening (any partial forward is
    cleaned up). An agent that accepts but doesn't answer Hello in time raises
    :class:`InjectionError`: re-injecting can't help, because the new payload
    can't bind a name the wedged one still holds.
    """
    socket_name = socket_name_for_pid(pid)
    if not adb.socket_exists(serial, socket_name):
        return None
    try:
        sock, local_port = _connect_forward(serial, socket_name)
    except OSError:
        return None
    # PING via Hello to confirm the agent is live and speaks our protocol.
    client = Client(sock, owns_socket=False)
    ok = False
    try:
        hello = client.hello()
        ok = True
    except AgentTimeoutError as exc:
        frozen = _frozen_message(serial, package, pid)
        if frozen:
            raise InjectionError(
                f"the agent on @{socket_name} did not answer Hello: {frozen}",
                hint=FROZEN_HINT) from exc
        raise InjectionError(
            f"an agent holds @{socket_name} but did not answer Hello ({exc}). Either the "
            f"app is frozen (a breakpoint, an ANR) or the agent is busy with another "
            f"client's long request.",
            hint=(f"Retry in a moment; if it keeps failing, resume the app or restart it "
                  f"(`adb -s {serial} shell am force-stop {package}`, then launch it)."),
        ) from exc
    except Exception:
        return None
    finally:
        if not ok:  # also on KeyboardInterrupt / SystemExit: never leak the forward
            _shutdown_and_close(sock)
            adb.remove_forward(serial, local_port)
    return Injection(
        serial=serial,
        package=package,
        pid=pid,
        socket_name=socket_name,
        local_port=local_port,
        sock=sock,
        warm=True,
        hello=hello,
    )


def connect_existing(serial: Optional[str], package: str) -> Optional[Injection]:
    """Connect to an agent already running in ``package``, never injecting.

    Returns ``None`` when the app isn't running or has no agent. Used by
    ``detach`` so shutting an agent down never injects one first.
    """
    serial = adb.resolve_serial(serial)
    pid = adb.pidof(serial, package)
    if pid is None:
        return None
    return _try_warm_connect(serial, pid, package)


# How long to wait for a stopped agent's abstract socket to disappear.
STOP_WAIT = 5.0


def stop_agent(injection: Injection, wait: Optional[float] = None) -> bool:
    """Send SHUTDOWN over ``injection``, close it, and wait for the agent's
    abstract socket to disappear. Returns True once nothing listens on it."""
    wait = STOP_WAIT if wait is None else wait
    try:
        Client(injection.sock, owns_socket=False).shutdown(timeout=wait if wait > 0 else None)
    except TransportError:
        pass  # unanswered or undeliverable: the socket check below decides
    finally:
        injection.close()
    return _wait_for_socket_gone(injection.serial, injection.socket_name, wait)


def _wait_for_socket_gone(serial: str, socket_name: str, wait: float) -> bool:
    """Poll /proc/net/unix until nothing listens on ``socket_name`` (True) or
    ``wait`` passes (False). Other clients' connections to the name don't count.

    Agents built before the accept fix close their LocalServerSocket on stop,
    but a thread blocked in accept() keeps the socket bound until one more
    connection arrives. So if the name is still there, connect once to let the
    old accept loop see it has stopped.
    """
    end = time.monotonic() + wait
    delay = 0.05
    kicked = False
    while True:
        if not adb.socket_exists(serial, socket_name):
            return True
        if not kicked:
            kicked = True
            _kick(serial, socket_name)
            continue
        if time.monotonic() >= end:
            return False
        time.sleep(delay)
        delay = min(delay * 2, 0.5)


def _kick(serial: str, socket_name: str) -> None:
    """Open and close one connection to ``socket_name`` (best-effort)."""
    try:
        sock, local_port = _connect_forward(serial, socket_name, connect_timeout=2.0)
    except OSError:
        return
    try:
        with sock:
            sock.settimeout(0.5)
            try:
                sock.recv(1)  # the stopped server accepts and drops it: EOF at once
            except OSError:
                pass
    finally:
        adb.remove_forward(serial, local_port)


def _check_debuggable(serial: str, package: str) -> None:
    """``run-as <pkg> true`` before pushing anything: a release build fails here
    with one clear line instead of after three pushes with a raw adb dump."""
    ok, said = adb.run_as_probe(serial, package)
    if not ok:
        detail = said.splitlines()[0]
        raise InjectionError(
            f"package '{package}' is not debuggable, so the agent can't be injected "
            f"(run-as said: {detail}).",
            hint="Install a debug build of the app (android:debuggable=true), then retry.",
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
                     max_attempts: int = 15, initial_delay: float = 0.1,
                     package: str = "", pid: Optional[int] = None) -> None:
    """Poll /proc/net/unix until the agent listens on its abstract socket.

    Exponential backoff capped at 1s (mirrors ui-inspector waitForAgentSocket).
    """
    delay = initial_delay
    for _ in range(max_attempts):
        if adb.socket_exists(serial, socket_name):
            return
        time.sleep(delay)
        delay = min(delay * 2, 1.0)
    frozen = _frozen_message(serial, package, pid) if pid is not None else None
    if frozen:
        raise InjectionError(
            f"timed out waiting for agent socket '{socket_name}': {frozen} The attach is "
            f"queued and runs when the app next wakes.",
            hint=FROZEN_HINT,
        )
    raise InjectionError(f"timed out waiting for agent socket '{socket_name}'.",
                         hint=AGENT_LOG_HINT)


def inject_and_connect(
    serial: Optional[str] = None,
    package: str = "com.oberkfell.a11yprobe",
    build_out: Optional[str] = None,
    force_reinject: bool = False,
) -> Injection:
    """Full inject + connect, returning a connected :class:`Injection`.

    ``serial`` ``None`` picks ``$ANDROID_SERIAL`` or the only attached device
    (:func:`adb.resolve_serial`). ``build_out`` is the artifacts directory;
    ``None`` resolves it via :func:`resolve_build_out` (env vars, then the
    repo's ``build-out/``).

    Sequence (CONTRACT.md section 2):
      1. check the device, resolve pid (app must be running)
      2. warm path: if the agent socket already exists and Hello succeeds, reuse
         it when it runs the local build (:func:`build_matches`); otherwise, or
         with ``force_reinject``, SHUTDOWN it and wait for its socket to go
      3. check the app is debuggable, then push .so + bootstrap.dex + payload.jar
         to /data/local/tmp
      4. run-as cp into the app private dir (.so 700, dex/jar 444)
      5. cmd activity attach-agent <pkg> <so>=<bootstrap>:<payload>:viewspector_<pid>
      6. wait for the abstract socket, adb forward, connect, Hello, and check
         the agent that answers runs the build just pushed
    """
    serial = adb.resolve_serial(serial)
    pid = adb.pidof(serial, package)
    if pid is None:
        raise InjectionError(f"package '{package}' is not running on {serial}.",
                             hint=_launch_hint(serial, package))

    # Enable attribute resolution-stack tracking, exactly as Android Studio's
    # Layout Inspector does. Best-effort: stacks only populate for views inflated
    # while this global is set, so a freshly-launched app benefits most.
    try:
        adb.shell(serial, "settings put global debug_view_attributes 1")
    except Exception:
        pass

    build_dir = resolve_build_out(build_out)
    want = local_build_id(build_dir)
    socket_name = socket_name_for_pid(pid)
    warm = _try_warm_connect(serial, pid, package)
    if warm is not None:
        if not force_reinject and build_matches(warm.build_id, want):
            return warm
        if not force_reinject:
            # The agent runs another build (a rebuild, a second checkout, or an
            # agent from before the handshake). Replacing it would cut off any
            # other client using it (an MCP server, say) and, with two builds
            # in use, every call would evict the other's agent. So only replace
            # it when nobody else is connected; otherwise keep it and say so.
            others = adb.socket_connections(serial, socket_name) - 1
            if others > 0:
                warm.note = (
                    f"the running agent is build {_short(warm.build_id)} but the local "
                    f"payload.jar is {_short(want)}; kept it because {others} other "
                    f"client(s) are connected to it (an MCP server, say). Attach with "
                    f"--force (MCP: force=true) to replace it for everyone.")
                return warm
        # --force, or a stale agent nobody else uses: stop it so the new payload
        # can bind the name.
        if not stop_agent(warm):
            raise InjectionError(
                f"the running agent on @{socket_name} did not stop within {STOP_WAIT:g}s of "
                f"SHUTDOWN, so a new one can't bind the name. It may still be finishing "
                f"another client's request.",
                hint=f"Retry in a moment; if it keeps failing: {_restart_hint(serial, package)}",
            )

    _check_debuggable(serial, package)
    frozen = _frozen_message(serial, package, pid)
    if frozen:
        # attach-agent would only be queued until the app wakes, and then start
        # an agent nobody is waiting for.
        raise InjectionError(frozen, hint=FROZEN_HINT)
    app_so, app_boot, app_payload = _push_and_stage(serial, package, build_dir)

    # Native agent option string: bootstrapDexPath:payloadPath:socketName.
    # The native Agent_OnAttach reads everything after '=' and splits on ':'.
    options = f"{app_boot}:{app_payload}:{socket_name}"
    adb.attach_agent(serial, package, app_so, options)

    _wait_for_socket(serial, socket_name, package=package, pid=pid)

    # Give the LocalServerSocket a brief moment to start accept()-ing.
    last_err: Optional[Exception] = None
    sock: Optional[socket.socket] = None
    local_port = 0
    for attempt in range(10):
        try:
            sock, local_port = _connect_forward(serial, socket_name)
            break
        except OSError as e:
            last_err = e
            time.sleep(0.2)
    if sock is None:
        raise InjectionError(
            f"could not connect to agent socket '{socket_name}': {last_err}",
            hint=AGENT_LOG_HINT,
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
        inj.hello = client.hello()
    except BaseException as e:  # KeyboardInterrupt / SystemExit too: never leak the forward
        inj.close()
        if not isinstance(e, Exception):
            raise
        raise InjectionError(f"agent attached but Hello failed: {e}",
                             hint=AGENT_LOG_HINT) from e
    if not build_matches(inj.build_id, want):
        # The payload we just pushed didn't bind the socket (another agent still
        # holds it, e.g. one we couldn't reach to stop), so this is not our agent.
        inj.close()
        raise InjectionError(
            f"the agent answering on @{socket_name} is not the one just injected (it runs "
            f"build {inj.build_id or 'from before the build handshake'}, build-out has "
            f"{want}).",
            hint=_restart_hint(serial, package),
        )
    return inj


def _short(build_id: Optional[str]) -> str:
    return build_id[:12] if build_id else "(from before the build handshake)"
