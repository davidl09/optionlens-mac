// When the updater runs: a check at launch and every 6 hours (the GitHub API allows 60 unauthenticated
// requests per hour per IP; checks are conditional, see release.ts), and a once-a-minute tick that lets
// automatic mode install a ready update when the safe-window policy allows it.
export const CHECK_EVERY_MS = 6 * 60 * 60 * 1000;
export const TICK_EVERY_MS = 60 * 1000;

export interface Schedulable { check(): Promise<void>; tick(): Promise<void> }

/** Starts the schedule; returns a function that stops it. */
export function scheduleUpdates(u: Schedulable, o: { checkEveryMs?: number; tickEveryMs?: number } = {}): () => void {
  void u.check();
  const checks = setInterval(() => void u.check(), o.checkEveryMs ?? CHECK_EVERY_MS);
  const ticks = setInterval(() => void u.tick(), o.tickEveryMs ?? TICK_EVERY_MS);
  return () => { clearInterval(checks); clearInterval(ticks); };
}
