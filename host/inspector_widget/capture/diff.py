"""Diff two captures of one lineage, node by node, by ref.

Spec "Capture and Walk", section 5.10 (contract in section 10). Carry-over
(``refs.assign``) already did the hard matching, so a node present in both
captures has the same ref in both. ``diff`` compares those nodes field by field
and classifies the rest:

* **changed**: text or label, state flags (checked, selected, disabled...),
  visibility, a11y (speakable, role, actions, behaviour flags, reading stop),
  bounds that move or resize by ``min_move_px`` or more, and, when both captures
  have them, View properties and composable params.
* **moved**: re-parented, or reordered among the siblings both captures share
  (an insertion or removal around a node is not a move).
* **added** / **removed**: refs in only one capture. A subtree collapses onto its
  top node with ``+N`` descendants.
* **rebound**: a recycled cell. The new node carries ``rebound_of`` naming the old
  ref, and the pair is reported once instead of as a removal plus an addition.

Lines use the grammar-v1 prefixes: ``~`` changed (and rebound), ``+`` added,
``-`` removed, ``>`` moved. A node's first line names it in full
(``~ n63 Switch #badSwitch "Notifications": unchecked -> checked``); further
lines for it repeat only the ref (``~ n63 props checked false -> true``). Nodes
that shift together (a scroll) collapse: a descendant that shifts with its parent
is counted as ``(+k inside)``, and three or more siblings that shift by the same
amount share one line.

Issue deltas compare ``(ref, rule)`` pairs on the nodes both captures hold (a
rebound pair counts as one node): ``resolved`` were in ``a`` only (the node is
still there and the finding is gone), ``new`` in ``b`` only, grouped per rule
with three example refs. The issues of removed nodes are not "resolved", nor
are those of added nodes "new": they are counted apart, as ``gone_with_node``
and ``on_new_nodes`` (a scroll or a closed dialog fixes nothing).

When fewer than 40% of the refs are shared (rebound pairs count as shared), the
result is ``"verdict":"new screen"`` plus b's outline preview instead of hundreds
of +/- lines.

Everything is budgeted: at most ``limit`` lines, at most ``max_bytes`` bytes of
compact JSON, with an explicit ``truncated`` block and a stateless cursor
(``<b>:d:<hash8>:<offset>``) for the next page.

Pure Python; device-free. Things this module cannot see are injected: View
properties (``props_a``/``props_b``, a callable per capture), the pixel diff
(``pixel_diff``, C9), the full selector grammar (``resolve``, C6) and a richer
outline preview (``preview``, C6).
"""

from __future__ import annotations

import bisect
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from ..output import Budget, dumps, json_cost, utf8_len
from . import lines as L
from .model import FLAGS, Index, OpError, UNode
from .refs import preorder

DEFAULT_INCLUDE = ("text", "state", "bounds", "visibility", "a11y", "issues")
OPTIONAL_INCLUDE = ("props", "params", "pixels")
INCLUDE_VALUES = DEFAULT_INCLUDE + OPTIONAL_INCLUDE
DEFAULT_LIMIT = 40
MIN_MOVE_PX = 4
MAX_LIMIT = 200
DEFAULT_MAX_BYTES = 4000
MIN_MAX_BYTES = 500
HARD_MAX_BYTES = 32000
#: Below this fraction of shared refs the verdict is "new screen".
NEW_SCREEN_SHARED = 0.4
PREVIEW_LINES = 20
PREVIEW_DEPTH = 2
#: Bytes kept free for the truncated block and the next hints.
FOOTER_RESERVE = 320
NEXT_MAX_BYTES = 200
CURSOR_LETTER = "d"
LABEL_MAX = 48

STATE_FLAGS = ("checked", "partial", "selected", "disabled", "focused")
STATE_WORDS = {
    "checked": ("unchecked", "checked"),
    "partial": ("not partial", "partial"),
    "selected": ("unselected", "selected"),
    "disabled": ("enabled", "disabled"),
    "focused": ("unfocused", "focused"),
}
#: Flags compared under "a11y" (what a node does, rather than its state).
BEHAVIOUR_FLAGS = tuple(f for f in FLAGS
                        if f not in STATE_FLAGS
                        and f not in ("hidden", "webview", "interop", "truncated", "redacted"))
#: Properties that flicker with touch and never mean the UI changed.
VOLATILE_PROPS = frozenset({"pressed", "hovered"})
VISIBLE_EPS = 0.05
PROPS_PER_NODE = 6
PARAMS_PER_NODE = 6
ISSUE_RULES_MAX = 6
ISSUE_EXAMPLES = 3
SHIFT_GROUP_MIN = 3

PropsFn = Callable[[UNode], "Mapping[str, Any] | None"]
PixelFn = Callable[[Index, Index, list], dict]
ResolveFn = Callable[[Index, str], UNode]
PreviewFn = Callable[[Index, "str | None"], list]


