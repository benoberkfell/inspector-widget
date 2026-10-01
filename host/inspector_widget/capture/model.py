"""Capture data model: options, metadata, raw facets, the unified node index.

This module is the shared vocabulary of the ``inspector_widget.capture`` package
(spec "Capture and Walk", sections 3 and 10). It is pure Python: no protobuf, no
Pillow, no device I/O, so every other capture module (store, fetch, index, refs,
query, analyzers, diff, images) and their tests can build on it in parallel.

Id spaces (see CONTRACT_NOTES.md in this directory):

* A **canonical key** is derived from device identity: ``view:<udid>``,
  ``sem:<acv>:<id>``, ``slot:<acv>:<hash8>``, ``a11y:<host>:<virtual>`` (or
  ``a11y:path:<root>:<0.i.j>`` when the agent's a11y ids are not unique) and the
  alias ``w:<rootUdid>``. Legacy ``compose:<id>`` is accepted only as a selector.
* A **ref** (``n23``) is the store-global, carried, never-reused handle.
* A node's **id** is its ref once refs are applied, else its key. ``Index.nodes``,
  ``UNode.parent/children/window``, every ``Tree`` and ``Index.reading`` hold ids;
  ``Index.by_key`` maps canonical keys (and aliases) to ids. ``remap_ids`` moves an
  index from one id space to another (``capture.index.apply_refs`` uses it).
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import os
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import MISSING, dataclass, field, fields
from typing import Any

SCHEMA = 1

# --------------------------------------------------------------------------- #
# Vocabularies
# --------------------------------------------------------------------------- #
KINDS = ("view", "compose", "slot", "a11y")

#: The fixed flag vocabulary of UNode.flags (spec section 3.3), in display order.
FLAGS = (
    "click", "longclick", "focus", "focused", "scroll", "checkable", "checked",
    "partial", "selected", "disabled", "heading", "edit", "password", "hidden",
    "live", "tgroup", "webview", "interop",
    # What the agent could not send: children cut at its depth cap, masked text.
    "truncated", "redacted",
)
FLAG_SET = frozenset(FLAGS)

CONF_VALUES = ("exact", "inferred", "none")
FACET_NAMES = ("view", "compose", "a11y", "slot")
FACET_STATUS = ("ok", "off", "unavailable", "unsupported", "error")
SEVERITIES = ("error", "warn", "info")
MATCH_KINDS = ("id", "locator", "structure", "geometry", "new")
SLOTS_POLICIES = ("if_available", "enable", "off")
LINT_MODES = ("tree", "full", "none")
CONSISTENCY = ("settled", "unsettled")
TREE_NAMES = ("ui", "views", "compose", "slots", "a11y")
ORIGINS = ("app", "library")

#: Error codes of spec section 5.1, each with a default hint.
ERROR_CODES: dict[str, str] = {
    "no_session": "Pass serial and package, or attach() first.",
    "device_lost": "The device or app went away; reconnect and capture again.",
    "agent_error": "The on-device agent failed; see `adb logcat -s ViewSpector`.",
    "capture_not_found": "The capture expired, was evicted or never existed; capture again.",
    "ref_not_in_capture": "The ref is not in this capture; capture again or use its sel.",
    "not_found": "Nothing matched; check the nearest labels.",
    "ambiguous": "More than one node matched; use one of the candidates' sel.",
    "bad_selector": "Selectors look like n23, @tag, #rid, Type\"label\" or #feed > \"Item 3\".",
    "bad_args": "Check the argument names and values.",
    "facet_unavailable": "That facet was not captured; capture again with it enabled.",
    "unsupported": "Not supported on this device or agent.",
    # TalkBack (tb_walk, tb_scenario, talkback): docs/design/talkback-navigation.md part 4
    "walk_not_found": "The walk expired or never existed; captures(what=\"walks\") lists them.",
    "talkback_unavailable": "TalkBack is not installed or cannot be turned on on this device.",
    "enable_failed": "TalkBack did not start; check talkback(action=\"status\").",
    "restore_failed": "The accessibility settings were not restored; run talkback(action=\"restore\").",
    "busy": "Another TalkBack walk or scenario holds this device; wait for it.",
    "app_left_foreground": "The app is not in the foreground; open it and retry.",
    "injector_failed": "No key injector reached TalkBack (candidates: what was tried).",
    "keymap_unknown": "TalkBack did not react to its keyboard shortcuts.",
    "focus_unreadable": "The agent could not read accessibility focus.",
    "start_not_found": "No focus stop matches start / target; pass a ref or the label as spoken.",
}


class OpError(Exception):
    """A structured, agent-facing error: ``{"error": {code, message, hint, candidates?}}``."""

    def __init__(self, code: str, message: str, hint: str | None = None,
                 candidates: list | None = None) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown OpError code {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.candidates = candidates

    def to_dict(self) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message, "hint": self.hint}
        if self.candidates is not None:
            err["candidates"] = list(self.candidates)
        return {"error": err}

    def __repr__(self) -> str:
        return f"OpError({self.code!r}, {self.message!r})"


# --------------------------------------------------------------------------- #
# Canonical keys, refs, capture ids, labels
# --------------------------------------------------------------------------- #
_INT = r"-?\d+"
KEY_RE = re.compile(
    rf"^(?:view:{_INT}|sem:{_INT}:{_INT}|slot:{_INT}:[0-9a-f]{{8}}|a11y:{_INT}:{_INT}"
    rf"|a11y:path:{_INT}:\d+(?:\.\d+)*|w:{_INT}|compose:{_INT})$"
)
REF_RE = re.compile(r"^n[1-9][0-9]*$")

#: Crockford base32 (no i, l, o, u), lower case. Capture ids are "c" + 5 of these.
CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"
CAPTURE_ID_RE = re.compile(r"^c[0-9abcdefghjkmnpqrstvwxyz]{5}$", re.IGNORECASE)
LABEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def view_key(udid: int) -> str:
    return f"view:{int(udid)}"


def sem_key(acv: int, sem_id: int) -> str:
    """Composite semantics key; distinct across ComposeViews (fixes ID3 host-side)."""
    return f"sem:{int(acv)}:{int(sem_id)}"


def anchor_hash(anchor: str) -> str:
    """blake2s-32 of an anchor string, as 8 hex chars (slot keys)."""
    return hashlib.blake2s(anchor.encode("utf-8"), digest_size=4).hexdigest()


def slot_key(acv: int, anchor: str) -> str:
    """Slot groups arrive with id 0, so they are keyed by their anchor's hash."""
    return f"slot:{int(acv)}:{anchor_hash(anchor)}"


