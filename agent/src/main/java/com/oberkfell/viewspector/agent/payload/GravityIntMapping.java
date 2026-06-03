/*
 * ViewSpector — clean-room re-implementation.
 *
 * Support for converting android.view.Gravity values into a set of strings.
 * Modeled faithfully on
 * tools-base/dynamic-layout-inspector/agent/appinspection/.../property/GravityIntMapping.java
 * (Apache 2.0, AOSP). Reproduced so the payload depends only on framework APIs + our proto
 * (CONTRACT §8). Seeds an {@link IntFlagMapping} with the Gravity constants and applies an
 * RTL-aware rewrite (left -> start, right -> end) when RELATIVE_LAYOUT_DIRECTION is set.
 */
package com.oberkfell.viewspector.agent.payload;

import android.view.Gravity;
import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.Set;
import java.util.function.IntFunction;

/** Support for converting android.view.Gravity values into a set of strings. */
public final class GravityIntMapping implements IntFunction<Set<String>> {
    private final IntFlagMapping gravityIntFlagMapping = new IntFlagMapping();

    public GravityIntMapping() {
        gravityIntFlagMapping.add(Gravity.FILL, Gravity.FILL, "fill");

        gravityIntFlagMapping.add(Gravity.FILL_VERTICAL, Gravity.FILL_VERTICAL, "fill_vertical");
        gravityIntFlagMapping.add(Gravity.FILL_VERTICAL, Gravity.TOP, "top");
        gravityIntFlagMapping.add(Gravity.FILL_VERTICAL, Gravity.BOTTOM, "bottom");

        gravityIntFlagMapping.add(
                Gravity.FILL_HORIZONTAL, Gravity.FILL_HORIZONTAL, "fill_horizontal");
        gravityIntFlagMapping.add(Gravity.FILL_HORIZONTAL, Gravity.LEFT, "left");
        gravityIntFlagMapping.add(Gravity.FILL_HORIZONTAL, Gravity.RIGHT, "right");

        gravityIntFlagMapping.add(Gravity.FILL, Gravity.CENTER, "center");
        gravityIntFlagMapping.add(
                Gravity.FILL_VERTICAL, Gravity.CENTER_VERTICAL, "center_vertical");
        gravityIntFlagMapping.add(
                Gravity.FILL_HORIZONTAL, Gravity.CENTER_HORIZONTAL, "center_horizontal");

        gravityIntFlagMapping.add(Gravity.CLIP_VERTICAL, Gravity.CLIP_VERTICAL, "clip_vertical");
        gravityIntFlagMapping.add(
                Gravity.CLIP_HORIZONTAL, Gravity.CLIP_HORIZONTAL, "clip_horizontal");
    }

    @Override
    public Set<String> apply(int value) {
        Set<String> values = gravityIntFlagMapping.apply(value);
        if ((value & Gravity.RELATIVE_LAYOUT_DIRECTION) != 0) {
            // Preserve insertion order while rewriting left/right to start/end.
            Set<String> rewritten = new LinkedHashSet<>();
            for (String v : values) {
                if (v.equals("left")) {
                    rewritten.add("start");
                } else if (v.equals("right")) {
                    rewritten.add("end");
                } else {
                    rewritten.add(v);
                }
            }
            values = Collections.unmodifiableSet(rewritten);
        }
        return values;
    }
}
