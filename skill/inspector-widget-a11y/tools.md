# Inspector Widget — tools (MCP) and CLI reference

The Inspector Widget MCP server (codename `viewspector`, at `host/mcp_server.py`)
has 26 tools and lists 18 by default: the 15 inspection tools below and the 3
TalkBack tools. The 8 capture-and-walk tools are listed with
`INSPECTOR_WIDGET_TOOLSET=capture` (or `all`; every tool stays callable by
name); `capture,talkback` lists them with the TalkBack tools, the set for
TalkBack navigation bugs. The host CLI (`host/cli.py`) mirrors all of them in 24 subcommands for
scripting. This is the reference for the accessibility workflow plus the
View/Compose tools you may reach for, and the MCP↔CLI mapping.

---

## MCP tools

All take `package` (except `list_devices` / `list_processes`) and an optional
`serial` (default: `$ANDROID_SERIAL`, else the only attached device). All
auto-attach, and re-attach on their own if the agent went away. Images are
written to temp PNG files (deleted when the MCP server exits) and the **path**
is returned (not inlined). A failed call returns `{error, hint?}`. Every tool
that used a session (not `list_*`, `talkback` or `detach`) adds the session's
`note`, if it has one, to its result or error, as the CLI prints it as a
warning: e.g. the agent runs another build than the local one;
`attach(force=true)` replaces it.

**Output (every tool, MCP and CLI alike).** Compact JSON, brief by default:
`detail="brief"` drops what is rarely needed and counts each omission
(`omitted`, `hidden`, `omitted_defaults`, `hidden_descendants`); `detail="full"`
returns the whole result. Tree tools (`dump_tree`, `dump_compose`,
`dump_accessibility`, `inspect`) take `max_depth` (1 = the roots) and `root`
(a node id or key from the result). `max_bytes` (default
`$INSPECTOR_WIDGET_MAX_BYTES`, else 32,000; `0` = no cap; 1,000..200,000) caps
the reply: a larger result becomes a **spill envelope** `{truncated, tool,
bytes, max_bytes, summary, preview, spill_path, hint}` of at most 3,000 bytes,
with the whole brief result in the `spill_path` file (read it with jq). The
hint says what to narrow; `max_bytes` exists on `dump_tree`, `get_properties`,
`dump_compose`, `dump_accessibility`, `a11y_lint` and `inspect` (for the
others, only `INSPECTOR_WIDGET_MAX_BYTES` raises it). The capture-and-walk and
TalkBack tools budget themselves instead (`max_bytes` per tool, an explicit
`truncated` / omitted marker, never a spill).

### Discovery / session
- **`list_devices()`** → `{devices:[{serial, api, abi, model, state}], count}`.
- **`list_processes(serial)`** → `{processes:[{package, pid, running}], count}` —
  debuggable apps only, running first.
- **`attach(serial, package, force=false)`** → `{attached, pid, warm, reused,
  api_level, abi, agent_version, build_id, window_count, root_ids, session}`.
  Optional warm-up; idempotent. `force=true` replaces a running agent. A `note`
  means the agent runs another build than the local one (kept because another
  client uses it); `force=true` replaces it.
- **`detach(serial, package, shutdown=true)`** → `{detached, agent_stopped, note?}`.
  Stops the agent for every client; `agent_stopped:false` (with a `note`) means
  it is still running. `shutdown=false` only drops this server's connection.
  Safe even if not attached.

### Accessibility (the core of this skill)
- **`dump_accessibility(serial, package, include_extras=true,
  include_rendering_info=false, focus_order="stops", max_depth?, root?)`** →
  unified `AccessibilityNodeInfo` tree (Views + Compose virtual nodes) with
  text/contentDescription/stateDescription/role, state flags, bounds, decoded
  actions, collection/range info, a `node_key` per node, plus the host-computed
  TalkBack `focus_order` (`[{order, key, speak}]`: each focus stop and what
  TalkBack announces there; `focus_order="none"` leaves it out, `"full"` adds
  the a11y ids), `reading_order_diagnostics` and a `generation`. The order is over the tree TalkBack sees: Views not
  important for accessibility carry `ignored` (`not_important`: their children are
  read in their place; `hidden`: a noHideDescendants subtree) and are no stops;
  each window carries `window_type` / `window_flags` / `modal`, and a window under
  an open modal dialog carries `covered_by` and has no stops.
