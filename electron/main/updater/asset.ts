// Which release asset this Mac installs: OptionLens-<version>-<arch>.zip for the running process's
// architecture, and only with a SHA-256 digest published by GitHub (the download is checked against it).
import type { Release, ReleaseAsset } from "./release";

export type Selected = { ok: true; asset: ReleaseAsset; sha256: string } | { ok: false; error: string };

const ARCHES: Record<string, string> = { arm64: "arm64", x64: "x64" };

/** "sha256:<64 hex>" → the hex digest, else null. */
export function parseSha256Digest(d: string | null): string | null {
  const m = d ? /^sha256:([0-9a-f]{64})$/.exec(d) : null;
  return m ? m[1] : null;
}

export function assetName(version: string, arch: string): string {
  return `OptionLens-${version}-${arch}.zip`;
}

export function selectAsset(release: Release, processArch: string): Selected {
  const arch = ARCHES[processArch];
  if (!arch) return { ok: false, error: `No OptionLens build for this architecture (${processArch}).` };
  const name = assetName(release.version, arch);
  const asset = release.assets.find((a) => a.name === name);
  if (!asset) return { ok: false, error: `Release ${release.version} has no ${name}.` };
  const sha256 = parseSha256Digest(asset.digest);
  if (!sha256) return { ok: false, error: `Release ${release.version} publishes no SHA-256 digest for ${name}; it is not installed.` };
  return { ok: true, asset, sha256 };
}
