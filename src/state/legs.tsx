// Calculator legs shared between the Chain and Calculator tabs of one symbol (and handed over by the Portfolio page).
import { createContext, ReactNode, useCallback, useContext, useMemo, useState } from "react";
import type { Leg } from "../api/types";

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

export function describeLeg(l: Leg): string {
  const q = l.qty > 0 ? `+${l.qty}` : String(l.qty);
  return l.kind === "stock" ? `${q} shares` : `${q} ${l.right} ${l.strike} ${l.expiry}`;
}
