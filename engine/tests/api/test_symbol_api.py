"""Per-symbol endpoints: values against the seeded fixtures, error mapping and on-demand paths."""
from __future__ import annotations

from datetime import date

import pytest

S = "/api/symbol/AAPL"


def get(client, url, status=200):
    r = client.get(url)
    assert r.status_code == status, r.text
    return r.json()


def test_summary_values(client):
    s = get(client, f"{S}/summary")
    assert s["symbol"] == "AAPL" and s["quote"]["price"] == pytest.approx(349.145, abs=5)
    assert 0.15 < s["iv30"] < 0.5 and 0.05 < s["hv20"] < 1 and s["iv_hv_ratio"] == pytest.approx(s["iv30"] / s["hv20"])
    assert s["iv_rank"] is None and s["iv_history_days"] == 1        # honest: < 20 stored days
    assert s["avg_option_volume_20d"] is None                        # no 20d option history yet
    assert s["next_earnings"]["date"] == "2027-01-28" and s["next_earnings"]["implied_move_pct"] > 0
    assert s["beta_1y"] == pytest.approx(1.0) and s["corr_spy_1y"] == pytest.approx(1.0)  # SPY seeded as 1.9×AAPL
    assert len(s["price_1y"]) == 252 and len(s["ideas"]) <= 3
    assert s["support"] == sorted(s["support"], reverse=True) and all(x < s["quote"]["price"] for x in s["support"])


def test_index_prefix_and_case_are_normalized(client):
    assert get(client, "/api/symbol/aapl/bars?range=1m")[-1]["date"] == "2026-11-19"   # live bar from the quote
    assert get(client, "/api/symbol/%5EAAPL/summary")["symbol"] == "AAPL"


def test_chain_and_expiry_validation(client):
    c = get(client, f"{S}/chain")
    assert c["expiry"] == "2026-11-20" and c["strikes"] == sorted(c["strikes"])
    assert [r["strike"] for r in c["calls"]] == sorted(r["strike"] for r in c["calls"])
    assert all(r["itm"] == (r["strike"] < c["underlying"]["price"]) for r in c["calls"])
    assert c["expiries"][0]["expiry"] == "2026-11-20"
    c2 = get(client, f"{S}/chain?expiry=2026-12-18")
    assert {r["expiry"] for r in c2["calls"] + c2["puts"]} == {"2026-12-18"}
    assert "not a listed expiry" in get(client, f"{S}/chain?expiry=2020-01-17", 422)["detail"]
    assert get(client, f"{S}/chain?expiry=garbage", 422)["detail"]


def test_activity_and_open_interest(client):
    a = get(client, f"{S}/activity?expiry=all")
    assert a["expiry"] is None
    assert sum(r["call_volume"] for r in a["by_strike"]) == a["totals"]["call_volume"]
    assert a["moneyness"]["call_otm"] + a["moneyness"]["call_atm"] + a["moneyness"]["call_itm"] == a["totals"]["call_volume"]
    assert len(a["top_contracts"]) == 20 and a["history"][-1]["call_volume"] == a["totals"]["call_volume"]
    oi = get(client, f"{S}/open-interest")
    assert oi["prior_date"] == "2026-11-18" and oi["expiry"] == "2026-11-20"
    assert all(r["call_oi_change"] == 0 for r in oi["by_strike"])      # prior EOD = same snapshot
    assert len(oi["max_pain"]) == len(oi["expiries"])


def test_contract_history(client):
    c = get(client, f"{S}/contract/AAPL261218C00350000")
    assert c["strike"] == 350 and c["right"] == "C" and c["expiry"] == "2026-12-18"
    assert c["latest"]["osi"] == "AAPL261218C00350000" and c["history"][0]["date"] == "2026-11-18"
    get(client, f"{S}/contract/AAPL261218C00351000", 404)
    get(client, f"{S}/contract/NOTANOSI", 422)


def test_volatility_skew_moves(client):
    v = get(client, f"{S}/volatility")
    cm = {r["tenor"]: r for r in v["constant_maturity"]}
    assert cm[30]["iv"] == pytest.approx(v["iv30"]) and cm[30]["chg_1d"] is None
    assert [h["window"] for h in v["hv"]] == [10, 20, 30, 60, 90, 252] and v["term_1w"] is None
    assert len(v["history"]) > 2000 and v["history"][-1]["iv30"] == pytest.approx(v["iv30"])
    sk = get(client, f"{S}/skew")
    assert sk["dte"] == 29 and [p["delta"] for p in sk["by_delta"]][:2] == [-0.05, -0.1]
    assert sk["by_delta"][-1]["delta"] == 0.05
    put25 = next(p["iv"] for p in sk["by_delta"] if p["delta"] == -0.25)
    call25 = next(p["iv"] for p in sk["by_delta"] if p["delta"] == 0.25)
    assert put25 > call25                                               # equity skew
    im = get(client, f"{S}/implied-moves")
    row = next(r for r in im["rows"] if r["expiry"] == "2027-02-19")
    assert not next(r for r in im["rows"] if r["expiry"] == "2027-01-15")["earnings_in_window"]
    assert row["earnings_in_window"] and row["iv_move"] == pytest.approx(im["spot"] * row["iv_move_pct"])


