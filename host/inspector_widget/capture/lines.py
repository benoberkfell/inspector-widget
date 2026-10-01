"""Line grammar v1: one line per node (spec "Capture and Walk" sections 5.1 and 6.4).

Every list the capture tools return (outline, find, lint examples, ambiguity
candidates, node dossier sub-lists) is made of lines in this grammar::

    line  := [prefix] indent seg (" > " seg)* [" [x,y wxh]"] (" !"issue)* [" +"N] [tail]
    seg   := ref [" "Type] [" #"rid] [" @"tag] [" \\""label"\\""] (" "flag)*
    tail  := (" "key"="value)* [" in " crumb (" < " crumb)*]
    crumb := ref [" "Type] [" "(#rid | @tag)] [" \\""label"\\""]

* ``indent`` is two spaces per depth. ``prefix`` is ``<stop>. `` in the reading
  view and ``~ ``/``+ ``/``- ``/``> `` in diffs.
* ``Type`` starts with an upper-case letter; ``flag`` is a lower-case word from
  :data:`model.FLAGS`. A rid or tag that is not a plain token is JSON-quoted
  (``@"my tag"``), which the selector grammar accepts too.
* Labels are cut at 48 characters with ``…`` and JSON-escaped, so quotes and
  newlines never break a line.
* Bounds are the node's **visible** rect in screen px (``UNode.b``).
* ``!issue`` is the rule's short code: ``a11y.<group>.<x>`` -> ``group``,
  ``render.<x>`` -> ``x`` (see :func:`short_code`).
* ``+N``: N descendants are hidden (by depth, max_children or budget).
* A chain line (``n2 LinearLayout > n4 FrameLayout #content > n6 AndroidComposeView``)
  is a run of single-child nodes with identical bounds; its bounds, issues, ``+N``
  and tail belong to its last member (a chain ends at a member with issues).
* The tail holds projections (``src=MainActivity.kt:151``, ``textSize=14sp``) and,
  in ``find``, the breadcrumb (`` in n10 @launcher_list < n5 ComposeView``). A
  ``+props:``/``+params:`` name that is also a field name (``text``, ``hint``,
  ``state`` ...) renders namespaced (``params.text=``, ``props.hint=``), so a
  line never carries one key twice.
* A crumb names its node by tag, else rid, else label; when that tag or rid is
  shared by other nodes of the capture (list cells) the label follows it
  (``n749 Card @card "Item 3"``), so the crumb still says which one.

Rows and lines are the same data: :func:`node_row` builds a row (the
``format="json"`` form), :func:`format_line` renders exactly that row, and
:func:`parse_line` reads a line back. Nothing here does I/O or imports protobuf.
"""

from __future__ import annotations

import fnmatch
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .. import normalize as nz
from .model import FLAG_SET, FLAGS, Index, OpError, UNode

GRAMMAR_VERSION = 1
LABEL_MAX = 48
CRUMB_LABEL_MAX = 24
VALUE_MAX = nz.VALUE_CAP
INDENT = "  "

#: Fields a line carries in its segment/bounds/issues part, in rendering order.
LINE_FIELDS = ("ref", "type", "rid", "tag", "label", "flags", "bounds", "issues")
SEG_FIELDS = ("ref", "type", "rid", "tag", "label", "flags")
DEFAULT_FIELDS = LINE_FIELDS
#: Fields that can be appended to the tail with ``+name``.
TAIL_FIELDS = ("src", "dp", "ids", "conf", "sel", "visible", "key", "kind", "stop", "text",
               "desc", "state", "hint", "role", "origin", "window", "declared", "anchor",
               "match", "since")
PROJECTIONS = ("props", "params")
_TAIL_SET = frozenset(TAIL_FIELDS)

#: Row keys that are not tail fields (everything else in a row renders as key=value).
_ROW_STRUCT = frozenset(SEG_FIELDS) | {"chain", "bounds", "issues", "hidden", "in", "depth",
                                      "order", "mark", "props", "params"}
#: Names a +props:/+params: projection cannot use bare in a line.
_RESERVED_KEYS = _TAIL_SET | _ROW_STRUCT


