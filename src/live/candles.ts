// The live candle of an intraday chart (pure): live prices aggregated into the current interval's bar, which rolls to
// a new bar at the interval boundary. Volume: the quote's day volume minus its value when the bar began (deltas);
// without day volumes the bar keeps the source's volume (null for a bar the source does not have yet).
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
    baseVolume: base, volumeAnchor: anchor, lastVolume: tick.volume, volume, closed: state ? [...state.closed, asBar(state)] : [],
  };
}

/** The stored bars with the live ones (rolled-over bars, then the current one): each replaces the stored bar of
 * its start or follows the last one. */
export function withLiveCandle(bars: IntradayBar[], c: LiveCandle | null): IntradayBar[] {
  if (!c) return bars;
  const out = [...bars];
  for (const bar of [...c.closed, asBar(c)]) {
    const t = Date.parse(bar.ts), last = out[out.length - 1];
    if (last && Date.parse(last.ts) === t) out[out.length - 1] = bar;
    else if (!last || Date.parse(last.ts) < t) out.push(bar);
  }
  return out;
}
