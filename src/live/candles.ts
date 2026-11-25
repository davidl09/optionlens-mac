// The live candle of an intraday chart (pure): live prices aggregated into the current interval's bar, which rolls to
// a new bar at the interval boundary. Volume: the quote's day volume minus its value when the bar began (deltas);
// without day volumes the bar keeps the source's volume (null for a bar the source does not have yet). The candles are
// kept per series in the live store (LiveStore.candle), so a reload or remount of the chart continues them.
import type { IntradayBar } from "../api/types";
import { nyParts } from "../lib/chartRange";

export interface LiveTick { price: number; /** The quote's cumulative day volume. */ volume: number | null; at: number }

export interface LiveCandle {
  start: number; open: number; high: number; low: number; close: number;
  /** The bar's volume before the deltas (the source's, 0 for a new bar), and the day volume deltas count from. */
  baseVolume: number | null; volumeAnchor: number | null;
  /** The last day volume seen (the next bar's anchor). */
  lastVolume: number | null;
  volume: number | null;
  /** Bars this live candle rolled over from (after the stored ones), oldest first. */
  closed: IntradayBar[];
}

const asBar = (c: LiveCandle): IntradayBar => ({ ts: new Date(c.start).toISOString(), open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume });

/** Start of the interval bar holding `at`: on the stored bars' grid within the same New York day, else on the
 * clock grid of New York time (1h bars at whole hours). */
export function bucketStart(at: number, intervalMs: number, last?: IntradayBar): number {
  if (last) {
    const t0 = Date.parse(last.ts);
    const p = nyParts(at), q = nyParts(t0);
    if (at >= t0 && p.month === q.month && p.day === q.day) return t0 + Math.floor((at - t0) / intervalMs) * intervalMs;
  }
  const p = nyParts(at);
  const sinceMidnight = (p.hour * 60 + p.minute) * 60_000 + (at % 60_000);
  return at - (sinceMidnight % intervalMs);
}

/** The live candle after one tick (null state: none yet). A tick before the stored bars' last bar is ignored. */
export function nextCandle(state: LiveCandle | null, tick: LiveTick, intervalMs: number, last?: IntradayBar): LiveCandle | null {
  if (last && tick.at < Date.parse(last.ts)) return state;
  const start = bucketStart(tick.at, intervalMs, last);
  const p = tick.price;
  if (state && state.start === start) {
    const volume = tick.volume !== null && state.volumeAnchor !== null ? (state.baseVolume ?? 0) + Math.max(0, tick.volume - state.volumeAnchor) : state.volume;
    return { ...state, high: Math.max(state.high, p), low: Math.min(state.low, p), close: p, volume, lastVolume: tick.volume ?? state.lastVolume };
  }
  if (state && start < state.start) return state;   // out of order
  const stored = last && Date.parse(last.ts) === start ? last : null;
  const base = stored ? stored.volume : tick.volume !== null ? 0 : null;
  const anchor = tick.volume === null ? null : state?.lastVolume ?? tick.volume;
  const volume = tick.volume !== null && anchor !== null ? (base ?? 0) + Math.max(0, tick.volume - anchor) : base;
  return {
    start, open: stored ? stored.open : p, high: Math.max(stored?.high ?? p, p), low: Math.min(stored?.low ?? p, p), close: p,
    baseVolume: base, volumeAnchor: anchor, lastVolume: tick.volume, volume,
    closed: state ? [...state.closed, asBar(state)].slice(-MAX_CLOSED) : [],
  };
}

/** Closed live candles kept per series (a day of 1m bars is ~1,000). */
export const MAX_CLOSED = 2000;

/**
 * The session VWAP per bar: the loaded bars' (engine: day-cumulative; outside regular hours null, or with extended
 * hours counted from the pre-market), `liveVwap` for the live candles (they carry none), null without it.
 */
export function vwapValues(bars: IntradayBar[], liveVwap: number | null | undefined): (number | null)[] {
  return bars.map((b) => (b.vwap === undefined ? liveVwap ?? null : b.vwap));
}

/**
 * The VWAP for the live candles: the quote's (a regular-session VWAP) on a regular-hours chart; with extended hours
 * the newest loaded bar's, carried forward, since the quote's would leave out the extended-hours volume the line counts.
 */
export function liveVwapFor(bars: IntradayBar[], extended: boolean, quoteVwap: number | null): number | null {
  if (!extended) return quoteVwap;
  for (let i = bars.length - 1; i >= 0; i--) if (bars[i].vwap != null) return bars[i].vwap ?? null;
  return null;
}

/**
 * The loaded bars with the live ones, merged by start: the forming candle replaces the loaded bar of its start; a
 * closed live candle replaces the loaded last bar or follows it, and inside the loaded bars only fills a start they
 * lack (a reloaded bar there is the engine's own: the source's, or one it built from the same live quotes). Closed
 * candles before the first loaded bar are left out (an older range).
 */
export function withLiveCandle(bars: IntradayBar[], c: LiveCandle | null): IntradayBar[] {
  if (!c) return bars;
  const first = bars.length ? Date.parse(bars[0].ts) : -Infinity;
  const last = bars.length ? Date.parse(bars[bars.length - 1].ts) : -Infinity;
  const byStart = new Map(bars.map((b) => [Date.parse(b.ts), b] as const));
  for (const b of c.closed) {
    const t = Date.parse(b.ts);
    if (t >= last || (t >= first && !byStart.has(t))) byStart.set(t, b);
  }
  byStart.set(c.start, asBar(c));
  return [...byStart.entries()].sort((a, b) => a[0] - b[0]).map(([, b]) => b);
}
