/*
 * ViewSpector — payload tree module.
 *
 * Walks window root view trees and builds proto [ViewInspection.ViewNode]s.
 *
 * Models the node-building logic on the real inspector's
 *   android-sources/tools-base/dynamic-layout-inspector/agent/appinspection/src/main/
 *     com/android/tools/agent/appinspection/proto/ViewExtensions.kt:54-170
 * (toNode / toNodeImpl / createResource / isValidResourceId), adapted to the
 * ViewSpector proto and StringTable, and using only public View APIs plus the
 * reflective hidden-API shims in [ViewReflect].
 *
 * All tree traversal touches live View state and MUST run on the main thread
 * (callers hop via MainThread.run before invoking [buildRoots]).
 */
package com.oberkfell.viewspector.agent.payload

import android.content.res.Resources
import android.graphics.Matrix
import android.util.Log
import android.view.View
import android.view.ViewGroup
import android.webkit.WebView
import android.widget.TextView
import com.oberkfell.viewspector.proto.ViewInspection
import kotlin.math.roundToInt

/**
 * Builds [ViewInspection.ViewNode] trees for window roots.
 *
 * A [TreeBuilder] is single-use per dump: construct it, call [buildRoots], then
 * read [visited] so the [Dispatcher] can fetch properties for every node it
 * emitted. All strings are interned into the shared [strings] table.
 */
class TreeBuilder(val strings: StringTable) {

    private companion object {
        const val TAG = "ViewSpector"
    }

    // Every View visited during the most recent buildRoots(), in pre-order.
    // Keyed accessor lets the Dispatcher resolve a node id back to its View for
    // property extraction without re-walking the tree.
    private val visitedViews: MutableList<View> = ArrayList()
    private val viewsById: MutableMap<Long, View> = LinkedHashMap()

    /** All Views visited during the last [buildRoots] call, in pre-order. */
    val visited: List<View> get() = visitedViews

    /** Look up a visited View by its uniqueDrawingId, or null if not present. */
    fun viewFor(id: Long): View? = viewsById[id]

    /**
     * Build view-node trees for the requested root.
     *
     * @param rootId a window root's uniqueDrawingId, or 0 to build every root.
     * @return one [ViewInspection.ViewNode] per matching root, in z-sorted order.
     *
     * MUST be called on the main thread.
     */
    fun buildRoots(rootId: Long): List<ViewInspection.ViewNode> {
        visitedViews.clear()
        viewsById.clear()

        val roots = RootsDetector.rootViews()
        val selected =
            if (rootId == 0L) {
                roots
            } else {
                roots.filter { ViewReflect.uniqueDrawingId(it) == rootId }
            }

        return selected.mapNotNull { root ->
            try {
                buildNode(root)
            } catch (t: Throwable) {
                Log.w(TAG, "Failed to build node tree for root", t)
                null
            }
        }
    }

    /** Recursively convert [view] (and any children) into a ViewNode. */
    private fun buildNode(view: View): ViewInspection.ViewNode {
        visitedViews.add(view)
        val id = ViewReflect.uniqueDrawingId(view)
        if (id != 0L) {
            viewsById[id] = view
        }

        val builder = ViewInspection.ViewNode.newBuilder()
        builder.id = id

        // class_name / package_name (ViewExtensions.kt:75-76).
        val viewClass = view.javaClass
        builder.className = strings.intern(viewClass.simpleName)
        try {
            viewClass.`package`?.name?.let { pkg -> builder.packageName = strings.intern(pkg) }
        } catch (t: Throwable) {
            Log.w(TAG, "Failed to read package name for ${viewClass.name}", t)
        }

        // bounds (ViewExtensions.kt:78-128).
        builder.bounds = buildBounds(view)

        // resource — the view's own @id (ViewExtensions.kt:74, createResource:144).
        createResource(view, safeViewId(view))?.let { res ->
            builder.resource = res
            // view_id_name = the R.id entry name (e.g. "my_button").
            entryNameOrNull(view, safeViewId(view))?.let { entry ->
                builder.viewIdName = strings.intern(entry)
            }
        }

        // layout_resource — the layout file that inflated this view, if known
        // (ViewExtensions.kt:130; sourceLayoutResId is @hide -> reflective).
        createResource(view, ViewReflect.sourceLayoutResId(view))?.let { res ->
            builder.layoutResource = res
        }

        // flags — IS_WEBVIEW (ViewExtensions.kt:132-134).
        if (isWebView(view)) {
            builder.flags = builder.flags or ViewInspection.ViewNode.Flag.IS_WEBVIEW_VALUE
        }

        // text_value — best-effort for TextViews (ViewExtensions.kt:136,
        // framework/ViewExtensions.kt:37-40 getTextValue).
        textValueOrNull(view)?.let { text -> builder.textValue = strings.intern(text) }

        // children (ViewExtensions.kt:137-139).
        if (view is ViewGroup) {
            val childCount =
                try {
                    view.childCount
                } catch (t: Throwable) {
                    Log.w(TAG, "ViewGroup.getChildCount() failed", t)
                    0
                }
            for (i in 0 until childCount) {
                val child =
                    try {
                        view.getChildAt(i)
                    } catch (t: Throwable) {
                        Log.w(TAG, "ViewGroup.getChildAt($i) failed", t)
                        null
                    } ?: continue
                builder.addChildren(buildNode(child))
            }
        }

        return builder.build()
    }

