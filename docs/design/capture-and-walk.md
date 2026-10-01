# Inspector Widget: Capture and Walk (final spec)

Status: final synthesis. The base is the **llm-first** design: agent surface, carried refs, line grammar, tight budgets, `next` hints and analyzers. Grafted in:
- from **state-architecture**: the store, facet registry, per-window screenshots, structured find filters, breadcrumbs, the on-screen digest and the "rebound" class;
- from **incremental-migration**: Phase 0, the test gates, golden-before-refactor, MCP `instructions`, `if_changed_since`, and capture ids surfacing through legacy tools.

§1 addresses every fatal flaw the judges raised.

Evidence used: the real outputs in `scratchpad/live/` (launcher: `mcp_phase2_deps/*`, `inspect0.json`, `compose0.json`; View screen: `scen_f/*`), the synthetic 259-view outputs in `scratchpad/capdesign/`, the audit ledger (`scratchpad/me/FINDINGS.md`, `A11Y_STREAM.md`, `RENDER_DESIGN.md`, `BACKLOG_R3.md`), and the current code on `main` (2b002f8).

---

## 0. Summary

### 0.1 Diagnosis (measured)

| Response (today, MCP `indent=2`) | Bytes | ~Tokens | Notes |
|---|---|---|---|
| `dump_compose`, real launcher (slot table populated) | 567,142 | ~140k | Over Claude Code's ~25k-token MCP output cap, so unusable |
| `inspect --properties`, launcher | 241,839 | ~60k | Over the cap |
| `dump_accessibility`, launcher | 91,773 | ~23k | |
| `inspect`, launcher (26 nodes) | 79,987 | ~20k | |
| `a11y_lint`, launcher (14 findings) | 11,754 | ~3k | |
| 259-view fake screen: `dump_tree` / `+props` / `dump_accessibility` / `inspect` / `inspect+props` | 149 KB / 1.74 MB / 488 KB / 547 KB / 3.85 MB | 37k … 960k | |

Where the bytes go:
1. **`json.dumps(indent=2)`** makes output 2.3-3.9x larger than compact JSON (inspect 77,747 → 26,234 B; compose 567,073 → 243,378 B).
2. **Compose slot params as Kotlin `toString()`** are 75% of compact `dump_compose`.
   - `style`, `modifiers`, `textStyle` and `typography` alone are 117 KB.
   - 381 of 399 composables have `id=0`, so none can be addressed.
   - 116 are zero-size effects; only 56 are app call sites (`MainActivity.kt`).
3. **Semantics lambda attrs** (`AccessibilityAction(label=null, action=Function1<...>)`): the four text-substitution and GetTextLayoutResult actions are 5.2 KB of inspect0's 12.9 KB compose facet.
4. **Accessibility noise.** Actions with ids take 6.0 KB, `SPANS_START_KEY:"[]"` extras 3.5 KB, per-node `package_name` 1.6 KB, and `-1` sentinels 2.8 KB; `actions_bitmask` duplicates `actions`. `focus_order` repeats the tree (18-22%).
5. **Properties** come as `{name,type,is_layout,value}` lists, about 115 per view. A static-defaults filter leaves roughly 8-12 per view.
6. **No way to narrow or address.** There is no depth, root, paging or projection control, and nothing lists root ids. Every call re-dumps the device, so no two calls see the same moment. Ids are unaddressable: `compose:<id>` collides (ID3) and a11y ids are wrong (ID1).

### 0.2 Decision

**Phase 0 (ships first, no new concepts).** Output hygiene at the output boundary of the existing 15 MCP tools and 13 CLI subcommands:
- compact JSON;
- brief-by-default slimming, with counted omissions and `detail="full"` to roll back;
- `max_depth` and `root`;
- a 32 KB byte budget with a spill envelope.

Measured on the real launcher: `dump_compose` 567 KB → ≤24 KB, `inspect` 80 KB → ≤13 KB, `dump_accessibility` 92 KB → ≤12.5 KB, `a11y_lint` 11.8 KB → ≤1.2 KB. Any oversize response (e.g. every 259-view dump) becomes a ≤3 KB envelope plus a spill file. That is an 85-96% cut per call on real screens.

**Phases 1-2 (capture and walk).**
- `capture` snapshots one moment once: views and properties, Compose semantics (plus the slot table if it is already populated), the unified a11y tree, per-window screenshots, tree lint and render signals.
- The snapshot goes into an immutable on-disk store that the CLI and the MCP server share.
- The agent then walks it with `outline`, `find`, `node`, `image`, `lint` and `diff`, which do no device I/O.
- Every logical node has one short ref (`n23`). Refs are carried across captures of the same app and are **never reused**, so a stale ref errors instead of silently pointing somewhere else.
- List output is one line per node, about 70-95 B each.
- Each tool has a default budget (capture 3 KB, outline 6 KB, find and node 3 KB).
- 12 MCP tools and 12 CLI subcommands are generated from one registry.

Headline results on real data (prototype-measured, see §8):

| Agent task | Today | New |
|---|---|---|
| "Why is this label cut off?" | ~160k tokens; not feasible in Claude Code | 3 calls, ~1.1k tokens |
| "Is the checkout button accessible?" | ~27k tokens | 3 calls, ~1.1k tokens |
| "What changed after I tapped X?" | ~40k tokens plus a manual diff | 3 calls, ~1.6k tokens |
| Accessibility audit | ~30k tokens; overlay colours broken | 5-6 calls, ~2.8k tokens |
| 259-view list screen | 137k-960k tokens | 4 calls, ~2.4k tokens |

---

## 1. Decisions, and the fix for each judged flaw

| Judged flaw | Fix in this spec |
|---|---|
| llm-first selector DSL contradicts its own examples (a space is both the descendant combinator and an atom separator) | **No CSS-like DSL.** `find` takes structured, schema-validated filters (§6.2). Node selectors are a tiny grammar with no whitespace combinator: only ` > ` (direct child), and errors report the exact column (§6.1). |
| llm-first stores one `screen.png` per capture, so dialog and popup crops use the wrong pixels (L3) | **Per-window screenshots** are stored as raw `Screenshot` protobufs per root (`shot/w_<rootUdid>.pb`). Crops, overlays and contrast use the node's own window image plus that window's screen offset. |
| llm-first sequential capture ids are reused after GC | **Random ids**: `c` + 5 Crockford-base32 chars, created by exclusive `mkdir` with retry on collision. They never come from a counter. |
| state-architecture lazily backfills props into an existing capture (mixed UI states) | **Captures are immutable.** Every facet is fetched at capture time according to the options; props are eager by default. Nothing is ever backfilled. A missing facet returns `facet_unavailable` with the exact recapture call. |
| state-architecture's `latest` is process-local in the MCP and store-wide in the CLI | `latest` is **store-wide on both surfaces**, filtered by the resolved session. Both surfaces use the same resolver function. |
| incremental-migration's capture-local ordinal handles silently repoint after a recapture | Refs are **store-global, monotonic, carried by a matcher and never reused**. There is one ref per logical node (the view, compose and a11y facets share it). |
| incremental-migration lists ~24 overlapping tools during migration | **Toolsets never overlap by default.** Until the deliberate flip (S4) the default toolset stays the legacy 15, and the new tools are opt-in with `INSPECTOR_WIDGET_TOOLSET=capture` (or `all`). After the flip the default is the 12. Hidden tools stay callable by name. |
| Correlation is built on unlanded identity fixes (ID1/ID3/CO1/CO4) | Composite semantics keys are computed on the host, which fixes ID3 without an agent change. An **ID1 detector** (duplicate `(host_view_id, virtual_id)` pairs) falls back to one-to-one IoU matching per window. Every node carries per-facet `conf`, and capture `diagnostics` say when a fallback was used. "Exact" correlation is only an acceptance target on data recorded after improve/a11y-agent-identity. |
| Global labels (state-architecture) | Labels are unique **per lineage** (serial + package). |
| `TTL=0` overloaded to mean memory-only | A separate `INSPECTOR_WIDGET_CAPTURE_PERSIST=0`. |
| (gap in all three) N identical lint lines for N collection cells | Findings are **template-collapsed** by `src` or by anchor with collection ordinals wildcarded (§3.8). |
| (gap in all three) No text-overflow signal | Pulled forward as optional WP R1 (additive agent fields). The analyzer slot exists from Phase 1. |

What was deliberately left out: the CSS selector DSL, lazy facet backfill, and a separate `list_windows` tool. Windows are listed by `capture`, and in Phase 0 by `dump_tree(max_depth=1)`.

---

## 2. Phase 0: output hygiene (ship first)

### 2.1 Modules

- `host/inspector_widget/output.py` (new, pure):
  - encoding (`dumps`: compact, `ensure_ascii=False`, `default=str`);
  - budgets (`Budget`);
  - spill and the envelope;
  - per-tool `slim()`;
  - the `OUTPUT_PARAMS` table;
  - `augment_schemas(TOOLS)` for MCP and `add_cli_flags(subparser, tool)` for the CLI, both generated from that one table.
- `host/inspector_widget/normalize.py` (new, pure): Compose value normalization, the library-source classifier, action-attr detection, the brief a11y node, colour and dimension formatting, and non-default property filtering.
- `host/inspector_widget/normalize_defaults.py` (new, data): the static View property defaults table.

Slimming happens **only at the output boundary**. Shapers (`strings`, `a11y`, `correlate`), lint and overlays keep consuming full dicts and are not edited. That avoids conflicts with the a11y streams.

### 2.2 Rules applied to every tool on both surfaces

1. **Compact JSON everywhere.** MCP `_call_tool_text` and every CLI JSON site emit compact JSON; `--pretty` restores `indent=2` for humans.
2. **`detail`** is `"brief"` (default) or `"full"`. `full` reproduces today's content exactly (compact).
3. **`max_bytes`**: default 32,000 (env `INSPECTOR_WIDGET_MAX_BYTES`; `0` means unlimited), range 1,000..200,000.
   - When the brief result is over budget, the response becomes the **spill envelope** (§2.5) and the full brief result is written to a spill file.
   - CLI `--json FILE` never spills (it is already a file). CLI `--json -` and MCP behave identically.
4. **Omissions are always counted.** Examples: `"omitted":{"defaults":57}`, `"hidden":{"library_composables":325}`.

### 2.3 Brief rules per tool

