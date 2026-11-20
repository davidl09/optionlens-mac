// Ingest API bodies (INTEGRATION.md §6) and the client the bridge pushes through.

/** JSON transport to the engine (main/engine-http.ts provides it). */
export interface JsonHttp { get<T>(path: string): Promise<T>; post<T>(path: string, body: unknown): Promise<T> }

type N = number | null;

export interface IngestUnderlying { price: number; bid: N; ask: N; open: N; high: N; low: N; prev_close: N; volume: N; ts: string }
export interface IngestContract {
  expiry: string; strike: number; right: "C" | "P";
  bid: N; ask: N; last: N; volume: N; oi: N; iv: N; delta: N; gamma: N; theta: N; vega: N;
}
export interface IngestChain {
  source: "ibkr"; symbol: string; fetched_at: string; final: boolean; session_date?: string;
  underlying: IngestUnderlying; contracts: IngestContract[];
}
export interface VolBars { source: "ibkr"; symbol: string; kind: "iv30" | "hv30"; bars: { date: string; value: number }[] }
export interface IndexBars { source: "ibkr"; symbol: string; bars: { date: string; open: N; high: N; low: N; close: number }[] }
export interface IngestQuote { symbol: string; price: N; change: N; change_pct: N; ts: string }
export interface UnderlyingStat {
  symbol: string; price: N; change_pct: N; volume: N;
  call_volume: N; put_volume: N; call_oi: N; put_oi: N; iv30: N; hv30: N;
}
export interface UnderlyingStats { source: "ibkr"; session_date: string; ts: string; stats: UnderlyingStat[] }
export interface OptionBar { expiry: string; strike: number; right: "C" | "P"; date: string; open: N; high: N; low: N; close: number; volume: N }
export interface OptionBars { source: "ibkr"; symbol: string; bars: OptionBar[] }
export interface Heartbeat { client: string; ibkr_connected: boolean; account_type: "live" | "paper" | null }

export interface IngestApi {
  wants(): Promise<unknown>;
  chain(b: IngestChain): Promise<unknown>;
  volHistory(b: VolBars): Promise<unknown>;
  indexBars(b: IndexBars): Promise<unknown>;
  quotes(quotes: IngestQuote[]): Promise<unknown>;
  underlyingStats(b: UnderlyingStats): Promise<unknown>;
  optionBars(b: OptionBars): Promise<unknown>;
  heartbeat(b: Heartbeat): Promise<unknown>;
}

export function ingestApi(http: JsonHttp): IngestApi {
  return {
    wants: () => http.get("/api/ingest/wants"),
    chain: (b) => http.post("/api/ingest/chain", b),
    volHistory: (b) => http.post(b.kind === "iv30" ? "/api/ingest/iv-history" : "/api/ingest/hv-history", b),
    indexBars: (b) => http.post("/api/ingest/index-bars", b),
    quotes: (quotes) => http.post("/api/ingest/quotes", { source: "ibkr", quotes }),
    underlyingStats: (b) => http.post("/api/ingest/underlying-stats", b),
    optionBars: (b) => http.post("/api/ingest/option-bars", b),
    heartbeat: (b) => http.post("/api/ingest/heartbeat", b),
  };
}
