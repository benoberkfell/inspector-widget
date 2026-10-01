"""Images of a capture: per-window crops, overlays keyed by ref, inline payloads, pixel diff.

Spec "Capture and Walk", section 5.8. Every pixel comes from the capture's own
per-window screenshots (``shot/w_<rootUdid>.pb``), so crops and overlays always
match the tree they were captured with:

* ``crop(loaded, n, pad=, max_side=)`` cuts a node's **visible** rect out of the
  screenshot of the node's own window, shifting by that window's screen offset and
  scaling by the capture scale. It works without Pillow (stdlib PNG codec).
* ``overlay(loaded, ix, kind, marks, ...)`` draws boxes labelled with refs
  (``marks``), issues coloured by severity (``lint``), TalkBack stops numbered in
  order (``reading``), every box (``bounds``) or Compose semantics (``compose``)
  over a window, or over the whole screen composited from every window in z order.
* ``walk_overlay(loaded, ix, record)`` draws a stored TalkBack walk (``tb_walk``):
  its stops numbered by step with arrows in the order TalkBack went, the model's
  next stop dashed where it differs, and mismatches in red.
  Drawing is delegated to ``overlay.render_items``; it needs Pillow and raises
  ``OpError("unsupported")`` without it.
* ``inline(path, max_side)`` returns ``(mime, base64, estimated tokens)`` for an
  MCP ``ImageContent``, downscaled so the long side is at most ``max_side``.
* ``pixel_diff(la, lb, a, b)`` puts two captures side by side with a delta mask
  and boxes around the nodes whose pixels changed.

Outputs are PNG files under the capture's ``img/`` directory, named by a hash of
the parameters, and reused when they already exist (the capture is immutable).
They are written through ``loaded.put_derived`` and located through
``loaded.derived_path`` (see ``CONTRACT_NOTES.md``).
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import struct
import zlib
from array import array
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from . import rules as R
from .model import Index, OpError, UNode

#: bump when image rendering changes, so cached files are not reused
IMG_VERSION = 1
MAX_MARKS = 60
DEFAULT_PAD = 16
DEFAULT_MAX_SIDE = 1024
TOKENS_PER_PX = 1 / 750
OVERLAY_KINDS = ("none", "marks", "lint", "reading", "bounds", "compose", "walk")
DIFF_THRESHOLD = 24

# overlay._PALETTE indices: 0 blue, 1 red, 2 green, 3 amber
SEV_COLOR_IDX = {"error": 1, "warn": 3, "info": 0}
CLEAN_COLOR_IDX = 2
_ACTION_FLAGS = frozenset({"click", "longclick", "edit", "checkable"})
_PNG_SIG = b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------- #
# Pillow (optional) and the stdlib PNG codec
# --------------------------------------------------------------------------- #
def _pil() -> Any:
    """``PIL.Image`` if Pillow is importable, else None (checked on every call, so a
    test can hide Pillow)."""
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - ImportError, or a None entry in sys.modules
        return None
    return Image


def _require_pil(what: str) -> Any:
    Image = _pil()
    if Image is None:
        raise OpError("unsupported", f"{what} needs Pillow, which is not installed",
                      hint="pip install Pillow (or the inspector-widget[overlay] extra); "
                           "crops work without it")
    return Image


def encode_png(w: int, h: int, rgba: bytes) -> bytes:
    """RGBA8888 rows -> PNG bytes (Pillow when present, else zlib + struct)."""
    Image = _pil()
    if Image is not None:
        import io

        buf = io.BytesIO()
        Image.frombytes("RGBA", (w, h), bytes(rgba)).save(buf, "PNG", compress_level=6)
        return buf.getvalue()
    stride = w * 4
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (_PNG_SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def png_size(data_or_path: bytes | str) -> tuple[int, int]:
    """Width and height from a PNG's IHDR."""
    if isinstance(data_or_path, str):
        with open(data_or_path, "rb") as f:
            head = f.read(24)
    else:
        head = data_or_path[:24]
    if head[:8] != _PNG_SIG:
        raise ValueError("not a PNG")
    return struct.unpack(">II", head[16:24])


def decode_png(data: bytes) -> tuple[int, int, bytes]:
    """PNG bytes -> (w, h, RGBA8888). Pillow when present; else a stdlib decoder
    for 8-bit, non-interlaced RGB/RGBA/grey (what the tools write)."""
    Image = _pil()
    if Image is not None:
        import io

        img = Image.open(io.BytesIO(data)).convert("RGBA")
        return img.width, img.height, img.tobytes()
    if data[:8] != _PNG_SIG:
        raise ValueError("not a PNG")
    pos, idat = 8, bytearray()
    w = h = depth = ctype = interlace = 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(ctype)
    if depth != 8 or channels is None or interlace:
        raise ValueError(f"unsupported PNG (depth {depth}, colour type {ctype}); install Pillow")
    raw = zlib.decompress(bytes(idat))
    stride = w * channels
    prev = bytearray(stride)
    out = bytearray()
    p = 0
    for _ in range(h):
        ftype = raw[p]
        line = bytearray(raw[p + 1:p + 1 + stride])
        p += 1 + stride
        for i in range(stride):
            a = line[i - channels] if i >= channels else 0
            b = prev[i]
            if ftype == 1:
                line[i] = (line[i] + a) & 0xFF
            elif ftype == 2:
                line[i] = (line[i] + b) & 0xFF
            elif ftype == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 0xFF
            elif ftype == 4:
                c = prev[i - channels] if i >= channels else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) \
                    & 0xFF
        prev = line
        if channels == 4:
            out += line
        else:
            for i in range(0, stride, channels):
                px = line[i:i + channels]
                if channels == 3:
                    out += px + b"\xff"
                elif channels == 1:
                    out += bytes((px[0], px[0], px[0], 255))
                else:
                    out += bytes((px[0], px[0], px[0], px[1]))
    return w, h, bytes(out)