| Tool | Brief output (`detail="full"` restores the legacy shape) |
|---|---|
| `dump_tree` | E3 fixed (the `strings.py` node shape). `bounds` become `[x,y,w,h]` (plus `render` quad only if transformed); `qualified_name` is dropped when it equals package.class. `include_properties` gives `properties: {view_id: {name: value}}` holding **non-default** values, with `omitted_defaults` per view. New params: `max_depth`, and `root` (a node id in the response to re-root on). |
| `get_properties` | `{name: value}` map of **all** properties (the caller asked for one view). Colours become `#AARRGGBB`; dimensions are px. `filter="nondefault"` is optional. |
| `dump_compose` | Semantics attrs are normalized (§3.7); lambda attrs become an `actions:[names]` list. Slot nodes are `user_code_only=true` by default: library composables are hoisted away and `hidden.library_composables` is counted. Values are normalized and each is capped at 120 chars. New params: `max_depth`, `root`. |
| `compose_overlay` | `on_screen` is capped at 30 entries, with `on_screen_total`. |
| `dump_accessibility` | Each node drops `-1` sentinels, `package_name` when it equals the app package, `actions_bitmask`, action ids (names kept), focus/selection/granularity boilerplate actions, empty `SPANS_START_KEY` extras, `important` (already implied by `important_for_accessibility`) and the default-true flags (`enabled`, `visible_to_user`); `disabled` and `hidden` are added instead. `focus_order` defaults to `"stops"`: `[{order, id, speakable}]` for stops only. `"full"` and `"none"` are the other values. |
| `a11y_lint` | `group_by="rule"` (default): `{summary, density, contrast_sampled, by_rule:{rule:{sev,n,msg,nodes:[≤3 ids],more}}, diagnostics}`. `group_by="none"` gives the old `findings` list. |
| `inspect` | Per node: `node_key`, `bounds:[x,y,w,h]`, `view{id,class_name,resource?,text?}`, `compose{name,attrs(normalized),actions,src}`, the brief a11y node (no `id`/`host_view_id`/`virtual_id` when `conf` is exact), and `conf` only when not exact. New params: `max_depth`, `root`. Properties (with `include_properties`) become non-default maps. |
| `inspect_node` | Brief a11y, normalized compose attrs, properties as a non-default map. |
| `list_devices`, `list_processes`, `attach`, `detach`, `screenshot`, `a11y_overlay`, `component_image` | Compact only. |

**CLI parity.** Every new MCP param has a kebab-case flag with the same default on the mapped subcommand: `--max-bytes`, `--detail`, `--max-depth`, `--root`, `--user-code-only/--no-user-code-only`, `--focus-order`, `--group-by`, `--filter`, `--pretty`. `test_phase0_parity.py` checks this.

### 2.4 Tool-list cost
Param descriptions are one line each. `tools/list` (compact) must stay ≤ 18,500 B; today it is ≈15.9 KB.

### 2.5 Spill envelope (≤ 3,000 B)

```json
{"truncated":true,"tool":"inspect","bytes":102907,"max_bytes":32000,
 "summary":{"windows":1,"nodes":260,"view":259,"compose":0,"a11y":259,"max_depth":4},
 "preview":["view:1001 LinearLayout #view_1 [1,3 300x60] +6","  view:1002 LinearLayout #view_2 [2,6 300x60] +6","  …23 more lines"],
 "spill_path":"~/Library/Caches/inspector-widget/spill/inspect-20260930T142233-7f3a.json",
 "hint":"Narrow with max_depth=2 or root=<id>, raise max_bytes (<=200000), or read spill_path with jq."}
```

- The preview is a generic depth-2 walk (≤25 lines) over the known legacy shapes (`roots`, `windows[].root`, `children`).
- Spill files live in `<store>/spill/` with a 1 h TTL.
- From S3 on, the envelope also carries `"capture":"c…"` and, when the capture toolset is listed, a hint such as `outline(capture="c…", root=…)`.

### 2.6 Phase-0 targets (MCP and CLI `--json -`, compact, defaults)

These come from prototype `scratchpad/synth/slim_proto.py` on the real outputs, with slack.

| Screen | Tool | Today | Target |
|---|---|---|---|
| launcher | `dump_compose` (slots populated) | 567,142 | ≤ 24,000 |
| launcher | `dump_compose(include_slot_table=false)` | 23,941 | ≤ 6,000 |
| launcher | `inspect` | 79,987 | ≤ 13,000 |
| launcher | `dump_accessibility` | 91,773 | ≤ 12,500 |
| launcher | `a11y_lint` | 11,754 | ≤ 1,200 |
| launcher | `dump_tree(include_properties)` | 95,530 | ≤ 8,000 |
| launcher | `get_properties` (one view) | 8,865 | ≤ 3,500 |
| View screen | `inspect` | 132,868 | ≤ 18,000 |
| View screen | `dump_tree(include_properties)` (40 views) | 648,551 | ≤ 20,000 or envelope |
| 259-view | any tool, default args | 149 KB-3.85 MB | ≤ 32,000; oversize → envelope ≤ 3,000 |
| 259-view | `dump_tree(max_depth=1)` (lists windows) | n/a | ≤ 2,000 |

### 2.7 Rollback
Setting `INSPECTOR_WIDGET_MAX_BYTES=0` plus `detail="full"` (or `--detail full --max-bytes 0`) reproduces today's content, compact.

---

## 3. Data model

### 3.1 Capture
A capture is an immutable record of one moment of one app on one device.

