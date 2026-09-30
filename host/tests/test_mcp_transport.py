"""MCP transport: the server must start, list tools, and flag tool errors with
``isError: true`` under every transport it supports — the installed ``mcp`` SDK
(1.x decorator API or 2.x constructor-handler API) and the JSON-RPC fallback.

Regression guard: mcp 2.x removed ``Server.list_tools()`` / ``call_tool()``
decorators, so the server crashed on startup for every fresh install while
``--self-check`` still reported OK.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import sys
import types
from pathlib import Path

import pytest

import mcp_server

HOST_DIR = Path(__file__).resolve().parents[1]

_BLOCK_MCP = (
    "import sys, runpy\n"
    "class _Block:\n"
    "    def find_spec(self, name, path=None, target=None):\n"
    "        if name == 'mcp' or name.startswith('mcp.'):\n"
    "            raise ImportError('mcp blocked for test')\n"
    "sys.meta_path.insert(0, _Block())\n"
    "sys.argv = [%r]\n"
    "runpy.run_path(sys.argv[0], run_name='__main__')\n"
)


def _roundtrip(block_mcp: bool) -> dict:
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "no_such_tool", "arguments": {}}},
    ]
    script = str(HOST_DIR / "mcp_server.py")
    cmd = [sys.executable, "-c", _BLOCK_MCP % script] if block_mcp else [sys.executable, script]
    env = dict(os.environ, PYTHONPATH=str(HOST_DIR))
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env, cwd=str(HOST_DIR),
    )
    # Keep stdin open until every response is in: like a real client. The SDK
    # transport drops in-flight tool calls when stdin hits EOF.
    timer = threading.Timer(60, proc.kill)
    timer.start()
    by_id = {}
    try:
        for m in messages:
            proc.stdin.write(json.dumps(m) + "\n")
        proc.stdin.flush()
        while not {1, 2, 3} <= set(by_id):
            line = proc.stdout.readline()
            if not line:
                break
            if line.strip().startswith("{"):
                msg = json.loads(line)
                if "id" in msg:
                    by_id[msg["id"]] = msg
    finally:
        timer.cancel()
        proc.stdin.close()
        proc.wait(timeout=30)
    stderr = proc.stderr.read()
    proc.stderr.close()
    proc.stdout.close()
    assert {1, 2, 3} <= set(by_id), f"missing responses; stderr:\n{stderr[-2000:]}"
    return by_id


def _assert_protocol(by_id: dict) -> None:
    assert by_id[1]["result"]["serverInfo"]["name"] == "inspector-widget"
    names = {t["name"] for t in by_id[2]["result"]["tools"]}
    assert names == set(mcp_server.TOOLS)
    call = by_id[3]["result"]
    assert call["isError"] is True
    assert "error" in json.loads(call["content"][0]["text"])


def test_sdk_transport_roundtrip():
    pytest.importorskip("mcp")
    _assert_protocol(_roundtrip(block_mcp=False))


def test_fallback_transport_roundtrip():
    _assert_protocol(_roundtrip(block_mcp=True))


def test_build_server_with_mcp2_handler_api(monkeypatch):
    """Drive _build_mcp_server against a stub shaped like the mcp 2.x API."""

    class Model:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    fake_types = types.SimpleNamespace(
        Tool=Model, ListToolsResult=Model, CallToolResult=Model, TextContent=Model,
    )

    class Server2:  # 2.x: no list_tools/call_tool decorators
        def __init__(self, name, *, version="", on_list_tools=None, on_call_tool=None):
            self.name, self.version = name, version
            self.on_list_tools, self.on_call_tool = on_list_tools, on_call_tool

    mcp_pkg = types.ModuleType("mcp")
    mcp_pkg.types = fake_types
    server_mod = types.ModuleType("mcp.server")
    server_mod.Server = Server2
    monkeypatch.setitem(sys.modules, "mcp", mcp_pkg)
    monkeypatch.setitem(sys.modules, "mcp.types", fake_types)
    monkeypatch.setitem(sys.modules, "mcp.server", server_mod)

    server = mcp_server._build_mcp_server()
    assert isinstance(server, Server2)

    listed = asyncio.run(server.on_list_tools(None, None))
    assert {t.name for t in listed.tools} == set(mcp_server.TOOLS)

    params = types.SimpleNamespace(name="no_such_tool", arguments=None)
    result = asyncio.run(server.on_call_tool(None, params))
    assert result.isError is True
    assert "error" in json.loads(result.content[0].text)


def test_self_check_reports_incompatible_sdk(monkeypatch, capsys):
    """An importable SDK that fits neither API shape must fail --self-check."""
    monkeypatch.setattr(
        mcp_server, "_build_mcp_server",
        lambda: (_ for _ in ()).throw(TypeError("unexpected keyword 'on_list_tools'")),
    )
    assert mcp_server._self_check() == 1
    assert "INCOMPATIBLE" in capsys.readouterr().out
