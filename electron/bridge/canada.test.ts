// Canadian listings through the bridge (INTEGRATION.md §6 "Markets"): how IBKR names them (TSX / TSX Venture stocks,
// Montréal Exchange options), that a pushed MX chain is keyed like Questrade's, per-market plans and sessions, and
// that an account without Canadian market data loses nothing of its US work. Against the fake TWS: the conventions
// are IBKR's documented ones, not verified against a live Gateway here.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Logger } from "../main/log";
import { DEFAULT_BRIDGE } from "../main/settings";
import type { BridgeStatus } from "../shared/api";
import { Bridge } from "./bridge";
import { optionContractsRequest, optionRows, pickExpiries, underlyingCandidates, underlyingContract } from "./chain";
import { ContractCache } from "./contract-cache";
import { contractId, emptyMarket, FakeConnector, FakeSession, memoryContractStore, type FakeMarket } from "./fake-tws.test-helper";
import { ContractServer } from "./farms";
import type { IngestApi, IngestChain, IngestContract, IngestQuotes, UnderlyingStats } from "./ingest";
import { pushChain, pushDividendHistory, pushUnderlyingStats, pushVolHistory, resolveUnderlying, type JobContext } from "./jobs";
import { Ledger } from "./ledger";
import { LineBudget } from "./line-budget";
import { interleave, listingOf, marketOf } from "./markets";
import { OptionsFeed } from "./options-feed";
import { HistoricalPacer } from "./pacer";
import { Permissions } from "./permissions";
import { positionRow } from "./positions";
import { TICK } from "./ticks";
import { TwsRequestError, type OptionParams, type TwsContract } from "./tws";
import { parseWants, planWants } from "./wants";

/** The chain both sides are tested against (the engine: engine/tests/test_bridge_canada.py). */
const MX = JSON.parse(readFileSync(join(__dirname, "../../engine/tests/fixtures/ibkr_mx_chain.json"), "utf8")) as {
  symbol: string; underlying: TwsContract; contracts: TwsContract[]; posted: Pick<IngestContract, "expiry" | "strike" | "right">[]; osi: string[];
};
/** The engine's code of a posted contract row (sources/osi.py format_osi: the root of a Canadian symbol is the symbol). */
const osi = (symbol: string, c: Pick<IngestContract, "expiry" | "strike" | "right">) =>
  `${symbol}${c.expiry.slice(2).replace(/-/g, "")}${c.right}${String(Math.round(c.strike * 1000)).padStart(8, "0")}`;

/** US Thanksgiving, 11:00 ET: Toronto trades, New York does not. */
const NOW = Date.parse("2026-11-26T16:00:00Z");
const PARAMS: OptionParams[] = [
  { exchange: "SMART", tradingClass: "AAPL", multiplier: "100", expirations: ["20261204"], strikes: [] },
  { exchange: "CDE", tradingClass: "RY", multiplier: "100", expirations: ["20261218", "20270115"], strikes: [] },
  { exchange: "CDE", tradingClass: "RY1", multiplier: "100", expirations: ["20261218"], strikes: [] },   // an adjusted class
];

interface Denials { stocks: boolean; options: boolean; history: boolean }

/** AAPL (US) and the fixture's RY.TO (TSX, options on CDE), each fully quoted unless Canada is `denied`. */
function twoMarkets(denied: Partial<Denials> = {}): FakeMarket {
  const m = emptyMarket();
  m.optionParams = PARAMS;
  m.details = (c) => {
    if (c.secType === "OPT") {
      return c.currency === "CAD" ? MX.contracts.filter((x) => x.lastTradeDateOrContractMonth === c.lastTradeDateOrContractMonth && c.exchange === "CDE")
        : [95, 100, 105].flatMap((k) => (["C", "P"] as const).map((right) => ({ ...c, strike: k, right, conId: k * 10 + (right === "C" ? 1 : 2) })));
    }
    if (c.currency === "USD") return [{ ...c, conId: 265598 }];
    if (c.symbol === "RY" && c.primaryExchange === "TSE") return [{ ...c, conId: MX.underlying.conId }];
    if (c.symbol === "RCI B" && c.primaryExchange === "TSE") return [{ ...c, conId: 5551 }];   // found under the spaced spelling only
    if (c.symbol === "ABC" && c.primaryExchange === "VENTURE") return [{ ...c, conId: 5552 }];
    throw new TwsRequestError(200, "No security definition has been found for the request");
  };
  m.ticks = (id) => {
    const stock = id.endsWith(":STK");
    if (stock) return [{ kind: "price", field: TICK.LAST, value: id.startsWith("CAD/") ? 121 : 100 }, { kind: "price", field: TICK.CLOSE, value: id.startsWith("CAD/") ? 120 : 99 }];
    return [{ kind: "price", field: TICK.DELAYED_BID, value: 2 }, { kind: "price", field: TICK.DELAYED_ASK, value: 2.2 }];
  };
  m.streamErrors = (id) => {
    if (!id.startsWith("CAD/")) return undefined;
    if (id.endsWith(":STK")) return denied.stocks ? new TwsRequestError(10089, "Requested market data requires additional subscription for API. Delayed market data is available. RY TSE/TOP/ALL") : undefined;
    return denied.options ? new TwsRequestError(354, "Requested market data is not subscribed. Delayed market data is not enabled.") : undefined;
  };
  m.historical = (c) => (c.currency === "CAD" && denied.history
    ? new TwsRequestError(162, "Historical Market Data Service error message:No market data permissions for TSE STK")
    : [{ time: "20261125", open: 0.2, high: 0.2, low: 0.2, close: 0.2, volume: null }]);
  return m;
}