- **`a11y_lint(serial, package, include_contrast=true, scale=1.0,
  wcag_mode=false, rules=[...], include_rendering_info=true, group_by="rule")`**
  → brief (default): `{summary, by_rule:{"<rule>":{sev, n, msg, nodes:[first 3
  node keys], more?}}, covered_by_rule?, diagnostics (warn/error), density,
  font_scale, generation, contrast_sampled, omitted}`. `group_by="none"` (or
  `detail="full"`): `{summary, findings:[{rule, alias, severity, node_key, node,
  bounds, bounds_dp, window, collection, message, evidence}], covered_findings?,
  diagnostics, stats, ...}`. The DETECT and VERIFY engine, run over the unified
  a11y tree (Views + Compose). `findings`, `by_rule` and the `summary` counts are
  what TalkBack can reach now. Findings on a window under an open dialog
  (`window.covered_by`) are kept apart: counted in `summary.covered {error, warn,
  info, total, windows}`, grouped in `covered_by_rule` (the shape of `by_rule`)
  when brief, listed in full in `covered_findings`.
  `rules` runs a subset (ids, `R1`..`R23` aliases or ATF names; unknown ids are a
  tool error); `wcag_mode` uses 44dp targets; `include_contrast=false` skips the
  pixel rule. See **rules.md**.
- **`a11y_overlay(serial, package, scale=1.0, include_contrast=true,
  wcag_mode=false)`** → `{path, boxes, labels, flagged, flagged_by_bounds,
  finding_count, covered_windows?, findings_covered?, summary, ...}`. Screenshot
  with every a11y node boxed, each focus stop numbered and labelled with what
  TalkBack says, colored by severity (red=error, amber=warn, blue=info,
  green=clean; a finding that maps to no a11y node is a dashed box at its own
  bounds). A window under an open dialog is not boxed (its boxes would cover the
  dialog); its findings are only counted (`findings_covered`). The SEE step.

### Per-element dossier / image
- **`inspect_node(serial, package, node_key|view_id|semantics_id|bounds,
  include_image=true)`** → dossier `{node_key, bounds, correlation_confidence,
  generation, where, context, view?, compose?, a11y?, list_item?, a11y_only?,
  a11y_parent?, resolved_from?, key_note?, component_image{path, window?}, lint[],
  lint_summary, lint_diagnostics}`. `where` is a breadcrumb such as `view:20
  RecyclerView > row 1: view:31 ComposeView > composeview:32 > compose:32:4 Button
  'Delete'`. `compose` carries the semantics attrs (Role, TestTag, Text, ...); its
  `source` is null here, since `file:line` needs the slot table (`dump_compose`
  with `enable_inspection`; match by testTag or bounds). `view` carries typed
  properties; `a11y` is the element's node; `lint` is exactly the `a11y_lint`
  findings for this element and the a11y nodes merged into it. The LOCATE step.
  Selectors: `node_key` = `"view:<id>"`|`"compose:<acvId>:<semanticsId>"`|
  `"composeview:<acvId>"`; `view_id` = uniqueDrawingId; `semantics_id` = Compose
  id (only when one ComposeView has it); `bounds` = `{x,y,w,h}` px (deepest
  covering element). Every key the a11y dump, the lint and `inspect` hand out
  resolves; a stale Compose key is re-resolved (`resolved_from`) when unambiguous,
  and a recycled cell's key gets a `key_note`.
- **`component_image(serial, package, node_key|view_id|semantics_id|bounds)`** →
  `{path, source, window?, serial, package, node_key}` — a cropped PNG of one element (`source`: `skp` |
  `bitmap_crop`, cut from the element's own window, e.g. a dialog). Use when you
  only need the picture.

### Compose / View (context, not a11y-specific)
- **`dump_compose(serial, package, include_semantics=true,
  include_slot_table=true, enable_inspection=false)`** → Compose semantics tree;
  slot-table composables with `file:line` only if inspection is already on or you pass
  `enable_inspection=true`. That hot-reloads and **resets `remember{}` state** (open
  dialogs, typed text, scroll, toggles) and re-mints Compose node ids, so do it before
  reproducing a state-dependent bug, not after. The layer `dump_tree` cannot see.
  An empty slot table comes with a `note`: it suggests `enable_inspection` only
  when a readable Compose UI is on screen, and otherwise says why there is no slot
  table (no ComposeView; `compose_obfuscated`; `semantics_failed`).
- **`compose_overlay(serial, package, scale=1.0, all_boxes=false)`** → screenshot
  with every on-screen Compose element boxed (text/role + bounds) + a flat
  on-screen text list.
- **`dump_tree(serial, package, include_properties=false,
  include_resolution_stack=false, include_screenshot=false, scale=1.0)`** → the
  classic Android View hierarchy, with `diagnostics` when the agent cut or could
  not read part of it (`depth-truncated=N`; the cut nodes carry
  `CHILDREN_TRUNCATED`, masked password fields `TEXT_REDACTED`).
- **`get_properties(serial, package, view_id, include_resolution_stack=false)`** →
  one View's typed attributes (colors `#AARRGGBB`, resources resolved,
  `is_layout` marked).
