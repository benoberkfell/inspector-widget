/*
 * ViewSpector — payload :: password redaction.
 *
 * The payload must never send the plaintext of a password field. TextView.getText()
 * on a password EditText returns the real characters (the bullets on screen are only a
 * TransformationMethod), and so would the "text" property and the accessibility text of
 * a visible-password field. Every text path masks such text with one U+2022 per
 * character (the PasswordTransformationMethod dot), so the length (and "has text")
 * survives for linting while the secret does not: the View tree and properties
 * (TreeBuilder, Properties), the a11y text (AccessibilityInspector), the recorded
 * accessibility events (A11yEventTap), and the Compose semantics and slot table
 * (ComposeInspector, through [Secrets] for the values a password field's text reaches).
 * A Compose text field is a password field by ComposeInspector.isPasswordNode, which the
 * semantics dump, the a11y text and the event tap all ask, so the three agree.
 */
package com.oberkfell.viewspector.agent.payload

import android.os.Build
import android.text.InputType
import android.text.method.PasswordTransformationMethod
import android.util.Log
import android.view.View
import android.widget.TextView
import java.lang.reflect.Field
import java.lang.reflect.Modifier

object Redaction {

    private const val TAG = "ViewSpector"

    /** PasswordTransformationMethod's dot. */
    const val MASK_CHAR = '•'

    /** One [MASK_CHAR] per UTF-16 unit of [text], as PasswordTransformationMethod draws it. */
    fun mask(text: CharSequence): String {
        val n = text.length
        if (n == 0) return ""
        val sb = StringBuilder(n)
        repeat(n) { sb.append(MASK_CHAR) }
        return sb.toString()
    }

    /**
     * True for the password input types: text password, web password, visible password
     * and number password. Mirrors TextView.isPasswordInputType +
     * isVisiblePasswordInputType (frameworks/base TextView.java).
     */
    fun isPasswordInputType(inputType: Int): Boolean {
        val variation = inputType and (InputType.TYPE_MASK_CLASS or InputType.TYPE_MASK_VARIATION)
        return variation == (InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD) ||
            variation == (InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_WEB_PASSWORD) ||
            variation == (InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD) ||
            variation == (InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_VARIATION_PASSWORD)
    }

    /**
     * Whether [view] holds a password: a TextView whose input type is a password variation
     * (this stays true while a "show password" toggle has the text visible) or whose
     * transformation method masks it, or any View whose autofill hints name a password.
     * Any failure counts as "not a password" only when nothing points at one.
     */
    fun isPasswordView(view: View): Boolean {
        if (view is TextView) {
            try {
                if (isPasswordInputType(view.inputType)) return true
            } catch (t: Throwable) {
                Log.w(TAG, "TextView.getInputType() failed", t)
            }
            try {
                if (view.transformationMethod is PasswordTransformationMethod) return true
            } catch (t: Throwable) {
                Log.w(TAG, "TextView.getTransformationMethod() failed", t)
            }
        }
        if (Build.VERSION.SDK_INT >= 26) {
            try {
                // View.AUTOFILL_HINT_PASSWORD is "password"; androidx adds "newPassword".
                view.autofillHints?.forEach { hint ->
                    if (hint != null && hint.contains("password", ignoreCase = true)) return true
                }
            } catch (t: Throwable) {
                Log.w(TAG, "View.getAutofillHints() failed", t)
            }
        }
        return false
    }

    private const val PASSWORD_VISUAL_TRANSFORMATION =
        "androidx.compose.ui.text.input.PasswordVisualTransformation"

    // androidx.compose.ui.text.input.KeyboardType.Password / .NumberPassword (a value class
    // over Int; the getter is name-mangled, e.g. getKeyboardType-PjHm6EE).
    private const val KEYBOARD_TYPE_PASSWORD = 7
    private const val KEYBOARD_TYPE_NUMBER_PASSWORD = 8

