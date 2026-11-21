import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EMPTY_RECORD, parseUpdateRecord, type UpdateRecord } from "./record";
import { confirmed, onLaunch, Probation, rollbackImpossible, rollbackStarted } from "./rollback";

const pending = { version: "0.1.4", previousVersion: "0.1.3", backupPath: "/b/OptionLens-0.1.3.app", launches: 0, rollingBack: false };
const rec = (over: Partial<UpdateRecord["pending"] & object> = {}, top: Partial<UpdateRecord> = {}): UpdateRecord => ({ ...EMPTY_RECORD, pending: { ...pending, ...over }, ...top });

describe("launch decisions", () => {
  it("nothing on probation: nothing to do", () => {
    expect(onLaunch(EMPTY_RECORD, "0.1.3")).toEqual({ rec: EMPTY_RECORD, decision: { kind: "none" } });
  });

  it("the first two launches of a new version are watched; after two unconfirmed launches the next one rolls back", () => {
    let r = rec();
    const first = onLaunch(r, "0.1.4");
    expect(first.decision).toEqual({ kind: "watch" });
    r = first.rec;
    const second = onLaunch(r, "0.1.4");                 // the first launch crashed before it was confirmed
    expect(second.decision).toEqual({ kind: "watch" });
    const third = onLaunch(second.rec, "0.1.4");          // and the second one too
    expect(third.decision).toMatchObject({ kind: "rollback", pending: { version: "0.1.4", previousVersion: "0.1.3" } });
  });

  it("a confirmed launch ends probation", () => {
    const r = confirmed(onLaunch(rec(), "0.1.4").rec);
    expect(r.pending).toBeNull();
    expect(onLaunch(r, "0.1.4").decision).toEqual({ kind: "none" });
  });

  it("after a rollback the restored version reports it, skips the bad version and does not roll back itself", () => {
    const started = rollbackStarted(rec({ launches: 2 }));
    expect(started.pending?.rollingBack).toBe(true);
    expect(started.skipped).toEqual(["0.1.4"]);
    const { rec: after, decision } = onLaunch(started, "0.1.3");
    expect(decision).toEqual({ kind: "none" });
    expect(after.pending).toBeNull();
    expect(after.skipped).toEqual(["0.1.4"]);
    expect(after.notice).toMatch(/0.1.4 did not start correctly, so 0.1.3 was restored/);
    expect(onLaunch(after, "0.1.3").decision).toEqual({ kind: "none" });
  });

  it("loop guard: still on the bad version after a rollback → stop trying and say so", () => {
    const { rec: after, decision } = onLaunch(rollbackStarted(rec({ launches: 2 })), "0.1.4");
    expect(decision).toEqual({ kind: "none" });
    expect(after.pending).toBeNull();
    expect(after.notice).toMatch(/restoring 0.1.3 failed/);
    expect(onLaunch(after, "0.1.4").decision).toEqual({ kind: "none" });
  });

  it("the swap never happened (helper failed): the old version says the install failed", () => {
    const { rec: after } = onLaunch(rec(), "0.1.3");
    expect(after.pending).toBeNull();
    expect(after.skipped).toEqual([]);
    expect(after.notice).toMatch(/could not be installed/);
  });

  it("no backup to restore: give up with a message and skip the version", () => {
    const r = rollbackImpossible(rec(), "the backup is missing.");
    expect(r.pending).toBeNull();
    expect(r.skipped).toEqual(["0.1.4"]);
    expect(r.notice).toMatch(/could not be restored: the backup is missing/);
  });

  it("parses a stored record tolerantly", () => {
    expect(parseUpdateRecord(null)).toEqual(EMPTY_RECORD);
    expect(parseUpdateRecord({ pending: { version: 1 }, skipped: ["0.1.4", 7], notice: 3 })).toEqual({ ...EMPTY_RECORD, skipped: ["0.1.4"] });
    expect(parseUpdateRecord({ pending: { ...pending, launches: -1 } }).pending).toEqual(pending);
  });
});

describe("probation", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });
  const make = () => { const confirm = vi.fn(); const rollback = vi.fn(); const p = new Probation({ onConfirm: confirm, onRollback: rollback }); p.start(); return { p, confirm, rollback }; };

  it("confirms once the engine is healthy and 2 minutes have passed", () => {
    const { p, confirm, rollback } = make();
    p.engineStarting();
    vi.advanceTimersByTime(20_000);
    p.engineHealthy();
    expect(confirm).not.toHaveBeenCalled();
    vi.advanceTimersByTime(100_000);
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(rollback).not.toHaveBeenCalled();
  });

  it("rolls back when the engine is not healthy 60 s after it started (measured from the engine start)", () => {
    const { p, rollback } = make();
    vi.advanceTimersByTime(30_000);     // database backup before the engine starts
    p.engineStarting();
    vi.advanceTimersByTime(59_000);
    expect(rollback).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1_000);
    expect(rollback).toHaveBeenCalledTimes(1);
    p.engineHealthy();
    vi.advanceTimersByTime(200_000);
    expect(rollback).toHaveBeenCalledTimes(1);
  });

  it("a quit after the engine was healthy confirms; a quit before does not", () => {
    const a = make();
    a.p.engineStarting(); a.p.engineHealthy(); a.p.quitting();
    expect(a.confirm).toHaveBeenCalledTimes(1);
    const b = make();
    b.p.engineStarting(); b.p.quitting();
    expect(b.confirm).not.toHaveBeenCalled();
  });
});
