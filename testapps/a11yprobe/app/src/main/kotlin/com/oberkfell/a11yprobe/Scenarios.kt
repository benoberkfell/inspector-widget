// ============================================================================
// Scenarios.kt — each function renders the GOOD and BAD variant of one lint
// rule. Every interesting node carries a testTag good_<rule> / bad_<rule> so
// the host can pinpoint it (testTagsAsResourceId is on, see ProbeRoot.kt, so
// the tags are also the a11y viewIdResourceName) and crop its per-component
// image (testapps.md §3).
//
// The BAD variants are DELIBERATE accessibility defects. They are the corpus the
// host lint (inspector_widget.a11y_lint, R1..R12) is validated against, so do not
// "fix" them: each GOOD variant already shows the fix. The host golden test
// (host/tests/test_device_a11y_golden.py) asserts that every BAD node is flagged
// with the rule id named in its comment and that the GOOD node is not.
//
// Design rationale (testapps.md §0): a lint rule fires on the PRESENCE/ABSENCE
// of a semantics key (ContentDescription, Role, ToggleableState, ...). Each pair
// is built so the GOOD variant emits the key and the BAD variant omits it, and
// otherwise the two are as alike as possible (BAD labels differ from GOOD labels
// so the cross-node duplicate-label rule never pairs them). Touch target and
// contrast are not pure semantics: touch target is measured from the node's
// bounds and contrast is sampled from the rendered pixels.
// ============================================================================
package com.oberkfell.a11yprobe

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Favorite
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.disabled
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.isTraversalGroup
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.semantics.traversalIndex
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

// ---------------------------------------------------------------------------
// 1. MissingContentDescription — IconButton with vs without contentDescription.
// BAD -> a11y.label.missing (R1): TalkBack announces an unlabeled "Button".
// ---------------------------------------------------------------------------
@Composable
fun IconButtonScenario() = Section(
    "IconButton contentDescription",
    good = {
        IconButton(onClick = {}, modifier = Modifier.testTag("good_icon_button")) {
            Icon(Icons.Filled.Favorite, contentDescription = "Add to favorites")
        }
    },
    bad = {
        IconButton(onClick = {}, modifier = Modifier.testTag("bad_icon_button")) {
            Icon(Icons.Filled.Favorite, contentDescription = null)
        }
    },
)

// ---------------------------------------------------------------------------
// 2. AccessibilityTouchTarget — 48dp (ok) vs 32dp (too small).
// Both carry Role.Button + a description so the ONLY differing signal is bounds.
// BAD -> a11y.touch_target.small (R2).
// ---------------------------------------------------------------------------
@Composable
fun TouchTargetScenario() = Section(
    "Touch target size",
    good = {
        Box(
            Modifier
                .size(48.dp)
                .clip(MaterialTheme.shapes.small)
                .background(MaterialTheme.colorScheme.primary)
                .clickable {}
                .testTag("good_touch_target")
                .semantics {
                    contentDescription = "48dp button"
                    role = Role.Button
                }
        )
    },
    bad = {
        Box(
            Modifier
                .size(32.dp)
                .clip(MaterialTheme.shapes.small)
                .background(MaterialTheme.colorScheme.error)
                .clickable {}
                .testTag("bad_touch_target")
                .semantics {
                    contentDescription = "32dp button"
                    role = Role.Button
                }
        )
    },
)

// ---------------------------------------------------------------------------
// 3. AccessibilityTextContrast — high vs low contrast on white (bitmap-validated).
// BAD -> a11y.contrast.low (R3): #BFBFBF on white is ~1.8:1 (needs 4.5:1).
// ---------------------------------------------------------------------------
@Composable
fun TextContrastScenario() = Section(
    "Text contrast",
    good = {
        Surface(color = Color.White) {
            Text(
                "Readable (16:1 on white)",
                color = Color(0xFF1A1A1A),
                modifier = Modifier
                    .padding(8.dp)
                    .testTag("good_contrast")
            )
        }
    },
    bad = {
        Surface(color = Color.White) {
            Text(
                "Hard to read (1.6:1)",
                color = Color(0xFFBFBFBF),
                modifier = Modifier
                    .padding(8.dp)
                    .testTag("bad_contrast")
            )
        }
    },
)

