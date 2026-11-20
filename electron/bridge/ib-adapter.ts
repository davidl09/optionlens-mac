// TwsConnector implemented with @stoqey/ib (IBApi, the event-based TWS API port). This is the only file
// that knows the library; it maps request ids to promises/handlers, applies per-request timeouts and
// shields the bridge from listener exceptions (a bad tick never crashes the process).
import { BarSizeSetting, EventName, IBApi, isNonFatalError, MarketDataType, type Contract, type ContractDetails, type SecType as IbSecType } from "@stoqey/ib";
import { CODE, TwsRequestError, type ConnectOptions, type HistBar, type OptionParams, type SecType, type TickEvent, type TickHandlers, type TwsConnector, type TwsContract, type TwsSession, type TwsSystemMessage } from "./tws";

/** Outgoing messages per second: @stoqey/ib queues every request and cancel and sends at this rate. */
const MSG_RATE = 40;
const HIST_TIMEOUT_MS = 120_000;
const DETAILS_TIMEOUT_MS = 60_000;

function toIb(c: TwsContract): Contract {
  return {
    symbol: c.symbol, secType: c.secType as IbSecType, exchange: c.exchange, currency: c.currency, conId: c.conId,
    primaryExch: c.primaryExchange, lastTradeDateOrContractMonth: c.lastTradeDateOrContractMonth, strike: c.strike,
    right: c.right as Contract["right"], tradingClass: c.tradingClass, multiplier: c.multiplier === undefined ? undefined : Number(c.multiplier),
  };
}

