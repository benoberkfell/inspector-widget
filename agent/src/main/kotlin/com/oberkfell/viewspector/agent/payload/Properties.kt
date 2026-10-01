/*
 * ViewSpector — clean-room re-implementation of Android Studio's View Layout Inspector.
 *
 * PAYLOAD :: comprehensive attribute extraction + resolution stack.
 *
 * This module turns a live android.view.View into a ViewInspection.PropertyGroup using the
 * Android framework's inspection SPI (android.view.inspector.{StaticInspectionCompanionProvider,
 * InspectionCompanion, PropertyMapper, PropertyReader}). It is modeled faithfully on the AOSP
 * dynamic-layout-inspector agent:
 *   tools-base/.../agent/appinspection/proto/property/{PropertyCache.kt, SimplePropertyReader.kt,
 *                                                       PropertyTypeMapper.java, PropertyBuilder.kt,
 *                                                       GravityIntMapping.java, IntFlagMapping.java}
 *   tools-base/.../agent/appinspection/proto/ViewExtensions.kt  (createResource / getNamespace)
 * and on the newer ui-inspector resource string formatting
 *   tools-base/ui-inspector/.../view/ViewExtensions.kt:145-164  (resolveResourceToString)
 *
 * Differences forced by OUR proto (proto/view_inspection.proto):
 *   - Property has NO FlagValue message and NO namespace field. GRAVITY / INT_FLAG sets are
 *     joined with '|' into a single string and stored in `str_value` (string-table id). This
 *     matches the newer ui-inspector pipeline where flags are a "|"-joined string.
 *   - Property.source / Property.resolution_stack are string-table ids of *formatted resource
 *     names* (e.g. "@android:style/Widget.Material.Button"), not Resource messages. We format
 *     resource ids via resolveResourceToString() and intern them.
 *   - Property.resource_value (RESOURCE type, from readResourceId) IS a ViewInspection.Resource
 *     message (type/namespace/name interned), matching createResource() in ViewExtensions.kt.
 *
 * Honest fallback (CONTRACT §6): a view whose class — and none of its superclasses — has a
 * generated `<Class>$InspectionCompanion` yields only the attributes contributed by the base
 * android.view.View companion (always present on a real device). Custom views that never ran the
 * AndroidX resource-inspection annotation processor therefore surface as "just a View" for
 * property purposes. ViewGroup.LayoutParams properties (is_layout=true) are read the same way and
 * are likewise limited to whatever LayoutParams subclass actually ships a companion.
 *
 * Threading note: getAttributeResolutionStack / getAttributeSourceResourceMap and most
 * companions are safe to read off the main thread, but some properties (e.g. those that trigger
 * View.resolvePadding()) throw android.util.AndroidRuntimeException unless read on the UI thread,
 * and WebView throws if read off the UI thread. The Dispatcher owns thread placement; forView()
 * itself is thread-agnostic and simply propagates such exceptions to the caller so it can retry on
 * the main thread (mirrors ViewExtensions.kt:199-215).
 */
package com.oberkfell.viewspector.agent.payload

import android.content.res.ColorStateList
import android.content.res.Resources
import android.graphics.Color
import android.graphics.drawable.ColorDrawable
import android.graphics.drawable.Drawable
import android.os.Build
import android.util.Log
import android.view.View
import android.view.ViewGroup
import android.view.animation.Animation
import android.view.inspector.InspectionCompanion
import android.view.inspector.PropertyMapper
import android.view.inspector.PropertyReader
import android.view.inspector.StaticInspectionCompanionProvider
import com.oberkfell.viewspector.proto.ViewInspection
import java.util.function.IntFunction

/**
 * Extracts the full attribute set (view + layout-params), with optional resolution stack, for a
 * single [View], producing a [ViewInspection.PropertyGroup].
 *
 * @param strings the shared per-response string table; all attribute names, string values, enum
 *   labels, joined flag strings, class names, and formatted resource names are interned into it.
 */
