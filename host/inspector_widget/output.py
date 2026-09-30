"""Output boundary for every tool response: compact JSON, brief slimming, budgets.

Phase 0 of the "Capture and Walk" spec (sections 2 and 10). Pure Python, with no
protobuf and no device I/O. The entry points (``mcp_server._call_tool_text`` and
the CLI's JSON emitters) call, in order::

    brief = output.slim(tool, result, args)          # detail="full" returns result as-is
    text  = output.finalize(tool, brief, max_bytes=args.get("max_bytes"),
                            spill_dir=None)           # compact text, or a spill envelope

and generate their extra parameters from the one ``OUTPUT_PARAMS`` table with
``augment_schemas(TOOLS)`` (MCP) and ``add_cli_flags(subparser, tool)`` (CLI).

Rules applied to every tool:

1. Compact JSON everywhere (``pretty=True`` restores ``indent=2`` for humans).
2. ``detail`` is ``"brief"`` (default) or ``"full"``. ``full`` returns the legacy
   content unchanged.
3. ``max_bytes`` defaults to ``$INSPECTOR_WIDGET_MAX_BYTES`` or 32,000; ``0`` means
   unlimited, and other values are clamped to 1,000..200,000. An over-budget result
   becomes a spill envelope of at most 3,000 bytes, and the full brief result is
   written to a spill file.
4. Omissions are always counted (``omitted``, ``hidden``, ``omitted_defaults``,
   ``hidden_descendants``).
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from . import normalize as nz

DEFAULT_MAX_BYTES = 32000
MIN_MAX_BYTES = 1000
HARD_MAX_BYTES = 200000
ENVELOPE_MAX_BYTES = 3000
FOOTER_RESERVE = 200
SPILL_TTL_S = 3600
PREVIEW_MAX_LINES = 25
PREVIEW_DEPTH = 2
PREVIEW_CHAIN = 8  # members of a single-child chain shown on one preview line
ENV_MAX_BYTES = "INSPECTOR_WIDGET_MAX_BYTES"

#: Tools whose result is a tree (they accept max_depth and root).
TREE_TOOLS = ("dump_tree", "dump_compose", "dump_accessibility", "inspect")

#: MCP tool -> the CLI subcommand that exposes it (AGENTS.md section 5).
CLI_SUBCOMMANDS: dict[str, str] = {
    "list_devices": "devices",
    "list_processes": "packages",
    "attach": "attach",
    "detach": "detach",
    "dump_tree": "dump",
    "get_properties": "get-properties",
    "screenshot": "screenshot",
    "dump_compose": "compose",
    "compose_overlay": "compose",
    "dump_accessibility": "a11y",
    "a11y_lint": "a11y-lint",
    "a11y_overlay": "a11y",
    "inspect": "inspect",
    "inspect_node": "inspect-node",
    "component_image": "component-image",
    "talkback": "talkback",
    "tb_walk": "tb-walk",
    "tb_scenario": "tb-scenario",
}


# --------------------------------------------------------------------------- #
# Encoding and budgets
# --------------------------------------------------------------------------- #
def dumps(obj: Any, pretty: bool = False) -> str:
    """Compact JSON (``ensure_ascii=False``, ``default=str``); ``pretty`` -> indent=2."""
    if pretty:
        return json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)


def utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


def json_cost(obj: Any) -> int:
    """UTF-8 bytes of ``obj`` as compact JSON (a list item costs this plus 1 comma)."""
    return utf8_len(dumps(obj))


def env_max_bytes(env: Mapping[str, str] | None = None) -> int:
    """``$INSPECTOR_WIDGET_MAX_BYTES`` (0 = unlimited), else 32,000."""
    env = os.environ if env is None else env
    raw = (env.get(ENV_MAX_BYTES) or "").strip()
    if not raw:
        return DEFAULT_MAX_BYTES
    try:
        return clamp_max_bytes(int(raw))
    except ValueError:
        return DEFAULT_MAX_BYTES


def clamp_max_bytes(value: int) -> int:
    value = int(value)
    if value <= 0:
        return 0
    return max(MIN_MAX_BYTES, min(HARD_MAX_BYTES, value))


def resolve_max_bytes(value: Any = None) -> int:
    """A tool's ``max_bytes`` argument: None -> env default; 0 -> 0 (unlimited);
    anything else clamped to 1,000..200,000."""
    if value is None or value == "":
        return env_max_bytes()
    return clamp_max_bytes(int(value))


class Budget:
    """UTF-8 byte accounting with a footer reserve.

    ``fits(s)`` asks whether ``s`` still fits while leaving ``reserve`` bytes for
    a footer (truncation notice, cursor, ``next`` hints); ``add(s)`` also books it.
    Callers account for their own separators (a JSON list item costs
    ``json_cost(item) + 1``). ``max_bytes`` of 0 or None means unlimited.
    """

    def __init__(self, max_bytes: int, reserve: int = FOOTER_RESERVE) -> None:
        self.max_bytes = int(max_bytes or 0)
        self.reserve = int(reserve)
        self.used = 0

    @property
    def unlimited(self) -> bool:
        return self.max_bytes <= 0

    @property
    def remaining(self) -> int | None:
        if self.unlimited:
            return None
        return self.max_bytes - self.reserve - self.used

    def cost(self, s: str | int) -> int:
        return s if isinstance(s, int) else utf8_len(s)

    def fits(self, s: str | int) -> bool:
        return self.unlimited or self.used + self.cost(s) + self.reserve <= self.max_bytes

    def add(self, s: str | int) -> bool:
        if not self.fits(s):
            return False
        self.used += self.cost(s)
        return True

    def __repr__(self) -> str:
        return f"Budget(used={self.used}, max_bytes={self.max_bytes}, reserve={self.reserve})"


# --------------------------------------------------------------------------- #
# Spill files and the envelope
# --------------------------------------------------------------------------- #
def default_spill_dir(env: Mapping[str, str] | None = None) -> str:
    """Where spill files go when the caller names no directory.

    ``<store root>/spill``, the root the capture store uses (spec 4.1), unless
    ``$INSPECTOR_WIDGET_CAPTURE_PERSIST`` is off (memory-only mode): then nothing
    may land in the persistent cache, and spill files go to
    ``<system temp>/inspector-widget-<uid>/spill`` instead, a directory private to
    this user (0700, checked to be ours and not a link; otherwise a fresh
    per-process temp directory). Files there are purged after 1 h like any spill.
    Callers holding a store can pass ``store.spill_dir()``, which follows the
    store's own mode."""
    from .capture.model import default_store_root
    from .capture.store import env_persist

    env = os.environ if env is None else env
    if env_persist(env):
        return os.path.join(default_store_root(env), "spill")
    return os.path.join(_private_temp_root(), "spill")


