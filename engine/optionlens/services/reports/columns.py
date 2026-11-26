"""Shared column sets (built once, reused by every report family). Keys equal universe-frame
column keys, so frame-derived reports materialize rows with `frame.rows(idx, keys)`."""
from __future__ import annotations

from .base import Col, group

G_DESC, G_QUOTE, G_PERF, G_TECH = "Description", "Quote", "Performance", "Technicals"
G_VOL, G_OPT, G_MONEY, G_CAT, G_DIV = "Volatility", "Option activity", "Volume by moneyness", "Catalysts", "Dividends"
G_FUND = "Fundamentals"

# ---- stock description
SYMBOL = Col("symbol", "Symbol", "symbol")
DESC = group(G_DESC, (
    SYMBOL,
    Col("name", "Name", "text"),
    Col("market_cap", "Market Cap", "bigmoney", description="Price × shares outstanding (Questrade's daily symbol "
        "info), else Questrade's or Nasdaq's figure"),
    Col("kind", "Type", "badge"),
    Col("sector", "Sector", "text", default_hidden=True),
    Col("industry", "Industry", "text", default_hidden=True),
    Col("in_watchlist", "In Watchlist", "bool", default_hidden=True),
))
DESC_SHORT = tuple(c for c in DESC if c.key in ("symbol", "name", "market_cap", "sector"))

# ---- quote
HALTED = Col("halted", "Halt", "halt", G_QUOTE, "HALTED while the quote source reports a trading halt (Questrade)")
QUOTE = group(G_QUOTE, (
    Col("price", "Price", "money"),
    Col("change_pct", "% Chg", "pct_signed"),
    Col("prev_close", "Prev Close", "money", default_hidden=True),
    Col("volume", "Volume", "int"),
))
VWAP = group(G_QUOTE, (
    Col("vwap", "VWAP", "money", description="The session's volume-weighted average price as the quote reports it "
        "(Questrade)"),
    Col("pct_vs_vwap", "% vs VWAP", "pct_signed", description="Price / VWAP − 1"),
))

# ---- fundamentals (services.fundamentals: one source for every page)
FUNDAMENTALS = group(G_FUND, (
    Col("pe", "P/E", "num2", description="Price / EPS (Questrade's daily symbol info; positive EPS only)"),
    Col("eps", "EPS", "money", description="Earnings per share (Questrade)"),
    Col("shares_outstanding", "Shares Out", "int", default_hidden=True, description="Questrade"),
    Col("avg_volume_20d", "Avg Vol 20d", "int", default_hidden=True, description="Questrade's 20-day average volume"),
    Col("avg_volume_3m", "Avg Vol 3m", "int", default_hidden=True, description="Questrade's 3-month average volume"),
    Col("industry_sector", "Sector (Questrade)", "text", default_hidden=True),
    Col("industry_group", "Industry group", "text", default_hidden=True),
    Col("industry_subgroup", "Industry sub-group", "text", default_hidden=True),
    Col("fundamentals_source", "Fundamentals source", "text", default_hidden=True,
        description="questrade = the daily symbol info; nasdaq = market cap only (no Questrade data yet)"),
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
    Col("iv30", "IV30", "vol", description="Implied volatility for 30 days to expiry, annualized (vol points)"),
    Col("iv_rank", "IV Rank", "pct", description="Where today's IV30 sits between its 52-week low (0%) and high (100%); "
        "needs 20 stored days"),
    Col("iv_pct", "IV Pct", "pct", description="Share of the last year's stored IV30 values below today's; needs 20 "
        "stored days"),
    Col("hv20", "HV20", "vol", description="Historical volatility: how much the price actually moved over the last "
        "20 sessions, annualized"),
    Col("hv60", "HV60", "vol", default_hidden=True, description="Historical volatility over the last 60 sessions, "
        "annualized"),
    Col("iv_hv_ratio", "IV/HV", "ratio", description="IV30 / HV20"),
))
IV_CHANGES = group(G_VOL, (
    Col("iv30_chg_1d", "IV30 1d chg (pts)", "vol", description="IV30 − previous stored IV30, vol points"),
    Col("iv30_chg_pct", "IV30 % chg", "pct_signed", description="IV30 / previous stored IV30 − 1"),
    Col("iv30_chg_5d", "IV30 5d chg (pts)", "vol", description="IV30 − IV30 five stored days earlier"),
))
TERM = group(G_VOL, (
    Col("iv60", "IV60", "vol", description="Implied volatility for 60 days to expiry, annualized"),
    Col("iv90", "IV90", "vol", description="Implied volatility for 90 days to expiry, annualized"),
    Col("term_slope", "Term slope", "vol", description="IV90 − IV30 (vol points)"),
    Col("skew25_30", "25Δ skew", "vol", description="Put 25Δ IV − call 25Δ IV at 30 days"),
))

# ---- option activity
OPT = group(G_OPT, (
    Col("option_volume", "Option Volume", "int"),
    Col("avg_option_volume_20d", "Avg Opt Vol (20d)", "int",
        description="Mean daily option volume of the prior stored days (needs ≥ 5)"),
    Col("rel_option_volume", "Rel Opt Vol", "ratio", description="Option volume / 20d average"),
    Col("pc_volume_ratio", "P/C Vol", "ratio", description="Put/call ratio: put volume ÷ call volume"),
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
DIV_YIELD = Col("div_yield", "Div Yield", "pct", G_DIV, "Indicated annual dividend (latest regular × payments per "
                "year) / price; Questrade's yield while the history is too short")

# The 30+ stock-screener metric columns (stock-screener, etf-screener, watchlist tables).
STOCK_SCREENER = (*DESC, *QUOTE, *VWAP, HALTED, Col("ret_1d", "% Chg 1d", "pct_signed", G_PERF, default_hidden=True),
                  *PERF, *TECH, *FUNDAMENTALS, DIV_YIELD, *CATALYSTS, *VOL_CORE, *OPT)
MOVERS = (*DESC, *QUOTE, *VWAP, HALTED, Col("rel_volume", "Rel Vol", "ratio", G_TECH),
          Col("avg_volume_30d", "Avg Vol 30d", "int", G_TECH),
          Col("gap_pct", "Gap %", "pct_signed", G_PERF), Col("ret_5d", "% Chg 5d", "pct_signed", G_PERF),
          Col("ret_1m", "% Chg 1m", "pct_signed", G_PERF), Col("iv30", "IV30", "vol", G_VOL),
          Col("iv_rank", "IV Rank", "pct", G_VOL), *CATALYSTS[:2])

# Notes shared by every frame-derived report.
NOTE_CHAIN_UNIVERSE = ("Covers the chain universe only: watchlist symbols, recently opened symbols and the most "
                       "liquid symbols by option volume.")
NOTE_IV_RANK = "IV rank and IV percentile stay blank until 20 days of IV30 are stored."
NOTE_OPT_AVG = ("Average option volume uses up to 20 prior collected days (fewer while history builds) and is blank "
                "before the first one; it is never estimated.")
NOTE_DIVS = ("Dividends of US listings: Nasdaq's where it has them, else derived from IBKR's dividend-adjusted "
             "closes, else Questrade's current dividend; of Canadian listings: TMX Money's history (in today's shares, "
             "CAD payments only), else Questrade's current dividend. Blank until a source has data.")
NOTE_DELAY = ("Prices are IBKR quotes (real-time or 15-minute delayed, per your market-data subscriptions); "
              "outside the session, the last daily close.")


def keys(cols) -> list[str]:
    return [c.key for c in cols]
