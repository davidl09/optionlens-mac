// Plain-language status helpers: job results, the IBKR pill, the next chain-collection window.
import type { BridgeStatus } from "../../electron/shared/api";
import type { JobRun } from "../api/types";

export type JobState = "ok" | "warning" | "failed" | "running" | "unknown";

/** A job run's result; a run that stored nothing (an EOD day with no closing data) is a warning, not OK. */
export function jobState(j: Pick<JobRun, "ok" | "finished_at" | "detail">): JobState {
  const stored0 = /\bnothing archived\b|\b0 option_eod rows, 0 bars\b/i.test(j.detail ?? "");
  if (j.ok === null) return j.finished_at ? "unknown" : "running";
  if (stored0) return "warning";
  return j.ok ? "ok" : "failed";
}

/** Job details for people: a JSON object (an engine that sends raw progress) as "key value" pairs. */
export function readableDetail(detail: string | null): string | null {
  if (!detail) return detail;
  const t = detail.trim();
  if (!/^[{[]/.test(t)) return detail;
  try {
    const v: unknown = JSON.parse(t);
    if (v && typeof v === "object" && !Array.isArray(v)) {
      return Object.entries(v as Record<string, unknown>).map(([k, x]) => `${k.replace(/_/g, " ")} ${typeof x === "number" ? (+x.toPrecision(6)).toLocaleString("en-US", { maximumFractionDigits: 6 }) : String(x)}`).join(", ");
    }
  } catch { /* not JSON: shown as sent */ }
  return detail;
}

const isDelayedNote = (n: string) => /delayed/i.test(n);

/** "Populating last session's data: 340/1145" while the bridge fills the last session after hours, else null. */
export function populateText(b: BridgeStatus | null | undefined): string | null {
  const p = b?.populate;
  return p ? `Populating last session's data: ${p.done.toLocaleString("en-US")}/${p.total.toLocaleString("en-US")}` : null;
}

/** The header pill in plain words: "IBKR connected · delayed data", "IBKR not connected", ... */
export function bridgePillLabel(b: BridgeStatus): string {
  if (!b.enabled) return "IBKR off";
  if (b.tws === "connecting") return "IBKR connecting…";
  if (b.tws !== "connected") return "IBKR not connected";
  if (b.paused) return "IBKR connected · paused";
  return b.notes.some(isDelayedNote) ? "IBKR connected · delayed data" : "IBKR connected";
}

/** Chain collection by the bridge: 09:00–20:00 ET on weekdays (market holidays aside). */
export const COLLECTION_START_H = 9;
export const COLLECTION_END_H = 20;

const NY_PARTS = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", hour: "2-digit", hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit" });
const WD = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/**
 * When the bridge next collects chains: null while a window is open (it is collecting now), else the start of the
 * next weekday window as "Mon 09:00 ET" (holidays are not known here, so a holiday Monday reads as Monday).
 */
export function nextCollectionWindow(now: Date): string | null {
  const p = Object.fromEntries(NY_PARTS.formatToParts(now).map((x) => [x.type, x.value]));
  const wd = WD.indexOf(p.weekday);
  const hour = Number(p.hour);
  const weekday = wd >= 1 && wd <= 5;
  if (weekday && hour >= COLLECTION_START_H && hour < COLLECTION_END_H) return null;
  let d = wd;
  let ahead = weekday && hour < COLLECTION_START_H ? 0 : 1;
  for (; ; ahead += 1) { d = (wd + ahead) % 7; if (d >= 1 && d <= 5) break; }
  const when = ahead === 0 ? "today" : ahead === 1 ? "tomorrow" : WD[d];
  return `${when} 09:00 ET`;
}
