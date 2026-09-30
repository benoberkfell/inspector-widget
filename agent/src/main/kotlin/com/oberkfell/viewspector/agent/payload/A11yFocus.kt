/*
 * ViewSpector — payload :: where accessibility focus is (A11yFocusCommand), and acting on a node
 * (A11yActCommand).
 *
 * READ (main thread, one hop; target < 5 ms):
 *   Each window's ViewRootImpl records the accessibility-focused View
 *   (getAccessibilityFocusedHost(), @UnsupportedAppUsage, ViewRootImpl.java:6317) and, for a
 *   virtual node, a copy of that node (getAccessibilityFocusedVirtualView(), :6328), refreshed by
 *   requestSendAccessibilityEvent on every TYPE_VIEW_ACCESSIBILITY_FOCUSED from a provider. This
 *   is exactly what AccessibilityInteractionController.findFocus serves TalkBack: the host View,
 *   or provider.createAccessibilityNodeInfo(<the recorded virtual id>). The node is rebuilt the
 *   same way here (locally: screen px, no connection) and mapped like a dump node
 *   (AccessibilityInspector.snapshot), identified by the host-key contract (A11yIds). Top-most
 *   window first. When the ViewRootImpl members are unreachable, the fallback asks the
 *   in-process query connection: root.createAccessibilityNodeInfo() +
 *   setQueryFromAppProcessEnabled + findFocus(FOCUS_ACCESSIBILITY) (API 34+).
 *   stale = the window still records a node that no longer resolves (the provider returns null
 *   for the virtual id, or the View is detached / not shown): ViewRootImpl clears such focus on
 *   the next subtree change. "No focus in the app's windows" is a valid answer (TalkBack is
 *   off, or focus is in the IME / SystemUI).
 *   Input focus (optional): the focused window's findFocus() View, or the virtual node its
 *   provider reports for FOCUS_INPUT, as the controller does.
 *
 * LONG-POLL ([await], SERVER thread, never the main thread): wait for an event-tap
 * TYPE_VIEW_ACCESSIBILITY_FOCUSED newer than after_seq, then for quiet_ms without any event,
 * then read. Every POLL_MS it also re-scans the roots on the main thread (tapping new windows,
 * e.g. a dialog that just opened) and compares the focus identity with the one at the start,
 * so focus moving into a window the tap did not cover yet still ends the wait.
 *
 * ACT (main thread): performAction through the in-process query connection, the path
 * AccessibilityInteractionController serves a service's performAction on (API 34+); the direct
 * View / provider call otherwise. Accessibility focus actions need touch exploration (a screen
 * reader) on: View.requestAccessibilityFocus (View.java:15160) and Compose refuse otherwise, so
 * they are refused up front with an explanation.
 */
package com.oberkfell.viewspector.agent.payload

import android.accessibilityservice.AccessibilityServiceInfo
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.view.View
import android.view.accessibility.AccessibilityManager
import android.view.accessibility.AccessibilityNodeInfo
import com.oberkfell.viewspector.proto.ViewInspection
import java.lang.reflect.Field
import java.lang.reflect.Method

object A11yFocus {

    private const val TAG = "ViewSpector"
    private const val HOST_VIEW_ID = A11yIds.HOST_VIEW_ID

    private const val MAX_WAIT_MS = 30_000
    private const val MAX_QUIET_MS = 5_000
    private const val MAX_SUBTREE_DEPTH = 16
    private const val MAX_SUBTREE_NODES = 400
    private const val POLL_MS = 100L

    // ------------------------------------------------------------ reflective handles

    private val viewRootImplClass: Class<*>? = try {
        Class.forName("android.view.ViewRootImpl")
    } catch (t: Throwable) {
        Log.w(TAG, "android.view.ViewRootImpl not loadable; accessibility focus comes from findFocus", t)
        null
    }

    private val focusedHostM: Method? = method("getAccessibilityFocusedHost")
    private val focusedVirtualM: Method? = method("getAccessibilityFocusedVirtualView")
    private val focusedHostF: Field? = if (focusedHostM == null) field("mAccessibilityFocusedHost") else null
    private val focusedVirtualF: Field? =
        if (focusedVirtualM == null) field("mAccessibilityFocusedVirtualView") else null

