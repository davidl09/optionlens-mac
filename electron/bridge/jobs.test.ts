import { describe, expect, it, vi } from "vitest";
import { emptyMarket, FakeSession, contractId, memoryContractStore, type FakeMarket } from "./fake-tws.test-helper";
import type { IngestApi, IngestChain } from "./ingest";
import { ContractCache } from "./contract-cache";
import { ContractServer, SECDEF_PROBE_MS, type FarmState } from "./farms";
import { OptionsFeed } from "./options-feed";
import { collectQuotes, ContractsUnavailable, OPTION_PARAMS_RETRY_MS, OptionQuotesClosed, pacedHistorical, pushChain, pushDividendHistory, pushIndexBars, pushIndexQuotes, pushLiveQuotes, pushUnderlyingStats, pushVolHistory, type JobContext } from "./jobs";
import { LineBudget } from "./line-budget";
import { HistoricalPacer } from "./pacer";
import { RetryLater } from "./scheduler";
import { TICK } from "./ticks";
import { CODE, TwsRequestError, TwsTimeoutError, type TwsContract } from "./tws";

function fakeIngest(): IngestApi & { calls: Record<string, unknown[]> } {
  const calls: Record<string, unknown[]> = {};
  const rec = (name: string) => async (b: unknown) => { (calls[name] ??= []).push(b); return { stored: 1 }; };
  return { calls, wants: async () => ({}), referencePrice: async () => { throw new Error("404 no price stored"); }, liveWants: async () => ({ symbols: [], interval_s: 5 }), chain: rec("chain"), noOptions: rec("noOptions"), volHistory: rec("volHistory"), indexBars: rec("indexBars"), dividendHistory: rec("dividendHistory"), quotes: rec("quotes"),
    underlyingStats: rec("underlyingStats"), optionBars: rec("optionBars"), intradayBars: rec("intradayBars"), ackWants: rec("ackWants"), heartbeat: rec("heartbeat"), refreshResult: rec("refreshResult"), positions: rec("positions") };
}

function ctxFor(session: FakeSession, o: Partial<JobContext> = {}): JobContext & { ingest: ReturnType<typeof fakeIngest> } {
  return { session, ingest: fakeIngest(), pacer: new HistoricalPacer({ minSpacingMs: 0 }), lines: new LineBudget(() => 50), now: () => new Date("2026-11-20T15:00:00Z"),
    signal: new AbortController().signal, checkpoint: async () => undefined, batchTimeoutMs: 300, ...o } as JobContext & { ingest: ReturnType<typeof fakeIngest> };
}

/** AAPL at 100 with weekly options 50..150 step 5 for two expiries; every option fully quoted. */
function aaplMarket(): FakeMarket {
  const m = emptyMarket();
  m.optionParams = [{ exchange: "SMART", tradingClass: "AAPL", multiplier: "100", expirations: ["20261127", "20261204", "20261211"], strikes: [] }];
  m.details = (c) => {
    if (c.secType === "STK") return [{ ...c, conId: 265598 }];
    const out: TwsContract[] = [];
    for (let k = 50; k <= 150; k += 5) for (const right of ["C", "P"] as const) out.push({ ...c, strike: k, right, conId: k * 10 });
    return out;
  };
  const optTicks = (k: number) => [
    { kind: "price" as const, field: TICK.BID, value: Math.max(0, (100 - k) / 2) }, { kind: "price" as const, field: TICK.ASK, value: Math.max(0.05, (100 - k) / 2 + 0.1) },
    { kind: "option" as const, field: TICK.MODEL_OPTION, iv: 0.3, delta: 0.5, gamma: 0.01, vega: 0.1, theta: -0.02 },
  ];
  m.ticks = (id) => id === "AAPL:STK"
    ? [{ kind: "price", field: TICK.LAST, value: 100 }, { kind: "price", field: TICK.CLOSE, value: 98 }]
    : id.split(":").length === 4 ? optTicks(Number(id.split(":")[2])) : undefined;
  return m;
}

