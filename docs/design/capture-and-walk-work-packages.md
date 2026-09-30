# Capture and Walk: work packages

Generated alongside `capture-and-walk.md`. Status is tracked in git history.

## F1: Offline fixtures: 259-view wide scene and real-data replay scenes
- Effort: M (1.5 days)
- Depends on: none
- Files: `host/tests/fakescenes.py`, `host/tests/fixtures/live/launcher/views_props.json.gz`, `host/tests/fixtures/live/launcher/compose_sem.json.gz`, `host/tests/fixtures/live/launcher/compose_slots.json.gz`, `host/tests/fixtures/live/launcher/a11y.json.gz`, `host/tests/fixtures/live/launcher/screen.png`, `host/tests/fixtures/live/viewscreen/views_props.json.gz`, `host/tests/fixtures/live/viewscreen/a11y.json.gz`, `host/tests/fixtures/live/viewscreen/screen.png`, `host/tests/test_fakescenes.py`

Start only after improve/offline-e2e-harness (host/tests/fakeagent.py: FakeAgent, FakeDevice, FakeAdb, Scene, behaviour hook) is merged to main. Do not edit fakeagent.py; build on it in a new module.

(1) wide_scene(fan=6): a Scene that reproduces the E6 fixture from scratchpad/entrypoints/fakeagent.py (build_tree/build_a11y/_props).
- A depth 0..3 LinearLayout tree with fan 6, giving 259 Views: 216 TextView leaves labelled 'Label N', each with a view_N resource id.
- Bounds x=n, y=3n, 300x60.
- About 60 properties per view, encoded like Properties.kt: 10 real ones plus attr_0..attr_49 INT32.
- a11y: clickable, focusable and enabled TextViews with actions 16/4/8/64/128.
- One window and a small screenshot.

(2) replay_behaviour(name): a FakeAgent behaviour that answers Hello, GetWindows, DumpTree (honouring include_properties, include_screenshot and root_id), GetProperties, Screenshot(root_id), DumpCompose (honouring include_semantics, include_slot_table and enable_inspection) and DumpA11y. Responses are protobuf rebuilt from the JSON fixtures.
- Converters must handle the strings.dump_tree_to_dict shape, the legacy MCP flat-bounds dump_tree shape (E3 values kept as recorded), the dump_compose_to_dict shape and the a11y_to_dict shape.
- Property values are re-encoded per Properties.kt (GRAVITY and INT_FLAG as a joined str_value; COLOR and DIMENSION as int32).
- Screenshots are converted from PNG to ABGR_8888: a 9-byte LE header plus pixels, deflated, exactly as Capture.kt does. Use Pillow, which is a test dependency.

(3) Copy and gzip the fixtures.
- Launcher: scratchpad/live/mcp_phase2_deps/{dump_tree_full,dump_compose,dump_compose_sem,dump_accessibility}.json plus live/ref_main.png.
- View screen: scratchpad/live/scen_f/{dump_props,a11y}.json plus scen_f/shot.png.
- Record the provenance (source file, timestamp, and the pre-ID1 caveat) in the fakescenes.py docstring.

See spec sections 8 and 11.

**Acceptance:** - On wide_scene, today's code (pre-Phase-0) reproduces the E6 sizes within 25% through mcp_server._call_tool_text over the harness fake adb: dump_tree about 149 KB, dump_tree+props about 1.74 MB, dump_accessibility about 488 KB, inspect about 547 KB, inspect+props about 3.85 MB.
- Launcher replay: compact strings.dump_compose_to_dict output is within 5% of 243,378 B; a11y_to_dict windows equal the fixture's windows; the CLI-shape tree round-trips (json to pb to dict equals json).
- png._decode_to_rgba on a replayed screenshot returns 1280x2856, and sampled pixels equal the PNG's.
- The whole test module runs offline in under 10 s with no adb.
- scripts/test.sh is green.

## P0-1: Output layer: compact encoding, budgets, spill envelope, brief slimming, value normalization
- Effort: M (2 days)
- Depends on: none
- Files: `host/inspector_widget/output.py`, `host/inspector_widget/normalize.py`, `host/inspector_widget/normalize_defaults.py`, `host/tests/test_output.py`, `host/tests/test_normalize.py`

Pure, dependency-free modules per spec sections 2, 3.7 and 10 (output.py and normalize.py contracts). Nothing is wired into the entrypoints in this WP.

output.py:
- dumps: compact, ensure_ascii=False, default=str; pretty on request.
- Budget: UTF-8 byte accounting with a 200 B footer reserve.
- finalize(tool, result, max_bytes, spill_dir): if the brief result is over budget, write the spill file and return the spill envelope (summary counts plus a generic depth-2 preview of 25 lines or fewer over the roots / windows[].root / children shapes, spill_path and hint). The default ceiling comes from INSPECTOR_WIDGET_MAX_BYTES (default 32000; 0 means unlimited).
- slim(tool, result, args): the per-tool brief rules of spec section 2.3 for all 15 legacy tools, including max_depth and root re-rooting on the result dict, user_code_only hoisting, the focus_order modes, group_by for a11y_lint, and properties as name-to-value maps (non-default for multi-view dumps). Omissions are counted. detail=full returns the input unchanged.
- OUTPUT_PARAMS table, augment_schemas(TOOLS) and add_cli_flags(subparser, tool), both generated from that one table (kebab-case flags, identical defaults).

normalize.py:
- compose_value, is_action_attr, textstyle_brief, is_library_source (with a LIBRARY_FILES set generated from the Compose/Material/Foundation jar sources), a11y_node_brief, color_hex, nondefault_props, KEY_PROPS.