    /**
     * Absolute on-screen bounds for [view]: an axis-aligned [ViewInspection.Rect]
     * from getLocationOnScreen()+width/height, plus a [ViewInspection.Quad] render
     * shape only when the view carries a non-identity transform (rotation / scale
     * / skew). Mirrors ViewExtensions.kt:78-128.
     */
    private fun buildBounds(view: View): ViewInspection.Bounds {
        val bounds = ViewInspection.Bounds.newBuilder()

        val location = IntArray(2)
        try {
            view.getLocationOnScreen(location)
        } catch (t: Throwable) {
            Log.w(TAG, "View.getLocationOnScreen() failed", t)
            location[0] = 0
            location[1] = 0
        }
        val x = location[0]
        val y = location[1]
        val w =
            try {
                view.width
            } catch (t: Throwable) {
                0
            }
        val h =
            try {
                view.height
            } catch (t: Throwable) {
                0
            }

        bounds.layout =
            ViewInspection.Rect.newBuilder()
                .setX(x)
                .setY(y)
                .setW(w)
                .setH(h)
                .build()

        // Render quad: only when the view's own transform matrix is non-identity.
        // ViewExtensions.kt:91-126 uses transformMatrixToGlobal (hidden); per the
        // module spec we use the public getMatrix() local transform instead and
        // map the four local corners, then offset into absolute screen space by
        // the (untransformed) location. This is a best-effort visual indicator of
        // rotation/scale/skew, matching the reference's intent.
        try {
            val matrix: Matrix? = view.matrix
            if (matrix != null && !matrix.isIdentity && w > 0 && h > 0) {
                val corners =
                    floatArrayOf(
                        0f, 0f,
                        w.toFloat(), 0f,
                        w.toFloat(), h.toFloat(),
                        0f, h.toFloat(),
                    )
                matrix.mapPoints(corners)
                if (corners.none { it.isNaN() }) {
                    bounds.render =
                        ViewInspection.Quad.newBuilder()
                            .setX0((corners[0] + x).roundToInt())
                            .setY0((corners[1] + y).roundToInt())
                            .setX1((corners[2] + x).roundToInt())
                            .setY1((corners[3] + y).roundToInt())
                            .setX2((corners[4] + x).roundToInt())
                            .setY2((corners[5] + y).roundToInt())
                            .setX3((corners[6] + x).roundToInt())
                            .setY3((corners[7] + y).roundToInt())
                            .build()
                }
            }
        } catch (t: Throwable) {
            Log.w(TAG, "Failed to compute render quad", t)
        }

        return bounds.build()
    }

    /**
     * Build a [ViewInspection.Resource] for [resourceId] using [view]'s
     * Resources, or null if the id is invalid / not found. Mirrors
     * ViewExtensions.kt:144-170 (createResource + isValidResourceId).
     */
    private fun createResource(view: View, resourceId: Int): ViewInspection.Resource? {
        if (!isValidResourceId(resourceId)) return null
        val resources =
            try {
                view.resources
            } catch (t: Throwable) {
                Log.w(TAG, "View.getResources() failed", t)
                return null
            } ?: return null

        return try {
            ViewInspection.Resource.newBuilder()
                .setType(strings.intern(resources.getResourceTypeName(resourceId)))
                .setNamespace(strings.intern(resources.getResourcePackageName(resourceId)))
                .setName(strings.intern(resources.getResourceEntryName(resourceId)))
                .build()
        } catch (ex: Resources.NotFoundException) {
            null
        } catch (t: Throwable) {
            Log.w(TAG, "Failed to resolve resource 0x${resourceId.toString(16)}", t)
            null
        }
    }

    /** The R.id entry name for [resourceId] (e.g. "my_button"), or null. */
    private fun entryNameOrNull(view: View, resourceId: Int): String? {
        if (!isValidResourceId(resourceId)) return null
        return try {
            view.resources?.getResourceEntryName(resourceId)
        } catch (t: Throwable) {
            null
        }
    }

    private fun safeViewId(view: View): Int =
        try {
            view.id
        } catch (t: Throwable) {
            Resources.ID_NULL
        }

    /**
     * Whether [resourceId] is a usable, dynamic resource id. Mirrors
     * ViewExtensions.kt:160-170 (is_valid_resid in frameworks ResourceUtils.h):
     * non-null, package id present and not 0xff, type id present.
     */
    private fun isValidResourceId(resourceId: Int): Boolean {
        if (resourceId == Resources.ID_NULL) return false
        // Resources.ID_PACKAGE_MASK == 0xff000000, ID_TYPE_MASK == 0x00ff0000.
        // These constants are @hide, so use the literals (ViewExtensions.kt:167-169).
        val packageMask = 0xff000000.toInt()
        val typeMask = 0x00ff0000
        return (resourceId and packageMask) != 0 &&
            (resourceId and packageMask) != packageMask &&
            (resourceId and typeMask) != 0
    }

    /** True when [view] is (or subclasses) android.webkit.WebView. */
    private fun isWebView(view: View): Boolean =
        try {
            WebView::class.java.isAssignableFrom(view.javaClass)
        } catch (t: Throwable) {
            false
        }

    /**
     * Best-effort text for text-bearing views (framework/ViewExtensions.kt:37-40):
     * TextView.text.toString(), or null otherwise. Guarded because subclasses can
     * throw from getText().
     */
    private fun textValueOrNull(view: View): String? {
        if (view !is TextView) return null
        return try {
            view.text?.toString()
        } catch (t: Throwable) {
            Log.w(TAG, "TextView.getText() failed", t)
            null
        }
    }
}
