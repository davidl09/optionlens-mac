// The renderer's live-quote store (INTEGRATION.md §9): what the mounted views want live (refcounted, unioned into
// the window's one subscription) and the latest live values per symbol / OSI, with per-key listeners so a patch
// re-renders only the cells that show that key; and the intraday charts' live candles per series, kept for the
// window's lifetime. Pure: the network side is connection.ts.
import type { LiveCandle } from "./candles";

export type LiveState = "live" | "delayed" | "connecting" | "stale" | "market_closed" | "not_connected" | "idle";

export interface LiveStatus {
  state: LiveState;
  source: "questrade" | "ibkr" | null;
  session?: "regular" | "extended" | "closed";
  mode?: "poll" | "stream" | "snapshots" | null;
  interval_s?: number | null;
  /** Streaming is chosen but Questrade is polled meanwhile: why (e.g. "handshake failed"). */
  fallback?: string | null;
  detail?: string | null;
  symbols?: number;
  contracts?: number;
  /** Followed symbols kept live in the background (no window shows them). */
  background?: number;
}

/** Fields of one live key as the engine patches them (only those it has), plus where / when they came from. */
export interface LiveFields {
  price?: number; change?: number; change_pct?: number; bid?: number; ask?: number; mid?: number; last?: number;
  open?: number; high?: number; low?: number; prev_close?: number; volume?: number; oi?: number;
  iv?: number; delta?: number; gamma?: number; theta?: number; vega?: number;
  delayed?: boolean; source?: "questrade" | "ibkr"; ts?: string;
  /** Underlyings: the session VWAP and the halt flag as Questrade reports them. */
  vwap?: number; halted?: boolean;
}

export interface LivePatch { quotes?: Record<string, LiveFields>; options?: Record<string, LiveFields> }

/** One expiry of a chain, a strike band of it (both bounds inclusive; null = unbounded). */
export interface ChainView { symbol: string; expiry: string; strike_min: number | null; strike_max: number | null }

export interface LiveSubscription { symbols?: string[]; contracts?: string[]; chains?: ChainView[] }

export interface Wanted { symbols: string[]; option_contracts: string[]; chains: ChainView[] }

/** The engine's caps (live/resolve.py and api/routes_live.py). */
export const MAX_SYMBOLS = 500;
export const MAX_CONTRACTS = 1000;
export const MAX_CHAINS = 8;

export type Kind = "q" | "o";
const keyOf = (kind: Kind, id: string) => `${kind}:${id}`;

export const NOT_CONNECTED: LiveStatus = { state: "not_connected", source: null, detail: "The engine's live channel is not reachable." };
const INITIAL: LiveStatus = { state: "connecting", source: null };

/** Order-independent identity of a subscription (a view re-renders with equal arrays: no churn). */
export function subscriptionKey(s: LiveSubscription): string {
  const chains = (s.chains ?? []).map((c) => `${c.symbol}|${c.expiry}|${c.strike_min ?? ""}|${c.strike_max ?? ""}`);
  return JSON.stringify([[...new Set(s.symbols ?? [])].sort(), [...new Set(s.contracts ?? [])].sort(), [...new Set(chains)].sort()]);
}

export function unionOf(subs: Iterable<LiveSubscription>): Wanted {
  const symbols = new Set<string>(), contracts = new Set<string>();
  const chains = new Map<string, ChainView>();
  for (const s of subs) {
    for (const x of s.symbols ?? []) if (x) symbols.add(x.toUpperCase());
    for (const x of s.contracts ?? []) if (x) contracts.add(x);
    for (const c of s.chains ?? []) chains.set(`${c.symbol}|${c.expiry}|${c.strike_min}|${c.strike_max}`, { ...c, symbol: c.symbol.toUpperCase() });
  }
  return {
    symbols: [...symbols].sort().slice(0, MAX_SYMBOLS),
    option_contracts: [...contracts].sort().slice(0, MAX_CONTRACTS),
    chains: [...chains.values()].slice(0, MAX_CHAINS),
  };
}

export class LiveStore {
  private values = new Map<string, LiveFields>();
  private keyListeners = new Map<string, Set<() => void>>();
  private statusListeners = new Set<() => void>();
  private wantedListeners = new Set<() => void>();
  private subs = new Map<number, LiveSubscription>();
  private nextId = 1;
  private wantedCache: Wanted = unionOf([]);
  private wantedKey = subscriptionKey({});
  status: LiveStatus = INITIAL;

  // ---------------- what the views want
  add(sub: LiveSubscription): number {
    const id = this.nextId++;
    this.subs.set(id, sub);
    this.recompute();
    return id;
  }

  remove(id: number): void {
    if (this.subs.delete(id)) this.recompute();
  }

  wanted(): Wanted { return this.wantedCache; }

  onWanted(cb: () => void): () => void { this.wantedListeners.add(cb); return () => { this.wantedListeners.delete(cb); }; }

  private recompute(): void {
    const w = unionOf(this.subs.values());
    const k = subscriptionKey({ symbols: w.symbols, contracts: w.option_contracts, chains: w.chains });
    if (k === this.wantedKey) return;
    this.wantedCache = w;
    this.wantedKey = k;
    for (const l of this.wantedListeners) l();
  }

  // ---------------- live values
  /** Merge a patch; listeners of each changed key are told once. */
  apply(p: LivePatch): void {
    const touched: string[] = [];
    for (const [kind, items] of [["q", p.quotes], ["o", p.options]] as const) {
      for (const [id, f] of Object.entries(items ?? {})) {
        const k = keyOf(kind, id);
        this.values.set(k, { ...this.values.get(k), ...f });
        touched.push(k);
      }
    }
    for (const k of touched) for (const l of this.keyListeners.get(k) ?? []) l();
  }

  get(kind: Kind, id: string): LiveFields | null { return this.values.get(keyOf(kind, id)) ?? null; }

  onKey(kind: Kind, id: string, cb: () => void): () => void {
    const k = keyOf(kind, id);
    let set = this.keyListeners.get(k);
    if (!set) this.keyListeners.set(k, (set = new Set()));
    set.add(cb);
    return () => { set!.delete(cb); if (!set!.size) this.keyListeners.delete(k); };
  }

  // ---------------- live candles of the intraday charts (closed ones too), per series (symbol | interval | hours)
  private candles = new Map<string, LiveCandle>();

  candle(series: string): LiveCandle | null { return this.candles.get(series) ?? null; }

  keepCandle(series: string, c: LiveCandle | null): void {
    if (c) this.candles.set(series, c);
    else this.candles.delete(series);
  }

  // ---------------- status
  setStatus(s: LiveStatus): void {
    if (JSON.stringify(s) === JSON.stringify(this.status)) return;
    this.status = s;
    for (const l of this.statusListeners) l();
  }

  onStatus(cb: () => void): () => void { this.statusListeners.add(cb); return () => { this.statusListeners.delete(cb); }; }
}

/** Whether values are flowing now (patches are only shown as live in these states). */
export const isFlowing = (s: LiveStatus) => s.state === "live" || s.state === "delayed";

/** The one word for values that came over the live channel: "delayed" when they are (the value's own `delayed` flag,
 * or the channel's delayed state), else "live". Every label that calls streamed values "live" goes through this. */
export function streamedWord(delayed: boolean | null | undefined, status?: LiveStatus | null): "live" | "delayed" {
  return delayed === true || status?.state === "delayed" ? "delayed" : "live";
}
