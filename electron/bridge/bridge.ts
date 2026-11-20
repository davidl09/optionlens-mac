// IBKR bridge orchestrator: keeps the TWS session (TwsSupervisor), heartbeats the engine, polls
// /api/ingest/wants and turns the plan into scheduler tasks in the §6 priority order, serves
// client-triggered refreshes and reports status. All TWS / HTTP specifics live in jobs.ts and below.
import type { BridgeSettings, BridgeStatus, JobKind, RefreshResult } from "../shared/api";
import type { Logger } from "../main/log";
import { batchLines } from "../main/settings";
import { FULL_CHAIN, type ChainShape } from "./chain";
import { COMPETING_MESSAGE, COMPETING_PAUSE_MS, TwsSupervisor } from "./connection";
import type { IngestApi } from "./ingest";
import { FINAL_MIN_COVERAGE, pushChain, pushIndexBars, pushIndexQuotes, pushOptionBars, pushUnderlyingStats, pushVolHistory, resolveOptionContracts, type JobContext } from "./jobs";
import type { Ledger } from "./ledger";
import { LineBudget } from "./line-budget";
import { nyDate } from "./ny";
import { HistoricalPacer } from "./pacer";
import { RetryLater, Scheduler, type Task } from "./scheduler";
import { isCompetingSession, type TwsConnector, type TwsContract, type TwsSession } from "./tws";
import { ledgerKey, parseWants, planWants, type Wants } from "./wants";

export interface BridgeDeps {
  connector: TwsConnector;
  ingest: IngestApi;
  ledger: Ledger;
  settings: () => BridgeSettings;
  clientName: string;
  log: Logger;
  onStatus: (s: BridgeStatus) => void;
  pacer?: HistoricalPacer;
  now?: () => number;
  heartbeatMs?: number;
  wantsMs?: number;
  batchTimeoutMs?: number;
}

const HISTORICAL_KINDS: JobKind[] = ["option-bars", "iv-history"];
const onDemandKey = (symbol: string) => `ondemand:${symbol.toUpperCase()}`;

export class Bridge {
  readonly scheduler: Scheduler;
  private readonly supervisor: TwsSupervisor;
  private readonly pacer: HistoricalPacer;
  private readonly lines: LineBudget;
  private readonly now: () => number;
  private session: TwsSession | null = null;
  private timers: ReturnType<typeof setInterval>[] = [];
  private readonly lastChainPush = new Map<string, number>();
  /** Last chain attempt (success or failure): universe refreshes wait max_age after either. */
  private readonly lastChainAttempt = new Map<string, number>();
  private readonly lastStatsPush = new Map<string, number>();
  private lastPushAt: string | null = null;
  private lastError: string | null = null;
  private wants: Wants | null = null;
  private polling = false;
  private backfill = { total: 0, done: 0, failed: 0 };
  /** What the last pushes revealed about the account's market data (null = not seen yet). */
  private feed = { optionsDelayed: null as boolean | null, stocksDelayed: null as boolean | null, optionTicksMissing: false };
  /** Last chain posted with less than FINAL_MIN_COVERAGE of its contracts quoted (status note). */
  private incompleteChain: { symbol: string; coverage: number } | null = null;

  constructor(private readonly d: BridgeDeps) {
    this.now = d.now ?? Date.now;
    this.pacer = d.pacer ?? new HistoricalPacer({ now: this.now });
    this.lines = new LineBudget(() => batchLines(d.settings()));
    this.scheduler = new Scheduler({ now: this.now, onChange: () => this.emit() });
    this.supervisor = new TwsSupervisor({
      connector: d.connector, settings: d.settings, log: d.log,
      onSession: (s) => this.onSession(s),
      onChange: () => this.onConnectionChange(),
    });
    this.scheduler.pause();   // until a TWS session exists
  }

  start(): void {
    this.supervisor.start();
    void this.heartbeat();
    this.timers.push(setInterval(() => void this.heartbeat(), this.d.heartbeatMs ?? 60_000));
    this.timers.push(setInterval(() => void this.pollWants(), this.d.wantsMs ?? 5 * 60_000));
  }

