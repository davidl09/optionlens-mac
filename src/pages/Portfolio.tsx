// /portfolio (My → Portfolio): the user's positions from Questrade and IBKR, read-only (INTEGRATION.md §11). The engine
// values everything (live prices while quotes flow, P/L, greeks, FX-converted totals); this page shows it: a totals
// header, positions grouped by underlying with expandable legs, a risk graph per underlying, "Open in calculator"
// (the positions at their cost basis, pinned), each source's state and what to do when one needs a permission.
// Refreshes: every 3 s while live quotes flow, else every 60 s, only while the window is visible (the engine fetches
// Questrade's accounts at most once a minute), and on demand.
import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { Portfolio, PortfolioAggregate, PortfolioPosition, PortfolioSource, PortfolioUnderlying } from "../api/types";
import { useApi } from "../api/useApi";
import { Payoff } from "../components/charts/Payoff";
import { MoneyScope } from "../components/Money";
import { StatGrid } from "../components/StatGrid";
import { AsOf, Async, Card, Empty, ErrorBox, InfoBox, Segmented, Spinner } from "../components/ui";
import { fmt, signClass } from "../lib/format";
import { currencyOf } from "../lib/markets";
import { anySourceUsable, calculatorLegs, instrumentText, MAX_CALC_LEGS, money, PRICE_SOURCE_TEXT, SOURCE_TONE, sourceStateText, sumText, sumValue, unknownCount } from "../lib/portfolio";
import { Flash, isFlowing, useLive, useLiveStatus } from "../live";
import type { SymbolNavState } from "./symbol/SymbolLayout";

const LIVE_MS = 3_000;
const IDLE_MS = 60_000;
const CURRENCIES = [{ value: "USD", label: "USD" }, { value: "CAD", label: "CAD" }];

/** Re-runs `fn` every `ms` while the window is visible. */
function useVisibleInterval(fn: () => void, ms: number) {
  useEffect(() => {
    const t = window.setInterval(() => { if (document.visibilityState !== "hidden") fn(); }, ms);
    return () => window.clearInterval(t);
  }, [fn, ms]);
}

function SourceLine({ s }: { s: PortfolioSource }) {
  const counts = s.state === "ok" || s.state === "stale" ? ` · ${s.positions} position${s.positions === 1 ? "" : "s"}, ${s.accounts} account${s.accounts === 1 ? "" : "s"}` : "";
  return (
    <div className="portfolio-source" data-testid={`source-${s.broker}`} data-state={s.state}>
      <span className={`badge ${SOURCE_TONE[s.state]}`}>{s.label}: {sourceStateText(s)}</span>
      <span className="muted small">{counts}{s.fetched_at && <> · <AsOf ts={s.fetched_at} label="updated" /></>}</span>
      {s.state === "needs_permission" && s.detail && <div className="warn-box" role="alert">{s.detail} <Link to="/settings">Settings → Data sources</Link></div>}
      {(s.state === "error" || s.state === "stale") && s.detail && <div className="muted small">{s.detail}</div>}
      {s.state === "not_connected" && <span className="muted small">{s.detail ?? "Not connected."}</span>}
      {s.state === "disabled" && <span className="muted small">Hidden: “Show my positions” is off (Settings → Data sources).</span>}
      {s.state === "waiting" && s.detail && <span className="muted small">{s.detail}</span>}
    </div>
  );
}

/** A money sum of an aggregate (converted, else per currency), flashing on live changes. */
function Sum({ agg, k, flowing, signed = false }: { agg: PortfolioAggregate; k: string; flowing: boolean; signed?: boolean }) {
  const v = sumValue(agg, k);
  const missing = unknownCount(agg, k);
  return (
    <span className={signed ? signClass(v) : undefined} title={missing ? `${missing} position(s) without a value are left out` : undefined}>
      <Flash value={v} active={flowing}>{sumText(agg, k)}</Flash>{missing ? " *" : ""}
    </span>
  );
}

