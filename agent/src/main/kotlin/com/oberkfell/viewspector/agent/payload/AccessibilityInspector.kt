/*
 * ViewSpector — payload :: accessibility (AccessibilityNodeInfo) tree extraction.
 *
 * In-process extraction of the unified accessibility tree — classic Views AND
 * virtual nodes served by an AccessibilityNodeProvider (Compose semantics, WebView,
 * ExploreByTouchHelper) — as Assistive Technology (TalkBack / UiAutomator) observes
 * it. The whole dump runs on the MAIN THREAD (the caller hops via MainThread.run),
 * since every getter touches live View / ANI state.
 *
 * WALK
 *   Query mode (API 34+, preferred):
 *     1. node = root.createAccessibilityNodeInfo() — the node AS COMPOSED by the
 *        View's AccessibilityDelegate / provider, i.e. what AT sees.
 *     2. node.setQueryFromAppProcessEnabled(root, true) — the public API-34 switch
 *        that gives the node a direct in-process connection, so getChild() resolves
 *        through the window's AccessibilityInteractionController exactly as for AT.
 *        Reset to false in a finally.
 *     3. Recurse via getChildCount() / getChild(i); getChild dispatches through the
 *        provider for virtual children, so one recursion covers Views and Compose.
 *   Local mode (API < 34, or when query mode is unavailable / throws):
 *     The same recursion, but each packed child id (hidden getChildId(i)) is resolved
 *     locally the way AccessibilityInteractionController does it: View id -> View,
 *     then View.createAccessibilityNodeInfo() for a real View or
 *     provider.createAccessibilityNodeInfo(virtualId) for a virtual one. Compose and
 *     other provider content therefore survive the fallback. If getChildId is not
 *     reachable, real ViewGroups fall back to the public addChildrenForAccessibility.
 *     The diagnostics string names the mode used for every root.
 *
 * IDENTITY (the host's ID contract; see A11yIds for the bit layout)
 *   host_view_id = uniqueDrawingId of the node's OWN backing View: the real View for
 *                  a View node, the provider host (e.g. that AndroidComposeView) for a
 *                  virtual node. 0 = could not be resolved (counted in diagnostics).
 *   virtual_id   = -1 for a real View, else the virtual descendant id (HIGH 32 bits of
 *                  the packed node id; for Compose it is the SemanticsNode id).
 *   is_virtual   = virtual_id != -1.
 *   provider_class = on a provider host only: the host View's class name
 *                  (e.g. androidx.compose.ui.platform.AndroidComposeView, android.webkit.WebView).
 *   Each node's identity is decoded from its own packed source id (getSourceNodeId):
 *   LOW 32 bits = View.getAccessibilityViewId(), mapped to the View through an index
 *   of every View under the roots built before the walk; HIGH 32 bits = virtual id.
 *   traversal_before / traversal_after / label_for / labeled_by / labeled_by_list are
 *   decoded the same way and emitted as HOST NODE KEYS,
 *   (host_view_id << 32) ^ (virtual_id & 0xFFFFFFFF), 0 = none.
 *
 * Recycling: AccessibilityNodeInfo.recycle() is a deprecated no-op since API 33 and a
 * double recycle is a latent crash, so nothing here recycles; references are dropped
 * at the end of the MainThread.run block.
 *
 * Bounds: getBoundsInScreen() — absolute screen px, the same space as ViewNode.bounds
 * and ComposeNode.bounds. In query mode the connection returns them relative to the window,
 * so a window away from the screen origin (dialog, popup) is shifted back by the offset
 * connectionOffset measures (diagnostics: window-offset=dx,dy).
 *
 * Passwords (Redaction.kt): a password field's text is masked, and so is the text of an
 * EDITABLE node whose password status cannot be determined (fail closed: its source is
 * unresolved, or it is a Compose node whose semantics are out of reach or unreadable). The
 * diagnostics list those (redaction_masked) and the AndroidComposeViews whose password fields
 * cannot be identified at all (redaction_unverified).
 */
package com.oberkfell.viewspector.agent.payload

import android.accessibilityservice.AccessibilityServiceInfo
import android.graphics.Rect
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.view.accessibility.AccessibilityManager
import android.view.accessibility.AccessibilityNodeInfo
import com.oberkfell.viewspector.proto.ViewInspection
import java.lang.reflect.Field
import java.lang.reflect.Method

object AccessibilityInspector {

    private const val TAG = "ViewSpector"

    // Loop / fan-out guards. walk() depth is 0-based, so MAX_DEPTH = cap - 1 sends at most
    // WireLimits.MAX_TREE_DEPTH levels (deeper, the host can't parse the response at all);
    // a node at the cap with children is flagged children_truncated.
    private const val MAX_DEPTH = WireLimits.MAX_TREE_DEPTH - 1
    private const val MAX_NODES = 5000
    private const val VIEW_MAX_DEPTH = 400

    // How many times one dump may rebuild the accessibility-id -> View index after a miss
    // (Views attached mid-walk, e.g. a RecyclerView laying out a new cell).
    private const val MAX_INDEX_BUILDS = 3

    private const val HOST_VIEW_ID = A11yIds.HOST_VIEW_ID

    // Single-node Compose password lookups a lite snapshot makes before it indexes the tree.
    private const val LITE_COMPOSE_LOOKUPS = 8

    // Cap on emitted extras per node, and skip very large stringified values (Parcelables).
    private const val MAX_EXTRAS = 64
    private const val MAX_EXTRA_VALUE_LEN = 512

    // Well-known extras key TalkBack reads for the spoken role.
    private const val ROLE_DESC_KEY = "AccessibilityNodeInfo.roleDescription"

    // ------------------------------------------------------------ reflective handles
    // Resolved once (the object initializes on first use, on the main thread). Every
    // failure is logged once here and degrades the dependent feature, never the dump.

    /** @hide View.getAccessibilityViewId(): the LOW half of a packed node id (@UnsupportedAppUsage). */
    private val getAccessibilityViewIdM: Method? =
        method(View::class.java, "getAccessibilityViewId")

    /** @hide AccessibilityNodeInfo.getSourceNodeId(): the node's own packed id (@UnsupportedAppUsage @TestApi). */
    private val getSourceNodeIdM: Method? =
        method(AccessibilityNodeInfo::class.java, "getSourceNodeId")

    /** @hide AccessibilityNodeInfo.getChildId(int): the packed id of child i. */
    private val getChildIdM: Method? =
        method(AccessibilityNodeInfo::class.java, "getChildId", Int::class.javaPrimitiveType!!)

