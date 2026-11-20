# OptionLens app ⇄ engine integration contract (authoritative)

Two parts, one machine:
- **engine** (this Python code; FastAPI + DuckDB + in-process scheduler) — the app's embedded backend.
  The Mac app launches it as a local sidecar process bound to loopback. Collects data (Nasdaq
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
| `OPTIONLENS_DATA_DIR` | `~/.local/share/optionlens` | Database directory; the app passes `~/Library/Application Support/OptionLens/data` |

Launch: `python -m optionlens.cli serve [--port N]`. Once the API is ready the engine prints exactly one
line `LISTENING <port>` on stdout (logs go to stderr); the app reads the port from it. SIGTERM (or
SIGINT) shuts it down gracefully. One engine per DATA_DIR (DuckDB is single-process).

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
                   "bid": 9.1, "ask": 9.3, "last": 9.2, "volume": 812, "oi": 15321,
                   "iv": 0.251, "delta": 0.52, "gamma": 0.011, "theta": -0.12, "vega": 0.41 } ] }
→ 200 {"stored": n_contracts, "symbol": "AAPL"}
```
For `final` chains the engine uses `rth_close` (when given) as the underlying price of the snapshot: the
session's close in metrics, the archive's `underlying` and the moneyness reference. After 16:00 ET the live
`price` is an extended-hours trade and tick 9 is still the previous session's close, so neither is the
session's close. The bridge centres a closing chain's strikes on `rth_close` too. Greeks (`iv`, `delta`, ...)
are TWS's model values computed from the live underlying, i.e. possibly an after-hours price; the engine
stores them as sent.

Server builds OSI codes, stores through the same pipeline as any other chain source (latest chain,
quote, today's metrics row; `final` also writes the end-of-day archive for `session_date`). A chain from
IBKR wins over a chain from any other source for the same session. The bridge posts only contracts that got
data, and sends `final: true` only when at least 95% of the contracts it requested were quoted; a less
complete closing chain is posted with `final: false` (live chain only, never archived) and retried later.

```
POST /api/ingest/iv-history
{ "source": "ibkr", "symbol": "AAPL", "kind": "iv30",   // IBKR OPTION_IMPLIED_VOLATILITY daily close
  "bars": [ { "date": "2025-11-20", "value": 0.243 }, ... ] }
→ 200 {"stored": n}
POST /api/ingest/hv-history   (same shape, kind "hv30": IBKR HISTORICAL_VOLATILITY)     → 200
POST /api/ingest/index-bars
{ "source": "ibkr", "symbol": "VIX", "bars": [ {"date": "...", "open":..,"high":..,"low":..,"close":..} ] }
→ 200 {"stored": n}
POST /api/ingest/quotes
{ "source": "ibkr", "quotes": [ { "symbol": "VIX", "price": 27.8, "change": 3.26, "change_pct": 0.1327,
  "ts": "..." } ] }
→ 200 {"stored": n}
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
batches of a longer task — and every other kind gets a weighted share of TWS time (eod_chains 8,
chain_universe 4, underlying_stats 3, index quotes/bars 2, option_bars 1, iv_history 1), so each kind with
work makes progress in every cycle.
```
{ "iv_history": [ {"symbol": "AAPL", "from": "2016-01-01"} ],   // symbols whose stored IV history is
                                                                // missing or older than the last session
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
  "index_symbols": ["VIX", "VIX9D", "VIX3M", "VIX6M"],
  "chains_on_demand_max_age_s": 120 }
```

Coverage: there is no free, licensed source of full option chains for the whole universe that this user can
use (CBOE's website feed is blocked and not licensed for collection; Alpaca does not serve Canadian
residents). Server-side scheduled chain sweeps therefore run only if a vendor ChainSource is configured
(none by default). Reports that need full chains (strategy screeners, option screener, OI changes, large
volume contracts) cover the chain universe; their `notes` say so. Universe-level reports (unusual volume,
IV rankings, put/call, broad view) use underlying-stats for every symbol.

IV history semantics: values ingested via `/api/ingest/iv-history` populate a per-symbol daily IV30
series used for IV rank/percentile, vol history charts and the model-pricing IV proxy, wherever the
server's own collected IV30 for that date is missing (collected values win; both are kept, tagged by source).

## 7. Client-triggered refresh

When the user opens a symbol page the app (renderer → main) asks the bridge to fetch that symbol's chain
from IBKR (if connected) and POST it, then reloads the page data from the engine. Without IBKR the engine
serves the most recent chain it has stored (from an earlier IBKR push or a vendor source).
