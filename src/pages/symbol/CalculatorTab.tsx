// Strategy payout calculator: legs editor, templates, eval-date & IV-shift sliders, payoff chart, metrics.
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api/client";
import type { CalcRequest, CalcResult, Chain, ColumnDef, ExpiryInfo, Leg, ReportRow, StrategyTemplate } from "../../api/types";
import { useApi, useDebounced } from "../../api/useApi";
import { Payoff } from "../../components/charts/Payoff";
import { DataTable } from "../../components/DataTable";
import { StatGrid } from "../../components/StatGrid";
import { AsOf, Async, Card, Empty, ErrorBox, Spinner } from "../../components/ui";
import { addDays, daysBetween, todayNY } from "../../lib/dates";
import { fmt, signClass } from "../../lib/format";
import { nearest } from "../../lib/indicators";
import { useLegs } from "../../state/legs";
import { useSym } from "./shared";

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

function LegEditor({ sym, leg, index, expiries, spot, result, onChange, onRemove }: {
  sym: string; leg: Leg; index: number; expiries: ExpiryInfo[]; spot: number; result: CalcResult | null; onChange: (l: Leg) => void; onRemove: () => void;
}) {
  const chain = useApi(leg.kind === "option" ? () => api.chain(sym, leg.expiry) : null, [sym, leg.kind, leg.expiry]);
  const strikes = chain.data?.strikes ?? (leg.strike !== undefined ? [leg.strike] : []);
  const used = result?.legs[index];
  // the strike to keep when the expiry/type changes (the select then snaps to the nearest strike listed there)
  const wantStrike = useRef(leg.strike);
  if (leg.strike !== undefined) wantStrike.current = leg.strike;
  const listed = chain.data && chain.data.expiry === leg.expiry ? chain.data.strikes : null;
  useEffect(() => {
    if (leg.kind !== "option" || !listed?.length || (leg.strike !== undefined && listed.includes(leg.strike))) return;
    onChange({ ...leg, strike: nearest(listed, wantStrike.current ?? spot) ?? undefined, price: null, iv: null });
  });
  const step = (d: number) => {
    let q = leg.qty + d * (leg.kind === "stock" ? 100 : 1);
    if (q === 0) q += d * (leg.kind === "stock" ? 100 : 1);
    onChange({ ...leg, qty: q });
  };
  // changing what the leg is resets price/iv to the server defaults (mid / contract IV)
  const reset = (patch: Partial<Leg>) => onChange({ ...leg, ...patch, price: null, iv: null });
  return (
    <tr>
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
        <input type="number" step="0.01" className="w-num" aria-label="Entry price" placeholder={used ? fmt(used.mid ?? used.price, "num2") : "mid"}
          value={leg.price ?? ""} onChange={(e) => onChange({ ...leg, price: e.target.value === "" ? null : Number(e.target.value) })} />
      </td>
      <td>
        {leg.kind === "option" && (
          <input type="number" step="0.1" className="w-num" aria-label="IV in vol points" placeholder={used?.iv != null ? fmt(used.iv, "vol") : "IV"}
            value={leg.iv != null ? +(leg.iv * 100).toFixed(2) : ""} onChange={(e) => onChange({ ...leg, iv: e.target.value === "" ? null : Number(e.target.value) / 100 })} />
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
const plCell = (c: ColumnDef, r: ReportRow) => (c.key.startsWith("pnl") ? <span className={signClass(r[c.key])}>{fmt(r[c.key], "money")}</span> : undefined);

function Metrics({ r }: { r: CalcResult }) {
  const unlimited = <span className="muted">Unlimited</span>;
  return (
    <StatGrid cols={4} items={[
      { label: r.cost >= 0 ? "Net debit" : "Net credit", value: Math.abs(r.cost), fmt: "money" },
      { label: "Max profit", node: r.max_profit === null ? unlimited : <span className="pos">{fmt(r.max_profit, "money")}</span> },
      { label: "Max loss", node: r.max_loss === null ? unlimited : <span className="neg">{fmt(r.max_loss, "money")}</span> },
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

function CalculatorBody({ sym, chain, base, setBase }: { sym: string; chain: Chain; base: string; setBase: (e: string) => void }) {
  const { legs, setLegs } = useLegs();
  const templates = useApi(api.strategyTemplates, []);
  const [templateId, setTemplateId] = useState("");
  const [evalDays, setEvalDays] = useState(0);
  const [ivShift, setIvShift] = useState(0);
  const today = todayNY();
  const spot = chain.underlying.price;

  const optionExpiries = legs.filter((l) => l.kind === "option" && l.expiry).map((l) => l.expiry as string).sort();
  const maxDays = optionExpiries.length ? Math.max(0, daysBetween(today, optionExpiries[0])) : 0;
  const days = Math.min(evalDays, maxDays);
  const evalDate = addDays(today, days);

  const liveKey = JSON.stringify({ symbol: sym, legs, iv_shift: ivShift / 100, eval_date: evalDate });
  const reqKey = useDebounced(liveKey, 300);
  const req = useMemo(() => JSON.parse(reqKey) as CalcRequest, [reqKey]);   // the debounced request decides (not the live legs)
  // price only a settled, complete position (debounced == live, every option leg has a strike)
  const ready = reqKey === liveKey && legs.length > 0 && legs.every((l) => l.kind === "stock" || l.strike !== undefined);
  const calc = useApi(ready ? () => api.calcStrategy(req) : null, [req, ready]);
  const [templateMsg, setTemplateMsg] = useState<string | null>(null);

  const applyTemplate = (id: string) => {
    setTemplateId(id);
    const t = templates.data?.find((x) => x.id === id);
    if (!t) return;
    const built = instantiateTemplate(t, spot, chain.expiries, base, chain.strikes);
    setTemplateMsg(built ? null : `${t.label} can't be built here: not enough listed strikes or expiries around ${fmt(spot, "money")}.`);
    if (built) setLegs(built);
  };
  const addLeg = (kind: "C" | "P" | "stock") => {
    const atm = nearest(chain.strikes, spot) ?? spot;
    setLegs([...legs, kind === "stock" ? { kind: "stock", qty: 100 } : { kind: "option", right: kind, qty: 1, expiry: base, strike: atm }]);
  };
  const result = calc.data && legs.length ? calc.data : null;
  const families = useMemo(() => {
    const m = new Map<string, StrategyTemplate[]>();
    for (const t of templates.data ?? []) m.set(t.family, [...(m.get(t.family) ?? []), t]);
    return [...m.entries()];
  }, [templates.data]);

  return (
    <div className="stack">
      <Card title="Legs" subtitle={<>Spot {fmt(spot, "money")} · <AsOf ts={chain.asof} /></>} actions={
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
              <thead><tr><th>Type</th><th>Right</th><th>Expiry</th><th>Strike</th><th>Qty</th><th>Price</th><th>IV</th><th /></tr></thead>
              <tbody>
                {legs.map((l, i) => (
                  <LegEditor key={i} sym={sym} leg={l} index={i} expiries={chain.expiries} spot={spot} result={result}
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
          {legs.length > 0 && <button className="btn btn-sm btn-ghost" onClick={() => { setLegs([]); setTemplateId(""); }}>Clear all</button>}
          <span className="muted small">Empty price = mid; empty IV = contract IV. Options are per contract (×100), stock per share.</span>
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
            <div className={calc.loading ? "refetching" : undefined}>
              <div className="grid-main-side">
                <Card title="Profit / loss"><Payoff result={result} evalLabel={`At ${evalDate}`} /></Card>
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
