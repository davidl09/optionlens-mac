// Portfolio page helpers (pure; INTEGRATION.md §11). The engine values and aggregates everything; these only pick
// what to show: a sum in the reporting currency (converted) or per currency, money with its currency, source state
// texts, and the calculator legs of an underlying's positions at their cost basis.
import type { Leg, PortfolioAggregate, PortfolioPosition, PortfolioSource, PortfolioSums } from "../api/types";

const MONEY = new Map<string, Intl.NumberFormat>();

/** "$1,234.56" (USD) / "CA$1,234.56" (CAD); "—" when unknown. */
export function money(v: number | null | undefined, currency: string, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const key = `${currency}:${digits}`;
  let f = MONEY.get(key);
  if (!f) {
    f = new Intl.NumberFormat("en-US", { style: "currency", currency, minimumFractionDigits: digits, maximumFractionDigits: digits });
    MONEY.set(key, f);
  }
  return f.format(v);
}

export interface Shown { currency: string; values: PortfolioSums }

/**
 * What an aggregate shows: the converted sums when every currency could be converted, else the per-currency sums
 * (several entries: no cross-currency total without a broker's FX rate). Empty when nothing is held.
 */
export function shownSums(agg: PortfolioAggregate): Shown[] {
  const cur = Object.keys(agg.by_currency);
  if (!cur.length) return [];
  if (cur.length === 1) return [{ currency: cur[0], values: agg.by_currency[cur[0]] }];
  if (agg.converted.values) return [{ currency: agg.converted.currency, values: agg.converted.values }];
  return cur.map((c) => ({ currency: c, values: agg.by_currency[c] }));
}

/** One line per shown currency, e.g. "$12,345.00" or "$10,000.00 · CA$7,000.00". */
export function sumText(agg: PortfolioAggregate, key: string, digits = 2): string {
  const s = shownSums(agg);
  if (!s.length) return "—";
  return s.map((x) => money(x.values[key] as number | null, x.currency, digits)).join(" · ");
}

/** The first shown value of `key` (for colouring / flashing); null when none. */
export function sumValue(agg: PortfolioAggregate, key: string): number | null {
  const v = shownSums(agg)[0]?.values[key];
  return typeof v === "number" ? v : null;
}

/** How many positions a sum left out because the value was unknown for them. */
export function unknownCount(agg: PortfolioAggregate, key: string): number {
  return shownSums(agg).reduce((n, s) => n + (s.values.unknown?.[key] ?? 0), 0);
}

export const SOURCE_TONE: Record<PortfolioSource["state"], string> = {
  ok: "pos", needs_permission: "warn", not_connected: "neutral", error: "neg", disabled: "neutral", waiting: "neutral", stale: "warn",
};
const STATE_TEXT: Record<PortfolioSource["state"], string> = {
  ok: "connected", needs_permission: "permission needed", not_connected: "not connected", error: "error", disabled: "hidden", waiting: "waiting", stale: "stale",
};
export const sourceStateText = (s: PortfolioSource) => STATE_TEXT[s.state];

/** Whether the page has anything to show or wait for (some source is on and could supply positions). */
export function anySourceUsable(sources: PortfolioSource[]): boolean {
  return sources.some((s) => s.enabled && (s.state === "ok" || s.state === "stale" || s.fetched_at !== null));
}

export const MAX_CALC_LEGS = 8;

/**
 * The calculator legs of an underlying's positions, each at its cost basis as a pinned price (the calculator's
 * "your price": live quotes do not move it). The same contract held in several accounts is one leg (quantities
 * summed, cost averaged by quantity); options are in contracts of 100 shares (other multipliers scaled). Null when
 * there are more legs than the calculator takes or a position has no cost basis.
 */
export function calculatorLegs(positions: PortfolioPosition[]): Leg[] | null {
  const by = new Map<string, { leg: Leg; cost: number }>();
  for (const p of positions) {
    if (p.avg_cost === null || (p.kind !== "stock" && p.kind !== "option")) return null;
    const qty = p.kind === "option" ? (p.qty * p.multiplier) / 100 : p.qty;
    const key = p.kind === "stock" ? "stock" : p.osi ?? `${p.expiry}${p.right}${p.strike}`;
    const cur = by.get(key);
    if (!cur) {
      by.set(key, { leg: p.kind === "stock" ? { kind: "stock", qty, price: p.avg_cost } : { kind: "option", right: p.right!, strike: p.strike!, expiry: p.expiry!, qty, price: p.avg_cost }, cost: qty * p.avg_cost });
      continue;
    }
    cur.leg = { ...cur.leg, qty: cur.leg.qty + qty };
    cur.cost += qty * p.avg_cost;
  }
  const legs = [...by.values()].filter((x) => x.leg.qty !== 0).map((x) => ({ ...x.leg, price: x.cost / x.leg.qty }));
  return legs.length && legs.length <= MAX_CALC_LEGS ? legs : null;
}

/** "−2 C 110 Dec 18 '26" / "200 sh" for a position row. */
export function instrumentText(p: PortfolioPosition): string {
  if (p.kind === "stock") return "Stock";
  if (p.kind === "other") return p.symbol;
  const d = p.expiry ? new Date(`${p.expiry}T12:00:00Z`) : null;
  const when = d ? d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "2-digit", timeZone: "UTC" }) : "";
  return `${p.right === "C" ? "Call" : "Put"} ${p.strike} · ${when}${p.multiplier !== 100 ? ` · ×${p.multiplier}` : ""}`;
}

export const PRICE_SOURCE_TEXT: Record<NonNullable<PortfolioPosition["price_source"]>, string> = {
  live: "Live quote", stored: "Last stored quote / chain", broker: "The broker's own price",
};
