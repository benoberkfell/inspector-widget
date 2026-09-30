"""The capture store (spec "Capture and Walk", section 4): an on-disk, immutable
record of captures that the CLI and the MCP server share.

Layout under the root (``model.default_store_root()`` unless given)::

    store.json                          {schema, next_ref}: the global ref counter
    store.lock                          flock: refs, publish, labels, pins, lineage files
    gc.lock                             flock, taken non-blocking by gc()
    session.json                        default session {serial, package, at}
    lineages/<serial>__<package>-<h>.json {serial, package, latest, history, labels, tomb}
    captures/<id>/                      meta.json refmap.json raw/ shot/ index.jsonl.gz
                                        derived/ img/ out/ .used .complete [.pinned .stripped]
    .staging/<id>.<pid>/                captures being written
    .trash/                             deletions in progress
    spill/                              Phase-0 spill files (1 h TTL)

Every directory is 0700 and every file 0600. The rules this module implements:

* **Ids** are ``c`` + 5 Crockford base32 characters from ``os.urandom``. A capture
  is written into an exclusive ``.staging/<id>.<pid>`` directory, fsynced, gets
  ``.complete`` last and is published with one ``os.rename`` into ``captures/``.
  Readers ignore directories without ``.complete``, so a partial capture is never
  visible. If the id turned out to be taken, publish picks a new one and retries.
* **Refs** come from ``store.json`` ``next_ref`` via :meth:`CaptureStore.next_refs`
  under :meth:`CaptureStore.refs_lock`. Only ``gc(all=True)`` resets the counter.
  If ``store.json`` is lost, the counter is recovered above every ref still on disk.
* **Lineages** (serial + package) hold ``latest``, ``history`` (newest first, at
  most 50), per-lineage ``labels`` and ref ``tomb``-stones. They are never
  TTL-collected.
* **Labels** are unique per lineage; a capture has at most one. The lineage file is
  the source of truth, and ``.pinned`` markers are the source of truth for pins,
  so ``meta.json`` is written once and never rewritten. :meth:`CaptureStore.load`
  and :meth:`CaptureStore.list` overlay the current label and pin onto the meta.
* **Resolution**: one function, :meth:`CaptureStore.resolve`, for both surfaces.
* **Retention**: TTL since last use (``.used`` mtime, touched at most once a minute),
  then the per-lineage cap, the total count cap and the byte cap (heavy files are
  stripped before whole captures go). Unlabeled captures go first, then the least
  recently used. Pinned captures (at most 20) are exempt from TTL and caps but
  count towards them.
* **Memory-only mode** (``persist=False`` or ``INSPECTOR_WIDGET_CAPTURE_PERSIST=0``):
  the root is a private temp directory removed by :meth:`CaptureStore.close` and
  at exit; nothing is written under the configured root.
* **Memory**: a per-store LRU keeps up to 8 hydrated indexes (about 256 MB).

The lock is re-entrant per thread, so an ops layer can hold :meth:`refs_lock`
across ref matching and :meth:`publish`. GC triggered by a publish runs when the
outermost ``refs_lock`` is released, never while it is held.
"""

from __future__ import annotations

import atexit
import contextlib
import dataclasses
import errno
import gzip
import json
import os
import re
import shutil
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from .model import (
    COMPLETE_MARKER,
    CROCKFORD,
    INDEX_FILE,
    LABEL_RE,
    META_FILE,
    RAW_FILES,
    REFMAP_FILE,
    USED_MARKER,
    CaptureMeta,
    Index,
    LineageState,
    OpError,
    RawCapture,
    default_store_root,
    index_from_jsonl,
    index_to_jsonl,
    is_capture_id,
    is_ref,
    is_valid_label,
    lineage_file_name,
    ref_num,
    ref_str,
    remap_ids,
    shot_file,
    skp_file,
)

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt

STORE_SCHEMA = 1

DEFAULT_TTL_S = 24 * 3600
MIN_TTL_S = 60
DEFAULT_MAX_CAPTURES = 200
DEFAULT_MAX_MB = 1024
LINEAGE_CAP = 50
HISTORY_CAP = 50
MAX_PINNED = 20
TOMB_CAP = 5000
STAGING_TTL_S = 3600
SPILL_TTL_S = 3600
USED_TOUCH_S = 60
MEM_INDEXES = 8
MEM_BYTES = 256 << 20
NODE_BYTES_EST = 1500  # hydrated UNode, spec 3.10

ENV_TTL = "INSPECTOR_WIDGET_CAPTURE_TTL"
ENV_MAX = "INSPECTOR_WIDGET_CAPTURE_MAX"
ENV_MAX_MB = "INSPECTOR_WIDGET_CAPTURE_MAX_MB"
ENV_PERSIST = "INSPECTOR_WIDGET_CAPTURE_PERSIST"

#: Words the ``capture`` argument reserves; they can never be labels.
RESERVED_LABELS = frozenset({"latest", "prev"})
#: Per-capture subdirectories the byte cap strips first (plus raw/skp_*.bin).
HEAVY_DIRS = ("img", "out", "derived")
DERIVED_DIRS = ("derived", "img", "out")

PINNED_MARKER = ".pinned"
STRIPPED_MARKER = ".stripped"
CAPTURE_LOCK = ".lock"

_LATEST_RE = re.compile(r"^latest(?:~(\d+))?$")
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}

SPEC_HINT = "Pass a capture id (c7h2kq), a label, @label, latest, prev or latest~N."
LABEL_HINT = ("Labels match ^[a-z][a-z0-9_-]{0,31}$, must not look like a capture id "
              "(c + 5 letters/digits) and cannot be 'latest' or 'prev'.")


# --------------------------------------------------------------------------- #
# Configuration from the environment
# --------------------------------------------------------------------------- #
def parse_duration(value: Any) -> float | None:
    """``"6h"`` -> 21600.0; units s/m/h/d, a bare number is seconds; None if invalid."""
    if isinstance(value, (int, float)):
        return float(value)
    m = _DURATION_RE.match(str(value or ""))
    if not m:
        return None
    return float(m.group(1)) * _UNITS[m.group(2).lower()]


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        v = int(str(env.get(name, "")).strip())
    except ValueError:
        return default
    return v if v > 0 else default


def env_ttl_s(env: Mapping[str, str] | None = None) -> float:
    """``$INSPECTOR_WIDGET_CAPTURE_TTL`` (e.g. ``6h``), default 24 h, at least 60 s."""
    env = os.environ if env is None else env
    raw = env.get(ENV_TTL)
    ttl = parse_duration(raw) if raw else None
    if ttl is None:
        return float(DEFAULT_TTL_S)
    return max(float(MIN_TTL_S), ttl)


def env_max_captures(env: Mapping[str, str] | None = None) -> int:
    return _env_int(os.environ if env is None else env, ENV_MAX, DEFAULT_MAX_CAPTURES)


def env_max_bytes(env: Mapping[str, str] | None = None) -> int:
    return _env_int(os.environ if env is None else env, ENV_MAX_MB, DEFAULT_MAX_MB) << 20


def env_persist(env: Mapping[str, str] | None = None) -> bool:
    """False when ``$INSPECTOR_WIDGET_CAPTURE_PERSIST`` is 0/false/no/off."""
    env = os.environ if env is None else env
    return str(env.get(ENV_PERSIST, "1")).strip().lower() not in ("0", "false", "no", "off")


def new_capture_id() -> str:
    """``c`` + 5 Crockford base32 characters from ``os.urandom`` (uniform: 256 % 32 == 0)."""
    return "c" + "".join(CROCKFORD[b % 32] for b in os.urandom(5))