function fakeIngest(): IngestApi & { calls: Record<string, unknown[]> } {
  const calls: Record<string, unknown[]> = {};
  const rec = (name: string) => async (b: unknown) => { (calls[name] ??= []).push(b); return { stored: 1 }; };
  return { calls, wants: async () => ({}), referencePrice: async () => { throw new Error("404 no price stored"); }, liveWants: async () => ({ symbols: [], interval_s: 5 }), chain: rec("chain"), noOptions: rec("noOptions"), volHistory: rec("volHistory"), indexBars: rec("indexBars"), dividendHistory: rec("dividendHistory"), quotes: rec("quotes"),
    underlyingStats: rec("underlyingStats"), optionBars: rec("optionBars"), intradayBars: rec("intradayBars"), ackWants: rec("ackWants"), heartbeat: rec("heartbeat"), refreshResult: rec("refreshResult"), positions: rec("positions") };
}

function ctxFor(session: FakeSession, o: Partial<JobContext> = {}): JobContext & { ingest: ReturnType<typeof fakeIngest>; notes: string[] } {
  const notes: string[] = [];
  return { session, ingest: fakeIngest(), pacer: new HistoricalPacer({ minSpacingMs: 0 }), lines: new LineBudget(() => 50), now: () => new Date(NOW),
    signal: new AbortController().signal, checkpoint: async () => undefined, batchTimeoutMs: 100, note: (m: string) => notes.push(m), notes, ...o } as JobContext & { ingest: ReturnType<typeof fakeIngest>; notes: string[] };
}

describe("IBKR contracts of Canadian listings", () => {
  it("names a TSX / TSX Venture stock by primary exchange and CAD, keeping the dot of class shares", () => {
    expect(underlyingContract("RY.TO")).toEqual({ symbol: "RY", secType: "STK", exchange: "SMART", currency: "CAD", primaryExchange: "TSE" });
    expect(underlyingContract("rci.b.to")).toEqual({ symbol: "RCI.B", secType: "STK", exchange: "SMART", currency: "CAD", primaryExchange: "TSE" });
    expect(underlyingContract("AP.UN.TO")).toMatchObject({ symbol: "AP.UN", primaryExchange: "TSE" });
    expect(underlyingContract("XYZ.VN")).toEqual({ symbol: "XYZ", secType: "STK", exchange: "SMART", currency: "CAD", primaryExchange: "VENTURE" });
    // the US listing of the same ticker is another contract, exactly as before
    expect(underlyingContract("RY")).toEqual({ symbol: "RY", secType: "STK", exchange: "SMART", currency: "USD" });
    expect(underlyingContract("BRK.B")).toEqual({ symbol: "BRK B", secType: "STK", exchange: "SMART", currency: "USD" });
    expect(underlyingContract("SPX")).toEqual({ symbol: "SPX", secType: "IND", exchange: "CBOE", currency: "USD" });
    expect([marketOf("RY"), marketOf("RY.TO"), marketOf("XYZ.VN"), marketOf(".TO")]).toEqual(["US", "CA", "CA", "US"]);
  });

  it("offers the spaced spelling of a class share as a second candidate (the contract server decides)", () => {
    expect(underlyingCandidates("RCI.B.TO").map((c) => c.symbol)).toEqual(["RCI.B", "RCI B"]);
    expect(underlyingCandidates("RY.TO").map((c) => c.symbol)).toEqual(["RY"]);
    expect(underlyingCandidates("BRK.B").map((c) => c.symbol)).toEqual(["BRK B"]);
    expect(underlyingCandidates("SPX")).toEqual([underlyingContract("SPX")]);
  });

  it("asks for Montréal Exchange options on CDE in CAD, US ones on SMART in USD", () => {
    const pick = { expiry: "20261218", tradingClass: "RY", multiplier: "100" };
    expect(optionContractsRequest("RY.TO", underlyingContract("RY.TO"), pick)).toEqual({
      symbol: "RY", secType: "OPT", exchange: "CDE", currency: "CAD", lastTradeDateOrContractMonth: "20261218", tradingClass: "RY", multiplier: "100" });
    expect(optionContractsRequest("AAPL", underlyingContract("AAPL"), { ...pick, tradingClass: "AAPL" })).toMatchObject({ symbol: "AAPL", exchange: "SMART", currency: "USD" });
  });

  it("takes a Canadian symbol's option parameters from the CDE rows and prefers the standard class", () => {
    const now = new Date(NOW);
    expect(optionRows(PARAMS, "RY.TO").map((p) => p.tradingClass)).toEqual(["RY", "RY1"]);
    expect(optionRows(PARAMS, "AAPL").map((p) => p.exchange)).toEqual(["SMART"]);
    expect(pickExpiries(PARAMS, "RY.TO", now, 8)).toEqual([
      { expiry: "20261218", tradingClass: "RY", multiplier: "100" }, { expiry: "20270115", tradingClass: "RY", multiplier: "100" }]);
    expect(pickExpiries(PARAMS, "AAPL", now, 8)).toEqual([{ expiry: "20261204", tradingClass: "AAPL", multiplier: "100" }]);
    // TECK.B's options trade as class TECK (the engine's MX root); an adjusted class listed first does not win
    const teck: OptionParams[] = [{ exchange: "CDE", tradingClass: "TECK1", multiplier: "100", expirations: ["20261218"], strikes: [] },
      { exchange: "CDE", tradingClass: "TECK", multiplier: "100", expirations: ["20261218"], strikes: [] }];
    expect(listingOf("TECK.B.TO").optionClass).toBe("TECK");
    expect(pickExpiries(teck, "TECK.B.TO", now, 8)).toEqual([{ expiry: "20261218", tradingClass: "TECK", multiplier: "100" }]);
    // rows of the exchange absent (IBKR answered another exchange only): they are used rather than nothing
    expect(optionRows([PARAMS[0]], "RY.TO")).toEqual([PARAMS[0]]);
  });
});

