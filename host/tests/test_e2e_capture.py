"""Capture and walk end to end, through the real entrypoints (WP S2).

The MCP server (``mcp_server._call_tool`` / ``_run_tool``) and the CLI
(``cli.main``) run against the harness fake adb and agent (real attach, real
TCP, the recorded View-screen replay behind the agent). The flow an agent
takes: attach, capture, outline, find, node, lint, image, a tap on the device,
recapture with a diff, diff. Both surfaces share one store:

* the CLI reads an MCP-made capture by id, by label and via latest, and prints
  the MCP text byte for byte (``--json``);
* a CLI in another OS process reads it too (queries never touch adb);
* an MCP call reads a CLI-made capture;
* CLI queries send nothing to the agent (no SHUTDOWN, no request at all).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import capture_harness as ch
import pytest

import cli
import mcp_server

HOST_DIR = Path(__file__).resolve().parents[1]


def mcp(tool: str, **args) -> dict:
    text, is_error = mcp_server._call_tool_text(tool, args)
    doc = json.loads(text)
    assert not is_error, doc
    return doc


def mcp_text(tool: str, **args) -> str:
    text, is_error = mcp_server._call_tool_text(tool, args)
    assert not is_error, text
    return text


def run_cli(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main([str(a) for a in argv])
    return rc, out.getvalue(), err.getvalue()


def cli_json(*argv) -> str:
    rc, out, err = run_cli(*argv, "--json")
    assert rc == 0, err
    return out.rstrip("\n")


def test_capture_and_walk_through_the_mcp_server_and_the_cli(tmp_path):
    with ch.harness("viewscreen", str(tmp_path), toolset="capture") as (dev, scene):
        # attach makes the app the default session, and points at capture()
        att = mcp("attach", serial=ch.SERIAL, package=ch.PACKAGE)
        assert att["next"] == ["capture()"]

        # capture: no serial/package needed now
        cap = mcp("capture", label="before")
        cid = cap["capture"]
        assert cap["label"] == "before" and cap["session"] == f"{ch.SERIAL}/{ch.PACKAGE}"
        assert cap["facets"]["views"] == 40 and cap["lint"].startswith("9 warn 5 info")

        # the walk: outline, find, node, lint, image
        out = mcp("outline")
        assert out["capture"] == cid and out["total"] >= 30
        found = mcp("find", text="Notifications", flags=["click"])
        assert found["total"] == 2
        node = mcp("node", ref="#badSwitch", props="nondefault")
        assert node["tap_xy"] == [242, 1254] and node["props"]["values"]["checked"] is True
        lint = mcp("lint")
        assert sum(lint["counts"].values()) == 14
        text, images, is_error = mcp_server._call_tool("image", {"ref": "#badSwitch",
                                                                 "inline": True})
        img = json.loads(text)
        assert not is_error and os.path.isfile(img["path"]) and img["inline_tokens"] > 0
        assert images and images[0][0] == "image/png"

        # the CLI reads the MCP's capture: by id, by label, via latest; same bytes
        before = dev.commands()
        for spec in (cid, "before", "@before", "latest"):
            assert cli_json("find", "-c", spec, "--text", "Notifications", "--flags", "click") \
                == mcp_text("find", capture=spec, text="Notifications", flags=["click"])
        assert cli_json("node", "#badSwitch", "--props", "nondefault") == \
            mcp_text("node", ref="#badSwitch", props="nondefault")
        assert cli_json("outline", "--view", "reading") == mcp_text("outline", view="reading")
        assert cli_json("lint", "--group", "node") == mcp_text("lint", group="node")
        assert ch.same_moment(cli_json("captures")) == ch.same_moment(mcp_text("captures"))
        # CLI queries never talk to the agent: no SHUTDOWN, no request at all
        assert dev.commands() == before

        # the human rendering, and the path the CLI prints for image
        rc, human, _ = run_cli("outline", "--root", "#badSwitch")
        assert rc == 0 and human.startswith(f"capture={cid}") and "#badSwitch" in human
        png = tmp_path / "switch.png"
        rc, printed, _ = run_cli("image", "#badSwitch", "--out", png)
        assert rc == 0 and printed.splitlines()[0] == str(png) and png.stat().st_size > 0

        # the user taps the switch; recapture with a diff, then diff
        ch.tap_switch(scene)
        again = mcp("capture", diff_from="before")
        ref = node["ref"]
        assert again["diff"]["lines"][0] == \
            f'~ {ref} Switch #badSwitch "Notifications": checked -> unchecked'
        assert mcp("node", ref=ref)["ref"] == ref  # the ref carried over
        d = mcp("diff", a="before")
        assert d["b"] == again["capture"] and d["summary"]["changed"] == 1
        assert cli_json("diff", "before") == mcp_text("diff", a="before")
        # the old capture now says it is stale
        assert mcp("outline", capture="before")["stale"].startswith(again["capture"])

        # the CLI captures; the MCP reads it by label
        rc, printed, err = run_cli("capture", "--label", "cli", "-q")
        assert rc == 0, err
        cli_cid = printed.strip()
        shown = mcp("captures", action="show", id="cli")
        assert shown["capture"] == cli_cid and shown["label"] == "cli"
        assert "shutdown" not in dev.commands()


def test_a_cli_in_another_process_reads_an_mcp_capture(tmp_path):
    with ch.harness("launcher", str(tmp_path), toolset="capture"):
        cap = mcp("capture", serial=ch.SERIAL, package=ch.PACKAGE, label="shared")
        want = mcp_text("find", capture="shared", text="state", flags=["click"])
        env = dict(os.environ, PYTHONPATH=str(HOST_DIR), PATH="")  # no adb reachable
        proc = subprocess.run(
            [sys.executable, str(HOST_DIR / "cli.py"), "find", "-c", "shared", "--text",
             "state", "--flags", "click", "--json"],
            capture_output=True, text=True, env=env, cwd=str(HOST_DIR), timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.rstrip("\n") == want
        assert json.loads(proc.stdout)["capture"] == cap["capture"]


def test_cli_errors_are_the_mcp_envelope_on_stderr(tmp_path):
    with ch.harness("launcher", str(tmp_path)):
        rc, out, err = run_cli("outline")
        assert rc == 1 and out == ""
        assert json.loads(err)["error"]["code"] == "capture_not_found"
        text, is_error = mcp_server._call_tool_text("outline", {})
        assert is_error and text == err.strip()
        rc, _out, err = run_cli("find", "--limit", "0")
        assert rc == 1 and json.loads(err)["error"]["code"] == "bad_args"


def test_capture_retries_a_lost_session_but_not_a_hot_reload(tmp_path, monkeypatch):
    """The MCP's retry rule (lifecycle-polish): a capture that lost its session is
    repeated once on a fresh attach; slots="enable" (a hot reload) is not."""
    from inspector_widget.client import SessionLostError
    from inspector_widget.capture import fetch

    with ch.harness("launcher", str(tmp_path), toolset="capture"):
        real = fetch.fetch
        calls = []

        def flaky(session, opts=None, **kw):
            calls.append(opts.slots if opts else None)
            if len(calls) == 1:
                raise SessionLostError("the agent went away")
            return real(session, opts, **kw)

        monkeypatch.setattr(fetch, "fetch", flaky)
        doc = mcp("capture", serial=ch.SERIAL, package=ch.PACKAGE)
        assert doc["capture"] and len(calls) == 2

        calls.clear()
        text, is_error = mcp_server._call_tool_text(
            "capture", {"serial": ch.SERIAL, "package": ch.PACKAGE, "slots": "enable"})
        assert is_error and json.loads(text)["error"]["code"] == "device_lost"
        assert calls == ["enable"]


@pytest.mark.parametrize("scene", ["launcher", "wide", "mixed"])
def test_every_capture_tool_answers_on_every_scene(tmp_path, scene):
    pytest.importorskip("PIL")
    with ch.harness(scene, str(tmp_path), toolset="capture"):
        mcp("capture", serial=ch.SERIAL, package=ch.PACKAGE)
        mcp("capture", diff_from="prev")
        for tool, args in [("outline", {}), ("find", {"flags": ["click"]}),
                           ("node", {"ref": "n1"}), ("lint", {}), ("image", {"overlay": "marks"}),
                           ("diff", {}), ("captures", {})]:
            assert ch.same_moment(cli_json(tool, *_argv(args))) == \
                ch.same_moment(mcp_text(tool, **args)), (scene, tool)


def _argv(args: dict) -> list[str]:
    out: list[str] = []
    for k, v in args.items():
        if k == "ref":
            out.append(v)
        else:
            out += [f"--{k.replace('_', '-')}", ",".join(v) if isinstance(v, list) else str(v)]
    return out


def test_a_label_with_upper_case_is_stored_lowercase_and_says_so(tmp_path):
    # G27: labels are lowercase; "afterCompose" used to be rejected (bad_args). Both surfaces
    # store it lowercased, say so in a note, and resolve either spelling.
    with ch.harness("viewscreen", str(tmp_path), toolset="capture"):
        cap = mcp("capture", serial=ch.SERIAL, package=ch.PACKAGE, label="afterCompose")
        assert cap["label"] == "aftercompose"
        assert "label 'afterCompose' stored as 'aftercompose' (labels are lowercase)" \
            in cap["note"]
        for spec in ("aftercompose", "afterCompose", "@AfterCompose"):
            assert mcp("captures", action="show", id=spec)["capture"] == cap["capture"]
        rc, printed, err = run_cli("capture", "--label", "BeforeTap", "--json")
        assert rc == 0, err
        doc = json.loads(printed)
        assert doc["label"] == "beforetap" and "stored as 'beforetap'" in doc["note"]
        moved = mcp("captures", action="label", id=cap["capture"], label="Final")
        assert moved["label"] == "final" and moved["note"] == \
            "label 'Final' stored as 'final' (labels are lowercase)"
        same = mcp("captures", action="label", id=cap["capture"], label="final")
        assert "note" not in same
        text, is_error = mcp_server._call_tool_text("capture", {"label": "Bad Label"})
        assert is_error and "upper case is lowercased" in text