def _fit(w: int, h: int, max_side: int | None) -> tuple[int, int]:
    if not max_side or max(w, h) <= max_side:
        return w, h
    f = max_side / max(w, h)
    return max(1, round(w * f)), max(1, round(h * f))


def resize(w: int, h: int, rgba: bytes, w2: int, h2: int) -> bytes:
    """Resample RGBA8888 to ``w2`` x ``h2`` (Pillow: bilinear; stdlib: nearest)."""
    if (w2, h2) == (w, h):
        return bytes(rgba)
    Image = _pil()
    if Image is not None:
        return Image.frombytes("RGBA", (w, h), bytes(rgba)).resize(
            (w2, h2), Image.BILINEAR).tobytes()
    src = array("I")
    src.frombytes(bytes(rgba))
    xs = [min(w - 1, int((x + 0.5) * w / w2)) for x in range(w2)]
    out = array("I")
    for y in range(h2):
        base = min(h - 1, int((y + 0.5) * h / h2)) * w
        out.extend([src[base + x] for x in xs])
    return out.tobytes()


def crop_rgba(w: int, rgba: bytes, x0: int, y0: int, x1: int, y1: int) -> bytes:
    """Rows ``y0..y1`` x columns ``x0..x1`` of a ``w``-wide RGBA8888 buffer."""
    return b"".join(rgba[(y * w + x0) * 4:(y * w + x1) * 4] for y in range(y0, y1))


# --------------------------------------------------------------------------- #
# The capture's image artifacts
# --------------------------------------------------------------------------- #
def _hash(params: Any) -> str:
    blob = json.dumps({"v": IMG_VERSION, **params}, sort_keys=True, default=str).encode()
    return hashlib.blake2s(blob, digest_size=4).hexdigest()


def _fname(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)


def _artifact(loaded: Any, name: str, make: Callable[[], bytes]) -> tuple[str, bool]:
    """``(path, cached)`` of derived artifact ``name`` (``img/...``), made once."""
    path_of = getattr(loaded, "derived_path", None)
    if path_of is not None:
        p = path_of(name)
        if p and os.path.exists(p):
            return p, True
    p = loaded.put_derived(name, make())
    if not p and path_of is not None:
        p = path_of(name)
    if not p:
        raise RuntimeError("LoadedCapture.put_derived must return the path it wrote")
    return str(p), False


def _shot(loaded: Any, udid: int) -> Any:
    from ..proto import view_inspection_pb2 as pb

    try:
        v = loaded.shot(int(udid))
    except (KeyError, OSError, ValueError):
        v = None
    if v is not None and not hasattr(v, "SerializeToString"):
        v = pb.Screenshot.FromString(bytes(v)) if v else None
    if v is None or not v.data:
        raise OpError("facet_unavailable", f"no screenshot of window w:{udid} in this capture",
                      hint="capture(screenshot=true) stores one per window")
    return v


def _decode(shot: Any) -> tuple[int, int, bytes]:
    from .. import png

    try:
        return png._decode_to_rgba(shot)
    except Exception as e:
        raise OpError("facet_unavailable", f"the stored screenshot does not decode: {e}") from e


def _rect(b: Any) -> tuple[int, int, int, int] | None:
    if not b or len(b) < 4:
        return None
    return int(b[0]), int(b[1]), int(b[2]), int(b[3])


def _ref(n: UNode) -> str:
    return n.ref or n.id


def _capture_id(loaded: Any, ix: Index | None = None) -> str | None:
    meta = getattr(loaded, "meta", None) or (ix.meta if ix is not None else None)
    return getattr(meta, "id", None)


class _Win:
    """A window root, its screen rect and its screenshot's pixel scale."""

    def __init__(self, loaded: Any, node: UNode) -> None:
        if "view" not in node.ids:
            raise OpError("unsupported", f"window {_ref(node)} has no View id")
        self.node = node
        self.udid = int(node.ids["view"])
        self.rect = _rect(node.b) or (0, 0, 0, 0)
        self.shot = _shot(loaded, self.udid)
        self._px: tuple[int, int, bytes] | None = None
        w = int(self.shot.width or 0)
        if w <= 0:
            w = self.pixels()[0]
        scale = float(self.shot.scale or 0.0)
        if scale <= 0.0:
            scale = w / self.rect[2] if self.rect[2] > 0 else 1.0
        self.scale = scale

    def pixels(self) -> tuple[int, int, bytes]:
        if self._px is None:
            self._px = _decode(self.shot)
        return self._px

    def png(self, loaded: Any) -> str:
        path, _ = _artifact(loaded, f"img/w_{self.udid}.png",
                            lambda: encode_png(*self.pixels()))
        return path


