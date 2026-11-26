// OptionLens API contract. Authoritative: server JSON must match these shapes exactly.
// Conventions: dates "YYYY-MM-DD"; timestamps ISO-8601 with offset; percentages and vols are
// fractions (0.25 = 25%); `null` = unknown / insufficient history (never fabricated).

export type ISODate = string;
export type ISODateTime = string;
export type Right = "C" | "P";
export type Pricing = "real" | "model";
export type SymbolKind = "stock" | "etf" | "index";
/** Where a value came from (INTEGRATION.md §5). */
export type DataSource = "vendor" | "ibkr" | "questrade" | "cboe" | "nasdaq" | "tmx" | "yahoo" | "model" | "reconstructed" | "live" | "ibkr-adjusted" | "computed";

/** Markets followed (SPEC §3 "Symbols"): US listings are bare tickers (RY), Canadian ones carry the exchange suffix
 * (RY.TO on the TSX, XYZ.VN on the TSX Venture). A symbol's market fixes its currency. */
export type Market = "US" | "CA";
export type Currency = "USD" | "CAD";
export interface MarketFields {
  market: Market;
  exchange: "TSX" | "TSXV" | null;   // Canadian listing venue; null for US listings (venue not tracked)
  currency: Currency;
}
export interface SymbolRef extends MarketFields { symbol: string; name: string | null; kind: SymbolKind }

export interface Quote {
  symbol: string; ts: ISODateTime; price: number; change: number; change_pct: number;
  open: number | null; high: number | null; low: number | null; prev_close: number | null;
  volume: number | null; bid: number | null; ask: number | null; iv30: number | null;
  source?: DataSource; delayed?: boolean;
  /** Collected after its session's close (IBKR frozen data: evening, weekend, before the open). */
  after_hours?: boolean;
  /** The session's VWAP as the quote reported it (Questrade); null = not sent. */
  vwap?: number | null;
  /** Trading halted (Questrade isHalted, observed within 90 min); null = unknown. */
  halted?: boolean | null;
}

export interface Bar {
  date: ISODate; open: number; high: number; low: number; close: number; volume: number;
  /** /bars only: simple moving averages over the full stored history (null with fewer bars before this one). */
  sma20?: number | null; sma50?: number | null; sma200?: number | null;
}

/** GET /bars?interval=… (INTEGRATION.md §5 "Intraday bars"). */
export type IntradayInterval = "1m" | "5m" | "15m" | "1h";
export type IntradayRange = "1d" | "5d" | "1m" | "3m";
export interface IntradayBar {
  ts: ISODateTime; open: number; high: number; low: number; close: number; volume: number | null;   // ts: the bar's start
  /** The session VWAP after this bar (regular hours only; null in extended hours / without volume). */
  vwap?: number | null;
}
export interface TimeSpan { start: ISODateTime; end: ISODateTime }
/** A hole in the hours shown that the chart marks instead of collapsing (INTEGRATION.md §5 "Intraday bars"): `lag` —
 * the candle source has not answered these minutes yet and no live-built bar covers them; `missing` — no source could
 * fill them. */
export interface IntradayGap extends TimeSpan { kind: "lag" | "missing" }
export interface IntradayBars {
  symbol: string; interval: IntradayInterval; range: IntradayRange; extended: boolean;
  start: ISODateTime; end: ISODateTime;
  bars: IntradayBar[];              // regular hours only unless `extended`
  has_extended: boolean;            // the stored bars include pre-market / after-hours ones (the toggle is offered)
  sources: DataSource[];            // of the bars shown, most bars first
  delayed: boolean | null;          // null: unknown or mixed
  missing: TimeSpan[];              // spans no engine-side source could fill (the desktop app asks the IBKR bridge)
  vwap_source: DataSource | null;   // the bars' source when from their own VWAPs, "computed" from typical prices
  problem: string | null;           // why, when a configured source could not be used (e.g. "questrade: not connected")
  /** Seconds from the end of the source's newest bar to now (null: no source bars); the bars after it are built from
   * live quotes (source "live") while the symbol is live. Absent from older engines. */
  lag_s?: number | null;
  /** Holes to mark, ascending (absent from older engines). */
  gaps?: IntradayGap[];
}

// ---------------- status ----------------
export interface JobRun { job: string; started_at: ISODateTime; finished_at: ISODateTime | null; ok: boolean | null; detail: string | null }
export interface Status {
  now: ISODateTime; market_open: boolean; next_sweep_at: ISODateTime | null;
  universe_size: number;
  optionable_count: number;           // universe symbols with a chain stored in the last 5 sessions
  options_listed_count?: number;      // universe symbols a chain source showed to list options (a chain was stored)
  no_options_count?: number;          // universe symbols the IBKR bridge confirmed list no options
  last_sweep_at: ISODateTime | null; last_eod_date: ISODate | null;
  iv_history_days: number;            // distinct dates in metrics_daily
  option_eod_days: number;            // distinct dates in option_eod
  earnings_backfill_progress: number; // 0..1 (Nasdaq, US listings)
  earnings_backfill_ca_progress: number; // 0..1 (TMX Money, Canadian listings)
  row_counts: Record<string, number>;
  jobs: JobRun[];                     // latest run of each job + last 50 runs
  db_size_bytes: number;
  sources: { name: string; ok: boolean | null; last_success_at: ISODateTime | null; detail: string | null }[];
  bridge: {
    connected: boolean; last_push_at: ISODateTime | null; client: string | null;
    /** IBKR server farms as the bridge's last heartbeat reported them (null: not reported / not connected). */
    farms?: { market_data: FarmState; historical: FarmState; sec_def: FarmState } | null;
    /** US options market data of the account ("delayed_only": no OPRA subscription). */
    options_subscription?: "unknown" | "realtime" | "delayed_only" | null;
  };
}

