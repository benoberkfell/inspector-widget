"""OV1 / OV2: the a11y overlay colours the nodes findings target and stays legible.

OV1  findings map to a11y nodes through the ID contract (typed keys; an untyped Compose
     finding by semantics id + overlapping bounds); unmatched findings are drawn at their
     own bounds; the summary reports what was flagged.
OV2  label text reaches 4.5:1 on its badge, labels of nested nodes don't overprint each
     other, font size follows the capture scale, and fonts load without macOS paths.
"""

from __future__ import annotations

import pytest

from inspector_widget import a11y, overlay

import mixed_fixture as mf

PIL = pytest.importorskip("PIL", reason="overlay rendering needs Pillow")
from PIL import Image  # noqa: E402

RED = (219, 68, 55)
AMBER = (244, 180, 0)
BLUE = (66, 133, 244)
GREEN = (15, 157, 88)


@pytest.fixture
def base(tmp_path):
    p = tmp_path / "base.png"
    Image.new("RGB", (mf.W, 2400), (0, 0, 0)).save(p)
    return str(p)


def _edge_colors(img, x0, y0, w, h, scale=1.0):
    """Colours along the four edges of a (full-res) rect, as drawn on a scaled canvas."""
    x0, y0, w, h = (int(v * scale) for v in (x0, y0, w, h))
    out = set()
    for y in range(y0 + 2, y0 + h - 2, 3):
        out.add(img.getpixel((x0 + 1, y)))
        out.add(img.getpixel((min(x0 + w, img.width - 1), y)))
    for x in range(x0 + 2, x0 + w - 2, 3):
        out.add(img.getpixel((x, min(y0 + h, img.height - 1))))
    return out


def _findings():
    orphan = {"rule": "a11y.contrast.low", "severity": "warn", "node": {"id": 999},
              "bounds": {"x": 100, "y": 2300, "w": 200, "h": 50}}
    return mf.untyped_compose_findings() + mf.typed_findings() + [orphan]


def test_severity_colours_apply_via_the_contract(base, tmp_path):
    data = a11y.a11y_to_dict(mf.a11y_response())
    out = str(tmp_path / "ov.png")
    s = overlay.render_a11y_overlay(base, data, out, findings=_findings())
    # 3 checkboxes (untyped, by semantics id + bounds), the row-1 Delete (warn),
    # view:52 (typed), compose:66:2 (a11y pair) -> 6 nodes; the id-999 finding by bounds.
    assert s["flagged_nodes"] == 6
    assert s["flagged_by_bounds"] == 1
    assert s["flagged"] == 7
    assert s["findings"] == len(_findings()) and s["findings_unplaced"] == 0
    img = Image.open(out).convert("RGB")
    for row, y in mf.CELL_Y.items():
        assert RED in _edge_colors(img, 820, y + 40, 120, 120), f"row {row} checkbox not red"
    assert AMBER in _edge_colors(img, 944, mf.CELL_Y[1] + 44, 112, 112)
    assert GREEN in _edge_colors(img, 944, mf.CELL_Y[0] + 44, 112, 112)  # row 0 Delete clean
    assert RED in _edge_colors(img, 940, 860, 120, 120)                 # view:52
    assert BLUE in _edge_colors(img, 40, 1500, 1000, 150)                # compose:66:2


def test_bare_semantics_id_never_colours_a_view(base, tmp_path):
    # node.id 50 is view:50's id but, untyped, it is read as a semantics id: no a11y
    # virtual node has it, so it must land on its own bounds, not on view:50.
    data = a11y.a11y_to_dict(mf.a11y_response())
    f = [{"rule": "r", "severity": "error", "node": {"id": 50},
          "bounds": {"x": 0, "y": 820, "w": 1080, "h": 200}}]
    s = overlay.render_a11y_overlay(base, data, str(tmp_path / "o.png"), findings=f)
    assert s["flagged_nodes"] == 0 and s["flagged_by_bounds"] == 1


def test_scaled_overlay_still_maps_findings(tmp_path):
    base = tmp_path / "half.png"
    Image.new("RGB", (mf.W // 2, 1200), (0, 0, 0)).save(base)
    data = a11y.a11y_to_dict(mf.a11y_response())
    out = str(tmp_path / "half_ov.png")
    s = overlay.render_a11y_overlay(str(base), data, out, findings=_findings(), scale=0.5)
    assert s["size"] == [mf.W // 2, 1200] and s["flagged_nodes"] == 6
    img = Image.open(out).convert("RGB")
    assert RED in _edge_colors(img, 940, 860, 120, 120, scale=0.5)


# --------------------------------------------------------------------------- OV2
@pytest.mark.parametrize("bg", list(overlay._SEVERITY_COLOR.values()) + overlay._PALETTE
                         + [overlay._A11Y_BOX_COLOR] + list(overlay._CONF_COLOR.values()))
def test_label_text_is_legible_on_every_badge(bg):
    badge, fg = overlay.legible_pair(bg)
    assert overlay.contrast_ratio(badge, fg) >= 4.5


def test_warn_amber_gets_dark_text():
    _, fg = overlay.legible_pair(AMBER)
    assert fg == (0, 0, 0)  # white on amber was 1.85:1


def test_nested_labels_do_not_overprint(base):
    cv = overlay._Canvas(base, 1.0)
    font = overlay.load_font(20)
    anchor = (100, 300, 400, 200)
    for text in ("Outer card", "Inner title", "Innermost"):
        assert cv.label(text, anchor, GREEN, font)
    rects = cv.placed
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            a, b = rects[i], rects[j]
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1], (a, b)


def test_font_size_follows_capture_scale():
    assert overlay.font_px(20, 1.0) == 20
    assert overlay.font_px(20, 0.5) == 10
    assert overlay.font_px(20, 0.25) == overlay._MIN_FONT_PX
    big = overlay.load_font(overlay.font_px(20, 1.0))
    small = overlay.load_font(overlay.font_px(20, 0.5))
    assert small.getbbox("Delete")[3] < big.getbbox("Delete")[3]


def test_font_falls_back_without_system_fonts(monkeypatch):
    monkeypatch.setattr(overlay, "_FONT_CANDIDATES", ("/nonexistent/font.ttf",))
    monkeypatch.setattr(overlay, "_BOLD_FONT_CANDIDATES", ())
    monkeypatch.setattr(overlay, "_FONT_CACHE", {})
    f = overlay.load_font(17)
    assert f.getbbox("Ag")[3] > 0
    if hasattr(f, "size"):
        assert f.size == 17  # Pillow >= 10.1 scales the default font


def test_integrated_label_shows_overlap_iou_not_a_float_error():
    assert overlay._integrated_label({"node_key": "view:1", "correlation_confidence": "exact"}) \
        == "view:1"
    assert overlay._integrated_label({"node_key": "compose:2:3",
                                      "correlation_confidence": "overlap",
                                      "a11y_iou": 0.724}) == "compose:2:3 a11y~0.72"


def test_focus_stops_are_labelled_with_their_announcement(base, tmp_path):
    data = a11y.a11y_to_dict(mf.a11y_response())
    items = overlay._a11y_collect_items([w["root"] for w in data["windows"]],
                                        data["focus_order"])
    labels = [i["label"] for i in items if i["label"]]
    assert labels == mf.EXPECTED_SPEECH  # one label per stop, in tree order
