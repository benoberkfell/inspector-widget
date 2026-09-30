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
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

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


class AgentStartupError(InjectionError):
    """The agent was attached but did not start, and the app's logcat says why.

    Read from logcat while waiting for the agent's socket, so it is reported
    within a poll or two of the failure rather than at the socket timeout.

    ``kind`` names the failure: ``classpath_shadowing`` (the payload linked
    against the app's own, shrunk copy of a library class), ``payload_start``
    (``Payload.start`` threw), ``bootstrap`` (the bootstrap dex could not load
    the payload), ``native`` (the JVMTI agent failed), ``library_load`` (the app
    could not load libviewspector.so), ``bind`` (another server holds the
    socket name), ``crash`` (the app died) or ``unknown`` (an agent error with
    no socket after a grace period). ``cause`` is the root exception line
    (e.g. ``java.lang.NoSuchMethodError: ...``) when there is one, ``log`` the
    logcat lines it came from. ``cacheable`` failures recur on every retry into
    the same app process with the same agent build, so :func:`inject_and_connect`
    reports them again without re-injecting.
    """

    def __init__(self, message: str, hint: Optional[str] = None, *, kind: str = "unknown",
                 cause: Optional[str] = None, log: Sequence[str] = (),
                 cacheable: bool = False) -> None:
        super().__init__(message, hint)
        self.kind = kind
        self.cause = cause
        self.log = list(log)
        self.cacheable = cacheable
        self.cached = False  # True on a copy returned from the failure cache
        self.first_seen = time.time()

    def repeated(self, pid: int) -> "AgentStartupError":
        """This failure, reported again for a retry that was not re-injected."""
        ago = max(0, int(time.time() - self.first_seen))
        again = AgentStartupError(
            f"{self} [not re-injected: this app process (pid {pid}) failed this way with the "
            f"same agent build {ago}s ago, and would again]",
            hint=self.hint, kind=self.kind, cause=self.cause, log=self.log, cacheable=self.cacheable)
        again.cached = True
        again.first_seen = self.first_seen
        return again


# Appended to the hint of a failure that recurs on every retry (``cacheable``).
# Only a long-lived host (the MCP server, a library Session) remembers it; each
# CLI run is a new process and injects again.
_RETRY_NOTE = ("Retrying the same app process with the same agent build fails the same way: "
               "the MCP server reports it again without re-injecting until the app restarts or "
               "build-out changes (attach with --force, MCP force=true, to inject anyway).")


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


# --------------------------------------------------------------------------- #
# Start-up failure detection. When the agent can't start, the app says why in
# logcat within milliseconds of the attach: the agent under its tag, a crash
# under AndroidRuntime, a library the runtime couldn't load under
# ActivityThread. The socket wait reads those lines as it polls, so a failed
# injection is reported at once and with its cause instead of at the timeout.
# --------------------------------------------------------------------------- #
_AGENT_TAG = "ViewSpector"  # the agent's logcat tag (codename; AGENTS.md section 1)
_AGENT_PACKAGE = "com.oberkfell.viewspector"  # its classes, as they appear in stack traces
# What each missed socket poll reads from the app's logcat.
STARTUP_LOG_SPECS = (f"{_AGENT_TAG}:E", "AndroidRuntime:E", "ActivityThread:E")
# An agent error this module doesn't recognise fails the wait only if the socket
# is still missing this long (seconds) after the error was first seen.
UNKNOWN_ERROR_GRACE = 1.5

