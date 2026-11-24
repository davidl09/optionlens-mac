// Markets followed (SPEC §3 "Symbols"; mirrors the engine's analytics/markets.py, whose API fields
// `market` / `exchange` / `currency` are authoritative): US listings are bare tickers, Canadian ones carry
// Questrade's exchange suffix (.TO = TSX, .VN = TSX Venture). A symbol's market fixes its currency.
import type { Currency, Market } from "../api/types";
import type { MoneyStyle } from "./format";

const CA_SUFFIXES: Record<string, "TSX" | "TSXV"> = { ".TO": "TSX", ".VN": "TSXV" };

function suffix(symbol: string): string | null {
  const s = symbol.toUpperCase();
  return Object.keys(CA_SUFFIXES).find((x) => s.endsWith(x) && s.length > x.length) ?? null;
}

export function marketOf(symbol: string): Market {
  return suffix(symbol) ? "CA" : "US";
}

export function currencyOf(symbol: string): Currency {
  return marketOf(symbol) === "CA" ? "CAD" : "USD";
}

export function exchangeOf(symbol: string): "TSX" | "TSXV" | null {
  const s = suffix(symbol);
  return s ? CA_SUFFIXES[s] : null;
}

/** The IBKR bridge serves US listings only (INTEGRATION.md §6); Canadian ones come from Questrade. */
export function bridgeServes(symbol: string): boolean {
  return marketOf(symbol) === "US";
}

/** How a symbol's money values are labelled (format.ts): C$ for Canadian listings, plain $ for US ones. */
export function moneyStyleOf(symbol: string): MoneyStyle {
  const currency = currencyOf(symbol);
  return { currency, explicit: currency !== "USD" };
}
