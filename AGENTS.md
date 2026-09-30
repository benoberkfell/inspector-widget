# AGENTS.md — Inspector Widget

Guide for any agent or human working in this repository. Read this first.

> **Inspector Widget** is a **standalone, agent-driven Android layout inspector** — a
> clean-room re-implementation of Android Studio's Layout Inspector that runs from a CLI
> or an MCP server, with **no Android Studio and no Google inspector jars**. It injects a
> native agent into any *debuggable* app and returns the View hierarchy, typed properties,
> the Compose semantics + slot tree, the unified accessibility tree, screenshots, and
> per-component images — plus an integrated view that correlates all of it per node.

Verified live on `emulator-5554` (API 36, arm64) against the bundled `com.oberkfell.a11yprobe`
test app.

---

## 1. Naming convention — READ THIS BEFORE RENAMING ANYTHING

The product has two names on purpose. Do not collapse them.

| Surface | Name | Examples |
|---|---|---|
| **User-facing** | **Inspector Widget** | CLI prog `inspector-widget`, MCP server name `inspector-widget`, Python package `inspector_widget`, README/skill, env vars `INSPECTOR_WIDGET_LOG` / `INSPECTOR_WIDGET_SKIAPARSER_DIR` |
| **Android internals (codename)** | **viewspector** / **ViewSpector** | `com.oberkfell.viewspector` (Android package), `libviewspector.so`, `VWSPCT01` framing magic, abstract socket `viewspector_<pid>`, logcat `TAG="ViewSpector"`, protobuf package `viewspector.proto` |

**Never rename the codename internals.** They form the fixed on-device contract (see
`CONTRACT.md`). In particular the host prints `adb logcat -s ViewSpector` as a debug hint —
that string MUST match the agent's real logcat tag or it sends users to an empty log.
Old env vars `VIEWSPECTOR_LOG` / `VIEWSPECTOR_SKIAPARSER_DIR` are still honored as fallbacks.

---

## 2. Architecture (3 on-device layers + host + MCP)

```
host (python) ──adb push/run-as/attach-agent/forward──► libviewspector.so   (JVMTI, C++)
                                                          └► Bootstrap.java   (bootstrap classloader bridge)
                                                             └► DexClassLoader(payload, parent=appCL)
                                                                └► payload (Kotlin): LocalServerSocket
host socket client ◄── VWSPCT01-framed protobuf (proto/view_inspection.proto) ──► Dispatcher
cli.py / mcp_server.py ── drive the host; mcp_server exposes 18 tools to an LLM agent
```

- **Wire**: 8-byte magic `VWSPCT01` + 4-byte big-endian length + protobuf. Abstract socket
  `viewspector_<pid>`, reached via `adb forward`. **Not** gRPC, **not** the transport daemon.
- **Classloader**: the payload's loader is a child of the app's (parent-first), yet the payload
  links against none of the app's classes: it carries its own Kotlin stdlib and protobuf-lite,
  relocated under `com.oberkfell.viewspector.shaded`, and reaches app types (AndroidX,
  Compose) only by reflection. So an app's R8-shrunk Kotlin can no longer break the attach;
  a `NoSuchMethodError` on an unrelocated `kotlin.*` class at attach means a stale
  `payload.jar` (the host reports `stale_agent`: run `scripts/build.sh`). See CONTRACT.md §2.
- **Accessibility**: in-process `View.setQueryFromAppProcessEnabled` (API 34+) so a single
  recursion covers Views *and* Compose virtual a11y nodes.
- **Compose**: pure-reflection extractor over the app's own bundled
  `androidx.compose.ui.tooling.data` (semantics tree always available; slot table populated
  by toggling `isDebugInspectorInfoEnabled` + `HotReloader` recomposition).
- **Per-component images**: capture an SKP (`ViewDebug.startRenderingCommandsCapture`, API 33+),
  cut a graphicsLayer by `layerId` via Google's auto-downloaded `skiaparser`; fall back to a
  BITMAP crop.

