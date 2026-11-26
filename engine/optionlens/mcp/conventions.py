"""Units and data provenance in OptionLens data, written for AI agents (the `optionlens://conventions` resource and the
`describe_units` tool). It restates docs/INTEGRATION.md §5-§9 and the ReportDef column formats; keep them in step."""

CONVENTIONS = """\
# OptionLens data conventions (read before interpreting numbers)

## Units
- Fractions, never percent: change_pct, returns, yields, IV, HV, IV rank / percentile, probabilities (pop),
  win rates, skews. 0.25 means 25 %. Alert and filter values use the same units.
- Prices are per share in the symbol's currency: USD for US listings (AAPL, RY), CAD for Canadian ones (RY.TO on the
  TSX, XYZ.VN on the TSX Venture; Montréal Exchange options). Symbol answers carry `market` (US | CA), `exchange` and
  `currency`; report rows of a symbol carry `currency`. Values are never converted: do not add USD and CAD amounts.
  One option contract is 100 shares: multiply option prices by 100 for dollars per contract.
- Position values are dollars for the whole position: calc_strategy cost (+ debit / − credit), P&L curves, max profit /
  loss (null = unlimited), trade-idea cost / max profit / max loss, backtest P&L; option quantities are contracts
  (×100 shares), stock quantities shares.
- Greeks are per share: delta and gamma per $1 move of the underlying, theta $ per calendar day, vega $ per 1 vol
  point (0.01 of IV). Position greeks (calc_strategy) are totals for the position in $.
- Volume and open interest are contracts (options) or shares (stocks); premium is dollars.
- Dates YYYY-MM-DD (New York trading days); times ISO-8601 with offset. dte = calendar days to expiry.
- Universe reports take `market`: US (default), CA or all (aggregates: US or CA only); earnings reports too, the
  dividend and Nasdaq calendar reports cover US listings only. Earnings of US listings come from Nasdaq, of Canadian
  listings from TMX Money, else Yahoo Finance (unofficial endpoints; `source` names who reported each event; without
  a date `unavailable` / `next_missing` say why). Dividend histories of Canadian listings are not available
  (`unavailable` says why); their risk-free rate is the Bank of Canada 3-month T-bill.
- Report columns carry `fmt`: pct / pct_signed / vol = fraction, money = per share in the row's currency, bigmoney =
  amount in the row's currency, int / num /
  num2 = plain numbers, ratio = x times, date / datetime, osi = option code, badge / text / bool.

## Missing values
- null means unknown or not available — never zero. Do not treat null volume / OI / IV as 0.
- An empty table has `empty_reason`; `notes` explain coverage limits and proxies; `method` gives formulas.
- `truncated: true` on a report: the engine kept the first 5,000 rows. `_page` in a tool answer: this MCP server
  cut long arrays (page with offset / limit).

## Provenance flags
- source: where values came from — ibkr (IBKR bridge), questrade, cboe, nasdaq, vendor, model (a model price, not a
  quote), reconstructed (rebuilt from historical daily bars: no bid / ask / OI).
- delayed: true = delayed market data (no real-time subscription for that class).
- after_hours: the stored quote / chain / row was collected after its session closed (frozen closing data of that
  session: evening, weekend, before the open). It is that session's close, not a new trade.
- iv_source: the chain source (e.g. ibkr, questrade), computed_mid (Black-Scholes from the bid/ask mid) or
  computed_last (from a last trade of the session). greeks_source: the source, or computed (from the IV).
  atm_iv_computed / iv_computed: a metric uses engine-computed IVs because the source gave none.
- contracts_from_cache: the chain's contract list was a saved one (IBKR's contract server was down); quotes are
  current, newly listed expiries may be missing.
- optionable: true = a chain is stored, false = every chain source serving the symbol confirmed it lists no options,
  null = not known yet (e.g. one source lists none, another has not answered).
- IV30 and IV rank / percentile are of the latest stored value (iv30_asof); rank / percentile are null when that
  value is not from the last session.

## Live vs stored
- While a symbol's market is open (US: 04:00-20:00 ET for stocks, 09:30-close for options; Canada: 09:30-close),
  values on the user's screen update live (live_status, live_quotes). Other tools read the
  stored data (latest chain / quote, end-of-day archives). History-based metrics (IV rank, HV, returns, backtests)
  are never live.

## What agents can and cannot do
- Read everything the app shows (symbols, chains, reports, market overview, watchlists, alerts, presets,
  notifications, status) and what the user is looking at (get_current_view).
- Edit watchlists, alerts and presets, and mark notifications read. No trading, orders or account access exist.
"""