def _private_temp_root() -> str:
    import stat
    import tempfile

    getuid = getattr(os, "getuid", None)
    if getuid is not None:
        path = os.path.join(tempfile.gettempdir(), f"inspector-widget-{getuid()}")
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        except OSError:
            path = ""
        if path:
            try:
                st = os.lstat(path)
                if stat.S_ISDIR(st.st_mode) and st.st_uid == getuid():
                    if stat.S_IMODE(st.st_mode) != 0o700:
                        os.chmod(path, 0o700)
                    return path
            except OSError:
                pass
    global _PROCESS_TEMP
    if _PROCESS_TEMP is None or not os.path.isdir(_PROCESS_TEMP):
        _PROCESS_TEMP = tempfile.mkdtemp(prefix="inspector-widget-")
    return _PROCESS_TEMP


_PROCESS_TEMP: str | None = None


def makedirs_private(path: str) -> None:
    """``os.makedirs`` where every directory it creates is 0700 (``makedirs``'s
    ``mode`` applies to the leaf only, and the umask filters it). Existing
    directories are left alone."""
    path = os.path.abspath(path)
    missing = []
    cur = path
    while not os.path.isdir(cur):
        missing.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for d in reversed(missing):
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            continue
        os.chmod(d, 0o700)


def purge_spill(spill_dir: str, ttl_s: float = SPILL_TTL_S, now: float | None = None) -> int:
    """Delete spill files older than ``ttl_s``; returns how many were removed."""
    now = time.time() if now is None else now
    removed = 0
    try:
        names = os.listdir(spill_dir)
    except OSError:
        return 0
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(spill_dir, name)
        try:
            if now - os.path.getmtime(path) > ttl_s:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    return removed


def write_spill(tool: str, text: str, spill_dir: str, now: float | None = None) -> str:
    """Write ``text`` to ``<spill_dir>/<tool>-<YYYYmmddTHHMMSS>-<4hex>.json`` (0600;
    every directory it creates, the store root included, is 0700) and return the
    path."""
    makedirs_private(spill_dir)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(time.time() if now is None else now))
    safe_tool = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in tool) or "tool"
    for _ in range(8):
        path = os.path.join(spill_dir, f"{safe_tool}-{stamp}-{os.urandom(2).hex()}.json")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        return path
    raise OSError(f"could not create a spill file in {spill_dir}")


def _tree_roots(result: Any) -> list[tuple[Any, dict]]:
    """``[(window id or None, root node)]`` for the known legacy tree shapes."""
    if not isinstance(result, Mapping):
        return []
    out: list[tuple[Any, dict]] = []
    for r in result.get("roots") or []:
        if isinstance(r, Mapping):
            out.append((None, r))
    for w in result.get("windows") or []:
        if isinstance(w, Mapping) and isinstance(w.get("root"), Mapping):
            out.append((w.get("view_id", w.get("root_view_id")), w["root"]))
    return out


def _walk(node: Mapping, depth: int = 0) -> Iterator[tuple[Mapping, int]]:
    stack = [(node, depth)]
    while stack:
        n, d = stack.pop()
        yield n, d
        kids = n.get("children") or []
        for c in reversed(kids):
            if isinstance(c, Mapping):
                stack.append((c, d + 1))


def _count(node: Mapping) -> int:
    return sum(1 for _ in _walk(node))


def summarize(result: Any) -> dict[str, Any]:
    """Counts for the envelope: windows, nodes, facet counts, levels, list lengths."""
    s: dict[str, Any] = {}
    roots = _tree_roots(result)
    if roots:
        nodes = 0
        levels = 0
        facets: Counter = Counter()
        for _, r in roots:
            for n, d in _walk(r):
                nodes += 1
                levels = max(levels, d + 1)
                for f in ("view", "compose", "a11y"):
                    if n.get(f):
                        facets[f] += 1
        s["windows"] = len(roots)
        s["nodes"] = nodes
        for f in ("view", "compose", "a11y"):
            if facets[f]:
                s[f] = facets[f]
        s["max_depth"] = levels
    if isinstance(result, Mapping):
        for k, v in result.items():
            if k in ("roots", "windows", "children") or k == "summary":
                continue
            if isinstance(v, list) or isinstance(v, Mapping) and k in ("properties", "by_rule"):
                s[k] = len(v)
    return s


def _node_line_parts(n: Mapping) -> tuple[str, str, str, str, list[int] | None]:
    """(key, Type, #rid/@tag, label, bounds) for a node of any legacy shape."""
    view = n.get("view") if isinstance(n.get("view"), Mapping) else None
    comp = n.get("compose") if isinstance(n.get("compose"), Mapping) else None
    a11y = n.get("a11y") if isinstance(n.get("a11y"), Mapping) else None
    if "node_key" in n:
        key = str(n["node_key"])
    elif "host_view_id" in n:
        key = f"a11y:{n.get('host_view_id')}:{n.get('virtual_id')}"
    elif "kind" in n or "attrs" in n:
        key = f"compose:{n.get('id')}"
    else:
        key = f"view:{n.get('id')}"
    src = view or n
    attrs = (comp or n).get("attrs") if isinstance((comp or n).get("attrs"), Mapping) else {}
    typ = ""
    if src.get("class_name"):
        typ = str(src["class_name"]).rsplit(".", 1)[-1]
    elif comp or "kind" in n:
        typ = str((comp or n).get("name") or "")
    elif a11y and a11y.get("class_name"):
        typ = str(a11y["class_name"]).rsplit(".", 1)[-1]
    rid = ""
    res = src.get("resource")
    name = src.get("view_id_name") or (res.get("name") if isinstance(res, Mapping) else None)
    if not name and isinstance(res, str) and "/" in res:  # brief inspect: "@pkg:id/name"
        name = res.rsplit("/", 1)[-1]
    if not name and src.get("view_id_resource_name"):
        name = str(src["view_id_resource_name"]).split("/")[-1]
    if name:
        rid = f"#{name}"
    elif attrs.get("TestTag"):
        rid = f"@{attrs['TestTag']}"
    label = (src.get("text") or src.get("speakable") or attrs.get("Text")
             or attrs.get("ContentDescription") or (a11y or {}).get("speakable") or "")
    return key, typ, rid, str(label), nz.rect_list(n.get("bounds"))


def _preview_seg(n: Mapping) -> tuple[str, list[int] | None]:
    key, typ, rid, label, b = _node_line_parts(n)
    parts = [key]
    if typ:
        parts.append(typ)
    if rid:
        parts.append(rid)
    if label:
        parts.append(json.dumps(label if len(label) <= 30 else label[:29] + "…",
                                ensure_ascii=False))
    return " ".join(parts), b


