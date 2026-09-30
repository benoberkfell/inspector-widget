import com.google.protobuf.gradle.id
import com.google.protobuf.gradle.proto
import org.jetbrains.kotlin.gradle.dsl.JvmTarget
import java.io.File
import java.util.Properties

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
