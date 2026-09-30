"""Guards around regenerating the protobuf bindings (device-free).

The checked-in ``view_inspection_pb2.py`` is stamped with the protobuf release
that produced it; the runtime pin (``protobuf>=6.33.5,<7``) must accept it. A
protoc from a newer major (34+ emits 7.x gencode) would produce a module the
pinned runtime refuses to import, so ``generate_proto.sh`` must refuse that
protoc, and ``make clean`` must never delete the tracked gencode.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess

import pytest

_HOST = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(_HOST)
_PB2_REL = os.path.join("inspector_widget", "proto", "view_inspection_pb2.py")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _gencode_version() -> str:
    with open(os.path.join(_HOST, _PB2_REL), encoding="utf-8") as f:
        m = re.search(r"^# Protobuf Python Version: (\d+)\.(\d+)\.(\d+)", f.read(), re.M)
    assert m, "view_inspection_pb2.py lost its '# Protobuf Python Version:' header"
    return ".".join(m.groups())


def _scratch_repo(tmp_path):
    """A copy of just what generate_proto.sh touches: proto/ + host/{script, pb2}."""
    shutil.copytree(os.path.join(_REPO, "proto"), tmp_path / "proto")
    host = tmp_path / "host"
    (host / "inspector_widget" / "proto").mkdir(parents=True)
    shutil.copy2(os.path.join(_HOST, "generate_proto.sh"), host / "generate_proto.sh")
    shutil.copy2(os.path.join(_HOST, "Makefile"), host / "Makefile")
    for name in ("__init__.py", "view_inspection_pb2.py"):
        shutil.copy2(os.path.join(_HOST, "inspector_widget", "proto", name),
                     host / "inspector_widget" / "proto" / name)
    return host


def _fake_protoc(tmp_path, version: str):
    """A protoc that reports ``version`` and records that it was asked to generate."""
    path = tmp_path / f"protoc-{version}"
    marker = tmp_path / f"generated-by-{version}"
    path.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then echo "libprotoc {version}"; exit 0; fi\n'
        f'touch "{marker}"\n'
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path), marker


def _run(host, protoc, **env):
    full_env = {k: v for k, v in os.environ.items() if k != "ALLOW_PROTOC_MISMATCH"}
    full_env.update(PROTOC=protoc, **env)
    return subprocess.run(["bash", str(host / "generate_proto.sh")],
                          capture_output=True, text=True, env=full_env)


def test_runtime_pins_accept_the_checked_in_gencode():
    major, minor, patch = (int(p) for p in _gencode_version().split("."))
    floor = f"protobuf>={major}.{minor}.{patch},<{major + 1}"
    for rel in ("requirements.txt", "pyproject.toml"):
        with open(os.path.join(_HOST, rel), encoding="utf-8") as f:
            assert floor in f.read(), f"{rel} should pin {floor} to match the gencode"


def test_refuses_protoc_from_a_different_major(tmp_path):
    host = _scratch_repo(tmp_path)
    pb2 = host / _PB2_REL
    before = pb2.read_bytes()
    protoc, marker = _fake_protoc(tmp_path, "35.1")
    res = _run(host, protoc)
    assert res.returncode == 1
    assert "protoc 35.1 does not match" in res.stderr
    assert "protoc 33.x" in res.stderr  # the header says 6.33.5 -> protoc 33
    assert not marker.exists(), "protoc must not be invoked to generate on a mismatch"
    assert pb2.read_bytes() == before


def test_accepts_matching_protoc_major(tmp_path):
    host = _scratch_repo(tmp_path)
    expected_major = _gencode_version().split(".")[1]
    protoc, marker = _fake_protoc(tmp_path, f"{expected_major}.9")
    res = _run(host, protoc)
    assert res.returncode == 0, res.stderr
    assert marker.exists()


def test_mismatch_override_is_explicit(tmp_path):
    host = _scratch_repo(tmp_path)
    protoc, marker = _fake_protoc(tmp_path, "35.1")
    res = _run(host, protoc, ALLOW_PROTOC_MISMATCH="1")
    assert res.returncode == 0, res.stderr
    assert "warning" in res.stderr and marker.exists()


def test_missing_gencode_is_an_error_not_a_guess(tmp_path):
    host = _scratch_repo(tmp_path)
    (host / _PB2_REL).unlink()
    protoc, marker = _fake_protoc(tmp_path, "33.5")
    res = _run(host, protoc)
    assert res.returncode == 1
    assert "git checkout -- host/inspector_widget/proto/view_inspection_pb2.py" in res.stderr
    assert not marker.exists()


def test_make_clean_keeps_the_tracked_gencode(tmp_path):
    if shutil.which("make") is None:
        pytest.skip("needs make")
    host = _scratch_repo(tmp_path)
    (host / "build").mkdir()
    res = subprocess.run(["make", "-s", "-C", str(host), "clean"], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert (host / _PB2_REL).is_file()
    assert not (host / "build").exists()
