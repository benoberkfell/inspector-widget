"""Images (C9): per-window crops, overlays keyed by ref, inline payloads, pixel diff.

The two-window scene below mirrors the offline harness's ``default_scene``
(improve/offline-e2e-harness, ``tests/fakeagent.py``): a 360x640 main window
(root 1001) and a popup window (root 2001) at (40,560) 280x64, with the same
painted rectangles. Each window's screenshot is window-relative, rendered the way
``Scene.render`` does. When the harness is on the path, the same checks also run
on its own renderer.
"""

from __future__ import annotations

import base64
import os
import sys

import capture_builders as cb
import fakescenes as fs
import pytest
from loaded_fakes import FakeLoaded, screen_of

from inspector_widget.capture import images as im
from inspector_widget.capture.model import Issue, OpError

BG = (250, 250, 250)
OK_BLUE = (30, 60, 200)
SUBMIT = (98, 0, 238)
IMAGE = (200, 120, 40)
POPUP = (50, 50, 50)
MAIN = (0, 0, 360, 640)
POPUP_RECT = (40, 560, 280, 64)
PAINT = [((16, 80, 120, 48), OK_BLUE), ((16, 176, 200, 56), SUBMIT),
         ((240, 176, 96, 96), IMAGE), (POPUP_RECT, POPUP)]
AMBER = (244, 180, 0)
RED = (219, 68, 55)


def default_index(capture_id: str = "cdflt0"):
    """The harness default scene as an Index (refs n1..n11)."""
    b = cb.IndexBuilder(capture_id, package="com.oberkfell.a11yprobe", screen=(360, 640),
                        dpi=420)
    decor = b.window("n1", "DecorView", MAIN, udid=1001)
    content = b.view(decor, "n2", "LinearLayout", MAIN, udid=1002, rid="content")
    b.view(content, "n3", "TextView", (16, 24, 328, 40), udid=1003, rid="title",
           label="Hello world")
    b.view(content, "n4", "Button", (16, 80, 120, 48), udid=1004, rid="ok", label="OK",
           flags=["click", "focus"])
    b.view(content, "n5", "ImageView", (160, 80, 32, 32), udid=1005, rid="logo")
    acv = b.view(content, "n6", "AndroidComposeView", (0, 160, 360, 400), udid=1006,
                 cls="AndroidComposeView")
    b.compose(acv, "n7", sem_id=2, b=(16, 176, 200, 56), type="Button", label="Submit",
              flags=["click"])
    b.compose(acv, "n8", sem_id=3, b=(240, 176, 96, 96), type="Image")
    b.compose(acv, "n9", sem_id=6, b=(16, 440, 64, 64), flags=["click"])
    popup = b.window("n10", "PopupDecorView", POPUP_RECT, udid=2001)
    b.view(popup, "n11", "TextView", (56, 576, 248, 32), udid=2002, label="Saved")
    b.reading(["n3", "n4", "n7", "n11"])
    return b.build()


def default_shots(scale: float = 1.0):
    return {1001: screen_of(MAIN, BG, PAINT, scale), 2001: screen_of(POPUP_RECT, BG, PAINT, scale)}


def _loaded(ix=None, scale: float = 1.0, shots=None):
    ix = ix if ix is not None else default_index()
    return ix, FakeLoaded(ix, shots=shots if shots is not None else default_shots(scale))


def _pixels(path: str) -> tuple[int, int, set]:
    with open(path, "rb") as f:
        w, h, rgba = im.decode_png(f.read())
    return w, h, {tuple(rgba[i:i + 3]) for i in range(0, len(rgba), 4)}


def _pixel(path: str, x: int, y: int) -> tuple[int, int, int]:
    with open(path, "rb") as f:
        w, _h, rgba = im.decode_png(f.read())
    o = (y * w + x) * 4
    return tuple(rgba[o:o + 3])


@pytest.fixture
def no_pillow(monkeypatch):
    """Pillow 'uninstalled': every ``from PIL import ...`` raises ImportError."""
    for name in list(sys.modules):
        if name == "PIL" or name.startswith("PIL."):
            monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.setitem(sys.modules, "PIL.Image", None)
    assert im._pil() is None