    // The packed linkage ids, read straight from the node. The public getters
    // (getTraversalBefore() ...) resolve the target through the connection, which
    // costs a query and fails outright on an unsealed local-mode node; they are only
    // used when a field is unreachable.
    private val traversalBeforeF: Field? = field(AccessibilityNodeInfo::class.java, "mTraversalBefore")
    private val traversalAfterF: Field? = field(AccessibilityNodeInfo::class.java, "mTraversalAfter")
    private val labelForF: Field? = field(AccessibilityNodeInfo::class.java, "mLabelForId")
    private val labeledByF: Field? = field(AccessibilityNodeInfo::class.java, "mLabeledById")

    /** android.util.LongArray mLabeledByIds (API 35+, the multiple-labeledBy list); absent before. */
    private val labeledByIdsF: Field? =
        field(AccessibilityNodeInfo::class.java, "mLabeledByIds", logMissing = Build.VERSION.SDK_INT >= 35)

    /**
     * AccessibilityNodeInfo has no traversal-group accessor through API 37 (checked against the
     * android-36.1 and android-37.0 platform jars). Probe for one so a future platform is picked
     * up; until then is_traversal_group comes from the Compose semantics (IsTraversalGroup).
     */
    private val isTraversalGroupM: Method? = try {
        AccessibilityNodeInfo::class.java.getMethod("isTraversalGroup")
    } catch (t: Throwable) {
        Log.i(TAG, "AccessibilityNodeInfo.isTraversalGroup() absent; using Compose IsTraversalGroup")
        null
    }

    /** @hide android.view.accessibility.AccessibilityNodeIdManager, the framework's own id -> View map. */
    private val idManager: Pair<Any, Method>? = try {
        val cls = Class.forName("android.view.accessibility.AccessibilityNodeIdManager")
        val inst = cls.getMethod("getInstance").invoke(null)
        val find = cls.getMethod("findView", Int::class.javaPrimitiveType)
        if (inst != null) inst to find else null
    } catch (t: Throwable) {
        Log.i(TAG, "AccessibilityNodeIdManager not reachable; relying on the local View index", t)
        null
    }

    // ------------------------------------------------------------ per-dump state

    /** The resolved identity of one node: its backing View (null = unresolved) and virtual id. */
    private class Ident(val view: View?, val virtualId: Int)

    private class Ctx(
        val roots: List<View>,
        val strings: StringTable,
        val includeExtras: Boolean,
        val includeRenderingInfo: Boolean,
        val maxDepth: Int = MAX_DEPTH,
        val maxNodes: Int = MAX_NODES,
        /**
         * Skip the fields that need a walk of a whole semantics tree (is_traversal_group and
         * layout_size of Compose nodes): the focus reader maps a node or two and must stay fast.
         */
        val lite: Boolean = false,
        /** What was masked for want of a password status, for the diagnostics. */
        val redaction: Redaction.Unverified = Redaction.Unverified(),
    ) {
        var count = 0
        val byA11yId = HashMap<Int, View>()

        /**
         * The window root being walked. On API 37 (observed live) a window's root View reports
         * the accessibility view id ROOT_ITEM_ID, shared by every window root, so that id
         * resolves to the current root rather than through the index. On API <= 36 a root has
         * an ordinary counter id (View.getAccessibilityViewId) and resolves through the index;
         * both cases are handled.
         */
        var currentRoot: View? = null

        /**
         * Added to the bounds of every node the in-process connection returns for the current
         * window (see [connectionOffset]); 0,0 for a window at the screen origin.
         */
        var boundsDx = 0
        var boundsDy = 0
        var indexBuilds = 0
        var unresolvedNodes = 0
        var unresolvedLinks = 0
        var nullChildren = 0
        var unenumerable = 0
        var reflectFailures = 0
        var depthTruncated = 0
        val loggedFailures = HashSet<String>()
        val composeIndex = HashMap<View, ComposeInspector.SemanticsIndex?>()

        /** Single-node Compose password lookups made in lite mode (see [composePassword]). */
        var composeLookups = 0

        /** Count a failed reflective call; log the first failure of each [kind] per dump. */
        fun fail(kind: String, t: Throwable) {
            reflectFailures++
            if (loggedFailures.add(kind)) Log.w(TAG, "$kind failed (further failures counted only)", t)
        }
    }

