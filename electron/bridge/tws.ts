// The bridge's view of TWS / IB Gateway: a narrow, promise-based contract. The @stoqey/ib adapter
// (ib-adapter.ts) implements it; tests use a fake. Nothing else in the bridge imports @stoqey/ib.

export type SecType = "STK" | "IND" | "OPT";

export interface TwsContract {
  symbol: string;
  secType: SecType;
  exchange: string;
  currency: "USD";
  conId?: number;
  primaryExchange?: string;
  /** OPT: expiry YYYYMMDD. */
  lastTradeDateOrContractMonth?: string;
  strike?: number;
  right?: "C" | "P";
  tradingClass?: string;
  multiplier?: string;
}

export interface HistBar { time: string; open: number; high: number; low: number; close: number; volume: number | null }

export interface OptionParams { exchange: string; tradingClass: string; multiplier: string; expirations: string[]; strikes: number[] }

export type WhatToShow = "OPTION_IMPLIED_VOLATILITY" | "HISTORICAL_VOLATILITY" | "TRADES";

export interface HistoricalRequest { duration: string; whatToShow: WhatToShow; useRth: boolean }

/** A market-data event for one subscription. Field numbers are IBKR tick types. */
export type TickEvent =
  | { kind: "price"; field: number; value: number }
  | { kind: "size"; field: number; value: number }
  | { kind: "generic"; field: number; value: number }
  | { kind: "option"; field: number; iv?: number; delta?: number; gamma?: number; vega?: number; theta?: number; undPrice?: number }
  | { kind: "string"; field: number; value: string };

export interface TickHandlers { onTick(e: TickEvent): void; onError(e: TwsRequestError): void }

/** A request-level error from TWS (e.g. 200 no security definition, 162 pacing violation). */
export class TwsRequestError extends Error {
  constructor(readonly code: number, message: string) { super(`TWS ${code}: ${message}`); this.name = "TwsRequestError"; }
}

/** TWS did not answer a request in time (a bridge-side timeout, not an IBKR error code). */
export class TwsTimeoutError extends Error {
  constructor(what: string, ms: number) { super(`${what}: no answer from TWS within ${Math.round(ms / 1000)} s`); this.name = "TwsTimeoutError"; }
}

/** A connection-level message (reqId -1): 1100 lost, 1101/1102 restored, 10197 competing session, 21xx farm status. */
export interface TwsSystemMessage { code: number; message: string }

export interface TwsSession {
  readonly accounts: string[];
  historical(contract: TwsContract, req: HistoricalRequest, signal: AbortSignal): Promise<HistBar[]>;
  contractDetails(contract: TwsContract, signal: AbortSignal): Promise<TwsContract[]>;
  /** `timeoutMs`: how long to wait for the answer (the adapter's default when omitted). */
  optionParams(underlying: { symbol: string; secType: SecType; conId: number }, signal: AbortSignal, timeoutMs?: number): Promise<OptionParams[]>;
  /** Streams market data; returns a cancel function. Never throws for bad ticks. */
  stream(contract: TwsContract, genericTicks: string, handlers: TickHandlers): () => void;
  /**
   * Estimated time (ms) until every message issued so far has actually been sent: the API client paces
   * outgoing messages (requests and cancels alike), so a request issued now reaches TWS this much later.
   */
  sendBacklogMs(): number;
  onSystemMessage(cb: (m: TwsSystemMessage) => void): void;
  onClose(cb: (reason: string) => void): void;
  close(): void;
}

export interface ConnectOptions { host: string; port: number; clientId: number; timeoutMs: number }

export interface TwsConnector {
  connect(o: ConnectOptions): Promise<TwsSession>;
}

// ---- error classification (IBKR message codes) ----
export const CODE = {
  MAX_TICKERS: 101,                // market-data line limit reached: the subscription was refused
  NO_SECURITY_DEFINITION: 200,
  HMDS_ERROR: 162,                 // pacing violation, or "query returned no data", or competing session
  MKT_DATA_NOT_SUBSCRIBED: 354,
  CLIENT_ID_IN_USE: 326,
  CONNECTIVITY_LOST: 1100,
  RESTORED_DATA_LOST: 1101,
  RESTORED_DATA_KEPT: 1102,
  COMPETING_SESSION: 10197,
  NOT_CONNECTED: 504,
} as const;

/** "Not subscribed" / "additional subscription required" / "delayed not enabled": the contract gets no data. */
export const NOT_SUBSCRIBED_CODES = new Set<number>([354, 10089, 10090, 10168]);

/** Historical pacing violation (back off and retry). */
export function isPacingError(e: unknown): boolean {
  if (!(e instanceof TwsRequestError)) return false;
  if (e.code === CODE.HMDS_ERROR) return /pacing/i.test(e.message);
  return e.code === 420;
}

/**
 * The account lacks the market-data permission for this contract / data type (e.g. 162 "No market data
 * permissions for NYSE STK"): every similar request fails the same way, so stop asking.
 */
export function isPermissionError(e: unknown): boolean {
  if (!(e instanceof TwsRequestError)) return false;
  if (NOT_SUBSCRIBED_CODES.has(e.code)) return true;
  return e.code === CODE.HMDS_ERROR && /permission|not subscribed|subscription/i.test(e.message);
}

/** Historical request that legitimately has no data (do not retry). */
export function isNoDataError(e: unknown): boolean {
  return e instanceof TwsRequestError && (e.code === CODE.NO_SECURITY_DEFINITION
    || (e.code === CODE.HMDS_ERROR && /no data|returned no|no historical/i.test(e.message)));
}

/** The user is logged in elsewhere with the same username (market data / HMDS unavailable). */
export function isCompetingSession(e: unknown): boolean {
  return e instanceof TwsRequestError && (e.code === CODE.COMPETING_SESSION || /competing|different ip address/i.test(e.message));
}
