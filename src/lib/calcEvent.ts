// The calculator's earnings wording (pure): the note above the payoff chart and the IVs a leg is valued with when
// they differ from its entered IV (the engine's earnings IV crush, CalcResult.event / CalcLeg.iv_eval / iv_expiry).
import type { CalcEvent, CalcLeg } from "../api/types";

const pct1 = (x: number) => `${(x * 100).toFixed(1)}%`;
const vol0 = (x: number) => `${Math.round(x * 100)}%`;

/** The IV after earnings of the latest-expiring leg the crush applies to (else the chain's ex-event IV). */
export function ivAfterEarnings(ev: CalcEvent, legs: CalcLeg[]): number | null {
  const crushed = legs.filter((l) => l.iv_base != null && l.expiry).sort((a, b) => (a.expiry! < b.expiry! ? 1 : -1));
  return crushed[0]?.iv_base ?? ev.base_iv;
}

/** "Earnings 2026-12-02 (implied move ±12.8%): IV crush modelled — back-month IV after earnings 42%" and the like. */
export function earningsNote(ev: CalcEvent, legs: CalcLeg[]): string {
  const head = `Earnings ${ev.earnings_date ?? ev.reaction_date}${ev.implied_move_pct != null ? ` (implied move ±${pct1(ev.implied_move_pct)})` : ""}`;
  if (ev.applied) {
    const after = ivAfterEarnings(ev, legs);
    return `${head}: IV crush modelled${after != null ? ` — back-month IV after earnings ${vol0(after)}` : ""}`;
  }
  if (ev.implied_move_pct != null) return `${head}: IV crush off — every leg keeps its IV through the report`;
  return `${head}: IV crush not modelled — ${ev.reason ?? "no estimate"}`;
}

const DIFF = 0.0005;   // 0.05 vol pt: below this the used IV is the entered one

/** "at 2026-12-04: 42.3%"-style notes for the dates a leg is valued at an IV other than its entered IV (+ shift). */
export function legIvNotes(leg: CalcLeg, ivShift: number, evalDate: string, expiryUsed: string | null): string[] {
  if (leg.kind !== "option" || leg.iv == null) return [];
  const entered = leg.iv + ivShift;
  const out: string[] = [];
  if (leg.iv_eval != null && Math.abs(leg.iv_eval - entered) > DIFF) out.push(`at ${evalDate}: ${pct1(leg.iv_eval)}`);
  if (leg.iv_expiry != null && Math.abs(leg.iv_expiry - entered) > DIFF && expiryUsed !== evalDate) out.push(`at ${expiryUsed ?? "expiry"}: ${pct1(leg.iv_expiry)}`);
  if (out.length && leg.base_floored) out.push("ex-earnings IV floored");
  return out;
}