    /**
     * Build one [ViewInspection.DumpA11yResponse.Window] per root View, plus a
     * human-readable diagnostics string. MUST be called on the main thread.
     *
     * @param rootViews z-sorted window roots (RootsDetector.rootViews()).
     * @param strings shared interner; every CharSequence is .toString()-ed and
     *   interned (id 0 = absent).
     * @param includeExtras iterate getExtras() per node.
     * @param includeRenderingInfo refreshWithExtraData + getExtraRenderingInfo per
     *   node (query mode only; costly extra round-trip).
     * @param allRoots every current root (RootsDetector.rootViews()), for each window's z.
     */
    fun dump(
        rootViews: List<View>,
        strings: StringTable,
        includeExtras: Boolean,
        includeRenderingInfo: Boolean,
        allRoots: List<View> = rootViews,
    ): Pair<List<ViewInspection.DumpA11yResponse.Window>, String> {
        val ctx = Ctx(rootViews, strings, includeExtras, includeRenderingInfo)
        val diag = StringBuilder("roots=${rootViews.size}; api=${Build.VERSION.SDK_INT}; ids=host-key")
        val windows = ArrayList<ViewInspection.DumpA11yResponse.Window>()

        // Compose computes traversal_before/after (setTraversalValues) only while an accessibility
        // service is on (AndroidComposeViewAccessibilityDelegateCompat.isEnabled, ui 1.7 to 1.12:
        // AccessibilityManager.isEnabled AND a non-empty enabled-service list). isEnabled alone is
        // not enough: it turns true in-process once query-from-app-process opens a direct
        // connection, with no service running. Without a service ComposeTraversal.prime computes
        // the order per window below; the token says which case this is.
        val a11yOn = try {
            rootViews.firstOrNull()?.context
                ?.getSystemService(AccessibilityManager::class.java)
                ?.let { am ->
                    am.isEnabled &&
                        am.getEnabledAccessibilityServiceList(AccessibilityServiceInfo.FEEDBACK_ALL_MASK)
                            .isNotEmpty()
                }
        } catch (t: Throwable) {
            Log.w(TAG, "AccessibilityManager state unavailable", t)
            null
        }
        when (a11yOn) {
            true -> diag.append("; a11y-services=on")
            false -> diag.append("; a11y-services=off")
            null -> {}
        }

        buildIndex(ctx)
        if (getAccessibilityViewIdM == null) {
            diag.append("; WARN View.getAccessibilityViewId unreachable: host_view_id cannot be resolved")
        }
        if (getSourceNodeIdM == null) {
            diag.append("; WARN AccessibilityNodeInfo.getSourceNodeId unreachable: identity from child ids only")
        }

        for (root in rootViews) {
            ctx.currentRoot = root
            // The host node on which app-process query mode was enabled; reset on it in finally.
            var enabledNode: AccessibilityNodeInfo? = null
            val countBefore = ctx.count
            // Compose's reading order (traversal_before/after) for this window, service or not.
            windowToken(root)?.let { diag.append("; root#${idOf(root)} $it") }
            val traversal = ComposeTraversal.prime(root)
            traversal.token()?.let { diag.append("; root#${idOf(root)} $it") }
            try {
                var node: ViewInspection.A11yNode? = null
                if (Build.VERSION.SDK_INT >= 34) {
                    try {
                        val hostNode = root.createAccessibilityNodeInfo()
                        if (hostNode != null) {
                            hostNode.setQueryFromAppProcessEnabled(root, true)
                            enabledNode = hostNode
                            val (dx, dy) = connectionOffset(hostNode, ctx)
                            ctx.boundsDx = dx
                            ctx.boundsDy = dy
                            node = walk(hostNode, identify(hostNode, null, ctx), ctx, 0, local = false)
                            diag.append("; root#${idOf(root)} query-from-app-process")
                            if (dx != 0 || dy != 0) diag.append("; root#${idOf(root)} window-offset=$dx,$dy")
                        } else {
                            diag.append("; root#${idOf(root)} null host node")
                        }
                    } catch (t: Throwable) {
                        Log.w(TAG, "query-from-app-process failed; falling back to the local walk", t)
                        diag.append(
                            "; root#${idOf(root)} query-from-app-process failed (${t.javaClass.simpleName})",
                        )
                        resetQueryMode(enabledNode, root)
                        enabledNode = null
                        ctx.count = countBefore
                        ctx.boundsDx = 0
                        ctx.boundsDy = 0
                        node = null
                    }
                }
                if (node == null) {
                    node = walkLocalRoot(root, ctx)
                    diag.append(
                        "; root#${idOf(root)} local-fallback" +
                            if (Build.VERSION.SDK_INT < 34) " (api<34)" else "",
                    )
                }
                if (node != null) windows.add(window(root, node, WindowInfos.zOf(root, allRoots), ctx))
            } catch (t: Throwable) {
                Log.w(TAG, "a11y dump failed for root", t)
                diag.append("; root#${idOf(root)} error ${t.javaClass.simpleName}")
            } finally {
                resetQueryMode(enabledNode, root)
                ComposeTraversal.restore(traversal)
            }
        }

        diag.append("; nodes=${ctx.count}; views-indexed=${ctx.byA11yId.size}")
        if (ctx.unresolvedNodes > 0) diag.append("; unresolved-nodes=${ctx.unresolvedNodes}")
        if (ctx.unresolvedLinks > 0) diag.append("; unresolved-links=${ctx.unresolvedLinks}")
        if (ctx.nullChildren > 0) diag.append("; null-children=${ctx.nullChildren}")
        if (ctx.unenumerable > 0) diag.append("; provider-children-unreachable=${ctx.unenumerable}")
        if (ctx.reflectFailures > 0) diag.append("; reflect-failures=${ctx.reflectFailures}")
        if (ctx.depthTruncated > 0) {
            diag.append(
                "; depth-truncated=${ctx.depthTruncated} (children below " +
                    "${WireLimits.MAX_TREE_DEPTH} levels not sent)",
            )
        }
        if (ctx.count >= ctx.maxNodes) diag.append("; node-cap=${ctx.maxNodes} reached (later nodes not sent)")
        for ((view, index) in ctx.composeIndex) {
            if (index == null || ComposeInspector.isRenamed(view)) ctx.redaction.composeView(idOf(view))
        }
        ctx.redaction.appendTo(diag)
        return windows to diag.toString()
    }

    /**
     * The shift that puts connection-served bounds back in screen px. For each node it serves,
     * AccessibilityInteractionController translates boundsInScreen by -mWindowLeft/-mWindowTop
     * (into window coordinates) and relies on the caller's window-to-screen matrix to map them
     * back; system_server supplies that matrix for a real service, the in-process
     * query-from-app-process connection supplies none. So in a window that does not start at the
     * screen origin (a dialog, a popup) every node but the locally built root came back
     * window-relative. Measure the shift on the first child of the root that is a real View: its
     * node as served by the connection against the same View's node built locally
     * (createAccessibilityNodeInfo, screen px). 0,0 when they agree or the probe fails.
     */
    private fun connectionOffset(hostNode: AccessibilityNodeInfo, ctx: Ctx): Pair<Int, Int> {
        return try {
            for (i in 0 until hostNode.childCount) {
                val child = childOf(hostNode, i) ?: continue
                val packed = sourceNodeId(child, ctx) ?: continue
                if (A11yIds.isUndefined(packed) || A11yIds.virtualIdOf(packed) != HOST_VIEW_ID) continue
                val view = viewFor(A11yIds.accessibilityViewIdOf(packed), ctx) ?: continue
                val local = view.createAccessibilityNodeInfo() ?: continue
                val viaConnection = Rect()
                val direct = Rect()
                child.getBoundsInScreen(viaConnection)
                local.getBoundsInScreen(direct)
                if (direct.isEmpty || viaConnection.isEmpty) continue
                return (direct.left - viaConnection.left) to (direct.top - viaConnection.top)
            }
            0 to 0
        } catch (t: Throwable) {
            ctx.fail("a11y window-offset probe", t)
            0 to 0
        }
    }

    /**
     * "window type=T flags=0xF" for a window root: its WindowManager.LayoutParams type and flags.
     * The host decides from them which window is modal (neither FLAG_NOT_TOUCH_MODAL nor
     * FLAG_NOT_FOCUSABLE): the system reports no window below a modal one to accessibility
     * services, so TalkBack cannot reach the activity under an open dialog.
     */
    private fun windowToken(root: View): String? {
        val lp = try {
            root.layoutParams as? WindowManager.LayoutParams
        } catch (_: Throwable) {
            null
        } ?: return null
        return "window type=${lp.type} flags=0x${Integer.toHexString(lp.flags)}"
    }

    @Volatile private var queryResetFailureLogged = false

    private fun resetQueryMode(node: AccessibilityNodeInfo?, root: View) {
        if (node == null) return
        try {
            node.setQueryFromAppProcessEnabled(root, false)
        } catch (t: Throwable) {
            // The node is sealed by the time the walk ends, so this throws "sealed instance" on
            // every dump (seen on API 37). Nothing is left behind: the connection id lives on this
            // discarded node, and the direct connection belongs to the ViewRootImpl
            // (ensureDirectConnection). Say so once per process, quietly.
            if (!queryResetFailureLogged) {
                queryResetFailureLogged = true
                Log.d(TAG, "setQueryFromAppProcessEnabled(false) skipped: ${t.javaClass.simpleName} (logged once)")
            }
        }
    }