# The first line of an E/ViewSpector message that means the agent will not
# serve, and the kind of failure it is.
_FATAL_AGENT_LINES = (
    (re.compile(r"^initialize: error invoking"), "payload_start"),
    (re.compile(r"server thread crashed"), "payload_start"),
    (re.compile(r"^initialize: "), "bootstrap"),  # every other initialize error aborts it
    (re.compile(r"^(Cannot bind @|Failed to bind LocalServerSocket)"), "bind"),
    (re.compile(r"^(Agent options|GetEnv\(|JVMTI error|AttachCurrentThread failed|"
                r"Pending JNI exception|Could not find|Failed to build Java string|"
                r"Bootstrap\.initialize threw)"), "native"),
)
# Errors that name a class the payload linked against but found without the
# member (or shape) it was compiled against.
_LINKAGE_ERRORS = frozenset({
    "java.lang.NoSuchMethodError", "java.lang.NoSuchFieldError", "java.lang.NoClassDefFoundError",
    "java.lang.IncompatibleClassChangeError", "java.lang.AbstractMethodError",
    "java.lang.IllegalAccessError", "java.lang.VerifyError",
})
# Libraries both the payload and a typical app bring, which a shrunk app copy
# can shadow.
_SHARED_LIBRARIES = (("kotlin.", "Kotlin"), ("kotlinx.", "Kotlin"),
                     ("com.google.protobuf.", "protobuf"))
_DECLARED_IN = re.compile(r"\s*\(declaration of '([\w.$]+)' appears in ([^)]+)\)")
_CLASS_REF = re.compile(r"(?:in class |Failed resolution of: )L([\w/$]+);")
_MAX_LOG_LINES = 40


