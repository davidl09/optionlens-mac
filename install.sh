#!/usr/bin/env bash
# OptionLens installer for macOS and Linux: installs (or updates to) the latest release.
#
#   curl -fsSL https://raw.githubusercontent.com/davidl09/optionlens-mac/main/install.sh | bash
#
# Both: find the latest release (GitHub API), download this system's asset over HTTPS and check its SHA-256
# against the digest GitHub publishes for it (no digest, no install). It never uses sudo.
# - macOS: the zip for this Mac's architecture is extracted with ditto; the app's bundle id, version and
#   signature are checked; a running OptionLens is quit; the installed app is kept as a backup
#   (~/Library/Application Support/OptionLens/backups, the newest 2 are kept); the new app is moved into
#   /Applications and opened. Files downloaded with curl are not quarantined, so the app (ad-hoc signed, not
#   notarized) opens without a Gatekeeper prompt.
# - Linux (x86_64): OptionLens-<version>-linux-x64.AppImage is checked (AppImage, name and version), made
#   executable and moved to ~/.local/bin/OptionLens.AppImage (OPTIONLENS_BIN_DIR overrides the folder) in one
#   rename; a launcher entry (~/.local/share/applications/optionlens.desktop) and its icon are written, and
#   OptionLens is started (unless it is already running: then quit it from the tray and open it again).
# The whole script is functions until the last line, so a truncated download runs nothing.
set -euo pipefail

REPO="davidl09/optionlens-mac"
BUNDLE_ID="app.optionlens.mac"
KEEP_BACKUPS=2

say() { printf '==> %s\n' "$*"; }
die() { printf 'OptionLens install failed: %s\n' "$*" >&2; exit 1; }

# fetch_release <file>: the latest release's JSON from the GitHub API.
fetch_release() {
  curl -fsSL --proto '=https' --tlsv1.2 -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$REPO/releases/latest" -o "$1" \
    || die "could not reach the GitHub API (offline, or rate limited: try again in an hour)."
}

