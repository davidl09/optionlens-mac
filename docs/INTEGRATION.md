# OptionLens app ⇄ engine integration contract (authoritative)

Two parts, one machine:
- **engine** (this Python code; FastAPI + DuckDB + in-process scheduler) — the app's embedded backend.
  The desktop app launches it as a local sidecar process bound to loopback. Collects data (Nasdaq
  prices/calendars, Treasury rates; option data pushed by the IBKR bridge or an optional vendor source),
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
| `OPTIONLENS_CHAIN_SOURCE` | `none` | Optional server-side vendor chain source (none by default) |
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
  `"vendor" | "ibkr" | "cboe" | "nasdaq" | "model" | "reconstructed"`.
- `Status` gains `sources: { name: string; ok: boolean | null; last_success_at: string | null; detail: string | null }[]`
  and `bridge: { connected: boolean; last_push_at: string | null; client: string | null }`.
- Errors are `{"detail": "<message>"}`; where the UI acts on an error the body adds a machine-readable `code`.
  Every endpoint that needs a stored chain (chain, activity, open-interest, contract, skew, implied-moves,
  straddle, ideas, `POST /api/calc/strategy`) answers a known symbol without one with
  `404 {"detail": "...", "code": "no_chain"}` (not fetched yet), or `"code": "no_options"` when a chain source
  confirmed the symbol lists no options. The app shows "fetching" / "connect IB Gateway" / "no options" for these
  instead of an error.
- `SymbolSummary.optionable` is tri-state: `true` once a chain was stored, `false` when a chain source (the IBKR
  bridge via `/api/ingest/no-options`, or a vendor source) confirmed no listed options, `null` = unknown.
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
quote, today's metrics row; `final` also writes the end-of-day archive for `session_date`). A chain from
IBKR wins over a chain from any other source for the same session. The bridge posts only contracts that got
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
{ "client": "optionlens-mac 0.1.0", "ibkr_connected": true, "account_type": "live"|"paper"|null }
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
                      // (chain-derived values for the same session win); iv30 also feeds IV history
POST /api/ingest/option-bars               // historical daily bars of individual contracts (gap catch-up)
{ "source": "ibkr", "symbol": "AAPL",
  "bars": [ { "expiry": "2026-12-18", "strike": 350.0, "right": "C", "date": "2026-11-19",
              "open": .., "high": .., "low": .., "close": 9.2, "volume": 812 } ] }
→ 200 {"stored": n}   // stored as end-of-day rows with source "reconstructed" (bid/ask/oi null, IV and delta
                      // computed server-side from close + underlying close); never overwrite real rows.
                      // Pushes add to the session's reconstructed archive (same contract = replaced):
                      // the bridge pushes contract by contract
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
  "underlying_stats": { "symbols": ["AAPL", ...], "max_age_s": 1800 },   // whole universe, during the session
  "chain_universe": { "symbols": ["AAPL", ...], "max_age_s": 3600,       // watchlists + most liquid N (config,
                      "expiries": 6, "moneyness": 0.20 },                // default 150) + recently opened symbols;
                                                                // 09:00-20:00 ET on trading days, else []
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
The underlying-stats pass also posts each stock's quote (last or mid, bid/ask, open/high/low, previous close,
volume) to `/api/ingest/quotes` from the same streams. Without a quote of the latest session (none pushed,
weekends, no chain), `SymbolSummary.quote` and the universe tables (`price`, `volume`, OHLC, change) show the
latest daily bar: its close, OHLC and volume and the change vs the previous close, with `source` the bars'
source (`"nasdaq"`, `"ibkr"` for the cash indices) and `ts` that session's close.

Coverage: there is no free, licensed source of full option chains for the whole universe that this user can
use (CBOE's website feed is blocked and not licensed for collection; Alpaca does not serve Canadian
residents). Server-side scheduled chain sweeps therefore run only if a vendor ChainSource is configured
(none by default). Reports that need full chains (strategy screeners, option screener, OI changes, large
volume contracts) cover the chain universe; their `notes` say so. Universe-level reports (unusual volume,
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

When the user opens a symbol page the app (renderer → main) asks the bridge to fetch that symbol's chain
from IBKR (if connected) and POST it, then reloads the page data from the engine. Without IBKR the engine
serves the most recent chain it has stored (from an earlier IBKR push or a vendor source).

The on-demand refresh runs at any time, also outside market hours and on weekends: the bridge requests
market data type 4 (delayed-frozen), so TWS answers with the last values of the session. Its result
(`RefreshResult`, electron/shared/api.ts) is `refreshed` / `fresh` / `no-options` (IBKR lists no options:
the bridge POSTed `/api/ingest/no-options`) / `unavailable` (bridge off, not connected or paused; asked again
when the bridge connects while the page is open) / `failed`. After `refreshed` or `no-options` the page reloads.

Chain fetch details (bridge): the first 8 expiries listed by `reqSecDefOptParams` (SMART), strikes within ±25%
of spot. The option parameters are cached per underlying for the New York day; when TWS does not answer within
60 s the request is sent once more with a 120 s wait, then the refresh fails with "IB Gateway did not send
<symbol>'s option expiries … try again in a minute" (never `no-options`). Every listed contract of every picked
expiry is posted, those TWS sent nothing for with null fields (shown as "no quote"); an expiry whose contract list
TWS did not deliver is skipped, not fatal. The bridge status names the gaps of the last chain ("Incomplete IBKR
chain for DELL: 62% of contracts quoted; no quotes for 2026-11-27; no contracts from IBKR for …"). Frozen /
weekend quotes: an ask ≤ 0 is no quote, a 0 bid counts only against a real ask (−1 is always no quote).
`contracts[].last_ts` is the time of the last trade (ticks 45 / 88) when TWS sends it.
