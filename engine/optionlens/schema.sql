-- OptionLens DuckDB schema (idempotent). All TIMESTAMP columns hold naive UTC.
CREATE SEQUENCE IF NOT EXISTS seq_watchlists START 1;
CREATE SEQUENCE IF NOT EXISTS seq_presets START 1;
CREATE SEQUENCE IF NOT EXISTS seq_alerts START 1;
CREATE SEQUENCE IF NOT EXISTS seq_notifications START 1;
CREATE SEQUENCE IF NOT EXISTS seq_job_runs START 1;

CREATE TABLE IF NOT EXISTS symbols (
    symbol      VARCHAR PRIMARY KEY,
    name        VARCHAR,
    kind        VARCHAR NOT NULL,          -- 'stock' | 'etf' | 'index'
    sector      VARCHAR,
    industry    VARCHAR,
    market_cap  DOUBLE,
    in_universe BOOLEAN NOT NULL DEFAULT false,
    optionable  BOOLEAN,                   -- NULL = not yet probed
    updated_at  TIMESTAMP
);

CREATE TABLE IF NOT EXISTS daily_bars (
    symbol VARCHAR NOT NULL, date DATE NOT NULL,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE NOT NULL, volume BIGINT,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS quotes_latest (
    symbol VARCHAR PRIMARY KEY, ts TIMESTAMP NOT NULL,
    price DOUBLE, change DOUBLE, change_pct DOUBLE, open DOUBLE, high DOUBLE, low DOUBLE,
    prev_close DOUBLE, volume BIGINT, bid DOUBLE, ask DOUBLE, iv30 DOUBLE,
    source VARCHAR                             -- who supplied it (docs/INTEGRATION.md §5)
);

-- The served chain per symbol, replaced when a chain wins jobs/precedence.py against it. ts = fetch time.
CREATE TABLE IF NOT EXISTS option_latest (
    symbol VARCHAR NOT NULL, osi VARCHAR PRIMARY KEY, expiry DATE NOT NULL, strike DOUBLE NOT NULL,
    "right" VARCHAR NOT NULL, bid DOUBLE, ask DOUBLE, last DOUBLE, change DOUBLE,
    volume BIGINT, oi BIGINT, iv DOUBLE, delta DOUBLE, gamma DOUBLE, theta DOUBLE, vega DOUBLE,
    rho DOUBLE, theo DOUBLE, ts TIMESTAMP NOT NULL,
    source VARCHAR                             -- 'ibkr' | 'vendor' | 'cboe' (legacy)
);

-- Final sweep of each trading day. Logical key (date, osi); no PK index on purpose (billions of rows
-- at full retention; an ART index would not fit in memory). Uniqueness is kept by delete-then-insert
-- per (date, symbol) in repo.options. source: 'ibkr' | 'questrade' | 'vendor' | 'cboe' (legacy) | 'reconstructed'
-- (from historical contract bars: bid/ask/oi NULL, IV/greeks computed). Which real archive wins follows
-- jobs/precedence.py; a reconstructed archive never replaces any other.
CREATE TABLE IF NOT EXISTS option_eod (
    date DATE NOT NULL, symbol VARCHAR NOT NULL, osi VARCHAR NOT NULL, expiry DATE NOT NULL,
    strike DOUBLE NOT NULL, "right" VARCHAR NOT NULL, bid DOUBLE, ask DOUBLE, last DOUBLE,
    volume BIGINT, oi BIGINT, iv DOUBLE, delta DOUBLE, gamma DOUBLE, theta DOUBLE, vega DOUBLE,
    underlying DOUBLE,
    source VARCHAR
);

CREATE TABLE IF NOT EXISTS metrics_daily (
    date DATE NOT NULL, symbol VARCHAR NOT NULL,
    close DOUBLE, change_pct DOUBLE, volume BIGINT,
    iv30 DOUBLE, iv60 DOUBLE, iv90 DOUBLE, iv120 DOUBLE, iv180 DOUBLE, iv360 DOUBLE,
    iv30_cboe DOUBLE, atm_iv_front DOUBLE, skew25_30 DOUBLE, put_skew_30 DOUBLE,
    hv10 DOUBLE, hv20 DOUBLE, hv30 DOUBLE, hv60 DOUBLE, hv90 DOUBLE, hv252 DOUBLE,
    call_volume BIGINT, put_volume BIGINT, call_oi BIGINT, put_oi BIGINT,
    call_premium DOUBLE, put_premium DOUBLE,
    call_vol_otm BIGINT, call_vol_atm BIGINT, call_vol_itm BIGINT,
    put_vol_otm BIGINT, put_vol_atm BIGINT, put_vol_itm BIGINT,
    call_prem_otm DOUBLE, call_prem_atm DOUBLE, call_prem_itm DOUBLE,
    put_prem_otm DOUBLE, put_prem_atm DOUBLE, put_prem_itm DOUBLE,
    net_delta_volume DOUBLE, straddle30_pct DOUBLE, expected_move30_pct DOUBLE,
    updated_at TIMESTAMP,
    source VARCHAR,     -- chain source of the chain-derived values ('ibkr', 'vendor', 'cboe'), or
                        -- '<source>:stats' while the row holds only pushed underlying stats
    PRIMARY KEY (date, symbol)
);

CREATE TABLE IF NOT EXISTS earnings (
    symbol VARCHAR NOT NULL, date DATE NOT NULL, timing VARCHAR NOT NULL, fiscal_quarter VARCHAR,
    eps_actual DOUBLE, eps_estimate DOUBLE, surprise_pct DOUBLE, num_estimates INTEGER,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS dividends (
    symbol VARCHAR NOT NULL, ex_date DATE NOT NULL, amount DOUBLE NOT NULL, pay_date DATE,
    record_date DATE, declared_date DATE, kind VARCHAR NOT NULL,
    PRIMARY KEY (symbol, ex_date)
);

CREATE TABLE IF NOT EXISTS index_bars (
    symbol VARCHAR NOT NULL, date DATE NOT NULL,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE NOT NULL,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS rates (date DATE PRIMARY KEY, rate DOUBLE NOT NULL);
-- CAD risk-free rate: Bank of Canada 3-month T-bill (sources/boc.py), fraction; `rates` above is USD.
CREATE TABLE IF NOT EXISTS rates_cad (date DATE PRIMARY KEY, rate DOUBLE NOT NULL);

CREATE TABLE IF NOT EXISTS report_snapshots (
    date DATE NOT NULL, report_id VARCHAR NOT NULL, symbol VARCHAR NOT NULL, payload JSON,
    PRIMARY KEY (date, report_id, symbol)
);

CREATE TABLE IF NOT EXISTS watchlists (
    id INTEGER PRIMARY KEY DEFAULT nextval('seq_watchlists'),
    name VARCHAR NOT NULL UNIQUE, created_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlist_items (
    watchlist_id INTEGER NOT NULL, symbol VARCHAR NOT NULL, added_at TIMESTAMP NOT NULL,
    PRIMARY KEY (watchlist_id, symbol)
);

CREATE TABLE IF NOT EXISTS presets (
    id INTEGER PRIMARY KEY DEFAULT nextval('seq_presets'),
    report_id VARCHAR NOT NULL, name VARCHAR NOT NULL, payload JSON, created_at TIMESTAMP NOT NULL,
    UNIQUE (report_id, name)
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY DEFAULT nextval('seq_alerts'),
    kind VARCHAR NOT NULL, symbol VARCHAR, params JSON, enabled BOOLEAN NOT NULL DEFAULT true,
    cooldown_min INTEGER NOT NULL DEFAULT 240, last_fired_at TIMESTAMP, created_at TIMESTAMP NOT NULL
);
-- Per-alert, per-symbol cooldown state (watchlist-scoped alerts fire per symbol).
CREATE TABLE IF NOT EXISTS alert_fires (
    alert_id INTEGER NOT NULL, symbol VARCHAR NOT NULL, fired_at TIMESTAMP NOT NULL,
    PRIMARY KEY (alert_id, symbol)
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY DEFAULT nextval('seq_notifications'),
    ts TIMESTAMP NOT NULL, alert_id INTEGER, title VARCHAR NOT NULL, body VARCHAR NOT NULL,
    link VARCHAR, read BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS http_cache (key VARCHAR PRIMARY KEY, payload JSON, fetched_at TIMESTAMP NOT NULL);

CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY DEFAULT nextval('seq_job_runs'),
    job VARCHAR NOT NULL, started_at TIMESTAMP NOT NULL, finished_at TIMESTAMP, ok BOOLEAN, detail VARCHAR
);

-- Daily volatility series pushed by data clients (docs/INTEGRATION.md §6): kind 'iv30' | 'hv30',
-- source e.g. 'ibkr'. Kept beside the server's own metrics_daily.iv30 (collected values win on read).
CREATE TABLE IF NOT EXISTS vol_history (
    symbol VARCHAR NOT NULL, kind VARCHAR NOT NULL, source VARCHAR NOT NULL, date DATE NOT NULL,
    value DOUBLE NOT NULL, updated_at TIMESTAMP NOT NULL,
    PRIMARY KEY (symbol, kind, source, date)
);

-- IBKR bridge state: the latest heartbeat (single row, id = 1) and the latest ingest push.
CREATE TABLE IF NOT EXISTS bridge_status (
    id INTEGER PRIMARY KEY, client VARCHAR, ibkr_connected BOOLEAN, account_type VARCHAR,
    heartbeat_at TIMESTAMP, last_push_at TIMESTAMP
);

-- Symbols the user opened recently (part of the bridge's chain universe). One row per symbol.
CREATE TABLE IF NOT EXISTS symbol_views (symbol VARCHAR PRIMARY KEY, viewed_at TIMESTAMP NOT NULL);

-- Missing end-of-day archives offered to the bridge as option-bars catch-up (bounded re-offers).
CREATE TABLE IF NOT EXISTS eod_catchup (
    date DATE NOT NULL, symbol VARCHAR NOT NULL, offered_at TIMESTAMP NOT NULL, offers INTEGER NOT NULL,
    PRIMARY KEY (date, symbol)
);

-- Intraday bars (GET /api/symbol/{sym}/bars?interval=...), kept per interval for its retention (analytics/intraday.py):
-- `ts` is the bar's start (UTC). intraday_spans: the time spans already fetched for (symbol, interval), so only the
-- missing windows are asked for again (a span may hold no bars: nights, weekends).
CREATE TABLE IF NOT EXISTS intraday_bars (
    symbol VARCHAR NOT NULL, interval VARCHAR NOT NULL, ts TIMESTAMP NOT NULL,
    open DOUBLE NOT NULL, high DOUBLE NOT NULL, low DOUBLE NOT NULL, close DOUBLE NOT NULL, volume BIGINT,
    source VARCHAR NOT NULL, delayed BOOLEAN,
    PRIMARY KEY (symbol, interval, ts)
);
CREATE TABLE IF NOT EXISTS intraday_spans (
    symbol VARCHAR NOT NULL, interval VARCHAR NOT NULL, start_ts TIMESTAMP NOT NULL, end_ts TIMESTAMP NOT NULL,
    source VARCHAR NOT NULL, fetched_at TIMESTAMP NOT NULL
);

-- Migration of databases created before the `source` columns: existing rows came from CBOE. The default
-- only back-fills those rows; new rows always state their source (NULL when unknown).
ALTER TABLE quotes_latest ADD COLUMN IF NOT EXISTS source VARCHAR DEFAULT 'cboe';
ALTER TABLE quotes_latest ALTER COLUMN source DROP DEFAULT;
ALTER TABLE option_latest ADD COLUMN IF NOT EXISTS source VARCHAR DEFAULT 'cboe';
ALTER TABLE option_latest ALTER COLUMN source DROP DEFAULT;
ALTER TABLE option_eod ADD COLUMN IF NOT EXISTS source VARCHAR DEFAULT 'cboe';
ALTER TABLE option_eod ALTER COLUMN source DROP DEFAULT;
ALTER TABLE metrics_daily ADD COLUMN IF NOT EXISTS source VARCHAR DEFAULT 'cboe';
ALTER TABLE metrics_daily ALTER COLUMN source DROP DEFAULT;
-- `delayed` (docs/INTEGRATION.md §6): the data was pushed from delayed market data; NULL = unknown/older data.
ALTER TABLE quotes_latest ADD COLUMN IF NOT EXISTS delayed BOOLEAN;
ALTER TABLE option_latest ADD COLUMN IF NOT EXISTS delayed BOOLEAN;
ALTER TABLE metrics_daily ADD COLUMN IF NOT EXISTS delayed BOOLEAN;
-- The chain was built from a contract list the client saved earlier (IBKR's contract server unavailable): when
-- that list was listed. NULL = listed at fetch time.
ALTER TABLE option_latest ADD COLUMN IF NOT EXISTS contracts_listed_at TIMESTAMP;
-- The bridge's last heartbeat: IBKR server farms (JSON {market_data, historical, sec_def}) and the account's US
-- options market data ('unknown' | 'realtime' | 'delayed_only').
ALTER TABLE bridge_status ADD COLUMN IF NOT EXISTS farms VARCHAR;
ALTER TABLE bridge_status ADD COLUMN IF NOT EXISTS options_subscription VARCHAR;
-- Provenance of IV and greeks (docs/INTEGRATION.md §6 "Computed IV and greeks"): the chain source's name
-- ('ibkr', ...) when it sent them, 'computed_mid' / 'computed_last' (IV) and 'computed' (greeks) when the engine
-- derived them, NULL = none. last_ts: time of the last trade when the source said. metrics_daily.iv_computed:
-- the row's chain IV metrics (iv30..iv360, atm_iv_front, skews) come from engine-computed IVs.
ALTER TABLE option_latest ADD COLUMN IF NOT EXISTS last_ts TIMESTAMP;
ALTER TABLE option_latest ADD COLUMN IF NOT EXISTS iv_source VARCHAR;
ALTER TABLE option_latest ADD COLUMN IF NOT EXISTS greeks_source VARCHAR;
ALTER TABLE option_eod ADD COLUMN IF NOT EXISTS iv_source VARCHAR;
ALTER TABLE option_eod ADD COLUMN IF NOT EXISTS greeks_source VARCHAR;
ALTER TABLE metrics_daily ADD COLUMN IF NOT EXISTS iv_computed BOOLEAN;
-- metrics_daily.after_hours (docs/INTEGRATION.md §6 "After-hours fills"): the row was created after the session's
-- collection window had ended (IBKR frozen data of the last session, e.g. a weekend first launch) because the
-- session had no row; a push from inside the window or a closing snapshot replaces it, and it never replaces one.
-- NULL / false = collected during the session or from its closing snapshot.
ALTER TABLE metrics_daily ADD COLUMN IF NOT EXISTS after_hours BOOLEAN;
-- Chain precedence (docs/INTEGRATION.md §6 "Chain precedence", jobs/precedence.py): the quality of the chain behind a
-- metrics_daily row (chain_final: a closing snapshot; chain_quoted: contracts with a real ask) and whether an archive
-- holds delayed data. NULL = from before these columns (unknown).
ALTER TABLE metrics_daily ADD COLUMN IF NOT EXISTS chain_final BOOLEAN;
ALTER TABLE metrics_daily ADD COLUMN IF NOT EXISTS chain_quoted INTEGER;
ALTER TABLE option_eod ADD COLUMN IF NOT EXISTS delayed BOOLEAN;
-- Chain precedence without a source tie-break (docs/INTEGRATION.md §6 "Chain precedence"): the served chain is chosen
-- like a session's metrics row and archive, so option_latest records whether it is a closing snapshot, and the metrics
-- row / archive when the chain behind them was fetched (the last tie-break: fresher wins). NULL = older rows (unknown).
ALTER TABLE option_latest ADD COLUMN IF NOT EXISTS final BOOLEAN;
ALTER TABLE metrics_daily ADD COLUMN IF NOT EXISTS chain_fetched_at TIMESTAMP;
ALTER TABLE option_eod ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMP;
-- Questrade quote fields (docs/INTEGRATION.md §8 "Reference data"): the session VWAP as the quote reported it (NULL =
-- not sent, e.g. IBKR pushes) and each intraday bar's own VWAP (Questrade candles; NULL = not sent).
ALTER TABLE quotes_latest ADD COLUMN IF NOT EXISTS vwap DOUBLE;
ALTER TABLE intraday_bars ADD COLUMN IF NOT EXISTS vwap DOUBLE;

-- Dividends from sources other than Nasdaq (docs/INTEGRATION.md §6 "Dividends"): 'ibkr-adjusted' (derived from IBKR's
-- dividend-adjusted vs raw daily closes) and 'questrade' (the current dividend of GET v1/symbols/:id). Nasdaq's rows
-- stay in `dividends`; readers see the merge of both (repo/dividends.py, analytics.dividends.SOURCE_PRIORITY).
CREATE TABLE IF NOT EXISTS dividends_derived (
    symbol VARCHAR NOT NULL, ex_date DATE NOT NULL, source VARCHAR NOT NULL, amount DOUBLE NOT NULL, pay_date DATE,
    kind VARCHAR NOT NULL, updated_at TIMESTAMP NOT NULL,
    PRIMARY KEY (symbol, ex_date, source)
);
-- The IBKR dividend-history fetches (also of symbols that paid nothing): the span of closes they covered.
CREATE TABLE IF NOT EXISTS dividend_history_fetches (
    symbol VARCHAR PRIMARY KEY, source VARCHAR NOT NULL, first_date DATE, last_date DATE, dividends INTEGER NOT NULL,
    fetched_at TIMESTAMP NOT NULL
);

-- One reference snapshot per symbol per day (Questrade GET v1/symbols/:id, docs/INTEGRATION.md §8): the single source of
-- P/E, EPS, market cap, shares, 52-week range, average volumes and Questrade's sector classification.
CREATE TABLE IF NOT EXISTS fundamentals_snapshot (
    symbol VARCHAR NOT NULL, date DATE NOT NULL, source VARCHAR NOT NULL,
    pe DOUBLE, eps DOUBLE, market_cap DOUBLE, shares_outstanding DOUBLE, high_52w DOUBLE, low_52w DOUBLE,
    avg_volume_20d DOUBLE, avg_volume_3m DOUBLE, sector VARCHAR, industry_group VARCHAR, industry_subgroup VARCHAR,
    dividend_yield DOUBLE, prev_close DOUBLE, fetched_at TIMESTAMP NOT NULL,
    PRIMARY KEY (symbol, date)
);

-- The latest extended-hours quote per symbol and market ('pre' 04:00-09:30, 'post' close-20:00 ET) of session_date.
CREATE TABLE IF NOT EXISTS extended_quotes (
    symbol VARCHAR NOT NULL, market VARCHAR NOT NULL, session_date DATE NOT NULL, ts TIMESTAMP NOT NULL,
    price DOUBLE, regular_price DOUBLE, volume BIGINT, last_trade_at TIMESTAMP, source VARCHAR NOT NULL,
    delayed BOOLEAN,
    PRIMARY KEY (symbol, market)
);

-- Trading halts as the quotes report them (Questrade isHalted): the current state, when it last changed (NULL: no
-- change observed yet) and when it was last observed.
CREATE TABLE IF NOT EXISTS halt_status (
    symbol VARCHAR PRIMARY KEY, halted BOOLEAN NOT NULL, changed_at TIMESTAMP, observed_at TIMESTAMP NOT NULL,
    source VARCHAR NOT NULL
);

-- A symbol's market, exchange and currency follow from the symbol itself (analytics/markets.py): no columns.

-- What each chain source answered for a symbol it has no chain of (docs/INTEGRATION.md §5 "Listed options"): verdict
-- 'no_options' (the source lists no options for it) or 'not_listed' (the source does not carry it); a chain stored
-- from the source removes its row. symbols.optionable is false only once every chain source serving the symbol has a
-- row here (jobs/optionable.py): one source's "none" is that source's answer, not the symbol's.
CREATE TABLE IF NOT EXISTS chain_verdicts (
    symbol VARCHAR NOT NULL, source VARCHAR NOT NULL, verdict VARCHAR NOT NULL, answered_at TIMESTAMP NOT NULL,
    PRIMARY KEY (symbol, source)
);
-- Migration (idempotent): optionable = false without any source's 'no_options' row was set from a single source's
-- answer before chain_verdicts existed; it becomes unknown again, so every serving source is asked.
UPDATE symbols SET optionable = NULL WHERE optionable = false
    AND symbol NOT IN (SELECT symbol FROM chain_verdicts WHERE verdict = 'no_options');
-- Earnings sources (docs/INTEGRATION.md §6 "Earnings"): 'nasdaq' for US listings, 'tmx' / 'yahoo' for Canadian ones.
-- Rows stored before the column came from Nasdaq.
ALTER TABLE earnings ADD COLUMN IF NOT EXISTS source VARCHAR DEFAULT 'nasdaq';
ALTER TABLE earnings ALTER COLUMN source DROP DEFAULT;