def proj_key(group: str, name: str) -> str:
    """How a projected property or parameter is keyed in a line: bare, or
    ``group.name`` when the name is also a field (``params.text``)."""
    return f"{group}.{name}" if name in _RESERVED_KEYS else name

PropsFn = Callable[[UNode], "Mapping[str, Any] | None"]


# --------------------------------------------------------------------------- #
# Small formatters
# --------------------------------------------------------------------------- #
_BARE_IDENT = re.compile(r"[A-Za-z0-9_.:/$-]+")
_BARE_VALUE = re.compile(r'[^\s"\\]+')
_TYPE_BAD = re.compile(r"[^A-Za-z0-9_$]")


def jstr(s: str) -> str:
    """A JSON string literal (escapes quotes, backslashes and control characters)."""
    return json.dumps(s, ensure_ascii=False)


def cut(s: str, n: int = LABEL_MAX) -> str:
    """Cut ``s`` to at most ``n`` characters, the last being ``…`` when cut."""
    return s if len(s) <= n else s[: n - 1] + "…"


def ident(s: str) -> str:
    """A rid or tag as written after ``#``/``@``: bare when it is a plain token,
    else JSON-quoted (the selector grammar accepts both)."""
    return s if _BARE_IDENT.fullmatch(s) else jstr(s)


def fmt_num(v: float) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if not math.isfinite(v):  # NaN / inf
        return jstr(str(v))
    if float(v).is_integer():
        return str(int(v))
    return f"{round(v, 4):.4f}".rstrip("0").rstrip(".")


def plain_value(v: Any) -> Any:
    """A property value with a source/resolution stack -> its bare value."""
    if isinstance(v, Mapping) and "value" in v:
        return v["value"]
    return v


def fmt_value(v: Any) -> str | None:
    """A tail value as one whitespace-free token (JSON-quoted when needed).

    Numbers render compactly, lists of scalars comma-joined (``0,919,426.7,9``),
    dicts as ``k:v,k:v``; anything else that would contain whitespace or quotes
    is JSON-quoted. None means "no value" (the field is left out)."""
    v = plain_value(v)
    if v is None:
        return None
    if isinstance(v, (bool, int, float)):
        return fmt_num(v)
    if isinstance(v, str):
        v = nz.cap(v)
        return v if _BARE_VALUE.fullmatch(v) else jstr(v)
    if isinstance(v, Mapping):
        parts = [f"{k}:{fmt_value(x)}" for k, x in v.items() if x is not None]
        s = ",".join(parts)
        return s if s and _BARE_VALUE.fullmatch(s) else jstr(json.dumps(
            v, ensure_ascii=False, separators=(",", ":"), default=str))
    if isinstance(v, (list, tuple)):
        items = [fmt_value(x) for x in v]
        s = ",".join(x for x in items if x is not None)
        if s and _BARE_VALUE.fullmatch(s) and all(x and "," not in x for x in items):
            return s
        return jstr(json.dumps(list(v), ensure_ascii=False, separators=(",", ":"),
                               default=str))
    return fmt_value(str(v))


def short_code(rule_id: str) -> str:
    """A rule id's short code for ``!issue``: the catalog's code (``rules.short``:
    ``a11y.<group>.<x>`` -> ``group``, or ``group_x`` when the group has several
    rules; ``render.<x>`` -> ``x``); for a rule the catalog does not know, the
    group, and anything else with dots turned into underscores."""
    from . import rules as R  # the catalog (pure; imports only model)

    if R.is_known(rule_id):
        return R.short(rule_id)
    parts = str(rule_id).split(".")
    if parts[0] == "a11y" and len(parts) >= 3:
        code = parts[1]
    elif parts[0] == "render" and len(parts) >= 2:
        code = "_".join(parts[1:])
    else:
        code = "_".join(parts)
    return re.sub(r"[^A-Za-z0-9_]", "_", code) or "issue"


def issue_codes(n: UNode, all_tb: bool = False) -> list[str]:
    """The node's distinct issue short codes, sorted. TalkBack rules outside the default
    lint (``rules.DEFAULT_TB``: the heuristic or design-call ones, such as double_stop) are
    left out unless ``all_tb``: the reading view shows them, a tree view does not."""
    from .rules import DEFAULT_TB

    return sorted({short_code(i.id) for i in n.issues
                   if all_tb or not i.id.startswith("tb.") or i.id in DEFAULT_TB})


