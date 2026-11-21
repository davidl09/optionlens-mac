// Bridge task bodies: talk to TWS through the TwsSession contract, assemble ingest bodies and push them.
// Market-data work is done in batches no larger than the line budget; every historical
// request goes through the pacer (begin → request → success | violation → RetryLater).
import { assembleChain, assembleContract, assembleUnderlying, isoDate, pickExpiries, selectContracts, underlyingContract, type ChainShape } from "./chain";
import type { IngestApi, IngestChain, OptionBar, UnderlyingStat } from "./ingest";
import type { HistoricalPacer } from "./pacer";
import { RetryLater } from "./scheduler";
import type { LineBudget } from "./line-budget";
import { bestPrice, changeFromClose, emptySnapshot, livePrice, TickAccumulator, type QuoteSnapshot } from "./ticks";
import { CODE, isCompetingSession, isNoDataError, isPacingError, NOT_SUBSCRIBED_CODES, TwsRequestError, TwsTimeoutError, type HistBar, type HistoricalRequest, type TwsContract, type TwsSession } from "./tws";
import { durationFor } from "./wants";

export interface JobContext {
  session: TwsSession;
  ingest: IngestApi;
  pacer: HistoricalPacer;
  /** Market-data lines one batch may use. */
  lines: LineBudget;
  now: () => Date;
  signal: AbortSignal;
  /** Lets waiting on-demand work run first; called only while no market-data lines are held. */
  checkpoint: () => Promise<void>;
  /** How long one subscription waits for its data, from the moment its request is sent (or its first tick). */
  batchTimeoutMs?: number;
}

const sleep = (ms: number, signal: AbortSignal) => new Promise<void>((resolve) => {
  const t = setTimeout(resolve, ms);
  signal.addEventListener("abort", () => { clearTimeout(t); resolve(); }, { once: true });
});

function throwIfAborted(signal: AbortSignal): void {
  if (signal.aborted) throw signal.reason instanceof Error ? signal.reason : new Error("cancelled");
}

// ---------------- market data ----------------

export interface CollectOptions { genericTicks: string; complete: (a: TickAccumulator) => boolean; minDwellMs?: number }

interface BatchResult { snaps: QuoteSnapshot[]; lineLimited: boolean[]; notSubscribed: number }

/**
 * Streams one batch until every subscription is complete or has timed out, then cancels them all. Each
 * contract's clock starts when its request is actually sent (the API client paces outgoing messages, and
 * the previous batch's cancels are still ahead in that queue) or at its first tick — never at enqueue.
 */
async function streamBatch(ctx: JobContext, batch: TwsContract[], o: CollectOptions): Promise<BatchResult> {
  const timeout = ctx.batchTimeoutMs ?? 4000;
  const dwell = o.minDwellMs ?? 600;
  const accs = batch.map(() => new TickAccumulator());
  const firstTick: (number | null)[] = batch.map(() => null);
  const sentAt: number[] = [];
  const lineLimited = batch.map(() => false);
  let notSubscribed = 0;
  let fatal: TwsRequestError | null = null;
  const cancels = batch.map((c, j) => {
    const cancel = ctx.session.stream(c, o.genericTicks, {
      onTick: (e) => { firstTick[j] ??= Date.now(); accs[j].apply(e); },
      onError: (err) => {
        accs[j].dead = true;
        if (err.code === CODE.MAX_TICKERS) lineLimited[j] = true;
        else if (NOT_SUBSCRIBED_CODES.has(err.code)) notSubscribed += 1;
        if (isCompetingSession(err)) fatal = err;
      },
    });
    sentAt[j] = Date.now() + ctx.session.sendBacklogMs();
    return cancel;
  });
  const settled = (j: number, now: number) => {
    const t0 = firstTick[j] ?? sentAt[j];
    return now >= t0 + timeout || (now >= t0 + dwell && o.complete(accs[j]));
  };
  try {
    while (!ctx.signal.aborted && !fatal) {
      const now = Date.now();
      if (accs.every((_, j) => settled(j, now))) break;
      await sleep(100, ctx.signal);
    }
  } finally {
    for (const cancel of cancels) cancel();
  }
  if (fatal) throw fatal;
  throwIfAborted(ctx.signal);
  return { snaps: accs.map((a) => a.snapshot()), lineLimited, notSubscribed };
}

/**
 * Streams each contract (≤ ctx.lines.size at a time) and returns one snapshot per contract, in order.
 * Per-contract rejections leave that snapshot empty; a competing session aborts the whole collection;
 * contracts refused for the line limit (error 101) shrink the budget and are retried; if every contract
 * was refused for lack of a subscription, throws.
 */