function Totals({ p, flowing }: { p: Portfolio; flowing: boolean }) {
  const pct = p.totals.converted.values?.unrealized_pct ?? (Object.keys(p.totals.by_currency).length === 1 ? Object.values(p.totals.by_currency)[0].unrealized_pct : null);
  const bw = p.beta_weighted;
  return (
    <Card title="Totals" subtitle={<span className="muted small">in {p.currency} where every currency converts; otherwise per currency</span>}>
      <StatGrid cols={4} items={[
        { label: "Net liquidation", node: <Sum agg={p.balance_totals} k="net_liquidation" flowing={false} /> },
        { label: "Cash", node: <Sum agg={p.balance_totals} k="cash" flowing={false} /> },
        { label: "Buying power", node: <Sum agg={p.balance_totals} k="buying_power" flowing={false} /> },
        { label: "Market value", node: <Sum agg={p.totals} k="market_value" flowing={flowing} /> },
        { label: "Unrealized P/L", node: <Sum agg={p.totals} k="unrealized_pnl" flowing={flowing} signed />, sub: pct !== null && pct !== undefined ? fmt(pct, "pct_signed") : undefined },
        { label: "Day P/L", node: <Sum agg={p.totals} k="day_pnl" flowing={flowing} signed />, title: "The broker's day P/L where it reports one, else (price − previous close) × quantity (exact for positions held through the previous close)" },
        { label: "Delta $", node: <Sum agg={p.totals} k="delta_dollars" flowing={flowing} signed />, title: "Share-equivalent delta × underlying price" },
        { label: "Theta / day", node: <Sum agg={p.totals} k="theta" flowing={flowing} signed /> },
        { label: "Vega / vol pt", node: <Sum agg={p.totals} k="vega" flowing={flowing} signed /> },
        { label: "Beta-weighted Δ (SPY)", node: bw.spy_shares === null ? <span className="muted">—</span> : <span className={signClass(bw.spy_shares)}><Flash value={bw.spy_shares} active={flowing}>{fmt(bw.spy_shares, "num")} sh</Flash></span>,
          sub: bw.dollars !== null ? `${money(bw.dollars, "USD", 0)}${bw.excluded ? ` · ${bw.excluded} not weighted` : ""}` : undefined,
          title: "Σ delta × price × 1-year beta ÷ SPY price, over US positions with a beta" },
      ]} />
      {p.totals.converted.missing_fx.length > 0 && <p className="small warn-text">No {p.totals.converted.missing_fx.join(", ")} → {p.currency} rate from a broker yet: totals are shown per currency (never added across currencies).</p>}
      {p.fx.length > 0 && <p className="muted small" data-testid="fx-label">FX: {p.fx[0].label}</p>}
    </Card>
  );
}

function num(v: number | null, kind: "num" | "num2" | "money" = "num2") { return fmt(v, kind); }

function LegRow({ r, flowing }: { r: PortfolioPosition; flowing: boolean }) {
  return (
    <tr className="portfolio-leg" data-testid={`leg-${r.symbol}`}>
      <td className="indent"><span className={`badge ${r.broker === "ibkr" ? "info" : "neutral"}`}>{r.broker === "ibkr" ? "IBKR" : "Questrade"}</span> <span className="muted small">{r.account}</span></td>
      <td>{instrumentText(r)}{r.delayed && <span className="badge warn" title="Delayed data"> delayed</span>}</td>
      <td className="num">{fmt(r.qty, "num")}</td>
      <td className="num">{money(r.avg_cost, r.currency)}</td>
      <td className="num" title={r.price_source ? PRICE_SOURCE_TEXT[r.price_source] : "No price"}>
        <Flash value={r.price} active={flowing && r.price_source === "live"}>{money(r.price, r.currency)}</Flash>
        {r.price_source && r.price_source !== "live" && <small className="muted"> {r.price_source}</small>}
      </td>
      <td className="num"><Flash value={r.market_value} active={flowing}>{money(r.market_value, r.currency)}</Flash></td>
      <td className={`num ${signClass(r.unrealized_pnl)}`}><Flash value={r.unrealized_pnl} active={flowing}>{money(r.unrealized_pnl, r.currency)}</Flash> <small>{fmt(r.unrealized_pct, "pct_signed")}</small></td>
      <td className={`num ${signClass(r.day_pnl)}`} title={r.day_pnl_basis === "prev_close" ? "(price − previous close) × quantity" : r.day_pnl_basis === "broker" ? "The broker's day P/L" : undefined}>
        <Flash value={r.day_pnl} active={flowing}>{money(r.day_pnl, r.currency)}</Flash>
      </td>
      <td className="num">{num(r.delta, "num")}</td>
      <td className="num">{money(r.delta_dollars, r.currency, 0)}</td>
      <td className="num">{num(r.gamma)}</td>
      <td className="num">{money(r.theta, r.currency)}</td>
      <td className="num">{money(r.vega, r.currency)}</td>
      <td className="num">{fmt(r.iv, "vol")}</td>
    </tr>
  );
}