- `meta.json`:
  - identity and target: `id`, `schema:1`, `lineage:{serial,package}`, `pid`, `api`, `abi`, `agent_version`, `agent_build` (read with getattr; filled once session-lifecycle's E9/H4 land);
  - device: `device:{dpi, font_scale, screen:[w,h], orientation}`;
  - timing: `created_at`, `took_ms`;
  - `options` (the capture args), `facets:{name:{status, reason?, ms, bytes}}`;
  - consistency: `fingerprint`, `consistency: settled|unsettled`, `compose_generation`;
  - lineage links and flags: `prev`, `label?`, `pinned`, `diagnostics:[…]`.
- Raw facets are the source of truth, stored verbatim as protobuf bytes:
  - `raw/windows.pb`, `raw/views.pb` (DumpTree with PropertyGroups), `raw/compose_sem.pb`, `raw/slots.pb` (optional), `raw/a11y.pb`;
  - `raw/a11y_render.pb` (optional), `raw/skp_<root>.bin` (optional);
  - `shot/w_<rootUdid>.pb`: one `Screenshot` message per window, still deflated. It is never held as RGBA beyond one request; a full-resolution 1280x2856 frame is 14.6 MB as RGBA.
- `refmap.json` maps canonical key to ref. It is part of the record, not derived, because refs depend on the carry-over at creation time.
- Derived and rebuildable:
  - `index.jsonl.gz` (one node per line), rebuilt from raw plus `refmap` when `schema` changes;
  - `derived/lint.<opthash>.json`, `img/*.png` (materialized PNGs, crops, overlays), `out/*` (exports, spills).

### 3.2 Facet registry (capture/fetch.py)

Every facet has a fetch function, a policy and a status (`ok | off | unavailable | unsupported | error`, plus a reason). New facets such as RENDER_DESIGN's `DumpRender`, WindowInfo and text layout are one registry entry each.

| Facet | Wire request | Policy (default) | Stored |
|---|---|---|---|
| windows | GetWindows | always | `raw/windows.pb` |
| views + props + first-window screenshot | DumpTree(root 0, props=`props`, resolution_stack=`resolution_stack`, screenshot=`screenshot`, scale) | always (props on) | `raw/views.pb` plus `shot/w_<firstRoot>.pb`, split out of the response |
| other windows' screenshots | Screenshot(root_id=r) for each other root | on when `screenshot` | `shot/w_<r>.pb` |
| compose semantics | DumpCompose(sem=true, slots=false) | always | `raw/compose_sem.pb` |
| slot table | DumpCompose(sem=false, slots=true, enable_inspection=(slots=="enable")) | `"if_available"`: reads without hot-reload, empty unless inspection is already on. `"enable"` is **destructive** and is issued **first**, before every other facet. `"off"` skips it. | `raw/slots.pb` |
| a11y | DumpA11y(extras=true, rendering=`a11y_rendering`) | always | `raw/a11y.pb` |
| SKP | CaptureSkp per window | opt-in (`skp=true`) | `raw/skp_<r>.bin`. Status is `unsupported` when the SKP version exceeds skiaparser's (API 37: v110 > 109). |
| fingerprint re-check | DumpTree(no props) + DumpCompose(sem) | always | `meta.fingerprint` |

Request order: [slots enable] → GetWindows → DumpTree → Screenshot(other roots) → DumpCompose(sem) → [slots if_available] → DumpA11y → [SKP] → re-check.

Consistency:
- The fingerprint is blake2b-128 over View tuples `(id, class, bounds, text)`, semantics tuples `(acv, id, bounds, Text, ContentDescription, StateDescription, ToggleableState, Selected, Disabled, EditableText)` and the window root ids. It excludes `Focused` and pixels, so cursors, ripples and spinners don't make a capture unstable.
- If the re-check fingerprint differs from the first one, the capture retries up to 2 times, 150 ms apart; after that it is marked `consistency:"unsettled"` with a warning.
- `settle_ms` polls the fingerprint until two consecutive values match (cap 3,000 ms) before fetching.
- `if_changed_since=C` computes only the fingerprint (about 30-150 ms). If it equals C's, the tool returns `{"capture":"C","unchanged":true,"age_s":…}` and writes nothing.

### 3.3 Unified node (UNode)
There is one record per node; it is persisted as one line of `index.jsonl.gz`.

| Field | Meaning |
|---|---|
| `ref` | `"n23"`, store-global, carried, never reused (§3.9) |
| `key` | canonical key (§3.4) |
| `kind` | `view` \| `compose` (semantics node) \| `slot` (composable group) \| `a11y` (virtual node with no View/Compose twin, e.g. WebView) |
| `window` | ref of the window's root View. A window root is a View node flagged `window:true` with a `z` index. |
| `parent`, `children`, `depth` | the canonical **ui** tree; other trees are in `Index.trees` (§3.5) |
| `type` | Display type, resolved in this order: a11y role (Button, Switch, Checkbox, Image, Tab…) → linked app composable name (ListItem, Text) → View simple class → a11y class simple name, except the generic `View` → **omitted**. Types are never invented; the `flags` carry behaviour. |
| `ids` | device identities: `{view:udid, sem:"acv:id", a11y:"host:virt", layer:render_node_id, slot:"File.kt:line#k"}` |
| `rid`, `tag` | resource-id entry name (View id or a11y viewIdResourceName) and Compose testTag |
| `label` | what TalkBack speaks: a11y speakable (contentDescription > text > stateDescription; a focusable node's label is built from its non-focusable descendants per RO1) → Compose Text/ContentDescription → View text |
| `text`, `desc`, `state`, `hint`, `role` | raw strings, for find |
| `b` | **visible** bounds `[x,y,w,h]` in screen px (semantics and a11y bounds are already clipped) |
| `declared_b` | declared bounds (View layout rect, or the slot-linked composable box) when known |
| `visible` | the visible fraction of `declared_b`, when known |
| `flags` | fixed vocabulary: `click longclick focus focused scroll checkable checked partial selected disabled heading edit password hidden live tgroup webview interop` |
| `stop` | TalkBack stop index or null |
| `src` | `File.kt:line` (slots, and semantics nodes linked to a slot) |
| `origin` | slots only: `app` or `library` |
| `facets` | `view{class, qualified, layout_res}` (props decoded lazily from `views.pb` by view id); `compose{attrs (normalized), actions, slots:[refs]}`; `a11y{speakable, class, role, flags, actions (names), state, collection, range, labeled_by→ref, traversal→ref}`; `slot{name, params (raw strings kept), mods}` |
| `conf` | per facet: `exact` \| `inferred` \| `none` |
| `issues` | `[{id, sev, evidence, conf}]`; messages live once in the rule catalog |
| `match`, `since`, `rebound_of?` | carry-over provenance (§3.9) |
| `anchor` | semantic path signature (§3.4) |
| `sel` | shortest unique locator in this capture (§3.4) |
| reserved | `render{}` (RENDER_DESIGN facet), `fragment`, `adapter_pos` |

### 3.4 Keys, refs, locators and anchors

- **Canonical keys** are internal, derived from device identity, and also accepted as selectors:
  - `view:<udid>`;
  - `sem:<acvUdid>:<semanticsId>` (composite, which fixes ID3 on the host);
  - `slot:<acvUdid>:<blake2s32(anchor)>` (slots arrive with id 0);
  - `a11y:<hostUdid>:<virtualId>`; when the ID1 detector fires, `a11y:path:<rootUdid>:<childIndexPath>`;
  - `w:<rootUdid>` is an alias of the window's root View.
  - Legacy input `compose:<id>` resolves only when it is unique across ComposeViews; otherwise the error lists the candidates.
- **Refs** are `n<int>` from a store-global monotonic counter (`store.json.next_ref`, under the refs lock).
  - A ref belongs to exactly one lineage forever.
  - In pre-order, a first capture reads `n1, n2, n3…` (or continues from the global counter).
  - The counter resets only on `captures gc --all`, which is a documented full wipe.
- **`sel`** is the shortest locator that is unique in the capture, tried in this order: `#rid` → `@tag` → `Type"label"` → `"label"` → `<parent sel> > Type"label"` → the ref. It is always emitted in a form the selector grammar (§6.1) accepts, so agents can paste it into notes, tests or a source search.
- **Anchor** is a semantic path from the window: `w<z>/` followed by segments.
  - View: `Class#rid`, or `Class:ordinal` among same-class siblings.
  - Semantics: `@tag`, else `Role"label≤24"`, else `:ordinal`.
  - Slot: `Name@File.kt:line[key]:ordinal`.
  - Children of a collection (a11y CollectionInfo, RecyclerView, Lazy list) get `[i]`. The **template anchor** replaces `[i]` with `[*]`.

### 3.5 Trees (views over the same node set)
`Index.trees` holds `{ui, views, compose, slots, a11y}`, each as `{roots, children}`, plus `Index.reading` (a list of refs).

- **ui** (default):
  - View spine, one window root per GetWindows root, ordered by z (RootsDetector order). Dialogs and popups are separate window roots.
  - Each AndroidComposeView's semantics subtree is grafted under it. The synthetic window root that reuses the ACV id (the ID3 collision) is folded into the ACV node.
  - The a11y data attaches as a **facet**. Unmatched virtual nodes become `kind=a11y` children of their host View.
  - AndroidView-inside-Compose: the View subtree under AndroidViewsHandler is re-parented under the smallest containing semantics node in the same ACV (`interop` flag, `conf` inferred; exact in phase 3 via LayoutNode ids).
  - RecyclerView cells that hold ComposeViews each graft independently, and composite keys keep them distinct. This requires ID2 (recursion into nested ComposeViews), which is on the improve/a11y-agent-identity branch.
- **views**: the raw View hierarchy (replaces `dump_tree`).
- **compose**: semantics per ACV.
- **slots**: the composable call tree. Each semantics node links to the slot groups that emitted it. The link is exact once the agent emits LayoutNode ids (R3); until then it uses bounds equality plus text/testTag plus nesting (`conf` inferred). That link is how `src`, `maxLines`, `overflow` and font sizes reach semantics nodes (CO5).
- **a11y**: the AccessibilityNodeInfo tree. Matched nodes appear under their refs.
- **reading**: TalkBack stops in order.

### 3.6 Correlation (in the index, not in correlate.py)
- Coordinates are normalized to **screen px**. When a semantics root lies outside its ACV's screen bounds (the dialog offset, CO4), the whole ACV subtree is shifted by the ACV's screen offset and `diagnostics` records it.
- a11y ↔ View: `host_view_id == view udid` with `virtual_id == -1`.
- a11y ↔ semantics: `(host_view_id == acv udid, virtual_id == semanticsId)`. Both joins are exact.
- **ID1 detector**: if the a11y tree has duplicate `(host_view_id, virtual_id)` pairs (live today, pre-fix, 40 nodes share 2 pairs), id joins are disabled. a11y facets are then assigned by one-to-one IoU ≥ 0.6 within the same window, with ties broken by class and label compatibility and then depth (CO3). Those facets get `conf:"inferred"`, and the capture adds the diagnostic `"a11y ids not unique (agent ID1); a11y facets matched by bounds"`.

### 3.7 Value normalization (`normalize.py`, shared by Phase 0 and the index)
- Drop `"null"`, empty strings, `Modifier`, `TextUnit.Unspecified` (2143289344), `Color.Unspecified` (16), `NaN`, `tmpN_rcvr` params, `ComposableLambdaImpl@…` and `Function…` values (kept as `λ` only for `on*` and `content` slot names).
- `pkg.Class@hash` becomes `Class`. `maxLines 2147483647` becomes `inf`. Int enums are decoded (e.g. overflow `1` → `Clip`).
- `TextStyle(…)` keeps only fields that differ from default: `16sp/24sp w400 ls0.5sp #1D1B20`. A packed Color ULong becomes `#AARRGGBB`.
- A modifier chain becomes element names plus key args.
- Every value is capped at 120 chars with `…(+N)`. `params="raw"` bypasses all of this.
- **Library classifier**: the source file is not in a shipped `LIBRARY_FILES` set (generated from the Compose/Material/Foundation jars) and the name starts uppercase → `origin=app`. The agent's `package_hash` (R3) replaces this heuristic.
- **Properties**:
  - `nondefault` means the value differs from `normalize_defaults.STATIC_VIEW_DEFAULTS` (per class family, e.g. pivot = centre, `maxWidth`/`maxHeight` = MAX, scrollbar and haptics defaults) and from the per-capture class majority when the class has ≥3 instances.
  - `key` is a curated list per family: visibility, alpha, enabled, clickable, padding, margins, min sizes, layout_*, text, textSize, textColor, maxLines, ellipsize, hint, scaleType, checked, background.

### 3.8 Analyzers (`capture/analyzers.py`)
Analyzers are the single extension point for roadmap items 1 and 3. They write `node.issues` keyed by ref.

- **Render signals now**:
  - `render.zero_size`;
  - `render.offscreen` (outside its window or the screen);
  - `render.hidden` (props `visibility != visible` or `alpha == 0`, or a11y not `visible_to_user`);
  - `render.clipped`: exact when `declared_b` is known and `visible < 1`. It is inferred when a node's visible rect touches the viewport edge of a scrollable ancestor and is under 50% of the median height of its same-type siblings (the launcher's 27-of-216 px row).
  - Evidence includes `clipped_by`.
- **Render signals later**: `render.text_overflow` (R1), `render.covered` and `render.drawn_mismatch` (SKP/DumpRender, R3).
- **Lint adapter**: runs the unified lint, `a11y_lint.run_lint`, over the stored trees exactly as the live `a11y_lint` tool does: the unified a11y tree (`a11y.a11y_to_dict(a11y.pb)`: Views and Compose in one pass, so View screens are linted too) with the Compose semantics (`compose_sem.pb`) joined for detail. Each finding maps to its a11y node by `(host, virtual)` and from there to a ref, as a TalkBack stop does.
  - Touch-target findings on nodes with `render.clipped` at a scroll edge are annotated "likely false positive" (L2).
  - Contrast (~4 s) runs only when `lint="full"` or `lint(contrast=true)`. It reads the stored per-window shot and the result is cached.
  - A capture defaults to `a11y_rendering=false`, so the text-size rules that need ExtraRenderingInfo (R11, R18) can report less than the live `a11y_lint` (rendering info on by default); `capture(a11y_rendering=true)` matches it.
- **Reading order**: `talkback.reading_order` (the TalkBack model; it returns `a11y.reading_order`'s shape) over the whole a11y dump, mapped to refs; sets `stop` and `Index.reading`.
- **Rule catalog** (`capture/rules.py`): each rule has `id`, `short` (`a11y.<group>.<x>` → `group`, `render.<x>` → `x`), `sev`, `msg` and `fix`, plus aliases `R1..R12`. Unknown ids are rejected (E10).
- **Template collapse**: findings are grouped by `(rule, src or template anchor)`, e.g. `a11y.label.missing ×8 in #feed cells (FeedRow.kt:42 IconButton), e.g. n118 n125 n132 +5`.

### 3.9 Ref carry-over (`capture/refs.py`)
When a capture is taken, its index is matched against the latest capture of the same lineage, under the store refs lock.

1. **Device identity** (only when the pid and `compose_generation` are both the same): the canonical key is equal. **Collection guard**: inside a collection item, the label or the anchor without its ordinal must also agree. Otherwise the node gets a new ref and `rebound_of` is recorded (a recycled cell).
2. **Unique locators** present in both captures: `(window z, #rid)`, `@tag`, or a11y uniqueId, each unique on both sides.
3. **Structure**: the parent is matched, and the type, label and ordinal among same-type siblings are all the same.
4. **Geometry**: same type, IoU ≥ 0.8, one-to-one greedy within a matched parent or window.

Ambiguous candidates get **new refs** and are never guessed. Old refs that go unmatched become tombstones in the lineage file (`{ref: [type, label≤40, sel, last_capture]}`, capped at 5,000, LRU). A stale ref fails with `ref_not_in_capture` plus its last-seen info. `match` (`id | locator | structure | geometry | new`) and `since` are recorded per node.

### 3.10 Sizes
- A small screen is about 0.4-0.9 MB per capture; screenshots dominate (a deflated ABGR screenshot pb is ~0.2-0.6 MB).
- The 259-view fixture with props is about 1-3 MB.
- `index.jsonl.gz` is about 10-40 KB.
- In memory (MCP), a hydrated index is about 1-2 KB per node; 5,000 nodes is about 10 MB.

---

## 4. Store (`capture/store.py`)

### 4.1 Location and layout
The root is chosen in this order: `$INSPECTOR_WIDGET_CAPTURE_DIR`, then `$XDG_CACHE_HOME/inspector-widget`, then the platform cache (macOS `~/Library/Caches/inspector-widget`, Linux `~/.cache/inspector-widget`). Directories are mode 0700. The store is local-only.

```
store.json                      {schema:1, next_ref:N}
store.lock                      flock: ref allocation + capture publish + label moves (held ~10-50 ms, never across device I/O)
gc.lock
session.json                    default session {serial, package, at} (last attach/capture, either surface)
lineages/<serial>__<package>.json  {latest, history:[ids newest first ≤50], labels:{name:id}, tomb:{…}}
captures/<id>/  meta.json refmap.json raw/ shot/ index.jsonl.gz derived/ img/ out/ .used .complete
.staging/<id>.<pid>/            in-flight captures
.trash/                         deletions in progress
spill/                          Phase-0 spill files (1 h TTL)
```

### 4.2 Ids, labels and resolution (one resolver for both surfaces)
- **Ids**: `c` + 5 Crockford-base32 chars from `os.urandom`, created with an exclusive `mkdir` in `.staging`, then `os.rename` into `captures/`. On a collision, retry with a new id. Ids are matched case-insensitively.
- **Labels** match `^[a-z][a-z0-9_-]{0,31}$` and must not match the id pattern. They are unique **per lineage**. Relabelling moves the label and the response reports `moved_from`.
- **The `capture` argument** accepts: an id, `label` or `@label`, `latest` (the default), `prev`, or `latest~N`.
  - `latest`, `prev` and `latest~N` resolve within the **resolved session's lineage** (§5.1), else across the whole store (newest `created_at`).
  - A label resolves in the resolved lineage first, else it must be unique across the store; otherwise the error lists candidates.
- `latest` means the same thing in the CLI and the MCP because both call `store.resolve()`.

### 4.3 Retention
- TTL is 24 h since last use (`INSPECTOR_WIDGET_CAPTURE_TTL`, e.g. `6h`). `.used` mtime is touched at most once a minute, so `meta.json` is never rewritten just to record a read.
- Caps: 200 captures total (`INSPECTOR_WIDGET_CAPTURE_MAX`), 1 GiB (`INSPECTOR_WIDGET_CAPTURE_MAX_MB`), and 50 unpinned per lineage (unlabeled captures are evicted first).
- Pinned captures (at most 20) are exempt from TTL and caps, but not from an explicit drop.
- Eviction order: expired → over per-lineage count (LRU) → over size. For size, heavy files (`img/`, `out/`, `derived/`, `skp`) are stripped from LRU captures first, and whole captures are deleted after that.
- GC runs on every capture publish, at MCP startup, and on `captures gc`, under a non-blocking `gc.lock`. It purges `.staging` directories older than 1 h and empties `.trash`.
- Lineage files are tiny and are never TTL-collected, which is what guarantees refs are never reused. `captures gc --all` wipes everything, including the ref counter, and says so.
- **Memory-only mode**: `INSPECTOR_WIDGET_CAPTURE_PERSIST=0` (MCP) roots the store in a per-process temp dir that is deleted at exit. The CLI cannot see those captures, and the capture response says so.

### 4.4 Memory (MCP)
An LRU keeps up to 8 hydrated indexes (≤256 MB estimated). Eviction drops only the in-memory form; re-hydrating takes about 10-50 ms. Screenshots are decoded per request only.

### 4.5 Concurrency
- A capture is built in `.staging`, fsynced, has `.complete` written last, and is published by rename. Readers ignore directories without `.complete`.
- Facet files are immutable once published. Derived artifacts (crops, overlays, lint caches, exports) are written temp-then-replace under a per-capture lock and keyed by a parameter hash, so their inputs are always frozen.
- Deletion renames to `.trash/` and then removes the tree. A reader that races it gets `capture_not_found`.
- MCP device I/O is serialized per session by the existing Client lock (plus session-lifecycle's H1 deadlines). Captures of the same lineage serialize on the refs lock only during matching and publish.
- The CLI and the MCP server, including several MCP servers, can create and read concurrently. The only shared mutable state is `store.json`, the lineage files and `session.json`, all written under `store.lock`.

### 4.6 CLI access
- Every query subcommand takes `-c/--capture` (id | label | latest | prev | latest~N).
- Cursors are stateless, so a cursor from one invocation works in the next.
- Scripting example: `C=$(inspector-widget capture -q)` then `inspector-widget find -c $C --flags click --json`.
- `captures export` returns paths (`nodes.jsonl`, legacy JSON, raw pbs), never contents.

### 4.7 Cleanup and privacy
- Every PNG lives under its capture's `img/`, which closes E12 (`$TMPDIR` litter and `OUT.png.base.png`).
- MCP `atexit` closes sessions only; it deletes nothing that is persisted.
- Captures contain on-screen text and screenshots. Mitigations: 0700 permissions, the 24 h TTL, `captures gc --all`, and `PERSIST=0`. Password text redaction is agent-side (backlog D8); `redact=true` is reserved.

---

## 5. Tool surface

### 5.1 Conventions (all new tools, both surfaces)

- **Envelope**: compact JSON. List data goes in `lines` (strings in the line grammar). `format:"json"` returns `rows` (objects with the same projected fields).
- Errors are `{"error":{"code","message","hint","candidates"?}}` with `isError:true`; the CLI exits 1 and prints the same JSON to stderr.
- Every response echoes `capture`. It adds `age_s` when older than 120 s, `stale:"c9q4tz is newer (40s)"` when a newer capture exists in the lineage, and `pid_changed:true` when the live session's pid differs.
- **Line grammar v1**, documented once in the MCP instructions and the `outline` description:
  - `line := indent seg (" > " seg)* [" [x,y wxh]"] (" !"issue)* [" +"N] [tail]`
  - `seg := ref [" "Type] [" #"rid] [" @"tag] [" \""label"\""] (" "flag)*`
  - Indent is 2 spaces per depth. `Type` starts with an uppercase letter; flags are lowercase words from the vocabulary in §3.3.
  - Labels are cut at 48 chars with `…`; quotes and newlines are escaped.
  - Bounds are the **visible** rect in screen px, which matches the screenshot pixels.
  - `!issue` uses the rule's short code (`!role !label !touch_target !contrast !state !clipped !hidden …`).
  - `+N` means N descendants are hidden (by depth, collapse or budget).
  - `tail` holds projections (`src=MainActivity.kt:151`, `textSize=14sp`) and, in `find`, the breadcrumb ` in n10 @launcher_list < n5 ComposeView`.
  - Reading view lines are prefixed `<stop>. `. Diff lines are prefixed `~` (changed), `+`, `-`, or `>` (moved).
- **Session defaulting** (`serial` and `package` are optional on every new tool). They resolve in this order:
  1. explicit arguments;
  2. the capture argument's lineage;
  3. `session.json`, the last attach or capture from either surface;
  4. the single running debuggable app on the single device (honouring `ANDROID_SERIAL`).
  Otherwise the tool returns `no_session` with candidates.
- **Budgets**: every tool has a default `max_bytes` (§7). Truncation is always explicit: `"truncated":{"omitted":143,"why":"max_lines|max_bytes","cursor":"c7h2kq:o:3f9a12bc:80"}`. `spill:true` writes the complete result to `out/` and returns its path plus the first rows.
- **`next`**: at most 3 concrete follow-up calls, ≤200 B, chosen by deterministic rules (§6.6).
- **Error codes**:
  - `no_session`, `device_lost` (mapped from session-lifecycle's E7 errors), `agent_error`;
  - `capture_not_found` (expired, evicted or unknown; gives the TTL), `ref_not_in_capture` (last-seen info plus `sel`);
  - `not_found` (3 nearest labels), `ambiguous` (top 5 lines), `bad_selector` (column plus examples), `bad_args`;
  - `facet_unavailable` (the exact recapture call), `unsupported`.
- MCP annotations: `readOnlyHint:true` on `outline`, `find`, `node`, `image`, `lint`, `diff`, and on `captures` for list/show/export.

### 5.2 Session tools (shared by every toolset; today's shapes plus additive fields)
1. `list_devices()`: today's shape, plus `default_session`. CLI `devices` gains `--json`, api, abi and model.
2. `list_processes(serial?)`: today's shape. CLI `packages` gains pid, running and `--json`.
3. `attach(serial, package, force=false)`: today's fields, with real metadata from session-lifecycle's E9 and `force` from E4. It also sets the default session and adds `"next":["capture()"]`.
4. `detach(serial, package, shutdown=true)`: `shutdown=false` only disconnects this client (session-lifecycle's H5).

### 5.3 capture

`capture(serial?, package?, label?, props=true, resolution_stack=false, slots="if_available"|"enable"|"off", screenshot=true, screenshot_scale=1.0, skp=false, a11y_rendering=false, lint="tree"|"full"|"none", settle_ms=0, diff_from?, if_changed_since?, outline_lines=20, on_screen=true, pin=false, max_bytes=3000)`

Example on the real launcher (post-ID1 ids; refs illustrative), 1,380 B:
```json
{"capture":"c7h2kq","session":"emulator-5554/com.oberkfell.a11yprobe","pid":4312,
 "device":"API 37 1280x2856 480dpi font 1.0","took_ms":640,"consistency":"settled",
 "facets":{"views":8,"props":8,"compose":17,"a11y":40,"slots":"not populated","shots":1,"skp":"off"},
 "windows":["n1 DecorView [0,0 1280x2856] z0"],
 "lint":"14 warn: 12 role, 1 state, 1 touch_target (contrast not run)",
 "issues":"1 clipped: n22",
 "outline":["n1 DecorView [0,0 1280x2856]",
  "  n2 LinearLayout > n4 FrameLayout #content > n5 ComposeView > n6 AndroidComposeView [0,0 1280x2856]",
  "    n9 TextView \"A11yProbe\" [48,210 322x84]",
  "    n10 @launcher_list scroll [0,348 1280x2436] +12",
  "  n24 View #navigationBarBackground [0,2784 1280x72]",
  "  n25 View #statusBarBackground [0,0 1280x156] hidden"],
 "on_screen":["n11 \"▶ All scenarios (lint everything), every BAD/GO…\"","n12 \"Icon button label, MissingContentDescription\"","…10 more: outline(root=\"n10\")"],
 "next":["outline(root=\"n10\")","lint()","node(\"n22\")"]}
```

- `on_screen` lists labelled text that sits **under `+N`** in the preview, so it does not duplicate outline lines (≤20 lines).
- With `slots="enable"` the response says: `"warning":"slots=enable hot-reloaded every composition (resets remember{} state); semantics ids re-minted; refs carried by locator/structure"`.
- When the slot table is not populated: `"facets":{…,"slots":"not populated (slots=\"enable\" is destructive: resets remember{} state)"}`.
- With `diff_from`: adds `"diff":{summary, lines≤10, issues}`.
- With `if_changed_since` and an unchanged fingerprint: `{"capture":"c7h2kq","unchanged":true,"age_s":95}` (≈50 B).
- CLI: `capture [-s S] [-p P] [--label L] [--no-props] [--slots if_available|enable|off] [--no-screenshot] [--scale F] [--skp] [--lint tree|full|none] [--settle-ms N] [--diff-from C] [--if-changed-since C] [--pin] [-q] [--json]`. Human output prints the id on line 1, then the summary and outline.

### 5.4 captures

`captures(action="list"|"show"|"pin"|"unpin"|"label"|"drop"|"export"|"gc", id?, label?, what="nodes"|"views"|"compose"|"slots"|"a11y"|"props"|"lint"|"raw"|"all", format="jsonl"|"json"|"legacy"|"raw", all=false, limit=20, max_bytes=2000)`

`list` example, 310 B:
```json
{"lines":["c9q4tz emulator-5554/com.oberkfell.a11yprobe 40 nodes 0.8MB 12s ago",
 "c8m2pa @before emulator-5554/com.oberkfell.a11yprobe 40 nodes 0.8MB 15s ago pinned",
 "c7h2kq emulator-5554/com.oberkfell.a11yprobe 26 nodes 0.6MB 4m ago"],
 "store":"~/Library/Caches/inspector-widget 2.2MB ttl 24h"}
```

- `export` returns `{path, rows, bytes, hint}`, never contents. `format="legacy"` regenerates the old `dump_tree`/`dump_compose`/`dump_accessibility` JSON from the pbs; `format="raw"` copies the pbs (used for fixtures).
- CLI: `captures [ls|show C|pin C|unpin C|label C NAME|rm C|export C --what … --format …|gc [--all]]`.

### 5.5 outline

`outline(capture="latest", root?, view="ui"|"views"|"compose"|"slots"|"a11y"|"reading", depth=3, detail="semantic"|"all", origin="app"|"all" (slots), max_children=12, max_lines=80, fields?, cursor?, format="lines"|"json", max_bytes=6000)`

Example `outline(root="n10")` on the real launcher, ≈1.4 KB (13 of 13 lines):
```json
{"capture":"c7h2kq","view":"ui","root":"n10","shown":13,"total":13,"lines":[
 "n10 @launcher_list scroll [0,348 1280x2436]",
 "  n11 @launch_all \"▶ All scenarios (lint everything), every BAD/GO…\" click [0,348 1280x216] !role !state",
 "  n12 @launch_icon_button \"Icon button label, MissingContentDescription\" click [0,567 1280x216] !role",
 "  …(9 more lines of the same shape)…",
 "  n22 @launch_heading \"Section heading, MissingHeading\" click [0,2757 1280x27] !clipped !role !touch_target"]}
```

- `view="reading"` on the View screen gives lines such as `3. n13 ImageButton #badImageButton click !label`.
- `view="slots"` (app origin) looks like: `n301 ListItem [0,2757 1280x216] src=MainActivity.kt:150` / `  n302 Text "Section heading" [48,2799 365x72] src=MainActivity.kt:151`. The whole launcher is 56 lines, about 4.5 KB, versus today's 567 KB.
- CLI: `outline [-c C] [--root R] [--view …] [--depth N] [--detail …] [--origin …] [--max-children N] [--max-lines N] [--fields F] [--cursor K] [--format …] [--json]`.

### 5.6 find (structured filters; all ANDed)

`find(capture="latest", text?, text_re?, type?, rid?, tag?, src?, role?, flags?[all-of], any_flags?, has?[], missing?[], issue?, within?, at?[x,y], overlaps?[x,y,w,h], min_dp?, max_dp?, kind?, window?, in="ui"|"slots"|"all", sort="tree"|"reading"|"top"|"area", limit=20, fields?, cursor?, count_only=false, format="lines", max_bytes=3000)`

Example `find(text="state", flags=["click"])`, 360 B:
```json
{"capture":"c7h2kq","total":2,"lines":[
 "n15 @launch_toggle_state \"Switch state desc, MissingStateDescription\" click [0,1224 1280x216] !role in n10 @launcher_list",
 "n16 @launch_checkbox_state \"Checkbox state desc, MissingStateDescription\" click [0,1443 1280x216] !role in n10 @launcher_list"]}
```

- A single hit adds `"path":"n1 > n6 > n10 > n15"`. `count_only` returns `{"total":N}`.
- `find(flags=["click"], max_dp=47)` finds small touch targets. `find(type="Text", fields="+src,+params:maxLines,overflow,+visible")` is a one-call truncation audit.
- CLI: `find [-c C] [--text T] [--type T] [--rid R] [--tag T] [--src S] [--role R] [--flags a,b] [--any-flags …] [--has …] [--missing …] [--issue I] [--within SEL] [--at X,Y] [--overlaps X,Y,W,H] [--min-dp N] [--max-dp N] [--in …] [--sort …] [--limit N] [--fields F] [--count] [--json]`.

### 5.7 node

`node(ref | refs[≤10], capture="latest", facets="core,layout,a11y,compose,issues", props="none"|"key"|"nondefault"|"all"|[names/globs], params="brief"|"raw", ancestors=false, children=false, image=false, max_bytes=3000 (6000 for batches))`

Example `node("n22")` from a capture with slots populated, ≈1.2 KB:
```json
{"capture":"c7h2kq","ref":"n22","sel":"@launch_heading","label":"Section heading, MissingHeading",
 "b":[0,2757,1280,27],"dp":[0,919,426.7,9],"tap_xy":[640,2770],
 "layout":{"declared":[0,2757,1280,216],"visible":0.125,"clipped_by":"n10 @launcher_list (bottom 2784)"},
 "ids":{"sem":"82:448","a11y":"82:448"},"conf":{"a11y":"exact","slot":"inferred"},
 "a11y":{"speakable":"Section heading, MissingHeading","role":null,"stop":13,"flags":["click","focus"],"actions":["CLICK"]},
 "compose":{"sem":{"TestTag":"launch_heading","Text":"Section heading, MissingHeading","IsTraversalGroup":"true"},"actions":["OnClick","RequestFocus","GetTextLayoutResult"],
  "slots":["n301 ListItem MainActivity.kt:150 modifier=clickable,testTag","n302 Text MainActivity.kt:151 \"Section heading\" 16sp/24sp overflow=Clip","n305 Text MainActivity.kt:152 \"MissingHeading\" 14sp/20sp"]},
 "issues":["render.clipped: 27 of 216px visible at the scroll edge of n10","a11y.touch_target.small warn: 9dp tall (likely false positive: clipped at scroll edge)","a11y.role.missing_on_clickable warn"],
 "parent":"n10 @launcher_list","children":0,"next":["image(ref=\"n22\")","find(within=\"n10\",flags=[\"click\"])"]}
```

- Without slots: `"slots":"not captured: capture(slots=\"enable\") recomposes once and resets remember{} state"`, `layout.declared` is absent, and `render.clipped` is inferred.
- A View with non-default props: `node("#badSwitch", props="nondefault")` returns `"props":{"mode":"nondefault","n":14,"of":142,"values":{"text":"Notifications","textSize":"42px (14sp)","textColor":"#FF1C1B20","minHeight":144,…}}`, about 0.9 KB in total (today `get_properties` is 8.9 KB).
- Facets are added in priority order core > issues > a11y > layout > compose > text > props > children. Anything that doesn't fit is listed in `"omitted":["props(nondefault,23): node(\"n63\",props=\"nondefault\")"]`.
- `tap_xy` is the centre of the **visible** part of the node, for `adb shell input tap`.
- CLI: `node REF… [-c C] [--facets …] [--props …] [--params raw] [--ancestors] [--children] [--image] [--json]`.

### 5.8 image

`image(capture="latest", ref?, window?, overlay="none"|"marks"|"lint"|"reading"|"bounds"|"compose", marks="auto"|"all"|[refs], pad=16, source="auto"|"screenshot"|"skp", max_side=1024, inline=false, max_bytes=600)`

Example, 230 B:
```json
{"capture":"c7h2kq","ref":"n22","kind":"crop","path":"~/Library/Caches/inspector-widget/captures/c7h2kq/img/n22-p16.png","px":[1312,59],"window":"n1","from":"screenshot","note":"visible part only (clipped by n10)"}
```

- Crops and overlays are always cut from the capture's own per-window screenshot, so the pixels match the tree.
- `marks="auto"` draws Set-of-Mark boxes labelled with **refs** for clickable, issue and stop nodes (≤60 marks).
- `lint` and `reading` overlays colour and number nodes by ref-keyed issues and stops, which fixes OV1 by construction.
- `inline=true` adds an MCP `ImageContent`, downscaled so the long side is ≤ `max_side`. It costs about w·h/750 tokens: a full screen at 1024 px is ≈630 tokens, a button crop ≈75.
- `source="skp"` needs `capture(skp=true)`. It reports `unsupported` on API 37 and falls back to the screenshot when `source="auto"`.
- CLI: `image [REF] [-c C] [--window W] [--overlay …] [--marks …] [--pad N] [--max-side N] [--out F.png]`. The CLI prints the path; `inline` is an allow-listed transport-only difference.

### 5.9 lint

`lint(capture="latest", rules?[ids|R1..R12], severity="info", within?, contrast=false, wcag=false, group="rule"|"node"|"none", per_rule=3, limit=30, cursor?, max_bytes=4000)`

Example on the real launcher, ≈1.0 KB (today 11,754 B):
```json
{"capture":"c7h2kq","counts":{"error":0,"warn":14,"info":0},"contrast":"not run (lint(contrast=true) ~4s)",
 "rules":[{"rule":"a11y.role.missing_on_clickable","sev":"warn","n":12,"msg":"Clickable node has no Role, so TalkBack cannot announce what it does.","fix":"Modifier.semantics { role = Role.Button }, or Button/ListItem(onClick)","nodes":["n11 \"▶ All scenarios (lint everything)…\"","n12 \"Icon button label, MissingCon…\"","n13 \"Touch target size, Accessibil…\"","+9 more: lint(rules=[\"R5\"],group=\"node\")"]},
  {"rule":"a11y.state.not_exposed","sev":"warn","n":1,"msg":"Looks stateful but exposes no state.","fix":"Modifier.toggleable(…) or stateDescription","nodes":["n11 \"▶ All scenarios (lint everything)…\""]},
  {"rule":"a11y.touch_target.small","sev":"warn","n":1,"msg":"Touch target under 48dp.","nodes":["n22 \"Section heading, MissingHeading\" 426.7x9dp (likely false positive: clipped at scroll edge)"]}],
 "next":["image(overlay=\"lint\")","node(\"n22\")"]}
```

- Template collapse for collections, e.g. `"nodes":["×8 in #feed cells (FeedRow.kt:42 IconButton): n118 n125 n132 +5"]`.
- CLI: `lint [-c C] [--rule R]… [--severity S] [--within SEL] [--contrast] [--wcag] [--group …] [--json]`.

### 5.10 diff

`diff(a="prev", b="latest", within?, include=["text","state","bounds","visibility","a11y","issues"] (+"props","params","pixels"), min_move_px=4, limit=40, image=false, max_bytes=4000)`

- Nodes are compared **by ref**; carry-over already did the hard matching.
- Change classes: changed, added, removed, moved, and rebound (a `rebound_of` pair).
- When fewer than 40% of refs are shared, it returns `"verdict":"new screen"` plus b's outline preview instead of hundreds of +/- lines.
- Captures from different lineages give `bad_args`.

Example (tap on `#badSwitch`, View screen), 390 B:
```json
{"a":"c8m2pa @before","b":"c9q4tz","dt_s":2.4,"same_pid":true,"summary":{"changed":1,"added":0,"removed":0,"moved":0,"unchanged":39},
 "lines":["~ n63 Switch #badSwitch \"Notifications\": unchecked -> checked","~ n63 props checked false -> true"],
 "issues":{"resolved":[],"new":[]},"next":["image(ref=\"n63\",capture=\"c9q4tz\")"]}
```

- `image=true` writes a side-by-side PNG with the changed boxes and a pixel-delta mask, and returns its path.
- CLI: `diff [A] [B] [--within SEL] [--include …] [--min-move-px N] [--image] [--json]`.

### 5.11 Phase 3 (optional): act
`act(action="tap"|"long_press"|"type"|"swipe"|"scroll"|"back"|"key", target=ref|selector|"x,y", text?, then="diff"|"capture"|"none", settle_ms=800)`

- Resolves the target to its visible centre from the latest capture. It refuses when `visible=0` and hints `act("scroll", "n10")`.
- Runs `adb shell input …`, settles, captures with carry-over, and returns the diff envelope (≈0.5-2 KB).
- CLI: `act tap n15`.

### 5.12 Toolsets and legacy
- `INSPECTOR_WIDGET_TOOLSET` values:
  - `legacy`: today's 15 tools. **Default until S4.**
  - `capture`: 4 session tools + `capture captures outline find node image lint diff` = 12. **Default after S4.**
  - `all`: 23.
- Hidden tools remain callable by name through `tools/call`.
- Legacy tools stay budgeted (Phase 0). From S3 on they are capture-backed and return a `capture` id.
- The CLI keeps every old subcommand indefinitely (they cost nothing in a tool list). After S4 each prints one stderr pointer line to its new equivalent.

### 5.13 MCP `instructions` (sent in `initialize`, ≤900 B; also in the fallback server)
> Inspector Widget reads the live UI of a debuggable Android app. Workflow: capture() takes one snapshot (views + properties, Compose, accessibility, screenshots, lint) and returns an id like c7h2kq with a short outline. Then query that snapshot with outline, find, node, image, lint and diff; they never touch the device. Every node has a short ref (n23) that stays the same across later captures of the same app; a ref that is gone returns an error instead of pointing elsewhere. Outline lines read: ref Type #resourceId @testTag "label" flags [x,y wxh] !issue +N (N hidden descendants); coordinates are screen pixels. After the UI changes, capture again (capture(diff_from="prev") also reports what changed). serial and package are optional once a session exists.

---

## 6. Query semantics

### 6.1 Node selectors (used by `node`, `image`, `find(within)`, `lint(within)`, `diff(within)`, `act`)

```
selector := ref | key | point | path
ref      := "n" digits
key      := view:<udid> | sem:<acv>:<id> | slot:<acv>:<hash> | a11y:<host>:<virt> | w:<rootUdid> | compose:<id> (legacy)
point    := <x>,<y>                    deepest visible node covering the point, top window first
path     := atom (" > " atom)*         each atom is a direct child of the previous; resolves the last
atom     := "#"rid | "@"tag | Type | Type"\"label\"" | "\"label\""   (exact label, case-sensitive; add "i" after the closing quote for case-insensitive)
```

- No other whitespace is allowed. A parse error returns `bad_selector` with the column and three examples (`n23`, `@launch_heading`, `#feed > "Item 3"`).
- 0 matches → `not_found` with the 3 nearest labels (fuzzy). More than 1 → `ambiguous` with the top 5 lines, each with its `sel`.

### 6.2 find filters
- `text`: case-insensitive substring over label, text, desc, state, hint and the slot `text` param. `text_re`: Python regex.
- `type`: a glob against display type, View simple or qualified class, composable name, a11y role, and a11y class simple name.
- `rid`, `tag`, `src`: globs.
- `flags`: all of; `any_flags`: any of.
- `has` / `missing`: from `label role state stop slots props issues a11y compose view`.
- `issue`: an id prefix or short code (`render.clipped`, `clipped`, `a11y.`, or a severity such as `error`).
- `at` and `overlaps` replace the old bounds selector.
- `min_dp` / `max_dp`: on min(w, h) in dp, using the capture's dpi, of the node's touch (a11y) bounds when it has them, else its bounds: a Compose control's minimumInteractiveComponentSize area counts, as it does for a finger. The touch-target lint (R2) is stricter: it also flags a clickable whose own layout is under 48dp.
- Domain: `in="ui"` covers view, compose and a11y nodes. `slots` or `all` add slot nodes; using `src` implies `all`.
- Evaluation is a linear scan over the immutable index, with O(1) maps for rid, tag, type and label. Measured scale is ≤5k nodes, which takes <20 ms.
- Each hit carries a breadcrumb of its 2 nearest meaningful ancestors (those with a rid, tag, label or collection).

### 6.3 Outline disclosure
The levels are: capture preview (depth 2, ≤20 lines) → `outline(root, depth)` → `node(ref)` → `node(ref, props="all", params="raw", facets="all")` → `captures export` (everything, as files).

With `detail="semantic"`, a node is **shown** when any of these hold:
- it is a window;
- it has a label, rid or tag;
- it is actionable (click, longclick, edit, checkable, scroll);
- it is a stop;
- it has issues;
- it is a non-zero-area leaf;
- it has ≥2 shown children.

Other nodes collapse, and their children are hoisted. A single-child chain with identical bounds renders as one line with every member's ref (`n2 LinearLayout > n4 FrameLayout #content > …`). Zero-size nodes and ViewStubs are hidden and counted in `hidden`. `detail="all"` shows everything; `view="views"` implies all.

On real data: the launcher's 26 nodes show as ≈15 lines, and the View screen's 40 nodes as 37 lines (≈2.6 KB).

### 6.4 Field projection
- The default line fields are `ref,type,rid,tag,label,flags,bounds,issues`.
- `+src`, `+dp`, `+ids`, `+conf`, `+sel`, `+visible`, `+props:a,b`, `+params:a,b` append to the tail. `-bounds` and similar remove a field.
- The same projection applies to `format:"json"` rows.

### 6.5 Cursors and budgets
- Cursors look like `<capture>:<toolLetter>:<hash8 of normalized args>:<offset>`. They are stateless and valid while the capture exists.
- Using a cursor with different args returns `bad_args`. An evicted capture returns `capture_not_found` with a recapture hint.
- Rendering adds whole lines while tracking UTF-8 bytes, and stops when `bytes + 200` (the footer reserve) would exceed `max_bytes`. It then adds `truncated{omitted, why, cursor}` and a `next` entry that continues or narrows.
- Hard caps: `find.limit` ≤ 200, `outline.max_lines` ≤ 400, `max_bytes` ≤ 32,000 unless `spill:true`.

### 6.6 `next` rules (≤3, deterministic)
- truncated → the cursor, or expanding the root;
- lint > 0 and not yet shown → `lint()`;
- render issues → `find(issue="render.")`;
- a compose node without slots → a `capture(slots="enable")` warning;
- a node with an issue or `visible < 1` → `image(ref)`;
- a diff with verdict "new screen" → `outline()`;
- an ambiguous selector → a disambiguating `sel`.

---

## 7. Response budget rules

| Tool | Default `max_bytes` | Typical (real launcher) | Notes |
|---|---|---|---|
| capture | 3,000 | 1.3-2.2 KB | preview ≤20 lines, on_screen ≤20 lines |
| outline | 6,000 | 1.4-2.5 KB | about 75-95 B per line; ≤80 lines per page |
| find | 3,000 | 0.3-0.6 KB | about 110 B per hit with breadcrumb; limit 20 |
| node | 3,000 (6,000 batch) | 0.8-1.5 KB | facet priority truncation; values ≤120 chars |
| image | 600 (plus the image) | 0.2 KB | inline images cost about w·h/750 tokens |
| lint | 4,000 | ≈1.0 KB | grouped; message once; 3 examples per rule |
| diff | 4,000 | 0.4-1.2 KB | ≤40 lines; "new screen" verdict |
| captures | 2,000 | 0.3 KB | |
| session tools | 2,000 | 0.1-0.4 KB | |
| legacy tools | 32,000 (`INSPECTOR_WIDGET_MAX_BYTES`) | §2.6 | spill envelope ≤3,000 |

- The ceiling is 32,000 B (≈9k tokens, under Claude Code's 10k-token warning); legacy tools accept up to 200,000 on explicit request.
- Every response states what was cut and how to get it.
- `tools/list` for the `capture` toolset is ≤12,000 B compact; the instructions are separate and ≤900 B.

---

## 8. Agent workflows with token math
Token estimate: about 3.5 B per token for compact JSON, and about 4 B per token for today's pretty JSON. "Today" figures are measured payloads from `scratchpad/live`.

**W1. "Why is the 'Section heading' row cut off?" (Compose)**
- New:
  1. `capture()`: 1.4 KB. The summary already says `issues: 1 clipped: n22`.
  2. `node("n22")`: 1.2 KB. Declared 216 px vs visible 27 px, `clipped_by` n10; the touch-target finding is marked a likely false positive.
  3. `image(ref="n22", inline=true, pad=48)`: 0.2 KB plus ≈130 image tokens.
  - Total ≈ 1.1k tokens.
  - If `file:line` and params are needed: `capture(slots="enable")` (warns once), then `node("n22")` again, for ≈ 2k in total.
- Today: `inspect` (80 KB ≈ 20k) has no clip or file:line data. `dump_compose` (567 KB ≈ 140k) exceeds the cap, and its slots are unlinked (CO5). That is ≥160k tokens and not feasible.

**W2. "Is the checkout button accessible?"**
- New: `capture()` 1.4 KB → `find(text="checkout", flags=["click"])` 0.3 KB → `node("n31", facets="core,a11y,issues")` 0.8 KB, optionally `lint(within="n31", contrast=true)`. Total ≈ 0.7-1.1k tokens.
- Today: `dump_accessibility` 92 KB + `a11y_lint` 11.8 KB + `inspect_node` 4-12 KB ≈ 27k tokens, with wrong ids pre-ID1.

**W3. "What changed after I tapped X?"**
- New:
  1. `capture(label="before")` 1.4 KB.
  2. `node("#badSwitch")` 0.9 KB gives `tap_xy`.
  3. Bash `adb shell input tap 242 1254`.
  4. `capture(diff_from="before", settle_ms=800)` 2.0 KB: summary plus diff lines, and n63 is still n63.
  - Total ≈ 1.2k tokens. With phase-3 `act("tap","#badSwitch")` it is 2 calls.
- Today: `inspect` twice (≈40k) plus a manual diff over churned ids, and it misses property changes.

**W4. "Audit this screen"**
- New:
  1. `capture(lint="full")` 1.5 KB.
  2. `lint()` 1.0 KB.
  3. `outline(view="reading")` 1.5 KB.
  4. `image(overlay="lint", inline=true)` 0.2 KB plus ≈630 image tokens.
  5. `node` × 2: 2.4 KB.
  - Total ≈ 2.8k tokens.
- Today: `a11y_lint` 11.8 KB + `a11y_overlay` (colours never applied, OV1) + `dump_accessibility` 92 KB for reading order (with the broken order numbers of RO1) + `inspect_node` × 2 ≈ 30k tokens.

**W5. Fix and verify (a coding agent editing the app)**
- New: `capture(label="baseline")` + `lint()` 2.4 KB → edit `MainActivity.kt:150` (known from `node().compose.slots`), rebuild, relaunch → `capture(diff_from="baseline")` 2.0 KB, e.g. `issues resolved: 12 role`. Refs carry via `@tag`/`#rid` across the pid change. About 1.3k tokens per iteration.
- Today: ≈25k tokens per iteration, diffing finding lists by hand.

**W6. Large list (259-view fake screen)**
- New: `capture()` ≤3 KB → `find(text="Label 4", limit=20)` ≤3 KB → `node("n47", props="nondefault")` ≤1.5 KB → `outline(root="n3", depth=1)` ≈1 KB. Total ≈ 2.4k tokens.
- Walking everything is `outline(depth=99)` over 3 pages of ≤6 KB each.
- Today: `inspect` 547 KB (≈137k) or `inspect+props` 3.85 MB (≈960k).

**W7. Mixed hierarchy: "the 3rd feed card's Delete does nothing" (RecyclerView of ComposeViews plus a dialog)**
- New: `capture()` 2.5 KB (the dialog is its own window, z1) → `find(text="Delete", flags=["click"], within="#feed")` 1 KB (breadcrumbs show which cell) → `lint(within="#feed")` 0.8 KB (template-collapsed) → `node("n118")` 1.2 KB (actions lack CLICK, or the node is under the dialog window). Total ≈ 1.6k tokens.
- Today: not reliable. Keys collide across cells (ID3), nested ComposeViews are missed (ID2), and dialog coordinates are offset (CO4).

**W8. The same flow from the stateless CLI (an agent using Bash); the store is shared with the MCP server**
```
$ inspector-widget capture --label before          # prints c7h2kq + summary
$ inspector-widget find --flags click --max-dp 47   # n22 "Section heading…" (latest = c7h2kq)
$ inspector-widget node n22 --json                  # same bytes as MCP node("n22")
$ inspector-widget image n22 --pad 16 --out /tmp/n22.png
$ inspector-widget outline --view slots --cursor c7h2kq:o:3f9a12bc:80
```

---

## 9. Migration

| Phase | Work packages | Result |
|---|---|---|
| 0 (now) | F1, P0-1, P0-2, G1 | Every legacy response compact, brief, budgeted and spilled. Parity table test. Goldens recorded. |
| 1 (library, parallel) | C1 → C2 C3 C4 C5 C6 C7 C8 C9 | The capture package, fully offline-tested. Not yet reachable from the entrypoints, so parity trivially holds. |
| 2 (surface) | S1 → S2 → L1 → S3 → D1 → S4 | New tools on both surfaces (opt-in toolset), live verification, legacy on captures with dead code deleted, docs, then the deliberate flip. |
| 3 (optional) | R1, R2, R3 | Text overflow, `act`, atomic capture and agent identity extras. |

**Legacy mapping (S3)**
Each legacy tool takes a capture with the needed facets, or reuses the lineage's latest if it is less than 2 s old with the same options. It then runs the existing shapers (`strings`, `a11y`, `correlate`) over a `ReplaySession(capture)` that serves the stored pbs through the Session method surface. Output is therefore byte-identical to the Phase-0 goldens, apart from the added `capture` field.

| Old tool | Replacement |
|---|---|
| `dump_tree` | `outline(view="views")` / `captures export what=views format=legacy` |
| `get_properties` | `node(view:<id>, props="all")` |
| `screenshot` | `image()` |
| `dump_compose` | `outline(view="compose"\|"slots")` |
| `compose_overlay` | `image(overlay="compose")` |
| `dump_accessibility` | `outline(view="a11y"\|"reading")` |
| `a11y_lint` | `lint()` |
| `a11y_overlay` | `image(overlay="lint")` |
| `inspect` | `capture()` + `outline(detail="all")` |
| `inspect_node` | `node(sel, facets="all", image=true)` |
| `component_image` | `image(ref)` |

**What S3 deletes from `mcp_server.py`** (target ≤700 lines, from 1,970):
- the remainder of the parallel decoder;
- the screenshot helpers and the second PNG encoder;
- the `HostFacade`/`_first_attr`/`_import_proto` fallbacks (a probe is kept for `--self-check`);
- `_device_density` and `_lint_fn`;
- the 15 `_h_*` wrappers and the hand-written TOOLS literal. Legacy specs live in `ops_legacy.py`.

The CLI raw-Client subcommands (attach, dump, compose, a11y, a11y-lint) move onto sessions: no SHUTDOWN except in `detach`, and `--force` works everywhere. `cli.py` target: ≤400 lines. `correlate.py` is not edited; its a11y-stream owners retire it later.

**Compat promises**
- The CLI `--json` shapes only change in Phase 0 (brief; `--detail full` restores them).
- Node keys `view:<id>` and `compose:<id>` keep working as selectors.
- `CONTRACT.md` is untouched until phase 3, and those changes are additive.

---

## 10. Module interfaces (contracts that let the work packages proceed in parallel)

```python
# output.py (P0-1)
def dumps(obj, pretty=False) -> str
class Budget:  # utf-8 byte accounting with footer reserve
    def __init__(self, max_bytes: int, reserve: int = 200): ...
    def fits(self, s: str) -> bool; def add(self, s: str) -> bool
def finalize(tool: str, result: dict, *, max_bytes: int|None, spill_dir: str, preview=None) -> str   # returns compact text or envelope
def slim(tool: str, result: dict, args: dict) -> dict          # Phase-0 brief rules; detail=full is identity
OUTPUT_PARAMS: dict[str, list[ParamDef]]                       # tool -> extra params
def augment_schemas(tools: dict) -> None; def add_cli_flags(sp, tool: str) -> None
# normalize.py (P0-1)
def compose_value(key: str, raw: str) -> str|None; def is_action_attr(raw: str) -> bool
def textstyle_brief(raw: str) -> str; def is_library_source(src: str|None) -> bool
def a11y_node_brief(d: dict, app_package: str|None) -> dict; def color_hex(argb: int) -> str
def nondefault_props(props: dict[int, dict[str, object]], classes: dict[int, str]) -> tuple[dict, dict]  # values, omitted counts
KEY_PROPS: dict[str, tuple[str, ...]]

# capture/model.py (C1)
@dataclass class CaptureOptions: props: bool=True; resolution_stack: bool=False; slots: str="if_available"; screenshot: bool=True; screenshot_scale: float=1.0; skp: bool=False; a11y_rendering: bool=False; lint: str="tree"; settle_ms: int=0
@dataclass class CaptureMeta: id, lineage: tuple[str,str], pid, api, abi, agent_version, device: dict, created_at, took_ms, options, facets: dict, fingerprint, consistency, compose_generation, label, pinned, prev, diagnostics: list, schema: int = 1
@dataclass class RawCapture: meta, windows: bytes, views: bytes, compose_sem: bytes, slots: bytes|None, a11y: bytes, a11y_render: bytes|None, shots: dict[int, bytes], skp: dict[int, bytes]
@dataclass class UNode: (fields of §3.3)
@dataclass class Tree: roots: list[str]; children: dict[str, list[str]]
@dataclass class Index: meta; nodes: dict[str, UNode]; trees: dict[str, Tree]; reading: list[str]; by_key: dict[str, str]; diagnostics: list[str]
class OpError(Exception): code: str; message: str; hint: str|None; candidates: list|None
def index_to_jsonl(ix) -> bytes; def index_from_jsonl(b, meta) -> Index

# capture/store.py (C2)
class CaptureStore:
    def __init__(self, root=None, persist=True, clock=time.time)
    def refs_lock(self) -> ContextManager
    def next_refs(self, n: int) -> int                                    # under refs_lock
    def lineage_state(self, serial, package) -> LineageState; def save_lineage_state(self, st)
    def publish(self, raw: RawCapture, ix: Index, refmap: dict) -> str   # atomic; returns id
    def resolve(self, spec: str = "latest", lineage: tuple|None = None) -> str
    def load(self, cid: str) -> "LoadedCapture"                           # .meta .index() .raw(name) .shot(root) .derived(name) .put_derived(name, bytes)
    def list(self, lineage=None, limit=20) -> list[CaptureMeta]
    def label(self, cid, name) -> str|None; def pin(self, cid, on: bool); def drop(self, cid)
    def gc(self, all=False) -> dict; def spill_dir(self) -> str
    def default_session(self) -> tuple|None; def set_default_session(self, serial, package)

# capture/fetch.py (C3)
FACETS: dict[str, Facet]
def fetch(session, opts: CaptureOptions, *, compose_generation: int, clock=time.monotonic) -> RawCapture
def fingerprint_now(session) -> str
def settle(session, settle_ms: int) -> None

# capture/index.py (C4)
def build_index(raw: RawCapture) -> Index          # nodes keyed by canonical key, ref=None
def apply_refs(ix: Index, refmap: dict[str, str]) -> Index

# capture/refs.py (C5)
@dataclass class LineageState: latest: str|None; history: list[str]; labels: dict; tomb: dict
def assign(new: Index, prev: Index|None, *, same_pid: bool, same_generation: bool, alloc: Callable[[int], int]) -> tuple[dict[str, str], dict]   # refmap, tomb updates

# capture/query.py + lines.py (C6)
def resolve_selector(ix, sel: str) -> UNode
def outline(ix, **params) -> dict; def find(ix, **filters_and_params) -> dict
def node(ix, loaded, refs: list[str], **params) -> dict
def render_line(ix, n, fields, depth=0, hidden=0, tail="") -> str

# capture/analyzers.py + rules.py (C7)
def analyze(ix, loaded, *, lint: str, density: int, font_scale: float) -> None   # issues, stops, reading
def lint_view(ix, loaded, **params) -> dict; RULES: dict; ALIASES: dict

# capture/diff.py (C8)
def diff(a: Index, b: Index, **params) -> dict
# capture/images.py (C9)
def crop(loaded, n, *, pad, max_side) -> dict; def overlay(loaded, ix, kind, marks, **kw) -> dict
def inline(path, max_side) -> tuple[str, str, int]; def pixel_diff(la, lb, a, b) -> dict

# ops.py (S1)
@dataclass class OpContext: store: CaptureStore; sessions: SessionProvider; caller: str
class SessionProvider(Protocol):
    def get(self, serial: str, package: str): ...    # MCP: wraps mcp_server.SESSIONS.get_or_attach; CLI: iw.attach + close() at exit
    def close_all(self) -> None: ...
def capture(ctx, **p) -> dict; outline/find/node/image/lint/diff/captures(ctx, **p) -> dict
# surface.py (S2)
@dataclass class Param: name, type, default, enum=None, minimum=None, maximum=None, help="", cli=None, positional=False
@dataclass class ToolSpec: name, cli_name, summary, params: list[Param], fn, read_only: bool, toolsets: set[str]
SPECS: list[ToolSpec]; INSTRUCTIONS: str
def json_schema(spec) -> dict; def validate(spec, args) -> dict
def mcp_entries(toolset: str) -> dict; def add_cli(subparsers) -> None
def run(name, args, ctx) -> tuple[str, list, bool]    # text, images, is_error
```

Library modules read only the public Session surface: `dump_tree`, `get_windows`, `screenshot`, `dump_compose`, `dump_a11y`, `get_properties`, plus `pid`, `serial`, `package`, `api_level`, `abi`, `agent_version`, `build_id` via getattr, and `close()` if present. They never call `detach()`.

---

## 11. Test and verification plan
- **Fixtures (F1)**, all offline:
  - the harness fake adb + agent (improve/offline-e2e-harness);
  - `wide_scene(fan=6)`, reproducing E6's 259-view screen;
  - launcher and View-screen **replays** converted from the real JSON in `scratchpad/live` (with screenshots).
  - L1 adds raw-pb replays of post-ID1 real captures.
- **Golden sizes** make "chonky" a regression test. Every tool × scene × default args must stay ≤ its budget (Phase 0: P0-2; capture tools: S1; real replays: L1).
- **Goldens before the refactor (G1)**: the outputs of all 15 legacy tools and 13 subcommands, which S3 must reproduce.
- **Parity**: `test_phase0_parity.py` is a hand map used until the registry exists. `test_surface.py` checks that every spec has an MCP tool and a CLI subcommand with identical names and defaults, and that CLI `--json` and MCP text are byte-equal for the same store.
- **Unit**:
  - the store (two-process publish race, GC under pin, crash mid-publish, memory-only mode);
  - the matcher (mutation suite and a 1,000-round never-reuse property test);
  - the query engine (grammar regex on every line, cursor completeness, random budgets);
  - the selector parser (errors with columns);
  - the analyzers (render signals, template collapse, lint mapping);
  - diff;
  - images (per-window crop, OV1 regression).
- **Symbol parity**: `test_symbol_parity.py` polices `output`, `ops`, `surface` and `capture.*` too (P0-2, S2).
- **Live verification (CLAUDE.md §2; AGENTS.md §6)** on `emulator-5554` with `com.oberkfell.a11yprobe`:
  - P0-2: all 15 tools and 13 subcommands, with sizes;
  - C3: a scratch fetch driver;
  - S2: workflows W1-W5 through both surfaces;
  - L1: capture timing, the slots=enable generation bump, tap-then-diff on `#badSwitch`, and a CLI reading an MCP-made capture;
  - S3: all legacy paths;
  - S4: the tool list per toolset.

---

## 12. Risks
1. **Stale captures mislead the agent.** Mitigations: capture is cheap (≈0.6 s); responses carry `age_s`, `stale` and `pid_changed`; `capture(diff_from)` and `act` always return fresh state; the instructions say to recapture after interacting.
2. **Carry-over mismatches**, worst case a recycled cell. Mitigations: refs are never reused; the collection guard; ambiguity gives a new ref; `rebound_of`; `match` and `since` per node; diff shows label changes, so a wrong carry is visible.
3. **Correlation quality before the identity fixes.** Mitigations: the ID1 detector with one-to-one IoU, per-facet `conf`, capture diagnostics; "exact" is only an acceptance target on post-ID1 fixtures (L1).
4. **Brief defaults hide the value that matters** (e.g. a "default" property that is the bug). Mitigations: every omission is counted; `detail="full"`, `params="raw"`, `props="all"`, `origin="all"` and `spill`; golden tests pin the omission counts.
5. **Truncation hides the needed node.** Mitigations: explicit `truncated` with a cursor; `find` ignores depth; semantic collapse keeps labelled and issue-bearing nodes; budgets are adjustable up to 32 KB.
6. **The line grammar is a new mini-language.** Mitigations: it is versioned and documented once in the instructions and the `outline` description; `format:"json"` rows are equivalent; tests check every emitted line against the regex.
7. **`slots="enable"` recomposes the app**, resetting state and re-minting ids. Mitigations: it is opt-in and destructive-labelled, issued first so the capture is self-consistent, bumps `compose_generation` (disabling id-based carry, while locators and structure still carry), and the long-term fix is startup-attach (R3).
8. **Facet skew across 5-7 requests.** Mitigations: the fingerprint re-check with retries, `consistency`, `settle_ms`; the atomic CaptureCommand in R3.
9. **Disk growth and privacy.** Mitigations: 0700, TTL, caps, heavy-file stripping, `PERSIST=0`, `gc --all`; agent-side password redaction (D8).
10. **Cross-process races.** Mitigations: mkdir-exclusive staging, rename to publish, `.complete`, flock on refs, GC and labels, rename-to-trash deletes.
11. **Tool-surface churn and tool-selection confusion.** Mitigations: toolsets never overlap by default; the flip is a separate, deliberate WP (S4) and env-reversible; legacy tools stay callable; CLI aliases are permanent; goldens.
12. **The S3 refactor reintroduces "ships fine, dies on device".** Mitigations: it comes after S2 and L1 are live-verified; ReplaySession reuses the existing shapers; goldens; symbol parity; mandatory live verification of every legacy path.
13. **Conflicts with improve/session-lifecycle.** Mitigations: the scope rules in §14; S2 and S3 start only after it merges.
14. **MCP client variance** (ImageContent, `instructions` ignored). Mitigations: paths are the default; tool descriptions still name the workflow; the fallback server supports both.
15. **Python protobuf recursion limit (~100 nesting levels) on very deep trees.** Mitigations: a parse failure becomes facet status `error` with a diagnostic, and the other facets still work; the agent-side depth cap belongs to the agent-hardening stream.
16. **The global ref counter grows** to 5-6 digits over weeks. Accepted: 2-3 tokens per ref; `gc --all` resets it and documents the reset.
17. **Heuristics** (display type, app vs library origin, inferred `render.clipped`, the props-default table). Mitigations: `conf` fields and "likely" wording; raw facets are one call away; R3 replaces them with agent data.

---

## 13. Acceptance criteria (global)

1. **Phase 0.**
   - Every legacy MCP tool and CLI `--json -` output is compact and brief, and ≤ 32,000 B by default.
   - The §2.6 targets are met on the launcher and View-screen replays and on `wide_scene`.
   - Spill files round-trip.
   - `detail="full"` with `max_bytes=0` reproduces the pre-change content.
   - The parity table test, symbol parity and `scripts/test.sh` are green, and live verification on emulator-5554 is recorded.
2. **Capture tools** (S1 offline plus L1 real replays).
   - Launcher: capture ≤2,500; `outline()` ≤2,500; `outline(root=list)` ≤2,000; `outline(view="slots")` ≤6,000; `outline(view="reading")` ≤2,000; `find` ≤600; `node` ≤1,500; `lint` ≤1,200; `captures list` ≤400; `image` ≤400.
   - View screen: `outline()` ≤3,000 (all 40 views); `node(props="nondefault")` ≤1,200.
   - 259-view: capture ≤3,000; outline pages ≤6,000, with exactly 259 View lines across pages; `find(limit=20)` ≤3,000; `node(props="nondefault")` ≤1,500.
   - Every default response ≤ its budget for every tool × scene.
3. **Behaviour.**
   - Refs are stable across unchanged recaptures and never reused (property test); stale refs return `ref_not_in_capture`.
   - `latest`, labels and cursors resolve identically from the CLI and the MCP.
   - A capture made by the MCP server is readable by the CLI and vice versa.
   - CLI `--json` equals MCP text byte for byte.
4. **Performance** on emulator-5554, launcher.
   - Warm capture (props, screenshot, a11y, semantics, tree lint) p50 ≤1.0 s.
   - `outline`/`find`/`node` p95 ≤50 ms on real captures and ≤20 ms on a 5,000-node synthetic.
   - Index build ≤100 ms on the launcher.
5. **Correctness.**
   - Per-window crops come from the right window.
   - Lint/reading overlay colours key by ref (OV1 regression).
   - ID1 fallback diagnostics appear on pre-ID1 data; ≥95% of a11y facets are `conf:exact` on post-ID1 fixtures.
   - Composite semantics keys are distinct across ComposeViews.
6. **Migration.**
   - Legacy goldens reproduce after S3, with only the documented deltas.
   - `mcp_server.py` ≤700 lines and `cli.py` ≤400.
   - `tools/list` for the `capture` toolset ≤12,000 B.
   - The default toolset flips only in S4.
7. **Workflows** W1-W5 run live through both surfaces within the §8 token totals (±25%).

---

## 14. Coordination with concurrent branches

- **improve/session-lifecycle** is in progress. It owns `inspector_widget/{client,inject,adb,__init__}.py`, and in `mcp_server.py` the `SessionCache`, `_session_alive`, `_safe_detach`, `_run_tool` (retry and error mapping), `tool_attach`/`tool_detach`, atexit and transport. It also owns the session teardown lines in `cli.py` (E4).
  - P0-2 touches only `_call_tool_text` (encoding), one `output.augment_schemas(TOOLS)` line, the bodies of `tool_dump_tree`/`tool_get_properties` plus the dead decoder functions (E3), the header comment, the CLI JSON-emission sites and the parser flags.
  - E3 is also listed for session-lifecycle in the ledger. The P0-2 owner checks with that branch first and does E3 only if it is still open.
  - All new code lives in new modules (`output`, `normalize`, `capture/*`, `ops`, `surface`, `ops_legacy`).
  - S2, S3 and S4 start only after session-lifecycle merges to `main`. The CLI SessionProvider relies on its `Session.close()` (no SHUTDOWN).
- **improve/offline-e2e-harness** (fake agent and fake adb, extended symbol parity) must merge before F1.
- **improve/a11y-agent-identity** (ID1, ID2, CO4 agent fixes) is not a build gate. The "exact" acceptance in L1 requires fixtures recorded after it merges.
- **a11y-host-model / a11y-lint-unified** (L1/L2/RO1/OV1/A2-A5): consumed only through the C7 adapter. No capture WP edits `a11y.py`, `a11y_lint.py`, `overlay.py`, `correlate.py` or `strings.py`.
- **agent-hardening stream** (Dispatcher.kt, TreeBuilder.kt, ComposeInspector.kt): R1 and R3 coordinate with it and start after session-lifecycle.
- Naming (AGENTS.md §1): every new user-facing name is `inspector-widget`/`INSPECTOR_WIDGET_*`. No codename identifier is renamed. The project is local-only: commit locally, never push.
