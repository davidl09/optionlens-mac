// Per-symbol fixtures conforming to src/api/types.ts.
import type {
  Activity, BacktestResult, BacktestTrade, Bar, Chain, ContractHistory, Dividends, Earnings, EarningsStrategyResult, Events, ExpiryInfo,
  Fundamentals, ImpliedMoves, Insiders, IntradayBars, IntradayInterval, IntradayRange, NewsItem, OpenInterest, OptionRow, PriceAction, Profile, Quote, ReportResult, Seasonality, Skew, IvSurface, SurfaceCell, SurfaceMode, SurfaceRows,
  KeyStats, StraddleInfo, SymbolSummary, TradeIdea, TradeIdeas, VolSeasonality, Volatility,
} from "../../src/api/types";
import { addDays, between, NOW, pick, reseed, rnd, round, rowsFor, SYMBOLS, TODAY, tradingDays, walk } from "./gen";

const SPOT = 231.42;

export function quote(sym: string): Quote {
  return { symbol: sym, ts: NOW, price: SPOT, change: 2.87, change_pct: 0.01256, open: 229.1, high: 232.05, low: 228.4, prev_close: 228.55, volume: 48_211_300, bid: 231.4, ask: 231.44, iv30: 0.274, source: "ibkr", delayed: false };
}

/** Stored daily history: 10 years, except DELL (a short history: no SMA 200 yet). */
const HISTORY_DAYS = (sym: string) => (sym === "DELL" ? 150 : 2520);

const INTERVAL_MIN: Record<IntradayInterval, number> = { "1m": 1, "5m": 5, "15m": 15, "1h": 60 };
const SESSIONS: Record<IntradayRange, number> = { "1d": 1, "5d": 5, "1m": 21, "3m": 63 };

/** Like the engine's /bars?interval=…: bars of the range's sessions up to NOW (15:45 ET), times as
 * EST (−05:00) throughout; extended adds 04:00–09:30 and (before today) 16:00–20:00. */
export function intradayBars(sym: string, interval: IntradayInterval, range: IntradayRange, extended: boolean): IntradayBars {
  const step = INTERVAL_MIN[interval];
  const days = tradingDays(SESSIONS[range]);
  reseed(7 + step);
  let p = SPOT * 0.98;
  const bars = days.flatMap((d) => {
    const out = [];
    const nowMin = d === TODAY ? 15 * 60 + 45 : 24 * 60;
    let pv = 0, vol = 0;   // the session VWAP from typical prices, as the engine computes it (regular hours only)
    for (let m = extended ? 4 * 60 : 9 * 60 + 30; m < Math.min(extended ? 20 * 60 : 16 * 60, nowMin); m += step) {
      const o = p;
      p = round(p * (1 + (rnd() - 0.5) * 0.002));
      const bar = { ts: new Date(Date.parse(`${d}T05:00:00Z`) + m * 60_000).toISOString(), open: round(o), high: round(Math.max(o, p) * 1.0005), low: round(Math.min(o, p) * 0.9995), close: p, volume: Math.round(between(1e4, 9e4) * step) };
      const regular = m >= 9 * 60 + 30 && m < 16 * 60;
      if (regular) { pv += ((bar.high + bar.low + bar.close) / 3) * bar.volume; vol += bar.volume; }
      out.push({ ...bar, vwap: regular ? round(pv / vol) : null });
    }
    return out;
  });
  return {
    symbol: sym, interval, range, extended, start: `${days[0]}T09:00:00Z`, end: NOW, bars, has_extended: true,
    sources: ["questrade"], delayed: false, missing: [], problem: null, vwap_source: "computed",
  };
}

/** Like the engine's /bars: the range's bars, with SMAs over the full stored history. */
export function bars(sym: string, range: string): Bar[] {
  const total = HISTORY_DAYS(sym);
  const n = Math.min(total, { "1m": 21, "3m": 63, "6m": 126, "1y": 252, "3y": 756, "5y": 1260, "10y": 2520, max: 2520 }[range] ?? 252);
  reseed(11);
  const closes = walk(total, SPOT * 0.8);
  const scale = SPOT / closes[closes.length - 1];
  const scaled = closes.map((c) => round(c * scale));
  const sma = (i: number, w: number) => (i + 1 >= w ? round(scaled.slice(i + 1 - w, i + 1).reduce((a, b) => a + b, 0) / w) : null);
  const all = tradingDays(total).map((date, i) => {
    const c = scaled[i];
    const o = round(c * (1 + (rnd() - 0.5) * 0.01));
    return { date, open: o, high: round(Math.max(o, c) * (1 + rnd() * 0.01)), low: round(Math.min(o, c) * (1 - rnd() * 0.01)), close: c, volume: Math.round(between(3e7, 9e7)),
      sma20: sma(i, 20), sma50: sma(i, 50), sma200: sma(i, 200) };
  });
  return all.slice(total - n);
}

