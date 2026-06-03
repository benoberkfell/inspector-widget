/*
 * ViewSpector — payload :: accessibility (AccessibilityNodeInfo) tree extraction.
 *
 * In-process extraction of the unified accessibility tree — classic Views AND
 * Compose virtual semantics nodes — exactly as Assistive Technology (TalkBack /
 * UiAutomator) observe it. See /tmp/a11y_design/extraction.md for the verified
 * API facts and rationale. The whole dump runs on the MAIN THREAD (caller hops
 * via MainThread.run), since every getter touches live View / ANI state.
 *
 * Strategy (design §1):
 *   1. Per root, call root.setQueryFromAppProcessEnabled(root, true) — the public
 *      API-34+ switch that makes AccessibilityNodeInfo.getChild()/getParent()
 *      resolve against the live in-process hierarchy (no AccessibilityService
 *      connection needed). Reset to false in a finally (design §8).
 *   2. node = root.createAccessibilityNodeInfo() — the node AS COMPOSED by the
 *      View's AccessibilityDelegate / provider, i.e. what AT actually sees.
 *   3. Recurse via node.getChildCount() / node.getChild(i). Because getChild()
 *      dispatches through the AccessibilityNodeProvider for virtual nodes, this
 *      single recursion TRANSPARENTLY covers Compose semantics children — no
 *      separate Compose branch is needed for the a11y tree (design §1).
 *
 * Fallback (design §4): when setQueryFromAppProcessEnabled is unavailable or
 * throws (older OEM build / API < 34), walk the View hierarchy structurally and
 * switch to the provider path (provider.createAccessibilityNodeInfo) for
 * AndroidComposeView / WebView, whose locally-built nodes carry child linkage
 * even without query-from-app-process.
 *
 * Recycling (design §8, §0 "Recycling caveat"): AccessibilityNodeInfo.recycle()
 * is a deprecated no-op since API 33 and double-recycle is a latent crash. On
 * API 34+ we do NOT call recycle() at all — references are dropped at the end of
 * the single MainThread.run block and GC reclaims them.
 *
 * Bounds: getBoundsInScreen() yields ABSOLUTE screen px, matching
 * ViewNode.bounds.layout, so the host can correlate a11y node ↔ view node ↔
 * screenshot pixel region directly (design §2 "Bounds").
 */
package com.oberkfell.viewspector.agent.payload

import android.graphics.Rect
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.view.View
import android.view.ViewGroup
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityNodeProvider
import com.oberkfell.viewspector.proto.ViewInspection
import java.lang.reflect.Method

object AccessibilityInspector {

    private const val TAG = "ViewSpector"

    // Loop / fan-out guards (design §1 "Loop / fan-out guards").
    private const val MAX_DEPTH = 250
    private const val MAX_NODES = 5000

    // AccessibilityNodeProvider.HOST_VIEW_ID (== -1): the virtualId of a real
    // (non-virtual) host node. Read reflectively to tolerate odd stub shapes.
    private const val HOST_VIEW_ID = -1

    // Cap on emitted extras per node, and skip very large stringified values
    // (Parcelables etc.) — design §2 "Extras".
    private const val MAX_EXTRAS = 64
    private const val MAX_EXTRA_VALUE_LEN = 512

    // Well-known extras key TalkBack reads for the spoken role (design §2).
    private const val ROLE_DESC_KEY = "AccessibilityNodeInfo.roleDescription"

    // Compose / framework traversal-group flag, read by literal key and tolerated
    // when absent (design §2 "Traversal ordering").
    private const val TRAVERSAL_GROUP_KEY = "android.view.accessibility.extra.IS_TRAVERSAL_GROUP"

    // @hide ViewGroup.getChildrenForAccessibility(): the a11y-ordered,
    // importantForAccessibility-filtered child Views used by the fallback path
    // (design §4). Resolved once; null when unavailable.
    private val getChildrenForA11y: Method? = try {
        ViewGroup::class.java.getDeclaredMethod("getChildrenForAccessibility")
            .also { it.isAccessible = true }
    } catch (t: Throwable) {
        null
    }

