// Company group: fundamentals, insiders, news, about.
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import type { ColumnDef, FinTable, Insiders, KeyStats, ReportRow } from "../../api/types";
import { useApi } from "../../api/useApi";
import { DataTable } from "../../components/DataTable";
import { StatGrid, StatValue } from "../../components/StatGrid";
import { AsOf, Async, Card, Empty, InfoBox, NullValue, Segmented, SourceBadge } from "../../components/ui";
import { fmtDateTime, fundamentalText } from "../../lib/format";
import { insiderText, sellSplit } from "../../lib/insiders";
import { useSym } from "./shared";

function FinTableView({ t }: { t: FinTable | null }) {
  if (!t) return <Empty>Not provided by the source for this symbol.</Empty>;
  return (
    <div className="dt-scroll">
      <table className="dt fin-table">
        <thead><tr><th>{t.title}</th>{t.periods.map((p) => <th key={p} className="num">{p}</th>)}</tr></thead>
        <tbody>
          {t.rows.map((r) => (
            <tr key={r.label}><td>{r.label}</td>{r.values.map((v, i) => <td key={i} className="num">{v !== null ? fundamentalText(r.label, v) : <NullValue hint="Not reported for this period." />}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** OptionLens's key figures (the symbol header and Summary show the same ones). */
function KeyStatsCard({ ks }: { ks: KeyStats }) {
  const v = (label: string, k: keyof KeyStats, kind: "num2" | "money" | "bigmoney" | "int" | "pct" | "text") => ({
    label, node: <span><StatValue value={ks[k]} kind={kind} hint="Not reported for this symbol yet." /> <SourceBadge source={ks.sources[k]} /></span>,
  });
  return (
    <Card title="Key figures" subtitle={<>OptionLens · Questrade's daily symbol info{ks.asof ? ` of ${ks.asof}` : ""}</>}>
      <StatGrid items={[
        v("Market cap", "market_cap", "bigmoney"), v("P/E", "pe", "num2"), v("EPS", "eps", "money"),
        v("Shares outstanding", "shares_outstanding", "bigmoney"), v("52w high", "high_52w", "money"), v("52w low", "low_52w", "money"),
        v("Avg volume 20d", "avg_volume_20d", "int"), v("Avg volume 3m", "avg_volume_3m", "int"), v("Dividend yield", "dividend_yield", "pct"),
        v("Sector (Questrade)", "sector", "text"), v("Industry group", "industry_group", "text"), v("Industry sub-group", "industry_subgroup", "text"),
      ]} />
    </Card>
  );
}

export function FundamentalsTab() {
  const sym = useSym();
  const [period, setPeriod] = useState<"annual" | "quarterly">("annual");
  const [statement, setStatement] = useState<"income" | "balance" | "cashflow" | "ratios">("income");
  const st = useApi(() => api.fundamentals(sym, period), [sym, period]);
  const peers = useApi(() => api.peers(sym), [sym]);
  const ks = useApi(() => api.keyStats(sym), [sym]);
  return (
    <div className="stack">
      <Async state={ks}>{(k) => <KeyStatsCard ks={k} />}</Async>
      <Async state={st}>{(f) => f.unavailable ? <InfoBox title="Not available:">{f.unavailable}</InfoBox> : (
        <>
          <Card title="Key statistics" subtitle={<>Nasdaq company data · <AsOf ts={f.fetched_at} label="fetched" /></>}>
            <p className="muted small">Nasdaq's other figures; market cap, P/E, EPS, yield, 52-week range, average volume and dividend dates are the key figures above (one value on every page).</p>
            <StatGrid items={[
              ...f.key_stats.map((k) => ({ label: k.label, value: k.value !== null ? fundamentalText(k.label, k.value) : null, fmt: "text" as const, hint: "Not provided by Nasdaq for this symbol." })),
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
      <Card title="Peer comparison" subtitle="same industry, top 10 by market cap · OptionLens figures">
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
    <Async state={st}>{(d) => {
      const text = insiderText(d.source);
      return (
        <div className="stack">
          <Card title="Insider activity summary" subtitle={text.summary}>
            <DataTable minimal rowLink={false} rows={d.summary.map((r) => ({ ...r, label: /^number of sells$/i.test(r.label) ? "Number of sells (all kinds)" : r.label })) as unknown as ReportRow[]}
              columns={[{ key: "label", label: "", fmt: "text" }, { key: "m3", label: "3 months", fmt: "text" }, { key: "m12", label: "12 months", fmt: "text" }]}
              emptyState={text.noSummary} />
            <SellSplitLine trades={d.trades} />
          </Card>
          <Card title="Transactions" subtitle={text.trades}>
            <DataTable columns={INSIDER_COLUMNS} rows={d.trades as unknown as ReportRow[]} rowLink={false} csvName={`${sym}-insiders`}
              emptyState={text.noTrades} />
          </Card>
        </div>
      );
    }}</Async>
  );
}

/** Automatic (10b5-1 plan) vs open-market sells among the listed transactions. */
function SellSplitLine({ trades }: { trades: Insiders["trades"] }) {
  const s = sellSplit(trades);
  if (!s.total) return null;
  return (
    <p className="small top-gap" title="Automatic Sell = sold under a pre-arranged Rule 10b5-1 trading plan; Sell = an open-market sale the insider chose to make.">
      Of the {s.total} sells listed below: <b>{s.automatic}</b> automatic (10b5-1 plan), <b>{s.openMarket}</b> open market{s.total - s.automatic - s.openMarket > 0 ? `, ${s.total - s.automatic - s.openMarket} other` : ""}.
    </p>
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