# --------------------------------------------------------------------------- #
# File helpers
# --------------------------------------------------------------------------- #
def _mkdir(path: str) -> bool:
    """Create one directory with mode 0700; False if it already existed."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        return False
    os.chmod(path, 0o700)  # mkdir's mode is filtered by the umask
    return True


def _ensure_dirs(base: str, rel: str) -> str:
    """Create every missing directory of ``rel`` under ``base`` with mode 0700."""
    path = base
    for part in rel.split("/"):
        if not part:
            continue
        path = os.path.join(path, part)
        if not os.path.isdir(path):
            _mkdir(path)
    return path


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_new(path: str, data: bytes, durable: bool) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]
        if durable:
            os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: str, data: bytes, durable: bool = True) -> None:
    """Write ``path`` via a temp file in the same directory and ``os.replace``."""
    tmp = f"{path}.tmp-{os.getpid()}-{os.urandom(3).hex()}"
    try:
        _write_new(tmp, data, durable)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _read_json(path: str) -> Any:
    try:
        return json.loads(_read_bytes(path).decode("utf-8"))
    except (OSError, ValueError):
        return None


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _touch(path: str, t: float) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    os.utime(path, (t, t))


def _mtime(path: str) -> float | None:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _tree_size(path: str) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(dirpath, name)).st_size
    return total


def _rmtree(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Inter-process locks
# --------------------------------------------------------------------------- #
def _flock(fd: int, blocking: bool) -> bool:
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            return False
        return True
    while True:  # pragma: no cover - Windows
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            if not blocking:
                return False
            time.sleep(0.02)


def _funlock(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:  # pragma: no cover - Windows
        with contextlib.suppress(OSError):
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


class FileLock:
    """An exclusive lock on a lock file, shared by every process (flock) and
    re-entrant for the thread that holds it. Threads of one process queue on an
    RLock first, so one open file description per process holds the flock."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._mutex = threading.RLock()
        self._depth = 0
        self._fd: int | None = None
        self._owner: int | None = None

    def acquire(self, blocking: bool = True) -> bool:
        if not self._mutex.acquire(blocking=blocking):
            return False
        if self._depth == 0:
            try:
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            except BaseException:
                self._mutex.release()
                raise
            try:
                ok = _flock(fd, blocking)
            except BaseException:
                os.close(fd)
                self._mutex.release()
                raise
            if not ok:
                os.close(fd)
                self._mutex.release()
                return False
            self._fd = fd
            self._owner = threading.get_ident()
        self._depth += 1
        return True

    def release(self) -> None:
        if self._depth <= 0 or self._owner != threading.get_ident():
            raise RuntimeError("release of an unheld FileLock")
        self._depth -= 1
        if self._depth == 0:
            fd, self._fd, self._owner = self._fd, None, None
            try:
                _funlock(fd)
            finally:
                os.close(fd)
        self._mutex.release()

    @property
    def held(self) -> bool:
        """True when the calling thread holds the lock."""
        return self._depth > 0 and self._owner == threading.get_ident()

    @property
    def depth(self) -> int:
        return self._depth if self.held else 0

    def __enter__(self):  # returns self
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


_LOCKS: dict[str, FileLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: str) -> FileLock:
    """One FileLock per lock file per process, so two stores on one root in the
    same process queue on it instead of deadlocking on their own flocks."""
    key = os.path.realpath(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = FileLock(key)
        return lock


def _reset_locks_after_fork() -> None:  # pragma: no cover - fork start method only
    global _LOCKS_GUARD
    _LOCKS.clear()
    _LOCKS_GUARD = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_locks_after_fork)


# --------------------------------------------------------------------------- #
# A loaded capture
# --------------------------------------------------------------------------- #
def _not_found(cid: str, ttl_s: float, detail: str = "") -> OpError:
    hours = ttl_s / 3600
    ttl = f"{hours:g}h" if hours >= 1 else f"{ttl_s / 60:g}m"
    msg = f"capture {cid} not found" + (f": {detail}" if detail else "")
    return OpError("capture_not_found", msg,
                   hint=f"Captures expire {ttl} after last use or are evicted by caps; "
                        "capture again (captures(action='list') shows what is kept).")


