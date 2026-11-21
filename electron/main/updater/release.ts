// The latest release of the update repo from the GitHub API (unauthenticated: 60 requests/h per IP). The
// response is cached with its ETag; the next check sends If-None-Match, and a 304 (which does not count
// against the rate limit) reuses the cached release. Assets outside the repo's release downloads are dropped.
import { downloadPrefix, latestReleaseUrl } from "./repo";

export interface ReleaseAsset { name: string; url: string; size: number; digest: string | null }
export interface Release {
  version: string;   // tag without the leading "v"
  notes: string;
  htmlUrl: string;
  publishedAt: string | null;
  draft: boolean;
  prerelease: boolean;
  assets: ReleaseAsset[];
}

export interface HttpResponse { status: number; headers: { get(name: string): string | null }; json(): Promise<unknown> }
export type HttpGet = (url: string, headers: Record<string, string>) => Promise<HttpResponse>;

/** The raw API body with its ETag, for conditional requests. */
export interface CachedResponse { repo: string; etag: string; body: unknown }
export interface ResponseCache { read(): CachedResponse | null; write(c: CachedResponse | null): void }

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const str = (v: unknown): string | null => (typeof v === "string" ? v : null);

function isRepoAssetUrl(url: string, repo: string): boolean {
  try {
    const u = new URL(url);
    // href === url: nothing for the URL parser to normalize away (no "..", no odd encodings).
    return u.protocol === "https:" && u.search === "" && u.hash === "" && u.href === url
      && url.toLowerCase().startsWith(downloadPrefix(repo).toLowerCase());
  } catch { return false; }
}

/** GitHub release JSON → Release; throws when the shape is not a release. */
export function parseRelease(v: unknown, repo: string): Release {
  if (!isObj(v) || typeof v.tag_name !== "string" || !Array.isArray(v.assets)) throw new Error("GitHub returned something that is not a release.");
  const assets: ReleaseAsset[] = [];
  for (const a of v.assets) {
    if (!isObj(a)) continue;
    const name = str(a.name), url = str(a.browser_download_url);
    if (!name || !url || typeof a.size !== "number" || !isRepoAssetUrl(url, repo)) continue;
    assets.push({ name, url, size: a.size, digest: str(a.digest) });
  }
  return {
    version: v.tag_name.replace(/^v/, ""),
    notes: str(v.body) ?? "",
    htmlUrl: str(v.html_url) ?? `https://github.com/${repo}/releases`,
    publishedAt: str(v.published_at),
    draft: v.draft === true,
    prerelease: v.prerelease === true,
    assets,
  };
}

export const API_HEADERS = { Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28" } as const;

export class ReleaseSource {
  constructor(private readonly http: HttpGet, private readonly cache: ResponseCache, private readonly userAgent: string) {}

  async latest(repo: string): Promise<Release> {
    const c = this.cache.read();
    const cached = c && c.repo === repo ? c : null;
    const res = await this.http(latestReleaseUrl(repo), { ...API_HEADERS, "User-Agent": this.userAgent, ...(cached ? { "If-None-Match": cached.etag } : {}) });
    if (res.status === 304 && cached) return parseRelease(cached.body, repo);
    if (res.status === 200) {
      const body = await res.json();
      const release = parseRelease(body, repo);
      const etag = res.headers.get("etag");
      this.cache.write(etag ? { repo, etag, body } : null);
      return release;
    }
    if (res.status === 404) throw new Error(`${repo} has no published release (or the repository does not exist).`);
    if (res.status === 403 || res.status === 429) {
      const reset = Number(res.headers.get("x-ratelimit-reset"));
      const at = Number.isFinite(reset) && reset > 0 ? ` until ${new Date(reset * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "";
      throw new Error(`GitHub's rate limit for update checks is used up${at}.`);
    }
    throw new Error(`GitHub answered ${res.status} to the update check.`);
  }
}

export function isCachedResponse(v: unknown): v is CachedResponse {
  return isObj(v) && typeof v.repo === "string" && typeof v.etag === "string" && "body" in v;
}
