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
#   4. am start the launcher MainActivity (or one scenario, with --scenario)
#
# Usage: scripts/install-a11yprobe.sh [SERIAL] [--no-launch] [--scenario ID] [--compose-bom V]
#   SERIAL defaults to $SERIAL, then $ANDROID_SERIAL, then emulator-5554.
#   --compose-bom V builds against Compose BOM V instead of 2024.09.00 (ui 1.7.0), e.g.
#   2025.06.00 (ui 1.8.2) to exercise the agent's Compose 1.8+ traversal-order path.
#   --scenario ID launches that scenario directly: a Compose id (icon_button, ...,
#   or "all"), view_xml, or an interop id S1..S6 / D1 / D2. See the top of
#   testapps/a11yprobe/app/src/main/kotlin/com/oberkfell/a11yprobe/MainActivity.kt.
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
SERIAL="${SERIAL:-${ANDROID_SERIAL:-emulator-5554}}"
SCENARIO=""
GRADLE_PROPS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --no-launch) LAUNCH=0 ;;
        --scenario)  [ $# -ge 2 ] || die "--scenario needs an id"; SCENARIO="$2"; shift ;;
        --compose-bom) [ $# -ge 2 ] || die "--compose-bom needs a version"
                     GRADLE_PROPS+=("-Pa11yprobe.composeBom=$2"); shift ;;
        -*)          die "Unknown flag: $1" ;;
        *)           SERIAL="$1" ;;
    esac
    shift
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
ANDROID_SERIAL="$SERIAL" "$GRADLEW" -p "$APP_DIR" --no-daemon ${GRADLE_PROPS[@]+"${GRADLE_PROPS[@]}"} :app:installDebug \
    || die "Gradle :app:installDebug failed."
ok "Installed $PACKAGE on $SERIAL."

# --------------------------------------------------------------------- launch
if [ "$LAUNCH" -eq 1 ]; then
    if [ -n "$SCENARIO" ]; then
        log "Launching scenario $SCENARIO ..."
        "$ADB" -s "$SERIAL" shell am start -S -W -n "$PACKAGE/.MainActivity" --es scenario "$SCENARIO" \
            || die "Failed to launch scenario $SCENARIO."
    else
        log "Launching $PACKAGE/.MainActivity ..."
        "$ADB" -s "$SERIAL" shell am start -n "$PACKAGE/.MainActivity" \
            || die "Failed to launch MainActivity."
    fi
    ok "Launched. Attach Inspector Widget with: scripts/run.sh $SERIAL $PACKAGE"
else
    ok "Skipping launch (--no-launch). Attach with: scripts/run.sh $SERIAL $PACKAGE"
fi
