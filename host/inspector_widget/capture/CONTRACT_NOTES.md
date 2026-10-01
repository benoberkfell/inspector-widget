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
  - Out-of-order publishes (A fetched first, B published first): when the
    lineage's latest has a newer `created_at`, the latest stays and the capture
    goes into `history` by `created_at`; its `prev` (when None) is the next older
    capture, and its tombstones are dropped (they name refs the newer latest
    still holds).
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
- **Lineage files** are `lineages/<serial>__<package>-<hash8>.json`: the
  sanitized names plus 8 hex chars of a hash of the exact serial and package, so
  `192.168.1.7:5555` and `192.168.1.7_5555`, or `com.Slack` and `com.slack` on a
  case-insensitive disk, never share a file. They also carry `serial` and
  `package`, and `lineage_state()` ignores a file whose stored pair differs.
  `lineage_state()` remembers its lineage, so
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
  - `gc(all=True)` returns `{all, removed, note}`. It renames every capture
    into `.trash` before deleting, as evictions do.
  - A complete capture whose `meta.json` is unreadable is still collected: it is
    dated by its `.complete` marker, counts towards the caps in a lineage of its
    own and is evicted like any other (it cannot be loaded or listed).
- **Known gap.** Refs of a lineage's *latest* capture get no tombstone when that
  capture is dropped or evicted, because no later capture was matched against
  it. Refs are still never reused: the counter is global.

## Fetch (C3, `capture/fetch.py`)

- **`fetch(session, opts=None, *, compose_generation=0, clock=time.monotonic,
  sleep, wall_clock=time.time, device=None, retries=2, retry_delay_s=0.15,
  skp_max_version=109) -> RawCapture`.** `meta.id` is `""` until the store
  publishes the capture.
  - `session` only needs the `CaptureSession` protocol, which is the public
    Session method surface. `pid`, `api_level`, `abi`, `agent_version`,
    `build_id` and `capture_skp` are read with getattr, so main's Session and
    session-lifecycle's Session both work.
  - fetch never calls adb. S1 passes `device={dpi, font_scale}`; fetch derives
    `screen` and `orientation` from the window roots when they are missing.
  - `compose_generation` is the lineage's current generation (0 for a new
    pid). The meta records it plus one when `enable_inspection` was sent. It
    also bumps when the enable request itself errors, since the hot reload may
    have happened anyway.
- **`meta.facets` keys**: `windows`, `views`, `props`, `shots`, `compose`,
  `slots`, `a11y`, `skp`, `fingerprint`.
  - `props` rides on DumpTree.
  - `fingerprint` times the re-check.
  - Facets that were not requested are `off` with a reason (`screenshot=false`,
    `slots=off`, `skp=false`, `props=false`).
- **Registry.** `FACETS` maps each name to a `Facet(name, request, policy,
  stored, run, position(opts), required, off_reason)`, and `plan(opts)` gives
  the request order. `windows` and `views` are the required facets.
- **Slots.** `enable` sends `DumpCompose(slots only, enable_inspection)` first
  and stores that reply. Retries read without enabling. `if_available` never
  enables.
  - Unpopulated slots: `unavailable` with `SLOTS_NOT_POPULATED`.
  - No ComposeView: `NO_COMPOSE`.
  - `SLOTS_ENABLE_WARNING` is for the capture response (S1). fetch does not add
    it to diagnostics.
- **Screenshots.** The first root is `DumpTree.roots[0]`, whose screenshot the
  agent embeds. It is split into `shots[firstRoot]`, and `views.pb` is stored
  without it. Every other root gets `Screenshot(root_id)`, and the first root
  does too if the embedded shot is missing. Each `shots[root]` is a serialized
  `Screenshot` message, still deflated.
- **a11y.** `a11y_rendering=True` sets `include_rendering_info` on the single
  DumpA11y. Its reply goes to `raw/a11y.pb`, and `RawCapture.a11y_render` stays
  None (reserved).
- **SKP.** An SKP is not stored when `supported=false` or when its version is
  above `SKP_MAX_VERSION` (109). Either case is `unsupported` with a reason.
- **Errors.**
  - `OSError` (transport, deadline) always propagates.
  - A failing required facet raises `OpError("agent_error")`.
  - An optional facet records `error` and the capture goes on.
  - A DumpTree that fails with properties is retried without them, giving
    `props: error`.
- **Consistency.**
  - `fingerprint_of(views, compose)` is a blake2b-128 hex over the tuples of
    spec 3.2. Strings are resolved per message, so a dump with properties and
    one without give the same value.
  - `fingerprint_raw(raw)` computes it for a stored capture, and
    `fingerprint_now(session)` for the live UI.
  - `meta.fingerprint` is the fingerprint of the stored data, even when the
    capture is unsettled.
  - A retry re-fetches every facet. After 3 attempts the capture is
    `unsettled`, with a diagnostic.
- **`settle(session, settle_ms) -> bool`** (section 10 said `None`) polls every
  100 ms and stops at two equal fingerprints. Its budget is `settle_ms`, capped
  at 3,000 ms. fetch runs it first and adds a diagnostic when the UI never
  settled.
- **`unchanged_since(session, meta)`** implements `if_changed_since`. It returns
  `{capture, unchanged: true, age_s}` when the lineage, the pid and the
  fingerprint all match, else None. It writes nothing.

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
  - `spill_dir=None` means `<store root>/spill`, via `model.default_store_root()`,
    unless `INSPECTOR_WIDGET_CAPTURE_PERSIST=0`: then a private per-user directory
    under the system temp dir (`<tmp>/inspector-widget-<uid>/spill`, 0700), so
    memory-only mode never writes screen text into the persistent cache. Callers
    that hold a `CaptureStore` can pass `store.spill_dir()`. Every directory a
    spill creates (the store root included) is 0700, and `CaptureStore` tightens
    an existing root that holds only its own layout.
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
  - A single-child chain is one line (`a > b > c`, at most 8 members) and costs
    one level, and zero-size leaves (ViewStubs) are left out, so the two levels
    shown are ones that branch (L1: live on S1 the preview stopped at
    DecorView/LinearLayout). A brief inspect node's `@pkg:id/name` is `#name`.
  - The hint suggests `detail="brief"` only when the call asked for full
    (`finalize(..., detail=)`).
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
  under 18,500 B. With the three TalkBack tools that took trimming (P0-2): shorter
  tool and parameter descriptions, `package` without one, a shared `scale`, and
  `a11y_lint.rules` without its 47-id enum (the handler checks the ids first and
  names the valid ones). It is 18,337 B with all 18 tools.
  - `max_bytes` defaults to the environment value both in the schema and on the
    CLI, so the two stay equal.
  - The CLI adds `--pretty` everywhere. `add_cli_flags` skips flags a subparser
    already has, so calling it for dump_compose and then compose_overlay on
    `compose` is safe.
  - `CLI_SUBCOMMANDS` maps each MCP tool to its subcommand, and
    `tool_args_from_cli(ns, tool)` turns parsed flags back into MCP args.
- **Brief property values** (`normalize.prop_value`):
  - COLOR becomes `#AARRGGBB`. GRAVITY/INT_FLAG are the agent's flag string: the
    value since E3 (`strings.property_to_dict`), the `label` beside a `0` in older
    recordings. The legacy MCP recordings lost it and stay `0`.
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
- **P0-2 changes to the brief rules**, found wiring them to the live shapes:
  - a11y_lint lists a finding's `node_key` (what inspect_node takes; the packed
    id only when there is none) and leaves out `stats` and the info-level
    diagnostics, counted as `omitted.stats` (fields) and `omitted.info_diagnostics`.
  - focus_order: a11y.py lists stops only (`is_focus_stop` appears only with
    structural entries), as `{order, key, id, speak, unlabeled?, window?}`; a
    brief stop is `{order, key, speak}` plus `unlabeled`/`window`/`covered_by`,
    and `id` only when there is no `key` (the recordings).
  - dump_accessibility `root` also takes a node's `node_key`.
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

## Phase-0 wiring (P0-2, `mcp_server.py`, `cli.py`, `results.py`)

- **MCP.** `_call_tool_text` -> `_render_result(name, result, args)`: an error
  result goes out as it is (compact, `isError`); anything else is
  `slim(name, result, args)` with the arguments normalized as the tool saw them
  (a `null` optional dropped, `12.0` an int), then `finalize(name, brief,
  max_bytes=args.get("max_bytes"))`. A `slim` error (an unknown `root`) is
  `isError` too. `output.augment_schemas(TOOLS)` runs once, after the TalkBack
  tools are in. If the host package cannot import, the server still starts and
  answers compact JSON.
