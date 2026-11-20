import { describe, expect, it } from "vitest";
import { assembleChain, assembleContract, isoDate, pickExpiries, selectContracts, underlyingContract } from "./chain";
import { bestPrice, TICK, TickAccumulator } from "./ticks";
import type { TwsContract } from "./tws";

const opt = (expiry: string, strike: number, right: "C" | "P"): TwsContract => ({ symbol: "AAPL", secType: "OPT", exchange: "SMART", currency: "USD", lastTradeDateOrContractMonth: expiry, strike, right, conId: strike * 10 + (right === "C" ? 1 : 2) });

describe("tick sanitizing", () => {
  it("drops TWS sentinels and garbage, keeps real zero bids", () => {
    const a = new TickAccumulator();
    a.apply({ kind: "price", field: TICK.BID, value: 0 });
    a.apply({ kind: "price", field: TICK.ASK, value: -1 });
    a.apply({ kind: "price", field: TICK.LAST, value: Number.MAX_VALUE });
    a.apply({ kind: "size", field: TICK.VOLUME, value: NaN });
    a.apply({ kind: "option", field: TICK.MODEL_OPTION, iv: -1, delta: -2, gamma: -2, vega: -2, theta: -2 });
    const s = a.snapshot();
    expect(s.bid).toBe(0);
    expect(s.ask).toBeNull();
    expect(s.last).toBeNull();
    expect(s.volume).toBeNull();
    expect(s.greeks).toBeNull();   // nothing computed
  });

  it("prefers model greeks over bid/ask computations and keeps a put delta of -1", () => {
    const a = new TickAccumulator();
    a.apply({ kind: "option", field: TICK.BID_OPTION, iv: 0.3, delta: -0.9, gamma: 0.01, vega: 0.1, theta: -0.05 });
    a.apply({ kind: "option", field: TICK.MODEL_OPTION, iv: 0.25, delta: -1, gamma: 0, vega: 0.2, theta: -0.04 });
    a.apply({ kind: "option", field: TICK.ASK_OPTION, iv: 0.5, delta: -0.5, gamma: 0.02, vega: 0.3, theta: -0.06 });
    expect(a.snapshot().greeks).toEqual({ iv: 0.25, delta: -1, gamma: 0, vega: 0.2, theta: -0.04 });
  });

  it("maps delayed tick ids (no OPRA) to the same fields and flags the snapshot", () => {
    const a = new TickAccumulator();
    a.apply({ kind: "price", field: TICK.DELAYED_BID, value: 1.1 });
    a.apply({ kind: "price", field: TICK.DELAYED_ASK, value: 1.2 });
    a.apply({ kind: "size", field: TICK.DELAYED_VOLUME, value: 10 });
    a.apply({ kind: "option", field: TICK.DELAYED_MODEL_OPTION, iv: 0.3, delta: 0.4, gamma: 0.01, vega: 0.1, theta: -0.01 });
    const s = a.snapshot();
    expect(s).toMatchObject({ bid: 1.1, ask: 1.2, volume: 10, delayed: true, oi: null, callOi: null });
    expect(a.optionComplete()).toBe(true);
    const live = new TickAccumulator();
    live.apply({ kind: "price", field: TICK.BID, value: 1 });
    expect(live.snapshot().delayed).toBe(false);
  });

  it("a previous close alone is not a current price (waits for last or bid/ask; close only as the fallback)", () => {
    const a = new TickAccumulator();
    a.apply({ kind: "price", field: TICK.CLOSE, value: 98 });
    expect(a.priceComplete()).toBe(false);
    expect(bestPrice(a.snapshot())).toBe(98);
    a.apply({ kind: "price", field: TICK.BID, value: 99.9 });
    expect(a.priceComplete()).toBe(false);
    a.apply({ kind: "price", field: TICK.ASK, value: 100.1 });
    expect(a.priceComplete()).toBe(true);
    expect(bestPrice(a.snapshot())).toBeCloseTo(100, 9);
  });

  it("ignores unknown fields and never throws on odd events", () => {
    const a = new TickAccumulator();
    expect(() => {
      a.apply({ kind: "price", field: 999, value: 1 });
      a.apply({ kind: "generic", field: TICK.OPTION_IMPLIED_VOL, value: 1e300 });
      a.apply({ kind: "option", field: 55, iv: 0.2 });
    }).not.toThrow();
    expect(a.snapshot().impliedVol).toBeNull();
  });
});