def _preview_kids(n: Mapping) -> list[Mapping]:
    """The children a preview shows: not a zero-size leaf (a ViewStub, an empty
    status-bar scrim), which outline hides too."""
    out = []
    for c in n.get("children") or []:
        if not isinstance(c, Mapping):
            continue
        b = nz.rect_list(c.get("bounds"))
        if b is not None and (b[2] <= 0 or b[3] <= 0) and not c.get("children"):
            continue
        out.append(c)
    return out


def _only_child(n: Mapping) -> Mapping | None:
    kids = _preview_kids(n)
    return kids[0] if len(kids) == 1 else None


def preview_lines(result: Any, max_lines: int = PREVIEW_MAX_LINES,
                  depth: int = PREVIEW_DEPTH) -> list[str]:
    """A generic ``depth``-level outline (at most ``max_lines`` lines) of a tree result.

    ``key Type #rid "label" [x,y wxh] +N``, where N counts the hidden descendants.
    A single-child chain is one line (``view:2 DecorView > view:3 LinearLayout >
    ...``, at most ``PREVIEW_CHAIN`` members) and costs one level, as in outline,
    so the levels shown are the ones that branch; zero-size leaves (ViewStubs) are
    left out.
    """
    lines: list[str] = []
    total = 0

    def walk(n: Mapping, d: int) -> None:
        nonlocal total
        if d >= depth:
            return
        chain = [n]
        while len(chain) < PREVIEW_CHAIN:
            only = _only_child(chain[-1])
            if only is None:
                break
            chain.append(only)
        last = chain[-1]
        total += 1
        if len(lines) < max_lines:
            segs = [_preview_seg(x) for x in chain]
            line = "  " * d + " > ".join(seg for seg, _b in segs)
            b = segs[-1][1]
            if b:
                line += f" [{b[0]},{b[1]} {b[2]}x{b[3]}]"
            hidden = _count(last) - 1 if d == depth - 1 else 0
            if hidden:
                line += f" +{hidden}"
            lines.append(line)
        for c in _preview_kids(last):
            walk(c, d + 1)

    for _, root in _tree_roots(result):
        walk(root, 0)
    if total > len(lines):
        lines[-1:] = [f"  …{total - len(lines) + 1} more lines"] if lines else []
    return lines


def _hint(tool: str, detail: str | None = None) -> str:
    narrow = "Narrow with max_depth=2 or root=<id>, " if tool in TREE_TOOLS else ""
    brief = 'use detail="brief", ' if detail == "full" else ""
    return (f"{narrow}raise max_bytes (<={HARD_MAX_BYTES}), {brief}or read "
            "spill_path with jq.")


def envelope(tool: str, result: Any, text: str, max_bytes: int, spill_path: str | None,
             preview: list[str] | None = None, error: str | None = None,
             detail: str | None = None) -> dict[str, Any]:
    """The spill envelope (spec 2.5), shrunk until it is at most
    ``min(3000, max_bytes)`` bytes."""
    limit = min(ENVELOPE_MAX_BYTES, max_bytes) if max_bytes > 0 else ENVELOPE_MAX_BYTES
    env: dict[str, Any] = {"truncated": True, "tool": tool, "bytes": utf8_len(text),
                           "max_bytes": max_bytes}
    if isinstance(result, Mapping) and result.get("capture"):
        env["capture"] = result["capture"]
    env["summary"] = summarize(result)
    env["preview"] = list(preview) if preview is not None else preview_lines(result)
    if spill_path:
        env["spill_path"] = spill_path
    if error:
        env["spill_error"] = error
    env["hint"] = _hint(tool, detail)
    if isinstance(result, Mapping) and isinstance(result.get("error"), str):
        env["error"] = result["error"]

    def size() -> int:
        return utf8_len(dumps(env))

    while size() > limit and env["preview"]:
        env["preview"].pop()
    if size() > limit:
        env.pop("summary", None)
    if size() > limit:
        env["hint"] = "Narrow the call or raise max_bytes."
    if size() > limit:
        env.pop("preview", None)
    return env


def finalize(tool: str, result: Any, *, max_bytes: int | None,
             spill_dir: str | None = None, preview: list[str] | None = None,
             pretty: bool = False, now: float | None = None,
             detail: str | None = None) -> str:
    """Encode a (slimmed) result for the wire.

    Returns the compact (or ``pretty``) JSON text when it fits ``max_bytes``
    (``None`` -> env default; ``0`` -> unlimited). Otherwise the result is written
    to a spill file in ``spill_dir`` (default ``<store>/spill``; files older than
    1 h are purged) and the returned text is the spill envelope. The budget is
    always measured on the compact encoding. ``detail`` is the call's (the
    envelope suggests brief only to a call that asked for full).
    """
    limit = resolve_max_bytes(max_bytes)
    text = dumps(result)
    if limit <= 0 or utf8_len(text) <= limit:
        return dumps(result, pretty=True) if pretty else text
    spill_dir = spill_dir or default_spill_dir()
    path = error = None
    try:
        purge_spill(spill_dir, now=now)
        path = write_spill(tool, text, spill_dir, now=now)
    except OSError as exc:
        error = f"{type(exc).__name__}: {exc}"
    env = envelope(tool, result, text, limit, path, preview=preview, error=error,
                   detail=detail)
    return dumps(env, pretty=pretty)


# --------------------------------------------------------------------------- #
# Brief slimming, per tool (spec 2.3)
# --------------------------------------------------------------------------- #
class _Ctx:
    def __init__(self, args: Mapping[str, Any]) -> None:
        md = args.get("max_depth")
        self.max_depth = max(1, int(md)) if md not in (None, "") else None
        self.omitted: Counter = Counter()

    def cut(self, depth: int) -> bool:
        """True when a node at ``depth`` (0-based) must not show its children."""
        return self.max_depth is not None and depth + 1 >= self.max_depth


def _error(tool: str, message: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"error": message, "tool": tool}
    out.update(extra)
    return out


def _root_spec(args: Mapping[str, Any]) -> str | None:
    r = args.get("root")
    if r is None or r == "":
        return None
    return str(r).strip()


def _find(roots: list[tuple[Any, dict]], pred: Callable[[Mapping], bool]
          ) -> list[tuple[Any, dict]]:
    return [(w, n) for w, r in roots for n, _ in _walk(r) if pred(n)]