    /** @hide AccessibilityNodeInfo.setSealed(boolean): performAction needs a sealed node. */
    private val setSealedM: Method? = try {
        AccessibilityNodeInfo::class.java.getDeclaredMethod("setSealed", Boolean::class.javaPrimitiveType)
            .also { it.isAccessible = true }
    } catch (t: Throwable) {
        Log.w(TAG, "AccessibilityNodeInfo.setSealed not reachable; actions use the direct path", t)
        null
    }

    private fun method(name: String): Method? = try {
        viewRootImplClass?.getDeclaredMethod(name)?.also { it.isAccessible = true }
    } catch (t: Throwable) {
        Log.w(TAG, "ViewRootImpl.$name not reachable", t)
        null
    }

    private fun field(name: String): Field? = try {
        viewRootImplClass?.getDeclaredField(name)?.also { it.isAccessible = true }
    } catch (t: Throwable) {
        Log.w(TAG, "ViewRootImpl.$name not reachable", t)
        null
    }

    private val viewRootReachable: Boolean
        get() = viewRootImplClass != null &&
            (focusedHostM != null || focusedHostF != null) &&
            (focusedVirtualM != null || focusedVirtualF != null)

    // ------------------------------------------------------------ results

    /** What one read produced. [seq] is the event-tap seq at the moment of the read. */
    class Read(
        val a11y: ViewInspection.A11yFocus?,
        val input: ViewInspection.A11yFocus?,
        val seq: Long,
        val readUs: Int,
        val touchExploration: Boolean,
        val servicesEnabled: Boolean,
        val diagnostics: String,
    )

    /** How a long-poll ended. */
    class Wait(val focusEvent: Boolean, val timedOut: Boolean, val waitedMs: Int, val note: String?)

    /** What an action did. */
    class Act(
        val performed: Boolean,
        val error: String?,
        val actionId: Int,
        val seqBefore: Long,
        val after: ViewInspection.A11yFocus?,
        val seq: Long,
        val diagnostics: String,
    )

    /** The identity of the accessibility focus (root, host, virtual id); for change detection. */
    private data class FocusId(val root: Long, val host: Long, val virtualId: Int)

    // ------------------------------------------------------------ long-poll (server thread)

    /**
     * Block the calling (server) thread per [cmd]: until a focus event newer than
     * cmd.afterSeq (then quiet_ms without events), or wait_ms. No-op for wait_ms <= 0.
     */
    fun await(cmd: ViewInspection.A11yFocusCommand): Wait {
        val waitMs = cmd.waitMs.coerceIn(0, MAX_WAIT_MS)
        val start = System.nanoTime()
        fun elapsedMs() = ((System.nanoTime() - start) / 1_000_000L).toInt()
        if (waitMs == 0) return Wait(A11yEventTap.focusAfter(cmd.afterSeq), false, 0, null)
        var afterSeq = cmd.afterSeq
        var note: String? = null
        val deadline = start + waitMs * 1_000_000L
        val hardDeadline = deadline + cmd.quietMs.coerceIn(0, MAX_QUIET_MS) * 1_000_000L

        // Tap the current windows and remember where focus is, for the poll below.
        val baseline = MainThread.run { tick() }
        val current = A11yEventTap.seq()
        if (afterSeq > current) {
            // A seq from an earlier agent (the app or the agent restarted): count from now.
            note = "after_seq $afterSeq is ahead of the event tap ($current): waiting for new events"
            afterSeq = current
        }
        var seen = A11yEventTap.focusAfter(afterSeq)
        var nextPoll = System.nanoTime() + POLL_MS * 1_000_000L
        while (!seen && !A11yEventTap.isClosed()) {
            val now = System.nanoTime()
            if (now >= deadline) break
            val untilMs = (minOf(deadline, nextPoll) - now) / 1_000_000L
            seen = A11yEventTap.awaitFocus(afterSeq, maxOf(1L, untilMs))
            if (!seen && System.nanoTime() >= nextPoll && System.nanoTime() < deadline) {
                val id = try {
                    MainThread.run { tick() }
                } catch (t: Throwable) {
                    null
                }
                if (id != null && id != baseline) {
                    seen = true
                    note = (note?.let { "$it; " } ?: "") + "focus moved without a tapped focus event (seen by polling)"
                }
                nextPoll = System.nanoTime() + POLL_MS * 1_000_000L
            }
        }
        if (seen && cmd.quietMs > 0) {
            A11yEventTap.awaitQuiet(cmd.quietMs.coerceIn(0, MAX_QUIET_MS).toLong(), hardDeadline)
        }
        return Wait(seen, !seen, elapsedMs(), note)
    }

