import { describe, expect, it, vi } from "vitest";
import { parseSha256Digest, selectAsset, updateAssetName } from "./asset";
import { asset, HEX, releaseJson, REPO } from "./fixtures.test-helper";
import { parseRelease, ReleaseSource, type CachedResponse, type HttpResponse } from "./release";
import { isRepoSlug } from "./repo";

describe("update source", () => {
  it("accepts owner/repo slugs only", () => {
    for (const ok of [REPO, "a/b", "Some-Org/repo.name_1"]) expect(isRepoSlug(ok)).toBe(true);
    for (const bad of ["", "davidl09", "a/b/c", "-a/b", "a/..", "a/.", "https://github.com/a/b", "a b/c", "a/b?x=1", 7, null]) expect(isRepoSlug(bad)).toBe(false);
  });
});

describe("release parsing", () => {
  it("keeps the notes as text and only assets served from the repo's release downloads", () => {
    const r = parseRelease(releaseJson({ assets: [
      asset("OptionLens-0.1.3-arm64.zip"),
      asset("evil.zip", { browser_download_url: "https://evil.example/OptionLens-0.1.3-arm64.zip" }),
      asset("other.zip", { browser_download_url: "https://github.com/someone/else/releases/download/v1/other.zip" }),
      asset("http.zip", { browser_download_url: `http://github.com/${REPO}/releases/download/v0.1.3/http.zip` }),
      asset("dots.zip", { browser_download_url: `https://github.com/${REPO}/releases/download/../../x/dots.zip` }),
    ] }), REPO);
    expect(r.version).toBe("0.1.3");
    expect(r.notes).toContain("<script>");
    expect(r.assets.map((a) => a.name)).toEqual(["OptionLens-0.1.3-arm64.zip"]);
  });

  it("rejects something that is not a release", () => {
    expect(() => parseRelease({ message: "Not Found" }, REPO)).toThrow(/not a release/);
  });
});

describe("asset selection and digests", () => {
  const r = parseRelease(releaseJson(), REPO);
  const pick = (platform: NodeJS.Platform, arch: string, release = r) => selectAsset(release, updateAssetName(platform, arch, release.version));
  it("picks the macOS zip for the process architecture", () => {
    expect(pick("darwin", "arm64")).toMatchObject({ ok: true, asset: { name: "OptionLens-0.1.3-arm64.zip" }, sha256: HEX });
    expect(pick("darwin", "x64")).toMatchObject({ ok: true, asset: { name: "OptionLens-0.1.3-x64.zip" }, sha256: "b".repeat(64) });
    expect(pick("darwin", "ia32")).toMatchObject({ ok: false, error: expect.stringMatching(/no OptionLens build for this system/) });
  });
  it("each OS picks its own asset from a release with every OS's files, never another OS's", () => {
    const names = ["OptionLens-0.1.3-arm64.zip", "OptionLens-0.1.3-x64.zip", "OptionLens-0.1.3-win-x64.zip", "OptionLens-0.1.3-win-x64-setup.exe",
      "OptionLens-0.1.3-linux-x64.AppImage", "OptionLens-0.1.3-linux-x64.AppImage.zsync", "OptionLens-0.1.3-x64.zip.blockmap"];
    const all = parseRelease(releaseJson({ assets: names.map((n) => asset(n)) }), REPO);
    expect(pick("darwin", "x64", all)).toMatchObject({ ok: true, asset: { name: "OptionLens-0.1.3-x64.zip" } });
    expect(pick("darwin", "arm64", all)).toMatchObject({ ok: true, asset: { name: "OptionLens-0.1.3-arm64.zip" } });
    expect(pick("win32", "x64", all)).toMatchObject({ ok: true, asset: { name: "OptionLens-0.1.3-win-x64-setup.exe" } });
    expect(pick("linux", "x64", all)).toMatchObject({ ok: true, asset: { name: "OptionLens-0.1.3-linux-x64.AppImage" } });
    for (const [os, arch] of [["linux", "arm64"], ["win32", "arm64"], ["win32", "ia32"], ["freebsd", "x64"]] as const) {
      expect(pick(os, arch, all)).toMatchObject({ ok: false });
    }
    // a macOS-only release (0.1.3 and earlier) offers nothing to Windows and Linux
    expect(pick("win32", "x64")).toMatchObject({ ok: false, error: expect.stringMatching(/has no OptionLens-0.1.3-win-x64-setup.exe/) });
    expect(pick("linux", "x64")).toMatchObject({ ok: false, error: expect.stringMatching(/has no OptionLens-0.1.3-linux-x64.AppImage/) });
  });
  it("refuses an asset without a SHA-256 digest", () => {
    const noDigest = parseRelease(releaseJson({ assets: [asset("OptionLens-0.1.3-arm64.zip", { digest: null })] }), REPO);
    expect(pick("darwin", "arm64", noDigest)).toMatchObject({ ok: false, error: expect.stringMatching(/no SHA-256 digest/) });
    const missing = parseRelease(releaseJson({ assets: [] }), REPO);
    expect(pick("darwin", "arm64", missing)).toMatchObject({ ok: false, error: expect.stringMatching(/has no OptionLens-0.1.3-arm64.zip/) });
  });
  it("parses only well-formed sha256 digests", () => {
    expect(parseSha256Digest(`sha256:${HEX}`)).toBe(HEX);
    for (const bad of [null, "", HEX, `sha1:${HEX}`, `sha256:${HEX.toUpperCase()}`, `sha256:${HEX}0`]) expect(parseSha256Digest(bad)).toBeNull();
  });
});

describe("ReleaseSource (ETag)", () => {
  const res = (status: number, body: unknown, headers: Record<string, string> = {}): HttpResponse =>
    ({ status, headers: { get: (n) => headers[n.toLowerCase()] ?? null }, json: async () => body });

  it("caches with the ETag and reuses the cached release on 304", async () => {
    let cache: CachedResponse | null = null;
    const http = vi.fn()
      .mockResolvedValueOnce(res(200, releaseJson(), { etag: 'W/"abc"' }))
      .mockResolvedValueOnce(res(304, null));
    const src = new ReleaseSource(http, { read: () => cache, write: (c) => { cache = c; } }, "OptionLens/0.1.2");
    expect((await src.latest(REPO)).version).toBe("0.1.3");
    expect(http.mock.calls[0][0]).toBe(`https://api.github.com/repos/${REPO}/releases/latest`);
    expect(http.mock.calls[0][1]["If-None-Match"]).toBeUndefined();
    expect((await src.latest(REPO)).version).toBe("0.1.3");
    expect(http.mock.calls[1][1]).toMatchObject({ "If-None-Match": 'W/"abc"', "User-Agent": "OptionLens/0.1.2" });
  });

  it("does not send another repo's ETag", async () => {
    const http = vi.fn().mockResolvedValue(res(200, releaseJson()));
    const src = new ReleaseSource(http, { read: () => ({ repo: "other/repo", etag: "x", body: releaseJson() }), write: () => undefined }, "ua");
    await src.latest(REPO);
    expect(http.mock.calls[0][1]["If-None-Match"]).toBeUndefined();
  });

  it("explains 404 and the rate limit", async () => {
    const src = (r: HttpResponse) => new ReleaseSource(async () => r, { read: () => null, write: () => undefined }, "ua");
    await expect(src(res(404, {})).latest(REPO)).rejects.toThrow(/no published release/);
    await expect(src(res(403, {}, { "x-ratelimit-reset": "1800000000" })).latest(REPO)).rejects.toThrow(/rate limit/);
    await expect(src(res(500, {})).latest(REPO)).rejects.toThrow(/500/);
  });
});
