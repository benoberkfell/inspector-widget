"""Screenshot-decode parity: mcp_server and png must agree, with no R/B swap bug.

``mcp_server._decode_screenshot_to_png`` used to carry its own pixel-unpacking
copy (the removed ``_pixels_to_rgba``) that mishandled ARGB_8888 (type 3),
swapping red and blue. The contract makes it delegate to
``inspector_widget.png._decode_to_rgba`` (single source of truth). This test
builds a synthetic ARGB_8888 buffer (in-memory byte order B,G,R,A) and asserts:

  * ``png._decode_to_rgba`` un-swaps to R,G,B,A, and
  * the PNG emitted by ``mcp_server._decode_screenshot_to_png`` decodes back to
    the exact same RGBA pixels (so the two paths agree; R and B are NOT swapped).
"""

from __future__ import annotations

import io
import struct
import zlib

import pytest

import mcp_server
from inspector_widget import png
from inspector_widget.proto import view_inspection_pb2 as pb


def _make_argb_screenshot(width, height, rgba_pixels):
    """Build a type-3 (ARGB_8888) Screenshot from desired *output* RGBA pixels.

    ARGB_8888 wire bytes are laid out B,G,R,A in memory, so we pack each desired
    (R,G,B,A) as (B,G,R,A) to mimic what the agent actually sends.
    """
    buf = bytearray()
    for (r, g, b, a) in rgba_pixels:
        buf += bytes([b, g, r, a])
    header = struct.pack("<ii", width, height) + bytes([png.BITMAP_TYPE_ARGB_8888])
    raw = header + bytes(buf)
    s = pb.Screenshot()
    s.width = width
    s.height = height
    s.bitmap_type = png.BITMAP_TYPE_ARGB_8888
    s.scale = 1.0
    s.data = zlib.compress(raw, 1)
    return s


# Distinct R/G/B so a swap is detectable: red, green, blue, and an asymmetric mix.
_PIXELS = [
    (255, 0, 0, 255),     # pure red  -> if R/B swapped, decodes as blue
    (0, 0, 255, 255),     # pure blue -> if R/B swapped, decodes as red
    (10, 128, 240, 255),  # asymmetric R!=B
    (200, 50, 7, 128),    # asymmetric + non-opaque alpha
]


def test_png_decode_unswaps_argb():
    s = _make_argb_screenshot(2, 2, _PIXELS)
    w, h, rgba = png._decode_to_rgba(s)
    assert (w, h) == (2, 2)
    expected = bytes(b for px in _PIXELS for b in px)
    assert rgba == expected, "png._decode_to_rgba did not un-swap ARGB B/R"


def test_mcp_and_png_agree_on_rgba():
    pytest.importorskip("PIL", reason="need Pillow to decode the emitted PNG")
    from PIL import Image

    s = _make_argb_screenshot(2, 2, _PIXELS)

    # Reference RGBA from the single source of truth.
    w, h, ref_rgba = png._decode_to_rgba(s)

    # mcp_server path -> PNG bytes -> decode back to RGBA via PIL.
    png_bytes = mcp_server._decode_screenshot_to_png(s)
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    assert img.size == (w, h)
    mcp_rgba = img.tobytes()

    assert mcp_rgba == ref_rgba, (
        "mcp_server._decode_screenshot_to_png and png._decode_to_rgba disagree "
        "(R/B swap regression?)"
    )


def test_red_is_not_decoded_as_blue():
    """Targeted assertion on the exact failure mode of the old bug."""
    s = _make_argb_screenshot(1, 1, [(255, 0, 0, 255)])
    _w, _h, rgba = png._decode_to_rgba(s)
    assert rgba == bytes([255, 0, 0, 255]), (
        "pure red ARGB pixel decoded with R/B swapped"
    )
