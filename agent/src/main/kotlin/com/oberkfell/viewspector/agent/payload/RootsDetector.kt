/*
 * ViewSpector — payload tree module.
 *
 * Roots detection. Models on the real dynamic-layout-inspector RootsDetector:
 *   android-sources/tools-base/dynamic-layout-inspector/agent/appinspection/src/main/
 *     com/android/tools/agent/appinspection/RootsDetector.kt:131-152
 * but stripped of XR support and the background poll thread — ViewSpector is a
 * synchronous request/response inspector, so we enumerate roots on demand.
 */
package com.oberkfell.viewspector.agent.payload

import android.os.Build
import android.util.Log
import android.view.View
import android.view.inspector.WindowInspector

/**
 * Detects the current set of window root views.
 *
 * Every public method here touches live [View] state (visibility, attach state,
 * Z-order, ids) and MUST therefore be called on the main (UI) Looper thread.
 * Callers use [MainThread.run] to hop on-thread before invoking these.
 *
 * Mirrors RootsDetector.getRootViews (RootsDetector.kt:131-152): collect global
 * window views, keep only those that are VISIBLE and attached, then sort by Z so
 * the bottom-most window (Activity content) is first and the top-most
 * (dialog/popup/toast/IME) is last. See build guide line 417 for why the
 * z-sorted order is load-bearing for the host's window compositing.
 */
object RootsDetector {

    private const val TAG = "ViewSpector"

    /**
     * The current root views, filtered to VISIBLE + attached and sorted by Z
     * (ascending). MUST be called on the main thread.
     *
     * [WindowInspector.getGlobalWindowViews] is API 29+ (the emulator target is
     * API 36); below that there is no clean-room way to enumerate global roots,
     * so we return an empty list rather than reflect into private framework
     * internals.
     */
    fun rootViews(): List<View> {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            Log.w(TAG, "RootsDetector requires API 29+, current=${Build.VERSION.SDK_INT}")
            return emptyList()
        }
        val views: List<View> =
            try {
                // RootsDetector.kt:150 — WindowInspector.getGlobalWindowViews().
                WindowInspector.getGlobalWindowViews()
            } catch (t: Throwable) {
                Log.w(TAG, "WindowInspector.getGlobalWindowViews() failed", t)
                return emptyList()
            }

        return views
            .filter { view ->
                try {
                    // RootsDetector.kt:145 — visibility == VISIBLE && isAttachedToWindow.
                    view.visibility == View.VISIBLE && view.isAttachedToWindow
                } catch (t: Throwable) {
                    Log.w(TAG, "Failed to inspect root view visibility/attach state", t)
                    false
                }
            }
            // RootsDetector.kt:145 — .sortedBy { it.view.z }. View.getZ() is API 21+.
            .sortedBy { view ->
                try {
                    view.z
                } catch (t: Throwable) {
                    Log.w(TAG, "View.getZ() failed; defaulting to 0", t)
                    0f
                }
            }
    }

    /**
     * The uniqueDrawingId of each current root view, in the same z-sorted order
     * as [rootViews]. MUST be called on the main thread.
     *
     * uniqueDrawingId is a stable per-instance Long (View.getUniqueDrawingId(),
     * API 29+, @hide so accessed reflectively — see [ViewReflect.uniqueDrawingId]).
     */
    fun rootIds(): List<Long> = rootViews().map { ViewReflect.uniqueDrawingId(it) }
}
