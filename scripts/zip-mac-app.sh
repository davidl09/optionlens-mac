#!/usr/bin/env bash
# Packs the built OptionLens.app (electron-builder `dir` output) into release/OptionLens-<version>-<arch>.zip
# with ditto, which keeps symlinks, permissions and the code signature intact (the release artifact).
# Usage: scripts/zip-mac-app.sh <arm64|x64>
set -euo pipefail
ARCH="$1"
cd "$(dirname "$0")/.."
case "$ARCH" in arm64|x64) ;; *) echo "arch must be arm64 or x64, not: $ARCH" >&2; exit 1 ;; esac
VERSION="$(node -p 'require("./package.json").version')"
APPS=(release/mac*/OptionLens.app)
[ "${#APPS[@]}" -eq 1 ] && [ -d "${APPS[0]}" ] || { echo "expected exactly one release/mac*/OptionLens.app" >&2; exit 1; }
ZIP="release/OptionLens-$VERSION-$ARCH.zip"
rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent "${APPS[0]}" "$ZIP"
echo "$ZIP"
