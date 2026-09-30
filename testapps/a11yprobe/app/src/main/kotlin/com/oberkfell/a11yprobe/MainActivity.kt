// ============================================================================
// MainActivity.kt — launcher + per-scenario host.
//
// Launch any scenario directly (package com.oberkfell.a11yprobe):
//
//   # Compose scenarios (ids in ScenarioRegistry.kt), or "all" for every one stacked:
//   adb shell am start -n com.oberkfell.a11yprobe/.MainActivity --es scenario icon_button
//   adb shell am start -n com.oberkfell.a11yprobe/.MainActivity --es scenario all
//
//   # Classic-View (XML) screen:
//   adb shell am start -n com.oberkfell.a11yprobe/.ViewScenarioActivity
//
//   # TalkBack navigation corpus, one variant per screen (TalkBackScenarios.kt):
//   adb shell am start -n com.oberkfell.a11yprobe/.MainActivity --es scenario tb_c1 --es variant bad
//   (tb_v* open TbViewActivity, tb_h* open InteropActivity; same extras)
//
//   # Mixed View/Compose screens and dialog windows (ids in InteropFragment.kt):
//   adb shell am start -n com.oberkfell.a11yprobe/.InteropActivity --es scenario S1
//     S1 RecyclerView of ComposeView cells     S4 LazyColumn with AndroidView rows
//     S2 RecyclerView of classic View cells    S5 Compose > AndroidView > RecyclerView > cells
//     S3 RecyclerView of mixed/hybrid cells    S6 RecyclerView grid of View cells
//     D1 DialogFragment (Views + ComposeView)  D2 Compose Dialog
//
// MainActivity also forwards "--es scenario view_xml" and the S*/D* ids to the
// right Activity, so `.MainActivity --es scenario <any id>` works for all of them
// (it finishes itself; prefer the direct component for scripted dumps).
// Add `-S` to force-stop a running instance first, `-W` to wait for launch.
//
// Without an extra it is a ComponentActivity with state-based navigation
// (testapps.md §4): LauncherScreen is a LazyColumn over SCENARIOS (testTag
// launcher_list); every row carries testTag launch_<id>. Tapping a row sets
// `selected` and the Scaffold renders that scenario's content(); the top-bar
// back button (a labeled GOOD example) clears it.
// ============================================================================
package com.oberkfell.a11yprobe

import android.content.Context
import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val requested = intent.getStringExtra(EXTRA_SCENARIO)
        forwardIntent(this, requested)?.let {
            startActivity(it.putExtra(EXTRA_VARIANT, intent.getStringExtra(EXTRA_VARIANT)))
            finish()
            return
        }
        // TalkBack corpus (TalkBackScenarios.kt): one scenario variant per screen.
        tbScenario(requested)?.let { sc ->
            setContent { ProbeRoot { TbScreen(sc, intent.getStringExtra(EXTRA_VARIANT)) } }
            return
        }
        setContent {
            ProbeRoot {
                AppRoot(
                    initial = SCENARIOS.firstOrNull { it.id == requested },
                    initialShowAll = requested == SCENARIO_ALL,
                )
            }
        }
    }

    companion object {
        const val EXTRA_SCENARIO = "scenario"
        const val SCENARIO_ALL = "all"
        const val SCENARIO_VIEW_XML = "view_xml"
        /** TalkBack corpus: `--es variant bad|good` (TalkBackScenarios.kt). */
        const val EXTRA_VARIANT = "variant"

        /** The Activity intent for a non-Compose scenario id, or null if MainActivity shows it. */
        fun forwardIntent(context: Context, id: String?): Intent? = when {
            id == SCENARIO_VIEW_XML -> Intent(context, ViewScenarioActivity::class.java)
            interopScenario(id) != null -> Intent(context, InteropActivity::class.java)
                .putExtra(InteropActivity.EXTRA_SCENARIO, interopScenario(id)!!.id)
            tbViewScenario(id) != null -> Intent(context, TbViewActivity::class.java)
                .putExtra(EXTRA_SCENARIO, tbViewScenario(id)!!.id)
            else -> null
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun AppRoot(initial: Scenario?, initialShowAll: Boolean) {
    var selected by remember { mutableStateOf(initial) }
    var showAll by remember { mutableStateOf(initialShowAll) }
    val current = selected
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(if (showAll) "All scenarios" else current?.title ?: "A11yProbe") },
                navigationIcon = {
                    if (current != null || showAll) {
                        IconButton(
                            onClick = { selected = null; showAll = false },
                            modifier = Modifier.testTag("nav_back")
                        ) {
                            // GOOD example: a labeled navigation icon.
                            Icon(
                                Icons.AutoMirrored.Filled.ArrowBack,
                                contentDescription = "Back to scenario list"
                            )
                        }
                    }
                }
            )
        }
    ) { pad ->
        Box(Modifier.padding(pad)) {
            when {
                showAll -> AllScenarios()
                current == null -> LauncherScreen(
                    onPick = { selected = it },
                    onShowAll = { showAll = true },
                )
                else -> current.content()
            }
        }
    }
}

