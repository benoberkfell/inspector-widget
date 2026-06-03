"""Overlay capture-scale regression.

Node bounds are full-resolution, but the base PNG is captured at ``scale`` (<=1).
Every renderer must multiply each drawn coordinate by ``scale`` so boxes land on
the (smaller) base image instead of flying off at full-res coordinates.

We render a full-res node {0,0,400,800} onto a 200x400 base with ``scale=0.5``.
Correct behaviour draws the box edges at the canvas border (right edge ~x=199,
bottom edge ~y=399). A non-scaling renderer would put those edges at x=399 /
y=799 — entirely off the 200x400 canvas — so only the top/left edges would show.
We sample pixels with PIL to prove the right/bottom edges are actually present.
"""

from __future__ import annotations

import pytest

from inspector_widget import overlay

PIL = pytest.importorskip("PIL", reason="overlay rendering needs Pillow")
from PIL import Image  # noqa: E402

BASE_W, BASE_H = 200, 400
FULL_W, FULL_H = 400, 800  # node bounds at full resolution (2x the base)
SCALE = 0.5


@pytest.fixture
def base_png(tmp_path):
    """A solid-black 200x400 base so any colored outline pixel is unambiguous."""
    p = tmp_path / "base.png"
    Image.new("RGB", (BASE_W, BASE_H), (0, 0, 0)).save(p)
    return str(p)


def _load(out_path):
    return Image.open(out_path).convert("RGB")


def _is_colored(px):
    """True if a pixel is clearly an outline (not the black background)."""
    r, g, b = px[:3]
    return (r + g + b) > 60


def test_render_integrated_overlay_scales_to_base(base_png, tmp_path):
    out = str(tmp_path / "out.png")
    merged = {
        "roots": [{
            "node_key": "view:1",
            "bounds": {"layout": {"x": 0, "y": 0, "w": FULL_W, "h": FULL_H}},
            "children": [],
        }],
    }

    summary = overlay.render_integrated_overlay(base_png, merged, out, scale=SCALE)

    # The returned size is the BASE size, not the full-res node size.
    assert summary["size"] == [BASE_W, BASE_H]
    assert summary["boxes"] == 1

    img = _load(out)
    assert img.size == (BASE_W, BASE_H)

    # The scaled box is [0,0 .. 200,400] (drawn with width=3). Its RIGHT edge
    # must sit at the canvas border (~x=197..199), and its BOTTOM edge at
    # (~y=397..399). Sample a midpoint of each edge inside the canvas.
    mid_y = BASE_H // 2
    mid_x = BASE_W // 2

    right_edge = any(_is_colored(img.getpixel((x, mid_y)))
                     for x in range(BASE_W - 4, BASE_W))
    bottom_edge = any(_is_colored(img.getpixel((mid_x, y)))
                      for y in range(BASE_H - 4, BASE_H))

    assert right_edge, (
        "no outline near the right canvas edge: the box was NOT scaled "
        "(full-res right edge x=400 would be off the 200px-wide canvas)"
    )
    assert bottom_edge, (
        "no outline near the bottom canvas edge: the box was NOT scaled "
        "(full-res bottom edge y=800 would be off the 400px-tall canvas)"
    )


def test_render_items_scales_each_coordinate(base_png, tmp_path):
    out = str(tmp_path / "items.png")
    items = [{"x": 0, "y": 0, "w": FULL_W, "h": FULL_H, "label": ""}]

    summary = overlay.render_items(base_png, items, out, scale=SCALE)
    assert summary["size"] == [BASE_W, BASE_H]

    img = _load(out)
    mid_y = BASE_H // 2
    # Right edge of the scaled box should be near x=199, well inside the canvas.
    assert any(_is_colored(img.getpixel((x, mid_y)))
               for x in range(BASE_W - 4, BASE_W)), \
        "render_items did not scale the drawn box onto the base canvas"


def test_scale_one_is_a_noop_on_matching_canvas(tmp_path):
    """With scale=1.0 and a node that exactly fills the base, the box hugs the
    full canvas border (sanity check that scaling math is identity at 1.0)."""
    base = tmp_path / "base1.png"
    Image.new("RGB", (BASE_W, BASE_H), (0, 0, 0)).save(base)
    out = str(tmp_path / "out1.png")
    items = [{"x": 0, "y": 0, "w": BASE_W, "h": BASE_H, "label": ""}]

    overlay.render_items(str(base), items, out, scale=1.0)
    img = _load(out)
    mid_y = BASE_H // 2
    assert any(_is_colored(img.getpixel((x, mid_y)))
               for x in range(BASE_W - 4, BASE_W))
