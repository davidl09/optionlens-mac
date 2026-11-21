// Symbol-search ranking: exact symbol, then symbol prefix, then company-name matches.
import type { SymbolRef } from "../api/types";

function rank(q: string, it: SymbolRef): number {
  const sym = it.symbol.toUpperCase();
  const name = it.name.toUpperCase();
  if (sym === q) return 0;
  if (sym.startsWith(q)) return 1;
  if (name.startsWith(q) || name.includes(` ${q}`)) return 2;
  if (sym.includes(q) || name.includes(q)) return 3;
  return 4;
}

/** Stable re-rank of the engine's matches; within a tier shorter symbols first (KO before KOLD). */
export function rankSymbols(query: string, items: SymbolRef[]): SymbolRef[] {
  const q = query.trim().toUpperCase().replace(/^\^/, "");
  if (!q) return items;
  return items
    .map((it, i) => ({ it, i, r: rank(q, it) }))
    .sort((a, b) => a.r - b.r || (a.r === 1 ? a.it.symbol.length - b.it.symbol.length : 0) || a.i - b.i)
    .map((x) => x.it);
}