    /**
     * Whether a Compose value marks its field as a password field: a
     * PasswordVisualTransformation, or KeyboardOptions (the composable's parameter) or
     * ImeOptions (what the text field hands its modifiers) with a password keyboard. Matched
     * by class name (the slot table is only readable when Compose is not renamed anyway).
     * Never throws; anything unreadable is "no".
     */
    fun isComposePasswordParam(v: Any?): Boolean {
        if (v == null) return false
        return try {
            val cls = v.javaClass
            when (cls.name) {
                PASSWORD_VISUAL_TRANSFORMATION -> true
                "androidx.compose.foundation.text.KeyboardOptions",
                "androidx.compose.ui.text.input.ImeOptions",
                -> {
                    val getter = cls.methods.firstOrNull {
                        it.name.startsWith("getKeyboardType") && it.parameterTypes.isEmpty() &&
                            it.returnType == Int::class.javaPrimitiveType
                    } ?: return false
                    val type = getter.invoke(v) as? Int
                    type == KEYBOARD_TYPE_PASSWORD || type == KEYBOARD_TYPE_NUMBER_PASSWORD
                }
                else -> false
            }
        } catch (_: Throwable) {
            false
        }
    }

    // Fields read from one object by [isComposePasswordModifier] (a text field element has ~12).
    private const val MAX_SCANNED_FIELDS = 32

    private val scannedFields = HashMap<Class<*>, List<Field>>()

    /**
     * Whether a Compose modifier element holds a password-field value ([isComposePasswordParam])
     * in one of its fields, or in a field of a function it holds. This is how a text field
     * without Password semantics still shows it is one: a visible-password field
     * (KeyboardOptions(keyboardType = Password), no visual transformation) passes its
     * ImeOptions to its semantics modifier, as a field (CoreTextFieldSemanticsModifier,
     * foundation 1.8+) or captured by the semantics lambda (AppendedSemanticsElement.properties,
     * 1.7), and BasicTextField(TextFieldState) keeps its KeyboardOptions on
     * TextFieldDecoratorModifier. Only fields are read: no app code runs. Never throws.
     */
    fun isComposePasswordModifier(element: Any): Boolean = try {
        fieldValues(element).any { v ->
            isComposePasswordParam(v) ||
                (v != null && SafeString.isFunction(v.javaClass) && fieldValues(v).any { isComposePasswordParam(it) })
        }
    } catch (_: Throwable) {
        false
    }

    /** The values of [obj]'s reference instance fields, its non-framework superclasses' included. */
    private fun fieldValues(obj: Any): List<Any?> {
        val fields = synchronized(scannedFields) {
            scannedFields.getOrPut(obj.javaClass) {
                val out = ArrayList<Field>()
                val boot = String::class.java.classLoader
                var k: Class<*>? = obj.javaClass
                while (k != null && k.classLoader !== boot && out.size < MAX_SCANNED_FIELDS) {
                    for (f in k.declaredFields) {
                        if (out.size == MAX_SCANNED_FIELDS) break
                        if (Modifier.isStatic(f.modifiers) || f.type.isPrimitive) continue
                        try {
                            f.isAccessible = true
                            out.add(f)
                        } catch (_: Throwable) {
                        }
                    }
                    k = k.superclass
                }
                out
            }
        }
        return fields.map { f ->
            try {
                f.get(obj)
            } catch (_: Throwable) {
                null
            }
        }
    }

    /**
     * Whether a Compose ContentType semantics value names a password: its autofill hints
     * (AndroidContentType.getAndroidAutofillHints) include "password" or "newPassword", the
     * hints [isPasswordView] looks for on a View. Compose foundation 1.10+ sets ContentType.Password
     * on a field with a password keyboard; an app may set it on any node. Never throws.
     */
    fun isPasswordContentType(v: Any?): Boolean {
        if (v == null) return false
        return try {
            val getter = v.javaClass.methods.firstOrNull {
                it.name == "getAndroidAutofillHints" && it.parameterTypes.isEmpty()
            } ?: return false
            (getter.invoke(v) as? Collection<*>)?.any {
                it is String && it.contains("password", ignoreCase = true)
            } == true
        } catch (_: Throwable) {
            false
        }
    }

