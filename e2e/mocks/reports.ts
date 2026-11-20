import type { ColumnDef, ParamDef, ReportDef, ReportResult } from "../../src/api/types";
import { NOW, rowsFor, tradingDays, round, between, reseed } from "./gen";

const C = (key: string, label: string, fmt: ColumnDef["fmt"], extra: Partial<ColumnDef> = {}): ColumnDef => ({ key, label, fmt, ...extra });

const BASE: ColumnDef[] = [C("symbol", "Symbol", "symbol"), C("name", "Name", "text"), C("price", "Price", "money"), C("change_pct", "% Chg", "pct_signed")];
const STRAT_PARAMS: ParamDef[] = [
  { key: "dte_min", label: "DTE min", type: "int", default: 7, min: 0 },
  { key: "dte_max", label: "DTE max", type: "int", default: 60, min: 0 },
  { key: "symbols", label: "Symbols", type: "symbols", default: [] },
  { key: "watchlist", label: "Watchlist", type: "watchlist", default: null },
  { key: "min_oi", label: "Min OI", type: "int", default: 100 },
  { key: "max_spread_pct", label: "Max spread %", type: "number", default: 0.1, step: 0.01 },
  { key: "pricing", label: "Pricing", type: "select", default: "natural", options: [{ value: "natural", label: "Natural" }, { value: "mid", label: "Mid" }] },
  { key: "earnings_filter", label: "Earnings", type: "select", default: "any", options: [{ value: "any", label: "Any" }, { value: "exclude", label: "Exclude" }, { value: "include", label: "Include" }] },
];
const STRAT_COLS: ColumnDef[] = [
  C("symbol", "Symbol", "symbol"), C("legs", "Legs", "text"), C("dte", "DTE", "int"), C("cost", "Credit", "money"),
  C("max_profit", "Max profit", "money"), C("max_loss", "Max loss", "money"), C("breakeven", "Breakeven", "money"),
  C("return_on_risk", "Return on risk", "pct", { group: "Returns" }), C("annualized", "Annualized", "pct", { group: "Returns" }),
  C("pop", "PoP", "pct", { group: "Probability" }), C("ev_iv", "EV (IV)", "money", { group: "Probability" }), C("ev_hv", "EV (HV)", "money", { group: "Probability" }),
  C("net_delta", "Net Δ", "num2", { group: "Greeks", default_hidden: true }), C("theta", "Θ", "num2", { group: "Greeks", default_hidden: true }),
  C("iv", "IV", "vol"), C("iv_rank", "IV rank", "pct"), C("earnings_before_expiry", "Earnings < exp", "bool"),
];