# --------------------------------------------------------------------------- #
# Rendering helpers (line grammar v1, spec 5.1)
# --------------------------------------------------------------------------- #
def quote(s: Any, n: int = LABEL_MAX) -> str:
    """A label in the line grammar: cut at ``n`` chars with an ellipsis, with
    backslashes, quotes and newlines escaped; ``-`` for None."""
    if s is None:
        return "-"
    s = str(s)
    if len(s) > n:
        s = s[: n - 1] + "…"
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return f'"{s}"'


def box(b: Sequence[int] | None) -> str:
    if not b or len(b) < 4:
        return "-"
    return f"[{int(b[0])},{int(b[1])} {int(b[2])}x{int(b[3])}]"


_SEG_FIELDS = L.Fields(line=("ref", "type", "rid", "tag", "label"))
_SEG_FIELDS_FLAGS = L.Fields(line=("ref", "type", "rid", "tag", "label", "flags"))
_SEG_FIELDS_NOLABEL = L.Fields(line=("ref", "type", "rid", "tag"))


def seg(n: UNode, *, flags: bool = False, label: bool = True) -> str:
    """``ref [Type] [#rid] [@tag] ["label"] (flag)*``, exactly as grammar v1 writes
    a segment (:func:`lines.seg_row`): display types, quoted odd rids and tags."""
    fields = _SEG_FIELDS_FLAGS if flags else (_SEG_FIELDS if label else _SEG_FIELDS_NOLABEL)
    if flags and not label:
        fields = L.Fields(line=("ref", "type", "rid", "tag", "flags"))
    return L.seg_text(L.seg_row(n, fields))


def issue_short(rule: str) -> str:
    """The rule's ``!issue`` short code (:func:`lines.short_code`, spec 3.8)."""
    return L.short_code(rule)


def node_line(n: UNode, depth: int = 0, hidden: int = 0) -> str:
    """An outline line: indent, seg with flags, visible bounds, ``!issue``, ``+N``."""
    return L.format_line(L.node_row(None, n, L.Fields(), depth=depth, hidden=hidden))


def _fmt_value(v: Any) -> str:
    if isinstance(v, Mapping) and "value" in v:
        v = v["value"]
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return v if v and " " not in v and '"' not in v and len(v) <= LABEL_MAX else quote(v)
    return quote(json.dumps(v, ensure_ascii=False, separators=(",", ":"), default=str))


def _norm_value(v: Any) -> Any:
    if isinstance(v, Mapping) and "value" in v:
        v = v["value"]
    if isinstance(v, list):
        return tuple(_norm_value(x) for x in v)
    if isinstance(v, Mapping):
        return tuple(sorted((str(k), _norm_value(x)) for k, x in v.items()))
    return v


# --------------------------------------------------------------------------- #
# Argument handling
# --------------------------------------------------------------------------- #
def _bad(message: str, hint: str | None = None) -> OpError:
    return OpError("bad_args", message, hint)


def _parse_include(include: Any) -> set[str] | None:
    """None means "the defaults, plus props/params when both captures have them".
    ``+x`` tokens add to the defaults; plain tokens replace them."""
    if include is None:
        return None
    if isinstance(include, str):
        tokens = [t.strip() for t in include.split(",") if t.strip()]
    else:
        tokens = [str(t).strip() for t in include]
    plus = any(t.startswith("+") for t in tokens)
    names = [t.lstrip("+") for t in tokens]
    bad = [t for t in names if t not in INCLUDE_VALUES]
    if bad:
        raise _bad(f"unknown include value(s): {', '.join(bad)}",
                   f"include takes {', '.join(INCLUDE_VALUES)}")
    chosen = set(DEFAULT_INCLUDE) if plus else set()
    chosen.update(names)
    return chosen


def _int_arg(name: str, value: Any, lo: int, hi: int | None = None) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise _bad(f"{name} must be an integer") from None
    if isinstance(value, bool) or v < lo or (hi is not None and v > hi):
        raise _bad(f"{name} must be between {lo} and {hi}" if hi is not None
                   else f"{name} must be >= {lo}")
    return v


def _max_bytes(value: Any) -> int:
    """As in every query tool (``query.resolve_max_bytes``): None is the 4,000
    default, 0 the 32,000 ceiling, anything else clamped to 500..32,000."""
    from .query import resolve_max_bytes  # C6

    return resolve_max_bytes(value, DEFAULT_MAX_BYTES)


def _cap_name(ix: Index) -> str:
    m = ix.meta
    if m is None:
        return "?"
    return f"{m.id} @{m.label}" if m.label else m.id


def _cid(ix: Index) -> str:
    return ix.meta.id if ix.meta is not None else "?"


