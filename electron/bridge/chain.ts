// Option-chain planning and assembly (pure): which underlying contract, which expiries and strikes,
// and how accumulated ticks become the POST /api/ingest/chain body (INTEGRATION.md §6).
import type { IngestChain, IngestContract, IngestUnderlying } from "./ingest";
import { nyYmd } from "./ny";
import { bestPrice, type QuoteSnapshot } from "./ticks";
import type { OptionParams, TwsContract } from "./tws";

/** Cash-settled indices (and the VIX family) and their listing exchange at IBKR. */
const INDEX_EXCHANGE: Record<string, string> = {
  SPX: "CBOE", XSP: "CBOE", NDX: "NASDAQ", RUT: "RUSSELL", VIX: "CBOE", VIX9D: "CBOE", VIX3M: "CBOE", VIX6M: "CBOE", DJX: "CBOE",
};

export function underlyingContract(symbol: string): TwsContract {
  const sym = symbol.toUpperCase();
  const idx = INDEX_EXCHANGE[sym];
  if (idx) return { symbol: sym, secType: "IND", exchange: idx, currency: "USD" };
  // IBKR writes share classes with a space (BRK B); the engine uses BRK.B.
  return { symbol: sym.replace(".", " "), secType: "STK", exchange: "SMART", currency: "USD" };
}

export interface ChainShape { expiries: number; moneyness: number }
/** On-demand refresh and closing (EOD) chains: first 8 expiries, strikes within ±25% of spot. */
export const FULL_CHAIN: ChainShape = { expiries: 8, moneyness: 0.25 };

export interface ExpiryPick { expiry: string; tradingClass: string; multiplier: string }

/**
 * First `count` expiries on or after `now`'s New York date (YYYYMMDD compare) from the SMART listing. When an expiry is
 * listed under several trading classes (SPX monthly AM vs SPXW PM) the class named like the symbol wins.
 */
export function pickExpiries(params: OptionParams[], symbol: string, now: Date, count: number): ExpiryPick[] {
  const smart = params.filter((p) => p.exchange === "SMART");
  const rows = smart.length ? smart : params;
  const todayKey = nyYmd(now);
  const best = new Map<string, ExpiryPick>();
  for (const p of rows) {
    for (const e of p.expirations) {
      if (!/^\d{8}$/.test(e) || e < todayKey) continue;
      const cur = best.get(e);
      if (!cur || (p.tradingClass === symbol && cur.tradingClass !== symbol)) best.set(e, { expiry: e, tradingClass: p.tradingClass, multiplier: p.multiplier });
    }
  }
  return [...best.values()].sort((a, b) => a.expiry.localeCompare(b.expiry)).slice(0, count);
}

export function inMoneyness(strike: number, spot: number, moneyness: number): boolean {
  return strike >= spot * (1 - moneyness) && strike <= spot * (1 + moneyness);
}

/** Option contracts (from contract details) within the strike band, sorted by expiry, strike, right. */
export function selectContracts(all: TwsContract[], spot: number, moneyness: number): TwsContract[] {
  const seen = new Set<string>();
  return all
    .filter((c) => c.secType === "OPT" && c.strike !== undefined && c.right !== undefined && c.lastTradeDateOrContractMonth !== undefined && inMoneyness(c.strike, spot, moneyness))
    .filter((c) => { const k = contractKey(c); if (seen.has(k)) return false; seen.add(k); return true; })
    .sort((a, b) => contractKey(a).localeCompare(contractKey(b)));
}

export function contractKey(c: TwsContract): string {
  return `${c.lastTradeDateOrContractMonth ?? ""}|${String(c.strike ?? 0).padStart(12, "0")}|${c.right ?? ""}`;
}

/** YYYYMMDD → YYYY-MM-DD (TWS sometimes appends a time; only the date part is used). */
export function isoDate(twsDate: string): string | null {
  const m = /^(\d{4})(\d{2})(\d{2})/.exec(twsDate.trim());
  return m ? `${m[1]}-${m[2]}-${m[3]}` : null;
}

/** `rthClose`: the regular-session close, sent with closing chains (the engine archives with it). */
export function assembleUnderlying(q: QuoteSnapshot, ts: string, rthClose: number | null = null): IngestUnderlying | null {
  const price = bestPrice(q) ?? rthClose;
  if (price === null) return null;
  return { price, bid: q.bid, ask: q.ask, open: q.open, high: q.high, low: q.low, prev_close: q.close, volume: q.volume, ts, rth_close: rthClose };
}

/** One contract row, or null when TWS gave nothing usable (no quote, no IV). */
export function assembleContract(c: TwsContract, q: QuoteSnapshot): IngestContract | null {
  const expiry = isoDate(c.lastTradeDateOrContractMonth ?? "");
  if (!expiry || c.strike === undefined || !(c.strike > 0) || (c.right !== "C" && c.right !== "P")) return null;
  const g = q.greeks;
  if (q.bid === null && q.ask === null && q.last === null && (g?.iv ?? null) === null) return null;
  const oi = (c.right === "C" ? q.callOi : q.putOi) ?? q.oi;
  return {
    expiry, strike: c.strike, right: c.right, bid: q.bid, ask: q.ask, last: q.last, volume: q.volume, oi,
    iv: g?.iv ?? null, delta: g?.delta ?? null, gamma: g?.gamma ?? null, theta: g?.theta ?? null, vega: g?.vega ?? null,
  };
}

export interface ChainMeta { symbol: string; fetchedAt: string; final: boolean; sessionDate: string | null; underlyingDelayed: boolean }

export function assembleChain(meta: ChainMeta, underlying: IngestUnderlying, quotes: { contract: TwsContract; quote: QuoteSnapshot }[]): IngestChain {
  if (meta.final && !meta.sessionDate) throw new Error("a final chain needs its session date");
  const contracts = quotes.map((x) => assembleContract(x.contract, x.quote)).filter((c): c is IngestContract => c !== null);
  return {
    source: "ibkr", symbol: meta.symbol, fetched_at: meta.fetchedAt, final: meta.final,
    ...(meta.final && meta.sessionDate ? { session_date: meta.sessionDate } : {}),
    delayed: meta.underlyingDelayed || quotes.some((x) => x.quote.delayed),
    underlying, contracts,
  };
}
