// ViewSpector — root build script.
//
// All plugin *versions* are pinned in settings.gradle.kts (pluginManagement).
// Here we only declare the plugins as available-but-not-applied so the
// subprojects can `apply` them by id without re-stating versions. This keeps
// the version matrix (AGP 8.7.2 / Kotlin 2.0.21 / protobuf-plugin 0.9.4) in one
// place, per CONTRACT §7.

plugins {
    id("com.android.application") apply false
    id("org.jetbrains.kotlin.android") apply false
    id("com.google.protobuf") apply false
}

// Convenience aggregate: `./gradlew buildArtifacts` produces both on-device
// halves the host needs (the agent APK carries libviewspector.so + payload dex;
// the bootstrap jar is d8'd into bootstrap.dex by scripts/build.sh).
tasks.register("buildArtifacts") {
    group = "viewspector"
    description = "Assembles the agent APK and the bootstrap jar."
    dependsOn(":agent:assembleDebug", ":bootstrap:jar")
}
