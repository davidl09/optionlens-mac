import { useMemo } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import type { ColumnDef, ReportRow, SymbolSummary } from "../../api/types";
import { useApi } from "../../api/useApi";
import { LineChart, LineSeries } from "../../components/charts/Charts";
import { usePalette } from "../../components/charts/EChart";
import { DataTable } from "../../components/DataTable";
import { StatGrid, StatValue } from "../../components/StatGrid";
import { Async, Card, SourceBadge } from "../../components/ui";
import { nextEarningsSub, noNextEarningsText } from "../../lib/earnings";
import { fmt, plural } from "../../lib/format";
import { atPrice } from "../../lib/keyStats";
import { isFlowing, useLiveQuote, useLiveStatus } from "../../live";
import { IDEA_COLUMNS, ideasAsRows, ivAsOf, useSym } from "./shared";
import { MoneyText } from "../../components/Money";

export const MIN_IV_HISTORY = 20;
const OPT_HINT = "Collected by the IBKR bridge during market hours; none stored for the latest session yet.";

export function ivHistoryHint(days: number): string {
  const need = Math.max(0, MIN_IV_HISTORY - days);
  return need > 0 ? `Needs ${need} more day${need === 1 ? "" : "s"} of collected IV history (${days} so far).` : "Not available.";
}

export function unlimitedCell(col: ColumnDef, row: ReportRow) {
  if ((col.key === "max_profit" || col.key === "max_loss") && row[col.key] === null) {
    // null = not applicable for seasonal (stock) ideas, unlimited for option strategies
    return <span className="muted">{row.family === "seasonality" ? "—" : "Unlimited"}</span>;
  }
  return undefined;
}

function Levels({ title, values, spot, tone }: { title: string; values: number[]; spot: number | null; tone: "pos" | "neg" }) {
  return (
    <div className="levels">
      <div className="levels-title">{title}</div>
      {values.length ? values.map((v) => (
        <div key={v} className="level-row"><span className={tone}><MoneyText value={v} /></span>{spot ? <small className="muted">{fmt(v / spot - 1, "pct_signed")}</small> : null}</div>
      )) : <div className="muted small">None found</div>}
    </div>
  );
}

const NO_KEY_STAT = "No Questrade reference data for this symbol yet (stored once a day with the sweeps).";

