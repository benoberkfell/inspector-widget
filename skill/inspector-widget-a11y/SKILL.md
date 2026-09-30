---
name: inspector-widget-a11y
description: >-
  Debug and fix Android accessibility issues on a running app using the Inspector
  Widget MCP tools. Use when asked to debug Android accessibility, fix a11y issues,
  improve TalkBack / screen reader support, add or correct contentDescription,
  enlarge touch targets, fix color contrast, set semantics roles / headings /
  state descriptions, or check reading (focus) order on an Android app — for both
  Jetpack Compose and classic View UIs. Drives a live device/emulator via the
  Inspector Widget MCP server (codename viewspector): dump the accessibility tree,
  run the a11y lint, render an annotated overlay, open a per-element dossier
  (a11y node + View/Compose facets + where it lives + its findings + component
  image), propose the concrete Compose/View fix, then re-lint to verify the
  finding cleared.
---

# Inspector Widget — Android Accessibility Debugging

This skill is the **methodology**. The Inspector Widget MCP server is the
**capability**. The MCP gives you tools that read a *live* Android app
(device or emulator) over adb; this skill tells you how to wield them to find,
fix, and verify accessibility defects.

You drive a running, **debuggable** app. The MCP injects an inspection agent
into the process and speaks to it. It can see what TalkBack sees (the unified
`AccessibilityNodeInfo` tree), the Jetpack Compose semantics + slot table (with
`file:line` for composables once Compose inspection is enabled), classic View
properties, and the rendered pixels. Views and Compose are one tree, whatever the
mix: Fragments, RecyclerView cells that are ComposeViews, plain Views or both,
`AndroidView` inside Compose, dialogs and popups.

---

## 0. When to use this skill

Trigger this for any request to **debug or fix accessibility on an Android app**:
"TalkBack reads this wrong", "this button has no label", "the tap target is too
small", "contrast is failing", "add contentDescription", "fix the reading order",
"make this screen accessible", "run an a11y audit". It works for Compose and for
View-based UIs.

You need: a connected device/emulator (`adb devices`), the target app **running**
and **debuggable** (`android:debuggable="true"`, i.e. a debug build), and the
Inspector Widget MCP registered (see §7). If the MCP is not registered, register
it first — the whole playbook depends on it.

---

## 1. The playbook: detect → fix → verify

The loop is always the same. Find the issues with data, *see* them on a
screenshot, open a dossier per finding so you know exactly which element (window,
list row, ComposeView, View class / Compose semantics) to change, propose the
concrete fix, then re-lint after the rebuild to confirm it cleared.

```
 list_devices ─▶ list_processes ─▶ attach
        │
        ▼
  dump_accessibility + a11y_lint      ← DETECT (what / where / why)
        │
        ▼
  a11y_overlay                        ← SEE  (boxes + labels + reading order)
        │
        ▼
  inspect_node  (per finding)         ← LOCATE (facets + where + findings + image)
        │
        ▼
  propose Compose / View fix          ← FIX  (you write the change)
        │
     (dev rebuilds & redeploys)
        │
        ▼
  a11y_lint  (same rule)              ← VERIFY (finding cleared?)
```

### Step 1 — Discover and attach

1. `list_devices` → choose a `serial` (e.g. `emulator-5554`).
2. `list_processes(serial)` → choose a running, debuggable `package`. Only
   debuggable apps appear; running apps are listed first.
3. `attach(serial, package)` → confirms the agent is live and reports
   `pid`, `api_level`, `abi`, `agent_version`, `window_count`. This is optional —
   every dump/lint/overlay tool **auto-attaches** — but calling it once up front
   surfaces connection problems early and warms the session.

The session is cached per `(serial, package)`, and the MCP keeps the injected
agent **and the skiaparser process warm** across calls, so the second and later
tools are fast. Pin the `serial` and `package` you chose and reuse them for
every subsequent call this session.

### Step 2 — DETECT: dump the a11y tree and run the lint

Run two tools:

- **`dump_accessibility(serial, package)`** — the unified accessibility tree
  exactly as TalkBack / UiAutomator see it. Both classic Views and Compose
  virtual nodes in one tree. Each node carries text / contentDescription /
  stateDescription / role, all a11y state flags, on-screen bounds, decoded
  actions (CLICK, SCROLL_FORWARD, SET_PROGRESS, …), and collection/range info.
  It also returns the host-computed **TalkBack reading order** (`focus_order`:
  `[{order, key, speak}]`, one entry per focus stop with what TalkBack
  announces there, e.g. `"Delete, button"` or `"Unlabeled, checkbox, not
  checked"`), built from the accessibility child order plus
  `traversalBefore`/`traversalAfter` over the tree TalkBack actually gets: a View
  that is not important for accessibility (a ScrollView's plain `LinearLayout`, a
  decorative `ImageView`) is skipped and its children read in its place (the node
  carries `ignored`), and while a modal dialog is open the windows under it are
  unreachable (`windows[i].covered_by`) and have no stops.
  `reading_order_diagnostics` reports cycles, dangling targets and covered
  windows. Every node has a `node_key` you can pass to `inspect_node`, and the
  dump has a `generation` that changes when Compose re-mints its ids. Read this
  to understand what gets announced and in what order.
- **`a11y_lint(serial, package)`** — the rule engine (R1..R18). It lints the
  same unified tree as `dump_accessibility`, so classic View screens, Compose,
  RecyclerView cells, AndroidView-in-Compose and dialogs are all covered in one
  call. Returns `findings[]`, each with a `rule` id and `alias` (`R1`..),
  `severity` (`error` | `warn` | `info`), a typed `node_key`
  (`view:<id>` / `compose:<acvId>:<semId>`), the `node` (label, role, class,
  testTag, source), `bounds` (px) and `bounds_dp` (dp), `window`, `collection`
  (list/row position; `window.covered_by` when the window is under an open
  dialog), a remediation `message`, and `evidence`. Also a `summary` (counts by
  severity and by rule), the dump's `generation` and `diagnostics`: read them
  before trusting a clean result. Rules judge what TalkBack reads: Views TalkBack
  never sees are skipped, and focus stops come from the reading order. By default
  it screenshots each window to run the one contrast rule; pass
  `include_contrast=false` to skip it (tree-only rules still run).

Read the lint `summary` first to triage: fix **errors** before **warns** before
**info**. Group findings by `rule` so you apply one canonical fix pattern across
all instances. The full catalogue of rules, what each detects, and the canonical
remediation is in **[rules.md](rules.md)** — keep it open while you work.

Useful `a11y_lint` arguments:

- `rules: ["a11y.label.missing", ...]` — run only a subset (great for the
  VERIFY step: re-run just the rule you fixed). Aliases (`"R1"`) and ATF check
  names (`"TouchTargetSize"`) work too; an unknown id is an error listing the
  valid ones.
- `wcag_mode: true` — use WCAG target sizes (44dp) instead of Material (48dp)
  for the touch-target rule.
- `include_contrast: false` — skip the pixel-sampling contrast rule when you
  only care about structural issues, or when no screenshot is wanted.

### Step 3 — SEE: render the annotated overlay

**`a11y_overlay(serial, package)`** screenshots the app and draws **every**
accessibility node as a labeled box with its speakable label and the computed
TalkBack reading-order number, color-coded by lint severity:

- **red** = error, **amber** = warn, **blue** = info, **green** = clean.
- a **dashed** box (in the severity colour, labelled with the rule) = a finding
  that maps to no a11y node, drawn at the finding's own bounds.
- Every window is drawn, a dialog over its activity; the numbers are the
  reading order, so the activity under an open dialog has none.

It returns the annotated PNG `path` plus the lint `summary`. **Always view this
image.** It is the single best "show me the a11y problems on screen" view: it
turns the abstract findings into a picture, makes the reading order visible
(catching jumps/loops), and lets you point a developer (or yourself) at exactly
which on-screen element each finding refers to. For Compose-content questions
without the a11y coloring, `compose_overlay` boxes every on-screen Compose
element with its text/role.

### Step 4 — LOCATE: open the per-element dossier

For each finding you intend to fix, call **`inspect_node`** to get the full
element dossier. Select the node by whichever id you have from the lint /
overlay / a11y dump:

- `node_key` — `"view:<uniqueDrawingId>"` or `"compose:<acvId>:<semanticsId>"`
  (exactly the finding's `node_key`; every key `dump_accessibility`, `a11y_lint`
  and `inspect` hand out resolves, including the Text/Icon children Compose merges
  into a button, which come back `a11y_only` with their `a11y_parent`)
- `view_id` — a View's `uniqueDrawingId`
- `semantics_id` — a Compose node's semantics id
- `bounds` — `{x, y, w, h}` in screen px (resolves to the deepest covering
  element; handy straight from a finding's `bounds`)

`inspect_node` returns a dossier with the facets that exist for that element:

- **`compose`** — the Compose semantics node (`attrs`: Role, TestTag, Text,
  ContentDescription, …). Its `source` is **null** in the dossier: `file:line`
  only exists in the slot table, which `inspect_node` does not fetch. To find the
  line, run `dump_compose(include_slot_table=true, enable_inspection=true)` (see
  §5 for the cost) and match the composable to the node by `testTag` or bounds.
- **`view`** — full View attributes + `properties` (typed: colors as
  `#AARRGGBB`, resources resolved, `is_layout` marked) for classic Views.
- **`a11y`** — the element's full `AccessibilityNodeInfo` (label, role, state,
  actions) — what TalkBack actually announces for it.
- **`component_image.path`** — a cropped PNG of *just that element* (SKP cut by
  the Compose graphicsLayer when available, else a bitmap crop of the element's
  own window, so a dialog element is cut from the dialog). View it to confirm you
  are fixing the right thing and to eyeball contrast.
- **`lint`** — exactly the `a11y_lint` findings for this node and the nodes
  Compose merged into it (same rules, contrast included), with `lint_summary` and
  `lint_diagnostics` (e.g. contrast not sampled). `correlation_confidence`
  (`exact` | `overlap` | `none`) says how solid the View↔Compose↔a11y
  correlation is.
- **`where` / `context`** — window > list row > ComposeView > AndroidView host.
- **`resolved_from`** — the key was stale (Compose re-minted ids) and was
  re-resolved by ComposeView, test tag, label, row and bounds; **`key_note`** —
  the key still exists but now names other content (a recycled cell).

With the dossier you have everything to write a precise fix: the **what** (the
rule + the element's current semantics), the **where** (the View's resource id /
class, or the Compose node's testTag, text and position in its window / list row;
`file:line` from the slot table when you need the exact line), and the
**picture** (component image).

If you only need the cropped image (not the whole dossier), call
`component_image` with the same selectors. For a whole-screen correlated model
in one shot, `inspect(serial, package, include_overlay=true)` merges
view+compose+a11y and can render the integrated overlay.

### Step 5 — FIX: propose the concrete change

Using the rule's canonical remediation (**[rules.md](rules.md)**) and the
dossier's location, write the concrete Compose or View change. Be specific —
name the modifier, the file, and the line. Common fixes:

- **Missing label** (`a11y.label.missing`, `a11y.image.no_description`): add a
  `contentDescription` (icon-only `IconButton`/`Icon`/`Image`), or attach visible
  `Text`. For Compose:
  `Modifier.semantics { contentDescription = "Play" }` or the component's
  `contentDescription = "Play"` parameter. For Views: `android:contentDescription`
  / `view.contentDescription = "…"`. A truly decorative image should be
  `contentDescription = null` (and not focusable), or
  `Modifier.semantics { hideFromAccessibility() }` (Compose 1.8+; it replaces the
  deprecated `invisibleToUser()`). For Views,
  `android:importantForAccessibility="no"`.
- **Small touch target** (`a11y.touch_target.small`): grow the *target*, not just
  the padding — `Modifier.sizeIn(minWidth = 48.dp, minHeight = 48.dp)` or
  `Modifier.minimumInteractiveComponentSize()`. (Padding does not enlarge the
  hit/target rect.)
- **Missing role** (`a11y.role.missing_on_clickable`): use a real `Button` /
  `IconButton`, or `Modifier.semantics { role = Role.Button }`.
- **State not exposed** (`a11y.state.not_exposed`): make it `Modifier.toggleable`
  / `selectable`, or set
  `Modifier.semantics { stateDescription = if (on) "On" else "Off" }`.
