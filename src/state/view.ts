// What the user is looking at, reported to the engine for AI agents (INTEGRATION.md §10 "Current view"): the route
// (symbol, tab, report), the engine requests whose answers the page shows (collected by useApi inside <ViewScope>),
// the table state and visible rows of its tables (DataTable) and page details. <ViewReporter> sends it on changes.
import { createContext, createElement, ReactNode, useContext, useEffect, useRef } from "react";
import type { ColumnFilter } from "../api/types";

/** One engine request a page made, exactly as sent (path with its query string). */
export interface DataRequest { method: string; path: string; body?: unknown }
/** The user's table state on a request's rows (the same shape presets store) and the rows on screen. */
export interface ViewTable { sort: { key: string; dir: "asc" | "desc" } | null; filters: Record<string, ColumnFilter>; hidden: string[]; symbols?: string[] }
export interface ViewRows { offset: number; limit: number; total: number }
export type ViewDetails = Record<string, string | number | boolean | null>;
export interface ViewData extends DataRequest { table?: ViewTable; rows?: ViewRows }

/** The PUT /api/view body. */
export interface CurrentView {
  route: string; title: string | null; symbol: string | null; tab: string | null; report_id: string | null;
  details: ViewDetails; data: ViewData[];
}

/** The engine keeps at most this many requests per view. */
export const MAX_VIEW_DATA = 20;

/** Route facts from the URL: /s/:sym/:tab, /reports/:id. */
export function describeRoute(pathname: string, search: string): Pick<CurrentView, "route" | "symbol" | "tab" | "report_id"> {
  const parts = pathname.split("/").filter(Boolean).map((p) => decodeURIComponent(p));
  const symbol = parts[0] === "s" && parts[1] ? parts[1].toUpperCase() : null;
  const tab = symbol ? parts[2] ?? "summary" : null;
  const report_id = parts[0] === "reports" && parts[1] ? parts[1] : null;
  return { route: pathname + search, symbol, tab, report_id };
}

// ---------------- the store: what the mounted page parts registered ----------------
const data = new Map<number, DataRequest[]>();
const tables = new Map<number, { requests: DataRequest[]; table: ViewTable; rows: ViewRows }>();
const details = new Map<number, ViewDetails>();
const listeners = new Set<() => void>();
let version = 0;
let nextSlot = 1;
const changed = () => { version += 1; for (const l of listeners) l(); };

const keyOf = (r: DataRequest) => `${r.method} ${r.path} ${r.body === undefined ? "" : JSON.stringify(r.body)}`;

export const viewStore = {
  subscribe(cb: () => void): () => void { listeners.add(cb); return () => { listeners.delete(cb); }; },
  version: (): number => version,
  /** The page's requests (deduplicated, at most MAX_VIEW_DATA, tables attached) and details. */
  snapshot(): { data: ViewData[]; details: ViewDetails } {
    const byKey = new Map<string, ViewData>();
    for (const reqs of data.values()) for (const r of reqs) if (!byKey.has(keyOf(r))) byKey.set(keyOf(r), { ...r });
    for (const t of tables.values()) {
      for (const r of t.requests) {
        const k = keyOf(r);
        byKey.set(k, { ...(byKey.get(k) ?? r), table: t.table, rows: t.rows });
      }
    }
    const merged: ViewDetails = {};
    for (const d of details.values()) Object.assign(merged, d);
    return { data: [...byKey.values()].slice(0, MAX_VIEW_DATA), details: merged };
  },
};

const InScope = createContext(false);

/** The part of the UI whose data is "the current view" (the main content, not the header). */
export function ViewScope({ children }: { children: ReactNode }) {
  return createElement(InScope.Provider, { value: true }, children);
}

function useSlot(): number {
  const ref = useRef(0);
  if (!ref.current) ref.current = nextSlot++;
  return ref.current;
}

function useRegistered<T>(map: Map<number, T>, value: T | null, key: string): void {
  const inScope = useContext(InScope);
  const slot = useSlot();
  const latest = useRef(value);
  latest.current = value;
  useEffect(() => {
    if (!inScope || latest.current === null) return undefined;
    map.set(slot, latest.current);
    changed();
    return () => { map.delete(slot); changed(); };
  }, [inScope, slot, map, key]);
}

/** A data hook's requests (useApi calls this). */
export function useViewData(requests: DataRequest[]): void {
  useRegistered(data, requests.length ? requests : null, JSON.stringify(requests));
}

/** A table's state and visible rows over the rows of `requests` (DataTable calls this). */
export function useViewTable(requests: DataRequest[] | undefined, table: ViewTable, rows: ViewRows): void {
  const value = requests?.length ? { requests, table, rows } : null;
  useRegistered(tables, value, JSON.stringify(value));
}

/** Page details agents should know (e.g. the selected expiry). */
export function useViewDetails(d: ViewDetails): void {
  useRegistered(details, d, JSON.stringify(d));
}