def test_earnings_moves_match_bars(client):
    e = get(client, f"{S}/earnings")
    assert len(e["events"]) == 3 and e["next"]["date"] == "2027-01-28"
    bars = {b["date"]: b for b in get(client, f"{S}/bars?range=1y")}
    ev = e["events"][0]                                                  # amc -> reaction next session
    react = bars[ev["reaction_date"]]
    assert ev["move_pct"] == pytest.approx(react["close"] / ev["pre_close"] - 1)
    assert ev["gap_pct"] == pytest.approx(react["open"] / ev["pre_close"] - 1)
    assert e["stats"]["count"] == 3 and e["next"]["implied_vs_hist_ratio"] == pytest.approx(
        e["next"]["implied_move_pct"] / e["stats"]["avg_abs_move"])


def test_earnings_strategies(client):
    r = get(client, f"{S}/earnings/strategies?lookback=3&entry=-2&exit=1")
    assert (r["lookback"], r["entry"], r["exit"]) == (3, -2, 1) and len(r["rows"]) == 30
    ls = next(x for x in r["rows"] if x["strategy"] == "long-straddle")
    assert ls["trades"] == 3 and ls["model_count"] == 3                  # no stored EOD then: model priced
    trades = get(client, f"{S}/earnings/strategies/long-straddle/trades?lookback=3&entry=-2&exit=1")
    assert len(trades) == 3 and all(t["pricing"] == "model" for t in trades)
    assert ls["avg_pnl"] == pytest.approx(sum(t["pnl"] for t in trades) / 3)
    get(client, f"{S}/earnings/strategies?lookback=99", 422)
    get(client, f"{S}/earnings/strategies/nope/trades", 404)


def test_stock_tabs(client):
    d = get(client, f"{S}/dividends")
    assert d["frequency"] == 4 and d["history"][0]["ex_date"] >= d["history"][-1]["ex_date"]
    se = get(client, f"{S}/seasonality")
    assert len(se["by_month"]) == 12 and se["current_month"] == date.today().month
    pa = get(client, f"{S}/price-action?range=3m")
    assert pa["rows"][0]["date"] > pa["rows"][-1]["date"] and pa["intraday_overnight"]["series"]
    ev = get(client, f"{S}/events")
    kinds = {i["kind"] for i in ev["items"]}
    assert kinds == {"earnings", "ex_dividend", "expiry"}
    vs = get(client, f"{S}/vol-seasonality")
    assert len(vs["months"]) == 12 and sum(m["iv_samples"] for m in vs["months"]) == 1
    get(client, f"{S}/bars?range=2y", 422)


def test_straddle_and_ideas(client):
    st = get(client, f"{S}/straddle")
    r = st["rows"][0]
    assert r["straddle"] == pytest.approx(r["call_mid"] + r["put_mid"]) and r["upper_be"] == pytest.approx(r["strike"] + r["straddle"])
    bt = st["backtest"]
    assert bt["summary"]["count"] == len(bt["trades"]) > 10 and bt["summary"]["model_count"] == bt["summary"]["count"]
    ideas = get(client, f"{S}/ideas")
    assert ideas["by_edge"] and all(i["edge"] is None or i["max_loss"] > 0 for i in ideas["by_edge"])


def test_company_tabs(client):
    f = get(client, f"{S}/fundamentals?period=annual")
    assert f["income"]["title"] == "Income statement" and f["fetched_at"]
    # FCF FY2026 = 145,097,000k − 9,041,000k; payback = market cap / FCF
    assert f["payback_years"] == pytest.approx(5.07e12 / ((145_097_000 - 9_041_000) * 1000))
    assert {"label": "P/E", "value": None} not in f["key_stats"] and f["key_stats"][0] == {"label": "Exchange", "value": "NASDAQ-GS"}
    get(client, f"{S}/fundamentals?period=weekly", 422)
    ins = get(client, f"{S}/insiders")
    t = ins["trades"][0]
    assert t["value"] == pytest.approx(t["shares"] * t["price"]) and ins["summary"][1]["m12"] == "45"
    assert get(client, f"{S}/news")[0]["source"] == "Barchart"
    p = get(client, f"{S}/profile")
    assert p["website"] == "https://www.apple.com" and p["exchange"] == "NASDAQ-GS" and [x["symbol"] for x in p["peers"]] == ["DELL"]
    peers = get(client, f"{S}/peers")
    assert [r["symbol"] for r in peers["rows"]] == ["AAPL", "DELL"] and peers["rows"][0]["iv30"] > 0


def test_errors(client):
    assert get(client, "/api/symbol/ZZZZZ/summary", 404) == {"detail": "unknown symbol ZZZZZ"}
    assert get(client, "/api/symbol/A$B/summary", 422)["detail"].startswith("malformed symbol")
    assert get(client, "/api/symbol/SPY/chain", 404)["detail"] == "no option chain for SPY"
    assert get(client, "/api/symbol/SPY/volatility")["term_today"] == []     # realized vol without options


def test_bars_carry_full_history_smas_on_short_ranges(client):
    """A 1-month range still has the 50- and 200-day averages (computed over all stored bars, not the range)."""
    import numpy as np
    rows = client.get("/api/symbol/AAPL/bars?range=1m").json()
    assert 15 <= len(rows) <= 25
    closes = np.array([r["close"] for r in client.get("/api/symbol/AAPL/bars?range=max").json()])
    last = rows[-1]
    assert last["sma200"] == pytest.approx(float(np.mean(closes[-200:])))
    assert last["sma50"] == pytest.approx(float(np.mean(closes[-50:])))
    assert all(r["sma20"] is not None for r in rows)