    /** SecureTextField / OutlinedSecureTextField / BasicSecureTextField (Compose 1.7+). */
    fun isComposeSecureFieldName(name: String): Boolean = name.endsWith("SecureTextField")

    /** Words of a parameter name that say it holds a secret (see [isSecretParamName]). */
    private val SECRET_NAME_WORDS = setOf(
        "password", "passwd", "passcode", "passphrase", "pin", "pincode", "secret", "credential",
        "credentials",
    )

    /**
     * Whether a composable parameter's [name] says it holds a secret: one of its words
     * (camelCase or snake_case: newPassword, pin_code, userSecret) is password, passcode, pin,
     * secret... Used only on a composable that wraps a password field, which may take the
     * secret as a String and build the field's transformation itself (Thunderbird's
     * PasswordInput(password = ...)).
     */
    fun isSecretParamName(name: String): Boolean {
        val words = ArrayList<String>()
        val sb = StringBuilder()
        for (i in name.indices) {
            val c = name[i]
            val separator = c == '_' || c == '-'
            val boundary = separator ||
                (c.isUpperCase() && i > 0 && (name[i - 1].isLowerCase() || name[i - 1].isDigit()))
            if (boundary && sb.isNotEmpty()) {
                words.add(sb.toString().lowercase())
                sb.setLength(0)
            }
            if (!separator) sb.append(c)
        }
        if (sb.isNotEmpty()) words.add(sb.toString().lowercase())
        return words.any { it in SECRET_NAME_WORDS }
    }

    /**
     * Secret texts (a password field's content) and the masking of any string that is one or
     * embeds one. A secret shorter than [MIN_EMBEDDED] chars masks only a string equal to it:
     * masking every "a" inside every other string would garble the dump, not protect it.
     */
    class Secrets {
        private val texts = HashSet<String>()
        private var longestFirst: List<String>? = null

        /** Adds [text] unless it is empty or already masked (the dots are no secret). */
        fun add(text: String) {
            if (text.isEmpty() || text.all { it == MASK_CHAR }) return
            if (texts.add(text)) longestFirst = null
        }

        fun addAll(texts: Iterable<String>) {
            for (t in texts) add(t)
        }

        fun isEmpty(): Boolean = texts.isEmpty()

        /** [value] masked when it is a secret; else each secret it embeds masked in place. */
        fun mask(value: String): String {
            if (texts.isEmpty() || value.isEmpty()) return value
            if (value in texts) return Redaction.mask(value)
            val order = longestFirst
                ?: texts.filter { it.length >= MIN_EMBEDDED }.sortedByDescending { it.length }
                    .also { longestFirst = it }
            var out = value
            for (s in order) {
                if (out.contains(s)) out = out.replace(s, Redaction.mask(s))
            }
            return out
        }

        private companion object {
            const val MIN_EMBEDDED = 3
        }
    }

    /** Compose semantics keys whose value is the text field's content. */
    private val COMPOSE_SECRET_KEYS = arrayOf("EditableText", "InputText")

    /**
     * Mask the field content in a Compose semantics config ([attrs] = name -> stringified
     * value, as ComposeInspector reads it) when the node is a [password] field (by default:
     * it carries the Password key; ComposeInspector.isPasswordNode also knows a
     * visible-password field). The plaintext is added to [secrets] first (InputText is the raw
     * content; EditableText the transformed text, dots unless the field shows its password),
     * so the slot table can mask it wherever a composable takes it as a parameter. Returns
     * true when something was masked.
     */
    fun redactComposeAttrs(
        attrs: MutableMap<String, String>,
        secrets: Secrets? = null,
        password: Boolean = attrs.containsKey("Password"),
    ): Boolean {
        if (!password) return false
        if (secrets != null) for (key in COMPOSE_SECRET_KEYS) attrs[key]?.let { secrets.add(it) }
        var changed = false
        for (key in COMPOSE_SECRET_KEYS) {
            val v = attrs[key] ?: continue
            if (v.isEmpty()) continue
            attrs[key] = mask(v)
            changed = true
        }
        return changed
    }
}
