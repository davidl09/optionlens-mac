import { describe, expect, it } from "vitest";
import { barStart, chunks, durationOf, endDateTime, parseIntradayRequest } from "./intraday";
import { IntradayGate } from "./pacer";

const H = 3_600_000, D = 24 * H;

describe("intraday requests", () => {
  it("accepts well-formed requests only", () => {
    const span = { start: "2026-11-19T09:00:00Z", end: "2026-11-20T09:00:00Z" };
    expect(parseIntradayRequest({ symbol: "aapl", interval: "5m", spans: [span] })).toEqual({ symbol: "AAPL", interval: "5m", spans: [span] });
    expect(parseIntradayRequest({ symbol: "AAPL", interval: "2m", spans: [span] })).toBeNull();
    expect(parseIntradayRequest({ symbol: "AAPL", interval: "5m", spans: [] })).toBeNull();
    expect(parseIntradayRequest({ symbol: "AAPL", interval: "5m", spans: [{ start: span.end, end: span.start }] })).toBeNull();
    expect(parseIntradayRequest({ symbol: "AAPL; rm", interval: "5m", spans: [span] })).toBeNull();
    expect(parseIntradayRequest(null)).toBeNull();
  });

  it("cuts a span into windows the bar size allows, newest first", () => {
    expect(chunks(0, 2.5 * D, "1m")).toEqual([{ start: 1.5 * D, end: 2.5 * D }, { start: 0.5 * D, end: 1.5 * D }, { start: 0, end: 0.5 * D }]);
    expect(chunks(0, 10 * D, "15m")).toHaveLength(2);
    expect(chunks(0, 92 * D, "1h")).toHaveLength(4);
    expect(durationOf({ start: 0, end: D })).toBe("86400 S");
    expect(durationOf({ start: 0, end: 20 * 60_000 })).toBe("1200 S");
    expect(durationOf({ start: 0, end: 7 * D })).toBe("7 D");
    expect(endDateTime(Date.parse("2026-11-17T21:05:09Z"))).toBe("20261117-21:05:09");
    expect(barStart({ time: "1763413800", open: 1, high: 1, low: 1, close: 1, volume: 1 })).toBe(1763413800_000);
    expect(barStart({ time: "20261117  09:30:00", open: 1, high: 1, low: 1, close: 1, volume: 1 })).toBeNull();
  });

  it("gate: no identical request within 15 s, at most 60 in 10 minutes", () => {
    let now = 0;
    const g = new IntradayGate({ now: () => now });
    g.begin("a");
    expect(g.readyAt("a")).toBe(15_000);
    expect(g.readyAt("b")).toBe(0);
    for (let i = 1; i < 60; i++) { now = i * 1000; g.begin(`k${i}`); }
    now = 61_000;
    expect(g.readyAt("new")).toBe(600_000);
    now = 600_001;
    expect(g.readyAt("new")).toBe(600_001);
  });
});
