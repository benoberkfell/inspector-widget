/*
 * ViewSpector — payload core module.
 *
 * String interning for compact wire payloads. Models on the real UI-Inspector
 * StringTable:
 *   android-sources/tools-base/ui-inspector/agent/inspectors/view/src/main/java/
 *     com/android/tools/ui/inspector/inspectors/view/StringTable.kt:27-49
 * adapted to the ViewSpector proto (ViewInspection.Strings / StringEntry) and
 * the SHARED PAYLOAD API: `intern(String?) -> Int`, `build() -> Strings`.
 *
 * Semantics (CONTRACT.md §5, proto comments): every "string-table id" field in
 * the protocol is an int32 index into Strings.entries, and id 0 means
 * absent/empty. So interning null or "" yields 0 and is NOT emitted in build().
 * Real assigned ids start at 1 and are stable for the lifetime of the table.
 */
package com.oberkfell.viewspector.agent.payload

import com.oberkfell.viewspector.proto.ViewInspection

/**
 * Associates each distinct non-empty string with a small positive int id,
 * deduplicating so equal strings share one id. Not thread-safe by itself; a
 * fresh table is built per response, used only on the thread assembling that
 * response (see [Dispatcher]).
 */
class StringTable {

    // Insertion-ordered so build() emits entries in ascending id order, which
    // keeps the wire output deterministic and easy to diff in tests.
    private val map = LinkedHashMap<String, Int>()

    /**
     * Returns the id for [s], assigning a new one (starting at 1) on first
     * sight. null or empty -> 0 (the "absent" sentinel), never stored.
     */
    fun intern(s: String?): Int {
        if (s.isNullOrEmpty()) return 0
        // Ids start at 1: size before insert is the count of already-assigned
        // ids, so the next id is size + 1.
        return map.getOrPut(s) { map.size + 1 }
    }

    /**
     * The string previously interned under [id], or "" for id 0, or null if no
     * such id was assigned. Mainly a convenience for tests / debugging; the wire
     * protocol resolves ids host-side from [build].
     */
    fun lookup(id: Int): String? {
        if (id == 0) return ""
        // Reverse scan; tables are small enough (per-response) that this is fine.
        return map.entries.firstOrNull { it.value == id }?.key
    }

    /** Number of distinct non-empty strings interned so far. */
    fun size(): Int = map.size

    /**
     * Builds the [ViewInspection.Strings] message carrying every interned
     * (id, str) pair. Excludes id 0 (it is the implicit empty/absent value and
     * must never appear in the table). Mirrors StringTable.toStringEntries
     * (StringTable.kt:40-48).
     */
    fun build(): ViewInspection.Strings {
        val builder = ViewInspection.Strings.newBuilder()
        for ((str, id) in map) {
            builder.addEntries(
                ViewInspection.StringEntry.newBuilder()
                    .setId(id)
                    .setStr(str)
                    .build()
            )
        }
        return builder.build()
    }
}