def a11y_key(host: int, virtual: int) -> str:
    return f"a11y:{int(host)}:{int(virtual)}"


def a11y_path_key(root: int, path: Sequence[int]) -> str:
    """Fallback a11y key when (host, virtual) pairs are not unique (the ID1 detector).

    ``path`` is the child-index path from the window's a11y root, starting with 0
    for the root itself: the root is ``0``, its third child ``0.2``.
    """
    if not path:
        raise ValueError("a11y path must start with 0 for the root")
    return f"a11y:path:{int(root)}:{'.'.join(str(int(i)) for i in path)}"


def window_key(root_udid: int) -> str:
    """Alias of a window's root View (resolves to the same node as view:<udid>)."""
    return f"w:{int(root_udid)}"


def compose_legacy_key(sem_id: int) -> str:
    return f"compose:{int(sem_id)}"


def is_key(s: Any) -> bool:
    return isinstance(s, str) and KEY_RE.match(s) is not None


def parse_key(key: str) -> tuple[str, tuple] | None:
    """Split a canonical key into ``(kind, parts)``; None if it is not a key.

    kinds: ``view (udid,)``, ``sem (acv, id)``, ``slot (acv, hash8)``,
    ``a11y (host, virtual)``, ``a11y_path (root, (0, i, ...))``, ``w (udid,)``,
    ``compose (id,)``.
    """
    if not is_key(key):
        return None
    head, _, rest = key.partition(":")
    if head == "view":
        return "view", (int(rest),)
    if head == "w":
        return "w", (int(rest),)
    if head == "compose":
        return "compose", (int(rest),)
    if head == "sem":
        a, b = rest.split(":")
        return "sem", (int(a), int(b))
    if head == "slot":
        a, b = rest.split(":")
        return "slot", (int(a), b)
    if rest.startswith("path:"):
        _, root, path = rest.split(":")
        return "a11y_path", (int(root), tuple(int(i) for i in path.split(".")))
    a, b = rest.split(":")
    return "a11y", (int(a), int(b))


