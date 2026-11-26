// Ingest API bodies (INTEGRATION.md §6) and the client the bridge pushes through.

/** JSON transport to the engine (main/engine-http.ts provides it). */
export interface JsonHttp { get<T>(path: string): Promise<T>; post<T>(path: string, body: unknown): Promise<T> }

type N = number | null;

export interface IngestUnderlying {
  price: number; bid: N; ask: N; open: N; high: N; low: N; prev_close: N; volume: N; ts: string;
  /** Regular-session close of session_date (closing chains; null when unknown or not final). */
  rth_close: N;
  /** `price` is the engine's reference price (ReferencePrice; `ts` its time): TWS sent no underlying price. */
  reference?: boolean;
}
/**
 * The engine's own current price of a symbol (GET /api/ingest/reference-price/{symbol}: its newest stored quote, else
 * its latest daily close), for a chain whose underlying TWS sent no price for (no market-data subscription for it).
 */
export interface ReferencePrice { symbol: string; price: number; ts: string; source: string | null }
export interface IngestContract {
  expiry: string; strike: number; right: "C" | "P";
  bid: N; ask: N; last: N;
  /** When `last` traded (TWS tick 45 / 88), null when unknown: a weekend snapshot's last can be days old. */
  last_ts: string | null;
  volume: N; oi: N; iv: N; delta: N; gamma: N; theta: N; vega: N;
}
export interface IngestChain {
  source: "ibkr"; symbol: string; fetched_at: string; final: boolean; session_date?: string;
  /** True when any quote in the push came from IBKR delayed data (no real-time subscription). */
  delayed: boolean;
  /** The contract list is a saved one (IBKR's contract server was unavailable), fetched at contracts_listed_at. */
  contracts_from_cache: boolean;
  contracts_listed_at: string | null;
  underlying: IngestUnderlying; contracts: IngestContract[];
}
export interface VolBars { source: "ibkr"; symbol: string; kind: "iv30" | "hv30"; bars: { date: string; value: number }[] }
export interface IndexBars { source: "ibkr"; symbol: string; bars: { date: string; open: N; high: N; low: N; close: number }[] }
/** Daily closes for the dividends behind IBKR's adjustment (INTEGRATION.md §6 "Dividends"): ADJUSTED_LAST and TRADES. */
export interface DividendHistory { source: "ibkr"; symbol: string; adjusted: { date: string; close: number }[]; trades: { date: string; close: number }[] }
export interface IngestQuote {
  symbol: string; price: N; change: N; change_pct: N; ts: string;
  /** Session fields where TWS sent them (stock quotes of the underlying-stats pass); absent = not sent. */
  bid?: N; ask?: N; open?: N; high?: N; low?: N; prev_close?: N; volume?: N;
}
export interface UnderlyingStat {
  symbol: string; price: N; change_pct: N; volume: N;
  call_volume: N; put_volume: N; call_oi: N; put_oi: N; iv30: N; hv30: N;
}
export interface UnderlyingStats { source: "ibkr"; session_date: string; ts: string; delayed: boolean; stats: UnderlyingStat[] }
export interface IngestQuotes { source: "ibkr"; delayed: boolean; quotes: IngestQuote[] }
export interface OptionBar { expiry: string; strike: number; right: "C" | "P"; date: string; open: N; high: N; low: N; close: number; volume: N }
export interface OptionBars { source: "ibkr"; symbol: string; bars: OptionBar[] }
/** Intraday bars for spans the engine could not fill (INTEGRATION.md §5): `spans` is what was fetched, all hours. */
export interface IntradayBarsPush {
  source: "ibkr"; symbol: string; interval: "1m" | "5m" | "15m" | "1h";
  /** Unknown for historical bars (null). */
  delayed: boolean | null;
  spans: { start: string; end: string }[];
  bars: { ts: string; open: number; high: number; low: number; close: number; volume: N }[];
}
/** IBKR lists no options for the symbol (no option parameters / no future expiries): the engine marks it optionable=false. */
export interface NoOptions { source: "ibkr"; symbol: string }
/** The bridge accepted (queued) these wants; the engine counts only accepted option_bars offers. */
export interface AckWants { kind: "option_bars"; items: { symbol: string; sessions: string[] }[] }
/** The outcome of an on-demand refresh (RefreshResult), reported to the engine's refresh round (INTEGRATION.md §7). */
export interface RefreshResultBody {
  source: "ibkr"; symbol: string; status: "pending" | "refreshed" | "fresh" | "no-options" | "unavailable" | "failed";
  message: string | null; waiting_for: "contract-server" | "market-hours" | null;
}
export type FarmStateName = "ok" | "inactive" | "connecting" | "down" | "unknown";
export interface Heartbeat {
  client: string; ibkr_connected: boolean; account_type: "live" | "paper" | null;
  /** IBKR server farms as the Gateway reports them (null while not connected). */
  farms: { market_data: FarmStateName; historical: FarmStateName; sec_def: FarmStateName } | null;
  /** What the account's US options market data allows ("delayed_only": no OPRA; option quotes in market hours only). */
  options_subscription: "unknown" | "realtime" | "delayed_only";
}

