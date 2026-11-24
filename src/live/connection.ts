// The window's live channel to the engine (INTEGRATION.md §9): one SSE stream (GET /api/live/stream, read with fetch
// so the Bearer token goes in a header, never a URL) and the window's subscription (POST /api/live/subscribe: the
// store's union, debounced, re-sent after every reconnect). A hidden or minimized window unsubscribes and closes the
// stream, so the engine stops fetching what nobody looks at.
import { SseParser } from "./sse";
import { LiveStore, NOT_CONNECTED, type LivePatch, type LiveStatus, type Wanted } from "./store";

export interface LiveTransport {
  stream(clientId: string, signal: AbortSignal): Promise<Response>;
  subscribe(body: { client_id: string } & Wanted): Promise<unknown>;
}

export interface Visibility { hidden(): boolean; onChange(cb: () => void): () => void }

export interface ConnectionOptions {
  transport: LiveTransport;
  visibility?: Visibility;
  clientId?: string;
  debounceMs?: number;
  /** Reconnect delays (ms): doubles from min to max while streams keep failing. */
  backoff?: { min: number; max: number };
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (t: unknown) => void;
}

const EMPTY: Wanted = { symbols: [], option_contracts: [], chains: [] };

export function randomClientId(): string {
  const c = globalThis.crypto;
  return c?.randomUUID ? c.randomUUID().replace(/-/g, "") : `w${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`;
}

export class LiveConnection {
  readonly clientId: string;
  private readonly t: LiveTransport;
  private readonly vis: Visibility | null;
  private readonly debounceMs: number;
  private readonly backoff: { min: number; max: number };
  private readonly setTimer: (fn: () => void, ms: number) => unknown;
  private readonly clearTimer: (t: unknown) => void;
  private ctl: AbortController | null = null;
  private retryTimer: unknown = null;
  private subTimer: unknown = null;
  private delay: number;
  private started = false;
  private sentKey: string | null = null;
  private unsub: (() => void)[] = [];

  constructor(readonly store: LiveStore, o: ConnectionOptions) {
    this.t = o.transport;
    this.vis = o.visibility ?? null;
    this.clientId = o.clientId ?? randomClientId();
    this.debounceMs = o.debounceMs ?? 250;
    this.backoff = o.backoff ?? { min: 1000, max: 15_000 };
    this.delay = this.backoff.min;
    this.setTimer = o.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer = o.clearTimer ?? ((t) => clearTimeout(t as ReturnType<typeof setTimeout>));
  }

  /** Idempotent. */
  start(): void {
    if (this.started) return;
    this.started = true;
    this.unsub.push(this.store.onWanted(() => this.scheduleSubscribe()));
    if (this.vis) this.unsub.push(this.vis.onChange(() => this.onVisibility()));
    if (!this.vis?.hidden()) this.open();
  }

  stop(): void {
    this.started = false;
    for (const u of this.unsub.splice(0)) u();
    this.close();
  }

  /** The engine connection changed (new base URL / token, recovery): reopen the stream. */
  restart(): void {
    if (!this.started) return;
    this.close();
    this.delay = this.backoff.min;
    if (!this.vis?.hidden()) this.open();
  }

  private onVisibility(): void {
    if (this.vis?.hidden()) {
      this.close();
      this.sentKey = null;
      void this.t.subscribe({ client_id: this.clientId, ...EMPTY }).catch(() => undefined);
      this.store.setStatus({ ...this.store.status, state: "idle" });
    } else {
      this.delay = this.backoff.min;
      this.open();
    }
  }

  private close(): void {
    this.ctl?.abort();
    this.ctl = null;
    if (this.retryTimer !== null) { this.clearTimer(this.retryTimer); this.retryTimer = null; }
    if (this.subTimer !== null) { this.clearTimer(this.subTimer); this.subTimer = null; }
  }

  private scheduleSubscribe(): void {
    if (!this.started || this.vis?.hidden()) return;
    if (this.subTimer !== null) this.clearTimer(this.subTimer);
    this.subTimer = this.setTimer(() => { this.subTimer = null; void this.sendSubscribe(false); }, this.debounceMs);
  }

  private async sendSubscribe(force: boolean): Promise<void> {
    const w = this.store.wanted();
    const key = JSON.stringify(w);
    if (!force && key === this.sentKey) return;
    this.sentKey = key;
    try {
      await this.t.subscribe({ client_id: this.clientId, ...w });
    } catch {
      this.sentKey = null;        // sent again after the next (re)connect
    }
  }

  private open(): void {
    if (this.ctl) return;
    const ctl = new AbortController();
    this.ctl = ctl;
    void this.run(ctl);
  }

  private async run(ctl: AbortController): Promise<void> {
    const opened = Date.now();
    try {
      const res = await this.t.stream(this.clientId, ctl.signal);
      if (!res.ok || !res.body) throw new Error(`live stream answered ${res.status}`);
      void this.sendSubscribe(true);
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      const parser = new SseParser();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        for (const ev of parser.feed(dec.decode(value, { stream: true }))) this.onEvent(ev.event, ev.data);
        this.delay = this.backoff.min;
      }
    } catch {
      if (ctl.signal.aborted) return;
      this.store.setStatus(NOT_CONNECTED);
    }
    if (ctl.signal.aborted || this.ctl !== ctl) return;
    this.ctl = null;
    // Reconnect: at once after a long-lived stream, else with backoff.
    const wait = Date.now() - opened > 30_000 ? this.backoff.min : this.delay;
    this.delay = Math.min(this.backoff.max, this.delay * 2);
    this.retryTimer = this.setTimer(() => { this.retryTimer = null; if (this.started && !this.vis?.hidden()) this.open(); }, wait);
  }

  private onEvent(event: string, data: string): void {
    let v: unknown;
    try { v = JSON.parse(data); } catch { return; }
    if (event === "status") this.store.setStatus(v as LiveStatus);
    else if (event === "patch") this.store.apply(v as LivePatch);
  }
}

/** document.visibilityState (Electron reports a minimized or hidden window as hidden). */
export const documentVisibility: Visibility = {
  hidden: () => typeof document !== "undefined" && document.visibilityState === "hidden",
  onChange: (cb) => {
    document.addEventListener("visibilitychange", cb);
    return () => document.removeEventListener("visibilitychange", cb);
  },
};
