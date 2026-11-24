import { describe, expect, it } from "vitest";
import { LatestRunner, type LatestState } from "./latest";

/** A runner on a fake clock with manually resolved requests. */
function rig(minIntervalMs = 500) {
  let t = 0;
  const timers: { at: number; fn: () => void }[] = [];
  const calls: { key: string; resolve: (v: string) => void; reject: (e: Error) => void }[] = [];
  const states: LatestState<string>[] = [];
  const r = new LatestRunner<string>(
    (key) => new Promise<string>((resolve, reject) => calls.push({ key, resolve, reject })), minIntervalMs, (s) => states.push(s),
    { now: () => t, setTimer: (fn, ms) => { const x = { at: t + ms, fn }; timers.push(x); return x; }, clearTimer: (x) => timers.splice(timers.indexOf(x as never), 1) },
  );
  const advance = (ms: number) => {
    t += ms;
    for (const x of [...timers].filter((y) => y.at <= t)) { timers.splice(timers.indexOf(x), 1); x.fn(); }
  };
  const flush = () => new Promise((res) => setTimeout(res, 0));
  return { r, calls, states, advance, flush };
}

describe("LatestRunner", () => {
  it("sends at once, keeps one request in flight and sends the newest key when it ends", async () => {
    const { r, calls, advance, flush } = rig();
    r.request("a");
    expect(calls.map((c) => c.key)).toEqual(["a"]);
    r.request("b");
    r.request("c");                        // b is never sent: c is newer
    expect(calls).toHaveLength(1);
    advance(600);
    calls[0].resolve("A");
    await flush();
    expect(r.state).toMatchObject({ data: "A", dataKey: "a" });
    expect(calls.map((c) => c.key)).toEqual(["a", "c"]);
  });

  it("never starves: a slow engine still shows each completed answer", async () => {
    const { r, calls, advance, flush } = rig();
    for (let i = 0; i < 5; i++) {
      r.request(`k${i}`);
      advance(2000);                       // every answer takes longer than the key changes
      calls[calls.length - 1].resolve(`v${i}`);
      await flush();
      expect(r.state.data).toBe(`v${i}`);
    }
  });

  it("spaces starts at least minIntervalMs apart (≤ 2/s)", async () => {
    const { r, calls, advance, flush } = rig(500);
    r.request("a");
    calls[0].resolve("A");
    await flush();
    r.request("b");
    expect(calls).toHaveLength(1);         // 0 ms after the last start
    advance(499);
    expect(calls).toHaveLength(1);
    advance(1);
    expect(calls.map((c) => c.key)).toEqual(["a", "b"]);
  });

  it("an unchanged key is not resent; null keeps the last answer; invalidate resends", async () => {
    const { r, calls, advance, flush } = rig();
    r.request("a");
    calls[0].resolve("A");
    await flush();
    advance(1000);
    r.request("a");
    r.request(null);
    expect(calls).toHaveLength(1);
    expect(r.state.data).toBe("A");
    r.request("a");
    r.invalidate();
    expect(calls).toHaveLength(2);
  });

  it("an error is shown with the last data; the next key clears it", async () => {
    const { r, calls, advance, flush } = rig();
    r.request("a");
    calls[0].resolve("A");
    await flush();
    advance(1000);
    r.request("b");
    calls[1].reject(new Error("boom"));
    await flush();
    expect(r.state).toMatchObject({ data: "A", error: new Error("boom") });
    advance(1000);
    r.request("c");
    calls[2].resolve("C");
    await flush();
    expect(r.state).toMatchObject({ data: "C", error: null });
  });
});
