# Capture package: contract notes

Decisions taken where the spec ("Capture and Walk", section 10) was ambiguous. The
parallel work packages (C2-C9, S1-S3) code against these. Change them only together
with every consumer.

## Model (C1, `capture/model.py`)

- **Id spaces.** A node's *id* is its ref once refs are applied, else its canonical
  key. `Index.nodes` is keyed by id, and ids are what `UNode.parent/children/window`,
  every `Tree`, and `Index.reading` hold. `Index.by_key` maps canonical keys and
  aliases (`w:<udid>`, legacy `compose:<id>` when unique) to ids.
  `build_index()` (C4) returns a key-space index (`ref=None`).
  `apply_refs(ix, refmap)` is `model.remap_ids(ix, refmap)`, where refmap maps keys to refs.
  `remap_ids` also sets `UNode.ref`. The copy it returns shares no mutable state
  with its input.
- **Node links inside facets** use ids and must sit under a name in
  `model.REF_FIELDS`, so that `remap_ids` rewrites them:
  - `a11y.labeled_by/label_for/traversal/traversal_before/traversal_after`
  - `compose.slots`
  - `slot.sem`

  To add a new link, extend the table. `rebound_of` names an old capture's ref and
  is never remapped.
- **Window roots.** The spec's "View node flagged `window:true`" is `UNode.z`: an int
  on window roots, None elsewhere. `UNode.is_window` is `z is not None`.
  `UNode.window` holds the window root's id; a root points at itself.
- **Primary tree.** `parent`, `children` and `depth` place a node in its primary
  tree: `ui` for view, compose and a11y nodes, `slots` for slot nodes. Slot nodes
  take `window` from the semantics node they link to, and it is None when they are
  unlinked.
- **Keys.**
  - `slot:<acv>:<hash8>`, where hash8 is `blake2s(anchor, digest_size=4).hexdigest()`.
  - The ID1 fallback key is `a11y:path:<rootUdid>:<0.i.j>`, where the path starts
    with `0` for the root.
  - Negative ints are allowed, e.g. `a11y:<host>:-1` for a View's own node.
- **`Issue`**: `{id, sev, evidence, conf}`. `conf` defaults to `exact`. The rule
  catalog in C7 validates ids; the model accepts any id.
- **`LineageState` lives in `model.py`.** refs.py (C5) and store.py (C2) import it
  from there. `lineage_file_name(serial, package)` gives the file name, with
  path-hostile characters replaced.
- **`CaptureMeta`.**
  - Field order follows section 10. Everything after `lineage` has a default.
    `agent_build` (spec 3.1) is appended after `schema`.
  - `options` is a `CaptureOptions`, written to JSON as a dict.
  - `lineage` is a `(serial, package)` tuple, written to JSON as
    `{"serial","package"}`.
  - `set_facet()` validates the status vocabulary.
- **`RawCapture`.**
  - An unfetched facet is `b""` for the required ones and `None` for `slots` and
    `a11y_render`. `meta.facets` says why.
  - On-disk paths come only from `RAW_FILES`, `shot_file(root)` and `skp_file(root)`.
    Use `RawCapture.files()` and `RawCapture.from_files()`; the store (C2) and
    fetch (C3) must not invent paths.
- **`index.jsonl(.gz)`.**
  - `index_to_jsonl(ix, compress=False)` writes a header line, then one node per
    line in pre-order.
  - The header holds `index` (schema), `capture`, `count`, `trees`, `reading`,
    `diagnostics` and `aliases`.
  - `index_from_jsonl` detects gzip. It raises `ValueError` on a schema mismatch
    or truncation, and the store rebuilds the index from the raw facets when that
    happens.
  - Nodes omit fields that equal their defaults.
- **Store root.** `model.default_store_root()` implements spec 4.1. The store (C2)
  uses it for `root=None`, and Phase-0 spill files go to `<root>/spill`
  (`output.default_spill_dir()`).
