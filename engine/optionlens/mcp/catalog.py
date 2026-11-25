"""The MCP tool catalog (docs/INTEGRATION.md §10): one tool per engine operation an AI agent may use, generated from
the engine's OpenAPI schema plus a thin curated layer (names, descriptions with units, write annotations). Pure: no
I/O and no MCP SDK, so the engine's auth can use it to decide what the MCP key may call.

Policy:
- every GET under /api is a read-only tool, unless EXCLUDED (a new GET endpoint becomes a tool by itself, named
  from its path; CURATED gives it a better name and description);
- any other method is a tool only when CURATED (each write is a decision: annotations and description);
- every operation is a tool or EXCLUDED with its reason (tests/mcp enforce it, so nothing drifts silently);
- a tool with a `gate` is listed and callable only while the user allowed that gate (GATES; the engine's auth refuses
  it otherwise, the MCP server hides it): the portfolio tools need "Allow agents to see my portfolio".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import quote

PAGE_DEFAULT = 100
PAGE_MAX = 1000

# ----------------------------------------------------------------------------- what agents never reach
EXCLUDED_PREFIXES: dict[str, str] = {
    "/api/ingest/": "IBKR bridge ingest and its work lists (internal to the app)",
}
EXCLUDED: dict[str, str] = {
    "GET /api/health": "liveness probe of the app (open, no data); `system_status` has the engine's state",
    "POST /api/symbol/{sym}/refresh": "an on-demand refresh round is orchestrated with the app's IBKR bridge "
                                      "(renderer and main process); agents read `symbol_refresh_status`",
    "POST /api/sources/questrade/token": "Questrade token (a secret)",
    "DELETE /api/sources/questrade/token": "Questrade token (a secret)",
    "POST /api/live/subscribe": "the renderer's live channel",
    "GET /api/live/stream": "the renderer's live channel (Server-Sent Events)",
    "PUT /api/live/mode": "live mode switching (streaming freezes the user's other Questrade platforms)",
    "GET /api/mcp": "AI-agent access administration (the app's Settings only)",
    "PUT /api/mcp": "AI-agent access administration (the app's Settings only)",
    "POST /api/mcp/regenerate": "AI-agent access administration (the app's Settings only)",
    "PUT /api/view": "the renderer reports its current view (agents read it with `get_current_view`)",
    "GET /api/portfolio/settings": "the user's \"Show my positions\" switches (the app's Settings only)",
    "PUT /api/portfolio/settings": "the user's \"Show my positions\" switches (the app's Settings only)",
}
# Gates: tools the user must allow on their own (Settings → AI agents), besides agent access itself.
PORTFOLIO = "portfolio"
GATES: dict[str, str] = {
    PORTFOLIO: "The user has not allowed AI agents to see the portfolio. They can turn it on in the OptionLens app: "
               "Settings → AI agents (MCP) → \"Allow agents to see my portfolio\".",
}
# Not engine endpoints at all (the app's main process, over IPC): data import, quitting / restarting the engine,
# updates, the IBKR bridge's connection settings. No MCP tool can reach them.

# ----------------------------------------------------------------------------- the curated layer
U_PRICE = "Prices in USD per share (option prices too: ×100 for one contract)."
U_FRAC = "Percentages, changes and IV are fractions (0.25 = 25%)."
U_GREEKS = ("Greeks per share: delta and gamma per $1 of the underlying, theta $/calendar day, vega $ per 1 vol "
            "point.")
U_PROV = ("Provenance on rows: source, delayed, after_hours; iv_source/greeks_source 'computed*' = engine-computed. "
          "null = unknown (never 0). See describe_units.")
U_TABLE = ("ReportResult: columns[] (key, label, fmt: pct/vol = fractions, money = $/share, bigmoney = $) and "
           "rows[]; notes explain coverage and proxies.")
PAGING = "Long arrays are paginated (limit/offset; offset < 0 counts from the end); `_page` says what was cut."


@dataclass(frozen=True)
class Curated:
    name: str
    description: str
    read_only: bool = True
    destructive: bool = False
    idempotent: bool = True
    # An extra object argument whose entries are sent as query parameters (a report's own params).
    params_object: str | None = None
    # Arguments the MCP server handles itself (never sent to the engine), as JSON-schema properties.
    local_args: Mapping[str, dict] = field(default_factory=dict)
    # A shaping step of the MCP server (shaping.py) applied to the answer: "report_list" | "current_view".
    post: str | None = None
    # A permission of its own the user must grant (GATES), else the tool is hidden and refused.
    gate: str | None = None


def _w(name: str, description: str, *, destructive: bool = False, idempotent: bool = True) -> Curated:
    return Curated(name, description, read_only=False, destructive=destructive, idempotent=idempotent)


CURATED: dict[str, Curated] = {
    # ---- system
    "GET /api/status": Curated("system_status", "Engine and data-collection status: market open, universe size, "
                               "last sweep / end-of-day dates, history depth, jobs, data sources (IBKR bridge, "
                               "Questrade) with their health."),
    "GET /api/search": Curated("symbol_search", "Find symbols by ticker or company name (q)."),
    "GET /api/sources/questrade": Curated("source_questrade_status", "Questrade connection state (never the token): "
                                          "state, real-time vs delayed, calls this hour, last sweep, top failures."),
    "GET /api/live/status": Curated("live_status", "Live-quote status: state (live/delayed/stale/market_closed/...), "
                                    "source, session, mode (stream/poll), what is kept live."),
    "GET /api/live/mode": Curated("live_mode", "Questrade live mode setting: {mode: stream|poll, default, saved}."),
    "GET /api/live/quotes": Curated("live_quotes", "Snapshot of the latest live values in memory (only what is on the "
                                    "user's screen is kept live): quotes {SYM: price, change, change_pct, bid, ask, "
                                    "volume, ts, source, delayed} and options {OSI: bid, ask, mid, last, iv, greeks}. "
                                    f"{U_PRICE} {U_FRAC} Missing keys are not live: use symbol_summary / symbol_chain."),
    # ---- the current view (screen context)
    "GET /api/view": Curated("get_current_view", "What the user is looking at in OptionLens right now: the view "
                             "(route, symbol, tab, report id and effective params, table sort/filters/hidden columns, "
                             "visible row range) AND the data the page shows, fetched again with the same requests. "
                             "Table rows get the user's filters and sort; by default only the visible rows (at most "
                             f"{PAGE_DEFAULT}) and visible columns are returned. {PAGING}", post="current_view"),
    # ---- symbol pages
    "GET /api/symbol/{sym}/refresh": Curated("symbol_refresh_status", "State of the latest on-demand chain refresh "
                                             "round of a symbol (per source: pending/ok/no_options/unavailable/failed)."),
    "GET /api/symbol/{sym}/summary": Curated("symbol_summary", "One symbol at a glance: name, kind, sector, quote, "
                                             "52w/3y range, beta, IV30 with IV rank/percentile, HV20, option volume, "
                                             f"next earnings / dividend, optionable (true/false/null=unknown). {U_PRICE} "
                                             f"{U_FRAC} {U_PROV}"),
    "GET /api/symbol/{sym}/key-stats": Curated("symbol_key_stats", "Fundamental key stats at the current price: P/E "
                                               "(price / EPS), EPS, market cap (price × shares), shares outstanding, "
                                               "52-week high/low, 20-day and 3-month average volume, sector / industry "
                                               "group / sub-group, dividend yield (fraction); `sources` names each "
                                               "figure's source (questrade, nasdaq, ibkr-adjusted, computed), `asof` the "
                                               f"snapshot date. {U_PRICE}"),
    "GET /api/symbol/{sym}/bars": Curated("symbol_bars", "Daily OHLCV bars (oldest first) with SMA20/50/200; with "
                                          "interval=1m|5m|15m|1h and range=1d|5d|1m|3m, intraday bars instead (ts = bar "
                                          "start, regular hours unless extended=true; `missing` = spans not stored; "
                                          "`vwap` = the day's VWAP after each bar (regular-hours bars only; with "
                                          "extended=true every bar from the pre-market on), `vwap_source` "
                                          "'computed' when from typical prices). "
                                          f"{U_PRICE} {PAGING}"),
    "GET /api/symbol/{sym}/price-action": Curated("symbol_price_action", f"Daily moves (newest first), gap and "
                                                  f"intraday vs overnight statistics. {U_PRICE} {U_FRAC}"),
    "GET /api/symbol/{sym}/chain": Curated("symbol_chain", "Option chain of one expiry (default: nearest): underlying "
                                           "quote, expiries[] (dte, atm_iv, straddle, volumes, OI), strikes, calls[] and "
                                           f"puts[] by strike (bid/ask/mid/last, volume, oi, iv, greeks). {U_PRICE} "
                                           f"{U_FRAC} {U_GREEKS} {U_PROV} {PAGING}"),
    "GET /api/symbol/{sym}/activity": Curated("symbol_activity", "Option volume / OI activity: by expiry, by strike "
                                              "(one expiry, or expiry=all), top contracts by volume, totals (premium "
                                              f"in $), moneyness split, daily history. {U_FRAC}"),
    "GET /api/symbol/{sym}/open-interest": Curated("symbol_open_interest", "Open interest by strike, max pain per "
                                                   "expiry, biggest OI increases / decreases since the prior session."),
    "GET /api/symbol/{sym}/contract/{osi}": Curated("symbol_contract", "One option contract: latest quote and its "
                                                    f"daily history (bid/ask/mid, iv, volume, oi, delta, underlying). "
                                                    f"{U_PRICE} {U_FRAC}"),
    "GET /api/symbol/{sym}/volatility": Curated("symbol_volatility", "Volatility: IV30 with rank/percentile, 52w IV "
                                                "range, constant-maturity IVs (30-360d), term structure (today, 1w, 1m "
                                                f"ago), HV windows, Parkinson, IV/HV history. {U_FRAC} {PAGING}"),
    "GET /api/symbol/{sym}/skew": Curated("symbol_skew", f"IV skew of one expiry by strike and by delta, skew history. "
                                          f"{U_FRAC}"),
    "GET /api/symbol/{sym}/implied-moves": Curated("symbol_implied_moves", "Implied move per expiry from the ATM "
                                                   f"straddle and from IV, with the price range. {U_PRICE} {U_FRAC}"),
    "GET /api/symbol/{sym}/vol-seasonality": Curated("symbol_vol_seasonality", f"Average IV30 / HV20 by calendar month. "
                                                     f"{U_FRAC}"),
    "GET /api/symbol/{sym}/earnings": Curated("symbol_earnings", "Earnings: next date with implied vs historical move, "
                                              "past events (timing, EPS, surprise, gap / move, drift) and stats. "
                                              f"{U_FRAC}"),
    "GET /api/symbol/{sym}/earnings/strategies": Curated("symbol_earnings_strategies", "Backtest of option strategies "
                                                         "around past earnings (lookback events, entry/exit days "
                                                         "relative to the event): win rate, returns, real vs model-"
                                                         f"priced trades. {U_FRAC}"),
    "GET /api/symbol/{sym}/earnings/strategies/{strategy}/trades": Curated(
        "symbol_earnings_strategy_trades", f"The individual trades of one earnings strategy backtest. {U_FRAC}"),
    "GET /api/symbol/{sym}/dividends": Curated("symbol_dividends", "Dividends: annual rate, yield, frequency, growth, "
                                               "next ex-date, history (each row's `source`: nasdaq, ibkr-adjusted = "
                                               "derived from IBKR's dividend-adjusted closes, questrade = its current "
                                               "dividend; `discrepancy` = the sources differ by > $0.01) and "
                                               f"dividend-capture results. {U_FRAC}"),
    "GET /api/symbol/{sym}/seasonality": Curated("symbol_seasonality", f"Monthly / weekday return seasonality. {U_FRAC}"),
    "GET /api/symbol/{sym}/events": Curated("symbol_events", "Upcoming and past events: earnings, ex-dividend dates, "
                                            "option expiries."),
    "GET /api/symbol/{sym}/straddle": Curated("symbol_straddle", "ATM straddle per expiry (price, % of spot, break-"
                                              f"evens) and a rolling 30-DTE straddle backtest. {U_PRICE} {U_FRAC}"),
    "GET /api/symbol/{sym}/ideas": Curated("symbol_ideas", "Screened trade ideas (verticals, condors, straddles, "
                                           "calendars...) ranked by edge and by win rate: cost / max profit / max loss "
                                           "in $ per position, probability of profit (fraction)."),
    "GET /api/symbol/{sym}/fundamentals": Curated("symbol_fundamentals", "Key stats and income / balance / cash-flow / "
                                                  "ratio tables (annual or quarterly), values as reported (text)."),
    "GET /api/symbol/{sym}/peers": Curated("symbol_peers", f"Peers comparison table. {U_TABLE}"),
    "GET /api/symbol/{sym}/insiders": Curated("symbol_insiders", "Insider trading summary and trades."),
    "GET /api/symbol/{sym}/news": Curated("symbol_news", "Recent news headlines with links."),
    "GET /api/symbol/{sym}/profile": Curated("symbol_profile", "Company profile: description, sector, industry, "
                                             "exchange, website, peers."),
    # ---- calculator and backtests (read-only POSTs: they compute, nothing is stored)
    "POST /api/calc/strategy": Curated("calc_strategy", "Price an option / stock position on the stored chain: legs "
                                       "(kind option|stock, right C|P, strike, expiry, qty: + long / - short, "
                                       "options in contracts of 100, stock in shares; price/iv optional overrides). "
                                       "Returns cost ($, + debit / - credit), P&L curves at expiry / eval date / today "
                                       "($ per position), breakevens, max profit / loss (null = unlimited), pop and "
                                       "position greeks ($ per 1 move/day/vol point).", idempotent=True),
    "GET /api/strategy-templates": Curated("calc_strategy_templates", "Predefined strategy templates (legs relative "
                                           "to ATM) for calc_strategy."),
    "POST /api/backtest/stock": Curated("backtest_stock", "Backtest a stock entry/exit rule on daily bars: trades, "
                                        f"summary (win rate, returns, CAGR, Sharpe, drawdown), equity curve. {U_FRAC} "
                                        f"{PAGING}"),
    "POST /api/backtest/options": Curated("backtest_options", "Backtest a rolling options strategy on stored / "
                                          "model-priced chains: trades (real vs model-priced), summary, equity. "
                                          f"{U_FRAC} {PAGING}"),
    # ---- reports
    "GET /api/reports": Curated("report_list", "The report catalog (screeners, rankings, calendars). Without "
                                "report_id: compact list (id, title, category, description, param keys). With "
                                "report_id: its full definition — typed params (type, default, min/max, options) and "
                                "columns (fmt gives units).",
                                local_args={
                                    "category": {"type": "string", "description": "Only this category",
                                                 "enum": ["Options", "Strategies", "Earnings", "Volatility", "Stocks",
                                                          "Dividends"]},
                                    "report_id": {"type": "string", "description": "Full definition of this report"}},
                                post="report_list"),
    "GET /api/reports/{report_id}": Curated("report_run", "Run a report with typed params (keys and types: report_list "
                                            "with report_id; missing params use their defaults). Symbols params take "
                                            f"a comma-separated string or an array. {U_TABLE} {PAGING} `columns` picks "
                                            "a subset of row keys.", params_object="params"),
    "GET /api/compare/straddle": Curated("compare_straddle", "Compare ATM straddles (~dte days) across symbols. "
                                         f"{U_TABLE}"),
    "GET /api/compare/volatility": Curated("compare_volatility", f"Compare IV / HV across symbols. {U_FRAC}"),
    "GET /api/market/overview": Curated("market_overview", "Dashboard: index cards, VIX term structure and history, "
                                        "sectors, gainers / losers, unusual options, IV movers, earnings today / "
                                        f"tomorrow, first watchlist. {U_PRICE} {U_FRAC} {PAGING}"),
    # ---- watchlists
    "GET /api/watchlists": Curated("watchlist_list", "All watchlists: id, name, symbols."),
    "GET /api/watchlists/{wid}": Curated("watchlist_get", "One watchlist: id, name, symbols."),
    "GET /api/watchlists/{wid}/table": Curated("watchlist_table", f"A watchlist as a table of quotes and metrics. "
                                               f"{U_TABLE}"),
    "POST /api/watchlists": _w("watchlist_create", "Create a watchlist {name, symbols?}.", idempotent=False),
    "PUT /api/watchlists/{wid}": _w("watchlist_update", "Rename a watchlist and/or replace its whole symbol list.",
                                    destructive=True),
    "DELETE /api/watchlists/{wid}": _w("watchlist_delete", "Delete a watchlist.", destructive=True),
    "POST /api/watchlists/{wid}/symbols/{sym}": _w("watchlist_add_symbol", "Add a symbol to a watchlist."),
    "DELETE /api/watchlists/{wid}/symbols/{sym}": _w("watchlist_remove_symbol", "Remove a symbol from a watchlist.",
                                                     destructive=True),
    # ---- presets
    "GET /api/presets": Curated("preset_list", "Saved report presets (params, filters, sort, hidden columns), "
                                "optionally of one report."),
    "POST /api/presets": _w("preset_save", "Save a report preset {report_id, name, payload: {params, filters: {col: "
                            "{min,max,text,values}}, sort: {key, dir}, hidden: [col]}}. Filter bounds in the API's "
                            "units (fractions for pct/vol columns).", idempotent=False),
    "DELETE /api/presets/{pid}": _w("preset_delete", "Delete a saved preset.", destructive=True),
    # ---- alerts and notifications
    "GET /api/alerts": Curated("alert_list", "Alerts: id, kind, symbol, params {value, watchlist_id}, enabled, "
                               "cooldown_min, last_fired_at."),
    "POST /api/alerts": _w("alert_create", "Create an alert {kind, symbol (or params.watchlist_id), params: {value}, "
                           "enabled, cooldown_min}. Values in the API's units (fractions for pct / IV kinds).",
                           idempotent=False),
    "PUT /api/alerts/{aid}": _w("alert_update", "Change an alert's fields (only those given).", destructive=True),
    "DELETE /api/alerts/{aid}": _w("alert_delete", "Delete an alert.", destructive=True),
    # ---- the user's portfolio (read-only; only while the user allowed it: gate "portfolio")
    "GET /api/portfolio": Curated("portfolio_positions", "The user's brokerage positions (Questrade, IBKR), read-only: "
                                  "per underlying the positions (qty: shares or contracts, avg_cost and price per "
                                  "share, market_value / cost_basis / unrealized_pnl / day_pnl in $ of the position's "
                                  "currency, unrealized_pct a fraction), position greeks (delta in shares, "
                                  "delta_dollars, gamma per $1, theta $/day, vega $/vol pt), aggregates per account / "
                                  "broker / total by currency and converted with the brokers' FX rates (null when no "
                                  "rate), balances, beta-weighted delta in SPY shares. Account numbers are masked. "
                                  "No trading exists.", gate=PORTFOLIO),
    "GET /api/portfolio/risk": Curated("portfolio_risk", "Risk graph of the user's positions on one underlying: P/L "
                                       "($ per position total) at the nearest expiry and today across underlying "
                                       "prices, each leg at its cost basis; breakevens, max profit / loss, greeks.",
                                       gate=PORTFOLIO),
    "GET /api/notifications": Curated("notification_list", "Alert notifications (newest first); unread=1 for unread "
                                      "only."),
    "POST /api/notifications/read": _w("notification_mark_read", "Mark notifications read: the given ids, or all "
                                       "when ids is omitted."),
}

# Path / query parameters the schema leaves undocumented.
PARAM_DOCS: dict[str, str] = {
    "sym": "Ticker, e.g. AAPL; indices as SPX or ^SPX",
    "wid": "Watchlist id (watchlist_list)",
    "pid": "Preset id (preset_list)",
    "aid": "Alert id (alert_list)",
    "osi": "OSI option code as in chain rows (`osi`), e.g. AAPL261218C00350000",
    "strategy": "Strategy key from symbol_earnings_strategies rows (`strategy`)",
    "expiry": "Expiry YYYY-MM-DD (default: the nearest; activity / open-interest also take 'all')",
    "range": "History range",
    "symbols": "Comma-separated tickers, e.g. AAPL,MSFT",
    "q": "Search text",
    "report_id": "Report id (report_list)",
    "unread": "1 = unread only",
    "dte": "Target days to expiry",
    "period": "annual or quarterly",
    "lookback": "Number of past earnings events",
    "entry": "Entry day relative to the earnings reaction day (negative = before)",
    "exit": "Exit day relative to the earnings reaction day",
    "contracts": "Comma-separated OSI codes",
}

PAGE_ARGS: dict[str, dict] = {
    "limit": {"type": "integer", "minimum": 1, "maximum": PAGE_MAX, "default": PAGE_DEFAULT,
              "description": "Max items per long array"},
    "offset": {"type": "integer", "default": 0, "description": "First item of long arrays; < 0 counts from the end "
                                                               "(-100 = the last 100)"},
    "columns": {"type": "array", "items": {"type": "string"},
                "description": "Only these keys in table rows (e.g. report columns)"},
}


# ----------------------------------------------------------------------------- tools
@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    method: str
    path: str                                   # OpenAPI path template, e.g. /api/symbol/{sym}/chain
    input_schema: dict
    path_params: tuple[str, ...] = ()
    query_params: tuple[str, ...] = ()
    body_params: tuple[str, ...] | None = None  # None: no request body
    read_only: bool = True
    destructive: bool = False
    idempotent: bool = True
    params_object: str | None = None
    local_args: tuple[str, ...] = ()
    post: str | None = None
    gate: str | None = None

    @property
    def operation(self) -> str:
        return f"{self.method} {self.path}"

    @property
    def paged(self) -> bool:
        return self.read_only

    def request(self, args: Mapping[str, Any]) -> tuple[str, dict[str, str], dict | None]:
        """(path, query, body) of a call with these arguments. ValueError for a missing path parameter."""
        path = self.path
        for p in self.path_params:
            v = args.get(p)
            if v is None or str(v) == "":
                raise ValueError(f"missing required argument {p!r}")
            path = path.replace("{" + p + "}", quote(str(v), safe=""))
        query = {k: query_value(args[k]) for k in self.query_params if args.get(k) is not None}
        if self.params_object and isinstance(args.get(self.params_object), Mapping):
            for k, v in args[self.params_object].items():
                if v is not None:
                    query[str(k)] = query_value(v)
        body = None if self.body_params is None else {k: args[k] for k in self.body_params if k in args}
        return path, query, body


def query_value(v: Any) -> str:
    """A JSON argument as a query-string value: arrays comma-joined, booleans true/false."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple)):
        return ",".join(query_value(x) for x in v)
    return str(v)


