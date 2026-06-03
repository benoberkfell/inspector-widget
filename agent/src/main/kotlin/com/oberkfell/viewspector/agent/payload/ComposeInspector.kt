/*
 * ViewSpector — clean-room re-implementation of Android Studio's View Layout Inspector.
 *
 * PAYLOAD :: Compose extraction by pure reflection (no compile-time Compose dependency).
 *
 * The payload runs inside the app classloader of a debuggable app that bundles
 * androidx.compose.ui (+ tooling-data). We reach Compose data two ways, both reflectively
 * against classes resolved from the live AndroidComposeView's own classloader:
 *
 *   A. SEMANTICS TREE (reliable; always present, backs accessibility — no setup needed):
 *      AndroidComposeView.getSemanticsOwner() -> SemanticsOwner.getRootSemanticsNode()
 *      -> recurse getChildren(); per node getBoundsInWindow():Rect and iterate
 *      getConfig():SemanticsConfiguration (Iterable<Map.Entry<SemanticsPropertyKey,Any?>>).
 *      Symbols verified in androidx source: SemanticsOwner.kt:47, SemanticsNode.kt:174/242/321,
 *      SemanticsConfiguration.kt:31-65, SemanticsProperties.kt:420; same entry the shipped
 *      inspector uses (ui-inspection LayoutInspectorTree.kt:108-109).
 *
 *   B. SLOT TABLE (conditional; composable names + file:line via the app's bundled
 *      androidx.compose.ui.tooling.data): read the View tag inspection_slot_table_set ->
 *      Set<CompositionData>, then SlotTreeKt.asTree(CompositionData):Group and walk Group
 *      (getName/getBox/getLocation/getChildren). Only populated when isDebugInspectorInfoEnabled
 *      was true at composition time (Wrapper.android.kt:78). Best-effort; reported in diagnostics.
 *
 * Bounds are in window px, which equals screenshot px (we capture the window), so they overlay
 * the BITMAP screenshot directly.
 */
package com.oberkfell.viewspector.agent.payload

import android.util.Log
import android.view.View
import android.view.ViewGroup
import com.oberkfell.viewspector.proto.ViewInspection
import java.lang.reflect.Method

object ComposeInspector {

    private const val TAG = "ViewSpector"
    private const val ANDROID_COMPOSE_VIEW = "androidx.compose.ui.platform.AndroidComposeView"
    private const val MAX_DEPTH = 400

    /**
     * Build a Compose window per AndroidComposeView found under [rootViews]. Call on the main thread.
     * Returns the windows plus a human diagnostics string describing what was reachable.
     */
    fun dump(
        rootViews: List<View>,
        strings: StringTable,
        includeSemantics: Boolean,
        includeSlotTable: Boolean,
    ): Pair<List<ViewInspection.DumpComposeResponse.Window>, String> {
        val composeViews = ArrayList<View>()
        for (root in rootViews) collectComposeViews(root, composeViews)
        val diag = StringBuilder()
        diag.append("found ${composeViews.size} AndroidComposeView(s)")

        val windows = ArrayList<ViewInspection.DumpComposeResponse.Window>()
        for (cv in composeViews) {
            val rootNode = ViewInspection.ComposeNode.newBuilder()
            rootNode.id = cv.uniqueDrawingId
            rootNode.name = strings.intern("AndroidComposeView")
            rootNode.kind = ViewInspection.ComposeNode.Kind.COMPOSABLE
            boundsOf(cv)?.let { rootNode.bounds = it }

            var produced = false
            if (includeSemantics) {
                try {
                    val semRoot = semanticsRootNode(cv)
                    if (semRoot != null) {
                        val n = buildSemanticsNode(semRoot, strings, 0)
                        if (n != null) { rootNode.addChildren(n); produced = true }
                    } else {
                        diag.append("; semantics owner/root unreachable")
                    }
                } catch (t: Throwable) {
                    Log.w(TAG, "semantics walk failed", t)
                    diag.append("; semantics error: ${t.javaClass.simpleName}")
                }
            }
            if (includeSlotTable) {
                try {
                    val groups = slotTableGroups(cv)
                    if (groups.isEmpty()) {
                        diag.append("; slot table empty (inspection_slot_table_set not populated)")
                    } else {
                        for (g in groups) {
                            for (n in buildSlotChildren(g, strings, 0)) {
                                rootNode.addChildren(n); produced = true
                            }
                        }
                    }
                } catch (t: Throwable) {
                    Log.w(TAG, "slot-table walk failed", t)
                    diag.append("; slot-table error: ${t.javaClass.simpleName}")
                }
            }

            windows.add(
                ViewInspection.DumpComposeResponse.Window.newBuilder()
                    .setViewId(cv.uniqueDrawingId)
                    .setRoot(rootNode.build())
                    .build()
            )
            if (!produced) diag.append("; view#${cv.uniqueDrawingId} produced no compose nodes")
        }
        return windows to diag.toString()
    }

