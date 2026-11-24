// Strategy payout calculator: legs editor, templates, eval-date & IV-shift sliders, payoff chart, metrics. Prices follow
// the pricing rule of lib/calcPricing.ts (your pinned price ?? live mid ?? stored mid) and re-price live, ≤ 2/s.
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api/client";
import type { CalcRequest, CalcResult, Chain, ColumnDef, ExpiryInfo, Leg, ReportRow, StrategyTemplate } from "../../api/types";
import { useApi, useLatest } from "../../api/useApi";
import { Payoff } from "../../components/charts/Payoff";
import { DataTable } from "../../components/DataTable";
import { StatGrid } from "../../components/StatGrid";
import { AsOf, Async, Card, Empty, ErrorBox, Spinner } from "../../components/ui";
import { addDays, daysBetween, todayNY } from "../../lib/dates";
import { fmt, signClass } from "../../lib/format";
import { calcRequest, isPinned, keepPriceRange, type Origin, type PricedLeg, priceLegs, type Range, type StoredQuote, structureKey, unpinned } from "../../lib/calcPricing";
import { nearest } from "../../lib/indicators";
import { isFlowing, liveStore, useLive, useLiveStatus, useThrottled } from "../../live";
import { useLegs } from "../../state/legs";
import { useSym } from "./shared";
import { moneyStyleOf } from "../../lib/markets";
import { MoneyText } from "../../components/Money";

/** Default base expiry for templates: first one ≥ 21 DTE, else the last. */
function defaultBase(expiries: ExpiryInfo[]): string | null {
  return (expiries.find((e) => e.dte >= 21) ?? expiries[expiries.length - 1])?.expiry ?? null;
}

/**
 * Template legs on the listed strikes of the base chain. Mirrors the server's push-outward rule: legs with
 * different offsets land on distinct strikes in the direction of their offset (nearest strike to spot is the anchor).
 * Calendar legs keep distinct expiries (base shifts earlier if needed). Returns null when that is impossible.
 */
export function instantiateTemplate(t: StrategyTemplate, spot: number, expiries: ExpiryInfo[], base: string, strikes: number[]): Leg[] | null {
  const maxIdx = Math.max(0, ...t.legs.map((l) => l.expiry_index ?? 0));
  const baseIdx = Math.min(Math.max(0, expiries.findIndex((e) => e.expiry === base)), expiries.length - 1 - maxIdx);
  if (baseIdx < 0) return null;
  const grid = [...strikes].sort((x, y) => x - y);
  if (t.legs.some((l) => l.kind === "option") && !grid.length) return null;
  const anchor = Math.max(0, grid.indexOf(nearest(grid, spot) ?? 0));
  // strike index per (right, offset): walk outward from the anchor so each further offset takes a further strike
  const idxFor = new Map<string, number>();
  for (const right of ["C", "P"]) {
    const offs = [...new Set(t.legs.filter((l) => l.kind === "option" && (l.right ?? "C") === right).map((l) => l.strike_offset_pct ?? 0))];
    for (const dir of [1, -1]) {
      let prev = anchor;
      for (const off of offs.filter((o) => o * dir > 0).sort((x, y) => dir * (x - y))) {
        let i = grid.indexOf(nearest(grid, spot * (1 + off)) ?? 0);
        i = dir > 0 ? Math.max(i, prev + 1) : Math.min(i, prev - 1);
        if (i < 0 || i >= grid.length) return null;
        idxFor.set(`${right}${off}`, i);
        prev = i;
      }
    }
    idxFor.set(`${right}0`, anchor);
  }
  return t.legs.map((l): Leg => {
    if (l.kind === "stock") return { kind: "stock", qty: l.qty };
    const right = l.right ?? "C";
    return { kind: "option", right, qty: l.qty, expiry: expiries[baseIdx + (l.expiry_index ?? 0)].expiry, strike: grid[idxFor.get(`${right}${l.strike_offset_pct ?? 0}`) as number] };
  });
}

const ORIGIN_TEXT = {
  price: { user: "your price", live: "live", stored: "stored" },
  iv: { user: "your IV", live: "live", stored: "stored" },
} as const;

