// Market-data lines one batch may use: the configured share of the account's line limit, reduced for
// the rest of the TWS session when TWS refuses subscriptions with error 101 (max number of tickers) —
// the configured limit is too high, or the user's own TWS windows hold more lines than assumed.
export class LineBudget {
  private cap: number | null = null;

  constructor(private readonly configured: () => number) {}

  get size(): number {
    return Math.max(1, Math.min(this.configured(), this.cap ?? Infinity));
  }

  /** The reduced size, or null while the configured one applies. */
  get reduced(): number | null {
    return this.cap !== null && this.cap < this.configured() ? this.cap : null;
  }

  /**
   * TWS refused some subscriptions of a batch while `accepted` were live: keep 10% below that from now on.
   * Returns whether the budget shrank (if not, retrying the refused contracts cannot help).
   */
  limitHit(accepted: number): boolean {
    const before = this.size;
    this.cap = Math.max(1, Math.min(before - 1, Math.floor(accepted * 0.9)));
    return this.size < before;
  }

  /** New TWS session or new settings: start from the configured size again. */
  reset(): void {
    this.cap = null;
  }
}