def _window_of(ix: Index, n: UNode) -> UNode:
    win = ix.nodes.get(n.window) if n.window else None
    if win is None:
        what = "an unlinked slot" if n.kind == "slot" else "not on any window"
        raise OpError("bad_args", f"{_ref(n)} is {what}, so it has no pixels",
                      hint="Pick a view, compose or a11y node.")
    return win


def _node(ix: Index, n: UNode | str) -> UNode:
    if isinstance(n, UNode):
        return n
    node = ix.get(str(n))
    if node is None:
        raise OpError("not_found", f"no node {n!r} in this capture")
    return node


# --------------------------------------------------------------------------- #
# crop
# --------------------------------------------------------------------------- #
def crop(loaded: Any, n: UNode | str, *, pad: int = DEFAULT_PAD,
         max_side: int = DEFAULT_MAX_SIDE) -> dict[str, Any]:
    """Cut node ``n``'s visible rect (plus ``pad`` screen px, clamped to the window)
    out of its own window's screenshot. Works without Pillow."""
    ix = loaded.index()
    node = _node(ix, n)
    pad = max(0, int(pad))
    max_side = max(16, int(max_side)) if max_side else 0
    r = _rect(node.b)
    if r is None or r[2] <= 0 or r[3] <= 0:
        raise OpError("bad_args", f"{_ref(node)} has no visible pixels (zero size or offscreen)",
                      hint="image(window=...) shows its whole window")
    win = _Win(loaded, _window_of(ix, node))
    ox, oy = win.rect[0], win.rect[1]
    s = win.scale
    W = int(win.shot.width or 0) or win.pixels()[0]
    H = int(win.shot.height or 0) or win.pixels()[1]
    x0 = max(0, int((r[0] - pad - ox) * s))
    y0 = max(0, int((r[1] - pad - oy) * s))
    x1 = min(W, math.ceil((r[0] + r[2] + pad - ox) * s))
    y1 = min(H, math.ceil((r[1] + r[3] + pad - oy) * s))
    if x1 <= x0 or y1 <= y0:
        raise OpError("bad_args", f"{_ref(node)} lies outside its window's screenshot",
                      hint=f"Scroll it into view and capture again, or image(window="
                           f"\"{_ref(win.node)}\") for the whole window.")
    cw, ch = x1 - x0, y1 - y0
    ow, oh = _fit(cw, ch, max_side)
    name = f"img/{_fname(_ref(node))}-p{pad}-{_hash({'k': node.key, 'pad': pad, 'm': max_side})}.png"

    def make() -> bytes:
        pw, _, rgba = win.pixels()
        return encode_png(ow, oh, resize(cw, ch, crop_rgba(pw, rgba, x0, y0, x1, y1), ow, oh))

    path, _cached = _artifact(loaded, name, make)
    out: dict[str, Any] = {"capture": _capture_id(loaded, ix), "ref": _ref(node), "kind": "crop",
                           "path": path, "px": [ow, oh], "window": _ref(win.node),
                           "from": "screenshot",
                           "rect": [int(ox + x0 / s), int(oy + y0 / s), int(cw / s), int(ch / s)]}
    if abs(s - 1.0) > 1e-6:
        out["scale"] = round(s, 4)
    note = _clip_note(ix, node)
    if note:
        out["note"] = note
    return out


def _clip_note(ix: Index, n: UNode) -> str | None:
    for iss in n.issues:
        if iss.id == "render.clipped":
            by = iss.evidence.get("clipped_by")
            return f"visible part only (clipped by {by})" if by else "visible part only"
    if n.visible is not None and n.visible < 1.0:
        return "visible part only"
    return None


# --------------------------------------------------------------------------- #
# overlay
# --------------------------------------------------------------------------- #
def _worst(n: UNode) -> str | None:
    return R.worst(i.sev for i in n.issues)


