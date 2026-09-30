#!/usr/bin/env bash
#
# Inspector Widget — smoke test.
#
# Injects the Inspector Widget agent into com.oberkfell.a11yprobe on emulator-5554
# (CONTRACT §0) and dumps the View tree, proving the full pipeline end-to-end:
#   build artifacts present  ->  python host driver  ->  attach-agent  ->
#   framed protobuf over the forwarded socket  ->  DumpTreeResponse.
#
# Usage:
#   scripts/run.sh [SERIAL] [PACKAGE]
# Defaults: SERIAL=emulator-5554  PACKAGE=com.oberkfell.a11yprobe
#
# Fails loudly with actionable guidance at each gate.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

SERIAL="${1:-emulator-5554}"
PACKAGE="${2:-com.oberkfell.a11yprobe}"

# Same lookup order as the host (inspector_widget.inject.resolve_build_out).
OUT_DIR="${INSPECTOR_WIDGET_ARTIFACTS:-${VIEWSPECTOR_ARTIFACTS:-$PROJECT_ROOT/build-out}}"
HOST_DIR="$PROJECT_ROOT/host"

ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
export ANDROID_HOME
export ANDROID_SDK_ROOT="$ANDROID_HOME"
ADB="${ADB:-$ANDROID_HOME/platform-tools/adb}"
command -v "$ADB" >/dev/null 2>&1 || ADB="adb"

PYTHON="${PYTHON:-python3}"

log()  { printf '\033[1;34m[run]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ ok ]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------- preconditions
log "Inspector Widget smoke test  (serial=$SERIAL  package=$PACKAGE)"

for f in libviewspector.so bootstrap.dex payload.jar; do
    [ -f "$OUT_DIR/$f" ] || die "Missing artifact $OUT_DIR/$f — run scripts/build.sh first."
done
ok "Artifacts present in $OUT_DIR."

command -v "$PYTHON" >/dev/null 2>&1 || die "python3 not found on PATH."
[ -d "$HOST_DIR" ] || die "Host directory not found at $HOST_DIR."

# Device reachable?
if ! "$ADB" -s "$SERIAL" get-state >/dev/null 2>&1; then
    die "Device '$SERIAL' is not connected. Check 'adb devices'."
fi
ok "Device $SERIAL is online."

# Package installed?
if ! "$ADB" -s "$SERIAL" shell pm list packages 2>/dev/null | grep -q "package:$PACKAGE\b"; then
    die "Package '$PACKAGE' is not installed on $SERIAL."
fi

# App running? attach-agent needs a live process.
PID="$("$ADB" -s "$SERIAL" shell pidof "$PACKAGE" 2>/dev/null | tr -d '\r' | awk '{print $1}')"
if [ -z "${PID:-}" ]; then
    log "App not running — launching its main activity..."
    "$ADB" -s "$SERIAL" shell monkey -p "$PACKAGE" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1 || true
    # Give it a moment to come up.
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        PID="$("$ADB" -s "$SERIAL" shell pidof "$PACKAGE" 2>/dev/null | tr -d '\r' | awk '{print $1}')"
        [ -n "${PID:-}" ] && break
        "$ADB" -s "$SERIAL" wait-for-device >/dev/null 2>&1 || true
        sleep_done=0
    done
    [ -n "${PID:-}" ] || die "Could not start '$PACKAGE'. Launch it manually and retry."
fi
ok "App $PACKAGE is running (pid $PID)."

# --------------------------------------------------- drive the host
# Use the documented public API of inspector_widget:
#   attach(serial, package) -> Session ; Session.dump_tree(...) -> DumpTreeResponse
# (the same surface host/mcp_server.py consumes). We run it inline so the smoke
# test does not depend on a specific argparse CLI shape.
log "Attaching agent and dumping the View tree via inspector_widget..."

INSPECTOR_WIDGET_ARTIFACTS="$OUT_DIR" \
"$PYTHON" - "$SERIAL" "$PACKAGE" "$HOST_DIR" <<'PYEOF'
import os, sys, json

serial, package, host_dir = sys.argv[1], sys.argv[2], sys.argv[3]
if host_dir not in sys.path:
    sys.path.insert(0, host_dir)

try:
    import inspector_widget as vh
except Exception as exc:
    sys.stderr.write(
        "Could not import inspector_widget from %s: %r\n"
        "The host module must be built/present (it is a separate module).\n"
        % (host_dir, exc)
    )
    raise SystemExit(3)

def _strings(strings_msg):
    table = {0: ""}
    if strings_msg is not None:
        for e in getattr(strings_msg, "entries", []):
            table[e.id] = e.str
    return table

def _count(node):
    return 1 + sum(_count(c) for c in node.children)

session = vh.attach(serial, package)
try:
    print(f"attached: api={getattr(session,'api_level',None)} "
          f"abi={getattr(session,'abi',None)} "
          f"agent={getattr(session,'agent_version',None)}")
    resp = session.dump_tree(
        root_id=0,
        include_properties=False,
        include_resolution_stack=False,
        include_screenshot=False,
        screenshot_scale=1.0,
    )
    table = _strings(resp.strings if resp.HasField("strings") else None)
    roots = list(resp.roots)
    total = sum(_count(r) for r in roots)
    print(f"roots: {len(roots)}   total views: {total}")
    for r in roots:
        cls = table.get(r.class_name, "?")
        b = r.bounds.layout if r.HasField("bounds") else None
        dims = f"{b.w}x{b.h}@({b.x},{b.y})" if b is not None else "?"
        print(f"  root id={r.id} {cls} {dims} children={len(r.children)}")
    # Print a shallow JSON preview of the first root for eyeballing.
    if roots:
        r0 = roots[0]
        preview = {
            "id": r0.id,
            "class": table.get(r0.class_name, None),
            "children": len(r0.children),
            "first_children": [
                table.get(c.class_name, None) for c in list(r0.children)[:8]
            ],
        }
        print("preview: " + json.dumps(preview))
    print("SMOKE_OK")
finally:
    for name in ("detach", "shutdown", "close"):
        fn = getattr(session, name, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass
            break
PYEOF

rc=$?
[ "$rc" -eq 0 ] || die "Host smoke test failed (exit $rc)."
ok "Smoke test passed — agent injected and tree dumped."
