"""User data (SPEC §9): watchlists (+ the quick ReportResult table), report presets, alerts and
notifications. Validation lives here; persistence in repo.user."""
from __future__ import annotations

from typing import Any, Iterable

from ..models import normalize_symbol
from ..repo import symbols as symbols_repo
from ..repo import user as user_repo
from . import context
from .errors import BadRequest, NotFound
from .reports import columns as C
from .reports import universe
from .reports.base import Col, in_market, jsonable, pick

# ============================================================================ watchlists
def _symbols(raw: Iterable[str]) -> list[str]:
    out = []
    for s in raw:
        sym = normalize_symbol(str(s))
        if not sym:
            continue
        if symbols_repo.get_symbol(sym) is None:
            context.ensure_known(sym)          # on-demand: creates the row or raises UnknownSymbol (404)
        out.append(sym)
    return list(dict.fromkeys(out))


def _name(name: Any) -> str:
    n = str(name or "").strip()
    if not n:
        raise BadRequest("name required")
    if len(n) > 100:
        raise BadRequest("name too long")
    return n


def list_watchlists() -> list[dict]:
    return user_repo.list_watchlists()


def get_watchlist(wid: int) -> dict:
    w = user_repo.get_watchlist(wid)
    if w is None:
        raise NotFound(f"watchlist {wid} not found")
    return w


def _unique_name(name: str, wid: int | None = None) -> None:
    if any(w["name"] == name and w["id"] != wid for w in user_repo.list_watchlists()):
        raise BadRequest(f"watchlist {name!r} already exists")


def create_watchlist(name: Any, symbols: Iterable[str] | None = None) -> dict:
    n = _name(name)
    _unique_name(n)
    return user_repo.create_watchlist(n, _symbols(symbols or []))


def update_watchlist(wid: int, name: Any = None, symbols: Iterable[str] | None = None) -> dict:
    get_watchlist(wid)
    n = None
    if name is not None:
        n = _name(name)
        _unique_name(n, wid)
    w = user_repo.update_watchlist(wid, n, None if symbols is None else _symbols(symbols))
    if w is None:
        raise NotFound(f"watchlist {wid} not found")
    return w


def delete_watchlist(wid: int) -> dict:
    if not user_repo.delete_watchlist(wid):
        raise NotFound(f"watchlist {wid} not found")
    return {"ok": True}


def add_symbol(wid: int, symbol: str) -> dict:
    get_watchlist(wid)
    user_repo.add_symbol(wid, _symbols([symbol])[0])
    return get_watchlist(wid)


def remove_symbol(wid: int, symbol: str) -> dict:
    get_watchlist(wid)
    user_repo.remove_symbol(wid, normalize_symbol(symbol))
    return get_watchlist(wid)


WATCHLIST_COLS = (
    *pick(C.DESC, "symbol", "name"), *C.QUOTE[:2], C.HALTED, *pick(C.QUOTE, "volume"), *pick(C.VWAP, "pct_vs_vwap"),
    *pick(C.PERF, "ret_5d", "ret_1m"), *pick(C.VOL_CORE, "iv30", "iv_rank", "hv20", "iv_hv_ratio"),
    *pick(C.OPT, "option_volume", "pc_volume_ratio"), *pick(C.CATALYSTS, "next_earnings", "days_to_earnings", "next_ex_div"),
    Col("market_cap", "Market Cap", "bigmoney", C.G_DESC, default_hidden=True),
)


def watchlist_table(wid: int) -> dict:
    """ReportResult of the watchlist's symbols (quotes, IV, earnings) in list order."""
    w = get_watchlist(wid)
    f = universe.get_frame()
    if any(s not in set(f["symbol"]) for s in w["symbols"]):
        universe.invalidate()           # symbols added since the last build
        f = universe.get_frame()
    pos = f.index()
    keys = [c.key for c in WATCHLIST_COLS]
    rows = []
    for s in w["symbols"]:
        i = pos.get(s)
        rows.append(f.rows([i], keys)[0] if i is not None else {"symbol": s, **{k: None for k in keys if k != "symbol"}})
    return {"id": f"watchlist-{wid}", "title": w["name"], "asof": jsonable(f.asof), "params": {"watchlist": wid},
            "columns": [c.as_dict() for c in WATCHLIST_COLS], "rows": jsonable(in_market(rows, None)),
            "truncated": False,
            "notes": [C.NOTE_IV_RANK, C.NOTE_DELAY]}


# ============================================================================ presets
def list_presets(report_id: str | None = None) -> list[dict]:
    return user_repo.list_presets(report_id)