export async function collectQuotes(ctx: JobContext, contracts: TwsContract[], o: CollectOptions): Promise<QuoteSnapshot[]> {
  const out: QuoteSnapshot[] = contracts.map(() => emptySnapshot());
  let pending = contracts.map((_, i) => i);
  let notSubscribed = 0;
  while (pending.length) {
    await ctx.checkpoint();
    throwIfAborted(ctx.signal);
    const batch = pending.slice(0, ctx.lines.size);
    const r = await streamBatch(ctx, batch.map((i) => contracts[i]), o);
    batch.forEach((i, j) => { out[i] = r.snaps[j]; });
    notSubscribed += r.notSubscribed;
    const refused = batch.filter((_, j) => r.lineLimited[j]);
    const retry = refused.length > 0 && ctx.lines.limitHit(batch.length - refused.length);
    pending = [...(retry ? refused : []), ...pending.slice(batch.length)];
  }
  if (contracts.length > 0 && notSubscribed === contracts.length) {
    throw new TwsRequestError(CODE.MKT_DATA_NOT_SUBSCRIBED, "no market data subscription for these contracts");
  }
  return out;
}

async function underlyingQuote(ctx: JobContext, c: TwsContract): Promise<QuoteSnapshot> {
  const [q] = await collectQuotes(ctx, [c], { genericTicks: "", complete: (a) => a.priceComplete(), minDwellMs: 400 });
  return q;
}

// ---------------- option contracts ----------------

/** One more try when TWS did not answer in time (large option contract-details lists can be slow). */
async function retryOnTimeout<T>(fn: () => Promise<T>): Promise<T> {
  try { return await fn(); } catch (e) {
    if (!(e instanceof TwsTimeoutError)) throw e;
    return fn();
  }
}

export interface ResolvedChain { underlying: QuoteSnapshot; spot: number; contracts: TwsContract[] }

/** TWS answered the option-parameter request with no listed (future) expiries: the symbol has no options. */
export class NoListedOptions extends Error {
  constructor(symbol: string) { super(`${symbol}: IBKR lists no options`); this.name = "NoListedOptions"; }
}

/** Contracts of `shape` around `spotOverride` (e.g. the session close), else around the current price. */
export async function resolveOptionContracts(ctx: JobContext, symbol: string, shape: ChainShape, spotOverride: number | null = null): Promise<ResolvedChain> {
  const und = underlyingContract(symbol);
  const [details] = await ctx.session.contractDetails(und, ctx.signal);
  if (!details?.conId) throw new Error(`${symbol}: not found at IBKR`);
  const resolved: TwsContract = { ...und, conId: details.conId };
  const quote = await underlyingQuote(ctx, resolved);
  const spot = spotOverride ?? bestPrice(quote);
  if (spot === null) throw new Error(`${symbol}: no underlying price from TWS (market data subscription?)`);
  const params = await ctx.session.optionParams({ symbol: und.symbol, secType: und.secType, conId: details.conId }, ctx.signal);
  const expiries = pickExpiries(params, und.symbol, ctx.now(), shape.expiries);
  if (!expiries.length) throw new NoListedOptions(symbol);
  const all: TwsContract[] = [];
  for (const e of expiries) {
    throwIfAborted(ctx.signal);
    const req: TwsContract = {
      symbol: und.symbol, secType: "OPT", exchange: "SMART", currency: "USD",
      lastTradeDateOrContractMonth: e.expiry, tradingClass: e.tradingClass, multiplier: e.multiplier,
    };
    all.push(...await retryOnTimeout(() => ctx.session.contractDetails(req, ctx.signal)));
  }
  return { underlying: quote, spot, contracts: selectContracts(all, spot, shape.moneyness) };
}

export interface ChainRequest { symbol: string; shape: ChainShape; final: boolean; sessionDate: string | null }
export interface ChainPushed {
  contracts: number;
  /** Share of the requested contracts that got data (and were posted). */
  coverage: number;
  optionsDelayed: boolean;
  underlyingDelayed: boolean;
}

/** A closing chain is archived only with at least this share of its requested contracts quoted. */
export const FINAL_MIN_COVERAGE = 0.95;

const OPTION_TICKS: CollectOptions = { genericTicks: "101", complete: (a) => a.optionComplete() };

/** Option quotes for `contracts`; contracts that got no usable data are asked for once more. */
async function optionQuotes(ctx: JobContext, contracts: TwsContract[]): Promise<QuoteSnapshot[]> {
  const quotes = await collectQuotes(ctx, contracts, OPTION_TICKS);
  const missing = contracts.flatMap((c, i) => (assembleContract(c, quotes[i]) === null ? [i] : []));
  if (missing.length > contracts.length * (1 - FINAL_MIN_COVERAGE)) {
    const again = await collectQuotes(ctx, missing.map((i) => contracts[i]), OPTION_TICKS);
    missing.forEach((i, k) => { quotes[i] = again[k]; });
  }
  return quotes;
}

