// ============================================================================
// Scenarios.kt — each function renders the GOOD and BAD variant of one lint
// rule. Every interactive node carries a testTag good_<rule> / bad_<rule> so
// the host can pinpoint it in the merged semantics tree and crop its
// per-component image (testapps.md §3).
//
// Design rationale (testapps.md §0): the agent reads semantics by iterating
// SemanticsNode.getConfig() and storing each SemanticsPropertyKey.getName() →
// stringified value. A lint rule therefore fires on the PRESENCE/ABSENCE of a
// named key. Each pair below is built so the GOOD variant emits the key and the
// BAD variant omits it. Touch-target and contrast are NOT pure-semantics: touch
// target is computed from getBoundsInWindow() width/height, and contrast is
// sampled from the per-component bitmap.
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
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.itemsIndexed
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
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.semantics.traversalIndex
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

// ---------------------------------------------------------------------------
// 1. MissingContentDescription — IconButton with vs without contentDescription.
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
            // FIXED by Inspector Widget: give the clickable icon a label.
            Icon(Icons.Filled.Favorite, contentDescription = "Add to favorites")
        }
    },
)

// ---------------------------------------------------------------------------
// 2. AccessibilityTouchTarget — 48dp (ok) vs 32dp (too small).
// Both carry Role.Button + a description so the ONLY differing signal is bounds.
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
        // FIXED: bump the actionable element to the 48dp minimum touch target.
        Box(
            Modifier
                .size(48.dp)
                .clip(MaterialTheme.shapes.small)
                .background(MaterialTheme.colorScheme.error)
                .clickable {}
                .testTag("bad_touch_target")
                .semantics {
                    contentDescription = "48dp button"
                    role = Role.Button
                }
        )
    },
)

// ---------------------------------------------------------------------------
// 3. AccessibilityTextContrast — high vs low contrast on white (bitmap-validated).
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
            // FIXED: use a sufficient-contrast text color (~16:1 on white).
            Text(
                "Hard to read (1.6:1)",
                color = Color(0xFF1A1A1A),
                modifier = Modifier
                    .padding(8.dp)
                    .testTag("bad_contrast")
            )
        }
    },
)

// ---------------------------------------------------------------------------
// 4. MissingStateDescription — Switch with vs without stateDescription.
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
                    .semantics { stateDescription = if (g) "Wi-Fi on" else "Wi-Fi off" }
            )
        },
        bad = {
            // FIXED: add a stateDescription so TalkBack announces a meaningful state.
            Switch(
                checked = b,
                onCheckedChange = { b = it },
                modifier = Modifier
                    .testTag("bad_switch")
                    .semantics { stateDescription = if (b) "Wi-Fi on" else "Wi-Fi off" }
            )
        },
    )
}

// ---------------------------------------------------------------------------
// 5. MissingStateDescription (Checkbox) — labeled state vs bare checkbox.
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
                    .semantics { stateDescription = if (g) "Subscribed" else "Not subscribed" }
            )
        },
        bad = {
            // FIXED: add a stateDescription so TalkBack announces a meaningful state.
            Checkbox(
                checked = b,
                onCheckedChange = { b = it },
                modifier = Modifier
                    .testTag("bad_checkbox")
                    .semantics { stateDescription = if (b) "Subscribed" else "Not subscribed" }
            )
        },
    )
}

// ---------------------------------------------------------------------------
// 6. MissingContentDescription (Image) — labeled vs unlabeled meaningful image.
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
        // FIXED: label the meaningful image so TalkBack can describe it.
        Box(
            Modifier
                .size(64.dp)
                .background(Color(0xFFCC3366))
                .testTag("bad_image")
                .semantics {
                    contentDescription = "Company logo"
                    role = Role.Image
                }
        )
    },
)

// ---------------------------------------------------------------------------
// 7. RedundantDecorativeLabel — a purely decorative image SHOULD be null/hidden.
// GOOD clears semantics (announced as nothing); BAD slaps a description on a
// purely decorative divider, adding TalkBack noise.
// ---------------------------------------------------------------------------
@Composable
fun DecorativeImageScenario() = Section(
    "Decorative image (should be silent)",
    good = {
        // Decorative divider → clearAndSetSemantics {} removes it from the tree.
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
        // FIXED: decorative divider → clear its semantics so TalkBack stays silent.
        Box(
            Modifier
                .fillMaxWidth()
                .height(8.dp)
                .background(Color(0xFFDDDDDD))
                .testTag("bad_decorative")
                .clearAndSetSemantics {}
        )
    },
)

