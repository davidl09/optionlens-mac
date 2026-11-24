// Live patches merged into the API's shapes (pure): a Quote, an OptionRow, a report row cell, the last daily bar.
// History-based values (IV rank, HV, backtests, returns) are never touched.
import type { Bar, OptionRow, ReportRow } from "../api/types";
import type { LiveFields } from "./store";

const has = (f: LiveFields, k: keyof LiveFields) => f[k] !== undefined && f[k] !== null;

export function withLiveQuote<T extends object>(q: T, f: LiveFields | null): T {
  if (!f) return q;
  const out = { ...q } as Record<string, unknown>;
  for (const k of ["price", "change", "change_pct", "bid", "ask", "open", "high", "low", "volume", "vwap", "halted"] as const) if (has(f, k)) out[k] = f[k];
  if (f.ts) out.ts = f.ts;
  if (f.source) out.source = f.source;
  if (f.delayed !== undefined) out.delayed = f.delayed;
  if (f.ts) out.after_hours = false;
  return out as T;
}

/** An option row with its live fields; `spot` (the live underlying) re-decides in/out of the money. */
export function withLiveOption(row: OptionRow, f: LiveFields | null, spot?: number | null): OptionRow {
  if (!f && spot == null) return row;
  const out: OptionRow = { ...row };
  if (f) {
    for (const k of ["bid", "ask", "last", "volume", "oi", "iv", "delta", "gamma", "theta", "vega"] as const) if (has(f, k)) out[k] = f[k] as number;
    out.mid = has(f, "mid") ? (f.mid as number) : out.bid !== null && out.ask !== null ? (out.bid + out.ask) / 2 : out.mid;
    if (has(f, "iv") && f.source) out.iv_source = f.source;
    if (has(f, "delta") && f.source) out.greeks_source = f.source;
  }
  if (spot != null) out.itm = row.right === "C" ? row.strike < spot : row.strike > spot;
  return out;
}

/** Report columns a live quote can update: of the row's underlying, or of its contract for rows with an `osi`. */
const QUOTE_COLUMNS = new Set(["price", "change", "change_pct", "bid", "ask", "volume", "open", "high", "low", "vwap", "halted"]);
const UNDERLYING_COLUMNS = new Set(["price", "change", "change_pct"]);
const OPTION_COLUMNS = new Set(["bid", "ask", "mid", "last", "volume", "oi", "iv", "delta", "gamma", "theta", "vega"]);

/** Which live key feeds a report cell: ["o", osi] for contract fields of rows with an `osi`, ["q", symbol] for quote
 * fields, null when the column is not a live one (history-based metrics, descriptions…). */
export function liveKeyOf(row: ReportRow, column: string): ["q" | "o", string] | null {
  const osi = typeof row.osi === "string" && row.osi ? row.osi : null;
  const sym = typeof row.symbol === "string" && row.symbol ? row.symbol : null;
  if (osi) {
    if (OPTION_COLUMNS.has(column)) return ["o", osi];
    return UNDERLYING_COLUMNS.has(column) && sym ? ["q", sym] : null;
  }
  return QUOTE_COLUMNS.has(column) && sym ? ["q", sym] : null;
}

/** The symbols and contracts of these rows (the visible page of a table). */
export function rowKeys(rows: ReportRow[]): { symbols: string[]; contracts: string[] } {
  const symbols = new Set<string>(), contracts = new Set<string>();
  for (const r of rows) {
    if (typeof r.symbol === "string" && r.symbol) symbols.add(r.symbol);
    if (typeof r.osi === "string" && r.osi) contracts.add(r.osi);
  }
  return { symbols: [...symbols], contracts: [...contracts] };
}

/** Daily bars with today's bar updated from the live quote (or appended when the last bar is an earlier session). */
export function withLiveBar(bars: Bar[], f: LiveFields | null, today: string): Bar[] {
  if (!f || !has(f, "price") || !bars.length) return bars;
  const price = f.price as number;
  const last = bars[bars.length - 1];
  if (last.date > today) return bars;
  if (last.date === today) {
    const b: Bar = { ...last, close: price, high: Math.max(last.high, f.high ?? price, price), low: Math.min(last.low, f.low ?? price, price), volume: f.volume ?? last.volume };
    return [...bars.slice(0, -1), b];
  }
  const open = f.open ?? price;
  return [...bars, { date: today, open, high: Math.max(open, f.high ?? price, price), low: Math.min(open, f.low ?? price, price), close: price, volume: f.volume ?? 0 }];
}
