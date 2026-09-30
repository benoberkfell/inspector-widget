"""Reusable per-component image client over Google's skiaparser gRPC server.

Given a device's serialized Skia picture (``CaptureSkpResponse.skp`` + ``version``) and a
list of requested nodes ``(id, x, y, w, h)`` — where ``id`` is a graphicsLayer/render-node
layer id (Compose) or a uniqueDrawingId (View) and the bounds are absolute screen px — this
launches (or reuses) the right ``skia-grpc-server`` for the SKP version (provisioned via
:mod:`inspector_widget.skiaparser`) and calls ``SkiaParserService.GetViewTree``. It walks the
returned ``InspectorView`` tree and returns one RGBA image per requested layer id:

    {layer_id: RgbaImage(width, height, rgba_bytes)}

This is the host side of the "SKP per-component image" flow described in integ.md §1.1/§1.3
("``render_node_id`` (graphicsLayer layerId) -> SKP draw layer via
``skia_client.GetViewTree(RequestedNodeInfo)``"). The skiaparser binary speaks raw RGBA in the
``InspectorView.image`` field (4 bytes/px, row-major), which we re-encode to PNG via Pillow when
available, else a tiny stdlib zlib encoder (so ``overlay`` is the only Pillow-gated feature).

Degrades gracefully: missing ``grpcio`` raises :class:`SkiaClientError` with an install hint;
an unsupported/empty SKP surfaces as an error the caller turns into a BITMAP-crop fallback.
"""
from __future__ import annotations

import os
import struct
import sys
import threading
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .skiaparser import SkiaParser, SkiaParserError

# Skiaparser ships large pictures back; raise gRPC's 4 MB default ceiling generously.
_MAX_MESSAGE_BYTES = 256 * 1024 * 1024  # 256 MB
# Per-call deadline; SKP decode of a full window is fast but allow headroom.
_RPC_TIMEOUT_S = 60.0


class SkiaClientError(RuntimeError):
    """Raised when the per-component image path is unavailable or fails."""


@dataclass
class RgbaImage:
    """A decoded raster: ``rgba`` is ``width*height*4`` bytes, row-major R,G,B,A."""
    width: int
    height: int
    rgba: bytes

    def to_png_bytes(self) -> bytes:
        return _rgba_to_png(self.rgba, self.width, self.height)

    def save_png(self, path: str) -> str:
        with open(path, "wb") as fh:
            fh.write(self.to_png_bytes())
        return path


# --------------------------------------------------------------------------- #
# gRPC stub import. The generated skia_pb2_grpc.py does `import skia_pb2`
# (absolute), so the skia_grpc dir must be importable. We add it to sys.path
# lazily, only when images are actually requested, to keep `import
# inspector_widget` lightweight and grpc-free.
# --------------------------------------------------------------------------- #
def _import_skia_grpc():
    skia_dir = Path(__file__).resolve().parent / "skia_grpc"
    p = str(skia_dir)
    if p not in sys.path:
        sys.path.insert(0, p)
    try:
        import grpc  # noqa: F401
    except Exception as exc:  # pragma: no cover - environment-dependent
        raise SkiaClientError(
            "grpcio>=1.81.0 is required for SKP per-component images "
            "(pip install 'inspector-widget[images]', or pip install 'grpcio>=1.81.0'); "
            "falling back to BITMAP crop. "
            f"({exc})"
        ) from exc
    try:
        import skia_pb2  # type: ignore
        import skia_pb2_grpc  # type: ignore
    except Exception as exc:  # pragma: no cover - generated stubs missing
        raise SkiaClientError(f"skia gRPC stubs unavailable: {exc}") from exc
    return grpc, skia_pb2, skia_pb2_grpc


# --------------------------------------------------------------------------- #
# Managed skiaparser server: launch once per SKP-server-version and reuse for
# subsequent requests within the process (so repeated inspect_node calls don't
# pay startup cost each time).
# --------------------------------------------------------------------------- #
class _ServerHandle:
    def __init__(self, proc, port: int):
        self.proc = proc
        self.port = port

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


