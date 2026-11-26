// What IBKR refused for lack of a market-data permission, so the bridge stops asking for a while. Two levels:
// - a group (one kind of data for one symbol, e.g. "RY.TO IV/HV history"): denied on the first refusal;
// - a data group of markets.ts (every stock of one exchange, a market's option history): denied on a refusal only
//   while nothing of it was ever served in this run — an account without the exchange's subscription fails every
//   request the same way, while one refused symbol among served ones is that symbol's problem.
// A denial lapses after `retryMs` (the account may have been given the subscription).
export interface Denial { id: string; label: string }

export class Permissions {
  private readonly denied = new Map<string, { at: number; label: string }>();
  private readonly served = new Set<string>();

  constructor(private readonly now: () => number, private readonly retryMs: number) {}

  isDenied(id: string | undefined): boolean {
    if (id === undefined) return false;
    const d = this.denied.get(id);
    if (d && this.now() - d.at >= this.retryMs) this.denied.delete(id);
    return this.denied.has(id);
  }

  /** Records a refusal; true when `g` was not denied before (the caller logs it once). */
  deny(g: Denial): boolean {
    const fresh = !this.denied.has(g.id);
    this.denied.set(g.id, { at: this.now(), label: g.label });
    return fresh;
  }

  /** Data of the data group arrived; true the first time (the caller logs it once). */
  serve(id: string): boolean {
    if (this.served.has(id)) return false;
    this.served.add(id);
    this.denied.delete(id);
    return true;
  }

  hasServed(id: string): boolean { return this.served.has(id); }

  labels(): string[] { return [...this.denied.values()].map((d) => d.label); }
}