class Properties(val strings: StringTable) {

    private companion object {
        const val TAG = "ViewSpector"

        // android.view.View / android.view.ViewGroup.LayoutParams canonical names — the roots at
        // which the superclass walk stops (PropertyCache.kt:45-47, :69).
        const val VIEW_FQCN = "android.view.View"
        const val LAYOUT_PARAMS_FQCN = "android.view.ViewGroup.LayoutParams"
    }

    // One shared provider/caches per Properties instance. Caches are keyed by Class so repeated
    // views of the same type reuse the (companion list, property-metadata list) — PropertyCache.kt.
    private val provider = StaticInspectionCompanionProvider()
    private val gravityMapping: IntFunction<Set<String>> = GravityIntMapping()
    private val viewCache = TypeCache(provider, VIEW_FQCN, gravityMapping)
    private val layoutCache = TypeCache(provider, LAYOUT_PARAMS_FQCN, gravityMapping)

    /** Views whose whole PropertyGroup failed ([forViewOrNull]); reported in diagnostics. */
    var failedViews: Int = 0
        private set

    /** Single properties that failed to read or encode (the rest of the view still reads). */
    var failedProperties: Int = 0
        private set

    // "<kind>:<class or property>" keys already logged, so a bad property on every row of a
    // list logs once per dump, not once per row.
    private val logged = HashSet<String>()

    private fun logOnce(key: String, message: String, t: Throwable) {
        if (logged.add(key)) Log.w(TAG, "$message (further failures of this kind counted only)", t)
    }

    /**
     * [forView] that never throws: a view whose properties can't be read at all yields null
     * (counted in [failedViews]) so one bad view never aborts a DUMP_TREE. Main thread.
     */
    fun forViewOrNull(view: View, includeResolutionStack: Boolean): ViewInspection.PropertyGroup? =
        try {
            forView(view, includeResolutionStack)
        } catch (t: Throwable) {
            failedViews++
            logOnce("view:${view.javaClass.name}", "properties failed for ${view.javaClass.name}", t)
            null
        }

    /**
     * Build the [ViewInspection.PropertyGroup] for [view].
     *
     * @param includeResolutionStack when true, also fills [ViewInspection.Property.getSource] and
     *   [ViewInspection.Property.getResolutionStackList] for VIEW-category attributes using the
     *   hidden View.getAttributeSourceResourceMap() / View.getAttributeResolutionStack(int) APIs
     *   (API 29+, additionally gated by the device setting `debug_view_attributes`). When the data
     *   is unavailable both are simply left empty.
     */
    fun forView(view: View, includeResolutionStack: Boolean): ViewInspection.PropertyGroup {
        val group = ViewInspection.PropertyGroup.newBuilder()
        group.setViewId(view.uniqueDrawingId)

        // --- View-category attributes -----------------------------------------------------------
        val viewData = viewCache.typeOf(view.javaClass)
        val viewProps = newAccumulators(viewData.metadata)
        val viewReader = ReaderImpl(
            view = view,
            props = viewProps,
            isLayout = false,
            includeResolutionStack = includeResolutionStack,
            // A password field's "text" property is its plaintext (Redaction.kt).
            redactText = Redaction.mustMaskView(view),
        )
        for (companion in viewData.companions) {
            try {
                @Suppress("UNCHECKED_CAST")
                (companion as InspectionCompanion<View>).readProperties(view, viewReader)
            } catch (t: Throwable) {
                // Let UI-thread-only failures (AndroidRuntimeException) propagate so the caller can
                // retry on the main thread; everything else is logged and skipped.
                if (t is android.util.AndroidRuntimeException) throw t
                Log.w(TAG, "readProperties(view) failed for ${view.javaClass.name}", t)
            }
        }
        for (acc in viewProps) {
            acc.build(view)?.let { group.addProperties(it) }
        }

        // --- LayoutParams-category attributes (is_layout = true) --------------------------------
        val lp: ViewGroup.LayoutParams? = view.layoutParams
        if (lp != null) {
            val layoutData = layoutCache.typeOf(lp.javaClass)
            val layoutProps = newAccumulators(layoutData.metadata)
            val layoutReader = ReaderImpl(
                view = view,
                props = layoutProps,
                isLayout = true,
                includeResolutionStack = false, // source/stack are VIEW-only (SimplePropertyReader.kt:159)
                redactText = false,
            )
            for (companion in layoutData.companions) {
                try {
                    @Suppress("UNCHECKED_CAST")
                    (companion as InspectionCompanion<ViewGroup.LayoutParams>)
                        .readProperties(lp, layoutReader)
                } catch (t: Throwable) {
                    if (t is android.util.AndroidRuntimeException) throw t
                    Log.w(TAG, "readProperties(layoutParams) failed for ${lp.javaClass.name}", t)
                }
            }
            for (acc in layoutProps) {
                acc.build(view)?.let { group.addProperties(it) }
            }
        }

        return group.build()
    }

