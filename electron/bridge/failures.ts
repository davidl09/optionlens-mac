// Failure reasons of the bridge's IBKR work, grouped and counted over the last 24 h (Settings → Data sources shows the
// most frequent ones, e.g. "12 × TWS error 200: No security definition has been found for the request").

export const FAILURE_WINDOW_MS = 24 * 3600_000;
const MAX_EVENTS = 50_000;

export interface FailureCount { reason: string; count: number }

/** The grouping key of a failure message: the symbol it was about is replaced, so the same reason groups. */
export function failureReason(message: string, symbol?: string): string {
  let r = message.trim();
  if (symbol) r = r.split(symbol).join("…");
  return r.length > 160 ? `${r.slice(0, 157)}…` : r;
}

export class FailureLog {
  private events: { at: number; reason: string }[] = [];
  constructor(private readonly now: () => number) {}

  record(message: string, symbol?: string): void {
    this.events.push({ at: this.now(), reason: failureReason(message, symbol) });
    if (this.events.length > MAX_EVENTS) this.events.splice(0, this.events.length - MAX_EVENTS);
  }

  /** The `n` most frequent reasons of the window, most frequent first (ties: the most recent first). */
  top(n = 3): FailureCount[] {
    const since = this.now() - FAILURE_WINDOW_MS;
    this.events = this.events.filter((e) => e.at >= since);
    const counts = new Map<string, { count: number; last: number }>();
    for (const e of this.events) {
      const c = counts.get(e.reason) ?? { count: 0, last: 0 };
      c.count += 1;
      c.last = e.at;
      counts.set(e.reason, c);
    }
    return [...counts].sort((a, b) => b[1].count - a[1].count || b[1].last - a[1].last).slice(0, n)
      .map(([reason, c]) => ({ reason, count: c.count }));
  }
}
