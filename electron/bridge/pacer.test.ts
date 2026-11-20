import { describe, expect, it } from "vitest";
import { HistoricalPacer } from "./pacer";

function clock(start = 1_000_000) {
  let t = start;
  return { now: () => t, advance: (ms: number) => { t += ms; } };
}

describe("HistoricalPacer", () => {
  it("allows the first request immediately and spaces later ones ≥ 10 s apart", () => {
    const c = clock();
    const p = new HistoricalPacer({ now: c.now });
    expect(p.readyAt()).toBe(c.now());
    p.begin();
    expect(p.readyAt()).toBe(c.now() + 10_000);
    c.advance(9_999);
    expect(() => p.begin()).toThrow(/before the pacer allowed/);
    c.advance(1);
    expect(() => p.begin()).not.toThrow();
  });

  it("caps starts per rolling 10-minute window", () => {
    const c = clock();
    const p = new HistoricalPacer({ now: c.now, minSpacingMs: 1000, maxPerWindow: 3, windowMs: 60_000 });
    const first = c.now();
    for (let i = 0; i < 3; i++) { c.advance(p.readyAt() - c.now()); p.begin(); }
    // 4th must wait until the first start leaves the window
    expect(p.readyAt()).toBe(first + 60_000);
  });

  it("backs off exponentially on pacing violations and resets after success", () => {
    const c = clock();
    const p = new HistoricalPacer({ now: c.now, baseBackoffMs: 30_000, maxBackoffMs: 100_000 });
    expect(p.violation()).toBe(30_000);
    expect(p.readyAt()).toBe(c.now() + 30_000);
    expect(p.violation()).toBe(60_000);
    expect(p.violation()).toBe(100_000);   // capped
    p.success();
    expect(p.violation()).toBe(30_000);
  });

  it("never schedules 60 requests within 10 minutes with defaults", () => {
    const c = clock();
    const p = new HistoricalPacer({ now: c.now });
    const starts: number[] = [];
    for (let i = 0; i < 200; i++) { c.advance(p.readyAt() - c.now()); p.begin(); starts.push(c.now()); }
    for (let i = 0; i + 59 < starts.length; i++) expect(starts[i + 59] - starts[i]).toBeGreaterThanOrEqual(600_000);
    for (let i = 1; i < starts.length; i++) expect(starts[i] - starts[i - 1]).toBeGreaterThanOrEqual(10_000);
  });
});