    private fun newAccumulators(metadata: List<PropMeta>): List<PropAccumulator> =
        metadata.map { PropAccumulator(it) }

    /**
     * Build a [ViewInspection.Resource] message for a resource id (used by readResourceId for the
     * RESOURCE property type), or null if the id is not a valid resource. Mirrors
     * tools-base/.../proto/ViewExtensions.kt:144-158 (createResource), adapted to our proto's
     * Resource message and shared string table.
     */
    private fun createResource(view: View, resourceId: Int): ViewInspection.Resource? {
        if (!isValidResourceId(resourceId)) return null
        return try {
            val res = view.resources
            ViewInspection.Resource.newBuilder()
                .setType(strings.intern(res.getResourceTypeName(resourceId)))
                .setNamespace(strings.intern(res.getResourcePackageName(resourceId)))
                .setName(strings.intern(res.getResourceEntryName(resourceId)))
                .build()
        } catch (ex: Resources.NotFoundException) {
            null
        }
    }

    // ----------------------------------------------------------------------------------------
    // Per-type cache: walks superclasses collecting companions + the merged property-metadata
    // table, exactly like PropertyCache.kt.typeOfImpl. The metadata list ordering is the id space
    // the companions' readProperties(...) callbacks index into.
    // ----------------------------------------------------------------------------------------
    private class TypeData(
        val metadata: List<PropMeta>,
        val companions: List<InspectionCompanion<*>>,
    )

    private class TypeCache(
        private val provider: StaticInspectionCompanionProvider,
        private val rootFqcn: String,
        private val gravityMapping: IntFunction<Set<String>>,
    ) {
        private val typeMap = HashMap<Class<*>, TypeData>()

        fun typeOf(clazz: Class<*>): TypeData {
            typeMap[clazz]?.let { return it }
            val data = typeOfImpl(clazz)
            typeMap[clazz] = data
            return data
        }

        private fun typeOfImpl(clazz: Class<*>): TypeData {
            typeMap[clazz]?.let { return it }

            val companion = loadCompanion(clazz)

            // Recurse into the superclass unless we've reached the declared root type
            // (PropertyCache.kt:68-70). canonicalName can be null for local/anonymous classes; in
            // that case keep walking until superclass == null.
            val superclass: Class<*>? = clazz.superclass
            val superData: TypeData? =
                if (clazz.canonicalName != rootFqcn && superclass != null) {
                    typeOfImpl(superclass)
                } else {
                    null
                }

            val companions = ArrayList<InspectionCompanion<*>>()
            superData?.let { companions.addAll(it.companions) }
            companion?.let { companions.add(it) }

            // Start the metadata table from the superclass table so subclass ids continue the
            // parent's id space (PropertyTypeMapper appends; ids are list indices).
            var metadata: MutableList<PropMeta> = ArrayList()
            superData?.let { metadata.addAll(it.metadata) }
            if (companion != null) {
                val mapper = MapperImpl(metadata, gravityMapping)
                try {
                    companion.mapProperties(mapper)
                    metadata = mapper.metadata
                } catch (t: Throwable) {
                    Log.w(TAG, "mapProperties failed for ${clazz.name}", t)
                }
            }

            val data = TypeData(metadata, companions)
            typeMap[clazz] = data
            return data
        }

        @Suppress("UNCHECKED_CAST")
        private fun loadCompanion(clazz: Class<*>): InspectionCompanion<Any>? {
            return try {
                // StaticInspectionCompanionProvider.provide reflectively loads
                // "<clazz.name>$InspectionCompanion" generated by the AndroidX resource-inspection
                // annotation processor (returns null when the class has no companion).
                provider.provide(clazz) as? InspectionCompanion<Any>
            } catch (t: Throwable) {
                Log.w(TAG, "InspectionCompanion lookup failed for ${clazz.name}", t)
                null
            }
        }
    }

