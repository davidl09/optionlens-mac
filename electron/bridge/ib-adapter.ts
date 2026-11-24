// TwsConnector implemented with @stoqey/ib (IBApi, the event-based TWS API port). This is the only file
// that knows the library; it maps request ids to promises/handlers, applies per-request timeouts and
// shields the bridge from listener exceptions (a bad tick never crashes the process).
import { BarSizeSetting, EventName, IBApi, isNonFatalError, MarketDataType, type Contract, type ContractDetails, type SecType as IbSecType } from "@stoqey/ib";
import { CODE, TwsRequestError, TwsTimeoutError, type AccountHandlers, type AccountValue, type ConnectOptions, type HistBar, type HistoricalRequest, type MarketDataKind, type OptionParams, type SecType, type TickEvent, type TickHandlers, type TwsConnector, type TwsContract, type TwsPosition, type TwsSession, type TwsSystemMessage } from "./tws";

/** Outgoing messages per second: @stoqey/ib queues every request and cancel and sends at this rate. */
const MSG_RATE = 40;
const HIST_TIMEOUT_MS = 120_000;
const DETAILS_TIMEOUT_MS = 60_000;
/** Option contract details of one expiry can be thousands of contracts (SPXW): TWS takes much longer. */
const OPTION_DETAILS_TIMEOUT_MS = 180_000;
/** After nextValidId, how long to wait for managedAccounts (TWS may send it just after). */
const ACCOUNTS_GRACE_MS = 1500;

/** The only expiry / contract-month formats TWS accepts in a request (it rejects anything else with error 321). */
const TWS_DATE = /^\d{6}(\d{2})?$/;

/**
 * Request contract. A contract IBKR already resolved (it has a conId) is addressed by conId + exchange only:
 * echoing contract-details fields back (expiry with a time suffix, multiplier, trading class) only gives TWS
 * something to reject. Throws for an expiry TWS would reject (a bridge bug, never sent).
 */
export function toIb(c: TwsContract): Contract {
  if (c.conId) return { conId: c.conId, exchange: c.exchange };
  const date = c.lastTradeDateOrContractMonth;
  if (date !== undefined && !TWS_DATE.test(date)) throw new Error(`${c.symbol}: invalid TWS expiry "${date}" (YYYYMMDD expected)`);
  return {
    symbol: c.symbol, secType: c.secType as IbSecType, exchange: c.exchange, currency: c.currency,
    primaryExch: c.primaryExchange, lastTradeDateOrContractMonth: date, strike: c.strike,
    right: c.right as Contract["right"], tradingClass: c.tradingClass, multiplier: c.multiplier === undefined ? undefined : Number(c.multiplier),
  };
}

/**
 * Expiry YYYYMMDD of a contract-details answer. TWS may send it with a time and zone ("20261218 16:00:00
 * US/Eastern"), which @stoqey/ib 1.6.13 does not split off (its decoder splits on the literal string "\\s+");
 * newer servers also send `lastTradeDate`. Only the leading date is kept.
 */
export function detailsExpiry(raw: { lastTradeDateOrContractMonth?: string; lastTradeDate?: string }): string | undefined {
  for (const v of [raw.lastTradeDateOrContractMonth, raw.lastTradeDate]) {
    const m = /^\s*(\d{8}|\d{6})(?!\d)/.exec(String(v ?? ""));
    if (m) return m[1];
  }
  return undefined;
}

function fromIb(d: ContractDetails): TwsContract | null {
  const c = d.contract;
  if (!c?.symbol || !c.secType) return null;
  const secType = String(c.secType) as SecType;
  if (secType !== "STK" && secType !== "IND" && secType !== "OPT") return null;
  const right = String(c.right ?? "").toUpperCase().startsWith("C") ? "C" : String(c.right ?? "").toUpperCase().startsWith("P") ? "P" : undefined;
  return {
    symbol: c.symbol, secType, exchange: c.exchange ?? "SMART", currency: "USD", conId: c.conId, primaryExchange: c.primaryExch,
    lastTradeDateOrContractMonth: detailsExpiry(c as { lastTradeDateOrContractMonth?: string; lastTradeDate?: string }), strike: c.strike, right, tradingClass: c.tradingClass,
    multiplier: c.multiplier === undefined ? undefined : String(c.multiplier),
  };
}

