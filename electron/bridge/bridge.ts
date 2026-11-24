// IBKR bridge orchestrator: keeps the TWS session (TwsSupervisor), heartbeats the engine, polls
// /api/ingest/wants and turns the plan into scheduler tasks in the §6 priority order, serves
// client-triggered refreshes and reports status. All TWS / HTTP specifics live in jobs.ts and below.
import type { BridgeSettings, BridgeStatus, IntradayRequest, IntradayResult, JobKind, RefreshResult } from "../shared/api";
import type { Logger } from "../main/log";
import { batchLines } from "../main/settings";
import { FULL_CHAIN, underlyingContract, type ChainShape } from "./chain";
import { COMPETING_MESSAGE, COMPETING_PAUSE_MS, TwsSupervisor } from "./connection";
import { ContractCache, type ContractStore } from "./contract-cache";
import { ContractServer, SECDEF_PROBE_MS, type FarmState } from "./farms";
import { FailureLog } from "./failures";
import type { IngestApi, RefreshResultBody } from "./ingest";
import { CONTRACTS_UNAVAILABLE, ContractsUnavailable, FINAL_MIN_COVERAGE, OPTION_QUOTES_CLOSED, OptionQuotesClosed, pushChain, type ChainPushed, pushIndexBars, pushIndexQuotes, pushIntradayBars, pushLiveQuotes, pushOptionBars, pushUnderlyingStats, pushVolHistory, resolveOptionContracts, type JobContext } from "./jobs";
import type { Ledger } from "./ledger";
import { LineBudget } from "./line-budget";
import { nyDate } from "./ny";
import { OptionsFeed, type OptionsFeedStore } from "./options-feed";
import { chunks, durationOf } from "./intraday";
import { HistoricalPacer, IntradayGate, type Gate } from "./pacer";
import { Parked, RetryLater, Scheduler, URGENT, type Outcome, type Task } from "./scheduler";
import { isCompetingSession, isPermissionError, type TwsConnector, type TwsContract, type TwsSession } from "./tws";
import { HIST_RETRY_MS, ledgerKey, parseLiveWants, parseWants, planWants, type Wants } from "./wants";

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
  /** How often the engine is asked for the symbols on screen (live fallback, INTEGRATION.md §9). */
  liveMs?: number;
  batchTimeoutMs?: number;
  /** Where saved contract definitions live (one entry per symbol; none: kept nowhere). */
  contractStore?: ContractStore | null;
  /** Where the account's options-feed state lives (none: re-learned each launch). */
  optionsFeedStore?: OptionsFeedStore | null;
}

/** Live fallback: the engine is asked this often for the symbols on screen (it answers none while Questrade is live). */
const LIVE_POLL_MS = 5_000;
const LIVE_KEY = "live-quotes";
const onDemandKey = (symbol: string) => `ondemand:${symbol.toUpperCase()}`;
/** Scheduled chain work: a round of it is the IBKR source's "last sweep" (Settings → Data sources). */
const CHAIN_ROUND_KINDS: readonly JobKind[] = ["chain-universe", "eod-chains"];

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

