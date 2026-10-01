/*
 * ViewSpector — payload core module.
 *
 * Command router. Switches on Request.commandCase, performs the work (hopping to
 * the main thread for anything that touches live View state), and builds a
 * Response that echoes Request.id with status OK, or status ERROR + message when
 * an exception escapes.
 *
 * Wiring per the module spec / CONTRACT.md §5–6:
 *   HELLO          -> agent_version / api_level / abi
 *   GET_WINDOWS    -> RootsDetector.rootIds() (main thread) + a fresh StringTable
 *   DUMP_TREE      -> TreeBuilder.buildRoots(rootId) (main thread); optionally a
 *                     PropertyGroup per visited view and/or a screenshot
 *   GET_PROPERTIES -> find view by id (walk roots) + Properties.forView
 *   SCREENSHOT     -> Capture.screenshot
 *   DUMP_A11Y      -> AccessibilityInspector.dump (main thread, event tap lifted)
 *   A11Y_FOCUS     -> A11yFocus.await (long-poll, this thread, no device lock) then
 *                     A11yFocus.read (main thread) + the event tap's records
 *   A11Y_ACT       -> A11yFocus.act (main thread)
 *   SHUTDOWN       -> ShutdownResponse; the Server stops after writing it
 *
 * The reference SessionHandler (ui-inspector SessionHandler.kt:96-160) is the
 * model for the "every command must produce exactly one response, exceptions
 * become ERROR replies, SHUTDOWN terminates after replying" structure.
 */
package com.oberkfell.viewspector.agent.payload

import android.os.Build
import android.util.Log
import android.view.View
import com.oberkfell.viewspector.proto.ViewInspection

/**
 * Handles one [ViewInspection.Request] at a time and returns its
 * [ViewInspection.Response]. Stateless.
 *
 * SHUTDOWN only builds the reply: the [Server] writes it and then stops.
 * (Stopping here, before the write, closed the requester's socket first, so
 * the host never saw the ShutdownResponse.)
 *
 * [deviceLock] is the Server's lock that serializes device work across
 * connections. The Server takes it around [handle] unless [takesDeviceLock]
 * says no: HELLO and SHUTDOWN touch no UI, and A11Y_FOCUS takes it itself
 * only after its long-poll, so a waiting client never holds up the others.
 */
class Dispatcher(private val deviceLock: Any = Any()) {

    private companion object {
        const val TAG = "ViewSpector"

        // CONTRACT.md §3 / module spec: HELLO advertises this agent version,
        // plus "+<build id>" (Payload.buildId: sha256 of the loaded payload.jar)
        // so the host can tell a stale agent from the build it would inject.
        const val AGENT_VERSION = "viewspector-0.1"

        // Default screenshot scale when a DumpTreeCommand leaves it unset (0f).
        const val DEFAULT_SCREENSHOT_SCALE = 1.0f

        // DUMP_TREE reads properties this many Views per main-thread hop, so a big tree
        // never blocks the UI thread for the whole extraction (other messages, input and
        // frames run between batches).
        const val PROPERTY_BATCH = 48
    }

    /** False for the commands the Server must run without holding [deviceLock]. */
    fun takesDeviceLock(req: ViewInspection.Request): Boolean =
        when (req.commandCase) {
            ViewInspection.Request.CommandCase.HELLO,
            ViewInspection.Request.CommandCase.SHUTDOWN,
            ViewInspection.Request.CommandCase.A11Y_FOCUS -> false
            else -> true
        }

