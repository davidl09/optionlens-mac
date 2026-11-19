"""Shared column sets (built once, reused by every report family). Keys equal universe-frame
column keys, so frame-derived reports materialize rows with `frame.rows(idx, keys)`."""
from __future__ import annotations

from .base import Col, group

G_DESC, G_QUOTE, G_PERF, G_TECH = "Description", "Quote", "Performance", "Technicals"
G_VOL, G_OPT, G_MONEY, G_CAT, G_DIV = "Volatility", "Option activity", "Volume by moneyness", "Catalysts", "Dividends"

# ---- stock description
SYMBOL = Col("symbol", "Symbol", "symbol")
DESC = group(G_DESC, (
    SYMBOL,
    Col("name", "Name", "text"),
    Col("market_cap", "Market Cap", "bigmoney"),
    Col("kind", "Type", "badge"),
    Col("sector", "Sector", "text", default_hidden=True),
    Col("industry", "Industry", "text", default_hidden=True),
    Col("in_watchlist", "In Watchlist", "bool", default_hidden=True),
))
DESC_SHORT = tuple(c for c in DESC if c.key in ("symbol", "name", "market_cap", "sector"))

# ---- quote
QUOTE = group(G_QUOTE, (
    Col("price", "Price", "money"),
    Col("change_pct", "% Chg", "pct_signed"),
    Col("prev_close", "Prev Close", "money", default_hidden=True),
    Col("volume", "Volume", "int"),
))

# ---- performance
PERF = group(G_PERF, (
    Col("ret_5d", "% Chg 5d", "pct_signed"),
    Col("ret_1m", "% Chg 1m", "pct_signed"),
    Col("ret_3m", "% Chg 3m", "pct_signed"),
    Col("ret_6m", "% Chg 6m", "pct_signed", default_hidden=True),
    Col("ret_ytd", "% Chg YTD", "pct_signed"),
    Col("ret_1y", "% Chg 1y", "pct_signed"),
    Col("gap_pct", "Gap %", "pct_signed", description="Today's open / previous close − 1"),
))

# ---- technicals
TECH = group(G_TECH, (
    Col("avg_volume_30d", "Avg Vol 30d", "int"),
    Col("rel_volume", "Rel Vol", "ratio", description="Today's volume / 30-day average"),
    Col("beta", "Beta (1y)", "num2"),
    Col("corr_spy", "Corr SPY (1y)", "num2", default_hidden=True),
    Col("rsi14", "RSI14", "num"),
    Col("atr14_pct", "ATR %", "pct"),
    Col("pct_from_high_52w", "% from 52w High", "pct_signed"),
    Col("pct_from_low_52w", "% from 52w Low", "pct_signed"),
    Col("pct_vs_sma20", "% vs SMA20", "pct_signed", default_hidden=True),
    Col("pct_vs_sma50", "% vs SMA50", "pct_signed"),
    Col("pct_vs_sma200", "% vs SMA200", "pct_signed"),
))

# ---- volatility
VOL_CORE = group(G_VOL, (
    Col("iv30", "IV30", "vol"),
    Col("iv_rank", "IV Rank", "pct", description="52w IV rank over stored IV30 history (needs ≥ 20 days)"),
    Col("iv_pct", "IV Pct", "pct", description="Share of stored IV30 values below today's (needs ≥ 20 days)"),
    Col("hv20", "HV20", "vol"),
    Col("hv60", "HV60", "vol", default_hidden=True),
    Col("iv_hv_ratio", "IV/HV", "ratio", description="IV30 / HV20"),
))
IV_CHANGES = group(G_VOL, (
    Col("iv30_chg_1d", "IV30 1d chg (pts)", "vol", description="IV30 − previous stored IV30, vol points"),
    Col("iv30_chg_pct", "IV30 % chg", "pct_signed", description="IV30 / previous stored IV30 − 1"),
    Col("iv30_chg_5d", "IV30 5d chg (pts)", "vol", description="IV30 − IV30 five stored days earlier"),
))
TERM = group(G_VOL, (
    Col("iv60", "IV60", "vol"),
    Col("iv90", "IV90", "vol"),
    Col("term_slope", "Term slope", "vol", description="IV90 − IV30 (vol points)"),
    Col("skew25_30", "25Δ skew", "vol", description="Put 25Δ IV − call 25Δ IV at 30 days"),
))