/** A position event's contract as the bridge's TwsPosition (expiry YYYYMMDD, right C / P). */
export function positionOf(account: string, c: Contract, pos: number, avgCost: number | undefined): TwsPosition {
  const r = String(c.right ?? "").toUpperCase();
  const num = (v: unknown) => (v === undefined || v === null || v === "" || !Number.isFinite(Number(v)) ? null : Number(v));
  return {
    account: String(account), pos: Number(pos), avgCost: num(avgCost),
    contract: {
      symbol: String(c.symbol ?? ""), secType: String(c.secType ?? ""), currency: c.currency ? String(c.currency) : null, conId: num(c.conId),
      expiry: detailsExpiry(c as { lastTradeDateOrContractMonth?: string; lastTradeDate?: string }) ?? null, strike: num(c.strike),
      right: r.startsWith("C") ? "C" : r.startsWith("P") ? "P" : null, multiplier: num(c.multiplier), tradingClass: c.tradingClass ? String(c.tradingClass) : null,
    },
  };
}

interface Pending { onError(e: TwsRequestError): void }

class IbSession implements TwsSession {
  private nextId = 1;
  private readonly pending = new Map<number, Pending>();
  private readonly ticks = new Map<number, TickHandlers>();
  private readonly systemListeners: ((m: TwsSystemMessage) => void)[] = [];
  private readonly closeListeners: ((reason: string) => void)[] = [];
  private closed = false;
  /** When the client's outgoing queue (paced at MSG_RATE) is estimated to be empty. */
  private queueFreeAt = 0;
  /** The market data type last sent (reqMarketDataType applies to the requests after it). */
  private dataType: MarketDataKind = 4;   // set at connect (MarketDataType.DELAYED_FROZEN)

  /** `early`: connection-level messages received during the handshake (farm status), replayed to each listener. */
  constructor(private readonly ib: IBApi, readonly accounts: string[], private readonly early: TwsSystemMessage[] = []) {
    ib.on(EventName.error, (err: Error, code: number, reqId: number) => this.safe(() => this.onError(err, code, reqId)));
    // @stoqey/ib 1.6.13 emits every TWS message with reqId -1 (farm status 21xx, 1100/1101/1102 connectivity) as
    // `info`, not `error`.
    ib.on(EventName.info, (message: string, code: number) => this.safe(() => this.onSystem({ code, message: String(message) })));
    ib.on(EventName.disconnected, () => this.safe(() => this.onDisconnected("connection closed")));
    const tick = (reqId: number, e: TickEvent) => {
      const h = this.ticks.get(reqId);
      if (h) this.safe(() => h.onTick(e));
    };
    ib.on(EventName.tickPrice, (reqId: number, field: number, value: number) => tick(reqId, { kind: "price", field, value }));
    ib.on(EventName.tickSize, (reqId: number, field?: number, value?: number) => {
      if (field !== undefined && value !== undefined) tick(reqId, { kind: "size", field, value: Number(value) });
    });
    ib.on(EventName.tickGeneric, (reqId: number, field: number, value: number) => tick(reqId, { kind: "generic", field, value }));
    ib.on(EventName.tickString, (reqId: number, field: number, value: string) => tick(reqId, { kind: "string", field, value: String(value) }));
    ib.on(EventName.tickOptionComputation, (reqId: number, field: number, _attrib: number | undefined, iv?: number, delta?: number, _optPrice?: number,
      _pvDiv?: number, gamma?: number, vega?: number, theta?: number, undPrice?: number) => tick(reqId, { kind: "option", field, iv, delta, gamma, vega, theta, undPrice }));
  }

  /** Issues one outgoing message (the library queues it) and books its slot in the send-queue estimate. */
  private send(fn: () => void): void {
    this.queueFreeAt = Math.max(Date.now(), this.queueFreeAt) + 1000 / MSG_RATE;
    fn();
  }

  sendBacklogMs(): number {
    return Math.max(0, this.queueFreeAt - Date.now());
  }

  private safe(fn: () => void): void {
    try { fn(); } catch { /* listener bugs must not take down the main process */ }
  }