// ---------------------------------------------------------------------------
// 4. MissingStateDescription — labeled Switch with a stateDescription vs a bare
// Switch with neither a label nor a custom state.
// BAD -> a11y.label.missing (R1): TalkBack says only "Off, Switch" — the user
// cannot tell what it toggles.
// ---------------------------------------------------------------------------
@Composable
fun ToggleStateScenario() {
    var g by remember { mutableStateOf(true) }
    var b by remember { mutableStateOf(true) }
    Section(
        "Switch stateDescription",
        good = {
            Switch(
                checked = g,
                onCheckedChange = { g = it },
                modifier = Modifier
                    .testTag("good_switch")
                    .semantics {
                        contentDescription = "Wi-Fi"
                        stateDescription = if (g) "Wi-Fi on" else "Wi-Fi off"
                    }
            )
        },
        bad = {
            Switch(
                checked = b,
                onCheckedChange = { b = it },
                modifier = Modifier.testTag("bad_switch")
            )
        },
    )
}

// ---------------------------------------------------------------------------
// 5. MissingStateDescription (Checkbox) — labeled state vs bare checkbox.
// BAD -> a11y.label.missing (R1): a bare Checkbox announces "Not checked, Checkbox".
// ---------------------------------------------------------------------------
@Composable
fun CheckboxStateScenario() {
    var g by remember { mutableStateOf(false) }
    var b by remember { mutableStateOf(false) }
    Section(
        "Checkbox stateDescription",
        good = {
            Checkbox(
                checked = g,
                onCheckedChange = { g = it },
                modifier = Modifier
                    .testTag("good_checkbox")
                    .semantics {
                        contentDescription = "Subscribe to newsletter"
                        stateDescription = if (g) "Subscribed" else "Not subscribed"
                    }
            )
        },
        bad = {
            Checkbox(
                checked = b,
                onCheckedChange = { b = it },
                modifier = Modifier.testTag("bad_checkbox")
            )
        },
    )
}

// ---------------------------------------------------------------------------
// 6. MissingContentDescription (Image) — labeled vs unlabeled meaningful image.
// BAD -> a11y.image.no_description (R6).
// ---------------------------------------------------------------------------
@Composable
fun ImageLabelScenario() = Section(
    "Image label",
    good = {
        Box(
            Modifier
                .size(64.dp)
                .background(Color(0xFF3366CC))
                .testTag("good_image")
                .semantics {
                    contentDescription = "Company logo"
                    role = Role.Image
                }
        )
    },
    bad = {
        Box(
            Modifier
                .size(64.dp)
                .background(Color(0xFFCC3366))
                .testTag("bad_image")
                .semantics { role = Role.Image }
        )
    },
)

// ---------------------------------------------------------------------------
// 7. RedundantDecorativeLabel — a purely decorative image SHOULD be silent.
// GOOD clears semantics (announced as nothing); BAD slaps a description on a
// purely decorative divider, adding a TalkBack stop that says nothing useful.
// (No R1..R12 rule targets this; the golden test checks the dump instead.)
// ---------------------------------------------------------------------------
@Composable
fun DecorativeImageScenario() = Section(
    "Decorative image (should be silent)",
    good = {
        // Decorative divider -> clearAndSetSemantics {} removes it from the tree.
        Box(
            Modifier
                .fillMaxWidth()
                .height(8.dp)
                .background(Color(0xFFDDDDDD))
                .testTag("good_decorative")
                .clearAndSetSemantics {}
        )
    },
    bad = {
        Box(
            Modifier
                .fillMaxWidth()
                .height(8.dp)
                .background(Color(0xFFDDDDDD))
                .testTag("bad_decorative")
                .semantics { contentDescription = "Decorative divider" }
        )
    },
)

// ---------------------------------------------------------------------------
// 8. MissingRole — custom clickable Box with vs without Role.Button.
// Both are >=48dp tall and named by their child Text, so the ONLY differing
// signal is the role. BAD -> a11y.role.missing_on_clickable (R5).
// ---------------------------------------------------------------------------
@Composable
fun CustomClickableRoleScenario() = Section(
    "Custom clickable Role",
    good = {
        Box(
            Modifier
                .clickable {}
                .semantics { role = Role.Button }
                .padding(horizontal = 24.dp, vertical = 16.dp)
                .testTag("good_role")
        ) { Text("Submit") }
    },
    bad = {
        Box(
            Modifier
                .clickable {}
                .padding(horizontal = 24.dp, vertical = 16.dp)
                .testTag("bad_role")
        ) { Text("Send") }
    },
)

