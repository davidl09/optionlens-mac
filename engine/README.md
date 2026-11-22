# OptionLens engine

The OptionLens engine is the backend embedded in the OptionLens Mac app (this repository). It is one
Python process: a FastAPI JSON API, a DuckDB database and an in-process scheduler that collects data.
The app starts it as a local sidecar on loopback and talks to it over HTTP with a per-launch token.
The engine does not serve the UI and does not accept network clients.

`../docs/INTEGRATION.md` is the app ⇄ engine contract (launch environment, auth, ingest API).
The API JSON contract is the app's `src/api/types.ts`; the contract tests read it from the enclosing
checkout (or `OPTIONLENS_TYPES_TS`).

## How the app runs it

The app sets the environment and runs `python -m optionlens.cli serve --port 0`:

| Variable | Set by the app to |
|---|---|
| `OPTIONLENS_API_TOKEN` | a random token for this launch (all requests except `GET /api/health` need `Authorization: Bearer <token>`) |
| `OPTIONLENS_DATA_DIR` | `~/Library/Application Support/OptionLens/data` |
| `OPTIONLENS_PARENT_PID` | the app's pid (the engine shuts down when the app is gone) |

When the API is ready, the engine prints one line, `LISTENING <port>`, on stdout. Logs go to stderr.
SIGTERM stops the engine gracefully. `OPTIONLENS_HOST` must be loopback (`127.0.0.1`, `::1` or
`localhost`). With any other value, the engine refuses to start.

## Running it standalone (development)

```
cd engine
uv sync
OPTIONLENS_DATA_DIR=/tmp/optionlens-dev uv run python -m optionlens.cli serve --port 8473
curl http://127.0.0.1:8473/api/health
```

When `OPTIONLENS_API_TOKEN` is not set, auth is off. Set `OPTIONLENS_NO_SCHEDULER=1` to serve stored
data without running collectors. Data commands (`uv run python -m optionlens.cli --help`: backfill,
eod, status, ...) open the same database. DuckDB allows one process per file, so stop the engine first.

Tests: `cd engine && uv run pytest -q`.

## What it collects (America/New_York, trading days)

- Option data is pushed by the app's IBKR bridge through the ingest API (`/api/ingest/*`): chains for the
  chain universe (watchlists, the most liquid `OPTIONLENS_CHAIN_UNIVERSE_SIZE` symbols and recently
  opened symbols), closing chains, universe-wide underlying option stats and IV/HV history.
  `GET /api/ingest/wants` tells the bridge what to fetch.
- With Questrade connected (Settings → Data sources; docs/INTEGRATION.md §8) the engine also sweeps the whole
  universe's chains itself: hourly in market hours, a closing snapshot at close + 20 min and after-hours fills.
  When both deliver a chain, the session keeps the better one (closing, real-time, more quoted contracts; ties:
  IBKR; `OPTIONLENS_CHAIN_SOURCES` sets the order).
- End of day: the closing chains are archived to `option_eod`. IV rank and percentile, vol history and
  the backtest IV proxy use the engine's own IV30 where it has one, and ingested IBKR IV30 for other dates.
- From the engine itself: 10 years of daily prices (Nasdaq), earnings dates and EPS, dividend calendar
  and history, and the 13-week T-bill rate (Treasury).

## Limitations

- Not available, so not implemented: the trade-level options tape (time & sales, block/sweep and
  multi-leg trade detection, buy/sell order-flow sentiment), auctions/imbalances, threshold list,
  PDUFA/M&A calendars, company guidance, ETF holdings, minute VWAP, proprietary fair value.
- Reports that need full chains cover the chain universe only. Their notes say so.
- Values that need history the engine does not have yet are shown as null, never made up. Option
  backtests are model-priced (badged `model`) where no stored option history exists.

## Layout

`../docs/SPEC.md` (requirements) · `../docs/` (contracts) · `optionlens/{sources,repo,analytics,services,api,jobs}` ·
`optionlens/sidecar.py` (process lifecycle) · `tests/`.
