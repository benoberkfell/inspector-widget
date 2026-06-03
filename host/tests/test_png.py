"""Tests for inspector_widget.png — screenshot decode + PNG emit.

Builds a synthetic Screenshot whose ``data`` is the wire format the agent emits:
a deflate-compressed [9-byte header][raw pixels] buffer. We exercise each
BitmapType (ABGR_8888, ARGB_8888, RGB_565) and confirm write_png emits a valid
PNG (8-byte signature) with the right dimensions. No device, no Pillow required
(png.py falls back to a stdlib encoder).
"""

from __future__ import annotations

import struct
import zlib

import pytest

from inspector_widget import png
from inspector_widget.proto import view_inspection_pb2 as pb

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _make_screenshot(width, height, bitmap_type, pixels) -> "pb.Screenshot":
    header = struct.pack("<ii", width, height) + bytes([bitmap_type])
    raw = header + pixels
    compressed = zlib.compress(raw, 1)
    s = pb.Screenshot()
    s.width = width
    s.height = height
    s.bitmap_type = bitmap_type
    s.scale = 1.0
    s.data = compressed
    return s


# --------------------------------------------------------------------------- #
# _decode_to_rgba
# --------------------------------------------------------------------------- #
def test_decode_abgr_8888_straight_copy():
    # ABGR_8888 in-memory bytes are already R,G,B,A -> straight copy. 2x1 image.
    # pixel0 = (10,20,30,255), pixel1 = (40,50,60,255)
    pixels = bytes([10, 20, 30, 255, 40, 50, 60, 255])
    s = _make_screenshot(2, 1, png.BITMAP_TYPE_ABGR_8888, pixels)
    w, h, rgba = png._decode_to_rgba(s)
    assert (w, h) == (2, 1)
    assert rgba == pixels


def test_decode_argb_8888_swaps_bgra_to_rgba():
    # ARGB_8888 in-memory bytes are B,G,R,A; decoder swaps to R,G,B,A.
    # one pixel with B=30,G=20,R=10,A=255 -> should become R=10,G=20,B=30,A=255.
    pixels = bytes([30, 20, 10, 255])
    s = _make_screenshot(1, 1, png.BITMAP_TYPE_ARGB_8888, pixels)
    w, h, rgba = png._decode_to_rgba(s)
    assert (w, h) == (1, 1)
    assert rgba == bytes([10, 20, 30, 255])


def test_decode_rgb_565_expands_to_rgba():
    # RGB_565 value 0xFFFF (all bits) -> white, opaque. LE 16-bit.
    pixels = struct.pack("<H", 0xFFFF)
    s = _make_screenshot(1, 1, png.BITMAP_TYPE_RGB_565, pixels)
    w, h, rgba = png._decode_to_rgba(s)
    assert (w, h) == (1, 1)
    assert rgba == bytes([255, 255, 255, 255])


def test_decode_rejects_unknown_bitmap_type():
    s = _make_screenshot(1, 1, 99, bytes([0, 0, 0, 0]))
    with pytest.raises(png.ScreenshotDecodeError):
        png._decode_to_rgba(s)


def test_decode_rejects_short_header():
    s = pb.Screenshot()
    s.data = zlib.compress(b"\x00\x01")  # < 9 bytes after inflate
    with pytest.raises(png.ScreenshotDecodeError):
        png._decode_to_rgba(s)


def test_decode_rejects_empty_data():
    s = pb.Screenshot()  # no data
    with pytest.raises(png.ScreenshotDecodeError):
        png._decode_to_rgba(s)


def test_decode_rejects_short_pixels():
    # Claims 4x4 ABGR (64 bytes) but supplies only 8.
    s = _make_screenshot(4, 4, png.BITMAP_TYPE_ABGR_8888, bytes(8))
    with pytest.raises(png.ScreenshotDecodeError):
        png._decode_to_rgba(s)


# --------------------------------------------------------------------------- #
# write_png
# --------------------------------------------------------------------------- #
def test_write_png_emits_valid_png(tmp_path):
    # 2x2 ABGR image.
    pixels = bytes(
        [255, 0, 0, 255,   0, 255, 0, 255,
         0, 0, 255, 255,   255, 255, 255, 255]
    )
    s = _make_screenshot(2, 2, png.BITMAP_TYPE_ABGR_8888, pixels)
    out = tmp_path / "shot.png"
    w, h = png.write_png(s, str(out))
    assert (w, h) == (2, 2)
    data = out.read_bytes()
    assert data[:8] == PNG_SIGNATURE
    # IHDR chunk type appears right after the 8-byte signature + 4-byte length.
    assert data[12:16] == b"IHDR"
    assert data.rstrip().endswith(b"IEND" + struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF)) or b"IEND" in data
