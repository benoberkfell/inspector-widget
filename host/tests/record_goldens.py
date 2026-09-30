"""Record the legacy tool outputs that ``test_legacy_golden.py`` pins (work package G1).

Every legacy MCP tool and every legacy CLI subcommand runs through its real entry
point (``mcp_server._call_tool_text`` and ``cli.main``) over the offline harness:
the fake adb and the fake agent of ``tests/fakeagent.py``, serving one of four
scenes:

* ``default``: the harness's own scene (``fakeagent.default_scene``: Views, a
  ComposeView, a dialog window);
* ``wide``: the 259-view E6 screen (``fakescenes.wide_scene``);
* ``launcher`` and ``viewscreen``: the replays of real recorded screens
  (``fakescenes.replay_scene``).

The goldens are the content each surface returned before Phase 0 (the "legacy"
output), so that ``detail="full"`` with ``max_bytes=0`` can be shown to reproduce
it (spec section 2.7), and so that S3 (the legacy tools on captures) has a target.
Brief goldens (the Phase-0 defaults) sit beside them once Phase 0 is in.

Volatile values are masked before anything is stored or compared: file paths
under the run's temp directory (``<path>``), adb forward ports (``tcp:<port>``).
The fake device's pid (4242) and build id are fixed, so they are kept.

Regenerate deliberately, from ``host/``::

    PYTHONPATH=. .venv/bin/python tests/record_goldens.py [--mode legacy|brief] [SCENE...]

Each golden entry records the commit it was recorded from (``source_commit``).
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator, Mapping
from typing import Any

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_HOST_DIR = os.path.dirname(_TESTS_DIR)
for _p in (_HOST_DIR, _TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fakeagent
import fakescenes
import pytest

GOLDEN_DIR = os.path.join(_TESTS_DIR, "golden", "legacy")
SCENES = ("default", "wide", "launcher", "viewscreen")
SERIAL = fakeagent.DEFAULT_SERIAL
PACKAGE = fakeagent.DEFAULT_PACKAGE

#: A View each scene has (get_properties, inspect_node, component_image).
VIEW_ID = {"default": 1003, "wide": 1001, "launcher": 82, "viewscreen": 13}

#: Golden modes: ``legacy`` is today's output (and what ``detail="full"`` with
#: ``max_bytes=0`` must reproduce); ``brief`` is the Phase-0 default output.
MODES = ("legacy", "brief")


# --------------------------------------------------------------------------- #
# What runs
# --------------------------------------------------------------------------- #
def mcp_calls(scene: str) -> list[tuple[str, str, dict[str, Any]]]:
    """``[(entry name, tool, args)]`` in the order they run; ``detach`` is last."""
    vid = VIEW_ID[scene]
    calls: list[tuple[str, str, dict[str, Any]]] = [
        ("list_devices", "list_devices", {}),
        ("list_processes", "list_processes", {"serial": SERIAL}),
        ("attach", "attach", {}),
        ("dump_tree", "dump_tree", {}),
        ("get_properties", "get_properties", {"view_id": vid}),
        ("screenshot", "screenshot", {}),
        ("dump_compose", "dump_compose", {}),
        ("compose_overlay", "compose_overlay", {}),
        ("dump_accessibility", "dump_accessibility", {}),
        ("a11y_lint", "a11y_lint", {}),
        ("a11y_overlay", "a11y_overlay", {}),
        ("inspect", "inspect", {}),
        ("inspect_node", "inspect_node", {"view_id": vid}),
        ("component_image", "component_image", {"view_id": vid}),
    ]
    if scene != "wide":  # properties inline (E3 decoding, the non-default filter)
        calls.insert(4, ("dump_tree_props", "dump_tree", {"include_properties": True}))
    if scene == "launcher":
        calls.insert(7, ("dump_compose_sem", "dump_compose", {"include_slot_table": False}))
    calls.append(("detach", "detach", {}))
    return calls


def cli_calls(scene: str, out_dir: str) -> list[tuple[str, list[str]]]:
    """``[(entry name, argv)]`` in the order they run; ``detach`` is last. The
    ``--json -`` ones are compared as JSON, the rest as text."""
    vid = str(VIEW_ID[scene])
    calls: list[tuple[str, list[str]]] = [
        ("devices", ["devices"]),
        ("packages", ["packages"]),
        ("attach", ["attach"]),
        ("dump", ["dump", "--json", "-"]),
        ("dump_text", ["dump"]),
        ("compose", ["compose", "--json", "-"]),
        ("compose_text", ["compose"]),
        ("a11y", ["a11y", "--json", "-"]),
        ("a11y_text", ["a11y"]),
        ("a11y_lint", ["a11y-lint", "--json", "-"]),
        ("inspect", ["inspect", "--json", "-"]),
        ("inspect_text", ["inspect"]),
        ("inspect_node", ["inspect-node", "--view-id", vid, "--json", "-"]),
        ("component_image", ["component-image", "--view-id", vid,
                             "--out", os.path.join(out_dir, "component.png")]),
        ("screenshot", ["screenshot", "--out", os.path.join(out_dir, "screen.png")]),
        ("get_properties", ["get-properties", "--view-id", vid, "--json", "-"]),
    ]
    if scene != "wide":
        calls.insert(5, ("dump_props", ["dump", "--properties", "--json", "-"]))
    calls.append(("detach", ["detach"]))
    return calls


def is_json_argv(argv: list[str]) -> bool:
    return "--json" in argv


# --------------------------------------------------------------------------- #
# The harness (the same patching as conftest.fake_device, outside pytest)
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def harness(scene: str, tmp: str) -> Iterator[fakeagent.FakeDevice]:
    """A fake device with the probe app running and ``scene`` behind its agent.

    Everything is undone on exit: the adb patch, the temp dir, the MCP session
    cache and closing flag, the density cache, and the environment (the key
    cache and the capture store, both under ``tmp``)."""
    import mcp_server
    from inspector_widget import adb

    with pytest.MonkeyPatch.context() as mp:
        dev = fakeagent.default_device()
        fakeagent.install(mp, dev, build_out=os.path.join(tmp, "build-out"))
        if scene != "default":
            name = "wide" if scene == "wide" else scene
            dev.behaviour = fakescenes.replay_behaviour(name, build_id=dev.default_build_id)
        tmpdir = os.path.join(tmp, "tmp")
        os.makedirs(tmpdir, exist_ok=True)
        mp.setattr(tempfile, "tempdir", tmpdir)
        cache = mcp_server.SessionCache()
        mp.setattr(mcp_server, "SESSIONS", cache)
        mp.setattr(mcp_server, "_closing", threading.Event())
        mp.setattr(adb, "_OWN_FORWARDS", {})
        mp.setattr(mcp_server._a11y_device_metrics, "_cache", {}, raising=False)
        mp.setenv("INSPECTOR_WIDGET_KEY_CACHE", os.path.join(tmp, "keys"))
        mp.setenv("INSPECTOR_WIDGET_CAPTURE_DIR", os.path.join(tmp, "store"))
        for var in ("INSPECTOR_WIDGET_MAX_BYTES", "ANDROID_SERIAL", "INSPECTOR_WIDGET_LOG",
                    "VIEWSPECTOR_LOG"):
            mp.delenv(var, raising=False)
        try:
            yield dev
        finally:
            for session in cache.all():
                injection = getattr(session, "injection", None)
                with contextlib.suppress(Exception):
                    if injection is not None:
                        injection.close()
            dev.close()


# --------------------------------------------------------------------------- #
# Masking
# --------------------------------------------------------------------------- #
_PORT = re.compile(r"tcp:\d+")

#: Keys whose value is a wall-clock measurement.
VOLATILE_KEYS = frozenset({"elapsed_ms", "took_ms"})


def mask(obj: Any, tmp: str) -> Any:
    """``obj`` with every string that names a path under ``tmp`` replaced by
    ``<path>`` (in text, the path itself), adb forward ports by ``<port>``, and
    timings (``VOLATILE_KEYS``) by ``"<ms>"``."""
    real = os.path.realpath(tmp)
    roots = sorted({tmp, real}, key=len, reverse=True)

    def text(s: str) -> str:
        for r in roots:
            s = re.sub(re.escape(r) + r"[^\s\"',)]*", "<path>", s)
        return _PORT.sub("tcp:<port>", s)

    def walk(o: Any) -> Any:
        if isinstance(o, str):
            return text(o)
        if isinstance(o, Mapping):
            return {k: "<ms>" if k in VOLATILE_KEYS else walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v) for v in o]
        return o

    return walk(obj)


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #
def run_mcp(scene: str, tmp: str, extra: Mapping[str, Any] | None = None,
            names: set[str] | None = None) -> dict[str, dict[str, Any]]:
    """Run the MCP calls of ``scene``; ``{entry: {"tool", "args", "is_error", "json"}}``.

    ``extra`` is added to every call's arguments (``detail``/``max_bytes``)."""
    import mcp_server

    out: dict[str, dict[str, Any]] = {}
    with harness(scene, tmp):
        for name, tool, args in mcp_calls(scene):
            if names is not None and name not in names and name != "detach":
                continue
            call = dict(args)
            if tool not in ("list_devices", "list_processes"):
                call.setdefault("serial", SERIAL)
                call.setdefault("package", PACKAGE)
            if extra:
                props = mcp_server.TOOLS[tool]["schema"].get("properties", {})
                call.update({k: v for k, v in extra.items() if k in props})
            text, is_error = mcp_server._call_tool_text(tool, call)
            out[name] = {"tool": tool, "args": args, "is_error": is_error,
                         "json": mask(json.loads(text), tmp)}
    return out