- **`OpError.to_dict()`** always has `hint` (null if none). It has `candidates`
  only when given. An unknown code raises `ValueError`.
- **Test builders** live in `tests/capture_builders.py`:
  - `IndexBuilder`
  - `launcher_index()`: the spec's section 5 example (n1..n25, n301..n305)
  - `wide_index()`: 259 views
  - `big_index(n)`

  The builder's `sel` is a stand-in (`@tag` or `#rid` when unique, else the ref).
  The real `sel` is computed in C4.

## Store (C2, `capture/store.py`)

- **Constructor.** `CaptureStore(root=None, persist=None, clock=time.time, *, env,
  ttl_s, max_captures, max_mb, max_bytes, lineage_cap, max_pinned, mem_indexes,
  mem_bytes, durable, gc_on_publish, rebuild, new_id)`.
  - `persist=None` (the section 10 default was `True`) reads
    `INSPECTOR_WIDGET_CAPTURE_PERSIST`. An explicit `True` or `False` wins.
  - In memory-only mode `root` is a private temp directory. `close()` removes
    it, and so does exit. `configured_root` still names the configured one.
  - The keyword-only arguments override the environment and the spec defaults.
    Tests use them; production code should not need them.
  - The TTL is at least 60 s. A bare number in `INSPECTOR_WIDGET_CAPTURE_TTL`
    means seconds; `s`, `m`, `h` and `d` suffixes also work.
- **Locking.** `refs_lock()` is `store.lock`: an flock that is re-entrant for
  the thread holding it. S1 holds it across `next_refs`, `refs.assign` and
  `publish`. `publish` and `save_lineage_state` take it themselves as well. The
  GC that a publish requests runs when the outermost `refs_lock` is released. It
  is best-effort and never fails the publish.
