#!/usr/bin/env bash
# Checks a built OptionLens.app as users get it: signature (ad-hoc, deep and strict), the bundled Python's
# signature, the minimum macOS of every bundled Mach-O, that the bundled engine starts from inside it and that the MCP
# launcher runs.
# Python runs without writing bytecode (as in the app): a .pyc written into the bundle breaks its seal,
# which the final codesign check would catch.
# Usage: scripts/verify-mac-app.sh <path to OptionLens.app>
set -euo pipefail
APP="$1"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[ -d "$APP/Contents" ] || { echo "not an app bundle: $APP" >&2; exit 1; }
codesign --verify --deep --strict --verbose=2 "$APP"
PY="$APP/Contents/Resources/python/bin/python3"
codesign --verify --strict "$PY"
PYTHONDONTWRITEBYTECODE=1 "$PY" -I -B "$ROOT/scripts/check-macos-minos.py" "$APP/Contents/Resources/python"
"$ROOT/scripts/smoke-bundled-engine.sh" "$PY"
# The MCP launcher for AI agents (docs/INTEGRATION.md §10): present, executable, and its server builds its tools.
MCP="$APP/Contents/Resources/mcp/optionlens-mcp"
[ -x "$MCP" ] || { echo "no executable MCP launcher at $MCP" >&2; exit 1; }
"$MCP" --check
codesign --verify --deep --strict "$APP"
echo "signature still valid after running the bundled engine"
