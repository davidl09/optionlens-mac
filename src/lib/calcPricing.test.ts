import { describe, expect, it } from "vitest";
import type { Leg } from "../api/types";
import type { LiveFields } from "../live/store";
import { calcRequest, extent, isPinned, keepPriceRange, liveMid, type MarketInputs, priceLeg, priceLegs, stableRange, structureKey, unpinned } from "./calcPricing";

const CALL: Leg = { kind: "option", right: "C", strike: 230, expiry: "2026-11-20", qty: 1 };
const OSI = "AAPL261120C00230000";

function market(live: Record<string, LiveFields> = {}, liveSpot: number | null = null): MarketInputs {
  return {
    stored: (l) => (l.strike === 230 ? { osi: OSI, mid: 5.2, iv: 0.27 } : null),
    live: (osi) => live[osi] ?? null,
    liveSpot, storedSpot: 231.4,
  };
}

describe("effective price rule: user ?? live ?? stored", () => {
  it("uses the stored mid / IV without live data", () => {
    const p = priceLeg(CALL, market());
    expect(p.leg).toMatchObject({ price: 5.2, iv: 0.27 });
    expect([p.priceFrom, p.ivFrom, p.osi, p.liveTs]).toEqual(["stored", "stored", OSI, null]);
  });

  it("follows the live mid (bid/ask midpoint) and IV of unedited legs", () => {
    const p = priceLeg(CALL, market({ [OSI]: { bid: 6, ask: 6.4, iv: 0.3, ts: "2026-11-20T15:00:00Z" } }));
    expect(p.leg).toMatchObject({ price: 6.2, iv: 0.3 });
    expect([p.priceFrom, p.ivFrom, p.liveTs]).toEqual(["live", "live", "2026-11-20T15:00:00Z"]);
  });

  it("marks live values that are delayed (only when a live value is used)", () => {
    const delayed = { [OSI]: { bid: 6, ask: 6.4, iv: 0.3, delayed: true } };
    expect(priceLeg(CALL, market(delayed)).liveDelayed).toBe(true);
    expect(priceLeg(CALL, market({ [OSI]: { bid: 6, ask: 6.4 } })).liveDelayed).toBe(false);
    expect(priceLeg({ ...CALL, price: 4, iv: 0.2 }, market(delayed)).liveDelayed).toBe(false);   // all yours: none used
  });

  it("keeps the user's pinned price / IV over live data (each on its own)", () => {
    const live = market({ [OSI]: { bid: 6, ask: 6.4, iv: 0.3 } });
    const price = priceLeg({ ...CALL, price: 4.5 }, live);
    expect(price.leg).toMatchObject({ price: 4.5, iv: 0.3 });
    expect([price.priceFrom, price.ivFrom]).toEqual(["user", "live"]);
    const iv = priceLeg({ ...CALL, iv: 0.5 }, live);
    expect(iv.leg).toMatchObject({ price: 6.2, iv: 0.5 });
    expect([iv.priceFrom, iv.ivFrom]).toEqual(["live", "user"]);
  });

  it("a one-sided live quote is no mid: falls back to the stored mid", () => {
    expect(liveMid({ bid: 6 })).toBeNull();
    expect(liveMid({ bid: 0, ask: 6 })).toBeNull();
    expect(liveMid({ mid: 6.1, bid: 6, ask: 6.4 })).toBe(6.1);
    expect(priceLeg(CALL, market({ [OSI]: { bid: 6 } })).leg.price).toBe(5.2);
  });

  it("stock legs: pinned ?? live spot ?? stored spot, no IV", () => {
    const stock: Leg = { kind: "stock", qty: 100 };
    expect(priceLeg(stock, market()).leg).toMatchObject({ price: 231.4, iv: null });
    expect(priceLeg(stock, market({}, 240)).leg.price).toBe(240);
    expect(priceLeg(stock, market({}, 240)).priceFrom).toBe("live");
    expect(priceLeg({ ...stock, price: 200 }, market({}, 240)).priceFrom).toBe("user");
  });

  it("an unlisted leg sends no price (the engine reports it)", () => {
    const p = priceLeg({ ...CALL, strike: 999 }, market());
    expect(p.leg.price).toBeNull();
    expect(p.priceFrom).toBeNull();
  });
});

