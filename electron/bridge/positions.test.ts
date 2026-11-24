import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Logger } from "../main/log";
import { emptyMarket, FakeSession } from "./fake-tws.test-helper";
import type { IngestPositions } from "./ingest";
import { maskAccount } from "./mask";
import { positionRow, PositionsFeed } from "./positions";
import type { TwsPosition } from "./tws";

const stk = (account: string, symbol: string, pos: number, avgCost: number, conId: number): TwsPosition =>
  ({ account, pos, avgCost, contract: { symbol, secType: "STK", currency: "USD", conId, expiry: null, strike: null, right: null, multiplier: null, tradingClass: null } });
const opt: TwsPosition = { account: "U1234567", pos: -2, avgCost: 245.5, contract: { symbol: "SPX", secType: "OPT", currency: "USD", conId: 9, expiry: "20261218", strike: 6000, right: "C", multiplier: 100, tradingClass: "SPXW" } };

function setup(fail = false) {
  const bodies: IngestPositions[] = [];
  const logs: string[] = [];
  const log: Logger = { info: (m) => logs.push(m), warn: (m) => logs.push(m), error: (m) => logs.push(m) };
  const feed = new PositionsFeed({ ingest: { positions: async (b) => { if (fail) throw new Error("engine down"); bodies.push(b); return {}; } }, log, pushMs: 60_000, debounceMs: 1_000 });
  const session = new FakeSession(emptyMarket(), ["U1234567"]);
  return { feed, session, bodies, logs };
}

describe("PositionsFeed", () => {
  beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(new Date("2026-11-24T15:00:00Z")); });
  afterEach(() => { vi.useRealTimers(); });

  it("pushes once the first list is complete, after changes (debounced) and every minute", async () => {
    const { feed, session, bodies } = setup();
    feed.update(session, true);
    const p = session.positionSubs[0].h;
    const s = session.summarySubs[0].h;
    p.onData(stk("U1234567", "AAPL", 100, 180, 1));
    p.onData(opt);
    await vi.advanceTimersByTimeAsync(5_000);
    expect(bodies).toHaveLength(0);                                   // not complete yet
    s.onData({ account: "U1234567", tag: "NetLiquidation", value: "50000.5", currency: "USD" });
    s.onData({ account: "U1234567", tag: "TotalCashValue", value: "1000", currency: "USD" });
    s.onData({ account: "U1234567", tag: "BuyingPower", value: "80000", currency: "USD" });
    s.onData({ account: "U1234567", tag: "ExchangeRate", value: "0.72", currency: "CAD" });
    s.onData({ account: "U1234567", tag: "ExchangeRate", value: "1", currency: "BASE" });
    p.onEnd();
    await vi.advanceTimersByTimeAsync(10);
    expect(bodies).toHaveLength(1);
    expect(bodies[0].positions).toEqual([positionRow(stk("U1234567", "AAPL", 100, 180, 1)), positionRow(opt)]);
    expect(bodies[0].positions[1]).toMatchObject({ sec_type: "OPT", expiry: "2026-12-18", strike: 6000, right: "C", trading_class: "SPXW", avg_cost: 245.5, multiplier: 100 });
    expect(bodies[0].accounts).toEqual([{ account: "U1234567", currency: "USD", net_liquidation: 50000.5, cash: 1000, buying_power: 80000 }]);
    expect(bodies[0].fx).toEqual([{ currency: "CAD", base: "USD", rate: 0.72 }]);
    p.onData(stk("U1234567", "AAPL", 0, 180, 1));                     // closed: dropped
    p.onData(stk("U1234567", "MSFT", 5, 400, 2));
    await vi.advanceTimersByTimeAsync(1_100);
    expect(bodies).toHaveLength(2);                                   // both changes in one push
    expect(bodies[1].positions.map((x) => x.symbol)).toEqual(["SPX", "MSFT"]);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(bodies.length).toBe(3);                                    // the minute's push
  });

  it("stops: cancels both subscriptions, forgets everything, pushes nothing more", async () => {
    const { feed, session, bodies } = setup();
    feed.update(session, true);
    session.positionSubs[0].h.onData(opt);
    session.positionSubs[0].h.onEnd();
    await vi.advanceTimersByTimeAsync(10);
    feed.update(session, false);
    expect(session.positionSubs[0].cancelled && session.summarySubs[0].cancelled).toBe(true);
    await vi.advanceTimersByTimeAsync(120_000);
    expect(bodies).toHaveLength(1);
    feed.update(session, true);                                       // wanted again: a fresh subscription
    expect(session.positionSubs).toHaveLength(2);
    expect(feed.body().positions).toEqual([]);
  });

  it("never logs an account number", async () => {
    const { feed, session, logs } = setup(true);
    feed.update(session, true);
    session.summarySubs[0].h.onData({ account: "U1234567", tag: "NetLiquidation", value: "1", currency: "USD" });
    session.positionSubs[0].h.onData(opt);
    session.positionSubs[0].h.onEnd();
    await vi.advanceTimersByTimeAsync(10);
    expect(logs.some((l) => l.includes("positions push failed") && l.includes("•••567"))).toBe(true);
    expect(logs.join("\n")).not.toContain("1234567");
    expect(maskAccount("U1234567")).toBe("•••567");
    expect(maskAccount("12")).toBe("•••");
  });
});
