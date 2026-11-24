// Stock group: chart, price action, dividends.
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api/client";
import type { Bar, ColumnDef, IntradayBar, IntradayBars, PriceAction, ReportRow } from "../../api/types";
import { useApi } from "../../api/useApi";
import { type CandleSeries, CandlesVolume, ChartMarker, dailySeries, SMA_LENGTHS, type SmaLength, smasWithData } from "../../components/charts/Candles";
import { LineChart, LineSeries } from "../../components/charts/Charts";
import { usePalette } from "../../components/charts/EChart";
import { DataTable } from "../../components/DataTable";
import { StatGrid } from "../../components/StatGrid";
import { Async, Card, EmptyNotice, InfoBox, Segmented, SourceBadge } from "../../components/ui";
import { desktop } from "../../desktop";
import type { IntradayResult } from "../../../electron/shared/api";
import { barLabel, barTitle, CHART_RANGES, chartView, inRegularHours, INTERVAL_LABEL, INTERVAL_MS, RANGE_INTERVALS, smaOf, type ChartView } from "../../lib/chartRange";
import { todayNY } from "../../lib/dates";
import { fmt } from "../../lib/format";
import { isFlowing, liveStore, useLive, useLiveInterval, useLiveQuote, useLiveStatus, useThrottled } from "../../live";
import { nextCandle, vwapValues, withLiveCandle, type LiveCandle } from "../../live/candles";
import { withLiveBar } from "../../live/merge";
import { useQueryValue, useSym } from "./shared";
import { useMoney } from "../../components/Money";
import { bridgeServes } from "../../lib/markets";

/** The candles with today's bar following the live price (at most once a second; only while quotes flow). */
function LiveDailyCandles({ sym, bars, smas, markers }: { sym: string; bars: Bar[]; smas: SmaLength[]; markers: ChartMarker[] }) {
  useLive({ symbols: [sym] });
  const flowing = isFlowing(useLiveStatus());
  const patch = useThrottled(useLiveQuote(sym), 1000);
  const shown = useMemo(() => (flowing ? withLiveBar(bars, patch, todayNY()) : bars), [bars, patch, flowing]);
  const series = useMemo(() => dailySeries(shown, smas, markers), [shown, smas, markers]);
  return <CandlesVolume series={series} live />;
}

function DailyChart({ sym, range, smas }: { sym: string; range: string; smas: SmaLength[] }) {
  const bars = useApi(() => api.bars(sym, range), [sym, range]);
  const events = useApi(() => api.events(sym), [sym]);
  const markers = useMemo<ChartMarker[]>(() => (events.data?.items ?? [])
    .filter((e) => e.past && (e.kind === "earnings" || e.kind === "ex_dividend"))
    .map((e) => ({ date: e.date, kind: e.kind === "earnings" ? "E" : "D", label: `${e.date}: ${e.label}` })), [events.data]);
  return (
    <Async state={bars}>{(b) => {
      if (!b.length) return <EmptyNotice>No daily price history stored for {sym} yet (it is collected from Nasdaq every evening).</EmptyNotice>;
      const missing = smas.filter((n) => !smasWithData(b, [n]).length);
      return (
        <>
          <LiveDailyCandles sym={sym} bars={b} smas={smas} markers={markers} />
          <div className="legend-note small muted">
            <span className="dot e" /> E = earnings · <span className="dot d" /> D = ex-dividend · SMAs use the full stored price history
            {missing.length > 0 && <> · {missing.map((n) => `SMA ${n}`).join(", ")}: not enough stored history yet</>}
          </div>
        </>
      );
    }}</Async>
  );
}

/**
 * The intraday bars' missing spans from the IBKR bridge (desktop app only): asked once per series shown, then the
 * bars are reloaded. Returns the bridge's answer (null: not asked).
 */