def _reroot(tool: str, roots: list[tuple[Any, dict]], spec: str | None,
            pred_for: Callable[[str], Callable[[Mapping], bool]]
            ) -> list[tuple[Any, dict]] | dict[str, Any]:
    if spec is None:
        return roots
    hits = _find(roots, pred_for(spec))
    if not hits:
        return _error(tool, f"root {spec!r} not found in the {tool} result",
                      hint="Use an id from the result (see summary/preview), or omit root.")
    if len(hits) > 1:
        return _error(tool, f"root {spec!r} is ambiguous ({len(hits)} nodes match)",
                      hint="Use a more specific id, e.g. a node_key.",
                      candidates=[_node_line_parts(n)[0] for _, n in hits[:5]])
    return hits


def _strip_prefix(spec: str, *prefixes: str) -> str:
    for p in prefixes:
        if spec.startswith(p):
            return spec[len(p):]
    return spec


def _set_children(out: dict, node: Mapping, depth: int, ctx: _Ctx,
                  fn: Callable[[Mapping, int], dict]) -> None:
    kids = [c for c in node.get("children") or [] if isinstance(c, Mapping)]
    if not kids:
        return
    if ctx.cut(depth):
        hidden = sum(_count(c) for c in kids)
        out["hidden_descendants"] = hidden
        ctx.omitted["depth"] += hidden
        return
    out["children"] = [fn(c, depth + 1) for c in kids]


def _finish(out: dict, ctx: _Ctx, extra: Mapping[str, int] | None = None) -> dict:
    omitted = Counter(ctx.omitted)
    if extra:
        omitted.update({k: v for k, v in extra.items() if v})
    omitted = +omitted
    if omitted:
        out["omitted"] = dict(sorted(omitted.items()))
    return out


def _mark_clipped(out: dict, bounds: Any) -> None:
    """strings._bounds_to_dict clamps a negative size to 0 and says ``clipped``;
    the brief ``[x,y,w,h]`` keeps that as ``clipped: true``."""
    if isinstance(bounds, Mapping) and bounds.get("clipped"):
        out["clipped"] = True


# ---- dump_tree ------------------------------------------------------------- #
def _view_pred(spec: str) -> Callable[[Mapping], bool]:
    target = _strip_prefix(spec, "view:", "w:")
    return lambda n: str(n.get("id")) == target


def _brief_view_node(n: Mapping, depth: int, ctx: _Ctx, seen: dict[int, Mapping],
                     parent_layout: Any = None) -> dict:
    """strings.py node shape, briefer: ``bounds`` as [x,y,w,h] (+``render`` quad when
    transformed), no ``qualified_name`` when it is package.class, no legacy
    ``resource.ref``, no ``view_id_name`` when it repeats the resource name, and
    ``layout_resource`` only where it differs from the parent's (children inherit it)."""
    cls = n.get("class_name")
    pkg = n.get("package_name")
    out: dict[str, Any] = {"id": n.get("id"), "class_name": cls}
    if pkg:
        out["package_name"] = pkg
    q = n.get("qualified_name")
    if q and q != (f"{pkg}.{cls}" if pkg else cls):
        out["qualified_name"] = q
    b = nz.rect_list(n.get("bounds"))
    if b is not None:
        out["bounds"] = b
        _mark_clipped(out, n.get("bounds"))
    quad = nz.render_quad(n.get("bounds"))
    if quad:
        out["render"] = quad
    res = n.get("resource")
    if isinstance(res, Mapping):
        out["resource"] = {k: v for k, v in res.items() if k != "ref"}
    lres = n.get("layout_resource")
    layout = {k: v for k, v in lres.items() if k != "ref"} if isinstance(lres, Mapping) else None
    if layout != parent_layout:  # None here means "not inflated from a layout"
        out["layout_resource"] = layout
    vin = n.get("view_id_name")
    if vin and not (isinstance(res, Mapping) and res.get("name") == vin):
        out["view_id_name"] = vin
    if n.get("text"):
        out["text"] = n["text"]
    if n.get("flags"):
        out["flags"] = n["flags"]
    if n.get("id") is not None:
        seen[int(n["id"])] = out
    _set_children(out, n, depth, ctx,
                  lambda c, d: _brief_view_node(c, d, ctx, seen, layout))
    return out


def _prop_groups(props: Any) -> dict[int, list]:
    """Legacy MCP list-of-groups or strings.py ``{view_id: [props]}`` -> {int: [props]}."""
    groups: dict[int, list] = {}
    if isinstance(props, Mapping):
        for vid, plist in props.items():
            try:
                groups[int(vid)] = list(plist or [])
            except (TypeError, ValueError):
                continue
    elif isinstance(props, list):
        for g in props:
            if isinstance(g, Mapping) and g.get("view_id") is not None:
                groups[int(g["view_id"])] = list(g.get("properties") or [])
    return groups


def _nondefault_groups(groups: Mapping[int, list], seen: Mapping[int, Mapping],
                       ctx: _Ctx) -> tuple[dict[str, dict], dict[str, int]]:
    """Non-default property maps for the views shown; the ``id`` property is dropped
    when it repeats the node's resource (counted as ``duplicates``)."""
    shown: dict[int, dict[str, Any]] = {}
    for vid, plist in groups.items():
        if vid not in seen:
            continue
        pmap = nz.props_to_map(plist)
        res = seen[vid].get("resource")
        if "id" in pmap and isinstance(res, Mapping) and pmap["id"] == nz.resource_str(res):
            del pmap["id"]
            ctx.omitted["duplicates"] += 1
        shown[vid] = pmap
    outside = len(groups) - len(shown)
    if outside:
        ctx.omitted["properties_views"] += outside
    classes = {vid: seen[vid].get("qualified_name") or seen[vid].get("class_name") or ""
               for vid in shown}
    bounds = {vid: seen[vid].get("bounds") for vid in shown}
    values, omitted = nz.nondefault_props(shown, classes, bounds=bounds)
    ctx.omitted["defaults"] += sum(omitted.values())
    return ({str(vid): v for vid, v in values.items()},
            {str(vid): n for vid, n in omitted.items()})


def _slim_dump_tree(tool: str, result: Mapping, args: Mapping) -> dict:
    ctx = _Ctx(args)
    roots = _reroot(tool, _tree_roots(result), _root_spec(args), _view_pred)
    if isinstance(roots, dict):
        return roots
    out: dict[str, Any] = {k: v for k, v in result.items()
                           if k not in ("roots", "properties")}
    seen: dict[int, Mapping] = {}
    out["roots"] = [_brief_view_node(n, 0, ctx, seen) for _, n in roots]
    if _root_spec(args) is not None:
        out["root"] = _root_spec(args)
    if result.get("properties"):
        out["properties"], out["omitted_defaults"] = _nondefault_groups(
            _prop_groups(result["properties"]), seen, ctx)
    return _finish(out, ctx)