def _clip(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


@dataclass
class _LogRecord:
    """One logged message: logcat prints a multi-line message (a stack trace)
    as lines sharing the timestamp, thread, level and tag."""

    tag: str
    level: str
    tid: int
    time: float
    lines: List[str] = field(default_factory=list)

    @property
    def header(self) -> str:
        return self.lines[0] if self.lines else ""


def _records(entries: Sequence["adb.LogEntry"]) -> List[_LogRecord]:
    out: List[_LogRecord] = []
    for e in entries:
        last = out[-1] if out else None
        if last is not None and (last.time, last.tid, last.tag, last.level) == (
                e.time, e.tid, e.tag, e.level):
            last.lines.append(e.message)
        else:
            out.append(_LogRecord(e.tag, e.level, e.tid, e.time, [e.message]))
    return out


def _root_cause(lines: Sequence[str]) -> Optional[str]:
    """The innermost exception of a logged stack trace: the last ``Caused by:``,
    else the exception line that follows the message."""
    causes = [s.strip()[len("Caused by: "):] for s in lines if s.strip().startswith("Caused by: ")]
    if causes:
        return causes[-1]
    for s in lines[1:]:
        s = s.strip()
        if s and not s.startswith(("at ", "...", "Suppressed:", "Process: ")):
            return s
    return None


def _shadowed(cause: str) -> Optional[Tuple[str, str, Optional[str], Optional[str], str]]:
    """``(exception, class, library, apk, detail)`` when ``cause`` is a linkage
    error on a class the app itself provides (its APK is where the class was
    declared) or on a library the payload shares with apps (Kotlin, protobuf).
    ``apk`` is the app APK the class came from, when the error says."""
    exc, _, detail = cause.partition(": ")
    if exc not in _LINKAGE_ERRORS:
        return None
    declared = _DECLARED_IN.search(detail)
    if declared:
        cls, where = declared.group(1), declared.group(2).strip()
    else:
        ref = _CLASS_REF.search(detail)
        if ref is None:
            return None
        cls, where = ref.group(1).replace("/", "."), ""
    library = next((name for prefix, name in _SHARED_LIBRARIES if cls.startswith(prefix)), None)
    apk_match = re.search(r"([^/!]+\.apk)", where) if where.startswith("/data/app/") else None
    apk = apk_match.group(1) if apk_match else None
    if library is None and apk is None:
        return None
    return exc, cls, library, apk, _DECLARED_IN.sub("", detail).strip().rstrip(".")


def _agent_error(kind: str, record: _LogRecord, serial: str, package: str, pid: int,
                 socket_name: str) -> AgentStartupError:
    app = f"'{package}' (pid {pid})"
    cause = _root_cause(record.lines)
    log = record.lines[:_MAX_LOG_LINES]
    header = _clip(record.header)
    retry = _RETRY_NOTE
    if kind == "payload_start":
        shadow = _shadowed(cause) if cause else None
        if shadow is not None:
            exc, cls, library, apk, detail = shadow
            own = "the app's own copy" + (f" (in its {apk})" if apk else "")
            return AgentStartupError(
                f"the agent payload failed to start in {app}: {exc}: {_clip(detail)}. The "
                f"payload's {cls} {'resolved' if apk else 'probably resolved'} to {own}, which "
                f"R8/ProGuard shrank: the app's minified {library or 'library'} classes lack "
                f"members the payload uses.",
                hint=("Inspect a build of the app without code shrinking (isMinifyEnabled = "
                      "false, e.g. its debug variant). " + retry),
                kind="classpath_shadowing", cause=cause, log=log, cacheable=True)
        return AgentStartupError(
            f"the agent payload failed to start in {app}: {_clip(cause or header)}.",
            hint=f"The full stack trace is in `{LOGCAT_HINT}`. {retry}",
            kind="payload_start", cause=cause, log=log, cacheable=True)
    if kind == "bootstrap":
        # A missing app class loader can mean the app is still starting up.
        transient = "ClassLoader" in record.header
        return AgentStartupError(
            f"the agent's bootstrap could not load its payload in {app}: {header}"
            + (f" ({_clip(cause)})" if cause else ""),
            hint=(f"Retry once the app has finished starting. {AGENT_LOG_HINT}" if transient
                  else f"Re-run scripts/build.sh for a complete build-out. {retry}"),
            kind="bootstrap", cause=cause, log=log, cacheable=not transient)
    if kind == "native":
        return AgentStartupError(
            f"the native agent failed to install in {app}: {header}",
            hint=f"{AGENT_LOG_HINT} {retry}",
            kind="native", cause=cause, log=log, cacheable=True)
    if kind == "bind":
        return AgentStartupError(
            f"the agent started in {app} but could not listen on @{socket_name}: another agent "
            f"(an earlier injection) still holds the name, so the new payload will not serve.",
            hint=_restart_hint(serial, package), kind="bind", cause=cause, log=log)
    return AgentStartupError(
        f"the agent logged an error in {app} and has not opened @{socket_name}: {header}"
        + (f" ({_clip(cause)})" if cause and cause not in record.header else ""),
        hint=AGENT_LOG_HINT, kind="unknown", cause=cause, log=log)


def _crash_error(record: _LogRecord, serial: str, package: str, pid: int) -> AgentStartupError:
    cause = _root_cause(record.lines)
    ours = any(_AGENT_PACKAGE in line for line in record.lines)
    return AgentStartupError(
        f"'{package}' (pid {pid}) crashed while the agent was starting: {_clip(cause or '')}"
        + (f". The stack trace runs through the agent ({_AGENT_PACKAGE})." if ours else "."),
        hint=f"`adb -s {serial} logcat -b crash` has the full trace. {_launch_hint(serial, package)}",
        kind="crash", cause=cause, log=record.lines[:_MAX_LOG_LINES])


def _load_error(record: _LogRecord, package: str, pid: int) -> AgentStartupError:
    reason = record.header.partition(" failed: ")[2] or record.header
    return AgentStartupError(
        f"'{package}' (pid {pid}) could not load the agent library: {_clip(reason)}",
        hint=(f"{NATIVE_SO_NAME} must be built for the app process's ABI and loadable from the "
              f"app's data directory. {_RETRY_NOTE}"),
        kind="library_load", cause=reason, log=record.lines[:_MAX_LOG_LINES], cacheable=True)


@dataclass
class _Diagnosis:
    error: AgentStartupError
    fatal: bool  # False: fail only if the socket is still missing after the grace


def _diagnose(entries: Sequence["adb.LogEntry"], serial: str, package: str, pid: int,
              socket_name: str) -> Optional[_Diagnosis]:
    """What the app's error log since the attach says about the agent's start,
    or ``None`` if nothing yet."""
    unknown: Optional[_Diagnosis] = None
    load_failures: List[_LogRecord] = []
    for record in _records(entries):
        if record.tag == "AndroidRuntime" and "FATAL EXCEPTION" in record.header:
            return _Diagnosis(_crash_error(record, serial, package, pid), fatal=True)
        if record.tag == _AGENT_TAG:
            kind = next((k for rx, k in _FATAL_AGENT_LINES if rx.search(record.header)), None)
            if kind is not None:
                return _Diagnosis(_agent_error(kind, record, serial, package, pid, socket_name),
                                  fatal=True)
            if unknown is None:
                unknown = _Diagnosis(
                    _agent_error("unknown", record, serial, package, pid, socket_name), fatal=False)
        elif (record.tag == "ActivityThread" and "Attaching agent with" in record.header
              and NATIVE_SO_NAME in record.header):
            load_failures.append(record)
    if load_failures:
        # ActivityThread tries the app's class loader, then none: the library
        # failed to load once both attempts have failed.
        return _Diagnosis(_load_error(load_failures[-1], package, pid),
                          fatal=len(load_failures) >= 2)
    return unknown


def _wait_for_socket(serial: str, socket_name: str,
                     max_attempts: int = 15, initial_delay: float = 0.1,
                     package: str = "", pid: Optional[int] = None,
                     since: Optional[float] = None) -> None:
    """Poll /proc/net/unix until the agent listens on its abstract socket.

    Exponential backoff capped at 1s (mirrors ui-inspector waitForAgentSocket).
    With ``pid``, every missed poll also reads the app's error log since
    ``since`` (the device clock at the attach, :func:`adb.device_time`) and
    raises :class:`AgentStartupError` as soon as it shows the start failed.
    """
    delay = initial_delay
    pending: Optional[_Diagnosis] = None
    pending_at = 0.0
    for _ in range(max_attempts):
        if adb.socket_exists(serial, socket_name):
            return
        if pid is not None:
            found = _diagnose(adb.logcat(serial, pid=pid, since=since, specs=STARTUP_LOG_SPECS)
                              or [], serial, package, pid, socket_name)
            if found is not None:
                if found.fatal:
                    raise found.error
                if pending is None:
                    pending, pending_at = found, time.monotonic()
                elif time.monotonic() - pending_at >= UNKNOWN_ERROR_GRACE:
                    raise found.error
        time.sleep(delay)
        delay = min(delay * 2, 1.0)
    if pending is not None:
        raise pending.error
    raise _socket_timeout_error(serial, socket_name, package, pid, since)


def _socket_timeout_error(serial: str, socket_name: str, package: str, pid: Optional[int],
                          since: Optional[float]) -> InjectionError:
    """Why the agent's socket never appeared, as far as the device can tell."""
    frozen = _frozen_message(serial, package, pid) if pid is not None else None
    if frozen:
        return InjectionError(
            f"timed out waiting for agent socket '{socket_name}': {frozen} The attach is "
            f"queued and runs when the app next wakes.",
            hint=FROZEN_HINT,
        )
    timed_out = f"timed out waiting for agent socket '{socket_name}'"
    if pid is None:
        return InjectionError(f"{timed_out}.", hint=AGENT_LOG_HINT)
    app = f"'{package}' (pid {pid})"
    try:
        now = adb.pidof(serial, package) if package else pid
    except adb.AdbError:
        now = pid
    if now != pid:
        return InjectionError(
            f"{timed_out}: {app} exited while the agent was starting"
            + (f" (it runs again as pid {now})." if now else "."),
            hint=f"`adb -s {serial} logcat -b crash` says why. {_launch_hint(serial, package)}")
    entries = adb.logcat(serial, pid=pid, since=since, specs=(f"{_AGENT_TAG}:I", "*:W"))
    if entries is None:
        return InjectionError(f"{timed_out}.", hint=AGENT_LOG_HINT)
    agent = [e for e in entries if e.tag == _AGENT_TAG]
    if not agent:
        return InjectionError(
            f"{timed_out}: the agent never started in {app}; it has logged nothing since the "
            f"attach. The app's main thread runs the attach, so a blocked main thread (paused "
            f"at a debugger breakpoint, or not responding) holds it back.",
            hint=(f"Resume the app, or restart it (`adb -s {serial} shell am force-stop "
                  f"{package}`, then launch it), and retry; `{LOGCAT_HINT}` shows whether "
                  f"the agent starts."))
    others = [e for e in entries if e.tag != _AGENT_TAG and e.level in ("E", "F")]
    tail = [f"{e.level}/{e.tag}: {_clip(e.message, 200)}" for e in agent[-6:] + others[-3:]]
    return InjectionError(f"{timed_out} in {app}. Its last log lines since the attach:\n  "
                          + "\n  ".join(tail), hint=AGENT_LOG_HINT)


# --------------------------------------------------------------------------- #
# Failed-injection memory. A failure the agent reported (a shrunk app's Kotlin
# shadowing the payload, say) recurs on every retry into the same process with
# the same build, so a retry is answered from here instead of re-injecting and
# failing again: an agent retrying in a loop gets the cause at once.
# --------------------------------------------------------------------------- #
_FailureKey = Tuple[str, str, int, Optional[str]]
_FAILED: Dict[_FailureKey, AgentStartupError] = {}
_FAILED_LOCK = threading.Lock()


def failed_injection(serial: str, package: str, pid: int,
                     build_id: Optional[str]) -> Optional[AgentStartupError]:
    """The remembered start-up failure for this app process and agent build."""
    with _FAILED_LOCK:
        return _FAILED.get((serial, package, pid, build_id))


def forget_failed_injections() -> None:
    """Forget every remembered start-up failure (the next attach re-injects)."""
    with _FAILED_LOCK:
        _FAILED.clear()


def _drop_other_pids(key: _FailureKey) -> None:
    """The app now runs as ``key``'s pid: its earlier processes are gone and
    can't be retried. Call with the lock held."""
    for stale in [k for k in _FAILED if k[:2] == key[:2] and k[2] != key[2]]:
        del _FAILED[stale]


def _remember_failure(key: _FailureKey, error: AgentStartupError) -> None:
    with _FAILED_LOCK:
        _drop_other_pids(key)
        _FAILED[key] = error


def _forget_failure(key: _FailureKey) -> None:
    """An agent of this build serves this process: forget its failures."""
    with _FAILED_LOCK:
        _drop_other_pids(key)
        _FAILED.pop(key, None)


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
      3. if this app process already failed to start this build, raise that
         error again (:class:`AgentStartupError`, unless ``force_reinject``)
      4. check the app is debuggable and not frozen, then push .so +
         bootstrap.dex + payload.jar to /data/local/tmp
      5. run-as cp into the app private dir (.so 700, dex/jar 444)
      6. cmd activity attach-agent <pkg> <so>=<bootstrap>:<payload>:viewspector_<pid>
      7. wait for the abstract socket, reading the app's error log as it polls
         so a failed start raises :class:`AgentStartupError` at once (and is
         remembered for step 3); then adb forward, connect, Hello, and check
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
    failure_key: _FailureKey = (serial, package, pid, want)
    warm = _try_warm_connect(serial, pid, package)
    if warm is not None:
        if not force_reinject and build_matches(warm.build_id, want):
            _forget_failure(failure_key)
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
    if not force_reinject:
        # This process already failed to start this build: say so again rather
        # than injecting into it (and replacing any stale agent) for nothing.
        failed = failed_injection(serial, package, pid, want)
        if failed is not None:
            if warm is not None:
                warm.close()
            raise failed.repeated(pid)
    if warm is not None:
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
    # The device clock just before the attach: log lines older than this are
    # an earlier attempt's, not this one's.
    since = adb.device_time(serial)
    adb.attach_agent(serial, package, app_so, options)

    try:
        _wait_for_socket(serial, socket_name, package=package, pid=pid, since=since)
    except AgentStartupError as exc:
        if exc.cacheable:
            _remember_failure(failure_key, exc)
        raise

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
    _forget_failure(failure_key)
    return inj


def _short(build_id: Optional[str]) -> str:
    return build_id[:12] if build_id else "(from before the build handshake)"
