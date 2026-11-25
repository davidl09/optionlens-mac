// /status, /about, 404
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { ColumnDef, ReportRow, Status } from "../api/types";
import { useApi } from "../api/useApi";
import { DataTable } from "../components/DataTable";
import { StatGrid } from "../components/StatGrid";
import { bridgeSummary } from "../components/Desktop";
import { CHAIN_COLLECTION_TEXT, nextCollection } from "../components/Collection";
import { AsOf, Async, Card, Progress } from "../components/ui";
import { useBridgeStatus } from "../state/desktop";
import { fmt } from "../lib/format";
import { farmsSummary, jobState, optionsSubscriptionText, readableDetail } from "../lib/status";

const JOB_COLUMNS: ColumnDef[] = [
  { key: "job", label: "Job", fmt: "text" },
  { key: "started_at", label: "Started", fmt: "datetime" },
  { key: "finished_at", label: "Finished", fmt: "datetime" },
  { key: "state", label: "Result", fmt: "badge" },
  { key: "detail", label: "Detail", fmt: "text" },
];

const SOURCE_COLUMNS: ColumnDef[] = [
  { key: "name", label: "Source", fmt: "text" },
  { key: "state", label: "State", fmt: "badge" },
  { key: "last_success_at", label: "Last success", fmt: "datetime" },
  { key: "detail", label: "Detail", fmt: "text" },
];

function SourcesCard({ s }: { s: Status }) {
  const rows = s.sources.map((x) => ({ ...x, state: x.ok === null ? "unknown" : x.ok ? "ok" : "failed" })) as unknown as ReportRow[];
  return <Card title="Data sources"><DataTable minimal rowLink={false} columns={SOURCE_COLUMNS} rows={rows} emptyText="No sources reported." /></Card>;
}

function BridgeCard({ s }: { s: Status }) {
  const local = useBridgeStatus();
  return (
    <Card title="IBKR bridge" actions={local ? <Link className="btn btn-sm" to="/settings">Bridge settings</Link> : undefined}>
      <StatGrid items={[
        { label: "Engine sees bridge", node: <span className={`badge ${s.bridge.connected ? "pos" : "neutral"}`}>{s.bridge.connected ? "connected" : "not connected"}</span> },
        { label: "Last push", value: s.bridge.last_push_at, fmt: "datetime", hint: "The bridge has not pushed data yet." },
        { label: "Client", value: s.bridge.client, fmt: "text", hint: "No bridge has connected to this engine yet." },
        ...(s.bridge.connected && s.bridge.farms ? [{ label: "IBKR servers", node: <span>{farmsSummary({ marketData: s.bridge.farms.market_data, historical: s.bridge.farms.historical, secDef: s.bridge.farms.sec_def })}</span> }] : []),
        ...(s.bridge.connected ? [{ label: "US options", node: <span>{optionsSubscriptionText(s.bridge.options_subscription)}</span> }] : []),
      ]} />
      {local && <p className="small bridge-summary">This Mac: {bridgeSummary(local)}</p>}
    </Card>
  );
}