const EXPIRY_DATES = ["2026-11-20", "2026-11-27", "2026-12-04", "2026-12-18", "2027-01-15", "2027-02-19", "2027-03-19", "2027-06-17"];
const dte = (e: string) => Math.round((Date.parse(`${e}T12:00:00Z`) - Date.parse(`${TODAY}T12:00:00Z`)) / 86_400_000);

export function expiries(): ExpiryInfo[] {
  reseed(5);
  return EXPIRY_DATES.map((e, i) => ({
    expiry: e, dte: dte(e), is_monthly: [3, 4, 5, 6, 7].includes(i), atm_iv: round(0.24 + i * 0.006, 4),
    straddle: round(SPOT * 0.27 * Math.sqrt(Math.max(1, dte(e)) / 365) * 0.8, 2),
    call_volume: Math.round(between(5e3, 9e4)), put_volume: Math.round(between(4e3, 7e4)), call_oi: Math.round(between(5e4, 4e5)), put_oi: Math.round(between(4e4, 3e5)),
  }));
}

const STRIKES = Array.from({ length: 33 }, (_, i) => 150 + i * 5);

function osi(sym: string, expiry: string, right: "C" | "P", strike: number) {
  return `${sym}${expiry.slice(2, 4)}${expiry.slice(5, 7)}${expiry.slice(8, 10)}${right}${String(Math.round(strike * 1000)).padStart(8, "0")}`;
}

function optionRow(sym: string, expiry: string, right: "C" | "P", strike: number): OptionRow {
  const t = Math.max(1, dte(expiry)) / 365;
  const m = Math.log(strike / SPOT);
  const iv = round(0.26 + 0.35 * m * m - (right === "P" ? 0.04 * m : 0), 4);
  const intrinsic = Math.max(0, right === "C" ? SPOT - strike : strike - SPOT);
  const tv = SPOT * iv * Math.sqrt(t) * 0.4 * Math.exp(-(m * m) / (2 * iv * iv * t));
  const mid = round(intrinsic + tv, 2);
  const spread = Math.max(0.02, mid * 0.03);
  const d = Math.max(-1, Math.min(1, (right === "C" ? 1 : -1) * (0.5 + (right === "C" ? -1 : 1) * m / (iv * Math.sqrt(t) * 2.5))));
  const delta = right === "C" ? Math.max(0.01, Math.min(0.99, d)) : Math.min(-0.01, Math.max(-0.99, d));
  const volume = Math.round(between(0, 6000) * Math.exp(-Math.abs(m) * 8));
  const oi = Math.round(between(200, 20000) * Math.exp(-Math.abs(m) * 5));
  return {
    osi: osi(sym, expiry, right, strike), expiry, strike, right,
    bid: round(Math.max(0, mid - spread / 2)), ask: round(mid + spread / 2), mid, last: round(mid * (1 + (rnd() - 0.5) * 0.04)), change: round((rnd() - 0.5) * mid * 0.2),
    volume, oi, iv: rnd() < 0.03 ? null : iv, delta: round(delta, 3), gamma: round(0.02 * Math.exp(-m * m * 20), 4), theta: round(-mid * 0.02 / Math.sqrt(t * 365), 3),
    vega: round(SPOT * Math.sqrt(t) * 0.004, 3), rho: round(0.05 * t, 3), theo: mid, itm: intrinsic > 0,
  };
}

export function chain(sym: string, expiry: string | null): Chain {
  const exp = expiry && EXPIRY_DATES.includes(expiry) ? expiry : EXPIRY_DATES[0];
  reseed(exp.length + Number(exp.slice(-2)));
  return {
    symbol: sym, underlying: quote(sym), asof: NOW, expiries: expiries(), expiry: exp, strikes: STRIKES,
    calls: STRIKES.map((k) => optionRow(sym, exp, "C", k)), puts: STRIKES.map((k) => optionRow(sym, exp, "P", k)),
    ...(sym === "MSFT" ? { source: "questrade", delayed: false } : { source: "ibkr", delayed: true }),   // MSFT: a Questrade chain
  };
}

function strikeAggs(withChange: boolean) {
  reseed(21);
  return STRIKES.map((k) => {
    const w = Math.exp(-Math.abs(Math.log(k / SPOT)) * 6);
    return {
      strike: k, call_volume: Math.round(between(100, 9000) * w), put_volume: Math.round(between(100, 7000) * w), call_oi: Math.round(between(1e3, 4e4) * w), put_oi: Math.round(between(1e3, 3e4) * w),
      call_oi_change: withChange ? Math.round(between(-3000, 4000) * w) : null, put_oi_change: withChange ? Math.round(between(-3000, 3000) * w) : null,
    };
  });
}