`CONTRACT.md` is the authoritative spec of the fixed identifiers, framing, and build matrix.

---

## 3. Repository layout

```
proto/view_inspection.proto   the wire protocol (protoc-validated)
agent/                        Android module: native C++ (cpp/) + Kotlin payload  -> APK -> artifacts
bootstrap/                    tiny Java classloader bridge -> bootstrap.dex
host/                         Python host driver + entry points
  inspector_widget/           the package (adb, inject, framing, client, png, strings,
                              a11y, a11y_lint, overlay, correlate, skiaparser, skia_client,
                              proto/, skia_grpc/, _cli.py/_mcp.py console-script wrappers)
  cli.py                      CLI entry point (16 subcommands)
  mcp_server.py               MCP server (18 tools) + `--self-check`
  tests/                      device-free pytest suite (+ @device smoke and a11y golden tests)
  pyproject.toml              packaging (wheel ships cli.py + mcp_server.py as py-modules)
  README.md  PACKAGING.md     host driver + packaging docs
scripts/                      build.sh, run.sh, test.sh, install-a11yprobe.sh, shadow_check.py
testapps/a11yprobe/           GOOD/BAD a11y corpus: Compose, classic View, mixed View/Compose, dialogs
skill/inspector-widget-a11y/  the a11y debugging Skill (SKILL.md, tools.md, rules.md)
build-out/                    generated artifacts (gitignored): libviewspector.so, bootstrap.dex, payload.jar, BUILD_ID
```

---

## 4. Build / Run / Test

