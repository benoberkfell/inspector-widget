// ============================================================================
// TbViewActivity.kt — the TalkBack navigation corpus (classic-View half).
//
//   adb shell am start -S -W -n com.oberkfell.a11yprobe/.TbViewActivity \
//       --es scenario tb_v4 --es variant bad        # or good (V6 also has bad_b)
//
// BAD and GOOD are separate screens. Expectations: host/tests/data/
// tb_corpus_expected.json. V12 mirrors Thunderbird's "N of M" off by one (an
// empty header item in the list), V13 AntennaPod's pager whose offscreen page
// is a WebView, V14 a DialogFragment whose first text is taken as its title
// (D1). Screens are built in code except V1's BAD layout (tb_v1_bad.xml),
// which needs <include> to duplicate an id.
// ============================================================================
package com.oberkfell.a11yprobe

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.util.TypedValue
import android.view.Gravity
import android.view.LayoutInflater
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.view.accessibility.AccessibilityNodeInfo
import android.webkit.WebView
import android.widget.Button
import android.widget.FrameLayout
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.PopupWindow
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.widget.SwitchCompat
import androidx.core.view.ViewCompat
import androidx.core.widget.NestedScrollView
import androidx.fragment.app.DialogFragment
import androidx.recyclerview.widget.DefaultItemAnimator
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import androidx.viewpager2.widget.ViewPager2

/** One classic-View TalkBack scenario (`--es scenario <id>`). */
class TbViewScenario(
    val id: String,
    val name: String,
    val title: String,
    val variants: List<String> = listOf("bad", "good"),
)

val TB_VIEW_SCENARIOS: List<TbViewScenario> = listOf(
    TbViewScenario("tb_v1", "stripe_order", "V1 Layout order vs reading order"),
    TbViewScenario("tb_v2", "traversal_cycle", "V2 traversalAfter cycle"),
    TbViewScenario("tb_v3", "link_to_unimportant", "V3 traversalBefore to a spacer"),
    TbViewScenario("tb_v4", "container_cd", "V4 A row with a description and a Switch"),
    TbViewScenario("tb_v5", "scrim", "V5 A card over a scrim"),
    TbViewScenario("tb_v6", "recycler_update", "V6 A RecyclerView that updates", listOf("bad", "bad_b", "good")),
    TbViewScenario("tb_v7", "custom_scroller", "V7 Rows moved by translationY"),
    TbViewScenario("tb_v8", "scroll_stall", "V8 A scroll that sends no event"),
    TbViewScenario("tb_v9", "model_baselines", "V9 Model baselines"),
    TbViewScenario("tb_v10", "popup_order", "V10 A popup's place in the order"),
    TbViewScenario("tb_v11", "activity_restore", "V11 Back to a folder list"),
    TbViewScenario("tb_v12", "list_header", "V12 A list with an empty header item"),
    TbViewScenario("tb_v13", "pager_webview", "V13 A pager with a WebView page"),
    TbViewScenario("tb_v14", "dialog_title", "V14 A dialog's title"),
)

fun tbViewScenario(id: String?): TbViewScenario? =
    TB_VIEW_SCENARIOS.firstOrNull { it.id.equals(id, ignoreCase = true) }

class TbViewActivity : AppCompatActivity() {

