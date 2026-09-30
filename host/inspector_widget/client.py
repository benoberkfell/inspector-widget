"""Synchronous request/response client over a framed ViewSpector socket.

One Request is in flight at a time; the matching Response (same monotonic id) is
read back immediately, mirroring the ui-inspector CommandSender. Convenience
methods build the proper proto Request envelopes and unwrap the typed Response.

Every request has a deadline (:func:`request_timeout`), so a frozen app can't
hang the host. Any transport failure (EOF, reset, timeout, a reply that doesn't
match the request) leaves the byte stream in an unknown state, so the Client
closes its socket and refuses further requests: the caller reconnects instead
of reading someone else's reply forever.
"""

from __future__ import annotations

import itertools
import logging
import os
import select
import socket
import threading
import time
from typing import Any, Dict, Optional

from google.protobuf.message import DecodeError

from . import framing
from .proto import view_inspection_pb2 as pb

log = logging.getLogger(__name__)

# Where to look when the agent misbehaves. The tag MUST match the agent's
# logcat TAG ("ViewSpector", AGENTS.md section 1).
LOGCAT_HINT = "adb logcat -s ViewSpector"

# --------------------------------------------------------------------------- #
# Deadlines
# --------------------------------------------------------------------------- #
TIMEOUT_ENV = "INSPECTOR_WIDGET_TIMEOUT"
LEGACY_TIMEOUT_ENV = "VIEWSPECTOR_TIMEOUT"
DEFAULT_TIMEOUT = 30.0
# Commands that capture pixels, walk every Compose/a11y node or hot-reload get
# this multiple of the base deadline.
SLOW_FACTOR = 4.0
_SLOW_COMMANDS = frozenset({"screenshot", "capture_skp", "dump_compose", "dump_a11y"})
# Hello never touches the main thread; cap it so a wedged agent is noticed fast.
HELLO_TIMEOUT = 10.0


def base_timeout() -> Optional[float]:
    """The per-request deadline in seconds: ``$INSPECTOR_WIDGET_TIMEOUT`` (legacy
    ``$VIEWSPECTOR_TIMEOUT``), default 30. ``0`` or a negative value disables it
    (useful while the app sits at a debugger breakpoint)."""
    raw = os.environ.get(TIMEOUT_ENV) or os.environ.get(LEGACY_TIMEOUT_ENV)
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        log.warning("ignoring %s=%r (not a number); using %ss", TIMEOUT_ENV, raw, DEFAULT_TIMEOUT)
        return DEFAULT_TIMEOUT
    return value if value > 0 else None


def request_timeout(request: "pb.Request", base: Optional[float] = None) -> Optional[float]:
    """The deadline in seconds for ``request`` (``None`` = no deadline).

    ``base`` defaults to :func:`base_timeout`. Hello is capped at
    ``HELLO_TIMEOUT``; a command that captures pixels, walks the Compose or
    accessibility tree, or inlines every property gets ``SLOW_FACTOR`` x base.
    An ``a11y_focus`` long-poll gets its own wait (``wait_ms`` + ``quiet_ms``)
    on top of the base, so the base stays the margin for a frozen app.
    """
    base = base_timeout() if base is None else base
    if base is None or base <= 0:
        return None
    command = request.WhichOneof("command")
    if command == "hello":
        return min(base, HELLO_TIMEOUT)
    if command == "a11y_focus":
        cmd = request.a11y_focus
        return base + (max(0, cmd.wait_ms) + max(0, cmd.quiet_ms)) / 1000.0
    slow = command in _SLOW_COMMANDS or (
        command == "dump_tree"
        and (request.dump_tree.include_screenshot or request.dump_tree.include_properties))
    return base * SLOW_FACTOR if slow else base


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class ClientError(RuntimeError):
    """Raised when the agent returns Response.status == ERROR."""


class TransportError(ConnectionError):
    """The connection to the agent failed; the Client that raised it is closed.

    ``hint`` is a one-line pointer for the user (where to look next).
    """

    hint = (f"Re-attach to continue; if it keeps failing, check `{LOGCAT_HINT}` "
            f"for agent-side errors.")


class SessionLostError(TransportError):
    """The agent went away mid-session (EOF, reset, or the stream fell out of
    sync). Reconnecting usually fixes it: the payload's idle watchdog stopped
    the server, the app restarted, or another client sent SHUTDOWN."""


class NotSentError(SessionLostError):
    """The request was never delivered: the client was already closed, or the
    write failed. Unlike a lost reply, the agent certainly didn't act on it."""