function useBridgeFill(sym: string, data: IntradayBars | null, reload: () => void): IntradayResult | "fetching" | null {
  const [state, setState] = useState<IntradayResult | "fetching" | null>(null);
  const asked = useRef(new Set<string>());
  const key = data ? `${sym}|${data.interval}|${data.range}` : null;
  useEffect(() => { setState(null); }, [key]);
  useEffect(() => {
    // The IBKR bridge serves US listings only: Canadian bars come from Questrade alone.
    if (!desktop || !data || !key || !data.missing.length || asked.current.has(key) || !bridgeServes(sym)) return undefined;
    asked.current.add(key);
    let alive = true;
    setState("fetching");
    desktop.intradayBars({ symbol: sym, interval: data.interval, spans: data.missing }).then((r) => {
      if (!alive) return;
      setState(r);
      if (r.status === "fetched" && r.bars > 0) reload();
    }, (e: unknown) => { if (alive) setState({ status: "failed", message: (e as Error).message }); });
    return () => { alive = false; };
  }, [sym, key, data, reload]);
  return state;
}

/**
 * The last bar following the live price while quotes flow (at most once a second; regular hours unless extended), and
 * the live quote's VWAP (null when not flowing or not sent). The live candles are kept in the live store per series, so
 * a reload of the bars or a remount continues them (merged with the loaded bars by start, live/candles.ts).
 */
function useLiveCandle(sym: string, bars: IntradayBar[], intervalMs: number, extended: boolean): { bars: IntradayBar[]; vwap: number | null } {
  useLive({ symbols: [sym] });
  const flowing = isFlowing(useLiveStatus());
  const patch = useThrottled(useLiveQuote(sym), 1000);
  const series = `${sym}|${intervalMs}|${extended ? "ext" : "rth"}`;
  const [candle, setCandle] = useState<LiveCandle | null>(() => liveStore.candle(series));
  useEffect(() => setCandle(liveStore.candle(series)), [series]);
  useEffect(() => {
    if (!flowing || patch?.price == null) return;
    const at = patch.ts ? Date.parse(patch.ts) : Date.now();
    if (!extended && !inRegularHours(at)) return;
    const next = nextCandle(liveStore.candle(series), { price: patch.price, volume: patch.volume ?? null, at }, intervalMs, bars[bars.length - 1]);
    liveStore.keepCandle(series, next);
    setCandle(next);
  }, [patch, flowing, extended, intervalMs, bars, series]);
  const shown = useMemo(() => withLiveCandle(bars, flowing ? candle : null), [bars, candle, flowing]);
  return { bars: shown, vwap: flowing ? patch?.vwap ?? null : null };
}

/** The source's newest bar lags this much (s) and live-built bars follow it: say so. */
const LAG_NOTE_S = 120;

/** "VWAP" (from the source's VWAPs, and the live quote's), "VWAP (computed)" (typical price × volume of the bars). */
function vwapLabel(d: IntradayBars): string {
  return d.vwap_source === "computed" ? "VWAP (computed)" : "VWAP";
}

function intradaySeries(bars: IntradayBar[], range: IntradayBars["range"], smas: readonly SmaLength[], vwap: { label: string; values: (number | null)[] } | null): CandleSeries {
  return {
    labels: bars.map((b) => barLabel(b.ts, range)), titles: bars.map((b) => barTitle(b.ts)), bars,
    smas: smas.filter((n) => bars.length >= n).map((n) => ({ label: `SMA ${n} (bars)`, length: n, values: smaOf(bars, n) })),
    markers: [], lines: vwap && vwap.values.some((v) => v !== null) ? [vwap] : [],
  };
}