    /**
     * Build one [ViewInspection.DumpA11yResponse.Window] per root View, plus a
     * human-readable diagnostics string. MUST be called on the main thread.
     *
     * @param rootViews z-sorted window roots (RootsDetector.rootViews()).
     * @param strings shared interner; every CharSequence is .toString()-ed and
     *   interned (id 0 = absent).
     * @param includeExtras iterate getExtras() per node (design default true).
     * @param includeRenderingInfo refreshWithExtraData + getExtraRenderingInfo per
     *   node (costly extra round-trip; design default false).
     */
    fun dump(
        rootViews: List<View>,
        strings: StringTable,
        includeExtras: Boolean,
        includeRenderingInfo: Boolean,
    ): Pair<List<ViewInspection.DumpA11yResponse.Window>, String> {
        val diag = StringBuilder("roots=${rootViews.size}; api=${Build.VERSION.SDK_INT}")
        val windows = ArrayList<ViewInspection.DumpA11yResponse.Window>()
        // Shared emitted-node counter across all roots (belt-and-suspenders cap).
        val count = intArrayOf(0)

        for (root in rootViews) {
            // The host node on which app-process query mode was enabled; reset on
            // it (not the View) in finally (design §8). setQueryFromAppProcessEnabled
            // is an instance method on AccessibilityNodeInfo(View source, boolean).
            var enabledNode: AccessibilityNodeInfo? = null
            try {
                val node: ViewInspection.A11yNode? =
                    if (Build.VERSION.SDK_INT >= 34) {
                        try {
                            // Build the AT-visible host node, then flip it into
                            // app-process query mode so getChild()/getParent()
                            // resolve in-process (no AccessibilityService).
                            val hostNode = root.createAccessibilityNodeInfo()
                            if (hostNode != null) {
                                hostNode.setQueryFromAppProcessEnabled(root, true)
                                enabledNode = hostNode
                                diag.append("; root#${idOf(root)} query-from-app-process")
                                walk(
                                    hostNode, root, HOST_VIEW_ID, strings, 0, count,
                                    includeExtras, includeRenderingInfo,
                                )
                            } else {
                                diag.append("; root#${idOf(root)} null host node, fallback")
                                walkFallback(root, strings, 0, count, includeExtras)
                            }
                        } catch (t: Throwable) {
                            // Older OEM build / disabled path: fall back to the
                            // structural View walk (design §4).
                            Log.w(TAG, "query-from-app-process failed; falling back", t)
                            diag.append(
                                "; root#${idOf(root)} query-from-app-process failed " +
                                    "(${t.javaClass.simpleName}), fallback",
                            )
                            // Reset before the fallback re-reads the hierarchy.
                            enabledNode?.let {
                                try {
                                    it.setQueryFromAppProcessEnabled(root, false)
                                } catch (_: Throwable) {
                                }
                            }
                            enabledNode = null
                            walkFallback(root, strings, 0, count, includeExtras)
                        }
                    } else {
                        diag.append("; root#${idOf(root)} pre-34 fallback")
                        walkFallback(root, strings, 0, count, includeExtras)
                    }

                if (node != null) {
                    windows.add(window(root, node))
                }
            } catch (t: Throwable) {
                Log.w(TAG, "a11y dump failed for root", t)
                diag.append("; root#${idOf(root)} error ${t.javaClass.simpleName}")
            } finally {
                enabledNode?.let {
                    try {
                        it.setQueryFromAppProcessEnabled(root, false)
                    } catch (_: Throwable) {
                    }
                }
            }
        }

        diag.append("; nodes=${count[0]}")
        return windows to diag.toString()
    }