    private fun window(
        root: View,
        node: ViewInspection.A11yNode,
        z: Int,
        ctx: Ctx,
    ): ViewInspection.DumpA11yResponse.Window =
        ViewInspection.DumpA11yResponse.Window.newBuilder()
            .setRootViewId(idOf(root))
            .setRoot(node)
            .setInfo(WindowInfos.of(root, z, ctx.strings))
            .build()

    /**
     * [node] (and its children to [maxDepth] levels) mapped exactly like a dump node, for the
     * focus reader (A11yFocus.kt). [node] was built locally (View.createAccessibilityNodeInfo /
     * provider.createAccessibilityNodeInfo), so its bounds are already screen px and children
     * resolve without a connection. [view] / [virtualId] are its identity (host-key contract).
     * Lite: no Compose traversal priming, no is_traversal_group / layout_size. Main thread.
     */
    fun snapshot(
        roots: List<View>,
        root: View,
        view: View?,
        virtualId: Int,
        node: AccessibilityNodeInfo,
        strings: StringTable,
        maxDepth: Int,
        maxNodes: Int,
        redaction: Redaction.Unverified,
    ): ViewInspection.A11yNode {
        val ctx = Ctx(roots, strings, includeExtras = true, includeRenderingInfo = false,
            maxDepth = maxDepth, maxNodes = maxNodes, lite = true, redaction = redaction)
        ctx.currentRoot = root
        return walk(node, Ident(view, virtualId), ctx, 0, local = true)
    }

    // ------------------------------------------------------------ identity

    /** Index every View under the roots by its accessibility view id (process-unique). */
    private fun buildIndex(ctx: Ctx) {
        ctx.indexBuilds++
        val m = getAccessibilityViewIdM ?: return
        ctx.byA11yId.clear()
        for (root in ctx.roots) indexViews(root, m, ctx, 0)
    }

    private fun indexViews(view: View, m: Method, ctx: Ctx, depth: Int) {
        if (depth > VIEW_MAX_DEPTH) return
        try {
            // getAccessibilityViewId() assigns an id on first use; attached Views already have
            // one (View.onAttachedToWindow registers it with AccessibilityNodeIdManager).
            // ROOT_ITEM_ID is shared by every window root; viewFor maps it to the current root.
            (m.invoke(view) as? Int)?.let { if (it != A11yIds.ROOT_ITEM_ID) ctx.byA11yId[it] = view }
        } catch (t: Throwable) {
            ctx.fail("View.getAccessibilityViewId()", t)
        }
        if (view is ViewGroup) {
            val n = try {
                view.childCount
            } catch (_: Throwable) {
                0
            }
            for (i in 0 until n) {
                val child = try {
                    view.getChildAt(i)
                } catch (_: Throwable) {
                    null
                } ?: continue
                indexViews(child, m, ctx, depth + 1)
            }
        }
    }

    /** The View whose accessibility view id is [aid], or null. */
    private fun viewFor(aid: Int, ctx: Ctx): View? {
        if (aid == A11yIds.UNDEFINED_ITEM_ID) return null
        if (aid == A11yIds.ROOT_ITEM_ID) return ctx.currentRoot
        ctx.byA11yId[aid]?.let { return it }
        if (getAccessibilityViewIdM != null && ctx.indexBuilds < MAX_INDEX_BUILDS) {
            buildIndex(ctx)
            ctx.byA11yId[aid]?.let { return it }
        }
        // Last resort: the framework's own registry (only returns includeForAccessibility Views).
        val (inst, find) = idManager ?: return null
        return try {
            (find.invoke(inst, aid) as? View)?.also { ctx.byA11yId[aid] = it }
        } catch (t: Throwable) {
            ctx.fail("AccessibilityNodeIdManager.findView", t)
            null
        }
    }

    /** The node's own packed id (low = accessibility view id, high = virtual id), or null. */
    private fun sourceNodeId(node: AccessibilityNodeInfo, ctx: Ctx): Long? {
        val m = getSourceNodeIdM ?: return null
        return try {
            m.invoke(node) as? Long
        } catch (t: Throwable) {
            ctx.fail("AccessibilityNodeInfo.getSourceNodeId()", t)
            null
        }
    }

    /** The packed id of child [index] of [node], or null when unreachable. */
    private fun childIdAt(node: AccessibilityNodeInfo, index: Int, ctx: Ctx): Long? {
        val m = getChildIdM ?: return null
        return try {
            m.invoke(node, index) as? Long
        } catch (t: Throwable) {
            ctx.fail("AccessibilityNodeInfo.getChildId(int)", t)
            null
        }
    }

    /**
     * Resolve [node]'s identity from its own packed source id, else from the packed id its
     * parent listed for it ([childIdFromParent]). Never guesses: an id whose View can't be
     * found yields host_view_id 0 (and is counted), not a neighbour's id.
     */
    private fun identify(node: AccessibilityNodeInfo, childIdFromParent: Long?, ctx: Ctx): Ident {
        var firstVid: Int? = null
        for (packed in arrayOf(sourceNodeId(node, ctx), childIdFromParent)) {
            if (packed == null || A11yIds.isUndefined(packed)) continue
            val vid = A11yIds.virtualIdOf(packed)
            if (firstVid == null) firstVid = vid
            val view = viewFor(A11yIds.accessibilityViewIdOf(packed), ctx) ?: continue
            return Ident(view, vid)
        }
        ctx.unresolvedNodes++
        return Ident(null, firstVid ?: HOST_VIEW_ID)
    }

    /** Convert a packed linkage id into the host node-key space; 0 = none / unresolvable. */
    private fun hostKeyOf(packed: Long?, ctx: Ctx): Long {
        if (packed == null || A11yIds.isUndefined(packed)) return 0L
        val view = viewFor(A11yIds.accessibilityViewIdOf(packed), ctx)
        val hostViewId = view?.let { idOf(it) } ?: 0L
        if (hostViewId == 0L) {
            ctx.unresolvedLinks++
            return 0L
        }
        return A11yIds.hostKey(hostViewId, A11yIds.virtualIdOf(packed))
    }

    private fun idOf(view: View): Long = try {
        view.uniqueDrawingId
    } catch (t: Throwable) {
        Log.w(TAG, "getUniqueDrawingId() failed", t)
        0L
    }

    /** On a provider host: the host View's class name; null for a View without a provider. */
    private fun providerHostClass(view: View): String? {
        val provider = try {
            view.accessibilityNodeProvider
        } catch (_: Throwable) {
            null
        }
        return if (provider != null) view.javaClass.name else null
    }

    // ------------------------------------------------------------ the walk

