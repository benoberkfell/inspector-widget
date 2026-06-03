"""Synchronous request/response client over a framed ViewSpector socket.

One Request is in flight at a time; the matching Response (same monotonic id) is
read back immediately, mirroring the ui-inspector CommandSender. Convenience
methods build the proper proto Request envelopes and unwrap the typed Response.
"""

from __future__ import annotations

import itertools
import socket
import threading
from typing import Optional

from . import framing
from .proto import view_inspection_pb2 as pb


class ClientError(RuntimeError):
    """Raised when the agent returns Response.status == ERROR."""


class Client:
    """Wraps a connected socket and speaks framed protobuf to the agent."""

    def __init__(self, sock: socket.socket, owns_socket: bool = True):
        self._sock = sock
        self._owns_socket = owns_socket
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Core send/recv
    # ------------------------------------------------------------------ #
    def send(self, request: "pb.Request") -> "pb.Response":
        """Assign a monotonic id, send the request, read and validate the response."""
        with self._lock:
            req_id = next(self._ids)
            request.id = req_id
            framing.write_message(self._sock, request.SerializeToString())
            raw = framing.read_message(self._sock)
            response = pb.Response()
            response.ParseFromString(raw)
            if response.id != req_id:
                raise ClientError(
                    f"response id mismatch: expected {req_id}, got {response.id}"
                )
            if response.status == pb.Response.ERROR:
                raise ClientError(
                    f"agent error (request id {req_id}): {response.error}"
                )
            return response

    # ------------------------------------------------------------------ #
    # Convenience commands
    # ------------------------------------------------------------------ #
    def hello(self) -> "pb.HelloResponse":
        req = pb.Request()
        req.hello.SetInParent()
        return self.send(req).hello

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

    def shutdown(self) -> "pb.ShutdownResponse":
        req = pb.Request()
        req.shutdown.SetInParent()
        return self.send(req).shutdown

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        if self._owns_socket:
            try:
                self._sock.close()
            except OSError:
                pass

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