    private fun window(root: View, node: ViewInspection.A11yNode): ViewInspection.DumpA11yResponse.Window =
        ViewInspection.DumpA11yResponse.Window.newBuilder()
            .setRootViewId(idOf(root))
            .setRoot(node)
            .build()

    // ------------------------------------------------------------ unified walk

    /**
     * Primary recursion. [node] is the ANI for the element; [sourceView] is the
     * backing host View when known (real host node OR the provider host for any
     * virtual descendant), and [virtualId] is HOST_VIEW_ID for real host nodes or
     * a non-host sentinel for virtual descendants.
     *
     * getChild() dispatches through the provider for virtual nodes, so a single
     * loop enumerates BOTH real-view children and Compose semantics children
     * (design §1).
     */
    private fun walk(
        node: AccessibilityNodeInfo,
        sourceView: View?,
        virtualId: Int,
        strings: StringTable,
        depth: Int,
        count: IntArray,
        includeExtras: Boolean,
        includeRenderingInfo: Boolean,
    ): ViewInspection.A11yNode {
        val b = ViewInspection.A11yNode.newBuilder()
        if (sourceView != null) b.hostViewId = idOf(sourceView)
        b.virtualId = virtualId
        b.isVirtual = virtualId != HOST_VIEW_ID
        // Mark the provider host (AndroidComposeView / WebView) so the host can
        // badge Compose-origin subtrees (design §2 "provider_class").
        if (!b.isVirtual && sourceView != null) {
            try {
                sourceView.accessibilityNodeProvider?.let {
                    b.providerClass = strings.intern(it.javaClass.simpleName)
                }
            } catch (_: Throwable) {
            }
        }

        mapNode(node, sourceView, b, strings, includeExtras, includeRenderingInfo)

        if (depth < MAX_DEPTH && count[0] < MAX_NODES) {
            val n = safeInt { node.childCount }
            for (i in 0 until n) {
                if (count[0] >= MAX_NODES) break
                val child = try {
                    node.getChild(i)
                } catch (t: Throwable) {
                    // A throwing / transiently-detached child is skipped; traversal
                    // continues (design §1, mirrors TreeBuilder defensive style).
                    null
                } ?: continue
                count[0]++
                // Virtual children share the same backing host View as their
                // provider host; flag them virtual via a non-host sentinel.
                b.addChildren(
                    walk(
                        child, sourceView, virtualIdOf(node, i),
                        strings, depth + 1, count, includeExtras, includeRenderingInfo,
                    ),
                )
            }
        }
        return b.build()
    }

    // ------------------------------------------------------------ node mapping