  private onError(err: Error, code: number, reqId: number): void {
    const message = err?.message ?? String(err);
    if (reqId === undefined || reqId < 0) { this.onSystem({ code, message }); return; }
    if (code === CODE.COMPETING_SESSION) for (const l of this.systemListeners) this.safe(() => l({ code, message }));
    if (isNonFatalError(code, err)) return;
    const e = new TwsRequestError(code, message);
    this.pending.get(reqId)?.onError(e);
    this.ticks.get(reqId)?.onError(e);
  }

  private onSystem(m: TwsSystemMessage): void {
    for (const l of this.systemListeners) this.safe(() => l(m));
    if (m.code === CODE.NOT_CONNECTED) this.onDisconnected("not connected");
  }

  private onDisconnected(reason: string): void {
    if (this.closed) return;
    this.closed = true;
    const e = new TwsRequestError(CODE.NOT_CONNECTED, reason);
    for (const p of [...this.pending.values()]) p.onError(e);
    this.pending.clear();
    this.ticks.clear();
    for (const l of this.closeListeners) this.safe(() => l(reason));
    this.ib.removeAllListeners();
  }

  /** Runs one request/response exchange with timeout, abort and error routing. */
  private request<T>(what: string, signal: AbortSignal, timeoutMs: number, start: (reqId: number, done: (v: T) => void) => void, cancel?: (reqId: number) => void): Promise<T> {
    if (this.closed) return Promise.reject(new TwsRequestError(CODE.NOT_CONNECTED, "not connected"));
    const reqId = this.nextId++;
    return new Promise<T>((resolve, reject) => {
      const finish = (fn: () => void) => {
        clearTimeout(timer);
        signal.removeEventListener("abort", onAbort);
        this.pending.delete(reqId);
        fn();
      };
      const onAbort = () => finish(() => { try { if (cancel) this.send(() => cancel(reqId)); } catch { /* ignore */ } reject(signal.reason instanceof Error ? signal.reason : new Error("cancelled")); });
      const timer = setTimeout(() => finish(() => { try { if (cancel) this.send(() => cancel(reqId)); } catch { /* ignore */ } reject(new TwsTimeoutError(what, timeoutMs)); }), timeoutMs);
      if (signal.aborted) { onAbort(); return; }
      signal.addEventListener("abort", onAbort, { once: true });
      this.pending.set(reqId, { onError: (e) => finish(() => reject(e)) });
      this.send(() => start(reqId, (v) => finish(() => resolve(v))));
    });
  }

  async historical(contract: TwsContract, req: HistoricalRequest, signal: AbortSignal): Promise<HistBar[]> {
    const ibc = toIb(contract);
    const bars: HistBar[] = [];
    let listener: ((...a: unknown[]) => void) | null = null;
    const p = this.request<HistBar[]>(`${contract.symbol} historical ${req.whatToShow}`, signal, HIST_TIMEOUT_MS, (reqId, done) => {
      listener = (id: unknown, time: unknown, open: unknown, high: unknown, low: unknown, close: unknown, volume: unknown) => {
        if (id !== reqId) return;
        if (typeof time === "string" && time.startsWith("finished")) { done(bars); return; }
        bars.push({ time: String(time), open: Number(open), high: Number(high), low: Number(low), close: Number(close), volume: Number.isFinite(Number(volume)) && Number(volume) >= 0 ? Number(volume) : null });
      };
      this.ib.on(EventName.historicalData, listener);
      const size = (req.barSize ?? BarSizeSetting.DAYS_ONE) as BarSizeSetting;
      this.ib.reqHistoricalData(reqId, ibc, req.endDateTime ?? "", req.duration, size, req.whatToShow as never, req.useRth ? 1 : 0, size === BarSizeSetting.DAYS_ONE ? 1 : 2, false);
    }, (reqId) => this.ib.cancelHistoricalData(reqId));
    return p.finally(() => { if (listener) this.ib.off(EventName.historicalData, listener); });
  }