function fromIb(d: ContractDetails): TwsContract | null {
  const c = d.contract;
  if (!c?.symbol || !c.secType) return null;
  const secType = String(c.secType) as SecType;
  if (secType !== "STK" && secType !== "IND" && secType !== "OPT") return null;
  const right = String(c.right ?? "").toUpperCase().startsWith("C") ? "C" : String(c.right ?? "").toUpperCase().startsWith("P") ? "P" : undefined;
  return {
    symbol: c.symbol, secType, exchange: c.exchange ?? "SMART", currency: "USD", conId: c.conId, primaryExchange: c.primaryExch,
    lastTradeDateOrContractMonth: c.lastTradeDateOrContractMonth, strike: c.strike, right, tradingClass: c.tradingClass,
    multiplier: c.multiplier === undefined ? undefined : String(c.multiplier),
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

  constructor(private readonly ib: IBApi, readonly accounts: string[]) {
    ib.on(EventName.error, (err: Error, code: number, reqId: number) => this.safe(() => this.onError(err, code, reqId)));
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
    if (reqId === undefined || reqId < 0) {
      for (const l of this.systemListeners) this.safe(() => l({ code, message }));
      if (code === CODE.NOT_CONNECTED) this.onDisconnected("not connected");
      return;
    }
    if (code === CODE.COMPETING_SESSION) for (const l of this.systemListeners) this.safe(() => l({ code, message }));
    if (isNonFatalError(code, err)) return;
    const e = new TwsRequestError(code, message);
    this.pending.get(reqId)?.onError(e);
    this.ticks.get(reqId)?.onError(e);
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
  private request<T>(signal: AbortSignal, timeoutMs: number, start: (reqId: number, done: (v: T) => void) => void, cancel?: (reqId: number) => void): Promise<T> {
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
      const timer = setTimeout(() => finish(() => { try { if (cancel) this.send(() => cancel(reqId)); } catch { /* ignore */ } reject(new TwsRequestError(CODE.NO_HIST_QUERY, "request timed out")); }), timeoutMs);
      if (signal.aborted) { onAbort(); return; }
      signal.addEventListener("abort", onAbort, { once: true });
      this.pending.set(reqId, { onError: (e) => finish(() => reject(e)) });
      this.send(() => start(reqId, (v) => finish(() => resolve(v))));
    });
  }

  historical(contract: TwsContract, req: { duration: string; whatToShow: string; useRth: boolean }, signal: AbortSignal): Promise<HistBar[]> {
    const bars: HistBar[] = [];
    let listener: ((...a: unknown[]) => void) | null = null;
    const p = this.request<HistBar[]>(signal, HIST_TIMEOUT_MS, (reqId, done) => {
      listener = (id: unknown, time: unknown, open: unknown, high: unknown, low: unknown, close: unknown, volume: unknown) => {
        if (id !== reqId) return;
        if (typeof time === "string" && time.startsWith("finished")) { done(bars); return; }
        bars.push({ time: String(time), open: Number(open), high: Number(high), low: Number(low), close: Number(close), volume: Number.isFinite(Number(volume)) && Number(volume) >= 0 ? Number(volume) : null });
      };
      this.ib.on(EventName.historicalData, listener);
      this.ib.reqHistoricalData(reqId, toIb(contract), "", req.duration, BarSizeSetting.DAYS_ONE, req.whatToShow as never, req.useRth ? 1 : 0, 1, false);
    }, (reqId) => this.ib.cancelHistoricalData(reqId));
    return p.finally(() => { if (listener) this.ib.off(EventName.historicalData, listener); });
  }

  contractDetails(contract: TwsContract, signal: AbortSignal): Promise<TwsContract[]> {
    const out: TwsContract[] = [];
    let onDetail: ((id: number, d: ContractDetails) => void) | null = null;
    let onEnd: ((id: number) => void) | null = null;
    const p = this.request<TwsContract[]>(signal, DETAILS_TIMEOUT_MS, (reqId, done) => {
      onDetail = (id, d) => { if (id === reqId) { const c = fromIb(d); if (c) out.push(c); } };
      onEnd = (id) => { if (id === reqId) done(out); };
      this.ib.on(EventName.contractDetails, onDetail);
      this.ib.on(EventName.contractDetailsEnd, onEnd);
      this.ib.reqContractDetails(reqId, toIb(contract));
    });
    return p.finally(() => {
      if (onDetail) this.ib.off(EventName.contractDetails, onDetail);
      if (onEnd) this.ib.off(EventName.contractDetailsEnd, onEnd);
    });
  }

  optionParams(u: { symbol: string; secType: SecType; conId: number }, signal: AbortSignal): Promise<OptionParams[]> {
    const out: OptionParams[] = [];
    let onParam: ((...a: unknown[]) => void) | null = null;
    let onEnd: ((id: number) => void) | null = null;
    const p = this.request<OptionParams[]>(signal, DETAILS_TIMEOUT_MS, (reqId, done) => {
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

  stream(contract: TwsContract, genericTicks: string, handlers: TickHandlers): () => void {
    if (this.closed) { handlers.onError(new TwsRequestError(CODE.NOT_CONNECTED, "not connected")); return () => undefined; }
    const reqId = this.nextId++;
    this.ticks.set(reqId, handlers);
    this.send(() => this.ib.reqMktData(reqId, toIb(contract), genericTicks, false, false));
    return () => {
      if (!this.ticks.delete(reqId) || this.closed) return;
      try { this.send(() => this.ib.cancelMktData(reqId)); } catch { /* ignore */ }
    };
  }

  onSystemMessage(cb: (m: TwsSystemMessage) => void): void { this.systemListeners.push(cb); }
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
      let accounts: string[] = [];
      let settled = false;
      const fail = (e: Error) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        ib.removeAllListeners();
        try { ib.disconnect(); } catch { /* ignore */ }
        reject(e);
      };
      const timer = setTimeout(() => fail(new Error(`no API handshake on port ${o.port}`)), o.timeoutMs);
      ib.on(EventName.managedAccounts, (list: string) => { accounts = String(list).split(",").map((a) => a.trim()).filter(Boolean); });
      ib.on(EventName.error, (err: Error, code: number, reqId: number) => {
        if (settled || (reqId !== undefined && reqId >= 0)) return;
        if (code === CODE.CLIENT_ID_IN_USE || code === 502 || code === CODE.NOT_CONNECTED) fail(new TwsRequestError(code, err?.message ?? "connect failed"));
      });
      ib.on(EventName.disconnected, () => fail(new Error(`connection to port ${o.port} closed`)));
      ib.on(EventName.nextValidId, () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        ib.removeAllListeners();
        // Delayed-frozen: TWS returns real-time data where the account has the subscription (and the
        // frozen closing snapshot after hours), and falls back to 15-min delayed data per contract
        // class where it does not (e.g. no OPRA). Delayed ticks use their own tick ids, which the
        // accumulator maps and flags, so every push says whether it was delayed.
        ib.reqMarketDataType(MarketDataType.DELAYED_FROZEN);
        resolve(new IbSession(ib, accounts));
      });
      try { ib.connect(o.clientId); } catch (e) { fail(e as Error); }
    });
  },
};