    /**
     * Enable Compose inspection so the slot table populates: set isDebugInspectorInfoEnabled,
     * add slot-table storage to each AndroidComposeView, then hot-reload to force a fresh
     * composition (which fills the tables). Replicates ComposeLayoutInspector.enableInspection +
     * addSlotTable (framework/ViewExtensions.kt) + hotReload. MUST run on the main thread.
     * Returns the number of slot tables newly added (0 => nothing to do / already enabled).
     */
    fun enableInspection(rootViews: List<View>): Int {
        val composeViews = ArrayList<View>()
        for (root in rootViews) collectComposeViews(root, composeViews)
        if (composeViews.isEmpty()) return 0
        val cl = composeViews.first().javaClass.classLoader ?: return 0

        setDebugInspectorInfoEnabled(cl)

        var added = 0
        for (cv in composeViews) {
            val tagId = slotTableTagId(cv, cl)
            if (tagId == 0) continue
            if (cv.getTag(tagId) is Set<*>) continue
            try {
                val set = java.util.Collections.newSetFromMap(java.util.WeakHashMap<Any, Boolean>())
                cv.setTag(tagId, set)
                added++
            } catch (t: Throwable) {
                Log.w(TAG, "addSlotTable failed", t)
            }
        }
        if (added > 0) {
            try {
                hotReload(cl)
            } catch (t: Throwable) {
                Log.w(TAG, "hotReload failed", t)
            }
        }
        return added
    }

    private fun setDebugInspectorInfoEnabled(cl: ClassLoader) {
        try {
            val k = Class.forName("androidx.compose.ui.platform.InspectableValueKt", false, cl)
            val f = k.getDeclaredField("isDebugInspectorInfoEnabled")
            f.isAccessible = true
            f.setBoolean(null, true)
        } catch (t: Throwable) {
            Log.w(TAG, "could not set isDebugInspectorInfoEnabled", t)
        }
    }

    private fun hotReload(cl: ClassLoader) {
        val hotReload = Class.forName("androidx.compose.runtime.HotReloader", false, cl)
        val companion = hotReload.getField("Companion").get(null)
        val save = companion.javaClass.getDeclaredMethod("saveStateAndDispose", Any::class.java)
        val load = companion.javaClass.getDeclaredMethod("loadStateAndCompose", Any::class.java)
        save.isAccessible = true
        load.isAccessible = true
        val context = Class.forName("android.app.ActivityThread")
            .getDeclaredMethod("currentApplication")
            .apply { isAccessible = true }
            .invoke(null)
        val state = save.invoke(companion, context)
        load.invoke(companion, state)
    }

    // ---------------------------------------------------------------- view discovery
    private fun collectComposeViews(view: View, out: MutableList<View>) {
        if (view.javaClass.canonicalName == ANDROID_COMPOSE_VIEW ||
            isAssignableToName(view, ANDROID_COMPOSE_VIEW)
        ) {
            out.add(view)
            // An AndroidComposeView has no child Views of interest for us; stop here.
            return
        }
        if (view is ViewGroup) {
            for (i in 0 until view.childCount) collectComposeViews(view.getChildAt(i), out)
        }
    }

