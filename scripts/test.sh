#!/usr/bin/env bash
# Run the Inspector Widget (viewspector) host test suite — device-free by default.
#
# Uses the host virtualenv's pytest so the in-tree inspector_widget package and its
# pinned protobuf runtime are what get exercised. Pass extra args through, e.g.:
#   scripts/test.sh -k framing
#   scripts/test.sh -m device        # opt-in emulator smoke test
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST_DIR="$REPO_ROOT/host"
VENV_PYTEST="$HOST_DIR/.venv/bin/pytest"

if [[ ! -x "$VENV_PYTEST" ]]; then
    echo "error: $VENV_PYTEST not found." >&2
    echo "Create the host venv and install dev deps first, e.g.:" >&2
    echo "  python3 -m venv $HOST_DIR/.venv" >&2
    echo "  $HOST_DIR/.venv/bin/pip install -r $HOST_DIR/requirements.txt pytest" >&2
    exit 1
fi

# Default to the device-free selection; allow callers to override the marker
# expression (or anything else) by passing their own args.
if [[ $# -eq 0 ]]; then
    exec "$VENV_PYTEST" "$HOST_DIR/tests" -q -m 'not device'
else
    exec "$VENV_PYTEST" "$HOST_DIR/tests" -q "$@"
fi