class AgentTimeoutError(TransportError, TimeoutError):
    """The agent did not reply before the deadline (a frozen main thread, a
    breakpoint, an ANR). Retrying straight away would only wait again."""

    hint = (f"If the app is just slow, raise the deadline: set {TIMEOUT_ENV}=<seconds> "
            f"(0 disables it) in the environment of the CLI, or of the MCP server process "
            f"(its launch config; the server reads it at each request). Otherwise check "
            f"whether the app is frozen, and `{LOGCAT_HINT}`.")


_LOST = "agent session lost (idle timeout, app restart, or the agent was shut down)"


def node_action(action: Any) -> int:
    """A ``pb.NodeAction`` value from itself or its name, case-insensitive, with or
    without the ``NODE_ACTION_`` prefix (``"click"``, ``"ACCESSIBILITY_FOCUS"``)."""
    if isinstance(action, int):
        return action
    name = str(action).strip().upper().replace("-", "_")
    if not name.startswith("NODE_ACTION_"):
        name = "NODE_ACTION_" + name
    try:
        return pb.NodeAction.Value(name)
    except ValueError:
        choices = ", ".join(n[len("NODE_ACTION_"):].lower() for n in pb.NodeAction.keys()
                            if n != "NODE_ACTION_UNSPECIFIED")
        raise ValueError(f"unknown node action {action!r}; one of: {choices}") from None


