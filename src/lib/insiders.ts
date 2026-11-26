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

export interface InsiderText { summary: string; noSummary: string; trades?: string; noTrades: string }

/** How the Insiders tab words each source's data (US listings: Nasdaq; Canadian listings: SEDI reports from TMX Money). */
export function insiderText(source: Insiders["source"]): InsiderText {
  if (source === "tmx") {
    return {
      summary: "TMX Money's totals of insider trading",
      noSummary: "No insider trading totals for this symbol.",
      trades: "SEDI insider reports of the last 12 months (latest 200), without the company's own buybacks · negative shares = disposed",
      noTrades: "No insider reports were filed for this symbol in the last 12 months.",
    };
  }
  return {
    summary: "Nasdaq counts every kind of sale together",
    noSummary: "Nasdaq has no insider summary for this symbol.",
    noTrades: "No insider transactions reported to Nasdaq for this symbol.",
  };
}