function IntradayBody({ sym, d, smas, fill }: { sym: string; d: IntradayBars; smas: SmaLength[]; fill: IntradayResult | "fetching" | null }) {
  const live = useLiveCandle(sym, d.bars, INTERVAL_MS[d.interval], d.extended);
  const shown = live.bars;
  const liveBuilt = (d.lag_s ?? 0) > LAG_NOTE_S && (d.sources.includes("live") || shown.length > d.bars.length);
  const series = useMemo(() => intradaySeries(shown, d.range, smas, { label: vwapLabel(d), values: vwapValues(shown, live.vwap) }), [shown, d, smas, live.vwap]);
  const short = smas.filter((n) => shown.length < n);
  const bridge = fill === "fetching" ? "Fetching the missing bars from IBKR…"
    : fill && fill.status !== "fetched" ? `IBKR: ${fill.message}` : null;
  return (
    <>
      {d.bars.length > 0 ? <CandlesVolume series={series} live key={`${d.range}|${d.interval}|${d.extended}`} />
        : <EmptyNotice>{bridge ?? `No ${d.interval} bars stored for ${sym} in this range yet${d.problem ? ` (${d.problem})` : ""}. They come from Questrade (Settings → Data sources) or, in the desktop app, from the IBKR bridge.`}</EmptyNotice>}
      <div className="legend-note small muted">
        Times in New York (ET) · {d.extended ? "including pre-market and after-hours" : "regular hours"} · overnight and weekend gaps collapsed
        {d.sources.length > 0 && <> · Source {d.sources.map((s) => <SourceBadge key={s} source={s} />)}{d.delayed && <SourceBadge source={null} delayed />}</>}
        {liveBuilt && <> · <span className="live-built" title={`The source's newest bar ended ${Math.round((d.lag_s ?? 0) / 60)} min ago; the minutes since are built from live quotes until its own bars arrive`}>latest minutes built from live quotes</span></>}
        {smas.length > 0 && <> · SMAs over the loaded bars</>}
        {series.lines?.length ? <> · {d.vwap_source === "computed" ? "VWAP computed from the bars (typical price × volume)" : "Session VWAP"}{live.vwap !== null && " · live point from the quote's VWAP"}</> : null}
        {short.length > 0 && <> · {short.map((n) => `SMA ${n}`).join(", ")}: fewer than {short[0]} bars loaded</>}
        {d.bars.length > 0 && bridge && <> · {bridge}</>}
        {d.bars.length > 0 && d.problem && !bridge && d.missing.length > 0 && <> · Some bars missing ({d.problem})</>}
      </div>
    </>
  );
}

/** How often the intraday bars are reloaded while live quotes flow and the window is visible. */
const BARS_RELOAD_MS = 60_000;

function IntradayChart({ sym, view, extended, smas, onExtended }: {
  sym: string; view: Extract<ChartView, { kind: "intraday" }>; extended: boolean; smas: SmaLength[]; onExtended: (available: boolean) => void;
}) {
  const st = useApi(() => api.intradayBars(sym, view.interval, view.range, extended), [sym, view.interval, view.range, extended]);
  const fill = useBridgeFill(sym, st.data, st.reload);
  useLiveInterval(st.reload, BARS_RELOAD_MS);   // settled source bars replace the live-built ones
  const hasExtended = st.data?.has_extended ?? false;
  useEffect(() => onExtended(hasExtended), [hasExtended, onExtended]);
  return <Async state={st}>{(d) => <IntradayBody sym={sym} d={d} smas={smas} fill={fill} />}</Async>;
}

export function ChartTab() {
  const sym = useSym();
  const [rangeQ, setRange] = useQueryValue("range", "1y");
  const [intervalQ, setIntervalQ] = useQueryValue("interval", null);
  const [extQ, setExt] = useQueryValue("ext", null);
  const view = chartView(rangeQ, intervalQ);
  const extended = extQ === "1";
  const [smas, setSmas] = useState<SmaLength[]>([50, 200]);
  const [extAvailable, setExtAvailable] = useState(false);   // the source has pre-market / after-hours bars here
  const offered = RANGE_INTERVALS[view.range];
  const interval = view.kind === "daily" ? "1d" : view.interval;
  return (
    <Card title="Price chart" actions={
      <div className="toolbar">
        <Segmented label="Range" value={view.range} onChange={setRange} options={CHART_RANGES.map((r) => ({ value: r, label: r.toUpperCase() }))} />
        {offered.length > 1 && (
          <Segmented label="Interval" value={interval} onChange={(i) => setIntervalQ(i === offered[0] ? null : i)}
            options={offered.map((i) => ({ value: i, label: INTERVAL_LABEL[i], title: i === "1d" ? "Daily bars" : `${i} bars` }))} />
        )}
        {view.kind === "intraday" && (extAvailable || extended) && (
          <label className={`chip ${extended ? "on" : ""}`} title="Show pre-market (from 04:00 ET) and after-hours (to 20:00 ET) bars where the source has them">
            <input type="checkbox" checked={extended} onChange={() => setExt(extended ? null : "1")} />Extended hours
          </label>
        )}
        <div className="toggle-chips">
          {SMA_LENGTHS.map((n) => (
            <label key={n} className={`chip ${smas.includes(n) ? "on" : ""}`}>
              <input type="checkbox" checked={smas.includes(n)} onChange={() => setSmas((s) => (s.includes(n) ? s.filter((x) => x !== n) : [...s, n].sort((a, b) => a - b)))} />
              SMA {n}{view.kind === "intraday" ? " (bars)" : ""}
            </label>
          ))}
        </div>
      </div>
    }>
      {view.kind === "daily" ? <DailyChart sym={sym} range={view.range} smas={smas} />
        : <IntradayChart sym={sym} view={view} extended={extended} smas={smas} onExtended={setExtAvailable} />}
    </Card>
  );
}