# --------------------------------------------------------------------------- #
# Crops
# --------------------------------------------------------------------------- #
def test_popup_node_crop_comes_from_its_own_window_shot():
    ix, loaded = _loaded()
    out = im.crop(loaded, ix.get("n11"), pad=0)
    assert loaded.shot_calls == [2001]
    assert out["window"] == "n10" and out["kind"] == "crop" and out["from"] == "screenshot"
    assert out["px"] == [248, 32] and out["rect"] == [56, 576, 248, 32]
    w, h, colours = _pixels(out["path"])
    assert (w, h) == (248, 32) and colours == {POPUP}
    # the main window's pixels at the same spot are background: a wrong window would show
    main = im.crop(loaded, ix.get("n3"), pad=0)
    assert _pixels(main["path"])[2] == {BG}


def test_main_window_crops_match_the_paint():
    _ix, loaded = _loaded()
    ok = im.crop(loaded, "n4", pad=0)
    assert _pixels(ok["path"])[2] == {OK_BLUE} and ok["px"] == [120, 48]
    submit = im.crop(loaded, "n7", pad=0)
    assert _pixels(submit["path"])[2] == {SUBMIT}
    padded = im.crop(loaded, "n4", pad=16)
    assert padded["px"] == [152, 80] and _pixels(padded["path"])[2] == {OK_BLUE, BG}


def test_crop_pad_is_clamped_to_the_window():
    _ix, loaded = _loaded()
    out = im.crop(loaded, "n11", pad=100)  # the popup is only 280x64
    assert out["px"] == [280, 64] and out["rect"] == [40, 560, 280, 64]


def test_half_scale_capture_crops_correctly():
    _ix, loaded = _loaded(scale=0.5)
    out = im.crop(loaded, "n4", pad=0)
    assert out["px"] == [60, 24] and out["scale"] == 0.5 and out["rect"] == [16, 80, 120, 48]
    assert _pixels(out["path"])[2] == {OK_BLUE}
    popup = im.crop(loaded, "n11", pad=0)
    assert popup["px"] == [124, 16] and _pixels(popup["path"])[2] == {POPUP}


def test_crop_downscales_to_max_side_and_notes_clipping():
    ix = cb.launcher_index()
    shot = fs.replay_scene("launcher").screenshot(1, 1.0)
    loaded = FakeLoaded(ix, shots={1: shot})
    out = im.crop(loaded, "n22", pad=16, max_side=640)
    assert out["px"] == [640, 30] and out["rect"] == [0, 2741, 1280, 59]
    assert out["note"] == "visible part only (clipped by n10)"
    assert len(im.crop(loaded, "n22")["path"]) > 0
    assert len(__import__("json").dumps(out)) < 400


def test_crops_work_without_pillow(no_pillow):
    ix, loaded = _loaded()
    out = im.crop(loaded, "n11", pad=0)
    assert _pixels(out["path"])[2] == {POPUP}
    big = im.crop(loaded, "n2", pad=0, max_side=100)  # stdlib downscale
    assert max(big["px"]) == 100
    with pytest.raises(OpError) as e:
        im.overlay(loaded, ix, "marks")
    assert e.value.code == "unsupported" and "Pillow" in e.value.message


def test_repeated_crop_reuses_the_cached_file():
    _ix, loaded = _loaded()
    a = im.crop(loaded, "n4", pad=8)
    mtime = os.stat(a["path"]).st_mtime_ns
    b = im.crop(loaded, "n4", pad=8)
    assert a == b and os.stat(b["path"]).st_mtime_ns == mtime
    assert sum(1 for p in loaded.puts if p.startswith("img/n4-p8-")) == 1
    c = im.crop(loaded, "n4", pad=9)
    assert c["path"] != a["path"]
    assert a["path"].startswith(os.path.join(loaded.path, "img"))


