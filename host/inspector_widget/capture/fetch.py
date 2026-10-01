"""Facet registry and fetch (spec "Capture and Walk", sections 3.1-3.2 and 10).

:func:`fetch` drives a live session through one capture and returns a
:class:`~inspector_widget.capture.model.RawCapture` holding the verbatim protobuf
bytes of every facet, plus a meta with per-facet status, timing and consistency.
It performs device I/O only through the narrow :class:`CaptureSession` protocol
(the public ``inspector_widget.Session`` surface), so it can be tested against
an in-process fake.

Request order (spec 3.2)::

    [DumpCompose(slots, enable_inspection)]   slots="enable" only, always first
    GetWindows
    DumpTree(props, resolution_stack, screenshot of the first root, scale)
    Screenshot(root_id=r)                     each other window root
    DumpCompose(semantics only)
    [DumpCompose(slots only)]                 slots="if_available": never enables
    DumpA11y(extras, rendering=a11y_rendering)
    [CaptureSkp(root_id=r)]                   skp=True and the session has capture_skp
    DumpTree(no props) + DumpCompose(sem)     the fingerprint re-check

Consistency: the fingerprint of the fetched views and semantics must equal a
fresh one taken right after the last facet. Otherwise the whole fetch is retried
up to 2 times, 150 ms apart, and then kept with ``consistency="unsettled"``.

Facet statuses (``meta.facets``) use the model vocabulary ``ok | off |
unavailable | unsupported | error``. A failing optional facet becomes ``error``
without failing the capture; a failing required facet (windows, views) raises
``OpError("agent_error")``. Transport failures (``OSError``: a lost connection, a
deadline) always propagate, since every later request would fail too.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .model import CaptureMeta, CaptureOptions, OpError, RawCapture

#: Newest SKP version any published skiaparser decodes (server 3: SKP 82-109,
#: see skiaparser.py). A newer SKP (API 37 writes v110) is ``unsupported``.
SKP_MAX_VERSION = 109
RETRIES = 2
RETRY_DELAY_S = 0.15
SETTLE_CAP_MS = 3000
SETTLE_INTERVAL_MS = 100

FINGERPRINT_VERSION = b"iw-fingerprint-1\n"
#: Semantics attrs in the fingerprint (Focused and pixels are left out on purpose,
#: so cursors, ripples and spinners do not make a capture unstable).
FINGERPRINT_SEM_ATTRS = ("Text", "ContentDescription", "StateDescription", "ToggleableState",
                         "Selected", "Disabled", "EditableText")

SLOTS_NOT_POPULATED = 'not populated (slots="enable" is destructive: resets remember{} state)'
SLOTS_ENABLE_WARNING = ("slots=enable hot-reloaded every composition (resets remember{} state); "
                        "semantics ids re-minted; refs carried by locator/structure")
NO_COMPOSE = "no AndroidComposeView"
COMPOSE_OBFUSCATED = ("obfuscated: Compose classes are renamed in this build (no semantics or "
                      "slot table; the a11y tree still works)")
UNSETTLED_DIAGNOSTIC = ("consistency unsettled: the UI kept changing across {n} attempts, "
                        "so facets may disagree slightly")

_COMPOSABLE = 0  # pb.ComposeNode.Kind
_SEMANTICS = 1


class CaptureSession(Protocol):
    """What fetch needs from a session: the public ``inspector_widget.Session``
    method surface. ``pid``, ``api_level``, ``abi``, ``agent_version`` and
    ``build_id`` are read with getattr (absent before session-lifecycle's E9), and
    ``capture_skp(root_id)`` is used only when present."""

    serial: str
    package: str

    def get_windows(self) -> Any: ...

    def dump_tree(self, root_id: int = 0, include_properties: bool = False,
                  include_resolution_stack: bool = False, include_screenshot: bool = False,
                  screenshot_scale: float = 1.0) -> Any: ...

    def screenshot(self, root_id: int = 0, scale: float = 1.0) -> Any: ...

    def dump_compose(self, root_view_id: int = 0, include_semantics: bool = True,
                     include_slot_table: bool = True, enable_inspection: bool = False) -> Any: ...

    def dump_a11y(self, root_id: int = 0, include_extras: bool = True,
                  include_rendering_info: bool = False) -> Any: ...


# --------------------------------------------------------------------------- #
# Fingerprint
# --------------------------------------------------------------------------- #
def _strings(msg: Any) -> dict[int, str]:
    return {e.id: e.str for e in msg.strings.entries} if msg is not None else {}


def _view_tuples(views: Any) -> Iterator[list]:
    st = _strings(views)
    stack = list(reversed(views.roots))
    while stack:
        n = stack.pop()
        r = n.bounds.layout
        yield ["V", n.id, st.get(n.class_name, ""), r.x, r.y, r.w, r.h, st.get(n.text_value, "")]
        stack.extend(reversed(n.children))


def _sem_tuples(compose: Any) -> Iterator[list]:
    st = _strings(compose)
    for w in compose.windows:
        stack = [w.root] if w.HasField("root") else []
        while stack:
            n = stack.pop()
            if n.kind == _SEMANTICS:
                attrs = {st.get(a.key, ""): st.get(a.value, "") for a in n.attrs}
                r = n.bounds.layout
                yield ["S", w.view_id, n.id, r.x, r.y, r.w, r.h,
                       *[attrs.get(k) for k in FINGERPRINT_SEM_ATTRS]]
            stack.extend(reversed(n.children))


def fingerprint_of(views: Any, compose: Any | None = None) -> str:
    """blake2b-128 (hex) over the window root ids, View ``(id, class, bounds,
    text)`` and semantics ``(acv, id, bounds, Text, ContentDescription,
    StateDescription, ToggleableState, Selected, Disabled, EditableText)``.

    Strings are resolved through each message's own string table, so a dump
    with properties and one without give the same fingerprint.
    """
    h = hashlib.blake2b(digest_size=16)
    h.update(FINGERPRINT_VERSION)

    def put(t: list) -> None:
        h.update(json.dumps(t, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        h.update(b"\n")

    put(["W", [r.id for r in views.roots]])
    for t in _view_tuples(views):
        put(t)
    if compose is not None:
        for t in _sem_tuples(compose):
            put(t)
    return h.hexdigest()


def fingerprint_raw(raw: RawCapture) -> str | None:
    """The fingerprint of a stored capture's views and semantics (None without views)."""
    if not raw.views:
        return None
    from ..proto import view_inspection_pb2 as pb

    views = pb.DumpTreeResponse.FromString(raw.views)
    compose = pb.DumpComposeResponse.FromString(raw.compose_sem) if raw.compose_sem else None
    return fingerprint_of(views, compose)


def _dump_sem(session: CaptureSession) -> Any:
    return session.dump_compose(root_view_id=0, include_semantics=True,
                                include_slot_table=False, enable_inspection=False)


def fingerprint_now(session: CaptureSession) -> str:
    """The live UI's fingerprint: DumpTree (no properties, no screenshot) plus
    DumpCompose (semantics only), about 30-150 ms. A failing compose dump is left
    out rather than failing the fingerprint."""
    views = session.dump_tree(root_id=0, include_properties=False,
                              include_resolution_stack=False, include_screenshot=False,
                              screenshot_scale=1.0)
    try:
        compose = _dump_sem(session)
    except OSError:
        raise
    except Exception:  # noqa: BLE001 - agent error on compose: views alone
        compose = None
    return fingerprint_of(views, compose)


def settle(session: CaptureSession, settle_ms: int, *,
           clock: Callable[[], float] = time.monotonic,
           sleep: Callable[[float], None] = time.sleep,
           interval_ms: int = SETTLE_INTERVAL_MS) -> bool:
    """Poll the fingerprint every ``interval_ms`` until two consecutive values are
    equal, for at most ``settle_ms`` (capped at 3,000 ms). Returns True when the
    UI settled (and for ``settle_ms <= 0``), False when the budget ran out."""
    if settle_ms <= 0:
        return True
    budget = min(int(settle_ms), SETTLE_CAP_MS) / 1000.0
    start = clock()
    prev = fingerprint_now(session)
    while True:
        left = budget - (clock() - start)
        if left < 0.001:  # not worth another poll (and no float stall on a fake clock)
            return False
        sleep(min(interval_ms / 1000.0, left))
        cur = fingerprint_now(session)
        if cur == prev:
            return True
        prev = cur


def unchanged_since(session: CaptureSession, meta: CaptureMeta, *,
                    now: Callable[[], float] = time.time) -> dict | None:
    """``if_changed_since``: ``{"capture", "unchanged": True, "age_s"}`` when the
    live UI of the same app and pid still has ``meta``'s fingerprint, else None.
    Costs one fingerprint (no facet fetch, nothing written)."""
    if not meta.fingerprint:
        return None
    if (getattr(session, "serial", None), getattr(session, "package", None)) != meta.lineage:
        return None
    pid = getattr(session, "pid", None)
    if meta.pid is not None and pid is not None and int(pid) != int(meta.pid):
        return None
    if fingerprint_now(session) != meta.fingerprint:
        return None
    return {"capture": meta.id, "unchanged": True,
            "age_s": max(0, round(now() - float(meta.created_at or 0.0)))}


# --------------------------------------------------------------------------- #
# The facet registry
# --------------------------------------------------------------------------- #
@dataclass
class _Attempt:
    """State of one fetch attempt, handed to every facet's ``run``."""

    session: CaptureSession
    opts: CaptureOptions
    raw: RawCapture
    first: bool
    skp_max_version: int
    window_ids: list[int] = field(default_factory=list)
    view_roots: list[int] = field(default_factory=list)
    views_msg: Any = None
    compose_msg: Any = None
    enabled_inspection: bool = False

    @property
    def meta(self) -> CaptureMeta:
        return self.raw.meta

    def set(self, name: str, status: str, reason: str | None = None, nbytes: int = 0) -> None:
        prev = self.meta.facets.get(name) or {}
        self.meta.set_facet(name, status, reason=reason, ms=prev.get("ms", 0), nbytes=nbytes)

    def roots(self) -> list[int]:
        """Window roots: the DumpTree order first (its first root has the DumpTree
        screenshot), then any GetWindows root the dump did not return."""
        return list(dict.fromkeys([*self.view_roots, *self.window_ids]))


@dataclass(frozen=True)
class Facet:
    """One registry entry: how a facet is fetched, when, and where it is stored.

    ``position(opts)`` orders the requests (None: not requested with these
    options, recorded as ``off``). ``required`` facets abort the capture when
    they fail; the others record ``error`` and the capture goes on.
    """

    name: str
    request: str
    policy: str
    stored: str
    run: Callable[[_Attempt], None]
    position: Callable[[CaptureOptions], int | None]
    required: bool = False
    off_reason: str | None = None


def _short(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text if len(text) <= 200 else text[:199] + "…"


def _run_windows(att: _Attempt) -> None:
    resp = att.session.get_windows()
    att.window_ids = [int(r) for r in resp.root_ids]
    att.raw.windows = resp.SerializeToString()
    att.set("windows", "ok", nbytes=len(att.raw.windows))


def _dump_views(att: _Attempt, props: bool) -> Any:
    o = att.opts
    return att.session.dump_tree(root_id=0, include_properties=props,
                                 include_resolution_stack=o.resolution_stack and props,
                                 include_screenshot=o.screenshot,
                                 screenshot_scale=o.screenshot_scale)


def _run_views(att: _Attempt) -> None:
    o = att.opts
    props_error = None
    try:
        resp = _dump_views(att, o.props)
    except OSError:
        raise
    except Exception as exc:
        if not o.props:
            raise
        # A property getter that throws on-device fails the whole dump; keep the
        # tree and screenshot without properties rather than losing the capture.
        props_error = _short(exc)
        resp = _dump_views(att, False)
    att.view_roots = [int(r.id) for r in resp.roots]
    if o.screenshot and resp.HasField("screenshot") and resp.screenshot.data and att.view_roots:
        att.raw.shots[att.view_roots[0]] = resp.screenshot.SerializeToString()
    resp.ClearField("screenshot")
    att.views_msg = resp
    att.raw.views = resp.SerializeToString()
    att.set("views", "ok", nbytes=len(att.raw.views))
    if not o.props:
        att.set("props", "off", reason="props=false")
    elif props_error:
        att.set("props", "error", reason=props_error)
    else:
        n = sum(g.ByteSize() for g in resp.properties)
        att.set("props", "ok", nbytes=n)


def _run_shots(att: _Attempt) -> None:
    o = att.opts
    errors: list[str] = []
    for root in att.roots():
        if root in att.raw.shots:
            continue
        try:
            resp = att.session.screenshot(root_id=root, scale=o.screenshot_scale)
        except OSError:
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{root}: {_short(exc)}")
            continue
        if resp.HasField("screenshot") and resp.screenshot.data:
            att.raw.shots[root] = resp.screenshot.SerializeToString()
        else:
            errors.append(f"{root}: empty screenshot")
    nbytes = sum(len(b) for b in att.raw.shots.values())
    if not att.raw.shots:
        att.set("shots", "error", reason="; ".join(errors) or "no window roots")
    else:
        att.set("shots", "ok", reason=("missing " + "; ".join(errors)) if errors else None,
                nbytes=nbytes)


def _tokens(diagnostics: str, prefix: str) -> list[str]:
    return [t.strip() for t in (diagnostics or "").split(";") if t.strip().startswith(prefix)]


def _has_semantics(resp: Any) -> bool:
    return any(c.kind == _SEMANTICS for w in resp.windows if w.HasField("root")
               for c in w.root.children)


def _run_compose(att: _Attempt) -> None:
    resp = _dump_sem(att.session)
    att.compose_msg = resp
    att.raw.compose_sem = resp.SerializeToString()
    nbytes = len(att.raw.compose_sem)
    failed = _tokens(resp.diagnostics, "semantics_failed")
    if not len(resp.windows):
        att.set("compose", "ok", reason=NO_COMPOSE, nbytes=nbytes)
    elif _tokens(resp.diagnostics, "compose_obfuscated"):
        # The agent still sends a window per ComposeView: not "ok, 0 nodes".
        att.set("compose", "unavailable", reason=COMPOSE_OBFUSCATED, nbytes=nbytes)
    elif failed and not _has_semantics(resp):
        att.set("compose", "unavailable", reason=failed[0], nbytes=nbytes)
    else:
        att.set("compose", "ok", nbytes=nbytes)


def _slots_populated(resp: Any) -> bool:
    return any(c.kind == _COMPOSABLE for w in resp.windows if w.HasField("root")
               for c in w.root.children)


def _run_slots(att: _Attempt) -> None:
    """``enable``: DumpCompose(enable_inspection) on the first attempt (it is
    issued first, so the recomposition happens before every other facet).
    ``if_available`` (and ``enable`` retries): a read that never enables."""
    enable = att.opts.slots == "enable" and att.first
    # Marked before sending: if the reply is an error, the hot reload may still
    # have happened, and assuming it did only costs id-based ref carry-over once.
    att.enabled_inspection = enable
    resp = att.session.dump_compose(root_view_id=0, include_semantics=False,
                                    include_slot_table=True, enable_inspection=enable)
    if _slots_populated(resp):
        att.raw.slots = resp.SerializeToString()
        att.set("slots", "ok", nbytes=len(att.raw.slots))
    elif not len(resp.windows):
        att.set("slots", "unavailable", reason=NO_COMPOSE)
    elif _tokens(resp.diagnostics, "compose_obfuscated"):
        att.set("slots", "unavailable", reason=COMPOSE_OBFUSCATED)  # enabling cannot help
    elif _tokens(resp.diagnostics, "slot_failed"):
        att.set("slots", "error", reason=_tokens(resp.diagnostics, "slot_failed")[0])
    elif enable:
        att.set("slots", "unavailable", reason="slot table still empty after enable_inspection")
    else:
        att.set("slots", "unavailable", reason=SLOTS_NOT_POPULATED)


def _run_a11y(att: _Attempt) -> None:
    resp = att.session.dump_a11y(root_id=0, include_extras=True,
                                 include_rendering_info=att.opts.a11y_rendering)
    att.raw.a11y = resp.SerializeToString()
    att.set("a11y", "ok", reason="with rendering info" if att.opts.a11y_rendering else None,
            nbytes=len(att.raw.a11y))


def skp_version(skp: bytes) -> int:
    """The version in an SKP header (``skiapict`` + little-endian uint32), 0 if none."""
    if len(skp) < 12 or skp[:8] != b"skiapict":
        return 0
    return int.from_bytes(skp[8:12], "little")


def _run_skp(att: _Attempt) -> None:
    capture_skp = getattr(att.session, "capture_skp", None)
    if capture_skp is None:
        att.set("skp", "unsupported", reason="the session has no capture_skp")
        return
    notes: list[str] = []
    unsupported = False
    for root in att.roots():
        try:
            resp = capture_skp(root_id=root)
        except OSError:
            raise
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{root}: {_short(exc)}")
            continue
        if not resp.supported:
            unsupported = True
            notes.append(f"{root}: {resp.error or 'SKP capture not supported'}")
            continue
        version = int(resp.version) or skp_version(resp.skp)
        if version > att.skp_max_version:
            unsupported = True
            notes.append(f"{root}: SKP v{version} > skiaparser's v{att.skp_max_version}")
            continue
        if not resp.skp:
            notes.append(f"{root}: empty SKP")
            continue
        att.raw.skp[root] = bytes(resp.skp)
    nbytes = sum(len(b) for b in att.raw.skp.values())
    reason = "; ".join(notes) or None
    if att.raw.skp:
        att.set("skp", "ok", reason=reason, nbytes=nbytes)
    elif unsupported:
        att.set("skp", "unsupported", reason=reason)
    else:
        att.set("skp", "error", reason=reason or "no window roots")


def _always(pos: int) -> Callable[[CaptureOptions], int | None]:
    return lambda _opts: pos


def _slots_position(o: CaptureOptions) -> int | None:
    return None if o.slots == "off" else (0 if o.slots == "enable" else 50)


#: Every facet, keyed by its ``meta.facets`` name, in default request order.
#: ``props`` has no request of its own (it rides on DumpTree) and is recorded by
#: ``views``. New facets (DumpRender, WindowInfo, text layout) are one entry each.
FACETS: dict[str, Facet] = {f.name: f for f in (
    Facet("windows", "GetWindows", "always", "raw/windows.pb", _run_windows,
          _always(10), required=True),
    Facet("views", "DumpTree(root 0, props, resolution_stack, screenshot, scale)",
          "always (props on by default)", "raw/views.pb + shot/w_<firstRoot>.pb", _run_views,
          _always(20), required=True),
    Facet("shots", "Screenshot(root_id=r) for each other root", "when screenshot",
          "shot/w_<root>.pb", _run_shots,
          lambda o: 30 if o.screenshot else None, off_reason="screenshot=false"),
    Facet("compose", "DumpCompose(semantics only)", "always", "raw/compose_sem.pb",
          _run_compose, _always(40)),
    Facet("slots", "DumpCompose(slot table only; enable_inspection only for slots=enable)",
          "if_available (reads, never enables) | enable (destructive, first) | off",
          "raw/slots.pb", _run_slots, _slots_position, off_reason="slots=off"),
    Facet("a11y", "DumpA11y(extras, rendering=a11y_rendering)", "always", "raw/a11y.pb",
          _run_a11y, _always(60)),
    Facet("skp", "CaptureSkp(root_id=r) per window", "opt-in (skp=true)", "raw/skp_<root>.bin",
          _run_skp, lambda o: 70 if o.skp else None, off_reason="skp=false"),
)}


def plan(opts: CaptureOptions) -> list[str]:
    """Facet names in request order for ``opts`` (the fingerprint re-check follows)."""
    ranked = [(f.position(opts), i, name) for i, (name, f) in enumerate(FACETS.items())]
    return [name for pos, _i, name in sorted((r for r in ranked if r[0] is not None),
                                             key=lambda r: (r[0], r[1]))]


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #
def _screen_of(views: Any) -> list[int] | None:
    if views is None or not len(views.roots):
        return None
    w = max(r.bounds.layout.x + r.bounds.layout.w for r in views.roots)
    h = max(r.bounds.layout.y + r.bounds.layout.h for r in views.roots)
    return [int(w), int(h)] if w > 0 and h > 0 else None


def _new_meta(session: CaptureSession, opts: CaptureOptions, compose_generation: int,
              created_at: float, device: Mapping[str, Any] | None) -> CaptureMeta:
    def attr(name: str) -> Any:
        v = getattr(session, name, None)
        return None if callable(v) else v

    return CaptureMeta(
        id="", lineage=(session.serial, session.package), pid=attr("pid"),
        api=attr("api_level"), abi=attr("abi"), agent_version=attr("agent_version"),
        agent_build=attr("build_id"), device=dict(device or {}), created_at=created_at,
        options=opts, compose_generation=int(compose_generation))


def _attempt(session: CaptureSession, opts: CaptureOptions, meta: CaptureMeta, *,
             first: bool, clock: Callable[[], float], skp_max_version: int) -> _Attempt:
    meta.facets = {}
    att = _Attempt(session, opts, RawCapture(meta=meta), first, skp_max_version)
    order = plan(opts)
    for name in order:
        f = FACETS[name]
        t0 = clock()
        try:
            f.run(att)
        except (OSError, OpError):
            raise
        except Exception as exc:  # the agent answered ERROR, or a bad reply
            if f.required:
                raise OpError("agent_error", f"{name}: {_short(exc)}",
                              hint="See `adb logcat -s ViewSpector` for the agent's side.") from exc
            meta.set_facet(name, "error", reason=_short(exc))
        entry = meta.facets.get(name)
        if entry is not None:
            entry["ms"] = round((clock() - t0) * 1000)
    for name, f in FACETS.items():
        if name not in order:
            meta.set_facet(name, "off", reason=f.off_reason)
    return att


def fetch(session: CaptureSession, opts: CaptureOptions | None = None, *,
          compose_generation: int = 0, clock: Callable[[], float] = time.monotonic,
          sleep: Callable[[float], None] = time.sleep,
          wall_clock: Callable[[], float] = time.time,
          device: Mapping[str, Any] | None = None, retries: int = RETRIES,
          retry_delay_s: float = RETRY_DELAY_S,
          skp_max_version: int = SKP_MAX_VERSION,
          foreign: Callable[[], Mapping[str, Any] | None] | None = None) -> RawCapture:
    """Fetch every facet ``opts`` asks for and return the RawCapture (meta.id is
    empty until the store publishes it).

    ``compose_generation`` is the lineage's current generation (0 for a new pid);
    the meta records it plus one when this fetch sent ``enable_inspection``,
    because the hot reload re-mints semantics ids. ``device`` ({dpi, font_scale,
    ...}, e.g. from adb) is merged into ``meta.device``; ``screen`` and
    ``orientation`` are derived from the window roots when missing. ``clock``
    times the facets, ``wall_clock`` stamps ``created_at``, ``sleep`` waits
    between retries and settle polls. ``foreign()``: what window of another app covers the
    app (default: the window manager's list through adb, :mod:`talkback.windows`); a
    capture under one says so (a diagnostic, ``meta.device["foreign_window"]``, and a
    token in the stored accessibility tree's diagnostics, so the TalkBack model reads no
    stop of the app: TalkBack reads that window).
    """
    opts = (opts or CaptureOptions()).validate()
    t_start = clock()
    meta = _new_meta(session, opts, compose_generation, wall_clock(), device)
    if opts.settle_ms > 0 and not settle(session, opts.settle_ms, clock=clock, sleep=sleep):
        meta.diagnostics.append(
            f"settle: the UI was still changing after {min(opts.settle_ms, SETTLE_CAP_MS)} ms")

    attempts = max(0, int(retries)) + 1
    att: _Attempt | None = None
    enabled = False
    fp = recheck = None
    for i in range(attempts):
        if i:
            sleep(retry_delay_s)
        att = _attempt(session, opts, meta, first=(i == 0), clock=clock,
                       skp_max_version=skp_max_version)
        enabled = enabled or att.enabled_inspection
        fp = fingerprint_of(att.views_msg, att.compose_msg)
        t0 = clock()
        try:
            recheck = fingerprint_now(session)
        except OSError:
            raise
        except Exception as exc:  # noqa: BLE001
            meta.set_facet("fingerprint", "error", reason=_short(exc),
                           ms=round((clock() - t0) * 1000))
            recheck = None
            break
        meta.set_facet("fingerprint", "ok", ms=round((clock() - t0) * 1000))
        if recheck == fp:
            break

    assert att is not None
    raw = att.raw
    meta.fingerprint = fp
    tries = i + 1
    if recheck == fp:
        meta.consistency = "settled"
        if tries > 1:
            meta.diagnostics.append(
                f"the UI changed during capture; settled after {tries} attempts")
    else:
        meta.consistency = "unsettled"
        meta.diagnostics.append("consistency unsettled: the fingerprint re-check failed"
                                if recheck is None else UNSETTLED_DIAGNOSTIC.format(n=tries))
    meta.compose_generation = int(compose_generation) + (1 if enabled else 0)
    screen = meta.device.get("screen") or _screen_of(att.views_msg)
    if screen:
        meta.device["screen"] = list(screen)
        meta.device.setdefault("orientation",
                               "landscape" if screen[0] > screen[1] else "portrait")
    cover = _foreign(session) if foreign is None else foreign()
    if cover:
        _mark_foreign(raw, dict(cover), session.package)
    meta.took_ms = round((clock() - t_start) * 1000)
    return raw


def _foreign(session: CaptureSession) -> Mapping[str, Any] | None:
    """The window of another app over the session's app now (talkback/windows.py), or None
    (none, or the window list cannot be read: an in-process fake session)."""
    serial, package = getattr(session, "serial", None), getattr(session, "package", None)
    if not isinstance(serial, str) or not isinstance(package, str) or not serial:
        return None
    try:
        from ..talkback import windows

        return windows.foreign_cover(serial, package)
    except Exception:  # noqa: BLE001 - a capture never fails on this check
        return None


def _mark_foreign(raw: RawCapture, cover: dict[str, Any], package: str) -> None:
    """Record ``cover`` in the capture: a diagnostic, ``meta.device["foreign_window"]`` and
    a token in the stored accessibility tree's diagnostics (talkback/windows.py ``token``)."""
    from ..talkback import windows

    raw.meta.device["foreign_window"] = cover
    # what covers it and how it goes, first: a capture shows 120 characters of a diagnostic
    raw.meta.diagnostics.append(f"covered by another app's window: {windows.name(cover)}; "
                                f"{windows.dismiss(cover)}; TalkBack reads it, not this app")
    if raw.a11y:
        from ..proto import view_inspection_pb2 as pb

        resp = pb.DumpA11yResponse.FromString(bytes(raw.a11y))
        tok = windows.token(cover)
        resp.diagnostics = f"{resp.diagnostics}; {tok}" if resp.diagnostics else tok
        raw.a11y = resp.SerializeToString()


__all__ = [
    "FACETS",
    "COMPOSE_OBFUSCATED",
    "FINGERPRINT_SEM_ATTRS",
    "NO_COMPOSE",
    "RETRIES",
    "RETRY_DELAY_S",
    "SETTLE_CAP_MS",
    "SETTLE_INTERVAL_MS",
    "SKP_MAX_VERSION",
    "SLOTS_ENABLE_WARNING",
    "SLOTS_NOT_POPULATED",
    "UNSETTLED_DIAGNOSTIC",
    "CaptureSession",
    "Facet",
    "fetch",
    "fingerprint_now",
    "fingerprint_of",
    "fingerprint_raw",
    "plan",
    "settle",
    "skp_version",
    "unchanged_since",
]