    /** Main thread: tap any new window and return the current focus identity. */
    private fun tick(): FocusId? {
        val roots = RootsDetector.rootViews()
        A11yEventTap.ensureInstalled(roots)
        return focusIdentity(roots)
    }

    private fun focusIdentity(roots: List<View>): FocusId? {
        for (root in roots.asReversed()) {
            val (host, vnode) = focusedIn(root) ?: continue
            val vid = vnode?.let { A11yViews.sourceNodeId(it) }?.let { A11yIds.virtualIdOf(it) } ?: HOST_VIEW_ID
            return FocusId(ViewReflect.uniqueDrawingId(root), ViewReflect.uniqueDrawingId(host), vid)
        }
        return null
    }

    // ------------------------------------------------------------ read (main thread)

    /** Read accessibility (and optionally input) focus. Main thread. */
    fun read(strings: StringTable, subtreeDepth: Int, includeInput: Boolean): Read {
        val t0 = System.nanoTime()
        val roots = RootsDetector.rootViews()
        val added = A11yEventTap.ensureInstalled(roots)
        val diag = StringBuilder("roots=${roots.size}")
        if (added > 0) diag.append("; event-tap +$added (${A11yEventTap.installedCount()} tapped)")
        val depth = subtreeDepth.coerceIn(0, MAX_SUBTREE_DEPTH)
        // Lift the taps while mapping, so a focused root reports its own importance.
        val (a11y, input) = A11yEventTap.withoutTap {
            val a = readA11y(roots, strings, depth, diag)
            val i = if (includeInput) readInput(roots, strings, depth, diag) else null
            a to i
        }
        val seq = A11yEventTap.seq()
        val (touch, services) = a11yState(roots)
        val readUs = ((System.nanoTime() - t0) / 1000L).toInt()
        return Read(a11y, input, seq, readUs, touch, services, diag.toString())
    }

    private fun readA11y(
        roots: List<View>,
        strings: StringTable,
        depth: Int,
        diag: StringBuilder,
    ): ViewInspection.A11yFocus? {
        if (!viewRootReachable) {
            diag.append("; ViewRootImpl focus members unreachable: find-focus fallback")
            return findFocusFallback(roots, strings, depth, diag)
        }
        var found: ViewInspection.A11yFocus? = null
        var others = 0
        // Top-most window first: TalkBack's focus lives in one window; a lower window that
        // still records a host is left over from before.
        for ((z, root) in roots.withIndex().reversed()) {
            val (host, vnode) = focusedIn(root) ?: continue
            if (found != null) {
                others++
                continue
            }
            found = focusOf(roots, root, z, host, vnode, strings, depth, "view-root", diag)
        }
        if (others > 0) diag.append("; focus-recorded-in-lower-windows=$others")
        if (found == null) diag.append("; no accessibility focus in the app's windows")
        return found
    }

    /** The focused host View of [root]'s ViewRootImpl and the recorded virtual node, or null. */
    private fun focusedIn(root: View): Pair<View, AccessibilityNodeInfo?>? {
        val vri = root.parent ?: return null
        if (viewRootImplClass?.isInstance(vri) != true) return null
        return try {
            val host = (focusedHostM?.invoke(vri) ?: focusedHostF?.get(vri)) as? View ?: return null
            val vnode = (focusedVirtualM?.invoke(vri) ?: focusedVirtualF?.get(vri)) as? AccessibilityNodeInfo
            host to vnode
        } catch (t: Throwable) {
            Log.w(TAG, "reading ViewRootImpl accessibility focus failed", t)
            null
        }
    }