function SummaryBody({ s }: { s: SymbolSummary }) {
  const p = usePalette();
  const q = s.quote;
  const patch = useLiveQuote(s.symbol);
  const ks = atPrice(s.key_stats, isFlowing(useLiveStatus()) ? patch?.price : null);   // the header's figures
  const src = (k: string) => <SourceBadge source={ks.sources[k]} />;
  const ivHint = ivHistoryHint(s.iv_history_days);
  const price = useMemo<LineSeries[]>(() => [{ name: "Close", data: s.price_1y.map((x) => [x.date, x.close]), color: p.series[0], area: true }], [s.price_1y, p]);
  const vol = useMemo<LineSeries[]>(() => [
    { name: "IV30", data: s.iv_hv_1y.map((x) => [x.date, x.iv30]), color: p.series[0] },
    { name: "HV20", data: s.iv_hv_1y.map((x) => [x.date, x.hv20]), color: p.series[1] },
  ], [s.iv_hv_1y, p]);
  const r = s.returns;
  const ne = s.next_earnings;
  const nd = s.next_dividend;

  return (
    <div className="stack">
      <div className="grid-main-side">
        <Card title="Key stats">
          <StatGrid items={[
            { label: "Open", value: q?.open, fmt: "money" },
            { label: "High", value: q?.high, fmt: "money" },
            { label: "Low", value: q?.low, fmt: "money" },
            { label: "Prev close", value: q?.prev_close, fmt: "money" },
            { label: "Volume", value: q?.volume, fmt: "int", sub: s.rel_volume !== null ? `${fmt(s.rel_volume, "ratio")} 30d avg` : undefined },
            { label: "Avg vol (30d)", value: s.avg_volume_30d, fmt: "int", sub: "from stored daily bars" },
            { label: "Avg vol 20d / 3m", node: <span><StatValue value={ks.avg_volume_20d} kind="int" hint={NO_KEY_STAT} /> / <StatValue value={ks.avg_volume_3m} kind="int" hint={NO_KEY_STAT} /> {src("avg_volume_20d")}</span> },
            { label: "52w range", node: <span><StatValue value={s.low_52w} kind="money" /> – <StatValue value={s.high_52w} kind="money" /> {ks.high_52w != null ? src("high_52w") : <small className="muted">stored bars</small>}</span> },
            { label: "3y range", node: <span><StatValue value={s.low_3y} kind="money" /> – <StatValue value={s.high_3y} kind="money" /></span> },
            { label: "Market cap", node: <span><StatValue value={ks.market_cap} kind="bigmoney" /> {src("market_cap")}</span>, title: "Price × shares outstanding (Questrade), else Nasdaq's figure" },
            { label: "P/E", node: <span><StatValue value={ks.pe} kind="num2" hint={ks.eps != null && ks.eps <= 0 ? "Not meaningful: EPS is not positive." : NO_KEY_STAT} /> {src("pe")}</span>, title: "Price / EPS" },
            { label: "EPS", node: <span><StatValue value={ks.eps} kind="money" hint={NO_KEY_STAT} /> {src("eps")}</span> },
            { label: "Shares out", node: <span><StatValue value={ks.shares_outstanding} kind="bigmoney" hint={NO_KEY_STAT} /> {src("shares_outstanding")}</span> },
            { label: "Div yield", node: <span><StatValue value={ks.dividend_yield} kind="pct" hint="No dividend data for this symbol." /> {src("dividend_yield")}</span>, title: "Indicated annual dividend / price; Questrade's yield while the history is too short" },
            { label: "Sector", value: s.sector, fmt: "text", sub: s.industry ?? undefined },
            { label: "Industry (Questrade)", node: <span><StatValue value={ks.industry_group} kind="text" hint={NO_KEY_STAT} /> {src("industry_group")}</span>, sub: [ks.sector, ks.industry_subgroup].filter(Boolean).join(" · ") || undefined },
            { label: "Beta (1y vs SPY)", value: s.beta_1y, fmt: "num2" },
            { label: "Corr SPY (1y)", value: s.corr_spy_1y, fmt: "num2" },
            { label: "IV30", value: s.iv30, fmt: "vol", sub: ivAsOf(s.iv30_asof) ? `as of ${ivAsOf(s.iv30_asof)}` : undefined },
            { label: "IV rank", value: s.iv_rank, fmt: "pct", hint: ivHint, sub: `${s.iv_history_days}d history` },
            { label: "IV percentile", value: s.iv_percentile, fmt: "pct", hint: ivHint },
            { label: "HV20", value: s.hv20, fmt: "vol" },
            { label: "IV / HV", value: s.iv_hv_ratio, fmt: "ratio", hint: "Needs both IV30 (latest session) and HV20." },
            { label: "Call / put vol", node: <span><StatValue value={s.call_volume} kind="int" hint={OPT_HINT} /> / <StatValue value={s.put_volume} kind="int" hint={OPT_HINT} /></span> },
            { label: "Opt vol vs 20d", value: s.rel_option_volume, fmt: "ratio", sub: s.avg_option_volume_20d !== null ? `avg ${fmt(s.avg_option_volume_20d, "int")} · ${s.avg_option_volume_days}d` : undefined, hint: "Needs at least one prior day of collected option volume.", title: s.avg_option_volume_days < 20 ? `Average over ${s.avg_option_volume_days} prior collected day(s) (target 20)` : undefined },
            { label: "P/C volume", value: s.pc_volume_ratio, fmt: "ratio", hint: OPT_HINT, title: "Put/call ratio: put volume ÷ call volume" },
            { label: "Total OI", value: s.total_oi, fmt: "int", hint: OPT_HINT, title: "Open interest: contracts open across all expiries" },
            { label: "RSI 14", value: s.rsi14, fmt: "num" },
            { label: "ATR 14", value: s.atr14_pct, fmt: "pct" },
            { label: "SMA 20 / 50 / 200", node: <span><StatValue value={s.sma20} kind="num2" /> · <StatValue value={s.sma50} kind="num2" /> · <StatValue value={s.sma200} kind="num2" /></span> },
          ]} />
        </Card>
        <div className="stack">
          <Card title="Next earnings" actions={<Link to={`/s/${s.symbol}/earnings`} className="small">Details →</Link>}>
            {ne ? <StatGrid cols={2} items={[
              { label: "Date", value: ne.date, fmt: "date", sub: <>{nextEarningsSub(ne)} <SourceBadge source={ne.source} /></> },
              { label: "Implied move", value: ne.implied_move_pct, fmt: "pct", hint: "No straddle available covering the event." },
              { label: "Avg |move|", value: ne.avg_abs_move_pct, fmt: "pct", hint: "No past earnings reactions in price history." },
              { label: "Implied / hist", value: ne.implied_move_pct !== null && ne.avg_abs_move_pct ? ne.implied_move_pct / ne.avg_abs_move_pct : null, fmt: "ratio" },
            ]} /> : <div className="muted small">{noNextEarningsText(s.symbol, s.next_earnings_missing)}</div>}
          </Card>
          <Card title="Next ex-dividend">
            {nd ? <StatGrid cols={2} items={[
              { label: "Ex-date", value: nd.ex_date, fmt: "date", sub: `in ${nd.days}d` },
              { label: "Amount", node: <span><StatValue value={nd.amount} kind="money" /> <SourceBadge source={nd.source} /></span> },
              { label: "Yield", value: nd.yield, fmt: "pct" },
            ]} /> : <div className="muted small">No announced ex-dividend date{s.kind === "index" ? " (indices pay none)" : ""}.</div>}
          </Card>
          <Card title="Support / resistance" subtitle="6-month pivot clusters">
            <div className="grid-2 tight">
              <Levels title="Resistance" values={s.resistance} spot={q?.price ?? null} tone="neg" />
              <Levels title="Support" values={s.support} spot={q?.price ?? null} tone="pos" />
            </div>
          </Card>
        </div>
      </div>
      <Card title="Returns">
        <StatGrid cols={8} items={[
          { label: "1 day", value: r.d1, fmt: "pct_signed" }, { label: "5 days", value: r.d5, fmt: "pct_signed" },
          { label: "1 month", value: r.m1, fmt: "pct_signed" }, { label: "3 months", value: r.m3, fmt: "pct_signed" },
          { label: "6 months", value: r.m6, fmt: "pct_signed" }, { label: "YTD", value: r.ytd, fmt: "pct_signed" },
          { label: "1 year", value: r.y1, fmt: "pct_signed" }, { label: "3 years", value: r.y3, fmt: "pct_signed" },
        ]} />
      </Card>
      <div className="grid-2">
        <Card title="Price, 1 year"><LineChart series={price} yFmt="money" height={220} /></Card>
        <Card title="IV30 vs HV20, 1 year" subtitle={`IV history: ${s.iv_history_days} ${plural(s.iv_history_days, "day")}`}><LineChart series={vol} yFmt="vol" height={220} /></Card>
      </div>
      <Card title="Top trade ideas" actions={<Link to={`/s/${s.symbol}/ideas`} className="small">All ideas →</Link>}>
        <DataTable columns={IDEA_COLUMNS} rows={ideasAsRows(s.ideas)} minimal rowLink={false} renderCell={unlimitedCell}
          emptyState={s.optionable === false ? "No trade ideas: no listed options." : "No trade ideas yet: they are built from a stored option chain."} />
      </Card>
    </div>
  );
}

export default function SummaryTab() {
  const sym = useSym();
  const st = useApi(() => api.summary(sym), [sym]);
  return <Async state={st}>{(s) => <SummaryBody s={s} />}</Async>;
}