normalize_defaults.py:
- STATIC_VIEW_DEFAULTS per class family, from the observations in scratchpad/live/scen_f/dump_props.json. Examples: pivot equals the centre; maxWidth and maxHeight equal INT_MAX; scrollbar defaults; defaultFocusHighlightEnabled, forceDarkAllowed, hapticFeedbackEnabled, saveEnabled and soundEffectsEnabled are true.

Do not import cli, mcp_server, a11y_lint or correlate. Do not edit strings.py or a11y.py.

**Acceptance:** - Unit tests cover every slim rule and every normalization rule, with omission counts asserted.
- detail=full is lossless: json.loads(dumps(slim(full))) equals the input.
- The envelope never exceeds max_bytes for max_bytes of 1000 or more, and the spill file loads back to the full brief result.
- A dev check run against scratchpad/live JSON (numbers pasted in the commit message) meets: launcher dump_compose with slots at 24,000 B or less; semantics-only at 6,000 or less; dump_accessibility at 12,500 or less; inspect at 13,000 or less; a11y_lint grouped at 1,200 or less; dump_tree+props at 8,000 or less. View screen: inspect at 18,000 or less; dump_tree+props at 20,000 or less.
- Importing the modules takes under 50 ms and does not import protobuf.
- ruff is clean on the new files.

## P0-2: Phase-0 wiring on both surfaces (compact, brief, budgeted, spilled) plus the E3 decoder fix
- Effort: M (2 days)
- Depends on: P0-1, F1
- Files: `host/mcp_server.py`, `host/cli.py`, `host/tests/test_phase0_budget.py`, `host/tests/test_phase0_parity.py`, `host/tests/test_cli_smoke.py`, `host/tests/test_mcp_transport.py`, `host/tests/test_symbol_parity.py`

Route every legacy tool result through output.slim and output.finalize, on both surfaces, in the same PR.

MCP. The only allowed hunks are:
- _call_tool_text: replace json.dumps(indent=2) with slim plus finalize, keeping the isError semantics.
- One output.augment_schemas(TOOLS) call after TOOLS.update.
- E3: tool_dump_tree and tool_get_properties use strings.dump_tree_to_dict / property_to_dict shapes, and the now-unused _resource_to_json, _bounds_to_json, _node_to_json, _property_to_json, _color_hex and _property_group_to_json are deleted. Check with the improve/session-lifecycle owner first and skip E3 if that branch already did it.
- The header comment (tool count).

CLI:
- _emit_json and the nine json.dumps(indent=2) sites go through output.
- build_parser gains flags via output.add_cli_flags; --pretty restores indent.
- --json FILE never spills; --json - behaves exactly like MCP.

Scope guard: no edits to SessionCache, _session_alive, _safe_detach, _run_tool, tool_attach, tool_detach, transport functions, or any session.detach()/inj.close() line, because improve/session-lifecycle owns them.

Add 'output' to the policed modules in test_symbol_parity.

Document in the dump_tree description that dump_tree(max_depth=1) lists the window roots.

**Acceptance:** - Over the harness fake adb and agent:
  - Launcher replay: dump_compose at 24,000 B or less; dump_compose(include_slot_table=false) at 6,000 or less; inspect at 13,000 or less; dump_accessibility at 12,500 or less; a11y_lint at 1,200 or less; dump_tree(include_properties) at 8,000 or less; get_properties at 3,500 or less.
  - View screen: inspect at 18,000 or less.
  - wide_scene: every tool's default response is 32,000 B or less; oversize responses are envelopes of 3,000 B or less whose spill_path loads to the full brief result; dump_tree(max_depth=1) is 2,000 B or less.
- detail=full with max_bytes=0 equals the pre-change content (compared as parsed JSON).
- MCP dump_tree and get_properties return strings.py shapes, with correct GRAVITY, INT_FLAG and DIMENSION values.
- test_phase0_parity: every new param has the kebab-case flag with the same default on the mapped subcommand, and CLI --json - output byte-equals the MCP text for the same args.
- tools/list is 18,500 B or less, compact.
- test_symbol_parity and scripts/test.sh are green.
- Live verify on emulator-5554 with com.oberkfell.a11yprobe: all 15 MCP tools via _run_tool and all 13 subcommands, with byte sizes recorded in the commit message and all within targets.

## G1: Record legacy golden outputs before the ops refactor
- Effort: S (1 day)
- Depends on: P0-2, F1
- Files: `host/tests/record_goldens.py`, `host/tests/golden/legacy/`, `host/tests/test_legacy_golden.py`

Record the outputs of all 15 legacy MCP tools (default and detail=full) and all 13 CLI subcommands (--json -, plus the human text for dump, compose, a11y and inspect). Run them through the real entrypoints (_call_tool_text and cli.main) over the harness fake adb and agent, on default_scene, wide_scene and the launcher and View-screen replays.

Mask volatile fields: temp and spill paths, timestamps, ports and pids.

The test compares parsed JSON and text against the goldens. The recording script regenerates them deliberately. Record the source commit in each golden file.

S3 must reproduce these goldens; the only allowed deltas are the documented ones (an added capture field, and hints that name new tools when they are listed).

**Acceptance:** - Goldens exist for 15 tools times 2 detail modes times 4 scenes, and for 13 subcommands times 4 scenes.
- test_legacy_golden is green on main after P0-2 and runs in under 30 s.
- Mutating any shaper (for example renaming a key in strings.node_to_dict) makes it fail with a readable diff.

