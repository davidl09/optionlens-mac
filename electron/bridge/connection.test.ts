import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { silentLogger } from "../main/log";
import { DEFAULT_BRIDGE } from "../main/settings";
import type { BridgeSettings } from "../shared/api";
import { accountType, COMPETING_MESSAGE, CONNECTIVITY_MESSAGE, reconnectDelayMs, TwsSupervisor } from "./connection";
import { emptyMarket, FakeConnector, FakeSession } from "./fake-tws.test-helper";
import { CODE, TwsRequestError, type TwsSession } from "./tws";

function setup(connector: FakeConnector, settings: Partial<BridgeSettings> = {}) {
  const sessions: (TwsSession | null)[] = [];
  const sup = new TwsSupervisor({ connector, settings: () => ({ ...DEFAULT_BRIDGE, ...settings }), log: silentLogger, onSession: (s) => sessions.push(s), onChange: () => undefined });
  return { sup, sessions };
}

describe("TwsSupervisor", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it("auto-detects ports in order 4001, 7496, 4002, 7497 and labels the endpoint", async () => {
    const paper = new FakeSession(emptyMarket(), ["DU1234567"]);
    const c = new FakeConnector(new Map([[4002, paper]]));
    const { sup, sessions } = setup(c);
    sup.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(c.attempts.map((a) => a.port)).toEqual([4001, 7496, 4002]);
    expect(sup.state()).toMatchObject({ tws: "connected", port: 4002, endpoint: "IB Gateway (paper)", accountType: "paper" });
    expect(sessions).toEqual([paper]);
    sup.stop();
  });

  it("uses only the configured port when set", async () => {
    const c = new FakeConnector(new Map());
    const { sup } = setup(c, { port: 4111 });
    sup.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(c.attempts.map((a) => a.port)).toEqual([4111]);
    expect(sup.state().tws).toBe("disconnected");
    expect(sup.state().lastError).toMatch(/port 4111/);
    sup.stop();
  });

  it("steps the client id when it is in use", async () => {
    const live = new FakeSession(emptyMarket(), ["U7654321"]);
    const c = new FakeConnector(new Map([[4001, (id: number) => (id < 19 ? new TwsRequestError(CODE.CLIENT_ID_IN_USE, "client id is already in use") : live)]]));
    const { sup } = setup(c, { clientId: 17 });
    sup.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(c.attempts.map((a) => a.clientId)).toEqual([17, 18, 19]);
    expect(sup.state()).toMatchObject({ tws: "connected", accountType: "live", endpoint: "IB Gateway (live)" });
    sup.stop();
  });

  it("reconnects with backoff after a disconnect (nightly reset)", async () => {
    const s1 = new FakeSession(emptyMarket());
    const s2 = new FakeSession(emptyMarket());
    const queue = [s1, s2];
    const c = new FakeConnector(new Map([[4001, () => queue.shift() ?? new TwsRequestError(502, "refused")]]));
    const { sup, sessions } = setup(c);
    sup.start();
    await vi.advanceTimersByTimeAsync(0);
    s1.emitClose("socket closed");
    expect(sup.state().tws).toBe("disconnected");
    await vi.advanceTimersByTimeAsync(reconnectDelayMs(1));
    expect(sup.state().tws).toBe("connected");
    expect(sessions).toEqual([s1, null, s2]);
    sup.stop();
  });

  it("pauses (no retry spam) on a competing session and resumes after the pause", async () => {
    const s = new FakeSession(emptyMarket());
    const c = new FakeConnector(new Map([[4001, s]]));
    const { sup } = setup(c);
    sup.start();
    await vi.advanceTimersByTimeAsync(0);
    s.emitSystem({ code: CODE.COMPETING_SESSION, message: "No market data during competing live session" });
    expect(sup.state().paused).toBe(COMPETING_MESSAGE);
    expect(sup.state().paused).toBe("IBKR session in use elsewhere — market data paused");
    await vi.advanceTimersByTimeAsync(5 * 60_000);
    expect(sup.state().paused).toBeNull();
    s.emitSystem({ code: CODE.CONNECTIVITY_LOST, message: "Connectivity between IB and TWS has been lost." });
    expect(sup.state().paused).toBe(CONNECTIVITY_MESSAGE);
    s.emitSystem({ code: CODE.RESTORED_DATA_KEPT, message: "restored" });
    expect(sup.state().paused).toBeNull();
    expect(c.attempts).toHaveLength(1);
    sup.stop();
  });

  it("does nothing when the bridge is disabled", () => {
    const c = new FakeConnector(new Map());
    const { sup } = setup(c, { enabled: false });
    sup.start();
    expect(sup.state().tws).toBe("disabled");
    expect(c.attempts).toHaveLength(0);
  });
});

it("classifies accounts", () => {
  expect(accountType([])).toBeNull();
  expect(accountType(["DU123"])).toBe("paper");
  expect(accountType(["U123"])).toBe("live");
});