export type FarmState = "ok" | "inactive" | "connecting" | "down" | "unknown";

/** A failure reason of a source and how often it occurred in the last 24 h, e.g. 1141 × "403 code 1016 (missing market-data permission)". */
export interface FailureCount { reason: string; count: number }

/** GET /api/sources/questrade, and POST / DELETE /api/sources/questrade/token (INTEGRATION.md §8). Never carries a token. */
export type QuestradeState = "disconnected" | "connected" | "expired" | "error" | "needs_attention";
export interface QuestradeStatus {
  /** `detail`: what is wrong / what to do (needs_attention: e.g. the app lacks the market-data permission). */
  name: string; state: QuestradeState; detail: string | null; refreshed_at: ISODateTime | null;
  /** Calls started in the last hour, of the engine's hourly budget (70% of Questrade's 15,000). */
  calls_last_hour: number; hour_budget: number;
  /** Questrade's last X-RateLimit-Remaining / X-RateLimit-Reset. */
  remaining: number | null; reset_at: ISODateTime | null; paused_until: ISODateTime | null;
  /** The last chain fetched was delayed data (null: none fetched since the engine started). */
  delayed: boolean | null;
  expiries: number; moneyness: number;
  last_chain_at: ISODateTime | null; last_sweep_at: ISODateTime | null; last_sweep_detail: string | null;
  /** Most frequent failure reasons of the last 24 h, most frequent first (at most 3). */
  failures: FailureCount[];
  /** The one market-data call made right after a token was saved ("Token OK: market data permitted" or what failed). */
  verification: { ok: boolean; message: string; at: ISODateTime } | null;
  /** The Portfolio's account access (INTEGRATION.md §11): needs_permission = the app lacks "Retrieve balances,
   * positions, orders and executions" (market data unaffected); `detail` says what to do. */
  account_access: { state: PortfolioSourceState; detail: string | null };
}

/** GET / PUT /api/live/mode (INTEGRATION.md §9): how Questrade keeps the screen live. `default` applies until the user chooses (`saved`). */
export type LiveMode = "stream" | "poll";
export interface LiveModeSetting { mode: LiveMode; default: LiveMode; saved: boolean }
/** The Questrade Level 1 stream connection (one per session; `ids`: the symbols / option contracts it carries per kind). */
export interface LiveStreamInfo {
  ids: { equity?: number; option?: number }; open: boolean; port: number | null;
  opened_at: ISODateTime | null; last_message_at: ISODateTime | null; messages: number; connects: number;
}
/** GET /api/live/status → `questrade`: the Questrade driver's details. `fallback`: why it polls although streaming is chosen. */
export interface QuestradeLiveDiagnostics {
  setting: LiveMode; streaming: boolean; fallback: string | null; fallback_detail: string | null; retry_at: ISODateTime | null;
  messages_per_s: number; last_message_at: ISODateTime | null; drops_recent: number; streams: LiveStreamInfo[];
}
/** GET /api/live/status: the live status (as on the live channel) plus the Questrade driver's details (null without Questrade). */
export interface LiveStatusReport {
  state: "live" | "delayed" | "connecting" | "stale" | "market_closed" | "not_connected" | "idle";
  source: "questrade" | "ibkr" | null; session: "regular" | "extended" | "closed"; mode: "poll" | "stream" | "snapshots" | null;
  interval_s: number | null; fallback: string | null; symbols: number; contracts: number; detail: string | null;
  /** Followed symbols (portfolio, watchlists, recently opened) no window shows, kept live for their 1-minute bars
   * (INTEGRATION.md §9 "Background symbols"). Absent from older engines. */
  background?: number;
  questrade: QuestradeLiveDiagnostics | null;
}

/**
 * GET / POST /api/symbol/{sym}/refresh (INTEGRATION.md §7): the on-demand refresh round of every connected chain
 * source, and the chain served now (the best one per §6 "Chain precedence").
 */
export type RefreshState = "pending" | "ok" | "no_options" | "unavailable" | "failed";
export interface RefreshSource {
  name: string; state: RefreshState; at: ISODateTime;
  /** Why (failed / unavailable), e.g. "IBKR's contract server is unavailable…" or Questrade's missing permission. */
  message: string | null;
  /** IBKR "unavailable": what the bridge waits for before it can fetch. */
  waiting_for: "contract-server" | "market-hours" | null;
  /** failed: the short form, e.g. "403 code 1016 (missing market-data permission)". */
  reason: string | null;
}
export interface RefreshStatus {
  symbol: string; started_at: ISODateTime | null; pending: boolean;
  sources: RefreshSource[];
  served: { source: DataSource | null; fetched_at: ISODateTime; delayed: boolean | null } | null;
}

