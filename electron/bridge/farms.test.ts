import { describe, expect, it } from "vitest";
import { ContractServer, FarmMonitor, parseFarmMessage, SECDEF_PROBE_MS, type FarmState } from "./farms";

describe("parseFarmMessage", () => {
  it("maps IBKR's farm codes to farm and state, the farm named by the text where it says", () => {
    expect(parseFarmMessage({ code: 2158, message: "Sec-def data farm connection is OK:secdefil" })).toEqual({ farm: "secDef", state: "ok", server: "secdefil" });
    expect(parseFarmMessage({ code: 2157, message: "Sec-def data farm connection is broken:secdefnj" })).toEqual({ farm: "secDef", state: "down", server: "secdefnj" });
    expect(parseFarmMessage({ code: 2104, message: "Market data farm connection is OK:usfarm.nj" })).toEqual({ farm: "marketData", state: "ok", server: "usfarm.nj" });
    expect(parseFarmMessage({ code: 2103, message: "Market data farm connection is broken:usopt" })).toMatchObject({ farm: "marketData", state: "down" });
    expect(parseFarmMessage({ code: 2105, message: "HMDS data farm connection is broken:ushmds" })).toMatchObject({ farm: "historical", state: "down" });
    expect(parseFarmMessage({ code: 2106, message: "HMDS data farm connection is OK:ushmds" })).toMatchObject({ farm: "historical", state: "ok" });
    expect(parseFarmMessage({ code: 2107, message: "HMDS data farm connection is inactive but should be available upon demand.ushmds" })).toMatchObject({ farm: "historical", state: "inactive" });
    expect(parseFarmMessage({ code: 2108, message: "Market data farm connection is inactive but should be available upon demand.usfarm" })).toMatchObject({ farm: "marketData", state: "inactive" });
    // 2119 is documented as "Market data farm is connecting"; some Gateways send it for the sec-def farm.
    expect(parseFarmMessage({ code: 2119, message: "Market data farm is connecting:usfarm" })).toMatchObject({ farm: "marketData", state: "connecting" });
    expect(parseFarmMessage({ code: 2119, message: "Sec-def data farm connection is connecting:secdefil" })).toMatchObject({ farm: "secDef", state: "connecting" });
    expect(parseFarmMessage({ code: 1100, message: "Connectivity between IB and TWS has been lost" })).toBeNull();
    expect(parseFarmMessage({ code: 2100, message: "API client has been unsubscribed from account data." })).toBeNull();
  });
});

describe("FarmMonitor", () => {
  it("a farm is as bad as its worst server, since that server's change; reports only real changes", () => {
    let now = 1000;
    const f = new FarmMonitor(() => now);
    expect(f.status("marketData").state).toBe("unknown");
    expect(f.apply({ code: 2104, message: "Market data farm connection is OK:usfarm" })).toBe("marketData");
    now = 2000;
    expect(f.apply({ code: 2103, message: "Market data farm connection is broken:usopt" })).toBe("marketData");
    now = 3000;
    expect(f.apply({ code: 2104, message: "Market data farm connection is OK:cashfarm" })).toBeNull();
    expect(f.status("marketData")).toEqual({ state: "down", since: 2000, server: "usopt" });
    expect(f.apply({ code: 2104, message: "Market data farm connection is OK:usopt" })).toBe("marketData");
    expect(f.snapshot().marketData).toMatchObject({ state: "ok" });
    expect(f.snapshot().secDef).toEqual({ state: "unknown", since: null, server: null });
    f.reset();
    expect(f.status("marketData").state).toBe("unknown");
  });
});

describe("ContractServer", () => {
  it("is unavailable while the farm is down (until it is OK), and for 15 min after an unanswered request (then one probe)", () => {
    let farm: FarmState = "unknown";
    let now = 0;
    const c = new ContractServer(() => farm, () => now);
    expect(c.available()).toBe(true);
    farm = "down";
    expect(c.readyAt()).toBe(Infinity);
    farm = "ok";
    expect(c.available()).toBe(true);
    c.unanswered();
    expect(c.available()).toBe(false);
    expect(c.unansweredSince()).toBe(0);
    now = SECDEF_PROBE_MS;
    expect(c.available()).toBe(true);               // the probe
    expect(c.unansweredSince()).toBeNull();
    c.unanswered();
    expect(c.readyAt()).toBe(2 * SECDEF_PROBE_MS);
    c.answered();
    expect(c.available()).toBe(true);
  });
});
