// ============================================================================
// InteropFragment.kt — the mixed View/Compose hierarchies and dialog windows.
//
//   S1  RecyclerView, every cell a ComposeView
//   S2  RecyclerView, every cell a classic View row
//   S3  RecyclerView, mixed view types: ComposeView / View / View-containing-ComposeView
//   S4  Compose LazyColumn whose odd items are AndroidView-wrapped classic rows
//   S5  ComposeView -> AndroidView -> RecyclerView -> ComposeView and View cells
//   S6  RecyclerView + GridLayoutManager(2) of classic View cells
//   D1  a DialogFragment window (Views + a ComposeView) over a classic screen
//   D2  a Compose Dialog window over a Compose screen
//
// Each list has ITEM_COUNT rows so it scrolls. The rows named in
// InteropScenario.flaws carry deliberate defects (see InteropCells.kt); all of
// them sit in the first six rows so they are on screen without scrolling. The
// host golden test (host/tests/test_device_a11y_golden.py) mirrors these tables.
// ============================================================================
package com.oberkfell.a11yprobe

import android.os.Bundle
import android.util.TypedValue
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Favorite
import androidx.compose.material.icons.filled.Share
import androidx.compose.material3.Button as M3Button
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.ComposeView
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.ui.window.Dialog
import androidx.core.view.ViewCompat
import androidx.fragment.app.DialogFragment
import androidx.fragment.app.Fragment
import androidx.recyclerview.widget.GridLayoutManager
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView

/**
 * One interop scenario.
 *
 * @param flaws  row position -> the deliberate defects planted in that row.
 * @param kindAt what each row is built from.
 */
class InteropScenario(
    val id: String,
    val title: String,
    val flaws: Map<Int, Set<Flaw>> = emptyMap(),
    val kindAt: (Int) -> CellKind = { CellKind.COMPOSE },
)

private val ONE_OF_EACH = mapOf(
    1 to setOf(Flaw.UNLABELED),
    2 to setOf(Flaw.SMALL_TARGET),
    3 to setOf(Flaw.STATELESS),
)
private val ALTERNATING = mapOf(
    1 to setOf(Flaw.UNLABELED),
    2 to setOf(Flaw.UNLABELED),
    3 to setOf(Flaw.SMALL_TARGET, Flaw.STATELESS),
    4 to setOf(Flaw.SMALL_TARGET, Flaw.STATELESS),
)
private val evenComposeOddView: (Int) -> CellKind =
    { if (it % 2 == 0) CellKind.COMPOSE else CellKind.VIEW }

/** Launch with `--es scenario <id>` on InteropActivity (or MainActivity, which forwards). */
val INTEROP_SCENARIOS: List<InteropScenario> = listOf(
    InteropScenario("S1", "S1: RecyclerView of ComposeView cells", ONE_OF_EACH) { CellKind.COMPOSE },
    InteropScenario("S2", "S2: RecyclerView of classic View cells", ONE_OF_EACH) { CellKind.VIEW },
    InteropScenario(
        "S3", "S3: RecyclerView of mixed cells",
        // Rows cycle compose, view, hybrid: each kind gets every defect once.
        mapOf(
            0 to setOf(Flaw.UNLABELED),
            1 to setOf(Flaw.UNLABELED),
            2 to setOf(Flaw.UNLABELED),
            3 to setOf(Flaw.SMALL_TARGET, Flaw.STATELESS),
            4 to setOf(Flaw.SMALL_TARGET, Flaw.STATELESS),
            5 to setOf(Flaw.SMALL_TARGET, Flaw.STATELESS),
        ),
    ) { CellKind.entries[it % 3] },
    InteropScenario("S4", "S4: LazyColumn with AndroidView rows", ALTERNATING, evenComposeOddView),
    InteropScenario("S5", "S5: Compose > AndroidView > RecyclerView > cells", ALTERNATING, evenComposeOddView),
    InteropScenario("S6", "S6: RecyclerView grid of View cells", ONE_OF_EACH) { CellKind.VIEW },
    InteropScenario("D1", "D1: DialogFragment over a View screen"),
    InteropScenario("D2", "D2: Compose Dialog over a Compose screen"),
)

fun interopScenario(id: String?): InteropScenario? =
    INTEROP_SCENARIOS.firstOrNull { it.id.equals(id, ignoreCase = true) }

class InteropFragment : Fragment() {

    private val scenario: InteropScenario
        get() = interopScenario(requireArguments().getString(ARG_SCENARIO))
            ?: error("unknown interop scenario ${requireArguments().getString(ARG_SCENARIO)}")

