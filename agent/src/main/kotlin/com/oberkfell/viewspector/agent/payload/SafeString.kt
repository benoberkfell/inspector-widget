/*
 * ViewSpector — payload :: turn values read out of the app into strings without running the app's
 * toString().
 *
 * The payload ships values it reads from the app's own objects (Compose semantics configurations,
 * composable call parameters, modifier arguments) as strings. toString() on those objects is code
 * the payload does not control, and on real apps it fails: Compose's AccessibilityAction.toString()
 * prints its action, a Kotlin FunctionReference renders itself through kotlin-reflect, and an app
 * that bundles kotlin-reflect without its builtins metadata throws
 * AssertionError("Built-in class kotlin.Any is not found") out of it (seen on Thunderbird,
 * Compose 1.12.1). An app data class can also print megabytes or do work in toString().
 *
 * [render] therefore calls toString() only on classes whose toString is known to format plain
 * values: framework strings and boxed primitives, enums (through name(), which is final), and an
 * allowlist of Kotlin and Compose value types matched by exact class name. It unpacks known
 * carriers through their getters (AnnotatedString.text, AccessibilityAction.label, the counts of
 * CollectionInfo), reads any other CharSequence through length/get, recurses into collections,
 * maps, arrays and kotlin Pair/Triple with caps, writes functions and lambdas as "<lambda>", and
 * writes anything else as its class's simple name.
 *
 * Classes are recognised by NAME, never with `is` against a Kotlin or Compose type: the payload may
 * be loaded child-first (its kotlin.* classes are then not the app's), and names keep this file
 * free of any compile-time Compose dependency. It uses only java.* and reflection, so it also runs
 * on a plain JVM. In an R8-renamed app no Compose name matches and every such value falls back to
 * its (renamed) class name, which is still safe.
 */
package com.oberkfell.viewspector.agent.payload

import java.lang.reflect.InvocationTargetException
import java.lang.reflect.Method

internal object SafeString {

    /** Printed for an AccessibilityAction / CustomAccessibilityAction without a label. */
    const val ACTION = "<action>"

    /** Printed for a function, lambda or function reference. */
    const val LAMBDA = "<lambda>"

    private const val MAX_ITEMS = 64
    private const val MAX_NESTING = 4
    private const val MAX_CHARS = 20_000

    /** [v] as a string; never throws (an internal failure yields "<error:ExceptionName>"). */
    fun of(v: Any?): String = try {
        render(v)
    } catch (t: Throwable) {
        errorToken(t)
    }

    /**
     * [v] as a string; throws only if a getter of a known carrier throws, so a caller can count
     * the failure. null renders as "null".
     */
    fun render(v: Any?): String = render(v, 0)

    /** "<error:Name>" for a failure, [Throwable] unwrapped from reflection. */
    fun errorToken(t: Throwable): String = "<error:${errorName(t)}>"

    /** The simple class name of [t] (the target of an InvocationTargetException). Never throws. */
    fun errorName(t: Throwable): String {
        val cause = (t as? InvocationTargetException)?.targetException ?: t
        return simpleNameOf(cause.javaClass)
    }

    /** The class's simple name ("" for anonymous classes becomes the tail of its binary name). */
    fun simpleNameOf(cls: Class<*>): String {
        val simple = try {
            cls.simpleName
        } catch (_: Throwable) {
            null
        }
        if (!simple.isNullOrEmpty()) return simple
        return cls.name.substringAfterLast('.')
    }

    // ------------------------------------------------------------------ rendering
    private val bootLoader: ClassLoader? = String::class.java.classLoader

    /** Loaded by the boot loader: a java.* or framework (android.*) class, not app code. */
    private fun isBoot(cls: Class<*>): Boolean = cls.classLoader === bootLoader

    private fun render(v: Any?, depth: Int): String {
        if (v == null) return "null"
        if (v is String) return v
        val cls = v.javaClass
        if (isBoot(cls)) {
            when (v) {
                is CharSequence, is Number, is Boolean, is Char -> return v.toString()
                is Enum<*> -> return v.name
                is Class<*> -> return v.name
            }
            if (cls.name in BOOT_TO_STRING) return v.toString()
        }
        if (v is Enum<*>) return v.name
        val name = cls.name
        carrier(name, v, depth)?.let { return it }
        if (name in TO_STRING_SAFE) return v.toString()
        // Any other CharSequence (an R8-renamed AnnotatedString, an app Spannable): its chars,
        // read through length/get, never its toString.
        if (v is CharSequence) return chars(v)
        if (depth < MAX_NESTING) {
            when {
                v is Collection<*> -> return items(v.iterator(), depth, top = depth == 0)
                v is Map<*, *> -> return entries(v, depth)
                cls.isArray -> return array(v, depth)
            }
        }
        if (isFunction(cls)) return LAMBDA
        return simpleNameOf(cls)
    }

