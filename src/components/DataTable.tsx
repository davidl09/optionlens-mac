// Generic table for any ColumnDef[] + rows: sort, per-column filters, column visibility,
// pagination, CSV export, symbol/contract links, model-price badges.
import { MouseEvent, ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import type { ColumnDef, ColumnFilter, ReportRow } from "../api/types";
import { toCsv, downloadText } from "../lib/csv";
import { badgeTone, displayScale, fmt, isMissing, isNumericFmt, signClass } from "../lib/format";
import { ModelBadge, NullValue, useClickOutside } from "./ui";

export interface SortState { key: string; dir: "asc" | "desc" }
export interface TableState { sort: SortState | null; filters: Record<string, ColumnFilter>; hidden: string[] }

export function initialTableState(columns: ColumnDef[], sort?: SortState | null): TableState {
  return { sort: sort ?? null, filters: {}, hidden: columns.filter((c) => c.default_hidden).map((c) => c.key) };
}

export interface DataTableProps {
  columns: ColumnDef[];
  rows: ReportRow[];
  state?: TableState;
  onStateChange?: (s: TableState) => void;
  defaultSort?: SortState | null;
  /** Where a row click goes; default `/s/:symbol` when the row has a symbol. `false` disables. */
  rowLink?: ((row: ReportRow) => string | null) | false;
  onRowClick?: (row: ReportRow) => void;
  renderCell?: (col: ColumnDef, row: ReportRow) => ReactNode | undefined;
  csvName?: string;
  toolbar?: ReactNode;
  /** Hide toolbar, filters and pagination (small embedded tables). */
  minimal?: boolean;
  pageSize?: number;
  maxHeight?: number | string;
  emptyText?: string;
  rowClass?: (row: ReportRow) => string | undefined;
}

const LOW_CARDINALITY = 14;
const PAGE_SIZES = [50, 100, 250, 500];

function filterActive(f: ColumnFilter | undefined): boolean {
  if (!f) return false;
  return f.min != null || f.max != null || (f.text != null && f.text !== "") || (f.values != null && f.values.length > 0);
}

function passes(v: unknown, f: ColumnFilter): boolean {
  if (f.min != null || f.max != null) {
    const n = typeof v === "number" ? v : Number(v);
    if (isMissing(v) || !Number.isFinite(n)) return false;
    if (f.min != null && n < f.min) return false;
    if (f.max != null && n > f.max) return false;
  }
  if (f.text) {
    if (isMissing(v) || !String(v).toLowerCase().includes(f.text.toLowerCase())) return false;
  }
  if (f.values && f.values.length) {
    if (!f.values.includes(isMissing(v) ? "" : String(v))) return false;
  }
  return true;
}

function compare(a: unknown, b: unknown): number {
  if (typeof a === "number" && typeof b === "number") return a - b;
  if (typeof a === "boolean" && typeof b === "boolean") return Number(a) - Number(b);
  return String(a).localeCompare(String(b), undefined, { numeric: true });
}

export function applyTable(rows: ReportRow[], state: TableState): ReportRow[] {
  const active = Object.entries(state.filters).filter(([, f]) => filterActive(f));
  let out = active.length ? rows.filter((r) => active.every(([k, f]) => passes(r[k], f))) : rows.slice();
  const s = state.sort;
  if (s) {
    const sign = s.dir === "asc" ? 1 : -1;
    out = out.sort((ra, rb) => {
      const a = ra[s.key], b = rb[s.key];
      const ma = isMissing(a), mb = isMissing(b);
      if (ma || mb) return ma && mb ? 0 : ma ? 1 : -1;  // missing always last
      return sign * compare(a, b);
    });
  }
  return out;
}

function filterSummary(col: ColumnDef, f: ColumnFilter): string {
  const sc = displayScale(col.fmt);
  const parts: string[] = [];
  if (f.min != null) parts.push(`≥ ${+(f.min * sc).toFixed(4)}`);
  if (f.max != null) parts.push(`≤ ${+(f.max * sc).toFixed(4)}`);
  if (f.text) parts.push(`contains “${f.text}”`);
  if (f.values?.length) parts.push(f.values.length <= 3 ? f.values.map((v) => v || "(empty)").join(" / ") : `${f.values.length} values`);
  return `${col.label} ${parts.join(" ")}`;
}

export function Cell({ col, row }: { col: ColumnDef; row: ReportRow }) {
  const v = row[col.key];
  if (isMissing(v)) return <NullValue hint={col.description ? `${col.label}: not available. ${col.description}` : undefined} />;
  switch (col.fmt) {
    case "symbol":
      return <Link className="sym-link" to={`/s/${encodeURIComponent(String(v))}`} onClick={(e) => e.stopPropagation()}>{String(v)}</Link>;
    case "osi": {
      const s = typeof row.symbol === "string" ? row.symbol : null;
      const text = fmt(v, "osi");
      return s ? <Link to={`/s/${encodeURIComponent(s)}/contract/${encodeURIComponent(String(v))}`} onClick={(e) => e.stopPropagation()}>{text}</Link> : <>{text}</>;
    }
    case "badge":
      return <span className={`badge ${badgeTone(v)}`}>{String(v)}</span>;
    case "pct_signed":
      return <span className={signClass(v)}>{fmt(v, col.fmt)}</span>;
    default:
      return <>{fmt(v, col.fmt)}</>;
  }
}

function FilterEditor({ col, rows, filter, onChange }: { col: ColumnDef; rows: ReportRow[]; filter: ColumnFilter | undefined; onChange: (f: ColumnFilter | null) => void }) {
  const numeric = isNumericFmt(col.fmt);
  const distinct = useMemo(() => {
    if (numeric) return null;
    const set = new Set<string>();
    for (const r of rows) {
      set.add(isMissing(r[col.key]) ? "" : String(r[col.key]));
      if (set.size > LOW_CARDINALITY) return null;
    }
    return [...set].sort();
  }, [rows, col.key, numeric]);
  const sc = displayScale(col.fmt);
  const f = filter ?? {};
  const num = (s: string): number | null => (s.trim() === "" || !Number.isFinite(Number(s)) ? null : Number(s) / sc);
  const shown = (n: number | null | undefined) => (n == null ? "" : String(+(n * sc).toFixed(6)));

  return (
    <div className="filter-editor">
      <div className="filter-title">{col.label}{sc !== 1 && <small> (in %)</small>}</div>
      {numeric && (
        <div className="filter-range">
          <input type="number" placeholder="min" aria-label={`${col.label} minimum`} defaultValue={shown(f.min)}
            onChange={(e) => onChange({ ...f, min: num(e.target.value) })} autoFocus />
          <span>to</span>
          <input type="number" placeholder="max" aria-label={`${col.label} maximum`} defaultValue={shown(f.max)}
            onChange={(e) => onChange({ ...f, max: num(e.target.value) })} />
        </div>
      )}
      {!numeric && distinct && (
        <div className="filter-values">
          {distinct.map((d) => {
            const on = f.values?.includes(d) ?? false;
            return (
              <label key={d}>
                <input type="checkbox" checked={on} onChange={() => {
                  const cur = new Set(f.values ?? []);
                  if (on) cur.delete(d); else cur.add(d);
                  onChange({ ...f, values: cur.size ? [...cur] : null });
                }} />
                {d === "" ? <em>(empty)</em> : col.fmt === "bool" ? fmt(d === "true", "bool") : d}
              </label>
            );
          })}
        </div>
      )}
      {!numeric && !distinct && (
        <input type="search" placeholder="contains…" aria-label={`${col.label} contains`} defaultValue={f.text ?? ""} autoFocus
          onChange={(e) => onChange({ ...f, text: e.target.value || null })} />
      )}
      <div className="filter-foot"><button className="btn btn-sm btn-ghost" onClick={() => onChange(null)}>Clear</button></div>
    </div>
  );
}

function Popover({ open, onClose, children, align = "left" }: { open: boolean; onClose: () => void; children: ReactNode; align?: "left" | "right" }) {
  const ref = useRef<HTMLDivElement>(null);
  useClickOutside(ref, onClose, open);
  if (!open) return null;
  return <div ref={ref} className={`popover ${align}`} onClick={(e) => e.stopPropagation()}>{children}</div>;
}

function ColumnsMenu({ columns, hidden, onChange }: { columns: ColumnDef[]; hidden: string[]; onChange: (h: string[]) => void }) {
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  const groups = useMemo(() => {
    const m = new Map<string, ColumnDef[]>();
    for (const c of columns) {
      const g = c.group ?? "General";
      m.set(g, [...(m.get(g) ?? []), c]);
    }
    return [...m.entries()];
  }, [columns]);
  const hiddenSet = new Set(hidden);
  const setGroup = (cols: ColumnDef[], visible: boolean) => {
    const next = new Set(hidden);
    for (const c of cols) { if (visible) next.delete(c.key); else next.add(c.key); }
    onChange([...next]);
  };
  return (
    <div className="pop-anchor">
      <button className="btn btn-sm" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        Columns{hidden.length ? ` (${columns.length - hidden.length}/${columns.length})` : ""}
      </button>
      <Popover open={open} onClose={close} align="right">
        <div className="columns-menu">
          {groups.map(([g, cols]) => {
            const vis = cols.filter((c) => !hiddenSet.has(c.key)).length;
            return (
              <fieldset key={g}>
                <legend>
                  <label>
                    <input type="checkbox" checked={vis === cols.length} ref={(el) => { if (el) el.indeterminate = vis > 0 && vis < cols.length; }}
                      onChange={() => setGroup(cols, vis < cols.length)} />
                    {g}
                  </label>
                </legend>
                {cols.map((c) => (
                  <label key={c.key} title={c.description}>
                    <input type="checkbox" checked={!hiddenSet.has(c.key)} onChange={() => setGroup([c], hiddenSet.has(c.key))} />
                    {c.label}
                  </label>
                ))}
              </fieldset>
            );
          })}
          <div className="filter-foot"><button className="btn btn-sm btn-ghost" onClick={() => onChange([])}>Show all</button></div>
        </div>
      </Popover>
    </div>
  );
}

export function DataTable(props: DataTableProps) {
  const { columns, rows, minimal = false, csvName, toolbar, maxHeight, emptyText = "No rows match.", renderCell, rowClass } = props;
  const navigate = useNavigate();
  const [inner, setInner] = useState<TableState>(() => initialTableState(columns, props.defaultSort));
  const state = props.state ?? inner;
  const setState = useCallback((s: TableState) => { if (props.onStateChange) props.onStateChange(s); else setInner(s); }, [props]);
  const [openFilter, setOpenFilter] = useState<string | null>(null);
  const closeFilter = useCallback(() => setOpenFilter(null), []);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(props.pageSize ?? 100);

  const view = useMemo(() => applyTable(rows, state), [rows, state]);
  useEffect(() => setPage(0), [state.filters, state.sort, rows]);

  const hiddenSet = useMemo(() => new Set(state.hidden), [state.hidden]);
  const visible = useMemo(() => columns.filter((c) => !hiddenSet.has(c.key)), [columns, hiddenSet]);
  const pages = Math.max(1, Math.ceil(view.length / pageSize));
  const pageRows = minimal ? view : view.slice(page * pageSize, page * pageSize + pageSize);
  const activeFilters = columns.filter((c) => filterActive(state.filters[c.key]));

  const rowLink = props.rowLink === false ? null : props.rowLink ?? ((r: ReportRow) => (typeof r.symbol === "string" && r.symbol ? `/s/${encodeURIComponent(r.symbol)}` : null));

  const toggleSort = (key: string) => {
    const cur = state.sort;
    const sort: SortState | null = !cur || cur.key !== key ? { key, dir: "desc" } : cur.dir === "desc" ? { key, dir: "asc" } : null;
    setState({ ...state, sort });
  };
  const setFilter = (key: string, f: ColumnFilter | null) => {
    const filters = { ...state.filters };
    if (f && filterActive(f)) filters[key] = f; else delete filters[key];
    setState({ ...state, filters });
  };
  const exportCsv = () => downloadText(`${csvName ?? "table"}.csv`, toCsv(visible, view));

  const onRow = (r: ReportRow, e: MouseEvent) => {
    if ((e.target as HTMLElement).closest("a,button,input,select")) return;
    if (props.onRowClick) { props.onRowClick(r); return; }
    const to = rowLink?.(r);
    if (to) navigate(to);
  };
  const clickable = Boolean(props.onRowClick || rowLink);

  return (
    <div className="datatable">
      {!minimal && (
        <div className="dt-toolbar">
          <div className="dt-toolbar-left">
            {toolbar}
            <span className="dt-count">{view.length === rows.length ? `${rows.length.toLocaleString()} rows` : `${view.length.toLocaleString()} of ${rows.length.toLocaleString()} rows`}</span>
          </div>
          <div className="dt-toolbar-right">
            <ColumnsMenu columns={columns} hidden={state.hidden} onChange={(hidden) => setState({ ...state, hidden })} />
            <button className="btn btn-sm" onClick={exportCsv} disabled={!view.length} title="Download the filtered rows as CSV">CSV</button>
          </div>
        </div>
      )}
      {activeFilters.length > 0 && (
        <div className="dt-chips">
          {activeFilters.map((c) => (
            <button key={c.key} className="chip" onClick={() => setFilter(c.key, null)} title="Remove filter">{filterSummary(c, state.filters[c.key])} ×</button>
          ))}
          <button className="btn btn-sm btn-ghost" onClick={() => setState({ ...state, filters: {} })}>Clear filters</button>
        </div>
      )}
      <div className="dt-scroll" style={maxHeight !== undefined ? { maxHeight } : undefined}>
        <table className="dt">
          <thead>
            <tr>
              {visible.map((c) => {
                const sorted = state.sort?.key === c.key ? state.sort.dir : null;
                const numeric = isNumericFmt(c.fmt);
                return (
                  <th key={c.key} className={numeric ? "num" : undefined} title={c.description} aria-sort={sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : undefined}>
                    <div className="th-inner">
                      <button className="th-sort" onClick={() => toggleSort(c.key)}>
                        {c.label}<span className="sort-ind">{sorted === "asc" ? "▲" : sorted === "desc" ? "▼" : ""}</span>
                      </button>
                      {!minimal && (
                        <span className="pop-anchor">
                          <button className={`th-filter ${filterActive(state.filters[c.key]) ? "on" : ""}`} aria-label={`Filter ${c.label}`}
                            onClick={() => setOpenFilter(openFilter === c.key ? null : c.key)}>
                            <svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true"><path d="M1.5 2h13l-5 6.2V13l-3 1.5V8.2z" fill="currentColor" /></svg>
                          </button>
                          <Popover open={openFilter === c.key} onClose={closeFilter} align={numeric ? "right" : "left"}>
                            <FilterEditor col={c} rows={rows} filter={state.filters[c.key]} onChange={(f) => setFilter(c.key, f)} />
                          </Popover>
                        </span>
                      )}
                    </div>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {pageRows.map((r, i) => (
              <tr key={i} className={`${clickable ? "clickable" : ""} ${rowClass?.(r) ?? ""}`} onClick={(e) => onRow(r, e)}>
                {visible.map((c, ci) => {
                  const custom = renderCell?.(c, r);
                  return (
                    <td key={c.key} className={isNumericFmt(c.fmt) ? "num" : undefined}>
                      {custom !== undefined ? custom : <Cell col={c} row={r} />}
                      {ci === 0 && r.pricing === "model" && <> <ModelBadge /></>}
                    </td>
                  );
                })}
              </tr>
            ))}
            {!pageRows.length && (
              <tr><td colSpan={Math.max(1, visible.length)} className="dt-empty">{rows.length ? emptyText : "No data."}</td></tr>
            )}
          </tbody>
        </table>
      </div>
      {!minimal && view.length > pageSize && (
        <div className="dt-pager">
          <button className="btn btn-sm" disabled={page === 0} onClick={() => setPage(0)} aria-label="First page">«</button>
          <button className="btn btn-sm" disabled={page === 0} onClick={() => setPage(page - 1)} aria-label="Previous page">‹</button>
          <span>Page {page + 1} / {pages}</span>
          <button className="btn btn-sm" disabled={page >= pages - 1} onClick={() => setPage(page + 1)} aria-label="Next page">›</button>
          <button className="btn btn-sm" disabled={page >= pages - 1} onClick={() => setPage(pages - 1)} aria-label="Last page">»</button>
          <select value={pageSize} onChange={(e) => setPageSize(Number(e.target.value))} aria-label="Rows per page">
            {PAGE_SIZES.map((n) => <option key={n} value={n}>{n} / page</option>)}
          </select>
        </div>
      )}
    </div>
  );
}

/** Columns for ReportRow[] that arrive without ColumnDefs (types.ts gives none for some payloads). */
export function inferColumns(rows: ReportRow[], preferred?: ColumnDef[]): ColumnDef[] {
  if (!rows.length) return preferred ?? [];
  const keys = Object.keys(rows[0]).filter((k) => k !== "pricing");
  const byKey = new Map((preferred ?? []).map((c) => [c.key, c]));
  const sample = (k: string) => rows.find((r) => !isMissing(r[k]))?.[k] ?? null;
  return keys.map((k) => byKey.get(k) ?? { key: k, label: humanize(k), fmt: guessFmt(k, sample(k)) });
}

const ACRONYMS = new Set(["iv", "hv", "oi", "dte", "eps", "pc", "pe", "atm", "otm", "itm", "sma", "rsi", "atr", "ytd", "ev", "pop"]);
function humanize(key: string): string {
  const words = key.split("_").map((w) => (ACRONYMS.has(w) || /^(iv|hv)\d+$/.test(w) ? w.toUpperCase() : w));
  const s = words.join(" ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function guessFmt(key: string, v: unknown): ColumnDef["fmt"] {
  const k = key.toLowerCase();
  if (k === "symbol") return "symbol";
  if (k === "osi") return "osi";
  if (/date$|^date|expiry/.test(k)) return "date";
  if (typeof v === "boolean") return "bool";
  if (typeof v !== "number") return "text";
  if (/^iv|^hv|_iv$|_hv$|vol30|^atm_iv/.test(k) && !/rank|ratio|volume|percentile/.test(k)) return "vol";
  if (/chg|change|ret|return|move|drift|perf/.test(k)) return "pct_signed";
  if (/pct|rank|percentile|yield|_pct$/.test(k)) return "pct";
  if (/ratio/.test(k)) return "ratio";
  if (/market_cap|premium|mkt_cap/.test(k)) return "bigmoney";
  if (/price|close|open|high|low|bid|ask|mid|last|straddle|strike/.test(k)) return "money";
  if (/volume|oi|count|days|^n$/.test(k)) return "int";
  return "num";
}
