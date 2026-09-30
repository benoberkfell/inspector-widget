"""Shared pytest fixtures + proto-building helpers for the host test suite.

Runs entirely with NO device: every test here builds protobuf messages by hand
(via ``view_inspection_pb2``) or feeds synthetic dicts to the pure host logic.

The one device-touching test (``test_smoke_emulator.py``) is gated behind the
``device`` marker and auto-skips when no adb/emulator is present.

Importing strategy: the package is ``inspector_widget`` and lives under ``host/``.
We make sure ``host/`` is on ``sys.path`` so ``import inspector_widget`` resolves
against the working tree regardless of how pytest is invoked.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from typing import Dict, Iterable, List, Optional, Tuple

import pytest

# --------------------------------------------------------------------------- #
# Make the in-tree inspector_widget package importable (host/ on sys.path).
# conftest.py lives at host/tests/conftest.py -> host/ is its parent's parent.
# --------------------------------------------------------------------------- #
_HOST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HOST_DIR not in sys.path:
    sys.path.insert(0, _HOST_DIR)

from inspector_widget.proto import view_inspection_pb2 as pb  # noqa: E402


# --------------------------------------------------------------------------- #
# String-table builder.
#
# Every "*_id" / interned text field in the protocol is an int32 index into a
# Strings table (id 0 == empty/absent). This helper interns text deterministically
# and hands back both the Strings message and an intern() function so tests can
# build nodes that reference real ids.
# --------------------------------------------------------------------------- #
class StringTableBuilder:
    """Builds a ``pb.Strings`` table, assigning ids 1.. to interned text."""

    def __init__(self) -> None:
        self._by_text: Dict[str, int] = {"": 0}
        self._next = 1

    def intern(self, text: Optional[str]) -> int:
        """Return the id for ``text`` (0 for None/empty), assigning a new id once."""
        if not text:
            return 0
        if text in self._by_text:
            return self._by_text[text]
        sid = self._next
        self._next += 1
        self._by_text[text] = sid
        return sid

    def build(self) -> "pb.Strings":
        strings = pb.Strings()
        for text, sid in sorted(self._by_text.items(), key=lambda kv: kv[1]):
            if sid == 0:
                continue
            strings.entries.add(id=sid, str=text)
        return strings


@pytest.fixture
def strings_builder() -> StringTableBuilder:
    return StringTableBuilder()


# --------------------------------------------------------------------------- #
# Low-level message builders (used across test_proto / test_strings / test_a11y).
# --------------------------------------------------------------------------- #
def make_rect(x: int = 0, y: int = 0, w: int = 0, h: int = 0) -> "pb.Rect":
    return pb.Rect(x=x, y=y, w=w, h=h)


def make_bounds(x: int = 0, y: int = 0, w: int = 0, h: int = 0,
                render: Optional["pb.Quad"] = None) -> "pb.Bounds":
    b = pb.Bounds(layout=make_rect(x, y, w, h))
    if render is not None:
        b.render.CopyFrom(render)
    return b


def make_view_node(
    sb: StringTableBuilder,
    *,
    node_id: int,
    class_name: str,
    package_name: Optional[str] = None,
    bounds: Tuple[int, int, int, int] = (0, 0, 0, 0),
    text: Optional[str] = None,
    view_id_name: Optional[str] = None,
    flags: int = 0,
    children: Optional[Iterable["pb.ViewNode"]] = None,
) -> "pb.ViewNode":
    node = pb.ViewNode(
        id=node_id,
        class_name=sb.intern(class_name),
        package_name=sb.intern(package_name),
        bounds=make_bounds(*bounds),
        view_id_name=sb.intern(view_id_name),
        text_value=sb.intern(text),
        flags=flags,
    )
    for c in children or []:
        node.children.add().CopyFrom(c)
    return node


def make_compose_node(
    sb: StringTableBuilder,
    *,
    node_id: int,
    name: str,
    kind: int = pb.ComposeNode.SEMANTICS,
    bounds: Tuple[int, int, int, int] = (0, 0, 0, 0),
    source: Optional[str] = None,
    render_node_id: int = 0,
    attrs: Optional[Dict[str, str]] = None,
    children: Optional[Iterable["pb.ComposeNode"]] = None,
) -> "pb.ComposeNode":
    node = pb.ComposeNode(
        id=node_id,
        name=sb.intern(name),
        kind=kind,
        bounds=make_bounds(*bounds),
        source=sb.intern(source),
        render_node_id=render_node_id,
    )
    for k, v in (attrs or {}).items():
        node.attrs.add(key=sb.intern(k), value=sb.intern(v))
    for c in children or []:
        node.children.add().CopyFrom(c)
    return node


def make_a11y_node(
    sb: StringTableBuilder,
    *,
    host_view_id: int = 0,
    virtual_id: int = 0,
    bounds: Tuple[int, int, int, int] = (0, 0, 0, 0),
    text: Optional[str] = None,
    content_description: Optional[str] = None,
    state_description: Optional[str] = None,
    role_description: Optional[str] = None,
    class_name: Optional[str] = None,
    actions: Optional[Iterable[Tuple[int, Optional[str]]]] = None,
    bool_flags: Optional[Iterable[str]] = None,
    int_fields: Optional[Dict[str, int]] = None,
    traversal_before: int = 0,
    traversal_after: int = 0,
    children: Optional[Iterable["pb.A11yNode"]] = None,
) -> "pb.A11yNode":
    node = pb.A11yNode(
        host_view_id=host_view_id,
        virtual_id=virtual_id,
        bounds=make_bounds(*bounds),
        text=sb.intern(text),
        content_description=sb.intern(content_description),
        state_description=sb.intern(state_description),
        role_description=sb.intern(role_description),
        class_name=sb.intern(class_name),
        traversal_before=traversal_before,
        traversal_after=traversal_after,
    )
    for flag in bool_flags or []:
        setattr(node, flag, True)
    for field, value in (int_fields or {}).items():
        setattr(node, field, value)
    for action_id, label in actions or []:
        node.actions.add(id=action_id, label=sb.intern(label))
    for c in children or []:
        node.children.add().CopyFrom(c)
    return node


# --------------------------------------------------------------------------- #
# Packed a11y id used by traversal_before/after + the synthetic "id" field
# (host_view_id << 32) ^ (virtual_id & 0xffffffff) — mirrors a11y.a11y_node_to_dict.
# --------------------------------------------------------------------------- #
def packed_a11y_id(host_view_id: int, virtual_id: int) -> int:
    return (host_view_id << 32) ^ (virtual_id & 0xFFFFFFFF)


# --------------------------------------------------------------------------- #
# Device detection for the optional smoke test.
# --------------------------------------------------------------------------- #
def adb_available() -> bool:
    return shutil.which("adb") is not None


def device_present(serial: str = "emulator-5554") -> bool:
    if not adb_available():
        return False
    try:
        out = subprocess.run(
            ["adb", "devices"], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return False
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[0] == serial and parts[1] == "device":
            return True
    return False


# --------------------------------------------------------------------------- #
# Offline end-to-end harness (tests/fakeagent.py).
#
# ``fake_device`` routes every adb subprocess to an in-process FakeDevice whose
# agents speak the real VWSPCT01 protocol over real TCP, so the REAL
# inject_and_connect / _try_warm_connect / Injection / Session / Client /
# correlate / overlay code runs. Everything is monkeypatch-scoped: the adb
# patch, the build-out default, tempfile's directory (tool PNGs land in the
# test's tmp dir), and mcp_server's module-level session + density caches.
# --------------------------------------------------------------------------- #
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

FAKE_SERIAL = "emulator-5554"
FAKE_PACKAGE = "com.oberkfell.a11yprobe"


@pytest.fixture
def fake_device(monkeypatch, tmp_path):
    """A FakeDevice with com.oberkfell.a11yprobe running (pid 4242), no agent yet."""
    import tempfile

    import fakeagent
    import mcp_server

    dev = fakeagent.default_device()
    dev.fake_adb = fakeagent.install(monkeypatch, dev, build_out=str(tmp_path / "build-out"))
    dev.tmpdir = tmp_path / "tmp"
    dev.tmpdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(dev.tmpdir))
    cache = mcp_server.SessionCache()
    monkeypatch.setattr(mcp_server, "SESSIONS", cache)
    # A test that runs the exit cleanup leaves the server "closing" (no more
    # attaches); every test starts with a fresh flag.
    monkeypatch.setattr(mcp_server, "_closing", threading.Event())
    # Forwards this process "made" (adb.remove_own_forwards cleans them up at
    # exit) belong to this test's fake device only.
    from inspector_widget import adb
    monkeypatch.setattr(adb, "_OWN_FORWARDS", {})
    monkeypatch.setattr(mcp_server._a11y_device_metrics, "_cache", {}, raising=False)
    try:
        yield dev
    finally:
        for session in cache.all():  # close host sockets without sending SHUTDOWN
            injection = getattr(session, "injection", None)
            try:
                if injection is not None:
                    injection.close()
            except Exception:
                pass
        dev.close()


@pytest.fixture
def warm_agent(fake_device):
    """An agent already injected into the probe app (as if by an earlier run)."""
    return fake_device.start_agent(FAKE_PACKAGE)


@pytest.fixture
def fast_sleep(monkeypatch):
    """Make inject's socket-wait / connect-retry backoff instant."""
    import time
    import types

    from inspector_widget import inject

    proxy = types.SimpleNamespace(**{k: getattr(time, k) for k in dir(time) if not k.startswith("_")})
    proxy.sleep = lambda _s: None
    monkeypatch.setattr(inject, "time", proxy)


class CliRun:
    def __init__(self, rc, out, err):
        self.rc, self.out, self.err = rc, out, err

    def json(self):
        import json
        return json.loads(self.out)

    def __repr__(self):
        return f"CliRun(rc={self.rc}, out={self.out[:300]!r}, err={self.err[-300:]!r})"


@pytest.fixture
def run_cli(capsys):
    """Run ``cli.main(argv)`` in-process; returns CliRun(rc, out, err)."""
    import cli

    def _run(*argv):
        capsys.readouterr()
        rc = cli.main([str(a) for a in argv])
        cap = capsys.readouterr()
        return CliRun(rc, cap.out, cap.err)

    return _run


@pytest.fixture
def mcp(fake_device):
    """Call an MCP tool through mcp_server._run_tool (serial/package default in)."""
    import mcp_server

    def _call(tool, **args):
        if tool not in ("list_devices", "list_processes"):
            args.setdefault("serial", FAKE_SERIAL)
            args.setdefault("package", FAKE_PACKAGE)
        elif tool == "list_processes":
            args.setdefault("serial", FAKE_SERIAL)
        return mcp_server._run_tool(tool, args)

    return _call