class LoadedCapture:
    """A published capture: ``.meta``, ``.index()``, ``.raw(name)``, ``.shot(root)``,
    ``.derived(name)`` and ``.put_derived(name, bytes)``.

    Reads are lazy. If the capture is deleted while this object is alive, reads
    raise ``OpError("capture_not_found")``; a facet that was simply not captured
    reads as None.
    """

    def __init__(self, store: CaptureStore, cid: str, path: str, meta: CaptureMeta) -> None:
        self.store = store
        self.id = cid
        self.path = path
        self.meta = meta
        self._reader: Any = None

    def __repr__(self) -> str:
        return f"LoadedCapture({self.id!r}, {self.meta.serial}/{self.meta.package})"

    # ---- existence ------------------------------------------------------ #
    def exists(self) -> bool:
        return os.path.exists(os.path.join(self.path, COMPLETE_MARKER))

    def _gone(self, detail: str = "it was deleted") -> OpError:
        return _not_found(self.id, self.store.ttl_s, detail)

    def _read(self, rel: str) -> bytes | None:
        try:
            return _read_bytes(os.path.join(self.path, rel))
        except FileNotFoundError:
            if not self.exists():
                raise self._gone() from None
            return None
        except NotADirectoryError:
            raise self._gone() from None

    # ---- facets --------------------------------------------------------- #
    def raw(self, name: str) -> bytes | None:
        """Raw facet bytes by RawCapture attribute name (``views``, ``a11y``, ...)."""
        if name not in RAW_FILES:
            raise OpError("bad_args", f"unknown raw facet {name!r}",
                          candidates=sorted(RAW_FILES))
        return self._read(RAW_FILES[name])

    def shot(self, root: int) -> bytes | None:
        """The window's ``Screenshot`` message bytes (still deflated), or None."""
        return self._read(shot_file(int(root)))

    def shot_roots(self) -> list[int]:
        return self._roots("shot", re.compile(r"^w_(-?\d+)\.pb$"))

    def skp(self, root: int) -> bytes | None:
        return self._read(skp_file(int(root)))

    def skp_roots(self) -> list[int]:
        return self._roots("raw", re.compile(r"^skp_(-?\d+)\.bin$"))

    def _roots(self, sub: str, pat: re.Pattern) -> list[int]:
        try:
            names = os.listdir(os.path.join(self.path, sub))
        except FileNotFoundError:
            if not self.exists():
                raise self._gone() from None
            return []
        return sorted(int(m.group(1)) for m in map(pat.match, names) if m)

    @property
    def stripped(self) -> bool:
        """True when GC stripped heavy files (img/, out/, derived/, SKPs) to save space."""
        return os.path.exists(os.path.join(self.path, STRIPPED_MARKER))

    def facet_reader(self) -> Any:
        """C4's lazy decoder (``index.FacetReader``) over this capture's raw facets.

        Raw files are read on first use and kept for the life of this object;
        ``props(udid)``, ``prop_list(udid)``, ``slot_params(path)`` and
        ``sem_attrs(acv, id)`` decode one group at a time."""
        if self._reader is None:
            from .index import FacetReader  # lazy: keeps protobuf out of store imports

            self._reader = FacetReader(_LazyRaw(self))
        return self._reader

    def props(self, view_udid: int) -> dict[str, Any] | None:
        """``{name: normalized value}`` for one View (None when the capture has no
        properties for it). The props accessor the query layer (C6) reads."""
        return self.facet_reader().props(int(view_udid))

    def refmap(self) -> dict[str, str]:
        data = self._read(REFMAP_FILE)
        return json.loads(data.decode("utf-8")) if data else {}

    def raw_capture(self) -> RawCapture:
        """Every stored facet as a RawCapture (for rebuilding the index)."""
        files: dict[str, bytes] = {}
        for rel in RAW_FILES.values():
            data = self._read(rel)
            if data is not None:
                files[rel] = data
        for root in self.shot_roots():
            data = self.shot(root)
            if data is not None:
                files[shot_file(root)] = data
        for root in self.skp_roots():
            data = self.skp(root)
            if data is not None:
                files[skp_file(root)] = data
        return RawCapture.from_files(self.meta, files)

    # ---- index ---------------------------------------------------------- #
    def index(self) -> Index:
        """The hydrated index (memory LRU, else ``index.jsonl.gz``, else rebuilt
        from the raw facets when the file is unreadable or of another schema)."""
        if not self.exists():
            raise self._gone()
        cached = self.store._cache_get(self.id)
        if cached is not None:
            return cached
        data = self._read(INDEX_FILE)
        ix: Index | None = None
        if data is not None:
            try:
                ix = index_from_jsonl(data, self.meta)
            except Exception:  # noqa: BLE001 - truncated gzip, bad JSON, old schema
                ix = None
        if ix is None:
            ix = self.store._rebuild_index(self)
        self.store._cache_put(self.id, ix)
        return ix

    # ---- derived artifacts ------------------------------------------------ #
    def derived_path(self, name: str) -> str:
        """Absolute path of a derived artifact. ``name`` is ``derived/<f>``,
        ``img/<f>`` or ``out/<f>``; a bare file name means ``derived/<f>``."""
        return os.path.join(self.path, _derived_rel(name))

    def derived(self, name: str) -> bytes | None:
        return self._read(_derived_rel(name))

    def put_derived(self, name: str, data: bytes) -> str:
        """Write a derived artifact atomically (temp then replace) under the
        per-capture lock; returns its absolute path."""
        rel = _derived_rel(name)
        path = os.path.join(self.path, rel)
        if not self.exists():
            raise self._gone()
        try:
            with _lock_for(os.path.join(self.path, CAPTURE_LOCK)):
                if not self.exists():
                    raise self._gone()
                _ensure_dirs(self.path, os.path.dirname(rel))
                _atomic_write(path, bytes(data), durable=False)
        except (FileNotFoundError, NotADirectoryError):
            raise self._gone() from None  # deleted while we waited or wrote
        return path

    # ---- bookkeeping ------------------------------------------------------ #
    def nbytes(self) -> int:
        return _tree_size(self.path)

    def node_count(self) -> int | None:
        """Nodes in the index, read from the index header (no hydration)."""
        cached = self.store._cache_peek(self.id)
        if cached is not None:
            return len(cached.nodes)
        try:
            with gzip.open(os.path.join(self.path, INDEX_FILE), "rb") as f:
                header = json.loads(f.readline().decode("utf-8"))
            return int(header.get("count"))
        except (OSError, ValueError, TypeError, EOFError):
            return None

    def age_s(self, now: float | None = None) -> float:
        now = self.store.clock() if now is None else now
        return max(0.0, now - float(self.meta.created_at or 0.0))


class _LazyRaw:
    """The RawCapture attributes FacetReader reads, fetched from disk on first use."""

    def __init__(self, loaded: LoadedCapture) -> None:
        self._loaded = loaded
        self._cache: dict[str, bytes | None] = {}
        self.meta = loaded.meta

    def _get(self, name: str) -> bytes | None:
        if name not in self._cache:
            self._cache[name] = self._loaded.raw(name)
        return self._cache[name]

    @property
    def views(self) -> bytes:
        return self._get("views") or b""

    @property
    def compose_sem(self) -> bytes:
        return self._get("compose_sem") or b""

    @property
    def slots(self) -> bytes | None:
        return self._get("slots")

    @property
    def a11y(self) -> bytes:
        return self._get("a11y") or b""


def _derived_rel(name: str) -> str:
    parts = str(name).replace("\\", "/").split("/")
    if len(parts) == 1:
        parts = ["derived", *parts]
    if parts[0] not in DERIVED_DIRS or any(p in ("", ".", "..") for p in parts) \
            or any(p.startswith(".") for p in parts):
        raise OpError("bad_args", f"bad derived artifact name {name!r}",
                      hint="Use derived/<file>, img/<file> or out/<file>.")
    return "/".join(parts)


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class _Entry:
    """One published capture as GC sees it."""

    cid: str
    lineage: tuple[str, str]
    created_at: float
    used_at: float
    pinned: bool
    labeled: bool
    nbytes: int
    heavy: int


