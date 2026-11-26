# OptionLens — specification (authoritative)

A self-hosted, single-user options & stock analytics site whose *functionality* parallels a
premium options-analytics subscription (per-symbol options/volatility/earnings analytics,
universe-wide screeners, strategy screeners, backtests, watchlists, alerts, CSV export).
Original name, original UI. Do **not** copy any third-party branding, text or layout.

Runs as the Mac app's embedded engine: a local sidecar on loopback, guarded by a per-launch bearer
token (docs/INTEGRATION.md §1–§2). Times are America/New_York unless noted. Dates are ISO
`YYYY-MM-DD`. Percent values in APIs are **fractions** (0.25 = 25%) unless a field name ends in
`_pts` (vol points) — IV/HV are fractions too (0.25 = 25% vol).

---------------------------------------------------------------------------------------------
## 0. Honesty rules (product requirements)

1. Never fabricate data. When a metric needs history we do not yet have, return `null` and the
   UI shows "—" with a tooltip ("needs N more days of collected history").
2. Data we cannot obtain for free and therefore do **not** implement (list them on /about):
   trade-level options tape (time & sales, block/sweep trades, multi-leg trade detection, order
   flow buy/sell sentiment, large delta trades), auctions/order imbalance, threshold list,
   PDUFA/M&A calendars, company guidance, ETF holdings, minute VWAP, proprietary fair value.
   Proxies that *are* implemented must be labelled as proxies (e.g. "Large volume contracts"
   is per-contract daily volume, not individual trades; "net delta" is volume×delta, unsigned
   by trade side).
3. Historical implied volatility and historical option prices start on the day the collector is
   first run (stored daily). Anything needing pre-install option prices (earnings straddle
   history, option-strategy backtests) uses **real stored EOD option prices when available**
   and otherwise a **Black-Scholes model** priced with an IV proxy; every such result row
   carries `pricing: "real" | "model"`, and the UI badges model rows.
   IV proxy for model pricing, in order: stored ATM IV for that symbol/date/tenor → stored IV30 →
   `HV30(date) * k` where k = median(IV30/HV30) over stored history for that symbol, else 1.15.
4. Data is ~15 min delayed (CBOE delayed feed). Show "as of" timestamps everywhere.
5. Canadian listings (§3 "Symbols and markets"): no free source of dividend histories or financial statements is
   used for them (Nasdaq covers US listings only, and a Toronto ticker can name a different US company: T.TO is
   TELUS, T is AT&T); Questrade's daily reference figures (P/E, EPS, market cap, current dividend) cover them like
   US listings. Their earnings come from TMX Money, else Yahoo Finance, and their profile, news and insider reports
   from TMX Money (unofficial endpoints, INTEGRATION.md §12 and §12.1). The engine never sends a Canadian symbol to Nasdaq; the symbol's Dividends answer (and its
   Earnings answer when no source has a date) says why in `unavailable`; dividend reports cover US listings only.

---------------------------------------------------------------------------------------------
## 1. Data sources (all free, verified reachable)

| Source | URL pattern | Gives |
|---|---|---|
| CBOE delayed chain | `https://cdn.cboe.com/api/global/delayed_quotes/options/{SYM}.json` (indices: `_SPX`, `_NDX`, `_RUT`, `_VIX`, `_XSP`) | underlying quote (price, OHLC, prev close, volume, `iv30` in vol points) + every contract: `option` OSI code e.g. `AAPL261120C00100000`, bid/ask/sizes, `iv` (fraction; 0 when unknown), OI, volume, delta/gamma/vega/theta/rho, theo, last_trade_price, change, prev_day_close. Follows 307 redirect → use `follow_redirects=True`. gzip ~0.2–2 MB. |
| CBOE delayed quote | `.../delayed_quotes/quotes/{SYM}.json` | quote only (VIX family: `_VIX9D _VIX _VIX3M _VIX6M`) |
| CBOE index history CSV | `https://cdn.cboe.com/api/global/us_indices/daily_prices/{VIX,VIX9D,VIX3M,VIX6M}_History.csv` | `DATE(MM/DD/YYYY),OPEN,HIGH,LOW,CLOSE` |
| Nasdaq price history | `https://api.nasdaq.com/api/quote/{SYM}/historical?assetclass=stocks|etf&fromdate=YYYY-MM-DD&todate=YYYY-MM-DD&limit=9999` | `data.tradesTable.rows[]` `{date "MM/DD/YYYY", close "$352.45", volume "34,209,930", open, high, low}` (strings w/ `$`/`,`; may be "N/A") |
| Nasdaq earnings calendar | `https://api.nasdaq.com/api/calendar/earnings?date=YYYY-MM-DD` | rows `{symbol, name, time: time-pre-market|time-after-hours|time-not-supplied, eps, epsForecast, surprise, fiscalQuarterEnding, noOfEsts, marketCap}` (past dates have `eps`/`surprise`) |
| Nasdaq dividend calendar | `https://api.nasdaq.com/api/calendar/dividends?date=YYYY-MM-DD` | `data.calendar.rows[]` `{symbol, companyName, dividend_Ex_Date, payment_Date, record_Date, dividend_Rate, indicated_Annual_Dividend, announcement_Date}` |
| Nasdaq dividend history | `https://api.nasdaq.com/api/quote/{SYM}/dividends?assetclass=stocks` | `data.dividends.rows[] {exOrEffDate, type, amount, declarationDate, recordDate, paymentDate}` |
| Nasdaq stock universe | `https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&download=true` | `data.rows[] {symbol,name,lastsale,netchange,pctchange,volume,marketCap,country,ipoyear,industry,sector}` |
| Nasdaq ETF list | `https://api.nasdaq.com/api/screener/etf?tableonly=true&limit=10000&download=true` | `data.data.rows[] {symbol, companyName, lastSalePrice, oneYearPercentage...}` |
| Nasdaq quote info/summary/profile | `/api/quote/{SYM}/info?assetclass=stocks`, `/api/quote/{SYM}/summary?assetclass=stocks`, `/api/company/{SYM}/company-profile` | sector, industry, PE, EPS, 1y target, description |
| Nasdaq financials | `/api/company/{SYM}/financials?frequency=1` (annual) / `2` (quarterly) | income / balance / cash flow / ratios tables |
| Nasdaq insider trades | `/api/company/{SYM}/insider-trades?limit=50&type=ALL&sortColumn=lastDate&sortOrder=DESC` | insider transactions |
| Nasdaq news | `/api/news/topic/articlebysymbol?q={sym}|stocks&offset=0&limit=20&fallback=true` | headlines |
| Nasdaq extended hours | `/api/quote/{SYM}/extended-trading?markettype=pre|post&assetclass=stocks&time=0` | pre/after-hours last, volume |
| Nasdaq earnings surprise | `/api/company/{SYM}/earnings-surprise` | last 4 quarters EPS vs consensus |

