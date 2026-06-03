"""Tests for inspector_widget.framing — the VWSPCT01 wire framing.

No socket library needed: we use a tiny in-memory fake socket that satisfies the
``sendall`` / ``recv`` surface framing.py relies on, including chunked ("partial")
delivery so we exercise the ``_recv_exactly`` loop.
"""

from __future__ import annotations

import struct

import pytest

from inspector_widget import framing


class FakeSocket:
    """In-memory bidirectional socket double.

    ``recv`` returns at most ``chunk_size`` bytes per call (0 == unlimited), so we
    can simulate partial reads. ``sendall`` appends to ``sent``.
    """

    def __init__(self, recv_buffer: bytes = b"", chunk_size: int = 0):
        self._recv = bytearray(recv_buffer)
        self.sent = bytearray()
        self.chunk_size = chunk_size

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, n: int) -> bytes:
        if not self._recv:
            return b""  # EOF
        take = n if self.chunk_size <= 0 else min(n, self.chunk_size)
        out = bytes(self._recv[:take])
        del self._recv[:take]
        return out


def _frame(payload: bytes) -> bytes:
    return framing.MAGIC + struct.pack(">I", len(payload)) + payload


def test_write_message_frames_magic_length_payload():
    sock = FakeSocket()
    payload = b"\x01\x02\x03hello"
    framing.write_message(sock, payload)
    assert bytes(sock.sent) == _frame(payload)
    # header layout: 8-byte magic + 4-byte big-endian length.
    assert sock.sent[:8] == framing.MAGIC
    (length,) = struct.unpack(">I", bytes(sock.sent[8:12]))
    assert length == len(payload)


def test_roundtrip_write_then_read():
    payload = b"a-serialized-request-\x00\xff"
    writer = FakeSocket()
    framing.write_message(writer, payload)
    reader = FakeSocket(recv_buffer=bytes(writer.sent))
    assert framing.read_message(reader) == payload


def test_roundtrip_empty_payload():
    writer = FakeSocket()
    framing.write_message(writer, b"")
    reader = FakeSocket(recv_buffer=bytes(writer.sent))
    assert framing.read_message(reader) == b""


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 7])
def test_partial_reads_reassemble(chunk_size):
    """read_message must reassemble a frame delivered one tiny chunk at a time."""
    payload = b"the quick brown fox jumps over the lazy dog" * 3
    reader = FakeSocket(recv_buffer=_frame(payload), chunk_size=chunk_size)
    assert framing.read_message(reader) == payload


def test_magic_mismatch_raises():
    bad = b"XXXXXXXX" + struct.pack(">I", 0)
    reader = FakeSocket(recv_buffer=bad)
    with pytest.raises(framing.FramingError) as ei:
        framing.read_message(reader)
    assert "magic" in str(ei.value).lower()


def test_truncated_magic_raises():
    reader = FakeSocket(recv_buffer=b"VWSP")  # short magic -> EOF mid-read
    with pytest.raises(framing.FramingError):
        framing.read_message(reader)


def test_truncated_body_raises():
    # Correct header claiming 10 bytes but only 4 supplied -> EOF mid-body.
    frame = framing.MAGIC + struct.pack(">I", 10) + b"abcd"
    reader = FakeSocket(recv_buffer=frame)
    with pytest.raises(framing.FramingError) as ei:
        framing.read_message(reader)
    assert "socket closed" in str(ei.value).lower()


def test_oversize_frame_rejected():
    huge = framing.MAX_MESSAGE_SIZE + 1
    frame = framing.MAGIC + struct.pack(">I", huge)
    reader = FakeSocket(recv_buffer=frame)
    with pytest.raises(framing.FramingError) as ei:
        framing.read_message(reader)
    assert "exceeds max" in str(ei.value).lower()
