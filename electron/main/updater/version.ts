// Semantic versions (x.y.z[-pre][+build], optional leading "v") and the "is this an update?" rule:
// only a strictly newer release version counts, never a prerelease and never a downgrade.
export interface SemVer { major: number; minor: number; patch: number; pre: string[] }

const SEMVER_RE = /^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/;

export function parseVersion(v: string): SemVer | null {
  const m = SEMVER_RE.exec(v.trim());
  if (!m) return null;
  return { major: Number(m[1]), minor: Number(m[2]), patch: Number(m[3]), pre: m[4] ? m[4].split(".") : [] };
}

function comparePre(a: string[], b: string[]): number {
  if (!a.length || !b.length) return Math.sign(b.length - a.length);   // a release sorts after its prereleases
  for (let i = 0; i < Math.min(a.length, b.length); i++) {
    const na = /^\d+$/.test(a[i]), nb = /^\d+$/.test(b[i]);
    if (na && nb) { const d = Number(a[i]) - Number(b[i]); if (d) return Math.sign(d); continue; }
    if (na !== nb) return na ? -1 : 1;                      // numeric identifiers sort first
    if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1;
  }
  return Math.sign(a.length - b.length);
}

/** <0, 0 or >0 as a is older than, the same as or newer than b. */
export function compareVersions(a: SemVer, b: SemVer): number {
  return Math.sign(a.major - b.major) || Math.sign(a.minor - b.minor) || Math.sign(a.patch - b.patch) || comparePre(a.pre, b.pre);
}

/** True only when `candidate` is a release (no prerelease part) strictly newer than `current`. */
export function isUpdate(candidate: string, current: string): boolean {
  const c = parseVersion(candidate), cur = parseVersion(current);
  return c !== null && cur !== null && c.pre.length === 0 && compareVersions(c, cur) > 0;
}
