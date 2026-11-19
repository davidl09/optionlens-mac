// Conversion between ParamDef values and their URL-string form.
import type { ParamDef } from "../api/types";

export type ParamStrings = Record<string, string>;

export function defaultString(def: ParamDef): string {
  return valueToString(def, def.default);
}

export function valueToString(def: ParamDef, v: unknown): string {
  if (v === null || v === undefined) return "";
  if (Array.isArray(v)) return v.map(String).join(",");
  if (def.type === "bool") return v === true || v === "true" || v === "1" ? "true" : "false";
  return String(v);
}

export function stringToValue(def: ParamDef, s: string): unknown {
  if (s === "") return null;
  switch (def.type) {
    case "int": case "number": { const n = Number(s); return Number.isFinite(n) ? n : null; }
    case "bool": return s === "true" || s === "1";
    case "symbols": return parseSymbols(s);
    case "watchlist": { const n = Number(s); return Number.isFinite(n) ? n : null; }
    default: return s;
  }
}

export function parseSymbols(s: string): string[] {
  return s.split(/[\s,;]+/).map((x) => x.trim().toUpperCase().replace(/^\^/, "")).filter(Boolean);
}

/** Effective string params: defaults overridden by `source` (e.g. URL query). */
export function resolveParams(defs: ParamDef[], source: URLSearchParams): ParamStrings {
  const out: ParamStrings = {};
  for (const d of defs) out[d.key] = source.has(d.key) ? source.get(d.key) ?? "" : defaultString(d);
  return out;
}
