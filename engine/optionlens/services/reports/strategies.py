"""Strategy screeners (SPEC §7 "Strategy screeners"): one ReportDef per analytics strategy screener
id, sharing params and columns; rows come from analytics.strategies.screen_universe over the
universe's ChainArrays (pre-filtered in SQL by right, DTE window and liquidity)."""
from __future__ import annotations

from dataclasses import dataclass


from ...analytics import strategies as st
from .. import context
from . import columns as C
from .base import Col, Output, Param, ReportDef, RunParams, group
from .common import KIND, SCOPE, base_mask, frame, p_int, p_num, p_select

# ---------------------------------------------------------------------------- columns
G_TRADE, G_RISK, G_PROB, G_GREEKS, G_VOL, G_LIQ = "Trade", "Risk / reward", "Probability", "Greeks", "Volatility", "Liquidity"
BASE_COLS = (
    Col("symbol", "Symbol", "symbol"),
    *group(G_TRADE, (
        Col("legs", "Legs", "text"),
        Col("expiry", "Expiry", "date"),
        Col("dte", "DTE", "int"),
        Col("spot", "Stock price", "money"),
        Col("price", "Net price", "money", description="Per share: debit (+) / credit (−) at the chosen pricing"),
        Col("cost", "Cost / credit ($)", "money", description="Per strategy unit (1 contract per leg)"),
        Col("pricing", "Pricing", "badge", default_hidden=True),
    )),
    *group(G_RISK, (
        Col("max_profit", "Max profit", "money", description="Blank = unlimited"),
        Col("max_loss", "Max loss", "money", description="Blank = unlimited"),
        Col("breakeven_low", "Breakeven low", "money"),
        Col("breakeven_high", "Breakeven high", "money", default_hidden=True),
        Col("return_on_risk", "Return on risk", "pct", description="Max profit / max loss"),
        Col("annualized_return", "Annualized", "pct", description="Return on risk · 365 / DTE (simple)"),
    )),
    *group(G_PROB, (
        Col("pop", "PoP", "pct", description="Probability of profit at expiry (lognormal, ATM IV, drift r − q)"),
        Col("prob_max_profit", "Prob max profit", "pct", default_hidden=True),
        Col("ev_iv", "EV (IV)", "money", description="Expected P/L at expiry under the IV distribution"),
        Col("ev_hv", "EV (HV)", "money", description="Expected P/L at expiry under HV30, zero drift"),
    )),
    *group(G_GREEKS, (
        Col("delta", "Net delta", "num2", description="Share-equivalents"),
        Col("theta", "Theta ($/day)", "money"),
        Col("vega", "Vega ($/vol pt)", "money", default_hidden=True),
    )),
    *group(G_VOL, (
        Col("iv", "IV", "vol", description="Mean leg IV"),
        Col("atm_iv", "ATM IV", "vol", default_hidden=True),
        Col("iv_rank", "IV Rank", "pct"),
        Col("hv30", "HV30", "vol", default_hidden=True),
    )),
    *group("Catalysts", (
        Col("earnings_before_expiry", "Earnings before expiry", "bool"),
        Col("next_earnings", "Next earnings", "date", default_hidden=True),
    )),
    *group(G_LIQ, (
        Col("min_leg_oi", "Min leg OI", "int"),
        Col("min_leg_volume", "Min leg volume", "int", default_hidden=True),
        Col("max_leg_spread_pct", "Max leg spread %", "pct"),
    )),
)
BACK_EXPIRY = Col("back_expiry", "Back expiry", "date", G_TRADE)
EXTRA_COLS = {
    "covered-calls": group("Covered call", (
        Col("static_return", "Static return", "pct", description="bid / (S − bid)"),
        Col("static_annualized", "Static annualized", "pct"),
        Col("if_called_return", "If-called return", "pct", description="(K − S + bid) / (S − bid)"),
        Col("if_called_annualized", "If-called annualized", "pct"),
        Col("downside_protection", "Downside protection", "pct", description="bid / S"),
        Col("prob_called", "Prob called", "pct"))),
    "naked-puts": group("Short put", (
        Col("return_on_cash", "Return on cash", "pct", description="bid / (K − bid)"),
        Col("return_on_cash_annualized", "Return on cash annualized", "pct"),
        Col("margin_req", "Margin req.", "money", description="Reg-T naked put estimate per contract"),
        Col("return_on_margin", "Return on margin", "pct"),
        Col("return_on_margin_annualized", "Return on margin annualized", "pct"),
        Col("prob_otm", "Prob OTM", "pct"))),
    "calendar": group("Calendar", (
        Col("front_iv", "Front IV", "vol"), Col("back_iv", "Back IV", "vol"),
        Col("iv_ratio", "IV ratio front/back", "ratio"), Col("forward_vol", "Forward vol", "vol"),
        Col("theta_ratio", "Theta ratio", "ratio"),
        Col("est_max_profit", "Est. max profit", "money",
            description="BS value of the back leg at front expiry (back IV, spot = strike) − debit"))),
    "synthetic-long": group("Synthetic", (
        Col("synthetic_price_natural", "Synthetic price (natural)", "money"),
        Col("synthetic_price_mid", "Synthetic price (mid)", "money"),
        Col("parity_gap_natural", "Parity gap (natural)", "money", description="(S·e^−qT − K·e^−rT) − (C − P)"),
        Col("parity_gap_mid", "Parity gap (mid)", "money"))),
}