# check_release <version> <asset> <url> <digest>: what the installer acts on has the expected shape.
check_release() {
  [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "unexpected release version: $1"
  [[ "$3" == "https://github.com/$REPO/releases/download/v$1/$2" ]] || die "unexpected download URL: $3"
  [[ "$4" =~ ^sha256:[0-9a-f]{64}$ ]] || die "the release does not publish a SHA-256 digest for $2; refusing to install it."
}

# download_verified <url> <file> <digest> <sha256 command…>
download_verified() {
  local url="$1" file="$2" digest="$3" actual
  shift 3
  curl -fL --proto '=https' --proto-redir '=https' --tlsv1.2 --progress-bar -o "$file" "$url" || die "the download failed."
  actual="sha256:$("$@" "$file" | awk '{print $1}')"
  [ "$actual" = "$digest" ] || die "the download's SHA-256 ($actual) does not match the release ($digest)."
  say "Checksum verified"
}

# ---------------------------------------------------------------------------------------------- Linux

# release_fields <release.json> <asset name suffix>: prints the version, the asset's name, URL and digest,
# one per line, from the GitHub release JSON (no jq or Python needed: the JSON is split at , { } [ so each
# "key": value pair starts a line; escaped quotes inside the notes never start a line with a bare quote).
release_fields() {
  tr ',{}[]' '\n\n\n\n\n' <"$1" | awk -v suffix="$2" '
    function val(line) { sub(/^[^:]*:[[:space:]]*/, "", line); gsub(/^"|"[[:space:]]*$/, "", line); return line }
    /^[[:space:]]*"draft":[[:space:]]*true/ || /^[[:space:]]*"prerelease":[[:space:]]*true/ { bad = 1 }
    /^[[:space:]]*"tag_name":/ && tag == "" { tag = val($0); sub(/^v/, "", tag) }
    /^[[:space:]]*"name":/ { name = val($0) }
    /^[[:space:]]*"digest":/ && name == want { digest = val($0) }
    /^[[:space:]]*"browser_download_url":/ && name == want { url = val($0) }
    { if (tag != "" && want == "") want = "OptionLens-" tag suffix }
    END { if (bad) exit 3; print tag; print want; print url; print digest }'
}

# is_appimage <file>: ELF magic, then "AI" 0x02 at offset 8 (AppImage type 2).
is_appimage() {
  [[ "$(head -c 11 "$1" | od -An -tx1 | tr -d ' \n')" == 7f454c46????????414902 ]]
}

# desktop_exec_arg <path>: one argument of a desktop entry's Exec key (quoted and escaped as the Desktop
# Entry Specification says; the same rule as electron/main/platform/linux.ts).
desktop_exec_arg() {
  local a="$1"
  case "$a" in *[[:space:]\"\'\\\>\<~\|\&\;\$\*\?\#\(\)\`]*)
    a="${a//\\/\\\\}"; a="${a//\"/\\\"}"; a="${a//\`/\\\`}"; a="${a//\$/\\\$}"; a="\"$a\"" ;;
  esac
  a="${a//\\/\\\\}"
  printf '%s' "${a//%/%%}"
}

# desktop_entry <AppImage path> <icon path>: the launcher entry.
desktop_entry() {
  cat <<EOF
[Desktop Entry]
Type=Application
Name=OptionLens
Comment=Options and stock analytics with an IBKR bridge
Exec=$(desktop_exec_arg "$1") %U
Icon=$2
Terminal=false
Categories=Finance;
StartupWMClass=optionlens
EOF
}

install_linux() {
  [ "$(uname -m)" = x86_64 ] || die "OptionLens for Linux is built for x86_64 only (this is $(uname -m))."
  local bin_dir="${OPTIONLENS_BIN_DIR:-$HOME/.local/bin}"
  local dest="$bin_dir/OptionLens.AppImage"
  local apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
  local icon="${XDG_DATA_HOME:-$HOME/.local/share}/icons/optionlens.png"
  local tmp info version asset url digest new
  mkdir -p "$bin_dir" "$apps" "$(dirname "$icon")"
  [ -w "$bin_dir" ] || die "$bin_dir is not writable for $(id -un)."
  command -v sha256sum >/dev/null || die "sha256sum is missing (coreutils)."
  tmp="$(mktemp -d)"
  # shellcheck disable=SC2064  # expand now: $tmp is local
  trap "rm -rf '$tmp'" EXIT

  say "Looking up the latest OptionLens release"
  fetch_release "$tmp/release.json"
  info="$(release_fields "$tmp/release.json" "-linux-x64.AppImage")" || die "the latest release is a draft or a prerelease."
  { read -r version; read -r asset; read -r url; read -r digest || digest=""; } <<<"$info"
  [ -n "$url" ] || die "the latest release ($version) has no $asset."
  check_release "$version" "$asset" "$url" "$digest"

  # Downloaded next to the destination, so the final move is one rename (atomic, even over a running AppImage).
  new="$(mktemp "$bin_dir/.OptionLens.AppImage.XXXXXX")"
  # shellcheck disable=SC2064  # expand now: both are local
  trap "rm -rf '$tmp'; rm -f '$new'" EXIT
  say "Downloading OptionLens $version (Linux x64)"
  download_verified "$url" "$new" "$digest" sha256sum
  chmod 755 "$new"
  is_appimage "$new" || die "$asset is not an AppImage."
  (cd "$tmp" && "$new" --appimage-extract '*.desktop' >/dev/null && "$new" --appimage-extract 'usr/share/icons/hicolor/*/apps/optionlens.png' >/dev/null) \
    || die "could not read $asset (is it an AppImage for this system?)."
  grep -qx 'Name=OptionLens' "$tmp"/squashfs-root/*.desktop && grep -qx "X-AppImage-Version=$version" "$tmp"/squashfs-root/*.desktop \
    || die "$asset is not OptionLens $version."
  say "AppImage verified"

  mv -f "$new" "$dest"
  local png
  png="$(find "$tmp/squashfs-root/usr/share/icons" -name optionlens.png -type f | sort -V | tail -n 1)"
  [ -z "$png" ] || cp -f "$png" "$icon"
  desktop_entry "$dest" "$icon" >"$apps/optionlens.desktop"
  command -v update-desktop-database >/dev/null && update-desktop-database "$apps" >/dev/null 2>&1 || true
  say "Installed OptionLens $version as $dest (launcher: $apps/optionlens.desktop)"
  if ! command -v fusermount3 >/dev/null && ! command -v fusermount >/dev/null; then
    say "Note: FUSE is not installed; AppImages need it (Debian/Ubuntu: sudo apt install fuse3)."
  fi
  if pgrep -u "$(id -u)" -x optionlens >/dev/null 2>&1; then
    say "OptionLens is running: quit it (tray menu → Quit OptionLens) and open it again to use $version."
  elif [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && [ -z "${OPTIONLENS_NO_LAUNCH:-}" ]; then
    nohup "$dest" >/dev/null 2>&1 &
  fi
  say "Done. OptionLens checks for updates itself from now on (Settings → Updates)."
}

# ---------------------------------------------------------------------------------------------- macOS

install_mac() {
  local apps_dir="/Applications"
  local dest="$apps_dir/OptionLens.app"
  local backups="$HOME/Library/Application Support/OptionLens/backups"
  local arch tmp info version asset url digest new old backup
  plist_value() { /usr/libexec/PlistBuddy -c "Print :$2" "$1/Contents/Info.plist" 2>/dev/null; }
  case "$(uname -m)" in
    arm64) arch=arm64 ;;
    x86_64) arch=x64 ;;
    *) die "unsupported CPU architecture: $(uname -m)" ;;
  esac
  [ -w "$apps_dir" ] || die "$apps_dir is not writable for $(id -un). Sign in with an administrator account, or ask an administrator to install OptionLens."

  tmp="$(mktemp -d -t optionlens-install)"
  # shellcheck disable=SC2064  # expand now: $tmp is local
  trap "rm -rf '$tmp'" EXIT

  say "Looking up the latest OptionLens release"
  fetch_release "$tmp/release.json"
  # The release JSON is parsed with JavaScript for Automation (built into macOS): version, asset URL, digest.
  info="$(osascript -l JavaScript - "$tmp/release.json" "$arch" <<'JXA'
ObjC.import("Foundation");
function run(argv) {
  const text = $.NSString.stringWithContentsOfFileEncodingError(argv[0], $.NSUTF8StringEncoding, null).js;
  const r = JSON.parse(text);
  if (r.draft || r.prerelease) throw new Error("the latest release is a draft or a prerelease");
  const version = String(r.tag_name || "").replace(/^v/, "");
  const name = "OptionLens-" + version + "-" + argv[1] + ".zip";
  const asset = (r.assets || []).find(function (a) { return a.name === name; });
  if (!asset) throw new Error("the latest release has no " + name);
  return [version, name, asset.browser_download_url, asset.digest || ""].join("\n");
}
JXA
)" || die "could not read the release information."
  { read -r version; read -r asset; read -r url; read -r digest || digest=""; } <<<"$info"
  check_release "$version" "$asset" "$url" "$digest"

  say "Downloading OptionLens $version ($arch)"
  download_verified "$url" "$tmp/$asset" "$digest" shasum -a 256

  mkdir "$tmp/stage"
  ditto -x -k "$tmp/$asset" "$tmp/stage" || die "could not extract $asset."
  new="$tmp/stage/OptionLens.app"
  [ "$(ls -A "$tmp/stage")" = "OptionLens.app" ] && [ -d "$new" ] && [ ! -L "$new" ] \
    || die "$asset does not contain exactly OptionLens.app."
  [ "$(plist_value "$new" CFBundleIdentifier)" = "$BUNDLE_ID" ] || die "the downloaded app has an unexpected bundle id."
  [ "$(plist_value "$new" CFBundleShortVersionString)" = "$version" ] || die "the downloaded app is not version $version."
  codesign --verify --deep --strict "$new" || die "the downloaded app's signature is invalid."
  say "Signature verified"

  if pgrep -x OptionLens >/dev/null; then
    say "Quitting the running OptionLens"
    osascript -e "tell application id \"$BUNDLE_ID\" to quit" >/dev/null 2>&1 || true
    for _ in $(seq 1 40); do
      pgrep -x OptionLens >/dev/null || break
      sleep 0.5
    done
    ! pgrep -x OptionLens >/dev/null || die "OptionLens did not quit within 20 s. Quit it from the menu bar (Quit OptionLens) and run the installer again."
  fi

  if [ -e "$dest" ]; then
    old="$(plist_value "$dest" CFBundleShortVersionString || true)"
    [[ "$old" =~ ^[0-9A-Za-z.+-]+$ ]] || old="unknown"
    mkdir -p "$backups"
    backup="$backups/OptionLens-$old.app"
    rm -rf "$backup"
    mv "$dest" "$backup" || die "could not move the installed OptionLens out of $apps_dir."
    touch "$backup"
    say "Kept OptionLens $old as a backup in $backups"
    shopt -s nullglob
    local all=("$backups"/OptionLens-*.app)
    if [ "${#all[@]}" -gt "$KEEP_BACKUPS" ]; then
      # Newest first by modification time (each backup is touched when it is made); drop the rest. The
      # names are OptionLens-<version>.app, so parsing ls output is safe here.
      # shellcheck disable=SC2012
      ls -1dt "${all[@]}" | tail -n +"$((KEEP_BACKUPS + 1))" | while IFS= read -r stale; do rm -rf "$stale"; done
    fi
    if ! mv "$new" "$dest"; then
      mv "$backup" "$dest" || true
      die "could not move the new app into $apps_dir (the previous version was put back)."
    fi
  else
    mv "$new" "$dest" || die "could not move the app into $apps_dir."
  fi

  say "Installed OptionLens $version in $apps_dir"
  open "$dest"
  say "Done. OptionLens checks for updates itself from now on (Settings → Updates)."
}

main() {
  [ "$(id -u)" -ne 0 ] || die "run it as your own user, not with sudo."
  case "$(uname -s)" in
    Darwin) install_mac ;;
    Linux) install_linux ;;
    *) die "this installer runs on macOS and Linux; on Windows use install.ps1 (see the README)." ;;
  esac
}

# Sourced (scripts/test-install-sh.sh): define the functions only. Run (`| bash` or a file): install.
if [ "${BASH_SOURCE[0]:-}" = "${0}" ] || [ -z "${BASH_SOURCE[0]:-}" ]; then main "$@"; fi
