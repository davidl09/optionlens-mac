// The update source: a GitHub repository "owner/repo" whose releases carry the app zips.
export const DEFAULT_UPDATE_REPO = "davidl09/optionlens-mac";

const REPO_SLUG_RE = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\/[A-Za-z0-9._-]{1,100}$/;

export function isRepoSlug(s: unknown): s is string {
  return typeof s === "string" && REPO_SLUG_RE.test(s) && !/\/\.{1,2}$/.test(s);
}

export function latestReleaseUrl(repo: string): string {
  return `https://api.github.com/repos/${repo}/releases/latest`;
}

/** Every asset of the repo's releases is served below this URL (GitHub then redirects to its CDN). */
export function downloadPrefix(repo: string): string {
  return `https://github.com/${repo}/releases/download/`;
}
