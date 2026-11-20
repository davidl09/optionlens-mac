// IBKR historical-data pacing gate. Every historical request the bridge makes is for daily bars. IBKR's
// hard pacing limits (60 requests per 10 minutes, identical requests within 15 s, 6 per contract within
// 2 s) apply to bars of 30 seconds or less; daily bars are only soft-throttled. So:
//   - requests are serialized by the scheduler; starts are ≥ the current spacing apart (default 1 s),
//   - a real pacing violation reported by TWS backs off exponentially (30 s → 60 s → … ≤ 10 min) and
//     doubles the spacing (≤ 10 s); every `relaxAfter` successes in a row halve it again, down to the base.
export interface Gate { readyAt(): number }

export interface PacerOptions {
  now?: () => number;
  minSpacingMs?: number;
  maxSpacingMs?: number;
  baseBackoffMs?: number;
  maxBackoffMs?: number;
  /** Successes in a row after which the spacing is halved again. */
  relaxAfter?: number;
}

export class HistoricalPacer implements Gate {
  private readonly now: () => number;
  private readonly minSpacing: number;
  private readonly maxSpacing: number;
  private readonly baseBackoff: number;
  private readonly maxBackoff: number;
  private readonly relaxAfter: number;
  private spacing: number;
  private lastStart = -Infinity;
  private backoffUntil = 0;
  private violations = 0;
  private successes = 0;

  constructor(o: PacerOptions = {}) {
    this.now = o.now ?? Date.now;
    this.minSpacing = o.minSpacingMs ?? 1000;
    this.maxSpacing = Math.max(this.minSpacing, o.maxSpacingMs ?? 10_000);
    this.baseBackoff = o.baseBackoffMs ?? 30_000;
    this.maxBackoff = o.maxBackoffMs ?? 600_000;
    this.relaxAfter = o.relaxAfter ?? 20;
    this.spacing = this.minSpacing;
  }

  /** Earliest time (epoch ms) the next historical request may start. */
  readyAt(now = this.now()): number {
    return Math.max(now, this.lastStart + this.spacing, this.backoffUntil);
  }

  /** How long (ms) until the next request may start. */
  delayMs(): number {
    const now = this.now();
    return this.readyAt(now) - now;
  }

  /** Current minimum spacing between starts (ms). */
  get spacingMs(): number { return this.spacing; }

  /** Records a request start. Throws if called before readyAt (a scheduler bug, not a runtime condition). */
  begin(): void {
    const now = this.now();
    if (now < this.readyAt(now)) throw new Error("historical request started before the pacer allowed it");
    this.lastStart = now;
  }

  /** Pacing violation reported by TWS: back off exponentially and space requests further apart. */
  violation(): number {
    this.violations += 1;
    this.successes = 0;
    this.spacing = Math.min(this.maxSpacing, this.spacing * 2);
    const delay = Math.min(this.maxBackoff, this.baseBackoff * 2 ** (this.violations - 1));
    this.backoffUntil = this.now() + delay;
    return delay;
  }

  success(): void {
    this.violations = 0;
    this.successes += 1;
    if (this.successes >= this.relaxAfter && this.spacing > this.minSpacing) {
      this.spacing = Math.max(this.minSpacing, this.spacing / 2);
      this.successes = 0;
    }
  }
}