describe("collectQuotes", () => {
  it("never holds more subscriptions than the line budget and cancels every stream", async () => {
    const s = new FakeSession(aaplMarket());
    const contracts = Array.from({ length: 23 }, (_, i): TwsContract => ({ symbol: "AAPL", secType: "OPT", exchange: "SMART", currency: "USD", lastTradeDateOrContractMonth: "20261127", strike: 80 + i, right: "C" }));
    const q = await collectQuotes(ctxFor(s, { lines: new LineBudget(() => 10) }), contracts, { genericTicks: "101", complete: (a) => a.optionComplete(), minDwellMs: 0 });
    expect(q).toHaveLength(23);
    expect(s.maxConcurrentStreams).toBeLessThanOrEqual(10);
    expect(s.streams.every((x) => x.cancelled)).toBe(true);
  });

  it("times each contract from when its request is actually sent, so paced later batches lose nothing", async () => {
    // The API client sends ≤ msgRate messages/s (requests and cancels): a 20-contract batch takes 100 ms to
    // send at 200/s, longer than the 60 ms per-contract timeout, and each batch's cancels delay the next one.
    const s = new FakeSession(aaplMarket());
    s.msgRate = 200;
    const contracts = Array.from({ length: 60 }, (_, i): TwsContract => ({ symbol: "AAPL", secType: "OPT", exchange: "SMART", currency: "USD", lastTradeDateOrContractMonth: "20261127", strike: 50 + i, right: "P" }));
    const q = await collectQuotes(ctxFor(s, { lines: new LineBudget(() => 20), batchTimeoutMs: 60 }), contracts, { genericTicks: "101", complete: (a) => a.optionComplete(), minDwellMs: 0 });
    expect(q.filter((x) => x.bid !== null)).toHaveLength(60);
    expect(s.maxConcurrentStreams).toBeLessThanOrEqual(20);
  });

  it("shrinks the batch on the line limit (error 101) and retries the refused contracts", async () => {
    const s = new FakeSession(aaplMarket());
    s.lineLimit = 15;
    const lines = new LineBudget(() => 20);
    const contracts = Array.from({ length: 40 }, (_, i): TwsContract => ({ symbol: "AAPL", secType: "OPT", exchange: "SMART", currency: "USD", lastTradeDateOrContractMonth: "20261127", strike: 60 + i, right: "C" }));
    const q = await collectQuotes(ctxFor(s, { lines }), contracts, { genericTicks: "101", complete: (a) => a.optionComplete(), minDwellMs: 0 });
    expect(q.filter((x) => x.ask !== null)).toHaveLength(40);
    expect(lines.reduced).toBe(13);
  });

  it("aborts on a competing session", async () => {
    const m = emptyMarket();
    m.streamErrors = () => new TwsRequestError(CODE.COMPETING_SESSION, "No market data during competing live session");
    const s = new FakeSession(m);
    await expect(collectQuotes(ctxFor(s), [{ symbol: "AAPL", secType: "STK", exchange: "SMART", currency: "USD" }], { genericTicks: "", complete: () => true }))
      .rejects.toThrow(/10197/);
    expect(s.streams[0].cancelled).toBe(true);
  });

  it("reports a missing subscription when every contract is refused", async () => {
    const m = emptyMarket();
    m.streamErrors = () => new TwsRequestError(CODE.MKT_DATA_NOT_SUBSCRIBED, "Requested market data is not subscribed");
    await expect(collectQuotes(ctxFor(new FakeSession(m)), [{ symbol: "AAPL", secType: "STK", exchange: "SMART", currency: "USD" }], { genericTicks: "", complete: () => true }))
      .rejects.toThrow(/subscription/);
  });
});