export function activity(sym: string, expiry: string | null): Activity {
  const c = chain(sym, expiry === "all" ? null : expiry);
  const top = [...c.calls, ...c.puts].sort((a, b) => (b.volume ?? 0) - (a.volume ?? 0)).slice(0, 20).map((r) => ({ ...r, vol_oi: r.oi ? round((r.volume ?? 0) / r.oi) : null, premium: round((r.volume ?? 0) * (r.mid ?? 0) * 100, 0) }));
  return {
    symbol: sym, asof: NOW, expiry: expiry === "all" || !expiry ? null : c.expiry, by_expiry: c.expiries, by_strike: strikeAggs(false), top_contracts: top,
    totals: { call_volume: 412_300, put_volume: 288_100, call_oi: 3_100_000, put_oi: 2_400_000, pc_volume: 0.7, pc_oi: 0.77, call_premium: 98_000_000, put_premium: 61_000_000 },
    moneyness: { call_otm: 220_000, call_atm: 140_000, call_itm: 52_300, put_otm: 170_000, put_atm: 90_000, put_itm: 28_100 },
    history: tradingDays(40).map((d) => ({ date: d, call_volume: Math.round(between(2e5, 6e5)), put_volume: Math.round(between(1.5e5, 4e5)), call_oi: null, put_oi: null })),
  };
}

export function openInterest(sym: string, expiry: string | null): OpenInterest {
  const c = chain(sym, expiry);
  const rows = [...c.calls, ...c.puts].slice(0, 10).map((r, i) => ({ ...r, oi_prev: (r.oi ?? 0) - (i - 4) * 300, oi_change: (i - 4) * 300 }));
  return {
    symbol: sym, expiry: c.expiry, prior_date: "2026-11-18", expiries: c.expiries, by_strike: strikeAggs(true),
    max_pain: c.expiries.map((e, i) => ({ expiry: e.expiry, strike: i === 7 ? null : 225 + (i % 3) * 5 })),
    top_increases: rows.filter((r) => r.oi_change > 0), top_decreases: rows.filter((r) => r.oi_change < 0),
  };
}

export function contract(sym: string, code: string): ContractHistory {
  const c = chain(sym, null);
  const latest = c.calls.find((r) => r.osi === code) ?? c.calls[16];
  reseed(9);
  return {
    osi: code, symbol: sym, expiry: latest.expiry, strike: latest.strike, right: latest.right, latest,
    history: tradingDays(15).map((d, i) => ({ date: d, bid: round(3 + i * 0.1), ask: round(3.1 + i * 0.1), mid: round(3.05 + i * 0.1), iv: round(0.25 + rnd() * 0.03, 4), volume: Math.round(between(100, 3000)), oi: 5000 + i * 120, delta: round(0.45 + i * 0.005, 3), underlying: round(SPOT - 10 + i) })),
  };
}

const IDEAS: TradeIdea[] = [
  { strategy: "bull-put-spreads", label: "Bull put spread", family: "vertical", legs: "-1 P 220 / +1 P 215 2026-12-18", expiry: "2026-12-18", cost: -1.45, max_profit: 145, max_loss: 355, pop: 0.71, ev_hv: 18.2, edge: 0.051, note: null },
  { strategy: "iron-condors-short", label: "Short iron condor", family: "condor_fly", legs: "+1 P 205 / -1 P 210 / -1 C 250 / +1 C 255 2026-12-18", expiry: "2026-12-18", cost: -1.2, max_profit: 120, max_loss: 380, pop: 0.64, ev_hv: 9.4, edge: 0.025, note: null },
  { strategy: "long-calls", label: "Long call", family: "single", legs: "+1 C 240 2027-01-15", expiry: "2027-01-15", cost: 6.1, max_profit: null, max_loss: 610, pop: 0.34, ev_hv: 22.0, edge: 0.036, note: null },
  { strategy: "atm-straddles", label: "Long ATM straddle", family: "straddle", legs: "+1 C 230 / +1 P 230 2026-12-18", expiry: "2026-12-18", cost: 14.3, max_profit: null, max_loss: 1430, pop: 0.41, ev_hv: -12, edge: -0.008, note: null },
  { strategy: "seasonality", label: "December seasonality", family: "seasonality", legs: "Long stock", expiry: null, cost: null, max_profit: null, max_loss: null, pop: 0.7, ev_hv: null, edge: null, note: "Up in 7 of the last 10 Decembers (avg +2.1%)." },
];

/** Symbols the mocked engine has no chain for, with the engine's error code (INTEGRATION.md §5). */
export const NO_CHAIN: Record<string, "no_chain" | "no_options"> = { DELL: "no_chain", XNOP: "no_options" };
const TODAY_NY = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());

export function summary(sym: string): SymbolSummary {
  const s = baseSummary(sym);
  const code = NO_CHAIN[sym];
  if (!code) return s;
  // No chain: optionable unknown (or confirmed none); IV30 only from the stored (ingested) history, as of the last session.
  return { ...s, optionable: code === "no_options" ? false : null, iv30: 0.523, iv30_asof: "2026-11-20", iv_rank: 1, iv_percentile: 0.99,
    iv_history_days: 252, iv_hv_ratio: 0.523 / s.hv20!, call_volume: null, put_volume: null, pc_volume_ratio: null, total_oi: null, ideas: [], options_asof: null };
}