## C1: Capture model, (de)serialization and test builders
- Effort: S (0.5-1 day)
- Depends on: none
- Files: `host/inspector_widget/capture/__init__.py`, `host/inspector_widget/capture/model.py`, `host/pyproject.toml`, `host/tests/capture_builders.py`, `host/tests/test_capture_model.py`

Create the inspector_widget.capture subpackage with the dataclasses and helpers in spec section 10 (model.py): CaptureOptions, CaptureMeta, RawCapture, UNode (fields of spec section 3.3), Tree, Index, Issue, LineageState, OpError with the error codes of spec section 5.1, and canonical key helpers (view:, sem:, slot:, a11y:, w:).

Also add:
- Index to/from index.jsonl(.gz) and meta.json serialization.
- 'inspector_widget.capture' in pyproject [tool.setuptools] packages, so wheel installs include it.
- host/tests/capture_builders.py: a tiny builder for hand-made Indexes (windows, views, compose, slot and a11y nodes, trees, issues, reading order). C5-C9 use it to test in parallel before the index builder exists. Include a builder for the launcher-like example of spec section 5.

**Acceptance:** - Index to jsonl round-trip is lossless, including unicode labels.
- Importing inspector_widget.capture does not import protobuf or Pillow.
- A wheel built from host/ contains inspector_widget/capture/model.py (checked in the test via setuptools packages config or a pip wheel smoke run).
- The builder reproduces the spec's launcher example: the refs, trees and labels used in spec sections 5.3-5.9.

## C2: Capture store: ids, atomic publish, labels, resolution, retention/GC, locks, memory LRU
- Effort: M (2 days)
- Depends on: C1
- Files: `host/inspector_widget/capture/store.py`, `host/tests/test_capture_store.py`

Implement CaptureStore per spec section 4 and the section 10 contract.

- Root resolution and env vars: INSPECTOR_WIDGET_CAPTURE_DIR, _TTL, _MAX, _MAX_MB, _PERSIST.
- Directory layout with mode 0700.
- Ids: c plus 5 Crockford base32 characters from urandom, via exclusive mkdir in .staging and publish by rename, with a .complete marker.
- store.json next_ref, allocated under refs_lock (flock), for the global ref counter.
- Lineage files: latest, history, per-lineage labels, tombstones.
- session.json: the default session.
- resolve(): id, label or @label, latest, prev, latest~N. Resolve within the lineage first, then store-wide. Identical results for any caller.
- load(): meta, lazy index, raw facet bytes, per-window shots, and derived get/put. Derived writes are atomic temp-then-replace under a per-capture lock.
- list, label (reporting moved_from), pin and unpin, drop (rename to .trash).
- gc: TTL based on .used mtime touched at most once a minute; per-lineage and total count caps with unlabeled captures first; byte cap that strips heavy files first; pinned captures exempt; staging older than 1 h purged; the spill/ directory with a 1 h TTL; gc(all=True) wipes everything including the counter.
- Memory-only mode.
- MCP in-memory LRU: 8 indexes.

**Acceptance:** - A multiprocessing test with 4 processes publishing 50 captures concurrently: all ids unique, no partial directory ever visible to a concurrent reader, and refs allocated disjointly.
- GC with a fake clock covers TTL, the per-lineage cap, the total cap, the byte cap with heavy-file stripping, the pin exemption and the unlabeled-first order.
- A publish killed before rename leaves only .staging content, which GC removes after 1 h.
- Deleting a capture under a reader raises OpError capture_not_found.
- resolve() returns the same id for callers 'mcp' and 'cli'.
- A label move reports moved_from, and labels are rejected when they match the id pattern.
- PERSIST=0 writes nothing under the configured root.
- Directories are 0700.
- Refs are never reused after gc() without all=True (property test).

## C3: Facet registry and fetch: per-window screenshots, slots policy, consistency fingerprint, settle, if_changed_since
- Effort: M (1.5 days)
- Depends on: C1, F1
- Files: `host/inspector_widget/capture/fetch.py`, `host/tests/test_capture_fetch.py`

Implement fetch(session, opts), fingerprint_now and settle per spec sections 3.1-3.2 and the section 10 contract, using only the public Session surface.

Request order:
- slots=enable: DumpCompose(enable_inspection) first.
- GetWindows.
- DumpTree(props, resolution_stack, screenshot of the first root).
- Screenshot(root_id) for each other root.
- DumpCompose(semantics only).
- slots if_available: DumpCompose(slots only, no enable).
- DumpA11y(extras, rendering).
- CaptureSkp per window, if requested and the method exists. Status is unsupported when the SKP version exceeds skiaparser's.
- Fingerprint re-check.

Store raw bytes. Split the DumpTree screenshot out into shots[firstRoot]. Keep each Screenshot message deflated.

Fingerprint: blake2b-128 over the tuples of spec section 3.2, excluding Focused and pixels. On a mismatch, retry up to 2 times 150 ms apart, then mark consistency unsettled.

Per-facet status, ms and bytes. A failing optional facet becomes status error without failing the capture.

Metadata (pid, api, abi, agent_version, build_id) is read via getattr so the code works before and after session-lifecycle's E9.

**Acceptance:** - Against the harness default_scene (2 windows), the RawCapture contains windows, views with props, shots for both roots (1001 and 2001), semantics, and a11y.
- The fake agent's request log shows the specified order.
- enable_inspection is sent only for slots=enable, and first.
- slots=if_available sends include_slot_table without enable_inspection.
- The fingerprint is stable across two fetches of an unchanged scene.
- A behaviour hook that mutates the tree between requests yields consistency unsettled after at most 2 retries.
- settle stops after two equal fingerprints.
- An injected a11y ERROR gives a11y status error and the other facets ok.
- Live check (scratch driver, results in the commit message) on emulator-5554 with the a11yprobe launcher: a warm fetch with props, screenshot, semantics and a11y takes 1.0 s or less.