describe("pushChain", () => {
  it("fetches expiries/strikes per the shape and posts the contract JSON", async () => {
    const s = new FakeSession(aaplMarket());
    const ctx = ctxFor(s);
    const n = (await pushChain(ctx, { symbol: "AAPL", shape: { expiries: 2, moneyness: 0.25 }, final: true, sessionDate: "2026-11-20" }))?.contracts;
    const body = ctx.ingest.calls.chain[0] as { contracts: { expiry: string; strike: number }[]; final: boolean; session_date: string; underlying: { price: number; prev_close: number } };
    expect(n).toBe(body.contracts.length);
    expect(body.final).toBe(true);
    expect(body.session_date).toBe("2026-11-20");
    expect(body.underlying).toMatchObject({ price: 100, prev_close: 98 });
    expect(new Set(body.contracts.map((c) => c.expiry))).toEqual(new Set(["2026-11-27", "2026-12-04"]));
    expect(Math.min(...body.contracts.map((c) => c.strike))).toBe(75);
    expect(Math.max(...body.contracts.map((c) => c.strike))).toBe(125);
    expect(body.contracts).toHaveLength(11 * 2 * 2);
  });

  it("centres a closing chain on the session's regular close (not the after-hours last) and sends it as rth_close", async () => {
    const m = aaplMarket();
    m.historical = (c, r) => (c.secType === "STK" && r.whatToShow === "TRADES" && r.useRth
      ? [{ time: "20261119", open: 96, high: 99, low: 95, close: 97, volume: 1 }, { time: "20261120", open: 97, high: 121, low: 96, close: 120, volume: 1 }]
      : []);
    const ctx = ctxFor(new FakeSession(m), { now: () => new Date("2026-11-20T21:30:00Z") });   // 16:30 ET, last = 100 after hours
    await pushChain(ctx, { symbol: "AAPL", shape: { expiries: 1, moneyness: 0.1 }, final: true, sessionDate: "2026-11-20" });
    const body = ctx.ingest.calls.chain[0] as { final: boolean; underlying: { price: number; rth_close: number | null }; contracts: { strike: number }[] };
    expect(body.final).toBe(true);
    expect(body.underlying).toMatchObject({ price: 100, rth_close: 120 });
    expect(Math.min(...body.contracts.map((c) => c.strike))).toBe(110);   // ±10% of 120, not of 100
  });

  it("never archives a closing chain with < 95% of its contracts quoted: posts it live and fails (retry)", async () => {
    const m = aaplMarket();
    const base = m.ticks;
    m.ticks = (id) => (id.endsWith(":C") && Number(id.split(":")[2]) >= 110 ? [] : base(id));   // the 110 call (1 of 10) is silent
    const ctx = ctxFor(new FakeSession(m));
    await expect(pushChain(ctx, { symbol: "AAPL", shape: { expiries: 1, moneyness: 0.1 }, final: true, sessionDate: "2026-11-20" }))
      .rejects.toThrow(/incomplete \(9 of 10 contracts quoted\)/);
    const body = ctx.ingest.calls.chain[0] as { final: boolean; session_date?: string; contracts: unknown[] };
    expect(body.final).toBe(false);
    expect(body.session_date).toBeUndefined();
    expect(body.contracts).toHaveLength(10);
  });

  it("a live chain posts every requested contract (unquoted ones with nulls) and reports coverage per expiry", async () => {
    const m = aaplMarket();
    const base = m.ticks;
    m.ticks = (id) => (id.endsWith(":C") && Number(id.split(":")[2]) >= 110 ? [] : base(id));
    const ctx = ctxFor(new FakeSession(m));
    const r = await pushChain(ctx, { symbol: "AAPL", shape: { expiries: 1, moneyness: 0.1 }, final: false, sessionDate: null });
    expect(r?.contracts).toBe(10);
    expect(r?.coverage).toBeCloseTo(0.9, 6);
    expect(r?.expiries).toEqual([{ expiry: "2026-11-27", contracts: 10, quoted: 9 }]);
    const body = ctx.ingest.calls.chain[0] as { contracts: { strike: number; right: string; bid: number | null; oi: number | null }[] };
    expect(body.contracts.find((c) => c.strike === 110 && c.right === "C")).toMatchObject({ bid: null, oi: null });
  });

  it("every picked expiry is accounted for: one whose contracts got no quotes, one whose contract list never came", async () => {
    // DELL on a Saturday: the two weeklies before earnings must not silently disappear.
    const m = aaplMarket();
    const base = m.ticks;
    m.ticks = (id) => (id.includes(":20261127:") ? [] : base(id));
    const s = new FakeSession(m);
    const details = s.contractDetails.bind(s);
    s.contractDetails = async (c) => {
      if (c.lastTradeDateOrContractMonth === "20261204") throw new TwsTimeoutError("AAPL OPT contract details 20261204", 180_000);
      return details(c);
    };
    const ctx = ctxFor(s);
    const r = await pushChain(ctx, { symbol: "AAPL", shape: { expiries: 3, moneyness: 0.1 }, final: false, sessionDate: null });
    expect(r?.expiries).toEqual([
      { expiry: "2026-11-27", contracts: 10, quoted: 0 },
      { expiry: "2026-12-04", contracts: 0, quoted: 0 },
      { expiry: "2026-12-11", contracts: 10, quoted: 10 },
    ]);
    const body = ctx.ingest.calls.chain[0] as { contracts: { expiry: string }[] };
    expect(new Set(body.contracts.map((c) => c.expiry))).toEqual(new Set(["2026-11-27", "2026-12-11"]));
  });

  it("reports a symbol IBKR lists no options for (no option parameters) to the engine instead of a chain", async () => {
    const m = aaplMarket();
    m.optionParams = [];
    const ctx = ctxFor(new FakeSession(m));
    expect(await pushChain(ctx, { symbol: "AAPL", shape: { expiries: 1, moneyness: 0.1 }, final: false, sessionDate: null })).toBeNull();
    expect(ctx.ingest.calls.noOptions).toEqual([{ source: "ibkr", symbol: "AAPL" }]);
    expect(ctx.ingest.calls.chain).toBeUndefined();
    m.optionParams = [{ exchange: "SMART", tradingClass: "AAPL", multiplier: "100", expirations: ["20261113"], strikes: [] }];   // expired only
    expect(await pushChain(ctx, { symbol: "AAPL", shape: { expiries: 1, moneyness: 0.1 }, final: false, sessionDate: null })).toBeNull();
    expect(ctx.ingest.calls.noOptions).toHaveLength(2);
  });

  it("an unanswered option-parameter request is asked once more with a longer wait; without a saved list the chain waits for the contract server (never 'no options')", async () => {
    const s = new FakeSession(aaplMarket());
    const waits: (number | undefined)[] = [];
    s.optionParams = async (_u, _signal, timeoutMs) => { waits.push(timeoutMs); throw new TwsTimeoutError("AAPL option parameters", timeoutMs ?? 60_000); };
    const ctx = ctxFor(s);
    await expect(pushChain(ctx, { symbol: "AAPL", shape: { expiries: 1, moneyness: 0.1 }, final: false, sessionDate: null }))
      .rejects.toThrow(ContractsUnavailable);
    expect(waits).toEqual([undefined, OPTION_PARAMS_RETRY_MS]);
    expect(ctx.ingest.calls.noOptions).toBeUndefined();
  });

  it("a late answer to the second option-parameter request still builds the chain", async () => {
    const m = aaplMarket();
    const s = new FakeSession(m);
    let n = 0;
    s.optionParams = async () => { n += 1; if (n === 1) throw new TwsTimeoutError("AAPL option parameters", 60_000); return m.optionParams; };
    expect((await pushChain(ctxFor(s), { symbol: "AAPL", shape: { expiries: 1, moneyness: 0.1 }, final: false, sessionDate: null }))?.contracts).toBe(10);
  });

  it("re-uses option parameters per underlying for the New York day, also after a restart (an empty answer forgets the symbol)", async () => {
    const m = aaplMarket();
    const s = new FakeSession(m);
    const store = memoryContractStore();
    let now = new Date("2026-11-21T15:00:00Z");                 // Saturday
    const ctx = ctxFor(s, { contracts: new ContractCache(store), now: () => now });
    const req = { symbol: "AAPL", shape: { expiries: 1, moneyness: 0.1 }, final: false, sessionDate: null };
    await pushChain(ctx, req);
    await pushChain({ ...ctx, contracts: new ContractCache(store) }, req);     // a new launch reads the saved answer
    expect(s.optionParamsCalls).toHaveLength(1);
    now = new Date("2026-11-22T05:30:00Z");                     // 00:30 New York: a new day
    await pushChain(ctx, req);
    expect(s.optionParamsCalls).toHaveLength(2);
    m.optionParams = [];
    now = new Date("2026-11-23T15:00:00Z");
    expect(await pushChain(ctx, req)).toBeNull();               // IBKR lists no options now
    expect(store.read("AAPL")).toBeNull();
    expect(s.optionParamsCalls).toHaveLength(3);
  });

  it("fails clearly when TWS sends no underlying price", async () => {
    const m = aaplMarket();
    const base = m.ticks;
    m.ticks = (id) => (id === "AAPL:STK" ? [] : base(id));
    await expect(pushChain(ctxFor(new FakeSession(m)), { symbol: "AAPL", shape: { expiries: 1, moneyness: 0.1 }, final: false, sessionDate: null }))
      .rejects.toThrow(/no underlying price/);
  });

  it("centres the chain on the engine's price when TWS sends no underlying price (any symbol), marked as such", async () => {
    const m = aaplMarket();
    const base = m.ticks;
    m.ticks = (id) => (id === "AAPL:STK" ? [] : base(id));      // e.g. no market-data subscription for the underlying
    const ctx = ctxFor(new FakeSession(m));
    const asked: string[] = [];
    ctx.ingest.referencePrice = async (symbol) => { asked.push(symbol); return { symbol, price: 100, ts: "2026-11-19T21:00:00Z", source: "ibkr" }; };
    const r = await pushChain(ctx, { symbol: "AAPL", shape: { expiries: 1, moneyness: 0.1 }, final: false, sessionDate: null });
    expect(asked).toEqual(["AAPL"]);
    expect(r?.reference).toEqual({ symbol: "AAPL", price: 100, ts: "2026-11-19T21:00:00Z", source: "ibkr" });
    const body = ctx.ingest.calls.chain[0] as IngestChain;
    expect(body.underlying).toMatchObject({ price: 100, ts: "2026-11-19T21:00:00Z", reference: true });
    expect(body.delayed).toBe(true);                             // the underlying price is not current
    expect(body.contracts.map((c) => c.strike)).toEqual([90, 90, 95, 95, 100, 100, 105, 105, 110, 110]);   // ±10% of 100
  });

  it("names IBKR's error codes when no option quote arrives", async () => {
    const m = aaplMarket();
    m.streamErrors = (id) => (id.split(":").length === 4 ? new TwsRequestError(10091, "Part of requested market data requires additional subscription") : undefined);
    await expect(pushChain(ctxFor(new FakeSession(m)), { symbol: "AAPL", shape: { expiries: 1, moneyness: 0.1 }, final: false, sessionDate: null }))
      .rejects.toThrow(/TWS returned no option quotes \(OPRA subscription\?\); IBKR errors: 10091 ×20/);   // 10 contracts, each asked twice
  });
});