# ---- get_properties -------------------------------------------------------- #
def _slim_get_properties(tool: str, result: Mapping, args: Mapping) -> dict:
    out: dict[str, Any] = {k: v for k, v in result.items() if k not in ("group", "properties")}
    if "group" in result:
        group = result.get("group") or {}
        plist = group.get("properties") if isinstance(group, Mapping) else None
        if isinstance(group, Mapping) and "view_id" not in out:
            out["view_id"] = group.get("view_id")
    else:
        plist = result.get("properties")
    values = nz.props_to_map(plist)
    if (args.get("filter") or "all") == "nondefault":
        vid = out.get("view_id") or 0
        kept, omitted = nz.nondefault_props({vid: values}, {vid: ""})
        values = kept[vid]
        if omitted[vid]:
            out["omitted"] = {"defaults": omitted[vid]}
    out["properties"] = values
    return out


# ---- dump_compose / compose_overlay ----------------------------------------- #
def _compose_pred(spec: str) -> Callable[[Mapping], bool]:
    target = _strip_prefix(spec, "compose:", "sem:")
    target = target.rsplit(":", 1)[-1]
    return lambda n: str(n.get("id")) == target


def _keep_composable(n: Mapping) -> bool:
    return n.get("kind") == "SEMANTICS" or nz.origin_of(n.get("name"), n.get("source")) == "app"


def _hoist(n: Mapping, counts: Counter) -> list[Mapping]:
    if _keep_composable(n):
        return [n]
    counts["library_composables"] += 1
    out: list[Mapping] = []
    for c in n.get("children") or []:
        if isinstance(c, Mapping):
            out.extend(_hoist(c, counts))
    return out


def _brief_compose_node(n: Mapping, depth: int, ctx: _Ctx, user_only: bool,
                        hidden: Counter, attr_counts: dict[str, int]) -> dict:
    out: dict[str, Any] = {"id": n.get("id"), "name": n.get("name")}
    if n.get("kind") and n.get("kind") != "SEMANTICS":
        out["kind"] = n["kind"]
    b = nz.rect_list(n.get("bounds"))
    if b is not None:
        out["bounds"] = b
        _mark_clipped(out, n.get("bounds"))
    quad = nz.render_quad(n.get("bounds"))
    if quad:
        out["render"] = quad
    if n.get("source"):
        out["source"] = n["source"]
    if n.get("render_node_id"):
        out["render_node_id"] = n["render_node_id"]
    values, actions = nz.compose_attrs_brief(n.get("attrs"), attr_counts)
    if values:
        out["attrs"] = values
    if actions:
        out["actions"] = actions
    kids: list[Mapping] = []
    for c in n.get("children") or []:
        if isinstance(c, Mapping):
            kids.extend(_hoist(c, hidden) if user_only else [c])
    if kids:
        if ctx.cut(depth):
            count = sum(_count_kept(c, user_only) for c in kids)
            out["hidden_descendants"] = count
            ctx.omitted["depth"] += count
        else:
            out["children"] = [_brief_compose_node(c, depth + 1, ctx, user_only, hidden,
                                                   attr_counts) for c in kids]
    return out


def _count_kept(n: Mapping, user_only: bool) -> int:
    if not user_only:
        return _count(n)
    scratch: Counter = Counter()
    total = 0
    for top in _hoist(n, scratch):
        total += 1
        for c in top.get("children") or []:
            if isinstance(c, Mapping):
                total += _count_kept(c, user_only)
    return total