## C4: Unified index builder: grafting, composite keys, a11y facet join with ID1 detector, slots linking, types/labels/flags/sel/anchors
- Effort: L (3-4 days)
- Depends on: C1, P0-1, F1
- Files: `host/inspector_widget/capture/index.py`, `host/inspector_widget/capture/anchors.py`, `host/tests/test_capture_index.py`

Implement build_index(raw) and apply_refs per spec sections 3.3-3.7 and 3.10. Decode via strings.StringResolver and the pb messages; do not edit strings.py.

- Build the ui, views, compose, slots and a11y trees.
- Window roots come from GetWindows root order (z).
- Graft the ACV semantics under the ACV View, folding the synthetic root that reuses the ACV id.
- Composite keys: sem:<acv>:<id>.
- Re-parent AndroidView subtrees under the smallest containing semantics node (interop flag, conf inferred).
- Screen-coordinate normalization: shift an ACV subtree whose semantics root lies outside the ACV's screen bounds, and record a diagnostic.
- a11y join: exact by (host_view_id, virtual_id). The ID1 detector (duplicate pairs) switches to one-to-one IoU of 0.6 or more per window, with conf inferred and a diagnostic. Unmatched a11y nodes become kind=a11y children.
- Slot nodes: normalized params via normalize.py; origin app or library; slot-to-semantics links by bounds, text and tag (conf inferred), with src copied to linked semantics nodes.
- Props decoded lazily per view from views.pb.
- Derive type (never invented; generic View is omitted), label, flags vocabulary, declared_b and visible, anchor and template anchor, and sel (shortest unique, in selector grammar form).

**Acceptance:** - Launcher replay: one window; the View spine plus 17 semantics nodes under the ACV with sem:<acv>:<id> keys; the ID1 detector fires on the pre-ID1 fixture, with the diagnostic present, all a11y facets conf inferred, and no a11y node attached twice.
- View-screen replay: 40 view nodes with a11y joined; props are decoded only when accessed.
- default_scene (post-ID1-style ids) joins exact.
- A synthetic scene with two ComposeViews that share semantics ids yields distinct keys, and compose:<id> is reported as ambiguous.
- A window-relative dialog semantics tree is shifted to screen px.
- sel is unique within every fixture capture; types, labels and flags are deterministic across runs.
- Build time is 100 ms or less on the launcher and 500 ms or less on a 5,000-node synthetic.

## C5: Refs and carry-over matcher (never reuse, rebound detection, tombstones)
- Effort: M (2 days)
- Depends on: C1
- Files: `host/inspector_widget/capture/refs.py`, `host/tests/test_capture_refs.py`

Implement assign(new, prev, same_pid, same_generation, alloc) per spec section 3.9, with a pure API; C2 persists the state.

Four passes:
1. Device identity (same pid and generation), with the collection guard: in a collection item, the label or the ordinal-free anchor must also agree, otherwise a new ref with rebound_of.
2. Unique locators: #rid within the window, @tag, a11y uniqueId.
3. Structure: matched parent, same type, same label, same ordinal.
4. Geometry: same type, IoU of 0.8 or more, one-to-one within a matched parent.

Ambiguous candidates get new refs. The first capture allocates in pre-order through alloc. Record match and since per node. Unmatched old refs become tombstones: [type, label up to 40 chars, sel, last_capture], capped at 5,000 with LRU.

Use capture_builders for fixtures.

**Acceptance:** The mutation suite passes:
- Unchanged scene: every ref kept, with match id.
- Text change with the same pid: ref kept.
- Insertion above: siblings keep their refs.
- Removal: tombstone written.
- pid change: refs carried via #rid, @tag and structure.
- Recycled RecyclerView cell (same udid, different label): new ref, rebound_of set.
- Generation bump: the id pass is disabled and locators still carry.
- Twin id-less siblings reordered: new refs, never swapped.

A 1,000-round random mutation property test never reuses a ref and never maps one ref to two nodes. Allocation is deterministic for identical inputs.

## C6: Query engine and line grammar: outline, find, node, selectors, projection, cursors, budgets
- Effort: L (3 days)
- Depends on: C1, P0-1
- Files: `host/inspector_widget/capture/lines.py`, `host/inspector_widget/capture/query.py`, `host/tests/test_capture_query.py`

Implement spec sections 5.1 and 5.5-5.7 and section 6 over an Index (tests use capture_builders).

- Line grammar v1, including collapsed chains, short issue codes, +N markers, tails and breadcrumbs.
- outline for views ui, views, compose, slots (origin filter), a11y and reading, with semantic collapse rules, max_children and max_lines.
- find with the structured filters of spec section 6.2 (text, text_re, type glob across names, rid, tag, src, role, flags, any_flags, has, missing, issue, within, at, overlaps, min_dp, max_dp, kind, window, in, sort, count_only), 2-level breadcrumbs, and a path for a single hit.
- node with facet priority truncation, an omitted list, the props modes (key, nondefault, all, names) via an accessor, params brief or raw, tap_xy at the visible centre, and batches of up to 10.
- The selector grammar of spec section 6.1: bad_selector errors with a column; not_found with 3 fuzzy labels; ambiguous with 5 lines.
- Field projection with + and - prefixes.
- format json rows.
- Stateless cursors.
- Budgets via output.Budget.