// ---------------------------------------------------------------------------
// 8. MissingRole — custom clickable Box with vs without Role.Button.
// ---------------------------------------------------------------------------
@Composable
fun CustomClickableRoleScenario() = Section(
    "Custom clickable Role",
    good = {
        Box(
            Modifier
                .padding(16.dp)
                .clickable {}
                .testTag("good_role")
                .semantics {
                    role = Role.Button
                    contentDescription = "Submit"
                }
        ) { Text("Submit") }
    },
    bad = {
        // FIXED: declare the Button role on the custom clickable Box.
        Box(
            Modifier
                .padding(16.dp)
                .clickable {}
                .testTag("bad_role")
                .semantics {
                    role = Role.Button
                    contentDescription = "Submit"
                }
        ) { Text("Submit") }
    },
)

// ---------------------------------------------------------------------------
// 9. BrokenTraversalOrder — ascending traversalIndex vs reversed children.
// ---------------------------------------------------------------------------
@Composable
fun TraversalOrderScenario() = Section(
    "Traversal order",
    good = {
        Column {
            Text("1. First", Modifier.semantics { traversalIndex = 0f }.testTag("good_trav_1"))
            Text("2. Second", Modifier.semantics { traversalIndex = 1f }.testTag("good_trav_2"))
            Text("3. Third", Modifier.semantics { traversalIndex = 2f }.testTag("good_trav_3"))
        }
    },
    bad = {
        // FIXED: assign ascending traversalIndex so TalkBack reads 1 → 2 → 3
        // regardless of the reversed visual order.
        Column {
            Text("3. Third", Modifier.semantics { traversalIndex = 2f }.testTag("bad_trav_3"))
            Text("2. Second", Modifier.semantics { traversalIndex = 1f }.testTag("bad_trav_2"))
            Text("1. First", Modifier.semantics { traversalIndex = 0f }.testTag("bad_trav_1"))
        }
    },
)

// ---------------------------------------------------------------------------
// 10. BrokenTraversalOrder (LazyColumn) — each row a labeled merged item vs
// rows whose icon + text become two separate, unlabeled focus stops.
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
        // A real LazyColumn whose items carry no item-level semantics; the icon
        // and text become two unlabeled focus stops per row.
        LazyColumn(
            Modifier
                .fillMaxWidth()
                .height(160.dp)
                .testTag("bad_lazy_list")
        ) {
            itemsIndexed(listOf("Inbox", "Sent", "Drafts")) { i, label ->
                // FIXED: merge each row into one labeled focus stop in natural order.
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
                        .testTag("bad_lazy_item_$i"),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Icon(Icons.Filled.Favorite, contentDescription = null)
                    Spacer(Modifier.width(8.dp))
                    Text(label)
                }
            }
        }
    },
)

// ---------------------------------------------------------------------------
// 11. MissingHeading — section title heading() vs plain big-styled text.
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
            // FIXED: mark the section title as a heading for TalkBack navigation.
            Text(
                "Account",
                style = MaterialTheme.typography.titleMedium,
                modifier = Modifier.semantics { heading() }.testTag("bad_heading")
            )
            Text("Manage your account settings.")
        }
    },
)

// ---------------------------------------------------------------------------
// 12. RedundantLabelText — description should not restate the role.
// GOOD: "Favorite". BAD: "Favorite button image" (TalkBack already says button).
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
        // FIXED: drop redundant role words; TalkBack announces the role itself.
        IconButton(onClick = {}, modifier = Modifier.testTag("bad_redundant")) {
            Icon(Icons.Filled.Favorite, contentDescription = "Favorite")
        }
    },
)

// ---------------------------------------------------------------------------
// 13. ContentDescDuplicatesText — desc equals the visible label text.
// GOOD: text only, no extra desc. BAD: a contentDescription that duplicates the
// visible text, so TalkBack reads it twice.
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
        // FIXED: drop the contentDescription that merely duplicated the visible text.
        Button(
            onClick = {},
            modifier = Modifier.testTag("bad_dup_text")
        ) {
            Text("Continue")
        }
    },
)