// ---------------------------------------------------------------------------
// 9. BrokenTraversalOrder — the reading-order corpus.
// Each column is its own traversal group, so traversalIndex is scoped to it.
//   GOOD            visual 1,2,3                         -> reads 1,2,3
//   GOOD reordered  visual 3,2,1 + ascending traversalIndex -> reads 1,2,3
//   BAD             visual 3,2,1, no traversalIndex       -> reads 3,2,1
// The golden test asserts exactly this order in the host's focus_order.
// ---------------------------------------------------------------------------
@Composable
fun TraversalOrderScenario() = Section(
    "Traversal order",
    good = {
        Column(Modifier.semantics { isTraversalGroup = true }.testTag("good_trav")) {
            Text("1. First", Modifier.semantics { traversalIndex = 0f }.testTag("good_trav_1"))
            Text("2. Second", Modifier.semantics { traversalIndex = 1f }.testTag("good_trav_2"))
            Text("3. Third", Modifier.semantics { traversalIndex = 2f }.testTag("good_trav_3"))
        }
        Spacer(Modifier.height(12.dp))
        Text("GOOD (reversed layout, reordered by traversalIndex)", style = MaterialTheme.typography.labelLarge)
        Spacer(Modifier.height(8.dp))
        Column(Modifier.semantics { isTraversalGroup = true }.testTag("good_trav_reordered")) {
            Text("3. Third (reordered)", Modifier.semantics { traversalIndex = 2f }.testTag("good_trav_r3"))
            Text("2. Second (reordered)", Modifier.semantics { traversalIndex = 1f }.testTag("good_trav_r2"))
            Text("1. First (reordered)", Modifier.semantics { traversalIndex = 0f }.testTag("good_trav_r1"))
        }
    },
    bad = {
        Column(Modifier.semantics { isTraversalGroup = true }.testTag("bad_trav")) {
            Text("3. Third (bad)", Modifier.testTag("bad_trav_3"))
            Text("2. Second (bad)", Modifier.testTag("bad_trav_2"))
            Text("1. First (bad)", Modifier.testTag("bad_trav_1"))
        }
    },
)

// ---------------------------------------------------------------------------
// 10. BrokenTraversalOrder (LazyColumn) — each row one labeled merged item vs
// rows whose icon and text are two separate focus stops, the icon unlabeled.
// BAD -> a11y.label.missing (R1) on every bad_lazy_icon_<i>.
// ---------------------------------------------------------------------------
@Composable
fun LazyListScenario() = Section(
    "List item semantics",
    good = {
        // Each row is one merged, labeled focus stop in natural order.
        Column(Modifier.testTag("good_lazy_list")) {
            listOf("Inbox", "Sent", "Drafts").forEachIndexed { i, label ->
                Row(
                    Modifier
                        .fillMaxWidth()
                        .clickable {}
                        .padding(12.dp)
                        .semantics(mergeDescendants = true) {
                            traversalIndex = i.toFloat()
                            contentDescription = "$label folder"
                            role = Role.Button
                        }
                        .testTag("good_lazy_item_$i"),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Icon(Icons.Filled.Favorite, contentDescription = null)
                    Spacer(Modifier.width(8.dp))
                    Text(label)
                }
            }
        }
    },
    bad = {
        // A real LazyColumn whose items carry no item-level semantics: the icon
        // and the text are two separate focus stops per row, and the icon (the
        // actual click target) has no label.
        LazyColumn(
            Modifier
                .fillMaxWidth()
                .height(160.dp)
                .testTag("bad_lazy_list")
        ) {
            itemsIndexed(listOf("Inbox", "Sent", "Drafts")) { i, label ->
                Row(
                    Modifier
                        .fillMaxWidth()
                        .padding(12.dp)
                        .testTag("bad_lazy_item_$i"),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Icon(
                        Icons.Filled.Favorite,
                        contentDescription = null,
                        modifier = Modifier
                            .clickable {}
                            .testTag("bad_lazy_icon_$i")
                    )
                    Spacer(Modifier.width(8.dp))
                    Text(label, Modifier.clickable {}.testTag("bad_lazy_text_$i"))
                }
            }
        }
    },
)

// ---------------------------------------------------------------------------
// 11. MissingHeading — section title heading() vs plain big-styled text.
// (No R1..R12 rule can see a missing heading on a short screen; the golden
// test checks the a11y heading flag instead.)
// ---------------------------------------------------------------------------
@Composable
fun HeadingScenario() = Section(
    "Section heading",
    good = {
        Column {
            Text(
                "Account",
                style = MaterialTheme.typography.titleMedium,
                modifier = Modifier.semantics { heading() }.testTag("good_heading")
            )
            Text("Manage your account settings.")
        }
    },
    bad = {
        Column {
            Text(
                "Account",
                style = MaterialTheme.typography.titleMedium,
                modifier = Modifier.testTag("bad_heading")
            )
            Text("Manage your account settings.")
        }
    },
)