function originTitle(what: "price" | "iv", from: Origin, stock: boolean): string {
  if (from === "user") return `Your ${what === "iv" ? "IV" : "price"}: pinned, live updates leave it alone (Reset to live to follow the market again)`;
  if (what === "iv") return from === "live" ? "The contract's live IV" : "The contract's IV in the stored chain";
  if (stock) return from === "live" ? "The live underlying price" : "The stored underlying price";
  return from === "live" ? "The contract's live mid (bid/ask midpoint)" : "The contract's mid in the stored chain";
}

function LockIcon() {
  return (
    <svg className="lock-icon" width="10" height="11" viewBox="0 0 10 11" aria-hidden="true">
      <rect x="1" y="5" width="8" height="6" rx="1" fill="currentColor" />
      <path d="M3 5V3.5a2 2 0 0 1 4 0V5" fill="none" stroke="currentColor" strokeWidth="1.4" />
    </svg>
  );
}

/** Where a leg's price / IV comes from: your pinned value (lock), live, or the stored chain. */
function OriginTag({ what, from, stock = false }: { what: "price" | "iv"; from: Origin | null; stock?: boolean }) {
  if (!from) return null;
  return (
    <span className={`leg-origin ${from}`} data-origin={from} title={originTitle(what, from, stock)}>
      {from === "user" && <LockIcon />}{ORIGIN_TEXT[what][from]}
    </span>
  );
}

function LegEditor({ leg, priced, listed, expiries, spot, flowing, onChange, onRemove }: {
  leg: Leg; priced: PricedLeg; listed: number[] | null; expiries: ExpiryInfo[]; spot: number; flowing: boolean;
  onChange: (l: Leg) => void; onRemove: () => void;
}) {
  const strikes = listed ?? (leg.strike !== undefined ? [leg.strike] : []);
  // the strike to keep when the expiry/type changes (the select then snaps to the nearest strike listed there)
  const wantStrike = useRef(leg.strike);
  if (leg.strike !== undefined) wantStrike.current = leg.strike;
  useEffect(() => {
    if (leg.kind !== "option" || !listed?.length || (leg.strike !== undefined && listed.includes(leg.strike))) return;
    onChange({ ...unpinned(leg), strike: nearest(listed, wantStrike.current ?? spot) ?? undefined });
  });
  const step = (d: number) => {
    let q = leg.qty + d * (leg.kind === "stock" ? 100 : 1);
    if (q === 0) q += d * (leg.kind === "stock" ? 100 : 1);
    onChange({ ...leg, qty: q });
  };
  // changing what the leg is drops its pins (a price typed for another contract means nothing here)
  const reset = (patch: Partial<Leg>) => onChange({ ...unpinned(leg), ...patch });
  const p = priced.leg;
  const stock = leg.kind === "stock";
  return (
    <tr className={isPinned(leg) ? "leg-pinned" : undefined}>
      <td>
        <select value={leg.kind} aria-label="Leg type" onChange={(e) => reset(e.target.value === "stock" ? { kind: "stock", qty: 100, right: undefined, strike: undefined, expiry: undefined } : { kind: "option", qty: 1, right: "C", expiry: expiries[0]?.expiry, strike: undefined })}>
          <option value="option">Option</option><option value="stock">Stock</option>
        </select>
      </td>
      <td>
        {leg.kind === "option" && (
          <select value={leg.right} aria-label="Call or put" onChange={(e) => reset({ right: e.target.value as "C" | "P" })}>
            <option value="C">Call</option><option value="P">Put</option>
          </select>
        )}
      </td>
      <td>
        {leg.kind === "option" && (
          <select value={leg.expiry} aria-label="Expiry" onChange={(e) => reset({ expiry: e.target.value, strike: undefined })}>
            {expiries.map((e) => <option key={e.expiry} value={e.expiry}>{e.expiry} ({e.dte}d)</option>)}
          </select>
        )}
      </td>
      <td>
        {leg.kind === "option" && (
          <select value={leg.strike ?? ""} aria-label="Strike" onChange={(e) => reset({ strike: Number(e.target.value) })}>
            {strikes.map((k) => <option key={k} value={k}>{fmt(k, "num")}</option>)}
          </select>
        )}
      </td>
      <td>
        <div className="qty">
          <button className="btn btn-sm" onClick={() => step(-1)} aria-label="Decrease quantity">−</button>
          <span className={`qty-val ${signClass(leg.qty)}`}>{leg.qty > 0 ? `+${leg.qty}` : leg.qty}</span>
          <button className="btn btn-sm" onClick={() => step(1)} aria-label="Increase quantity">+</button>
        </div>
      </td>
      <td>
        <div className="leg-value">
          <input type="number" step="0.01" className={`w-num ${priced.priceFrom === "user" ? "pinned" : ""}`} aria-label="Entry price"
            placeholder={p.price != null ? fmt(p.price, "num2") : "mid"}
            value={leg.price ?? ""} onChange={(e) => onChange({ ...leg, price: e.target.value === "" ? null : Number(e.target.value) })} />
          <OriginTag what="price" from={priced.priceFrom} stock={stock} />
        </div>
      </td>
      <td>
        {leg.kind === "option" && (
          <div className="leg-value">
            <input type="number" step="0.1" className={`w-num ${priced.ivFrom === "user" ? "pinned" : ""}`} aria-label="IV in vol points"
              placeholder={p.iv != null ? fmt(p.iv, "vol") : "IV"}
              value={leg.iv != null ? +(leg.iv * 100).toFixed(2) : ""} onChange={(e) => onChange({ ...leg, iv: e.target.value === "" ? null : Number(e.target.value) / 100 })} />
            <OriginTag what="iv" from={priced.ivFrom} />
          </div>
        )}
      </td>
      <td>
        {isPinned(leg) && (
          <button className="btn btn-sm btn-ghost" onClick={() => onChange(unpinned(leg))}
            title={flowing ? "Drop your price / IV: follow the live mid and IV again" : "Drop your price / IV: use the stored mid and IV (live quotes are not flowing)"}>
            {flowing ? "Reset to live" : "Reset to mid"}
          </button>
        )}
      </td>
      <td><button className="btn btn-sm btn-ghost" onClick={onRemove} aria-label="Remove leg">✕</button></td>
    </tr>
  );
}

