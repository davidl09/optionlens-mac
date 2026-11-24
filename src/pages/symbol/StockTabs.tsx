// Stock group: chart, price action, dividends.
import { useMemo, useState } from "react";
import { api } from "../../api/client";
import type { Bar, ColumnDef, PriceAction, ReportRow } from "../../api/types";
import { useApi } from "../../api/useApi";
import { CandlesVolume, ChartMarker, SMA_LENGTHS, type SmaLength, smasWithData } from "../../components/charts/Candles";
import { LineChart, LineSeries } from "../../components/charts/Charts";
import { usePalette } from "../../components/charts/EChart";
import { DataTable } from "../../components/DataTable";
import { StatGrid } from "../../components/StatGrid";
import { Async, Card, EmptyNotice, InfoBox, Segmented } from "../../components/ui";
import { todayNY } from "../../lib/dates";
import { fmt } from "../../lib/format";
import { isFlowing, useLive, useLiveQuote, useLiveStatus, useThrottled } from "../../live";
import { withLiveBar } from "../../live/merge";
import { useQueryValue, useSym } from "./shared";

const RANGES = ["1m", "3m", "6m", "1y", "3y", "5y", "10y"] as const;

/** The candles with today's bar following the live price (at most once a second; only while quotes flow). */
function LiveCandles({ sym, bars, smas, markers }: { sym: string; bars: Bar[]; smas: SmaLength[]; markers: ChartMarker[] }) {
  useLive({ symbols: [sym] });
  const flowing = isFlowing(useLiveStatus());
  const patch = useThrottled(useLiveQuote(sym), 1000);
  const shown = useMemo(() => (flowing ? withLiveBar(bars, patch, todayNY()) : bars), [bars, patch, flowing]);
  return <CandlesVolume bars={shown} smas={smas} markers={markers} />;
}

export function ChartTab() {
  const sym = useSym();
  const [range, setRange] = useQueryValue("range", "1y");
  const [smas, setSmas] = useState<SmaLength[]>([50, 200]);
  const bars = useApi(() => api.bars(sym, range ?? "1y"), [sym, range]);
  const events = useApi(() => api.events(sym), [sym]);
  const markers = useMemo<ChartMarker[]>(() => (events.data?.items ?? [])
    .filter((e) => e.past && (e.kind === "earnings" || e.kind === "ex_dividend"))
    .map((e) => ({ date: e.date, kind: e.kind === "earnings" ? "E" : "D", label: `${e.date}: ${e.label}` })), [events.data]);

  return (
    <Card title="Price chart" actions={
      <div className="toolbar">
        <Segmented label="Range" value={range ?? "1y"} onChange={setRange} options={RANGES.map((r) => ({ value: r, label: r.toUpperCase() }))} />
        <div className="toggle-chips">
          {SMA_LENGTHS.map((n) => (
            <label key={n} className={`chip ${smas.includes(n) ? "on" : ""}`}>
              <input type="checkbox" checked={smas.includes(n)} onChange={() => setSmas((s) => (s.includes(n) ? s.filter((x) => x !== n) : [...s, n].sort((a, b) => a - b)))} />
              SMA {n}
            </label>
          ))}
        </div>
      </div>
    }>
      <Async state={bars}>{(b) => {
        if (!b.length) return <EmptyNotice>No daily price history stored for {sym} yet (it is collected from Nasdaq every evening).</EmptyNotice>;
        const missing = smas.filter((n) => !smasWithData(b, [n]).length);
        return (
          <>
            <LiveCandles sym={sym} bars={b} smas={smas} markers={markers} />
            <div className="legend-note small muted">
              <span className="dot e" /> E = earnings · <span className="dot d" /> D = ex-dividend · SMAs use the full stored price history
              {missing.length > 0 && <> · {missing.map((n) => `SMA ${n}`).join(", ")}: not enough stored history yet</>}
            </div>
          </>
        );
      }}</Async>
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
  const st = useApi(() => api.dividends(sym), [sym]);
  return (
    <Async state={st}>{(d) => (
      <div className="stack">
        {!d.history.length && <InfoBox>No dividend history for {sym}. The free source (Nasdaq) has dividend history for Nasdaq-listed securities only, so NYSE / NYSE Arca listings show none here even when they pay dividends.</InfoBox>}
        <Card title="Dividend profile">
          <StatGrid items={[
            { label: "Annual rate", value: d.annual_rate, fmt: "money" },
            { label: "Yield", value: d.yield, fmt: "pct" },
            { label: "Payments / yr", value: d.frequency, fmt: "int" },
            { label: "Growth 1y", value: d.growth_1y, fmt: "pct_signed" },
            { label: "CAGR 3y", value: d.growth_3y, fmt: "pct_signed" },
            { label: "CAGR 5y", value: d.growth_5y, fmt: "pct_signed" },
            { label: "Next ex-date", value: d.next?.ex_date, fmt: "date", sub: d.next ? `${fmt(d.next.amount, "money")} · in ${d.next.days}d` : undefined, hint: "No announced ex-date." },
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
