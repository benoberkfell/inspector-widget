"""Session lifecycle, offline: deadlines, poisoning, liveness, retry, detach
semantics, serial resolution, cleanup and the JSON-RPC fallback's robustness.

Runs against the fake agent + fake adb in ``tests/fakeagent.py`` (see
``test_e2e_fake_agent.py`` for the per-tool coverage). Ledger ids: H1, H3, E2,
E4/H5, E5, E7, E9, E11, E12, H9, H10.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import fakeagent
from fakeagent import DEFAULT_PACKAGE as PKG
from fakeagent import DEFAULT_PID as PID
from fakeagent import DEFAULT_SERIAL as SERIAL

import inspector_widget as iw
import mcp_server
from inspector_widget import adb, client as clientmod, inject
from inspector_widget.client import (AgentTimeoutError, Client, ClientError, SessionLostError,
                                     TransportError)
from inspector_widget.proto import view_inspection_pb2 as pb

HOST_DIR = Path(__file__).resolve().parents[1]


def _request(command, **fields):
    req = pb.Request()
    getattr(req, command).SetInParent()
    for k, v in fields.items():
        setattr(getattr(req, command), k, v)
    return req


@pytest.fixture
def agent():
    a = fakeagent.FakeAgent()
    socks = []

    def connect():
        s = socket.create_connection(("127.0.0.1", a.port), timeout=5)
        socks.append(s)
        return Client(s)

    a.connect = connect
    yield a
    a.stop()
    for s in socks:
        s.close()


# =========================================================================== #
# H1: deadlines
# =========================================================================== #
def test_request_deadlines_by_command(monkeypatch):
    monkeypatch.delenv(clientmod.TIMEOUT_ENV, raising=False)
    monkeypatch.delenv(clientmod.LEGACY_TIMEOUT_ENV, raising=False)
    t = clientmod.request_timeout
    assert t(_request("get_properties")) == 30.0
    assert t(_request("dump_tree")) == 30.0
    assert t(_request("hello")) == 10.0
    for slow in ("screenshot", "capture_skp", "dump_compose", "dump_a11y"):
        assert t(_request(slow)) == 120.0, slow
    assert t(_request("dump_tree", include_screenshot=True)) == 120.0
    assert t(_request("dump_tree", include_properties=True)) == 120.0
    monkeypatch.setenv(clientmod.TIMEOUT_ENV, "2")
    assert (t(_request("get_windows")), t(_request("screenshot")), t(_request("hello"))) == (2, 8, 2)
    monkeypatch.setenv(clientmod.TIMEOUT_ENV, "0")
    assert t(_request("screenshot")) is None  # 0 disables the deadline
    monkeypatch.setenv(clientmod.TIMEOUT_ENV, "soon")
    assert t(_request("get_windows")) == 30.0  # garbage falls back to the default
    monkeypatch.delenv(clientmod.TIMEOUT_ENV)
    monkeypatch.setenv(clientmod.LEGACY_TIMEOUT_ENV, "3")
    assert t(_request("get_windows")) == 3.0


def test_a_hung_request_times_out_and_poisons_the_client(agent):
    agent.behaviour = lambda req: ((0, "hang") if req.WhichOneof("command") == "get_windows"
                                   else agent.default_behaviour(req))
    client = agent.connect()
    client._timeout = 0.3
    started = time.monotonic()
    with pytest.raises(AgentTimeoutError, match="did not reply to get_windows within 0.3s"):
        client.get_windows()
    assert time.monotonic() - started < 2
    assert client.broken and not client.is_open()
    sent = len(agent.requests)
    with pytest.raises(SessionLostError, match="no reply to get_windows"):
        client.hello()  # refused locally: the stream may still carry the late reply
    assert len(agent.requests) == sent


def test_a_slow_trickle_still_meets_the_whole_frame_deadline(agent):
    resp = agent.dispatch(_request("hello"))
    frame = fakeagent.frame(resp)
    agent.behaviour = lambda req: (0, frame[:6])  # half a header, then nothing
    client = agent.connect()
    with pytest.raises(AgentTimeoutError):
        client.hello(timeout=0.3)


# =========================================================================== #
# H3: poisoning + id-0 errors
# =========================================================================== #
def test_eof_poisons_the_client(agent):
    client = agent.connect()
    client.hello()
    agent.kill_clients()
    with pytest.raises(SessionLostError, match="agent session lost"):
        client.get_windows()
    assert client.broken
    with pytest.raises(SessionLostError):
        client.get_windows()


def test_is_open_sees_eof_and_stray_bytes_without_a_round_trip(agent):
    client = agent.connect()
    client.hello()
    assert client.is_open()
    agent.kill_clients()
    deadline = time.monotonic() + 2
    while client.is_open() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not client.is_open() and "closed the connection" in client.broken

    other = agent.connect()
    agent.behaviour = lambda req: (0, fakeagent.frame(agent.dispatch(req)) * 2)  # a duplicate reply
    other.hello()
    time.sleep(0.05)
    assert not other.is_open() and "out of sync" in other.broken


def test_an_agent_error_with_id_zero_keeps_the_connection(agent):
    agent.behaviour = lambda req: (
        (0, fakeagent.error_response(0, "Malformed request: boom"))
        if req.WhichOneof("command") == "get_windows" else agent.default_behaviour(req))
    client = agent.connect()
    with pytest.raises(ClientError, match="Malformed request: boom"):
        client.get_windows()
    assert not client.broken
    assert client.hello().agent_version.startswith("viewspector-")


def test_a_mismatched_reply_id_poisons_the_client(agent):
    agent.behaviour = lambda req: (0, pb.Response(id=req.id + 7, status=pb.Response.OK))
    client = agent.connect()
    with pytest.raises(SessionLostError, match="expected 1, got 8"):
        client.hello()
    assert client.broken


def test_shutdown_on_a_closed_client_says_nothing_was_sent(agent):
    client = agent.connect()
    client.hello()  # the connection is registered with the agent
    agent.kill_clients()
    with pytest.raises(SessionLostError):
        client.get_windows()
    with pytest.raises(SessionLostError):
        client.shutdown()
    assert agent.running
