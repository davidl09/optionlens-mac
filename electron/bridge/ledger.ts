// Attempt ledger: when each historical / closing item was last attempted. Persisted so backfills resume
// after a restart without re-requesting what was just done (or what IBKR has no data for).
export interface LedgerStore { read(): unknown; write(v: unknown): void }

const KEEP_MS = 7 * 86_400_000;

export class Ledger {
  private entries = new Map<string, number>();
  private saveTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly store: LedgerStore | null, private readonly now: () => number = Date.now) {
    const raw = store?.read();
    if (raw && typeof raw === "object" && !Array.isArray(raw)) {
      const cutoff = this.now() - KEEP_MS;
      for (const [k, v] of Object.entries(raw as Record<string, unknown>)) if (typeof v === "number" && v > cutoff) this.entries.set(k, v);
    }
  }

  attemptedWithin(key: string, ms: number): boolean {
    const t = this.entries.get(key);
    return t !== undefined && this.now() - t < ms;
  }

  mark(key: string): void {
    this.entries.set(key, this.now());
    this.scheduleSave();
  }

  flush(): void {
    if (this.saveTimer) { clearTimeout(this.saveTimer); this.saveTimer = null; }
    this.store?.write(Object.fromEntries(this.entries));
  }

  private scheduleSave(): void {
    if (!this.store || this.saveTimer) return;
    this.saveTimer = setTimeout(() => { this.saveTimer = null; this.flush(); }, 2000);
  }
}