**Acceptance:** - Every rendered line matches the grammar regex (a test helper).
- Cursor completeness: walking all pages of a 5,000-node builder index yields a pre-order with no duplicates or gaps.
- Budgets are never exceeded for random max_bytes between 500 and 32,000.
- Each find filter has a test.
- Selector tests cover valid forms, whitespace errors with exact columns, and ambiguity.
- json rows equal the line fields.
- Semantic collapse turns a launcher-like builder index of 26 nodes into about 15 lines.
- p95 per call is 20 ms or less on 5,000 nodes.

## C7: Analyzers: render signals, rule catalog, lint adapter with ref mapping and template collapse, reading order
- Effort: M (2 days)
- Depends on: C1
- Files: `host/inspector_widget/capture/analyzers.py`, `host/inspector_widget/capture/rules.py`, `host/tests/test_capture_analyzers.py`

Implement spec section 3.8 and the lint view of spec section 5.9.

- Render signals: render.zero_size, offscreen, hidden (props visibility or alpha, or a11y not visible_to_user), and clipped (exact from declared_b; inferred at a scrollable ancestor's viewport edge when under 50% of the same-type siblings' median height, with clipped_by).
- Rule catalog: id, short code, sev, msg and fix, plus R1..R12 aliases. Unknown ids are rejected.
- Lint adapter: call a11y_lint.lint_tree on dicts rebuilt from the stored compose_sem.pb via strings.dump_compose_to_dict, with a LintContext built once from capture meta (dpi, font_scale). Map each finding to a ref through sem:<acv>:<id>. Isolate the input choice in one function so the switch to the unified a11y tree after improve/a11y-lint-unified is a one-function change.
- Contrast only when requested: RGBA from the stored per-window shot via png._decode_to_rgba; results cached in derived.
- Annotate touch-target findings on scroll-edge-clipped nodes as likely false positives.
- Grouping by rule and by node, with template collapse by src or template anchor.
- Reading order via a11y.compute_traversal_order mapped to refs (stop, Index.reading).

Do not edit a11y_lint.py, a11y.py or overlay.py.

**Acceptance:** - Builder fixtures produce each render signal with the correct conf and evidence.
- On the launcher replay (through C4 in S1, or via a builder copy here), every lint finding maps to a ref with none unmapped.
- Eight identical findings in RecyclerView cells collapse to one group line naming the template and giving 3 example refs plus the remaining count.
- R5 is accepted as an alias of its rule id; rule 'bogus' gives bad_args.
- Contrast is computed once and served from cache on the second call.
- Grouped lint for the launcher is 1,200 B or less.

## C8: Diff between captures (by ref), rebound/new-screen verdicts, issue deltas
- Effort: S-M (1.5 days)
- Depends on: C1
- Files: `host/inspector_widget/capture/diff.py`, `host/tests/test_capture_diff.py`

Implement diff(a, b, within, include, min_move_px, limit, max_bytes) per spec section 5.10.

- Compare nodes with equal refs.
- Change classes: changed (text or label, state flags, visibility, a11y speakable, role and actions, bounds moves or resizes of min_move_px or more), added, removed, moved (re-parented or reordered), and rebound (rebound_of pairs).
- Issue deltas: resolved and new, by (ref, rule).
- props and params deltas when both captures have them.
- Verdict 'new screen' plus b's outline preview when fewer than 40% of refs are shared.
- Cross-lineage input returns bad_args.
- Lines use the grammar prefixes ~, +, -, >.
- Budget and cursor.

The pixel option delegates to C9 through an injected callable.

**Acceptance:** - Builder pairs cover every change class.
- The badSwitch toggle example returns exactly the switch state change plus the props checked line, 1 KB or less.
- A 30%-shared pair returns the new-screen verdict.
- Cross-lineage input returns bad_args.
- Budgets are respected at random max_bytes.

## C9: Images: per-window crops, Set-of-Mark/lint/reading overlays keyed by ref, inline payloads, pixel diff
- Effort: M (2 days)
- Depends on: C1
- Files: `host/inspector_widget/capture/images.py`, `host/tests/test_capture_images.py`

Implement spec section 5.8.

- Materialize PNGs from the stored Screenshot pb via png.write_png or png._decode_to_rgba, cached under img/.
- Crop a node from ITS window's shot, applying the window offset, pad, clamping and the capture scale. Crops work without Pillow via a stdlib path.
- Overlays (marks: auto, all or a list of refs, at most 60, labelled with refs; lint and reading coloured and numbered from ref-keyed issues and stops; bounds; compose) delegate drawing to overlay.render_items. They require Pillow and raise a clear error otherwise.
- Downscale to max_side.
- inline(path, max_side) returns (mime, base64, estimated_tokens = w*h/750).
- Pixel diff: side-by-side with changed boxes and a delta mask.
- Every output path is cached by a parameter hash under the capture.

**Acceptance:** - A popup-window node crop in default_scene comes from shot w_2001 and its pixels equal Scene.paint.
- A scale-0.5 capture crops correctly.
- OV1 regression: a node carrying a warn issue is drawn in the warn colour.
- Mark labels are refs.
- inline downscales to max_side and reports the token estimate.
- Crops work with Pillow uninstalled (monkeypatched).
- A repeated call reuses the cached file.

## S1: Ops layer: capture pipeline, the new tool functions, session resolution, next hints, staleness
- Effort: L (3 days)
- Depends on: C2, C3, C4, C5, C6, C7, C8, C9
- Files: `host/inspector_widget/ops.py`, `host/tests/test_ops.py`, `host/tests/test_capture_budgets.py`

Implement OpContext, the SessionProvider protocol and the tool functions: capture, captures, outline, find, node, image, lint, diff. See spec sections 5 and 10.

