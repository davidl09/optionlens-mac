// The holes an intraday chart marks instead of collapsing them (INTEGRATION.md §5 "Intraday bars", the engine's
// `gaps`): where they go among the bars, how many empty slots they take and what they say. Pure.
import type { IntradayBars, IntradayGap, IntradayRange } from "../api/types";
import { barLabel, hhmm, nyParts } from "./chartRange";
import { sourceLabel } from "./refresh";

/** A gap takes one empty slot per missing bar, at most this many (a long one stays readable without crowding). */
export const MAX_GAP_SLOTS = 30;

/** A category of the chart: a bar (its index in the bars) or an empty slot of a gap (its start). */
export type Slot = { bar: number } | { gap: number; ts: string };

/** A gap's slots `from`..`to` (inclusive slot indexes), its sentence and its short label. */
export interface GapBand { from: number; to: number; text: string; short: string }

export interface GapLayout { slots: Slot[]; bands: GapBand[] }

/** "12:02–12:15" (the missing bars' starts, New York time); with the weekday / date beyond 1D. */
export function gapTimes(g: IntradayGap, intervalMs: number, range: IntradayRange): string {
  const first = Date.parse(g.start);
  const last = Math.max(first, Date.parse(g.end) - intervalMs);
  const a = barLabel(new Date(first).toISOString(), range);
  if (last === first) return a;
  const p = nyParts(last), q = nyParts(first);
  const sameDay = p.month === q.month && p.day === q.day;
  return `${a}–${sameDay ? hhmm(p) : barLabel(new Date(last).toISOString(), range)}`;
}

/** What a gap says: `text` for the legend and tooltip, `short` on the chart. */
export function gapText(g: IntradayGap, d: Pick<IntradayBars, "interval" | "range" | "sources" | "lag_s">, intervalMs: number): { text: string; short: string } {
  const times = gapTimes(g, intervalMs, d.range);
  if (g.kind === "missing") return { text: `${times} · no bars from any source yet`, short: `${times} · no bars` };
  const source = d.sources.find((s) => s !== "live");
  const late = d.lag_s ? `~${Math.max(1, Math.round(d.lag_s / 60))} min late` : "late";
  return {
    text: `${times} · ${source ? `${sourceLabel(source)} candles` : "Candles"} arrive ${late} — filling in`,
    short: `${times} · candles ${late}`,
  };
}

/**
 * The chart's categories: the bars in order with each gap's empty slots before the first bar at or after its start
 * (bars and gaps ascending; the engine's gaps hold no bar). `describe` gives each gap's sentence and label.
 */
export function layoutGaps(bars: readonly { ts: string }[], gaps: readonly IntradayGap[], intervalMs: number,
  describe: (g: IntradayGap) => { text: string; short: string }): GapLayout {
  const slots: Slot[] = [];
  const bands: GapBand[] = [];
  let next = 0;
  const place = (gi: number) => {
    const g = gaps[gi];
    const a = Date.parse(g.start), span = Date.parse(g.end) - a;
    const n = Math.min(MAX_GAP_SLOTS, Math.max(1, Math.round(span / intervalMs)));
    const from = slots.length;
    for (let k = 0; k < n; k++) slots.push({ gap: gi, ts: new Date(a + (k * span) / n).toISOString() });
    bands.push({ from, to: slots.length - 1, ...describe(g) });
  };
  bars.forEach((b, i) => {
    const t = Date.parse(b.ts);
    while (next < gaps.length && Date.parse(gaps[next].start) <= t) place(next++);
    slots.push({ bar: i });
  });
  while (next < gaps.length) place(next++);
  return { slots, bands };
}

/** Per-bar values spread over the slots (null in a gap's slots). */
export function spread<T>(slots: readonly Slot[], values: readonly T[]): (T | null)[] {
  return slots.map((s) => ("bar" in s ? values[s.bar] ?? null : null));
}
