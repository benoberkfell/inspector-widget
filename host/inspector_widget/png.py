"""Decode a ViewSpector ``Screenshot`` and write a PNG file.

Wire format (CONTRACT.md section 5 / BitmapUtils.kt):
  The ``Screenshot.data`` bytes are a ``Deflater(BEST_SPEED)``-compressed buffer.
  After inflating, the buffer is a 9-byte header followed by raw pixels:
    [width  LE int32 @0]
    [height LE int32 @4]
    [BitmapType byte  @8]
  BitmapType: 1=RGB_565 (2 B/px), 2=ABGR_8888 (4 B/px), 3=ARGB_8888 (4 B/px).

Pixel layouts (matching BitmapUtils.kt DirectColorModel masks; little-endian):
  RGB_565    : 16-bit LE; R=bits 11..15, G=bits 5..10, B=bits 0..4
  ABGR_8888  : 32-bit LE int, masks R=0x000000ff G=0x0000ff00 B=0x00ff0000
               A=0xff000000  => in-memory byte order is R,G,B,A
  ARGB_8888  : 32-bit LE int, masks R=0x00ff0000 G=0x0000ff00 B=0x000000ff
               A=0xff000000  => in-memory byte order is B,G,R,A

We normalise every format to RGBA8888 rows and emit a PNG. If Pillow is present
we use it; otherwise a small pure-stdlib (zlib+struct) encoder is used.
"""

from __future__ import annotations

import struct
import zlib
from typing import Tuple

from .proto import view_inspection_pb2 as pb

BITMAP_HEADER_SIZE = 9

BITMAP_TYPE_RGB_565 = 1
BITMAP_TYPE_ABGR_8888 = 2
BITMAP_TYPE_ARGB_8888 = 3


class ScreenshotDecodeError(RuntimeError):
    pass


def _decode_to_rgba(screenshot: "pb.Screenshot") -> Tuple[int, int, bytes]:
    """Inflate + decode the screenshot to (width, height, rgba_bytes).

    ``rgba_bytes`` is width*height*4 bytes, row-major, R,G,B,A per pixel.
    """
    if not screenshot.data:
        raise ScreenshotDecodeError("screenshot has empty data")

    raw = zlib.decompress(bytes(screenshot.data))
    if len(raw) < BITMAP_HEADER_SIZE:
        raise ScreenshotDecodeError(
            f"inflated buffer too small for header: {len(raw)} bytes"
        )

    width, height = struct.unpack_from("<ii", raw, 0)
    bitmap_type = raw[8]
    pixels = raw[BITMAP_HEADER_SIZE:]

    if width <= 0 or height <= 0:
        raise ScreenshotDecodeError(f"invalid dimensions {width}x{height}")

    if bitmap_type == BITMAP_TYPE_RGB_565:
        expected = width * height * 2
        if len(pixels) < expected:
            raise ScreenshotDecodeError(
                f"RGB_565 short pixels: have {len(pixels)}, need {expected}"
            )
        rgba = bytearray(width * height * 4)
        # Iterate LE 16-bit values.
        for i in range(width * height):
            v = pixels[2 * i] | (pixels[2 * i + 1] << 8)
            r5 = (v >> 11) & 0x1F
            g6 = (v >> 5) & 0x3F
            b5 = v & 0x1F
            # Expand to 8 bits replicating high bits into low (standard 565->888).
            r = (r5 << 3) | (r5 >> 2)
            g = (g6 << 2) | (g6 >> 4)
            b = (b5 << 3) | (b5 >> 2)
            o = 4 * i
            rgba[o] = r
            rgba[o + 1] = g
            rgba[o + 2] = b
            rgba[o + 3] = 0xFF
        return width, height, bytes(rgba)

    elif bitmap_type in (BITMAP_TYPE_ABGR_8888, BITMAP_TYPE_ARGB_8888):
        expected = width * height * 4
        if len(pixels) < expected:
            raise ScreenshotDecodeError(
                f"8888 short pixels: have {len(pixels)}, need {expected}"
            )
        rgba = bytearray(width * height * 4)
        if bitmap_type == BITMAP_TYPE_ABGR_8888:
            # in-memory bytes already R,G,B,A — straight copy.
            rgba[:expected] = pixels[:expected]
        else:
            # ARGB_8888 wire: in-memory bytes are B,G,R,A. Swap to R,G,B,A.
            for i in range(width * height):
                o = 4 * i
                b = pixels[o]
                g = pixels[o + 1]
                r = pixels[o + 2]
                a = pixels[o + 3]
                rgba[o] = r
                rgba[o + 1] = g
                rgba[o + 2] = b
                rgba[o + 3] = a
        return width, height, bytes(rgba)

    else:
        raise ScreenshotDecodeError(f"unknown BitmapType byte {bitmap_type}")


def _write_png_stdlib(path: str, width: int, height: int, rgba: bytes) -> None:
    """Encode RGBA8888 pixels to a PNG using only zlib + struct."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    # IHDR: 8-bit, color type 6 (RGBA).
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)

    # Each scanline is prefixed with a filter-type byte (0 = none).
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        start = y * stride
        raw += rgba[start:start + stride]
    compressed = zlib.compress(bytes(raw), 9)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", ihdr))
        f.write(chunk(b"IDAT", compressed))
        f.write(chunk(b"IEND", b""))


def write_png(screenshot: "pb.Screenshot", path: str) -> Tuple[int, int]:
    """Decode ``screenshot`` and write a PNG to ``path``.

    Returns (width, height). Uses Pillow when available, else a stdlib encoder.
    """
    width, height, rgba = _decode_to_rgba(screenshot)
    try:
        from PIL import Image  # type: ignore

        img = Image.frombytes("RGBA", (width, height), rgba)
        img.save(path, "PNG")
    except ImportError:
        _write_png_stdlib(path, width, height, rgba)
    return width, height
