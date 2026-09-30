// ============================================================================
// TalkBackScenarios.kt — the TalkBack navigation corpus (Compose half).
//
// Each scenario is one TalkBack navigation defect, with the BAD and the GOOD
// variant on SEPARATE screens (a walk covers one whole screen):
//
//   adb shell am start -S -W -n com.oberkfell.a11yprobe/.MainActivity \
//       --es scenario tb_c1 --es variant bad        # or good (some have bad_b)
//
// Views (tb_v*) live in TbViewActivity and hybrids (tb_h*) in InteropActivity;
// MainActivity forwards those ids. The host's expectations for every scenario
// and variant are in host/tests/data/tb_corpus_expected.json, and
// host/tests/test_device_talkback.py walks them (design:
// docs/design/talkback-navigation.md part 5). Several mirror bugs seen in real
// apps: C1 (a clickable row with its own Checkbox), C14 (Now in Android:
// Interests -> topic -> back lands on "Search"), C15 (Now in Android's
// For-you grid of partially visible cards), C16 (AntennaPod: a pager whose
// offscreen page holds a WebView).
// ============================================================================
@file:OptIn(ExperimentalMaterial3Api::class, ExperimentalFoundationApi::class)

package com.oberkfell.a11yprobe

import android.webkit.WebView
import androidx.activity.compose.BackHandler
import androidx.compose.animation.AnimatedContent
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.Orientation
import androidx.compose.foundation.gestures.draggable
import androidx.compose.foundation.gestures.rememberDraggableState
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.itemsIndexed as gridItemsIndexed
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.pager.HorizontalPager
import androidx.compose.foundation.pager.rememberPagerState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Favorite
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.BottomSheetScaffold
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.Checkbox
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.SheetValue
import androidx.compose.material3.SwipeToDismissBox
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TextField
import androidx.compose.material3.rememberBottomSheetScaffoldState
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.material3.rememberStandardBottomSheetState
import androidx.compose.material3.rememberSwipeToDismissBoxState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.CustomAccessibilityAction
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.customActions
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.isTraversalGroup
import androidx.compose.ui.semantics.paneTitle
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.semantics.traversalIndex
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.ui.window.Dialog
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlin.math.roundToInt

/**
 * One TalkBack navigation scenario.
 *
 * @param id       `--es scenario <id>`; nodes carry testTags `<id>_*`.
 * @param name     the design's short name (docs/design/talkback-navigation.md part 5).
 * @param variants the `--es variant` values it understands; "bad" and "good" at least.
 * @param screen   renders one variant as a whole screen.
 */
class TbScenario(
    val id: String,
    val name: String,
    val title: String,
    val variants: List<String> = listOf("bad", "good"),
    val screen: @Composable (String) -> Unit,
)

val TB_SCENARIOS: List<TbScenario> = listOf(
    TbScenario("tb_c1", "double_stop_card", "C1 Card with its own Checkbox") { C1DoubleStop(it) },
    TbScenario("tb_c2", "two_column", "C2 Two columns of cards") { C2TwoColumns(it) },
    TbScenario("tb_c3", "traversal_index", "C3 traversalIndex on the title") { C3TraversalIndex(it) },
    TbScenario("tb_c4", "fake_dialog", "C4 A dialog drawn in the screen") { C4FakeDialog(it) },
    TbScenario("tb_c5", "sheet", "C5 A bottom sheet over content") { C5Sheet(it) },
    TbScenario("tb_c6", "list_update", "C6 A list that updates", listOf("bad", "bad_b", "good")) { C6ListUpdate(it) },
    TbScenario("tb_c7", "drag_scroller", "C7 Content moved by a drag") { C7DragScroller(it) },
    TbScenario("tb_c8", "pager_edge", "C8 A pager's next page") { C8PagerEdge(it) },
    TbScenario("tb_c9", "merged_text", "C9 A merged row's reading order") { C9MergedText(it) },
    TbScenario("tb_c10", "checkbox_state", "C10 An unchecked Checkbox") { C10CheckboxState(it) },
    TbScenario("tb_c11", "swipe_dismiss", "C11 Swipe to delete") { C11SwipeDismiss(it) },
    TbScenario("tb_c12", "focus_steal", "C12 A field that takes focus") { C12FocusSteal(it) },
    TbScenario("tb_c13", "dialog_initial", "C13 A dialog's first focus") { C13DialogInitial(it) },
    TbScenario("tb_c14", "nav_restore", "C14 Back to a list") { C14NavRestore(it) },
    TbScenario("tb_c15", "grid_partial", "C15 A grid of partly visible cards") { C15Grid(it) },
    TbScenario("tb_c16", "pager_webview", "C16 A pager with a WebView page") { C16PagerWebView(it) },
)

