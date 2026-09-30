// ============================================================================
// ViewScenarioActivity.kt — classic-View (XML) screen, the second extraction
// path (testapps.md §5). Inflates activity_view_scenarios.xml to exercise
// TreeBuilder.kt / Properties.kt and the AccessibilityNodeInfo fields the agent
// reflects: getContentDescription, getStateDescription, isHeading,
// getRoleDescription, bounds. GOOD/BAD pairs map onto the same lint rules the
// Compose path covers, confirming the host's View path detects the same defects.
// The BAD views are deliberate defects; see activity_view_scenarios.xml.
//
//   adb shell am start -n com.oberkfell.a11yprobe/.ViewScenarioActivity
// ============================================================================
package com.oberkfell.a11yprobe

import android.os.Bundle
import android.view.View
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.AccessibilityDelegateCompat
import androidx.core.view.ViewCompat
import androidx.core.view.accessibility.AccessibilityNodeInfoCompat
import com.oberkfell.a11yprobe.databinding.ActivityViewScenariosBinding

class ViewScenarioActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val binding = ActivityViewScenariosBinding.inflate(layoutInflater)
        setContentView(binding.root)

        // GOOD: programmatic state description on the labeled switch, kept in
        // sync with the checked state. (badSwitch deliberately has none.)
        applyStateDescription(binding.goodSwitch.isChecked, binding)
        binding.goodSwitch.setOnCheckedChangeListener { _, checked ->
            applyStateDescription(checked, binding)
        }

        // GOOD: heading flag for TalkBack heading navigation.
        // (badHeading: large text, NOT marked as a heading.)
        ViewCompat.setAccessibilityHeading(binding.goodHeading, true)

        // GOOD: custom clickable view gets a Button roleDescription + clickable.
        // (badCustomClickable: clickable, no role.)
        ViewCompat.setAccessibilityDelegate(
            binding.goodCustomClickable,
            object : AccessibilityDelegateCompat() {
                override fun onInitializeAccessibilityNodeInfo(
                    host: View,
                    info: AccessibilityNodeInfoCompat,
                ) {
                    super.onInitializeAccessibilityNodeInfo(host, info)
                    info.roleDescription = "Button"
                    info.isClickable = true
                }
            }
        )

        // GOOD: the labeled EditText is associated with its label via labelFor
        // in XML; nothing to wire here. (badEditText has no labelFor partner.)
    }

    private fun applyStateDescription(
        checked: Boolean,
        binding: ActivityViewScenariosBinding,
    ) {
        ViewCompat.setStateDescription(
            binding.goodSwitch,
            if (checked) "Notifications on" else "Notifications off"
        )
    }
}
