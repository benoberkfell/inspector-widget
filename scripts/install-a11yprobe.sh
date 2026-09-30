#!/usr/bin/env bash
#
# Inspector Widget — build & install the A11yProbe test app (com.oberkfell.a11yprobe).
#
# A11yProbe is the GOOD/BAD accessibility corpus the Task-B lint rules and the
# integrated component view validate against (testapps.md). It is a STANDALONE
# Gradle build under testapps/a11yprobe (not a viewspector subproject), pinned to
# the same JDK-17..23 / AGP-8.7.2 / Gradle-8.13 / compileSdk-36 matrix as the host.
#
# Pipeline (testapps.md §6):
#   1. select a JDK 17-23 (scripts/lib/select-jdk.sh, same as scripts/build.sh)
#   2. ensure the Gradle wrapper exists in testapps/a11yprobe (copy from root)
#   3. ./gradlew -p testapps/a11yprobe :app:installDebug
#   4. am start the launcher MainActivity
#
# Usage: scripts/install-a11yprobe.sh [SERIAL] [--no-launch]
#   SERIAL defaults to $SERIAL or emulator-5554.
#
# Fails loudly on any error.

set -euo pipefail

# ----------------------------------------------------------------- environment
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$PROJECT_ROOT/testapps/a11yprobe"

ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
export ANDROID_HOME
export ANDROID_SDK_ROOT="$ANDROID_HOME"
ADB="$ANDROID_HOME/platform-tools/adb"

PACKAGE="com.oberkfell.a11yprobe"
LAUNCH=1

log()  { printf '\033[1;34m[a11yprobe]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ ok ]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

# ----------------------------------------------------------------- args
SERIAL="${SERIAL:-emulator-5554}"
for arg in "$@"; do
    case "$arg" in
        --no-launch) LAUNCH=0 ;;
        -*)          die "Unknown flag: $arg" ;;
        *)           SERIAL="$arg" ;;
    esac
done

# A JDK 17-23 runs AGP 8.7.2 / Gradle 8.13 (same selection as scripts/build.sh).
# shellcheck source=lib/select-jdk.sh
. "$PROJECT_ROOT/scripts/lib/select-jdk.sh"
log "Selecting a JDK ($JDK_MIN-$JDK_MAX)..."
select_jdk || exit 1
ok "JAVA_HOME=$JAVA_HOME (JDK $(jdk_major "$JAVA_HOME"))"

[ -d "$APP_DIR" ] || die "App dir not found: $APP_DIR"
[ -x "$ADB" ] || die "adb not found at $ADB"

# --------------------------------------------------------- gradle wrapper
# Reuse the viewspector wrapper (pinned to 8.13). Copy any missing pieces.
if [ ! -x "$APP_DIR/gradlew" ]; then
    log "Copying Gradle wrapper from viewspector root (pinned to 8.13)..."
    [ -x "$PROJECT_ROOT/gradlew" ] || die "Root gradlew missing — run scripts/build.sh first."
    cp "$PROJECT_ROOT/gradlew" "$APP_DIR/gradlew"
    cp "$PROJECT_ROOT/gradlew.bat" "$APP_DIR/gradlew.bat" 2>/dev/null || true
    mkdir -p "$APP_DIR/gradle/wrapper"
    cp "$PROJECT_ROOT/gradle/wrapper/gradle-wrapper.jar" "$APP_DIR/gradle/wrapper/"
    cp "$PROJECT_ROOT/gradle/wrapper/gradle-wrapper.properties" "$APP_DIR/gradle/wrapper/"
    chmod +x "$APP_DIR/gradlew"
    ok "Wrapper copied."
fi

# Ensure local.properties points at the SDK (needed for a standalone build).
if [ ! -f "$APP_DIR/local.properties" ]; then
    log "Writing local.properties (sdk.dir=$ANDROID_HOME)..."
    printf 'sdk.dir=%s\n' "$ANDROID_HOME" > "$APP_DIR/local.properties"
fi

GRADLEW="$APP_DIR/gradlew"

# --------------------------------------------------------------------- build+install
log "Building & installing :app:installDebug to $SERIAL ..."
ANDROID_SERIAL="$SERIAL" "$GRADLEW" -p "$APP_DIR" --no-daemon :app:installDebug \
    || die "Gradle :app:installDebug failed."
ok "Installed $PACKAGE on $SERIAL."

# --------------------------------------------------------------------- launch
if [ "$LAUNCH" -eq 1 ]; then
    log "Launching $PACKAGE/.MainActivity ..."
    "$ADB" -s "$SERIAL" shell am start -n "$PACKAGE/.MainActivity" \
        || die "Failed to launch MainActivity."
    ok "Launched. Attach Inspector Widget with: scripts/run.sh $SERIAL $PACKAGE"
else
    ok "Skipping launch (--no-launch). Attach with: scripts/run.sh $SERIAL $PACKAGE"
fi
