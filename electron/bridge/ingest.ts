// Ingest API bodies (INTEGRATION.md §6) and the client the bridge pushes through.

/** JSON transport to the engine (main/engine-http.ts provides it). */
export interface JsonHttp { get<T>(path: string): Promise<T>; post<T>(path: string, body: unknown): Promise<T> }

type N = number | null;

export interface IngestUnderlying {
  price: number; bid: N; ask: N; open: N; high: N; low: N; prev_close: N; volume: N; ts: string;
  /** Regular-session close of session_date (closing chains; null when unknown or not final). */
  rth_close: N;
}
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
/** IBKR lists no options for the symbol (no option parameters / no future expiries): the engine marks it optionable=false. */
export interface NoOptions { source: "ibkr"; symbol: string }
/** The bridge accepted (queued) these wants; the engine counts only accepted option_bars offers. */
export interface AckWants { kind: "option_bars"; items: { symbol: string; sessions: string[] }[] }
export type FarmStateName = "ok" | "inactive" | "connecting" | "down" | "unknown";
export interface Heartbeat {
  client: string; ibkr_connected: boolean; account_type: "live" | "paper" | null;
  /** IBKR server farms as the Gateway reports them (null while not connected). */
  farms: { market_data: FarmStateName; historical: FarmStateName; sec_def: FarmStateName } | null;
  /** What the account's US options market data allows ("delayed_only": no OPRA; option quotes in market hours only). */
  options_subscription: "unknown" | "realtime" | "delayed_only";
}

export interface IngestApi {
  wants(): Promise<unknown>;
  chain(b: IngestChain): Promise<unknown>;
  noOptions(b: NoOptions): Promise<unknown>;
  volHistory(b: VolBars): Promise<unknown>;
  indexBars(b: IndexBars): Promise<unknown>;
  quotes(b: IngestQuotes): Promise<unknown>;
  underlyingStats(b: UnderlyingStats): Promise<unknown>;
  optionBars(b: OptionBars): Promise<unknown>;
  ackWants(b: AckWants): Promise<unknown>;
  heartbeat(b: Heartbeat): Promise<unknown>;
}

export function ingestApi(http: JsonHttp): IngestApi {
  return {
    wants: () => http.get("/api/ingest/wants"),
    chain: (b) => http.post("/api/ingest/chain", b),
    noOptions: (b) => http.post("/api/ingest/no-options", b),
    volHistory: (b) => http.post(b.kind === "iv30" ? "/api/ingest/iv-history" : "/api/ingest/hv-history", b),
    indexBars: (b) => http.post("/api/ingest/index-bars", b),
    quotes: (b) => http.post("/api/ingest/quotes", b),
    underlyingStats: (b) => http.post("/api/ingest/underlying-stats", b),
    optionBars: (b) => http.post("/api/ingest/option-bars", b),
    ackWants: (b) => http.post("/api/ingest/ack-wants", b),
    heartbeat: (b) => http.post("/api/ingest/heartbeat", b),
  };
}
