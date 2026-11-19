// Options group: trade ideas, straddle.
import { useMemo, useState } from "react";
import { api } from "../../api/client";
import type { ColumnDef, ReportRow, TradeIdea } from "../../api/types";
import { useApi } from "../../api/useApi";
import { BacktestResultView } from "../../components/BacktestResultView";
import { BarChart } from "../../components/charts/Charts";
import { DataTable } from "../../components/DataTable";
import { Async, Card, InfoBox, Segmented } from "../../components/ui";
import { IDEA_COLUMNS, ideasAsRows, shortDate, useSym } from "./shared";
import { unlimitedCell } from "./SummaryTab";

type Family = "all" | TradeIdea["family"];
const FAMILIES: { value: Family; label: string }[] = [
  { value: "all", label: "All" }, { value: "vertical", label: "Verticals" }, { value: "condor_fly", label: "Condors / flies" },
  { value: "single", label: "Long call / put" }, { value: "straddle", label: "Straddles" }, { value: "calendar", label: "Calendars" },
  { value: "seasonality", label: "Seasonality" },
];

export function IdeasTab() {
  const sym = useSym();
  const st = useApi(() => api.ideas(sym), [sym]);
  const [family, setFamily] = useState<Family>("all");
  const pick = (xs: TradeIdea[]) => ideasAsRows(family === "all" ? xs : xs.filter((x) => x.family === family));
  return (
    <Async state={st}>{(t) => (
      <div className="stack">
        <div className="toolbar"><Segmented label="Strategy family" value={family} onChange={setFamily} options={FAMILIES} /></div>
        <InfoBox>Ideas are drawn from the strategy screeners for {sym}. “Edge” = expected value under 30-day historical volatility ÷ max loss; “win rate” = probability of profit under implied volatility. Edge ideas need a probability of profit of at least 20%; win-rate ideas need a non-negative edge. These are model estimates, not recommendations.</InfoBox>
        <Card title="Ranked by edge (EV under HV ÷ risk)">
          <DataTable columns={IDEA_COLUMNS} rows={pick(t.by_edge)} rowLink={false} renderCell={unlimitedCell} csvName={`${sym}-ideas-edge`} />
        </Card>
        <Card title="Ranked by win rate (PoP)">
          <DataTable columns={IDEA_COLUMNS} rows={pick(t.by_win_rate)} rowLink={false} renderCell={unlimitedCell} csvName={`${sym}-ideas-pop`} />
        </Card>
      </div>
    )}</Async>
  );
}

const STRADDLE_COLUMNS: ColumnDef[] = [
  { key: "expiry", label: "Expiry", fmt: "date" },
  { key: "dte", label: "DTE", fmt: "int" },
  { key: "strike", label: "Strike", fmt: "num" },
  { key: "call_mid", label: "Call mid", fmt: "money" },
  { key: "put_mid", label: "Put mid", fmt: "money" },
  { key: "straddle", label: "Straddle", fmt: "money" },
  { key: "straddle_pct", label: "Straddle %", fmt: "pct" },
  { key: "lower_be", label: "Lower BE", fmt: "money" },
  { key: "upper_be", label: "Upper BE", fmt: "money" },
];

export function StraddleTab() {
  const sym = useSym();
  const st = useApi(() => api.straddle(sym), [sym]);
  return (
    <Async state={st}>{(s) => <StraddleBody rows={s.rows as unknown as ReportRow[]} raw={s} sym={sym} />}</Async>
  );
}

function StraddleBody({ rows, raw, sym }: { rows: ReportRow[]; raw: Awaited<ReturnType<typeof api.straddle>>; sym: string }) {
  const cats = useMemo(() => raw.rows.map((r) => shortDate(r.expiry)), [raw.rows]);
  return (
    <div className="stack">
      <div className="grid-main-side">
        <Card title="ATM straddle by expiry">
          <DataTable minimal columns={STRADDLE_COLUMNS} rows={rows} rowLink={false} maxHeight={380} />
        </Card>
        <Card title="Straddle % of spot">
          <BarChart categories={cats} series={[{ name: "Straddle %", data: raw.rows.map((r) => r.straddle_pct) }]} yFmt="pct" height={320} />
        </Card>
      </div>
      <h2 className="section-title">Historic 30-DTE straddle backtest <small className="muted">long ATM straddle held to expiry, last 2 years</small></h2>
      {raw.backtest ? <BacktestResultView result={raw.backtest} csvName={`${sym}-straddle-backtest`} /> : <InfoBox>Not enough price history to run the straddle backtest.</InfoBox>}
    </div>
  );
}
