// /backtest/stock, /backtest/options, /backtest/straddle-wing
import { FormEvent, ReactNode, useEffect, useState } from "react";
import { api } from "../api/client";
import type { BacktestResult, OptionsBacktestRequest, StockBacktestRequest } from "../api/types";
import { useApi } from "../api/useApi";
import { BacktestResultView } from "../components/BacktestResultView";
import { Card, ErrorBox, InfoBox, Spinner } from "../components/ui";
import { addDays, todayNY } from "../lib/dates";
import { MONTHS } from "../lib/format";
import { parseSymbols } from "../lib/params";

function Field({ label, children, help }: { label: string; children: ReactNode; help?: string }) {
  return <div className="param" title={help}><label>{label}</label>{children}</div>;
}

/** Runs a POST backtest and tracks loading / error / result. */
function useRunner<Req>(fn: (r: Req) => Promise<BacktestResult>) {
  const [state, setState] = useState<{ loading: boolean; error: Error | null; data: BacktestResult | null }>({ loading: false, error: null, data: null });
  const run = (req: Req) => {
    setState((s) => ({ ...s, loading: true, error: null }));
    fn(req).then((data) => setState({ loading: false, error: null, data }), (e: unknown) => setState({ loading: false, error: e instanceof Error ? e : new Error(String(e)), data: null }));
  };
  return { ...state, run };
}

function ResultArea({ r, name }: { r: { loading: boolean; error: Error | null; data: BacktestResult | null }; name: string }) {
  if (r.loading) return <Spinner label="Running backtest…" />;
  if (r.error) return <ErrorBox error={r.error} />;
  if (!r.data) return <InfoBox>Set the rules and press <b>Run backtest</b>.</InfoBox>;
  return <BacktestResultView result={r.data} csvName={name} />;
}

type EntryType = StockBacktestRequest["entry"]["type"];
const ENTRY_TYPES: { value: EntryType; label: string }[] = [
  { value: "every_n_days", label: "Every N trading days" }, { value: "day_of_month", label: "Day of month" },
  { value: "month_of_year", label: "Month of year (seasonal)" }, { value: "days_before_earnings", label: "N days before earnings" },
  { value: "days_after_earnings", label: "N days after earnings" }, { value: "gap", label: "Gap up / down ≥ x%" },
  { value: "rsi", label: "RSI(14) below / above x" }, { value: "sma_cross", label: "Close crosses SMA(n)" }, { value: "new_52w", label: "New 52-week high / low" },
];

