import { describe, expect, it } from "vitest";
import type { IntradayGap } from "../api/types";
import { gapText, gapTimes, layoutGaps, MAX_GAP_SLOTS, spread } from "./chartGaps";

const M = 60_000;
const et = (hm: string, day = "2026-11-17") => new Date(Date.parse(`${day}T${hm}:00-05:00`)).toISOString();   // EST
const minutes = (from: string, to: string) => {
  const out: { ts: string }[] = [];
  for (let t = Date.parse(et(from)); t <= Date.parse(et(to)); t += M) out.push({ ts: new Date(t).toISOString() });
  return out;
};
const lag: IntradayGap = { start: et("12:02"), end: et("12:16"), kind: "lag" };
const D = { interval: "1m", range: "1d", sources: ["questrade", "live"], lag_s: 957 } as const;

describe("chart gaps", () => {
  it("says which minutes are missing and why", () => {
    expect(gapTimes(lag, M, "1d")).toBe("12:02–12:15");
    expect(gapText(lag, { ...D, sources: [...D.sources] }, M)).toEqual({
      text: "12:02–12:15 · Questrade candles arrive ~16 min late — filling in", short: "12:02–12:15 · candles ~16 min late",
    });
    expect(gapText({ ...lag, kind: "missing" }, { ...D, sources: [] }, M).text).toBe("12:02–12:15 · no bars from any source yet");
    expect(gapTimes({ start: et("12:00"), end: et("12:15"), kind: "lag" }, 15 * M, "1d")).toBe("12:00");   // one 15m bar
    expect(gapTimes({ start: et("15:30", "2026-11-16"), end: et("10:00"), kind: "missing" }, 5 * M, "5d")).toBe("Mon 15:30–Tue 09:55");
    expect(gapText(lag, { ...D, sources: ["questrade"], lag_s: null }, M).short).toBe("12:02–12:15 · candles late");
  });

  it("puts one empty slot per missing bar between the bars around the hole", () => {
    const bars = [...minutes("11:58", "12:01"), ...minutes("12:16", "12:17")];
    const { slots, bands } = layoutGaps(bars, [lag], M, () => ({ text: "t", short: "s" }));
    expect(slots).toHaveLength(bars.length + 14);
    expect(slots.slice(0, 4)).toEqual([{ bar: 0 }, { bar: 1 }, { bar: 2 }, { bar: 3 }]);
    expect(slots[4]).toEqual({ gap: 0, ts: et("12:02") });
    expect(slots[17]).toEqual({ gap: 0, ts: et("12:15") });
    expect(slots[18]).toEqual({ bar: 4 });
    expect(bands).toEqual([{ from: 4, to: 17, text: "t", short: "s" }]);
    expect(spread(slots, bars.map((_, i) => i)).slice(2, 6)).toEqual([2, 3, null, null]);
  });

  it("places a gap at the end when no bar follows yet, and caps long ones", () => {
    const bars = minutes("11:58", "12:01");
    const open: IntradayGap = { start: et("12:02"), end: et("12:17"), kind: "lag" };
    expect(layoutGaps(bars, [open], M, () => ({ text: "", short: "" })).bands[0]).toMatchObject({ from: 4, to: 18 });
    const long: IntradayGap = { start: et("09:30"), end: et("11:58"), kind: "missing" };
    const { slots, bands } = layoutGaps(bars, [long], M, () => ({ text: "", short: "" }));
    expect(bands[0]).toMatchObject({ from: 0, to: MAX_GAP_SLOTS - 1 });
    expect(slots[MAX_GAP_SLOTS]).toEqual({ bar: 0 });
    expect(layoutGaps(bars, [], M, () => ({ text: "", short: "" })).slots).toEqual(bars.map((_, i) => ({ bar: i })));
  });
});
