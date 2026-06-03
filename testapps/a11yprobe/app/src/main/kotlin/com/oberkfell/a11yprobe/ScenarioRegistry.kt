// ============================================================================
// ScenarioRegistry.kt — drives the launcher list.
//
// Each Scenario is a matched GOOD/BAD pair for one Task-B lint rule. The host
// can drive the corpus deterministically: tap `launch_<id>`, dump the merged
// semantics tree, and assert the `good_*` node carries the expected key while
// the `bad_*` node omits it (testapps.md §0, §3, §7).
// ============================================================================
package com.oberkfell.a11yprobe

import androidx.compose.runtime.Composable

/**
 * A single scenario in the corpus.
 *
 * @param id      stable id; the launcher row carries testTag `launch_<id>`.
 * @param title   human-readable title shown in the launcher and top bar.
 * @param rule    the Task-B lint rule this scenario exercises.
 * @param content the screen that renders the GOOD variant above the BAD variant.
 */
data class Scenario(
    val id: String,
    val title: String,
    val rule: String,
    val content: @Composable () -> Unit,
)

/**
 * The full Compose corpus. Order is stable so scripts can index by position.
 * The classic-View (XML) screen is appended by the launcher itself, not here,
 * because it is a separate Activity (testapps.md §4, §5).
 */
val SCENARIOS: List<Scenario> = listOf(
    Scenario("icon_button", "Icon button label", "MissingContentDescription") { IconButtonScenario() },
    Scenario("touch_target", "Touch target size", "AccessibilityTouchTarget") { TouchTargetScenario() },
    Scenario("text_contrast", "Text contrast", "AccessibilityTextContrast") { TextContrastScenario() },
    Scenario("toggle_state", "Switch state desc", "MissingStateDescription") { ToggleStateScenario() },
    Scenario("checkbox_state", "Checkbox state desc", "MissingStateDescription") { CheckboxStateScenario() },
    Scenario("image_label", "Image label", "MissingContentDescription") { ImageLabelScenario() },
    Scenario("decorative_image", "Decorative image", "RedundantDecorativeLabel") { DecorativeImageScenario() },
    Scenario("custom_role", "Custom clickable role", "MissingRole") { CustomClickableRoleScenario() },
    Scenario("traversal", "Traversal order", "BrokenTraversalOrder") { TraversalOrderScenario() },
    Scenario("lazy_list", "List item semantics", "BrokenTraversalOrder") { LazyListScenario() },
    Scenario("heading", "Section heading", "MissingHeading") { HeadingScenario() },
    Scenario("redundant_label", "Redundant label", "RedundantLabelText") { RedundantLabelScenario() },
    Scenario("dup_text_desc", "Desc duplicates text", "ContentDescDuplicatesText") { DuplicateDescTextScenario() },
    Scenario("form_field", "Form field label", "MissingFormLabel") { FormFieldScenario() },
    Scenario("tiny_text", "Tiny fixed text size", "TinyTextSize") { TinyTextScenario() },
    Scenario("merged_focus", "Merged vs stolen focus", "MergedDescendantsFocus") { MergedFocusScenario() },
    Scenario("disabled", "Disabled control", "MissingDisabledState") { DisabledControlScenario() },
    Scenario("live_region", "Live region", "MissingLiveRegion") { LiveRegionScenario() },
    Scenario("dup_label", "Duplicate labels", "DuplicateClickableBounds") { DuplicateLabelScenario() },
)
