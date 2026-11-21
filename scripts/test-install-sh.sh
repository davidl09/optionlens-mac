#!/usr/bin/env bash
# Tests install.sh's Linux logic by sourcing it (functions only): release JSON parsing (pretty and minified,
# draft refusal, other OSes' assets ignored), desktop-entry quoting, and, given a built AppImage, a whole
# install_linux run against it into temp folders (GitHub API and download replaced by local files).
# Usage: scripts/test-install-sh.sh [path to OptionLens-<version>-linux-x64.AppImage]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091  # install.sh is linted on its own
source "$ROOT/install.sh"
APPIMAGE="${1:-}"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
fails=0
check() { if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1: expected [$3], got [$2]"; fails=$((fails + 1)); fi; }

D="sha256:$(printf 'a%.0s' $(seq 64))"
release() {   # release <version> <draft> → a GitHub-like release JSON with every OS's assets
  local v="$1" a assets=""
  for a in "$v-arm64.zip" "$v-x64.zip" "$v-win-x64.zip" "$v-win-x64-setup.exe" "$v-linux-x64.AppImage"; do
    assets="$assets${assets:+,}
    {\"url\": \"https://api.github.com/x\", \"name\": \"OptionLens-$a\", \"uploader\": {\"login\": \"bot\", \"id\": 1},
     \"size\": 10, \"digest\": \"${D%a}$( [ "$a" = "$v-linux-x64.AppImage" ] && echo b || echo a)\",
     \"browser_download_url\": \"https://github.com/$REPO/releases/download/v$v/OptionLens-$a\"}"
  done
  printf '{"tag_name": "v%s", "name": "v%s", "draft": %s, "prerelease": false,\n "body": "Fixes, \\"name\\": \\"x\\", [1,2] {\\"digest\\": 1}",\n "assets": [%s]}\n' "$v" "$v" "$2" "$assets"
}
release 0.1.4 false >"$T/pretty.json"
tr -d '\n' <"$T/pretty.json" >"$T/minified.json"
for f in pretty minified; do
  out="$(release_fields "$T/$f.json" -linux-x64.AppImage | tr '\n' ' ')"
  check "release_fields ($f)" "$out" "0.1.4 OptionLens-0.1.4-linux-x64.AppImage https://github.com/$REPO/releases/download/v0.1.4/OptionLens-0.1.4-linux-x64.AppImage ${D%a}b "
done
release 0.1.4 true >"$T/draft.json"
if release_fields "$T/draft.json" -linux-x64.AppImage >/dev/null; then check "draft refused" no yes; else check "draft refused" yes yes; fi
printf '{"tag_name": "v0.1.3", "draft": false, "assets": [{"name": "OptionLens-0.1.3-arm64.zip", "digest": "%s", "browser_download_url": "u"}]}' "$D" >"$T/maconly.json"
check "a macOS-only release has no Linux URL" "$(release_fields "$T/maconly.json" -linux-x64.AppImage | sed -n 3p)" ""

check "exec arg plain" "$(desktop_exec_arg /home/me/.local/bin/OptionLens.AppImage)" "/home/me/.local/bin/OptionLens.AppImage"
check "exec arg spaces" "$(desktop_exec_arg "/home/me/My Apps/OptionLens.AppImage")" '"/home/me/My Apps/OptionLens.AppImage"'
# shellcheck disable=SC2016
check "exec arg specials" "$(desktop_exec_arg '/a "b" $c `d` \e 100%')" '"/a \\"b\\" \\$c \\`d\\` \\\\e 100%%"'
check "desktop entry Exec" "$(desktop_entry "/h/My Apps/OptionLens.AppImage" /h/icon.png | grep '^Exec=')" 'Exec="/h/My Apps/OptionLens.AppImage" %U'

if [ -n "$APPIMAGE" ]; then
  VERSION="$(node -p 'require(process.argv[1]).version' "$ROOT/package.json")"
  if is_appimage "$APPIMAGE"; then check "is_appimage (built)" yes yes; else check "is_appimage (built)" no yes; fi
  printf '#!/bin/sh\n' >"$T/sh"
  if is_appimage "$T/sh"; then check "is_appimage (script)" yes no; else check "is_appimage (script)" no no; fi
  DIGEST="sha256:$(sha256sum "$APPIMAGE" | awk '{print $1}')"
  release "$VERSION" false | sed "s/\"${D%a}b\"/\"$DIGEST\"/" >"$T/release.json"
  fetch_release() { cp "$T/release.json" "$1"; }
  curl() { cp "$APPIMAGE" "${*: -2:1}"; }   # download_verified's curl: … -o <file> <url>
  mkdir -p "$T/home/My Apps" "$T/data"
  (HOME="$T/home" OPTIONLENS_BIN_DIR="$T/home/My Apps" XDG_DATA_HOME="$T/data" OPTIONLENS_NO_LAUNCH=1 install_linux)
  check "installed AppImage is the download" "$(sha256sum <"$T/home/My Apps/OptionLens.AppImage")" "$(sha256sum <"$APPIMAGE")"
  check "installed AppImage is executable" "$(stat -c %a "$T/home/My Apps/OptionLens.AppImage")" 755
  check "no leftover download" "$(find "$T/home/My Apps" -name '.OptionLens*' | wc -l)" 0
  check "launcher Exec" "$(grep '^Exec=' "$T/data/applications/optionlens.desktop")" "Exec=\"$T/home/My Apps/OptionLens.AppImage\" %U"
  check "launcher icon" "$(test -s "$T/data/icons/optionlens.png" && echo yes)" yes
  release "$VERSION" false >"$T/release.json"   # wrong digest: nothing is replaced
  cp "$T/home/My Apps/OptionLens.AppImage" "$T/before"
  if (HOME="$T/home" OPTIONLENS_BIN_DIR="$T/home/My Apps" XDG_DATA_HOME="$T/data" OPTIONLENS_NO_LAUNCH=1 install_linux) 2>"$T/err"; then
    check "digest mismatch refused" no yes
  else
    check "digest mismatch refused" "$(grep -c 'does not match the release' "$T/err")" 1
  fi
  check "no leftover download after a failed install" "$(find "$T/home/My Apps" -name '.OptionLens*' | wc -l)" 0
  check "installed AppImage untouched after a failed install" "$(cmp -s "$T/before" "$T/home/My Apps/OptionLens.AppImage" && echo same)" same
fi
[ "$fails" -eq 0 ] || { echo "$fails check(s) failed" >&2; exit 1; }
echo "install.sh: all checks passed"
