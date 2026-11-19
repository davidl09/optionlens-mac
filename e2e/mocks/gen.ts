// Deterministic fixture helpers (no Math.random: screenshots stay stable).
import type { ColumnDef, ReportRow } from "../../src/api/types";

export const TODAY = "2026-11-19";
export const NOW = "2026-11-19T15:45:00-05:00";

let seed = 42;
export function reseed(s: number) { seed = s; }
export function rnd(): number {
  seed = (seed * 1664525 + 1013904223) % 4294967296;
  return seed / 4294967296;
}
export const between = (a: number, b: number) => a + (b - a) * rnd();
export const pick = <T>(xs: T[]): T => xs[Math.floor(rnd() * xs.length)];
export const round = (v: number, d = 2) => Math.round(v * 10 ** d) / 10 ** d;

export function addDays(iso: string, n: number): string {
  const d = new Date(`${iso}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

/** Weekday dates going back from TODAY, oldest first. */
export function tradingDays(n: number, end = TODAY): string[] {
  const out: string[] = [];
  let d = end;
  while (out.length < n) {
    const wd = new Date(`${d}T12:00:00Z`).getUTCDay();
    if (wd !== 0 && wd !== 6) out.push(d);
    d = addDays(d, -1);
  }
  return out.reverse();
}

export function walk(n: number, start: number, vol = 0.015): number[] {
  const out = [start];
  for (let i = 1; i < n; i++) out.push(round(out[i - 1] * (1 + (rnd() - 0.48) * vol * 2), 2));
  return out;
}

export const SYMBOLS: [string, string, string][] = [
  ["AAPL", "Apple Inc.", "Technology"], ["MSFT", "Microsoft Corp.", "Technology"], ["NVDA", "NVIDIA Corp.", "Technology"],
  ["AMZN", "Amazon.com Inc.", "Consumer Discretionary"], ["META", "Meta Platforms", "Communication Services"], ["TSLA", "Tesla Inc.", "Consumer Discretionary"],
  ["JPM", "JPMorgan Chase", "Financials"], ["XOM", "Exxon Mobil", "Energy"], ["UNH", "UnitedHealth Group", "Health Care"],
  ["KO", "Coca-Cola Co.", "Consumer Staples"], ["AMD", "Advanced Micro Devices", "Technology"], ["NFLX", "Netflix Inc.", "Communication Services"],
  ["BAC", "Bank of America", "Financials"], ["PFE", "Pfizer Inc.", "Health Care"], ["DIS", "Walt Disney Co.", "Communication Services"],
  ["INTC", "Intel Corp.", "Technology"], ["CVX", "Chevron Corp.", "Energy"], ["WMT", "Walmart Inc.", "Consumer Staples"],
];

function valueFor(c: ColumnDef, i: number): unknown {
  const k = c.key;
  if (rnd() < 0.03 && c.fmt !== "symbol") return null; // sprinkle nulls: UI must show "—"
  switch (c.fmt) {
    case "symbol": return SYMBOLS[i % SYMBOLS.length][0];
    case "text":
      if (k === "name") return SYMBOLS[i % SYMBOLS.length][1];
      if (k === "sector") return SYMBOLS[i % SYMBOLS.length][2];
      if (k === "legs") return `-1 P ${round(between(90, 200), 0)} / +1 P ${round(between(80, 190), 0)} 2026-12-18`;
      if (k === "time") return pick(["BMO", "AMC"]);
      if (k === "type") return pick(["stock", "etf"]);
      return pick(["Software", "Semiconductors", "Banks", "Oil & Gas", "Retail"]);
    case "badge":
      if (k === "pricing") return pick(["real", "real", "model"]);
      if (k === "right") return pick(["C", "P"]);
      return pick(["Bullish", "Bearish", "Neutral"]);
    case "int": return Math.round(between(100, 250000));
    case "num": return round(between(1, 300), 2);
    case "num2": return round(between(-2, 5), 2);
    case "money": return round(between(5, 600), 2);
    case "bigmoney": return round(between(5e8, 3e12), 0);
    case "pct": return round(between(0, 1), 4);
    case "pct_signed": return round(between(-0.12, 0.12), 4);
    case "vol": return round(between(0.12, 0.9), 4);
    case "ratio": return round(between(0.3, 6), 2);
    case "date": return addDays(TODAY, Math.round(between(-20, 40)));
    case "datetime": return NOW;
    case "bool": return rnd() > 0.5;
    case "osi": {
      const sym = SYMBOLS[i % SYMBOLS.length][0];
      return `${sym}261218${pick(["C", "P"])}00${String(Math.round(between(50, 400))).padStart(3, "0")}000`;
    }
    default: return null;
  }
}

export function rowsFor(columns: ColumnDef[], n: number, s = 7): ReportRow[] {
  reseed(s);
  const rows: ReportRow[] = [];
  for (let i = 0; i < n; i++) {
    const r: ReportRow = {};
    for (const c of columns) r[c.key] = valueFor(c, i);
    if (!columns.some((c) => c.key === "symbol")) r.symbol = SYMBOLS[i % SYMBOLS.length][0];
    rows.push(r);
  }
  return rows;
}
