// Market-wide, user-data, calculator and backtest fixtures.
import type {
  Alert, CalcRequest, CalcResult, CompareVolatility, MarketOverview, Notification, Preset, ReportResult, Status, StrategyTemplate, SymbolRef, Watchlist,
} from "../../src/api/types";
import { between, NOW, reseed, rnd, round, rowsFor, SYMBOLS, tradingDays, walk } from "./gen";
import { REPORT_DEFS } from "./reports";
import { SPOT } from "./symbol";

export function status(): Status {
  return {
    now: NOW, market_open: true, next_sweep_at: "2026-11-19T16:00:00-05:00", universe_size: 1124, optionable_count: 1098,
    last_sweep_at: NOW, last_eod_date: "2026-11-18", iv_history_days: 12, option_eod_days: 11, earnings_backfill_progress: 0.64,
    row_counts: { symbols: 1124, daily_bars: 2_712_000, option_eod: 9_870_000, metrics_daily: 13_400, earnings: 41_200 },
    jobs: [
      { job: "sweep", started_at: "2026-11-19T15:30:00-05:00", finished_at: "2026-11-19T15:41:12-05:00", ok: true, detail: "1098 symbols" },
      { job: "eod", started_at: "2026-11-18T16:20:00-05:00", finished_at: "2026-11-18T16:48:03-05:00", ok: true, detail: null },
      { job: "earnings", started_at: "2026-11-19T05:30:00-05:00", finished_at: null, ok: null, detail: "backfill 64%" },
      { job: "prices_refresh", started_at: "2026-11-15T07:00:00-05:00", finished_at: "2026-11-15T07:22:00-05:00", ok: false, detail: "Nasdaq 429 for 3 symbols" },
    ],
    db_size_bytes: 1_840_000_000,
    sources: [
      { name: "ibkr", ok: true, last_success_at: NOW, detail: "bridge push 2 min ago" },
      { name: "nasdaq", ok: true, last_success_at: "2026-11-19T15:20:00-05:00", detail: null },
      { name: "treasury", ok: false, last_success_at: "2026-11-15T07:00:00-05:00", detail: "HTTP 503" },
      { name: "vendor", ok: null, last_success_at: null, detail: "not configured" },
    ],
    bridge: { connected: true, last_push_at: NOW, client: "optionlens-mac 0.1.0" },
  };
}

export function search(q: string): SymbolRef[] {
  const u = q.toUpperCase();
  const all: SymbolRef[] = [...SYMBOLS.map(([symbol, name]) => ({ symbol, name, kind: "stock" as const })), { symbol: "SPY", name: "SPDR S&P 500 ETF", kind: "etf" }, { symbol: "SPX", name: "S&P 500 Index", kind: "index" }];
  return [...all.filter((s) => s.symbol.startsWith(u)), ...all.filter((s) => !s.symbol.startsWith(u) && s.name.toUpperCase().includes(u))].slice(0, 20);
}

const def = (id: string) => REPORT_DEFS.find((d) => d.id === id)!;

export function overview(): MarketOverview {
  reseed(51);
  const idx: [string, string, number][] = [["SPY", "SPDR S&P 500", 668.2], ["QQQ", "Invesco QQQ", 601.4], ["IWM", "iShares Russell 2000", 238.9], ["DIA", "SPDR Dow Jones", 471.3], ["SPX", "S&P 500", 6702.1], ["NDX", "Nasdaq 100", 24611.0], ["RUT", "Russell 2000", 2391.5]];
  const days = tradingDays(252);
  const vix = walk(252, 17, 0.04);
  return {
    asof: NOW,
    indices: idx.map(([symbol, name, price]) => ({ symbol, name, price, change_pct: round((rnd() - 0.4) * 0.02, 4), iv30: symbol === "RUT" ? null : round(0.14 + rnd() * 0.08, 4), spark: walk(30, price, 0.008) })),
    vix_term: [{ tenor: "VIX9D", value: 15.2, change: -0.4 }, { tenor: "VIX", value: 16.8, change: -0.3 }, { tenor: "VIX3M", value: 18.9, change: 0.1 }, { tenor: "VIX6M", value: null, change: null }],
    vix_history: days.map((d, i) => ({ date: d, vix9d: round(vix[i] * 0.92), vix: vix[i], vix3m: round(vix[i] * 1.1), vix6m: i > 240 ? null : round(vix[i] * 1.16) })),
    sectors: ["XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC"].map((s, i) => ({ symbol: s, name: ["Technology", "Financials", "Energy", "Health Care", "Cons. Discretionary", "Cons. Staples", "Industrials", "Materials", "Utilities", "Real Estate", "Communication"][i], change_pct: round((rnd() - 0.5) * 0.03, 4), change_5d: round((rnd() - 0.5) * 0.06, 4), change_1m: i === 9 ? null : round((rnd() - 0.5) * 0.1, 4) })),
    gainers: rowsFor(def("market-movers").columns, 8, 1), losers: rowsFor(def("market-movers").columns, 8, 2),
    unusual_options: rowsFor(def("unusual-option-volume").columns, 10, 3), iv_movers: rowsFor(def("iv-movers").columns, 8, 4),
    earnings_today: rowsFor(def("earnings-calendar").columns, 5, 5), earnings_tomorrow: [],
    watchlist: watchlistTable(1).rows,
    empty_notes: { earnings_tomorrow: "No tracked company reports on the next session (2026-11-20)." },
  };
}

