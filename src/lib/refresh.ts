// One source-neutral reading of a symbol's on-demand refresh round (GET /api/symbol/{sym}/refresh, INTEGRATION.md §7):
// what the header chip and the empty options views say. A source is named only when it is the one with a problem and
// no other source delivered a chain.
import type { DataSource, RefreshSource, RefreshStatus } from "../api/types";

export const SOURCE_LABELS: Record<string, string> = { ibkr: "IBKR", questrade: "Questrade" };
export const sourceLabel = (name: string) => SOURCE_LABELS[name] ?? name;

export type RefreshSummary =
  /** No round started yet (plain browser before the first answer). */
  | { kind: "none" }
  /** No chain source is connected (neither IB Gateway nor Questrade). */
  | { kind: "no-source" }
  | { kind: "pending"; sources: string[] }
  /** A chain is served; `source` is the served chain's. */
  | { kind: "ok"; source: DataSource | null; delayed: boolean }
  | { kind: "no-options" }
  /** Nothing delivered, not a failure: e.g. IBKR waits for its contract server or market hours. */
  | { kind: "waiting"; text: string; waitingFor: RefreshSource["waiting_for"] }
  | { kind: "failed"; text: string; details: string[] };

/** A source's problem in a few words: "IBKR contract server unavailable", "Questrade: missing market-data permission". */
export function problemText(s: RefreshSource): string {
  const label = sourceLabel(s.name);
  if (s.waiting_for === "contract-server") return `${label} contract server unavailable`;
  if (s.waiting_for === "market-hours") return `${label}: option quotes in market hours only`;
  const meaning = s.reason?.match(/\(([^)]+)\)\s*$/)?.[1];
  return `${label}: ${meaning ?? s.reason ?? s.message ?? s.state}`;
}

/**
 * `bridgeAsked`: the app asked the bridge for IBKR and it has not answered yet — IBKR counts as pending until the
 * round lists it (the bridge reports "pending" itself a moment later).
 */
export function summarize(round: RefreshStatus | null, bridgeAsked = false): RefreshSummary {
  if (round === null || round.started_at === null) return { kind: "none" };
  const st = bridgeAsked && !round.sources.some((s) => s.name === "ibkr")
    ? { ...round, pending: true, sources: [...round.sources, { name: "ibkr", state: "pending" as const, at: round.started_at, message: null, waiting_for: null, reason: null }].sort((a, b) => a.name.localeCompare(b.name)) }
    : round;
  if (st.sources.length === 0) return { kind: "no-source" };
  if (st.pending) return { kind: "pending", sources: st.sources.map((s) => sourceLabel(s.name)) };
  if (st.sources.some((s) => s.state === "ok")) return { kind: "ok", source: st.served?.source ?? null, delayed: st.served?.delayed === true };
  if (st.sources.some((s) => s.state === "no_options")) return { kind: "no-options" };
  const text = st.sources.map(problemText).join("; ");
  if (st.sources.every((s) => s.state === "unavailable")) {
    return { kind: "waiting", text, waitingFor: st.sources.length === 1 ? st.sources[0].waiting_for : null };
  }
  return { kind: "failed", text, details: st.sources.map((s) => `${sourceLabel(s.name)}: ${s.message ?? s.reason ?? s.state}`) };
}
