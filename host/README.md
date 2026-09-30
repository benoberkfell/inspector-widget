# Inspector Widget — Host driver + MCP server

`host/` is the Python side of Inspector Widget. It contains:

- **`inspector_widget/`** — the host driver package. Discovers devices/processes
  over `adb`, injects the native agent + bootstrap dex + payload jar into a
  running debuggable app (`cmd activity attach-agent`), forwards the agent's
  abstract socket, and speaks the framed protobuf protocol from
  `proto/view_inspection.proto`. (Built by the host-driver module.)
- **`mcp_server.py`** — an [MCP](https://modelcontextprotocol.io) server that
  exposes Inspector Widget to an LLM agent as 15 tools, built on top of
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
  tools (15): list_devices, list_processes, attach, dump_tree, get_properties, screenshot, dump_compose, compose_overlay, dump_accessibility, a11y_lint, a11y_overlay, detach, inspect, inspect_node, component_image
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
before anything touches the device, the same way on every transport.

| Tool | Arguments | Returns |
|------|-----------|---------|
| `list_devices` | — | `{devices:[{serial, api, abi, model, state}], count}` |
| `list_processes` | `serial?` | `{serial, processes:[{package, pid, running}], count}` — debuggable packages only; running apps first |
| `attach` | `serial?`, `package`, `force=false` | `{attached, pid, warm, reused, api_level, abi, agent_version, build_id, window_count, root_ids, session, note?}` — injects if needed (idempotent); app must be running. `warm`: the agent was already running; `reused`: this server's cached session was reused; `note` when that session runs an older build. `force=true` stops any running agent and injects a fresh one |
| `dump_tree` | `serial`, `package`, `include_properties=false`, `include_resolution_stack=false`, `include_screenshot=false`, `scale=1.0` | `{roots:[ViewNode…], root_count, properties?, screenshot?}` — auto-attaches |
| `get_properties` | `serial`, `package`, `view_id`, `include_resolution_stack=false` | `{view_id, group:{view_id, properties:[{name,type,value,is_layout?,source?,resolution_stack?}]}}` |
| `screenshot` | `serial`, `package`, `scale=1.0` | `{path, width, height, bytes, scale}` — PNG saved on host |
| `dump_compose` | `serial`, `package`, `include_semantics=true`, `include_slot_table=true`, `enable_inspection=false` (opt-in: hot-reload resets `remember{}` state) | `{roots:[…]}` — Compose semantics tree + slot-table composables with `file:line` (the layer `dump_tree` cannot see) |
| `compose_overlay` | `serial`, `package`, `scale=1.0`, `all_boxes=false` | `{path, boxes, …}` — screenshot with every on-screen Compose element boxed (text/role + bounds) + a flat on-screen text list |
| `dump_accessibility` | `serial`, `package`, `include_extras=true`, `include_rendering_info=false` | unified `AccessibilityNodeInfo` tree (Views + Compose virtual nodes): text/contentDescription/stateDescription/role, state flags, bounds, decoded actions, collection/range info, plus host-computed TalkBack `focus_order` |
| `a11y_lint` | `serial`, `package`, `include_contrast=true`, `scale=1.0`, `wcag_mode=false`, `rules=[…]` | `{summary, findings:[{rule, severity, node, bounds, bounds_dp, message, evidence}], density, font_scale, …}` — the detect/verify engine; `wcag_mode` uses 44dp targets; `include_contrast=false` skips the pixel rule |
| `a11y_overlay` | `serial`, `package`, `scale=1.0`, `include_contrast=true`, `wcag_mode=false` | `{path, boxes, labels, flagged, summary, …}` — screenshot with every a11y node boxed + speakable label + reading-order number, colored by severity |
| `inspect` | `serial`, `package`, `include_properties=false`, `include_overlay=false` | whole-screen merged view+compose+a11y model with per-node correlation; can render the integrated overlay |
| `inspect_node` | `serial`, `package`, one of `node_key` \| `view_id` \| `semantics_id` \| `bounds`, `include_image=true` | dossier `{node_key, bounds, correlation_confidence, view?, compose?, a11y?, component_image{path}, lint[]}` — `compose` carries source `file:line` + modifiers, `view` typed properties, `lint` the element-focused findings |
| `component_image` | `serial`, `package`, one of `node_key` \| `view_id` \| `semantics_id` \| `bounds` | `{path, source}` — cropped PNG of one element (`source`: `skp` \| `bitmap_crop`) |
| `detach` | `serial?`, `package`, `shutdown=true` | `{detached, agent_stopped}` — `shutdown=true` sends SHUTDOWN, stopping the agent for every client (also one this server didn't attach; never injects one to stop it); `shutdown=false` only drops this server's cached connection |

### Node shape (`dump_tree`)

Each `ViewNode` JSON object:

- `id` — `View.getUniqueDrawingId()`, stable per view instance. **Pass this as
  `view_id` to `get_properties`.**
- `class_name`, `package_name` — simple class name + package.
- `bounds` — absolute on-screen `{x, y, w, h}` in px; plus `render_quad`
  (four `[x,y]` corners) when the view is rotated/scaled/skewed.
- `resource` — the view's own `@id`, as `{type, namespace, name, ref}` where
  `ref` is e.g. `"@id/my_button"`.
- `layout_resource` — the layout file that inflated it, if known.
- `view_id_name` — the R.id name (e.g. `"my_button"`), if any.
- `text` — best-effort text for `TextView`s.
- `flags` — e.g. `["IS_WEBVIEW"]`.
- `children` — nested nodes.

### Property shape (`get_properties` / `dump_tree include_properties`)

Each property: `{name, type, value, is_layout?, source?, resolution_stack?}`.
`type` is one of `STRING, BOOLEAN, BYTE, CHAR, DOUBLE, FLOAT, INT16, INT32,
INT64, OBJECT, COLOR, GRAVITY, INT_ENUM, INT_FLAG, RESOURCE, DRAWABLE, ANIM,
ANIMATOR, INTERPOLATOR, DIMENSION`. Decoding:

- `COLOR` → `#AARRGGBB` string.
- `RESOURCE` → `{type, namespace, name, ref}`.
- `BOOLEAN` → JSON bool; numeric types → JSON number; `DIMENSION`/`FLOAT` →
  float; string-like types → text.
- `is_layout: true` marks layout-param attributes (e.g. `layout_width`).
- With `include_resolution_stack=true`, `source` names the style/layout that set
  the value and `resolution_stack` lists the ordered chain considered.

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
call whose connection drops mid-way is retried once on a fresh attach. Errors
are returned as `{"error": "...", "hint"?: "..."}` text content with the call
flagged as an error, so the agent can read and recover; `hint` says where to
look next (the agent's logcat, or the timeout setting). When the server exits
it disconnects its sessions (removing their adb forwards) and leaves the
agents running, so the next start re-attaches warm.

### Session lifecycle (CLI and Python API)

`inspector_widget.attach(serial=None, package, build_out=None,
force_reinject=False)` returns a `Session`. `session.disconnect()` (also
`close()` and leaving a `with` block) drops the connection and keeps the agent
running; `session.shutdown()` stops the agent for every client;
`session.is_alive()` says whether the session is still usable;
`session.info()` has the pid, warm/cold and the agent's Hello. Every CLI
subcommand disconnects when it finishes, so a CLI run never disturbs an MCP
session on the same app; only `detach` stops the agent, and it never injects
one just to stop it. `--force` (every injecting subcommand) and MCP
`attach(force=true)` stop a running agent and inject a fresh one.
