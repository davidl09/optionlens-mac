// Shared option builders so every chart reads as one system.
import type { ColumnFmt } from "../../api/types";
import { fmt } from "../../lib/format";
import type { Palette } from "./EChart";

/** Compact axis labels (fewer decimals than table cells). */
export function axisLabelFmt(v: number, kind: ColumnFmt): string {
  if (kind === "pct" || kind === "pct_signed") return `${+(v * 100).toFixed(1)}%`;
  if (kind === "vol") return `${+(v * 100).toFixed(1)}`;
  if (kind === "money") return `$${+v.toFixed(2)}`;
  if (kind === "int" || kind === "bigmoney") return Math.abs(v) >= 1000 ? fmt(v, "bigmoney") : String(+v.toFixed(2));
  return fmt(v, kind);
}

export function axisStyle(p: Palette, valueFmt?: ColumnFmt) {
  return {
    axisLine: { lineStyle: { color: p.axis } },
    axisTick: { show: false },
    axisLabel: {
      color: p.muted, fontSize: 11, hideOverlap: true,
      ...(valueFmt ? { formatter: (v: number) => axisLabelFmt(v, valueFmt) } : {}),
    },
    splitLine: { lineStyle: { color: p.grid } },
  };
}

export function tooltipStyle(p: Palette) {
  return {
    backgroundColor: p.tooltipBg, borderColor: p.axis, borderWidth: 1,
    textStyle: { color: p.fg, fontSize: 12 }, extraCssText: "box-shadow:0 4px 14px rgba(0,0,0,.25);",
  };
}

export function legendStyle(p: Palette) {
  return { top: 0, right: 0, icon: "roundRect", itemWidth: 12, itemHeight: 4, textStyle: { color: p.fg2, fontSize: 11 } };
}

export const GRID = { left: 8, right: 16, top: 30, bottom: 6, containLabel: true };
/** Heatmaps: a fixed plot area (cells line up with known positions; y labels up to ~10 characters). */
export const HEAT_GRID = { left: 72, right: 12, top: 8, bottom: 26, containLabel: false };

interface TooltipItem { axisValue?: unknown; axisValueLabel?: string; marker?: string; seriesName?: string; value?: unknown }

/** Axis-trigger tooltip that formats every series with one ColumnFmt. */
export function axisTooltip(p: Palette, valueFmt: ColumnFmt, headerFmt?: (x: unknown) => string) {
  return {
    trigger: "axis",
    ...tooltipStyle(p),
    axisPointer: { type: "line", lineStyle: { color: p.muted, type: "dashed" } },
    formatter: (raw: TooltipItem | TooltipItem[]) => {
      const items = Array.isArray(raw) ? raw : [raw];
      if (!items.length) return "";
      const head = headerFmt ? headerFmt(items[0].axisValue) : items[0].axisValueLabel ?? String(items[0].axisValue ?? "");
      const lines = items.map((it) => {
        const v = Array.isArray(it.value) ? it.value[it.value.length - 1] : it.value;
        return `${it.marker ?? ""}${it.seriesName ?? ""}<b style="float:right;margin-left:16px">${fmt(v, valueFmt)}</b>`;
      });
      return `<div style="min-width:150px"><div style="opacity:.7;margin-bottom:2px">${head}</div>${lines.join("<br/>")}</div>`;
    },
  };
}