describe("a Montréal Exchange chain", () => {
  const req = { symbol: MX.symbol, shape: { expiries: 8, moneyness: 0.25 }, final: false, sessionDate: null };

  it("is requested with IBKR's Canadian contracts and posted under the codes Questrade's chain has", async () => {
    const s = new FakeSession(twoMarkets());
    const store = memoryContractStore();
    const ctx = ctxFor(s, { contracts: new ContractCache(store) });
    const r = await pushChain(ctx, req);
    expect(s.detailsCalls[0]).toEqual({ symbol: "RY", secType: "STK", exchange: "SMART", currency: "CAD", primaryExchange: "TSE" });
    expect(s.optionParamsCalls).toEqual([{ symbol: "RY", secType: "STK", conId: MX.underlying.conId }]);
    expect(s.detailsCalls.slice(1)).toEqual(["20261218", "20270115"].map((e) => ({
      symbol: "RY", secType: "OPT", exchange: "CDE", currency: "CAD", lastTradeDateOrContractMonth: e, tradingClass: "RY", multiplier: "100" })));
    // the underlying by conId, the options as the contract server returned them (on CDE)
    expect(s.streams[0].contract).toMatchObject({ conId: MX.underlying.conId, currency: "CAD" });
    expect(s.streams.slice(1).every((x) => x.contract.exchange === "CDE" && x.contract.currency === "CAD")).toBe(true);
    const body = ctx.ingest.calls.chain[0] as IngestChain;
    expect(body).toMatchObject({ source: "ibkr", symbol: "RY.TO", final: false, delayed: true, underlying: { price: 121, prev_close: 120 } });
    expect(body.contracts.map(({ expiry, strike, right }) => ({ expiry, strike, right }))).toEqual(MX.posted);
    expect(body.contracts.map((c) => osi(body.symbol, c))).toEqual(MX.osi);                 // = Questrade's MX coding
    expect(body.contracts.every((c) => c.bid === 2 && c.ask === 2.2)).toBe(true);
    expect(r).toMatchObject({ contracts: 4, coverage: 1, optionsDelayed: true });
    expect(ctx.notes).toEqual([`RY.TO is conId ${MX.underlying.conId} at IBKR ("RY", TSE, CAD)`]);
    // saved under the engine's symbol: the NYSE listing RY has its own entry
    expect([...store.files.keys()]).toEqual(["RY.TO"]);
  });

  it("is rebuilt from the saved contract list on CDE while IBKR's contract server is down", async () => {
    const store = memoryContractStore();
    await pushChain(ctxFor(new FakeSession(twoMarkets()), { contracts: new ContractCache(store) }), req);
    const s = new FakeSession(twoMarkets());
    const ctx = ctxFor(s, { contracts: new ContractCache(store), contractServer: new ContractServer(() => "down", () => NOW) });
    await pushChain(ctx, req);
    expect(s.detailsCalls).toEqual([]);
    expect(s.streams.slice(1).map((x) => x.contract)).toEqual(MX.contracts.map((c) => ({ ...c, symbol: "RY" })).sort((a, b) =>
      `${a.lastTradeDateOrContractMonth}${String(a.strike).padStart(8, "0")}${a.right}`.localeCompare(`${b.lastTradeDateOrContractMonth}${String(b.strike).padStart(8, "0")}${b.right}`)));
    const body = ctx.ingest.calls.chain[0] as IngestChain;
    expect(body.contracts_from_cache).toBe(true);
    expect(body.contracts.map((c) => osi(body.symbol, c)).sort()).toEqual([...MX.osi].sort());
  });

  it("is centred on the engine's price when the account has no TSX stock data but Montréal Exchange quotes", async () => {
    const ctx = ctxFor(new FakeSession(twoMarkets({ stocks: true })));
    ctx.ingest.referencePrice = async (symbol) => ({ symbol, price: 121.5, ts: "2026-11-26T15:58:00Z", source: "questrade" });
    const r = await pushChain(ctx, req);
    expect(r?.reference).toMatchObject({ symbol: "RY.TO", price: 121.5, source: "questrade" });
    const body = ctx.ingest.calls.chain[0] as IngestChain;
    expect(body.underlying).toMatchObject({ price: 121.5, reference: true });
    expect(body.contracts.map((c) => osi(body.symbol, c))).toEqual(MX.osi);
  });

  it("names the Montréal Exchange when no option quote arrives, with IBKR's error codes", async () => {
    const ctx = ctxFor(new FakeSession(twoMarkets({ options: true })));
    await expect(pushChain(ctx, req)).rejects.toThrow(/no market data subscription for these contracts \(IBKR errors: 354 ×4\)/);
    const m = twoMarkets();
    m.ticks = (id) => (id.endsWith(":STK") ? [{ kind: "price", field: TICK.LAST, value: 121 }] : []);
    await expect(pushChain(ctxFor(new FakeSession(m)), req)).rejects.toThrow("RY.TO: TWS returned no option quotes (Montréal Exchange subscription?)");
  });
});

