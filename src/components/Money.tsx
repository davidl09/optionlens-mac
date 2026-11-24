// The currency money values are shown in (format.ts MoneyStyle): a Canadian symbol's pages are in C$, a table
// holding both currencies labels US$ and C$ per row. Values are never converted.
import { createContext, ReactNode, useContext } from "react";
import type { Currency } from "../api/types";
import { fmt, USD_STYLE, type MoneyStyle } from "../lib/format";

const MoneyContext = createContext<MoneyStyle>(USD_STYLE);

export function MoneyScope({ currency, explicit, children }: { currency: Currency; explicit?: boolean; children: ReactNode }) {
  return <MoneyContext.Provider value={{ currency, explicit: explicit ?? currency !== "USD" }}>{children}</MoneyContext.Provider>;
}

export function useMoney(): MoneyStyle {
  return useContext(MoneyContext);
}

/** A row's own currency (report rows carry `currency`) within the table's style. */
export function rowMoney(base: MoneyStyle, row: Record<string, unknown>): MoneyStyle {
  return row.currency === "CAD" || row.currency === "USD" ? { currency: row.currency, explicit: base.explicit } : base;
}

/** Whether rows hold more than one currency (then USD is labelled US$). */
export function mixedCurrencies(rows: Record<string, unknown>[]): boolean {
  const seen = new Set(rows.map((r) => r.currency).filter((c) => c === "CAD" || c === "USD"));
  return seen.size > 1;
}

/** A money value in the surrounding currency. */
export function MoneyText({ value }: { value: unknown }) {
  return <>{fmt(value, "money", useMoney())}</>;
}
