// /status, /about, 404
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { ColumnDef, ReportRow } from "../api/types";
import { useApi } from "../api/useApi";
import { DataTable } from "../components/DataTable";
import { StatGrid } from "../components/StatGrid";
import { AsOf, Async, Card } from "../components/ui";
import { fmt } from "../lib/format";

const JOB_COLUMNS: ColumnDef[] = [
  { key: "job", label: "Job", fmt: "text" },
  { key: "started_at", label: "Started", fmt: "datetime" },
  { key: "finished_at", label: "Finished", fmt: "datetime" },
  { key: "state", label: "Result", fmt: "badge" },
  { key: "detail", label: "Detail", fmt: "text" },
];

export function StatusPage() {
  const st = useApi(api.status, []);
  return (
    <div className="stack">
      <div className="page-head"><h1 className="page-title">Data status</h1>{st.data && <AsOf ts={st.data.now} label="server time" />}</div>
      <Async state={st}>{(s) => {
        const jobs = s.jobs.map((j) => ({ ...j, state: j.ok === null ? (j.finished_at ? "unknown" : "running") : j.ok ? "ok" : "failed" })) as unknown as ReportRow[];
        const counts = Object.entries(s.row_counts).map(([table, rows]) => ({ table, rows })) as unknown as ReportRow[];
        return (
          <>
            <Card title="Collection">
              <StatGrid items={[
                { label: "Market", node: <span className={`badge ${s.market_open ? "pos" : "neutral"}`}>{s.market_open ? "open" : "closed"}</span> },
                { label: "Last sweep", value: s.last_sweep_at, fmt: "datetime", hint: "No sweep has run yet." },
                { label: "Next sweep", value: s.next_sweep_at, fmt: "datetime", hint: "None scheduled (market closed)." },
                { label: "Last EOD", value: s.last_eod_date, fmt: "date", hint: "End-of-day job has not run yet." },
                { label: "Universe", value: s.universe_size, fmt: "int" },
                { label: "Optionable", value: s.optionable_count, fmt: "int" },
                { label: "IV history", value: s.iv_history_days, fmt: "int", sub: "days in metrics" },
                { label: "EOD chains", value: s.option_eod_days, fmt: "int", sub: "days stored" },
                { label: "DB size", node: <span>{fmt(s.db_size_bytes / 1e6, "num")} MB</span> },
                { label: "Earnings backfill", node: <div className="progress" title={fmt(s.earnings_backfill_progress, "pct")}><div style={{ width: `${Math.round(s.earnings_backfill_progress * 100)}%` }} /><span>{fmt(s.earnings_backfill_progress, "pct")}</span></div> },
              ]} />
            </Card>
            <div className="grid-main-side">
              <Card title="Job runs"><DataTable columns={JOB_COLUMNS} rows={jobs} rowLink={false} csvName="job-runs" maxHeight={520} /></Card>
              <Card title="Row counts"><DataTable minimal rowLink={false} columns={[{ key: "table", label: "Table", fmt: "text" }, { key: "rows", label: "Rows", fmt: "int" }]} rows={counts} /></Card>
            </div>
          </>
        );
      }}</Async>
    </div>
  );
}

export function AboutPage() {
  return (
    <div className="stack about">
      <div className="page-head"><h1 className="page-title">About OptionLens</h1></div>
      <Card title="What this is">
        <p className="prose">OptionLens is a self-hosted options and stock analytics workstation for a single user. It collects free, delayed public market data on a schedule,
          stores it locally, and computes volatility, earnings, dividend and strategy analytics from that store. Nothing here is investment advice.</p>
      </Card>
      <div className="grid-2">
        <Card title="Where the data comes from">
          <ul className="prose-list">
            <li><b>CBOE delayed quotes</b> — option chains (bid/ask, volume, open interest, greeks) and underlying quotes, roughly 15 minutes behind; the VIX family of indices and their daily history.</li>
            <li><b>Nasdaq public endpoints</b> — daily price history, earnings and dividend calendars, dividend history, company profile, financial statements, insider filings, headlines and extended-hours quotes.</li>
            <li><b>U.S. Treasury</b> — the 13-week bill rate, used as the risk-free rate in option models.</li>
            <li>Everything else (IV rank, term structure, skew, expected moves, backtests…) is computed locally from those inputs.</li>
          </ul>
        </Card>
        <Card title="How to read the numbers">
          <ul className="prose-list">
            <li>A dash <span className="null-value">—</span> means a value is unknown, usually because not enough history has been collected yet. Hover it for the reason. Values are never filled in with guesses.</li>
            <li>Implied-volatility history (and therefore IV rank / percentile) begins on the first day the collector ran. IV rank needs at least 20 stored days.</li>
            <li>Historical option prices are only available from stored end-of-day chains. Where a result needs a price from before that, it is estimated with a Black-Scholes model and the row is labelled <span className="badge warn">model</span>; rows from stored prices count as real.</li>
            <li>The model's volatility input falls back from stored at-the-money IV, to stored 30-day IV, to recent historical volatility scaled by the symbol's typical implied/realized ratio.</li>
            <li>Quotes are delayed about 15 minutes; pages show an “as of” time.</li>
          </ul>
        </Card>
      </div>
      <Card title="Not available (no free source)">
        <ul className="prose-list cols-2">
          <li>Individual option trades: time & sales, block and sweep detection, multi-leg trade detection.</li>
          <li>Order-flow sentiment (whether trades hit the bid or the ask) and large single delta trades.</li>
          <li>Opening/closing auctions and order-imbalance data.</li>
          <li>Short-sale threshold lists.</li>
          <li>FDA decision (PDUFA) and merger calendars.</li>
          <li>Company guidance.</li>
          <li>ETF constituent holdings.</li>
          <li>Minute-level VWAP and intraday bars.</li>
          <li>Proprietary fair-value estimates.</li>
        </ul>
      </Card>
      <Card title="Proxies — read these labels carefully">
        <ul className="prose-list">
          <li><b>“Large volume contracts”</b> lists contracts whose <i>total daily volume</i> is large. It cannot tell one big trade from many small ones.</li>
          <li><b>Net delta</b> is volume × delta summed across contracts. It does not know whether each trade was a buy or a sell, so it is not directional flow.</li>
          <li><b>Unusual option volume</b> compares today's total volume with the symbol's recent average; it is not trade-level detection.</li>
          <li>Earnings strategy and option backtests mix real and model prices; check the real/model counts on each result.</li>
        </ul>
      </Card>
      <p className="muted small">See <Link to="/status">Data status</Link> for how much history has been collected so far.</p>
    </div>
  );
}

export function NotFound() {
  return (
    <div className="stack">
      <h1 className="page-title">Page not found</h1>
      <p><Link to="/">Back to the dashboard</Link></p>
    </div>
  );
}
