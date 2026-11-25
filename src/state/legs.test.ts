import { describe, expect, it } from "vitest";
import type { Leg } from "../api/types";
import { contractKey, heldQty } from "./legs";

const opt = (right: "C" | "P", strike: number, qty: number, expiry = "2026-12-18"): Leg => ({ kind: "option", right, strike, expiry, qty });

describe("heldQty", () => {
  it("nets the legs per contract; stock and contracts netting to zero are left out", () => {
    const held = heldQty([opt("C", 230, 1), opt("C", 230, 1), opt("P", 220, -1), opt("C", 240, 1), opt("C", 240, -1),
      { kind: "stock", qty: 100 }, opt("C", 230, -1, "2027-01-15")]);
    expect(held.get(contractKey("2026-12-18", 230, "C"))).toBe(2);
    expect(held.get(contractKey("2026-12-18", 220, "P"))).toBe(-1);
    expect(held.get(contractKey("2027-01-15", 230, "C"))).toBe(-1);
    expect(held.has(contractKey("2026-12-18", 240, "C"))).toBe(false);
    expect(held.size).toBe(3);
  });
});
