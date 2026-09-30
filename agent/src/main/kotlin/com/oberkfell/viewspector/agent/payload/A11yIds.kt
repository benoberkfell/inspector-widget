/*
 * ViewSpector — payload :: accessibility node-id arithmetic (pure, no Android deps).
 *
 * Two id spaces meet here and must never be mixed up:
 *
 *  1. AOSP's PACKED accessibility node id, as carried by AccessibilityNodeInfo
 *     (getSourceNodeId(), getChildId(i), and the private mTraversalBefore /
 *     mTraversalAfter / mLabelForId / mLabeledById / mLabeledByIds fields).
 *     AccessibilityNodeInfo.makeNodeId (AOSP core/java/android/view/accessibility/
 *     AccessibilityNodeInfo.java):
 *
 *         packed = ((long) virtualDescendantId << 32) | accessibilityViewId
 *
 *     LOW 32 bits  = View.getAccessibilityViewId() of the backing View,
 *     HIGH 32 bits = the virtual descendant id, AccessibilityNodeProvider.HOST_VIEW_ID
 *                    (-1) when the node is the View itself.
 *     For Compose, the virtual descendant id IS the SemanticsNode id, except the
 *     unmerged root SemanticsNode, which Compose maps to HOST_VIEW_ID (i.e. the
 *     AndroidComposeView's own node) — AndroidComposeViewAccessibilityDelegateCompat
 *     .semanticsNodeIdToAccessibilityVirtualNodeId, verified in ui-android 1.7.0 and 1.12.1.
 *
 *  2. The HOST NODE KEY the Python host uses for every A11yNode (host/inspector_widget/a11y.py):
 *
 *         key = (host_view_id << 32) ^ (virtual_id & 0xFFFFFFFF)
 *
 *     where host_view_id is View.getUniqueDrawingId() of the node's own backing View.
 *     A11yNode.traversal_before / traversal_after / label_for / labeled_by /
 *     labeled_by_list are emitted in THIS key space (0 = none), so the host can join
 *     them to nodes directly. The key fits a signed int64 while host_view_id < 2^31;
 *     uniqueDrawingIds are a small per-process counter, so this holds in practice.
 */
package com.oberkfell.viewspector.agent.payload

object A11yIds {

    /** AccessibilityNodeProvider.HOST_VIEW_ID: the "virtual id" of a real View's own node. */
    const val HOST_VIEW_ID: Int = -1

    /** AccessibilityNodeInfo.UNDEFINED_ITEM_ID. */
    const val UNDEFINED_ITEM_ID: Int = Int.MAX_VALUE

    /** AccessibilityNodeInfo.ROOT_ITEM_ID. */
    const val ROOT_ITEM_ID: Int = Int.MAX_VALUE - 1

    /** AccessibilityNodeInfo.UNDEFINED_NODE_ID == makeNodeId(UNDEFINED_ITEM_ID, UNDEFINED_ITEM_ID). */
    const val UNDEFINED_NODE_ID: Long = 0x7FFFFFFF7FFFFFFFL

    /** Low 32 bits of a packed node id: the backing View's accessibility view id. */
    fun accessibilityViewIdOf(packed: Long): Int = packed.toInt()

    /** High 32 bits of a packed node id: the virtual descendant id (HOST_VIEW_ID for a real View). */
    fun virtualIdOf(packed: Long): Int = (packed ushr 32).toInt()

    /** AOSP AccessibilityNodeInfo.makeNodeId, bit for bit (including the int→long sign extension). */
    fun makeNodeId(accessibilityViewId: Int, virtualDescendantId: Int): Long =
        (virtualDescendantId.toLong() shl 32) or accessibilityViewId.toLong()

    /** True when [packed] names no node (unset linkage field). */
    fun isUndefined(packed: Long): Boolean =
        packed == UNDEFINED_NODE_ID || accessibilityViewIdOf(packed) == UNDEFINED_ITEM_ID

    /** The host node key for (uniqueDrawingId of the backing View, virtual id). Mirrors a11y.py. */
    fun hostKey(hostViewId: Long, virtualId: Int): Long =
        (hostViewId shl 32) xor (virtualId.toLong() and 0xFFFFFFFFL)
}