// ---------------- market overview ----------------
export interface IndexCard { symbol: string; name: string; price: number | null; change_pct: number | null; iv30: number | null; spark: number[] }
export interface VixPoint { tenor: "VIX9D" | "VIX" | "VIX3M" | "VIX6M"; value: number | null; change: number | null }
export interface MarketOverview {
  asof: ISODateTime | null;
  /** Some symbols' session values are an after-hours fill (collected from IBKR frozen data after the close). */
  after_hours?: boolean;
  indices: IndexCard[];
  vix_term: VixPoint[];
  vix_history: { date: ISODate; vix9d: number | null; vix: number | null; vix3m: number | null; vix6m: number | null }[]; // 1y
  sectors: { symbol: string; name: string; change_pct: number | null; change_5d: number | null; change_1m: number | null }[];
  gainers: ReportRow[]; losers: ReportRow[];          // rows shaped like market-movers report
  unusual_options: ReportRow[];                        // top 10 of unusual-option-volume
  iv_movers: ReportRow[];                              // top 10 of iv-movers
  earnings_today: ReportRow[]; earnings_tomorrow: ReportRow[];  // earnings-calendar rows
  watchlist: ReportRow[];                              // first watchlist table rows
  empty_notes: Partial<Record<"gainers" | "losers" | "unusual_options" | "iv_movers" | "earnings_today" | "earnings_tomorrow" | "watchlist", string>>;  // why a panel is empty (only empty panels)
}

// ---------------- symbol summary ----------------
export interface ReturnsRow { d1: number | null; d5: number | null; m1: number | null; m3: number | null; m6: number | null; ytd: number | null; y1: number | null; y3: number | null }
export interface NextEarnings { date: ISODate; timing: "bmo" | "amc" | "unknown"; days: number; implied_move_pct: number | null; avg_abs_move_pct: number | null;
  source: DataSource | null }   // nasdaq (US listings) | tmx | yahoo (Canadian listings)
export interface NextDividend { ex_date: ISODate; amount: number; days: number; yield: number | null; source: DataSource | null }
/** GET /api/symbol/{sym}/key-stats (INTEGRATION.md §8 "Reference data"): one source of these figures for every page. */
export interface KeyStats {
  symbol: string; price: number | null;
  pe: number | null; eps: number | null; market_cap: number | null; shares_outstanding: number | null;
  high_52w: number | null; low_52w: number | null; avg_volume_20d: number | null; avg_volume_3m: number | null;
  sector: string | null; industry_group: string | null; industry_subgroup: string | null;
  dividend_yield: number | null;
  asof: ISODate | null;                        // the reference snapshot's date
  sources: Record<string, DataSource>;         // per figure
}
export interface SymbolSummary extends MarketFields {
  symbol: string; name: string; kind: SymbolKind; sector: string | null; industry: string | null;
  market_cap: number | null; in_universe: boolean;
  optionable: boolean | null;   // true: a chain is stored; false: every chain source serving it confirmed no listed options; null: unknown (not fetched yet)
  quote: Quote | null;
  key_stats: KeyStats;
  avg_volume_30d: number | null; rel_volume: number | null;
  high_52w: number | null; low_52w: number | null; high_3y: number | null; low_3y: number | null;
  beta_1y: number | null; corr_spy_1y: number | null;
  // iv30: latest stored IV30 (collected, else IBKR-ingested) as of iv30_asof; IV rank/percentile are of that value (null unless it is the last session's)
  iv30: number | null; iv30_asof: ISODate | null; iv_rank: number | null; iv_percentile: number | null; iv_history_days: number;
  hv20: number | null; iv_hv_ratio: number | null;
  call_volume: number | null; put_volume: number | null;
  avg_option_volume_20d: number | null; avg_option_volume_days: number;   // average over up to 20 prior stored days (days used; 0 = none)
  rel_option_volume: number | null; pc_volume_ratio: number | null; total_oi: number | null;
  returns: ReturnsRow;
  next_earnings: NextEarnings | null; next_dividend: NextDividend | null;
  next_earnings_missing: string | null;   // why there is no next earnings date (null when there is one)
  support: number[]; resistance: number[];
  rsi14: number | null; atr14_pct: number | null; sma20: number | null; sma50: number | null; sma200: number | null;
  price_1y: { date: ISODate; close: number }[];
  iv_hv_1y: { date: ISODate; iv30: number | null; hv20: number | null }[];
  ideas: TradeIdea[];       // top 3
  options_asof: ISODateTime | null;
}

// ---------------- price action ----------------
export interface PriceActionRow { date: ISODate; open: number; high: number; low: number; close: number; volume: number; change_pct: number | null; gap_pct: number | null; range_pct: number; rel_volume: number | null }
export interface PriceAction {
  rows: PriceActionRow[];   // newest first
  stats: { days: number; avg_abs_move: number; up_day_pct: number; avg_gap_abs: number; largest_up: PriceActionRow | null; largest_down: PriceActionRow | null; stdev_daily: number };
  intraday_overnight: { avg_overnight: number; avg_intraday: number; cum_overnight: number; cum_intraday: number; overnight_up_pct: number; intraday_up_pct: number; series: { date: ISODate; cum_overnight: number; cum_intraday: number }[] };
}