function GroupRow({ u, open, onToggle, onRisk, riskOpen, flowing }: { u: PortfolioUnderlying; open: boolean; onToggle: () => void; onRisk: () => void; riskOpen: boolean; flowing: boolean }) {
  const navigate = useNavigate();
  const legs = u.linkable ? calculatorLegs(u.positions) : null;
  const t = u.totals;
  return (
    <tr className="portfolio-group" data-testid={`group-${u.underlying}`}>
      <td>
        <button className="btn btn-sm btn-ghost" aria-expanded={open} aria-label={`${open ? "Collapse" : "Expand"} ${u.underlying}`} onClick={onToggle}>{open ? "▾" : "▸"}</button>{" "}
        {u.linkable ? <Link className="sym-link" to={`/s/${encodeURIComponent(u.underlying)}`}>{u.underlying}</Link> : <b>{u.underlying}</b>}
        <span className="muted small"> {u.positions.length} position{u.positions.length === 1 ? "" : "s"}</span>
      </td>
      <td>
        {u.risk && <button className="btn btn-sm" aria-pressed={riskOpen} onClick={onRisk}>Risk graph</button>}{" "}
        {u.linkable && <button className="btn btn-sm" disabled={!legs} title={legs ? "The positions at their cost basis (pinned prices) in the calculator" : `Needs every position's cost and at most ${MAX_CALC_LEGS} distinct legs`}
          onClick={() => legs && navigate(`/s/${encodeURIComponent(u.underlying)}/calculator`, { state: { legs } satisfies SymbolNavState })}>Open in calculator</button>}
      </td>
      <td /><td />
      <td className="num" title="The underlying's price">{u.spot !== null ? money(u.spot, u.currencies[0] ?? "USD") : "—"}</td>
      <td className="num"><Sum agg={t} k="market_value" flowing={flowing} /></td>
      <td className="num"><Sum agg={t} k="unrealized_pnl" flowing={flowing} signed /></td>
      <td className="num"><Sum agg={t} k="day_pnl" flowing={flowing} signed /></td>
      <td className="num">{fmt(sumValue(t, "delta"), "num")}</td>
      <td className="num"><Sum agg={t} k="delta_dollars" flowing={flowing} signed /></td>
      <td className="num">{fmt(sumValue(t, "gamma"), "num2")}</td>
      <td className="num"><Sum agg={t} k="theta" flowing={flowing} signed /></td>
      <td className="num"><Sum agg={t} k="vega" flowing={flowing} signed /></td>
      <td />
    </tr>
  );
}

const RISK_MS = 15_000;

function RiskPanel({ underlying, onClose }: { underlying: string; onClose: () => void }) {
  const risk = useApi(() => api.portfolioRisk(underlying), [underlying]);
  useVisibleInterval(risk.reload, RISK_MS);
  return (
    <Card title={`Risk graph · ${underlying}`} subtitle={<span className="muted small">every position on {underlying} across accounts, each leg at its cost basis</span>}
      actions={<button className="btn btn-sm" onClick={onClose}>Close</button>}>
      <Async state={risk}>{(r) => (
        <MoneyScope currency={currencyOf(underlying)}>
          {r.prices && r.pnl_expiry && r.pnl_today && r.pnl_eval && r.breakevens ? (
            <>
              <Payoff result={{ prices: r.prices, pnl_expiry: r.pnl_expiry, pnl_eval: r.pnl_eval, pnl_today: r.pnl_today, spot: r.spot, breakevens: r.breakevens }} evalLabel={null} scaleKey={underlying} height={300} />
              <StatGrid cols={4} items={[
                { label: "Cost basis", value: r.cost, fmt: "money", signed: true },
                { label: "Max profit", value: r.max_profit, fmt: "money", hint: "Unlimited" },
                { label: "Max loss", value: r.max_loss, fmt: "money", hint: "Unlimited" },
                { label: "Expiry shown", value: r.expiry_used, fmt: "date", hint: "Stock only" },
              ]} />
            </>
          ) : <Empty>No legs to chart.</Empty>}
          {r.skipped.length > 0 && <p className="muted small">Left out: {r.skipped.map((s) => `${s.symbol} (${s.reason})`).join("; ")}</p>}
        </MoneyScope>
      )}</Async>
    </Card>
  );
}

