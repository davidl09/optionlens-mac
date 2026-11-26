// Scriptable in-memory TwsSession / TwsConnector for unit tests (no TWS needed).
import type { ContractStore } from "./contract-cache";
import { TwsRequestError, type AccountHandlers, type AccountValue, type ConnectOptions, type HistBar, type HistoricalRequest, type MarketDataKind, type OptionParams, type SecType, type TickEvent, type TickHandlers, type TwsConnector, type TwsContract, type TwsPosition, type TwsSession, type TwsSystemMessage } from "./tws";

export interface FakeMarket {
  /** Ticks delivered (synchronously) when a contract is streamed, keyed by contractId(). */
  ticks: (id: string) => TickEvent[] | undefined;
  /** Errors delivered instead of ticks (`marketDataType`: of the request; undefined = the session default 4). */
  streamErrors: (id: string, marketDataType?: MarketDataKind) => TwsRequestError | undefined;
  details: (c: TwsContract) => TwsContract[];
  optionParams: OptionParams[];
  historical: (c: TwsContract, r: HistoricalRequest) => HistBar[] | Error;
}

/** "AAPL:STK", "AAPL:20261127:100:C"; a contract in another currency carries it in front ("CAD/RY:STK"). */
export const contractId = (c: TwsContract) =>
  `${c.currency === "USD" ? "" : `${c.currency}/`}${c.secType === "OPT" ? `${c.symbol}:${c.lastTradeDateOrContractMonth}:${c.strike}:${c.right}` : `${c.symbol}:${c.secType}`}`;

/** Real TWS rejects a request whose expiry is not YYYYMMDD / YYYYMM (error 321); so does the fake. */
export function twsValidationError(c: TwsContract): TwsRequestError | null {
  const d = c.lastTradeDateOrContractMonth;
  if (d === undefined || /^\d{6}(\d{2})?$/.test(d)) return null;
  return new TwsRequestError(321, "Error validating request.-'bW' : cause - lastTradeDateOrContractMonth: The date entered is invalid. Please use the format YYYYMMDD or YYYYMM");
}

export class FakeSession implements TwsSession {
  readonly streams: { contract: TwsContract; generic: string; cancelled: boolean; marketDataType: MarketDataKind | undefined }[] = [];
  readonly historicalCalls: { contract: TwsContract; req: HistoricalRequest }[] = [];
  /** Every reqSecDefOptParams (option parameters) and reqContractDetails the bridge sent. */
  readonly optionParamsCalls: { symbol: string; secType: SecType; conId: number }[] = [];
  readonly detailsCalls: TwsContract[] = [];
  maxConcurrentStreams = 0;
  /** Outgoing messages per second, like the real API client's queue (null = sent instantly, ticks synchronous). */
  msgRate: number | null = null;
  /** Live subscriptions TWS allows; beyond it a subscription is refused with error 101. */
  lineLimit: number | null = null;
  private open = 0;
  private queueFreeAt = 0;
  private sys: ((m: TwsSystemMessage) => void)[] = [];
  private closers: ((r: string) => void)[] = [];

  constructor(readonly market: FakeMarket, readonly accounts: string[] = ["DU123"]) {}

  async historical(contract: TwsContract, req: HistoricalRequest, _signal?: AbortSignal): Promise<HistBar[]> {
    this.historicalCalls.push({ contract, req });
    const invalid = twsValidationError(contract);
    if (invalid) throw invalid;
    const r = this.market.historical(contract, req);
    if (r instanceof Error) throw r;
    return r;
  }
  async contractDetails(c: TwsContract): Promise<TwsContract[]> {
    this.detailsCalls.push(c);
    const invalid = twsValidationError(c);
    if (invalid) throw invalid;
    return this.market.details(c);
  }
  async optionParams(u: { symbol: string; secType: SecType; conId: number }, _signal?: AbortSignal, _timeoutMs?: number): Promise<OptionParams[]> {
    this.optionParamsCalls.push(u);
    return this.market.optionParams;
  }

