// ============================================================================
// app/build.gradle.kts (testapps/a11yprobe)
// Compose + classic-View test app. Toolchain pinned to match the viewspector
// host exactly (testapps.md §1): compileSdk 36, minSdk 29, Java 17 bytecode
// (builds on any JDK 17-23), debuggable, Compose BOM 2024.09.00 (see
// a11yprobe.composeBom below), Kotlin-2.0
// compose compiler plugin.
// ============================================================================
import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose") // Kotlin 2.0 → separate compose plugin
}

android {
    namespace = "com.oberkfell.a11yprobe"
    compileSdk = 36
    buildToolsVersion = "36.1.0"

    defaultConfig {
        applicationId = "com.oberkfell.a11yprobe"
        minSdk = 29
        targetSdk = 36
        versionCode = 1
        versionName = "1.0"
    }

    buildTypes {
        getByName("debug") {
            isDebuggable = true
            isMinifyEnabled = false
        }
    }

    buildFeatures {
        compose = true
        viewBinding = true
    }

    // Java 17 bytecode from whichever JDK runs Gradle; no exact-JDK toolchain.
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
        // Lambdas as classes, not invokedynamic (the Kotlin 2.0 default), so the slot table
        // shows A11yProbe's own composables with their parameters: ui-tooling-data 1.7 reads
        // a call's parameters from the captured fields of its restart lambda by name, and
        // an indy lambda has none. The redaction check (host/tests/test_device_redaction.py)
        // needs PasswordWrapper's parameters to be there to see them masked.
        freeCompilerArgs.add("-Xlambdas=class")
    }
}

// The Compose BOM. The default (ui 1.7.0) exercises the agent's instance
// setTraversalValues path; -Pa11yprobe.composeBom=2025.06.00 (ui 1.8.2) builds the same
// app on Compose's static _androidKt.setTraversalValues path (ComposeTraversal.kt), which
// every Compose from 1.8 to 1.12 uses. Newer BOMs need AGP 9 and compileSdk 37.
val composeBomVersion = (project.findProperty("a11yprobe.composeBom") as String?) ?: "2024.09.00"

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:$composeBomVersion")
    implementation(composeBom)

    // Compose UI + Material3.
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.material3:material3")
    // Vector icons used by the launcher / scenarios (Favorite, ArrowBack, …).
    implementation("androidx.compose.material:material-icons-core")
    implementation("androidx.activity:activity-compose:1.9.2")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.3")

    // classic-View (XML) screen → exercises the AccessibilityNodeInfo path.
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.13.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.0")
    implementation("androidx.core:core-ktx:1.13.1")

    // Mixed View/Compose interop scenarios (InteropActivity): a Fragment host,
    // RecyclerView lists whose cells are ComposeView / classic View / hybrid rows,
    // and a DialogFragment window.
    implementation("androidx.fragment:fragment-ktx:1.8.3")
    implementation("androidx.recyclerview:recyclerview:1.3.2")

    debugImplementation("androidx.compose.ui:ui-tooling")
}
