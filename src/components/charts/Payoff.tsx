// Strategy P/L curves (expiry / chosen date / today) with breakeven and spot markers. The axes stay put while live
// ticks move the curves (calcPricing.stableRange): rescaled only when a curve leaves them or `scaleKey` changes.
import { useMemo, useRef } from "react";
import type { CalcResult } from "../../api/types";
import { extent, type Range, stableRange } from "../../lib/calcPricing";
import { fmt } from "../../lib/format";
import { EChart, usePalette } from "./EChart";
import { axisStyle, GRID, legendStyle, tooltipStyle } from "./common";

/** Vertical gradient: profit colour above zero, loss colour below (relative to the line's own y-extent). */
function signGradient(ys: number[], up: string, down: string) {
  const max = Math.max(...ys), min = Math.min(...ys);
  if (min >= 0) return up;
  if (max <= 0) return down;
  const z = max / (max - min);
  return { type: "linear", x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: up }, { offset: z, color: up }, { offset: z, color: down }, { offset: 1, color: down }] };
}

/** `scaleKey`: what the chart shows (position, eval date, IV shift); a new one rescales the axes. */
export function Payoff({ result, evalLabel, scaleKey = "", height = 340 }: { result: CalcResult; evalLabel: string; scaleKey?: string; height?: number }) {
  const p = usePalette();
  const yRange = useRef<{ key: string; range: Range } | null>(null);
  const option = useMemo(() => {
    const data = extent(result.pnl_expiry, result.pnl_eval, result.pnl_today) ?? [0, 0];
    const range = stableRange(yRange.current?.key === scaleKey ? yRange.current.range : null, data);
    yRange.current = { key: scaleKey, range };
    const xStyle = axisStyle(p, "money");
    const xs = result.prices;
    const pts = (ys: number[]) => xs.map((x, i) => [x, ys[i]]);
    const vline = (x: number, label: string, color: string, position = "insideEndTop") => ({
      xAxis: x, lineStyle: { color, type: "dashed", width: 1 }, label: { formatter: label, color, fontSize: 10, position },
    });
    return {
      animation: false,
      grid: { ...GRID, top: 32 },
      legend: { ...legendStyle(p), data: ["At expiry", evalLabel, "Today"] },
      tooltip: {
        trigger: "axis", ...tooltipStyle(p), axisPointer: { type: "line", lineStyle: { color: p.muted, type: "dashed" } },
        formatter: (items: { value: [number, number]; seriesName: string; marker: string }[]) => {
          const x = items[0]?.value[0];
          const rows = items.map((it) => `${it.marker}${it.seriesName}<b style="float:right;margin-left:16px">${fmt(it.value[1], "money")}</b>`);
          return `<div style="min-width:170px"><div style="opacity:.7">Price ${fmt(x, "money")} (${fmt(x / result.spot - 1, "pct_signed")})</div>${rows.join("<br/>")}</div>`;
        },
      },
      // fixed bounds (stable on live ticks); their own labels would be odd numbers, so only the inner ticks are labelled
      xAxis: { type: "value", min: xs[0], max: xs[xs.length - 1], ...xStyle, axisLabel: { ...xStyle.axisLabel, showMinLabel: false, showMaxLabel: false }, splitLine: { show: false } },
      yAxis: { type: "value", min: range[0], max: range[1], ...axisStyle(p, "money") },
      series: [
        {
          name: "At expiry", type: "line", data: pts(result.pnl_expiry), showSymbol: false,
          lineStyle: { width: 2, color: signGradient(result.pnl_expiry, p.up, p.down) }, itemStyle: { color: p.up },
          areaStyle: { opacity: 0.1, color: signGradient(result.pnl_expiry, p.up, p.down) },
          markLine: {
            symbol: "none", silent: true, animation: false,
            data: [
              { yAxis: 0, lineStyle: { color: p.axis, type: "solid", width: 1 }, label: { show: false } },
              vline(result.spot, "spot", p.accent, "insideStartTop"),
              ...result.breakevens.map((b) => vline(b, `BE ${fmt(b, "num2")}`, p.muted)),
            ],
          },
        },
        { name: evalLabel, type: "line", data: pts(result.pnl_eval), showSymbol: false, lineStyle: { width: 2, type: "dashed", color: p.series[6] }, itemStyle: { color: p.series[6] } },
        { name: "Today", type: "line", data: pts(result.pnl_today), showSymbol: false, lineStyle: { width: 1.5, type: "dotted", color: p.series[3] }, itemStyle: { color: p.series[3] } },
      ],
    };
  }, [p, result, evalLabel, scaleKey]);
  return <EChart option={option} height={height} ariaLabel="Strategy profit and loss chart" />;
}
