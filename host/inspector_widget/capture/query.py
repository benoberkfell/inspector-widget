"""Query engine over one capture's Index: outline, find, node and node selectors.

Spec "Capture and Walk" sections 5.1, 5.5-5.7, 6 and 7. Everything here is pure
and offline: it reads an immutable :class:`~inspector_widget.capture.model.Index`
(and, for property values, a props accessor) and returns small, budgeted dicts.
The ops layer (S1) resolves captures and sessions, adds staleness markers and maps
:class:`OpError` to the error envelope.

* :func:`parse_selector` / :func:`select` / :func:`resolve_selector`: the node
  selector grammar of spec 6.1 (``n23``, keys, ``x,y`` points and ``#rid``,
  ``@tag``, ``Type``, ``Type"label"``, ``"label"`` atoms joined by ``" > "``).
* :func:`outline`: a tree view (ui, views, compose, slots, a11y) or the reading
  order, with semantic collapse, chains, ``+N`` markers, depth, max_children,
  paging cursors and a byte budget.
* :func:`find`: structured filters, all ANDed, with breadcrumbs, a path for a
  single hit, sorting, counting and paging.
* :func:`node`: a dossier of one node (or up to 10) with facets added in priority
  order until the budget is used; what does not fit is listed in ``omitted``.

Lines follow grammar v1 (:mod:`.lines`); ``format="json"`` returns the same
fields as ``rows``. Cursors are stateless: ``<capture>:<tool letter>:<hash8 of
the normalized arguments>:<offset>``.

Props accessor. Property values are not in the index; they are decoded lazily
from the capture's ``views.pb``. :func:`node` (and the ``+props:`` projection of
outline and find) read them through ``props_fn`` when given, else through
``loaded.props(view_udid)``. Either returns ``{name: value}`` (normalized, see
``normalize.props_to_map``), a list of property dicts in the strings.py/legacy
shape (normalized here), or None when the view has no properties.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .. import normalize as nz
from ..output import Budget, dumps, utf8_len
from . import lines as L
from . import rules as R
from .lines import Fields, crumb, fmt_value, node_row, parse_fields, render_line
from .model import (
    FLAG_SET,
    FLAGS,
    KINDS,
    Index,
    Issue,
    OpError,
    Tree,
    UNode,
    is_key,
    is_ref,
    ref_num,
)

# --------------------------------------------------------------------------- #
# Limits and vocabularies (spec 5.5-5.7, 6.5, 7)
# --------------------------------------------------------------------------- #
TOOL_LETTERS = {"outline": "o", "find": "f", "lint": "l", "diff": "d"}
DEFAULT_MAX_BYTES = {"outline": 6000, "find": 3000, "node": 3000, "node_batch": 6000}
MAX_BYTES_CEILING = 32000
MIN_MAX_BYTES = 500
FIND_LIMIT = 20
FIND_LIMIT_MAX = 200
OUTLINE_DEPTH = 3
OUTLINE_DEPTH_MAX = 999
OUTLINE_MAX_LINES = 80
OUTLINE_MAX_LINES_MAX = 400
MAX_CHILDREN = 12
MAX_CHILDREN_MAX = 1000
CHAIN_MAX = 8  # members per chain line; a longer run continues on the next line
NODE_BATCH_MAX = 10
NEXT_MAX = 3
NEXT_MAX_BYTES = 200
VALUE_MAX = 120
#: Views decoded at most for the family-group majority of a rare class (props).
_FAMILY_PEERS_MAX = 200

OUTLINE_VIEWS = ("ui", "views", "compose", "slots", "a11y", "reading")
DETAILS = ("semantic", "all")
ORIGIN_FILTERS = ("app", "all")
FORMATS = ("lines", "json")
FIND_DOMAINS = ("ui", "slots", "all")
FIND_SORTS = ("tree", "reading", "top", "area")
HAS_TERMS = ("label", "role", "state", "stop", "slots", "props", "issues", "a11y", "compose",
             "view")
FACETS = ("core", "issues", "a11y", "layout", "compose", "text", "props", "children",
          "ancestors")
DEFAULT_FACETS = ("core", "layout", "a11y", "compose", "issues")
#: Order in which node() adds facets until the budget is used (spec 5.7).
FACET_PRIORITY = ("core", "issues", "a11y", "layout", "compose", "text", "props", "children",
                  "ancestors")
PROPS_MODES = ("none", "key", "nondefault", "all")
PARAMS_MODES = ("brief", "raw")
SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}

#: Flags that make a node worth a line on their own (spec 6.3).
ACTIONABLE = frozenset({"click", "longclick", "edit", "checkable", "scroll"})
#: A parent with one of these speaks its non-focusable text children as one stop.
_MERGING_FLAGS = frozenset({"click", "longclick"})
STUB_TYPES = frozenset({"ViewStub", "ViewStubCompat"})
SLOTS_NOT_CAPTURED = ('not captured: capture(slots="enable") recomposes once and resets '
                      'remember{} state')
SELECTOR_EXAMPLES = ("n23", "@launch_heading", '#feed > "Item 3"')

#: Arguments every query tool accepts and ignores (the ops layer uses them).
_PASS_THROUGH = frozenset({"capture", "serial", "package", "loaded", "props_fn", "tomb"})

PropsFn = Callable[[UNode], "Mapping[str, Any] | None"]


def _bad(message: str, hint: str | None = None) -> OpError:
    return OpError("bad_args", message, hint=hint)


def _cid(ix: Index) -> str | None:
    return ix.meta.id if ix.meta is not None else None


# --------------------------------------------------------------------------- #
# Argument validation
# --------------------------------------------------------------------------- #
def _check_unknown(tool: str, params: Mapping[str, Any], allowed: Iterable[str]) -> None:
    allowed = set(allowed) | _PASS_THROUGH
    bad = sorted(k for k in params if k not in allowed)
    if bad:
        takes = ", ".join(sorted(allowed - _PASS_THROUGH))
        raise _bad(f"{tool}: unknown argument{'s' if len(bad) > 1 else ''} {', '.join(bad)}",
                   hint=f"{tool} takes {takes}.")


def _enum(name: str, v: Any, choices: Sequence[str], default: str) -> str:
    if v is None:
        return default
    if not isinstance(v, str) or v not in choices:
        raise _bad(f"{name} must be one of {', '.join(choices)}; got {v!r}")
    return v


def _int(name: str, v: Any, default: int, lo: int, hi: int) -> int:
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise _bad(f"{name} must be an integer; got {v!r}")
    try:
        iv = int(v)
    except (TypeError, ValueError):
        raise _bad(f"{name} must be an integer; got {v!r}") from None
    if isinstance(v, float) and not v.is_integer():
        raise _bad(f"{name} must be an integer; got {v!r}")
    if iv < lo or iv > hi:
        raise _bad(f"{name} must be between {lo} and {hi}; got {iv}")
    return iv


def _num(name: str, v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool):
        raise _bad(f"{name} must be a number; got {v!r}")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise _bad(f"{name} must be a number; got {v!r}") from None
    if not math.isfinite(f) or f < 0:
        raise _bad(f"{name} must be a non-negative number; got {v!r}")
    return f


def _bool(name: str, v: Any, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.lower() in ("true", "false", "1", "0", "yes", "no"):
        return v.lower() in ("true", "1", "yes")
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    raise _bad(f"{name} must be true or false; got {v!r}")


def _str(name: str, v: Any) -> str | None:
    if v is None or v == "":
        return None
    if not isinstance(v, str):
        raise _bad(f"{name} must be a string; got {v!r}")
    return v


def _str_list(name: str, v: Any) -> list[str]:
    if v is None or v == "" or v == []:
        return []
    if isinstance(v, str):
        items = [x.strip() for x in v.split(",")]
    elif isinstance(v, (list, tuple)):
        items = []
        for x in v:
            if not isinstance(x, str):
                raise _bad(f"{name} entries must be strings; got {x!r}")
            items.extend(y.strip() for y in x.split(","))
    else:
        raise _bad(f"{name} must be a list of strings; got {v!r}")
    return [x for x in items if x]


def _numbers(name: str, v: Any, n: int) -> list[float] | None:
    if v is None or v == "" or v == []:
        return None
    if isinstance(v, str):
        parts = [p.strip() for p in v.split(",")]
    elif isinstance(v, (list, tuple)):
        parts = list(v)
    else:
        raise _bad(f"{name} must be {n} numbers; got {v!r}")
    if len(parts) != n:
        raise _bad(f"{name} must be {n} numbers; got {v!r}")
    out: list[float] = []
    for p in parts:
        if isinstance(p, bool):
            raise _bad(f"{name} must be {n} numbers; got {v!r}")
        try:
            out.append(float(p))
        except (TypeError, ValueError):
            raise _bad(f"{name} must be {n} numbers; got {v!r}") from None
    return out


def resolve_max_bytes(v: Any, default: int) -> int:
    """``max_bytes`` for every capture query tool (outline, find, node, lint and
    diff alike): None -> the tool default; 0 or less -> the 32,000 ceiling;
    otherwise clamped to 500..32,000 (spec 6.5)."""
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        raise _bad(f"max_bytes must be an integer; got {v!r}")
    try:
        iv = int(v)
    except (TypeError, ValueError):
        raise _bad(f"max_bytes must be an integer; got {v!r}") from None
    if iv <= 0:
        return MAX_BYTES_CEILING
    return max(MIN_MAX_BYTES, min(MAX_BYTES_CEILING, iv))


def _props_source(params: Mapping[str, Any], loaded: Any = None) -> PropsFn | None:
    """The props accessor: ``props_fn`` if given, else ``loaded.props(udid)``."""
    fn = params.get("props_fn")
    if loaded is None:
        loaded = params.get("loaded")
    raw: Callable[[int], Any] | None = None
    if fn is not None:
        if not callable(fn):
            raise _bad("props_fn must be callable")
        raw = fn
    elif loaded is not None and callable(getattr(loaded, "props", None)):
        raw = loaded.props
    if raw is None:
        return None
    cache: dict[int, Any] = {}

    def get(n: UNode) -> Mapping[str, Any] | None:
        udid = n.ids.get("view")
        if udid is None or n.kind != "view":
            return None
        udid = int(udid)
        if udid not in cache:
            v = raw(udid)
            if v is None:
                cache[udid] = None
            elif isinstance(v, Mapping):
                cache[udid] = dict(v)
            else:
                cache[udid] = nz.props_to_map(v)
        return cache[udid]

    return get


# --------------------------------------------------------------------------- #
# Cursors (spec 6.5)
# --------------------------------------------------------------------------- #
_CURSOR_RE = re.compile(r"^([^:\s]+):([a-z]):([0-9a-f]{8}):([0-9]+)$")


def args_hash(args: Mapping[str, Any]) -> str:
    """8 hex chars of blake2s over the normalized arguments (sorted keys)."""
    blob = json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      default=str)
    return hashlib.blake2s(blob.encode("utf-8"), digest_size=4).hexdigest()


def make_cursor(capture: str | None, tool: str, h: str, offset: int) -> str:
    return f"{capture or '-'}:{TOOL_LETTERS[tool]}:{h}:{int(offset)}"


def parse_cursor(cursor: str) -> tuple[str, str, str, int]:
    """``(capture, tool letter, hash8, offset)``; raises bad_args when malformed."""
    if not isinstance(cursor, str):
        raise _bad(f"cursor must be a string; got {cursor!r}")
    m = _CURSOR_RE.match(cursor.strip())
    if m is None:
        raise _bad(f"malformed cursor {cursor!r}",
                   hint="Cursors look like c7h2kq:o:3f9a12bc:80; copy them from truncated.cursor.")
    return m.group(1), m.group(2), m.group(3), int(m.group(4))


def cursor_capture(cursor: str | None) -> str | None:
    """The capture id a cursor belongs to (the ops layer resolves it first)."""
    if not cursor:
        return None
    try:
        return parse_cursor(cursor)[0]
    except OpError:
        return None


def _cursor_offset(ix: Index, tool: str, cursor: Any, h: str) -> int:
    if cursor is None or cursor == "":
        return 0
    cap, letter, ch, offset = parse_cursor(cursor)
    if letter != TOOL_LETTERS[tool]:
        raise _bad(f"this cursor belongs to another tool ({letter!r}), not {tool}")
    cid = _cid(ix) or "-"
    if cap.lower() != cid.lower():
        raise _bad(f"this cursor belongs to capture {cap}, not {cid}",
                   hint=f"Pass capture=\"{cap}\" with the cursor.")
    if ch != h:
        raise _bad("this cursor was made with different arguments",
                   hint="Repeat the exact call that returned the cursor (see its next hint), "
                        "adding only cursor=.")
    return offset


# --------------------------------------------------------------------------- #
# next hints (spec 6.6)
# --------------------------------------------------------------------------- #
def call(tool: str, *positional: Any, **kw: Any) -> str:
    """Render a follow-up call: ``outline(root="n10")``, ``node("n22")``."""
    parts = [json.dumps(p, ensure_ascii=False) for p in positional]
    for k, v in kw.items():
        if v is None:
            continue
        parts.append(f"{k}={json.dumps(v, ensure_ascii=False, separators=(',', ':'))}")
    return f"{tool}({','.join(parts)})"


def cursor_call(tool: str, args: Mapping[str, Any], page: Mapping[str, Any],
                cursor: str) -> str:
    """The follow-up call for a cursor: ``tool(**args, **page, cursor=...)``.

    ``args`` are the caller's non-default arguments that the cursor hash covers
    (the call fails without them); ``page`` the non-default page-shape arguments
    (max_lines, limit, max_bytes, format) that keep page 2 the size and shape of
    page 1. The page arguments are dropped when the call would not fit a ``next``
    list (200 B), the others never are."""
    full = call(tool, **args, **page, cursor=cursor)
    if not page or utf8_len(dumps([full])) <= NEXT_MAX_BYTES:
        return full
    return call(tool, **args, cursor=cursor)


def next_hints(candidates: Iterable[str | None]) -> list[str]:
    """At most 3 distinct hints whose JSON list is at most 200 bytes."""
    out: list[str] = []
    for c in candidates:
        if not c or c in out:
            continue
        trial = out + [c]
        if utf8_len(dumps(trial)) > NEXT_MAX_BYTES:
            continue
        out = trial
        if len(out) >= NEXT_MAX:
            break
    return out


# --------------------------------------------------------------------------- #
# Selectors (spec 6.1)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Atom:
    """One selector atom. ``kind`` is ref, key, rid, tag, type or label; a
    ``Type"label"`` atom has kind ``type`` with ``label`` set."""

    kind: str
    value: str | None = None
    label: str | None = None
    ci: bool = False
    prefix: bool = False
    column: int = 1

    def text(self) -> str:
        if self.kind in ("ref", "key"):
            return str(self.value)
        if self.kind in ("rid", "tag"):
            return ("#" if self.kind == "rid" else "@") + L.ident(str(self.value))
        out = self.value if self.kind == "type" else ""
        if self.label is not None:
            out = (out or "") + L.jstr(self.label + ("…" if self.prefix else ""))
            if self.ci:
                out += "i"
        return out or ""


@dataclass(frozen=True)
class Selector:
    """A parsed selector: ``kind`` is ref, key, point or path."""

    kind: str
    text: str
    atoms: tuple[Atom, ...] = ()
    point: tuple[int, int] | None = None


def _sel_error(sel: str, column: int, message: str) -> OpError:
    err = OpError("bad_selector", f"column {column}: {message} (in {sel!r})",
                  hint="Selectors: a ref n23, a key view:82, a point x,y, or atoms #rid @tag "
                       "Type Type\"label\" \"label\" joined by ' > ' (no other spaces).",
                  candidates=list(SELECTOR_EXAMPLES))
    err.column = column  # type: ignore[attr-defined]
    return err


_KEY_PREFIX = re.compile(r"(?:view|sem|slot|a11y|w|compose):")
_BARE_ID = re.compile(r"[A-Za-z0-9_.:/$-]+")
_TYPE_NAME = re.compile(r"[A-Z][A-Za-z0-9_$]*")
_REF_RUN = re.compile(r"n[0-9]+")
_INT_RUN = re.compile(r"-?[0-9]+")


def _quoted(sel: str, i: int, what: str) -> tuple[str, int]:
    """Parse a JSON string starting at ``sel[i] == '"'``; returns (text, end index)."""
    j = i + 1
    while j < len(sel):
        ch = sel[j]
        if ch == "\\":
            j += 2
            continue
        if ch == '"':
            break
        j += 1
    else:
        raise _sel_error(sel, i + 1, f"unterminated {what} (missing closing \")")
    try:
        text = json.loads(sel[i:j + 1])
    except ValueError:
        raise _sel_error(sel, i + 1, f"bad escape in {what}") from None
    return text, j + 1


def _parse_atom(sel: str, i: int) -> tuple[Atom, int]:
    col = i + 1
    ch = sel[i]
    if ch.isspace():
        raise _sel_error(sel, col, "unexpected whitespace; only ' > ' separates atoms")
    if ch in "#@":
        kind = "rid" if ch == "#" else "tag"
        what = "resource id" if ch == "#" else "test tag"
        if i + 1 < len(sel) and sel[i + 1] == '"':
            text, j = _quoted(sel, i + 1, what)
            if not text:
                raise _sel_error(sel, i + 2, f"empty {what}")
            return Atom(kind, value=text, column=col), j
        m = _BARE_ID.match(sel, i + 1)
        if m is None:
            raise _sel_error(sel, i + 2, f"expected a {what} after {ch}")
        return Atom(kind, value=m.group(0), column=col), m.end()
    if ch == '"':
        text, j = _quoted(sel, i, "label")
        ci = j < len(sel) and sel[j] == "i"
        if ci:
            j += 1
        prefix = text.endswith("…")
        return Atom("label", label=text[:-1] if prefix else text, ci=ci, prefix=prefix,
                    column=col), j
    if ch.isupper():
        m = _TYPE_NAME.match(sel, i)
        j = m.end()
        if j < len(sel) and sel[j] == '"':
            text, j = _quoted(sel, j, "label")
            ci = j < len(sel) and sel[j] == "i"
            if ci:
                j += 1
            prefix = text.endswith("…")
            return Atom("type", value=m.group(0), label=text[:-1] if prefix else text, ci=ci,
                        prefix=prefix, column=col), j
        return Atom("type", value=m.group(0), column=col), j
    m = _KEY_PREFIX.match(sel, i)
    if m is not None:
        j = i
        while j < len(sel) and not sel[j].isspace():
            j += 1
        text = sel[i:j]
        if not is_key(text):
            raise _sel_error(sel, col, f"malformed key {text!r} (e.g. view:82, sem:82:448, w:1)")
        return Atom("key", value=text, column=col), j
    m = _REF_RUN.match(sel, i)
    if m is not None:
        text = m.group(0)
        if not is_ref(text):
            raise _sel_error(sel, col, f"{text!r} is not a ref (refs look like n23)")
        return Atom("ref", value=text, column=col), m.end()
    raise _sel_error(sel, col, f"unexpected {ch!r}: an atom starts with #, @, \", a Type "
                               "(capital letter), a ref (n23) or a key (view:82)")


def _parse_point(sel: str) -> Selector:
    m = _INT_RUN.match(sel, 0)
    i = m.end()
    if i >= len(sel) or sel[i] != ",":
        raise _sel_error(sel, i + 1, "a point is x,y (two integers, no spaces)")
    m2 = _INT_RUN.match(sel, i + 1)
    if m2 is None:
        raise _sel_error(sel, i + 2, "a point is x,y (two integers, no spaces)")
    j = m2.end()
    if j != len(sel):
        raise _sel_error(sel, j + 1, "a point x,y stands alone")
    return Selector("point", sel, point=(int(m.group(0)), int(m2.group(0))))


def parse_selector(sel: Any) -> Selector:
    """Parse a node selector (spec 6.1). Raises ``OpError("bad_selector")`` whose
    message starts with ``column N:`` (1-based) and whose ``column`` attribute is N."""
    if isinstance(sel, Selector):
        return sel
    if not isinstance(sel, str):
        raise _sel_error(str(sel), 1, "a selector is a string")
    if sel == "":
        raise _sel_error(sel, 1, "empty selector")
    if sel[0].isdigit() or (sel[0] == "-" and sel[1:2].isdigit()):
        return _parse_point(sel)
    atoms: list[Atom] = []
    i = 0
    while True:
        atom, i = _parse_atom(sel, i)
        atoms.append(atom)
        if i >= len(sel):
            break
        if sel.startswith(" > ", i):
            if i + 3 >= len(sel):
                raise _sel_error(sel, i + 2, "' > ' must be followed by an atom")
            i += 3
            continue
        rest = sel[i:]
        if rest.isspace():
            raise _sel_error(sel, i + 1, "trailing whitespace")
        if sel[i] == " ":
            raise _sel_error(sel, i + 1, "unexpected space; atoms are joined only by ' > ' "
                                         "(one space each side), and Type\"label\" has no space")
        if sel[i] == ">":
            raise _sel_error(sel, i + 1, "write ' > ' with one space on each side")
        raise _sel_error(sel, i + 1, f"unexpected {sel[i]!r} after {atom.text()}")
    if len(atoms) == 1 and atoms[0].kind in ("ref", "key"):
        return Selector(atoms[0].kind, sel, tuple(atoms))
    return Selector("path", sel, tuple(atoms))


def type_names(n: UNode) -> list[str]:
    """Names a Type atom or the find ``type`` glob matches: display type, View
    simple and qualified class, composable name, a11y role, a11y class name."""
    out: list[str] = []

    def add(s: Any) -> None:
        if s and isinstance(s, str) and s not in out:
            out.append(s)

    add(n.type)
    add(L.display_type(n))  # the Type lines show (upper-cased, safe for the grammar)
    v = n.facets.get("view")
    if v:
        for k in ("class", "qualified"):
            c = v.get(k)
            if c:
                add(c)
                add(c.rsplit(".", 1)[-1])
    s = n.facets.get("slot")
    if s:
        add(s.get("name"))
    c = n.facets.get("compose")
    if c:
        add(c.get("name"))
    a = n.facets.get("a11y")
    add(n.role)
    if a:
        add(a.get("role"))
        ac = a.get("class")
        if ac:
            add(ac.rsplit(".", 1)[-1])
    return out


def _label_match(n: UNode, atom: Atom) -> bool:
    lab = L.display_label(n)
    if lab is None or atom.label is None:
        return False
    want = atom.label
    if atom.ci:
        lab, want = lab.casefold(), want.casefold()
    return lab.startswith(want) if atom.prefix else lab == want


def _atom_match(ix: Index, n: UNode, atom: Atom) -> bool:
    k = atom.kind
    if k == "ref":
        return n.id == atom.value or n.ref == atom.value
    if k == "key":
        return ix.resolve_id(atom.value) == n.id
    if k == "rid":
        return n.rid == atom.value
    if k == "tag":
        return n.tag == atom.value
    if k == "type":
        if atom.value not in type_names(n):
            return False
        return atom.label is None or _label_match(n, atom)
    if k == "label":
        return _label_match(n, atom)
    return False


def _ui(n: UNode) -> bool:
    return n.kind != "slot"


def _first_atom(ix: Index, atom: Atom) -> list[UNode]:
    if atom.kind in ("ref", "key"):
        nid = ix.resolve_id(atom.value)
        n = ix.nodes.get(nid) if nid is not None else None
        return [n] if n is not None else []
    ui = [n for n in ix.nodes.values() if _ui(n) and _atom_match(ix, n, atom)]
    if ui:
        return ui
    return [n for n in ix.nodes.values() if not _ui(n) and _atom_match(ix, n, atom)]


def _visible(n: UNode) -> bool:
    b = n.b
    return bool(b) and b[2] > 0 and b[3] > 0 and "hidden" not in n.flags


def at_point(ix: Index, x: float, y: float, *, all_nodes: bool = False) -> list[UNode]:
    """Visible ui nodes covering (x, y): top window first, then deepest first,
    then later in pre-order (drawn on top). ``all_nodes`` returns every hit."""
    hits: list[tuple[int, int, int, UNode]] = []
    for pos, n in enumerate(ix.nodes.values()):
        if not _ui(n) or not _visible(n):
            continue
        b = n.b
        if b[0] <= x < b[0] + b[2] and b[1] <= y < b[1] + b[3]:
            w = ix.nodes.get(n.window) if n.window else None
            z = w.z if w is not None and w.z is not None else 0
            hits.append((z, n.depth, pos, n))
    hits.sort(key=lambda t: (-t[0], -t[1], -t[2]))
    out = [t[3] for t in hits]
    return out if all_nodes else out[:1]


def _legacy_compose(ix: Index, key: str) -> list[UNode]:
    sem_id = key.split(":", 1)[1]
    return [n for n in ix.nodes.values()
            if n.key.startswith("sem:") and n.key.rsplit(":", 1)[-1] == sem_id]


def select(ix: Index, sel: Any) -> list[UNode]:
    """Every node a selector matches (0, 1 or more), without raising for 0 or
    many. The index builder can use it to check that a ``sel`` is unique."""
    s = parse_selector(sel)
    if s.kind == "point":
        return at_point(ix, *s.point)
    first = s.atoms[0]
    unknown_key = s.kind == "key" and ix.resolve_id(first.value) is None
    if unknown_key and first.value.startswith("compose:"):
        return _legacy_compose(ix, first.value)
    if unknown_key and first.value.startswith("w:"):
        n = ix.get("view:" + first.value[2:])
        return [n] if n is not None and n.is_window else []
    cands = _first_atom(ix, first)
    for atom in s.atoms[1:]:
        nxt: list[UNode] = []
        seen: set[str] = set()
        for p in cands:
            for cid in p.children:
                c = ix.nodes.get(cid)
                if c is not None and c.id not in seen and _atom_match(ix, c, atom):
                    seen.add(c.id)
                    nxt.append(c)
        cands = nxt
        if not cands:
            break
    return cands


_CAND_FIELDS = Fields(line=("ref", "type", "rid", "tag", "label"), tail=("sel",))
_AMBIG_FIELDS = Fields(tail=("sel",))


def _nearest(ix: Index, atom: Atom, k: int = 3) -> list[str]:
    """Up to k candidate lines whose rid/tag/type/label is closest to the atom's."""
    pool = [n for n in ix.nodes.values() if _ui(n)]
    if atom.kind == "rid":
        key, target = (lambda n: n.rid), atom.value
    elif atom.kind == "tag":
        key, target = (lambda n: n.tag), atom.value
    elif atom.kind == "type" and atom.label is None:
        key, target = (lambda n: L.display_type(n)), atom.value
    else:
        key, target = (lambda n: L.display_label(n)), atom.label
    by_value: dict[str, UNode] = {}
    for n in pool:
        v = key(n)
        if v and v not in by_value:
            by_value[v] = n
    if not by_value or not target:
        return []
    names = difflib.get_close_matches(target, list(by_value), n=k, cutoff=0.3)
    if atom.label is not None and not names:
        lowered = target.casefold()
        names = [v for v in by_value if lowered in v.casefold()][:k]
    return [render_line(ix, by_value[v], _CAND_FIELDS) for v in names]


