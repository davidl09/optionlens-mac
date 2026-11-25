"""IV surface: implied volatility per expiry (columns) × row bucket (delta, moneyness or strike) from one chain,
interpolated from reliable contracts only (docs/INTEGRATION.md "IV surface"). Pure.

A contract is reliable when it has an IV of the expiry's IV layer (vol.atm_iv_layered: the source's IVs, the
engine-computed ones only for an expiry the source gives none), a bid > 0, an ask >= bid, a spread of at most
MAX_SPREAD_OF_MID of its mid and open interest >= MIN_OI. Unreliable contracts never feed a reliable cell. A cell
that reliable contracts cannot give (not bracketed by them) falls back to all contracts with an IV and is marked
unreliable with the reason. Nothing is extrapolated: a target outside the quoted range has no IV.

Rows (ascending strike, i.e. bottom to top of the heatmap):
- delta: put deltas −0.10…−0.45, ATM (the expiry's ATM IV, vol.atm_iv_layered), call deltas 0.45…0.10 — the
  OTM side's feed delta, IV interpolated linearly in delta (vol.iv_by_delta, as the Skew page);
- moneyness: K/S − 1 from −20% to +20% in 2.5% steps, IV interpolated linearly in log-moneyness ln(K/S) over the
  OTM contracts (puts below spot, calls at/above);
- strike: listed strikes (the union over the columns within ±STRIKE_BAND of spot, the MAX_STRIKES nearest), the OTM
  contract's IV at that strike (no interpolation; null where the expiry does not list it).
"""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import date
from typing import Literal, Sequence, get_args

import numpy as np

from . import vol
from .chain import ChainArrays

RowKind = Literal["delta", "moneyness", "strike"]
Mode = Literal["iv", "vs_atm", "chg_1d"]
ROW_KINDS: tuple[str, ...] = get_args(RowKind)
MODES: tuple[str, ...] = get_args(Mode)
ATM_DELTA = 0.50
PUT_DELTAS = (-0.10, -0.15, -0.20, -0.25, -0.30, -0.35, -0.40, -0.45)
CALL_DELTAS = (0.45, 0.40, 0.35, 0.30, 0.25, 0.20, 0.15, 0.10)
DELTA_ROWS = (*PUT_DELTAS, ATM_DELTA, *CALL_DELTAS)
MONEYNESS_ROWS = tuple(round(-0.20 + 0.025 * i, 4) for i in range(17))
MIN_DTE, MAX_DTE = 1, 366          # columns: expiries from tomorrow to about a year out
STRIKE_BAND = 0.20                 # strike rows: within ±20% of spot ...
MAX_STRIKES = 41                   # ... at most the 41 nearest spot
MAX_SPREAD_OF_MID = 0.40           # (ask − bid) / mid above this: the mid's IV is mostly quote noise
MIN_OI = 1                         # no open interest: a quote nobody trades against

NOT_BRACKETED = "not bracketed by reliable contracts"
OUT_OF_RANGE = "outside the quoted range"
NOT_LISTED = "strike not listed for this expiry"


def thresholds() -> dict:
    return {"max_spread_of_mid": MAX_SPREAD_OF_MID, "min_oi": MIN_OI, "min_dte": MIN_DTE, "max_dte": MAX_DTE}


def contract_reasons(ca: ChainArrays) -> np.ndarray:
    """Per row: None when the contract's quote is reliable, else why not (first failing check). IV layers aside."""
    out = np.full(len(ca), None, dtype=object)
    mid = (ca.bid + ca.ask) / 2.0
    with np.errstate(divide="ignore", invalid="ignore"):
        spread = np.where(mid > 0, (ca.ask - ca.bid) / mid, np.inf)
    for i in range(len(ca)):
        if not np.isfinite(ca.iv[i]):
            out[i] = "no IV"
        elif not ca.bid[i] > 0:
            out[i] = "no bid"
        elif not ca.ask[i] >= ca.bid[i]:
            out[i] = "no ask" if not ca.ask[i] > 0 else "crossed quote"
        elif spread[i] > MAX_SPREAD_OF_MID:
            out[i] = f"spread {spread[i]:.0%} of mid (> {MAX_SPREAD_OF_MID:.0%})"
        elif ca.oi[i] < MIN_OI:
            out[i] = "no open interest"
    return out


def _is_ok(reasons: np.ndarray) -> np.ndarray:
    return np.array([r is None for r in reasons], dtype=bool)