def ref_str(n: int) -> str:
    if int(n) < 1:
        raise ValueError(f"refs start at n1, got {n}")
    return f"n{int(n)}"


def ref_num(ref: str) -> int:
    if not is_ref(ref):
        raise ValueError(f"not a ref: {ref!r}")
    return int(ref[1:])


def is_ref(s: Any) -> bool:
    return isinstance(s, str) and REF_RE.match(s) is not None


def is_capture_id(s: Any) -> bool:
    return isinstance(s, str) and CAPTURE_ID_RE.match(s) is not None


def normalize_capture_id(s: str) -> str:
    """Capture ids match case-insensitively; the canonical form is lower case."""
    return s.lower()


def is_valid_label(s: Any) -> bool:
    """Labels are ``^[a-z][a-z0-9_-]{0,31}$`` and must not look like a capture id."""
    return isinstance(s, str) and LABEL_RE.match(s) is not None and not is_capture_id(s)


# --------------------------------------------------------------------------- #
# Store location and on-disk layout (shared by store.py, fetch.py and output.py)
# --------------------------------------------------------------------------- #
META_FILE = "meta.json"
REFMAP_FILE = "refmap.json"
INDEX_FILE = "index.jsonl.gz"
COMPLETE_MARKER = ".complete"
USED_MARKER = ".used"

#: RawCapture attribute -> file path inside a capture directory.
RAW_FILES: dict[str, str] = {
    "windows": "raw/windows.pb",
    "views": "raw/views.pb",
    "compose_sem": "raw/compose_sem.pb",
    "slots": "raw/slots.pb",
    "a11y": "raw/a11y.pb",
    "a11y_render": "raw/a11y_render.pb",
}
_SHOT_RE = re.compile(r"^shot/w_(-?\d+)\.pb$")
_SKP_RE = re.compile(r"^raw/skp_(-?\d+)\.bin$")


def shot_file(root_udid: int) -> str:
    """One ``Screenshot`` message per window root, stored still deflated."""
    return f"shot/w_{int(root_udid)}.pb"


def skp_file(root_udid: int) -> str:
    return f"raw/skp_{int(root_udid)}.bin"


def default_store_root(env: Mapping[str, str] | None = None,
                       platform: str | None = None,
                       home: str | None = None) -> str:
    """Store root (spec 4.1): $INSPECTOR_WIDGET_CAPTURE_DIR, then
    $XDG_CACHE_HOME/inspector-widget, then the platform cache directory
    (macOS ~/Library/Caches/inspector-widget, else ~/.cache/inspector-widget)."""
    env = os.environ if env is None else env
    explicit = env.get("INSPECTOR_WIDGET_CAPTURE_DIR")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    xdg = env.get("XDG_CACHE_HOME")
    if xdg:
        return os.path.join(os.path.expanduser(xdg), "inspector-widget")
    home = home or os.path.expanduser("~")
    if (platform or sys.platform) == "darwin":
        return os.path.join(home, "Library", "Caches", "inspector-widget")
    return os.path.join(home, ".cache", "inspector-widget")


# --------------------------------------------------------------------------- #
# Small serialization helpers
# --------------------------------------------------------------------------- #
def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _is_default(value: Any, default: Any) -> bool:
    return value == default and type(value) is type(default)


def _field_default(f: Any) -> Any:
    if f.default_factory is not MISSING:
        return f.default_factory()
    return f.default


# --------------------------------------------------------------------------- #
# Options, meta, raw facets
# --------------------------------------------------------------------------- #
@dataclass
class CaptureOptions:
    """The capture() arguments that shape what is fetched (stored in meta.options)."""

    props: bool = True
    resolution_stack: bool = False
    slots: str = "if_available"
    screenshot: bool = True
    screenshot_scale: float = 1.0
    skp: bool = False
    a11y_rendering: bool = False
    lint: str = "tree"
    settle_ms: int = 0

    def validate(self) -> CaptureOptions:
        if self.slots not in SLOTS_POLICIES:
            raise OpError("bad_args", f"slots must be one of {', '.join(SLOTS_POLICIES)}")
        if self.lint not in LINT_MODES:
            raise OpError("bad_args", f"lint must be one of {', '.join(LINT_MODES)}")
        if not (0.0 < float(self.screenshot_scale) <= 1.0):
            raise OpError("bad_args", "screenshot_scale must be in (0, 1]")
        if int(self.settle_ms) < 0:
            raise OpError("bad_args", "settle_ms must be >= 0")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> CaptureOptions:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in names})


