// The chain sources (IBKR via the bridge, Questrade via the engine) as one shape, so Settings → Data sources and the
// header show them side by side with the same fields (INTEGRATION.md §6 "Chain precedence": no source is preferred).
import type { BridgeStatus } from "../../electron/shared/api";
import type { FailureCount, QuestradeState, QuestradeStatus } from "../api/types";

export type Tone = "pos" | "warn" | "neg" | "neutral";

export interface SourceView {
  name: "ibkr" | "questrade";
  label: string;
  /** Connection state in words ("connected", "needs attention", ...) and its tone. */
  state: string;
  tone: Tone;
  /** Usable for chains now (connected, not paused / needing attention). */
  active: boolean;
  /** What is wrong or what to do, when something is. */
  problem: string | null;
  /** "real-time" | "delayed …" | null (not known until a chain was fetched). */
  dataQuality: string | null;
  lastChainAt: string | null;
  lastSweepAt: string | null;
  lastSweepDetail: string | null;
  failures: FailureCount[];
  usage: string | null;
}

const n = (x: number) => x.toLocaleString("en-US");

/** "1,141 × 403 code 1016 (missing market-data permission)". */
export function failureText(f: FailureCount): string {
  return `${n(f.count)} × ${f.reason}`;
}

export function ibkrView(b: BridgeStatus): SourceView {
  const connected = b.enabled && b.tws === "connected";
  const state = !b.enabled ? "off" : b.tws === "connected" ? (b.paused ? "paused" : "connected") : b.tws === "connecting" ? "connecting" : "not connected";
  const tone: Tone = !connected ? "neutral" : b.paused ? "warn" : "pos";
  const delayedOnly = b.optionsSubscription === "delayed_only";
  const dataQuality = delayedOnly ? "delayed (no OPRA subscription)" : b.optionsDelayed === null ? null : b.optionsDelayed ? "delayed (no OPRA subscription)" : "real-time";
  return {
    name: "ibkr", label: "IBKR", state, tone, active: connected && !b.paused,
    problem: b.paused ?? (b.enabled && b.tws !== "connected" && b.tws !== "connecting" ? b.lastError ?? "Not connected to IB Gateway / TWS" : null),
    dataQuality, lastChainAt: b.lastChainAt, lastSweepAt: b.lastChainRound?.at ?? null, lastSweepDetail: b.lastChainRound?.detail ?? null,
    failures: b.failures,
    usage: connected ? `${n(b.linesPerBatch)} market-data lines per batch` : null,
  };
}

const QT_STATE: Record<QuestradeState, { label: string; tone: Tone }> = {
  connected: { label: "connected", tone: "pos" },
  disconnected: { label: "not connected", tone: "neutral" },
  expired: { label: "token expired", tone: "warn" },
  error: { label: "error", tone: "neg" },
  needs_attention: { label: "needs attention", tone: "warn" },
};

export function questradeView(s: QuestradeStatus): SourceView {
  const st = QT_STATE[s.state];
  return {
    name: "questrade", label: "Questrade", state: st.label, tone: st.tone, active: s.state === "connected",
    problem: s.state === "connected" || s.state === "disconnected" ? null : s.detail,
    dataQuality: s.delayed === null ? null : s.delayed ? "delayed (no real-time data package)" : "real-time",
    lastChainAt: s.last_chain_at, lastSweepAt: s.last_sweep_at, lastSweepDetail: s.last_sweep_detail,
    failures: s.failures,
    usage: `${n(s.calls_last_hour)} / ${n(s.hour_budget)} calls this hour${s.remaining !== null ? ` · ${n(s.remaining)} left at Questrade` : ""}`,
  };
}
