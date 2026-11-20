// Generic page for ANY report id: server params form, client-side table state, presets.
// Params and table state live in the URL so links are shareable.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type { ColumnDef, ColumnFilter, Preset, ReportDef, ReportRow } from "../api/types";
import { useApi } from "../api/useApi";
import { DataTable, inferColumns, SortState, TableState } from "../components/DataTable";
import { ParamForm } from "../components/ParamForm";
import { AsOf, Async, Card, ErrorBox, InfoBox, Spinner } from "../components/ui";
import { todayNY } from "../lib/dates";
import { ParamStrings, resolveParams, stringToValue, valueToString } from "../lib/params";

const K_SORT = "_sort", K_FILTER = "_f", K_HIDE = "_hide", K_WL = "_wl";

function readTableState(sp: URLSearchParams, columns: ColumnDef[], def: ReportDef): TableState {
  let sort: SortState | null = def.default_sort ?? null;
  const s = sp.get(K_SORT);
  if (s !== null) {
    const [key, dir] = s.split(":");
    sort = key ? { key, dir: dir === "asc" ? "asc" : "desc" } : null;
  }
  let filters: Record<string, ColumnFilter> = {};
  try { const f = sp.get(K_FILTER); if (f) filters = JSON.parse(f) as Record<string, ColumnFilter>; } catch { filters = {}; }
  const h = sp.get(K_HIDE);
  const hidden = h !== null ? h.split(",").filter(Boolean) : columns.filter((c) => c.default_hidden).map((c) => c.key);
  return { sort, filters, hidden };
}

function writeTableState(sp: URLSearchParams, t: TableState): URLSearchParams {
  const next = new URLSearchParams(sp);
  if (t.sort) next.set(K_SORT, `${t.sort.key}:${t.sort.dir}`); else next.set(K_SORT, "");
  if (Object.keys(t.filters).length) next.set(K_FILTER, JSON.stringify(t.filters)); else next.delete(K_FILTER);
  next.set(K_HIDE, t.hidden.join(","));
  return next;
}

