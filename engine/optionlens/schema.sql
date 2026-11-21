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

-- Replaced per symbol on every chain fetch (a chain older than the stored one is ignored). ts = fetch time.
CREATE TABLE IF NOT EXISTS option_latest (
    symbol VARCHAR NOT NULL, osi VARCHAR PRIMARY KEY, expiry DATE NOT NULL, strike DOUBLE NOT NULL,
    "right" VARCHAR NOT NULL, bid DOUBLE, ask DOUBLE, last DOUBLE, change DOUBLE,
    volume BIGINT, oi BIGINT, iv DOUBLE, delta DOUBLE, gamma DOUBLE, theta DOUBLE, vega DOUBLE,
    rho DOUBLE, theo DOUBLE, ts TIMESTAMP NOT NULL,
    source VARCHAR                             -- 'ibkr' | 'vendor' | 'cboe' (legacy)
);

-- Final sweep of each trading day. Logical key (date, osi); no PK index on purpose (billions of rows
-- at full retention; an ART index would not fit in memory). Uniqueness is kept by delete-then-insert
-- per (date, symbol) in repo.options. source: 'ibkr' | 'vendor' | 'cboe' (legacy) | 'reconstructed' (from
-- historical contract bars: bid/ask/oi NULL, IV/greeks computed). An IBKR archive is never replaced by
-- another source's, and a reconstructed archive never replaces any other.
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