describe("pin / reset", () => {
  it("a typed price or IV pins the leg; reset drops both", () => {
    expect(isPinned(CALL)).toBe(false);
    expect(isPinned({ ...CALL, price: 0 })).toBe(true);
    expect(isPinned({ ...CALL, iv: 0.2 })).toBe(true);
    const reset = unpinned({ ...CALL, price: 4.5, iv: 0.5 });
    expect(isPinned(reset)).toBe(false);
    expect(priceLeg(reset, market({ [OSI]: { bid: 6, ask: 6.4 } })).leg.price).toBe(6.2);   // follows live again
  });

  it("a live tick moves unedited legs only", () => {
    const legs: Leg[] = [{ ...CALL, price: 4.5 }, { ...CALL, qty: -1 }];
    const before = priceLegs(legs, market({ [OSI]: { bid: 6, ask: 6.4 } }));
    const after = priceLegs(legs, market({ [OSI]: { bid: 7, ask: 7.4 } }));
    expect(before.map((p) => p.leg.price)).toEqual([4.5, 6.2]);
    expect(after.map((p) => p.leg.price)).toEqual([4.5, 7.2]);
  });

  it("the request carries explicit prices, IVs and spot", () => {
    const req = calcRequest("AAPL", priceLegs([CALL, { kind: "stock", qty: 100 }], market({}, 240)), { spot: 240, ivShift: 0, evalDate: "2026-11-20", priceRange: [200, 260] });
    expect(req).toEqual({
      symbol: "AAPL", spot: 240, iv_shift: 0, eval_date: "2026-11-20", price_range: [200, 260],
      legs: [{ ...CALL, price: 5.2, iv: 0.27 }, { kind: "stock", qty: 100, price: 240, iv: null }],
    });
    expect(calcRequest("AAPL", [], { spot: 1, ivShift: 0, evalDate: "x", priceRange: null })).not.toHaveProperty("price_range");
    expect(calcRequest("AAPL", [], { spot: 1, ivShift: 0, evalDate: "x", priceRange: null })).not.toHaveProperty("earnings_crush");
    expect(calcRequest("AAPL", [], { spot: 1, ivShift: 0, evalDate: "x", priceRange: null, earningsCrush: false }).earnings_crush).toBe(false);
  });

  it("the structure key ignores prices", () => {
    expect(structureKey([{ ...CALL, price: 1 }])).toBe(structureKey([{ ...CALL, price: 2, iv: 0.4 }]));
    expect(structureKey([CALL])).not.toBe(structureKey([{ ...CALL, qty: 2 }]));
  });
});

describe("stable axes", () => {
  it("keeps the y range while the curve stays inside it; rebuilds it with 10% headroom when it leaves", () => {
    const r = stableRange(null, [-100, 100]);
    expect(r).toEqual([-120, 120]);
    expect(stableRange(r, [-110, 115])).toBe(r);           // a live tick: no rescale
    expect(stableRange(r, [-100, 130])).toEqual([-130, 160]);
    expect(stableRange(null, [50, 50])).toEqual([45, 55]);  // a flat curve still gets a span
  });

  it("keeps the pinned x range while the spot stays in its inner 80%", () => {
    expect(keepPriceRange(null, 230)).toBeNull();
    expect(keepPriceRange([200, 300], 230)).toEqual([200, 300]);
    expect(keepPriceRange([200, 300], 289)).toEqual([200, 300]);
    expect(keepPriceRange([200, 300], 291)).toBeNull();
    expect(keepPriceRange([200, 300], 205)).toBeNull();
  });

  it("extent spans every series", () => {
    expect(extent([1, 5], [-3, 2])).toEqual([-3, 5]);
    expect(extent([])).toBeNull();
  });
});