export function StatusPage() {
  const st = useApi(api.status, []);
  return (
    <div className="stack">
      <div className="page-head"><h1 className="page-title">Data status</h1>{st.data && <AsOf ts={st.data.now} label="server time" />}</div>
      <Async state={st}>{(s) => {
        const jobs = s.jobs.map((j) => ({ ...j, state: jobState(j), detail: readableDetail(j.detail) })) as unknown as ReportRow[];
        const counts = Object.entries(s.row_counts).map(([table, rows]) => ({ table, rows })) as unknown as ReportRow[];
        return (
          <>
            <Card title="Collection">
              <StatGrid items={[
                { label: "Market", node: <span className={`badge ${s.market_open ? "pos" : "neutral"}`}>{s.market_open ? "open" : "closed"}</span> },
                { label: "Last bridge push", value: s.bridge.last_push_at, fmt: "datetime", hint: "The IBKR bridge has not pushed data yet." },
                { label: "Next collection", node: <span>{nextCollection(s)}</span>, title: CHAIN_COLLECTION_TEXT },
                { label: "Last EOD", value: s.last_eod_date, fmt: "date", hint: "End-of-day job has not run yet." },
                { label: "Universe", value: s.universe_size, fmt: "int" },
                { label: "With a recent chain", value: s.optionable_count, fmt: "int", sub: `of ${fmt(s.universe_size, "int")} · last 5 sessions`, title: "Universe symbols with an option chain stored in the last 5 sessions" },
                ...(s.options_listed_count !== undefined ? [{ label: "Options listed", value: s.options_listed_count, fmt: "int" as const, sub: s.no_options_count ? `${fmt(s.no_options_count, "int")} confirmed none` : undefined, title: "Universe symbols the IBKR bridge has shown to list options (ever stored a chain)" }] : []),
                { label: "IV history", value: s.iv_history_days, fmt: "int", sub: "days in metrics" },
                { label: "EOD chains", value: s.option_eod_days, fmt: "int", sub: "days stored" },
                { label: "DB size", node: <span>{fmt(s.db_size_bytes / 1e6, "num")} MB</span> },
                { label: "Earnings backfill", node: <Progress value={s.earnings_backfill_progress} /> },
              ]} />
            </Card>
            <div className="grid-2">
              <SourcesCard s={s} />
              <BridgeCard s={s} />
            </div>
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
            <li><b>Interactive Brokers (IBKR bridge)</b> — option chains (bid/ask, volume, open interest, model greeks), per-underlying option volume / open interest / IV for the whole universe, IV and HV history, dividend history (derived from IBKR's dividend-adjusted daily closes, for every listing), VIX-family quotes and history, pushed from your IB Gateway or TWS. Real-time where your IBKR account has the subscription, otherwise 15-minute delayed (labelled <span className="badge warn">delayed</span>).</li>
            <li><b>Questrade</b> — option chains (bid/ask, volume, open interest, IV and greeks) and underlying quotes (with VWAP and trading halts) for the whole universe, extended-hours quotes for the pre-market / after-hours movers, and once a day each symbol's P/E, EPS, market cap, shares, 52-week range, average volumes, sector and current dividend — fetched by the engine with your Questrade API token (Settings → Data sources). Real-time with a Questrade real-time data package, otherwise delayed (labelled). When both IBKR and Questrade deliver a chain, the better one is used: a closing snapshot over an intraday one, real-time over delayed, more quoted contracts, then the fresher fetch.</li>
            <li><b>Nasdaq public endpoints</b> — daily price history, earnings and dividend calendars, dividend history, company profile, financial statements, insider filings, headlines and extended-hours quotes.</li>
            <li><b>U.S. Treasury</b> — the 13-week bill rate, used as the risk-free rate in option models of US listings.</li>
            <li><b>Canadian markets (with Questrade)</b> — about 200 of the largest TSX / TSX Venture stocks with Montréal Exchange options plus optionable Canadian ETFs (symbols like <b>RY.TO</b>; <b>RY</b> is the NYSE listing): chains, quotes, daily and intraday history from Questrade, prices in CAD (<b>C$</b>), sessions on the TSX holiday calendar, and the <b>Bank of Canada</b> 3-month T-bill as their risk-free rate. Reports have a Market filter (US by default).</li>
            <li>Everything else (IV rank, term structure, skew, the IV surface, expected moves, backtests…) is computed locally from those inputs.</li>
          </ul>
        </Card>
        <Card title="How to read the numbers">
          <ul className="prose-list">
            <li>A dash <span className="null-value">—</span> means a value is unknown, usually because not enough history has been collected yet. Hover it for the reason. Values are never filled in with guesses.</li>
            <li>Implied-volatility history (and therefore IV rank / percentile) begins on the first day the collector ran. IV rank needs at least 20 stored days.</li>
            <li>Historical option prices are only available from stored end-of-day chains. Where a result needs a price from before that, it is estimated with a Black-Scholes model and the row is labelled <span className="badge warn">model</span>; rows from stored prices count as real.</li>
            <li>The model's volatility input falls back from stored at-the-money IV, to stored 30-day IV, to recent historical volatility scaled by the symbol's typical implied/realized ratio.</li>
            <li>Full option chains are collected for the whole universe with Questrade connected, otherwise for the <b>chain universe</b> (watchlists, the most liquid symbols, recently opened symbols) from IBKR; chain-based screeners say so in their coverage note. Universe-wide reports use per-underlying IBKR statistics.</li>
            <li>The <b>IV surface</b> (Skew page) interpolates only between reliable contracts: a bid, a spread of at most 40% of the mid and open interest. Greyed, hatched cells come from contracts that fail one of these (the tooltip says which) and are not extrapolated.</li>
            <li>Rows labelled <span className="badge warn">reconstructed</span> were rebuilt from historical daily bars of individual contracts (no bid/ask/OI). Pages show an “as of” time.</li>
          </ul>
        </Card>
      </div>
      <Card title="Not available (no free source)">
        <ul className="prose-list cols-2">
          <li>Individual option trades: time & sales, block and sweep detection, multi-leg trade detection.</li>
          <li>Canadian listings: earnings dates, dividend histories (only Questrade’s current dividend), financial statements, insider filings and news (the free source, Nasdaq, covers US listings only); extended-hours quotes (the TSX has none). IBKR data for Canadian listings (the bridge stays US-only).</li>
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
          <li>The calculator's <b>earnings IV crush</b> is a model: it splits the ATM term structure into a base IV and one earnings jump, and values each leg at its base IV after the reaction session. Skew changes and a crush larger or smaller than the term structure implies are not modelled; switch it off above the chart to compare.</li>
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
