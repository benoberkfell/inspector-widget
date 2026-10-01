// ============================================================================
// ViewDefaultsActivity.kt — bare widgets, for the host's property-defaults table.
//
// Nothing here sets an attribute beyond its size, so every property the agent
// reads is the widget's default under this app's theme (Material3 DayNight).
// Two columns of the same widgets:
//   #defaults_bare      constructed in code: the framework classes
//                       (android.widget.TextView, Button, Switch, ...)
//   #defaults_inflated  inflated from view_defaults_inflated.xml with no
//                       attributes: the classes AppCompat/Material substitute
//                       (AppCompatTextView, MaterialButton, ...)
// host/inspector_widget/normalize_defaults.py (STATIC_VIEW_DEFAULTS) is
// re-recorded from a capture of this screen (WP L1).
//
//   adb shell am start -n com.oberkfell.a11yprobe/.ViewDefaultsActivity
//   (or MainActivity --es scenario view_defaults)
// ============================================================================
package com.oberkfell.a11yprobe

import android.os.Bundle
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

class ViewDefaultsActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val root = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        val bare = LinearLayout(this).apply {
            id = R.id.defaults_bare
            orientation = LinearLayout.VERTICAL
        }
        val size = (48 * resources.displayMetrics.density).toInt()
        fun add(v: View) = bare.addView(v, LinearLayout.LayoutParams(size * 3, size))
        add(View(this))
        add(FrameLayout(this))
        add(LinearLayout(this))
        add(ScrollView(this))
        add(TextView(this))
        add(ImageView(this))
        add(Button(this))
        add(EditText(this))
        add(CheckBox(this))
        @Suppress("UseSwitchCompatOrMaterialCode")
        add(Switch(this))
        root.addView(bare, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1f))
        val inflated = layoutInflater.inflate(R.layout.view_defaults_inflated, root, false)
        root.addView(inflated, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1f))
        setContentView(root)
    }
}