export const WATCHLISTS: Watchlist[] = [
  { id: 1, name: "Core", symbols: ["AAPL", "MSFT", "NVDA", "SPY"], created_at: "2026-11-01T10:00:00-05:00" },
  { id: 2, name: "Earnings plays", symbols: ["NFLX", "TSLA"], created_at: "2026-11-05T10:00:00-05:00" },
];

export function watchlistTable(id: number): ReportResult {
  const w = WATCHLISTS.find((x) => x.id === id) ?? WATCHLISTS[0];
  const columns = [
    { key: "symbol", label: "Symbol", fmt: "symbol" as const }, { key: "name", label: "Name", fmt: "text" as const }, { key: "price", label: "Price", fmt: "money" as const },
    { key: "change_pct", label: "% Chg", fmt: "pct_signed" as const }, { key: "iv30", label: "IV30", fmt: "vol" as const }, { key: "iv_rank", label: "IV rank", fmt: "pct" as const }, { key: "next_earnings", label: "Next earnings", fmt: "date" as const },
  ];
  const rows = rowsFor(columns, w.symbols.length, id).map((r, i) => ({ ...r, symbol: w.symbols[i] }));
  return { id: "watchlist", title: w.name, asof: NOW, params: {}, columns, rows, truncated: false, notes: [] };
}

export const PRESETS: Preset[] = [
  { id: 1, report_id: "unusual-option-volume", name: "Big call flow", created_at: NOW, payload: { params: { min_rel_volume: 3, min_volume: 5000, lookback_days: 0 }, filters: { pct_call: { min: 0.7 } }, sort: { key: "option_volume", dir: "desc" }, hidden: [] } },
];

export const ALERTS: Alert[] = [
  { id: 1, kind: "price_above", symbol: "AAPL", params: { value: 240 }, enabled: true, cooldown_min: 240, last_fired_at: null, created_at: NOW },
  { id: 2, kind: "iv_rank_above", symbol: null, params: { value: 0.8, watchlist_id: 1 }, enabled: false, cooldown_min: 240, last_fired_at: "2026-11-18T11:00:00-05:00", created_at: NOW },
  { id: 3, kind: "unusual_volume", symbol: "NVDA", params: {}, enabled: true, cooldown_min: 60, last_fired_at: null, created_at: NOW },
];

export const NOTIFICATIONS: Notification[] = [
  { id: 11, ts: NOW, alert_id: 1, title: "AAPL above $230", body: "Price 231.42 crossed 230.00", link: "/s/AAPL", read: false },
  { id: 12, ts: "2026-11-19T08:00:00-05:00", alert_id: null, title: "Morning digest", body: "3 watchlist names report earnings this week.", link: "/reports/earnings-calendar", read: false },
  { id: 13, ts: "2026-11-18T16:45:00-05:00", alert_id: null, title: "Close digest", body: "Unusual volume: NVDA, AMD, TSLA", link: null, read: true },
];

export const TEMPLATES: StrategyTemplate[] = [
  { id: "long-call", label: "Long call", family: "Single", description: "Buy one ATM call", legs: [{ kind: "option", right: "C", qty: 1, strike_offset_pct: 0 }] },
  { id: "bull-call-spread", label: "Bull call spread", family: "Vertical", description: "Buy ATM call, sell 5% OTM call", legs: [{ kind: "option", right: "C", qty: 1, strike_offset_pct: 0 }, { kind: "option", right: "C", qty: -1, strike_offset_pct: 0.05 }] },
  { id: "iron-butterfly-short", label: "Short iron butterfly", family: "Condor / fly", description: "Short ATM straddle with 5% wings", legs: [
    { kind: "option", right: "P", qty: 1, strike_offset_pct: -0.05 }, { kind: "option", right: "P", qty: -1, strike_offset_pct: 0 },
    { kind: "option", right: "C", qty: -1, strike_offset_pct: 0 }, { kind: "option", right: "C", qty: 1, strike_offset_pct: 0.05 }] },
  { id: "call-calendar", label: "Call calendar", family: "Calendar", description: "Sell front ATM call, buy next expiry", legs: [{ kind: "option", right: "C", qty: -1, strike_offset_pct: 0, expiry_index: 0 }, { kind: "option", right: "C", qty: 1, strike_offset_pct: 0, expiry_index: 1 }] },
  { id: "covered-call", label: "Covered call", family: "Stock", description: "Long 100 shares, short 5% OTM call", legs: [{ kind: "stock", qty: 100 }, { kind: "option", right: "C", qty: -1, strike_offset_pct: 0.05 }] },
];

