// Insider sells by kind, from the listed transactions (the source's summary counts every sell together).
import type { Insiders } from "../api/types";

export interface SellSplit { automatic: number; openMarket: number; total: number }

/** "Automatic Sell" = under a pre-arranged 10b5-1 trading plan; "Sell" = an open-market sale. */
export function sellSplit(trades: Insiders["trades"]): SellSplit {
  const sells = trades.filter((t) => /\bsell\b/i.test(t.transaction ?? ""));
  const automatic = sells.filter((t) => /automatic/i.test(t.transaction ?? "")).length;
  const openMarket = sells.filter((t) => /^\s*sell\s*$/i.test(t.transaction ?? "")).length;
  return { automatic, openMarket, total: sells.length };
}
