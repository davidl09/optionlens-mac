// Bridge task bodies: talk to TWS through the TwsSession contract, assemble ingest bodies and push them.
// Market-data work is done in batches no larger than the line budget; every historical
// request goes through the pacer (begin → request → success | violation → RetryLater).
import { assembleChain, assembleUnderlying, expiryCoverage, isoDate, isQuoted, pickExpiries, selectContracts, underlyingContract, type ChainShape, type ExpiryCoverage, type ExpiryPick } from "./chain";
import type { IntradayInterval } from "../shared/api";
import type { IngestApi, IngestChain, IngestQuote, OptionBar, ReferencePrice, UnderlyingStat } from "./ingest";
import { BAR_SIZE, barStart, durationOf, endDateTime, type Chunk } from "./intraday";
import type { HistoricalPacer } from "./pacer";
import { RetryLater } from "./scheduler";
import type { LineBudget } from "./line-budget";
import { bestPrice, changeFromClose, emptySnapshot, livePrice, TickAccumulator, type QuoteSnapshot } from "./ticks";
import { CODE, isCompetingSession, isNoDataError, isPacingError, NOT_SUBSCRIBED_CODES, TwsRequestError, TwsTimeoutError, type HistBar, type HistoricalRequest, type MarketDataKind, type OptionParams, type SecType, type TwsContract, type TwsSession } from "./tws";
import type { ContractCache, SavedContracts } from "./contract-cache";
import type { ContractServer } from "./farms";
import type { OptionDataType, OptionsFeed } from "./options-feed";
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
  /** Saved contract definitions (none: always asks TWS, saves nothing). */
  contracts?: ContractCache;
  /** Whether IBKR's contract (sec-def) server may be asked now (none: always). */
  contractServer?: ContractServer;
  /** What the account's options market data allows (none: option quotes always requested, session default type). */
  optionsFeed?: OptionsFeed;
}

const sleep = (ms: number, signal: AbortSignal) => new Promise<void>((resolve) => {
  const t = setTimeout(resolve, ms);
  signal.addEventListener("abort", () => { clearTimeout(t); resolve(); }, { once: true });
});

function throwIfAborted(signal: AbortSignal): void {
  if (signal.aborted) throw signal.reason instanceof Error ? signal.reason : new Error("cancelled");
}

// ---------------- market data ----------------

export interface CollectOptions {
  genericTicks: string; complete: (a: TickAccumulator) => boolean; minDwellMs?: number;
  /** IBKR market data type of the requests (session default when omitted). */
  marketDataType?: MarketDataKind;
  /** Counts the TWS error codes the subscriptions answered with (code -> count), when given. */
  errors?: Map<number, number>;
}

/** "10089 ×40, 354 ×2": TWS error codes by count, most frequent first. */
export function errorCodesText(errors: Map<number, number>): string {
  return [...errors].sort((a, b) => b[1] - a[1] || a[0] - b[0]).map(([code, n]) => `${code} ×${n}`).join(", ");
}

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
        o.errors?.set(err.code, (o.errors.get(err.code) ?? 0) + 1);
        if (err.code === CODE.MAX_TICKERS) lineLimited[j] = true;
        else if (NOT_SUBSCRIBED_CODES.has(err.code)) notSubscribed += 1;
        if (isCompetingSession(err)) fatal = err;
      },
    }, o.marketDataType);
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
    const codes = o.errors?.size ? ` (IBKR errors: ${errorCodesText(o.errors)})` : "";
    throw new TwsRequestError(CODE.MKT_DATA_NOT_SUBSCRIBED, `no market data subscription for these contracts${codes}`);
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

/** The second, longer wait for reqSecDefOptParams (the adapter's default is 60 s). */
export const OPTION_PARAMS_RETRY_MS = 120_000;

/** TWS answered no contract-definition (sec-def) request in time: IBKR's contract server is probably unavailable. */
class DefinitionsUnanswered extends Error {
  constructor(what: string) { super(`IB Gateway did not answer ${what}`); this.name = "DefinitionsUnanswered"; }
}

