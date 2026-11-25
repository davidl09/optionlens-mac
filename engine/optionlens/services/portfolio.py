"""The user's portfolio (docs/INTEGRATION.md §11): positions and balances from Questrade (fetched by the engine,
read-only account calls) and IBKR (pushed by the bridge), valued with OptionLens' own market data, aggregated, and
risk graphs per underlying. Read-only: no order, trade or account-changing call exists anywhere in OptionLens.

- Questrade is fetched when the Portfolio page asks and its data is older than QUESTRADE_MAX_AGE_S (the page polls
  every 60 s while visible), or at once on demand (`refresh`). A missing account permission (403 code 1016) is state
  `needs_permission` with what to do; it is not a market-data failure (the token is not flagged) and is asked again
  only on demand or after a new token was saved.
- IBKR: the bridge's pushes (on change and every 60 s while the engine wants them: `wanted("ibkr")`); a push older
  than IBKR_STALE_S is shown as of its time, state `stale`.
- Prices: the live value while some window keeps the contract live (the Portfolio page subscribes what it holds),
  else the fresher of the stored quote / chain and the broker's own price; FX only from the brokers' rates.
- Positions and balances live in memory only; account numbers are masked (portfolio/model.py)."""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence

import numpy as np

from ..analytics import bs
from ..analytics.calendar import NY, now_ny, year_fraction_to_expiry
from ..analytics.strategies import close_dt
from ..config import get_settings
from ..portfolio import ibkr as ibkr_map
from ..portfolio import risk as risk_graph
from ..portfolio import valuation as val
from ..portfolio.model import BROKER_LABEL, BROKERS, Holding
from ..portfolio.store import FILE_NAME, PortfolioSettings, Snapshots
from ..repo import options as options_repo
from ..sources import chain_source
from . import context as ctx
from . import symbol as symbol_service
from .errors import BadRequest, NotFound

log = logging.getLogger("optionlens.portfolio")

QUESTRADE_MAX_AGE_S = 55.0
IBKR_STALE_S = 180.0
LIVE_FRESH_S = 30.0          # a live value older than this is not "live"
CURRENCIES = ("USD", "CAD")
BENCHMARK = "SPY"

_snaps = Snapshots()
_settings: PortfolioSettings | None = None
_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
_qt_accounts: Any = None
_cache_lock = threading.Lock()
_beta: dict[tuple[str, date], float | None] = {}
_prev_mark: dict[tuple[str, date], float | None] = {}


def settings() -> PortfolioSettings:
    global _settings
    path = get_settings().data_dir / FILE_NAME
    if _settings is None or _settings.path != path:
        _settings = PortfolioSettings(path)
    return _settings


def settings_view() -> dict:
    return settings().view()


def set_settings(changes: dict[str, bool]) -> dict:
    """Save "Show my positions" switches; a broker switched off is forgotten at once (hidden, not fetched)."""
    view = settings().set(changes)
    for b, on in view.items():
        if not on:
            _snaps.clear(b)
    return view


def wanted(broker: str) -> bool:
    return settings().enabled(broker)


def reset() -> None:
    """Forget every snapshot (tests; a new data folder)."""
    global _qt_accounts
    for b in BROKERS:
        _snaps.clear(b, "waiting")
    _qt_accounts = None
    _locks.clear()
    with _cache_lock:
        _beta.clear()
        _prev_mark.clear()


# ----------------------------------------------------------------------------- Questrade
def _questrade_source():
    if "questrade" not in chain_source.server_source_names():
        return None
    return chain_source.REGISTRY["questrade"]()


def _questrade_accounts(src):
    """The account reader of the Questrade source in use (its token manager and HTTP client)."""
    global _qt_accounts
    from ..sources.questrade.accounts import QuestradeAccounts
    if _qt_accounts is None or _qt_accounts.api.tokens is not src.tokens:
        _qt_accounts = QuestradeAccounts(src.tokens, src.api, src.api.http)
    return _qt_accounts


def _lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    if loop not in _locks:
        _locks[loop] = asyncio.Lock()
    return _locks[loop]


