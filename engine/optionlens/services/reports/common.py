"""Helpers shared by report families: common params, frame -> rows, columnar (vectorized) top-N
materialization, and concatenation of per-contract ChainArrays."""
from __future__ import annotations

from datetime import date
from typing import Iterable, Mapping, Sequence

import numpy as np

from .base import MAX_ROWS, Col, Output, Param, RunParams
from ...analytics import markets
from ...models import Bar
from ...repo import bars as bars_repo
from ...repo import options as options_repo
from ...repo import quotes as quotes_repo
from .universe import Frame, get_frame, in_watchlist_mask, with_live_bar

# ----------------------------------------------------------------------------- common params
SYMBOLS = Param("symbols", "Symbols", "symbols", None, help="Comma-separated list (optional)")
WATCHLIST = Param("watchlist", "Watchlist", "watchlist", None, help="Restrict to a watchlist")
SCOPE = (SYMBOLS, WATCHLIST)
KIND = Param("kind", "Type", "select", "all",
             options=(("all", "All"), ("stock", "Stocks"), ("etf", "ETFs"), ("index", "Indices")))
UNIVERSE_ONLY = Param("universe_only", "Universe only", "bool", True,
                      help="Only the symbols OptionLens tracks every day. Off: also symbols you opened outside it")
MIN_PRICE = Param("min_price", "Min price", "number", 5.0, min=0, step=1)
MIN_STOCK_VOLUME = Param("min_volume", "Min volume", "int", 100_000, min=0, step=10_000)
MIN_MCAP = Param("min_market_cap", "Min market cap", "number", None, min=0,
                 help="In the listing's currency (US$ or C$), e.g. 1e10")
_MARKET_HELP = ("US listings (USD) or Canadian ones (TSX / TSX Venture, CAD). Ignored when Symbols are given")
MARKET = Param("market", "Market", "select", "US", options=(("US", "US"), ("CA", "Canada"), ("all", "All")),
               help=_MARKET_HELP)
MARKET_ONE = Param("market", "Market", "select", "US", options=(("US", "US"), ("CA", "Canada")),
                   help=_MARKET_HELP + "; totals never mix markets or currencies")


def p_int(name: str, label: str, default: int | None, lo: float | None = None, hi: float | None = None,
          help: str | None = None, step: float | None = None) -> Param:
    return Param(name, label, "int", default, min=lo, max=hi, help=help, step=step)


def p_num(name: str, label: str, default: float | None, lo: float | None = None, hi: float | None = None,
          help: str | None = None, step: float | None = None) -> Param:
    return Param(name, label, "number", default, min=lo, max=hi, help=help, step=step)


def p_select(name: str, label: str, default: str, options: Sequence[tuple[str, str]], help: str | None = None) -> Param:
    return Param(name, label, "select", default, options=tuple(options), help=help)


# ----------------------------------------------------------------------------- frame masks
def base_mask(f: Frame, p: RunParams) -> np.ndarray:
    """Scope (symbols/watchlist) ∧ market ∧ kind ∧ universe_only ∧ min price/volume/market cap, for params
    the report declares."""
    m = f.select(p.scope)
    if p.market is not None:
        m &= f["market"] == p.market
    kind = p.get("kind")
    if kind and kind != "all":
        m &= f["kind"] == kind
    if p.get("universe_only") and p.scope is None:
        m &= f["in_universe"]
    for key, col in (("min_price", "price"), ("min_market_cap", "market_cap")):
        v = p.get(key)
        if v is not None:
            m &= np.nan_to_num(f[col], nan=-np.inf) >= v
    return m


def chain_scope(p: RunParams) -> list[str] | None:
    """The symbols chain-based reports load: the scope (already within the market), else the market's symbols with
    a served chain, else None (every stored chain)."""
    if p.scope is not None or p.market is None:
        return p.scope
    return [s for s in options_repo.served_symbols() if markets.market_of(s).code == p.market]


def ge(a: np.ndarray, v) -> np.ndarray:
    """a >= v with NaN False (True everywhere when v is None)."""
    if v is None:
        return np.ones(len(a), bool)
    return np.nan_to_num(a, nan=-np.inf) >= v


def le(a: np.ndarray, v) -> np.ndarray:
    if v is None:
        return np.ones(len(a), bool)
    return np.nan_to_num(a, nan=np.inf) <= v


