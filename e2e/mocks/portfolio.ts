// /api/portfolio mocks (INTEGRATION.md §11): a scenario per state the page must show — positions from both brokers
// (USD and CAD, a broker FX rate), nothing held, Questrade missing its account permission, no broker connected — and
// the "Show my positions" settings. Stateful like the engine; tests set the scenario and read the requests made.
import type { Portfolio, PortfolioAggregate, PortfolioPosition, PortfolioRisk, PortfolioSettings, PortfolioSource, PortfolioSums } from "../../src/api/types";

export type Scenario = "positions" | "empty" | "needs_permission" | "none";
let scenario: Scenario = "positions";
let saved: PortfolioSettings = { questrade: true, ibkr: true };
/** Every GET /api/portfolio's `refresh` flag (on-demand fetches). */
export const refreshes: boolean[] = [];
/** A price step added to AAPL on each call (live repricing between polls). */
let tick = 0;

export function resetPortfolio(s: Scenario = "positions"): void {
  scenario = s;
  saved = { questrade: true, ibkr: true };
  refreshes.length = 0;
  tick = 0;
}
export const setScenario = (s: Scenario) => { scenario = s; };
export const settings = (): PortfolioSettings => saved;
export function setSettings(b: Partial<PortfolioSettings>): PortfolioSettings { saved = { ...saved, ...b }; return saved; }

export const NEEDS_PERMISSION = "Enable 'Retrieve balances, positions, orders and executions' for your Questrade app (API Centre → your personal app), then generate a new manual token and paste it in Settings → Data sources → Questrade. Market data keeps working meanwhile; OptionLens only reads accounts, positions and balances.";
const AT = "2026-11-24T15:01:00-05:00";

function source(broker: "questrade" | "ibkr", state: PortfolioSource["state"], positions: number, detail: string | null = null): PortfolioSource {
  const enabled = saved[broker];
  return { broker, label: broker === "ibkr" ? "IBKR" : "Questrade", enabled, state: enabled ? state : "disabled", detail: enabled ? detail : null,
    fetched_at: enabled && (state === "ok") ? AT : null, attempted_at: enabled ? AT : null, accounts: enabled && state === "ok" ? 1 : 0, positions: enabled && state === "ok" ? positions : 0 };
}

const sums = (o: Record<string, number | null>, unknown: Record<string, number> = {}): PortfolioSums => ({ ...o, unknown }) as PortfolioSums;

function agg(byCurrency: Record<string, Record<string, number | null>>, currency: string, convert: ((c: string) => number) | null): PortfolioAggregate {
  const by = Object.fromEntries(Object.entries(byCurrency).map(([c, v]) => [c, sums(v)]));
  let values: PortfolioSums | null = null;
  if (convert) {
    const out: Record<string, number | null> = {};
    for (const [c, v] of Object.entries(byCurrency)) for (const [k, x] of Object.entries(v)) if (x !== null) out[k] = (out[k] ?? 0) + x * convert(c);
    values = sums(out);
  }
  return { by_currency: by, converted: { currency, values, missing_fx: convert ? [] : Object.keys(byCurrency).filter((c) => c !== currency), rates: [] }, positions: 1 };
}

function pos(p: Partial<PortfolioPosition> & Pick<PortfolioPosition, "symbol" | "underlying" | "broker" | "kind" | "qty">): PortfolioPosition {
  return {
    id: `${p.broker}:acct:${p.symbol}`, account_id: "acct", account: p.broker === "ibkr" ? "•••567" : "Margin •••145", osi: null, expiry: null, right: null, strike: null,
    multiplier: 1, currency: "USD", avg_cost: null, price: null, price_source: null, price_ts: AT, delayed: false, spot: null, iv: null, market_value: null, cost_basis: null,
    unrealized_pnl: null, unrealized_pct: null, day_pnl: null, day_pnl_basis: null, delta: null, delta_dollars: null, gamma: null, theta: null, vega: null, beta: null,
    bwd_delta_spy: null, linkable: true, ...p,
  };
}

