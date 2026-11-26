// Market-wide, user-data, calculator and backtest fixtures.
import type {
  Alert, CalcRequest, CalcResult, CompareVolatility, MarketOverview, Notification, Preset, QuestradeStatus, ReportResult, Status, StrategyTemplate, SymbolRef, Watchlist,
} from "../../src/api/types";
import { between, NOW, reseed, rnd, round, rowsFor, SYMBOLS, tradingDays, walk } from "./gen";
import { REPORT_DEFS } from "./reports";
import { NO_CHAIN, SPOT } from "./symbol";

export function status(): Status {
  return {
    now: NOW, market_open: true, next_sweep_at: "2026-11-19T16:00:00-05:00", universe_size: 1124, optionable_count: 1098,
    options_listed_count: 1110, no_options_count: 14,
    last_sweep_at: NOW, last_eod_date: "2026-11-18", iv_history_days: 12, option_eod_days: 11, earnings_backfill_progress: 0.64, earnings_backfill_ca_progress: 0.3,
    row_counts: { symbols: 1124, daily_bars: 2_712_000, option_eod: 9_870_000, metrics_daily: 13_400, earnings: 41_200 },
    jobs: [
      { job: "sweep", started_at: "2026-11-19T15:30:00-05:00", finished_at: "2026-11-19T15:41:12-05:00", ok: true, detail: "1098 symbols" },
      { job: "eod", started_at: "2026-11-18T16:20:00-05:00", finished_at: "2026-11-18T16:48:03-05:00", ok: true, detail: null },
      { job: "eod", started_at: "2026-11-17T16:20:00-05:00", finished_at: "2026-11-17T16:21:00-05:00", ok: false,
        detail: "2026-11-17: nothing archived — no closing chain was stored after the close (is IB Gateway connected with the bridge on between the close and 20:00 ET?)" },
      { job: "earnings", started_at: "2026-11-19T05:30:00-05:00", finished_at: null, ok: null, detail: "backfill 64%" },
      { job: "prices_refresh", started_at: "2026-11-15T07:00:00-05:00", finished_at: "2026-11-15T07:22:00-05:00", ok: false, detail: "Nasdaq 429 for 3 symbols" },
    ],
    db_size_bytes: 1_840_000_000,
    sources: [
      { name: "ibkr", ok: true, last_success_at: NOW, detail: "bridge push 2 min ago" },
      { name: "nasdaq", ok: true, last_success_at: "2026-11-19T15:20:00-05:00", detail: null },
      { name: "treasury", ok: false, last_success_at: "2026-11-15T07:00:00-05:00", detail: "HTTP 503" },
      { name: "questrade", ok: null, last_success_at: null, detail: "not connected (Settings → Data sources) · 0/10,500 calls this hour" },
      { name: "tmx", ok: true, last_success_at: "2026-11-19T05:41:00-05:00", detail: "TMX Money: Canadian listings' earnings dates (first source; unofficial endpoint)" },
      { name: "yahoo", ok: false, last_success_at: null, detail: "Yahoo Finance: Canadian listings' earnings dates (fallback; unofficial endpoint) · 429 https://query2.finance.yahoo.com/v1/test/getcrumb" },
    ],
    bridge: { connected: true, last_push_at: NOW, client: "optionlens-mac 0.1.0" },
  };
}