describe("underlying stats", () => {
  it("maps generic ticks 100/101/104/106 to the stats body", async () => {
    const m = emptyMarket();
    m.ticks = (id) => id !== "AAPL:STK" ? undefined : [
      { kind: "price", field: TICK.LAST, value: 99 }, { kind: "price", field: TICK.CLOSE, value: 100 }, { kind: "size", field: TICK.VOLUME, value: 4.1e7 },
      { kind: "size", field: TICK.OPTION_CALL_VOLUME, value: 338000 }, { kind: "size", field: TICK.OPTION_PUT_VOLUME, value: 251000 },
      { kind: "size", field: TICK.OPTION_CALL_OI, value: 2960000 }, { kind: "size", field: TICK.OPTION_PUT_OI, value: 2730000 },
      { kind: "generic", field: TICK.OPTION_IMPLIED_VOL, value: 0.251 }, { kind: "generic", field: TICK.OPTION_HISTORICAL_VOL, value: 0.27 },
    ];
    const s = new FakeSession(m);
    const ctx = ctxFor(s);
    expect(await pushUnderlyingStats(ctx, ["AAPL", "NODATA"], "2026-11-20")).toEqual({ symbols: ["AAPL"], delayed: false, optionTicks: true });
    expect(s.streams[0].generic).toBe("100,101,104,106");
    // The same streams' stock quote is posted too (no extra lines): headers and tables get price, OHLC and volume.
    expect(s.streams).toHaveLength(2);
    expect(ctx.ingest.calls.quotes[0]).toMatchObject({ source: "ibkr", delayed: false, quotes: [{ symbol: "AAPL", price: 99, change: expect.closeTo(-1, 9), prev_close: 100, volume: 4.1e7 }] });
    expect(ctx.ingest.calls.underlyingStats[0]).toMatchObject({ source: "ibkr", session_date: "2026-11-20", delayed: false, stats: [{
      symbol: "AAPL", price: 99, change_pct: expect.closeTo(-0.01, 10), volume: 4.1e7, call_volume: 338000, put_volume: 251000,
      call_oi: 2960000, put_oi: 2730000, iv30: 0.251, hv30: 0.27,
    }] });
  });
});

