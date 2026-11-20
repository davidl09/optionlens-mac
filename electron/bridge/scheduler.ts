// The bridge's single work scheduler. Exactly one task runs at a time (so market-data line usage and
// historical requests never overlap); the next task is the highest-priority one whose gate is open
// (historical tasks are gated by the pacer), FIFO within a priority. Tasks are de-duplicated by key,
// cancellable by kind, and may ask to be retried later (RetryLater) up to their attempt limit.
import { JOB_KINDS, type JobKind } from "../shared/api";
import type { Gate } from "./pacer";

export interface Task {
  key: string;
  kind: JobKind;
  label: string;
  gate?: Gate;
  maxAttempts?: number;
  run(signal: AbortSignal): Promise<void>;
}

export type Outcome = { ok: true } | { ok: false; cancelled: boolean; error: string };

/** Thrown by a task to be re-queued (its gate decides when it runs again). */
export class RetryLater extends Error {
  constructor(message: string) { super(message); this.name = "RetryLater"; }
}

/** Lower number = runs first. */
export const PRIORITY: Record<JobKind, number> = Object.fromEntries(JOB_KINDS.map((k, i) => [k, i])) as Record<JobKind, number>;

interface Entry { task: Task; seq: number; attempts: number; resolve: (o: Outcome) => void; promise: Promise<Outcome> }

export interface SchedulerOptions {
  now?: () => number;
  onChange?: () => void;
  onDone?: (task: Task, outcome: Outcome) => void;
}

export class Scheduler {
  private queue: Entry[] = [];
  private running: { entry: Entry; ctrl: AbortController } | null = null;
  private paused = false;
  private seq = 0;
  private wake: ReturnType<typeof setTimeout> | null = null;
  private readonly now: () => number;

  constructor(private readonly o: SchedulerOptions = {}) {
    this.now = o.now ?? Date.now;
  }

  /** Queues a task; if one with the same key is queued or running, returns that one's outcome. */
  enqueue(task: Task): Promise<Outcome> {
    const existing = this.running?.entry.task.key === task.key ? this.running.entry : this.queue.find((e) => e.task.key === task.key);
    if (existing) return existing.promise;
    let resolve!: (o: Outcome) => void;
    const promise = new Promise<Outcome>((r) => { resolve = r; });
    this.queue.push({ task, seq: this.seq++, attempts: 0, resolve, promise });
    this.changed();
    this.pump();
    return promise;
  }

  has(key: string): boolean {
    return this.running?.entry.task.key === key || this.queue.some((e) => e.task.key === key);
  }

  /** Drops queued tasks of `kind` (all when omitted) and aborts the running one if it matches. */
  cancel(kind?: JobKind): void {
    const match = (e: Entry) => kind === undefined || e.task.kind === kind;
    const dropped = this.queue.filter(match);
    this.queue = this.queue.filter((e) => !match(e));
    for (const e of dropped) this.finish(e, { ok: false, cancelled: true, error: "cancelled" });
    if (this.running && match(this.running.entry)) this.running.ctrl.abort(new Error("cancelled"));
    this.changed();
  }

  /** Stops starting new tasks (the running one continues unless cancelled). */
  pause(): void { this.paused = true; this.changed(); }
  resume(): void { this.paused = false; this.changed(); this.pump(); }
  get isPaused(): boolean { return this.paused; }

  current(): { kind: JobKind; label: string } | null {
    return this.running ? { kind: this.running.entry.task.kind, label: this.running.entry.task.label } : null;
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

  /** Next runnable entry, or the earliest time one becomes runnable. */
  private pick(): { entry: Entry } | { at: number } | null {
    if (!this.queue.length) return null;
    const now = this.now();
    let best: Entry | null = null;
    let earliest = Infinity;
    for (const e of this.queue) {
      const at = e.task.gate ? e.task.gate.readyAt() : now;
      if (at > now) { earliest = Math.min(earliest, at); continue; }
      if (!best || PRIORITY[e.task.kind] < PRIORITY[best.task.kind] || (PRIORITY[e.task.kind] === PRIORITY[best.task.kind] && e.seq < best.seq)) best = e;
    }
    return best ? { entry: best } : { at: earliest };
  }

  private pump(): void {
    if (this.wake) { clearTimeout(this.wake); this.wake = null; }
    if (this.paused || this.running) return;
    const p = this.pick();
    if (!p) return;
    if ("at" in p) { this.wake = setTimeout(() => this.pump(), Math.max(10, p.at - this.now())); return; }
    const entry = p.entry;
    this.queue = this.queue.filter((e) => e !== entry);
    const ctrl = new AbortController();
    this.running = { entry, ctrl };
    entry.attempts += 1;
    this.changed();
    void this.execute(entry, ctrl);
  }

  private async execute(entry: Entry, ctrl: AbortController): Promise<void> {
    let outcome: Outcome | null;
    try {
      await entry.task.run(ctrl.signal);
      outcome = ctrl.signal.aborted ? { ok: false, cancelled: true, error: "cancelled" } : { ok: true };
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      if (ctrl.signal.aborted) outcome = { ok: false, cancelled: true, error: "cancelled" };
      else if (e instanceof RetryLater && entry.attempts < (entry.task.maxAttempts ?? 5)) outcome = null;
      else outcome = { ok: false, cancelled: false, error: msg };
    }
    this.running = null;
    if (outcome) this.finish(entry, outcome);
    else this.queue.push(entry);   // retry: keeps its seq (FIFO position) and promise
    this.changed();
    this.pump();
  }
}