def resolve_selector(ix: Index, sel: Any, *, tomb: Mapping[str, Sequence] | None = None) -> UNode:
    """Resolve a selector to exactly one node (spec 6.1, section 10).

    Errors (``OpError``): ``bad_selector`` (with the column), ``ref_not_in_capture``
    (with last-seen info when ``tomb``, the lineage tombstones, knows the ref),
    ``not_found`` (3 nearest labels as candidate lines) and ``ambiguous`` (the
    top 5 lines, each with its ``sel``)."""
    s = parse_selector(sel)
    cid = f"capture {_cid(ix)}" if _cid(ix) else "this capture"
    if s.kind == "ref":
        ref = s.atoms[0].value
        n = ix.get(ref)
        if n is not None:
            return n
        raise _ref_error(ix, ref, cid, tomb)
    matches = select(ix, s)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        if s.kind == "point":
            raise OpError("not_found", f"no visible node at {s.point[0]},{s.point[1]} in {cid}",
                          hint="Points are screen px; see outline() bounds.")
        if s.kind == "key":
            raise OpError("not_found", f"no node with key {s.atoms[0].value} in {cid}",
                          hint="Keys: view:<udid>, sem:<acv>:<id>, a11y:<host>:<virt>, w:<udid>.")
        deeper = _deeper(ix, s)
        if deeper:
            head = " > ".join(a.text() for a in s.atoms[:-1])
            last = s.atoms[-1]
            raise OpError("not_found",
                          f"nothing matches {s.text} in {cid}: ' > ' means a direct child, "
                          f"and {last.text()} is deeper under {head}",
                          hint=f"Use {call('find', within=head, **_atom_filters(last))}, or "
                               "one of the candidates' sel.",
                          candidates=[render_line(ix, n, _AMBIG_FIELDS) for n in deeper[:5]])
        quoted = _quoted_sel(ix, s)
        if quoted is not None:
            raise OpError("not_found",
                          f"nothing matches {s.text} in {cid}: it is one quoted label",
                          hint=f"Pass the sel without its outer quotes: {quoted}",
                          candidates=[quoted])
        last = s.atoms[-1]
        cands = _nearest(ix, last)
        raise OpError("not_found", f"nothing matches {s.text} in {cid}",
                      hint="Nearest matches are in candidates; labels match exactly unless "
                           "followed by i (case-insensitive) or ending in ….",
                      candidates=cands)
    top = [render_line(ix, n, _AMBIG_FIELDS) for n in matches[:5]]
    more = len(matches) - len(top)
    raise OpError("ambiguous", f"{len(matches)} nodes match {s.text}"
                  + (f" ({more} not listed)" if more > 0 else ""),
                  hint="Use one candidate's ref or sel, or add a parent: #list > \"Item\".",
                  candidates=top)


