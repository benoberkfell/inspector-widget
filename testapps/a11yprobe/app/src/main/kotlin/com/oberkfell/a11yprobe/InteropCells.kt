// ============================================================================
// InteropCells.kt — the list rows shared by the interop scenarios (S1..S6).
//
// A row is one of three kinds:
//   COMPOSE  a ComposeView cell (ComposeCell)
//   VIEW     a classic item_view_cell.xml row (bindViewCell)
//   HYBRID   a classic row whose action buttons live in a nested ComposeView
//
// Every row has the same controls — title, "Done" checkbox, "Notify" toggle,
// "More info" button, "Delete" button — so the repeated per-row labels are
// legitimate duplicates. A row listed in InteropScenario.flaws gets deliberate
// defects instead (the host golden test mirrors these tables):
//   UNLABELED     the delete button has no label        -> a11y.label.missing
//   SMALL_TARGET  the info button is a 24dp target      -> a11y.touch_target.small
//   STATELESS     the notify toggle has the Switch role but exposes no on/off
//                                                       -> a11y.state.not_exposed
// Compose controls carry testTags ("cell_<pos>", "done", "notify", "info",
// "delete") and View controls the same ids, so both surface in the a11y tree
// under the same resource names.
// ============================================================================
package com.oberkfell.a11yprobe

import android.view.View
import android.widget.CheckBox
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.TextView
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.selection.toggleable
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.Notifications
import androidx.compose.material3.Checkbox
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.ComposeView
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.core.view.AccessibilityDelegateCompat
import androidx.core.view.ViewCompat
import androidx.core.view.accessibility.AccessibilityNodeInfoCompat

/** A deliberate defect planted in one list row. */
enum class Flaw { UNLABELED, SMALL_TARGET, STATELESS }

/** What a list row is built from. [label] appears in the row title "Item <n> (<label>)". */
enum class CellKind(val label: String) { COMPOSE("compose"), VIEW("view"), HYBRID("hybrid") }

// ---------------------------------------------------------------------------
// Compose rows
// ---------------------------------------------------------------------------

/** A full Compose row (ComposeView cell, or a LazyColumn item). */
@Composable
fun ComposeCell(position: Int, flaws: Set<Flaw>) {
    var done by remember(position) { mutableStateOf(position % 2 == 0) }
    Row(
        Modifier
            .fillMaxWidth()
            .heightIn(min = 64.dp)
            .clickable { }
            .padding(start = 16.dp, end = 8.dp)
            .testTag("cell_$position"),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text("Item $position (${CellKind.COMPOSE.label})", Modifier.weight(1f))
        Checkbox(
            checked = done,
            onCheckedChange = { done = it },
            modifier = Modifier
                .testTag("done")
                .semantics { contentDescription = "Done" },
        )
        CellActions(position, flaws)
    }
}

/** The notify / info / delete controls, shared by Compose rows and the hybrid row's ComposeView. */
@Composable
fun CellActions(position: Int, flaws: Set<Flaw>) {
    NotifyToggle(position, stateless = Flaw.STATELESS in flaws)
    InfoButton(small = Flaw.SMALL_TARGET in flaws)
    DeleteButton(labeled = Flaw.UNLABELED !in flaws)
}

/**
 * GOOD: `toggleable(role = Switch)` exposes on/off.
 * BAD (stateless): `clickable(role = Switch)` — TalkBack says "Notify, Switch" and
 * never says whether it is on.
 */
@Composable
fun NotifyToggle(position: Int, stateless: Boolean) {
    var on by remember(position) { mutableStateOf(position % 3 == 0) }
    val tagged = Modifier.size(48.dp).testTag("notify")
    val interactive =
        if (stateless) tagged.clickable(role = Role.Switch) { on = !on }
        else tagged.toggleable(value = on, role = Role.Switch) { on = it }
    Box(interactive, contentAlignment = Alignment.Center) {
        Icon(
            Icons.Filled.Notifications,
            contentDescription = "Notify",
            tint = LocalContentColor.current.copy(alpha = if (on) 1f else 0.38f),
        )
    }
}

