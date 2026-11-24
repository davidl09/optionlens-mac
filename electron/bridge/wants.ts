// GET /api/ingest/wants (INTEGRATION.md §6): strict parsing (a malformed section is an error, an absent
// optional section is "nothing wanted") and planning (what to enqueue now, given freshness and the
// attempt ledger so a symbol IBKR has no data for is not re-requested on every poll).
import type { ChainShape } from "./chain";
import { nyDate } from "./ny";
import type { TwsContract } from "./tws";

export interface Wants {
  /** IV30 / HV30 daily history to fetch per symbol, from `from` (YYYY-MM-DD). */
  ivHistory: { symbol: string; from: string }[];
  hvHistory: { symbol: string; from: string }[];
  /** How many symbols the engine keeps IV/HV history for (wanted or already current); null = not said. */
  historySymbols: number | null;
  /**
   * `asOfSession` (YYYY-MM-DD): an after-hours fill — outside market hours the session (the last completed one)
   * lacks this data, and these symbols' frozen values are stored as that session's (INTEGRATION.md §6); null =
   * the live session.
   */
  underlyingStats: { symbols: string[]; maxAgeS: number; asOfSession: string | null } | null;
  chainUniverse: { symbols: string[]; maxAgeS: number; shape: ChainShape; asOfSession: string | null } | null;
  eodChains: { sessionDate: string; symbols: string[]; notBefore: number; deadline: number } | null;
  optionBars: { symbol: string; sessions: string[]; shape: ChainShape }[];
  indexSymbols: string[];
  /** Index daily history (SPX, NDX, ..., VIX family) the engine is missing, from `from` (YYYY-MM-DD). */
  indexBars: { symbol: string; from: string }[];
  /** Daily adjusted + raw closes for the dividends of these stocks / ETFs, from `from` (absent = none). */
  dividendHistory: { symbol: string; from: string }[];
  onDemandMaxAgeS: number;
}

export class WantsError extends Error {
  constructor(message: string) { super(`invalid wants: ${message}`); this.name = "WantsError"; }
}

type Obj = Record<string, unknown>;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const SYMBOL_RE = /^[A-Z0-9.^_-]{1,15}$/;

function obj(v: unknown, path: string): Obj {
  if (typeof v !== "object" || v === null || Array.isArray(v)) throw new WantsError(`${path} must be an object`);
  return v as Obj;
}
function arr(v: unknown, path: string): unknown[] {
  if (!Array.isArray(v)) throw new WantsError(`${path} must be an array`);
  return v;
}
function symbols(v: unknown, path: string): string[] {
  return arr(v, path).map((s, i) => {
    if (typeof s !== "string" || !SYMBOL_RE.test(s.toUpperCase())) throw new WantsError(`${path}[${i}] is not a symbol`);
    return s.toUpperCase();
  });
}
function date(v: unknown, path: string): string {
  if (typeof v !== "string" || !DATE_RE.test(v)) throw new WantsError(`${path} must be YYYY-MM-DD`);
  return v;
}
function num(v: unknown, path: string, min: number, max: number): number {
  if (typeof v !== "number" || !Number.isFinite(v) || v < min || v > max) throw new WantsError(`${path} must be a number in [${min}, ${max}]`);
  return v;
}
function time(v: unknown, path: string): number {
  const t = typeof v === "string" ? Date.parse(v) : NaN;
  if (!Number.isFinite(t)) throw new WantsError(`${path} must be an ISO date-time`);
  return t;
}
const present = (v: unknown) => v !== undefined && v !== null;
function shape(o: Obj, path: string): ChainShape {
  return { expiries: Math.round(num(o.expiries, `${path}.expiries`, 1, 60)), moneyness: num(o.moneyness, `${path}.moneyness`, 0.01, 1) };
}

/** [{symbol, from}] (absent = []). */
function symbolsFrom(v: unknown, path: string): { symbol: string; from: string }[] {
  return present(v) ? arr(v, path).map((x, i) => {
    const o = obj(x, `${path}[${i}]`);
    return { symbol: symbols([o.symbol], `${path}[${i}].symbol`)[0], from: date(o.from, `${path}[${i}].from`) };
  }) : [];
}

