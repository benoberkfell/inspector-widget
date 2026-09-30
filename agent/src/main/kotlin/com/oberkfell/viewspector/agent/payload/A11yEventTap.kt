/*
 * ViewSpector — payload :: accessibility event tap.
 *
 * Records the accessibility events the app sends (what TalkBack receives: focus moves and
 * clears, content and pane changes, scrolls, announcements, input focus) into a ring buffer of
 * [CAPACITY] records, so a host driving TalkBack can see what happened during a step and
 * long-poll for the next focus move instead of re-reading the tree (A11yFocus.kt).
 *
 * HOW: an event travels up its source's parent chain to the window's ViewRootImpl
 * (View.sendAccessibilityEventUncheckedInternal -> ViewGroup.requestSendAccessibilityEvent ->
 * ViewRootImpl.requestSendAccessibilityEvent -> AccessibilityManager). On the way the window
 * ROOT's ViewGroup.onRequestSendAccessibilityEvent consults the root's AccessibilityDelegate, so
 * one forwarding delegate per root sees every descendant event, Compose virtual nodes included
 * (AndroidComposeView sends through view.parent.requestSendAccessibilityEvent). The root's own
 * events skip that hook (its parent is the ViewRootImpl) and arrive through the delegate's
 * sendAccessibilityEventUnchecked. Every delegate method forwards to the delegate the app had
 * (or the platform default when it had none), so the app behaves the same.
 *
 * SIDE EFFECT: View.isImportantForAccessibility() counts "has a delegate", so a tapped root
 * reports itself important. A window root is always in the tree a service fetches, so
 * TalkBack's traversal does not change, but a dump would: dump_a11y runs inside [withoutTap].
 *
 * LIFECYCLE: installed lazily (the first A11yFocus / A11yAct), re-installed on roots that appear
 * later (every A11yFocus read and each long-poll tick re-scans), and removed by [shutdown]
 * (Server.run's finally: SHUTDOWN, idle stop): each root gets back exactly the delegate it had,
 * null included. A root whose delegate the app replaced since is left alone.
 *
 * THREADING: install / uninstall / record run on the main thread (events are sent there); the
 * buffer is guarded by [lock], which the server thread waits on during a long-poll.
 */
package com.oberkfell.viewspector.agent.payload

import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.util.Log
import android.view.View
import android.view.ViewGroup
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityNodeProvider
import com.oberkfell.viewspector.proto.ViewInspection
import java.util.WeakHashMap

object A11yEventTap {

    private const val TAG = "ViewSpector"

    /** Ring buffer size (records). */
    const val CAPACITY = 512

    private const val MAX_TEXT = 80

    // AccessibilityEvent.CONTENT_CHANGE_TYPE_PANE_TITLE | PANE_APPEARED | PANE_DISAPPEARED.
    private const val PANE_CHANGES = 0x8 or 0x10 or 0x20

    /** One recorded event. Strings are interned when a response is built ([toProto]). */
    class Record(
        val seq: Long,
        val uptimeMs: Long,
        val type: Int,
        val rootViewId: Long,
        val hostViewId: Long,
        val virtualId: Int,
        val contentChangeTypes: Int,
        val scrollDeltaX: Int,
        val scrollDeltaY: Int,
        val fromIndex: Int,
        val toIndex: Int,
        val itemCount: Int,
        val scrollX: Int,
        val scrollY: Int,
        val maxScrollX: Int,
        val maxScrollY: Int,
        val text: String?,
        val paneTitle: String?,
        val className: String?,
        val action: Int,
        val hostClass: String?,
    )

    /** The fields of an event, read on the main thread before the seq is assigned. */
    private class Fields(
        val type: Int, val rootViewId: Long, val hostViewId: Long, val virtualId: Int,
        val contentChangeTypes: Int, val scrollDeltaX: Int, val scrollDeltaY: Int,
        val fromIndex: Int, val toIndex: Int, val itemCount: Int, val scrollX: Int, val scrollY: Int,
        val maxScrollX: Int, val maxScrollY: Int, val text: String?, val paneTitle: String?,
        val className: String?, val action: Int, val hostClass: String?,
    )