    /**
     * Dispatches [req] to the right handler and returns the response. Any
     * exception thrown by a handler is caught and converted into an ERROR
     * response so a single bad command can never tear down the connection.
     */
    fun handle(req: ViewInspection.Request): ViewInspection.Response {
        val id = req.id
        MainThread.setCommandTimeout(mainThreadTimeoutMs(req.commandCase))
        return try {
            when (req.commandCase) {
                ViewInspection.Request.CommandCase.HELLO -> handleHello(id)
                ViewInspection.Request.CommandCase.GET_WINDOWS -> handleGetWindows(id)
                ViewInspection.Request.CommandCase.DUMP_TREE -> handleDumpTree(id, req.dumpTree)
                ViewInspection.Request.CommandCase.GET_PROPERTIES ->
                    handleGetProperties(id, req.getProperties)
                ViewInspection.Request.CommandCase.SCREENSHOT ->
                    handleScreenshot(id, req.screenshot)
                ViewInspection.Request.CommandCase.DUMP_COMPOSE ->
                    handleDumpCompose(id, req.dumpCompose)
                ViewInspection.Request.CommandCase.CAPTURE_SKP ->
                    handleCaptureSkp(id, req.captureSkp)
                ViewInspection.Request.CommandCase.DUMP_A11Y ->
                    handleDumpA11y(id, req.dumpA11Y)
                ViewInspection.Request.CommandCase.A11Y_FOCUS ->
                    handleA11yFocus(id, req.a11YFocus)
                ViewInspection.Request.CommandCase.A11Y_ACT ->
                    handleA11yAct(id, req.a11YAct)
                ViewInspection.Request.CommandCase.SHUTDOWN -> handleShutdown(id)
                ViewInspection.Request.CommandCase.COMMAND_NOT_SET ->
                    error(id, "No command set in request")
            }
        } catch (t: Throwable) {
            Log.e(TAG, "Error handling command ${req.commandCase}", t)
            error(id, describe(t))
        }
    }

    /**
     * How long this command's main-thread hops may wait: whole-tree walks get longer than a
     * lookup, but every budget stays under the host's deadline for that command (client.py
     * request_timeout: 30 s, 4x for a11y/Compose/properties), so a slow main thread yields an
     * ERROR reply rather than a host timeout that drops the connection.
     */
    private fun mainThreadTimeoutMs(command: ViewInspection.Request.CommandCase): Long =
        when (command) {
            ViewInspection.Request.CommandCase.DUMP_TREE -> MainThread.TREE_TIMEOUT_MS
            ViewInspection.Request.CommandCase.DUMP_A11Y,
            ViewInspection.Request.CommandCase.DUMP_COMPOSE -> MainThread.HEAVY_TIMEOUT_MS
            else -> MainThread.DEFAULT_TIMEOUT_MS
        }

    // ------------------------------------------------------------------ HELLO

    private fun handleHello(id: Int): ViewInspection.Response {
        val hello =
            ViewInspection.HelloResponse.newBuilder()
                .setAgentVersion("$AGENT_VERSION+${Payload.buildId}")
                .setApiLevel(Build.VERSION.SDK_INT)
                .setAbi(primaryAbi())
                .build()
        return ok(id).setHello(hello).build()
    }

    private fun primaryAbi(): String =
        try {
            // Build.SUPPORTED_ABIS[0] is the device's primary ABI (arm64-v8a on
            // the target emulator). Guard against an empty array defensively.
            Build.SUPPORTED_ABIS.firstOrNull() ?: ""
        } catch (t: Throwable) {
            Log.w(TAG, "Build.SUPPORTED_ABIS unavailable", t)
            ""
        }

    // ------------------------------------------------------------- GET_WINDOWS

    private fun handleGetWindows(id: Int): ViewInspection.Response {
        // RootsDetector touches live View state, so enumerate on the main thread.
        val rootIds: List<Long> = MainThread.run { RootsDetector.rootIds() }

        // A fresh StringTable per response (proto string-table is per-message).
        // GET_WINDOWS carries no interned strings today, but the protocol field
        // is present and the host expects a (possibly empty) Strings message.
        val strings = StringTable()

        val getWindows =
            ViewInspection.GetWindowsResponse.newBuilder()
                .addAllRootIds(rootIds)
                .setStrings(strings.build())
                .build()
        return ok(id).setGetWindows(getWindows).build()
    }

    // --------------------------------------------------------------- DUMP_TREE