describe("the underlying of a Canadian symbol", () => {
  it("is resolved once through contract details (trying the spaced class spelling), saved, then addressed by conId", async () => {
    const s = new FakeSession(twoMarkets());
    const store = memoryContractStore();
    const ctx = ctxFor(s, { contracts: new ContractCache(store) });
    expect(await resolveUnderlying(ctx, "RCI.B.TO")).toMatchObject({ symbol: "RCI.B", conId: 5551, currency: "CAD" });
    expect(s.detailsCalls.map((c) => c.symbol)).toEqual(["RCI.B", "RCI B"]);
    expect(ctx.notes).toEqual(['RCI.B.TO is conId 5551 at IBKR ("RCI B", TSE, CAD)']);
    const again = ctxFor(new FakeSession(twoMarkets()), { contracts: new ContractCache(store) });       // a new launch
    expect((await resolveUnderlying(again, "RCI.B.TO")).conId).toBe(5551);
    expect((again.session as FakeSession).detailsCalls).toEqual([]);
    // a US symbol is its contract as before: nothing asked
    expect(await resolveUnderlying(ctx, "AAPL")).toEqual(underlyingContract("AAPL"));
    expect(s.detailsCalls).toHaveLength(2);
  });

  it("is the documented contract while the contract server cannot be asked, and an unknown listing is asked once", async () => {
    const down = ctxFor(new FakeSession(twoMarkets()), { contracts: new ContractCache(memoryContractStore()), contractServer: new ContractServer(() => "down", () => NOW) });
    expect(await resolveUnderlying(down, "RY.TO")).toEqual(underlyingContract("RY.TO"));
    const s = new FakeSession(twoMarkets());
    const ctx = ctxFor(s, { contracts: new ContractCache(memoryContractStore()) });
    await expect(resolveUnderlying(ctx, "NOPE.A.TO")).rejects.toThrow(/No security definition/);
    expect(ctx.notes).toEqual(['NOPE.A.TO: not found at IBKR (asked for "NOPE.A", "NOPE A" on TSE, CAD): TWS 200: No security definition has been found for the request']);
    await expect(resolveUnderlying(ctx, "NOPE.A.TO")).rejects.toThrow(/not found at IBKR/);
    expect(s.detailsCalls).toHaveLength(2);
  });

  it("feeds stats, IV/HV and dividend history requests; an unknown listing does not fail its batch", async () => {
    const s = new FakeSession(twoMarkets());
    const ctx = ctxFor(s, { contracts: new ContractCache(memoryContractStore()) });
    const done = await pushUnderlyingStats(ctx, ["RY.TO", "NOPE.TO", "ABC.VN"], "2026-11-26");
    expect(done.symbols).toEqual(["RY.TO", "ABC.VN"]);
    expect(s.streams.map((x) => contractId(x.contract))).toEqual(["CAD/RY:STK", "CAD/ABC:STK"]);
    expect(s.streams.map((x) => x.contract.conId)).toEqual([MX.underlying.conId, 5552]);
    const stats = ctx.ingest.calls.underlyingStats[0] as UnderlyingStats;
    expect(stats).toMatchObject({ session_date: "2026-11-26", stats: [{ symbol: "RY.TO", price: 121 }, { symbol: "ABC.VN", price: 121 }] });
    expect((ctx.ingest.calls.quotes[0] as IngestQuotes).quotes.map((q) => q.symbol)).toEqual(["RY.TO", "ABC.VN"]);
    await pushVolHistory(ctx, "RY.TO", "2026-11-01", "iv30");
    await pushDividendHistory(ctx, "RY.TO", "2026-11-01");
    expect(s.historicalCalls.map((h) => [h.contract.conId, h.contract.currency, h.req.whatToShow])).toEqual([
      [MX.underlying.conId, "CAD", "OPTION_IMPLIED_VOLATILITY"], [MX.underlying.conId, "CAD", "ADJUSTED_LAST"], [MX.underlying.conId, "CAD", "TRADES"]]);
    expect(ctx.ingest.calls.volHistory[0]).toMatchObject({ symbol: "RY.TO", kind: "iv30" });
  });
});

describe("positions", () => {
  it("pass on the exchange TWS names, so the engine can tell RY on the TSX from RY on the NYSE", () => {
    const contract = { symbol: "RY", secType: "STK", currency: "CAD", conId: MX.underlying.conId ?? null, expiry: null, strike: null, right: null, multiplier: null, tradingClass: "RY" };
    expect(positionRow({ account: "U1", pos: 100, avgCost: 100, contract: { ...contract, exchange: "TSE" } })).toMatchObject({ symbol: "RY", currency: "CAD", exchange: "TSE" });
    expect(positionRow({ account: "U1", pos: 100, avgCost: 100, contract })).not.toHaveProperty("exchange");
  });
});

