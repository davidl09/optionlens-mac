// IV surface panel (Skew page): what each mode colours by, row labels, cell text and tooltips (pure).
import type { IvSurface, SurfaceCell, SurfaceMode, SurfaceRows } from "../api/types";
import { fmt } from "./format";
import type { HeatCell, ScaleKind } from "./heatScale";

export const ROW_OPTIONS: { value: SurfaceRows; label: string }[] = [
  { value: "delta", label: "Delta" }, { value: "moneyness", label: "Moneyness" }, { value: "strike", label: "Strike" },
];
export const MODE_OPTIONS: { value: SurfaceMode; label: string }[] = [
  { value: "iv", label: "IV" }, { value: "vs_atm", label: "vs ATM" }, { value: "chg_1d", label: "Change 1d" },
];

/** IV is a magnitude (one hue); vs ATM and the 1-day change have a sign (two hues around zero). */
export function scaleKind(mode: SurfaceMode): ScaleKind {
  return mode === "iv" ? "sequential" : "diverging";
}

const pct = (v: number) => String(+(v * 100).toFixed(1));

/** "25Δ put" / "ATM" / "10Δ call"; "−7.5%" / "0%" / "+5%"; "350". */
export function rowLabel(rows: SurfaceRows, v: number): string {
  if (rows === "delta") return v === 0.5 ? "ATM" : `${Math.round(Math.abs(v) * 100)}Δ ${v < 0 ? "put" : "call"}`;
  if (rows === "moneyness") return v === 0 ? "0%" : `${v > 0 ? "+" : "−"}${pct(Math.abs(v))}%`;
  return String(+v.toFixed(2));
}

/** A cell's printed value in vol points: "32.1" (IV), "+1.2" / "−0.4" (signed modes). */
export function cellText(mode: SurfaceMode, v: number): string {
  const pts = (v * 100).toFixed(1);
  if (mode === "iv") return pts;
  if (pts === "0.0" || pts === "-0.0") return "0.0";
  return v > 0 ? `+${pts}` : `−${pts.slice(1)}`;
}

/** The mode's value in a tooltip: "32.1%" (IV) or "+1.2 vol pts". */
export function valueText(mode: SurfaceMode, v: number | null): string {
  if (v === null) return "n/a";
  return mode === "iv" ? `${pct(v)}%` : `${cellText(mode, v)} vol pts`;
}

/** A cell the user can add to the calculator: reliable, with a real contract. */
export function isClickable(c: SurfaceCell): boolean {
  return c.reliable && c.osi !== null && c.strike !== null && c.right !== null;
}

/**
 * The heatmap cells of the surface (x = expiry column, y = row): cells with an IV; greyed (muted) when not reliable
 * or without a value (e.g. no prior day for the 1-day change); outlined when `held(cell, expiry)`.
 */
export function surfaceCells(s: IvSurface, held: (c: SurfaceCell, expiry: string) => boolean): HeatCell[] {
  const out: HeatCell[] = [];
  s.cells.forEach((row, y) => row.forEach((c, x) => {
    if (c.iv === null) return;
    out.push({ x, y, value: c.value, muted: !c.reliable || c.value === null, outlined: held(c, s.expiries[x].expiry) });
  }));
  return out;
}

const esc = (t: string) => t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

/** Tooltip HTML of cell (x, y): expiry (DTE), row, the nearest contract, IV, value, bid / ask, OI, why greyed. */
export function tooltipHtml(s: IvSurface, x: number, y: number, expiryLabel: string): string {
  const c = s.cells[y][x];
  const e = s.expiries[x];
  const lines = [`<b>${esc(expiryLabel)}</b> (${e.dte} DTE) · ${esc(rowLabel(s.rows_by, s.rows[y]))}`];
  if (e.earnings_expiry && s.earnings) lines.push(`<span style="opacity:.8">First expiry after earnings (${esc(s.earnings.date)})</span>`);
  if (c.strike !== null && c.right !== null) lines.push(`Contract ${esc(String(c.strike))} ${c.right === "C" ? "call" : "put"}`);
  lines.push(`IV <b>${valueText("iv", c.iv)}</b>${s.mode === "iv" ? "" : ` · ${s.mode === "vs_atm" ? "vs ATM" : "1d change"} <b>${valueText(s.mode, c.value)}</b>`}`);
  if (c.osi !== null) lines.push(`Bid / ask ${fmt(c.bid, "num2")} / ${fmt(c.ask, "num2")} · OI ${fmt(c.oi, "int")}`);
  if (!c.reliable && c.reason) lines.push(`<span style="color:var(--warn)">Unreliable: ${esc(c.reason)}</span>`);
  else if (c.value === null && s.mode === "chg_1d") lines.push(`<span style="opacity:.8">No prior-day IV for this cell</span>`);
  if (isClickable(c)) lines.push(`<span style="opacity:.7">Click to add to the calculator</span>`);
  return lines.join("<br/>");
}