def _ref_error(ix: Index, ref: str, cid: str, tomb: Mapping[str, Sequence] | None) -> OpError:
    """``ref_not_in_capture`` that says why: gone (the lineage's tombstone), newer
    than this capture, or unknown to this app's lineage (another app, a typo)."""
    info = (tomb or {}).get(ref)
    msg = f"{ref} is not in {cid}"
    if info:
        typ, label, last_sel, last_cap = (list(info) + [None] * 4)[:4]
        desc = " ".join(x for x in (typ, L.jstr(label) if label else None) if x)
        msg += f"; last seen in {last_cap} as {desc or 'a node'}"
        cands = None
        if last_sel:
            msg += f" (sel {last_sel})"
            cands = [last_sel]
        return OpError("ref_not_in_capture", msg,
                       hint="It left the screen (refs are never reused): select it by its "
                            "sel in a newer capture, or bring it back and capture again.",
                       candidates=cands)
    newest = max((ref_num(r) for r in ix.nodes if is_ref(r)), default=0)
    if ref_num(ref) > newest:
        return OpError("ref_not_in_capture",
                       f"{msg}; it is newer than every ref of this capture",
                       hint='It may come from a newer capture of this app: pass '
                            'capture="latest" (or that capture\'s id). Refs are never reused.')
    if tomb is not None:
        return OpError("ref_not_in_capture",
                       f"{msg}, and this app's lineage has no record of it",
                       hint="Probably a ref of another app (refs belong to one serial and "
                            "package) or a typo: select by text or sel, e.g. find(text=...).")
    return OpError("ref_not_in_capture", msg,
                   hint='Refs are never reused: capture="latest" if it came from a newer '
                        "capture, else select by text or sel (another app's ref, or a typo).")


def _deeper(ix: Index, s: Selector) -> list[UNode]:
    """For a path that matched nothing: nodes its last atom matches deeper (not as a
    direct child) under what the rest of the path matches."""
    if s.kind != "path" or len(s.atoms) < 2:
        return []
    heads = select(ix, Selector("path", s.text, s.atoms[:-1]))
    last = s.atoms[-1]
    out: list[UNode] = []
    seen: set[str] = set()
    for h in heads:
        for nid in _subtree_ids(ix, h) - {h.id}:
            n = ix.nodes.get(nid)
            if n is not None and nid not in seen and _ui(n) and _atom_match(ix, n, last):
                seen.add(nid)
                out.append(n)
    order = {nid: i for i, nid in enumerate(ix.nodes)}
    return sorted(out, key=lambda n: order.get(n.id, 0))


def _atom_filters(atom: Atom) -> dict[str, Any]:
    """find() filters that match what ``atom`` matches (roughly: text is a substring)."""
    out: dict[str, Any] = {}
    if atom.kind == "rid":
        out["rid"] = atom.value
    elif atom.kind == "tag":
        out["tag"] = atom.value
    elif atom.kind == "type":
        out["type"] = atom.value
    if atom.label is not None:
        out["text"] = atom.label
    return out


def _quoted_sel(ix: Index, s: Selector) -> str | None:
    """A sel pasted with its outer JSON quotes parses as one label atom; return the
    unquoted sel when that is what it is and it matches something."""
    if s.kind != "path" or len(s.atoms) != 1 or s.atoms[0].kind != "label":
        return None
    inner = s.atoms[0].label
    if not inner or s.atoms[0].prefix or s.atoms[0].ci:
        return None
    try:
        t = parse_selector(inner)
    except OpError:
        return None
    if t.kind == "path" and len(t.atoms) == 1 and t.atoms[0].kind == "label":
        return None
    return inner if select(ix, t) else None


# --------------------------------------------------------------------------- #
# Packing lines/rows under a byte budget
# --------------------------------------------------------------------------- #
def _kv_cost(k: str, v: Any) -> int:
    """Bytes a key adds to a compact JSON object (with its separating comma)."""
    return utf8_len(dumps(k)) + 1 + utf8_len(dumps(v)) + 1


@dataclass
class Page:
    """One budgeted page: the entries that fit and the page-dependent footer."""

    entries: list[Any] = field(default_factory=list)
    footer: dict[str, Any] = field(default_factory=dict)


def pack(base: Mapping[str, Any], list_key: str, total: int, offset: int, max_items: int,
         max_bytes: int, render: Callable[[int, bool], Any],
         footer: Callable[[int, bool], dict[str, Any]]) -> Page:
    """Add rendered entries (index ``offset`` onwards) while the whole response
    stays within ``max_bytes`` (spec 6.5).

    ``render(i, minimal)`` returns entry i (a line or row; ``minimal`` asks for the
    smallest form, used only when nothing else fits so a page always advances).
    ``footer(shown, more)`` returns the fields that depend on the page
    (shown, truncated, next); its cost is reserved before each entry is added."""
    budget = Budget(max_bytes, reserve=0)
    budget.add(utf8_len(dumps({**base, list_key: []})))
    entries: list[Any] = []
    end = min(total, offset + max_items)

    def fcost(f: Mapping[str, Any]) -> int:
        # keys appended to a non-empty object: its compact JSON minus the braces,
        # plus one separating comma
        return utf8_len(dumps(f)) - 1 if f else 0

    for i in range(offset, end):
        e = render(i, False)
        c = utf8_len(dumps(e)) + (1 if entries else 0)
        f = footer(len(entries) + 1, i + 1 < total)
        if not budget.fits(c + fcost(f)):
            if not entries:
                e = render(i, True)
                c = utf8_len(dumps(e))
                f = footer(1, i + 1 < total)
                if budget.fits(c + fcost(f)):
                    entries.append(e)
                    budget.add(c)
            break
        entries.append(e)
        budget.add(c)
    more = offset + len(entries) < total
    f = footer(len(entries), more)
    if not entries and not budget.fits(fcost(f)):
        f = {k: v for k, v in f.items() if k != "next"}
    return Page(entries, f)


def _truncated(tool: str, ix: Index, h: str, offset: int, shown: int, total: int,
               why: str) -> dict[str, Any]:
    return {"omitted": total - offset - shown, "why": why,
            "cursor": make_cursor(_cid(ix), tool, h, offset + shown)}


# --------------------------------------------------------------------------- #
# outline (spec 5.5, 6.3)
# --------------------------------------------------------------------------- #
def _stub(n: UNode) -> bool:
    """A ViewStub placeholder (hidden like zero-size nodes)."""
    if n.kind != "view":
        return False
    t = n.type
    if t and t.rsplit(".", 1)[-1] in STUB_TYPES:
        return True
    cls = (n.facets.get("view") or {}).get("class")
    return bool(cls) and cls.rsplit(".", 1)[-1] in STUB_TYPES