  /** Runs `fn` when the message would reach TWS (immediately without msgRate). */
  private send(fn: () => void): void {
    if (this.msgRate === null) { fn(); return; }
    this.queueFreeAt = Math.max(Date.now(), this.queueFreeAt) + 1000 / this.msgRate;
    setTimeout(fn, this.queueFreeAt - Date.now());
  }

  sendBacklogMs(): number {
    return this.msgRate === null ? 0 : Math.max(0, this.queueFreeAt - Date.now());
  }

  stream(contract: TwsContract, generic: string, h: TickHandlers, marketDataType?: MarketDataKind): () => void {
    const rec = { contract, generic, cancelled: false, marketDataType };
    this.streams.push(rec);
    let live = false;
    this.send(() => {
      if (rec.cancelled) return;
      if (this.lineLimit !== null && this.open >= this.lineLimit) { h.onError(new TwsRequestError(101, "Max number of tickers has been reached")); return; }
      live = true;
      this.open += 1;
      this.maxConcurrentStreams = Math.max(this.maxConcurrentStreams, this.open);
      const id = contractId(contract);
      const err = twsValidationError(contract) ?? this.market.streamErrors(id, marketDataType);
      if (err) h.onError(err);
      else for (const t of this.market.ticks(id) ?? []) h.onTick(t);
    });
    return () => {
      if (rec.cancelled) return;
      rec.cancelled = true;
      this.send(() => { if (live) { live = false; this.open -= 1; } });
    };
  }

  /** Read-only account subscriptions the bridge opened, with their handlers (tests emit through them). */
  readonly positionSubs: { h: AccountHandlers<TwsPosition>; cancelled: boolean }[] = [];
  readonly summarySubs: { tags: string; h: AccountHandlers<AccountValue>; cancelled: boolean }[] = [];
  positions(h: AccountHandlers<TwsPosition>): () => void {
    const rec = { h, cancelled: false };
    this.positionSubs.push(rec);
    return () => { rec.cancelled = true; };
  }
  accountSummary(tags: string, h: AccountHandlers<AccountValue>): () => void {
    const rec = { tags, h, cancelled: false };
    this.summarySubs.push(rec);
    return () => { rec.cancelled = true; };
  }

  onSystemMessage(cb: (m: TwsSystemMessage) => void): void { this.sys.push(cb); }
  onClose(cb: (r: string) => void): void { this.closers.push(cb); }
  close(): void { this.emitClose("closed"); }
  emitSystem(m: TwsSystemMessage): void { for (const s of this.sys) s(m); }
  emitClose(reason: string): void { const c = this.closers; this.closers = []; for (const f of c) f(reason); }
}

export class FakeConnector implements TwsConnector {
  readonly attempts: ConnectOptions[] = [];
  /** port → behaviour; missing port = connection refused. */
  constructor(public behaviour: Map<number, FakeSession | TwsRequestError | ((clientId: number) => FakeSession | TwsRequestError)>) {}
  async connect(o: ConnectOptions): Promise<TwsSession> {
    this.attempts.push(o);
    let b = this.behaviour.get(o.port);
    if (typeof b === "function") b = b(o.clientId);
    if (!b) throw new Error(`ECONNREFUSED 127.0.0.1:${o.port}`);
    if (b instanceof TwsRequestError) throw b;
    return b;
  }
}

export function emptyMarket(): FakeMarket {
  return { ticks: () => undefined, streamErrors: () => undefined, details: () => [], optionParams: [], historical: () => [] };
}

/** In-memory ContractStore (what the app keeps in <userData>/contract-cache/<SYMBOL>.json), JSON round-tripped. */
export function memoryContractStore(): ContractStore & { files: Map<string, string> } {
  const files = new Map<string, string>();
  return { files, read: (sym) => (files.has(sym) ? JSON.parse(files.get(sym)!) as unknown : null), write: (sym, v) => { files.set(sym, JSON.stringify(v)); } };
}
