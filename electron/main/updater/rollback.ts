// Rollback after a bad update. The first launches of a new version are on probation: the version is
// confirmed once the engine is healthy and the app has run for 2 minutes (or quits cleanly after the engine
// was healthy). If the engine is not healthy within 60 s of starting, or two launches end unconfirmed
// (crashes), the previous bundle is restored from its backup and the bad version is skipped from then on.
//
// Loop guard: a rollback is tried once. When the app comes back still on the bad version, or the backup is
// gone, it stops trying and tells the user instead. The restored version has no pending update, so it
// never rolls back itself.
import type { PendingUpdate, UpdateRecord } from "./record";

export const HEALTH_TIMEOUT_MS = 60_000;
export const PROBATION_MS = 2 * 60_000;
/** Unconfirmed launches after which the next launch rolls back. */
export const MAX_UNCONFIRMED_LAUNCHES = 2;

export type LaunchDecision = { kind: "none" } | { kind: "watch" } | { kind: "rollback"; pending: PendingUpdate };

const withSkipped = (skipped: string[], v: string) => (skipped.includes(v) ? skipped : [...skipped, v]);

/** What this launch of `current` means for the update on probation (pure: returns the new record). */
export function onLaunch(rec: UpdateRecord, current: string, logFile: string): { rec: UpdateRecord; decision: LaunchDecision } {
  const p = rec.pending;
  if (!p) return { rec, decision: { kind: "none" } };
  if (p.version !== current) {
    let notice = rec.notice;
    if (current === p.previousVersion) {
      notice = p.rollingBack
        ? `OptionLens ${p.version} did not start correctly, so ${current} was restored. ${p.version} will not be installed automatically.`
        : `The update to ${p.version} could not be installed (details: ${logFile}).`;
    }
    return { rec: { ...rec, pending: null, notice, skipped: p.rollingBack ? withSkipped(rec.skipped, p.version) : rec.skipped }, decision: { kind: "none" } };
  }
  if (p.rollingBack) {
    return { rec: { ...rec, pending: null, notice: `OptionLens ${current} did not start correctly and restoring ${p.previousVersion} failed. Reinstall with the one-line installer if problems continue.` }, decision: { kind: "none" } };
  }
  if (p.launches >= MAX_UNCONFIRMED_LAUNCHES) return { rec, decision: { kind: "rollback", pending: p } };
  return { rec: { ...rec, pending: { ...p, launches: p.launches + 1 } }, decision: { kind: "watch" } };
}

/** The rollback is starting: mark it (loop guard) and skip the bad version. */
export function rollbackStarted(rec: UpdateRecord): UpdateRecord {
  if (!rec.pending) return rec;
  return { ...rec, pending: { ...rec.pending, rollingBack: true }, skipped: withSkipped(rec.skipped, rec.pending.version) };
}

/** The rollback cannot happen (no backup, app not writable): give up and say so. */
export function rollbackImpossible(rec: UpdateRecord, why: string): UpdateRecord {
  if (!rec.pending) return rec;
  return { ...rec, pending: null, skipped: withSkipped(rec.skipped, rec.pending.version),
    notice: `OptionLens ${rec.pending.version} did not start correctly, and ${rec.pending.previousVersion} could not be restored: ${why}` };
}

export function confirmed(rec: UpdateRecord): UpdateRecord {
  return rec.pending ? { ...rec, pending: null } : rec;
}

export interface ProbationOptions {
  onRollback: () => void;
  onConfirm: () => void;
  healthTimeoutMs?: number;
  probationMs?: number;
}

/** Watches one launch on probation: arm with start(), feed it the engine's progress. */
export class Probation {
  private healthy = false;
  private settled = false;
  private elapsed = false;
  private healthTimer: ReturnType<typeof setTimeout> | null = null;
  private probationTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly o: ProbationOptions) {}

  start(): void {
    this.probationTimer = setTimeout(() => { this.elapsed = true; if (this.healthy) this.settle("confirm"); }, this.o.probationMs ?? PROBATION_MS);
  }

  /** The engine process is starting (the 60 s health deadline runs from here, after any DB backup). */
  engineStarting(): void {
    if (this.settled || this.healthy || this.healthTimer) return;
    this.healthTimer = setTimeout(() => { if (!this.healthy) this.settle("rollback"); }, this.o.healthTimeoutMs ?? HEALTH_TIMEOUT_MS);
  }

  engineHealthy(): void {
    if (this.settled) return;
    this.healthy = true;
    if (this.healthTimer) { clearTimeout(this.healthTimer); this.healthTimer = null; }
    if (this.elapsed) this.settle("confirm");
  }

  /** The user quits: a launch whose engine became healthy counts as good. */
  quitting(): void {
    if (!this.settled && this.healthy) this.settle("confirm");
  }

  private settle(how: "confirm" | "rollback"): void {
    if (this.settled) return;
    this.settled = true;
    if (this.healthTimer) clearTimeout(this.healthTimer);
    if (this.probationTimer) clearTimeout(this.probationTimer);
    if (how === "confirm") this.o.onConfirm(); else this.o.onRollback();
  }
}