_COLLECTION_TYPE = re.compile(r"(RecyclerView|ListView|GridView|ViewPager\d?|Lazy\w*|\w*Pager)$")


def is_collection(n: UNode) -> bool:
    """A list-like parent whose children ``max_children`` caps: scrollable, an a11y
    collection, or a RecyclerView/ListView/GridView/pager/Lazy list by type."""
    if "scroll" in n.flags or (n.facets.get("a11y") or {}).get("collection"):
        return True
    return any(_COLLECTION_TYPE.search(t) for t in type_names(n))


@dataclass(slots=True)
class _Item:
    members: list[str]
    anchor: str
    depth: int
    plus: int = 0
    cut: str | None = None  # "depth" or "children" when descendants were cut


class _Outline:
    """Semantic display structure of one tree (spec 6.3)."""

    def __init__(self, ix: Index, tree: Tree, roots: Sequence[str], *, semantic: bool,
                 slots: bool, origin_app: bool) -> None:
        self.ix = ix
        self.nodes = ix.nodes
        self.tree = tree
        self.semantic = semantic
        self.slots = slots
        self.origin_app = slots and origin_app
        self.chains = semantic and not slots
        self.hidden_zero = 0
        self.collapsed = 0
        self.library = 0
        self.kids: dict[str, list[str]] = {}
        self.shown: dict[str, bool] = {}
        self.size: dict[str, int] = {}
        self._prepare(roots)

    def _hidden(self, n: UNode) -> bool:
        """Zero-size nodes and ViewStubs are hidden (semantic detail only)."""
        if not self.semantic:
            return False
        b = n.b
        if b is not None and (b[2] <= 0 or b[3] <= 0):
            return True
        return not self.slots and _stub(n)

    def _prepare(self, roots: Sequence[str]) -> None:
        """One pre-order pass (visible children, hidden counts) and one post-order
        pass (shown, subtree sizes). Written for speed: 5,000 nodes in a few ms."""
        nodes = self.nodes
        tchildren = self.tree.children
        kids_map = self.kids
        hidden = self._hidden
        order: list[str] = []
        stack = list(reversed(roots))
        while stack:
            nid = stack.pop()
            if nid in kids_map or nid not in nodes:
                continue
            order.append(nid)
            visible: list[str] = []
            for c in tchildren.get(nid, ()):
                cn = nodes.get(c)
                if cn is None:
                    continue
                if hidden(cn):
                    self.hidden_zero += self._subtree_count(c)
                else:
                    visible.append(c)
            kids_map[nid] = visible
            if visible:
                stack.extend(reversed(visible))
        shown = self.shown
        size = self.size
        semantic, slots, origin_app = self.semantic, self.slots, self.origin_app
        for nid in reversed(order):
            kids = kids_map[nid]
            n = nodes[nid]
            if slots:
                s = (n.origin == "app") if origin_app else True
            elif not semantic:
                s = True
            elif self._merged(n, kids):
                s = False
            else:
                s = bool(n.z is not None or n.label or n.rid or n.tag or n.issues
                         or n.stop is not None or not kids
                         or not ACTIONABLE.isdisjoint(n.flags))
                if not s:  # a node with 2 or more shown children is shown too
                    s = sum(1 for c in kids if shown[c]) >= 2
            shown[nid] = s
            sz = 1
            for c in kids:
                sz += size[c]
            size[nid] = sz

    def _merged(self, n: UNode, kids: Sequence[str]) -> bool:
        """An a11y-only text leaf that its clickable, labelled parent speaks as part
        of one TalkBack stop (a Compose row's merged Text children): it collapses
        into the parent's ``+N`` instead of taking a line. Anything with its own
        identity, action, stop or issue keeps its line."""
        if n.kind != "a11y" or kids or n.rid or n.tag or n.issues or n.stop is not None:
            return False
        if not ACTIONABLE.isdisjoint(n.flags) or "focus" in n.flags:
            return False
        p = self.nodes.get(n.parent) if n.parent else None
        return p is not None and bool(p.label) and not _MERGING_FLAGS.isdisjoint(p.flags)

    def _subtree_count(self, nid: str) -> int:
        count = 0
        stack = [nid]
        tchildren = self.tree.children
        while stack:
            x = stack.pop()
            if x not in self.nodes:
                continue
            count += 1
            stack.extend(tchildren.get(x, ()))
        return count

    def chain(self, x: str, force: bool = False) -> tuple[list[str], str | None]:
        """The chain starting at ``x`` and its anchor, the last member (None when no
        member is shown: the chain collapses and its children are hoisted).

        A chain follows single visible children with identical bounds and the same
        kind, never into a window root, for at most CHAIN_MAX members, and ends at a
        member that has issues, so a chain line's issues, ``+N`` and tail all belong
        to its last member."""
        members = [x]
        any_shown = bool(force or self.shown.get(x))
        if self.chains:
            nodes = self.nodes
            while len(members) < CHAIN_MAX:
                last = members[-1]
                nl = nodes[last]
                if nl.issues:
                    break
                kids = self.kids.get(last) or ()
                if len(kids) != 1:
                    break
                c = kids[0]
                nc = nodes[c]
                if nc.is_window or nc.kind != nl.kind or not nl.b or nc.b != nl.b:
                    break
                members.append(c)
                any_shown = any_shown or bool(self.shown.get(c))
        return members, (members[-1] if any_shown else None)

    def expand(self, start: Sequence[str]) -> list[tuple[list[str], str]]:
        """Display children of a line: collapsed nodes are hoisted through."""
        if not self.semantic and not self.origin_app:
            return [([x], x) for x in start]  # detail="all": every node is a line
        out: list[tuple[list[str], str]] = []
        stack = [iter(start)]
        while stack:
            try:
                x = next(stack[-1])
            except StopIteration:
                stack.pop()
                continue
            members, anchor = self.chain(x)
            if anchor is not None:
                out.append((members, anchor))
                continue
            for m in members:
                if self.origin_app and self.nodes[m].origin != "app":
                    self.library += 1
                else:
                    self.collapsed += 1
            stack.append(iter(self.kids.get(members[-1]) or ()))
        return out

    def items(self, roots: Sequence[str], depth_limit: int, max_children: int,
              forced: Iterable[str] = ()) -> list[_Item]:
        forced = set(forced)
        top: list[tuple[list[str], str]] = []
        for r in roots:
            if r not in self.nodes:
                continue
            if r in forced:
                members, anchor = self.chain(r, force=True)
                top.append((members, anchor or r))
            else:
                top.extend(self.expand([r]))
        out: list[_Item] = []
        stack = [(m, a, 0) for m, a in reversed(top)]
        while stack:
            members, anchor, d = stack.pop()
            item = _Item(members, anchor, d)
            out.append(item)
            kids = self.expand(self.kids.get(members[-1]) or ())
            if not kids:
                continue
            if d >= depth_limit:
                item.plus = sum(self.size[m[0]] for m, _ in kids)
                item.cut = "depth"
                continue
            if is_collection(self.nodes[members[-1]]):
                keep, drop = kids[:max_children], kids[max_children:]
            else:
                keep, drop = kids, []
            if drop:
                item.plus = sum(self.size[m[0]] for m, _ in drop)
                item.cut = "children"
            for m, a in reversed(keep):
                stack.append((m, a, d + 1))
        return out


def _require_tree(ix: Index, view: str) -> None:
    """An empty slots tree, or an a11y tree whose facet was not fetched, is a
    missing facet (facet_unavailable with the recapture call), not an empty screen."""
    meta = ix.meta
    status = meta.facet_status(view) if meta is not None else None
    if view == "slots" and any(n.kind == "compose" for n in ix.nodes.values()):
        raise OpError("facet_unavailable", "the slot table was not captured (not populated)",
                      hint='capture(slots="enable") recomposes once and resets remember{} '
                           "state; then outline(view=\"slots\").")
    if view == "a11y" and status not in (None, "ok"):
        raise OpError("facet_unavailable", f"the accessibility tree was not captured ({status})",
                      hint="capture() again; see captures(action=\"show\") for the reason.")


def _tree_members(tree: Tree) -> set[str]:
    out = set(tree.roots)
    for kids in tree.children.values():
        out.update(kids)
    out.update(tree.children.keys())
    return out


_OUTLINE_ARGS = ("root", "view", "depth", "detail", "origin", "max_children", "max_lines",
                 "fields", "cursor", "format", "max_bytes")


def outline(ix: Index, **params: Any) -> dict[str, Any]:
    """``outline(capture, root?, view="ui", depth=3, detail="semantic", origin="app",
    max_children=12, max_lines=80, fields?, cursor?, format="lines", max_bytes=6000)``.

    Returns ``{capture, view, root?, shown, total, offset?, lines|rows, hidden?,
    truncated?, next?}`` (spec 5.5). ``depth`` counts display levels below the
    root(s) (0 = the roots only). ``view="views"`` implies ``detail="all"``."""
    _check_unknown("outline", params, _OUTLINE_ARGS)
    view = _enum("view", params.get("view"), OUTLINE_VIEWS, "ui")
    root_arg = _str("root", params.get("root"))
    root_node = resolve_selector(ix, root_arg, tomb=params.get("tomb")) if root_arg else None
    if root_node is not None and root_node.kind == "slot" and params.get("view") is None:
        view = "slots"  # a slot group lives in the slots tree
    detail = _enum("detail", params.get("detail"), DETAILS, "semantic")
    if view == "views":
        detail = "all"
    origin = _enum("origin", params.get("origin"), ORIGIN_FILTERS, "app")
    depth = _int("depth", params.get("depth"), OUTLINE_DEPTH, 0, OUTLINE_DEPTH_MAX)
    max_children = _int("max_children", params.get("max_children"), MAX_CHILDREN, 1,
                        MAX_CHILDREN_MAX)
    max_lines = _int("max_lines", params.get("max_lines"), OUTLINE_MAX_LINES, 1,
                     OUTLINE_MAX_LINES_MAX)
    fmt = _enum("format", params.get("format"), FORMATS, "lines")
    max_bytes = resolve_max_bytes(params.get("max_bytes"), DEFAULT_MAX_BYTES["outline"])
    base_fields = Fields(tail=("src",)) if view == "slots" else Fields()
    fields = parse_fields(params.get("fields"), base_fields)
    props_fn = _props_source(params) if fields.props else None
    if fields.props and props_fn is None:
        raise OpError("facet_unavailable", "+props: needs the capture's properties",
                      hint="The ops layer passes loaded=; call node(ref, props=...) instead.")
    if root_node is not None and view == "reading":
        raise _bad("root does not apply to view=\"reading\"")

    norm = {"view": view, "detail": detail, "origin": origin if view == "slots" else None,
            "depth": depth, "max_children": max_children, "fields": fields.spec(),
            "root": root_node.id if root_node else None}
    h = args_hash(norm)
    offset = _cursor_offset(ix, "outline", params.get("cursor"), h)

    hidden_counts: dict[str, int] = {}
    if view == "reading":
        nodes = [ix.nodes[r] for r in ix.reading if r in ix.nodes]
        items = [_Item([n.id], n.id, 0) for n in nodes]
        orders = [n.stop if n.stop is not None else i + 1 for i, n in enumerate(nodes)]
    else:
        tree = ix.tree(view)
        roots = list(tree.roots)
        if not roots:
            _require_tree(ix, view)
        if root_node is not None:
            if root_node.id not in _tree_members(tree):
                raise _bad(f"{root_node.id} is not in the {view} tree",
                           hint=f"Pick a root from outline(view=\"{view}\"), or use view=\"ui\".")
            roots = [root_node.id]
        o = _Outline(ix, tree, roots, semantic=detail == "semantic", slots=view == "slots",
                     origin_app=origin == "app")
        # an explicit root is always a line; so is each ComposeView's semantics root
        forced = [root_node.id] if root_node else (roots if view == "compose" else [])
        items = o.items(roots, depth, max_children, forced)
        orders = []
        for k, v in (("zero_size", o.hidden_zero), ("collapsed", o.collapsed),
                     ("library", o.library)):
            if v:
                hidden_counts[k] = v
    total = len(items)
    if offset > total:
        raise _bad(f"cursor offset {offset} is past the end ({total} lines)")

    def row_of(i: int, minimal: bool) -> dict[str, Any]:
        it = items[i]
        n = ix.nodes[it.anchor]
        if minimal:
            return {"ref": n.id}
        chain = [ix.nodes[m] for m in it.members] if len(it.members) > 1 else None
        return node_row(ix, n, fields, chain=chain, hidden=it.plus,
                        depth=it.depth if view != "reading" else None,
                        order=orders[i] if view == "reading" else None, props_fn=props_fn)

    def render(i: int, minimal: bool) -> Any:
        row = row_of(i, minimal)
        return row if fmt == "json" else L.format_line(row)

    cid = _cid(ix)
    base: dict[str, Any] = {"capture": cid, "view": view}
    if root_node is not None:
        base["root"] = root_node.id
    base["total"] = total
    if offset:
        base["offset"] = offset
    if hidden_counts:
        base["hidden"] = hidden_counts

    user_args = {k: params[k] for k in ("view", "root", "depth", "detail", "origin",
                                        "max_children", "fields")
                 if params.get(k) is not None}
    # page shape: not in the cursor hash, but page 2 should look like page 1
    page_args = {k: params[k] for k in ("max_lines", "max_bytes", "format")
                 if params.get(k) not in (None, "")}

    # nodes hidden by semantic collapse or zero size: detail="all" shows them
    reveal = None
    if hidden_counts.keys() & {"zero_size", "collapsed"} and detail == "semantic" \
            and view != "reading":
        reveal = call("outline", **{**{k: v for k, v in user_args.items()
                                       if k not in ("detail", "view")},
                                    **({"view": view} if view != "ui" else {}),
                                    "detail": "all"})

    # prefix facts over the page range, so each footer() is O(1)
    span = items[offset:min(total, offset + max_lines)]
    first_cut_at: list[_Item | None] = [None]
    render_upto, a11y_upto = [False], [False]
    for it in span:
        first_cut_at.append(first_cut_at[-1] or (it if it.cut else None))
        ids = [i.id for i in ix.nodes[it.anchor].issues]
        render_upto.append(render_upto[-1] or any(x.startswith("render.") for x in ids))
        a11y_upto.append(a11y_upto[-1] or any(x.startswith("a11y.") for x in ids))

    def footer(shown: int, more: bool) -> dict[str, Any]:
        f: dict[str, Any] = {"shown": shown}
        hints: list[str | None] = []
        if more:
            why = "max_lines" if shown >= max_lines else "max_bytes"
            f["truncated"] = _truncated("outline", ix, h, offset, shown, total, why)
            hints.append(cursor_call("outline", user_args, page_args,
                                     f["truncated"]["cursor"]))
        first_cut = first_cut_at[shown]
        if first_cut is not None:
            ref = ix.nodes[first_cut.anchor].id
            if first_cut.cut == "children":
                hints.append(call("outline", **{**user_args, "root": ref,
                                                "max_children": MAX_CHILDREN_MAX}))
            elif ref != (root_node.id if root_node else None):
                hints.append(call("outline", **{k: v for k, v in user_args.items()
                                                if k not in ("root", "depth")}, root=ref))
        if render_upto[shown]:
            hints.append(call("find", issue="render."))
        if a11y_upto[shown]:
            hints.append(call("lint"))
        if reveal is not None:
            hints.append(reveal)
        nx = next_hints(hints)
        if nx:
            f["next"] = nx
        return f

    page = pack(base, "rows" if fmt == "json" else "lines", total, offset, max_lines,
                 max_bytes, render, footer)
    return assemble(base, "rows" if fmt == "json" else "lines", page,
                     order=("capture", "view", "root", "shown", "total", "offset"))


