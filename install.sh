#!/usr/bin/env bash
# OptionLens for Mac installer: installs (or updates to) the latest release in /Applications.
#
#   curl -fsSL https://raw.githubusercontent.com/davidl09/optionlens-mac/main/install.sh | bash
#
# Steps: find the latest release (GitHub API), download the zip for this Mac's architecture over HTTPS,
# check its SHA-256 against the digest GitHub publishes for the asset, extract it with ditto, check the
# app's bundle id, version and signature, quit a running OptionLens, keep the installed app as a backup
# (~/Library/Application Support/OptionLens/backups, the newest 2 are kept), move the new app into
# /Applications and open it. It never uses sudo. Files downloaded with curl are not quarantined, so the
# app (ad-hoc signed, not notarized) opens without a Gatekeeper prompt.
set -euo pipefail

REPO="davidl09/optionlens-mac"
BUNDLE_ID="app.optionlens.mac"
APPS_DIR="/Applications"
DEST="$APPS_DIR/OptionLens.app"
BACKUPS="$HOME/Library/Application Support/OptionLens/backups"
KEEP_BACKUPS=2

say() { printf '==> %s\n' "$*"; }
die() { printf 'OptionLens install failed: %s\n' "$*" >&2; exit 1; }
plist_value() { /usr/libexec/PlistBuddy -c "Print :$2" "$1/Contents/Info.plist" 2>/dev/null; }

[ "$(uname -s)" = Darwin ] || die "OptionLens is a macOS app; this installer only runs on macOS."
[ "$(id -u)" -ne 0 ] || die "run it as your own user, not with sudo."
case "$(uname -m)" in
  arm64) ARCH=arm64 ;;
  x86_64) ARCH=x64 ;;
  *) die "unsupported CPU architecture: $(uname -m)" ;;
esac
[ -w "$APPS_DIR" ] || die "$APPS_DIR is not writable for $(id -un). Sign in with an administrator account, or ask an administrator to install OptionLens."

TMP="$(mktemp -d -t optionlens-install)"
trap 'rm -rf "$TMP"' EXIT

say "Looking up the latest OptionLens release"
curl -fsSL --proto '=https' --tlsv1.2 -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/$REPO/releases/latest" -o "$TMP/release.json" \
  || die "could not reach the GitHub API (offline, or rate limited: try again in an hour)."

# The release JSON is parsed with JavaScript for Automation (built into macOS): version, asset URL, digest.
INFO="$(osascript -l JavaScript - "$TMP/release.json" "$ARCH" <<'JXA'
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
{ read -r VERSION; read -r ASSET; read -r URL; read -r DIGEST || DIGEST=""; } <<<"$INFO"

[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "unexpected release version: $VERSION"
[[ "$URL" == "https://github.com/$REPO/releases/download/"* ]] || die "unexpected download URL: $URL"
[[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || die "the release does not publish a SHA-256 digest for $ASSET; refusing to install it."

say "Downloading OptionLens $VERSION ($ARCH)"
curl -fL --proto '=https' --proto-redir '=https' --tlsv1.2 --progress-bar -o "$TMP/$ASSET" "$URL" \
  || die "the download failed."
ACTUAL="sha256:$(shasum -a 256 "$TMP/$ASSET" | awk '{print $1}')"
[ "$ACTUAL" = "$DIGEST" ] || die "the download's SHA-256 ($ACTUAL) does not match the release ($DIGEST)."
say "Checksum verified"

mkdir "$TMP/stage"
ditto -x -k "$TMP/$ASSET" "$TMP/stage" || die "could not extract $ASSET."
NEW="$TMP/stage/OptionLens.app"
[ "$(ls -A "$TMP/stage")" = "OptionLens.app" ] && [ -d "$NEW" ] && [ ! -L "$NEW" ] \
  || die "$ASSET does not contain exactly OptionLens.app."
[ "$(plist_value "$NEW" CFBundleIdentifier)" = "$BUNDLE_ID" ] || die "the downloaded app has an unexpected bundle id."
[ "$(plist_value "$NEW" CFBundleShortVersionString)" = "$VERSION" ] || die "the downloaded app is not version $VERSION."
codesign --verify --deep --strict "$NEW" || die "the downloaded app's signature is invalid."
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

if [ -e "$DEST" ]; then
  OLD="$(plist_value "$DEST" CFBundleShortVersionString || true)"
  [[ "$OLD" =~ ^[0-9A-Za-z.+-]+$ ]] || OLD="unknown"
  mkdir -p "$BACKUPS"
  BACKUP="$BACKUPS/OptionLens-$OLD.app"
  rm -rf "$BACKUP"
  mv "$DEST" "$BACKUP" || die "could not move the installed OptionLens out of $APPS_DIR."
  touch "$BACKUP"
  say "Kept OptionLens $OLD as a backup in $BACKUPS"
  shopt -s nullglob
  ALL=("$BACKUPS"/OptionLens-*.app)
  if [ "${#ALL[@]}" -gt "$KEEP_BACKUPS" ]; then
    # Newest first by modification time (each backup is touched when it is made); drop the rest. The
    # names are OptionLens-<version>.app, so parsing ls output is safe here.
    # shellcheck disable=SC2012
    ls -1dt "${ALL[@]}" | tail -n +"$((KEEP_BACKUPS + 1))" | while IFS= read -r old; do rm -rf "$old"; done
  fi
  if ! mv "$NEW" "$DEST"; then
    mv "$BACKUP" "$DEST" || true
    die "could not move the new app into $APPS_DIR (the previous version was put back)."
  fi
else
  mv "$NEW" "$DEST" || die "could not move the app into $APPS_DIR."
fi

say "Installed OptionLens $VERSION in $APPS_DIR"
open "$DEST"
say "Done. OptionLens checks for updates itself from now on (Settings → Updates)."
