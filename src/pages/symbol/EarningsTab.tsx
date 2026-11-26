// Earnings: next event, history, charts, and the per-symbol earnings option-strategy backtest.
import { useMemo, useState } from "react";
import { api } from "../../api/client";
import type { ColumnDef, Earnings, EarningsEventRow, EarningsStats, ReportRow } from "../../api/types";
import { useApi } from "../../api/useApi";
import { TRADE_COLUMNS, tradesAsRows } from "../../components/BacktestResultView";
import { BarChart } from "../../components/charts/Charts";
import { usePalette } from "../../components/charts/EChart";
import { DataTable } from "../../components/DataTable";
import { StatGrid, StatItem } from "../../components/StatGrid";
import { Async, Card, InfoBox, Segmented, SourceBadge } from "../../components/ui";
import { nextEarningsSub, noNextEarningsText } from "../../lib/earnings";
import { useSym } from "./shared";

const EVENT_COLUMNS: ColumnDef[] = [
  { key: "date", label: "Date", fmt: "date" },
  { key: "timing_label", label: "Time", fmt: "badge", description: "BMO = before the open, AMC = after the close; “(inferred)” = read off which day gapped more; n/a = unknown" },
  { key: "timing_inferred", label: "Timing inferred", fmt: "bool", default_hidden: true, description: "Timing unknown; inferred from which day gapped more" },
  { key: "reaction_date", label: "Reaction day", fmt: "date", default_hidden: true },
  { key: "fiscal_quarter", label: "Fiscal qtr", fmt: "text" },
  { key: "eps_estimate", label: "EPS est", fmt: "num2", group: "EPS" },
  { key: "eps_actual", label: "EPS actual", fmt: "num2", group: "EPS" },
  { key: "surprise_pct", label: "Surprise", fmt: "pct_signed", group: "EPS" },
  { key: "pre_close", label: "Pre close", fmt: "money", group: "Move" },
  { key: "gap_pct", label: "Gap", fmt: "pct_signed", group: "Move" },
  { key: "move_pct", label: "Move", fmt: "pct_signed", group: "Move" },
  { key: "max_move_pct", label: "Max move", fmt: "pct", group: "Move" },
  { key: "implied_move_pct", label: "Implied", fmt: "pct", group: "Move", description: "Straddle-implied move, when the chain was stored before the event" },
  { key: "pre_drift_5", label: "Pre 5d", fmt: "pct_signed", group: "Drift", default_hidden: true },
  { key: "pre_drift_10", label: "Pre 10d", fmt: "pct_signed", group: "Drift", default_hidden: true },
  { key: "post_drift_1", label: "Post 1d", fmt: "pct_signed", group: "Drift" },
  { key: "post_drift_5", label: "Post 5d", fmt: "pct_signed", group: "Drift" },
  { key: "post_drift_10", label: "Post 10d", fmt: "pct_signed", group: "Drift" },
  { key: "post_drift_20", label: "Post 20d", fmt: "pct_signed", group: "Drift" },
  { key: "iv30_before", label: "IV30 before", fmt: "vol", group: "IV crush" },
  { key: "iv30_after", label: "IV30 after", fmt: "vol", group: "IV crush" },
];

const STRAT_COLUMNS: ColumnDef[] = [
  { key: "label", label: "Strategy", fmt: "text" },
  { key: "trades", label: "Trades", fmt: "int" },
  { key: "win_rate", label: "Win rate", fmt: "pct" },
  { key: "avg_return", label: "Avg return", fmt: "pct_signed", description: "% of debit, or of max risk for credit strategies" },
  { key: "median_return", label: "Median", fmt: "pct_signed" },
  { key: "best", label: "Best", fmt: "pct_signed" },
  { key: "worst", label: "Worst", fmt: "pct_signed" },
  { key: "avg_pnl", label: "Avg $ P/L", fmt: "money", description: "Per 1-lot" },
  { key: "real_count", label: "Real-priced", fmt: "int" },
  { key: "model_count", label: "Model-priced", fmt: "int" },
];

