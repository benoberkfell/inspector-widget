/*
 * ViewSpector — payload :: a cheap fingerprint of what a View shows.
 *
 * DUMP_TREE reads the tree in one main-thread hop and the properties in batches of later
 * hops (Dispatcher.PROPERTY_BATCH), so frames, input and adapter rebinds run in between. A
 * View reused in place (a RecyclerView ViewHolder rebound during a fling, a live feed
 * update, an animated label) can then show another item by the time its properties are
 * read. The Dispatcher stamps every View in the tree hop and again just before reading its
 * properties; a View whose stamp changed is counted (diagnostics "properties-changed=N"), so
 * the host knows those property groups describe a later UI state than their nodes.
 *
 * The stamp covers what a node sends and a rebind changes: the on-screen position and size,
 * visibility, attachment, and a TextView's text (masked text is hashed like any other; only
 * the hash is kept). Main thread only; never throws.
 */
package com.oberkfell.viewspector.agent.payload

import android.view.View
import android.widget.TextView

object ViewStamp {

    /** A hash of [view]'s position, size, visibility, attachment and text. */
    fun of(view: View): Int {
        return try {
            val loc = IntArray(2)
            view.getLocationOnScreen(loc)
            var h = loc[0]
            h = h * 31 + loc[1]
            h = h * 31 + view.width
            h = h * 31 + view.height
            h = h * 31 + view.visibility
            h = h * 31 + if (view.isAttachedToWindow) 1 else 0
            if (view is TextView) {
                val text = view.text
                h = h * 31 + (text?.toString()?.hashCode() ?: 0)
            }
            h
        } catch (_: Throwable) {
            // Unreadable now: a changed View as far as the caller can tell.
            System.identityHashCode(view) xor 0x5bd1e995
        }
    }
}