export function parseWants(raw: unknown): Wants {
  const w = obj(raw, "wants");
  const ivHistory = symbolsFrom(w.iv_history, "iv_history");
  const us = present(w.underlying_stats) ? obj(w.underlying_stats, "underlying_stats") : null;
  const cu = present(w.chain_universe) ? obj(w.chain_universe, "chain_universe") : null;
  const eod = present(w.eod_chains) ? obj(w.eod_chains, "eod_chains") : null;
  const optionBars = present(w.option_bars) ? arr(w.option_bars, "option_bars").map((x, i) => {
    const o = obj(x, `option_bars[${i}]`);
    return {
      symbol: symbols([o.symbol], `option_bars[${i}].symbol`)[0],
      sessions: arr(o.sessions, `option_bars[${i}].sessions`).map((d, j) => date(d, `option_bars[${i}].sessions[${j}]`)),
      shape: shape(o, `option_bars[${i}]`),
    };
  }) : [];
  return {
    ivHistory,
    hvHistory: symbolsFrom(w.hv_history, "hv_history"),
    historySymbols: present(w.history_symbols) ? Math.round(num(w.history_symbols, "history_symbols", 0, 1e6)) : null,
    underlyingStats: us ? {
      symbols: symbols(us.symbols, "underlying_stats.symbols"), maxAgeS: num(us.max_age_s, "underlying_stats.max_age_s", 1, 86_400 * 7),
      asOfSession: present(us.as_of_session) ? date(us.as_of_session, "underlying_stats.as_of_session") : null,
    } : null,
    chainUniverse: cu ? {
      symbols: symbols(cu.symbols, "chain_universe.symbols"), maxAgeS: num(cu.max_age_s, "chain_universe.max_age_s", 1, 86_400 * 7), shape: shape(cu, "chain_universe"),
      asOfSession: present(cu.as_of_session) ? date(cu.as_of_session, "chain_universe.as_of_session") : null,
    } : null,
    eodChains: eod ? {
      sessionDate: date(eod.session_date, "eod_chains.session_date"), symbols: symbols(eod.symbols, "eod_chains.symbols"),
      notBefore: time(eod.not_before, "eod_chains.not_before"), deadline: time(eod.deadline, "eod_chains.deadline"),
    } : null,
    optionBars,
    indexSymbols: present(w.index_symbols) ? symbols(w.index_symbols, "index_symbols") : [],
    indexBars: symbolsFrom(w.index_bars, "index_bars"),
    dividendHistory: symbolsFrom(w.dividend_history, "dividend_history"),
    onDemandMaxAgeS: present(w.chains_on_demand_max_age_s) ? num(w.chains_on_demand_max_age_s, "chains_on_demand_max_age_s", 0, 86_400) : 120,
  };
}

// ---------------- planning ----------------

/** How long an attempted historical item (success or failure) is not re-requested. */
export const HIST_RETRY_MS = 20 * 3600_000;
/** How long a failed closing chain waits before another attempt within the window. */
export const EOD_RETRY_MS = 30 * 60_000;

export interface PlanContext {
  now: number;
  /** Epoch ms of the last successful push of this kind for a symbol, if any. */
  lastChainPush(symbol: string): number | undefined;
  lastStatsPush(symbol: string): number | undefined;
  /** True when the ledger has an attempt for `key` within `ms`. */
  attemptedWithin(key: string, ms: number): boolean;
}

export interface Plan {
  eod: { sessionDate: string; symbols: string[] } | null;
  /** `asOfSession`: an after-hours fill of that session (see Wants), else null. */
  universe: { symbols: string[]; shape: ChainShape; asOfSession: string | null };
  stats: string[];
  statsAsOfSession: string | null;
  indexQuotes: string[];
  indexBars: { symbol: string; from: string }[];
  optionBars: { symbol: string; sessions: string[]; shape: ChainShape }[];
  volHistory: { symbol: string; from: string; kind: "iv30" | "hv30" }[];
  dividendHistory: { symbol: string; from: string }[];
}

