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
`payload.jar` into `build-out/`.

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
files and the **path** is returned (the image is not inlined).

| Tool | Arguments | Returns |
|------|-----------|---------|
| `list_devices` | — | `{devices:[{serial, api, abi, model, state}], count}` |
| `list_processes` | `serial` | `{serial, processes:[{package, pid, running}], count}` — debuggable packages only; running apps first |
| `attach` | `serial`, `package` | `{attached, api_level, abi, agent_version, window_count, session}` — injects if needed (idempotent); app must be running |
| `dump_tree` | `serial`, `package`, `include_properties=false`, `include_resolution_stack=false`, `include_screenshot=false`, `scale=1.0` | `{roots:[ViewNode…], root_count, properties?, screenshot?}` — auto-attaches |
| `get_properties` | `serial`, `package`, `view_id`, `include_resolution_stack=false` | `{view_id, group:{view_id, properties:[{name,type,value,is_layout?,source?,resolution_stack?}]}}` |
| `screenshot` | `serial`, `package`, `scale=1.0` | `{path, width, height, bytes, scale}` — PNG saved on host |
| `dump_compose` | `serial`, `package`, `include_semantics=true`, `include_slot_table=true`, `enable_inspection=false` (opt-in: hot-reload resets `remember{}` state) | `{roots:[…]}` — Compose semantics tree + slot-table composables with `file:line` (the layer `dump_tree` cannot see) |
| `compose_overlay` | `serial`, `package`, `scale=1.0`, `all_boxes=false` | `{path, boxes, …}` — screenshot with every on-screen Compose element boxed (text/role + bounds) + a flat on-screen text list |
| `dump_accessibility` | `serial`, `package`, `include_extras=true`, `include_rendering_info=false` | unified `AccessibilityNodeInfo` tree (Views + Compose virtual nodes): text/contentDescription/stateDescription/role, state flags, bounds, decoded actions, collection/range info, plus host-computed TalkBack `focus_order` |
| `a11y_lint` | `serial`, `package`, `include_contrast=true`, `scale=1.0`, `wcag_mode=false`, `rules=[…]`, `include_rendering_info=true` | `{summary, findings:[{rule, alias, severity, node_key, node, bounds, bounds_dp, window, collection, message, evidence}], diagnostics, stats, density, font_scale, …}` — the detect/verify engine (R1..R18) over the unified a11y tree (Views + Compose); `rules` takes ids, `R#` aliases or ATF names; `wcag_mode` uses 44dp targets; `include_contrast=false` skips the pixel rule |
| `a11y_overlay` | `serial`, `package`, `scale=1.0`, `include_contrast=true`, `wcag_mode=false` | `{path, boxes, labels, flagged, summary, …}` — screenshot with every a11y node boxed + speakable label + reading-order number, colored by severity |
| `inspect` | `serial`, `package`, `include_properties=false`, `include_overlay=false` | whole-screen merged view+compose+a11y model with per-node correlation; can render the integrated overlay |
| `inspect_node` | `serial`, `package`, one of `node_key` (`view:<id>` \| `compose:<acvId>:<semanticsId>` \| `composeview:<acvId>`) \| `view_id` \| `semantics_id` \| `bounds`, `include_image=true` | dossier `{node_key, bounds, correlation_confidence, generation, where, context, view?, compose?, a11y?, list_item?, component_image{path}, lint[]}` — `compose` carries source `file:line` + modifiers, `view` typed properties, `lint` the element-focused findings |
| `component_image` | `serial`, `package`, one of `node_key` \| `view_id` \| `semantics_id` \| `bounds` | `{path, source}` — cropped PNG of one element (`source`: `skp` \| `bitmap_crop`) |
| `detach` | `serial`, `package` | `{detached}` — shuts down the agent session, drops the cache |

### Node keys and reading order

- Node keys: `view:<uniqueDrawingId>` for Views, `compose:<acvId>:<semanticsId>` for
  Compose nodes (every AndroidComposeView — each RecyclerView cell, each ComposeView
  nested in an AndroidView — is its own id space), `composeview:<acvId>` for a Compose
  window's root, `virtual:<hostId>:<virtualId>` for other providers' virtual nodes.
  `inspect` / `dump_accessibility` hand them out; `inspect_node` / `component_image`
  take them. A bare `compose:<semanticsId>` (or `semantics_id`) is accepted only when
  one ComposeView has that id. Compose re-mints ids on recomposition: `inspect`'s
  `summary.generation` changes when that happens, and a key from an earlier dump is
  re-resolved by ComposeView, test tag, label, list row and bounds (the dossier then
  carries `resolved_from`).
- `dump_accessibility` gives every node a `node_key` and returns `focus_order` as
  `[{order, key, id, speak}]`: one entry per TalkBack focus stop with what TalkBack
  announces there (`"Delete, button"`, `"Unlabeled, checkbox, not checked"`), built
  from the accessibility child order plus `traversal_before`/`traversal_after` applied
  across the whole tree. `reading_order_diagnostics` reports constraint cycles, targets
  missing from the dump, and linkage ids in the wrong key space.

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
agent. Errors are returned as `{"error": "..."}` text content with the call
flagged as an error, so the agent can read and recover.
