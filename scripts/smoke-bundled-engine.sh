#!/usr/bin/env bash
# Smoke test of the engine bundled in a packaged app (scripts/smoke-bundled-engine.py, run by that bundled
# Python itself): LISTENING + /api/health, then a clean stop by closing its stdin. macOS, Linux and Windows
# (Git Bash). No bytecode is written (-B): a .pyc inside a signed macOS app would break its seal.
# Usage: scripts/smoke-bundled-engine.sh <path to the bundled python3 / python.exe> [timeout seconds]
set -euo pipefail
PY="$1"
[ -f "$PY" ] || { echo "not a file: $PY" >&2; exit 1; }
exec "$PY" -I -B "$(dirname "$0")/smoke-bundled-engine.py" "${2:-90}"