    /**
     * Emit EVERY [ViewInspection.A11yNode] field from [node] (design §2). Each
     * getter is individually guarded; one failing field never aborts the node.
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
        sourceView: View?,
        b: ViewInspection.A11yNode.Builder,
        strings: StringTable,
        includeExtras: Boolean,
        includeRenderingInfo: Boolean,
    ) {
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
        // tri-state getChecked(): CHECKED_STATE_FALSE/PARTIAL/TRUE (API 34+).
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
        // API 34+; safe{} swallows NoSuchMethodError on older runtimes.
        b.a11YDataSensitive = bool { node.isAccessibilityDataSensitive }
        b.requestInitialFocus = bool { node.hasRequestInitialAccessibilityFocus() }

        // importantForAccessibility — preferred from the View (design §2); for
        // virtual nodes (no backing View) fall back to the ANI predicate.
        b.importantForAccessibility =
            if (sourceView != null && !b.isVirtual) {
                safeInt { sourceView.importantForAccessibility }
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
        @Suppress("DEPRECATION")
        b.actionsBitmask = safeInt { node.actions }

        // --- collections & ranges ---------------------------------------------
        safe { node.collectionInfo }?.let { ci ->
            val cb = ViewInspection.A11yCollectionInfo.newBuilder()
            cb.rowCount = safeInt { ci.rowCount }
            cb.columnCount = safeInt { ci.columnCount }
            cb.hierarchical = bool { ci.isHierarchical }
            cb.selectionMode = safeInt { ci.selectionMode }
            // getItemCount / getImportantForAccessibilityItemCount are API 34+.
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

        // --- extras (roleDescription, traversal group, compose testTag/id …) --
        val extras: Bundle? = safe { node.extras }
        if (extras != null) {
            // roleDescription: the spoken role TalkBack reads (design §2).
            safe { extras.getCharSequence(ROLE_DESC_KEY) }?.let {
                b.roleDescription = strings.intern(it.toString())
            }
            // isTraversalGroup: read by literal key, tolerate absence (design §2).
            b.isTraversalGroup = bool { extras.getBoolean(TRAVERSAL_GROUP_KEY, false) }

            if (includeExtras) {
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
                    // Skip large stringified Parcelables (design §2 "Extras").
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

        // --- traversal / label linkage ----------------------------------------
        // getTraversalBefore/After/getLabelFor/getLabeledBy resolve to a target
        // ANI only under query-from-app-process; when present we emit the target's
        // packed id (host_view_id<<32 | virtual_id). The host computes final
        // TalkBack order from these inputs (design §3).
        linkPackedId(node, "getTraversalBefore")?.let { b.traversalBefore = it }
        linkPackedId(node, "getTraversalAfter")?.let { b.traversalAfter = it }
        linkPackedId(node, "getLabelFor")?.let { b.labelFor = it }
        linkPackedId(node, "getLabeledBy")?.let { b.labeledBy = it }
        linkedByList(node).forEach { b.addLabeledByList(it) }

        // --- ExtraRenderingInfo (opt-in; extra refreshWithExtraData round-trip) -
        if (includeRenderingInfo) {
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

    // ------------------------------------------------------------ fallback walk

    /**
     * Structural View-walk fallback used when query-from-app-process is
     * unavailable (design §4). Still merges Compose by switching to the provider
     * path for any View whose getAccessibilityNodeProvider() is non-null — those
     * provider-built nodes carry child linkage locally.
     */
    private fun walkFallback(
        view: View,
        strings: StringTable,
        depth: Int,
        count: IntArray,
        includeExtras: Boolean,
    ): ViewInspection.A11yNode {
        val b = ViewInspection.A11yNode.newBuilder()
        b.hostViewId = idOf(view)
        b.virtualId = HOST_VIEW_ID
        b.isVirtual = false

        val node = try {
            view.createAccessibilityNodeInfo()
        } catch (t: Throwable) {
            null
        }
        val provider: AccessibilityNodeProvider? = try {
            view.accessibilityNodeProvider
        } catch (_: Throwable) {
            null
        }
        provider?.let { b.providerClass = strings.intern(it.javaClass.simpleName) }

        if (node != null) {
            mapNode(node, view, b, strings, includeExtras, false)
        }

        if (depth < MAX_DEPTH && count[0] < MAX_NODES) {
            if (provider != null && node != null) {
                // Provider host: its locally-built nodes carry child linkage even
                // without query-from-app-process. Enumerate virtual children via
                // the host node and recurse through the unified walk.
                val n = safeInt { node.childCount }
                for (i in 0 until n) {
                    if (count[0] >= MAX_NODES) break
                    val child = try {
                        node.getChild(i)
                    } catch (_: Throwable) {
                        null
                    } ?: continue
                    count[0]++
                    b.addChildren(
                        walk(
                            child, view, virtualIdOf(node, i),
                            strings, depth + 1, count, includeExtras, false,
                        ),
                    )
                }
            } else if (view is ViewGroup) {
                for (child in childrenForA11y(view)) {
                    if (count[0] >= MAX_NODES) break
                    count[0]++
                    b.addChildren(walkFallback(child, strings, depth + 1, count, includeExtras))
                }
            }
        }
        return b.build()
    }

