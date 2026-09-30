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
- **The lint adapter** is `_lint_windows()`. It is the only place that chooses the
  lint input, and today that input is the Compose semantics from
  `raw/compose_sem.pb`.
  - Node ids are replaced by surrogates, so colliding semantics ids across
    ComposeViews (ID3) stay distinct. Each surrogate maps back to
    `sem:<acv>:<id>`, and the synthetic window root maps to `view:<acv>`.
  - When improve/a11y-lint-unified lands, return the unified a11y tree there.
    Findings that carry a typed `node_key` (`view:`, `compose:<acv>:<id>`,
    `virtual:`) already map through `_finding_key`.
  - A surrogate id wins over a typed key. Over Compose input, the unified lint
    derives its keys from the ids it is given (`compose:1:<surrogate>`), and
    those keys name no real node. The launcher mapping was checked against that
    branch's `a11y_lint.py`: every finding maps.
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
  - An issue id the catalog does not know is still shown, with a generic entry.
- **`lint_view()`.**
  - By default it reports `a11y.*` rules. `render.*` issues appear with
    `rules=["render."]`, and `next` points at `find(issue="render.")`.
  - `contrast=True` and `wcag=True` results are cached as `lint.<hash8>.json`,
    stored by canonical key.
  - Cursors are `<capture>:l:<hash8 of args>:<offset>`. A cursor from other
    arguments or another capture is `bad_args`.
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