/** Every scenario's content stacked in one scroll, for a single "lint everything" pass. */
@Composable
fun AllScenarios() {
    androidx.compose.runtime.CompositionLocalProvider(LocalSectionScroll provides false) {
        Column(
            Modifier
                .fillMaxSize()
                .verticalScroll(rememberScrollState())
                .testTag("all_scenarios")
        ) {
            for (sc in SCENARIOS) {
                Box(Modifier.testTag("all_${sc.id}")) { sc.content() }
                HorizontalDivider()
            }
        }
    }
}

@Composable
private fun LauncherScreen(
    onPick: (Scenario) -> Unit,
    onShowAll: () -> Unit,
) {
    val context = LocalContext.current
    LazyColumn(
        Modifier
            .fillMaxSize()
            .testTag("launcher_list")
    ) {
        item {
            ListItem(
                headlineContent = { Text("▶ All scenarios (lint everything)") },
                supportingContent = { Text("every BAD/GOOD variant on one scrollable screen") },
                modifier = Modifier
                    .clickable { onShowAll() }
                    .testTag("launch_all")
            )
            HorizontalDivider()
        }
        items(SCENARIOS, key = { it.id }) { sc ->
            ListItem(
                headlineContent = { Text(sc.title) },
                supportingContent = { Text(sc.lintRule?.let { "${sc.rule} · $it" } ?: sc.rule) },
                modifier = Modifier
                    .clickable { onPick(sc) }
                    .testTag("launch_${sc.id}")
            )
            HorizontalDivider()
        }
        item {
            ListItem(
                headlineContent = { Text("Classic View screen (XML)") },
                supportingContent = { Text("exercises the View extraction path") },
                modifier = Modifier
                    .clickable {
                        MainActivity.forwardIntent(context, MainActivity.SCENARIO_VIEW_XML)
                            ?.let(context::startActivity)
                    }
                    .testTag("launch_view_xml")
            )
            HorizontalDivider()
        }
        items(INTEROP_SCENARIOS, key = { it.id }) { sc ->
            ListItem(
                headlineContent = { Text(sc.title) },
                supportingContent = { Text("mixed View/Compose hierarchy") },
                modifier = Modifier
                    .clickable {
                        MainActivity.forwardIntent(context, sc.id)?.let(context::startActivity)
                    }
                    .testTag("launch_${sc.id}")
            )
            HorizontalDivider()
        }
    }
}

/**
 * When false (set by [AllScenarios]), a [Section] does NOT take fillMaxSize or own a
 * vertical scroll — so many Sections can be stacked inside one outer scroll for a
 * single "lint everything" pass. Default true for the normal per-scenario screen.
 */
val LocalSectionScroll = androidx.compose.runtime.compositionLocalOf { true }

/**
 * Shared layout: a titled section with the GOOD variant on top and BAD below.
 * The section title itself is a real `heading()` so navigation between sections
 * is correct even when a scenario's BAD content omits its own heading.
 * The whole section scrolls so tall scenarios stay reachable.
 */
@Composable
fun Section(
    title: String,
    good: @Composable () -> Unit,
    bad: @Composable () -> Unit,
) {
    val scroll = LocalSectionScroll.current
    Column(
        (if (scroll) Modifier.fillMaxSize().verticalScroll(rememberScrollState())
         else Modifier.fillMaxWidth())
            .padding(16.dp)
    ) {
        Text(
            title,
            style = MaterialTheme.typography.titleMedium,
            modifier = Modifier
                .semantics { heading() }
                .testTag("section_title")
        )
        Spacer(Modifier.height(12.dp))
        Text("GOOD", style = MaterialTheme.typography.labelLarge)
        Spacer(Modifier.height(8.dp))
        good()
        Spacer(Modifier.height(24.dp))
        Text("BAD", style = MaterialTheme.typography.labelLarge)
        Spacer(Modifier.height(8.dp))
        bad()
    }
}
