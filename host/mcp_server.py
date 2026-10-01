#!/usr/bin/env python3
# Inspector Widget — MCP server.
#
# Exposes the Inspector Widget Android View Layout Inspector to an LLM agent over the
# Model Context Protocol (stdio transport).  It is a thin orchestration layer on
# top of the `inspector_widget` package (the Python host driver that pushes the
# native agent + dex/jar, attaches via `cmd activity attach-agent`, forwards the
# abstract socket and speaks the framed protobuf protocol from
# `proto/view_inspection.proto`).
#
# Two execution modes, selected at runtime:
#   1. The real `mcp` SDK (preferred — `pip install mcp`).  We register the
#      tools via the low-level `mcp.server.Server` API and serve over stdio.
#   2. A self-contained JSON-RPC 2.0 / MCP-over-stdio fallback used when the
#      `mcp` package is not importable, so the server still runs with only
#      stdlib + protobuf present.  The fallback implements just enough of the
#      MCP handshake (`initialize`, `tools/list`, `tools/call`,
#      `notifications/initialized`) to drive the same tool surface.
#
# The host driver is the single source of truth for adb/injection/transport.
# This module owns only: the 18 legacy tool schemas (15 inspection tools + 3
# TalkBack tools), argument validation, per-(serial,package) session caching, and
# screenshot temp-file materialisation. Protobuf responses are shaped by the
# package (strings, a11y, correlate, results); every legacy tool result then
# leaves through inspector_widget.output: compact JSON, brief by default
# (detail="full" for the legacy content), and over max_bytes a spill envelope
# plus a spill file (Phase 0 of docs/design/capture-and-walk.md).
#
# The 8 capture-and-walk tools (capture, captures, outline, find, node, image,
# lint, diff) come from one registry, inspector_widget.surface, which also
# generates the CLI subcommands; inspector_widget.ops implements them.
# INSPECTOR_WIDGET_TOOLSET chooses what tools/list shows (default: the legacy
# and TalkBack tools); every tool stays callable by name.
#
# See host/README.md for build + run + `claude mcp add` instructions.

from __future__ import annotations

import argparse
import asyncio
import atexit
import contextlib
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import threading
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Make the host package importable when this file is launched directly
# (`python host/mcp_server.py`).  host/ is the parent dir of this file and it
# contains the `inspector_widget` package.
# --------------------------------------------------------------------------- #
_HOST_DIR = os.path.dirname(os.path.abspath(__file__))
if _HOST_DIR not in sys.path:
    sys.path.insert(0, _HOST_DIR)

log = logging.getLogger("inspector-widget.mcp")


# --------------------------------------------------------------------------- #
# Host-driver facade.
#
# We depend on the public surface of `inspector_widget` (built as a sibling
# module). The facade below isolates the import in ONE place, so a broken host
# package is one clear error (and --self-check can probe it).
#
# Required `inspector_widget` public API (documented in host/README.md):
#
#   inspector_widget.list_devices() -> list[dict]
#       each: {"serial": str, "api": int|None, "abi": str|None,
#              "model": str|None, "state": str}
#
#   inspector_widget.list_processes(serial) -> list[dict]
#       each: {"package": str, "pid": int|None, "running": bool}
#       (debuggable packages only)
#
#   inspector_widget.attach(serial, package, force_reinject=False) -> Session
#       Injects the agent if not already attached, forwards the socket, performs
#       the HELLO handshake.  Returns a live Session.  Cheap/idempotent if the
#       agent is already attached for that (serial, package).
#
#   Session attributes / methods (all synchronous, blocking):
#       .serial: str
#       .package: str
#       .pid: int, .warm: bool
#       .api_level: int
#       .abi: str
#       .agent_version: str
#       .info() -> dict of the above
#       .is_alive() -> bool   (connection open + app still on the same pid)
#       .disconnect()         (drop the connection, agent keeps running)
#       .shutdown()           (SHUTDOWN: stop the agent for every client)
#       .get_windows() -> GetWindowsResponse
#       .dump_tree(root_id=0, include_properties=False,
#                  include_resolution_stack=False, include_screenshot=False,
#                  screenshot_scale=1.0)
#           -> ViewInspection.DumpTreeResponse  (raw proto message)
#       .get_properties(view_id, include_resolution_stack=False)
#           -> ViewInspection.GetPropertiesResponse  (raw proto message)
#       .screenshot(root_id=0, scale=1.0)
#           -> ViewInspection.ScreenshotResponse  (raw proto message)
#
# The proto module is `inspector_widget.proto.view_inspection_pb2` (generated;
# protobuf package `viewspector.proto`). The self-check probes it; the tools reach
# it through the package.
# --------------------------------------------------------------------------- #


class HostUnavailableError(RuntimeError):
    """Raised when the inspector_widget package cannot be imported/used."""


def _import_host() -> Any:
    """Import and return the inspector_widget module (or raise)."""
    try:
        import inspector_widget  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on sibling build
        raise HostUnavailableError(
            "Could not import 'inspector_widget'. Build the host package first "
            "(scripts/build.sh) and run from the project's virtualenv. "
            f"Underlying error: {exc!r}"
        ) from exc
    return inspector_widget


def _import_proto() -> Any:
    """Import the generated protobuf bindings (``inspector_widget/proto/
    view_inspection_pb2.py``, the module the package itself uses); for
    ``--self-check`` and the startup health line."""
    try:
        from inspector_widget.proto import view_inspection_pb2
    except Exception as exc:
        raise HostUnavailableError(
            "Could not import the generated protobuf module "
            "'inspector_widget.proto.view_inspection_pb2'. "
            f"Run scripts/build.sh to generate it. Underlying error: {exc!r}"
        ) from exc
    return view_inspection_pb2


class HostFacade:
    """Single, lazily-initialised gateway to the inspector_widget driver."""

    def __init__(self) -> None:
        self._host: Any = None
        self._proto: Any = None
        self._lock = threading.Lock()

    @property
    def host(self) -> Any:
        if self._host is None:
            with self._lock:
                if self._host is None:
                    self._host = _import_host()
        return self._host

    @property
    def proto(self) -> Any:
        if self._proto is None:
            with self._lock:
                if self._proto is None:
                    self._proto = _import_proto()
        return self._proto

    # -- adb-level queries -------------------------------------------------- #
    def list_devices(self) -> List[Dict[str, Any]]:
        return list(self.host.list_devices())

    def list_processes(self, serial: str) -> List[Dict[str, Any]]:
        return list(self.host.list_processes(serial))

    def attach(self, serial: str, package: str, force_reinject: bool = False) -> Any:
        return self.host.attach(serial, package, force_reinject=force_reinject)

    def connect_existing(self, serial: str, package: str) -> Any:
        return self.host.connect_existing(serial, package)


HOST = HostFacade()


# --------------------------------------------------------------------------- #
# Shutdown and per-call state.
#
# _closing is set first thing in exit cleanup: from then on no tool retries
# and no session is attached (or cached), so a call the cleanup cut off can't
# re-attach behind it and leave a session or an adb forward nobody removes.
#
# _CALL holds the state of the tool call running on this thread (_run_tool
# sets it): the sessions it used, for the stale-build note, and the detach
# count it first saw per app, so its retry never re-injects an agent that a
# concurrent detach just stopped.
# --------------------------------------------------------------------------- #
_closing = threading.Event()
_CALL = threading.local()


class _CallState:
    """What one tool call touched: the sessions it got, and for each (serial,
    package) the stop count it saw first (see SessionCache.detaching)."""

    def __init__(self) -> None:
        self.sessions: List[Any] = []
        self.stops_seen: Dict[Tuple[str, str], int] = {}


def _current_call() -> Optional[_CallState]:
    return getattr(_CALL, "state", None)


# --------------------------------------------------------------------------- #
# Session cache, keyed by (serial, package).
#
# A cached session is reused only while Session.is_alive() holds (the socket
# is open and the app still runs under the same pid); otherwise it is dropped
# and re-attached. Each key has its own lock, so a slow cold inject into one
# app never blocks tools on another; detach holds it too.
# --------------------------------------------------------------------------- #
class SessionCache:
    def __init__(self) -> None:
        self._sessions: Dict[Tuple[str, str], Any] = {}
        self._key_locks: Dict[Tuple[str, str], threading.Lock] = {}
        # How many times detach stopped (or set out to stop) each key's agent.
        self._stops: Dict[Tuple[str, str], int] = {}
        self._lock = threading.Lock()  # guards the dicts only

    def _key(self, serial: str, package: str) -> Tuple[str, str]:
        return (serial, package)

    def _key_lock(self, key: Tuple[str, str]) -> threading.Lock:
        with self._lock:
            return self._key_locks.setdefault(key, threading.Lock())

    def get_or_attach(self, serial: str, package: str, force: bool = False) -> Any:
        key = self._key(serial, package)
        with self._key_lock(key):
            self._check_may_attach(key, serial, package)
            with self._lock:
                session = self._sessions.get(key)
            if session is not None and not force and _session_alive(session):
                return _note_used(session)
            if session is not None:
                # Dead (agent idled out, app restarted, stream broke) or forced:
                # release it before attaching afresh.
                self._forget(key, session)
                _disconnect(session)
            session = HOST.attach(serial, package, force_reinject=force)
            with self._lock:
                if not _closing.is_set():
                    self._sessions[key] = session
                    return _note_used(session)
            # Exit cleanup began while this attach ran and has emptied the
            # cache already: release the new session (and its forward) here.
            _disconnect(session)
            raise ServerClosingError(_CLOSING_MESSAGE)

    def _check_may_attach(self, key: Tuple[str, str], serial: str, package: str) -> None:
        """Refuse while the server shuts down, and, within one tool call, once
        a detach stopped this app's agent (the call's retry must not re-inject
        it). The next call attaches as usual."""
        if _closing.is_set():
            raise ServerClosingError(_CLOSING_MESSAGE)
        call = _current_call()
        if call is None:
            return
        with self._lock:
            stops = self._stops.get(key, 0)
        if call.stops_seen.setdefault(key, stops) != stops:
            raise ToolError(f"{package} on {serial} was detached while this call ran; "
                            f"call the tool again to attach afresh")

    @contextlib.contextmanager
    def detaching(self, serial: str, package: str, stop: bool) -> Iterator[Optional[Any]]:
        """Hold the key's lock for a detach; yield the cached session (dropped
        from the cache), or None. ``stop``: the agent is about to be stopped,
        so no call that was using it may re-attach (retry) afterwards."""
        key = self._key(serial, package)
        with self._key_lock(key):
            with self._lock:
                if stop:
                    self._stops[key] = self._stops.get(key, 0) + 1
                session = self._sessions.pop(key, None)
            yield session

    def stopped_during(self, call: _CallState) -> bool:
        """Whether a detach stopped an agent ``call`` used, after it first used it."""
        with self._lock:
            return any(self._stops.get(key, 0) != seen for key, seen in call.stops_seen.items())

    def _forget(self, key: Tuple[str, str], session: Any) -> None:
        with self._lock:
            if self._sessions.get(key) is session:
                del self._sessions[key]

    def peek(self, serial: str, package: str) -> Optional[Any]:
        with self._lock:
            return self._sessions.get(self._key(serial, package))

    def drop(self, serial: str, package: str) -> Optional[Any]:
        with self._lock:
            return self._sessions.pop(self._key(serial, package), None)

    def all(self) -> List[Any]:
        with self._lock:
            return list(self._sessions.values())

    def close_all(self) -> None:
        """Disconnect every cached session (agents keep running). For exit,
        once ``_closing`` is set, so that no attach caches a session after it."""
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            _disconnect(session)


SESSIONS = SessionCache()

_CLOSING_MESSAGE = "the MCP server is shutting down; not attaching"


def _note_used(session: Any) -> Any:
    """Record ``session`` as used by the current tool call; returns it."""
    call = _current_call()
    if call is not None:
        call.sessions.append(session)
    return session