def assemble(base: Mapping[str, Any], list_key: str, page: Page,
             order: Sequence[str]) -> dict[str, Any]:
    """The response dict: ``order`` keys first, then the list, then the rest."""
    merged = {**base, **page.footer, list_key: page.entries}
    out: dict[str, Any] = {}
    for k in order:
        if k in merged:
            out[k] = merged.pop(k)
    out[list_key] = merged.pop(list_key)
    out.update(merged)
    return out


# --------------------------------------------------------------------------- #
# find (spec 5.6, 6.2)
# --------------------------------------------------------------------------- #
_FIND_FILTERS = ("text", "text_re", "type", "rid", "tag", "src", "role", "flags", "any_flags",
                 "has", "missing", "issue", "within", "at", "overlaps", "min_dp", "max_dp",
                 "kind", "window", "in")
_FIND_ARGS = _FIND_FILTERS + ("in_", "sort", "limit", "fields", "cursor", "count_only",
                              "format", "max_bytes")


def _glob(pattern: str, *, case: bool = False) -> Callable[[str | None], bool]:
    rx = re.compile(fnmatch.translate(pattern), 0 if case else re.IGNORECASE)
    return lambda s: bool(s) and rx.match(s) is not None


def _subtree_ids(ix: Index, n: UNode) -> set[str]:
    out: set[str] = set()
    stack = [n.id]
    while stack:
        x = stack.pop()
        if x in out:
            continue
        out.add(x)
        node = ix.nodes.get(x)
        if node is not None:
            stack.extend(node.children)
    return out


def _has(ix: Index, n: UNode, term: str, props_on: bool) -> bool:
    if term == "label":
        return bool(n.label)
    if term == "role":
        return bool(n.role or (n.facets.get("a11y") or {}).get("role"))
    if term == "state":
        return bool(n.state or (n.facets.get("a11y") or {}).get("state"))
    if term == "stop":
        return n.stop is not None
    if term == "slots":
        return bool((n.facets.get("compose") or {}).get("slots"))
    if term == "props":
        return props_on and n.kind == "view" and "view" in n.ids
    if term == "issues":
        return bool(n.issues)
    if term == "a11y":
        return n.kind == "a11y" or "a11y" in n.facets
    if term == "compose":
        return "compose" in n.facets
    if term == "view":
        return "view" in n.facets
    return False


def _issue_pred(q: str) -> Callable[[UNode], bool]:
    q = q.strip()
    if q in SEVERITY_ORDER:
        return lambda n: any(i.sev == q for i in n.issues)

    def pred(n: UNode) -> bool:
        return any(i.id == q or i.id.startswith(q) or L.short_code(i.id) == q
                   or R.group(i.id) == q for i in n.issues)
    return pred


def _text_fields(n: UNode) -> list[str]:
    out = [s for s in (n.label, n.text, n.desc, n.state, n.hint) if s]
    if n.kind == "slot":
        t = ((n.facets.get("slot") or {}).get("params") or {}).get("text")
        if isinstance(t, str) and t:
            out.append(t)
    return out


def _min_dp(n: UNode, dpi: float | None) -> float | None:
    if not n.b or not dpi:
        return None
    return min(n.b[2], n.b[3]) / (dpi / 160.0)


def _intersects(b: Sequence[float], r: Sequence[float]) -> bool:
    return (b[0] < r[0] + r[2] and r[0] < b[0] + b[2] and b[1] < r[1] + r[3]
            and r[1] < b[1] + b[3])


def _find_predicates(ix: Index, params: Mapping[str, Any], props_on: bool
                     ) -> tuple[list[Callable[[UNode], bool]], dict[str, Any]]:
    """Validated filter predicates plus their normalized form (cursor hash)."""
    preds: list[Callable[[UNode], bool]] = []
    norm: dict[str, Any] = {}
    text = _str("text", params.get("text"))
    if text:
        low = text.casefold()
        preds.append(lambda n: any(low in s.casefold() for s in _text_fields(n)))
        norm["text"] = text
    text_re = _str("text_re", params.get("text_re"))
    if text_re:
        try:
            rx = re.compile(text_re)
        except re.error as e:
            raise _bad(f"text_re is not a valid regex: {e}") from None
        preds.append(lambda n: any(rx.search(s) for s in _text_fields(n)))
        norm["text_re"] = text_re
    typ = _str("type", params.get("type"))
    if typ:
        g = _glob(typ)
        preds.append(lambda n: any(g(s) for s in type_names(n)))
        norm["type"] = typ
    for name, getter in (("rid", lambda n: n.rid), ("tag", lambda n: n.tag)):
        pat = _str(name, params.get(name))
        if pat:
            g = _glob(pat, case=True)
            preds.append(lambda n, g=g, getter=getter: g(getter(n)))
            norm[name] = pat
    src = _str("src", params.get("src"))
    if src:
        if ":" in src:
            g = _glob(src, case=True)
            preds.append(lambda n: g(n.src))
        else:
            g = _glob(src, case=True)
            preds.append(lambda n: bool(n.src) and g(n.src.split(":", 1)[0].rsplit("/", 1)[-1]))
        norm["src"] = src
    role = _str("role", params.get("role"))
    if role:
        g = _glob(role)
        preds.append(lambda n: g(n.role) or g((n.facets.get("a11y") or {}).get("role")))
        norm["role"] = role
    for name, want_all in (("flags", True), ("any_flags", False)):
        fl = _str_list(name, params.get(name))
        if fl:
            unknown = [f for f in fl if f not in FLAG_SET]
            if unknown:
                raise _bad(f"unknown flag{'s' if len(unknown) > 1 else ''} {', '.join(unknown)}",
                           hint=f"Flags: {' '.join(FLAGS)}.")
            fs = frozenset(fl)
            if want_all:
                preds.append(lambda n, fs=fs: fs.issubset(n.flags))
            else:
                preds.append(lambda n, fs=fs: not fs.isdisjoint(n.flags))
            norm[name] = sorted(fs)
    for name, positive in (("has", True), ("missing", False)):
        terms = _str_list(name, params.get(name))
        if terms:
            unknown = [t for t in terms if t not in HAS_TERMS]
            if unknown:
                raise _bad(f"{name}: unknown term {', '.join(unknown)}",
                           hint=f"Terms: {' '.join(HAS_TERMS)}.")
            ts = tuple(sorted(set(terms)))
            if positive:
                preds.append(lambda n, ts=ts: all(_has(ix, n, t, props_on) for t in ts))
            else:
                preds.append(lambda n, ts=ts: not any(_has(ix, n, t, props_on) for t in ts))
            norm[name] = list(ts)
    issue = _str("issue", params.get("issue"))
    if issue:
        preds.append(_issue_pred(issue))
        norm["issue"] = issue
    within = _str("within", params.get("within"))
    if within:
        w = resolve_selector(ix, within, tomb=params.get("tomb"))
        ids = _subtree_ids(ix, w)
        preds.append(lambda n: n.id in ids)
        norm["within"] = w.id
    at = _numbers("at", params.get("at"), 2)
    if at:
        x, y = at
        preds.append(lambda n: bool(n.b) and n.b[0] <= x < n.b[0] + n.b[2]
                     and n.b[1] <= y < n.b[1] + n.b[3])
        norm["at"] = at
    ov = _numbers("overlaps", params.get("overlaps"), 4)
    if ov:
        if ov[2] <= 0 or ov[3] <= 0:
            raise _bad("overlaps needs a positive width and height")
        preds.append(lambda n: bool(n.b) and n.b[2] > 0 and n.b[3] > 0 and _intersects(n.b, ov))
        norm["overlaps"] = ov
    mn, mx = _num("min_dp", params.get("min_dp")), _num("max_dp", params.get("max_dp"))
    if mn is not None or mx is not None:
        dpi = L.capture_dpi(ix)
        if dpi is None:
            raise OpError("facet_unavailable", "min_dp/max_dp need the capture's dpi",
                          hint="The capture's device.dpi is missing; filter with overlaps.")
        if mn is not None:
            preds.append(lambda n: (_min_dp(n, dpi) or 0) >= mn)
            norm["min_dp"] = mn
        if mx is not None:
            preds.append(lambda n: _min_dp(n, dpi) is not None and _min_dp(n, dpi) <= mx)
            norm["max_dp"] = mx
    kinds = _str_list("kind", params.get("kind"))
    if kinds:
        bad = [k for k in kinds if k not in KINDS]
        if bad:
            raise _bad(f"kind must be among {', '.join(KINDS)}; got {', '.join(bad)}")
        ks = frozenset(kinds)
        preds.append(lambda n: n.kind in ks)
        norm["kind"] = sorted(ks)
    window = params.get("window")
    if window is not None and window != "":
        wnode = None
        if isinstance(window, str) and window.isdigit():
            window = int(window)
        if isinstance(window, int) and not isinstance(window, bool):
            wnode = next((w for w in ix.windows() if w.z == window), None)
            if wnode is None:
                raise OpError("not_found", f"no window with z {window}",
                              candidates=[crumb(w) + f" z{w.z}" for w in ix.windows()])
        elif isinstance(window, str):
            wnode = resolve_selector(ix, window, tomb=params.get("tomb"))
            if not wnode.is_window:
                raise _bad(f"{wnode.id} is not a window root",
                           hint="Windows: " + ", ".join(crumb(w) for w in ix.windows()))
        else:
            raise _bad(f"window must be a selector or a z index; got {window!r}")
        wid = wnode.id
        preds.append(lambda n: n.window == wid)
        norm["window"] = wid
    return preds, norm