def display_type(n: UNode) -> str | None:
    """``UNode.type`` made safe for the grammar (``Type`` starts upper-case)."""
    t = n.type
    if not t:
        return None
    t = _TYPE_BAD.sub("", str(t).rsplit(".", 1)[-1])
    if not t:
        return None
    return t if t[0].isupper() else t[0].upper() + t[1:] if t[0].isalpha() else "T" + t


def display_label(n: UNode) -> str | None:
    """What a line shows as the label: ``UNode.label``; a slot group falls back to
    its ``text`` (the Text composable's string)."""
    if n.label:
        return n.label
    if n.kind == "slot":
        if n.text:
            return n.text
        params = (n.facets.get("slot") or {}).get("params") or {}
        t = params.get("text")
        if isinstance(t, str) and t:
            return t
    return None


def display_flags(n: UNode) -> list[str]:
    """``UNode.flags`` in vocabulary order (unknown words are dropped)."""
    have = set(n.flags)
    return [f for f in FLAGS if f in have]


def fmt_bounds(b: Sequence[int]) -> str:
    return f"[{int(b[0])},{int(b[1])} {int(b[2])}x{int(b[3])}]"


def to_dp(b: Sequence[float] | None, dpi: float | None) -> list[float] | None:
    """Pixel rect -> dp rect (1 decimal), using the capture's dpi."""
    if not b or not dpi:
        return None
    k = float(dpi) / 160.0
    out: list[float] = []
    for v in b:
        d = round(float(v) / k, 1)
        out.append(int(d) if d.is_integer() else d)
    return out


def capture_dpi(ix: Index) -> float | None:
    meta = ix.meta
    if meta is None:
        return None
    dpi = (meta.device or {}).get("dpi")
    try:
        return float(dpi) if dpi else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Field projection (spec 6.4)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Fields:
    """Which fields a line or row carries.

    ``line`` is the subset of :data:`LINE_FIELDS` (``ref`` is always present),
    ``tail`` the extra fields appended as ``key=value`` in the order asked,
    ``props``/``params`` the property and slot-parameter names (globs allowed)."""

    line: tuple[str, ...] = DEFAULT_FIELDS
    tail: tuple[str, ...] = ()
    props: tuple[str, ...] = ()
    params: tuple[str, ...] = ()

    def spec(self) -> str:
        """Canonical text (used in cursor hashes)."""
        parts = [",".join(self.line), ",".join(self.tail)]
        parts.append("props:" + ",".join(self.props))
        parts.append("params:" + ",".join(self.params))
        return "|".join(parts)

    def with_tail(self, *names: str) -> Fields:
        extra = tuple(n for n in names if n not in self.tail)
        return Fields(self.line, self.tail + extra, self.props, self.params)


_NAME_RE = re.compile(r"[A-Za-z_*?\[][\w*?\[\]!.:-]*")