/**
 * The underlying's regular-session close of `sessionDate` from a daily TRADES bar with useRTH (null when
 * TWS has none). After 16:00 ET the last price is an extended-hours trade, and tick 9 (close) is still the
 * previous session's close until IBKR's overnight reset, so neither is the session's close.
 */
export async function sessionClose(ctx: JobContext, symbol: string, sessionDate: string): Promise<number | null> {
  const wait = ctx.pacer.delayMs();   // chain tasks are not gated by the pacer: wait for it here (timers may fire a ms early)
  if (wait > 0) await sleep(wait + 5, ctx.signal);
  throwIfAborted(ctx.signal);
  try {
    const bars = await pacedHistorical(ctx, underlyingContract(symbol), { duration: "2 D", whatToShow: "TRADES", useRth: true });
    const bar = bars.find((b) => isoDate(b.time) === sessionDate && validBar(b));
    return bar ? bar.close : null;
  } catch (e) {
    if (ctx.signal.aborted) throw e;
    return null;   // pacing, timeout, permission: fall back to the live price (the chain matters more)
  }
}

/**
 * Fetches and posts one chain (only the contracts that got data). A closing chain is centred on the
 * session's regular close and carries it as underlying.rth_close. A closing chain below
 * FINAL_MIN_COVERAGE is never archived: it is posted as a live chain and the task fails, so it is retried.
 * When IBKR lists no options for the symbol, posts that instead (POST /api/ingest/no-options) and returns null.
 */
export async function pushChain(ctx: JobContext, req: ChainRequest): Promise<ChainPushed | null> {
  const rthClose = req.final && req.sessionDate ? await sessionClose(ctx, req.symbol, req.sessionDate) : null;
  let r: ResolvedChain;
  try {
    r = await resolveOptionContracts(ctx, req.symbol, req.shape, rthClose);
  } catch (e) {
    if (!(e instanceof NoListedOptions)) throw e;
    await ctx.ingest.noOptions({ source: "ibkr", symbol: req.symbol });
    return null;
  }
  const quotes = await optionQuotes(ctx, r.contracts);
  const fetchedAt = ctx.now().toISOString();
  const underlying = assembleUnderlying(r.underlying, fetchedAt, rthClose);
  if (!underlying) throw new Error(`${req.symbol}: no underlying price`);
  const rows = r.contracts.map((contract, i) => ({ contract, quote: quotes[i] }));
  const meta = { symbol: req.symbol, fetchedAt, final: req.final, sessionDate: req.sessionDate, underlyingDelayed: r.underlying.delayed };
  let body: IngestChain = assembleChain(meta, underlying, rows);
  if (!body.contracts.length) throw new Error(`${req.symbol}: TWS returned no option quotes (OPRA subscription?)`);
  const coverage = body.contracts.length / r.contracts.length;
  const incompleteFinal = req.final && coverage < FINAL_MIN_COVERAGE;
  if (incompleteFinal) body = assembleChain({ ...meta, final: false, sessionDate: null }, { ...underlying, rth_close: null }, rows);
  await ctx.ingest.chain(body);
  if (incompleteFinal) {
    throw new Error(`${req.symbol}: closing chain incomplete (${body.contracts.length} of ${r.contracts.length} contracts quoted); posted as a live chain, not archived — will retry`);
  }
  return { contracts: body.contracts.length, coverage, optionsDelayed: quotes.some((q) => q.delayed), underlyingDelayed: r.underlying.delayed };
}

// ---------------- universe stats and index quotes ----------------

export function assembleStat(symbol: string, q: QuoteSnapshot): UnderlyingStat | null {
  const price = bestPrice(q);
  const any = price ?? q.callVolume ?? q.callOi ?? q.impliedVol;
  if (any === null) return null;
  return {
    symbol, price, change_pct: changeFromClose(q)?.changePct ?? null, volume: q.volume,
    call_volume: q.callVolume, put_volume: q.putVolume, call_oi: q.callOi, put_oi: q.putOi, iv30: q.impliedVol, hv30: q.historicalVol,
  };
}

export interface StatsPushed {
  symbols: string[];
  delayed: boolean;
  /** Whether any symbol got option volume / OI ticks (generic 100/101 can be unavailable on delayed data). */
  optionTicks: boolean;
}