    private fun chars(cs: CharSequence): String {
        val n = cs.length
        val sb = StringBuilder(minOf(n, MAX_CHARS))
        for (i in 0 until minOf(n, MAX_CHARS)) sb.append(cs[i])
        if (n > MAX_CHARS) sb.append('…')
        return sb.toString()
    }

    /** Items joined with ", "; bracketed unless [top] (a top-level list keeps the old bare form). */
    private fun items(iter: Iterator<*>, depth: Int, top: Boolean): String {
        val sb = StringBuilder()
        var n = 0
        while (iter.hasNext()) {
            val item = iter.next()
            if (n == MAX_ITEMS) {
                sb.append(", …")
                break
            }
            if (n > 0) sb.append(", ")
            sb.append(render(item, depth + 1))
            n++
        }
        return if (top) sb.toString() else "[$sb]"
    }

    private fun entries(map: Map<*, *>, depth: Int): String {
        val sb = StringBuilder("{")
        var n = 0
        for (e in map.entries) {
            if (n == MAX_ITEMS) {
                sb.append(", …")
                break
            }
            if (n > 0) sb.append(", ")
            sb.append(render(e.key, depth + 1)).append('=').append(render(e.value, depth + 1))
            n++
        }
        return sb.append('}').toString()
    }

    private fun array(arr: Any, depth: Int): String {
        val len = java.lang.reflect.Array.getLength(arr)
        val list = ArrayList<Any?>(minOf(len, MAX_ITEMS + 1))
        for (i in 0 until minOf(len, MAX_ITEMS + 1)) list.add(java.lang.reflect.Array.get(arr, i))
        return "[" + items(list.iterator(), depth, top = true) + "]"
    }

    /**
     * A function object: a Kotlin lambda or function reference, or a D8/javac lambda. Its toString
     * is never called (a Kotlin one renders through kotlin-reflect).
     */
    fun isFunction(cls: Class<*>): Boolean {
        val name = cls.name
        if (cls.isSynthetic || "\$\$Lambda" in name || "ExternalSyntheticLambda" in name) return true
        var k: Class<*>? = cls
        var guard = 0
        while (k != null && k != Any::class.java && guard++ < 16) {
            if (k.name in FUNCTION_BASES) return true
            for (i in k.interfaces) {
                val n = i.name
                if (n == "kotlin.Function" || n.startsWith("kotlin.jvm.functions.Function") ||
                    n.startsWith("kotlin.reflect.")
                ) {
                    return true
                }
            }
            k = k.superclass
        }
        return false
    }

    private val FUNCTION_BASES = setOf(
        "kotlin.jvm.internal.Lambda",
        "kotlin.jvm.internal.CallableReference",
        "kotlin.jvm.internal.FunctionReference",
        "kotlin.jvm.internal.FunctionReferenceImpl",
        "kotlin.jvm.internal.AdaptedFunctionReference",
        "kotlin.jvm.internal.PropertyReference",
    )

    /** Framework classes whose toString prints only their own numbers or text. */
    private val BOOT_TO_STRING = setOf(
        "android.graphics.Rect", "android.graphics.RectF",
        "android.graphics.Point", "android.graphics.PointF",
        "android.util.Size", "android.util.SizeF",
        "java.util.Locale", "java.util.UUID",
    )