    /** Buffered records after some seq, up to [seq] (the newest a read saw). */
    class Snapshot(val events: List<Record>, val seq: Long, val dropped: Long)

    // --- buffer, guarded by lock -------------------------------------------------------
    private val lock = Object()
    private val ring = arrayOfNulls<Record>(CAPACITY)
    private var lastSeq = 0L
    private var lastFocusSeq = 0L
    private var lastEventNanos = 0L

    @Volatile
    private var closed = false

    @Volatile
    private var loggedRecordFailure = false

    // --- installed taps, main thread only ----------------------------------------------
    private val taps = WeakHashMap<View, Tap>()

    /** Taps left by an earlier payload that [ensureInstalled] replaced (they were not removed). */
    var foreignUnwrapped = 0
        private set

    /**
     * The forwarding delegate. [original] is the root's delegate before the tap (null = the
     * platform default), restored on uninstall. It keeps no reference to its root (the host
     * passed to each call is the root), so [taps] stays weak.
     */
    private class Tap(val original: View.AccessibilityDelegate?) :
        View.AccessibilityDelegate() {

        override fun onRequestSendAccessibilityEvent(
            host: ViewGroup,
            child: View,
            event: AccessibilityEvent,
        ): Boolean {
            val propagate = if (original != null) {
                original.onRequestSendAccessibilityEvent(host, child, event)
            } else {
                super.onRequestSendAccessibilityEvent(host, child, event)
            }
            // Only what goes on to the ViewRootImpl reaches an accessibility service.
            if (propagate) record(host, event)
            return propagate
        }

        override fun sendAccessibilityEventUnchecked(host: View, event: AccessibilityEvent) {
            // The root's own event: populated (source, text) inside the forward. Read it
            // afterwards on API 33+, where AccessibilityEvent.recycle() is a no-op; before that
            // the system may recycle it on the way out, so read what is there up front.
            val early = Build.VERSION.SDK_INT < 33
            if (early) record(host, event)
            if (original != null) {
                original.sendAccessibilityEventUnchecked(host, event)
            } else {
                super.sendAccessibilityEventUnchecked(host, event)
            }
            if (!early) record(host, event)
        }

        override fun sendAccessibilityEvent(host: View, eventType: Int) {
            if (original != null) original.sendAccessibilityEvent(host, eventType)
            else super.sendAccessibilityEvent(host, eventType)
        }

        override fun dispatchPopulateAccessibilityEvent(host: View, event: AccessibilityEvent): Boolean =
            if (original != null) original.dispatchPopulateAccessibilityEvent(host, event)
            else super.dispatchPopulateAccessibilityEvent(host, event)

        override fun onPopulateAccessibilityEvent(host: View, event: AccessibilityEvent) {
            if (original != null) original.onPopulateAccessibilityEvent(host, event)
            else super.onPopulateAccessibilityEvent(host, event)
        }

        override fun onInitializeAccessibilityEvent(host: View, event: AccessibilityEvent) {
            if (original != null) original.onInitializeAccessibilityEvent(host, event)
            else super.onInitializeAccessibilityEvent(host, event)
        }

        override fun onInitializeAccessibilityNodeInfo(host: View, info: AccessibilityNodeInfo) {
            if (original != null) original.onInitializeAccessibilityNodeInfo(host, info)
            else super.onInitializeAccessibilityNodeInfo(host, info)
        }

        override fun addExtraDataToAccessibilityNodeInfo(
            host: View,
            info: AccessibilityNodeInfo,
            extraDataKey: String,
            arguments: Bundle?,
        ) {
            if (original != null) original.addExtraDataToAccessibilityNodeInfo(host, info, extraDataKey, arguments)
            else super.addExtraDataToAccessibilityNodeInfo(host, info, extraDataKey, arguments)
        }

        override fun performAccessibilityAction(host: View, action: Int, args: Bundle?): Boolean =
            if (original != null) original.performAccessibilityAction(host, action, args)
            else super.performAccessibilityAction(host, action, args)

        override fun getAccessibilityNodeProvider(host: View): AccessibilityNodeProvider? =
            if (original != null) original.getAccessibilityNodeProvider(host)
            else super.getAccessibilityNodeProvider(host)
    }