    // ---------------------------------------------------------------- semantics (A)
    private fun semanticsRootNode(composeView: View): Any? {
        val owner = invoke(composeView, "getSemanticsOwner") ?: return null
        // Merged root == what TalkBack sees == best human labels.
        return invoke(owner, "getRootSemanticsNode")
    }

    private fun buildSemanticsNode(node: Any, strings: StringTable, depth: Int): ViewInspection.ComposeNode? {
        if (depth > MAX_DEPTH) return null
        val b = ViewInspection.ComposeNode.newBuilder()
        b.kind = ViewInspection.ComposeNode.Kind.SEMANTICS
        (invoke(node, "getId") as? Int)?.let { b.id = it.toLong() }

        // bounds: getBoundsInWindow() -> Compose Rect (window px == screenshot px)
        semanticsBounds(node)?.let { b.bounds = it }

        // attrs: iterate the SemanticsConfiguration
        val attrs = readSemanticsConfig(node)
        for ((k, v) in attrs) {
            b.addAttrs(
                ViewInspection.ComposeNode.Attr.newBuilder()
                    .setKey(strings.intern(k)).setValue(strings.intern(v)).build()
            )
        }
        b.name = strings.intern(bestLabel(attrs))

        // children
        (invoke(node, "getChildren") as? List<*>)?.forEach { child ->
            if (child != null) buildSemanticsNode(child, strings, depth + 1)?.let { b.addChildren(it) }
        }
        return b.build()
    }

    private fun semanticsBounds(node: Any): ViewInspection.Bounds? {
        val rect = invoke(node, "getBoundsInWindow") ?: return null
        val l = (invoke(rect, "getLeft") as? Float) ?: return null
        val t = (invoke(rect, "getTop") as? Float) ?: return null
        val r = (invoke(rect, "getRight") as? Float) ?: return null
        val btm = (invoke(rect, "getBottom") as? Float) ?: return null
        val w = (r - l).toInt(); val h = (btm - t).toInt()
        if (w <= 0 || h <= 0) return null
        return ViewInspection.Bounds.newBuilder()
            .setLayout(
                ViewInspection.Rect.newBuilder()
                    .setX(l.toInt()).setY(t.toInt()).setW(w).setH(h).build()
            ).build()
    }

    private fun readSemanticsConfig(node: Any): LinkedHashMap<String, String> {
        val out = LinkedHashMap<String, String>()
        val config = invoke(node, "getConfig") ?: return out
        val iter = invoke(config, "iterator") as? Iterator<*> ?: return out
        while (iter.hasNext()) {
            val entry = iter.next() as? Map.Entry<*, *> ?: continue
            val key = entry.key ?: continue
            val name = invoke(key, "getName") as? String ?: continue
            val value = entry.value
            out[name] = stringifySemanticsValue(value)
        }
        return out
    }

    private fun stringifySemanticsValue(value: Any?): String {
        if (value == null) return ""
        return try {
            when (value) {
                is CharSequence -> value.toString()
                is List<*> -> value.joinToString(", ") { annotatedOrString(it) }
                else -> annotatedOrString(value)
            }
        } catch (t: Throwable) { value.toString() }
    }

    private fun annotatedOrString(v: Any?): String {
        if (v == null) return ""
        // AnnotatedString.getText() ; Role/ToggleableState/etc. -> toString() ; AccessibilityAction.getLabel()
        invoke(v, "getText")?.let { if (it is CharSequence) return it.toString() }
        invoke(v, "getLabel")?.let { if (it is CharSequence) return it.toString() }
        invoke(v, "getCurrent")?.let { return it.toString() } // ProgressBarRangeInfo
        return v.toString()
    }

    private fun bestLabel(attrs: Map<String, String>): String {
        for (k in listOf("Text", "ContentDescription", "EditableText", "InputText", "Role", "TestTag")) {
            val v = attrs[k]
            if (!v.isNullOrBlank()) return v
        }
        return "Node"
    }