    /**
     * The A11yFocus for [host] (+ the recorded virtual node [vnode]) in [root], rebuilt the way
     * AccessibilityInteractionController.findFocus does for FOCUS_ACCESSIBILITY.
     */
    private fun focusOf(
        roots: List<View>,
        root: View,
        z: Int,
        host: View,
        vnode: AccessibilityNodeInfo?,
        strings: StringTable,
        depth: Int,
        source: String,
        diag: StringBuilder,
    ): ViewInspection.A11yFocus {
        val provider = try {
            host.accessibilityNodeProvider
        } catch (_: Throwable) {
            null
        }
        var stale = false
        var virtualId = HOST_VIEW_ID
        val node: AccessibilityNodeInfo? = if (vnode != null) {
            virtualId = A11yViews.sourceNodeId(vnode)?.let { A11yIds.virtualIdOf(it) } ?: HOST_VIEW_ID
            val fresh = try {
                provider?.createAccessibilityNodeInfo(virtualId)
            } catch (_: Throwable) {
                null
            }
            if (fresh == null) {
                stale = true
                diag.append("; focused virtual node $virtualId no longer resolves (stale)")
            }
            fresh ?: vnode
        } else {
            if (provider != null) diag.append("; provider host focused as a View (TalkBack's findFocus sees none)")
            try {
                host.createAccessibilityNodeInfo()
            } catch (_: Throwable) {
                null
            }
        }
        val shown = try {
            host.isAttachedToWindow && host.isShown
        } catch (_: Throwable) {
            false
        }
        if (!shown) {
            stale = true
            diag.append("; focused View is detached or not shown (stale)")
        }
        return focusProto(roots, root, z, host, virtualId, node, stale, source, strings, depth)
    }

    private fun focusProto(
        roots: List<View>,
        root: View,
        z: Int,
        view: View?,
        virtualId: Int,
        node: AccessibilityNodeInfo?,
        stale: Boolean,
        source: String,
        strings: StringTable,
        depth: Int,
    ): ViewInspection.A11yFocus {
        val b = ViewInspection.A11yFocus.newBuilder()
            .setRootViewId(ViewReflect.uniqueDrawingId(root))
            .setHostViewId(view?.let { ViewReflect.uniqueDrawingId(it) } ?: 0L)
            .setVirtualId(virtualId)
            .setStale(stale)
            .setSource(source)
            .setWindow(WindowInfos.of(root, z, strings))
        view?.let { b.hostClass = strings.intern(it.javaClass.name) }
        if (node != null) {
            val mapped = AccessibilityInspector.snapshot(
                roots, root, view, virtualId, node, strings, depth, MAX_SUBTREE_NODES,
            )
            b.node = mapped
            b.bounds = mapped.bounds
        }
        return b.build()
    }

    /** API 34+: ask each window's in-process query connection (findFocus) for the focused node. */
    private fun findFocusFallback(
        roots: List<View>,
        strings: StringTable,
        depth: Int,
        diag: StringBuilder,
    ): ViewInspection.A11yFocus? {
        if (Build.VERSION.SDK_INT < 34) {
            diag.append("; find-focus needs API 34")
            return null
        }
        for ((z, root) in roots.withIndex().reversed()) {
            val hostNode = try {
                root.createAccessibilityNodeInfo()
            } catch (_: Throwable) {
                null
            } ?: continue
            try {
                hostNode.setQueryFromAppProcessEnabled(root, true)
                val focused = hostNode.findFocus(AccessibilityNodeInfo.FOCUS_ACCESSIBILITY) ?: continue
                val packed = A11yViews.sourceNodeId(focused) ?: continue
                if (A11yIds.isUndefined(packed)) continue
                val view = A11yViews.viewFor(root, A11yIds.accessibilityViewIdOf(packed))
                val vid = A11yIds.virtualIdOf(packed)
                // Rebuild locally so the bounds are screen px (the connection serves them
                // window-relative for a window away from the screen origin).
                val local = if (view == null) {
                    null
                } else {
                    try {
                        val provider = view.accessibilityNodeProvider
                        if (provider != null) provider.createAccessibilityNodeInfo(vid) else view.createAccessibilityNodeInfo()
                    } catch (_: Throwable) {
                        null
                    }
                }
                return focusProto(roots, root, z, view, vid, local ?: focused, local == null && view != null,
                    "find-focus", strings, depth)
            } catch (t: Throwable) {
                diag.append("; root#${ViewReflect.uniqueDrawingId(root)} find-focus failed (${t.javaClass.simpleName})")
            } finally {
                try {
                    hostNode.setQueryFromAppProcessEnabled(root, false)
                } catch (_: Throwable) {
                }
            }
        }
        diag.append("; no accessibility focus in the app's windows")
        return null
    }