Capture pipeline:
1. Resolve the session through the chain of spec section 5.1: explicit args, then the capture's lineage, then store.default_session, then the single running debuggable app on the single device (honouring ANDROID_SERIAL); otherwise no_session with candidates.
2. Optional if_changed_since short-circuit and settle.
3. fetch.
4. build_index.
5. Under store.refs_lock: load the lineage's latest index, refs.assign, apply_refs, analyzers.analyze, store.publish, update lineage state and the default session.
6. Optional diff_from.
7. Render the summary (preview, deduplicated on_screen, windows, lint and issues lines, diagnostics, warnings for slots=enable).

Track compose_generation per session (incremented on slots=enable).

All responses:
- Error mapping to OpError codes.
- next hints per spec section 6.6.
- Staleness markers: age_s, stale, pid_changed.

No surface wiring in this WP. Tests call the ops functions over the harness fake adb and agent with a provider that attaches through inspector_widget.attach.

**Acceptance:** - Golden sizes over the harness fake adb and agent:
  - Launcher replay: capture at 2,500 B or less; outline() at 2,500 or less; outline(root of the list) at 2,000 or less; outline(view=slots) at 6,000 or less; outline(view=reading) at 2,000 or less; find(text) at 600 or less; node at 1,500 or less; lint at 1,200 or less; captures list at 400 or less; image at 400 or less.
  - View screen: outline() at 3,000 or less with all 40 views; node(props=nondefault) at 1,200 or less.
  - wide_scene: capture at 3,000 or less; default outline pages at 6,000 or less, where paging yields exactly 259 View lines; find(text=Label 4, limit=20) at 3,000 or less; node(props=nondefault) at 1,500 or less.
  - A parametrized test shows every tool times every scene is within its default budget.
- Refs are unchanged across two captures of an unchanged scene.
- After a behaviour-hook mutation, diff reports exactly the mutation.
- if_changed_since returns unchanged without writing a capture.
- Envelopes are byte-identical for callers 'cli' and 'mcp' against the same store.
- next has at most 3 entries and at most 200 B.

## S2: Surface registry and thin wiring: new tools on MCP and CLI, toolsets, validation, instructions, ImageContent
- Effort: M-L (2.5 days)
- Depends on: S1, P0-2
- Files: `host/inspector_widget/surface.py`, `host/mcp_server.py`, `host/cli.py`, `host/tests/test_surface.py`, `host/tests/test_symbol_parity.py`, `host/tests/test_e2e_capture.py`

Start only after improve/session-lifecycle is merged to main: this WP edits _build_mcp_server and CLI session handling.

surface.py:
- Param, ToolSpec, and SPECS for the 8 new tools (capture, captures, outline, find, node, image, lint, diff).
- JSON schema generation.
- A stdlib validator (types, enums including rule ids and aliases, ranges; errors map to bad_args) used on all transports (E5 validation part).
- mcp_entries(toolset) in the existing TOOLS entry format, and add_cli(subparsers) generating kebab-case flags, positionals and human renderers (header line plus lines; --json prints the envelope; capture -q prints only the id).
- run(name, args, ctx) returning (text, images, is_error).
- INSTRUCTIONS text (spec section 5.13).
- Toolsets via INSPECTOR_WIDGET_TOOLSET: legacy (default in this WP), capture, all. Hidden tools stay callable by name.

mcp_server.py (thin):
- Register the new entries.
- Filter tools/list by toolset.
- Pass instructions in initialize on SDK 1.x, SDK 2.x and the fallback.
- Emit ImageContent for images, alongside TextContent.
- One call so attach records the default session.
- --self-check prints the active toolset.

cli.py (thin): surface.add_cli, and a CLI SessionProvider that uses Session.close() (never SHUTDOWN).

Extend test_symbol_parity to police surface, ops, output and capture.

**Acceptance:** - Auto-parity test: every spec is exposed as exactly one MCP tool and one CLI subcommand, with identical param names (snake to kebab) and defaults.
- The validator rejects bad types, enums and ranges with bad_args on SDK 1.x, SDK 2.x and the fallback.
- Toolsets:
  - legacy lists exactly the 15 legacy tools, with tools/list bytes unchanged versus G1.
  - capture lists 12 tools, tools/list compact at 12,000 B or less.
  - all lists 23.
  - Hidden tools are callable by name.
- initialize carries the instructions (900 B or less) on all transports.
- image(inline=true) yields ImageContent plus TextContent; the CLI prints a path.
- e2e with two processes over the fake adb: a capture made through the MCP _run_tool path is readable from cli.main by id, by label and via latest.
- CLI query subcommands send no SHUTDOWN (fake agent log).
- CLI --json equals the MCP text byte for byte.
- scripts/test.sh and test_symbol_parity are green.
- Live verify on emulator-5554: workflows W1-W5 of spec section 8 through both MCP and CLI, with byte totals recorded in the commit message and within 25% of the spec numbers.

## L1: Live verification, raw-pb fixtures from real captures, replay agent, defaults table refresh
- Effort: M (1.5 days)
- Depends on: S2
- Files: `host/tests/fixtures/captures/`, `host/tests/capture_replay.py`, `host/tests/test_capture_replay.py`, `host/inspector_widget/normalize_defaults.py`

On emulator-5554, preferably after improve/a11y-agent-identity has merged so ids are post-ID1, record real captures with captures export format=raw:
- the a11yprobe launcher;
- ViewScenarioActivity;
- the all-scenarios screen;
- optionally the interop RecyclerView+ComposeView scene (scratchpad/interop) on emulator-5556.

Commit the raw pbs and meta, with screenshots at full resolution, or downscaled if a fixture exceeds 3 MB.