    private fun handleDumpTree(
        id: Int,
        cmd: ViewInspection.DumpTreeCommand,
    ): ViewInspection.Response {
        val rootId = cmd.rootId
        val includeProperties = cmd.includeProperties
        val includeResolutionStack = cmd.includeResolutionStack
        val includeScreenshot = cmd.includeScreenshot
        val scale = effectiveScale(cmd.screenshotScale)

        val strings = StringTable()
        val treeBuilder = TreeBuilder(strings)
        val properties = if (includeProperties) Properties(strings) else null
        val diag = ArrayList<String>()

        // The tree and the screenshot root come from one main-thread hop, so they are
        // consistent (no addView/removeView can race between them).
        val assembled =
            MainThread.run {
                val roots: List<ViewInspection.ViewNode> = treeBuilder.buildRoots(rootId)
                // Exactly the Views the tree emitted (in pre-order), so every property group
                // belongs to a node in the response.
                val views: List<View> = ArrayList(treeBuilder.visited)
                val firstRoot: View? = selectRootViews(rootId).firstOrNull()
                // What each View showed now, to spot the ones a later property hop finds changed.
                val stamps = if (includeProperties) IntArray(views.size) { ViewStamp.of(views[it]) } else null
                DumpTreeWork(roots, views, firstRoot, stamps)
            }
        if (treeBuilder.truncatedNodes > 0) {
            diag.add(
                "depth-truncated=${treeBuilder.truncatedNodes} (children below " +
                    "${WireLimits.MAX_TREE_DEPTH} levels not sent)",
            )
        }

        // Properties are read in batches, one main-thread hop each (see PROPERTY_BATCH). A
        // View detached between hops still reads its last state; one that changed between the
        // tree hop and its batch (a rebound list cell) is counted: properties-changed=N.
        val propertyGroups: List<ViewInspection.PropertyGroup> =
            if (properties != null) {
                readPropertyGroups(properties, assembled.views, assembled.stamps, includeResolutionStack, diag)
            } else {
                emptyList()
            }

        // Screenshot capture must NOT run on the main thread; Capture issues its
        // PixelCopy request on its own Handler thread (CONTRACT.md §6). The
        // firstRoot View reference resolved above is used here off-thread.
        val screenshot: ViewInspection.Screenshot? =
            if (includeScreenshot && assembled.firstRoot != null) {
                try {
                    Capture.screenshot(assembled.firstRoot, scale)
                } catch (t: Throwable) {
                    Log.w(TAG, "Screenshot capture failed during DUMP_TREE", t)
                    null
                }
            } else {
                null
            }

        val dumpTree =
            ViewInspection.DumpTreeResponse.newBuilder()
                .addAllRoots(assembled.roots)
                .addAllProperties(propertyGroups)
                .setStrings(strings.build())
                .apply { if (screenshot != null) setScreenshot(screenshot) }
                .apply { if (diag.isNotEmpty()) setDiagnostics(diag.joinToString("; ")) }
                .build()
        return ok(id).setDumpTree(dumpTree).build()
    }

    /**
     * Property groups for [views], [PROPERTY_BATCH] per main-thread hop. A view whose
     * properties throw is skipped (Properties.forViewOrNull) and counted. If a batch never
     * got the main thread in time, the dump returns the groups read so far and says so; if
     * one started but overran, nothing can be returned (it still writes the shared string
     * table), so the timeout propagates as an ERROR. A View whose [stamps] entry (ViewStamp,
     * taken in the tree hop) no longer matches is counted: its properties describe a later
     * state than its node ("properties-changed=N").
     */
    private fun readPropertyGroups(
        properties: Properties,
        views: List<View>,
        stamps: IntArray?,
        includeResolutionStack: Boolean,
        diag: MutableList<String>,
    ): List<ViewInspection.PropertyGroup> {
        val out = ArrayList<ViewInspection.PropertyGroup>(views.size)
        var changed = 0
        var next = 0
        while (next < views.size) {
            val start = next
            val batch = views.subList(next, minOf(next + PROPERTY_BATCH, views.size))
            val groups =
                try {
                    MainThread.run {
                        if (stamps != null) {
                            for (i in batch.indices) if (ViewStamp.of(batch[i]) != stamps[start + i]) changed++
                        }
                        batch.mapNotNull { properties.forViewOrNull(it, includeResolutionStack) }
                    }
                } catch (e: MainThread.MainThreadTimeoutException) {
                    if (e.started) throw e
                    diag.add("properties-incomplete: the main thread was busy; read $next of ${views.size} views")
                    break
                }
            out.addAll(groups)
            next += batch.size
        }
        if (changed > 0) {
            diag.add("properties-changed=$changed (views that changed between the tree read and their property read)")
        }
        if (properties.failedViews > 0) diag.add("properties-failed-views=${properties.failedViews}")
        if (properties.failedProperties > 0) diag.add("properties-failed=${properties.failedProperties}")
        return out
    }

