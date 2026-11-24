// Candlestick + volume (two stacked grids sharing one category axis), SMA overlays, event markers. One category per
// bar, so nights and weekends take no room on intraday charts.
import { useMemo } from "react";
import type { Bar } from "../../api/types";
import { fmt } from "../../lib/format";
import { EChart, usePalette } from "./EChart";
import { axisStyle, legendStyle, tooltipStyle } from "./common";

export interface ChartMarker { date: string; kind: "E" | "D"; label: string }

export const SMA_LENGTHS = [20, 50, 200] as const;
export type SmaLength = (typeof SMA_LENGTHS)[number];
const SMA_KEY = { 20: "sma20", 50: "sma50", 200: "sma200" } as const;

export interface CandleBar { open: number; high: number; low: number; close: number; volume: number | null }

/** What the chart draws: per bar an axis label, a tooltip title and OHLCV; SMA lines; markers by bar index. */
export interface CandleSeries {
  labels: string[];
  titles: string[];
  bars: CandleBar[];
  smas: { label: string; length: SmaLength; values: (number | null)[] }[];
  markers: { index: number; kind: "E" | "D"; label: string }[];
  /** More price lines over the candles (e.g. the session VWAP), dashed. */
  lines?: { label: string; values: (number | null)[] }[];
}

/** The engine's full-history SMA of each bar (null before enough history). */
export function smaSeries(bars: Bar[], n: SmaLength): (number | null)[] {
  return bars.map((b) => b[SMA_KEY[n]] ?? null);
}

/** SMA lengths with at least one value in these bars (only those get a line and a legend entry). */
export function smasWithData(bars: Bar[], smas: readonly SmaLength[]): SmaLength[] {
  return smas.filter((n) => bars.some((b) => b[SMA_KEY[n]] != null));
}

/** Daily bars as a chart series: date labels, the engine's SMAs, markers on the first bar on/after their date. */
export function dailySeries(bars: Bar[], smas: readonly SmaLength[], markers: ChartMarker[]): CandleSeries {
  const dates = bars.map((b) => b.date);
  const index = new Map(dates.map((d, i) => [d, i]));
  return {
    labels: dates, titles: dates, bars,
    smas: smasWithData(bars, smas).map((n) => ({ label: `SMA ${n}`, length: n, values: smaSeries(bars, n) })),
    markers: markers.flatMap((m) => {
      const i = index.get(m.date) ?? dates.findIndex((d) => d >= m.date);
      return i < 0 ? [] : [{ index: i, kind: m.kind, label: m.label }];
    }),
  };
}

/** `live`: the series updates in place (live candle): the user's zoom is kept across updates. */
export function CandlesVolume({ series, height = 460, live = false }: { series: CandleSeries; height?: number; live?: boolean }) {
  const p = usePalette();
  const option = useMemo(() => {
    const { labels, titles, bars, smas } = series;
    const lines = series.lines ?? [];
    const legend = [...smas.map((s) => s.label), ...lines.map((l) => l.label)];
    const smaColor = (n: SmaLength) => [p.series[3], p.series[4], p.series[6]][SMA_LENGTHS.indexOf(n)];
    const markPoints = series.markers.map((m) => {
      const b = bars[m.index];
      return {
        coord: [labels[m.index], m.kind === "E" ? b.high : b.low], value: m.kind, name: m.label,
        symbol: "pin", symbolSize: 26, symbolOffset: [0, m.kind === "E" ? -6 : 6], symbolRotate: m.kind === "E" ? 0 : 180,
        itemStyle: { color: m.kind === "E" ? p.series[6] : p.series[2] },
        label: { color: "#fff", fontSize: 10, fontWeight: 600, formatter: m.kind, offset: [0, m.kind === "E" ? 0 : 3] },
      };
    });
    return {
      animation: false,
      legend: legend.length ? { ...legendStyle(p), data: legend } : undefined,
      tooltip: {
        trigger: "axis", ...tooltipStyle(p), axisPointer: { type: "cross", lineStyle: { color: p.muted } },
        formatter: (items: { dataIndex: number }[]) => {
          const i = items[0]?.dataIndex ?? -1;
          const b = bars[i];
          if (!b) return "";
          const chg = i > 0 ? b.close / bars[i - 1].close - 1 : null;
          return `<b>${titles[i]}</b><br/>O ${fmt(b.open, "num2")} H ${fmt(b.high, "num2")}<br/>L ${fmt(b.low, "num2")} C ${fmt(b.close, "num2")}<br/>Chg ${fmt(chg, "pct_signed")} · Vol ${fmt(b.volume, "int")}`;
        },
      },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid: [
        { left: 8, right: 16, top: 30, height: "62%", containLabel: true },
        { left: 8, right: 16, top: "76%", bottom: 34, containLabel: true },
      ],
      xAxis: [
        { type: "category", data: labels, ...axisStyle(p), axisLabel: { show: false }, splitLine: { show: false }, boundaryGap: true },
        { type: "category", data: labels, gridIndex: 1, ...axisStyle(p), splitLine: { show: false }, boundaryGap: true },
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
        ...smas.map((s) => ({
          name: s.label, type: "line", data: s.values, showSymbol: false, connectNulls: false,
          lineStyle: { width: 1.5, color: smaColor(s.length) }, itemStyle: { color: smaColor(s.length) },
        })),
        ...lines.map((l) => ({
          name: l.label, type: "line", data: l.values, showSymbol: false, connectNulls: false,
          lineStyle: { width: 1.5, type: "dashed", color: p.series[1] }, itemStyle: { color: p.series[1] },
        })),
        {
          name: "Volume", type: "bar", xAxisIndex: 1, yAxisIndex: 1,
          data: bars.map((b, i) => ({ value: b.volume, itemStyle: { color: i > 0 && b.close < bars[i - 1].close ? p.down : p.up, opacity: 0.6 } })),
        },
      ],
    };
  }, [p, series]);
  const n = series.bars.length;
  const last = series.bars[n - 1];
  return (
    <>
      <EChart option={option} height={height} keepZoom={live} ariaLabel="Price candlestick chart with volume" />
      {last && (
        <p className="sr-only chart-summary" data-bars={n}>
          {n} bars; last bar {series.titles[n - 1]}: open {fmt(last.open, "num2")}, high {fmt(last.high, "num2")}, low {fmt(last.low, "num2")},
          close {fmt(last.close, "num2")}, volume {fmt(last.volume, "int")}
        </p>
      )}
    </>
  );
}