function baseSummary(sym: string): SymbolSummary {
  const b = bars(sym, "1y");
  reseed(13);
  const name = SYMBOLS.find((s) => s[0] === sym)?.[1] ?? `${sym} Holdings`;
  return {
    symbol: sym, name, kind: ["SPY", "QQQ", "IWM", "DIA"].includes(sym) ? "etf" : ["SPX", "NDX", "RUT", "XSP"].includes(sym) ? "index" : "stock",
    market: "US", exchange: null, currency: "USD",
    sector: "Technology", industry: "Consumer Electronics", market_cap: 3.48e12, optionable: true, in_universe: true, quote: quote(sym),
    avg_volume_30d: 52_100_000, rel_volume: 0.93, high_52w: 248.1, low_52w: 168.3, high_3y: 248.1, low_3y: 124.2, beta_1y: 1.12, corr_spy_1y: 0.71,
    iv30: 0.274, iv30_asof: TODAY_NY, iv_rank: null, iv_percentile: null, iv_history_days: 12, hv20: 0.221, iv_hv_ratio: 1.24,
    call_volume: 412_300, put_volume: 288_100, avg_option_volume_20d: null, avg_option_volume_days: 0, rel_option_volume: null, pc_volume_ratio: 0.7, total_oi: 5_500_000,
    returns: { d1: 0.0126, d5: -0.018, m1: 0.041, m3: 0.083, m6: 0.12, ytd: 0.19, y1: 0.24, y3: null },
    next_earnings: { date: "2027-01-28", timing: "amc", days: 70, implied_move_pct: null, avg_abs_move_pct: 0.038 },
    next_dividend: { ex_date: "2026-11-24", amount: 0.26, days: 5, yield: 0.0045, source: "nasdaq" }, key_stats: keyStats(sym),
    support: [224.5, 218.2, 210.0], resistance: [236.8, 241.3, 248.1],
    rsi14: 58.2, atr14_pct: 0.0172, sma20: 228.4, sma50: 224.9, sma200: 211.7,
    price_1y: b.map((x) => ({ date: x.date, close: x.close })),
    iv_hv_1y: b.map((x, i) => ({ date: x.date, iv30: i > b.length - 13 ? round(0.26 + rnd() * 0.03, 4) : null, hv20: round(0.18 + rnd() * 0.08, 4) })),
    ideas: IDEAS.slice(0, 3), options_asof: NOW,
  };
}

export function priceAction(): PriceAction {
  const b = bars("AAPL", "1y");
  const rows = b.map((x, i) => ({
    ...x, change_pct: i ? round(x.close / b[i - 1].close - 1, 5) : null, gap_pct: i ? round(x.open / b[i - 1].close - 1, 5) : null,
    range_pct: round((x.high - x.low) / x.close, 5), rel_volume: round(x.volume / 6e7, 2),
  })).reverse();
  let co = 0, ci = 0;
  const series = b.slice(1).map((x, i) => { co += x.open / b[i].close - 1; ci += x.close / x.open - 1; return { date: x.date, cum_overnight: round(co, 5), cum_intraday: round(ci, 5) }; });
  return {
    rows,
    stats: { days: rows.length, avg_abs_move: 0.0121, up_day_pct: 0.53, avg_gap_abs: 0.0048, largest_up: rows[10], largest_down: rows[40], stdev_daily: 0.0158 },
    intraday_overnight: { avg_overnight: 0.0004, avg_intraday: 0.0003, cum_overnight: co, cum_intraday: ci, overnight_up_pct: 0.55, intraday_up_pct: 0.51, series },
  };
}

export function volatility(sym: string): Volatility {
  const v = baseVolatility(sym);
  return NO_CHAIN[sym] ? { ...v, asof: null, iv30: 0.523, iv30_asof: "2026-11-20", term_today: [], term_1w: null, term_1m: null } : v;
}

function baseVolatility(sym: string): Volatility {
  const b = bars("AAPL", "3y");
  reseed(17);
  const term = (shift: number) => expiries().map((e) => ({ expiry: e.expiry, dte: e.dte, atm_iv: e.atm_iv === null ? null : round(e.atm_iv + shift, 4) }));
  return {
    symbol: sym, asof: NOW, iv30: 0.274, iv30_asof: TODAY_NY, iv_rank: null, iv_percentile: null, history_days: 12, iv_high_52w: null, iv_low_52w: null,
    constant_maturity: ([30, 60, 90, 120, 180, 360] as const).map((t, i) => ({ tenor: t, iv: round(0.274 + i * 0.004, 4), chg_1d: round((rnd() - 0.5) * 0.02, 4), chg_1w: null })),
    term_today: term(0), term_1w: term(0.012), term_1m: null,
    hv: ([10, 20, 30, 60, 90, 252] as const).map((w) => ({ window: w, value: round(0.2 + w / 3000, 4) })), parkinson20: 0.198,
    history: b.map((x, i) => ({ date: x.date, iv30: i > b.length - 13 ? round(0.26 + rnd() * 0.03, 4) : null, iv60: null, iv90: i > b.length - 13 ? 0.285 : null, hv10: round(0.15 + rnd() * 0.15, 4), hv20: round(0.17 + rnd() * 0.1, 4), hv60: round(0.2 + rnd() * 0.05, 4) })),
    risk_premium: b.slice(-12).map((x) => ({ date: x.date, iv30: round(0.26 + rnd() * 0.03, 4), hv20: round(0.2 + rnd() * 0.03, 4), realized_fwd21: null })),
  };
}

