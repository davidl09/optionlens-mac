import { describe, expect, it } from "vitest";
import type { IntradayBar } from "../api/types";
import { barLabel, chartView, inRegularHours, smaOf } from "../lib/chartRange";
import { bucketStart, nextCandle, vwapValues, withLiveCandle, type LiveCandle } from "./candles";
import { LiveStore } from "./store";

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

describe("live candles across bar reloads", () => {
  const M1 = 60_000;
  // 1m bars 11:50..11:57 ET (the source's candles end ~15 minutes before now), Tue 2026-11-17 (EST)
  const minute = (hm: string, o: Partial<IntradayBar> = {}) => bar(`2026-11-17T${String(Number(hm.slice(0, 2)) + 5).padStart(2, "0")}:${hm.slice(3)}:00.000Z`, o);
  const loaded = ["11:50", "11:51", "11:52", "11:53", "11:54", "11:55", "11:56", "11:57"].map((x) => minute(x));
  const hms = (bars: IntradayBar[]) => bars.map((b) => new Date(Date.parse(b.ts) - 5 * 3_600_000).toISOString().slice(11, 16));
  const ticks = (store: LiveStore, series: string, bars: IntradayBar[], from: number, n: number) => {
    for (let i = 0; i < n; i++) {
      const at = t("2026-11-17T17:12:05Z") + (from + i) * 20_000;   // every 20 s from 12:12:05 ET
      store.keepCandle(series, nextCandle(store.candle(series), { price: 200 + from + i, volume: 1_000 + 10 * (from + i), at }, M1, bars[bars.length - 1]));
    }
  };

  it("keeps the closed candles in the store: a remount (fresh state from the store) continues them", () => {
    const store = new LiveStore();
    ticks(store, "DELL|60000|rth", loaded, 0, 9);                      // 12:12:05 .. 12:14:45: three minutes
    const remounted = store.candle("DELL|60000|rth");
    expect(hms(withLiveCandle(loaded, remounted))).toEqual([...hms(loaded), "12:12", "12:13", "12:14"]);
    expect(store.candle("DELL|60000|ext")).toBeNull();                 // per series
    ticks(store, "DELL|60000|rth", loaded, 9, 3);                      // 12:15:05 ..: rolls on from the kept state
    expect(hms(withLiveCandle(loaded, store.candle("DELL|60000|rth"))).slice(-4)).toEqual(["12:12", "12:13", "12:14", "12:15"]);
  });

  it("a reload with lagging candles keeps the live minutes; reloaded bars win inside them, live fills what they lack", () => {
    const store = new LiveStore();
    ticks(store, "DELL|60000|rth", loaded, 0, 9);
    const c = store.candle("DELL|60000|rth");
    // reloaded: the source caught up to 11:59, the engine's live-built 12:13 is there too (12:12 it lacks)
    const reloaded = [...loaded, minute("11:58"), minute("11:59"), minute("12:13", { close: 999 })];
    const shown = withLiveCandle(reloaded, c);
    expect(hms(shown)).toEqual([...hms(loaded), "11:58", "11:59", "12:12", "12:13", "12:14"]);
    expect(shown.find((b) => hms([b])[0] === "12:13")?.close).toBe(205);   // the reloaded last bar: the live one replaces it
    const reloadedMore = [...reloaded, minute("12:14", { close: 999 }), minute("12:15", { close: 998 })];
    const later = withLiveCandle(reloadedMore, c);
    expect(later.find((b) => hms([b])[0] === "12:13")?.close).toBe(999);   // inside the reloaded bars: theirs
    // the forming candle (12:14) replaces the loaded bar of its start; nothing newer is dropped
    expect(hms(later).slice(-3)).toEqual(["12:13", "12:14", "12:15"]);
    expect(later.find((b) => hms([b])[0] === "12:14")?.close).toBe(208);
    expect(withLiveCandle([minute("12:13")], c).map((b) => hms([b])[0])).toEqual(["12:13", "12:14"]);   // an older closed one before the range: left out
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

describe("vwapValues", () => {
  it("keeps the stored session VWAP and gives the live bars the quote's", () => {
    const stored: IntradayBar[] = [
      { ts: "2026-11-20T14:30:00Z", open: 1, high: 1, low: 1, close: 1, volume: 1, vwap: 1.0 },
      { ts: "2026-11-20T14:35:00Z", open: 1, high: 1, low: 1, close: 1, volume: 1, vwap: null },
    ];
    const live: IntradayBar = { ts: "2026-11-20T14:40:00Z", open: 1, high: 1, low: 1, close: 1, volume: 1 };
    expect(vwapValues([...stored, live], 1.02)).toEqual([1.0, null, 1.02]);
    expect(vwapValues([...stored, live], null)).toEqual([1.0, null, null]);
  });
});