function Positions({ p, flowing }: { p: Portfolio; flowing: boolean }) {
  const [open, setOpen] = useState<Set<string>>(() => new Set(p.underlyings.map((u) => u.underlying)));
  const [risk, setRisk] = useState<string | null>(null);
  const toggle = (u: string) => setOpen((s) => { const n = new Set(s); if (n.has(u)) n.delete(u); else n.add(u); return n; });
  return (
    <>
      <Card title="Positions" subtitle={<span className="muted small">grouped by underlying · prices: live while quotes flow, else the fresher of the stored quote and the broker's</span>}>
        <div className="table-scroll">
          <table className="dt portfolio-table">
            <thead><tr>
              <th>Underlying / account</th><th>Position</th><th className="num">Qty</th><th className="num">Avg cost</th><th className="num">Price</th>
              <th className="num">Market value</th><th className="num">Unrealized P/L</th><th className="num">Day P/L</th><th className="num">Delta</th>
              <th className="num">Delta $</th><th className="num">Gamma</th><th className="num">Theta/day</th><th className="num">Vega/pt</th><th className="num">IV</th>
            </tr></thead>
            <tbody>
              {p.underlyings.map((u) => (
                <Fragment key={u.underlying}>
                  <GroupRow u={u} open={open.has(u.underlying)} onToggle={() => toggle(u.underlying)} riskOpen={risk === u.underlying} onRisk={() => setRisk(risk === u.underlying ? null : u.underlying)} flowing={flowing} />
                  {open.has(u.underlying) && u.positions.map((r) => <LegRow key={r.id} r={r} flowing={flowing} />)}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      {risk && <RiskPanel underlying={risk} onClose={() => setRisk(null)} />}
    </>
  );
}

function Accounts({ p }: { p: Portfolio }) {
  if (!p.accounts.length) return null;
  return (
    <Card title="Accounts">
      <table className="dt">
        <thead><tr><th>Broker</th><th>Account</th><th>Currency</th><th className="num">Net liquidation</th><th className="num">Cash</th><th className="num">Buying power</th><th className="num">Positions</th></tr></thead>
        <tbody>
          {p.accounts.flatMap((a) => (a.balances.length ? a.balances : [{ currency: "", net_liquidation: null, cash: null, buying_power: null }]).map((b, i) => (
            <tr key={`${a.account_id}:${b.currency}`}>
              <td>{i === 0 ? (a.broker === "ibkr" ? "IBKR" : "Questrade") : ""}</td><td>{i === 0 ? a.account : ""}</td><td>{b.currency || "—"}</td>
              <td className="num">{money(b.net_liquidation, b.currency || "USD")}</td><td className="num">{money(b.cash, b.currency || "USD")}</td>
              <td className="num">{money(b.buying_power, b.currency || "USD")}</td><td className="num">{i === 0 ? a.totals.positions : ""}</td>
            </tr>
          )))}
        </tbody>
      </table>
    </Card>
  );
}

function Body({ p, flowing }: { p: Portfolio; flowing: boolean }) {
  useLive(useMemo(() => (p.live.symbols.length || p.live.contracts.length ? { symbols: p.live.symbols, contracts: p.live.contracts } : null), [p.live.symbols.join(","), p.live.contracts.join(",")]));
  const held = p.underlyings.length > 0;
  return (
    <div className="stack">
      <Card title="Sources">{p.sources.map((s) => <SourceLine key={s.broker} s={s} />)}</Card>
      {held && <Totals p={p} flowing={flowing} />}
      {held && <Positions p={p} flowing={flowing} />}
      {!held && (anySourceUsable(p.sources)
        ? <Card><Empty>No open positions.</Empty></Card>
        : <Card><Empty>
            Connect a broker to see your positions here: Questrade (Settings → Data sources, with the permission “Retrieve balances, positions, orders and executions”)
            or IBKR (IB Gateway / TWS connected; the read-only API is enough). OptionLens only reads positions and balances.
          </Empty></Card>)}
      <Accounts p={p} />
    </div>
  );
}

export default function PortfolioPage() {
  const [currency, setCurrency] = useState("USD");
  const force = useRef(false);          // the next request fetches the brokers' data now (Refresh)
  const state = useApi(() => { const now = force.current; force.current = false; return api.portfolio(currency, now); }, [currency]);
  const flowing = isFlowing(useLiveStatus());
  useVisibleInterval(state.reload, flowing ? LIVE_MS : IDLE_MS);
  return (
    <div className="stack">
      <div className="page-head">
        <h1 className="page-title">Portfolio</h1>
        <div className="toolbar">
          <Segmented label="Reporting currency" value={currency} options={CURRENCIES} onChange={setCurrency} />
          <button className="btn btn-sm" onClick={() => { force.current = true; state.refetch(); }} disabled={state.loading}>Refresh</button>
          {state.data && <AsOf ts={state.data.asof} label="refreshed" />}
        </div>
      </div>
      <InfoBox>Read-only: OptionLens shows your positions and balances and never places, changes or cancels orders. Account numbers show only their last 3 digits; nothing leaves this computer.</InfoBox>
      {state.error && !state.data && <ErrorBox error={state.error} onRetry={state.refetch} />}
      {!state.data && state.loading && <Spinner />}
      {state.data && <Body p={state.data} flowing={flowing} />}
    </div>
  );
}