    private fun handleCaptureSkp(
        id: Int,
        cmd: ViewInspection.CaptureSkpCommand,
    ): ViewInspection.Response {
        val root = MainThread.run { selectRootViews(cmd.rootId).firstOrNull() }
        val resp = ViewInspection.CaptureSkpResponse.newBuilder()
        if (root == null) {
            resp.supported = true
            resp.error = "no root view found for id ${cmd.rootId}"
            return ok(id).setCaptureSkp(resp).build()
        }
        // captureSkp invalidates on the main thread internally and blocks (off-main) for a frame.
        val result = Capture.captureSkp(root)
        resp.supported = result.supported
        result.error?.let { resp.error = it }
        result.skp?.let {
            resp.skp = it
            resp.version = readSkpVersion(it)
        }
        return ok(id).setCaptureSkp(resp).build()
    }

    /**
     * DUMP_A11Y — the unified AccessibilityNodeInfo tree (classic Views + Compose
     * virtual semantics nodes), exactly as Assistive Technology observes it.
     *
     * All ANI work runs on the main thread in a single hop (it reads live View /
     * ANI state); unlike screenshots there is no off-thread step. AccessibilityInspector
     * obtains each root's host node, enables app-process query mode, and recurses
     * via getChild() (which transparently covers Compose). See the a11y design spec
     * §1/§7 and AccessibilityInspector.kt.
     */
    private fun handleDumpA11y(
        id: Int,
        cmd: ViewInspection.DumpA11yCommand,
    ): ViewInspection.Response {
        val rootId = cmd.rootId
        // include_extras / include_rendering_info are taken as sent. The host's
        // client defaults include_extras=true and include_rendering_info=false
        // (design §5), so proto3's lack of bool presence is a non-issue here.
        val includeExtras = cmd.includeExtras
        val includeRenderingInfo = cmd.includeRenderingInfo

        val strings = StringTable()

        val (windows, diag) =
            MainThread.run {
                val roots: List<View> = selectRootViews(rootId)
                // The event tap's delegate on each root would make the root report itself
                // important for accessibility; dump what the app has (A11yEventTap).
                A11yEventTap.withoutTap {
                    AccessibilityInspector.dump(
                        roots, strings, includeExtras, includeRenderingInfo, RootsDetector.rootViews(),
                    )
                }
            }

        val resp =
            ViewInspection.DumpA11yResponse.newBuilder()
                .addAllWindows(windows)
                .setStrings(strings.build())
                .setDiagnostics(diag)
                .build()
        return ok(id).setDumpA11Y(resp).build()
    }