- **CLI.** `_emit_result(args, tool, legacy, result)`: `--detail full` prints
  `legacy`, the subcommand's own pre-Phase-0 document; otherwise `slim(tool,
  result)`, where `result` is the MCP tool's document built by the same
  `results.*` function. So `--json -` is byte-equal to the MCP text for the same
  arguments, except the flag a dump_compose note names (`--enable-inspection` on
  the CLI). `--json -` is budgeted like the MCP (an envelope and a spill file);
  `--json FILE` gets the whole document and never spills; exit code 1 when `slim`
  rejects an argument. The subcommands that always print JSON (inspect-node,
  get-properties and component-image without `--json`, talkback) go through it
  too. `add_cli_flags` runs for every subcommand that prints JSON, so `--pretty`
  is on all of them. A brief `a11y --lint` groups its embedded lint by rule.
- **`results.py`** (new): the documents both surfaces build, with the target
  (`serial`, `package`) and the fields each tool always reports
  (`root_count`, `contrast_sampled`, the dump_compose note).
- **E3.** mcp_server's own proto decoder is gone: `dump_tree` and
  `get_properties` use `strings.dump_tree_to_dict` / `get_properties_to_dict`,
  so the full shapes are the CLI's (bounds `{layout, render?}`, resources
  `{namespace, type, name}`, properties keyed by view id, COLOR as its int).
  `strings.property_to_dict` now gives GRAVITY/INT_FLAG the agent's flag string as
  the value (`""` for an empty set), not `0` beside a `label`. Also deleted: the
  screenshot helpers and the second PNG encoder (`png.write_png` for both
  surfaces), `_first_attr` and the `_import_proto` fallback names, the dict branch
  of `_session_get_windows`, the object branches of `_device_to_json` /
  `_process_to_json`, `_device_density` and `_lint_fn`.
- **Goldens** (`tests/golden/legacy/`, `test_legacy_golden.py`): the rollback
  (`detail="full"`, `max_bytes=0`) reproduces the pre-Phase-0 outputs recorded at
  G1 except the documented deltas (`record_goldens.LEGACY_DELTAS`): the E3 shapes
  (MCP `dump_tree`, `get_properties`; GRAVITY/INT_FLAG values in both surfaces'
  property lists) and the MCP screenshot's file size (another PNG encoder, the
  same pixels).
- **Measured** through `_call_tool_text` and the CLI's `--json -` over the harness
  fake adb and agent (compact bytes; "legacy" is the full result with indent=2,
  as the MCP sent it before Phase 0):

  | Scene | Call | Legacy | MCP = CLI | Target |
  |---|---|---|---|---|
  | launcher | `dump_compose()` | 567,142 | 17,399 | 24,000 |
  | launcher | `dump_compose(include_slot_table=false)` | 23,941 | 4,470 | 6,000 |
  | launcher | `inspect()` | 115,286 | 12,781 | 13,000 |
  | launcher | `dump_accessibility()` | 85,974 | 10,250 | 12,500 |
  | launcher | `a11y_lint()` | 3,624 | 1,053 | 1,200 |
  | launcher | `dump_tree(include_properties)` | 106,813 | 3,691 | 8,000 |
  | launcher | `get_properties(82)` | 11,178 | 2,157 | 3,500 |
  | View screen | `inspect()` | 117,913 | 14,081 | 18,000 |
  | View screen | `dump_tree(include_properties)` | 644,021 | 16,767 | 20,000 |
  | 259-view | `dump_tree(max_depth=1)` | 169,658 | 297 | 2,000 |
  | 259-view | `dump_tree` / `dump_accessibility` / `inspect` / `+props` | 170 KB-3.85 MB | envelopes of 766-866 | 3,000 |

  `tools/list` is 18,337 B compact (18 tools).

## Query engine and line grammar (C6, `capture/query.py`, `capture/lines.py`)

- **Signatures.** Section 10 exactly, plus keyword-only extras:
  - `resolve_selector(ix, sel, *, tomb=None)`: `tomb` is the lineage's tombstone
    map (`LineageState.tomb`), used for `ref_not_in_capture` last-seen info.
  - `outline(ix, **p)`, `find(ix, **p)` and `node(ix, loaded, refs, **p)` also
    accept `capture`, `serial`, `package`, `loaded`, `props_fn` and `tomb`. Any
    other unknown argument is `bad_args`. `find` takes `in` (or `in_` from
    Python). `node` also takes `image_fn(node)` and `issue_fmt(issue)` hooks.
  - `render_line(ix, n, fields=None, depth=0, hidden=0, tail="", **kw)`:
    `fields` is a `lines.Fields` or a `fields` argument. `tail` is raw text,
    appended as is.
  - `select(ix, sel) -> list[UNode]` returns every match without raising. C4
    uses it to check that a generated `sel` is unique and parses.
- **Props accessor.** `node` (and `+props:` projections) read property values
  through `props_fn(view_udid)` if given, else `loaded.props(view_udid)`. The
  result is `{name: value}` (normalized), a list of strings.py/legacy property
  dicts (normalized with `normalize.props_to_map`), or None. **C2/C4: expose
  `LoadedCapture.props(udid)`.** Results are cached per call. `nondefault` also
  reads the same-class views, because `normalize.nondefault_props` needs the
  class majority.
- **Facet statuses read.** `meta.facet_status("slots")` and
  `meta.facet_status("a11y")`. `outline(view="slots")` on a capture that has
  Compose nodes but no slot groups is `facet_unavailable` (hint:
  `capture(slots="enable")`). With no Compose at all it is an empty outline.
- **Line grammar v1.** `lines.LINE_RE` is the regex, `format_line(row)` renders a
  row and `parse_line(line)` reads it back. `row_matches_line(row, line)`
  checks that a json row and its line carry the same fields.
  - A rid or tag that is not a plain `[A-Za-z0-9_.:/$-]+` token is JSON-quoted
    (`@"my tag"`). The selector grammar accepts the same form.
  - `Type` is `UNode.type` with anything outside `[A-Za-z0-9_$]` removed and
    the first letter upper-cased. Flags print in `model.FLAGS` order.
  - A line's label is `UNode.label`. For slot nodes it falls back to `text` (or
    the `text` param). It is cut at 48 characters with `…`.
  - `!code` is `short_code(rule)`: the catalog's code (`rules.short`: the group,
    or `group_x` when the group has several rules), `render.<x>` gives `x`, and
    anything else has dots turned into `_`. Codes are distinct and sorted.
  - A `+props:`/`+params:` name that is also a field or row key (`text`, `hint`,
    `state`, `src` ...) renders namespaced (`params.text=`, `props.hint=`;
    `lines.proj_key`), so no line carries a key twice. Other names stay bare
    (`textSize=14sp`). Json rows keep `props`/`params` as nested objects.
  - Tail values are one token: numbers compact, lists comma-joined, dicts as
    `k:v,…`. Anything with spaces or quotes is JSON-quoted. Values are capped at
    120 characters.
  - The row keys `mark` (a diff prefix `~ + - >`), `order` (the reading prefix
    `N. `) and `depth` (the indent) are rendered by `format_line`. C8 should
    build diff lines with `node_row(..., mark="~")`.
- **Chains.** A chain follows single visible children that have identical
  bounds and the same kind. It never enters a window root, holds at most
  `CHAIN_MAX` (8) members, and ends at a member with issues. If no member is shown, the chain collapses and its
  children are hoisted. Otherwise it is one line whose bounds, issues, `+N`
  and tail belong to the **last** member (the row's top-level `ref`), with
  `row["chain"]` listing every segment.
- **Outline disclosure.**
  - `depth` counts display levels (0 = the roots only).
  - In semantic detail a node is shown when it is a window, has a
    label/rid/tag, is actionable (click, longclick, edit, checkable, scroll),
    is a stop, has issues, is a leaf, or has 2 or more shown direct children.
  - Zero-size nodes and ViewStubs are hidden with their subtree. An empty
    `AndroidViewsHandler` (Compose interop plumbing) is not a content leaf: it
    collapses into its parent's `+N` (L1: one line per Thunderbird ComposeView row).
  - Each tree node is exactly one of: on a line, collapsed (hoisted through),
    hidden, or counted in one line's `+N`. The response's
    `hidden:{zero_size, collapsed, library}` counts the middle two, and a
    test checks this partition.
  - `max_children` caps only collections: nodes that scroll, have an a11y
    `collection`, or are a RecyclerView/ListView/GridView/pager/`Lazy*`. Other
    parents list every child and rely on paging. (With a global cap, the View
    screen's 22-child form would be cut.)
  - A slot `root` without an explicit `view` selects `view="slots"`.
  - `view="compose"` always prints each ComposeView's semantics root.
    `view="slots"` hoists library groups (`hidden.library`) and has no chains.
    `view="views"` implies `detail="all"`.
- **find.**
  - `in="slots"` means slot nodes only, and `in="all"` means everything. `src`
    implies `all` unless `in` is given.
  - `src` without a `:` globs the file name; with a `:` it globs `File.kt:line`.
  - `type`, `role` and `text` are case-insensitive. `rid`, `tag` and `src` are
    case-sensitive globs.
  - `within` is the selected subtree including the node itself.
  - `window` takes a window selector or a z index.
  - `sort="area"` puts the smallest nodes first. `sort="reading"` puts stops
    first, in order.
  - A single hit gets `path`: the landmark ancestors from the root, joined by
    ` / ` (it skips levels, so it is not a selector; ` > ` means a direct child).
  - Breadcrumbs are the 2 nearest ancestors with a rid, tag, label or a11y
    collection. A crumb whose tag or rid other nodes share (list cells) adds the
    label: `in n749 Card @card "Item 3" < n733 RecyclerView #feed`.
  - A filter that can only match slot data (`src`, `in="slots"`, `has=slots`) on
    a Compose capture without the slot table is `facet_unavailable` with the
    recapture hint; `in="all"` and `+params:` there add a `notes` entry.
  - `find(issue=...)` takes a rule id or prefix, a short code, a group
    (`label` = both label rules) or a severity.