// ---------------------------------------------------------------------------
// 12. RedundantLabelText — description should not restate the role.
// GOOD: "Favorite". BAD: "Favorite button image" (TalkBack already says button).
// BAD -> a11y.label.redundant (R4).
// ---------------------------------------------------------------------------
@Composable
fun RedundantLabelScenario() = Section(
    "Redundant label",
    good = {
        IconButton(onClick = {}, modifier = Modifier.testTag("good_redundant")) {
            Icon(Icons.Filled.Favorite, contentDescription = "Favorite")
        }
    },
    bad = {
        IconButton(onClick = {}, modifier = Modifier.testTag("bad_redundant")) {
            Icon(Icons.Filled.Favorite, contentDescription = "Favorite button image")
        }
    },
)

// ---------------------------------------------------------------------------
// 13. ContentDescDuplicatesText — desc equals the visible label text.
// GOOD: text only, no extra desc. BAD: a contentDescription that duplicates the
// visible text. BAD -> a11y.label.redundant (R4, reason equals_text).
// ---------------------------------------------------------------------------
@Composable
fun DuplicateDescTextScenario() = Section(
    "Description duplicates text",
    good = {
        Button(onClick = {}, modifier = Modifier.testTag("good_dup_text")) {
            Text("Continue")
        }
    },
    bad = {
        Button(
            onClick = {},
            modifier = Modifier
                .testTag("bad_dup_text")
                .semantics { contentDescription = "Next" }
        ) {
            Text("Next")
        }
    },
)

// ---------------------------------------------------------------------------
// 14. MissingFormLabel — text field with vs without a label/hint.
// BAD -> a11y.label.missing (R1): an empty field with no label or hint
// announces only "Edit box".
// ---------------------------------------------------------------------------
@Composable
fun FormFieldScenario() {
    var g by remember { mutableStateOf("") }
    var b by remember { mutableStateOf("") }
    Section(
        "Form field label",
        good = {
            OutlinedTextField(
                value = g,
                onValueChange = { g = it },
                label = { Text("Email address") },
                placeholder = { Text("name@example.com") },
                modifier = Modifier
                    .fillMaxWidth()
                    .testTag("good_form_field")
            )
        },
        bad = {
            OutlinedTextField(
                value = b,
                onValueChange = { b = it },
                modifier = Modifier
                    .fillMaxWidth()
                    .testTag("bad_form_field")
            )
        },
    )
}

// ---------------------------------------------------------------------------
// 15. TinyTextSize — tiny 8sp vs a readable 16sp.
// (No R1..R12 rule measures text size yet; kept as corpus for a future rule.)
// ---------------------------------------------------------------------------
@Composable
fun TinyTextScenario() = Section(
    "Tiny fixed text size",
    good = {
        Text(
            "Body text at a readable 16sp",
            fontSize = 16.sp,
            modifier = Modifier.testTag("good_tiny_text")
        )
    },
    bad = {
        Text(
            "Tiny 8sp text that is hard to read",
            fontSize = 8.sp,
            modifier = Modifier.testTag("bad_tiny_text")
        )
    },
)

// ---------------------------------------------------------------------------
// 16. MergedDescendantsFocus — a clickable row that merges its children into one
// focus stop vs a row whose own clickable icon steals focus as a separate
// (and tiny) target. BAD -> a11y.touch_target.small (R2) on the 24dp child.
// ---------------------------------------------------------------------------
@Composable
fun MergedFocusScenario() = Section(
    "Merged vs stolen focus",
    good = {
        // One focus stop for the whole row.
        Row(
            Modifier
                .fillMaxWidth()
                .clickable {}
                .padding(12.dp)
                .semantics(mergeDescendants = true) { role = Role.Button }
                .testTag("good_merged_focus"),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Icon(Icons.Filled.PlayArrow, contentDescription = null)
            Spacer(Modifier.width(8.dp))
            Text("Play episode")
            Spacer(Modifier.weight(1f))
            Text("12:34")
        }
    },
    bad = {
        Row(
            Modifier
                .fillMaxWidth()
                .clickable {}
                .padding(12.dp)
                .testTag("bad_merged_focus"),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Icon(
                Icons.Filled.PlayArrow,
                contentDescription = "Play",
                modifier = Modifier
                    .clickable {}
                    .testTag("bad_merged_focus_child")
            )
            Spacer(Modifier.width(8.dp))
            Text("Play episode (bad)")
            Spacer(Modifier.weight(1f))
            Text("12:34")
        }
    },
)