describe("Permissions", () => {
  it("denies a data group only while nothing of it was served, and lets a denial lapse", () => {
    let now = 0;
    const p = new Permissions(() => now, 1000);
    const tsx = listingOf("RY.TO").stockData!;
    expect(listingOf("AAPL").stockData).toBeUndefined();          // a US symbol does not say which exchange lists it
    expect([tsx.id, listingOf("XYZ.VN").stockData!.id, listingOf("RY.TO").optionData.id, listingOf("AAPL").optionData.id])
      .toEqual(["stocks:TSX", "stocks:TSXV", "options:CA", "options:US"]);
    expect(p.isDenied(tsx.id)).toBe(false);
    expect(p.deny(tsx)).toBe(true);
    expect(p.deny(tsx)).toBe(false);                               // logged once
    expect(p.isDenied(tsx.id)).toBe(true);
    expect(p.isDenied("stocks:TSXV")).toBe(false);
    expect(p.labels()).toEqual(["TSX stocks"]);
    now = 1000;
    expect(p.isDenied(tsx.id)).toBe(false);                        // asked again after the retry time
    expect(p.serve(tsx.id)).toBe(true);
    expect(p.serve(tsx.id)).toBe(false);
    expect(p.hasServed(tsx.id)).toBe(true);
    expect(p.isDenied(undefined)).toBe(false);
  });
});

describe("per-market wants", () => {
  const RAW = {
    version: 2,
    underlying_stats: [
      { market: "US", session_date: "2026-11-25", symbols: ["AAPL"], max_age_s: 21_600, as_of_session: "2026-11-25" },
      { market: "CA", session_date: "2026-11-26", symbols: ["RY.TO", "ABC.VN"], max_age_s: 1800 }],
    chain_universe: [
      { market: "US", symbols: ["AAPL"], max_age_s: 21_600, expiries: 1, moneyness: 0.2, as_of_session: "2026-11-25" },
      { market: "CA", symbols: ["RY.TO"], max_age_s: 3600, expiries: 6, moneyness: 0.2 }],
    eod_chains: [
      { market: "US", session_date: "2026-11-25", symbols: ["AAPL", "SPX"], not_before: "2026-11-25T16:20:00-05:00", deadline: "2026-11-25T20:00:00-05:00" },
      { market: "CA", session_date: "2026-11-26", symbols: ["RY.TO"], not_before: "2026-11-26T16:20:00-05:00", deadline: "2026-11-26T20:00:00-05:00" }],
  };
  const ctx = (now: number) => ({ now, lastChainPush: () => undefined, lastStatsPush: () => undefined, attemptedWithin: () => false });

  it("parses one plan per market, each with its own session", () => {
    const w = parseWants(RAW);
    expect(w.underlyingStats).toEqual([
      { symbols: ["AAPL"], maxAgeS: 21_600, asOfSession: "2026-11-25", sessionDate: "2026-11-25" },
      { symbols: ["RY.TO", "ABC.VN"], maxAgeS: 1800, asOfSession: null, sessionDate: "2026-11-26" }]);
    expect(w.chainUniverse.map((u) => u.asOfSession)).toEqual(["2026-11-25", null]);
    expect(w.eodChains.map((e) => e.sessionDate)).toEqual(["2026-11-25", "2026-11-26"]);
    expect(() => parseWants({ underlying_stats: [{ symbols: ["RY.TO"], max_age_s: 1800, session_date: "today" }] })).toThrow(/underlying_stats\[0\]\.session_date/);
    expect(() => parseWants({ eod_chains: [null] })).toThrow(/eod_chains\[0\]/);
  });

  it("plans a market's closing chains only in that market's own window", () => {
    const w = parseWants(RAW);
    const midday = planWants(w, ctx(NOW));                              // Thanksgiving 11:00 ET: no window is open
    expect(midday.eod).toEqual([]);
    expect(midday.universe.map((u) => u.symbols)).toEqual([["AAPL"], ["RY.TO"]]);
    expect(midday.stats.map((s) => [s.sessionDate, s.symbols])).toEqual([["2026-11-25", ["AAPL"]], ["2026-11-26", ["RY.TO", "ABC.VN"]]]);
    const afterToronto = planWants(w, ctx(Date.parse("2026-11-26T21:30:00Z")));   // 16:30 ET: Toronto's window only
    expect(afterToronto.eod).toEqual([{ sessionDate: "2026-11-26", symbols: ["RY.TO"] }]);
    expect(afterToronto.universe.map((u) => u.symbols)).toEqual([["AAPL"], []]);
  });

  it("interleaves the markets' work", () => {
    expect(interleave([["A1", "A2", "A3"], ["C1"], []])).toEqual(["A1", "C1", "A2", "A3"]);
  });

  it("queues the markets' chains in turns, so one market's round never waits for the whole of the other's", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const live = { version: 2, chain_universe: [
      { market: "US", symbols: ["AAPL", "MSFT", "KO"], max_age_s: 3600, expiries: 1, moneyness: 0.2 },
      { market: "CA", symbols: ["RY.TO"], max_age_s: 3600, expiries: 6, moneyness: 0.2 }] };
    const h = harness(live, twoMarkets());
    h.bridge.start();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(h.calls.filter((c) => c.startsWith("chain:"))).toEqual(["chain:AAPL", "chain:RY.TO", "chain:MSFT", "chain:KO"]);
    h.bridge.stop();
    vi.useRealTimers();
  });
});

