// The bridge's single work scheduler. Exactly one task holds TWS at a time (so market-data line usage and
// historical requests never overlap). Choosing the next task:
//   - on-demand refreshes (the user is looking at the page) always go first, and may even run inside a
//     long task at its checkpoints (between market-data batches, when it holds no lines);
//   - every other kind gets a fair share of TWS time by weight (weighted fair queueing on run time), so
//     a long chain-universe round never starves stats, index data or backfills; ties go to §6 order;
//   - a task whose gate is closed (historical tasks wait for the pacer) is skipped until it opens;
//   - FIFO within a kind.
// Tasks are de-duplicated by key, cancellable by kind / key / group, and may ask to be retried later
// (RetryLater) up to their attempt limit.
import { JOB_KINDS, type JobKind } from "../shared/api";
import type { Gate } from "./pacer";

export interface Task {
  key: string;
  kind: JobKind;
  label: string;
  /** Tasks that stand or fall together (e.g. all option-bar requests of one symbol). */
  group?: string;
  gate?: Gate;
  maxAttempts?: number;
  /** `checkpoint` runs any waiting on-demand tasks first; call it only while holding no market-data lines. */
  run(signal: AbortSignal, checkpoint: () => Promise<void>): Promise<void>;
}

export type Outcome = { ok: true } | { ok: false; cancelled: boolean; error: string };

/** Thrown by a task to be re-queued (its gate decides when it runs again). */
export class RetryLater extends Error {
  constructor(message: string) { super(message); this.name = "RetryLater"; }
}

/** Tie-break order (lower first), per INTEGRATION.md §6. */
export const PRIORITY: Record<JobKind, number> = Object.fromEntries(JOB_KINDS.map((k, i) => [k, i])) as Record<JobKind, number>;

/** Kind that pre-empts everything else. */
export const URGENT: JobKind = "on-demand";

/** Share of TWS time per kind while several kinds have work (relative weights). */
export const WEIGHT: Record<Exclude<JobKind, typeof URGENT>, number> = {
  "eod-chains": 8, "chain-universe": 4, "underlying-stats": 3, index: 2, "option-bars": 1, "iv-history": 1,
};

export interface CancelFilter { kind?: JobKind; key?: string; group?: string; /** Leave running / suspended tasks alone. */ queuedOnly?: boolean }

/** Lexicographic a < b. */
function before(a: readonly number[], b: readonly number[]): boolean {
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return a[i] < b[i];
  return false;
}

interface Entry { task: Task; seq: number; attempts: number; resolve: (o: Outcome) => void; promise: Promise<Outcome> }
interface Running { entry: Entry; ctrl: AbortController; /** Time spent running on-demand tasks at its checkpoints. */ lentMs: number }

export interface SchedulerOptions {
  now?: () => number;
  onChange?: () => void;
  onDone?: (task: Task, outcome: Outcome) => void;
}

export class Scheduler {
  private queue: Entry[] = [];
  /** The task holding TWS, and below it the tasks suspended at a checkpoint (innermost last). */
  private stack: Running[] = [];
  private paused = false;
  private seq = 0;
  private wake: ReturnType<typeof setTimeout> | null = null;
  /** Weighted run time per kind (virtual time of the fair queue). */
  private readonly served = new Map<JobKind, number>();
  private readonly now: () => number;

  constructor(private readonly o: SchedulerOptions = {}) {
    this.now = o.now ?? Date.now;
  }

  /** Queues a task; if one with the same key is queued or running, returns that one's outcome. */
  enqueue(task: Task): Promise<Outcome> {
    const existing = this.stack.find((r) => r.entry.task.key === task.key)?.entry ?? this.queue.find((e) => e.task.key === task.key);
    if (existing) return existing.promise;
    // A kind that was idle starts level with the busiest-served active kinds: no credit, no debt for idle time.
    if (task.kind !== URGENT && !this.active(task.kind)) this.served.set(task.kind, this.floor());
    let resolve!: (o: Outcome) => void;
    const promise = new Promise<Outcome>((r) => { resolve = r; });
    this.queue.push({ task, seq: this.seq++, attempts: 0, resolve, promise });
    this.changed();
    this.pump();
    return promise;
  }

  has(key: string): boolean {
    return this.stack.some((r) => r.entry.task.key === key) || this.queue.some((e) => e.task.key === key);
  }

  /** Drops matching queued tasks (all when no filter) and aborts matching running / suspended ones. */
  cancel(f: CancelFilter = {}): void {
    const match = (e: Entry) => (f.kind === undefined || e.task.kind === f.kind) && (f.key === undefined || e.task.key === f.key)
      && (f.group === undefined || e.task.group === f.group);
    const dropped = this.queue.filter(match);
    this.queue = this.queue.filter((e) => !match(e));
    for (const e of dropped) this.finish(e, { ok: false, cancelled: true, error: "cancelled" });
    if (!f.queuedOnly) for (const r of this.stack) if (match(r.entry)) r.ctrl.abort(new Error("cancelled"));
    this.changed();
  }

