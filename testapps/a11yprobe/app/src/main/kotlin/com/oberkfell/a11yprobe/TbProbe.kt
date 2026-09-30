// ============================================================================
// TbProbe.kt — list mutations on demand, for TalkBack "survive" checks.
//
// The host's tb_scenario survive (mutate=probe:<action>) sends
//
//   adb shell am broadcast -a com.oberkfell.a11yprobe.TB_PROBE -p com.oberkfell.a11yprobe \
//       --es action insert_top|shuffle|notify_all|change_item|refresh [--ei index K]
//
// TbProbeReceiver is registered in the DEBUG manifest only (src/debug), so a
// release build exports nothing. It forwards the action to whichever list
// screen is showing (TalkBackScenarios C6, TbViewActivity V6, TbHybrid H2):
// they subscribe with [TbProbe.listen] while visible.
// ============================================================================
package com.oberkfell.a11yprobe

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.Looper
import android.util.Log

/** A probe action: what to do to the list, and the item index for change_item. */
data class TbProbeAction(val action: String, val index: Int)

object TbProbe {
    const val ACTION = "com.oberkfell.a11yprobe.TB_PROBE"
    private val listeners = mutableListOf<(TbProbeAction) -> Unit>()
    private val main = Handler(Looper.getMainLooper())

    /** Subscribe while a list screen is visible; call the returned function to stop. */
    fun listen(listener: (TbProbeAction) -> Unit): () -> Unit {
        synchronized(listeners) { listeners += listener }
        return { synchronized(listeners) { listeners -= listener } }
    }

    fun dispatch(action: TbProbeAction) {
        val snapshot = synchronized(listeners) { listeners.toList() }
        Log.i("A11yProbe", "TB_PROBE $action -> ${snapshot.size} listener(s)")
        main.post { snapshot.forEach { it(action) } }
    }
}

class TbProbeReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != TbProbe.ACTION) return
        TbProbe.dispatch(
            TbProbeAction(intent.getStringExtra("action") ?: "refresh", intent.getIntExtra("index", 7))
        )
    }
}
