# ViewSpector — Implementation Contract (read in full before writing code)

A standalone, agent-driven re-implementation of Android Studio's **View** Layout Inspector.
Everything here is FIXED. Do not rename packages, paths, or protocol constants. Build against these.

## 0. Target environment (already verified live)
- Host: macOS arm64. Android SDK at `~/Library/Android/sdk` (NDK `27.1.12297006`, cmake `3.22.1`, build-tools `36.1.0`, platform `android-36`). `adb`, `gradle`, `java 21`, `protoc 34.1`, `python3` on PATH. No standalone `kotlinc` (use the Kotlin Gradle plugin).
- Device: `emulator-5554`, **API 36, arm64-v8a**.
- Inspect target (debuggable, installed): **`com.oberkfell.a11yprobe`**, the bundled test app under `testapps/a11yprobe` (`scripts/install-a11yprobe.sh`). Any other debuggable app works the same way.
- Project root: the repository root. Paths below are relative to it.

## 1. Reference sources (cite, don't reinvent)
- Reverse-eng docs (kept outside this repo, in a sibling `../docs/`; not distributed): `05-VIEW_INSPECTOR_BUILD_GUIDE.md` (build order Phase A–D), `01-LAYOUT_INSPECTION_SPEC.md` (Parts II–IV), `00-README.md`.
- Real source to model on (AOSP, Apache-2.0; not redistributed): `tools-base/ui-inspector/**` (host injection + framing + agent layering), `.../dynamic-layout-inspector/agent/appinspection/**` (RootsDetector, property extraction, BitmapExtensions, ViewExtensions), `.../app-inspection/agent/**` (AppInspectionService/InspectorContext dex-load) , `.../dynamic-layout-inspector/common/.../BitmapUtils.kt` + `util/ZipUtils.kt`.
- Read those for exact framework API signatures and SDK gates. Cite file:line in code comments where a choice is non-obvious.

## 2. Architecture (3 on-device layers + host + mcp)
```
HOST (python)                         DEVICE (target app process)
  adb push  libviewspector.so  ─┐
  adb push  bootstrap.dex       ├─► /data/local/tmp ──run-as cp──► app data dir
  adb push  payload.jar         ─┘
  adb shell cmd activity attach-agent <pkg> <so>=<bootstrap.dex+payload.jar args>
                                        │ native Agent_OnLoad/OnAttach
                                        ▼
                            (1) NATIVE JVMTI  libviewspector.so  [C++/NDK]
                                  AddToBootstrapClassLoaderSearch(bootstrap.dex)
                                  FindClass com/oberkfell/viewspector/agent/Bootstrap
                                  call static Bootstrap.initialize(payloadPath, socketName)
                                        ▼
                            (2) BOOTSTRAP  Bootstrap.java  [in bootstrap classloader]
                                  find app ClassLoader (ActivityThread.currentApplication)
                                  DexClassLoader(payloadPath, optDir, null, appClassLoader)
                                  load Payload, run Payload.start(socketName) on a new thread
                                        ▼
                            (3) PAYLOAD  com.oberkfell.viewspector.agent.payload.*  [Kotlin, child of app CL]
                                  LocalServerSocket(socketName)  ── framing ──►  serves Requests
  adb forward tcp:<port> localabstract:<socketName>  ◄───────────────────────────┘
  socket client speaks framed protobuf
MCP (python) ── exposes tools over the host driver
```
Rationale for 3 layers (do not collapse): the payload MUST run in a classloader that is a child of the **app** classloader so it can resolve AndroidX/Material `*$InspectionCompanion` classes for comprehensive property coverage. The bootstrap layer exists only to give the native agent a class it can `FindClass` after `AddToBootstrapClassLoaderSearch`. See ui-inspector README + app-inspection InspectorContext.

## 3. Fixed identifiers
- Kotlin/Java packages: payload = `com.oberkfell.viewspector.agent.payload`; bootstrap = `com.oberkfell.viewspector.agent` (class `Bootstrap`); proto java = `com.oberkfell.viewspector.proto` (outer class `ViewInspection`).
- Native lib: `libviewspector.so` (target abi `arm64-v8a`). JVMTI entry symbols: `Agent_OnAttach` (and `Agent_OnLoad`).
- Artifacts the host pushes: `libviewspector.so`, `bootstrap.dex`, `payload.jar` (payload as a dex-in-jar loadable by DexClassLoader).
- Abstract socket name: `viewspector_<pid>` (LocalServerSocket name, i.e. `@viewspector_<pid>` abstract namespace).
- Python host package: `inspector_widget` (module files under `host/inspector_widget/`; the user-facing name, see AGENTS.md §1). CLI entry `host/cli.py`, MCP entry `host/mcp_server.py`.