    // ----------------------------------------------------------------------------------------
    // PropertyMapper: companion calls mapX(name, attrId) once per type; we append a PropMeta and
    // return its index as the id. Faithful to PropertyTypeMapper.java. For GRAVITY / INT_ENUM /
    // INT_FLAG we additionally record the decode function the reader will apply.
    // ----------------------------------------------------------------------------------------
    private class MapperImpl(
        existing: List<PropMeta>,
        private val gravityMapping: IntFunction<Set<String>>,
    ) : PropertyMapper {
        val metadata: MutableList<PropMeta> = ArrayList(existing)

        private fun map(name: String, attributeId: Int, type: ViewInspection.Property.Type): Int {
            val id = metadata.size
            metadata.add(PropMeta(name, attributeId, type))
            return id
        }

        override fun mapBoolean(name: String, attributeId: Int) =
            map(name, attributeId, ViewInspection.Property.Type.BOOLEAN)

        override fun mapByte(name: String, attributeId: Int) =
            map(name, attributeId, ViewInspection.Property.Type.BYTE)

        override fun mapChar(name: String, attributeId: Int) =
            map(name, attributeId, ViewInspection.Property.Type.CHAR)

        override fun mapDouble(name: String, attributeId: Int) =
            map(name, attributeId, ViewInspection.Property.Type.DOUBLE)

        override fun mapFloat(name: String, attributeId: Int) =
            map(name, attributeId, ViewInspection.Property.Type.FLOAT)

        override fun mapInt(name: String, attributeId: Int) =
            map(name, attributeId, ViewInspection.Property.Type.INT32)

        override fun mapLong(name: String, attributeId: Int) =
            map(name, attributeId, ViewInspection.Property.Type.INT64)

        override fun mapShort(name: String, attributeId: Int) =
            map(name, attributeId, ViewInspection.Property.Type.INT16)

        override fun mapObject(name: String, attributeId: Int) =
            map(name, attributeId, ViewInspection.Property.Type.OBJECT)

        override fun mapColor(name: String, attributeId: Int) =
            map(name, attributeId, ViewInspection.Property.Type.COLOR)

        override fun mapGravity(name: String, attributeId: Int): Int {
            val id = map(name, attributeId, ViewInspection.Property.Type.GRAVITY)
            metadata[id].flagMapping = gravityMapping
            return id
        }

        override fun mapIntEnum(
            name: String,
            attributeId: Int,
            mapping: IntFunction<String>,
        ): Int {
            val id = map(name, attributeId, ViewInspection.Property.Type.INT_ENUM)
            metadata[id].enumMapping = mapping
            return id
        }

        override fun mapIntFlag(
            name: String,
            attributeId: Int,
            mapping: IntFunction<Set<String>>,
        ): Int {
            val id = map(name, attributeId, ViewInspection.Property.Type.INT_FLAG)
            metadata[id].flagMapping = mapping
            return id
        }

        override fun mapResourceId(name: String, attributeId: Int) =
            map(name, attributeId, ViewInspection.Property.Type.RESOURCE)
    }