    override fun onCreateView(
        inflater: LayoutInflater,
        container: ViewGroup?,
        savedInstanceState: Bundle?,
    ): View {
        val sc = scenario
        return when (sc.id) {
            "S1", "S2", "S3" -> recyclerScreen(sc, spanCount = 1)
            "S6" -> recyclerScreen(sc, spanCount = 2)
            "S4" -> composeScreen { LazyWithAndroidViews(sc) }
            "S5" -> composeScreen { NestedRecycler(sc) }
            "D1" -> dialogHostScreen(sc)
            "D2" -> composeScreen { ComposeDialogScreen(sc) }
            else -> error("unhandled interop scenario ${sc.id}")
        }
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        if (scenario.id == "D1" && savedInstanceState == null) showProbeDialog()
    }

    private fun showProbeDialog() {
        ProbeDialogFragment().show(childFragmentManager, "D1")
    }

    /** Classic root: a heading TextView over a RecyclerView (S1, S2, S3, S6). */
    private fun recyclerScreen(sc: InteropScenario, spanCount: Int): View {
        val ctx = requireContext()
        return classicColumn(sc.title).apply {
            addView(
                RecyclerView(ctx).apply {
                    id = R.id.interop_list
                    layoutManager =
                        if (spanCount > 1) GridLayoutManager(ctx, spanCount) else LinearLayoutManager(ctx)
                    adapter = CellAdapter(sc, compact = spanCount > 1)
                },
                LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f),
            )
        }
    }

    /** D1: a classic screen; the dialog itself is ProbeDialogFragment. */
    private fun dialogHostScreen(sc: InteropScenario): View {
        val ctx = requireContext()
        return classicColumn(sc.title).apply {
            addView(TextView(ctx).apply {
                text = "The dialog is its own window, centered on screen."
                setPadding(dp(16), 0, dp(16), dp(12))
            })
            addView(Button(ctx).apply {
                id = R.id.open_dialog
                text = "Open dialog"
                setOnClickListener { showProbeDialog() }
            })
        }
    }

    private fun classicColumn(title: String): LinearLayout {
        val ctx = requireContext()
        return LinearLayout(ctx).apply {
            id = R.id.interop_root
            orientation = LinearLayout.VERTICAL
            fitsSystemWindows = true
            addView(TextView(ctx).apply {
                id = R.id.interop_header
                text = title
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 20f)
                setPadding(dp(16), dp(12), dp(16), dp(12))
                ViewCompat.setAccessibilityHeading(this, true)
            })
        }
    }

    /** Compose root (S4, S5, D2). */
    private fun composeScreen(content: @Composable () -> Unit): View =
        ComposeView(requireContext()).apply {
            id = R.id.interop_root
            setContent { ProbeRoot { content() } }
        }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    companion object {
        private const val ARG_SCENARIO = "scenario"
        const val ITEM_COUNT = 30

        fun newInstance(scenario: String) = InteropFragment().apply {
            arguments = Bundle().apply { putString(ARG_SCENARIO, scenario) }
        }
    }
}

// ---------------------------------------------------------------------------
// RecyclerView adapter (S1, S2, S3, S5, S6)
// ---------------------------------------------------------------------------

class CellAdapter(
    private val scenario: InteropScenario,
    private val compact: Boolean = false,
) : RecyclerView.Adapter<RecyclerView.ViewHolder>() {

    override fun getItemCount() = InteropFragment.ITEM_COUNT

    override fun getItemViewType(position: Int) = scenario.kindAt(position).ordinal

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): RecyclerView.ViewHolder {
        val inflater = LayoutInflater.from(parent.context)
        return when (CellKind.entries[viewType]) {
            CellKind.COMPOSE -> ComposeHolder(ComposeView(parent.context).apply {
                id = R.id.compose_cell
                layoutParams = RecyclerView.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                )
            })
            CellKind.VIEW -> ViewRowHolder(inflater.inflate(R.layout.item_view_cell, parent, false))
            CellKind.HYBRID -> HybridHolder(inflater.inflate(R.layout.item_hybrid_cell, parent, false))
        }
    }

    override fun onBindViewHolder(holder: RecyclerView.ViewHolder, position: Int) {
        val flaws = scenario.flaws[position].orEmpty()
        when (holder) {
            is ComposeHolder -> holder.cv.setContent { ProbeRoot { ComposeCell(position, flaws) } }
            is ViewRowHolder -> bindViewCell(holder.itemView, position, flaws, compact)
            is HybridHolder -> bindHybridCell(holder.itemView, position, flaws)
        }
    }

    class ComposeHolder(val cv: ComposeView) : RecyclerView.ViewHolder(cv)
    class ViewRowHolder(v: View) : RecyclerView.ViewHolder(v)
    class HybridHolder(v: View) : RecyclerView.ViewHolder(v)
}

// ---------------------------------------------------------------------------
// Compose screens
// ---------------------------------------------------------------------------

