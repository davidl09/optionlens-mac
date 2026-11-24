// Typed fetch wrappers — one function per endpoint in SPEC §10.
// GETs go through a short-TTL in-memory cache with in-flight de-duplication;
// mutations invalidate the affected URL prefixes.
// In the desktop app the engine's base URL and per-launch token come from the preload (memory only,
// never persisted); in a plain browser requests are same-origin without a token.
import type {
  RefreshStatus,
  Activity, Alert, AlertKind, BacktestResult, BacktestTrade, Bar, CalcRequest, CalcResult, Chain,
  CompareVolatility, ContractHistory, Dividends, Earnings, EarningsStrategyResult, Events,
  Fundamentals, ImpliedMoves, Insiders, MarketOverview, NewsItem, Notification, OpenInterest,
  OptionsBacktestRequest, Preset, PresetPayload, PriceAction, Profile, ReportDef, ReportResult,
  QuestradeStatus, Seasonality, Skew, Status, StockBacktestRequest, StraddleInfo, StrategyTemplate, SymbolRef,
  SymbolSummary, TradeIdeas, VolSeasonality, Volatility, Watchlist,
} from "./types";

export class ApiError extends Error {
  /** `code`: the engine's machine-readable error code where it sends one (e.g. "no_chain"), else null. */
  constructor(public status: number, message: string, public code: string | null = null) {
    super(message);
    this.name = "ApiError";
  }
}

export type QueryValue = string | number | boolean | null | undefined | string[];
export type Query = Record<string, QueryValue>;

/** Builds "?a=1&b=x,y" skipping null/undefined/empty values. */
export function qs(query: Query = {}): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === null || v === undefined || v === "") continue;
    if (Array.isArray(v)) {
      if (v.length) sp.set(k, v.join(","));
    } else sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

function errorMessage(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) {
      return d.map((x) => (x && typeof x === "object" && "msg" in x ? String((x as { msg: unknown }).msg) : String(x))).join("; ");
    }
  }
  return fallback;
}

function errorCode(body: unknown): string | null {
  return body && typeof body === "object" && "code" in body && typeof body.code === "string" ? body.code : null;
}

// ---------------- engine connection + connectivity ----------------
export interface ClientConnection { baseUrl: string; token: string }
export type Connectivity = "ok" | "unreachable" | "unauthorized";

let conn: ClientConnection | null = null;
let connectivity: Connectivity = "ok";
/** Bumped whenever every mounted query should reload (recovery, new engine connection, IBKR refresh). */
let epoch = 0;
const listeners = new Set<() => void>();
const emit = () => { for (const l of listeners) l(); };

export const connection = {
  subscribe(cb: () => void): () => void { listeners.add(cb); return () => { listeners.delete(cb); }; },
  connectivity: (): Connectivity => connectivity,
  epoch: (): number => epoch,
};

/** Point the client at the engine (desktop) and reload everything. */
export function configureClient(c: ClientConnection | null): void {
  const changed = c?.baseUrl !== conn?.baseUrl || c?.token !== conn?.token;
  conn = c;
  if (changed) reloadAll();
}

/** Drop the cache and make every mounted query refetch. */
export function reloadAll(): void {
  cache.clear();
  epoch += 1;
  if (connectivity !== "ok") connectivity = "ok";
  emit();
}

function setConnectivity(next: Connectivity): void {
  if (next === connectivity) return;
  const recovered = connectivity !== "ok" && next === "ok";
  connectivity = next;
  if (recovered) { cache.clear(); epoch += 1; }
  emit();
}

/**
 * fetch() against the engine with its base URL and token, the response left to the caller (the live channel reads
 * a streamed body, src/live/connection.ts). Never puts the token in the URL.
 */
export function engineFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (conn?.token) headers.set("Authorization", `Bearer ${conn.token}`);
  return fetch(`${conn?.baseUrl ?? ""}${path}`, { ...init, headers });
}

/** A GET the engine has not answered by then fails with a retryable error instead of loading forever. */
export const GET_TIMEOUT_MS = 45_000;

async function request<T>(method: string, url: string, body?: unknown, timeoutMs: number | null = null): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (conn?.token) headers.Authorization = `Bearer ${conn.token}`;
  const ctl = timeoutMs !== null ? new AbortController() : null;
  const timer = ctl ? setTimeout(() => ctl.abort(), timeoutMs!) : null;
  let res: Response;
  let text: string;
  try {
    res = await fetch(`${conn?.baseUrl ?? ""}${url}`, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined, signal: ctl?.signal });
    text = await res.text();
  } catch {
    if (ctl?.signal.aborted) throw new ApiError(0, `No answer from the engine after ${Math.round(timeoutMs! / 1000)} s (it may be waiting on a slow data source).`, "timeout");
    setConnectivity("unreachable");
    throw new ApiError(0, "Network error — the OptionLens engine is not reachable.");
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
  let parsed: unknown = null;
  if (text) {
    try { parsed = JSON.parse(text); } catch { parsed = null; }
  }
  if (res.status === 401) setConnectivity("unauthorized");
  else setConnectivity("ok");
  if (!res.ok) throw new ApiError(res.status, errorMessage(parsed, `${res.status} ${res.statusText || "error"}`), errorCode(parsed));
  return parsed as T;
}

