// ViewSpector — bootstrap module.
//
// Bootstrap.java is the *single* class the native agent FindClass-es after
// AddToBootstrapClassLoaderSearch(bootstrap.dex) (CONTRACT §2/§3). It must be
// framework-only Java (NO Kotlin stdlib) so it stays clean inside the bootstrap
// classloader; it then DexClassLoaders payload.jar as a child of the app
// classloader and hands off to Payload.start.
//
// This is a plain java-library producing ONLY Bootstrap's bytecode as a jar.
// scripts/build.sh d8's that jar into bootstrap.dex. Public framework classes
// (Looper, Log, dalvik.system.DexClassLoader, ...) are provided at compile time
// by the platform android.jar as compileOnly, and exist on-device at runtime —
// they must NOT be bundled.
//
// NOTE for the bootstrap-module author: the public android.jar stub does NOT
// expose hidden classes such as android.app.ActivityThread. To find the app
// ClassLoader, use the public path
//   Looper.getMainLooper().getThread().getContextClassLoader()
// (as the reference ui-inspector InspectorService does), or reach
// ActivityThread.currentApplication() via reflection. Referencing ActivityThread
// directly will NOT compile against this classpath.

import java.util.Properties

plugins {
    `java-library`
}

java {
    // Java 11 bytecode, per the module brief. d8 happily lowers this for dex.
    sourceCompatibility = JavaVersion.VERSION_11
    targetCompatibility = JavaVersion.VERSION_11
}

// Resolve the platform android.jar from the SDK declared in local.properties
// (sdk.dir). compileOnly => present for compilation, absent from the jar.
val sdkDir: String = run {
    val props = Properties()
    val f = rootProject.file("local.properties")
    if (f.exists()) {
        f.inputStream().use { stream -> props.load(stream) }
    }
    props.getProperty("sdk.dir")
        ?: System.getenv("ANDROID_HOME")
        ?: System.getenv("ANDROID_SDK_ROOT")
        ?: error("Android SDK location not found: set sdk.dir in local.properties or ANDROID_HOME.")
}

val platformAndroidJar = "$sdkDir/platforms/android-36/android.jar"

dependencies {
    // Framework stubs for compilation only; never packaged into bootstrap.jar.
    compileOnly(files(platformAndroidJar))
}

tasks.named<Jar>("jar") {
    archiveBaseName.set("bootstrap")
    // Deterministic jar: no timestamps, stable ordering. Keeps the dexed output
    // reproducible (CONTRACT: reproducible build).
    isPreserveFileTimestamps = false
    isReproducibleFileOrder = true
    // Only Bootstrap's own classes; android.jar is compileOnly so nothing else
    // can leak in.
    from(sourceSets.main.get().output)
}
