/*
 * ViewSpector — payload core module.
 *
 * Locate a single View by its View.getUniqueDrawingId() by walking each root's
 * hierarchy. Used by the GET_PROPERTIES path in [Dispatcher] to resolve a
 * client-supplied view id to a live View before extracting its properties.
 *
 * uniqueDrawingId is the same stable per-instance Long that [TreeBuilder] writes
 * into ViewNode.id (proto/ViewExtensions.kt:72 in the reference inspector). We
 * read it via [ViewReflect.uniqueDrawingId] since the public android.jar does
 * not expose the @hide getter at compile time.
 *
 * MUST be called on the main thread: it touches live ViewGroup children.
 */
package com.oberkfell.viewspector.agent.payload

import android.view.View
import android.view.ViewGroup

/**
 * Depth-first search over [rootViews] (and all descendants) for the View whose
 * uniqueDrawingId equals [uniqueDrawingId]. Returns the first match, or null if
 * no attached view in any root carries that id.
 *
 * A [uniqueDrawingId] of 0 never matches (0 is the "no id" sentinel), so this
 * returns null for it without walking.
 */
fun findViewById(rootViews: List<View>, uniqueDrawingId: Long): View? {
    if (uniqueDrawingId == 0L) return null
    for (root in rootViews) {
        val found = searchView(root, uniqueDrawingId)
        if (found != null) return found
    }
    return null
}

/**
 * Recursively searches [view] and its descendants. Iterative-friendly recursion:
 * view trees are shallow enough that the JVM stack is never at risk, and this
 * mirrors the reference inspector's recursive tree walks.
 */
private fun searchView(view: View, uniqueDrawingId: Long): View? {
    if (ViewReflect.uniqueDrawingId(view) == uniqueDrawingId) {
        return view
    }
    if (view is ViewGroup) {
        // Snapshot the child count once; addView/removeView must not happen
        // concurrently because callers hold the main thread.
        val count = view.childCount
        for (i in 0 until count) {
            val child = view.getChildAt(i) ?: continue
            val found = searchView(child, uniqueDrawingId)
            if (found != null) return found
        }
    }
    return null
}