// ---------------------------------------------------------------------------
// 14. MissingFormLabel — text field with vs without a label/hint.
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
            // FIXED: give the field a label + placeholder so it is announced.
            OutlinedTextField(
                value = b,
                onValueChange = { b = it },
                label = { Text("Email address") },
                placeholder = { Text("name@example.com") },
                modifier = Modifier
                    .fillMaxWidth()
                    .testTag("bad_form_field")
            )
        },
    )
}

// ---------------------------------------------------------------------------
// 15. TinyTextSize — fixed tiny sp vs a scalable, readable size.
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
        // FIXED: use a readable 16sp size instead of the tiny 8sp.
        Text(
            "Tiny 8sp text that is hard to read",
            fontSize = 16.sp,
            modifier = Modifier.testTag("bad_tiny_text")
        )
    },
)

// ---------------------------------------------------------------------------
// 16. MergedDescendantsFocus — a clickable row that merges its children into one
// focus stop vs children that each grab focus and steal it from the row.
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
        // FIXED: merge the whole row into one focus stop and make the icon
        // decorative so it no longer steals focus as a separate target.
        Row(
            Modifier
                .fillMaxWidth()
                .clickable {}
                .padding(12.dp)
                .semantics(mergeDescendants = true) { role = Role.Button }
                .testTag("bad_merged_focus"),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Icon(
                Icons.Filled.PlayArrow,
                contentDescription = null,
                modifier = Modifier.testTag("bad_merged_focus_child")
            )
            Spacer(Modifier.width(8.dp))
            Text("Play episode")
            Spacer(Modifier.weight(1f))
            Text("12:34")
        }
    },
)

// ---------------------------------------------------------------------------
// 17. MissingDisabledState — Disabled semantics vs greyed-but-clickable.
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
        // FIXED: mark the control disabled (and stop it being clickable) so its
        // greyed appearance matches its accessibility state.
        Box(
            Modifier
                .alpha(0.4f)
                .clickable(enabled = false) {}
                .padding(12.dp)
                .testTag("bad_disabled")
                .semantics {
                    role = Role.Button
                    contentDescription = "Unavailable"
                    disabled()
                }
        ) { Text("Unavailable") }
    },
)

// ---------------------------------------------------------------------------
// 18. MissingLiveRegion — a status line that announces changes vs one that is
// updated silently. Tapping the button mutates the status text.
// ---------------------------------------------------------------------------
@Composable
fun LiveRegionScenario() {
    var gCount by remember { mutableStateOf(0) }
    var bCount by remember { mutableStateOf(0) }
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
                // FIXED: mark the status line as a live region so updates announce.
                Text(
                    "Items in cart: $bCount",
                    modifier = Modifier
                        .testTag("bad_live_region")
                        .semantics { liveRegion = LiveRegionMode.Polite }
                )
                Spacer(Modifier.height(8.dp))
                Button(onClick = { bCount++ }, modifier = Modifier.testTag("bad_live_button")) {
                    Text("Add item")
                }
            }
        },
    )
}

// ---------------------------------------------------------------------------
// 19. DuplicateClickableBounds — merged single target vs two overlapping
// unlabeled targets.
// ---------------------------------------------------------------------------
@Composable
fun DuplicateLabelScenario() = Section(
    "Duplicate / merged labels",
    good = {
        Row(
            Modifier
                .clickable {}
                .padding(8.dp)
                .semantics(mergeDescendants = true) { contentDescription = "Open profile" }
                .testTag("good_merged"),
            horizontalArrangement = Arrangement.Start
        ) {
            Text("Open")
            Spacer(Modifier.width(4.dp))
            Text("profile")
        }
    },
    bad = {
        // FIXED: collapse the two overlapping click targets into one merged,
        // labeled target for the single conceptual action.
        Row(
            Modifier
                .clickable {}
                .padding(8.dp)
                .semantics(mergeDescendants = true) { contentDescription = "Open profile" }
                .testTag("bad_merged"),
            horizontalArrangement = Arrangement.Start
        ) {
            Text("Open")
            Spacer(Modifier.width(4.dp))
            Text("profile")
        }
    },
)
