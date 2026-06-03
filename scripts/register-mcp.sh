#!/usr/bin/env bash
#
# Register the Inspector Widget MCP server with the `claude` CLI using THIS
# checkout's absolute paths — so nobody has to hand-edit a path. Idempotent:
# re-run any time (e.g. after moving the repo) to refresh the registration.
#
# Usage:
#   ./scripts/register-mcp.sh [server-name]      # default name: inspector-widget
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="$ROOT/host"
NAME="${1:-inspector-widget}"

# Prefer the project venv's python (has the deps); fall back to python3 on PATH.
PY="$HOST/.venv/bin/python"
if [ ! -x "$PY" ]; then
    PY="$(command -v python3 || true)"
    echo "note: $HOST/.venv not found; using '$PY'. Create the venv for the full deps:" >&2
    echo "      python3 -m venv $HOST/.venv && $HOST/.venv/bin/pip install -r $HOST/requirements.txt" >&2
fi

[ -n "$PY" ]                  || { echo "error: no python3 found on PATH." >&2; exit 1; }
[ -f "$HOST/mcp_server.py" ]  || { echo "error: $HOST/mcp_server.py not found." >&2; exit 1; }
command -v claude >/dev/null  || { echo "error: the 'claude' CLI is not on PATH." >&2; exit 1; }

# Replace any prior registration of this name, then add fresh.
claude mcp remove "$NAME" >/dev/null 2>&1 || true
claude mcp add "$NAME" -- env "PYTHONPATH=$HOST" "$PY" "$HOST/mcp_server.py"

echo "Registered MCP server '$NAME':"
echo "    python : $PY"
echo "    server : $HOST/mcp_server.py"
echo "Verify:  claude mcp list      (expect: $NAME ... ✓ Connected)"
echo "Sanity:  $PY $HOST/mcp_server.py --self-check"