- **node.**
  - Facets: `core issues a11y layout compose text props children ancestors`,
    or `all`. `core` is always included. For slot nodes, `compose` renders
    the `slot` facet.
  - `props` defaults to `none`, and `facets` containing `props` implies
    `nondefault`. `key` and `nondefault` drop the `id` property when it repeats
    the rid.
  - Parts are packed in priority order across the batch, with room reserved
    for short `omitted` entries. The long form
    `"<facet>(detail): node(...)"` is used when it fits.
  - Batch errors are reported per item as `{sel, error}`. A single-node error
    raises.
  - A props facet cut to fit says so: `{mode, n (values shown), of, more,
    values}` and the omitted entry `props(all,+52 more): node(...)`. When even
    the short omitted entries do not fit, each node keeps a count marker
    (`"3 facets: raise max_bytes"`); when the nodes' core fields alone exceed
    `max_bytes` it is `bad_args` naming the size that fits. `omitted` is never
    silently emptied.
  - `tap_xy` is the centre of the visible part: `b` clipped to the node's window
    and the screen. A hidden, offscreen, zero-size or out-of-window node has no
    `tap_xy`; `tap` says why (`not tappable: offscreen (scroll it into view, then
    capture again)`). `query.visible_rect(ix, n)` is the shared helper.