describe("index quotes", () => {
  it("posts value and change vs. the previous close; a symbol with only a close is skipped", async () => {
    const m = emptyMarket();
    m.ticks = (id) => id === "VIX:IND" ? [{ kind: "price", field: TICK.LAST, value: 27.8 }, { kind: "price", field: TICK.CLOSE, value: 24.54 }]
      : id === "VIX9D:IND" ? [{ kind: "price", field: TICK.CLOSE, value: 22 }] : undefined;
    const ctx = ctxFor(new FakeSession(m), { batchTimeoutMs: 100 });
    expect(await pushIndexQuotes(ctx, ["VIX", "VIX9D"])).toBe(1);
    const body = ctx.ingest.calls.quotes[0] as { quotes: { symbol: string; price: number; change: number; change_pct: number }[] };
    expect(body.quotes).toHaveLength(1);
    expect(body.quotes[0]).toMatchObject({ symbol: "VIX", price: 27.8, change: expect.closeTo(3.26, 6), change_pct: expect.closeTo(3.26 / 24.54, 6) });
  });
});

describe("live snapshots (INTEGRATION.md §9 IBKR fallback)", () => {
  it("posts the quotes of the symbols on screen (bid/ask/OHLC/volume, change vs. the close); unpriced ones are skipped", async () => {
    const m = emptyMarket();
    m.ticks = (id) => id === "AAPL:STK" ? [{ kind: "price", field: TICK.LAST, value: 101 }, { kind: "price", field: TICK.CLOSE, value: 100 },
      { kind: "price", field: TICK.BID, value: 100.9 }, { kind: "price", field: TICK.ASK, value: 101.1 }] : undefined;
    const ctx = ctxFor(new FakeSession(m), { batchTimeoutMs: 100 });
    expect(await pushLiveQuotes(ctx, ["AAPL", "NONE"])).toBe(1);
    expect(ctx.ingest.calls.quotes[0]).toMatchObject({ source: "ibkr", delayed: false, quotes: [{ symbol: "AAPL", price: 101, bid: 100.9, ask: 101.1, prev_close: 100, change: expect.closeTo(1, 9) }] });
  });
});

describe("delayed data (no real-time subscription)", () => {
  it("pushes delayed stats with nulls (never zeros) when generic ticks are missing, and says so", async () => {
    const m = emptyMarket();
    m.ticks = (id) => id === "KO:STK" ? [{ kind: "price", field: TICK.DELAYED_LAST, value: 60 }, { kind: "price", field: TICK.DELAYED_CLOSE, value: 61 }] : undefined;
    const ctx = ctxFor(new FakeSession(m));
    expect(await pushUnderlyingStats(ctx, ["KO"], "2026-11-20")).toEqual({ symbols: ["KO"], delayed: true, optionTicks: false });
    expect(ctx.ingest.calls.underlyingStats[0]).toMatchObject({ delayed: true, stats: [{ symbol: "KO", price: 60, call_volume: null, put_volume: null, call_oi: null, put_oi: null, iv30: null, hv30: null }] });
  });
});

describe("index history", () => {
  it("fetches daily TRADES bars of the IND contract back to `from` (10 years at most) and posts them", async () => {
    const m = emptyMarket();
    m.historical = () => [{ time: "20161118", open: 1, high: 1, low: 1, close: 1, volume: null }, { time: "20261120", open: 6600, high: 6650, low: 6550, close: 6602.1, volume: null }];
    const ctx = ctxFor(new FakeSession(m));
    expect(await pushIndexBars(ctx, "SPX", "2016-11-21")).toBe(1);
    const call = (ctx.session as FakeSession).historicalCalls[0];
    expect(call.contract).toMatchObject({ symbol: "SPX", secType: "IND", exchange: "CBOE" });
    expect(call.req).toEqual({ duration: "10 Y", whatToShow: "TRADES", useRth: true });
    expect(ctx.ingest.calls.indexBars[0]).toEqual({ source: "ibkr", symbol: "SPX", bars: [{ date: "2026-11-20", open: 6600, high: 6650, low: 6550, close: 6602.1 }] });
  });

  it("pushes a symbol's adjusted and raw daily closes (two paced requests) for its dividends", async () => {
    const m = emptyMarket();
    const reqs: string[] = [];
    m.historical = (c, r) => {
      reqs.push(`${c.symbol}:${c.secType}:${r.whatToShow}:${r.duration}:${r.useRth}:${r.barSize ?? "1 day"}`);
      const adj = r.whatToShow === "ADJUSTED_LAST";
      return [{ time: "20151231", open: 1, high: 1, low: 1, close: 1, volume: 1 },
        { time: "20261118", open: 60, high: 60, low: 60, close: adj ? 59.49 : 60, volume: 1 },
        { time: "20261119", open: 60, high: 60, low: 60, close: adj ? 59.5 : 59.5, volume: 1 }];
    };
    const pacer = new HistoricalPacer({ minSpacingMs: 0 });
    const ctx = ctxFor(new FakeSession(m), { pacer });
    expect(await pushDividendHistory(ctx, "KO", "2016-11-01")).toBe(2);
    expect(reqs).toEqual(["KO:STK:ADJUSTED_LAST:10 Y:true:1 day", "KO:STK:TRADES:10 Y:true:1 day"]);
    expect(ctx.ingest.calls.dividendHistory[0]).toEqual({ source: "ibkr", symbol: "KO",
      adjusted: [{ date: "2026-11-18", close: 59.49 }, { date: "2026-11-19", close: 59.5 }],
      trades: [{ date: "2026-11-18", close: 60 }, { date: "2026-11-19", close: 59.5 }] });   // the 2015 bar is before `from`
  });
});