// ---------------- cache ----------------
const DEFAULT_TTL_MS = 30_000;
const cache = new Map<string, { at: number; promise: Promise<unknown> }>();

function get<T>(url: string, ttl = DEFAULT_TTL_MS): Promise<T> {
  const hit = cache.get(url);
  if (ttl > 0 && hit && Date.now() - hit.at < ttl) return hit.promise as Promise<T>;
  const promise = request<T>("GET", url, undefined, GET_TIMEOUT_MS);
  if (ttl > 0) {
    cache.set(url, { at: Date.now(), promise });
    promise.catch(() => cache.delete(url));
  }
  return promise;
}

/** Drop cached GETs whose URL starts with any prefix (all when none given). */
export function invalidate(...prefixes: string[]): void {
  if (!prefixes.length) { cache.clear(); return; }
  for (const key of [...cache.keys()]) if (prefixes.some((p) => key.startsWith(p))) cache.delete(key);
}

async function mutate<T>(method: string, url: string, body: unknown, invalidates: string[]): Promise<T> {
  const out = await request<T>(method, url, body);
  invalidate(...invalidates);
  return out;
}

const enc = encodeURIComponent;
const sym = (s: string) => `/api/symbol/${enc(s.toUpperCase())}`;

// ---------------- request bodies not named in types.ts ----------------
export interface WatchlistInput { name: string; symbols?: string[] }
export interface PresetInput { report_id: string; name: string; payload: PresetPayload }
export interface AlertInput {
  kind: AlertKind; symbol: string | null; params: Alert["params"]; enabled: boolean; cooldown_min: number;
}