# ---------------------------------------------------------------------------- params
SHARED = (
    p_int("dte_min", "Min DTE", 7, 0), p_int("dte_max", "Max DTE", 60, 0, 900),
    p_int("min_oi", "Min OI per leg", 100, 0, help="Also applied in the database before screening"),
    p_int("min_volume", "Min volume per leg", 0, 0),
    p_num("max_spread_pct", "Max spread % per leg", 0.25, 0, step=0.05, help="(ask − bid) / mid"),
    p_select("pricing", "Pricing", "natural", (("natural", "Natural (bid/ask)"), ("mid", "Mid"))),
    p_select("earnings_filter", "Earnings before expiry", "any",
             (("any", "Any"), ("exclude", "Exclude"), ("include", "Only with earnings"))),
    KIND, *SCOPE,
)


def _delta(lo: float | None, hi: float | None, what: str) -> tuple[Param, Param]:
    return (p_num("delta_min", f"Min |delta| ({what})", lo, 0, 1, step=0.05),
            p_num("delta_max", f"Max |delta| ({what})", hi, 0, 1, step=0.05))


WIDTH = (p_int("max_width_steps", "Max width (strikes)", 5, 1, 20, help="Listed strikes between legs"),
         p_num("width_min_pct", "Min width % of spot", None, 0, 1, step=0.01),
         p_num("width_max_pct", "Max width % of spot", 0.10, 0, 1, step=0.01))
CAL_GAP = (p_int("back_gap_min", "Min back − front DTE", 20, 1), p_int("back_gap_max", "Max back − front DTE", 70, 1))
SIDE = (p_select("side", "Side", "long", (("long", "Long (buy)"), ("short", "Short (sell)"))),)


@dataclass(frozen=True)
class Spec:
    id: str
    title: str
    description: str
    params: tuple[Param, ...]
    sort: str
    right: str | None          # SQL pre-filter
    descending: bool = True


SPECS = (
    Spec("covered-calls", "Covered calls", "Buy 100 shares and sell one call: static and if-called returns, "
         "downside protection and probability of assignment.", _delta(0.15, 0.50, "call"), "static_annualized", "C"),
    Spec("naked-puts", "Naked / cash-secured puts", "Sell one put: return on cash and on Reg-T margin, "
         "breakeven and probability OTM.", _delta(0.10, 0.35, "put"), "return_on_cash_annualized", "P"),
    Spec("long-calls", "Long calls", "Buy one call.", _delta(0.30, 0.70, "call"), "ev_hv", "C"),
    Spec("long-puts", "Long puts", "Buy one put.", _delta(0.30, 0.70, "put"), "ev_hv", "P"),
    Spec("bull-call-spreads", "Bull call spreads (debit)", "Buy a call, sell a higher call, same expiry.",
         (*_delta(0.35, 0.70, "long call"), *WIDTH), "ev_hv", "C"),
    Spec("bear-put-spreads", "Bear put spreads (debit)", "Buy a put, sell a lower put, same expiry.",
         (*_delta(0.35, 0.70, "long put"), *WIDTH), "ev_hv", "P"),
    Spec("bull-put-spreads", "Bull put spreads (credit)", "Sell a put, buy a lower put, same expiry.",
         (*_delta(0.15, 0.40, "short put"), *WIDTH), "return_on_risk", "P"),
    Spec("bear-call-spreads", "Bear call spreads (credit)", "Sell a call, buy a higher call, same expiry.",
         (*_delta(0.15, 0.40, "short call"), *WIDTH), "return_on_risk", "C"),
    Spec("iron-condors-short", "Short iron condors", "Sell an OTM put spread and an OTM call spread "
         "(short inner strikes, long outer).", (*_delta(0.10, 0.30, "short strikes"), *WIDTH), "return_on_risk", None),
    Spec("iron-condors-long", "Long iron condors", "Buy an OTM put spread and an OTM call spread "
         "(long inner strikes, short outer).", (*_delta(0.10, 0.35, "inner strikes"), *WIDTH), "ev_hv", None),
    Spec("call-butterflies-long", "Long call butterflies", "+1/−2/+1 calls with equal wings.",
         (*_delta(0.30, 0.70, "body"), *WIDTH), "ev_hv", "C"),
    Spec("call-butterflies-short", "Short call butterflies", "−1/+2/−1 calls with equal wings.",
         (*_delta(0.30, 0.70, "body"), *WIDTH), "ev_hv", "C"),
    Spec("put-butterflies-long", "Long put butterflies", "+1/−2/+1 puts with equal wings.",
         (*_delta(0.30, 0.70, "body"), *WIDTH), "ev_hv", "P"),
    Spec("put-butterflies-short", "Short put butterflies", "−1/+2/−1 puts with equal wings.",
         (*_delta(0.30, 0.70, "body"), *WIDTH), "ev_hv", "P"),
    Spec("iron-butterflies-short", "Short iron butterflies", "Sell the ATM straddle and buy wings.",
         (*_delta(0.40, 0.60, "body call"), *WIDTH), "return_on_risk", None),
    Spec("iron-butterflies-long", "Long iron butterflies", "Buy the ATM straddle and sell wings.",
         (*_delta(0.40, 0.60, "body call"), *WIDTH), "ev_hv", None),
    Spec("calendar-calls", "Call calendars", "Sell a front call, buy a later call at the same strike.",
         (*_delta(0.35, 0.65, "front call"), *CAL_GAP), "ev_hv", "C"),
    Spec("calendar-puts", "Put calendars", "Sell a front put, buy a later put at the same strike.",
         (*_delta(0.35, 0.65, "front put"), *CAL_GAP), "ev_hv", "P"),
    Spec("atm-straddles", "ATM straddles", "Call and put at the strike nearest spot, per expiry (long or short).",
         SIDE, "ev_hv", None),
    Spec("strangles", "Strangles", "OTM call and put with matching |delta| (long or short).",
         (*SIDE, *_delta(0.15, 0.35, "legs")), "ev_hv", None),
    Spec("synthetic-long", "Synthetic long stock", "Buy a call and sell a put at the same strike and expiry; "
         "parity gap shows the discount/premium vs stock after carry.", _delta(0.35, 0.65, "call"),
         "parity_gap_mid", None),
)
assert {s.id for s in SPECS} == set(st.SCREENER_IDS)