    private lateinit var variant: String
    private var stopProbe: (() -> Unit)? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val sc = tbViewScenario(intent.getStringExtra(MainActivity.EXTRA_SCENARIO)) ?: TB_VIEW_SCENARIOS.first()
        variant = intent.getStringExtra(MainActivity.EXTRA_VARIANT)?.lowercase()
            ?.takeIf { it in sc.variants } ?: "bad"
        val heading = "${sc.title.substringBefore(' ')} ${variant.uppercase().replace('_', ' ')}: ${sc.title.substringAfter(' ')}"
        setContentView(
            when (sc.id) {
                "tb_v1" -> v1StripeOrder(heading)
                "tb_v2" -> v2TraversalCycle(heading)
                "tb_v3" -> v3LinkToUnimportant(heading)
                "tb_v4" -> v4ContainerCd(heading)
                "tb_v5" -> v5Scrim(heading)
                "tb_v6" -> v6RecyclerUpdate(heading)
                "tb_v7" -> v7CustomScroller(heading)
                "tb_v8" -> v8ScrollStall(heading)
                "tb_v9" -> v9ModelBaselines(heading)
                "tb_v10" -> v10PopupOrder(heading)
                "tb_v11" -> v11ActivityRestore(heading)
                "tb_v12" -> v12ListHeader(heading)
                "tb_v13" -> v13PagerWebView(heading)
                else -> v14DialogTitle(heading)
            },
        )
    }

    override fun onDestroy() {
        stopProbe?.invoke()
        super.onDestroy()
    }

    private val bad get() = variant.startsWith("bad")

    // ------------------------------------------------------------------ helpers
    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    private fun column(title: String): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        fitsSystemWindows = true
        setPadding(dp(16), dp(8), dp(16), dp(8))
        addView(text(title, 22f).also { ViewCompat.setAccessibilityHeading(it, true) })
    }

    private fun text(s: String, sp: Float = 18f): TextView = TextView(this).apply {
        text = s
        setTextSize(TypedValue.COMPLEX_UNIT_SP, sp)
        setPadding(dp(8), dp(10), dp(8), dp(10))
    }

    private fun button(s: String): Button = Button(this).apply {
        text = s
        setOnClickListener { }
    }

    private fun rows(prefix: String, n: Int): List<TextView> = (1..n).map { i ->
        text("$prefix $i").apply {
            minHeight = dp(64)
            setOnClickListener { }
        }
    }

    // ------------------------------------------------------------------ V1
    private fun v1StripeOrder(title: String): View {
        val col = column(title)
        if (bad) {
            val form = LayoutInflater.from(this).inflate(R.layout.tb_v1_bad, col, false)
            val fields = form.findViewById<LinearLayout>(R.id.tb_v1_fields)
            (fields.getChildAt(0) as TextView).text = "Email"
            form.findViewById<FrameLayout>(R.id.tb_v1_phone_box).findViewById<TextView>(R.id.tb_v1_field).text = "Phone"
            col.addView(form)
        } else {
            col.addView(LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                addView(text("Name", 22f), LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
                addView(text("Ada Lovelace", 12f))
            })
            col.addView(button("Save"))
            col.addView(text("Email"))
            col.addView(text("Phone"))
        }
        return col
    }

    // ------------------------------------------------------------------ V2
    private fun v2TraversalCycle(title: String): View {
        val col = column(title)
        val a = text("Alpha").apply { id = View.generateViewId() }
        val b = text("Beta").apply { id = View.generateViewId() }
        val c = text("Gamma").apply { id = View.generateViewId() }
        listOf(a, b, c).forEach(col::addView)
        if (bad) {
            a.accessibilityTraversalAfter = b.id
            b.accessibilityTraversalAfter = a.id
        }
        return col
    }

    // ------------------------------------------------------------------ V3
    private fun v3LinkToUnimportant(title: String): View {
        val col = column(title)
        val spacer = View(this).apply {
            id = View.generateViewId()
            importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
        }
        col.addView(spacer, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(8)))
        val s1 = text("Step 1: pick a plan").apply { id = View.generateViewId() }
        val s2 = text("Step 2: pay")
        val s3 = text("Summary: read me first").apply { id = View.generateViewId() }
        listOf(s1, s2, s3).forEach(col::addView)
        // The author wants the summary read first. A link to a View TalkBack
        // never sees (the spacer) is dropped; a link to Step 1 works.
        s3.accessibilityTraversalBefore = if (bad) spacer.id else s1.id
        return col
    }

    // ------------------------------------------------------------------ V4
    @SuppressLint("SetTextI18n")
    private fun v4ContainerCd(title: String): View {
        val col = column(title)
        val sw = SwitchCompat(this).apply { isChecked = true }
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            minimumHeight = dp(64)
            addView(text("Wi-Fi"), LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            addView(sw)
        }
        if (bad) {
            row.contentDescription = "Settings row"
            row.setOnClickListener { }
        } else {
            sw.isClickable = false
            sw.isFocusable = false
            row.setOnClickListener { sw.toggle() }
        }
        col.addView(row)
        col.addView(button("Advanced"))
        return col
    }

    // ------------------------------------------------------------------ V5
    private fun v5Scrim(title: String): View {
        val frame = FrameLayout(this).apply { fitsSystemWindows = true }
        val background = column(title).apply {
            (1..8).forEach { addView(button("Background $it")) }
        }
        val scrim = View(this).apply {
            setBackgroundColor(Color.argb(0x99, 0, 0, 0))
            setOnClickListener { }
        }
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.WHITE)
            setPadding(dp(24), dp(24), dp(24), dp(24))
            addView(text("Choose an option", 20f).also { ViewCompat.setAccessibilityHeading(it, true) })
            addView(button("Option A"))
            addView(button("Option B"))
        }
        if (!bad) {
            background.importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS
            scrim.importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
            ViewCompat.setAccessibilityPaneTitle(card, "Choose an option")
        }
        frame.addView(background)
        frame.addView(scrim, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        frame.addView(card, FrameLayout.LayoutParams(dp(300), ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.CENTER))
        return frame
    }

    // ------------------------------------------------------------------ V6
    private data class Mail(val id: Long, val text: String)

    private class MailHolder(val tv: TextView) : RecyclerView.ViewHolder(tv)

    private fun mailView(ctx: Context): TextView = TextView(ctx).apply {
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 18f)
        setPadding(dp(16), dp(16), dp(16), dp(16))
        layoutParams = RecyclerView.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
        setOnClickListener { }
    }

    /** BAD: a plain adapter over a list; every change is notifyDataSetChanged (or notifyItemChanged). */
    private inner class PlainMailAdapter(val items: MutableList<Mail>) : RecyclerView.Adapter<MailHolder>() {
        override fun getItemCount() = items.size
        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int) = MailHolder(mailView(parent.context))
        override fun onBindViewHolder(holder: MailHolder, position: Int) { holder.tv.text = items[position].text }
    }

    /** GOOD: DiffUtil with stable ids and payloads, and no change animation. */
    private inner class DiffMailAdapter : ListAdapter<Mail, MailHolder>(object : DiffUtil.ItemCallback<Mail>() {
        override fun areItemsTheSame(a: Mail, b: Mail) = a.id == b.id
        override fun areContentsTheSame(a: Mail, b: Mail) = a == b
        override fun getChangePayload(a: Mail, b: Mail): Any = b.text
    }) {
        init { setHasStableIds(true) }
        override fun getItemId(position: Int) = getItem(position).id
        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int) = MailHolder(mailView(parent.context))
        override fun onBindViewHolder(holder: MailHolder, position: Int) { holder.tv.text = getItem(position).text }
    }

    private fun v6RecyclerUpdate(title: String): View {
        val col = column(title)
        var mails = (1L..50L).map { Mail(it, "Mail $it") }
        var next = 100L
        val list = RecyclerView(this).apply { layoutManager = LinearLayoutManager(this@TbViewActivity) }
        if (bad) {
            val items = mails.toMutableList()
            val adapter = PlainMailAdapter(items)
            list.adapter = adapter
            list.itemAnimator = DefaultItemAnimator()  // change animations on: a new holder per change
            stopProbe = TbProbe.listen { a ->
                when (a.action) {
                    "insert_top" -> { items.add(0, Mail(next, "New mail ${next++ - 99}")); adapter.notifyDataSetChanged() }
                    "shuffle" -> { items.shuffle(); adapter.notifyDataSetChanged() }
                    "change_item" -> items.getOrNull(a.index)?.let {
                        items[a.index] = it.copy(text = it.text + " (read)")
                        adapter.notifyItemChanged(a.index)
                    }
                    else -> adapter.notifyDataSetChanged()
                }
            }
        } else {
            val adapter = DiffMailAdapter()
            list.adapter = adapter
            (list.itemAnimator as? DefaultItemAnimator)?.supportsChangeAnimations = false
            adapter.submitList(mails)
            stopProbe = TbProbe.listen { a ->
                mails = when (a.action) {
                    "insert_top" -> listOf(Mail(next, "New mail ${next++ - 99}")) + mails
                    "shuffle" -> mails.shuffled()
                    "change_item" -> mails.mapIndexed { i, m -> if (i == a.index) m.copy(text = m.text + " (read)") else m }
                    else -> mails.toList()
                }
                adapter.submitList(mails)
            }
        }
        col.addView(list, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        return col
    }

    // ------------------------------------------------------------------ V7
    @SuppressLint("ClickableViewAccessibility")
    private fun v7CustomScroller(title: String): View {
        val col = column(title)
        val content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            rows("Log entry", 30).forEach(::addView)
        }
        if (bad) {
            // A hand-rolled scroller: drags move the rows with translationY and
            // expose no scroll actions, so the clipped rows are invisible to TalkBack.
            val box = FrameLayout(this).apply { clipChildren = true }
            box.addView(content)
            var lastY = 0f
            box.setOnTouchListener { _, e ->
                when (e.actionMasked) {
                    MotionEvent.ACTION_DOWN -> lastY = e.rawY
                    MotionEvent.ACTION_MOVE -> {
                        content.translationY = (content.translationY + e.rawY - lastY).coerceAtMost(0f)
                        lastY = e.rawY
                    }
                }
                true
            }
            col.addView(box, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        } else {
            col.addView(NestedScrollView(this).apply { addView(content) },
                LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        }
        return col
    }

    // ------------------------------------------------------------------ V8
    private fun v8ScrollStall(title: String): View {
        val col = column(title)
        val content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            rows("Song", 30).forEach(::addView)
        }
        if (bad) {
            // Says it scrolls (a ScrollView with ACTION_SCROLL_FORWARD) but moves its
            // children with translationY and never sends TYPE_VIEW_SCROLLED.
            val box = FrameLayout(this).apply { clipChildren = true }
            box.addView(content)
            ViewCompat.setAccessibilityDelegate(box, object : androidx.core.view.AccessibilityDelegateCompat() {
                override fun onInitializeAccessibilityNodeInfo(host: View, info: androidx.core.view.accessibility.AccessibilityNodeInfoCompat) {
                    super.onInitializeAccessibilityNodeInfo(host, info)
                    info.className = ScrollView::class.java.name
                    info.isScrollable = true
                    if (content.translationY > -(content.height - host.height).toFloat()) {
                        info.addAction(androidx.core.view.accessibility.AccessibilityNodeInfoCompat.AccessibilityActionCompat.ACTION_SCROLL_FORWARD)
                    }
                }

                override fun performAccessibilityAction(host: View, action: Int, args: Bundle?): Boolean {
                    if (action == AccessibilityNodeInfo.ACTION_SCROLL_FORWARD) {
                        content.translationY -= host.height * 0.8f
                        return true
                    }
                    return super.performAccessibilityAction(host, action, args)
                }
            })
            col.addView(box, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        } else {
            col.addView(ScrollView(this).apply { addView(content) },
                LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        }
        return col
    }

    // ------------------------------------------------------------------ V9
    private fun v9ModelBaselines(title: String): View {
        val col = column(title)
        // An ImageButton without a label (BAD) / with one (GOOD): TalkBack says "Button".
        col.addView(ImageButton(this).apply {
            setImageResource(android.R.drawable.ic_menu_share)
            if (!bad) contentDescription = "Share"
            setOnClickListener { }
        })
        // A clickable container whose children are all focusable: a silent container;
        // its Buttons are the stops.
        col.addView(LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setOnClickListener { }
            addView(button("Reply"))
            addView(button("Forward"))
        })
        // A focusable container whose text children are INVISIBLE (BAD: it still
        // speaks them, from nothing on screen) / GONE (GOOD).
        col.addView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            isFocusable = true
            minimumHeight = dp(48)
            addView(text("Hidden hint").apply { visibility = if (bad) View.INVISIBLE else View.GONE })
            if (!bad) contentDescription = "Status: all synced"
        })
        col.addView(text("Last synced 5 minutes ago"))
        return col
    }

    // ------------------------------------------------------------------ V10
    private fun v10PopupOrder(title: String): View {
        val col = column(title)
        rows("Task", 6).forEach(col::addView)
        col.post {
            val tip = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setBackgroundColor(Color.rgb(0xFF, 0xF3, 0xC4))
                setPadding(dp(16), dp(16), dp(16), dp(16))
                addView(text("Tip: swipe a task to archive it"))
                addView(button("Got it"))
            }
            // BAD a non-focusable popup: TalkBack orders windows by position, so it
            // is read after the tasks above AND below it. GOOD a focusable popup.
            PopupWindow(tip, dp(300), ViewGroup.LayoutParams.WRAP_CONTENT, !bad)
                .showAtLocation(col, Gravity.CENTER, 0, 0)
        }
        return col
    }

    // ------------------------------------------------------------------ V11
    private fun v11ActivityRestore(title: String): View {
        val col = column(title)
        val rows = rows("Folder", 12)
        rows.forEachIndexed { i, row ->
            row.setOnClickListener {
                startActivity(Intent(this, TbViewDetailActivity::class.java)
                    .putExtra("folder", i + 1).putExtra(MainActivity.EXTRA_VARIANT, variant))
                if (bad) finish()  // BAD: the list is re-created when the user comes back
            }
            col.addView(row)
        }
        return col
    }

    // ------------------------------------------------------------------ V12
    private class HeaderMailAdapter(val header: Boolean, val count: Int, val make: (Context) -> TextView) :
        RecyclerView.Adapter<RecyclerView.ViewHolder>() {
        override fun getItemCount() = count + if (header) 1 else 0
        override fun getItemViewType(position: Int) = if (header && position == 0) 1 else 0
        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): RecyclerView.ViewHolder =
            if (viewType == 1) object : RecyclerView.ViewHolder(View(parent.context).apply {
                // Thunderbird's list header: present in the adapter, empty on screen.
                layoutParams = RecyclerView.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 1)
            }) {} else object : RecyclerView.ViewHolder(make(parent.context)) {}

        override fun onBindViewHolder(holder: RecyclerView.ViewHolder, position: Int) {
            (holder.itemView as? TextView)?.text = "Message ${position + if (header) 0 else 1}"
        }
    }

    private fun v12ListHeader(title: String): View {
        val col = column(title)
        col.addView(RecyclerView(this).apply {
            layoutManager = LinearLayoutManager(this@TbViewActivity)
            adapter = HeaderMailAdapter(bad, 20) { mailView(it) }
        }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        return col
    }

    // ------------------------------------------------------------------ V13
    private fun v13PagerWebView(title: String): View {
        val col = column(title)
        val pager = ViewPager2(this)
        pager.adapter = object : RecyclerView.Adapter<RecyclerView.ViewHolder>() {
            override fun getItemCount() = 3
            override fun getItemViewType(position: Int) = position
            override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): RecyclerView.ViewHolder {
                val page: View = if (viewType == 1) {
                    WebView(parent.context).apply {
                        loadDataWithBaseURL(null, SHOW_NOTES_HTML, "text/html", "utf-8", null)
                    }
                } else {
                    LinearLayout(parent.context).apply {
                        orientation = LinearLayout.VERTICAL
                        addView(text(if (viewType == 0) "Episode 12: Accessibility" else "Chapters", 20f))
                        addView(button(if (viewType == 0) "Play episode" else "Chapter 1"))
                    }
                }
                page.layoutParams = ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
                return object : RecyclerView.ViewHolder(page) {}
            }

            override fun onBindViewHolder(holder: RecyclerView.ViewHolder, position: Int) {}
        }
        // Keep the neighbouring page (the WebView) attached, as AntennaPod does.
        pager.offscreenPageLimit = 1
        if (!bad) {
            val recycler = pager.getChildAt(0) as RecyclerView
            fun hideOffscreen() {
                for (i in 0 until pager.adapter!!.itemCount) {
                    recycler.findViewHolderForAdapterPosition(i)?.itemView?.importantForAccessibility =
                        if (i == pager.currentItem) View.IMPORTANT_FOR_ACCESSIBILITY_AUTO
                        else View.IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS
                }
            }
            pager.registerOnPageChangeCallback(object : ViewPager2.OnPageChangeCallback() {
                override fun onPageSelected(position: Int) = hideOffscreen()
            })
            recycler.addOnChildAttachStateChangeListener(object : RecyclerView.OnChildAttachStateChangeListener {
                override fun onChildViewAttachedToWindow(view: View) { recycler.post { hideOffscreen() } }
                override fun onChildViewDetachedFromWindow(view: View) {}
            })
        }
        col.addView(pager, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(420)))
        col.addView(button("Download"))
        return col
    }

    // ------------------------------------------------------------------ V14
    private fun v14DialogTitle(title: String): View {
        val col = column(title)
        col.addView(text("A photo from Tuesday"))
        col.addView(button("Share item").apply {
            setOnClickListener { TbShareDialog.newInstance(bad).show(supportFragmentManager, "tb_v14") }
        })
        return col
    }
}