def excluded(operation: str) -> str | None:
    """Why an operation ("METHOD /path") is never a tool, or None."""
    if operation in EXCLUDED:
        return EXCLUDED[operation]
    path = operation.split(" ", 1)[1]
    return next((why for prefix, why in EXCLUDED_PREFIXES.items() if path.startswith(prefix)), None)


def auto_name(method: str, path: str) -> str:
    """A tool name from an uncurated GET path: /api/symbol/{sym}/price-action -> symbol_price_action."""
    parts = [p for p in path.removeprefix("/api/").split("/") if p and not p.startswith("{")]
    name = "_".join(parts).replace("-", "_")
    return name if method == "GET" else f"{method.lower()}_{name}"


def build(openapi: Mapping[str, Any]) -> list[ToolDef]:
    """The tools of an OpenAPI document (FastAPI's app.openapi()). ValueError on duplicate names / argument clashes."""
    components = openapi.get("components", {}).get("schemas", {})
    tools: list[ToolDef] = []
    for path, ops in openapi.get("paths", {}).items():
        if not path.startswith("/api/"):
            continue
        for method, op in ops.items():
            m = method.upper()
            operation = f"{m} {path}"
            cur = CURATED.get(operation)
            if excluded(operation) or (cur is None and m != "GET"):
                continue
            tools.append(_tool(m, path, op, cur, components))
    names = [t.name for t in tools]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate tool names: {sorted({n for n in names if names.count(n) > 1})}")
    return tools


