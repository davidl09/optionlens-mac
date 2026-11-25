import { describe, expect, it } from "vitest";
import { contrast, gradientCss, inkOn, makeScale, scaleColor } from "./heatScale";

const SEQ = ["#000000", "#808080", "#ffffff"];
const DIV = ["#ff0000", "#888888", "#00ff00"];

describe("heat scales", () => {
  it("sequential spans the values; nulls and NaN are ignored", () => {
    const s = makeScale("sequential", [0.2, null, 0.4, Number.NaN, 0.3], SEQ);
    expect([s.min, s.max]).toEqual([0.2, 0.4]);
    expect(scaleColor(s, 0.2)).toBe("#000000");
    expect(scaleColor(s, 0.3)).toBe("#808080");
    expect(scaleColor(s, 0.4)).toBe("#ffffff");
    expect(scaleColor(s, 0.25)).toBe("#404040");
    expect(scaleColor(s, 9)).toBe("#ffffff");                      // clamped
  });

  it("diverging is symmetric around zero: zero is the neutral middle", () => {
    const s = makeScale("diverging", [-0.01, 0.03], DIV);
    expect([s.min, s.max]).toEqual([-0.03, 0.03]);
    expect(scaleColor(s, 0)).toBe("#888888");
    expect(scaleColor(s, 0.03)).toBe("#00ff00");
    expect(scaleColor(s, -0.03)).toBe("#ff0000");
  });

  it("degenerate spans still colour every value", () => {
    const flat = makeScale("sequential", [0.25, 0.25], SEQ);
    expect(flat.max).toBeGreaterThan(flat.min);
    expect(scaleColor(flat, 0.25)).toBe("#808080");
    const zero = makeScale("diverging", [0, null], DIV);
    expect(scaleColor(zero, 0)).toBe("#888888");
    expect(makeScale("sequential", [], SEQ).max).toBeGreaterThan(0);
  });

  it("picks the ink that reads on the cell", () => {
    expect(contrast("#000000", "#ffffff")).toBeCloseTo(21, 0);
    expect(inkOn("#a9cdf7", "#e6e9ef", "#141922")).toBe("#141922");   // a light cell: dark ink
    expect(inkOn("#1a2f4f", "#e6e9ef", "#141922")).toBe("#e6e9ef");   // a dark cell: light ink
    expect(gradientCss(makeScale("diverging", [1], DIV))).toBe("linear-gradient(90deg, #ff0000, #888888, #00ff00)");
  });
});