/**
 * V14: a DialogFragment. BAD has no window title, so TalkBack takes its first text
 * ("Share item", also a heading) as the title, skips it, and lands on the next
 * stop, an unlabelled icon button. GOOD gives the window its own title and labels
 * the buttons, so TalkBack starts on the heading.
 */
class TbShareDialog : DialogFragment() {
    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
        val bad = requireArguments().getBoolean("bad")
        val ctx = requireContext()
        val pad = (24 * resources.displayMetrics.density).toInt()
        return LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
            addView(TextView(ctx).apply {
                text = "Share item"
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 20f)
                ViewCompat.setAccessibilityHeading(this, true)
            })
            addView(LinearLayout(ctx).apply {
                orientation = LinearLayout.HORIZONTAL
                for ((icon, label) in listOf(android.R.drawable.ic_menu_send to "Send", android.R.drawable.ic_menu_save to "Save")) {
                    addView(ImageButton(ctx).apply {
                        setImageResource(icon)
                        if (!bad) contentDescription = label
                        setOnClickListener { }
                    })
                }
            })
            addView(Button(ctx).apply { text = "Close"; setOnClickListener { dismiss() } })
        }
    }

    override fun onStart() {
        super.onStart()
        if (!requireArguments().getBoolean("bad")) dialog?.setTitle("Share options")
    }

    companion object {
        fun newInstance(bad: Boolean) = TbShareDialog().apply { arguments = Bundle().apply { putBoolean("bad", bad) } }
    }
}

/** V11's second screen. BAD: back re-creates the list (a new Activity), so TalkBack starts at its top. */
class TbViewDetailActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val folder = intent.getIntExtra("folder", 1)
        val bad = intent.getStringExtra(MainActivity.EXTRA_VARIANT)?.startsWith("bad") ?: true
        title = "Folder $folder"
        setContentView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            fitsSystemWindows = true
            val pad = (16 * resources.displayMetrics.density).toInt()
            setPadding(pad, pad, pad, pad)
            addView(TextView(this@TbViewDetailActivity).apply {
                text = "Folder $folder"
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 22f)
                ViewCompat.setAccessibilityHeading(this, true)
            })
            addView(TextView(this@TbViewDetailActivity).apply { text = "No messages." })
        })
        if (bad) {
            onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
                override fun handleOnBackPressed() {
                    startActivity(Intent(this@TbViewDetailActivity, TbViewActivity::class.java)
                        .putExtra(MainActivity.EXTRA_SCENARIO, "tb_v11").putExtra(MainActivity.EXTRA_VARIANT, "bad"))
                    finish()
                }
            })
        }
    }
}