- **Missing / broken headings** (`a11y.heading.structure`): mark section titles
  with `Modifier.semantics { heading() }`.
- **Redundant label** (`a11y.label.redundant`): drop the duplicated
  `contentDescription` (let the visible Text speak), or remove the type word —
  the Role already announces "Button"/"Image"; say "Submit", not "Submit button".
- **Low contrast** (`a11y.contrast.low`): darken text or lighten background to
  reach ≥ 4.5:1 (normal) / ≥ 3:1 (large ≥ ~18pt). The ratio is sampled from real
  rendered pixels, so it reflects the actual theme.
- **Empty focusable** (`a11y.node.empty_focusable`): give it content/label, or
  remove it from the a11y tree with `Modifier.clearAndSetSemantics {}` (Compose)
  / `importantForAccessibility = no` (View).
- **Missing grouping** (`a11y.grouping.missing`): wrap the row in
  `Modifier.semantics(mergeDescendants = true) {}` (or a single clickable parent)
  so TalkBack announces it as one stop.
- **Duplicate labels** (`a11y.duplicate.label`): disambiguate ("Open settings"
  vs "Open profile") or group for context.
- **Fixed text scaling** (`a11y.text.fixed_scaling`): size text in `sp`
  (`fontSize = 16.sp`), not `dp`/`px`, so it honors the user's font-scale.
- **ATF-style checks** (R13–R18: duplicate clickable bounds, contentDescription
  on a text field, unclear link text, unlabeled form field, traversal cycles,
  tiny text): see [rules.md](rules.md) for each fix.

Each finding's `message` already contains the targeted remediation for that exact
node — quote and adapt it. You generally do **not** apply the source edit
yourself unless asked; you propose it precisely. The developer rebuilds and
redeploys.

### Step 6 — VERIFY: re-lint the finding

After the app is rebuilt and the new build is running on the device, re-run the
lint **scoped to the rule(s) you addressed** and confirm the finding is gone:

```
a11y_lint(serial, package, rules=["a11y.label.missing"])
```

Check the `summary.by_rule` count for that rule dropped (ideally to 0 for the
elements you fixed) and that no element you touched still appears in `findings`.
For a visual gut-check, re-run `a11y_overlay` and confirm the previously-red box
is now green. If the finding persists, re-open `inspect_node` on it: the dossier
will show whether the new semantics actually landed (e.g. the
`contentDescription` now present in the `a11y` / `compose` facet) or whether the
fix went to the wrong element.

Do not declare success on a stale dump — the lint reads the *live* app, so it
must be re-run against the **rebuilt** process. (If the package was reinstalled,
the session re-attaches automatically on the next call.)

---

## 2. Reading the lint findings

Every finding has the same shape:

```json
{
  "rule": "a11y.touch_target.small", "alias": "R2",
  "severity": "warn",
  "node_key": "compose:1234:42",
  "node": { "id": 5299989643306, "key": "compose:1234:42", "name": "IconButton",
            "role": "Button", "label": "Play", "test_tag": "play", "source": null },
  "bounds": { "x": 24, "y": 880, "w": 96, "h": 96 },
  "bounds_dp": { "x": 9.1, "y": 335, "w": 36.6, "h": 36.6 },
  "window": { "index": 0, "root_view_id": 77 },
  "collection": null,
  "message": "Touch target is 36.6x36.6dp (< 48dp). Make the touchable area …",
  "evidence": { "w_dp": 36.6, "h_dp": 36.6, "min_dp": 48, "standard": "material" }
}
```

- **`severity`** drives priority: errors are real breakage (unlabeled actionable
  element, clickable image with no description, contrast far below the floor);
  warns are likely defects; infos are structure/quality nudges.