async def refresh_questrade(force: bool = False) -> None:
    """Fetch Questrade's accounts when they are due (see module doc). Never raises: the outcome is the state."""
    from ..sources.questrade.accounts import AccountAccessMissing
    from ..sources.questrade.auth import TokenError
    from ..sources.questrade.errors import QuestradeError
    if not wanted("questrade"):
        _snaps.clear("questrade")
        return
    src = _questrade_source()
    now = datetime.now(timezone.utc)
    if src is None:
        _snaps.failed("questrade", "not_connected", "Questrade is not enabled in this engine.", now, keep=False)
        return
    why = src.unavailable()
    if why is not None:
        state = "not_connected" if why == "not connected" else "error"
        detail = "Connect Questrade in Settings → Data sources." if why == "not connected" else f"Questrade: {why}"
        _snaps.failed("questrade", state, detail, now, keep=False)
        return
    async with _lock():
        cur = _snaps.get("questrade")
        age = (now - cur.attempted_at).total_seconds() if cur.attempted_at else None
        if not force and (cur.state == "needs_permission" or (age is not None and age < QUESTRADE_MAX_AGE_S)):
            return
        try:
            snap = await _questrade_accounts(src).snapshot()
        except AccountAccessMissing as e:
            _snaps.failed("questrade", "needs_permission", str(e), datetime.now(timezone.utc), keep=False)
            return
        except (QuestradeError, TokenError) as e:
            detail = getattr(e, "reason", None) or str(e)
            log.warning("portfolio: Questrade accounts failed: %s", detail)
            _snaps.failed("questrade", "error", f"Questrade: {detail}", datetime.now(timezone.utc))
            return
        _snaps.ok(snap)


def reset_questrade() -> None:
    """Questrade was disconnected: its positions are forgotten."""
    _snaps.clear("questrade", "not_connected")


def questrade_access() -> dict:
    """{state, detail} of Questrade account access (Settings → Data sources → Questrade), without a call:
    ok | needs_permission | error | not_connected | disabled | waiting (not asked yet)."""
    s = _snaps.get("questrade")
    return {"state": s.state if wanted("questrade") else "disabled", "detail": s.detail}


# ----------------------------------------------------------------------------- IBKR
def ingest_ibkr(body: Mapping[str, Any]) -> dict:
    """POST /api/ingest/positions: replace IBKR's snapshot (ignored while "Show my positions" is off for IBKR)."""
    if not wanted("ibkr"):
        _snaps.clear("ibkr")
        return {"stored": 0, "accounts": 0, "ignored": True}
    snap = ibkr_map.snapshot(body, datetime.now(timezone.utc))
    _snaps.ok(snap)
    return {"stored": len(snap.holdings), "accounts": len(snap.accounts), "ignored": False}


# ----------------------------------------------------------------------------- what is held
def _sources(now: datetime) -> list[dict]:
    out = []
    for b in BROKERS:
        s = _snaps.get(b)
        on = wanted(b)
        state = s.state if on else "disabled"
        if on and b == "ibkr" and s.snapshot is not None and (now - s.snapshot.fetched_at).total_seconds() > IBKR_STALE_S:
            state = "stale"
        if on and b == "ibkr" and s.snapshot is None and state == "waiting":
            detail = "Waiting for the IBKR bridge (IB Gateway / TWS connected, read-only API is enough)."
        else:
            detail = s.detail
        snap = s.snapshot if on else None
        out.append({"broker": b, "label": BROKER_LABEL[b], "enabled": on, "state": state, "detail": detail,
                    "fetched_at": snap.fetched_at if snap else None, "attempted_at": s.attempted_at if on else None,
                    "accounts": len(snap.accounts) if snap else 0, "positions": len(snap.holdings) if snap else 0})
    return out


def _held() -> tuple[list[Holding], list, list]:
    """(holdings, accounts, fx rates) of the enabled brokers' last snapshots."""
    holdings, accounts, fx = [], [], []
    for b in BROKERS:
        if not wanted(b):
            continue
        snap = _snaps.get(b).snapshot
        if snap is not None:
            holdings += snap.holdings
            accounts += snap.accounts
            fx += snap.fx
    return holdings, accounts, fx


def held_underlyings() -> list[str]:
    """Underlyings of the held stocks and options (enabled brokers' last snapshots, memory only), the largest position
    by cost first (live/background.py keeps them live)."""
    size: dict[str, float] = {}
    for h in _held()[0]:
        if h.kind in ("stock", "option") and h.underlying:
            size[h.underlying] = size.get(h.underlying, 0.0) + abs(h.qty * (h.avg_cost or 0.0) * h.multiplier)
    return sorted(size, key=lambda u: (-size[u], u))