// ---------------------------------------------------------------------------------------------- the whole bridge
function harness(wants: unknown, market: FakeMarket, o: { live?: () => unknown } = {}) {
  const calls: string[] = [];
  const logs: string[] = [];
  const stats: UnderlyingStats[] = [];
  const chains: IngestChain[] = [];
  const quotes: IngestQuotes[] = [];
  const log: Logger = { info: (m) => logs.push(m), warn: (m) => logs.push(m), error: (m) => logs.push(m) };
  const rec = (name: string) => async (b: unknown) => { calls.push(`${name}:${(b as { symbol?: string })?.symbol ?? ""}`); return {}; };
  const ingest: IngestApi = {
    wants: async () => wants, referencePrice: async () => ({ symbol: "RY.TO", price: 121, ts: "2026-11-26T15:59:00Z", source: "questrade" }),
    liveWants: async () => (o.live ? o.live() : { symbols: [], interval_s: 5 }),
    chain: async (b) => { chains.push(b); return rec("chain")(b); }, noOptions: rec("noOptions"), ackWants: async () => ({}), volHistory: rec("vol"), indexBars: rec("bars"),
    dividendHistory: rec("divs"), quotes: async (b) => { quotes.push(b); return {}; }, underlyingStats: async (b) => { stats.push(b); calls.push(`stats:${b.stats.map((s) => s.symbol).join(",")}`); return {}; },
    optionBars: rec("obars"), intradayBars: rec("intraday"), heartbeat: async () => ({}), refreshResult: async () => ({}), positions: async () => ({}),
  };
  const session = new FakeSession(market);
  const statuses: BridgeStatus[] = [];
  const feeds = new Map<string, unknown>();
  const bridge = new Bridge({
    connector: new FakeConnector(new Map([[4001, session]])), ingest, ledger: new Ledger(null, () => Date.now()), settings: () => DEFAULT_BRIDGE,
    clientName: "optionlens-mac test", log, onStatus: (s) => statuses.push(s), now: () => Date.now(),
    pacer: new HistoricalPacer({ minSpacingMs: 0 }), batchTimeoutMs: 50, contractStore: memoryContractStore(),
    optionsFeedStore: (m) => ({ read: () => feeds.get(m) ?? null, write: (v) => { feeds.set(m, v); } }),
  });
  return { bridge, calls, logs, stats, chains, quotes, session, statuses, feeds, status: () => statuses[statuses.length - 1] };
}

const WANTS = {
  version: 2,
  iv_history: [{ symbol: "RY.TO", from: "2026-11-01" }, { symbol: "ABC.VN", from: "2026-11-01" }, { symbol: "AAPL", from: "2026-11-01" }],
  hv_history: [{ symbol: "RY.TO", from: "2026-11-01" }, { symbol: "AAPL", from: "2026-11-01" }],
  dividend_history: [{ symbol: "RY.TO", from: "2026-11-01" }, { symbol: "AAPL", from: "2026-11-01" }],
  underlying_stats: [
    { market: "US", session_date: "2026-11-25", symbols: ["AAPL", "KO"], max_age_s: 21_600, as_of_session: "2026-11-25" },
    { market: "CA", session_date: "2026-11-26", symbols: ["RY.TO", "RCI.B.TO", "ABC.VN"], max_age_s: 1800 }],
  chain_universe: [
    { market: "US", symbols: ["AAPL", "MSFT", "KO"], max_age_s: 21_600, expiries: 1, moneyness: 0.2, as_of_session: "2026-11-25" },
    { market: "CA", symbols: ["RY.TO"], max_age_s: 3600, expiries: 6, moneyness: 0.2 }],
  chains_on_demand_max_age_s: 120,
};

