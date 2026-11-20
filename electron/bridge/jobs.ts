// Bridge task bodies: talk to TWS through the TwsSession contract, assemble ingest bodies and push them.
// Market-data work is done in batches no larger than the configured line budget; every historical
// request goes through the pacer (begin → request → success | violation → RetryLater).
import { assembleChain, assembleUnderlying, isoDate, pickExpiries, selectContracts, underlyingContract, type ChainShape } from "./chain";
import type { IngestApi, IngestChain, OptionBar, UnderlyingStat } from "./ingest";
import type { HistoricalPacer } from "./pacer";
import { RetryLater } from "./scheduler";
import { bestPrice, TickAccumulator, type QuoteSnapshot } from "./ticks";
import { CODE, isCompetingSession, isNoDataError, isPacingError, NOT_SUBSCRIBED_CODES, TwsRequestError, type HistBar, type HistoricalRequest, type TwsContract, type TwsSession } from "./tws";
import { durationFor } from "./wants";

export interface JobContext {
  session: TwsSession;
  ingest: IngestApi;
  pacer: HistoricalPacer;
  /** Market-data lines one batch may use. */
  lines: number;
  now: () => Date;
  signal: AbortSignal;
  /** Max time a batch of subscriptions is held open. */
  batchTimeoutMs?: number;
}

const sleep = (ms: number, signal: AbortSignal) => new Promise<void>((resolve) => {
  const t = setTimeout(resolve, ms);
  signal.addEventListener("abort", () => { clearTimeout(t); resolve(); }, { once: true });
});

function throwIfAborted(signal: AbortSignal): void {
  if (signal.aborted) throw signal.reason instanceof Error ? signal.reason : new Error("cancelled");
}

/** America/New_York calendar date (the engine's session date convention). */
export function nyDate(d: Date): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" }).format(d);
}

// ---------------- market data ----------------

export interface CollectOptions { genericTicks: string; complete: (a: TickAccumulator) => boolean; minDwellMs?: number }

/**
 * Streams each contract (≤ ctx.lines at a time) until every subscription in the batch is complete or the
 * batch times out, then cancels them. Per-contract rejections mark that contract dead; a competing
 * session aborts the whole collection; if every contract was refused for lack of a subscription, throws.
 */
