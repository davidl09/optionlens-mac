"""Per-snapshot option-chain aggregates (SPEC §4 moneyness buckets, §6 definitions).

Premium = Σ volume·mid·100. Moneyness: ATM if |K/S − 1| ≤ 2.5%; calls OTM above, ITM below;
puts mirror. Volume/OI totals count every contract in the snapshot.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Mapping

import numpy as np

from . import vol
from .calendar import is_monthly_expiry
from .chain import ChainArrays, option_row

ATM_BAND = 0.025
METRIC_KEYS = (
    "iv30", "iv60", "iv90", "iv120", "iv180", "iv360", "iv30_cboe", "atm_iv_front",
    "skew25_30", "put_skew_30", "call_volume", "put_volume", "call_oi", "put_oi",
    "call_premium", "put_premium", "call_vol_otm", "call_vol_atm", "call_vol_itm",
    "put_vol_otm", "put_vol_atm", "put_vol_itm", "call_prem_otm", "call_prem_atm",
    "call_prem_itm", "put_prem_otm", "put_prem_atm", "put_prem_itm", "net_delta_volume",
    "straddle30_pct", "expected_move30_pct", "iv_computed",
)


def moneyness_masks(ca: ChainArrays) -> dict[str, np.ndarray]:
    """{otm, atm, itm} boolean masks per row (right-aware)."""
    m = ca.strike / ca.spot - 1.0
    atm = np.abs(m) <= ATM_BAND
    above, below = (m > ATM_BAND), (m < -ATM_BAND)
    c = ca.is_call
    return {"atm": atm, "otm": np.where(c, above, below), "itm": np.where(c, below, above)}


def _premium(ca: ChainArrays) -> np.ndarray:
    return ca.volume * np.nan_to_num(ca.mid) * 100.0


def flow_totals(ca: ChainArrays) -> dict:
    """Volume, OI, premium totals and moneyness breakdown (volume and premium)."""
    c, p = ca.is_call, ~ca.is_call
    prem = _premium(ca)
    mm = moneyness_masks(ca)
    out = {
        "call_volume": int(ca.volume[c].sum()), "put_volume": int(ca.volume[p].sum()),
        "call_oi": int(ca.oi[c].sum()), "put_oi": int(ca.oi[p].sum()),
        "call_premium": float(prem[c].sum()), "put_premium": float(prem[p].sum()),
    }
    for side, sm in (("call", c), ("put", p)):
        for b in ("otm", "atm", "itm"):
            sel = sm & mm[b]
            out[f"{side}_vol_{b}"] = int(ca.volume[sel].sum())
            out[f"{side}_prem_{b}"] = float(prem[sel].sum())
    known = np.isfinite(ca.delta)            # contracts without a delta are left out; none known -> null, never 0
    out["net_delta_volume"] = float(np.sum(ca.volume[known] * ca.delta[known] * 100.0)) if known.any() else None
    return out


def _nearest_strike_straddle(ca: ChainArrays, sl: slice) -> tuple[float | None, float | None, float | None]:
    """(strike, call_mid, put_mid) at the strike nearest spot that has both a call and a put."""
    k, c, mid = ca.strike[sl], ca.is_call[sl], ca.mid[sl]
    calls = {float(kk): float(m) for kk, m in zip(k[c], mid[c])}
    puts = {float(kk): float(m) for kk, m in zip(k[~c], mid[~c])}
    common = sorted(set(calls) & set(puts))
    if not common:
        return None, None, None
    ks = np.array(common)
    strike = float(ks[np.argmin(np.abs(ks - ca.spot))])
    cm, pm = calls[strike], puts[strike]
    return strike, (cm if math.isfinite(cm) else None), (pm if math.isfinite(pm) else None)


def straddle_rows(ca: ChainArrays) -> list[dict]:
    """StraddleInfo.rows: per expiry ATM (nearest-strike) straddle and breakevens."""
    rows = []
    for i, sl in enumerate(ca.expiry_slices()):
        k, cm, pm = _nearest_strike_straddle(ca, sl)
        st = cm + pm if cm is not None and pm is not None else None
        rows.append({
            "expiry": ca.expiries[i], "dte": int(ca.dte[sl][0]), "strike": k,
            "call_mid": cm, "put_mid": pm, "straddle": st,
            "straddle_pct": st / ca.spot if st is not None else None,
            "lower_be": k - st if st is not None else None,
            "upper_be": k + st if st is not None else None,
        })
    return rows


def expiry_infos(ca: ChainArrays) -> list[dict]:
    """ExpiryInfo per expiry (atm_iv per SPEC §6, straddle at strike nearest spot)."""
    atm = vol.atm_iv_by_expiry(ca)
    strad = straddle_rows(ca)
    out = []
    for i, sl in enumerate(ca.expiry_slices()):
        c = ca.is_call[sl]
        v, oi = ca.volume[sl], ca.oi[sl]
        out.append({
            "expiry": ca.expiries[i], "dte": int(ca.dte[sl][0]),
            "is_monthly": is_monthly_expiry(ca.expiries[i]), "atm_iv": atm[i]["atm_iv"],
            "atm_iv_computed": atm[i]["atm_iv_computed"],
            "straddle": strad[i]["straddle"],
            "call_volume": int(v[c].sum()), "put_volume": int(v[~c].sum()),
            "call_oi": int(oi[c].sum()), "put_oi": int(oi[~c].sum()),
        })
    return out


def expiry_term_rows(ca: ChainArrays) -> list[dict]:
    """Per expiry {expiry, dte, t, atm_iv, straddle} — the input of
    earnings.implied_earnings_move and of term-structure/implied-move consumers."""
    atm = vol.atm_iv_by_expiry(ca)
    return [{**a, "straddle": s["straddle"]} for a, s in zip(atm, straddle_rows(ca))]


def _nearest_dte_index(dtes: list[int], target: int) -> int | None:
    if not dtes:
        return None
    return int(np.argmin([abs(d - target) for d in dtes]))  # ties -> earlier expiry


def compute_chain_metrics(chain, now: datetime) -> dict:
    """metrics_daily option columns for one ChainSnapshot (keys = METRIC_KEYS)."""
    if not chain.quote.price or chain.quote.price <= 0:
        return {k: (chain.quote.iv30 if k == "iv30_cboe" else None) for k in METRIC_KEYS}
    ca = ChainArrays.from_snapshot(chain, now)
    return metrics_from_arrays(ca, iv30_cboe=chain.quote.iv30)


def iv_metric_layer(term: list[dict]) -> tuple[list[dict], bool | None]:
    """The per-expiry ATM IVs a chain-wide IV metric (IV30…IV360 interpolation, front ATM IV, 25-delta skew) is
    computed from, and whether they are engine-computed (None: no IV at all). One layer for the whole metric:
    the expiries with a source ATM IV when there is at least one (DTE >= 1), else the computed ones — a term
    structure never interpolates a source IV against a computed one."""
    usable = [r for r in term if r["atm_iv"] is not None and r["dte"] >= 1]
    if not usable:
        return term, None
    computed = all(r["atm_iv_computed"] for r in usable)
    return [r if r["atm_iv_computed"] == computed else {**r, "atm_iv": None} for r in term], computed


def metrics_from_arrays(ca: ChainArrays, iv30_cboe: float | None = None) -> dict:
    out: dict = {k: None for k in METRIC_KEYS}
    out["iv30_cboe"] = iv30_cboe
    if len(ca) == 0 or not ca.spot or ca.spot <= 0:
        return out
    term, computed = iv_metric_layer(vol.atm_iv_by_expiry(ca))
    out["iv_computed"] = computed
    cm = vol.constant_maturity(term)
    for d, v in cm.items():
        out[f"iv{d}"] = v
    front = next((r for r in term if r["dte"] >= 1 and r["atm_iv"] is not None), None)
    out["atm_iv_front"] = front["atm_iv"] if front else None
    sk = vol.skew_30(ca.iv_layer(bool(computed)), cm[30])
    out["skew25_30"], out["put_skew_30"] = sk["skew25_30"], sk["put_skew_30"]
    out.update(flow_totals(ca))
    strad = straddle_rows(ca)
    j = _nearest_dte_index([r["dte"] for r in strad], 30)
    out["straddle30_pct"] = strad[j]["straddle_pct"] if j is not None else None
    out["expected_move30_pct"] = cm[30] * math.sqrt(30 / 365) if cm[30] is not None else None
    return out


# ----------------------------------------------------------------------------- per-strike / OI
def by_strike(ca: ChainArrays, expiry: date | None = None,
              prior_oi: Mapping[str, int] | None = None) -> list[dict]:
    """StrikeAgg rows (ascending strike) for one expiry, or all expiries aggregated when expiry is
    None. OI change = oi − prior_oi[osi] (contracts absent from prior count as 0); None when
    prior_oi is None."""
    sel = np.ones(len(ca), bool) if expiry is None else (ca.exp_idx == ca.expiries.index(expiry))
    ks, inv = np.unique(ca.strike[sel], return_inverse=True)
    c = ca.is_call[sel]
    n = len(ks)

    def agg(vals, mask):
        return np.bincount(inv[mask], weights=vals[mask], minlength=n)

    v, oi = ca.volume[sel], ca.oi[sel]
    chg = None
    if prior_oi is not None:
        prev = np.array([float(prior_oi.get(o, 0)) for o in ca.osi[sel]])
        chg = oi - prev
    rows = []
    cv, pv, co, po = agg(v, c), agg(v, ~c), agg(oi, c), agg(oi, ~c)
    cc = agg(chg, c) if chg is not None else None
    pc = agg(chg, ~c) if chg is not None else None
    for i, k in enumerate(ks):
        rows.append({"strike": float(k), "call_volume": int(cv[i]), "put_volume": int(pv[i]),
                     "call_oi": int(co[i]), "put_oi": int(po[i]),
                     "call_oi_change": int(cc[i]) if cc is not None else None,
                     "put_oi_change": int(pc[i]) if pc is not None else None})
    return rows


def max_pain(ca: ChainArrays, expiry: date) -> float | None:
    """Strike minimizing Σ OI·intrinsic paid to holders (calls + puts) at that expiry."""
    sel = ca.exp_idx == ca.expiries.index(expiry)
    k, oi, c = ca.strike[sel], ca.oi[sel], ca.is_call[sel]
    if oi.sum() <= 0:
        return None
    x = np.unique(k)[:, None]
    pay = np.where(c, np.maximum(x - k, 0.0), np.maximum(k - x, 0.0)) @ oi
    return float(x[np.argmin(pay), 0])


def max_pain_all(ca: ChainArrays) -> list[dict]:
    return [{"expiry": e, "strike": max_pain(ca, e)} for e in ca.expiries]


def top_contracts(ca: ChainArrays, n: int = 20) -> list[dict]:
    """Top n by volume: OptionRow + vol_oi + premium."""
    order = np.argsort(-ca.volume, kind="stable")[:n]
    prem = _premium(ca)
    out = []
    for i in order:
        if ca.volume[i] <= 0:
            break
        row = option_row(ca.src[i], ca.spot)
        row["vol_oi"] = float(ca.volume[i] / ca.oi[i]) if ca.oi[i] > 0 else None
        row["premium"] = float(prem[i])
        out.append(row)
    return out


def oi_changes(ca: ChainArrays, prior_oi: Mapping[str, int], n: int = 20,
               expiry: date | None = None) -> dict:
    """{top_increases, top_decreases}: OptionRow + oi_prev + oi_change (contracts absent from the
    prior day count as oi_prev 0)."""
    sel = np.arange(len(ca)) if expiry is None else np.nonzero(ca.exp_idx == ca.expiries.index(expiry))[0]
    prev = np.array([float(prior_oi.get(ca.osi[i], 0)) for i in sel])
    chg = ca.oi[sel] - prev

    def rows(order):
        out = []
        for j in order:
            r = option_row(ca.src[sel[j]], ca.spot)
            r["oi_prev"], r["oi_change"] = int(prev[j]), int(chg[j])
            out.append(r)
        return out

    inc = [j for j in np.argsort(-chg, kind="stable") if chg[j] > 0][:n]
    dec = [j for j in np.argsort(chg, kind="stable") if chg[j] < 0][:n]
    return {"top_increases": rows(inc), "top_decreases": rows(dec)}


def implied_moves(ca: ChainArrays, earnings_date: date | None = None) -> list[dict]:
    """ImpliedMoves.rows. iv = ATM IV; iv_move = spot·iv·√t with t the BSM year fraction (equals
    √(dte/365) at the close); low/high = spot ∓ iv_move (straddle when IV is unknown);
    earnings_in_window = as-of date ≤ earnings_date ≤ expiry."""
    atm = vol.atm_iv_by_expiry(ca)
    strad = straddle_rows(ca)
    asof_day = ca.asof_date
    out = []
    for a, s in zip(atm, strad):
        iv = a["atm_iv"]
        move = ca.spot * iv * math.sqrt(a["t"]) if iv is not None else None
        band = move if move is not None else s["straddle"]
        out.append({
            "expiry": a["expiry"], "dte": a["dte"], "straddle": s["straddle"],
            "straddle_pct": s["straddle_pct"], "iv": iv, "iv_move": move,
            "iv_move_pct": move / ca.spot if move is not None else None,
            "low": ca.spot - band if band is not None else None,
            "high": ca.spot + band if band is not None else None,
            "earnings_in_window": bool(earnings_date is not None and asof_day <= earnings_date <= a["expiry"]),
        })
    return out


def term_structure(ca: ChainArrays) -> list[dict]:
    """TermPoint rows (all expiries, including DTE 0)."""
    return [{"expiry": r["expiry"], "dte": r["dte"], "atm_iv": r["atm_iv"], "atm_iv_computed": r["atm_iv_computed"]}
            for r in vol.atm_iv_by_expiry(ca)]


def smile(ca: ChainArrays, expiry: date) -> list[dict]:
    """Skew.points for one expiry: per strike call/put IV and delta, moneyness = K/S − 1."""
    sl = ca.expiry_slices()[ca.expiries.index(expiry)]
    pts: dict[float, dict] = {}
    for k, c, iv, d in zip(ca.strike[sl], ca.is_call[sl], ca.iv[sl], ca.delta[sl]):
        p = pts.setdefault(float(k), {"strike": float(k), "moneyness": float(k / ca.spot - 1.0),
                                      "call_iv": None, "put_iv": None, "call_delta": None, "put_delta": None})
        side = "call" if c else "put"
        p[f"{side}_iv"] = float(iv) if np.isfinite(iv) else None
        p[f"{side}_delta"] = float(d) if np.isfinite(d) else None
    return [pts[k] for k in sorted(pts)]
