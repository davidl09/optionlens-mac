import math
from datetime import date, datetime

import pytest

from optionlens.analytics import chainmetrics as cm
from optionlens.analytics.calendar import NY
from optionlens.analytics.chain import ChainArrays
from optionlens.models import ChainSnapshot, Quote

from .helpers import contract, load_aapl_chain

NOW = datetime(2026, 1, 5, 12, 0, tzinfo=NY)


@pytest.fixture(scope="module")
def aapl():
    snap, raw = load_aapl_chain()
    return snap, raw, cm.compute_chain_metrics(snap, snap.fetched_at)


def test_keys_exact(aapl):
    _, _, m = aapl
    assert set(m) == set(cm.METRIC_KEYS)
    assert len(cm.METRIC_KEYS) == 31


def test_iv30_close_to_cboe(aapl):
    _, raw, m = aapl
    assert m["iv30_cboe"] == pytest.approx(raw["data"]["iv30"] / 100)
    assert abs(m["iv30"] - 0.25007) < 0.02
    for k in ("iv60", "iv90", "iv120", "iv180", "iv360", "atm_iv_front"):
        assert 0.1 < m[k] < 0.6
    assert m["expected_move30_pct"] == pytest.approx(m["iv30"] * math.sqrt(30 / 365))


def test_volume_oi_totals_equal_fixture_sums(aapl):
    _, raw, m = aapl
    opts = raw["data"]["options"]
    side = lambda o: o["option"][-9]  # noqa: E731
    assert m["call_volume"] == sum(int(o["volume"]) for o in opts if side(o) == "C")
    assert m["put_volume"] == sum(int(o["volume"]) for o in opts if side(o) == "P")
    assert m["call_oi"] == sum(int(o["open_interest"]) for o in opts if side(o) == "C")
    assert m["put_oi"] == sum(int(o["open_interest"]) for o in opts if side(o) == "P")
    nd = sum(o["volume"] * o["delta"] * 100 for o in opts)
    assert m["net_delta_volume"] == pytest.approx(nd)


def test_moneyness_buckets_partition_totals(aapl):
    _, _, m = aapl
    for s in ("call", "put"):
        assert sum(m[f"{s}_vol_{b}"] for b in ("otm", "atm", "itm")) == m[f"{s}_volume"]
        assert sum(m[f"{s}_prem_{b}"] for b in ("otm", "atm", "itm")) == pytest.approx(m[f"{s}_premium"])


def test_premium_uses_spec_mid(aapl):
    snap, raw, m = aapl
    tot = 0.0
    for c in snap.contracts:
        if c.right == "C" and c.mid is not None:
            tot += c.volume * c.mid * 100
    assert m["call_premium"] == pytest.approx(tot)


def test_straddle30_from_expiry_nearest_30dte(aapl):
    snap, _, m = aapl
    ca = ChainArrays.from_snapshot(snap)
    rows = cm.straddle_rows(ca)
    best = min(rows, key=lambda r: abs(r["dte"] - 30))
    spot = snap.quote.price
    assert best["expiry"] == date(2026, 12, 18)
    ks = sorted({c.strike for c in snap.contracts if c.expiry == best["expiry"]})
    k = min(ks, key=lambda x: abs(x - spot))
    call = next(c for c in snap.contracts if c.expiry == best["expiry"] and c.strike == k and c.right == "C")
    put = next(c for c in snap.contracts if c.expiry == best["expiry"] and c.strike == k and c.right == "P")
    assert m["straddle30_pct"] == pytest.approx((call.mid + put.mid) / spot)
    assert 0.03 < m["straddle30_pct"] < 0.10


def test_expiry_infos_and_term(aapl):
    snap, _, _ = aapl
    ca = ChainArrays.from_snapshot(snap)
    infos = cm.expiry_infos(ca)
    assert len(infos) == 24
    assert sum(i["call_volume"] for i in infos) == int(ca.volume[ca.is_call].sum())
    monthly = {i["expiry"] for i in infos if i["is_monthly"]}
    assert date(2026, 11, 20) in monthly and date(2026, 12, 18) in monthly and date(2026, 11, 27) not in monthly
    assert infos[0]["dte"] == 1
    assert len(cm.term_structure(ca)) == 24


def test_missing_price_gives_nulls():
    snap, _ = load_aapl_chain()
    q = Quote(**{**{f: getattr(snap.quote, f) for f in snap.quote.__dataclass_fields__}, "price": None})
    m = cm.compute_chain_metrics(ChainSnapshot(q, snap.contracts, snap.fetched_at), snap.fetched_at)
    assert m["iv30"] is None and m["call_volume"] is None and m["iv30_cboe"] == snap.quote.iv30