def parse_fields(spec: Any, base: Fields | None = None) -> Fields:
    """Parse a ``fields`` argument (spec 6.4).

    ``spec`` is None, a comma-separated string or a list of tokens:

    * ``+name`` adds a field, ``-name`` removes one; ``+props:a,b`` and
      ``+params:a,b`` add projections. Bare names after ``props:``/``params:``
      continue that list (``+params:maxLines,overflow``).
    * If the first token has no sign, the list replaces the defaults
      (``ref`` is always kept): ``fields="label,bounds"``.

    Unknown names raise ``OpError("bad_args")``.
    """
    base = base or Fields()
    if spec is None or spec == "" or spec == []:
        return base
    if isinstance(spec, str):
        tokens = [t.strip() for t in spec.split(",")]
    elif isinstance(spec, (list, tuple)):
        tokens = []
        for t in spec:
            if not isinstance(t, str):
                raise OpError("bad_args", f"fields entries must be strings, got {t!r}")
            tokens.extend(x.strip() for x in t.split(","))
    else:
        raise OpError("bad_args", "fields must be a string like \"+src,-bounds\" or a list")
    tokens = [t for t in tokens if t]
    if not tokens:
        return base
    explicit = tokens[0][0] not in "+-"
    line = ["ref"] if explicit else list(base.line)
    tail = [] if explicit else list(base.tail)
    props = [] if explicit else list(base.props)
    params = [] if explicit else list(base.params)
    current: list[str] | None = None
    valid = ", ".join(LINE_FIELDS + TAIL_FIELDS) + ", props:<names>, params:<names>"
    for tok in tokens:
        sign = tok[0] if tok[0] in "+-" else ""
        name = tok[1:].strip() if sign else tok
        if not sign and current is not None and ":" not in name:
            if not _NAME_RE.fullmatch(name):
                raise OpError("bad_args", f"bad property/param name {name!r} in fields")
            if name not in current:
                current.append(name)
            continue
        current = None
        head, colon, rest = name.partition(":")
        if colon and head in PROJECTIONS:
            target = props if head == "props" else params
            if sign == "-":
                raise OpError("bad_args", f"use -{head} (without names) to drop {head}")
            names = [x for x in (r.strip() for r in rest.split(",")) if x]
            if not names:
                raise OpError("bad_args", f"{head}: needs at least one name, e.g. +{head}:textSize")
            for x in names:
                if not _NAME_RE.fullmatch(x):
                    raise OpError("bad_args", f"bad {head} name {x!r} in fields")
                if x not in target:
                    target.append(x)
            current = target
            continue
        if name in PROJECTIONS and sign == "-":
            (props if name == "props" else params).clear()
            continue
        if name == "ref":
            if sign == "-":
                raise OpError("bad_args", "ref is always shown and cannot be removed")
            continue
        if name == "bounds" or name in SEG_FIELDS or name == "issues":
            if sign == "-":
                if name in line:
                    line.remove(name)
            elif name not in line:
                line.append(name)
            continue
        if name in _TAIL_SET:
            if sign == "-":
                if name in tail:
                    tail.remove(name)
            elif name not in tail:
                tail.append(name)
            continue
        raise OpError("bad_args", f"unknown field {name!r}", hint=f"Fields: {valid}.")
    ordered = tuple(f for f in LINE_FIELDS if f in line)
    return Fields(ordered, tuple(tail), tuple(props), tuple(params))


# --------------------------------------------------------------------------- #
# Node values used by rows
# --------------------------------------------------------------------------- #
def slot_params(ix: Index, n: UNode) -> dict[str, Any]:
    """Slot parameters of a node: its own (slot node) or those of the slot groups
    linked to it (a semantics node), first link wins. Raw strings as stored."""
    if n.kind == "slot":
        return dict((n.facets.get("slot") or {}).get("params") or {})
    out: dict[str, Any] = {}
    for sid in (n.facets.get("compose") or {}).get("slots") or ():
        s = ix.nodes.get(sid)
        if s is None:
            continue
        for k, v in ((s.facets.get("slot") or {}).get("params") or {}).items():
            out.setdefault(k, v)
    return out


def brief_param(key: str, raw: Any) -> str | None:
    """A slot parameter in brief form (normalize.compose_value; None = dropped)."""
    if raw is None:
        return None
    return nz.compose_value(key, raw)


def match_names(names: Iterable[str], patterns: Sequence[str]) -> list[str]:
    names = list(names)
    out: list[str] = []
    for pat in patterns:
        if any(ch in pat for ch in "*?["):
            out.extend(n for n in names if fnmatch.fnmatchcase(n, pat) and n not in out)
        elif pat in names and pat not in out:
            out.append(pat)
    return out


