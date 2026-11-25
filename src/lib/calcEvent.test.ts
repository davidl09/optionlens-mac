import { describe, expect, it } from "vitest";
import type { CalcEvent, CalcLeg } from "../api/types";
import { earningsNote, ivAfterEarnings, legIvNotes } from "./calcEvent";

const EVENT: CalcEvent = {
  earnings_date: "2026-12-02", reaction_date: "2026-12-03", implied_move_pct: 0.1278, base_iv: 0.4231, method: "post_event_pair",
  expiries_used: ["2026-12-04", "2026-12-24"], applied: true, reason: null,
};
const G = { delta: 0, gamma: 0, theta: 0, vega: 0 };
const leg = (o: Partial<CalcLeg>): CalcLeg => ({ kind: "option", right: "C", strike: 200, qty: 1, price: 1, iv: 0.62, mid: 1, iv_eval: 0.62, iv_expiry: 0.62, iv_base: null, base_floored: false, greeks: G, ...o });
const FRONT = leg({ expiry: "2026-12-04", qty: -1, iv: 0.917, iv_eval: 0.917, iv_expiry: null, iv_base: 0.4231 });
const BACK = leg({ expiry: "2026-12-24", iv_expiry: 0.418, iv_base: 0.418 });

describe("earnings note", () => {
  it("names the event, the implied move and the back month's IV after earnings", () => {
    expect(earningsNote(EVENT, [FRONT, BACK])).toBe("Earnings 2026-12-02 (implied move ±12.8%): IV crush modelled — back-month IV after earnings 42%");
    expect(ivAfterEarnings(EVENT, [BACK, FRONT])).toBe(0.418);
    expect(ivAfterEarnings(EVENT, [])).toBe(0.4231);                    // no crushed leg: the chain's ex-event IV
  });

  it("says when the crush is off or could not be estimated", () => {
    expect(earningsNote({ ...EVENT, applied: false, reason: "switched off (earnings_crush=false)" }, [])).toBe(
      "Earnings 2026-12-02 (implied move ±12.8%): IV crush off — every leg keeps its IV through the report");
    const none: CalcEvent = { ...EVENT, implied_move_pct: null, base_iv: null, method: null, expiries_used: null, applied: false, reason: "the term structure prices no earnings premium" };
    expect(earningsNote(none, [])).toBe("Earnings 2026-12-02: IV crush not modelled — the term structure prices no earnings premium");
    expect(earningsNote({ ...none, earnings_date: null }, [])).toContain("Earnings 2026-12-03:");
  });
});

describe("IVs used per leg", () => {
  it("lists the dates whose IV differs from the entered one (+ shift)", () => {
    expect(legIvNotes(BACK, 0, "2026-11-25", "2026-12-04")).toEqual(["at 2026-12-04: 41.8%"]);
    expect(legIvNotes(FRONT, 0, "2026-11-25", "2026-12-04")).toEqual([]);
    expect(legIvNotes({ ...BACK, iv_eval: 0.67, iv_expiry: 0.468 }, 0.05, "2026-11-25", "2026-12-04")).toEqual(["at 2026-12-04: 46.8%"]);
    expect(legIvNotes({ ...BACK, iv_eval: 0.7 }, 0, "2026-12-01", "2026-12-04")).toEqual(["at 2026-12-01: 70.0%", "at 2026-12-04: 41.8%"]);
    expect(legIvNotes({ ...BACK, iv_eval: 0.418 }, 0, "2026-12-04", "2026-12-04")).toEqual(["at 2026-12-04: 41.8%"]);   // one date, once
    expect(legIvNotes({ ...BACK, base_floored: true }, 0, "2026-11-25", "2026-12-04")).toEqual(["at 2026-12-04: 41.8%", "ex-earnings IV floored"]);
    expect(legIvNotes(leg({ kind: "stock", iv: null }), 0, "x", null)).toEqual([]);
  });
});
