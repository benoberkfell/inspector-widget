// ============================================================================
// TbHybrid.kt — the TalkBack navigation corpus (mixed View/Compose half).
//
//   adb shell am start -S -W -n com.oberkfell.a11yprobe/.InteropActivity \
//       --es scenario tb_h1 --es variant bad        # or good
//
// InteropFragment hosts these next to S1..S6 / D1 / D2 (INTEROP_SCENARIOS).
// Expectations: host/tests/data/tb_corpus_expected.json.
// ============================================================================
package com.oberkfell.a11yprobe

import android.content.Context
import android.os.Bundle
import android.util.TypedValue
import android.view.Gravity
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.ComposeView
import androidx.compose.ui.platform.ViewCompositionStrategy
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.isTraversalGroup
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.view.ViewCompat
import androidx.fragment.app.DialogFragment
import androidx.fragment.app.Fragment
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView

/** The hybrid TalkBack scenarios, appended to INTEROP_SCENARIOS. */
val TB_HYBRID_SCENARIOS: List<InteropScenario> = listOf(
    InteropScenario("tb_h1", "H1 ComposeView cells hidden"),
    InteropScenario("tb_h2", "H2 ComposeView cells recycled"),
    InteropScenario("tb_h3", "H3 AndroidView rows in a LazyColumn"),
    InteropScenario("tb_h4", "H4 A banner over a ComposeView"),
    InteropScenario("tb_h5", "H5 A Compose dialog over Views"),
    InteropScenario("tb_h6", "H6 Nested scrolling containers"),
)

/** The screen of hybrid scenario ``id`` in ``variant`` ("bad"/"good"). */
fun tbHybridScreen(fragment: Fragment, id: String, variant: String): View {
    val ctx = fragment.requireContext()
    val bad = variant.startsWith("bad")
    val title = "${id.removePrefix("tb_").uppercase()} ${variant.uppercase()}: " +
        TB_HYBRID_SCENARIOS.first { it.id == id }.title.substringAfter(' ')
    return when (id) {
        "tb_h1" -> h1HiddenCells(ctx, title, bad)
        "tb_h2" -> h2RecycledCells(ctx, title, bad)
        "tb_h3" -> h3AndroidViewRows(ctx, title, bad)
        "tb_h4" -> h4OverlayBanner(ctx, title, bad)
        "tb_h5" -> h5FragmentOverlay(fragment, title, bad)
        else -> h6NestedScroll(ctx, title)
    }
}

private fun Context.dp(v: Int) = (v * resources.displayMetrics.density).toInt()

private fun Context.header(title: String) = TextView(this).apply {
    text = title
    setTextSize(TypedValue.COMPLEX_UNIT_SP, 20f)
    setPadding(dp(16), dp(12), dp(16), dp(12))
    ViewCompat.setAccessibilityHeading(this, true)
}

private fun Context.column(title: String) = LinearLayout(this).apply {
    orientation = LinearLayout.VERTICAL
    fitsSystemWindows = true
    addView(header(title))
}

private fun LinearLayout.fill(v: View) = addView(v, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))

@androidx.compose.runtime.Composable
private fun ProductCell(n: Int) {
    Row(Modifier.fillMaxWidth().clickable {}.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
        Text("Product $n", Modifier.weight(1f))
        Text("$${n * 3}")
    }
}

// ---------------------------------------------------------------------------
// H1 compose_cell_hidden: BAD every ComposeView cell is importantForAccessibility
// = noHideDescendants (a folklore "fix" for double announcements): the whole cell
// is gone for TalkBack. GOOD the default.
// ---------------------------------------------------------------------------
private fun h1HiddenCells(ctx: Context, title: String, bad: Boolean): View = ctx.column(title).apply {
    fill(RecyclerView(ctx).apply {
        layoutManager = LinearLayoutManager(ctx)
        adapter = object : RecyclerView.Adapter<RecyclerView.ViewHolder>() {
            override fun getItemCount() = 20
            override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): RecyclerView.ViewHolder =
                object : RecyclerView.ViewHolder(ComposeView(parent.context).apply {
                    layoutParams = RecyclerView.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
                    if (bad) importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS
                }) {}

            override fun onBindViewHolder(holder: RecyclerView.ViewHolder, position: Int) {
                (holder.itemView as ComposeView).setContent { ProbeRoot { ProductCell(position + 1) } }
            }
        }
    })
}

