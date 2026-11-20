import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HistoricalPacer } from "./pacer";
import { RetryLater, Scheduler, type Task } from "./scheduler";
import type { JobKind } from "../shared/api";

const task = (kind: JobKind, key: string, run: Task["run"] = async () => undefined, extra: Partial<Task> = {}): Task => ({ kind, key, label: key, run, ...extra });

describe("Scheduler", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it("runs one task at a time: on-demand first, then a fair share per kind (ties in §6 order), FIFO within a kind", async () => {
    const s = new Scheduler();
    s.pause();
    const order: string[] = [];
    let active = 0;
    let maxActive = 0;
    const mk = (kind: JobKind, key: string) => task(kind, key, async () => {
      active += 1; maxActive = Math.max(maxActive, active);
      order.push(key);
      await new Promise((r) => setTimeout(r, 5));
      active -= 1;
    });
    const all = [
      s.enqueue(mk("iv-history", "iv1")), s.enqueue(mk("option-bars", "ob1")), s.enqueue(mk("underlying-stats", "st1")),
      s.enqueue(mk("chain-universe", "cu1")), s.enqueue(mk("eod-chains", "eod1")), s.enqueue(mk("on-demand", "od1")),
      s.enqueue(mk("chain-universe", "cu2")),
    ];
    s.resume();
    await vi.runAllTimersAsync();
    await Promise.all(all);
    expect(order).toEqual(["od1", "eod1", "cu1", "st1", "ob1", "iv1", "cu2"]);
    expect(maxActive).toBe(1);
  });

  it("never starves lower kinds behind a long chain-universe round", async () => {
    let now = 0;
    const s = new Scheduler({ now: () => now });
    s.pause();
    const order: string[] = [];
    const mk = (kind: JobKind, key: string, ms: number) => task(kind, key, async () => { order.push(key); now += ms; });
    const all = Array.from({ length: 100 }, (_, i) => s.enqueue(mk("chain-universe", `cu${i}`, 30_000)));
    all.push(s.enqueue(mk("underlying-stats", "stats", 5_000)), s.enqueue(mk("index", "vix", 1_000)), s.enqueue(mk("iv-history", "iv", 1_000)));
    s.resume();
    await vi.runAllTimersAsync();
    await Promise.all(all);
    for (const k of ["stats", "vix", "iv"]) expect(order.indexOf(k)).toBeLessThan(5);
    // A kind that gets work later starts at the others' level: it shares, it does not monopolize.
    const late = Array.from({ length: 5 }, (_, i) => s.enqueue(mk("option-bars", `ob${i}`, 30_000)));
    const more = Array.from({ length: 5 }, (_, i) => s.enqueue(mk("chain-universe", `cu-b${i}`, 30_000)));
    await vi.runAllTimersAsync();
    await Promise.all([...late, ...more]);
    expect(order.slice(103, 106).some((k) => k.startsWith("cu-b"))).toBe(true);
  });

  it("runs a waiting on-demand task at the running task's checkpoint", async () => {
    const s = new Scheduler();
    const order: string[] = [];
    let releaseBatch!: () => void;
    const long = s.enqueue(task("chain-universe", "long", async (_signal, checkpoint) => {
      order.push("long:batch1");
      await new Promise<void>((r) => { releaseBatch = r; });
      await checkpoint();
      order.push("long:batch2");
    }));
    await vi.advanceTimersByTimeAsync(0);
    const od = s.enqueue(task("on-demand", "od", async (_signal, checkpoint) => { await checkpoint(); order.push("od"); }));
    const other = s.enqueue(task("index", "idx", async () => { order.push("idx"); }));
    expect(s.current()?.kind).toBe("chain-universe");
    releaseBatch();
    await vi.runAllTimersAsync();
    await Promise.all([long, od, other]);
    expect(order).toEqual(["long:batch1", "od", "long:batch2", "idx"]);
  });

  it("cancels by key, including a task waiting in the queue", async () => {
    const s = new Scheduler();
    s.pause();
    const a = s.enqueue(task("on-demand", "ondemand:AAPL"));
    const b = s.enqueue(task("on-demand", "ondemand:MSFT"));
    s.cancel({ key: "ondemand:AAPL" });
    s.resume();
    await vi.runAllTimersAsync();
    expect(await a).toMatchObject({ ok: false, cancelled: true });
    expect(await b).toEqual({ ok: true });
  });

  it("de-duplicates by key and shares the outcome", async () => {
    const s = new Scheduler();
    const run = vi.fn(async () => undefined);
    const a = s.enqueue(task("on-demand", "k", run));
    const b = s.enqueue(task("on-demand", "k", run));
    await vi.runAllTimersAsync();
    expect(await a).toEqual({ ok: true });
    expect(await b).toEqual({ ok: true });
    expect(run).toHaveBeenCalledTimes(1);
  });

  it("waits for a gated task without blocking ungated ones", async () => {
    let now = 0;
    vi.setSystemTime(0);
    const pacer = new HistoricalPacer({ now: () => now, minSpacingMs: 10_000 });
    pacer.begin();   // next historical slot at t = 10 s
    const s = new Scheduler({ now: () => now });
    const order: string[] = [];
    const hist = s.enqueue(task("iv-history", "hist", async () => { pacer.begin(); order.push(`hist@${now}`); }, { gate: pacer }));
    const chain = s.enqueue(task("chain-universe", "chain", async () => { order.push(`chain@${now}`); }));
    await vi.advanceTimersByTimeAsync(0);
    expect(order).toEqual(["chain@0"]);
    now = 10_000;
    await vi.advanceTimersByTimeAsync(10_000);
    await Promise.all([hist, chain]);
    expect(order).toEqual(["chain@0", "hist@10000"]);
  });

  it("retries a RetryLater task up to maxAttempts, then reports failure", async () => {
    const s = new Scheduler();
    const run = vi.fn(async () => { throw new RetryLater("pacing"); });
    const out = s.enqueue(task("iv-history", "r", run, { maxAttempts: 3 }));
    await vi.runAllTimersAsync();
    expect(await out).toEqual({ ok: false, cancelled: false, error: "pacing" });
    expect(run).toHaveBeenCalledTimes(3);
  });

  it("cancels queued tasks of a kind and aborts the running one", async () => {
    const s = new Scheduler();
    let sawAbort = false;
    const running = s.enqueue(task("iv-history", "a", (signal) => new Promise<void>((resolve) => {
      signal.addEventListener("abort", () => { sawAbort = true; resolve(); });
    })));
    const queued = s.enqueue(task("iv-history", "b"));
    const other = s.enqueue(task("index", "c"));
    await vi.advanceTimersByTimeAsync(0);
    s.cancel({ kind: "iv-history" });
    await vi.runAllTimersAsync();
    expect(sawAbort).toBe(true);
    expect(await running).toMatchObject({ ok: false, cancelled: true });
    expect(await queued).toMatchObject({ ok: false, cancelled: true });
    expect(await other).toEqual({ ok: true });
  });

  it("a failing task does not stop the queue", async () => {
    const s = new Scheduler();
    const bad = s.enqueue(task("on-demand", "bad", async () => { throw new Error("boom"); }));
    const good = s.enqueue(task("index", "good"));
    await vi.runAllTimersAsync();
    expect(await bad).toEqual({ ok: false, cancelled: false, error: "boom" });
    expect(await good).toEqual({ ok: true });
  });
});
