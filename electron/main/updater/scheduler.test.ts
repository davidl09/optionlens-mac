import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { releaseJson } from "./fixtures.test-helper";
import { ReleaseSource, type CachedResponse, type HttpGet } from "./release";
import { CHECK_EVERY_MS, scheduleUpdates } from "./scheduler";

describe("update schedule", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it("checks at launch and every 6 hours, ticks every minute, and stops", async () => {
    const u = { check: vi.fn(async () => undefined), tick: vi.fn(async () => undefined) };
    const stop = scheduleUpdates(u);
    expect(u.check).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(CHECK_EVERY_MS - 1);
    expect(u.check).toHaveBeenCalledTimes(1);
    expect(u.tick).toHaveBeenCalledTimes(359);
    await vi.advanceTimersByTimeAsync(1);
    expect(u.check).toHaveBeenCalledTimes(2);
    stop();
    await vi.advanceTimersByTimeAsync(CHECK_EVERY_MS * 2);
    expect(u.check).toHaveBeenCalledTimes(2);
  });

  it("scheduled checks are conditional (If-None-Match), so a day of checks costs one full response", async () => {
    let cache: CachedResponse | null = null;
    const sent: (string | undefined)[] = [];
    const http: HttpGet = async (_url, headers) => {
      sent.push(headers["If-None-Match"]);
      return headers["If-None-Match"] === '"v1"'
        ? { status: 304, headers: { get: () => null }, json: async () => null }
        : { status: 200, headers: { get: (n) => (n === "etag" ? '"v1"' : null) }, json: async () => releaseJson() };
    };
    const src = new ReleaseSource(http, { read: () => cache, write: (c) => { cache = c; } }, "ua");
    const versions: string[] = [];
    const stop = scheduleUpdates({ check: async () => { versions.push((await src.latest("davidl09/optionlens-mac")).version); }, tick: async () => undefined });
    await vi.advanceTimersByTimeAsync(CHECK_EVERY_MS * 4);
    stop();
    expect(sent).toEqual([undefined, '"v1"', '"v1"', '"v1"', '"v1"']);
    expect(versions).toEqual(["0.1.3", "0.1.3", "0.1.3", "0.1.3", "0.1.3"]);
  });
});
