import { describe, expect, it, vi } from "vitest";
import type { Bar, OptionRow } from "../api/types";
import { LiveConnection, type LiveTransport, type Visibility } from "./connection";
import { liveKeyOf, rowKeys, withLiveBar, withLiveOption, withLiveQuote } from "./merge";
import { SseParser } from "./sse";
import { LiveStore, MAX_CHAINS, subscriptionKey, unionOf } from "./store";

describe("SSE parser", () => {
  it("assembles events across chunks and drops comments", () => {
    const p = new SseParser();
    expect(p.feed("event: status\ndata: {\"state\"")).toEqual([]);
    expect(p.feed(":\"live\"}\n\n: keep-alive\n\nevent: patch\r\ndata: {}\r\n\r\n")).toEqual([
      { event: "status", data: "{\"state\":\"live\"}" }, { event: "patch", data: "{}" },
    ]);
    expect(p.feed("data: a\ndata: b\n\n")).toEqual([{ event: "message", data: "a\nb" }]);
  });
});

describe("store", () => {
  it("unions the mounted views' subscriptions (refcounted, deduplicated, capped) and tells listeners only on change", () => {
    const s = new LiveStore();
    const seen: string[][] = [];
    s.onWanted(() => seen.push(s.wanted().symbols));
    const a = s.add({ symbols: ["aapl", "SPY"] });
    const b = s.add({ symbols: ["SPY"], contracts: ["X1"], chains: [{ symbol: "aapl", expiry: "2026-12-18", strike_min: 340, strike_max: 360 }] });
    expect(s.wanted()).toEqual({ symbols: ["AAPL", "SPY"], option_contracts: ["X1"], chains: [{ symbol: "AAPL", expiry: "2026-12-18", strike_min: 340, strike_max: 360 }] });
    const c = s.add({ symbols: ["SPY"] });                     // same union: no notification
    expect(seen).toHaveLength(2);
    s.remove(b);
    expect(s.wanted()).toEqual({ symbols: ["AAPL", "SPY"], option_contracts: [], chains: [] });
    s.remove(a); s.remove(c);
    expect(s.wanted().symbols).toEqual([]);
    const many = Array.from({ length: 20 }, (_, i) => ({ symbol: "A", expiry: `2026-12-${10 + i}`, strike_min: null, strike_max: null }));
    expect(unionOf([{ chains: many }]).chains).toHaveLength(MAX_CHAINS);
    expect(subscriptionKey({ symbols: ["B", "A"] })).toBe(subscriptionKey({ symbols: ["A", "B", "A"] }));
  });

  it("merges patches per key and notifies only that key's listeners", () => {
    const s = new LiveStore();
    const aapl = vi.fn(), osi = vi.fn();
    s.onKey("q", "AAPL", aapl);
    const off = s.onKey("o", "AAPL261218C00350000", osi);
    s.apply({ quotes: { AAPL: { price: 350, bid: 349.9 } } });
    s.apply({ quotes: { AAPL: { price: 351 } }, options: { SPY261218C00600000: { bid: 1 } } });
    expect(s.get("q", "AAPL")).toEqual({ price: 351, bid: 349.9 });
    expect(aapl).toHaveBeenCalledTimes(2);
    expect(osi).not.toHaveBeenCalled();
    off();
    s.apply({ options: { AAPL261218C00350000: { bid: 2 } } });
    expect(osi).not.toHaveBeenCalled();
  });
});

describe("merge", () => {
  const row: OptionRow = { osi: "AAPL261218C00350000", expiry: "2026-12-18", strike: 350, right: "C", bid: 9, ask: 9.4, mid: 9.2, last: 9.1, change: null,
    volume: 10, oi: 100, iv: 0.25, delta: 0.5, gamma: 0.01, theta: -0.1, vega: 0.3, rho: null, theo: null, itm: false, iv_source: "ibkr", greeks_source: "ibkr" };

  it("patches quotes and option rows (mid recomputed, sources relabelled, moneyness from the live spot)", () => {
    const q = withLiveQuote({ price: 1, change: 0, change_pct: 0, ts: "old", source: "ibkr" }, { price: 2, change: 1, ts: "new", source: "questrade" });
    expect(q).toMatchObject({ price: 2, change: 1, change_pct: 0, ts: "new", source: "questrade" });
    const r = withLiveOption(row, { bid: 10, ask: 10.4, iv: 0.27, source: "questrade" }, 351);
    expect(r).toMatchObject({ bid: 10, ask: 10.4, mid: 10.2, iv: 0.27, iv_source: "questrade", greeks_source: "ibkr", itm: true, oi: 100 });
    expect(withLiveOption(row, null)).toBe(row);
    // the session VWAP and the halt flag follow the live quote (a false halt clears the badge)
    const h = withLiveQuote({ price: 1, vwap: 0.9, halted: true }, { vwap: 1.01, halted: false });
    expect(h).toMatchObject({ vwap: 1.01, halted: false });
  });

  it("picks the live key of a report cell: contract fields of osi rows, quote fields of symbol rows, nothing for history metrics", () => {
    const stock = { symbol: "AAPL", price: 1, iv_rank: 0.5 };
    const opt = { symbol: "AAPL", osi: "AAPL261218C00350000", bid: 1, price: 350 };
    expect(liveKeyOf(stock, "price")).toEqual(["q", "AAPL"]);
    expect(liveKeyOf(stock, "iv_rank")).toBeNull();
    expect(liveKeyOf(stock, "vwap")).toEqual(["q", "AAPL"]);
    expect(liveKeyOf(stock, "halted")).toEqual(["q", "AAPL"]);
    expect(liveKeyOf(opt, "bid")).toEqual(["o", "AAPL261218C00350000"]);
    expect(liveKeyOf(opt, "price")).toEqual(["q", "AAPL"]);
    expect(liveKeyOf(opt, "oi_change")).toBeNull();
    expect(rowKeys([stock, opt, { name: "x" }])).toEqual({ symbols: ["AAPL"], contracts: ["AAPL261218C00350000"] });
  });

  it("updates today's bar or appends one", () => {
    const bars: Bar[] = [{ date: "2026-11-23", open: 10, high: 11, low: 9, close: 10.5, volume: 100 }];
    expect(withLiveBar(bars, { price: 12, volume: 300 }, "2026-11-23")).toEqual([{ date: "2026-11-23", open: 10, high: 12, low: 9, close: 12, volume: 300 }]);
    expect(withLiveBar(bars, { price: 10.8, open: 10.6, high: 11, low: 10.5, volume: 5 }, "2026-11-24")[1])
      .toEqual({ date: "2026-11-24", open: 10.6, high: 11, low: 10.5, close: 10.8, volume: 5 });
    expect(withLiveBar(bars, null, "2026-11-24")).toBe(bars);
  });
});

