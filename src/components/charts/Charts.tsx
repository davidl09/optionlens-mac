// Line / bar / heatmap / smile charts built on the EChart wrapper.
import { useMemo } from "react";
import type { ColumnFmt } from "../../api/types";
import { fmt } from "../../lib/format";
import { EChart, Palette, usePalette } from "./EChart";
import { axisStyle, axisTooltip, GRID, legendStyle, tooltipStyle } from "./common";
import { Empty } from "../ui";

export type XY = [string | number, number | null];

export interface LineSeries {
  name: string;
  data: XY[];
  color?: string;
  dashed?: boolean;
  area?: boolean;
  step?: boolean;
}

export interface RefLine { x?: string | number; y?: number; label?: string; color?: string }

/** yyyy-mm-dd of a local-midnight timestamp (toISOString would shift the day in UTC+ zones). */
const localDate = (x: unknown) => {
  const d = new Date(Number(x));
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};

function markLines(p: Palette, refs: RefLine[] | undefined) {
  if (!refs?.length) return undefined;
  return {
    symbol: "none", silent: true, animation: false,
    data: refs.map((r) => ({
      ...(r.x !== undefined ? { xAxis: r.x } : { yAxis: r.y }),
      lineStyle: { color: r.color ?? p.muted, type: "dashed", width: 1 },
      label: { show: Boolean(r.label), formatter: r.label ?? "", color: r.color ?? p.fg2, fontSize: 10, position: r.x !== undefined ? "insideEndTop" : "insideEndTop" },
    })),
  };
}

export interface LineChartProps {
  series: LineSeries[];
  xType?: "time" | "category" | "value";
  yFmt?: ColumnFmt;
  xFmt?: ColumnFmt;
  height?: number;
  refLines?: RefLine[];
  zoom?: boolean;
  ariaLabel?: string;
  yZero?: boolean;
}

export function LineChart({ series, xType = "time", yFmt = "num", xFmt, height = 260, refLines, zoom = false, ariaLabel, yZero = false }: LineChartProps) {
  const p = usePalette();
  const option = useMemo(() => ({
    animation: false,
    color: p.series,
    grid: { ...GRID, top: series.length > 1 ? 30 : 14, bottom: zoom ? 30 : 6 },
    legend: series.length > 1 ? { ...legendStyle(p), data: series.map((s) => s.name) } : undefined,
    tooltip: axisTooltip(p, yFmt, xFmt ? (x) => fmt(x, xFmt) : xType === "time" ? localDate : undefined),
    xAxis: { type: xType, scale: xType === "value", ...axisStyle(p, xFmt), splitLine: { show: false } },
    yAxis: { type: "value", scale: !yZero, ...axisStyle(p, yFmt) },
    dataZoom: zoom ? [{ type: "inside" }, { type: "slider", height: 16, bottom: 4, borderColor: p.axis, textStyle: { color: p.muted } }] : undefined,
    series: series.map((s, i) => ({
      name: s.name, type: "line", data: s.data, showSymbol: false, connectNulls: false, step: s.step ? "middle" : undefined,
      lineStyle: { width: 2, type: s.dashed ? "dashed" : "solid", ...(s.color ? { color: s.color } : {}) },
      itemStyle: s.color ? { color: s.color } : undefined,
      areaStyle: s.area ? { opacity: 0.12 } : undefined,
      markLine: i === 0 ? markLines(p, refLines) : undefined,
    })),
  }), [p, series, xType, yFmt, xFmt, refLines, zoom, yZero]);
  const points = Math.max(0, ...series.map((s) => s.data.filter((d) => d[1] !== null && d[1] !== undefined).length));
  if (points < 2) return <Empty>Not enough collected history to chart yet ({points} {points === 1 ? "point" : "points"}).</Empty>;
  return <EChart option={option} height={height} ariaLabel={ariaLabel} />;
}

export interface BarSeries { name: string; data: (number | null)[]; color?: string; stack?: string }

export interface BarChartProps {
  categories: (string | number)[];
  series: BarSeries[];
  yFmt?: ColumnFmt;
  height?: number;
  horizontal?: boolean;
  /** Colour each bar of a single series by its sign. */
  signColors?: boolean;
  onClickCategory?: (category: string) => void;
  refLines?: RefLine[];
  ariaLabel?: string;
}

