// IBKR bridge orchestrator: keeps the TWS session (TwsSupervisor), heartbeats the engine, polls
// /api/ingest/wants and turns the plan into scheduler tasks in the §6 priority order, serves
// client-triggered refreshes and reports status. All TWS / HTTP specifics live in jobs.ts and below.
import type { BridgeSettings, BridgeStatus, JobKind, RefreshResult } from "../shared/api";
import type { Logger } from "../main/log";
import { batchLines } from "../main/settings";
import { FULL_CHAIN, type ChainShape } from "./chain";
import { COMPETING_MESSAGE, COMPETING_PAUSE_MS, TwsSupervisor } from "./connection";
import type { IngestApi } from "./ingest";
import { FINAL_MIN_COVERAGE, pushChain, type ChainPushed, pushIndexBars, pushIndexQuotes, pushOptionBars, pushUnderlyingStats, pushVolHistory, resolveOptionContracts, type JobContext } from "./jobs";
import type { Ledger } from "./ledger";
import { LineBudget } from "./line-budget";
import { nyDate } from "./ny";
import { OptionParamsCache } from "./option-params";
import { HistoricalPacer } from "./pacer";
import { RetryLater, Scheduler, type Outcome, type Task } from "./scheduler";
import { isCompetingSession, isPermissionError, type TwsConnector, type TwsContract, type TwsSession } from "./tws";
import { HIST_RETRY_MS, ledgerKey, parseWants, planWants, type Wants } from "./wants";

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

/** Tasks that need the same market-data permission (one kind of data for one symbol). */
interface Group { id: string; label: string }
const group = {
  optionBars: (s: string): Group => ({ id: `option-bars:${s}`, label: `${s} option bars` }),
  volHistory: (s: string): Group => ({ id: `iv-history:${s}`, label: `${s} IV/HV history` }),
  indexBars: (s: string): Group => ({ id: `index-bars:${s}`, label: `${s} daily bars` }),
};

interface ChainGaps { symbol: string; coverage: number; noQuotes: string[]; noContracts: string[] }

/** What a posted chain is missing, or null when complete enough (≥ FINAL_MIN_COVERAGE, every expiry quoted). */
function chainGaps(symbol: string, r: ChainPushed): ChainGaps | null {
  const noQuotes = r.expiries.filter((e) => e.contracts > 0 && e.quoted === 0).map((e) => e.expiry);
  const noContracts = r.expiries.filter((e) => e.contracts === 0).map((e) => e.expiry);
  if (r.coverage >= FINAL_MIN_COVERAGE && !noQuotes.length && !noContracts.length) return null;
  return { symbol, coverage: r.coverage, noQuotes, noContracts };
}

function chainGapsText(g: ChainGaps): string {
  const parts = [`${Math.round(g.coverage * 100)}% of contracts quoted`];
  if (g.noQuotes.length) parts.push(`no quotes for ${g.noQuotes.join(", ")}`);
  if (g.noContracts.length) parts.push(`no contracts from IBKR for ${g.noContracts.join(", ")}`);
  return `Incomplete IBKR chain for ${g.symbol}: ${parts.join("; ")}`;
}

export class Bridge {
  readonly scheduler: Scheduler;
  private readonly supervisor: TwsSupervisor;
  private readonly pacer: HistoricalPacer;
  private readonly lines: LineBudget;
  private readonly optionParams = new OptionParamsCache();
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
  /** Accepted option-bar catch-up plans not finished yet, by plan ledger key. */
  private readonly obarsPlans = new Map<string, { symbol: string; sessions: string[]; shape: ChainShape }>();
  /** Option-bar plans (ledger keys) whose contract tasks are still queued or running. */
  private readonly obarsInFlight = new Set<string>();
  /** Task groups (symbol + kind) TWS refused for lack of a market-data permission: when, and what. */
  private readonly denied = new Map<string, { at: number; label: string }>();
  /** Symbols whose last chain attempt found that IBKR lists no options. */
  private readonly noOptions = new Set<string>();
  /** Last chain posted with gaps (status note): under FINAL_MIN_COVERAGE quoted, expiries without quotes or contracts. */
  private chainGaps: ChainGaps | null = null;

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