// ---------------------------------------------------------------------------
// H2 compose_cell_recycle: ComposeView cells + TB_PROBE change_item. BAD the
// default composition strategy and no stable ids: a change rebinds (and
// re-composes) the cell under TalkBack's focus. GOOD stable ids, a payload
// update and DisposeOnViewTreeLifecycleDestroyed.
// ---------------------------------------------------------------------------
private fun h2RecycledCells(ctx: Context, title: String, bad: Boolean): View {
    val names = (1..30).map { "Track $it" }.toMutableList()
    val list = RecyclerView(ctx).apply { layoutManager = LinearLayoutManager(ctx) }
    val adapter = object : RecyclerView.Adapter<RecyclerView.ViewHolder>() {
        init { setHasStableIds(!bad) }
        override fun getItemCount() = names.size
        override fun getItemId(position: Int) = names[position].substringBefore(" (").hashCode().toLong()
        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): RecyclerView.ViewHolder =
            object : RecyclerView.ViewHolder(ComposeView(parent.context).apply {
                layoutParams = RecyclerView.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
                if (!bad) setViewCompositionStrategy(ViewCompositionStrategy.DisposeOnViewTreeLifecycleDestroyed)
            }) {}

        override fun onBindViewHolder(holder: RecyclerView.ViewHolder, position: Int) {
            val text = names[position]
            (holder.itemView as ComposeView).setContent {
                ProbeRoot { Text(text, Modifier.fillMaxWidth().clickable {}.padding(16.dp)) }
            }
        }
    }
    list.adapter = adapter
    val stop = TbProbe.listen { a ->
        when (a.action) {
            "change_item" -> if (a.index in names.indices) {
                names[a.index] = names[a.index].substringBefore(" (") + " (played)"
                if (bad) adapter.notifyDataSetChanged() else adapter.notifyItemChanged(a.index, "played")
            }
            "insert_top" -> { names.add(0, "New track"); if (bad) adapter.notifyDataSetChanged() else adapter.notifyItemInserted(0) }
            else -> adapter.notifyDataSetChanged()
        }
    }
    list.addOnAttachStateChangeListener(object : View.OnAttachStateChangeListener {
        override fun onViewAttachedToWindow(v: View) {}
        override fun onViewDetachedFromWindow(v: View) = stop()
    })
    return ctx.column(title).apply { fill(list) }
}

