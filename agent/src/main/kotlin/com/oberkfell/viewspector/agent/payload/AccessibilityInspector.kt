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
 * and ComposeNode.bounds.
 */
package com.oberkfell.viewspector.agent.payload

import android.graphics.Rect
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.view.View
import android.view.ViewGroup
import android.view.accessibility.AccessibilityManager
import android.view.accessibility.AccessibilityNodeInfo
import com.oberkfell.viewspector.proto.ViewInspection
import java.lang.reflect.Field
import java.lang.reflect.Method

object AccessibilityInspector {

    private const val TAG = "ViewSpector"

    // Loop / fan-out guards.
    private const val MAX_DEPTH = 250
    private const val MAX_NODES = 5000
    private const val VIEW_MAX_DEPTH = 400

    // How many times one dump may rebuild the accessibility-id -> View index after a miss
    // (Views attached mid-walk, e.g. a RecyclerView laying out a new cell).
    private const val MAX_INDEX_BUILDS = 3

    private const val HOST_VIEW_ID = A11yIds.HOST_VIEW_ID

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
    ) {
        var count = 0
        val byA11yId = HashMap<Int, View>()
        var indexBuilds = 0
        var unresolvedNodes = 0
        var unresolvedLinks = 0
        var nullChildren = 0
        var unenumerable = 0
        var reflectFailures = 0
        val loggedFailures = HashSet<String>()
        val composeIndex = HashMap<View, ComposeInspector.SemanticsIndex?>()

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
     */
    fun dump(
        rootViews: List<View>,
        strings: StringTable,
        includeExtras: Boolean,
        includeRenderingInfo: Boolean,
    ): Pair<List<ViewInspection.DumpA11yResponse.Window>, String> {
        val ctx = Ctx(rootViews, strings, includeExtras, includeRenderingInfo)
        val diag = StringBuilder("roots=${rootViews.size}; api=${Build.VERSION.SDK_INT}; ids=host-key")
        val windows = ArrayList<ViewInspection.DumpA11yResponse.Window>()

        // Compose computes traversal_before/after (setTraversalValues) only while an accessibility
        // service is on (AndroidComposeViewAccessibilityDelegateCompat.isEnabled, ui 1.7 to 1.12), so
        // without one those fields are empty for Compose nodes. Say so; the host must not read
        // missing linkage as "no ordering constraints".
        val a11yOn = try {
            rootViews.firstOrNull()?.context
                ?.getSystemService(AccessibilityManager::class.java)?.isEnabled
        } catch (t: Throwable) {
            Log.w(TAG, "AccessibilityManager.isEnabled failed", t)
            null
        }
        when (a11yOn) {
            true -> diag.append("; a11y-services=on")
            false -> diag.append("; a11y-services=off (Compose omits traversal_before/after)")
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
            // The host node on which app-process query mode was enabled; reset on it in finally.
            var enabledNode: AccessibilityNodeInfo? = null
            val countBefore = ctx.count
            try {
                var node: ViewInspection.A11yNode? = null
                if (Build.VERSION.SDK_INT >= 34) {
                    try {
                        val hostNode = root.createAccessibilityNodeInfo()
                        if (hostNode != null) {
                            hostNode.setQueryFromAppProcessEnabled(root, true)
                            enabledNode = hostNode
                            node = walk(hostNode, identify(hostNode, null, ctx), ctx, 0, local = false)
                            diag.append("; root#${idOf(root)} query-from-app-process")
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
                if (node != null) windows.add(window(root, node))
            } catch (t: Throwable) {
                Log.w(TAG, "a11y dump failed for root", t)
                diag.append("; root#${idOf(root)} error ${t.javaClass.simpleName}")
            } finally {
                resetQueryMode(enabledNode, root)
            }
        }

        diag.append("; nodes=${ctx.count}; views-indexed=${ctx.byA11yId.size}")
        if (ctx.unresolvedNodes > 0) diag.append("; unresolved-nodes=${ctx.unresolvedNodes}")
        if (ctx.unresolvedLinks > 0) diag.append("; unresolved-links=${ctx.unresolvedLinks}")
        if (ctx.nullChildren > 0) diag.append("; null-children=${ctx.nullChildren}")
        if (ctx.unenumerable > 0) diag.append("; provider-children-unreachable=${ctx.unenumerable}")
        if (ctx.reflectFailures > 0) diag.append("; reflect-failures=${ctx.reflectFailures}")
        return windows to diag.toString()
    }

    private fun resetQueryMode(node: AccessibilityNodeInfo?, root: View) {
        if (node == null) return
        try {
            node.setQueryFromAppProcessEnabled(root, false)
        } catch (t: Throwable) {
            Log.w(TAG, "setQueryFromAppProcessEnabled(false) failed", t)
        }
    }

    private fun window(root: View, node: ViewInspection.A11yNode): ViewInspection.DumpA11yResponse.Window =
        ViewInspection.DumpA11yResponse.Window.newBuilder()
            .setRootViewId(idOf(root))
            .setRoot(node)
            .build()

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
            (m.invoke(view) as? Int)?.let { ctx.byA11yId[it] = view }
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
        if (aid == A11yIds.UNDEFINED_ITEM_ID || aid == A11yIds.ROOT_ITEM_ID) return null
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

        if (depth < MAX_DEPTH && ctx.count < MAX_NODES) {
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
                    if (ctx.count >= MAX_NODES) break
                    val childId: Long? = if (haveIds) childIds[i] else null
                    val child: AccessibilityNodeInfo? =
                        if (local) {
                            resolveLocal(childIds[i], ctx)
                        } else {
                            try {
                                node.getChild(i)
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
            if (ctx.count >= MAX_NODES) break
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
        b.text = s(safe { node.text })
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
        b.bounds = ViewInspection.Bounds.newBuilder()
            .setLayout(
                ViewInspection.Rect.newBuilder()
                    .setX(r.left).setY(r.top).setW(r.width()).setH(r.height()),
            )
            .build()

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
        b.isTraversalGroup = traversalGroup(node, ident, ctx)

        // importantForAccessibility — from the backing View for a real View node; for a
        // virtual node (no View of its own) fall back to the ANI predicate.
        val view = ident.view
        b.importantForAccessibility =
            if (view != null && !b.isVirtual) {
                safeInt { view.importantForAccessibility }
            } else {
                if (bool { node.isImportantForAccessibility }) View.IMPORTANT_FOR_ACCESSIBILITY_YES
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
        val view = ident.view ?: return false
        if (!ComposeInspector.isAndroidComposeView(view)) return false
        val index = if (ctx.composeIndex.containsKey(view)) {
            ctx.composeIndex[view]
        } else {
            ComposeInspector.semanticsIndex(view).also { ctx.composeIndex[view] = it }
        } ?: return false
        val semId = if (ident.virtualId == HOST_VIEW_ID) index.rootSemanticsId else ident.virtualId
        return semId in index.traversalGroups
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
