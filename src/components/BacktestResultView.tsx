// Shared rendering of a BacktestResult (stock, options and straddle backtests).
import { useMemo } from "react";
import type { BacktestResult, BacktestTrade, ColumnDef, ReportRow } from "../api/types";
import { fmt } from "../lib/format";
import { BarChart, LineChart, LineSeries } from "./charts/Charts";
import { usePalette } from "./charts/EChart";
import { DataTable } from "./DataTable";
import { StatGrid } from "./StatGrid";
import { Card, EmptyNotice, NotesBox } from "./ui";
import { useMoney } from "./Money";

export const TRADE_COLUMNS: ColumnDef[] = [
  { key: "symbol", label: "Symbol", fmt: "symbol" },
  { key: "entry_date", label: "Entry", fmt: "date" },
  { key: "exit_date", label: "Exit", fmt: "date" },
  { key: "legs", label: "Legs", fmt: "text" },
  { key: "entry_value", label: "Entry value", fmt: "money" },
  { key: "exit_value", label: "Exit value", fmt: "money" },
  { key: "pnl", label: "P/L", fmt: "money" },
  { key: "ret", label: "Return", fmt: "pct_signed" },
  { key: "pricing", label: "Pricing", fmt: "badge", description: "real = stored EOD option prices; model = Black-Scholes with an IV proxy" },
  { key: "note", label: "Note", fmt: "text", default_hidden: true },
];

export function tradesAsRows(trades: BacktestTrade[]): ReportRow[] {
  return trades as unknown as ReportRow[];
}

export function BacktestResultView({ result, csvName = "backtest-trades" }: { result: BacktestResult; csvName?: string }) {
  const p = usePalette();
  const money = useMoney();
  const s = result.summary;
  const equity = useMemo<LineSeries[]>(() => {
    const out: LineSeries[] = [{ name: "Strategy", data: result.equity.map((e) => [e.date, e.value]), color: p.series[0] }];
    if (result.benchmark) out.push({ name: "Buy & hold", data: result.benchmark.map((e) => [e.date, e.value]), color: p.muted, dashed: true });
    return out;
  }, [result, p]);
  const hist = useMemo(() => ({
    cats: result.histogram.map((h) => `${fmt(h.bin_start, "pct")}…${fmt(h.bin_end, "pct")}`),
    data: result.histogram.map((h) => h.count),
  }), [result.histogram]);

  return (
    <div className="stack">
      <NotesBox title="Notes:" notes={result.notes} />
      <Card title="Summary" subtitle={`${s.real_count} real-priced · ${s.model_count} model-priced`}>
        <StatGrid items={[
          { label: "Trades", value: s.count, fmt: "int" },
          { label: "Win rate", value: s.win_rate, fmt: "pct" },
          { label: "Avg return", value: s.avg_return, fmt: "pct_signed" },
          { label: "Median return", value: s.median_return, fmt: "pct_signed" },
          { label: "Total return", value: s.total_return, fmt: "pct_signed", title: "Stock backtests compound trade returns; option backtests do not (see notes)" },
          { label: "CAGR", value: s.cagr, fmt: "pct_signed" },
          { label: "Sharpe", value: s.sharpe, fmt: "num2" },
          { label: "Max drawdown", value: s.max_drawdown, fmt: "pct_signed" },
          { label: "Avg hold (days)", value: s.avg_hold_days, fmt: "num" },
          { label: "Total P/L", value: s.total_pnl, fmt: "money", signed: true, sub: s.starting_capital != null ? `on ${fmt(s.starting_capital, "money", money)} starting capital` : undefined },
        ]} />
      </Card>
      <div className="grid-2">
        <Card title="Equity curve">
          {result.equity.length ? <LineChart series={equity} yFmt="money" height={260} /> : <EmptyNotice>No trades: the entry rule never triggered in this period.</EmptyNotice>}
        </Card>
        <Card title="Return distribution">
          {hist.data.length ? <BarChart categories={hist.cats} series={[{ name: "Trades", data: hist.data }]} yFmt="int" height={260} /> : <EmptyNotice>No trades: the entry rule never triggered in this period.</EmptyNotice>}
        </Card>
      </div>
      <Card title="Trades">
        <DataTable columns={TRADE_COLUMNS} rows={tradesAsRows(result.trades)} csvName={csvName} rowLink={false} maxHeight={480} emptyState="No trades: the entry rule never triggered in this period." />
      </Card>
    </div>
  );
}