def _tail_value(ix: Index, n: UNode, name: str) -> Any:
    if name == "src":
        return n.src
    if name == "dp":
        return to_dp(n.b, capture_dpi(ix))
    if name == "ids":
        return dict(n.ids) or None
    if name == "conf":
        return dict(n.conf) or None
    if name == "sel":
        return n.sel
    if name == "visible":
        return n.visible
    if name == "key":
        return n.key
    if name == "kind":
        return n.kind
    if name == "stop":
        return n.stop
    if name in ("text", "desc", "state", "hint"):
        v = getattr(n, name)
        return cut(v, VALUE_MAX) if isinstance(v, str) and v else None
    if name == "role":
        return n.role or (n.facets.get("a11y") or {}).get("role")
    if name == "origin":
        return n.origin
    if name == "window":
        return n.window
    if name == "declared":
        return list(n.declared_b) if n.declared_b else None
    if name == "anchor":
        return n.anchor
    if name == "match":
        return n.match
    if name == "since":
        return n.since
    raise OpError("bad_args", f"unknown field {name!r}")


def seg_row(n: UNode, fields: Fields) -> dict[str, Any]:
    """The segment part of a row: ref, Type, #rid, @tag, "label", flags."""
    row: dict[str, Any] = {"ref": n.id}
    want = fields.line
    if "type" in want:
        t = display_type(n)
        if t:
            row["type"] = t
    if "rid" in want and n.rid:
        row["rid"] = n.rid
    if "tag" in want and n.tag:
        row["tag"] = n.tag
    if "label" in want:
        lab = display_label(n)
        if lab:
            row["label"] = cut(lab)
    if "flags" in want:
        fl = display_flags(n)
        if fl:
            row["flags"] = fl
    return row


def node_row(ix: Index, n: UNode, fields: Fields | Any = None, *, chain: Sequence[UNode] | None = None,
             hidden: int = 0, depth: int | None = None, order: int | None = None,
             crumbs: Sequence[str] | None = None, props_fn: PropsFn | None = None,
             mark: str | None = None, all_tb: bool = False) -> dict[str, Any]:
    """One row: the ``format="json"`` twin of a line (see :func:`format_line`).

    ``n`` is the node (the last member of a chain). ``chain`` lists every member of
    a chain line, top to bottom. ``hidden`` is the ``+N`` count,
    ``depth`` the indent, ``order`` the reading-view prefix, ``crumbs`` the find
    breadcrumb and ``mark`` a diff prefix. ``all_tb``: every TalkBack issue code (see
    :func:`issue_codes`)."""
    if not isinstance(fields, Fields):
        fields = parse_fields(fields)
    row: dict[str, Any] = {}
    if mark:
        row["mark"] = mark
    if order is not None:
        row["order"] = int(order)
    if depth is not None:
        row["depth"] = int(depth)
    row.update(seg_row(n, fields))
    if chain is not None and len(chain) > 1:
        row["chain"] = [seg_row(m, fields) for m in chain]
    if "bounds" in fields.line and n.b:
        row["bounds"] = [int(v) for v in n.b]
    if "issues" in fields.line:
        codes = issue_codes(n, all_tb)
        if codes:
            row["issues"] = codes
    if hidden:
        row["hidden"] = int(hidden)
    for name in fields.tail:
        v = _tail_value(ix, n, name)
        if v is not None and fmt_value(v) is not None:
            row[name] = v
    if fields.props:
        values = props_fn(n) if props_fn is not None else None
        if values:
            picked = match_names(values.keys(), fields.props)
            pv = {k: plain_value(values[k]) for k in picked if plain_value(values[k]) is not None}
            if pv:
                row["props"] = pv
    if fields.params:
        raw = slot_params(ix, n)
        picked = match_names(raw.keys(), fields.params)
        pv = {}
        for k in picked:
            b = brief_param(k, raw[k])
            if b is not None:
                pv[k] = b
        if pv:
            row["params"] = pv
    if crumbs:
        row["in"] = list(crumbs)
    return row


def seg_text(s: Mapping[str, Any]) -> str:
    """A segment row (:func:`seg_row`) as text: ``ref [Type] [#rid] [@tag]
    ["label"] (flag)*``."""
    return _seg_text(s)


def _seg_text(s: Mapping[str, Any]) -> str:
    out = [str(s["ref"])]
    if s.get("type"):
        out.append(s["type"])
    if s.get("rid"):
        out.append("#" + ident(s["rid"]))
    if s.get("tag"):
        out.append("@" + ident(s["tag"]))
    if s.get("label") is not None:
        out.append(jstr(s["label"]))
    out.extend(s.get("flags") or ())
    return " ".join(out)


