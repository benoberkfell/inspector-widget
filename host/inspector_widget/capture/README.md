# `inspector_widget.capture`: capture once, walk it with small queries

An LLM agent should not get megabytes of dump. A **capture** fetches one moment of
one app once (views and properties, Compose semantics and slot table, the unified
accessibility tree, one screenshot per window), keeps it on disk under an id such as
`c7h2kq`, and every later question is a small, budgeted query against that record:
`outline`, `find`, `node`, `image`, `lint`, `diff`. The queries never touch the device.

Spec: "Capture and Walk" (sections 3-7 and the section 10 contracts).
Implementation decisions beyond the spec, module by module, are in
[`CONTRACT_NOTES.md`](CONTRACT_NOTES.md). This package is pure library code. It is not
wired to the CLI or the MCP server yet (S1 and S2 do that); the output layer
(`output.py`, `normalize*.py`) is, since P0-2.

## Module map

| Module | WP | Role |
|---|---|---|
| `model.py` | C1 | `CaptureOptions`, `CaptureMeta`, `RawCapture`, `UNode`, `Tree`, `Index`, `LineageState`, `OpError`; canonical keys (`view:`, `sem:`, `slot:`, `a11y:`, `w:`), refs (`n23`), `remap_ids`, `index.jsonl(.gz)`, store root |
| `store.py` | C2 | `CaptureStore`: atomic publish, ids, labels, `resolve()`, retention and GC, the ref counter, lineage files, the memory LRU; `LoadedCapture` reads a published capture |
| `fetch.py` | C3 | `fetch(session, opts)` -> `RawCapture`: the facet registry, request order, fingerprint re-check, `settle`, `if_changed_since` |
| `index.py` | C4 | `build_index(raw)` -> key-space `Index` (ui, views, compose, slots, a11y trees; correlation; ID1 detector); `apply_refs`; `FacetReader` (lazy props and params) |
| `anchors.py` | C4 | anchors, template anchors, collection positions, `sel` locators, a reference selector matcher |
| `refs.py` | C5 | `assign(new, prev, ...)`: carry refs across captures of one lineage, tombstones |
| `query.py`, `lines.py` | C6 | selectors, `outline`, `find`, `node`; line grammar v1, field projection, cursors, `next` hints, `resolve_max_bytes` |
| `analyzers.py`, `rules.py` | C7 | `analyze` (render signals, lint adapter, reading order), `lint_view`, `lint_summary`; the rule catalog |
| `diff.py` | C8 | `diff(a, b)` by ref: changed, moved, added, removed, rebound; "new screen" verdict |
| `images.py` | C9 | per-window crops, Set-of-Mark/lint/reading overlays, inline images, pixel diff |
| `../output.py`, `../normalize*.py` | P0-1 | compact JSON, `Budget`, spill envelope, brief slimming, value normalization |

Importing the package root loads only `model`: no protobuf, no Pillow.

## Data flow

```
session --fetch--> RawCapture --build_index--> Index (canonical keys, ref=None)
                                      |
                              analyze (issues, stops, reading)       <- no lock
                              prev = store.load(latest).index()      <- no lock
                                      |
         with store.refs_lock():   (milliseconds: hold it for these three only)
             refs.assign(ix, prev, alloc=store.next_refs) -> refmap, tomb
             ix = index.apply_refs(ix, refmap)           (ids become refs)
             cid = store.publish(raw, ix, refmap, tomb=tomb)
                                      |
store.load(cid) -> LoadedCapture --.index()--> outline / find / node / lint / image / diff
```

Keep `analyze` and the previous index load **outside** the lock: `store.lock`
serializes every publish, label, pin, drop and GC eviction of every process, and
`lint="full"` spends about 4 s on contrast. `apply_refs` carries issues, stops,
`reading` and the node ids inside issue evidence into ref space. Under the lock,
re-read `store.lineage_state(serial, package).latest` and reload `prev` only when
another process published meanwhile. `tests/test_capture_pipeline_offline.py`
(`Pipeline.capture`) is the reference implementation of this order.

## Invariants

- **Immutable captures.** Raw facets (`raw/*.pb`, `shot/w_<root>.pb`) are the source
  of truth, written once into `.staging/` and published by one rename after
  `.complete`. `meta.json` is never rewritten: labels live in the lineage file,
  pins in a `.pinned` marker. Derived artifacts (index, lint caches, PNGs) are
  rebuildable and written temp-then-replace, keyed by their inputs. Deletion
  renames into `.trash/` first, so a reader sees `capture_not_found`, never half a
  capture. `refmap.json` only grows: a rebuild adds refs for keys it lacked.