    // ----------------------------------------------------------------------------------------
    // PropertyReader: companion calls readX(id, value) per instance. We stash the value (and any
    // runtime-refined type) into the matching PropAccumulator. Faithful to SimplePropertyReader.kt.
    // ----------------------------------------------------------------------------------------
    private inner class ReaderImpl(
        private val view: View,
        private val props: List<PropAccumulator>,
        private val isLayout: Boolean,
        private val includeResolutionStack: Boolean,
        // Mask the "text" attribute (a password field's content).
        private val redactText: Boolean,
    ) : PropertyReader {

        // Cache the source map once per reader (SimplePropertyReader.kt:45). Guarded: the API is
        // hidden and only yields data when debug_view_attributes is enabled.
        private val sourceMap: Map<Int, Int> =
            if (includeResolutionStack) safeAttributeSourceResourceMap(view) else emptyMap()

        /**
         * Runs one read callback so a failure drops only that property: the companion goes on
         * reading the rest of the view (an exception escaping a readX call would otherwise end
         * the companion's readProperties for every attribute after it).
         */
        private inline fun guard(id: Int, block: () -> Unit) {
            try {
                block()
            } catch (t: Throwable) {
                failedProperties++
                val acc = props.getOrNull(id)
                acc?.value = null
                val name = acc?.meta?.name ?: "#$id"
                logOnce("read:$name", "reading property $name of ${view.javaClass.name} failed", t)
            }
        }

        override fun readBoolean(id: Int, b: Boolean) = guard(id) { readAny(id, if (b) 1 else 0) }

        override fun readByte(id: Int, b: Byte) = guard(id) { readAny(id, b.toInt()) }

        override fun readChar(id: Int, c: Char) = guard(id) { readAny(id, c.code) }

        override fun readDouble(id: Int, d: Double) = guard(id) { readAny(id, d) }

        override fun readFloat(id: Int, f: Float) = guard(id) { readAny(id, f) }

        override fun readInt(id: Int, i: Int) = guard(id) { readAny(id, i) }

        override fun readLong(id: Int, l: Long) = guard(id) { readAny(id, l) }

        override fun readShort(id: Int, s: Short) = guard(id) { readAny(id, s.toInt()) }

        override fun readObject(id: Int, o: Any?) = guard(id) { readObjectImpl(id, o) }

        private fun readObjectImpl(id: Int, o: Any?) {
            // Runtime type refinement (SimplePropertyReader.kt:79-110). Only sets a value for the
            // recognized object kinds; anything else leaves the property unread (dropped on build).
            when (o) {
                is CharSequence -> {
                    // Any CharSequence, not only String: an EditText's text is an Editable,
                    // most TextViews' a Spanned. Stringified here, on the main thread.
                    props[id].type = ViewInspection.Property.Type.STRING
                    val meta = props[id].meta
                    val isText = meta.attributeId == android.R.attr.text || meta.name == "text"
                    readAny(id, if (redactText && isText) Redaction.mask(o) else o.toString())
                }
                is ColorStateList -> {
                    props[id].type = ViewInspection.Property.Type.COLOR
                    readAny(id, o.getColorForState(view.drawableState, o.defaultColor))
                }
                is ColorDrawable -> {
                    props[id].type = ViewInspection.Property.Type.COLOR
                    readAny(id, o.color)
                }
                is Drawable -> {
                    props[id].type = ViewInspection.Property.Type.DRAWABLE
                    readAny(id, o)
                }
                is Animation -> {
                    props[id].type = ViewInspection.Property.Type.ANIM
                    readAny(id, o)
                }
                else -> {
                    // android.animation.StateListAnimator -> ANIMATOR,
                    // android.graphics.Interpolator / android.animation.TimeInterpolator ->
                    // INTERPOLATOR. Match by class name so we don't hard-depend on classes that may
                    // vary by API surface.
                    if (o != null) {
                        val cn = o.javaClass.name
                        when {
                            isAssignableToName(o, "android.animation.StateListAnimator") -> {
                                props[id].type = ViewInspection.Property.Type.ANIMATOR
                                readAny(id, o)
                            }
                            isAssignableToName(o, "android.animation.TimeInterpolator") ||
                                isAssignableToName(o, "android.graphics.Interpolator") -> {
                                props[id].type = ViewInspection.Property.Type.INTERPOLATOR
                                readAny(id, o)
                            }
                            else -> {
                                // Unrecognized object: keep as OBJECT and stringify its class.
                                Log.v(TAG, "readObject: unhandled object type $cn (id=$id)")
                            }
                        }
                    }
                }
            }
        }

        override fun readColor(id: Int, color: Int) = guard(id) { readAny(id, color) }

        // A ColorLong packs a color space and half-float components; Color.toArgb(long)
        // converts it to the sRGB ARGB int the COLOR type carries (truncating it to Int
        // would read the color-space bits as blue).
        override fun readColor(id: Int, color: Long) = guard(id) { readAny(id, Color.toArgb(color)) }

        // A null Color is "no color": leave the property absent rather than claim 0x00000000.
        override fun readColor(id: Int, color: Color?) = guard(id) {
            if (color != null) readAny(id, color.toArgb())
        }

        override fun readGravity(id: Int, value: Int) = guard(id) { readIntFlagImpl(id, value) }

        override fun readIntEnum(id: Int, value: Int) = guard(id) { readIntEnumImpl(id, value) }

        private fun readIntEnumImpl(id: Int, value: Int) {
            val meta = props[id].meta
            val mapping = meta.enumMapping
            if (mapping != null) {
                val mapped = mapping.apply(value)
                if (mapped != null) {
                    readAny(id, mapped)
                    return
                }
            }
            // Unmapped enum: downgrade to a raw INT32 (SimplePropertyReader.kt:138-139).
            props[id].type = ViewInspection.Property.Type.INT32
            readAny(id, value)
        }

        override fun readIntFlag(id: Int, value: Int) = guard(id) { readIntFlagImpl(id, value) }

        private fun readIntFlagImpl(id: Int, value: Int) {
            val mapping = props[id].meta.flagMapping
            if (mapping != null) {
                // Our proto has no FlagValue; join the set with '|' into a single string and store
                // in str_value (the newer ui-inspector flag encoding).
                readAny(id, mapping.apply(value))
            } else {
                // No mapping registered: fall back to the raw int so the value isn't lost.
                props[id].type = ViewInspection.Property.Type.INT32
                readAny(id, value)
            }
        }

        override fun readResourceId(id: Int, value: Int) = guard(id) {
            val resource = createResource(view, value)
            if (resource != null) {
                readAny(id, resource)
            }
            // If the resource id is invalid, leave the property unread (it will be dropped).
        }

        private fun readAny(id: Int, value: Any?) {
            val acc = props[id]
            acc.value = value
            acc.isLayout = isLayout
            if (!isLayout && includeResolutionStack) {
                // source = the single resource that supplied the value.
                sourceMap[acc.meta.attributeId]?.let { resId ->
                    resolveResourceToString(view, resId)?.let { acc.source = it }
                }
                // resolution_stack = the ordered chain of resources consulted.
                for (resId in safeAttributeResolutionStack(view, acc.meta.attributeId)) {
                    resolveResourceToString(view, resId)?.let { acc.resolutionStack.add(it) }
                }
            }
        }
    }