/** Local wall-clock time "18:42" (the status is read on this computer). */
function hhmm(t: number | string | null): string {
  if (t === null) return "?";
  const d = new Date(t);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

const unusable = (s: FarmState) => s === "down" || s === "connecting";

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
  private readonly intradayGate: IntradayGate;
  private readonly lines: LineBudget;
  private readonly contracts: ContractCache;
  private readonly contractServer: ContractServer;
  private readonly optionsFeed: OptionsFeed;
  private readonly now: () => number;
  /** The sec-def farm state last seen (its return to OK resumes parked chains). */
  private secDefState: FarmState = "unknown";
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
  /** Historical tasks of this round (since the queues were last idle), counted as they are queued and finish. */
  private round = { obars: { total: 0, done: 0, failed: 0 }, vol: { total: 0, done: 0, failed: 0 } };
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
  /** The after-hours fill in progress (status), counted as its stats batches and chains are queued and finish. */
  private populate: { sessionDate: string; done: number; total: number } | null = null;
  /** A chain fill wanted while the stats fill still ran: the wants are polled again once the stats are done. */
  private chainFillDeferred = false;
  /** Failure reasons of IBKR work (grouped, last 24 h). */
  private readonly failures: FailureLog;
  /** Chain tasks (universe, closing chains, fills) since the chain queues were last idle, and the last such round. */
  private chainRound = { done: 0, failed: 0 };
  private lastChainRound: { at: string; detail: string } | null = null;

  constructor(private readonly d: BridgeDeps) {
    this.now = d.now ?? Date.now;
    this.pacer = d.pacer ?? new HistoricalPacer({ now: this.now });
    this.intradayGate = new IntradayGate({ now: this.now });
    this.lines = new LineBudget(() => batchLines(d.settings()));
    this.scheduler = new Scheduler({ now: this.now, onChange: () => this.emit() });
    this.contracts = new ContractCache(d.contractStore ?? null);
    this.contractServer = new ContractServer(() => this.supervisor.state().farms.secDef.state, this.now);
    this.optionsFeed = new OptionsFeed(d.optionsFeedStore ?? null);
    this.failures = new FailureLog(this.now);
    this.supervisor = new TwsSupervisor({
      connector: d.connector, settings: d.settings, log: d.log, now: this.now,
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
    this.timers.push(setInterval(() => void this.pollLive(), this.d.liveMs ?? LIVE_POLL_MS));
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
    this.resetFill();
    this.scheduler.cancel();
    this.lines.reset();
    this.supervisor.restart();
  }

  /** User cancel: also forgets accepted option-bar plans, so they are not resumed. */
  cancel(kind?: JobKind): void {
    if (kind === undefined || kind === "option-bars") this.obarsPlans.clear();
    if (kind === undefined || kind === "underlying-stats" || kind === "chain-universe") this.resetFill();
    this.scheduler.cancel({ kind });
  }

  /** The page that asked for this refresh went away: drop it if it has not finished. */
  cancelRefresh(symbol: string): void { this.scheduler.cancel({ key: onDemandKey(symbol) }); }

  status(): BridgeStatus {
    const c = this.supervisor.state();
    return {
      enabled: this.d.settings().enabled, tws: c.tws, port: c.port, endpoint: c.endpoint, accountType: c.accountType, paused: c.paused,
      lastError: c.lastError ?? this.lastError, notes: this.notes(), lastPushAt: this.lastPushAt,
      current: this.scheduler.current(), queued: this.scheduler.counts(), backfill: this.backfill(),
      populate: this.populate ? { ...this.populate } : null,
      farms: c.farms, contractsAvailable: this.contractServer.available(), optionsSubscription: this.optionsFeed.mode,
      optionsDelayed: this.feed.optionsDelayed, lastChainAt: this.lastChainAt(), lastChainRound: this.lastChainRound,
      failures: this.failures.top(), linesPerBatch: this.lines.size,
    };
  }

  private lastChainAt(): string | null {
    let last = -Infinity;
    for (const t of this.lastChainPush.values()) last = Math.max(last, t);
    return Number.isFinite(last) ? new Date(last).toISOString() : null;
  }

  /**
   * Backfill progress. IV/HV history is counted over every symbol the engine keeps history for (two series each,
   * wants.history_symbols): a series already current, or attempted recently, is done — so a restart or an app
   * update does not start the count from zero. Option-bar catch-up is counted per round.
   */
  private backfill(): { total: number; done: number; failed: number } {
    const o = this.round.obars;
    const v = this.round.vol;
    const symbols = this.wants?.historySymbols;
    if (symbols === null || symbols === undefined) return { total: o.total + v.total, done: o.done + v.done, failed: o.failed + v.failed };
    const volTotal = Math.max(2 * symbols, v.total);
    const volLeft = this.scheduler.counts()["iv-history"] + (this.scheduler.current()?.kind === "iv-history" ? 1 : 0);
    return { total: o.total + volTotal, done: o.done + Math.max(0, volTotal - volLeft - v.failed), failed: o.failed + v.failed };
  }

  private notes(): string[] {
    const out: string[] = [...this.serverNotes()];
    if (this.optionsFeed.mode === "delayed_only") out.push("US options: delayed only (no OPRA subscription) — option quotes are collected during market hours (09:30–16:00 ET)");
    else if (this.feed.optionsDelayed) out.push("IBKR delayed (no OPRA subscription)");
    if (this.feed.stocksDelayed) out.push("IBKR delayed stock quotes (no real-time US stock subscription)");
    if (this.feed.optionTicksMissing) out.push("IBKR sends no option volume / open interest for underlyings (generic ticks 100/101); sent as unknown");
    const reduced = this.lines.reduced;
    if (reduced !== null) out.push(`IBKR market-data line limit reached: batches reduced to ${reduced} lines (Settings → Market data lines is ${this.d.settings().marketDataLines})`);
    const denied = [...this.denied.values()].map((d) => d.label);
    if (denied.length) out.push(`No IBKR market-data permission (skipped): ${denied.slice(0, 3).join(", ")}${denied.length > 3 ? ` and ${denied.length - 3} more` : ""}`);
    if (this.chainGaps) out.push(chainGapsText(this.chainGaps));
    return out;
  }

  /** IBKR servers the Gateway reports unavailable (or that do not answer), and what the bridge does meanwhile. */
  private serverNotes(): string[] {
    const f = this.supervisor.state().farms;
    const out: string[] = [];
    const parked = this.scheduler.parked();
    const waiting = parked ? `; ${parked} chain task${parked === 1 ? "" : "s"} without one wait${parked === 1 ? "s" : ""} until it is back` : "";
    const unanswered = this.contractServer.unansweredSince();
    if (unusable(f.secDef.state)) {
      out.push(`IBKR sec-def server: unavailable since ${hhmm(f.secDef.since)} (often weekend maintenance) — chains use saved contract lists${waiting}`);
    } else if (unanswered !== null) {
      const head = f.secDef.state === "unknown"
        ? `IBKR sec-def server: not answering since ${hhmm(unanswered)} (IB Gateway reported no status; often weekend maintenance)`
        : `IBKR sec-def server: reported connected but not answering contract requests since ${hhmm(unanswered)}`;
      out.push(`${head} — chains use saved contract lists${waiting}; asking again at ${hhmm(unanswered + SECDEF_PROBE_MS)}`);
    }
    if (f.marketData.state === "down") out.push(`IBKR market-data server: unavailable since ${hhmm(f.marketData.since)}`);
    if (f.historical.state === "down") out.push(`IBKR historical-data server (HMDS): unavailable since ${hhmm(f.historical.since)}`);
    return out;
  }

  /**
   * An on-demand refresh (the app asks right after starting the engine's refresh round, INTEGRATION.md §7): "pending"
   * is reported to that round at once, the outcome when known (POST /api/ingest/refresh-result), and returned.
   */
  async refreshSymbol(symbol: string): Promise<RefreshResult> {
    const sym = symbol.toUpperCase();
    await this.reportRefresh({ source: "ibkr", symbol: sym, status: "pending", message: null, waiting_for: null });
    const r = await this.refresh(sym);
    await this.reportRefresh({ source: "ibkr", symbol: sym, status: r.status,
      message: "message" in r ? r.message : null, waiting_for: "waitingFor" in r ? r.waitingFor ?? null : null });
    return r;
  }

  private async reportRefresh(b: RefreshResultBody): Promise<void> {
    try { await this.d.ingest.refreshResult(b); } catch (e) { this.d.log.warn(`bridge refresh-result: ${(e as Error).message}`); }
  }

  /** Why on-demand work cannot run now (bridge off, not connected, paused), else null. */
  private unavailable(): string | null {
    const c = this.supervisor.state();
    if (!this.d.settings().enabled) return "The IBKR bridge is turned off.";
    if (!this.session) return "Not connected to IB Gateway / TWS.";
    return c.paused;
  }

  private async refresh(sym: string): Promise<RefreshResult> {
    const why = this.unavailable();
    if (why) return { status: "unavailable", message: why };
    const last = this.lastChainPush.get(sym);
    if (last !== undefined && this.now() - last < (this.wants?.onDemandMaxAgeS ?? 120) * 1000) return { status: "fresh" };
    const marketHours = { status: "unavailable", message: OPTION_QUOTES_CLOSED, waitingFor: "market-hours" } as const;
    const contractServer = { status: "unavailable", message: CONTRACTS_UNAVAILABLE, waitingFor: "contract-server" } as const;
    if (!this.optionsFeed.canQuote(new Date(this.now()))) return marketHours;
    if (!this.contractServer.available() && !this.contracts.saved(underlyingContract(sym).symbol, new Date(this.now()))) return contractServer;
    const outcome = await this.scheduler.enqueue(this.chainTask("on-demand", onDemandKey(sym), sym, FULL_CHAIN, null));
    if (outcome.ok) return this.noOptions.has(sym) ? { status: "no-options" } : { status: "refreshed" };
    if (outcome.cancelled) return { status: "unavailable", message: "Refresh cancelled." };
    if (outcome.error === OPTION_QUOTES_CLOSED) return marketHours;
    if (outcome.error === CONTRACTS_UNAVAILABLE) return contractServer;
    return { status: "failed", message: outcome.error };
  }

  /**
   * Intraday bars for the spans the engine could not fill (INTEGRATION.md §5 "Intraday bars"): one on-demand task per
   * request window (newest first), each paced by the HistoricalPacer and the IntradayGate, pushed as it arrives.
   */
  async intradayBars(req: IntradayRequest): Promise<IntradayResult> {
    const why = this.unavailable();
    if (why) return { status: "unavailable", message: why };
    const windows = req.spans.flatMap((s) => chunks(Date.parse(s.start), Date.parse(s.end), req.interval));
    let bars = 0;
    for (const c of windows) {
      const key = `intraday:${req.symbol}:${req.interval}:${c.end}:${durationOf(c)}`;
      const gate = { readyAt: () => Math.max(this.pacer.readyAt(), this.intradayGate.readyAt(key)) };
      const outcome = await this.enqueue(this.task(URGENT, key, `${req.symbol} ${req.interval} bars`, async (ctx) => {
        this.intradayGate.begin(key);
        bars += await pushIntradayBars(ctx, req.symbol, req.interval, c);
        this.pushed();
      }, { gate, symbol: req.symbol }));
      if (!outcome.ok) return { status: outcome.cancelled ? "unavailable" : "failed", message: outcome.cancelled ? "Cancelled." : outcome.error };
    }
    return { status: "fetched", bars };
  }

  // ---------------- connection ----------------

  private onSession(s: TwsSession | null): void {
    this.session = s;
    this.lines.reset();
    if (!s) { this.resetFill(); this.scheduler.cancel(); this.scheduler.pause(); this.emit(); return; }
    this.contractServer.answered();   // a new Gateway session: contract requests are tried afresh
    this.onConnectionChange();
    void this.heartbeat();
    void this.pollWants();
  }

  private onConnectionChange(): void {
    const c = this.supervisor.state();
    const secDef = c.farms.secDef.state;
    if (secDef !== this.secDefState) {
      const was = this.secDefState;
      this.secDefState = secDef;
      if (secDef === "ok") {
        // 2158: contract definitions can be asked for again; parked chains resume.
        this.contractServer.answered();
        if (unusable(was)) this.d.log.info("bridge: IBKR sec-def server is back; resuming chains waiting for contract definitions");
        this.scheduler.recheck();
      } else if (unusable(secDef)) this.d.log.warn(`bridge: IBKR sec-def server ${secDef}; chains use saved contract lists`);
    }
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
      const f = c.farms;
      await this.d.ingest.heartbeat({
        client: this.d.clientName, ibkr_connected: c.tws === "connected", account_type: c.accountType,
        farms: c.tws === "connected" ? { market_data: f.marketData.state, historical: f.historical.state, sec_def: f.secDef.state } : null,
        options_subscription: this.optionsFeed.mode,
      });
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

  /**
   * Live fallback (INTEGRATION.md §9): while Questrade is unavailable the engine names the symbols on screen; one
   * snapshot task of them at a time (a "live" share of TWS time, never pre-empting other work).
   */
  async pollLive(): Promise<void> {
    if (!this.session || this.supervisor.state().paused || this.scheduler.has(LIVE_KEY)) return;
    let symbols: string[];
    try {
      symbols = parseLiveWants(await this.d.ingest.liveWants()).symbols;
    } catch {
      return;   // an older engine without live quotes, or not up yet: next poll
    }
    if (!symbols.length) return;
    void this.enqueue(this.task("live", LIVE_KEY, `live quotes (${symbols.length} symbols)`, async (ctx) => { if (await pushLiveQuotes(ctx, symbols)) this.pushed(); }));
  }

  private enqueuePlan(w: Wants): void {
    const now = this.now();
    const plan = planWants(w, {
      now,
      lastChainPush: (s) => this.lastChainAttempt.get(s),
      lastStatsPush: (s) => this.lastStatsPush.get(s),
      attemptedWithin: (k, ms) => this.d.ledger.attemptedWithin(k, ms),
    });
    // Option quotes the account cannot get now (no OPRA subscription, outside market hours) are not asked for.
    const optionQuotes = this.optionsFeed.canQuote(new Date(now));
    if (plan.eod && optionQuotes) {
      const day = plan.eod.sessionDate;
      for (const s of plan.eod.symbols) this.enqueue(this.chainTask("eod-chains", `eod:${day}:${s}`, s, FULL_CHAIN, day));
    }
    const queueStats = plan.stats.length > 0 && this.idle("underlying-stats");
    // An after-hours fill collects the stats first (fast, and they rank the chain universe by liquidity): its chains
    // are asked for again once no stats work is left.
    const universe = optionQuotes ? plan.universe.symbols : [];
    this.chainFillDeferred = plan.universe.asOfSession !== null && universe.length > 0 && (queueStats || !this.idle("underlying-stats"));
    if (!this.chainFillDeferred) {
      for (const s of universe) this.fillTask(plan.universe.asOfSession, this.chainTask("chain-universe", `universe:${s}`, s, plan.universe.shape, null), 1);
    }
    if (queueStats) {
      const fill = plan.statsAsOfSession;
      const day = fill ?? w.eodChains?.sessionDate ?? nyDate(new Date(now));
      const n = this.lines.size;
      for (let i = 0; i < plan.stats.length; i += n) this.fillTask(fill, this.statsTask(plan.stats.slice(i, i + n), day), Math.min(n, plan.stats.length - i));
    }
    if (this.populate && this.populate.done >= this.populate.total && !this.chainFillDeferred) this.populate = null;
    if (plan.indexQuotes.length) {
      this.enqueue(this.task("index", "index-quotes", "VIX family quotes", async (ctx) => { await pushIndexQuotes(ctx, plan.indexQuotes); this.pushed(); }));
    }
    for (const { symbol: s, from } of plan.indexBars) {
      this.enqueue(this.task("index", `index-bars:${s}`, `${s} daily bars`, async (ctx) => { if (await pushIndexBars(ctx, s, from)) this.pushed(); },
        { gate: this.pacer, ledgerKey: ledgerKey.indexBars(s), group: group.indexBars(s) }));
    }
    if (this.idle("option-bars") && this.idle("iv-history")) this.round = { obars: { total: 0, done: 0, failed: 0 }, vol: { total: 0, done: 0, failed: 0 } };
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

  /**
   * Queues a task; with `fillSession` it is part of that session's after-hours fill: `items` (symbols) count towards
   * the populate progress, and the last stats batch re-polls the wants for a deferred chain fill.
   */
  private fillTask(fillSession: string | null, t: Task, items: number): void {
    if (fillSession === null) { void this.enqueue(t); return; }
    if (this.scheduler.has(t.key)) return;
    if (this.populate?.sessionDate !== fillSession) this.populate = { sessionDate: fillSession, done: 0, total: 0 };
    const p = this.populate;
    p.total += items;
    void this.enqueue(t).then(() => {
      p.done += items;
      if (this.populate === p && p.done >= p.total && !this.chainFillDeferred) this.populate = null;
      if (t.kind === "underlying-stats" && this.chainFillDeferred && this.idle("underlying-stats")) void this.pollWants();
      this.emit();
    });
  }

  /** Its queued work is dropped (disconnect, cancel, new settings): the next wants poll counts a fill afresh. */
  private resetFill(): void {
    this.populate = null;
    this.chainFillDeferred = false;
  }

  private idle(kind: JobKind): boolean {
    return this.scheduler.counts()[kind] === 0 && this.scheduler.current()?.kind !== kind;
  }

  /** Queues a task (or joins the queued one with its key); a task of a denied group fails at once. */
  private enqueue(t: Task): Promise<Outcome> {
    if (this.isDenied(t.group)) return Promise.resolve({ ok: false, cancelled: false, error: "no market-data permission" });
    if (this.scheduler.has(t.key)) return this.scheduler.enqueue(t);
    const counter = t.gate === undefined ? null : t.kind === "option-bars" ? this.round.obars : t.kind === "iv-history" ? this.round.vol : null;
    if (counter) counter.total += 1;
    const outcome = this.scheduler.enqueue(t);
    void outcome.then((o) => {
      if (CHAIN_ROUND_KINDS.includes(t.kind) && !(!o.ok && o.cancelled)) this.chainDone(o.ok);
      if (!counter || (!o.ok && o.cancelled)) return;
      if (o.ok) counter.done += 1; else counter.failed += 1;
      this.emit();
    });
    return outcome;
  }

  /** Counts a finished chain task; once no chain work is queued the round is the "last sweep" of the IBKR source. */
  private chainDone(ok: boolean): void {
    if (ok) this.chainRound.done += 1; else this.chainRound.failed += 1;
    if (!CHAIN_ROUND_KINDS.every((k) => this.idle(k))) return;
    const { done, failed } = this.chainRound;
    this.lastChainRound = { at: new Date(this.now()).toISOString(), detail: `${done} chain${done === 1 ? "" : "s"}${failed ? `, ${failed} failed` : ""}` };
    this.chainRound = { done: 0, failed: 0 };
    this.emit();
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
    return {
      session, ingest: this.d.ingest, pacer: this.pacer, lines: this.lines, now: () => new Date(this.now()), signal, checkpoint, batchTimeoutMs: this.d.batchTimeoutMs,
      contracts: this.contracts, contractServer: this.contractServer, optionsFeed: this.optionsFeed,
    };
  }

  /**
   * Wraps a job: context, ledger marking (not on retry/cancel), competing-session detection, logging. Work that needs
   * IBKR's contract server while it is unavailable (and no saved contract list) is parked until it is back; option
   * quotes the account cannot get now are skipped. An on-demand refresh reports both instead (refreshSymbol).
   */
  private task(kind: JobKind, key: string, label: string, work: (ctx: JobContext) => Promise<void>,
    o: { gate?: Gate; ledgerKey?: string; maxAttempts?: number; group?: Group; symbol?: string } = {}): Task {
    return {
      key, kind, label, gate: o.gate, maxAttempts: o.maxAttempts, group: o.group?.id,
      // Historical requests (the pacer-gated tasks) give way to an on-demand refresh at once: aborted, re-queued.
      preemptible: o.gate !== undefined && kind !== URGENT,
      run: async (signal, checkpoint) => {
        try {
          await work(this.context(signal, checkpoint));
          if (o.ledgerKey) this.d.ledger.mark(o.ledgerKey);
        } catch (e) {
          if (signal.aborted || e instanceof RetryLater) throw e;
          if (e instanceof ContractsUnavailable && kind !== URGENT) throw new Parked(this.contractServer, e.message);
          if (e instanceof ContractsUnavailable || (e instanceof OptionQuotesClosed && kind === URGENT)) throw e;
          if (o.ledgerKey) this.d.ledger.mark(o.ledgerKey);
          if (e instanceof OptionQuotesClosed) return;
          if (isCompetingSession(e)) this.supervisor.pause(COMPETING_MESSAGE, COMPETING_PAUSE_MS);
          if (o.group && isPermissionError(e)) this.deny(o.group.id, o.group.label, e);
          this.failures.record((e as Error).message, o.symbol);
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
    }, { ledgerKey: final ? ledgerKey.eod(sessionDate, symbol) : undefined, symbol });
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
        if (!ctx.signal.aborted && !(e instanceof RetryLater) && !(e instanceof ContractsUnavailable)) this.planDone(planKey);
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
