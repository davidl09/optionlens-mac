// The engine's on-demand refresh rounds (INTEGRATION.md §7), stateful like the engine: POST /api/symbol/{sym}/refresh
// starts a round (with Questrade when a test sets `questradeRefresh`), the fake bridge reports to
// /api/ingest/refresh-result, GET reads the round.
import type { RefreshSource, RefreshStatus } from "../../src/api/types";

const AT = "2026-11-19T15:45:00-05:00";
const rounds = new Map<string, RefreshStatus>();
/** What Questrade's part of a new round is (null: Questrade not connected). */
let questrade: Partial<RefreshSource> | null = null;

export function resetRefresh(q: Partial<RefreshSource> | null = null): void {
  rounds.clear();
  questrade = q;
}

const BRIDGE_STATES: Record<string, RefreshSource["state"]> = {
  pending: "pending", refreshed: "ok", fresh: "ok", "no-options": "no_options", unavailable: "unavailable", failed: "failed",
};

function finish(r: RefreshStatus): RefreshStatus {
  r.pending = r.sources.some((s) => s.state === "pending");
  const ok = r.sources.find((s) => s.state === "ok");
  r.served = ok ? { source: ok.name === "questrade" ? "questrade" : "ibkr", fetched_at: AT, delayed: false } : null;
  return r;
}

const attempt = (name: string, o: Partial<RefreshSource>): RefreshSource =>
  ({ name, state: "ok", at: AT, message: null, waiting_for: null, reason: null, ...o });

export function start(sym: string): RefreshStatus {
  const r: RefreshStatus = { symbol: sym, started_at: AT, pending: false, sources: questrade ? [attempt("questrade", questrade)] : [], served: null };
  rounds.set(sym, r);
  return finish(r);
}

export function status(sym: string): RefreshStatus {
  return rounds.get(sym) ?? { symbol: sym, started_at: null, pending: false, sources: [], served: null };
}

export function report(body: { symbol: string; status: string; message?: string | null; waiting_for?: RefreshSource["waiting_for"] }): RefreshStatus {
  const sym = body.symbol.toUpperCase();
  const r = rounds.get(sym) ?? start(sym);
  const a = attempt("ibkr", { state: BRIDGE_STATES[body.status], message: body.message ?? null, waiting_for: body.waiting_for ?? null });
  r.sources = [...r.sources.filter((s) => s.name !== "ibkr"), a].sort((x, y) => x.name.localeCompare(y.name));
  return finish(r);
}