## 4. Wire framing (host ⇄ payload, both directions)
Each message on the socket is: `MAGIC(8 bytes ascii "VWSPCT01")` + `LEN(4 bytes, big-endian uint32, = payload length)` + `payload(protobuf-encoded Request or Response)`. Reader: read 8, assert magic; read 4, parse BE length; read exactly LEN bytes; parse. One Request in flight at a time, synchronous Response (mirror ui-inspector CommandSender). Magic mismatch => hard error.

## 5. Protocol
`proto/view_inspection.proto` (package `viewspector.proto`) is FINAL and validated. Generate Kotlin/Java (lite is fine) for the payload and Python for the host from this exact file. Key semantics: string-table interning (int32 id → Strings.entries; id 0 = absent); `ViewNode.id` = `View.getUniqueDrawingId()`; `Bounds.render` Quad only when transformed; BITMAP wire = **9-byte header** [width LE int32@0][height LE int32@4][BitmapType byte@8] + raw pixels, the whole buffer **`java.util.zip.Deflater(BEST_SPEED)`**-compressed (host inflates with `Inflater`); BitmapType bytes: 1=RGB_565, 2=ABGR_8888, 3=ARGB_8888 (ARGB_8888 Bitmap.Config maps to ABGR_8888 on the wire).

## 6. Capability requirements (from the build guide)
- Roots: `android.view.inspector.WindowInspector.getGlobalWindowViews()` on the **main thread** (API 29+; emulator is 36). Filter visible+attached; sort by `View.getZ()`; key by `uniqueDrawingId`.
- Tree walk: recurse ViewGroups on the main thread; build ViewNode with absolute bounds (`getLocationOnScreen` + width/height), Quad when `getMatrix()`/rotation present; resource names via `View.getId()` + `resources.getResourceName`; IS_WEBVIEW flag if assignable from `android.webkit.WebView`; best-effort text for `TextView`.
- Properties: `android.view.inspector.StaticInspectionCompanionProvider` to load `<ViewClass>$InspectionCompanion`; map via `PropertyMapper`, read via a custom `PropertyReader`; include layout-param properties; resolution stack via `View.getAttributeResolutionStack(int)` / `getAttributeSourceResourceMap()` where available (API 29+); decode gravity/flag ints to labels. Honest fallback: views without a companion yield only base View attributes.
- Screenshot (BITMAP): `PixelCopy.request(window/surface, bitmap, listener, handler)` or per-view `ViewTreeObserver.registerFrameCommitCallback` + PixelCopy; scale via Bitmap; then the §5 header+Deflater encoding. Run capture off the main thread but request on a Handler thread per PixelCopy contract.
- SKP (per-component images): `CaptureSkpCommand` records a Skia picture via `ViewDebug.startRenderingCommandsCapture` (API 33+). The host cuts per-component images out of it by `layerId` with Google's `skiaparser` (`host/inspector_widget/skiaparser.py`, `skia_client.py`) and falls back to a BITMAP crop.
- Must run main-thread work via `Handler(Looper.getMainLooper())` and never block the main thread on the socket.

## 7. Build outputs (where things land)
Final artifacts copied to `build-out/` (gitignored): `libviewspector.so`, `bootstrap.dex`, `payload.jar`. The host reads from there. Provide `scripts/build.sh` that produces all three (gradle assemble + extract from APK/AARs + d8 as needed) and `scripts/run.sh` that injects into `com.oberkfell.a11yprobe` (default; pass a serial and package to override) and runs a smoke dump. Use SDK at `~/Library/Android/sdk`; `compileSdk 36`, `minSdk 29`, ndk `27.1.12297006`, build-tools `36.1.0`.

## 8. Coding standards
- Kotlin for payload, Java for Bootstrap (tiny, framework-only, no Kotlin stdlib dependency so it stays clean in the bootstrap classloader). C++17 for the native agent.
- No dependency on `androidx.inspection`, no Google inspector jars — this is a clean-room re-implementation using framework APIs + our own proto.
- Defensive: every reflective/framework call that can fail on some API level guarded and logged via `android.util.Log` tag `"ViewSpector"`.
- Python: stdlib + `protobuf` + (optional) `mcp`; no heavyweight deps. adb via `subprocess`.