def _session_alive(session: Any) -> bool:
    try:
        return bool(session.is_alive())
    except Exception:  # noqa: BLE001 - a probe that fails means "not usable"
        log.debug("session liveness check failed", exc_info=True)
        return False


def _disconnect(session: Any) -> None:
    """Drop a session's connection and adb forward; the agent keeps running."""
    try:
        session.disconnect()
    except Exception:  # pragma: no cover - best-effort teardown
        log.debug("session.disconnect() failed", exc_info=True)


class _NeverRaised(Exception):
    """Stands in for a host exception class when the host package won't import."""


def _transport_error() -> type:
    try:
        from inspector_widget.client import TransportError
    except Exception:  # pragma: no cover - host package missing
        return _NeverRaised
    return TransportError


def _serial(serial: Optional[str]) -> str:
    """The serial to use: as given, else $ANDROID_SERIAL / the only attached device."""
    if isinstance(serial, str) and serial.strip():
        return serial
    from inspector_widget import adb
    return adb.resolve_serial(None)


# --------------------------------------------------------------------------- #
# Screenshots: written as PNG by inspector_widget.png (the one decoder of record,
# AGENTS.md section 6) into this server's temp directory.
# --------------------------------------------------------------------------- #
def _save_screenshot_png(screenshot_msg: Any, dest_path: str) -> Dict[str, Any]:
    """Write a Screenshot proto to ``dest_path`` as PNG; returns
    ``{path, width, height, bytes, scale}``."""
    from inspector_widget import png as pngmod

    pngmod.write_png(screenshot_msg, dest_path)
    return {
        "path": dest_path,
        "width": int(screenshot_msg.width),
        "height": int(screenshot_msg.height),
        "bytes": os.path.getsize(dest_path),
        "scale": float(screenshot_msg.scale) if screenshot_msg.scale else 1.0,
    }


_TMP_PREFIX = "viewspector_"
# PNGs go in one directory per server process (under $TMPDIR), deleted when the
# server exits, so they don't pile up across sessions.
_TMP_DIRS: Dict[str, str] = {}
_TMP_LOCK = threading.Lock()


def _tmp_dir() -> str:
    base = tempfile.gettempdir()
    with _TMP_LOCK:
        path = _TMP_DIRS.get(base)
        if path is None or not os.path.isdir(path):
            path = tempfile.mkdtemp(prefix=f"inspector-widget-{os.getpid()}-", dir=base)
            _TMP_DIRS[base] = path
        return path


def _tmp_png_path(serial: str, package: str, tag: str) -> str:
    safe_pkg = package.replace("/", "_").replace(":", "_")
    safe_serial = serial.replace("/", "_").replace(":", "_")
    fd, path = tempfile.mkstemp(
        prefix=f"{_TMP_PREFIX}{safe_serial}_{safe_pkg}_{tag}_", suffix=".png", dir=_tmp_dir()
    )
    os.close(fd)
    return path