    // ------------------------------------------------------------------ install / remove

    /** Tap every root in [roots] that has no live tap yet. Main thread. Returns how many were added. */
    fun ensureInstalled(roots: List<View>): Int {
        if (closed) return 0
        var added = 0
        for (root in roots) {
            val current = try {
                root.accessibilityDelegate
            } catch (t: Throwable) {
                Log.w(TAG, "event tap: getAccessibilityDelegate failed", t)
                continue
            }
            val existing = taps[root]
            if (existing != null && current === existing) continue
            val tap = Tap(unwrapForeign(current))
            try {
                root.accessibilityDelegate = tap
                taps[root] = tap
                added++
            } catch (t: Throwable) {
                Log.w(TAG, "event tap: setAccessibilityDelegate failed", t)
            }
        }
        return added
    }

    /** How many roots currently carry a live tap. Main thread. */
    fun installedCount(): Int = taps.entries.count { (root, tap) -> root.accessibilityDelegate === tap }

    /**
     * A tap left by an earlier injection's payload (the same class from another classloader)
     * forwards to the app's real delegate: take that one, so taps never stack.
     */
    private fun unwrapForeign(delegate: View.AccessibilityDelegate?): View.AccessibilityDelegate? {
        if (delegate == null || delegate is Tap || delegate.javaClass.name != Tap::class.java.name) {
            return delegate
        }
        return try {
            val f = delegate.javaClass.getDeclaredField("original").also { it.isAccessible = true }
            (f.get(delegate) as? View.AccessibilityDelegate).also {
                foreignUnwrapped++
                Log.w(TAG, "event tap: replaced a tap an earlier payload left on a window root")
            }
        } catch (t: Throwable) {
            Log.w(TAG, "event tap: could not unwrap an earlier payload's tap", t)
            delegate
        }
    }

    /** Give every tapped root back its own delegate. Main thread. Returns how many were restored. */
    fun uninstallAll(): Int {
        var restored = 0
        for ((root, tap) in taps.entries.toList()) {
            try {
                if (root.accessibilityDelegate === tap) {
                    root.accessibilityDelegate = tap.original
                    restored++
                }
            } catch (t: Throwable) {
                Log.w(TAG, "event tap: could not restore a root's delegate", t)
            }
        }
        taps.clear()
        return restored
    }

    /**
     * Run [block] with every tap lifted (the app's own delegates back in place), then put the
     * taps back. Main thread: nothing else runs on it meanwhile, so no event is missed.
     */
    fun <T> withoutTap(block: () -> T): T {
        val lifted = ArrayList<Pair<View, Tap>>()
        for ((root, tap) in taps.entries) {
            try {
                if (root.accessibilityDelegate === tap) {
                    root.accessibilityDelegate = tap.original
                    lifted.add(root to tap)
                }
            } catch (_: Throwable) {
            }
        }
        try {
            return block()
        } finally {
            for ((root, tap) in lifted) {
                try {
                    if (root.accessibilityDelegate === tap.original) root.accessibilityDelegate = tap
                } catch (_: Throwable) {
                }
            }
        }
    }

    /**
     * Stop recording, wake every long-poll, and restore the roots' delegates on the main thread.
     * Called by the server thread as the server stops; never throws. If the main thread is
     * stuck, the posted restore still runs once it frees up (it is posted not to be cancelled
     * on the timeout: a tap left installed would pin this payload's classloader).
     */
    fun shutdown() {
        closed = true
        synchronized(lock) { lock.notifyAll() }
        try {
            val restored = MainThread.run(timeoutMs = 2000, cancelOnTimeout = false) { uninstallAll() }
            if (restored > 0) Log.i(TAG, "event tap removed from $restored window root(s)")
        } catch (t: Throwable) {
            Log.w(TAG, "event tap: restoring the delegates did not finish in time; it runs when the main thread frees up", t)
        }
    }