    // ---------------------------------------------------------------- slot table (B)
    @Suppress("UNCHECKED_CAST")
    private fun slotTableGroups(composeView: View): List<Any> {
        val cl = composeView.javaClass.classLoader ?: return emptyList()
        val tagId = slotTableTagId(composeView, cl)
        if (tagId == 0) return emptyList()
        val set = composeView.getTag(tagId) as? Set<Any> ?: return emptyList()
        if (set.isEmpty()) return emptyList()
        val slotTreeKt = try { Class.forName("androidx.compose.ui.tooling.data.SlotTreeKt", false, cl) } catch (t: Throwable) { return emptyList() }
        val compositionDataCls = try { Class.forName("androidx.compose.runtime.tooling.CompositionData", false, cl) } catch (t: Throwable) { return emptyList() }
        val asTree: Method = try { slotTreeKt.getMethod("asTree", compositionDataCls) } catch (t: Throwable) { return emptyList() }
        val groups = ArrayList<Any>()
        for (cd in set) {
            try {
                val g = asTree.invoke(null, cd)
                if (g != null) groups.add(g)
            } catch (t: Throwable) { /* skip */ }
        }
        return groups
    }

    private fun slotTableTagId(view: View, cl: ClassLoader): Int {
        // Primary: the merged app resource id, by name.
        try {
            val id = view.resources.getIdentifier(
                "inspection_slot_table_set", "id", view.context.packageName
            )
            if (id != 0) return id
        } catch (_: Throwable) {}
        // Fallback: androidx.compose.ui.R$id.inspection_slot_table_set
        return try {
            val rid = Class.forName("androidx.compose.ui.R\$id", false, cl)
            rid.getField("inspection_slot_table_set").getInt(null)
        } catch (_: Throwable) { 0 }
    }

    // Slot-table depth cap: keep nesting well under protobuf's 100-level parse limit.
    private const val SLOT_MAX_DEPTH = 60

    /**
     * Returns the list of NAMED-composable nodes contributed by [group] and its descendants.
     * Anonymous/structural groups (no name) are collapsed: their named children are hoisted to
     * the caller, which both flattens the very deep slot tree (avoiding the proto recursion limit)
     * and yields a readable composable hierarchy. Each named node carries bounds, file:line, and
     * its call parameters.
     */
    // Infrastructural composable names that carry no UI meaning: collapse them like unnamed groups
    // so their modifiers/children attach to the nearest MEANINGFUL composable (Tape, Polaroid, Text…).
    private val STRUCTURAL = setOf(
        "Layout", "ReusableComposeNode", "ReusableContent", "ReusableContentHost",
        "SubcomposeLayout", "CompositionLocalProvider",
    )

    private fun isStructuralName(name: String): Boolean =
        name in STRUCTURAL || name.startsWith("<") || name.startsWith("remember")

    private fun meaningfulName(group: Any): String? =
        (invoke(group, "getName") as? String)?.takeIf { it.isNotBlank() && !isStructuralName(it) }

    private fun buildSlotChildren(group: Any, strings: StringTable, depth: Int): List<ViewInspection.ComposeNode> {
        if (depth > SLOT_MAX_DEPTH) return emptyList()
        val name = meaningfulName(group)
        val childGroups = (invoke(group, "getChildren") as? Collection<*>) ?: emptyList<Any?>()
        val childNodes = ArrayList<ViewInspection.ComposeNode>()
        for (c in childGroups) if (c != null) childNodes.addAll(buildSlotChildren(c, strings, depth + 1))

        if (name == null) return childNodes // structural group: hoist children up

        val b = ViewInspection.ComposeNode.newBuilder()
        b.kind = ViewInspection.ComposeNode.Kind.COMPOSABLE
        b.name = strings.intern(name)
        slotBox(group)?.let { b.bounds = it }
        slotLocation(group)?.let { b.source = strings.intern(it) }
        // Modifiers live on the LayoutNode (NodeGroup), which is an UNNAMED descendant of this
        // named composable. Gather modifiers from this group's owned unnamed-descendant chain,
        // stopping at the next named composable (whose modifiers belong to it).
        val mods = LinkedHashSet<String>()
        collectOwnedModifiers(group, mods, 0)
        if (mods.isNotEmpty()) {
            b.addAttrs(
                ViewInspection.ComposeNode.Attr.newBuilder()
                    .setKey(strings.intern("modifiers"))
                    .setValue(strings.intern(mods.joinToString(" → "))).build()
            )
        }
        // Render-node (graphicsLayer) id, so the host can cut a per-component SKP image.
        val rnid = collectRenderNodeId(group, 0)
        if (rnid != 0L) b.renderNodeId = rnid
        for (p in slotParameters(group)) {
            b.addAttrs(
                ViewInspection.ComposeNode.Attr.newBuilder()
                    .setKey(strings.intern(p.first)).setValue(strings.intern(p.second)).build()
            )
        }
        childNodes.forEach { b.addChildren(it) }
        return listOf(b.build())
    }