const ENTRY = [-10, -5, -3, -2, -1];
const EXIT = [1, 2, 3, 5, 10];
const LOOKBACK = [4, 8, 12];

function statItems(s: EarningsStats): StatItem[] {
  return [
    { label: "Events", value: s.count, fmt: "int" },
    { label: "Avg move", value: s.avg_move, fmt: "pct_signed" },
    { label: "Avg |move|", value: s.avg_abs_move, fmt: "pct" },
    { label: "Median |move|", value: s.median_abs_move, fmt: "pct" },
    { label: "Stdev move", value: s.stdev_move, fmt: "pct" },
    { label: "% up", value: s.up_pct, fmt: "pct" },
    { label: "Max up", value: s.max_up, fmt: "pct_signed" },
    { label: "Max down", value: s.max_down, fmt: "pct_signed" },
    { label: "Avg |gap|", value: s.avg_abs_gap, fmt: "pct" },
    { label: "Avg pre-drift 5d", value: s.avg_pre_drift_5, fmt: "pct_signed" },
    { label: "Avg post-drift 5d", value: s.avg_post_drift_5, fmt: "pct_signed" },
    { label: "Avg post-drift 20d", value: s.avg_post_drift_20, fmt: "pct_signed" },
  ];
}

function mean(xs: (number | null)[]): number | null {
  const v = xs.filter((x): x is number => x !== null);
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
}

function Charts({ events }: { events: EarningsEventRow[] }) {
  const p = usePalette();
  const ev = useMemo(() => events.slice(0, 16).reverse(), [events]);
  const cats = ev.map((e) => e.fiscal_quarter ?? e.date);
  const drift: [string, keyof EarningsEventRow][] = [["Pre 10d", "pre_drift_10"], ["Pre 5d", "pre_drift_5"], ["Post 1d", "post_drift_1"], ["Post 5d", "post_drift_5"], ["Post 10d", "post_drift_10"], ["Post 20d", "post_drift_20"]];
  return (
    <div className="grid-3">
      <Card title="Reaction vs implied move" subtitle="|move| on reaction day vs straddle-implied">
        <BarChart categories={cats} yFmt="pct" height={240} series={[
          { name: "|Move|", data: ev.map((e) => (e.move_pct === null ? null : Math.abs(e.move_pct))), color: p.series[0] },
          { name: "Implied", data: ev.map((e) => e.implied_move_pct), color: p.series[3] },
        ]} />
      </Card>
      <Card title="EPS estimate vs actual">
        <BarChart categories={cats} yFmt="num2" height={240} series={[
          { name: "Estimate", data: ev.map((e) => e.eps_estimate), color: p.muted },
          { name: "Actual", data: ev.map((e) => e.eps_actual), color: p.series[2] },
        ]} />
      </Card>
      <Card title="Average drift around earnings" subtitle={`over ${events.length} events`}>
        <BarChart categories={drift.map((d) => d[0])} yFmt="pct_signed" height={240} signColors
          series={[{ name: "Avg drift", data: drift.map(([, k]) => mean(events.map((e) => e[k] as number | null))) }]} />
      </Card>
    </div>
  );
}

