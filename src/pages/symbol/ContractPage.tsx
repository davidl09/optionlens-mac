// Single-contract history (from stored end-of-day chains).
import { useMemo } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../../api/client";
import type { ColumnDef, ContractHistory, ReportRow } from "../../api/types";
import { useApi } from "../../api/useApi";
import { BarChart, LineChart, LineSeries } from "../../components/charts/Charts";
import { usePalette } from "../../components/charts/EChart";
import { DataTable } from "../../components/DataTable";
import { StatGrid } from "../../components/StatGrid";
import { Async, Card, InfoBox } from "../../components/ui";
import { fmt } from "../../lib/format";
import { useSym } from "./shared";

const HIST_COLUMNS: ColumnDef[] = [
  { key: "date", label: "Date", fmt: "date" },
  { key: "bid", label: "Bid", fmt: "money" },
  { key: "ask", label: "Ask", fmt: "money" },
  { key: "mid", label: "Mid", fmt: "money" },
  { key: "iv", label: "IV", fmt: "vol" },
  { key: "volume", label: "Volume", fmt: "int" },
  { key: "oi", label: "OI", fmt: "int" },
  { key: "delta", label: "Δ", fmt: "num2" },
  { key: "underlying", label: "Underlying", fmt: "money" },
];

function ContractBody({ c }: { c: ContractHistory }) {
  const p = usePalette();
  const h = useMemo(() => [...c.history].sort((a, b) => a.date.localeCompare(b.date)), [c.history]);
  const line = (name: string, key: "mid" | "iv" | "delta" | "underlying", color: string): LineSeries[] => [{ name, data: h.map((x) => [x.date, x[key]]), color }];
  const l = c.latest;
  return (
    <div className="stack">
      <Card title="Latest">
        {l ? <StatGrid items={[
          { label: "Bid", value: l.bid, fmt: "money" }, { label: "Ask", value: l.ask, fmt: "money" }, { label: "Mid", value: l.mid, fmt: "money" },
          { label: "Last", value: l.last, fmt: "money" }, { label: "Change", value: l.change, fmt: "num2", signed: true },
          { label: "Volume", value: l.volume, fmt: "int" }, { label: "Open interest", value: l.oi, fmt: "int" }, { label: "IV", value: l.iv, fmt: "vol" },
          { label: "Delta", value: l.delta, fmt: "num2" }, { label: "Gamma", value: l.gamma, fmt: "num2" }, { label: "Theta", value: l.theta, fmt: "num2" },
          { label: "Vega", value: l.vega, fmt: "num2" },
        ]} /> : <div className="muted small">Not in the current chain (expired or delisted).</div>}
      </Card>
      {h.length ? (
        <>
          <div className="grid-2">
            <Card title="Mid price"><LineChart series={line("Mid", "mid", p.series[0])} yFmt="money" height={200} /></Card>
            <Card title="Implied volatility"><LineChart series={line("IV", "iv", p.series[1])} yFmt="vol" height={200} /></Card>
            <Card title="Open interest & volume">
              <BarChart categories={h.map((x) => x.date)} yFmt="int" height={200}
                series={[{ name: "OI", data: h.map((x) => x.oi), color: p.series[0] }, { name: "Volume", data: h.map((x) => x.volume), color: p.series[2] }]} />
            </Card>
            <Card title="Delta"><LineChart series={line("Delta", "delta", p.series[6])} yFmt="num2" height={200} /></Card>
          </div>
          <Card title="Underlying"><LineChart series={line("Underlying", "underlying", p.series[3])} yFmt="money" height={180} /></Card>
        </>
      ) : <InfoBox>No stored history yet — contract history starts on the day the collector first stored end-of-day chains.</InfoBox>}
      <Card title="Daily history">
        <DataTable columns={HIST_COLUMNS} rows={[...c.history].sort((a, b) => b.date.localeCompare(a.date)) as unknown as ReportRow[]} rowLink={false} csvName={c.osi} />
      </Card>
    </div>
  );
}

export default function ContractPage() {
  const sym = useSym();
  const { osi = "" } = useParams();
  const st = useApi(() => api.contract(sym, osi), [sym, osi]);
  return (
    <div className="stack">
      <div className="breadcrumb"><Link to={`/s/${sym}/chain`}>← {sym} chain</Link></div>
      <Async state={st}>{(c) => (
        <>
          <h2 className="page-title">{c.symbol} {c.expiry} {fmt(c.strike, "num")} {c.right === "C" ? "Call" : "Put"} <small className="muted mono">{c.osi}</small></h2>
          <ContractBody c={c} />
        </>
      )}</Async>
    </div>
  );
}