    /** Local-mode root: the root's own node, resolved and walked without a connection. */
    private fun walkLocalRoot(root: View, ctx: Ctx): ViewInspection.A11yNode? {
        val node = try {
            root.createAccessibilityNodeInfo()
        } catch (t: Throwable) {
            Log.w(TAG, "createAccessibilityNodeInfo() failed on root", t)
            null
        } ?: return null
        return walk(node, identify(node, null, ctx), ctx, 0, local = true)
    }

    /**
     * Resolve a packed child id without a connection, mirroring AOSP
     * AccessibilityInteractionController (findAccessibilityNodeInfoByAccessibilityIdUiThread +
     * populateAccessibilityNodeInfoForView): find the View, skip it unless shown, then ask its
     * provider for the virtual id (HOST_VIEW_ID included) or, with no provider, the View itself.
     */
    private fun resolveLocal(packed: Long, ctx: Ctx): AccessibilityNodeInfo? {
        if (A11yIds.isUndefined(packed)) return null
        val view = viewFor(A11yIds.accessibilityViewIdOf(packed), ctx) ?: return null
        val shown = try {
            view.windowVisibility == View.VISIBLE && view.isShown
        } catch (_: Throwable) {
            false
        }
        if (!shown) return null
        val vid = A11yIds.virtualIdOf(packed)
        return try {
            val provider = view.accessibilityNodeProvider
            if (provider != null) {
                provider.createAccessibilityNodeInfo(vid)
            } else {
                view.createAccessibilityNodeInfo()
            }
        } catch (t: Throwable) {
            Log.w(TAG, "local node resolution failed for virtual id $vid", t)
            null
        }
    }

    /**
     * Emit [node] (identity [ident]) and recurse. In query mode children come from
     * getChild(i); in local mode from [resolveLocal] on the packed child ids. Either way
     * each child's identity is decoded from its own packed id, never inherited.
     */
    private fun walk(
        node: AccessibilityNodeInfo,
        ident: Ident,
        ctx: Ctx,
        depth: Int,
        local: Boolean,
    ): ViewInspection.A11yNode {
        val b = ViewInspection.A11yNode.newBuilder()
        val view = ident.view
        b.hostViewId = view?.let { idOf(it) } ?: 0L
        b.virtualId = ident.virtualId
        b.isVirtual = ident.virtualId != HOST_VIEW_ID
        if (!b.isVirtual && view != null) {
            providerHostClass(view)?.let { b.providerClass = ctx.strings.intern(it) }
        }

        mapNode(node, ident, b, ctx, local)
        if (!local && depth > 0 && (ctx.boundsDx != 0 || ctx.boundsDy != 0)) {
            val l = b.bounds.layout
            b.bounds = b.bounds.toBuilder()
                .setLayout(l.toBuilder().setX(l.x + ctx.boundsDx).setY(l.y + ctx.boundsDy))
                .build()
        }

        if (depth < ctx.maxDepth && ctx.count < ctx.maxNodes) {
            val n = safeInt { node.childCount }
            val childIds = LongArray(n)
            var haveIds = true
            for (i in 0 until n) {
                val id = childIdAt(node, i, ctx)
                if (id == null) haveIds = false else childIds[i] = id
            }
            if (n > 0 && local && !haveIds) {
                walkLocalChildrenWithoutIds(ident, b, ctx, depth)
            } else {
                for (i in 0 until n) {
                    if (ctx.count >= ctx.maxNodes) break
                    val childId: Long? = if (haveIds) childIds[i] else null
                    val child: AccessibilityNodeInfo? =
                        if (local) {
                            resolveLocal(childIds[i], ctx)
                        } else {
                            try {
                                childOf(node, i)
                            } catch (t: Throwable) {
                                // A throwing / transiently-detached child is skipped.
                                null
                            }
                        }
                    if (child == null) {
                        ctx.nullChildren++
                        continue
                    }
                    ctx.count++
                    b.addChildren(walk(child, identify(child, childId, ctx), ctx, depth + 1, local))
                }
            }
        }
        if (depth >= ctx.maxDepth && safeInt { node.childCount } > 0) {
            // The depth cap (the wire cap for a dump): this node's children were not walked.
            b.childrenTruncated = true
            ctx.depthTruncated++
        }
        return b.build()
    }

    /**
     * Local mode without the hidden getChildId: a real, provider-less ViewGroup can still list
     * its accessibility children through the public View.addChildrenForAccessibility (the same
     * call ViewGroup.onInitializeAccessibilityNodeInfoInternal uses to fill the child ids).
     * Virtual children of a provider can't be enumerated this way; they are counted.
     */
    private fun walkLocalChildrenWithoutIds(
        ident: Ident,
        b: ViewInspection.A11yNode.Builder,
        ctx: Ctx,
        depth: Int,
    ) {
        val view = ident.view
        val hasProvider = view != null && providerHostClass(view) != null
        if (view == null || ident.virtualId != HOST_VIEW_ID || hasProvider || view !is ViewGroup) {
            ctx.unenumerable++
            return
        }
        val kids = ArrayList<View>()
        try {
            view.addChildrenForAccessibility(kids)
        } catch (t: Throwable) {
            Log.w(TAG, "addChildrenForAccessibility failed", t)
            return
        }
        for (child in kids) {
            if (ctx.count >= ctx.maxNodes) break
            val ani = try {
                child.createAccessibilityNodeInfo()
            } catch (t: Throwable) {
                null
            }
            if (ani == null) {
                ctx.nullChildren++
                continue
            }
            ctx.count++
            b.addChildren(walk(ani, Ident(child, HOST_VIEW_ID), ctx, depth + 1, local = true))
        }
    }

    // ------------------------------------------------------------ node mapping

