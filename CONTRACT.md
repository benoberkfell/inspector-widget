# ViewSpector — Implementation Contract (read in full before writing code)

A standalone, agent-driven re-implementation of Android Studio's **View** Layout Inspector.
Everything here is FIXED. Do not rename packages, paths, or protocol constants. Build against these.

## 0. Target environment (already verified live)
- Host: macOS arm64. Android SDK at `~/Library/Android/sdk` (NDK `27.1.12297006`, cmake `3.22.1`, build-tools `36.1.0`, platform `android-36`). `adb`, `gradle`, a JDK 17–23, `python3` on PATH; `protoc 33.x` only to regenerate the Python bindings (`host/generate_proto.sh` enforces it). No standalone `kotlinc` (use the Kotlin Gradle plugin).
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
`proto/view_inspection.proto` (package `viewspector.proto`) is FINAL and validated. Generate Kotlin/Java (lite is fine) for the payload and Python for the host from this exact file. Key semantics: string-table interning (int32 id → Strings.entries; id 0 = absent); `ViewNode.id` = `View.getUniqueDrawingId()`; `Bounds.render` Quad only when transformed; BITMAP wire = **9-byte header** [width LE int32@0][height LE int32@4][BitmapType byte@8] + raw pixels, the whole buffer **`java.util.zip.Deflater(BEST_SPEED)`**-compressed (host inflates with `Inflater`); BitmapType bytes: 1=RGB_565, 2=ABGR_8888, 3=ARGB_8888 (ARGB_8888 Bitmap.Config maps to ABGR_8888 on the wire). `HelloResponse.agent_version` is `viewspector-0.1+<sha256 of the loaded payload.jar>` (`+unknown` if the payload could not hash it; no `+` part from agents that predate this), which the host compares with its own `payload.jar` to replace a stale agent. SHUTDOWN stops the agent for every client: the payload writes the `ShutdownResponse` first, then closes the server socket (releasing `@viewspector_<pid>`) and shuts down every client connection, so each client sees EOF at once. Requests from different connections are served one at a time, except HELLO and SHUTDOWN, which are answered without waiting. The payload stops itself after 5 minutes with no client connected. In `/proc/net/unix` only the listening entry (Flags `00010000`) means an agent holds the name: each client connection is listed under the same `@viewspector_<pid>` for as long as it is open.

## 6. Capability requirements (from the build guide)
- Roots: `android.view.inspector.WindowInspector.getGlobalWindowViews()` on the **main thread** (API 29+; emulator is 36). Filter visible+attached; sort by `View.getZ()`; key by `uniqueDrawingId`.
- Tree walk: recurse ViewGroups on the main thread; build ViewNode with absolute bounds (`getLocationOnScreen` + width/height), Quad when `getMatrix()`/rotation present; resource names via `View.getId()` + `resources.getResourceName`; IS_WEBVIEW flag if assignable from `android.webkit.WebView`; best-effort text for `TextView`.
- Properties: `android.view.inspector.StaticInspectionCompanionProvider` to load `<ViewClass>$InspectionCompanion`; map via `PropertyMapper`, read via a custom `PropertyReader`; include layout-param properties; resolution stack via `View.getAttributeResolutionStack(int)` / `getAttributeSourceResourceMap()` where available (API 29+); decode gravity/flag ints to labels. Honest fallback: views without a companion yield only base View attributes.
- Screenshot (BITMAP): `PixelCopy.request(window/surface, bitmap, listener, handler)` or per-view `ViewTreeObserver.registerFrameCommitCallback` + PixelCopy; scale via Bitmap; then the §5 header+Deflater encoding. Run capture off the main thread but request on a Handler thread per PixelCopy contract.
- SKP (per-component images): `CaptureSkpCommand` records a Skia picture via `ViewDebug.startRenderingCommandsCapture` (API 33+). The host cuts per-component images out of it by `layerId` with Google's `skiaparser` (`host/inspector_widget/skiaparser.py`, `skia_client.py`) and falls back to a BITMAP crop.
- Must run main-thread work via `Handler(Looper.getMainLooper())` and never block the main thread on the socket.

