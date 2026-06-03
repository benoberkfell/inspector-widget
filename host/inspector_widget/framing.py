"""ViewSpector wire framing (host side).

Mirrors the agent's ``com.oberkfell.viewspector.agent.payload.Framing`` object and
CONTRACT.md section 4:

    MAGIC(8 bytes ascii "VWSPCT01") + LEN(4 bytes big-endian uint32) + payload

One Request is in flight at a time and the Response is read synchronously
(mirrors the ui-inspector CommandSender). A magic mismatch is a hard error.
"""

from __future__ import annotations

import socket
import struct

# 8-byte ASCII magic. Must match the agent's Framing.MAGIC exactly.
MAGIC = b"VWSPCT01"
MAGIC_SIZE = 8
LENGTH_SIZE = 4
# Guard against absurd allocations from a corrupt/misframed stream.
MAX_MESSAGE_SIZE = 256 * 1024 * 1024  # 256 MiB


class FramingError(IOError):
    """Raised on magic mismatch, short read, or an over-large frame."""


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    """Read exactly ``n`` bytes from ``sock`` or raise ``FramingError`` on EOF."""
    if n == 0:
        return b""
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise FramingError(
                f"socket closed while reading message body "
                f"(needed {n} bytes, got {n - remaining})"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def write_message(sock: socket.socket, payload: bytes) -> None:
    """Frame and send ``payload`` (an encoded protobuf Request) on ``sock``."""
    header = MAGIC + struct.pack(">I", len(payload))
    # sendall guarantees the whole buffer is written or an error is raised.
    sock.sendall(header + payload)


def read_message(sock: socket.socket) -> bytes:
    """Read one framed message from ``sock`` and return its raw payload bytes.

    Reads the 8-byte magic (asserts it), the 4-byte big-endian length, then
    exactly that many payload bytes. Raises ``FramingError`` on any mismatch.
    """
    magic = _recv_exactly(sock, MAGIC_SIZE)
    if magic != MAGIC:
        raise FramingError(
            f"bad framing magic: expected {MAGIC!r}, got {magic!r}"
        )
    (length,) = struct.unpack(">I", _recv_exactly(sock, LENGTH_SIZE))
    if length > MAX_MESSAGE_SIZE:
        raise FramingError(
            f"framed message length {length} exceeds max {MAX_MESSAGE_SIZE}"
        )
    return _recv_exactly(sock, length)
