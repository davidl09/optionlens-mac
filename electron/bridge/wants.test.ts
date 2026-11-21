import { describe, expect, it } from "vitest";
import { durationFor, EOD_RETRY_MS, ledgerKey, parseWants, planWants, WantsError, type PlanContext } from "./wants";

const RAW = {
  iv_history: [{ symbol: "AAPL", from: "2016-01-01" }, { symbol: "msft", from: "2026-11-01" }],
  hv_history: [{ symbol: "AAPL", from: "2016-01-01" }, { symbol: "msft", from: "2026-11-01" }],
  history_symbols: 5,
  underlying_stats: { symbols: ["AAPL", "MSFT", "KO"], max_age_s: 1800 },
  chain_universe: { symbols: ["AAPL", "MSFT", "TSLA"], max_age_s: 3600, expiries: 6, moneyness: 0.2 },
  eod_chains: { session_date: "2026-11-20", symbols: ["AAPL", "SPX"], not_before: "2026-11-20T16:20:00-05:00", deadline: "2026-11-20T20:00:00-05:00" },
  option_bars: [{ symbol: "AAPL", sessions: ["2026-11-18"], expiries: 6, moneyness: 0.15 }],
  index_symbols: ["VIX", "VIX9D"],
  index_bars: [{ symbol: "SPX", from: "2016-11-21" }, { symbol: "vix", from: "2026-11-19" }],
  chains_on_demand_max_age_s: 120,
};

const ctx = (now: number, o: Partial<PlanContext> = {}): PlanContext => ({
  now, lastChainPush: () => undefined, lastStatsPush: () => undefined, attemptedWithin: () => false, ...o,
});

describe("parseWants", () => {
  it("parses the full §6 shape", () => {
    const w = parseWants(RAW);
    expect(w.ivHistory[1]).toEqual({ symbol: "MSFT", from: "2026-11-01" });
    expect(w.chainUniverse).toEqual({ symbols: ["AAPL", "MSFT", "TSLA"], maxAgeS: 3600, shape: { expiries: 6, moneyness: 0.2 } });
    expect(w.eodChains?.notBefore).toBe(Date.parse("2026-11-20T21:20:00Z"));
    expect(w.optionBars[0]).toEqual({ symbol: "AAPL", sessions: ["2026-11-18"], shape: { expiries: 6, moneyness: 0.15 } });
    expect(w.indexBars).toEqual([{ symbol: "SPX", from: "2016-11-21" }, { symbol: "VIX", from: "2026-11-19" }]);
    expect(w.hvHistory[1]).toEqual({ symbol: "MSFT", from: "2026-11-01" });
    expect(w.historySymbols).toBe(5);
  });

  it("plans IV and HV history per series: a current HV series is not fetched again from 10 years back", () => {
    const w = parseWants({ iv_history: [{ symbol: "AAPL", from: "2026-11-20" }], hv_history: [] });
    expect(planWants(w, ctx(Date.parse("2026-11-21T15:00:00Z"))).volHistory).toEqual([{ symbol: "AAPL", from: "2026-11-20", kind: "iv30" }]);
  });

  it("treats absent sections as nothing wanted (older servers)", () => {
    const w = parseWants({ iv_history: [], eod_chains: null, index_symbols: ["VIX"] });
    expect(w.underlyingStats).toBeNull();
    expect(w.chainUniverse).toBeNull();
    expect(w.optionBars).toEqual([]);
    expect(w.onDemandMaxAgeS).toBe(120);
    expect(w.hvHistory).toEqual([]);
    expect(w.historySymbols).toBeNull();
  });

  it("fails fast on malformed sections", () => {
    expect(() => parseWants({ ...RAW, iv_history: [{ symbol: "AAPL", from: "yesterday" }] })).toThrow(WantsError);
    expect(() => parseWants({ ...RAW, chain_universe: { ...RAW.chain_universe, moneyness: 5 } })).toThrow(/moneyness/);
    expect(() => parseWants({ ...RAW, index_symbols: "VIX" })).toThrow(/index_symbols/);
    expect(() => parseWants({ ...RAW, index_bars: [{ symbol: "SPX" }] })).toThrow(/index_bars\[0\]\.from/);
    expect(() => parseWants(null)).toThrow(WantsError);
  });
});

describe("planWants", () => {
  const w = parseWants(RAW);
  const duringSession = Date.parse("2026-11-20T15:00:00Z");
  const afterClose = Date.parse("2026-11-20T22:00:00Z");

  it("plans universe chains, stats, index work and backfills during the session (no EOD yet)", () => {
    const p = planWants(w, ctx(duringSession));
    expect(p.eod).toBeNull();
    expect(p.universe.symbols).toEqual(["AAPL", "MSFT", "TSLA"]);
    expect(p.stats).toEqual(["AAPL", "MSFT", "KO"]);
    expect(p.indexQuotes).toEqual(["VIX", "VIX9D"]);
    expect(p.volHistory).toHaveLength(4);   // iv30 + hv30 for each
    expect(p.optionBars).toHaveLength(1);
  });

  it("in the EOD window plans closing chains and leaves those symbols out of the universe refresh", () => {
    const p = planWants(w, ctx(afterClose));
    expect(p.eod).toEqual({ sessionDate: "2026-11-20", symbols: ["AAPL", "SPX"] });
    expect(p.universe.symbols).toEqual(["MSFT", "TSLA"]);
  });

  it("respects freshness and the attempt ledger", () => {
    const p = planWants(w, ctx(afterClose, {
      lastChainPush: (s) => (s === "MSFT" ? afterClose - 60_000 : undefined),
      lastStatsPush: (s) => (s === "KO" ? afterClose - 3600_000 : afterClose - 60_000),
      attemptedWithin: (k, ms) => (k === ledgerKey.eod("2026-11-20", "SPX") && ms === EOD_RETRY_MS) || k === ledgerKey.vol("iv30", "AAPL") || k === "bars:VIX",
    }));
    expect(p.eod?.symbols).toEqual(["AAPL"]);
    expect(p.universe.symbols).toEqual(["TSLA"]);
    expect(p.stats).toEqual(["KO"]);
    expect(p.volHistory.map((v) => `${v.kind}:${v.symbol}`)).toEqual(["iv30:MSFT", "hv30:AAPL", "hv30:MSFT"]);
    expect(p.indexBars).toEqual([{ symbol: "SPX", from: "2016-11-21" }]);
  });
});

describe("durationFor", () => {
  const today = new Date("2026-11-20T12:00:00Z");
  it("uses days up to a year, then whole years capped at 10", () => {
    expect(durationFor("2026-11-20", today)).toBe("2 D");
    expect(durationFor("2026-11-01", today)).toBe("20 D");
    expect(durationFor("2025-06-01", today)).toBe("2 Y");
    expect(durationFor("2010-01-01", today)).toBe("10 Y");
  });
  it("counts days to the New York date, not the UTC one", () => {
    expect(durationFor("2026-11-01", new Date("2026-11-21T02:00:00Z"))).toBe("20 D");   // 21:00 ET on the 20th
  });
});