def _visible(n: UNode) -> bool:
    r = _rect(n.b)
    return r is not None and r[2] > 0 and r[3] > 0


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _select(ix: Index, kind: str, marks: Any, scope: list[UNode]
            ) -> tuple[list[tuple[UNode, str, int]], int]:
    """``([(node, label, colour index)], omitted)`` for one overlay kind."""
    order = {n.id: i for i, n in enumerate(scope)}

    def issue_label(n: UNode) -> str:
        shorts = []
        for i in sorted(n.issues, key=lambda i: -R.SEV_RANK.get(i.sev, 0)):
            s = R.short(i.id)
            if s not in shorts:
                shorts.append(s)
        more = f"+{len(shorts) - 1}" if len(shorts) > 1 else ""
        return f"{_ref(n)} !{shorts[0]}{more}"

    def sev_idx(n: UNode) -> int:
        return SEV_COLOR_IDX.get(_worst(n) or "", CLEAN_COLOR_IDX)

    picked: list[tuple[UNode, str, int, tuple]] = []
    if isinstance(marks, (list, tuple)):
        chosen = [_node(ix, m) for m in marks]
        for n in chosen:
            label = _ref(n) if kind != "reading" or n.stop is None else f"{n.stop} {_ref(n)}"
            picked.append((n, label, sev_idx(n) if kind in ("lint", "reading") else 0,
                           (order.get(n.id, 0),)))
    elif kind == "marks":
        for n in scope:
            clicky = bool(set(n.flags) & _ACTION_FLAGS)
            if marks == "all" or n.issues or clicky or n.stop is not None:
                pri = (0 if n.issues else 1, 0 if clicky else 1,
                       n.stop if n.stop is not None else 1 << 20, order[n.id])
                picked.append((n, _ref(n), 0, pri))
    elif kind == "lint":
        for n in scope:
            if n.issues:
                picked.append((n, issue_label(n), sev_idx(n),
                               (-R.SEV_RANK.get(_worst(n) or "", 0), order[n.id])))
            elif marks == "all" or set(n.flags) & _ACTION_FLAGS or n.stop is not None:
                picked.append((n, "", CLEAN_COLOR_IDX, (9, order[n.id])))
    elif kind == "reading":
        for n in scope:
            if n.stop is not None:
                picked.append((n, f"{n.stop} {_ref(n)}", sev_idx(n), (n.stop,)))
    elif kind == "bounds":
        for n in scope:
            content = bool(n.label or n.rid or n.tag or set(n.flags) & _ACTION_FLAGS)
            picked.append((n, _ref(n), n.depth % 8, (0 if content else 1, order[n.id])))
    elif kind == "compose":
        for n in scope:
            if n.kind == "compose":
                text = n.label or n.type or n.tag
                label = f"{_ref(n)} {_cut(text, 24)}" if text else _ref(n)
                picked.append((n, label, n.depth % 8, (0 if text else 1, order[n.id])))
    picked.sort(key=lambda p: p[3])
    omitted = max(0, len(picked) - MAX_MARKS)
    return [(n, label, c) for n, label, c, _ in picked[:MAX_MARKS]], omitted


def _screen_size(ix: Index, wins: Sequence[_Win]) -> tuple[int, int]:
    dev = (getattr(ix.meta, "device", None) or {}) if ix.meta else {}
    if dev.get("screen") and len(dev["screen"]) >= 2:
        return int(dev["screen"][0]), int(dev["screen"][1])
    return (max(w.rect[0] + w.rect[2] for w in wins), max(w.rect[1] + w.rect[3] for w in wins))


def _base(loaded: Any, ix: Index, wins: Sequence[_Win], max_side: int
          ) -> tuple[str, int, int, float, tuple[int, int]]:
    """The picture the overlay is drawn on, downscaled to ``max_side``:
    ``(png path, w, h, screen-px -> image-px factor, screen origin)``. One window:
    its own screenshot; several: the screen composited in z order."""
    Image = _require_pil("Overlays")
    if len(wins) == 1:
        w0 = wins[0]
        W, H = (int(w0.shot.width or 0), int(w0.shot.height or 0))
        if W <= 0 or H <= 0:
            W, H = w0.pixels()[0], w0.pixels()[1]
        ow, oh = _fit(W, H, max_side)
        if (ow, oh) == (W, H):
            return w0.png(loaded), W, H, w0.scale, (w0.rect[0], w0.rect[1])
        name = f"img/base-w{w0.udid}-{_hash({'m': max_side})}.png"

        def make_one() -> bytes:
            pw, ph, rgba = w0.pixels()
            return encode_png(ow, oh, resize(pw, ph, rgba, ow, oh))

        path, _ = _artifact(loaded, name, make_one)
        return path, ow, oh, w0.scale * ow / W, (w0.rect[0], w0.rect[1])
    sw, sh = _screen_size(ix, wins)
    s = min(w.scale for w in wins)
    cw, ch = max(1, int(sw * s)), max(1, int(sh * s))
    ow, oh = _fit(cw, ch, max_side)
    name = f"img/base-screen-{_hash({'m': max_side, 'w': [w.udid for w in wins]})}.png"

    def make_screen() -> bytes:
        import io

        canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 255))
        for w in sorted(wins, key=lambda w: (w.node.z or 0)):
            pw, ph, rgba = w.pixels()
            img = Image.frombytes("RGBA", (pw, ph), rgba)
            tw, th = max(1, int(w.rect[2] * s)), max(1, int(w.rect[3] * s))
            if (tw, th) != (pw, ph):
                img = img.resize((tw, th), Image.BILINEAR)
            # over, not paste: a dialog window is transparent outside its card, and
            # the window under it shows there (a paste made it black)
            layer = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
            layer.paste(img, (int(w.rect[0] * s), int(w.rect[1] * s)))
            canvas = Image.alpha_composite(canvas, layer)
        if (ow, oh) != (cw, ch):
            canvas = canvas.resize((ow, oh), Image.BILINEAR)
        buf = io.BytesIO()
        canvas.save(buf, "PNG", compress_level=6)
        return buf.getvalue()

    path, _ = _artifact(loaded, name, make_screen)
    return path, ow, oh, s * ow / cw, (0, 0)


def _draw(loaded: Any, name: str, base: str, items: list[dict[str, Any]], font_px: int) -> str:
    """Render ``items`` over ``base`` with ``overlay.render_items`` into artifact ``name``."""
    from .. import overlay as ov

    def make() -> bytes:
        tmp_dir = os.path.dirname(os.path.abspath(base))
        tmp = os.path.join(tmp_dir, f".draw-{os.getpid()}-{_hash({'n': name})}.png")
        try:
            ov.render_items(base, items, tmp, font_size=font_px, scale=1.0)
            with open(tmp, "rb") as f:
                return f.read()
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    path, _ = _artifact(loaded, name, make)
    return path


