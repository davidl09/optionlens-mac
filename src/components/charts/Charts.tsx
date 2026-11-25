// Line / bar / heatmap / smile charts built on the EChart wrapper.
import { ReactNode, useMemo } from "react";
import type { ColumnFmt } from "../../api/types";
import { fmt } from "../../lib/format";
import { EChart, Palette, usePalette } from "./EChart";
import { axisStyle, axisTooltip, GRID, HEAT_GRID, legendStyle, tooltipStyle } from "./common";
import { gradientCss, type HeatCell, inkOn, makeScale, scaleColor, type ScaleKind } from "../../lib/heatScale";

export type { HeatCell };
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

const hasPoint = (s: LineSeries) => s.data.some((d) => d[1] !== null && d[1] !== undefined);

export function LineChart({ series: all, xType = "time", yFmt = "num", xFmt, height = 260, refLines, zoom = false, ariaLabel, yZero = false }: LineChartProps) {
  const p = usePalette();
  // Only series with data are drawn and get a legend entry; the others are named under the chart.
  const series = useMemo(() => all.filter(hasPoint), [all]);
  const missing = all.filter((s) => !hasPoint(s)).map((s) => s.name);
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
  return (
    <>
      <EChart option={option} height={height} ariaLabel={ariaLabel} />
      {missing.length > 0 && <div className="chart-note muted small">No data yet: {missing.join(", ")}.</div>}
    </>
  );
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

/** A badge drawn before an x-axis label (e.g. "E" on the earnings expiry). */
export interface XMark { index: number; badge: string }

export interface HeatmapProps {
  xLabels: string[];
  yLabels: string[];
  /** The cells drawn (x / y index into the labels). */
  cells: HeatCell[];
  /** Diverging (default): two hues around a neutral zero; sequential: one hue from the lowest to the highest value. */
  scale?: ScaleKind;
  /** Legend and default cell text / tooltip format. */
  valueFmt?: ColumnFmt;
  /** Text printed in a cell when it fits (default: the value in valueFmt without "%"). */
  cellText?: (v: number) => string;
  /** Tooltip HTML of a cell (default: "y x: value"). */
  tooltip?: (cell: HeatCell) => string;
  onCellClick?: (cell: HeatCell) => void;
  xMarks?: XMark[];
  /** Extra legend items after the colour bar. */
  legend?: ReactNode;
  height?: number;
  ariaLabel?: string;
}

const CELL_FONT = 10;

/** Diagonal hatch lines (45°, 6 px apart) clipped to a rect. */
function hatch(x: number, y: number, w: number, h: number, color: string) {
  const out = [];
  for (let d = -h + 3; d < w; d += 6) {
    const t0 = Math.max(0, -d), t1 = Math.min(h, w - d);
    if (t1 > t0) out.push({ type: "line", silent: true, shape: { x1: x + d + t0, y1: y + h - t0, x2: x + d + t1, y2: y + h - t1 }, style: { stroke: color, lineWidth: 1, opacity: 0.55 } });
  }
  return out;
}

/** Heatmap of cells on category axes, coloured on a sequential or diverging scale, values printed where they fit. */
export function Heatmap({ xLabels, yLabels, cells, scale: kind = "diverging", valueFmt = "pct_signed", cellText, tooltip, onCellClick, xMarks, legend, height = 320, ariaLabel }: HeatmapProps) {
  const p = usePalette();
  const scale = useMemo(() => makeScale(kind, cells.filter((c) => !c.muted).map((c) => c.value),
    kind === "diverging" ? [p.down, p.heatMid, p.up] : p.heatSeq), [kind, cells, p]);
  const option = useMemo(() => {
    const text = cellText ?? ((v: number) => fmt(v, valueFmt).replace("%", ""));
    const marks = new Map((xMarks ?? []).map((m) => [m.index, m.badge]));
    const label = (v: string, i: number) => (marks.has(i) ? `{badge|${marks.get(i)}} ${v}` : v);
    return {
      animation: false,
      grid: HEAT_GRID,
      tooltip: {
        ...tooltipStyle(p), trigger: "item",
        formatter: (it: { value: [number, number, number] }) => {
          const c = cells[it.value[2]];
          return tooltip ? tooltip(c) : `${yLabels[c.y]} ${xLabels[c.x]}: <b>${fmt(c.value, valueFmt)}</b>`;
        },
      },
      xAxis: { type: "category", data: xLabels, ...axisStyle(p), splitLine: { show: false },
        axisLabel: { ...axisStyle(p).axisLabel, formatter: label, rich: { badge: { backgroundColor: p.accent, color: "#fff", borderRadius: 3, padding: [1, 3, 1, 3], fontSize: 10, fontWeight: "bold" } } } },
      yAxis: { type: "category", data: yLabels, ...axisStyle(p), splitLine: { show: false } },
      series: [{
        type: "custom", data: cells.map((c, i) => [c.x, c.y, i]), encode: { x: 0, y: 1, tooltip: 2 },
        cursor: onCellClick ? "pointer" : "default",
        renderItem: (_: unknown, api: { value: (d: number) => number; coord: (v: number[]) => number[]; size: (v: number[]) => number[] }) => {
          const c = cells[api.value(2)];
          const [cx, cy] = api.coord([api.value(0), api.value(1)]);
          const [bw, bh] = api.size([1, 1]);
          const x = cx - bw / 2 + 1, y = cy - bh / 2 + 1, w = bw - 2, h = bh - 2;
          const fill = c.muted || c.value === null ? p.heatNa : scaleColor(scale, c.value);
          const children: object[] = [{ type: "rect", shape: { x, y, width: w, height: h, r: 2 },
            style: { fill, stroke: c.outlined ? p.fg : "none", lineWidth: c.outlined ? 2 : 0 } }];
          if (c.muted) children.push(...hatch(x, y, w, h, p.muted));
          else if (c.value !== null) {
            const t = text(c.value);
            if (h >= CELL_FONT + 3 && t.length * CELL_FONT * 0.6 + 4 <= w) {
              children.push({ type: "text", silent: true, style: { text: t, x: cx, y: cy, align: "center", verticalAlign: "middle", fontSize: CELL_FONT, fill: inkOn(fill, p.fg, p.panel) } });
            }
          }
          return { type: "group", children };
        },
      }],
    };
  }, [p, xLabels, yLabels, cells, valueFmt, cellText, tooltip, onCellClick, xMarks, scale]);
  return (
    <>
      <EChart option={option} height={height} ariaLabel={ariaLabel}
        onClick={onCellClick ? (e) => { const i = (e.value as number[] | undefined)?.[2]; if (i !== undefined) onCellClick(cells[i]); } : undefined} />
      <div className="heat-legend small muted" data-testid="heat-legend">
        <span>{fmt(scale.min, valueFmt)}</span>
        <span className="heat-bar" style={{ background: gradientCss(scale) }} />
        <span>{fmt(scale.max, valueFmt)}</span>
        {legend}
      </div>
    </>
  );
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
