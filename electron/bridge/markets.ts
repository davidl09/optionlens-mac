// How a symbol of the engine is listed at IBKR (pure; mirrors the engine's analytics/markets.py, which is
// authoritative for symbols): a US listing is its bare ticker (AAPL, BRK.B), a Canadian one carries the exchange
// suffix (.TO = TSX, .VN = TSX Venture: RY.TO, RCI.B.TO). The one place of the bridge that knows IBKR's conventions
// per market — everything else asks listingOf().
//
// IBKR conventions (TWS API "Contracts"; verify against a live Gateway, see docs/INTEGRATION.md §6 "Markets"):
// - US stock: symbol with a space for share classes (BRK B), SMART, USD. Options: SMART (OPRA), USD.
// - Canadian stock: SMART with primaryExchange TSE (TSX) or VENTURE (TSX Venture), CAD; share classes and units are
//   written with a dot (RCI.B, AP.UN). Options: the Montréal Exchange, exchange CDE, CAD, multiplier 100, trading
//   class = the ticker before any class / unit suffix (RY, TECK for TECK.B: the engine's MX root, sources/osi.py).
export type Currency = "USD" | "CAD";
export type MarketCode = "US" | "CA";

export interface Listing {
  market: MarketCode;
  currency: Currency;
  /** IBKR's symbol of the stock. */
  ticker: string;
  /** Other spellings of `ticker` to try when IBKR does not know it (class shares), in order. */
  tickerAliases: string[];
  /** IBKR's primary exchange of the stock, where the listing must be named (undefined: SMART alone resolves it). */
  primaryExchange?: string;
  /** IBKR's exchange of the listing's options: the rows of reqSecDefOptParams used, and where they are quoted. */
  optionExchange: string;
  /** The trading class of the standard option contracts, where it is known beforehand. */
  optionClass: string;
  /**
   * Market-data permissions at IBKR are per exchange: the groups this listing's stock and option data belong to.
   * `stockData` is undefined where the symbol does not say which exchange lists it (US stocks: NYSE, Nasdaq, Arca
   * and others each have their own subscription).
   */
  stockData?: DataGroup;
  optionData: DataGroup;
}

/** Data that needs the same IBKR market-data subscription. */
export interface DataGroup { id: string; label: string; /** The subscription's name, for messages. */ subscription: string }

const US_OPTIONS: DataGroup = { id: "options:US", label: "US options", subscription: "OPRA" };
const CA_OPTIONS: DataGroup = { id: "options:CA", label: "Canadian options", subscription: "Montréal Exchange" };
const CA_VENUES: Record<string, { primaryExchange: string; stockData: DataGroup }> = {
  ".TO": { primaryExchange: "TSE", stockData: { id: "stocks:TSX", label: "TSX stocks", subscription: "TSX" } },
  ".VN": { primaryExchange: "VENTURE", stockData: { id: "stocks:TSXV", label: "TSX Venture stocks", subscription: "TSX Venture" } },
};

/** The option-data group of each market (one options feed per market: bridge.ts). */
export const OPTION_DATA: Record<MarketCode, DataGroup> = { US: US_OPTIONS, CA: CA_OPTIONS };
export const MARKET_CODES = Object.keys(OPTION_DATA) as MarketCode[];

export function listingOf(symbol: string): Listing {
  const sym = symbol.toUpperCase();
  const suffix = Object.keys(CA_VENUES).find((x) => sym.endsWith(x) && sym.length > x.length);
  if (!suffix) {
    // IBKR writes US share classes with a space (BRK B); the engine uses BRK.B.
    const ticker = sym.replace(".", " ");
    return { market: "US", currency: "USD", ticker, tickerAliases: [], optionExchange: "SMART", optionClass: ticker, optionData: US_OPTIONS };
  }
  const ticker = sym.slice(0, -suffix.length);
  return {
    market: "CA", currency: "CAD", ticker, tickerAliases: ticker.includes(".") ? [ticker.replace(/\./g, " ")] : [],
    primaryExchange: CA_VENUES[suffix].primaryExchange, optionExchange: "CDE", optionClass: ticker.split(".")[0],
    stockData: CA_VENUES[suffix].stockData, optionData: CA_OPTIONS,
  };
}

export function marketOf(symbol: string): MarketCode {
  return listingOf(symbol).market;
}

/** Lists merged by taking one item of each in turn, so no list waits for the whole of another (same order within). */
export function interleave<T>(lists: T[][]): T[] {
  const out: T[] = [];
  for (let i = 0; lists.some((l) => i < l.length); i++) for (const l of lists) if (i < l.length) out.push(l[i]);
  return out;
}
