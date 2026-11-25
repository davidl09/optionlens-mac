// Heatmap colour scales (pure): sequential (one hue, magnitude) or diverging (two hues around a neutral zero), the
// colour of a value on them and a readable ink for text printed on a cell.

export type ScaleKind = "sequential" | "diverging";

/** One heatmap cell: x / y index into the axis labels. */
export interface HeatCell {
  x: number;
  y: number;
  value: number | null;
  /** Greyed and hatched (e.g. not reliable): no colour, no printed value. */
  muted?: boolean;
  /** Outlined (e.g. the contract is in the calculator). */
  outlined?: boolean;
}

export interface HeatScale {
  kind: ScaleKind;
  min: number;
  max: number;
  /** Colours from min to max (#rrggbb), evenly spaced. */
  stops: string[];
}

/**
 * The scale over `values` (nulls ignored): sequential spans [min, max] of the values; diverging is symmetric around
 * zero (±max |value|), so zero always gets the middle stop. A degenerate span is widened so every value has a colour.
 */
export function makeScale(kind: ScaleKind, values: (number | null)[], stops: string[]): HeatScale {
  const vs = values.filter((v): v is number => v !== null && Number.isFinite(v));
  if (kind === "diverging") {
    const m = Math.max(1e-9, ...vs.map(Math.abs));
    return { kind, min: -m, max: m, stops };
  }
  const lo = vs.length ? Math.min(...vs) : 0;
  const hi = vs.length ? Math.max(...vs) : 1;
  return hi > lo ? { kind, min: lo, max: hi, stops } : { kind, min: lo - 0.5e-3, max: hi + 0.5e-3, stops };
}

function rgb(hex: string): [number, number, number] {
  const h = hex.replace("#", "");
  const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h.slice(0, 6);
  const n = Number.parseInt(full, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

const hex2 = (x: number) => Math.round(Math.min(255, Math.max(0, x))).toString(16).padStart(2, "0");

/** The colour of `v` on the scale (clamped to its ends), interpolated linearly in RGB between neighbouring stops. */
export function scaleColor(s: HeatScale, v: number): string {
  const t = Math.min(1, Math.max(0, (v - s.min) / (s.max - s.min)));
  const seg = t * (s.stops.length - 1);
  const i = Math.min(s.stops.length - 2, Math.floor(seg));
  const f = seg - i;
  const [a, b] = [rgb(s.stops[i]), rgb(s.stops[i + 1])];
  return `#${hex2(a[0] + (b[0] - a[0]) * f)}${hex2(a[1] + (b[1] - a[1]) * f)}${hex2(a[2] + (b[2] - a[2]) * f)}`;
}

function luminance(hex: string): number {
  const lin = (c: number) => { const x = c / 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; };
  const [r, g, b] = rgb(hex).map(lin);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** WCAG contrast ratio of two colours. */
export function contrast(a: string, b: string): number {
  const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
}

/** Of the two text inks, the one that reads better on `bg`. */
export function inkOn(bg: string, ink1: string, ink2: string): string {
  return contrast(bg, ink1) >= contrast(bg, ink2) ? ink1 : ink2;
}

/** CSS linear-gradient of the scale (legend bar). */
export function gradientCss(s: HeatScale): string {
  return `linear-gradient(90deg, ${s.stops.join(", ")})`;
}
