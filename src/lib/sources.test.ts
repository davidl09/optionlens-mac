import { describe, expect, it } from "vitest";
import type { BridgeStatus } from "../../electron/shared/api";
import type { QuestradeStatus, RefreshSource, RefreshStatus } from "../api/types";
import { problemText, summarize } from "./refresh";
import { failureText, ibkrView, questradeView } from "./sources";

const BRIDGE: BridgeStatus = {
  enabled: true, tws: "connected", port: 4001, endpoint: "IB Gateway (live)", accountType: "live", paused: null, lastError: null,
  notes: [], lastPushAt: null, current: null, queued: {} as BridgeStatus["queued"], backfill: { total: 0, done: 0, failed: 0 },
  populate: null, contractsAvailable: true, optionsSubscription: "unknown",
  farms: { marketData: { state: "ok", since: null, server: null }, historical: { state: "ok", since: null, server: null }, secDef: { state: "ok", since: null, server: null } },
  optionsDelayed: true, lastChainAt: "2026-11-20T15:00:00Z", lastChainRound: { at: "2026-11-20T15:10:00Z", detail: "40 chains, 2 failed" },
  failures: [{ reason: "TWS error 200: No security definition has been found for the request", count: 2 }], linesPerBatch: 90,
};

const QT: QuestradeStatus = {
  name: "questrade", state: "needs_attention", refreshed_at: null, calls_last_hour: 2283, hour_budget: 10500, remaining: null,
  reset_at: null, paused_until: null, delayed: null, expiries: 8, moneyness: 0.25, last_chain_at: null, last_sweep_at: null,
  last_sweep_detail: null, verification: null,
  detail: "Your Questrade app lacks the 'Retrieve delayed and real-time market data' permission. In Questrade's API Centre enable it for your personal app, generate a new manual token, and paste it here.",
  failures: [{ reason: "403 code 1016 (missing market-data permission)", count: 1141 }],
};

describe("source views", () => {
  it("give IBKR and Questrade the same fields", () => {
    const ib = ibkrView(BRIDGE);
    const qt = questradeView(QT);
    expect(Object.keys(ib).sort()).toEqual(Object.keys(qt).sort());
    expect(ib).toMatchObject({ state: "connected", tone: "pos", active: true, dataQuality: "delayed (no OPRA subscription)", lastSweepDetail: "40 chains, 2 failed", usage: "90 market-data lines per batch" });
    expect(qt).toMatchObject({ state: "needs attention", tone: "warn", active: false, problem: QT.detail, usage: "2,283 / 10,500 calls this hour" });
    expect(failureText(qt.failures[0])).toBe("1,141 × 403 code 1016 (missing market-data permission)");
    expect(ibkrView({ ...BRIDGE, tws: "disconnected", lastError: null })).toMatchObject({ state: "not connected", active: false, problem: "Not connected to IB Gateway / TWS", usage: null });
    expect(questradeView({ ...QT, state: "connected", delayed: false })).toMatchObject({ problem: null, dataQuality: "real-time" });
  });
});

const src = (name: string, state: RefreshSource["state"], o: Partial<RefreshSource> = {}): RefreshSource =>
  ({ name, state, at: "2026-11-20T15:00:00Z", message: null, waiting_for: null, reason: null, ...o });
const round = (sources: RefreshSource[], o: Partial<RefreshStatus> = {}): RefreshStatus =>
  ({ symbol: "DELL", started_at: "2026-11-20T15:00:00Z", pending: sources.some((s) => s.state === "pending"), sources, served: null, ...o });

describe("refresh summary", () => {
  it("is source-neutral while fetching and once a chain is served", () => {
    expect(summarize(null)).toEqual({ kind: "none" });
    expect(summarize(round([]))).toEqual({ kind: "no-source" });
    expect(summarize(round([src("questrade", "ok")]), true)).toEqual({ kind: "pending", sources: ["IBKR", "Questrade"] });   // bridge asked, not reported yet
    expect(summarize(round([src("ibkr", "pending"), src("questrade", "ok")]))).toEqual({ kind: "pending", sources: ["IBKR", "Questrade"] });
    expect(summarize(round([src("ibkr", "failed", { message: "boom" }), src("questrade", "ok")], { served: { source: "questrade", fetched_at: "x", delayed: false } })))
      .toEqual({ kind: "ok", source: "questrade", delayed: false });
  });
  it("names a source only when it has the problem and no other delivered", () => {
    const qt = src("questrade", "failed", { reason: "403 code 1016 (missing market-data permission)", message: QT.detail });
    const ib = src("ibkr", "unavailable", { waiting_for: "contract-server", message: "IBKR's contract server is unavailable" });
    const s = summarize(round([ib, qt]));
    expect(s).toMatchObject({ kind: "failed", text: "IBKR contract server unavailable; Questrade: missing market-data permission" });
    expect(summarize(round([ib]))).toEqual({ kind: "waiting", text: "IBKR contract server unavailable", waitingFor: "contract-server" });
    expect(problemText(src("ibkr", "failed", { message: "TWS error 200" }))).toBe("IBKR: TWS error 200");
    expect(summarize(round([src("ibkr", "no_options"), src("questrade", "unavailable", { message: "Questrade does not list DELL" })]))).toEqual({ kind: "no-options" });
  });
});
