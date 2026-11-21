import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { silentLogger } from "../main/log";
import { DEFAULT_BRIDGE } from "../main/settings";
import type { BridgeStatus } from "../shared/api";
import { Bridge } from "./bridge";
import { emptyMarket, FakeConnector, FakeSession } from "./fake-tws.test-helper";
import type { IngestApi } from "./ingest";
import { Ledger } from "./ledger";
import { HistoricalPacer } from "./pacer";
import { TICK } from "./ticks";
import { CODE, TwsRequestError } from "./tws";

const NOW = Date.parse("2026-11-20T15:00:00Z");

function market() {
  const m = emptyMarket();
  m.ticks = (id) => (id.endsWith(":STK") || id.endsWith(":IND")
    ? [{ kind: "price", field: TICK.LAST, value: 100 }, { kind: "price", field: TICK.CLOSE, value: 99 }]
    : [{ kind: "price", field: TICK.BID, value: 1 }, { kind: "price", field: TICK.ASK, value: 1.1 }, { kind: "option", field: TICK.MODEL_OPTION, iv: 0.3, delta: 0.5, gamma: 0.01, vega: 0.1, theta: -0.02 }]);
  m.optionParams = [{ exchange: "SMART", tradingClass: "AAPL", multiplier: "100", expirations: ["20261127"], strikes: [] }];
  m.details = (c) => (c.secType === "OPT" ? [100, 105].flatMap((k) => (["C", "P"] as const).map((right) => ({ ...c, strike: k, right, conId: k * 10 + (right === "C" ? 1 : 2) }))) : [{ ...c, conId: 1 }]);
  m.historical = () => [{ time: "20261119", open: 0.2, high: 0.2, low: 0.2, close: 0.2, volume: null }];
  return m;
}

function harness(wants: unknown, sessionMarket = market(), o: { pacer?: HistoricalPacer } = {}) {
  const calls: string[] = [];
  const rec = (name: string) => async (b: unknown) => { calls.push(`${name}:${(b as { symbol?: string })?.symbol ?? ""}`); return {}; };
  const ingest: IngestApi = { wants: async () => (typeof wants === "function" ? wants() : wants), chain: rec("chain"), noOptions: rec("noOptions"), ackWants: async (b) => { calls.push(`ack:${JSON.stringify(b)}`); return {}; }, volHistory: rec("vol"), indexBars: rec("bars"), quotes: async () => { calls.push("quotes"); return {}; },
    underlyingStats: async () => { calls.push("stats"); return {}; }, optionBars: rec("obars"), heartbeat: async () => { calls.push("heartbeat"); return {}; } };
  const session = new FakeSession(sessionMarket);
  const statuses: BridgeStatus[] = [];
  const bridge = new Bridge({
    connector: new FakeConnector(new Map([[4001, session]])), ingest, ledger: new Ledger(null, () => NOW), settings: () => DEFAULT_BRIDGE,
    clientName: "optionlens-mac test", log: silentLogger, onStatus: (s) => statuses.push(s), now: () => Date.now(),
    pacer: o.pacer ?? new HistoricalPacer({ minSpacingMs: 0 }), batchTimeoutMs: 50,
  });
  return { bridge, calls, session, statuses };
}

const WANTS = {
  iv_history: [{ symbol: "AAPL", from: "2026-01-02" }],
  underlying_stats: { symbols: ["AAPL", "KO"], max_age_s: 1800 },
  chain_universe: { symbols: ["MSFT"], max_age_s: 3600, expiries: 1, moneyness: 0.2 },
  eod_chains: { session_date: "2026-11-20", symbols: ["AAPL"], not_before: "2026-11-20T16:20:00-05:00", deadline: "2026-11-20T20:00:00-05:00" },
  index_symbols: ["VIX"],
  index_bars: [{ symbol: "SPX", from: "2016-11-21" }],
  chains_on_demand_max_age_s: 120,
};

