#!/usr/bin/env bash
# Checks a built OptionLens.app as users get it: signature (ad-hoc, deep and strict), the bundled Python's
# signature, the minimum macOS of every bundled Mach-O, and that the bundled engine starts from inside it.
# Usage: scripts/verify-mac-app.sh <path to OptionLens.app>
set -euo pipefail
APP="$1"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[ -d "$APP/Contents" ] || { echo "not an app bundle: $APP" >&2; exit 1; }
codesign --verify --deep --strict --verbose=2 "$APP"
PY="$APP/Contents/Resources/python/bin/python3"
codesign --verify --strict "$PY"
"$PY" -I "$ROOT/scripts/check-macos-minos.py" "$APP/Contents/Resources/python"
"$ROOT/scripts/smoke-bundled-engine.sh" "$PY"