  async contractDetails(contract: TwsContract, signal: AbortSignal): Promise<TwsContract[]> {
    const ibc = toIb(contract);
    const out: TwsContract[] = [];
    let onDetail: ((id: number, d: ContractDetails) => void) | null = null;
    let onEnd: ((id: number) => void) | null = null;
    const timeout = contract.secType === "OPT" ? OPTION_DETAILS_TIMEOUT_MS : DETAILS_TIMEOUT_MS;
    const what = `${contract.symbol} ${contract.secType} contract details${contract.lastTradeDateOrContractMonth ? ` ${contract.lastTradeDateOrContractMonth}` : ""}`;
    const p = this.request<TwsContract[]>(what, signal, timeout, (reqId, done) => {
      onDetail = (id, d) => { if (id === reqId) { const c = fromIb(d); if (c) out.push(c); } };
      onEnd = (id) => { if (id === reqId) done(out); };
      this.ib.on(EventName.contractDetails, onDetail);
      this.ib.on(EventName.contractDetailsEnd, onEnd);
      this.ib.reqContractDetails(reqId, ibc);
    });
    return p.finally(() => {
      if (onDetail) this.ib.off(EventName.contractDetails, onDetail);
      if (onEnd) this.ib.off(EventName.contractDetailsEnd, onEnd);
    });
  }

  optionParams(u: { symbol: string; secType: SecType; conId: number }, signal: AbortSignal, timeoutMs = DETAILS_TIMEOUT_MS): Promise<OptionParams[]> {
    const out: OptionParams[] = [];
    let onParam: ((...a: unknown[]) => void) | null = null;
    let onEnd: ((id: number) => void) | null = null;
    const p = this.request<OptionParams[]>(`${u.symbol} option parameters`, signal, timeoutMs, (reqId, done) => {
      onParam = (id, exchange, _conId, tradingClass, multiplier, expirations, strikes) => {
        if (id !== reqId) return;
        out.push({ exchange: String(exchange), tradingClass: String(tradingClass), multiplier: String(multiplier),
          expirations: Array.isArray(expirations) ? expirations.map(String) : [], strikes: Array.isArray(strikes) ? strikes.map(Number).filter(Number.isFinite) : [] });
      };
      onEnd = (id) => { if (id === reqId) done(out); };
      this.ib.on(EventName.securityDefinitionOptionParameter, onParam);
      this.ib.on(EventName.securityDefinitionOptionParameterEnd, onEnd);
      this.ib.reqSecDefOptParams(reqId, u.symbol, "", u.secType, u.conId);
    });
    return p.finally(() => {
      if (onParam) this.ib.off(EventName.securityDefinitionOptionParameter, onParam);
      if (onEnd) this.ib.off(EventName.securityDefinitionOptionParameterEnd, onEnd);
    });
  }

  stream(contract: TwsContract, genericTicks: string, handlers: TickHandlers, marketDataType: MarketDataKind = 4): () => void {
    if (this.closed) { handlers.onError(new TwsRequestError(CODE.NOT_CONNECTED, "not connected")); return () => undefined; }
    const ibc = toIb(contract);
    const reqId = this.nextId++;
    this.ticks.set(reqId, handlers);
    if (marketDataType !== this.dataType) {
      this.dataType = marketDataType;
      this.send(() => this.ib.reqMarketDataType(marketDataType as MarketDataType));
    }
    this.send(() => this.ib.reqMktData(reqId, ibc, genericTicks, false, false));
    return () => {
      if (!this.ticks.delete(reqId) || this.closed) return;
      try { this.send(() => this.ib.cancelMktData(reqId)); } catch { /* ignore */ }
    };
  }

  positions(h: AccountHandlers<TwsPosition>): () => void {
    if (this.closed) { h.onError(new TwsRequestError(CODE.NOT_CONNECTED, "not connected")); return () => undefined; }
    const onPos = (account: string, c: Contract, pos: number, avgCost?: number) => this.safe(() => h.onData(positionOf(account, c, pos, avgCost)));
    const onEnd = () => this.safe(() => h.onEnd());
    this.ib.on(EventName.position, onPos);
    this.ib.on(EventName.positionEnd, onEnd);
    this.send(() => this.ib.reqPositions());
    let open = true;
    return () => {
      if (!open) return;
      open = false;
      this.ib.off(EventName.position, onPos);
      this.ib.off(EventName.positionEnd, onEnd);
      if (!this.closed) try { this.send(() => this.ib.cancelPositions()); } catch { /* ignore */ }
    };
  }

