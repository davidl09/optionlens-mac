// Which release asset an installation updates from, by OS and CPU (the one authoritative list of update
// asset names; CI, install.sh and install.ps1 build and pick the same names), and only with a SHA-256 digest
// published by GitHub (the download is checked against it). Names are matched exactly, so an installation
// never picks another OS's file: the macOS zips keep their original names (OptionLens-<v>-<arch>.zip, which
// 0.1.3 updaters look for); Windows and Linux assets carry the OS in their name.
import type { Release, ReleaseAsset } from "./release";

export type Selected = { ok: true; asset: ReleaseAsset; sha256: string } | { ok: false; error: string };

const UPDATE_ASSETS: Record<string, (version: string) => string> = {
  "darwin-arm64": (v) => `OptionLens-${v}-arm64.zip`,
  "darwin-x64": (v) => `OptionLens-${v}-x64.zip`,
  "win32-x64": (v) => windowsSetupName(v),
  "linux-x64": (v) => `OptionLens-${v}-linux-x64.AppImage`,
};

/** The Windows installer of `version` (also the name its copy in the backups folder keeps). */
export function windowsSetupName(version: string): string {
  return `OptionLens-${version}-win-x64-setup.exe`;
}

/** The asset this OS and CPU install `version` from, or null when there is no build for them. */
export function updateAssetName(platform: NodeJS.Platform, arch: string, version: string): string | null {
  return UPDATE_ASSETS[`${platform}-${arch}`]?.(version) ?? null;
}

/** "sha256:<64 hex>" → the hex digest, else null. */
export function parseSha256Digest(d: string | null): string | null {
  const m = d ? /^sha256:([0-9a-f]{64})$/.exec(d) : null;
  return m ? m[1] : null;
}

export function selectAsset(release: Release, name: string | null): Selected {
  if (!name) return { ok: false, error: "There is no OptionLens build for this system (operating system and CPU)." };
  const asset = release.assets.find((a) => a.name === name);
  if (!asset) return { ok: false, error: `Release ${release.version} has no ${name}.` };
  const sha256 = parseSha256Digest(asset.digest);
  if (!sha256) return { ok: false, error: `Release ${release.version} publishes no SHA-256 digest for ${name}; it is not installed.` };
  return { ok: true, asset, sha256 };
}
