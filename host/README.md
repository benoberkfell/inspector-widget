# Inspector Widget — Host driver + MCP server

`host/` is the Python side of Inspector Widget. It contains:

- **`inspector_widget/`** — the host driver package. Discovers devices/processes
  over `adb`, injects the native agent + bootstrap dex + payload jar into a
  running debuggable app (`cmd activity attach-agent`), forwards the agent's
  abstract socket, and speaks the framed protobuf protocol from
  `proto/view_inspection.proto`. (Built by the host-driver module.)
- **`mcp_server.py`** — an [MCP](https://modelcontextprotocol.io) server that
  exposes Inspector Widget to an LLM agent as 18 tools, built on top of
  `inspector_widget`.

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
  tools (18): list_devices, list_processes, attach, dump_tree, get_properties, screenshot, dump_compose, compose_overlay, dump_accessibility, a11y_lint, a11y_overlay, detach, inspect, inspect_node, component_image, talkback, tb_walk, tb_scenario
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
| `dump_tree` | `serial`, `package`, `include_properties=false`, `include_resolution_stack=false`, `include_screenshot=false`, `scale=1.0`, `root_id=0` | `{serial, package, roots:[ViewNode…], root_count, properties?, screenshot?}` — auto-attaches; brief: `properties` is `{view_id: {name: value}}` (non-default values) plus `omitted_defaults` |
| `get_properties` | `serial`, `package`, `view_id`, `include_resolution_stack=false`, `filter=all` | brief: `{serial, package, view_id, properties:{name: value}}`; full: `{…, group:{view_id, properties:[{name,type,is_layout,value,source?,resolution_stack?}]}}` |
| `screenshot` | `serial`, `package`, `scale=1.0` | `{path, width, height, bytes, scale}` — PNG saved on host |
| `dump_compose` | `serial`, `package`, `include_semantics=true`, `include_slot_table=true`, `enable_inspection=false` (opt-in: hot-reload resets `remember{}` state) | `{roots:[…]}` — Compose semantics tree + slot-table composables with `file:line` (the layer `dump_tree` cannot see) |
| `compose_overlay` | `serial`, `package`, `scale=1.0`, `all_boxes=false` | `{path, boxes, …}` — screenshot with every on-screen Compose element boxed (text/role + bounds) + a flat on-screen text list |
| `dump_accessibility` | `serial`, `package`, `include_extras=true`, `include_rendering_info=false` | unified `AccessibilityNodeInfo` tree (Views + Compose virtual nodes): text/contentDescription/stateDescription/role, state flags, bounds, decoded actions, collection/range info, plus host-computed TalkBack `focus_order` and a `generation`; nodes TalkBack never sees carry `ignored`, windows under a modal dialog `covered_by` |
| `a11y_lint` | `serial`, `package`, `include_contrast=true`, `scale=1.0`, `wcag_mode=false`, `rules=[…]`, `include_rendering_info=true`, `group_by=rule` | brief: `{summary, by_rule:{rule:{sev, n, msg, nodes:[≤3 node keys], more?}}, diagnostics (warn/error), density, font_scale, generation, contrast_sampled, omitted}`; `group_by=none` / full: `{summary, findings:[{rule, alias, severity, node_key, node, bounds, bounds_dp, window, collection, message, evidence}], diagnostics, stats, …}` — the detect/verify engine (R1..R18) over the unified a11y tree (Views + Compose), judging what TalkBack reads; `rules` takes ids, `R#` aliases or ATF names; `wcag_mode` uses 44dp targets; `include_contrast=false` skips the pixel rule |
| `a11y_overlay` | `serial`, `package`, `scale=1.0`, `include_contrast=true`, `wcag_mode=false` | `{path, boxes, labels, flagged, summary, …}` — every window composited (a dialog over its activity), every a11y node boxed + what TalkBack says + reading-order number; red error, amber warn, blue info, green clean, dashed = a finding with no a11y node, drawn at its bounds |
| `inspect` | `serial`, `package`, `include_properties=false`, `include_overlay=false` | whole-screen merged view+compose+a11y model with per-node correlation and `summary.generation`; can render the integrated overlay (all windows; green exact, amber overlap, grey none) |
| `inspect_node` | `serial`, `package`, one of `node_key` (`view:<id>` \| `compose:<acvId>:<semanticsId>` \| `composeview:<acvId>`) \| `view_id` \| `semantics_id` \| `bounds`, `include_image=true` | dossier `{node_key, bounds, correlation_confidence, generation, where, context, view?, compose?, a11y?, list_item?, a11y_only?, a11y_parent?, resolved_from?, key_note?, component_image{path}, lint[], lint_summary, lint_diagnostics}` — `compose` carries the semantics attrs (`source` is null: `file:line` needs `dump_compose` with the slot table), `view` typed properties, `lint` exactly the `a11y_lint` findings for the element and the nodes merged into it |
| `component_image` | `serial`, `package`, one of `node_key` \| `view_id` \| `semantics_id` \| `bounds` | `{path, source, window?}` — cropped PNG of one element, cut from its own window (`source`: `skp` \| `bitmap_crop`) |
| `detach` | `serial?`, `package`, `shutdown=true` | `{detached, agent_stopped, note?}` — `shutdown=true` sends SHUTDOWN, stopping the agent for every client (also one this server didn't attach, or one in the app's new process after a restart; never injects one to stop it); `agent_stopped` is true only once nothing listens on the agent's socket; `shutdown=false` only drops this server's cached connection |

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
  `[x, y, w, h]` and `render` when transformed].
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
agent. A cached session is checked before each use (the connection is still
open and the app still has the same pid); a dead one (the agent idled out, the
app restarted, another client sent SHUTDOWN) is dropped and re-attached, and a
read-only call whose connection drops mid-way is retried once on a fresh attach.
Not retried: a timeout (it would only wait again), `attach`/`detach` (they
manage the session themselves), the TalkBack tools (a retry would repeat
device-wide key presses), `dump_compose` with `enable_inspection=true`
unless the request provably never left the host (the hot reload must not run
twice), a call whose app a concurrent `detach` stopped (the retry would inject
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
