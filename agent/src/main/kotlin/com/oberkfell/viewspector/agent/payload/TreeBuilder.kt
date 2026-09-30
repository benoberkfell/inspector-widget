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
import java.lang.reflect.Method
import kotlin.math.abs
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

        /**
         * @hide View.transformMatrixToGlobal(Matrix): the view-local -> screen matrix, with
         * every ancestor's transform and scroll and the window position (the reference's
         * ViewExtensions.kt:91-126 uses it for the render bounds). Reachable because the
         * native agent disables hidden-API enforcement; null falls back to the view's own
         * matrix around its untransformed origin.
         */
        val transformMatrixToGlobal: Method? =
            try {
                View::class.java.getMethod("transformMatrixToGlobal", Matrix::class.java)
            } catch (t: Throwable) {
                Log.i(TAG, "View.transformMatrixToGlobal unavailable; render quads use the own matrix only")
                null
            }

        private const val EPS = 1e-4f

        /** True when [m] only translates: the axis-aligned layout rect is then exact. */
        fun isTranslateOnly(m: Matrix): Boolean {
            val v = FloatArray(9)
            m.getValues(v)
            return abs(v[Matrix.MSCALE_X] - 1f) < EPS && abs(v[Matrix.MSCALE_Y] - 1f) < EPS &&
                abs(v[Matrix.MSKEW_X]) < EPS && abs(v[Matrix.MSKEW_Y]) < EPS &&
                abs(v[Matrix.MPERSP_0]) < EPS && abs(v[Matrix.MPERSP_1]) < EPS &&
                abs(v[Matrix.MPERSP_2] - 1f) < EPS
        }
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
     * Nodes of the last [buildRoots] at the depth cap ([WireLimits.MAX_TREE_DEPTH]) whose
     * children were not sent (each is flagged CHILDREN_TRUNCATED).
     */
    var truncatedNodes: Int = 0
        private set

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
        truncatedNodes = 0

        val roots = RootsDetector.rootViews()
        val selected =
            if (rootId == 0L) {
                roots
            } else {
                roots.filter { ViewReflect.uniqueDrawingId(it) == rootId }
            }

        return selected.mapNotNull { root ->
            try {
                buildNode(root, 1, ancestorTransformed = false)
            } catch (t: Throwable) {
                Log.w(TAG, "Failed to build node tree for root", t)
                null
            }
        }
    }

    /**
     * Recursively convert [view] (and any children) into a ViewNode. [depth] is 1 for a
     * window root; a node at [WireLimits.MAX_TREE_DEPTH] keeps its fields but not its
     * children (flagged CHILDREN_TRUNCATED): a deeper tree would make the whole response
     * unparseable by the host's protobuf runtime.
     */
    private fun buildNode(view: View, depth: Int, ancestorTransformed: Boolean): ViewInspection.ViewNode {
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
        // Whether this view or any ancestor has a non-identity transform: only then can
        // the view need a render quad (ancestor rotations rotate it too).
        val transformed = ancestorTransformed || !hasIdentityMatrix(view)
        builder.bounds = buildBounds(view, transformed)

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
        // framework/ViewExtensions.kt:37-40 getTextValue). A password field's text is
        // masked (Redaction.kt) and flagged TEXT_REDACTED.
        textValueOrNull(view)?.let { (text, redacted) ->
            builder.textValue = strings.intern(text)
            if (redacted) {
                builder.flags = builder.flags or ViewInspection.ViewNode.Flag.TEXT_REDACTED_VALUE
            }
        }

        // children (ViewExtensions.kt:137-139).
        if (view is ViewGroup) {
            val childCount =
                try {
                    view.childCount
                } catch (t: Throwable) {
                    Log.w(TAG, "ViewGroup.getChildCount() failed", t)
                    0
                }
            if (childCount > 0 && depth >= WireLimits.MAX_TREE_DEPTH) {
                builder.flags = builder.flags or ViewInspection.ViewNode.Flag.CHILDREN_TRUNCATED_VALUE
                truncatedNodes++
                return builder.build()
            }
            for (i in 0 until childCount) {
                val child =
                    try {
                        view.getChildAt(i)
                    } catch (t: Throwable) {
                        Log.w(TAG, "ViewGroup.getChildAt($i) failed", t)
                        null
                    } ?: continue
                builder.addChildren(buildNode(child, depth + 1, transformed))
            }
        }

        return builder.build()
    }

    /**
     * Absolute on-screen bounds for [view]: an axis-aligned [ViewInspection.Rect]
     * from getLocationOnScreen()+width/height, plus a [ViewInspection.Quad] render
     * shape when a rotation / scale / skew applies to the view (its own transform
     * or an ancestor's; [transformed] says whether any exists). Mirrors
     * ViewExtensions.kt:78-128.
     */
    private fun buildBounds(view: View, transformed: Boolean): ViewInspection.Bounds {
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

        if (transformed && w > 0 && h > 0) {
            try {
                renderQuad(view, x, y, w, h)?.let { bounds.render = it }
            } catch (t: Throwable) {
                Log.w(TAG, "Failed to compute render quad", t)
            }
        }

        return bounds.build()
    }

    /**
     * The view's four corners on screen, in drawing order (top-left, top-right,
     * bottom-right, bottom-left), or null when the transform is a pure translation
     * (the layout rect is then exact).
     *
     * getLocationOnScreen() already includes every transform (the view's own too), so
     * the corners must not be mapped through the view matrix and then offset by that
     * location again: that applied the view's transform twice. Preferred: map the
     * view-local corners through transformMatrixToGlobal (all ancestors included).
     * Fallback: the view's own matrix around its untransformed origin, which is the
     * location minus where the matrix moves local (0,0); exact unless an ancestor is
     * transformed too.
     */
    private fun renderQuad(view: View, x: Int, y: Int, w: Int, h: Int): ViewInspection.Quad? {
        val corners =
            floatArrayOf(
                0f, 0f,
                w.toFloat(), 0f,
                w.toFloat(), h.toFloat(),
                0f, h.toFloat(),
            )
        val global = globalMatrix(view)
        if (global != null) {
            if (isTranslateOnly(global)) return null
            global.mapPoints(corners)
        } else {
            val own: Matrix = view.matrix ?: return null
            if (isTranslateOnly(own)) return null
            val origin = floatArrayOf(0f, 0f)
            own.mapPoints(origin)
            val ox = x - origin[0]
            val oy = y - origin[1]
            own.mapPoints(corners)
            for (i in corners.indices step 2) {
                corners[i] += ox
                corners[i + 1] += oy
            }
        }
        if (corners.any { it.isNaN() || it.isInfinite() }) return null
        return ViewInspection.Quad.newBuilder()
            .setX0(corners[0].roundToInt())
            .setY0(corners[1].roundToInt())
            .setX1(corners[2].roundToInt())
            .setY1(corners[3].roundToInt())
            .setX2(corners[4].roundToInt())
            .setY2(corners[5].roundToInt())
            .setX3(corners[6].roundToInt())
            .setY3(corners[7].roundToInt())
            .build()
    }

    /** View-local -> screen matrix via the hidden transformMatrixToGlobal, or null. */
    private fun globalMatrix(view: View): Matrix? {
        val m = transformMatrixToGlobal ?: return null
        return try {
            Matrix().also { m.invoke(view, it) }
        } catch (t: Throwable) {
            Log.w(TAG, "View.transformMatrixToGlobal failed", t)
            null
        }
    }

    /** View.getMatrix().isIdentity, guarded (hasIdentityMatrix itself is hidden). */
    private fun hasIdentityMatrix(view: View): Boolean =
        try {
            view.matrix?.isIdentity ?: true
        } catch (t: Throwable) {
            true
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
     * TextView.text.toString() paired with false, or null for other views. Guarded
     * because subclasses can throw from getText(). getText() of a password field is
     * the plaintext (the dots are only a TransformationMethod), so for one
     * ([Redaction.isPasswordView]) the text is masked and paired with true.
     */
    private fun textValueOrNull(view: View): Pair<String, Boolean>? {
        if (view !is TextView) return null
        val text: CharSequence =
            try {
                view.text
            } catch (t: Throwable) {
                Log.w(TAG, "TextView.getText() failed", t)
                null
            } ?: return null
        if (text.isEmpty()) return "" to false
        return if (Redaction.isPasswordView(view)) {
            Redaction.mask(text) to true
        } else {
            text.toString() to false
        }
    }
}
