// ============================================================================
// InteropActivity.kt — host for the mixed View/Compose interop scenarios.
//
// An AppCompatActivity (so, a FragmentActivity) whose only content is an
// InteropFragment. The scenario is picked by the "scenario" intent extra so a
// script can drive every case with `am start`:
//
//   adb shell am start -n com.oberkfell.a11yprobe/.InteropActivity --es scenario S3
//
// Scenario ids (S1..S6, D1, D2) are listed in InteropFragment.kt.
// ============================================================================
package com.oberkfell.a11yprobe

import android.os.Bundle
import android.widget.FrameLayout
import androidx.appcompat.app.AppCompatActivity

class InteropActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val container = FrameLayout(this).apply { id = R.id.interop_container }
        setContentView(container)
        val scenario = interopScenario(intent.getStringExtra(EXTRA_SCENARIO))
            ?: INTEROP_SCENARIOS.first()
        title = scenario.title
        if (savedInstanceState == null) {
            supportFragmentManager.beginTransaction()
                .replace(R.id.interop_container, InteropFragment.newInstance(scenario.id), "interop_${scenario.id}")
                .commit()
        }
    }

    companion object {
        const val EXTRA_SCENARIO = "scenario"
    }
}