def test_crop_errors():
    ix, loaded = _loaded()
    with pytest.raises(OpError) as e:
        im.crop(loaded, "n99")
    assert e.value.code == "not_found"
    ix.get("n5").b = [160, 80, 0, 0]
    with pytest.raises(OpError) as e:
        im.crop(loaded, "n5")
    assert e.value.code == "bad_args"
    ix2 = default_index()
    loaded2 = FakeLoaded(ix2, shots={1001: default_shots()[1001]})
    with pytest.raises(OpError) as e:
        im.crop(loaded2, "n11")
    assert e.value.code == "facet_unavailable" and "2001" in e.value.message
    launcher = cb.launcher_index()
    with pytest.raises(OpError) as e:
        im.crop(FakeLoaded(launcher, shots={}), "n304")  # a slot group with no semantics link
    assert e.value.code == "bad_args"


# --------------------------------------------------------------------------- #
# Overlays
# --------------------------------------------------------------------------- #
@pytest.fixture
def drawn(monkeypatch):
    """Record the items each overlay hands to overlay.render_items (still drawing)."""
    from inspector_widget import overlay as ov

    calls: list[list[dict]] = []
    real = ov.render_items

    def spy(base, items, out, **kw):
        calls.append([dict(i) for i in items])
        return real(base, items, out, **kw)

    monkeypatch.setattr(ov, "render_items", spy)
    return calls


def _near(path: str, x: int, y: int, colour, r: int = 3) -> bool:
    with open(path, "rb") as f:
        w, h, rgba = im.decode_png(f.read())
    for yy in range(max(0, y - r), min(h, y + r + 1)):
        for xx in range(max(0, x - r), min(w, x + r + 1)):
            o = (yy * w + xx) * 4
            if tuple(rgba[o:o + 3]) == colour:
                return True
    return False


def test_ov1_lint_overlay_colours_nodes_by_their_ref_keyed_issues(drawn):
    ix, loaded = _loaded()
    ix.get("n4").issues.append(Issue("a11y.touch_target.small", "warn"))
    ix.get("n7").issues.append(Issue("a11y.label.missing", "error"))
    out = im.overlay(loaded, ix, "lint", window="n1")
    assert out["window"] == "n1" and out["px"] == [360, 640]
    items = {i["label"].split()[0] if i["label"] else "": i for i in drawn[-1]}
    assert items["n4"]["color_idx"] == im.SEV_COLOR_IDX["warn"]
    assert items["n7"]["color_idx"] == im.SEV_COLOR_IDX["error"]
    # the drawn box edges carry the severity colours (OV1: they were never applied)
    assert _near(out["path"], 16, 80 + 24, AMBER)  # n4's left edge, mid-height
    assert _near(out["path"], 16, 176 + 28, RED)
    assert items["n4"]["label"] == "n4 !touch_target"


def test_mark_labels_are_refs_and_capped_at_60(drawn):
    ix, loaded = _loaded()
    out = im.overlay(loaded, ix, "marks")
    assert out["window"] == "screen" and out["marks"] == len(drawn[-1])
    labels = [i["label"] for i in drawn[-1]]
    assert set(labels) == {"n3", "n4", "n7", "n9", "n11"}  # clickable or a stop
    assert all(ix.get(label).ref == label for label in labels)
    wide = cb.wide_index()
    wloaded = FakeLoaded(wide, shots={1001: screen_of((1, 3, 300, 60), BG, [])})
    out = im.overlay(wloaded, wide, "marks", marks="all")
    assert out["marks"] == 60 and out["omitted"] > 0
    assert all(i["label"].startswith("n") for i in drawn[-1])


def test_explicit_marks_and_reading_numbers(drawn):
    ix, loaded = _loaded()
    im.overlay(loaded, ix, "marks", marks=["n4", "n11"])
    assert sorted(i["label"] for i in drawn[-1]) == ["n11", "n4"]
    im.overlay(loaded, ix, "reading")
    assert sorted(i["label"] for i in drawn[-1]) == ["1 n3", "2 n4", "3 n7", "4 n11"]
    with pytest.raises(OpError) as e:
        im.overlay(loaded, ix, "marks", marks=["n404"])
    assert e.value.code == "not_found"
    with pytest.raises(OpError):
        im.overlay(loaded, ix, "sparkles")


