// Key stats at a live price (INTEGRATION.md §8 "Reference data"): the engine resolves P/E and market cap at the stored
// price (price / EPS, price × shares, analytics/fundamentals.py); while quotes stream the header applies the same
// formula to the live price. Figures without their inputs (no EPS / shares: e.g. Nasdaq's market cap) stay as served.
import type { KeyStats } from "../api/types";

export function atPrice(ks: KeyStats, price: number | null | undefined): KeyStats {
  if (price == null || !(price > 0)) return ks;
  const pe = ks.eps == null ? ks.pe : ks.eps > 0 ? price / ks.eps : null;
  const cap = ks.shares_outstanding != null && ks.shares_outstanding > 0 ? price * ks.shares_outstanding : ks.market_cap;
  return { ...ks, price, pe, market_cap: cap };
}

/** price / VWAP − 1, null without both. */
export function vsVwap(price: number | null | undefined, vwap: number | null | undefined): number | null {
  return price != null && vwap != null && vwap > 0 ? price / vwap - 1 : null;
}
