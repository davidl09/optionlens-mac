# OptionLens app ⇄ engine integration contract (authoritative)

Two parts, one machine:
- **engine** (this Python code; FastAPI + DuckDB + in-process scheduler) — the app's embedded backend.
  The desktop app launches it as a local sidecar process bound to loopback. Collects data (Nasdaq
  prices/calendars, Treasury rates; option data pushed by the IBKR bridge and/or fetched from Questrade, §8),
  archives end-of-day, serves the analytics API. API-only: it does not serve the UI.
- **optionlens-mac** — Electron app: the UI (React, formerly `web/`), the engine's lifecycle, and the
  **IBKR bridge** (talks to the user's running TWS / IB Gateway and pushes data to the engine).

No remote server: the engine listens on loopback only. A per-launch token still guards the
API against other local processes and web pages.

---------------------------------------------------------------------------------------------------
## 1. Engine configuration (environment, set by the app at launch)

| Variable | Default | Meaning |
|---|---|---|
| `OPTIONLENS_HOST` | `127.0.0.1` | Bind address; must be `127.0.0.1`, `::1` or `localhost` (anything else: the engine refuses to start) |
| `OPTIONLENS_PORT` | `8473` | Port; `0` = bind a free port. `serve --port N` overrides it |
| `OPTIONLENS_API_TOKEN` | — | Random per launch, chosen by the app (≥ 16 chars). Unset = auth disabled (development only) |
| `OPTIONLENS_PARENT_PID` | — | The app's pid: the engine shuts down when that process disappears |
| `OPTIONLENS_CORS_ORIGINS` | `app://optionlens` | Comma list of allowed browser origins (the Electron renderer origin) |
| `OPTIONLENS_CHAIN_UNIVERSE_SIZE` | `150` | Most-liquid symbols (by option volume) included in the chain universe besides watchlists and recently opened symbols |
| `OPTIONLENS_CHAIN_SOURCES` | `ibkr-bridge,questrade` | Enabled chain sources (no order: no source is preferred, §6 "Chain precedence"): `ibkr-bridge` = chains pushed by the bridge; `questrade` = the engine's own Questrade sweeps (§8), active once a token is pasted. Unknown names: the engine refuses to start |
| `OPTIONLENS_QUESTRADE_EXPIRIES` | `8` | Questrade sweeps: first N expiries (on/after today) |
| `OPTIONLENS_QUESTRADE_MONEYNESS` | `0.25` | Questrade sweeps: strikes within ±this fraction of the underlying price |
| `OPTIONLENS_QUESTRADE_LIVE` | `poll` | Default live mode until the user chooses one in Settings (`PUT /api/live/mode`, saved in `DATA_DIR/live_mode.json`, §9): `poll` = Level 1 snapshots of what is on screen at a cadence within the rate limits; `stream` = Level 1 WebSocket streams (Questrade: freezes the market data of any other IQ platform open at the same time). Anything else: the engine refuses to start |
| `OPTIONLENS_DATA_DIR` | `~/.local/share/optionlens` | Database directory; the app passes `<userData>/data` (macOS `~/Library/Application Support/OptionLens/data`, Windows `%APPDATA%\OptionLens\data`, Linux `~/.config/OptionLens/data`) |

Launch: `python -m optionlens.cli serve [--port N] [--exit-on-stdin-eof]`. Once the API is ready the engine
prints exactly one line `LISTENING <port>` on stdout (logs go to stderr); the app reads the port from it.
Stopping: the app starts the engine with `--exit-on-stdin-eof` and a stdin pipe, and closes that pipe to stop
it; the engine then shuts down gracefully (scheduler stopped, database closed) and exits with code 0. This is
the same on macOS, Linux and Windows (Windows has no SIGTERM); the app kills the process if it has not exited
after 8 s. SIGTERM (or SIGINT) also shuts it down gracefully on macOS and Linux. The parent-pid watchdog
uses `kill(pid, 0)` on POSIX and a process handle (`OpenProcess` + `WaitForSingleObject`) on Windows.
One engine per DATA_DIR (DuckDB is single-process and locks the file; on Windows the file cannot be
replaced while the engine has it open, so the app stops the engine before an import).

## 2. Authentication

- With a token configured, every request except `GET /api/health` must send
  `Authorization: Bearer <token>`. Missing/wrong → `401 {"detail":"unauthorized"}`. Comparison is
  constant-time. CORS preflights (`OPTIONS`) are answered without a token.
- `GET /api/health` (no auth) → `{"ok": true, "version": "<semver>", "auth": true|false,
  "server_id": "<stable random id stored in DATA_DIR>"}`.

## 3. (removed — no certificate pinning: the engine is loopback-only, plain HTTP)

## 4. CORS

The renderer is served from the custom protocol `app://optionlens`. The server answers CORS for the
origins in `OPTIONLENS_CORS_ORIGINS` (methods GET, POST, PUT, DELETE, OPTIONS; header Authorization,
Content-Type). No cookies.

## 5. Existing API

Unchanged from the API contract in optionlens-mac `src/api/types.ts` (same paths, same JSON), except:
- `ReportResult`, `Chain`, `Quote`, `ExpiryInfo` and per-row data may carry `source` where known:
  `"vendor" | "ibkr" | "questrade" | "cboe" | "nasdaq" | "model" | "reconstructed"`; `Chain.source` is the stored chain's.
- `Status` gains `sources: { name: string; ok: boolean | null; last_success_at: string | null; detail: string | null }[]`
  and `bridge: { connected: boolean; last_push_at: string | null; client: string | null; farms: {...} | null;
  options_subscription: string | null }` (`farms` and `options_subscription` as the last heartbeat sent them, §6;
  `farms` null while the bridge is not connected).
- `Chain.contracts_from_cache` / `Chain.contracts_listed_at`: the bridge built the stored chain from a contract list it
  saved earlier (IBKR's contract server was unavailable, §7), listed at that time; quotes are current, expiries listed
  since then are missing. The UI notes "contract list from <date> (IBKR contract server unavailable)".
- Errors are `{"detail": "<message>"}`; where the UI acts on an error the body adds a machine-readable `code`.
  Every endpoint that needs a stored chain (chain, activity, open-interest, contract, skew, implied-moves,
  straddle, ideas, `POST /api/calc/strategy`) answers a known symbol without one with
  `404 {"detail": "...", "code": "no_chain"}` (not fetched yet), or `"code": "no_options"` when a chain source
  confirmed the symbol lists no options. The app shows "fetching" / "connect IB Gateway" / "no options" for these
  instead of an error.
- `SymbolSummary.optionable` is tri-state: `true` once a chain was stored, `false` when a chain source (the IBKR
  bridge via `/api/ingest/no-options`, or a vendor source) confirmed no listed options, `null` = unknown.
- `Quote.after_hours` / `Chain.after_hours`: the stored quote / chain was collected after its session's closing data
  arrived (close + 20 min: evening, weekend, before the next open), i.e. from IBKR's frozen data of that session.
  `ReportResult.after_hours` / `MarketOverview.after_hours`: some rows' session values are an after-hours fill
  (§6 "After-hours fills"). The UI shows a neutral "Last session's close (collected after hours)" badge for them.
- `CalcRequest.spot` (optional, > 0): the renderer's live underlying price, used instead of the stored chain's;
  `CalcResult.legs[].osi`: the contract each option leg priced (null for stock legs), so the calculator can follow
  its live quote (§9).
- Current IV30 (`SymbolSummary.iv30` / `iv30_asof`, `Volatility.iv30` / `iv30_asof`, report and peer IV30 columns):
  the latest value of the merged IV history (§6 "IV history semantics") and its date. IV rank / percentile are
  of that same value, and only when it is from the last session (else null: stale, never shown as today's).

## 6. Ingest API (written by the IBKR bridge; all POST except `wants`, Bearer auth, JSON, idempotent)

Times are ISO-8601 with offset; dates `YYYY-MM-DD`; IV/percent as fractions; prices per share.

```
POST /api/ingest/chain
{ "source": "ibkr", "symbol": "AAPL", "fetched_at": "...",
  "final": false,                       // true = closing snapshot for session `session_date`
  "session_date": "2026-11-20",         // required when final
  "contracts_from_cache": false,        // optional (default false): the contract list is a saved one (§7)
  "contracts_listed_at": null,          // required when contracts_from_cache: when that list was listed
  "underlying": { "price": 349.1, "bid": 349.0, "ask": 349.2, "open": ..., "high": ..., "low": ...,
                  "prev_close": ..., "volume": 123, "ts": "...",
                  "rth_close": 348.7 },             // final chains: regular-session close of session_date
                                                    // (daily TRADES bar, useRTH); optional, null if unknown
  "contracts": [ { "expiry": "2026-12-18", "strike": 350.0, "right": "C",
                   "bid": 9.1, "ask": 9.3, "last": 9.2, "last_ts": "...",   // last_ts: when `last` traded (optional)
                   "volume": 812, "oi": 15321,
                   "iv": 0.251, "delta": 0.52, "gamma": 0.011, "theta": -0.12, "vega": 0.41 } ] }
→ 200 {"stored": n_contracts, "symbol": "AAPL"}
```
For `final` chains the engine uses `rth_close` (when given) as the underlying price of the snapshot: the
session's close in metrics, the archive's `underlying` and the moneyness reference. After 16:00 ET the live
`price` is an extended-hours trade and tick 9 is still the previous session's close, so neither is the
session's close. The bridge centres a closing chain's strikes on `rth_close` too. Greeks (`iv`, `delta`, ...)
are TWS's model values computed from the live underlying, i.e. possibly an after-hours price; the engine
stores them as sent.

**Computed IV and greeks.** Delayed data, and weekends especially, bring few TWS model computations. For a
contract without a source IV the engine solves one (Black-Scholes, `analytics.bs.implied_vol`) from the mid when
bid and ask are both > 0, else from `last` only when that trade is from the snapshot's session (`last_ts` at/after
its 09:30 ET open; without `last_ts`, a volume > 0). Inputs: the underlying price at the time of the quotes, `r`
from the rates table, `q` from the dividend-yield helper, T to 16:00 ET on expiry. The quotes' time is
`fetched_at` during the session, else the session's close (an evening or weekend snapshot holds the closing
quotes; the underlying is then that session's stored daily close when there is one). A price outside the
no-arbitrage bounds leaves the IV null. A contract without a source delta gets delta, gamma, theta and vega from
its IV (source or computed). Source values are never replaced and `theo` is never computed. Every row says where
its values came from — `OptionRow.iv_source`: the chain source (`"ibkr"`) | `"computed_mid"` | `"computed_last"` |
null; `OptionRow.greeks_source`: the chain source | `"computed"` | null — and the chain shows computed values
in italics with an explanation. `OptionRow.last_ts` is the last trade's time when the source gave it (a weekend
`last` can be days old). Reconstructed archives (option-bars) are labelled `computed_last` / `computed` the same way.

IV metrics never mix the two silently. ATM IV per expiry (`ExpiryInfo.atm_iv`, term structure, implied moves,
strategy probabilities) is computed from one IV layer per expiry: the source IVs when they give one, else the
computed ones (`ExpiryInfo.atm_iv_computed` / `TermPoint.atm_iv_computed` = true). The chain-wide metrics of a
session (IV30…IV360 constant-maturity interpolation, front ATM IV, 25-delta skews) use one layer for the whole
chain: the expiries with a source ATM IV when there is at least one, else the computed ones; the metrics row's
`iv_computed` says which (null: no IV at all). So IV30 is never interpolated between a source IV and a computed one.

Server builds OSI codes, stores through the same pipeline as any other chain source (latest chain,
quote, today's metrics row; `final` also writes the end-of-day archive for `session_date`). Which source's chain
is the session's chain follows "Chain precedence" below. The bridge posts only contracts that got
data, and sends `final: true` only when at least 95% of the contracts it requested were quoted; a less
complete closing chain is posted with `final: false` (live chain only, never archived) and retried later.

```
POST /api/ingest/no-options                // IBKR lists no options for the symbol (reqSecDefOptParams
{ "source": "ibkr", "symbol": "DELL" }    // returned no parameters / no future expiries)
→ 200 {"symbol": "DELL", "optionable": false}   // 404 for unknown symbols. A later chain push sets it true
                                                // again; stored data is kept. Such symbols leave the chain
                                                // universe (opening one still asks the bridge, §7)
POST /api/ingest/iv-history
{ "source": "ibkr", "symbol": "AAPL", "kind": "iv30",   // IBKR OPTION_IMPLIED_VOLATILITY daily close
  "bars": [ { "date": "2025-11-20", "value": 0.243 }, ... ] }
→ 200 {"stored": n}
POST /api/ingest/hv-history   (same shape, kind "hv30": IBKR HISTORICAL_VOLATILITY)     → 200
POST /api/ingest/index-bars               // IBKR daily TRADES bars of an IND contract (an index has no bid/ask)
{ "source": "ibkr", "symbol": "VIX", "bars": [ {"date": "...", "open":..,"high":..,"low":..,"close":..} ] }
→ 200 {"stored": n}   // SPX, NDX, RUT, XSP: stored as the index's daily bars (the one bar series every price,
                      // return, range, HV and chart computation of a symbol reads); VIX family: market series
POST /api/ingest/quotes
{ "source": "ibkr", "quotes": [ { "symbol": "VIX", "price": 27.8, "change": 3.26, "change_pct": 0.1327,
  "ts": "...",
  "bid": .., "ask": .., "open": .., "high": .., "low": .., "prev_close": .., "volume": .. } ] }   // optional
→ 200 {"stored": n}   // field-wise upsert: fields sent replace the stored quote's, others are kept
POST /api/ingest/heartbeat
{ "client": "optionlens-mac 0.1.0", "ibkr_connected": true, "account_type": "live"|"paper"|null,
  "farms": { "market_data": "ok", "historical": "ok", "sec_def": "down" } | null,   // optional; per farm
                       // "ok" | "inactive" | "connecting" | "down" | "unknown", as the Gateway reports them (§7)
  "options_subscription": "unknown" | "realtime" | "delayed_only" }               // optional (§7 "No OPRA")
→ 200 {"server_time": "..."}
```

```
POST /api/ingest/underlying-stats          // universe-wide per-underlying option stats (IBKR generic ticks
                                           // 100 option volume, 101 option OI, 104 HV, 106 IV on the stock)
{ "source": "ibkr", "session_date": "2026-11-20", "ts": "...",
  "stats": [ { "symbol": "AAPL", "price": 349.1, "change_pct": -0.0098, "volume": 4.1e7,
               "call_volume": 338000, "put_volume": 251000, "call_oi": 2960000, "put_oi": 2730000,
               "iv30": 0.251, "hv30": 0.27 } ] }
→ 200 {"stored": n}   // fills that session's metrics row fields where no chain-derived value exists
                      // (chain-derived values for the same session win); iv30 also feeds IV history.
                      // session_date must be the session of ts (400 otherwise, e.g. a fill queued before
                      // the open and sent after it); ts at/after 20:00 ET of session_date = after-hours fill
POST /api/ingest/option-bars               // historical daily bars of individual contracts (gap catch-up)
{ "source": "ibkr", "symbol": "AAPL",
  "bars": [ { "expiry": "2026-12-18", "strike": 350.0, "right": "C", "date": "2026-11-19",
              "open": .., "high": .., "low": .., "close": 9.2, "volume": 812 } ] }
→ 200 {"stored": n}   // stored as end-of-day rows with source "reconstructed" (bid/ask/oi null, IV and delta
                      // computed server-side from close + underlying close); never overwrite real rows.
                      // Pushes add to the session's reconstructed archive (same contract = replaced):
                      // the bridge pushes contract by contract
POST /api/ingest/refresh-result            // an on-demand refresh's state (§7): "pending" when the bridge accepted it,
{ "source": "ibkr", "symbol": "DELL",      // then its outcome: "refreshed" | "fresh" | "no-options" | "unavailable" |
  "status": "unavailable",                 // "failed" (RefreshResult)
  "message": "IBKR's contract server is unavailable; …", "waiting_for": "contract-server" }   // both optional
→ 200 RefreshStatus (§7)
GET /api/ingest/live-wants                 // the symbols on screen to snapshot now (§9): only while the bridge is
→ 200 {"symbols": ["AAPL", ...], "interval_s": 5}  // the live fallback (Questrade unavailable), else []. The bridge
                                           // polls it every 5 s while connected and runs one "live" task at a time
                                           // (a weighted share of TWS time, weight 6; never pre-empts), posting the
                                           // quotes to /api/ingest/quotes as the stats pass does
POST /api/ingest/ack-wants                 // the bridge accepted (queued) these wants
{ "kind": "option_bars", "items": [ { "symbol": "AAPL", "sessions": ["2026-11-18"] } ] }
→ 200 {"recorded": n_session_symbol_pairs}
```
Only accepted `option_bars` offers count: GET wants records nothing. A (session, symbol) is offered while it has
no archive or only a reconstructed (possibly partial) one, until it was accepted 3 times, and again only 6 h
after an accepted offer. The bridge keeps an accepted plan until every contract of it is done (per-contract
attempt ledger), so a reconnect or restart resumes it without repeating finished contracts.

**`delayed` flag.** `/api/ingest/chain`, `/api/ingest/quotes` and `/api/ingest/underlying-stats` bodies carry a
top-level boolean `delayed` (always present): true when at least one value in the push came from IBKR delayed
data (no real-time subscription for that contract class; the bridge uses reqMarketDataType(4), so subscribed
classes stay real-time). Unknown values are sent as null, never 0 (delayed data often lacks generic ticks
100/101/104/106, so call/put volume, OI, iv30 and hv30 may be null). The engine stores the flag with the data and
exposes it as `delayed: boolean` on `Quote` and `Chain` JSON so the UI can badge delayed data.

`GET /api/ingest/wants` → what the server would like the bridge to fetch now (the bridge polls it every
5 min while connected, and immediately after connecting). The bridge runs one task at a time, always
within IBKR pacing: on-demand refreshes (user-triggered) first — they even run between the market-data
batches of a longer task, and a running historical request (IV/HV history, index bars, option bars) is
aborted and re-queued (no attempt used) so the refresh does not wait behind a long download — and every other kind gets a weighted share of TWS time (eod_chains 8,
chain_universe 4, underlying_stats 3, index quotes/bars 2, option_bars 1, iv_history 1), so each kind with
work makes progress in every cycle.
```
{ "iv_history": [ {"symbol": "AAPL", "from": "2016-01-01"} ],   // IV30 series to fetch (see below)
  "hv_history": [ {"symbol": "AAPL", "from": "2026-11-20"} ],   // HV30 series, same rule per series
  "history_symbols": 836,                                       // symbols kept (wanted or already current)
  "underlying_stats": { "symbols": ["AAPL", ...], "max_age_s": 1800 },   // whole universe, open-20:00 ET; else
                                                                // an after-hours fill (below) with "as_of_session"
  "chain_universe": { "symbols": ["AAPL", ...], "max_age_s": 3600,       // watchlists + most liquid N (config,
                      "expiries": 6, "moneyness": 0.20 },                // default 150) + recently opened symbols;
                                                                // 09:00-20:00 ET on trading days; else an
                                                                // after-hours fill (below) with "as_of_session"
  "eod_chains": { "session_date": "2026-11-20", "symbols": ["AAPL", ...], "not_before": "...", "deadline": "..." },
                                                                // chain_universe + indices (SPX, NDX, RUT, XSP)
                                                                // after close+20m until 20:00 ET
  "option_bars": [ {"symbol": "AAPL", "sessions": ["2026-11-18"], "expiries": 6, "moneyness": 0.15} ],
                                                                // sessions without a real end-of-day archive
                                                                // (see ack-wants)
  "index_symbols": ["VIX", "VIX9D", "VIX3M", "VIX6M"],                // quotes
  "index_bars": [ {"symbol": "SPX", "from": "2016-11-21"} ],            // daily history of SPX, NDX, RUT, XSP and
                                                                // the VIX family: 10 years when missing, else from
                                                                // the day after the last stored bar
  "chains_on_demand_max_age_s": 120 }
```
**After-hours fills.** Outside those hours (evening, weekend, weekday before the open) the engine asks for the current
session's data (`session_date`: the last completed session) when it is missing — so a first launch on a weekend
populates the universe reports, dashboard, IV rankings and screeners instead of waiting for Monday:
```
"underlying_stats": { "symbols": [...], "max_age_s": 21600, "as_of_session": "2026-11-20" },
     // when > 20% of the universe has no metrics row for as_of_session: those symbols (else [])
"chain_universe": { "symbols": [...], "max_age_s": 21600, "expiries": 6, "moneyness": 0.20, "as_of_session": "2026-11-20" },
     // chain-universe symbols without a chain fetched since as_of_session's open
```
Symbols come in fill order: watchlists, recently opened, most liquid (stored option volume), the rest. `max_age_s`
6 h: a fill is pushed once; a symbol IBKR sent nothing for is asked again 6 h later. The bridge runs them on its
normal scheduler (on-demand still first) right after connecting, the stats pass before the chains (it takes a few
minutes for the whole universe, and its option volumes rank the chain universe): it asks for the chains again once
no stats work is left. The session-wide market data type 4 (delayed-frozen) makes TWS answer with the session's
frozen values; generic ticks 100/101/104/106 are often absent then and are posted as null (never 0) — current IV30
then comes from the IV history (§5). The stats push uses `session_date` = `as_of_session`. The bridge status reports
`populate: {sessionDate, done, total}` ("Populating last session's data: 340/1145" in Settings, Status and empty
views) while a fill runs.

Precedence of one session's `metrics_daily` row (jobs/chains.py), with `after_hours` = the row is a fill:
- the session's own data — chains fetched before close + 20 min or a closing snapshot (`final`), stats with `ts`
  before 20:00 ET — replaces a fill, and among itself follows "Chain precedence" below (chain-derived values win
  over stats);
- a fill (a non-final chain fetched later, stats with `ts` at/after 20:00 ET) only creates the row of a session
  that has none, or refreshes an earlier fill; it never touches a row of the session's own data, not even its
  NULL fields. Its close, change and volume come from the session's stored daily bar when there is one (a
  frozen stock price can be an extended-hours trade);
- `option_latest` and `quotes_latest` always hold the newest data (`after_hours` on Chain / Quote says when it
  was collected); `option_eod` (the archive) is written only from a closing snapshot (`final`), never from a fill.

**Chain precedence** (jobs/precedence.py). IBKR and Questrade are equal sources: whichever chain is better is used, no
source is preferred. One rule decides the served chain (`option_latest`, what every chain endpoint reads), the session's
metrics row (within one tier: own data / fills) and its archive (`option_eod`). A chain of a later session always
replaces the served one. Within one session, two non-final chains fetched more than 15 min apart are not the same
snapshot: the fresher one wins. Otherwise: (1) a closing snapshot (`final`) beats a chain that is not one; (2)
real-time data beats delayed or unknown (`delayed`); (3) more quoted contracts — an ask > 0 and some bid, OI or volume,
i.e. the rows the archive keeps — wins; (4) the fresher fetch wins. The same chain stored again (same source and fetch
time) replaces itself. Stored for the rule: `option_latest.final`, `metrics_daily.chain_final` / `chain_quoted` /
`chain_fetched_at`, `option_eod.delayed` / `fetched_at` (rows from before a column existed count as unknown: intraday,
not real-time, fetched long ago). A reconstructed archive never replaces a real one. Every row keeps its `source`.
`quotes_latest` holds the newest quote whatever chain is served. `eod_chains` (wants) leaves out a session's symbols
whose archive is real-time (an IBKR closing chain could win only with more quoted contracts) and those a healthy
server-side source covers ("Splitting chain work" below).

**IV across sources.** Any source's IV for a contract in the session comes before a computed one. Before the engine
computes IV for a contract without a source IV, it takes the IV (and the greeks, when the contract has none) that
another source sent for the same contract in the same session (the served chain, fetched since the session's open),
keeping that source's `iv_source` / `greeks_source`. And when a chain loses to the served one of the same session,
the served chain takes the losing chain's source IVs (with its source greeks, when it has them) for its contracts
whose IV is computed or missing; its metrics row and archive are rebuilt from it where they hold that chain. So
computed values appear only where no source has an IV.

**Splitting chain work** (jobs/bridge_plan.py `for_bridge`). While a server-side source (Questrade) is healthy —
connected, not needing attention (§8 "Errors"), and not failing (more than half of its last 50 fetches failed,
jobs/source_health.py) — it sweeps the whole universe itself, so `chain_universe` (also its after-hours fill) and
`eod_chains` ask the bridge only for the symbols that source did not cover: its last attempt for the symbol failed, or
it does not list the symbol (a symbol it was not asked yet counts as covered). IV/HV history, underlying stats, index
quotes / bars and option-bar catch-up stay the bridge's: only IBKR provides them. When Questrade is unavailable or
failing, the wants give the bridge the full lists again at the next poll.

The underlying-stats pass also posts each stock's quote (last or mid, bid/ask, open/high/low, previous close,
volume) to `/api/ingest/quotes` from the same streams. Without a quote of the latest session (none pushed,
weekends, no chain), `SymbolSummary.quote` and the universe tables (`price`, `volume`, OHLC, change) show the
latest daily bar: its close, OHLC and volume and the change vs the previous close, with `source` the bars'
source (`"nasdaq"`, `"ibkr"` for the cash indices) and `ts` that session's close.

Coverage: without Questrade there is no free, licensed source of full option chains for the whole universe that
this user can use (CBOE's website feed is blocked and not licensed for collection; Alpaca does not serve Canadian
residents): chains then come from the bridge for the chain universe only. With Questrade connected (§8) the engine
sweeps the whole universe itself. Reports that need full chains (strategy screeners, option screener, OI changes,
large volume contracts) cover the symbols with a stored chain; their `notes` say so. Universe-level reports (unusual volume,
IV rankings, put/call, broad view) use underlying-stats for every symbol.

History wants: a series (IV30 or HV30, each on its own) is wanted from 10 years back only when the symbol has no
backfilled history of it — none, or one spanning under 30 days; the IV30 values that underlying-stats pushes add
to the history are single days (stored with source `<source>:stats`) and never count as one. A backfilled series
is wanted from the day after its last point when that predates the last completed session, and not at all when
current. The bridge shows IV/HV backfill progress over all `history_symbols` (two series each): series already
current, or attempted within the last 20 h, count as done, so a restart or update does not reset the count.

IV history semantics: values ingested via `/api/ingest/iv-history` populate a per-symbol daily IV30
series used for IV rank/percentile, vol history charts and the model-pricing IV proxy, wherever the
server's own collected IV30 for that date is missing (collected values win; both are kept, tagged by source).

## 7. Client-triggered refresh

**One refresh round from every connected source** (services/refresh.py). When the user opens a symbol page (or clicks
Retry) the renderer calls `POST /api/symbol/{sym}/refresh`: the engine starts a round, fetches its own connected
sources (Questrade) in the background, and marks IBKR pending while the bridge's heartbeat says it is connected. The
renderer then asks the bridge (renderer → main) for the IBKR chain; the bridge reports `pending` and then its outcome
to the same round (`POST /api/ingest/refresh-result`). Each chain is stored as it arrives, so the first one is served at
once and a better second one replaces it (§6 "Chain precedence"). A round still pending is joined, not repeated; a
source that delivered within 2 min is not asked again; an IBKR attempt without a report after 5 min counts as failed.
A source that is set up but unusable (e.g. Questrade needing attention) is listed as failed with its reason, without a
call; a source not set up is not listed. Chain endpoints themselves fetch nothing (they record the visit).

`GET /api/symbol/{sym}/refresh` (and the POST's answer) → `RefreshStatus` (types.ts):
`{ symbol, started_at, pending, sources: [{ name: "ibkr" | "questrade", state: "pending" | "ok" | "no_options" |
"unavailable" | "failed", message, waiting_for, reason, at }], served: { source, fetched_at, delayed } | null }`
(`reason`: the short form of a failure, e.g. "403 code 1016 (missing market-data permission)"). The renderer polls it
every 1.5 s while a source is pending and reloads the page each time a source delivered. The wording is source-neutral:
"Fetching chain (IBKR, Questrade)…", then the served chain's source badge; a source is named only when it is the one with
a problem and no other source delivered ("IBKR contract server unavailable; Questrade: missing market-data
permission").

The on-demand refresh runs at any time, also outside market hours and on weekends: the bridge requests
market data type 4 (delayed-frozen), so TWS answers with the last values of the session. Its result
(`RefreshResult`, electron/shared/api.ts, also reported to the round) is `refreshed` / `fresh` / `no-options` (IBKR lists no options:
the bridge POSTed `/api/ingest/no-options`) / `unavailable` (bridge off, not connected or paused; asked again
when the bridge connects while the page is open) / `failed`. After `refreshed` or `no-options` the page reloads.

Chain fetch details (bridge): the first 8 expiries listed by `reqSecDefOptParams` (underlying symbol, futFopExchange
"", secType STK/IND, the underlying's conId; SMART rows), strikes within ±25% of spot. The option parameters are kept
per underlying for the New York day; an answer without rows (the end message alone) means IBKR lists no options
(`no-options`, at once). When TWS does not answer within 60 s the request is sent once more with a 120 s wait. Every
listed contract of every picked expiry is posted, those TWS sent nothing for with null fields (shown as "no quote"); an
expiry whose contract list TWS did not deliver is skipped, not fatal. The bridge status names the gaps of the last chain
("Incomplete IBKR chain for DELL: 62% of contracts quoted; no quotes for 2026-11-27; no contracts from IBKR for …").
Frozen / weekend quotes: an ask ≤ 0 is no quote, a 0 bid counts only against a real ask (−1 is always no quote).
`contracts[].last_ts` is the time of the last trade (ticks 45 / 88) when TWS sends it.

**IBKR server farms.** IB Gateway reports its connections to IBKR's server farms as connection-level messages
(reqId −1; @stoqey/ib emits them as `info` events): 2104/2103 market data OK/broken, 2108 market data inactive,
2119 connecting, 2106/2105 HMDS (historical) OK/broken, 2107 HMDS inactive, 2158/2157 sec-def OK/broken (the farm
is taken from the message text where it names one). The bridge keeps each farm's state (worst of its servers) in its
status (`farms`, Settings / Status: "IBKR servers") and the heartbeat. The sec-def farm answers contract details and
option parameters; on weekends IBKR often takes it down for maintenance while market data and HMDS keep working.

**Saved contract lists.** After each resolution the bridge saves, per underlying (in the app's data dir,
`contract-cache/<SYMBOL>.json`): the underlying's conId, the option parameters (expiries, trading classes, multipliers,
strikes) and, per expiry, the resolved option contracts (conId, expiry, strike, right, trading class) with when they
were fetched. While the sec-def farm is reported broken or connecting, or for 15 min after a contract request went
unanswered (then the next request is the one probe), the bridge asks IBKR for no contract definitions: a chain is built
from the saved list when it is at most 7 days old (expired expiries dropped; expiries listed since then are missing)
and its quotes are requested by conId on SMART (the market-data farm serves them). Such a push carries
`contracts_from_cache: true`. Without a usable saved list, chain work (universe, closing chains, option-bar catch-up)
is parked — not failed, not retried — until the Gateway reports the sec-def farm OK (2158) or the 15 min probe time;
an on-demand refresh answers `unavailable` with `waitingFor: "contract-server"` ("IBKR's contract server is
unavailable; will fetch automatically when it's back") and the page asks again once the bridge reports
`contractsAvailable`. Settings / Status: "IBKR sec-def server: unavailable since 18:42 (often weekend maintenance) —
chains use saved contract lists".

**No OPRA (US options delayed only).** Without an OPRA subscription TWS sends delayed option quotes during the regular
session but refuses delayed-frozen option data outside it (354 on a weekend). After a 354 / 10089 / 10090 / 10168 on
option quotes the bridge records `options_subscription: "delayed_only"` (persisted, `bridge-options-feed.json`). Then,
outside 09:30–16:00 ET (and on a day even delayed option data was refused, e.g. a holiday), no option market data is
requested: closing and universe chains are not queued, and an on-demand refresh answers `unavailable` with
`waitingFor: "market-hours"` ("Option quotes update during market hours (no real-time options subscription); showing
the last stored chain"; a neutral chip, not a failure). During the session option quotes are requested as delayed data
(market data type 3); the day's first option request is a probe with type 4, and real-time option quotes end the mode
(`realtime`). Stock-side work (underlying stats, quotes, IV/HV history, index bars) is unaffected. Settings / Status:
"US options: delayed only (no OPRA subscription)".

## 8. Questrade (server-side chain source)

The engine fetches option chains and underlying quotes itself from the user's Questrade account
(`engine/optionlens/sources/questrade/`), next to or instead of the IBKR bridge. Off until the user pastes a token.

**Verified in Questrade's API documentation** (questrade.com/api/documentation, read 2026-11-22):
- *Getting started / Security*: a personal app is registered in the API Centre; "New manual authorization" →
  "Generate new token" gives a refresh token. It is redeemed at `https://login.questrade.com/oauth2/token` with
  `grant_type=refresh_token&refresh_token=…` (the Security page shows a POST with a form body; the engine sends the
  token that way, never in a URL). The answer has `access_token`, `token_type` "Bearer", `expires_in` (samples: 300
  and 1800 s), a new `refresh_token` and `api_server` (samples with and without a trailing `/v1`); calls go to
  `<api_server>/v1/...` with `Authorization: Bearer`. API Centre → Revoke (or `oauth2/revoke`) cancels a token.
- *Rate limiting*: market data calls (`markets`, `markets/quotes/:id`, `markets/candles/:id`, `symbols/:id`,
  `symbols/:id/options`) allow 20 requests/s and 15,000/h (account calls 30/s, 30,000/h). Limited responses carry
  `X-RateLimit-Remaining` (requests left against the current limit) and `X-RateLimit-Reset` (Unix time it expires);
  over the limit: HTTP 429 (error code 1006) with the same headers.
- *Error handling*: `{"code", "message"}`; the full table is in `sources/questrade/errors.py` (404 1001 invalid
  endpoint, 400 1002-1004, 413 1005, 429 1006 rate limit, 500 1007-1009, 502 1010, 503 1011, 405 1012, 400 1013,
  401 1014 missing authorization header, 400 1015 malformed authorization header, 403 1016 "Request is out-of-allowed
  OAuth scopes", 401 1017 access token invalid, 404 1018 account / 1019 symbol / 1020 order not found, 500 1021).
- *symbols/search* (`prefix`) → symbols (`symbol`, `symbolId`, `securityType` Stock/Option/Index/…, `listingExchange`,
  `currency`); *symbols/:id* → `prevDayClosePrice`, `hasOptions`, …; *symbols/:id/options* → per expiry
  (`expiryDate`) per root (`optionRoot`, `multiplier`) per strike (`strikePrice`, `callSymbolId`, `putSymbolId`);
  *markets/quotes?ids=* → Level 1 quotes (`bidPrice`, `askPrice`, `lastTradePrice`, `lastTradePriceTrHrs` = last
  regular-hours trade, `volume`, open/high/low, `delay`, `isHalted`); *markets/quotes/options* (POST) → per option
  `bidPrice`, `askPrice`, `lastTradePrice`, `lastTradeTime`, `volume`, `openInterest`, `volatility` (sample
  52.374257: percent), `delta`, `gamma`, `theta`, `vega`, `rho`, `delay` ("how much is data delayed"). Request body:
  `filters` [{`underlyingId`, `expiryDate` (both required), `optionType`, `minstrikePrice`, `maxstrikePrice`}] — "if
  you need multiple expiries, specify multiple filters" — and/or option ids.
- *Real-time vs delayed*: "Questrade user needs to be subscribed to a real-time data package, to receive market
  quotes in real-time, otherwise call to get quote is considered snap quote and limit per market can be quickly
  reached … the response will return delayed data. (Please check "delay" parameter in response always)".
- Some field tables and samples disagree (search results under `symbols` / `symbol`; chain under `options` /
  `symbols`, root under `optionRoot` / `root`; option quotes under `optionQuotes` / `quotes`): the engine reads both,
  and `optionChain` for the chain. Not stated in the docs, so not relied on: the refresh token's lifetime and
  single use (each redemption returns a new token and the engine always switches to it; reportedly an unused token
  lapses after about 7 days, hence a daily keep-alive refresh), theta / vega units (stored as sent, like IBKR's), and
  index symbology (an `Index` search result whose name before any `.` suffix is the symbol, e.g. SPX; untested
  against the live API).

**Errors** (errors.py, client.py). Every failed call is a `QuestradeError` with a class and a grouping reason
("403 code 1016 (missing market-data permission)"):
- *auth / permission*: HTTP 401 after the one token refresh, any 403, codes 1014-1017. Every further call would fail the
  same way, so the source **needs attention** at once (circuit breaker): state `needs_attention` (persisted with the
  token), the running sweep or refresh stops asking (at most the calls already in flight), nothing is retried, and no
  Questrade job runs — sweeps, fills, on-demand refreshes, the keep-alive — until a new token is saved. The detail says
  what to do; for 1016: "Your Questrade app lacks the 'Retrieve delayed and real-time market data' permission. In
  Questrade's API Centre enable it for your personal app, generate a new manual token, and paste it here." Meanwhile the
  bridge's chain work resumes its full role (§6 "Splitting chain work").
- *rate limit*: 429 / 1006: wait for the reset (Pacing), then retry.
- *transient*: 5xx, timeouts, connection errors: client backoff, then the sweep's one retry pass.
- *other* (e.g. 404 1019): that symbol fails; not retried.
Failure reasons are counted per source over 24 h (jobs/source_health.py) and shown grouped: `QuestradeStatus.failures`
(`[{reason, count}]`, top 3), the sweep detail ("failures: 1,141 × 403 code 1016 (missing market-data permission)")
and the Status line. The IBKR source shows its own the same way (`BridgeStatus.failures`).

**Connection** (`POST /api/sources/questrade/token {"refresh_token": "…"}` → `QuestradeStatus`, `DELETE` same path,
`GET /api/sources/questrade`; types.ts `QuestradeStatus`). The engine redeems the pasted token at once (422
`code: "token_rejected"` when Questrade refuses it, `"upstream"` when its login is unreachable; nothing is stored
then) and stores the rotated refresh token in `DATA_DIR/secrets/questrade.json` — written atomically (temp file,
fsync, rename), mode 0600 in a 0700 directory (Windows: the user profile's default ACL) — before the access token is
used, on every refresh. Tokens are never logged, returned or put in URLs. The access token is refreshed 60 s before
`expires_in` runs out and after a 401 (one retry). `state`: `disconnected` | `connected` | `expired` (Questrade
refused the stored refresh token: "paste a new one"; no further logins until then) | `error` (login unreachable or
the file could not be saved; the token is kept, calls fail fast for 60 s, then retry) | `needs_attention` (see
"Errors"). Saving a token clears `needs_attention` and the earlier failure counts, then checks it with one market-data
call (`symbols/search?prefix=SPY`): `verification` = `{ok: true, message: "Token OK: market data permitted"}` or the
classified error (e.g. the 1016 text above, the source needing attention again); only a passed check starts the
pending sweep (in the session) or after-hours fill. Settings → Data sources shows IBKR and Questrade side by side with
the same fields — state, data (real-time / delayed), last chain, last sweep, top failure reasons, usage (calls this
hour / market-data lines) — plus, for Questrade, the token check, a password field and the API Centre steps; Status lists
`questrade` in `sources` (detail: state · data type · failures · calls this hour · last sweep).

**Pacing** (ratelimit.py): at most 70% of each documented market-data limit — calls spaced 1/14 s, at most 10,500 in
any rolling hour — and of the headers' current limit: when calls used against it (documented limit − remaining)
reach 70%, calls pause until its reset (the hourly window when the reset is > 2 s away, else the per-second one; the
docs do not say which the headers describe). A 429 pauses until the reset (else 5 s) and retries, up to 5 times;
5xx / timeouts back off 0.5 s · 2^n, 3 times.

**Fetching one chain**: symbol id (`symbols/search`, cached in `DATA_DIR/questrade_symbols.json`; not-found answers
re-asked after 7 days and the symbol is skipped, never marked unoptionable), `symbols/:id` once per New York day
(`hasOptions` false → no options), `markets/quotes?ids=`, `symbols/:id/options`, and one `markets/quotes/options`
POST with one filter per expiry (first `QUESTRADE_EXPIRIES` on/after today, strikes within ±`QUESTRADE_MONEYNESS`
of the price): 3 calls per chain (plus 2 per symbol per day). Mapping: OSI from root/expiry/right/strike (standard
roots and 100 multiplier only; SPX/NDX/RUT keep the PM root where an expiry has both); every listed contract in the
band is stored, unquoted ones with null fields; prices ≤ 0 are no price, a 0 bid counts only against a real ask;
volume / OI as reported (null when absent); `volatility` / 100; greeks null without a volatility; `delayed` when the
underlying's or any option's `delay` is set. A closing snapshot takes the underlying's `lastTradePriceTrHrs`. Rows
are tagged `source: "questrade"`; missing IV / greeks are borrowed or computed as for pushed chains.

**Schedule** (jobs/scheduler.py; each run skips Questrade when it is not connected): `sweep` every
`SWEEP_INTERVAL_MIN` (60) from 09:45 to the close; `sweep_final` at close + 20 min (until 20:00 ET, also as a
catch-up after a late start): the closing snapshot, archived per "Chain precedence"; `sweep_fill` hourly at :35
outside the session (evening, weekend, before the open): an after-hours fill of the universe symbols without a chain
fetched since the current session's open, watchlists / recently opened / most liquid first (the bridge's fill order) —
never archived, never over the session's own data; it also refreshes an idle token daily. Coverage is a stored chain
(or a confirmed "not listed" / "no options", asked again after 6 h), never an attempt: a symbol whose fetch failed is
asked again 30 min later, and the run says "N symbols … failed recently" instead of "covered". Swept: the universe plus SPX, NDX, RUT, XSP. With ~1,150 symbols a sweep is ~3,500 calls (the day's first ~5,800
with the daily symbol info), under the 10,500/h budget; the on-demand refresh of an opened symbol also uses Questrade
when it is connected (§7).

---------------------------------------------------------------------------------------------------
## 9. Live quotes (everything on screen updates while the market is open)

**Verified in Questrade's API documentation** (questrade.com/api/documentation, "Streaming", "Rate limiting",
"markets/quotes/:id", "markets/quotes/options"; read 2026-11-24):
- L1 streaming: "you can use either WebSocket or RawSocket"; "You must specify stream=true (or '"stream": true' in
  JSON) and then the mode": `GET v1/markets/quotes?ids=…&stream=true&mode=WebSocket` for equities, and **options can
  be streamed** the same way: `POST v1/markets/quotes/options` with `"stream": true, "mode": "WebSocket"` and `filters`
  and/or `optionIds` (also `markets/quotes/strategies`). The answer is `{"streamPort": N}`; "Ports for L1 are separate
  but still remain same for the same URL within the same day".
- Handshake: on a WebSocket "send the access token as a message without any delimiters"; "Do not use the word Bearer
  in sockets"; a recognized token is answered `{"success": true}` (RawSocket: token + a space within 5 s).
- Session: "Keeping a socket open does not extend your session. You need to send requests at least every 30 minutes
  otherwise your socket will get disconnected when the session expires."
- "If you use L1 Streaming in Trading API, then your market data will freeze in any other IQ Platform that you are using
  simultaneously." — hence polling is the default and streaming opt-in (Settings switch / `PUT /api/live/mode`).
- Rate limits: market-data calls 20/s and 15,000/h (§8); the table lists `markets/quotes/:id` but not
  `markets/quotes/options` or how streams count, so every quote and stream-port request goes through the same 70% gate.
- Snap quotes: without a real-time data package "call to get quote is considered snap quote and limit per market can be
  quickly reached … the response will return delayed data (check "delay")". The user's account answers `delay` 0
  (real-time), verified live during development.
- **Verified live** (v0.1.12 on a real-time account, 2026-11-24, during market hours): Questrade allows **one streaming
  connection per session**. The equity stream request (`GET markets/quotes?…&stream=true&mode=WebSocket`) and the option
  stream request (`POST markets/quotes/options {"optionIds", "stream": true, "mode": "WebSocket"}`) both handshake and
  answer the **same `streamPort`** (18101); **one connection carries both message kinds**, `{"quotes": [...]}`
  (equities: `symbol`, `symbolId`, `tier`, `bidPrice`, `bidSize`, `askPrice`, `askSize`, `lastTradePriceTrHrs`, …) and
  `{"optionQuotes": [...]}` (options: `underlying`, `underlyingId`, `symbol` e.g. `DELL27Nov26C285.00`, `symbolId`,
  `bidPrice`, `bidSize`, …); **opening a second connection closes the first** (`1005`, no status). So the stream requests
  only register ids on the session's port; v0.1.12's two connections kicked each other off (3 drops in 5 min → polling).
- **Not documented** (so not relied on, or marked unverified in code): the maximum symbols per stream or per quote
  call, keep-alive / ping behaviour, the socket URL path (the engine connects to the first stream request's path:
  equities' when any are wanted), and whether stream messages carry only changed fields (they are merged per symbolId,
  as client libraries do). Quote calls carry at most 100 ids (our choice).

**Architecture** (engine `optionlens/live/`, renderer `src/live/`): one subsystem behind a narrow contract.
- `POST /api/live/subscribe {client_id, symbols[], option_contracts[] (OSI), chains[{symbol, expiry, strike_min,
  strike_max}]}` replaces that window's set (≤ 500 symbols, 1,000 contracts, 8 chain views; chain views resolve to the
  served contracts of that expiry in the band). → `{client_id, symbols, contracts, status}`.
- `GET /api/live/stream?client_id=` (text/event-stream, same Bearer token; the renderer reads it with fetch streaming,
  so no token in a URL): `event: status` `{state, source, session, mode, interval_s, symbols, contracts, detail}` and
  `event: patch` `{quotes: {SYM: {price, change, change_pct, bid, ask, open, high, low, prev_close, volume, delayed,
  source, ts}}, options: {OSI: {bid, ask, mid, last, volume, oi, iv, delta, gamma, theta, vega, delayed, source, ts}}}`
  with only changed fields; a `: keep-alive` comment every 15 s. A window without an open stream for 30 s is dropped.
  `GET /api/live/status` is the current status.
- `state`: `live` (data flowing, real-time) · `delayed` (flowing, `delay` set) · `connecting` · `stale` (no success for
  max(10 s, 3 × the poll interval), detail = the last error) · `market_closed` (outside 04:00–20:00 ET on NYSE trading
  days, or only options wanted outside 09:30–close; nothing is fetched) · `not_connected` (no usable source) · `idle`
  (nothing subscribed). Extended hours (04:00–09:30, close–20:00) keep underlyings live, never options.
- Source: Questrade while it is usable (connected, not needing attention); when it is unavailable, or failing for 30 s,
  and the IBKR bridge is connected, the bridge snapshots the visible symbols (`live-wants`, §6; option cells keep the
  stored chain) — the Questrade driver keeps retrying and takes over again on its first success.
- The hub keeps exactly the union of all windows' sets live (Registry/diff), coalesces to at most one update per key per
  250 ms per window, keeps the latest values in memory and persists the changed ones every 60 s: `quotes_latest`
  field-wise (`source` the live source) and the served `option_latest` rows' market fields (`iv_source` /
  `greeks_source` relabelled when an IV / greeks arrive; the chain's fetch time, source and `delayed` unchanged, so
  "Chain precedence" is unaffected). Never `option_eod`: archives come only from the closing snapshot job. The chain
  cache of patched symbols is dropped. On shutdown open streams are ended first (uvicorn would wait for them).
- Polling cadence (`live/questrade.py`): one round = ⌈symbols/100⌉ GET `markets/quotes` + ⌈contracts/100⌉ POST
  `markets/quotes/options {"optionIds"}`; every `max(2 s, calls / ((10,500 − 6,000)/3600 s))`: live polling uses at
  most the 4,500/h left after a 6,000/h reserve for the sweeps (the day's first sweep ~5,800), all within the 70% gate
  (10,500/h, 14/s). E.g. a quote header: every 2 s; a chain window of ±10 strikes (42 contracts) + its underlying: 2 s;
  300 contracts + 1 underlying (4 calls): 3.2 s; 1,000 contracts (11 calls): 8.8 s. A changed set is polled at once
  (at most once a second). Ids: `symbols/search` (cached), `symbols/:id` once a day (previous close), `symbols/:id/options`
  once per underlying per day (option ids).
- Live mode (`live/mode.py`): `GET /api/live/mode` → `{mode: "stream"|"poll", default, saved}`; `PUT /api/live/mode
  {"mode"}` saves it in `DATA_DIR/live_mode.json` and puts it into effect at once (streams opened / closed, no engine
  restart). `OPTIONLENS_QUESTRADE_LIVE` is only the default until the user chooses (default `poll`). Settings → Data
  sources → Questrade: switch "Stream live quotes (tick-by-tick)" (on = stream, off = poll), Questrade's freeze warning
  under it, and the live status line (from `GET /api/live/status`).
- Streaming mode: ONE stream connection for the union of underlyings and contracts (Questrade allows one per session,
  see above; no per-stream limit is documented or observed, so the set is not split). Opening it: the stream request of
  each wanted kind (registering its ids; all answered ports must be equal, else "handshake failed"), then one socket;
  messages are dispatched per key (`quotes` → underlyings, `optionQuotes` → contracts; a message may hold both). A
  changed set (1 s debounce) reopens it: the old socket is fully closed first, then both stream requests are re-issued
  and one socket connects (never two at once). Renewed (new stream requests with a fresh token) 60 s before the access
  token expires and within 2 s after it was refreshed elsewhere (e.g. by a sweep); reopened after a drop with backoff
  (1, 2 s). **Falls back to polling** — status `mode: "poll"`, `fallback: <reason>`, header "LIVE · Questrade (polling
  fallback: <reason>)" — when a handshake fails (port request, socket or token not answered `{"success": true}`), when a
  the stream sends no message within 15 s of its handshake in the regular session, or after ≥ 3 drops in 5 min; streaming
  is retried every 5 min. Diagnostics: the handshake result and the first 3 raw messages of every connection are logged (label `live stream`)
  (INFO, ≤ 300 chars, fields named like token / session / account / key masked; the access token never logged);
  `GET /api/live/status` adds `questrade: {setting, streaming, fallback, fallback_detail, retry_at, messages_per_s (10 s
  window), last_message_at, drops_recent, streams: [{ids: {equity, option}, open, port, opened_at, last_message_at,
  messages, connects}]}` (`streams`: the one connection, empty while not streaming). Unverified against the live API: whether Questrade sends an initial snapshot after subscribing (if not,
  a quiet stream in the regular session falls back to polling after 15 s).
- Renderer: `useLive(subscription)` registers what a view shows (refcounted; the window's union is posted, debounced
  250 ms); `useLiveQuote(symbol)` / `useLiveOption(osi)` / `useLiveCell` read values; `merge.ts` patches Quotes,
  OptionRows, report cells and the last daily bar. A changed value gets a 600 ms static tint (no animation, no layout
  shift; same with prefers-reduced-motion). A hidden or minimized window (Electron background throttling) unsubscribes
  and closes its stream. What updates: the symbol header quote; the chain (visible expiry and strike window only); the
  calculator (live spot, legs' live mid / IV for legs without a typed price, recomputed via `/api/calc/strategy` at most
  once a second); watchlist tables; dashboard index cards, gainers / losers / watchlist rows; report pages (price,
  change, bid / ask / last / mid, volume, IV / greeks of option rows on the current page; the report re-runs quietly every
  45 s while live, keeping sort, filters, page and scroll); the chart's last candle (at most once a second).
  History-based metrics (IV rank, HV, returns, backtests) are not live.