| Bank of Canada Valet | `https://www.bankofcanada.ca/valet/observations/TB.CDN.90D.MID/json?start_date=YYYY-MM-DD` | CAD risk-free rate: "Treasury bills, 3-month" mid yield (legacy id V39065), percent, business daily: `observations[] {d, TB.CDN.90D.MID: {v}}` (verified 2026-11-24) |
| Questrade (user's account) | `symbols/search`, `symbols/:id`, `symbols/:id/options`, `markets/quotes`, `markets/candles/:id` (docs/INTEGRATION.md §8) | Canadian listings: symbol ids (TSX `.TO`, TSX Venture `.VN`, CAD), market cap / hasOptions / yield, Montréal Exchange chains, daily (`OneDay`) and intraday candles |
| TMX Money (unofficial) | `POST https://app-money.tmx.com/graphql`: `getEnhancedEarningsForDate(date)`, `getWSHEventData(symbols)` (read 2026-11-26) | Canadian listings' earnings calendar (BTO/AMC, EPS est/actual/surprise, years back) and next dates (INTEGRATION.md §12) |
| Yahoo Finance (unofficial) | cookie `fc.yahoo.com`, crumb `query2…/v1/test/getcrumb`, `query2…/v10/finance/quoteSummary/{SYM}?modules=calendarEvents` | Canadian listings' next earnings date when TMX Money has none (often 429 to an honest client) |
| Montréal Exchange options list | `https://www.m-x.ca/en/trading/data/options-list` (read once, 2026-11-24) | the equity / ETF option classes bundled as `engine/optionlens/canada.txt` (the Canadian universe's candidates; not fetched at run time) |

All Nasdaq calls need a browser User-Agent and `Accept: application/json`. Use a generic UA,
never personal info. Rate limits: Nasdaq ≤ 4 req/s, CBOE ≤ 8 concurrent. Retries: 3 with
exponential backoff on 429/5xx/timeouts. Yahoo throttles non-browser clients: used only as the last fallback for
Canadian listings' next earnings date, asked as OptionLens and given up at the first 429 (INTEGRATION.md §12).
Risk-free rate: Treasury 13-week bill (`https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{YEAR}/all?type=daily_treasury_bill_rates&field_tdr_date_value={YEAR}&_format=csv`, column for 13 weeks "COUPON EQUIVALENT"), fallback config `RISK_FREE_RATE=0.04`.
CAD (Canadian listings' options): Bank of Canada 3-month T-bill (above), fallback config `RISK_FREE_RATE_CAD=0.03`.

---------------------------------------------------------------------------------------------
## 2. Architecture

```
server/optionlens/
  config.py          Settings (env OPTIONLENS_*): DATA_DIR(default ~/.local/share/optionlens),
                     UNIVERSE_STOCKS(1000), ETF list file, SWEEP_INTERVAL_MIN(30), HOST, PORT(8473),
                     NTFY_URL, SMTP_* (optional), RISK_FREE_RATE, TZ, API_TOKEN, PARENT_PID,
                     CORS_ORIGINS, CHAIN_UNIVERSE_SIZE, CHAIN_SOURCES, QUESTRADE_EXPIRIES,
                     QUESTRADE_MONEYNESS, UNIVERSE_CA_STOCKS(200), CA_FILE, RISK_FREE_RATE_CAD
                     (HOST loopback only)
  models.py          frozen dataclasses shared across layers (see §3)
  net.py             one shared httpx.AsyncClient, per-host rate limiter, retry
  sources/           INFRA: parse remote payloads -> models.  cboe.py nasdaq.py treasury.py
  db.py              DuckDB connection (single process), schema bootstrap/migrations
  schema.sql
  repo/              PERSISTENCE: read/write functions per aggregate (symbols, bars, options,
                     metrics, earnings, dividends, user (watchlists/presets/alerts/notifications),
                     cache, jobs). Only module that writes SQL.
  analytics/         DOMAIN: pure functions over numpy arrays / models, no I/O. Fully unit tested.
                     bs.py vol.py chainmetrics.py earnings.py stats.py strategies.py payoff.py
                     backtest.py dividends.py calendar.py (NYSE and TSX trading calendars)
                     markets.py (symbol -> market / exchange / currency / calendar)
  services/          APPLICATION: compose repo + analytics for the API. symbol.py market.py
                     backtests.py alerts.py; reports/ (registry + one module per report family)
  jobs/              scheduler.py (asyncio, NY tz, trading-day aware) + collectors
  api/               FastAPI routers (thin: parse params -> service -> JSON); app.py: bearer auth,
                     CORS, body limit. API only (unknown paths -> 404 JSON).
  sidecar.py         uvicorn lifecycle: `LISTENING <port>` on stdout, SIGTERM, parent-death watchdog
  cli.py             `python -m optionlens.cli backfill|sweep|serve|eod|status`
(optionlens-mac) src/api/types.ts is the API contract (engine JSON must match; contract tests read it)
```
Dependency rule: api → services → (repo, analytics); jobs → (sources, repo, analytics);
analytics depends on nothing but numpy/scipy/models. Single process runs API + scheduler
(DuckDB single-writer). Blocking DB/compute runs in a threadpool (`anyio.to_thread`), each
thread uses its own `con.cursor()`.

---------------------------------------------------------------------------------------------
## 3. Core models (server/optionlens/models.py)

```python
Right = Literal["C","P"]
@dataclass(frozen=True) class Quote: symbol, ts: datetime, price, change, change_pct(fraction),
    open, high, low, prev_close, volume:int, bid, ask, iv30: float|None (fraction)
@dataclass(frozen=True) class OptionContract: osi, symbol, expiry: date, strike: float, right: Right,
    bid, ask, last, change, volume:int, oi:int, iv: float|None (None when feed gives 0),
    delta, gamma, theta, vega, rho, theo
@dataclass(frozen=True) class ChainSnapshot: quote: Quote, contracts: tuple[OptionContract,...], fetched_at: datetime
@dataclass(frozen=True) class Bar: symbol, date, open, high, low, close, volume:int
@dataclass(frozen=True) class EarningsEvent: symbol, date, timing: Literal["bmo","amc","unknown"],
    fiscal_quarter: str|None, eps_actual, eps_estimate, surprise_pct(fraction)|None, num_estimates:int|None
@dataclass(frozen=True) class Dividend: symbol, ex_date, amount, pay_date, record_date, declared_date, kind:str
```
OSI parse: root = chars before the 6-digit date, YYMMDD, C/P, strike = int(last 8)/1000.

**Symbols and markets** (analytics/markets.py, the one place that maps a symbol to its market). Two markets:
- US: NYSE / Nasdaq listings, OPRA options, USD, NYSE calendar. Canonical symbol = the bare ticker (`RY`, `BRK.B`).
- Canada: TSX / TSX Venture listings, Montréal Exchange (MX) options, CAD, TSX calendar. Canonical symbol = the
  Toronto ticker with Questrade's exchange suffix: `.TO` (TSX) or `.VN` (TSX Venture), class shares and units keeping
  their dot (`RY.TO`, `RCI.B.TO`, `AP.UN.TO`). So `RY` (NYSE, USD) and `RY.TO` (TSX, CAD) are two symbols and never
  resolve to each other (Questrade search: exact symbol, the market's currency, TSX / TSXV listing). Canadian listings
  in the universe are CAD-traded (USD units such as `DLR.U.TO` are left out), so a symbol's market fixes its currency.
- MX contracts get an OSI-like code whose root is the underlying's canonical symbol: `RY.TO261218C00120000` (OPRA's
  RY contract is `RY261218C00120000`). Questrade lists an MX equity option under the ticker before any class / unit
  suffix (`TECK` for TECK.B, `AP` for AP.UN); roots with a digit (adjusted deliverables) and multipliers other than
  100 are dropped, as for OPRA. MX equity options are American, 100 shares, expire on the third Friday (the trading
  day before when it is a holiday) and stop trading 16:00 ET, as US options.
- Every symbol reference in the API carries `market` ("US" | "CA"), `exchange` ("TSX" | "TSXV" | null for US) and
  `currency` ("USD" | "CAD"); report rows of a symbol carry `currency`. Money is shown in it (C$ always; US$ where a
  view mixes currencies, else $) and never converted or summed across currencies.
CBOE index roots in OSI may differ from symbol (e.g. `SPXW`); keep `symbol` = requested symbol
(without `_`), store root in osi only. `mid = (bid+ask)/2` when both >0 else last.

---------------------------------------------------------------------------------------------
## 4. Storage (DuckDB, `DATA_DIR/optionlens.duckdb`)

```
symbols(symbol PK, name, kind  -- 'stock'|'etf'|'index', sector, industry, market_cap DOUBLE,
        in_universe BOOL, optionable BOOL, updated_at TIMESTAMP)
daily_bars(symbol, date, open, high, low, close, volume BIGINT, PK(symbol,date))
quotes_latest(symbol PK, ts, price, change, change_pct, open, high, low, prev_close, volume, bid, ask, iv30)
option_latest(symbol, osi, expiry, strike, right, bid, ask, last, change, volume, oi, iv, delta,
        gamma, theta, vega, rho, theo, ts, PK(osi))            -- replaced per symbol each fetch
option_eod(date, symbol, osi, expiry, strike, right, bid, ask, last, volume, oi, iv, delta, gamma,
        theta, vega, underlying, PK(date, osi))                -- final sweep of each trading day;
        -- skip rows with bid=0 AND oi=0 AND volume=0; retention OPTION_EOD_RETENTION_DAYS (730)
metrics_daily(date, symbol, PK(date,symbol), close, change_pct, volume,
        iv30, iv60, iv90, iv120, iv180, iv360, iv30_cboe, atm_iv_front,
        skew25_30 (put25 IV - call25 IV @30d), put_skew_30 (put25 IV - ATM IV @30d),
        hv10, hv20, hv30, hv60, hv90, hv252,
        call_volume, put_volume, call_oi, put_oi, call_premium, put_premium (Σ vol*mid*100),
        call_vol_otm, call_vol_atm, call_vol_itm, put_vol_otm, put_vol_atm, put_vol_itm,
        call_prem_otm, call_prem_atm, call_prem_itm, put_prem_otm, put_prem_atm, put_prem_itm,
        net_delta_volume (Σ vol*delta*100 over calls and puts), straddle30_pct, expected_move30_pct,
        updated_at)                                            -- today's row upserted every sweep
earnings(symbol, date, timing, fiscal_quarter, eps_actual, eps_estimate, surprise_pct, num_estimates,
        source ('nasdaq' | 'tmx' | 'yahoo'), PK(symbol,date))
earnings_lookups(symbol PK, checked_at, source, detail)  -- Canadian listings' daily next-date lookup
dividends(symbol, ex_date, amount, pay_date, record_date, declared_date, kind, PK(symbol, ex_date))
index_bars(symbol, date, open, high, low, close, PK(symbol,date))   -- VIX family
rates(date PK, rate)                                                 -- 13w T-bill fraction (USD)
rates_cad(date PK, rate)                                             -- Bank of Canada 3-month T-bill fraction
symbols.dividend_yield DOUBLE                                        -- Questrade's indicated yield (Canadian listings)
report_snapshots(date, report_id, symbol, payload JSON, PK(date, report_id, symbol))
watchlists(id PK, name UNIQUE, created_at); watchlist_items(watchlist_id, symbol, added_at, PK(watchlist_id, symbol))
presets(id PK, report_id, name, payload JSON, created_at, UNIQUE(report_id,name))
alerts(id PK, kind, symbol NULL, params JSON, enabled BOOL, cooldown_min INT, last_fired_at, created_at)
notifications(id PK, ts, alert_id NULL, title, body, link, read BOOL)
http_cache(key PK, payload JSON, fetched_at)          -- TTL cache for fundamentals/news/insider/profile
job_runs(id PK, job, started_at, finished_at, ok BOOL, detail)
```
Moneyness buckets: ATM if |strike/spot − 1| ≤ 2.5%; calls OTM if strike > spot·1.025, ITM if
< spot·0.975; puts mirror.

---------------------------------------------------------------------------------------------
## 5. Jobs (America/New_York; NYSE and TSX trading days incl. holidays & early closes)

Trading-day jobs fire on days either exchange trades and each run handles the markets that traded (a TSX holiday
leaves the Canadian symbols out, a US-only holiday the US ones); sessions, closing-data boundaries, after-hours fills,
live sessions and intraday sessions use the symbol's exchange calendar. TSX calendar (tsx.com, 2026 / 2027 lists):
closed New Year's Day, Family Day (3rd Mon Feb), Good Friday, Victoria Day (Monday before May 25), Canada Day, Civic
Holiday (1st Mon Aug), Labour Day, Thanksgiving (2nd Mon Oct), Christmas, Boxing Day; a weekend holiday is taken on
the next free weekday (Christmas / Boxing Day on a weekend: Monday and Tuesday); early close 13:00 on a weekday
Christmas Eve only. Report snapshots and the EOD digest are the US market's.

| Job | When | What |
|---|---|---|
| universe | Mon 06:00 + first run | Top `UNIVERSE_STOCKS` common stocks by market cap from Nasdaq screener (exclude `^`, `/`, warrants/units/preferreds: symbols with `^`/`/` or names containing "Warrant", "Unit", "Right", "Preferred"), plus `server/optionlens/etfs.txt` (~120 liquid optionable ETFs incl. SPY QQQ IWM DIA, sector SPDRs XLK XLF XLE XLV XLY XLP XLI XLB XLU XLRE XLC, GLD SLV TLT HYG EEM EFA FXI KWEB SMH ARKK UVXY VXX SOXL TQQQ SQQQ …) and indices SPX NDX RUT XSP (CBOE `_` prefix). Upsert `symbols`. |
| universe_ca | Mon 06:05 + when a Questrade token is saved (skipped without Questrade) | Canadian universe: each candidate of `canada.txt` (MX equity / ETF option classes) resolved with Questrade (`<ticker>.TO`, else `.VN`, CAD) and described with `symbols/:id`; kept: `hasOptions`; of the stocks the `UNIVERSE_CA_STOCKS` (200) largest by Questrade's `marketCap`, plus every ETF / trust. Only Canadian universe flags change |
| prices_backfill | first run + daily 06:10 for symbols missing history | Nasdaq 10y history → daily_bars (Canadian listings: Questrade `OneDay` candles, continued past the 2,000-candle limit; depth and split adjustment are not documented — stored as returned, the weekly refresh replaces a changed history) |
| prices_refresh | Sat 07:00 | refetch last 30 days for all (corrects splits/revisions); on detected split (close jump inconsistent) refetch full |
| earnings | daily 05:30; backfill once | calendar for today−10 … today+90 (one call/day). Backfill: every weekday for the past 3 years (resumable; store progress in job_runs). Store every row (not only universe). |
| earnings_ca | daily 05:35; backfill daily 04:15 until complete | Canadian listings: TMX Money's calendar today−10 … today+90 (weekdays), then each stock's next date from TMX Money, else Yahoo Finance (INTEGRATION.md §12). Backfill: TMX's calendar, 3 years (resumable). |
| dividends | daily 05:45 | calendar today−10 … today+60; weekly (Sun) per-universe-symbol dividend history |
| vix | daily 17:00 | CBOE VIX family CSVs → index_bars |
| rates | daily 17:05 | Treasury 13w bill |
| rates_cad | daily 17:10 | Bank of Canada 3-month T-bill since the last stored day (11 years on first run) |
| sweep | trading days every SWEEP_INTERVAL_MIN from 09:45 to 16:00 | fetch CBOE chain for every universe symbol (concurrency 8): replace option_latest, upsert quotes_latest, compute chain metrics + HV → upsert today's metrics_daily, then evaluate alerts |
| eod | trading days 16:20 (13:20 early close) | final sweep + write option_eod for the day + write today's daily_bars from CBOE quote (o/h/l/c/volume) + store report snapshots (unusual volume report rows) + evaluate alerts + digest notification |
| prune | daily 03:00 | retention for option_eod, job_runs (90d), notifications (180d) |
On-demand: any API call for a symbol whose `option_latest` is older than 120 s during market
hours (or missing) triggers a single fetch (deduplicated by an in-flight lock per symbol). Symbols
outside the universe are fetched on demand too (and their history backfilled on first visit).

---------------------------------------------------------------------------------------------
## 6. Analytics definitions (exact)

- **Returns/HV**: log returns of daily closes. `HV_n = stdev(last n log returns, ddof=1)·√252`.
  Parkinson HV also offered: `sqrt(Σ ln(H/L)² / (4 n ln2))·√252`.
- **Black–Scholes–Merton** with continuous dividend yield q (q = trailing-12m dividends / price,
  0 for indices except SPX/XSP use 0.013 default). price, delta, gamma, vega (per 1 vol pt =
  /100), theta (per calendar day = /365), rho (per 1%). IV solver: Brent on [1e-4, 5] with
  price bounds check; returns None if price outside no-arb bounds. T in years = calendar
  days/365 (+ intraday fraction: expiry at 16:00 ET).
- **ATM IV per expiry**: for each expiry, take the two strikes bracketing spot; at each strike
  average call & put IV (whichever >0); linear interpolate in strike to spot. Ignore expiries
  with DTE < 1 for tenor interpolation (but show them in term structure).
- **Constant-maturity IV_T** (30/60/90/120/180/360 days): linear interpolation in total variance
  `w = σ²·t` between bracketing expiries; flat extrapolation outside the range.
- **25Δ skew**: within each of the two expiries bracketing 30d, interpolate IV at call delta
  +0.25 and put delta −0.25 (linear in delta, using feed deltas); then interpolate to 30d in
  variance like IV_T. `skew25_30 = put25 − call25`; `put_skew_30 = put25 − atm30`.
- **IV rank** (52w) = (cur − min)/(max − min) over the last 252 stored `iv30` values incl. today;
  **IV percentile** = fraction of those values strictly below cur. `null` if fewer than 20 stored
  values; always return `history_days`.
- **Expected move (to expiry)**: `straddle = mid(ATM call) + mid(ATM put)` at strike nearest
  spot; `straddle_pct = straddle / spot`; IV-based 1σ move = `spot·atm_iv·√(dte/365)`.
  `expected_move30_pct` = atm30 IV · √(30/365).  `straddle30_pct` = straddle % of the expiry
  nearest 30 DTE.
- **Earnings move** for event (date d, timing):
  `pre_close` = close of last trading day before the reaction day; reaction day = d if bmo,
  next trading day after d if amc; if unknown, treat as amc when the |gap| on d+1 > |gap| on d,
  else bmo (and mark `timing_inferred=true`).
  `gap_pct = open(react)/pre_close − 1`, `move_pct = close(react)/pre_close − 1`,
  `max_move_pct` = max(|high/pre_close−1|, |low/pre_close−1|) on react day,
  drift windows: `pre_drift_{5,10}` = pre_close/close(pre_close day − N) − 1;
  `post_drift_{1,5,10,20}` = close(react + N)/close(react) − 1.
  Stats: count, avg move, avg |move|, median |move|, stdev move, % up, max up, max down,
  avg |gap|. **Implied earnings move** = straddle of first expiry whose date ≥ reaction day,
  as % of spot; also "excess implied" = implied − typical non-earnings move derived from
  front-expiry: `sqrt(max(0, σ_front²·t_front − σ_back_ex²·(t_front − 1/252)))` (single-day
  event vol, Ivol-term method using the next expiry as ex-event baseline); expose both.
- **Probability** (lognormal, risk-neutral drift r−q, vol σ): P(S_T > K) = N(d2).
  Strategy probability of profit integrates payoff>0 over the lognormal density numerically
  (grid of 2001 log-price points ±6σ). Expected value under IV and under HV (σ=HV30, drift 0).
- **Max pain**: strike minimizing Σ_OI intrinsic value paid to holders across calls+puts for an expiry.
- **Beta / correlation vs SPY**: 1y daily log returns, aligned dates.
- **RSI14** (Wilder), **ATR14%**, SMA 20/50/200, distance from 52w high/low.
- **Support/resistance**: pivot highs/lows (5-bar fractals) over 6 months, clustered within 1.5%,
  return the 3 nearest above (resistance) and below (support) current price.
- **Seasonality**: monthly returns (last close of month / last close prior month − 1) per year
  up to 10y: table year×month, per-month avg, median, % positive; day-of-week avg return &
  % positive; "intraday vs overnight": overnight = open/prev close − 1, intraday = close/open − 1,
  cumulative and averages.
- **Dividend capture**: for each ex-date: buy close day before ex, sell close on ex-date →
  price return, + dividend, total return; recovery days = trading days until close ≥ pre-ex close.
- **Covered call** (long 100 sh at ask/last, short call at bid): `static_return = bid/(S−bid)`,
  `if_called_return = (K − S + bid)/(S − bid)`, annualized = (1+r)^(365/dte) − 1 is NOT used;
  annualized = r · 365/dte (simple). `downside_protection = bid/S`. prob called = P(S_T>K).
- **Cash-secured / naked put** (sell put at bid): `return_on_cash = bid/(K − bid)`,
  annualized simple; breakeven K − bid; `margin_req = max(0.20·S − max(0,S−K) + bid, 0.10·K + bid)·100`;
  `return_on_margin = bid·100/margin_req`; prob OTM = P(S_T>K).
- **Verticals**: legs at same expiry, widths from strike grid; credit spreads priced short@bid,
  long@ask (natural) and also mid; max profit/loss, breakeven, return on risk, PoP, prob of max
  profit, EV(IV), EV(HV). Debit spreads mirror. **Iron condor / butterflies / calendars /
  straddles / strangles / synthetic** defined analogously in analytics/strategies.py with natural
  and mid pricing. Calendar: short front/long back same strike; metrics: debit, front IV, back IV,
  IV ratio front/back, forward vol `sqrt((σb²tb − σf²tf)/(tb − tf))`, theta ratio, est. max profit
  at front expiry (BS value of back at front expiry with back IV, spot = K − debit).
  Synthetic long: long call @ask + short put @bid at same K/expiry: synthetic price = K + call_ask − put_bid;
  discount vs spot after carry = S·e^{−qT}... carry-adjusted parity gap = (S − PV(K)) − (C − P) using
  mids; report both natural and mid.
- Liquidity filters available to all strategy screeners: min OI per leg, min volume, max bid-ask
  spread % of mid (`(ask−bid)/mid`), min bid > 0.

---------------------------------------------------------------------------------------------
## 7. Reports (universe-wide screeners) — generic contract

Every screener/report is registered in `services/reports/registry.py`:
`ReportDef(id, title, category, description, params: list[ParamDef], columns: list[ColumnDef],
run: (params) -> list[dict], row_key)`. API: `GET /api/reports` (definitions) and
`GET /api/reports/{id}?<params>` → `ReportResult`. UI renders any report generically: param form
(server-side), column filters (client-side, min/max/text/multi-select per column), sorting,
column-group toggles, CSV export of the filtered rows, saved presets (params + filters + sort +
hidden columns), watchlist membership filter, row click → symbol page. Max 5000 rows returned
(`truncated: true` flag otherwise).

**Markets.** Every report declares `ReportDef.markets`: `any` = a `market` param US (default) / CA / all; `one` =
US / CA (aggregates: options-broad-view, performance-by-sector, performance-by-industry); `us` = a US-only source, no
param (dividends, splits / IPO calendars, pre-market / after-hours movers, event-driven). Earnings reports are `any`
(Canadian listings' events from TMX Money / Yahoo Finance), the upcoming-earnings summary `one`. The filter
applies before limits (universe frame, chain loads) and again to the result rows; an explicit `symbols` list is
shown whatever its market, a watchlist is narrowed to it. Rows of a symbol carry `currency`.

Column `fmt` values: `symbol`, `text`, `int`, `num`, `num2`, `money`, `bigmoney` (1.2B), `pct`
(fraction→%), `pct_signed` (colored ±), `vol` (IV/HV fraction → "25.3"), `date`, `datetime`,
`bool`, `osi`, `ratio` (2 decimals ×), `badge`.

Required reports (id — columns highlights; params):
**Options**
- `unusual-option-volume` — Symbol, Name, Market Cap, Type, Sector, Industry, InWatchlist, Option
  Volume, Avg Volume (20d), Relative Volume, Call Vol, Put Vol, % Call, % Put, $ Call, $ Put (premium),
  OTM/ATM/ITM call & put volume (group "Volume by moneyness"), Volume-weighted net delta, Sentiment
  (Bullish if %Call ≥ 70%, Bearish if %Put ≥ 70%, else Neutral), Prev Close, Price, % Chg, Next
  Earnings, Days To Earnings, Days Since Earnings, Next Ex-Div, Days To Ex-Div, IV30, IV30 % chg,
  IV Rank. Params: min_rel_volume(2), min_volume(1000), lookback_days(0..10: when >0 return
  historic report snapshots with Record Date, # appearances, Close, Next Close, % Chg Next Day).
- `iv-rankings` — IV30, IV rank, IV pct, IV30 1d/5d change (pts), HV20, IV/HV, IV60, IV90, term slope (iv90−iv30), skew; params: sort.
- `iv-movers` — biggest 1-day IV30 change up/down (abs & %).
- `option-volume-leaders` — total/call/put volume, P/C volume ratio, premium, vs avg.
- `oi-changes` — per contract: OI today, OI prior day, change, %change, volume, IV, delta; params min_change.
- `large-volume-contracts` (proxy for large trades) — per contract volume ≥ min (default 2000) or
  premium ≥ min; vol/OI, side heuristic none; params min_volume, min_premium, dte range.
- `option-screener` — per-contract screener: Symbol, Expiry, DTE, Type, Strike, Bid, Ask, Mid, Last,
  Spread %, Volume, OI, Vol/OI, IV, Delta, Gamma, Theta, Vega, Moneyness %, Breakeven, Premium % of
  stock, Annualized premium yield, Prob ITM, Underlying, IV Rank, Earnings before expiry; params:
  right, dte_min/max, delta_min/max, moneyness range, min_oi, min_volume, max_spread_pct, symbols (optional list), watchlist.
- `options-broad-view` — per sector & total: call/put volume, P/C, premium, avg IV30, avg IV rank; plus market P/C ratio history (rows by date) in `extra`.
- `put-protection` — cost of protective put at target DTE & %OTM: put ask, cost %, annualized cost, IV.
**Strategy screeners** (shared params: dte_min, dte_max, symbols/watchlist, min_oi, max_spread_pct,
  pricing natural|mid, earnings_filter any|exclude|include, plus strategy-specific delta/width):
  `covered-calls`, `naked-puts`, `long-calls`, `long-puts`, `bull-call-spreads` (debit),
  `bear-put-spreads` (debit), `bull-put-spreads` (credit), `bear-call-spreads` (credit),
  `iron-condors-short` (short inner/long outer), `iron-condors-long`, `call-butterflies-long`,
  `call-butterflies-short`, `put-butterflies-long`, `put-butterflies-short`, `iron-butterflies-short`,
  `iron-butterflies-long`, `calendar-calls`, `calendar-puts`, `atm-straddles`, `strangles`,
  `synthetic-long`. Each row: legs description (e.g. "-1 C 200 / +1 C 210 2026-12-18"), cost/credit,
  max profit, max loss, breakevens, return on risk, annualized, PoP, EV(IV), EV(HV), net delta,
  theta, DTE, IV, IV rank, earnings-before-expiry flag. Index variants via `symbols=SPX,NDX,RUT,XSP`
  (UI link "Index options strategies").
**Earnings**
- `earnings-calendar` — date range params (default today..+7): Date, Time (BMO/AMC), Symbol, Name, Mkt
  Cap, EPS est, # ests, Implied move (straddle), Avg |move| last 8, Implied/Historical ratio, IV30,
  IV rank, Last move.
- `earnings-results` (recent reporters' stock moves) — past range: EPS actual/est/surprise, gap %,
  move %, vs implied (if stored), post drift 5d.
- `earnings-pattern-screener` — upcoming (next N days) with historic stats over last 8/12 events:
  % up, avg move, avg |move|, pre-drift 5d avg & win %, post-drift 5d avg & win %; params lookback (4/8/12), window.
- `earnings-option-strategy-screener` — for upcoming reporters (or symbols list) backtest
  each strategy over last N (≤12) events with entry offset {−10,−5,−3,−2,−1} trading days and
  exit offset {+1,+2,+3,+5,+10} trading days relative to the reaction day (bmo: reaction day =
  event day; amc: next trading day; entry/exit at close; entry −1 means close before reaction
  day). Strategies (≥30): long/short ATM straddle, long/short 5%/10% OTM strangle, long/short
  ATM call, ATM put, 25Δ call, 25Δ put, bull/bear call/put verticals (ATM/5% wide), long/short
  iron condor (5/10%), long/short iron butterfly (ATM, 5% wings), long/short call/put calendar
  (front = first expiry ≥ exit date, back = next monthly), covered call ATM, long stock, short stock,
  stock + protective put. Expiry chosen = first expiry ≥ exit date (≥ 2 days after exit for short
  premium). Columns: Symbol, Strategy, Next earnings, # trades, Win rate, Avg return (% of debit
  or of max risk for credit), Median, Best, Worst, Avg $ P/L per contract, `pricing` mix
  (real/model count). Prices use mid; skip observations with spread% > 25% (real data only).
- `earnings-todays-opportunities` — today+tomorrow reporters ranked by implied vs historical move gap.
- `earnings-upcoming-summary` — per day next 30 days: count, BMO/AMC counts, total mkt cap, notable names (top 5 by cap).
**Volatility**
- `term-structure-screener` — iv30, iv60, iv90, slope, backwardation flag (iv30 > iv90·1.05).
- `skew-screener` — skew25_30, put_skew_30, rank of skew among stored history.
- `iv-hv-premium` (risk premium) — IV30 − HV20/HV30, ratio, rank.
- `zero-dte-straddles` — SPX, XSP, SPY, QQQ, IWM (+ any symbol with expiry today): straddle price,
  % move implied, breakevens, IV.
**Stocks**
- `stock-screener` — 30+ columns: Price, %Chg 1d/5d/1m/3m/6m/YTD/1y, Volume, Avg Vol 30d, Rel Vol, Mkt
  Cap, Sector, Industry, Div Yield, Next Ex-Div, Next Earnings, Days to earnings, IV30, IV Rank,
  IV pct, HV20, HV60, IV/HV, Option Volume, Rel Opt Vol, P/C vol, Total OI, Beta, Corr SPY, RSI14,
  ATR%, % from 52w High/Low, % vs SMA20/50/200, Gap %.
- `etf-screener` — same columns for kind='etf'.
- `market-movers` (gainers/losers/most active, param side), `gainers-high-volume`, `losers-high-volume`,
  `52w-highs-lows` (param side), `unusual-stock-volume` (rel vol ≥ x), `volume-burst` (today vol ≥ 3× avg
  and |chg| ≥ 2%), `gap-and-go` (param up|down: |gap| ≥ x% and close continues in gap direction),
  `outperformers` (vs SPY over 1m/3m/6m/1y), `performance-by-sector`, `performance-by-industry`,
  `seasonality-screener` (current/next month historic avg return & % positive over 10y),
  `premarket-movers` and `afterhours-movers` (every symbol from Questrade's extended-hours quotes, collected every
  5 min in pre-market / after-hours, INTEGRATION.md §8; Nasdaq's extended endpoint as the fallback for watchlist ∪
  top N by volume, 5-min cache), `event-driven` (upcoming earnings/ex-div within N days).
- `splits-calendar` (Nasdaq splits calendar: ratio new:old, split factor, Forward/Reverse, universe join) and
  `new-listings` (Nasdaq IPO calendar, param status priced|upcoming|filed, months 1–6); both cached 6 h.
**Dividends**
- `dividend-calendar` (ex-date range), `future-ex-dates`, `dividend-announcements` (by declaration
  date), `dividend-increases` (latest regular amount > previous regular), `best-dividends` (yield
  rank with payout history count), `dividend-capture` (historic capture stats per symbol for next
  ex-date), `reits-mlps` (industry filter REIT/MLP/closed-end fund), `dividend-comparison` (symbols list).
Each report's `notes` field states its proxies and data limitations.

---------------------------------------------------------------------------------------------
## 8. Per-symbol pages (route `/s/:sym/:tab`)

Tabs (left nav grouped like: Stock · Options · Volatility · Earnings · Tools · Company):
- **summary**: quote header (price, chg, %chg, as-of), key stats grid (open/high/low, volume vs
  30d avg, 52w & 3y high/low, market cap, sector/industry, beta, corr SPY, IV30, IV rank/pct,
  HV20, IV/HV, option volume calls/puts vs 20d avg, P/C, total OI, next earnings (date, timing,
  implied move, days), next ex-div (date, amount, yield)), returns row (1d,5d,1m,3m,6m,YTD,1y,3y),
  support/resistance levels, mini charts: price 1y + IV30 vs HV20, top 3 trade ideas.
- **chart**: candlestick + volume, ranges 1m/3m/6m/1y/3y/5y/10y, SMA 20/50/200 toggles, earnings
  and ex-div markers.
- **price-action** / **history**: daily table (date, O,H,L,C, %chg, gap%, range%, volume, rel vol),
  stats (avg daily |move|, up-day %, avg gap, largest moves), intraday vs overnight returns, CSV.
- **dividends**: history table, yield, annual rate, frequency, growth (1y/3y/5y CAGR), next ex,
  capture stats.
- **chain**: expiry selector (with DTE), view calls/puts/both (straddle view: calls left, strike
  middle, puts right), strike range (±N strikes or all), columns: bid, ask, mid, last, chg, vol,
  OI, IV, Δ, Γ, Θ, V, theo; ITM shading; highlight vol > OI; click a cell → adds leg to calculator.
- **activity**: volume & OI by expiry (bar), by strike for chosen expiry (calls vs puts), top 20
  contracts by volume, P/C volume & OI ratios, call/put volume history (from metrics_daily),
  moneyness breakdown.
- **open-interest**: OI by strike chart per expiry, OI change vs prior day (needs option_eod),
  max pain per expiry, top OI increases.
- **contract** (`/s/:sym/contract/:osi`): history of mid, IV, OI, volume, delta, underlying from option_eod.
- **calculator** (strategy payout): legs editor (stock, calls, puts; qty ±; price default mid,
  editable), templates (all strategies from §7), P/L at expiration + today + chosen date (slider),
  IV shift slider (−50..+50 pts), table of P/L by price, greeks totals, breakevens, max profit/loss,
  PoP, EV(IV)/EV(HV), cost; uses BSM with each leg's own IV.
- **ideas** (trade ideas): top strategies for this symbol from the strategy screeners ranked by
  EV(HV)/risk ("edge") and by PoP ("win rate"), filter by strategy family (verticals, condors/flies,
  long call/put, straddles), plus seasonality idea (current-month historic stats).
- **volatility**: IV30 vs HV20 (+HV10/60 toggles) history chart, IV rank/pct stats with history
  days, constant-maturity table (30/60/90/120/180/360) with 1d/1w change, term structure chart
  (today vs 1w ago vs 1m ago when available), HV table (10/20/30/60/90/252 + Parkinson 20), IV/HV
  risk premium chart, realized-vs-implied (IV30 at t vs HV over next 21 trading days, when history allows).
- **skew**: IV smile per expiry (by strike and by delta), 25Δ skew history, put skew history.
- **implied-moves**: per expiry: DTE, straddle, straddle %, ±1σ IV move, price range, earnings-in-window flag.
- **vol-seasonality**: avg IV30 by calendar month (stored history) + HV20 by month (10y price history).
- **earnings**: next event panel (date, timing, implied move, excess implied, avg |move|, ratio);
  history table (date, timing, fiscal qtr, EPS est/actual/surprise, pre-close, gap%, move%, max
  move%, post-drift 1/5/10/20, implied move if stored, IV crush (IV30 before/after) if stored);
  stats; charts: move bars vs implied, EPS est vs actual, pre/post drift averages;
  "Option strategies" sub-panel = earnings-option-strategy backtest for this symbol (all strategies,
  selectable entry/exit), with per-event trade list for a chosen strategy.
- **seasonality**: monthly heatmap, month stats, day-of-week, intraday/overnight.
- **events**: upcoming/past earnings & dividends timeline; option expiries.
- **straddle**: ATM straddle per expiry (price, %, breakevens) + historic straddle-to-expiry backtest
  (rolling 30-DTE straddles, real/model) with win rate and avg return.
- **fundamentals**: key ratios (Nasdaq summary), income statement, balance sheet, cash flow, ratios
  (annual/quarterly toggle), payback period (market cap / trailing FCF), peer comps (same industry,
  top 10 by market cap: price perf, IV30, IV rank, PE when available).
- **insiders**, **news**, **about** (profile, website, similar stocks = industry peers).

---------------------------------------------------------------------------------------------
## 9. Other pages
- `/` dashboard: index cards (SPY QQQ IWM DIA + SPX/NDX/RUT), VIX term (9D/30D/3M/6M with history
  chart and contango/backwardation), sector ETF performance heat strip, top gainers/losers, top
  unusual option volume (10), earnings today/tomorrow with implied moves, IV movers, watchlist
  quick table, data freshness.
- `/reports` index (cards grouped by category) and `/reports/:id` generic report page.
- `/backtest/stock`: rule backtester — universe = one symbol or list; entry rule one of: every
  N trading days / day-of-month / month-of-year (seasonal) / N days before earnings / N days after
  earnings / gap up|down ≥ x% / RSI below|above x / close crosses SMA(n) above|below / new 52w high|low;
  exit rule: hold N days / at earnings reaction close / stop-loss % / take-profit % (first hit;
  daily close based); direction long|short; date range. Output: trades table, summary (count, win
  rate, avg, median, total compounded, CAGR, Sharpe (daily, of equity curve), max drawdown, avg
  hold), equity curve chart, return distribution histogram, comparison to buy & hold.
- `/backtest/options`: strategy (any from §7 templates parameterized by delta or % OTM or width),
  symbol, entry schedule (every Nth trading day / monthly on first trading day / weekly on day X),
  target DTE (nearest expiry to target), exit (hold to expiry / DTE ≤ x / take profit % of max
  / stop loss %), date range; pricing per day: real option_eod mid when available for every leg,
  else model (BSM, IV proxy per §0.3, strikes snapped to listing-like grid: $1 < $50, $2.5 < $100,
  $5 < $500, $10 above; indices $5/$25 by price); expiries synthesized as third Fridays + weeklies
  (Fridays) when model. Output: trades (entry/exit dates, legs, entry/exit value, P/L, %),
  summary stats, equity curve, `pricing` per trade. `/backtest/straddle-wing` preset for straddle
  ± wings (iron fly) per symbol.
- `/compare/volatility?symbols=` multi-symbol IV30 & HV20 chart overlay + table;
  `/compare/straddle?symbols=&dte=30` straddle %/implied move comparison.
- `/watchlists` (CRUD, quick table of quotes/IV/earnings for each list; "add to watchlist" button on
  every symbol page & report row menu), `/alerts` (CRUD + notification feed with unread badge in
  header), `/status` (job runs, data freshness, row counts, history days, universe size), `/about`
  (data sources, limitations list from §0).
- Global header: symbol search with autocomplete (symbols table), nav menus (Market · Options ·
  Strategies · Earnings · Volatility · Stocks · Dividends · Backtest · My), notification bell,
  dark/light theme toggle (default dark).

Alerts kinds: `price_above`, `price_below`, `pct_change_abs` (|%chg| ≥ x), `iv30_above`,
`iv30_below`, `iv_rank_above`, `iv_rank_below`, `rel_option_volume_above`, `unusual_volume`
(symbol appears in unusual-option-volume report), `earnings_within_days`, `ex_div_within_days`.
Scope: single symbol, or watchlist (params.watchlist_id). Delivery: in-app notification always;
plus ntfy POST to `NTFY_URL` if configured; plus SMTP email if configured. Cooldown per alert per
symbol (default 240 min). Digests at 08:00 (earnings today, top premarket from watchlist) and
16:45 (unusual volume top 10, IV movers, watchlist summary) as notifications.

---------------------------------------------------------------------------------------------
## 10. API (all JSON; contract in optionlens-mac src/api/types.ts — server MUST match it; ingest API: docs/INTEGRATION.md §6)

```
GET  /api/health                                  {ok, version, auth, server_id} (no auth)
GET  /api/status                                  Status
GET  /api/search?q=                               SymbolRef[] (max 20; prefix match symbol, then name contains; with market / exchange / currency)
GET  /api/market/overview                         MarketOverview
GET  /api/symbol/{sym}/summary                    SymbolSummary
GET  /api/symbol/{sym}/bars?range=1m|3m|6m|1y|3y|5y|10y|max   Bar[]
GET  /api/symbol/{sym}/price-action?range=1y      PriceAction
GET  /api/symbol/{sym}/chain?expiry=              Chain (expiry default = nearest ≥ today)
GET  /api/symbol/{sym}/activity?expiry=           Activity
GET  /api/symbol/{sym}/open-interest?expiry=      OpenInterest
GET  /api/symbol/{sym}/contract/{osi}             ContractHistory
GET  /api/symbol/{sym}/volatility                 Volatility
GET  /api/symbol/{sym}/skew?expiry=               Skew
GET  /api/symbol/{sym}/implied-moves              ImpliedMoves
GET  /api/symbol/{sym}/vol-seasonality            VolSeasonality
GET  /api/symbol/{sym}/earnings                   Earnings
GET  /api/symbol/{sym}/earnings/strategies?lookback=12&entry=-1&exit=1   EarningsStrategyResult
GET  /api/symbol/{sym}/earnings/strategies/{strategy}/trades?lookback=&entry=&exit=  BacktestTrade[]
GET  /api/symbol/{sym}/dividends                  Dividends
GET  /api/symbol/{sym}/seasonality                Seasonality
GET  /api/symbol/{sym}/events                     Events
GET  /api/symbol/{sym}/straddle                   StraddleInfo
GET  /api/symbol/{sym}/ideas                      TradeIdeas
GET  /api/symbol/{sym}/fundamentals?period=annual|quarterly  Fundamentals
GET  /api/symbol/{sym}/peers                      ReportResult (peer comps)
GET  /api/symbol/{sym}/insiders                   Insiders
GET  /api/symbol/{sym}/news                       NewsItem[]
GET  /api/symbol/{sym}/profile                    Profile
POST /api/calc/strategy                           CalcRequest -> CalcResult
GET  /api/strategy-templates                      StrategyTemplate[]
GET  /api/reports                                 ReportDef[]
GET  /api/reports/{id}?...params                  ReportResult
POST /api/backtest/stock                          StockBacktestRequest -> BacktestResult
POST /api/backtest/options                        OptionsBacktestRequest -> BacktestResult
GET  /api/compare/volatility?symbols=A,B          CompareVolatility
GET  /api/compare/straddle?symbols=A,B&dte=30     ReportResult
GET/POST /api/watchlists ; GET/PUT/DELETE /api/watchlists/{id} ; POST/DELETE /api/watchlists/{id}/symbols/{sym}
GET  /api/watchlists/{id}/table                   ReportResult
GET/POST /api/presets?report_id= ; DELETE /api/presets/{id}
GET/POST /api/alerts ; PUT/DELETE /api/alerts/{id}
GET  /api/notifications?unread=1 ; POST /api/notifications/read (body {ids?:number[]} all if omitted)
```
Canadian symbols are `RY.TO` / `XYZ.VN` (§3); one not stored yet is looked up on Questrade (404 without it).
Errors: `{detail: string}` with 404 for unknown symbol, 422 for bad params. Symbols upper-cased;
index symbols accepted as `SPX` or `^SPX` (stored as `SPX`, fetched as `_SPX`). NaN/inf → null.
