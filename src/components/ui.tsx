// Small presentational primitives.
import { createContext, ReactNode, RefObject, useContext, useEffect } from "react";
import type { DataSource } from "../api/types";
import type { ApiState } from "../api/useApi";
import { fmtDateTime, NULL_TEXT } from "../lib/format";

export function Spinner({ label = "Loading…" }: { label?: string }) {
  return <div className="spinner" role="status"><span className="spinner-dot" />{label}</div>;
}

export function ErrorBox({ error, onRetry }: { error: Error | string; onRetry?: () => void }) {
  const msg = typeof error === "string" ? error : error.message;
  return (
    <div className="error-box" role="alert">
      <strong>Couldn’t load this.</strong> <span>{msg}</span>
      {onRetry && <button className="btn btn-sm" onClick={onRetry}>Retry</button>}
    </div>
  );
}

export function InfoBox({ children, title }: { children: ReactNode; title?: string }) {
  return <div className="info-box">{title && <strong>{title} </strong>}{children}</div>;
}

export const DEFAULT_NULL_HINT = "Not available yet — needs more collected history, or the source does not provide it.";

export function NullValue({ hint = DEFAULT_NULL_HINT }: { hint?: string }) {
  return <span className="null-value" title={hint} aria-label={`not available: ${hint}`}>{NULL_TEXT}</span>;
}

export function AsOf({ ts, label = "as of" }: { ts: string | null | undefined; label?: string }) {
  return (
    <span className="asof" title="Time of the most recent data stored by the engine.">
      {label} {ts ? fmtDateTime(ts) : <NullValue hint="No data collected yet." />}
    </span>
  );
}

const SOURCES: Record<DataSource, { label: string; tone: "pos" | "warn" | "neutral"; title: string }> = {
  ibkr: { label: "IBKR", tone: "pos", title: "Interactive Brokers market data via the bridge" },
  vendor: { label: "vendor", tone: "warn", title: "Third-party vendor data (may be indicative)" },
  reconstructed: { label: "reconstructed", tone: "warn", title: "Rebuilt from historical daily bars: no bid/ask/OI; IV and delta computed from the close" },
  model: { label: "model", tone: "warn", title: "Priced with a Black-Scholes model" },
  cboe: { label: "CBOE", tone: "neutral", title: "CBOE" },
  nasdaq: { label: "Nasdaq", tone: "neutral", title: "Nasdaq public data" },
};

export function isDataSource(v: unknown): v is DataSource {
  return typeof v === "string" && v in SOURCES;
}

/** Where the data came from; `delayed` adds a delayed marker (IBKR without a real-time subscription). */
export function SourceBadge({ source, delayed = false }: { source: DataSource | null | undefined; delayed?: boolean }) {
  if (!source && !delayed) return null;
  const s = source ? SOURCES[source] : null;
  return (
    <>
      {s && <span className={`badge ${s.tone} source-badge`} title={s.title}>{s.label}</span>}
      {delayed && <span className="badge warn source-badge" title="15-minute delayed data (no real-time subscription for this contract class)">delayed</span>}
    </>
  );
}

export function ModelBadge() {
  return <span className="badge warn" title="Priced with a Black-Scholes model (IV proxy) because stored option prices were not available for this date.">model</span>;
}

export function Card({ title, actions, children, className = "", subtitle }: { title?: ReactNode; subtitle?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header className="card-head">
          <div className="card-titles">{title && <h3>{title}</h3>}{subtitle && <span className="card-sub">{subtitle}</span>}</div>
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      <div className="card-body">{children}</div>
    </section>
  );
}

export function Segmented<T extends string | number>({ value, options, onChange, size = "sm", label }: {
  value: T; options: { value: T; label: ReactNode; title?: string }[]; onChange: (v: T) => void; size?: "sm" | "md"; label?: string;
}) {
  return (
    <div className={`segmented ${size}`} role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button key={String(o.value)} role="radio" aria-checked={o.value === value} title={o.title}
          className={o.value === value ? "active" : ""} onClick={() => onChange(o.value)}>{o.label}</button>
      ))}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

/** How Async shows a failed query: a page provides a view for the errors it understands (null = ErrorBox). */
export type ErrorView = (error: Error, retry: () => void) => ReactNode | null;
export const AsyncErrorView = createContext<ErrorView>(() => null);

/** Spinner on first load, error box (or the page's AsyncErrorView) on failure, stale data dimmed while refetching. */
export function Async<T>({ state, children }: { state: ApiState<T>; children: (data: T) => ReactNode }) {
  const errorView = useContext(AsyncErrorView);
  if (state.error && !state.loading) return errorView(state.error, state.refetch) ?? <ErrorBox error={state.error} onRetry={state.refetch} />;
  if (state.data === null) return state.loading ? <Spinner /> : null;
  return <div className={state.loading ? "refetching" : undefined}>{children(state.data)}</div>;
}

export function useClickOutside(ref: RefObject<HTMLElement>, onOutside: () => void, active: boolean): void {
  useEffect(() => {
    if (!active) return undefined;
    const onDown = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) onOutside(); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onOutside(); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [ref, onOutside, active]);
}