@dataclass
class CaptureMeta:
    """``meta.json``: identity, target, device, timing, options, facet status,
    consistency, lineage links and flags of one capture (spec 3.1)."""

    id: str
    lineage: tuple[str, str]  # (serial, package)
    pid: int | None = None
    api: int | None = None
    abi: str | None = None
    agent_version: str | None = None
    device: dict[str, Any] = field(default_factory=dict)  # dpi, font_scale, screen [w,h], orientation
    created_at: float = 0.0
    took_ms: int = 0
    options: CaptureOptions = field(default_factory=CaptureOptions)
    facets: dict[str, dict[str, Any]] = field(default_factory=dict)  # name -> {status, reason?, ms, bytes}
    fingerprint: str | None = None
    consistency: str = "settled"
    compose_generation: int = 0
    label: str | None = None
    pinned: bool = False
    prev: str | None = None
    diagnostics: list[str] = field(default_factory=list)
    schema: int = SCHEMA
    agent_build: str | None = None

    def __post_init__(self) -> None:
        self.lineage = (str(self.lineage[0]), str(self.lineage[1]))
        if isinstance(self.options, Mapping):
            self.options = CaptureOptions.from_dict(self.options)

    @property
    def serial(self) -> str:
        return self.lineage[0]

    @property
    def package(self) -> str:
        return self.lineage[1]

    def set_facet(self, name: str, status: str, *, reason: str | None = None,
                  ms: int = 0, nbytes: int = 0) -> None:
        if status not in FACET_STATUS:
            raise ValueError(f"facet status must be one of {FACET_STATUS}, got {status!r}")
        entry: dict[str, Any] = {"status": status, "ms": int(ms), "bytes": int(nbytes)}
        if reason:
            entry["reason"] = reason
        self.facets[name] = entry

    def facet_status(self, name: str) -> str | None:
        entry = self.facets.get(name)
        return entry.get("status") if entry else None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if f.name == "lineage":
                v = {"serial": self.lineage[0], "package": self.lineage[1]}
            elif f.name == "options":
                v = self.options.to_dict()
            out[f.name] = v
        return out

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> CaptureMeta:
        names = {f.name for f in fields(cls)}
        kw = {k: v for k, v in d.items() if k in names}
        lin = kw.get("lineage")
        if isinstance(lin, Mapping):
            kw["lineage"] = (lin.get("serial"), lin.get("package"))
        kw["options"] = CaptureOptions.from_dict(kw.get("options"))
        return cls(**kw)

    def to_json(self) -> bytes:
        return _dumps(self.to_dict()).encode("utf-8")

    @classmethod
    def from_json(cls, b: bytes) -> CaptureMeta:
        return cls.from_dict(json.loads(b.decode("utf-8") if isinstance(b, bytes) else b))


def meta_to_json(meta: CaptureMeta) -> bytes:
    return meta.to_json()


def meta_from_json(b: bytes) -> CaptureMeta:
    return CaptureMeta.from_json(b)


