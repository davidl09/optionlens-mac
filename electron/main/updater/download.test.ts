import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { downloadVerified, type StreamGet, type StreamResponse } from "./download";

const DATA = Buffer.from("x".repeat(5000));
const SHA = createHash("sha256").update(DATA).digest("hex");
const URL0 = "https://github.com/davidl09/optionlens-mac/releases/download/v0.1.3/OptionLens-0.1.3-arm64.zip";

async function* chunks(buf: Buffer, size = 1000) { for (let i = 0; i < buf.length; i += size) yield buf.subarray(i, i + size); }
const ok = (buf = DATA): StreamResponse => ({ status: 200, headers: { get: () => null }, body: chunks(buf) });
const redirect = (to: string): StreamResponse => ({ status: 302, headers: { get: (n) => (n === "location" ? to : null) }, body: null });

describe("verified download", () => {
  let dir: string;
  beforeEach(() => { dir = mkdtempSync(join(tmpdir(), "ol-dl-")); });
  afterEach(() => { rmSync(dir, { recursive: true, force: true }); });

  it("follows HTTPS redirects, reports progress and keeps a file whose digest matches", async () => {
    const seen: string[] = [];
    const get: StreamGet = async (url) => { seen.push(url); return url === URL0 ? redirect("https://release-assets.githubusercontent.com/x?sig=1") : ok(); };
    const progress: number[] = [];
    const dest = join(dir, "a.zip");
    await downloadVerified(get, { url: URL0, dest, sha256: SHA, size: DATA.length, onProgress: (f) => progress.push(f) });
    expect(seen).toEqual([URL0, "https://release-assets.githubusercontent.com/x?sig=1"]);
    expect(readFileSync(dest)).toEqual(DATA);
    expect(progress[0]).toBe(0.2);
    expect(progress.at(-1)).toBe(1);
  });

  it("deletes the file and fails when the digest does not match", async () => {
    const dest = join(dir, "a.zip");
    await expect(downloadVerified(async () => ok(), { url: URL0, dest, sha256: "0".repeat(64), size: DATA.length })).rejects.toThrow(/does not match/);
    expect(existsSync(dest)).toBe(false);
  });

  it("fails on a size mismatch (larger or truncated)", async () => {
    const dest = join(dir, "a.zip");
    await expect(downloadVerified(async () => ok(), { url: URL0, dest, sha256: SHA, size: 4000 })).rejects.toThrow(/larger/);
    await expect(downloadVerified(async () => ok(DATA.subarray(0, 3000)), { url: URL0, dest, sha256: SHA, size: DATA.length })).rejects.toThrow(/incomplete/);
    expect(existsSync(dest)).toBe(false);
  });

  it("refuses non-HTTPS URLs and redirects, endless redirects and HTTP errors", async () => {
    const dest = join(dir, "a.zip");
    const spec = { url: URL0, dest, sha256: SHA, size: DATA.length };
    await expect(downloadVerified(async () => redirect("http://evil.example/a.zip"), spec)).rejects.toThrow(/non-HTTPS/);
    await expect(downloadVerified(async () => ok(), { ...spec, url: "http://github.com/a" })).rejects.toThrow(/non-HTTPS/);
    await expect(downloadVerified(async () => redirect("https://github.com/loop"), spec)).rejects.toThrow(/redirected too often/);
    await expect(downloadVerified(async () => ({ status: 404, headers: { get: () => null }, body: null }), spec)).rejects.toThrow(/HTTP 404/);
  });
});