## 7. Build outputs (where things land)
Final artifacts copied to `build-out/` (gitignored): `libviewspector.so`, `bootstrap.dex`, `payload.jar`, plus `BUILD_ID` (the sha256 of `payload.jar`, the build id the agent reports in Hello). The host reads from there. Provide `scripts/build.sh` that produces all three (gradle assemble + extract from APK/AARs + d8 as needed) and `scripts/run.sh` that injects into `com.oberkfell.a11yprobe` (default; pass a serial and package to override) and runs a smoke dump. Use SDK at `~/Library/Android/sdk`; `compileSdk 36`, `minSdk 29`, ndk `27.1.12297006`, build-tools `36.1.0`.

## 8. Coding standards
- Kotlin for payload, Java for Bootstrap (tiny, framework-only, no Kotlin stdlib dependency so it stays clean in the bootstrap classloader). C++17 for the native agent.
- No dependency on `androidx.inspection`, no Google inspector jars — this is a clean-room re-implementation using framework APIs + our own proto.
- Defensive: every reflective/framework call that can fail on some API level guarded and logged via `android.util.Log` tag `"ViewSpector"`.
- Python: stdlib + `protobuf` + (optional) `mcp`; no heavyweight deps. adb via `subprocess`.

## 9. Accessibility and Compose node identity
The agent emits ids in the spaces below (agent side: `A11yIds.kt`, `AccessibilityInspector.kt`, `ComposeInspector.kt`); the host keys nodes from them. No proto change is involved.
- AOSP packs an accessibility node id as `(virtualDescendantId << 32) | accessibilityViewId` (`AccessibilityNodeInfo.makeNodeId`). The LOW half names the backing View (`View.getAccessibilityViewId()`), the HIGH half the virtual descendant (`-1` = the View itself). The agent decodes each node from its own packed source id.
- `A11yNode.host_view_id` = `uniqueDrawingId` of the node's own backing View: the real View for a View node, the provider host (e.g. that `AndroidComposeView`) for a virtual node. `0` = unresolvable (counted in the diagnostics as `unresolved-nodes`).
- `A11yNode.virtual_id` = `-1` for a real View, else the virtual descendant id; for Compose it is the `SemanticsNode` id, except that Compose serves the unmerged root `SemanticsNode` as the `AndroidComposeView`'s own node (`virtual_id -1`). `is_virtual = (virtual_id != -1)`. `provider_class` is set only on provider hosts, to the host View's class name.
- Host node key = `(host_view_id << 32) ^ (virtual_id & 0xFFFFFFFF)`. `traversal_before`, `traversal_after`, `label_for`, `labeled_by` and `labeled_by_list` are emitted in that same key space (`0` = none).
- `is_traversal_group`: no platform accessor exists through API 37, so it comes from the Compose `IsTraversalGroup` semantics flag (false for Views).
- `layout_size_w/h` on a Compose virtual node = the measured size (px) of the node's `LayoutNode` (the space it reserves, `minimumInteractiveComponentSize` included). Compose reports no `ExtraRenderingInfo` and widens every clickable's `boundsInScreen` to the 48dp touch size, so this is the only view of how big the control is laid out. On View nodes the fields keep their `ExtraRenderingInfo.getLayoutSize()` meaning (LayoutParams, only with `include_rendering_info`).
- Compose computes `traversal_before/after` only while an accessibility service runs; without one the agent runs Compose's own `setTraversalValues` for each `AndroidComposeView` before the walk (the private instance method on ui <= 1.7, the static `_androidKt` helper on ui 1.8 to 1.12), so the linkage is always present. Afterwards it restores the delegate to Compose's no-service state: it clears the maps it filled and sets `currentSemanticsNodesInvalidated` back to true (the walk clears it), so a service turned on later still gets Compose's order. Diagnostics: `a11y-services=on|off`, `compose-traversal computed=N|service=N|unavailable=N`.
- `important_for_accessibility` on a real View node is the View's mode, except that AUTO is reported as YES when the View resolves to important (`isImportantForAccessibility`: actionable, listeners, a delegate, a provider, a live region, a pane, a heading). AUTO therefore means "not important": TalkBack (which does not request not-important Views) never sees it and reads its children in its place, while the agent's in-process connection fetches it anyway. Virtual nodes report YES / AUTO the same way.
- Each window root adds a diagnostics token `root#<id> window type=T flags=0xF` (its `WindowManager.LayoutParams`); the host treats a window without `FLAG_NOT_TOUCH_MODAL` and `FLAG_NOT_FOCUSABLE` as modal and the windows below it as unreachable to accessibility services.
- Window root ids: on API 37 (observed live) a window's root View reports the accessibility view id `ROOT_ITEM_ID`, shared by every window root; on API <= 36 a root has an ordinary counter id (`View.getAccessibilityViewId`). The agent handles both (it resolves `ROOT_ITEM_ID` to the root being walked). The identity, linkage and window-offset logic has been verified live on API 37 only; the API 34-36 query path and the API 29-33 local fallback follow the AOSP 36.1 sources but have not been run.
- `DumpComposeResponse.Window.view_id` = the `AndroidComposeView`'s `uniqueDrawingId`, and every `AndroidComposeView` gets its own Window, including ones nested in `AndroidView` or RecyclerView cells at any depth. The Window's synthetic root node carries that same id: key it `composeview:<acvId>`, never `compose:<id>`. Semantics nodes are keyed `compose:<acvId>:<semanticsId>`; semantics ids are re-minted on recomposition, so a key is only valid for the dump that produced it.
- Slot-table nodes (`ComposeNode.kind = COMPOSABLE` below the synthetic root) carry a negative id (`<= -2`), derived from the hash of their group's slot-table identity (an anchor the slot table keeps while the group lives), unique within their Window and stable across dumps of an unchanged composition. They never collide with a semantics id (positive), the synthetic root (the positive `uniqueDrawingId`) or `-1`; key them `compose:<acvId>:<id>` like semantics nodes. Semantics and slot-table nesting on the wire is capped at 80 levels below the synthetic root (the host's protobuf parser rejects messages nested ~100 deep); deeper subtrees are cut and reported.
- An `AndroidComposeView` whose class R8 renamed is still found (by the `wrapped_composition_tag` view tag Compose sets on every one, or the kept `findViewByAccessibilityIdTraversal(int)` override) and gets its Window; the diagnostics then say why it has no nodes instead of reporting `found 0`. Semantics values, composable parameters and modifier arguments are stringified without calling the app's `toString()` (`SafeString.kt`): functions print `<lambda>`, an `AccessibilityAction` its label or `<action>`, unknown classes their simple name.
- `DumpComposeResponse.diagnostics` tokens (separated by `; `; stable prefixes): `found N AndroidComposeView(s)`; `compose_obfuscated: Compose present but classes are renamed (AndroidComposeView is <cls>), semantics/slot table unavailable, a11y still works`; `semantics_failed: view#<acvId> classes_renamed|owner_unreachable|root_unreachable|error=<Throwable>` (that ComposeView has no semantics tree); `semantics_partial: view#<acvId> nodes_failed=N values_failed=M` (the tree is there; the failed values read `<error:Name>`); `semantics_truncated: view#<acvId> depth>80 subtrees=N`; `slot_failed: view#<acvId> error=<Throwable>`; `slot_partial: view#<acvId> groups_failed=N`; `slot_truncated: view#<acvId> depth>80 subtrees=N`; `slot table empty (inspection_slot_table_set not populated) for N/M view(s)`; `view#<id>[,view#<id>] produced no compose nodes`. `enable_inspection` cannot help a `semantics_failed` or `compose_obfuscated` view (it only populates the slot table, by names R8 renamed).
- All bounds (`ViewNode`, `A11yNode`, `ComposeNode`) are SCREEN px. Compose's window-relative bounds are shifted by the window's on-screen origin agent-side, and so are the a11y bounds the in-process connection returns window-relative for a window away from the screen origin (a dialog; diagnostics `window-offset=dx,dy`).
- API < 34 (or when query-from-app-process fails) the a11y walk resolves child ids locally the way `AccessibilityInteractionController` does, so provider (Compose) content is kept; the diagnostics say `local-fallback` for that root.
