# Inspector Widget — tools (MCP) and CLI reference

The Inspector Widget MCP server (codename `viewspector`, at `host/mcp_server.py`)
exposes 18 tools. The host CLI (`host/cli.py`) mirrors them in 16 subcommands
for scripting. This is the reference for the accessibility workflow plus the
View/Compose tools you may reach for, and the MCP↔CLI mapping.

---

## MCP tools

All take `package` (except `list_devices` / `list_processes`) and an optional
`serial` (default: `$ANDROID_SERIAL`, else the only attached device). All
auto-attach, and re-attach on their own if the agent went away. Images are
written to temp PNG files (deleted when the MCP server exits) and the **path**
is returned (not inlined). A failed call returns `{error, hint?}`.

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
  include_rendering_info=false)`** → unified `AccessibilityNodeInfo` tree (Views +
  Compose virtual nodes) with text/contentDescription/stateDescription/role,
  state flags, bounds, decoded actions, collection/range info, a `node_key` per
  node, plus the host-computed TalkBack `focus_order` (`[{order, key, speak}]`:
  each focus stop and what TalkBack announces there), `reading_order_diagnostics`
  and a `generation`. The order is over the tree TalkBack sees: Views not
  important for accessibility carry `ignored` (`not_important`: their children are
  read in their place; `hidden`: a noHideDescendants subtree) and are no stops;
  each window carries `window_type` / `window_flags` / `modal`, and a window under
  an open modal dialog carries `covered_by` and has no stops.
- **`a11y_lint(serial, package, include_contrast=true, scale=1.0,
  wcag_mode=false, rules=[...], include_rendering_info=true)`** → `{summary,
  findings:[{rule, alias, severity, node_key, node, bounds, bounds_dp, window,
  collection, message, evidence}], diagnostics, stats, density, font_scale,
  generation, ...}`. The DETECT and VERIFY engine, run over the unified a11y tree
  (Views + Compose). `window.covered_by` marks a finding under an open dialog.
  `rules` runs a subset (ids, `R1`..`R18` aliases or ATF names; unknown ids are a
  tool error); `wcag_mode` uses 44dp targets; `include_contrast=false` skips the
  pixel rule. See **rules.md**.
- **`a11y_overlay(serial, package, scale=1.0, include_contrast=true,
  wcag_mode=false)`** → `{path, boxes, labels, flagged, flagged_by_bounds,
  summary, ...}`. Screenshot with every a11y node boxed, each focus stop numbered
  and labelled with what TalkBack says, colored by severity (red=error,
  amber=warn, blue=info, green=clean; a finding that maps to no a11y node is a
  dashed box at its own bounds). The SEE step.

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
  `{path, source, window?}` — a cropped PNG of one element (`source`: `skp` |
  `bitmap_crop`, cut from the element's own window, e.g. a dialog). Use when you
  only need the picture.

### Compose / View (context, not a11y-specific)
- **`dump_compose(serial, package, include_semantics=true,
  include_slot_table=true, enable_inspection=false)`** → Compose semantics tree;
  slot-table composables with `file:line` only if inspection is already on or you pass
  `enable_inspection=true`. That hot-reloads and **resets `remember{}` state** (open
  dialogs, typed text, scroll, toggles) and re-mints Compose node ids, so do it before
  reproducing a state-dependent bug, not after. The layer `dump_tree` cannot see.
- **`compose_overlay(serial, package, scale=1.0, all_boxes=false)`** → screenshot
  with every on-screen Compose element boxed (text/role + bounds) + a flat
  on-screen text list.
- **`dump_tree(serial, package, include_properties=false,
  include_resolution_stack=false, include_screenshot=false, scale=1.0)`** → the
  classic Android View hierarchy.
- **`get_properties(serial, package, view_id, include_resolution_stack=false)`** →
  one View's typed attributes (colors `#AARRGGBB`, resources resolved,
  `is_layout` marked).
- **`screenshot(serial, package, scale=1.0)`** → `{path, width, height}`.
- **`inspect(serial, package, include_properties=false, include_overlay=false)`** →
  whole-screen merged view+compose+a11y model with per-node correlation and a
  `summary.generation`; can render the integrated overlay (every window
  composited; box colour = correlation: green `exact`, amber `overlap` (label
  `a11y~IoU`), grey `none`).

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
```

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
