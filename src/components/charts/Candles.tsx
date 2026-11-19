// Candlestick + volume (two stacked grids sharing one category axis), SMA overlays, event markers.
import { useMemo } from "react";
import type { Bar } from "../../api/types";
import { fmt } from "../../lib/format";
import { sma } from "../../lib/indicators";
import { EChart, usePalette } from "./EChart";
import { axisStyle, legendStyle, tooltipStyle } from "./common";

export interface ChartMarker { date: string; kind: "E" | "D"; label: string }

export function CandlesVolume({ bars, smas, markers, height = 460 }: { bars: Bar[]; smas: number[]; markers: ChartMarker[]; height?: number }) {
  const p = usePalette();
  const option = useMemo(() => {
    const dates = bars.map((b) => b.date);
    const closes = bars.map((b) => b.close);
    const index = new Map(dates.map((d, i) => [d, i]));
    const smaColors = [p.series[3], p.series[4], p.series[6]];
    const markPoints = markers
      .map((m) => {
        // snap to the first bar on/after the event date
        let i = index.get(m.date);
        if (i === undefined) { i = dates.findIndex((d) => d >= m.date); if (i < 0) return null; }
        const b = bars[i];
        return {
          coord: [dates[i], m.kind === "E" ? b.high : b.low], value: m.kind, name: m.label,
          symbol: "pin", symbolSize: 26, symbolOffset: [0, m.kind === "E" ? -6 : 6], symbolRotate: m.kind === "E" ? 0 : 180,
          itemStyle: { color: m.kind === "E" ? p.series[6] : p.series[2] },
          label: { color: "#fff", fontSize: 10, fontWeight: 600, formatter: m.kind, offset: [0, m.kind === "E" ? 0 : 3] },
        };
      })
      .filter(Boolean);
    return {
      animation: false,
      legend: smas.length ? { ...legendStyle(p), data: smas.map((n) => `SMA ${n}`) } : undefined,
      tooltip: {
        trigger: "axis", ...tooltipStyle(p), axisPointer: { type: "cross", lineStyle: { color: p.muted } },
        formatter: (items: { dataIndex: number }[]) => {
          const b = bars[items[0]?.dataIndex ?? -1];
          if (!b) return "";
          const chg = items[0].dataIndex > 0 ? b.close / bars[items[0].dataIndex - 1].close - 1 : null;
          return `<b>${b.date}</b><br/>O ${fmt(b.open, "num2")} H ${fmt(b.high, "num2")}<br/>L ${fmt(b.low, "num2")} C ${fmt(b.close, "num2")}<br/>Chg ${fmt(chg, "pct_signed")} · Vol ${fmt(b.volume, "int")}`;
        },
      },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid: [
        { left: 8, right: 16, top: 30, height: "62%", containLabel: true },
        { left: 8, right: 16, top: "76%", bottom: 34, containLabel: true },
      ],
      xAxis: [
        { type: "category", data: dates, ...axisStyle(p), axisLabel: { show: false }, splitLine: { show: false }, boundaryGap: true },
        { type: "category", data: dates, gridIndex: 1, ...axisStyle(p), splitLine: { show: false }, boundaryGap: true },
      ],
      yAxis: [
        { type: "value", scale: true, ...axisStyle(p, "money") },
        { type: "value", gridIndex: 1, ...axisStyle(p, "int"), splitNumber: 2 },
      ],
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1] },
        { type: "slider", xAxisIndex: [0, 1], height: 16, bottom: 6, borderColor: p.axis, textStyle: { color: p.muted } },
      ],
      series: [
        {
          name: "Price", type: "candlestick", data: bars.map((b) => [b.open, b.close, b.low, b.high]),
          itemStyle: { color: p.up, color0: p.down, borderColor: p.up, borderColor0: p.down },
          markPoint: { data: markPoints, tooltip: { ...tooltipStyle(p), formatter: (it: { name: string }) => it.name } },
        },
        ...smas.map((n, i) => ({
          name: `SMA ${n}`, type: "line", data: sma(closes, n), showSymbol: false,
          lineStyle: { width: 1.5, color: smaColors[i % smaColors.length] }, itemStyle: { color: smaColors[i % smaColors.length] },
        })),
        {
          name: "Volume", type: "bar", xAxisIndex: 1, yAxisIndex: 1,
          data: bars.map((b, i) => ({ value: b.volume, itemStyle: { color: i > 0 && b.close < bars[i - 1].close ? p.down : p.up, opacity: 0.6 } })),
        },
      ],
    };
  }, [p, bars, smas, markers]);
  return <EChart option={option} height={height} ariaLabel="Price candlestick chart with volume" />;
}
