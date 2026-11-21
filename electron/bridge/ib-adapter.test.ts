import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// A stand-in for @stoqey/ib's IBApi: an event emitter whose request methods record their calls.
vi.mock("@stoqey/ib", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@stoqey/ib")>();
  const { EventEmitter } = await import("node:events");
  class FakeIBApi extends EventEmitter {
    static instances: FakeIBApi[] = [];
    readonly calls: string[] = [];
    /** Contracts of market-data / historical requests, as they would be sent to TWS. */
    readonly sent: { reqId: number; contract: Record<string, unknown> }[] = [];
    constructor() { super(); FakeIBApi.instances.push(this); }
    /** Like real TWS: a request whose expiry is not YYYYMMDD / YYYYMM is rejected with error 321. */
    private validate(reqId: number, contract: Record<string, unknown>): boolean {
      this.sent.push({ reqId, contract });
      const d = contract.lastTradeDateOrContractMonth;
      if (d === undefined || d === "" || /^\d{6}(\d{2})?$/.test(String(d))) return true;
      this.emit("error", new Error("Error validating request.-'bW' : cause - lastTradeDateOrContractMonth: The date entered is invalid. Please use the format YYYYMMDD or YYYYMM"), 321, reqId);
      return false;
    }
    reqHistoricalData(reqId: number, contract: Record<string, unknown>) {
      if (this.validate(reqId, contract)) queueMicrotask(() => { this.emit("historicalData", reqId, "20261120", 1, 2, 0.5, 1.5, 10); this.emit("historicalData", reqId, "finished", -1, -1, -1, -1, -1); });
    }
    cancelHistoricalData() { this.calls.push("cancelHistoricalData"); }
    connect() { this.calls.push("connect"); }
    disconnect() { this.calls.push("disconnect"); }
    reqMarketDataType(type: number) { this.calls.push(`reqMarketDataType:${type}`); }
    reqContractDetails() { this.calls.push("reqContractDetails"); }
    reqMktData(reqId: number, contract: Record<string, unknown>) { this.calls.push("reqMktData"); if (this.validate(reqId, contract)) this.emit("tickPrice", reqId, 1, 2.5); }
    cancelMktData() { this.calls.push("cancelMktData"); }
  }
  return { ...actual, IBApi: FakeIBApi };
});

import { EventName, IBApi } from "@stoqey/ib";
import { detailsExpiry, ibConnector, toIb } from "./ib-adapter";
import { TwsRequestError, TwsTimeoutError, type TwsSession } from "./tws";

type Fake = IBApi & { calls: string[] };
const lastIb = () => (IBApi as unknown as { instances: Fake[] }).instances.at(-1)!;
const connect = () => ibConnector.connect({ host: "127.0.0.1", port: 4001, clientId: 17, timeoutMs: 4000 });

