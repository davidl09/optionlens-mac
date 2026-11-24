// Pure helpers for result tables (no React).
import type { ColumnDef, ColumnFilter, ReportRow } from "../api/types";
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

// ---------------- table state (sort, column filters, hidden columns) ----------------
// The rule is shared with the engine's MCP server (engine/optionlens/mcp/shaping.py apply_table), which applies the
// user's table state to the rows of the current view; engine/tests/fixtures/table-state-cases.json keeps both equal.
export interface SortState { key: string; dir: "asc" | "desc" }
export interface TableState { sort: SortState | null; filters: Record<string, ColumnFilter>; hidden: string[] }

export function filterActive(f: ColumnFilter | undefined): boolean {
  if (!f) return false;
  return f.min != null || f.max != null || (f.text != null && f.text !== "") || (f.values != null && f.values.length > 0);
}

function passes(v: unknown, f: ColumnFilter): boolean {
  if (f.min != null || f.max != null) {
    const n = typeof v === "number" ? v : Number(v);
    if (isMissing(v) || !Number.isFinite(n)) return false;
    if (f.min != null && n < f.min) return false;
    if (f.max != null && n > f.max) return false;
  }
  if (f.text) {
    if (isMissing(v) || !String(v).toLowerCase().includes(f.text.toLowerCase())) return false;
  }
  if (f.values && f.values.length) {
    if (!f.values.includes(isMissing(v) ? "" : String(v))) return false;
  }
  return true;
}

function compare(a: unknown, b: unknown): number {
  if (typeof a === "number" && typeof b === "number") return a - b;
  if (typeof a === "boolean" && typeof b === "boolean") return Number(a) - Number(b);
  return String(a).localeCompare(String(b), undefined, { numeric: true });
}

export function applyTable(rows: ReportRow[], state: TableState): ReportRow[] {
  const active = Object.entries(state.filters).filter(([, f]) => filterActive(f));
  let out = active.length ? rows.filter((r) => active.every(([k, f]) => passes(r[k], f))) : rows.slice();
  const s = state.sort;
  if (s) {
    const sign = s.dir === "asc" ? 1 : -1;
    out = out.sort((ra, rb) => {
      const a = ra[s.key], b = rb[s.key];
      const ma = isMissing(a), mb = isMissing(b);
      if (ma || mb) return ma && mb ? 0 : ma ? 1 : -1;  // missing always last
      return sign * compare(a, b);
    });
  }
  return out;
}