    /**
     * Emit EVERY [ViewInspection.A11yNode] field from [node]. Each getter is individually
     * guarded; one failing field never aborts the node.
     *
     * DEPRECATION is suppressed because several accessors we MUST surface to fill
     * the proto are deprecated-but-still-canonical: AccessibilityNodeInfo.isChecked
     * (proto keeps both the legacy bool AND the tri-state checked_state),
     * CollectionItemInfo.isHeading (only accessor for the proto heading bool),
     * getActions() (legacy bitmask field), and Bundle.get() (generic extras read).
     */
    @Suppress("DEPRECATION")
    private fun mapNode(
        node: AccessibilityNodeInfo,
        ident: Ident,
        b: ViewInspection.A11yNode.Builder,
        ctx: Ctx,
        local: Boolean,
    ) {
        val strings = ctx.strings
        fun s(cs: CharSequence?): Int = strings.intern(cs?.toString())

        // --- text & description ------------------------------------------------
        b.text = s(safe { a11yText(node, ident, ctx) })
        b.contentDescription = s(safe { node.contentDescription })
        b.hintText = s(safe { node.hintText })
        b.stateDescription = s(safe { node.stateDescription })
        b.error = s(safe { node.error })
        b.tooltipText = s(safe { node.tooltipText })
        b.paneTitle = s(safe { node.paneTitle })
        b.containerTitle = s(safe { node.containerTitle })
        // supplementalDescription is API 35+: NoSuchMethodError is caught by safe{}.
        b.supplementalDescription = s(safe { node.supplementalDescription })
        b.textSelectionStart = safeInt { node.textSelectionStart }
        b.textSelectionEnd = safeInt { node.textSelectionEnd }

        // --- class / package / resource ---------------------------------------
        b.className = s(safe { node.className })
        b.packageName = s(safe { node.packageName })
        b.viewIdResourceName = strings.intern(safe { node.viewIdResourceName })
        b.uniqueId = strings.intern(safe { node.uniqueId })

        // --- bounds in screen (absolute px -> Bounds.layout) ------------------
        val r = Rect()
        try {
            node.getBoundsInScreen(r)
        } catch (_: Throwable) {
        }
        // A node clipped away by an ancestor (an off-screen pager page, a row scrolled out
        // of its list) comes back inverted: View.getBoundsOnScreen clamps each edge to every
        // parent separately, so right < left or bottom < top (seen: h=-2159). Clamp the size
        // to 0 (a zero-area node) and flag it rather than send a negative extent.
        val clipped = r.right < r.left || r.bottom < r.top
        b.bounds = ViewInspection.Bounds.newBuilder()
            .setLayout(
                ViewInspection.Rect.newBuilder()
                    .setX(r.left).setY(r.top)
                    .setW(maxOf(0, r.width())).setH(maxOf(0, r.height())),
            )
            .build()
        if (clipped) b.boundsClipped = true

        // --- boolean state flags ----------------------------------------------
        b.clickable = bool { node.isClickable }
        b.longClickable = bool { node.isLongClickable }
        b.contextClickable = bool { node.isContextClickable }
        b.checkable = bool { node.isCheckable }
        b.checked = bool { node.isChecked }
        // tri-state getChecked(): CHECKED_STATE_FALSE/PARTIAL/TRUE (API 36+).
        b.checkedState = safeInt { node.checked }
        b.focusable = bool { node.isFocusable }
        b.focused = bool { node.isFocused }
        b.accessibilityFocused = bool { node.isAccessibilityFocused }
        b.selected = bool { node.isSelected }
        b.enabled = bool { node.isEnabled }
        b.password = bool { node.isPassword }
        b.scrollable = bool { node.isScrollable }
        b.visibleToUser = bool { node.isVisibleToUser }
        b.heading = bool { node.isHeading }
        b.screenReaderFocusable = bool { node.isScreenReaderFocusable }
        b.dismissable = bool { node.isDismissable }
        b.editable = bool { node.isEditable }
        b.multiLine = bool { node.isMultiLine }
        b.contentInvalid = bool { node.isContentInvalid }
        b.showingHintText = bool { node.isShowingHintText }
        b.textEntryKey = bool { node.isTextEntryKey }
        b.textSelectable = bool { node.isTextSelectable }
        b.fieldRequired = bool { node.isFieldRequired }
        b.canOpenPopup = bool { node.canOpenPopup() }
        // API 34+; bool{} swallows NoSuchMethodError on older runtimes.
        b.a11YDataSensitive = bool { node.isAccessibilityDataSensitive }
        b.requestInitialFocus = bool { node.hasRequestInitialAccessibilityFocus() }
        if (!ctx.lite) {
            b.isTraversalGroup = traversalGroup(node, ident, ctx)
            composeLayoutSize(ident, ctx)?.let { (w, h) ->
                b.layoutSizeW = w
                b.layoutSizeH = h
            }
        }

        // importantForAccessibility. A real View reports its mode, except that AUTO is reported
        // as YES when the View resolves to important (node.isImportantForAccessibility, set from
        // View.isImportantForAccessibility(): actionable, listeners, an accessibility delegate,
        // a provider, a live region, a pane title or a heading). So AUTO on a real View means
        // "left at auto and NOT important": TalkBack, which does not request not-important
        // Views, never sees it and reads its children in its place. The in-process connection
        // fetches not-important Views too (DirectAccessibilityConnection), so the host needs
        // this to model what TalkBack sees. A virtual node (no View of its own) reports the ANI
        // predicate the same way: YES or AUTO.
        val view = ident.view
        val important = bool { node.isImportantForAccessibility }
        b.importantForAccessibility =
            if (view != null && !b.isVirtual) {
                val mode = safeInt { view.importantForAccessibility }
                if (mode == View.IMPORTANT_FOR_ACCESSIBILITY_AUTO && important) {
                    View.IMPORTANT_FOR_ACCESSIBILITY_YES
                } else {
                    mode
                }
            } else {
                if (important) View.IMPORTANT_FOR_ACCESSIBILITY_YES
                else View.IMPORTANT_FOR_ACCESSIBILITY_AUTO
            }

        // --- live region / input ----------------------------------------------
        b.liveRegion = safeInt { node.liveRegion }
        b.inputType = safeInt { node.inputType }
        b.movementGranularities = safeInt { node.movementGranularities }
        b.maxTextLength = safeInt { node.maxTextLength }
        b.expandedState = safeInt { node.expandedState }
        b.drawingOrder = safeInt { node.drawingOrder }
        b.actionsBitmask = safeInt { node.actions }

        // --- collections & ranges ---------------------------------------------
        safe { node.collectionInfo }?.let { ci ->
            val cb = ViewInspection.A11yCollectionInfo.newBuilder()
            cb.rowCount = safeInt { ci.rowCount }
            cb.columnCount = safeInt { ci.columnCount }
            cb.hierarchical = bool { ci.isHierarchical }
            cb.selectionMode = safeInt { ci.selectionMode }
            // getItemCount / getImportantForAccessibilityItemCount are newer APIs; safeInt
            // swallows the NoSuchMethodError on older runtimes.
            cb.itemCount = safeInt { ci.itemCount }
            cb.importantItemCount = safeInt { ci.importantForAccessibilityItemCount }
            b.collectionInfo = cb.build()
        }
        safe { node.collectionItemInfo }?.let { cii ->
            val ib = ViewInspection.A11yCollectionItemInfo.newBuilder()
            ib.rowIndex = safeInt { cii.rowIndex }
            ib.columnIndex = safeInt { cii.columnIndex }
            ib.rowSpan = safeInt { cii.rowSpan }
            ib.columnSpan = safeInt { cii.columnSpan }
            ib.heading = bool { cii.isHeading }
            ib.selected = bool { cii.isSelected }
            ib.rowTitle = strings.intern(safe { cii.rowTitle })
            ib.columnTitle = strings.intern(safe { cii.columnTitle })
            b.collectionItemInfo = ib.build()
        }
        safe { node.rangeInfo }?.let { ri ->
            b.rangeInfo = ViewInspection.A11yRangeInfo.newBuilder()
                .setType(safeInt { ri.type })
                .setMin(safeFloat { ri.min })
                .setMax(safeFloat { ri.max })
                .setCurrent(safeFloat { ri.current })
                .build()
        }

        // --- actions (id + custom-action label) -------------------------------
        try {
            node.actionList?.forEach { a ->
                if (a == null) return@forEach
                b.addActions(
                    ViewInspection.A11yAction.newBuilder()
                        .setId(safeInt { a.id })
                        .setLabel(strings.intern(safe { a.label }?.toString()))
                        .build(),
                )
            }
        } catch (_: Throwable) {
        }

        // --- extras (roleDescription, compose testTag/id …) -------------------
        val extras: Bundle? = safe { node.extras }
        if (extras != null) {
            // roleDescription: the spoken role TalkBack reads.
            safe { extras.getCharSequence(ROLE_DESC_KEY) }?.let {
                b.roleDescription = strings.intern(it.toString())
            }

            if (ctx.includeExtras) {
                var c = 0
                val keys = try {
                    extras.keySet()
                } catch (_: Throwable) {
                    emptySet<String>()
                }
                for (k in keys) {
                    if (c >= MAX_EXTRAS) break
                    c++
                    val raw = try {
                        extras.get(k)?.toString()
                    } catch (_: Throwable) {
                        null
                    }
                    // Skip large stringified Parcelables.
                    val v = if (raw != null && raw.length > MAX_EXTRA_VALUE_LEN) null else raw
                    b.addExtras(
                        ViewInspection.A11yExtra.newBuilder()
                            .setKey(strings.intern(k))
                            .setValue(strings.intern(v))
                            .build(),
                    )
                }
            }
        }

        // --- traversal / label linkage, in the HOST NODE KEY space ------------
        b.traversalBefore = hostKeyOf(linkId(node, traversalBeforeF, "getTraversalBefore", ctx, local), ctx)
        b.traversalAfter = hostKeyOf(linkId(node, traversalAfterF, "getTraversalAfter", ctx, local), ctx)
        b.labelFor = hostKeyOf(linkId(node, labelForF, "getLabelFor", ctx, local), ctx)
        b.labeledBy = hostKeyOf(linkId(node, labeledByF, "getLabeledBy", ctx, local), ctx)
        for (packed in labeledByIds(node, ctx, local)) {
            val key = hostKeyOf(packed, ctx)
            if (key != 0L) b.addLabeledByList(key)
        }

        // --- ExtraRenderingInfo (opt-in; needs the query-mode connection) -----
        if (ctx.includeRenderingInfo && !local) {
            try {
                node.refreshWithExtraData(
                    AccessibilityNodeInfo.EXTRA_DATA_RENDERING_INFO_KEY, Bundle(),
                )
                node.extraRenderingInfo?.let { eri ->
                    safe { eri.layoutSize }?.let { sz ->
                        b.layoutSizeW = safeInt { sz.width }
                        b.layoutSizeH = safeInt { sz.height }
                    }
                    b.textSizePx = safeFloat { eri.textSizeInPx }
                    b.textSizeUnit = safeInt { eri.textSizeUnit }
                }
            } catch (_: Throwable) {
            }
        }
    }

