import { describe, expect, it } from "vitest";
import type { KeyStats } from "../api/types";
import { atPrice, vsVwap } from "./keyStats";

const KS: KeyStats = {
  symbol: "AAPL", price: 210, pe: 30, eps: 7, market_cap: 3.15e12, shares_outstanding: 15e9, high_52w: 290, low_52w: 160,
  avg_volume_20d: 5e7, avg_volume_3m: 4.8e7, sector: "Technology", industry_group: "Hardware", industry_subgroup: "Computers",
  dividend_yield: 0.005, asof: "2026-11-24", sources: { pe: "questrade", market_cap: "questrade" },
};

describe("key stats at a live price", () => {
  it("recomputes P/E and market cap with the engine's formula", () => {
    const live = atPrice(KS, 224);
    expect(live.pe).toBeCloseTo(32);
    expect(live.market_cap).toBeCloseTo(224 * 15e9);
    expect(live.eps).toBe(7);
    expect(live.sources).toBe(KS.sources);
  });

  it("keeps figures it has no inputs for and never invents a P/E", () => {
    const nasdaqOnly: KeyStats = { ...KS, pe: null, eps: null, shares_outstanding: null, market_cap: 8e10, sources: { market_cap: "nasdaq" } };
    expect(atPrice(nasdaqOnly, 50)).toMatchObject({ pe: null, market_cap: 8e10 });
    expect(atPrice({ ...KS, eps: -1.2 }, 50).pe).toBeNull();          // negative EPS: no P/E
    expect(atPrice(KS, null)).toBe(KS);
  });

  it("measures the price against the VWAP", () => {
    expect(vsVwap(101, 100)).toBeCloseTo(0.01);
    expect(vsVwap(101, null)).toBeNull();
    expect(vsVwap(null, 100)).toBeNull();
  });
});