export function portfolio(currency: string, refresh: boolean): Portfolio {
  refreshes.push(refresh);
  tick += 1;
  const sources = scenario === "none"
    ? [source("questrade", "not_connected", 0, "Connect Questrade in Settings → Data sources."), source("ibkr", "waiting", 0, "Waiting for the IBKR bridge (IB Gateway / TWS connected, read-only API is enough).")]
    : scenario === "needs_permission"
      ? [source("questrade", "needs_permission", 0, NEEDS_PERMISSION), source("ibkr", "waiting", 0, "Waiting for the IBKR bridge (IB Gateway / TWS connected, read-only API is enough).")]
      : [source("questrade", "ok", scenario === "empty" ? 0 : 2), source("ibkr", "ok", scenario === "empty" ? 0 : 2)];
  const empty: Portfolio = {
    currency, asof: AT, sources, fx: [], totals: agg({}, currency, null), balance_totals: agg({}, currency, null),
    beta_weighted: { spy_shares: null, dollars: null, spy_price: 600, positions: 0, excluded: 0 }, brokers: [], accounts: [], underlyings: [],
    live: { symbols: ["SPY"], contracts: [] },
  };
  if (scenario !== "positions") return empty;
  const price = 230 + tick * 0.5;
  const aaplStock = pos({ broker: "ibkr", kind: "stock", symbol: "AAPL", underlying: "AAPL", qty: 100, avg_cost: 200, price, price_source: "live", spot: price,
    market_value: price * 100, cost_basis: 20_000, unrealized_pnl: (price - 200) * 100, unrealized_pct: (price - 200) / 200, day_pnl: 150, day_pnl_basis: "prev_close",
    delta: 100, delta_dollars: price * 100, gamma: 0, theta: 0, vega: 0, beta: 1.2, bwd_delta_spy: 46 });
  const aaplCall = pos({ broker: "questrade", kind: "option", symbol: "AAPL261218C00250000", underlying: "AAPL", osi: "AAPL261218C00250000", expiry: "2026-12-18",
    right: "C", strike: 250, qty: -1, multiplier: 100, avg_cost: 4.1, price: 3.2, price_source: "live", spot: price, iv: 0.28, market_value: -320, cost_basis: -410,
    unrealized_pnl: 90, unrealized_pct: 0.22, day_pnl: -12, day_pnl_basis: "broker", delta: -38, delta_dollars: -38 * price, gamma: -1.1, theta: 9.5, vega: -21, beta: 1.2, bwd_delta_spy: -17 });
  const thi = pos({ broker: "questrade", kind: "stock", symbol: "THI.TO", underlying: "THI.TO", currency: "CAD", qty: 100, avg_cost: 60.23, price: 60.17, price_source: "broker",
    market_value: 6017, cost_basis: 6023, unrealized_pnl: -6, unrealized_pct: -0.001, delta: 100, delta_dollars: 6017, gamma: 0, theta: 0, vega: 0, linkable: false });
  const spy = pos({ broker: "ibkr", kind: "stock", symbol: "SPY", underlying: "SPY", qty: 10, avg_cost: 550, price: 600, price_source: "stored", spot: 600, market_value: 6000,
    cost_basis: 5500, unrealized_pnl: 500, unrealized_pct: 0.0909, delta: 10, delta_dollars: 6000, gamma: 0, theta: 0, vega: 0, beta: 1, bwd_delta_spy: 10 });
  const rate = 1.38;
  const conv = (c: string) => (c === currency ? 1 : c === "CAD" ? 1 / rate : rate);
  const usd = { market_value: price * 100 - 320 + 6000, cost_basis: 20_000 - 410 + 5500, unrealized_pnl: (price - 200) * 100 + 90 + 500, day_pnl: 138, delta_dollars: price * 62 + 6000, theta: 9.5, vega: -21 };
  const cad = { market_value: 6017, cost_basis: 6023, unrealized_pnl: -6, day_pnl: null, delta_dollars: 6017, theta: 0, vega: 0 };
  const fx = [{ base: "USD", quote: "CAD", rate, source: "Questrade balances (combined CAD ÷ combined USD)", asof: AT, label: `1 USD = ${rate.toFixed(4)} CAD (Questrade balances (combined CAD ÷ combined USD))` }];
  const group = (u: string, rs: PortfolioPosition[], byCur: Record<string, Record<string, number | null>>) => ({
    underlying: u, currencies: [rs[0].currency], spot: rs[0].spot, linkable: rs.every((r) => r.linkable), risk: rs.some((r) => r.kind === "option"),
    totals: agg(byCur, currency, conv), bwd_delta_spy: null, positions: rs,
  });
  return {
    ...empty, fx,
    totals: { ...agg({ USD: usd, CAD: cad }, currency, conv), positions: 4 },
    balance_totals: agg({ USD: { net_liquidation: 50_000, cash: 10_000, buying_power: 80_000 }, CAD: { net_liquidation: 249_988.7, cash: 243_971.7, buying_power: 496_367.2 } }, currency, conv),
    beta_weighted: { spy_shares: 39, dollars: 23_400, spy_price: 600, positions: 3, excluded: 1 },
    accounts: [
      { broker: "questrade", account_id: "q1", account: "Margin •••145", balances: [{ currency: "CAD", net_liquidation: 249_988.7, cash: 243_971.7, buying_power: 496_367.2 }], balance_totals: agg({}, currency, conv), totals: { ...agg({}, currency, conv), positions: 2 } },
      { broker: "ibkr", account_id: "i1", account: "•••567", balances: [{ currency: "USD", net_liquidation: 50_000, cash: 10_000, buying_power: 80_000 }], balance_totals: agg({}, currency, conv), totals: { ...agg({}, currency, conv), positions: 2 } },
    ],
    underlyings: [
      group("AAPL", [aaplStock, aaplCall], { USD: { market_value: price * 100 - 320, unrealized_pnl: (price - 200) * 100 + 90, day_pnl: 138, delta: 62, delta_dollars: price * 62, gamma: -1.1, theta: 9.5, vega: -21 } }),
      group("SPY", [spy], { USD: { market_value: 6000, unrealized_pnl: 500, day_pnl: null, delta: 10, delta_dollars: 6000, gamma: 0, theta: 0, vega: 0 } }),
      group("THI.TO", [thi], { CAD: { market_value: 6017, unrealized_pnl: -6, day_pnl: null, delta: 100, delta_dollars: 6017, gamma: 0, theta: 0, vega: 0 } }),
    ],
    live: { symbols: ["AAPL", "SPY"], contracts: ["AAPL261218C00250000"] },
  };
}

export function risk(underlying: string): PortfolioRisk {
  const prices = Array.from({ length: 121 }, (_, i) => 150 + i);
  const pnl = (s: number) => (s - 200) * 100 - Math.max(0, s - 250) * 100 + 410;
  return {
    underlying, currency: "USD", spot: 231, prices, pnl_expiry: prices.map(pnl), pnl_eval: prices.map((s) => pnl(s) - 50), pnl_today: prices.map((s) => pnl(s) - 50),
    breakevens: [195.9], max_profit: 5410, max_loss: null, cost: 19_590, expiry_used: "2026-12-18", pop: 0.6, ev_iv: null, ev_hv: null,
    greeks: { delta: 62, gamma: -1.1, theta: 9.5, vega: -21 }, table: [], asof: AT, legs: [],
    positions: [{ symbol: "AAPL", account: "•••567", broker: "ibkr", kind: "stock", qty: 100, avg_cost: 200, osi: null, expiry: null, right: null, strike: null, multiplier: 1 }],
    skipped: [],
  };
}