def frame_output(f: Frame, mask: np.ndarray, columns: Sequence[Col], *, rename: Mapping[str, str] | None = None,
                 extra: Mapping[str, np.ndarray] | None = None, notes: Iterable[str] = (),
                 sort: tuple[str, bool] | None = None, limit: int | None = None) -> Output:
    """Rows for frame rows in `mask` with the column keys. `rename` maps output key -> frame key;
    `extra` supplies per-frame-row arrays for keys not in the frame; `in_watchlist` is computed
    per request. `sort` = (key, descending) pre-sorts vectorized before materializing (top `limit`)."""
    rename = dict(rename or {})
    extra = dict(extra or {})
    keys = [c.key for c in columns]
    if "in_watchlist" in keys and "in_watchlist" not in extra:
        extra["in_watchlist"] = in_watchlist_mask(f)
    idx = np.nonzero(mask)[0]
    truncated = False
    if sort is not None:
        sk, desc = sort
        src = extra[sk] if sk in extra else f[rename.get(sk, sk)]
        idx = idx[order_by(src[idx], desc)]
    if limit is not None and len(idx) > limit:
        idx, truncated = idx[:limit], True
    lists = {k: _tolist((extra[k] if k in extra else f[rename.get(k, k)])[idx]) for k in keys}
    rows = [{k: lists[k][i] for k in keys} for i in range(len(idx))]
    return Output(rows, list(notes), asof=f.asof, truncated=truncated, after_hours=after_hours_rows(f, idx))


def after_hours_rows(f: Frame, idx: np.ndarray | None = None) -> bool:
    """Whether any frame row (of `idx`, default all) holds session values from an after-hours fill."""
    a = f.cols.get("after_hours")
    return a is not None and bool(np.nan_to_num(a if idx is None else a[idx]).any())


def order_by(a: np.ndarray, descending: bool = True) -> np.ndarray:
    """argsort with NaN/None last (stable)."""
    if a.dtype == object:
        present = np.array([v is not None for v in a])
        idx_p = np.nonzero(present)[0]
        vals = a[idx_p]
        try:
            o = np.argsort(vals.astype(float) if all(isinstance(v, (int, float)) for v in vals) else vals.astype(str),
                           kind="stable")
        except (TypeError, ValueError):
            o = np.arange(len(idx_p))
        if descending:
            o = o[::-1]
        return np.concatenate([idx_p[o], np.nonzero(~present)[0]]).astype(int)
    v = a.astype(float)
    fin = np.isfinite(v)
    key = np.where(fin, -v if descending else v, np.inf)
    return np.argsort(key, kind="stable")


def _tolist(a: np.ndarray) -> list:
    if a.dtype.kind == "f":
        return [None if x != x else x for x in a.tolist()]
    return a.tolist()


def columnar_rows(cols: Mapping[str, np.ndarray], keys: Sequence[str], sort: tuple[str, bool] | None = None,
                  limit: int | None = MAX_ROWS) -> tuple[list[dict], bool]:
    """Materialize the top `limit` rows of equal-length column arrays, sorted vectorized."""
    n = len(next(iter(cols.values()))) if cols else 0
    idx = np.arange(n)
    if sort is not None and n:
        idx = order_by(np.asarray(cols[sort[0]]), sort[1])
    truncated = limit is not None and n > limit
    if truncated:
        idx = idx[:limit]
    lists = {k: _tolist(np.asarray(cols[k])[idx]) for k in keys if k in cols}
    return [{k: lists[k][i] for k in lists} for i in range(len(idx))], truncated


def frame() -> Frame:
    return get_frame()


def as_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, np.datetime64):
        return None if np.isnat(v) else v.astype("datetime64[D]").astype(object)
    return v


def bar_lists(symbols: Sequence[str], start: date) -> dict[str, list[Bar]]:
    """{symbol: [Bar]} since start (one query), today's quote appended when newer than the last bar."""
    raw = bars_repo.bars_since(start, list(symbols))
    quotes = quotes_repo.all_quotes()
    out = {}
    for s, b in raw.items():
        dates, o, h, lo, c, v, _ = with_live_bar(b, quotes.get(s))
        out[s] = [Bar(s, d, _n(oo), _n(hh), _n(ll), float(cc), None if vv != vv else int(vv))
                  for d, oo, hh, ll, cc, vv in zip(dates, o.tolist(), h.tolist(), lo.tolist(), c.tolist(), v.tolist())]
    return out


def _n(x: float) -> float | None:
    return None if x != x else float(x)