// ---------------------------------------------------------------------------
// H3 androidview_boundary: LazyColumn rows whose AndroidView root is
// importantForAccessibility=no, with TextViews inside. BAD the holder links the
// root, which TalkBack never sees (TalkBack 17 still reaches the TextViews, as
// separate stops). GOOD the root is one screen-reader stop for the row, inside
// a traversal group.
// ---------------------------------------------------------------------------
private fun h3AndroidViewRows(ctx: Context, title: String, bad: Boolean): View = ComposeView(ctx).apply {
    setContent {
        ProbeRoot {
            Column(Modifier.fillMaxSize().safeDrawingPadding()) {
                Text(title, style = MaterialTheme.typography.titleLarge,
                    modifier = Modifier.padding(16.dp).semantics { heading() })
                LazyColumn(Modifier.fillMaxSize()) {
                    items(20) { i ->
                        if (i % 2 == 0) {
                            ProductCell(i + 1)
                        } else {
                            AndroidView(
                                factory = { c ->
                                    LinearLayout(c).apply {
                                        orientation = LinearLayout.VERTICAL
                                        importantForAccessibility = if (bad) View.IMPORTANT_FOR_ACCESSIBILITY_NO
                                        else View.IMPORTANT_FOR_ACCESSIBILITY_YES
                                        if (!bad) isScreenReaderFocusable = true
                                        setPadding(c.dp(16), c.dp(12), c.dp(16), c.dp(12))
                                        addView(TextView(c).apply { text = "Product ${i + 1}"; setTextSize(TypedValue.COMPLEX_UNIT_SP, 16f) })
                                        addView(TextView(c).apply { text = "Sold out" })
                                    }
                                },
                                modifier = Modifier.fillMaxWidth()
                                    .then(if (bad) Modifier else Modifier.semantics { isTraversalGroup = true }),
                            )
                        }
                    }
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// H4 overlay_banner: BAD a banner View drawn over the top of a full-height
// ComposeView: STRIPE ties them on top and reads the taller ComposeView first,
// so the banner comes LAST. GOOD the banner sits above the ComposeView.
// ---------------------------------------------------------------------------
private fun h4OverlayBanner(ctx: Context, title: String, bad: Boolean): View {
    val banner = TextView(ctx).apply {
        text = "Offline: changes will sync later"
        setBackgroundColor(android.graphics.Color.rgb(0xFF, 0xE0, 0x82))
        setPadding(ctx.dp(16), ctx.dp(12), ctx.dp(16), ctx.dp(12))
    }
    val compose = ComposeView(ctx).apply {
        setContent {
            ProbeRoot {
                Column(Modifier.fillMaxSize().padding(top = if (bad) 120.dp else 0.dp)) {
                    Text(title, style = MaterialTheme.typography.titleLarge,
                        modifier = Modifier.padding(16.dp).semantics { heading() })
                    for (n in 1..8) ProductCell(n)
                }
            }
        }
    }
    return if (bad) {
        FrameLayout(ctx).apply {
            fitsSystemWindows = true
            addView(compose, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
            addView(banner, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.TOP))
        }
    } else {
        LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            fitsSystemWindows = true
            addView(banner)
            fill(compose)
        }
    }
}

// ---------------------------------------------------------------------------
// H5 fragment_overlay: BAD a Compose "dialog" (scrim + card) drawn in a
// ComposeView over the Fragment's classic Views, in the same window. GOOD a
// DialogFragment wrapping the same ComposeView content.
// ---------------------------------------------------------------------------
private fun h5FragmentOverlay(fragment: Fragment, title: String, bad: Boolean): View {
    val ctx = fragment.requireContext()
    val views = ctx.column(title).apply {
        for (n in 1..8) addView(Button(ctx).apply { text = "Account $n"; setOnClickListener { } })
    }
    if (!bad) {
        views.post { TbComposeDialog().show(fragment.childFragmentManager, "tb_h5") }
        return views
    }
    return FrameLayout(ctx).apply {
        addView(views)
        addView(ComposeView(ctx).apply { setContent { ProbeRoot { SignOutOverlay() } } },
            FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
    }
}

@androidx.compose.runtime.Composable
private fun SignOutOverlay() {
    Box(
        Modifier
            .fillMaxSize()
            .background(Color(0x99000000))
            .clickable(interactionSource = remember { MutableInteractionSource() }, indication = null) {},
        contentAlignment = Alignment.Center,
    ) { SignOutCard() }
}

@androidx.compose.runtime.Composable
private fun SignOutCard() {
    Card(Modifier.padding(32.dp)) {
        Column(Modifier.padding(24.dp)) {
            Text("Sign out?", style = MaterialTheme.typography.titleLarge, modifier = Modifier.semantics { heading() })
            Row {
                TextButton(onClick = {}) { Text("Stay") }
                TextButton(onClick = {}) { Text("Sign out") }
            }
        }
    }
}

/** H5 GOOD: the same content in its own (modal) dialog window. */
class TbComposeDialog : DialogFragment() {
    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View =
        ComposeView(requireContext()).apply { setContent { ProbeRoot { SignOutCard() } } }
}

// ---------------------------------------------------------------------------
// H6 nested_scroll (calibration, one variant): Compose > AndroidView >
// RecyclerView > cells, between Compose content; the walk checks TalkBack
// scrolls the inner RecyclerView and then leaves it for "After the list".
// ---------------------------------------------------------------------------
private fun h6NestedScroll(ctx: Context, title: String): View = ComposeView(ctx).apply {
    setContent {
        ProbeRoot {
            Column(Modifier.fillMaxSize().padding(top = 24.dp)) {
                Text(title, style = MaterialTheme.typography.titleLarge,
                    modifier = Modifier.padding(16.dp).semantics { heading() })
                AndroidView(
                    factory = { c ->
                        RecyclerView(c).apply {
                            layoutManager = LinearLayoutManager(c)
                            adapter = object : RecyclerView.Adapter<RecyclerView.ViewHolder>() {
                                override fun getItemCount() = 15
                                override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): RecyclerView.ViewHolder =
                                    object : RecyclerView.ViewHolder(TextView(parent.context).apply {
                                        layoutParams = RecyclerView.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, c.dp(64))
                                        setPadding(c.dp(16), c.dp(16), c.dp(16), c.dp(16))
                                        setOnClickListener { }
                                    }) {}

                                override fun onBindViewHolder(holder: RecyclerView.ViewHolder, position: Int) {
                                    (holder.itemView as TextView).text = "Inner row ${position + 1}"
                                }
                            }
                        }
                    },
                    modifier = Modifier.fillMaxWidth().weight(1f),
                )
                TextButton(onClick = {}, Modifier.fillMaxWidth()) { Text("After the list") }
            }
        }
    }
}