describe("weekend on-demand chain (delayed-frozen, no OPRA)", () => {
  it("posts a chain from delayed ticks 66/67/83 on a Saturday; every request TWS gets has a valid expiry", async () => {
    const m = aaplMarket();
    m.ticks = (id) => id === "AAPL:STK"
      ? [{ kind: "price", field: TICK.DELAYED_LAST, value: 100 }, { kind: "price", field: TICK.DELAYED_CLOSE, value: 98 }]
      : id.split(":").length === 4 ? [
        { kind: "price", field: TICK.DELAYED_BID, value: 1 }, { kind: "price", field: TICK.DELAYED_ASK, value: 1.2 },
        { kind: "option", field: TICK.DELAYED_MODEL_OPTION, iv: 0.31, delta: 0.45, gamma: 0.02, vega: 0.1, theta: -0.03 }] : undefined;
    const s = new FakeSession(m);
    const ctx = ctxFor(s, { now: () => new Date("2026-11-21T17:00:00Z") });   // Saturday
    const r = await pushChain(ctx, { symbol: "AAPL", shape: { expiries: 1, moneyness: 0.1 }, final: false, sessionDate: null });
    expect(r?.coverage).toBe(1);
    const body = ctx.ingest.calls.chain[0] as { delayed: boolean; contracts: { expiry: string; iv: number; delta: number }[] };
    expect(body.delayed).toBe(true);
    expect(body.contracts[0]).toMatchObject({ expiry: "2026-11-27", iv: 0.31, delta: 0.45 });
  });

  it("a contract with an expiry TWS rejects (error 321) gets no data — the fake behaves like TWS", async () => {
    const s = new FakeSession(aaplMarket());
    const bad: TwsContract = { symbol: "AAPL", secType: "OPT", exchange: "SMART", currency: "USD", lastTradeDateOrContractMonth: "2026-11-27", strike: 100, right: "C" };
    const [q] = await collectQuotes(ctxFor(s), [bad], { genericTicks: "101", complete: (a) => a.optionComplete(), minDwellMs: 0 });
    expect(q.bid).toBeNull();
    await expect(pacedHistorical(ctxFor(s), bad, { duration: "5 D", whatToShow: "TRADES", useRth: true })).rejects.toThrow(/321/);
  });
});

describe("historical", () => {
  it("retries a request TWS did not answer in time without treating it as a pacing violation", async () => {
    const m = emptyMarket();
    m.historical = () => new TwsTimeoutError("AAPL historical TRADES", 120_000);
    const pacer = new HistoricalPacer({ minSpacingMs: 0 });
    const violation = vi.spyOn(pacer, "violation");
    await expect(pacedHistorical(ctxFor(new FakeSession(m), { pacer }), { symbol: "AAPL", secType: "STK", exchange: "SMART", currency: "USD" }, { duration: "1 Y", whatToShow: "TRADES", useRth: true }))
      .rejects.toBeInstanceOf(RetryLater);
    expect(violation).not.toHaveBeenCalled();
  });

  it("backs off and asks to retry on pacing violations; treats 'no data' as empty", async () => {
    const m = emptyMarket();
    let mode: "pacing" | "nodata" | "ok" = "pacing";
    m.historical = () => mode === "pacing" ? new TwsRequestError(162, "Historical Market Data Service error message:API historical data query cancelled: pacing violation")
      : mode === "nodata" ? new TwsRequestError(162, "Historical Market Data Service error message:HMDS query returned no data") :
      [{ time: "20261119", open: 0.24, high: 0.25, low: 0.23, close: 0.243, volume: null }, { time: "20151231", open: 1, high: 1, low: 1, close: 0.3, volume: null }];
    const pacer = new HistoricalPacer({ minSpacingMs: 0 });
    const violation = vi.spyOn(pacer, "violation");
    const ctx = ctxFor(new FakeSession(m), { pacer });
    await expect(pacedHistorical(ctx, { symbol: "AAPL", secType: "STK", exchange: "SMART", currency: "USD" }, { duration: "1 Y", whatToShow: "TRADES", useRth: true }))
      .rejects.toBeInstanceOf(RetryLater);
    expect(violation).toHaveBeenCalledTimes(1);
    pacer.success();
    mode = "nodata";
    expect(await pushVolHistory(ctxFor(new FakeSession(m), { pacer: new HistoricalPacer() }), "AAPL", "2016-01-01", "iv30")).toBe(0);
    mode = "ok";
    const okCtx = ctxFor(new FakeSession(m), { pacer: new HistoricalPacer() });
    expect(await pushVolHistory(okCtx, "AAPL", "2016-01-01", "iv30")).toBe(1);   // the 2015 bar is before `from`
    expect(okCtx.ingest.calls.volHistory[0]).toEqual({ source: "ibkr", symbol: "AAPL", kind: "iv30", bars: [{ date: "2026-11-19", value: 0.243 }] });
    expect((okCtx.session as FakeSession).historicalCalls[0].req).toEqual({ duration: "10 Y", whatToShow: "OPTION_IMPLIED_VOLATILITY", useRth: true });
  });
});