def run_cli(scene: str, tmp: str, extra: list[str] | None = None,
            names: set[str] | None = None) -> dict[str, dict[str, Any]]:
    """Run the CLI calls of ``scene``; ``{entry: {"argv", "rc", "json"|"stdout",
    "stderr"}}``. ``extra`` flags are added to every ``--json`` call."""
    import cli

    out: dict[str, dict[str, Any]] = {}
    files = os.path.join(tmp, "out")
    os.makedirs(files, exist_ok=True)
    with harness(scene, tmp):
        for name, argv in cli_calls(scene, files):
            if names is not None and name not in names and name != "detach":
                continue
            run_argv = list(argv) + (list(extra or []) if is_json_argv(argv) else [])
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = cli.main(run_argv)
            entry: dict[str, Any] = {"argv": mask(argv, tmp), "rc": rc,
                                     "stderr": mask(stderr.getvalue(), tmp)}
            if is_json_argv(argv) or name == "component_image":
                entry["json"] = mask(json.loads(stdout.getvalue()), tmp)
            else:
                entry["stdout"] = mask(stdout.getvalue(), tmp)
            out[name] = entry
    return out


# --------------------------------------------------------------------------- #
# Golden files
# --------------------------------------------------------------------------- #
def golden_path(scene: str, surface: str, mode: str) -> str:
    return os.path.join(GOLDEN_DIR, scene, f"{surface}-{mode}.json.gz")


