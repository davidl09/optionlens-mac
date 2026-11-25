// Calculator legs shared between the Chain and Calculator tabs of one symbol (and handed over by the Portfolio page).
import { createContext, ReactNode, useCallback, useContext, useMemo, useState } from "react";
import type { Leg, Right } from "../api/types";

interface LegsState { legs: Leg[]; setLegs: (legs: Leg[]) => void; addLeg: (leg: Leg) => void }

const LegsCtx = createContext<LegsState | null>(null);

/** `initial`: legs to start with (e.g. the Portfolio's "Open in calculator": positions at their cost basis, pinned). */
export function LegsProvider({ children, initial }: { children: ReactNode; initial?: Leg[] }) {
  const [legs, setLegs] = useState<Leg[]>(() => initial ?? []);
  const addLeg = useCallback((leg: Leg) => setLegs((ls) => [...ls, leg]), []);
  const value = useMemo(() => ({ legs, setLegs, addLeg }), [legs, addLeg]);
  return <LegsCtx.Provider value={value}>{children}</LegsCtx.Provider>;
}

export function useLegs(): LegsState {
  const v = useContext(LegsCtx);
  if (!v) throw new Error("useLegs must be used inside LegsProvider");
  return v;
}

/** One option contract's key in `heldQty` (expiry, strike, right). */
export function contractKey(expiry: string, strike: number, right: Right): string {
  return `${expiry}|${strike}|${right}`;
}

/** Net quantity per option contract over the legs (buys positive, sells negative); contracts netting to 0 left out. */
export function heldQty(legs: Leg[]): Map<string, number> {
  const out = new Map<string, number>();
  for (const l of legs) {
    if (l.kind !== "option" || !l.expiry || l.strike == null || !l.right) continue;
    const k = contractKey(l.expiry, l.strike, l.right);
    const q = (out.get(k) ?? 0) + l.qty;
    if (q === 0) out.delete(k); else out.set(k, q);
  }
  return out;
}

export function describeLeg(l: Leg): string {
  const q = l.qty > 0 ? `+${l.qty}` : String(l.qty);
  return l.kind === "stock" ? `${q} shares` : `${q} ${l.right} ${l.strike} ${l.expiry}`;
}