export const ledgerKey = {
  eod: (day: string, s: string) => `eod:${day}:${s}`,
  vol: (kind: "iv30" | "hv30", s: string) => `${kind}:${s}`,
  indexBars: (s: string) => `bars:${s}`,
  dividends: (s: string) => `divs:${s}`,
  optionBars: (s: string, sessions: string[]) => `obars:${s}:${[...sessions].sort().join(",")}`,
  /** One contract of an option-bars plan. */
  optionBar: (c: TwsContract, sessions: string[]) => `obar:${c.conId ?? `${c.symbol}${c.lastTradeDateOrContractMonth}${c.right}${c.strike}`}:${[...sessions].sort().join(",")}`,
};

const stale = (last: number | undefined, now: number, maxAgeS: number) => last === undefined || now - last >= maxAgeS * 1000;

export function planWants(w: Wants, c: PlanContext): Plan {
  const eodActive = w.eodChains !== null && c.now >= w.eodChains.notBefore && c.now <= w.eodChains.deadline;
  const eod = eodActive && w.eodChains ? {
    sessionDate: w.eodChains.sessionDate,
    symbols: w.eodChains.symbols.filter((s) => !c.attemptedWithin(ledgerKey.eod(w.eodChains!.sessionDate, s), EOD_RETRY_MS)),
  } : null;
  const eodSet = new Set(eodActive && w.eodChains ? w.eodChains.symbols : []);
  const cu = w.chainUniverse;
  return {
    eod,
    universe: {
      symbols: cu ? cu.symbols.filter((s) => !eodSet.has(s) && stale(c.lastChainPush(s), c.now, cu.maxAgeS)) : [],
      shape: cu ? cu.shape : { expiries: 6, moneyness: 0.2 },
      asOfSession: cu?.asOfSession ?? null,
    },
    stats: w.underlyingStats ? w.underlyingStats.symbols.filter((s) => stale(c.lastStatsPush(s), c.now, w.underlyingStats!.maxAgeS)) : [],
    statsAsOfSession: w.underlyingStats?.asOfSession ?? null,
    indexQuotes: w.indexSymbols,
    indexBars: w.indexBars.filter((x) => !c.attemptedWithin(ledgerKey.indexBars(x.symbol), HIST_RETRY_MS)),
    optionBars: w.optionBars.filter((o) => o.sessions.length > 0 && !c.attemptedWithin(ledgerKey.optionBars(o.symbol, o.sessions), HIST_RETRY_MS)),
    volHistory: ([["iv30", w.ivHistory], ["hv30", w.hvHistory]] as const).flatMap(([kind, list]) => list
      .filter((x) => !c.attemptedWithin(ledgerKey.vol(kind, x.symbol), HIST_RETRY_MS))
      .map((x) => ({ symbol: x.symbol, from: x.from, kind }))),
    dividendHistory: w.dividendHistory.filter((x) => !c.attemptedWithin(ledgerKey.dividends(x.symbol), HIST_RETRY_MS)),
  };
}

/** IBKR duration string covering `from` → `now`'s New York date (≤ 365 days in D, else whole years, max 10 Y). */
export function durationFor(from: string, now: Date): string {
  const days = Math.round((Date.parse(`${nyDate(now)}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86_400_000) + 1;
  if (days <= 1) return "2 D";
  if (days <= 365) return `${days} D`;
  return `${Math.min(10, Math.ceil(days / 365))} Y`;
}

/** GET /api/ingest/live-wants (INTEGRATION.md §9): the symbols on screen to snapshot while the bridge is the live fallback. */
export interface LiveWants { symbols: string[]; intervalMs: number }

export function parseLiveWants(raw: unknown): LiveWants {
  const w = obj(raw, "live-wants");
  return { symbols: symbols(w.symbols ?? [], "live-wants.symbols").slice(0, 50), intervalMs: num(w.interval_s, "live-wants.interval_s", 1, 3600) * 1000 };
}