export const api = {
  health: () => get<{ ok: boolean; version?: string }>("/api/health", 0),
  status: () => get<Status>("/api/status", 10_000),
  questradeStatus: () => get<QuestradeStatus>("/api/sources/questrade", 5_000),
  /** The token goes in the body only; the answer never contains it. */
  connectQuestrade: (refreshToken: string) => mutate<QuestradeStatus>("POST", "/api/sources/questrade/token", { refresh_token: refreshToken }, ["/api/sources", "/api/status"]),
  disconnectQuestrade: () => mutate<QuestradeStatus>("DELETE", "/api/sources/questrade/token", undefined, ["/api/sources", "/api/status"]),
  search: (q: string) => get<SymbolRef[]>(`/api/search${qs({ q })}`, 60_000),
  marketOverview: () => get<MarketOverview>("/api/market/overview"),

  /** Starts (or joins) the on-demand refresh round of every connected chain source (INTEGRATION.md §7). */
  startRefresh: (s: string) => request<RefreshStatus>("POST", `${sym(s)}/refresh`, undefined, GET_TIMEOUT_MS),
  refreshStatus: (s: string) => get<RefreshStatus>(`${sym(s)}/refresh`, 0),
  summary: (s: string) => get<SymbolSummary>(`${sym(s)}/summary`),
  bars: (s: string, range: string) => get<Bar[]>(`${sym(s)}/bars${qs({ range })}`),
  priceAction: (s: string, range = "1y") => get<PriceAction>(`${sym(s)}/price-action${qs({ range })}`),
  chain: (s: string, expiry?: string | null) => get<Chain>(`${sym(s)}/chain${qs({ expiry })}`),
  activity: (s: string, expiry?: string | null) => get<Activity>(`${sym(s)}/activity${qs({ expiry })}`),
  openInterest: (s: string, expiry?: string | null) => get<OpenInterest>(`${sym(s)}/open-interest${qs({ expiry })}`),
  contract: (s: string, osi: string) => get<ContractHistory>(`${sym(s)}/contract/${enc(osi)}`),
  volatility: (s: string) => get<Volatility>(`${sym(s)}/volatility`),
  skew: (s: string, expiry?: string | null) => get<Skew>(`${sym(s)}/skew${qs({ expiry })}`),
  impliedMoves: (s: string) => get<ImpliedMoves>(`${sym(s)}/implied-moves`),
  volSeasonality: (s: string) => get<VolSeasonality>(`${sym(s)}/vol-seasonality`),
  earnings: (s: string) => get<Earnings>(`${sym(s)}/earnings`),
  earningsStrategies: (s: string, p: { lookback: number; entry: number; exit: number }) =>
    get<EarningsStrategyResult>(`${sym(s)}/earnings/strategies${qs(p)}`, 300_000),
  earningsStrategyTrades: (s: string, strategy: string, p: { lookback: number; entry: number; exit: number }) =>
    get<BacktestTrade[]>(`${sym(s)}/earnings/strategies/${enc(strategy)}/trades${qs(p)}`, 300_000),
  dividends: (s: string) => get<Dividends>(`${sym(s)}/dividends`),
  seasonality: (s: string) => get<Seasonality>(`${sym(s)}/seasonality`),
  events: (s: string) => get<Events>(`${sym(s)}/events`),
  straddle: (s: string) => get<StraddleInfo>(`${sym(s)}/straddle`),
  ideas: (s: string) => get<TradeIdeas>(`${sym(s)}/ideas`),
  fundamentals: (s: string, period: "annual" | "quarterly") => get<Fundamentals>(`${sym(s)}/fundamentals${qs({ period })}`, 300_000),
  peers: (s: string) => get<ReportResult>(`${sym(s)}/peers`, 300_000),
  insiders: (s: string) => get<Insiders>(`${sym(s)}/insiders`, 300_000),
  news: (s: string) => get<NewsItem[]>(`${sym(s)}/news`, 120_000),
  profile: (s: string) => get<Profile>(`${sym(s)}/profile`, 300_000),

  calcStrategy: (req: CalcRequest) => request<CalcResult>("POST", "/api/calc/strategy", req),
  strategyTemplates: () => get<StrategyTemplate[]>("/api/strategy-templates", 600_000),

  reportDefs: () => get<ReportDef[]>("/api/reports", 300_000),
  report: (id: string, params: Query) => get<ReportResult>(`/api/reports/${enc(id)}${qs(params)}`),

  backtestStock: (req: StockBacktestRequest) => request<BacktestResult>("POST", "/api/backtest/stock", req),
  backtestOptions: (req: OptionsBacktestRequest) => request<BacktestResult>("POST", "/api/backtest/options", req),

  compareVolatility: (symbols: string[]) => get<CompareVolatility>(`/api/compare/volatility${qs({ symbols })}`),
  compareStraddle: (symbols: string[], dte: number) => get<ReportResult>(`/api/compare/straddle${qs({ symbols, dte })}`),

  watchlists: () => get<Watchlist[]>("/api/watchlists"),
  watchlist: (id: number) => get<Watchlist>(`/api/watchlists/${id}`),
  createWatchlist: (body: WatchlistInput) => mutate<Watchlist>("POST", "/api/watchlists", body, ["/api/watchlists", "/api/market"]),
  updateWatchlist: (id: number, body: Partial<WatchlistInput>) => mutate<Watchlist>("PUT", `/api/watchlists/${id}`, body, ["/api/watchlists", "/api/market"]),
  deleteWatchlist: (id: number) => mutate<unknown>("DELETE", `/api/watchlists/${id}`, undefined, ["/api/watchlists", "/api/market"]),
  addToWatchlist: (id: number, s: string) => mutate<Watchlist>("POST", `/api/watchlists/${id}/symbols/${enc(s.toUpperCase())}`, undefined, ["/api/watchlists", "/api/market"]),
  removeFromWatchlist: (id: number, s: string) => mutate<Watchlist>("DELETE", `/api/watchlists/${id}/symbols/${enc(s.toUpperCase())}`, undefined, ["/api/watchlists", "/api/market"]),
  watchlistTable: (id: number) => get<ReportResult>(`/api/watchlists/${id}/table`),

  presets: (reportId: string) => get<Preset[]>(`/api/presets${qs({ report_id: reportId })}`),
  createPreset: (body: PresetInput) => mutate<Preset>("POST", "/api/presets", body, ["/api/presets"]),
  deletePreset: (id: number) => mutate<unknown>("DELETE", `/api/presets/${id}`, undefined, ["/api/presets"]),

  alerts: () => get<Alert[]>("/api/alerts"),
  createAlert: (body: AlertInput) => mutate<Alert>("POST", "/api/alerts", body, ["/api/alerts"]),
  updateAlert: (id: number, body: Partial<AlertInput>) => mutate<Alert>("PUT", `/api/alerts/${id}`, body, ["/api/alerts"]),
  deleteAlert: (id: number) => mutate<unknown>("DELETE", `/api/alerts/${id}`, undefined, ["/api/alerts"]),

  notifications: (unreadOnly = false) => get<Notification[]>(`/api/notifications${qs({ unread: unreadOnly ? 1 : null })}`, 0),
  markNotificationsRead: (ids?: number[]) => mutate<unknown>("POST", "/api/notifications/read", ids ? { ids } : {}, ["/api/notifications"]),
};
