// Strategy-template facts the forms need.
import type { StrategyTemplate } from "../api/types";

/** True when the template places two legs of the same type at different strikes (spreads, wings, butterflies):
 * only then does the width between strikes matter. */
export function templateUsesWidth(t: Pick<StrategyTemplate, "legs"> | undefined): boolean {
  if (!t) return false;
  const byRight = new Map<string, Set<number>>();
  for (const l of t.legs) {
    if (l.kind !== "option" || !l.right) continue;
    const set = byRight.get(l.right) ?? new Set<number>();
    set.add(l.strike_offset_pct ?? 0);
    byRight.set(l.right, set);
  }
  return [...byRight.values()].some((s) => s.size > 1);
}