    // ----------------------------------------------------------------------------------------
    // Accumulator: holds the mutable in-flight state for one attribute id, then emits a Property.
    // Mirrors PropertyBuilder.kt but targets OUR proto (no FlagValue / namespace; flags -> string).
    // ----------------------------------------------------------------------------------------
    private inner class PropAccumulator(val meta: PropMeta) {
        var type: ViewInspection.Property.Type = meta.type
        var value: Any? = null
        var isLayout: Boolean = false
        var source: String? = null
        val resolutionStack: MutableList<String> = ArrayList()

        /**
         * Emit the proto [ViewInspection.Property], or null if no value was ever read for this id
         * (so unread attributes are dropped — PropertyBuilder.build returns null on a null value).
         */
        fun build(view: View): ViewInspection.Property? =
            try {
                buildImpl()
            } catch (t: Throwable) {
                // A value of an unexpected runtime type (a companion that read an Integer for a
                // STRING id, a flag mapping returning null...): drop this property only.
                failedProperties++
                logOnce(
                    "build:${meta.name}",
                    "encoding property ${meta.name} ($type) of ${view.javaClass.name} failed",
                    t,
                )
                null
            }

        private fun buildImpl(): ViewInspection.Property? {
            val v = value ?: return null
            // Protobuf-(lite) generated setters return Builder (fluent), so they are NOT exposed as
            // Kotlin assignable properties — call the explicit setters.
            val b = ViewInspection.Property.newBuilder()
            b.setName(strings.intern(meta.name))
            b.setType(type)
            b.setIsLayout(isLayout)

            when (type) {
                ViewInspection.Property.Type.STRING,
                ViewInspection.Property.Type.INT_ENUM -> {
                    b.setStrValue(strings.intern(v as String))
                }
                ViewInspection.Property.Type.GRAVITY,
                ViewInspection.Property.Type.INT_FLAG -> {
                    @Suppress("UNCHECKED_CAST")
                    val flags = v as Set<String>
                    // Joined "|"-separated flag string in str_value (id 0 when empty).
                    b.setStrValue(strings.intern(if (flags.isEmpty()) "" else flags.joinToString("|")))
                }
                ViewInspection.Property.Type.INT32,
                ViewInspection.Property.Type.INT16,
                ViewInspection.Property.Type.BYTE,
                ViewInspection.Property.Type.CHAR,
                ViewInspection.Property.Type.COLOR,
                ViewInspection.Property.Type.DIMENSION -> {
                    b.setInt32Value((v as Number).toInt())
                }
                ViewInspection.Property.Type.BOOLEAN -> {
                    // readBoolean already widened to 1/0.
                    b.setInt32Value((v as Number).toInt())
                }
                ViewInspection.Property.Type.INT64 -> {
                    b.setInt64Value((v as Number).toLong())
                }
                ViewInspection.Property.Type.DOUBLE -> {
                    b.setDoubleValue((v as Number).toDouble())
                }
                ViewInspection.Property.Type.FLOAT -> {
                    b.setFloatValue((v as Number).toFloat())
                }
                ViewInspection.Property.Type.RESOURCE -> {
                    b.setResourceValue(v as ViewInspection.Resource)
                }
                ViewInspection.Property.Type.OBJECT,
                ViewInspection.Property.Type.DRAWABLE,
                ViewInspection.Property.Type.ANIM,
                ViewInspection.Property.Type.ANIMATOR,
                ViewInspection.Property.Type.INTERPOLATOR -> {
                    // No value to surface beyond identity: intern the runtime class name.
                    b.setStrValue(strings.intern(v.javaClass.name))
                }
                else -> {
                    Log.w(TAG, "Unhandled property type $type for ${meta.name}; dropping")
                    return null
                }
            }

            source?.let { b.setSource(strings.intern(it)) }
            for (entry in resolutionStack) {
                b.addResolutionStack(strings.intern(entry))
            }
            return b.build()
        }
    }
}