class CaptureStore:
    """The on-disk capture store shared by the CLI and the MCP server (spec 4).

    ``root=None`` uses ``model.default_store_root()``. ``persist=None`` honours
    ``$INSPECTOR_WIDGET_CAPTURE_PERSIST`` (default on); False keeps everything
    in a private temp directory that :meth:`close` (and exit) removes. ``clock``
    is wall time (``created_at``, ``.used``, TTLs). The keyword-only arguments
    override the environment and the spec's defaults (mostly for tests).
    """

    def __init__(self, root: str | None = None, persist: bool | None = None,
                 clock: Callable[[], float] = time.time, *,
                 env: Mapping[str, str] | None = None,
                 ttl_s: float | None = None, max_captures: int | None = None,
                 max_mb: int | None = None, max_bytes: int | None = None,
                 lineage_cap: int = LINEAGE_CAP, max_pinned: int = MAX_PINNED,
                 mem_indexes: int = MEM_INDEXES, mem_bytes: int = MEM_BYTES,
                 durable: bool = True, gc_on_publish: bool = True,
                 rebuild: Callable[[RawCapture, dict], Index] | None = None,
                 new_id: Callable[[], str] = new_capture_id) -> None:
        env = os.environ if env is None else env
        self.clock = clock
        self.persist = env_persist(env) if persist is None else bool(persist)
        self.configured_root = os.path.abspath(os.path.expanduser(root)) if root \
            else default_store_root(env)
        if self.persist:
            self.root = self.configured_root
        else:
            self.root = tempfile.mkdtemp(prefix="inspector-widget-captures-")
            os.chmod(self.root, 0o700)
            atexit.register(self.close)
        self.ttl_s = max(float(MIN_TTL_S), float(ttl_s)) if ttl_s is not None else env_ttl_s(env)
        self.max_captures = int(max_captures) if max_captures else env_max_captures(env)
        if max_bytes is not None:
            self.max_bytes = int(max_bytes)
        elif max_mb is not None:
            self.max_bytes = int(max_mb) << 20
        else:
            self.max_bytes = env_max_bytes(env)
        self.lineage_cap = int(lineage_cap)
        self.max_pinned = int(max_pinned)
        self.mem_indexes = max(1, int(mem_indexes))
        self.mem_bytes = int(mem_bytes)
        self.durable = bool(durable)
        self.gc_on_publish = bool(gc_on_publish)
        self.rebuild = rebuild
        self._new_id = new_id
        self._layout_ok = False
        self._closed = False
        self._cache: OrderedDict[str, tuple[Index, int]] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._gc_pending: set[str] | None = None

    # ------------------------------------------------------------------ paths
    def _p(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)

    @property
    def captures_dir(self) -> str:
        return self._p("captures")

    def capture_dir(self, cid: str) -> str:
        return self._p("captures", cid)

    def spill_dir(self) -> str:
        """``<root>/spill`` for Phase-0 spill files (created, 0700)."""
        self._ensure_layout()
        return self._p("spill")

    def _ensure_layout(self) -> None:
        if self._layout_ok:
            return
        if self._closed:
            raise RuntimeError("the capture store is closed")
        if not os.path.isdir(self.root):
            parent = os.path.dirname(self.root)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            _mkdir(self.root)
        for sub in ("captures", "lineages", ".staging", ".trash", "spill"):
            p = self._p(sub)
            if not os.path.isdir(p):
                _mkdir(p)
        self._layout_ok = True

    def close(self) -> None:
        """Forget cached indexes; in memory-only mode also delete the temp root."""
        with self._cache_lock:
            self._cache.clear()
        if not self.persist and not self._closed:
            self._closed = True
            self._layout_ok = False
            _rmtree(self.root)

    @property
    def memory_only(self) -> bool:
        return not self.persist

    # ------------------------------------------------------------------ locks
    @contextlib.contextmanager
    def refs_lock(self) -> Iterator[None]:
        """The store lock (``store.lock``): ref allocation, publish, label moves,
        pins and lineage files. Re-entrant for the holding thread; never hold it
        across device I/O. A GC requested by publish runs after the outermost
        release."""
        self._ensure_layout()
        lock = _lock_for(self._p("store.lock"))
        lock.acquire()
        try:
            yield
        finally:
            outermost = lock.depth == 1
            lock.release()
        if outermost and self._gc_pending is not None:
            keep, self._gc_pending = self._gc_pending, None
            with contextlib.suppress(Exception):  # retention is best-effort after a publish
                self.gc(keep=keep)

    # ------------------------------------------------------------------ refs
    def next_refs(self, n: int) -> int:
        """Reserve ``n`` consecutive refs and return the first number (``n1`` is 1).
        The counter lives in ``store.json`` and only ``gc(all=True)`` resets it."""
        if int(n) < 0:
            raise ValueError("n must be >= 0")
        with self.refs_lock():
            first = self._read_next_ref()
            if n:
                self._write_store_json(first + int(n))
            return first

    def _read_next_ref(self) -> int:
        data = _read_json(self._p("store.json"))
        if isinstance(data, dict) and isinstance(data.get("next_ref"), int) \
                and data["next_ref"] >= 1:
            return max(data["next_ref"], 1)
        return self._recover_next_ref()

    def _write_store_json(self, next_ref: int) -> None:
        _atomic_write(self._p("store.json"),
                      _json_bytes({"schema": STORE_SCHEMA, "next_ref": int(next_ref)}),
                      self.durable)

    def _recover_next_ref(self) -> int:
        """store.json is missing or unreadable: continue above every ref on disk."""
        high = 0
        for cid in self._complete_ids():
            data = _read_json(os.path.join(self.capture_dir(cid), REFMAP_FILE))
            if isinstance(data, dict):
                for ref in data.values():
                    if is_ref(ref):
                        high = max(high, ref_num(ref))
        for _lin, st in self._lineages():
            for ref in st.tomb:
                if is_ref(ref):
                    high = max(high, ref_num(ref))
        return high + 1

    # ------------------------------------------------------------------ lineages
    def _lineage_path(self, serial: str, package: str) -> str:
        return self._p("lineages", lineage_file_name(serial, package))

    def lineage_state(self, serial: str, package: str) -> LineageState:
        """The lineage's state (empty if it has none yet). Modify and save it under
        :meth:`refs_lock`, and before :meth:`publish` (which updates latest and
        history itself) or after re-reading it."""
        data = _read_json(self._lineage_path(serial, package))
        if isinstance(data, dict) and (str(data.get("serial")), str(data.get("package"))) \
                != (str(serial), str(package)):
            data = None  # another lineage's file (a name collision): never trust it
        st = LineageState.from_dict(data if isinstance(data, dict) else None)
        st.__dict__["_iw_lineage"] = (str(serial), str(package))
        return st

    def save_lineage_state(self, st: LineageState, serial: str | None = None,
                           package: str | None = None) -> None:
        """Persist a LineageState obtained from :meth:`lineage_state` (or name the
        lineage). Caps history at 50 and tombstones at 5,000 (oldest dropped)."""
        lineage = (serial, package) if serial is not None and package is not None \
            else st.__dict__.get("_iw_lineage")
        if lineage is None and st.latest:
            meta = self._read_meta(st.latest)
            lineage = meta.lineage if meta else None
        if lineage is None:
            raise ValueError("save_lineage_state needs the state from lineage_state() "
                             "or an explicit serial and package")
        with self.refs_lock():
            self._write_lineage(lineage, st)

    def _write_lineage(self, lineage: tuple[str, str], st: LineageState) -> None:
        st.history = list(dict.fromkeys(st.history))[:HISTORY_CAP]
        if len(st.tomb) > TOMB_CAP:
            st.tomb = dict(list(st.tomb.items())[-TOMB_CAP:])
        body = {"serial": lineage[0], "package": lineage[1], **st.to_dict()}
        _atomic_write(self._lineage_path(*lineage), _json_bytes(body), self.durable)
        st.__dict__["_iw_lineage"] = (str(lineage[0]), str(lineage[1]))

    def _lineages(self) -> list[tuple[tuple[str, str], LineageState]]:
        out = []
        try:
            names = sorted(os.listdir(self._p("lineages")))
        except OSError:
            return out
        for name in names:
            if not name.endswith(".json"):
                continue
            data = _read_json(self._p("lineages", name))
            if not isinstance(data, dict) or "serial" not in data or "package" not in data:
                continue
            lin = (str(data["serial"]), str(data["package"]))
            st = LineageState.from_dict(data)
            st.__dict__["_iw_lineage"] = lin
            out.append((lin, st))
        return out

    def lineages(self) -> list[tuple[str, str]]:
        """Every lineage (serial, package) the store has seen."""
        return [lin for lin, _st in self._lineages()]

    # ------------------------------------------------------------------ session
    def default_session(self) -> tuple[str, str] | None:
        """The last attach or capture from either surface, as ``(serial, package)``."""
        data = _read_json(self._p("session.json"))
        if isinstance(data, dict) and data.get("serial") and data.get("package"):
            return str(data["serial"]), str(data["package"])
        return None

    def set_default_session(self, serial: str, package: str) -> None:
        with self.refs_lock():
            _atomic_write(self._p("session.json"),
                          _json_bytes({"serial": serial, "package": package,
                                       "at": self.clock()}), self.durable)

    # ------------------------------------------------------------------ captures on disk
    def _complete_ids(self) -> list[str]:
        try:
            names = os.listdir(self.captures_dir)
        except OSError:
            return []
        return [n for n in names if is_capture_id(n)
                and os.path.exists(os.path.join(self.captures_dir, n, COMPLETE_MARKER))]

    def exists(self, cid: str) -> bool:
        return is_capture_id(cid) and os.path.exists(
            os.path.join(self.capture_dir(cid.lower()), COMPLETE_MARKER))

    def _read_meta(self, cid: str) -> CaptureMeta | None:
        try:
            data = _read_bytes(os.path.join(self.capture_dir(cid), META_FILE))
            return CaptureMeta.from_json(data)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _is_pinned(self, cid: str) -> bool:
        return os.path.exists(os.path.join(self.capture_dir(cid), PINNED_MARKER))

    def _pinned_ids(self) -> list[str]:
        return [c for c in self._complete_ids() if self._is_pinned(c)]

    def _overlay(self, meta: CaptureMeta, labels_by_lineage: dict | None = None) -> CaptureMeta:
        """Current label (lineage file) and pin (marker) onto a meta read from disk."""
        if labels_by_lineage is not None and meta.lineage in labels_by_lineage:
            labels = labels_by_lineage[meta.lineage]
        else:
            labels = self.lineage_state(*meta.lineage).labels
        meta.label = next((name for name, cid in labels.items() if cid == meta.id), None)
        meta.pinned = self._is_pinned(meta.id)
        return meta

    # ------------------------------------------------------------------ publish
    def publish(self, raw: RawCapture, ix: Index, refmap: Mapping[str, str], *,
                tomb: Mapping[str, list] | None = None) -> str:
        """Write a capture atomically and return its new id.

        Sets ``raw.meta.id``, ``created_at`` (if unset) and ``prev`` (if unset: the
        lineage's latest), then writes meta.json, refmap.json, the raw facets, the
        per-window shots and index.jsonl.gz into ``.staging``, marks it complete and
        renames it into ``captures/``. Under the store lock it then makes it the
        lineage's latest (unless the latest has a newer ``created_at``: a capture
        that started first but published last goes into ``history`` by time, its
        ``prev`` is the next older capture, and its tombstones are dropped because
        they name refs the newer latest holds), applies ``meta.label`` (moving the label from another
        capture of the lineage), merges ``tomb`` updates (refs present in
        ``refmap`` leave the tomb) and sets the default session. ``meta.pinned``
        pins it (bad_args when 20 are pinned already).
        """
        meta = raw.meta
        if meta is None:
            raise ValueError("raw.meta is required")
        serial, package = meta.lineage
        if meta.label is not None and not self._label_ok(meta.label):
            raise OpError("bad_args", f"invalid label {meta.label!r}", hint=LABEL_HINT)
        refmap = dict(refmap)
        self._ensure_layout()
        with self.refs_lock():
            st = self.lineage_state(serial, package)
            if meta.pinned:
                self._check_pin_room()
            if not meta.created_at:
                meta.created_at = self.clock()
            latest = st.latest if st.latest and self.exists(st.latest) else None
            lmeta = self._read_meta(latest) if latest else None
            # A capture that started first can publish last (two processes capture
            # one app at once). The lineage follows created_at, not publish order:
            # the newer latest stays, and this one goes into history by time.
            late = lmeta is not None and float(lmeta.created_at or 0.0) > float(meta.created_at)
            pos = 0
            if late:
                born = {h: getattr(self._read_meta(h), "created_at", 0.0) or 0.0
                        for h in st.history}
                pos = next((i for i, h in enumerate(st.history)
                            if float(born[h]) <= float(meta.created_at)), len(st.history))
                if meta.prev is None and pos < len(st.history):
                    meta.prev = st.history[pos]
            elif meta.prev is None and latest:
                meta.prev = latest
            cid = self._write_capture(raw, ix, refmap)
            if late:
                st.history.insert(pos, cid)
            else:
                st.latest = cid
                st.history = [cid, *[h for h in st.history if h != cid]]
            if meta.label:
                st.labels = {k: v for k, v in st.labels.items() if v != cid}
                st.labels[meta.label] = cid
            if tomb and not late:
                # (a late capture was matched against the newer latest, so its
                # tombstones name refs that latest still has)
                for ref, info in tomb.items():
                    st.tomb.pop(ref, None)
                    st.tomb[ref] = list(info)
            for ref in refmap.values():
                st.tomb.pop(ref, None)
            self._write_lineage((serial, package), st)
            self.set_default_session(serial, package)
            if self.gc_on_publish:
                self._gc_pending = (self._gc_pending or set()) | {cid}
        self._cache_put(cid, dataclasses.replace(ix, meta=meta))
        return cid

    def _write_capture(self, raw: RawCapture, ix: Index, refmap: dict) -> str:
        meta = raw.meta
        cid, staging = self._make_staging()
        try:
            meta.id = cid
            files = raw.files()
            for rel, data in files.items():
                _ensure_dirs(staging, os.path.dirname(rel))
                _write_new(os.path.join(staging, rel), data, self.durable)
            _write_new(os.path.join(staging, REFMAP_FILE), _json_bytes(refmap), self.durable)
            self._write_id_files(staging, raw, ix)
            now = self.clock()
            _touch(os.path.join(staging, USED_MARKER), now)
            if meta.pinned:
                _touch(os.path.join(staging, PINNED_MARKER), now)
            for sub in {os.path.dirname(rel) for rel in files}:
                if sub and self.durable:
                    _fsync_dir(os.path.join(staging, sub))
            _write_new(os.path.join(staging, COMPLETE_MARKER), b"", self.durable)
            if self.durable:
                _fsync_dir(staging)
            for _attempt in range(16):
                try:
                    os.rename(staging, self.capture_dir(cid))
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY, errno.ENOTDIR):
                        raise
                # The id was taken meanwhile: take another and rewrite what names it.
                cid, staging = self._re_id(staging)
                meta.id = cid
                for name in (META_FILE, INDEX_FILE):
                    os.remove(os.path.join(staging, name))
                self._write_id_files(staging, raw, ix)
            else:  # pragma: no cover - 16 collisions in a row
                raise OSError(errno.EEXIST, "could not find a free capture id")
            if self.durable:
                _fsync_dir(self.captures_dir)
            return cid
        except BaseException:
            _rmtree(staging)
            raise

    def _write_id_files(self, staging: str, raw: RawCapture, ix: Index) -> None:
        meta = raw.meta
        # A node whose ref was minted for this capture (match "new") has been
        # there since this capture; refs.assign could not name it before the id
        # existed. Rewritten on every id retry, so it always names the final id.
        for n in ix.nodes.values():
            if n.match == "new":
                n.since = meta.id
        _write_new(os.path.join(staging, META_FILE), meta.to_json(), self.durable)
        _write_new(os.path.join(staging, INDEX_FILE),
                   index_to_jsonl(dataclasses.replace(ix, meta=meta), compress=True),
                   self.durable)

    def _fresh_id(self) -> str:
        for _ in range(64):
            cid = self._new_id().lower()
            if not is_capture_id(cid):
                raise ValueError(f"id source returned {cid!r}, not a capture id")
            if not os.path.lexists(self.capture_dir(cid)):
                return cid
        raise OSError(errno.EEXIST, "could not find a free capture id")

    def _make_staging(self) -> tuple[str, str]:
        while True:
            cid = self._fresh_id()
            staging = self._p(".staging", f"{cid}.{os.getpid()}")
            if _mkdir(staging):
                return cid, staging

    def _re_id(self, staging: str) -> tuple[str, str]:
        while True:
            cid = self._fresh_id()
            target = self._p(".staging", f"{cid}.{os.getpid()}")
            if os.path.lexists(target):
                continue
            os.rename(staging, target)
            return cid, target

    # ------------------------------------------------------------------ load / list
    def load(self, cid: str) -> LoadedCapture:
        """A published capture by id (case-insensitive). Unknown, expired or
        deleted ids raise ``OpError("capture_not_found")``; resolve labels and
        ``latest`` with :meth:`resolve` first. Loading counts as a use."""
        if not is_capture_id(cid):
            raise OpError("bad_args", f"not a capture id: {cid!r}", hint=SPEC_HINT)
        cid = cid.lower()
        path = self.capture_dir(cid)
        if not os.path.exists(os.path.join(path, COMPLETE_MARKER)):
            raise _not_found(cid, self.ttl_s)
        meta = self._read_meta(cid)
        if meta is None:
            raise _not_found(cid, self.ttl_s, "its meta.json is unreadable")
        self._overlay(meta)
        self._touch_used(path)
        return LoadedCapture(self, cid, path, meta)

    def _touch_used(self, path: str) -> None:
        """Record a use: ``.used`` mtime, at most once a minute."""
        used = os.path.join(path, USED_MARKER)
        now = self.clock()
        last = _mtime(used)
        if last is None or now - last >= USED_TOUCH_S:
            with contextlib.suppress(OSError):
                _touch(used, now)

    def list(self, lineage: tuple[str, str] | None = None,
             limit: int | None = 20) -> list[CaptureMeta]:
        """Published captures, newest ``created_at`` first, with current labels and
        pins. ``limit`` None or 0 means all."""
        labels = {lin: st.labels for lin, st in self._lineages()}
        metas = []
        for cid in self._complete_ids():
            meta = self._read_meta(cid)
            if meta is None:
                continue
            if lineage is not None and meta.lineage != tuple(lineage):
                continue
            metas.append(self._overlay(meta, labels))
        metas.sort(key=lambda m: (m.created_at, m.id), reverse=True)
        return metas[:limit] if limit else metas

    def summary(self) -> dict[str, Any]:
        """``{root, captures, bytes, ttl_s, persist}`` for listings."""
        ids = self._complete_ids()
        return {"root": self.root, "captures": len(ids),
                "bytes": sum(_tree_size(self.capture_dir(c)) for c in ids),
                "ttl_s": self.ttl_s, "persist": self.persist}

    # ------------------------------------------------------------------ resolve
    def resolve(self, spec: str | None = "latest",
                lineage: tuple[str, str] | None = None) -> str:
        """The single resolver for the ``capture`` argument on both surfaces.

        Accepts an id (case-insensitive), ``label`` or ``@label``, ``latest``,
        ``prev`` and ``latest~N``. ``latest``/``prev``/``latest~N`` count within
        ``lineage`` (the resolved session) when given, else across the whole store
        by ``created_at``. A label resolves in ``lineage`` first, else it must be
        unique across the store (``ambiguous`` lists the candidates).
        """
        s = str(spec).strip() if spec is not None else ""
        s = s or "latest"
        lineage = (str(lineage[0]), str(lineage[1])) if lineage else None
        if s == "prev":
            return self._resolve_nth(1, s, lineage)
        m = _LATEST_RE.match(s)
        if m:
            return self._resolve_nth(int(m.group(1) or 0), s, lineage)
        if is_capture_id(s):
            cid = s.lower()
            if self.exists(cid):
                return cid
            raise _not_found(cid, self.ttl_s)
        name = s.removeprefix("@")
        if LABEL_RE.match(name):
            return self._resolve_label(name, lineage)
        raise OpError("bad_args", f"bad capture reference {spec!r}", hint=SPEC_HINT)

    def _resolve_nth(self, n: int, spec: str, lineage: tuple[str, str] | None) -> str:
        if lineage is not None:
            st = self.lineage_state(*lineage)
            ids = [c for c in st.history if self.exists(c)]
            if n >= len(ids):  # older captures than history keeps (pinned ones)
                older = [m.id for m in self.list(lineage, limit=None) if m.id not in ids]
                ids.extend(older)
            where = f"{lineage[0]}/{lineage[1]}"
        else:
            ids = [m.id for m in self.list(limit=None)]
            where = "the store"
        if n < len(ids):
            return ids[n]
        if not ids:
            raise OpError("capture_not_found", f"no captures yet in {where}",
                          hint="Run capture() first.")
        raise OpError("capture_not_found",
                      f"{spec} needs {n + 1} captures but {where} has {len(ids)}",
                      hint="captures(action='list') shows what is kept.")

    def _resolve_label(self, name: str, lineage: tuple[str, str] | None) -> str:
        hits: list[tuple[tuple[str, str], str]] = []
        for lin, st in self._lineages():
            cid = st.labels.get(name)
            if cid and self.exists(cid):
                if lineage is not None and lin == lineage:
                    return cid
                hits.append((lin, cid))
        if len(hits) == 1:
            return hits[0][1]
        if hits:
            raise OpError("ambiguous", f"label {name!r} exists in {len(hits)} lineages",
                          hint="Pass the capture id, or the serial and package of the app.",
                          candidates=[f"{cid} {lin[0]}/{lin[1]}" for lin, cid in hits])
        raise OpError("capture_not_found", f"no capture is labelled {name!r}",
                      hint="captures(action='list') shows ids and labels.")

    # ------------------------------------------------------------------ labels, pins, drop
    @staticmethod
    def _label_ok(name: str) -> bool:
        return is_valid_label(name) and name not in RESERVED_LABELS

    def label(self, cid: str, name: str | None) -> str | None:
        """Label a capture (one label per capture, unique per lineage). Moving a
        label that another capture of the lineage had returns that capture's id
        (``moved_from``), else None. ``name`` None or "" removes the label."""
        meta = self.load(cid).meta
        if name:
            name = name.removeprefix("@")
            if not self._label_ok(name):
                raise OpError("bad_args", f"invalid label {name!r}", hint=LABEL_HINT)
        with self.refs_lock():
            if not self.exists(meta.id):
                raise _not_found(meta.id, self.ttl_s)
            st = self.lineage_state(*meta.lineage)
            previous = st.labels.get(name) if name else None
            st.labels = {k: v for k, v in st.labels.items() if v != meta.id}
            if name:
                st.labels[name] = meta.id
            self._write_lineage(meta.lineage, st)
        return previous if previous and previous != meta.id else None

    def _check_pin_room(self) -> None:
        pinned = self._pinned_ids()
        if len(pinned) >= self.max_pinned:
            raise OpError("bad_args", f"{len(pinned)} captures are pinned already "
                          f"(at most {self.max_pinned})", hint="Unpin one first.",
                          candidates=sorted(pinned))

    def pin(self, cid: str, on: bool = True) -> None:
        """Pin (exempt from TTL and caps, not from drop) or unpin a capture."""
        loaded = self.load(cid)
        with self.refs_lock():
            if not loaded.exists():
                raise loaded._gone()
            marker = os.path.join(loaded.path, PINNED_MARKER)
            if on:
                if not os.path.exists(marker):
                    self._check_pin_room()
                    _touch(marker, self.clock())
            else:
                with contextlib.suppress(FileNotFoundError):
                    os.remove(marker)

    def drop(self, cid: str) -> None:
        """Delete a capture (pinned or not): rename to ``.trash`` then remove.
        A reader that races it gets ``capture_not_found``."""
        if not is_capture_id(cid):
            raise OpError("bad_args", f"not a capture id: {cid!r}", hint=SPEC_HINT)
        cid = cid.lower()
        if not self.exists(cid):
            raise _not_found(cid, self.ttl_s)
        trash = self._evict(cid, check_pin=False)
        if trash is None:
            raise _not_found(cid, self.ttl_s)
        _rmtree(trash)

    def _evict(self, cid: str, check_pin: bool) -> str | None:
        """Move a capture to ``.trash`` and fix its lineage; returns the trash path
        (None if it vanished or got pinned meanwhile). Takes the store lock."""
        meta = self._read_meta(cid)
        with self.refs_lock():
            path = self.capture_dir(cid)
            if check_pin and self._is_pinned(cid):
                return None
            trash = self._p(".trash", f"{cid}.{os.getpid()}.{os.urandom(3).hex()}")
            try:
                os.rename(path, trash)
            except FileNotFoundError:
                return None
            if meta is not None:
                st = self.lineage_state(*meta.lineage)
                self._forget(st, {cid})
                self._write_lineage(meta.lineage, st)
        self._cache_drop(cid)
        return trash

    def _forget(self, st: LineageState, gone: set[str]) -> None:
        st.history = [h for h in st.history if h not in gone]
        st.labels = {k: v for k, v in st.labels.items() if v not in gone}
        if st.latest in gone:
            st.latest = next((h for h in st.history if self.exists(h)), None)

    # ------------------------------------------------------------------ gc
    def gc(self, all: bool = False, *, keep: set[str] | frozenset[str] | tuple = ()) -> dict:
        """Apply retention (spec 4.3) under a non-blocking ``gc.lock``.

        Order: stale ``.staging`` (> 1 h) and ``.trash`` are purged, spill files
        older than 1 h too; then expired captures, the per-lineage cap, the total
        count cap and the byte cap (heavy files first, then whole captures).
        Unlabeled captures go first, then the least recently used. Pinned
        captures and ``keep`` are never evicted. ``all=True`` wipes everything,
        including the ref counter.
        """
        self._ensure_layout()
        if all:
            return self._gc_all()
        glock = _lock_for(self._p("gc.lock"))
        if not glock.acquire(blocking=False):
            return {"skipped": "another gc is running"}
        try:
            return self._gc(set(keep))
        finally:
            glock.release()

    def _gc(self, keep: set[str]) -> dict:
        now = self.clock()
        report: dict[str, Any] = {"removed": [], "stripped": [],
                                  "staging_purged": self._purge_dir(".staging", now),
                                  "trash_purged": self._purge_dir(".trash", None),
                                  "spill_purged": self._purge_spill(now),
                                  "incomplete_purged": self._purge_incomplete(now)}
        entries = self._scan(now)
        victims: dict[str, str] = {}

        def evictable(e: _Entry) -> bool:
            return not e.pinned and e.cid not in keep and e.cid not in victims

        def order(es: list[_Entry]) -> list[_Entry]:
            return sorted(es, key=lambda e: (e.labeled, e.used_at, e.created_at, e.cid))

        for e in entries:
            if evictable(e) and now - e.used_at > self.ttl_s:
                victims[e.cid] = "expired"
        by_lineage: dict[tuple[str, str], list[_Entry]] = {}
        for e in entries:
            if e.cid not in victims and not e.pinned:
                by_lineage.setdefault(e.lineage, []).append(e)
        for es in by_lineage.values():
            excess = len(es) - self.lineage_cap
            for e in order([e for e in es if evictable(e)])[:max(0, excess)]:
                victims[e.cid] = "lineage_cap"
        live = [e for e in entries if e.cid not in victims]
        excess = len(live) - self.max_captures
        for e in order([e for e in live if evictable(e)])[:max(0, excess)]:
            victims[e.cid] = "count_cap"
        live = [e for e in entries if e.cid not in victims]
        total = sum(e.nbytes for e in live)
        if total > self.max_bytes:
            for e in order([e for e in live if evictable(e) and e.heavy]):
                if total <= self.max_bytes:
                    break
                freed = self._strip(e.cid)
                if freed:
                    total -= freed
                    e.nbytes -= freed
                    report["stripped"].append(e.cid)
            for e in order([e for e in live if evictable(e)]):
                if total <= self.max_bytes:
                    break
                victims[e.cid] = "bytes"
                total -= e.nbytes
        for cid, why in victims.items():
            trash = self._evict(cid, check_pin=True)
            if trash is not None:
                _rmtree(trash)
                report["removed"].append({"id": cid, "why": why})
        self._prune_lineages()
        remaining = [e for e in entries if e.cid not in {r["id"] for r in report["removed"]}]
        report["captures"] = len(remaining)
        report["bytes"] = sum(e.nbytes for e in remaining)
        return report

    def _scan(self, now: float) -> list[_Entry]:
        labeled = {cid for _lin, st in self._lineages() for cid in st.labels.values()}
        out = []
        for cid in self._complete_ids():
            path = self.capture_dir(cid)
            meta = self._read_meta(cid)
            done = _mtime(os.path.join(path, COMPLETE_MARKER)) or now
            used = _mtime(os.path.join(path, USED_MARKER))
            if used is None:
                used = done
            heavy = sum(_tree_size(os.path.join(path, d)) for d in HEAVY_DIRS)
            for root in self._skp_files(path):
                with contextlib.suppress(OSError):
                    heavy += os.lstat(root).st_size
            # A capture whose meta.json is unreadable (torn, corrupt, a newer
            # format) cannot be loaded or listed, but it still takes space and
            # still expires: it counts in a lineage of its own, dated by .complete.
            lineage = meta.lineage if meta is not None else ("", "")
            created = float(meta.created_at or 0.0) if meta is not None else float(done)
            out.append(_Entry(cid, lineage, created, used, self._is_pinned(cid),
                              cid in labeled, _tree_size(path), heavy))
        return out

    @staticmethod
    def _skp_files(path: str) -> list[str]:
        try:
            names = os.listdir(os.path.join(path, "raw"))
        except OSError:
            return []
        return [os.path.join(path, "raw", n) for n in names if re.match(r"^skp_-?\d+\.bin$", n)]

    def _strip(self, cid: str) -> int:
        """Delete a capture's heavy files (img/, out/, derived/, SKPs); returns bytes freed."""
        path = self.capture_dir(cid)
        freed = 0
        lock = _lock_for(os.path.join(path, CAPTURE_LOCK))
        try:
            lock.acquire()
        except FileNotFoundError:
            return 0  # evicted meanwhile
        try:
            if not os.path.exists(os.path.join(path, COMPLETE_MARKER)):
                return 0
            for d in HEAVY_DIRS:
                sub = os.path.join(path, d)
                size = _tree_size(sub)
                if size:
                    _rmtree(sub)
                    freed += size
            for f in self._skp_files(path):
                with contextlib.suppress(OSError):
                    size = os.lstat(f).st_size
                    os.remove(f)
                    freed += size
            if freed:
                with contextlib.suppress(OSError):
                    _touch(os.path.join(path, STRIPPED_MARKER), self.clock())
        finally:
            lock.release()
        return freed

    def _purge_dir(self, sub: str, now: float | None) -> int:
        """Remove entries of ``sub``: all of them (now=None) or those older than 1 h."""
        base = self._p(sub)
        try:
            names = os.listdir(base)
        except OSError:
            return 0
        n = 0
        for name in names:
            path = os.path.join(base, name)
            if now is not None:
                mt = _mtime(path)
                if mt is None or now - mt <= STAGING_TTL_S:
                    continue
            if os.path.isdir(path) and not os.path.islink(path):
                _rmtree(path)
            else:
                with contextlib.suppress(OSError):
                    os.remove(path)
            n += 1
        return n

    def _purge_incomplete(self, now: float) -> int:
        """Directories in captures/ without .complete (never produced by publish,
        but a hand edit or a foreign tool may leave them), older than 1 h."""
        try:
            names = os.listdir(self.captures_dir)
        except OSError:
            return 0
        n = 0
        for name in names:
            path = os.path.join(self.captures_dir, name)
            if os.path.exists(os.path.join(path, COMPLETE_MARKER)):
                continue
            mt = _mtime(path)
            if mt is not None and now - mt > STAGING_TTL_S:
                _rmtree(path)
                n += 1
        return n

    def _purge_spill(self, now: float) -> int:
        from ..output import purge_spill  # lazy: output pulls in the normalizers
        return purge_spill(self._p("spill"), SPILL_TTL_S, now=now)

    def _prune_lineages(self) -> None:
        """Drop lineage entries that point at captures no longer on disk."""
        for lin, st in self._lineages():
            gone = {c for c in [*st.history, *st.labels.values(), st.latest]
                    if c and not self.exists(c)}
            if not gone:
                continue
            with self.refs_lock():
                st = self.lineage_state(*lin)
                gone = {c for c in [*st.history, *st.labels.values(), st.latest]
                        if c and not self.exists(c)}
                if gone:
                    self._forget(st, gone)
                    self._write_lineage(lin, st)

    def _gc_all(self) -> dict:
        glock = _lock_for(self._p("gc.lock"))
        with glock, self.refs_lock():
            ids = self._complete_ids()
            # Captures go to .trash first (as every eviction does), so a reader
            # racing the wipe sees capture_not_found, never a half-deleted capture.
            try:
                names = os.listdir(self.captures_dir)
            except OSError:
                names = []
            for name in names:
                with contextlib.suppress(OSError):
                    os.rename(os.path.join(self.captures_dir, name),
                              self._p(".trash", f"{name}.{os.getpid()}.{os.urandom(3).hex()}"))
            for sub in ("captures", "lineages", "spill", ".trash"):
                self._purge_dir(sub, None)
            self._purge_dir(".staging", self.clock())
            for name in ("store.json", "session.json"):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(self._p(name))
            self._gc_pending = None
        with self._cache_lock:
            self._cache.clear()
        return {"all": True, "removed": len(ids),
                "note": "Wiped every capture, label, lineage and the default session. "
                        "The ref counter is reset: refs start again at n1."}

    # ------------------------------------------------------------------ index memory
    def _cache_get(self, cid: str) -> Index | None:
        with self._cache_lock:
            hit = self._cache.get(cid)
            if hit is None:
                return None
            self._cache.move_to_end(cid)
            return hit[0]

    def _cache_peek(self, cid: str) -> Index | None:
        with self._cache_lock:
            hit = self._cache.get(cid)
            return hit[0] if hit else None

    def _cache_put(self, cid: str, ix: Index) -> None:
        est = max(1, len(ix.nodes)) * NODE_BYTES_EST
        with self._cache_lock:
            self._cache[cid] = (ix, est)
            self._cache.move_to_end(cid)
            while len(self._cache) > 1 and (
                    len(self._cache) > self.mem_indexes
                    or sum(e for _ix, e in self._cache.values()) > self.mem_bytes):
                self._cache.popitem(last=False)

    def _cache_drop(self, cid: str) -> None:
        with self._cache_lock:
            self._cache.pop(cid, None)

    def cached_ids(self) -> list[str]:
        """Ids whose index is hydrated in memory, least recently used first."""
        with self._cache_lock:
            return list(self._cache)

    def _rebuild_index(self, loaded: LoadedCapture) -> Index:
        """Rebuild an unreadable index from the raw facets and refmap, and save it.

        The default rebuild runs the analyzers with the capture's lint option, but
        never contrast (about 4 s) while this thread holds the store lock; such an
        index is served but not saved, so a later load outside the lock rebuilds it
        in full. A node the refmap has no ref for (a newer index builder emits a
        key the capture never had) gets a fresh ref from the counter, added to
        ``refmap.json`` under the store lock, so every process agrees on it and the
        index stays in ref space (the next capture of the lineage matches it)."""
        locked = _lock_for(self._p("store.lock")).held
        fn = self.rebuild
        try:
            if fn is None:
                ix = _default_rebuild(loaded.raw_capture(), loaded.refmap(), contrast=not locked)
            else:
                ix = fn(loaded.raw_capture(), loaded.refmap())
        except OpError:
            raise
        except Exception as exc:
            raise _not_found(loaded.id, self.ttl_s,
                             f"its index is unreadable and could not be rebuilt ({exc})") from exc
        ix = dataclasses.replace(ix, meta=loaded.meta)
        missing = [nid for nid in ix.nodes if not is_ref(nid)]
        if missing:
            ix = self._mint_missing(loaded, ix, missing)
        complete = all(is_ref(nid) for nid in ix.nodes)
        partial = locked and fn is None and \
            getattr(loaded.meta.options, "lint", "tree") == "full"
        if complete and not partial:
            with contextlib.suppress(OSError), \
                    _lock_for(os.path.join(loaded.path, CAPTURE_LOCK)):
                if loaded.exists():
                    _atomic_write(os.path.join(loaded.path, INDEX_FILE),
                                  index_to_jsonl(ix, compress=True), durable=False)
        return ix

    def _mint_missing(self, loaded: LoadedCapture, ix: Index, missing: list[str]) -> Index:
        """Give the rebuilt index's key-space nodes refs (see _rebuild_index)."""
        keys = {nid: ix.nodes[nid].key or nid for nid in missing}
        try:
            with self.refs_lock():
                current = loaded.refmap()  # re-read: another process may have minted
                need = list(dict.fromkeys(k for k in keys.values() if k not in current))
                if need:
                    first = self.next_refs(len(need))
                    current.update({k: ref_str(first + i) for i, k in enumerate(need)})
                    with _lock_for(os.path.join(loaded.path, CAPTURE_LOCK)):
                        if not loaded.exists():
                            raise loaded._gone()
                        _atomic_write(os.path.join(loaded.path, REFMAP_FILE),
                                      _json_bytes(current), self.durable)
        except OSError:
            return ix  # served as is, and not saved
        mapping = {nid: current[k] for nid, k in keys.items() if k in current}
        out = remap_ids(ix, mapping)
        for ref in mapping.values():
            n = out.nodes[ref]
            n.match, n.since = "new", loaded.id
            if n.sel == n.key:
                n.sel = ref
        return out