const PL_COLUMNS: ColumnDef[] = [
  { key: "price", label: "Price", fmt: "money" },
  { key: "change_pct", label: "Move", fmt: "pct_signed" },
  { key: "pnl_expiry", label: "P/L at expiry", fmt: "money" },
  { key: "pnl_eval", label: "P/L at date", fmt: "money" },
];
const plCell = (c: ColumnDef, r: ReportRow) => (c.key.startsWith("pnl") ? <span className={signClass(r[c.key])}><MoneyText value={r[c.key]} /></span> : undefined);

function Metrics({ r }: { r: CalcResult }) {
  const unlimited = <span className="muted">Unlimited</span>;
  return (
    <StatGrid cols={4} items={[
      { label: r.cost >= 0 ? "Net debit" : "Net credit", value: Math.abs(r.cost), fmt: "money" },
      { label: "Max profit", node: r.max_profit === null ? unlimited : <span className="pos"><MoneyText value={r.max_profit} /></span> },
      { label: "Max loss", node: r.max_loss === null ? unlimited : <span className="neg"><MoneyText value={r.max_loss} /></span> },
      { label: "Breakevens", node: r.breakevens.length ? <span>{r.breakevens.map((b) => fmt(b, "num2")).join(" · ")}</span> : <span className="muted">none</span> },
      { label: "Prob. of profit", value: r.pop, fmt: "pct" },
      { label: "EV (IV)", value: r.ev_iv, fmt: "money", signed: true },
      { label: "EV (HV30)", value: r.ev_hv, fmt: "money", signed: true, hint: "Needs 30 days of price history." },
      { label: "Expiry used", value: r.expiry_used, fmt: "date" },
      { label: "Delta ($/1)", value: r.greeks.delta, fmt: "num2", signed: true },
      { label: "Gamma", value: r.greeks.gamma, fmt: "num2" },
      { label: "Theta ($/day)", value: r.greeks.theta, fmt: "num2", signed: true },
      { label: "Vega ($/vol pt)", value: r.greeks.vega, fmt: "num2", signed: true },
    ]} />
  );
}