// ---------------- connection
function sseResponse(frames: string[], keepOpen = false): { res: Response; push: (f: string) => void; end: () => void } {
  let ctl!: ReadableStreamDefaultController<Uint8Array>;
  const enc = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({ start(c) { ctl = c; for (const f of frames) c.enqueue(enc.encode(f)); if (!keepOpen) c.close(); } });
  return { res: new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } }), push: (f) => ctl.enqueue(enc.encode(f)), end: () => ctl.close() };
}

class Vis implements Visibility {
  isHidden = false;
  private cbs = new Set<() => void>();
  hidden() { return this.isHidden; }
  onChange(cb: () => void) { this.cbs.add(cb); return () => { this.cbs.delete(cb); }; }
  set(h: boolean) { this.isHidden = h; for (const c of this.cbs) c(); }
}

const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };

describe("connection", () => {
  it("reads status and patches, subscribes after opening, debounces changes and unsubscribes when hidden", async () => {
    vi.useFakeTimers();
    try {
      const store = new LiveStore();
      const subs: unknown[] = [];
      const live = sseResponse(['event: status\ndata: {"state":"live","source":"questrade"}\n\n'], true);
      const signals: AbortSignal[] = [];
      const t: LiveTransport = { stream: async (_id, signal) => { signals.push(signal); return live.res; }, subscribe: async (b) => { subs.push(b); return {}; } };
      const vis = new Vis();
      const c = new LiveConnection(store, { transport: t, visibility: vis, clientId: "client-0001", debounceMs: 100 });
      store.add({ symbols: ["AAPL"] });
      c.start();
      await flush();
      expect(store.status).toEqual({ state: "live", source: "questrade" });
      expect(subs).toEqual([{ client_id: "client-0001", symbols: ["AAPL"], option_contracts: [], chains: [] }]);
      live.push('event: patch\ndata: {"quotes":{"AAPL":{"price":351}}}\n\n');
      await flush();
      expect(store.get("q", "AAPL")).toEqual({ price: 351 });
      store.add({ symbols: ["SPY"] });
      store.add({ contracts: ["X"] });
      await vi.advanceTimersByTimeAsync(150);
      expect(subs).toHaveLength(2);                                     // two changes, one request
      expect(subs[1]).toMatchObject({ symbols: ["AAPL", "SPY"], option_contracts: ["X"] });
      vis.set(true);                                                    // minimized / hidden window
      await flush();
      expect(signals[0].aborted).toBe(true);
      expect(subs[2]).toEqual({ client_id: "client-0001", symbols: [], option_contracts: [], chains: [] });
      expect(store.status.state).toBe("idle");
    } finally {
      vi.useRealTimers();
    }
  });

  it("reconnects with backoff after failures and re-sends the subscription; reports not connected", async () => {
    vi.useFakeTimers();
    try {
      const store = new LiveStore();
      store.add({ symbols: ["AAPL"] });
      const subs: unknown[] = [];
      let calls = 0;
      const t: LiveTransport = {
        stream: async () => { calls += 1; if (calls < 3) throw new TypeError("fetch failed"); return sseResponse(['event: status\ndata: {"state":"market_closed","source":null}\n\n'], true).res; },
        subscribe: async (b) => { subs.push(b); return {}; },
      };
      const c = new LiveConnection(store, { transport: t, clientId: "client-0002", backoff: { min: 1000, max: 8000 } });
      c.start();
      await flush();
      expect(store.status.state).toBe("not_connected");
      await vi.advanceTimersByTimeAsync(999);
      expect(calls).toBe(1);
      await vi.advanceTimersByTimeAsync(2);
      expect(calls).toBe(2);
      await vi.advanceTimersByTimeAsync(2000);                          // doubled
      expect(calls).toBe(3);
      await flush();
      expect(store.status.state).toBe("market_closed");
      expect(subs).toHaveLength(1);
      c.stop();
    } finally {
      vi.useRealTimers();
    }
  });
});