- **`screenshot(serial, package, scale=1.0)`** → `{path, width, height}`.
- **`inspect(serial, package, include_properties=false, include_overlay=false)`** →
  whole-screen merged view+compose+a11y model with per-node correlation and a
  `summary.generation`; can render the integrated overlay (every window
  composited; box colour = correlation: green `exact`, amber `overlap` (label
  `a11y~IoU`), grey `none`).

### TalkBack (device-wide: TalkBack runs for every app; settings are restored)
Listed in the default toolset (in their pre-capture shape) and with
`INSPECTOR_WIDGET_TOOLSET=talkback`, `capture,talkback` or `all`; one
implementation either way, so results name capture refs; where the capture
tools are not listed (the default listing) `keys` maps them to node keys
(`inspect_node(node_key=...)`), and `next` names only listed tools. Failures are
error envelopes with a code (`talkback_unavailable`, `busy`, `injector_failed`,
`start_not_found`, `app_left_foreground`, `talkback_on`, `log_level_failed` ...).
Never retried. Being device-wide, they act only on a device and app the caller
named or its own default session (its last attach or capture), else
`$ANDROID_SERIAL` or the only device: never on the store's shared default alone
(another agent's). A walk or scenario on an app you did not name says which
(`session`).
- **`talkback(action=status|on|off|restore, serial?, package?, verbose_log=false)`**
  → TalkBack state, or what changed (`on` snapshots the settings first).
- **`tb_walk(serial?, package?, start="current", direction="next", max_steps=60,
  until="wrap", expect=[...], recapture="on_unknown", leave_on=false, ...)`** →
  captures the screen with TalkBack on, presses the real TalkBack's
  next/previous, and returns `{capture, walk, recaptured?, start, steps, ended,
  lines, diff, findings, restore, next}` (at most 5 KB at 60 steps): one line
  per step by ref (`3. n14 "Add to favorites, Button" via=autoscroll(n10)
  !double_stop`), `diff` = actual vs model by class and ref (`model`, `skip`,
  `double`, `out_of_order`, `loop`, `trap`, `escape`, `stuck`, `left_app` ...).
  `start` and `expect` take refs, selectors (`@tag`, `#rid`, `Type"label"`) or
  labels (a label that only looks like a selector, `@alice`, is matched as
  spoken); a ref no capture holds fails before TalkBack is touched. Focus on a
  node no capture holds (scrolled in) recaptures, at most once per 3 steps.
  Stored as `<store>/walks/<id>.json`.
- **`tb_scenario(kind=focus_after|restore|survive, serial?, package?, target?,
  action="activate", mutate?, wait_ms=2000, ...)`** → where real TalkBack focus
  goes after an action (`activate`, `back`, `tap:<ref>`, `key:<combo>`), after
  back, or after a list update (`mutate`); `{scenario, capture, after, target,
  did, timeline, focus, verdict, finding?, cause?, restore}` (at most 1 KB;
  `cause` from the before / after captures).
- **`image(overlay="walk", walk=<id>)`** draws a walk on its capture (numbered
  arcs in TalkBack's order, the model's next stop dashed, mismatches red);
  **`captures(what="walks")`** lists the stored walks, `captures(action="show",
  id=<walk id>)` shows every step; with `what="walks"`, `show`, `export` and
  `drop` take a walk id or `latest` (never a capture).
- The loop: `capture -> lint(rules=["tb"]) -> outline(view="reading",
  explain=true) -> node(ref, facets="tb") -> tb_walk(start=ref) ->
  image(overlay="walk")` (SKILL.md §5).

### Capture and walk (`INSPECTOR_WIDGET_TOOLSET=capture` or `all`)
Capture once, keep it by id, walk it with small budgeted queries (no device
I/O after the capture). Refs (`n23`) carry across captures of one app.
- **`capture(serial?, package?, label?, slots="if_available", lint="tree",
  diff_from?, ...)`** → `{capture, session, device, facets, windows, lint,
  issues, diagnostics?, outline, on_screen, next}`. `slots="enable"` hot-reloads
  the app (destructive; never retried). `diagnostics` leads with what the agent
  could not send (`views: depth-truncated=N`, `compose: compose_obfuscated...`).
- **`outline(view=ui|views|compose|slots|a11y|reading, root?, depth=3, ...)`**,
  **`find(text, type, rid, tag, src, role, flags, issue, within, at, min_dp,
  max_dp, window, ...)`** (filters ANDed; `max_dp` on the touch bounds;
  `flags=["truncated"]` lists nodes with cut children), **`node(ref|refs)`**,
  **`image(ref|overlay)`**, **`lint(rules, within, severity, contrast, ...)`**
  (one bug repeated in list cells is one `×N in <list> cells` line; findings
  under an open dialog are counted apart in `covered {n, windows, by}` and listed
  with `within=<that window>`),
  **`diff(a="prev", b="latest")`** (issue deltas only on nodes both captures
  hold: `resolved`, `new`, plus `gone_with_node` / `on_new_nodes` counts), and
  **`captures(action=list|show|pin|unpin|label|drop|export|gc, what=...)`**
  (`what="walks"`: the stored TalkBack walks). The TalkBack model runs on a
  capture: `outline(view="reading", explain=true, include_skipped=true, from=,
  direction=, granularity=)`, `node(ref, facets="tb")` and `lint(rules=["tb"])`
  ([rules.md](rules.md#talkback-navigation-rules-tb)).
- `serial`/`package` default to this caller's own last attach or capture (the
  MCP server's; `INSPECTOR_WIDGET_SESSION=serial/package` for a CLI), then the
  store's shared default; a query resolved by the shared default while the
  store holds other apps carries `session`.

---

## CLI (host/cli.py)

Each call is its own process: it connects to the running agent (injecting it
the first time) and leaves it running when it exits; only `detach` stops it.
Only the Compose key registry (a small file per app process) carries over, so a
`compose:` key from `a11y-lint` still resolves in a later `inspect-node`. Run
from the project root (adds its own dir to `sys.path`). `--serial` defaults to
`$ANDROID_SERIAL`, else the only attached device.

```
python host/cli.py devices
python host/cli.py packages   --serial SERIAL
python host/cli.py attach     --serial SERIAL --package PKG
python host/cli.py detach     --serial SERIAL --package PKG
python host/cli.py dump       --serial SERIAL --package PKG [--json -] [--screenshot out.png] [--properties]
python host/cli.py get-properties --serial SERIAL --package PKG --view-id ID [--json -] [--resolution-stack]
python host/cli.py screenshot --serial SERIAL --package PKG --out out.png [--scale 0.5]
python host/cli.py compose    --serial SERIAL --package PKG [--json -] [--overlay out.png] [--all-boxes] [--enable-inspection]
python host/cli.py a11y       --serial SERIAL --package PKG [--json -] [--overlay out.png] [--lint] [--wcag] [--no-contrast]
python host/cli.py a11y-lint  --serial SERIAL --package PKG [--json -] [--rule RULE_ID|R#]... [--overlay out.png] [--wcag] [--no-contrast] [--no-rendering-info]
python host/cli.py inspect    --serial SERIAL --package PKG [--json -] [--properties] [--overlay out.png]
python host/cli.py inspect-node    --serial SERIAL --package PKG (--node-key KEY | --view-id ID | --semantics-id ID | --bounds x,y,w,h) [--json -] [--no-image]
python host/cli.py component-image --serial SERIAL --package PKG (--node-key KEY | ...) --out out.png
python host/cli.py talkback   [status|on|off|restore] [-s SERIAL] [--json]
python host/cli.py tb-walk    [-s SERIAL] [-p PKG] [--start REF] [--prev] [--expect A,B ...] [--until edge] [--json]
python host/cli.py tb-scenario focus-after|restore|survive [--target REF] [--action tap:REF] [--mutate ...] [--json]
python host/cli.py capture    [-s SERIAL] [-p PKG] [--label L] [--json]      # prints the summary (-q: the id)
python host/cli.py outline    [-c CAPTURE] [--view reading] [--root SEL] [--fields -bounds] [--json]
python host/cli.py find       [-c CAPTURE] [--type T] [--tag T] [--flags click] [--max-dp 47] [--count] [--json]
python host/cli.py node       REF [REF...] [--props nondefault] [--json]
python host/cli.py lint       [-c CAPTURE] [--rule R1] [--within SEL] [--json]
python host/cli.py diff       [A] [B] [--json]
python host/cli.py captures   [ls|show|pin|unpin|label|rm|export|gc] [ID] [LABEL] [--what walks]
python host/cli.py image      [REF] [--overlay walk --walk WALK_ID] [--out out.png]
```

The tree and lint subcommands take the MCP output parameters as flags with the
same defaults (`--detail`, `--max-bytes`, `--max-depth`, `--root`,
`--group-by`, `--focus-order`, `--filter`), and `--json -` prints exactly what
the MCP tool returns. The capture-and-walk subcommands print a human rendering
by default (their `next` hints as `inspector-widget ...` commands) and the MCP
text with `--json`.

### MCP ↔ CLI mapping

| MCP tool | CLI equivalent |
|----------|----------------|
| `list_devices` | `devices` |
| `list_processes` | `packages --serial …` |
| `attach` | `attach --serial … --package …` |
| `a11y_lint` | `a11y-lint …` ( `--rule` per rule, `--wcag`, `--no-contrast`, `--no-rendering-info`, `--overlay` ) |
| `dump_accessibility` | `a11y …` ( `--json -` for the tree ) |
| `a11y_overlay` | `a11y --overlay out.png --lint` (or `a11y-lint --overlay out.png`) |
| `dump_compose` | `compose --json -` |
| `compose_overlay` | `compose --overlay out.png` |
| `dump_tree` | `dump --json -` |
| `get_properties` | `get-properties --view-id ID` |
| `screenshot` | `screenshot --out out.png` (or `dump --screenshot out.png`) |
| `inspect` | `inspect --json -` (`--overlay out.png`) |
| `inspect_node` | `inspect-node --node-key KEY` (or `--view-id` / `--semantics-id` / `--bounds x,y,w,h`) |
| `component_image` | `component-image --node-key KEY --out out.png` |
| `capture`, `captures`, `outline`, `find`, `node`, `image`, `lint`, `diff`, `talkback`, `tb_walk`, `tb_scenario` | the same names (`--kebab-case` flags; `tb-walk --prev`, `tb-scenario focus-after`) |
| `detach` | `detach` |

`inspect-node` / `component-image` take the same keys and raise the same errors
(ambiguous or stale key: exit 1 with the candidates) as the MCP tools.

### When to use which

- **MCP** — interactive a11y debugging (the playbook in SKILL.md). Stateful and
  warm: caches the attached session and keeps the agent + skiaparser process
  alive across calls, so dump → lint → overlay → inspect → re-lint is fast.
- **CLI** — one-shot audits, CI/scripts, or producing a `--json` / `--overlay`
  artifact for a bug report. Every call reconnects, so it is slower for
  iterative loops.

---

## Practice corpus: A11yProbe

`testapps/a11yprobe` (package `com.oberkfell.a11yprobe`) pairs a GOOD and a
deliberately BAD variant of each defect, so you can watch every tool find it.
Install with `scripts/install-a11yprobe.sh <serial>`, then open a scenario:

```
# Compose GOOD/BAD pairs (icon_button, touch_target, text_contrast, traversal, lazy_list, ...; "all")
adb -s <serial> shell am start -S -W -n com.oberkfell.a11yprobe/.MainActivity --es scenario icon_button
# Classic-View GOOD/BAD pairs (a ScrollView of XML Views)
adb -s <serial> shell am start -S -W -n com.oberkfell.a11yprobe/.ViewScenarioActivity
# Mixed View/Compose hierarchies and dialog windows:
#   S1 RecyclerView of ComposeView cells   S2 of View cells   S3 mixed (Compose, View, hybrid) cells
#   S4 LazyColumn with AndroidView rows    S5 ComposeView > AndroidView > RecyclerView > ComposeView cells
#   S6 RecyclerView grid                   D1 DialogFragment (Views + a ComposeView)   D2 Compose Dialog
adb -s <serial> shell am start -S -W -n com.oberkfell.a11yprobe/.InteropActivity --es scenario S3
```
