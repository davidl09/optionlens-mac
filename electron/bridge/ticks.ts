// Market-data tick accumulation. TWS marks "not available" with sentinels (-1 prices, -2 greeks,
// Double.MAX_VALUE) and occasionally sends garbage; every value is sanitized here so nothing downstream
// sees a non-finite or sentinel number. Tick field numbers are IBKR tick types; live/frozen and delayed
// ids map to the same fields, and any delayed tick marks the snapshot `delayed`. Values never received
// stay null (never 0).
import type { TickEvent } from "./tws";

export const TICK = {
  BID: 1, ASK: 2, LAST: 4, HIGH: 6, LOW: 7, VOLUME: 8, CLOSE: 9, OPEN: 14,
  OPEN_INTEREST: 22, OPTION_CALL_OI: 27, OPTION_PUT_OI: 28, OPTION_CALL_VOLUME: 29, OPTION_PUT_VOLUME: 30,
  OPTION_HISTORICAL_VOL: 23, OPTION_IMPLIED_VOL: 24,
  BID_OPTION: 10, ASK_OPTION: 11, LAST_OPTION: 12, MODEL_OPTION: 13,
  DELAYED_BID: 66, DELAYED_ASK: 67, DELAYED_LAST: 68, DELAYED_HIGH: 72, DELAYED_LOW: 73, DELAYED_VOLUME: 74, DELAYED_CLOSE: 75, DELAYED_OPEN: 76,
  DELAYED_BID_OPTION: 80, DELAYED_ASK_OPTION: 81, DELAYED_LAST_OPTION: 82, DELAYED_MODEL_OPTION: 83,
} as const;

const DELAYED_FIELDS = new Set<number>([66, 67, 68, 72, 73, 74, 75, 76, 80, 81, 82, 83]);

const HUGE = 1e12;
const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v) && Math.abs(v) < HUGE;

/** Price ≥ 0 (a 0 bid is real for far OTM options); -1 = no quote. */
export const cleanPrice = (v: unknown): number | null => (finite(v) && v >= 0 ? v : null);
export const cleanSize = (v: unknown): number | null => (finite(v) && v >= 0 ? v : null);
/** Volatility as a fraction; TWS sends -1 when not computed. Above 2000% is garbage. */
export const cleanVol = (v: unknown): number | null => (finite(v) && v > 0 && v <= 20 ? v : null);
export const cleanDelta = (v: unknown): number | null => (finite(v) && v >= -1 && v <= 1 ? v : null);
/** Gamma / vega are non-negative; -2 = not computed. */
export const cleanNonNeg = (v: unknown): number | null => (finite(v) && v >= 0 ? v : null);
/** Theta can be any sign; -2 exactly is TWS's "not computed" sentinel. */
export const cleanTheta = (v: unknown): number | null => (finite(v) && v !== -2 ? v : null);

export interface Greeks { iv: number | null; delta: number | null; gamma: number | null; vega: number | null; theta: number | null }

export interface QuoteSnapshot {
  bid: number | null; ask: number | null; last: number | null;
  open: number | null; high: number | null; low: number | null; close: number | null;
  volume: number | null;
  oi: number | null; callOi: number | null; putOi: number | null;
  callVolume: number | null; putVolume: number | null;
  impliedVol: number | null; historicalVol: number | null;
  /** Model computation (tick 13 / 83), else the first other computation received. */
  greeks: Greeks | null;
  /** At least one delayed tick was received (no real-time subscription for this contract class). */
  delayed: boolean;
}

const PRICE_FIELDS: Record<number, keyof QuoteSnapshot> = {
  [TICK.BID]: "bid", [TICK.ASK]: "ask", [TICK.LAST]: "last", [TICK.HIGH]: "high", [TICK.LOW]: "low", [TICK.CLOSE]: "close", [TICK.OPEN]: "open",
  [TICK.DELAYED_BID]: "bid", [TICK.DELAYED_ASK]: "ask", [TICK.DELAYED_LAST]: "last", [TICK.DELAYED_HIGH]: "high", [TICK.DELAYED_LOW]: "low",
  [TICK.DELAYED_CLOSE]: "close", [TICK.DELAYED_OPEN]: "open",
};
const SIZE_FIELDS: Record<number, keyof QuoteSnapshot> = {
  [TICK.VOLUME]: "volume", [TICK.OPEN_INTEREST]: "oi", [TICK.OPTION_CALL_OI]: "callOi", [TICK.OPTION_PUT_OI]: "putOi",
  [TICK.OPTION_CALL_VOLUME]: "callVolume", [TICK.OPTION_PUT_VOLUME]: "putVolume", [TICK.DELAYED_VOLUME]: "volume",
};
const GENERIC_FIELDS: Record<number, keyof QuoteSnapshot> = { [TICK.OPTION_IMPLIED_VOL]: "impliedVol", [TICK.OPTION_HISTORICAL_VOL]: "historicalVol" };
const COMPUTATION_FIELDS = new Set<number>([TICK.BID_OPTION, TICK.ASK_OPTION, TICK.LAST_OPTION, TICK.MODEL_OPTION,
  TICK.DELAYED_BID_OPTION, TICK.DELAYED_ASK_OPTION, TICK.DELAYED_LAST_OPTION, TICK.DELAYED_MODEL_OPTION]);