def _small_chain():
    e1, e2 = date(2026, 1, 16), date(2026, 2, 20)
    rows = [
        contract(e1, 90, "C", 10.0, 10.4, iv=0.3, delta=0.9, volume=5, oi=100),
        contract(e1, 90, "P", 0.2, 0.3, iv=0.32, delta=-0.1, volume=50, oi=300),
        contract(e1, 100, "C", 2.0, 2.2, iv=0.25, delta=0.5, volume=100, oi=500),
        contract(e1, 100, "P", 1.9, 2.1, iv=0.27, delta=-0.5, volume=80, oi=400),
        contract(e1, 110, "C", 0.3, 0.4, iv=0.24, delta=0.1, volume=40, oi=600),
        contract(e1, 110, "P", 10.0, 10.5, iv=0.3, delta=-0.9, volume=0, oi=10),
        contract(e2, 100, "C", 4.0, 4.4, iv=0.26, delta=0.52, volume=7, oi=70),
        contract(e2, 100, "P", 3.8, 4.2, iv=0.28, delta=-0.48, volume=3, oi=30, last=4.0),
    ]
    return ChainArrays.from_contracts(rows, 101.0, NOW, symbol="TEST")


def test_small_chain_buckets_and_premium():
    ca = _small_chain()
    m = cm.metrics_from_arrays(ca)
    # spot 101: 100 is ATM (|100/101−1| = 0.99%), 90 below band, 110 above band
    assert m["call_vol_itm"] == 5 and m["call_vol_atm"] == 107 and m["call_vol_otm"] == 40
    assert m["put_vol_otm"] == 50 and m["put_vol_atm"] == 83 and m["put_vol_itm"] == 0
    assert m["call_prem_atm"] == pytest.approx(100 * 2.1 * 100 + 7 * 4.2 * 100)
    assert m["put_premium"] == pytest.approx(50 * 0.25 * 100 + 80 * 2.0 * 100 + 3 * 4.0 * 100)


def test_max_pain_hand_example():
    ca = _small_chain()
    e1 = date(2026, 1, 16)
    # payout(X) = Σ call OI·max(X−K,0) + Σ put OI·max(K−X,0)
    def pay(x):
        calls = {90: 100, 100: 500, 110: 600}
        puts = {90: 300, 100: 400, 110: 10}
        return sum(o * max(x - k, 0) for k, o in calls.items()) + sum(o * max(k - x, 0) for k, o in puts.items())
    best = min((90, 100, 110), key=pay)
    assert cm.max_pain(ca, e1) == best
    assert [r["expiry"] for r in cm.max_pain_all(ca)] == list(ca.expiries)


def test_by_strike_with_prior_oi_and_oi_changes():
    ca = _small_chain()
    e1 = date(2026, 1, 16)
    osi = {(c.strike, c.right): c.osi for c in ca.src if c.expiry == e1}
    prior = {osi[(100.0, "C")]: 450, osi[(100.0, "P")]: 420, osi[(90.0, "P")]: 300}
    rows = cm.by_strike(ca, e1, prior)
    r100 = next(r for r in rows if r["strike"] == 100)
    assert r100["call_oi_change"] == 50 and r100["put_oi_change"] == -20
    r110 = next(r for r in rows if r["strike"] == 110)
    assert r110["call_oi_change"] == 600  # absent from prior -> new
    assert cm.by_strike(ca, e1)[0]["call_oi_change"] is None
    all_rows = cm.by_strike(ca)
    assert next(r for r in all_rows if r["strike"] == 100)["call_oi"] == 570
    ch = cm.oi_changes(ca, prior, expiry=e1)
    assert ch["top_increases"][0]["oi_change"] == 600 and ch["top_decreases"][0]["oi_change"] == -20
    assert ch["top_decreases"][0]["oi_prev"] == 420


def test_top_contracts_and_implied_moves():
    ca = _small_chain()
    top = cm.top_contracts(ca, 3)
    assert [t["volume"] for t in top] == [100, 80, 50]
    assert top[0]["vol_oi"] == pytest.approx(100 / 500) and top[0]["premium"] == pytest.approx(100 * 2.1 * 100)
    assert top[0]["itm"] is True and top[1]["itm"] is False and top[0]["mid"] == pytest.approx(2.1)
    rows = cm.implied_moves(ca, earnings_date=date(2026, 1, 28))
    assert rows[0]["straddle"] == pytest.approx(2.1 + 2.0)
    assert rows[0]["straddle_pct"] == pytest.approx(4.1 / 101)
    assert rows[0]["earnings_in_window"] is False and rows[1]["earnings_in_window"] is True
    iv = rows[0]["iv"]
    assert rows[0]["iv_move"] == pytest.approx(101 * iv * math.sqrt(ca.t[0]))
    pts = cm.smile(ca, date(2026, 1, 16))
    assert [p["strike"] for p in pts] == [90, 100, 110] and pts[1]["put_delta"] == -0.5


def test_expiry_term_rows_feed_earnings_implied_move(aapl):
    from optionlens.analytics import earnings
    snap, _, _ = aapl
    ca = ChainArrays.from_snapshot(snap)
    rows = cm.expiry_term_rows(ca)
    assert set(rows[0]) == {"expiry", "dte", "t", "atm_iv", "straddle"}
    out = earnings.implied_earnings_move(rows, date(2026, 11, 24), snap.quote.price)
    assert out["expiry_used"] == date(2026, 11, 25)
    r = next(x for x in rows if x["expiry"] == date(2026, 11, 25))
    assert out["implied_move_pct"] == pytest.approx(r["straddle"] / snap.quote.price)
    assert out["excess_implied_move_pct"] is not None and 0 <= out["excess_implied_move_pct"] < 0.1