/** One batch of underlying stats (generic ticks 100/101/104/106 on the underlying). */
export async function pushUnderlyingStats(ctx: JobContext, symbols: string[], sessionDate: string): Promise<StatsPushed> {
  const quotes = await collectQuotes(ctx, symbols.map(underlyingContract), { genericTicks: "100,101,104,106", complete: (a) => a.statsComplete(), minDwellMs: 1500 });
  const stats = symbols.map((s, i) => assembleStat(s, quotes[i])).filter((s): s is UnderlyingStat => s !== null);
  const delayed = quotes.some((q) => q.delayed);
  if (stats.length) await ctx.ingest.underlyingStats({ source: "ibkr", session_date: sessionDate, ts: ctx.now().toISOString(), delayed, stats });
  return { symbols: stats.map((s) => s.symbol), delayed, optionTicks: stats.some((s) => s.call_volume !== null || s.call_oi !== null) };
}

/** VIX-family quotes: a current value and its change vs. the previous close (a symbol with only a close is skipped). */
export async function pushIndexQuotes(ctx: JobContext, symbols: string[]): Promise<number> {
  const quotes = await collectQuotes(ctx, symbols.map(underlyingContract), { genericTicks: "", complete: (a) => a.priceComplete() && a.snapshot().close !== null });
  const ts = ctx.now().toISOString();
  const rows = symbols.flatMap((symbol, i) => {
    const price = livePrice(quotes[i]);
    if (price === null) return [];
    const ch = changeFromClose(quotes[i]);
    return [{ symbol, price, change: ch?.change ?? null, change_pct: ch?.changePct ?? null, ts }];
  });
  if (rows.length) await ctx.ingest.quotes({ source: "ibkr", delayed: quotes.some((q) => q.delayed), quotes: rows });
  return rows.length;
}

// ---------------- historical ----------------

/** One paced historical request. Pacing violations back off and re-queue; "no data" yields []; permission
 * errors are rethrown as they are (the caller stops the whole symbol/kind). */
export async function pacedHistorical(ctx: JobContext, contract: TwsContract, req: HistoricalRequest): Promise<HistBar[]> {
  ctx.pacer.begin();
  try {
    const bars = await ctx.session.historical(contract, req, ctx.signal);
    ctx.pacer.success();
    return bars;
  } catch (e) {
    if (isPacingError(e)) { const ms = ctx.pacer.violation(); throw new RetryLater(`pacing violation; backing off ${Math.round(ms / 1000)} s`); }
    if (e instanceof TwsTimeoutError) throw new RetryLater(e.message);   // a slow answer is not a pacing violation
    if (isNoDataError(e)) return [];
    throw e;
  }
}

const validBar = (b: HistBar) => [b.open, b.high, b.low, b.close].every((v) => Number.isFinite(v) && v > 0);

export async function pushVolHistory(ctx: JobContext, symbol: string, from: string, kind: "iv30" | "hv30"): Promise<number> {
  const bars = await pacedHistorical(ctx, underlyingContract(symbol), {
    duration: durationFor(from, ctx.now()), whatToShow: kind === "iv30" ? "OPTION_IMPLIED_VOLATILITY" : "HISTORICAL_VOLATILITY", useRth: true,
  });
  const points = bars.flatMap((b) => {
    const date = isoDate(b.time);
    return date && date >= from && Number.isFinite(b.close) && b.close > 0 && b.close <= 20 ? [{ date, value: b.close }] : [];
  });
  if (points.length) await ctx.ingest.volHistory({ source: "ibkr", symbol, kind, bars: points });
  return points.length;
}

export async function pushIndexBars(ctx: JobContext, symbol: string): Promise<number> {
  const bars = await pacedHistorical(ctx, underlyingContract(symbol), { duration: "1 Y", whatToShow: "TRADES", useRth: true });
  const rows = bars.filter(validBar).flatMap((b) => {
    const date = isoDate(b.time);
    return date ? [{ date, open: b.open, high: b.high, low: b.low, close: b.close }] : [];
  });
  if (rows.length) await ctx.ingest.indexBars({ source: "ibkr", symbol, bars: rows });
  return rows.length;
}

/** Daily TRADES bars of one (unexpired) contract for the wanted sessions. */
export async function pushOptionBars(ctx: JobContext, symbol: string, contract: TwsContract, sessions: string[]): Promise<number> {
  const wanted = new Set(sessions);
  const earliest = [...sessions].sort()[0];
  const bars = await pacedHistorical(ctx, contract, { duration: durationFor(earliest, ctx.now()), whatToShow: "TRADES", useRth: true });
  const expiry = isoDate(contract.lastTradeDateOrContractMonth ?? "");
  if (!expiry || contract.strike === undefined || (contract.right !== "C" && contract.right !== "P")) return 0;
  const rows: OptionBar[] = bars.filter(validBar).flatMap((b) => {
    const date = isoDate(b.time);
    return date && wanted.has(date)
      ? [{ expiry, strike: contract.strike!, right: contract.right!, date, open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume }]
      : [];
  });
  if (rows.length) await ctx.ingest.optionBars({ source: "ibkr", symbol, bars: rows });
  return rows.length;
}
