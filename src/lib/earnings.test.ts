import { describe, expect, it } from "vitest";
import { isDataSource, sourceLabel } from "../components/ui";
import { nextEarningsSub, noNextEarningsText } from "./earnings";

describe("earnings wording", () => {
  it("says when and how the next report comes", () => {
    expect(nextEarningsSub({ timing: "bmo", days: 5 })).toBe("BMO · in 5d");
    expect(nextEarningsSub({ timing: "unknown", days: 0 })).toBe("time n/a · in 0d");
  });
  it("passes the engine's reason for a missing date through", () => {
    const why = "No upcoming earnings date for RY.TO: TMX Money and Yahoo Finance were unavailable (…) (checked 2026-11-26).";
    expect(noNextEarningsText("RY.TO", why)).toBe(why);
    expect(noNextEarningsText("AAPL", null)).toBe("No upcoming earnings date for AAPL.");
  });
  it("badges the Canadian earnings sources", () => {
    expect(isDataSource("tmx") && isDataSource("yahoo")).toBe(true);
    expect([sourceLabel("tmx"), sourceLabel("yahoo"), sourceLabel("nasdaq")]).toEqual(["TMX Money", "Yahoo Finance", "Nasdaq"]);
  });
});