@Composable
private fun Header(title: String) {
    Text(
        title,
        style = MaterialTheme.typography.titleLarge,
        modifier = Modifier
            .padding(horizontal = 16.dp, vertical = 12.dp)
            .semantics { heading() },
    )
}

/** S4: a Compose LazyColumn; odd items are AndroidView-wrapped classic rows. */
@Composable
private fun LazyWithAndroidViews(sc: InteropScenario) {
    LazyColumn(
        Modifier
            .fillMaxSize()
            .safeDrawingPadding()
            .testTag("s4_list"),
    ) {
        item { Header(sc.title) }
        items(
            count = InteropFragment.ITEM_COUNT,
            key = { it },
            contentType = { sc.kindAt(it) },
        ) { pos ->
            val flaws = sc.flaws[pos].orEmpty()
            if (sc.kindAt(pos) == CellKind.VIEW) {
                AndroidView(
                    factory = { ctx ->
                        LayoutInflater.from(ctx).inflate(R.layout.item_view_cell, null, false)
                    },
                    update = { v -> bindViewCell(v, pos, flaws) },
                    modifier = Modifier.fillMaxWidth(),
                )
            } else {
                ComposeCell(pos, flaws)
            }
        }
    }
}

/** S5: ComposeView -> AndroidView -> RecyclerView -> ComposeView / View cells. */
@Composable
private fun NestedRecycler(sc: InteropScenario) {
    Column(
        Modifier
            .fillMaxSize()
            .safeDrawingPadding(),
    ) {
        Header(sc.title)
        AndroidView(
            factory = { ctx ->
                RecyclerView(ctx).apply {
                    id = R.id.interop_list
                    layoutManager = LinearLayoutManager(ctx)
                    adapter = CellAdapter(sc)
                }
            },
            modifier = Modifier
                .fillMaxWidth()
                .weight(1f)
                .testTag("s5_recycler"),
        )
    }
}

/** D2: a Compose screen that opens a Compose Dialog (a separate window + AndroidComposeView). */
@Composable
private fun ComposeDialogScreen(sc: InteropScenario) {
    var open by remember { mutableStateOf(true) }
    Column(
        Modifier
            .fillMaxSize()
            .safeDrawingPadding(),
    ) {
        Header(sc.title)
        Text(
            "The dialog is its own window with its own AndroidComposeView.",
            Modifier.padding(horizontal = 16.dp),
        )
        M3Button(
            onClick = { open = true },
            modifier = Modifier
                .padding(16.dp)
                .testTag("open_dialog"),
        ) { Text("Open dialog") }
    }
    if (open) {
        Dialog(onDismissRequest = { open = false }) {
            // The dialog is a separate semantics owner: opt its tags in again.
            TagsAsResourceIds {
                Surface(shape = MaterialTheme.shapes.large, tonalElevation = 6.dp) {
                    Column(Modifier.padding(24.dp)) {
                        Text(
                            "Share item",
                            style = MaterialTheme.typography.titleLarge,
                            modifier = Modifier.semantics { heading() },
                        )
                        DialogIconButtons()
                        TextButton(
                            onClick = { open = false },
                            modifier = Modifier.testTag("dialog_close"),
                        ) { Text("Close") }
                    }
                }
            }
        }
    }
}

/** BAD `dialog_compose_unlabeled` (no label) next to GOOD `dialog_compose_labeled`. */
@Composable
fun DialogIconButtons() {
    Row {
        IconButton(onClick = { }, modifier = Modifier.testTag("dialog_compose_unlabeled")) {
            Icon(Icons.Filled.Share, contentDescription = null)
        }
        IconButton(onClick = { }, modifier = Modifier.testTag("dialog_compose_labeled")) {
            Icon(Icons.Filled.Favorite, contentDescription = "Add to favorites")
        }
    }
}

// ---------------------------------------------------------------------------
// D1 dialog
// ---------------------------------------------------------------------------

/** A floating dialog window mixing classic Views and a ComposeView (dialog_probe.xml). */
class ProbeDialogFragment : DialogFragment() {
    override fun onCreateView(
        inflater: LayoutInflater,
        container: ViewGroup?,
        savedInstanceState: Bundle?,
    ): View {
        val root = inflater.inflate(R.layout.dialog_probe, container, false)
        ViewCompat.setAccessibilityHeading(root.findViewById(R.id.dialog_title), true)
        root.findViewById<ComposeView>(R.id.dialog_compose).setContent {
            ProbeRoot { DialogIconButtons() }
        }
        root.findViewById<View>(R.id.dialog_view_unlabeled).setOnClickListener { }
        root.findViewById<View>(R.id.dialog_view_labeled).setOnClickListener { }
        root.findViewById<View>(R.id.dialog_close).setOnClickListener { dismiss() }
        return root
    }
}
