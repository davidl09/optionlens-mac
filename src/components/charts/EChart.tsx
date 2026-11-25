// Minimal ECharts wrapper: init once, setOption on change, resize with the container,
// re-theme from CSS variables when the app theme changes.
import { useEffect, useMemo, useRef } from "react";
import * as echarts from "echarts/core";
import { BarChart, CandlestickChart, CustomChart, LineChart, ScatterChart } from "echarts/charts";
import {
  DataZoomComponent, GridComponent, LegendComponent, MarkAreaComponent, MarkLineComponent, MarkPointComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { EChartsCoreOption } from "echarts/core";
import { useTheme } from "../../state/theme";

echarts.use([
  BarChart, CandlestickChart, CustomChart, LineChart, ScatterChart, DataZoomComponent, GridComponent, LegendComponent,
  MarkAreaComponent, MarkLineComponent, MarkPointComponent, TooltipComponent, CanvasRenderer,
]);

export type ChartOption = EChartsCoreOption;

export interface Palette {
  fg: string; fg2: string; muted: string; grid: string; axis: string; panel: string; tooltipBg: string;
  up: string; down: string; accent: string; series: string[];
  /** Heatmaps: the neutral midpoint, the sequential ramp (low to high) and the fill of a cell without a value. */
  heatMid: string; heatSeq: string[]; heatNa: string;
}

function cssVar(style: CSSStyleDeclaration, name: string, fallback: string): string {
  return style.getPropertyValue(name).trim() || fallback;
}

/** Chart colours, read from the CSS variables of the active theme. */
export function usePalette(): Palette {
  const { theme } = useTheme();
  return useMemo(() => {
    const s = getComputedStyle(document.documentElement);
    const series = [1, 2, 3, 4, 5, 6, 7, 8].map((i) => cssVar(s, `--series-${i}`, "#3987e5"));
    return {
      fg: cssVar(s, "--fg", "#e7e9ee"), fg2: cssVar(s, "--fg-2", "#b4bac6"), muted: cssVar(s, "--muted", "#898781"),
      grid: cssVar(s, "--chart-grid", "#2c2c2a"), axis: cssVar(s, "--chart-axis", "#383835"), panel: cssVar(s, "--panel", "#161a21"),
      tooltipBg: cssVar(s, "--panel-2", "#1c212a"), up: cssVar(s, "--up", "#1fae74"), down: cssVar(s, "--down", "#e5605f"),
      accent: cssVar(s, "--accent", "#3987e5"), series,
      heatMid: cssVar(s, "--heat-mid", "#2a3140"), heatNa: cssVar(s, "--heat-na", "#1f2530"),
      heatSeq: [1, 2, 3].map((i) => cssVar(s, `--heat-seq-${i}`, "#3987e5")),
    };
    // `theme` is the trigger for re-reading the variables
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [theme]);
}

export interface ChartClick { name?: string; value?: unknown; dataIndex?: number; seriesName?: string }

export interface EChartProps {
  option: ChartOption;
  height?: number;
  onClick?: (params: ChartClick) => void;
  ariaLabel?: string;
  /** Keep the user's zoom (dataZoom start/end, in %) when the option changes, e.g. on live updates. */
  keepZoom?: boolean;
}

export function EChart({ option, height = 260, onClick, ariaLabel, keepZoom = false }: EChartProps) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const clickRef = useRef(onClick);
  clickRef.current = onClick;

  useEffect(() => {
    if (!el.current) return undefined;
    const c = echarts.init(el.current, undefined, { renderer: "canvas" });
    chart.current = c;
    c.on("click", (p) => clickRef.current?.(p as ChartClick));
    const ro = new ResizeObserver(() => c.resize());
    ro.observe(el.current);
    return () => { ro.disconnect(); c.dispose(); chart.current = null; };
  }, []);

  useEffect(() => {
    const c = chart.current;
    if (!c) return;
    const zoom = keepZoom ? (c.getOption() as { dataZoom?: { start?: number; end?: number }[] } | undefined)?.dataZoom?.[0] : undefined;
    c.setOption(option, { notMerge: true, lazyUpdate: true });
    if (zoom && zoom.start !== undefined && zoom.end !== undefined && (zoom.start > 0 || zoom.end < 100)) {
      c.dispatchAction({ type: "dataZoom", start: zoom.start, end: zoom.end });
    }
  }, [option, keepZoom]);

  return <div ref={el} className="echart" style={{ height }} role="img" aria-label={ariaLabel} />;
}
