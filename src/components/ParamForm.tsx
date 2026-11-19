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
      case "int": case "number":
        return <input id={id} type="number" value={v} min={d.min} max={d.max} step={d.step ?? (d.type === "int" ? 1 : "any")} className="w-num"
          onChange={(e) => set(d.key, e.target.value)} />;
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