fun tbScenario(id: String?): TbScenario? = TB_SCENARIOS.firstOrNull { it.id.equals(id, ignoreCase = true) }

/** The screen for one scenario and variant (an unknown variant falls back to "bad"). */
@Composable
fun TbScreen(sc: TbScenario, variant: String?) {
    val v = variant?.lowercase()?.takeIf { it in sc.variants } ?: "bad"
    sc.screen(v)
}

@Composable
private fun TbTitle(text: String) {
    Text(
        text,
        style = MaterialTheme.typography.titleLarge,
        modifier = Modifier
            .padding(bottom = 8.dp)
            .semantics { heading() }
            .testTag("tb_title"),
    )
}

/** A whole-screen column with the scenario's heading at the top. */
@Composable
private fun TbColumn(
    title: String,
    scroll: Boolean = false,
    content: @Composable ColumnScope.() -> Unit,
) {
    Column(
        Modifier
            .fillMaxSize()
            .safeDrawingPadding()
            .then(if (scroll) Modifier.verticalScroll(rememberScrollState()) else Modifier)
            .padding(16.dp),
    ) {
        TbTitle(title)
        content()
    }
}

private fun label(variant: String) = variant.uppercase().replace('_', ' ')

// ---------------------------------------------------------------------------
// C1 double_stop_card: a clickable card with its own Checkbox handler = two stops
// per card (BAD). GOOD: the row is the one toggleable stop; the Checkbox has no
// handler of its own.
// ---------------------------------------------------------------------------
@Composable
private fun C1DoubleStop(variant: String) = TbColumn("C1 ${label(variant)}: orders") {
    val checked = remember { mutableStateListOf(false, true, false) }
    for (i in 0 until 3) {
        if (variant == "bad") {
            Card(
                onClick = {},
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(vertical = 6.dp)
                    .testTag("tb_c1_card_$i"),
            ) {
                Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
                    Text("Order ${i + 1}", Modifier.weight(1f))
                    Checkbox(checked = checked[i], onCheckedChange = { checked[i] = it })
                }
            }
        } else {
            Row(
                Modifier
                    .fillMaxWidth()
                    .padding(vertical = 6.dp)
                    .toggleable(value = checked[i], role = Role.Checkbox, onValueChange = { checked[i] = it })
                    .padding(16.dp)
                    .testTag("tb_c1_row_$i"),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text("Order ${i + 1}", Modifier.weight(1f))
                Checkbox(checked = checked[i], onCheckedChange = null)
            }
        }
    }
}