  stop(): void {
    for (const t of this.timers) clearInterval(t);
    this.timers = [];
    this.scheduler.dispose();
    this.supervisor.stop();
    this.d.ledger.flush();
  }

  /** Apply changed settings (reconnects). */
  reconfigure(): void {
    this.scheduler.cancel();
    this.lines.reset();
    this.supervisor.restart();
  }

  cancel(kind?: JobKind): void { this.scheduler.cancel({ kind }); }

  /** The page that asked for this refresh went away: drop it if it has not finished. */
  cancelRefresh(symbol: string): void { this.scheduler.cancel({ key: onDemandKey(symbol) }); }

  status(): BridgeStatus {
    const c = this.supervisor.state();
    return {
      enabled: this.d.settings().enabled, tws: c.tws, port: c.port, endpoint: c.endpoint, accountType: c.accountType, paused: c.paused,
      lastError: c.lastError ?? this.lastError, notes: this.notes(), lastPushAt: this.lastPushAt,
      current: this.scheduler.current(), queued: this.scheduler.counts(), backfill: { ...this.backfill },
    };
  }

  private notes(): string[] {
    const out: string[] = [];
    if (this.feed.optionsDelayed) out.push("IBKR delayed (no OPRA subscription)");
    if (this.feed.stocksDelayed) out.push("IBKR delayed stock quotes (no real-time US stock subscription)");
    if (this.feed.optionTicksMissing) out.push("IBKR sends no option volume / open interest for underlyings (generic ticks 100/101); sent as unknown");
    const reduced = this.lines.reduced;
    if (reduced !== null) out.push(`IBKR market-data line limit reached: batches reduced to ${reduced} lines (Settings → Market data lines is ${this.d.settings().marketDataLines})`);
    if (this.incompleteChain) out.push(`Incomplete IBKR chain for ${this.incompleteChain.symbol}: ${Math.round(this.incompleteChain.coverage * 100)}% of contracts quoted`);
    return out;
  }

  async refreshSymbol(symbol: string): Promise<RefreshResult> {
    const sym = symbol.toUpperCase();
    const c = this.supervisor.state();
    if (!this.d.settings().enabled) return { status: "unavailable", message: "The IBKR bridge is turned off." };
    if (!this.session) return { status: "unavailable", message: "Not connected to IB Gateway / TWS." };
    if (c.paused) return { status: "unavailable", message: c.paused };
    const last = this.lastChainPush.get(sym);
    if (last !== undefined && this.now() - last < (this.wants?.onDemandMaxAgeS ?? 120) * 1000) return { status: "fresh" };
    const outcome = await this.scheduler.enqueue(this.chainTask("on-demand", onDemandKey(sym), sym, FULL_CHAIN, null));
    if (outcome.ok) return { status: "refreshed" };
    return outcome.cancelled ? { status: "unavailable", message: "Refresh cancelled." } : { status: "failed", message: outcome.error };
  }

  // ---------------- connection ----------------

  private onSession(s: TwsSession | null): void {
    this.session = s;
    this.lines.reset();
    if (!s) { this.scheduler.cancel(); this.scheduler.pause(); this.emit(); return; }
    this.onConnectionChange();
    void this.heartbeat();
    void this.pollWants();
  }

  private onConnectionChange(): void {
    const c = this.supervisor.state();
    const runnable = this.session !== null && c.paused === null;
    if (runnable && this.scheduler.isPaused) { this.scheduler.resume(); void this.pollWants(); }
    else if (!runnable && !this.scheduler.isPaused) this.scheduler.pause();
    this.emit();
  }

  private emit(): void {
    try { this.d.onStatus(this.status()); } catch (e) { this.d.log.error(`bridge status listener: ${(e as Error).message}`); }
  }

  private async heartbeat(): Promise<void> {
    const c = this.supervisor.state();
    try {
      await this.d.ingest.heartbeat({ client: this.d.clientName, ibkr_connected: c.tws === "connected", account_type: c.accountType });
    } catch { /* engine not up yet: next beat */ }
  }

