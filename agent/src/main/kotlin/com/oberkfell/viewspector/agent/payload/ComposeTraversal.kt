/*
 * ViewSpector — payload :: Compose accessibility traversal order without a running service.
 *
 * Compose publishes its reading order as traversalBefore/traversalAfter on the nodes it serves
 * (AndroidComposeViewAccessibilityDelegateCompat reads idToBeforeMap / idToAfterMap while it
 * populates each AccessibilityNodeInfo). It fills those maps (setTraversalValues, the geometry
 * grouping sort that honours isTraversalGroup and traversalIndex) only while its isEnabled is
 * true, i.e. while an accessibility service is running. Without TalkBack the dump would carry no
 * ordering at all, and a Scaffold whose content is composed before its top bar would read the
 * top bar last.
 *
 * [prime] runs Compose's own setTraversalValues on each AndroidComposeView before the walk, so
 * the dumped linkage is the order TalkBack would get. It touches only the delegate's two
 * traversal maps, and [restore] clears them again afterwards when they started out empty (the
 * state of a delegate that has never had a service). When a service is on, Compose keeps the
 * maps itself and nothing is done.
 *
 * Bytecode layouts (checked with javap):
 *   ui 1.7.0 / 1.8.2: private instance setTraversalValues().
 *   ui 1.12.1:        static AndroidComposeViewAccessibilityDelegateCompat_androidKt
 *                     .setTraversalValues(IntObjectMap currentSemanticsNodes,
 *                     MutableIntIntMap idToBeforeMap, MutableIntIntMap idToAfterMap, Resources),
 *                     fed from the private getCurrentSemanticsNodes().
 *   isEnabled is `isEnabled$ui_release` (1.7) or `isEnabled$ui` (1.12).
 * Every step is reflective and optional: a miss leaves the dump as it was and is reported.
 */
package com.oberkfell.viewspector.agent.payload

import android.util.Log
import android.view.View
import android.view.ViewGroup
import java.lang.reflect.Field
import java.lang.reflect.Method

internal object ComposeTraversal {

    private const val TAG = "ViewSpector"
    private const val VIEW_MAX_DEPTH = 400
    private const val DELEGATE_CLASS =
        "androidx.compose.ui.platform.AndroidComposeViewAccessibilityDelegateCompat"
    private const val DELEGATE_KT_CLASS =
        "androidx.compose.ui.platform.AndroidComposeViewAccessibilityDelegateCompat_androidKt"

    /** What [prime] did for one window root. */
    class Result {
        /** AndroidComposeViews whose delegate already computes the order (a service is on). */
        var byService = 0

        /** AndroidComposeViews whose order this dump computed. */
        var computed = 0

        /** AndroidComposeViews where the computation was not reachable. */
        var failed = 0

        /** Maps to clear after the walk (they were empty before [prime] filled them). */
        val toClear = ArrayList<Any>()

        fun token(): String? {
            if (byService + computed + failed == 0) return null
            val parts = ArrayList<String>()
            if (computed > 0) parts.add("computed=$computed")
            if (byService > 0) parts.add("service=$byService")
            if (failed > 0) parts.add("unavailable=$failed")
            return "compose-traversal " + parts.joinToString(",")
        }
    }

    /** Compute Compose's traversal order for every AndroidComposeView under [root]. Main thread. */
    fun prime(root: View): Result {
        val result = Result()
        val acvs = ArrayList<View>()
        collect(root, acvs, 0)
        for (acv in acvs) {
            try {
                primeOne(acv, result)
            } catch (t: Throwable) {
                result.failed++
                logOnce("prime", t)
            }
        }
        return result
    }

    /** Clear the maps [prime] filled from empty, so the app is left as it was. */
    fun restore(result: Result) {
        for (map in result.toClear) {
            try {
                map.javaClass.getMethod("clear").invoke(map)
            } catch (t: Throwable) {
                logOnce("restore", t)
            }
        }
        result.toClear.clear()
    }

    private fun collect(view: View, out: MutableList<View>, depth: Int) {
        if (depth > VIEW_MAX_DEPTH) return
        if (ComposeInspector.isAndroidComposeView(view)) out.add(view)
        if (view is ViewGroup) {
            val n = try { view.childCount } catch (_: Throwable) { 0 }
            for (i in 0 until n) {
                val child = try { view.getChildAt(i) } catch (_: Throwable) { null } ?: continue
                collect(child, out, depth + 1)
            }
        }
    }

    private fun primeOne(acv: View, result: Result) {
        val delegate = delegateOf(acv)
        if (delegate == null) {
            result.failed++
            return
        }
        val cls = delegate.javaClass
        val enabled = cls.declaredMethods.firstOrNull {
            it.name.startsWith("isEnabled$") && it.parameterCount == 0 &&
                it.returnType == Boolean::class.javaPrimitiveType
        }
        if (enabled != null) {
            enabled.isAccessible = true
            if (enabled.invoke(delegate) == true) {
                result.byService++
                return
            }
        }
        val before = mapField(cls, "idToBeforeMap")?.get(delegate)
        val after = mapField(cls, "idToAfterMap")?.get(delegate)
        if (before == null || after == null) {
            result.failed++
            return
        }
        val wasEmpty = isEmpty(before) && isEmpty(after)

        // ui 1.7 / 1.8: private instance method.
        val instance = cls.declaredMethods.firstOrNull { it.name == "setTraversalValues" && it.parameterCount == 0 }
        if (instance != null) {
            instance.isAccessible = true
            instance.invoke(delegate)
        } else {
            // ui 1.9+ (checked on 1.12.1): a static helper fed the current semantics snapshot.
            val nodes = declared(cls, "getCurrentSemanticsNodes")?.invoke(delegate)
            val kt = Class.forName(DELEGATE_KT_CLASS, false, cls.classLoader)
            val static = kt.declaredMethods.firstOrNull { it.name == "setTraversalValues" && it.parameterCount == 4 }
                ?: kt.declaredMethods.firstOrNull { it.name == "access\$setTraversalValues" && it.parameterCount == 4 }
            if (nodes == null || static == null) {
                result.failed++
                return
            }
            static.isAccessible = true
            static.invoke(null, nodes, before, after, acv.context.resources)
        }
        result.computed++
        if (wasEmpty) {
            result.toClear.add(before)
            result.toClear.add(after)
        }
    }

    /** The AndroidComposeView's AndroidComposeViewAccessibilityDelegateCompat, found by field type. */
    private fun delegateOf(acv: View): Any? {
        var k: Class<*>? = acv.javaClass
        while (k != null && k != View::class.java) {
            for (f in k.declaredFields) {
                if (f.type.name == DELEGATE_CLASS) {
                    f.isAccessible = true
                    return f.get(acv)
                }
            }
            k = k.superclass
        }
        return null
    }

    private fun mapField(cls: Class<*>, name: String): Field? = try {
        cls.getDeclaredField(name).also { it.isAccessible = true }
    } catch (_: NoSuchFieldException) {
        null
    }

    private fun declared(cls: Class<*>, name: String): Method? =
        cls.declaredMethods.firstOrNull { it.name == name && it.parameterCount == 0 }
            ?.also { it.isAccessible = true }

    private fun isEmpty(map: Any): Boolean = try {
        map.javaClass.getMethod("isEmpty").invoke(map) == true
    } catch (_: Throwable) {
        false
    }

    private val logged = HashSet<String>()

    private fun logOnce(kind: String, t: Throwable) {
        if (logged.add(kind)) Log.w(TAG, "Compose traversal order: $kind failed", t)
    }
}