- **Selectors (extensions to 6.1).**
  - Atoms can also be refs or keys, so `n10 > @x` works and a generated
    `<parent sel> > Type"label"` always parses.
  - `#"…"` and `@"…"` quote unusual ids.
  - A label ending in `…` matches as a prefix, so labels copied from a cut
    line still resolve.
  - Atoms match ui nodes first and fall back to slot nodes when no ui node
    matches.
  - `bad_selector` errors start with `column N:` (1-based) and set
    `OpError.column`. The column is the first character that breaks the
    grammar: for a bad separator, the character right after the atom.
  - `not_found` on a path whose last atom matches deeper (not as a direct child)
    says ` > ` means a direct child, suggests `find(within=..., ...)` and lists
    the deeper matches. A sel pasted with its outer JSON quotes (one label atom
    whose text is a matching selector) gets that sel as hint and candidate.
  - `ref_not_in_capture` says why: last seen (lineage tombstone), newer than
    every ref of this capture (`capture="latest"`), or no record in the lineage
    (another app's ref, or a typo; only claimed when `tomb` is passed).
  - Type atoms and the `type` glob also match the display type lines show
    (`RowMeasurePolicy` for the composable `rowMeasurePolicy`).
- **Cursors.**
  - Letters: `o` outline, `f` find, plus `l` lint and `d` diff for C7/C8 (see
    `TOOL_LETTERS`, `make_cursor`, `parse_cursor`, `args_hash`).
  - The hash covers the normalized arguments, with root/within/window
    resolved to ids. It excludes `cursor`, `max_lines`/`limit`, `max_bytes`
    and `format`, so page size and format can change between pages.
  - `cursor_capture(cursor)` lets the ops layer resolve the capture a cursor
    belongs to.
- **Budgets.**
  - Defaults: outline 6,000, find 3,000, node 3,000 (6,000 for a batch), lint
    4,000, diff 4,000. Every tool (C6, C7 and C8) goes through
    `query.resolve_max_bytes`: `0` means the 32,000 ceiling, and other values are
    clamped to 500..32,000.
  - `query.pack()` uses `output.Budget` and reserves the page footer's exact
    cost before each entry. When nothing fits it emits a minimal `ref` line,
    so every page advances. C7 and C8 can reuse `pack` and `assemble`.
- **`next`** (`call()`, `next_hints()`): at most 3 hints and at most 200 B. A
  cursor hint (`cursor_call(tool, args, page, cursor)`, used by outline, find,
  lint and diff) repeats every non-default argument the cursor hash covers plus
  the page shape (`max_lines`/`limit`, `max_bytes`, `format`); the page-shape
  arguments are dropped only when the hint would not fit 200 B. `image(ref)` is
  suggested only for a node with pixels on screen. The destructive
  `capture(slots="enable")` is never a hint: the compose facet's `slots` string
  carries the warning. An outline that hid collapsed or zero-size nodes offers
  `outline(detail="all", ...)`. Every hint runs verbatim
  (`tests/capture_hints.py`, `test_every_next_hint_runs_verbatim`).

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

## Index (C4, `capture/index.py`, `capture/anchors.py`)

- **API.** `build_index(raw) -> Index` (key space, every `ref` None) and
  `apply_refs(ix, refmap) -> Index` (`remap_ids`, plus every fallback `sel` equal to
  the node's key becomes its ref). Extras:
  - `resolve_key(ix, key) -> id` also takes the agent contract spellings
    `compose:<acv>:<id>` and `composeview:<acv>`, and legacy `compose:<id>`. It raises
    `OpError("ambiguous", candidates=[ids])` or `OpError("not_found")`.
    `legacy_candidates(ix, "compose:<id>")` lists what a legacy key could mean.
  - `FacetReader(raw)` decodes lazily: `props(udid)` (normalize.props_to_map),
    `prop_list(udid)` (strings.property_to_dict), `slot_params(ids["slot_path"])`
    (raw strings, for `params="raw"`), `sem_attrs(acv, id)`. `decoded` counts the
    property groups decoded. `build_index` never decodes properties; it only peeks
    `visibility`, `enabled`, `clickable` and `longClickable` by string id.
  - `anchors.py` (pure): `template()`, `without_ordinals()`, `collection_index()`,
    `is_collection()`, `assign_ui_anchors()`, `assign_sels()`, and `match_sel(ix, sel)`,
    a reference evaluator of the spec 6.1 path grammar. C6's resolver must agree
    with it.
- **Node ids and placement.**
  - Window roots come from GetWindows order (`z`); DumpTree roots missing from it
    follow in DumpTree order.
  - The synthetic Compose root that reuses the ACV id is folded into the ACV View
    node. Semantics roots come first among the ACV's ui children, then its View
    children (e.g. `AndroidViewsHandler`).
  - The children of `AndroidViewsHandler` (AndroidView holders) move in the ui tree
    under the smallest semantics node of the same ACV that contains them, with
    flag `interop` and `conf["ui"] = "inferred"`. The handler stays, emptied. The
    `views` tree keeps the raw View parents.
  - a11y-only nodes (`kind=a11y`) hang under the node of their a11y parent (else
    their host View, else the window root), after its other children.
  - `Index.nodes` order: ui pre-order (windows by z), then slots pre-order.
    `Index.reading` stays empty and `stop` None; the analyzers (C7) fill them.
- **Bounds.** Views: `b` is the exact-joined a11y rect when it lies inside the
  layout rect (the framework's own clipped rect), else the layout rect clipped by
  its ancestors. `declared_b` is the layout rect when it differs. Semantics: `b` is
  the agent's rect (already clipped); `declared_b` is the emitter slot's box when it
  differs and contains `b`. `visible` = area(b) / area(declared_b), 3 decimals.
  Slots: `b` is the raw box. Compose bounds that are window-relative (pre-CO4, no
  `bounds=screen` in the DumpCompose diagnostics) are shifted by the window root's
  origin for that ACV (semantics and slots). This happens when its first sized root
  lies outside the ACV and fits once shifted, and it adds a diagnostic.
- **Accessibility join and the ID1 detector.**
  - Exact: `(udid, -1)` -> `view:<udid>`, `(acv, id)` -> `sem:<acv>:<id>`. With the
    ID1 fix the ACV's own node `(acv, -1)` stands for its root semantics node; it
    joins the ACV View.
  - Host 0 (the agent could not resolve the View): IoU >= 0.6 within the window,
    `inferred`.
  - The detector marks a window when it holds a duplicate `(host, virtual)` pair
    (host != 0), or a virtual node hosted by a View that has View children and is
    not an ACV, a WebView or a `provider_class` host (the View-screen shape of ID1).
    Diagnostics: `index.ID1_DUPLICATES` (the spec's text) or `index.ID1_IMPLAUSIBLE`.
  - In a marked window every a11y facet, including those of a11y-only nodes, is
    `inferred`. Matching goes top-down over the a11y tree, one-to-one and greedy
    per sibling group. Candidates are view and compose nodes in the subtree of the
    parent's match, with IoU >= 0.6. They are ranked by IoU (0.05 steps), then a
    compatibility score (host id hint, class vs View class or Role, text, clickable,
    virtual-vs-View when hosts are trustworthy), then the shallowest. A node left
    over may take a free node in that subtree whose rect overlaps it by >= 90% of
    the smaller one with compatibility >= 1 (a row clipped by the list viewport).
    Pre-ID1 a ComposeView's own a11y node may land on the root semantics node
    instead of the ACV View.
  - Keys of a11y-only nodes: `a11y:<host>:<virt>` when that pair is unique and the
    window is not marked, else `a11y:path:<rootUdid>:<0.i.j>`.
  - Aliases in `by_key`: `a11y:<host>:<virt>` for exact joins with unique pairs,
    `a11y:path:…` for every matched node in a marked window, `w:<root>`, and
    `compose:<id>` only when it names exactly one node (a semantics id in one ACV,
    or an ACV's udid, which was the old synthetic root key).
  - Links (`labeled_by`, `label_for`, `traversal_before`, `traversal_after`; the
    first resolvable `labeled_by_list` entry when `labeled_by` is unset) are mapped
    from the agent's host-key space `(host << 32) ^ (virt & 0xFFFFFFFF)`, and only
    in unmarked windows.
- **The a11y facet** holds `class`, `speakable`, `role` (role_description), `flags` (the
  raw a11y bool names, minus `enabled`/`visible_to_user`/`is_virtual`, plus
  `disabled`/`hidden`), `actions` (names without the focus/selection/granularity
  boilerplate), and when present `state`, `hint`, `error`, `tooltip`, `pane`,
  `container`, `supplemental`. It may also hold `collection {rows, cols, items?,
  hierarchical?, selection?}`, `item {row, col, row_span?, col_span?, heading?,
  selected?}`, `range {type, min, max, cur}`, `live`, `checked: "partial"`,
  `input_type`, `max_text_length`, `text_size_px` and `res` (the full
  viewIdResourceName), plus `package` (only when it is not the app's), `provider`,
  `b` (when it differs from the node's `b`), `extras` (empty SPANS dropped) and the
  links above.
- **Slots.**
  - Slot groups are the COMPOSABLE children of each window's synthetic root.
    The agent sends one root per composition in hash order; `graft_slot_roots`
    makes one tree of them: the main composition (`ProvideAndroidCompositionLocals`,
    else the largest root) first, then every other sized root grafted under the
    smallest already-placed group whose box holds its top-left or bottom-right
    corner (Lazy items under their list even when clipped, TopAppBar and the
    content under Scaffold), after that group's own children, top then left. The
    graft is `conf.slots = "inferred"`. Roots nothing holds follow the main one;
    zero-size roots (effects) come last, by name. Slot nodes are stored in this
    tree's pre-order, and their anchors and keys follow it. On the launcher,
    `outline(view="slots", depth=99, max_children=1000)` is the spec's 56 app
    lines (4.7 KB).
  - Facet: `slot {name, params (normalize.compose_value, modifiers removed), mods
    (the brief modifier chain), sem: [ids]}`.
  - `ids`: `slot_path` (`"<acv>/<i.j.k>"` into the synthetic root's children),
    `slot` (`File.kt:line#k`, k counting groups with that source in the ACV), and
    `layer` (render_node_id, when set).
  - `text` is the `text` param. `conf["slot"]` is exact, and `conf["sem"]` is
    inferred when the slot is linked.
  - **Window**: the linked semantics node's window, else the ACV's window. This
    refines the model note, where an unlinked slot had `window` None.
  - A lazy item's `key` param appears in the anchor (`[heading]`).
- **Slot links (inferred).**
  - The *emitter* of a semantics node is the deepest slot whose box contains its
    `b` and agrees with it once clipped by the parent semantics node (IoU >= 0.9).
    A slot whose `testTag(tag=…)` modifier names the node's tag is preferred.
    Nodes are processed deepest first, one-to-one.
  - Its nearest app-origin ancestor-or-self that also fits is the primary link. It
    gives the node `src`, `declared_b` and (after any role) its `type`.
  - App-origin slots nested under a primary slot (the nearest one wins) are linked
    to the same node.
  - `compose.slots` = [primary, nested… in slot order]; `slot.sem` = [node].
    `conf["slot"] = "inferred"` on the semantics node.
- **Derived fields.**
  - `type`: Compose `Role` attr / a11y `role_description` (one capitalized word) /
    `ROLE_BY_A11Y_CLASS[a11y class]`; then the primary app slot name; then the View
    class; then the a11y class simple name unless it is `View`. `role` holds the
    first step's value.
  - `label`: a11y speakable (contentDescription > text > stateDescription; a node
    that is screen-reader-focusable, clickable or long-clickable and has none
    speaks its non-focusable descendants, joined by ", "); then Compose
    ContentDescription > Text > EditableText > StateDescription; then View text.
    `label`, `text`, `desc`, `state` and `hint` are capped at 1,000 chars.
  - `flags`: the union of a11y, Compose attr and View property flags, in `FLAGS`
    order. `focus` is dropped when `click` or `longclick` is set (clickable implies
    focusable), matching the spec's outline examples. Two flags say what the
    agent could not send: `truncated` (ViewNode `CHILDREN_TRUNCATED`, A11yNode
    `children_truncated`: children cut at the wire depth cap; the view facet also
    has `children_truncated: true`) and `redacted` (`TEXT_REDACTED`: a password
    field's text is masked; view facet `text_redacted: true`). Neither is a
    behaviour change in `diff`.
  - `diagnostics` starts with the agent's own incomplete-data tokens, one
    `facet: token` line each (`views: depth-truncated=3 (...)`, `compose:
    semantics_failed: view#9 ...`, `a11y: node-cap=...`; the prefixes are
    `index.INCOMPLETE_TOKENS`, the list correlate's `summary["incomplete"]` uses),
    then a count of the `truncated` Views with the `find` call that lists them.
    `fetch` marks the compose facet `unavailable` ("obfuscated: ...") when the
    agent reports `compose_obfuscated`, or the first `semantics_failed` token
    when no ComposeView produced semantics, and the slots facet the same way
    (never the destructive "not populated" hint there).
- **Anchors** (`anchors.py`).
  - Views: `Class#rid`, or `Class:k`. Semantics and a11y-only nodes: `@tag`, else
    `Type"label≤24"`, else `Type:k` / `:k`. A duplicate among siblings gets `:k`.
  - Slots: `Name@src[key]:k` (always with `k`), continuing from the ACV's anchor.
  - Children of a collection get `[i]`. A collection is an a11y
    `collection`, a RecyclerView/ListView/GridView-like class, or a Compose node with
    `CollectionInfo`/`IndexForKey`/`ScrollToIndex`. `i` is the CollectionItemInfo
    row (or column) when siblings' values are distinct, else the position.
  - Labels, tags, rids and classes escape `\ / " [ ] :`; sources escape all of those
    except `:`.
- **sel**:
  - Tried in order: `#rid`, `@tag`, `Type"label"`, `"label"`, then `<parent sel> >
    Type"label"` and `<parent sel> > Type` (unique among the siblings, parent sel
    not a fallback, at most 3 atoms and 120 chars), else the id.
  - Labels are used only when ≤ 40 chars on one line, rids only when they match
    `[A-Za-z_][A-Za-z0-9_.]*`, tags only when they match `[A-Za-z0-9_.:-]+`, and
    types only when they match `[A-Z][A-Za-z0-9_]*`.
  - Uniqueness is over ui nodes (view, compose, a11y). Slot nodes always use their
    id.
- **Test scenes** (`tests/capture_scenes.py`):
  - `raw_from_scene(scene)` fetches a RawCapture from an F1 scene.
  - `V`/`C`/`S` + `encode()` hand-build protobuf screens in post-ID1 form, or
    pre-ID1 (`pre_id1=True`) and window-relative (`window_relative=True`).
  - `mixed_scene()`: three ComposeView cells plus a View cell in a RecyclerView;
    an AndroidView holding a TextView and a nested ComposeView; and a dialog.
  - `default_like_scene()`: a port of the harness `default_scene`.
  - `big_scene(n)`: 13 index nodes per cell.

## Refs and carry-over (C5, `capture/refs.py`)

- **`assign(new, prev, *, same_pid, same_generation, alloc) -> (refmap, tomb_updates)`**
  - `new` is a key-space index (the `build_index` output). `prev` is the lineage's
    latest published index (ref space) or None. A `prev` whose node ids are not
    refs raises `ValueError`; two different lineages raise `OpError("bad_args")`.
  - `alloc(n)` reserves `n` consecutive fresh ref numbers and returns the first
    one. This is `CaptureStore.next_refs` under `refs_lock`. It is called at most
    once per capture, and not at all when every node carries over. Fresh refs go
    out in pre-order (ui tree with windows in z order, then the slot tree), so
    allocation is deterministic for identical inputs.
  - `refmap` maps every canonical key of `new` to its ref; hand it to
    `apply_refs`. `tomb_updates` maps each old ref that found no node to
    `[type (or kind), label cut to 40 chars with …, sel (or the ref), prev capture id]`.
  - Side effect: `assign` writes `match`, `since` and `rebound_of` onto `new`'s
    nodes (`refs.annotate`), so `apply_refs` carries them into the published
    index. `since` is the capture where the ref was first assigned: the prev
    node's `since` (or the prev capture id) when carried, else `new.meta.id`
    (None when that is not a capture id yet).
  - `plan(...)` is the same without the side effect and returns the whole
    `Assignment` (`stats` counts per match kind, `rebound`, `ambiguous`).
- **Helpers for the store and the query layer**: `identity_flags(new.meta,
  prev.meta) -> (same_pid, same_generation)`; `merge_tomb(tomb, updates)` (cap
  5,000; dict order is the LRU order, newest last) or `apply_to_lineage(state,
  updates)`; `touch_tomb(tomb, ref)` on a lookup; `stale_ref_error(ref,
  capture_id, tomb)` builds `ref_not_in_capture` with the last-seen info and the
  old `sel` as a candidate. `LineageState` is re-exported from `model`.
- **Decisions beyond spec 3.9**:
  - Pass 1 treats `view:`, `sem:` and `a11y:` keys as device identity. Slot keys
    and `a11y:path:` keys are positional, so pass 1 accepts them only when the
    type, label and content (first three labels below) also agree.
  - The collection guard is decided per cell. A cell is a child of a collection:
    a View whose class is RecyclerView, ListView, GridView (and relatives), a
    `Lazy*` display type, an `a11y.collection` facet, a Compose `CollectionInfo`
    attr, or any node whose anchor's last segment has `[i]`. The cell's identity
    label is its first label that no other cell of that collection has (with two
    or more cells on screen). The cell keeps its refs when that identity is
    unchanged. Otherwise it must stay at the same position: the same adapter
    position (`adapter_pos`, else the CollectionItemInfo position, which is
    `row * columns + column` in a grid) when both sides have one, else the same
    child index after the list's scroll offset (the most common index shift of the
    cells matched by key; a tie means no offset). At the same position a cell with
    no identity label on either side keeps its refs; a cell whose identity label
    changed keeps them only when its old data did not move to another cell and
    another distinguishing label or testTag of the cell is unchanged (an in-place
    edit). A lone label changing in place is a data-set change (a page-sized
    scroll, a filter, a refresh) and rebinds. Otherwise every node of the cell
    gets a new ref, and the ones that matched by key get `rebound_of`.
  - Locators (pass 2) respect the guard too: a node inside a cell carries by a
    locator only when its cell is already matched to the other node's cell, and a
    cell root only when both collections are matched and show two or more cells.
    A lone section header or a one-page pager is unique on both sides and still a
    different item.
  - Structure matches a sibling that is unique by `(kind, type, rid, tag,
    label)` on both sides even when its ordinal changed. Look-alikes are split by
    content. True twins match by ordinal only when the whole twin group is
    unchanged; otherwise they are ambiguous, and they and their subtrees stay out
    of the geometry pass. Collection cells match by content only.
  - Geometry also needs equal labels (or equal content for label-less nodes),
    skips nodes inside collection cells, and never breaks an IoU tie.
  - The a11y uniqueId locator is read from `facets.a11y.unique_id` (or
    `uniqueId`); C4 should store it under `unique_id`.
- **Views without device identity (L1).** A #rid is unique per screen, not per
  app: without an identity match, the locator, structure and geometry passes
  pair a View only with a View of the same class inflated from the same layout
  (`layout_res`), so another activity's #coordinator_layout never takes the ref
  (live: Thunderbird's message list and composer).
- **Test helpers**: `tests/capture_keyscenes.py` (renamed from `capture_scenes.py`
  when C4's scene module of that name merged) builds key-space scenes (`V`, `C`,
  `S`, `A`, `scene()`), re-keys them (`rekey`, `shift_udids`, `key_space`), and
  `Chain` publishes a sequence the way the store will (plan, annotate, apply,
  merge tombstones).

## Diff (C8, `capture/diff.py`)

- **`diff(a, b, *, within=None, include=None, min_move_px=4, limit=40,
  max_bytes=4000, cursor=None, image=False, props_a=None, props_b=None,
  pixel_diff=None, resolve=None, preview=None) -> dict`**. `a` and `b` are
  ref-space indexes of one lineage (different lineages raise
  `OpError("bad_args")`, as do bad argument values). Nodes compare by ref.
- **What the section 10 signature leaves to the caller** (injected, so diff stays
  pure):
  - `props_a` / `props_b`: `node -> {name: value}` (or None) for each capture.
    Pass them only when both captures have properties; values may be the brief
    `{value, source?}` form.
  - `pixel_diff(a, b, refs) -> dict` (C9 through S1, e.g. a lambda around
    `images.pixel_diff(la, lb, a, b)`). It runs when `image=True` or `include`
    has `pixels`; its result is returned under `image`.
  - `resolve(ix, sel) -> UNode` for `within` (C6's `resolve_selector`). Without
    it, `within` takes refs, keys and aliases. It is looked up in `b`, then `a`.
  - `preview(ix, root) -> list[str]` for the "new screen" outline (C6's outline
    lines). The built-in fallback is a depth-2 walk of the ui tree.
- **`include`**: None means the spec's six defaults, plus `props` when both
  accessors are given and `params` when both captures have slot nodes. A list
  or comma string replaces the defaults; `+x` tokens add to them. Asking for
  something neither capture has adds a note instead of failing.
- **Result**: `{a: "id @label", b, dt_s, same_pid, within?, summary, notes?,
  lines, issues?, image?, truncated?, next}`.
  - `summary` partitions b's nodes in scope: `changed`, `moved`, `unchanged`
    (shared refs) plus `added` and `rebound` (the latter only when non-zero);
    `removed` counts a's nodes that are gone.
  - Lines: `~ <ref Type #rid @tag "label">: <change>` first, then
    `~ <ref> <change>` for more changes of the same node. The label is left out
    of the name when the change is the label itself. `> ...: <old parent> -> <new
    parent>` or `reordered in <parent> (i -> j)` (a longest increasing
    subsequence keeps the minimum set of siblings in place). `+`/`-` lines are
    outline lines with `+N` descendants. A rebound pair is `~ <new>: rebound, was
    <old ref> "<old label>" (+k inside)`. A pure translation is `shifted by
    dx,dy to [x,y wxh]`; descendants that shift with their parent are counted
    `(+k inside)`, and three or more siblings shifting together share one line
    `~ N nodes in <parent> shifted by dx,dy: n1 n2 n3 +k`.
  - Order: b's pre-order for changed, moved, added and rebound nodes, then the
    removals in a's pre-order.
  - `issues`: `{resolved, new}`, each `"<rule> ×N: n1 n2 n3 +k"` (at most 6
    rules), compared on the nodes both captures hold (a rebound pair is one
    node, shown by its new ref): `resolved` means the node is still there and
    the finding is gone. The issues of removed nodes are counted as
    `gone_with_node`, those of added nodes as `on_new_nodes` (when non-zero);
    a scroll or a closed dialog resolves nothing. An issue change alone does
    not make a node "changed". When only one
    capture ran `lint=full`, `a11y.contrast*` rules are not compared (noted);
    when one ran `lint=none`, issues are not compared at all (noted).
  - Slot nodes are compared only when both captures have a slot table.
  - Volatile properties (`pressed`, `hovered`) are ignored. At most 6 property or
    param lines per node, then `props +N more`.
- **Verdict**: when shared refs are under 40% of the union of ui refs (a rebound
  pair counts as shared), the result is `{..., verdict: "new screen", shared:
  "6 of 20 refs (30%)", summary: {added, removed, kept, rebound?}, outline,
  next: ["outline(capture=...)"]}` with no `lines`.
- **Budget and cursor**: at most `limit` (1..200) lines and `max_bytes`
  (`query.resolve_max_bytes`: 0 = the 32,000 ceiling, else clamped to
  500..32,000) bytes of compact JSON. Pages always make progress. The cursor is
  `<b id>:d:<hash8>:<offset>`; the hash covers a, b, within, the effective
  include and min_move_px (not limit or max_bytes). A cursor used with other
  arguments raises `bad_args`. The continuation hint repeats a, b and every
  non-default argument: `diff(a=..., b=..., [within=, include=, min_move_px=,
  image=, limit=, max_bytes=,] cursor=...)`.
- **Line grammar.** Diff segments are rendered by `lines.seg_row`/`seg_text`
  (display types, JSON-quoted odd rids and tags), so a diff line names a node
  exactly as an outline line does and its tag or rid pastes as a selector.

## Analyzers (C7, `capture/analyzers.py` + `capture/rules.py`)

- **What they read from a capture.** Only the section 10 `LoadedCapture` surface:
  `meta`, `raw(name)`, `shot(root)`, `derived(name)` and `put_derived(name, bytes)`.
  `raw()` may return `None`/`b""` or raise `KeyError`/`OSError` for a missing
  facet. `shot()` may return the stored bytes or a `Screenshot` message.
  `tests/loaded_fakes.FakeLoaded` is a stand-in for the store's `LoadedCapture`.
- **`analyze(ix, loaded, lint=, density=, font_scale=)`** accepts a
  `LoadedCapture`, a `RawCapture` (at capture time, before publish) or None
  (render signals only).
  - It owns every `render.*` and `a11y.*` issue and replaces them on each run, so
    it is idempotent. Issues with other ids are kept.
  - It sets `UNode.stop` and `Index.reading` only when the capture has an a11y
    facet.
  - Diagnostics use the prefixes `lint:`, `contrast:` and `reading:`. After a
    contrast run, `contrast: sampled N windows` is also how `lint_view` and
    `lint_summary` know contrast ran.
  - Run it on the ref-space index (after `apply_refs`). Links in evidence
    (`clipped_by`, `children_ids`, `node_ids`) hold node ids and are not rewritten
    by `remap_ids`. They still resolve through `Index.get()` from either id space.
- **What the analyzers expect from the index builder (C4).**
  - `ids["a11y"] = "host:virtual"` on a node that carries the a11y facet.
  - When the ID1 detector fires, register each matched a11y node's
    `a11y:path:<root>:<0.i.j>` key as a `by_key` alias. Reading order maps
    TalkBack stops through it, and never guesses a duplicated `(host, virtual)`
    pair.
  - The a11y facet's `flags` use the UNode vocabulary. `hidden` means not visible
    to the user.
  - `compose.attrs` keep their raw keys, so `VerticalScrollAxisRange` and
    `HorizontalScrollAxisRange` give a scroll container's axis.
  - Children of a collection have `[i]` in their `anchor`. `type` groups
    same-type siblings.
- **The lint adapter** runs `a11y_lint.run_lint` over the stored trees, exactly as
  the live `a11y_lint` tool does: the unified a11y tree (`raw/a11y.pb` through
  `a11y.a11y_to_dict`, Views and Compose in one pass, so View screens are linted
  too) with the Compose semantics (`raw/compose_sem.pb`) joined for detail.
  - `_A11yDump` decodes `raw/a11y.pb` once per analysis; the lint and the reading
    order share it. A finding maps to its dump node by `(host, virtual)`; a pair
    the dump repeats (ID1) is told apart by the finding's window and bounds, or
    reported as unmapped. The dump node maps to an index node as a TalkBack stop
    does (the `a11y:path:` alias first, then a unique pair).
  - Contrast asks `run_lint` for window screenshots through `_StoredShots`, which
    serves each window's own stored screenshot (a scale-less one gets its width
    over the window's).
  - Evidence keeps what an agent can act on. Dropped: what the node itself says
    (`label`, `class_name`), how the lint worked (`checked`, `bounds_source`,
    `standard`, `floor_dp`, `clipped_axes`, sampling), and the lint's typed keys
    of other nodes (`duplicates`, `duplicate_of`). `node_ids` names the OTHER
    nodes involved (R12's same-label group, R13's twin, from
    `duplicate_of_id`), as node ids that `apply_refs` turns into refs.
  - No a11y facet: no lint, and the diagnostic `lint: not run: no accessibility
    tree`. A rule that raised is reported as `lint: N rule errors: ...`.
  - `LINT_CACHE_VERSION` 2: cached lint results from the Compose-semantics input
    are not served.
  - A finding whose node is not in the index is reported in diagnostics, never
    dropped silently.
- **Render issue evidence.**
  - `render.clipped`: `{visible_px, declared_px, visible?, clipped_by, edge,
    scroll?, est?}`. The inferred form has `est: "sibling median"`.
    - Severity is info at a scroll edge (normal scrolling) and warn when a
      non-scrolling parent clips the node.
    - conf is exact for a known declared box or a scroll viewport, and inferred
      otherwise.
  - `render.hidden`: `{why, hides?}`.
  - `render.offscreen`: `{rect, outside: window|screen}`.
  - `render.zero_size`: `{w, h}`.
  - Only nodes with content (a label, text or actions), or containers of such
    nodes, are reported, at most one render issue per subtree. Content scrolled
    fully out of a scroll container is not an issue.
- **Inferred clips compare look-alikes (L1).** `render.clipped` (inferred)
  measures a node at a scroll edge against the median of siblings of the same
  type *and* #rid (a row's #star_click_area is not a #divider), and a scroll
  container whose actions do not name an axis gets it from its CollectionInfo
  (rows x 1: vertical; 1 x cols: horizontal).
- **False positives at a scroll edge.** A touch-target finding on a node clipped
  at a scroll edge gets `note: "likely false positive: clipped at scroll edge"`.
  A contrast finding there gets a low-confidence note and conf inferred.
- **Rule catalog (`rules.py`).**
  - It holds R1..R12 plus R13..R18 (the unified lint's rules). `lint_view` reports
    a rule the installed lint cannot produce under `unavailable`.
  - It also holds the four render rules, plus the reserved `render.text_overflow`,
    `render.covered` and `render.drawn_mismatch` (`planned`).
  - `resolve()` accepts ids, aliases, ATF check names, short codes and family
    prefixes (`a11y.`, `render.`), all case-insensitive. Anything else raises
    `OpError("bad_args")`.
  - Short codes are unique: a group with one rule keeps the group (`role`), a
    group with several gets `group_x` (`label_missing`, `label_redundant`,
    `text_fixed_scaling`, `text_too_small`). The bare group still selects all of
    them (`resolve("label")`, `find(issue="label")`).
  - An issue id the catalog does not know is still shown, with a generic entry.
- **`lint_view()`.**
  - By default it reports `a11y.*` rules. `render.*` issues appear with
    `rules=["render."]`, and `next` points at `find(issue="render.")`.
  - `contrast=True` and `wcag=True` results are cached as `lint.<hash8>.json`,
    stored by canonical key.
  - Cursors are `<capture>:l:<hash8 of args>:<offset>`. A cursor from other
    arguments or another capture is `bad_args`. `limit` and `max_bytes` are not
    hashed, so the page size may change between pages, and the cursor hint
    repeats every non-default argument (rules, severity, within, contrast, wcag,
    group, per_rule, limit, max_bytes).
  - A page always holds at least one finding: when nothing fits, the first one
    in its smallest form (one example node, then no msg/fix), and the optional
    fields are shed to stay within `max_bytes` (500 at least).
  - `lint_summary(ix)` returns the `lint` and `issues` one-liners for `capture()`.

## Images (C9, `capture/images.py`)

- **One additive `LoadedCapture` method for C2: `derived_path(name) -> str`.** It
  returns the absolute path where derived artifact `name` lives, whether or not it
  exists yet. Images check it to reuse a PNG without reading it back.
  - A name under `img/` (e.g. `img/n22-p16-1a2b3c4d.png`) lives in the capture's
    `img/` directory. Any other name (e.g. `lint.1a2b3c4d.json`) lives in
    `derived/`.
  - `put_derived(name, data)` writes atomically (temp, then replace) and returns
    that same path. `derived(name)` returns the bytes or None.
  - `tests/loaded_fakes.FakeLoaded` implements exactly this.
- Images also read `meta`, `index()` and `shot(root)`.
- Outputs are named by a parameter hash: `img/<ref>-p<pad>-<hash>.png` for
  crops, `img/ov-<kind>-<hash>.png` for overlays, `img/w_<udid>.png` and
  `img/base-*.png` for the pictures underneath, and
  `img/pdiff-<a>-<hash>.png` (under b's capture) for pixel diffs.
- An overlay's hash includes its labels and colours, so issues added later (for
  example contrast) never serve a stale overlay.
- Crops, `inline()` and the PNG codec work without Pillow.
- `overlay()` and `pixel_diff()` raise `OpError("unsupported")` without Pillow.
- A missing window screenshot raises `facet_unavailable`. A node with no pixels
  (zero size, or an unlinked slot) raises `bad_args`.
- With neither `window` nor `ref`, an overlay covers the whole screen,
  composited from every window in z order. With `ref` it is drawn on that node's
  crop.
- `overlay(loaded, ix, kind, marks, *, window, ref, pad, max_side)` hands
  `overlay.render_items` coordinates already in image pixels (`scale=1.0`), with
  `color_idx` 1 for error, 3 for warn, 0 for info and 2 for no issue. That works
  with both main's `render_items` and the a11y branches' version.
- `pixel_diff(la, lb, a, b, *, refs=None, threshold=24, max_side=1024,
  max_boxes=20)`: `refs` (for example the changed refs from `diff()`) chooses
  which boxes are drawn. Otherwise it boxes the deepest nodes whose pixels
  changed.

## Integration (improve/capture-core)

The module branches were merged onto improve/capture-base and run end to end in
`tests/test_capture_pipeline_offline.py` (fetch -> build_index -> `analyze` on the
key-space index and `prev.index()`, both outside the lock -> under `refs_lock`:
re-check the lineage's latest, `refs.assign`, `apply_refs`, `publish` -> `load`,
then every query, lint, image and diff call). Where two modules disagreed, this is
what now holds:

- **Capture order (S1).** `store.lock` serializes every publish, `next_refs`,
  label, pin, drop and GC eviction of every process, so hold it for milliseconds:
  `refs.assign`, `apply_refs` and `publish` only. Run `analyze` before taking it,
  on the key-space index (`lint="full"` spends ~4 s on contrast):
  `remap_ids`/`apply_refs` carry issues, stops and `reading` over and rewrite the
  node ids in issue evidence (`model.EVIDENCE_REF_FIELDS`: `clipped_by`,
  `children_ids`, `node_ids`). Hydrate the previous capture's index before the
  lock too, and under it only re-read `lineage_state().latest` and reload when
  another process published meanwhile. A rebuild the store runs while this
  thread holds the lock never runs contrast (lint "full" runs as "tree") and is
  not saved.
- **Rebuilt indexes stay in ref space.** When a rebuild emits a key the refmap
  lacks (a newer index builder), the store mints refs for it under the lock and
  adds them to `refmap.json` (additive: existing entries never change), so the
  next capture of the lineage can match against it. `refs.assign` also ignores
  (never matches, never tombstones) a prev node without a ref; only a prev with
  no ref at all is rejected.

- **Test helpers.** C4 and C5 both added `tests/capture_scenes.py`. C4's protobuf
  scene builder keeps the name; C5's key-space builder is
  `tests/capture_keyscenes.py`.
- **Props accessor (C6 <- C2/C4).** `LoadedCapture.props(view_udid)` returns
  `{name: normalized value}` through `LoadedCapture.facet_reader()`, a C4
  `FacetReader` over the capture's raw files (read on first use, one
  PropertyGroup decoded per view). `query.node(..., props=...)` finds it without a
  `props_fn`; the ops layer can still pass one.
- **`since` (C5 -> C2).** `refs.assign` cannot name a capture that has no id yet,
  so a node minted in this capture has `since=None` until `publish`, which sets it
  to the new id (on every id retry) for every node whose `match` is `new`.
- **Default rebuild (C2 -> C4, C7).** An unreadable index is rebuilt with
  `build_index` + `apply_refs` and then `analyze(ix, raw, lint=meta.options.lint)`,
  so issues, stops and `reading` come back (no contrast while the store lock is
  held; see above). `match`/`since`/`rebound_of` are not in the refmap and are not
  restored.
- **a11y facet flags (C4 -> C6, C7).** `facets.a11y.flags` use the UNode
  vocabulary (`click`, `longclick`, `focus`, `focused`, `scroll`, `checkable`,
  `checked`, `partial`, `selected`, `disabled`, `heading`, `edit`, `password`,
  `hidden`, `live`, `tgroup`). `focus` stays beside `click` there (the facet says
  what the framework reported). Booleans with no word that still matter are
  under `facets.a11y.more` (`context_clickable`, `accessibility_focused`,
  `dismissable`, `content_invalid`, `field_required`, `can_open_popup`,
  `a11y_data_sensitive`, `request_initial_focus`); the rest are dropped.
  `facets.a11y.unique_id` carries the a11y uniqueId (C5's locator).
  `facets.a11y.b` is left out once the View's `b` became that same rect.
- **Reading order (C7).** `talkback.reading_order`, the TalkBack 17 model, over the
  whole `a11y_to_dict` dump (window order, windows under a dialog). It merges a
  clickable row's Text into the row's stop (RO1). A ScrollView's children are
  top-level scroll items, each a stop; TalkBack's `isScrollable` reads scroll
  actions, not the `scrollable` flag, so a focusable container without a scroll
  action is one stop that speaks its text. The recorded launcher (pre-ID1, no
  service, so no traversal links: composition order) reads the 12 rows, then the
  title; a live dump from a current agent starts at the title, as TalkBack does.
- **Outline (C6).** In `detail="semantic"`, an a11y-only leaf with no rid, tag,
  issue, stop or action of its own, under a clickable/long-clickable parent that
  has a label, collapses (counted in `hidden.collapsed`): it is Text the row
  already speaks. `outline(root=@launcher_list)` is the spec's 13 lines again.
- **node() trims (C6).** `since` is left out when it is the capture itself;
  `a11y.res` is left out when it only repeats the rid; `Focused=false` is left
  out of `compose.sem`; brief slot lines leave out params that are Compose
  defaults (`softWrap=true`, `maxLines=inf`, `minLines=1`, `overflow=Clip`,
  `enabled=true`, zero elevations), content lambdas and bare theme `colors`
  objects (`params="raw"` and `+params:` still show them).
- **Non-default props for rare classes (normalize, C6).** `nondefault_props(...,
  groups=)` takes an optional fallback peer group per view: a view whose class has
  fewer than 3 instances uses the majority of its group. C6 passes
  `normalize.family_group(class_family(...))` (the widest family under View, e.g.
  TextView for a Switch) over at most 200 peers, so theme-wide values (hint and
  highlight colours, autofill flags) do not read as customised. Phase-0 callers
  pass no groups and are unchanged.
- **Measured with the pipeline** (compact bytes; target in parentheses):
  (re-measured after the unified lint and the TalkBack model landed)
  - launcher: capture stand-in 1,069 (2,500); `outline()` 2,026 (2,500);
    `outline(root=@launcher_list)` 1,653 (2,000); `outline(view="reading")` 1,567
    (2,000); `find(text="state", flags=click)` 388 (600); `node(@launch_heading)`
    1,382 (1,500); `lint()` 726 (1,200); crop 281 (400); captures list stand-in
    174 (400).
  - View screen (now linted: 14 findings): capture stand-in 2,202 (3,000);
    `outline()` 2,733 (3,000; 38 Views on lines, 2 ViewStubs hidden); `lint()`
    1,340 (4,000); `node(#badSwitch, props="nondefault")` 1,196 (1,200).
  - wide: capture stand-in 2,488 (3,000); outline pages <= 5,913 (6,000), 4 pages
    holding exactly the 259 Views; `find(text="Label 4", limit=20)` 1,547 (3,000);
    `node(#view_47, props="nondefault")` 786 (1,500); `outline(root, depth=1)` 641.
- **Gaps found at integration, since closed:** the View screen had no lint
  findings while the adapter read Compose semantics (the unified lint closed it),
  and action `0x01020036` showed as `CUSTOM_0x01020036` (action-ids decodes it as
  `SHOW_ON_SCREEN`).

## Ops (S1, `inspector_widget/ops.py`)

- **Entry points.** One function per tool, `capture`, `captures`, `outline`, `find`,
  `node`, `image`, `lint`, `diff`, each `fn(ctx, **args) -> dict`, raising `OpError`.
  `ops.run(ctx, tool, args)` maps every exception to the error envelope
  (`error_envelope`): `OpError` as it is; `adb.DeviceError` -> `no_session`;
  `TransportError` (a lost session), `AdbError` and other `OSError` -> `device_lost`;
  `InjectionError` saying the app is not running or not debuggable -> `no_session`;
  `AgentTimeoutError`, `ClientError` and any other `InjectionError` -> `agent_error`; anything
  else is a bug and gets the code `internal` (outside the spec's vocabulary on
  purpose: it is not the agent's fault).
- **`OpContext(store, sessions, caller)`.** `sessions` is a `SessionProvider`:
  `get(serial, package)`, `close_all()`, and optionally `live_pid(serial,
  package)` (a cached session's pid, no device I/O) and `device(serial)` (`{dpi,
  font_scale}`; else adb's, cached per context). `ops.AttachProvider` attaches with
  `inspector_widget.attach` and disconnects at `close_all()` (never SHUTDOWN): the
  CLI's provider and the tests'. The MCP server's wraps its session cache.
  `caller` changes nothing in the responses: the CLI and the MCP get the same bytes.
- **Session defaulting.** `device_lineage` (capture): explicit serial and package;
  else the lineage of `diff_from`/`if_changed_since` when it names one capture;
  else the store's default session (a lone serial or package must match it);
  else `adb.resolve_serial` (honours `$ANDROID_SERIAL`) and the single running
  debuggable app, `no_session` with candidates otherwise. `query_lineage` (every
  other tool) stops before adb: explicit, the capture argument's lineage, the
  default session; a lone serial or package picks the one lineage of the store
  that matches (`ambiguous`, or `capture_not_found` when none); None resolves
  across the whole store. `attach` (MCP and CLI) records the default session.
- **Capture order** as above ("Capture order"), with the generation: the lineage
  latest's `compose_generation` when the pid is the same, raised by
  `OpContext.bump_generation` (the MCP's legacy `dump_compose(enable_inspection)`
  calls it). The CLI cannot see a hot reload made by another process's legacy
  tool; the carry-over's collection guard and locators are the fallback there.
- **`diff_from`** is resolved before any device I/O, in terms of the new capture:
  `prev` is today's `latest`, `latest~N` today's `latest~(N-1)`, `latest` itself is
  `bad_args`. A diff that fails after the publish is reported inside `diff`
  (`{a, error}`), never as a failed capture. **`if_changed_since`** that names no
  capture (the first poll) captures.
- **The capture summary** (spec 5.3), within `max_bytes` (3,000): `capture`,
  `label`/`moved_from`, `pinned`, `session`, `pid`, `device`, `took_ms`,
  `consistency`, `facets` (counts, or the status reason: `off`, `not populated
  (...)`), `windows`, `lint`, `issues`, `warning` (slots=enable), `diagnostics` (at
  most 3, 120 chars), `store` (memory-only), `note` (the session's), `diff`
  (`{a, summary, lines<=10, issues?}`; empty issue deltas left out), then the
  preview `outline` (depth 2, at most `outline_lines` lines and 800 B, 400 B next to
  a diff), `on_screen` (the labelled TalkBack stops the preview does not show, in
  reading order, at most 3), and `next`. Every cut list ends with `…N more: <call>`;
  a budget below the header's size never drops the header.
- **Staleness** on every query response, right after `capture` (after `b` in
  diff): `age_s` over 120 s; `stale: "<latest> is newer (<dt>s)"` where dt is how
  much later the lineage's latest was taken; `pid_changed: true` when the
  provider's live pid, else the latest capture's pid, differs.
- **Resolution details.** A `cursor` names its capture, which wins over the
  default `capture="latest"`. `diff(a="prev")` is b's predecessor (`meta.prev`),
  not the lineage's second newest. `node(refs=[x])` with one entry is `node(ref=x)`.
  `node(image=true)` embeds `{path, px}` of the crop (or `{error}`).
- **`captures export`**: `format="raw"` copies the protobuf replies (the whole
  capture for what nodes/all/raw, else the facet's pb) and `format="legacy"`
  regenerates the old dump JSON (views, compose, slots, a11y); a `what` without
  that form is `bad_args` (L1: both were ignored).
- **`captures`.** `list` shows the resolved session's lineage (all of the store
  when none, or with `all=true`), newest first, and says how many captures of
  other apps it hides; `show` is the meta (non-default options, facet statuses,
  diagnostics, path); `label` with an empty label removes it; `export` writes
  `out/{nodes.jsonl|nodes.json, views.json, compose.json, slots.json, a11y.json,
  props.json, issues.jsonl}`, or for `raw` the stored files as they are
  (`out/raw/*.pb`, `out/shot/w_<root>.pb`, `out/meta.json`), and returns the path (the `out/` directory
  for several files), rows and bytes, never contents; `gc` summarizes the
  store's gc (`all=true` wipes everything, the ref counter included).
- **node() trim (C6).** node() leaves out `key` when its `ids` spell it
  (`view:16` = `ids.view` 16, `sem:82:448` = `ids.sem`, `a11y:34:21` for an a11y-only
  node); slot and `a11y:path:` keys stay.
- **Measured** through `ops.run` over the harness fake adb and agent
  (`tests/test_capture_budgets.py`, compact bytes; target in parentheses):
  - launcher: capture 1,153 (2,500); capture with a diff 1,229; `outline()` 2,012
    (2,500); `outline(root=@launcher_list)` 1,639 (2,000); `outline(view="slots")`
    491 (6,000); `outline(view="reading")` 1,553 (2,000); `find(text="state",
    flags=click)` 388 (600); `node(@launch_heading)` 1,276 (1,500); `lint()` 453
    (1,200); `captures()` 259 (400); `image(ref)` 296 (400).
  - View screen: capture 1,482; `outline()` 2,688 (3,000, all 40 Views);
    `node(#badSwitch, props="nondefault")` 1,197 on a recapture (1,200); `lint()`
    1,330 (14 findings).
  - wide: capture 1,517 (3,000); outline pages <= 5,926 (6,000), exactly the 259
    Views; `find(text="Label 4", limit=20)` 1,505 (3,000); `node(#view_47,
    props="nondefault")` 782 (1,500).
  - mixed: capture 1,271; `outline()` 992; `lint()` 500.
  - Workflows (tokens, spec section 8): W1 907 (1,100), W2 563 (1,100), W3 1,004
    (1,200; 1,462 with the stand-in renderer), W4 1,976 (2,800), W6 1,261 (2,400),
    W7 870 (1,600).

## Surface (S2, `inspector_widget/surface.py`)

- **One registry.** `SPECS` holds a `ToolSpec` per tool (`name`, `cli_name`,
  `summary`, `params`, `fn` = the ops function, `read_only`, `toolsets`,
  `description`, a human renderer). `json_schema(spec)` gives the MCP
  inputSchema, `mcp_entries(toolset, context=, passthrough=)` the entries
  `mcp_server.TOOLS` holds (with `surface` and, added by the server, `on_error`),
  `add_cli(subparsers, context=)` the subcommands.
- **Parameters.** A `Param` is on both surfaces unless allow-listed: `image.inline`
  is MCP-only (the pixels ride as ImageContent), `image.out` and
  `capture.build_out` are CLI-only. The CLI flag is `--kebab-name` with the MCP
  default (booleans that default to true get `--x/--no-x`), plus aliases
  (`-s -p -c`, `--scale`, `--count`, a repeatable `--rule`); `captures`'s action,
  id and label, `node`'s refs, `image`'s ref and `diff`'s a/b are positionals. A
  list parameter takes `a,b` on the CLI. `cli_args` passes only values that differ
  from the default, so `inspector-widget outline` is exactly MCP `outline()`.
- **Validation** (`validate`, stdlib, one place for every transport): unknown
  names, types (a whole-number float is an int), enums, ranges and rule ids
  (`rules.resolve`) raise `bad_args` with a hint describing the parameter; an
  explicit null means the default; a comma string is accepted for a list. The MCP
  server skips its own jsonschema check for these tools.
- **Toolsets.** `INSPECTOR_WIDGET_TOOLSET` is a name or a comma list: `legacy`
  (the 15), `capture` (the 4 session tools + the 8 = 12), `talkback` (4 + 3),
  `all` (26). The default is `legacy,talkback`: exactly the 18 tools (and the
  18,337 B tools/list) of before, until the deliberate flip (S4). An unknown name
  logs a warning and lists the default. Every tool stays callable by name.
  Measured tools/list (compact): default 18,337 B, capture 11,634 (12,000),
  legacy 13,247, capture,talkback 16,724, all 28,385 (the capture tools spend each
  parameter description once; the instructions carry the rest). Instructions:
  764 B (capture), 875 B (capture,talkback), 714 B (the default).
- **Instructions** (`instructions(listed)`, at most 900 B): the spec 5.13 text when
  `capture` is listed, else a legacy text naming the toolset variable; plus a
  TalkBack sentence when `tb_walk` is listed. Sent in initialize by the SDK 1.x
  and 2.x servers (a Server without the parameter gets none) and the fallback.
- **Running.** `execute(name, args, ctx, surface=, passthrough=)` returns a
  `Result` (a dict, plus `images` and `is_error`); `run()` gives `(text, images,
  is_error)`. `image(inline=true)` adds `inline_tokens` to the text and the PNG
  (downscaled to `max_side`) as `(mime, base64)`. The MCP server passes
  `SessionLostError` through so its retry applies: `capture` is repeated once on a
  fresh attach unless `slots="enable"` (a hot reload is never repeated).
- **CLI.** `--json` prints the MCP text byte for byte (`--pretty` indents it);
  without it a human rendering (a header line, then the lines; `capture` prints
  its id first, `-q` only the id; `image` prints the path). Errors print the same
  JSON envelope to stderr, exit 1. The generated subcommands never resolve a
  serial through adb themselves (queries do no device I/O) and close their
  sessions without SHUTDOWN.

## Live verification and real replays (L1)

- **Fixtures.** `tests/fixtures/captures/<name>/`: `meta.json`, `raw/<facet>.pb.gz`,
  `shot/w_<root>.pb` and `source.json`, recorded on emulator-5558 (API 37,
  480 dpi) with the post-hardening agent: A11yProbe (launcher, launcher after
  slots=enable, View screen, all scenarios, D1 dialog, S1 before/after a scroll,
  the bare-widget defaults screen), Thunderbird's message list with View and
  with ComposeView rows (demo mailbox), Now in Android's For you and Settings
  dialog. `tests/capture_replay.py` records them (`record STORE ID NAME`) and
  serves one as a `fakescenes.SceneData` (`FakeAgent.from_capture(name)`);
  `replay_device` gives the harness device the recorded package, pid, density
  and font scale. `tests/test_capture_replay.py` runs every budget, the >= 95%
  `conf:exact` and no-ID1 checks, per-window crops, the scroll rebinding and the
  live regressions over them.
- **Hardened agent values.** SafeString sends an unlabelled AccessibilityAction as
  `<action>`, a labelled one as its label and a lambda as `<lambda>`;
  `normalize.is_action_attr(raw, key)` takes the marker and the SemanticsActions
  keys, so they stay actions, not attr values.
- **Composited screens** alpha-composite each window over the ones below (a
  dialog window is transparent outside its card).