def _truthy(v: Any, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(v)


def _slim_dump_compose(tool: str, result: Mapping, args: Mapping) -> dict:
    ctx = _Ctx(args)
    user_only = _truthy(args.get("user_code_only"), True)
    spec = _root_spec(args)
    windows = [w for w in result.get("windows") or [] if isinstance(w, Mapping)]
    roots = _reroot(tool, [(w.get("view_id"), w["root"]) for w in windows
                           if isinstance(w.get("root"), Mapping)], spec, _compose_pred)
    if isinstance(roots, dict):
        return roots
    hidden: Counter = Counter()
    attr_counts: dict[str, int] = {}
    out: dict[str, Any] = {k: v for k, v in result.items() if k != "windows"}
    new_windows = []
    for view_id, root in roots:
        new_windows.append({"view_id": view_id,
                            "root": _brief_compose_node(root, 0, ctx, user_only, hidden,
                                                        attr_counts)})
    if spec is None:  # keep windows the agent reported without a root
        for w in windows:
            if not isinstance(w.get("root"), Mapping):
                new_windows.append(dict(w))
    else:
        out["root"] = spec
    out["windows"] = new_windows
    if hidden:
        out["hidden"] = dict(hidden)
    return _finish(out, ctx, {"attr_values": attr_counts.get("attrs", 0),
                              "boilerplate_actions": attr_counts.get("actions", 0)})


def _slim_compose_overlay(tool: str, result: Mapping, args: Mapping) -> dict:
    out: dict[str, Any] = {}
    for k, v in result.items():
        if k == "overlay_path" and v == result.get("path"):
            continue
        out[k] = v
    entries = result.get("on_screen")
    if isinstance(entries, list):
        brief = []
        for e in entries[:30]:
            if isinstance(e, Mapping):
                b = {k: v for k, v in e.items() if v is not None and k != "bounds"}
                if e.get("bounds") is not None:
                    b["bounds"] = nz.rect_list(e.get("bounds"))
                brief.append(b)
        out["on_screen"] = brief
        out["on_screen_total"] = len(entries)
    return out


# ---- dump_accessibility ---------------------------------------------------- #
def _a11y_pred(spec: str) -> Callable[[Mapping], bool]:
    """A node's ``node_key`` (``view:<id>``, ``compose:<acv>:<sem>``), else
    ``host:virt`` / ``a11y:host:virt``, else its packed ``id``."""
    target = _strip_prefix(spec, "a11y:")
    if ":" in target:
        host, _, virt = target.partition(":")
        return lambda n: n.get("node_key") == spec or (
            str(n.get("host_view_id")) == host and str(n.get("virtual_id")) == virt)
    return lambda n: str(n.get("id")) == target


def _app_package(result: Mapping, args: Mapping) -> str | None:
    pkg = result.get("package") or args.get("package")
    if pkg:
        return str(pkg)
    counts: Counter = Counter()
    for _, r in _tree_roots(result):
        for n, _d in _walk(r):
            if n.get("package_name"):
                counts[n["package_name"]] += 1
    return counts.most_common(1)[0][0] if counts else None


def _brief_a11y_node(n: Mapping, depth: int, ctx: _Ctx, pkg: str | None,
                     counts: dict[str, int]) -> dict:
    out = nz.a11y_node_brief(n, pkg, counts)
    _set_children(out, n, depth, ctx, lambda c, d: _brief_a11y_node(c, d, ctx, pkg, counts))
    return out


def _slim_dump_accessibility(tool: str, result: Mapping, args: Mapping) -> dict:
    ctx = _Ctx(args)
    spec = _root_spec(args)
    windows = [w for w in result.get("windows") or [] if isinstance(w, Mapping)]
    roots = _reroot(tool, [(w.get("root_view_id"), w["root"]) for w in windows
                           if isinstance(w.get("root"), Mapping)], spec, _a11y_pred)
    if isinstance(roots, dict):
        return roots
    pkg = _app_package(result, args)
    counts: dict[str, int] = {}
    out: dict[str, Any] = {k: v for k, v in result.items()
                           if k not in ("windows", "focus_order")}
    out["windows"] = [{"root_view_id": wid, "root": _brief_a11y_node(r, 0, ctx, pkg, counts)}
                      for wid, r in roots]
    if spec is not None:
        out["root"] = spec
    mode = args.get("focus_order") or "stops"
    order = result.get("focus_order")
    extra = {"boilerplate_actions": counts.get("actions", 0),
             "empty_extras": counts.get("extras", 0), "defaults": counts.get("defaults", 0)}
    if isinstance(order, list):
        if mode == "full":
            out["focus_order"] = order
        elif mode == "none":
            extra["focus_order"] = len(order)
        else:
            stops = [e for e in order if isinstance(e, Mapping) and _is_stop(e)]
            out["focus_order"] = [_brief_stop(e) for e in stops]
            extra["focus_order_non_stops"] = len(order) - len(stops)
    return _finish(out, ctx, extra)


def _is_stop(entry: Mapping) -> bool:
    """a11y.py lists stops only (``is_focus_stop`` appears only with structural
    entries); recordings from before it marked every entry."""
    return bool(entry.get("is_focus_stop", entry.get("order") is not None))


def _brief_stop(entry: Mapping) -> dict:
    """A focus stop as ``{order, key, speak}`` (plus ``unlabeled`` / ``window`` when
    set); the packed ``id`` only when there is no ``key`` (older recordings, which
    call ``speak`` ``speakable``)."""
    out: dict[str, Any] = {"order": entry.get("order")}
    if entry.get("key") is not None:
        out["key"] = entry["key"]
    else:
        out["id"] = entry.get("id")
    for k in ("speak", "speakable", "unlabeled", "window", "covered_by"):
        if k in entry:
            out[k] = entry[k]
    return out


# ---- a11y_lint --------------------------------------------------------------- #
def _finding_node_id(f: Mapping) -> Any:
    """The finding's node_key (what inspect_node takes), else its node id."""
    if f.get("node_key"):
        return f["node_key"]
    node = f.get("node")
    if isinstance(node, Mapping):
        return node.get("id")
    return node if node is not None else f.get("node_id")


def _slim_a11y_lint(tool: str, result: Mapping, args: Mapping) -> dict:
    """Findings grouped by rule (count, message once, 3 node keys); the run's
    ``stats`` and its info-level diagnostics are left out and counted."""
    if (args.get("group_by") or "rule") == "none":
        return dict(result)
    out: dict[str, Any] = {k: v for k, v in result.items()
                           if k not in ("findings", "summary", "stats", "diagnostics")}
    extra: dict[str, int] = {}
    if isinstance(result.get("stats"), Mapping):
        extra["stats"] = len(result["stats"])
    summary = result.get("summary")
    if isinstance(summary, Mapping):
        out["summary"] = {k: v for k, v in summary.items() if k != "by_rule"}
    by_rule: dict[str, dict[str, Any]] = {}
    for f in result.get("findings") or []:
        if not isinstance(f, Mapping):
            continue
        r = by_rule.setdefault(str(f.get("rule")), {"sev": f.get("severity"), "n": 0,
                                                    "msg": f.get("message"), "nodes": []})
        r["n"] += 1
        if len(r["nodes"]) < 3:
            r["nodes"].append(_finding_node_id(f))
    for r in by_rule.values():
        if r["n"] > len(r["nodes"]):
            r["more"] = r["n"] - len(r["nodes"])
    out["by_rule"] = by_rule
    diags = result.get("diagnostics")
    if isinstance(diags, list):
        kept = [d for d in diags if not (isinstance(d, Mapping) and d.get("level") == "info")]
        extra["info_diagnostics"] = len(diags) - len(kept)
        if kept:
            out["diagnostics"] = kept
    elif diags is not None:
        out["diagnostics"] = diags
    return _finish(out, _Ctx({}), extra)


# ---- inspect / inspect_node -------------------------------------------------- #
def _inspect_pred(spec: str) -> Callable[[Mapping], bool]:
    def pred(n: Mapping) -> bool:
        if n.get("node_key") == spec:
            return True
        key = str(n.get("node_key") or "")
        return key.split(":", 1)[-1] == spec
    return pred


def _brief_view_facet(v: Mapping, keep_qualified: bool = False) -> dict:
    out: dict[str, Any] = {"id": v.get("id"), "class_name": v.get("class_name")}
    if keep_qualified and v.get("qualified_name"):
        out["qualified_name"] = v["qualified_name"]
    res = nz.resource_str(v.get("resource")) if v.get("resource") else None
    if res:
        out["resource"] = res
    elif v.get("view_id_name"):
        out["resource"] = v["view_id_name"]
    if v.get("text"):
        out["text"] = v["text"]
    return out


def _brief_compose_facet(c: Mapping, attr_counts: dict[str, int]) -> dict:
    out: dict[str, Any] = {"name": c.get("name")}
    values, actions = nz.compose_attrs_brief(c.get("attrs"), attr_counts)
    if values:
        out["attrs"] = values
    if actions:
        out["actions"] = actions
    if c.get("source"):
        out["src"] = c["source"]
    if c.get("render_node_id"):
        out["render_node_id"] = c["render_node_id"]
    if c.get("kind") and c.get("kind") != "SEMANTICS":
        out["kind"] = c["kind"]
    return out


#: a11y fields ``speakable`` copies (the first one set is what it holds)
_SPEAKABLE_SOURCES = ("content_description", "text", "state_description", "hint_text")


def _brief_a11y_facet(a: Mapping, conf: str | None, pkg: str | None,
                      counts: dict[str, int], node_key: Any = None) -> dict:
    """The a11y facet of an inspect node, without what the node already says: its
    ids when the match is exact, its own ``node_key``, and ``speakable`` when it
    only repeats one of the label fields beside it."""
    out = nz.a11y_node_brief(a, pkg, counts)
    if conf == "exact":
        for k in ("id", "host_view_id", "virtual_id"):
            out.pop(k, None)
    if node_key is not None and out.get("node_key") == node_key:
        del out["node_key"]
    if "speakable" in out and out["speakable"] in [out.get(k) for k in _SPEAKABLE_SOURCES]:
        del out["speakable"]
    return out


def _brief_inspect_node(n: Mapping, depth: int, ctx: _Ctx, pkg: str | None,
                        counts: dict[str, int], props: dict[int, tuple[dict, list]]) -> dict:
    conf = n.get("correlation_confidence")
    out: dict[str, Any] = {"node_key": n.get("node_key"), "bounds": nz.rect_list(n.get("bounds"))}
    if n.get("render_quad"):
        out["render"] = n["render_quad"]
    if isinstance(n.get("view"), Mapping):
        out["view"] = _brief_view_facet(n["view"])
        if n["view"].get("properties") is not None and n["view"].get("id") is not None:
            props[int(n["view"]["id"])] = (out["view"], n["view"]["properties"])
    if isinstance(n.get("compose"), Mapping):
        out["compose"] = _brief_compose_facet(n["compose"], counts)
    if isinstance(n.get("a11y"), Mapping):
        out["a11y"] = _brief_a11y_facet(n["a11y"], conf, pkg, counts, n.get("node_key"))
    if conf and conf != "exact":
        out["conf"] = conf
        iou = n.get("a11y_iou")
        if isinstance(iou, (int, float)) and iou < 1.0:
            out["a11y_iou"] = iou
    _set_children(out, n, depth, ctx,
                  lambda c, d: _brief_inspect_node(c, d, ctx, pkg, counts, props))
    return out


def _attach_nondefault(props: dict[int, tuple[dict, list]], ctx: _Ctx) -> None:
    if not props:
        return
    maps = {vid: nz.props_to_map(plist) for vid, (_, plist) in props.items()}
    classes = {vid: facet.get("class_name") or "" for vid, (facet, _) in props.items()}
    values, omitted = nz.nondefault_props(maps, classes)
    for vid, (facet, _) in props.items():
        facet["properties"] = values[vid]
        facet["omitted_defaults"] = omitted[vid]
        ctx.omitted["defaults"] += omitted[vid]


def _slim_inspect(tool: str, result: Mapping, args: Mapping) -> dict:
    ctx = _Ctx(args)
    spec = _root_spec(args)
    roots = _reroot(tool, _tree_roots(result), spec, _inspect_pred)
    if isinstance(roots, dict):
        return roots
    pkg = _app_package(result, args)
    counts: dict[str, int] = {}
    props: dict[int, tuple[dict, list]] = {}
    out: dict[str, Any] = {k: v for k, v in result.items() if k != "roots"}
    out["roots"] = [_brief_inspect_node(r, 0, ctx, pkg, counts, props) for _, r in roots]
    _attach_nondefault(props, ctx)
    if spec is not None:
        out["root"] = spec
    return _finish(out, ctx, {"attr_values": counts.get("attrs", 0),
                              "boilerplate_actions": counts.get("actions", 0),
                              "empty_extras": counts.get("extras", 0),
                              "a11y_defaults": counts.get("defaults", 0)})


def _slim_inspect_node(tool: str, result: Mapping, args: Mapping) -> dict:
    ctx = _Ctx(args)
    pkg = _app_package(result, args)
    counts: dict[str, int] = {}
    out: dict[str, Any] = {}
    for k, v in result.items():
        if k == "bounds":
            out["bounds"] = nz.rect_list(v)
        elif k == "view" and isinstance(v, Mapping):
            facet = _brief_view_facet(v, keep_qualified=True)
            if v.get("properties") is not None and v.get("id") is not None:
                _attach_nondefault({int(v["id"]): (facet, v["properties"])}, ctx)
            out["view"] = facet
        elif k == "compose" and isinstance(v, Mapping):
            out["compose"] = _brief_compose_facet(v, counts)
        elif k == "a11y" and isinstance(v, Mapping):
            out["a11y"] = nz.a11y_node_brief(v, pkg, counts)
        elif k == "render_quad":
            out["render"] = v
        else:
            out[k] = v
    return _finish(out, ctx, {"attr_values": counts.get("attrs", 0),
                              "boilerplate_actions": counts.get("actions", 0),
                              "empty_extras": counts.get("extras", 0),
                              "a11y_defaults": counts.get("defaults", 0)})


_SLIMMERS: dict[str, Callable[[str, Mapping, Mapping], dict]] = {
    "dump_tree": _slim_dump_tree,
    "get_properties": _slim_get_properties,
    "dump_compose": _slim_dump_compose,
    "compose_overlay": _slim_compose_overlay,
    "dump_accessibility": _slim_dump_accessibility,
    "a11y_lint": _slim_a11y_lint,
    "inspect": _slim_inspect,
    "inspect_node": _slim_inspect_node,
}

#: Tools whose brief output is the legacy output (compact only).
COMPACT_ONLY_TOOLS = ("list_devices", "list_processes", "attach", "detach", "screenshot",
                      "a11y_overlay", "component_image")


def _bad_args(tool: str, args: Mapping[str, Any]) -> dict[str, Any] | None:
    """An error dict when an output parameter has an invalid value, else None."""
    for p in OUTPUT_PARAMS.get(tool, []):
        v = args.get(p.name)
        if v is None or v == "":
            continue
        if p.enum and v not in p.enum:
            return _error(tool, f"{p.name} must be one of {', '.join(p.enum)}, got {v!r}")
        if p.type == "integer":
            try:
                iv = int(v)
            except (TypeError, ValueError):
                return _error(tool, f"{p.name} must be an integer, got {v!r}")
            if p.minimum is not None and iv < p.minimum and p.name != "max_depth":
                return _error(tool, f"{p.name} must be >= {p.minimum}, got {v!r}")
    return None


def slim(tool: str, result: dict, args: dict) -> dict:
    """Apply the Phase-0 brief rules of ``tool`` to its legacy ``result``.

    ``args`` are the tool arguments (``detail``, ``max_depth``, ``root``,
    ``user_code_only``, ``focus_order``, ``group_by``, ``filter``, and ``package``
    as a hint). ``detail="full"`` returns ``result`` itself, unchanged. So do
    error results, the compact-only tools and unknown tools. The input is never
    mutated. An unknown or ambiguous ``root``, or an invalid parameter value,
    yields ``{"error", "tool", "hint"?, "candidates"?}``.
    """
    args = args or {}
    if (args.get("detail") or "brief") == "full":
        return result
    if not isinstance(result, Mapping) or "error" in result:
        return result
    fn = _SLIMMERS.get(tool)
    if fn is None:
        return result
    return _bad_args(tool, args) or fn(tool, result, args)


# --------------------------------------------------------------------------- #
# The parameter table, and MCP / CLI generation from it
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ParamDef:
    """One extra tool parameter. ``default`` may be a callable (evaluated when the
    schema or parser is generated, e.g. max_bytes reads the environment); None
    means "no default" (omitted from the schema, ``None`` on the CLI)."""

    name: str
    type: str  # "string" | "integer" | "boolean" | "number"
    default: Any = None
    enum: tuple[str, ...] | None = None
    minimum: int | None = None
    maximum: int | None = None
    help: str = ""
    cli: str | None = None

    def default_value(self) -> Any:
        return self.default() if callable(self.default) else self.default

    @property
    def flag(self) -> str:
        return self.cli or "--" + self.name.replace("_", "-")

    def json_schema(self) -> dict[str, Any]:
        s: dict[str, Any] = {"type": self.type}
        if self.enum:
            s["enum"] = list(self.enum)
        d = self.default_value()
        if d is not None:
            s["default"] = d
        if self.minimum is not None:
            s["minimum"] = self.minimum
        if self.maximum is not None:
            s["maximum"] = self.maximum
        if self.help:
            s["description"] = self.help
        return s


P_DETAIL = ParamDef("detail", "string", "brief", enum=("brief", "full"),
                    help="full = legacy")
P_MAX_BYTES = ParamDef("max_bytes", "integer", env_max_bytes,
                       help="0 = no cap; excess spills to a file")
P_MAX_DEPTH = ParamDef("max_depth", "integer", None, help="Levels to keep (1 = roots)")
P_ROOT = ParamDef("root", "string", None, help="Node id/key to re-root on")
P_USER_CODE_ONLY = ParamDef("user_code_only", "boolean", True,
                            help="Hide library composables")
P_FOCUS_ORDER = ParamDef("focus_order", "string", "stops", enum=("stops", "full", "none"))
P_GROUP_BY = ParamDef("group_by", "string", "rule", enum=("rule", "none"),
                      help="rule: counts + 3 nodes")
P_FILTER = ParamDef("filter", "string", "all", enum=("all", "nondefault"))

OUTPUT_PARAMS: dict[str, list[ParamDef]] = {
    "dump_tree": [P_DETAIL, P_MAX_BYTES, P_MAX_DEPTH, P_ROOT],
    "get_properties": [P_DETAIL, P_MAX_BYTES, P_FILTER],
    "dump_compose": [P_DETAIL, P_MAX_BYTES, P_MAX_DEPTH, P_ROOT, P_USER_CODE_ONLY],
    "compose_overlay": [P_DETAIL],
    "dump_accessibility": [P_DETAIL, P_MAX_BYTES, P_MAX_DEPTH, P_ROOT, P_FOCUS_ORDER],
    "a11y_lint": [P_DETAIL, P_MAX_BYTES, P_GROUP_BY],
    "inspect": [P_DETAIL, P_MAX_BYTES, P_MAX_DEPTH, P_ROOT],
    "inspect_node": [P_DETAIL],
}

#: CLI-only flag: restores indent=2 for humans (MCP output is always compact).
PRETTY_FLAG = "--pretty"


def augment_schemas(tools: dict) -> None:
    """Add the OUTPUT_PARAMS of each tool to its MCP JSON schema, in place.

    ``tools`` is ``{name: {"schema": {...}, ...}}`` (mcp_server.TOOLS). Existing
    properties are never overwritten, so a second call changes nothing.
    """
    for name, params in OUTPUT_PARAMS.items():
        entry = tools.get(name)
        if not isinstance(entry, dict):
            continue
        schema = entry.setdefault("schema", {"type": "object"})
        props = schema.setdefault("properties", {})
        for p in params:
            props.setdefault(p.name, p.json_schema())


def add_cli_flags(sp: argparse.ArgumentParser, tool: str) -> None:
    """Add the OUTPUT_PARAMS of ``tool`` to a subcommand parser as kebab-case flags
    with the same defaults, plus ``--pretty``. Flags the parser already has are
    skipped, so calling it for two tools that share a subcommand is safe."""
    existing = set(getattr(sp, "_option_string_actions", {}))
    for p in OUTPUT_PARAMS.get(tool, []):
        if p.flag in existing:
            continue
        kw: dict[str, Any] = {"dest": p.name, "default": p.default_value(), "help": p.help}
        if p.type == "boolean":
            if p.default_value():
                kw["action"] = argparse.BooleanOptionalAction
            else:
                kw["action"] = "store_true"
        else:
            kw["type"] = {"integer": int, "number": float}.get(p.type, str)
            if p.enum:
                kw["choices"] = list(p.enum)
            if p.type in ("integer", "number"):
                kw["metavar"] = "N"
            elif not p.enum:
                kw["metavar"] = p.name.upper()
        sp.add_argument(p.flag, **kw)
        existing.add(p.flag)
    if PRETTY_FLAG not in existing:
        sp.add_argument(PRETTY_FLAG, action="store_true", default=False,
                        help="Indent the JSON for humans (the default is compact).")


def tool_args_from_cli(ns: argparse.Namespace, tool: str) -> dict[str, Any]:
    """The OUTPUT_PARAMS values of a parsed CLI namespace, as MCP-style tool args."""
    return {p.name: getattr(ns, p.name) for p in OUTPUT_PARAMS.get(tool, [])
            if hasattr(ns, p.name)}


def full_args(tool: str, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """``args`` completed with the OUTPUT_PARAMS defaults (a deep copy)."""
    out = copy.deepcopy(dict(args or {}))
    for p in OUTPUT_PARAMS.get(tool, []):
        out.setdefault(p.name, p.default_value())
    return out


__all__ = [
    "CLI_SUBCOMMANDS",
    "COMPACT_ONLY_TOOLS",
    "DEFAULT_MAX_BYTES",
    "ENVELOPE_MAX_BYTES",
    "FOOTER_RESERVE",
    "HARD_MAX_BYTES",
    "MIN_MAX_BYTES",
    "OUTPUT_PARAMS",
    "PRETTY_FLAG",
    "SPILL_TTL_S",
    "TREE_TOOLS",
    "Budget",
    "ParamDef",
    "add_cli_flags",
    "augment_schemas",
    "clamp_max_bytes",
    "default_spill_dir",
    "dumps",
    "env_max_bytes",
    "envelope",
    "finalize",
    "full_args",
    "json_cost",
    "preview_lines",
    "purge_spill",
    "resolve_max_bytes",
    "slim",
    "summarize",
    "tool_args_from_cli",
    "utf8_len",
    "write_spill",
]
