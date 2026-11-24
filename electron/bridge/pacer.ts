// IBKR historical-data pacing gate. The bridge's historical requests are daily bars, and intraday bars on request
// (1 min to 1 hour, also gated by IntradayGate below). IBKR's
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

/**
 * Extra limits for intraday bar requests: IBKR's hard "small bars" pacing rules (written for bars of 30 s or less,
 * applied here to every intraday request to stay well clear of them): at most 60 requests in any 10 minutes and no
 * identical request (same contract, bar size, end and duration) within 15 s. These requests also go through the
 * HistoricalPacer (spacing, backoff after a violation), whose ≥ 1 s spacing keeps them under 6 per contract in 2 s.
 */
export class IntradayGate {
  private readonly now: () => number;
  private starts: number[] = [];
  private readonly lastByKey = new Map<string, number>();

  private readonly max: number;
  private readonly window: number;
  private readonly identical: number;

  constructor(o: { now?: () => number; maxPerWindow?: number; windowMs?: number; identicalMs?: number } = {}) {
    this.now = o.now ?? Date.now;
    this.max = o.maxPerWindow ?? 60;
    this.window = o.windowMs ?? 600_000;
    this.identical = o.identicalMs ?? 15_000;
  }

  /** Earliest time (epoch ms) a request with this key may start. */
  readyAt(key: string, now = this.now()): number {
    this.starts = this.starts.filter((t) => t > now - this.window);
    const byWindow = this.starts.length >= this.max ? this.starts[0] + this.window : now;
    const last = this.lastByKey.get(key);
    return Math.max(now, byWindow, last === undefined ? now : last + this.identical);
  }

  begin(key: string): void {
    const now = this.now();
    this.starts.push(now);
    this.lastByKey.set(key, now);
    for (const [k, t] of this.lastByKey) if (t <= now - this.identical) this.lastByKey.delete(k);
  }
}
