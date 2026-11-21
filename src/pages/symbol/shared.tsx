// Pieces shared by several symbol tabs.
import { useParams, useSearchParams } from "react-router-dom";
import type { ColumnDef, ExpiryInfo, ReportRow, TradeIdea } from "../../api/types";
import { todayNY } from "../../lib/dates";
import { fmt, MONTHS } from "../../lib/format";

export function useSym(): string {
  const { sym = "" } = useParams();
  return sym.toUpperCase();
}

/** A single query-string value with a setter that keeps the other params (replace history). */
export function useQueryValue(key: string, fallback: string | null = null): [string | null, (v: string | null) => void] {
  const [sp, setSp] = useSearchParams();
  const value = sp.get(key) ?? fallback;
  const set = (v: string | null) => {
    const next = new URLSearchParams(sp);
    if (v === null || v === "") next.delete(key); else next.set(key, v);
    setSp(next, { replace: true });
  };
  return [value, set];
}

export function shortDate(iso: string): string {
  const [y, m, d] = iso.split("-");
  return `${MONTHS[Number(m) - 1]} ${Number(d)}${y !== String(new Date().getFullYear()) ? ` ’${y.slice(2)}` : ""}`;
}

/** "Nov 20" for an IV30 as-of date that is not today (NY), else null (today's value needs no date). */
export function ivAsOf(asof: string | null): string | null {
  return asof && asof !== todayNY() ? shortDate(asof) : null;
}

export function ExpiryPills({ expiries, value, onChange, allowAll = false }: {
  expiries: ExpiryInfo[]; value: string | null; onChange: (expiry: string) => void; allowAll?: boolean;
}) {
  return (
    <div className="pills" role="tablist" aria-label="Expiry">
      {allowAll && <button role="tab" aria-selected={value === "all"} className={`pill ${value === "all" ? "active" : ""}`} onClick={() => onChange("all")}>All</button>}
      {expiries.map((e) => (
        <button key={e.expiry} role="tab" aria-selected={value === e.expiry} title={`${e.expiry} · ${e.dte} DTE${e.is_monthly ? " · monthly" : ""} · ATM IV ${fmt(e.atm_iv, "vol")}`}
          className={`pill ${value === e.expiry ? "active" : ""} ${e.is_monthly ? "monthly" : ""}`} onClick={() => onChange(e.expiry)}>
          {shortDate(e.expiry)}<span className="pill-sub">{e.dte}d</span>
        </button>
      ))}
    </div>
  );
}

export const IDEA_COLUMNS: ColumnDef[] = [
  { key: "label", label: "Strategy", fmt: "text" },
  { key: "family", label: "Family", fmt: "badge", default_hidden: true },
  { key: "legs", label: "Legs", fmt: "text" },
  { key: "expiry", label: "Expiry", fmt: "date" },
  { key: "cost", label: "Cost / credit", fmt: "money", description: "Net debit (+) or credit (−) per position" },
  { key: "max_profit", label: "Max profit", fmt: "money", description: "Empty = unlimited" },
  { key: "max_loss", label: "Max loss", fmt: "money", description: "Empty = unlimited" },
  { key: "pop", label: "PoP", fmt: "pct", description: "Probability of profit at expiry (lognormal, IV)" },
  { key: "ev_hv", label: "EV (HV)", fmt: "money", description: "Expected value using 30-day historical volatility" },
  { key: "edge", label: "Edge", fmt: "pct_signed", description: "EV(HV) ÷ max loss" },
  { key: "note", label: "Note", fmt: "text" },
];

export const ideasAsRows = (ideas: TradeIdea[]) => ideas as unknown as ReportRow[];
