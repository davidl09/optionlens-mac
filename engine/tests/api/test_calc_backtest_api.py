"""Calculator, strategy templates, backtests, system endpoints and the SPA fallback."""
from __future__ import annotations

import pytest

EXP = "2026-12-18"


def post(client, url, body, status=200):
    r = client.post(url, json=body)
    assert r.status_code == status, r.text
    return r.json()


def _chain_row(client, strike, right="calls"):
    c = client.get(f"/api/symbol/AAPL/chain?expiry={EXP}").json()
    return c, next(r for r in c[right] if r["strike"] == strike)


def test_long_call_max_loss_and_breakeven(client):
    _, row = _chain_row(client, 350)
    r = post(client, "/api/calc/strategy",
             {"symbol": "AAPL", "legs": [{"kind": "option", "right": "C", "strike": 350, "expiry": EXP, "qty": 1}]})
    prem = row["mid"]
    assert r["legs"][0]["price"] == pytest.approx(prem) and r["legs"][0]["iv"] == pytest.approx(row["iv"])
    assert r["cost"] == pytest.approx(prem * 100)
    assert r["max_loss"] == pytest.approx(prem * 100) and r["max_profit"] is None
    assert r["breakevens"] == [pytest.approx(350 + prem, abs=0.01)] and r["expiry_used"] == EXP
    assert 0 < r["pop"] < 0.5 and r["greeks"]["delta"] == pytest.approx(row["delta"] * 100, rel=0.15)
    assert len(r["prices"]) == 121 and len(r["table"]) == 15


def test_covered_call_max_profit(client):
    c, row = _chain_row(client, 360)
    spot = c["underlying"]["price"]
    r = post(client, "/api/calc/strategy", {"symbol": "AAPL", "legs": [
        {"kind": "stock", "qty": 100}, {"kind": "option", "right": "C", "strike": 360, "expiry": EXP, "qty": -1}]})
    assert r["max_profit"] == pytest.approx((360 - spot + row["mid"]) * 100, rel=1e-6)
    assert r["max_loss"] == pytest.approx((spot - row["mid"]) * 100, rel=1e-3)


def test_calc_overrides_and_validation(client):
    r = post(client, "/api/calc/strategy", {"symbol": "AAPL", "iv_shift": 0.05, "points": 51, "price_range": [300, 400],
                                            "legs": [{"kind": "option", "right": "P", "strike": 340, "expiry": EXP,
                                                      "qty": 2, "price": 5.0, "iv": 0.3}]})
    assert r["cost"] == pytest.approx(1000) and r["prices"][0] == 300 and len(r["prices"]) == 51
    assert "not a listed contract" in post(client, "/api/calc/strategy", {"symbol": "AAPL", "legs": [
        {"kind": "option", "right": "C", "strike": 351.5, "expiry": EXP, "qty": 1}]}, 422)["detail"]
    assert post(client, "/api/calc/strategy", {"symbol": "AAPL", "legs": []}, 422)["detail"]
    post(client, "/api/calc/strategy", {"symbol": "AAPL", "legs": [{"kind": "stock", "qty": 0}]}, 422)


def test_strategy_templates(client):
    t = client.get("/api/strategy-templates").json()
    assert len(t) == 29 and {"long-call", "iron-condor-short", "synthetic-long"} <= {x["id"] for x in t}


def test_stock_backtest_consistency(client):
    r = post(client, "/api/backtest/stock", {"symbols": ["AAPL"], "entry": {"type": "every_n_days", "n": 21},
                                             "exit": {"hold_days": 5}, "side": "long", "start": "2020-01-01"})
    s, trades = r["summary"], r["trades"]
    assert s["count"] == len(trades) > 50
    assert s["starting_capital"] == 10_000 and any("$10,000" in n for n in r["notes"])
    assert s["total_pnl"] == pytest.approx(r["equity"][-1]["value"] - r["equity"][0]["value"])
    assert s["total_pnl"] == pytest.approx(s["starting_capital"] * s["total_return"])    # P/L agrees with the return
    assert s["win_rate"] == pytest.approx(sum(t["pnl"] > 0 for t in trades) / len(trades))
    assert all(t["ret"] == pytest.approx(t["exit_value"] / t["entry_value"] - 1) for t in trades)
    assert r["benchmark"] and r["equity"][0]["date"] >= "2020-01-01"
    assert post(client, "/api/backtest/stock", {"symbols": ["AAPL"], "entry": {"type": "moon"}, "exit": {},
                                                "side": "long"}, 422)["detail"].startswith("entry.type")


def test_options_backtest(client):
    body = {"symbol": "AAPL", "strategy": "iron-butterfly-short", "strike_rule": {"type": "atm"}, "width_pct": 0.05,
            "entry": {"type": "monthly"}, "target_dte": 30, "exit": {"type": "expiry"}, "start": "2025-01-01"}
    r = post(client, "/api/backtest/options", body)
    assert r["summary"]["count"] == len(r["trades"]) >= 18 and r["summary"]["model_count"] == r["summary"]["count"]
    assert r["summary"]["total_pnl"] == pytest.approx(sum(t["pnl"] for t in r["trades"]))
    assert "width_pct" in post(client, "/api/backtest/options", {**body, "width_pct": 5}, 422)["detail"]
    post(client, "/api/backtest/options", {**body, "strategy": "nope"}, 422)


def test_system_endpoints(client):
    h = client.get("/api/health").json()
    assert h["ok"] is True and h["auth"] is False and set(h) == {"ok", "version", "auth", "server_id"}
    st = client.get("/api/status").json()
    assert st["universe_size"] == 2 and st["iv_history_days"] == 1 and st["option_eod_days"] == 1
    assert st["row_counts"]["option_latest"] > 3000 and st["last_eod_date"] == "2026-11-18"
    assert [x["symbol"] for x in client.get("/api/search?q=aap").json()] == ["AAPL"]
    cv = client.get("/api/compare/volatility?symbols=AAPL,spy").json()
    assert cv["symbols"] == ["AAPL", "SPY"] and cv["table"][1]["iv30"] is None and cv["table"][0]["iv30"] > 0
    assert client.get("/api/compare/volatility?symbols=").status_code == 422


def test_api_only_unknown_paths_404_json(client):
    for path in ("/", "/s/AAPL/summary", "/assets/main.js", "/api/nope"):
        r = client.get(path)
        assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
        assert r.json() == {"detail": "Not Found"}
