// Intraday bars from IBKR for the spans the engine could not fill (INTEGRATION.md §5 "Intraday bars"): the request
// shape the renderer may send, and how a span is cut into reqHistoricalData requests. Pure.
import type { IntradayInterval, IntradayRequest } from "../shared/api";
import type { BarSize, HistBar } from "./tws";

export const INTRADAY_INTERVALS: readonly IntradayInterval[] = ["1m", "5m", "15m", "1h"];
export const BAR_SIZE: Record<IntradayInterval, BarSize> = { "1m": "1 min", "5m": "5 mins", "15m": "15 mins", "1h": "1 hour" };
const DAY_MS = 86_400_000;
/** The longest duration IBKR's "Valid Duration and Bar Size Settings" allow per bar size: 1 day of 1-min bars,
 * a week of 5- or 15-min bars, a month of hourly bars. */
const MAX_SPAN_MS: Record<IntradayInterval, number> = { "1m": DAY_MS, "5m": 7 * DAY_MS, "15m": 7 * DAY_MS, "1h": 30 * DAY_MS };
const MAX_SPANS = 100;
const SYMBOL_RE = /^[A-Za-z0-9.^_-]{1,15}$/;

export interface Chunk { start: number; end: number }

/** A renderer request, or null when malformed (unknown interval, bad times, start ≥ end, too many spans). */
export function parseIntradayRequest(x: unknown): IntradayRequest | null {
  if (!x || typeof x !== "object") return null;
  const r = x as Record<string, unknown>;
  if (typeof r.symbol !== "string" || !SYMBOL_RE.test(r.symbol)) return null;
  if (!INTRADAY_INTERVALS.includes(r.interval as IntradayInterval)) return null;
  if (!Array.isArray(r.spans) || !r.spans.length || r.spans.length > MAX_SPANS) return null;
  const spans: IntradayRequest["spans"] = [];
  for (const s of r.spans as unknown[]) {
    const o = (s ?? {}) as Record<string, unknown>;
    if (typeof o.start !== "string" || typeof o.end !== "string") return null;
    const a = Date.parse(o.start), b = Date.parse(o.end);
    if (!Number.isFinite(a) || !Number.isFinite(b) || a >= b) return null;
    spans.push({ start: o.start, end: o.end });
  }
  return { symbol: r.symbol.toUpperCase(), interval: r.interval as IntradayInterval, spans };
}

/** Request windows covering a span, newest first, each no longer than the bar size allows. */
export function chunks(start: number, end: number, interval: IntradayInterval): Chunk[] {
  const out: Chunk[] = [];
  for (let e = end; e > start; e -= MAX_SPAN_MS[interval]) out.push({ start: Math.max(start, e - MAX_SPAN_MS[interval]), end: e });
  return out;
}

/** reqHistoricalData durationStr of a window: seconds up to a day, else whole days. */
export function durationOf(c: Chunk): string {
  const s = Math.ceil((c.end - c.start) / 1000);
  return s <= 86_400 ? `${Math.max(s, 60)} S` : `${Math.ceil(s / 86_400)} D`;
}

/** reqHistoricalData endDateTime in UTC ("yyyyMMdd-HH:mm:ss"). */
export function endDateTime(ms: number): string {
  const iso = new Date(ms).toISOString();   // 2026-11-17T21:00:00.000Z
  return `${iso.slice(0, 4)}${iso.slice(5, 7)}${iso.slice(8, 10)}-${iso.slice(11, 19)}`;
}

/** A bar's start (formatDate 2: epoch seconds), or null. */
export function barStart(b: HistBar): number | null {
  const s = Number(b.time);
  return /^\d+$/.test(b.time.trim()) && Number.isFinite(s) ? s * 1000 : null;
}
