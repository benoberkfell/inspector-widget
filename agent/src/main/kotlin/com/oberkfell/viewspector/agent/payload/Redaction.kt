/*
 * ViewSpector — payload :: password redaction.
 *
 * The payload must never send the plaintext of a password field. TextView.getText()
 * on a password EditText returns the real characters (the bullets on screen are only a
 * TransformationMethod), and so would the "text" property and the accessibility text of
 * a visible-password field. Every text path masks such text with one U+2022 per
 * character (the PasswordTransformationMethod dot), so the length (and "has text")
 * survives for linting while the secret does not.
 */
package com.oberkfell.viewspector.agent.payload

import android.os.Build
import android.text.InputType
import android.text.method.PasswordTransformationMethod
import android.util.Log
import android.view.View
import android.widget.TextView

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

    /** Compose semantics keys whose value is the text field's content. */
    private val COMPOSE_SECRET_KEYS = arrayOf("EditableText", "InputText")

    /**
     * Mask the field content in a Compose semantics config ([attrs] = name -> stringified
     * value, as ComposeInspector reads it) when the node is a password field (it carries
     * the Password key). Returns true when something was masked.
     */
    fun redactComposeAttrs(attrs: MutableMap<String, String>): Boolean {
        if (!attrs.containsKey("Password")) return false
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
