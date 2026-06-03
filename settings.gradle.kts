// ViewSpector — root settings.
// Owns plugin resolution (pluginManagement) and dependency repositories so the
// per-module build.gradle.kts files only declare *which* plugins they apply,
// never versions. Versions are pinned here exactly per CONTRACT §0/§7.

pluginManagement {
    repositories {
        google {
            content {
                includeGroupByRegex("com\\.android.*")
                includeGroupByRegex("com\\.google.*")
                includeGroupByRegex("androidx.*")
            }
        }
        mavenCentral()
        gradlePluginPortal()
    }
    plugins {
        id("com.android.application") version "8.7.2"
        id("org.jetbrains.kotlin.android") version "2.0.21"
        id("com.google.protobuf") version "0.9.4"
    }
}

@Suppress("UnstableApiUsage")
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.PREFER_PROJECT)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "viewspector"

include(":agent")
include(":bootstrap")