    /**
     * Input focus: the focused View of the window that has window focus (else of any window),
     * as AccessibilityInteractionController.findFocus serves FOCUS_INPUT: the provider's own
     * answer for a provider host, else the View's node.
     */
    private fun readInput(
        roots: List<View>,
        strings: StringTable,
        depth: Int,
        diag: StringBuilder,
    ): ViewInspection.A11yFocus? {
        val ordered = roots.withIndex().reversed().sortedByDescending { (_, r) ->
            try {
                r.hasWindowFocus()
            } catch (_: Throwable) {
                false
            }
        }
        for ((z, root) in ordered) {
            val focused = try {
                root.findFocus()
            } catch (_: Throwable) {
                null
            } ?: continue
            val provider = try {
                focused.accessibilityNodeProvider
            } catch (_: Throwable) {
                null
            }
            var node: AccessibilityNodeInfo? = null
            var vid = HOST_VIEW_ID
            if (provider != null) {
                node = try {
                    provider.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
                } catch (_: Throwable) {
                    null
                }
                node?.let { n ->
                    A11yViews.sourceNodeId(n)?.takeIf { !A11yIds.isUndefined(it) }?.let { vid = A11yIds.virtualIdOf(it) }
                }
            }
            if (node == null) {
                vid = HOST_VIEW_ID
                node = try {
                    focused.createAccessibilityNodeInfo()
                } catch (_: Throwable) {
                    null
                }
            }
            return focusProto(roots, root, z, focused, vid, node, false, "input-focus", strings, depth)
        }
        diag.append("; no input focus")
        return null
    }

    /** (touch exploration on, an accessibility service enabled) as the app sees it. */
    private fun a11yState(roots: List<View>): Pair<Boolean, Boolean> {
        val am = managerOf(roots.firstOrNull()) ?: return false to false
        return try {
            val services = am.isEnabled &&
                am.getEnabledAccessibilityServiceList(AccessibilityServiceInfo.FEEDBACK_ALL_MASK).isNotEmpty()
            am.isTouchExplorationEnabled to services
        } catch (t: Throwable) {
            false to false
        }
    }

    private fun managerOf(view: View?): AccessibilityManager? = try {
        view?.context?.getSystemService(AccessibilityManager::class.java)
    } catch (_: Throwable) {
        null
    }

    // ------------------------------------------------------------ act (main thread)

    /** Perform [cmd] on its node and read accessibility focus afterwards. Main thread. */
    fun act(cmd: ViewInspection.A11yActCommand, strings: StringTable): Act {
        val roots = RootsDetector.rootViews()
        A11yEventTap.ensureInstalled(roots)
        val seqBefore = A11yEventTap.seq()
        val depth = cmd.subtreeDepth.coerceIn(0, MAX_SUBTREE_DEPTH)
        val diag = StringBuilder("roots=${roots.size}")
        fun done(performed: Boolean, error: String?, actionId: Int): Act {
            val after = A11yEventTap.withoutTap { readA11y(roots, strings, depth, diag) }
            return Act(performed, error, actionId, seqBefore, after, A11yEventTap.seq(), diag.toString())
        }

        val actionId = actionIdOf(cmd)
            ?: return done(false, "unknown action ${cmd.action} (raw_action_id ${cmd.rawActionId})", 0)
        val view = findViewById(roots, cmd.hostViewId)
            ?: return done(false, "no View with host_view_id ${cmd.hostViewId} in the app's windows " +
                "(the key is from an older dump, or its window closed)", actionId)
        if (actionId == AccessibilityNodeInfo.ACTION_ACCESSIBILITY_FOCUS ||
            actionId == AccessibilityNodeInfo.ACTION_CLEAR_ACCESSIBILITY_FOCUS
        ) {
            val am = managerOf(view)
            val touch = try {
                am != null && am.isEnabled && am.isTouchExplorationEnabled
            } catch (_: Throwable) {
                false
            }
            if (!touch) {
                return done(false, "touch exploration is off: accessibility focus exists only while a " +
                    "screen reader such as TalkBack runs (View.requestAccessibilityFocus and Compose " +
                    "refuse the action otherwise). Turn TalkBack on first.", actionId)
            }
        }
        val provider = try {
            view.accessibilityNodeProvider
        } catch (_: Throwable) {
            null
        }
        if (provider == null && cmd.virtualId != HOST_VIEW_ID) {
            return done(false, "View ${cmd.hostViewId} serves no virtual nodes, so virtual id " +
                "${cmd.virtualId} does not exist (use -1 for the View itself)", actionId)
        }
        val node = try {
            if (provider != null) provider.createAccessibilityNodeInfo(cmd.virtualId) else view.createAccessibilityNodeInfo()
        } catch (_: Throwable) {
            null
        } ?: return done(false, "virtual id ${cmd.virtualId} no longer resolves under View " +
            "${cmd.hostViewId} (the node is gone; take a fresh dump)", actionId)
        val args = bundleOf(cmd.argsList)

        var performed: Boolean? = null
        if (Build.VERSION.SDK_INT >= 34 && setSealedM != null) {
            // Through the query connection: AccessibilityInteractionController's
            // performAccessibilityAction, as for a service's performAction.
            try {
                node.setQueryFromAppProcessEnabled(view, true)
                setSealedM.invoke(node, true)
                performed = node.performAction(actionId, args)
                diag.append("; via=query-connection")
            } catch (t: Throwable) {
                diag.append("; query-connection failed (${t.javaClass.simpleName}); via=direct")
            } finally {
                try {
                    setSealedM.invoke(node, false)
                    node.setQueryFromAppProcessEnabled(view, false)
                } catch (_: Throwable) {
                }
            }
        } else {
            diag.append("; via=direct")
        }
        if (performed == null) {
            performed = try {
                if (provider != null) provider.performAction(cmd.virtualId, actionId, args)
                else view.performAccessibilityAction(actionId, args)
            } catch (t: Throwable) {
                return done(false, "the action threw ${t.javaClass.simpleName}: ${t.message}", actionId)
            }
        }
        return done(performed, if (performed) null else "the node refused the action (performAction returned false)", actionId)
    }

