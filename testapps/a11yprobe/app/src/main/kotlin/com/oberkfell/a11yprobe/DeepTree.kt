// ============================================================================
// DeepTree.kt — scenario "deep_tree" (dump-only, no lint rule).
//
// Trees deeper than the agent's wire cap (80 levels, WireLimits.MAX_TREE_DEPTH):
// DEEP_TREE_LEVELS nested semantics nodes (each a labeled Box, so the Compose
// semantics tree and the accessibility tree nest that deep too) and an
// AndroidView holding DEEP_TREE_LEVELS nested FrameLayouts (the View tree).
// A protobuf message nested past about 100 levels cannot be parsed at all, so
// the agent cuts each tree at the cap and says so; the host golden test checks
// the dumps still parse and carry the truncation markers.
//
//   adb shell am start -n com.oberkfell.a11yprobe/.MainActivity --es scenario deep_tree
// ============================================================================
package com.oberkfell.a11yprobe

import android.content.Context
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.TextView
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView

/** Nesting of each deep tree; well past the 80-level cap. */
const val DEEP_TREE_LEVELS = 100

@Composable
fun DeepTreeScenario() {
    Column(
        Modifier
            .fillMaxSize()
            .padding(16.dp)
            .testTag("deep_tree")
    ) {
        Text(
            "Deep trees",
            style = MaterialTheme.typography.titleMedium,
            modifier = Modifier.semantics { heading() }
        )
        NestedSemantics(DEEP_TREE_LEVELS)
        AndroidView(
            factory = { context -> nestedFrames(context, DEEP_TREE_LEVELS) },
            modifier = Modifier
                .fillMaxWidth()
                .height(48.dp)
                .testTag("deep_views")
        )
    }
}

/** [level] Boxes, each its own semantics node, nested; the innermost holds a Text. */
@Composable
private fun NestedSemantics(level: Int) {
    Box(Modifier.semantics { contentDescription = "Level $level" }) {
        if (level > 1) NestedSemantics(level - 1) else Text("Deepest node")
    }
}

/** [levels] FrameLayouts, each the only child of the one above; the innermost holds a TextView. */
private fun nestedFrames(context: Context, levels: Int): View {
    val root = FrameLayout(context)
    var parent: ViewGroup = root
    repeat(levels - 1) {
        val child = FrameLayout(context)
        parent.addView(
            child,
            ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT),
        )
        parent = child
    }
    parent.addView(TextView(context).apply { text = "Deepest view" })
    return root
}