export function emptySnapshot(): QuoteSnapshot {
  return { bid: null, ask: null, last: null, open: null, high: null, low: null, close: null, volume: null, oi: null, callOi: null, putOi: null,
    callVolume: null, putVolume: null, impliedVol: null, historicalVol: null, greeks: null, delayed: false };
}

export class TickAccumulator {
  private readonly s: QuoteSnapshot = emptySnapshot();
  private greeksFromModel = false;
  /** Set when TWS rejected the subscription (e.g. no security definition): stop waiting for it. */
  dead = false;

  apply(e: TickEvent): void {
    if (e.kind !== "generic" && DELAYED_FIELDS.has(e.field)) this.s.delayed = true;
    switch (e.kind) {
      case "price": {
        const k = PRICE_FIELDS[e.field];
        if (k) this.set(k, cleanPrice(e.value));
        return;
      }
      case "size": {
        const k = SIZE_FIELDS[e.field];
        if (k) this.set(k, cleanSize(e.value));
        return;
      }
      case "generic": {
        const k = GENERIC_FIELDS[e.field];
        if (k) this.set(k, cleanVol(e.value));
        return;
      }
      case "option": {
        if (!COMPUTATION_FIELDS.has(e.field)) return;
        const isModel = e.field === TICK.MODEL_OPTION || e.field === TICK.DELAYED_MODEL_OPTION;
        if (this.greeksFromModel && !isModel) return;
        const g: Greeks = { iv: cleanVol(e.iv), delta: cleanDelta(e.delta), gamma: cleanNonNeg(e.gamma), vega: cleanNonNeg(e.vega), theta: cleanTheta(e.theta) };
        if (g.iv === null && g.delta === null) return;   // nothing computed yet
        this.s.greeks = g;
        this.greeksFromModel = isModel;
        return;
      }
    }
  }

  /** Keeps the last valid value: a later sentinel (e.g. bid -1 after a real bid) does not erase it. */
  private set(k: keyof QuoteSnapshot, v: number | null): void {
    if (v !== null) (this.s as unknown as Record<string, number | null>)[k] = v;
  }

  snapshot(): QuoteSnapshot {
    return { ...this.s, greeks: this.s.greeks ? { ...this.s.greeks } : null };
  }

  /** Option contract has a two-sided quote and model greeks (OI may lag; not required). */
  optionComplete(): boolean {
    return this.dead || (this.s.bid !== null && this.s.ask !== null && this.greeksFromModel);
  }

  /** Underlying has a current price (last, or a two-sided quote). The previous close alone is not one: it is
   * only the fallback once the subscription timed out (bestPrice). */
  priceComplete(): boolean {
    return this.dead || this.s.last !== null || (this.s.bid !== null && this.s.ask !== null);
  }

  /** Underlying stats: price plus the option-volume / OI / vol generic ticks. */
  statsComplete(): boolean {
    return this.dead || (this.priceComplete() && this.s.callVolume !== null && this.s.callOi !== null && this.s.impliedVol !== null);
  }
}

/** Current price: last, else mid; null when TWS sent neither. */
export function livePrice(q: QuoteSnapshot): number | null {
  if (q.last !== null && q.last > 0) return q.last;
  if (q.bid !== null && q.ask !== null && q.bid > 0 && q.ask >= q.bid) return (q.bid + q.ask) / 2;
  return null;
}

/** Best single price for an underlying: the current price, else the previous close. */
export function bestPrice(q: QuoteSnapshot): number | null {
  return livePrice(q) ?? (q.close !== null && q.close > 0 ? q.close : null);
}

/** Change vs. the previous close (tick 9), only against a current price (never close vs. close). */
export function changeFromClose(q: QuoteSnapshot): { change: number; changePct: number } | null {
  const live = livePrice(q);
  if (live === null || q.close === null || !(q.close > 0)) return null;
  return { change: live - q.close, changePct: live / q.close - 1 };
}
