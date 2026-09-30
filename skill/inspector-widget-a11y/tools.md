# Inspector Widget — tools (MCP) and CLI reference

The Inspector Widget MCP server (codename `viewspector`, at `host/mcp_server.py`)
exposes 15 tools. The host CLI (`host/cli.py`) mirrors the most useful ones for
scripting. This is the reference for the accessibility workflow plus the
View/Compose tools you may reach for, and the MCP↔CLI mapping.

---

## MCP tools

All take `serial` + `package` (except `list_devices`). All auto-attach. Images
are written to temp PNG files and the **path** is returned (not inlined).

### Discovery / session
- **`list_devices()`** → `{devices:[{serial, api, abi, model, state}], count}`.
- **`list_processes(serial)`** → `{processes:[{package, pid, running}], count}` —
  debuggable apps only, running first.
- **`attach(serial, package)`** → `{attached, api_level, abi, agent_version,
  window_count, session}`. Optional warm-up; idempotent.
- **`detach(serial, package)`** → `{detached}`. Ends the session, frees the
  device. Safe even if not attached.

### Accessibility (the core of this skill)
- **`dump_accessibility(serial, package, include_extras=true,
  include_rendering_info=false)`** → unified `AccessibilityNodeInfo` tree (Views +
  Compose virtual nodes) with text/contentDescription/stateDescription/role,
  state flags, bounds, decoded actions, collection/range info, plus the
  host-computed TalkBack `focus_order` (reading order).
- **`a11y_lint(serial, package, include_contrast=true, scale=1.0,
  wcag_mode=false, rules=[...])`** → `{summary, findings:[{rule, severity, node,
  bounds, bounds_dp, message, evidence}], density, font_scale, ...}`. The DETECT
  and VERIFY engine. `rules` runs a subset; `wcag_mode` uses 44dp targets;
  `include_contrast=false` skips the pixel rule. See **rules.md**.
- **`a11y_overlay(serial, package, scale=1.0, include_contrast=true,
  wcag_mode=false)`** → `{path, boxes, labels, flagged, summary, ...}`. Screenshot
  with every a11y node boxed + speakable label + reading-order number, colored by
  severity (red=error, amber=warn, blue=info, green=clean). The SEE step.

### Per-element dossier / image
- **`inspect_node(serial, package, node_key|view_id|semantics_id|bounds,
  include_image=true)`** → dossier `{node_key, bounds, correlation_confidence,
  view?, compose?, a11y?, component_image{path}, lint[]}`. `compose` carries the
  **source `file:line`** + modifiers; `view` carries typed properties; `a11y` is
  the element's node; `lint` is the findings focused to this element. The LOCATE
  step. Selectors: `node_key` = `"view:<id>"`|`"compose:<id>"`; `view_id` =
  uniqueDrawingId; `semantics_id` = Compose id; `bounds` = `{x,y,w,h}` px
  (deepest covering element).
- **`component_image(serial, package, node_key|view_id|semantics_id|bounds)`** →
  `{path, source}` — a cropped PNG of one element (`source`: `skp` |
  `bitmap_crop`). Use when you only need the picture.

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
  whole-screen merged view+compose+a11y model with per-node correlation; can
  render the integrated overlay.

---

## CLI (host/cli.py)

Stateless — each call re-injects and tears down. Run from the project root
(adds its own dir to `sys.path`). Defaults: `--serial emulator-5554`.

```
python host/cli.py devices
python host/cli.py packages   --serial SERIAL
python host/cli.py attach     --serial SERIAL --package PKG
python host/cli.py dump       --serial SERIAL --package PKG [--json -] [--screenshot out.png] [--properties]
python host/cli.py compose    --serial SERIAL --package PKG [--json -] [--overlay out.png] [--all-boxes]
python host/cli.py a11y       --serial SERIAL --package PKG [--json -] [--overlay out.png] [--lint] [--wcag] [--no-contrast]
python host/cli.py a11y-lint  --serial SERIAL --package PKG [--json -] [--rule RULE_ID]... [--overlay out.png] [--wcag] [--no-contrast]
```

### MCP ↔ CLI mapping

| MCP tool | CLI equivalent |
|----------|----------------|
| `list_devices` | `devices` |
| `list_processes` | `packages --serial …` |
| `attach` | `attach --serial … --package …` |
| `a11y_lint` | `a11y-lint …` ( `--rule` per rule, `--wcag`, `--no-contrast`, `--overlay` ) |
| `dump_accessibility` | `a11y …` ( `--json -` for the tree ) |
| `a11y_overlay` | `a11y --overlay out.png --lint` (or `a11y-lint --overlay out.png`) |
| `dump_compose` | `compose --json -` |
| `compose_overlay` | `compose --overlay out.png` |
| `dump_tree` | `dump --json -` |
| `screenshot` | `dump --screenshot out.png` |

The CLI has **no** direct equivalent of `inspect_node` / `component_image` /
`inspect` / `get_properties` as standalone subcommands — use the MCP for the
per-element dossier and the integrated tree.

### When to use which

- **MCP** — interactive a11y debugging (the playbook in SKILL.md). Stateful and
  warm: caches the attached session and keeps the agent + skiaparser process
  alive across calls, so dump → lint → overlay → inspect → re-lint is fast.
- **CLI** — one-shot audits, CI/scripts, or producing a `--json` / `--overlay`
  artifact for a bug report. Stateless, so slower for iterative loops.