def test_composite_screen_puts_each_window_at_its_offset():
    ix, loaded = _loaded()
    out = im.overlay(loaded, ix, "none")
    assert out["window"] == "screen" and out["px"] == [360, 640]
    assert _pixel(out["path"], 100, 590) == POPUP  # inside the popup
    assert _pixel(out["path"], 20, 90) == OK_BLUE
    assert _pixel(out["path"], 5, 5) == BG


def test_overlay_on_a_node_uses_its_crop(drawn):
    ix, loaded = _loaded()
    out = im.overlay(loaded, ix, "marks", ref="n11", pad=4)
    assert out["ref"] == "n11" and out["window"] == "n10" and out["px"] == [256, 40]
    assert [i["label"] for i in drawn[-1]] == ["n11"]
    assert drawn[-1][0]["x"] == 4 and drawn[-1][0]["y"] == 4


def test_every_overlay_kind_renders_and_is_cached():
    ix, loaded = _loaded()
    paths = set()
    for kind in im.OVERLAY_KINDS:
        a = im.overlay(loaded, ix, kind, max_side=320)
        b = im.overlay(loaded, ix, kind, max_side=320)
        assert a == b and os.path.exists(a["path"]) and max(a["px"]) <= 320
        paths.add(a["path"])
    assert len(paths) == len(im.OVERLAY_KINDS)
    names = [p for p in loaded.puts if p.startswith("img/ov-")]
    assert len(names) == len(set(names)) == len(im.OVERLAY_KINDS) - 1  # "none" draws nothing


def test_a_rendering_code_change_misses_the_cache(monkeypatch):
    # L6: base composites were keyed by max_side and window ids only, so a rendering fix
    # reused the stale PNGs until someone bumped IMG_VERSION by hand. Every derived name
    # now folds in a fingerprint of the rendering code.
    ix, loaded = _loaded()
    first = im.overlay(loaded, ix, "marks", max_side=320)
    crop = im.crop(loaded, "n4", pad=8)
    n_puts = len(loaded.puts)
    assert im.overlay(loaded, ix, "marks", max_side=320) == first  # same code: a hit
    assert im.crop(loaded, "n4", pad=8) == crop and len(loaded.puts) == n_puts
    monkeypatch.setattr(im, "_FINGERPRINT", "changed-code")
    again = im.overlay(loaded, ix, "marks", max_side=320)
    assert again["path"] != first["path"] and os.path.exists(again["path"])
    assert im.crop(loaded, "n4", pad=8)["path"] != crop["path"]
    new = loaded.puts[n_puts:]
    assert any(p.startswith("img/base-") for p in new), new  # the base is redrawn too
    assert any(p.startswith("img/ov-marks") for p in new) and any(
        p.startswith("img/n4-p8-") for p in new)


def test_the_code_fingerprint_hashes_the_rendering_sources(monkeypatch):
    monkeypatch.setattr(im, "_FINGERPRINT", None)
    fp = im.code_fingerprint()
    assert len(fp) == 12 and im.code_fingerprint() == fp
    monkeypatch.setattr(im, "_FINGERPRINT", None)
    monkeypatch.setattr(im, "RENDER_MODULES", ("capture/no_such_module.py",))
    from inspector_widget import __version__
    assert im.code_fingerprint() == f"v{__version__}"  # no source to read: the version


def test_overlay_cache_key_follows_the_issues():
    ix, loaded = _loaded()
    first = im.overlay(loaded, ix, "lint")
    ix.get("n4").issues.append(Issue("a11y.contrast.low", "error"))
    assert im.overlay(loaded, ix, "lint")["path"] != first["path"]


# --------------------------------------------------------------------------- #
# inline
# --------------------------------------------------------------------------- #
def test_inline_downscales_to_max_side_and_estimates_tokens():
    ix = cb.launcher_index()
    loaded = FakeLoaded(ix, shots={1: fs.replay_scene("launcher").screenshot(1, 1.0)})
    full = im.overlay(loaded, ix, "none", max_side=4096)
    assert full["px"] == [1280, 2856]
    mime, data, tokens = im.inline(full["path"], 1024)
    w, h = im.png_size(base64.b64decode(data))
    assert mime == "image/png" and max(w, h) == 1024 and (w, h) == (459, 1024)
    assert tokens == round(459 * 1024 / 750) == 627  # the spec's ~630 for a full screen
    crop = im.crop(loaded, "n22", pad=0, max_side=4096)
    _, data2, tokens2 = im.inline(crop["path"], 2048)
    assert im.png_size(base64.b64decode(data2)) == (1280, 27) and tokens2 == 46


