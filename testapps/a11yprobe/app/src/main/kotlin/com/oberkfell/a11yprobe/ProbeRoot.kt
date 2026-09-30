// ============================================================================
// ProbeRoot.kt — the root every A11yProbe composition sits under.
//
// Compose keeps testTags out of the AccessibilityNodeInfo tree unless the
// semantics tree opts in with `testTagsAsResourceId`. Every composition
// (the launcher, each RecyclerView ComposeView cell, the hybrid cells, each
// Compose Dialog window) is its own semantics owner, so each one must opt in.
// With it, `Modifier.testTag("bad_icon_button")` surfaces as the a11y node's
// viewIdResourceName, which is how the host golden test
// (host/tests/test_device_a11y_golden.py) finds GOOD/BAD nodes in the unified
// a11y dump without depending on semantics ids.
// ============================================================================
package com.oberkfell.a11yprobe

import androidx.compose.foundation.layout.Box
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.testTagsAsResourceId

/** MaterialTheme + [TagsAsResourceIds]: wrap the content of every ComposeView with this. */
@Composable
fun ProbeRoot(content: @Composable () -> Unit) {
    MaterialTheme { TagsAsResourceIds(content) }
}

/**
 * Exposes testTags as a11y resource ids for everything below. Use it directly inside a
 * Compose `Dialog { }`: the dialog is a separate window (and semantics owner), so the
 * flag set by the enclosing [ProbeRoot] does not reach it.
 */
@OptIn(ExperimentalComposeUiApi::class)
@Composable
fun TagsAsResourceIds(content: @Composable () -> Unit) {
    Box(Modifier.semantics { testTagsAsResourceId = true }) { content() }
}