export function calc(req: CalcRequest): CalcResult {
  const n = 61;
  const prices = Array.from({ length: n }, (_, i) => round(SPOT * (0.8 + (0.4 * i) / (n - 1))));
  const legs = req.legs.map((l) => ({ ...l, price: l.price ?? (l.kind === "stock" ? SPOT : 5.2), iv: l.iv ?? (l.kind === "stock" ? null : 0.27), mid: l.kind === "stock" ? SPOT : 5.2 }));
  const payoff = (s: number) => legs.reduce((acc, l) => {
    const intr = l.kind === "stock" ? s : Math.max(0, l.right === "C" ? s - (l.strike ?? SPOT) : (l.strike ?? SPOT) - s);
    return acc + l.qty * (intr - l.price) * (l.kind === "stock" ? 1 : 100);
  }, 0);
  const shift = (req.iv_shift ?? 0) * 500;
  const pnl_expiry = prices.map((s) => round(payoff(s)));
  const pnl_eval = prices.map((s, i) => round(pnl_expiry[i] * 0.7 + shift + (s - SPOT) * 2));
  const pnl_today = prices.map((s, i) => round(pnl_expiry[i] * 0.4 + shift + (s - SPOT) * 3));
  const be: number[] = [];
  for (let i = 1; i < n; i++) if (Math.sign(pnl_expiry[i]) !== Math.sign(pnl_expiry[i - 1])) be.push(prices[i]);
  return {
    spot: SPOT, asof: NOW, legs, cost: round(legs.reduce((a, l) => a + l.qty * l.price * (l.kind === "stock" ? 1 : 100), 0)), prices, pnl_expiry, pnl_eval, pnl_today, breakevens: be,
    max_profit: legs.some((l) => l.kind === "option" && l.right === "C" && l.qty > 0 && !legs.some((m) => m.right === "C" && m.qty < 0)) ? null : Math.max(...pnl_expiry),
    max_loss: Math.min(...pnl_expiry), pop: 0.43, ev_iv: -12.4, ev_hv: 18.9, greeks: { delta: 41.2, gamma: 2.1, theta: -9.8, vega: 22.5 },
    table: prices.filter((_, i) => i % 4 === 0).map((p, i) => ({ price: p, change_pct: round(p / SPOT - 1, 4), pnl_expiry: pnl_expiry[i * 4], pnl_eval: pnl_eval[i * 4] })),
    expiry_used: req.legs.find((l) => l.expiry)?.expiry ?? null,
  };
}

export function compareVolatility(symbols: string[]): CompareVolatility {
  const days = tradingDays(120);
  return {
    symbols,
    series: symbols.map((s, k) => { reseed(60 + k); return { symbol: s, points: days.map((d, i) => ({ date: d, iv30: i > 107 ? round(0.18 + k * 0.05 + rnd() * 0.02, 4) : null, hv20: round(0.15 + k * 0.04 + rnd() * 0.04, 4) })) }; }),
    table: symbols.map((s, k) => ({ symbol: s, iv30: round(0.2 + k * 0.05, 4), hv20: round(0.17 + k * 0.04, 4), iv_rank: null, iv_hv_ratio: round(1.1 + k * 0.05, 2) })),
  };
}

export function compareStraddle(symbols: string[], dte: number): ReportResult {
  const columns = [
    { key: "symbol", label: "Symbol", fmt: "symbol" as const }, { key: "expiry", label: "Expiry", fmt: "date" as const }, { key: "dte", label: "DTE", fmt: "int" as const },
    { key: "straddle", label: "Straddle", fmt: "money" as const }, { key: "straddle_pct", label: "Straddle %", fmt: "pct" as const }, { key: "atm_iv", label: "ATM IV", fmt: "vol" as const },
  ];
  reseed(dte);
  return { id: "compare-straddle", title: "Straddle comparison", asof: NOW, params: { symbols, dte }, columns, rows: symbols.map((s) => ({ symbol: s, expiry: "2026-12-18", dte: 29, straddle: round(between(3, 30)), straddle_pct: round(between(0.03, 0.09), 4), atm_iv: round(between(0.15, 0.5), 4) })), truncated: false, notes: [] };
}