const PA_COLUMNS: ColumnDef[] = [
  { key: "date", label: "Date", fmt: "date" },
  { key: "open", label: "Open", fmt: "money" },
  { key: "high", label: "High", fmt: "money" },
  { key: "low", label: "Low", fmt: "money" },
  { key: "close", label: "Close", fmt: "money" },
  { key: "change_pct", label: "% Chg", fmt: "pct_signed" },
  { key: "gap_pct", label: "Gap", fmt: "pct_signed" },
  { key: "range_pct", label: "Range", fmt: "pct" },
  { key: "volume", label: "Volume", fmt: "int" },
  { key: "rel_volume", label: "Rel vol", fmt: "ratio" },
];

function PriceActionBody({ pa, sym }: { pa: PriceAction; sym: string }) {
  const p = usePalette();
  const io = pa.intraday_overnight;
  const series = useMemo<LineSeries[]>(() => [
    { name: "Overnight (cum.)", data: io.series.map((x) => [x.date, x.cum_overnight]), color: p.series[6] },
    { name: "Intraday (cum.)", data: io.series.map((x) => [x.date, x.cum_intraday]), color: p.series[1] },
  ], [io.series, p]);
  const st = pa.stats;
  return (
    <div className="stack">
      <div className="grid-2">
        <Card title="Daily stats" subtitle={`${st.days} sessions`}>
          <StatGrid cols={3} items={[
            { label: "Avg |move|", value: st.avg_abs_move, fmt: "pct" },
            { label: "Daily stdev", value: st.stdev_daily, fmt: "pct" },
            { label: "Up days", value: st.up_day_pct, fmt: "pct" },
            { label: "Avg |gap|", value: st.avg_gap_abs, fmt: "pct" },
            { label: "Largest up", value: st.largest_up?.change_pct, fmt: "pct_signed", sub: st.largest_up?.date },
            { label: "Largest down", value: st.largest_down?.change_pct, fmt: "pct_signed", sub: st.largest_down?.date },
          ]} />
        </Card>
        <Card title="Intraday vs overnight">
          <StatGrid cols={3} items={[
            { label: "Avg overnight", value: io.avg_overnight, fmt: "pct_signed" },
            { label: "Avg intraday", value: io.avg_intraday, fmt: "pct_signed" },
            { label: "Overnight up", value: io.overnight_up_pct, fmt: "pct" },
            { label: "Cum. overnight", value: io.cum_overnight, fmt: "pct_signed" },
            { label: "Cum. intraday", value: io.cum_intraday, fmt: "pct_signed" },
            { label: "Intraday up", value: io.intraday_up_pct, fmt: "pct" },
          ]} />
        </Card>
      </div>
      <Card title="Cumulative overnight vs intraday return"><LineChart series={series} yFmt="pct_signed" height={240} /></Card>
      <Card title="Daily history">
        <DataTable columns={PA_COLUMNS} rows={pa.rows as unknown as ReportRow[]} csvName={`${sym}-price-action`} rowLink={false} maxHeight={520} />
      </Card>
    </div>
  );
}

export function PriceActionTab() {
  const sym = useSym();
  const [range, setRange] = useQueryValue("range", "1y");
  const st = useApi(() => api.priceAction(sym, range ?? "1y"), [sym, range]);
  return (
    <div className="stack">
      <div className="toolbar"><Segmented label="Range" value={range ?? "1y"} onChange={setRange} options={["3m", "6m", "1y", "3y", "5y"].map((r) => ({ value: r, label: r.toUpperCase() }))} /></div>
      <Async state={st}>{(pa) => <PriceActionBody pa={pa} sym={sym} />}</Async>
    </div>
  );
}

