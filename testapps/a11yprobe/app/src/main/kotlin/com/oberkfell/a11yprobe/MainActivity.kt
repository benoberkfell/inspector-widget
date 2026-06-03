// ============================================================================
// MainActivity.kt — launcher + per-scenario host.
//
// A single ComponentActivity with state-based navigation (testapps.md §4):
//   * LauncherScreen is a LazyColumn over SCENARIOS (testTag launcher_list);
//     every row carries testTag launch_<id>.
//   * Tapping a row sets `selected` and the Scaffold renders that scenario's
//     content(); the top-bar back button (a labeled GOOD example) clears it.
//   * One extra row (launch_view_xml) starts ViewScenarioActivity, the
//     classic-View extraction path.
// This keeps the whole corpus reachable by a deterministic testTag path so a
// script can `am start` + tap-by-testTag and dump each scenario in turn.
// ============================================================================
package com.oberkfell.a11yprobe

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
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                AppRoot(
                    onOpenViewScreen = {
                        startActivity(Intent(this, ViewScenarioActivity::class.java))
                    }
                )
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun AppRoot(onOpenViewScreen: () -> Unit) {
    var selected by remember { mutableStateOf<Scenario?>(null) }
    var showAll by remember { mutableStateOf(false) }
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
                    onOpenViewScreen = onOpenViewScreen,
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
    onOpenViewScreen: () -> Unit,
    onShowAll: () -> Unit,
) {
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
                supportingContent = { Text(sc.rule) },
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
                    .clickable { onOpenViewScreen() }
                    .testTag("launch_view_xml")
            )
        }
    }
}

/**
 * Shared layout: a titled section with the GOOD variant on top and BAD below.
 * The section title itself is a real `heading()` so navigation between sections
 * is correct even when a scenario's BAD content omits its own heading.
 * The whole section scrolls so tall scenarios stay reachable.
 */
/**
 * When false (set by [AllScenarios]), a [Section] does NOT take fillMaxSize or own a
 * vertical scroll — so many Sections can be stacked inside one outer scroll for a
 * single "lint everything" pass. Default true for the normal per-scenario screen.
 */
val LocalSectionScroll = androidx.compose.runtime.compositionLocalOf { true }

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