  // ---------------- wants → tasks ----------------

  async pollWants(): Promise<void> {
    if (this.polling || !this.session || this.supervisor.state().paused) return;
    this.polling = true;
    try {
      this.wants = parseWants(await this.d.ingest.wants());
      this.enqueuePlan(this.wants);
    } catch (e) {
      this.lastError = `wants: ${(e as Error).message}`;
      this.d.log.warn(`bridge ${this.lastError}`);
      this.emit();
    } finally {
      this.polling = false;
    }
  }

  private enqueuePlan(w: Wants): void {
    const now = this.now();
    const plan = planWants(w, {
      now,
      lastChainPush: (s) => this.lastChainAttempt.get(s),
      lastStatsPush: (s) => this.lastStatsPush.get(s),
      attemptedWithin: (k, ms) => this.d.ledger.attemptedWithin(k, ms),
    });
    if (plan.eod) {
      const day = plan.eod.sessionDate;
      for (const s of plan.eod.symbols) this.enqueue(this.chainTask("eod-chains", `eod:${day}:${s}`, s, FULL_CHAIN, day));
    }
    for (const s of plan.universe.symbols) this.enqueue(this.chainTask("chain-universe", `universe:${s}`, s, plan.universe.shape, null));
    if (plan.stats.length && this.idle("underlying-stats")) {
      const day = w.eodChains?.sessionDate ?? nyDate(new Date(now));
      const n = this.lines.size;
      for (let i = 0; i < plan.stats.length; i += n) this.enqueue(this.statsTask(plan.stats.slice(i, i + n), day));
    }
    if (plan.indexQuotes.length) {
      this.enqueue(this.task("index", "index-quotes", "VIX family quotes", async (ctx) => { await pushIndexQuotes(ctx, plan.indexQuotes); this.pushed(); }));
    }
    for (const s of plan.indexBars) {
      this.enqueue(this.task("index", `index-bars:${s}`, `${s} daily bars`, async (ctx) => { await pushIndexBars(ctx, s); this.pushed(); },
        { gate: this.pacer, ledgerKey: ledgerKey.indexBars(s) }));
    }
    if (this.idle("option-bars") && this.idle("iv-history")) this.backfill = { total: 0, done: 0, failed: 0 };
    for (const o of plan.optionBars) this.enqueue(this.optionBarsPlanTask(o.symbol, o.sessions, o.shape));
    for (const v of plan.volHistory) {
      this.enqueue(this.task("iv-history", `vol:${v.kind}:${v.symbol}`, `${v.symbol} ${v.kind === "iv30" ? "IV" : "HV"} history`,
        async (ctx) => { if (await pushVolHistory(ctx, v.symbol, v.from, v.kind)) this.pushed(); },
        { gate: this.pacer, ledgerKey: ledgerKey.vol(v.kind, v.symbol), maxAttempts: 6 }));
    }
  }

  private idle(kind: JobKind): boolean {
    return this.scheduler.counts()[kind] === 0 && this.scheduler.current()?.kind !== kind;
  }

  private enqueue(t: Task): void {
    if (this.scheduler.has(t.key)) return;
    const historical = HISTORICAL_KINDS.includes(t.kind) && t.gate !== undefined;
    if (historical) this.backfill.total += 1;
    void this.scheduler.enqueue(t).then((o) => {
      if (!historical || (!o.ok && o.cancelled)) return;
      if (o.ok) this.backfill.done += 1; else this.backfill.failed += 1;
      this.emit();
    });
  }

  private pushed(): void {
    this.lastPushAt = new Date(this.now()).toISOString();
  }

  // ---------------- task factories ----------------

  private context(signal: AbortSignal, checkpoint: () => Promise<void>): JobContext {
    const session = this.session;
    if (!session) throw new Error("not connected to IB Gateway / TWS");
    return { session, ingest: this.d.ingest, pacer: this.pacer, lines: this.lines, now: () => new Date(this.now()), signal, checkpoint, batchTimeoutMs: this.d.batchTimeoutMs };
  }