    /**
     * Read a Group's realized MODIFIER chain via getModifierInfo() -> List<ModifierInfo>, decoding
     * each Modifier.Element through the InspectableValue SPI (nameFallback + inspectableElements ->
     * name + ValueElement args), e.g. "padding(all=16.dp) → background(color=…) → clickable".
     * Requires isDebugInspectorInfoEnabled (we set it in enableInspection). Returns null if none.
     */
    /** Decoded modifier strings from a single group's getModifierInfo() (empty unless a NodeGroup). */
    private fun slotModifiersOf(group: Any): List<String> {
        val infos = invoke(group, "getModifierInfo") as? List<*> ?: return emptyList()
        if (infos.isEmpty()) return emptyList()
        val parts = ArrayList<String>()
        for (mi in infos) {
            if (mi == null) continue
            val mod = invoke(mi, "getModifier") ?: continue
            describeModifierElement(mod)?.let { parts.add(it) }
        }
        return parts
    }

    /** The graphicsLayer RenderNode/layer id on [group]'s LayoutNode, if any (via GraphicLayerInfo). */
    private fun renderNodeIdOf(group: Any): Long {
        val infos = invoke(group, "getModifierInfo") as? List<*> ?: return 0L
        for (mi in infos) {
            if (mi == null) continue
            // ModifierInfo.extra is a androidx.compose.ui.layout.GraphicLayerInfo (an INTERFACE) for
            // the graphicsLayer element; GraphicLayerInfo.layerId is the SKP draw-layer id.
            val extra = invoke(mi, "getExtra") ?: continue
            val cl = extra.javaClass.classLoader ?: continue
            val gli = try {
                Class.forName("androidx.compose.ui.layout.GraphicLayerInfo", false, cl)
            } catch (t: Throwable) { continue }
            if (gli.isInstance(extra)) {
                val id = invoke(extra, "getLayerId") as? Long
                if (id != null && id != 0L) return id
            }
        }
        return 0L
    }

    /** First graphicsLayer id from [group] and its UNNAMED descendant chain (stopping at named ones). */
    private fun collectRenderNodeId(group: Any, depth: Int): Long {
        if (depth > 20) return 0L
        val own = renderNodeIdOf(group)
        if (own != 0L) return own
        val children = invoke(group, "getChildren") as? Collection<*> ?: return 0L
        for (c in children) {
            if (c == null || meaningfulName(c) != null) continue
            val id = collectRenderNodeId(c, depth + 1)
            if (id != 0L) return id
        }
        return 0L
    }

    /** Collect modifiers from [group] and its UNNAMED descendant chain, stopping at named composables. */
    private fun collectOwnedModifiers(group: Any, out: MutableSet<String>, depth: Int) {
        if (depth > 20) return
        out.addAll(slotModifiersOf(group))
        val children = invoke(group, "getChildren") as? Collection<*> ?: return
        for (c in children) {
            if (c == null) continue
            if (meaningfulName(c) == null) collectOwnedModifiers(c, out, depth + 1)
        }
    }