export function skew(sym: string, expiry: string | null): Skew {
  const c = chain(sym, expiry);
  return {
    symbol: sym, expiry: c.expiry, dte: dte(c.expiry), spot: SPOT, expiries: c.expiries,
    points: STRIKES.map((k, i) => ({ strike: k, moneyness: round(k / SPOT - 1, 4), call_iv: c.calls[i].iv, put_iv: c.puts[i].iv, call_delta: c.calls[i].delta, put_delta: c.puts[i].delta })),
    by_delta: [-0.05, -0.1, -0.25, -0.4, -0.5, 0.5, 0.4, 0.25, 0.1, 0.05].map((d) => ({ delta: d, iv: round(0.27 + (d < 0 ? 0.08 : 0.03) * (0.5 - Math.abs(d)), 4) })),
    history: tradingDays(12).map((d) => ({ date: d, skew25_30: round(0.04 + rnd() * 0.01, 4), put_skew_30: round(0.025 + rnd() * 0.01, 4) })),
  };
}

const SURFACE_ROWS: Record<Exclude<SurfaceRows, "strike">, number[]> = {
  delta: [-0.1, -0.15, -0.2, -0.25, -0.3, -0.35, -0.4, -0.45, 0.5, 0.45, 0.4, 0.35, 0.3, 0.25, 0.2, 0.15, 0.1],
  moneyness: Array.from({ length: 17 }, (_, i) => round(-0.2 + 0.025 * i, 4)),
};

/** Like the engine's /iv-surface: each cell the OTM contract nearest the row (no interpolation); the front expiry's
 *  10Δ wings and contracts without a bid / OI are unreliable. Earnings 2027-01-28: 2027-02-19 is the earnings expiry. */
export function ivSurface(sym: string, rowsBy: SurfaceRows, mode: SurfaceMode): IvSurface {
  const exps = expiries();
  const rows = rowsBy === "strike" ? STRIKES.filter((k) => Math.abs(k / SPOT - 1) <= 0.2) : SURFACE_ROWS[rowsBy];
  const chains = exps.map((e) => chain(sym, e.expiry));
  const cells: SurfaceCell[][] = rows.map((v) => chains.map((c, x): SurfaceCell => {
    const atm = exps[x].atm_iv;
    const otm = [...c.puts.filter((r) => r.strike < SPOT), ...c.calls.filter((r) => r.strike >= SPOT)];
    const byDelta = rowsBy === "delta" && v !== 0.5;
    const target = byDelta ? v : rowsBy === "strike" ? v : SPOT * (1 + (rowsBy === "moneyness" ? v : 0));
    const cands = byDelta ? otm.filter((r) => (v < 0) === (r.right === "P")) : otm;
    const dist = (r: OptionRow) => Math.abs((byDelta ? r.delta ?? 9 : r.strike) - target);
    const pick = cands.reduce((a, b) => (dist(b) < dist(a) ? b : a));
    const iv = rowsBy === "delta" && v === 0.5 ? atm : pick.iv;
    const wing = x === 0 && rowsBy === "delta" && Math.abs(v) === 0.1;
    const reason = iv === null ? "no IV" : wing || !(pick.bid! > 0) ? "no bid" : !pick.oi ? "no open interest" : null;
    const value = iv === null ? null : mode === "iv" ? iv : mode === "vs_atm" ? (atm === null ? null : round(iv - atm, 4)) : round(0.004 * Math.sin(x + v * 10), 4);
    return { iv, value, osi: pick.osi, strike: pick.strike, right: pick.right, bid: pick.bid, ask: pick.ask, oi: pick.oi, reliable: reason === null, reason };
  }));
  return {
    symbol: sym, spot: SPOT, asof: NOW, rows_by: rowsBy, mode, rows, cells,
    expiries: exps.map((e) => ({ expiry: e.expiry, dte: e.dte, atm_iv: e.atm_iv, iv_computed: false, earnings_expiry: e.expiry === "2027-02-19" })),
    prior_date: mode === "chg_1d" ? "2026-11-18" : null, earnings: { date: "2027-01-28", reaction_date: "2027-01-29" },
    thresholds: { max_spread_of_mid: 0.4, min_oi: 1, min_dte: 1, max_dte: 366 }, source: "ibkr", delayed: true,
  };
}