// ---------------- chain ----------------
export interface OptionRow {
  osi: string; expiry: ISODate; strike: number; right: Right;
  bid: number | null; ask: number | null; mid: number | null; last: number | null; change: number | null;
  volume: number | null; oi: number | null; iv: number | null;   // null = unknown (never 0)
  delta: number | null; gamma: number | null; theta: number | null; vega: number | null; rho: number | null; theo: number | null;
  itm: boolean;
  source?: DataSource;
  /** When `last` traded (null = unknown: on a weekend snapshot it can be days old). */
  last_ts?: ISODateTime | null;
  /** Where `iv` came from: the chain source, or computed by the engine from the mid / a current-session last (Black-Scholes). */
  iv_source?: IvSource | null;
  /** Where delta/gamma/theta/vega came from: the chain source, or computed by the engine from `iv`. */
  greeks_source?: DataSource | "computed" | null;
}
export type IvSource = DataSource | "computed_mid" | "computed_last";
export interface ExpiryInfo {
  expiry: ISODate; dte: number; is_monthly: boolean; atm_iv: number | null; straddle: number | null; call_volume: number; put_volume: number; call_oi: number; put_oi: number; source?: DataSource;
  /** atm_iv is from engine-computed IVs (the source gave none near the money for this expiry). */
  atm_iv_computed?: boolean;
}
export interface Chain {
  symbol: string; underlying: Quote; asof: ISODateTime;
  expiries: ExpiryInfo[];
  expiry: ISODate;                 // selected
  strikes: number[];               // sorted asc for selected expiry
  calls: OptionRow[]; puts: OptionRow[];   // selected expiry, sorted by strike
  source?: DataSource; delayed?: boolean;
  /** Fetched after its session's close (IBKR frozen data: the session's closing quotes). */
  after_hours?: boolean;
  /** The contract list was a saved one (IBKR's contract server was unavailable), fetched at contracts_listed_at:
   * expiries listed since then are missing. Quotes are current. */
  contracts_from_cache?: boolean;
  contracts_listed_at?: ISODateTime | null;
}

// ---------------- activity / OI ----------------
export interface StrikeAgg { strike: number; call_volume: number; put_volume: number; call_oi: number; put_oi: number; call_oi_change: number | null; put_oi_change: number | null }
export interface Activity {
  symbol: string; asof: ISODateTime | null; expiry: ISODate | null;
  by_expiry: ExpiryInfo[];
  by_strike: StrikeAgg[];          // selected expiry (or all expiries aggregated when expiry=all)
  top_contracts: (OptionRow & { vol_oi: number | null; premium: number })[];  // top 20 by volume
  totals: { call_volume: number; put_volume: number; call_oi: number; put_oi: number; pc_volume: number | null; pc_oi: number | null; call_premium: number; put_premium: number };
  moneyness: { call_otm: number; call_atm: number; call_itm: number; put_otm: number; put_atm: number; put_itm: number };
  history: { date: ISODate; call_volume: number | null; put_volume: number | null; call_oi: number | null; put_oi: number | null }[];
}
export interface OpenInterest {
  symbol: string; expiry: ISODate | null; prior_date: ISODate | null;  // prior_date null => no OI change available
  expiries: ExpiryInfo[];
  by_strike: StrikeAgg[];
  max_pain: { expiry: ISODate; strike: number | null }[];
  top_increases: (OptionRow & { oi_prev: number; oi_change: number })[];
  top_decreases: (OptionRow & { oi_prev: number; oi_change: number })[];
}
export interface ContractHistory {
  osi: string; symbol: string; expiry: ISODate; strike: number; right: Right;
  latest: OptionRow | null;
  history: { date: ISODate; bid: number | null; ask: number | null; mid: number | null; iv: number | null; volume: number | null; oi: number | null; delta: number | null; underlying: number | null }[];
}

