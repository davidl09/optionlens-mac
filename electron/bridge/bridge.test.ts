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

function harness(wants: unknown, sessionMarket = market()) {
  const calls: string[] = [];
  const rec = (name: string) => async (b: unknown) => { calls.push(`${name}:${(b as { symbol?: string })?.symbol ?? ""}`); return {}; };
  const ingest: IngestApi = { wants: async () => wants, chain: rec("chain"), volHistory: rec("vol"), indexBars: rec("bars"), quotes: async () => { calls.push("quotes"); return {}; },
    underlyingStats: async () => { calls.push("stats"); return {}; }, optionBars: rec("obars"), heartbeat: async () => { calls.push("heartbeat"); return {}; } };
  const session = new FakeSession(sessionMarket);
  const statuses: BridgeStatus[] = [];
  const bridge = new Bridge({
    connector: new FakeConnector(new Map([[4001, session]])), ingest, ledger: new Ledger(null, () => NOW), settings: () => DEFAULT_BRIDGE,
    clientName: "optionlens-mac test", log: silentLogger, onStatus: (s) => statuses.push(s), now: () => Date.now(),
    pacer: new HistoricalPacer({ minSpacingMs: 0 }), batchTimeoutMs: 50,
  });
  return { bridge, calls, session, statuses };
}

const WANTS = {
  iv_history: [{ symbol: "AAPL", from: "2026-01-02" }],
  underlying_stats: { symbols: ["AAPL", "KO"], max_age_s: 1800 },
  chain_universe: { symbols: ["MSFT"], max_age_s: 3600, expiries: 1, moneyness: 0.2 },
  eod_chains: { session_date: "2026-11-20", symbols: ["AAPL"], not_before: "2026-11-20T16:20:00-05:00", deadline: "2026-11-20T20:00:00-05:00" },
  index_symbols: ["VIX"],
  chains_on_demand_max_age_s: 120,
};

describe("Bridge", () => {
  beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(NOW); });
  afterEach(() => { vi.useRealTimers(); });

  it("on connect: heartbeats, polls wants and works through them in §6 priority order", async () => {
    const h = harness(WANTS);
    h.bridge.start();
    await vi.advanceTimersByTimeAsync(5_000);
    const work = h.calls.filter((c) => c !== "heartbeat");
    expect(work).toEqual(["chain:MSFT", "stats", "quotes", "bars:VIX", "vol:AAPL", "vol:AAPL"]);
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

  it("reports unavailable when not connected", async () => {
    const h = harness({});
    expect(await h.bridge.refreshSymbol("AAPL")).toMatchObject({ status: "unavailable" });
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