export const REPORT_DEFS: ReportDef[] = [
  {
    id: "unusual-option-volume", title: "Unusual option volume", category: "Options",
    description: "Symbols whose option volume today is far above their 20-day average.",
    params: [
      { key: "min_rel_volume", label: "Min rel. volume", type: "number", default: 2, step: 0.5 },
      { key: "min_volume", label: "Min volume", type: "int", default: 1000 },
      { key: "lookback_days", label: "Lookback days", type: "int", default: 0, min: 0, max: 10, help: "When > 0, returns stored historic snapshots" },
    ],
    columns: [
      ...BASE.slice(0, 2), C("market_cap", "Mkt cap", "bigmoney"), C("sector", "Sector", "text"),
      C("option_volume", "Option vol", "int"), C("avg_volume_20d", "Avg vol 20d", "int"), C("rel_volume", "Rel vol", "ratio"),
      C("pct_call", "% Call", "pct"), C("pct_put", "% Put", "pct"), C("call_premium", "$ Call", "bigmoney"), C("put_premium", "$ Put", "bigmoney"),
      C("call_vol_otm", "Call OTM", "int", { group: "Volume by moneyness", default_hidden: true }), C("put_vol_otm", "Put OTM", "int", { group: "Volume by moneyness", default_hidden: true }),
      C("net_delta", "Net delta (vol×Δ)", "int", { description: "Proxy: unsigned by trade side" }), C("sentiment", "Sentiment", "badge"),
      C("price", "Price", "money"), C("change_pct", "% Chg", "pct_signed"), C("next_earnings", "Next earnings", "date"),
      C("iv30", "IV30", "vol"), C("iv_rank", "IV rank", "pct"), C("in_watchlist", "In watchlist", "bool"),
    ],
    default_sort: { key: "rel_volume", dir: "desc" },
  },
  {
    id: "iv-rankings", title: "IV rankings", category: "Options", description: "Implied volatility level, rank and percentile across the universe.",
    params: [{ key: "sort", label: "Sort by", type: "select", default: "iv_rank", options: [{ value: "iv_rank", label: "IV rank" }, { value: "iv30", label: "IV30" }] }],
    columns: [C("symbol", "Symbol", "symbol"), C("iv30", "IV30", "vol"), C("iv_rank", "IV rank", "pct"), C("iv_pct", "IV pct", "pct"), C("iv30_chg_1d", "1d chg (pts)", "vol"), C("hv20", "HV20", "vol"), C("iv_hv", "IV/HV", "ratio"), C("term_slope", "Term slope", "vol")],
  },
  {
    id: "oi-changes", title: "Open interest changes", category: "Options", description: "Contracts with the largest day-over-day OI change.",
    params: [{ key: "min_change", label: "Min change", type: "int", default: 1000 }],
    columns: [C("symbol", "Symbol", "symbol"), C("osi", "Contract", "osi"), C("oi", "OI", "int"), C("oi_prev", "Prior OI", "int"), C("oi_change", "Change", "int"), C("oi_change_pct", "% chg", "pct_signed"), C("volume", "Volume", "int"), C("iv", "IV", "vol")],
  },
  {
    id: "options-broad-view", title: "Options broad view", category: "Options", description: "Call/put activity by sector.", params: [],
    columns: [C("sector", "Sector", "text"), C("call_volume", "Call vol", "int"), C("put_volume", "Put vol", "int"), C("pc_ratio", "P/C", "ratio"), C("avg_iv30", "Avg IV30", "vol")],
  },
  { id: "covered-calls", title: "Covered calls", category: "Strategies", description: "Buy-write candidates by static and if-called return.", params: STRAT_PARAMS, columns: STRAT_COLS, default_sort: { key: "annualized", dir: "desc" } },
  { id: "bull-put-spreads", title: "Bull put spreads (credit)", category: "Strategies", description: "Short put verticals ranked by return on risk.", params: STRAT_PARAMS, columns: STRAT_COLS },
  { id: "iron-condors-short", title: "Short iron condors", category: "Strategies", description: "Short inner strikes, long wings.", params: STRAT_PARAMS, columns: STRAT_COLS },
  {
    id: "earnings-calendar", title: "Earnings calendar", category: "Earnings", description: "Upcoming reporters with implied vs historical moves.",
    params: [{ key: "start", label: "From", type: "date", default: "2026-11-19" }, { key: "end", label: "To", type: "date", default: "2026-11-26" }],
    columns: [C("date", "Date", "date"), C("time", "Time", "text"), C("symbol", "Symbol", "symbol"), C("name", "Name", "text"), C("market_cap", "Mkt cap", "bigmoney"),
      C("eps_est", "EPS est", "num2"), C("implied_move", "Implied move", "pct"), C("avg_abs_move", "Avg |move| (8)", "pct"), C("implied_hist_ratio", "Impl/Hist", "ratio"), C("iv_rank", "IV rank", "pct")],
  },
  {
    id: "earnings-option-strategy-screener", title: "Earnings option strategy screener", category: "Earnings", description: "Backtested earnings strategies for upcoming reporters.",
    params: [{ key: "lookback", label: "Lookback", type: "select", default: "8", options: [{ value: "4", label: "4" }, { value: "8", label: "8" }, { value: "12", label: "12" }] }],
    columns: [C("symbol", "Symbol", "symbol"), C("strategy", "Strategy", "text"), C("trades", "# trades", "int"), C("win_rate", "Win rate", "pct"), C("avg_return", "Avg return", "pct_signed"), C("pricing", "Pricing", "badge")],
  },
  {
    id: "term-structure-screener", title: "Term structure", category: "Volatility", description: "Front vs back implied volatility; backwardation flags.", params: [],
    columns: [C("symbol", "Symbol", "symbol"), C("iv30", "IV30", "vol"), C("iv60", "IV60", "vol"), C("iv90", "IV90", "vol"), C("slope", "Slope", "vol"), C("backwardation", "Backwardation", "bool")],
  },
  {
    id: "iv-movers", title: "IV movers", category: "Volatility", description: "Largest one-day IV30 changes.", params: [],
    columns: [C("symbol", "Symbol", "symbol"), C("iv30", "IV30", "vol"), C("iv30_chg_1d", "1d chg (pts)", "vol"), C("iv30_chg_pct", "1d chg %", "pct_signed"), C("iv_rank", "IV rank", "pct")],
  },
  {
    id: "stock-screener", title: "Stock screener", category: "Stocks", description: "30+ technical, volatility and option-activity columns.",
    params: [{ key: "min_market_cap", label: "Min mkt cap", type: "number", default: 1e9 }, { key: "sector", label: "Sector", type: "text", default: "" }],
    columns: [...BASE, C("chg_5d", "5d", "pct_signed", { group: "Performance" }), C("chg_1m", "1m", "pct_signed", { group: "Performance" }), C("chg_ytd", "YTD", "pct_signed", { group: "Performance" }),
      C("volume", "Volume", "int"), C("rel_volume", "Rel vol", "ratio"), C("market_cap", "Mkt cap", "bigmoney"), C("sector", "Sector", "text"),
      C("iv30", "IV30", "vol", { group: "Volatility" }), C("iv_rank", "IV rank", "pct", { group: "Volatility" }), C("hv20", "HV20", "vol", { group: "Volatility" }),
      C("rsi14", "RSI14", "num", { group: "Technicals" }), C("beta", "Beta", "num2", { group: "Technicals", default_hidden: true })],
  },
  {
    id: "market-movers", title: "Market movers", category: "Stocks", description: "Top gainers, losers and most active.",
    params: [{ key: "side", label: "Side", type: "select", default: "gainers", options: [{ value: "gainers", label: "Gainers" }, { value: "losers", label: "Losers" }, { value: "active", label: "Most active" }] }],
    columns: [...BASE, C("volume", "Volume", "int"), C("rel_volume", "Rel vol", "ratio")],
  },
  {
    id: "performance-by-sector", title: "Performance by sector", category: "Stocks", description: "Sector returns over several windows.", params: [],
    columns: [C("sector", "Sector", "text"), C("chg_1d", "1d", "pct_signed"), C("chg_1m", "1m", "pct_signed"), C("chg_ytd", "YTD", "pct_signed")],
  },
  {
    id: "dividend-calendar", title: "Dividend calendar", category: "Dividends", description: "Upcoming ex-dividend dates.",
    params: [{ key: "start", label: "From", type: "date", default: "2026-11-19" }, { key: "end", label: "To", type: "date", default: "2026-12-19" }],
    columns: [C("ex_date", "Ex-date", "date"), C("symbol", "Symbol", "symbol"), C("name", "Name", "text"), C("amount", "Amount", "money"), C("yield", "Yield", "pct"), C("pay_date", "Pay date", "date")],
  },
];