// ---------------- volatility ----------------
export interface TermPoint { expiry: ISODate; dte: number; atm_iv: number | null; atm_iv_computed?: boolean }
export interface Volatility {
  symbol: string; asof: ISODateTime | null;
  iv30: number | null; iv30_asof: ISODate | null; iv_rank: number | null; iv_percentile: number | null; history_days: number;  // as SymbolSummary
  iv_high_52w: number | null; iv_low_52w: number | null;
  constant_maturity: { tenor: 30 | 60 | 90 | 120 | 180 | 360; iv: number | null; chg_1d: number | null; chg_1w: number | null }[];
  term_today: TermPoint[]; term_1w: TermPoint[] | null; term_1m: TermPoint[] | null;  // 1w/1m from option_eod
  hv: { window: 10 | 20 | 30 | 60 | 90 | 252; value: number | null }[];
  parkinson20: number | null;
  history: { date: ISODate; iv30: number | null; iv60: number | null; iv90: number | null; hv10: number | null; hv20: number | null; hv60: number | null }[];  // hv back 10y, iv from collection start
  risk_premium: { date: ISODate; iv30: number | null; hv20: number | null; realized_fwd21: number | null }[];
}
export interface Skew {
  symbol: string; expiry: ISODate; dte: number; spot: number; expiries: ExpiryInfo[];
  points: { strike: number; moneyness: number; call_iv: number | null; put_iv: number | null; call_delta: number | null; put_delta: number | null }[];
  by_delta: { delta: number; iv: number | null }[];   // put deltas -0.05..-0.5 then call 0.5..0.05 mapped to x in [-0.95..0.95]? -> x = signed delta
  history: { date: ISODate; skew25_30: number | null; put_skew_30: number | null }[];
}
export type SurfaceRows = "delta" | "moneyness" | "strike";
export type SurfaceMode = "iv" | "vs_atm" | "chg_1d";
/** One IV-surface cell: its IV, the mode's value and the nearest real contract (null fields: no contract). */
export interface SurfaceCell {
  iv: number | null;
  value: number | null;               // iv: IV; vs_atm: IV − the expiry's ATM IV; chg_1d: IV − the prior session's (fractions)
  osi: string | null; strike: number | null; right: Right | null;
  bid: number | null; ask: number | null; oi: number | null;
  reliable: boolean;                  // from reliable contracts only (see thresholds); false = greyed
  reason: string | null;              // why not reliable
}
/** IV per expiry × row from the stored chain (docs/INTEGRATION.md "IV surface"). */
export interface IvSurface {
  symbol: string; spot: number; asof: ISODateTime | null;
  rows_by: SurfaceRows; mode: SurfaceMode;
  rows: number[];                     // delta: signed OTM-side delta (puts < 0, ATM = 0.5); moneyness: K/S − 1; strike
  expiries: { expiry: ISODate; dte: number; atm_iv: number | null; iv_computed: boolean; earnings_expiry: boolean }[];
  cells: SurfaceCell[][];             // cells[row][expiry], rows ascending in strike
  prior_date: ISODate | null;         // chg_1d: the stored EOD chain compared to (null: none, or another mode)
  earnings: { date: ISODate; reaction_date: ISODate | null } | null;
  thresholds: { max_spread_of_mid: number; min_oi: number; min_dte: number; max_dte: number };
  source?: DataSource; delayed: boolean;
}
export interface ImpliedMoves {
  symbol: string; spot: number; asof: ISODateTime | null;
  rows: { expiry: ISODate; dte: number; straddle: number | null; straddle_pct: number | null; iv: number | null; iv_move: number | null; iv_move_pct: number | null; low: number | null; high: number | null; earnings_in_window: boolean }[];
}
export interface VolSeasonality {
  symbol: string;
  months: { month: number; avg_iv30: number | null; iv_samples: number; avg_hv20: number | null; hv_years: number }[];
}

// ---------------- earnings ----------------
export interface EarningsEventRow {
  date: ISODate; timing: "bmo" | "amc" | "unknown"; timing_inferred: boolean; reaction_date: ISODate | null;
  /** "BMO", "AMC", "AMC (inferred)" (unknown timing read off the price gaps; timing then holds the inferred one), "n/a". */
  timing_label?: string;
  source: DataSource | null;    // who reported the event: nasdaq | tmx | yahoo
  fiscal_quarter: string | null; eps_estimate: number | null; eps_actual: number | null; surprise_pct: number | null;
  pre_close: number | null; gap_pct: number | null; move_pct: number | null; max_move_pct: number | null;
  pre_drift_5: number | null; pre_drift_10: number | null;
  post_drift_1: number | null; post_drift_5: number | null; post_drift_10: number | null; post_drift_20: number | null;
  implied_move_pct: number | null; iv30_before: number | null; iv30_after: number | null;
}
export interface EarningsStats {
  count: number; avg_move: number | null; avg_abs_move: number | null; median_abs_move: number | null; stdev_move: number | null;
  up_pct: number | null; max_up: number | null; max_down: number | null; avg_abs_gap: number | null;
  avg_pre_drift_5: number | null; avg_post_drift_5: number | null; avg_post_drift_20: number | null;
}
export interface Earnings {
  symbol: string;
  next: (NextEarnings & { excess_implied_move_pct: number | null; implied_vs_hist_ratio: number | null; expiry_used: ISODate | null }) | null;
  events: EarningsEventRow[];   // newest first, past only (up to 40)
  stats: EarningsStats;         // over last 12
  stats_all: EarningsStats;
  unavailable: string | null;   // why a Canadian listing has no earnings data (its last TMX Money / Yahoo Finance lookup)
  next_missing: string | null;  // why there is no next earnings date (null when there is one)
}
export interface StrategyBacktestSummary {
  strategy: string; label: string; trades: number; win_rate: number | null; avg_return: number | null; median_return: number | null;
  best: number | null; worst: number | null; avg_pnl: number | null; real_count: number; model_count: number;
}
export interface EarningsStrategyResult { symbol: string; lookback: number; entry: number; exit: number; rows: StrategyBacktestSummary[]; notes: string[] }  // notes: model pricing method (event variance)