    private fun actionIdOf(cmd: ViewInspection.A11yActCommand): Int? = when (cmd.action) {
        ViewInspection.NodeAction.NODE_ACTION_ACCESSIBILITY_FOCUS -> AccessibilityNodeInfo.ACTION_ACCESSIBILITY_FOCUS
        ViewInspection.NodeAction.NODE_ACTION_CLEAR_ACCESSIBILITY_FOCUS ->
            AccessibilityNodeInfo.ACTION_CLEAR_ACCESSIBILITY_FOCUS
        ViewInspection.NodeAction.NODE_ACTION_CLICK -> AccessibilityNodeInfo.ACTION_CLICK
        ViewInspection.NodeAction.NODE_ACTION_LONG_CLICK -> AccessibilityNodeInfo.ACTION_LONG_CLICK
        ViewInspection.NodeAction.NODE_ACTION_SCROLL_FORWARD -> AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
        ViewInspection.NodeAction.NODE_ACTION_SCROLL_BACKWARD -> AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
        ViewInspection.NodeAction.NODE_ACTION_SHOW_ON_SCREEN ->
            AccessibilityNodeInfo.AccessibilityAction.ACTION_SHOW_ON_SCREEN.id
        ViewInspection.NodeAction.NODE_ACTION_FOCUS -> AccessibilityNodeInfo.ACTION_FOCUS
        ViewInspection.NodeAction.NODE_ACTION_CLEAR_FOCUS -> AccessibilityNodeInfo.ACTION_CLEAR_FOCUS
        ViewInspection.NodeAction.NODE_ACTION_SET_TEXT -> AccessibilityNodeInfo.ACTION_SET_TEXT
        ViewInspection.NodeAction.NODE_ACTION_EXPAND -> AccessibilityNodeInfo.ACTION_EXPAND
        ViewInspection.NodeAction.NODE_ACTION_COLLAPSE -> AccessibilityNodeInfo.ACTION_COLLAPSE
        ViewInspection.NodeAction.NODE_ACTION_DISMISS -> AccessibilityNodeInfo.ACTION_DISMISS
        ViewInspection.NodeAction.NODE_ACTION_RAW -> cmd.rawActionId.takeIf { it != 0 }
        else -> null
    }

    private fun bundleOf(args: List<ViewInspection.ActionArg>): Bundle? {
        if (args.isEmpty()) return null
        val b = Bundle()
        for (a in args) {
            when (a.valueCase) {
                // CharSequence: ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE is read with getCharSequence.
                ViewInspection.ActionArg.ValueCase.STRING_VALUE -> b.putCharSequence(a.key, a.stringValue)
                ViewInspection.ActionArg.ValueCase.INT_VALUE -> b.putInt(a.key, a.intValue)
                ViewInspection.ActionArg.ValueCase.BOOL_VALUE -> b.putBoolean(a.key, a.boolValue)
                ViewInspection.ActionArg.ValueCase.FLOAT_VALUE -> b.putFloat(a.key, a.floatValue)
                else -> {}
            }
        }
        return b
    }
}
