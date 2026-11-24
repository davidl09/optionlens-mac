// /s/:sym/* — quote header, grouped tab sub-nav, and tab routes (registry is the single source).
import { ComponentType } from "react";
import { Link, NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { api } from "../../api/client";
import type { Leg } from "../../api/types";
import { useApi } from "../../api/useApi";
import { AddToWatchlist } from "../../components/AddToWatchlist";
import { AfterHoursBadge, AsOf, ErrorBox, HaltedBadge, SourceBadge, Spinner } from "../../components/ui";
import { fmt, signClass } from "../../lib/format";
import { atPrice, vsVwap } from "../../lib/keyStats";
import { Flash, isFlowing, useLive, useLiveQuote, useLiveStatus } from "../../live";
import { withLiveQuote } from "../../live/merge";
import { LegsProvider } from "../../state/legs";
import CalculatorTab from "./CalculatorTab";
import { RefreshChip, SymbolRefreshProvider } from "./ChainRefresh";
import ChainTab from "./ChainTab";
import { AboutTab, FundamentalsTab, InsidersTab, NewsTab } from "./CompanyTabs";
import ContractPage from "./ContractPage";
import EarningsTab from "./EarningsTab";
import { ActivityTab, OpenInterestTab } from "./FlowTabs";
import { ivAsOf, useSym } from "./shared";
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

/** `optionable` (true / false / null = not known yet) as a header badge: nothing when a chain is stored. */
function OptionsBadge({ optionable }: { optionable: boolean | null }) {
  if (optionable === false) return <span className="badge warn" title="A chain source confirmed that this symbol lists no options">no options</span>;
  if (optionable === null) return <span className="badge subtle" title="No option chain fetched for this symbol yet">options not loaded</span>;
  return null;
}

function QuoteHeader({ sym }: { sym: string }) {
  const st = useApi(() => api.summary(sym), [sym]);
  useLive({ symbols: [sym] });
  const patch = useLiveQuote(sym);
  const flowing = isFlowing(useLiveStatus());
  if (st.error) return <ErrorBox error={st.error} onRetry={st.refetch} />;
  const s = st.data && st.data.symbol === sym ? st.data : null;
  const q = s?.quote ? withLiveQuote(s.quote, patch) : null;
  const ks = s ? atPrice(s.key_stats, q?.price) : null;   // P/E and market cap follow the live price
  const gap = vsVwap(q?.price, q?.vwap);
  const ivDate = ivAsOf(s?.iv30_asof ?? null);
  return (
    <div className="quote-header">
      <div className="qh-id">
        <h1>{sym}</h1>
        <div className="qh-name">{s ? s.name : st.loading ? <Spinner label="" /> : ""}{s && <span className="badge neutral">{s.kind}</span>}{s && <OptionsBadge optionable={s.optionable} />}</div>
      </div>
      {q && (
        <div className="qh-price">
          <span className="qh-last"><Flash value={q.price} active={flowing && patch !== null}>{fmt(q.price, "money")}</Flash></span>
          <span className={`qh-chg ${signClass(q.change)}`}>{q.change > 0 ? "+" : ""}{fmt(q.change, "num2")} ({fmt(q.change_pct, "pct_signed")})</span>
          <AsOf ts={q.ts} />
          <SourceBadge source={q.source} delayed={q.delayed === true} />
          <AfterHoursBadge show={q.after_hours} />
          <HaltedBadge halted={q.halted} />
        </div>
      )}
      {s && (
        <div className="qh-mini">
          <span title={s.iv30_asof ? `Latest stored IV30, as of ${s.iv30_asof}` : undefined}>IV30 <b>{fmt(s.iv30, "vol")}</b>{ivDate && <small> ({ivDate})</small>}</span>
          <span>IV rank <b>{fmt(s.iv_rank, "pct")}</b></span>
          <span>HV20 <b>{fmt(s.hv20, "vol")}</b></span>
          {q?.vwap != null && <span className="qh-vwap" title="The session's volume-weighted average price (Questrade) and the price against it">VWAP <b>{fmt(q.vwap, "money")}</b> <small className={signClass(gap)}>({fmt(gap, "pct_signed")} vs VWAP)</small></span>}
          {ks?.market_cap != null && <span title={`Market cap: ${ks.sources.market_cap === "questrade" ? "price × shares outstanding (Questrade)" : "Nasdaq"}`}>Mkt cap <b>{fmt(ks.market_cap, "bigmoney")}</b></span>}
          {ks?.pe != null && <span title="Price / EPS (Questrade's EPS)">P/E <b>{fmt(ks.pe, "num2")}</b></span>}
          {s.next_earnings && <span>Earnings <b>{s.next_earnings.date}</b> <small>({s.next_earnings.days}d)</small></span>}
        </div>
      )}
      <div className="qh-actions">
        <RefreshChip />
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

/** Calculator legs a navigation hands over (Portfolio → "Open in calculator"): read once when the symbol's pages mount
 * (switching tabs keeps the legs, as before). */
export interface SymbolNavState { legs?: Leg[] }

export default function SymbolLayout() {
  const sym = useSym();
  const handed = (useLocation().state as SymbolNavState | null)?.legs;
  return (
    <LegsProvider key={sym} initial={handed}>
      <SymbolRefreshProvider sym={sym}>
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
      </SymbolRefreshProvider>
    </LegsProvider>
  );
}
