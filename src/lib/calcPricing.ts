// The calculator's pricing rule and chart-axis stability (pure). Applied right before every calc request:
//   price(leg) = the user's pinned price ?? the live mid ?? the stored mid   (stock legs: ?? live price ?? stored spot)
//   iv(leg)    = the user's pinned IV    ?? the live IV  ?? the stored IV
// so the engine gets explicit values and never re-derives them, edited legs stay put and the others follow live data.
import type { CalcRequest, Leg } from "../api/types";
import type { LiveFields } from "../live/store";

/** Where a leg's value came from: the user's input (pinned), the live store, or the stored chain / quote. */
export type Origin = "user" | "live" | "stored";

/** A contract's stored values (the chain row of the leg's expiry, right and strike). */
export interface StoredQuote { osi: string | null; mid: number | null; iv: number | null }

export interface MarketInputs {
  /** The stored chain row of an option leg (null: not listed / chain not loaded). */
  stored: (leg: Leg) => StoredQuote | null;
  /** The live fields of a contract while live quotes flow (null: none / not flowing). */
  live: (osi: string) => LiveFields | null;
  /** The live underlying price while live quotes flow, else null. */
  liveSpot: number | null;
  storedSpot: number;
}

export interface PricedLeg {
  /** The leg as sent to the engine: explicit price / iv whenever one is known. */
  leg: Leg;
  priceFrom: Origin | null;
  ivFrom: Origin | null;
  osi: string | null;
  /** Time of the live values used (null: none used). */
  liveTs: string | null;
}

const num = (x: unknown): x is number => typeof x === "number" && Number.isFinite(x);

/** The live mid of a contract: its mid, else the bid/ask midpoint when both are quoted. */
export function liveMid(f: LiveFields | null): number | null {
  if (!f) return null;
  if (num(f.mid)) return f.mid;
  return num(f.bid) && num(f.ask) && f.bid > 0 && f.ask > 0 ? (f.bid + f.ask) / 2 : null;
}

/** The first known value and where it came from. */
function first(user: number | null | undefined, live: number | null | undefined, stored: number | null | undefined): [number | null, Origin | null] {
  if (num(user)) return [user, "user"];
  if (num(live)) return [live, "live"];
  if (num(stored)) return [stored, "stored"];
  return [null, null];
}

export const isPinned = (l: Leg) => num(l.price) || num(l.iv);

/** The leg with its pins dropped: it follows live data again. */
export const unpinned = (l: Leg): Leg => ({ ...l, price: null, iv: null });

export function priceLeg(l: Leg, m: MarketInputs): PricedLeg {
  if (l.kind === "stock") {
    const [price, priceFrom] = first(l.price, m.liveSpot, m.storedSpot);
    return { leg: { ...l, price, iv: null }, priceFrom, ivFrom: null, osi: null, liveTs: null };
  }
  const s = m.stored(l);
  const f = s?.osi ? m.live(s.osi) : null;
  const [price, priceFrom] = first(l.price, liveMid(f), s?.mid);
  const [iv, ivFrom] = first(l.iv, f?.iv, s?.iv);
  const liveTs = priceFrom === "live" || ivFrom === "live" ? f?.ts ?? null : null;
  return { leg: { ...l, price, iv }, priceFrom, ivFrom, osi: s?.osi ?? null, liveTs };
}

export function priceLegs(legs: Leg[], m: MarketInputs): PricedLeg[] {
  return legs.map((l) => priceLeg(l, m));
}

/** The position's shape (what the legs are, not what they cost): a new shape rescales the chart. */
export function structureKey(legs: Leg[]): string {
  return JSON.stringify(legs.map((l) => [l.kind, l.right ?? null, l.strike ?? null, l.expiry ?? null, l.qty]));
}

export type Range = [number, number];

/**
 * The x range to send: the pinned one while the spot stays inside its inner part (`inner` of its span off each end),
 * else none (the engine picks spot ± its default and the caller pins that). Keeps the x axis and table rows still
 * while the live spot moves.
 */
export function keepPriceRange(pinned: Range | null, spot: number, inner = 0.1): Range | null {
  if (!pinned) return null;
  const pad = (pinned[1] - pinned[0]) * inner;
  return spot >= pinned[0] + pad && spot <= pinned[1] - pad ? pinned : null;
}

/** [min, max] of the given series (null when empty / not finite). */
export function extent(...series: number[][]): Range | null {
  let lo = Infinity, hi = -Infinity;
  for (const s of series) for (const v of s) if (Number.isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  return lo <= hi ? [lo, hi] : null;
}

/**
 * A y range with `headroom` (10%) of the curve's span above and below. While the curve stays inside the previous range
 * it is kept (no jumping on live ticks); once the curve leaves it the range is rebuilt around the curve (bounds rounded
 * outwards to a readable step).
 */
export function stableRange(prev: Range | null, data: Range, headroom = 0.1): Range {
  if (prev && data[0] >= prev[0] && data[1] <= prev[1]) return prev;
  const span = data[1] - data[0] || Math.max(1, Math.abs(data[0]));
  const lo = data[0] - span * headroom, hi = data[1] + span * headroom;
  const step = 10 ** Math.floor(Math.log10((hi - lo) / 5));   // round outwards to a readable axis bound
  return [Math.floor(lo / step) * step, Math.ceil(hi / step) * step];
}

/** The calc request of priced legs (explicit per-leg price / iv and spot). */
export function calcRequest(symbol: string, priced: PricedLeg[], o: { spot: number; ivShift: number; evalDate: string; priceRange: Range | null }): CalcRequest {
  return {
    symbol, legs: priced.map((p) => p.leg), spot: o.spot, iv_shift: o.ivShift, eval_date: o.evalDate,
    ...(o.priceRange ? { price_range: o.priceRange } : {}),
  };
}