// ---------------- dividends / seasonality / events ----------------
export interface Dividends {
  symbol: string; annual_rate: number | null; yield: number | null; frequency: number | null; // payments/yr
  growth_1y: number | null; growth_3y: number | null; growth_5y: number | null;
  next: NextDividend | null;
  history: { ex_date: ISODate; amount: number; pay_date: ISODate | null; record_date: ISODate | null; declared_date: ISODate | null; kind: string;
    one_off?: boolean;       // special / one-off: left out of annual_rate, yield, frequency and growth
    source: DataSource | null;   // nasdaq | tmx (Canadian listings, in today's shares) | ibkr-adjusted (derived from IBKR adjusted closes) | questrade
    discrepancy: boolean }[];    // the sources' amounts for this ex-date differ by more than $0.01
  capture: { ex_date: ISODate; amount: number; pre_close: number | null; ex_close: number | null; price_return: number | null; total_return: number | null; recovery_days: number | null }[];
  capture_stats: { count: number; avg_total_return: number | null; win_rate: number | null; avg_recovery_days: number | null };
  unavailable: string | null;   // why a Canadian listing's history is missing or incomplete (TMX Money unreachable, paid in another currency); yield may then be Questrade's
}
export interface Seasonality {
  symbol: string;
  years: number[];
  monthly: { year: number; month: number; ret: number | null }[];
  by_month: { month: number; avg: number | null; median: number | null; pos_pct: number | null; n: number }[];
  by_weekday: { weekday: number; avg: number | null; pos_pct: number | null; n: number }[];  // 0=Mon
  current_month: number;
}
export interface Events {
  symbol: string;
  items: { date: ISODate; kind: "earnings" | "ex_dividend" | "expiry"; label: string; past: boolean }[];
}
export interface StraddleInfo {
  symbol: string; spot: number | null;
  rows: { expiry: ISODate; dte: number; strike: number; call_mid: number | null; put_mid: number | null; straddle: number | null; straddle_pct: number | null; lower_be: number | null; upper_be: number | null }[];
  backtest: BacktestResult | null;   // rolling ~30-DTE long ATM straddle held to expiry, last 2y
}

// ---------------- trade ideas ----------------
export interface TradeIdea {
  strategy: string; label: string; family: "vertical" | "condor_fly" | "single" | "straddle" | "calendar" | "seasonality";
  legs: string; expiry: ISODate | null; cost: number | null; max_profit: number | null; max_loss: number | null;
  pop: number | null; ev_hv: number | null; edge: number | null; // ev_hv / max_loss
  note: string | null;
}
export interface TradeIdeas { symbol: string; by_edge: TradeIdea[]; by_win_rate: TradeIdea[] }

// ---------------- fundamentals / company ----------------
export interface FinTable { title: string; periods: string[]; rows: { label: string; values: (string | null)[] }[] }
export interface Fundamentals {
  symbol: string; period: "annual" | "quarterly";
  key_stats: { label: string; value: string | null }[];
  income: FinTable | null; balance: FinTable | null; cashflow: FinTable | null; ratios: FinTable | null;
  payback_years: number | null; fetched_at: ISODateTime | null;
  unavailable: string | null;      // why there are no statements / key statistics (Canadian listings), else null
}
export interface Insiders {
  symbol: string;
  source: "nasdaq" | "tmx";        // nasdaq: US listings; tmx: Canadian listings (SEDI reports; shares signed, negative = disposed)
  summary: { label: string; m3: string | null; m12: string | null }[];
  trades: { insider: string; relation: string | null; date: ISODate | null; transaction: string | null; shares: number | null; price: number | null; value: number | null; shares_held: number | null }[];
}
export interface NewsItem { title: string; url: string; source: string | null; published: string | null; summary: string | null }
export interface Profile { symbol: string; name: string; description: string | null; website: string | null; sector: string | null; industry: string | null; exchange: string | null; peers: SymbolRef[] }

// ---------------- calculator ----------------
export interface Leg {
  kind: "stock" | "option";
  right?: Right; strike?: number; expiry?: ISODate;
  qty: number;                     // +long / -short; options in contracts (x100), stock in shares
  price?: number | null;           // entry price per share/contract-unit; default mid
  iv?: number | null;              // default contract IV
}
export interface CalcRequest {
  symbol: string; legs: Leg[];
  iv_shift?: number;               // vol points as fraction, e.g. 0.05 = +5 vol pts
  eval_date?: ISODate;             // P/L curve date (default today)
  price_range?: [number, number];  // default spot ±(3σ to last expiry, min 20%)
  points?: number;                 // default 121
  spot?: number;                   // the live underlying price (default: the stored chain's)
  earnings_crush?: boolean;        // model the earnings IV crush when an event is estimated (default true)
}
export interface CalcGreeks { delta: number; gamma: number; theta: number; vega: number }  // $ per 1 move / day / vol pt
/** The next earnings inside the position's life, as the calculator modelled it (null: none before the last expiry). */
export interface CalcEvent {
  earnings_date: ISODate | null; reaction_date: ISODate;   // reaction: the session that trades the report
  implied_move_pct: number | null;   // sqrt(event variance) from the ATM term structure, e.g. 0.128 = ±12.8%
  base_iv: number | null;            // the chain's ex-event ATM IV
  method: "pre_event" | "post_event_pair" | null; expiries_used: ISODate[] | null;
  applied: boolean;                  // legs were valued through the IV crush
  reason: string | null;             // why not applied (no estimate, or switched off)
}
export interface CalcLeg extends Leg {
  price: number; iv: number | null; mid: number | null; osi?: string | null;   // osi: the contract (live quotes)
  iv_eval: number | null; iv_expiry: number | null;   // vol used at the eval date / at expiry_used (incl. iv_shift); null: expired or stock
  iv_base?: number | null; base_floored?: boolean;    // calculator: the leg's ex-event IV when the crush applies to it; floored: hit the floor
  greeks: CalcGreeks;                                 // this leg's position greeks
}
export interface CalcResult {
  spot: number; asof: ISODateTime | null; legs: CalcLeg[];
  cost: number;                    // net debit (+) / credit (−) in dollars
  prices: number[];
  pnl_expiry: number[];            // at first (nearest) expiry among option legs; later legs valued by BSM
  pnl_eval: number[];              // at eval_date
  pnl_today: number[];
  breakevens: number[];            // at expiry curve
  max_profit: number | null; max_loss: number | null;  // null = unlimited
  pop: number | null; ev_iv: number | null; ev_hv: number | null;
  ev_hv_reason: string | null;     // why ev_hv is null (earnings before expiry_used: HV30 has no earnings move; no HV30)
  greeks: CalcGreeks;              // position totals
  vega_by_expiry: { expiry: ISODate; vega: number }[];   // Σ leg vega per option expiry (sums to greeks.vega)
  event: CalcEvent | null;
  table: { price: number; change_pct: number; pnl_expiry: number; pnl_eval: number }[];  // ~15 rows
  expiry_used: ISODate | null;
}
export interface StrategyTemplate { id: string; label: string; family: string; description: string; legs: { kind: "stock" | "option"; right?: Right; qty: number; strike_offset_pct?: number; expiry_index?: number }[] }