    /**
     * The a11y-ordered, importantForAccessibility-filtered child Views (design
     * §4). Prefers the @hide ViewGroup.getChildrenForAccessibility(); falls back
     * to raw getChildAt order.
     */
    private fun childrenForA11y(vg: ViewGroup): List<View> {
        getChildrenForA11y?.let { m ->
            try {
                @Suppress("UNCHECKED_CAST")
                val list = m.invoke(vg) as? List<View>
                if (list != null) return list.filterNotNull()
            } catch (_: Throwable) {
            }
        }
        return (0 until vg.childCount).mapNotNull {
            try {
                vg.getChildAt(it)
            } catch (_: Throwable) {
                null
            }
        }
    }

    // ------------------------------------------------------------ id helpers

    private fun idOf(view: View): Long = ViewReflect.uniqueDrawingId(view)

    /**
     * A non-host virtual id sentinel for a child enumerated by index. The exact
     * provider virtual id is not exposed without @hide reflection; what matters
     * downstream is the is_virtual flag and the (host_view_id, is_virtual) pairing
     * back to the View/Compose node — so we encode the child's ordinal in a
     * negative, never-HOST_VIEW_ID space.
     */
    private fun virtualIdOf(parent: AccessibilityNodeInfo, index: Int): Int {
        // Try the @hide AccessibilityNodeInfo.getChildId(int) -> packed long, from
        // which the low 32 bits are the virtual descendant id. Best-effort.
        try {
            val m = AccessibilityNodeInfo::class.java
                .getDeclaredMethod("getChildId", Int::class.javaPrimitiveType)
            m.isAccessible = true
            val packed = m.invoke(parent, index) as? Long
            if (packed != null) {
                val vid = (packed and 0xffffffffL).toInt()
                if (vid != HOST_VIEW_ID) return vid
            }
        } catch (_: Throwable) {
        }
        // Fallback sentinel: a stable, non-host marker. -2 - index keeps it
        // negative and distinct from HOST_VIEW_ID (-1).
        return -2 - index
    }

    /**
     * Resolve a target-ANI-returning linkage getter ([getterName], e.g.
     * getTraversalBefore) to the target's packed id
     * (host_view_id<<32 | virtual_id) when resolvable, else 0. The target ANI's
     * source View id is not directly exposed, so we pack its window-relative
     * source-node id via the @hide getSourceNodeId(), falling back to 0.
     */
    private fun linkPackedId(node: AccessibilityNodeInfo, getterName: String): Long? {
        val target = try {
            val m = AccessibilityNodeInfo::class.java.getMethod(getterName)
            m.invoke(node) as? AccessibilityNodeInfo
        } catch (_: Throwable) {
            null
        } ?: return null
        val packed = sourceNodeId(target)
        return if (packed != 0L) packed else null
    }

    private fun linkedByList(node: AccessibilityNodeInfo): List<Long> {
        val out = ArrayList<Long>()
        try {
            // getLabeledByList() is API 34+; call reflectively to avoid a
            // NoSuchMethodError on older runtimes. The list may carry nulls.
            val m = AccessibilityNodeInfo::class.java.getMethod("getLabeledByList")
            val list = m.invoke(node) as? List<*>
            list?.forEach { t ->
                val target = t as? AccessibilityNodeInfo ?: return@forEach
                val packed = sourceNodeId(target)
                if (packed != 0L) out.add(packed)
            }
        } catch (_: Throwable) {
        }
        return out
    }

    /**
     * The @hide AccessibilityNodeInfo.getSourceNodeId():long — a packed
     * (accessibilityViewId<<32 | virtualDescendantId). Used only to express
     * traversal/label linkage targets; 0 when unavailable.
     */
    private fun sourceNodeId(node: AccessibilityNodeInfo): Long {
        return try {
            val m = AccessibilityNodeInfo::class.java.getDeclaredMethod("getSourceNodeId")
            m.isAccessible = true
            (m.invoke(node) as? Long) ?: 0L
        } catch (_: Throwable) {
            0L
        }
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