// ---------------------------------------------------------------------------
// C2 two_column: two columns of cards whose rows overlap; Compose sorts by rows,
// so BAD reads A1 B1 A2 B2 (zig-zag). GOOD: each column is a traversal group.
// ---------------------------------------------------------------------------
@Composable
private fun C2TwoColumns(variant: String) = TbColumn("C2 ${label(variant)}: products") {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        for ((col, heights) in listOf("A" to listOf(120, 160, 100), "B" to listOf(100, 140, 180))) {
            Column(
                Modifier
                    .weight(1f)
                    .then(if (variant == "good") Modifier.semantics { isTraversalGroup = true } else Modifier)
                    .testTag("tb_c2_col_$col"),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                heights.forEachIndexed { i, h ->
                    Card(onClick = {}, modifier = Modifier.fillMaxWidth().height(h.dp)) {
                        Text("Product $col${i + 1}", Modifier.padding(12.dp))
                    }
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// C3 traversal_index: traversalIndex = 1f on a title, sorted over the whole level,
// reads it last (BAD). GOOD: the ordering lives inside a traversal group.
// ---------------------------------------------------------------------------
@Composable
private fun C3TraversalIndex(variant: String) = TbColumn("C3 ${label(variant)}: inbox") {
    if (variant == "bad") {
        Text("Inbox, 3 unread", Modifier.semantics { traversalIndex = 1f }.testTag("tb_c3_header"))
        for (m in listOf("Invoice", "Lunch", "Report")) {
            Button(onClick = {}, Modifier.fillMaxWidth()) { Text("Open $m") }
        }
    } else {
        Column(Modifier.semantics { isTraversalGroup = true }) {
            Text("Inbox, 3 unread", Modifier.semantics { traversalIndex = -1f }.testTag("tb_c3_header"))
            for (m in listOf("Invoice", "Lunch", "Report")) {
                Button(onClick = {}, Modifier.fillMaxWidth()) { Text("Open $m") }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// C4 fake_dialog: a "dialog" drawn in the same composition over a list, with a
// clickable (tap-outside-to-dismiss) scrim. BAD: next from the dialog walks into
// the list behind it, and the scrim is an unlabelled stop. GOOD: a real Dialog
// window, which TalkBack cannot leave.
// ---------------------------------------------------------------------------
@Composable
private fun C4FakeDialog(variant: String) {
    Box(Modifier.fillMaxSize()) {
        TbColumn("C4 ${label(variant)}: drafts") {
            for (i in 1..8) {
                Button(onClick = {}, Modifier.fillMaxWidth()) { Text("Draft $i") }
            }
        }
        if (variant == "bad") {
            Box(
                Modifier
                    .fillMaxSize()
                    .background(Color(0x99000000))
                    .clickable(interactionSource = remember { MutableInteractionSource() }, indication = null) {}
                    .testTag("tb_c4_scrim"),
            )
            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { DeleteDraftCard() }
        } else {
            Dialog(onDismissRequest = {}) { TagsAsResourceIds { DeleteDraftCard() } }
        }
    }
}

@Composable
private fun DeleteDraftCard() {
    Card(Modifier.padding(32.dp).testTag("tb_c4_dialog")) {
        Column(Modifier.padding(24.dp)) {
            Text("Delete draft?", style = MaterialTheme.typography.titleLarge, modifier = Modifier.semantics { heading() })
            Text("This cannot be undone.", Modifier.padding(vertical = 8.dp))
            Row {
                TextButton(onClick = {}) { Text("Cancel") }
                TextButton(onClick = {}) { Text("Delete") }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// C5 sheet: BAD a standard BottomSheetScaffold sheet, expanded over the content
// in the same window. GOOD an M3 ModalBottomSheet (its own window).
// ---------------------------------------------------------------------------
@Composable
private fun C5Sheet(variant: String) {
    val content: @Composable () -> Unit = {
        TbColumn("C5 ${label(variant)}: results") {
            for (i in 1..8) Button(onClick = {}, Modifier.fillMaxWidth()) { Text("Result $i") }
        }
    }
    if (variant == "bad") {
        BottomSheetScaffold(
            scaffoldState = rememberBottomSheetScaffoldState(
                bottomSheetState = rememberStandardBottomSheetState(initialValue = SheetValue.Expanded),
            ),
            sheetContent = { FilterSheet() },
            sheetPeekHeight = 0.dp,
        ) { content() }
    } else {
        content()
        ModalBottomSheet(onDismissRequest = {}, sheetState = rememberModalBottomSheetState()) {
            TagsAsResourceIds { FilterSheet() }
        }
    }
}

@Composable
private fun FilterSheet() {
    Column(Modifier.padding(24.dp).testTag("tb_c5_sheet")) {
        Text("Filters", style = MaterialTheme.typography.titleLarge, modifier = Modifier.semantics { heading() })
        val on = remember { mutableStateListOf(true, false, false) }
        listOf("In stock", "On sale", "Free shipping").forEachIndexed { i, f ->
            Row(
                Modifier
                    .fillMaxWidth()
                    .toggleable(value = on[i], role = Role.Checkbox, onValueChange = { on[i] = it })
                    .padding(vertical = 8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Checkbox(checked = on[i], onCheckedChange = null)
                Text(f)
            }
        }
        Button(onClick = {}) { Text("Apply") }
    }
}

// ---------------------------------------------------------------------------
// C6 list_update: 30 messages that TB_PROBE changes. GOOD: stable keys, so the
// focused row survives an insert. BAD: no keys (positions are identity: after
// insert_top every row shows its predecessor). BAD_B: keys minted per refresh
// ("$gen-$id"), so refresh re-creates every row.
// ---------------------------------------------------------------------------
private data class Msg(val id: Int, val text: String)

@Composable
private fun C6ListUpdate(variant: String) {
    val items = remember { mutableStateListOf<Msg>().apply { addAll((1..30).map { Msg(it, "Message $it") }) } }
    var gen by remember { mutableIntStateOf(0) }
    var next by remember { mutableIntStateOf(100) }
    DisposableEffect(Unit) {
        val stop = TbProbe.listen { a ->
            when (a.action) {
                "insert_top" -> items.add(0, Msg(next++, "New message ${next - 100}"))
                "shuffle" -> items.shuffle()
                "change_item" -> items.getOrNull(a.index)?.let { items[a.index] = it.copy(text = it.text + " (edited)") }
                "notify_all", "refresh" -> gen++
            }
        }
        onDispose { stop() }
    }
    Column(Modifier.fillMaxSize().safeDrawingPadding().padding(16.dp)) {
        TbTitle("C6 ${label(variant)}: messages")
        LazyColumn(Modifier.fillMaxSize().testTag("tb_c6_list")) {
            when (variant) {
                "good" -> items(items, key = { it.id }) { MessageRow(it) }
                "bad_b" -> items(items, key = { "$gen-${it.id}" }) { MessageRow(it) }
                else -> items(items) { MessageRow(it) }
            }
        }
    }
}

@Composable
private fun MessageRow(m: Msg) {
    Text(
        m.text,
        Modifier
            .fillMaxWidth()
            .clickable {}
            .padding(16.dp),
    )
}

// ---------------------------------------------------------------------------
// C7 drag_scroller: BAD rows moved by a drag gesture and an offset, with no
// scroll semantics: the rows past the bottom never get reached. GOOD verticalScroll.
// ---------------------------------------------------------------------------
@Composable
private fun C7DragScroller(variant: String) {
    if (variant == "good") {
        TbColumn("C7 GOOD: log", scroll = true) { LogRows() }
        return
    }
    var offset by remember { mutableStateOf(0f) }
    Column(Modifier.fillMaxSize().safeDrawingPadding().padding(16.dp)) {
        TbTitle("C7 BAD: log")
        Box(
            Modifier
                .fillMaxSize()
                .draggable(
                    orientation = Orientation.Vertical,
                    state = rememberDraggableState { d -> offset = (offset + d).coerceAtMost(0f) },
                )
                .testTag("tb_c7_drag"),
        ) {
            Column(Modifier.offset { IntOffset(0, offset.roundToInt()) }) { LogRows() }
        }
    }
}

@Composable
private fun LogRows() {
    for (i in 1..30) Text("Log entry $i", Modifier.fillMaxWidth().height(72.dp).padding(8.dp))
}

// ---------------------------------------------------------------------------
// C8 pager_edge: a HorizontalPager is a PAGER to TalkBack, which never
// auto-scrolls one: BAD next from page 1's last button leaves the pager and
// pages 2-3 are unreachable by swipe. GOOD visible tabs switch pages, and the
// pager itself does not scroll by gesture.
// ---------------------------------------------------------------------------
@Composable
private fun C8PagerEdge(variant: String) = TbColumn("C8 ${label(variant)}: albums") {
    val pager = rememberPagerState { 3 }
    val scope = rememberCoroutineScope()
    if (variant == "good") {
        TabRow(selectedTabIndex = pager.currentPage) {
            for (p in 0 until 3) {
                Tab(selected = pager.currentPage == p, onClick = { scope.launch { pager.animateScrollToPage(p) } },
                    text = { Text("Album ${p + 1}") })
            }
        }
    }
    HorizontalPager(
        state = pager,
        userScrollEnabled = variant != "good",
        modifier = Modifier.fillMaxWidth().height(260.dp).testTag("tb_c8_pager"),
    ) { page ->
        Column(Modifier.fillMaxSize().padding(16.dp)) {
            Text("Album ${page + 1}", style = MaterialTheme.typography.titleMedium)
            Button(onClick = {}) { Text("Play album ${page + 1}") }
            Button(onClick = {}) { Text("Share album ${page + 1}") }
        }
    }
    Button(onClick = {}, Modifier.fillMaxWidth()) { Text("Done") }
}

// ---------------------------------------------------------------------------
// C9 merged_text: BAD a clickable row whose price Text is composed before the
// title but placed at the end: the merged row reads "$5, Socks". GOOD one label
// in reading order.
// ---------------------------------------------------------------------------
@Composable
private fun C9MergedText(variant: String) = TbColumn("C9 ${label(variant)}: cart") {
    for ((name, price) in listOf("Socks" to "$5", "Scarf" to "$18")) {
        Box(
            Modifier
                .fillMaxWidth()
                .clickable {}
                .padding(16.dp)
                .then(if (variant == "good") Modifier.clearAndSetSemantics { contentDescription = "$name, $price" } else Modifier)
                .testTag("tb_c9_row"),
        ) {
            Text(price, Modifier.align(Alignment.CenterEnd))
            Text(name, Modifier.align(Alignment.CenterStart))
        }
    }
}

// ---------------------------------------------------------------------------
// C10 checkbox_state (calibration): TalkBack 16.2 says no "not checked" for an
// unchecked Compose Checkbox without a stateDescription. GOOD sets one.
// ---------------------------------------------------------------------------
@Composable
private fun C10CheckboxState(variant: String) = TbColumn("C10 ${label(variant)}: newsletter") {
    var checked by remember { mutableStateOf(false) }
    Row(
        Modifier
            .fillMaxWidth()
            .toggleable(value = checked, role = Role.Checkbox, onValueChange = { checked = it })
            .then(if (variant == "good") Modifier.semantics {
                stateDescription = if (checked) "Subscribed" else "Not subscribed"
            } else Modifier)
            .padding(16.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Checkbox(checked = checked, onCheckedChange = null)
        Text("Weekly newsletter")
    }
}

// ---------------------------------------------------------------------------
// C11 swipe_dismiss (static only): BAD rows deleted by a swipe TalkBack cannot
// make. GOOD the same rows with a "Delete" custom action.
// ---------------------------------------------------------------------------
@Composable
private fun C11SwipeDismiss(variant: String) = TbColumn("C11 ${label(variant)}: reminders") {
    val rows = remember { mutableStateListOf("Water plants", "Call Ada", "Pay rent") }
    for (r in rows.toList()) {
        key(r) {
            val state = rememberSwipeToDismissBoxState(confirmValueChange = { rows.remove(r); true })
            SwipeToDismissBox(
                state = state,
                backgroundContent = { Icon(Icons.Filled.Delete, contentDescription = null) },
                modifier = if (variant == "good") Modifier.semantics {
                    customActions = listOf(CustomAccessibilityAction("Delete") { rows.remove(r); true })
                } else Modifier,
            ) {
                Card(Modifier.fillMaxWidth()) { Text(r, Modifier.padding(16.dp)) }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// C12 focus_steal: BAD a LaunchedEffect keyed on a ticking value keeps calling
// requestFocus() on the field, which pulls TalkBack back to it every 2s. GOOD
// asks once.
// ---------------------------------------------------------------------------
@Composable
private fun C12FocusSteal(variant: String) = TbColumn("C12 ${label(variant)}: search") {
    val fr = remember { FocusRequester() }
    var text by remember { mutableStateOf("") }
    var tick by remember { mutableIntStateOf(0) }
    if (variant == "bad") {
        LaunchedEffect(Unit) { while (true) { delay(2000); tick++ } }
        LaunchedEffect(tick) { fr.requestFocus() }
    } else {
        LaunchedEffect(Unit) { fr.requestFocus() }
    }
    TextField(value = text, onValueChange = { text = it }, label = { Text("Search") },
        modifier = Modifier.fillMaxWidth().focusRequester(fr).testTag("tb_c12_field"))
    for (s in listOf("Recent: shoes", "Recent: lamps", "Recent: desks", "Recent: chairs", "Recent: rugs")) {
        TextButton(onClick = {}, Modifier.fillMaxWidth()) { Text(s) }
    }
}

// ---------------------------------------------------------------------------
// C13 dialog_initial: BAD a Dialog whose first child is an unlabelled close
// button (and no window title): TalkBack's first focus is that button. GOOD an
// AlertDialog with a title, text and labelled buttons.
// ---------------------------------------------------------------------------
@Composable
private fun C13DialogInitial(variant: String) = TbColumn("C13 ${label(variant)}: files") {
    var open by remember { mutableStateOf(false) }
    Text("report.pdf")
    Button(onClick = { open = true }, Modifier.testTag("tb_c13_open")) { Text("Rename file") }
    if (open && variant == "bad") {
        Dialog(onDismissRequest = { open = false }) {
            TagsAsResourceIds {
                Card {
                    Column(Modifier.padding(16.dp)) {
                        IconButton(onClick = { open = false }) { Icon(Icons.Filled.Close, contentDescription = null) }
                        Text("Rename report.pdf", style = MaterialTheme.typography.titleLarge)
                        var name by remember { mutableStateOf("report.pdf") }
                        TextField(value = name, onValueChange = { name = it })
                        Button(onClick = { open = false }) { Text("Save") }
                    }
                }
            }
        }
    } else if (open) {
        AlertDialog(
            onDismissRequest = { open = false },
            title = { Text("Rename report.pdf") },
            text = { Text("Pick a new name for the file.") },
            confirmButton = { TextButton(onClick = { open = false }) { Text("Save") } },
            dismissButton = { TextButton(onClick = { open = false }) { Text("Cancel") } },
        )
    }
}

// ---------------------------------------------------------------------------
// C14 nav_restore (Now in Android: Interests -> topic -> back lands on "Search"):
// two destinations in one Activity. BAD the list's state lives inside the
// destination (so it is re-created) and no destination has a pane title:
// TalkBack has no record to restore and starts over at the top bar. GOOD the
// list state is hoisted and each destination is a pane with its own title.
// ---------------------------------------------------------------------------
@Composable
private fun C14NavRestore(variant: String) {
    var topic by rememberSaveable { mutableStateOf<Int?>(null) }
    BackHandler(enabled = topic != null) { topic = null }
    val hoisted = rememberLazyListState()
    Column(Modifier.fillMaxSize().safeDrawingPadding()) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            TbTitle(if (topic == null) "C14 ${label(variant)}: interests" else "Topic ${topic!! + 1}")
            Spacer(Modifier.weight(1f))
            IconButton(onClick = {}) { Icon(Icons.Filled.Search, contentDescription = "Search") }
        }
        AnimatedContent(targetState = topic, label = "tb_c14_nav") { t ->
            val pane = if (variant == "good") Modifier.semantics {
                paneTitle = if (t == null) "Interests" else "Topic ${t + 1}"
            } else Modifier
            if (t == null) {
                val state: LazyListState = if (variant == "good") hoisted else rememberLazyListState()
                LazyColumn(Modifier.fillMaxSize().then(pane).testTag("tb_c14_list"), state = state) {
                    items(30, key = { it }) { i ->
                        Text("Topic ${i + 1}", Modifier.fillMaxWidth().clickable { topic = i }.padding(16.dp))
                    }
                }
            } else {
                Column(Modifier.fillMaxSize().then(pane).padding(16.dp)) {
                    Text("Topic ${t + 1}", style = MaterialTheme.typography.headlineSmall, modifier = Modifier.semantics { heading() })
                    Text("Follow this topic to see its news in For you.")
                    Button(onClick = {}) { Text("Follow") }
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// C15 grid_partial (Now in Android's For-you feed): a two-column grid of
// clickable news cards of mixed heights, each with its own bookmark button, so
// cards are often half on screen. BAD as NiA ships it. GOOD each card is one
// traversal group of equal height.
// ---------------------------------------------------------------------------
@Composable
private fun C15Grid(variant: String) = Column(Modifier.fillMaxSize().safeDrawingPadding().padding(16.dp)) {
    TbTitle("C15 ${label(variant)}: for you")
    LazyVerticalGrid(
        columns = GridCells.Fixed(2),
        modifier = Modifier.fillMaxSize().testTag("tb_c15_grid"),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        gridItemsIndexed((1..16).toList(), key = { _, n -> n }) { i, n ->
            val h = if (variant == "good") 220 else if (i % 3 == 0) 300 else 200
            Card(
                onClick = {},
                modifier = Modifier
                    .fillMaxWidth()
                    .height(h.dp)
                    .then(if (variant == "good") Modifier.semantics { isTraversalGroup = true } else Modifier),
            ) {
                Column(Modifier.padding(12.dp)) {
                    Text("News $n", style = MaterialTheme.typography.titleMedium)
                    Text("Story $n in brief.")
                    IconButton(onClick = {}) { Icon(Icons.Filled.Favorite, contentDescription = "Bookmark news $n") }
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// C16 pager_webview (AntennaPod): a pager keeps its neighbouring page, a WebView
// of show notes, composed off screen. BAD the offscreen WebView stays in the
// accessibility tree, so TalkBack walks into content nobody can see. GOOD pages
// that are not showing are hidden from accessibility.
// ---------------------------------------------------------------------------
@Composable
private fun C16PagerWebView(variant: String) = TbColumn("C16 ${label(variant)}: episode") {
    val pager = rememberPagerState { 3 }
    HorizontalPager(
        state = pager,
        beyondViewportPageCount = 1,
        modifier = Modifier.fillMaxWidth().height(420.dp).testTag("tb_c16_pager"),
    ) { page ->
        val hidden = variant == "good" && page != pager.currentPage
        when (page) {
            1 -> AndroidView(
                factory = { ctx ->
                    WebView(ctx).apply {
                        loadDataWithBaseURL(null, SHOW_NOTES_HTML, "text/html", "utf-8", null)
                    }
                },
                update = { wv ->
                    wv.importantForAccessibility = if (hidden)
                        android.view.View.IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS
                    else android.view.View.IMPORTANT_FOR_ACCESSIBILITY_AUTO
                },
                modifier = Modifier.fillMaxSize().testTag("tb_c16_webview"),
            )
            else -> Column(
                Modifier
                    .fillMaxSize()
                    .padding(16.dp)
                    .then(if (hidden) Modifier.clearAndSetSemantics {} else Modifier),
            ) {
                Text(if (page == 0) "Episode 12: Accessibility" else "Chapters", style = MaterialTheme.typography.titleMedium)
                Button(onClick = {}) { Text(if (page == 0) "Play episode" else "Chapter 1") }
            }
        }
    }
    Button(onClick = {}, Modifier.fillMaxWidth()) { Text("Download") }
}

const val SHOW_NOTES_HTML = """<html><body>
<h2>Show notes</h2>
<p>In this episode we test screen readers.</p>
<p><a href="https://example.com/a">Link one</a></p>
<p><a href="https://example.com/b">Link two</a></p>
</body></html>"""
