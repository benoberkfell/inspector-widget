"""Capture-and-walk operations: the tool functions both surfaces call (WP S1).

``capture`` snapshots one moment of one app once (views and properties, Compose
semantics and slot table, the unified accessibility tree, one screenshot per
window, lint and render signals), publishes it to the on-disk store the CLI and
the MCP server share, and answers with a short summary. ``outline``, ``find``,
``node``, ``image``, ``lint`` and ``diff`` then walk a stored capture with small,
budgeted queries; they never touch the device. ``captures`` lists and manages
the store. Spec: docs/design/capture-and-walk.md sections 5-7 and 10.

Every function takes an :class:`OpContext` (the store, a session provider and the
caller) plus the tool's arguments, and returns a JSON-ready dict. Failures raise
:class:`~inspector_widget.capture.model.OpError`; :func:`run` maps every
exception to the error envelope ``{"error": {code, message, hint, candidates?}}``.

The capture order follows ``capture/CONTRACT_NOTES.md`` ("Capture order"): fetch,
``build_index``, ``analyze`` and the previous capture's index load happen before
the store lock; ``refs.assign``, ``apply_refs`` and ``publish`` happen under it,
which serializes every publish of every process for milliseconds only.

Session defaulting (spec 5.1): explicit ``serial``/``package``, then the lineage
of a capture argument, then the caller's default session, then the single
running debuggable app on the single device. The default session is the
caller's own first (``OpContext.session``: its last attach or capture, kept in
memory by an MCP server; else ``$INSPECTOR_WIDGET_SESSION``), and only then the
store's shared one (the last attach or capture of any caller), so concurrent
agents on one store do not read or capture each other's apps. A query that the
shared default resolved, while the store also holds other apps, says which app
it read (``session``). An arg-less capture whose default app is not running
falls through to the single running debuggable app. Query tools stop before
the device steps: they never call adb.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .capture import analyzers, fetch, images, index, lines, query, refs, walks
from .capture import diff as cdiff
from .capture.model import (
    ERROR_CODES,
    REF_RE,
    CaptureOptions,
    Index,
    OpError,
    UNode,
    is_capture_id,
    is_valid_label,
)
from .capture.store import RESERVED_LABELS, CaptureStore, LoadedCapture
from .output import dumps, utf8_len

TOOL_NAMES = ("capture", "captures", "outline", "find", "node", "image", "lint", "diff")
#: The TalkBack tools (docs/design/talkback-navigation.md part 4 B): they drive the
#: real TalkBack, device-wide, and record against captures.
TB_TOOL_NAMES = ("talkback", "tb_walk", "tb_scenario")

# --------------------------------------------------------------------------- #
# Budgets and defaults (spec section 7)
# --------------------------------------------------------------------------- #
CAPTURE_MAX_BYTES = 3000
CAPTURES_MAX_BYTES = 2000
IMAGE_MAX_BYTES = 600
OUTLINE_LINES = 20          # capture preview: at most this many outline lines ...
PREVIEW_BYTES = 800         # ... and this many bytes of them
PREVIEW_BYTES_DIFF = 400    # smaller when the capture also reports a diff
PREVIEW_DEPTH = 2
ON_SCREEN_MAX = 3           # labelled stops hidden by the preview, shown by name
DIFF_LINES = 10             # capture(diff_from=...) diff lines
DIFF_MAX_BYTES = 1200
DIAGNOSTICS_MAX = 3
DIAGNOSTIC_CHARS = 120
STALE_AGE_S = 120
CAPTURES_LIMIT = 20
CAPTURE_ACTIONS = ("list", "show", "pin", "unpin", "label", "drop", "export", "gc")
EXPORT_WHAT = ("nodes", "views", "compose", "slots", "a11y", "props", "lint", "raw", "all")
#: captures(what=...): what export writes, or "walks" to list the stored TalkBack walks.
CAPTURES_WHAT = EXPORT_WHAT + ("walks",)
EXPORT_FORMATS = ("jsonl", "json", "legacy", "raw")
IMAGE_SOURCES = ("auto", "screenshot", "skp")
MEMORY_ONLY_NOTE = ("memory-only store (INSPECTOR_WIDGET_CAPTURE_PERSIST=0): this capture "
                    "lives only in this process; the CLI cannot read it")

#: Error code for a failure that is a bug here (never an OpError the library raised).
INTERNAL = "internal"


# --------------------------------------------------------------------------- #
# Context and session providers
# --------------------------------------------------------------------------- #
class SessionProvider(Protocol):
    """How the ops layer reaches a live app. MCP: the server's session cache;
    CLI: ``inspector_widget.attach`` and ``Session.close()`` at exit (never
    SHUTDOWN). Optional extras, read with getattr: ``live_pid(serial, package)``
    (a cached session's pid, without device I/O) and ``device(serial)`` ({dpi,
    font_scale})."""

    def get(self, serial: str, package: str) -> Any: ...

    def close_all(self) -> None: ...


class AttachProvider:
    """A provider that attaches with :func:`inspector_widget.attach` and keeps
    one session per app until :meth:`close_all`, which disconnects them (the
    agents keep running for the next caller). The CLI's provider, and the one
    the offline tests use."""

    def __init__(self, build_out: str | None = None, force: bool = False,
                 attach: Callable[..., Any] | None = None) -> None:
        self.build_out = build_out
        self.force = bool(force)
        self._attach = attach
        self._sessions: dict[tuple[str, str], Any] = {}
        self._lock = threading.Lock()

    def get(self, serial: str, package: str) -> Any:
        key = (serial, package)
        with self._lock:
            session = self._sessions.get(key)
        if session is not None:
            return session
        attach = self._attach
        if attach is None:
            import inspector_widget as iw
            attach = iw.attach
        session = attach(serial, package, build_out=self.build_out,
                         force_reinject=self.force)
        with self._lock:
            self._sessions[key] = session
        return session

    def live_pid(self, serial: str, package: str) -> int | None:
        with self._lock:
            session = self._sessions.get((serial, package))
        return getattr(session, "pid", None) if session is not None else None

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            close = getattr(session, "close", None) or getattr(session, "disconnect", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()


@dataclass
class OpContext:
    """What every tool function needs: the store, the session provider and who
    is calling (``"cli"`` or ``"mcp"``; the responses are identical)."""

    store: CaptureStore
    sessions: SessionProvider | None = None
    caller: str = "mcp"
    #: Compose generation per (serial, package, pid): bumped by a hot reload
    #: (capture(slots="enable") or the legacy dump_compose(enable_inspection)).
    generations: dict[tuple[str, str, int], int] = field(default_factory=dict)
    #: This caller's own default session: its last attach or capture. Wins over
    #: the store's shared default (which every caller of the store rewrites).
    session: tuple[str, str] | None = None
    #: The tool names this caller can see (an MCP listing), None for every tool (the
    #: CLI): a TalkBack result then hints only listed tools and, without ``node``,
    #: names each ref's node key (what the legacy inspect_node takes).
    listed: frozenset[str] | None = None

    def bump_generation(self, serial: str, package: str, pid: int | None) -> None:
        """Record a hot reload that did not go through capture(): semantics ids
        were re-minted, so the next capture must not carry refs by device id."""
        if pid is None:
            return
        key = (str(serial), str(package), int(pid))
        self.generations[key] = self.generations.get(key, 0) + 1

    def device(self, serial: str) -> dict[str, Any]:
        """``{dpi, font_scale}`` of the device now (the provider's, else adb's).

        Read for every capture, never kept: a11y testing changes the font scale
        and display size between captures (``settings put system font_scale``,
        ``wm density``), and a capture must report and lint at the values it
        was taken with, as a CLI capture at the same moment does."""
        probe = getattr(self.sessions, "device", None)
        metrics = probe(serial) if callable(probe) else None
        if not metrics:
            from . import adb
            metrics = {"dpi": adb.display_density(serial), "font_scale": adb.font_scale(serial)}
        return {k: v for k, v in dict(metrics).items() if v is not None}


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
def _bad(message: str, hint: str | None = None, candidates: list | None = None) -> OpError:
    return OpError("bad_args", message, hint=hint, candidates=candidates)


def error_envelope(exc: BaseException) -> dict[str, Any]:
    """The error envelope for any exception a tool raised (spec 5.1)."""
    if isinstance(exc, OpError):
        return exc.to_dict()
    from . import adb
    from .client import AgentTimeoutError, ClientError, TransportError
    from .inject import InjectionError

    msg = str(exc) or type(exc).__name__
    hint = getattr(exc, "hint", None)
    hint = hint if isinstance(hint, str) and hint else None
    if isinstance(exc, adb.DeviceError):
        code, hint = "no_session", hint or "Pass serial (see list_devices), or connect one device."
    elif isinstance(exc, AgentTimeoutError):
        code = "agent_error"
    elif isinstance(exc, TransportError):
        code = "device_lost"
    elif isinstance(exc, ClientError):
        code = "agent_error"
    elif isinstance(exc, InjectionError):
        # the app is not there to inspect (inject.py's two messages), or the agent failed
        code = "no_session" if re.search(r"is not (running|debuggable)", msg) else "agent_error"
    elif isinstance(exc, (adb.AdbError, OSError)):
        code = "device_lost"
    else:
        return {"error": {"code": INTERNAL, "message": f"{type(exc).__name__}: {msg}",
                          "hint": "A bug in Inspector Widget; INSPECTOR_WIDGET_LOG=DEBUG "
                                  "shows the traceback."}}
    return OpError(code, msg, hint=hint).to_dict()


def is_error(doc: Any) -> bool:
    return isinstance(doc, dict) and isinstance(doc.get("error"), dict)


# --------------------------------------------------------------------------- #
# Session and capture resolution
# --------------------------------------------------------------------------- #
def _explicit(v: Any) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _spec_lineage(store: CaptureStore, spec: Any) -> tuple[str, str] | None:
    """The lineage of a capture argument that names one capture (an id or a
    label that resolves on its own), else None."""
    s = _explicit(spec)
    if s is None or s == "prev" or re.match(r"^latest(~\d+)?$", s):
        return None
    try:
        cid = store.resolve(s, None)
    except OpError:
        return None
    try:
        return tuple(store.load(cid).meta.lineage)  # type: ignore[return-value]
    except OpError:
        return None


ENV_SESSION = "INSPECTOR_WIDGET_SESSION"


def _env_session() -> tuple[str, str] | None:
    """``$INSPECTOR_WIDGET_SESSION`` (``serial/package``): a caller's own default
    session when it keeps none in memory (a CLI in one terminal or agent)."""
    raw = (os.environ.get(ENV_SESSION) or "").strip()
    if not raw:
        return None
    serial, sep, package = raw.partition("/")
    if not sep or not serial.strip() or not package.strip():
        raise _bad(f"{ENV_SESSION} must be serial/package; got {raw!r}",
                   hint=f"e.g. {ENV_SESSION}=emulator-5554/com.example.app, or unset it")
    return serial.strip(), package.strip()


def default_session(ctx: OpContext) -> tuple[tuple[str, str] | None, bool]:
    """``(lineage, shared)``: the caller's own default session (``ctx.session``,
    else ``$INSPECTOR_WIDGET_SESSION``), else the store's shared default
    (``shared`` True: the last attach or capture of ANY caller of the store)."""
    own = ctx.session or _env_session()
    if own is not None:
        return tuple(own), False  # type: ignore[return-value]
    return ctx.store.default_session(), True


def _query_lineage(ctx: OpContext, serial: Any = None, package: Any = None,
                   specs: Iterable[Any] = ()) -> tuple[tuple[str, str] | None, bool]:
    """:func:`query_lineage`, plus whether the store's SHARED default chose it
    (no serial, package or capture argument said which app)."""
    store = ctx.store
    serial, package = _explicit(serial), _explicit(package)
    if serial and package:
        return (serial, package), False
    named = next((lin for lin in (_spec_lineage(store, s) for s in specs) if lin), None)
    default, shared = default_session(ctx)
    if not serial and not package:
        if named:
            return named, False
        return default, shared and default is not None
    for cand in (named, default):
        if cand and serial in (None, cand[0]) and package in (None, cand[1]):
            return cand, False
    hits = [lin for lin in store.lineages()
            if serial in (None, lin[0]) and package in (None, lin[1])]
    if len(hits) == 1:
        return hits[0], False
    if not hits:
        what = package if package else f"any app on {serial}"
        raise OpError("capture_not_found", f"no captures of {what} yet",
                      hint="Run capture() first.")
    raise OpError("ambiguous", f"{serial or package} matches {len(hits)} apps in the store",
                  hint="Pass both serial and package.",
                  candidates=[f"{s}/{p}" for s, p in hits])


def query_lineage(ctx: OpContext, serial: Any = None, package: Any = None,
                  specs: Iterable[Any] = ()) -> tuple[str, str] | None:
    """The lineage a query resolves ``latest``/``prev`` in (no device I/O):
    explicit serial and package, then the lineage of a capture argument that
    names one capture, then the default session (the caller's own, else the
    store's shared one). A lone serial or package picks the one lineage of the
    store that matches it. None: resolve across the whole store."""
    return _query_lineage(ctx, serial, package, specs)[0]


def _session_mark(ctx: OpContext, lineage: tuple[str, str] | None, shared: bool
                  ) -> dict[str, Any]:
    """``{"session": "serial/package"}`` when the store's shared default chose
    the app a query read and the store also holds other apps: another caller
    may have moved the shared default, so the response says which app it is."""
    if not shared or lineage is None:
        return {}
    try:
        others = any(tuple(lin) != tuple(lineage) for lin in ctx.store.lineages())
    except Exception:  # noqa: BLE001 - a nicety
        others = False
    return {"session": f"{lineage[0]}/{lineage[1]}"} if others else {}


def _device_target(ctx: OpContext, serial: Any = None, package: Any = None,
                   specs: Iterable[Any] = ()) -> tuple[tuple[str, str], bool]:
    """:func:`device_lineage`, plus whether a default session chose the app (no
    serial, package or capture argument), so a capture may fall through to the
    single running app when the default's app is gone."""
    store = ctx.store
    serial, package = _explicit(serial), _explicit(package)
    if serial and package:
        return (serial, package), False
    if not serial and not package:
        for spec in specs:
            lin = _spec_lineage(store, spec)
            if lin is not None:
                return lin, False
    default, _shared = default_session(ctx)
    if default and serial in (None, default[0]) and package in (None, default[1]):
        return default, not serial and not package
    return _running_app(serial, package), False


def device_lineage(ctx: OpContext, serial: Any = None, package: Any = None,
                   specs: Iterable[Any] = ()) -> tuple[str, str]:
    """The app a device tool (capture) targets: :func:`query_lineage`'s chain,
    then the single running debuggable app on the single device (honouring
    ``$ANDROID_SERIAL``). ``no_session`` with candidates otherwise."""
    return _device_target(ctx, serial, package, specs)[0]


def _running_app(serial: str | None, package: str | None = None,
                 gone: str | None = None) -> tuple[str, str]:
    """The single running debuggable app on ``serial`` (else the only device);
    ``no_session`` naming the candidates otherwise. ``gone``: the default
    session's app, which is not running (said in the error)."""
    from . import adb
    try:
        serial = adb.resolve_serial(serial)
    except adb.DeviceError as exc:
        raise OpError("no_session", str(exc), hint="Pass serial and package, or attach() first."
                      ) from None
    if package:
        return serial, package
    import inspector_widget as iw
    procs = iw.list_processes(serial)
    running = [p["package"] for p in procs if p.get("running")]
    if len(running) == 1:
        return serial, running[0]
    lead = f"{gone} (the default session) is not running, and " if gone else ""
    if running:
        raise OpError("no_session", f"{lead}{len(running)} debuggable apps are running on "
                                    f"{serial}",
                      hint="Pass package (or attach() first).", candidates=sorted(running))
    launch = (f"Launch it (adb -s {serial} shell monkey -p {gone} -c "
              f"android.intent.category.LAUNCHER 1), or pass package." if gone
              else "Start the app, then pass package (or attach() first).")
    raise OpError("no_session", f"{lead}no debuggable app is running on {serial}",
                  hint=launch, candidates=sorted(p["package"] for p in procs))


def _not_running(exc: BaseException) -> bool:
    """Whether an attach failed because the app is not there to inspect."""
    from .inject import InjectionError
    if isinstance(exc, OpError):
        return exc.code == "no_session"
    return isinstance(exc, InjectionError) and bool(
        re.search(r"is not (running|debuggable)", str(exc)))


def remember_session(ctx: OpContext, serial: str, package: str) -> None:
    """Make (serial, package) the caller's default session and the store's
    shared one (after an attach or a capture)."""
    ctx.session = (str(serial), str(package))
    with contextlib.suppress(Exception):  # the shared default is a convenience
        ctx.store.set_default_session(serial, package)


def _load(ctx: OpContext, spec: Any, lineage: tuple[str, str] | None) -> LoadedCapture:
    return ctx.store.load(ctx.store.resolve(_explicit(spec) or "latest", lineage))


def _loaded_for_query(ctx: OpContext, p: dict[str, Any], cursor_key: str = "cursor"
                      ) -> tuple[LoadedCapture, dict[str, Any]]:
    """Pop ``capture``/``serial``/``package`` from ``p`` and load the capture a
    query reads (a cursor names its own capture); plus the ``session`` mark
    (:func:`_session_mark`) to stamp on the response."""
    spec = p.pop("capture", None)
    serial, package = p.pop("serial", None), p.pop("package", None)
    if _explicit(spec) in (None, "latest"):  # the default: a cursor's own capture wins
        spec = query.cursor_capture(p.get(cursor_key)) or spec
    lineage, shared = _query_lineage(ctx, serial, package, [spec])
    lc = _load(ctx, spec, lineage)
    return lc, _session_mark(ctx, tuple(lc.meta.lineage), shared)  # type: ignore[arg-type]


class _Tomb(dict):
    """A lineage's tombstones plus ``next_ref``, the store's ref counter: a ref at
    or above it was never issued (query._ref_error says so)."""

    next_ref: int | None = None


def _tomb(ctx: OpContext, lc: LoadedCapture) -> dict:
    out = _Tomb()
    try:
        out.update(ctx.store.lineage_state(*lc.meta.lineage).tomb)
    except Exception:  # noqa: BLE001 - last-seen info is a nicety
        pass
    with contextlib.suppress(Exception):
        out.next_ref = int(ctx.store.peek_next_ref())
    return out


# --------------------------------------------------------------------------- #
# Staleness markers (spec 5.1)
# --------------------------------------------------------------------------- #
def staleness(ctx: OpContext, lc: LoadedCapture) -> dict[str, Any]:
    """``age_s`` (over 120 s), ``stale`` (a newer capture of the lineage exists)
    and ``pid_changed`` (the app runs under another pid now: a cached live
    session, else the lineage's latest capture, says so)."""
    store = ctx.store
    out: dict[str, Any] = {}
    age = lc.age_s()
    if age > STALE_AGE_S:
        out["age_s"] = round(age)
    serial, package = lc.meta.lineage
    latest_pid = None
    try:
        latest = store.lineage_state(serial, package).latest
    except Exception:  # noqa: BLE001
        latest = None
    if latest and latest != lc.id and store.exists(latest):
        with contextlib.suppress(OpError):
            lm = store.load(latest).meta
            dt = max(0, round(float(lm.created_at) - float(lc.meta.created_at)))
            out["stale"] = f"{latest} is newer ({dt}s)"
            latest_pid = lm.pid
    live = None
    probe = getattr(ctx.sessions, "live_pid", None)
    if callable(probe):
        with contextlib.suppress(Exception):
            live = probe(serial, package)
    now_pid = live if live is not None else latest_pid
    if now_pid is not None and lc.meta.pid is not None and int(now_pid) != int(lc.meta.pid):
        out["pid_changed"] = True
    return out


def _stamp(result: dict[str, Any], marks: Mapping[str, Any], after: str = "capture"
           ) -> dict[str, Any]:
    """``result`` with ``marks`` inserted right after key ``after``."""
    if not marks:
        return result
    out: dict[str, Any] = {}
    placed = False
    for k, v in result.items():
        out[k] = v
        if k == after:
            out.update(marks)
            placed = True
    if not placed:
        out.update(marks)
    return out


# --------------------------------------------------------------------------- #
# capture
# --------------------------------------------------------------------------- #
def _bool(name: str, v: Any, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    raise _bad(f"{name} must be true or false; got {v!r}")


def _int(name: str, v: Any, default: int, lo: int, hi: int) -> int:
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v:
        raise _bad(f"{name} must be an integer; got {v!r}")
    if not lo <= int(v) <= hi:
        raise _bad(f"{name} must be in {lo}..{hi}; got {v}")
    return int(v)


def _enum(name: str, v: Any, allowed: tuple[str, ...], default: str) -> str:
    if v is None:
        return default
    if v not in allowed:
        raise _bad(f"{name} must be one of {', '.join(allowed)}; got {v!r}")
    return v


def _check_label(label: Any) -> str | None:
    if label is None or label == "":
        return None
    if not isinstance(label, str):
        raise _bad(f"label must be a string; got {label!r}")
    name = label.removeprefix("@")
    if not is_valid_label(name) or name in RESERVED_LABELS or is_capture_id(name):
        raise _bad(f"invalid label {label!r}",
                   hint="Labels match ^[a-z][a-z0-9_-]{0,31}$ and must not look like a "
                        "capture id, latest or prev.")
    return name


def _latest(ctx: OpContext, lineage: tuple[str, str]) -> LoadedCapture | None:
    st = ctx.store.lineage_state(*lineage)
    if st.latest and ctx.store.exists(st.latest):
        with contextlib.suppress(OpError):
            return ctx.store.load(st.latest)
    return None


def capture(ctx: OpContext, serial: Any = None, package: Any = None, label: Any = None,
            props: Any = None, resolution_stack: Any = None, slots: Any = None,
            screenshot: Any = None, screenshot_scale: Any = None, skp: Any = None,
            a11y_rendering: Any = None, lint: Any = None, settle_ms: Any = None,
            diff_from: Any = None, if_changed_since: Any = None, outline_lines: Any = None,
            on_screen: Any = None, pin: Any = None, max_bytes: Any = None) -> dict[str, Any]:
    """Snapshot the app once and publish it; returns the capture summary (spec 5.3)."""
    name = _check_label(label)
    try:
        scale = float(1.0 if screenshot_scale is None else screenshot_scale)
    except (TypeError, ValueError):
        raise _bad(f"screenshot_scale must be a number in (0, 1]; got {screenshot_scale!r}"
                   ) from None
    opts = CaptureOptions(
        props=_bool("props", props, True),
        resolution_stack=_bool("resolution_stack", resolution_stack, False),
        slots=_enum("slots", slots, ("if_available", "enable", "off"), "if_available"),
        screenshot=_bool("screenshot", screenshot, True),
        screenshot_scale=scale,
        skp=_bool("skp", skp, False),
        a11y_rendering=_bool("a11y_rendering", a11y_rendering, False),
        lint=_enum("lint", lint, ("tree", "full", "none"), "tree"),
        settle_ms=_int("settle_ms", settle_ms, 0, 0, 3000)).validate()
    n_lines = _int("outline_lines", outline_lines, OUTLINE_LINES, 0, 80)
    want_on_screen = _bool("on_screen", on_screen, True)
    want_pin = _bool("pin", pin, False)
    budget = query.resolve_max_bytes(max_bytes, CAPTURE_MAX_BYTES)
    if ctx.sessions is None:
        raise OpError("unsupported", "this surface cannot reach a device")

    store = ctx.store
    lineage, implicit = _device_target(ctx, serial, package, [diff_from, if_changed_since])
    base_id = _diff_base(ctx, diff_from, lineage) if diff_from is not None else None
    fell_through = None
    try:
        session = ctx.sessions.get(*lineage)
    except Exception as exc:
        if not implicit or not _not_running(exc):
            raise
        # The default session's app is gone (spec 5.1): the single running
        # debuggable app, or no_session naming the running ones.
        gone = lineage[1]
        lineage = _running_app(lineage[0], gone=gone)
        fell_through = (f"{gone} (the default session) is not running; captured "
                        f"{lineage[1]}, the only running debuggable app")
        if diff_from is not None:
            base_id = _diff_base(ctx, diff_from, lineage)
        session = ctx.sessions.get(*lineage)
    if if_changed_since is not None:
        try:
            since: LoadedCapture | None = _load(ctx, if_changed_since, lineage)
        except OpError as e:
            if e.code != "capture_not_found":
                raise
            since = None  # nothing to compare with (e.g. the first poll): capture
        same = fetch.unchanged_since(session, since.meta, now=store.clock) if since else None
        if same is not None:
            return same

    lc, moved_from = publish_capture(ctx, lineage, session, opts, label=name, pin=want_pin)
    ix = lc.index()

    diff_doc = None
    diff_next: list[str] = []
    if base_id is not None:
        try:
            diff_doc, diff_next = _capture_diff(store.load(base_id), lc)
        except OpError as e:  # the capture is published: report the diff's failure in it
            diff_doc = {"a": base_id, "error": e.message}
    note = getattr(session, "note", None)
    notes = [n for n in (fell_through, note) if isinstance(n, str) and n]
    return _summary(ctx, lc, ix, budget=budget, n_lines=n_lines, on_screen=want_on_screen,
                    diff_doc=diff_doc, diff_next=diff_next,
                    moved_from=moved_from if moved_from and moved_from != lc.id else None,
                    note="; ".join(notes) or None)


def publish_capture(ctx: OpContext, lineage: tuple[str, str], session: Any,
                    opts: CaptureOptions, *, label: str | None = None, pin: bool = False,
                    diagnostics: Iterable[str] = ()) -> tuple[LoadedCapture, str | None]:
    """Fetch, index, analyze and publish one capture of ``session``'s app (the
    capture pipeline in ``CONTRACT_NOTES.md`` order); ``(loaded capture, the
    capture ``label`` moved from)``. ``diagnostics`` are added to the capture's
    own (a TalkBack walk says it was on). The app becomes the caller's default
    session. capture() and the TalkBack tools' captures both come through here."""
    store = ctx.store
    prev = _latest(ctx, lineage)
    pid = getattr(session, "pid", None)
    gen = 0
    if prev is not None and pid is not None and prev.meta.pid == pid:
        gen = int(prev.meta.compose_generation or 0)
    if pid is not None:
        gen = max(gen, ctx.generations.get((lineage[0], lineage[1], int(pid)), 0))
    raw = fetch.fetch(session, opts, compose_generation=gen, device=ctx.device(lineage[0]),
                      wall_clock=store.clock)
    if pid is not None:
        ctx.generations[(lineage[0], lineage[1], int(pid))] = int(raw.meta.compose_generation)
    for d in diagnostics:
        if d not in raw.meta.diagnostics:
            raw.meta.diagnostics.append(d)
    ix = index.build_index(raw)
    # Outside the store lock (CONTRACT_NOTES "Capture order"): analyze the
    # key-space index (lint="full" spends seconds on contrast) and hydrate the
    # previous capture's index.
    analyzers.analyze(ix, raw, lint=opts.lint)
    pix = prev.index() if prev is not None else None
    moved_from = None
    with store.refs_lock():  # assign + apply_refs + publish only: milliseconds
        st = store.lineage_state(*lineage)
        if st.latest != (prev.id if prev is not None else None):
            prev = _latest(ctx, lineage)  # another process published meanwhile
            pix = prev.index() if prev is not None else None
            st = store.lineage_state(*lineage)
        if label:
            moved_from = st.labels.get(label)
        same_pid, same_gen = refs.identity_flags(raw.meta, pix.meta if pix else None)
        refmap, tomb = refs.assign(ix, pix, same_pid=same_pid, same_generation=same_gen,
                                   alloc=store.next_refs)
        ix = index.apply_refs(ix, refmap)
        raw.meta.label = label
        raw.meta.pinned = pin
        cid = store.publish(raw, ix, refmap, tomb=tomb)
    lc = store.load(cid)
    ctx.session = (lineage[0], lineage[1])  # this caller's default from now on
    return lc, moved_from


def _diff_base(ctx: OpContext, spec: Any, lineage: tuple[str, str]) -> str:
    """The capture ``capture(diff_from=spec)`` compares with, resolved BEFORE the
    new capture exists (so a bad spec fails without touching the device):
    ``prev`` is the capture before the new one, i.e. today's ``latest``, and
    ``latest~N`` is today's ``latest~(N-1)``."""
    s = _explicit(spec)
    if s is None:
        raise _bad("diff_from must name a capture (an id, a label, prev or latest~N)")
    m = re.match(r"^latest(?:~(\d+))?$", s)
    if m and int(m.group(1) or 0) == 0:
        raise _bad(f"diff_from={s!r} would be this new capture itself",
                   hint='diff_from="prev" compares with the capture before it.')
    if s == "prev":
        s = "latest"
    elif m:
        s = f"latest~{int(m.group(1)) - 1}"
    return ctx.store.resolve(s, lineage)


def _capture_diff(base: LoadedCapture, lc: LoadedCapture) -> tuple[dict[str, Any], list[str]]:
    d = _diff(base, lc, limit=DIFF_LINES, max_bytes=DIFF_MAX_BYTES)
    keep = ("a", "verdict", "shared", "summary", "notes", "lines", "issues", "truncated")
    doc = {k: d[k] for k in keep if k in d}
    if doc.get("issues") == {"resolved": [], "new": []}:  # nothing else to say either
        del doc["issues"]
    nxt = [h for h in d.get("next") or [] if h.startswith("diff(")]
    return doc, nxt


def _device_line(m: Any) -> str:
    dev = m.device or {}
    screen = dev.get("screen") or []
    parts = [f"API {m.api}" if m.api else None,
             f"{screen[0]}x{screen[1]}" if len(screen) == 2 else None,
             f"{dev['dpi']}dpi" if dev.get("dpi") else None,
             f"font {dev['font_scale']}" if dev.get("font_scale") is not None else None]
    return " ".join(p for p in parts if p)


def _facet_summary(ix: Index, lc: LoadedCapture) -> dict[str, Any]:
    m = lc.meta
    kinds: dict[str, int] = {}
    a11y = 0
    for n in ix.nodes.values():
        kinds[n.kind] = kinds.get(n.kind, 0) + 1
        if "a11y" in n.ids:
            a11y += 1

    def status(name: str, count: int | None = None) -> Any:
        entry = m.facets.get(name) or {}
        st = entry.get("status")
        if st == "ok" and count is not None:
            return count
        if st in (None, "ok"):
            return count if count is not None else st
        if st == "off":
            return "off"
        return entry.get("reason") or st

    return {"views": kinds.get("view", 0), "props": status("props", kinds.get("view", 0)),
            "compose": status("compose", kinds.get("compose", 0)), "a11y": status("a11y", a11y),
            "slots": status("slots", kinds.get("slot", 0)),
            "shots": len(lc.shot_roots()), "skp": status("skp", len(lc.skp_roots()))}


def _diagnostics(ix: Index, lc: LoadedCapture) -> list[str]:
    seen: list[str] = []
    for d in [*lc.meta.diagnostics, *ix.diagnostics]:
        if d and d not in seen:
            seen.append(d)
    out = [lines.cut(d, DIAGNOSTIC_CHARS) for d in seen[:DIAGNOSTICS_MAX]]
    if len(seen) > DIAGNOSTICS_MAX:
        out.append(f"…{len(seen) - DIAGNOSTICS_MAX} more: captures(action=\"show\")")
    return out


def _refs_in(text: Iterable[str]) -> set[str]:
    return set(re.findall(r"(?:^|[\s>])(n[1-9][0-9]*)\b", " ".join(text)))


def _cost(obj: Any) -> int:
    return utf8_len(dumps(obj))


def _summary(ctx: OpContext, lc: LoadedCapture, ix: Index, *, budget: int, n_lines: int,
             on_screen: bool, diff_doc: dict | None, diff_next: list[str],
             moved_from: str | None, note: str | None) -> dict[str, Any]:
    """The capture() response (spec 5.3), within ``budget`` bytes: the header,
    then the preview outline, the labelled stops the preview hides, and next."""
    m = lc.meta
    out: dict[str, Any] = {"capture": lc.id}
    if m.label:
        out["label"] = m.label
        if moved_from:
            out["moved_from"] = moved_from
    if m.pinned:
        out["pinned"] = True
    out.update({"session": f"{m.serial}/{m.package}", "pid": m.pid, "device": _device_line(m),
                "took_ms": m.took_ms, "consistency": m.consistency,
                "facets": _facet_summary(ix, lc),
                "windows": [f"{lines.crumb(w)} {lines.fmt_bounds(w.b)} z{w.z}"
                            if w.b else f"{lines.crumb(w)} z{w.z}" for w in ix.windows()]})
    out.update(analyzers.lint_summary(ix))
    if m.options.slots == "enable":
        out["warning"] = fetch.SLOTS_ENABLE_WARNING
    diags = _diagnostics(ix, lc)
    if diags:
        out["diagnostics"] = diags
    if ctx.store.memory_only:
        out["store"] = MEMORY_ONLY_NOTE
    if note:
        out["note"] = note
    if diff_doc is not None:
        out["diff"] = diff_doc

    # next, decided up front so its bytes are reserved
    render_refs = [n.id for n in ix.nodes.values()
                   if n.kind != "slot" and any(i.id.startswith("render.") for i in n.issues)]
    lint_n = sum(1 for n in ix.nodes.values() for i in n.issues if i.id.startswith("a11y."))

    reserve = 200 + 40  # next (<= 200 B) and its key
    room = budget - _cost(out) - reserve
    preview: list[str] = []
    more_lines = 0
    shown: set[str] = set()
    if n_lines > 0 and room > 0:
        pv = query.outline(ix, depth=PREVIEW_DEPTH, max_lines=n_lines, max_bytes=0)
        cap = min(room, PREVIEW_BYTES_DIFF if diff_doc is not None else PREVIEW_BYTES)
        used = 0
        pv_lines = list(pv.get("lines") or [])
        for ln in pv_lines:
            c = utf8_len(dumps(ln)) + 1
            if preview and used + c > cap:
                break
            preview.append(ln)
            used += c
        total = pv.get("total") or len(pv_lines)
        more_lines = total - len(preview)
        shown = _refs_in(preview)
        out["outline"] = _with_marker(preview, total)
        room -= _cost(preview) + 12

    hidden: list[UNode] = []
    if on_screen and room > 60:
        for r in ix.reading:
            n = ix.nodes.get(r)
            if n is None or n.kind == "slot" or not n.label or n.id in shown:
                continue
            if not n.b or n.b[2] <= 0 or n.b[3] <= 0:
                continue
            hidden.append(n)
        if hidden:
            entries = [f"{n.id} {lines.jstr(lines.cut(n.label, lines.LABEL_MAX))}"
                       for n in hidden[:ON_SCREEN_MAX]]
            while entries and _cost(_with_marker(entries, len(hidden), READING_MORE)) + 14 > room:
                entries.pop()
            out["on_screen"] = _with_marker(entries, len(hidden), READING_MORE)

    hints: list[str | None] = [*diff_next]
    if more_lines > 0 or hidden:
        hints.append("outline()")
    if lint_n:
        hints.append("lint()")
    if len(render_refs) == 1:
        hints.append(query.call("node", render_refs[0]))
    elif render_refs:
        hints.append(query.call("find", issue="render."))
    nxt = query.next_hints(hints)
    if nxt:
        out["next"] = nxt
    return _fit(out, budget)


OUTLINE_MORE = "…{n} more line{s}: outline()"
READING_MORE = "…{n} more: outline(view=\"reading\")"


def _with_marker(items: list[str], total: int, marker: str = OUTLINE_MORE) -> list[str]:
    """``items`` plus a ``…N more`` entry saying how to get the rest of ``total``."""
    rest = total - len(items)
    if rest <= 0:
        return list(items)
    return [*items, marker.format(n=rest, s="s" if rest > 1 else "")]


def _fit(out: dict[str, Any], budget: int) -> dict[str, Any]:
    """Shed optional parts until ``out`` fits ``budget`` bytes, never the header,
    and never silently: a cut list keeps its ``…N more`` entry."""
    for key, marker in (("on_screen", READING_MORE), ("outline", OUTLINE_MORE),
                        ("diagnostics", None), ("next", None)):
        items = out.get(key)
        if _cost(out) <= budget or not isinstance(items, list):
            continue
        body = [x for x in items if not x.startswith("…")]
        total = len(body) + sum(int(m.group(1)) for x in items if x.startswith("…")
                                for m in [re.match(r"…(\d+)", x)] if m)
        while body and _cost(out) > budget:
            body.pop()
            out[key] = _with_marker(body, total, marker) if marker else body
        if not out[key] or (marker and not body and _cost(out) > budget):
            out.pop(key, None)
    if _cost(out) > budget and isinstance(out.get("diff"), dict):
        d = out["diff"]
        while d.get("lines") and _cost(out) > budget:
            d["lines"].pop()
            d["cut"] = d.get("cut", 0) + 1
    return out


# --------------------------------------------------------------------------- #
# Query tools
# --------------------------------------------------------------------------- #
def _props_ok(lc: LoadedCapture) -> bool:
    return bool(lc.meta.options.props) and lc.meta.facet_status("props") == "ok"


def outline(ctx: OpContext, **p: Any) -> dict[str, Any]:
    """``outline`` (spec 5.5) over a stored capture."""
    p = _clean(p)
    lc, mark = _loaded_for_query(ctx, p)
    ix = lc.index()
    out = query.outline(ix, loaded=lc, tomb=_tomb(ctx, lc), **p)
    return _stamp(out, {**mark, **staleness(ctx, lc)})


def find(ctx: OpContext, **p: Any) -> dict[str, Any]:
    """``find`` (spec 5.6) over a stored capture."""
    p = _clean(p)
    if "in_" in p:
        p["in"] = p.pop("in_")
    lc, mark = _loaded_for_query(ctx, p)
    ix = lc.index()
    out = query.find(ix, loaded=lc, tomb=_tomb(ctx, lc), **p)
    return _stamp(out, {**mark, **staleness(ctx, lc)})


def node(ctx: OpContext, ref: Any = None, refs: Any = None, **p: Any) -> dict[str, Any]:
    """``node`` (spec 5.7): one node (``ref``) or up to 10 (``refs``)."""
    p = _clean(p)
    if ref is not None and refs is not None:
        raise _bad("pass ref or refs, not both")
    sels = refs if refs is not None else ref
    if sels is None or sels == [] or sels == "":
        raise _bad("node needs ref (a ref, key or selector) or refs",
                   hint='node(ref="n23"), node(ref="#badSwitch"), node(refs=["n1","n2"])')
    if isinstance(sels, list) and len(sels) == 1:
        sels = sels[0]
    lc, mark = _loaded_for_query(ctx, p)
    ix = lc.index()

    def image_fn(n: UNode) -> Any:
        try:
            crop = images.crop(lc, n)
        except OpError as e:
            return {"error": e.message}
        return {"path": crop["path"], "px": crop["px"]}

    out = query.node(ix, lc, sels, tomb=_tomb(ctx, lc), image_fn=image_fn, **p)
    return _stamp(out, {**mark, **staleness(ctx, lc)})


def lint(ctx: OpContext, **p: Any) -> dict[str, Any]:
    """``lint`` (spec 5.9) over a stored capture; contrast on request (cached)."""
    p = _clean(p)
    lc, mark = _loaded_for_query(ctx, p)
    ix = lc.index()
    kw = {k: p[k] for k in ("rules", "severity", "within", "contrast", "wcag", "group",
                            "per_rule", "limit", "cursor", "max_bytes") if k in p}
    unknown = sorted(set(p) - set(kw))
    if unknown:
        raise _bad(f"unknown argument(s) for lint: {', '.join(unknown)}")
    out = analyzers.lint_view(ix, lc, **kw)
    return _stamp(out, {**mark, **staleness(ctx, lc)})


def image(ctx: OpContext, ref: Any = None, window: Any = None, overlay: Any = None,
          marks: Any = None, pad: Any = None, source: Any = None, max_side: Any = None,
          max_bytes: Any = None, walk: Any = None, **p: Any) -> dict[str, Any]:
    """``image`` (spec 5.8): a node's crop from its own window's screenshot, or
    an overlay (marks, lint, reading, bounds, compose, walk) of a window or the
    screen. Returns the PNG's path; the MCP surface can add the pixels (inline)."""
    p = _clean(p)
    kind = _enum("overlay", overlay, images.OVERLAY_KINDS, "none")
    if kind == "walk" or _explicit(walk) is not None:
        if kind not in ("walk", "none"):
            raise _bad(f"walk draws overlay=\"walk\", not {kind!r}")
        if _explicit(ref) is not None:
            raise _bad("overlay=\"walk\" draws a window or the screen, not a node's crop",
                       hint="Pass window (a window ref) to draw one window.")
        return _walk_image(ctx, walk, window, max_side, max_bytes, p)
    src = _enum("source", source, IMAGE_SOURCES, "auto")
    pad_px = _int("pad", pad, images.DEFAULT_PAD, 0, 2000)
    side = _int("max_side", max_side, images.DEFAULT_MAX_SIDE, 64, 4096)
    budget = query.resolve_max_bytes(max_bytes, IMAGE_MAX_BYTES)
    mk = "auto" if marks is None else marks
    unknown = sorted(set(p) - {"capture", "serial", "package"})
    if unknown:
        raise _bad(f"unknown argument(s) for image: {', '.join(unknown)}")
    lc, mark = _loaded_for_query(ctx, p)
    ix = lc.index()
    if src == "skp":
        if not lc.skp_roots():
            raise OpError("facet_unavailable", "this capture has no SKP",
                          hint="capture(skp=true), then image(source=\"skp\")")
        raise OpError("unsupported", "cutting images from a stored SKP is not implemented yet",
                      hint='image(source="screenshot") crops the window screenshot')
    tomb = _tomb(ctx, lc)
    target = query.resolve_selector(ix, ref, tomb=tomb) if _explicit(ref) else None
    win = query.resolve_selector(ix, window, tomb=tomb) if _explicit(window) else None
    if isinstance(mk, list):
        mk = [query.resolve_selector(ix, s, tomb=tomb).id for s in mk]
    if kind == "none" and target is not None:
        out = images.crop(lc, target, pad=pad_px, max_side=side)
    else:
        out = images.overlay(lc, ix, kind, mk, window=win.id if win else None,
                             ref=target.id if target else None, pad=pad_px, max_side=side)
    out = _stamp(dict(out), {**mark, **staleness(ctx, lc)})
    for key in ("rect", "note", "omitted", "scale"):
        if _cost(out) <= budget:
            break
        out.pop(key, None)
    return out


def _walk_image(ctx: OpContext, walk: Any, window: Any, max_side: Any, max_bytes: Any,
                p: dict[str, Any]) -> dict[str, Any]:
    """image(overlay="walk", walk=...): a stored walk drawn on the capture it started
    from (or ``capture``): TalkBack's order as numbered arrows, the model's prediction
    dashed where it differs, mismatches in red."""
    side = _int("max_side", max_side, images.DEFAULT_MAX_SIDE, 64, 4096)
    budget = query.resolve_max_bytes(max_bytes, IMAGE_MAX_BYTES)
    unknown = sorted(set(p) - {"capture", "serial", "package"})
    if unknown:
        raise _bad(f"unknown argument(s) for image: {', '.join(unknown)}")
    spec = _explicit(p.get("capture"))
    if spec == "latest":
        spec = None  # the schema default: the walk is drawn on its own capture
    lineage, shared = _query_lineage(ctx, p.get("serial"), p.get("package"),
                                     [spec] if spec else [])
    store = ctx.store
    wid = walks.resolve(store, walk, lineage)
    rec = walks.load(store, wid)
    caps = [c for c in rec.get("captures") or [] if isinstance(c, str)]
    if spec is None:
        if not caps:
            raise OpError("facet_unavailable", f"walk {wid} was recorded without a capture",
                          hint="tb_walk() again: it captures the screen it walks.")
        lc = store.load(caps[0])
    else:
        lc = _load(ctx, spec, lineage)
    ix = lc.index()
    win = query.resolve_selector(ix, window, tomb=_tomb(ctx, lc)) if _explicit(window) else None
    out = images.walk_overlay(lc, ix, rec, window=win.id if win else None, max_side=side)
    mark = _session_mark(ctx, tuple(lc.meta.lineage), shared)  # type: ignore[arg-type]
    out = _stamp(dict(out), mark)
    for key in ("legend", "note", "omitted"):
        if _cost(out) <= budget:
            break
        out.pop(key, None)
    return out


def _diff(la: LoadedCapture, lb: LoadedCapture, **kw: Any) -> dict[str, Any]:
    ia, ib = la.index(), lb.index()

    def preview(ix: Index, root: str | None) -> list[str]:
        return query.outline(ix, root=root, depth=2, max_lines=20)["lines"]

    def pixels(xa: Index, xb: Index, changed: Any) -> dict[str, Any]:
        return images.pixel_diff(la, lb, xa, xb, refs=changed)

    both = _props_ok(la) and _props_ok(lb)

    def props_fn(lc: LoadedCapture) -> Callable[[UNode], Any]:
        def get(n: UNode) -> Any:
            if n.kind != "view" or "view" not in n.ids:
                return None
            return lc.props(int(n.ids["view"]))
        return get

    return cdiff.diff(ia, ib, props_a=props_fn(la) if both else None,
                      props_b=props_fn(lb) if both else None, resolve=query.resolve_selector,
                      preview=preview, pixel_diff=pixels, **kw)


def diff(ctx: OpContext, a: Any = None, b: Any = None, **p: Any) -> dict[str, Any]:
    """``diff`` (spec 5.10): two captures of one app, compared by ref. ``a``
    defaults to ``prev`` (the capture before ``b``), ``b`` to ``latest``."""
    p = _clean(p)
    serial, package = p.pop("serial", None), p.pop("package", None)
    a_spec = _explicit(a) or "prev"
    b_spec = _explicit(b)
    if b_spec in (None, "latest"):
        b_spec = query.cursor_capture(p.get("cursor")) or "latest"
    lineage, shared = _query_lineage(ctx, serial, package, [b_spec, a_spec])
    lb = _load(ctx, b_spec, lineage)
    if a_spec == "prev":
        prev_id = lb.meta.prev
        if not prev_id or not ctx.store.exists(prev_id):
            raise OpError("capture_not_found", f"{lb.id} has no earlier capture to compare with",
                          hint="Pass a=<capture id or label> (captures() lists them).")
        la = ctx.store.load(prev_id)
    else:
        la = _load(ctx, a_spec, tuple(lb.meta.lineage))  # type: ignore[arg-type]
    out = _diff(la, lb, **p)
    mark = _session_mark(ctx, tuple(lb.meta.lineage), shared)  # type: ignore[arg-type]
    return _stamp(out, {**mark, **staleness(ctx, lb)}, after="b")


def _clean(p: Mapping[str, Any]) -> dict[str, Any]:
    """Arguments without explicit nulls (a null means the default)."""
    return {k: v for k, v in p.items() if v is not None}


# --------------------------------------------------------------------------- #
# captures
# --------------------------------------------------------------------------- #
def _home(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if home and path.startswith(home + os.sep) else path


def _mb(n: int) -> str:
    return f"{n / 1e6:.1f}MB"


def _ago(s: float) -> str:
    s = max(0, int(s))
    if s < 120:
        return f"{s}s ago"
    if s < 7200:
        return f"{s // 60}m ago"
    if s < 172800:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _ttl(s: float) -> str:
    """``24h``, ``6h``, ``30m``, ``7d`` (days only from 3 days up)."""
    s = int(s)
    for unit, n, floor in (("d", 86400, 3 * 86400), ("h", 3600, 3600), ("m", 60, 60)):
        if s >= floor and s % n == 0:
            return f"{s // n}{unit}"
    return f"{s}s"


def captures(ctx: OpContext, action: Any = None, id: Any = None, label: Any = None,
             what: Any = None, format: Any = None, all: Any = None, limit: Any = None,
             max_bytes: Any = None, serial: Any = None, package: Any = None) -> dict[str, Any]:
    """``captures`` (spec 5.4): list, show, pin, unpin, label, drop, export, gc."""
    act = _enum("action", action, CAPTURE_ACTIONS, "list")
    every = _bool("all", all, False)
    budget = query.resolve_max_bytes(max_bytes, CAPTURES_MAX_BYTES)
    store = ctx.store
    if act == "list":
        n = _int("limit", limit, CAPTURES_LIMIT, 1, 200)
        if what == "walks":
            return _walks_list(ctx, n, every, serial, package, budget)
        return _captures_list(ctx, n, every, serial, package, budget)
    if act == "gc":
        return _gc(store, every)
    if what == "walks" or _stored_walk(store, _explicit(id)):
        # what="walks" never falls through to a capture: drop id="latest" drops a walk
        return _walk_action(ctx, act, _explicit(id), serial, package,
                            None if max_bytes is None else budget)
    spec = _explicit(id) or ("latest" if act in ("show", "export") else None)
    if spec is None:
        raise _bad(f"captures(action=\"{act}\") needs id (a capture id, label, latest or prev)")
    lineage = query_lineage(ctx, serial, package, [spec])
    cid = store.resolve(spec, lineage)
    if act == "show":
        return _fit_doc(_show(ctx, store.load(cid)), budget)
    if act in ("pin", "unpin"):
        store.pin(cid, act == "pin")
        return {"capture": cid, "pinned": act == "pin"}
    if act == "label":
        name = _check_label(label)
        moved = store.label(cid, name)
        out: dict[str, Any] = {"capture": cid, "label": name}
        if moved:
            out["moved_from"] = moved
        return out
    if act == "drop":
        store.drop(cid)
        return {"dropped": cid}
    return _export(ctx, store.load(cid), _enum("what", what, EXPORT_WHAT, "nodes"),
                   _enum("format", format, EXPORT_FORMATS, "jsonl"))


def _stored_walk(store: CaptureStore, ident: str | None) -> bool:
    """An id that names a stored walk (a capture label may look like one)."""
    return walks.is_walk_id(ident) and os.path.exists(
        os.path.join(walks.walks_dir(store), f"{ident}.json"))


def _walks_list(ctx: OpContext, limit: int, every: bool, serial: Any, package: Any,
                budget: int) -> dict[str, Any]:
    """captures(what="walks"): the stored TalkBack walks and scenarios, newest first."""
    lineage = None if every else query_lineage(ctx, serial, package)
    rows, total = walks.listing(ctx.store, lineage, limit)
    out: dict[str, Any] = {"lines": rows}
    if total > len(rows):
        out["more"] = f"{total - len(rows)} older: captures(what=\"walks\",limit={min(200, total)})"
    if rows:
        out["next"] = [query.call("captures", action="show", id=rows[0].split(" ", 1)[0])]
    else:
        out["hint"] = "tb_walk() and tb_scenario() store one each."
    while _cost(out) > budget and out["lines"]:
        out["lines"].pop()
        out["more"] = f"{total - len(out['lines'])} more: captures(what=\"walks\",limit=" \
                      f"{max(1, len(out['lines']))},max_bytes={min(32000, budget * 2)})"
    return out


def _walk_action(ctx: OpContext, act: str, wid: str | None, serial: Any, package: Any,
                 budget: int | None) -> dict[str, Any]:
    """captures(action=show|export|drop, id=<walk id>): a stored walk or scenario."""
    store = ctx.store
    if act not in ("show", "export", "drop"):
        raise _bad(f"captures(action=\"{act}\") is for captures; a walk can be shown, "
                   f"exported or dropped")
    if wid is None or wid == "latest":
        wid = walks.resolve(store, None, query_lineage(ctx, serial, package), kind=None)
    else:
        wid = walks.resolve(store, wid, kind=None)  # bad_args / walk_not_found otherwise
    rec = walks.load(store, wid)
    if act == "drop":
        walks.drop(store, wid)
        return {"dropped": wid}
    if act == "export":
        path = os.path.join(walks.walks_dir(store), f"{wid}.json")
        out = {"walk": wid, "path": path, "bytes": os.path.getsize(path),
               "captures": rec.get("captures") or []}
        out["hint"] = ("JSON: steps (key, ref, cap, speak, via, bounds ...), predicted, findings "
                       "(talkback/diff.py); read with jq.")
        return out
    return walks.stored_result(rec, budget, listed=ctx.listed)


def _captures_list(ctx: OpContext, limit: int, every: bool, serial: Any, package: Any,
                   budget: int) -> dict[str, Any]:
    store = ctx.store
    lineage = None if every else query_lineage(ctx, serial, package)
    metas = store.list(lineage, limit=None)
    rows: list[str] = []
    now = store.clock()
    for m in metas[:limit]:
        with contextlib.suppress(OpError):
            lc = store.load(m.id)
            lab = f" @{m.label}" if m.label else ""
            pin = " pinned" if m.pinned else ""
            nodes = lc.node_count()
            rows.append(f"{m.id}{lab} {m.serial}/{m.package} "
                        f"{nodes if nodes is not None else '?'} nodes {_mb(lc.nbytes())} "
                        f"{_ago(now - float(m.created_at or 0))}{pin}")
    s = store.summary()
    out: dict[str, Any] = {"lines": rows}
    if len(metas) > limit:
        out["more"] = f"{len(metas) - limit} older: captures(limit={min(200, len(metas))})"
    if lineage is not None:
        others = s.get("captures", 0) - len(metas)
        if others > 0:
            out["others"] = f"{others} of other apps: captures(all=true)"
    ttl = _ttl(store.ttl_s)
    out["store"] = (f"{_home(store.root)} {_mb(int(s.get('bytes', 0)))} ttl {ttl}"
                    + (" memory-only" if store.memory_only else ""))
    while _cost(out) > budget and out["lines"]:
        out["lines"].pop()
        out["more"] = f"{len(metas) - len(out['lines'])} more: captures(limit=" \
                      f"{max(1, len(out['lines']))},max_bytes={min(32000, budget * 2)})"
    return out


def _show(ctx: OpContext, lc: LoadedCapture) -> dict[str, Any]:
    m = lc.meta
    defaults = CaptureOptions().to_dict()
    opts = {k: v for k, v in m.options.to_dict().items() if defaults.get(k) != v}
    facets = {}
    for name, entry in m.facets.items():
        st = entry.get("status")
        facets[name] = st if st == "ok" else f"{st}: {entry.get('reason')}" \
            if entry.get("reason") else st
    out: dict[str, Any] = {"capture": lc.id}
    if m.label:
        out["label"] = m.label
    if m.pinned:
        out["pinned"] = True
    out.update({"session": f"{m.serial}/{m.package}", "pid": m.pid, "device": _device_line(m),
                "agent": m.agent_version, "created": _ago(lc.age_s()), "took_ms": m.took_ms,
                "consistency": m.consistency, "compose_generation": m.compose_generation,
                "options": opts, "facets": facets, "nodes": lc.node_count(),
                "bytes": lc.nbytes(), "prev": m.prev, "path": lc.path})
    out = _stamp(out, staleness(ctx, lc))
    diags = [*m.diagnostics]
    with contextlib.suppress(Exception):
        diags += [d for d in lc.index().diagnostics if d not in diags]
    if diags:
        out["diagnostics"] = [lines.cut(d, 200) for d in diags]
    return {k: v for k, v in out.items() if v not in (None, {}, [])}


def _fit_doc(out: dict[str, Any], budget: int) -> dict[str, Any]:
    diags = out.get("diagnostics")
    while isinstance(diags, list) and diags and _cost(out) > budget:
        diags.pop()
    if isinstance(diags, list) and not diags:
        out.pop("diagnostics", None)
    for key in ("options", "facets", "path"):
        if _cost(out) <= budget:
            break
        out.pop(key, None)
    return out


def _gc(store: CaptureStore, every: bool) -> dict[str, Any]:
    res = store.gc(all=every)
    if every:
        out = {k: res[k] for k in ("all", "removed", "note") if k in res}
        n_walks = walks.wipe(store)
        if n_walks:
            out["walks_removed"] = n_walks
        return out
    if "skipped" in res:
        return {"skipped": res["skipped"]}
    removed = res.get("removed") or []
    out: dict[str, Any] = {"removed": len(removed)}
    if removed:
        out["ids"] = [f"{r.get('id')} {r.get('why')}" for r in removed[:10]]
    for k in ("stripped", "staging_purged", "trash_purged", "spill_purged", "captures", "bytes"):
        v = res.get(k)
        if v:
            out[k] = len(v) if isinstance(v, list) else v
    return out


def _export(ctx: OpContext, lc: LoadedCapture, what: str, fmt: str) -> dict[str, Any]:
    """Write the capture's data as files under its ``out/`` and return their paths
    (never the contents)."""
    from .capture.model import index_to_jsonl

    ix = lc.index()
    parts = _export_parts(what, fmt)
    written: list[tuple[str, int, int]] = []  # (path, rows, bytes)

    def put(name: str, data: bytes, rows: int) -> None:
        path = lc.put_derived(f"out/{name}", data)
        written.append((path, rows, len(data)))

    for part in parts:
        if part == "nodes":
            body = index_to_jsonl(ix)
            if fmt == "json":
                recs = [json.loads(ln) for ln in body.decode("utf-8").splitlines()[1:] if ln]
                put("nodes.json", dumps(recs).encode("utf-8"), len(recs))
            else:
                put("nodes.jsonl", body, len(ix.nodes))
        elif part in ("views", "compose", "slots", "a11y"):
            doc = _legacy_facet(lc, part)
            if doc is None:
                continue
            put(f"{part}.json", dumps(doc).encode("utf-8"), _count_nodes(doc))
        elif part == "props":
            if not _props_ok(lc):
                continue
            recs = {}
            for n in ix.nodes.values():
                if n.kind == "view" and "view" in n.ids:
                    vals = lc.props(int(n.ids["view"]))
                    if vals:
                        recs[n.id] = vals
            put("props.json", dumps(recs).encode("utf-8"), len(recs))
        elif part == "lint":
            recs = [{"ref": n.id, **i.to_dict()} for n in ix.nodes.values() for i in n.issues]
            body = ("\n".join(dumps(r) for r in recs) + "\n").encode("utf-8") if recs else b""
            put("issues.jsonl", body, len(recs))
        elif part == "raw":  # out/raw/*.pb, out/shot/w_<root>.pb, out/meta.json
            for rel, data in sorted(lc.raw_capture().files().items()):
                put(rel, data, 1)
            put("meta.json", lc.meta.to_json(), 1)
        elif part.startswith("raw:"):  # one facet's protobuf reply, as the agent sent it
            data = lc.raw(part[4:])
            if data:
                put(f"raw/{part[4:]}.pb", data, 1)
    if not written:
        raise OpError("facet_unavailable", f"capture {lc.id} has no {what} to export",
                      hint=f"captures(action=\"show\",id=\"{lc.id}\") lists its facets")
    total = sum(b for _p, _r, b in written)
    rows = sum(r for _p, r, _b in written)
    first = written[0][0]
    path = os.path.dirname(first) if len(written) > 1 else first
    raw = any(p == "raw" or p.startswith("raw:") for p in parts)
    if len(written) > 1 and (raw or what == "all"):
        path = os.path.join(lc.path, "out")
    out: dict[str, Any] = {"capture": lc.id, "path": path.rstrip(os.sep), "rows": rows,
                           "bytes": total}
    if len(written) > 1:
        out["files"] = len(written)
    out["hint"] = ("raw/*.pb are the agent's protobuf replies (proto/view_inspection.proto)."
                   if raw else "Read with jq or a JSON reader; deleted with the capture.")
    return out


#: format="raw" for one facet: the pb that holds it (props travel in views.pb).
_RAW_PART = {"views": "raw:views", "props": "raw:views", "compose": "raw:compose_sem",
             "slots": "raw:slots", "a11y": "raw:a11y"}
_LEGACY_PARTS = ("views", "compose", "slots", "a11y")


def _export_parts(what: str, fmt: str) -> list[str]:
    """What ``_export`` writes: ``format`` picks the form (spec 5.4). ``raw`` copies
    the agent's protobuf replies (the whole capture for nodes/all/raw, else the
    facet's pb); ``legacy`` regenerates the old dump_tree/dump_compose/
    dump_accessibility JSON; ``jsonl``/``json`` are the index and the facets."""
    if fmt == "raw":
        if what in ("nodes", "all", "raw"):
            return ["raw"]
        if what in _RAW_PART:
            return [_RAW_PART[what]]
        raise _bad(f"what={what!r} has no raw form",
                   hint='captures(action="export", what="raw") copies every protobuf reply.')
    if fmt == "legacy":
        if what in ("nodes", "all"):
            return list(_LEGACY_PARTS)
        if what in _LEGACY_PARTS:
            return [what]
        raise _bad(f"what={what!r} has no legacy form (views, compose, slots, a11y do)",
                   hint='format="json" exports props and lint; what="raw" the protobufs.')
    return list(EXPORT_WHAT[:-1]) if what == "all" else [what]


def _legacy_facet(lc: LoadedCapture, part: str) -> Any:
    from .proto import view_inspection_pb2 as pb

    name = {"views": "views", "compose": "compose_sem", "slots": "slots", "a11y": "a11y"}[part]
    data = lc.raw(name)
    if not data:
        return None
    if part == "views":
        from . import strings
        return strings.dump_tree_to_dict(pb.DumpTreeResponse.FromString(data))
    if part in ("compose", "slots"):
        from . import strings
        return strings.dump_compose_to_dict(pb.DumpComposeResponse.FromString(data))
    from . import a11y
    return a11y.a11y_to_dict(pb.DumpA11yResponse.FromString(data))


def _count_nodes(doc: Any) -> int:
    n = 0
    stack = [doc]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            if "children" in o:
                n += 1
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
    return n


# --------------------------------------------------------------------------- #
# TalkBack: talkback, tb_walk, tb_scenario (talkback-navigation.md part 4 B)
# --------------------------------------------------------------------------- #
TALKBACK_ACTIONS = ("status", "on", "off", "restore")
TB_DIRECTIONS = ("next", "prev")
TB_UNTIL = ("wrap", "edge", "loop", "steps")
TB_RECAPTURE = ("on_unknown", "never")
TB_UTTERANCE = ("auto", "model", "logcat")
TB_INJECTORS = ("auto", "uinput", "touch")
TB_KINDS = ("focus_after", "restore", "survive")
TB_MAX_STEPS = 60
TB_STEP_TIMEOUT_MS = 1500
TB_SETTLE_MS = 120
TB_WAIT_MS = 2000
TB_WALK_MAX_BYTES = walks.WALK_MAX_BYTES
TB_WALK_HARD_MAX = 100000
TB_SCENARIO_MAX_BYTES = walks.SCENARIO_MAX_BYTES
#: What a walk captures: the screen TalkBack walks (refs, a11y, pixels for the walk
#: overlay), without View properties (node(props=...) asks for a fresh capture).
TB_CAPTURE = CaptureOptions(props=False)


def _tb_error(exc: BaseException) -> OpError | None:
    """A TalkBack failure as an envelope code: busy, talkback_unavailable,
    enable_failed, restore_failed, app_left_foreground, injector_failed (the
    injectors tried are the candidates), keymap_unknown, start_not_found ...
    A ValueError from the engine's own argument checks is bad_args."""
    from .talkback import device as tbdevice
    from .talkback import inject as tbinject
    from .talkback import walk as tbwalk

    if isinstance(exc, (tbdevice.TalkBackError, tbwalk.WalkError, tbinject.InjectorError)):
        code = str(getattr(exc, "code", "") or "talkback_error")
        if code not in ERROR_CODES:
            code = "talkback_error"  # a TalkBack failure, not the agent's: no ViewSpector log
        tried = list(getattr(exc, "tried", None) or [])
        hint = getattr(exc, "hint", None)
        return OpError(code, str(exc),
                       hint=hint if isinstance(hint, str) and hint else ERROR_CODES.get(code),
                       candidates=tried or None)
    if isinstance(exc, ValueError) and not isinstance(exc, OpError):
        return _bad(str(exc))
    return None


def _tb_call(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return fn()
    except OpError:
        raise
    except Exception as exc:
        err = _tb_error(exc)
        if err is None:
            raise
        raise err from None


def _tb_default(ctx: OpContext, serial: str | None, package: str | None
                ) -> tuple[str, str] | None:
    """The default session a DEVICE-WIDE TalkBack tool may act on: the caller's own
    (its last attach or capture, or ``$INSPECTOR_WIDGET_SESSION``). The store's shared
    default (the last attach or capture of ANY caller: another agent's server, another
    checkout) only completes a serial or package the caller named, and never against
    ``$ANDROID_SERIAL``: TalkBack must not turn on on someone else's device."""
    default, shared = default_session(ctx)
    if default is None:
        return None
    if shared:
        if serial is None and package is None:
            return None
        env = os.environ.get("ANDROID_SERIAL")
        if serial is None and env and env != default[0]:
            return None
    if serial in (None, default[0]) and package in (None, default[1]):
        return default
    return None


def _tb_device(ctx: OpContext, serial: Any, package: Any) -> tuple[str, str | None]:
    """(serial, package) for the talkback tool: explicit, else the caller's own default
    session's (:func:`_tb_default`), else ``$ANDROID_SERIAL`` or the only device."""
    s, p = _explicit(serial), _explicit(package)
    if s is None:
        default = _tb_default(ctx, s, p)
        if default is not None:
            s, p = default[0], p or default[1]
    if s is None:
        from . import adb
        try:
            s = adb.resolve_serial(None)
        except adb.DeviceError as exc:
            raise OpError("no_session", str(exc), hint="Pass serial (see list_devices).") from None
    return s, p


def _tb_session(ctx: OpContext, serial: Any, package: Any
                ) -> tuple[tuple[str, str], Any, str | None]:
    """The app a TalkBack walk or scenario drives, its live session, and a note when the
    caller did not name it: explicit, else the caller's own default session
    (:func:`_tb_default`), else the single running debuggable app on ``$ANDROID_SERIAL``
    or the only device."""
    if ctx.sessions is None:
        raise OpError("unsupported", "this surface cannot reach a device")
    s, p = _explicit(serial), _explicit(package)
    note = None
    if s and p:
        lineage, implicit = (s, p), False
    else:
        default = _tb_default(ctx, s, p)
        if default is not None:
            lineage, implicit = default, s is None and p is None
        else:
            lineage, implicit = _running_app(s, p), False
    try:
        session = ctx.sessions.get(*lineage)
    except Exception as exc:
        if not implicit or not _not_running(exc):
            raise
        gone = lineage[1]
        lineage = _running_app(lineage[0], gone=gone)
        session = ctx.sessions.get(*lineage)
        note = (f"{gone} (the default session) is not running; drove {lineage[1]}, the only "
                f"running debuggable app")
    if note is None and not (s and p):
        note = ""  # say which app it was: the caller did not name both
    return lineage, session, note


def _looks_like_selector(sel: str) -> bool:
    """A ref, #rid, @tag, Type"label", a key or a chain: resolved in the capture.
    Anything else is the label as spoken, which the walk matches itself."""
    if sel.startswith(("compose:", "virtual:", "legacy:")):
        return False  # the walk's own node keys
    return bool(REF_RE.match(sel)) or sel[:1] in ("#", "@", '"') or '"' in sel \
        or " > " in sel or bool(re.match(r"^(view|sem|a11y|w):", sel))


class _TbCaptures:
    """The captures a TalkBack walk or scenario records against (its ``hook``).

    The first is taken once TalkBack has settled (the screen as TalkBack walks it;
    a ref given as ``start`` / ``target`` resolves there). A walk recaptures when
    focus lands on a node no capture holds (scrolled-in items), at most once per
    ``walks.RECAPTURE_EVERY`` steps, and once more at the end if steps since were
    unknown. A scenario captures before its action (again after keys moved focus to
    the target) and after it. A failed capture is a note, never a failed walk."""

    def __init__(self, ctx: OpContext, lineage: tuple[str, str], session: Any, what: str,
                 recapture: bool = False) -> None:
        self.ctx, self.lineage, self.session, self.what = ctx, lineage, session, what
        self.recapture = recapture
        self.taken: list[tuple[int, LoadedCapture]] = []
        self.notes: list[str] = []
        self._known: set[str] = set()
        self._last_at = 0
        self._last_i = 0
        self._pending = False
        self._failed = 0

    def _take(self, at: int) -> LoadedCapture | None:
        if self._failed >= 2:
            return None
        from .talkback import device as tbdevice
        try:
            top = tbdevice.top_package(self.lineage[0])
        except Exception:  # noqa: BLE001 - unknown: try the capture
            top = None
        if top and top != self.lineage[1]:
            # A backgrounded app may be frozen: its agent would only time out.
            self.notes.append(f"no capture at step {at}: {top} is in front, not "
                              f"{self.lineage[1]}")
            return None
        try:
            lc, _moved = publish_capture(self.ctx, self.lineage, self.session, TB_CAPTURE,
                                         diagnostics=[f"taken with TalkBack on ({self.what})"])
            self._known |= walks.keys_of(lc)
        except Exception as exc:  # noqa: BLE001 - the walk goes on without it
            self._failed += 1
            self.notes.append(lines.cut(f"capture at step {at} failed: {exc}", 140))
            return None
        self.taken.append((at, lc))
        self.ctx.store.held.add(lc.id)  # until release(): a full lineage evicts unlabeled first
        self._last_at, self._pending = at, False
        for d in lc.index().diagnostics:  # the model's warnings (talkback/recycler.py)
            if d.startswith("tb: ") and "not mapped" not in d and d[4:] not in self.notes:
                self.notes.append(d[4:])
        return lc

    # ---- the engine's hook (talkback.walk.run_walk / scenarios.run_scenario)
    def start(self, snap: Any) -> None:
        self._take(0)

    def resolve(self, sel: Any, stop: bool = True) -> Any:
        """A ref or selector as the node key the engine matches: the stop TalkBack
        focuses for it (``stop``), else the node itself. Labels pass through, and so does
        a label that only looks like a selector ("@alice", "#general", 'Say "Hi"',
        "Settings > Display"): only a ref that does not resolve is an error."""
        if not isinstance(sel, str) or sel in ("current", "first") or not self.taken:
            return sel
        if not _looks_like_selector(sel):
            return sel
        lc = self.taken[-1][1]
        ix = lc.index()
        try:
            node = query.resolve_selector(ix, sel, tomb=_tomb(self.ctx, lc))
        except OpError:
            if REF_RE.match(sel):
                raise
            return sel  # the label as spoken: the walk matches it itself
        return _stop_key(ix, lc, node, stop=stop) or sel

    def resolve_action(self, action: Any) -> Any:
        """The selectors in an action sequence (``tap:``, ``long_press:``, ``expect:``) as
        node keys: ``tap:<ref or selector>`` taps that node (its key), ``long_press:`` and
        ``expect:`` name the stop TalkBack focuses for it; other steps as given."""
        if not isinstance(action, str):
            return action
        out = []
        for raw in action.split(";"):
            step = raw.strip()
            head, sep, arg = step.partition(":")
            h, arg = head.strip().lower().replace("-", "_"), arg.strip()
            if sep and arg and h in ("tap", "long_press", "longpress", "expect"):
                if h != "expect" or _looks_like_selector(arg):
                    step = f"{head.strip()}:{self.resolve(arg, stop=h != 'tap')}"
            out.append(step)
        return "; ".join(x for x in out if x)

    def step(self, st: Any, snap: Any) -> None:
        self._last_i = int(getattr(st, "i", 0) or 0)
        key = getattr(st, "key", None)
        if not self.recapture or not key or key in self._known or key.startswith("legacy:"):
            return  # an agent without per-node ids: bound by label and box instead
        if self._last_i - self._last_at < walks.RECAPTURE_EVERY:
            self._pending = True
            return
        self._take(self._last_i)

    def ready(self, snap: Any, pressed: bool) -> None:
        if pressed:  # keys moved focus to the target and may have scrolled
            self._take(1)

    def finish(self, snap: Any, ended: str | None = None) -> None:
        if self.what == "tb_scenario":
            self._take(2)
        elif self._pending and self.recapture and ended not in ("left_app", "timeout"):
            self._take(self._last_i)

    def binding(self) -> walks.Binding:
        return walks.Binding(self.taken)

    def release(self) -> None:
        """Let retention evict the captures again (the walk is bound and stored)."""
        self.ctx.store.held.difference_update(lc.id for _at, lc in self.taken)

    def resolve_expect(self, expect: list[str] | None) -> list[str] | None:
        """``expect`` entries that are selectors become refs (the start capture's)."""
        if not expect or not self.taken:
            return expect
        lc = self.taken[0][1]
        ix = lc.index()
        out = []
        for e in expect:
            if isinstance(e, str) and _looks_like_selector(e) and not REF_RE.match(e):
                try:
                    hits = query.select(ix, e)
                except OpError:
                    hits = []  # a label that looks like a selector: matched as spoken
                if len(hits) == 1:
                    tbc = _tb_capture(ix, lc)
                    nid = hits[0].id
                    if tbc is not None:
                        info = tbc.explain(nid)
                        merged = str(info.get("why_not") or "")
                        if merged.startswith("merged_into:"):
                            nid = merged.split(":", 1)[1]
                    out.append(nid)
                    continue
            out.append(e)
        return out


def _tb_capture(ix: Index, lc: LoadedCapture) -> Any:
    from .capture.tb import TbCapture
    return TbCapture.of(ix, lc)


def _stop_key(ix: Index, lc: LoadedCapture, node: UNode, stop: bool = True) -> str | None:
    """The accessibility node key TalkBack focuses for ``node``: its own when it is a
    stop, else the stop it is merged into, else its own (A11yAct can focus any node).
    ``stop=False``: the node's own key."""
    tbc = _tb_capture(ix, lc)
    if tbc is None:
        return None
    x = tbc.node(node.id)
    if x is None:
        return None
    why_not = str(tbc.explain(node.id).get("why_not") or "") if stop else ""
    if why_not.startswith("merged_into:"):
        other = tbc.node(why_not.split(":", 1)[1])
        key = getattr(other, "key", None)
        if key:
            return str(key)
    key = getattr(x, "key", None) or (getattr(x, "raw", None) or {}).get("node_key")
    return str(key) if key else None


def talkback(ctx: OpContext, action: Any = None, serial: Any = None, package: Any = None,
             verbose_log: Any = None) -> dict[str, Any]:
    """``talkback``: status (read-only) | on | off | restore, DEVICE-WIDE. ``on``
    snapshots the accessibility settings first; restore writes them back."""
    from .talkback import device as tbdevice

    act = _enum("action", action, TALKBACK_ACTIONS, "status")
    verbose = _bool("verbose_log", verbose_log, False)
    s, _p = _tb_device(ctx, serial, package)
    # "on" keeps in front only an app the caller named (the default session's app may
    # be in the background by now: that is no reason to refuse)
    keep = _explicit(package) if act == "on" else None
    out = dict(_tb_call(lambda: tbdevice.action(s, act, package=keep, verbose_log=verbose)))
    if _explicit(serial) is None:
        out["serial"] = s  # device-wide: say which device it was
    hints = []
    installed = isinstance(out.get("talkback"), dict) and out["talkback"].get("installed")
    if act in ("status", "on") and (installed or act == "on"):
        hints.append(query.call("tb_walk"))
    if out.get("restore_pending"):
        hints.append(query.call("talkback", action="restore"))
    if hints:
        out["next"] = hints[:3]
    return out


def tb_walk(ctx: OpContext, serial: Any = None, package: Any = None, start: Any = None,
            direction: Any = None, max_steps: Any = None, until: Any = None, expect: Any = None,
            step_timeout_ms: Any = None, settle_ms: Any = None, recapture: Any = None,
            utterance: Any = None, injector: Any = None, leave_on: Any = None,
            max_lines: Any = None, max_bytes: Any = None) -> dict[str, Any]:
    """``tb_walk``: walk the real TalkBack through the app (DEVICE-WIDE: turned on,
    then restored), record each stop as a capture ref, diff actual vs predicted vs
    visual, store ``<store>/walks/<id>.json``; at most ``max_bytes`` (5 KB)."""
    opts = {
        "start": _explicit(start) or "current",
        "direction": _enum("direction", direction, TB_DIRECTIONS, "next"),
        "max_steps": _int("max_steps", max_steps, TB_MAX_STEPS, 1, 300),
        "until": _enum("until", until, TB_UNTIL, "wrap"),
        "step_timeout_ms": _int("step_timeout_ms", step_timeout_ms, TB_STEP_TIMEOUT_MS, 100,
                                10000),
        "settle_ms": _int("settle_ms", settle_ms, TB_SETTLE_MS, 10, 2000),
        "recapture": _enum("recapture", recapture, TB_RECAPTURE, "on_unknown"),
        "utterance": _enum("utterance", utterance, TB_UTTERANCE, "auto"),
        "injector": _enum("injector", injector, TB_INJECTORS, "auto"),
        "leave_on": _bool("leave_on", leave_on, False),
    }
    if expect is not None and (not isinstance(expect, list)
                               or not all(isinstance(e, str) for e in expect)):
        raise _bad("expect must be a list of refs, selectors or labels")
    n_lines = _int("max_lines", max_lines, walks.WALK_MAX_LINES, 5, 300)
    budget = _int("max_bytes", max_bytes, TB_WALK_MAX_BYTES, -(1 << 30), TB_WALK_HARD_MAX)
    budget = query.MAX_BYTES_CEILING if budget <= 0 else max(1000, budget)
    lineage, session, note = _tb_session(ctx, serial, package)
    _tb_check_refs(ctx, lineage, opts["start"], expect)
    budget -= _tb_mark_bytes(lineage, note)
    hook = _TbCaptures(ctx, lineage, session, "tb_walk",
                       recapture=opts["recapture"] == "on_unknown")
    if note:
        hook.notes.append(note)
    try:
        out = _tb_walk_record(ctx, hook, session, opts, expect, n_lines, budget)
    finally:
        hook.release()
    return _tb_session_mark(out, lineage, note)


def _tb_walk_record(ctx: OpContext, hook: _TbCaptures, session: Any, opts: dict[str, Any],
                    expect: Any, n_lines: int, budget: int) -> dict[str, Any]:
    from .talkback import walk as tbwalk

    record = _tb_call(lambda: tbwalk.run_walk(
        session, start=opts["start"], direction=opts["direction"], max_steps=opts["max_steps"],
        until=opts["until"], expect=None, step_timeout_ms=opts["step_timeout_ms"],
        settle_ms=opts["settle_ms"], recapture=opts["recapture"], utterance=opts["utterance"],
        injector=opts["injector"], leave_on=opts["leave_on"], save=False, hook=hook, full=True))
    record["id"] = record.get("walk") or tbwalk._walk_id()
    # the engine's own note predates the capture store: say which captures were added
    record["recapture"] = ", ".join(f"step {at}: {lc.id}" for at, lc in hook.taken[1:]) or None
    exp = hook.resolve_expect(list(expect) if expect else None)
    walks.bind_walk(record, hook.binding(), expect=exp)
    if expect:
        record["expect_given"] = list(expect)
    record["notes"] = list(record.get("notes") or []) + hook.notes
    record.pop("saved", None)
    record.pop("dump", None)
    try:
        walks.save(ctx.store, record)
    except OSError as exc:
        record["notes"].append(f"could not store the walk: {exc}")
        record["id"] = None
    return walks.walk_result(record, max_lines=n_lines, max_bytes=budget, listed=ctx.listed)


def tb_scenario(ctx: OpContext, kind: Any = None, serial: Any = None, package: Any = None,
                target: Any = None, action: Any = None, mutate: Any = None,
                wait_ms: Any = None, injector: Any = None, leave_on: Any = None,
                step_timeout_ms: Any = None, settle_ms: Any = None,
                max_bytes: Any = None) -> dict[str, Any]:
    """``tb_scenario``: where the real TalkBack's focus goes (DEVICE-WIDE), recorded
    against a capture before and one after; at most ``max_bytes`` (1 KB)."""
    if kind is None:
        raise _bad("tb_scenario needs kind: focus_after, restore or survive",
                   hint='tb_scenario(kind="survive",target="n47",mutate="tap:n49")')
    k = _enum("kind", kind, TB_KINDS, "focus_after")
    if k == "survive" and not _explicit(mutate):
        raise _bad("survive needs mutate: tap:<selector> | activate | key:<combo> | "
                   "broadcast:<am args> | probe:<action>")
    opts = {
        "target": _explicit(target), "action": _explicit(action) or "activate",
        "mutate": _explicit(mutate),
        "wait_ms": _int("wait_ms", wait_ms, TB_WAIT_MS, 300, 20000),
        "injector": _enum("injector", injector, TB_INJECTORS, "auto"),
        "leave_on": _bool("leave_on", leave_on, False),
        "step_timeout_ms": _int("step_timeout_ms", step_timeout_ms, TB_STEP_TIMEOUT_MS, 100,
                                10000),
        "settle_ms": _int("settle_ms", settle_ms, TB_SETTLE_MS, 10, 2000),
    }
    budget = query.resolve_max_bytes(max_bytes, TB_SCENARIO_MAX_BYTES)
    lineage, session, note = _tb_session(ctx, serial, package)
    _tb_check_refs(ctx, lineage, opts["target"], opts["action"], opts["mutate"])
    budget -= _tb_mark_bytes(lineage, note)
    hook = _TbCaptures(ctx, lineage, session, "tb_scenario")
    if note:
        hook.notes.append(note)
    try:
        out = _tb_scenario_record(ctx, hook, session, k, opts, budget)
    finally:
        hook.release()
    return _tb_session_mark(out, lineage, note)


def _tb_check_refs(ctx: OpContext, lineage: tuple[str, str], *sels: Any) -> None:
    """Fail before TalkBack is touched when a ref (``n12``, or ``tap:n12`` /
    ``long_press:n12`` / ``expect:n12`` in an action sequence) names no node of the app's
    latest capture: the walk's own capture carries refs over from it, so a ref it lacks
    would only fail later, with the device already driven."""
    wanted: list[str] = []
    for x in sels:
        for v in (x if isinstance(x, list) else [x]):
            if not isinstance(v, str):
                continue
            for step in v.split(";") if ";" in v else [v]:
                step = step.strip()
                head, sep, arg = step.partition(":")
                if sep and head.strip().lower().replace("-", "_") in (
                        "tap", "long_press", "longpress", "expect"):
                    step = arg.strip()
                if REF_RE.match(step):
                    wanted.append(step)
    if not wanted:
        return
    try:
        lc = _load(ctx, "latest", lineage)
    except OpError:
        raise OpError("capture_not_found", f"{wanted[0]} is a capture ref, but no capture of "
                                           f"{lineage[1]} is stored",
                      hint="capture() first, or pass the label as spoken.") from None
    ix = lc.index()
    for r in wanted:
        query.resolve_selector(ix, r, tomb=_tomb(ctx, lc))  # ref_not_in_capture & co


def _tb_mark_bytes(lineage: tuple[str, str], note: str | None) -> int:
    """What :func:`_tb_session_mark` adds to a response (its budget makes room)."""
    if note is None:
        return 0
    return len(dumps({"session": f"{lineage[0]}/{lineage[1]}"}).encode("utf-8")) - 1


def _tb_session_mark(out: dict[str, Any], lineage: tuple[str, str], note: str | None
                     ) -> dict[str, Any]:
    """``session: serial/package`` first in a walk or scenario the caller did not name
    the app of (``note`` is not None): it acted device-wide, on that device."""
    if note is None:
        return out
    return {"session": f"{lineage[0]}/{lineage[1]}", **out}


def _tb_scenario_record(ctx: OpContext, hook: _TbCaptures, session: Any, k: str,
                        opts: dict[str, Any], budget: int) -> dict[str, Any]:
    from .talkback import scenarios as tbscenarios
    from .talkback import walk as tbwalk

    out = _tb_call(lambda: tbscenarios.run_scenario(
        session, k, target=opts["target"], action=opts["action"], mutate=opts["mutate"],
        wait_ms=opts["wait_ms"], injector=opts["injector"], leave_on=opts["leave_on"],
        step_timeout_ms=opts["step_timeout_ms"], settle_ms=opts["settle_ms"], save=False,
        hook=hook))
    rec = dict(out, id="t" + tbwalk._walk_id()[1:])
    taken = [at for at, _lc in hook.taken]
    before_at = 1 if 1 in taken else 0
    walks.bind_scenario(rec, hook.binding(), before_at=before_at, after_at=2)
    before = next((lc for at, lc in reversed(hook.taken) if at <= 1), None)
    after = next((lc for at, lc in hook.taken if at == 2), None)
    tgt = (rec.get("target") or {}).get("ref") if isinstance(rec.get("target"), dict) else None
    if k == "survive":
        rec["cause_text"] = walks.survive_cause(before, after, tgt)
    if k != "restore" and not rec.get("speak_after") and not rec.get("announced") \
            and "speech" not in rec and _visual_change(before, after, tgt):
        # the target's pixels changed while TalkBack said nothing (a state only drawn)
        flags = list(rec.get("flags") or [])
        if "changed visually, speech did not" not in flags:
            flags.append("changed visually, speech did not")
        rec["flags"] = flags
    rec["notes"] = list(rec.get("notes") or []) + hook.notes
    rec.pop("saved", None)
    try:
        walks.save(ctx.store, rec)
    except OSError as exc:
        rec["notes"].append(f"could not store the scenario: {exc}")
        rec["id"] = None
    return walks.scenario_result(rec, max_bytes=budget, listed=ctx.listed)


def _visual_change(before: LoadedCapture | None, after: LoadedCapture | None,
                   ref: str | None, threshold: float = 0.02) -> bool | None:
    """Whether node ``ref``'s pixels differ between two captures' screenshots (more than
    ``threshold`` of them changed; None when either capture cannot tell: no screenshot,
    the node gone or moved)."""
    if before is None or after is None or not ref or not REF_RE.match(str(ref)):
        return None
    try:
        crops = []
        for lc in (before, after):
            ix = lc.index()
            node = ix.get(str(ref))
            r = images._rect(node.b) if node is not None else None
            if node is None or r is None or r[2] <= 0 or r[3] <= 0:
                return None
            win = images._Win(lc, images._window_of(ix, node))
            pw, ph, rgba = win.pixels()
            s, ox, oy = win.scale, win.rect[0], win.rect[1]
            x0, y0 = max(0, int((r[0] - ox) * s)), max(0, int((r[1] - oy) * s))
            x1, y1 = min(pw, int((r[0] + r[2] - ox) * s)), min(ph, int((r[1] + r[3] - oy) * s))
            if x1 <= x0 or y1 <= y0:
                return None
            crops.append(((x1 - x0, y1 - y0), images.crop_rgba(pw, rgba, x0, y0, x1, y1)))
        (sa, a), (sb, b) = crops
        if sa != sb:
            return None
        n = len(a) // 4
        diff = sum(1 for i in range(0, len(a), 4) if a[i:i + 3] != b[i:i + 3])
        return n > 0 and diff / n > threshold
    except Exception:  # noqa: BLE001 - a hint only: no screenshot, an old capture
        return None


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
TOOLS: dict[str, Callable[..., dict[str, Any]]] = {
    "capture": capture, "captures": captures, "outline": outline, "find": find, "node": node,
    "image": image, "lint": lint, "diff": diff,
    "talkback": talkback, "tb_walk": tb_walk, "tb_scenario": tb_scenario,
}


def call(ctx: OpContext, tool: str, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Run one tool function; exceptions propagate (see :func:`run`)."""
    fn = TOOLS.get(tool)
    if fn is None:
        raise _bad(f"unknown tool {tool!r}", candidates=list(TOOLS))
    return fn(ctx, **dict(args or {}))


def run(ctx: OpContext, tool: str, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Run one tool function; every failure becomes the error envelope."""
    try:
        return call(ctx, tool, args)
    except Exception as exc:  # noqa: BLE001 - mapped to the agent-facing envelope
        return error_envelope(exc)


__all__ = [
    "AttachProvider",
    "OpContext",
    "SessionProvider",
    "TB_TOOL_NAMES",
    "TOOLS",
    "TOOL_NAMES",
    "call",
    "capture",
    "captures",
    "default_session",
    "device_lineage",
    "diff",
    "error_envelope",
    "find",
    "image",
    "is_error",
    "lint",
    "node",
    "outline",
    "publish_capture",
    "query_lineage",
    "remember_session",
    "run",
    "staleness",
    "talkback",
    "tb_scenario",
    "tb_walk",
]
