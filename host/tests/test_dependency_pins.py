"""The declared grpcio floor must satisfy the checked-in gRPC stubs (device-free).

``skia_grpc/skia_pb2_grpc.py`` raises at import when ``grpc.__version__`` is older
than the ``GRPC_GENERATED_VERSION`` it was generated with, so every place that
declares grpcio (the ``images``/``all`` extras and requirements.txt) must floor it
at that version, and the missing-grpcio hint must name the real distribution.
"""

from __future__ import annotations

import os
import re

_HOST = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(_HOST, rel), encoding="utf-8") as f:
        return f.read()


def _version_tuple(v: str):
    return tuple(int(p) for p in v.split("."))


def _generated_grpc_version() -> str:
    m = re.search(r"^GRPC_GENERATED_VERSION = '([\d.]+)'",
                  _read("inspector_widget/skia_grpc/skia_pb2_grpc.py"), re.M)
    assert m, "skia_pb2_grpc.py lost its GRPC_GENERATED_VERSION line"
    return m.group(1)


def _grpcio_floors(text: str):
    return re.findall(r"""^\s*["']?grpcio>=([\d.]+)["']?,?\s*$|images = \["grpcio>=([\d.]+)"\]""",
                      text, re.M)


def test_every_grpcio_floor_meets_the_generated_stubs():
    needed = _version_tuple(_generated_grpc_version())
    for rel in ("pyproject.toml", "requirements.txt"):
        floors = [a or b for a, b in _grpcio_floors(_read(rel))]
        assert floors, f"no grpcio>= requirement found in {rel}"
        for floor in floors:
            assert _version_tuple(floor) >= needed, (
                f"{rel} floors grpcio at {floor}, but skia_pb2_grpc.py needs "
                f">= {_generated_grpc_version()}")


def test_pyproject_declares_grpcio_in_images_and_all():
    floors = [a or b for a, b in _grpcio_floors(_read("pyproject.toml"))]
    assert len(floors) == 2, f"expected grpcio in the images + all extras, got {floors}"


def test_missing_grpcio_hint_names_the_real_distribution():
    name = re.search(r'^name = "([^"]+)"', _read("pyproject.toml"), re.M).group(1)
    client = _read("inspector_widget/skia_client.py")
    assert f"pip install '{name}[images]'" in client
    assert "viewspector-host" not in client
    assert f"grpcio>={_generated_grpc_version()}" in client