class Client:
    """Wraps a connected socket and speaks framed protobuf to the agent.

    ``timeout`` overrides the per-request base deadline in seconds (``0``
    disables it); ``None`` reads :func:`base_timeout` at each request.
    """

    def __init__(self, sock: socket.socket, owns_socket: bool = True,
                 timeout: Optional[float] = None):
        self._sock = sock
        self._owns_socket = owns_socket
        self._timeout = timeout
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._broken: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Health
    # ------------------------------------------------------------------ #
    @property
    def broken(self) -> Optional[str]:
        """Why this client stopped working, or ``None`` while it is usable."""
        return self._broken

    def is_open(self) -> bool:
        """True while the connection looks usable, checked without a round trip.

        An idle connection has nothing to read. A readable one means the agent
        hung up (EOF) or sent bytes nobody asked for (a desync); either way the
        client is closed and this returns False.
        """
        if self._broken:
            return False
        if not self._lock.acquire(blocking=False):
            return True  # a request is in flight on another thread
        try:
            try:
                readable, _, _ = select.select([self._sock], [], [], 0)
            except (OSError, ValueError):
                self._poison("the socket is closed")
                return False
            if not readable:
                return True
            try:
                self._sock.settimeout(0)
                peek = self._sock.recv(1, socket.MSG_PEEK)
            except (BlockingIOError, InterruptedError):
                return True
            except OSError as exc:
                self._poison(f"connection error ({exc})")
                return False
            self._poison("the agent closed the connection" if not peek
                         else "unexpected bytes from the agent while idle (stream out of sync)")
            return False
        finally:
            self._lock.release()

    def _poison(self, reason: str) -> None:
        """Mark the client unusable and close the socket (whoever owns it).

        Only for the thread that holds the request lock (or when no request can
        be in flight); from any other thread use :meth:`abort`.
        """
        if self._broken is None:
            self._broken = reason
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._close_socket()

    def _close_socket(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def abort(self, reason: str = "disconnected") -> bool:
        """Mark the client unusable and end the connection, from any thread.

        A request in flight on another thread fails at once with
        :class:`SessionLostError`: shutting the socket down wakes its read, and
        that thread closes the socket on its way out. (Closing it from here
        instead can leave that thread polling a closed descriptor until its
        deadline.) Returns True if the socket was closed here, False if the
        in-flight request's thread will close it.
        """
        if self._broken is None:
            self._broken = reason
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        if not self._lock.acquire(blocking=False):
            return False
        try:
            self._close_socket()
        finally:
            self._lock.release()
        return True

    # ------------------------------------------------------------------ #
    # Core send/recv
    # ------------------------------------------------------------------ #
    def send(self, request: "pb.Request", timeout: Optional[float] = None) -> "pb.Response":
        """Assign a monotonic id, send the request, read and validate the response.

        Raises :class:`ClientError` for an agent ERROR reply (the connection
        stays usable), :class:`AgentTimeoutError` when the deadline passes and
        :class:`SessionLostError` for any other transport failure; those two
        close the client. A request that never left the host (the client was
        already closed, or the write failed) raises :class:`NotSentError`, a
        :class:`SessionLostError`. ``timeout`` overrides the base deadline for
        this call.
        """
        with self._lock:
            try:
                return self._send_locked(request, timeout)
            finally:
                if self._broken:
                    # Closed from elsewhere (abort) while this request ran: the
                    # socket is ours to close now.
                    self._close_socket()

    def _send_locked(self, request: "pb.Request", timeout: Optional[float]) -> "pb.Response":
        """The body of :meth:`send`; the caller holds ``self._lock``."""
        command = request.WhichOneof("command") or "<unset>"
        if self._broken:
            raise NotSentError(f"{_LOST}: {self._broken}")
        req_id = next(self._ids)
        request.id = req_id
        limit = request_timeout(request, timeout if timeout is not None else self._timeout)
        deadline = None if limit is None else time.monotonic() + limit
        try:
            self._sock.settimeout(limit)
            framing.write_message(self._sock, request.SerializeToString())
        except socket.timeout as exc:
            self._poison(f"could not send {command} within {limit:g}s")
            raise AgentTimeoutError(
                f"the agent did not take {command} within {limit:g}s (is the app's main "
                f"thread frozen?); the connection was closed") from exc
        except OSError as exc:
            self._poison(f"{type(exc).__name__}: {exc}")
            raise NotSentError(f"{_LOST}: could not send {command} ({exc})") from exc
        try:
            raw = framing.read_message(self._sock, deadline=deadline)
        except (socket.timeout, framing.FrameTimeout) as exc:
            self._poison(f"no reply to {command} within {limit:g}s")
            raise AgentTimeoutError(
                f"the agent did not reply to {command} within {limit:g}s (is the app's "
                f"main thread frozen?); the connection was closed"
            ) from exc
        except (OSError, framing.FramingError) as exc:
            self._poison(f"{type(exc).__name__}: {exc}")
            raise SessionLostError(f"{_LOST}: {exc}") from exc
        response = pb.Response()
        try:
            response.ParseFromString(raw)
        except DecodeError as exc:
            self._poison(f"undecodable reply to {command}")
            raise SessionLostError(f"{_LOST}: undecodable reply ({exc})") from exc
        if response.id != req_id:
            if response.id == 0 and response.status == pb.Response.ERROR:
                # The agent couldn't parse the request, so it answered with
                # id 0; one reply per frame means the stream is still in step.
                raise ClientError(f"agent error (request id {req_id}): {response.error}")
            self._poison(f"reply id {response.id} for request {req_id}")
            raise SessionLostError(
                f"{_LOST}: response id mismatch: expected {req_id}, got {response.id} "
                f"(the stream is out of sync)")
        if response.status == pb.Response.ERROR:
            raise ClientError(
                f"agent error (request id {req_id}): {response.error}"
            )
        return response

    # ------------------------------------------------------------------ #
    # Convenience commands
    # ------------------------------------------------------------------ #
    def hello(self, timeout: Optional[float] = None) -> "pb.HelloResponse":
        req = pb.Request()
        req.hello.SetInParent()
        return self.send(req, timeout=timeout).hello

    def get_windows(self) -> "pb.GetWindowsResponse":
        req = pb.Request()
        req.get_windows.SetInParent()
        return self.send(req).get_windows

    def dump_tree(
        self,
        root_id: int = 0,
        properties: bool = False,
        resolution_stack: bool = False,
        screenshot: bool = False,
        scale: float = 1.0,
    ) -> "pb.DumpTreeResponse":
        req = pb.Request()
        cmd = req.dump_tree
        cmd.root_id = root_id
        cmd.include_properties = properties
        # A resolution stack only makes sense when properties are included.
        cmd.include_resolution_stack = resolution_stack and properties
        cmd.include_screenshot = screenshot
        cmd.screenshot_scale = scale
        return self.send(req).dump_tree

    def get_properties(
        self, view_id: int, resolution_stack: bool = False
    ) -> "pb.GetPropertiesResponse":
        req = pb.Request()
        cmd = req.get_properties
        cmd.view_id = view_id
        cmd.include_resolution_stack = resolution_stack
        return self.send(req).get_properties

    def screenshot(self, root_id: int = 0, scale: float = 1.0) -> "pb.ScreenshotResponse":
        req = pb.Request()
        cmd = req.screenshot
        cmd.root_id = root_id
        cmd.scale = scale
        return self.send(req).screenshot

    def dump_compose(
        self,
        root_view_id: int = 0,
        include_semantics: bool = True,
        include_slot_table: bool = True,
        enable_inspection: bool = False,
    ) -> "pb.DumpComposeResponse":
        req = pb.Request()
        cmd = req.dump_compose
        cmd.root_view_id = root_view_id
        cmd.include_semantics = include_semantics
        cmd.include_slot_table = include_slot_table
        cmd.enable_inspection = enable_inspection
        return self.send(req).dump_compose


    def dump_a11y(
        self,
        root_id: int = 0,
        include_extras: bool = True,
        include_rendering_info: bool = False,
    ) -> "pb.DumpA11yResponse":
        """Dump the unified AccessibilityNodeInfo tree (Views + Compose virtual nodes).

        ``root_id`` 0 means all window roots. ``include_extras`` iterates each
        node's getExtras() bundle (roleDescription, compose testTag/id, ...).
        ``include_rendering_info`` triggers a per-node refreshWithExtraData round
        trip (costly; off by default).
        """
        req = pb.Request()
        cmd = req.dump_a11y
        cmd.root_id = root_id
        cmd.include_extras = include_extras
        cmd.include_rendering_info = include_rendering_info
        return self.send(req).dump_a11y

    def a11y_focus(
        self,
        after_seq: int = 0,
        wait_ms: int = 0,
        quiet_ms: int = 0,
        include_input_focus: bool = False,
        subtree_depth: int = 0,
        max_events: int = 0,
    ) -> "pb.A11yFocusResponse":
        """Where accessibility focus is, plus the accessibility events recorded since ``after_seq``.

        ``wait_ms`` > 0 long-polls: the agent answers once a TYPE_VIEW_ACCESSIBILITY_FOCUSED
        event newer than ``after_seq`` was recorded and ``quiet_ms`` then passed without any
        event, or when ``wait_ms`` runs out (``timed_out``). Pass the response's ``seq`` as the
        next call's ``after_seq``. The deadline is the base deadline plus the wait.
        """
        req = pb.Request()
        cmd = req.a11y_focus
        cmd.after_seq = after_seq
        cmd.wait_ms = wait_ms
        cmd.quiet_ms = quiet_ms
        cmd.include_input_focus = include_input_focus
        cmd.subtree_depth = subtree_depth
        cmd.max_events = max_events
        return self.send(req).a11y_focus

    def a11y_act(
        self,
        host_view_id: int,
        virtual_id: int = -1,
        action: Any = "accessibility_focus",
        raw_action_id: int = 0,
        args: Optional[Dict[str, Any]] = None,
        subtree_depth: int = 0,
    ) -> "pb.A11yActResponse":
        """Perform one accessibility action on the node ``(host_view_id, virtual_id)``.

        ``action`` is a ``pb.NodeAction`` value or its name, with or without the
        ``NODE_ACTION_`` prefix (``"accessibility_focus"``, ``"click"``, ...); ``"raw"``
        sends ``raw_action_id``. ``args`` become the action's Bundle (str / int / bool /
        float values). A refusal (e.g. accessibility focus with TalkBack off) comes back
        with ``performed`` False and ``error`` set, not as an exception.
        """
        req = pb.Request()
        cmd = req.a11y_act
        cmd.host_view_id = host_view_id
        cmd.virtual_id = virtual_id
        cmd.action = node_action(action)
        cmd.raw_action_id = raw_action_id
        cmd.subtree_depth = subtree_depth
        for key, value in (args or {}).items():
            arg = cmd.args.add(key=key)
            if isinstance(value, bool):
                arg.bool_value = value
            elif isinstance(value, int):
                arg.int_value = value
            elif isinstance(value, float):
                arg.float_value = value
            else:
                arg.string_value = str(value)
        return self.send(req).a11y_act

    def capture_skp(self, root_id: int = 0) -> "pb.CaptureSkpResponse":
        req = pb.Request()
        req.capture_skp.root_id = root_id
        return self.send(req).capture_skp

    def shutdown(self, timeout: Optional[float] = None) -> "pb.ShutdownResponse":
        """Ask the agent to stop, for every client (not just this connection).

        Agents built before the reply fix close the connection before writing
        the reply, so EOF after the request went out is taken as delivered;
        only the agent's socket going away proves it stopped (see
        ``inject.stop_agent``). The client is closed afterwards either way.
        Raises :class:`NotSentError` if the request never went out (the client
        was already closed, possibly while waiting behind another request) and
        :class:`AgentTimeoutError` if the agent never answered.
        """
        req = pb.Request()
        req.shutdown.SetInParent()
        try:
            return self.send(req, timeout=timeout).shutdown
        except NotSentError:
            raise
        except SessionLostError:
            return pb.ShutdownResponse()
        finally:
            self._poison("shut down")

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        if self._owns_socket:
            self._poison("closed")

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