/** One position as TWS reports it (reqPositions): `avg_cost` per share (STK), per contract (OPT, × multiplier). */
export interface IngestPosition {
  account: string; sec_type: string; symbol: string; currency: string; qty: number; avg_cost: N;
  multiplier: N; expiry: string | null; strike: N; right: "C" | "P" | null; trading_class: string | null; con_id: N;
}
/** The accounts' summary (base currency) and exchange rates (`rate`: base-currency value of 1 `currency`). */
export interface IngestAccountSummary { account: string; currency: string; net_liquidation: N; cash: N; buying_power: N }
export interface IngestPositions {
  source: "ibkr"; fetched_at: string; positions: IngestPosition[]; accounts: IngestAccountSummary[];
  fx: { currency: string; base: string; rate: number }[];
}

export interface IngestApi {
  wants(): Promise<unknown>;
  /** ReferencePrice of `symbol` (404 when the engine stores none). */
  referencePrice(symbol: string): Promise<unknown>;
  /** The symbols on screen to snapshot now (INTEGRATION.md §9; none unless the bridge is the live fallback). */
  liveWants(): Promise<unknown>;
  chain(b: IngestChain): Promise<unknown>;
  noOptions(b: NoOptions): Promise<unknown>;
  volHistory(b: VolBars): Promise<unknown>;
  indexBars(b: IndexBars): Promise<unknown>;
  dividendHistory(b: DividendHistory): Promise<unknown>;
  quotes(b: IngestQuotes): Promise<unknown>;
  underlyingStats(b: UnderlyingStats): Promise<unknown>;
  optionBars(b: OptionBars): Promise<unknown>;
  intradayBars(b: IntradayBarsPush): Promise<unknown>;
  ackWants(b: AckWants): Promise<unknown>;
  heartbeat(b: Heartbeat): Promise<unknown>;
  refreshResult(b: RefreshResultBody): Promise<unknown>;
  /** The accounts' positions and summary (read-only account data, INTEGRATION.md §6 / §11). */
  positions(b: IngestPositions): Promise<unknown>;
}

export function ingestApi(http: JsonHttp): IngestApi {
  return {
    wants: () => http.get("/api/ingest/wants"),
    referencePrice: (symbol) => http.get(`/api/ingest/reference-price/${encodeURIComponent(symbol)}`),
    liveWants: () => http.get("/api/ingest/live-wants"),
    chain: (b) => http.post("/api/ingest/chain", b),
    noOptions: (b) => http.post("/api/ingest/no-options", b),
    volHistory: (b) => http.post(b.kind === "iv30" ? "/api/ingest/iv-history" : "/api/ingest/hv-history", b),
    indexBars: (b) => http.post("/api/ingest/index-bars", b),
    dividendHistory: (b) => http.post("/api/ingest/dividend-history", b),
    quotes: (b) => http.post("/api/ingest/quotes", b),
    underlyingStats: (b) => http.post("/api/ingest/underlying-stats", b),
    optionBars: (b) => http.post("/api/ingest/option-bars", b),
    intradayBars: (b) => http.post("/api/ingest/intraday-bars", b),
    ackWants: (b) => http.post("/api/ingest/ack-wants", b),
    heartbeat: (b) => http.post("/api/ingest/heartbeat", b),
    refreshResult: (b) => http.post("/api/ingest/refresh-result", b),
    positions: (b) => http.post("/api/ingest/positions", b),
  };
}
