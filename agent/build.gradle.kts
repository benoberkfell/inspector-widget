import com.android.build.api.artifact.ScopedArtifact
import com.android.build.api.variant.ScopedArtifacts
import com.google.protobuf.gradle.id
import com.google.protobuf.gradle.proto
import org.jetbrains.kotlin.gradle.dsl.JvmTarget
import org.objectweb.asm.ClassReader
import org.objectweb.asm.ClassWriter
import org.objectweb.asm.commons.ClassRemapper
import org.objectweb.asm.commons.Remapper
import java.io.File
import java.util.Properties
import java.util.jar.JarFile
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

// ViewSpector — agent module.
//
// This is a *code carrier*: a debuggable com.android.application with NO
// components (no Activity/Service). We build it as an APK only to get two
// products out the other side, which scripts/build.sh extracts:
//   1. lib/arm64-v8a/libviewspector.so   — the JVMTI native agent
//   2. classes*.dex                       — the Kotlin payload + generated proto,
//                                           repackaged into payload.jar
//
// The proto is compiled to JAVALITE in-module (DexClassLoader-friendly, no full
// protobuf runtime), from the single shared schema at ../proto. See CONTRACT §3/§5.

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.google.protobuf")
}

// --- Toolchain auto-resolution ------------------------------------------------
// SDK component versions auto-resolve to whatever the host has installed, so a
// fresh machine never has to hand-edit a pinned version. Precedence per value:
// an explicit gradle -P property / env var, else the newest installed, else a
// known-good fallback. (The CONTRACT versions are reproducibility hints, not floors.)
val androidSdkDir: File? =
    (System.getenv("ANDROID_HOME") ?: System.getenv("ANDROID_SDK_ROOT"))?.let { File(it) }
        ?: rootProject.file("local.properties").takeIf { it.exists() }?.let { lp ->
            Properties().apply { lp.inputStream().use { load(it) } }
                .getProperty("sdk.dir")?.let { File(it) }
        }
val versionOrder = Comparator<String> { a, b ->
    val pa = a.split('.', '-').map { it.toIntOrNull() ?: 0 }
    val pb = b.split('.', '-').map { it.toIntOrNull() ?: 0 }
    (0 until maxOf(pa.size, pb.size))
        .firstNotNullOfOrNull { i -> pa.getOrElse(i) { 0 }.compareTo(pb.getOrElse(i) { 0 }).takeIf { it != 0 } }
        ?: 0
}
fun newestInstalled(component: String): String? =
    androidSdkDir?.resolve(component)?.takeIf { it.isDirectory }
        ?.listFiles { f -> f.isDirectory }?.map { it.name }?.maxWithOrNull(versionOrder)

val resolvedCompileSdk = (findProperty("compileSdkOverride") as String?)?.toIntOrNull() ?: 36
val resolvedNdk = (findProperty("ndkVersion") as String?)
    ?: System.getenv("NDK_VERSION") ?: newestInstalled("ndk") ?: "27.1.12297006"
val resolvedCmake = (findProperty("cmakeVersion") as String?) ?: newestInstalled("cmake")

android {
    namespace = "com.oberkfell.viewspector.agent"
    // Auto-resolved (see the toolchain block above). buildToolsVersion is left
    // unset so AGP selects a compatible installed build-tools automatically.
    compileSdk = resolvedCompileSdk
    ndkVersion = resolvedNdk

    defaultConfig {
        applicationId = "com.oberkfell.viewspector.agent"
        minSdk = 29
        targetSdk = 36
        versionCode = 1
        versionName = "1.0"

        ndk {
            // arm64-v8a only — the target emulator is arm64 (CONTRACT §0).
            abiFilters += "arm64-v8a"
        }

        externalNativeBuild {
            cmake {
                // c++17, and pass JAVA_HOME through so CMake can vendor the JDK's
                // jvmti.h (the NDK sysroot does not ship one — see CMakeLists.txt).
                cppFlags += "-std=c++17"
                arguments += "-DANDROID_STL=c++_static"
            }
        }
    }

    externalNativeBuild {
        cmake {
            path = file("src/main/cpp/CMakeLists.txt")
            // Auto-resolved to an installed CMake (override via -PcmakeVersion);
            // unset lets AGP use its bundled/compatible CMake.
            resolvedCmake?.let { version = it }
        }
    }

    buildTypes {
        // Only debug is meaningful: the app must be debuggable for hidden-API
        // reflection (SkiaQWorkaround-style access) and for attach-agent to work.
        getByName("debug") {
            isDebuggable = true
            isMinifyEnabled = false
        }
        getByName("release") {
            isMinifyEnabled = false
            isDebuggable = true
        }
    }

    // Java 17 bytecode, compiled by whichever JDK runs Gradle (17-23). No
    // toolchain is requested on purpose: jvmToolchain(N) demands an *exact* JDK N
    // be installed, which broke builds on machines with only a newer JDK.
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    // The payload runs as a child of the *app* classloader at runtime. We never
    // ship androidx into it. Keep the APK free of unused resources/packaging.
    packaging {
        resources {
            excludes += setOf(
                "META-INF/*.kotlin_module",
                "META-INF/AL2.0",
                "META-INF/LGPL2.1",
                "META-INF/DEPENDENCIES",
                "DebugProbesKt.bin",
            )
        }
        // Keep the .so uncompressed-ness up to AGP defaults; we extract it raw.
        jniLibs {
            useLegacyPackaging = false
        }
    }

    lint {
        abortOnError = false
        checkReleaseBuilds = false
    }
}