def _args_hash(a: Index, b: Index, root: str | None, inc: Iterable[str], mm: int) -> str:
    norm = {"a": _cid(a).lower(), "b": _cid(b).lower(), "within": root,
            "include": sorted(inc), "min_move_px": mm}
    raw = json.dumps(norm, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.blake2s(raw, digest_size=4).hexdigest()


def _parse_cursor(cursor: str, b: Index, h: str) -> int:
    parts = str(cursor).split(":")
    if len(parts) != 4 or parts[1] != CURSOR_LETTER:
        raise _bad(f"not a diff cursor: {cursor!r}")
    cap, _, ch, off = parts
    if cap.lower() != _cid(b).lower() or ch != h:
        raise _bad("the cursor belongs to a diff with different arguments",
                   "Repeat the call with the same a, b, within, include and min_move_px.")
    try:
        offset = int(off)
    except ValueError:
        raise _bad(f"bad cursor offset in {cursor!r}") from None
    if offset < 0:
        raise _bad(f"bad cursor offset in {cursor!r}")
    return offset


def _resolve_root(a: Index, b: Index, within: str, resolve: ResolveFn | None) -> str:
    """The id (ref) ``within`` names, looked up in b first, then in a."""
    last: OpError | None = None
    for ix in (b, a):
        if resolve is not None:
            try:
                return resolve(ix, within).id
            except OpError as e:
                if e.code not in ("not_found", "ref_not_in_capture"):
                    raise
                last = e
        else:
            nid = ix.resolve_id(within)
            if nid is not None:
                return nid
    if last is not None:
        raise last
    raise OpError("not_found", f"{within!r} matches no node in either capture",
                  "within takes a ref (n23), a key (view:82) or an alias (w:1).")


# --------------------------------------------------------------------------- #
# Tree helpers
# --------------------------------------------------------------------------- #
def _subtree(ix: Index, root: str) -> list[str]:
    out: list[str] = []
    stack = [root]
    seen: set[str] = set()
    while stack:
        nid = stack.pop()
        if nid in seen or nid not in ix.nodes:
            continue
        seen.add(nid)
        out.append(nid)
        stack.extend(reversed(ix.nodes[nid].children))
    return out


def _lis_keep(seq: Sequence[int]) -> set[int]:
    """Indices (into ``seq``) of one longest increasing subsequence."""
    tails: list[int] = []
    tails_idx: list[int] = []
    prev: list[int] = [-1] * len(seq)
    for i, v in enumerate(seq):
        k = bisect.bisect_left(tails, v)
        if k == len(tails):
            tails.append(v)
            tails_idx.append(i)
        else:
            tails[k] = v
            tails_idx[k] = i
        prev[i] = tails_idx[k - 1] if k > 0 else -1
    keep: set[int] = set()
    i = tails_idx[-1] if tails_idx else -1
    while i >= 0:
        keep.add(i)
        i = prev[i]
    return keep


# --------------------------------------------------------------------------- #
# Field comparison
# --------------------------------------------------------------------------- #
class _Cmp:
    def __init__(self, inc: set[str], mm: int, props_a: PropsFn | None,
                 props_b: PropsFn | None) -> None:
        self.inc = inc
        self.mm = mm
        self.props_a = props_a
        self.props_b = props_b

    def changes(self, x: UNode, y: UNode) -> tuple[list[str], tuple[int, int] | None, int]:
        """(descriptions, pure shift or None, where the shift goes in the list).
        The shift is reported separately so nodes that scroll together can collapse."""
        out: list[str] = []
        inc = self.inc
        fx, fy = set(x.flags), set(y.flags)
        if "state" in inc:
            flips = [f for f in STATE_FLAGS if (f in fx) != (f in fy)]
            if flips:
                old = ", ".join(STATE_WORDS[f][f in fx] for f in flips)
                new = ", ".join(STATE_WORDS[f][f in fy] for f in flips)
                out.append(f"{old} -> {new}")
            if x.state != y.state:
                out.append(f"state {quote(x.state)} -> {quote(y.state)}")
        label_change = (x.label, y.label) if x.label != y.label else None
        if "text" in inc:
            if label_change:
                out.append(f"label {quote(x.label)} -> {quote(y.label)}")
            for name in ("text", "desc", "hint"):
                ov, nv = getattr(x, name), getattr(y, name)
                if ov != nv and (ov, nv) != label_change:
                    out.append(f"{name} {quote(ov)} -> {quote(nv)}")
        if "visibility" in inc:
            if ("hidden" in fx) != ("hidden" in fy):
                out.append("hidden -> shown" if "hidden" in fx else "shown -> hidden")
            if (x.visible is not None and y.visible is not None
                    and abs(x.visible - y.visible) >= VISIBLE_EPS):
                out.append(f"visible {round(x.visible * 100)}% -> {round(y.visible * 100)}%")
        shift = None
        shift_at = len(out)
        if "bounds" in inc and x.b != y.b:
            desc, shift = self._bounds(x.b, y.b)
            if desc:
                out.append(desc)
        if "a11y" in inc:
            out.extend(self._a11y(x, y, fx, fy, label_change))
        if "props" in inc and self.props_a is not None and self.props_b is not None:
            out.extend(self._props(x, y))
        if "params" in inc:
            out.extend(self._params(x, y))
        return out, shift, shift_at

    def _bounds(self, bx: Sequence[int] | None, by: Sequence[int] | None
                ) -> tuple[str | None, tuple[int, int] | None]:
        if not bx or not by or len(bx) < 4 or len(by) < 4:
            return (f"bounds {box(bx)} -> {box(by)}" if (bx or by) else None), None
        dx, dy = int(by[0]) - int(bx[0]), int(by[1]) - int(bx[1])
        dw, dh = int(by[2]) - int(bx[2]), int(by[3]) - int(bx[3])
        if max(abs(dw), abs(dh)) >= self.mm:
            return f"bounds {box(bx)} -> {box(by)}", None
        if max(abs(dx), abs(dy)) >= self.mm:
            return None, (dx, dy)
        return None, None

    def _a11y(self, x: UNode, y: UNode, fx: set[str], fy: set[str],
              label_change: tuple | None) -> list[str]:
        out: list[str] = []
        if x.role != y.role:
            out.append(f"role {x.role or '-'} -> {y.role or '-'}")
        ax, ay = x.facets.get("a11y") or {}, y.facets.get("a11y") or {}
        sx, sy = ax.get("speakable"), ay.get("speakable")
        if sx != sy and (sx, sy) != label_change:
            out.append(f"speakable {quote(sx)} -> {quote(sy)}")
        acts_x, acts_y = list(ax.get("actions") or ()), list(ay.get("actions") or ())
        added = [a for a in acts_y if a not in acts_x]
        gone = [a for a in acts_x if a not in acts_y]
        if added or gone:
            out.append("actions " + " ".join([f"+{a}" for a in added] + [f"-{a}" for a in gone]))
        fl_add = [f for f in BEHAVIOUR_FLAGS if f in fy and f not in fx]
        fl_del = [f for f in BEHAVIOUR_FLAGS if f in fx and f not in fy]
        if fl_add or fl_del:
            out.append("flags " + " ".join([f"+{f}" for f in fl_add] + [f"-{f}" for f in fl_del]))
        if (x.stop is None) != (y.stop is None):
            out.append(f"stop {x.stop if x.stop is not None else '-'} -> "
                       f"{y.stop if y.stop is not None else '-'}")
        return out

    def _props(self, x: UNode, y: UNode) -> list[str]:
        px = self.props_a(x) if self.props_a is not None else None
        py = self.props_b(y) if self.props_b is not None else None
        if px is None or py is None:
            return []
        names = list(py) + [k for k in px if k not in py]
        out: list[str] = []
        more = 0
        for name in names:
            if name in VOLATILE_PROPS:
                continue
            ov, nv = px.get(name), py.get(name)
            if _norm_value(ov) == _norm_value(nv):
                continue
            if len(out) >= PROPS_PER_NODE:
                more += 1
                continue
            out.append(f"props {name} {_fmt_value(ov)} -> {_fmt_value(nv)}")
        if more:
            out.append(f"props +{more} more")
        return out

    def _params(self, x: UNode, y: UNode) -> list[str]:
        px = (x.facets.get("slot") or {}).get("params")
        py = (y.facets.get("slot") or {}).get("params")
        if px is None and py is None:
            return []
        px, py = px or {}, py or {}
        names = list(py) + [k for k in px if k not in py]
        out: list[str] = []
        more = 0
        for name in names:
            ov, nv = px.get(name), py.get(name)
            if ov == nv:
                continue
            if len(out) >= PARAMS_PER_NODE:
                more += 1
                continue
            out.append(f"params {name} {_fmt_value(ov)} -> {_fmt_value(nv)}")
        if more:
            out.append(f"params +{more} more")
        return out


# --------------------------------------------------------------------------- #
# Issues
# --------------------------------------------------------------------------- #
def _lint_mode(ix: Index) -> str:
    m = ix.meta
    opts = getattr(m, "options", None) if m is not None else None
    return getattr(opts, "lint", "tree") or "tree"


def _issue_pairs(ix: Index, ids: Iterable[str], skip: Callable[[str], bool]
                 ) -> dict[tuple[str, str], None]:
    out: dict[tuple[str, str], None] = {}
    for nid in ids:
        for i in ix.nodes[nid].issues:
            if not skip(i.id):
                out[(nid, i.id)] = None
    return out


def _issue_groups(pairs: Iterable[tuple[str, str]]) -> tuple[list[str], int]:
    by_rule: dict[str, list[str]] = {}
    for ref, rule in pairs:
        by_rule.setdefault(rule, []).append(ref)
    ranked = sorted(by_rule.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    lines = []
    for rule, refs in ranked[:ISSUE_RULES_MAX]:
        s = f"{rule} ×{len(refs)}: " + " ".join(refs[:ISSUE_EXAMPLES])
        if len(refs) > ISSUE_EXAMPLES:
            s += f" +{len(refs) - ISSUE_EXAMPLES}"
        lines.append(s)
    if len(ranked) > ISSUE_RULES_MAX:
        lines.append(f"+{len(ranked) - ISSUE_RULES_MAX} more rules")
    return lines, sum(len(r) for r in by_rule.values())


# --------------------------------------------------------------------------- #
# Preview (new screen)
# --------------------------------------------------------------------------- #
def outline_preview(ix: Index, root: str | None = None, *, depth: int = PREVIEW_DEPTH,
                    max_lines: int = PREVIEW_LINES) -> list[str]:
    """A small outline of ``ix`` (ui tree, ``depth`` levels, ``+N`` hidden)."""
    order = preorder(ix)
    size: dict[str, int] = {}
    for nid in reversed(order):
        size[nid] = 1 + sum(size.get(c, 0) for c in ix.nodes[nid].children)
    lines: list[str] = []
    total = 0
    roots = [root] if root is not None else list(ix.tree("ui").roots)
    for node, d in (pair for r in roots for pair in ix.walk("ui", r, max_depth=depth - 1)):
        total += 1
        if len(lines) < max_lines:
            hidden = size.get(node.id, 1) - 1 if d == depth - 1 else 0
            lines.append(node_line(node, d, hidden))
    if total > len(lines):
        lines[-1:] = [f"…{total - len(lines) + 1} more: outline()"]
    return lines


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #
def diff(a: Index, b: Index, *, within: str | None = None, include: Any = None,
         min_move_px: int = MIN_MOVE_PX, limit: int = DEFAULT_LIMIT, max_bytes: int | None = None,
         cursor: str | None = None, image: bool = False, props_a: PropsFn | None = None,
         props_b: PropsFn | None = None, pixel_diff: PixelFn | None = None,
         resolve: ResolveFn | None = None, preview: PreviewFn | None = None) -> dict:
    """Compare capture ``a`` (older) with ``b`` (newer), by ref. See the module doc.

    ``props_a``/``props_b`` return a node's View properties as ``{name: value}``
    (or None); pass them only when both captures have properties. ``pixel_diff``
    is called as ``pixel_diff(a, b, refs)`` when ``image`` is true or ``include``
    has ``pixels``. ``resolve`` is the selector resolver for ``within`` (defaults to
    refs, keys and aliases). Raises OpError ``bad_args`` for captures of different
    lineages and for bad arguments.
    """
    inc_req = _parse_include(include)
    mm = _int_arg("min_move_px", min_move_px, 0)
    lim = _int_arg("limit", limit, 1, MAX_LIMIT)
    budget_max = _max_bytes(max_bytes)
    ma, mb = a.meta, b.meta
    if ma is not None and mb is not None and tuple(ma.lineage) != tuple(mb.lineage):
        raise _bad(f"captures {ma.id} and {mb.id} belong to different lineages "
                   f"({'/'.join(ma.lineage)} vs {'/'.join(mb.lineage)})",
                   "diff compares two captures of the same serial and package.")
    notes: list[str] = []
    root = _resolve_root(a, b, within, resolve) if within else None

    # ---- scope
    if root is not None:
        ids_a = _subtree(a, root) if root in a.nodes else []
        ids_b = _subtree(b, root) if root in b.nodes else []
    else:
        ids_a, ids_b = preorder(a), preorder(b)
    slots_a = any(a.nodes[i].kind == "slot" for i in ids_a)
    slots_b = any(b.nodes[i].kind == "slot" for i in ids_b)
    if slots_a != slots_b:
        ids_a = [i for i in ids_a if a.nodes[i].kind != "slot"]
        ids_b = [i for i in ids_b if b.nodes[i].kind != "slot"]
        notes.append(f"slot table only in {'a' if slots_a else 'b'}; slot nodes not compared")

    # ---- effective include
    have_props = props_a is not None and props_b is not None
    have_params = slots_a and slots_b
    if inc_req is None:
        inc = set(DEFAULT_INCLUDE)
        if have_props:
            inc.add("props")
        if have_params:
            inc.add("params")
    else:
        inc = set(inc_req)
        if "props" in inc and not have_props:
            notes.append("props not compared: not captured in both")
            inc.discard("props")
        if "params" in inc and not have_params:
            notes.append("params not compared: slot table not captured in both")
            inc.discard("params")
    want_pixels = bool(image) or "pixels" in inc
    inc.discard("pixels")
    h = _args_hash(a, b, root, inc | ({"pixels"} if want_pixels else set()), mm)
    offset = _parse_cursor(cursor, b, h) if cursor else 0

    set_a, set_b = set(ids_a), set(ids_b)
    shared = [i for i in ids_b if i in set_a]
    shared_set = set(shared)

    # ---- rebound pairs (recycled cells): b node -> a ref
    rebound: dict[str, str] = {}
    for i in ids_b:
        old = b.nodes[i].rebound_of
        if i not in set_a and old and old in set_a and old not in set_b:
            rebound[i] = old
    rebound_old = set(rebound.values())
    added = [i for i in ids_b if i not in set_a and i not in rebound]
    removed = [i for i in ids_a if i not in set_b and i not in rebound_old]

    header: dict[str, Any] = {"a": _cap_name(a), "b": _cap_name(b)}
    if ma is not None and mb is not None:
        header["dt_s"] = round(float(mb.created_at) - float(ma.created_at), 1)
        header["same_pid"] = ma.pid is not None and ma.pid == mb.pid
    if root is not None:
        header["within"] = root

    # ---- verdict
    ui_a = {i for i in ids_a if a.nodes[i].kind != "slot"}
    ui_b = {i for i in ids_b if b.nodes[i].kind != "slot"}
    ui_shared = len(ui_a & ui_b) + sum(1 for i in rebound if i in ui_b)
    ui_union = len(ui_a | ui_b) - sum(1 for i in rebound if i in ui_b)
    ratio = ui_shared / ui_union if ui_union else 1.0

    if ratio < NEW_SCREEN_SHARED:
        return _new_screen(a, b, header, root, ui_shared, ui_union, ratio, len(added),
                           len(removed), len(rebound), budget_max, notes, preview)

    cmp = _Cmp(inc, mm, props_a, props_b)
    descs: dict[str, list[str]] = {}
    shifts: dict[str, tuple[int, int]] = {}
    shift_at: dict[str, int] = {}
    moved: dict[str, str] = {}
    for i in shared:
        x, y = a.nodes[i], b.nodes[i]
        d, sh, at = cmp.changes(x, y)
        if d:
            descs[i] = d
        if sh is not None:
            shifts[i] = sh
            shift_at[i] = at
        if x.parent != y.parent:
            moved[i] = f"{x.parent or '-'} -> " + (seg(b.nodes[y.parent]) if y.parent in b.nodes
                                                     else "-")
    # reorders among siblings both captures share (insertions/removals are not moves)
    for p in [None] + shared:
        kids_b = ([r for r in b.tree("ui").roots] + [r for r in b.tree("slots").roots]
                  if p is None else b.nodes[p].children)
        kids_a = ([r for r in a.tree("ui").roots] + [r for r in a.tree("slots").roots]
                  if p is None else a.nodes[p].children)
        common_b = [c for c in kids_b if c in shared_set and c not in moved
                    and a.nodes[c].parent == b.nodes[c].parent]
        if len(common_b) < 2:
            continue
        pos_a = {c: k for k, c in enumerate(c for c in kids_a if c in shared_set)}
        seq = [pos_a.get(c, -1) for c in common_b]
        if seq == sorted(seq):
            continue
        keep = _lis_keep(seq)
        full_a = {c: k for k, c in enumerate(kids_a)}
        full_b = {c: k for k, c in enumerate(kids_b)}
        for k, c in enumerate(common_b):
            if k not in keep:
                moved[c] = f"reordered in {p or 'roots'} ({full_a.get(c, '?')} -> {full_b.get(c)})"

    # ---- collapse shifts: descendants that shift with their parent are "inside"
    def shift_top(i: str) -> str:
        d = shifts[i]
        cur = i
        while True:
            p = b.nodes[cur].parent
            if p is None or p not in shifts or shifts[p] != d:
                return cur
            cur = p

    inside: dict[str, int] = {}
    carried: set[str] = set()
    for i in shifts:
        top = shift_top(i)
        if top != i:
            carried.add(i)
            inside[top] = inside.get(top, 0) + 1
    only_shift_tops = [i for i in shared if i in shifts and i not in carried
                       and i not in descs and i not in moved]
    groups: dict[tuple, list[str]] = {}
    for i in only_shift_tops:
        groups.setdefault((b.nodes[i].parent, shifts[i]), []).append(i)
    grouped: dict[str, tuple] = {}
    for gkey, members in groups.items():
        if len(members) >= SHIFT_GROUP_MIN:
            for m in members:
                grouped[m] = gkey

    # ---- subtree collapse for added / removed / rebound
    def collapse(ids: list[str], ix: Index, members: set[str]) -> dict[str, int]:
        tops: dict[str, int] = {}
        for i in ids:
            cur = i
            while ix.nodes[cur].parent in members:
                cur = ix.nodes[cur].parent
            tops[cur] = tops.get(cur, 0) + (0 if cur == i else 1)
        return tops

    added_tops = collapse(added, b, set(added))
    removed_tops = collapse(removed, a, set(removed))
    rebound_tops = collapse(list(rebound), b, set(rebound))

    # ---- lines, in b's pre-order, then removals in a's
    lines: list[str] = []
    focus: list[str] = []
    emitted_groups: set[tuple] = set()
    for i in ids_b:
        y = b.nodes[i]
        if i in rebound_tops:
            old = a.nodes[rebound[i]]
            extra = f" (+{rebound_tops[i]} inside)" if rebound_tops[i] else ""
            lines.append(f"~ {seg(y)}: rebound, was {old.id}"
                         + (f" {quote(old.label)}" if old.label else "") + extra)
            focus.append(i)
            continue
        if i in added_tops:
            n = added_tops[i]
            lines.append("+ " + node_line(y, 0, n))
            focus.append(i)
            continue
        if i not in shared_set:
            continue
        if i in grouped:
            gkey = grouped[i]
            if gkey not in emitted_groups:
                emitted_groups.add(gkey)
                members = groups[gkey]
                parent, (dx, dy) = gkey
                where = seg(b.nodes[parent]) if parent in b.nodes else "roots"
                n_in = sum(inside.get(m, 0) for m in members)
                s = (f"~ {len(members)} nodes in {where} shifted by {dx},{dy}: "
                     + " ".join(members[:3]))
                if len(members) > 3:
                    s += f" +{len(members) - 3}"
                if n_in:
                    s += f" (+{n_in} inside)"
                lines.append(s)
                focus.append(members[0])
            continue
        d = list(descs.get(i, ()))
        if i in shifts and i not in carried and i not in moved:
            dx, dy = shifts[i]
            s = f"shifted by {dx},{dy} to {box(y.b)}"
            if inside.get(i):
                s += f" (+{inside[i]} inside)"
            d.insert(shift_at[i], s)
        # a label change already shows the new label; do not repeat it in the name
        name = seg(y, label=not any(s.startswith(("label ", "params text ")) for s in d))
        if i in moved:
            lines.append(f"> {name}: {moved[i]}")
            lines.extend(f"~ {i} {s}" for s in d)
            focus.append(i)
        elif d:
            lines.append(f"~ {name}: {d[0]}")
            lines.extend(f"~ {i} {s}" for s in d[1:])
            focus.append(i)
    for i in ids_a:
        if i in removed_tops:
            lines.append("- " + node_line(a.nodes[i], 0, removed_tops[i]))

    changed_nodes = {i for i in shared if i in descs or i in shifts} - set(moved)
    summary = {"changed": len(changed_nodes), "added": len(added), "removed": len(removed),
               "moved": len(moved), "unchanged": len(shared) - len(changed_nodes) - len(moved)}
    if rebound:
        summary["rebound"] = len(rebound)

    # ---- issue deltas
    issues: dict[str, Any] | None = None
    issues_new_n = 0
    if "issues" in inc:
        la, lb = _lint_mode(a), _lint_mode(b)
        if "none" in (la, lb) and la != lb:
            notes.append(f"issues not compared: lint=none in {'a' if la == 'none' else 'b'}")
        else:
            contrast_one_sided = (la == "full") != (lb == "full")
            if contrast_one_sided:
                notes.append("contrast issues not compared (lint=full in only one capture)")

            def skip(rule: str) -> bool:
                return contrast_one_sided and rule.startswith("a11y.contrast")

            # Compare on the nodes both captures hold; a rebound cell is one
            # node (b's ref keyed by the a ref it replaced).
            ident_b = {i: rebound.get(i, i) for i in ids_b}
            both = set(shared) | set(rebound.values())
            ia = _issue_pairs(a, [i for i in ids_a if i in both], skip)
            ib_by_ident = {(ident_b[i], rule): (i, rule)
                           for i, rule in _issue_pairs(b, [i for i in ids_b
                                                           if ident_b[i] in both], skip)}
            res_lines, _ = _issue_groups(k for k in ia if k not in ib_by_ident)
            new_lines, issues_new_n = _issue_groups(
                v for k, v in ib_by_ident.items() if k not in ia)
            issues = {"resolved": res_lines, "new": new_lines}
            gone = len(_issue_pairs(a, [i for i in removed], skip))
            fresh = len(_issue_pairs(b, [i for i in added], skip))
            if gone:
                issues["gone_with_node"] = gone
            if fresh:
                issues["on_new_nodes"] = fresh
                issues_new_n += fresh

    image_result = None
    if want_pixels:
        if pixel_diff is None:
            notes.append("pixels not compared: no image backend here")
        else:
            image_result = pixel_diff(a, b, list(dict.fromkeys(focus)))

    base: dict[str, Any] = dict(header)
    base["summary"] = summary
    if notes:
        base["notes"] = notes
    base["lines"] = []
    if issues is not None:
        base["issues"] = issues
    if image_result is not None:
        base["image"] = image_result

    def next_cursor(k: int) -> str:
        return f"{_cid(b)}:{CURSOR_LETTER}:{h}:{offset + k}"

    # the cursor's follow-up repeats every argument the hash covers, plus the
    # caller's page size, so it can be run exactly as given
    from .query import cursor_call  # C6

    hint_args: dict[str, Any] = {"a": _cid(a), "b": _cid(b)}
    if root is not None:
        hint_args["within"] = root
    if include is not None:
        hint_args["include"] = include
    if mm != MIN_MOVE_PX:
        hint_args["min_move_px"] = mm
    if image:
        hint_args["image"] = True
    hint_page: dict[str, Any] = {}
    if lim != DEFAULT_LIMIT:
        hint_page["limit"] = lim
    if max_bytes is not None and budget_max != DEFAULT_MAX_BYTES:
        hint_page["max_bytes"] = max_bytes

    def assemble(k: int, why: str) -> dict:
        out = dict(base)
        out["lines"] = lines[offset:offset + k]
        rest = len(lines) - offset - k
        nxt: list[str] = []
        if rest > 0:
            out["truncated"] = {"omitted": rest, "why": why, "cursor": next_cursor(k)}
            nxt.append(cursor_call("diff", hint_args, hint_page, next_cursor(k)))
        if focus:
            nxt.append(f'image(ref="{focus[0]}",capture="{_cid(b)}")')
        if issues_new_n:
            nxt.append(f'lint(capture="{_cid(b)}")')
        out["next"] = _cap_next(nxt)
        return out

    # ---- page the lines into the budget: estimate with a footer reserve, then
    # settle on the exact size (a page always makes progress when it can)
    max_k = max(0, min(lim, len(lines) - offset))

    def why(k: int) -> str:
        return "max_lines" if k >= lim else "max_bytes"

    budget = Budget(budget_max, reserve=FOOTER_RESERVE)
    budget.add(utf8_len(dumps(base)))
    k = 0
    for ln in lines[offset:offset + max_k]:
        if not budget.add(json_cost(ln) + (1 if k else 0)):
            break
        k += 1
    if budget_max:
        while k < max_k and utf8_len(dumps(assemble(k + 1, why(k + 1)))) <= budget_max:
            k += 1
        while k > 0 and utf8_len(dumps(assemble(k, why(k)))) > budget_max:
            k -= 1
    result = assemble(k, why(k))
    if budget_max and k == 0 and max_k:
        roomier = _fit(assemble(1, why(1)), budget_max)
        if utf8_len(dumps(roomier)) <= budget_max:
            result = roomier
    return _fit(result, budget_max)


def _cap_next(nxt: list[str]) -> list[str]:
    out: list[str] = []
    for s in nxt[:3]:
        if json_cost(out + [s]) > NEXT_MAX_BYTES:
            break
        out.append(s)
    return out


def _fit(result: dict, max_bytes: int) -> dict:
    """Last resort when even an empty page is too big: shed optional parts
    (non-cursor next hints, issue examples, notes, the preview) until it fits."""
    if not max_bytes:
        return result

    def size() -> int:
        return utf8_len(dumps(result))

    if size() > max_bytes and result.get("next"):
        result["next"] = [s for s in result["next"] if s.startswith("diff(")][:1]
    iss = result.get("issues")
    while size() > max_bytes and iss and (iss.get("resolved") or iss.get("new")):
        side = "resolved" if len(iss.get("resolved") or ()) >= len(iss.get("new") or ()) \
            else "new"
        iss[side] = iss[side][:-1]
        iss["more"] = iss.get("more", 0) + 1
    while size() > max_bytes and result.get("outline"):
        result["outline"] = result["outline"][:-1]
    for key in ("notes", "next"):
        if size() > max_bytes and result.get(key):
            result[key] = []
    return result


def _new_screen(a: Index, b: Index, header: dict, root: str | None, shared: int, union: int,
                ratio: float, n_added: int, n_removed: int, n_rebound: int, max_bytes: int,
                notes: list[str], preview: PreviewFn | None) -> dict:
    result: dict[str, Any] = dict(header)
    result["verdict"] = "new screen"
    result["shared"] = f"{shared} of {union} refs ({round(ratio * 100)}%)"
    summary = {"added": n_added, "removed": n_removed, "kept": shared - n_rebound}
    if n_rebound:
        summary["rebound"] = n_rebound
    result["summary"] = summary
    if notes:
        result["notes"] = notes
    lines = preview(b, root) if preview is not None else outline_preview(b, root)
    result["outline"] = []
    call = f'outline(capture="{_cid(b)}"' + (f',root="{root}")' if root else ")")
    result["next"] = [call]
    budget = Budget(max_bytes, reserve=FOOTER_RESERVE)
    budget.add(utf8_len(dumps(result)))
    for k, ln in enumerate(lines[:PREVIEW_LINES]):
        if not budget.add(json_cost(ln) + (1 if k else 0)):
            break
        result["outline"].append(ln)
    return _fit(result, max_bytes)


__all__ = [
    "BEHAVIOUR_FLAGS",
    "DEFAULT_INCLUDE",
    "INCLUDE_VALUES",
    "NEW_SCREEN_SHARED",
    "OPTIONAL_INCLUDE",
    "STATE_FLAGS",
    "VOLATILE_PROPS",
    "box",
    "diff",
    "issue_short",
    "node_line",
    "outline_preview",
    "quote",
    "seg",
]
