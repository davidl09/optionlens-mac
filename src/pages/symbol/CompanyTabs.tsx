// Company group: fundamentals, insiders, news, about.
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import type { ColumnDef, FinTable, ReportRow } from "../../api/types";
import { useApi } from "../../api/useApi";
import { DataTable } from "../../components/DataTable";
import { StatGrid, StatValue } from "../../components/StatGrid";
import { AsOf, Async, Card, Empty, NullValue, Segmented } from "../../components/ui";
import { fmtDateTime } from "../../lib/format";
import { useSym } from "./shared";

function FinTableView({ t }: { t: FinTable | null }) {
  if (!t) return <Empty>Not provided by the source for this symbol.</Empty>;
  return (
    <div className="dt-scroll">
      <table className="dt fin-table">
        <thead><tr><th>{t.title}</th>{t.periods.map((p) => <th key={p} className="num">{p}</th>)}</tr></thead>
        <tbody>
          {t.rows.map((r) => (
            <tr key={r.label}><td>{r.label}</td>{r.values.map((v, i) => <td key={i} className="num">{v ?? <NullValue />}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function FundamentalsTab() {
  const sym = useSym();
  const [period, setPeriod] = useState<"annual" | "quarterly">("annual");
  const [statement, setStatement] = useState<"income" | "balance" | "cashflow" | "ratios">("income");
  const st = useApi(() => api.fundamentals(sym, period), [sym, period]);
  const peers = useApi(() => api.peers(sym), [sym]);
  return (
    <div className="stack">
      <Async state={st}>{(f) => (
        <>
          <Card title="Key statistics" subtitle={<AsOf ts={f.fetched_at} label="fetched" />}>
            <StatGrid items={[
              ...f.key_stats.map((k) => ({ label: k.label, value: k.value, fmt: "text" as const })),
              { label: "Payback period", node: <StatValue value={f.payback_years} kind="num" hint="Needs positive trailing free cash flow." />, sub: "market cap ÷ trailing FCF (years)" },
            ]} />
          </Card>
          <Card title="Financial statements" actions={
            <div className="toolbar">
              <Segmented label="Statement" value={statement} onChange={setStatement} options={[{ value: "income", label: "Income" }, { value: "balance", label: "Balance sheet" }, { value: "cashflow", label: "Cash flow" }, { value: "ratios", label: "Ratios" }]} />
              <Segmented label="Period" value={period} onChange={setPeriod} options={[{ value: "annual", label: "Annual" }, { value: "quarterly", label: "Quarterly" }]} />
            </div>
          }>
            <FinTableView t={f[statement]} />
          </Card>
        </>
      )}</Async>
      <Card title="Peer comparison" subtitle="same industry, top 10 by market cap">
        <Async state={peers}>{(r) => <DataTable columns={r.columns} rows={r.rows} csvName={`${sym}-peers`} minimal />}</Async>
      </Card>
    </div>
  );
}

const INSIDER_COLUMNS: ColumnDef[] = [
  { key: "insider", label: "Insider", fmt: "text" },
  { key: "relation", label: "Relation", fmt: "text" },
  { key: "date", label: "Date", fmt: "date" },
  { key: "transaction", label: "Transaction", fmt: "badge" },
  { key: "shares", label: "Shares", fmt: "int" },
  { key: "price", label: "Price", fmt: "money" },
  { key: "value", label: "Value", fmt: "bigmoney" },
  { key: "shares_held", label: "Shares held", fmt: "int" },
];

export function InsidersTab() {
  const sym = useSym();
  const st = useApi(() => api.insiders(sym), [sym]);
  return (
    <Async state={st}>{(d) => (
      <div className="stack">
        <Card title="Insider activity summary">
          <DataTable minimal rowLink={false} rows={d.summary as unknown as ReportRow[]} columns={[{ key: "label", label: "", fmt: "text" }, { key: "m3", label: "3 months", fmt: "text" }, { key: "m12", label: "12 months", fmt: "text" }]} />
        </Card>
        <Card title="Transactions">
          <DataTable columns={INSIDER_COLUMNS} rows={d.trades as unknown as ReportRow[]} rowLink={false} csvName={`${sym}-insiders`} />
        </Card>
      </div>
    )}</Async>
  );
}

export function NewsTab() {
  const sym = useSym();
  const st = useApi(() => api.news(sym), [sym]);
  return (
    <Async state={st}>{(items) => (
      <Card title={`News · ${sym}`}>
        {items.length ? (
          <ul className="news-list">
            {items.map((n, i) => (
              <li key={`${n.url}-${i}`}>
                <a href={n.url} target="_blank" rel="noreferrer noopener">{n.title}</a>
                <div className="news-meta">{n.source ?? "—"}{n.published ? ` · ${/T/.test(n.published) ? fmtDateTime(n.published) : n.published}` : ""}</div>
                {n.summary && <p className="news-summary">{n.summary}</p>}
              </li>
            ))}
          </ul>
        ) : <Empty>No recent headlines.</Empty>}
      </Card>
    )}</Async>
  );
}

export function AboutTab() {
  const sym = useSym();
  const st = useApi(() => api.profile(sym), [sym]);
  return (
    <Async state={st}>{(p) => (
      <div className="grid-main-side">
        <Card title={p.name}>
          <StatGrid cols={4} items={[
            { label: "Sector", value: p.sector, fmt: "text" }, { label: "Industry", value: p.industry, fmt: "text" },
            { label: "Exchange", value: p.exchange, fmt: "text" },
            { label: "Website", node: p.website ? <a href={p.website} target="_blank" rel="noreferrer noopener">{p.website.replace(/^https?:\/\//, "")}</a> : <NullValue /> },
          ]} />
          <p className="prose">{p.description ?? "No company description available."}</p>
        </Card>
        <Card title="Similar stocks" subtitle="industry peers">
          {p.peers.length ? (
            <ul className="peer-list">
              {p.peers.map((x) => <li key={x.symbol}><Link to={`/s/${x.symbol}`}><b>{x.symbol}</b></Link> <span className="muted">{x.name}</span></li>)}
            </ul>
          ) : <Empty>No peers found.</Empty>}
        </Card>
      </div>
    )}</Async>
  );
}