kotlin {
    // Match the Java target above (Kotlin would otherwise default jvmTarget to
    // the running JDK and fail the JVM-target consistency check).
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

protobuf {
    protoc {
        // Pinned protoc matching the runtime (protobuf-javalite 3.25.5).
        artifact = "com.google.protobuf:protoc:3.25.5"
    }
    generateProtoTasks {
        all().forEach { task ->
            task.builtins {
                // Configure the built-in `java` generator to emit the LITE
                // runtime (protobuf-javalite). `id("java")` create-or-configures
                // the existing builtin; adding `option("lite")` switches it to
                // lite codegen, which matches the protobuf-javalite dependency
                // and keeps the dex small + DexClassLoader-friendly (CONTRACT §5).
                id("java") {
                    option("lite")
                }
            }
        }
    }
}

// Feed the single shared schema (CONTRACT: one proto, ../proto) into this
// module's "main" proto source set. java_package/outer_classname in the file
// pin the generated classes to com.oberkfell.viewspector.proto.ViewInspection.
// The `proto { }` extension on AGP source sets is contributed by the
// com.google.protobuf plugin (imported above as `proto`).
android.sourceSets.getByName("main").proto {
    srcDir(rootProject.layout.projectDirectory.dir("proto").asFile.absolutePath)
}

dependencies {
    // Lite runtime only — no androidx.inspection, no Google inspector jars
    // (CONTRACT §8). This is the *sole* third-party dep that ends up in the dex.
    implementation("com.google.protobuf:protobuf-javalite:3.25.5")
}

// --- Classloader isolation: relocate the bundled libraries -------------------
// Bootstrap loads payload.jar in a DexClassLoader whose parent is the APP
// classloader, and class loading is parent-first, so any class the app also
// defines resolves to the app's copy, not ours. Most apps bundle the Kotlin
// stdlib and many bundle protobuf-lite, usually another version, and R8 strips
// whatever the app itself does not call. On an R8-shrunk app the payload then
// dies on its first line (NoSuchMethodError: Intrinsics.checkNotNullParameter),
// or later with IllegalAccessError from package-private access between our
// classes and the app's copies of the same package.
//
// So every library class in the payload moves under payloadShadedRoot before
// dexing: kotlin.Unit becomes com.oberkfell.viewspector.shaded.kotlin.Unit, and
// so on. The payload then defines only com.oberkfell.viewspector.* classes,
// which no app defines, so nothing it links against can be replaced. The
// framework (android.*, java.*) still comes from the boot classloader, and the
// app's own types (androidx.*, Compose, *$InspectionCompanion) are reached only
// by reflection through the view's own classloader, as before.
//
// Why relocation rather than a child-first loader (DelegateLastClassLoader, API
// 27+): child-first keeps the payload's copies first but under the same names
// as the app's, so a lookup of any class the payload does not bundle (the
// stdlib's reflective probe for kotlin-reflect, a class a newer stdlib added)
// still falls through to the app's copy, which links against the app's half of
// that package. Relocated names cannot collide under any loader order, a
// dex-level check proves it (scripts/shadow_check.py), and Bootstrap and the
// attach path stay exactly as they are.
//
// String constants inside the relocated library classes are rewritten too: they
// name classes the libraries load reflectively (the stdlib's
// "kotlin.reflect.jvm.internal.ReflectionFactoryImpl", protobuf's
// "com.google.protobuf.ExtensionRegistry" and other full-runtime probes). Left
// alone, Class.forName would find the APP's copy and hand our relocated code an
// instance of the app's class. Strings in our own classes are never rewritten:
// they name app classes on purpose. Kotlin types never cross into app code: the
// payload talks to the app through java.* / android.* types and reflection only.
//
// This runs as an AGP whole-program class transform (every class of the
// variant, project and dependencies) just before dexing, so the APK that
// scripts/build.sh unpacks into payload.jar already carries the relocated dex.
// scripts/shadow_check.py and host/tests/test_payload_isolation.py check it.

/** Library packages bundled in the payload that must not resolve against the app. */
val payloadRelocatedPackages = listOf(
    "kotlin/",
    "kotlinx/",
    "org/jetbrains/annotations/",
    "org/intellij/lang/annotations/",
    "com/google/protobuf/",
)

/** Where [payloadRelocatedPackages] go; must stay inside com/oberkfell/viewspector/. */
val payloadShadedRoot = "com/oberkfell/viewspector/shaded/"

abstract class RelocatePayloadClassesTask : DefaultTask() {
    @get:InputFiles
    @get:PathSensitive(PathSensitivity.RELATIVE)
    abstract val allJars: ListProperty<RegularFile>

    @get:InputFiles
    @get:PathSensitive(PathSensitivity.RELATIVE)
    abstract val allDirectories: ListProperty<Directory>

    /** Internal-name prefixes (with a trailing slash) to relocate. */
    @get:Input
    abstract val packages: ListProperty<String>

    /** Internal-name prefix (with a trailing slash) the packages move under. */
    @get:Input
    abstract val shadedRoot: Property<String>

    @get:OutputFile
    abstract val output: RegularFileProperty

    /** Maps relocated type names everywhere, and string constants only inside relocated classes. */
    private class Relocator(private val packages: List<String>, private val root: String) : Remapper() {
        private val dottedPackages = packages.map { it.replace('/', '.') }
        private val dottedRoot = root.replace('/', '.')

        /** True while remapping a class that is itself being relocated (a library class). */
        var inLibraryClass = false

        fun relocates(internalName: String): Boolean = packages.any { internalName.startsWith(it) }

        override fun map(internalName: String): String =
            if (relocates(internalName)) root + internalName else internalName

        override fun mapValue(value: Any?): Any? {
            if (value !is String) return super.mapValue(value)
            if (!inLibraryClass) return value
            for (i in packages.indices) {
                if (value.startsWith(packages[i])) return root + value
                if (value.startsWith(dottedPackages[i])) return dottedRoot + value
            }
            return value
        }
    }

    @TaskAction
    fun relocate() {
        val root = shadedRoot.get()
        require(root.endsWith("/") && root.startsWith("com/oberkfell/viewspector/")) {
            "shadedRoot must be an internal-name prefix inside com/oberkfell/viewspector/, got '$root'"
        }
        val relocator = Relocator(packages.get(), root)
        // Sorted, first wins, fixed timestamps: the jar (and so the dex and
        // BUILD_ID) is reproducible.
        val classes = sortedMapOf<String, ByteArray>()
        var relocated = 0

        fun add(entryName: String, bytes: ByteArray) {
            // Only classes: payload.jar carries dex alone, so resources never reach the device.
            if (!entryName.endsWith(".class")) return
            if (entryName.startsWith("META-INF/") || entryName.endsWith("module-info.class")) return
            val internalName = entryName.removeSuffix(".class")
            val library = relocator.relocates(internalName)
            val outName = relocator.map(internalName) + ".class"
            if (classes.containsKey(outName)) return
            relocator.inLibraryClass = library
            val writer = ClassWriter(0)
            ClassReader(bytes).accept(ClassRemapper(writer, relocator), 0)
            classes[outName] = writer.toByteArray()
            if (library) relocated++
        }

        allJars.get().forEach { jar ->
            JarFile(jar.asFile).use { jf ->
                jf.entries().asSequence().filter { !it.isDirectory }.forEach { e ->
                    add(e.name, jf.getInputStream(e).use { it.readBytes() })
                }
            }
        }
        allDirectories.get().forEach { dir ->
            val base = dir.asFile
            base.walkTopDown().filter { it.isFile }.forEach { f ->
                add(f.relativeTo(base).invariantSeparatorsPath, f.readBytes())
            }
        }

        val out = output.get().asFile
        out.parentFile.mkdirs()
        ZipOutputStream(out.outputStream().buffered()).use { zip ->
            for ((name, bytes) in classes) {
                val entry = ZipEntry(name)
                entry.time = 0L
                zip.putNextEntry(entry)
                zip.write(bytes)
                zip.closeEntry()
            }
        }
        logger.lifecycle(
            "payload: relocated $relocated library classes under ${root.replace('/', '.')} " +
                "(${classes.size} classes in all)"
        )
    }
}

androidComponents {
    onVariants { variant ->
        val relocate = tasks.register<RelocatePayloadClassesTask>(
            "relocate${variant.name.replaceFirstChar { it.uppercase() }}PayloadClasses"
        ) {
            packages.set(payloadRelocatedPackages)
            shadedRoot.set(payloadShadedRoot)
        }
        variant.artifacts.forScope(ScopedArtifacts.Scope.ALL)
            .use(relocate)
            .toTransform(
                ScopedArtifact.CLASSES,
                RelocatePayloadClassesTask::allJars,
                RelocatePayloadClassesTask::allDirectories,
                RelocatePayloadClassesTask::output,
            )
    }
}