def _is_listed_us(h: Holding) -> bool:
    return h.currency == "USD" and h.kind in ("stock", "option")


def live_keys() -> tuple[list[str], list[str]]:
    """(symbols, OSI codes) to keep live for the held USD stocks and options (plus SPY, the beta benchmark)."""
    hs = [h for h in _held()[0] if _is_listed_us(h)]
    symbols = sorted({h.underlying for h in hs} | {BENCHMARK})
    return symbols, sorted({h.osi for h in hs if h.osi})


# ----------------------------------------------------------------------------- marks
def _ts(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(v)) if v else None
    except ValueError:
        return None


def _fresh_live(f: Mapping | None, now: datetime) -> bool:
    ts = _ts(f.get("ts")) if f else None
    return ts is not None and (now - ts).total_seconds() <= LIVE_FRESH_S


def _num(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if np.isfinite(x) else None


def _pick(cands: Sequence[tuple[datetime | None, float | None, str, bool | None]]) -> tuple:
    """The freshest candidate with a price > 0: (price, source, ts, delayed)."""
    ok = [c for c in cands if c[1] is not None and c[1] > 0]
    if not ok:
        return None, None, None, None
    ts, price, source, delayed = max(ok, key=lambda c: c[0] or datetime.min.replace(tzinfo=timezone.utc))
    return price, source, ts, delayed


class _Market:
    """Marks of the held instruments from the live snapshot, the stored data and the brokers' prices (one view)."""

    def __init__(self, live_snap: Mapping, now: datetime, fetched: Mapping[str, datetime]):
        self.now, self.fetched = now, fetched
        self.lq: Mapping = live_snap.get("quotes") or {}
        self.lo: Mapping = live_snap.get("options") or {}
        self._chains: dict[str, Any] = {}
        self._quotes: dict[str, dict | None] = {}

    def chain(self, sym: str):
        if sym not in self._chains:
            try:
                self._chains[sym] = ctx.load_chain_arrays(sym, ensure_fresh=False)
            except Exception:  # noqa: BLE001 - an unknown symbol: no stored chain
                self._chains[sym] = None
        return self._chains[sym]

    def quote(self, sym: str) -> dict | None:
        if sym not in self._quotes:
            try:
                self._quotes[sym] = ctx.quote_dict(sym)
            except Exception:  # noqa: BLE001 - not a symbol OptionLens knows (e.g. a TSX listing)
                self._quotes[sym] = None
        return self._quotes[sym]

    def rq(self, sym: str) -> tuple[float, float]:
        ca = self.chain(sym)
        return (ca.r, ca.q) if ca is not None else (ctx.risk_free_rate(), 0.0)

    def stock(self, sym: str, broker: Holding | None = None) -> val.Mark:
        live = self.lq.get(sym)
        if _fresh_live(live, self.now) and _num(live.get("price")):
            return val.Mark(_num(live["price"]), "live", _ts(live["ts"]), _num(live.get("prev_close"))
                            or self._stored_prev(sym, broker), None, None, live.get("delayed"))
        q = self.quote(sym) if broker is None or broker.currency == "USD" else None
        cands = []
        if q is not None:
            cands.append((_ts(q.get("ts")), _num(q.get("price")), "stored", q.get("delayed")))
        if broker is not None and broker.broker_price is not None:
            cands.append((self.fetched.get(broker.broker), broker.broker_price, "broker", None))
        price, source, ts, delayed = _pick(cands)
        return val.Mark(price, source, ts, self._stored_prev(sym, broker), None, None, delayed)

    def _stored_prev(self, sym: str, broker: Holding | None) -> float | None:
        q = self.quote(sym) if broker is None or broker.currency == "USD" else None
        return _num(q.get("prev_close")) if q else (broker.prev_close if broker else None)

    def underlying_price(self, sym: str, holdings: Sequence[Holding]) -> float | None:
        stock = next((h for h in holdings if h.kind == "stock" and h.underlying == sym), None)
        m = self.stock(sym, stock)
        if m.price is not None:
            return m.price
        ca = self.chain(sym)
        return float(ca.spot) if ca is not None and ca.spot else None

    def option(self, h: Holding, spot: float | None) -> val.Mark:
        live = self.lo.get(h.osi or "")
        row = None
        ca = self.chain(h.underlying) if h.currency == "USD" else None
        if ca is not None and h.osi:
            hit = np.nonzero(ca.osi == h.osi)[0]
            row = int(hit[0]) if len(hit) else None
        if _fresh_live(live, self.now) and (lm := _live_mid(live)) is not None:
            price, source, ts, delayed = lm, "live", _ts(live["ts"]), live.get("delayed")
        else:
            cands = []
            if row is not None:
                cands.append((ca.asof, _num(ca.mid[row]), "stored", None))
            if h.broker_price is not None:
                cands.append((self.fetched.get(h.broker), h.broker_price, "broker", None))
            price, source, ts, delayed = _pick(cands)
        iv = _num(live.get("iv")) if live and _fresh_live(live, self.now) else None
        if iv is None and row is not None:
            iv = _num(ca.iv[row])
        if iv is None and price is not None and spot and h.expiry and close_dt(h.expiry) > self.now:
            r, q = self.rq(h.underlying)
            iv = bs.implied_vol(price, spot, h.strike, year_fraction_to_expiry(h.expiry, self.now), r, q, h.right)
        prev = h.prev_close if h.prev_close is not None else _prior_mark(h.osi, self.now)
        return val.Mark(price, source, ts, prev, spot, iv, delayed)


def _live_mid(f: Mapping) -> float | None:
    if (m := _num(f.get("mid"))) is not None and m > 0:
        return m
    b, a = _num(f.get("bid")), _num(f.get("ask"))
    if b is not None and a is not None and b > 0 and a > 0:
        return (b + a) / 2.0
    return _num(f.get("last"))


def _prior_mark(osi: str | None, now: datetime) -> float | None:
    """The contract's mid (else last) in the archive of the last session before today (cached per day)."""
    if not osi:
        return None
    today = now.astimezone(NY).date()
    key = (osi, today)
    with _cache_lock:
        if key in _prev_mark:
            return _prev_mark[key]
    value = None
    try:
        h = options_repo.eod_for_osi(osi)
        idx = [i for i, d in enumerate(h["date"]) if ctx.as_date(d) and ctx.as_date(d) < today]
        if idx:
            i = idx[-1]
            b, a, last = _num(h["bid"][i]), _num(h["ask"][i]), _num(h["last"][i])
            value = (b + a) / 2.0 if b and a and b > 0 and a > 0 else (last if last and last > 0 else None)
    except Exception:  # noqa: BLE001 - no archive: no previous mark
        value = None
    with _cache_lock:
        _prev_mark[key] = value
    return value


def _beta_of(sym: str) -> float | None:
    key = (sym, ctx.today())
    with _cache_lock:
        if key in _beta:
            return _beta[key]
    try:
        b = symbol_service.beta_1y(sym)
    except Exception:  # noqa: BLE001 - unknown symbol / no bars
        b = None
    with _cache_lock:
        _beta[key] = b
    return b


# ----------------------------------------------------------------------------- the view
def _currency(c: str) -> str:
    c = c.upper()
    if c not in CURRENCIES:
        raise BadRequest(f"currency must be one of {', '.join(CURRENCIES)}")
    return c


def _rows(holdings: Sequence[Holding], mk: _Market) -> list[dict]:
    spy = mk.stock(BENCHMARK).price
    spots: dict[str, float | None] = {}
    rows = []
    for h in holdings:
        r_, q_ = mk.rq(h.underlying) if _is_listed_us(h) else (ctx.risk_free_rate(), 0.0)
        if h.kind == "option":
            if h.underlying not in spots:
                spots[h.underlying] = mk.underlying_price(h.underlying, holdings) if h.currency == "USD" else None
            mark = mk.option(h, spots[h.underlying])
        elif h.kind == "stock":
            mark = mk.stock(h.underlying, h)
        else:
            mark = val.Mark(h.broker_price, "broker" if h.broker_price is not None else None,
                            mk.fetched.get(h.broker))
        beta = _beta_of(h.underlying) if _is_listed_us(h) else None
        row = val.position(h, mark, now=mk.now, r=r_, q=q_, beta=beta, spy_price=spy)
        row["id"] = f"{h.broker}:{h.account_id}:{h.symbol}"
        row["linkable"] = _is_listed_us(h)
        rows.append(row)
    return rows


def _sort_key(r: dict) -> tuple:
    return (r["kind"] != "stock", str(r["expiry"] or ""), r["strike"] or 0.0, r["right"] or "", r["account"])


def view(currency: str, live_snap: Mapping) -> dict:
    """GET /api/portfolio (types.ts Portfolio)."""
    cur = _currency(currency)
    now = datetime.now(timezone.utc)
    holdings, accounts, fx = _held()
    fetched = {b: s.snapshot.fetched_at for b in BROKERS if (s := _snaps.get(b)).snapshot is not None}
    mk = _Market(live_snap, now, fetched)
    rows = _rows(holdings, mk)
    by_und: dict[str, list[dict]] = {}
    for r in rows:
        by_und.setdefault(r["underlying"], []).append(r)
    underlyings = []
    for und in sorted(by_und):
        rs = sorted(by_und[und], key=_sort_key)
        currencies = sorted({r["currency"] for r in rs})
        underlyings.append({
            "underlying": und, "currencies": currencies,
            "spot": next((r["spot"] for r in rs if r["spot"] is not None), None),
            "linkable": all(r["linkable"] for r in rs),
            "risk": any(r["kind"] == "option" for r in rs) and all(r["linkable"] for r in rs),
            "totals": val.aggregate(rs, cur, fx, (*val.SUMMED, "delta", "gamma")),
            "bwd_delta_spy": val.beta_weighted(rs, None)["spy_shares"],
            "positions": rs,
        })
    per_account = []
    for a in accounts:
        rs = [r for r in rows if r["broker"] == a.broker and r["account_id"] == a.account_id]
        per_account.append({"broker": a.broker, "account_id": a.account_id, "account": a.account,
                            "balances": [{"currency": b.currency, "net_liquidation": b.net_liquidation, "cash": b.cash,
                                          "buying_power": b.buying_power} for b in a.balances],
                            "balance_totals": val.balances(a.balances, cur, fx),
                            "totals": val.aggregate(rs, cur, fx)})
    brokers = []
    for b in BROKERS:
        rs = [r for r in rows if r["broker"] == b]
        bal = [x for a in accounts if a.broker == b for x in a.balances]
        if rs or bal:
            brokers.append({"broker": b, "label": BROKER_LABEL[b], "totals": val.aggregate(rs, cur, fx),
                            "balance_totals": val.balances(bal, cur, fx)})
    spy = mk.stock(BENCHMARK).price
    symbols, contracts = live_keys()
    return {
        "currency": cur, "asof": now, "sources": _sources(now),
        "fx": [val.fx_view(x) for x in sorted(fx, key=lambda x: x.asof, reverse=True)],
        "totals": val.aggregate(rows, cur, fx),
        "balance_totals": val.balances([x for a in accounts for x in a.balances], cur, fx),
        "beta_weighted": val.beta_weighted(rows, spy),
        "brokers": brokers, "accounts": per_account, "underlyings": underlyings,
        "live": {"symbols": symbols, "contracts": contracts},
    }


def risk(underlying: str, live_snap: Mapping) -> dict:
    """GET /api/portfolio/risk?underlying=: the risk graph of everything held on `underlying` (portfolio/risk.py)."""
    und = underlying.strip().upper()
    holdings = [h for h in _held()[0] if h.underlying == und]
    if not holdings:
        raise NotFound(f"no position on {und}")
    if not all(_is_listed_us(h) for h in holdings):
        raise BadRequest(f"{und}: risk graphs cover US-listed stocks and options only")
    now = datetime.now(timezone.utc)
    fetched = {b: s.snapshot.fetched_at for b in BROKERS if (s := _snaps.get(b)).snapshot is not None}
    mk = _Market(live_snap, now, fetched)
    spot = mk.underlying_price(und, holdings)
    if spot is None:
        raise NotFound(f"no price for {und}")
    ivs = {h.osi: mk.option(h, spot).iv for h in holdings if h.kind == "option" and h.osi}
    r, q = mk.rq(und)
    ca = mk.chain(und)
    out = risk_graph.graph(holdings, ivs, spot, now_ny(), r, q, hv30=ca.hv30 if ca is not None else None)
    return {"underlying": und, "currency": "USD", "spot": spot,
            "positions": [{"symbol": h.symbol, "account": h.account, "broker": h.broker, "kind": h.kind, "qty": h.qty,
                           "avg_cost": h.avg_cost, "osi": h.osi, "expiry": h.expiry, "right": h.right,
                           "strike": h.strike, "multiplier": h.multiplier} for h in holdings], **out}

