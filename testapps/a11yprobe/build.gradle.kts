// ============================================================================
// Root build.gradle.kts (testapps/a11yprobe)
// All plugins are declared `apply false` here and applied in :app. Versions are
// pinned in settings.gradle.kts pluginManagement (testapps.md §1).
// ============================================================================
plugins {
    id("com.android.application") apply false
    id("org.jetbrains.kotlin.android") apply false
    id("org.jetbrains.kotlin.plugin.compose") apply false
}
