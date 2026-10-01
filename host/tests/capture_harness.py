"""The capture-and-walk tools over the harness fake adb and agent.

``harness(scene, tmp)`` is ``record_goldens.harness`` for a scene *object*: the
fake device's agent serves a :class:`fakescenes.SceneData` (the recorded launcher
and View-screen replays, the 259-view wide scene, C4's mixed hierarchy) over real
TCP, so the real attach / Session / Client / framing code runs, and a test can
change the scene between captures (``tap_switch``). ``ops_context()`` builds an
OpContext over the test's store with an :class:`ops.AttachProvider`, which
attaches through ``inspector_widget.attach`` like the CLI does.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import threading
from collections.abc import Iterator
from typing import Any

import fakeagent
import fakescenes
import pytest
from test_capture_pipeline_offline import mixed_scene_data, tap_switch  # noqa: F401

from inspector_widget import ops
from inspector_widget.capture.store import CaptureStore
from inspector_widget.output import dumps
from inspector_widget.proto import view_inspection_pb2 as pb

SERIAL = fakeagent.DEFAULT_SERIAL
PACKAGE = fakeagent.DEFAULT_PACKAGE
PID = fakeagent.DEFAULT_PID
SCENES = ("launcher", "viewscreen", "wide", "mixed")


def make_scene(name: str) -> fakescenes.SceneData:
    if name == "mixed":
        return mixed_scene_data()
    return fakescenes.scene(name)


def _with_build_id(scene: fakescenes.SceneData, build_id: str | None) -> Any:
    def behaviour(req: pb.Request) -> tuple:
        delay, resp = scene.behaviour(req)
        if build_id and req.WhichOneof("command") == "hello" and resp.HasField("hello"):
            resp.hello.agent_version = f"{fakeagent.AGENT_VERSION}+{build_id}"
        return delay, resp
    return behaviour


@contextlib.contextmanager
def harness(scene: str | fakescenes.SceneData, tmp: str, *, toolset: str | None = None
            ) -> Iterator[tuple[fakeagent.FakeDevice, fakescenes.SceneData]]:
    """A fake device with the probe app running and ``scene`` behind its agent;
    the capture store, temp dir, MCP session cache and environment all live under
    ``tmp`` and are undone on exit. ``toolset`` sets INSPECTOR_WIDGET_TOOLSET."""
    import mcp_server
    from inspector_widget import adb

    data = make_scene(scene) if isinstance(scene, str) else scene
    with pytest.MonkeyPatch.context() as mp:
        dev = fakeagent.default_device()
        fakeagent.install(mp, dev, build_out=os.path.join(tmp, "build-out"))
        dev.behaviour = _with_build_id(data, dev.default_build_id)
        tmpdir = os.path.join(tmp, "tmp")
        os.makedirs(tmpdir, exist_ok=True)
        mp.setattr(tempfile, "tempdir", tmpdir)
        cache = mcp_server.SessionCache()
        mp.setattr(mcp_server, "SESSIONS", cache)
        mp.setattr(mcp_server, "_closing", threading.Event())
        mp.setattr(mcp_server, "_OPS", None, raising=False)
        mp.setattr(adb, "_OWN_FORWARDS", {})
        mp.setattr(mcp_server._a11y_device_metrics, "_cache", {}, raising=False)
        mp.setenv("INSPECTOR_WIDGET_KEY_CACHE", os.path.join(tmp, "keys"))
        mp.setenv("INSPECTOR_WIDGET_CAPTURE_DIR", os.path.join(tmp, "store"))
        for var in ("INSPECTOR_WIDGET_MAX_BYTES", "ANDROID_SERIAL", "INSPECTOR_WIDGET_LOG",
                    "VIEWSPECTOR_LOG", "INSPECTOR_WIDGET_CAPTURE_PERSIST",
                    "INSPECTOR_WIDGET_TOOLSET", "INSPECTOR_WIDGET_SESSION"):
            mp.delenv(var, raising=False)
        if toolset:
            mp.setenv("INSPECTOR_WIDGET_TOOLSET", toolset)
        try:
            yield dev, data
        finally:
            for session in cache.all():
                injection = getattr(session, "injection", None)
                with contextlib.suppress(Exception):
                    if injection is not None:
                        injection.close()
            dev.close()


def ops_context(caller: str = "cli", store: CaptureStore | None = None) -> ops.OpContext:
    """An OpContext over the harness store (``$INSPECTOR_WIDGET_CAPTURE_DIR``)."""
    return ops.OpContext(store or CaptureStore(), ops.AttachProvider(), caller)


_AGO = re.compile(r"\b\d+[smhd] ago\b")


def same_moment(text: str) -> str:
    """``text`` with the wall-clock ages (``12s ago``) masked: two calls a second
    apart describe the same capture with different ages."""
    return _AGO.sub("<ago>", text)


def nbytes(obj: Any) -> int:
    return len(dumps(obj).encode("utf-8"))


def ok(doc: dict) -> dict:
    """``doc``, asserting it is not an error envelope."""
    assert not ops.is_error(doc), doc
    return doc