  /** Stops starting new tasks (the running one continues unless cancelled). */
  pause(): void { this.paused = true; this.changed(); }
  resume(): void { this.paused = false; this.changed(); this.pump(); }
  get isPaused(): boolean { return this.paused; }

  current(): { kind: JobKind; label: string } | null {
    const top = this.stack[this.stack.length - 1];
    return top ? { kind: top.entry.task.kind, label: top.entry.task.label } : null;
  }

  counts(): Record<JobKind, number> {
    const out = Object.fromEntries(JOB_KINDS.map((k) => [k, 0])) as Record<JobKind, number>;
    for (const e of this.queue) out[e.task.kind] += 1;
    return out;
  }

  dispose(): void {
    if (this.wake) clearTimeout(this.wake);
    this.wake = null;
    this.cancel();
  }

  private changed(): void { this.o.onChange?.(); }

  private finish(e: Entry, outcome: Outcome): void {
    e.resolve(outcome);
    this.o.onDone?.(e.task, outcome);
  }

  private active(kind: JobKind): boolean {
    return this.queue.some((e) => e.task.kind === kind) || this.stack.some((r) => r.entry.task.kind === kind);
  }

  /** Smallest virtual time among kinds with work (0 when idle): a kind that becomes active starts there. */
  private floor(): number {
    let min = Infinity;
    for (const k of JOB_KINDS) if (k !== URGENT && this.active(k)) min = Math.min(min, this.served.get(k) ?? 0);
    return Number.isFinite(min) ? min : 0;
  }

  private rank(e: Entry): [number, number, number, number] {
    const k = e.task.kind;
    return [k === URGENT ? 0 : 1, k === URGENT ? 0 : this.served.get(k) ?? 0, PRIORITY[k], e.seq];
  }

  /** Next runnable entry (optionally only urgent ones), or the earliest time one becomes runnable. */
  private pick(urgentOnly = false): { entry: Entry } | { at: number } | null {
    const now = this.now();
    let best: Entry | null = null;
    let bestRank: readonly number[] = [];
    let earliest = Infinity;
    for (const e of this.queue) {
      if (urgentOnly && e.task.kind !== URGENT) continue;
      const at = e.task.gate ? e.task.gate.readyAt() : now;
      if (at > now) { earliest = Math.min(earliest, at); continue; }
      const r = this.rank(e);
      if (!best || before(r, bestRank)) { best = e; bestRank = r; }
    }
    if (best) return { entry: best };
    return Number.isFinite(earliest) ? { at: earliest } : null;
  }

  private pump(): void {
    if (this.wake) { clearTimeout(this.wake); this.wake = null; }
    if (this.paused || this.stack.length) return;
    const p = this.pick();
    if (!p) return;
    if ("at" in p) { this.wake = setTimeout(() => this.pump(), Math.max(10, p.at - this.now())); return; }
    void this.runEntry(p.entry).then(() => this.pump());
  }

  /** Runs waiting on-demand tasks inside the current task (which holds no lines while it waits here). */
  private async checkpoint(): Promise<void> {
    const host = this.stack[this.stack.length - 1];
    for (;;) {
      if (this.paused || !host || this.stack.some((r) => r.entry.task.kind === URGENT)) return;
      const p = this.pick(true);
      if (!p || "at" in p) return;
      const t0 = this.now();
      await this.runEntry(p.entry);
      host.lentMs += this.now() - t0;
    }
  }

  /** Runs one entry on top of the stack; finishes it or re-queues it (RetryLater). */
  private async runEntry(entry: Entry): Promise<void> {
    this.queue = this.queue.filter((e) => e !== entry);
    const ctrl = new AbortController();
    const running: Running = { entry, ctrl, lentMs: 0 };
    this.stack.push(running);
    entry.attempts += 1;
    this.changed();
    const started = this.now();
    let outcome: Outcome | null;
    try {
      await entry.task.run(ctrl.signal, () => this.checkpoint());
      outcome = ctrl.signal.aborted ? { ok: false, cancelled: true, error: "cancelled" } : { ok: true };
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      if (ctrl.signal.aborted) outcome = { ok: false, cancelled: true, error: "cancelled" };
      else if (e instanceof RetryLater && entry.attempts < (entry.task.maxAttempts ?? 5)) outcome = null;
      else outcome = { ok: false, cancelled: false, error: msg };
    }
    this.stack = this.stack.filter((r) => r !== running);
    const kind = entry.task.kind;
    if (kind !== URGENT) this.served.set(kind, (this.served.get(kind) ?? 0) + (this.now() - started - running.lentMs) / WEIGHT[kind as keyof typeof WEIGHT]);
    if (outcome) this.finish(entry, outcome);
    else this.queue.push(entry);   // retry: keeps its seq (FIFO position) and promise
    this.changed();
  }
}