    /**
     * Kotlin and Compose value types whose toString formats only numbers, enums or fixed text (no
     * nested app object, no lambda). ScrollAxisRange's toString calls its value/maxValue functions,
     * exactly as Compose's accessibility delegate does to fill a node's scroll range.
     */
    private val TO_STRING_SAFE = setOf(
        "kotlin.ranges.IntRange", "kotlin.ranges.LongRange", "kotlin.ranges.CharRange",
        "kotlin.ranges.ClosedFloatRange", "kotlin.ranges.ClosedDoubleRange",
        "kotlin.ranges.OpenEndFloatRange", "kotlin.ranges.OpenEndDoubleRange",
        "androidx.compose.ui.semantics.Role",
        "androidx.compose.ui.semantics.LiveRegionMode",
        "androidx.compose.ui.semantics.ScrollAxisRange",
        "androidx.compose.ui.text.TextRange",
        "androidx.compose.ui.text.input.ImeAction",
        "androidx.compose.ui.text.input.KeyboardType",
        "androidx.compose.ui.text.input.KeyboardCapitalization",
        "androidx.compose.ui.text.font.FontWeight",
        "androidx.compose.ui.text.font.FontStyle",
        "androidx.compose.ui.text.style.TextAlign",
        "androidx.compose.ui.text.style.TextDecoration",
        "androidx.compose.ui.text.style.TextOverflow",
        "androidx.compose.ui.text.style.TextDirection",
        "androidx.compose.ui.unit.Dp", "androidx.compose.ui.unit.DpOffset",
        "androidx.compose.ui.unit.DpSize", "androidx.compose.ui.unit.DpRect",
        "androidx.compose.ui.unit.TextUnit", "androidx.compose.ui.unit.IntOffset",
        "androidx.compose.ui.unit.IntSize", "androidx.compose.ui.unit.IntRect",
        "androidx.compose.ui.unit.Constraints",
        "androidx.compose.ui.geometry.Offset", "androidx.compose.ui.geometry.Size",
        "androidx.compose.ui.geometry.Rect", "androidx.compose.ui.geometry.CornerRadius",
        "androidx.compose.ui.geometry.RoundRect",
        "androidx.compose.ui.graphics.Color", "androidx.compose.ui.graphics.SolidColor",
        "androidx.compose.ui.graphics.RectangleShapeKt\$RectangleShape\$1",
        "androidx.compose.ui.BiasAlignment", "androidx.compose.ui.BiasAlignment\$Horizontal",
        "androidx.compose.ui.BiasAlignment\$Vertical", "androidx.compose.ui.BiasAbsoluteAlignment",
        "androidx.compose.ui.BiasAbsoluteAlignment\$Horizontal",
        "androidx.compose.foundation.shape.RoundedCornerShape",
        "androidx.compose.foundation.shape.CutCornerShape",
        "androidx.compose.foundation.layout.PaddingValuesImpl",
    )

    /** A known carrier [v] (class [name]) unpacked through its getters; null when not a carrier. */
    private fun carrier(name: String, v: Any, d: Int): String? = when (name) {
        "kotlin.Unit" -> "kotlin.Unit"
        "kotlin.Pair" -> "(" + render(get(v, "getFirst"), d + 1) + ", " + render(get(v, "getSecond"), d + 1) + ")"
        "kotlin.Triple" ->
            "(" + render(get(v, "getFirst"), d + 1) + ", " + render(get(v, "getSecond"), d + 1) +
                ", " + render(get(v, "getThird"), d + 1) + ")"
        // A CharSequence; its text is the only part a reader wants.
        "androidx.compose.ui.text.AnnotatedString" -> render(get(v, "getText"), d + 1)
        // Their toString prints the action function (the Thunderbird crash); the label is what
        // TalkBack reads.
        "androidx.compose.ui.semantics.AccessibilityAction",
        "androidx.compose.ui.semantics.CustomAccessibilityAction",
        -> label(v)
        // The current value only (the form the host has always received).
        "androidx.compose.ui.semantics.ProgressBarRangeInfo" -> render(get(v, "getCurrent"), d + 1)
        // No toString of their own (Object.toString would print an identity hash).
        "androidx.compose.ui.semantics.CollectionInfo" ->
            "CollectionInfo(rowCount=${render(get(v, "getRowCount"), d + 1)}, " +
                "columnCount=${render(get(v, "getColumnCount"), d + 1)})"
        "androidx.compose.ui.semantics.CollectionItemInfo" ->
            "CollectionItemInfo(rowIndex=${render(get(v, "getRowIndex"), d + 1)}, " +
                "rowSpan=${render(get(v, "getRowSpan"), d + 1)}, " +
                "columnIndex=${render(get(v, "getColumnIndex"), d + 1)}, " +
                "columnSpan=${render(get(v, "getColumnSpan"), d + 1)})"
        else -> null
    }

    private fun label(action: Any): String = (get(action, "getLabel") as? CharSequence)?.toString() ?: ACTION

    // ------------------------------------------------------------------ reflection
    private val methods = HashMap<String, Method?>()

    /** [obj].[name]() through a cached public no-arg method; null when absent. Throws if it throws. */
    private fun get(obj: Any, name: String): Any? {
        val key = obj.javaClass.name + "#" + name
        val m = synchronized(methods) {
            if (methods.containsKey(key)) {
                methods[key]
            } else {
                val found = try {
                    obj.javaClass.getMethod(name).also { it.isAccessible = true }
                } catch (_: Throwable) {
                    null
                }
                methods[key] = found
                found
            }
        } ?: return null
        return m.invoke(obj)
    }
}
