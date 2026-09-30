/*
 * ViewSpector — clean-room re-implementation of Android Studio's View Layout Inspector.
 *
 * PAYLOAD :: Compose extraction by pure reflection (no compile-time Compose dependency).
 *
 * The payload runs inside the app classloader of a debuggable app that bundles
 * androidx.compose.ui (+ tooling-data). We reach Compose data two ways, both reflectively
 * against classes resolved from the live AndroidComposeView's own classloader:
 *
 *   A. SEMANTICS TREE (reliable; always present, backs accessibility — no setup needed):
 *      AndroidComposeView.getSemanticsOwner() -> SemanticsOwner.getRootSemanticsNode()
 *      -> recurse getChildren(); per node getBoundsInWindow():Rect and iterate
 *      getConfig():SemanticsConfiguration (Iterable<Map.Entry<SemanticsPropertyKey,Any?>>).
 *      Symbols verified in androidx source: SemanticsOwner.kt:47, SemanticsNode.kt:174/242/321,
 *      SemanticsConfiguration.kt:31-65, SemanticsProperties.kt:420; same entry the shipped
 *      inspector uses (ui-inspection LayoutInspectorTree.kt:108-109).
 *
 *   B. SLOT TABLE (conditional; composable names + file:line via the app's bundled
 *      androidx.compose.ui.tooling.data): read the View tag inspection_slot_table_set ->
 *      Set<CompositionData>, then SlotTreeKt.asTree(CompositionData):Group and walk Group
 *      (getName/getBox/getLocation/getChildren). Only populated when isDebugInspectorInfoEnabled
 *      was true at composition time (Wrapper.android.kt:78). Best-effort; reported in diagnostics.
 *
 * DISCOVERY: every AndroidComposeView under the window roots gets its own Window, including
 * ones nested inside interop Views (AndroidView -> AndroidViewsHandler -> ... -> ComposeView),
 * inside RecyclerView cells, and at any depth. Window.view_id is that AndroidComposeView's
 * uniqueDrawingId, which is also the host_view_id of its accessibility node.
 *
 * COORDINATES: every ComposeNode bound is in SCREEN px, the same space as ViewNode.bounds and
 * A11yNode.bounds (boundsInScreen). Compose reports semantics and slot-table bounds in window
 * px (SemanticsNode.boundsInWindow, ui-tooling-data boundsOfLayoutNode -> positionInWindow), so
 * each is shifted by the window's on-screen origin (getLocationOnScreen - getLocationInWindow of
 * the AndroidComposeView). For a full-screen activity window that shift is 0; for dialogs and
 * popups it is not.
 *
 * IDS: a SEMANTICS node's id is its SemanticsNode id, which is also the virtual id of the matching
 * accessibility node under the same AndroidComposeView. Compose mints these from a process-wide
 * counter (SemanticsModifierKt.generateSemanticsId in ui 1.7 through 1.12) but re-mints them when a
 * LayoutNode is reused, and an id only means something relative to its own SemanticsOwner, so the
 * host keys it as compose:<acvId>:<semanticsId>. The synthetic ROOT node of each Window (name
 * "AndroidComposeView", kind COMPOSABLE) carries id = the AndroidComposeView's uniqueDrawingId
 * (== Window.view_id); that number lives in a different space from semantics ids and can equal
 * one, so the host must key the root as composeview:<acvId>, never as compose:<id>. Semantics ids
 * are re-minted on recomposition (e.g. the hot reload enable_inspection triggers), so a key is
 * only valid for the dump that produced it. A SLOT-TABLE node (kind COMPOSABLE) has no semantics
 * id; it carries a negative id (<= -2, so it never meets a semantics id, the synthetic root's
 * positive id, or -1 = the host View) derived from the hash of its group's slot-table identity
 * (an anchor the slot table keeps for as long as the group lives), so the id is stable across
 * dumps of an unchanged composition (the same idea as Android Studio's anchor-hash ids). It is
 * unique within its Window, and the host keys it compose:<acvId>:<id> like a semantics node.
 *
 * DETECTION: an AndroidComposeView is recognised by class name, or structurally when R8 renamed
 * the class: Compose tags every AndroidComposeView with its WrappedComposition under the resource
 * id wrapped_composition_tag (resource names survive R8), and, when that id cannot be resolved, by
 * the kept override of the hidden View.findViewByAccessibilityIdTraversal(int) plus an
 * accessibility delegate. A renamed one gets a Window like any other; its semantics are then
 * usually unreachable (the SemanticsOwner/SemanticsNode methods are renamed too), which the
 * diagnostics say instead of reporting "found 0".
 *
 * VALUES: semantics values, composable parameters and modifier arguments are app objects; they are
 * stringified by [SafeString], which never runs an arbitrary toString(). Every semantics entry,
 * node and slot-table group is guarded on its own, so one bad value costs that value, never the
 * ComposeView; the failures are counted in the diagnostics.
 *
 * DIAGNOSTICS (tokens separated by "; ", stable prefixes for the host to match):
 *   found N AndroidComposeView(s) [(M nested in interop Views)]; bounds=screen
 *   compose_obfuscated: Compose present but classes are renamed (AndroidComposeView is <cls>),
 *       semantics/slot table unavailable, a11y still works
 *   semantics_failed: view#<acvId> <reason>   reason = classes_renamed | owner_unreachable |
 *       root_unreachable | error=<Throwable>   (no semantics tree for that ComposeView)
 *   semantics_partial: view#<acvId> nodes_failed=N values_failed=M   (the tree is there; N nodes
 *       lost a part, M attribute values read "<error:...>")
 *   semantics_truncated: view#<acvId> depth>80 subtrees=N
 *   slot_failed: view#<acvId> error=<Throwable>
 *   slot_partial: view#<acvId> groups_failed=N
 *   slot_truncated: view#<acvId> depth>80 subtrees=N   (named-composable nesting; or raw>512)
 *   slot table empty (inspection_slot_table_set not populated) for N/M view(s)
 *   view#<acvId>[,view#<acvId>...] produced no compose nodes
 */