def overlay(loaded: Any, ix: Index, kind: str = "marks", marks: Any = "auto", *,
            window: str | None = None, ref: str | None = None, pad: int = DEFAULT_PAD,
            max_side: int = DEFAULT_MAX_SIDE) -> dict[str, Any]:
    """Draw ``kind`` over a window (``window``, or ``ref``'s window) or the whole
    screen. ``marks``: ``"auto"``, ``"all"`` or a list of refs; at most 60 boxes are
    drawn and the rest counted in ``omitted``. With ``ref`` the picture is that
    node's crop (``crop()``, plus ``pad``) and only nodes overlapping it are drawn.
    ``kind="none"`` gives the plain picture."""
    if kind not in OVERLAY_KINDS:
        raise OpError("bad_args", f"overlay must be one of {', '.join(OVERLAY_KINDS)}")
    if not (marks in ("auto", "all") or isinstance(marks, (list, tuple))):
        raise OpError("bad_args", 'marks must be "auto", "all" or a list of refs')
    _require_pil("Overlays")
    max_side = max(64, int(max_side)) if max_side else 4096
    target = _node(ix, ref) if ref else None
    if target is not None:
        win_nodes = [_window_of(ix, target)]
    elif window:
        wn = _node(ix, window)
        win_nodes = [wn if wn.is_window else _window_of(ix, wn)]
    else:
        win_nodes = ix.windows()
    if not win_nodes:
        raise OpError("facet_unavailable", "this capture has no windows")
    wins = []
    for wn in win_nodes:
        try:
            wins.append(_Win(loaded, wn))
        except OpError:
            if len(win_nodes) == 1:
                raise
    if not wins:
        raise OpError("facet_unavailable", "no window of this capture has a screenshot",
                      hint="capture(screenshot=true)")
    if target is not None:
        c = crop(loaded, target, pad=pad, max_side=max_side)
        base, (bw, bh) = c["path"], c["px"]
        rx, ry, rw, _rh = c["rect"]
        f, (ox, oy) = bw / rw if rw else 1.0, (rx, ry)
    else:
        base, bw, bh, f, (ox, oy) = _base(loaded, ix, wins, max_side)

    ids = {w.node.id for w in wins}
    if kind == "lint" and target is None and not window:
        # as the a11y overlay: a window under an open dialog is not drawn over the dialog
        from .analyzers import covered_windows

        ids -= set(covered_windows(ix, loaded))
    scope = [n for n, _ in ix.walk("ui") if n.window in ids and _visible(n)]
    if target is not None:
        tr = _rect(target.b) or (0, 0, 0, 0)
        box = (tr[0] - pad, tr[1] - pad, tr[2] + 2 * pad, tr[3] + 2 * pad)
        scope = [n for n in scope if _overlaps(_rect(n.b), box)]
    chosen, omitted = ([], 0) if kind == "none" else _select(ix, kind, marks, scope)
    items = []
    for n, label, color in chosen:
        r = _rect(n.b)
        if r is None:
            continue
        items.append({"x": round((r[0] - ox) * f), "y": round((r[1] - oy) * f),
                      "w": max(1, round(r[2] * f)), "h": max(1, round(r[3] * f)),
                      "label": label, "color_idx": color})
    params = {"kind": kind, "marks": marks if isinstance(marks, str) else [_ref(n) for n, *_ in
                                                                          chosen],
              "w": sorted(ids), "ref": target.key if target else None, "pad": pad,
              "m": max_side, "issues": _issue_digest(chosen)}
    font_px = max(11, min(22, round(max(bw, bh) / 55)))
    name = f"img/ov-{kind}-{_hash(params)}.png"
    path = base if kind == "none" else _draw(loaded, name, base, items, font_px)
    pw, ph = png_size(path)
    out: dict[str, Any] = {"capture": _capture_id(loaded, ix), "kind": "overlay",
                           "overlay": kind, "path": path, "px": [pw, ph],
                           "window": _ref(wins[0].node) if len(wins) == 1 else "screen"}
    if target is not None:
        out["ref"] = _ref(target)
    if kind != "none":
        out["marks"] = len(items)
    if omitted:
        out["omitted"] = omitted
    if kind in ("lint", "reading"):
        out["legend"] = "red error, amber warn, blue info, green no issue"
    return out


# --------------------------------------------------------------------------- #
# walk overlay
# --------------------------------------------------------------------------- #
WALK_OK, WALK_BAD, WALK_MODEL = 0, 1, 3  # overlay._PALETTE: blue, red, amber
WALK_LEGEND = ("numbers: TalkBack's steps; blue arrows: the order it went; red: a finding "
               "or the model disagrees; dashed amber: the model's next stop; dashed red: "
               "predicted, never reached")