  /** Wraps a job: context, ledger marking (not on retry/cancel), competing-session detection, logging. */
  private task(kind: JobKind, key: string, label: string, work: (ctx: JobContext) => Promise<void>,
    o: { gate?: HistoricalPacer; ledgerKey?: string; maxAttempts?: number } = {}): Task {
    return {
      key, kind, label, gate: o.gate, maxAttempts: o.maxAttempts,
      run: async (signal, checkpoint) => {
        try {
          await work(this.context(signal, checkpoint));
          if (o.ledgerKey) this.d.ledger.mark(o.ledgerKey);
        } catch (e) {
          if (signal.aborted || e instanceof RetryLater) throw e;
          if (o.ledgerKey) this.d.ledger.mark(o.ledgerKey);
          if (isCompetingSession(e)) this.supervisor.pause(COMPETING_MESSAGE, COMPETING_PAUSE_MS);
          this.lastError = `${label}: ${(e as Error).message}`;
          this.d.log.warn(`bridge ${this.lastError}`);
          throw e;
        }
      },
    };
  }

  private chainTask(kind: JobKind, key: string, symbol: string, shape: ChainShape, sessionDate: string | null): Task {
    const final = sessionDate !== null;
    return this.task(kind, key, `${symbol} ${final ? "closing chain" : "chain"}`, async (ctx) => {
      this.lastChainAttempt.set(symbol, this.now());
      const r = await pushChain(ctx, { symbol, shape, final, sessionDate });
      if (r.coverage < FINAL_MIN_COVERAGE) {
        this.incompleteChain = { symbol, coverage: r.coverage };
        this.d.log.warn(`bridge: ${symbol} chain incomplete (${Math.round(r.coverage * 100)}% of contracts quoted)`);
      } else if (this.incompleteChain?.symbol === symbol) this.incompleteChain = null;
      this.feed.optionsDelayed = r.optionsDelayed;
      this.feed.stocksDelayed = r.underlyingDelayed;
      this.lastChainPush.set(symbol, this.now());
      this.pushed();
    }, { ledgerKey: final ? ledgerKey.eod(sessionDate, symbol) : undefined });
  }

  private statsTask(symbols: string[], sessionDate: string): Task {
    return this.task("underlying-stats", `stats:${symbols[0]}:${symbols.length}`, `stats ${symbols[0]}…${symbols[symbols.length - 1]}`, async (ctx) => {
      const done = await pushUnderlyingStats(ctx, symbols, sessionDate);
      const t = this.now();
      // Symbols without data are marked too, so they wait max_age like the rest instead of re-queuing.
      for (const s of symbols) this.lastStatsPush.set(s, t);
      if (done.symbols.length) {
        this.feed.stocksDelayed = done.delayed;
        this.feed.optionTicksMissing = !done.optionTicks;
        this.pushed();
      }
    });
  }

  private optionBarsPlanTask(symbol: string, sessions: string[], shape: ChainShape): Task {
    return this.task("option-bars", `obars-plan:${symbol}`, `${symbol} option bars (contracts)`, async (ctx) => {
      const r = await resolveOptionContracts(ctx, symbol, shape);
      for (const c of r.contracts) this.enqueue(this.optionBarsTask(symbol, c, sessions));
    }, { ledgerKey: ledgerKey.optionBars(symbol, sessions) });
  }

  private optionBarsTask(symbol: string, c: TwsContract, sessions: string[]): Task {
    const label = `${symbol} ${c.lastTradeDateOrContractMonth} ${c.strike}${c.right} bars`;
    return this.task("option-bars", `obars:${symbol}:${c.conId ?? `${c.lastTradeDateOrContractMonth}${c.right}${c.strike}`}`, label,
      async (ctx) => { if (await pushOptionBars(ctx, symbol, c, sessions)) this.pushed(); }, { gate: this.pacer, maxAttempts: 6 });
  }
}
