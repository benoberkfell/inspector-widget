"""Lifecycle polish, offline: no retry during exit cleanup or after a detach,
retries only for calls that are safe to repeat.

Runs against the fake agent + fake adb in ``tests/fakeagent.py``.
"""

from __future__ import annotations

import threading
import time

import pytest

from fakeagent import DEFAULT_PACKAGE as PKG
from fakeagent import DEFAULT_SERIAL as SERIAL

import mcp_server
from inspector_widget import adb, client as clientmod
from inspector_widget.client import NotSentError, SessionLostError
from inspector_widget.proto import view_inspection_pb2 as pb


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return predicate()


def _in_thread(fn):
    out = {}
    worker = threading.Thread(target=lambda: out.update(fn()), daemon=True)
    worker.start()
    return worker, out


def _hang_on(agent, command):
    agent.behaviour = lambda req: ((0, "hang") if req.WhichOneof("command") == command
                                   else agent.default_behaviour(req))


def _drop_first(agent, command):
    """Close the connection instead of answering the first ``command``."""
    dropped = {"n": 0}

    def behaviour(req):
        if req.WhichOneof("command") == command and not dropped["n"]:
            dropped["n"] += 1
            return 0, "close"
        return agent.default_behaviour(req)

    agent.behaviour = behaviour


# =========================================================================== #
# 1. Exit cleanup and detach never let a retry re-attach
# =========================================================================== #
def test_exit_cleanup_mid_call_does_not_reattach(mcp, fake_device, monkeypatch):
    """The reviewer's exit race: cleanup aborts a call in flight; its retry
    used to re-attach behind the cleanup, leaving a cached session and a
    forward nobody removed."""
    monkeypatch.setenv(clientmod.TIMEOUT_ENV, "5")
    assert mcp("attach")["attached"]
    _hang_on(fake_device.agent(), "screenshot")
    worker, result = _in_thread(lambda: mcp("screenshot"))
    assert _wait_until(lambda: "screenshot" in fake_device.commands())
    mcp_server._cleanup_at_exit()
    worker.join(5)
    assert not worker.is_alive()
    assert "agent session lost" in result["error"], result
    assert fake_device.commands().count("hello") == 1  # no re-attach
    assert mcp_server.SESSIONS.all() == []
    assert fake_device.forward_names() == []


def test_nothing_attaches_once_exit_cleanup_began(mcp, fake_device):
    mcp_server._cleanup_at_exit()
    res = mcp("dump_tree")
    assert res["error"] == "the MCP server is shutting down; not attaching"
    assert fake_device.wire == [] and fake_device.forward_names() == []


class _FakeSession:
    """A host-Session stand-in (as in the reviewer's exit_race.py)."""

    def __init__(self, n):
        self.n = n
        self.gone = threading.Event()
        self.note = None
        self.disconnects = 0

    def is_alive(self):
        return not self.gone.is_set()

    def disconnect(self):
        self.disconnects += 1
        self.gone.set()

    def shutdown(self):
        self.gone.set()  # the agent stops: a request in flight loses its connection
        return True

    def screenshot(self, root_id=0, scale=1.0):
        if self.n == 0:
            self.gone.wait(5)
            raise SessionLostError("agent session lost: disconnected")

        class _NoShot:
            def HasField(self, _field):
                return False

        return _NoShot()


def test_an_attach_that_finishes_after_cleanup_is_released_not_cached(monkeypatch):
    monkeypatch.setattr(mcp_server, "SESSIONS", mcp_server.SessionCache())
    monkeypatch.setattr(mcp_server, "_closing", threading.Event())
    monkeypatch.setattr(adb, "remove_own_forwards", lambda *a, **k: 0)
    entered, release, made = threading.Event(), threading.Event(), []

    def slow_attach(serial, package, force_reinject=False):
        entered.set()
        release.wait(5)
        made.append(_FakeSession(1))
        return made[-1]

    monkeypatch.setattr(mcp_server.HOST, "attach", slow_attach)
    worker, result = _in_thread(lambda: mcp_server._run_tool(
        "screenshot", {"serial": SERIAL, "package": PKG}))
    assert entered.wait(5)
    mcp_server._cleanup_at_exit()  # empties the cache while the attach still runs
    release.set()
    worker.join(5)
    assert result["error"] == "the MCP server is shutting down; not attaching"
    assert made[0].disconnects == 1 and mcp_server.SESSIONS.all() == []