export function impliedMoves(sym: string): ImpliedMoves {
  return {
    symbol: sym, spot: SPOT, asof: NOW,
    rows: expiries().map((e) => {
      const mv = e.atm_iv === null ? null : round(SPOT * e.atm_iv * Math.sqrt(Math.max(1, e.dte) / 365), 2);
      return { expiry: e.expiry, dte: e.dte, straddle: e.straddle, straddle_pct: e.straddle === null ? null : round(e.straddle / SPOT, 4), iv: e.atm_iv, iv_move: mv, iv_move_pct: mv === null ? null : round(mv / SPOT, 4), low: mv === null ? null : round(SPOT - mv), high: mv === null ? null : round(SPOT + mv), earnings_in_window: e.dte > 70 };
    }),
  };
}

export function volSeasonality(sym: string): VolSeasonality {
  reseed(4);
  return { symbol: sym, months: Array.from({ length: 12 }, (_, i) => ({ month: i + 1, avg_iv30: i === 10 ? 0.27 : null, iv_samples: i === 10 ? 12 : 0, avg_hv20: round(0.2 + rnd() * 0.08, 4), hv_years: 10 })) };
}

export function earnings(sym: string): Earnings {
  reseed(23);
  const events = Array.from({ length: 16 }, (_, i) => {
    const date = addDays("2026-10-30", -91 * i);
    const move = round((rnd() - 0.45) * 0.12, 4);
    return {
      date, timing: "amc" as const, timing_inferred: i % 5 === 3, reaction_date: addDays(date, 1), fiscal_quarter: `Q${((3 - i) % 4 + 4) % 4 + 1} ${2026 - Math.floor(i / 4)}`,
      eps_estimate: round(1.5 + rnd() * 0.5), eps_actual: round(1.55 + rnd() * 0.5), surprise_pct: round((rnd() - 0.3) * 0.1, 4),
      pre_close: round(200 + rnd() * 30), gap_pct: round(move * 0.8, 4), move_pct: move, max_move_pct: round(Math.abs(move) * 1.3, 4),
      pre_drift_5: round((rnd() - 0.5) * 0.05, 4), pre_drift_10: round((rnd() - 0.5) * 0.07, 4), post_drift_1: round((rnd() - 0.5) * 0.03, 4),
      post_drift_5: round((rnd() - 0.5) * 0.05, 4), post_drift_10: round((rnd() - 0.5) * 0.06, 4), post_drift_20: round((rnd() - 0.5) * 0.08, 4),
      implied_move_pct: i === 0 ? 0.052 : null, iv30_before: i === 0 ? 0.36 : null, iv30_after: i === 0 ? 0.25 : null,
    };
  });
  const stats = { count: 12, avg_move: 0.006, avg_abs_move: 0.038, median_abs_move: 0.033, stdev_move: 0.047, up_pct: 0.58, max_up: 0.072, max_down: -0.061, avg_abs_gap: 0.031, avg_pre_drift_5: 0.004, avg_post_drift_5: -0.002, avg_post_drift_20: 0.011 };
  return {
    symbol: sym, next: { date: "2027-01-28", timing: "amc", days: 70, implied_move_pct: 0.049, avg_abs_move_pct: 0.038, excess_implied_move_pct: 0.041, implied_vs_hist_ratio: 1.29, expiry_used: "2027-01-29" },
    events, stats, stats_all: { ...stats, count: 16 }, unavailable: null,
  };
}

export function earningsStrategies(sym: string, lookback: number, entry: number, exit: number): EarningsStrategyResult {
  reseed(lookback + entry * 3 + exit * 7 + 100);
  const names = ["long_straddle", "short_straddle", "long_strangle_5", "short_strangle_5", "long_call_atm", "long_put_atm", "short_iron_condor_5", "long_iron_fly", "call_calendar", "long_stock"];
  return {
    symbol: sym, lookback, entry, exit,
    notes: ["Model-priced legs whose expiry spans the reaction day use total variance base_iv²·T + event_var."],
    rows: names.map((n) => ({ strategy: n, label: n.replace(/_/g, " ").replace(/^\w/, (m) => m.toUpperCase()), trades: lookback, win_rate: round(rnd(), 3), avg_return: round((rnd() - 0.5) * 0.4, 4), median_return: round((rnd() - 0.5) * 0.3, 4), best: round(rnd() * 1.2, 4), worst: round(-rnd(), 4), avg_pnl: round((rnd() - 0.5) * 300), real_count: 1, model_count: lookback - 1 })),
  };
}

export function trades(sym: string, n: number): BacktestTrade[] {
  reseed(31);
  return Array.from({ length: n }, (_, i) => {
    const entry = addDays("2026-10-28", -91 * i);
    const ev = round(between(5, 15));
    const xv = round(ev * between(0.4, 1.8));
    return { symbol: sym, entry_date: entry, exit_date: addDays(entry, 3), legs: "+1 C 230 / +1 P 230", entry_value: ev * 100, exit_value: xv * 100, pnl: round((xv - ev) * 100), ret: round(xv / ev - 1, 4), pricing: i === 0 ? "real" : "model", note: null };
  });
}

