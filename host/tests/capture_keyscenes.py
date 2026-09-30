"""Key-space capture scenes for the carry-over (refs) and diff tests.

``tests/capture_builders.py`` builds indexes that already carry refs, which is
what the query modules consume. Carry-over consumes the other form: an index
keyed by canonical key with no refs, as ``index.build_index`` returns it. This
module builds those from a small nested description::

    ix = scene(
        V("DecorView", 1,
          V("LinearLayout", 2,
            V("TextView", 3, label="Title", b=(0, 0, 100, 40)),
            V("RecyclerView", 4, rid="feed",
              V("LinearLayout", 10, V("TextView", 11, label="Item 0")),
              V("LinearLayout", 20, V("TextView", 21, label="Item 1"))))),
        cid="c00001", pid=100)

and ``Chain`` publishes a sequence of such scenes the way the store does (refs
assigned against the previous capture, then applied), so tests get the ref-space
indexes a diff compares.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from inspector_widget.capture import model as m
from inspector_widget.capture import refs

SERIAL = "emulator-5554"
PACKAGE = "com.example.app"

_FIELDS = ("rid", "tag", "label", "text", "desc", "state", "hint", "role", "flags", "stop",
           "src", "origin", "anchor", "adapter_pos", "visible", "declared_b")


def V(type_: str, udid: int, *kids: dict, **kw: Any) -> dict:
    """A View node (key ``view:<udid>``). ``cls`` defaults to the type."""
    return {"kind": "view", "type": type_, "udid": int(udid), "kids": list(kids), **kw}


def C(sem_id: int, *kids: dict, type_: str | None = None, **kw: Any) -> dict:
    """A Compose semantics node (key ``sem:<acv>:<id>``, acv from the nearest
    AndroidComposeView ancestor unless ``acv=`` is given)."""
    return {"kind": "compose", "type": type_, "sem": int(sem_id), "kids": list(kids), **kw}


def S(name: str, anchor: str, *kids: dict, acv: int = 0, **kw: Any) -> dict:
    """A slot-table group (key ``slot:<acv>:<hash(anchor)>``)."""
    return {"kind": "slot", "type": name, "anchor_s": anchor, "acv": acv, "kids": list(kids), **kw}


def A(host: int, virt: int, *kids: dict, type_: str | None = None, **kw: Any) -> dict:
    """An a11y-only node (key ``a11y:<host>:<virt>``)."""
    return {"kind": "a11y", "type": type_, "host": host, "virt": virt, "kids": list(kids), **kw}


def _key(spec: Mapping, acv: int | None) -> str:
    kind = spec["kind"]
    if kind == "view":
        return m.view_key(spec["udid"])
    if kind == "compose":
        a = spec.get("acv", acv)
        if a is None:
            raise ValueError("compose node needs an AndroidComposeView ancestor or acv=")
        return m.sem_key(a, spec["sem"])
    if kind == "slot":
        return m.slot_key(spec.get("acv", 0), spec["anchor_s"])
    return m.a11y_key(spec["host"], spec["virt"])


def scene(*roots: dict, slots: tuple[dict, ...] = (), cid: str = "c00001", pid: int = 100,
          gen: int = 0, serial: str = SERIAL, package: str = PACKAGE,
          created_at: float = 1_790_000_000.0, lint: str = "tree") -> m.Index:
    """A key-space Index (no refs) from nested V/C/S/A specs. Window roots are
    the top-level ``roots`` (z in order); ``slots`` are slot-tree roots."""
    meta = m.CaptureMeta(id=cid, lineage=(serial, package), pid=pid, compose_generation=gen,
                         created_at=created_at, options=m.CaptureOptions(lint=lint))
    nodes: dict[str, m.UNode] = {}
    ui_roots: list[str] = []
    slot_roots: list[str] = []
    aliases: dict[str, str] = {}

    def add(spec: Mapping, parent: str | None, depth: int, window: str | None,
            acv: int | None) -> str:
        key = _key(spec, acv)
        if key in nodes:
            raise ValueError(f"duplicate key {key}")
        kind = spec["kind"]
        facets: dict[str, dict] = {k: dict(v) for k, v in (spec.get("facets") or {}).items()}
        ids: dict[str, Any] = {}
        if kind == "view":
            facets.setdefault("view", {"class": spec.get("cls") or spec["type"]})
            ids["view"] = spec["udid"]
            if (spec.get("cls") or spec["type"]) == "AndroidComposeView":
                acv = spec["udid"]
        elif kind == "compose":
            cf = facets.setdefault("compose", {})
            if spec.get("attrs") is not None:
                cf["attrs"] = dict(spec["attrs"])
            ids["sem"] = key[4:]
        elif kind == "slot":
            sf = facets.setdefault("slot", {"name": spec["type"]})
            if spec.get("params") is not None:
                sf["params"] = dict(spec["params"])
        else:
            ids["a11y"] = f"{spec['host']}:{spec['virt']}"
        if spec.get("a11y") is not None:
            facets["a11y"] = dict(spec["a11y"])
        node = m.UNode(key=key, kind=kind, type=spec.get("type"), parent=parent, depth=depth,
                       ids=ids, facets=facets,
                       b=[int(v) for v in spec["b"]] if spec.get("b") is not None else None)
        for f in _FIELDS:
            if f in spec:
                v = spec[f]
                setattr(node, f, list(v) if isinstance(v, tuple) else v)
        if kind == "slot":
            node.anchor = spec["anchor_s"]
        for rule in spec.get("issues") or ():
            rid, sev = (rule, "warn") if isinstance(rule, str) else rule
            node.issues.append(m.Issue(id=rid, sev=sev))
        if parent is None and kind != "slot":
            node.z = len(ui_roots)
            ui_roots.append(key)
            window = key
            aliases[m.window_key(spec["udid"])] = key
        elif parent is None:
            slot_roots.append(key)
        node.window = window
        nodes[key] = node
        for k in spec.get("kids") or ():
            node.children.append(add(k, key, depth + 1, window, acv))
        return key

    for r in roots:
        add(r, None, 0, None, None)
    for r in slots:
        add(r, None, 0, None, None)

    def tree(members: set[str], roots_: list[str]) -> m.Tree:
        t = m.Tree()

        def walk(nid: str, anchor: str | None) -> None:
            if nid in members:
                if anchor is None:
                    t.roots.append(nid)
                else:
                    t.children.setdefault(anchor, []).append(nid)
                anchor = nid
            for c in nodes[nid].children:
                walk(c, anchor)

        for r in roots_:
            walk(r, None)
        return t

    all_ui = {k for k, n in nodes.items() if n.kind != "slot"}
    trees = {
        "ui": tree(all_ui, ui_roots),
        "views": tree({k for k in all_ui if nodes[k].kind == "view"}, ui_roots),
        "compose": tree({k for k in all_ui if nodes[k].kind == "compose"}, ui_roots),
        "slots": tree({k for k, n in nodes.items() if n.kind == "slot"}, slot_roots),
        "a11y": tree({k for k in all_ui if nodes[k].kind == "a11y" or "a11y" in nodes[k].facets},
                     ui_roots),
    }
    order = []
    for r in ui_roots + slot_roots:
        stack = [r]
        while stack:
            nid = stack.pop()
            order.append(nid)
            stack.extend(reversed(nodes[nid].children))
    by_key = {k: k for k in order}
    by_key.update(aliases)
    ix = m.Index(meta=meta, nodes={k: nodes[k] for k in order}, trees=trees, reading=[],
                 by_key=by_key, diagnostics=[])
    # a stand-in sel, like capture_builders: @tag, #rid when unique, else None (-> ref)
    tags: dict[str, int] = {}
    rids: dict[str, int] = {}
    for n in ix.nodes.values():
        if n.tag:
            tags[n.tag] = tags.get(n.tag, 0) + 1
        if n.rid:
            rids[n.rid] = rids.get(n.rid, 0) + 1
    for n in ix.nodes.values():
        if n.tag and tags[n.tag] == 1:
            n.sel = f"@{n.tag}"
        elif n.rid and rids[n.rid] == 1:
            n.sel = f"#{n.rid}"
    return ix


def key_space(ix: m.Index) -> m.Index:
    """A ref-space index (e.g. from capture_builders) re-keyed by canonical key, refs cleared."""
    out = m.remap_ids(ix, {n.ref: n.key for n in ix.nodes.values() if n.ref}, set_refs=False)
    for n in out.nodes.values():
        n.ref = None
        if n.sel and m.is_ref(n.sel):
            n.sel = None
    return out


def rekey(ix: m.Index, fn: Callable[[str], str]) -> m.Index:
    """A key-space index with every canonical key (and alias) passed through ``fn``,
    e.g. to simulate a process restart (new udids)."""
    mapping = {k: fn(k) for k in ix.nodes}
    out = m.remap_ids(ix, mapping, set_refs=False)
    for n in out.nodes.values():
        n.key = mapping.get(n.key, n.key)
    aliases = {}
    for k, v in ix.by_key.items():
        if k in ix.nodes:
            continue
        aliases[fn(k) if m.is_key(k) else k] = mapping.get(v, v)
    out.by_key = {n.key: nid for nid, n in out.nodes.items()}
    out.by_key.update(aliases)
    return out


def shift_udids(delta: int) -> Callable[[str], str]:
    """A ``rekey`` function that adds ``delta`` to every udid in a key."""
    def fn(key: str) -> str:
        parsed = m.parse_key(key)
        if parsed is None:
            return key
        kind, parts = parsed
        if kind == "view":
            return m.view_key(parts[0] + delta)
        if kind == "w":
            return m.window_key(parts[0] + delta)
        if kind == "sem":
            return m.sem_key(parts[0] + delta, parts[1])
        if kind == "a11y":
            return m.a11y_key(parts[0] + delta, parts[1])
        if kind == "slot":
            return f"slot:{parts[0] + delta}:{parts[1]}"
        return key
    return fn


class Counter:
    """The store's global ref counter, as ``alloc`` sees it."""

    def __init__(self, start: int = 1) -> None:
        self.next = start
        self.calls: list[int] = []

    def __call__(self, n: int) -> int:
        self.calls.append(n)
        start = self.next
        self.next += n
        return start


class Chain:
    """Publishes key-space scenes of one lineage in sequence, like the store:
    assign against the previous capture, apply the refmap, keep tombstones."""

    def __init__(self, start: int = 1) -> None:
        self.alloc = Counter(start)
        self.prev: m.Index | None = None
        self.tomb: dict[str, list] = {}
        self.history: list[m.Index] = []
        self.last: refs.Assignment | None = None

    def publish(self, new: m.Index, *, same_pid: bool | None = None,
                same_generation: bool | None = None) -> m.Index:
        sp, sg = refs.identity_flags(new.meta, self.prev.meta if self.prev else None)
        sp = sp if same_pid is None else same_pid
        sg = sg if same_generation is None else same_generation
        self.last = refs.plan(new, self.prev, same_pid=sp, same_generation=sg, alloc=self.alloc)
        refs.annotate(new, self.last)
        out = m.remap_ids(new, self.last.refmap)
        self.tomb = refs.merge_tomb(self.tomb, self.last.tomb)
        self.prev = out
        self.history.append(out)
        return out

    def ref(self, key: str, ix: m.Index | None = None) -> str:
        """The ref a canonical key got in ``ix`` (default: the latest capture)."""
        ix = ix or self.prev
        return ix.by_key[key]