PATH_JOINER = " / "


def _path(ix: Index, n: UNode) -> str:
    """The hit's landmark ancestors, window first: ``n1 / n6 / n10 / n15``. It
    skips levels, so it is joined with `` / `` (not a selector: `` > `` means a
    direct child there)."""
    chain = [n]
    for a in ix.ancestors(n.id):
        if a.is_window or L.is_landmark(a) or ACTIONABLE.intersection(a.flags) or a.parent is None:
            chain.append(a)
    return PATH_JOINER.join(x.id for x in reversed(chain))


def _slots_missing(ix: Index) -> bool:
    """The screen has Compose but the capture has no slot table (not populated)."""
    return not ix.tree("slots").roots and any(n.kind == "compose" for n in ix.nodes.values())


def find(ix: Index, **params: Any) -> dict[str, Any]:
    """``find(capture, text?, text_re?, type?, rid?, tag?, src?, role?, flags?,
    any_flags?, has?, missing?, issue?, within?, at?, overlaps?, min_dp?, max_dp?,
    kind?, window?, in="ui", sort="tree", limit=20, fields?, cursor?,
    count_only=false, format="lines", max_bytes=3000)``.

    All filters are ANDed (spec 6.2). Returns ``{capture, total, shown?, lines|rows,
    path? (single hit), truncated?, next?}``; ``count_only`` returns
    ``{capture, total}``. Each line carries a breadcrumb of its 2 nearest landmark
    ancestors. ``sort="area"`` puts the smallest nodes first."""
    params = dict(params)
    if "in_" in params:
        if params.get("in") is not None:
            raise _bad("pass in or in_, not both")
        params["in"] = params.pop("in_")
    _check_unknown("find", params, _FIND_ARGS)
    domain_arg = params.get("in")
    domain = _enum("in", domain_arg, FIND_DOMAINS, "ui")
    if domain_arg is None and params.get("src"):
        domain = "all"
    sort = _enum("sort", params.get("sort"), FIND_SORTS, "tree")
    limit = _int("limit", params.get("limit"), FIND_LIMIT, 1, FIND_LIMIT_MAX)
    count_only = _bool("count_only", params.get("count_only"), False)
    fmt = _enum("format", params.get("format"), FORMATS, "lines")
    max_bytes = resolve_max_bytes(params.get("max_bytes"), DEFAULT_MAX_BYTES["find"])
    fields = parse_fields(params.get("fields"))
    props_fn = _props_source(params) if fields.props else None
    if fields.props and props_fn is None:
        raise OpError("facet_unavailable", "+props: needs the capture's properties",
                      hint="The ops layer passes loaded=; call node(ref, props=...) instead.")
    props_on = bool(ix.meta is None or ix.meta.options.props)
    notes: list[str] = []
    if _slots_missing(ix):
        need = [what for what, on in (
            ("src", bool(params.get("src"))), ('in="slots"', domain == "slots"),
            ("has=slots", "slots" in _str_list("has", params.get("has"))))
            if on]
        if need:
            raise OpError("facet_unavailable",
                          f"{need[0]} needs the slot table, which this capture does not have "
                          "(not populated)",
                          hint='capture(slots="enable") recomposes once and resets remember{} '
                               "state; then find again.")
        if domain == "all" or fields.params:
            notes.append('slot table not captured: no slot nodes or params '
                         '(capture(slots="enable") recomposes and resets remember{} state)')
    preds, norm = _find_predicates(ix, params, props_on)
    norm.update({"in": domain, "sort": sort, "fields": fields.spec()})
    h = args_hash(norm)
    offset = _cursor_offset(ix, "find", params.get("cursor"), h)

    if domain == "ui":
        pool = [n for n in ix.nodes.values() if n.kind != "slot"]
    elif domain == "slots":
        pool = [n for n in ix.nodes.values() if n.kind == "slot"]
    else:
        pool = list(ix.nodes.values())
    hits = [n for n in pool if all(p(n) for p in preds)] if preds else pool
    if sort == "reading":
        pos = {n.id: i for i, n in enumerate(hits)}
        hits.sort(key=lambda n: (0, n.stop, 0) if n.stop is not None else (1, 0, pos[n.id]))
    elif sort == "top":
        pos = {n.id: i for i, n in enumerate(hits)}
        hits.sort(key=lambda n: (n.b[1], n.b[0], pos[n.id]) if n.b else
                  (float("inf"), 0, pos[n.id]))
    elif sort == "area":
        pos = {n.id: i for i, n in enumerate(hits)}
        hits.sort(key=lambda n: (n.b[2] * n.b[3], pos[n.id]) if n.b else
                  (float("inf"), pos[n.id]))
    total = len(hits)
    cid = _cid(ix)
    if count_only:
        return {"capture": cid, "total": total}
    if offset > total:
        raise _bad(f"cursor offset {offset} is past the end ({total} hits)")

    def render(i: int, minimal: bool) -> Any:
        n = hits[i]
        row = {"ref": n.id} if minimal else node_row(
            ix, n, fields, crumbs=L.breadcrumbs(ix, n), props_fn=props_fn)
        return row if fmt == "json" else L.format_line(row)

    base: dict[str, Any] = {"capture": cid, "total": total}
    if offset:
        base["offset"] = offset
    if total == 1:
        base["path"] = _path(ix, hits[0])
    if notes:
        base["notes"] = notes
    user_args = {k: params[k] for k in _FIND_FILTERS + ("sort", "fields")
                 if params.get(k) not in (None, "", [])}
    page_args = {k: params[k] for k in ("limit", "max_bytes", "format")
                 if params.get(k) not in (None, "", [])}

    def footer(shown: int, more: bool) -> dict[str, Any]:
        f: dict[str, Any] = {}
        hints: list[str | None] = []
        if more or offset:
            f["shown"] = shown
        if more:
            why = "limit" if shown >= limit else "max_bytes"
            f["truncated"] = _truncated("find", ix, h, offset, shown, total, why)
            hints.append(cursor_call("find", user_args, page_args, f["truncated"]["cursor"]))
        if total == 1:
            n = hits[0]
            hints.append(call("node", n.id))
            if _wants_image(ix, n):
                hints.append(call("image", ref=n.id))
        nx = next_hints(hints)
        if nx:
            f["next"] = nx
        return f

    list_key = "rows" if fmt == "json" else "lines"
    page = pack(base, list_key, total, offset, limit, max_bytes, render, footer)
    return assemble(base, list_key, page, order=("capture", "total", "shown", "offset", "path"))


# --------------------------------------------------------------------------- #
# node (spec 5.7)
# --------------------------------------------------------------------------- #
_NODE_ARGS = ("facets", "props", "params", "ancestors", "children", "image", "max_bytes",
              "image_fn", "issue_fmt")


def _cap(v: Any, n: int = VALUE_MAX) -> Any:
    if isinstance(v, str):
        return nz.cap(v, n)
    if isinstance(v, Mapping):
        return {k: _cap(x, n) for k, x in v.items()}
    if isinstance(v, list):
        return [_cap(x, n) for x in v]
    return v


def issue_text(i: Issue) -> str:
    """One issue as text: ``<rule id> <sev>[ (conf)]: k=v … (note)``."""
    s = f"{i.id} {i.sev}"
    if i.conf and i.conf != "exact":
        s += f" ({i.conf})"
    ev = dict(i.evidence or {})
    note = ev.pop("note", None)
    parts = []
    for k, v in ev.items():
        fv = fmt_value(v)
        if fv is not None:
            parts.append(f"{k}={fv}")
    if parts:
        s += ": " + " ".join(parts)
    if note:
        s += f" ({note})"
    return nz.cap(s, 200)


def _sorted_issues(n: UNode) -> list[Issue]:
    return sorted(n.issues, key=lambda i: (SEVERITY_ORDER.get(i.sev, 3), i.id))


_NOT_DRAWN = {"render.offscreen": "offscreen", "render.zero_size": "zero size"}


def visible_rect(ix: Index, n: UNode) -> tuple[list[int] | None, str | None]:
    """``(rect, None)``: the part of ``n`` that is on screen, its visible bounds
    clipped to its window and the screen; or ``(None, why)`` for a node nobody can
    see or tap (hidden, offscreen, zero size, outside its window)."""
    if "hidden" in n.flags:
        return None, "hidden"
    for i in n.issues:
        if i.id in _NOT_DRAWN:
            return None, _NOT_DRAWN[i.id]
    b = n.b
    if not b or b[2] <= 0 or b[3] <= 0:
        return None, "zero size"
    x0, y0, x1, y1 = b[0], b[1], b[0] + b[2], b[1] + b[3]
    w = ix.nodes.get(n.window) if n.window else None
    clips = []
    if w is not None and w.id != n.id and w.b and w.b[2] > 0 and w.b[3] > 0:
        clips.append(w.b)
    screen = (ix.meta.device or {}).get("screen") if ix.meta is not None else None
    if screen and len(screen) >= 2 and screen[0] and screen[1]:
        clips.append([0, 0, screen[0], screen[1]])
    for c in clips:
        x0, y0 = max(x0, c[0]), max(y0, c[1])
        x1, y1 = min(x1, c[0] + c[2]), min(y1, c[1] + c[3])
    if x1 <= x0 or y1 <= y0:
        return None, "outside its window"
    return [int(x0), int(y0), int(x1 - x0), int(y1 - y0)], None