export function BarChart({ categories, series, yFmt = "num", height = 260, horizontal = false, signColors = false, onClickCategory, refLines, ariaLabel }: BarChartProps) {
  const p = usePalette();
  const option = useMemo(() => {
    const cat = { type: "category", data: categories, ...axisStyle(p), splitLine: { show: false } };
    const val = { type: "value", ...axisStyle(p, yFmt) };
    return {
      animation: false,
      color: p.series,
      grid: { ...GRID, top: series.length > 1 ? 30 : 14 },
      legend: series.length > 1 ? { ...legendStyle(p), data: series.map((s) => s.name) } : undefined,
      tooltip: { ...axisTooltip(p, yFmt), axisPointer: { type: "shadow" } },
      xAxis: horizontal ? val : cat,
      yAxis: horizontal ? { ...cat, inverse: true } : val,
      series: series.map((s, i) => ({
        name: s.name, type: "bar", stack: s.stack, barMaxWidth: 22, data: signColors
          ? s.data.map((v) => ({ value: v, itemStyle: { color: (v ?? 0) >= 0 ? p.up : p.down } }))
          : s.data,
        itemStyle: { borderRadius: horizontal ? [0, 3, 3, 0] : [3, 3, 0, 0], ...(s.color ? { color: s.color } : {}) },
        markLine: i === 0 ? markLines(p, refLines) : undefined,
      })),
    };
  }, [p, categories, series, yFmt, horizontal, signColors, refLines]);
  return <EChart option={option} height={height} ariaLabel={ariaLabel}
    onClick={onClickCategory ? (e) => { if (e.name !== undefined) onClickCategory(String(e.name)); } : undefined} />;
}

export interface HeatmapProps {
  xLabels: string[];
  yLabels: string[];
  /** [xIndex, yIndex, value] */
  data: [number, number, number | null][];
  valueFmt?: ColumnFmt;
  height?: number;
  ariaLabel?: string;
}

/** Diverging heatmap centred at zero, with values printed in cells. */
export function Heatmap({ xLabels, yLabels, data, valueFmt = "pct_signed", height = 320, ariaLabel }: HeatmapProps) {
  const p = usePalette();
  const option = useMemo(() => {
    const max = Math.max(1e-9, ...data.map((d) => Math.abs(d[2] ?? 0)));
    const neutral = getComputedStyle(document.documentElement).getPropertyValue("--heat-mid").trim() || "#383835";
    return {
      animation: false,
      grid: { ...GRID, top: 6, bottom: 40 },
      tooltip: { ...tooltipStyle(p), formatter: (it: { value: [number, number, number | null] }) => `${yLabels[it.value[1]]} ${xLabels[it.value[0]]}: <b>${fmt(it.value[2], valueFmt)}</b>` },
      xAxis: { type: "category", data: xLabels, ...axisStyle(p), splitArea: { show: false }, splitLine: { show: false } },
      yAxis: { type: "category", data: yLabels, ...axisStyle(p), splitLine: { show: false } },
      visualMap: { min: -max, max, calculable: false, orient: "horizontal", left: "center", bottom: 0, itemHeight: 120, itemWidth: 10,
        inRange: { color: [p.down, neutral, p.up] }, textStyle: { color: p.muted }, formatter: (v: number) => fmt(v, valueFmt) },
      series: [{
        type: "heatmap", data: data.map((d) => [d[0], d[1], d[2] ?? "-"]),
        label: { show: true, color: p.fg, fontSize: 10, formatter: (it: { value: [number, number, number | string] }) => (typeof it.value[2] === "number" ? fmt(it.value[2], valueFmt).replace("%", "") : "") },
        itemStyle: { borderColor: p.panel, borderWidth: 2, borderRadius: 2 },
        emphasis: { itemStyle: { borderColor: p.fg, borderWidth: 1 } },
      }],
    };
  }, [p, xLabels, yLabels, data, valueFmt]);
  return <EChart option={option} height={height} ariaLabel={ariaLabel} />;
}

export interface SmileProps {
  /** x = strike (money) or call-equivalent delta (num2). */
  xFmt: ColumnFmt;
  series: LineSeries[];
  spot?: number | null;
  height?: number;
}

/** IV smile: IV (vol) against strike or delta, with optional spot marker. */
export function Smile({ xFmt, series, spot, height = 300 }: SmileProps) {
  const p = usePalette();
  const refs = useMemo<RefLine[] | undefined>(() => (spot ? [{ x: spot, label: "spot", color: p.accent }] : undefined), [spot, p.accent]);
  const withPoints = useMemo(() => series.map((s) => ({ ...s, data: s.data.filter((d) => d[1] !== null) })), [series]);
  return <LineChart series={withPoints} xType="value" xFmt={xFmt} yFmt="vol" height={height} refLines={refs} ariaLabel="Implied volatility smile" />;
}
