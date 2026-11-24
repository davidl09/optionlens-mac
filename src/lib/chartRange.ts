// Price chart ranges and bar intervals (the Chart tab): which intervals each range offers, New York time labels and
// the SMA of an intraday series. Pure.
import type { IntradayBar, IntradayInterval, IntradayRange } from "../api/types";

export const CHART_RANGES = ["1d", "5d", "1m", "3m", "6m", "1y", "3y", "5y", "10y"] as const;
export type ChartRange = (typeof CHART_RANGES)[number];
/** "1d": daily bars. */
export type ChartInterval = IntradayInterval | "1d";

/** The intervals a range offers, its default first (the engine offers the same intraday combinations,
 * analytics/intraday.py RANGE_INTERVALS); ranges from 6M on are daily only. */
export const RANGE_INTERVALS: Record<ChartRange, readonly ChartInterval[]> = {
  "1d": ["1m", "5m", "15m"], "5d": ["5m", "15m", "1h"], "1m": ["15m", "1h", "1d"], "3m": ["1h", "1d"],
  "6m": ["1d"], "1y": ["1d"], "3y": ["1d"], "5y": ["1d"], "10y": ["1d"],
};

export const INTERVAL_MS: Record<IntradayInterval, number> = { "1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000 };
export const INTERVAL_LABEL: Record<ChartInterval, string> = { "1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "1d": "D" };

export type ChartView =
  | { kind: "daily"; range: ChartRange }
  | { kind: "intraday"; range: IntradayRange; interval: IntradayInterval };

/** What to show for the URL's range / interval (unknown values fall back to 1Y daily / the range's default). */
export function chartView(range: string | null, interval: string | null): ChartView {
  const r = (CHART_RANGES as readonly string[]).includes(range ?? "") ? (range as ChartRange) : "1y";
  const offered = RANGE_INTERVALS[r];
  const i = offered.includes(interval as ChartInterval) ? (interval as ChartInterval) : offered[0];
  return i === "1d" ? { kind: "daily", range: r } : { kind: "intraday", range: r as IntradayRange, interval: i };
}

const NY_PARTS = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", weekday: "short", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});

/** New York wall-clock parts of an instant. */
export function nyParts(ms: number): { weekday: string; month: string; day: string; hour: number; minute: number } {
  const p = Object.fromEntries(NY_PARTS.formatToParts(new Date(ms)).map((x) => [x.type, x.value]));
  return { weekday: p.weekday, month: p.month, day: p.day, hour: Number(p.hour), minute: Number(p.minute) };
}

const hhmm = (p: ReturnType<typeof nyParts>) => `${String(p.hour).padStart(2, "0")}:${String(p.minute).padStart(2, "0")}`;

/** Axis label of a bar start (New York time): the time on 1D, with the weekday on 5D, with the date beyond. */
export function barLabel(ts: string, range: IntradayRange): string {
  const p = nyParts(Date.parse(ts));
  if (range === "1d") return hhmm(p);
  if (range === "5d") return `${p.weekday} ${hhmm(p)}`;
  return `${p.month}-${p.day} ${hhmm(p)}`;
}

/** Tooltip title of a bar start: "Tue 11-17 09:30 ET". */
export function barTitle(ts: string): string {
  const p = nyParts(Date.parse(ts));
  return `${p.weekday} ${p.month}-${p.day} ${hhmm(p)} ET`;
}

/** Whether an instant is in the regular session (weekday 09:30–16:00 ET; exchange holidays and early closes are the
 * engine's business: no live ticks arrive then). */
export function inRegularHours(ms: number): boolean {
  const p = nyParts(ms);
  const m = p.hour * 60 + p.minute;
  return p.weekday !== "Sat" && p.weekday !== "Sun" && m >= 570 && m < 960;
}

/** Simple moving average of the closes over the series itself (null until `n` bars). */
export function smaOf(bars: Pick<IntradayBar, "close">[], n: number): (number | null)[] {
  let sum = 0;
  return bars.map((b, i) => {
    sum += b.close;
    if (i >= n) sum -= bars[i - n].close;
    return i + 1 >= n ? sum / n : null;
  });
}