export function StockBacktest() {
  const r = useRunner(api.backtestStock);
  const [symbols, setSymbols] = useState("SPY");
  const [entry, setEntry] = useState<StockBacktestRequest["entry"]>({ type: "every_n_days", n: 21 });
  const [hold, setHold] = useState("10");
  const [atReaction, setAtReaction] = useState(false);
  const [stop, setStop] = useState("");
  const [take, setTake] = useState("");
  const [side, setSide] = useState<"long" | "short">("long");
  const [start, setStart] = useState(addDays(todayNY(), -365 * 5));
  const [end, setEnd] = useState(todayNY());
  const num = (s: string) => (s.trim() === "" ? undefined : Number(s));
  const setE = (patch: Partial<StockBacktestRequest["entry"]>) => setEntry((e) => ({ ...e, ...patch }));
  const t = entry.type;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    r.run({
      symbols: parseSymbols(symbols), entry, side, start, end,
      exit: { hold_days: num(hold), at_earnings_reaction: atReaction || undefined, stop_loss: num(stop) !== undefined ? Number(stop) / 100 : undefined, take_profit: num(take) !== undefined ? Number(take) / 100 : undefined },
    });
  };

  return (
    <div className="stack">
      <div className="page-head"><div><h1 className="page-title">Stock rule backtest</h1><p className="page-desc">Daily-close based rule tester on stored price history. Exits trigger on the first rule hit.</p></div></div>
      <Card>
        <form className="param-form" onSubmit={submit}>
          <Field label="Symbols"><input className="w-wide" value={symbols} onChange={(e) => setSymbols(e.target.value.toUpperCase())} placeholder="AAPL, MSFT" /></Field>
          <Field label="Entry rule">
            <select value={t} onChange={(e) => { const ty = e.target.value as EntryType; setEntry({ type: ty, n: ty === "sma_cross" ? 50 : 5, value: ty === "gap" ? 0.02 : 30, direction: ty === "gap" ? "up" : ty === "new_52w" ? "high" : "below" }); }}>
              {ENTRY_TYPES.map((x) => <option key={x.value} value={x.value}>{x.label}</option>)}
            </select>
          </Field>
          {["every_n_days", "days_before_earnings", "days_after_earnings", "sma_cross"].includes(t) && (
            <Field label={t === "sma_cross" ? "SMA length" : "N (days)"}><input type="number" className="w-num" value={entry.n ?? ""} onChange={(e) => setE({ n: Number(e.target.value) })} /></Field>
          )}
          {t === "day_of_month" && <Field label="Day"><input type="number" min={1} max={31} className="w-num" value={entry.day ?? 1} onChange={(e) => setE({ day: Number(e.target.value) })} /></Field>}
          {t === "month_of_year" && (
            <Field label="Month"><select value={entry.month ?? 1} onChange={(e) => setE({ month: Number(e.target.value) })}>{MONTHS.map((m, i) => <option key={m} value={i + 1}>{m}</option>)}</select></Field>
          )}
          {(t === "gap" || t === "rsi") && <Field label={t === "gap" ? "Gap ≥ (%)" : "RSI level"}><input type="number" className="w-num" value={entry.value === undefined ? "" : t === "gap" ? +(entry.value * 100).toFixed(4) : entry.value}
              onChange={(e) => setE({ value: t === "gap" ? Number(e.target.value) / 100 : Number(e.target.value) })} /></Field>}
          {["gap", "rsi", "sma_cross", "new_52w"].includes(t) && (
            <Field label="Direction">
              <select value={entry.direction} onChange={(e) => setE({ direction: e.target.value as StockBacktestRequest["entry"]["direction"] })}>
                {(t === "gap" ? ["up", "down"] : t === "new_52w" ? ["high", "low"] : ["below", "above"]).map((d) => <option key={d} value={d}>{d}</option>)}
              </select>
            </Field>
          )}
          <Field label="Hold (days)"><input type="number" className="w-num" value={hold} onChange={(e) => setHold(e.target.value)} /></Field>
          <div className="param param-bool"><label>Exit at earnings reaction</label><input type="checkbox" checked={atReaction} onChange={(e) => setAtReaction(e.target.checked)} /></div>
          <Field label="Stop loss (%)"><input type="number" className="w-num" value={stop} onChange={(e) => setStop(e.target.value)} placeholder="none" /></Field>
          <Field label="Take profit (%)"><input type="number" className="w-num" value={take} onChange={(e) => setTake(e.target.value)} placeholder="none" /></Field>
          <Field label="Direction"><select value={side} onChange={(e) => setSide(e.target.value as "long" | "short")}><option value="long">Long</option><option value="short">Short</option></select></Field>
          <Field label="Start"><input type="date" value={start} onChange={(e) => setStart(e.target.value)} /></Field>
          <Field label="End"><input type="date" value={end} onChange={(e) => setEnd(e.target.value)} /></Field>
          <div className="param param-submit"><button className="btn btn-primary" type="submit" disabled={r.loading}>Run backtest</button></div>
        </form>
      </Card>
      <ResultArea r={r} name="stock-backtest" />
    </div>
  );
}

export interface OptionsPreset { strategyMatch: RegExp; width_pct: number; title: string; description: string }

export const STRADDLE_WING_PRESET: OptionsPreset = {
  strategyMatch: /iron[-_ ]?(fly|butterfl)/i, width_pct: 0.05,
  title: "Straddle / wing backtest", description: "Short ATM straddle with protective wings (iron butterfly), rolled on a schedule.",
};