export function search(q: string): SymbolRef[] {
  const u = q.toUpperCase();
  const us = { market: "US", exchange: null, currency: "USD" } as const;
  const all: SymbolRef[] = [...SYMBOLS.map(([symbol, name]) => ({ symbol, name, kind: "stock" as const, ...us })), { symbol: "SPY", name: "SPDR S&P 500 ETF", kind: "etf", ...us }, { symbol: "SPLG", name: null, kind: "etf", ...us }, { symbol: "SPX", name: "S&P 500 Index", kind: "index", ...us }];
  return [...all.filter((s) => s.symbol.startsWith(u)), ...all.filter((s) => !s.symbol.startsWith(u) && (s.name ?? "").toUpperCase().includes(u))].slice(0, 20);
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
    sectors: ["XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC"].map((s, i) => ({ symbol: s, name: ["Technology", "Financials", "Energy", "Health Care", "Consumer Discretionary", "Consumer Staples", "Industrials", "Materials", "Utilities", "Real Estate", "Communication Services"][i], change_pct: round((rnd() - 0.5) * 0.03, 4), change_5d: round((rnd() - 0.5) * 0.06, 4), change_1m: i === 9 ? null : round((rnd() - 0.5) * 0.1, 4) })),
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

/** The mock's next earnings (symbol.ts overview: 2027-01-28 after the close) react on this session. */
const EARNINGS = { date: "2027-01-28", reaction: "2027-01-29", move: 0.051, base_iv: 0.22 };

/** Like the engine: the legs' given prices / IVs (else mid 5.20, IV 27%), the given spot (else SPOT) and price range;
 * PoP / EV weigh the expiry P/L by a normal around the spot, so prices and spot move them. A leg expiring on or after
 * the earnings reaction is "crushed" to 80% of its IV after it (unless earnings_crush is false); EV(HV30) is withheld
 * when the reaction falls before the first expiry. */
export function calc(req: CalcRequest): CalcResult {
  const n = 61;
  const spot = req.spot ?? SPOT;
  const [lo, hi] = req.price_range ?? [spot * 0.8, spot * 1.2];
  const prices = Array.from({ length: n }, (_, i) => round(lo + ((hi - lo) * i) / (n - 1)));
  const shiftIv = req.iv_shift ?? 0;
  const firstExp = req.legs.map((l) => l.expiry).filter((e): e is string => !!e).sort()[0] ?? null;
  const inEvent = (l: { kind: string; expiry?: string }) => l.kind === "option" && !!l.expiry && l.expiry >= EARNINGS.reaction;
  const event = req.legs.some(inEvent) ? {
    earnings_date: EARNINGS.date, reaction_date: EARNINGS.reaction, implied_move_pct: EARNINGS.move, base_iv: EARNINGS.base_iv,
    method: "pre_event" as const, expiries_used: ["2027-01-15", "2027-02-19"], applied: req.earnings_crush !== false,
    reason: req.earnings_crush === false ? "switched off (earnings_crush=false)" : null,
  } : null;
  const crushed = (l: { kind: string; expiry?: string }) => !!event?.applied && inEvent(l);
  const legs = req.legs.map((l) => {
    const iv = l.iv ?? (l.kind === "stock" ? null : 0.27);
    const base = iv !== null && crushed(l) ? round(iv * 0.8, 4) : null;
    const alive = (at: string | null) => l.kind === "option" && iv !== null && !!l.expiry && (!at || l.expiry > at);
    const vega = l.kind === "option" ? round((22.5 / req.legs.filter((m) => m.kind === "option").length) * Math.sign(l.qty), 4) : 0;
    return {
      ...l, price: l.price ?? (l.kind === "stock" ? spot : 5.2), iv, mid: l.kind === "stock" ? spot : 5.2,
      iv_eval: alive(null) ? round((iv ?? 0) + shiftIv, 4) : null,
      iv_expiry: alive(firstExp) ? round((base ?? iv ?? 0) + shiftIv, 4) : null,
      iv_base: base, base_floored: false, greeks: { delta: 0, gamma: 0, theta: 0, vega },
    };
  });
  const payoff = (s: number) => legs.reduce((acc, l) => {
    const intr = l.kind === "stock" ? s : Math.max(0, l.right === "C" ? s - (l.strike ?? SPOT) : (l.strike ?? SPOT) - s);
    return acc + l.qty * (intr - l.price) * (l.kind === "stock" ? 1 : 100);
  }, 0);
  const shift = (req.iv_shift ?? 0) * 500;
  const pnl_expiry = prices.map((s) => round(payoff(s)));
  const pnl_eval = prices.map((s, i) => round(pnl_expiry[i] * 0.7 + shift + (s - spot) * 2));
  const pnl_today = prices.map((s, i) => round(pnl_expiry[i] * 0.4 + shift + (s - spot) * 3));
  const be: number[] = [];
  for (let i = 1; i < n; i++) if (Math.sign(pnl_expiry[i]) !== Math.sign(pnl_expiry[i - 1])) be.push(prices[i]);
  const w = prices.map((s) => Math.exp(-(((s - spot) / (0.08 * spot)) ** 2) / 2));
  const wsum = w.reduce((a, b) => a + b, 0);
  const ev = w.reduce((a, x, i) => a + x * payoff(prices[i]), 0) / wsum;
  const pop = w.reduce((a, x, i) => a + (payoff(prices[i]) > 0 ? x : 0), 0) / wsum;
  return {
    spot, asof: NOW, legs, cost: round(legs.reduce((a, l) => a + l.qty * l.price * (l.kind === "stock" ? 1 : 100), 0)), prices, pnl_expiry, pnl_eval, pnl_today, breakevens: be,
    max_profit: legs.some((l) => l.kind === "option" && l.right === "C" && l.qty > 0 && !legs.some((m) => m.right === "C" && m.qty < 0)) ? null : Math.max(...pnl_expiry),
    max_loss: Math.min(...pnl_expiry), pop: round(pop, 4), ev_iv: round(ev),
    ev_hv: firstExp && firstExp >= EARNINGS.reaction ? null : round(ev + 31.3),
    ev_hv_reason: firstExp && firstExp >= EARNINGS.reaction ? `Earnings react ${EARNINGS.reaction}, before ${firstExp}: HV30 holds no earnings move` : null,
    greeks: { delta: 41.2, gamma: 2.1, theta: -9.8, vega: round(legs.reduce((a, l) => a + l.greeks.vega, 0), 4) },
    vega_by_expiry: [...new Set(legs.filter((l) => l.kind === "option" && l.expiry).map((l) => l.expiry as string))].sort()
      .map((e) => ({ expiry: e, vega: round(legs.filter((l) => l.expiry === e).reduce((a, l) => a + l.greeks.vega, 0), 4) })),
    event,
    table: prices.filter((_, i) => i % 4 === 0).map((p, i) => ({ price: p, change_pct: round(p / spot - 1, 4), pnl_expiry: pnl_expiry[i * 4], pnl_eval: pnl_eval[i * 4] })),
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
  return { id: "compare-straddle", title: "Compare straddles", asof: NOW, params: { symbols, dte }, columns,
    // like the engine: a symbol without a stored chain comes back as its symbol only
    rows: symbols.map((s) => (NO_CHAIN[s] ? { symbol: s } : { symbol: s, expiry: "2026-12-18", dte: 29, straddle: round(between(3, 30)), straddle_pct: round(between(0.03, 0.09), 4), atm_iv: round(between(0.15, 0.5), 4) })),
    truncated: false, notes: [] };
}

/** GET /api/sources/questrade (and the answer of POST / DELETE .../token) in a given state. */
export function questradeStatus(state: QuestradeStatus["state"] = "disconnected"): QuestradeStatus {
  const connected = state === "connected";
  return {
    name: "questrade", state, refreshed_at: state === "disconnected" ? null : "2026-11-19T15:30:00-05:00",
    detail: state === "expired" ? "Questrade refused the refresh token (it expired, was already used or was revoked). Generate a new one in Questrade's API Centre and paste it here."
      : state === "error" ? "Questrade login is unreachable (ConnectError)"
      : state === "needs_attention" ? "Your Questrade app lacks the 'Retrieve delayed and real-time market data' permission. In Questrade's API Centre enable it for your personal app, generate a new manual token, and paste it here." : null,
    calls_last_hour: connected ? 1234 : 0, hour_budget: 10500, remaining: connected ? 13766 : null,
    reset_at: connected ? "2026-11-19T16:00:00-05:00" : null, paused_until: null,
    delayed: connected ? false : null, expiries: 8, moneyness: 0.25,
    last_chain_at: connected ? "2026-11-19T15:41:00-05:00" : null,
    last_sweep_at: connected ? "2026-11-19T15:46:00-05:00" : null,
    last_sweep_detail: connected ? "questrade: 1140 chains, 2 not optionable, 3 not listed, 0 failed, 96s" : null,
    failures: state === "needs_attention" ? [{ reason: "403 code 1016 (missing market-data permission)", count: 1141 }] : [],
    verification: connected ? { ok: true, message: "Token OK: market data permitted", at: "2026-11-19T15:30:00-05:00" } : null,
    account_access: connected ? { state: "ok", detail: null } : { state: "not_connected", detail: "Connect Questrade in Settings → Data sources." },
  };
}
