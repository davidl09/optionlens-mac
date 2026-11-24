import { describe, expect, it } from "vitest";
import type { PortfolioAggregate, PortfolioPosition } from "../api/types";
import { calculatorLegs, money, shownSums, sumText } from "./portfolio";

const agg = (by: Record<string, Record<string, number | null>>, converted: Record<string, number | null> | null): PortfolioAggregate => ({
  by_currency: Object.fromEntries(Object.entries(by).map(([c, v]) => [c, { ...v, unknown: {} }])) as PortfolioAggregate["by_currency"],
  converted: { currency: "USD", values: converted ? { ...converted, unknown: {} } as PortfolioAggregate["by_currency"][string] : null, missing_fx: converted ? [] : ["CAD"], rates: [] },
  positions: 1,
});

const pos = (p: Partial<PortfolioPosition>): PortfolioPosition => ({
  id: "x", broker: "ibkr", account_id: "a", account: "•••567", kind: "option", symbol: "S", underlying: "AAPL", osi: "AAPL261218C00250000", expiry: "2026-12-18", right: "C", strike: 250,
  qty: -1, multiplier: 100, currency: "USD", avg_cost: 4, price: null, price_source: null, price_ts: null, delayed: null, spot: null, iv: null, market_value: null, cost_basis: null,
  unrealized_pnl: null, unrealized_pct: null, day_pnl: null, day_pnl_basis: null, delta: null, delta_dollars: null, gamma: null, theta: null, vega: null, beta: null, bwd_delta_spy: null,
  linkable: true, ...p,
});

describe("portfolio sums", () => {
  it("show the converted total, else each currency on its own (never added up)", () => {
    expect(sumText(agg({ USD: { market_value: 100 }, CAD: { market_value: 138 } }, { market_value: 200 }), "market_value")).toBe("$200.00");
    expect(sumText(agg({ USD: { market_value: 100 }, CAD: { market_value: 138 } }, null), "market_value")).toBe("$100.00 · CA$138.00");
    expect(shownSums(agg({}, null))).toEqual([]);
    expect(money(null, "USD")).toBe("—");
  });
});

describe("calculator legs", () => {
  it("are the positions at their cost basis; one contract in two accounts is one leg (cost averaged by quantity)", () => {
    const legs = calculatorLegs([
      pos({ kind: "stock", osi: null, expiry: null, right: null, strike: null, qty: 100, multiplier: 1, avg_cost: 200 }),
      pos({ qty: -1, avg_cost: 4 }), pos({ qty: -3, avg_cost: 6, account: "•••145" }),
    ]);
    expect(legs).toEqual([{ kind: "stock", qty: 100, price: 200 }, { kind: "option", right: "C", strike: 250, expiry: "2026-12-18", qty: -4, price: 5.5 }]);
  });
  it("scale other multipliers to contracts of 100 and refuse what the calculator cannot take", () => {
    expect(calculatorLegs([pos({ qty: 2, multiplier: 10 })])![0].qty).toBeCloseTo(0.2);
    expect(calculatorLegs([pos({ avg_cost: null })])).toBeNull();
    expect(calculatorLegs(Array.from({ length: 9 }, (_, i) => pos({ osi: `X${i}`, strike: 200 + i })))).toBeNull();
  });
});