// ---------------- reports ----------------
export type ColumnFmt = "symbol" | "text" | "int" | "num" | "num2" | "money" | "bigmoney" | "pct" | "pct_signed" | "vol" | "date" | "datetime" | "bool" | "osi" | "ratio" | "badge" | "halt";
export interface ColumnDef { key: string; label: string; fmt: ColumnFmt; group?: string; description?: string; default_hidden?: boolean }
export interface ParamDef {
  key: string; label: string; type: "int" | "number" | "text" | "select" | "bool" | "date" | "symbols" | "watchlist";
  default: unknown; options?: { value: string; label: string }[]; min?: number; max?: number; step?: number; help?: string;
}
export interface ReportDef { id: string; title: string; category: "Options" | "Strategies" | "Earnings" | "Volatility" | "Stocks" | "Dividends"; description: string; params: ParamDef[]; columns: ColumnDef[]; default_sort?: { key: string; dir: "asc" | "desc" } }
export type ReportRow = Record<string, unknown> & { symbol?: string };
export interface ReportResult {
  id: string; title: string; asof: ISODateTime | null;
  params: Record<string, unknown>;     // effective params
  columns: ColumnDef[]; rows: ReportRow[]; truncated: boolean;
  /** Some rows' session values are an after-hours fill (collected from IBKR frozen data after the close). */
  after_hours?: boolean;
  notes: string[];                     // limitations / proxies, in plain words
  /** The precise method behind the notes (formulas, thresholds), shown on request. */
  method?: string[];
  /** Only when `rows` is empty: why (the run's own explanation, or the report's known reason). */
  empty_reason?: string;
  extra?: Record<string, unknown>;     // report-specific (e.g. P/C history)
  source?: DataSource;
}

// ---------------- backtests ----------------
export interface StockBacktestRequest {
  symbols: string[];
  entry: { type: "every_n_days" | "day_of_month" | "month_of_year" | "days_before_earnings" | "days_after_earnings" | "gap" | "rsi" | "sma_cross" | "new_52w"; n?: number; value?: number; direction?: "up" | "down" | "above" | "below" | "high" | "low"; month?: number; day?: number };
  exit: { hold_days?: number; at_earnings_reaction?: boolean; stop_loss?: number; take_profit?: number };
  side: "long" | "short"; start?: ISODate; end?: ISODate;
}
export interface OptionsBacktestRequest {
  symbol: string; strategy: string;              // strategy template id
  strike_rule: { type: "delta" | "pct_otm" | "atm"; value?: number }; width_pct?: number;
  entry: { type: "every_n_days" | "monthly" | "weekly"; n?: number; weekday?: number };
  target_dte: number; exit: { type: "expiry" | "dte"; dte?: number; take_profit?: number; stop_loss?: number };
  start?: ISODate; end?: ISODate; qty?: number;
}
export interface BacktestTrade {
  symbol: string; entry_date: ISODate; exit_date: ISODate; legs: string; entry_value: number; exit_value: number;
  pnl: number; ret: number | null; pricing: Pricing; note?: string | null;
}
export interface BacktestResult {
  trades: BacktestTrade[];
  summary: { count: number; win_rate: number | null; avg_return: number | null; median_return: number | null; total_return: number | null; cagr: number | null; sharpe: number | null; max_drawdown: number | null; avg_hold_days: number | null;
    /** Equity curve's first value (stock: $10,000; options: the cash that funds every trade); total_pnl = its dollar change. */
    starting_capital?: number | null; total_pnl: number | null; real_count: number; model_count: number };
  equity: { date: ISODate; value: number }[];
  benchmark: { date: ISODate; value: number }[] | null;   // buy & hold, same start value
  histogram: { bin_start: number; bin_end: number; count: number }[];
  notes: string[];
}

// ---------------- compare ----------------
export interface CompareVolatility { symbols: string[]; series: { symbol: string; points: { date: ISODate; iv30: number | null; hv20: number | null }[] }[]; table: ReportRow[] }