    // ------------------------------------------------------------------ recording

    private fun record(root: View, event: AccessibilityEvent) {
        if (closed) return
        val f = try {
            fields(root, event)
        } catch (t: Throwable) {
            if (!loggedRecordFailure) {
                loggedRecordFailure = true
                Log.w(TAG, "event tap: could not read an event (further failures not logged)", t)
            }
            return
        }
        synchronized(lock) {
            val seq = ++lastSeq
            ring[((seq - 1) % CAPACITY).toInt()] = Record(
                seq, SystemClock.uptimeMillis(), f.type, f.rootViewId, f.hostViewId, f.virtualId,
                f.contentChangeTypes, f.scrollDeltaX, f.scrollDeltaY, f.fromIndex, f.toIndex,
                f.itemCount, f.scrollX, f.scrollY, f.maxScrollX, f.maxScrollY, f.text, f.paneTitle,
                f.className, f.action, f.hostClass,
            )
            if (f.type == AccessibilityEvent.TYPE_VIEW_ACCESSIBILITY_FOCUSED) lastFocusSeq = seq
            lastEventNanos = System.nanoTime()
            lock.notifyAll()
        }
    }

    private fun fields(root: View, event: AccessibilityEvent): Fields {
        val type = event.eventType
        var hostViewId = 0L
        var hostClass: String? = null
        var virtualId = A11yIds.HOST_VIEW_ID
        var sourceView: View? = null
        val packed = A11yViews.sourceNodeId(event)
        if (packed != null && !A11yIds.isUndefined(packed)) {
            virtualId = A11yIds.virtualIdOf(packed)
            A11yViews.viewFor(root, A11yIds.accessibilityViewIdOf(packed))?.let {
                hostViewId = ViewReflect.uniqueDrawingId(it)
                hostClass = it.javaClass.name
                // The View itself is the source; a virtual node's host View is not the field.
                if (virtualId == A11yIds.HOST_VIEW_ID) sourceView = it
            }
        }
        // A password field's event text is its content (TYPE_VIEW_TEXT_CHANGED): mask it like
        // every other text path (Redaction.kt). The event says so for a masked field (and a
        // Compose Password node); a visible-password field only by its View's input type.
        val secret = event.isPassword || sourceView?.let { Redaction.isPasswordView(it) } == true
        val text = textOf(event, secret)
        val changes = event.contentChangeTypes
        val pane = if (type == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED && (changes and PANE_CHANGES) != 0) {
            text
        } else {
            null
        }
        return Fields(
            type = type,
            rootViewId = ViewReflect.uniqueDrawingId(root),
            hostViewId = hostViewId,
            virtualId = virtualId,
            contentChangeTypes = changes,
            scrollDeltaX = event.scrollDeltaX,
            scrollDeltaY = event.scrollDeltaY,
            fromIndex = event.fromIndex,
            toIndex = event.toIndex,
            itemCount = event.itemCount,
            scrollX = event.scrollX,
            scrollY = event.scrollY,
            maxScrollX = event.maxScrollX,
            maxScrollY = event.maxScrollY,
            text = text,
            paneTitle = pane,
            className = event.className?.toString(),
            action = event.action,
            hostClass = hostClass,
        )
    }

    /**
     * The event's text (else its content description), at most [MAX_TEXT] chars; the text of
     * a [secret] (password) source masked. A masked field's own text is mostly dots already,
     * but not the character just typed (PasswordTransformationMethod shows it briefly), and a
     * visible-password field's is the plaintext.
     */
    private fun textOf(event: AccessibilityEvent, secret: Boolean): String? {
        val joined = event.text?.filter { !it.isNullOrEmpty() }?.joinToString(" ")
        val s = when {
            !joined.isNullOrEmpty() -> if (secret) Redaction.mask(joined) else joined
            else -> event.contentDescription?.toString()
        }
        if (s.isNullOrEmpty()) return null
        return if (s.length > MAX_TEXT) s.substring(0, MAX_TEXT) else s
    }

    // ------------------------------------------------------------------ reading (any thread)