- **`publish(raw, ix, refmap, *, tomb=None) -> id`.**
  - It mutates `raw.meta`: `id`, `created_at` (when 0) and `prev` (when None; it
    becomes the lineage's latest).
  - It applies `meta.label`, moving it silently from another capture of the
    lineage. Call `label()` after publishing to learn `moved_from`.
  - It honours `meta.pinned`: `bad_args` when 20 are pinned already.
  - It merges `tomb` updates. Refs present in `refmap` leave the tomb, which is
    capped at 5,000 with the oldest dropped first.
  - It sets the default session and caches the index in memory.
  - If the id was taken between staging and rename, it re-ids and rewrites
    meta.json and the index.
- **Sources of truth.** Labels live in the lineage file (a capture has at most
  one) and pins in a `.pinned` marker. `meta.json` is written once. `load()` and
  `list()` overlay the current `label` and `pinned`. `latest` and `prev` are
  reserved and can never be labels.
- **Lineage files** also carry `serial` and `package`, because file names are
  sanitized. `lineage_state()` remembers its lineage, so
  `save_lineage_state(st)` needs no arguments. `save_lineage_state(st, serial,
  package)` also works. Save before `publish`, or re-read after it, because
  publish rewrites `latest` and `history`.
- **`resolve(spec, lineage)`.**
  - `latest`, `prev` and `latest~N` walk the lineage's `history`, then any older
    captures of the lineage (such as pinned ones past the 50-entry history) by
    `created_at`. Without a lineage they walk the whole store by `created_at`.
  - A lineage that has no captures gives `capture_not_found`. There is no
    store-wide fallback, so another app's capture is never returned.
  - Labels resolve as in spec 4.2. `ambiguous` candidates read `"<id>
    <serial>/<package>"`.
  - An empty or None spec means `latest`.
- **`load(cid)`** takes ids only (case-insensitive). Anything else is
  `bad_args`: resolve it first. A load counts as a use and touches `.used` at
  most once a minute.
- **`LoadedCapture`** has:
  - `id`, `path`, `meta`, `exists()`, `stripped`;
  - `index()`, `raw(name)`, `shot(root)`, `shot_roots()`, `skp(root)`,
    `skp_roots()`, `refmap()`, `raw_capture()`;
  - `derived(name)`, `put_derived(name, bytes) -> path`, `derived_path(name)`;
  - `nbytes()`, `node_count()`, `age_s()`.

  Reads after a deletion raise `capture_not_found`. A facet that was never
  captured reads as None.
  - Derived names are `derived/<f>`, `img/<f>` or `out/<f>`. A bare name means
    `derived/<f>`.
  - `index()` rebuilds an unreadable or old-schema index through
    `rebuild(raw, refmap) -> Index`, then saves it. The default rebuild is C4's
    `apply_refs(build_index(raw), refmap)`. S1 should pass one that also runs
    the analyzers.
- **Retention.** Caps count pinned captures but never evict them, and never
  evict `gc(keep=...)`. The eviction order is unlabeled first, then least
  recently used. The byte cap strips `img/`, `out/`, `derived/` and
  `raw/skp_*.bin` in that order before it deletes whole captures, and marks
  stripped captures `.stripped`.
  - `gc()` returns `{removed:[{id, why}], stripped, staging_purged,
    trash_purged, spill_purged, incomplete_purged, captures, bytes}`, where
    `why` is one of `expired`, `lineage_cap`, `count_cap` or `bytes`.
  - It returns `{"skipped": ...}` while another gc runs.
  - `gc(all=True)` returns `{all, removed, note}`.
- **Known gap.** Refs of a lineage's *latest* capture get no tombstone when that
  capture is dropped or evicted, because no later capture was matched against
  it. Refs are still never reused: the counter is global.

## Output layer (P0-1, `output.py`, `normalize.py`, `normalize_defaults.py`)

- **Call order at the boundary**: `slim(tool, result, args)` then
  `finalize(tool, brief, max_bytes=args.get("max_bytes"), spill_dir=None)`.
  - `slim` returns the input object itself for `detail="full"`, for error dicts,
    for the compact-only tools and for unknown tools. Otherwise it builds a new
    dict and never mutates its input.
  - An unknown or ambiguous `root`, or an invalid parameter value (a bad enum,
    a non-integer `max_depth`), returns `{"error", "tool", "hint"?,
    "candidates"?}` instead of raising, so callers mark it `isError` like any
    other error dict.
- **`finalize` extras**: it adds the keyword-only arguments `pretty=False` and
  `now=None`. The budget is always measured on the compact encoding, so
  `--pretty` never changes whether a result spills.
  - `max_bytes=None` means `$INSPECTOR_WIDGET_MAX_BYTES`, else 32,000. `0` means
    unlimited, and other values are clamped to 1,000..200,000
    (`resolve_max_bytes`).
  - `spill_dir=None` means `<store root>/spill`, via `model.default_store_root()`.
    Spill files are 0600 in a 0700 directory, and files older than 1 h are purged
    whenever something spills.
  - If the spill write fails, the envelope carries `spill_error` and no
    `spill_path`.
  - The envelope copies `capture` from the result when present (S3). It is at
    most `min(3000, max_bytes)` bytes.
- **Preview lines** use the form `key Type #rid|@tag "label≤30" [x,y wxh] +N`.
  - `key` is the node_key, or `view:<id>`, `compose:<id>` or
    `a11y:<host>:<virt>`, by shape.
  - `+N` counts the descendants hidden below the preview depth. It is shown on the
    last previewed level only, so root lines have no `+N`.
- **Counted omissions**: `omitted` is a dict of counters, with keys
  - `defaults`, `duplicates`, `depth`, `properties_views`, `attr_values`,
    `boilerplate_actions`, `empty_extras`, `focus_order`,
    `focus_order_non_stops`;
  - `a11y_defaults` in inspect and inspect_node, so that a11y defaults are not
    mixed with property defaults.

  dump_compose puts hoisted library composables in
  `hidden.library_composables`. A node cut by `max_depth` carries
  `hidden_descendants: N` instead of `children`. Per-view counts are in
  `omitted_defaults: {view_id: n}` (dump_tree) or `view.omitted_defaults`
  (inspect, inspect_node).
- **`max_depth` counts levels**: 1 keeps the roots only, which is how
  `dump_tree(max_depth=1)` lists windows. Values below 1 count as 1. `root` takes
  the forms below. Accepted
  prefixes are stripped, and a bare id that matches two nodes is ambiguous (for
  example `82` in inspect is both `view:82` and `compose:82`).
  - dump_tree: `82`, `view:82`, `w:82`
  - dump_compose: `325`, `compose:325`, `sem:82:325`
  - dump_accessibility: packed `id`, `host:virt`, `a11y:host:virt`
  - inspect: a node_key or the bare id after its prefix
- **Params generated from `OUTPUT_PARAMS`** (only for tools whose output is
  shaped):
  - `detail` for dump_tree, get_properties, dump_compose, compose_overlay,
    dump_accessibility, a11y_lint, inspect and inspect_node.
  - `max_bytes` for all of the above except compose_overlay and inspect_node,
    whose outputs are small and bounded.
  - `max_depth` and `root` for the four tree tools.
  - `user_code_only` (dump_compose), `focus_order` (dump_accessibility),
    `group_by` (a11y_lint) and `filter` (get_properties).

  The compact-only tools get no extra params, since `tools/list` must stay at or
  under 18,500 B. It is 18,379 B with main's TOOLS, which leaves about 120 B for
  P0-2's doc edits.
  - `max_bytes` defaults to the environment value both in the schema and on the
    CLI, so the two stay equal.
  - The CLI adds `--pretty` everywhere. `add_cli_flags` skips flags a subparser
    already has, so calling it for dump_compose and then compose_overlay on
    `compose` is safe.
  - `CLI_SUBCOMMANDS` maps each MCP tool to its subcommand, and
    `tool_args_from_cli(ns, tool)` turns parsed flags back into MCP args.
- **Brief property values** (`normalize.prop_value`):
  - COLOR becomes `#AARRGGBB`, and GRAVITY/INT_FLAG become their label. The legacy
    MCP shape has lost the label and stays `0`.
  - Resources become `@ns:type/name`, and drawable, animator and object class
    names become simple names.
  - FLOAT is rounded to 7 significant digits, which is float32-exact.
  - A value with a source or resolution stack becomes `{value, source?, stack?}`.
- **Hidden as defaults** (`nondefault_props`):
  - Static family defaults (`normalize_defaults.STATIC_VIEW_DEFAULTS`, walking
    `FAMILY_PARENTS`). Pivots count as default when they sit at the centre of
    the bounds.
  - Derived font metrics (`DERIVED_PROPS`).
  - The per-capture class majority: at least 3 views, more than 50%, and not a
    `MAJORITY_EXEMPT` property.
  - The family comes from the property set first and the class name second.
- **Brief a11y node** (`normalize.a11y_node_brief`). Beyond spec 2.3 it also
  drops:
  - None values;
  - `important_for_accessibility` when it is `YES` (counted);
  - the `is_virtual` flag, which `virtual_id` implies;
  - `-1` and `false` fields inside `collection_info`.

  The brief Compose facet likewise drops the text-substitution actions
  `SetTextSubstitution`, `ShowTextSubstitution` and `ClearTextSubstitution`
  (counted as `boilerplate_actions`).
- **dump_tree brief node**: the strings.py shape, with these changes:
  - `bounds` is `[x,y,w,h]`, plus `render` when the view is transformed.
  - `qualified_name` is dropped when it equals package.class.
  - The legacy `resource.ref` is dropped, and so is `view_id_name` when it
    repeats the resource name.
  - `layout_resource` is shown only where it differs from the parent's; children
    inherit it. `null` marks a view that was not inflated from its parent's
    layout.
  - The `id` property is dropped when it repeats the node's resource, and counted
    as `duplicates`.
- **Library code** (`normalize.is_library_source`): a source file listed in
  `normalize_defaults.LIBRARY_FILES`, regenerated with
  `host/tests/gen_library_files.py`. A missing source counts as library. The
  origin is `app` when the file is not a library file and the composable name
  starts upper-case. dump_compose always keeps the window root and semantics nodes.
- **Measured brief sizes on the checked-in real outputs**, in compact bytes (spec
  2.6 target in parentheses):
  - launcher `dump_compose`: 17,399 (24,000); with `include_slot_table=false`,
    4,470 (6,000)
  - launcher `inspect`: 9,896 (13,000)
  - launcher `dump_accessibility`: 9,751 (12,500)
  - launcher `a11y_lint`: 1,055 (1,200)
  - launcher `dump_tree(include_properties)`: 3,481 (8,000)
  - launcher `get_properties`: 2,154 (3,500)
  - View screen `inspect`: 14,477 (18,000)
  - View screen `dump_tree(include_properties)`: 16,584 (20,000)

## Offline scenes (F1, `tests/fakescenes.py`)

- **No harness dependency.** improve/offline-e2e-harness (`tests/fakeagent.py`)
  is not on `main` yet, so the scenes do not import it. A `SceneData` answers
  `pb.Request` -> `pb.Response`, and exposes it three ways:
  - `scene.behaviour` is the harness hook `behaviour(req) -> (0.0, response)`.
    Use `FakeAgent(behaviour=...)` or set `FakeDevice.behaviour`; the skipped
    test `test_wide_scene_over_the_harness_fake_adb` runs that path once the
    harness is present.
  - `scene.session()` returns a `SceneSession`, an in-process `Session` stand-in
    with the section 10 public surface:
    - `dump_tree`, `get_windows`, `screenshot`, `dump_compose`, `dump_a11y`,
      `get_properties`, `hello` and `capture_skp`
    - `pid`, `serial`, `package`, `api_level`, `abi`, `agent_version` and
      `build_id`
    - `close()`

    Requests and responses are round-tripped through protobuf serialization,
    and an ERROR raises `SceneError`. C3 (fetch) and C4 (index) can test against
    real data this way, with no sockets.
  - `fake_attach(scene)` replaces `inspector_widget.attach` for MCP and CLI
    tests. Also swap in a fresh `mcp_server.SESSIONS`.
- **Scenes.**
  - `wide_scene(fan=6)` is the E6 fixture: 259 Views with 60 properties each, a
    mirrored a11y tree, one "Root" semantics node and an 8x8 screenshot. It
    reproduces the E6 MCP sizes within 0.3% (149 KB / 1.74 MB / 488 KB /
    547 KB / 3.85 MB).
  - `replay_scene("launcher" | "viewscreen", slots_populated=True)` rebuilds
    responses from `tests/fixtures/live`. The data is **pre-ID1**: every Compose
    a11y node is `1:11`.
  - The slot table is served when requested and inspection is on (as recorded,
    or after `enable_inspection`). Semantics ids are not re-minted.
  - `include_semantics=False` strips the semantics subtree from the window root.
    Properties are served without `source`/`resolution_stack` unless those were
    requested. An unknown view id returns the agent's
    `No view found with id N`.
- **Converters.** `views_to_pb`, `compose_to_pb` and `a11y_to_pb` accept the
  strings.py and a11y.py shapes and the legacy MCP dump_tree shape.
  - Legacy E3 values are kept as recorded: GRAVITY/INT_FLAG labels are lost, and
    `#AARRGGBB` colours are re-encoded to int32.
  - `png_to_screenshot` produces ABGR_8888 (in-memory R,G,B,A) with a 9-byte LE
    header, deflated.
  - The strings.py tree shape round-trips exactly. The a11y windows round-trip
    exactly; `focus_order` is recomputed by a11y.py, so compare windows only.