export function backtest(sym: string): BacktestResult {
  const t = trades(sym, 24);
  let eq = 10000;
  return {
    trades: t,
    summary: { count: t.length, win_rate: 0.46, avg_return: 0.021, median_return: -0.04, total_return: 0.18, cagr: 0.085, sharpe: 0.61, max_drawdown: -0.22, avg_hold_days: 29, total_pnl: 1800, real_count: 1, model_count: 23 },
    equity: [...t].reverse().map((x) => ({ date: x.exit_date, value: round((eq *= 1 + (x.ret ?? 0) * 0.1)) })),
    benchmark: [...t].reverse().map((x, i) => ({ date: x.exit_date, value: 10000 + i * 120 })),
    histogram: [-0.6, -0.4, -0.2, 0, 0.2, 0.4, 0.6].map((b, i) => ({ bin_start: b, bin_end: b + 0.2, count: [2, 3, 5, 6, 4, 3, 1][i] })),
    notes: ["23 of 24 trades are model-priced: stored option prices only exist since collection began."],
  };
}

/** Key stats as the engine resolves them at the stored quote's price (P/E = price / EPS, market cap = price × shares). */
export function keyStats(sym: string): KeyStats {
  const price = quote(sym).price;
  return {
    symbol: sym, price, pe: round(price / 7.12), eps: 7.12, market_cap: round(price * 15.2e9), shares_outstanding: 15.2e9,
    high_52w: 248.1, low_52w: 168.3, avg_volume_20d: 51_300_000, avg_volume_3m: 49_800_000, sector: "Technology",
    industry_group: "Computer Hardware", industry_subgroup: "Computers", dividend_yield: 0.0045, asof: TODAY_NY,
    sources: { pe: "questrade", eps: "questrade", market_cap: "questrade", shares_outstanding: "questrade", high_52w: "questrade",
      low_52w: "questrade", avg_volume_20d: "questrade", avg_volume_3m: "questrade", sector: "questrade", industry_group: "questrade",
      industry_subgroup: "questrade", dividend_yield: "nasdaq" },
  };
}

export function dividends(sym: string): Dividends {
  const history = Array.from({ length: 12 }, (_, i) => ({ ex_date: addDays("2026-08-11", -91 * i), amount: round(0.26 - Math.floor(i / 4) * 0.01), pay_date: addDays("2026-08-14", -91 * i), record_date: addDays("2026-08-11", -91 * i), declared_date: addDays("2026-07-31", -91 * i), kind: "Cash", source: "nasdaq" as const, discrepancy: false }));
  return {
    symbol: sym, annual_rate: 1.04, yield: 0.0045, frequency: 4, growth_1y: 0.04, growth_3y: 0.042, growth_5y: 0.051,
    next: { ex_date: "2026-11-24", amount: 0.26, days: 5, yield: 0.0045, source: "nasdaq" }, history,
    capture: history.map((h, i) => ({ ex_date: h.ex_date, amount: h.amount, pre_close: 220, ex_close: round(220 - 0.3 + i * 0.05), price_return: round(-0.0014 + i * 0.0002, 5), total_return: round(-0.0002 + i * 0.0002, 5), recovery_days: i === 3 ? null : i + 1 })),
    capture_stats: { count: 12, avg_total_return: 0.0009, win_rate: 0.58, avg_recovery_days: 4.2 }, unavailable: null,
  };
}

export function seasonality(sym: string): Seasonality {
  reseed(29);
  const years = Array.from({ length: 10 }, (_, i) => 2017 + i);
  return {
    symbol: sym, years,
    monthly: years.flatMap((y) => Array.from({ length: 12 }, (_, m) => ({ year: y, month: m + 1, ret: y === 2026 && m > 10 ? null : round((rnd() - 0.45) * 0.16, 4) }))),
    by_month: Array.from({ length: 12 }, (_, m) => ({ month: m + 1, avg: round((rnd() - 0.4) * 0.05, 4), median: round((rnd() - 0.4) * 0.04, 4), pos_pct: round(0.4 + rnd() * 0.4, 3), n: 10 })),
    by_weekday: Array.from({ length: 5 }, (_, d) => ({ weekday: d, avg: round((rnd() - 0.45) * 0.004, 5), pos_pct: round(0.48 + rnd() * 0.08, 3), n: 520 })),
    current_month: 11,
  };
}

export function events(sym: string): Events {
  return {
    symbol: sym,
    items: [
      { date: "2026-11-24", kind: "ex_dividend", label: "Ex-dividend $0.26", past: false },
      { date: "2026-12-18", kind: "expiry", label: "Monthly options expiry", past: false },
      { date: "2027-01-28", kind: "earnings", label: "Q1 2027 earnings (AMC)", past: false },
      { date: "2026-10-30", kind: "earnings", label: "Q4 2026 earnings — moved +3.1%", past: true },
      { date: "2026-08-11", kind: "ex_dividend", label: "Ex-dividend $0.26", past: true },
      { date: "2026-07-30", kind: "earnings", label: "Q3 2026 earnings — moved −2.4%", past: true },
    ],
  };
}