def format_line(row: Mapping[str, Any]) -> str:
    """Render one row as a line of grammar v1 (the inverse of :func:`parse_line`)."""
    out: list[str] = []
    if row.get("mark"):
        out.append(f"{row['mark']} ")
    if row.get("order") is not None:
        out.append(f"{row['order']}. ")
    out.append(INDENT * int(row.get("depth") or 0))
    segs = row.get("chain") or [row]
    out.append(" > ".join(_seg_text(s) for s in segs))
    b = row.get("bounds")
    if b:
        out.append(" " + fmt_bounds(b))
    for c in row.get("issues") or ():
        out.append(" !" + c)
    if row.get("hidden"):
        out.append(f" +{int(row['hidden'])}")
    for k, v in row.items():
        if k in _ROW_STRUCT:
            continue
        fv = fmt_value(v)
        if fv is not None:
            out.append(f" {k}={fv}")
    for group in ("props", "params"):
        for k, v in (row.get(group) or {}).items():
            fv = fmt_value(v)
            if fv is not None:
                out.append(f" {proj_key(group, k)}={fv}")
    if row.get("in"):
        out.append(" in " + " < ".join(row["in"]))
    return "".join(out)


def render_line(ix: Index, n: UNode, fields: Fields | Any = None, depth: int = 0,
                hidden: int = 0, tail: str = "", **kw: Any) -> str:
    """Spec section 10: one node as a line. ``fields`` is a :class:`Fields` or a
    ``fields`` argument (None = the defaults). ``tail`` is raw text appended as is
    (include its leading space). Keyword arguments go to :func:`node_row`."""
    return format_line(node_row(ix, n, fields, hidden=hidden, depth=depth, **kw)) + tail


# --------------------------------------------------------------------------- #
# Brief references (breadcrumbs, parents, candidates)
# --------------------------------------------------------------------------- #
def crumb(n: UNode, label_max: int = CRUMB_LABEL_MAX, ix: Index | None = None) -> str:
    """``ref [Type] [@tag | #rid] ["label"]``: a short reference to a node, by tag,
    else rid, else label. With ``ix``, a tag or rid that other nodes of the capture
    share (every cell of a list) is followed by the label, which tells them apart."""
    out = [n.id]
    t = display_type(n)
    if t:
        out.append(t)
    lab = display_label(n)
    if n.tag:
        out.append("@" + ident(n.tag))
        shared = ix is not None and _ident_counts(ix)[0].get(n.tag, 0) > 1
    elif n.rid:
        out.append("#" + ident(n.rid))
        shared = ix is not None and _ident_counts(ix)[1].get(n.rid, 0) > 1
    else:
        shared = True
    if shared and lab:
        out.append(jstr(cut(lab, label_max)))
    return " ".join(out)


def _ident_counts(ix: Index) -> tuple[dict[str, int], dict[str, int]]:
    """How many ui nodes carry each testTag and each rid (cached on the index)."""
    cached = ix.__dict__.get("_iw_ident_counts")
    if cached is not None and cached[0] == len(ix.nodes):
        return cached[1], cached[2]
    tags: dict[str, int] = {}
    rids: dict[str, int] = {}
    for m in ix.nodes.values():
        if m.kind == "slot":
            continue
        if m.tag:
            tags[m.tag] = tags.get(m.tag, 0) + 1
        if m.rid:
            rids[m.rid] = rids.get(m.rid, 0) + 1
    ix.__dict__["_iw_ident_counts"] = (len(ix.nodes), tags, rids)
    return tags, rids


def is_landmark(n: UNode) -> bool:
    """A node worth naming in a breadcrumb: it has a rid, tag, label or is a collection."""
    if n.rid or n.tag or n.label:
        return True
    a = n.facets.get("a11y") or {}
    return bool(a.get("collection"))


def breadcrumbs(ix: Index, n: UNode, k: int = 2) -> list[str]:
    """The ``k`` nearest landmark ancestors, nearest first (spec 6.2)."""
    out: list[str] = []
    seen: set[str] = set()
    p = n.parent
    while p is not None and p not in seen and len(out) < k:
        seen.add(p)
        a = ix.nodes.get(p)
        if a is None:
            break
        if is_landmark(a):
            out.append(crumb(a, ix=ix))
        p = a.parent
    return out