def _walk_moves(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The steps TalkBack moved to, bound to a ref, in walk order."""
    return [s for s in record.get("steps") or []
            if s.get("moved") and s.get("key") and not s.get("edge")
            and s.get("via") not in ("left_app", "lost") and not s.get("unbound")]


def walk_expected(record: Mapping[str, Any]) -> dict[int, str | None]:
    """The model's next stop (a ref) before each step of the walk, where it predicted
    one (the walk's first lap; talkback.diff's model check, by ref)."""
    pred = [p.get("ref") for p in record.get("predicted") or [] if p.get("ref")]
    step = 1 if record.get("direction", "next") == "next" else -1
    out: dict[int, str | None] = {}
    moves = _walk_moves(record)
    pos = pred.index(moves[0]["ref"]) if moves and moves[0]["ref"] in pred else None
    for s in moves[1:]:
        if s.get("via") == "stolen":
            pos = pred.index(s["ref"]) if s["ref"] in pred else None
            continue
        i = pos + step if pos is not None else None
        out[int(s["i"])] = pred[i] if i is not None and 0 <= i < len(pred) else None
        pos = pred.index(s["ref"]) if s["ref"] in pred else None
    return out


def _walk_bad_steps(record: Mapping[str, Any], expected: Mapping[int, str | None]) -> set[int]:
    bad = {int(i) for f in record.get("findings") or [] if f.get("code") != "model.mismatch"
           for i in f.get("steps") or []}
    for i, exp in expected.items():
        s = next((x for x in record.get("steps") or [] if x.get("i") == i), None)
        if s is not None and exp is not None and exp != s.get("ref"):
            bad.add(i)
    return bad


def _arrow_png(base: str, out: str, arrows: list[tuple], width: int) -> None:
    """Draw ``arrows`` (``((x0,y0), (x1,y1), colour index, dashed)``) over ``base`` as
    arcs that bulge to the left of their direction, so a move down a column and the
    move back up it do not lie on one line, with an arrowhead at the end."""
    from .. import overlay as ov

    Image = _require_pil("Overlays")
    from PIL import ImageDraw

    img = Image.open(base).convert("RGBA")
    d = ImageDraw.Draw(img)
    head = max(8, width * 4)
    side = max(img.size) / 5
    for (x0, y0), (x1, y1), color, dashed in arrows:
        rgb = tuple(ov._PALETTE[color % len(ov._PALETTE)])
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        if length < 2:
            continue
        bulge = min(0.35 * length, side)
        cx, cy = (x0 + x1) / 2 - dy / length * bulge, (y0 + y1) / 2 + dx / length * bulge
        n = max(8, int(length / 6))
        pts = [((1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t * t * x1,
                (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t * t * y1)
               for t in (k / n for k in range(n + 1))]
        for k in range(n):
            if not dashed or (k // 2) % 2 == 0:
                d.line([pts[k], pts[k + 1]], fill=rgb, width=width)
        ang = math.atan2(y1 - cy, x1 - cx)  # the curve's direction where it arrives
        for sgn in (1, -1):
            a = ang + math.pi + sgn * 0.45
            d.line([(x1, y1), (x1 + head * math.cos(a), y1 + head * math.sin(a))], fill=rgb,
                   width=width)
    img.save(out, "PNG")


def walk_overlay(loaded: Any, ix: Index, record: Mapping[str, Any], *, window: str | None = None,
                 max_side: int = DEFAULT_MAX_SIDE) -> dict[str, Any]:
    """A stored walk drawn on capture ``loaded`` (a window, or the screen composited
    from every window): each stop boxed and labelled with its step numbers and ref,
    arrows between consecutive steps, the model's next stop dashed where it differs,
    predicted stops the walk never reached dashed red. Steps whose node is not in
    this capture (or window) are counted in ``omitted``."""
    _require_pil("Overlays")
    max_side = max(64, int(max_side)) if max_side else 4096
    if window:
        wn = _node(ix, window)
        win_nodes = [wn if wn.is_window else _window_of(ix, wn)]
    else:
        win_nodes = ix.windows()
    if not win_nodes:
        raise OpError("facet_unavailable", "this capture has no windows")
    wins = []
    for wn in win_nodes:
        try:
            wins.append(_Win(loaded, wn))
        except OpError:
            if len(win_nodes) == 1:
                raise
    if not wins:
        raise OpError("facet_unavailable", "no window of this capture has a screenshot",
                      hint="capture(screenshot=true)")
    base, bw, bh, f, (ox, oy) = _base(loaded, ix, wins, max_side)
    ids = {w.node.id for w in wins}

    def box(ref: str | None) -> tuple[int, int, int, int] | None:
        n = ix.get(ref) if ref else None
        r = _rect(n.b) if n is not None else None
        if n is None or r is None or n.window not in ids or r[2] <= 0 or r[3] <= 0:
            return None
        return (round((r[0] - ox) * f), round((r[1] - oy) * f), max(1, round(r[2] * f)),
                max(1, round(r[3] * f)))

    def centre(b: tuple[int, int, int, int]) -> tuple[float, float]:
        return b[0] + b[2] / 2, b[1] + b[3] / 2

    moves = _walk_moves(record)
    expected = walk_expected(record)
    bad = _walk_bad_steps(record, expected)
    order: dict[str, list[int]] = {}
    for s in moves:
        order.setdefault(s["ref"], []).append(int(s["i"]))
    items: list[dict[str, Any]] = []
    drawn: set[str] = set()
    omitted = 0
    for ref, steps in order.items():
        b = box(ref)
        if b is None:
            omitted += len(steps)
            continue
        nums = ",".join(str(i) for i in steps[:3]) + ("+" if len(steps) > 3 else "")
        color = WALK_BAD if any(i in bad for i in steps) else WALK_OK
        items.append({"x": b[0], "y": b[1], "w": b[2], "h": b[3], "label": f"{nums} {ref}",
                      "color_idx": color})
        drawn.add(ref)
    vs = record.get("vs_model") or {}
    unvisited = [r for r in vs.get("unvisited") or [] if r not in drawn]
    for f_ in record.get("findings") or []:
        if f_.get("code") == "tb.skipped":
            unvisited += [r for r in f_.get("refs") or [] if r not in drawn and r not in unvisited]
    for ref in unvisited[:MAX_MARKS]:
        b = box(ref)
        if b is not None:
            items.append({"x": b[0], "y": b[1], "w": b[2], "h": b[3], "label": f"skip {ref}",
                          "color_idx": WALK_BAD, "dashed": True})
    width = max(2, round(max(bw, bh) / 400))
    arrows: list[tuple] = []
    for a, s in zip(moves, moves[1:]):
        ba, bs = box(a["ref"]), box(s["ref"])
        if ba is not None and bs is not None and a["ref"] != s["ref"] and s.get("via") != "wrap":
            arrows.append((centre(ba), centre(bs), WALK_BAD if int(s["i"]) in bad else WALK_OK,
                           False))
        exp = expected.get(int(s["i"]))
        be = box(exp)
        if exp and exp != s["ref"] and ba is not None and be is not None and exp != a["ref"]:
            arrows.append((centre(ba), centre(be), WALK_MODEL, True))
    font_px = max(11, min(22, round(max(bw, bh) / 55)))
    params = {"kind": "walk", "walk": record.get("id"), "w": sorted(ids), "m": max_side,
              "items": [(it["label"], it["color_idx"], it.get("dashed", False)) for it in items],
              "arrows": [(a[2], a[3]) for a in arrows], "v": IMG_VERSION}
    name = f"img/ov-walk-{_hash(params)}.png"

    def make() -> bytes:
        from .. import overlay as ov

        tmp_dir = os.path.dirname(os.path.abspath(base))
        tmp = os.path.join(tmp_dir, f".walk-{os.getpid()}-{_hash({'n': name})}.png")
        try:
            ov.render_items(base, items, tmp, font_size=font_px, scale=1.0)
            _arrow_png(tmp, tmp, arrows, width)
            with open(tmp, "rb") as fh:
                return fh.read()
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    path, _cached = _artifact(loaded, name, make)
    pw, ph = png_size(path)
    out: dict[str, Any] = {"capture": _capture_id(loaded, ix), "walk": record.get("id"),
                           "kind": "overlay", "overlay": "walk", "path": path, "px": [pw, ph],
                           "window": _ref(wins[0].node) if len(wins) == 1 else "screen",
                           "steps": sum(len(v) for r, v in order.items() if r in drawn),
                           "arrows": len(arrows)}
    if omitted:
        out["omitted"] = omitted
        others = [c for c in record.get("captures") or [] if c != out["capture"]]
        out["note"] = (f"{omitted} step(s) not on this capture"
                       + (f"; try capture=\"{others[-1]}\"" if others else "")
                       + (" or the screen (no window)" if window else ""))
    out["legend"] = WALK_LEGEND
    return out


def _overlaps(r: tuple[int, int, int, int] | None, box: tuple[int, int, int, int]) -> bool:
    if r is None:
        return False
    return not (r[0] >= box[0] + box[2] or r[0] + r[2] <= box[0]
                or r[1] >= box[1] + box[3] or r[1] + r[3] <= box[1])


def _issue_digest(chosen: Iterable[tuple[UNode, str, int]]) -> str:
    """Folds labels and colours into the cache key, so issues added after capture
    (lint with contrast) never serve a stale overlay."""
    blob = "|".join(f"{_ref(n)}:{label}:{c}" for n, label, c in chosen).encode()
    return hashlib.blake2s(blob, digest_size=4).hexdigest()


# --------------------------------------------------------------------------- #
# inline
# --------------------------------------------------------------------------- #
def inline(path: str, max_side: int = DEFAULT_MAX_SIDE) -> tuple[str, str, int]:
    """``(mime, base64 data, estimated tokens)`` for an MCP ``ImageContent``: the PNG
    at ``path``, downscaled so its long side is at most ``max_side``. Tokens are
    estimated as ``w * h / 750``."""
    with open(path, "rb") as f:
        data = f.read()
    w, h = png_size(data)
    w2, h2 = _fit(w, h, max(16, int(max_side)) if max_side else 0)
    if (w2, h2) != (w, h):
        pw, ph, rgba = decode_png(data)
        data = encode_png(w2, h2, resize(pw, ph, rgba, w2, h2))
    return "image/png", base64.b64encode(data).decode("ascii"), max(1, round(w2 * h2 *
                                                                              TOKENS_PER_PX))


# --------------------------------------------------------------------------- #
# pixel diff
# --------------------------------------------------------------------------- #
def _screen_rgba(loaded: Any, ix: Index, max_side: int) -> tuple[Any, float]:
    """The capture's screen (one window, or all composited) as a PIL image, and the
    screen-px -> image-px factor."""
    Image = _require_pil("Pixel diff")
    wins = []
    for wn in ix.windows():
        try:
            wins.append(_Win(loaded, wn))
        except OpError:
            continue
    if not wins:
        raise OpError("facet_unavailable", f"capture {_capture_id(loaded, ix)} has no screenshot",
                      hint="capture(screenshot=true)")
    path, _w, _h, f, (ox, oy) = _base(loaded, ix, wins, max_side)
    img = Image.open(path).convert("RGBA")
    if (ox, oy) != (0, 0):  # a single window that does not start at the screen origin
        sw, sh = _screen_size(ix, wins)
        canvas = Image.new("RGBA", (max(1, int(sw * f)), max(1, int(sh * f))), (0, 0, 0, 255))
        canvas.paste(img, (int(ox * f), int(oy * f)))
        img = canvas
    return img, f


def pixel_diff(la: Any, lb: Any, a: Index, b: Index, *, refs: Sequence[str] | None = None,
               threshold: int = DIFF_THRESHOLD, max_side: int = DEFAULT_MAX_SIDE,
               max_boxes: int = 20) -> dict[str, Any]:
    """Side by side ``[a | b | delta mask]`` with boxes around the nodes of ``b``
    whose pixels changed (or around ``refs``, e.g. the changed refs from diff()).
    Written under ``b``'s capture. Needs Pillow."""
    Image = _require_pil("Pixel diff")
    from PIL import ImageChops, ImageDraw

    ia, _fa = _screen_rgba(la, a, max_side)
    ib, fb = _screen_rgba(lb, b, max_side)
    if ia.size != ib.size:
        ia = ia.resize(ib.size, Image.BILINEAR)
    delta = ImageChops.difference(ia.convert("RGB"), ib.convert("RGB")).convert("L")
    mask = delta.point(lambda v: 255 if v > threshold else 0)
    hist = mask.histogram()
    changed_px = hist[255]
    total_px = ib.size[0] * ib.size[1]
    bbox = mask.getbbox()

    boxes: list[UNode] = []
    more = 0
    if refs:
        boxes = [_node(b, r) for r in refs][:max_boxes]
        more = max(0, len(refs) - max_boxes)
    elif bbox is not None:
        hot: set[str] = set()
        for n, _ in b.walk("ui"):
            r = _rect(n.b)
            if r is None or r[2] <= 0 or r[3] <= 0 or n.is_window:
                continue
            box = (int(r[0] * fb), int(r[1] * fb), int((r[0] + r[2]) * fb) + 1,
                   int((r[1] + r[3]) * fb) + 1)
            if mask.crop(box).getbbox() is not None:
                hot.add(n.id)
        # the deepest changed nodes: none of their descendants changed
        leaves = [nid for nid in hot
                  if not any(c in hot for c in b.tree("ui").children.get(nid, ()))]
        order = {nid: i for i, nid in enumerate(b.nodes)}
        leaves.sort(key=lambda nid: order.get(nid, 0))
        boxes = [b.nodes[nid] for nid in leaves[:max_boxes]]
        more = max(0, len(leaves) - max_boxes)

    params = {"a": _capture_id(la, a), "refs": list(refs or []), "t": threshold, "m": max_side}
    name = f"img/pdiff-{_fname(str(_capture_id(la, a)))}-{_hash(params)}.png"

    def make() -> bytes:
        import io

        W, H = ib.size
        red = Image.new("RGBA", (W, H), (230, 40, 40, 255))
        dark = Image.new("RGBA", (W, H), (0, 0, 0, 255))
        mask_img = Image.composite(red, dark, mask)
        sheet = Image.new("RGBA", (W * 3 + 16, H), (255, 255, 255, 255))
        sheet.paste(ia, (0, 0))
        sheet.paste(ib, (W + 8, 0))
        sheet.paste(mask_img, (2 * W + 16, 0))
        draw = ImageDraw.Draw(sheet)
        for n in boxes:
            r = _rect(n.b)
            if r is None:
                continue
            x, y = int(r[0] * fb), int(r[1] * fb)
            w, h = max(1, int(r[2] * fb)), max(1, int(r[3] * fb))
            for dx in (0, W + 8):
                draw.rectangle([dx + x, y, dx + x + w, y + h], outline=(230, 40, 40, 255),
                               width=3)
            draw.text((W + 8 + x + 4, y + 2), _ref(n), fill=(230, 40, 40, 255))
        buf = io.BytesIO()
        sheet.convert("RGB").save(buf, "PNG", compress_level=6)
        return buf.getvalue()

    path, _ = _artifact(lb, name, make)
    out: dict[str, Any] = {"a": _capture_id(la, a), "b": _capture_id(lb, b),
                           "kind": "pixel_diff", "path": path, "px": list(png_size(path)),
                           "changed_px": int(changed_px),
                           "changed": round(changed_px / total_px, 4) if total_px else 0.0,
                           "nodes": [_ref(n) for n in boxes]}
    if bbox is not None:
        out["bbox"] = [int(bbox[0] / fb), int(bbox[1] / fb), int((bbox[2] - bbox[0]) / fb),
                       int((bbox[3] - bbox[1]) / fb)]
    if more:
        out["more"] = more
    return out


__all__ = [
    "DEFAULT_MAX_SIDE", "DEFAULT_PAD", "MAX_MARKS", "OVERLAY_KINDS", "crop", "decode_png",
    "encode_png", "inline", "overlay", "pixel_diff", "png_size", "resize",
]