const RECALC_MS = 500;   // ≤ 2 calc requests per second
const chainKey = (expiry: string, right: string, strike: number) => `${expiry}|${right}|${strike}`;

/** The stored chain of every leg expiry (one cached GET each): strikes per expiry and each contract's OSI / mid / IV. */
function useLegChains(sym: string, legs: Leg[]) {
  const expiries = [...new Set(legs.filter((l) => l.kind === "option" && l.expiry).map((l) => l.expiry as string))].sort();
  const key = expiries.join(",");
  const st = useApi(expiries.length ? () => Promise.all(expiries.map((e) => api.chain(sym, e))) : null, [sym, key]);
  return useMemo(() => {
    const quotes = new Map<string, StoredQuote>();
    const strikes = new Map<string, number[]>();
    for (const c of st.data ?? []) {
      strikes.set(c.expiry, c.strikes);
      for (const r of [...c.calls, ...c.puts]) quotes.set(chainKey(r.expiry, r.right, r.strike), { osi: r.osi, mid: r.mid, iv: r.iv });
    }
    const stored = (l: Leg) => (l.kind === "option" && l.expiry && l.right && l.strike !== undefined ? quotes.get(chainKey(l.expiry, l.right, l.strike)) ?? null : null);
    return { stored, strikes, loading: st.loading };
  }, [st.data, st.loading]);
}

/** Re-renders (at most every `ms`) when the live values of the underlying or of these contracts change while flowing. */
function useLiveTicks(sym: string, osis: string[], flowing: boolean, ms: number): number {
  useLive({ symbols: [sym], contracts: osis });
  const [tick, setTick] = useState(0);
  const osisKey = osis.join(",");
  useEffect(() => {
    if (!flowing) return undefined;
    const bump = () => setTick((n) => n + 1);
    const offs = [liveStore.onKey("q", sym.toUpperCase(), bump), ...osis.map((o) => liveStore.onKey("o", o, bump))];
    return () => offs.forEach((off) => off());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flowing, sym, osisKey]);
  return useThrottled(tick, ms);
}

const latest = (a: string | null, b: string | null | undefined) => (b && (!a || b > a) ? b : a);