def _default_rebuild(raw: RawCapture, refmap: dict, *, contrast: bool = True) -> Index:
    """build_index + apply_refs from capture/index.py (C4), then analyze() from
    capture/analyzers.py (C7) with the capture's own lint option, so a rebuilt
    index carries the same issues, stops and reading order as the published one.
    ``contrast=False`` runs lint "full" as "tree" (the store lock is held).
    Carry-over provenance (match, since, rebound_of) is not in the refmap and is
    not restored."""
    try:
        from .index import apply_refs, build_index
    except ImportError as exc:
        raise RuntimeError("no index builder (inspector_widget.capture.index) available") from exc
    ix = apply_refs(build_index(raw), refmap)
    try:
        from .analyzers import analyze
    except ImportError:  # pragma: no cover - analyzers ship with the index builder
        return ix
    lint = getattr(getattr(raw.meta, "options", None), "lint", "tree") or "tree"
    if lint == "full" and not contrast:
        lint = "tree"
    analyze(ix, raw, lint=lint)
    return ix


__all__ = [
    "DEFAULT_MAX_CAPTURES",
    "DEFAULT_MAX_MB",
    "DEFAULT_TTL_S",
    "ENV_MAX",
    "ENV_MAX_MB",
    "ENV_PERSIST",
    "ENV_TTL",
    "HISTORY_CAP",
    "LINEAGE_CAP",
    "MAX_PINNED",
    "RESERVED_LABELS",
    "TOMB_CAP",
    "CaptureStore",
    "FileLock",
    "LoadedCapture",
    "env_max_bytes",
    "env_max_captures",
    "env_persist",
    "env_ttl_s",
    "new_capture_id",
    "parse_duration",
]
