// IBKR historical-data pacing gate. Rules respected (worst case, all bar sizes):
//   - requests are serialized by the scheduler; starts are ≥ minSpacingMs apart (default 10 s),
//   - at most maxPerWindow starts in any windowMs (IBKR: 60 per 10 min; default 55 for margin),
//   - after a pacing violation, exponential backoff (30 s → 60 s → … ≤ 10 min) on top of the above.
export interface Gate { readyAt(): number }

export interface PacerOptions {
  now?: () => number;
  minSpacingMs?: number;
  maxPerWindow?: number;
  windowMs?: number;
  baseBackoffMs?: number;
  maxBackoffMs?: number;
}

export class HistoricalPacer implements Gate {
  private readonly now: () => number;
  private readonly spacing: number;
  private readonly maxPerWindow: number;
  private readonly windowMs: number;
  private readonly baseBackoff: number;
  private readonly maxBackoff: number;
  private starts: number[] = [];
  private backoffUntil = 0;
  private violations = 0;

  constructor(o: PacerOptions = {}) {
    this.now = o.now ?? Date.now;
    this.spacing = o.minSpacingMs ?? 10_000;
    this.maxPerWindow = o.maxPerWindow ?? 55;
    this.windowMs = o.windowMs ?? 600_000;
    this.baseBackoff = o.baseBackoffMs ?? 30_000;
    this.maxBackoff = o.maxBackoffMs ?? 600_000;
  }

  /** Earliest time (epoch ms) the next historical request may start. */
  readyAt(): number {
    const now = this.now();
    this.starts = this.starts.filter((t) => t > now - this.windowMs);
    const last = this.starts.length ? this.starts[this.starts.length - 1] : -Infinity;
    let t = Math.max(now, last + this.spacing, this.backoffUntil);
    if (this.starts.length >= this.maxPerWindow) t = Math.max(t, this.starts[this.starts.length - this.maxPerWindow] + this.windowMs);
    return t;
  }

  /** Records a request start. Throws if called before readyAt (a scheduler bug, not a runtime condition). */
  begin(): void {
    const now = this.now();
    if (now < this.readyAt()) throw new Error("historical request started before the pacer allowed it");
    this.starts.push(now);
  }

  /** Pacing violation reported by TWS: back off exponentially. */
  violation(): number {
    this.violations += 1;
    const delay = Math.min(this.maxBackoff, this.baseBackoff * 2 ** (this.violations - 1));
    this.backoffUntil = this.now() + delay;
    return delay;
  }

  success(): void {
    this.violations = 0;
  }
}