function CalculatorBody({ sym, chain, base, setBase }: { sym: string; chain: Chain; base: string; setBase: (e: string) => void }) {
  const { legs, setLegs } = useLegs();
  const templates = useApi(api.strategyTemplates, []);
  const [templateId, setTemplateId] = useState("");
  const [evalDays, setEvalDays] = useState(0);
  const [ivShift, setIvShift] = useState(0);
  const today = todayNY();
  const storedSpot = chain.underlying.price;

  const optionExpiries = legs.filter((l) => l.kind === "option" && l.expiry).map((l) => l.expiry as string).sort();
  const maxDays = optionExpiries.length ? Math.max(0, daysBetween(today, optionExpiries[0])) : 0;
  const days = Math.min(evalDays, maxDays);
  const evalDate = addDays(today, days);

  // The pricing rule (calcPricing.ts), applied to the current legs before every request: your pinned price / IV, else
  // the live mid / IV (while quotes flow), else the stored chain's; the spot likewise. Live ticks re-price ≤ 2/s.
  const legChains = useLegChains(sym, legs);
  const osis = useMemo(() => [...new Set(legs.map((l) => legChains.stored(l)?.osi).filter((o): o is string => !!o))], [legs, legChains]);
  const flowing = isFlowing(useLiveStatus());
  const tick = useLiveTicks(sym, osis, flowing, RECALC_MS);
  const market = useMemo(() => {
    const q = flowing ? liveStore.get("q", sym.toUpperCase()) : null;
    const liveSpot = q?.price ?? q?.last ?? null;
    const priced = priceLegs(legs, { stored: legChains.stored, live: (o) => (flowing ? liveStore.get("o", o) : null), liveSpot, storedSpot });
    const liveTs = priced.reduce<string | null>((a, p) => latest(a, p.liveTs), liveSpot !== null ? q?.ts ?? null : null);
    return { priced, liveSpot, liveTs };
    // `tick` is the trigger for re-reading the live store
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [legs, legChains, flowing, sym, storedSpot, tick]);
  const spot = market.liveSpot ?? storedSpot;

  // Stable chart: the x range (and table rows) is pinned per position / eval date / IV shift while the spot stays in it.
  const scaleKey = JSON.stringify([structureKey(legs), evalDate, ivShift]);
  const [pin, setPin] = useState<{ key: string; range: Range } | null>(null);
  const priceRange = keepPriceRange(pin?.key === scaleKey ? pin.range : null, spot);
  // price only a complete position (every option leg has a strike) once its chains are known
  const ready = legs.length > 0 && !legChains.loading && legs.every((l) => l.kind === "stock" || l.strike !== undefined);
  const reqKey = ready ? JSON.stringify({ scaleKey, req: calcRequest(sym, market.priced, { spot, ivShift: ivShift / 100, evalDate, priceRange }) }) : null;
  const calc = useLatest(reqKey, (k) => api.calcStrategy((JSON.parse(k) as { req: CalcRequest }).req), RECALC_MS);
  const answered = useMemo(() => (calc.dataKey ? JSON.parse(calc.dataKey) as { scaleKey: string; req: CalcRequest } : null), [calc.dataKey]);
  useEffect(() => {
    const r = calc.data;
    if (!answered || !r || answered.req.price_range || !r.prices.length) return;
    setPin({ key: answered.scaleKey, range: [r.prices[0], r.prices[r.prices.length - 1]] });   // the engine's default range
  }, [answered, calc.data]);
  const [templateMsg, setTemplateMsg] = useState<string | null>(null);

  const applyTemplate = (id: string) => {
    setTemplateId(id);
    const t = templates.data?.find((x) => x.id === id);
    if (!t) return;
    const built = instantiateTemplate(t, spot, chain.expiries, base, chain.strikes);
    setTemplateMsg(built ? null : `${t.label} can't be built here: not enough listed strikes or expiries around ${fmt(spot, "money", moneyStyleOf(sym))}.`);
    if (built) setLegs(built);
  };
  const addLeg = (kind: "C" | "P" | "stock") => {
    const atm = nearest(chain.strikes, spot) ?? spot;
    setLegs([...legs, kind === "stock" ? { kind: "stock", qty: 100 } : { kind: "option", right: kind, qty: 1, expiry: base, strike: atm }]);
  };
  const result = calc.data && legs.length ? calc.data : null;
  const stale = answered?.scaleKey !== scaleKey;   // the shown answer is of another position / date / IV shift
  const anyPinned = legs.some(isPinned);
  const families = useMemo(() => {
    const m = new Map<string, StrategyTemplate[]>();
    for (const t of templates.data ?? []) m.set(t.family, [...(m.get(t.family) ?? []), t]);
    return [...m.entries()];
  }, [templates.data]);

  return (
    <div className="stack">
      <Card title="Legs" subtitle={<>
        Spot <MoneyText value={spot} /> <span className={`leg-origin ${market.liveSpot !== null ? "live" : "stored"}`}>{market.liveSpot !== null ? "live" : "stored"}</span>
        {" · "}{market.liveTs ? <AsOf ts={market.liveTs} label="live as of" /> : <AsOf ts={chain.asof} />}
      </>} actions={
        <div className="toolbar">
          <label className="field-inline">Base expiry
            <select value={base} onChange={(e) => setBase(e.target.value)}>
              {chain.expiries.map((e) => <option key={e.expiry} value={e.expiry}>{e.expiry} ({e.dte}d)</option>)}
            </select>
          </label>
          <label className="field-inline">Template
            <select value={templateId} onChange={(e) => applyTemplate(e.target.value)} aria-label="Strategy template">
              <option value="">Choose…</option>
              {families.map(([f, ts]) => <optgroup key={f} label={f}>{ts.map((t) => <option key={t.id} value={t.id} title={t.description}>{t.label}</option>)}</optgroup>)}
            </select>
          </label>
        </div>
      }>
        {templateMsg && <ErrorBox error={new Error(templateMsg)} />}
        {legs.length > 0 ? (
          <div className="dt-scroll">
            <table className="dt legs-table">
              <thead><tr><th>Type</th><th>Right</th><th>Expiry</th><th>Strike</th><th>Qty</th><th>Price</th><th>IV</th><th /><th /></tr></thead>
              <tbody>
                {legs.map((l, i) => (
                  <LegEditor key={i} leg={l} priced={market.priced[i]} listed={l.expiry ? legChains.strikes.get(l.expiry) ?? null : null}
                    expiries={chain.expiries} spot={spot} flowing={flowing}
                    onChange={(nl) => setLegs(legs.map((x, j) => (j === i ? nl : x)))} onRemove={() => setLegs(legs.filter((_, j) => j !== i))} />
                ))}
              </tbody>
            </table>
          </div>
        ) : <Empty>No legs yet — pick a template, add a leg, or click a bid/ask in the chain.</Empty>}
        <div className="toolbar top-gap">
          <button className="btn btn-sm" onClick={() => addLeg("C")}>+ Call</button>
          <button className="btn btn-sm" onClick={() => addLeg("P")}>+ Put</button>
          <button className="btn btn-sm" onClick={() => addLeg("stock")}>+ Stock</button>
          {anyPinned && <button className="btn btn-sm btn-ghost" onClick={() => setLegs(legs.map(unpinned))}>Reset all</button>}
          {legs.length > 0 && <button className="btn btn-sm btn-ghost" onClick={() => { setLegs([]); setTemplateId(""); }}>Clear all</button>}
          <span className="muted small">An empty price / IV follows the live mid / IV (the stored chain's while live quotes are off); a typed one is yours (pinned) until reset. Options are per contract (×100), stock per share.</span>
        </div>
      </Card>
      {legs.length > 0 && (
        <>
          <Card>
            <div className="sliders">
              <label className="slider">
                <span>Evaluation date <b>{evalDate}</b> <small className="muted">(+{days}d of {maxDays})</small></span>
                <input type="range" min={0} max={maxDays} value={days} onChange={(e) => setEvalDays(Number(e.target.value))} aria-label="Evaluation date" />
              </label>
              <label className="slider">
                <span>IV shift <b className={signClass(ivShift)}>{ivShift > 0 ? "+" : ""}{ivShift} vol pts</b>{ivShift !== 0 && <button className="link-btn" onClick={() => setIvShift(0)}>reset</button>}</span>
                <input type="range" min={-50} max={50} value={ivShift} onChange={(e) => setIvShift(Number(e.target.value))} aria-label="IV shift" />
              </label>
            </div>
          </Card>
          {calc.error && <ErrorBox error={calc.error} />}
          {!result && !calc.error && <Spinner label="Pricing…" />}
          {result && (
            <div className={stale && calc.busy ? "refetching" : undefined}>
              <div className="grid-main-side">
                <Card title="Profit / loss"><Payoff result={result} evalLabel={`At ${evalDate}`} scaleKey={answered?.scaleKey} /></Card>
                <Card title="Position metrics"><Metrics r={result} /></Card>
              </div>
              <Card title="P/L by underlying price" className="top-gap">
                <DataTable minimal rowLink={false} columns={PL_COLUMNS} rows={result.table as unknown as ReportRow[]} renderCell={plCell} />
              </Card>
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default function CalculatorTab() {
  const sym = useSym();
  const [base, setBase] = useState<string | null>(null);
  // first load the default chain to learn the expiries, then the chain of the base expiry (strikes for templates)
  const first = useApi(() => api.chain(sym, null), [sym]);
  const baseExp = base ?? (first.data ? defaultBase(first.data.expiries) ?? first.data.expiry : null);
  const chain = useApi(baseExp ? () => api.chain(sym, baseExp) : null, [sym, baseExp]);
  return (
    <Async state={baseExp ? chain : first}>{(c) => <CalculatorBody sym={sym} chain={c} base={baseExp ?? c.expiry} setBase={setBase} />}</Async>
  );
}