// ---------------------------------------------------------------------------
// 17. MissingDisabledState — Disabled semantics vs greyed-but-clickable.
// (No R1..R12 rule; the golden test checks that BAD is still enabled+clickable.)
// ---------------------------------------------------------------------------
@Composable
fun DisabledControlScenario() = Section(
    "Disabled state",
    good = {
        // Button(enabled=false) emits the Disabled semantics flag.
        Button(onClick = {}, enabled = false, modifier = Modifier.testTag("good_disabled")) {
            Text("Unavailable")
        }
    },
    bad = {
        // Looks disabled (40% alpha) but is still an enabled, clickable button.
        Box(
            Modifier
                .alpha(0.4f)
                .clickable(role = Role.Button) {}
                .padding(12.dp)
                .testTag("bad_disabled")
        ) { Text("Unavailable (bad)") }
    },
)

// ---------------------------------------------------------------------------
// 18. MissingLiveRegion — a status line that announces changes vs one that is
// updated silently. Tapping the button mutates the status text.
// (No R1..R12 rule; the golden test checks the a11y live_region field.)
// ---------------------------------------------------------------------------
@Composable
fun LiveRegionScenario() {
    var gCount by remember { mutableIntStateOf(0) }
    var bCount by remember { mutableIntStateOf(0) }
    Section(
        "Live region announcement",
        good = {
            Column {
                Text(
                    "Items in cart: $gCount",
                    modifier = Modifier
                        .testTag("good_live_region")
                        .semantics { liveRegion = LiveRegionMode.Polite }
                )
                Spacer(Modifier.height(8.dp))
                Button(onClick = { gCount++ }, modifier = Modifier.testTag("good_live_button")) {
                    Text("Add item")
                }
            }
        },
        bad = {
            Column {
                Text(
                    "Items in cart (bad): $bCount",
                    modifier = Modifier.testTag("bad_live_region")
                )
                Spacer(Modifier.height(8.dp))
                Button(onClick = { bCount++ }, modifier = Modifier.testTag("bad_live_button")) {
                    Text("Add item (bad)")
                }
            }
        },
    )
}

// ---------------------------------------------------------------------------
// 19. DuplicateClickableBounds — one merged, labeled target vs two overlapping
// click targets for the same action, the inner one unlabeled.
// BAD -> a11y.label.missing (R1) on bad_merged_inner (or a duplicate-bounds rule).
// ---------------------------------------------------------------------------
@Composable
fun DuplicateLabelScenario() = Section(
    "Duplicate / merged labels",
    good = {
        Row(
            Modifier
                .clickable {}
                .heightIn(min = 48.dp)
                .padding(horizontal = 12.dp)
                .semantics(mergeDescendants = true) {
                    contentDescription = "Open profile"
                    role = Role.Button
                }
                .testTag("good_merged"),
            horizontalArrangement = Arrangement.Start,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text("Open")
            Spacer(Modifier.width(4.dp))
            Text("profile")
        }
    },
    bad = {
        Box(Modifier.clickable {}.testTag("bad_merged")) {
            Row(
                Modifier
                    .heightIn(min = 48.dp)
                    .padding(horizontal = 12.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text("Open")
                Spacer(Modifier.width(4.dp))
                Text("profile (bad)")
            }
            // A second, unlabeled click target stacked on the same bounds.
            Box(
                Modifier
                    .matchParentSize()
                    .clickable {}
                    .testTag("bad_merged_inner")
            )
        }
    },
)

// ---------------------------------------------------------------------------
// 20. MissingToggleState — a custom switch built from clickable(role = Switch)
// that never exposes on/off vs the same row built with toggleable().
// BAD -> a11y.state.not_exposed (R7): TalkBack says "Dark mode, Switch" with no state.
// ---------------------------------------------------------------------------
@Composable
fun CustomToggleScenario() {
    var g by remember { mutableStateOf(true) }
    var b by remember { mutableStateOf(true) }
    Section(
        "Custom toggle state",
        good = {
            Row(
                Modifier
                    .fillMaxWidth()
                    .toggleable(value = g, role = Role.Switch) { g = it }
                    .padding(12.dp)
                    .testTag("good_custom_toggle"),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text("Dark mode", Modifier.weight(1f))
                Knob(on = g)
            }
        },
        bad = {
            Row(
                Modifier
                    .fillMaxWidth()
                    .clickable(role = Role.Switch) { b = !b }
                    .padding(12.dp)
                    .testTag("bad_custom_toggle"),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text("Dark mode (bad)", Modifier.weight(1f))
                Knob(on = b)
            }
        },
    )
}

/** A purely visual switch knob (no semantics). */
@Composable
private fun Knob(on: Boolean) {
    Box(
        Modifier
            .size(width = 40.dp, height = 24.dp)
            .clip(CircleShape)
            .background(if (on) MaterialTheme.colorScheme.primary else Color(0xFF9E9E9E))
    )
}