NOTES = ("Probability of profit (PoP) and expected value (EV) come from a standard price model, not from past "
         "trades.",
         "Natural pricing buys at the ask and sells at the bid; mid pricing uses the midpoint of each leg.",
         "For index options, set Symbols to SPX,NDX,RUT,XSP.",
         C.NOTE_IV_RANK,
         C.NOTE_CHAIN_UNIVERSE)
METHOD = ("PoP and EV use a lognormal model with the expiry's ATM IV and drift r − q; EV (HV) uses HV30 and zero "
          "drift. EV is the undiscounted expected P/L at expiry.",
          "Mid pricing: (bid + ask) / 2 per leg.")


def _screen_params(spec: Spec, p: RunParams) -> st.ScreenParams:
    kw = dict(dte_min=p["dte_min"], dte_max=p["dte_max"], min_oi=p["min_oi"], min_volume=p["min_volume"],
              max_spread_pct=p["max_spread_pct"], pricing=p["pricing"], earnings_filter=p["earnings_filter"])
    for k in ("delta_min", "delta_max", "max_width_steps", "width_min_pct", "width_max_pct", "back_gap_min",
              "back_gap_max", "side"):
        if k in p and p[k] is not None:
            kw[k] = p[k]
    return st.ScreenParams(**kw)


def _runner(spec: Spec):
    def run(p: RunParams) -> Output:
        sp = _screen_params(spec, p)
        dte_max = sp.dte_max + (sp.back_gap_max if spec.id.startswith("calendar") else 0)
        scope = p.scope
        if scope is None and p.get("kind") not in (None, "all"):
            f = frame()
            scope = list(f["symbol"][base_mask(f, p)])
        chains = context.load_universe_chain_arrays(scope, spec.right, sp.dte_min, dte_max, sp.min_oi, sp.min_volume)
        rows, truncated = st.screen_universe(spec.id, chains, sp, spec.sort, spec.descending)
        for r in rows:
            r.pop("strikes", None)
        notes = [] if chains else ["No stored chains match the filters."]
        asof = max((c.asof for c in chains), default=None)
        return Output(rows, notes, asof=asof, truncated=truncated, presorted=True)
    return run


def _columns(spec: Spec) -> tuple[Col, ...]:
    cols = list(BASE_COLS)
    if spec.id.startswith("calendar"):
        cols.insert(3, BACK_EXPIRY)
        cols += EXTRA_COLS["calendar"]
    cols += EXTRA_COLS.get(spec.id, ())
    return tuple(cols)


REPORTS = tuple(
    ReportDef(s.id, s.title, "Strategies", s.description, (*SHARED, *s.params), _columns(s), _runner(s),
              (s.sort, "desc" if s.descending else "asc"), NOTES, METHOD)
    for s in SPECS)