# --------------------------------------------------------------------------- #
# Grammar regex and parser (tests, and consumers that read lines back)
# --------------------------------------------------------------------------- #
_JSTR = r'"(?:[^"\\\x00-\x1f]|\\.)*"'
_REF = r"(?:n[1-9][0-9]*|(?:view|sem|slot|a11y|w|compose):[\w.:-]+)"
_TYPE = r"[A-Z][A-Za-z0-9_$]*"
_ID = rf"(?:[A-Za-z0-9_.:/$-]+|{_JSTR})"
_FLAG = "(?:" + "|".join(sorted(FLAGS, key=len, reverse=True)) + ")"
_SEG = rf"{_REF}(?: {_TYPE})?(?: #{_ID})?(?: @{_ID})?(?: {_JSTR})?(?: {_FLAG})*"
_CRUMB = rf"{_REF}(?: {_TYPE})?(?: (?:#{_ID}|@{_ID}))?(?: {_JSTR})?"
_TOKEN = rf" [A-Za-z_][\w.:-]*=(?:{_JSTR}|[^\s\"\\]+)"
#: The whole-line grammar (v1). Every line the query engine renders matches it.
LINE_RE = re.compile(
    rf"^(?:[~+\->] )?(?:[1-9][0-9]*\. )?(?:{INDENT})*{_SEG}(?: > {_SEG})*"
    rf"(?: \[-?[0-9]+,-?[0-9]+ [0-9]+x[0-9]+\])?(?: ![A-Za-z0-9_]+)*(?: \+[0-9]+)?"
    rf"(?:{_TOKEN})*(?: in {_CRUMB}(?: < {_CRUMB})*)?$"
)

_KV_RE = re.compile(r"^[A-Za-z_][\w.:-]*=")
_TOK_RE = re.compile(
    rf'[#@]{_JSTR}|{_JSTR}|\[-?[0-9]+,-?[0-9]+ [0-9]+x[0-9]+\]'
    rf'|[A-Za-z_][\w.:-]*={_JSTR}|[^\s"]+'
)


def is_line(s: str) -> bool:
    return isinstance(s, str) and LINE_RE.match(s) is not None


def _tokens(s: str) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(s):
        m = _TOK_RE.match(s, i)
        if m is None:
            raise ValueError(f"cannot tokenize line at column {i + 1}: {s!r}")
        out.append(m.group(0))
        i = m.end()
        if i < len(s):
            if s[i] != " ":
                raise ValueError(f"expected a space at column {i + 1}: {s!r}")
            i += 1
    return out


def _unq(tok: str) -> str:
    return json.loads(tok) if tok.startswith('"') else tok