/** GOOD: a 48dp target. BAD (small): the same button shrunk to a 24dp target. */
@Composable
fun InfoButton(small: Boolean) {
    val target = if (small) Modifier.padding(horizontal = 12.dp).size(24.dp) else Modifier.size(48.dp)
    Box(
        target
            .testTag("info")
            .clickable(role = Role.Button) { },
        contentAlignment = Alignment.Center,
    ) {
        Icon(Icons.Filled.Info, contentDescription = "More info", modifier = Modifier.size(24.dp))
    }
}

/** GOOD: labeled "Delete". BAD (unlabeled): the icon has no contentDescription. */
@Composable
fun DeleteButton(labeled: Boolean) {
    IconButton(onClick = { }, modifier = Modifier.testTag("delete")) {
        Icon(Icons.Filled.Delete, contentDescription = if (labeled) "Delete" else null)
    }
}

// ---------------------------------------------------------------------------
// Classic View rows
// ---------------------------------------------------------------------------

/**
 * Bind an item_view_cell.xml row (RecyclerView cell or LazyColumn AndroidView item).
 * Sets EVERY property on every bind: RecyclerView recycles rows across positions.
 * [compact] hides the checkbox (the 2-column grid has no room for it).
 */
fun bindViewCell(root: View, position: Int, flaws: Set<Flaw>, compact: Boolean = false) {
    root.findViewById<TextView>(R.id.title).text = "Item $position (${CellKind.VIEW.label})"
    root.findViewById<CheckBox>(R.id.check).apply {
        isChecked = position % 2 == 0
        visibility = if (compact) View.GONE else View.VISIBLE
    }
    bindNotify(root.findViewById(R.id.notify), position, stateless = Flaw.STATELESS in flaws)
    root.findViewById<ImageButton>(R.id.info).apply {
        contentDescription = "More info"
        val small = Flaw.SMALL_TARGET in flaws
        val lp = layoutParams as LinearLayout.LayoutParams
        lp.width = dp(this, if (small) 24 else 48)
        lp.height = lp.width
        lp.marginStart = if (small) dp(this, 12) else 0
        lp.marginEnd = lp.marginStart
        layoutParams = lp
        setPadding(if (small) 0 else dp(this, 12))
        setOnClickListener { }
    }
    root.findViewById<ImageButton>(R.id.delete).apply {
        contentDescription = if (Flaw.UNLABELED in flaws) null else "Delete"
        setOnClickListener { }
    }
    root.setOnClickListener { }
}

/** Bind an item_hybrid_cell.xml row: classic title + checkbox, Compose action buttons. */
fun bindHybridCell(root: View, position: Int, flaws: Set<Flaw>) {
    root.findViewById<TextView>(R.id.hybrid_title).text = "Item $position (${CellKind.HYBRID.label})"
    root.findViewById<CheckBox>(R.id.hybrid_check).isChecked = position % 2 == 0
    root.findViewById<ComposeView>(R.id.hybrid_compose).setContent {
        ProbeRoot {
            Row(verticalAlignment = Alignment.CenterVertically) { CellActions(position, flaws) }
        }
    }
    root.setOnClickListener { }
}

/**
 * A classic "Notify" toggle: an ImageButton whose delegate reports the Switch class.
 * GOOD: also reports checkable + checked. BAD (stateless): the Switch class only.
 */
private fun bindNotify(button: ImageButton, position: Int, stateless: Boolean) {
    button.contentDescription = "Notify"
    button.setTag(R.id.tag_notify_on, position % 3 == 0)
    fun render() {
        button.alpha = if (button.getTag(R.id.tag_notify_on) == true) 1f else 0.38f
    }
    render()
    button.setOnClickListener {
        it.setTag(R.id.tag_notify_on, it.getTag(R.id.tag_notify_on) != true)
        render()
    }
    ViewCompat.setAccessibilityDelegate(button, NotifyDelegate(stateless))
}

private class NotifyDelegate(private val stateless: Boolean) : AccessibilityDelegateCompat() {
    override fun onInitializeAccessibilityNodeInfo(host: View, info: AccessibilityNodeInfoCompat) {
        super.onInitializeAccessibilityNodeInfo(host, info)
        info.className = "android.widget.Switch"
        if (!stateless) {
            info.isCheckable = true
            info.isChecked = host.getTag(R.id.tag_notify_on) == true
        }
    }
}

private fun View.setPadding(all: Int) = setPadding(all, all, all, all)

private fun dp(view: View, value: Int): Int =
    (value * view.resources.displayMetrics.density).toInt()