/**
 * Immutable per-attribute metadata captured during mapProperties. `attributeId` is the real
 * Android resource attribute id (e.g. android.R.attr.text) used for namespace/source/resolution.
 */
internal class PropMeta(
    val name: String,
    val attributeId: Int,
    val type: ViewInspection.Property.Type,
) {
    // Set by the mapper for GRAVITY/INT_FLAG (flagMapping) and INT_ENUM (enumMapping).
    var enumMapping: IntFunction<String>? = null
    var flagMapping: IntFunction<Set<String>>? = null
}

// ------------------------------------------------------------------------------------------------
// Resource helpers — clean-room equivalents of ViewExtensions.kt.createResource / getNamespace and
// ui-inspector ViewExtensions.kt.resolveResourceToString. All reflective/hidden-API access guarded.
// ------------------------------------------------------------------------------------------------

/**
 * Format a resource id into its canonical reference string (e.g. "@android:style/Widget.Button",
 * "@layout/activity_main", "@com.foo:id/bar"), or null if invalid. Mirrors
 * ui-inspector ViewExtensions.kt:145-164. Used for the interned source / resolution_stack strings.
 */
private fun resolveResourceToString(view: View, resourceId: Int): String? {
    if (!isValidResourceId(resourceId)) return null
    return try {
        val res = view.resources
        val type = res.getResourceTypeName(resourceId)
        val pkg = res.getResourcePackageName(resourceId)
        val name = res.getResourceEntryName(resourceId)
        when (pkg) {
            "android" -> "@android:$type/$name"
            view.context.packageName -> "@$type/$name"
            else -> "@$pkg:$type/$name"
        }
    } catch (ex: Resources.NotFoundException) {
        null
    }
}