def _tap_xy(ix: Index, n: UNode) -> tuple[list[int] | None, str | None]:
    """The centre of the visible part (spec 5.7), or None and why not."""
    r, why = visible_rect(ix, n)
    if r is None:
        return None, why
    return [int(r[0] + r[2] // 2), int(r[1] + r[3] // 2)], None


def _wants_image(ix: Index, n: UNode) -> bool:
    """image(ref) is worth suggesting: something to look at (an issue, or only
    part visible) and pixels to cut (it is on screen)."""
    if not (n.issues or (n.visible is not None and n.visible < 1)):
        return False
    return n.kind != "slot" and visible_rect(ix, n)[0] is not None


def _clipped_by(ix: Index, n: UNode) -> str | None:
    for i in n.issues:
        if i.id != "render.clipped":
            continue
        ev = i.evidence or {}
        by = ev.get("clipped_by")
        if not by:
            continue
        c = ix.get(str(by))
        text = crumb(c) if c is not None else str(by)
        edge = ev.get("edge")
        if c is not None and c.b and edge in ("top", "bottom", "left", "right"):
            x, y, w, h = c.b
            coord = {"top": y, "bottom": y + h, "left": x, "right": x + w}[edge]
            text += f" ({edge} {coord})"
        elif edge:
            text += f" ({edge})"
        return text
    return None


def _has_slots(ix: Index) -> bool:
    if ix.tree("slots").roots:
        return True
    meta = ix.meta
    return bool(meta is not None and meta.facet_status("slots") == "ok")


def _slot_line(ix: Index, s: UNode, raw: bool) -> Any:
    params = dict((s.facets.get("slot") or {}).get("params") or {})
    if raw:
        out: dict[str, Any] = {"ref": s.id, "name": (s.facets.get("slot") or {}).get("name")
                               or s.type, "src": s.src}
        if params:
            out["params"] = _cap(params)
        return {k: v for k, v in out.items() if v is not None}
    names = tuple(k for k, v in params.items()
                  if k != "text" and not _default_param(k, L.brief_param(k, v)))[:6]
    f = Fields(line=("ref", "type", "label", "bounds"), tail=("src",), params=names)
    return render_line(ix, s, f)


#: Brief slot-parameter values that are Compose's own defaults, left out of the
#: one-line slot summaries in node() (``params="raw"`` and ``+params:`` show them).
_PARAM_DEFAULTS = {
    "softWrap": "true", "maxLines": "inf", "minLines": "1", "tonalElevation": "0.0",
    "shadowElevation": "0.0", "enabled": "true", "overflow": "Clip",
}


def _default_param(key: str, brief: str | None) -> bool:
    """True for a parameter not worth a slot line: dropped by normalization, a
    known default, a content lambda (the children show it) or a theme object."""
    if brief is None:
        return True
    if _PARAM_DEFAULTS.get(key) == brief:
        return True
    if brief == nz.LAMBDA and not key.startswith("on"):
        return True
    return key == "colors" and "(" not in brief and brief[:1].isupper()


def _a11y_facet(ix: Index, n: UNode) -> Any:
    a = n.facets.get("a11y")
    if not a and n.kind != "a11y":
        return "none (not in the accessibility tree)" if n.kind in ("view", "compose") else None
    out: dict[str, Any] = {}
    links = ("labeled_by", "label_for", "traversal", "traversal_before", "traversal_after")
    for k, v in (a or {}).items():
        if k in links:
            refs = v if isinstance(v, list) else [v]
            briefs = [crumb(ix.nodes[r]) if r in ix.nodes else str(r) for r in refs if r]
            if briefs:
                out[k] = briefs if isinstance(v, list) else briefs[0]
        elif k == "class":
            simple = str(v).rsplit(".", 1)[-1] if v else None
            if simple and simple != "View":
                out[k] = simple
        elif v is None:
            if k == "role":
                out[k] = None
        elif v in ([], {}, ""):
            continue
        elif k == "flags" and list(v) == L.display_flags(n):
            continue  # the same as the node's flags (core)
        elif k == "actions" and isinstance(v, list):
            acts = [x for x in v if x not in nz.BOILERPLATE_ACTIONS]
            if acts:
                out[k] = acts
        elif k == "res" and n.rid and str(v).rsplit("/", 1)[-1] == n.rid:
            continue  # viewIdResourceName that only repeats the rid
        else:
            out[k] = _cap(v)
    if n.stop is not None:
        out["stop"] = n.stop
    return out or None


def _layout_facet(ix: Index, n: UNode) -> dict[str, Any] | None:
    out: dict[str, Any] = {}
    if n.declared_b:
        out["declared"] = list(n.declared_b)
    if n.visible is not None:
        out["visible"] = n.visible
    clip = _clipped_by(ix, n)
    if clip:
        out["clipped_by"] = clip
    if n.render:
        out["render"] = _cap(n.render)
    return out or None


def _compose_facet(ix: Index, n: UNode, raw: bool) -> Any:
    if n.kind == "slot":
        sf = dict(n.facets.get("slot") or {})
        out: dict[str, Any] = {"name": sf.get("name") or n.type}
        if n.origin:
            out["origin"] = n.origin
        params = sf.get("params") or {}
        if params:
            if raw:
                out["params"] = _cap(params)
            else:
                brief = {}
                for k, v in params.items():
                    b = L.brief_param(k, v)
                    if b is not None:
                        brief[k] = b
                if brief:
                    out["params"] = brief
        if sf.get("mods"):
            out["mods"] = _cap(sf["mods"])
        sem = sf.get("sem") or []
        if sem:
            out["sem"] = [crumb(ix.nodes[s]) for s in sem if s in ix.nodes]
        return out
    c = n.facets.get("compose")
    if c is None and n.kind != "compose":
        return None
    c = c or {}
    out = {}
    attrs = c.get("attrs")
    if attrs:
        # Focused=false is the resting state of every focusable node (and is left
        # out of the fingerprint); Focused=true stays.
        shown = {k: v for k, v in attrs.items()
                 if not (k == "Focused" and str(v).strip().lower() == "false")}
        if shown:
            out["sem"] = _cap(shown)
    if c.get("actions"):
        out["actions"] = list(c["actions"])
    links = c.get("slots") or []
    if links:
        out["slots"] = [_slot_line(ix, ix.nodes[s], raw) for s in links if s in ix.nodes]
    elif not _has_slots(ix):
        out["slots"] = SLOTS_NOT_CAPTURED
    return out or None


def _text_facet(n: UNode) -> dict[str, Any] | None:
    out = {k: _cap(getattr(n, k)) for k in ("text", "desc", "state", "hint") if getattr(n, k)}
    return out or None


def _view_class(n: UNode) -> str | None:
    return (n.facets.get("view") or {}).get("class")


def _props_facet(ix: Index, n: UNode, mode: Any, props_fn: PropsFn | None) -> Any:
    if n.kind != "view" or "view" not in n.ids:
        return "not a View (properties belong to Views)"
    if ix.meta is not None and not ix.meta.options.props:
        return 'not captured: capture(props=true)'
    if props_fn is None:
        return "unavailable (no properties accessor)"
    values = props_fn(n)
    if values is None:
        return "unavailable for this view"
    names = list(values)
    if isinstance(mode, (list, tuple)):
        picked = L.match_names(names, list(mode))
        label = "names"
        kept = {k: values[k] for k in picked}
    elif mode == "all":
        label = "all"
        kept = dict(values)
    elif mode == "key":
        label = "key"
        fam = nz.class_family(_view_class(n), names)
        kept = {k: values[k] for k in nz.key_props_for(fam) if k in values}
    else:
        label = "nondefault"
        cls = _view_class(n) or ""
        views = [m for m in ix.nodes.values() if m.kind == "view" and "view" in m.ids]
        peers = [m for m in views if _view_class(m) == cls]
        vid = int(n.ids["view"])
        props = {vid: values}
        classes = {vid: cls}
        bounds = {vid: list(n.declared_b or n.b or [0, 0, 0, 0])}
        groups: dict[int, str] | None = None
        if len(peers) < 3:
            # A rare class: fall back to the majority of its family group (every
            # TextView-like view, every ViewGroup), bounded to keep this cheap.
            group = nz.family_group(nz.class_family(cls, values))
            groups = {vid: group}
            peers = []
            for m in views:
                if len(peers) >= _FAMILY_PEERS_MAX:
                    break
                if int(m.ids["view"]) == vid:
                    continue
                pv = props_fn(m)
                if pv is not None and nz.family_group(
                        nz.class_family(_view_class(m), pv)) == group:
                    peers.append(m)
                    groups[int(m.ids["view"])] = group
        for m in peers:
            mid = int(m.ids["view"])
            if mid == vid:
                continue
            pv = props_fn(m)
            if pv is not None:
                props[mid] = pv
                classes[mid] = _view_class(m) or ""
                bounds[mid] = list(m.declared_b or m.b or [0, 0, 0, 0])
        kept_all, _omitted = nz.nondefault_props(props, classes, bounds=bounds, groups=groups)
        kept = kept_all.get(vid, {})
    if label in ("key", "nondefault") and n.rid and "id" in kept:
        idv = L.plain_value(kept["id"])
        if isinstance(idv, str) and idv.rsplit("/", 1)[-1] == n.rid:
            kept = {k: v for k, v in kept.items() if k != "id"}  # the rid says it already
    return {"mode": label, "n": len(kept), "of": len(values), "values": _cap(kept)}


def _as_list(name: str, v: Any, choices: Sequence[str]) -> list[str]:
    items = _str_list(name, v)
    bad = [x for x in items if x not in choices]
    if bad:
        raise _bad(f"{name}: unknown {', '.join(bad)}", hint=f"{name}: {', '.join(choices)}.")
    return items


@dataclass
class _Part:
    node: int
    facet: str
    key: str
    value: Any
    detail: str = ""


def _node_parts(ix: Index, n: UNode, *, facets: Sequence[str], props_mode: Any, raw: bool,
                ancestors: bool, children: bool, props_fn: PropsFn | None, idx: int,
                image: Any, issue_fmt: Callable[[Issue], str] | None,
                multi_window: bool, implicit_props: bool = False
                ) -> tuple[list[_Part], list[_Part]]:
    """(core parts, optional parts in priority order) of one node's dossier."""
    core: list[tuple[str, Any]] = [("ref", n.id)]
    if n.sel and n.sel != n.id:
        core.append(("sel", n.sel))
    t = L.display_type(n)
    if t:
        core.append(("type", t))
    if n.label:
        core.append(("label", nz.cap(n.label, VALUE_MAX)))
    if n.b:
        core.append(("b", list(n.b)))
    extra: list[tuple[str, Any]] = [("key", n.key)]
    if n.rid and n.sel != "#" + L.ident(n.rid):
        extra.append(("rid", n.rid))
    if n.tag and n.sel != "@" + L.ident(n.tag):
        extra.append(("tag", n.tag))
    if n.role:
        extra.append(("role", n.role))
    fl = L.display_flags(n)
    if fl:
        extra.append(("flags", fl))
    dp = L.to_dp(n.b, L.capture_dpi(ix))
    if dp:
        extra.append(("dp", dp))
    if n.kind != "slot":
        tap, why = _tap_xy(ix, n)
        if tap:
            extra.append(("tap_xy", tap))
        else:
            extra.append(("tap", f"not tappable: {why}"
                          + (" (scroll it into view, then capture again)"
                             if why in ("offscreen", "outside its window") else "")))
    if n.stop is not None and not (n.facets.get("a11y") and "a11y" in facets):
        extra.append(("stop", n.stop))
    if n.src:
        extra.append(("src", n.src))
    if n.origin:
        extra.append(("origin", n.origin))
    if multi_window and n.window and n.window in ix.nodes:
        w = ix.nodes[n.window]
        extra.append(("window", crumb(w) + (f" z{w.z}" if w.z is not None else "")))
    vf = n.facets.get("view") or {}
    if vf.get("qualified") or vf.get("class"):
        cls = vf.get("qualified") or vf.get("class")
        if cls != t:
            extra.append(("class", cls))
    if vf.get("layout_res"):
        extra.append(("layout_res", vf["layout_res"]))
    if n.ids:
        extra.append(("ids", dict(n.ids)))
    if n.conf:
        extra.append(("conf", dict(n.conf)))
    for k in ("match", "since", "rebound_of"):
        v = getattr(n, k)
        if v and not (k == "since" and v == _cid(ix)):  # new in this capture: match says so
            extra.append((k, v))
    parent = ix.nodes.get(n.parent) if n.parent else None
    extra.append(("parent", crumb(parent, ix=ix) if parent is not None else None))
    if not children:
        extra.append(("children", len(n.children)))
    core_parts = [_Part(idx, "core", k, v) for k, v in core]
    core_parts += [_Part(idx, "core+", k, v) for k, v in extra]
    if image is not None:
        core_parts.append(_Part(idx, "core+", "image", image))

    opt: list[_Part] = []
    for facet in FACET_PRIORITY:
        if facet == "core":
            continue
        if facet == "issues" and "issues" in facets and n.issues:
            fmt = issue_fmt or issue_text
            opt.append(_Part(idx, "issues", "issues", [fmt(i) for i in _sorted_issues(n)],
                             f"({len(n.issues)})"))
        elif facet == "a11y" and "a11y" in facets:
            v = _a11y_facet(ix, n)
            if v is not None:
                opt.append(_Part(idx, "a11y", "a11y", v))
        elif facet == "layout" and "layout" in facets:
            v = _layout_facet(ix, n)
            if v is not None:
                opt.append(_Part(idx, "layout", "layout", v))
        elif facet == "compose" and "compose" in facets:
            v = _compose_facet(ix, n, raw)
            if v is not None:
                opt.append(_Part(idx, "compose", "slot" if n.kind == "slot" else "compose", v))
        elif facet == "text" and "text" in facets:
            v = _text_facet(n)
            if v is not None:
                opt.append(_Part(idx, "text", "text", v))
        elif facet == "props" and props_mode not in (None, "none"):
            if implicit_props and (n.kind != "view" or "view" not in n.ids):
                continue
            v = _props_facet(ix, n, props_mode, props_fn)
            detail = ""
            if isinstance(v, dict):
                detail = f"({v['mode']},{v['n']})"
            opt.append(_Part(idx, "props", "props", v, detail))
        elif facet == "children" and children and n.children:
            kids = [ix.nodes[c] for c in n.children if c in ix.nodes]
            opt.append(_Part(idx, "children", "children",
                             [render_line(ix, c, None, hidden=0) for c in kids],
                             f"({len(kids)})"))
        elif facet == "ancestors" and ancestors and n.parent:
            anc = list(reversed(ix.ancestors(n.id)))
            opt.append(_Part(idx, "ancestors", "ancestors", [crumb(a) for a in anc],
                             f"({len(anc)})"))
    return core_parts, opt


def _shrink(part: _Part, room: int) -> Any:
    """The largest prefix of a list/dict-valued part (props values, children,
    issues, slots) that costs at most ``room`` bytes, or None. A props prefix
    says how many values it holds (``n``), of how many (``of``) and how many
    more there are (``more``)."""
    key, v = part.key, part.value
    if key == "props" and isinstance(v, dict) and isinstance(v.get("values"), dict):
        items = list(v["values"].items())
        for k in range(len(items) - 1, 0, -1):
            cand = {**v, "n": k, "values": dict(items[:k]), "more": len(items) - k}
            if _kv_cost(key, cand) <= room:
                return cand
        return None
    if isinstance(v, list) and v:
        for k in range(len(v) - 1, 0, -1):
            cand = v[:k] + [f"+{len(v) - k} more"]
            if _kv_cost(key, cand) <= room:
                return cand
    return None


def _shrunk_detail(part: _Part, shown: Any) -> str:
    """The omitted entry of a props facet shown in part: ``props(all,+52 more)``
    (a cut list already ends with its own ``+N more`` item)."""
    if isinstance(shown, dict) and "more" in shown:
        return f"({shown.get('mode')},+{shown['more']} more)"
    return part.detail


def _call_for(ref: str, part: _Part, props_mode: Any, need: int) -> str:
    kw: dict[str, Any] = {"facets": part.facet}
    if part.facet == "props":
        kw = {"facets": "core", "props": props_mode}
    elif part.facet in ("children", "ancestors"):
        kw = {"facets": "core", part.facet: True}
    if need > DEFAULT_MAX_BYTES["node"]:
        kw["max_bytes"] = min(MAX_BYTES_CEILING, need)
    return call("node", ref, **kw)


def node(ix: Index, loaded: Any, refs: Any, **params: Any) -> dict[str, Any]:
    """``node(ref | refs[<=10], capture, facets="core,layout,a11y,compose,issues",
    props="none"|"key"|"nondefault"|"all"|[names/globs], params="brief"|"raw",
    ancestors=false, children=false, image=false, max_bytes=3000 (6000 batch))``.

    A dossier of one node, or ``{capture, nodes:[...]}`` for several (per-node
    errors are reported in place). Facets are added in priority order core >
    issues > a11y > layout > compose > text > props > children until the budget is
    used; the rest are listed in ``omitted`` with the call that fetches them.
    ``tap_xy`` is the centre of the visible bounds. ``image`` is filled by
    ``image_fn(node)`` when the ops layer passes one."""
    _check_unknown("node", params, _NODE_ARGS)
    sels = [refs] if isinstance(refs, str) else list(refs or [])
    if not sels:
        raise _bad("node needs a ref or selector, e.g. node(\"n23\")")
    if len(sels) > NODE_BATCH_MAX:
        raise _bad(f"node takes at most {NODE_BATCH_MAX} refs; got {len(sels)}")
    batch = len(sels) > 1
    raw_facets = params.get("facets")
    if raw_facets is not None and _str_list("facets", raw_facets) == ["all"]:
        facets = list(FACETS)
    else:
        facets = _as_list("facets", raw_facets, FACETS + ("all",)) if raw_facets else list(
            DEFAULT_FACETS)
        if "all" in facets:
            facets = list(FACETS)
    pm = params.get("props")
    if pm is None or pm == "":
        props_mode: Any = "none"
    elif isinstance(pm, str) and pm in PROPS_MODES:
        props_mode = pm
    else:
        names = _str_list("props", pm)
        if not names:
            raise _bad("props must be none, key, nondefault, all or a list of names")
        props_mode = names
    implicit_props = False
    if props_mode == "none" and raw_facets is not None and "props" in facets:
        props_mode, implicit_props = "nondefault", True
    raw = _enum("params", params.get("params"), PARAMS_MODES, "brief") == "raw"
    explicit = raw_facets is not None
    ancestors = _bool("ancestors", params.get("ancestors"), False) or (
        explicit and "ancestors" in facets)
    children = _bool("children", params.get("children"), False) or (
        explicit and "children" in facets)
    want_image = _bool("image", params.get("image"), False)
    image_fn = params.get("image_fn")
    issue_fmt = params.get("issue_fmt")
    max_bytes = resolve_max_bytes(params.get("max_bytes"),
                                  DEFAULT_MAX_BYTES["node_batch" if batch else "node"])
    props_fn = _props_source(params, loaded) if props_mode != "none" else None
    tomb = params.get("tomb")
    cid = _cid(ix)
    multi_window = len(ix.windows()) > 1

    resolved: list[tuple[str, UNode | None, OpError | None]] = []
    for s in sels:
        try:
            resolved.append((s, resolve_selector(ix, s, tomb=tomb), None))
        except OpError as e:
            if not batch:
                raise
            resolved.append((s, None, e))

    docs: list[dict[str, Any]] = []
    cores: list[list[_Part]] = []
    opts: list[list[_Part]] = []
    for i, (s, n, err) in enumerate(resolved):
        if n is None:
            docs.append({"sel": s, **err.to_dict()})
            cores.append([])
            opts.append([])
            continue
        image = None
        if want_image and callable(image_fn):
            image = image_fn(n)
        c, o = _node_parts(ix, n, facets=facets, props_mode=props_mode, raw=raw,
                           ancestors=ancestors, children=children, props_fn=props_fn, idx=i,
                           image=image, issue_fmt=issue_fmt, multi_window=multi_window,
                           implicit_props=implicit_props)
        docs.append({})
        cores.append(c)
        opts.append(o)

    omitted: list[list[tuple[_Part, int]]] = [[] for _ in resolved]
    shrunk: dict[int, str] = {}  # id(part) -> its omitted detail when a prefix is shown

    def envelope(long_omitted: bool | str = True, with_next: bool = True) -> dict[str, Any]:
        outs = []
        for i, d in enumerate(docs):
            doc = dict(d)
            if omitted[i]:
                ref = doc.get("ref", "")
                entries = []
                if long_omitted == "count":
                    entries = [f"{len(omitted[i])} facet{'s' if len(omitted[i]) > 1 else ''}: "
                               "raise max_bytes"]
                for part, need in omitted[i] if long_omitted != "count" else ():
                    head = f"{part.facet}{shrunk.get(id(part), part.detail)}"
                    entries.append(f"{head}: {_call_for(ref, part, props_mode, need)}"
                                   if long_omitted else head)
                doc["omitted"] = entries
            outs.append(doc)
        env: dict[str, Any] = {"capture": cid}
        if batch:
            env["nodes"] = outs
        else:
            env.update(outs[0])
        if with_next:
            nx = _node_next(ix, resolved, omitted, props_mode)
            if nx:
                env["next"] = nx
        return env

    def size(**kw: Any) -> int:
        return utf8_len(dumps(envelope(**kw)))

    # 1) core parts (always); 2) optional parts round-robin by priority, keeping
    #    room for a short omitted entry for every part still undecided; 3) a refill
    #    pass; 4) fix-ups that guarantee the budget whatever happens.
    for i, parts in enumerate(cores):
        for p in parts:
            docs[i][p.key] = p.value
    pending = [(i, p) for facet in FACET_PRIORITY[1:] for i, parts in enumerate(opts)
               for p in parts if p.facet == facet]

    def entry_cost(p: _Part) -> int:
        return utf8_len(dumps(f"{p.facet}{p.detail}")) + 1

    slack = 80  # the omitted key itself and a next hint that names an omitted part
    reserve = sum(entry_cost(p) for _, p in pending) + slack
    for i, p in pending:
        reserve -= entry_cost(p)
        docs[i][p.key] = p.value
        used = size(long_omitted=False)
        if used + reserve <= max_bytes:
            continue
        del docs[i][p.key]
        cost = _kv_cost(p.key, p.value)
        room = max_bytes - (used - cost) - reserve - entry_cost(p)
        smaller = _shrink(p, room) if room > 40 else None
        if smaller is not None:
            docs[i][p.key] = smaller
            shrunk[id(p)] = _shrunk_detail(p, smaller)
        omitted[i].append((p, used + 200))
    for i, p in pending:  # refill: an omitted part may fit now that the set is final
        entry = next((e for e in omitted[i] if e[0] is p), None)
        if entry is None:
            continue
        before = docs[i].get(p.key)
        omitted[i].remove(entry)
        docs[i][p.key] = p.value
        if size(long_omitted=False) > max_bytes:
            if before is None:
                del docs[i][p.key]
            else:
                docs[i][p.key] = before
            omitted[i].append(entry)
        else:
            shrunk.pop(id(p), None)
    long_form, with_next = True, True
    if size(long_omitted=True) > max_bytes:
        long_form = False
    for i in range(len(docs) - 1, -1, -1):
        for p in reversed(opts[i]):
            if p.key in docs[i] and size(long_omitted=long_form) > max_bytes:
                del docs[i][p.key]
                shrunk.pop(id(p), None)
                if not any(q is p for q, _ in omitted[i]):
                    omitted[i].append((p, max_bytes + _kv_cost(p.key, p.value) + 200))
    if size(long_omitted=long_form, with_next=with_next) > max_bytes:
        with_next = False
    for i in range(len(docs) - 1, -1, -1):
        for p in reversed(cores[i]):
            if p.facet == "core+" and p.key in docs[i] and \
                    size(long_omitted=long_form, with_next=with_next) > max_bytes:
                del docs[i][p.key]
    if size(long_omitted=long_form, with_next=with_next) > max_bytes:
        for i in range(len(docs)):
            if isinstance(docs[i].get("label"), str):
                docs[i]["label"] = L.cut(docs[i]["label"])
        long_form = "count"  # still say what was left out, without the calls
    need = size(long_omitted=long_form, with_next=with_next)
    if need > max_bytes:
        # never drop the "something is missing" markers to squeeze under the budget:
        # the core of these nodes does not fit, so say what would
        raise _bad(f"node: the core fields of {len(sels)} node{'s' if batch else ''} take "
                   f"{need} bytes, over max_bytes={max_bytes}",
                   hint=f"Pass max_bytes={min(MAX_BYTES_CEILING, need + 50)} or fewer refs.")
    return envelope(long_omitted=long_form, with_next=with_next)


def _node_next(ix: Index, resolved: Sequence[tuple[str, UNode | None, OpError | None]],
               omitted: Sequence[Sequence[tuple[_Part, int]]], props_mode: Any) -> list[str]:
    hints: list[str | None] = []
    for i, (_s, n, _e) in enumerate(resolved):
        if n is None:
            continue
        if _wants_image(ix, n):
            hints.append(call("image", ref=n.id))
        # (capture(slots="enable") is destructive: never a next hint; the compose
        # facet says how to get the slot table and what it costs)
        if omitted[i]:
            part, need = omitted[i][0]
            hints.append(_call_for(n.id, part, props_mode, need))
    return next_hints(hints)


__all__ = [
    "ACTIONABLE",
    "CHAIN_MAX",
    "DEFAULT_FACETS",
    "DEFAULT_MAX_BYTES",
    "FACETS",
    "FACET_PRIORITY",
    "FIND_DOMAINS",
    "FIND_SORTS",
    "HAS_TERMS",
    "MAX_BYTES_CEILING",
    "MIN_MAX_BYTES",
    "OUTLINE_VIEWS",
    "PROPS_MODES",
    "SELECTOR_EXAMPLES",
    "TOOL_LETTERS",
    "Atom",
    "Page",
    "Selector",
    "args_hash",
    "assemble",
    "at_point",
    "call",
    "cursor_call",
    "cursor_capture",
    "find",
    "is_collection",
    "issue_text",
    "make_cursor",
    "next_hints",
    "node",
    "outline",
    "pack",
    "parse_cursor",
    "parse_selector",
    "render_line",
    "resolve_max_bytes",
    "resolve_selector",
    "select",
    "type_names",
    "visible_rect",
]
