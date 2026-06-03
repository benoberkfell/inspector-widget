/*
 * ViewSpector — clean-room re-implementation.
 *
 * Loose adoption of android.view.inspector.IntFlagMapping, modeled faithfully on
 * tools-base/dynamic-layout-inspector/agent/appinspection/.../property/IntFlagMapping.java
 * (Apache 2.0, AOSP). Reproduced here so the payload has no dependency on the Google
 * inspector jars (CONTRACT §8: clean-room, framework APIs + our own proto only).
 *
 * Given a property value, apply() returns the set of flag names whose (mask,target)
 * pair matches, with greedy first-wins ordering so composite flags (e.g. "fill")
 * suppress their constituent single-bit flags.
 */
package com.oberkfell.viewspector.agent.payload;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.function.IntFunction;

/** Loose adoption of android.view.inspector.IntFlagMapping. */
public final class IntFlagMapping implements IntFunction<Set<String>> {
    private final List<Flag> mFlags = new ArrayList<>();

    /**
     * Get a set of the names of enabled flags for a given property value.
     *
     * @param value The value of the property
     * @return The names of the enabled flags, empty if no flags enabled
     */
    @Override
    public Set<String> apply(int value) {
        // LinkedHashSet keeps insertion (== registration) order so the joined
        // string is deterministic across runs (e.g. "top|left").
        Set<String> enabledFlagNames = new LinkedHashSet<>();
        int alreadyIncluded = 0;

        for (Flag flag : mFlags) {
            if (flag.isEnabledFor(value) && ((alreadyIncluded & flag.mTarget) != flag.mTarget)) {
                enabledFlagNames.add(flag.mName);
                alreadyIncluded |= flag.mTarget;
            }
        }

        return enabledFlagNames;
    }

    /**
     * Add a flag to the map.
     *
     * @param mask The bit mask to compare to and with a value
     * @param target The target value to compare the masked value with
     * @param name The name of the flag to include if enabled
     */
    public void add(int mask, int target, String name) {
        mFlags.add(new Flag(mask, target, name));
    }

    /** Inner class that holds the name, mask, and target value of a flag. */
    private static final class Flag {
        private final String mName;
        private final int mTarget;
        private final int mMask;

        private Flag(int mask, int target, String name) {
            mTarget = target;
            mMask = mask;
            mName = name;
        }

        /**
         * Compare the supplied property value against the mask and target.
         *
         * @param value The value to check
         * @return True if this flag is enabled
         */
        private boolean isEnabledFor(int value) {
            return (value & mMask) == mTarget;
        }
    }
}