describe("IBKR contract (sec-def) server unavailable", () => {
  const req = { symbol: "AAPL", shape: { expiries: 2, moneyness: 0.25 }, final: false, sessionDate: null };
  type Body = { contracts_from_cache: boolean; contracts_listed_at: string | null; contracts: { expiry: string }[] };

  function setup() {
    const m = aaplMarket();
    const s = new FakeSession(m);
    const store = memoryContractStore();
    const clock = { farm: "ok" as FarmState, now: new Date("2026-11-23T15:00:00Z") };   // Monday
    const server = new ContractServer(() => clock.farm, () => clock.now.getTime());
    const ctx = ctxFor(s, { contracts: new ContractCache(store), contractServer: server, now: () => clock.now });
    const definitionRequests = () => s.optionParamsCalls.length + s.detailsCalls.length;
    return { m, s, store, server, ctx, clock, definitionRequests };
  }

  it("normal path: asks IBKR, posts a chain listed now and saves its contract list", async () => {
    const { s, store, ctx } = setup();
    await pushChain(ctx, req);
    const body = ctx.ingest.calls.chain[0] as Body;
    expect(body).toMatchObject({ contracts_from_cache: false, contracts_listed_at: null });
    expect(s.optionParamsCalls).toEqual([{ symbol: "AAPL", secType: "STK", conId: 265598 }]);
    expect(s.detailsCalls.map((c) => c.secType)).toEqual(["STK", "OPT", "OPT"]);
    const saved = store.read("AAPL") as { conId: number; expiries: Record<string, { contracts: unknown[]; fetchedAt: string }> };
    expect(saved.conId).toBe(265598);
    expect(Object.keys(saved.expiries)).toEqual(["20261127", "20261204"]);
    expect(saved.expiries["20261127"].contracts).toHaveLength(42);   // every listed contract, not only the strike band
  });

  it("farm reported down: builds the chain from the saved list (expired expiries dropped) and asks IBKR only for quotes", async () => {
    const { s, ctx, clock, definitionRequests } = setup();
    await pushChain(ctx, req);
    clock.farm = "down";
    clock.now = new Date("2026-11-28T15:00:00Z");              // Saturday: 2026-11-27 has expired
    const asked = definitionRequests();
    const streamed = s.streams.length;
    const r = await pushChain(ctx, req);
    expect(definitionRequests()).toBe(asked);
    const body = ctx.ingest.calls.chain[1] as Body;
    expect(body).toMatchObject({ contracts_from_cache: true, contracts_listed_at: "2026-11-23T15:00:00.000Z" });
    expect(new Set(body.contracts.map((c) => c.expiry))).toEqual(new Set(["2026-12-04"]));
    expect(r?.listedAt).toBe("2026-11-23T15:00:00.000Z");
    // Quotes by conId (+ SMART): the market-data farm serves them without contract definitions.
    expect(s.streams.slice(streamed).every((x) => x.contract.conId !== undefined)).toBe(true);
  });

  it("farm down and nothing saved: ContractsUnavailable at once, nothing asked of IBKR", async () => {
    const { s, ctx, clock, definitionRequests } = setup();
    clock.farm = "down";
    await expect(pushChain(ctx, req)).rejects.toBeInstanceOf(ContractsUnavailable);
    expect(definitionRequests()).toBe(0);
    expect(s.streams).toHaveLength(0);
  });

  it("unanswered requests without a farm status: uses the saved list, then asks again only after 15 min (one probe)", async () => {
    const { s, m, ctx, clock, server } = setup();
    clock.farm = "unknown";
    await pushChain(ctx, req);
    clock.now = new Date("2026-11-24T15:00:00Z");              // a new day: the parameters are asked for again
    let answer = false;
    s.optionParams = async (u, _signal, timeoutMs) => {
      s.optionParamsCalls.push(u);
      if (answer) return m.optionParams;
      throw new TwsTimeoutError("AAPL option parameters", timeoutMs ?? 60_000);
    };
    await pushChain(ctx, req);
    expect(s.optionParamsCalls).toHaveLength(3);               // the day before, then two unanswered tries
    expect((ctx.ingest.calls.chain[1] as Body).contracts_from_cache).toBe(true);
    expect(server.available()).toBe(false);
    await pushChain(ctx, req);                                 // within 15 min: the saved list, nothing asked
    expect(s.optionParamsCalls).toHaveLength(3);
    clock.now = new Date(clock.now.getTime() + SECDEF_PROBE_MS);
    answer = true;
    await pushChain(ctx, req);                                 // the probe is answered: listed now again
    expect(s.optionParamsCalls).toHaveLength(4);
    expect((ctx.ingest.calls.chain[3] as Body).contracts_from_cache).toBe(false);
    expect(server.available()).toBe(true);
  });

  it("zero option parameters (end without rows) is 'no options' at once: one request, no timeout, contract server still fine", async () => {
    const { s, m, ctx, server } = setup();
    m.optionParams = [];
    expect(await pushChain(ctx, req)).toBeNull();
    expect(s.optionParamsCalls).toHaveLength(1);
    expect(ctx.ingest.calls.noOptions).toEqual([{ source: "ibkr", symbol: "AAPL" }]);
    expect(server.available()).toBe(true);
  });
});

