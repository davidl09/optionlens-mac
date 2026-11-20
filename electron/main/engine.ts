// Engine sidecar supervisor: spawns `<python> -m optionlens.cli serve --port 0` bound to loopback with a
// random per-launch token, reads `LISTENING <port>` from stdout, health-checks it, restarts it with
// backoff when it dies and stops it (SIGTERM, then SIGKILL) on quit.
import { randomBytes } from "node:crypto";
import type { EngineConnection, EngineState, EngineStatus } from "../shared/api";
import type { Logger } from "./log";

/** The parts of a child process the supervisor uses (node:child_process satisfies it). */
export interface ChildLike {
  readonly pid?: number;
  readonly stdout: NodeJS.ReadableStream | null;
  readonly stderr: NodeJS.ReadableStream | null;
  kill(signal?: NodeJS.Signals): boolean;
  once(event: "exit", cb: (code: number | null, signal: NodeJS.Signals | null) => void): unknown;
  once(event: "error", cb: (err: Error) => void): unknown;
}
export type SpawnFn = (cmd: string, args: string[], opts: { env: NodeJS.ProcessEnv; cwd?: string }) => ChildLike;
export type FetchFn = (url: string, init?: { signal?: AbortSignal }) => Promise<{ ok: boolean; status: number; json(): Promise<unknown> }>;

export interface EngineCommand { python: string; env: Record<string, string>; cwd?: string }

export interface EngineOptions {
  command: EngineCommand;
  dataDir: string;
  spawn: SpawnFn;
  fetch: FetchFn;
  parentPid: number;
  log: Logger;
  onStatus: (s: EngineStatus) => void;
  startTimeoutMs?: number;
  stopTimeoutMs?: number;
  /** Uptime after which the restart backoff resets. */
  stableAfterMs?: number;
}

const LISTENING_RE = /^LISTENING (\d{1,5})\s*$/;

/** Port from an engine stdout line, else null. */
export function parseListening(line: string): number | null {
  const m = LISTENING_RE.exec(line.trim());
  if (!m) return null;
  const port = Number(m[1]);
  return port >= 1 && port <= 65535 ? port : null;
}

/** Delay before restart attempt `n` (1-based): 1 s, 2 s, 4 s … capped at 60 s. */
export function restartDelayMs(n: number): number {
  return Math.min(60_000, 1000 * 2 ** Math.max(0, n - 1));
}

export function engineArgs(): string[] {
  return ["-m", "optionlens.cli", "serve", "--port", "0"];
}

/** Splits a stream into lines. */
function onLines(stream: NodeJS.ReadableStream | null, cb: (line: string) => void): void {
  if (!stream) return;
  let buf = "";
  stream.setEncoding("utf8");
  stream.on("data", (chunk: string) => {
    buf += chunk;
    let i: number;
    while ((i = buf.indexOf("\n")) >= 0) { cb(buf.slice(0, i).replace(/\r$/, "")); buf = buf.slice(i + 1); }
  });
}

export class EngineSupervisor {
  readonly token = randomBytes(32).toString("base64url");
  private child: ChildLike | null = null;
  private state: EngineState = "stopped";
  private connection: EngineConnection | null = null;
  private version: string | null = null;
  private restarts = 0;
  private failuresInRow = 0;
  private lastError: string | null = null;
  private stopping = false;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private exited: Promise<void> = Promise.resolve();
  private readonly stderrTail: string[] = [];
  /** Why the current spawn is being killed by us (start timeout, failed health check). */
  private spawnError: string | null = null;

  constructor(private readonly o: EngineOptions) {}

  status(): EngineStatus {
    return { state: this.state, connection: this.connection, version: this.version, pid: this.child?.pid ?? null,
      restarts: this.restarts, lastError: this.lastError, dataDir: this.o.dataDir };
  }

  start(): void {
    this.stopping = false;
    if (this.child || this.timer) return;
    this.spawnOnce();
  }

  async stop(): Promise<void> {
    this.stopping = true;
    if (this.timer) { clearTimeout(this.timer); this.timer = null; }
    const child = this.child;
    if (child) {
      child.kill("SIGTERM");
      const killer = setTimeout(() => child.kill("SIGKILL"), this.o.stopTimeoutMs ?? 8000);
      await this.exited;
      clearTimeout(killer);
    }
    this.set("stopped");
  }

  async restart(): Promise<void> {
    await this.stop();
    this.failuresInRow = 0;
    this.start();
  }

