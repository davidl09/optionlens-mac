// Pure helpers for result tables (no React).
import type { ColumnDef, ReportRow } from "../api/types";
import { isMissing } from "./format";

/** Keys of the columns that have no value in any row (none when there are no rows: nothing to judge by). */
export function emptyColumnKeys(columns: ColumnDef[], rows: ReportRow[]): Set<string> {
  if (!rows.length) return new Set();
  return new Set(columns.filter((c) => rows.every((r) => isMissing(r[c.key]))).map((c) => c.key));
}

const ACRONYMS = new Set(["iv", "hv", "oi", "dte", "eps", "pc", "pe", "atm", "otm", "itm", "sma", "rsi", "atr", "ytd", "ev", "pop"]);
const SPECIAL: Record<string, string> = { pc: "P/C" };

/** "pc_history" → "P/C history", "iv30_chg" → "IV30 chg": a readable label for a payload key. */
export function humanize(key: string): string {
  const words = key.split("_").map((w) => SPECIAL[w] ?? (ACRONYMS.has(w) || /^(iv|hv)\d+$/.test(w) ? w.toUpperCase() : w));
  const s = words.join(" ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}
