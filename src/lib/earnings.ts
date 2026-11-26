// Earnings wording shared by the Summary and Earnings tabs (the engine says why a date is missing: Nasdaq for US
// listings, TMX Money / Yahoo Finance for Canadian ones, INTEGRATION.md §12 "Earnings sources").
import type { NextEarnings } from "../api/types";

/** "BMO · in 5d" under the next earnings date. */
export function nextEarningsSub(n: Pick<NextEarnings, "timing" | "days">): string {
  return `${n.timing === "unknown" ? "time n/a" : n.timing.toUpperCase()} · in ${n.days}d`;
}

/** Why there is no upcoming earnings date: the engine's reason, else a plain statement. */
export function noNextEarningsText(sym: string, missing: string | null | undefined): string {
  return missing ?? `No upcoming earnings date for ${sym}.`;
}