  /** Runs `fn` with the engine stopped (e.g. to replace its database), then starts it again. */
  async whileStopped<T>(fn: () => Promise<T> | T): Promise<T> {
    await this.stop();
    try { return await fn(); } finally { this.failuresInRow = 0; this.start(); }
  }

  private set(state: EngineState, patch: { connection?: EngineConnection | null; error?: string | null } = {}): void {
    this.state = state;
    if (patch.connection !== undefined) this.connection = patch.connection;
    if (state !== "running") this.connection = null;
    if (patch.error !== undefined) this.lastError = patch.error;
    this.o.onStatus(this.status());
  }

  private spawnOnce(): void {
    this.timer = null;
    this.stderrTail.length = 0;
    this.spawnError = null;
    this.set(this.restarts ? "restarting" : "starting");
    const env: NodeJS.ProcessEnv = {
      ...process.env,
      ...this.o.command.env,
      OPTIONLENS_HOST: "127.0.0.1",
      OPTIONLENS_API_TOKEN: this.token,
      OPTIONLENS_DATA_DIR: this.o.dataDir,
      OPTIONLENS_PARENT_PID: String(this.o.parentPid),
      PYTHONUNBUFFERED: "1",
    };
    let child: ChildLike;
    try {
      child = this.o.spawn(this.o.command.python, engineArgs(), { env, cwd: this.o.command.cwd });
    } catch (e) {
      this.onExit(`could not start ${this.o.command.python}: ${(e as Error).message}`);
      return;
    }
    this.child = child;
    let resolveExit!: () => void;
    this.exited = new Promise((r) => { resolveExit = r; });
    const startedAt = Date.now();
    let listening = false;
    const startTimer = setTimeout(() => {
      if (!listening) { this.spawnError = "engine did not report LISTENING in time"; child.kill("SIGKILL"); }
    }, this.o.startTimeoutMs ?? 120_000);

    onLines(child.stdout, (line) => {
      const port = listening ? null : parseListening(line);
      if (port === null) { this.o.log.info(`engine: ${line}`); return; }
      listening = true;
      clearTimeout(startTimer);
      void this.healthCheck(port);
    });
    onLines(child.stderr, (line) => {
      this.o.log.info(`engine: ${line}`);
      this.stderrTail.push(line);
      if (this.stderrTail.length > 15) this.stderrTail.shift();
    });
    let done = false;
    const finish = (reason: string) => {
      if (done) return;
      done = true;
      clearTimeout(startTimer);
      this.child = null;
      resolveExit();
      if (Date.now() - startedAt > (this.o.stableAfterMs ?? 60_000)) this.failuresInRow = 0;
      this.onExit(reason);
    };
    child.once("error", (err) => finish(`engine process error: ${err.message}`));
    child.once("exit", (code, signal) => finish(`engine exited (${signal ?? `code ${code}`})`));
  }

  private async healthCheck(port: number): Promise<void> {
    const baseUrl = `http://127.0.0.1:${port}`;
    const deadline = Date.now() + 20_000;
    while (Date.now() < deadline && this.child && !this.stopping) {
      try {
        const res = await this.o.fetch(`${baseUrl}/api/health`, { signal: AbortSignal.timeout(3000) });
        const body = (await res.json()) as { ok?: unknown; version?: unknown };
        if (res.ok && body.ok === true) {
          this.version = typeof body.version === "string" ? body.version : null;
          this.set("running", { connection: { baseUrl, token: this.token }, error: null });
          this.o.log.info(`engine running on ${baseUrl} (version ${this.version ?? "?"})`);
          return;
        }
      } catch { /* not up yet */ }
      await new Promise((r) => setTimeout(r, 300));
    }
    if (this.child && !this.stopping) {
      this.spawnError = `engine on port ${port} failed its health check`;
      this.child.kill("SIGKILL");
    }
  }

  private onExit(reason: string): void {
    if (this.stopping) { this.set("stopped"); return; }
    const tail = this.stderrTail.slice(-3).join(" | ");
    const error = this.spawnError ?? `${reason}${tail ? `: ${tail}` : ""}`;
    this.o.log.warn(`engine: ${error}`);
    this.restarts += 1;
    this.failuresInRow += 1;
    const delay = restartDelayMs(this.failuresInRow);
    this.set(this.failuresInRow >= 5 ? "failed" : "restarting", { error });
    this.timer = setTimeout(() => this.spawnOnce(), delay);
  }
}