export function reportResult(id: string, params: Record<string, string>): ReportResult | null {
  const def = REPORT_DEFS.find((d) => d.id === id);
  if (!def) return null;
  const n = id === "stock-screener" ? 600 : id === "options-broad-view" || id === "performance-by-sector" ? 11 : 60;
  const effective = Object.fromEntries(def.params.map((p) => [p.key, params[p.key] ?? p.default]));
  const extra = id === "options-broad-view" ? (() => {
    reseed(3);
    return { pc_history: tradingDays(30).map((d) => ({ date: d, pc_volume: round(between(0.6, 1.2)), pc_premium: round(between(0.5, 1.4)) })) };
  })() : undefined;
  return {
    id, title: def.title, asof: NOW, params: effective, columns: def.columns,
    rows: id === "covered-calls" ? rowsFor(def.columns, n, id.length).map((r, i) => ({ ...r, source: i % 3 === 0 ? "reconstructed" : "ibkr" })) : rowsFor(def.columns, n, id.length),
    truncated: false,
    notes: id === "unusual-option-volume" ? ["Volume is per-contract daily totals; it does not identify individual trades.", "Net delta is volume × delta, unsigned by trade side."]
      : id === "covered-calls" ? ["Covers the chain universe only (watchlists, the most liquid symbols and recently opened symbols): 152 symbols.", "Mid prices."] : [],
    ...(extra ? { extra } : {}),
  };
}
