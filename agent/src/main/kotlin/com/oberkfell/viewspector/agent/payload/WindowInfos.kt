/*
 * ViewSpector — payload :: window metadata (ViewInspection.WindowInfo) for one window root.
 *
 * What an accessibility service knows about a window, read from the app side:
 *   title        LayoutParams.accessibilityTitle (@hide field). AOSP WindowState.getWindowInfo
 *                reports it as AccessibilityWindowInfo.getTitle(); PhoneWindow.setTitle (from
 *                Activity.setTitle / onTitleChanged and Dialog.setTitle) keeps it in sync.
 *                TalkBack speaks it on a window change and skips a first stop that repeats it.
 *   layout_title LayoutParams.getTitle(), the window manager's tag.
 *   type / flags LayoutParams.type / flags (which windows are modal: CONTRACT.md §9).
 *   frame        the root's position on screen and size (a root fills its window).
 *   z            the root's index in RootsDetector.rootViews() (bottom first).
 *   insets       getRootWindowInsets(): status bars, navigation bars, IME, display cutout
 *                (WindowInsets.getInsets / isVisible, API 30+; absent below).
 * Main thread; every read is guarded.
 */
package com.oberkfell.viewspector.agent.payload

import android.os.Build
import android.util.Log
import android.view.View
import android.view.WindowInsets
import android.view.WindowManager
import com.oberkfell.viewspector.proto.ViewInspection
import java.lang.reflect.Field

object WindowInfos {

    private const val TAG = "ViewSpector"

    /** @hide WindowManager.LayoutParams.accessibilityTitle. */
    private val accessibilityTitleF: Field? = try {
        WindowManager.LayoutParams::class.java.getDeclaredField("accessibilityTitle").also { it.isAccessible = true }
    } catch (t: Throwable) {
        Log.w(TAG, "WindowManager.LayoutParams.accessibilityTitle not reachable; window titles omitted", t)
        null
    }

    /** The WindowInfo of [root], at stacking position [z]. Main thread. */
    fun of(root: View, z: Int, strings: StringTable): ViewInspection.WindowInfo {
        val b = ViewInspection.WindowInfo.newBuilder()
            .setRootViewId(ViewReflect.uniqueDrawingId(root))
            .setZ(z)
        val lp = try {
            root.layoutParams as? WindowManager.LayoutParams
        } catch (_: Throwable) {
            null
        }
        if (lp != null) {
            b.windowType = lp.type
            b.wmFlags = lp.flags
            b.layoutTitle = strings.intern(safe { lp.title?.toString() })
            b.title = strings.intern(safe { (accessibilityTitleF?.get(lp) as? CharSequence)?.toString() })
        }
        try {
            val loc = IntArray(2)
            root.getLocationOnScreen(loc)
            b.frame = ViewInspection.Bounds.newBuilder()
                .setLayout(
                    ViewInspection.Rect.newBuilder()
                        .setX(loc[0]).setY(loc[1]).setW(root.width).setH(root.height),
                )
                .build()
        } catch (t: Throwable) {
            Log.w(TAG, "window frame unavailable", t)
        }
        b.hasWindowFocus = safe { root.hasWindowFocus() } ?: false
        b.displayId = safe { root.display?.displayId } ?: 0
        if (Build.VERSION.SDK_INT >= 30) {
            safe { root.rootWindowInsets }?.let { wi ->
                inset(wi, WindowInsets.Type.statusBars())?.let { b.statusBars = it }
                inset(wi, WindowInsets.Type.navigationBars())?.let { b.navigationBars = it }
                inset(wi, WindowInsets.Type.ime())?.let { b.ime = it }
                inset(wi, WindowInsets.Type.displayCutout())?.let { b.displayCutout = it }
            }
        }
        return b.build()
    }

    /** The stacking position of [root] among the current roots (bottom = 0), or -1. Main thread. */
    fun zOf(root: View, allRoots: List<View>): Int = allRoots.indexOf(root)

    private fun inset(wi: WindowInsets, type: Int): ViewInspection.WindowInsetsInfo? {
        if (Build.VERSION.SDK_INT < 30) return null
        return try {
            val i = wi.getInsets(type)
            ViewInspection.WindowInsetsInfo.newBuilder()
                .setLeft(i.left).setTop(i.top).setRight(i.right).setBottom(i.bottom)
                .setVisible(wi.isVisible(type))
                .build()
        } catch (t: Throwable) {
            null
        }
    }

    private inline fun <T> safe(block: () -> T): T? = try {
        block()
    } catch (_: Throwable) {
        null
    }
}