function StrategyPanel({ sym }: { sym: string }) {
  const [lookback, setLookback] = useState(12);
  const [entry, setEntry] = useState(-1);
  const [exit, setExit] = useState(1);
  const [strategy, setStrategy] = useState<string | null>(null);
  const params = { lookback, entry, exit };
  const res = useApi(() => api.earningsStrategies(sym, params), [sym, lookback, entry, exit]);
  const trades = useApi(strategy ? () => api.earningsStrategyTrades(sym, strategy, params) : null, [sym, strategy, lookback, entry, exit]);
  const label = res.data?.rows.find((r) => r.strategy === strategy)?.label ?? strategy;
  return (
    <Card title="Earnings option strategies (backtest)" subtitle="Entry/exit at the close, relative to the reaction day. Mid prices; model-priced trades are labelled.">
      <div className="toolbar">
        <label className="field-inline">Entry
          <select value={entry} onChange={(e) => setEntry(Number(e.target.value))}>{ENTRY.map((n) => <option key={n} value={n}>{n} day{n === -1 ? "" : "s"} (close)</option>)}</select>
        </label>
        <label className="field-inline">Exit
          <select value={exit} onChange={(e) => setExit(Number(e.target.value))}>{EXIT.map((n) => <option key={n} value={n}>+{n} day{n === 1 ? "" : "s"}</option>)}</select>
        </label>
        <label className="field-inline">Lookback
          <Segmented label="Lookback" value={lookback} onChange={setLookback} options={LOOKBACK.map((n) => ({ value: n, label: `${n}` }))} />
        </label>
        <span className="muted small">Click a strategy to list its trades.</span>
      </div>
      <Async state={res}>{(r) => (
        <>
          <DataTable columns={STRAT_COLUMNS} rows={r.rows as unknown as ReportRow[]} csvName={`${sym}-earnings-strategies`} defaultSort={{ key: "avg_return", dir: "desc" }}
            onRowClick={(row) => setStrategy(String(row.strategy))} rowClass={(row) => (row.strategy === strategy ? "row-selected" : undefined)} maxHeight={420} />
          {r.notes.length > 0 && <div className="top-gap"><InfoBox title="Model pricing:">{r.notes.join(" ")}</InfoBox></div>}
        </>
      )}</Async>
      {strategy && (
        <div className="top-gap">
          <h4 className="sub-title">Trades · {label} <button className="link-btn" onClick={() => setStrategy(null)}>close</button></h4>
          <Async state={trades}>{(t) => <DataTable columns={TRADE_COLUMNS} rows={tradesAsRows(t)} rowLink={false} csvName={`${sym}-${strategy}-trades`}
            emptyState="No trades for this strategy: none of the past events had the prices it needs." />}</Async>
        </div>
      )}
    </Card>
  );
}

function EarningsBody({ e, sym }: { e: Earnings; sym: string }) {
  const [scope, setScope] = useState<"recent" | "all">("recent");
  const n = e.next;
  if (e.unavailable) return <InfoBox title="Not available:">{e.unavailable}</InfoBox>;
  return (
    <div className="stack">
      <Card title="Next earnings">
        {n ? <StatGrid items={[
          { label: "Date", value: n.date, fmt: "date", sub: <>{nextEarningsSub(n)} <SourceBadge source={n.source} /></> },
          { label: "Implied move", value: n.implied_move_pct, fmt: "pct", hint: "No stored straddle covering the event.", sub: n.expiry_used ? `exp ${n.expiry_used}` : undefined },
          { label: "Excess implied", value: n.excess_implied_move_pct, fmt: "pct", title: "Event-day move implied beyond the normal daily volatility (term-structure method)" },
          { label: "Avg |move| (hist)", value: n.avg_abs_move_pct, fmt: "pct" },
          { label: "Implied / hist", value: n.implied_vs_hist_ratio, fmt: "ratio" },
        ]} /> : <div className="muted small">{noNextEarningsText(sym, e.next_missing)}</div>}
      </Card>
      <Card title="Reaction statistics" actions={<Segmented label="Scope" value={scope} onChange={setScope} options={[{ value: "recent", label: "Last 12" }, { value: "all", label: "All" }]} />}>
        <StatGrid items={statItems(scope === "recent" ? e.stats : e.stats_all)} />
      </Card>
      {e.events.length ? <Charts events={e.events} /> : <InfoBox>No past earnings events found.</InfoBox>}
      <Card title="Earnings history">
        <DataTable columns={EVENT_COLUMNS} rows={e.events.map((x) => ({ ...x, timing_label: x.timing_label ?? (x.timing === "unknown" ? "n/a" : x.timing.toUpperCase()) })) as unknown as ReportRow[]}
          rowLink={false} csvName={`${sym}-earnings`} maxHeight={480} emptyState="No past earnings events with price history are stored." />
      </Card>
      <StrategyPanel sym={sym} />
    </div>
  );
}

export default function EarningsTab() {
  const sym = useSym();
  const st = useApi(() => api.earnings(sym), [sym]);
  return <Async state={st}>{(e) => <EarningsBody e={e} sym={sym} />}</Async>;
}
