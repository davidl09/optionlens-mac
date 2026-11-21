// Downloads a release asset over HTTPS to a file and checks it against the SHA-256 digest that the GitHub
// API published for it. Redirects are followed by hand (GitHub sends asset downloads to its CDN) and must
// stay on HTTPS. More bytes than the published size, a different size or a different digest → the file
// is deleted and the download fails.
import { createHash } from "node:crypto";
import { createWriteStream } from "node:fs";
import { rm } from "node:fs/promises";
import { Readable, Transform } from "node:stream";
import { pipeline } from "node:stream/promises";

export interface StreamResponse { status: number; headers: { get(name: string): string | null }; body: AsyncIterable<Uint8Array> | null }
/** One HTTP GET that does not follow redirects itself. */
export type StreamGet = (url: string) => Promise<StreamResponse>;

export interface DownloadSpec {
  url: string;
  dest: string;
  sha256: string;
  size: number;
  onProgress?: (fraction: number) => void;
}

const REDIRECTS = new Set([301, 302, 303, 307, 308]);
const MAX_REDIRECTS = 5;

async function openHttps(get: StreamGet, url: string): Promise<StreamResponse> {
  let current = new URL(url);
  for (let hop = 0; ; hop++) {
    if (current.protocol !== "https:") throw new Error(`Refusing a non-HTTPS download URL (${current.protocol}//${current.host}).`);
    const res = await get(current.href);
    if (!REDIRECTS.has(res.status)) {
      if (res.status !== 200 || !res.body) throw new Error(`The download failed (HTTP ${res.status}).`);
      return res;
    }
    const location = res.headers.get("location");
    if (!location || hop >= MAX_REDIRECTS) throw new Error("The download was redirected too often or without a location.");
    current = new URL(location, current);
  }
}

export async function downloadVerified(get: StreamGet, spec: DownloadSpec): Promise<void> {
  const res = await openHttps(get, spec.url);
  const hash = createHash("sha256");
  let bytes = 0;
  let reported = -1;
  const meter = new Transform({
    transform(chunk: Buffer, _enc, done) {
      bytes += chunk.length;
      if (bytes > spec.size) { done(new Error("The download is larger than the release says.")); return; }
      hash.update(chunk);
      const pct = spec.size > 0 ? Math.floor((bytes / spec.size) * 100) : 0;
      if (pct !== reported) { reported = pct; spec.onProgress?.(pct / 100); }
      done(null, chunk);
    },
  });
  try {
    await pipeline(Readable.from(res.body!), meter, createWriteStream(spec.dest, { mode: 0o600 }));
    if (bytes !== spec.size) throw new Error(`The download is incomplete (${bytes} of ${spec.size} bytes).`);
    const actual = hash.digest("hex");
    if (actual !== spec.sha256) throw new Error(`The download's SHA-256 (${actual}) does not match the release (${spec.sha256}); it was deleted.`);
  } catch (e) {
    await rm(spec.dest, { force: true });
    throw e;
  }
}