def _tool(method: str, path: str, op: Mapping, cur: Curated | None, components: Mapping) -> ToolDef:
    defs: dict[str, Any] = {}
    props: dict[str, Any] = {}
    required: list[str] = []
    path_params, query_params = [], []
    for prm in op.get("parameters", []):
        name, where = prm["name"], prm["in"]
        if where not in ("path", "query"):
            continue
        s = _clean(prm.get("schema", {}), components, defs)
        doc = prm.get("description") or s.get("description") or PARAM_DOCS.get(name)
        props[name] = {**s, **({"description": doc} if doc else {})}
        (path_params if where == "path" else query_params).append(name)
        if prm.get("required"):
            required.append(name)
    body_params: tuple[str, ...] | None = None
    if "requestBody" in op:
        raw = op["requestBody"]["content"]["application/json"]["schema"]
        body = _clean(_body_object(raw, components), components, defs)
        for k, v in body.get("properties", {}).items():
            if k in props:
                raise ValueError(f"{method} {path}: body field {k!r} clashes with a parameter")
            props[k] = v
        required += [k for k in body.get("required", []) if k not in required]
        body_params = tuple(body.get("properties", {}))
    read_only = method == "GET" if cur is None else cur.read_only
    if cur is not None and cur.params_object:
        props[cur.params_object] = {"type": "object", "description": "Report params by key (see report_list)",
                                    "additionalProperties": {"type": ["string", "number", "boolean", "array"]}}
    for k, v in (cur.local_args if cur else {}).items():
        props[k] = v
    if read_only:
        for k, v in PAGE_ARGS.items():
            if k in props:
                raise ValueError(f"{method} {path}: argument {k!r} clashes with the paging arguments")
            props[k] = v
    schema: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = required
    if defs:
        schema["$defs"] = defs
    doc = (op.get("description") or op.get("summary") or "").strip()
    return ToolDef(
        name=cur.name if cur else auto_name(method, path),
        description=cur.description if cur else f"{doc or 'Engine endpoint'} ({method} {path}).",
        method=method, path=path, input_schema=schema, path_params=tuple(path_params),
        query_params=tuple(query_params), body_params=body_params, read_only=read_only,
        destructive=bool(cur and cur.destructive), idempotent=cur.idempotent if cur else True,
        params_object=cur.params_object if cur else None, local_args=tuple(cur.local_args) if cur else (),
        post=cur.post if cur else None, gate=cur.gate if cur else None)