// ---------------- user data ----------------
export interface Watchlist { id: number; name: string; symbols: string[]; created_at: ISODateTime }
export interface Preset { id: number; report_id: string; name: string; payload: PresetPayload; created_at: ISODateTime }
export interface PresetPayload { params: Record<string, unknown>; filters: Record<string, ColumnFilter>; sort?: { key: string; dir: "asc" | "desc" } | null; hidden?: string[] }
export type ColumnFilter = { min?: number | null; max?: number | null; text?: string | null; values?: string[] | null };
export type AlertKind = "price_above" | "price_below" | "pct_change_abs" | "iv30_above" | "iv30_below" | "iv_rank_above" | "iv_rank_below" | "rel_option_volume_above" | "unusual_volume" | "earnings_within_days" | "ex_div_within_days" | "halted" | "resumed";
export interface Alert { id: number; kind: AlertKind; symbol: string | null; params: { value?: number; watchlist_id?: number }; enabled: boolean; cooldown_min: number; last_fired_at: ISODateTime | null; created_at: ISODateTime }
export interface Notification { id: number; ts: ISODateTime; alert_id: number | null; title: string; body: string; link: string | null; read: boolean }

// ---------------- AI agents (MCP, INTEGRATION.md §10) ----------------
/** GET /api/mcp: never the key. `clients`: MCP clients that called in the last 10 minutes. */
export interface McpStatus {
  /** `portfolio`: "Allow agents to see my portfolio" (off by default; off again whenever access is turned off). */
  enabled: boolean; portfolio: boolean; connection_file: string; port: number | null;
  last_call: { at: ISODateTime; operation: string; client: string } | null;
  clients: { name: string; last_seen_at: ISODateTime }[];
}

// ---------------- portfolio (INTEGRATION.md §11, read-only) ----------------
export type Broker = "questrade" | "ibkr";
export type PortfolioSourceState = "ok" | "needs_permission" | "not_connected" | "error" | "disabled" | "waiting" | "stale";
export interface PortfolioSource {
  broker: Broker; label: string; enabled: boolean; state: PortfolioSourceState; detail: string | null;
  fetched_at: ISODateTime | null; attempted_at: ISODateTime | null; accounts: number; positions: number;
}
/** A position: qty in shares / contracts (+ long, − short); avg_cost, price per share; money in the position's currency. */
export interface PortfolioPosition {
  id: string; broker: Broker; account_id: string; account: string; kind: "stock" | "option" | "other";
  symbol: string; underlying: string; osi: string | null; expiry: ISODate | null; right: Right | null; strike: number | null;
  qty: number; multiplier: number; currency: string; avg_cost: number | null;
  price: number | null; price_source: "live" | "stored" | "broker" | null; price_ts: ISODateTime | null; delayed: boolean | null;
  spot: number | null; iv: number | null;
  market_value: number | null; cost_basis: number | null; unrealized_pnl: number | null; unrealized_pct: number | null;
  /** `day_pnl_basis`: "broker" (the broker's figure) | "prev_close" (price − previous close: exact if held at that close). */
  day_pnl: number | null; day_pnl_basis: "broker" | "prev_close" | null;
  /** Position greeks: delta in shares, delta_dollars, gamma (shares per $1), theta ($/day), vega ($/vol pt). */
  delta: number | null; delta_dollars: number | null; gamma: number | null; theta: number | null; vega: number | null;
  beta: number | null; bwd_delta_spy: number | null;
  /** A stock / option of a listing OptionLens follows (US, or Canadian in CAD): its symbol page, live quotes and the calculator apply. */
  linkable: boolean;
}
/** Sums per currency; `unknown` counts the rows a value was unknown for (left out of the sum). */
export type PortfolioSums = Record<string, number | null> & { unknown: Record<string, number> };
export interface FxRateView { base: string; quote: string; rate: number; source: string; asof: ISODateTime; label: string }
export interface PortfolioAggregate {
  by_currency: Record<string, PortfolioSums>;
  /** Converted into the reporting currency with the brokers' rates; `values` null when a rate is missing (`missing_fx`). */
  converted: { currency: string; values: PortfolioSums | null; missing_fx: string[]; rates: FxRateView[] };
  positions: number;
}
export interface PortfolioBalance { currency: string; net_liquidation: number | null; cash: number | null; buying_power: number | null }
export interface PortfolioAccount { broker: Broker; account_id: string; account: string; balances: PortfolioBalance[]; balance_totals: PortfolioAggregate; totals: PortfolioAggregate }
export interface PortfolioUnderlying {
  underlying: string; currencies: string[]; spot: number | null; linkable: boolean;
  /** A risk graph is available (options on a US underlying). */
  risk: boolean;
  totals: PortfolioAggregate; bwd_delta_spy: number | null; positions: PortfolioPosition[];
}
export interface Portfolio {
  currency: string; asof: ISODateTime; sources: PortfolioSource[]; fx: FxRateView[];
  totals: PortfolioAggregate; balance_totals: PortfolioAggregate;
  beta_weighted: { spy_shares: number | null; dollars: number | null; spy_price: number | null; positions: number; excluded: number };
  brokers: { broker: Broker; label: string; totals: PortfolioAggregate; balance_totals: PortfolioAggregate }[];
  accounts: PortfolioAccount[]; underlyings: PortfolioUnderlying[];
  /** What the page keeps live (held US stocks / options and SPY). */
  live: { symbols: string[]; contracts: string[] };
}
export interface PortfolioRisk extends Partial<CalcResult> {
  underlying: string; currency: string; spot: number;
  positions: { symbol: string; account: string; broker: Broker; kind: "stock" | "option" | "other"; qty: number; avg_cost: number | null; osi: string | null; expiry: ISODate | null; right: Right | null; strike: number | null; multiplier: number }[];
  skipped: { symbol: string; account: string; reason: string }[];
}
export interface PortfolioSettings { questrade: boolean; ibkr: boolean }