function PresetBar({ def, params, table, onApply }: { def: ReportDef; params: ParamStrings; table: TableState; onApply: (p: Preset) => void }) {
  const presets = useApi(() => api.presets(def.id), [def.id]);
  const [name, setName] = useState("");
  const [sel, setSel] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const save = async () => {
    if (!name.trim()) return;
    setErr(null);
    const typed = Object.fromEntries(def.params.map((d) => [d.key, stringToValue(d, params[d.key] ?? "")]));
    try {
      await api.createPreset({ report_id: def.id, name: name.trim(), payload: { params: typed, filters: table.filters, sort: table.sort, hidden: table.hidden } });
      setName(""); presets.refetch();
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  const del = async () => {
    if (!sel) return;
    try { await api.deletePreset(Number(sel)); setSel(""); presets.refetch(); } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  const list = presets.data ?? [];
  return (
    <div className="preset-bar">
      <label className="field-inline">Preset
        <select value={sel} onChange={(e) => { setSel(e.target.value); const p = list.find((x) => String(x.id) === e.target.value); if (p) onApply(p); }}>
          <option value="">{list.length ? "Choose…" : "none saved"}</option>
          {list.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
      </label>
      {sel && <button className="btn btn-sm btn-ghost" onClick={del}>Delete</button>}
      <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Save current as…" aria-label="Preset name" onKeyDown={(e) => { if (e.key === "Enter") void save(); }} />
      <button className="btn btn-sm" onClick={() => void save()} disabled={!name.trim()}>Save</button>
      {err && <span className="neg small">{err}</span>}
    </div>
  );
}

function WatchlistFilter({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const lists = useApi(api.watchlists, []);
  return (
    <label className="field-inline">Watchlist only
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">off</option>
        {(lists.data ?? []).map((w) => <option key={w.id} value={w.id}>{w.name} ({w.symbols.length})</option>)}
      </select>
    </label>
  );
}

function ExtraData({ extra }: { extra: Record<string, unknown> | undefined }) {
  if (!extra) return null;
  const tables = Object.entries(extra).filter(([, v]) => Array.isArray(v) && v.length && typeof v[0] === "object") as [string, ReportRow[]][];
  return (
    <>
      {tables.map(([k, rows]) => (
        <Card key={k} title={k.replace(/_/g, " ").replace(/^\w/, (m) => m.toUpperCase())}>
          <DataTable columns={inferColumns(rows)} rows={rows} csvName={k} maxHeight={360} />
        </Card>
      ))}
    </>
  );
}

function ReportView({ def }: { def: ReportDef }) {
  const [sp, setSp] = useSearchParams();
  const applied = useMemo(() => resolveParams(def.params, sp), [def.params, sp]);
  const appliedKey = JSON.stringify(applied);
  const [form, setForm] = useState<ParamStrings>(applied);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => setForm(applied), [appliedKey]);

  const result = useApi(() => api.report(def.id, applied), [def.id, appliedKey]);
  const columns = result.data?.columns?.length ? result.data.columns : def.columns;
  const table = useMemo(() => readTableState(sp, columns, def), [sp, columns, def]);
  const setTable = useCallback((t: TableState) => setSp(writeTableState(sp, t), { replace: true }), [sp, setSp]);

  const wl = sp.get(K_WL) ?? "";
  const lists = useApi(wl ? api.watchlists : null, [wl]);
  const wlSymbols = useMemo(() => new Set(lists.data?.find((w) => String(w.id) === wl)?.symbols ?? []), [lists.data, wl]);
  const rows = useMemo(() => {
    const all = result.data?.rows ?? [];
    return wl ? all.filter((r) => typeof r.symbol === "string" && wlSymbols.has(r.symbol)) : all;
  }, [result.data, wl, wlSymbols]);

  // Chain-based screeners only cover the chain universe; the engine says so in a note (INTEGRATION.md §6).
  const notes = result.data?.notes ?? [];
  const coverage = notes.filter(isCoverageNote);
  const otherNotes = notes.filter((n) => !isCoverageNote(n));

  const run = () => {
    const next = new URLSearchParams(sp);
    for (const d of def.params) next.set(d.key, form[d.key] ?? "");
    setSp(next);
  };
  const setWl = (v: string) => { const next = new URLSearchParams(sp); if (v) next.set(K_WL, v); else next.delete(K_WL); setSp(next, { replace: true }); };
  const applyPreset = (p: Preset) => {
    const next = new URLSearchParams();
    for (const d of def.params) if (d.key in p.payload.params) next.set(d.key, valueToString(d, p.payload.params[d.key]));
    const withTable = writeTableState(next, { sort: p.payload.sort ?? null, filters: p.payload.filters ?? {}, hidden: p.payload.hidden ?? columns.filter((c) => c.default_hidden).map((c) => c.key) });
    if (wl) withTable.set(K_WL, wl);
    setSp(withTable);
  };

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <div className="breadcrumb"><Link to={`/reports?category=${def.category}`}>{def.category}</Link></div>
          <h1 className="page-title">{def.title}</h1>
          <p className="page-desc">{def.description}</p>
        </div>
        {result.data && <AsOf ts={result.data.asof} />}
      </div>
      {def.params.length > 0 && (
        <Card className="params-card">
          <ParamForm defs={def.params} values={form} onChange={setForm} onSubmit={run} busy={result.loading} />
        </Card>
      )}
      {coverage.length > 0 && (
        <div className="warn-box coverage-note"><strong>Coverage:</strong> <span>{coverage.join(" ")}</span></div>
      )}
      {otherNotes.length > 0 && (
        <InfoBox title="Data notes:">
          <ul className="notes">{otherNotes.map((n, i) => <li key={i}>{n}</li>)}</ul>
        </InfoBox>
      )}
      {result.data?.truncated && <div className="warn-box">Showing the first {result.data.rows.length.toLocaleString()} rows — narrow the parameters to see everything.</div>}
      {result.error && <ErrorBox error={result.error} onRetry={result.refetch} />}
      {!result.data && result.loading && <Spinner label="Running report…" />}
      {result.data && (
        <Card className={`flush ${result.loading ? "refetching" : ""}`}>
          <DataTable columns={columns} rows={rows} state={table} onStateChange={setTable} csvName={`${def.id}-${todayNY()}`}
            toolbar={<><WatchlistFilter value={wl} onChange={setWl} /><PresetBar def={def} params={applied} table={table} onApply={applyPreset} /></>} maxHeight="70vh" />
        </Card>
      )}
      <ExtraData extra={result.data?.extra} />
    </div>
  );
}

export function isCoverageNote(note: string): boolean {
  return /chain universe/i.test(note);
}

export default function ReportPage() {
  const { id = "" } = useParams();
  const defs = useApi(api.reportDefs, []);
  return (
    <Async state={defs}>{(list) => {
      const def = list.find((d) => d.id === id);
      return def ? <ReportView key={def.id} def={def} /> : <ErrorBox error={`Unknown report “${id}”.`} />;
    }}</Async>
  );
}