package com.oberkfell.viewspector.agent.payload

import android.util.Log
import android.view.View
import android.view.ViewGroup
import com.oberkfell.viewspector.proto.ViewInspection
import java.lang.reflect.InvocationTargetException
import java.lang.reflect.Method

object ComposeInspector {

    private const val TAG = "ViewSpector"
    private const val ANDROID_COMPOSE_VIEW = "androidx.compose.ui.platform.AndroidComposeView"

    // Walk cap for the unmerged semantics index (no proto is built from it).
    private const val MAX_DEPTH = 400

    // Nesting caps for what goes on the wire: Response > DumpComposeResponse > Window > root node
    // > 80 levels > Bounds > Rect stays under the ~100-message nesting limit of the host's
    // protobuf parser (a deeper tree makes the WHOLE response unparseable). Deeper subtrees are
    // cut and counted (semantics_truncated / slot_truncated).
    private const val SEM_EMIT_MAX_DEPTH = 80
    private const val SLOT_MAX_DEPTH = 80

    // Raw slot-table groups nest far deeper than the named composables emitted from them; this
    // only guards the recursion.
    private const val SLOT_RAW_MAX_DEPTH = 512

    // Entries read from one SemanticsConfiguration (a real one holds a few dozen).
    private const val MAX_CONFIG_ENTRIES = 256

    private const val CLASSES_RENAMED = "classes_renamed"

    /** Per-ComposeView counters and log throttling for one dump. */
    private class WalkCtx {
        var semNodeFailures = 0
        var semValueFailures = 0
        var semTruncated = 0
        var slotGroupFailures = 0
        var slotTruncated = 0
        val slotIds = SlotIds()
        private val logged = HashSet<String>()

        /** Log the first failure of each [kind] (per ComposeView), without a second failure. */
        fun log(kind: String, t: Throwable) {
            if (!logged.add(kind)) return
            try {
                Log.w(TAG, "compose: $kind failed (further failures counted only)", t)
            } catch (_: Throwable) {
                Log.w(TAG, "compose: $kind failed: ${SafeString.errorName(t)}")
            }
        }
    }