@dataclass
class RawCapture:
    """The source of truth of a capture: verbatim protobuf response bytes.

    ``windows`` GetWindowsResponse, ``views`` DumpTreeResponse (with PropertyGroups;
    its screenshot moved to ``shots``), ``compose_sem`` / ``slots``
    DumpComposeResponse, ``a11y`` / ``a11y_render`` DumpA11yResponse, ``shots``
    {window root udid: Screenshot bytes (still deflated)}, ``skp`` {root udid: SKP}.
    An unfetched facet is ``b""`` (or None for the optional ones); meta.facets says why.
    """

    meta: CaptureMeta
    windows: bytes = b""
    views: bytes = b""
    compose_sem: bytes = b""
    slots: bytes | None = None
    a11y: bytes = b""
    a11y_render: bytes | None = None
    shots: dict[int, bytes] = field(default_factory=dict)
    skp: dict[int, bytes] = field(default_factory=dict)

    def files(self) -> dict[str, bytes]:
        """``{relative path: bytes}`` of every present facet (meta.json excluded)."""
        out: dict[str, bytes] = {}
        for attr, rel in RAW_FILES.items():
            data = getattr(self, attr)
            if data:
                out[rel] = bytes(data)
        for root, data in self.shots.items():
            out[shot_file(root)] = bytes(data)
        for root, data in self.skp.items():
            out[skp_file(root)] = bytes(data)
        return out

    @classmethod
    def from_files(cls, meta: CaptureMeta, files: Mapping[str, bytes]) -> RawCapture:
        raw = cls(meta=meta)
        by_path = {rel: attr for attr, rel in RAW_FILES.items()}
        for rel, data in files.items():
            if rel in by_path:
                setattr(raw, by_path[rel], bytes(data))
                continue
            m = _SHOT_RE.match(rel)
            if m:
                raw.shots[int(m.group(1))] = bytes(data)
                continue
            m = _SKP_RE.match(rel)
            if m:
                raw.skp[int(m.group(1))] = bytes(data)
        return raw

    def nbytes(self) -> int:
        return sum(len(v) for v in self.files().values())


# --------------------------------------------------------------------------- #
# Unified node, trees, index
# --------------------------------------------------------------------------- #
@dataclass
class Issue:
    """One finding on a node. Messages live once in the rule catalog (capture/rules.py)."""

    id: str
    sev: str = "warn"
    evidence: dict[str, Any] = field(default_factory=dict)
    conf: str = "exact"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "sev": self.sev}
        if self.evidence:
            out["evidence"] = self.evidence
        if self.conf != "exact":
            out["conf"] = self.conf
        return out

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Issue:
        return cls(id=d["id"], sev=d.get("sev", "warn"), evidence=dict(d.get("evidence") or {}),
                   conf=d.get("conf", "exact"))


#: Facet fields that hold node ids (rewritten by remap_ids). Put any new
#: node-to-node link in a facet under one of these names, or extend the table.
REF_FIELDS: dict[str, tuple[str, ...]] = {
    "a11y": ("labeled_by", "label_for", "traversal", "traversal_before", "traversal_after"),
    "compose": ("slots",),
    "slot": ("sem",),
}
#: Issue evidence fields that hold node ids (rewritten by remap_ids too), so the
#: analyzers can run on a key-space index before refs are assigned.
EVIDENCE_REF_FIELDS: tuple[str, ...] = ("clipped_by", "children_ids", "node_ids")


@dataclass
class UNode:
    """One logical node (spec 3.3); one line of ``index.jsonl.gz``.

    ``parent``/``children``/``depth`` are the node's place in its primary tree:
    ``ui`` for view, compose and a11y nodes, ``slots`` for slot nodes. Window roots
    are View nodes with ``z`` set (their z order); ``window`` is the id of the
    window root the node belongs to (a window root's ``window`` is itself).
    """

    key: str
    kind: str = "view"
    ref: str | None = None
    window: str | None = None
    z: int | None = None
    parent: str | None = None
    children: list[str] = field(default_factory=list)
    depth: int = 0
    type: str | None = None
    ids: dict[str, Any] = field(default_factory=dict)
    rid: str | None = None
    tag: str | None = None
    label: str | None = None
    text: str | None = None
    desc: str | None = None
    state: str | None = None
    hint: str | None = None
    role: str | None = None
    b: list[int] | None = None
    declared_b: list[int] | None = None
    visible: float | None = None
    flags: list[str] = field(default_factory=list)
    stop: int | None = None
    src: str | None = None
    origin: str | None = None
    facets: dict[str, dict[str, Any]] = field(default_factory=dict)
    conf: dict[str, str] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    match: str | None = None
    since: str | None = None
    rebound_of: str | None = None
    anchor: str | None = None
    sel: str | None = None
    render: dict[str, Any] | None = None  # reserved: RENDER_DESIGN facet
    fragment: str | None = None  # reserved
    adapter_pos: int | None = None  # reserved

    @property
    def id(self) -> str:
        """The node's id in its Index: the ref once assigned, else the canonical key."""
        return self.ref or self.key

    @property
    def is_window(self) -> bool:
        return self.z is not None

    def has_flag(self, flag: str) -> bool:
        return flag in self.flags

    def to_dict(self) -> dict[str, Any]:
        """Compact dict: fields equal to their default are omitted (lossless)."""
        out: dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if f.name == "key":
                out["key"] = v
                continue
            if _is_default(v, _field_default(f)):
                continue
            if f.name == "issues":
                v = [i.to_dict() for i in v]
            out[f.name] = v
        return out

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> UNode:
        names = {f.name for f in fields(cls)}
        kw = {k: v for k, v in d.items() if k in names}
        if "issues" in kw:
            kw["issues"] = [i if isinstance(i, Issue) else Issue.from_dict(i) for i in kw["issues"]]
        return cls(**kw)