/** What an on-demand refresh reports when it needs contract definitions IBKR cannot send now and none are saved. */
export const CONTRACTS_UNAVAILABLE = "IBKR's contract server is unavailable; will fetch automatically when it's back";

/** The chain needs fresh contract definitions, IBKR's contract server cannot send them now, and none are saved. */
export class ContractsUnavailable extends Error {
  constructor(readonly symbol: string) { super(CONTRACTS_UNAVAILABLE); this.name = "ContractsUnavailable"; }
}

/** What a refresh reports when option quotes cannot be requested now (no OPRA subscription, outside market hours). */
export const OPTION_QUOTES_CLOSED = "Option quotes update during market hours (no real-time options subscription); showing the last stored chain";

/** Option market data would be refused now (no real-time options subscription, outside regular hours): skipped. */
export class OptionQuotesClosed extends Error {
  constructor(readonly symbol: string) { super(OPTION_QUOTES_CLOSED); this.name = "OptionQuotesClosed"; }
}

/** The underlying's conId from contract details (a sec-def request). */
async function underlyingConId(ctx: JobContext, und: TwsContract): Promise<number> {
  let details: TwsContract[];
  try {
    details = await ctx.session.contractDetails(und, ctx.signal);
  } catch (e) {
    if (e instanceof TwsTimeoutError) throw new DefinitionsUnanswered(`${und.symbol}'s contract details`);
    throw e;
  }
  const conId = details[0]?.conId;
  if (!conId) throw new Error(`${und.symbol}: not found at IBKR`);
  return conId;
}

/**
 * The underlying's option parameters: today's saved answer, else TWS's, asked once more with a longer wait when
 * TWS does not answer (it answers late while busy). `fresh`: asked TWS just now.
 */
async function optionParams(ctx: JobContext, u: { symbol: string; secType: SecType; conId: number }): Promise<{ params: OptionParams[]; fresh: boolean }> {
  const cached = ctx.contracts?.todaysParams(u.symbol, u.conId, ctx.now());
  if (cached) return { params: cached, fresh: false };
  try {
    return { params: await ctx.session.optionParams(u, ctx.signal), fresh: true };
  } catch (e) {
    if (!(e instanceof TwsTimeoutError)) throw e;
    try {
      return { params: await ctx.session.optionParams(u, ctx.signal, OPTION_PARAMS_RETRY_MS), fresh: true };
    } catch (e2) {
      if (!(e2 instanceof TwsTimeoutError)) throw e2;
      throw new DefinitionsUnanswered(`${u.symbol}'s option expiries (asked twice, waited ${OPTION_PARAMS_RETRY_MS / 1000} s)`);
    }
  }
}

export interface ResolvedChain {
  underlying: QuoteSnapshot; spot: number; contracts: TwsContract[];
  /** The engine's price the strike band is centred on when TWS sent no underlying price, else null. */
  reference: ReferencePrice | null;
  /** Every picked expiry (YYYYMMDD), in order, including those without contracts (none listed in the band, or
   * TWS did not deliver the contract list: two timeouts, reported per expiry rather than failing the chain). */
  expiries: string[];
  /** When the saved contract list used was fetched (oldest expiry used), null when IBKR sent every list now. */
  listedAt: string | null;
}

/** TWS answered the option-parameter request with no listed (future) expiries: the symbol has no options. */
export class NoListedOptions extends Error {
  constructor(symbol: string) { super(`${symbol}: IBKR lists no options`); this.name = "NoListedOptions"; }
}

const oldest = (times: string[]): string | null => (times.length ? [...times].sort()[0] : null);

/**
 * The picked expiries' contracts as IBKR lists them now (option parameters, then contract details per expiry), saved
 * for later. An expiry whose list TWS does not deliver (two timeouts) uses its saved list if there is one, else stays
 * without contracts; when no expiry was delivered the definitions went unanswered.
 */