/**
 * Fast structural validity check for a resource id, avoiding expensive resolution APIs that throw
 * and spam logcat for invalid ids. Mirrors isValidResourceId in both reference ViewExtensions.kt.
 */
private fun isValidResourceId(resourceId: Int): Boolean {
    if (resourceId == 0) return false
    val packageId = (resourceId ushr 24) and 0xFF
    val typeId = (resourceId ushr 16) and 0xFF
    // package and type must be non-zero; package 0xFF is disallowed by AssetManager2.
    return packageId != 0 && packageId != 0xFF && typeId != 0
}

/**
 * View.getAttributeSourceResourceMap(): Map<attrId, resId>. Present in the API 36 android.jar but
 * historically @hide; it only returns non-empty data when the device setting
 * `debug_view_attributes` is enabled (CONTRACT §6). Wrapped in try/catch so an absent/throwing
 * implementation on some API levels degrades to an empty map rather than failing the dump.
 */
private fun safeAttributeSourceResourceMap(view: View): Map<Int, Int> {
    if (Build.VERSION.SDK_INT < 29) return emptyMap()
    return try {
        view.attributeSourceResourceMap ?: emptyMap()
    } catch (t: Throwable) {
        Log.v("ViewSpector", "getAttributeSourceResourceMap unavailable", t)
        emptyMap()
    }
}

/**
 * View.getAttributeResolutionStack(int attrId): int[]. Present in the API 36 android.jar but
 * historically @hide; only populated when `debug_view_attributes` is enabled. Returns an empty
 * array on any failure (CONTRACT §6: "If unavailable, leave empty").
 */
private fun safeAttributeResolutionStack(view: View, attributeId: Int): IntArray {
    if (Build.VERSION.SDK_INT < 29 || attributeId == 0) return IntArray(0)
    return try {
        view.getAttributeResolutionStack(attributeId) ?: IntArray(0)
    } catch (t: Throwable) {
        Log.v("ViewSpector", "getAttributeResolutionStack unavailable", t)
        IntArray(0)
    }
}

/** True if [o] is an instance of the class/interface named [fqcn] (loaded via o's classloader). */
private fun isAssignableToName(o: Any, fqcn: String): Boolean {
    return try {
        val clazz = Class.forName(fqcn, false, o.javaClass.classLoader)
        clazz.isInstance(o)
    } catch (t: Throwable) {
        false
    }
}
