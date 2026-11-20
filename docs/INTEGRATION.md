# OptionLens client ⇄ server integration contract (authoritative; identical copy in both repos)

Two repos:
- **optionlens-server** — Python service (FastAPI + DuckDB + in-process scheduler). Runs 24/7 on any
  Linux/macOS box. Collects data (Alpaca option chains, Nasdaq prices/calendars, Treasury rates),
  archives end-of-day, serves the analytics API. API-only: it no longer serves the web UI.
- **optionlens-mac** — Electron app: the UI (React, formerly `web/`), server connection settings,
  and the **IBKR bridge** (talks to the user's running TWS / IB Gateway on the Mac and pushes data
  to the server).

No tailnet assumption: the server may be reached over a LAN or the internet. Security is therefore
built in: every request carries a token; HTTPS with a pinned self-signed certificate is the default.

---------------------------------------------------------------------------------------------------
## 1. Server configuration (env, also read from `~/.config/optionlens/server.env` if present)

| Variable | Default | Meaning |
|---|---|---|
| `OPTIONLENS_HOST` | `127.0.0.1` | Bind address (`0.0.0.0` to accept network clients) |
| `OPTIONLENS_PORT` | `8473` | Port |
| `OPTIONLENS_API_TOKEN` | — | Required whenever HOST is not loopback (server refuses to start otherwise). Generate with `optionlens.cli gen-token`. |
| `OPTIONLENS_TLS` | `auto` | `auto`: HTTPS with a self-signed cert when HOST is not loopback, plain HTTP on loopback. `on` / `off` force it. |
| `OPTIONLENS_TLS_CERT`, `OPTIONLENS_TLS_KEY` | — | Use your own cert/key instead of the generated one |
| `OPTIONLENS_CORS_ORIGINS` | `app://optionlens` | Comma list of allowed browser origins (the Electron renderer origin) |
| `OPTIONLENS_ALPACA_KEY_ID`, `OPTIONLENS_ALPACA_SECRET_KEY` | — | Alpaca market-data credentials (paper account keys are fine) |
| `OPTIONLENS_DATA_DIR` | `~/.local/share/optionlens` | Database + TLS material |

Generated TLS material lives in `DATA_DIR/tls/{cert.pem,key.pem}` (EC P-256, 10-year validity,
SAN = every non-loopback IP of the host + its hostname + `localhost`). `optionlens.cli tls-fingerprint`
prints the SHA-256 fingerprint of the served certificate (uppercase hex, colon-separated).

## 2. Authentication

- Every `/api/**` request except `GET /api/health` must send `Authorization: Bearer <token>`.
  Missing/wrong → `401 {"detail":"unauthorized"}`. Comparison is constant-time.
- When HOST is loopback and no token is configured, auth is disabled (local development only).
- `GET /api/health` (no auth) → `{"ok": true, "version": "<semver>", "auth": true|false, "tls": true|false,
  "server_id": "<stable random id stored in DATA_DIR>"}`.

## 3. Certificate pinning (client side)

- First connection to a new server URL: the app fetches `/api/health`, shows the certificate's SHA-256
  fingerprint and asks the user to confirm it matches `optionlens.cli tls-fingerprint` on the server
  (trust on first use). The confirmed fingerprint is stored with the server URL; later connections
  accept only that certificate (Electron `session.setCertificateVerifyProc`). A changed fingerprint
  is a hard error with a "re-pair" button. Plain `http://` is allowed only for loopback/LAN URLs and
  shows a warning badge.

## 4. CORS

The renderer is served from the custom protocol `app://optionlens`. The server answers CORS for the
origins in `OPTIONLENS_CORS_ORIGINS` (methods GET, POST, PUT, DELETE, OPTIONS; header Authorization,
Content-Type). No cookies.

## 5. Existing API

Unchanged from the API contract in optionlens-mac `src/api/types.ts` (same paths, same JSON), except:
- `ReportResult`, `Chain`, `Quote`, `ExpiryInfo` and per-row data may carry `source` where known:
  `"alpaca" | "ibkr" | "cboe" | "nasdaq" | "model" | "reconstructed"`.
- `Status` gains `sources: { name: string; ok: boolean | null; last_success_at: string | null; detail: string | null }[]`
  and `bridge: { connected: boolean; last_push_at: string | null; client: string | null }`.

## 6. Ingest API (written by the IBKR bridge; all POST, Bearer auth, JSON, idempotent)

Times are ISO-8601 with offset; dates `YYYY-MM-DD`; IV/percent as fractions; prices per share.

```
POST /api/ingest/chain
{ "source": "ibkr", "symbol": "AAPL", "fetched_at": "...",
  "final": false,                       // true = closing snapshot for session `session_date`
  "session_date": "2026-11-20",         // required when final
  "underlying": { "price": 349.1, "bid": 349.0, "ask": 349.2, "open": ..., "high": ..., "low": ...,
                  "prev_close": ..., "volume": 123, "ts": "..." },
  "contracts": [ { "expiry": "2026-12-18", "strike": 350.0, "right": "C",
                   "bid": 9.1, "ask": 9.3, "last": 9.2, "volume": 812, "oi": 15321,
                   "iv": 0.251, "delta": 0.52, "gamma": 0.011, "theta": -0.12, "vega": 0.41 } ] }
→ 200 {"stored": n_contracts, "symbol": "AAPL"}
```
Server builds OSI codes, stores through the same pipeline as any other chain source (latest chain,
quote, today's metrics row; `final` also writes the end-of-day archive for `session_date`). A chain from
IBKR wins over an Alpaca chain fetched earlier for the same session (real OPRA quotes beat indicative).

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

`GET /api/ingest/wants` → what the server would like the bridge to fetch now (the bridge polls it every
5 min while connected, and immediately after connecting):
```
{ "iv_history": [ {"symbol": "AAPL", "from": "2016-01-01"} ],   // symbols whose stored IV history is
                                                                // missing or older than the last session
  "eod_chains": { "session_date": "2026-11-20", "symbols": ["AAPL", ...], "not_before": "...", "deadline": "..." },
                                                                // watchlists + indices (SPX, NDX, RUT, XSP)
                                                                // after close+20m until 20:00 ET
  "index_symbols": ["VIX", "VIX9D", "VIX3M", "VIX6M"],
  "chains_on_demand_max_age_s": 120 }
```

IV history semantics: values ingested via `/api/ingest/iv-history` populate a per-symbol daily IV30
series used for IV rank/percentile, vol history charts and the model-pricing IV proxy, wherever the
server's own collected IV30 for that date is missing (collected values win; both are kept, tagged by source).

## 7. Client-triggered refresh

When the user opens a symbol page the app (renderer → main) asks the bridge to fetch that symbol's chain
from IBKR (if connected) and POST it, then reloads the page data from the server. Without IBKR the server
serves its own most recent chain (Alpaca).