@dataclass
class Tree:
    """One view over the node set: ordered roots plus ordered child lists (ids)."""

    roots: list[str] = field(default_factory=list)
    children: dict[str, list[str]] = field(default_factory=dict)

    def walk(self, root: str | None = None, max_depth: int | None = None
             ) -> Iterator[tuple[str, int]]:
        """Pre-order ``(id, depth)``; ``max_depth`` counts levels below the start (0 = start only)."""
        stack = [(r, 0) for r in reversed([root] if root is not None else self.roots)]
        while stack:
            nid, depth = stack.pop()
            yield nid, depth
            if max_depth is not None and depth >= max_depth:
                continue
            for c in reversed(self.children.get(nid, ())):
                stack.append((c, depth + 1))

    def parents(self) -> dict[str, str]:
        return {c: p for p, kids in self.children.items() for c in kids}

    def to_dict(self) -> dict[str, Any]:
        return {"roots": list(self.roots), "children": {k: list(v) for k, v in self.children.items() if v}}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> Tree:
        d = d or {}
        return cls(roots=list(d.get("roots") or []),
                   children={k: list(v) for k, v in (d.get("children") or {}).items()})


@dataclass
class Index:
    """The unified, immutable-by-convention index of one capture."""

    meta: CaptureMeta | None = None
    nodes: dict[str, UNode] = field(default_factory=dict)  # id -> node, in pre-order
    trees: dict[str, Tree] = field(default_factory=dict)  # ui, views, compose, slots, a11y
    reading: list[str] = field(default_factory=list)  # TalkBack stops, in order
    by_key: dict[str, str] = field(default_factory=dict)  # canonical key or alias -> id
    diagnostics: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.nodes)

    def resolve_id(self, ident: str) -> str | None:
        """An id, canonical key or alias -> the node id (None if unknown)."""
        if ident in self.nodes:
            return ident
        return self.by_key.get(ident)

    def get(self, ident: str) -> UNode | None:
        nid = self.resolve_id(ident)
        return self.nodes.get(nid) if nid is not None else None

    def tree(self, name: str = "ui") -> Tree:
        return self.trees.get(name) or Tree()

    def children_of(self, ident: str, tree: str = "ui") -> list[UNode]:
        nid = self.resolve_id(ident)
        if nid is None:
            return []
        return [self.nodes[c] for c in self.tree(tree).children.get(nid, ()) if c in self.nodes]

    def walk(self, tree: str = "ui", root: str | None = None,
             max_depth: int | None = None) -> Iterator[tuple[UNode, int]]:
        """Pre-order ``(node, depth)`` over one of the trees."""
        start = self.resolve_id(root) if root is not None else None
        if root is not None and start is None:
            return
        for nid, depth in self.tree(tree).walk(start, max_depth):
            node = self.nodes.get(nid)
            if node is not None:
                yield node, depth

    def windows(self) -> list[UNode]:
        """Window roots in z order."""
        wins = [n for n in self.nodes.values() if n.is_window]
        return sorted(wins, key=lambda n: (n.z, n.id))

    def ancestors(self, ident: str) -> list[UNode]:
        """Parents from the nearest upwards (primary tree of the node)."""
        out: list[UNode] = []
        node = self.get(ident)
        seen = set()
        while node is not None and node.parent and node.parent not in seen:
            seen.add(node.parent)
            node = self.nodes.get(node.parent)
            if node is not None:
                out.append(node)
        return out

    def rebuild_by_key(self) -> None:
        """Re-derive canonical keys (keeps existing aliases that still resolve)."""
        aliases = {k: v for k, v in self.by_key.items() if v in self.nodes}
        aliases.update({n.key: nid for nid, n in self.nodes.items()})
        self.by_key = aliases


