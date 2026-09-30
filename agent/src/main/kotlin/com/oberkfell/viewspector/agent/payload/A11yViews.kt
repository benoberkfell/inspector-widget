/*
 * ViewSpector — payload :: accessibility id -> View lookups shared by the focus reader and the
 * event tap (A11yFocus.kt, A11yEventTap.kt).
 *
 * Both start from a PACKED node id (A11yIds: low 32 bits = View.getAccessibilityViewId(), high
 * 32 bits = virtual id): an event's getSourceNodeId() or a node's getSourceNodeId(). The View
 * behind the low half is found the way ViewRootImpl finds an event's source
 * (getSourceForAccessibilityEvent: AccessibilityNodeIdManager.findView), with a walk of the
 * window as the fallback. Every hidden member is resolved once; a missing one degrades the
 * lookup (logged once), never the caller. Main thread only.
 */
package com.oberkfell.viewspector.agent.payload

import android.util.Log
import android.view.View
import android.view.ViewGroup
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityRecord
import java.lang.reflect.Method

object A11yViews {

    private const val TAG = "ViewSpector"
    private const val MAX_DEPTH = 400

    /** @hide View.getAccessibilityViewId() (@UnsupportedAppUsage). */
    private val getAccessibilityViewIdM: Method? = method(View::class.java, "getAccessibilityViewId")

    /** @hide AccessibilityRecord.getSourceNodeId() (@UnsupportedAppUsage); AccessibilityEvent inherits it. */
    private val recordSourceNodeIdM: Method? = method(AccessibilityRecord::class.java, "getSourceNodeId")

    /** @hide AccessibilityNodeInfo.getSourceNodeId() (@UnsupportedAppUsage @TestApi). */
    private val nodeSourceNodeIdM: Method? = method(AccessibilityNodeInfo::class.java, "getSourceNodeId")

    /** @hide AccessibilityNodeIdManager: the framework's accessibility-id -> View registry. */
    private val idManager: Pair<Any, Method>? = try {
        val cls = Class.forName("android.view.accessibility.AccessibilityNodeIdManager")
        val inst = cls.getMethod("getInstance").invoke(null)
        val find = cls.getMethod("findView", Int::class.javaPrimitiveType)
        if (inst != null) inst to find else null
    } catch (t: Throwable) {
        Log.i(TAG, "AccessibilityNodeIdManager not reachable; accessibility ids resolve by walking the window", t)
        null
    }

    /** The packed source id of an event, or null when unreachable. */
    fun sourceNodeId(record: AccessibilityRecord): Long? = invokeLong(recordSourceNodeIdM, record)

    /** The packed source id of a node, or null when unreachable. */
    fun sourceNodeId(node: AccessibilityNodeInfo): Long? = invokeLong(nodeSourceNodeIdM, node)

    /** View.getAccessibilityViewId(), or null when unreachable. */
    fun accessibilityViewId(view: View): Int? = try {
        getAccessibilityViewIdM?.invoke(view) as? Int
    } catch (t: Throwable) {
        null
    }

    /**
     * The View under [root] whose accessibility view id is [aid], or null. ROOT_ITEM_ID (shared
     * by every window root on API 37) is [root] itself.
     */
    fun viewFor(root: View, aid: Int): View? {
        if (aid == A11yIds.UNDEFINED_ITEM_ID) return null
        if (aid == A11yIds.ROOT_ITEM_ID) return root
        idManager?.let { (inst, find) ->
            try {
                (find.invoke(inst, aid) as? View)?.let { return it }
            } catch (_: Throwable) {
            }
        }
        return search(root, aid, 0)
    }

    private fun search(view: View, aid: Int, depth: Int): View? {
        if (depth > MAX_DEPTH) return null
        if (accessibilityViewId(view) == aid) return view
        if (view is ViewGroup) {
            for (i in 0 until view.childCount) {
                val child = view.getChildAt(i) ?: continue
                search(child, aid, depth + 1)?.let { return it }
            }
        }
        return null
    }

    private fun invokeLong(m: Method?, target: Any): Long? = try {
        m?.invoke(target) as? Long
    } catch (t: Throwable) {
        null
    }

    private fun method(cls: Class<*>, name: String): Method? = try {
        (
            try {
                cls.getDeclaredMethod(name)
            } catch (_: NoSuchMethodException) {
                cls.getMethod(name)
            }
            ).also { it.isAccessible = true }
    } catch (t: Throwable) {
        Log.w(TAG, "${cls.simpleName}.$name not reachable via reflection", t)
        null
    }
}