async function listedContracts(ctx: JobContext, und: TwsContract, conId: number, shape: ChainShape, saved: () => SavedContracts | null): Promise<{ contracts: TwsContract[]; expiries: string[]; listedAt: string | null }> {
  const asked = await optionParams(ctx, { symbol: und.symbol, secType: und.secType, conId });
  const picks = pickExpiries(asked.params, und.symbol, ctx.now(), shape.expiries);
  if (!picks.length) {
    if (asked.fresh) ctx.contracts?.forget(und.symbol);
    throw new NoListedOptions(und.symbol);
  }
  const all: TwsContract[] = [];
  const delivered: { pick: ExpiryPick; contracts: TwsContract[] }[] = [];
  const savedAt: string[] = [];
  for (const e of picks) {
    throwIfAborted(ctx.signal);
    const req: TwsContract = {
      symbol: und.symbol, secType: "OPT", exchange: "SMART", currency: "USD",
      lastTradeDateOrContractMonth: e.expiry, tradingClass: e.tradingClass, multiplier: e.multiplier,
    };
    try {
      const list = await retryOnTimeout(() => ctx.session.contractDetails(req, ctx.signal));
      delivered.push({ pick: e, contracts: list });
      all.push(...list);
    } catch (err) {
      if (!(err instanceof TwsTimeoutError)) throw err;
      const s = saved()?.expiries.find((x) => x.pick.expiry === e.expiry);
      if (s) { all.push(...s.contracts); savedAt.push(s.fetchedAt); }
    }
  }
  if (!delivered.length && !savedAt.length) throw new DefinitionsUnanswered(`${und.symbol}'s option contract details`);
  ctx.contracts?.remember(und.symbol, und.secType, conId, asked.fresh ? asked.params : null, delivered, ctx.now());
  return { contracts: all, expiries: picks.map((e) => e.expiry), listedAt: oldest(savedAt) };
}

/**
 * Contracts of `shape` around `spotOverride` (e.g. the session close), else around the current price. Asks IBKR's
 * contract (sec-def) server while it is available; when it is not, or does not answer, the saved contract list
 * (≤ 7 days old, unexpired expiries) is used — quotes still come from the market-data server, by conId. Without a
 * saved list: ContractsUnavailable.
 */
export async function resolveOptionContracts(ctx: JobContext, symbol: string, shape: ChainShape, spotOverride: number | null = null): Promise<ResolvedChain> {
  const und = underlyingContract(symbol);
  const server = ctx.contractServer;
  let savedList: SavedContracts | null | undefined;
  const saved = () => (savedList === undefined ? (savedList = ctx.contracts?.saved(und.symbol, ctx.now()) ?? null) : savedList);
  const unanswered = (e: unknown) => { if (!(e instanceof DefinitionsUnanswered)) throw e; server?.unanswered(); live = false; };
  let live = server?.available() ?? true;

  let conId: number | undefined;
  if (live) { try { conId = await underlyingConId(ctx, und); } catch (e) { unanswered(e); } }
  conId ??= saved()?.conId;
  if (conId === undefined) throw new ContractsUnavailable(symbol);
  const quote = await underlyingQuote(ctx, { ...und, conId });
  const ticked = spotOverride ?? bestPrice(quote);
  const reference = ticked === null ? await referencePrice(ctx, symbol) : null;
  const spot = ticked ?? reference?.price ?? null;
  if (spot === null) throw new Error(`${symbol}: no underlying price from TWS (market data subscription?) and none stored in the engine`);

  if (live && (server?.available() ?? true)) {
    try {
      const r = await listedContracts(ctx, und, conId, shape, saved);
      server?.answered();
      return { underlying: quote, spot, reference, contracts: selectContracts(r.contracts, spot, shape.moneyness), expiries: r.expiries, listedAt: r.listedAt };
    } catch (e) { unanswered(e); }
  }
  const s = saved();
  if (!s) throw new ContractsUnavailable(symbol);
  const picks = s.expiries.slice(0, shape.expiries);
  return {
    underlying: quote, spot, reference, contracts: selectContracts(picks.flatMap((p) => p.contracts), spot, shape.moneyness),
    expiries: picks.map((p) => p.pick.expiry), listedAt: oldest(picks.map((p) => p.fetchedAt)),
  };
}

/**
 * The engine's current price of `symbol` (ReferencePrice), for a chain whose underlying TWS sent no price for (e.g. a
 * CBOE index without that market-data subscription); null when the engine has none or cannot be asked.
 */