def _reliable(ca: ChainArrays) -> tuple[np.ndarray, np.ndarray, np.ndarray, ChainArrays]:
    """(reasons per row (None = reliable), ATM IV per expiry from reliable contracts, bool per expiry: that layer is
    engine-computed, the chain with only reliable IVs)."""
    reasons = contract_reasons(ca)
    atm, computed = vol.atm_iv_layered(replace(ca, iv=np.where(_is_ok(reasons), ca.iv, np.nan)))
    flag = ca.iv_computed if ca.iv_computed is not None else np.zeros(len(ca), dtype=bool)
    reasons[_is_ok(reasons) & (flag != computed[ca.exp_idx])] = "engine-computed IV (this expiry has source IVs)"
    return reasons, atm, computed, replace(ca, iv=np.where(_is_ok(reasons), ca.iv, np.nan))


def columns(ca: ChainArrays) -> list[int]:
    """Expiry indices shown as columns: MIN_DTE <= DTE <= MAX_DTE."""
    return [i for i, d in enumerate(ca.exp_dte) if MIN_DTE <= d <= MAX_DTE]


def surface_strikes(ca: ChainArrays, cols: Sequence[int]) -> list[float]:
    """Strike rows: the strikes listed by the columns within ±STRIKE_BAND of spot, the MAX_STRIKES nearest, ascending."""
    sel = np.isin(ca.exp_idx, list(cols)) & (np.abs(ca.strike / ca.spot - 1.0) <= STRIKE_BAND)
    ks = np.unique(ca.strike[sel])
    near = ks[np.argsort(np.abs(ks - ca.spot), kind="stable")[:MAX_STRIKES]]
    return [float(k) for k in np.sort(near)]


def _num(x) -> float | None:
    return float(x) if x is not None and np.isfinite(x) else None


def _cell(ca: ChainArrays, j: int | None, iv: float | None, reliable: bool, reason: str | None) -> dict:
    """A cell: its IV and the nearest real contract (row j of the chain)."""
    c = {"iv": iv, "value": iv, "osi": None, "strike": None, "right": None, "bid": None, "ask": None, "oi": None,
         "reliable": reliable, "reason": None if reliable else reason}
    if j is not None:
        c.update(osi=ca.osi[j], strike=float(ca.strike[j]), right="C" if ca.is_call[j] else "P",
                 bid=_num(ca.bid[j]), ask=_num(ca.ask[j]), oi=int(ca.oi[j]))
    return c


def _nearest(coord: np.ndarray, target: float, mask: np.ndarray) -> int | None:
    """Row index with coord nearest target within mask (None when empty)."""
    idx = np.nonzero(mask & np.isfinite(coord))[0]
    return int(idx[np.argmin(np.abs(coord[idx] - target))]) if len(idx) else None


def _resolve(ca: ChainArrays, coord: np.ndarray, target: float, side: np.ndarray, ok: np.ndarray, has_iv: np.ndarray,
             iv_ok: float | None, iv_any: float | None, reasons: np.ndarray) -> dict:
    """The cell from its IV off reliable contracts (iv_ok) or, failing that, off all contracts with an IV (iv_any),
    with the nearest contract among the inputs used."""
    if iv_ok is not None:
        return _cell(ca, _nearest(coord, target, side & ok), iv_ok, True, None)
    if iv_any is None:
        return _cell(ca, _nearest(coord, target, side), None, False, OUT_OF_RANGE)
    j = _nearest(coord, target, side & has_iv)
    return _cell(ca, j, iv_any, False, reasons[j] if j is not None and reasons[j] is not None else NOT_BRACKETED)