  /** User cancel: also forgets accepted option-bar plans, so they are not resumed. */
  cancel(kind?: JobKind): void {
    if (kind === undefined || kind === "option-bars") this.obarsPlans.clear();
    this.scheduler.cancel({ kind });
  }

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
    const denied = [...this.denied.values()].map((d) => d.label);
    if (denied.length) out.push(`No IBKR market-data permission (skipped): ${denied.slice(0, 3).join(", ")}${denied.length > 3 ? ` and ${denied.length - 3} more` : ""}`);
    if (this.chainGaps) out.push(chainGapsText(this.chainGaps));
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
    if (outcome.ok) return this.noOptions.has(sym) ? { status: "no-options" } : { status: "refreshed" };
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
    for (const { symbol: s, from } of plan.indexBars) {
      this.enqueue(this.task("index", `index-bars:${s}`, `${s} daily bars`, async (ctx) => { if (await pushIndexBars(ctx, s, from)) this.pushed(); },
        { gate: this.pacer, ledgerKey: ledgerKey.indexBars(s), group: group.indexBars(s) }));
    }
    if (this.idle("option-bars") && this.idle("iv-history")) this.backfill = { total: 0, done: 0, failed: 0 };
    // Accepted catch-up plans are kept until all their contracts are done, so a reconnect resumes them
    // (the engine does not offer an accepted plan again for hours).
    const accepted = plan.optionBars.filter((o) => !this.obarsPlans.has(ledgerKey.optionBars(o.symbol, o.sessions)) && !this.isDenied(group.optionBars(o.symbol).id));
    for (const o of accepted) this.obarsPlans.set(ledgerKey.optionBars(o.symbol, o.sessions), o);
    for (const [k, o] of this.obarsPlans) if (!this.obarsInFlight.has(k)) void this.enqueue(this.optionBarsPlanTask(o.symbol, o.sessions, o.shape));
    // Only accepted offers count on the engine's side (a want the bridge skipped is offered again).
    if (accepted.length) {
      void this.d.ingest.ackWants({ kind: "option_bars", items: accepted.map((o) => ({ symbol: o.symbol, sessions: o.sessions })) })
        .catch((e: unknown) => this.d.log.warn(`bridge ack-wants: ${(e as Error).message}`));
    }
    for (const v of plan.volHistory) {
      this.enqueue(this.task("iv-history", `vol:${v.kind}:${v.symbol}`, `${v.symbol} ${v.kind === "iv30" ? "IV" : "HV"} history`,
        async (ctx) => { if (await pushVolHistory(ctx, v.symbol, v.from, v.kind)) this.pushed(); },
        { gate: this.pacer, ledgerKey: ledgerKey.vol(v.kind, v.symbol), maxAttempts: 6, group: group.volHistory(v.symbol) }));
    }
  }

  private idle(kind: JobKind): boolean {
    return this.scheduler.counts()[kind] === 0 && this.scheduler.current()?.kind !== kind;
  }

  /** Queues a task (or joins the queued one with its key); a task of a denied group fails at once. */
  private enqueue(t: Task): Promise<Outcome> {
    if (this.isDenied(t.group)) return Promise.resolve({ ok: false, cancelled: false, error: "no market-data permission" });
    if (this.scheduler.has(t.key)) return this.scheduler.enqueue(t);
    const historical = HISTORICAL_KINDS.includes(t.kind) && t.gate !== undefined;
    if (historical) this.backfill.total += 1;
    const outcome = this.scheduler.enqueue(t);
    void outcome.then((o) => {
      if (!historical || (!o.ok && o.cancelled)) return;
      if (o.ok) this.backfill.done += 1; else this.backfill.failed += 1;
      this.emit();
    });
    return outcome;
  }

  private isDenied(group: string | undefined): boolean {
    const d = group === undefined ? undefined : this.denied.get(group);
    if (d && this.now() - d.at >= HIST_RETRY_MS) this.denied.delete(group!);
    return d !== undefined && this.denied.has(group!);
  }

  /** A permission error stops the whole group (all its queued tasks fail the same way). */
  private deny(group: string, label: string, e: unknown): void {
    if (!this.denied.has(group)) this.d.log.warn(`bridge: no market-data permission for ${label}; skipping it (${(e as Error).message})`);
    this.denied.set(group, { at: this.now(), label });
    this.scheduler.cancel({ group, queuedOnly: true });   // the failing task itself reports its error
  }

  private pushed(): void {
    this.lastPushAt = new Date(this.now()).toISOString();
  }

  // ---------------- task factories ----------------

  private context(signal: AbortSignal, checkpoint: () => Promise<void>): JobContext {
    const session = this.session;
    if (!session) throw new Error("not connected to IB Gateway / TWS");
    return { session, ingest: this.d.ingest, pacer: this.pacer, lines: this.lines, now: () => new Date(this.now()), signal, checkpoint, batchTimeoutMs: this.d.batchTimeoutMs, optionParams: this.optionParams };
  }

  /** Wraps a job: context, ledger marking (not on retry/cancel), competing-session detection, logging. */
  private task(kind: JobKind, key: string, label: string, work: (ctx: JobContext) => Promise<void>,
    o: { gate?: HistoricalPacer; ledgerKey?: string; maxAttempts?: number; group?: Group } = {}): Task {
    return {
      key, kind, label, gate: o.gate, maxAttempts: o.maxAttempts, group: o.group?.id,
      // Historical requests (the pacer-gated tasks) give way to an on-demand refresh at once: aborted, re-queued.
      preemptible: o.gate !== undefined,
      run: async (signal, checkpoint) => {
        try {
          await work(this.context(signal, checkpoint));
          if (o.ledgerKey) this.d.ledger.mark(o.ledgerKey);
        } catch (e) {
          if (signal.aborted || e instanceof RetryLater) throw e;
          if (o.ledgerKey) this.d.ledger.mark(o.ledgerKey);
          if (isCompetingSession(e)) this.supervisor.pause(COMPETING_MESSAGE, COMPETING_PAUSE_MS);
          if (o.group && isPermissionError(e)) this.deny(o.group.id, o.group.label, e);
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
      if (r === null) { this.noOptions.add(symbol); this.pushed(); return; }
      this.noOptions.delete(symbol);
      const gaps = chainGaps(symbol, r);
      if (gaps) {
        this.chainGaps = gaps;
        this.d.log.warn(`bridge: ${chainGapsText(gaps)}`);
      } else if (this.chainGaps?.symbol === symbol) this.chainGaps = null;
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

  /**
   * Resolves the symbol's contracts and queues one bars task per contract not done yet (per-contract
   * ledger), so a catch-up interrupted by a disconnect, cancel or restart resumes where it stopped. The
   * plan's own ledger key is marked only once every contract task has finished (not when cancelled).
   */
  private optionBarsPlanTask(symbol: string, sessions: string[], shape: ChainShape): Task {
    const planKey = ledgerKey.optionBars(symbol, sessions);
    return this.task("option-bars", `obars-plan:${symbol}`, `${symbol} option bars (contracts)`, async (ctx) => {
      this.obarsInFlight.add(planKey);
      try {
        const r = await resolveOptionContracts(ctx, symbol, shape);
        const todo = r.contracts.filter((c) => !this.d.ledger.attemptedWithin(ledgerKey.optionBar(c, sessions), HIST_RETRY_MS));
        void Promise.all(todo.map((c) => this.enqueue(this.optionBarsTask(symbol, c, sessions)))).then((outcomes) => {
          this.obarsInFlight.delete(planKey);
          if (outcomes.every((o) => o.ok || !o.cancelled)) this.planDone(planKey);
        });
      } catch (e) {
        this.obarsInFlight.delete(planKey);
        if (!ctx.signal.aborted && !(e instanceof RetryLater)) this.planDone(planKey);
        throw e;
      }
    }, { group: group.optionBars(symbol) });
  }

  private planDone(planKey: string): void {
    this.d.ledger.mark(planKey);
    this.obarsPlans.delete(planKey);
  }

  private optionBarsTask(symbol: string, c: TwsContract, sessions: string[]): Task {
    const label = `${symbol} ${c.lastTradeDateOrContractMonth} ${c.strike}${c.right} bars`;
    return this.task("option-bars", `obars:${symbol}:${c.conId ?? `${c.lastTradeDateOrContractMonth}${c.right}${c.strike}`}`, label,
      async (ctx) => { if (await pushOptionBars(ctx, symbol, c, sessions)) this.pushed(); },
      { gate: this.pacer, maxAttempts: 6, group: group.optionBars(symbol), ledgerKey: ledgerKey.optionBar(c, sessions) });
  }
}