capture_replay.py serves a recorded capture over the fake wire as a FakeAgent behaviour: FakeAgent.from_capture.

Re-record STATIC_VIEW_DEFAULTS from bare View, ViewGroup, TextView, ImageView, Button and Switch instances on device, and update normalize_defaults.py.

Run the live checks of spec section 13 items 3-5 and record the numbers in the commit message.

**Acceptance:** - Replayed real captures pass S1's budget assertions.
- On post-ID1 fixtures, 95% or more of a11y facets are conf exact, and the ID1 diagnostic is absent.
- Live on emulator-5554:
  - Warm capture p50 is 1.0 s or less (launcher; props, screenshot, semantics, a11y, tree lint).
  - outline, find and node p95 are 50 ms or less.
  - A slots=enable capture warns, bumps compose_generation, and keeps the refs of 90% or more of the @tag and #rid nodes.
  - Tap-then-diff on #badSwitch reports exactly the switch change, plus props checked, on the same ref.
  - A CLI invocation reads an MCP-made capture by label.

## S3: Legacy tools on captures (ReplaySession), CLI raw-Client unification, dead-code deletion
- Effort: L (3 days)
- Depends on: S2, G1, L1
- Files: `host/inspector_widget/ops_legacy.py`, `host/mcp_server.py`, `host/cli.py`, `host/tests/test_ops_legacy.py`, `host/tests/test_legacy_golden.py`

Start after improve/session-lifecycle is merged.

ops_legacy.py:
- Define the 15 legacy tools (and the 4 session tools with optional serial and package plus default-session side effects) as ToolSpecs registered through surface, with schemas that keep today's names, E13 fixes, rule-id validation (E10) and the Phase-0 params.
- Implementation: take a capture with the needed facets, or reuse the lineage's latest if it is under 2 s old with the same options. Then run the existing shapers and correlate (strings, a11y, correlate.inspect_tree, find_node and component_image) over ReplaySession(capture). ReplaySession serves the stored pbs through the Session method surface, honouring each call's args: semantics versus slots, props, screenshot root and scale, and capture_skp from the stored SKP.
- Add the capture field to every response; spill envelopes carry the capture id and toolset-aware hints.

mcp_server.py:
- Delete the hand-written TOOLS literal and the 15 _h_* wrappers, the decoder remnants, the screenshot helpers and second PNG encoder, the HostFacade/_first_attr fallbacks (keep an import probe for --self-check), _device_density and _lint_fn.

cli.py:
- Move the raw-Client subcommands (attach, dump, compose, a11y, a11y-lint) onto sessions: no SHUTDOWN except detach, and --force everywhere.
- Keep the human text renderers.

Do not edit correlate.py, a11y.py, a11y_lint.py or overlay.py.

**Acceptance:** - G1 goldens reproduce except the documented deltas (added capture field; hints naming new tools only when they are listed), and test_legacy_golden is updated only for those deltas.
- mcp_server.py is 700 lines or fewer and cli.py 400 or fewer.
- A grep test finds no remaining duplicate lint-context construction or overlay orchestration outside ops and capture.
- Rule id 'R1' works, and an unknown id gives bad_args.
- The fake agent log shows no SHUTDOWN from any subcommand except detach.
- test_symbol_parity and scripts/test.sh are green.
- Live verify on emulator-5554: all 15 legacy tools via _run_tool and all 13 subcommands, with outputs structurally equal to the goldens' shapes and sizes within Phase-0 targets.

## D1: Docs and accessibility Skill rewrite around capture and walk
- Effort: S-M (1-1.5 days)
- Depends on: S3
- Files: `AGENTS.md`, `CLAUDE.md`, `README.md`, `host/README.md`, `host/PACKAGING.md`, `skill/inspector-widget-a11y/SKILL.md`, `skill/inspector-widget-a11y/tools.md`, `skill/inspector-widget-a11y/rules.md`

Update every document to the post-flip state, landing together with S4.

- AGENTS.md section 5: capability table, toolsets, tool counts (12 in the capture toolset, 15 legacy, 23 in all), CLI subcommands, and the fast commands.
- CLAUDE.md: the fast commands and self-check description.
- README and host/README: the capture workflow, the line grammar (documented once), budgets, store location, env vars (INSPECTOR_WIDGET_TOOLSET, _CAPTURE_DIR, _CAPTURE_TTL, _CAPTURE_MAX, _CAPTURE_MAX_MB, _CAPTURE_PERSIST, _MAX_BYTES), and privacy notes.
- PACKAGING.md: the capture subpackage.
- SKILL.md and tools.md rewritten to the workflow capture, lint, outline(view=reading), node, image(overlay=lint), fix, capture(diff_from=baseline); rules.md: rule ids, short codes and aliases.
- Respect AGENTS.md section 1 naming: no codename renames.

**Acceptance:** - Tool names and counts in every doc match mcp_server.py --self-check output for each toolset (checked by a small test that greps the docs).
- Every fast command in CLAUDE.md runs as written on a checkout.
- The Skill's example calls validate against the surface schemas (a test parses the fenced calls).
- No stale references remain to 'seven tools', to the 15-tool default, or to the claim that the CLI re-injects and tears down.

## S4: Flip the default toolset to capture (deliberate deprecation of legacy listing)
- Effort: S (0.5 day)
- Depends on: S3, D1, L1
- Files: `host/inspector_widget/surface.py`, `host/tests/test_surface.py`, `host/cli.py`

This is the deliberate deprecation step and needs the owner's sign-off before merge.