describe("chain planning", () => {
  it("maps indices to IND contracts and share classes to IBKR spelling", () => {
    expect(underlyingContract("spx")).toEqual({ symbol: "SPX", secType: "IND", exchange: "CBOE", currency: "USD" });
    expect(underlyingContract("BRK.B")).toEqual({ symbol: "BRK B", secType: "STK", exchange: "SMART", currency: "USD" });
  });

  it("picks the first N unexpired expiries, preferring the trading class named like the symbol", () => {
    const params = [
      { exchange: "SMART", tradingClass: "SPXW", multiplier: "100", expirations: ["20261119", "20261120", "20261123", "20261218"], strikes: [] },
      { exchange: "SMART", tradingClass: "SPX", multiplier: "100", expirations: ["20261218"], strikes: [] },
      { exchange: "CBOE", tradingClass: "SPX", multiplier: "100", expirations: ["20261120"], strikes: [] },
    ];
    const picks = pickExpiries(params, "SPX", new Date("2026-11-20T15:00:00Z"), 3);
    expect(picks.map((p) => `${p.expiry}/${p.tradingClass}`)).toEqual(["20261120/SPXW", "20261123/SPXW", "20261218/SPX"]);
  });

  it("keeps today's (0DTE) expiry in the evening, when the UTC date is already tomorrow", () => {
    const params = [{ exchange: "SMART", tradingClass: "SPXW", multiplier: "100", expirations: ["20261120", "20261123"], strikes: [] }];
    // 19:30 ET on 2026-11-20 = 00:30 UTC on 2026-11-21 (closing chains run until 20:00 ET).
    expect(pickExpiries(params, "SPX", new Date("2026-11-21T00:30:00Z"), 1).map((p) => p.expiry)).toEqual(["20261120"]);
  });

  it("selects strikes within ±moneyness of spot, de-duplicated and sorted", () => {
    const all = [opt("20261218", 130, "C"), opt("20261218", 100, "P"), opt("20261218", 100, "C"), opt("20261218", 74, "C"), opt("20261218", 100, "C"), opt("20261211", 125, "P")];
    const sel = selectContracts(all, 100, 0.25);
    expect(sel.map((c) => `${c.lastTradeDateOrContractMonth}:${c.strike}${c.right}`)).toEqual(["20261211:125P", "20261218:100C", "20261218:100P"]);
  });

  it("parses TWS dates", () => {
    expect(isoDate("20261218")).toBe("2026-12-18");
    expect(isoDate("20261218  16:00:00")).toBe("2026-12-18");
    expect(isoDate("bad")).toBeNull();
  });
});

describe("chain assembly (ticks → ingest JSON)", () => {
  const ticksFor = (events: Parameters<TickAccumulator["apply"]>[0][]) => { const a = new TickAccumulator(); events.forEach((e) => a.apply(e)); return a.snapshot(); };

  it("builds the contract row with the right-specific open interest", () => {
    const q = ticksFor([
      { kind: "price", field: TICK.BID, value: 9.1 }, { kind: "price", field: TICK.ASK, value: 9.3 }, { kind: "price", field: TICK.LAST, value: 9.2 },
      { kind: "size", field: TICK.VOLUME, value: 812 }, { kind: "size", field: TICK.OPTION_CALL_OI, value: 15321 },
      { kind: "option", field: TICK.MODEL_OPTION, iv: 0.251, delta: 0.52, gamma: 0.011, vega: 0.41, theta: -0.12 },
    ]);
    expect(assembleContract(opt("20261218", 350, "C"), q)).toEqual({
      expiry: "2026-12-18", strike: 350, right: "C", bid: 9.1, ask: 9.3, last: 9.2, volume: 812, oi: 15321,
      iv: 0.251, delta: 0.52, gamma: 0.011, theta: -0.12, vega: 0.41,
    });
  });

  it("drops contracts with nothing usable and requires a session date for final chains", () => {
    const empty = ticksFor([]);
    const und = { price: 349.1, bid: 349, ask: 349.2, open: null, high: null, low: null, prev_close: 351, volume: 1e6, ts: "2026-11-20T21:00:00.000Z" };
    const body = assembleChain({ symbol: "AAPL", fetchedAt: und.ts, final: true, sessionDate: "2026-11-20", underlyingDelayed: false }, und, [
      { contract: opt("20261218", 350, "C"), quote: empty },
      { contract: opt("20261218", 350, "P"), quote: ticksFor([{ kind: "price", field: TICK.BID, value: 1 }]) },
    ]);
    expect(body).toMatchObject({ source: "ibkr", symbol: "AAPL", final: true, session_date: "2026-11-20" });
    expect(body.contracts).toHaveLength(1);
    expect(body.contracts[0]).toMatchObject({ right: "P", bid: 1, ask: null, iv: null });
    expect(() => assembleChain({ symbol: "AAPL", fetchedAt: und.ts, final: true, sessionDate: null, underlyingDelayed: false }, und, [])).toThrow(/session date/);
    const live = assembleChain({ symbol: "AAPL", fetchedAt: und.ts, final: false, sessionDate: null, underlyingDelayed: true }, und, []);
    expect("session_date" in live).toBe(false);
    expect(live.delayed).toBe(true);
    expect(body.delayed).toBe(false);
  });
});
