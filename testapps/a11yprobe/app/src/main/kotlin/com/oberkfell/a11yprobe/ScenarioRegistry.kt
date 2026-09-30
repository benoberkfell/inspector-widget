// ============================================================================
// ScenarioRegistry.kt — drives the launcher list.
//
// Each Scenario is a matched GOOD/BAD pair for one accessibility rule. The host
// drives the corpus deterministically: launch a scenario by intent extra (see
// MainActivity), dump the unified a11y tree, lint it, and assert the `bad_*`
// node is flagged with [Scenario.lintRule] while the `good_*` node is not
// (host/tests/test_device_a11y_golden.py; testapps.md §0, §3, §7).
// ============================================================================
package com.oberkfell.a11yprobe

import androidx.compose.runtime.Composable

/**
 * A single scenario in the corpus.
 *
 * @param id       stable id; the launcher row carries testTag `launch_<id>` and
 *                 `am start ... --es scenario <id>` opens it directly.
 * @param title    human-readable title shown in the launcher and top bar.
 * @param rule     the accessibility check this scenario exercises (ATF-style name).
 * @param lintRule the host lint rule id (inspector_widget.a11y_lint) expected on the
 *                 BAD variant, or null when no R1..R12 rule can see the defect and the
 *                 golden test checks the a11y dump instead.
 * @param content  the screen that renders the GOOD variant above the BAD variant.
 */
data class Scenario(
    val id: String,
    val title: String,
    val rule: String,
    val lintRule: String?,
    val content: @Composable () -> Unit,
)

/**
 * The full Compose corpus. Order is stable so scripts can index by position.
 * The classic-View (XML) screen and the mixed View/Compose interop screens are
 * separate Activities (ViewScenarioActivity, InteropActivity); the launcher
 * appends rows for them.
 */
val SCENARIOS: List<Scenario> = listOf(
    Scenario("icon_button", "Icon button label", "MissingContentDescription", "a11y.label.missing") { IconButtonScenario() },
    Scenario("touch_target", "Touch target size", "AccessibilityTouchTarget", "a11y.touch_target.small") { TouchTargetScenario() },
    Scenario("text_contrast", "Text contrast", "AccessibilityTextContrast", "a11y.contrast.low") { TextContrastScenario() },
    Scenario("toggle_state", "Switch state desc", "MissingStateDescription", "a11y.label.missing") { ToggleStateScenario() },
    Scenario("checkbox_state", "Checkbox state desc", "MissingStateDescription", "a11y.label.missing") { CheckboxStateScenario() },
    Scenario("image_label", "Image label", "MissingContentDescription", "a11y.image.no_description") { ImageLabelScenario() },
    Scenario("decorative_image", "Decorative image", "RedundantDecorativeLabel", null) { DecorativeImageScenario() },
    Scenario("custom_role", "Custom clickable role", "MissingRole", "a11y.role.missing_on_clickable") { CustomClickableRoleScenario() },
    Scenario("traversal", "Traversal order", "BrokenTraversalOrder", null) { TraversalOrderScenario() },
    Scenario("lazy_list", "List item semantics", "BrokenTraversalOrder", "a11y.label.missing") { LazyListScenario() },
    Scenario("heading", "Section heading", "MissingHeading", null) { HeadingScenario() },
    Scenario("redundant_label", "Redundant label", "RedundantLabelText", "a11y.label.redundant") { RedundantLabelScenario() },
    Scenario("dup_text_desc", "Desc duplicates text", "ContentDescDuplicatesText", "a11y.label.redundant") { DuplicateDescTextScenario() },
    Scenario("form_field", "Form field label", "MissingFormLabel", "a11y.label.missing") { FormFieldScenario() },
    Scenario("tiny_text", "Tiny fixed text size", "TinyTextSize", null) { TinyTextScenario() },
    Scenario("merged_focus", "Merged vs stolen focus", "MergedDescendantsFocus", "a11y.touch_target.small") { MergedFocusScenario() },
    Scenario("disabled", "Disabled control", "MissingDisabledState", null) { DisabledControlScenario() },
    Scenario("live_region", "Live region", "MissingLiveRegion", null) { LiveRegionScenario() },
    Scenario("dup_label", "Duplicate labels", "DuplicateClickableBounds", "a11y.label.missing") { DuplicateLabelScenario() },
    Scenario("custom_toggle", "Custom toggle state", "MissingStateDescription", "a11y.state.not_exposed") { CustomToggleScenario() },
)