def _remove_quietly(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


@contextlib.contextmanager
def _png_output(serial: str, package: str, tag: str) -> Iterator[str]:
    """A new PNG path for a tool result; removed again if the tool fails."""
    path = _tmp_png_path(serial, package, tag)
    try:
        yield path
    except BaseException:
        _remove_quietly(path)
        raise


@contextlib.contextmanager
def _png_scratch(serial: str, package: str, tag: str) -> Iterator[str]:
    """A scratch PNG path (an overlay's base screenshot); always removed."""
    path = _tmp_png_path(serial, package, tag)
    try:
        yield path
    finally:
        _remove_quietly(path)


def _cleanup_at_exit() -> None:
    """Restore the TalkBack settings this server changed, disconnect cached
    sessions (removing their adb forwards; agents keep running for the next
    start), remove any other forward this process still holds (an attach cut
    short), and delete this process's PNG directory.

    ``_closing`` goes up first: a tool call this cuts off must not retry, and
    no attach may cache a session behind the cleanup.
    """
    _closing.set()
    try:
        from inspector_widget.talkback import device as tbdevice
        tbdevice.restore_owned()
    except Exception:  # noqa: BLE001 - exit cleanup is best-effort; the state file remains
        pass
    try:
        SESSIONS.close_all()
    except Exception:  # noqa: BLE001 - exit cleanup is best-effort
        pass
    try:
        from inspector_widget import adb
        adb.remove_own_forwards()
    except Exception:  # noqa: BLE001
        pass
    with _TMP_LOCK:
        dirs = list(_TMP_DIRS.values())
        _TMP_DIRS.clear()
    for path in dirs:
        shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Tool implementations.  Each returns a JSON-serialisable dict.  They are sync;
# the MCP layer (real or fallback) runs them in a worker thread because the host
# driver does blocking adb + socket I/O.
# --------------------------------------------------------------------------- #
def _device_to_json(d: Dict[str, Any]) -> Dict[str, Any]:
    """One ``inspector_widget.list_devices()`` entry, as list_devices reports it."""
    return {
        "serial": d.get("serial"),
        "api": d.get("api"),
        "abi": d.get("abi"),
        "model": d.get("model"),
        "state": d.get("state", "device"),
    }


def _process_to_json(p: Dict[str, Any]) -> Dict[str, Any]:
    """One ``inspector_widget.list_processes()`` entry."""
    return {
        "package": p.get("package"),
        "pid": p.get("pid"),
        "running": bool(p.get("pid")) if p.get("running") is None else bool(p.get("running")),
    }


def tool_list_devices() -> Dict[str, Any]:
    devices = [_device_to_json(d) for d in HOST.list_devices()]
    return {"devices": devices, "count": len(devices)}


def tool_list_processes(serial: Optional[str] = None) -> Dict[str, Any]:
    serial = _serial(serial)
    procs = [_process_to_json(p) for p in HOST.list_processes(serial)]
    procs.sort(key=lambda p: (not p["running"], p["package"] or ""))
    top = _foreground_package(serial)
    for p in procs:
        if top and p["package"] == top:
            p["foreground"] = True  # the app on screen: what "this screen" means
    return {"serial": serial, "processes": procs, "count": len(procs)}


def _foreground_package(serial: str) -> Optional[str]:
    """The package of the resumed activity on top, or None when it cannot be read."""
    try:
        from inspector_widget.talkback import device as tbdevice
        return tbdevice.top_package(serial)
    except Exception:  # noqa: BLE001 - an extra, never a failure
        return None


def tool_attach(serial: Optional[str], package: str, force: bool = False) -> Dict[str, Any]:
    _require(package, "package")
    serial = _serial(serial)
    cached = SESSIONS.peek(serial, package)
    session = SESSIONS.get_or_attach(serial, package, force=bool(force))
    # get_windows confirms the agent answers real commands (not just Hello).
    try:
        windows = _session_get_windows(session)
    except _session_lost_error() as exc:
        # The connection died between the liveness check and now (the agent
        # idled out or dropped us): re-attach once. A second failure is real.
        # The failed client closed itself, so get_or_attach replaces it. A
        # timeout is not retried: it would only wait out the deadline again.
        log.info("get_windows after attach failed (%s); re-attaching once", exc)
        session = SESSIONS.get_or_attach(serial, package)
        windows = _session_get_windows(session)
    root_ids = windows.get("root_ids", [])
    info = session.info()
    result = {
        "serial": serial,
        "package": package,
        "attached": True,
        "pid": info.get("pid"),
        "warm": info.get("warm"),
        "reused": session is cached,
        "api_level": info.get("api_level"),
        "abi": info.get("abi"),
        "agent_version": info.get("agent_version"),
        "build_id": info.get("build_id"),
        "window_count": len(root_ids),
        "root_ids": root_ids,
        "session": f"{serial}/{package}",
    }
    note = _session_note(session)
    if note:
        result["note"] = note
    _remember_session(serial, package)
    if "capture" in _listed_tools():
        result["next"] = ["capture()"]
    return result


def _session_note(session: Any) -> Optional[str]:
    """The warning a session carries, as the CLI prints it for every subcommand:
    the attach's own note (e.g. a stale-build agent kept for other clients), else
    whether a cached session's agent runs another build than the local payload.jar."""
    note = getattr(session, "note", None)
    if note:
        return str(note)
    if not hasattr(session, "build_id"):
        return None  # not a host Session (a test double): nothing to compare
    return _stale_build_note(session.build_id)


def _add_session_note(result: Any, call: _CallState) -> None:
    """Add the note of the session ``call`` used last to ``result`` (a tool's
    dict, an error included), after any note the tool gave itself."""
    if not isinstance(result, dict) or not call.sessions:
        return
    note = _session_note(call.sessions[-1])
    if not note:
        return
    own = result.get("note")
    if not own:
        result["note"] = note
    elif note not in str(own):
        result["note"] = f"{str(own).rstrip()} Also: {note}"


def _stale_build_note(agent_build: Optional[str]) -> Optional[str]:
    """A hint when a reused session runs another build than build-out/payload.jar."""
    try:
        from inspector_widget import inject
        if inject.build_matches(agent_build, inject.local_build_id()):
            return None
    except Exception:  # noqa: BLE001 - informational only
        return None
    return ("this session's agent runs a different build than the local payload.jar "
            "(rebuilt since it was injected?); attach with force=true to replace it")


def tool_dump_tree(
    serial: str,
    package: str,
    include_properties: bool = False,
    include_resolution_stack: bool = False,
    include_screenshot: bool = False,
    scale: float = 1.0,
    root_id: int = 0,
) -> Dict[str, Any]:
    _require(package, "package")
    serial = _serial(serial)
    scale = _clamp_scale(scale)
    root_id = _as_int(root_id, "root_id")
    from inspector_widget import results, strings as st
    session = SESSIONS.get_or_attach(serial, package)
    resp = session.dump_tree(
        root_id=root_id,
        include_properties=bool(include_properties),
        include_resolution_stack=bool(include_resolution_stack),
        include_screenshot=bool(include_screenshot),
        screenshot_scale=scale,
    )
    result = results.dump_tree(st.dump_tree_to_dict(resp), serial, package,
                               include_properties=bool(include_properties))
    if include_screenshot and resp.HasField("screenshot"):
        with _png_output(serial, package, "tree") as dest:
            result["screenshot"] = _save_screenshot_png(resp.screenshot, dest)
    return result


def tool_get_properties(
    serial: str,
    package: str,
    view_id: int,
    include_resolution_stack: bool = False,
) -> Dict[str, Any]:
    _require(package, "package")
    view_id = _as_int(view_id, "view_id")
    serial = _serial(serial)
    from inspector_widget import results, strings as st
    session = SESSIONS.get_or_attach(serial, package)
    resp = session.get_properties(
        view_id=view_id, include_resolution_stack=bool(include_resolution_stack)
    )
    if not resp.HasField("group"):
        return {"serial": serial, "package": package, "view_id": view_id, "group": None}
    return results.get_properties(st.get_properties_to_dict(resp), serial, package)


def tool_screenshot(serial: Optional[str], package: str, scale: float = 1.0) -> Dict[str, Any]:
    _require(package, "package")
    serial = _serial(serial)
    scale = _clamp_scale(scale)
    session = SESSIONS.get_or_attach(serial, package)
    resp = session.screenshot(root_id=0, scale=scale)
    if not resp.HasField("screenshot"):
        raise ToolError("agent returned no screenshot")
    with _png_output(serial, package, "shot") as dest:
        meta = _save_screenshot_png(resp.screenshot, dest)
    meta.update({"serial": serial, "package": package})
    return meta


def tool_detach(serial: Optional[str], package: str, shutdown: bool = True) -> Dict[str, Any]:
    """shutdown=True (default): stop the agent for every client, whether or not
    this server attached it (never injects one). shutdown=False: only drop this
    server's cached connection and leave the agent running.

    Holds the app's session lock throughout, so no tool attaches to the app
    in between; a call that loses its session to this shutdown won't retry."""
    _require(package, "package")
    serial = _serial(serial)
    with SESSIONS.detaching(serial, package, stop=bool(shutdown)) as cached:
        return _detach_locked(serial, package, bool(shutdown), cached)


def _detach_locked(serial: str, package: str, shutdown: bool,
                   cached: Optional[Any]) -> Dict[str, Any]:
    """tool_detach's body; the caller holds the key lock and dropped ``cached``."""
    if not shutdown:
        if cached is None:
            return {"serial": serial, "package": package, "detached": False,
                    "note": "no cached session for this (serial, package)"}
        _disconnect(cached)
        return {"serial": serial, "package": package, "detached": True, "agent_stopped": False}
    session = cached
    if session is not None and not _session_alive(session):
        # A dead connection, or the app restarted under a new pid: stopping
        # through it would reach nothing. Find the agent that runs now, as the
        # CLI's detach does.
        _disconnect(session)
        session = None
    if session is None:
        session = HOST.connect_existing(serial, package)
        if session is None:
            return {"serial": serial, "package": package, "detached": cached is not None,
                    "note": "no agent is running in this app; nothing to stop"}
    stopped = bool(session.shutdown())
    result = {"serial": serial, "package": package, "detached": True, "agent_stopped": stopped}
    if not stopped:
        result["note"] = ("the agent was asked to stop but its socket is still there; it may "
                          "be finishing another client's request, or the app is frozen. Retry "
                          "detach, or force-stop the app (adb shell am force-stop <package>).")
    return result


def tool_dump_compose(
    serial: str, package: str,
    include_semantics: bool = True, include_slot_table: bool = True,
    enable_inspection: bool = False,
) -> Dict[str, Any]:
    """Dump the Compose layer (semantics tree + slot table) of the app's UI."""
    _require(package, "package")
    serial = _serial(serial)
    from inspector_widget import results, strings as st
    session = SESSIONS.get_or_attach(serial, package)
    if enable_inspection:
        # The hot reload re-mints semantics ids: the next capture must not carry
        # refs by device id (even if this request fails after reaching the agent).
        _note_hot_reload(serial, package, getattr(session, "pid", None))
    resp = session.dump_compose(include_semantics=include_semantics,
                                include_slot_table=include_slot_table,
                                enable_inspection=enable_inspection)
    data = results.with_target(st.dump_compose_to_dict(resp), serial, package)
    if include_slot_table and not enable_inspection:
        note = results.compose_note(data, "enable_inspection=true", st.ENABLE_INSPECTION_WARNING)
        if note:
            data["note"] = note
    return data


def tool_compose_overlay(serial: str, package: str, scale: float = 1.0,
                         labeled_only: bool = True) -> Dict[str, Any]:
    """Screenshot the app and draw every on-screen Compose element (text/role/bounds)
    as a labeled box over it. Returns the annotated PNG path + the on-screen text list."""
    _require(package, "package")
    serial = _serial(serial)
    from inspector_widget import strings as st, overlay as ov, png as pngmod
    scale = _clamp_scale(scale)
    session = SESSIONS.get_or_attach(serial, package)
    # Overlay uses the semantics tree only (clean, on-screen text labels; no recompose).
    data = st.dump_compose_to_dict(
        session.dump_compose(include_semantics=True, include_slot_table=False))
    roots = [w["root"] for w in data.get("windows", []) if w.get("root")]
    shot = session.screenshot(root_id=0, scale=scale)
    if not shot.HasField("screenshot"):
        raise ToolError("agent returned no screenshot")
    base_scale = float(shot.screenshot.scale) or scale
    with _png_scratch(serial, package, "compose_base") as base, \
            _png_output(serial, package, "compose_overlay") as out:
        pngmod.write_png(shot.screenshot, base)
        summary = ov.render_compose_overlay(base, roots, out, labeled_only=labeled_only,
                                            scale=base_scale)
    on_screen: List[Dict[str, Any]] = []

    def _collect(n: Dict[str, Any]) -> None:
        a = n.get("attrs", {}) or {}
        t = a.get("Text") or a.get("ContentDescription")
        b = (n.get("bounds") or {}).get("layout")
        if t and b:
            on_screen.append({"text": t, "role": a.get("Role"), "bounds": b})
        for ch in n.get("children", []) or []:
            _collect(ch)

    for r in roots:
        _collect(r)
    return {
        "serial": serial, "package": package, "path": out, "overlay_path": out,
        "diagnostics": data.get("diagnostics"), "boxes": summary["boxes"],
        "labels": summary["labels"], "size": summary["size"], "on_screen": on_screen,
    }


def _h_dump_compose(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_dump_compose(
        args.get("serial"), args.get("package"),
        include_semantics=args.get("include_semantics", True),
        include_slot_table=args.get("include_slot_table", True),
        enable_inspection=args.get("enable_inspection", False),
    )


def _h_compose_overlay(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_compose_overlay(
        args.get("serial"), args.get("package"),
        scale=args.get("scale", 1.0),
        labeled_only=not args.get("all_boxes", False),
    )


def _session_get_windows(session: Any) -> Dict[str, Any]:
    """``{root_ids}`` of the app's windows (a GetWindowsResponse)."""
    from inspector_widget import strings as st
    return st.get_windows_to_dict(session.get_windows())


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
class ToolError(Exception):
    """Raised for invalid arguments / tool-level failures; surfaced to the agent."""


class ServerClosingError(ToolError):
    """The server is shutting down (exit cleanup began): nothing attaches any more."""


def _require(value: Any, name: str) -> None:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        raise ToolError(f"missing required argument: {name}")


def _clamp_scale(scale: Any) -> float:
    try:
        s = float(scale)
    except (TypeError, ValueError):
        raise ToolError(f"scale must be a number, got {scale!r}")
    if s <= 0:
        s = 1.0
    # Contract: capture scale <= 1.0.
    return min(s, 1.0)


def _as_int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be an integer, got {value!r}")


# --------------------------------------------------------------------------- #
# Tool registry — shared by both the real-MCP and fallback servers.
# Each entry: name -> (handler, json-schema, description).
# Handlers take a single `args: dict` and return a JSON-serialisable dict.
# --------------------------------------------------------------------------- #
def _h_list_devices(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_list_devices()


# Handlers use args.get(...) (not indexing) so the tool functions' own _require()
# validators raise clean ToolError messages instead of a raw KeyError when a
# client omits a required argument.
def _h_list_processes(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_list_processes(args.get("serial"))


def _h_attach(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_attach(args.get("serial"), args.get("package"), force=args.get("force", False))


def _h_dump_tree(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_dump_tree(
        args.get("serial"),
        args.get("package"),
        include_properties=args.get("include_properties", False),
        include_resolution_stack=args.get("include_resolution_stack", False),
        include_screenshot=args.get("include_screenshot", False),
        scale=args.get("scale", 1.0),
        root_id=args.get("root_id", 0),
    )


def _h_get_properties(args: Dict[str, Any]) -> Dict[str, Any]:
    if "view_id" not in args:
        raise ToolError("missing required argument: view_id")
    return tool_get_properties(
        args.get("serial"),
        args.get("package"),
        args.get("view_id"),
        include_resolution_stack=args.get("include_resolution_stack", False),
    )


def _h_screenshot(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_screenshot(args.get("serial"), args.get("package"), scale=args.get("scale", 1.0))


def _h_detach(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_detach(args.get("serial"), args.get("package"),
                       shutdown=args.get("shutdown", True))


_SERIAL = {"type": "string", "description": "Default: $ANDROID_SERIAL or the only device"}
_PACKAGE = {"type": "string"}  # the tools say "debuggable app"; list_processes lists them
# Screenshot scale; 0 < scale <= 1 says it all (tools/list stays small, spec 2.4).
_SCALE = {"type": "number", "default": 1.0, "exclusiveMinimum": 0, "maximum": 1.0}

# --------------------------------------------------------------------------- #
# Accessibility tools (dump / lint / overlay).
# --------------------------------------------------------------------------- #
#: How long a density / font-scale probe stays fresh. Short: an agent testing at
#: a large font or display size changes them between calls (settings put
#: system font_scale, wm density), and every lint and capture must use the
#: values the device has now, as the CLI (which probes on every run) does.
_METRICS_TTL_S = 2.0


def _a11y_device_metrics(serial: str):
    """(density_dpi, font_scale) of the device now, for the lint and captures:
    probed at most once per _METRICS_TTL_S per serial (one tool call's
    probes share one read; the next call re-reads)."""
    from inspector_widget import adb
    cache = _a11y_device_metrics.__dict__.setdefault("_cache", {})
    now = time.monotonic()
    hit = cache.get(serial)
    if hit is not None and len(hit) == 3 and now - hit[2] < _METRICS_TTL_S:
        return hit[0], hit[1]
    try:
        density = adb.display_density(serial)
    except Exception:
        density = None  # the lint assumes 420dpi and says so in its diagnostics
    try:
        fscale = adb.font_scale(serial)
    except Exception:
        fscale = 1.0
    cache[serial] = (density, fscale, now)
    return density, fscale


def _a11y_lint_rules(rules: Any):
    """Validate rule ids/aliases up front so a typo is a clear tool error."""
    from inspector_widget import a11y_lint
    if rules is not None and not isinstance(rules, (list, tuple, str)):
        raise ToolError(f"rules must be a list of rule ids, got {type(rules).__name__}")
    try:
        return a11y_lint.resolve_rule_ids(rules)
    except a11y_lint.UnknownRuleError as e:
        raise ToolError(str(e))


def tool_dump_accessibility(
    serial: str, package: str,
    include_extras: bool = True, include_rendering_info: bool = False,
) -> Dict[str, Any]:
    """Dump the unified AccessibilityNodeInfo tree (Views + Compose virtual nodes)
    exactly as TalkBack/UiAutomator see it, plus the host-computed reading order."""
    _require(package, "package")
    serial = _serial(serial)
    from inspector_widget import a11y as a11ymod, correlate, results
    session = SESSIONS.get_or_attach(serial, package)
    resp = session.dump_a11y(root_id=0, include_extras=bool(include_extras),
                             include_rendering_info=bool(include_rendering_info))
    data = a11ymod.a11y_to_dict(resp)
    correlate.record_a11y(session, data)  # its Compose keys re-resolve in inspect_node
    return results.with_target(data, serial, package)


def tool_a11y_lint(
    serial: str, package: str,
    include_contrast: bool = True, scale: float = 1.0,
    wcag_mode: bool = False, rules: Optional[List[str]] = None,
    include_rendering_info: bool = True,
) -> Dict[str, Any]:
    """Run the host-side accessibility lint (R1..R23) over the unified a11y tree
    (Views + Compose, joined with Compose semantics detail). Returns findings with
    typed node keys plus a summary and diagnostics. Contrast samples each window."""
    _require(package, "package")
    serial = _serial(serial)
    from inspector_widget import a11y_lint, correlate, results
    enabled = _a11y_lint_rules(rules)
    scale = _clamp_scale(scale)
    session = SESSIONS.get_or_attach(serial, package)
    density, fscale = _a11y_device_metrics(serial)
    report = a11y_lint.run_lint(
        session, density=density, font_scale=fscale,
        include_contrast=bool(include_contrast), scale=scale, wcag_mode=bool(wcag_mode),
        rules=enabled, include_rendering_info=bool(include_rendering_info))
    correlate.record_a11y(session, report.a11y_data, (report.compose_data or {}).get("windows"))
    return results.a11y_lint(report.to_dict(), serial, package)


def tool_a11y_overlay(
    serial: str, package: str, scale: float = 1.0,
    include_contrast: bool = True, wcag_mode: bool = False,
) -> Dict[str, Any]:
    """Screenshot the app and draw every accessibility node (box + speakable label
    + TalkBack reading-order number), color-coded by lint severity. Returns the
    annotated PNG path plus the lint summary."""
    _require(package, "package")
    serial = _serial(serial)
    from inspector_widget import a11y as a11ymod, a11y_lint, overlay as ov
    scale = _clamp_scale(scale)
    session = SESSIONS.get_or_attach(serial, package)
    # One a11y dump feeds both the boxes/reading order and the lint.
    a11y_data = a11ymod.a11y_to_dict(session.dump_a11y(
        root_id=0, include_extras=True, include_rendering_info=True))
    density, fscale = _a11y_device_metrics(serial)
    report = a11y_lint.run_lint(
        session, density=density, font_scale=fscale,
        include_contrast=bool(include_contrast), scale=scale, wcag_mode=bool(wcag_mode),
        a11y_data=a11y_data)
    lint_out = report.to_dict()
    findings = lint_out["findings"]
    # The ones under an open dialog go in too: the overlay counts them and draws none.
    covered = lint_out.get("covered_findings") or []
    with _png_scratch(serial, package, "a11y_base") as base, \
            _png_output(serial, package, "a11y_overlay") as out:
        try:
            base_scale = ov.write_screen_png(session, a11y_data, base, scale=scale)
        except RuntimeError as exc:
            raise ToolError(str(exc)) from None
        summary = ov.render_a11y_overlay(base, a11y_data, out, findings=findings + covered,
                                         scale=base_scale)
    result = {
        "serial": serial, "package": package,
        "path": out, "overlay_path": out,
        "boxes": summary["boxes"], "labels": summary["labels"],
        "flagged": summary["flagged"], "flagged_by_bounds": summary.get("flagged_by_bounds"),
        "size": summary["size"],
        "finding_count": len(findings),
        "summary": lint_out["summary"],
        "lint_diagnostics": lint_out["diagnostics"],
        "diagnostics": a11y_data.get("diagnostics"),
    }
    if summary.get("covered_windows"):
        # a window under an open dialog: not drawn, its findings only counted
        result["covered_windows"] = summary["covered_windows"]
        result["findings_covered"] = summary.get("findings_covered", 0)
    return result


def _h_dump_accessibility(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_dump_accessibility(
        args.get("serial"), args.get("package"),
        include_extras=args.get("include_extras", True),
        include_rendering_info=args.get("include_rendering_info", False),
    )


def _h_a11y_lint(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_a11y_lint(
        args.get("serial"), args.get("package"),
        include_contrast=args.get("include_contrast", True),
        scale=args.get("scale", 1.0),
        wcag_mode=args.get("wcag_mode", False),
        rules=args.get("rules"),
        include_rendering_info=args.get("include_rendering_info", True),
    )


def _h_a11y_overlay(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_a11y_overlay(
        args.get("serial"), args.get("package"),
        scale=args.get("scale", 1.0),
        include_contrast=args.get("include_contrast", True),
        wcag_mode=args.get("wcag_mode", False),
    )


# Rule ids are checked by _a11y_lint_rules (a clear error naming the valid ids), not
# by an enum in the schema: listing all 47 ids and aliases would cost ~900 B of
# tools/list on every session (spec 2.4).
_A11Y_RULE_ITEMS: Dict[str, Any] = {"type": "string"}


TOOLS: Dict[str, Dict[str, Any]] = {
    "list_devices": {
        "handler": _h_list_devices,
        "description": (
            "List adb devices and emulators (serial, API level, ABI). Call first."
        ),
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "list_processes": {
        "handler": _h_list_processes,
        "description": (
            "List a device's debuggable packages (only those can be inspected) and the pid of "
            "each running one."
        ),
        "schema": {
            "type": "object",
            "properties": {"serial": _SERIAL},
            "additionalProperties": False,
        },
    },
    "attach": {
        "handler": _h_attach,
        "description": (
            "Inject the agent into a RUNNING debuggable app and open a session (idempotent; "
            "other tools auto-attach): pid, warm, reused, API level, ABI, agent version, "
            "window_count, root_ids. A dropped session re-attaches by itself."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "force": {"type": "boolean", "default": False,
                          "description": "Stop any running agent and inject a fresh one."},
            },
            "required": ["package"],
            "additionalProperties": False,
        },
    },
    "dump_tree": {
        "handler": _h_dump_tree,
        "description": (
            "The View hierarchy (auto-attaches). Nodes carry id (uniqueDrawingId, for "
            "get_properties), class_name, bounds [x,y,w,h] in screen px (render when "
            "transformed), resource, text and flags. max_depth=1 lists the window roots."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "include_properties": {
                    "type": "boolean",
                    "default": False,
                    "description": "Add each view's non-default attributes.",
                },
                "include_resolution_stack": {
                    "type": "boolean",
                    "default": False,
                    "description": "Add where each attribute was resolved (style/layout chain).",
                },
                "include_screenshot": {
                    "type": "boolean",
                    "default": False,
                    "description": "Also save a screenshot PNG (screenshot.path).",
                },
                "scale": _SCALE,
                "root_id": {
                    "type": "integer",
                    "default": 0,
                    "description": "Window root to dump (attach's root_ids); 0 = all.",
                },
            },
            "required": ["package"],
            "additionalProperties": False,
        },
    },
    "get_properties": {
        "handler": _h_get_properties,
        "description": (
            "Every attribute of one view as {name: value}: colors #AARRGGBB, dimensions px, "
            "gravity/flags by name, resources @type/name."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "view_id": {
                    "type": "integer",
                    "description": "Its 'id' in dump_tree (uniqueDrawingId).",
                },
                "include_resolution_stack": {
                    "type": "boolean",
                    "default": False,
                    "description": "Add each attribute's source and style/layout chain.",
                },
            },
            "required": ["package", "view_id"],
            "additionalProperties": False,
        },
    },
    "screenshot": {
        "handler": _h_screenshot,
        "description": (
            "Save a PNG of the app's UI; returns path, width, height. PNGs live in a "
            "per-server temp dir deleted on exit: copy one to keep it."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "scale": _SCALE,
            },
            "required": ["package"],
            "additionalProperties": False,
        },
    },
    "dump_compose": {
        "handler": _h_dump_compose,
        "description": (
            "The Jetpack Compose layer dump_tree stops at: the semantics tree (text, "
            "contentDescription, role, state, bounds of each on-screen element) and, when "
            "populated, the slot table's app composables with file:line. Auto-attaches."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "include_semantics": {"type": "boolean", "default": True,
                    "description": "Include the semantics tree."},
                "include_slot_table": {"type": "boolean", "default": True,
                    "description": "Include the slot table (composables, file:line)."},
                "enable_inspection": {"type": "boolean", "default": False,
                    "description": "Populate the slot table by hot-reloading. DESTRUCTIVE: "
                                   "resets remember{} state in every composition (dialogs, text "
                                   "input, scroll, toggles) and re-mints Compose ids."},
            },
            "required": ["package"],
            "additionalProperties": False,
        },
    },
    "compose_overlay": {
        "handler": _h_compose_overlay,
        "description": (
            "Screenshot with each on-screen Compose element boxed and labelled (text/role); "
            "returns the PNG path and the on-screen text."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "scale": _SCALE,
                "all_boxes": {"type": "boolean", "default": False,
                              "description": "Box every node, not only those with text or a role."},
            },
            "required": ["package"],
            "additionalProperties": False,
        },
    },
    "dump_accessibility": {
        "handler": _h_dump_accessibility,
        "description": (
            "The accessibility tree TalkBack sees: Views and Compose virtual nodes in one tree. "
            "Nodes carry node_key (view:<id> | compose:<acvId>:<semId>, for inspect_node), "
            "text/contentDescription/state/role, flags, bounds, actions, collection/range info. "
            "focus_order is TalkBack's reading order ({order, key, speak}); nodes under an "
            "open dialog are unreachable (covered_by). Auto-attaches."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "include_extras": {"type": "boolean", "default": True,
                    "description": "Read extras (roleDescription, Compose testTag)."},
                "include_rendering_info": {"type": "boolean", "default": False,
                    "description": "Add layout and text sizes (slow)."},
            },
            "required": ["package"],
            "additionalProperties": False,
        },
    },
    "a11y_lint": {
        "handler": _h_a11y_lint,
        "description": (
            "Lint the a11y tree (Views, Compose, RecyclerView cells, AndroidView-in-Compose), "
            "rules R1..R23: labels, touch targets, contrast (per window), roles, state, empty "
            "stops, headings, grouping, text size, duplicates, forms, links, traversal. by_rule: "
            "each rule's count, message and first node_keys (for inspect_node); group_by=none "
            "lists every finding (node_key, bounds px/dp, window, message, evidence). Those "
            "under an open dialog are apart: covered_by_rule, covered_findings."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "include_contrast": {"type": "boolean", "default": True,
                    "description": "Sample screenshots for the contrast rule (R3)."},
                "scale": _SCALE,
                "wcag_mode": {"type": "boolean", "default": False,
                    "description": "WCAG 44dp targets instead of Material 48dp."},
                "rules": {"type": "array", "items": _A11Y_RULE_ITEMS,
                    "description": "Rule ids (a11y.label.missing, ...), R1..R23 or ATF check "
                                   "names; omit for all."},
                "include_rendering_info": {"type": "boolean", "default": True,
                    "description": "Read View text sizes (R11, R18, size-aware contrast)."},
            },
            "required": ["package"],
            "additionalProperties": False,
        },
    },
    "a11y_overlay": {
        "handler": _h_a11y_overlay,
        "description": (
            "Screenshot with each a11y node boxed with its spoken label and TalkBack order, "
            "colored by lint severity. Returns the PNG path and the lint summary."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "scale": _SCALE,
                "include_contrast": {"type": "boolean", "default": True,
                    "description": "Run the contrast rule too."},
                "wcag_mode": {"type": "boolean", "default": False,
                    "description": "WCAG 44dp targets instead of Material 48dp."},
            },
            "required": ["package"],
            "additionalProperties": False,
        },
    },
    "detach": {
        "handler": _h_detach,
        "description": (
            "End a session. shutdown=true (default) stops the agent for every client (it never "
            "injects one to stop it); shutdown=false only drops this server's connection."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "shutdown": {"type": "boolean", "default": True,
                             "description": "false: only drop this server's connection."},
            },
            "required": ["package"],
            "additionalProperties": False,
        },
    },
}


# --------------------------------------------------------------------------- #
# Integrated inspector tools (integ.md PART 1 / tool list #13,#14,#15):
#   inspect          — whole-screen merged tree (view + compose + a11y, correlated)
#   inspect_node     — full dossier for ONE element + its component image + focused lint
#   component_image  — cut a per-component image (SKP by layerId else BITMAP crop)
# Defined here (before _run_tool) and registered via TOOLS.update(...).
# --------------------------------------------------------------------------- #
def tool_inspect(serial: str, package: str, include_properties: bool = False,
                 include_overlay: bool = False) -> Dict[str, Any]:
    """Whole-screen integrated tree: each node carries view/compose/a11y/image-ref + correlation."""
    _require(package, "package")
    serial = _serial(serial)
    from inspector_widget import correlate, results
    session = SESSIONS.get_or_attach(serial, package)
    merged = correlate.inspect_tree(session, include_properties=bool(include_properties))
    result = results.inspect(merged, serial, package)
    if include_overlay:
        from inspector_widget import overlay as ov
        with _png_scratch(serial, package, "integrated_base") as base:
            out = _tmp_png_path(serial, package, "integrated_overlay")
            try:  # every window (a dialog included), composited at its screen origin
                base_scale = ov.write_windows_png(session, correlate.window_origins(merged), base)
                result["overlay"] = ov.render_integrated_overlay(base, merged, out, scale=base_scale)
            except (RuntimeError, AttributeError) as exc:
                _remove_quietly(out)
                result["overlay_error"] = str(exc)
            except BaseException:
                _remove_quietly(out)
                raise
    return result


def tool_inspect_node(serial: str, package: str, node_key: Optional[str] = None,
                      view_id: Optional[int] = None, semantics_id: Optional[int] = None,
                      bounds: Optional[Dict[str, Any]] = None,
                      include_image: bool = True) -> Dict[str, Any]:
    """Full dossier for ONE element (by node_key | view_id | semantics_id | bounds)."""
    _require(package, "package")
    from inspector_widget import correlate
    if not any(v is not None for v in (node_key, view_id, semantics_id, bounds)):
        raise ToolError("inspect_node needs one of: node_key, view_id, semantics_id, bounds")
    vid = _as_int(view_id, "view_id") if view_id is not None else None
    sid = _as_int(semantics_id, "semantics_id") if semantics_id is not None else None
    serial = _serial(serial)
    session = SESSIONS.get_or_attach(serial, package)
    density, fscale = _a11y_device_metrics(serial)
    with contextlib.ExitStack() as stack:
        image_path = (stack.enter_context(_png_output(serial, package, "dossier"))
                      if include_image else None)
        try:
            # lint=True: the a11y_lint report (same rules, Compose detail, rendering info,
            # contrast of the node's window) filtered to this node.
            dossier = correlate.inspect_node(
                session, node_key=node_key, view_id=vid, semantics_id=sid, bounds=bounds,
                include_image=bool(include_image), image_path=image_path,
                lint=True, density=density, font_scale=fscale,
            )
        except correlate.NodeKeyError as exc:
            raise ToolError(str(exc)) from None
        if dossier is None:
            raise ToolError("no matching element found for the given selector")
        if image_path and not (dossier.get("component_image") or {}).get("path"):
            _remove_quietly(image_path)
    from inspector_widget import results
    return results.with_target(dossier, serial, package)


def tool_component_image(serial: str, package: str, node_key: Optional[str] = None,
                         view_id: Optional[int] = None, semantics_id: Optional[int] = None,
                         bounds: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Cut a per-component image for one element (SKP by graphicsLayer layerId, else BITMAP crop)."""
    _require(package, "package")
    from inspector_widget import correlate
    if not any(v is not None for v in (node_key, view_id, semantics_id, bounds)):
        raise ToolError("component_image needs one of: node_key, view_id, semantics_id, bounds")
    vid = _as_int(view_id, "view_id") if view_id is not None else None
    sid = _as_int(semantics_id, "semantics_id") if semantics_id is not None else None
    serial = _serial(serial)
    session = SESSIONS.get_or_attach(serial, package)
    merged = correlate.inspect_tree(session, include_properties=False)
    try:
        node = correlate.find_node(merged, node_key=node_key, view_id=vid,
                                   semantics_id=sid, bounds=bounds)
    except correlate.NodeKeyError as exc:
        raise ToolError(str(exc)) from None
    if node is None:
        raise ToolError("no matching element found for the given selector")
    with _png_output(serial, package, "component") as out:
        img = correlate.component_image(session, node, out_path=out, merged=merged)
        if not img.get("path"):
            _remove_quietly(out)
    from inspector_widget import results
    return results.with_target(img, serial, package, node_key=node.get("node_key"))


def _h_inspect(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_inspect(
        args.get("serial"), args.get("package"),
        include_properties=args.get("include_properties", False),
        include_overlay=args.get("include_overlay", False),
    )


def _h_inspect_node(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_inspect_node(
        args.get("serial"), args.get("package"),
        node_key=args.get("node_key"), view_id=args.get("view_id"),
        semantics_id=args.get("semantics_id"), bounds=args.get("bounds"),
        include_image=args.get("include_image", True),
    )


def _h_component_image(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_component_image(
        args.get("serial"), args.get("package"),
        node_key=args.get("node_key"), view_id=args.get("view_id"),
        semantics_id=args.get("semantics_id"), bounds=args.get("bounds"),
    )


_BOUNDS_SCHEMA = {
    "type": "object",
    "description": "Screen px; the deepest covering element.",
    "properties": {
        "x": {"type": "integer"}, "y": {"type": "integer"},
        "w": {"type": "integer"}, "h": {"type": "integer"},
    },
    "required": ["x", "y", "w", "h"],
    "additionalProperties": False,
}

TOOLS.update({
    "inspect": {
        "handler": _h_inspect,
        "description": (
            "The whole screen as one merged tree: Views with every ComposeView's semantics "
            "grafted in (RecyclerView cells, AndroidView nesting) and a11y joined by id. Keys "
            "view:<id>, compose:<acvId>:<semId>, composeview:<acvId>. Nodes carry bounds, view{}, "
            "compose{}, a11y{} (TalkBack order) and conf when the join is not exact; summary "
            "has counts."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "include_properties": {"type": "boolean", "default": False,
                    "description": "Inline view properties (view.properties)."},
                "include_overlay": {"type": "boolean", "default": False,
                    "description": "Also render an overlay PNG (overlay.path)."},
            },
            "required": ["package"],
            "additionalProperties": False,
        },
    },
    "inspect_node": {
        "handler": _h_inspect_node,
        "description": (
            "Everything about ONE element, by node_key (from inspect, dump_accessibility or "
            "a11y_lint), view_id, semantics_id or bounds: view properties, a11y, compose attrs "
            "(file:line when the slot table is populated), context (window > list row > "
            "ComposeView), a component image PNG and its lint findings. Old Compose keys are "
            "re-resolved (resolved_from)."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "node_key": {"type": "string",
                    "description": "view:<id> | compose:<acvId>:<semId> | composeview:<acvId>."},
                "view_id": {"type": "integer", "description": "A view's uniqueDrawingId."},
                "semantics_id": {"type": "integer",
                    "description": "Ambiguous across ComposeViews: prefer node_key."},
                "bounds": _BOUNDS_SCHEMA,
                "include_image": {"type": "boolean", "default": True,
                    "description": "Cut the component image (component_image.path)."},
            },
            "required": ["package"],
            "additionalProperties": False,
        },
    },
    "component_image": {
        "handler": _h_component_image,
        "description": (
            "Save one element's image as a PNG: from the SKP by its Compose graphicsLayer id when "
            "available, else cropped from its own window's screenshot. Returns path, source "
            "(skp | bitmap_crop) and window."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "node_key": {"type": "string",
                    "description": "view:<id> | compose:<acvId>:<semId> | composeview:<acvId>."},
                "view_id": {"type": "integer", "description": "A view's uniqueDrawingId."},
                "semantics_id": {"type": "integer", "description": "A Compose node's semantics id."},
                "bounds": _BOUNDS_SCHEMA,
            },
            "required": ["package"],
            "additionalProperties": False,
        },
    },
})


# --------------------------------------------------------------------------- #
# TalkBack navigation (inspector_widget.talkback). DEVICE-WIDE: these turn the
# system screen reader on (snapshotting the settings first) and restore it.
#
# One implementation: ops.talkback / ops.tb_walk / ops.tb_scenario, registered in
# inspector_widget.surface (which also generates the CLI subcommands). The entries
# below are only how the DEFAULT listing (legacy + talkback) presents them, byte for
# byte as before the capture tools existed (surface.legacy_talkback); the surface
# entries replace the handlers and schemas when the capture section registers them.
# --------------------------------------------------------------------------- #
_DEVICE_WIDE = (
    "DEVICE-WIDE: TalkBack runs for every app; settings are snapshotted and restored "
    "afterwards, at exit, or by talkback(action='restore'). ")
_TB_ANNOTATIONS = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False,
                   "openWorldHint": False}


def _tb_unavailable(args: Dict[str, Any]) -> Dict[str, Any]:
    """Replaced by the surface's handler; reached only when the host package
    (inspector_widget.surface) does not import."""
    raise ToolError("the TalkBack tools need the inspector_widget host package")


_TB_LEGACY_ENTRIES: Dict[str, Dict[str, Any]] = {
    "talkback": {
        "handler": _tb_unavailable,
        "annotations": dict(_TB_ANNOTATIONS, title="TalkBack status / on / off / restore",
                            idempotentHint=True),
        "description": (
            _DEVICE_WIDE + "status (read-only): enabled, services, a pending restore, "
            "injectors. on: TalkBack on, log level VERBOSE (walks read its words; stays on "
            "until off/restore/exit). off: TalkBack off. restore: write the snapshot back."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "action": {"type": "string", "enum": ["status", "on", "off", "restore"]},
                "package": dict(_PACKAGE, description="on: the app to keep in the foreground."),
                "verbose_log": {"type": "boolean", "default": True,
                                "description": "on: log level VERBOSE first; restore resets it."},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    "tb_walk": {
        "handler": _tb_unavailable,
        "annotations": dict(_TB_ANNOTATIONS, title="Walk real TalkBack focus"),
        "description": (
            _DEVICE_WIDE + "Presses REAL TalkBack's next/previous, records where focus lands "
            "and what TalkBack says, and diffs that order with the model's. Returns a line per "
            "step (a ref; keys maps refs to node keys for inspect_node), how it ended, "
            "talkback_started, the diff and tb.* findings with fixes; the walk is saved. "
            "relaunch=true: TalkBack first, app restarted, as its users have it. "
            "show=<walk id>: page a saved walk, no device."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "start": {"type": "string", "default": "current",
                          "description": "current, first, or a node key / label."},
                "direction": {"type": "string", "enum": ["next", "prev"], "default": "next"},
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 300, "default": 60},
                "until": {"type": "string", "enum": ["wrap", "edge", "loop", "steps"],
                          "default": "wrap",
                          "description": "wrap: one lap; steps: exactly max_steps."},
                "expect": {"type": "array", "items": {"type": "string"},
                           "description": "Expected order (labels or node keys)."},
                "step_timeout_ms": {"type": "integer", "minimum": 100, "maximum": 10000,
                                    "default": 1500},
                "settle_ms": {"type": "integer", "minimum": 10, "maximum": 2000, "default": 120},
                "recapture": {"type": "string", "enum": ["on_unknown", "never"],
                              "default": "on_unknown"},
                "utterance": {"type": "string", "enum": ["auto", "model", "logcat"],
                              "default": "auto", "description": "model: faster, its words."},
                "injector": {"type": "string", "enum": ["auto", "uinput", "touch"],
                             "default": "auto"},
                "leave_on": {"type": "boolean", "default": False},
                "max_lines": {"type": "integer", "minimum": 5, "maximum": 300, "default": 60},
                "max_bytes": {"type": "integer", "minimum": 1000, "maximum": 100000,
                              "default": 5000},
                "relaunch": {"type": "boolean", "default": False},
                "show": {"type": "string"},
                "steps": {"type": "string", "description": "e.g. 17-42"},
                "speech": {"type": "string", "enum": ["cut", "full"], "default": "cut"},
                "findings": {"type": "string", "enum": ["compact", "all", "none"],
                             "default": "compact"},
            },
            "required": ["package"],
            "additionalProperties": False,
        },
    },
    "tb_scenario": {
        "handler": _tb_unavailable,
        "annotations": dict(_TB_ANNOTATIONS, title="TalkBack focus scenarios"),
        "description": (
            _DEVICE_WIDE + "Where REAL TalkBack focus goes. focus_after: do action, classify "
            "the landing (tb.initial_focus). restore: activate the target, go back, classify "
            "(tb.restore_failed). survive: focus the target, apply mutate, classify over wait_ms "
            "(tb.focus_reset/lost/drift). relaunch=true: TalkBack first, app restarted."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "serial": _SERIAL,
                "package": _PACKAGE,
                "kind": {"type": "string", "enum": ["focus_after", "restore", "survive"]},
                "target": {"type": "string",
                           "description": "Node key or label; default: current focus."},
                "action": {"type": "string", "default": "activate",
                           "description": "activate | back | tap (bypasses TalkBack) | "
                                          "key:<combo>, e.g. key:META+SPACE"},
                "mutate": {"type": "string",
                           "description": "survive: tap:<selector> | activate | key:<combo> | "
                                          "broadcast:<args> | probe:<action>."},
                "wait_ms": {"type": "integer", "minimum": 300, "maximum": 20000, "default": 2000},
                "injector": {"type": "string", "enum": ["auto", "uinput", "touch"],
                             "default": "auto"},
                "leave_on": {"type": "boolean", "default": False},
                "step_timeout_ms": {"type": "integer", "minimum": 100, "maximum": 10000,
                                    "default": 1500},
                "settle_ms": {"type": "integer", "minimum": 10, "maximum": 2000, "default": 120},
                "relaunch": {"type": "boolean", "default": False},
            },
            "required": ["package", "kind"],
            "additionalProperties": False,
        },
    },
}


TOOLS.update(_TB_LEGACY_ENTRIES)  # in place: the listing order is the default's
#: How the default listing presents the TalkBack tools (no handler: the surface's runs).
_TB_LEGACY_LISTING: Dict[str, Dict[str, Any]] = {
    name: {k: entry[k] for k in ("description", "schema", "annotations")}
    for name, entry in _TB_LEGACY_ENTRIES.items()}


# Phase-0 output parameters (detail, max_bytes, max_depth, root, user_code_only,
# focus_order, group_by, filter), generated from inspector_widget.output's one
# OUTPUT_PARAMS table; the CLI gets the same flags from it (add_cli_flags).
# If the host package won't import, the server still starts (compact JSON, no
# output parameters) and --self-check says why.
try:
    from inspector_widget import output
except Exception:  # pragma: no cover - depends on the host package
    output = None  # type: ignore[assignment]
if output is not None:
    output.augment_schemas(TOOLS)


# Tools that manage the session themselves (attach re-attaches once on its own;
# detach must never re-attach), so _run_tool never retries them. The TalkBack
# tools press keys device-wide: a retry would repeat half a walk.
_NO_RETRY = frozenset({"attach", "detach", "talkback", "tb_walk", "tb_scenario"})

# Tools that only read the app's state, so running one twice is harmless even if
# the agent acted on the first request before the connection went. dump_compose
# joins them unless enable_inspection hot-reloads the app, and capture unless
# slots="enable" does (see _read_only). The other capture-and-walk tools read
# the store only.
_READ_ONLY_TOOLS = frozenset({
    "list_devices", "list_processes", "dump_tree", "get_properties", "screenshot",
    "compose_overlay", "dump_accessibility", "a11y_lint", "a11y_overlay", "inspect",
    "inspect_node", "component_image",
    "captures", "outline", "find", "node", "image", "lint", "diff",
})


def _read_only(name: str, args: Dict[str, Any]) -> bool:
    if name == "dump_compose":
        return not args.get("enable_inspection")
    if name == "capture":
        return args.get("slots") != "enable"
    return name in _READ_ONLY_TOOLS


def _retry_refusal(name: str, args: Dict[str, Any], exc: BaseException,
                   call: _CallState) -> Optional[str]:
    """Why a call that lost its session must not be retried, or None to retry.

    Never while the server shuts down, never for the _NO_RETRY tools, never once a
    detach stopped the agent the call used (the retry would re-inject it); and
    a call that changes the app (dump_compose with enable_inspection) only when
    the request provably never reached the agent (NotSentError). Never
    capture(slots="enable"): its hot reload is one of several requests, so a
    later request's NotSentError says nothing about the reload already sent.
    """
    if _closing.is_set():
        return "the server is shutting down"
    if name in _NO_RETRY:
        return "this tool is never retried"
    if SESSIONS.stopped_during(call):
        return "the app was detached while this call ran"
    if _read_only(name, args):
        return None
    if name == "capture":
        # capture sends several requests, the hot reload first: a NotSentError
        # proves only that the failing (later) request never left, not that
        # the delivered DumpCompose(enable_inspection) did not run.
        return ("capture(slots=\"enable\") hot-reloads the app before it reads it; "
                "never run twice")
    if isinstance(exc, _not_sent_error()):
        return None
    return "the request may have reached the agent, and running it twice is not harmless"


def _run_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Invoke a tool handler, converting any failure into a structured error dict.

    Every tool returns a plain dict — including failures ({"error": ..., "tool": ...}
    plus a "hint" when there is a next step to suggest) — so a bad argument, a
    missing device, an unavailable skiaparser/Pillow/grpcio, or an agent ERROR
    never raises a raw exception (or hangs) at the transport layer.

    Arguments are validated against the tool's inputSchema first, the same way
    for every transport. If the agent drops the session mid-call (idle timeout,
    app restart), a read-only call is retried once on a fresh attach (see
    _retry_refusal for when it isn't). A result from a tool that used a session
    carries that session's warning as "note", as the CLI prints it.
    """
    entry = TOOLS.get(name)
    if entry is None:
        return {"error": f"unknown tool: {name}", "tool": name,
                "available_tools": sorted(TOOLS.keys())}
    if args is None:
        args = {}
    call = _CallState()
    outer = _current_call()
    _CALL.state = call
    try:
        result = _run_tool_call(name, entry, args, call)
    finally:
        _CALL.state = outer
    _add_session_note(result, call)
    return result


def _run_tool_call(name: str, entry: Dict[str, Any], args: Any,
                   call: _CallState) -> Dict[str, Any]:
    """_run_tool's body, with ``call`` as this thread's call state.

    A capture-and-walk tool (an entry with ``on_error``) validates its own
    arguments (bad_args) and reports every failure in its error envelope."""
    on_error = entry.get("on_error")
    try:
        args = _normalize_arguments(entry["schema"], args)
        if on_error is None:
            _validate_arguments(name, entry["schema"], args)
        try:
            return entry["handler"](args)
        except _session_lost_error() as exc:
            refusal = _retry_refusal(name, args, exc, call)
            if refusal is not None:
                log.info("tool %s: %s; not retrying: %s", name, exc, refusal)
                raise
            # The client closed itself, so the retry's get_or_attach sees a dead
            # session and re-attaches.
            log.info("tool %s: %s; re-attaching and retrying once", name, exc)
            return entry["handler"](args)
    except ToolError as exc:
        if on_error is not None:
            return on_error(_session_lost_error()(str(exc)))  # closing / detached meanwhile
        return {"error": str(exc), "tool": name}
    except Exception as exc:  # never leak a stack trace through the transport
        if on_error is not None:
            log.warning("tool %s failed: %s", name, exc)
            return on_error(exc)
        if _is_expected_error(exc):
            log.warning("tool %s failed: %s", name, exc)
        else:
            log.exception("tool %s failed", name)
        out = {"error": f"{type(exc).__name__}: {exc}", "tool": name}
        hint = getattr(exc, "hint", None)
        frozen = _frozen_note(exc, args)
        if frozen:
            out["error"] = f"{out['error'].rstrip('.')}. {frozen}"
            hint = _frozen_hint()
        if isinstance(hint, str) and hint:
            out["hint"] = hint
        return out


def _frozen_note(exc: BaseException, args: Dict[str, Any]) -> Optional[str]:
    """For a timeout: whether the app sits frozen in the background (the likely
    cause, and not one the generic timeout hint names)."""
    try:
        from inspector_widget import inject
        from inspector_widget.client import AgentTimeoutError
    except Exception:  # pragma: no cover - host package missing
        return None
    package = args.get("package") if isinstance(args, dict) else None
    if not isinstance(exc, AgentTimeoutError) or not isinstance(package, str) or not package:
        return None
    serial = args.get("serial")
    return inject.frozen_note(serial if isinstance(serial, str) and serial else None, package)


def _frozen_hint() -> str:
    from inspector_widget import inject
    return inject.FROZEN_HINT


def _session_lost_error() -> type:
    try:
        from inspector_widget.client import SessionLostError
    except Exception:  # pragma: no cover - host package missing
        return _NeverRaised
    return SessionLostError


def _not_sent_error() -> type:
    try:
        from inspector_widget.client import NotSentError
    except Exception:  # pragma: no cover - host package missing
        return _NeverRaised
    return NotSentError


def _is_expected_error(exc: BaseException) -> bool:
    """Errors with a clear message of their own (no traceback needed in the log)."""
    try:
        from inspector_widget.adb import AdbError, DeviceError
        from inspector_widget.client import ClientError, TransportError
        from inspector_widget.inject import InjectionError
    except Exception:  # pragma: no cover - host package missing
        return False
    return isinstance(exc, (AdbError, DeviceError, ClientError, TransportError, InjectionError))


# --------------------------------------------------------------------------- #
# Capture and walk (inspector_widget.surface / ops): capture once, then query the
# stored capture. One registry generates these tools and the CLI subcommands.
# They are always registered (callable by name); INSPECTOR_WIDGET_TOOLSET picks
# what tools/list shows (_listed_tools).
# --------------------------------------------------------------------------- #
try:
    from inspector_widget import surface
except Exception:  # pragma: no cover - depends on the host package
    surface = None  # type: ignore[assignment]

_TOOLSET_ENV = "INSPECTOR_WIDGET_TOOLSET"  # surface.ENV_TOOLSET
_OPS: Optional[Any] = None
_OPS_LOCK = threading.Lock()


class _McpSessions:
    """The ops layer's SessionProvider over this server's session cache."""

    def get(self, serial: str, package: str) -> Any:
        return SESSIONS.get_or_attach(serial, package)

    def live_pid(self, serial: str, package: str) -> Optional[int]:
        session = SESSIONS.peek(serial, package)
        return getattr(session, "pid", None) if session is not None else None

    def device(self, serial: str) -> Dict[str, Any]:
        density, fscale = _a11y_device_metrics(serial)
        return {"dpi": density, "font_scale": fscale}

    def close_all(self) -> None:  # the server's exit cleanup owns the sessions
        pass


def _ops_context() -> Any:
    """The capture store and session provider of this server, created on first use
    (again when $INSPECTOR_WIDGET_CAPTURE_DIR or _PERSIST names another store)."""
    global _OPS
    from inspector_widget import ops
    from inspector_widget.capture.model import default_store_root
    from inspector_widget.capture.store import CaptureStore, env_persist
    root, persist = default_store_root(), env_persist()
    with _OPS_LOCK:
        if _OPS is None or _OPS.store.configured_root != root or _OPS.store.persist != persist:
            _OPS = ops.OpContext(CaptureStore(), _McpSessions(), "mcp")
        # what an agent of this server can call: TalkBack results hint only listed tools
        # (and name node keys where the capture tools are not listed)
        _OPS.listed = frozenset(_listed_tools())
        return _OPS


def _remember_session(serial: str, package: str) -> None:
    """attach makes (serial, package) the default session of the capture tools."""
    if surface is None:
        return
    try:
        from inspector_widget import ops
        ops.remember_session(_ops_context(), serial, package)
    except Exception:  # noqa: BLE001 - a convenience, never a failure
        log.debug("could not record the default session", exc_info=True)


def _note_hot_reload(serial: str, package: str, pid: Optional[int]) -> None:
    if surface is None:
        return
    with contextlib.suppress(Exception):
        _ops_context().bump_generation(serial, package, pid)


if surface is not None:
    TOOLS.update({
        name: dict(entry, on_error=surface.error_result)
        for name, entry in surface.mcp_entries(
            "all", context=_ops_context, passthrough=(_session_lost_error(),)).items()
    })


def _listed_tools() -> Dict[str, Dict[str, Any]]:
    """The TOOLS entries tools/list shows: the INSPECTOR_WIDGET_TOOLSET toolset
    (default: the 15 legacy tools and the TalkBack tools), in TOOLS order. Beside
    the legacy tools (and without the capture tools) the TalkBack tools keep
    their pre-capture description and schema (surface.legacy_talkback); every
    call runs the surface's implementation."""
    if surface is None:
        return dict(TOOLS)
    try:
        names = set(surface.toolset_names())
    except ValueError as exc:
        log.warning("%s; listing the default toolset", exc)
        names = set(surface.toolset_names(surface.DEFAULT_TOOLSET))
    listed = {name: entry for name, entry in TOOLS.items() if name in names}
    if surface.legacy_talkback(names):
        for name, shape in _TB_LEGACY_LISTING.items():
            if name in listed:
                listed[name] = dict(listed[name], **shape)
    elif "tb_walk" in listed and "outline" not in names:
        # the TalkBack tools alone: no capture loop to point at
        listed["tb_walk"] = dict(listed["tb_walk"], description=surface.D_TB_WALK_ALONE)
    return listed


def _instructions() -> Optional[str]:
    """The MCP ``instructions`` for the listed toolset (at most 900 B)."""
    if surface is None:
        return None
    return surface.instructions(_listed_tools())


# --------------------------------------------------------------------------- #
# Argument validation against each tool's inputSchema (all transports).
# --------------------------------------------------------------------------- #
_VALIDATORS: Dict[str, Any] = {}


def _normalize_arguments(schema: Dict[str, Any], args: Any) -> Any:
    """``args`` with an explicit null for an optional argument dropped (null
    means "use the default", as omitting it does) and a whole-number float for
    an integer one made an int (12.0 -> 12; JSON has a single number type and
    some clients send every number as a float). Nested objects too. Anything
    else is left for validation to report."""
    if not isinstance(args, dict):
        return args
    props = schema.get("properties") or {}
    required = set(schema.get("required") or ())
    out: Dict[str, Any] = {}
    for key, value in args.items():
        spec = props.get(key)
        if spec is None:
            out[key] = value  # unknown: validation names it
            continue
        if value is None and key not in required:
            continue
        typ = spec.get("type")
        if typ == "integer" and _whole_float(value):
            value = int(value)
        elif typ == "object" and isinstance(value, dict) and spec.get("properties"):
            value = _normalize_arguments(spec, value)
        out[key] = value
    return out


def _whole_float(value: Any) -> bool:
    return isinstance(value, float) and value.is_integer()


def _validate_arguments(name: str, schema: Dict[str, Any], args: Any) -> None:
    """Raise ToolError if ``args`` doesn't match ``schema``.

    Uses jsonschema (a dependency of the mcp SDK) when importable, else a
    minimal check of required / unknown / type / numeric bounds.
    """
    if not isinstance(args, dict):
        raise ToolError(f"arguments must be a JSON object, got {type(args).__name__}")
    try:
        import jsonschema  # type: ignore
    except Exception:
        _minimal_validate(schema, args)
        return
    validator = _VALIDATORS.get(name)
    if validator is None:
        cls = jsonschema.validators.validator_for(schema)
        validator = _VALIDATORS[name] = cls(schema)
    errors = sorted(validator.iter_errors(args), key=lambda e: (len(e.path), list(map(str, e.path))))
    if errors:
        raise ToolError(_describe_schema_error(errors[0], schema))


def _describe_schema_error(err: Any, schema: Dict[str, Any]) -> str:
    where = ".".join(str(p) for p in err.path)
    if err.validator == "required":
        missing = [r for r in err.validator_value if r not in (err.instance or {})]
        field = ".".join([where, missing[0]]) if where and missing else (missing[0] if missing else where)
        return f"missing required argument: {field}"
    if err.validator == "additionalProperties":
        allowed = set((err.schema or {}).get("properties", {}))
        extra = sorted(k for k in (err.instance or {}) if k not in allowed)
        return (f"unknown argument(s): {', '.join(extra)}"
                + (f" in {where}" if where else "")
                + f" (allowed: {', '.join(sorted(allowed))})")
    return f"invalid argument {where or '(arguments)'}: {err.message}"


_JSON_TYPES: Dict[str, Tuple[type, ...]] = {
    "string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,),
    "array": (list,), "object": (dict,),
}


def _check_bounds(spec: Dict[str, Any], value: Any, name: str) -> None:
    """The numeric bounds of ``spec``, with jsonschema's wording."""
    checks = (
        ("minimum", lambda v, b: v < b, "less than the minimum"),
        ("exclusiveMinimum", lambda v, b: v <= b, "less than or equal to the minimum"),
        ("maximum", lambda v, b: v > b, "greater than the maximum"),
        ("exclusiveMaximum", lambda v, b: v >= b, "greater than or equal to the maximum"),
    )
    for keyword, fails, words in checks:
        if keyword in spec and fails(value, spec[keyword]):
            raise ToolError(f"invalid argument {name}: {value!r} is {words} of {spec[keyword]!r}")


def _minimal_validate(schema: Dict[str, Any], args: Dict[str, Any], where: str = "") -> None:
    """The jsonschema-free fallback: required, unknown, type and numeric bounds."""
    props = schema.get("properties", {})
    for req in schema.get("required", []):
        if req not in args:
            raise ToolError(f"missing required argument: {where}{req}")
    if schema.get("additionalProperties") is False:
        extra = sorted(k for k in args if k not in props)
        if extra:
            raise ToolError(f"unknown argument(s): {', '.join(extra)} "
                            f"(allowed: {', '.join(sorted(props))})")
    for key, value in args.items():
        spec = props.get(key) or {}
        typ = spec.get("type")
        ok = True
        if typ in _JSON_TYPES:
            # As jsonschema: a bool is no number, and 12.0 is an integer.
            ok = (isinstance(value, _JSON_TYPES[typ]) or (typ == "integer" and _whole_float(value))
                  ) and not (typ in ("integer", "number") and isinstance(value, bool))
        if not ok:
            raise ToolError(f"invalid argument {where}{key}: {value!r} is not of type '{typ}'")
        if typ in ("integer", "number"):
            _check_bounds(spec, value, f"{where}{key}")
        if typ == "object" and isinstance(value, dict) and spec.get("properties"):
            _minimal_validate(spec, value, f"{where}{key}.")
        if typ == "array" and isinstance(value, list):
            item_type = (spec.get("items") or {}).get("type")
            if item_type in _JSON_TYPES and not all(isinstance(v, _JSON_TYPES[item_type])
                                                    for v in value):
                raise ToolError(f"invalid argument {where}{key}: every item must be a {item_type}")


# --------------------------------------------------------------------------- #
# Real MCP SDK path.
# --------------------------------------------------------------------------- #
def _call_tool_text(name: str, arguments: Dict[str, Any]) -> Tuple[str, bool]:
    """Run a tool and render its MCP text payload. Returns ``(text, is_error)``.

    Shared by the SDK transport and the JSON-RPC fallback so both report
    failures identically (``isError: true`` + a JSON ``{"error": ...}`` body).
    ``_run_tool`` converts handler failures into a top-level ``{"error": ...}``
    dict rather than raising, so that key is what marks a failed call.

    Every legacy result leaves through the output layer (Phase 0): the brief
    rules of ``output.slim`` unless ``detail="full"``, then ``output.finalize``,
    which encodes compact JSON and, over ``max_bytes``, writes the result to a
    spill file and returns the spill envelope instead. An unknown or ambiguous
    ``root`` comes back from ``slim`` as an error. The capture-and-walk tools
    budget their own responses and are only encoded (compact JSON).
    """
    text, _images, is_error = _call_tool(name, arguments)
    return text, is_error


def _call_tool(name: str, arguments: Dict[str, Any]) -> Tuple[str, List[Tuple[str, str]], bool]:
    """``(text, images, is_error)``: :func:`_call_tool_text` plus the images a
    capture-and-walk tool returns beside its text (``image(inline=true)``), as
    ``(mime, base64)`` pairs for MCP ImageContent."""
    try:
        args = {} if arguments is None else arguments
        result = _run_tool(name, args)
        if surface is not None and isinstance(result, surface.Result):
            return result.text(), result.images, result.is_error
        text, is_error = _render_result(name, result, args)
        return text, [], is_error
    except (ToolError, HostUnavailableError) as exc:
        return _compact({"error": str(exc)}), [], True
    except Exception as exc:  # surfaced to the agent, not raised
        log.exception("tool %s failed", name)
        return _compact({"error": f"{type(exc).__name__}: {exc}", "tool": name}), [], True


def _compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)


def _render_result(name: str, result: Any, args: Any) -> Tuple[str, bool]:
    """``(text, is_error)`` for a tool's result dict and the arguments it was called
    with: errors as they are (compact), everything else slimmed and budgeted."""
    if surface is not None and isinstance(result, surface.Result):
        return result.text(), result.is_error
    failed = isinstance(result, dict) and "error" in result
    if failed or output is None:
        return _compact(result), failed
    entry = TOOLS.get(name)
    if entry is not None and isinstance(args, dict):
        args = _normalize_arguments(entry["schema"], args)  # as the tool saw them
    if not isinstance(args, dict):
        args = {}
    brief = output.slim(name, result, args)
    text = output.finalize(name, brief, max_bytes=args.get("max_bytes"),
                           detail=args.get("detail"))
    return text, isinstance(brief, dict) and "error" in brief


def _build_mcp_server() -> Any:
    """Construct the `mcp` SDK server, or return None if the SDK is absent.

    Supports both SDK API generations:
      - mcp 1.x: ``Server(name)`` + ``@server.list_tools()`` / ``@server.call_tool()``
        decorators.
      - mcp 2.x: the decorators are gone; handlers are passed to the constructor
        as ``on_list_tools`` / ``on_call_tool`` and receive ``(ctx, params)``.
    Raises if the SDK is importable but neither API shape fits, so
    ``--self-check`` can report it instead of the server dying at startup.
    """
    try:
        import mcp.types as types
        from mcp.server import Server
    except Exception as exc:
        log.info("mcp SDK not available (%r); using JSON-RPC fallback.", exc)
        return None

    def tool(name: str, entry: Dict[str, Any]) -> Any:
        kw = dict(name=name, description=entry["description"], inputSchema=entry["schema"])
        if entry.get("annotations"):
            try:
                return types.Tool(annotations=entry["annotations"], **kw)
            except Exception:  # noqa: BLE001 - an SDK without ToolAnnotations
                pass
        return types.Tool(**kw)

    def tool_list() -> List[Any]:
        return [tool(name, entry) for name, entry in _listed_tools().items()]

    async def run_tool(name: str, arguments: Optional[Dict[str, Any]]) -> Any:
        text, images, is_error = await asyncio.to_thread(_call_tool, name, arguments or {})
        content = [types.TextContent(type="text", text=text)]
        content += [types.ImageContent(type="image", data=data, mimeType=mime)
                    for mime, data in images]
        return types.CallToolResult(content=content, isError=is_error)

    instructions = _instructions()

    def new_server(**kw: Any) -> Any:
        """Server(...) with the instructions, or without them on an SDK that has
        no such parameter (they are also in each tool's description)."""
        if instructions:
            try:
                return Server("inspector-widget", version=_SERVER_INFO["version"],
                              instructions=instructions, **kw)
            except TypeError:
                pass
        return Server("inspector-widget", version=_SERVER_INFO["version"], **kw)

    if hasattr(Server, "list_tools"):  # mcp 1.x decorator API
        server = new_server()

        @server.list_tools()
        async def list_tools() -> List[Any]:  # type: ignore[misc]
            return tool_list()

        # _run_tool validates arguments for every transport; turn the SDK's own
        # check off (where supported) so a bad argument gets the same JSON error.
        try:
            call_tool_decorator = server.call_tool(validate_input=False)
        except TypeError:
            call_tool_decorator = server.call_tool()

        @call_tool_decorator
        async def call_tool(name: str, arguments: Dict[str, Any]) -> Any:  # type: ignore[misc]
            return await run_tool(name, arguments)

        return server

    async def on_list_tools(ctx: Any, params: Any) -> Any:  # mcp 2.x handler API
        return types.ListToolsResult(tools=tool_list())

    async def on_call_tool(ctx: Any, params: Any) -> Any:
        return await run_tool(params.name, params.arguments)

    return new_server(on_list_tools=on_list_tools, on_call_tool=on_call_tool)


def _serve_with_mcp() -> bool:
    """Try to serve using the real `mcp` SDK. Returns True if it ran."""
    global _SDK_TRANSPORT
    server = _build_mcp_server()
    if server is None:
        return False
    from mcp.server.stdio import stdio_server
    _SDK_TRANSPORT = True

    async def _main() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    asyncio.run(_main())
    return True


# --------------------------------------------------------------------------- #
# Fallback: minimal MCP-over-stdio (JSON-RPC 2.0). Used only when `mcp` is
# missing. Implements initialize / tools/list / tools/call and ignores
# notifications. Messages are newline-delimited JSON on stdin/stdout (line
# framing), which is what `claude mcp` and most MCP stdio clients accept.
# --------------------------------------------------------------------------- #
_PROTOCOL_VERSION = "2024-11-05"
_SERVER_INFO = {"name": "inspector-widget", "version": "1.0.0"}


def _jsonrpc_result(req_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _fallback_handle(message: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(message, dict):
        # Batches (arrays) and bare scalars aren't MCP requests.
        what = "batch requests are not supported" if isinstance(message, list) \
            else "a request must be a JSON object"
        return _jsonrpc_error(None, -32600, f"invalid request: {what}")
    method = message.get("method")
    req_id = message.get("id")
    if not isinstance(method, str):
        return _jsonrpc_error(req_id, -32600, "invalid request: 'method' must be a string")
    if "id" not in message:
        # A notification: never answered, not even with an error (JSON-RPC 2.0
        # section 4.1). MCP's are notifications/* (initialized, cancelled, ...),
        # which need nothing from this server; any other is ignored rather than
        # run, since its result could never be reported.
        if not method.startswith("notifications/"):
            log.debug("ignoring JSON-RPC notification %r", method)
        return None
    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _jsonrpc_error(req_id, -32602, "invalid params: 'params' must be an object")

    if method.startswith("notifications/"):
        # Sent as a request (with an id) by mistake: a request is always
        # answered, and there is nothing to report but that it arrived.
        return _jsonrpc_result(req_id, {})

    if method == "initialize":
        result = {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": _SERVER_INFO,
        }
        instructions = _instructions()
        if instructions:
            result["instructions"] = instructions
        return _jsonrpc_result(req_id, result)

    if method == "ping":
        return _jsonrpc_result(req_id, {})

    if method == "tools/list":
        tools = [
            {
                "name": name,
                "description": entry["description"],
                "inputSchema": entry["schema"],
                **({"annotations": entry["annotations"]} if entry.get("annotations") else {}),
            }
            for name, entry in _listed_tools().items()
        ]
        return _jsonrpc_result(req_id, {"tools": tools})

    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str):
            return _jsonrpc_error(req_id, -32602, "invalid params: 'name' must be a string")
        arguments = params.get("arguments")
        text, images, is_error = _call_tool(name, {} if arguments is None else arguments)
        content = [{"type": "text", "text": text}]
        content += [{"type": "image", "data": data, "mimeType": mime} for mime, data in images]
        return _jsonrpc_result(req_id, {"content": content, "isError": is_error})

    return _jsonrpc_error(req_id, -32601, f"method not found: {method}")


def _serve_fallback() -> None:
    """Newline-delimited JSON-RPC 2.0 loop on stdin/stdout."""
    log.info("Serving Inspector Widget MCP via JSON-RPC stdio fallback (mcp SDK absent).")
    stdin = sys.stdin
    stdout = sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            err = _jsonrpc_error(None, -32700, "parse error")
            stdout.write(json.dumps(err) + "\n")
            stdout.flush()
            continue
        try:
            response = _fallback_handle(message)
        except Exception as exc:  # pragma: no cover - the traceback goes to stderr only
            log.exception("internal error handling a JSON-RPC message")
            if isinstance(message, dict) and "id" not in message:
                continue  # a notification: no reply, not even an error
            req_id = message.get("id") if isinstance(message, dict) else None
            response = _jsonrpc_error(req_id, -32603, f"internal error: {type(exc).__name__}: {exc}")
        if response is not None:
            stdout.write(json.dumps(response, default=str) + "\n")
            stdout.flush()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _self_check() -> int:
    """Print tool surface + dependency status; for diagnostics.

    Exits non-zero when something the server needs to start is broken (host
    package, proto gencode, or an installed-but-incompatible mcp SDK). Missing
    optional deps are reported with the tools they degrade, but don't fail.
    """
    failed = False
    print("Inspector Widget MCP server — self check")
    listed = _listed_tools()
    toolset = surface.active_toolset() if surface is not None else "all"
    print(f"  toolset: {toolset} ({len(listed)} listed; set {_TOOLSET_ENV} = legacy, "
          f"capture, talkback, all or a comma list)")
    print(f"  tools ({len(listed)}): {', '.join(listed)}")
    hidden = [name for name in TOOLS if name not in listed]
    if hidden:
        print(f"  not listed, callable by name ({len(hidden)}): {', '.join(hidden)}")
    try:
        HOST.host  # noqa: B018 - trigger import
        print("  inspector_widget: OK")
    except Exception as exc:
        failed = True
        print(f"  inspector_widget: UNAVAILABLE ({exc})")
    try:
        HOST.proto  # noqa: B018
        print("  view_inspection_pb2: OK")
    except Exception as exc:
        failed = True
        print(f"  view_inspection_pb2: UNAVAILABLE ({exc})")
    try:
        server = _build_mcp_server()
    except Exception as exc:
        failed = True
        print(f"  mcp SDK: INCOMPATIBLE ({type(exc).__name__}: {exc})")
    else:
        if server is None:
            print("  mcp SDK: absent (will use JSON-RPC stdio fallback)")
        else:
            print(f"  mcp SDK: {_dist_version('mcp')} OK (real MCP transport)")
    for dist, problem, degraded in _probe_optional_deps():
        if problem is None:
            print(f"  {dist}: {_dist_version(dist)} OK")
        else:
            print(f"  {dist}: {problem} — degrades: {degraded}")
    return 1 if failed else 0


def _probe_optional_deps() -> List[Tuple[str, Optional[str], str]]:
    """``[(dist, problem_or_None, what_it_degrades)]`` for the optional deps.

    grpcio is probed by loading the SKP gRPC stubs, not just ``import grpc``:
    the stubs refuse to load on a grpcio older than the one they were generated
    with, so a bare import would report an unusable grpcio as OK.
    """
    out: List[Tuple[str, Optional[str], str]] = []
    try:
        import PIL  # noqa: F401

        pil_problem = None
    except Exception:
        pil_problem = "MISSING (pip install Pillow)"
    out.append(("Pillow", pil_problem,
                "compose_overlay, a11y_overlay, inspect overlay, component_image crop fallback"))
    try:
        from inspector_widget import skia_client

        skia_client._import_skia_grpc()
        grpc_problem = None
    except Exception as exc:
        grpc_problem = f"UNUSABLE ({exc})"
    out.append(("grpcio", grpc_problem,
                "component_image SKP rendering (falls back to a screenshot crop)"))
    return out


def _dist_version(dist: str) -> str:
    try:
        from importlib.metadata import version

        return version(dist)
    except Exception:
        return "unknown version"


# True while the mcp SDK's stdio transport serves (see _on_sigterm).
_SDK_TRANSPORT = False


def _on_sigterm(signum: int, frame: Any) -> None:
    """SIGTERM: clean up (forwards, PNGs) and exit now, whichever transport runs.

    With the fallback transport, SystemExit unwinds the main thread (closing
    any half-made connection) and atexit cleans up. The SDK transport reads
    stdin on a worker thread that keeps the process alive until stdin closes,
    so SystemExit alone would leave the server running (and a later SIGKILL
    would skip the cleanup): clean up here, then exit at once. Tool calls run
    on worker threads there, so this handler never interrupts one holding the
    locks the cleanup takes.
    """
    if not _SDK_TRANSPORT:
        raise SystemExit(0)
    try:
        run_exit_hooks = getattr(atexit, "_run_exitfuncs", None)
        if run_exit_hooks is not None:
            run_exit_hooks()  # _cleanup_at_exit and any other exit hook
        else:  # pragma: no cover - not CPython
            _cleanup_at_exit()
    finally:
        os._exit(0)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="inspector-widget-mcp",
        description="Inspector Widget MCP server (Android View Layout Inspector for LLM agents).",
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="Print the tool surface and import status, then exit.",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("INSPECTOR_WIDGET_LOG") or os.environ.get("VIEWSPECTOR_LOG", "WARNING"),
        help="Logging level (DEBUG/INFO/WARNING/ERROR). Logs go to stderr.",
    )
    args = parser.parse_args(argv)

    # All logging MUST go to stderr — stdout is the MCP transport.
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.WARNING),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.self_check:
        rc = _self_check()
        # Artifact status is informational: a missing artifact warns, never fails.
        print("\n".join(_artifact_report()))
        return rc

    _log_startup_health()
    # Remove this server's adb forwards and PNG directory however it exits
    # (stdin closed, an error, or SIGTERM; see _on_sigterm). Agents keep
    # running for the next start.
    atexit.register(_cleanup_at_exit)
    try:
        import signal

        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ImportError, ValueError, OSError):  # not the main thread / unsupported
        pass

    if not _serve_with_mcp():
        _serve_fallback()
    return 0


def _log_startup_health() -> None:
    """Emit one INFO health line at startup: tool count, host/proto + optional
    deps (Pillow, grpcio, mcp SDK) and the transport that will be used. Goes to
    stderr (stdout is the MCP transport). Best-effort; never raises."""
    def _ok(fn) -> str:
        try:
            fn()
            return "ok"
        except Exception as exc:  # noqa: BLE001
            return f"UNAVAILABLE({type(exc).__name__})"

    host_status = _ok(lambda: HOST.host)
    proto_status = _ok(lambda: HOST.proto)

    def _present(mod: str) -> str:
        try:
            __import__(mod)
            return "present"
        except Exception:
            return "absent"

    deps = {dist: ("present" if problem is None else "unusable")
            for dist, problem, _ in _probe_optional_deps()}
    pillow = deps["Pillow"]
    grpcio = deps["grpcio"]
    mcp_sdk = _present("mcp")
    transport = "mcp-sdk" if mcp_sdk == "present" else "jsonrpc-fallback"
    log.info(
        "Inspector Widget MCP starting: %d tools listed (toolset %s), %d callable | host=%s "
        "proto=%s | Pillow(overlays)=%s grpcio(SKP images)=%s mcp-sdk=%s | transport=%s",
        len(_listed_tools()), surface.active_toolset() if surface is not None else "all",
        len(TOOLS), host_status, proto_status, pillow, grpcio, mcp_sdk, transport,
    )
    try:
        from inspector_widget import inject

        st = inject.artifact_status()
        if st["missing"]:
            log.warning(
                "on-device artifacts missing from %s (%s): %s. Injecting will fail; "
                "set %s to a directory built by scripts/build.sh.",
                st["dir"], st["source"], ", ".join(st["missing"]), inject.ARTIFACTS_ENV,
            )
    except Exception:  # noqa: BLE001 - health logging must never block startup
        pass


def _artifact_report() -> List[str]:
    """Self-check lines: where the on-device artifacts are looked up, and which exist.

    The directory comes from ``$INSPECTOR_WIDGET_ARTIFACTS``, then the legacy
    ``$VIEWSPECTOR_ARTIFACTS``, then the checkout's ``build-out/``. A missing
    artifact is only a warning: re-attaching to an app that already has the
    agent loaded does not need them.
    """
    try:
        from inspector_widget import inject

        st = inject.artifact_status()
    except Exception as exc:  # noqa: BLE001
        return [f"  artifacts: UNKNOWN (inspector_widget.inject unavailable: {exc})"]
    lines = [f"  artifacts: {st['dir']} (from {st['source']})"]
    lines += [f"    {name}: {'OK' if ok else 'MISSING'}" for name, ok in st["present"].items()]
    if st["build_id"]:
        lines.append(f"    build id: {st['build_id']}")
    if st["missing"]:
        lines.append(
            f"  WARNING: {len(st['missing'])} of {len(st['present'])} artifacts missing; "
            f"injecting an app will fail. Run scripts/build.sh and set "
            f"{inject.ARTIFACTS_ENV}=<checkout>/build-out."
        )
    return lines


if __name__ == "__main__":
    raise SystemExit(main())

