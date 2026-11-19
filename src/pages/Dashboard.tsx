// Market dashboard (GET /api/market/overview) with compact tables sized from the report definitions.
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { ColumnDef, MarketOverview, ReportDef, ReportRow, Status } from "../api/types";
import { useApi } from "../api/useApi";
import { LineChart, LineSeries } from "../components/charts/Charts";
import { usePalette } from "../components/charts/EChart";
import { DataTable, inferColumns } from "../components/DataTable";
import { StatValue } from "../components/StatGrid";
import { AsOf, Async, Card, Empty, Segmented } from "../components/ui";
import { fmt, plural, signClass } from "../lib/format";

function Sparkline({ values, width = 96, height = 30 }: { values: number[]; width?: number; height?: number }) {
  if (values.length < 2) return <svg width={width} height={height} aria-hidden="true" />;
  const min = Math.min(...values), max = Math.max(...values);
  const span = max - min || 1;
  const pts = values.map((v, i) => `${((i / (values.length - 1)) * width).toFixed(1)},${(height - 2 - ((v - min) / span) * (height - 4)).toFixed(1)}`).join(" ");
  const up = values[values.length - 1] >= values[0];
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className={`spark ${up ? "pos" : "neg"}`} aria-hidden="true">
      <polyline points={pts} fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

/** Pick a few columns: preferred keys first, then the report's first visible columns. */
function compactColumns(rows: ReportRow[], def: ReportDef | undefined, preferred: string[], max = 5): ColumnDef[] {
  const all = def?.columns ?? inferColumns(rows);
  const present = rows.length ? all.filter((c) => c.key in rows[0]) : all;
  const pool = present.length ? present : all;
  const out: ColumnDef[] = [];
  for (const k of preferred) { const c = pool.find((x) => x.key === k); if (c && out.length < max) out.push(c); }
  for (const c of pool) if (out.length < max && !out.includes(c) && !c.default_hidden) out.push(c);
  return out;
}

function MiniTable({ title, rows, def, preferred, link, max, empty }: { title: string; rows: ReportRow[]; def?: ReportDef; preferred: string[]; link?: string; max?: number; empty?: string }) {
  const cols = useMemo(() => compactColumns(rows, def, preferred, max), [rows, def, preferred, max]);
  return (
    <Card title={title} actions={link ? <Link to={link} className="small">Full report →</Link> : undefined} className="mini-table">
      {rows.length ? <DataTable minimal columns={cols} rows={rows} /> : <Empty>{empty ?? "Nothing to show right now."}</Empty>}
    </Card>
  );
}

function IndexCards({ m }: { m: MarketOverview }) {
  return (
    <div className="index-cards">
      {m.indices.map((c) => (
        <Link key={c.symbol} to={`/s/${c.symbol}`} className="index-card">
          <div className="ic-top"><b>{c.symbol}</b><span className="muted ic-name">{c.name}</span></div>
          <div className="ic-mid">
            <span className="ic-price"><StatValue value={c.price} kind="num2" /></span>
            <span className={signClass(c.change_pct)}><StatValue value={c.change_pct} kind="pct_signed" /></span>
          </div>
          <div className="ic-bot"><Sparkline values={c.spark} /><span className="muted small nowrap">IV30 {fmt(c.iv30, "vol")}</span></div>
        </Link>
      ))}
    </div>
  );
}

function VixCard({ m }: { m: MarketOverview }) {
  const p = usePalette();
  const v = Object.fromEntries(m.vix_term.map((x) => [x.tenor, x.value]));
  const shape = v.VIX != null && v.VIX3M != null ? (v.VIX < v.VIX3M ? "Contango" : "Backwardation") : null;
  const series = useMemo<LineSeries[]>(() => [
    { name: "VIX9D", data: m.vix_history.map((h) => [h.date, h.vix9d]), color: p.series[3] },
    { name: "VIX", data: m.vix_history.map((h) => [h.date, h.vix]), color: p.series[0] },
    { name: "VIX3M", data: m.vix_history.map((h) => [h.date, h.vix3m]), color: p.series[2] },
    { name: "VIX6M", data: m.vix_history.map((h) => [h.date, h.vix6m]), color: p.series[6] },
  ], [m.vix_history, p]);
  return (
    <Card title="VIX term structure" actions={shape && <span className={`badge ${shape === "Contango" ? "pos" : "neg"}`} title="VIX vs VIX3M">{shape}</span>}>
      <div className="vix-term">
        {m.vix_term.map((x) => (
          <div key={x.tenor} className="vix-pt">
            <span className="muted small">{x.tenor}</span>
            <b><StatValue value={x.value} kind="num2" /></b>
            <span className={`small ${signClass(x.change)}`}>{x.change !== null ? `${x.change > 0 ? "+" : ""}${fmt(x.change, "num2")}` : ""}</span>
          </div>
        ))}
      </div>
      <LineChart series={series} yFmt="num2" height={200} />
    </Card>
  );
}

function SectorStrip({ m }: { m: MarketOverview }) {
  const [span, setSpan] = useState<"change_pct" | "change_5d" | "change_1m">("change_pct");
  const max = Math.max(0.005, ...m.sectors.map((s) => Math.abs(s[span] ?? 0)));
  return (
    <Card title="Sector ETFs" actions={<Segmented label="Period" value={span} onChange={setSpan} options={[{ value: "change_pct", label: "1D" }, { value: "change_5d", label: "5D" }, { value: "change_1m", label: "1M" }]} />}>
      <div className="heat-strip">
        {m.sectors.map((s) => {
          const v = s[span];
          const strength = v === null ? 0 : Math.round((Math.min(1, Math.abs(v) / max)) * 70);
          const bg = v === null ? "var(--panel-2)" : `color-mix(in srgb, var(${v >= 0 ? "--up" : "--down"}) ${strength}%, var(--panel-2))`;
          return (
            <Link key={s.symbol} to={`/s/${s.symbol}`} className="heat-tile" style={{ background: bg }} title={s.name}>
              <b>{s.symbol}</b><span>{fmt(v, "pct_signed")}</span><small>{s.name}</small>
            </Link>
          );
        })}
      </div>
    </Card>
  );
}

function Freshness({ m, status }: { m: MarketOverview; status: Status | null }) {
  return (
    <Card title="Data freshness" actions={<Link to="/status" className="small">Status →</Link>}>
      <div className="fresh">
        <div><span className="muted">Overview</span> <AsOf ts={m.asof} /></div>
        {status && <>
          <div><span className="muted">Market</span> <span className={`badge ${status.market_open ? "pos" : "neutral"}`}>{status.market_open ? "open" : "closed"}</span></div>
          <div><span className="muted">Last sweep</span> <AsOf ts={status.last_sweep_at} label="" /></div>
          <div><span className="muted">IV history</span> <b>{status.iv_history_days}</b> {plural(status.iv_history_days, "day")} · <span className="muted">EOD chains</span> <b>{status.option_eod_days}</b> {plural(status.option_eod_days, "day")}</div>
        </>}
      </div>
    </Card>
  );
}

function DashboardBody({ m, defs, status }: { m: MarketOverview; defs: ReportDef[]; status: Status | null }) {
  const d = (id: string) => defs.find((x) => x.id === id);
  return (
    <div className="stack">
      <IndexCards m={m} />
      <SectorStrip m={m} />
      <div className="grid-main-side">
        <MiniTable title="Unusual option volume" rows={m.unusual_options} empty={m.empty_notes.unusual_options} def={d("unusual-option-volume")} max={7}
          preferred={["symbol", "option_volume", "rel_volume", "vol_oi", "pct_call", "sentiment", "iv30"]} link="/reports/unusual-option-volume" />
        <VixCard m={m} />
      </div>
      <div className="grid-3">
        <MiniTable title="Top gainers" rows={m.gainers} empty={m.empty_notes.gainers} def={d("market-movers")} preferred={["symbol", "price", "change_pct", "volume", "rel_volume"]} link="/reports/market-movers?side=gainers" />
        <MiniTable title="Top losers" rows={m.losers} empty={m.empty_notes.losers} def={d("market-movers")} preferred={["symbol", "price", "change_pct", "volume", "rel_volume"]} link="/reports/market-movers?side=losers" />
        <MiniTable title="IV movers" rows={m.iv_movers} empty={m.empty_notes.iv_movers} def={d("iv-movers")} preferred={["symbol", "iv30", "iv30_chg_1d", "iv30_chg_pct", "iv_rank"]} link="/reports/iv-movers" />
      </div>
      <div className="grid-3">
        <MiniTable title="Earnings today" rows={m.earnings_today} empty={m.empty_notes.earnings_today} def={d("earnings-calendar")} preferred={["symbol", "time", "implied_move", "avg_abs_move", "iv_rank"]} link="/reports/earnings-calendar" />
        <MiniTable title="Earnings next session" rows={m.earnings_tomorrow} empty={m.empty_notes.earnings_tomorrow} def={d("earnings-calendar")} preferred={["symbol", "time", "implied_move", "avg_abs_move", "iv_rank"]} />
        <Freshness m={m} status={status} />
      </div>
      <MiniTable title="Watchlist" rows={m.watchlist} empty={m.empty_notes.watchlist} preferred={["symbol", "name", "price", "change_pct", "iv30", "iv_rank", "next_earnings"]} max={8} link="/watchlists" />
    </div>
  );
}

export default function Dashboard() {
  const st = useApi(api.marketOverview, []);
  const defs = useApi(api.reportDefs, []);
  const status = useApi(api.status, []);
  return (
    <div className="stack">
      <div className="page-head"><h1 className="page-title">Market dashboard</h1>{st.data && <AsOf ts={st.data.asof} />}</div>
      <Async state={st}>{(m) => <DashboardBody m={m} defs={defs.data ?? []} status={status.data} />}</Async>
    </div>
  );
}