    // ------------------------------------------------------------ linkage helpers

    /**
     * The packed id stored in linkage field [f] of [node]. When the field itself is
     * unreachable, fall back to the public getter [getterName] (resolves the target node
     * through the connection; query mode only) and read the target's own packed id.
     */
    private fun linkId(
        node: AccessibilityNodeInfo,
        f: Field?,
        getterName: String,
        ctx: Ctx,
        local: Boolean,
    ): Long? {
        if (f != null) {
            try {
                return f.getLong(node)
            } catch (t: Throwable) {
                ctx.fail("read AccessibilityNodeInfo.${f.name}", t)
            }
        }
        // The getter needs a connection (query mode); an unsealed local-mode node would throw.
        if (local) return null
        val target = try {
            AccessibilityNodeInfo::class.java.getMethod(getterName).invoke(node) as? AccessibilityNodeInfo
        } catch (t: Throwable) {
            ctx.fail("AccessibilityNodeInfo.$getterName()", t)
            null
        } ?: return null
        return sourceNodeId(target, ctx)
    }

    /** The packed ids of the multiple-labeledBy list (API 35+); empty when none / unavailable. */
    private fun labeledByIds(node: AccessibilityNodeInfo, ctx: Ctx, local: Boolean): List<Long> {
        val f = labeledByIdsF
        if (f != null) {
            try {
                val arr = f.get(node) ?: return emptyList()
                val size = arr.javaClass.getMethod("size").invoke(arr) as Int
                val get = arr.javaClass.getMethod("get", Int::class.javaPrimitiveType)
                return (0 until size).map { get.invoke(arr, it) as Long }
            } catch (t: Throwable) {
                ctx.fail("read AccessibilityNodeInfo.mLabeledByIds", t)
            }
        }
        // Public getLabeledByList() (API 35+): resolves targets through the connection, so
        // query mode only.
        if (local || Build.VERSION.SDK_INT < 35) return emptyList()
        return try {
            val list = AccessibilityNodeInfo::class.java.getMethod("getLabeledByList").invoke(node) as? List<*>
            list?.mapNotNull { (it as? AccessibilityNodeInfo)?.let { t -> sourceNodeId(t, ctx) } } ?: emptyList()
        } catch (t: Throwable) {
            ctx.fail("AccessibilityNodeInfo.getLabeledByList()", t)
            emptyList()
        }
    }

    /**
     * is_traversal_group: the framework accessor when a platform has one; otherwise, for a node
     * served by an AndroidComposeView, the IsTraversalGroup flag of its SemanticsNode (the host
     * node itself stands for the unmerged root SemanticsNode). Views have no such concept.
     */
    private fun traversalGroup(node: AccessibilityNodeInfo, ident: Ident, ctx: Ctx): Boolean {
        isTraversalGroupM?.let { m ->
            return try {
                m.invoke(node) as? Boolean ?: false
            } catch (t: Throwable) {
                ctx.fail("AccessibilityNodeInfo.isTraversalGroup()", t)
                false
            }
        }
        val index = composeIndexOf(ident, ctx) ?: return false
        val semId = if (ident.virtualId == HOST_VIEW_ID) index.rootSemanticsId else ident.virtualId
        return semId in index.traversalGroups
    }

    /** The semantics index of the AndroidComposeView behind [ident], or null for other nodes. */
    private fun composeIndexOf(ident: Ident, ctx: Ctx): ComposeInspector.SemanticsIndex? {
        val view = ident.view ?: return null
        if (!ComposeInspector.isAndroidComposeView(view)) return null
        return if (ctx.composeIndex.containsKey(view)) {
            ctx.composeIndex[view]
        } else {
            ComposeInspector.semanticsIndex(view).also { ctx.composeIndex[view] = it }
        }
    }