    /**
     * A11Y_FOCUS: where accessibility focus is, plus the events the tap recorded after
     * after_seq. With wait_ms > 0 this (server) thread first long-polls for the next focus
     * event WITHOUT the device lock (A11yFocus.await); the read itself is one main-thread hop
     * under the lock. The seq returned is the tap's seq at the moment of the read, so a focus
     * event after the read is always newer than it (the next long-poll sees it).
     */
    private fun handleA11yFocus(
        id: Int,
        cmd: ViewInspection.A11yFocusCommand,
    ): ViewInspection.Response {
        val wait = A11yFocus.await(cmd)
        val strings = StringTable()
        val read = synchronized(deviceLock) {
            MainThread.run { A11yFocus.read(strings, cmd.subtreeDepth, cmd.includeInputFocus) }
        }
        val snap = A11yEventTap.eventsAfter(cmd.afterSeq, cmd.maxEvents, read.seq)
        val resp = ViewInspection.A11yFocusResponse.newBuilder()
        read.a11y?.let { resp.setA11Y(it) }
        read.input?.let { resp.setInput(it) }
        for (r in snap.events) resp.addEvents(A11yEventTap.toProto(r, strings))
        val diag = StringBuilder(read.diagnostics)
        diag.append("; read=${read.readUs}us")
        if (cmd.waitMs > 0) diag.append("; waited=${wait.waitedMs}ms")
        wait.note?.let { diag.append("; ").append(it) }
        if (snap.dropped > 0) diag.append("; events-dropped=${snap.dropped}")
        // Text masked for want of a password status (Redaction.kt): the focus read's, then the
        // events' (by seq).
        for (r in snap.events) if (r.textUnverified) read.redaction.masked("event:${r.seq}")
        read.redaction.appendTo(diag)
        resp.setSeq(read.seq)
            .setDropped(snap.dropped)
            .setFocusEvent(wait.focusEvent)
            .setTimedOut(wait.timedOut)
            .setWaitedMs(wait.waitedMs)
            .setReadUs(read.readUs)
            .setReadUptimeMs(read.uptimeMs)
            .setTouchExploration(read.touchExploration)
            .setServicesEnabled(read.servicesEnabled)
            .setDiagnostics(diag.toString())
            .setStrings(strings.build())
        return ok(id).setA11YFocus(resp).build()
    }

    /**
     * A11Y_ACT: perform one accessibility action on a node (A11yFocus.act) and report the
     * focus right after it. A refusal is an OK response with performed=false and error set.
     */
    private fun handleA11yAct(
        id: Int,
        cmd: ViewInspection.A11yActCommand,
    ): ViewInspection.Response {
        val strings = StringTable()
        val act = MainThread.run { A11yFocus.act(cmd, strings) }
        val resp = ViewInspection.A11yActResponse.newBuilder()
            .setPerformed(act.performed)
            .setActionId(act.actionId)
            .setSeqBefore(act.seqBefore)
            .setSeq(act.seq)
            .setDiagnostics(act.diagnostics)
        act.error?.let { resp.setError(it) }
        act.after?.let { resp.setAfter(it) }
        resp.setStrings(strings.build())
        return ok(id).setA11YAct(resp).build()
    }

    /** Read the SKP version int from a serialized SkPicture: "skiapict" magic then LE uint32. */
    private fun readSkpVersion(skp: com.google.protobuf.ByteString): Int {
        val magic = "skiapict".toByteArray(Charsets.US_ASCII)
        if (skp.size() < 12) return 0
        for (i in magic.indices) if (skp.byteAt(i) != magic[i]) return 0
        return (skp.byteAt(8).toInt() and 0xFF) or
            ((skp.byteAt(9).toInt() and 0xFF) shl 8) or
            ((skp.byteAt(10).toInt() and 0xFF) shl 16) or
            ((skp.byteAt(11).toInt() and 0xFF) shl 24)
    }

    private fun handleDumpCompose(
        id: Int,
        cmd: ViewInspection.DumpComposeCommand,
    ): ViewInspection.Response {
        val rootId = cmd.rootViewId
        val includeSemantics = cmd.includeSemantics || (!cmd.includeSemantics && !cmd.includeSlotTable)
        val includeSlot = cmd.includeSlotTable || (!cmd.includeSemantics && !cmd.includeSlotTable)
        val strings = StringTable()

        // Optionally enable Compose inspection (flag + slot-table storage + hot reload) so the
        // slot table populates. The hot-reload recomposition runs on the main thread, so we kick
        // it off in a main-thread hop, then wait OFF the main thread for the next frame(s) to
        // populate the tables before reading them.
        if (cmd.enableInspection && includeSlot) {
            val added = MainThread.run { ComposeInspector.enableInspection(selectRootViews(rootId)) }
            if (added > 0) {
                try { Thread.sleep(1000) } catch (_: InterruptedException) {}
            }
        }

        val (windows, diag) =
            MainThread.run {
                val roots: List<View> = selectRootViews(rootId)
                ComposeInspector.dump(roots, strings, includeSemantics, includeSlot)
            }

        val resp = ViewInspection.DumpComposeResponse.newBuilder()
            .addAllWindows(windows)
            .setStrings(strings.build())
            .setDiagnostics(diag)
            .build()
        return ok(id).setDumpCompose(resp).build()
    }

