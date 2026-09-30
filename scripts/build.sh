#!/usr/bin/env bash
#
# Inspector Widget — build all three on-device artifacts into build-out/:
#   libviewspector.so   (JVMTI native agent, arm64-v8a)
#   bootstrap.dex       (the FindClass target; d8'd from bootstrap.jar)
#   payload.jar         (dex-in-jar: Kotlin payload + generated proto, for DexClassLoader)
#
# Pipeline (CONTRACT §2/§7):
#   1. ./gradlew :agent:assembleDebug :bootstrap:jar
#   2. From the agent APK: pull lib/arm64-v8a/libviewspector.so
#                          and repackage classes*.dex -> payload.jar
#   3. d8 the bootstrap jar -> bootstrap.dex
#   4. copy all three into build-out/
#
# Fails loudly on any error.

set -euo pipefail

# ----------------------------------------------------------------- environment
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
export ANDROID_HOME
export ANDROID_SDK_ROOT="$ANDROID_HOME"

# d8 (from build-tools) and a platform android.jar (d8 --lib bootclasspath) both
# auto-resolve to the newest installed. Override with BUILD_TOOLS_VERSION / PLATFORM_JAR.
if [ -n "${BUILD_TOOLS_VERSION:-}" ]; then
    BUILD_TOOLS="$ANDROID_HOME/build-tools/$BUILD_TOOLS_VERSION"
else
    BUILD_TOOLS="$(ls -d "$ANDROID_HOME"/build-tools/*/ 2>/dev/null | sort -V | tail -1)"
    BUILD_TOOLS="${BUILD_TOOLS%/}"
fi
D8="$BUILD_TOOLS/d8"
PLATFORM_JAR="${PLATFORM_JAR:-$(ls "$ANDROID_HOME"/platforms/android-*/android.jar 2>/dev/null | sort -V | tail -1)}"

OUT_DIR="$PROJECT_ROOT/build-out"
WORK_DIR="$PROJECT_ROOT/build/viewspector-package"