- Change the default INSPECTOR_WIDGET_TOOLSET from legacy to capture.
- Legacy tools stay callable by name and are listed with TOOLSET=legacy or all.
- The legacy CLI subcommands print one stderr pointer line to the new equivalent (for example 'dump -> outline --view views'), and their stdout is unchanged.
- Record a release note in the commit message.

**Acceptance:** - The default tools/list has exactly 12 tools and is 12,000 B or less compact.
- TOOLSET=legacy and TOOLSET=all restore 15 and 23 tools.
- tools/call to a hidden legacy tool succeeds.
- Legacy CLI stdout is byte-identical to the G1 goldens (after S3's deltas) with exactly one added stderr line.
- Live check on emulator-5554 of the tools/list for each toolset through the real MCP SDK.
- The owner's approval is noted in the commit message.

## R1: Phase 3: text-overflow and visibility facts from the agent; render.text_overflow analyzer
- Effort: M-L (2.5 days)
- Depends on: C7, S2
- Files: `proto/view_inspection.proto`, `agent/src/main/kotlin/com/oberkfell/viewspector/agent/payload/TreeBuilder.kt`, `agent/src/main/kotlin/com/oberkfell/viewspector/agent/payload/ComposeInspector.kt`, `host/inspector_widget/proto/view_inspection_pb2.py`, `CONTRACT.md`, `host/inspector_widget/capture/analyzers.py`, `host/tests/fakeagent.py`, `host/tests/test_render_text_overflow.py`

Start after improve/session-lifecycle, and coordinate with the agent-hardening stream, which also edits TreeBuilder and ComposeInspector.

Additive proto fields only:
- ViewNode.text_layout {line_count, ellipsis_count, max_lines, visible_text_end}, from TextView.getLayout.
- ViewNode effective visibility: alpha, is_shown.
- ComposeNode.text_layout for semantics nodes with Text, via the GetTextLayoutResult semantics action: line_count, has_visual_overflow, did_overflow_width and height, ellipsized, visible_text_end (see RENDER_DESIGN TextLayoutInfo).

Regenerate the gencode with the pinned protoc. Update CONTRACT.md. Extend fakeagent to encode the new fields.

Add the analyzer rule render.text_overflow (conf exact), with evidence (visible substring, maxLines, overflow mode, and src when slots are linked). The host tolerates older agents: absent fields mean no signal.

Add a deliberately ellipsized label to the a11yprobe corpus only in coordination with improve/a11yprobe-corpus.

**Acceptance:** - Protobuf changes are additive only (a proto compatibility check test).
- Fake-agent tests produce render.text_overflow with correct evidence; an agent without the fields produces no signal and no error.
- The build passes (./scripts/build.sh).
- Live on emulator-5554: find(issue=text_overflow) hits the ellipsized label, with src when the capture has slots.

## R2: Phase 3: act tool (tap/type/scroll by ref, then settle, capture and diff)
- Effort: M (1.5 days)
- Depends on: S4
- Files: `host/inspector_widget/act.py`, `host/tests/test_act.py`

Implement act(action, target, text, then, settle_ms) per spec section 5.11 in a new module, and register its ToolSpec through surface's extension hook (a register_spec call from act.py, imported by surface). No other edits.

- Resolve the target to the centre of its visible part from the latest capture; refuse when visible is 0, with a scroll hint.
- Run adb shell input tap, swipe, text or keyevent through inspector_widget.adb.shell.
- Settle by fingerprint, then capture with carry-over.
- Return the diff envelope.
- CLI: act tap n15 via the generated subparser.

**Acceptance:** - The fake adb records 'input tap X Y' at the exact visible centre.
- A visible=0 target gives a refusal with a hint.
- The response is 2 KB or less and contains the diff.
- Parity test covers the new spec.
- Live on emulator-5554: act(tap, #badSwitch) reports the switch change on the same ref.

## R3: Phase 3: atomic CaptureCommand, WindowInfo, generation counter, exact slot-to-semantics links, package_hash
- Effort: L (4 days)
- Depends on: R1, L1
- Files: `proto/view_inspection.proto`, `agent/src/main/kotlin/com/oberkfell/viewspector/agent/payload/Dispatcher.kt`, `agent/src/main/kotlin/com/oberkfell/viewspector/agent/payload/ComposeInspector.kt`, `host/inspector_widget/proto/view_inspection_pb2.py`, `CONTRACT.md`, `host/inspector_widget/capture/fetch.py`, `host/inspector_widget/capture/index.py`, `host/tests/test_capture_atomic.py`

Agent plus host, additive, coordinated with the agent-hardening stream.

Agent:
- CaptureCommand(facets bitmask) returns all facets from one main-thread pass plus a per-window PixelCopy.
- GetWindows gains WindowInfo {type, title, z, focused, frame}.
- A change_count / generation counter.
- Slot NodeGroups carry the LayoutNode semanticsId, giving exact slot-to-semantics and AndroidView-holder links.
- ComposeNode package_hash (app or library origin).
- Optionally, design notes for a startup-attach mode that enables inspection before the first composition.

Host:
- fetch prefers CaptureCommand when the agent supports it and falls back to multi-request otherwise.
- index uses exact links (conf exact) and package_hash origin.
- Windows get a kind (activity, dialog or popup).

**Acceptance:** - On a new agent, a capture issues one request (plus screenshots per the design), and skew is at most one frame.
- Old agents still work through the fallback (fake-agent test for both).
- Slot links and interop re-parenting report conf exact.
- Dialogs show kind dialog.
- Protobuf changes are additive; CONTRACT.md is updated.
- Live on emulator-5554, the launcher capture p50 improves on L1's number, recorded in the commit message.