def test_a_call_that_loses_its_session_to_detach_does_not_reinject(monkeypatch):
    monkeypatch.setattr(mcp_server, "SESSIONS", mcp_server.SessionCache())
    monkeypatch.setattr(mcp_server, "_closing", threading.Event())
    attaches = []

    def fake_attach(serial, package, force_reinject=False):
        attaches.append(_FakeSession(len(attaches)))
        return attaches[-1]

    monkeypatch.setattr(mcp_server.HOST, "attach", fake_attach)
    worker, result = _in_thread(lambda: mcp_server._run_tool(
        "screenshot", {"serial": SERIAL, "package": PKG}))
    assert _wait_until(lambda: attaches)
    res = mcp_server._run_tool("detach", {"serial": SERIAL, "package": PKG})
    assert res["agent_stopped"] is True
    worker.join(5)
    assert "agent session lost" in result["error"], result
    assert len(attaches) == 1  # the retry did not attach (inject) again
    assert mcp_server.SESSIONS.all() == []
    # The next call is a new request: it attaches as usual.
    assert mcp_server._run_tool("screenshot", {"serial": SERIAL, "package": PKG})["error"] \
        == "agent returned no screenshot"
    assert len(attaches) == 2


def test_detach_waits_for_an_attach_to_the_same_app(mcp, fake_device, monkeypatch):
    """detach holds the app's session lock: it can't run between an attach
    and the moment that attach caches its session."""
    entered, release = threading.Event(), threading.Event()
    real_attach = mcp_server.HOST.attach

    def slow_attach(*args, **kwargs):
        entered.set()
        release.wait(5)
        return real_attach(*args, **kwargs)

    monkeypatch.setattr(mcp_server.HOST, "attach", slow_attach)
    attaching, attached = _in_thread(lambda: mcp("attach"))
    assert entered.wait(5)
    detaching, detached = _in_thread(lambda: mcp("detach"))
    time.sleep(0.2)
    assert detaching.is_alive()  # queued behind the attach
    release.set()
    attaching.join(5)
    detaching.join(5)
    assert attached["attached"] and detached["agent_stopped"] is True
    assert mcp_server.SESSIONS.all() == [] and fake_device.agent() is None


# =========================================================================== #
# 2. Only calls that are safe to repeat are retried
# =========================================================================== #
def test_a_destructive_dump_compose_is_not_resent(mcp, fake_device, warm_agent):
    _drop_first(warm_agent, "dump_compose")
    res = mcp("dump_compose", enable_inspection=True)
    assert "agent session lost" in res["error"], res
    assert fake_device.commands().count("dump_compose") == 1  # hot-reloaded once, at most


def test_a_read_only_dump_compose_is_retried(mcp, fake_device, warm_agent):
    _drop_first(warm_agent, "dump_compose")
    res = mcp("dump_compose")
    assert "error" not in res, res
    assert fake_device.commands().count("dump_compose") == 2


def test_a_destructive_call_that_was_never_sent_is_retried(mcp, fake_device, warm_agent,
                                                           monkeypatch):
    """NotSentError proves the agent never saw the request, so sending it on a
    fresh connection is its first delivery, not a repeat."""
    real_write = clientmod.framing.write_message
    failed = {"n": 0}

    def write_fails_once(sock, payload):
        command = pb.Request.FromString(payload).WhichOneof("command")
        if command == "dump_compose" and not failed["n"]:
            failed["n"] += 1
            raise BrokenPipeError("broken pipe")
        return real_write(sock, payload)

    monkeypatch.setattr(clientmod.framing, "write_message", write_fails_once)
    res = mcp("dump_compose", enable_inspection=True)
    assert failed["n"] == 1 and "error" not in res, res
    assert fake_device.commands().count("dump_compose") == 1
    assert fake_device.commands().count("hello") == 2  # reconnected (warm) for the retry


def test_retry_policy_table():
    call = mcp_server._CallState()
    lost, not_sent = SessionLostError("lost"), NotSentError("not sent")
    refusal = mcp_server._retry_refusal
    assert refusal("dump_tree", {}, lost, call) is None
    assert refusal("dump_compose", {"enable_inspection": False}, lost, call) is None
    assert refusal("dump_compose", {"enable_inspection": True}, lost, call) is not None
    assert refusal("dump_compose", {"enable_inspection": True}, not_sent, call) is None
    for tool in ("attach", "detach"):
        assert refusal(tool, {}, not_sent, call) is not None
    # Every tool is classified: read-only, or one that is not simply repeated.
    unlisted = set(mcp_server.TOOLS) - mcp_server._READ_ONLY_TOOLS - mcp_server._NO_RETRY
    assert unlisted == {"dump_compose"}
