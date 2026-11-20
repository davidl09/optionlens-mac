// /s/:sym/* — quote header, grouped tab sub-nav, and tab routes (registry is the single source).
import { ComponentType, useEffect, useState } from "react";
import type { RefreshResult } from "../../../electron/shared/api";
import { Link, NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api, invalidate, reloadAll } from "../../api/client";
import { useApi } from "../../api/useApi";
import { AddToWatchlist } from "../../components/AddToWatchlist";
import { AsOf, ErrorBox, SourceBadge, Spinner } from "../../components/ui";
import { desktop } from "../../desktop";
import { fmt, signClass } from "../../lib/format";
import { LegsProvider } from "../../state/legs";
import CalculatorTab from "./CalculatorTab";
import ChainTab from "./ChainTab";
import { AboutTab, FundamentalsTab, InsidersTab, NewsTab } from "./CompanyTabs";
import ContractPage from "./ContractPage";
import EarningsTab from "./EarningsTab";
import { ActivityTab, OpenInterestTab } from "./FlowTabs";
import { useSym } from "./shared";
import { ChartTab, DividendsTab, PriceActionTab } from "./StockTabs";
import { IdeasTab, StraddleTab } from "./StrategyTabs";
import SummaryTab from "./SummaryTab";
import { EventsTab, SeasonalityTab } from "./ToolsTabs";
import { ImpliedMovesTab, SkewTab, VolatilityTab, VolSeasonalityTab } from "./VolTabs";

interface TabDef { path: string; label: string; component: ComponentType }

export const TAB_GROUPS: { group: string; tabs: TabDef[] }[] = [
  { group: "Stock", tabs: [
    { path: "summary", label: "Summary", component: SummaryTab },
    { path: "chart", label: "Chart", component: ChartTab },
    { path: "price-action", label: "Price action", component: PriceActionTab },
    { path: "dividends", label: "Dividends", component: DividendsTab },
  ] },
  { group: "Options", tabs: [
    { path: "chain", label: "Chain", component: ChainTab },
    { path: "activity", label: "Activity", component: ActivityTab },
    { path: "open-interest", label: "Open interest", component: OpenInterestTab },
    { path: "calculator", label: "Calculator", component: CalculatorTab },
    { path: "ideas", label: "Trade ideas", component: IdeasTab },
    { path: "straddle", label: "Straddle", component: StraddleTab },
  ] },
  { group: "Volatility", tabs: [
    { path: "volatility", label: "Volatility", component: VolatilityTab },
    { path: "skew", label: "Skew", component: SkewTab },
    { path: "implied-moves", label: "Implied moves", component: ImpliedMovesTab },
    { path: "vol-seasonality", label: "Vol seasonality", component: VolSeasonalityTab },
  ] },
  { group: "Earnings", tabs: [{ path: "earnings", label: "Earnings", component: EarningsTab }] },
  { group: "Tools", tabs: [
    { path: "seasonality", label: "Seasonality", component: SeasonalityTab },
    { path: "events", label: "Events", component: EventsTab },
  ] },
  { group: "Company", tabs: [
    { path: "fundamentals", label: "Fundamentals", component: FundamentalsTab },
    { path: "insiders", label: "Insiders", component: InsidersTab },
    { path: "news", label: "News", component: NewsTab },
    { path: "about", label: "About", component: AboutTab },
  ] },
];

export const ALL_TABS = TAB_GROUPS.flatMap((g) => g.tabs);

/**
 * INTEGRATION.md §7: opening a symbol asks the bridge (main process) to fetch its chain from IBKR and push it
 * to the engine; when that worked, every query on the page reloads. Without the desktop app: no-op.
 */
function useIbkrRefresh(sym: string): RefreshResult | "pending" | null {
  const [state, setState] = useState<RefreshResult | "pending" | null>(null);
  useEffect(() => {
    if (!desktop) return undefined;
    let alive = true;
    setState("pending");
    desktop.refreshSymbol(sym).then((r) => {
      if (!alive) return;
      setState(r);
      if (r.status === "refreshed") { invalidate(`/api/symbol/${sym}`); reloadAll(); }
    }, (e: unknown) => { if (alive) setState({ status: "failed", message: e instanceof Error ? e.message : String(e) }); });
    return () => { alive = false; };
  }, [sym]);
  return state;
}

