import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { EngineStatus } from "../shared/api";
import { EngineSupervisor, parseListening, restartDelayMs, type ChildLike, type SpawnFn } from "./engine";
import { resolveEngineCommand } from "./engine-paths";
import { silentLogger } from "./log";

class FakeChild extends EventEmitter implements ChildLike {
  readonly stdout = new PassThrough();
  readonly stderr = new PassThrough();
  readonly signals: string[] = [];
  constructor(readonly pid: number) { super(); }
  kill(signal: NodeJS.Signals = "SIGTERM"): boolean { this.signals.push(signal); setTimeout(() => this.emit("exit", null, signal), 1); return true; }
  listen(port: number) { this.stdout.write(`INFO starting\nLISTENING ${port}\n`); }
  crash(code = 1) { this.stderr.write("Traceback: boom\n"); this.emit("exit", code, null); }
}

function harness() {
  const children: FakeChild[] = [];
  const envs: NodeJS.ProcessEnv[] = [];
  const spawn: SpawnFn = (_cmd, _args, opts) => { const c = new FakeChild(100 + children.length); children.push(c); envs.push(opts.env); return c; };
  const statuses: EngineStatus[] = [];
  const fetch = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ ok: true, version: "1.2.3" }) }));
  const sup = new EngineSupervisor({ command: { python: "python3", env: { PYTHONPATH: "/engine" } }, dataDir: "/data", spawn, fetch, parentPid: 4242,
    log: silentLogger, onStatus: (s) => statuses.push(s), stopTimeoutMs: 50 });
  return { sup, children, envs, statuses, fetch };
}

describe("engine supervisor", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it("spawns with loopback host, per-launch token, data dir and parent pid; runs after LISTENING + health", async () => {
    const h = harness();
    h.sup.start();
    expect(h.envs[0]).toMatchObject({ OPTIONLENS_HOST: "127.0.0.1", OPTIONLENS_DATA_DIR: "/data", OPTIONLENS_PARENT_PID: "4242", PYTHONPATH: "/engine" });
    expect(h.envs[0].OPTIONLENS_API_TOKEN).toBe(h.sup.token);
    expect(h.sup.token.length).toBeGreaterThanOrEqual(40);
    expect(h.sup.status().state).toBe("starting");
    h.children[0].listen(53111);
    await vi.advanceTimersByTimeAsync(10);
    expect(h.fetch).toHaveBeenCalledWith("http://127.0.0.1:53111/api/health", expect.anything());
    expect(h.sup.status()).toMatchObject({ state: "running", version: "1.2.3", connection: { baseUrl: "http://127.0.0.1:53111", token: h.sup.token } });
  });

  it("does not start again after shutdown (app quit while an import holds the engine stopped)", async () => {
    const h = harness();
    h.sup.start();
    h.children[0].listen(5000);
    await vi.advanceTimersByTimeAsync(10);
    let finishImport!: () => void;
    const imported = h.sup.whileStopped(() => new Promise<void>((r) => { finishImport = r; }));
    await vi.advanceTimersByTimeAsync(10);
    await h.sup.shutdown();   // Cmd+Q during the copy
    finishImport();
    await imported;
    await vi.advanceTimersByTimeAsync(5_000);
    expect(h.children).toHaveLength(1);
    expect(h.sup.status().state).toBe("stopped");
    h.sup.start();
    expect(h.children).toHaveLength(1);
  });

  it("restarts with backoff when the engine dies, then recovers", async () => {
    const h = harness();
    h.sup.start();
    h.children[0].listen(5000);
    await vi.advanceTimersByTimeAsync(10);
    h.children[0].crash();
    expect(h.sup.status()).toMatchObject({ state: "restarting", connection: null, restarts: 1 });
    expect(h.sup.status().lastError).toMatch(/code 1.*Traceback/);
    await vi.advanceTimersByTimeAsync(restartDelayMs(1));
    expect(h.children).toHaveLength(2);
    h.children[1].listen(5001);
    await vi.advanceTimersByTimeAsync(10);
    expect(h.sup.status()).toMatchObject({ state: "running", connection: { baseUrl: "http://127.0.0.1:5001" } });
  });

  it("reports failed after repeated crashes but keeps retrying", async () => {
    const h = harness();
    h.sup.start();
    for (let i = 1; i <= 5; i++) { h.children[i - 1].crash(); await vi.advanceTimersByTimeAsync(restartDelayMs(i)); }
    expect(h.statuses.some((s) => s.state === "failed")).toBe(true);
    expect(h.children.length).toBe(6);
  });

  it("stops with SIGTERM and does not restart", async () => {
    const h = harness();
    h.sup.start();
    h.children[0].listen(5000);
    await vi.advanceTimersByTimeAsync(10);
    const stopped = h.sup.stop();
    await vi.advanceTimersByTimeAsync(10);
    await stopped;
    expect(h.children[0].signals).toEqual(["SIGTERM"]);
    expect(h.sup.status().state).toBe("stopped");
    await vi.advanceTimersByTimeAsync(120_000);
    expect(h.children).toHaveLength(1);
  });

  it("runs work while stopped (data import) and starts again", async () => {
    const h = harness();
    h.sup.start();
    h.children[0].listen(5000);
    await vi.advanceTimersByTimeAsync(10);
    const p = h.sup.whileStopped(() => { expect(h.sup.status().state).toBe("stopped"); return 7; });
    await vi.advanceTimersByTimeAsync(10);
    expect(await p).toBe(7);
    expect(h.children).toHaveLength(2);
  });

  it("parses LISTENING lines strictly", () => {
    expect(parseListening("LISTENING 8473")).toBe(8473);
    expect(parseListening("LISTENING 8473\r")).toBe(8473);
    expect(parseListening("INFO LISTENING 80")).toBeNull();
    expect(parseListening("LISTENING 99999")).toBeNull();
    expect(restartDelayMs(1)).toBe(1000);
    expect(restartDelayMs(10)).toBe(60_000);
  });
});

describe("engine paths", () => {
  it("uses the bundled Python and engine in the packaged app", () => {
    const c = resolveEngineCommand({ packaged: true, resourcesPath: "/App/Contents/Resources", appPath: "/x", env: {} });
    expect(c.python).toBe("/App/Contents/Resources/python/bin/python3");
    expect(c.env.PYTHONPATH).toBeUndefined();
    expect(c.cwd).toBe("/App/Contents/Resources");
  });
  it("honours OPTIONLENS_PYTHON / OPTIONLENS_ENGINE_DIR in development", () => {
    const c = resolveEngineCommand({ packaged: false, resourcesPath: "", appPath: "/repo", env: { OPTIONLENS_PYTHON: "/venv/bin/python", OPTIONLENS_ENGINE_DIR: "/srv/engine" } });
    expect(c).toMatchObject({ python: "/venv/bin/python", cwd: "/srv/engine", env: { PYTHONPATH: "/srv/engine" } });
  });
});