    /** The newest recorded seq (0 = nothing recorded yet). */
    fun seq(): Long = synchronized(lock) { lastSeq }

    /** True once a TYPE_VIEW_ACCESSIBILITY_FOCUSED event newer than [afterSeq] was recorded. */
    fun focusAfter(afterSeq: Long): Boolean = synchronized(lock) { lastFocusSeq > afterSeq }

    /** True after [shutdown]. */
    fun isClosed(): Boolean = closed

    /**
     * Wait up to [timeoutMs] for a focus event newer than [afterSeq] (returns at once when there
     * is one, or when the tap closes). Returns [focusAfter]. Server thread.
     */
    fun awaitFocus(afterSeq: Long, timeoutMs: Long): Boolean {
        synchronized(lock) {
            if (lastFocusSeq > afterSeq || closed || timeoutMs <= 0) return lastFocusSeq > afterSeq
            try {
                lock.wait(timeoutMs)
            } catch (ie: InterruptedException) {
                Thread.currentThread().interrupt()
            }
            return lastFocusSeq > afterSeq
        }
    }

    /**
     * Wait until [quietMs] passed without any event, or until [untilNanos] (System.nanoTime).
     * Returns true when the quiet period was reached. Server thread.
     */
    fun awaitQuiet(quietMs: Long, untilNanos: Long): Boolean {
        val quietNanos = quietMs * 1_000_000L
        synchronized(lock) {
            while (!closed) {
                val now = System.nanoTime()
                val since = now - lastEventNanos
                if (lastEventNanos == 0L || since >= quietNanos) return true
                val left = minOf(quietNanos - since, untilNanos - now)
                if (left <= 0) return false
                try {
                    lock.wait(maxOf(1L, left / 1_000_000L))
                } catch (ie: InterruptedException) {
                    Thread.currentThread().interrupt()
                    return false
                }
            }
            return false
        }
    }

    /**
     * The buffered records with seq in (afterSeq, upTo], oldest first; the newest [max] only when
     * [max] > 0. [Snapshot.dropped] counts records in that range the buffer no longer holds.
     */
    fun eventsAfter(afterSeq: Long, max: Int, upTo: Long): Snapshot {
        synchronized(lock) {
            val newest = minOf(upTo, lastSeq)
            val oldestKept = maxOf(1L, lastSeq - CAPACITY + 1)
            val first = maxOf(afterSeq + 1, oldestKept)
            val dropped = maxOf(0L, minOf(oldestKept, newest + 1) - (afterSeq + 1))
            var start = first
            if (max > 0 && newest - start + 1 > max) start = newest - max + 1
            val out = ArrayList<Record>()
            var s = start
            while (s <= newest) {
                ring[((s - 1) % CAPACITY).toInt()]?.let { if (it.seq == s) out.add(it) }
                s++
            }
            return Snapshot(out, newest, dropped)
        }
    }

    /** The wire form of [r], interning its strings into [strings]. */
    fun toProto(r: Record, strings: StringTable): ViewInspection.A11yEventRecord =
        ViewInspection.A11yEventRecord.newBuilder()
            .setSeq(r.seq)
            .setUptimeMs(r.uptimeMs)
            .setType(r.type)
            .setRootViewId(r.rootViewId)
            .setHostViewId(r.hostViewId)
            .setVirtualId(r.virtualId)
            .setContentChangeTypes(r.contentChangeTypes)
            .setScrollDeltaX(r.scrollDeltaX)
            .setScrollDeltaY(r.scrollDeltaY)
            .setFromIndex(r.fromIndex)
            .setToIndex(r.toIndex)
            .setItemCount(r.itemCount)
            .setScrollX(r.scrollX)
            .setScrollY(r.scrollY)
            .setMaxScrollX(r.maxScrollX)
            .setMaxScrollY(r.maxScrollY)
            .setText(strings.intern(r.text))
            .setPaneTitle(strings.intern(r.paneTitle))
            .setClassName(strings.intern(r.className))
            .setAction(r.action)
            .setHostClass(strings.intern(r.hostClass))
            .build()
}