# ---- option activity
OPT = group(G_OPT, (
    Col("option_volume", "Option Volume", "int"),
    Col("avg_option_volume_20d", "Avg Opt Vol (20d)", "int",
        description="Mean daily option volume of the prior stored days (needs ≥ 5)"),
    Col("rel_option_volume", "Rel Opt Vol", "ratio", description="Option volume / 20d average"),
    Col("pc_volume_ratio", "P/C Vol", "ratio"),
    Col("total_oi", "Total OI", "int"),
))
FLOW = group(G_OPT, (
    Col("call_volume", "Call Vol", "int"),
    Col("put_volume", "Put Vol", "int"),
    Col("pct_call", "% Call", "pct"),
    Col("pct_put", "% Put", "pct"),
    Col("call_premium", "$ Call", "bigmoney", description="Σ volume × mid × 100"),
    Col("put_premium", "$ Put", "bigmoney", description="Σ volume × mid × 100"),
))
MONEYNESS = group(G_MONEY, tuple(
    Col(f"{side}_vol_{b}", f"{side.title()} {b.upper()}", "int", default_hidden=True)
    for side in ("call", "put") for b in ("otm", "atm", "itm")))

# ---- catalysts
CATALYSTS = group(G_CAT, (
    Col("next_earnings", "Next Earnings", "date"),
    Col("days_to_earnings", "Days To Earnings", "int"),
    Col("days_since_earnings", "Days Since Earnings", "int", default_hidden=True),
    Col("next_ex_div", "Next Ex-Div", "date"),
    Col("days_to_ex_div", "Days To Ex-Div", "int", default_hidden=True),
))
DIV_YIELD = Col("div_yield", "Div Yield", "pct", G_DIV, "Trailing-12m dividends / price (Nasdaq-listed only)")

# The 30+ stock-screener metric columns (stock-screener, etf-screener, watchlist tables).
STOCK_SCREENER = (*DESC, *QUOTE, Col("ret_1d", "% Chg 1d", "pct_signed", G_PERF, default_hidden=True), *PERF,
                  *TECH, DIV_YIELD, *CATALYSTS, *VOL_CORE, *OPT)
MOVERS = (*DESC, *QUOTE, Col("rel_volume", "Rel Vol", "ratio", G_TECH), Col("avg_volume_30d", "Avg Vol 30d", "int", G_TECH),
          Col("gap_pct", "Gap %", "pct_signed", G_PERF), Col("ret_5d", "% Chg 5d", "pct_signed", G_PERF),
          Col("ret_1m", "% Chg 1m", "pct_signed", G_PERF), Col("iv30", "IV30", "vol", G_VOL),
          Col("iv_rank", "IV Rank", "pct", G_VOL), *CATALYSTS[:2])

# Notes shared by every frame-derived report.
NOTE_IV_RANK = ("IV rank / IV percentile need ≥ 20 stored daily IV30 values (collected since the first "
                "sweep); they are blank until then.")
NOTE_OPT_AVG = ("Average / relative option volume need ≥ 5 prior collected days of option volume; blank "
                "until then.")
NOTE_DIVS = "Dividend data (yield, ex-dates) covers Nasdaq-listed securities only; NYSE/ARCA listings show blanks."
NOTE_DELAY = "Quotes and chains are ~15 min delayed (CBOE delayed feed)."


def keys(cols) -> list[str]:
    return [c.key for c in cols]
