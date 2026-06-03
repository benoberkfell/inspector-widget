# Inspector Widget

<p align="center">
  <img src="assets/inspector-widget.png" alt="Inspector Widget — a green Android detective with a magnifying glass, screwdrivers, a wrench, and a view-hierarchy readout" width="420">
</p>

A **standalone, agent-driven Android View Layout Inspector** — a clean-room re-implementation of
Android Studio's View layout inspection that runs from the command line / an MCP server, with **no
Android Studio and no Google inspector jars**. Reverse-engineered from
Android Studio's own sources, built and verified live on an API 36 emulator (arm64).

## What it does
Injects a native agent into any **debuggable** app and returns:
- the live **View hierarchy** (uniqueDrawingId, class/package, absolute bounds + transform Quad, resource ids, text);
- **typed attributes** for every view via the framework `InspectionCompanion` SPI (booleans, enums, flags, colors, dimensions… hundreds of properties per screen), with **attribute resolution stacks** when `debug_view_attributes` is on;
- a **BITMAP screenshot** (PixelCopy), decoded host-side to PNG.

**Compose** is supported via a pure-reflection in-app extractor (no Compose compile dependency):
- the live **semantics tree** — every on-screen element's Text / ContentDescription / Role / state /
  bounds (always available, no setup);
- the **slot table** — the full composable hierarchy with **parameters, modifiers, and `file:line`**
  source locations. Inspector Widget enables it the way Android Studio does: set
  `isDebugInspectorInfoEnabled`, add slot-table storage, and `HotReloader` hot-reload to force a fresh
  composition that populates it (`dump_compose --enable_inspection`, on by default). The deep tree is
  collapsed to named composables (structural groups hoisted) to stay readable and under protobuf's
  recursion limit.

`compose_overlay` draws every on-screen Compose element as a labeled box over the screenshot.

## Architecture (3 on-device layers + host + MCP)
```
host (python) ──adb push/run-as/attach-agent/forward──► libviewspector.so (JVMTI, C++)
                                                          └► Bootstrap.java (bootstrap classloader)
                                                             └► DexClassLoader(payload, parent=appCL)
                                                                └► payload (Kotlin): LocalServerSocket
host socket client ◄── VWSPCT01 framed protobuf (view_inspection.proto) ──► Dispatcher
mcp_server.py ── exposes 15 tools to an LLM agent over the host driver
```
See `CONTRACT.md` for the fixed identifiers, framing, and build matrix.

## Build (one command)
```bash
./scripts/build.sh        # -> build-out/{libviewspector.so, bootstrap.dex, payload.jar}
```
**Requires** JDK 17+ and an Android SDK. The build *pins* NDK `27.1.12297006`, build-tools `36.1.0`,
and platform `android-36` in `agent/build.gradle.kts` for reproducible single-host builds — relax those
to whatever you have installed (the agent only calls API 33–34 symbols, via reflection, and compiles to
Java 17). The Gradle wrapper auto-fetches Gradle 8.13, so you don't pick it. The only hard runtime floor
is **`minSdk 29`** on the target device. `scripts/build.sh` selects a JDK and produces all three artifacts.

## Use (CLI)
```bash
python3 -m venv host/.venv && host/.venv/bin/pip install -r host/requirements.txt
PYTHONPATH=host host/.venv/bin/python host/cli.py devices
PYTHONPATH=host host/.venv/bin/python host/cli.py dump \
    --serial emulator-5554 --package com.example.app \
    --properties --resolution-stack --screenshot out.png --json tree.json
```

## Use (MCP — drive it from an agent)
```bash
# Easiest — registers with this checkout's absolute paths (no path editing):
./scripts/register-mcp.sh
claude mcp list                  # expect: inspector-widget ... ✓ Connected

# Or register manually, FROM THE REPO ROOT so $PWD expands to your checkout:
claude mcp add inspector-widget -- \
  env PYTHONPATH="$PWD/host" "$PWD/host/.venv/bin/python" "$PWD/host/mcp_server.py"
```
Tools (15): `list_devices`, `list_processes`, `attach`, `dump_tree`, `get_properties`,
`screenshot`, `dump_compose`, `compose_overlay`, `dump_accessibility`, `a11y_lint`,
`a11y_overlay`, `inspect`, `inspect_node`, `component_image`, `detach`.

Compose (CLI):
```bash
PYTHONPATH=host host/.venv/bin/python host/cli.py compose \
    --serial emulator-5554 --package com.example.app \
    --overlay screen.png --json compose.json
```

## Status: working end-to-end
Verified on `emulator-5554` (API 36, arm64), all reachable through the 15 MCP tools: the live
**View tree** with typed properties + resolution stacks, the **Compose** semantics tree and slot
table (parameters/modifiers + `file:line`), the unified **accessibility tree** with TalkBack reading
order plus lint + severity-colored overlay, per-component **SKP images**, and the **integrated
inspect** view that merges View + Compose + a11y with per-node correlation.

## Layout
```
proto/view_inspection.proto     the wire protocol (protoc-validated)
agent/                          Android module: native cpp + Kotlin payload  -> APK -> artifacts
bootstrap/                      tiny Java classloader bridge -> bootstrap.dex
host/inspector_widget/          python driver (adb, inject, framing, client, png, strings)
host/cli.py  host/mcp_server.py CLI + MCP server
scripts/build.sh scripts/run.sh build + smoke test
build-out/                      libviewspector.so, bootstrap.dex, payload.jar
```

## License
[Apache-2.0](LICENSE). See [`NOTICE`](NOTICE) for attribution.

## Acknowledgements
Inspector Widget is a clean-room reimplementation reverse-engineered from the
**Android Open Source Project** — Android Studio's Layout Inspector (`tools-base`)
and Jetpack Compose's UI tooling (AndroidX), both Apache-2.0. No AOSP source is
redistributed here; see [`NOTICE`](NOTICE) for the full attribution. "Android",
"Android Studio", and "Jetpack Compose" are trademarks of Google LLC; this project
is independent and not affiliated with or endorsed by Google.