async function referencePrice(ctx: JobContext, symbol: string): Promise<ReferencePrice | null> {
  try {
    const r = (await ctx.ingest.referencePrice(symbol)) as Partial<ReferencePrice> | null;
    return r && typeof r.price === "number" && r.price > 0 && typeof r.ts === "string"
      ? { symbol, price: r.price, ts: r.ts, source: r.source ?? null } : null;
  } catch {
    return null;
  }
}

export interface ChainRequest { symbol: string; shape: ChainShape; final: boolean; sessionDate: string | null }
export interface ChainPushed {
  /** Contracts posted (all requested ones; those without data have null fields). */
  contracts: number;
  /** Share of the requested contracts TWS quoted. */
  coverage: number;
  /** Per picked expiry: contracts listed in the strike band and how many were quoted. */
  expiries: ExpiryCoverage[];
  optionsDelayed: boolean;
  underlyingDelayed: boolean;
  /** When the saved contract list the chain was built from was fetched (IBKR's contract server unavailable), else null. */
  listedAt: string | null;
  /** The engine's price the chain was centred on (TWS sent no underlying price), else null. */
  reference: ReferencePrice | null;
}

/** A closing chain is archived only with at least this share of its requested contracts quoted. */
export const FINAL_MIN_COVERAGE = 0.95;

const OPTION_TICKS: CollectOptions = { genericTicks: "101", complete: (a) => a.optionComplete() };

/** Option quotes for `contracts`; contracts that got no usable data are asked for once more. */
async function collectOptionQuotes(ctx: JobContext, contracts: TwsContract[], marketDataType: OptionDataType, errors?: Map<number, number>): Promise<QuoteSnapshot[]> {
  const o: CollectOptions = { ...OPTION_TICKS, marketDataType, errors };
  const quotes = await collectQuotes(ctx, contracts, o);
  const missing = contracts.flatMap((_, i) => (isQuoted(quotes[i]) ? [] : [i]));
  if (missing.length > contracts.length * (1 - FINAL_MIN_COVERAGE)) {
    const again = await collectQuotes(ctx, missing.map((i) => contracts[i]), o);
    missing.forEach((i, k) => { quotes[i] = again[k]; });
  }
  return quotes;
}

const isSubscriptionRefusal = (e: unknown) => e instanceof TwsRequestError && NOT_SUBSCRIBED_CODES.has(e.code);

/**
 * Option quotes with the market data type the account's options feed allows now. A refusal for lack of a subscription
 * switches the feed to delayed-only: retried at once as delayed data where that applies (regular hours), else the
 * quotes are skipped (OptionQuotesClosed).
 */
