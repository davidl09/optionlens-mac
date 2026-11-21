#!/usr/bin/env bash
# Extracts a release zip the way the installer and the in-app updater do (ditto -x -k into an empty folder),
# checks that it holds exactly OptionLens.app, and runs scripts/verify-mac-app.sh on the extracted app.
# Usage: scripts/verify-mac-zip.sh <OptionLens-<version>-<arch>.zip>
set -euo pipefail
ZIP="$1"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT
ditto -x -k "$ZIP" "$OUT"
ENTRIES="$(ls -A "$OUT")"
[ "$ENTRIES" = "OptionLens.app" ] || { echo "the zip must contain only OptionLens.app, found: $ENTRIES" >&2; exit 1; }
"$ROOT/scripts/verify-mac-app.sh" "$OUT/OptionLens.app"