    private fun describeModifierElement(el: Any): String? {
        // InspectableValue: nameFallback + inspectableElements (Sequence<ValueElement>).
        val name = invoke(el, "getNameFallback") as? String
        val args = ArrayList<String>()
        val seq = invoke(el, "getInspectableElements")
        val iter = invoke(seq, "iterator") as? Iterator<*>
        var guard = 0
        while (iter != null && iter.hasNext() && guard < 24) {
            guard++
            val ve = iter.next() ?: continue
            val vn = invoke(ve, "getName") as? String ?: continue
            val vv = invoke(ve, "getValue")
            args.add("$vn=${annotatedOrString(vv)}")
        }
        val base = name ?: el.javaClass.simpleName
            .removeSuffix("Element").removeSuffix("Modifier").ifBlank { return null }
        return if (args.isEmpty()) base else "$base(${args.joinToString(", ")})"
    }

    /** Read a Group's call parameters (ParameterInformation: name, value) as key/value strings. */
    private fun slotParameters(group: Any): List<Pair<String, String>> {
        val params = invoke(group, "getParameters") as? List<*> ?: return emptyList()
        val out = ArrayList<Pair<String, String>>()
        for (p in params) {
            if (p == null) continue
            val pn = invoke(p, "getName") as? String ?: continue
            val pv = invoke(p, "getValue")
            out.add(pn to (pv?.let { annotatedOrString(it) } ?: "null"))
        }
        return out
    }

    private fun slotBox(group: Any): ViewInspection.Bounds? {
        val box = invoke(group, "getBox") ?: return null
        val l = intOf(box, "getLeft") ?: return null
        val t = intOf(box, "getTop") ?: return null
        val r = intOf(box, "getRight") ?: return null
        val btm = intOf(box, "getBottom") ?: return null
        val w = r - l; val h = btm - t
        if (w <= 0 || h <= 0) return null
        return ViewInspection.Bounds.newBuilder()
            .setLayout(ViewInspection.Rect.newBuilder().setX(l).setY(t).setW(w).setH(h).build())
            .build()
    }

    private fun slotLocation(group: Any): String? {
        val loc = invoke(group, "getLocation") ?: return null
        val file = invoke(loc, "getSourceFile") as? String
        val line = invoke(loc, "getLineNumber") as? Int
        return when {
            file != null && line != null && line >= 0 -> "$file:$line"
            file != null -> file
            else -> null
        }
    }

    // ---------------------------------------------------------------- bounds of a View
    private fun boundsOf(view: View): ViewInspection.Bounds? {
        return try {
            val loc = IntArray(2); view.getLocationOnScreen(loc)
            val w = view.width; val h = view.height
            if (w <= 0 || h <= 0) return null
            ViewInspection.Bounds.newBuilder()
                .setLayout(ViewInspection.Rect.newBuilder().setX(loc[0]).setY(loc[1]).setW(w).setH(h).build())
                .build()
        } catch (t: Throwable) { null }
    }

    // ---------------------------------------------------------------- reflection helpers
    private fun invoke(obj: Any?, method: String): Any? {
        if (obj == null) return null
        val m = findMethod(obj.javaClass, method) ?: return null
        return try { m.isAccessible = true; m.invoke(obj) } catch (t: Throwable) { null }
    }

    private fun intOf(obj: Any?, method: String): Int? = (invoke(obj, method) as? Number)?.toInt()

    private val methodCache = HashMap<String, Method?>()

    private fun findMethod(cls: Class<*>, name: String): Method? {
        val key = cls.name + "#" + name
        methodCache[key]?.let { return it }
        if (methodCache.containsKey(key)) return null
        var found: Method? = null
        // public (incl. inherited) first
        try { found = cls.getMethod(name) } catch (_: Throwable) {}
        if (found == null) {
            var k: Class<*>? = cls
            loop@ while (k != null) {
                try { found = k.getDeclaredMethod(name); break@loop } catch (_: Throwable) {}
                for (i in k.interfaces) {
                    try { found = i.getDeclaredMethod(name); break@loop } catch (_: Throwable) {}
                }
                k = k.superclass
            }
        }
        methodCache[key] = found
        return found
    }

    private fun isAssignableToName(obj: Any, fqName: String): Boolean {
        var c: Class<*>? = obj.javaClass
        while (c != null) {
            if (c.canonicalName == fqName || c.name == fqName) return true
            c = c.superclass
        }
        return false
    }
}