def remap_ids(ix: Index, mapping: Mapping[str, str], *, set_refs: bool = True) -> Index:
    """Return a copy of ``ix`` (sharing no mutable state with it) with node ids
    rewritten through ``mapping``.

    Ids missing from ``mapping`` stay as they are. Rewrites ``Index.nodes`` keys,
    ``UNode.parent/children/window``, the REF_FIELDS facet links, the
    EVIDENCE_REF_FIELDS of every issue, every Tree,
    ``Index.reading`` and ``Index.by_key`` values. With ``set_refs`` a node whose
    new id is a ref gets ``UNode.ref`` set to it. ``rebound_of`` is left alone
    (it names a ref of an earlier capture).
    """
    def m(x: str | None) -> str | None:
        return mapping.get(x, x) if x is not None else None

    def m_value(v: Any) -> Any:
        if isinstance(v, str):
            return m(v)
        if isinstance(v, list):
            return [m_value(x) for x in v]
        return v

    new_nodes: dict[str, UNode] = {}
    for nid, node in ix.nodes.items():
        kw = {f.name: getattr(node, f.name) for f in fields(node)}
        n2 = UNode(**kw)
        new_id = m(nid)
        if set_refs and is_ref(new_id):
            n2.ref = new_id
        n2.parent = m(node.parent)
        n2.window = m(node.window)
        n2.children = [m(c) for c in node.children]
        facets = {}
        for fname, facet in node.facets.items():
            facet2 = dict(facet)
            for link in REF_FIELDS.get(fname, ()):
                if link in facet2:
                    facet2[link] = m_value(facet2[link])
            facets[fname] = facet2
        n2.facets = copy.deepcopy(facets)
        n2.ids = dict(node.ids)
        n2.conf = dict(node.conf)
        n2.flags = list(node.flags)
        n2.issues = []
        for i in node.issues:
            ev = copy.deepcopy(i.evidence)
            for link in EVIDENCE_REF_FIELDS:
                if link in ev:
                    ev[link] = m_value(ev[link])
            n2.issues.append(Issue(i.id, i.sev, ev, i.conf))
        for name in ("b", "declared_b"):
            if getattr(node, name) is not None:
                setattr(n2, name, list(getattr(node, name)))
        new_nodes[new_id] = n2
    trees = {name: Tree(roots=[m(r) for r in t.roots],
                        children={m(p): [m(c) for c in kids] for p, kids in t.children.items()})
             for name, t in ix.trees.items()}
    return Index(meta=ix.meta, nodes=new_nodes, trees=trees,
                 reading=[m(r) for r in ix.reading],
                 by_key={k: m(v) for k, v in ix.by_key.items()},
                 diagnostics=list(ix.diagnostics))


# --------------------------------------------------------------------------- #
# index.jsonl(.gz)
# --------------------------------------------------------------------------- #
def index_to_jsonl(ix: Index, compress: bool = False) -> bytes:
    """Serialize an Index: a header line, then one node per line (pre-order).

    Header: ``{"index": SCHEMA, "capture", "count", "trees", "reading",
    "diagnostics", "aliases"}`` where ``aliases`` are the by_key entries that are
    not a node's own canonical key. ``compress=True`` gzips the result
    (``index.jsonl.gz``); ``index_from_jsonl`` accepts either form.
    """
    own = {n.key for n in ix.nodes.values()}
    header = {
        "index": SCHEMA,
        "capture": ix.meta.id if ix.meta is not None else None,
        "count": len(ix.nodes),
        "trees": {name: t.to_dict() for name, t in ix.trees.items()},
        "reading": list(ix.reading),
        "diagnostics": list(ix.diagnostics),
        "aliases": {k: v for k, v in ix.by_key.items() if k not in own},
    }
    lines = [_dumps(header)]
    lines.extend(_dumps(n.to_dict()) for n in ix.nodes.values())
    data = ("\n".join(lines) + "\n").encode("utf-8")
    return gzip.compress(data, compresslevel=6, mtime=0) if compress else data