class SkiaImageClient:
    """Provisions/launches a skiaparser server and cuts per-component images from an SKP."""

    def __init__(self, parser: Optional[SkiaParser] = None):
        self._parser = parser or SkiaParser()
        self._servers: Dict[int, _ServerHandle] = {}  # server_version -> handle
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ server
    def _server_for_skp(self, skp_version: int) -> _ServerHandle:
        """Return a live server able to decode ``skp_version``, launching if needed."""
        try:
            server_version = self._parser.server_for_skp(skp_version)
        except SkiaParserError as exc:
            raise SkiaClientError(str(exc)) from exc
        with self._lock:
            handle = self._servers.get(server_version)
            if handle is not None and handle.alive():
                return handle
            if handle is not None:
                self._servers.pop(server_version, None)
            try:
                binary = self._parser.ensure(server_version)
                proc, port = self._parser.launch(binary)
            except SkiaParserError as exc:
                raise SkiaClientError(str(exc)) from exc
            handle = _ServerHandle(proc, port)
            self._servers[server_version] = handle
            return handle

    def close(self) -> None:
        """Terminate any launched skiaparser servers."""
        with self._lock:
            for handle in self._servers.values():
                try:
                    if handle.alive():
                        handle.proc.terminate()
                except Exception:  # pragma: no cover - best-effort teardown
                    pass
            self._servers.clear()

    def __enter__(self) -> "SkiaImageClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------ images
    def per_component_images(
        self,
        skp: bytes,
        version: int,
        nodes: Sequence[Tuple[int, int, int, int, int]],
        scale: float = 1.0,
    ) -> Dict[int, RgbaImage]:
        """Cut one RGBA image per requested node from ``skp``.

        ``nodes`` is a sequence of ``(id, x, y, w, h)`` (RequestedNodeInfo) with ``id`` =
        graphicsLayer layerId / render-node id and bounds in absolute screen px. Returns
        ``{id: RgbaImage}`` for every layer the parser actually produced an image for.
        Raises :class:`SkiaClientError` if the path is unavailable (caller falls back to crop).
        """
        if not skp:
            raise SkiaClientError("empty SKP (capture unsupported on this device)")
        requested = [n for n in nodes if n and int(n[3]) > 0 and int(n[4]) > 0]
        if not requested:
            return {}

        grpc, skia_pb2, _ = _import_skia_grpc()
        if not version:
            version = _read_skp_version(skp)
        handle = self._server_for_skp(version)

        req = skia_pb2.GetViewTreeRequest()
        req.skp = bytes(skp)
        req.version = int(version)
        req.scale = float(scale) if scale else 1.0
        req.total_size = len(skp)
        for (nid, x, y, w, h) in requested:
            rn = req.requested_nodes.add()
            rn.id = int(nid)
            rn.x = int(x)
            rn.y = int(y)
            rn.width = int(w)
            rn.height = int(h)

        options = [
            ("grpc.max_receive_message_length", _MAX_MESSAGE_BYTES),
            ("grpc.max_send_message_length", _MAX_MESSAGE_BYTES),
        ]
        target = f"127.0.0.1:{handle.port}"
        try:
            with grpc.insecure_channel(target, options=options) as channel:
                from skia_pb2_grpc import SkiaParserServiceStub  # type: ignore
                stub = SkiaParserServiceStub(channel)
                resp = stub.GetViewTree(req, timeout=_RPC_TIMEOUT_S)
        except grpc.RpcError as exc:  # pragma: no cover - device/server-dependent
            raise SkiaClientError(f"skiaparser GetViewTree failed: {exc}") from exc

        wanted = {int(n[0]) for n in requested}
        out: Dict[int, RgbaImage] = {}
        if resp.HasField("root"):
            _collect_images(resp.root, wanted, out)
        return out


# --------------------------------------------------------------------------- helpers
def _collect_images(view, wanted: set, out: Dict[int, RgbaImage]) -> None:
    """Walk the InspectorView tree, decoding image bytes for every wanted layer id."""
    vid = int(getattr(view, "id", 0))
    img = bytes(getattr(view, "image", b"") or b"")
    if vid in wanted and img:
        w = int(view.width)
        h = int(view.height)
        if w > 0 and h > 0:
            # skiaparser emits raw RGBA (4 B/px). Guard against short buffers.
            expected = w * h * 4
            if len(img) >= expected:
                out[vid] = RgbaImage(w, h, img[:expected])
    for child in getattr(view, "children", []) or []:
        _collect_images(child, wanted, out)


def _read_skp_version(skp: bytes) -> int:
    """Best-effort SKP version from the 'skiapict' header (magic + LE uint32 version)."""
    # Header: 8-byte magic "skiapict" then a little-endian uint32 version.
    if len(skp) >= 12 and skp[:8] == b"skiapict":
        return struct.unpack_from("<I", skp, 8)[0]
    raise SkiaClientError("could not read SKP version from header; pass version explicitly")


def _rgba_to_png(rgba: bytes, width: int, height: int) -> bytes:
    """Encode RGBA8888 pixels to PNG. Pillow if present, else stdlib zlib."""
    try:
        from PIL import Image  # type: ignore

        return _pillow_png(Image, rgba, width, height)
    except ImportError:
        return _stdlib_png(rgba, width, height)


def _pillow_png(Image, rgba: bytes, width: int, height: int) -> bytes:
    import io

    img = Image.frombytes("RGBA", (width, height), bytes(rgba))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _stdlib_png(rgba: bytes, width: int, height: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 per scanline
        start = y * stride
        raw += rgba[start:start + stride]
    idat = zlib.compress(bytes(raw), 9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


# A process-wide default client so repeated MCP/CLI calls reuse one launched server.
_DEFAULT_CLIENT: Optional[SkiaImageClient] = None
_DEFAULT_LOCK = threading.Lock()


def default_client() -> SkiaImageClient:
    """Return a shared :class:`SkiaImageClient` (one launched server per process)."""
    global _DEFAULT_CLIENT
    with _DEFAULT_LOCK:
        if _DEFAULT_CLIENT is None:
            _DEFAULT_CLIENT = SkiaImageClient()
        return _DEFAULT_CLIENT


def per_component_images(
    skp: bytes,
    nodes: Sequence[Tuple[int, int, int, int, int]],
    version: int = 0,
    scale: float = 1.0,
    client: Optional[SkiaImageClient] = None,
) -> Dict[int, RgbaImage]:
    """Module-level convenience over :meth:`SkiaImageClient.per_component_images`.

    ``nodes``: sequence of ``(id, x, y, w, h)``. Uses a shared server unless ``client`` given.
    """
    cl = client or default_client()
    return cl.per_component_images(skp, version, nodes, scale=scale)