- **`node_key`** is the selector you feed to `inspect_node(node_key=...)`:
  `view:<id>` for a View, `compose:<acvId>:<semId>` for a Compose node. Compose
  keys go stale when the UI recomposes (the response's `generation` changes);
  `inspect_node` re-resolves a key the lint handed out when it can do so
  unambiguously, otherwise re-lint. The finding's `bounds` also work as a
  selector. `node.id` is the a11y node id used
  by `dump_accessibility` and the overlay.
- **`message`** is the canonical remediation for *that* node — it already names
  the modifier/attribute and the corrected value. Use it verbatim as the basis
  of your proposed fix.
- **`evidence`** is the proof: the measured contrast ratio and sampled fg/bg
  hexes, the dp dimensions vs the required minimum, the matched type-word, etc.
  Cite it so the fix is justified, not asserted.

The complete rule reference — id, what it flags, severity logic, and the exact
fix — is in **[rules.md](rules.md)**.

---

## 3. MCP vs CLI: when to use which

**Prefer the MCP for interactive a11y debugging.** It is stateful and *warm*:
it caches the attached session per `(serial, package)` and keeps both the
injected agent and the skiaparser subprocess alive across calls, so the
dump → lint → overlay → inspect → re-lint loop stays fast and you never re-pay
injection cost between steps. The MCP also materializes images to PNG paths and
shapes everything into agent-friendly JSON. This is the default for everything
in this skill.

**Use the CLI (`host/cli.py`) when** you are *not* in an MCP-driven loop:

- scripting / CI / a one-shot audit from a shell;
- producing a JSON or overlay artifact to attach to a bug report
  (`--json out.json`, `--overlay out.png`);
- quick local sanity checks without an MCP client.

Each CLI invocation is its own process: it connects to the agent (reusing one
already loaded in the app, else injecting it) and disconnects when it is done.
Only the Compose key registry outlives it (a small file per app process), so a
`compose:` key from `a11y-lint` still resolves in a later `inspect-node`. That
makes it slower for iterative work but perfect for a single deterministic
command. The 16 subcommands mirror the tools: `a11y-lint` (≈ `a11y_lint`), `a11y`
(dump + `--overlay`/`--lint` ≈ `dump_accessibility` + `a11y_overlay`),
`inspect-node`, `component-image`, `inspect`, `compose`, `dump`,
`get-properties`, `screenshot`, `devices`, `packages`, `attach`, `detach`,
`talkback`, `tb-walk`, `tb-scenario`. See
**[tools.md](tools.md)** for the full MCP↔CLI mapping and exact invocations.

---

## 4. A complete example session

```
1.  list_devices()                                 → serial = emulator-5554
2.  list_processes("emulator-5554")                → package = com.example.player
3.  attach("emulator-5554", "com.example.player")   → agent live, 1 window
4.  a11y_lint(serial, package)                      → 2 error, 3 warn, 1 info
      • a11y.label.missing  compose:1234:42  (play/pause IconButton)
      • a11y.contrast.low   compose:1234:51  (caption, 2.9:1)
5.  a11y_overlay(serial, package)                   → view PNG: that box is RED
6.  inspect_node(serial, package, node_key="compose:1234:42") → compose.attrs:
      Role=Button, TestTag=play_pause; a11y label empty; where: … > compose:1234:42
      Button; component_image shows a bare ▶ glyph
7.  (optional) dump_compose(include_slot_table=true, enable_inspection=true) and find
      the composable with testTag play_pause → source Player.kt:88
8.  PROPOSE: in Player.kt:88, add contentDescription = if (playing) "Pause" else "Play"
      to the IconButton's Icon (or Modifier.semantics { contentDescription = … }).
   (developer rebuilds + redeploys)
9.  a11y_lint(serial, package, rules=["a11y.label.missing"]) → 0 findings ✓
10. a11y_overlay(serial, package)                   → that box is now GREEN ✓
11. detach(serial, package)                         → free the device session
```

---

## 5. Tips, gotchas, hygiene

- **App must be running and debuggable.** Release builds and non-running apps
  cannot be attached. If `attach` fails, launch the app and confirm it is a
  debug build.
- **Re-lint against the rebuilt app**, not a stale dump. The tools read the live
  process; verification only counts after redeploy.
- **Pixel rules need pixels.** `a11y.contrast.low` only runs when screenshots are
  sampled (default on, one per window). Tree-only rules run regardless. The sample
  is fast at `scale=1.0`; a lower `scale` only loses precision on thin text.
- **Text size needs rendering info.** The text-size rules (R11, R18) and
  large-text contrast use View ExtraRenderingInfo, which the lint requests by
  default. Compose text never reports a size, so Compose contrast uses the
  normal-text threshold and a `diagnostics` entry says so.
- **Off-screen / below-the-fold nodes are skipped** by the lint (not visible to
  the user → no meaningful touch target or pixels). A row cut off at a scroll edge
  gets an `info` touch-target finding saying it is probably clipped. Scroll the
  content into view, then re-lint.
- **Compose `file:line` comes from the slot table** (`dump_compose` with
  `include_slot_table=true`), never from `inspect_node` or the lint (their
  `source` is null). The slot table is empty until Compose inspection is
  enabled: `dump_compose(enable_inspection=true)` (CLI
  `compose --enable-inspection`). That hot-reloads every composition and **resets
  `remember{}` state** (open dialogs, typed text, scroll, toggles) and re-mints
  Compose node ids, so enable it before reproducing a state-dependent bug, then
  re-dump. If the slot table is still empty after that, the app may be a
  minified/release build.
- **Compose keys go stale.** Recomposition, list recycling and navigation re-mint
  semantics ids; the `generation` of each dump says when. `inspect_node`
  re-resolves a key it (or `dump_accessibility` / `a11y_lint`) saw before, and
  refuses to guess on a weak or tied match: re-run the lint and use the fresh key,
  or pass the finding's `bounds` too.
- **Views TalkBack never sees are skipped.** A View left at
  `importantForAccessibility="auto"` that is not clickable/focusable and has no
  text or description (layout containers, decorative icons) is not important for
  accessibility; the dump marks it `ignored` and neither the reading order nor
  the lint count it. Its children still count.
- **Dialogs.** While a modal dialog is open TalkBack cannot reach the activity
  under it: its window has `covered_by`, no reading order, and its findings carry
  `window.covered_by` (still real defects, reachable once the dialog closes).
- **Material 48dp vs WCAG 44dp.** Default touch-target floor is Material 48dp;
  pass `wcag_mode=true` for the WCAG 2.5.8 44dp target (24dp is the hard floor).
- **`detach` when done** to free device resources. It is safe to call even if
  not attached.

---

## 6. Tool quick reference

The accessibility-focused tools (full list + the View/Compose tools in
**[tools.md](tools.md)**):

| Tool | Use it to |
|------|-----------|
| `list_devices` | discover serials |
| `list_processes` | find a running, debuggable package |
| `attach` | open/warm a session (optional; tools auto-attach) |
| `dump_accessibility` | read the unified a11y tree + TalkBack reading order |
| `a11y_lint` | get rule findings (DETECT / VERIFY) |
| `a11y_overlay` | see findings + reading order on the screenshot |
| `inspect_node` | per-element dossier: facets + where + its findings + image |
| `component_image` | just the cropped image of one element |
| `inspect` | whole-screen merged view+compose+a11y model |
| `compose_overlay` | see on-screen Compose text/role boxes |
| `detach` | end the session |

---

## 7. Register the Inspector Widget MCP

Register the server once so the tools above are available. Use the host venv
python (so protobuf / the mcp SDK are on the path), or set `PYTHONPATH` to the
`host/` directory and run with any python that has the deps. The codename is
`viewspector`; the import package is `inspector_widget`; the server lives at
`host/mcp_server.py`.

Register with the helper — it fills in this checkout's absolute paths for you
(run from the repo root):

```
./scripts/register-mcp.sh
```

Or register manually, **from the repo root** so `$PWD` expands to your checkout
(use the venv python so protobuf + the mcp SDK are on the path):

```
claude mcp add inspector-widget -- env PYTHONPATH="$PWD/host" "$PWD/host/.venv/bin/python" "$PWD/host/mcp_server.py"
```

Verify the server and its tool surface without a device:

```
claude mcp list
host/.venv/bin/python host/mcp_server.py --self-check
```

The MCP server runs even without the `mcp` SDK installed (it falls back to a
self-contained JSON-RPC-over-stdio implementation), but installing the deps from
'host/requirements.txt' is recommended. Build artifacts must be present in
'build-out/' (run 'scripts/build.sh'), or in the directory named by
INSPECTOR_WIDGET_ARTIFACTS (needed for a wheel install; '--self-check' shows
which directory it resolved), and adb must be on PATH with a device connected.
