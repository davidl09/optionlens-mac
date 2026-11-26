// Saved contract definitions per underlying, persisted in the app's data dir (one small JSON file per engine symbol:
// AAPL, RY.TO): the underlying's conId, the
// option parameters (reqSecDefOptParams) and, per expiry, the option contracts IBKR resolved (contract details).
// The parameters are re-used for the New York day (listed expiries and strikes change at most daily; on a busy
// Gateway the request can take a minute). Everything saved is the fallback when IBKR's contract (sec-def) server
// is unavailable: up to SAVED_MAX_AGE_MS old, expired expiries dropped (newly listed ones are then missing).
import { optionRows, type ExpiryPick } from "./chain";
import { listingOf } from "./markets";
import { nyDate, nyYmd } from "./ny";
import type { OptionParams, SecType, TwsContract } from "./tws";

export const SAVED_MAX_AGE_MS = 7 * 86_400_000;

export interface ContractStore { read(symbol: string): unknown; write(symbol: string, v: unknown): void }

interface SavedExpiry { tradingClass: string; multiplier: string; fetchedAt: string; contracts: [number, number, "C" | "P"][] }
interface Entry { v: 1; symbol: string; secType: SecType; conId: number; paramsAt: string | null; params: OptionParams[]; expiries: Record<string, SavedExpiry> }

/** A usable saved contract list: the underlying's conId and the unexpired expiries' contracts, oldest list first. */
export interface SavedContracts {
  conId: number;
  expiries: { pick: ExpiryPick; fetchedAt: string; contracts: TwsContract[] }[];
}

function isEntry(v: unknown): v is Entry {
  const e = v as Entry | null;
  return !!e && e.v === 1 && typeof e.conId === "number" && Array.isArray(e.params) && !!e.expiries && typeof e.expiries === "object";
}

export class ContractCache {
  /** Parameters per symbol (the day cache); contract lists are read from the store when needed. */
  private readonly params = new Map<string, { conId: number; paramsAt: string | null; params: OptionParams[] } | null>();
  /** Symbols IBKR's contract server knew no listing for, in this run (not asked again; a restart asks afresh). */
  private readonly unknown = new Set<string>();

  constructor(private readonly store: ContractStore | null) {}

  private read(symbol: string): Entry | null {
    try {
      const v = this.store?.read(symbol);
      return isEntry(v) ? v : null;
    } catch { return null; }
  }

  /** Today's (New York day) option parameters of the underlying `conId`, else null. */
  todaysParams(symbol: string, conId: number, now: Date): OptionParams[] | null {
    if (!this.params.has(symbol)) {
      const e = this.read(symbol);
      this.params.set(symbol, e ? { conId: e.conId, paramsAt: e.paramsAt, params: e.params } : null);
    }
    const p = this.params.get(symbol);
    return p && p.conId === conId && p.paramsAt !== null && nyDate(new Date(p.paramsAt)) === nyDate(now) && p.params.length ? p.params : null;
  }

  /** The underlying's conId as IBKR resolved it before (it never changes), else null. */
  conId(symbol: string): number | null {
    return this.params.get(symbol)?.conId ?? this.read(symbol)?.conId ?? null;
  }

  /** IBKR knows no listing of `symbol` (asked in this run). */
  isUnknown(symbol: string): boolean { return this.unknown.has(symbol); }
  markUnknown(symbol: string): void { this.unknown.add(symbol); }

  /** Records the underlying's resolution alone (no option definitions asked): kept unless the symbol is saved already. */
  rememberUnderlying(symbol: string, secType: SecType, conId: number): void {
    if (this.conId(symbol) === conId) return;
    const entry: Entry = { v: 1, symbol, secType, conId, paramsAt: null, params: [], expiries: {} };
    this.params.set(symbol, { conId, paramsAt: null, params: [] });
    try { this.store?.write(symbol, entry); } catch { /* not saved: resolved again next launch */ }
  }

  /**
   * Records a resolution: `params` (null: not asked, the day's cached ones were used) and the contract lists TWS
   * delivered per expiry. Merged into the saved entry; expired and stale expiries are dropped. Empty parameters
   * (IBKR lists no options) forget the symbol.
   */
  remember(symbol: string, secType: SecType, conId: number, params: OptionParams[] | null, delivered: { pick: ExpiryPick; contracts: TwsContract[] }[], now: Date): void {
    if (params !== null && params.length === 0) { this.forget(symbol); return; }
    const old = this.read(symbol);
    const base: Entry = old && old.conId === conId ? old : { v: 1, symbol, secType, conId, paramsAt: null, params: [], expiries: {} };
    const at = now.toISOString();
    const entry: Entry = { ...base, secType, ...(params ? { paramsAt: at, params: optionRows(params, symbol) } : {}), expiries: {} };
    for (const [exp, x] of Object.entries(base.expiries)) if (usable(exp, x, now)) entry.expiries[exp] = x;
    for (const { pick, contracts } of delivered) {
      const rows = contracts.flatMap((c): [number, number, "C" | "P"][] =>
        c.conId && c.strike !== undefined && (c.right === "C" || c.right === "P") && c.lastTradeDateOrContractMonth === pick.expiry ? [[c.conId, c.strike, c.right]] : []);
      if (rows.length) entry.expiries[pick.expiry] = { tradingClass: pick.tradingClass, multiplier: pick.multiplier, fetchedAt: at, contracts: rows };
    }
    this.params.set(symbol, { conId, paramsAt: entry.paramsAt, params: entry.params });
    try { this.store?.write(symbol, entry); } catch { /* not saved: the next resolution tries again */ }
  }

  forget(symbol: string): void {
    this.params.set(symbol, null);
    try { this.store?.write(symbol, null); } catch { /* ignore */ }
  }

  /** The saved contract list for a chain without IBKR's contract server, or null when none is usable. */
  saved(symbol: string, now: Date): SavedContracts | null {
    const e = this.read(symbol);
    if (!e) return null;
    const l = listingOf(symbol);
    const expiries = Object.entries(e.expiries)
      .filter(([exp, x]) => usable(exp, x, now))
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([expiry, x]) => ({
        pick: { expiry, tradingClass: x.tradingClass, multiplier: x.multiplier }, fetchedAt: x.fetchedAt,
        contracts: x.contracts.map(([conId, strike, right]): TwsContract => ({
          symbol: l.ticker, secType: "OPT", exchange: l.optionExchange, currency: l.currency, conId, lastTradeDateOrContractMonth: expiry, strike, right,
          tradingClass: x.tradingClass, multiplier: x.multiplier,
        })),
      }));
    return expiries.length ? { conId: e.conId, expiries } : null;
  }
}

/** Not expired (New York date) and saved within SAVED_MAX_AGE_MS. */
function usable(expiry: string, x: SavedExpiry, now: Date): boolean {
  return expiry >= nyYmd(now) && now.getTime() - Date.parse(x.fetchedAt) <= SAVED_MAX_AGE_MS && x.contracts.length > 0;
}