describe("Bridge", () => {
  beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(NOW); });
  afterEach(() => { vi.useRealTimers(); });

  it("on connect: heartbeats, polls wants and works through all of them (chains first)", async () => {
    const h = harness(WANTS);
    h.bridge.start();
    await vi.advanceTimersByTimeAsync(5_000);
    const work = h.calls.filter((c) => c !== "heartbeat");
    expect(work[0]).toBe("chain:MSFT");
    expect([...work].sort()).toEqual(["bars:SPX", "chain:MSFT", "quotes", "quotes", "stats", "vol:AAPL", "vol:AAPL"]);   // quotes: VIX family + the stats pass
    expect(h.calls.filter((c) => c === "heartbeat").length).toBeGreaterThanOrEqual(1);
    const last = h.statuses[h.statuses.length - 1];
    expect(last).toMatchObject({ tws: "connected", endpoint: "IB Gateway (live)", accountType: "paper", current: null });
    expect(last.lastPushAt).not.toBeNull();
    expect(last.backfill).toEqual({ total: 2, done: 2, failed: 0 });
    h.bridge.stop();
  });

  it("serves client-triggered refreshes and reports freshness", async () => {
    const h = harness({ iv_history: [], chains_on_demand_max_age_s: 120 });
    h.bridge.start();
    await vi.advanceTimersByTimeAsync(0);
    const p = h.bridge.refreshSymbol("aapl");
    await vi.advanceTimersByTimeAsync(2_000);
    expect(await p).toEqual({ status: "refreshed" });
    expect(h.calls).toContain("chain:AAPL");
    expect(await h.bridge.refreshSymbol("AAPL")).toEqual({ status: "fresh" });
    h.bridge.stop();
  });

  it("a refresh of a symbol IBKR lists no options for reports it (and the engine is told)", async () => {
    const m = market();
    m.optionParams = [];
    const h = harness({ iv_history: [], chains_on_demand_max_age_s: 120 }, m);
    h.bridge.start();
    await vi.advanceTimersByTimeAsync(0);
    const p = h.bridge.refreshSymbol("DELL");
    await vi.advanceTimersByTimeAsync(2_000);
    expect(await p).toEqual({ status: "no-options" });
    expect(h.calls).toContain("noOptions:DELL");
    expect(h.calls).not.toContain("chain:DELL");
    m.optionParams = market().optionParams;                     // options listed later: a normal refresh again
    const again = h.bridge.refreshSymbol("DELL");
    await vi.advanceTimersByTimeAsync(2_000);
    expect(await again).toEqual({ status: "refreshed" });
    h.bridge.stop();
  });

  it("names expiries without quotes in the status (and clears the note once the chain is complete)", async () => {
    const m = market();
    m.optionParams = [{ exchange: "SMART", tradingClass: "DELL", multiplier: "100", expirations: ["20261127", "20261204"], strikes: [] }];
    const quotedTicks = m.ticks;
    m.ticks = (id) => (id.includes(":20261127:") ? [] : quotedTicks(id));
    const h = harness({ iv_history: [], chains_on_demand_max_age_s: 0 }, m);
    h.bridge.start();
    await vi.advanceTimersByTimeAsync(0);
    const p = h.bridge.refreshSymbol("DELL");
    await vi.advanceTimersByTimeAsync(2_000);
    expect(await p).toEqual({ status: "refreshed" });
    expect(h.bridge.status().notes).toContain("Incomplete IBKR chain for DELL: 50% of contracts quoted; no quotes for 2026-11-27");
    m.ticks = quotedTicks;
    const again = h.bridge.refreshSymbol("DELL");
    await vi.advanceTimersByTimeAsync(2_000);
    await again;
    expect(h.bridge.status().notes.some((n) => n.includes("DELL"))).toBe(false);
    h.bridge.stop();
  });

  it("an on-demand refresh pre-empts a running IV-history download, which resumes afterwards", async () => {
    const h = harness({ iv_history: [{ symbol: "MDLN", from: "2016-01-01" }], chains_on_demand_max_age_s: 0 });
    const order: string[] = [];
    let downloads = 0;
    const real = h.session.historical.bind(h.session);
    h.session.historical = (c, r, signal) => {
      downloads += 1;
      order.push(`hist:${c.symbol}:${downloads}`);
      if (downloads > 1) return real(c, r, signal);
      return new Promise((_, reject) => signal!.addEventListener("abort", () => reject(signal!.reason), { once: true }));   // a slow 10-year download
    };
    h.bridge.start();
    await vi.advanceTimersByTimeAsync(10);
    expect(h.bridge.status().current?.kind).toBe("iv-history");
    const p = h.bridge.refreshSymbol("DELL");
    await vi.advanceTimersByTimeAsync(2_000);
    expect(await p).toEqual({ status: "refreshed" });
    expect(h.calls.indexOf("chain:DELL")).toBeLessThan(h.calls.indexOf("vol:MDLN"));
    expect(order[0]).toBe("hist:MDLN:1");
    expect(h.calls.filter((c) => c === "vol:MDLN")).toHaveLength(2);   // IV and HV both done after the refresh
    expect(h.bridge.status().lastError).toBeNull();
    h.bridge.stop();
  });

  it("reports unavailable when not connected", async () => {
    const h = harness({});
    expect(await h.bridge.refreshSymbol("AAPL")).toMatchObject({ status: "unavailable" });
  });

  it("a market-data permission error stops that symbol's option-bar catch-up (not each contract) with a status note", async () => {
    const m = market();
    m.historical = (c) => (c.secType === "OPT" ? new TwsRequestError(162, "Historical Market Data Service error message:No market data permissions for OPRA OPT")
      : [{ time: "20261119", open: 0.2, high: 0.2, low: 0.2, close: 0.2, volume: null }]);
    const h = harness({ option_bars: [{ symbol: "AAPL", sessions: ["2026-11-18"], expiries: 1, moneyness: 0.2 }], iv_history: [{ symbol: "KO", from: "2026-01-02" }] }, m);
    h.bridge.start();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(h.session.historicalCalls.filter((x) => x.contract.secType === "OPT")).toHaveLength(1);   // 4 contracts planned
    expect(h.calls.filter((c) => c === "vol:KO")).toHaveLength(2);                                   // other symbols go on
    expect(h.bridge.status().notes.join(" ")).toMatch(/No IBKR market-data permission \(skipped\): AAPL option bars/);
    h.bridge.stop();
  });

  it("option-bar catch-up resumes after a reconnect without repeating done contracts; acks each plan once", async () => {
    let polls = 0;
    // Like the engine: an accepted plan is not offered again for hours.
    const wants = () => (polls++ === 0 ? { option_bars: [{ symbol: "AAPL", sessions: ["2026-11-18"], expiries: 1, moneyness: 0.2 }] } : {});
    const h = harness(wants, market(), { pacer: new HistoricalPacer({ minSpacingMs: 1000 }) });
    h.bridge.start();
    await vi.advanceTimersByTimeAsync(2_000);
    const opt = () => h.session.historicalCalls.filter((x) => x.contract.secType === "OPT").map((x) => x.contract.conId);
    const before = opt();
    expect(before.length).toBeGreaterThan(0);
    expect(before.length).toBeLessThan(4);
    h.session.emitClose("nightly reset");
    await vi.advanceTimersByTimeAsync(20_000);
    const all = opt();
    expect(all).toHaveLength(4);                        // 2 strikes × C/P, each requested once
    expect(new Set(all).size).toBe(4);
    expect(h.calls.filter((c) => c.startsWith("ack:"))).toEqual([`ack:${JSON.stringify({ kind: "option_bars", items: [{ symbol: "AAPL", sessions: ["2026-11-18"] }] })}`]);
    h.bridge.stop();
  });

  it("pauses work on a competing session instead of retrying", async () => {
    const m = market();
    m.streamErrors = () => new TwsRequestError(CODE.COMPETING_SESSION, "No market data during competing live session");
    const h = harness({ chain_universe: { symbols: ["MSFT", "TSLA"], max_age_s: 3600, expiries: 1, moneyness: 0.2 } }, m);
    h.bridge.start();
    await vi.advanceTimersByTimeAsync(3_000);
    const st = h.bridge.status();
    expect(st.paused).toBe("IBKR session in use elsewhere — market data paused");
    expect(h.session.streams.length).toBe(1);   // TSLA was not attempted while paused
    expect(await h.bridge.refreshSymbol("AAPL")).toEqual({ status: "unavailable", message: st.paused });
    h.bridge.stop();
  });
});