    /**
     * layout_size_w/h for a Compose virtual node: the measured size of its LayoutNode (px). A
     * Compose node reports no ExtraRenderingInfo, and its boundsInScreen are its touch bounds,
     * which Compose widens to the minimum touch size for any clickable; this is the space the
     * node actually reserves (Modifier.minimumInteractiveComponentSize() included), so the lint
     * can tell a 48dp Material control from a 24dp clickable. Null for View nodes, the
     * AndroidComposeView's own node, and Compose's synthetic role/contentDescription nodes.
     */
    private fun composeLayoutSize(ident: Ident, ctx: Ctx): Pair<Int, Int>? {
        if (ident.virtualId == HOST_VIEW_ID) return null
        val packed = composeIndexOf(ident, ctx)?.layoutSizes?.get(ident.virtualId) ?: return null
        return (packed ushr 32).toInt() to packed.toInt()
    }

    // ------------------------------------------------------------ reflection helpers

    private fun method(cls: Class<*>, name: String, vararg params: Class<*>): Method? =
        try {
            (
                try {
                    cls.getDeclaredMethod(name, *params)
                } catch (_: NoSuchMethodException) {
                    cls.getMethod(name, *params)
                }
                ).also { it.isAccessible = true }
        } catch (t: Throwable) {
            Log.w(TAG, "${cls.simpleName}.$name not reachable via reflection", t)
            null
        }

    private fun field(cls: Class<*>, name: String, logMissing: Boolean = true): Field? =
        try {
            cls.getDeclaredField(name).also { it.isAccessible = true }
        } catch (t: Throwable) {
            if (logMissing) Log.w(TAG, "${cls.simpleName}.$name not reachable via reflection", t)
            null
        }

    // ------------------------------------------------------------ guard helpers

    /**
     * Child [i] of a connection-backed node, fetched without prefetching on API 33+
     * (getChild(int, int) with strategy 0). Plain getChild asks the interaction controller
     * to prefetch up to 50 descendants per call; the in-process connection has no cache to
     * keep them in and the walk fetches every node itself anyway, so that was main-thread
     * work thrown away on every node.
     */
    private fun childOf(node: AccessibilityNodeInfo, i: Int): AccessibilityNodeInfo? =
        if (Build.VERSION.SDK_INT >= 33) node.getChild(i, 0) else node.getChild(i)

    /**
     * The node's text, masked (Redaction.kt) for a password field ([passwordState]), and for
     * an editable node whose status is UNKNOWN (fail closed; listed in ctx.redaction). A node
     * showing its hint keeps it.
     */
    private fun a11yText(node: AccessibilityNodeInfo, ident: Ident, ctx: Ctx): CharSequence? {
        val text = node.text
        if (text.isNullOrEmpty() || bool { node.isShowingHintText }) return text
        val state = passwordState(node, ident, ctx)
        if (!Redaction.mustMask(state, isEditable(node))) return text
        if (state == Redaction.PasswordState.UNKNOWN) ctx.redaction.masked(keyOf(ident))
        return Redaction.mask(text)
    }

    /**
     * Whether [node] is a password field: it says so (isPassword), its input type is a
     * password variation (a visible-password field is not isPassword, yet its text is the
     * plaintext), its View is one (Redaction.passwordStateOf: a password transformation or
     * autofill hint), or it is a Compose password field ([composePassword]). UNKNOWN when its
     * View is unresolved, or for a Compose node whose status cannot be determined. Another
     * provider's virtual node is taken at its word (isPassword / input type).
     */
    private fun passwordState(node: AccessibilityNodeInfo, ident: Ident, ctx: Ctx): Redaction.PasswordState {
        if (bool { node.isPassword } || Redaction.isPasswordInputType(safeInt { node.inputType })) {
            return Redaction.PasswordState.PASSWORD
        }
        val view = ident.view ?: return Redaction.PasswordState.UNKNOWN
        if (ident.virtualId == HOST_VIEW_ID) return Redaction.passwordStateOf(view)
        if (ComposeInspector.isAndroidComposeView(view)) return composePassword(view, ident.virtualId, ctx)
        return Redaction.PasswordState.NOT_PASSWORD
    }

    /** An editable text node: isEditable, an EditText class name, or a SET_TEXT action. */
    private fun isEditable(node: AccessibilityNodeInfo): Boolean =
        bool { node.isEditable } ||
            Redaction.isEditableClassName(safe { node.className }) ||
            safe { node.actionList }?.any { it.id == AccessibilityNodeInfo.ACTION_SET_TEXT } == true

    /** The node key of [ident] in the host's form (view:, compose:, virtual:), for diagnostics. */
    private fun keyOf(ident: Ident): String {
        val view = ident.view
        val host = view?.let { idOf(it) } ?: 0L
        if (ident.virtualId == HOST_VIEW_ID) return "view:$host"
        val compose = view != null && ComposeInspector.isAndroidComposeView(view)
        return "${if (compose) "compose" else "virtual"}:$host:${ident.virtualId}"
    }

    /**
     * The password status of Compose virtual node [virtualId] of [view]
     * (ComposeInspector.passwordState). Compose sets no input type, so a visible-password
     * field's node is neither isPassword nor a password input type. From the dump's semantics
     * index; the lite snapshot (a node or two) looks up its one node instead, and builds the
     * index after [LITE_COMPOSE_LOOKUPS] lookups (a deep subtree would walk the tree per node).
     * UNKNOWN when the semantics tree is out of reach (R8 renamed Compose), and a renamed
     * AndroidComposeView is noted as one whose password fields cannot be identified.
     */
    private fun composePassword(view: View, virtualId: Int, ctx: Ctx): Redaction.PasswordState {
        if (ComposeInspector.isRenamed(view)) ctx.redaction.composeView(idOf(view))
        if (ctx.lite && ctx.composeLookups++ < LITE_COMPOSE_LOOKUPS) {
            return ComposeInspector.passwordState(view, virtualId)
        }
        val index = composeIndexOf(Ident(view, virtualId), ctx)
        if (index == null) {
            ctx.redaction.composeView(idOf(view))
            return Redaction.PasswordState.UNKNOWN
        }
        return index.passwordState(virtualId)
    }

    private inline fun <T> safe(block: () -> T): T? = try {
        block()
    } catch (t: Throwable) {
        null
    }

    private inline fun bool(block: () -> Boolean): Boolean = try {
        block()
    } catch (t: Throwable) {
        false
    }

    private inline fun safeInt(block: () -> Int): Int = try {
        block()
    } catch (t: Throwable) {
        0
    }

    private inline fun safeFloat(block: () -> Float): Float = try {
        block()
    } catch (t: Throwable) {
        0f
    }
}
