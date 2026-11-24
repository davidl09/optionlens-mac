#!/usr/bin/env bash
# Builds the engine payload that electron-builder copies into the app's resources/python (macOS:
# OptionLens.app/Contents/Resources/python): a relocatable CPython 3.13 (python-build-standalone via uv)
# with the engine's locked dependencies and the engine itself (package `optionlens`, with its metadata so
# /api/health reports its version). Runs on macOS, Linux and Windows (Git Bash on the GitHub runner), on the
# target OS and architecture (wheels are platform-specific).
# - macOS: wheels for the app's minimum macOS (electron-builder.yml minimumSystemVersion), not the build
#   machine's: a macOS 15 runner would otherwise pick numpy/scipy wheels that need macOS 14.
# - Linux: manylinux_2_28 wheels (glibc 2.28), not the build machine's newer glibc, so the AppImage's engine
#   runs on older distributions (CI checks every bundled ELF file: scripts/check-linux-glibc.sh).
# - Windows: python.exe sits at the top of the bundle and the standard library in Lib\ (not bin/, lib/).
set -euo pipefail
cd "$(dirname "$0")/.."

PY_VERSION="${PY_VERSION:-3.13}"
LINUX_PLATFORM="x86_64-manylinux_2_28"
PLATFORM_ARGS=()
case "$(uname -s)" in
  Darwin) OS=mac ;;
  Linux) OS=linux ;;
  MINGW* | MSYS* | CYGWIN*) OS=windows ;;
  *) echo "unsupported build OS: $(uname -s)" >&2; exit 1 ;;
esac
case "$OS-$(uname -m)" in
  mac-arm64) PLATFORM_ARGS=(--python-platform aarch64-apple-darwin) ;;
  mac-x86_64) PLATFORM_ARGS=(--python-platform x86_64-apple-darwin) ;;
  linux-x86_64) PLATFORM_ARGS=(--python-platform "$LINUX_PLATFORM") ;;
  windows-*) ;;
  *) echo "unsupported build machine: $OS $(uname -m)" >&2; exit 1 ;;
esac
if [ "$OS" = mac ]; then
  MIN_MACOS="$(sed -n 's/^ *minimumSystemVersion: *"\{0,1\}\([0-9.]*\)"\{0,1\}.*/\1/p' electron-builder.yml)"
  [ -n "$MIN_MACOS" ] || { echo "no minimumSystemVersion in electron-builder.yml" >&2; exit 1; }
  export MACOSX_DEPLOYMENT_TARGET="$MIN_MACOS"   # uv: the macOS version --python-platform targets
fi
rm -rf build/python build/pyinst build/requirements.txt
mkdir -p build

# 1. Relocatable CPython (uv installs python-build-standalone builds).
uv python install "$PY_VERSION" --install-dir build/pyinst
src="$(find build/pyinst -maxdepth 1 -mindepth 1 -type d -name "cpython-${PY_VERSION}*" | head -n 1)"
[ -n "$src" ] || { echo "no CPython ${PY_VERSION} under build/pyinst" >&2; exit 1; }
mv "$src" build/python
rm -rf build/pyinst
# Where python-build-standalone keeps things, and what is not needed at runtime (Tk, IDLE, headers, docs).
if [ "$OS" = windows ]; then
  PY=build/python/python.exe
  LIB=build/python/Lib
  STRIP=(build/python/include build/python/libs build/python/tcl build/python/Scripts "$LIB"/{idlelib,tkinter,turtledemo,ensurepip,lib2to3}
    build/python/DLLs/_tkinter.pyd build/python/DLLs/tcl*.dll build/python/DLLs/tk*.dll)
else
  PY=build/python/bin/python3
  LIB="$(echo build/python/lib/python3.*)"
  STRIP=(build/python/include build/python/share build/python/lib/itcl* build/python/lib/tcl* build/python/lib/tk*
    build/python/lib/libtcl* build/python/lib/libtk* "$LIB"/{idlelib,tkinter,turtledemo,ensurepip,lib2to3} "$LIB"/lib-dynload/_tkinter*)
fi
[ -f "$PY" ] || { echo "no interpreter at $PY" >&2; exit 1; }
# This copy is private to the app: let uv install into it.
find build/python -maxdepth 3 -name EXTERNALLY-MANAGED -delete

# 2. The engine's locked runtime dependencies (no dev deps, not the project itself).
(cd engine && uv export --frozen --no-dev --no-hashes --no-emit-project --format requirements-txt -o ../build/requirements.txt)
uv pip install --python "$PY" ${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"} --no-cache -r build/requirements.txt

rm -rf "${STRIP[@]}" "$LIB"/site-packages/pip "$LIB"/site-packages/pip-*.dist-info

# 3. The engine itself (non-editable), then byte-compile everything (the installed app is read-only:
#    signed on macOS, a read-only squashfs in the AppImage).
uv pip install --python "$PY" --no-deps --no-cache ./engine
"$PY" -m compileall -q "$LIB" >/dev/null

# 4. Smoke: the bundled interpreter imports the engine, its CLI and MCP server (and, on macOS, nothing needs a newer
#    macOS than the app's minimum; on Linux, nothing needs a newer glibc than the baseline).
PYTHONNOUSERSITE=1 "$PY" -c "import importlib.metadata as m, optionlens.cli, optionlens.mcp.server, duckdb, fastapi, numpy, scipy.stats, pyarrow; print('engine bundle ok', m.version('optionlens'))"
if [ "$OS" = mac ]; then "$PY" -I scripts/check-macos-minos.py build/python; fi
if [ "$OS" = linux ]; then scripts/check-linux-glibc.sh build/python; fi
du -sh build/python