def load_golden(scene: str, surface: str, mode: str) -> dict[str, Any]:
    with gzip.open(golden_path(scene, surface, mode), "rt", encoding="utf-8") as f:
        return json.load(f)


def write_golden(scene: str, surface: str, mode: str, entries: Mapping[str, Any],
                 commit: str) -> str:
    path = golden_path(scene, surface, mode)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc = {"scene": scene, "surface": surface, "mode": mode,
           "entries": {k: dict(v, source_commit=commit) for k, v in entries.items()}}
    text = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as f:
        f.write(text.encode("utf-8"))
    return path


def source_commit() -> str:
    """HEAD's short sha, ``+dirty`` when tracked files outside the goldens changed."""
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=_HOST_DIR, capture_output=True,
                              text=True, check=False).stdout.strip()

    sha = git("rev-parse", "--short=12", "HEAD") or "unknown"
    dirty = [line for line in git("status", "--porcelain", "--untracked-files=no").splitlines()
             if "tests/golden/" not in line]
    return sha + ("+dirty" if dirty else "")


# --------------------------------------------------------------------------- #
# Comparing
# --------------------------------------------------------------------------- #
def diff(golden: Any, actual: Any, path: str = "$", limit: int = 12) -> list[str]:
    """Readable differences between two JSON values: at most ``limit`` lines of
    ``path: golden X != actual Y`` (lists and dicts are walked)."""
    out: list[str] = []

    def short(v: Any) -> str:
        s = json.dumps(v, ensure_ascii=False, default=str)
        return s if len(s) <= 120 else s[:117] + "..."

    def walk(g: Any, a: Any, p: str) -> None:
        if len(out) >= limit:
            return
        if isinstance(g, Mapping) and isinstance(a, Mapping):
            for k in list(g) + [k for k in a if k not in g]:
                if k not in a:
                    out.append(f"{p}.{k}: missing (golden {short(g[k])})")
                elif k not in g:
                    out.append(f"{p}.{k}: unexpected {short(a[k])}")
                else:
                    walk(g[k], a[k], f"{p}.{k}")
                if len(out) >= limit:
                    return
        elif isinstance(g, list) and isinstance(a, list):
            if len(g) != len(a):
                out.append(f"{p}: {len(g)} items in the golden, {len(a)} now")
            for i, (x, y) in enumerate(zip(g, a)):
                walk(x, y, f"{p}[{i}]")
                if len(out) >= limit:
                    return
        elif g != a or type(g) is not type(a):
            out.append(f"{p}: golden {short(g)} != actual {short(a)}")

    walk(golden, actual, path)
    return out


def comparable(entry: Mapping[str, Any]) -> dict[str, Any]:
    """What a golden entry pins: the output (JSON or text), the exit code and the
    error flag; not how it was recorded."""
    return {k: v for k, v in entry.items() if k in ("json", "stdout", "stderr", "rc", "is_error")}


# --------------------------------------------------------------------------- #
# Script
# --------------------------------------------------------------------------- #
def record(scenes: list[str], mode: str) -> list[str]:
    commit = source_commit()
    written = []
    for scene in scenes:
        with tempfile.TemporaryDirectory(prefix="iw-golden-") as tmp:
            written.append(write_golden(scene, "mcp", mode,
                                        run_mcp(scene, os.path.join(tmp, "mcp")), commit))
            written.append(write_golden(scene, "cli", mode,
                                        run_cli(scene, os.path.join(tmp, "cli")), commit))
    return written


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--mode", choices=MODES, default="legacy")
    p.add_argument("scenes", nargs="*", metavar="SCENE", help=f"any of {', '.join(SCENES)}")
    args = p.parse_args(argv)
    unknown = sorted(set(args.scenes) - set(SCENES))
    if unknown:
        p.error(f"unknown scene(s): {', '.join(unknown)}")
    for path in record(args.scenes or list(SCENES), args.mode):
        print(os.path.relpath(path, _HOST_DIR))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