describe("Bridge with US and Canadian work", () => {
  beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(NOW); });
  afterEach(() => { vi.useRealTimers(); });

  it("works through both markets: stats per market and session, chains taking turns, histories", async () => {
    const h = harness(WANTS, twoMarkets());
    h.bridge.start();
    await vi.advanceTimersByTimeAsync(20_000);
    // one stats push per market and exchange, each for its own market's session
    expect(h.stats.map((b) => [b.session_date, b.stats.map((s) => s.symbol)])).toEqual(expect.arrayContaining([
      ["2026-11-25", ["AAPL", "KO"]], ["2026-11-26", ["RY.TO", "RCI.B.TO"]], ["2026-11-26", ["ABC.VN"]]]));
    expect(h.stats).toHaveLength(3);
    // Toronto's live chains are not held back by the US after-hours fill (whose chains follow its stats)
    const chains = h.calls.filter((c) => c.startsWith("chain:"));
    expect(chains).toEqual(["chain:RY.TO", "chain:AAPL", "chain:MSFT", "chain:KO"]);
    expect(h.chains[0]).toMatchObject({ symbol: "RY.TO", delayed: true });
    expect(h.chains[0].contracts.map((c) => osi("RY.TO", c))).toEqual(MX.osi);
    expect(h.calls.filter((c) => c.startsWith("vol:")).sort()).toEqual(["vol:AAPL", "vol:AAPL", "vol:ABC.VN", "vol:RY.TO", "vol:RY.TO"]);
    expect(h.calls.filter((c) => c.startsWith("divs:")).sort()).toEqual(["divs:AAPL", "divs:RY.TO"]);
    expect(h.logs).toEqual(expect.arrayContaining([
      `bridge: RY.TO is conId ${MX.underlying.conId} at IBKR ("RY", TSE, CAD)`,
      "bridge: IBKR serves TSX stocks market data", "bridge: IBKR serves TSX Venture stocks market data",
      "bridge: first Canadian options chain pushed: RY.TO, 4 contracts, 100% quoted, options delayed, underlying real-time",
      "bridge: first US options chain pushed: AAPL, 6 contracts, 100% quoted, options delayed, underlying real-time"]));
    expect(h.status().populate).toBeNull();                                                 // the US fill is complete
    expect(h.status().notes).toContain("IBKR delayed Canadian option quotes (no Montréal Exchange subscription)");
    expect(h.status().notes).toContain("IBKR delayed (no OPRA subscription)");
    h.bridge.stop();
  });

  it("an account without Canadian market data: Canada is skipped as a group, every US task still runs", async () => {
    const h = harness(WANTS, twoMarkets({ stocks: true, options: true, history: true }));
    h.bridge.start();
    await vi.advanceTimersByTimeAsync(20_000);
    // US: exactly the work of a US-only plan
    expect(h.stats.map((b) => [b.session_date, b.stats.map((s) => s.symbol)])).toEqual([["2026-11-25", ["AAPL", "KO"]]]);
    expect(h.calls.filter((c) => c.startsWith("chain:"))).toEqual(["chain:AAPL", "chain:MSFT", "chain:KO"]);
    expect(h.calls.filter((c) => c.startsWith("vol:") || c.startsWith("divs:")).sort()).toEqual(["divs:AAPL", "vol:AAPL", "vol:AAPL"]);
    // Canada: each exchange's stocks are refused once (the first request of them, here a historical one), with
    // IBKR's own message; the options feed learns it has no subscription
    const denied = h.logs.filter((l) => /^bridge: no market-data permission for TSX( Venture)? stocks/.test(l));
    expect(denied).toEqual([
      "bridge: no market-data permission for TSX stocks (no TSX subscription?); skipping them for 20 h (TWS 162: Historical Market Data Service error message:No market data permissions for TSE STK)",
      "bridge: no market-data permission for TSX Venture stocks (no TSX Venture subscription?); skipping them for 20 h (TWS 162: Historical Market Data Service error message:No market data permissions for TSE STK)"]);
    expect(h.logs.some((l) => /^bridge RY\.TO chain: TWS 354: no market data subscription for these contracts \(IBKR errors: 354 ×\d+\)/.test(l))).toBe(true);
    expect(h.feeds.get("CA")).toMatchObject({ mode: "delayed_only" });
    expect((h.feeds.get("US") as { mode?: string } | undefined)?.mode ?? "unknown").toBe("unknown");   // the US feed is another subscription
    const notes = h.status().notes.join("\n");
    expect(notes).toContain("No IBKR market-data permission (skipped): ");
    expect(notes).toContain("TSX stocks");
    expect(notes).toContain("Canadian options: delayed only (no Montréal Exchange subscription)");
    expect(notes).not.toContain("US options: delayed only");
    // one refused request per exchange: the rest of its queued stats and history is dropped, none of it asked
    expect(h.session.historicalCalls.filter((c) => c.contract.currency === "CAD")).toHaveLength(2);
    expect(h.session.streams.filter((x) => contractId(x.contract).startsWith("CAD/") && x.contract.secType === "STK").length).toBeLessThanOrEqual(1);   // the chain's underlying
    // the next polls ask nothing Canadian of TWS again
    const streams = h.session.streams.length, details = h.session.detailsCalls.length;
    await h.bridge.pollWants();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(h.session.streams.slice(streams).filter((x) => x.contract.currency === "CAD")).toEqual([]);
    expect(h.session.detailsCalls.slice(details).filter((c) => c.currency === "CAD")).toEqual([]);
    expect(h.stats).toHaveLength(1);
    h.bridge.stop();
  });

  it("refused Canadian stock quotes deny the exchange with IBKR's error codes; US stats are pushed as ever", async () => {
    const h = harness({ version: 2, underlying_stats: WANTS.underlying_stats }, twoMarkets({ stocks: true }));
    h.bridge.start();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(h.stats.map((b) => b.stats.map((s) => s.symbol))).toEqual([["AAPL", "KO"]]);
    expect(h.logs.filter((l) => l.includes("no market-data permission"))).toEqual([
      "bridge: no market-data permission for TSX stocks (no TSX subscription?); skipping them for 20 h (TWS 354: no market data subscription for these contracts (IBKR errors: 10089 ×2))",
      "bridge: no market-data permission for TSX Venture stocks (no TSX Venture subscription?); skipping them for 20 h (TWS 354: no market data subscription for these contracts (IBKR errors: 10089 ×1))"]);
    expect(h.status().notes).toContain("No IBKR market-data permission (skipped): TSX stocks, TSX Venture stocks");
    h.bridge.stop();
  });

  it("a refused Canadian history request drops the exchange's queued history, not the US one", async () => {
    const wants = { version: 2, iv_history: [{ symbol: "RY.TO", from: "2026-11-01" }, { symbol: "RCI.B.TO", from: "2026-11-01" }, { symbol: "AAPL", from: "2026-11-01" }],
      hv_history: [{ symbol: "RY.TO", from: "2026-11-01" }, { symbol: "AAPL", from: "2026-11-01" }] };
    const h = harness(wants, twoMarkets({ history: true }));
    h.bridge.start();
    await vi.advanceTimersByTimeAsync(20_000);
    expect(h.session.historicalCalls.filter((c) => c.contract.currency === "CAD")).toHaveLength(1);
    expect(h.calls.filter((c) => c.startsWith("vol:"))).toEqual(["vol:AAPL", "vol:AAPL"]);
    expect(h.logs).toContain("bridge: no market-data permission for TSX stocks (no TSX subscription?); skipping them for 20 h (TWS 162: Historical Market Data Service error message:No market data permissions for TSE STK)");
    expect(h.status().backfill.failed).toBeGreaterThanOrEqual(1);
    h.bridge.stop();
  });

  it("one refused symbol among served ones is that symbol's problem: the exchange is not denied", async () => {
    const m = twoMarkets();
    const historical = m.historical;
    m.historical = (c, r) => (c.conId === 5551 ? new TwsRequestError(162, "Historical Market Data Service error message:No market data permissions for TSE STK") : historical(c, r));
    const wants = { version: 2, iv_history: [{ symbol: "RY.TO", from: "2026-11-01" }, { symbol: "RCI.B.TO", from: "2026-11-01" }, { symbol: "AP.UN.TO", from: "2026-11-01" }] };
    m.details = ((details) => (c: TwsContract) => (c.symbol === "AP.UN" ? [{ ...c, conId: 5553 }] : details(c)))(m.details);
    const h = harness(wants, m);
    h.bridge.start();
    await vi.advanceTimersByTimeAsync(20_000);
    expect(h.calls.filter((c) => c.startsWith("vol:"))).toEqual(["vol:RY.TO", "vol:AP.UN.TO"]);
    expect(h.logs.filter((l) => l.includes("no market-data permission"))).toEqual([
      "bridge: no market-data permission for RCI.B.TO IV/HV history; skipping it (TWS 162: Historical Market Data Service error message:No market data permissions for TSE STK)"]);
    h.bridge.stop();
  });

  it("live fallback: symbols of a denied exchange are left out, the others are still quoted", async () => {
    const h = harness({ version: 2, underlying_stats: [{ market: "CA", session_date: "2026-11-26", symbols: ["RY.TO"], max_age_s: 1800 }] },
      twoMarkets({ stocks: true }), { live: () => ({ symbols: ["AAPL", "RY.TO"], interval_s: 5 }) });
    h.bridge.start();
    await vi.advanceTimersByTimeAsync(12_000);
    const live = h.quotes.filter((q) => q.quotes.some((x) => x.symbol === "AAPL"));
    expect(live.length).toBeGreaterThanOrEqual(1);
    expect(h.quotes.flatMap((q) => q.quotes.map((x) => x.symbol)).includes("RY.TO")).toBe(false);
    const later = h.session.streams.length;
    await vi.advanceTimersByTimeAsync(5_000);
    expect(h.session.streams.slice(later).map((x) => contractId(x.contract))).toEqual(["AAPL:STK"]);
    h.bridge.stop();
  });

  it("an on-demand refresh of a Canadian symbol runs like a US one, and says so when its options feed is closed", async () => {
    const h = harness({ version: 2, chains_on_demand_max_age_s: 120 }, twoMarkets());
    h.bridge.start();
    await vi.advanceTimersByTimeAsync(2_000);
    const r = h.bridge.refreshSymbol("ry.to");
    await vi.advanceTimersByTimeAsync(5_000);
    expect(await r).toEqual({ status: "refreshed" });
    expect(h.chains[0].symbol).toBe("RY.TO");
    h.bridge.stop();
    // no Montréal Exchange subscription, on a Saturday: the Canadian feed is closed, the US one is not
    vi.setSystemTime(Date.parse("2026-11-28T17:00:00Z"));
    const d = harness({ version: 2, chains_on_demand_max_age_s: 120 }, twoMarkets({ options: true }));
    d.bridge.start();
    await vi.advanceTimersByTimeAsync(2_000);
    const first = d.bridge.refreshSymbol("RY.TO");
    await vi.advanceTimersByTimeAsync(5_000);
    expect(await first).toMatchObject({ status: "unavailable", waitingFor: "market-hours" });
    const us = d.bridge.refreshSymbol("AAPL");
    await vi.advanceTimersByTimeAsync(5_000);
    expect(await us).toEqual({ status: "refreshed" });
    d.bridge.stop();
  });
});

describe("OptionsFeed per market", () => {
  it("keeps each market's state apart", () => {
    const us = new OptionsFeed(null), ca = new OptionsFeed(null);
    ca.refused(new Date("2026-11-28T17:00:00Z"), undefined);
    expect([us.mode, ca.mode]).toEqual(["unknown", "delayed_only"]);
    expect([us.canQuote(new Date("2026-11-28T17:00:00Z")), ca.canQuote(new Date("2026-11-28T17:00:00Z"))]).toEqual([true, false]);
  });
});
