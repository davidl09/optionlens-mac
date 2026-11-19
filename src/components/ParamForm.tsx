// Renders ParamDef[] as a compact form. Values are kept in their URL-string form.
import { FormEvent, ReactNode } from "react";
import { api } from "../api/client";
import type { ParamDef } from "../api/types";
import { useApi } from "../api/useApi";
import type { ParamStrings } from "../lib/params";

export interface ParamFormProps {
  defs: ParamDef[];
  values: ParamStrings;
  onChange: (values: ParamStrings) => void;
  onSubmit: () => void;
  submitLabel?: string;
  busy?: boolean;
  extra?: ReactNode;
}

/** Fractional report params (0.05 = 5%) are shown and typed in percent; the stored/URL value stays the server's fraction. */
const PERCENT_KEYS = new Set(["min_change", "min_gap", "min_yield", "min_outperformance", "moneyness_min", "moneyness_max"]);
const isPercent = (d: ParamDef) => (d.type === "number") && (d.key.endsWith("_pct") || PERCENT_KEYS.has(d.key));
const scale = (n: number, by: number) => +(n * by).toFixed(8);
const toPercent = (v: string) => (v.trim() === "" || !Number.isFinite(Number(v)) ? v : String(scale(Number(v), 100)));
const fromPercent = (v: string) => (v.trim() === "" || !Number.isFinite(Number(v)) ? v : String(scale(Number(v), 0.01)));

export function ParamForm({ defs, values, onChange, onSubmit, submitLabel = "Run", busy = false, extra }: ParamFormProps) {
  const needsWatchlists = defs.some((d) => d.type === "watchlist");
  const wl = useApi(needsWatchlists ? api.watchlists : null, [needsWatchlists]);
  const set = (k: string, v: string) => onChange({ ...values, [k]: v });
  const submit = (e: FormEvent) => { e.preventDefault(); onSubmit(); };

  const field = (d: ParamDef) => {
    const v = values[d.key] ?? "";
    const id = `param-${d.key}`;
    switch (d.type) {
      case "bool":
        return <input id={id} type="checkbox" checked={v === "true"} onChange={(e) => set(d.key, e.target.checked ? "true" : "false")} />;
      case "select":
        return (
          <select id={id} value={v} onChange={(e) => set(d.key, e.target.value)}>
            {(d.options ?? []).map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        );
      case "watchlist":
        return (
          <select id={id} value={v} onChange={(e) => set(d.key, e.target.value)}>
            <option value="">— any —</option>
            {(wl.data ?? []).map((w) => <option key={w.id} value={String(w.id)}>{w.name}</option>)}
          </select>
        );
      case "symbols":
        return <input id={id} type="text" value={v} placeholder="e.g. AAPL, MSFT" spellCheck={false} className="w-wide"
          onChange={(e) => set(d.key, e.target.value.toUpperCase())} />;
      case "date":
        return <input id={id} type="date" value={v} onChange={(e) => set(d.key, e.target.value)} />;
      case "int": case "number": {
        const pct = isPercent(d);
        const sc = (n: number | undefined) => (pct && n !== undefined ? scale(n, 100) : n);
        return (
          <>
            <input id={id} type="number" value={pct ? toPercent(v) : v} min={sc(d.min)} max={sc(d.max)} step={sc(d.step) ?? (d.type === "int" ? 1 : "any")} className="w-num"
              onChange={(e) => set(d.key, pct ? fromPercent(e.target.value) : e.target.value)} />
            {pct && <span className="muted"> %</span>}
          </>
        );
      }
      default:
        return <input id={id} type="text" value={v} onChange={(e) => set(d.key, e.target.value)} />;
    }
  };

  return (
    <form className="param-form" onSubmit={submit}>
      {defs.map((d) => (
        <div className={`param ${d.type === "bool" ? "param-bool" : ""}`} key={d.key} title={d.help}>
          <label htmlFor={`param-${d.key}`}>{d.label}</label>
          {field(d)}
        </div>
      ))}
      {extra}
      <div className="param param-submit">
        <button type="submit" className="btn btn-primary" disabled={busy}>{busy ? "Running…" : submitLabel}</button>
      </div>
    </form>
  );
}