    /**
     * Build a Compose window per AndroidComposeView found under [rootViews]. Call on the main thread.
     * Returns the windows plus a diagnostics string describing what was reachable (the tokens are
     * listed in the header, DIAGNOSTICS).
     */
    fun dump(
        rootViews: List<View>,
        strings: StringTable,
        includeSemantics: Boolean,
        includeSlotTable: Boolean,
    ): Pair<List<ViewInspection.DumpComposeResponse.Window>, String> {
        val composeViews = ArrayList<View>()
        val nested = intArrayOf(0)
        for (root in rootViews) collectComposeViews(root, composeViews, nested, false, 0)
        val diag = StringBuilder()
        diag.append("found ${composeViews.size} AndroidComposeView(s)")
        if (nested[0] > 0) diag.append(" (${nested[0]} nested in interop Views)")
        diag.append("; bounds=screen")

        // Per-view problems: one token per ComposeView (a RecyclerView of ComposeView cells can
        // mean dozens of windows), aggregated lists at the end.
        val tokens = ArrayList<String>()
        val slotEmpty = ArrayList<Long>()
        val noNodes = ArrayList<Long>()
        val renamedClasses = LinkedHashSet<String>()
        var renamedEmpty = 0

        val windows = ArrayList<ViewInspection.DumpComposeResponse.Window>()
        for (cv in composeViews) {
            val acvId = cv.uniqueDrawingId
            val renamed = isRenamed(cv)
            if (renamed) renamedClasses.add(cv.javaClass.name)
            val ctx = WalkCtx()
            // Window px -> screen px shift for everything Compose reports in window coordinates.
            val off = windowOriginOnScreen(cv)
            // Synthetic root: id = the AndroidComposeView's uniqueDrawingId (== Window.view_id).
            // NOT a semantics id; the host keys it composeview:<acvId> (see the header, IDS). Its
            // name stays "AndroidComposeView" for a renamed class too: the host matches on it.
            val rootNode = ViewInspection.ComposeNode.newBuilder()
            rootNode.id = acvId
            rootNode.name = strings.intern("AndroidComposeView")
            rootNode.kind = ViewInspection.ComposeNode.Kind.COMPOSABLE
            boundsOf(cv)?.let { rootNode.bounds = it }

            var produced = false
            if (includeSemantics) {
                val reason = try {
                    val (semRoot, missing) = semanticsRoot(cv)
                    if (semRoot == null) {
                        if (renamed) CLASSES_RENAMED else missing
                    } else {
                        rootNode.addChildren(buildSemanticsNode(semRoot, strings, 0, off, ctx))
                        produced = true
                        null
                    }
                } catch (t: Throwable) {
                    ctx.log("semantics walk", t)
                    "error=${SafeString.errorName(t)}"
                }
                if (reason != null) tokens.add("semantics_failed: view#$acvId $reason")
                if (ctx.semNodeFailures > 0 || ctx.semValueFailures > 0) {
                    tokens.add(
                        "semantics_partial: view#$acvId nodes_failed=${ctx.semNodeFailures} " +
                            "values_failed=${ctx.semValueFailures}",
                    )
                }
                if (ctx.semTruncated > 0) {
                    tokens.add("semantics_truncated: view#$acvId depth>$SEM_EMIT_MAX_DEPTH subtrees=${ctx.semTruncated}")
                }
            }
            if (includeSlotTable) {
                try {
                    val groups = slotTableGroups(cv, ctx)
                    if (groups.isEmpty()) {
                        slotEmpty.add(acvId)
                    } else {
                        for ((i, g) in groups.withIndex()) {
                            try {
                                for (n in buildSlotChildren(g, strings, 0, 0, off, 0L, i, ctx)) {
                                    rootNode.addChildren(n); produced = true
                                }
                            } catch (t: Throwable) {
                                ctx.slotGroupFailures++
                                ctx.log("slot-table composition", t)
                            }
                        }
                    }
                } catch (t: Throwable) {
                    ctx.log("slot-table walk", t)
                    tokens.add("slot_failed: view#$acvId error=${SafeString.errorName(t)}")
                }
                if (ctx.slotGroupFailures > 0) {
                    tokens.add("slot_partial: view#$acvId groups_failed=${ctx.slotGroupFailures}")
                }
                if (ctx.slotTruncated > 0) {
                    tokens.add("slot_truncated: view#$acvId depth>$SLOT_MAX_DEPTH subtrees=${ctx.slotTruncated}")
                }
            }

            windows.add(
                ViewInspection.DumpComposeResponse.Window.newBuilder()
                    .setViewId(acvId)
                    .setRoot(rootNode.build())
                    .build()
            )
            if (!produced) {
                noNodes.add(acvId)
                if (renamed) renamedEmpty++
            }
        }
        if (renamedEmpty > 0) {
            val names = renamedClasses.joinToString("/")
            diag.append(
                "; compose_obfuscated: Compose present but classes are renamed (AndroidComposeView is " +
                    "$names), semantics/slot table unavailable, a11y still works",
            )
        }
        for (t in tokens) diag.append("; ").append(t)
        if (slotEmpty.isNotEmpty()) {
            diag.append(
                "; slot table empty (inspection_slot_table_set not populated) for " +
                    "${slotEmpty.size}/${composeViews.size} view(s)"
            )
        }
        if (noNodes.isNotEmpty()) {
            diag.append("; view#${noNodes.joinToString(",view#")} produced no compose nodes")
        }
        return windows to diag.toString()
    }

    /**
     * Enable Compose inspection so the slot table populates: set isDebugInspectorInfoEnabled,
     * add slot-table storage to each AndroidComposeView, then hot-reload to force a fresh
     * composition (which fills the tables). Replicates ComposeLayoutInspector.enableInspection +
     * addSlotTable (framework/ViewExtensions.kt) + hotReload. MUST run on the main thread.
     * Returns the number of slot tables newly added (0 => nothing to do / already enabled).
     * AndroidComposeViews whose class R8 renamed are skipped: the flag, HotReloader and the
     * tooling-data reader are all looked up by name, so nothing would populate their tables.
     */
    fun enableInspection(rootViews: List<View>): Int {
        val composeViews = ArrayList<View>()
        for (root in rootViews) collectComposeViews(root, composeViews, intArrayOf(0), false, 0)
        composeViews.removeAll { isRenamed(it) }
        if (composeViews.isEmpty()) return 0
        val cl = composeViews.first().javaClass.classLoader ?: return 0

        setDebugInspectorInfoEnabled(cl)

        var added = 0
        for (cv in composeViews) {
            val tagId = slotTableTagId(cv, cl)
            if (tagId == 0) continue
            if (cv.getTag(tagId) is Set<*>) continue
            try {
                val set = java.util.Collections.newSetFromMap(java.util.WeakHashMap<Any, Boolean>())
                cv.setTag(tagId, set)
                added++
            } catch (t: Throwable) {
                Log.w(TAG, "addSlotTable failed", t)
            }
        }
        if (added > 0) {
            try {
                hotReload(cl)
            } catch (t: Throwable) {
                Log.w(TAG, "hotReload failed", t)
            }
        }
        return added
    }

    private fun setDebugInspectorInfoEnabled(cl: ClassLoader) {
        try {
            val k = Class.forName("androidx.compose.ui.platform.InspectableValueKt", false, cl)
            val f = k.getDeclaredField("isDebugInspectorInfoEnabled")
            f.isAccessible = true
            f.setBoolean(null, true)
        } catch (t: Throwable) {
            Log.w(TAG, "could not set isDebugInspectorInfoEnabled", t)
        }
    }

    private fun hotReload(cl: ClassLoader) {
        val hotReload = Class.forName("androidx.compose.runtime.HotReloader", false, cl)
        val companion = hotReload.getField("Companion").get(null)
        val save = companion.javaClass.getDeclaredMethod("saveStateAndDispose", Any::class.java)
        val load = companion.javaClass.getDeclaredMethod("loadStateAndCompose", Any::class.java)
        save.isAccessible = true
        load.isAccessible = true
        val context = Class.forName("android.app.ActivityThread")
            .getDeclaredMethod("currentApplication")
            .apply { isAccessible = true }
            .invoke(null)
        val state = save.invoke(companion, context)
        load.invoke(companion, state)
    }

    // ---------------------------------------------------------------- view discovery
    // View-hierarchy depth cap for discovery (belt and braces; real hierarchies are far shallower).
    private const val VIEW_MAX_DEPTH = 400

    /**
     * Collect every AndroidComposeView under [view], in pre-order. We do NOT stop at an
     * AndroidComposeView: it is a ViewGroup whose AndroidViewsHandler child hosts the interop
     * Views of AndroidView { } (AndroidViewHolder), and those can contain further ComposeViews
     * (and RecyclerViews of ComposeView cells) to any depth. [nested] counts the ones found inside
     * another AndroidComposeView, for diagnostics.
     */
    private fun collectComposeViews(
        view: View,
        out: MutableList<View>,
        nested: IntArray,
        insideCompose: Boolean,
        depth: Int,
    ) {
        if (depth > VIEW_MAX_DEPTH) return
        val isAcv = isAndroidComposeView(view)
        if (isAcv) {
            out.add(view)
            if (insideCompose) nested[0]++
        }
        if (view is ViewGroup) {
            val n = try { view.childCount } catch (t: Throwable) { 0 }
            for (i in 0 until n) {
                val child = try { view.getChildAt(i) } catch (t: Throwable) { null } ?: continue
                collectComposeViews(child, out, nested, insideCompose || isAcv, depth + 1)
            }
        }
    }

    // Per-class caches (main thread only). byNameCache: a subclass of AndroidComposeView by name.
    // overrideCache: an app class that declares the hidden findViewByAccessibilityIdTraversal(int),
    // which AndroidComposeView overrides and R8 keeps (it overrides a framework method, and
    // Compose's consumer rules keep it); consulted only when the tag id cannot be resolved.
    private val byNameCache = HashMap<Class<*>, Boolean>()
    private val overrideCache = HashMap<Class<*>, Boolean>()

    // R.id.wrapped_composition_tag: -1 = not looked up yet, 0 = unavailable.
    private var wrappedTagId = -1

    /**
     * True when [view] is an AndroidComposeView: by class name, or structurally when R8 renamed
     * the class (see the header, DETECTION). Main thread.
     */
    internal fun isAndroidComposeView(view: View): Boolean {
        if (isByName(view.javaClass)) return true
        if (view !is ViewGroup) return false
        // Wrapper.android.kt doSetContent: owner.view.setTag(R.id.wrapped_composition_tag, ...)
        // on every AndroidComposeView. A SparseArray lookup; the value is never touched.
        val tagId = wrappedCompositionTagId(view)
        if (tagId != 0) {
            return try { view.getTag(tagId) != null } catch (_: Throwable) { false }
        }
        val overrides = overrideCache.getOrPut(view.javaClass) {
            try { declaresAccessibilityIdTraversal(view.javaClass) } catch (_: Throwable) { false }
        }
        return overrides && try { view.accessibilityDelegate != null } catch (_: Throwable) { false }
    }

    /** True for an AndroidComposeView found structurally, i.e. one whose class R8 renamed. */
    internal fun isRenamed(view: View): Boolean = !isByName(view.javaClass)

    private fun isByName(cls: Class<*>): Boolean = byNameCache.getOrPut(cls) {
        try { isAssignableToName(cls, ANDROID_COMPOSE_VIEW) } catch (_: Throwable) { false }
    }

    /** An app ViewGroup subclass in [cls]'s chain declares findViewByAccessibilityIdTraversal(int). */
    private fun declaresAccessibilityIdTraversal(cls: Class<*>): Boolean {
        if (!ViewGroup::class.java.isAssignableFrom(cls)) return false
        val boot = View::class.java.classLoader
        var k: Class<*>? = cls
        while (k != null && k.classLoader !== boot) {
            try {
                val m = k.getDeclaredMethod("findViewByAccessibilityIdTraversal", Int::class.javaPrimitiveType)
                if (View::class.java.isAssignableFrom(m.returnType)) return true
            } catch (_: Throwable) {
                // not declared here
            }
            k = k.superclass
        }
        return false
    }

    private fun wrappedCompositionTagId(view: View): Int {
        if (wrappedTagId != -1) return wrappedTagId
        var id = 0
        try {
            id = view.resources.getIdentifier("wrapped_composition_tag", "id", view.context.packageName)
        } catch (_: Throwable) {
            // no resources
        }
        if (id == 0) {
            id = try {
                Class.forName("androidx.compose.ui.R\$id", false, view.javaClass.classLoader)
                    .getField("wrapped_composition_tag").getInt(null)
            } catch (_: Throwable) {
                0
            }
        }
        wrappedTagId = id
        return id
    }

    /**
     * The on-screen position of [view]'s window origin: getLocationOnScreen - getLocationInWindow.
     * Adding it converts Compose's window px (boundsInWindow / positionInWindow) to screen px.
     */
    private fun windowOriginOnScreen(view: View): IntArray {
        return try {
            val onScreen = IntArray(2)
            val inWindow = IntArray(2)
            view.getLocationOnScreen(onScreen)
            view.getLocationInWindow(inWindow)
            intArrayOf(onScreen[0] - inWindow[0], onScreen[1] - inWindow[1])
        } catch (t: Throwable) {
            Log.w(TAG, "window origin unavailable; compose bounds stay window-relative", t)
            intArrayOf(0, 0)
        }
    }

    // ---------------------------------------------------------------- a11y support
    /**
     * Per-AndroidComposeView facts the accessibility walk needs but AccessibilityNodeInfo does not
     * carry. [rootSemanticsId] is the unmerged root SemanticsNode id (Compose exposes it as the
     * AndroidComposeView's own node, virtual id HOST_VIEW_ID); [traversalGroups] holds the ids of
     * unmerged SemanticsNodes whose config sets IsTraversalGroup = true. [layoutSizes] maps each
     * unmerged SemanticsNode id to the measured size of its LayoutNode (px, [packSize]): the space
     * the node reserves in layout, including Modifier.minimumInteractiveComponentSize() padding,
     * unlike its a11y boundsInScreen, which Compose widens to the 48dp touch size for any clickable.
     */
    internal class SemanticsIndex(
        val rootSemanticsId: Int,
        val traversalGroups: Set<Int>,
        val layoutSizes: Map<Int, Long>,
    )

    internal fun packSize(w: Int, h: Int): Long = (w.toLong() shl 32) or (h.toLong() and 0xFFFFFFFFL)

    /**
     * Build the [SemanticsIndex] for [composeView] by walking its UNMERGED semantics tree (the one
     * the accessibility delegate serves). Returns null when the semantics owner is unreachable.
     * Call on the main thread.
     */
    internal fun semanticsIndex(composeView: View): SemanticsIndex? {
        return try {
            val owner = invoke(composeView, "getSemanticsOwner") ?: return null
            val root = invoke(owner, "getUnmergedRootSemanticsNode") ?: return null
            val rootId = invoke(root, "getId") as? Int ?: return null
            val groups = HashSet<Int>()
            val sizes = HashMap<Int, Long>()
            collectUnmerged(root, groups, sizes, 0)
            SemanticsIndex(rootId, groups, sizes)
        } catch (t: Throwable) {
            Log.w(TAG, "semantics index failed", t)
            null
        }
    }

    private var indexFailureLogged = false

    private fun collectUnmerged(node: Any, groups: MutableSet<Int>, sizes: MutableMap<Int, Long>, depth: Int) {
        if (depth > MAX_DEPTH) return
        val id = invoke(node, "getId") as? Int
        if (id != null) {
            if (configFlag(node, "IsTraversalGroup")) groups.add(id)
            // SemanticsNode.layoutInfo is the node's LayoutNode (public LayoutInfo width/height).
            val info = invoke(node, "getLayoutInfo")
            val w = intOf(info, "getWidth")
            val h = intOf(info, "getHeight")
            if (w != null && h != null && w > 0 && h > 0) sizes[id] = packSize(w, h)
        }
        (invoke(node, "getChildren") as? List<*>)?.forEach { child ->
            // Per node: one bad subtree must not cost the index of the whole ComposeView.
            if (child != null) {
                try {
                    collectUnmerged(child, groups, sizes, depth + 1)
                } catch (t: Throwable) {
                    if (!indexFailureLogged) {
                        indexFailureLogged = true
                        Log.w(TAG, "semantics index: a node was skipped (${SafeString.errorName(t)}; logged once)")
                    }
                }
            }
        }
    }

    /** True when [node]'s SemanticsConfiguration maps the key named [keyName] to Boolean true. */
    private fun configFlag(node: Any, keyName: String): Boolean {
        val config = invoke(node, "getConfig") ?: return false
        val iter = invoke(config, "iterator") as? Iterator<*> ?: return false
        var guard = 0
        while (guard++ < MAX_CONFIG_ENTRIES && iter.hasNext()) {
            val entry = iter.next() as? Map.Entry<*, *> ?: continue
            val key = entry.key ?: continue
            // Compared as a String / Boolean: never through the app value's equals().
            if ((invoke(key, "getName") as? String) == keyName) return (entry.value as? Boolean) == true
        }
        return false
    }

    // ---------------------------------------------------------------- semantics (A)
    /**
     * The merged root SemanticsNode of [composeView] (merged root == what TalkBack sees == the
     * best human labels), or null plus the reason token: owner_unreachable (no getSemanticsOwner,
     * or it returned null) / root_unreachable. Throws when a getter itself throws.
     */
    private fun semanticsRoot(composeView: View): Pair<Any?, String> {
        val owner = invokeChecked(composeView, "getSemanticsOwner") ?: return null to "owner_unreachable"
        val root = invokeChecked(owner, "getRootSemanticsNode") ?: return null to "root_unreachable"
        return root to ""
    }

    /**
     * One SemanticsNode and its subtree. Each part (id, bounds, attributes, each child) is guarded
     * on its own: a failure costs that part and is counted in [ctx], never the node's siblings or
     * the ComposeView. Children deeper than [SEM_EMIT_MAX_DEPTH] are cut and counted.
     */
    private fun buildSemanticsNode(
        node: Any,
        strings: StringTable,
        depth: Int,
        off: IntArray,
        ctx: WalkCtx,
    ): ViewInspection.ComposeNode {
        val b = ViewInspection.ComposeNode.newBuilder()
        b.kind = ViewInspection.ComposeNode.Kind.SEMANTICS
        var failed = false
        try {
            (invoke(node, "getId") as? Int)?.let { b.id = it.toLong() }
        } catch (t: Throwable) {
            failed = true
            ctx.log("semantics id", t)
        }

        // bounds: getBoundsInWindow() -> Compose Rect (window px), shifted to screen px.
        try {
            semanticsBounds(node, off)?.let { b.bounds = it }
        } catch (t: Throwable) {
            failed = true
            ctx.log("semantics bounds", t)
        }

        // attrs: iterate the SemanticsConfiguration (guarded per entry inside)
        val attrs = try {
            readSemanticsConfig(node, ctx)
        } catch (t: Throwable) {
            failed = true
            ctx.log("semantics config", t)
            LinkedHashMap()
        }
        for ((k, v) in attrs) {
            b.addAttrs(
                ViewInspection.ComposeNode.Attr.newBuilder()
                    .setKey(strings.intern(k)).setValue(strings.intern(v)).build()
            )
        }
        b.name = strings.intern(bestLabel(attrs))

        // children
        val children = try {
            invoke(node, "getChildren") as? List<*>
        } catch (t: Throwable) {
            failed = true
            ctx.log("semantics children", t)
            null
        }
        if (children != null) {
            for (child in children) {
                if (child == null) continue
                if (depth + 1 >= SEM_EMIT_MAX_DEPTH) {
                    ctx.semTruncated++
                    continue
                }
                try {
                    b.addChildren(buildSemanticsNode(child, strings, depth + 1, off, ctx))
                } catch (t: Throwable) {
                    ctx.semNodeFailures++
                    ctx.log("semantics node", t)
                }
            }
        }
        if (failed) ctx.semNodeFailures++
        return b.build()
    }

    private fun semanticsBounds(node: Any, off: IntArray): ViewInspection.Bounds? {
        val rect = invoke(node, "getBoundsInWindow") ?: return null
        val l = (invoke(rect, "getLeft") as? Float) ?: return null
        val t = (invoke(rect, "getTop") as? Float) ?: return null
        val r = (invoke(rect, "getRight") as? Float) ?: return null
        val btm = (invoke(rect, "getBottom") as? Float) ?: return null
        // floor/ceil like Compose's own boundsInScreen for its AccessibilityNodeInfo
        // (AndroidComposeViewAccessibilityDelegateCompat), so the rects line up with a11y bounds.
        val x0 = kotlin.math.floor(l).toInt() + off[0]
        val y0 = kotlin.math.floor(t).toInt() + off[1]
        val x1 = kotlin.math.ceil(r).toInt() + off[0]
        val y1 = kotlin.math.ceil(btm).toInt() + off[1]
        val w = x1 - x0; val h = y1 - y0
        if (w <= 0 || h <= 0 || r - l <= 0f || btm - t <= 0f) return null
        return ViewInspection.Bounds.newBuilder()
            .setLayout(
                ViewInspection.Rect.newBuilder()
                    .setX(x0).setY(y0).setW(w).setH(h).build()
            ).build()
    }

    /**
     * The node's SemanticsConfiguration as name -> string. Each entry is guarded on its own: an
     * entry whose value cannot be stringified keeps its key (presence matters: OnClick, Heading,
     * Disabled) with the value "<error:Name>", counted as a value failure.
     */
    private fun readSemanticsConfig(node: Any, ctx: WalkCtx): LinkedHashMap<String, String> {
        val out = LinkedHashMap<String, String>()
        val config = invoke(node, "getConfig") ?: return out
        val iter = invoke(config, "iterator") as? Iterator<*> ?: return out
        var guard = 0
        while (guard++ < MAX_CONFIG_ENTRIES) {
            val raw: Any? = try {
                if (!iter.hasNext()) break
                iter.next()
            } catch (t: Throwable) {
                ctx.semValueFailures++
                ctx.log("semantics entry", t)
                break
            }
            val entry = raw as? Map.Entry<*, *> ?: continue
            val name = try {
                entry.key?.let { invoke(it, "getName") as? String }
            } catch (t: Throwable) {
                ctx.log("semantics key", t)
                null
            } ?: continue
            out[name] = try {
                entry.value?.let { SafeString.render(it) } ?: ""
            } catch (t: Throwable) {
                ctx.semValueFailures++
                ctx.log("semantics value", t)
                SafeString.errorToken(t)
            }
        }
        return out
    }

    private fun bestLabel(attrs: Map<String, String>): String {
        for (k in listOf("Text", "ContentDescription", "EditableText", "InputText", "Role", "TestTag")) {
            val v = attrs[k]
            if (!v.isNullOrBlank()) return v
        }
        return "Node"
    }

    // ---------------------------------------------------------------- slot table (B)
    @Suppress("UNCHECKED_CAST")
    private fun slotTableGroups(composeView: View, ctx: WalkCtx): List<Any> {
        val cl = composeView.javaClass.classLoader ?: return emptyList()
        val tagId = slotTableTagId(composeView, cl)
        if (tagId == 0) return emptyList()
        val set = composeView.getTag(tagId) as? Set<Any> ?: return emptyList()
        if (set.isEmpty()) return emptyList()
        val slotTreeKt = try { Class.forName("androidx.compose.ui.tooling.data.SlotTreeKt", false, cl) } catch (t: Throwable) { return emptyList() }
        val compositionDataCls = try { Class.forName("androidx.compose.runtime.tooling.CompositionData", false, cl) } catch (t: Throwable) { return emptyList() }
        val asTree: Method = try { slotTreeKt.getMethod("asTree", compositionDataCls) } catch (t: Throwable) { return emptyList() }
        val groups = ArrayList<Any>()
        for (cd in set) {
            try {
                val g = asTree.invoke(null, cd)
                if (g != null) groups.add(g)
            } catch (t: Throwable) {
                ctx.slotGroupFailures++
                ctx.log("slot-table asTree", t)
            }
        }
        return groups
    }

    private fun slotTableTagId(view: View, cl: ClassLoader): Int {
        // Primary: the merged app resource id, by name.
        try {
            val id = view.resources.getIdentifier(
                "inspection_slot_table_set", "id", view.context.packageName
            )
            if (id != 0) return id
        } catch (_: Throwable) {}
        // Fallback: androidx.compose.ui.R$id.inspection_slot_table_set
        return try {
            val rid = Class.forName("androidx.compose.ui.R\$id", false, cl)
            rid.getField("inspection_slot_table_set").getInt(null)
        } catch (_: Throwable) { 0 }
    }

    // Infrastructural composable names that carry no UI meaning: collapse them like unnamed groups
    // so their modifiers/children attach to the nearest MEANINGFUL composable (Tape, Polaroid, Text…).
    private val STRUCTURAL = setOf(
        "Layout", "ReusableComposeNode", "ReusableContent", "ReusableContentHost",
        "SubcomposeLayout", "CompositionLocalProvider",
    )

    private fun isStructuralName(name: String): Boolean =
        name in STRUCTURAL || name.startsWith("<") || name.startsWith("remember")

    private fun meaningfulName(group: Any): String? =
        (invoke(group, "getName") as? String)?.takeIf { it.isNotBlank() && !isStructuralName(it) }

    /**
     * Ids for the named slot-table nodes of one Window: negative (<= -2), unique within the
     * Window, and stable across dumps while the group lives (see the header, IDS). The base is the
     * hash of Group.identity: the group's slot-table anchor, which the slot table keeps for the
     * group's lifetime (identity hash), or for a group inside inline source information a
     * data-class path from such an anchor (same hash on every read; ui-tooling-data sets identity
     * on named groups with a non-empty box only). Without one, a hash of the parent id, the name,
     * an Int group key and the sibling index. A collision takes the next free id.
     */
    private class SlotIds {
        private val used = HashSet<Long>()

        fun mint(group: Any, name: String, parentId: Long, siblingIndex: Int): Long {
            // identity is a Compose runtime object (GapAnchor / LinkAnchor / path), never app code.
            val h = invoke(group, "getIdentity")?.let {
                try { it.hashCode() } catch (_: Throwable) { null }
            } ?: run {
                // Only an Int key is hashed: any other key may be an app object (key(x) { }).
                val key = invoke(group, "getKey") as? Int ?: 0
                ((parentId.hashCode() * 31 + name.hashCode()) * 31 + key) * 31 + siblingIndex
            }
            var id = -2L - (h.toLong() and 0x7FFFFFFFL)
            while (!used.add(id)) id = if (id <= MIN_ID) -2L else id - 1
            return id
        }

        companion object {
            private const val MIN_ID = -2L - 0x7FFFFFFFL
        }
    }

    /**
     * The NAMED-composable nodes contributed by [group] and its descendants. Anonymous/structural
     * groups (no name) are collapsed: their named children are hoisted to the caller, which both
     * flattens the very deep slot tree and yields a readable composable hierarchy. Each named node
     * carries an id ([SlotIds]), bounds, file:line, and its call parameters. [namedDepth] counts
     * the named ancestors (the nesting that goes on the wire, capped at [SLOT_MAX_DEPTH]);
     * [rawDepth] the raw groups (capped at [SLOT_RAW_MAX_DEPTH] as a recursion guard). A cut
     * subtree is counted in [ctx] (slot_truncated); a child group that throws is counted
     * (slot_partial) and its siblings still come through.
     */
    private fun buildSlotChildren(
        group: Any,
        strings: StringTable,
        rawDepth: Int,
        namedDepth: Int,
        off: IntArray,
        parentId: Long,
        siblingIndex: Int,
        ctx: WalkCtx,
    ): List<ViewInspection.ComposeNode> {
        if (rawDepth > SLOT_RAW_MAX_DEPTH) {
            ctx.slotTruncated++
            return emptyList()
        }
        val name = meaningfulName(group)
        if (name != null && namedDepth >= SLOT_MAX_DEPTH) {
            ctx.slotTruncated++
            return emptyList()
        }
        val id = if (name != null) ctx.slotIds.mint(group, name, parentId, siblingIndex) else parentId
        val childNamedDepth = if (name != null) namedDepth + 1 else namedDepth
        val childGroups = (invoke(group, "getChildren") as? Collection<*>) ?: emptyList<Any?>()
        val childNodes = ArrayList<ViewInspection.ComposeNode>()
        var index = 0
        for (c in childGroups) {
            if (c == null) continue
            try {
                childNodes.addAll(
                    buildSlotChildren(c, strings, rawDepth + 1, childNamedDepth, off, id, index++, ctx),
                )
            } catch (t: Throwable) {
                ctx.slotGroupFailures++
                ctx.log("slot-table group", t)
            }
        }

        if (name == null) return childNodes // structural group: hoist children up

        val b = ViewInspection.ComposeNode.newBuilder()
        b.kind = ViewInspection.ComposeNode.Kind.COMPOSABLE
        b.id = id
        b.name = strings.intern(name)
        slotBox(group, off)?.let { b.bounds = it }
        slotLocation(group)?.let { b.source = strings.intern(it) }
        // Modifiers live on the LayoutNode (NodeGroup), which is an UNNAMED descendant of this
        // named composable. Gather modifiers from this group's owned unnamed-descendant chain,
        // stopping at the next named composable (whose modifiers belong to it).
        val mods = LinkedHashSet<String>()
        try {
            collectOwnedModifiers(group, mods, 0)
        } catch (t: Throwable) {
            ctx.log("slot-table modifiers", t)
        }
        if (mods.isNotEmpty()) {
            b.addAttrs(
                ViewInspection.ComposeNode.Attr.newBuilder()
                    .setKey(strings.intern("modifiers"))
                    .setValue(strings.intern(mods.joinToString(" → "))).build()
            )
        }
        // Render-node (graphicsLayer) id, so the host can cut a per-component SKP image.
        val rnid = try { collectRenderNodeId(group, 0) } catch (t: Throwable) { 0L }
        if (rnid != 0L) b.renderNodeId = rnid
        val params = try {
            slotParameters(group)
        } catch (t: Throwable) {
            ctx.log("slot-table parameters", t)
            emptyList()
        }
        for (p in params) {
            b.addAttrs(
                ViewInspection.ComposeNode.Attr.newBuilder()
                    .setKey(strings.intern(p.first)).setValue(strings.intern(p.second)).build()
            )
        }
        childNodes.forEach { b.addChildren(it) }
        return listOf(b.build())
    }

    /**
     * Read a Group's realized MODIFIER chain via getModifierInfo() -> List<ModifierInfo>, decoding
     * each Modifier.Element through the InspectableValue SPI (nameFallback + inspectableElements ->
     * name + ValueElement args), e.g. "padding(all=16.dp) → background(color=…) → clickable".
     * Requires isDebugInspectorInfoEnabled (we set it in enableInspection). Returns null if none.
     */
    /** Decoded modifier strings from a single group's getModifierInfo() (empty unless a NodeGroup). */
    private fun slotModifiersOf(group: Any): List<String> {
        val infos = invoke(group, "getModifierInfo") as? List<*> ?: return emptyList()
        if (infos.isEmpty()) return emptyList()
        val parts = ArrayList<String>()
        for (mi in infos) {
            if (mi == null) continue
            val mod = invoke(mi, "getModifier") ?: continue
            describeModifierElement(mod)?.let { parts.add(it) }
        }
        return parts
    }

    /** The graphicsLayer RenderNode/layer id on [group]'s LayoutNode, if any (via GraphicLayerInfo). */
    private fun renderNodeIdOf(group: Any): Long {
        val infos = invoke(group, "getModifierInfo") as? List<*> ?: return 0L
        for (mi in infos) {
            if (mi == null) continue
            // ModifierInfo.extra is a androidx.compose.ui.layout.GraphicLayerInfo (an INTERFACE) for
            // the graphicsLayer element; GraphicLayerInfo.layerId is the SKP draw-layer id.
            val extra = invoke(mi, "getExtra") ?: continue
            val cl = extra.javaClass.classLoader ?: continue
            val gli = try {
                Class.forName("androidx.compose.ui.layout.GraphicLayerInfo", false, cl)
            } catch (t: Throwable) { continue }
            if (gli.isInstance(extra)) {
                val id = invoke(extra, "getLayerId") as? Long
                if (id != null && id != 0L) return id
            }
        }
        return 0L
    }

    /** First graphicsLayer id from [group] and its UNNAMED descendant chain (stopping at named ones). */
    private fun collectRenderNodeId(group: Any, depth: Int): Long {
        if (depth > 20) return 0L
        val own = renderNodeIdOf(group)
        if (own != 0L) return own
        val children = invoke(group, "getChildren") as? Collection<*> ?: return 0L
        for (c in children) {
            if (c == null || meaningfulName(c) != null) continue
            val id = collectRenderNodeId(c, depth + 1)
            if (id != 0L) return id
        }
        return 0L
    }

    /** Collect modifiers from [group] and its UNNAMED descendant chain, stopping at named composables. */
    private fun collectOwnedModifiers(group: Any, out: MutableSet<String>, depth: Int) {
        if (depth > 20) return
        out.addAll(slotModifiersOf(group))
        val children = invoke(group, "getChildren") as? Collection<*> ?: return
        for (c in children) {
            if (c == null) continue
            if (meaningfulName(c) == null) collectOwnedModifiers(c, out, depth + 1)
        }
    }

    private fun describeModifierElement(el: Any): String? {
        // InspectableValue: nameFallback + inspectableElements (Sequence<ValueElement>).
        val name = invoke(el, "getNameFallback") as? String
        val args = ArrayList<String>()
        // The InspectableValue SPI evaluates the modifier's inspectorInfo block (library code, or
        // the app's own for a custom modifier); a failure ends the argument list, not the node.
        try {
            val seq = invoke(el, "getInspectableElements")
            val iter = invoke(seq, "iterator") as? Iterator<*>
            var guard = 0
            while (iter != null && iter.hasNext() && guard < 24) {
                guard++
                val ve = iter.next() ?: continue
                val vn = invoke(ve, "getName") as? String ?: continue
                val vv = invoke(ve, "getValue")
                args.add("$vn=${SafeString.of(vv)}")
            }
        } catch (_: Throwable) {
            args.add("…")
        }
        val base = name ?: SafeString.simpleNameOf(el.javaClass)
            .removeSuffix("Element").removeSuffix("Modifier").ifBlank { return null }
        return if (args.isEmpty()) base else "$base(${args.joinToString(", ")})"
    }

    /** Read a Group's call parameters (ParameterInformation: name, value) as key/value strings. */
    private fun slotParameters(group: Any): List<Pair<String, String>> {
        val params = invoke(group, "getParameters") as? List<*> ?: return emptyList()
        val out = ArrayList<Pair<String, String>>()
        for (p in params) {
            if (p == null) continue
            val pn = invoke(p, "getName") as? String ?: continue
            val pv = invoke(p, "getValue")
            out.add(pn to SafeString.of(pv))
        }
        return out
    }

    /** Group.box is window px (ui-tooling-data boundsOfLayoutNode uses positionInWindow); shift to screen. */
    private fun slotBox(group: Any, off: IntArray): ViewInspection.Bounds? {
        val box = invoke(group, "getBox") ?: return null
        val l = intOf(box, "getLeft") ?: return null
        val t = intOf(box, "getTop") ?: return null
        val r = intOf(box, "getRight") ?: return null
        val btm = intOf(box, "getBottom") ?: return null
        val w = r - l; val h = btm - t
        if (w <= 0 || h <= 0) return null
        return ViewInspection.Bounds.newBuilder()
            .setLayout(
                ViewInspection.Rect.newBuilder()
                    .setX(l + off[0]).setY(t + off[1]).setW(w).setH(h).build()
            )
            .build()
    }

    private fun slotLocation(group: Any): String? {
        val loc = invoke(group, "getLocation") ?: return null
        val file = invoke(loc, "getSourceFile") as? String
        val line = invoke(loc, "getLineNumber") as? Int
        return when {
            file != null && line != null && line >= 0 -> "$file:$line"
            file != null -> file
            else -> null
        }
    }

    // ---------------------------------------------------------------- bounds of a View
    private fun boundsOf(view: View): ViewInspection.Bounds? {
        return try {
            val loc = IntArray(2); view.getLocationOnScreen(loc)
            val w = view.width; val h = view.height
            if (w <= 0 || h <= 0) return null
            ViewInspection.Bounds.newBuilder()
                .setLayout(ViewInspection.Rect.newBuilder().setX(loc[0]).setY(loc[1]).setW(w).setH(h).build())
                .build()
        } catch (t: Throwable) { null }
    }

    // ---------------------------------------------------------------- reflection helpers
    private fun invoke(obj: Any?, method: String): Any? {
        if (obj == null) return null
        val m = findMethod(obj.javaClass, method) ?: return null
        return try { m.isAccessible = true; m.invoke(obj) } catch (t: Throwable) { null }
    }

    /**
     * Like [invoke], but a getter that throws is not mistaken for a missing one: returns null only
     * when the method is absent (or returns null) and rethrows the getter's own exception.
     */
    private fun invokeChecked(obj: Any, method: String): Any? {
        val m = findMethod(obj.javaClass, method) ?: return null
        m.isAccessible = true
        return try {
            m.invoke(obj)
        } catch (e: InvocationTargetException) {
            throw e.targetException ?: e
        }
    }

    private fun intOf(obj: Any?, method: String): Int? = (invoke(obj, method) as? Number)?.toInt()

    private val methodCache = HashMap<String, Method?>()

    private fun findMethod(cls: Class<*>, name: String): Method? {
        val key = cls.name + "#" + name
        methodCache[key]?.let { return it }
        if (methodCache.containsKey(key)) return null
        var found: Method? = null
        // public (incl. inherited) first
        try { found = cls.getMethod(name) } catch (_: Throwable) {}
        if (found == null) {
            var k: Class<*>? = cls
            loop@ while (k != null) {
                try { found = k.getDeclaredMethod(name); break@loop } catch (_: Throwable) {}
                for (i in k.interfaces) {
                    try { found = i.getDeclaredMethod(name); break@loop } catch (_: Throwable) {}
                }
                k = k.superclass
            }
        }
        methodCache[key] = found
        return found
    }

    private fun isAssignableToName(cls: Class<*>, fqName: String): Boolean {
        var c: Class<*>? = cls
        while (c != null) {
            if (c.canonicalName == fqName || c.name == fqName) return true
            c = c.superclass
        }
        return false
    }
}