_REF = "#/components/schemas/"


def _clean(s: Any, components: Mapping, defs: dict) -> Any:
    """A compact JSON schema: titles dropped, `anyOf [X, null]` (optional) collapsed to X, component refs moved to
    `$defs` (collected, transitively, into `defs`)."""
    if isinstance(s, list):
        return [_clean(x, components, defs) for x in s]
    if not isinstance(s, dict):
        return s
    out = {k: _clean(v, components, defs) for k, v in s.items() if k != "title"}
    if isinstance(out.get("anyOf"), list):
        rest = [x for x in out["anyOf"] if x != {"type": "null"}]
        if len(rest) == 1 and len(rest) < len(out["anyOf"]):
            del out["anyOf"]
            out = {**rest[0], **out}
            if isinstance(out.get("enum"), list):
                out["enum"] = [e for e in out["enum"] if e is not None]
    ref = out.get("$ref")
    if isinstance(ref, str) and ref.startswith(_REF):
        name = ref[len(_REF):]
        out["$ref"] = f"#/$defs/{name}"
        if name not in defs:
            defs[name] = {}
            defs[name] = _clean(components[name], components, defs)
    return out


def _body_object(s: Mapping, components: Mapping) -> Mapping:
    """The (raw) object schema of a request body: an optional body's object, a component ref's definition."""
    alts = [x for x in s.get("anyOf", ()) if x != {"type": "null"}]
    if len(alts) == 1:
        s = alts[0]
    ref = s.get("$ref")
    return components[ref[len(_REF):]] if isinstance(ref, str) and ref.startswith(_REF) else s


# ----------------------------------------------------------------------------- matching requests to tools
def _pattern(template: str) -> re.Pattern:
    out = re.sub(r"\\\{[A-Za-z_][A-Za-z0-9_]*\\\}", "[^/]+", re.escape(template))
    return re.compile(f"^{out}$")


class Matcher:
    """Which tool an HTTP request (method, path) belongs to: the engine's auth decides with it what the MCP key may
    call; the MCP server checks the current view's requests with it."""

    def __init__(self, tools: list[ToolDef]):
        self._by_method: dict[str, list[tuple[re.Pattern, ToolDef]]] = {}
        # Literal paths first, so /api/watchlists/{wid} never shadows a literal sibling.
        for t in sorted(tools, key=lambda t: t.path.count("{")):
            self._by_method.setdefault(t.method, []).append((_pattern(t.path), t))

    def match(self, method: str, path: str) -> ToolDef | None:
        return next((t for rx, t in self._by_method.get(method.upper(), ()) if rx.match(path)), None)