describe("no OPRA subscription (US options delayed only)", () => {
  const req = { symbol: "AAPL", shape: { expiries: 1, moneyness: 0.1 }, final: false, sessionDate: null };
  const SATURDAY = new Date("2026-11-21T17:00:00Z");
  const MONDAY_10AM = new Date("2026-11-23T15:00:00Z");      // 10:00 ET
  const isOption = (id: string) => id.split(":").length === 4;
  function memoryStore() {
    let v: unknown = null;
    return { read: () => v, write: (x: unknown) => { v = JSON.parse(JSON.stringify(x)); } };
  }
  /** Delayed ticks only (no real-time options), like an account without OPRA during the session. */
  function delayedMarket(): FakeMarket {
    const m = aaplMarket();
    m.ticks = (id) => isOption(id)
      ? [{ kind: "price", field: TICK.DELAYED_BID, value: 1 }, { kind: "price", field: TICK.DELAYED_ASK, value: 1.2 }]
      : [{ kind: "price", field: TICK.LAST, value: 100 }, { kind: "price", field: TICK.CLOSE, value: 98 }];
    return m;
  }

  it("a 354 on option quotes outside market hours switches to delayed-only (persisted); option quotes are then not requested", async () => {
    const m = aaplMarket();
    m.streamErrors = (id) => (isOption(id) ? new TwsRequestError(354, "Requested market data is not subscribed.") : undefined);
    const s = new FakeSession(m);
    const store = memoryStore();
    const feed = new OptionsFeed(store);
    const ctx = ctxFor(s, { optionsFeed: feed, now: () => SATURDAY });
    await expect(pushChain(ctx, req)).rejects.toBeInstanceOf(OptionQuotesClosed);
    expect(feed.mode).toBe("delayed_only");
    expect(new OptionsFeed(store).mode).toBe("delayed_only");
    const streamed = s.streams.length;
    await expect(pushChain(ctx, req)).rejects.toBeInstanceOf(OptionQuotesClosed);
    expect(s.streams).toHaveLength(streamed);                  // nothing requested at all
    expect(ctx.ingest.calls.chain).toBeUndefined();
    // Stock-side work is unaffected.
    expect((await pushUnderlyingStats(ctx, ["AAPL"], "2026-11-20")).symbols).toEqual(["AAPL"]);
  });

  it("in market hours the day's first option request is the probe (session default), later ones ask for delayed data (type 3)", async () => {
    const store = memoryStore();
    store.write({ mode: "delayed_only", since: "2026-11-21T17:00:00.000Z", probedDay: null, refusedDay: null });
    const s = new FakeSession(delayedMarket());
    const ctx = ctxFor(s, { optionsFeed: new OptionsFeed(store), now: () => MONDAY_10AM });
    const optionTypes = (from: number) => [...new Set(s.streams.slice(from).filter((x) => x.contract.secType === "OPT").map((x) => x.marketDataType))];
    await pushChain(ctx, req);
    expect(optionTypes(0)).toEqual([undefined]);
    const n = s.streams.length;
    await pushChain(ctx, req);
    expect(optionTypes(n)).toEqual([3]);
    expect(s.streams.filter((x) => x.contract.secType === "STK").every((x) => x.marketDataType === undefined)).toBe(true);
    expect(ctx.ingest.calls.chain).toHaveLength(2);
  });

  it("a refused probe in market hours is retried at once as delayed data; real-time option quotes end the mode", async () => {
    const store = memoryStore();
    store.write({ mode: "delayed_only", since: null, probedDay: null, refusedDay: null });
    const m = delayedMarket();
    m.streamErrors = (id, type) => (isOption(id) && type !== 3 ? new TwsRequestError(354, "Requested market data is not subscribed.") : undefined);
    const feed = new OptionsFeed(store);
    let now = MONDAY_10AM;
    const ctx = ctxFor(new FakeSession(m), { optionsFeed: feed, now: () => now });
    expect((await pushChain(ctx, req))?.coverage).toBe(1);
    expect(feed.mode).toBe("delayed_only");
    now = new Date("2026-11-24T15:00:00Z");                     // next day's probe gets real-time quotes (OPRA added)
    const ctx2 = ctxFor(new FakeSession(aaplMarket()), { optionsFeed: feed, now: () => now });
    await pushChain(ctx2, req);
    expect(feed.mode).toBe("realtime");
  });
});

it("contractId helper distinguishes options", () => {
  expect(contractId({ symbol: "A", secType: "OPT", exchange: "SMART", currency: "USD", lastTradeDateOrContractMonth: "20261127", strike: 5, right: "C" })).toBe("A:20261127:5:C");
});
