#!/usr/bin/env bash
# Regenerate the Python protobuf bindings for Inspector Widget.
#
# Produces host/inspector_widget/proto/view_inspection_pb2.py from the FINAL
# protocol at proto/view_inspection.proto. Requires `protoc` on PATH
# (CONTRACT.md verified protoc 34.1) and the matching `protobuf` runtime for
# import (pip install -r host/requirements.txt).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

PROTO_DIR="$REPO_ROOT/proto"
PROTO_FILE="$PROTO_DIR/view_inspection.proto"
OUT_DIR="$HERE/inspector_widget/proto"

if ! command -v protoc >/dev/null 2>&1; then
  echo "error: protoc not found on PATH" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

# The generated module imports the bare name `view_inspection_pb2`; placing it
# in the proto package (which is on sys.path via the package) resolves it.
protoc \
  --proto_path="$PROTO_DIR" \
  --python_out="$OUT_DIR" \
  "$PROTO_FILE"

# Ensure the package marker exists.
if [ ! -f "$OUT_DIR/__init__.py" ]; then
  cat > "$OUT_DIR/__init__.py" <<'EOF'
"""Generated protocol buffer bindings for Inspector Widget."""
EOF
fi

echo "generated $OUT_DIR/view_inspection_pb2.py"
