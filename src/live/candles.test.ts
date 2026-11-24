import { describe, expect, it } from "vitest";
import type { IntradayBar } from "../api/types";
import { barLabel, chartView, inRegularHours, smaOf } from "../lib/chartRange";
import { bucketStart, nextCandle, withLiveCandle, type LiveCandle } from "./candles";

const M5 = 300_000;
const t = (iso: string) => Date.parse(iso);
// Tue 2026-11-17, EST (UTC-5): 10:00 ET = 15:00Z
const bar = (iso: string, o: Partial<IntradayBar> = {}): IntradayBar => ({ ts: iso, open: 100, high: 101, low: 99, close: 100.5, volume: 1000, ...o });

describe("live candle", () => {
  const stored = [bar("2026-11-17T14:50:00.000Z"), bar("2026-11-17T14:55:00.000Z")];
  const last = stored[1];

  it("buckets on the stored bars' grid, else on New York clock time", () => {
    expect(bucketStart(t("2026-11-17T14:57:12Z"), M5, last)).toBe(t("2026-11-17T14:55:00Z"));
    expect(bucketStart(t("2026-11-17T15:03:00Z"), M5, last)).toBe(t("2026-11-17T15:00:00Z"));
    // a new day: the clock grid (1h bars at whole New York hours)
    expect(bucketStart(t("2026-11-18T14:31:00Z"), 3_600_000, last)).toBe(t("2026-11-18T14:00:00Z"));
  });

  it("updates the bar it falls in from the stored one, volume from day-volume deltas", () => {
    let c = nextCandle(null, { price: 102, volume: 50_000, at: t("2026-11-17T14:57:00Z") }, M5, last);
    expect(c).toMatchObject({ start: t("2026-11-17T14:55:00Z"), open: 100, high: 102, low: 99, close: 102, volume: 1000 });
    c = nextCandle(c, { price: 98.5, volume: 50_400, at: t("2026-11-17T14:58:00Z") }, M5, last);
    expect(c).toMatchObject({ high: 102, low: 98.5, close: 98.5, volume: 1400 });
    const shown = withLiveCandle(stored, c);
    expect(shown).toHaveLength(2);
    expect(shown[1]).toEqual({ ts: "2026-11-17T14:55:00.000Z", open: 100, high: 102, low: 98.5, close: 98.5, volume: 1400 });
  });

  it("rolls to a new bar at the boundary, counting volume from the last day volume seen", () => {
    let c = nextCandle(null, { price: 101, volume: 50_000, at: t("2026-11-17T14:59:30Z") }, M5, last);
    c = nextCandle(c, { price: 101.4, volume: 50_300, at: t("2026-11-17T15:00:20Z") }, M5, last) as LiveCandle;
    expect(c).toMatchObject({ start: t("2026-11-17T15:00:00Z"), open: 101.4, high: 101.4, low: 101.4, close: 101.4, volume: 300 });
    const shown = withLiveCandle(stored, c);
    expect(shown).toHaveLength(3);
    expect(shown[1]).toMatchObject({ ts: "2026-11-17T14:55:00.000Z", high: 101, close: 101 });   // the rolled-over bar stays
    expect(shown[2].ts).toBe("2026-11-17T15:00:00.000Z");
    c = nextCandle(c, { price: 102, volume: 50_500, at: t("2026-11-17T15:05:01Z") }, M5, last) as LiveCandle;
    expect(withLiveCandle(stored, c).map((b) => b.ts.slice(11, 16))).toEqual(["14:50", "14:55", "15:00", "15:05"]);
    expect(c.volume).toBe(200);
  });

  it("without day volumes keeps the source's volume (unknown for a new bar); ignores stale and out-of-order ticks", () => {
    let c = nextCandle(null, { price: 101, volume: null, at: t("2026-11-17T14:56:00Z") }, M5, last);
    expect(c?.volume).toBe(1000);
    c = nextCandle(c, { price: 101, volume: null, at: t("2026-11-17T15:01:00Z") }, M5, last);
    expect(c?.volume).toBeNull();
    expect(nextCandle(c, { price: 1, volume: null, at: t("2026-11-17T14:56:30Z") }, M5, last)).toBe(c);   // older bucket
    expect(nextCandle(null, { price: 1, volume: null, at: t("2026-11-17T14:40:00Z") }, M5, last)).toBeNull();   // before the stored bars
  });
});

describe("chart ranges", () => {
  it("offers sensible intervals per range, daily beyond 3M", () => {
    expect(chartView("1d", null)).toEqual({ kind: "intraday", range: "1d", interval: "1m" });
    expect(chartView("5d", "1h")).toEqual({ kind: "intraday", range: "5d", interval: "1h" });
    expect(chartView("5d", "1m")).toEqual({ kind: "intraday", range: "5d", interval: "5m" });   // not offered: default
    expect(chartView("1m", null)).toEqual({ kind: "intraday", range: "1m", interval: "15m" });
    expect(chartView("3m", "1d")).toEqual({ kind: "daily", range: "3m" });
    expect(chartView("1y", "5m")).toEqual({ kind: "daily", range: "1y" });
    expect(chartView("bogus", null)).toEqual({ kind: "daily", range: "1y" });
  });

  it("labels bars in New York time and knows the regular session", () => {
    expect(barLabel("2026-11-17T14:30:00Z", "1d")).toBe("09:30");
    expect(barLabel("2026-11-17T14:30:00Z", "5d")).toBe("Tue 09:30");
    expect(barLabel("2026-07-01T13:30:00Z", "1m")).toBe("07-01 09:30");   // EDT
    expect(inRegularHours(t("2026-11-17T14:30:00Z"))).toBe(true);
    expect(inRegularHours(t("2026-11-17T14:29:00Z"))).toBe(false);
    expect(inRegularHours(t("2026-11-17T21:00:00Z"))).toBe(false);
    expect(inRegularHours(t("2026-11-21T15:00:00Z"))).toBe(false);   // Saturday
  });

  it("SMA over the series itself", () => {
    expect(smaOf([1, 2, 3, 4].map((close) => ({ close })), 3)).toEqual([null, null, 2, 3]);
  });
});