def _expiry_cells(ca: ChainArrays, i: int, kind: str, rows: Sequence[float], reasons: np.ndarray,
                  atm_ok: float | None, atm_any: float | None, used: ChainArrays) -> list[dict]:
    sel = ca.exp_idx == i
    ok, has_iv = _is_ok(reasons), np.isfinite(ca.iv)
    otm = sel & np.where(ca.is_call, ca.strike >= ca.spot, ca.strike < ca.spot)
    out = []
    if kind == "delta":
        side = [d for d in rows if d != ATM_DELTA]
        good = dict(zip(side, (r["iv"] for r in vol.iv_by_delta(used, i, side))))
        rough = dict(zip(side, (r["iv"] for r in vol.iv_by_delta(ca, i, side))))
        for d in rows:
            if d == ATM_DELTA:
                out.append(_resolve(ca, ca.strike, ca.spot, otm, ok, has_iv, atm_ok, atm_any, reasons))
            else:
                right = sel & (ca.is_call if d > 0 else ~ca.is_call)
                out.append(_resolve(ca, ca.delta, d, right, ok, has_iv, good[d], rough[d], reasons))
    elif kind == "moneyness":
        x = np.log(ca.strike / ca.spot)
        for m in rows:
            t = math.log(1.0 + m)   # as ln(K/S): a bucket on a listed strike hits it exactly
            out.append(_resolve(ca, x, t, otm, ok, has_iv, vol.interp_bracketed(x[otm & ok], ca.iv[otm & ok], t),
                                vol.interp_bracketed(x[otm & has_iv], ca.iv[otm & has_iv], t), reasons))
    else:
        for k in rows:
            hit = np.nonzero(otm & (ca.strike == k))[0]
            if not len(hit):
                out.append(_cell(ca, None, None, False, NOT_LISTED))
                continue
            j = int(hit[0])
            iv = _num(ca.iv[j])
            out.append(_cell(ca, j, iv, iv is not None and reasons[j] is None, reasons[j]))
    return out


def build(ca: ChainArrays, kind: str, *, strikes: Sequence[float] | None = None,
          expiries: Sequence[date] | None = None) -> dict:
    """{rows, expiries, cells} of one chain: rows = the row values (signed delta with ATM = 0.5, moneyness K/S − 1, or
    strike); expiries = [{expiry, dte, atm_iv, iv_computed, earnings_expiry}]; cells[row][column] (value = IV).
    ``strikes`` / ``expiries`` fix the rows / columns (e.g. a prior day's chain matched to today's surface)."""
    if kind not in ROW_KINDS:
        raise ValueError(f"rows must be one of {ROW_KINDS}")
    cols = columns(ca) if expiries is None else [ca.expiries.index(e) for e in expiries if e in ca.expiries]
    rows = list(DELTA_ROWS if kind == "delta" else MONEYNESS_ROWS if kind == "moneyness"
                else strikes if strikes is not None else surface_strikes(ca, cols))
    reasons, atm, computed, used = _reliable(ca)
    atm_any = vol.atm_iv_layered(ca)[0]
    earn = ca.earnings_before_expiry()
    first_earn = next((i for i in cols if earn[i]), None)
    by_col = [_expiry_cells(ca, i, kind, rows, reasons, _num(atm[i]), _num(atm_any[i]), used) for i in cols]
    return {
        "rows": rows,
        "expiries": [{"expiry": ca.expiries[i], "dte": int(ca.exp_dte[i]), "atm_iv": _num(atm[i]),
                      "iv_computed": bool(computed[i]), "earnings_expiry": i == first_earn} for i in cols],
        "cells": [[by_col[c][r] for c in range(len(cols))] for r in range(len(rows))],
    }


def with_mode(surf: dict, mode: str, prior: dict | None = None) -> list[list[dict]]:
    """The cells with `value` per mode: iv; vs_atm = IV − the column's ATM IV (vol points); chg_1d = IV − the same
    cell's IV in ``prior`` (a surface of the previous session's chain, same rows; null without it, a cell missing
    there or without an IV on either day). A chg_1d cell is reliable only when both days' cells are."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    atm = [e["atm_iv"] for e in surf["expiries"]]
    prev_col = {}
    if prior is not None:
        prev_col = {e["expiry"]: c for c, e in enumerate(prior["expiries"])}
    out = []
    for r, row in enumerate(surf["cells"]):
        cells = []
        for c, cell in enumerate(row):
            iv, new = cell["iv"], dict(cell)
            if mode == "vs_atm":
                new["value"] = iv - atm[c] if iv is not None and atm[c] is not None else None
            elif mode == "chg_1d":
                pc = prev_col.get(surf["expiries"][c]["expiry"])
                was = prior["cells"][r][pc] if pc is not None else None
                new["value"] = iv - was["iv"] if iv is not None and was is not None and was["iv"] is not None else None
                if new["reliable"] and was is not None and was["iv"] is not None and not was["reliable"]:
                    new["reliable"], new["reason"] = False, f"prior day: {was['reason']}"
            cells.append(new)
        out.append(cells)
    return out