def test_inline_without_pillow(no_pillow):
    _ix, loaded = _loaded()
    out = im.crop(loaded, "n2", pad=0)
    mime, data, tokens = im.inline(out["path"], 64)
    w, h = im.png_size(base64.b64decode(data))
    assert mime == "image/png" and (w, h) == (36, 64) and tokens == round(36 * 64 / 750) == 3


# --------------------------------------------------------------------------- #
# PNG codec
# --------------------------------------------------------------------------- #
def test_stdlib_decoder_reads_pillow_filtered_pngs(monkeypatch):
    import io

    from PIL import Image

    img = Image.new("RGBA", (37, 23))
    img.putdata([((x * 7) % 256, (y * 11) % 256, (x * y) % 256, 255)
                 for y in range(23) for x in range(37)])
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)  # adaptive filters 0-4
    expected = img.tobytes()
    monkeypatch.setattr(im, "_pil", lambda: None)
    assert im.decode_png(buf.getvalue()) == (37, 23, expected)
    rgb = io.BytesIO()
    img.convert("RGB").save(rgb, "PNG")
    assert im.decode_png(rgb.getvalue())[2] == expected
    assert im.decode_png(im.encode_png(37, 23, expected)) == (37, 23, expected)


# --------------------------------------------------------------------------- #
# Pixel diff
# --------------------------------------------------------------------------- #
def test_pixel_diff_boxes_the_nodes_that_changed():
    a, la = _loaded(default_index("cdiffa"))
    changed = [((16, 80, 120, 48), (200, 30, 30)) if r == (16, 80, 120, 48) else (r, c)
               for r, c in PAINT]
    b = default_index("cdiffb")
    lb = FakeLoaded(b, shots={1001: screen_of(MAIN, BG, changed),
                              2001: screen_of(POPUP_RECT, BG, changed)})
    out = im.pixel_diff(la, lb, a, b)
    assert out["a"] == "cdiffa" and out["b"] == "cdiffb" and out["kind"] == "pixel_diff"
    assert out["nodes"] == ["n4"] and out["bbox"] == [16, 80, 120, 48]
    assert out["changed_px"] == 120 * 48 and out["changed"] == round(5760 / (360 * 640), 4)
    assert out["px"] == [360 * 3 + 16, 640] and out["path"].startswith(lb.path)
    assert _pixel(out["path"], 2 * 360 + 16 + 20, 100) == (230, 40, 40)  # mask: changed
    assert _pixel(out["path"], 2 * 360 + 16 + 5, 5) == (0, 0, 0)  # mask: unchanged
    again = im.pixel_diff(la, lb, a, b)
    assert again == out
    same = im.pixel_diff(la, la, a, a)
    assert same["changed_px"] == 0 and same["nodes"] == [] and "bbox" not in same
    boxed = im.pixel_diff(la, lb, a, b, refs=["n7"])
    assert boxed["nodes"] == ["n7"]


# --------------------------------------------------------------------------- #
# The real harness scene, when improve/offline-e2e-harness is on the path
# --------------------------------------------------------------------------- #
def test_popup_crop_on_the_harness_default_scene():
    fakeagent = pytest.importorskip("fakeagent")
    scene = fakeagent.default_scene()
    shots = {}
    for root in scene.roots:
        w, h, rgba = scene.render(root, 1.0)
        shots[root.id] = fs.rgba_to_screenshot(w, h, rgba, 1.0)
    _ix, loaded = _loaded(shots=shots)
    out = im.crop(loaded, "n11", pad=0)
    assert loaded.shot_calls == [2001]
    expected = {scene.pixel_at(x, y) for x in range(56, 56 + 248) for y in range(576, 576 + 32)}
    assert _pixels(out["path"])[2] == expected == {POPUP}