async function optionQuotes(ctx: JobContext, symbol: string, contracts: TwsContract[], errors?: Map<number, number>): Promise<QuoteSnapshot[]> {
  const feed = ctx.optionsFeed;
  let type = feed?.dataType(ctx.now());
  let quotes: QuoteSnapshot[];
  try {
    quotes = await collectOptionQuotes(ctx, contracts, type, errors);
  } catch (e) {
    if (!feed || !isSubscriptionRefusal(e)) throw e;
    feed.refused(ctx.now(), type);
    if (!feed.canQuote(ctx.now())) throw new OptionQuotesClosed(symbol);
    const next = feed.dataType(ctx.now());
    if (next === type) throw e;
    type = next;
    quotes = await collectOptionQuotes(ctx, contracts, type, errors);
  }
  if (quotes.some(isQuoted)) feed?.quoted(ctx.now(), quotes.some((q) => q.delayed), type);
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
 * Fetches and posts one chain: every contract IBKR lists in the strike band of the picked expiries, those TWS
 * sent nothing for with null fields. A closing chain is centred on the session's regular close and carries it
 * as underlying.rth_close. A closing chain below FINAL_MIN_COVERAGE quoted is never archived: it is posted as a
 * live chain and the task fails, so it is retried. When IBKR lists no options for the symbol, posts that
 * instead (POST /api/ingest/no-options) and returns null.
 */
export async function pushChain(ctx: JobContext, req: ChainRequest): Promise<ChainPushed | null> {
  if (ctx.optionsFeed && !ctx.optionsFeed.canQuote(ctx.now())) throw new OptionQuotesClosed(req.symbol);
  const rthClose = req.final && req.sessionDate ? await sessionClose(ctx, req.symbol, req.sessionDate) : null;
  let r: ResolvedChain;
  try {
    r = await resolveOptionContracts(ctx, req.symbol, req.shape, rthClose);
  } catch (e) {
    if (!(e instanceof NoListedOptions)) throw e;
    await ctx.ingest.noOptions({ source: "ibkr", symbol: req.symbol });
    return null;
  }
  const errors = new Map<number, number>();
  const quotes = await optionQuotes(ctx, req.symbol, r.contracts, errors);
  const fetchedAt = ctx.now().toISOString();
  const underlying = assembleUnderlying(r.underlying, fetchedAt, rthClose, r.reference);
  if (!underlying) throw new Error(`${req.symbol}: no underlying price`);
  const rows = r.contracts.map((contract, i) => ({ contract, quote: quotes[i] }));
  const quoted = quotes.filter(isQuoted).length;
  if (!quoted) {
    const codes = errors.size ? `; IBKR errors: ${errorCodesText(errors)}` : "";
    throw new Error(`${req.symbol}: TWS returned no option quotes (OPRA subscription?)${codes}`);
  }
  // A reference price is not current: the chain is marked delayed.
  const underlyingDelayed = r.underlying.delayed || underlying.reference === true;
  const meta = { symbol: req.symbol, fetchedAt, final: req.final, sessionDate: req.sessionDate, underlyingDelayed, listedAt: r.listedAt };
  const coverage = quoted / r.contracts.length;
  const incompleteFinal = req.final && coverage < FINAL_MIN_COVERAGE;
  const body: IngestChain = incompleteFinal
    ? assembleChain({ ...meta, final: false, sessionDate: null }, { ...underlying, rth_close: null }, rows)
    : assembleChain(meta, underlying, rows);
  await ctx.ingest.chain(body);
  if (incompleteFinal) {
    throw new Error(`${req.symbol}: closing chain incomplete (${quoted} of ${r.contracts.length} contracts quoted); posted as a live chain, not archived — will retry`);
  }
  return {
    contracts: body.contracts.length, coverage, expiries: expiryCoverage(r.expiries, rows), listedAt: r.listedAt,
    optionsDelayed: quotes.some((q) => q.delayed), underlyingDelayed: r.underlying.delayed, reference: underlying.reference ? r.reference : null,
  };
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

/** The stock quote of a stats snapshot (current price required): posted with the stats, so symbol headers and
 * tables have the session's price, OHLC and volume without a chain. */
export function assembleQuote(symbol: string, q: QuoteSnapshot, ts: string): IngestQuote | null {
  const price = livePrice(q);
  if (price === null) return null;
  const ch = changeFromClose(q);
  return { symbol, price, change: ch?.change ?? null, change_pct: ch?.changePct ?? null, ts,
    bid: q.bid, ask: q.ask, open: q.open, high: q.high, low: q.low, prev_close: q.close, volume: q.volume };
}

/** One batch of underlying stats (generic ticks 100/101/104/106 on the underlying), and the same streams' quotes. */
export async function pushUnderlyingStats(ctx: JobContext, symbols: string[], sessionDate: string): Promise<StatsPushed> {
  const quotes = await collectQuotes(ctx, symbols.map(underlyingContract), { genericTicks: "100,101,104,106", complete: (a) => a.statsComplete(), minDwellMs: 1500 });
  const stats = symbols.map((s, i) => assembleStat(s, quotes[i])).filter((s): s is UnderlyingStat => s !== null);
  const delayed = quotes.some((q) => q.delayed);
  const ts = ctx.now().toISOString();
  if (stats.length) await ctx.ingest.underlyingStats({ source: "ibkr", session_date: sessionDate, ts, delayed, stats });
  const rows = symbols.map((s, i) => assembleQuote(s, quotes[i], ts)).filter((q): q is IngestQuote => q !== null);
  if (rows.length) await ctx.ingest.quotes({ source: "ibkr", delayed, quotes: rows });
  return { symbols: stats.map((s) => s.symbol), delayed, optionTicks: stats.some((s) => s.call_volume !== null || s.call_oi !== null) };
}

/** Live snapshots of the symbols on screen (INTEGRATION.md §9: only while Questrade is unavailable): their quotes,
 * posted like the stats pass's so the engine passes them on to the windows. */
export async function pushLiveQuotes(ctx: JobContext, symbols: string[]): Promise<number> {
  const quotes = await collectQuotes(ctx, symbols.map(underlyingContract), { genericTicks: "", complete: (a) => a.priceComplete(), minDwellMs: 400 });
  const ts = ctx.now().toISOString();
  const rows = symbols.map((s, i) => assembleQuote(s, quotes[i], ts)).filter((q): q is IngestQuote => q !== null);
  if (rows.length) await ctx.ingest.quotes({ source: "ibkr", delayed: quotes.some((q) => q.delayed), quotes: rows });
  return rows.length;
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

/**
 * Daily bars of an index (IND: SPX, NDX, RUT, XSP, the VIX family) from `from` on (up to 10 years). TRADES is
 * the index's own value series at IBKR (an index has no bid/ask, so MIDPOINT/BID_ASK do not apply; volume is
 * meaningless and not sent).
 */
export async function pushIndexBars(ctx: JobContext, symbol: string, from: string): Promise<number> {
  const bars = await pacedHistorical(ctx, underlyingContract(symbol), { duration: durationFor(from, ctx.now()), whatToShow: "TRADES", useRth: true });
  const rows = bars.filter(validBar).flatMap((b) => {
    const date = isoDate(b.time);
    return date && date >= from ? [{ date, open: b.open, high: b.high, low: b.low, close: b.close }] : [];
  });
  if (rows.length) await ctx.ingest.indexBars({ source: "ibkr", symbol, bars: rows });
  return rows.length;
}

/**
 * The closes behind a symbol's dividends (INTEGRATION.md §6 "Dividends"): daily ADJUSTED_LAST (dividend- and
 * split-adjusted) and TRADES (split-adjusted) bars from `from` on (up to 10 years, regular hours), one request pair per
 * symbol through the pacer like any daily bars; the engine derives the dividends from the step in their ratio.
 */
export async function pushDividendHistory(ctx: JobContext, symbol: string, from: string): Promise<number> {
  const req = { duration: durationFor(from, ctx.now()), useRth: true } as const;
  const closes = (bars: HistBar[]) => bars.flatMap((b) => {
    const date = isoDate(b.time);
    return date && date >= from && Number.isFinite(b.close) && b.close > 0 ? [{ date, close: b.close }] : [];
  });
  const adjusted = closes(await pacedHistorical(ctx, underlyingContract(symbol), { ...req, whatToShow: "ADJUSTED_LAST" }));
  const trades = closes(await pacedHistorical(ctx, underlyingContract(symbol), { ...req, whatToShow: "TRADES" }));
  await ctx.ingest.dividendHistory({ source: "ibkr", symbol, adjusted, trades });
  return Math.min(adjusted.length, trades.length);
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

/**
 * Intraday TRADES bars of one request window (intraday.chunks), every hour IBKR has (useRTH 0: one stored series
 * serves the regular-hours and the extended-hours view), pushed with the window as the span they cover.
 */
export async function pushIntradayBars(ctx: JobContext, symbol: string, interval: IntradayInterval, c: Chunk): Promise<number> {
  const bars = await pacedHistorical(ctx, underlyingContract(symbol), {
    duration: durationOf(c), whatToShow: "TRADES", useRth: false, barSize: BAR_SIZE[interval], endDateTime: endDateTime(c.end),
  });
  const rows = bars.filter(validBar).flatMap((b) => {
    const t = barStart(b);
    return t !== null && t >= c.start && t <= c.end
      ? [{ ts: new Date(t).toISOString(), open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume }]
      : [];
  });
  await ctx.ingest.intradayBars({ source: "ibkr", symbol, interval, delayed: null,
    spans: [{ start: new Date(c.start).toISOString(), end: new Date(c.end).toISOString() }], bars: rows });
  return rows.length;
}