export async function collectQuotes(ctx: JobContext, contracts: TwsContract[], o: CollectOptions): Promise<QuoteSnapshot[]> {
  const out: QuoteSnapshot[] = [];
  const timeout = ctx.batchTimeoutMs ?? 4000;
  const dwell = o.minDwellMs ?? 600;
  let notSubscribed = 0;
  for (let i = 0; i < contracts.length; i += ctx.lines) {
    throwIfAborted(ctx.signal);
    const batch = contracts.slice(i, i + ctx.lines);
    const accs = batch.map(() => new TickAccumulator());
    let fatal: TwsRequestError | null = null;
    const cancels = batch.map((c, j) => ctx.session.stream(c, o.genericTicks, {
      onTick: (e) => accs[j].apply(e),
      onError: (err) => {
        accs[j].dead = true;
        if (NOT_SUBSCRIBED_CODES.has(err.code)) notSubscribed += 1;
        if (isCompetingSession(err)) fatal = err;
      },
    }));
    const started = Date.now();
    try {
      while (!ctx.signal.aborted && !fatal) {
        const elapsed = Date.now() - started;
        if (elapsed >= timeout || (elapsed >= dwell && accs.every(o.complete))) break;
        await sleep(100, ctx.signal);
      }
    } finally {
      for (const cancel of cancels) cancel();
    }
    if (fatal) throw fatal;
    throwIfAborted(ctx.signal);
    out.push(...accs.map((a) => a.snapshot()));
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

export interface ResolvedChain { underlying: QuoteSnapshot; spot: number; contracts: TwsContract[] }

export async function resolveOptionContracts(ctx: JobContext, symbol: string, shape: ChainShape): Promise<ResolvedChain> {
  const und = underlyingContract(symbol);
  const [details] = await ctx.session.contractDetails(und, ctx.signal);
  if (!details?.conId) throw new Error(`${symbol}: not found at IBKR`);
  const resolved: TwsContract = { ...und, conId: details.conId };
  const quote = await underlyingQuote(ctx, resolved);
  const spot = bestPrice(quote);
  if (spot === null) throw new Error(`${symbol}: no underlying price from TWS (market data subscription?)`);
  const params = await ctx.session.optionParams({ symbol: und.symbol, secType: und.secType, conId: details.conId }, ctx.signal);
  const expiries = pickExpiries(params, und.symbol, ctx.now(), shape.expiries);
  if (!expiries.length) throw new Error(`${symbol}: no listed option expiries`);
  const all: TwsContract[] = [];
  for (const e of expiries) {
    throwIfAborted(ctx.signal);
    all.push(...await ctx.session.contractDetails({
      symbol: und.symbol, secType: "OPT", exchange: "SMART", currency: "USD",
      lastTradeDateOrContractMonth: e.expiry, tradingClass: e.tradingClass, multiplier: e.multiplier,
    }, ctx.signal));
  }
  return { underlying: quote, spot, contracts: selectContracts(all, spot, shape.moneyness) };
}

export interface ChainRequest { symbol: string; shape: ChainShape; final: boolean; sessionDate: string | null }
export interface ChainPushed { contracts: number; optionsDelayed: boolean; underlyingDelayed: boolean }

/** Fetches and posts one chain. */
export async function pushChain(ctx: JobContext, req: ChainRequest): Promise<ChainPushed> {
  const r = await resolveOptionContracts(ctx, req.symbol, req.shape);
  const quotes = await collectQuotes(ctx, r.contracts, { genericTicks: "101", complete: (a) => a.optionComplete() });
  const fetchedAt = ctx.now().toISOString();
  const underlying = assembleUnderlying(r.underlying, fetchedAt);
  if (!underlying) throw new Error(`${req.symbol}: no underlying price`);
  const body: IngestChain = assembleChain({ symbol: req.symbol, fetchedAt, final: req.final, sessionDate: req.sessionDate, underlyingDelayed: r.underlying.delayed }, underlying,
    r.contracts.map((contract, i) => ({ contract, quote: quotes[i] })));
  if (!body.contracts.length) throw new Error(`${req.symbol}: TWS returned no option quotes (OPRA subscription?)`);
  await ctx.ingest.chain(body);
  return { contracts: body.contracts.length, optionsDelayed: quotes.some((q) => q.delayed), underlyingDelayed: r.underlying.delayed };
}

// ---------------- universe stats and index quotes ----------------

export function assembleStat(symbol: string, q: QuoteSnapshot): UnderlyingStat | null {
  const price = bestPrice(q);
  const any = price ?? q.callVolume ?? q.callOi ?? q.impliedVol;
  if (any === null) return null;
  return {
    symbol, price, change_pct: price !== null && q.close ? price / q.close - 1 : null, volume: q.volume,
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

export async function pushIndexQuotes(ctx: JobContext, symbols: string[]): Promise<number> {
  const quotes = await collectQuotes(ctx, symbols.map(underlyingContract), { genericTicks: "", complete: (a) => a.priceComplete() && a.snapshot().close !== null });
  const ts = ctx.now().toISOString();
  const rows = symbols.flatMap((symbol, i) => {
    const q = quotes[i];
    const price = bestPrice(q);
    if (price === null) return [];
    const change = q.close ? price - q.close : null;
    return [{ symbol, price, change, change_pct: change !== null && q.close ? change / q.close : null, ts }];
  });
  if (rows.length) await ctx.ingest.quotes({ source: "ibkr", delayed: quotes.some((q) => q.delayed), quotes: rows });
  return rows.length;
}

// ---------------- historical ----------------

/** One paced historical request. Pacing violations back off and re-queue; "no data" yields []. */
export async function pacedHistorical(ctx: JobContext, contract: TwsContract, req: HistoricalRequest): Promise<HistBar[]> {
  ctx.pacer.begin();
  try {
    const bars = await ctx.session.historical(contract, req, ctx.signal);
    ctx.pacer.success();
    return bars;
  } catch (e) {
    if (isPacingError(e)) { const ms = ctx.pacer.violation(); throw new RetryLater(`pacing violation; backing off ${Math.round(ms / 1000)} s`); }
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