  accountSummary(tags: string, h: AccountHandlers<AccountValue>): () => void {
    if (this.closed) { h.onError(new TwsRequestError(CODE.NOT_CONNECTED, "not connected")); return () => undefined; }
    const reqId = this.nextId++;
    const onValue = (id: number, account: string, tag: string, value: string, currency: string) => {
      if (id === reqId) this.safe(() => h.onData({ account: String(account), tag: String(tag), value: String(value), currency: String(currency ?? "") }));
    };
    const onEnd = (id: number) => { if (id === reqId) this.safe(() => h.onEnd()); };
    this.ib.on(EventName.accountSummary, onValue);
    this.ib.on(EventName.accountSummaryEnd, onEnd);
    this.pending.set(reqId, { onError: (e) => this.safe(() => h.onError(e)) });
    this.send(() => this.ib.reqAccountSummary(reqId, "All", tags));
    let open = true;
    return () => {
      if (!open) return;
      open = false;
      this.pending.delete(reqId);
      this.ib.off(EventName.accountSummary, onValue);
      this.ib.off(EventName.accountSummaryEnd, onEnd);
      if (!this.closed) try { this.send(() => this.ib.cancelAccountSummary(reqId)); } catch { /* ignore */ }
    };
  }

  onSystemMessage(cb: (m: TwsSystemMessage) => void): void {
    this.systemListeners.push(cb);
    for (const m of this.early) this.safe(() => cb(m));
  }
  onClose(cb: (reason: string) => void): void { this.closeListeners.push(cb); }

  close(): void {
    if (this.closed) return;
    try { this.ib.disconnect(); } catch { /* ignore */ }
    this.onDisconnected("closed by the app");
  }
}

export const ibConnector: TwsConnector = {
  connect(o: ConnectOptions): Promise<TwsSession> {
    const ib = new IBApi({ host: o.host, port: o.port, maxReqPerSec: MSG_RATE });
    return new Promise<TwsSession>((resolve, reject) => {
      let accounts: string[] | null = null;
      const early: TwsSystemMessage[] = [];
      let gotId = false;
      let settled = false;
      let grace: ReturnType<typeof setTimeout> | null = null;
      const stopTimers = () => { clearTimeout(timer); if (grace) clearTimeout(grace); };
      const fail = (e: Error) => {
        if (settled) return;
        settled = true;
        stopTimers();
        ib.removeAllListeners();
        try { ib.disconnect(); } catch { /* ignore */ }
        reject(e);
      };
      // Ready once both nextValidId and managedAccounts arrived (in either order), or ACCOUNTS_GRACE_MS
      // after nextValidId without accounts.
      const ready = () => {
        if (settled) return;
        settled = true;
        stopTimers();
        ib.removeAllListeners();
        // Delayed-frozen: TWS returns real-time data where the account has the subscription (and the
        // frozen closing snapshot after hours), and falls back to 15-min delayed data per contract
        // class where it does not (e.g. no OPRA). Delayed ticks use their own tick ids, which the
        // accumulator maps and flags, so every push says whether it was delayed.
        ib.reqMarketDataType(MarketDataType.DELAYED_FROZEN);
        resolve(new IbSession(ib, accounts ?? [], early));
      };
      const timer = setTimeout(() => fail(new Error(`no API handshake on port ${o.port}`)), o.timeoutMs);
      ib.on(EventName.managedAccounts, (list: string) => {
        accounts = String(list).split(",").map((a) => a.trim()).filter(Boolean);
        if (gotId) ready();
      });
      const onSystem = (code: number, message: string) => {
        if (settled) return;
        if (code === CODE.CLIENT_ID_IN_USE || code === 502 || code === CODE.NOT_CONNECTED) fail(new TwsRequestError(code, message));
        else early.push({ code, message });
      };
      ib.on(EventName.error, (err: Error, code: number, reqId: number) => {
        if (reqId === undefined || reqId < 0) onSystem(code, err?.message ?? "connect failed");
      });
      ib.on(EventName.info, (message: string, code: number) => onSystem(code, String(message)));
      ib.on(EventName.disconnected, () => fail(new Error(`connection to port ${o.port} closed`)));
      ib.on(EventName.nextValidId, () => {
        if (settled || gotId) return;
        gotId = true;
        if (accounts !== null) ready();
        else grace = setTimeout(ready, ACCOUNTS_GRACE_MS);
      });
      try { ib.connect(o.clientId); } catch (e) { fail(e as Error); }
    });
  },
};