def parse_line(line: str) -> dict[str, Any]:
    """Read a line of grammar v1 back into a row-like dict.

    Returns ``{mark?, order?, depth, segs:[{ref,type?,rid?,tag?,label?,flags?}],
    bounds?, issues?, hidden?, tail:{key: raw token}, in?:[crumb text]}``. Tail
    values stay as rendered (compare with :func:`fmt_value`). Raises ValueError
    when the line does not match :data:`LINE_RE`."""
    if not is_line(line):
        raise ValueError(f"not a grammar v1 line: {line!r}")
    out: dict[str, Any] = {}
    s = line
    m = re.match(r"^([~+\->]) ", s)
    if m:
        out["mark"] = m.group(1)
        s = s[2:]
    m = re.match(r"^([1-9][0-9]*)\. ", s)
    if m:
        out["order"] = int(m.group(1))
        s = s[m.end():]
    depth = 0
    while s.startswith(INDENT):
        depth += 1
        s = s[len(INDENT):]
    out["depth"] = depth
    toks = _tokens(s)
    segs: list[dict[str, Any]] = []
    tail: dict[str, str] = {}
    i = 0
    state = "seg"
    cur: dict[str, Any] | None = None
    crumbs: list[str] = []
    crumb_cur: list[str] = []
    while i < len(toks):
        t = toks[i]
        if state == "crumbs":
            if t == "<":
                crumbs.append(" ".join(crumb_cur))
                crumb_cur = []
            else:
                crumb_cur.append(t)
            i += 1
            continue
        if state == "seg" and (cur is None):
            cur = {"ref": t}
            segs.append(cur)
            i += 1
            continue
        if state == "seg" and t == ">":
            cur = None
            i += 1
            continue
        if _KV_RE.match(t):
            k, _, v = t.partition("=")
            tail[k] = v
            state = "tail"
            i += 1
            continue
        if state == "seg" and t[:1].isupper() and "type" not in cur and not any(
                k in cur for k in ("rid", "tag", "label", "flags")):
            cur["type"] = t
        elif state == "seg" and t.startswith("#") and len(t) > 1 and not any(
                k in cur for k in ("tag", "label", "flags")):
            cur["rid"] = _unq(t[1:])
        elif state == "seg" and t.startswith("@") and not any(k in cur for k in ("label", "flags")):
            cur["tag"] = _unq(t[1:])
        elif state == "seg" and t.startswith('"') and "flags" not in cur:
            cur["label"] = json.loads(t)
        elif state == "seg" and t in FLAG_SET:
            cur.setdefault("flags", []).append(t)
        elif t.startswith("[") and "bounds" not in out and state == "seg":
            x_y, wh = t[1:-1].split(" ")
            x, y = x_y.split(",")
            w, h = wh.split("x")
            out["bounds"] = [int(x), int(y), int(w), int(h)]
            state = "post"
        elif t.startswith("!") and state in ("seg", "post"):
            out.setdefault("issues", []).append(t[1:])
            state = "post"
        elif t.startswith("+") and t[1:].isdigit() and state in ("seg", "post"):
            out["hidden"] = int(t[1:])
            state = "tail"
        elif t == "in":
            state = "crumbs"
        else:
            raise ValueError(f"unexpected token {t!r} in {line!r}")
        i += 1
    if crumb_cur:
        crumbs.append(" ".join(crumb_cur))
    out["segs"] = segs
    if tail:
        out["tail"] = tail
    if crumbs:
        out["in"] = crumbs
    return out


def row_matches_line(row: Mapping[str, Any], line: str) -> bool:
    """True when ``line`` carries exactly the fields of ``row`` (json rows equal
    line fields). Used by tests and by anyone checking a renderer."""
    p = parse_line(line)
    segs = row.get("chain") or [row]
    want_segs = [{k: s[k] for k in SEG_FIELDS if k in s} for s in segs]
    if p["segs"] != want_segs:
        return False
    for k in ("bounds", "issues", "hidden", "in", "order", "mark"):
        if row.get(k) != p.get(k):
            return False
    if int(row.get("depth") or 0) != p["depth"]:
        return False
    want_tail: dict[str, str] = {}
    for k, v in row.items():
        if k in _ROW_STRUCT:
            continue
        fv = fmt_value(v)
        if fv is not None:
            want_tail[k] = fv
    for group in ("props", "params"):
        for k, v in (row.get(group) or {}).items():
            fv = fmt_value(v)
            if fv is not None:
                want_tail[proj_key(group, k)] = fv
    return want_tail == p.get("tail", {})


__all__ = [
    "CRUMB_LABEL_MAX",
    "DEFAULT_FIELDS",
    "GRAMMAR_VERSION",
    "LABEL_MAX",
    "LINE_FIELDS",
    "LINE_RE",
    "TAIL_FIELDS",
    "Fields",
    "breadcrumbs",
    "brief_param",
    "capture_dpi",
    "crumb",
    "cut",
    "display_flags",
    "display_label",
    "display_type",
    "fmt_bounds",
    "fmt_num",
    "fmt_value",
    "format_line",
    "ident",
    "is_landmark",
    "is_line",
    "issue_codes",
    "jstr",
    "match_names",
    "node_row",
    "parse_fields",
    "parse_line",
    "plain_value",
    "proj_key",
    "render_line",
    "row_matches_line",
    "seg_row",
    "seg_text",
    "short_code",
    "slot_params",
    "to_dp",
]
