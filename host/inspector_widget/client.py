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
from typing import Optional

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
    """
    base = base_timeout() if base is None else base
    if base is None or base <= 0:
        return None
    command = request.WhichOneof("command")
    if command == "hello":
        return min(base, HELLO_TIMEOUT)
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


class AgentTimeoutError(TransportError, TimeoutError):
    """The agent did not reply before the deadline (a frozen main thread, a
    breakpoint, an ANR). Retrying straight away would only wait again."""

    hint = (f"If the app is just slow, raise the deadline with {TIMEOUT_ENV}=<seconds> "
            f"(0 disables it); otherwise check whether the app is frozen and `{LOGCAT_HINT}`.")


_LOST = "agent session lost (idle timeout, app restart, or the agent was shut down)"


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
        """Mark the client unusable and close the socket (whoever owns it)."""
        if self._broken is None:
            self._broken = reason
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # Core send/recv
    # ------------------------------------------------------------------ #
    def send(self, request: "pb.Request", timeout: Optional[float] = None) -> "pb.Response":
        """Assign a monotonic id, send the request, read and validate the response.

        Raises :class:`ClientError` for an agent ERROR reply (the connection
        stays usable), :class:`AgentTimeoutError` when the deadline passes and
        :class:`SessionLostError` for any other transport failure; those two
        close the client. ``timeout`` overrides the base deadline for this call.
        """
        command = request.WhichOneof("command") or "<unset>"
        with self._lock:
            if self._broken:
                raise SessionLostError(f"{_LOST}: {self._broken}")
            req_id = next(self._ids)
            request.id = req_id
            limit = request_timeout(request, timeout if timeout is not None else self._timeout)
            deadline = None if limit is None else time.monotonic() + limit
            try:
                self._sock.settimeout(limit)
                framing.write_message(self._sock, request.SerializeToString())
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

    def capture_skp(self, root_id: int = 0) -> "pb.CaptureSkpResponse":
        req = pb.Request()
        req.capture_skp.root_id = root_id
        return self.send(req).capture_skp

    def shutdown(self, timeout: Optional[float] = None) -> "pb.ShutdownResponse":
        """Stop the agent, for every client (not just this connection).

        Agents built before the reply fix close the connection before writing
        the reply; that EOF after sending counts as success. The client is
        closed afterwards either way. Raises :class:`SessionLostError` if the
        client was already closed (nothing was sent) and
        :class:`AgentTimeoutError` if a busy agent never answers.
        """
        if self._broken:
            raise SessionLostError(f"{_LOST}: {self._broken}")
        req = pb.Request()
        req.shutdown.SetInParent()
        try:
            return self.send(req, timeout=timeout).shutdown
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
