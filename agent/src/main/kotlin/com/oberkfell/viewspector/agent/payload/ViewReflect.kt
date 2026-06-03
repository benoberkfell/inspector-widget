/*
 * ViewSpector — payload tree module.
 *
 * Reflective accessors for hidden ("@hide") android.view.View APIs that the
 * tree walk needs but which are not present in the public android.jar the
 * payload compiles against. On-device (API 36) these methods exist and are
 * reachable from the app classloader, so reflection resolves them at runtime.
 *
 * Every accessor is fully guarded and logged; failures degrade to a sentinel
 * (0 / null) rather than throwing, per the ViewSpector coding standards.
 */
package com.oberkfell.viewspector.agent.payload

import android.util.Log
import android.view.View
import java.lang.reflect.Method

/**
 * Cached reflective handles for hidden View methods.
 *
 * The real inspector accesses these as ordinary Kotlin properties because it
 * builds against a framework stub that exposes them (e.g.
 * proto/ViewExtensions.kt:72 `id = uniqueDrawingId`,
 * proto/ViewExtensions.kt:130 `view.sourceLayoutResId`). We resolve them
 * reflectively instead so this module compiles against the standard SDK.
 */
object ViewReflect {

    private const val TAG = "ViewSpector"

    // View.getUniqueDrawingId():long — @hide, API 29+. Stable per View instance.
    private val getUniqueDrawingId: Method? = resolve("getUniqueDrawingId")

    // View.getSourceLayoutResId():int — @hide. The layout file resource id that
    // inflated this view, or Resources.ID_NULL (0) if unknown.
    private val getSourceLayoutResId: Method? = resolve("getSourceLayoutResId")

    private fun resolve(name: String): Method? =
        try {
            View::class.java.getMethod(name).also { it.isAccessible = true }
        } catch (t: Throwable) {
            Log.w(TAG, "View.$name() not resolvable via reflection", t)
            null
        }

    /**
     * The view's stable uniqueDrawingId, or 0 if the hidden method is
     * unavailable / throws. 0 is treated downstream as "no id".
     */
    fun uniqueDrawingId(view: View): Long {
        val m = getUniqueDrawingId ?: return 0L
        return try {
            (m.invoke(view) as? Long) ?: 0L
        } catch (t: Throwable) {
            Log.w(TAG, "getUniqueDrawingId() invocation failed", t)
            0L
        }
    }

    /**
     * The resource id of the layout that inflated [view], or 0
     * (Resources.ID_NULL) if unknown / unavailable.
     */
    fun sourceLayoutResId(view: View): Int {
        val m = getSourceLayoutResId ?: return 0
        return try {
            (m.invoke(view) as? Int) ?: 0
        } catch (t: Throwable) {
            Log.w(TAG, "getSourceLayoutResId() invocation failed", t)
            0
        }
    }
}
