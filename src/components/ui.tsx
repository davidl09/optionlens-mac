// Small presentational primitives.
import { createContext, ReactNode, RefObject, useContext, useEffect, useRef } from "react";
import type { DataSource } from "../api/types";
import type { ApiState } from "../api/useApi";
import { fmt, fmtDateTime, NULL_TEXT } from "../lib/format";

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

/** Collapsed detail (e.g. the precise method behind plain-language notes). */
export function Disclosure({ summary, children }: { summary: ReactNode; children: ReactNode }) {
  return <details className="disclosure"><summary>{summary}</summary><div className="disclosure-body">{children}</div></details>;
}

/** Notes in plain words as a list; the precise method (when there is one) collapsed underneath. */
export function NotesBox({ notes, method, title = "Data notes:" }: { notes: string[]; method?: string[]; title?: string }) {
  if (!notes.length && !method?.length) return null;
  return (
    <InfoBox title={title}>
      {notes.length > 0 && <ul className="notes">{notes.map((n, i) => <li key={i}>{n}</li>)}</ul>}
      {method && method.length > 0 && (
        <Disclosure summary="Method">
          <ul className="notes">{method.map((n, i) => <li key={i}>{n}</li>)}</ul>
        </Disclosure>
      )}
    </InfoBox>
  );
}

/** Why a list or table is empty, in place of a bare "No data." */
export function EmptyNotice({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`empty-notice ${className}`} role="status">{children}</div>;
}

/** A file path that wraps only after a separator, never inside a folder name. */
export function PathText({ path }: { path: string }) {
  const parts = path.split(/(?<=[/\\])/);
  return <code className="path">{parts.map((p, i) => <span key={i}>{p}{i < parts.length - 1 && <wbr />}</span>)}</code>;
}

/** A progress bar with its label beside it (never on top of the fill). */
export function Progress({ value, label, title }: { value: number; label?: ReactNode; title?: string }) {
  const v = Math.max(0, Math.min(1, value));
  return (
    <div className="progress-row" title={title}>
      <div className="progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(v * 100)}><div style={{ width: `${Math.round(v * 100)}%` }} /></div>
      <span className="progress-label">{label ?? fmt(v, "pct")}</span>
    </div>
  );
}

export function Popover({ open, onClose, children, align = "left" }: { open: boolean; onClose: () => void; children: ReactNode; align?: "left" | "right" }) {
  const ref = useRef<HTMLDivElement>(null);
  useClickOutside(ref, onClose, open);
  if (!open) return null;
  return <div ref={ref} className={`popover ${align}`} onClick={(e) => e.stopPropagation()}>{children}</div>;
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
  questrade: { label: "Questrade", tone: "pos", title: "Questrade market data, fetched by the engine with your Questrade API token" },
  vendor: { label: "vendor", tone: "warn", title: "Third-party vendor data (may be indicative)" },
  reconstructed: { label: "reconstructed", tone: "warn", title: "Rebuilt from historical daily bars: no bid/ask/OI; IV and delta computed from the close" },
  model: { label: "model", tone: "warn", title: "Priced with a Black-Scholes model" },
  cboe: { label: "CBOE", tone: "neutral", title: "CBOE" },
  nasdaq: { label: "Nasdaq", tone: "neutral", title: "Nasdaq public data" },
  live: { label: "live quotes", tone: "neutral", title: "Built from live quotes: the latest minutes, until the source's own bars for them arrive" },
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
      {delayed && <span className="badge info source-badge" title="15-minute delayed data (no real-time subscription for this contract class)">delayed</span>}
    </>
  );
}

export const AFTER_HOURS_TEXT = "Last session's close (collected after hours)";

/** Values collected after their session ended (IBKR frozen data: evening, weekend, before the open). Neutral: honest, not a warning. */
export function AfterHoursBadge({ show }: { show: boolean | undefined }) {
  if (!show) return null;
  return (
    <span className="badge neutral source-badge after-hours" title={`${AFTER_HOURS_TEXT}: collected from IBKR's frozen data after the session ended. Option quotes are the closing ones; a stock's last price can be an extended-hours trade.`}>
      {AFTER_HOURS_TEXT}
    </span>
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