describe("ibConnector", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it("waits for managedAccounts when it arrives after nextValidId (account type is known)", async () => {
    const p = connect();
    const ib = lastIb();
    ib.emit(EventName.nextValidId, 1);
    await vi.advanceTimersByTimeAsync(200);
    ib.emit(EventName.managedAccounts, "DU111,DU222");
    const s = await p;
    expect(s.accounts).toEqual(["DU111", "DU222"]);
    // Delayed-frozen (4) for the whole session: outside market hours every request (the after-hours stats pass, chains)
    // gets the last session's frozen values; without a subscription, 15-minute delayed data.
    expect(ib.calls).toContain("reqMarketDataType:4");
  });

  it("accepts managedAccounts before nextValidId, and connects without accounts after a short grace", async () => {
    const p1 = connect();
    lastIb().emit(EventName.managedAccounts, "U1");
    lastIb().emit(EventName.nextValidId, 1);
    expect((await p1).accounts).toEqual(["U1"]);
    const p2 = connect();
    lastIb().emit(EventName.nextValidId, 1);
    await vi.advanceTimersByTimeAsync(2000);
    expect((await p2).accounts).toEqual([]);
  });

  it("reports a request timeout as a timeout, not as a TWS error code (366 looked like pacing)", async () => {
    const p = connect();
    lastIb().emit(EventName.managedAccounts, "DU1");
    lastIb().emit(EventName.nextValidId, 1);
    const s: TwsSession = await p;
    const details = s.contractDetails({ symbol: "SPX", secType: "OPT", exchange: "SMART", currency: "USD", lastTradeDateOrContractMonth: "20261218" }, new AbortController().signal);
    const outcome = details.then(() => null, (e: unknown) => e);
    await vi.advanceTimersByTimeAsync(61_000);   // option contract details get longer than the 60 s default
    let e: unknown = undefined;
    void outcome.then((v) => { e = v; });
    await vi.advanceTimersByTimeAsync(0);
    expect(e).toBeUndefined();
    await vi.advanceTimersByTimeAsync(120_000);
    e = await outcome;
    expect(e).toBeInstanceOf(TwsTimeoutError);
    expect(e).not.toBeInstanceOf(TwsRequestError);
  });

  it("estimates the outgoing queue backlog at 40 messages/s", async () => {
    const p = connect();
    lastIb().emit(EventName.managedAccounts, "DU1");
    lastIb().emit(EventName.nextValidId, 1);
    const s = await p;
    const cancels = Array.from({ length: 80 }, () => s.stream({ symbol: "AAPL", secType: "STK", exchange: "SMART", currency: "USD" }, "", { onTick: () => undefined, onError: () => undefined }));
    expect(s.sendBacklogMs()).toBeGreaterThanOrEqual(1950);
    expect(s.sendBacklogMs()).toBeLessThanOrEqual(2000);
    for (const c of cancels) c();
    expect(s.sendBacklogMs()).toBeGreaterThanOrEqual(3950);
  });

  it("normalizes contract-details expiries with a time suffix and requests resolved contracts by conId only (TWS error 321)", async () => {
    vi.useRealTimers();
    const p = connect();
    lastIb().emit(EventName.managedAccounts, "DU1");
    lastIb().emit(EventName.nextValidId, 1);
    const s = await p;
    const ib = lastIb() as Fake & { sent: { reqId: number; contract: Record<string, unknown> }[] };
    const details = s.contractDetails({ symbol: "DELL", secType: "OPT", exchange: "SMART", currency: "USD", lastTradeDateOrContractMonth: "20261218" }, new AbortController().signal);
    // What @stoqey/ib 1.6.13 hands over: its decoder splits on the literal "\\s+", so the time and zone stay attached.
    ib.emit(EventName.contractDetails, 1, { contract: { symbol: "DELL", secType: "OPT", lastTradeDateOrContractMonth: "20261218 16:00:00 US/Eastern", lastTradeDate: "20261218", strike: 327.5, right: "P", exchange: "SMART", currency: "USD", conId: 777, tradingClass: "DELL", multiplier: 100 } });
    ib.emit(EventName.contractDetailsEnd, 1);
    const [c] = await details;
    expect(c).toMatchObject({ lastTradeDateOrContractMonth: "20261218", strike: 327.5, right: "P", conId: 777 });
    let bid: number | null = null;
    let err: Error | null = null;
    s.stream(c, "101", { onTick: (e) => { if (e.kind === "price") bid = e.value; }, onError: (e) => { err = e; } });
    await new Promise((r) => setTimeout(r, 50));
    expect(err).toBeNull();
    expect(bid).toBe(2.5);
    const bars = await s.historical(c, { duration: "5 D", whatToShow: "TRADES", useRth: true }, new AbortController().signal);
    expect(bars).toHaveLength(1);
    for (const x of ib.sent) expect(x.contract).toEqual({ conId: 777, exchange: "SMART" });
    // An unresolved contract with a bad expiry is never sent.
    await expect(s.historical({ symbol: "DELL", secType: "OPT", exchange: "SMART", currency: "USD", lastTradeDateOrContractMonth: "2026-12-18", strike: 1, right: "C" }, { duration: "5 D", whatToShow: "TRADES", useRth: true }, new AbortController().signal)).rejects.toThrow(/invalid TWS expiry/);
  });

  it("reads the expiry from lastTradeDate when the combined field is unusable", () => {
    expect(detailsExpiry({ lastTradeDateOrContractMonth: "20261218 16:00:00 US/Eastern" })).toBe("20261218");
    expect(detailsExpiry({ lastTradeDateOrContractMonth: "", lastTradeDate: "20261217" })).toBe("20261217");
    expect(detailsExpiry({ lastTradeDateOrContractMonth: "2026" })).toBeUndefined();
    expect(toIb({ symbol: "SPX", secType: "IND", exchange: "CBOE", currency: "USD" })).toMatchObject({ symbol: "SPX", secType: "IND", exchange: "CBOE" });
  });
});
