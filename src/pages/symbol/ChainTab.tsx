// Option chain in straddle layout: calls | strike | puts. Clicking bid/ask adds a calculator leg.
import { ReactNode, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import type { Chain, Leg, OptionRow } from "../../api/types";
import { useApi } from "../../api/useApi";
import { useViewDetails } from "../../state/view";
import { StatGrid } from "../../components/StatGrid";
import { AfterHoursBadge, AsOf, Async, Card, NullValue, Segmented, SourceBadge } from "../../components/ui";
import { fmt, fmtDateTime, isMissing, signClass } from "../../lib/format";
import { nearest } from "../../lib/indicators";
import { Flash, isFlowing, useLive, useLiveOption, useLiveQuote, useLiveStatus } from "../../live";
import { withLiveOption } from "../../live/merge";
import { describeLeg, useLegs } from "../../state/legs";
import { ExpiryPills, useQueryValue, useSym } from "./shared";

type Side = "both" | "calls" | "puts";
type RangeOpt = "10" | "20" | "all";

interface Col { key: keyof OptionRow; label: string; fmt: "money" | "num2" | "int" | "vol" | "pct_signed"; greek?: boolean; title?: string }

const SIDE_COLS: Col[] = [
  { key: "bid", label: "Bid", fmt: "money", title: "Click to SELL at the bid" },
  { key: "ask", label: "Ask", fmt: "money", title: "Click to BUY at the ask" },
  { key: "mid", label: "Mid", fmt: "money" },
  { key: "last", label: "Last", fmt: "money" },
  { key: "change", label: "Chg", fmt: "num2" },
  { key: "volume", label: "Vol", fmt: "int" },
  { key: "oi", label: "OI", fmt: "int" },
  { key: "iv", label: "IV", fmt: "vol" },
  { key: "delta", label: "Δ", fmt: "num2", greek: true, title: "Delta" },
  { key: "gamma", label: "Γ", fmt: "num2", greek: true, title: "Gamma" },
  { key: "theta", label: "Θ", fmt: "num2", greek: true, title: "Theta per day" },
  { key: "vega", label: "V", fmt: "num2", greek: true, title: "Vega per vol point" },
  { key: "theo", label: "Theo", fmt: "money", greek: true, title: "Feed theoretical value" },
];

type OnLeg = (row: OptionRow, qty: number, price: number) => void;

function ChainTable({ chain, side, range, greeks, onLeg }: { chain: Chain; side: Side; range: RangeOpt; greeks: boolean; onLeg: OnLeg }) {
  const stored = chain.underlying.price;
  const liveSpot = useLiveQuote(chain.symbol)?.price;
  const spot = liveSpot ?? stored;
  const cols = SIDE_COLS.filter((c) => greeks || !c.greek);
  const callCols = [...cols].reverse();
  const byStrike = useMemo(() => {
    const m = new Map<number, { c?: OptionRow; p?: OptionRow }>();
    for (const c of chain.calls) m.set(c.strike, { ...m.get(c.strike), c });
    for (const p of chain.puts) m.set(p.strike, { ...m.get(p.strike), p });
    return m;
  }, [chain]);
  const strikes = useMemo(() => {
    const all = chain.strikes.length ? chain.strikes : [...byStrike.keys()].sort((a, b) => a - b);
    if (range === "all") return all;
    const atm = nearest(all, stored);      // the window stays put while the live price moves
    const i = atm === null ? 0 : all.indexOf(atm);
    const n = Number(range);
    return all.slice(Math.max(0, i - n), i + n + 1);
  }, [chain.strikes, byStrike, range, stored]);
  const spotAfter = strikes.findIndex((k) => k > spot); // spot line goes before this index
  // Live: the visible expiry and strike window only (re-subscribed when either changes).
  useLive(strikes.length ? { symbols: [chain.symbol], chains: [{ symbol: chain.symbol, expiry: chain.expiry, strike_min: strikes[0], strike_max: strikes[strikes.length - 1] }] } : { symbols: [chain.symbol] });
  const flowing = isFlowing(useLiveStatus());

  const showC = side !== "puts", showP = side !== "calls";
  const width = (showC ? cols.length : 0) + (showP ? cols.length : 0) + 1;
  return (
    <div className="dt-scroll chain-scroll">
      <table className="dt chain">
        <thead>
          <tr className="chain-sides">
            {showC && <th colSpan={cols.length} className="side-h calls-h">Calls</th>}
            <th className="strike-h" />
            {showP && <th colSpan={cols.length} className="side-h puts-h">Puts</th>}
          </tr>
          <tr>
            {showC && callCols.map((c) => <th key={`c-${c.key}`} className="num" title={c.title}>{c.label}</th>)}
            <th className="strike-col">Strike</th>
            {showP && cols.map((c) => <th key={`p-${c.key}`} className="num" title={c.title}>{c.label}</th>)}
          </tr>
        </thead>
        <tbody>
          {strikes.map((k, i) => {
            const r = byStrike.get(k) ?? {};
            return (
              <FragmentRow key={k} spotLine={i === spotAfter} spot={spot} left={showC ? cols.length : 0} right={showP ? cols.length : 0}>
                <ChainRow chain={chain} strike={k} call={showC ? r.c : undefined} put={showP ? r.p : undefined} showC={showC} showP={showP}
                  callCols={callCols} cols={cols} spot={liveSpot ?? null} flowing={flowing} onLeg={onLeg} />
              </FragmentRow>
            );
          })}
          {!strikes.length && <tr><td colSpan={width} className="dt-empty">No contracts for this expiry.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

/** One strike: its call and put with their live fields merged in (each row re-renders only for its own contracts). */
function ChainRow({ chain, strike, call, put, showC, showP, callCols, cols, spot, flowing, onLeg }: {
  chain: Chain; strike: number; call?: OptionRow; put?: OptionRow; showC: boolean; showP: boolean; callCols: Col[]; cols: Col[];
  spot: number | null; flowing: boolean; onLeg: OnLeg;
}) {
  const pc = useLiveOption(call?.osi), pp = useLiveOption(put?.osi);
  const c = call && withLiveOption(call, pc, spot), p = put && withLiveOption(put, pp, spot);
  return (
    <tr>
      {showC && callCols.map((col) => chainCell(chain, c, col, flowing && pc !== null, onLeg))}
      <td className="strike-col">{fmt(strike, "num")}<RowSource chain={chain} rows={[call, put]} /></td>
      {showP && cols.map((col) => chainCell(chain, p, col, flowing && pp !== null, onLeg))}
    </tr>
  );
}

function chainCell(chain: Chain, row: OptionRow | undefined, c: Col, live: boolean, onLeg: OnLeg): ReactNode {
    if (!row) return <td key={c.key} className="num muted">·</td>;
    const v = row[c.key];
    const fl = (node: ReactNode) => <Flash value={v} active={live}>{node}</Flash>;
    const base = `num ${row.itm ? "itm" : ""}`;
    if ((c.key === "bid" || c.key === "ask") && !isMissing(v) && (v as number) > 0) {
      const qty = c.key === "bid" ? -1 : 1;
      return (
        <td key={c.key} className={`${base} quote-cell`}>
          <button className={`quote-btn ${c.key}`} title={`${qty > 0 ? "Buy" : "Sell"} 1 ${row.right === "C" ? "call" : "put"} ${row.strike} @ ${fmt(v, "money")} → calculator`}
            onClick={() => onLeg(row, qty, v as number)}>{fl(fmt(v, "num2"))}</button>
        </td>
      );
    }
    if (c.key === "volume") {
      const hot = row.volume !== null && row.volume > 0 && row.volume > (row.oi ?? 0);
      return <td key={c.key} className={`${base} ${hot ? "hot" : ""}`} title={hot ? "Volume exceeds open interest" : undefined}>{fl(fmt(v, "int"))}</td>;
    }
    if (c.key === "last") {
      return <td key={c.key} className={base}><Link to={`/s/${chain.symbol}/contract/${encodeURIComponent(row.osi)}`} title={row.last_ts ? `Last trade ${fmtDateTime(row.last_ts)} — contract history` : "Contract history"}>{fl(isMissing(v) ? "—" : fmt(v, "num2"))}</Link></td>;
    }
    if (isMissing(v)) return <td key={c.key} className={base}><NullValue hint={c.key === "iv" ? "Feed reported no IV for this contract." : undefined} /></td>;
    const computed = computedHint(row, c.key);
    if (computed) return <td key={c.key} className={base} title={computed}><i>{fl(fmt(v, c.fmt === "money" ? "num2" : c.fmt))}</i></td>;
    return <td key={c.key} className={`${base} ${c.key === "change" ? signClass(v) : ""}`}>{fl(fmt(v, c.fmt === "money" ? "num2" : c.fmt))}</td>;
}


/** Why an IV / greek cell is shown in italics: the engine computed it because the feed sent none. */
function computedHint(row: OptionRow, key: Col["key"]): string | null {
  if (key === "iv" && (row.iv_source === "computed_mid" || row.iv_source === "computed_last")) {
    return `Computed by OptionLens (Black-Scholes) from the ${row.iv_source === "computed_mid" ? "bid/ask mid" : "last trade of the session"}: the feed sent no IV`;
  }
  if ((key === "delta" || key === "gamma" || key === "theta" || key === "vega") && row.greeks_source === "computed") {
    return "Computed by OptionLens (Black-Scholes) from the IV: the feed sent no greeks";
  }
  return null;
}

/** A row whose source differs from the chain's (e.g. reconstructed among IBKR rows) is badged at its strike. */
function RowSource({ chain, rows }: { chain: Chain; rows: (OptionRow | undefined)[] }) {
  const src = rows.find((x) => x?.source && x.source !== chain.source)?.source;
  return src ? <> <SourceBadge source={src} /></> : null;
}

function FragmentRow({ spotLine, spot, left, right, children }: { spotLine: boolean; spot: number; left: number; right: number; children: ReactNode }) {
  return (
    <>
      {spotLine && (
        <tr className="spot-row" title="Underlying price">
          {left > 0 && <td colSpan={left} className="spot-line" />}
          <td className="strike-col spot-label"><span>{fmt(spot, "num2")}</span></td>
          {right > 0 && <td colSpan={right} className="spot-line" />}
        </tr>
      )}
      {children}
    </>
  );
}

export default function ChainTab() {
  const sym = useSym();
  const [expiry, setExpiry] = useQueryValue("expiry");
  const [side, setSide] = useState<Side>("both");
  const [range, setRange] = useState<RangeOpt>("10");
  const [greeks, setGreeks] = useState(false);
  const { legs, addLeg } = useLegs();
  const [lastAdded, setLastAdded] = useState<Leg | null>(null);
  const st = useApi(() => api.chain(sym, expiry), [sym, expiry]);
  // For agents (current view): the expiry shown and what part of the chain is on screen.
  useViewDetails({ expiry: st.data?.expiry ?? expiry ?? null, side, strikes_around_atm: range, greeks });

  const onLeg = (row: OptionRow, qty: number, price: number) => {
    // the clicked bid / ask is the user's price (pinned in the calculator); the IV is not chosen: it follows live data
    const leg: Leg = { kind: "option", right: row.right, strike: row.strike, expiry: row.expiry, qty, price, iv: null };
    addLeg(leg);
    setLastAdded(leg);
  };

  return (
    <Async state={st}>{(chain) => {
      const info = chain.expiries.find((e) => e.expiry === chain.expiry);
      return (
        <div className="stack">
          <Card>
            <ExpiryPills expiries={chain.expiries} value={chain.expiry} onChange={setExpiry} />
            <div className="chain-bar">
              {info && <StatGrid cols={6} items={[
                { label: "Expiry", value: info.expiry, fmt: "date", sub: `${info.dte} DTE${info.is_monthly ? " · monthly" : ""}` },
                { label: "ATM IV", value: info.atm_iv, fmt: "vol" },
                { label: "Straddle", value: info.straddle, fmt: "money", sub: info.straddle !== null ? fmt(info.straddle / chain.underlying.price, "pct") : undefined },
                { label: "Call / put vol", node: <span>{fmt(info.call_volume, "int")} / {fmt(info.put_volume, "int")}</span> },
                { label: "Call / put OI", node: <span>{fmt(info.call_oi, "int")} / {fmt(info.put_oi, "int")}</span> },
                { label: "Underlying", value: chain.underlying.price, fmt: "money", sub: <AsOf ts={chain.asof} /> },
              ]} />}
            </div>
            <div className="toolbar">
              <Segmented label="Side" value={side} onChange={setSide} options={[{ value: "both", label: "Both" }, { value: "calls", label: "Calls" }, { value: "puts", label: "Puts" }]} />
              <Segmented label="Strikes" value={range} onChange={setRange} options={[{ value: "10", label: "±10" }, { value: "20", label: "±20" }, { value: "all", label: "All" }]} />
              <label className="check-inline"><input type="checkbox" checked={greeks} onChange={(e) => setGreeks(e.target.checked)} /> Greeks</label>
              {(chain.source || chain.delayed) && <span className="chain-source">Source <SourceBadge source={chain.source} delayed={chain.delayed === true} /></span>}
              <AfterHoursBadge show={chain.after_hours} />
              {chain.contracts_from_cache && <span className="muted small" title="IBKR's contract (sec-def) server did not answer, so the bridge used the contract list it saved earlier. Quotes are current; expiries listed since then are missing.">contract list from {chain.contracts_listed_at ? fmtDateTime(chain.contracts_listed_at).slice(0, 10) : "earlier"} (IBKR contract server unavailable)</span>}
              <span className="muted small legend-inline"><span className="swatch itm" /> in the money · <span className="swatch hot" /> volume &gt; OI · click bid to sell / ask to buy</span>
            </div>
          </Card>
          {lastAdded && (
            <div className="leg-toast" role="status">
              Added <b>{describeLeg(lastAdded)}</b> @ {fmt(lastAdded.price, "money")} · {legs.length} leg{legs.length === 1 ? "" : "s"} in calculator
              <Link className="btn btn-sm btn-primary" to={`/s/${sym}/calculator`}>Open calculator →</Link>
              <button className="btn btn-sm btn-ghost" onClick={() => setLastAdded(null)} aria-label="Dismiss">×</button>
            </div>
          )}
          <Card className="flush">
            <ChainTable chain={chain} side={side} range={range} greeks={greeks} onLeg={onLeg} />
          </Card>
        </div>
      );
    }}</Async>
  );
}
