import { describe, expect, it } from "vitest";
import { FAILURE_WINDOW_MS, FailureLog, failureReason } from "./failures";

describe("FailureLog", () => {
  it("groups reasons across symbols and counts them over the last 24 h", () => {
    let t = 0;
    const log = new FailureLog(() => t);
    log.record("TWS error 200: No security definition has been found for DELL", "DELL");
    log.record("TWS error 200: No security definition has been found for XYZ", "XYZ");
    log.record("Timed out waiting for market data");
    expect(log.top()).toEqual([
      { reason: "TWS error 200: No security definition has been found for …", count: 2 },
      { reason: "Timed out waiting for market data", count: 1 },
    ]);
    t = FAILURE_WINDOW_MS + 1;
    expect(log.top()).toEqual([]);
  });

  it("keeps long reasons short", () => {
    expect(failureReason("x".repeat(300))).toHaveLength(158);
  });
});
