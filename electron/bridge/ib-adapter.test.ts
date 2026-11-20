import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// A stand-in for @stoqey/ib's IBApi: an event emitter whose request methods record their calls.
vi.mock("@stoqey/ib", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@stoqey/ib")>();
  const { EventEmitter } = await import("node:events");
  class FakeIBApi extends EventEmitter {
    static instances: FakeIBApi[] = [];
    readonly calls: string[] = [];
    constructor() { super(); FakeIBApi.instances.push(this); }
    connect() { this.calls.push("connect"); }
    disconnect() { this.calls.push("disconnect"); }
    reqMarketDataType() { this.calls.push("reqMarketDataType"); }
    reqContractDetails() { this.calls.push("reqContractDetails"); }
    reqMktData() { this.calls.push("reqMktData"); }
    cancelMktData() { this.calls.push("cancelMktData"); }
  }
  return { ...actual, IBApi: FakeIBApi };
});

import { EventName, IBApi } from "@stoqey/ib";
import { ibConnector } from "./ib-adapter";
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
    expect(ib.calls).toContain("reqMarketDataType");
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
});