**Build the on-device artifacts** (one command):
```bash
./scripts/build.sh        # -> build-out/{libviewspector.so, bootstrap.dex, payload.jar, BUILD_ID}
```
`BUILD_ID` is the sha256 of `payload.jar`; the agent reports the same hash in Hello
(`viewspector-0.1+<sha256>`), and the host replaces a running agent whose build differs, so a
rebuild takes effect on the next attach without restarting the app. The exception: while
another client (an MCP server, say) is connected to that agent, it is kept and the attach warns
instead (`--force` / `force=true` replaces it anyway), so two checkouts with different builds
don't evict each other's agent on every call.
Pinned for reproducibility (in `settings.gradle.kts` / `agent/build.gradle.kts`): AGP 8.7.2,
Kotlin 2.0.21, protobuf-plugin 0.9.4, NDK `27.1.12297006`, build-tools `36.1.0`, compileSdk/targetSdk 36.
The real requirements are looser: **any JDK 17–23** to run Gradle (`build.sh` honours an in-range
`JAVA_HOME`, else finds one; the code targets Java 17/11 bytecode, with no exact-JDK toolchain; Gradle
8.13 can't run on JDK 24+), the wrapper auto-fetches **Gradle 8.13**, and the only hard runtime floor is
**`minSdk 29`**. Relax the SDK/NDK pins to whatever you have installed. The Gradle wrapper jar is tracked
so the build runs without a preinstalled `gradle`; **`local.properties` is not tracked** — point Gradle at
your SDK via `local.properties` (`sdk.dir=...`) or the `ANDROID_HOME` env var.

**Host setup**:
```bash
python3 -m venv host/.venv && host/.venv/bin/pip install -r host/requirements.txt
```

**Run (CLI)** — `PYTHONPATH=host host/.venv/bin/python host/cli.py <subcommand>`:
```bash
host/cli.py devices
host/cli.py inspect      --serial emulator-5554 --package com.oberkfell.a11yprobe --json -
host/cli.py a11y-lint    --serial emulator-5554 --package com.oberkfell.a11yprobe
host/cli.py component-image --serial ... --node-key compose:<acvId>:<semanticsId> --out comp.png
```
Artifacts are read from `--build-out DIR`, else `$INSPECTOR_WIDGET_ARTIFACTS`, else the legacy
`$VIEWSPECTOR_ARTIFACTS`, else the checkout's `build-out/`. A wheel install has no checkout to fall
back on, so set the env var there (the MCP server honours it too; `--self-check` shows what it found).
`--serial` (MCP: `serial`) defaults to `$ANDROID_SERIAL`, else the only attached device; with two
emulators up, pass it or set `ANDROID_SERIAL`.

**Session lifecycle.** Every subcommand except `detach` disconnects when it finishes and leaves the
agent running (the next run is a warm connect, and a concurrent MCP session is untouched); `detach`
sends SHUTDOWN, which stops the agent for every client (each one sees EOF at once), and never
injects one first; it reports the agent stopped only once nothing listens on its socket (exit 1,
MCP `agent_stopped: false`, otherwise). `--force` (MCP `attach(force=true)`) stops a running agent
and injects afresh. The MCP server re-attaches a cached session that died (idle timeout, app
restart, another client's SHUTDOWN) and retries a call once if the connection drops mid-way; a
timeout is reported, not retried. Each agent request has a deadline (`INSPECTOR_WIDGET_TIMEOUT`,
default 30s, 4x for screenshots/Compose/a11y dumps; `0` disables it), so a frozen app returns an
error, not a hang. An app in the background can be frozen by Android (the cached-apps freezer);
attach then says so rather than queuing an injection, and asks for the app in the foreground.
In `/proc/net/unix` only the listening entry means an agent is there: every client connection is
listed under the same `@viewspector_<pid>` for as long as it is open (`adb.socket_exists` vs
`adb.socket_connections`).

**Run (MCP)**:
```bash
./scripts/register-mcp.sh                        # register with real paths (no editing)
# or manually, from the repo root so $PWD expands to your checkout:
claude mcp add inspector-widget -- \
  env PYTHONPATH="$PWD/host" "$PWD/host/.venv/bin/python" "$PWD/host/mcp_server.py"
host/mcp_server.py --self-check                  # prints proto status + the 18 tools
```

**Test**:
```bash
./scripts/test.sh                                   # device-free suite
cd host && .venv/bin/python -m pytest tests -q -m "not device"
cd host && .venv/bin/python -m pytest tests -q -m device   # live emulator smoke (needs adb)
```

**A11yProbe test corpus** (`testapps/a11yprobe`, package `com.oberkfell.a11yprobe`). Every
scenario pairs a GOOD variant with a BAD one; the BAD ones are deliberate defects, so never
"fix" them. Install with `scripts/install-a11yprobe.sh <serial>` (add `--scenario <id>` to open
one), then launch any scenario directly by intent extra:
```bash
# Compose GOOD/BAD pairs (ids in ScenarioRegistry.kt, e.g. icon_button, traversal; "all" stacks every one)
adb -s <serial> shell am start -S -W -n com.oberkfell.a11yprobe/.MainActivity --es scenario icon_button
# Classic-View GOOD/BAD pairs (XML)
adb -s <serial> shell am start -S -W -n com.oberkfell.a11yprobe/.ViewScenarioActivity
# Mixed View/Compose hierarchies and dialog windows (ids in InteropFragment.kt):
#   S1 RecyclerView of ComposeView cells   S2 of View cells   S3 mixed + View-containing-ComposeView cells
#   S4 LazyColumn with AndroidView rows    S5 ComposeView > AndroidView > RecyclerView > cells
#   S6 RecyclerView grid                   D1 DialogFragment (Views + ComposeView)   D2 Compose Dialog
adb -s <serial> shell am start -S -W -n com.oberkfell.a11yprobe/.InteropActivity --es scenario S3
```
`host/tests/test_device_a11y_golden.py` (marked `device`) launches each scenario that way and
asserts the golden answers: every BAD node flagged with its rule id, GOOD nodes not flagged,
unique a11y node keys, one Compose window per ComposeView, and known reading orders (the
Compose traversal screen, the classic-View ScrollView screen read item by item, only the
dialog readable while D1/D2 are open). It runs only when `ANDROID_SERIAL` names the device:
```bash
cd host && ANDROID_SERIAL=<serial> .venv/bin/python -m pytest tests/test_device_a11y_golden.py -q -m device
```
The default build uses Compose BOM 2024.09.00 (ui 1.7.0); `scripts/install-a11yprobe.sh <serial>
--compose-bom 2025.06.00` builds it on ui 1.8.2, which takes the agent's other Compose traversal
code path (1.8 to 1.12). `tests/test_device_compose_order_under_talkback.py` turns TalkBack on for
a few seconds, so it also needs `INSPECTOR_WIDGET_TALKBACK_TESTS=1`.

---

## 5. Capabilities (CLI ↔ MCP parity)

16 CLI subcommands / 18 MCP tools. Keep them at parity (see §6).

| Group | MCP tools | CLI subcommands |
|---|---|---|
| Device/session | `list_devices`, `list_processes`, `attach`, `detach` | `devices`, `packages`, `attach`, `detach` |
| View | `dump_tree`, `get_properties`, `screenshot` | `dump`, `get-properties`, `screenshot` |
| Compose | `dump_compose`, `compose_overlay` | `compose` (+`--overlay`) |
| Accessibility | `dump_accessibility`, `a11y_lint`, `a11y_overlay` | `a11y` (+`--lint`/`--overlay`), `a11y-lint` |
| Integrated | `inspect`, `inspect_node`, `component_image` | `inspect`, `inspect-node`, `component-image` |
| TalkBack (device-wide; needs TalkBack installed) | `talkback`, `tb_walk`, `tb_scenario` | `talkback status\|on\|off\|restore`, `tb-walk`, `tb-scenario` |

Every subcommand routes through `inspector_widget.attach() -> Session` (the same facade the MCP
uses); the older ones then drive `session.client` directly (works; their bodies are not yet shared
with the MCP tools).

---

## 6. Working on this codebase

**The #1 hazard: latent device-path bugs.** Most host logic only runs against a live device,
so the offline pytest suite can stay green while a device-only path calls a symbol that does
not exist, has the wrong signature, or the wrong unit. This has bitten the project repeatedly
(`adb.display_density`, `a11y.lint_a11y`, `overlay.render_integrated_overlay`, a dpi-vs-ratio
density, an ARGB red/blue swap). Defend against it on **every** change:

1. **Symbol-parity test** — `host/tests/test_symbol_parity.py` AST-scans `cli.py`,
   `mcp_server.py` and the device-path package modules. It asserts every `adb.*` / `a11y.*` /
   `a11y_lint.*` / `overlay.*` / `png.*` / `correlate.*` / `inject.*` / `client.*` access resolves,
   and it binds every resolvable call into `inspector_widget` against the real signature
   (kwargs, arity, Session/Client/Injection methods, proto fields, `getattr` probes). Run it;
   if you add a cross-module call, it must pass.
2. **Offline end-to-end harness** — `host/tests/test_e2e_fake_agent.py` runs every CLI
   subcommand and MCP tool against `host/tests/fakeagent.py`. Only the adb subprocess is faked
   (`adb._run`), so the real inject/Session/Client/framing code talks VWSPCT01 over TCP to a
   fake agent that encodes replies the way the Kotlin payload does. Use the conftest fixtures
   (`fake_device`, `warm_agent`, `run_cli`, `mcp`) and assert on both the wire
   (`fake_device.requests("dump_tree")`) and the output. Add an e2e test with every new
   subcommand or tool; the coverage guards fail otherwise. Open ledger bugs are strict xfails
   carrying the ledger id: fixing one flips it to XPASS, so remove the marker in the same change.
   If you change the agent's wire behaviour, update the fake to match (it also models older
   agents: `build_id=None`, `reply_to_shutdown=False`, `linger_after_stop=True`,
   `close_clients_on_stop=False`, `hello_waits_for_other_clients=True`). Its a11y ids are the
   A1-fixed agent's; `legacy_a11y_ids=True` reproduces what the agent on this branch sends.
   Session-lifecycle behaviour (deadlines, poisoning, re-attach, detach, serials, the build
   handshake) is covered in `host/tests/test_session_lifecycle.py`.
3. **Live-verify on the emulator**, not just pytest. Launch the test app
   (`adb shell am start -n com.oberkfell.a11yprobe/.MainActivity`) and actually run the CLI /
   MCP paths you touched. Offline-green ≠ works-on-device: the fake encodes what we *believe*
   the agent sends.
4. **Keep CLI ↔ MCP ↔ Session at parity.** A capability reachable one way but not the other is
   a bug. If you add an MCP tool, add the CLI subcommand (and vice versa).

**Conventions:**
- `.java` files live under `agent/src/main/java/...`, **not** `src/main/kotlin` — Kotlin
  resolves them but `javac` never compiles them there → `NoClassDefFoundError` at runtime.
- The payload's Kotlin stdlib and protobuf-lite are relocated under
  `com.oberkfell.viewspector.shaded` at build time (`agent/build.gradle.kts`), so an app's own
  copies (R8-shrunk ones especially) can't shadow them through the parent-first app classloader
  (CONTRACT.md §2). So never hand a Kotlin-typed value (lambda, `Pair`, `Unit`, `Sequence`)
  to app code, nor cast an app object to one (`as Function0<*>`, `is Pair<*, *>`): the app's
  `kotlin.*` is not the payload's. Talk to app objects through `java.*` / `android.*` types
  and reflection. `scripts/shadow_check.py [APK_OR_DIR ...]` checks payload.jar (build.sh runs
  it); `INSPECTOR_WIDGET_SHADOW_APKS=<dir>` makes `test_payload_isolation.py` check real APKs.
- a11y model: the reading order and the lint judge the tree TalkBack gets, not the raw dump.
  Views that are not important for accessibility (`important_for_accessibility` AUTO on a real
  View, see CONTRACT.md §9) are skipped with their children hoisted (`ignored`), and windows
  under a modal dialog are `covered_by` it. Node keys are `view:<id>` /
  `compose:<acvId>:<semanticsId>`; a dump's `generation` changes when Compose re-mints ids, and
  `correlate.record_a11y` / the per-app-process key registry let `inspect_node` re-resolve keys.
- Units: a11y lint density is **device DPI (e.g. 420)**, not a px/dp ratio. `LintContext.density`
  is DPI; `adb.display_density()` returns DPI; `mcp_server._device_density()` returns DPI.
- Overlays: node bounds are full-resolution; a screenshot captured at `scale < 1` is smaller.
  Overlay renderers take a `scale` and multiply coordinates by it — always pass the capture scale.
- One screenshot decoder of record: `inspector_widget.png._decode_to_rgba` (handles RGB_565 /
  ABGR_8888 / ARGB_8888, the last needs an R/B swap). Don't fork it; `mcp_server` delegates to it.
- protobuf runtime must be **>= 6.33.5, < 7** (the checked-in gencode's floor). Pinning lower
  makes the proto module unimportable on install. Regenerate the bindings only with
  `host/generate_proto.sh` (or `make -C host proto`): it requires protoc 33.x and refuses others.
- The wheel ships `cli.py` and `mcp_server.py` as top-level py-modules so the console scripts
  work after `pip install` (not just editable installs). See `host/PACKAGING.md`.

**The proven loop:** audit (read-only) → fix (file-partitioned) → **live**-verify → adversarial
review. The audit and review are skeptical and execution-backed; the live-verify is what catches
the device-only regressions the offline suite and code review miss.

---

## 7. Key references

- `CONTRACT.md` — fixed on-device identifiers, framing, build matrix (the codename contract).
- `README.md` — product overview + quickstart.
- `host/README.md` — host driver internals and the full tool surface.
- `host/PACKAGING.md` — wheel/console-script packaging.
- `skill/inspector-widget-a11y/` — the accessibility debugging Skill (rules R1..R18).
- `../docs/` — the original reverse-engineering spec this tool was built from.