log()  { printf '\033[1;34m[build]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ ok ]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

# Gradle needs a JDK 17-23 (AGP 8.7.2 floor; Gradle 8.13 ceiling). The modules
# compile to Java 17 bytecode, so any JDK in range works. An in-range JAVA_HOME
# is honoured; otherwise one is auto-selected (see scripts/lib/select-jdk.sh).
# shellcheck source=lib/select-jdk.sh
. "$PROJECT_ROOT/scripts/lib/select-jdk.sh"
log "Selecting a JDK ($JDK_MIN-$JDK_MAX)..."
select_jdk || exit 1
ok "JAVA_HOME=$JAVA_HOME (JDK $(jdk_major "$JAVA_HOME"))"

[ -n "$BUILD_TOOLS" ] && [ -x "$D8" ] \
    || die "d8 not found under $ANDROID_HOME/build-tools — install any build-tools (e.g. sdkmanager 'build-tools;36.0.0')."
[ -n "$PLATFORM_JAR" ] && [ -f "$PLATFORM_JAR" ] \
    || die "no android.jar under $ANDROID_HOME/platforms — install a platform (e.g. sdkmanager 'platforms;android-36')."
ok "build-tools $(basename "$BUILD_TOOLS")  |  platform $(basename "$(dirname "$PLATFORM_JAR")")"

# ------------------------------------------------------------- gradle wrapper
# The wrapper jar/scripts are generated once if absent, pinned to 8.13.
if [ ! -x "$PROJECT_ROOT/gradlew" ]; then
    log "Gradle wrapper missing — generating (pinned to 8.13)..."
    command -v gradle >/dev/null 2>&1 \
        || die "No 'gradle' on PATH to bootstrap the wrapper. Install Gradle or commit gradlew."
    gradle wrapper --gradle-version 8.13 --distribution-type bin \
        || die "Failed to generate the Gradle wrapper."
    ok "Wrapper generated."
fi
GRADLEW="$PROJECT_ROOT/gradlew"

# --------------------------------------------------------------------- build
log "Assembling :agent:assembleDebug and :bootstrap:jar ..."
"$GRADLEW" --no-daemon :agent:assembleDebug :bootstrap:jar \
    || die "Gradle build failed."
ok "Gradle build complete."

# --------------------------------------------------- locate the agent APK
APK="$(find "$PROJECT_ROOT/agent/build/outputs/apk/debug" -name '*.apk' -print -quit 2>/dev/null || true)"
[ -n "$APK" ] && [ -f "$APK" ] || die "Agent APK not found under agent/build/outputs/apk/debug."
ok "Agent APK: $APK"

BOOTSTRAP_JAR="$(find "$PROJECT_ROOT/bootstrap/build/libs" -name 'bootstrap*.jar' -print -quit 2>/dev/null || true)"
[ -n "$BOOTSTRAP_JAR" ] && [ -f "$BOOTSTRAP_JAR" ] || die "bootstrap jar not found under bootstrap/build/libs."
ok "Bootstrap jar: $BOOTSTRAP_JAR"

# ------------------------------------------------------------ stage outputs
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR" "$OUT_DIR"

# (1) native lib: lib/arm64-v8a/libviewspector.so
log "Extracting libviewspector.so (arm64-v8a) from the APK..."
SO_ENTRY="lib/arm64-v8a/libviewspector.so"
unzip -o -q "$APK" "$SO_ENTRY" -d "$WORK_DIR" \
    || die "APK does not contain $SO_ENTRY — did CMake build the native agent?"
cp "$WORK_DIR/$SO_ENTRY" "$OUT_DIR/libviewspector.so"
ok "-> $OUT_DIR/libviewspector.so"

# (2) payload.jar: repackage the APK's classes*.dex into a jar DexClassLoader can load.
log "Repackaging payload dex -> payload.jar ..."
DEX_DIR="$WORK_DIR/dex"
mkdir -p "$DEX_DIR"
# List dex entries in the APK and extract them.
DEX_ENTRIES="$(unzip -Z1 "$APK" 'classes*.dex' 2>/dev/null || true)"
[ -n "$DEX_ENTRIES" ] || die "APK contains no classes*.dex — payload would be empty."
unzip -o -q "$APK" 'classes*.dex' -d "$DEX_DIR" || die "Failed to extract dex from APK."
# A jar is just a zip; placing classesN.dex at the root makes it DexClassLoader-loadable.
PAYLOAD_JAR="$OUT_DIR/payload.jar"
rm -f "$PAYLOAD_JAR"
(
    cd "$DEX_DIR"
    # Deterministic: sorted entries, no extra metadata.
    zip -q -X "$PAYLOAD_JAR" classes*.dex
) || die "Failed to build payload.jar."
ok "-> $PAYLOAD_JAR ($(unzip -Z1 "$PAYLOAD_JAR" | tr '\n' ' '))"

# (3) bootstrap.dex: d8 the bootstrap jar (Java 11 bytecode) into a single dex.
log "Dexing bootstrap.jar -> bootstrap.dex ..."
D8_OUT="$WORK_DIR/bootstrap-dex"
mkdir -p "$D8_OUT"
"$D8" \
    --min-api 29 \
    --lib "$PLATFORM_JAR" \
    --output "$D8_OUT" \
    "$BOOTSTRAP_JAR" \
    || die "d8 failed on bootstrap.jar."
[ -f "$D8_OUT/classes.dex" ] || die "d8 produced no classes.dex."
cp "$D8_OUT/classes.dex" "$OUT_DIR/bootstrap.dex"
ok "-> $OUT_DIR/bootstrap.dex"

# ------------------------------------------------------------------- summary
log "Artifacts in $OUT_DIR:"
ls -l "$OUT_DIR/libviewspector.so" "$OUT_DIR/bootstrap.dex" "$OUT_DIR/payload.jar" \
    | sed 's/^/    /'
ok "Inspector Widget build complete."