function RefreshChip({ state }: { state: RefreshResult | "pending" | null }) {
  if (state === null) return null;
  if (state === "pending") return <span className="refresh-chip" role="status"><span className="spinner-dot" />IBKR refresh…</span>;
  switch (state.status) {
    case "refreshed": return <span className="refresh-chip pos" title="Chain fetched from IBKR just now">IBKR ✓</span>;
    case "fresh": return <span className="refresh-chip pos" title="Chain from IBKR is recent">IBKR ✓</span>;
    case "unavailable": return <span className="refresh-chip" title={state.message}>IBKR off</span>;
    case "failed": return <span className="refresh-chip warn" title={state.message}>IBKR refresh failed</span>;
  }
}

function QuoteHeader({ sym }: { sym: string }) {
  const refresh = useIbkrRefresh(sym);
  const st = useApi(() => api.summary(sym), [sym]);
  if (st.error) return <ErrorBox error={st.error} onRetry={st.refetch} />;
  const s = st.data && st.data.symbol === sym ? st.data : null;
  const q = s?.quote ?? null;
  return (
    <div className="quote-header">
      <div className="qh-id">
        <h1>{sym}</h1>
        <div className="qh-name">{s ? s.name : st.loading ? <Spinner label="" /> : ""}{s && <span className="badge neutral">{s.kind}</span>}{s && !s.optionable && <span className="badge warn" title="No listed options found">no options</span>}</div>
      </div>
      {q && (
        <div className="qh-price">
          <span className="qh-last">{fmt(q.price, "money")}</span>
          <span className={`qh-chg ${signClass(q.change)}`}>{q.change > 0 ? "+" : ""}{fmt(q.change, "num2")} ({fmt(q.change_pct, "pct_signed")})</span>
          <AsOf ts={q.ts} />
          <SourceBadge source={q.source} delayed={q.delayed === true} />
        </div>
      )}
      {s && (
        <div className="qh-mini">
          <span>IV30 <b>{fmt(s.iv30, "vol")}</b></span>
          <span>IV rank <b>{fmt(s.iv_rank, "pct")}</b></span>
          <span>HV20 <b>{fmt(s.hv20, "vol")}</b></span>
          {s.next_earnings && <span>Earnings <b>{s.next_earnings.date}</b> <small>({s.next_earnings.days}d)</small></span>}
        </div>
      )}
      <div className="qh-actions">
        <RefreshChip state={refresh} />
        <AddToWatchlist symbol={sym} />
        <Link className="btn btn-sm" to={`/alerts?symbol=${sym}`}>+ Alert</Link>
      </div>
    </div>
  );
}

function SubNav({ sym }: { sym: string }) {
  return (
    <nav className="sub-nav" aria-label={`${sym} sections`}>
      {TAB_GROUPS.map((g) => (
        <div className="sub-group" key={g.group}>
          <div className="sub-group-title">{g.group}</div>
          {g.tabs.map((t) => <NavLink key={t.path} to={`/s/${sym}/${t.path}`} className={({ isActive }) => (isActive ? "active" : "")}>{t.label}</NavLink>)}
        </div>
      ))}
    </nav>
  );
}

export default function SymbolLayout() {
  const sym = useSym();
  return (
    <LegsProvider key={sym}>
      <div className="symbol-page">
        <QuoteHeader sym={sym} />
        <div className="symbol-body">
          <SubNav sym={sym} />
          <div className="symbol-content">
            <Routes>
              <Route index element={<Navigate to="summary" replace />} />
              {ALL_TABS.map((t) => <Route key={t.path} path={t.path} element={<t.component />} />)}
              <Route path="contract/:osi" element={<ContractPage />} />
              <Route path="*" element={<ErrorBox error={`Unknown tab for ${sym}.`} />} />
            </Routes>
          </div>
        </div>
      </div>
    </LegsProvider>
  );
}