def create_preset(report_id: Any, name: Any, payload: Any) -> dict:
    from .reports import registry        # local: registry imports every report family
    rid = str(report_id or "")
    if rid not in registry.REPORTS:
        raise BadRequest(f"unknown report {rid!r}")
    if not isinstance(payload, dict) or not isinstance(payload.get("params", {}), dict) \
            or not isinstance(payload.get("filters", {}), dict):
        raise BadRequest("payload must be {params: {}, filters: {}, sort?, hidden?}")
    clean = {"params": payload.get("params") or {}, "filters": payload.get("filters") or {},
             "sort": payload.get("sort"), "hidden": payload.get("hidden") or []}
    return user_repo.save_preset(rid, _name(name), clean)


def delete_preset(pid: int) -> dict:
    if not user_repo.delete_preset(pid):
        raise NotFound(f"preset {pid} not found")
    return {"ok": True}


# ============================================================================ alerts
ALERT_KINDS = ("price_above", "price_below", "pct_change_abs", "iv30_above", "iv30_below", "iv_rank_above",
               "iv_rank_below", "rel_option_volume_above", "unusual_volume", "earnings_within_days",
               "ex_div_within_days", "halted", "resumed")
VALUELESS = frozenset({"unusual_volume", "halted", "resumed"})
DEFAULT_COOLDOWN = 240


def _alert_fields(kind: Any, symbol: Any, params: Any, enabled: Any, cooldown_min: Any, partial: dict | None = None) -> dict:
    """Validated alert fields; `partial` = existing alert for PUT (None fields keep its values; the caller
    resolves `symbol`)."""
    cur = partial or {}
    kind = cur.get("kind") if kind is None else kind
    if kind not in ALERT_KINDS:
        raise BadRequest(f"unknown alert kind {kind!r}")
    params = cur.get("params", {}) if params is None else params
    if not isinstance(params, dict):
        raise BadRequest("params must be an object")
    params = {k: v for k, v in params.items() if k in ("value", "watchlist_id") and v is not None}
    sym = _symbols([symbol])[0] if symbol else None
    if "watchlist_id" in params:
        try:
            params["watchlist_id"] = int(params["watchlist_id"])
        except (TypeError, ValueError):
            raise BadRequest("watchlist_id must be an integer") from None
        if user_repo.get_watchlist(params["watchlist_id"]) is None:
            raise BadRequest(f"unknown watchlist {params['watchlist_id']}")
    if not sym and "watchlist_id" not in params:
        raise BadRequest("alert needs a symbol or params.watchlist_id")
    if kind not in VALUELESS:
        try:
            params["value"] = float(params["value"])
        except (KeyError, TypeError, ValueError):
            raise BadRequest(f"{kind} needs a numeric params.value") from None
    cd = cur.get("cooldown_min", DEFAULT_COOLDOWN) if cooldown_min is None else cooldown_min
    try:
        cd = int(cd)
    except (TypeError, ValueError):
        raise BadRequest("cooldown_min must be an integer") from None
    if cd < 0:
        raise BadRequest("cooldown_min must be >= 0")
    en = cur.get("enabled", True) if enabled is None else bool(enabled)
    return {"kind": kind, "symbol": sym, "params": params, "enabled": en, "cooldown_min": cd}


def list_alerts() -> list[dict]:
    return user_repo.list_alerts()


def create_alert(kind: Any, symbol: Any = None, params: Any = None, enabled: Any = True,
                 cooldown_min: Any = DEFAULT_COOLDOWN) -> dict:
    return user_repo.create_alert(**_alert_fields(kind, symbol, params or {}, enabled, cooldown_min))


def update_alert(aid: int, fields: dict) -> dict:
    cur = user_repo.get_alert(aid)
    if cur is None:
        raise NotFound(f"alert {aid} not found")
    sym = fields["symbol"] if "symbol" in fields else cur["symbol"]
    upd = _alert_fields(fields.get("kind"), sym, fields.get("params"), fields.get("enabled"),
                        fields.get("cooldown_min"), partial=cur)
    return user_repo.update_alert(aid, **upd)


def delete_alert(aid: int) -> dict:
    if not user_repo.delete_alert(aid):
        raise NotFound(f"alert {aid} not found")
    return {"ok": True}


# ============================================================================ notifications
def list_notifications(unread_only: bool = False) -> list[dict]:
    return user_repo.list_notifications(unread_only)


def mark_read(ids: Iterable[int] | None = None) -> dict:
    n = user_repo.mark_read(None if ids is None else [int(i) for i in ids])
    return {"ok": True, "updated": int(n), "unread": int(user_repo.unread_count())}
