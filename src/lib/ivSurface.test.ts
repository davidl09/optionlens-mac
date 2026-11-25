import { describe, expect, it } from "vitest";
import type { IvSurface, SurfaceCell } from "../api/types";
import { cellText, isClickable, rowLabel, scaleKind, surfaceCells, tooltipHtml, valueText } from "./ivSurface";

const cell = (o: Partial<SurfaceCell> = {}): SurfaceCell => ({
  iv: 0.3, value: 0.3, osi: "AAPL261218P00340000", strike: 340, right: "P", bid: 1.2, ask: 1.3, oi: 1500, reliable: true, reason: null, ...o,
});

const surface = (o: Partial<IvSurface> = {}): IvSurface => ({
  symbol: "AAPL", spot: 348.5, asof: null, rows_by: "delta", mode: "iv", rows: [-0.25, 0.5],
  expiries: [{ expiry: "2026-12-18", dte: 29, atm_iv: 0.27, iv_computed: false, earnings_expiry: false },
    { expiry: "2027-02-19", dte: 92, atm_iv: 0.3, iv_computed: false, earnings_expiry: true }],
  cells: [[cell(), cell({ reliable: false, reason: "spread 60% of mid (> 40%)" })], [cell({ iv: null, value: null, osi: null, strike: null, right: null }), cell({ value: null })]],
  prior_date: null, earnings: { date: "2027-01-28", reaction_date: "2027-01-29" },
  thresholds: { max_spread_of_mid: 0.4, min_oi: 1, min_dte: 1, max_dte: 366 }, delayed: false, ...o,
});

describe("IV surface helpers", () => {
  it("colours IV on a sequential scale and the signed modes on a diverging one", () => {
    expect(scaleKind("iv")).toBe("sequential");
    expect(scaleKind("vs_atm")).toBe("diverging");
    expect(scaleKind("chg_1d")).toBe("diverging");
  });

  it("labels rows", () => {
    expect([-0.1, -0.45, 0.5, 0.25].map((v) => rowLabel("delta", v))).toEqual(["10Δ put", "45Δ put", "ATM", "25Δ call"]);
    expect([-0.2, -0.075, 0, 0.025].map((v) => rowLabel("moneyness", v))).toEqual(["−20%", "−7.5%", "0%", "+2.5%"]);
    expect(rowLabel("strike", 347.5)).toBe("347.5");
  });

  it("formats cell values in vol points", () => {
    expect(cellText("iv", 0.3214)).toBe("32.1");
    expect(cellText("vs_atm", 0.0123)).toBe("+1.2");
    expect(cellText("chg_1d", -0.004)).toBe("−0.4");
    expect(cellText("chg_1d", -0.0001)).toBe("0.0");
    expect(valueText("iv", 0.3214)).toBe("32.1%");
    expect(valueText("vs_atm", 0.0123)).toBe("+1.2 vol pts");
    expect(valueText("chg_1d", null)).toBe("n/a");
  });

  it("draws cells with an IV, greys unreliable or valueless ones, outlines held contracts", () => {
    const s = surface();
    const cells = surfaceCells(s, (c, exp) => c.strike === 340 && exp === "2026-12-18");
    expect(cells).toEqual([
      { x: 0, y: 0, value: 0.3, muted: false, outlined: true },
      { x: 1, y: 0, value: 0.3, muted: true, outlined: false },
      { x: 1, y: 1, value: null, muted: true, outlined: false },
    ]);
    expect(isClickable(s.cells[0][0])).toBe(true);
    expect(isClickable(s.cells[0][1])).toBe(false);
    expect(isClickable(s.cells[1][0])).toBe(false);
  });

  it("tooltips name the expiry, row, contract, quote and why a cell is greyed (escaped)", () => {
    const s = surface({ mode: "vs_atm" });
    const t = tooltipHtml(s, 1, 0, "Feb 19 ’27");
    expect(t).toContain("Feb 19 ’27</b> (92 DTE) · 25Δ put");
    expect(t).toContain("First expiry after earnings (2027-01-28)");
    expect(t).toContain("Contract 340 put");
    expect(t).toContain("Bid / ask 1.20 / 1.30 · OI 1,500");
    expect(t).toContain("Unreliable: spread 60% of mid (&gt; 40%)");
    expect(t).not.toContain("Click to add");
    expect(tooltipHtml(s, 0, 0, "Dec 18")).toContain("Click to add to the calculator");
    expect(tooltipHtml(s, 0, 0, "Dec 18")).toContain("vs ATM <b>+30.0 vol pts</b>");
  });
});