export function straddle(sym: string): StraddleInfo {
  return {
    symbol: sym, spot: SPOT,
    rows: expiries().map((e) => ({ expiry: e.expiry, dte: e.dte, strike: 230, call_mid: e.straddle === null ? null : round(e.straddle * 0.52), put_mid: e.straddle === null ? null : round(e.straddle * 0.48), straddle: e.straddle, straddle_pct: e.straddle === null ? null : round(e.straddle / SPOT, 4), lower_be: e.straddle === null ? null : round(230 - e.straddle), upper_be: e.straddle === null ? null : round(230 + e.straddle) })),
    backtest: backtest(sym),
  };
}

export function ideas(sym: string): TradeIdeas {
  return { symbol: sym, by_edge: [...IDEAS].sort((a, b) => (b.edge ?? -9) - (a.edge ?? -9)), by_win_rate: [...IDEAS].sort((a, b) => (b.pop ?? 0) - (a.pop ?? 0)) };
}

export function fundamentals(sym: string, period: "annual" | "quarterly"): Fundamentals {
  const periods = period === "annual" ? ["FY2026", "FY2025", "FY2024", "FY2023"] : ["Q4 26", "Q3 26", "Q2 26", "Q1 26"];
  const table = (title: string, labels: string[]) => ({ title, periods, rows: labels.map((l, i) => ({ label: l, values: periods.map((_, j) => (i === 2 && j === 3 ? null : `$${(390 - i * 60 - j * 12).toFixed(0)}B`)) })) });
  return {
    symbol: sym, period, fetched_at: NOW, payback_years: 31.4,
    key_stats: [{ label: "P/E (TTM)", value: "35.2" }, { label: "EPS (TTM)", value: "$6.57" }, { label: "1y target", value: "$245.00" }, { label: "Forward P/E", value: null }, { label: "Profit margin", value: "24.3%" }],
    income: table("Income statement", ["Total revenue", "Gross profit", "Operating income", "Net income"]),
    balance: table("Balance sheet", ["Total assets", "Total liabilities", "Shareholder equity"]),
    cashflow: table("Cash flow", ["Operating cash flow", "Capital expenditures", "Free cash flow"]),
    ratios: null,
  };
}

export function peers(sym: string): ReportResult {
  const columns = [
    { key: "symbol", label: "Symbol", fmt: "symbol" as const }, { key: "name", label: "Name", fmt: "text" as const }, { key: "market_cap", label: "Mkt cap", fmt: "bigmoney" as const },
    { key: "chg_1y", label: "1y", fmt: "pct_signed" as const }, { key: "iv30", label: "IV30", fmt: "vol" as const }, { key: "iv_rank", label: "IV rank", fmt: "pct" as const }, { key: "pe", label: "P/E", fmt: "num" as const },
  ];
  return { id: "peers", title: `${sym} peers`, asof: NOW, params: {}, columns, rows: rowsFor(columns, 8, 2), truncated: false, notes: [] };
}

export function insiders(sym: string): Insiders {
  reseed(37);
  return {
    symbol: sym,
    summary: [{ label: "Buys", m3: "2", m12: "5" }, { label: "Sells", m3: "11", m12: "38" }, { label: "Net shares", m3: "-1,204,000", m12: null }],
    trades: Array.from({ length: 12 }, (_, i) => ({ insider: pick(["Doe Jane", "Smith John", "Lee Ann", "Park Min"]), relation: pick(["CEO", "CFO", "Director", null]), date: addDays(TODAY, -i * 9), transaction: pick(["Sell", "Automatic Sell", "Automatic Sell", "Buy", "Option Execute"]), shares: Math.round(between(1e3, 1e5)), price: round(between(200, 240)), value: round(between(2e5, 2e7), 0), shares_held: Math.round(between(1e5, 4e6)) })),
  };
}

export function news(sym: string): NewsItem[] {
  return Array.from({ length: 6 }, (_, i) => ({ title: `${sym} headline number ${i + 1}: market reacts to sector update`, url: `https://example.com/news/${i}`, source: pick(["Newswire", "MarketDesk", null]), published: i % 2 ? "2026-11-18T14:00:00Z" : "Nov 17, 2026", summary: i % 3 ? "A short summary of the article text." : null }));
}

export function profile(sym: string): Profile {
  return {
    symbol: sym, name: SYMBOLS.find((s) => s[0] === sym)?.[1] ?? `${sym} Holdings`, description: "Designs, manufactures and sells consumer electronics, software and services worldwide.",
    website: "https://example.com", sector: "Technology", industry: "Consumer Electronics", exchange: "NASDAQ",
    peers: SYMBOLS.slice(1, 7).map(([symbol, name]) => ({ symbol, name, kind: "stock" as const, market: "US" as const, exchange: null, currency: "USD" as const })),
  };
}

export { SPOT, pick };
