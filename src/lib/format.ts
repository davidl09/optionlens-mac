// One formatter per ColumnFmt. Used by tables, stat grids and chart tooltips.
// Missing values (null / undefined / NaN / ±Infinity) always render as NULL_TEXT.
import type { ColumnFmt } from "../api/types";

export const NULL_TEXT = "—";

export function isMissing(v: unknown): boolean {
  return v === null || v === undefined || (typeof v === "number" && !Number.isFinite(v)) || v === "";
}

function toNum(v: unknown): number | null {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v === "string" && v.trim() !== "") {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

const nf = (min: number, max: number) => new Intl.NumberFormat("en-US", { minimumFractionDigits: min, maximumFractionDigits: max });
const NF0 = nf(0, 0);
const NF2 = nf(2, 2);
const NF_UP2 = nf(0, 2);
const NF1 = nf(1, 1);

function pctText(n: number, signed: boolean): string {
  const p = n * 100;
  const digits = Math.abs(p) < 10 ? 2 : 1;
  const s = p.toFixed(digits);
  const sign = signed && p > 0 ? "+" : "";
  return `${sign}${s === "-0.00" || s === "-0.0" ? s.slice(1) : s}%`;
}

function bigText(n: number): string {
  const a = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (a >= 1e12) return `${sign}${(a / 1e12).toFixed(2)}T`;
  if (a >= 1e9) return `${sign}${(a / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${sign}${(a / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${sign}${(a / 1e3).toFixed(1)}K`;
  return `${sign}${a.toFixed(0)}`;
}

const NY_DT = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
});

export function fmtDateTime(v: string): string {
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return v;
  const parts = Object.fromEntries(NY_DT.formatToParts(d).map((p) => [p.type, p.value]));
  const hour = parts.hour === "24" ? "00" : parts.hour;
  return `${parts.year}-${parts.month}-${parts.day} ${hour}:${parts.minute} ET`;
}

/** Parse an OSI code (e.g. AAPL261120C00100000, SPXW 261120P05000000). */
export function parseOsi(osi: string): { root: string; expiry: string; right: "C" | "P"; strike: number } | null {
  const m = /^([A-Z0-9.^_]+?)\s*(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/.exec(osi.trim().toUpperCase());
  if (!m) return null;
  return { root: m[1], expiry: `20${m[2]}-${m[3]}-${m[4]}`, right: m[5] as "C" | "P", strike: Number(m[6]) / 1000 };
}

const formatters: Record<ColumnFmt, (v: unknown) => string> = {
  symbol: (v) => String(v),
  text: (v) => String(v),
  badge: (v) => String(v),
  int: (v) => { const n = toNum(v); return n === null ? NULL_TEXT : NF0.format(Math.round(n)); },
  num: (v) => { const n = toNum(v); return n === null ? NULL_TEXT : Math.abs(n) >= 1000 ? NF0.format(n) : NF_UP2.format(n); },
  num2: (v) => { const n = toNum(v); return n === null ? NULL_TEXT : NF2.format(n); },
  money: (v) => { const n = toNum(v); return n === null ? NULL_TEXT : `${n < 0 ? "-" : ""}$${NF2.format(Math.abs(n))}`; },
  bigmoney: (v) => { const n = toNum(v); return n === null ? NULL_TEXT : bigText(n); },
  pct: (v) => { const n = toNum(v); return n === null ? NULL_TEXT : pctText(n, false); },
  pct_signed: (v) => { const n = toNum(v); return n === null ? NULL_TEXT : pctText(n, true); },
  vol: (v) => { const n = toNum(v); return n === null ? NULL_TEXT : NF1.format(n * 100); },
  ratio: (v) => { const n = toNum(v); return n === null ? NULL_TEXT : `${NF2.format(n)}×`; },
  date: (v) => String(v).slice(0, 10),
  datetime: (v) => fmtDateTime(String(v)),
  bool: (v) => (v === true || v === "true" || v === 1 ? "Yes" : v === false || v === "false" || v === 0 ? "No" : String(v)),
  osi: (v) => {
    const p = parseOsi(String(v));
    return p ? `${p.root} ${p.expiry} ${p.right} ${NF_UP2.format(p.strike)}` : String(v);
  },
};

export function fmt(v: unknown, kind: ColumnFmt): string {
  if (isMissing(v)) return NULL_TEXT;
  return formatters[kind](v);
}

/** Multiplier between stored value and what a user types in a filter (fractions are shown as %). */
export function displayScale(kind: ColumnFmt): number {
  return kind === "pct" || kind === "pct_signed" || kind === "vol" ? 100 : 1;
}

export function isNumericFmt(kind: ColumnFmt): boolean {
  return ["int", "num", "num2", "money", "bigmoney", "pct", "pct_signed", "vol", "ratio"].includes(kind);
}

/** "pos" | "neg" | "" for colouring signed values. */
export function signClass(v: unknown): string {
  const n = toNum(v);
  if (n === null || n === 0) return "";
  return n > 0 ? "pos" : "neg";
}

/** Tone for badge values (sentiment-ish words). */
export function badgeTone(v: unknown): "pos" | "neg" | "warn" | "neutral" {
  const s = String(v).toLowerCase();
  if (/bull|up|high|gain|call|buy|long|real|yes|ok|contango/.test(s)) return "pos";
  if (/bear|down|low|loss|put|sell|short|backward|fail|no\b/.test(s)) return "neg";
  if (/model|warn|proxy|stale/.test(s)) return "warn";
  return "neutral";
}

export const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"];

/** "1 day" / "3 days" (count rendered by the caller). */
export function plural(n: number, word: string): string {
  return n === 1 ? word : `${word}s`;
}