    /** Carries the main-thread results of a DUMP_TREE across the thread hop. */
    private class DumpTreeWork(
        val roots: List<ViewInspection.ViewNode>,
        val views: List<View>,
        val firstRoot: View?,
        val stamps: IntArray?,
    )

    // ---------------------------------------------------------- GET_PROPERTIES

    private fun handleGetProperties(
        id: Int,
        cmd: ViewInspection.GetPropertiesCommand,
    ): ViewInspection.Response {
        val viewId = cmd.viewId
        val includeResolutionStack = cmd.includeResolutionStack

        val strings = StringTable()
        val properties = Properties(strings)

        val group: ViewInspection.PropertyGroup? =
            MainThread.run {
                val view = findViewById(RootsDetector.rootViews(), viewId)
                view?.let { properties.forView(it, includeResolutionStack) }
            }

        if (group == null) {
            return error(id, "No view found with id $viewId")
        }

        val getProperties =
            ViewInspection.GetPropertiesResponse.newBuilder()
                .setGroup(group)
                .setStrings(strings.build())
                .build()
        return ok(id).setGetProperties(getProperties).build()
    }

    // -------------------------------------------------------------- SCREENSHOT

    private fun handleScreenshot(
        id: Int,
        cmd: ViewInspection.ScreenshotCommand,
    ): ViewInspection.Response {
        val rootId = cmd.rootId
        val scale = effectiveScale(cmd.scale)

        // Resolve the target root View on the main thread, then capture off it.
        val root: View = MainThread.run { selectRootViews(rootId).firstOrNull() }
            ?: return error(id, "No root view found for id $rootId")

        val screenshot = Capture.screenshot(root, scale)

        val response =
            ViewInspection.ScreenshotResponse.newBuilder()
                .setScreenshot(screenshot)
                .build()
        return ok(id).setScreenshot(response).build()
    }

    // ---------------------------------------------------------------- SHUTDOWN

    private fun handleShutdown(id: Int): ViewInspection.Response =
        // Server.serveConnection writes this reply, then stops the server.
        ok(id).setShutdown(ViewInspection.ShutdownResponse.getDefaultInstance()).build()

    // ----------------------------------------------------------------- helpers

    /**
     * Resolves the live root Views a request targets. MUST be called on the main
     * thread. rootId 0 means "all roots"; otherwise only the root whose
     * uniqueDrawingId matches is returned (empty if none matches).
     */
    private fun selectRootViews(rootId: Long): List<View> {
        val roots = RootsDetector.rootViews()
        if (rootId == 0L) return roots
        return roots.filter { ViewReflect.uniqueDrawingId(it) == rootId }
    }

    /** A DumpTree/Screenshot scale of 0 (unset) defaults to 1.0; clamp to <=1.0. */
    private fun effectiveScale(raw: Float): Float {
        if (raw <= 0f) return DEFAULT_SCREENSHOT_SCALE
        // CONTRACT.md §5: scale applied at capture is <= 1.0.
        return if (raw > 1.0f) 1.0f else raw
    }

    private fun ok(id: Int): ViewInspection.Response.Builder =
        ViewInspection.Response.newBuilder()
            .setId(id)
            .setStatus(ViewInspection.Response.Status.OK)

    private fun error(id: Int, message: String): ViewInspection.Response =
        ViewInspection.Response.newBuilder()
            .setId(id)
            .setStatus(ViewInspection.Response.Status.ERROR)
            .setError(message)
            .build()

    private fun describe(t: Throwable): String {
        val msg = t.message
        return if (msg.isNullOrEmpty()) t.javaClass.simpleName else "${t.javaClass.simpleName}: $msg"
    }
}
