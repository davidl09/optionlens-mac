#!/usr/bin/env bash
# Builds the engine payload that electron-builder copies into OptionLens.app/Contents/Resources/python:
# a relocatable CPython 3.13 (python-build-standalone via uv) with the engine's locked dependencies and
# the engine itself (package `optionlens`, with its metadata so /api/health reports its version).
# Run on the target architecture (wheels are platform-specific): CI runs it on arm64 and x64 macOS runners.
set -euo pipefail
cd "$(dirname "$0")/.."

PY_VERSION="${PY_VERSION:-3.13}"
rm -rf build/python build/pyinst build/requirements.txt
mkdir -p build

# 1. Relocatable CPython (uv installs python-build-standalone builds).
uv python install "$PY_VERSION" --install-dir build/pyinst
src="$(find build/pyinst -maxdepth 1 -mindepth 1 -type d -name "cpython-${PY_VERSION}*" | head -n 1)"
[ -n "$src" ] || { echo "no CPython ${PY_VERSION} under build/pyinst" >&2; exit 1; }
mv "$src" build/python
rm -rf build/pyinst
# This copy is private to the app bundle: let uv install into it.
find build/python/lib -maxdepth 2 -name EXTERNALLY-MANAGED -delete
PY=build/python/bin/python3

# 2. The engine's locked runtime dependencies (no dev deps, not the project itself).
(cd engine && uv export --frozen --no-dev --no-hashes --no-emit-project --format requirements-txt -o ../build/requirements.txt)
uv pip install --python "$PY" --no-cache -r build/requirements.txt

# Not needed at runtime: Tk, IDLE, pip/ensurepip, headers, docs.
rm -rf build/python/include build/python/share build/python/lib/itcl* build/python/lib/tcl* build/python/lib/tk* \
  build/python/lib/libtcl* build/python/lib/libtk* build/python/lib/python3.*/{idlelib,tkinter,turtledemo,ensurepip,lib2to3} \
  build/python/lib/python3.*/site-packages/pip build/python/lib/python3.*/site-packages/pip-*.dist-info \
  build/python/lib/python3.*/lib-dynload/_tkinter*

# 3. The engine itself (non-editable), then byte-compile everything (the signed bundle is read-only).
uv pip install --python "$PY" --no-deps --no-cache ./engine
"$PY" -m compileall -q build/python/lib >/dev/null

# 4. Smoke: the bundled interpreter imports the engine and its CLI.
PYTHONNOUSERSITE=1 "$PY" -c "import importlib.metadata as m, optionlens.cli, duckdb, fastapi; print('engine bundle ok', m.version('optionlens'))"
du -sh build/python
