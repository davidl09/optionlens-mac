import { describe, expect, it } from "vitest";
import { HistoricalPacer } from "./pacer";

function clock(start = 1_000_000) {
  let t = start;
  return { now: () => t, advance: (ms: number) => { t += ms; } };
}

describe("HistoricalPacer", () => {
  it("allows the first request immediately and spaces daily-bar requests 1 s apart (no 30-s-bar hard limits)", () => {
    const c = clock();
    const p = new HistoricalPacer({ now: c.now });
    expect(p.readyAt()).toBe(c.now());
    p.begin();
    expect(p.readyAt()).toBe(c.now() + 1000);
    c.advance(999);
    expect(() => p.begin()).toThrow(/before the pacer allowed/);
    c.advance(1);
    expect(() => p.begin()).not.toThrow();
    // 120 requests in ~2 minutes: daily bars are not capped at 60 per 10 minutes.
    for (let i = 0; i < 120; i++) { c.advance(p.readyAt() - c.now()); p.begin(); }
    expect(p.readyAt() - 1_000_000).toBeLessThan(125_000);
  });

  it("begin() right when ready never trips over a moving clock", () => {
    let t = 0;
    const p = new HistoricalPacer({ now: () => (t += 1), minSpacingMs: 0 });   // every read is 1 ms later
    expect(() => p.begin()).not.toThrow();
  });

  it("backs off exponentially on pacing violations, widens the spacing and relaxes it after successes", () => {
    const c = clock();
    const p = new HistoricalPacer({ now: c.now, baseBackoffMs: 30_000, maxBackoffMs: 100_000, minSpacingMs: 1000, maxSpacingMs: 4000, relaxAfter: 3 });
    expect(p.violation()).toBe(30_000);
    expect(p.readyAt()).toBe(c.now() + 30_000);
    expect(p.spacingMs).toBe(2000);
    expect(p.violation()).toBe(60_000);
    expect(p.violation()).toBe(100_000);   // capped
    expect(p.spacingMs).toBe(4000);        // capped
    p.success(); p.success();
    expect(p.spacingMs).toBe(4000);
    p.success();
    expect(p.spacingMs).toBe(2000);
    expect(p.violation()).toBe(30_000);    // successes reset the backoff
  });
});