const DIV_COLUMNS: ColumnDef[] = [
  { key: "ex_date", label: "Ex-date", fmt: "date" },
  { key: "amount", label: "Amount", fmt: "money" },
  { key: "kind", label: "Type", fmt: "badge" },
  { key: "declared_date", label: "Declared", fmt: "date" },
  { key: "record_date", label: "Record", fmt: "date" },
  { key: "pay_date", label: "Pay date", fmt: "date" },
  { key: "source", label: "Source", fmt: "text", description: "Nasdaq; IBKR adj. = derived from IBKR's dividend-adjusted closes (amounts in today's shares); Questrade = its current dividend" },
  { key: "discrepancy", label: "Sources differ", fmt: "bool", description: "Another source's amount for this ex-date differs by more than $0.01" },
];
const CAPTURE_COLUMNS: ColumnDef[] = [
  { key: "ex_date", label: "Ex-date", fmt: "date" },
  { key: "amount", label: "Dividend", fmt: "money" },
  { key: "pre_close", label: "Close before ex", fmt: "money" },
  { key: "ex_close", label: "Close on ex", fmt: "money" },
  { key: "price_return", label: "Price return", fmt: "pct_signed" },
  { key: "total_return", label: "Total return", fmt: "pct_signed" },
  { key: "recovery_days", label: "Recovery days", fmt: "int", description: "Trading days until close ≥ pre-ex close" },
];

export function DividendsTab() {
  const sym = useSym();
  const money = useMoney();
  const st = useApi(() => api.dividends(sym), [sym]);
  return (
    <Async state={st}>{(d) => (
      <div className="stack">
        {d.unavailable && <InfoBox title="Not available:">{d.unavailable}</InfoBox>}
        {!d.history.length && !d.unavailable && <InfoBox>No dividends stored for {sym} yet. They come from Nasdaq, from IBKR's dividend-adjusted price history (collected by the IBKR bridge in the desktop app) and from Questrade's daily symbol info; a symbol that pays none stays empty.</InfoBox>}
        {d.history.some((h) => h.discrepancy) && <InfoBox>Some ex-dates have amounts that differ between sources by more than $0.01 (marked below); the amount shown is Nasdaq's, else IBKR's, else Questrade's.</InfoBox>}
        <Card title="Dividend profile">
          <StatGrid items={[
            { label: "Annual rate", value: d.annual_rate, fmt: "money" },
            { label: "Yield", value: d.yield, fmt: "pct" },
            { label: "Payments / yr", value: d.frequency, fmt: "int" },
            { label: "Growth 1y", value: d.growth_1y, fmt: "pct_signed" },
            { label: "CAGR 3y", value: d.growth_3y, fmt: "pct_signed" },
            { label: "CAGR 5y", value: d.growth_5y, fmt: "pct_signed" },
            { label: "Next ex-date", value: d.next?.ex_date, fmt: "date", sub: d.next ? `${fmt(d.next.amount, "money", money)} · in ${d.next.days}d` : undefined, hint: "No announced ex-date." },
          ]} />
        </Card>
        <Card title="Dividend capture" subtitle="Buy at close before ex-date, sell at close on ex-date">
          <StatGrid cols={4} items={[
            { label: "Ex-dates measured", value: d.capture_stats.count, fmt: "int", title: "Ex-dates in the capture table: those with a close on the day before and on the ex-date (the history below lists every payment)" },
            { label: "Avg total return", value: d.capture_stats.avg_total_return, fmt: "pct_signed" },
            { label: "Win rate", value: d.capture_stats.win_rate, fmt: "pct" },
            { label: "Avg recovery (days)", value: d.capture_stats.avg_recovery_days, fmt: "num" },
          ]} />
          <DataTable columns={CAPTURE_COLUMNS} rows={d.capture as unknown as ReportRow[]} csvName={`${sym}-dividend-capture`} rowLink={false} maxHeight={360} />
        </Card>
        <Card title="History">
          <DataTable columns={DIV_COLUMNS} rows={d.history as unknown as ReportRow[]} csvName={`${sym}-dividends`} rowLink={false} maxHeight={420} />
        </Card>
      </div>
    )}</Async>
  );
}