export function OptionsBacktest({ preset }: { preset?: OptionsPreset }) {
  const r = useRunner(api.backtestOptions);
  const templates = useApi(api.strategyTemplates, []);
  const [req, setReq] = useState<OptionsBacktestRequest>({
    symbol: "SPY", strategy: "", strike_rule: { type: "atm" }, width_pct: preset?.width_pct ?? 0.05,
    entry: { type: "monthly" }, target_dte: 30, exit: { type: "expiry" },
    start: addDays(todayNY(), -365 * 2), end: todayNY(), qty: 1,
  });
  const set = (patch: Partial<OptionsBacktestRequest>) => setReq((x) => ({ ...x, ...patch }));
  useEffect(() => {
    const list = templates.data;
    if (!list?.length || req.strategy) return;
    const match = preset ? list.find((t) => preset.strategyMatch.test(t.id) || preset.strategyMatch.test(t.label)) : undefined;
    set({ strategy: (match ?? list[0]).id });
  }, [templates.data, preset, req.strategy]);
  const pct = (v: number | undefined) => (v === undefined ? "" : String(+(v * 100).toFixed(4)));
  const fromPct = (s: string) => (s.trim() === "" ? undefined : Number(s) / 100);

  const submit = (e: FormEvent) => { e.preventDefault(); r.run({ ...req, symbol: req.symbol.toUpperCase() }); };

  return (
    <div className="stack">
      <div className="page-head"><div>
        <h1 className="page-title">{preset?.title ?? "Options strategy backtest"}</h1>
        <p className="page-desc">{preset?.description ?? "Any strategy template on a schedule. Uses stored end-of-day option prices when every leg is available, otherwise a Black-Scholes model with an IV proxy — each trade is labelled real or model."}</p>
      </div></div>
      <Card>
        <form className="param-form" onSubmit={submit}>
          <Field label="Symbol"><input value={req.symbol} onChange={(e) => set({ symbol: e.target.value.toUpperCase() })} className="w-sym" /></Field>
          <Field label="Strategy">
            <select value={req.strategy} onChange={(e) => set({ strategy: e.target.value })}>
              {(templates.data ?? []).map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
            </select>
          </Field>
          <Field label="Strike rule">
            <select value={req.strike_rule.type} onChange={(e) => set({ strike_rule: { type: e.target.value as OptionsBacktestRequest["strike_rule"]["type"], value: e.target.value === "delta" ? 0.3 : e.target.value === "pct_otm" ? 0.05 : undefined } })}>
              <option value="atm">At the money</option><option value="delta">By delta</option><option value="pct_otm">% out of the money</option>
            </select>
          </Field>
          {req.strike_rule.type === "delta" && <Field label="Delta"><input type="number" step="0.05" className="w-num" value={req.strike_rule.value ?? ""} onChange={(e) => set({ strike_rule: { type: "delta", value: Number(e.target.value) } })} /></Field>}
          {req.strike_rule.type === "pct_otm" && <Field label="% OTM"><input type="number" className="w-num" value={pct(req.strike_rule.value)} onChange={(e) => set({ strike_rule: { type: "pct_otm", value: fromPct(e.target.value) } })} /></Field>}
          <Field label="Wing width (%)" help="Width between strikes for spreads / wings"><input type="number" className="w-num" value={pct(req.width_pct)} onChange={(e) => set({ width_pct: fromPct(e.target.value) })} /></Field>
          <Field label="Entry">
            <select value={req.entry.type} onChange={(e) => set({ entry: { type: e.target.value as OptionsBacktestRequest["entry"]["type"], n: 10, weekday: 0 } })}>
              <option value="monthly">Monthly (first trading day)</option><option value="weekly">Weekly on day</option><option value="every_n_days">Every N trading days</option>
            </select>
          </Field>
          {req.entry.type === "every_n_days" && <Field label="N"><input type="number" className="w-num" value={req.entry.n ?? ""} onChange={(e) => set({ entry: { ...req.entry, n: Number(e.target.value) } })} /></Field>}
          {req.entry.type === "weekly" && (
            <Field label="Weekday"><select value={req.entry.weekday ?? 0} onChange={(e) => set({ entry: { ...req.entry, weekday: Number(e.target.value) } })}>{["Mon", "Tue", "Wed", "Thu", "Fri"].map((d, i) => <option key={d} value={i}>{d}</option>)}</select></Field>
          )}
          <Field label="Target DTE"><input type="number" className="w-num" value={req.target_dte} onChange={(e) => set({ target_dte: Number(e.target.value) })} /></Field>
          <Field label="Exit">
            <select value={req.exit.type} onChange={(e) => set({ exit: { ...req.exit, type: e.target.value as "expiry" | "dte", dte: e.target.value === "dte" ? 7 : undefined } })}>
              <option value="expiry">Hold to expiry</option><option value="dte">When DTE ≤ x</option>
            </select>
          </Field>
          {req.exit.type === "dte" && <Field label="Exit DTE"><input type="number" className="w-num" value={req.exit.dte ?? ""} onChange={(e) => set({ exit: { ...req.exit, dte: Number(e.target.value) } })} /></Field>}
          <Field label="Take profit (% of max)"><input type="number" className="w-num" placeholder="none" value={pct(req.exit.take_profit)} onChange={(e) => set({ exit: { ...req.exit, take_profit: fromPct(e.target.value) } })} /></Field>
          <Field label="Stop loss (%)"><input type="number" className="w-num" placeholder="none" value={pct(req.exit.stop_loss)} onChange={(e) => set({ exit: { ...req.exit, stop_loss: fromPct(e.target.value) } })} /></Field>
          <Field label="Contracts"><input type="number" min={1} className="w-num" value={req.qty ?? 1} onChange={(e) => set({ qty: Number(e.target.value) })} /></Field>
          <Field label="Start"><input type="date" value={req.start} onChange={(e) => set({ start: e.target.value })} /></Field>
          <Field label="End"><input type="date" value={req.end} onChange={(e) => set({ end: e.target.value })} /></Field>
          <div className="param param-submit"><button className="btn btn-primary" type="submit" disabled={r.loading || !req.strategy}>Run backtest</button></div>
        </form>
      </Card>
      <ResultArea r={r} name={`${req.symbol}-${req.strategy}-backtest`} />
    </div>
  );
}

export function StraddleWingBacktest() {
  return <OptionsBacktest preset={STRADDLE_WING_PRESET} />;
}