- **Refs are never reused.** One store-global counter (`store.json`), reset only by
  `gc(all=True)`. A ref belongs to one lineage (serial + package). Carry-over keeps
  a ref only for the same node; anything ambiguous gets a new ref, and a recycled
  list cell gets a new ref with `rebound_of` (the collection guard: identity
  label, adapter position, the list's scroll shift). A ref that left the screen is
  a tombstone and answers `ref_not_in_capture` with where it was last seen.
- **Pixels match the tree, per window.** Every crop and overlay is cut from the
  capture's own screenshot of the node's window; bounds are visible screen px.
  `query.visible_rect` is the on-screen part: `tap_xy` is its centre, and
  `image(ref)` is only suggested when it exists.
- **Budgets are explicit.** Every tool has a default `max_bytes` (outline 6,000,
  find/node 3,000, a node batch 6,000, lint/diff 4,000); `query.resolve_max_bytes`
  is the one rule (0 = the 32,000 ceiling, else 500..32,000). Anything cut is
  counted (`truncated`, `hidden`, `omitted`, `more`) and says how to get it.
  Pages always advance. A cursor (`<capture>:<letter>:<hash8>:<offset>`) is
  stateless, and its `next` hint repeats every non-default argument, so hints run
  verbatim (`tests/capture_hints.py`). At most 3 hints, 200 B. Destructive calls
  (`capture(slots="enable")`) are never hints.
- **One line grammar.** Outline, find, lint examples, diff lines and error
  candidates are grammar-v1 lines (`lines.LINE_RE`); `format="json"` rows carry
  the same fields. A `sel` always parses as a selector.

## How the wiring packages call in

**P0-2 (legacy tools, both surfaces; done)** uses only the output layer, as
`mcp_server._render_result` and `cli._emit_result` do:

```python
brief = output.slim(tool, result, args)                      # detail="full" is identity
text = output.finalize(tool, brief, max_bytes=args.get("max_bytes"),
                       spill_dir=None)                        # or store.spill_dir()
output.augment_schemas(TOOLS)                                 # MCP schemas
output.add_cli_flags(subparser, tool)                         # CLI flags, same defaults
```

`spill_dir=None` means `<store root>/spill`, or a private temp directory when
`INSPECTOR_WIDGET_CAPTURE_PERSIST=0`, so memory-only mode never spills into the
persistent cache.

**S1 (`ops.py`)** owns sessions, the capture pipeline and the tool functions:

- One `CaptureStore()` per process (`persist` honours the environment). Resolve the
  `capture` argument with `store.resolve(spec, lineage)` and read with
  `store.load(cid)`; both surfaces share that resolver.
- Capture in the order of the data-flow diagram. Track `compose_generation` per
  session (bump it after `slots="enable"`) and pass `refs.identity_flags(new.meta,
  prev.meta)` to `assign`. Pass `rebuild=` only if it keeps the default's rules
  (no contrast while the lock is held).
- Queries: `query.outline(ix, loaded=lc, tomb=st.tomb, **args)`, `query.find(...)`,
  `query.node(ix, lc, refs, tomb=..., image_fn=...)`, `analyzers.lint_view(ix, lc,
  **args)`, `images.crop(lc, node, ...)` / `images.overlay(lc, ix, kind, ...)`,
  `diff.diff(ia, ib, props_a=..., props_b=..., resolve=query.resolve_selector,
  preview=..., pixel_diff=...)`. `tomb` is `store.lineage_state(*lineage).tomb`;
  without it `ref_not_in_capture` cannot say where a ref was last seen.
- Map `OpError` to the error envelope with `err.to_dict()`, and add the staleness
  markers (`age_s`, `stale`, `pid_changed`). `query.cursor_capture(cursor)` names
  the capture a cursor belongs to, so resolve that one first.
- `analyzers.lint_summary(ix)` gives the capture summary's `lint`/`issues` lines;
  `Pipeline.summary` in the pipeline test is a stand-in for the capture response.

**S2 (`surface.py`)** generates both surfaces from one registry of `ToolSpec`s whose
functions are S1's. Validation happens once there (`bad_args`); the library
functions also validate and raise `OpError("bad_args")`, so either layer is safe.
Per-tool defaults to copy into the specs: `query.DEFAULT_MAX_BYTES`,
`analyzers.LINT_MAX_BYTES`/`LINT_LIMIT`/`PER_RULE`, `diff.DEFAULT_MAX_BYTES`/
`DEFAULT_LIMIT`/`MIN_MOVE_PX`. The `next` hints use MCP argument names (`in=`
for find's domain); the CLI renders the same calls as hints, not flags.

## Tests

`cd host && PYTHONPATH=. .venv/bin/python -m pytest tests -q -m "not device"`.
The `tests/test_capture_*.py` files cover each module; `test_capture_pipeline_offline.py`
runs the whole pipeline over the recorded launcher and View-screen replays, the
259-view wide scene and a mixed View/Compose/dialog scene, checks every default
response against its budget, and runs every emitted hint.