def index_from_jsonl(b: bytes, meta: CaptureMeta | None) -> Index:
    """Inverse of index_to_jsonl. Raises ValueError on a schema mismatch or a
    truncated file, so the store can rebuild the index from the raw facets."""
    if b[:2] == b"\x1f\x8b":
        b = gzip.decompress(b)
    lines = [ln for ln in b.decode("utf-8").split("\n") if ln]
    if not lines:
        raise ValueError("empty index")
    header = json.loads(lines[0])
    if header.get("index") != SCHEMA:
        raise ValueError(f"index schema {header.get('index')!r} != {SCHEMA}")
    nodes: dict[str, UNode] = {}
    for ln in lines[1:]:
        node = UNode.from_dict(json.loads(ln))
        nodes[node.id] = node
    if header.get("count") != len(nodes):
        raise ValueError(f"index truncated: {len(nodes)} of {header.get('count')} nodes")
    by_key = {n.key: nid for nid, n in nodes.items()}
    by_key.update(header.get("aliases") or {})
    return Index(meta=meta, nodes=nodes,
                 trees={k: Tree.from_dict(v) for k, v in (header.get("trees") or {}).items()},
                 reading=list(header.get("reading") or []), by_key=by_key,
                 diagnostics=list(header.get("diagnostics") or []))


# --------------------------------------------------------------------------- #
# Lineage state (lineages/<serial>__<package>.json)
# --------------------------------------------------------------------------- #
@dataclass
class LineageState:
    """Per-lineage bookkeeping. ``history`` is newest first (at most 50 ids);
    ``labels`` maps label -> capture id; ``tomb`` maps a retired ref to
    ``[type, label<=40, sel, last_capture]`` (capped at 5,000, LRU)."""

    latest: str | None = None
    history: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    tomb: dict[str, list] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"latest": self.latest, "history": list(self.history),
                "labels": dict(self.labels), "tomb": dict(self.tomb)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> LineageState:
        d = d or {}
        return cls(latest=d.get("latest"), history=list(d.get("history") or []),
                   labels=dict(d.get("labels") or {}), tomb=dict(d.get("tomb") or {}))


def lineage_file_name(serial: str, package: str) -> str:
    """``<serial>__<package>-<hash8>.json``: path-hostile characters replaced, plus
    8 hex chars of a hash of the exact serial and package, so lineages that
    sanitize alike (``192.168.1.7:5555`` and ``192.168.1.7_5555``) or differ only
    in case (``com.Slack``, ``com.slack`` on a case-insensitive disk) never share
    a file."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", f"{serial}__{package}")
    exact = f"{serial}\x00{package}".encode()
    return f"{safe}-{hashlib.blake2s(exact, digest_size=4).hexdigest()}.json"


__all__ = [
    "CAPTURE_ID_RE",
    "COMPLETE_MARKER",
    "CONF_VALUES",
    "CONSISTENCY",
    "CROCKFORD",
    "ERROR_CODES",
    "EVIDENCE_REF_FIELDS",
    "FACET_NAMES",
    "FACET_STATUS",
    "FLAGS",
    "FLAG_SET",
    "INDEX_FILE",
    "KEY_RE",
    "KINDS",
    "LABEL_RE",
    "LINT_MODES",
    "MATCH_KINDS",
    "META_FILE",
    "ORIGINS",
    "RAW_FILES",
    "REFMAP_FILE",
    "REF_FIELDS",
    "REF_RE",
    "SCHEMA",
    "SEVERITIES",
    "SLOTS_POLICIES",
    "TREE_NAMES",
    "USED_MARKER",
    "CaptureMeta",
    "CaptureOptions",
    "Index",
    "Issue",
    "LineageState",
    "OpError",
    "RawCapture",
    "Tree",
    "UNode",
    "a11y_key",
    "a11y_path_key",
    "anchor_hash",
    "compose_legacy_key",
    "default_store_root",
    "index_from_jsonl",
    "index_to_jsonl",
    "is_capture_id",
    "is_key",
    "is_ref",
    "is_valid_label",
    "lineage_file_name",
    "meta_from_json",
    "meta_to_json",
    "normalize_capture_id",
    "parse_key",
    "ref_num",
    "ref_str",
    "remap_ids",
    "sem_key",
    "shot_file",
    "skp_file",
    "slot_key",
    "view_key",
    "window_key",
]
