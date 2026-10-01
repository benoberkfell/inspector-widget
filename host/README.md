# Inspector Widget — Host driver + MCP server

`host/` is the Python side of Inspector Widget. It contains:

- **`inspector_widget/`** — the host driver package. Discovers devices/processes
  over `adb`, injects the native agent + bootstrap dex + payload jar into a
  running debuggable app (`cmd activity attach-agent`), forwards the agent's
  abstract socket, and speaks the framed protobuf protocol from
  `proto/view_inspection.proto`. (Built by the host-driver module.)
- **`mcp_server.py`** — an [MCP](https://modelcontextprotocol.io) server that
  exposes Inspector Widget to an LLM agent as 26 tools (18 listed by default:
  the 15 inspection tools and the 3 TalkBack tools; the 8 capture-and-walk
  tools with `INSPECTOR_WIDGET_TOOLSET=capture` or `all`; the TalkBack tools in
  their capture shape with `capture,talkback`, `talkback` or `all`), built on
  top of `inspector_widget`.

The wire protocol, packages, socket names, and screenshot encoding are fixed by
[`../CONTRACT.md`](../CONTRACT.md). The MCP server only orchestrates the driver,
caches sessions, materialises screenshots to PNG, and shapes the protobuf
responses into agent-friendly JSON (string-table ids resolved to text).

---

## 1. Build

The device artifacts (native `.so`, `bootstrap.dex`, `payload.jar`) are
produced by the top-level build script:

```bash
# from the repo root
scripts/build.sh
```

`scripts/build.sh` builds `libviewspector.so`, `bootstrap.dex` and
`payload.jar` into `build-out/`, plus `BUILD_ID` (the sha256 of `payload.jar`).
A running agent reports the same hash in Hello (`agent_version` is
`viewspector-0.1+<sha256>`); when it differs from the local `payload.jar`
(you rebuilt), the host stops the old agent and injects the new one.

The Python protobuf bindings (`host/inspector_widget/proto/view_inspection_pb2.py`,
imported via `from .proto import view_inspection_pb2`) are checked in. After
changing `proto/view_inspection.proto`, regenerate them with
`host/generate_proto.sh` (or `make -C host proto`). It requires **protoc 33.x**,
the release the checked-in gencode and the `protobuf>=6.33.5,<7` runtime pin
expect, and refuses any other major (protoc 34+ emits 7.x gencode that the pinned
runtime can't import). `PROTOC="python -m grpc_tools.protoc"` with
`grpcio-tools==1.81.0` provides protoc 33.5 without a system install.

### Python environment

The host needs Python 3.10+ with `protobuf` (and, for the preferred MCP
transport, the `mcp` SDK):

```bash
cd host
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` pins `protobuf` and `mcp`. The MCP server runs **without**
the `mcp` package too — it falls back to a self-contained JSON-RPC-over-stdio
MCP implementation — but installing `mcp` is recommended.

### Verify the install

```bash
python host/mcp_server.py --self-check
```

This prints the tool surface, whether `inspector_widget`,
`view_inspection_pb2`, and the optional dependencies are importable, and where the
device artifacts are looked up (a missing artifact is a warning, not a failure). Example:

```
Inspector Widget MCP server — self check
  toolset: legacy,talkback (18 listed; set INSPECTOR_WIDGET_TOOLSET = legacy, capture, talkback, all or a comma list)
  tools (18): list_devices, list_processes, attach, dump_tree, get_properties, screenshot, dump_compose, compose_overlay, dump_accessibility, a11y_lint, a11y_overlay, detach, inspect, inspect_node, component_image, talkback, tb_walk, tb_scenario
  not listed, callable by name (8): capture, captures, outline, find, node, image, lint, diff
  inspector_widget: OK
  view_inspection_pb2: OK
  mcp SDK: 1.30.0 OK (real MCP transport)
  Pillow: 12.3.0 OK
  grpcio: 1.84.0 OK
  artifacts: /path/to/inspector-widget/build-out (from default)
    libviewspector.so: OK
    bootstrap.dex: OK
    payload.jar: OK
    build id: 0ba5e3f7633ef8fc14c17052bda5321d21b1f1b858210bfdbd443dd05aba3b24
```

---

## 2. Run the MCP server

The server speaks MCP over **stdio** (stdin/stdout). stdout is reserved for the
protocol; all logs go to stderr.

```bash
# preferred: from the host venv that has protobuf (+ mcp) installed; run from the repo root
host/.venv/bin/python host/mcp_server.py --self-check
```

Useful flags / env:

- `--self-check` — print the tool surface and dependency status (mcp SDK, Pillow, grpcio,
  with the tools each missing one degrades) and where the device artifacts were
  found, then exit. Exits 1 if the host package, the proto gencode, or the installed
  mcp SDK is broken; missing artifacts are only a warning.
- `--log-level DEBUG|INFO|WARNING|ERROR` (or `INSPECTOR_WIDGET_LOG=DEBUG`) — log
  verbosity (to stderr).
- `INSPECTOR_WIDGET_ARTIFACTS=DIR` — where to read `libviewspector.so`,
  `bootstrap.dex` and `payload.jar` (legacy `VIEWSPECTOR_ARTIFACTS` still works).
  Defaults to the checkout's `build-out/`; **required after a wheel install**,
  where the package lives in site-packages. The CLI's `--build-out DIR` is the
  per-command equivalent.
- `ANDROID_SERIAL=SERIAL` — the device to use when a tool gets no `serial` (the
  CLI: no `--serial`). Without it, the only attached device is used; with
  several, the call fails and lists them.
- `INSPECTOR_WIDGET_TIMEOUT=SECONDS` — per-request deadline for the agent
  (default 30; screenshots, SKP capture, Compose/a11y dumps and `dump_tree`
  with properties or a screenshot get 4x). A call that runs out returns an
  error with a `hint` instead of hanging on a frozen app. `0` disables it (for
  an app paused at a breakpoint). Legacy `VIEWSPECTOR_TIMEOUT` still works.

Prerequisites at runtime:

- `adb` on `PATH` and a device/emulator connected.
- The device artifacts built by `scripts/build.sh`, in `build-out/` or wherever
  `INSPECTOR_WIDGET_ARTIFACTS` points.
- The target app **running** and **debuggable** before `attach`/`dump_tree`.

---

## 3. Register with Claude (`claude mcp add`)

The simplest way is the helper, which fills in this checkout's absolute paths so
you never hand-edit one — re-run it any time (e.g. after moving the repo):

```bash
./scripts/register-mcp.sh        # from the repo root
claude mcp list                  # expect: inspector-widget ... ✓ Connected
```

To register manually, run it **from the repo root** so `$PWD` expands to your
checkout (use the **venv python** so `protobuf`/`mcp` are on the path):

```bash
claude mcp add inspector-widget -- \
  env PYTHONPATH="$PWD/host" "$PWD/host/.venv/bin/python" "$PWD/host/mcp_server.py"
```

Equivalent explicit JSON config (e.g. for `~/.claude.json` / an MCP client's
`mcpServers` map) — replace `<REPO>` with the absolute path to your checkout:

```json
{
  "mcpServers": {
    "inspector-widget": {
      "command": "env",
      "args": [
        "PYTHONPATH=<REPO>/host",
        "<REPO>/host/.venv/bin/python",
        "<REPO>/host/mcp_server.py"
      ],
      "env": {
        "INSPECTOR_WIDGET_LOG": "WARNING",
        "INSPECTOR_WIDGET_ARTIFACTS": "<REPO>/build-out"
      }
    }
  }
}
```

If you installed `protobuf`/`mcp` into your system Python instead of a venv,
use that interpreter as `command`. If you installed the wheel (so the
`inspector-widget-mcp` console script is on `PATH`), register that and tell it
where the artifacts are, since it can't find the checkout on its own:

```bash
claude mcp add inspector-widget -e INSPECTOR_WIDGET_ARTIFACTS="$PWD/build-out" -- inspector-widget-mcp
```

Verify with:

```bash
claude mcp list          # shows "inspector-widget"
```

---

## 4. Tool surface

All tools return JSON. String-table ids from the wire are resolved to text, so
trees and properties are directly readable. Screenshots are written to temp PNG
files and the **path** is returned (the image is not inlined). The PNGs live in
one `inspector-widget-<pid>-*` directory under `$TMPDIR`, deleted when the
server exits; copy a file elsewhere to keep it.

`serial` is optional on every tool: it defaults to `$ANDROID_SERIAL`, else the
only attached device. Arguments are checked against each tool's `inputSchema`
before anything touches the device, the same way on every transport; an
explicit `null` for an optional argument means its default, and a whole-number
float (`12.0`) passes for an integer. `scale` is in (0, 1].

### Output: compact, brief by default, budgeted

Every result leaves through `inspector_widget.output` (Phase 0 of
`docs/design/capture-and-walk.md`), on the MCP and the CLI alike:

- **Compact JSON.** The CLI's `--pretty` indents it for humans.
- **`detail="brief"`** (default) drops what the agent does not need and counts
  every omission (`omitted`, `hidden`, `omitted_defaults`, `hidden_descendants`):
  bounds become `[x,y,w,h]`; properties become `{name: value}` maps (colors
  `#AARRGGBB`, resources `@type/name`) holding only non-default values when a
  tree carries them; a11y nodes drop sentinels and boilerplate actions;
  `focus_order` keeps the stops as `{order, key, speak}`; `a11y_lint` groups its
  findings by rule (count, message, the first three node keys); Compose slot
  tables show app code only (`user_code_only=false` for library composables).
  **`detail="full"`** returns the tool's whole result (the pre-Phase-0 content).
- **`max_depth`** (1 = the roots: `dump_tree(max_depth=1)` lists the window roots)
  and **`root`** (a node id or key from the result) on `dump_tree`,
  `dump_compose`, `dump_accessibility` and `inspect`; `focus_order` (`stops` |
  `full` | `none`), `group_by` (`rule` | `none`) and `filter` (`all` |
  `nondefault`) where they apply.
- **`max_bytes`** (default `$INSPECTOR_WIDGET_MAX_BYTES`, else 32,000; `0` = no
  cap; 1,000..200,000). A larger result is written to a spill file under
  `<store>/spill/` (1 h TTL) and the response is a spill envelope of at most
  3,000 bytes: `{truncated, tool, bytes, max_bytes, summary, preview, spill_path,
  hint}`. `detail="full"` with `max_bytes=0` is the rollback to the legacy output.
  Tools without a `max_bytes` argument (`inspect_node`, the overlays,
  `component_image`, ...) are budgeted at the environment default; their hint
  names the spill file and `INSPECTOR_WIDGET_MAX_BYTES`, never an argument the
  tool would reject.

The CLI takes the same parameters as kebab-case flags with the same defaults
(`--detail`, `--max-bytes`, `--max-depth`, `--root`, `--user-code-only` /
`--no-user-code-only`, `--focus-order`, `--group-by`, `--filter`), and
`--json -` prints the bytes the MCP tool returns for the same arguments.
`--json FILE` writes the whole document and never spills. With
`--detail full` each subcommand prints its own pre-Phase-0 document.

| Tool | Arguments | Returns |
|------|-----------|---------|
| `list_devices` | — | `{devices:[{serial, api, abi, model, state}], count}` |
| `list_processes` | `serial?` | `{serial, processes:[{package, pid, running}], count}` — debuggable packages only; running apps first |
| `attach` | `serial?`, `package`, `force=false` | `{attached, pid, warm, reused, api_level, abi, agent_version, build_id, window_count, root_ids, session, note?}` — injects if needed (idempotent); app must be running. `warm`: the agent was already running; `reused`: this server's cached session was reused; `note` when that session runs an older build. `force=true` stops any running agent and injects a fresh one |
| `dump_tree` | `serial`, `package`, `include_properties=false`, `include_resolution_stack=false`, `include_screenshot=false`, `scale=1.0`, `root_id=0` | `{serial, package, roots:[ViewNode…], root_count, properties?, diagnostics?, screenshot?}` — auto-attaches; brief: `properties` is `{view_id: {name: value}}` (non-default values) plus `omitted_defaults`; `diagnostics` is what the agent cut or could not read (`depth-truncated=N`, `properties-failed=N`), beside each cut node's `CHILDREN_TRUNCATED` flag |
| `get_properties` | `serial`, `package`, `view_id`, `include_resolution_stack=false`, `filter=all` | brief: `{serial, package, view_id, properties:{name: value}}`; full: `{…, group:{view_id, properties:[{name,type,is_layout,value,source?,resolution_stack?}]}}` |
| `screenshot` | `serial`, `package`, `scale=1.0` | `{path, width, height, bytes, scale}` — PNG saved on host |
| `dump_compose` | `serial`, `package`, `include_semantics=true`, `include_slot_table=true`, `enable_inspection=false` (opt-in: hot-reload resets `remember{}` state) | `{windows:[…], diagnostics, note?}` — Compose semantics tree + slot-table composables with `file:line` (the layer `dump_tree` cannot see). An empty slot table gets a `note`: how to populate it (`enable_inspection=true`, with its warning) only on a readable Compose UI; with no ComposeView, or an obfuscated / unreadable Compose (`compose_obfuscated`, `semantics_failed`), the note says why there is no slot table instead (the CLI prints the same) |
| `compose_overlay` | `serial`, `package`, `scale=1.0`, `all_boxes=false` | `{path, boxes, …}` — screenshot with every on-screen Compose element boxed (text/role + bounds) + a flat on-screen text list |
| `dump_accessibility` | `serial`, `package`, `include_extras=true`, `include_rendering_info=false` | unified `AccessibilityNodeInfo` tree (Views + Compose virtual nodes): text/contentDescription/stateDescription/role, state flags, bounds, decoded actions, collection/range info, plus host-computed TalkBack `focus_order` and a `generation`; nodes TalkBack never sees carry `ignored`, windows under a modal dialog `covered_by` |
| `a11y_lint` | `serial`, `package`, `include_contrast=true`, `scale=1.0`, `wcag_mode=false`, `rules=[…]`, `include_rendering_info=true`, `group_by=rule` | brief: `{summary, by_rule:{rule:{sev, n, msg, nodes:[≤3 node keys], more?}}, diagnostics (warn/error), density, font_scale, generation, contrast_sampled, omitted}`; `group_by=none` / full: `{summary, findings:[{rule, alias, severity, node_key, node, bounds, bounds_dp, window, collection, message, evidence}], diagnostics, stats, …}` — the detect/verify engine (R1..R18) over the unified a11y tree (Views + Compose), judging what TalkBack reads; `rules` takes ids, `R#` aliases or ATF names; `wcag_mode` uses 44dp targets; `include_contrast=false` skips the pixel rule |
| `a11y_overlay` | `serial`, `package`, `scale=1.0`, `include_contrast=true`, `wcag_mode=false` | `{path, boxes, labels, flagged, summary, …}` — every window composited (a dialog over its activity), every a11y node boxed + what TalkBack says + reading-order number; red error, amber warn, blue info, green clean, dashed = a finding with no a11y node, drawn at its bounds |
| `inspect` | `serial`, `package`, `include_properties=false`, `include_overlay=false` | whole-screen merged view+compose+a11y model with per-node correlation and `summary.generation`; can render the integrated overlay (all windows; green exact, amber overlap, grey none) |
| `inspect_node` | `serial`, `package`, one of `node_key` (`view:<id>` \| `compose:<acvId>:<semanticsId>` \| `composeview:<acvId>`) \| `view_id` \| `semantics_id` \| `bounds`, `include_image=true` | dossier `{node_key, bounds, correlation_confidence, generation, where, context, view?, compose?, a11y?, list_item?, a11y_only?, a11y_parent?, resolved_from?, key_note?, component_image{path}, lint[], lint_summary, lint_diagnostics}` — `compose` carries the semantics attrs (`source` is null: `file:line` needs `dump_compose` with the slot table), `view` typed properties, `lint` exactly the `a11y_lint` findings for the element and the nodes merged into it |
| `component_image` | `serial`, `package`, one of `node_key` \| `view_id` \| `semantics_id` \| `bounds` | `{path, source, window?, serial, package, node_key}` — cropped PNG of one element, cut from its own window (`source`: `skp` \| `bitmap_crop`); `component-image` prints the same document |
| `talkback`, `tb_walk`, `tb_scenario` | see [TalkBack](#talkback-toolset-talkback) below | the default listing shows them in this pre-capture shape (`package` required, node keys or labels as `start` / `target`); the calls run the capture surface's implementation, so results name capture refs |
| `detach` | `serial?`, `package`, `shutdown=true` | `{detached, agent_stopped, note?}` — `shutdown=true` sends SHUTDOWN, stopping the agent for every client (also one this server didn't attach, or one in the app's new process after a restart; never injects one to stop it); `agent_stopped` is true only once nothing listens on the agent's socket; `shutdown=false` only drops this server's cached connection |

### Capture and walk (toolset `capture`)

`INSPECTOR_WIDGET_TOOLSET` picks what `tools/list` shows: `legacy` (the 15
inspection tools), `talkback` (the 4 session tools and the 3 TalkBack tools),
`capture` (the 4 session tools and the 8 below), `all`, or a comma list. The
default is `legacy,talkback` (18) until the deliberate flip; every tool stays
callable by name. The MCP `instructions` name only listed tools. Compact
`tools/list` sizes: default 18,337 B, `capture` 11,994 (at most 12,000),
`talkback` 5,114, `capture,talkback` 15,522, `all` 27,183 (no listing averages
more than 1,300 B a tool). The same 8 tools (and the 3 TalkBack tools) are CLI
subcommands generated from one registry
(`inspector_widget/surface.py`: same names, kebab-case flags, same defaults;
`--json` prints the MCP text, the human output prints `next` hints as
`inspector-widget ...` commands; a flag value starting with `-`, as in
`--fields -bounds`, is accepted).

`capture` snapshots the app once (views and properties, Compose semantics and
slot table, the unified a11y tree, a screenshot per window, lint and render
signals) into the on-disk store both surfaces share
(`$INSPECTOR_WIDGET_CAPTURE_DIR`); the others query a stored capture with no
device I/O. Every node has a short ref (`n23`) that carries across captures of
one app; a ref that left the screen answers `ref_not_in_capture` with where it
was last seen and its `sel` (a durable selector such as `@cell_1 > @delete`).

| Tool | Arguments (defaults) | Returns |
|------|-----------|---------|
| `capture` | `serial?`, `package?`, `label?`, `props=true`, `resolution_stack=false`, `slots=if_available` (`enable` hot-reloads the app first: destructive), `screenshot=true`, `screenshot_scale=1.0`, `skp=false`, `a11y_rendering=false`, `lint=tree` (`full` adds contrast), `settle_ms=0`, `diff_from?`, `if_changed_since?`, `outline_lines=20`, `on_screen=true`, `pin=false`, `max_bytes=3000` | `{capture, session, pid, device, took_ms, consistency, facets, windows, lint, issues, diagnostics?, note?, diff?, outline, on_screen, next}`; `facets.compose` says `obfuscated: ...` (or the `semantics_failed` reason) instead of a count when Compose cannot be read; `diagnostics` leads with the agent's own cuts (`views: depth-truncated=N`, `compose: semantics_failed: ...`) |
| `captures` | `action=list` (`show`, `pin`, `unpin`, `label`, `drop`, `export`, `gc`), `id?`, `label?`, `what=nodes` (`walks` lists the stored TalkBack walks), `format=jsonl`, `all=false`, `limit=20`, `max_bytes=2000`, `serial?`, `package?` | list lines, one capture's details, or the paths `export` wrote; `show` / `export` / `drop` also take a walk id (`w3f9ak1`, `t...` for a scenario); `gc(all=true)` wipes the store, walks included |
| `outline` | `capture=latest`, `root?`, `view=ui` (`views`, `compose`, `slots`, `a11y`, `reading`), `depth=3`, `detail=semantic`, `origin=app`, `max_children=12`, `max_lines=80`, `fields?`, `cursor?`, `format=lines`, `max_bytes=6000` | one grammar-v1 line per node, `+N` hidden counts, a cursor; the expand hint follows the biggest cut |
| `find` | `capture=latest`, `text`, `text_re`, `type`, `rid`, `tag`, `src`, `role` (globs), `flags`, `any_flags`, `has`, `missing`, `issue`, `within`, `at`, `overlaps`, `min_dp`/`max_dp` (on the touch (a11y) bounds), `kind`, `window` (selector or z index), `in=ui`, `sort=tree`, `limit=20`, `fields?`, `cursor?`, `count_only=false`, `format=lines`, `max_bytes=3000` | matching lines (filters ANDed), `total`; flags include `truncated` (children the agent did not send) and `redacted` (masked password text) |
| `node` | `ref` or `refs` (≤10), `capture=latest`, `facets?`, `props=none`, `params=brief`, `ancestors=false`, `children=false`, `image=false`, `max_bytes?` | everything about one node: ids, bounds, `tap_xy`, layout/clip, a11y, compose (slots with `file:line`), issues, props |
| `image` | `ref?`, `capture=latest`, `window?`, `overlay=none` (`marks`, `lint`, `reading`, `bounds`, `compose`, `walk`), `marks=auto`, `pad=16`, `source=auto`, `max_side=1024`, `inline=false` (MCP only), `max_bytes=600`, `walk?` | `{path, ...}` of a crop from the node's own window, or an overlay; `overlay="walk"` (or `walk=<id>`) draws a stored TalkBack walk on the capture it started from (below) |
| `lint` | `capture=latest`, `rules?`, `severity=info`, `within?`, `contrast=false`, `wcag=false`, `group=rule`, `per_rule=3`, `limit=30`, `cursor?`, `max_bytes=4000` | findings grouped by rule with fixes; one bug repeated in list cells collapses to `×N in <list> cells (...)`; `+N more` hints keep the call's scope |
| `diff` | `a=prev`, `b=latest`, `within?`, `include?`, `min_move_px=4`, `limit=40`, `image=false`, `cursor?`, `max_bytes=4000` | changed / moved / added / removed / rebound lines; `issues: {resolved, new, gone_with_node?, on_new_nodes?}` compared on the nodes both captures hold (a scroll resolves nothing) |

Arguments are validated once for both surfaces (`bad_args` with the expected
type). `serial` and `package` are optional: explicit ones win, then the
lineage of a named capture, then the **caller's own** default session (the MCP
server's last attach or capture, kept in memory; `$INSPECTOR_WIDGET_SESSION`
= `serial/package` for a CLI), then the store's shared default (the last
attach or capture of any caller), then, for `capture`, the single running
debuggable app (also when the default session's app is no longer running; the
capture says so in `note`). A query the shared default resolved, while the
store holds other apps, carries `session` naming the app it read. Each capture
reads the device's dpi and font scale at that moment. `capture` and `captures`
carry `destructiveHint` (a hot reload, deleting captures); a
`capture(slots="enable")` that loses its session is never retried.

### TalkBack (toolset `talkback`)

The three TalkBack tools are in the same registry (`inspector_widget/surface.py`
over `ops.talkback` / `ops.tb_walk` / `ops.tb_scenario`), listed with
`INSPECTOR_WIDGET_TOOLSET=talkback`, `capture,talkback` or `all`, and in the
default listing in their pre-capture shape. They are **device-wide**: TalkBack
runs for every app while it is on; the accessibility settings are snapshotted
first and restored afterwards, at the server's exit, or by
`talkback(action="restore")`. They carry `destructiveHint` and are never
retried. Failures are the usual error envelope with a TalkBack code:
`talkback_unavailable`, `enable_failed`, `restore_failed`, `busy` (another walk
holds the device), `app_left_foreground`, `injector_failed` (the injectors
tried are the `candidates`), `keymap_unknown`, `start_not_found`.

The loop the MCP instructions give: `capture -> lint(rules=["tb"]) ->
outline(view="reading",explain=true) -> node(ref,facets="tb") ->
tb_walk(start=ref) -> image(overlay="walk")`. The static side (the `tb.*`
rules, the reading view's `explain`, the node `tb` facet) predicts TalkBack
from a stored capture; `tb_walk` confirms it with the real one.

| Tool | Arguments (defaults) | Returns |
|------|-----------|---------|
| `talkback` | `action=status` (`on`, `off`, `restore`), `serial?`, `package?` (on: the app kept in front), `verbose_log=false` | the TalkBack state (installed, enabled, touch exploration, services, a pending restore, injectors), or what `on` / `off` / `restore` changed; `next` points at `tb_walk()` and the restore |
| `tb_walk` | `serial?`, `package?`, `start=current` (`first`, a ref, a selector, or a label as spoken), `direction=next`, `max_steps=60`, `until=wrap` (`edge`, `loop`, `steps`), `expect?` (refs, selectors or labels), `step_timeout_ms=1500`, `settle_ms=120`, `recapture=on_unknown`, `utterance=auto`, `injector=auto`, `leave_on=false`, `max_lines=60`, `max_bytes=5000` | `{capture, walk, recaptured?, talkback, start, steps, ended, ms, lines, diff, findings, expect?, notes?, restore, next}` (at most 5 KB at 60 steps): one line per step, `3. n14 "Add to favorites, Button" via=autoscroll(n10) !double_stop`; `diff` classifies the walk against the model by ref (`model: "13 agree, 1 differ: step 7 ..."`, `skip`, `double`, `out_of_order`, `loop`, `trap`, `escape`, `stuck`, `left_app`, `unvisited` ...); `findings` give `tb.*` codes, refs, basis `walk` and the fix (once per code) |
| `tb_scenario` | `kind` (`focus_after`, `restore`, `survive`), `serial?`, `package?`, `target?` (a ref, selector or label; default: the current focus), `action=activate` (`back`, `tap:<ref>`, `key:<combo>`), `mutate?` (survive: `tap:<ref>`, `activate`, `key:`, `broadcast:<am args>`, `probe:<action>`), `wait_ms=2000`, `injector=auto`, `leave_on=false`, `step_timeout_ms=1500`, `settle_ms=120`, `max_bytes=1000` | `{scenario, kind, capture, after, target, did, windows?, timeline, focus, verdict, finding?, cause?, restore, next}` (at most 1 KB): verdicts `initial_ok` / `on_close_or_unlabeled` / `behind_overlay` / `stayed_on_opener`, `restored` / `near` / `top`, `kept` / `drifted` / `restored` / `reset_top` / `lost`; findings `tb.initial_focus`, `tb.restore_failed`, `tb.focus_reset` / `focus_lost` / `focus_drift`; `cause` comes from the before / after captures (`n47 rebound as n103 ...; 12 removed, 12 added`) |

A walk captures the screen once TalkBack has settled (`props` off; its
diagnostics say `taken with TalkBack on`), so `start`, `expect` and
`tap:<ref>` resolve as refs there, and every step names its node by ref. When
focus lands on a node no capture holds (TalkBack scrolled it in), the walk
recaptures, at most once per three steps (`recapture="never"` turns that off:
those steps show `?<key>`); refs carry over, so a node keeps its ref. The
record (every step with its key, ref, capture, speech and bounds; the predicted
order; the findings) is stored as `<store>/walks/<id>.json` with the capture
ids: `captures(what="walks")` lists walks and scenarios, `captures(action=
"show", id=...)` shows one (every step), `export` gives the JSON path. The
newest 100 are kept. `image(overlay="walk", walk=<id>)` draws a walk on the
capture it started from (or `capture=`): stops numbered by step, arcs in
TalkBack's order, the model's next stop dashed amber where it differs,
mismatches red, predicted stops never reached dashed red; `window=` draws one
window, steps on other captures or windows are counted in `omitted`.

### Node keys and reading order

- Node keys: `view:<uniqueDrawingId>` for Views, `compose:<acvId>:<semanticsId>` for
  Compose nodes (every AndroidComposeView — each RecyclerView cell, each ComposeView
  nested in an AndroidView — is its own id space), `composeview:<acvId>` for a Compose
  window's root, `virtual:<hostId>:<virtualId>` for other providers' virtual nodes.
  `inspect` / `dump_accessibility` / `a11y_lint` hand them out; `inspect_node` /
  `component_image` take them, and every one of them resolves (the a11y nodes Compose
  serves for children merged into a focusable parent, and its synthetic role /
  description nodes, are grafted as `a11y_only` with their `a11y_parent`). A bare
  `compose:<semanticsId>` (or `semantics_id`) is accepted only when one ComposeView has
  that id. Compose re-mints ids on recomposition: the `generation` of each dump (the same
  value from `dump_accessibility`, `a11y_lint` and `inspect` for one UI state) changes
  when that happens, and a key any of them handed out earlier is re-resolved by
  ComposeView, test tag, label, list row and bounds (the dossier then carries
  `resolved_from`; a weak or tied match is an error, never a guess). A key that still
  exists but now names other content (a recycled cell) gets a `key_note`. The key
  registry lives per app process in `$INSPECTOR_WIDGET_KEY_CACHE` (default
  `<tmp>/inspector-widget-keys`, `0` = memory only), so separate CLI runs share it.
- `dump_accessibility` gives every node a `node_key` and returns `focus_order` as
  `[{order, key, id, speak}]` (brief: without `id`; `root` also takes a node key):
  one entry per TalkBack focus stop with what TalkBack
  announces there (`"Delete, button"`, `"Unlabeled, checkbox, not checked"`), built
  from the accessibility child order plus `traversal_before`/`traversal_after` applied
  across the whole tree. It walks the tree TalkBack gets: a View that is not important
  for accessibility is replaced by its children (`ignored: not_important`), a
  noHideDescendants subtree is dropped (`ignored: hidden`), and the windows under the
  topmost modal window (no `FLAG_NOT_TOUCH_MODAL` / `FLAG_NOT_FOCUSABLE`, from the
  agent's `window type=… flags=…` token) are `covered_by` it and have no stops.
  `reading_order_diagnostics` reports constraint cycles, targets missing from the dump,
  linkage ids in the wrong key space and covered windows.

### Node shape (`dump_tree`)

The MCP and the CLI decode the wire with the same `inspector_widget.strings`.
Each `ViewNode` JSON object (`detail="full"`; brief notes in brackets):

- `id` — `View.getUniqueDrawingId()`, stable per view instance. **Pass this as
  `view_id` to `get_properties`.**
- `class_name`, `package_name`, `qualified_name` [dropped when it is just
  package.class].
- `bounds` — `{layout: {x, y, w, h}, render?}`: absolute on-screen px, plus the
  `render` quad (`x0..y3`) when the view is rotated/scaled/skewed [brief:
  `[x, y, w, h]` and `render` when transformed]. A negative size from the
  agent (an accessibility node clipped out of its parent, e.g. an off-screen
  pager page) is clamped to 0 and marked `clipped: true` (brief keeps it).
- `resource` — the view's own `@id`, as `{namespace, type, name}`.
- `layout_resource` — the layout file that inflated it, if known [brief: only
  where it differs from the parent's].
- `view_id_name` — the R.id name (e.g. `"my_button"`), if any.
- `text` — best-effort text for `TextView`s.
- `flags` — e.g. `["IS_WEBVIEW"]`.
- `children` — nested nodes [brief: `hidden_descendants: N` past `max_depth`].

### Property shape (`get_properties` / `dump_tree include_properties`)

Each property (`detail="full"`): `{name, type, is_layout, value, source?,
resolution_stack?}`. `type` is one of `STRING, BOOLEAN, BYTE, CHAR, DOUBLE, FLOAT,
INT16, INT32, INT64, OBJECT, COLOR, GRAVITY, INT_ENUM, INT_FLAG, RESOURCE,
DRAWABLE, ANIM, ANIMATOR, INTERPOLATOR, DIMENSION`. Decoding:

- `COLOR` → the ARGB int (brief: `#AARRGGBB`).
- `GRAVITY` / `INT_FLAG` → the `|`-joined flag names the agent sends
  (`"center_vertical|start"`; `""` for an empty set).
- `DIMENSION` → px (int); `FLOAT` → float.
- `RESOURCE` → `{namespace, type, name}` (brief: `@type/name`).
- `BOOLEAN` → JSON bool; other numeric types → JSON number; string-like types → text.
- `is_layout: true` marks layout-param attributes (e.g. `layout_width`).
- With `include_resolution_stack=true`, `source` names the style/layout that set
  the value and `resolution_stack` lists the ordered chain considered (brief:
  `{value, source, stack}`).

### Typical agent flow

1. `list_devices` → pick a `serial`.
2. `list_processes(serial)` → pick a running, debuggable `package`.
3. `attach(serial, package)` (optional — `dump_tree` auto-attaches).
4. `dump_tree(serial, package)` → read the tree, find a node `id`.
5. `get_properties(serial, package, view_id=<id>)` → inspect that view's
   attributes; add `include_resolution_stack=true` to see where values came from.
6. `screenshot(serial, package)` → render the UI to a PNG when pixels are needed.
7. `detach(serial, package)` when done.

Sessions are cached per `(serial, package)`; repeated calls reuse the live
agent. The density and font scale the lint uses are re-read after 2 s, so a
`settings put system font_scale` or `wm density` change shows in the next call. A cached session is checked before each use (the connection is still
open and the app still has the same pid); a dead one (the agent idled out, the
app restarted, another client sent SHUTDOWN) is dropped and re-attached, and a
read-only call whose connection drops mid-way is retried once on a fresh attach.
Not retried: a timeout (it would only wait again), `attach`/`detach` (they
manage the session themselves), the TalkBack tools (a retry would repeat
device-wide key presses), `dump_compose` with `enable_inspection=true`
unless the request provably never left the host (the hot reload must not run
twice), `capture(slots="enable")` at all (its hot reload is one of several
requests), a call whose app a concurrent `detach` stopped (the retry would inject
the agent again), and anything once the server is exiting. Every tool that
used a session carries that session's warning as `note` (e.g. its agent runs
another build than the local payload.jar), as the CLI prints it for every
subcommand. Errors are returned as
`{"error": "...", "hint"?: "..."}` text content with the call flagged as an
error, so the agent can read and recover; `hint` is the next step for that
error (launch the app, install a debug build, bring a frozen app to the
foreground, raise the timeout, read the agent's logcat). When the server exits,
including on SIGTERM, it disconnects its sessions, removes every adb forward it
made and deletes its PNGs, and leaves the agents running, so the next start
re-attaches warm.

When an injected agent can't start, the app logs why within milliseconds, and
the host reads that (`adb logcat --pid <pid>`, tags `ViewSpector`,
`AndroidRuntime`, `ActivityThread`) while it waits for the agent's socket: the
attach fails at once with the cause (`inject.AgentStartupError`, carrying
`kind`, `cause` and the log lines) instead of timing out after ~13 s. The
kinds:

- `stale_agent`: `Payload.start` died with a linkage error (`NoSuchMethodError`
  and the like) on a Kotlin or protobuf class under its original name. The
  payload carries its own Kotlin and protobuf relocated under
  `com.oberkfell.viewspector.shaded` (CONTRACT.md §2), so an app's own copy,
  R8-shrunk or not, can't stand in for them; only a `payload.jar` built before
  that relocation links the app's. The fix is `scripts/build.sh` (or pointing
  `INSPECTOR_WIDGET_ARTIFACTS` / `--build-out` at a current build-out), not a
  different build of the app.
- `classpath_shadowing`: the same, on a class the app itself declares (its APK
  is named): the app's copy lacks members the payload uses, so inspect a build
  of the app without code shrinking.
- `payload_start`, `bootstrap`, `native` (the JVMTI agent aborted; it logs at E
  only what aborts the install, and a JVMTI error it recovers from, such as
  hidden-API silencing, at W, which the host ignores), `bind` (an earlier
  agent still holds the socket name), `crash` (the app died) and `unknown` (an
  agent error line the host doesn't know, with still no socket 1.5 s later).
- `library_load`: the app could not load `libviewspector.so`. ActivityThread's
  E line names only the class loader and the agent argument, so the host
  quotes ART's own reason from the app's W log (`Agent attach failed
  (result=N) : Unable to dlopen ...`), or says it wasn't logged.

A failure that recurs on every retry into the same process with the same
artifacts (a linkage error, a library that can't load, a bootstrap that can't
find its payload; not an out-of-memory or a thread that couldn't start) is
remembered by a long-lived host (the MCP server, a Python caller) per
`(serial, package, pid, artifacts)`, where the artifacts are all three files
(`inject.artifacts_id`, so rebuilding only the native agent or the bootstrap
counts), and reported again without re-injecting until the app restarts or an
artifact changes; `force` injects anyway. A socket timeout with no such error
says what the app did log since the attach, or that the attach never ran (the
app's main thread runs it). Before pushing anything the host also checks that
the app is debuggable and that `libviewspector.so` (by its ELF header) matches
the app process's ABI: `app_process64` or `app_process32` (read from
`/proc/<pid>/exe`) on the device's primary ABI, since ART loads agents without
native-bridge translation. The agent is built for arm64-v8a only (`abiFilters`
in `agent/build.gradle.kts`), so a 32-bit app or an x86_64 emulator is refused
with one line (use an arm64 device or emulator image) instead of a failed
attach.

`inspect` (CLI `--json`, MCP `inspect`) reports in `summary.incomplete` the
agent's diagnostics tokens that say a dump was cut or partly unreadable
(`depth-truncated=N`, `node-cap=...`, `semantics_truncated: ...`,
`slot_truncated: ...`, `compose_obfuscated`, `semantics_failed: ...`,
`properties-changed=N`, ...), keyed by `view` / `compose` / `a11y`; a View
node's facet carries its `flags` (`CHILDREN_TRUNCATED`, `TEXT_REDACTED`).
Password redaction fails closed (CONTRACT.md §5): editable text whose password
status the agent cannot determine (an R8-obfuscated Compose app, an unresolved
source) goes out masked too, and `summary.redaction` (a dossier's `redaction`)
carries the agent's `redaction_masked: ...` (which values, by node key) and
`redaction_unverified: view#...` (the ComposeViews whose password fields cannot
be identified) tokens, so dots in a field an agent expected to read are
explained. The same tokens are in the `dump_accessibility` / `dump_compose`
diagnostics and in those of a focus read (`Session.a11y_focus`, `a11y_act`).

### Session lifecycle (CLI and Python API)

`inspector_widget.attach(serial=None, package, build_out=None,
force_reinject=False)` returns a `Session`. `session.disconnect()` (also
`close()` and leaving a `with` block) drops the connection and keeps the agent
running; `session.shutdown()` stops the agent for every client;
`session.is_alive()` says whether the session is still usable;
`session.info()` has the pid, warm/cold and the agent's Hello. Every CLI
subcommand disconnects when it finishes, so a CLI run never disturbs an MCP
session on the same app; only `detach` stops the agent, and it never injects
one just to stop it (it exits 1 if the agent didn't stop). `--force` (every
injecting subcommand) and MCP `attach(force=true)` stop a running agent and
inject a fresh one. An agent running another build than the local payload.jar
is replaced on attach, unless other clients are connected to it: then it is
kept, the CLI prints a warning and every MCP tool using the session a `note`,
and `--force` / `force=true` replaces it.
