"""A known symbol without a stored chain: tri-state `optionable` (null until a chain source answers),
404s with a machine-readable code (no_chain / no_options) on every chain endpoint, the no-options
ingest, and a weekend-stamped quote counted as the previous session's bar."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from optionlens.models import Bar, Quote
from optionlens.repo import bars, quotes, symbols
from optionlens.services import context

from .test_engine_api import _chain, _contract

SYM = "HPQ"
QSYM = "HPQV"
CHAIN_PATHS = ("chain", "activity", "open-interest", "skew", "iv-surface", "implied-moves", "straddle", "ideas",
               f"contract/{SYM}261218C00120000")


def _bars_only_symbol(sym: str) -> None:
    """A known stock with daily bars (AAPL's / 10, volume 1M) and nothing else."""
    symbols.upsert_symbols([{"symbol": sym, "name": f"{sym} test", "kind": "stock"}])
    aapl = bars.get_bars("AAPL")
    bars.upsert_bars([Bar(sym, d, o / 10, h / 10, lo / 10, c / 10, 1_000_000) for d, o, h, lo, c in zip(
        aapl["date"].astype(object).tolist(), aapl["open"].tolist(), aapl["high"].tolist(), aapl["low"].tolist(),
        aapl["close"].tolist())])


@pytest.fixture
def hpq(client):
    _bars_only_symbol(SYM)
    return client


def _codes(client) -> set:
    out = set()
    for p in CHAIN_PATHS:
        r = client.get(f"/api/symbol/{SYM}/{p}")
        assert r.status_code == 404 and "code" in r.json(), (p, r.text)
        out.add((r.json()["code"], r.json()["detail"]))
    r = client.post("/api/calc/strategy", json={"symbol": SYM, "legs": [{"kind": "stock", "qty": 100}]})
    assert r.status_code == 404
    return out | {(r.json()["code"], r.json()["detail"])}


def test_optionable_is_tri_state_and_no_chain_errors_carry_a_code(hpq):
    client = hpq
    assert client.get(f"/api/symbol/{SYM}/summary").json()["optionable"] is None      # never asked: unknown
    assert _codes(client) == {("no_chain", f"no option chain for {SYM}")}
    assert client.get(f"/api/symbol/{SYM}/volatility").status_code == 200              # realized vol needs no chain

    r = client.post("/api/ingest/no-options", json={"source": "ibkr", "symbol": SYM.lower()})
    assert r.status_code == 200 and r.json() == {"symbol": SYM, "optionable": False}
    assert client.get(f"/api/symbol/{SYM}/summary").json()["optionable"] is False
    assert _codes(client) == {("no_options", f"{SYM} lists no options")}

    assert client.post("/api/ingest/chain", json=_chain(symbol=SYM)).status_code == 200   # options listed after all
    assert client.get(f"/api/symbol/{SYM}/summary").json()["optionable"] is True
    assert client.get(f"/api/symbol/{SYM}/chain").status_code == 200


def test_no_options_ingest_validation(client):
    assert client.post("/api/ingest/no-options", json={"source": "ibkr", "symbol": "ZZZZZ"}).status_code == 404
    assert client.post("/api/ingest/no-options", json={"symbol": "AAPL"}).status_code == 422


def test_a_weekend_quote_is_the_previous_sessions_bar(client):
    sat = datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc)        # Saturday: Friday's closing data
    q = Quote("HPQX", sat, 30.0, 0.5, 0.0169, 29.6, 30.2, 29.5, 29.5, 10_000, None, None, None)
    assert context.quote_session(q).isoformat() == "2026-11-20"
    symbols.upsert_symbols([{"symbol": "HPQX", "name": "HPQ test", "kind": "stock"}])
    bars.upsert_bars([Bar("HPQX", datetime(2026, 11, 19).date(), 29.0, 29.6, 28.9, 29.5, 9_000)])
    quotes.upsert_quotes([q], "ibkr")
    got = context.load_bars("HPQX")
    assert [b.date.isoformat() for b in got] == ["2026-11-19", "2026-11-20"] and got[-1].close == 30.0


def test_without_a_quote_the_latest_daily_bar_is_the_quote(client):
    """No quote pushed (e.g. a weekend, no chain): headers and tables show the latest daily bar's close, OHLC,
    volume and change vs the previous close, tagged with the bars' source (never '—' while bars exist)."""
    _bars_only_symbol(QSYM)
    b = bars.get_bars(QSYM)
    q = client.get(f"/api/symbol/{QSYM}/summary").json()["quote"]
    last, prev = float(b["close"][-1]), float(b["close"][-2])
    assert q["price"] == pytest.approx(last) and q["prev_close"] == pytest.approx(prev)
    assert q["open"] == pytest.approx(float(b["open"][-1])) and q["volume"] == 1_000_000
    assert q["change"] == pytest.approx(last - prev) and q["change_pct"] == pytest.approx(last / prev - 1)
    assert q["source"] == "nasdaq" and q["ts"].startswith(str(b["date"][-1])[:10])
    wl = client.post("/api/watchlists", json={"name": "fallback", "symbols": [QSYM]}).json()
    row = client.get(f"/api/watchlists/{wl['id']}/table").json()["rows"][0]
    assert row["volume"] == 1_000_000 and row["price"] == pytest.approx(last)
    client.delete(f"/api/watchlists/{wl['id']}")


def test_a_pushed_stock_quote_keeps_ohlc_and_volume_and_fills_gaps_from_bars(client):
    QSYM = "HPQW"
    _bars_only_symbol(QSYM)
    ts = datetime.now(timezone.utc).isoformat()
    body = {"source": "ibkr", "delayed": True, "quotes": [{"symbol": QSYM, "price": 31.5, "change": 0.5, "change_pct": 0.016,
            "bid": 31.4, "ask": 31.6, "open": 31.0, "high": 32.0, "low": 30.9, "prev_close": 31.0, "volume": 2_500_000.0, "ts": ts}]}
    assert client.post("/api/ingest/quotes", json=body).json() == {"stored": 1}
    q = client.get(f"/api/symbol/{QSYM}/summary").json()["quote"]
    assert (q["price"], q["open"], q["high"], q["low"], q["volume"], q["bid"]) == (31.5, 31.0, 32.0, 30.9, 2_500_000, 31.4)
    assert q["source"] == "ibkr" and q["delayed"] is True
    assert client.post("/api/ingest/quotes", json={**body, "quotes": [{**body["quotes"][0], "volume": -1}]}).status_code == 422


def test_reference_price_and_a_chain_centred_on_it(client):
    """GET /api/ingest/reference-price: the engine's own price (here the latest daily close, no quote stored) for a
    client whose feed sent no underlying price; a chain pushed with that price (`reference`) is stored and served,
    but the price becomes the stored quote only while there is none."""
    sym = "HPQR"
    _bars_only_symbol(sym)
    ref = client.get(f"/api/ingest/reference-price/{sym.lower()}")
    assert ref.status_code == 200, ref.text
    body = ref.json()
    last = bars.get_bars(sym)["close"][-1]
    assert body["symbol"] == sym and body["price"] == pytest.approx(last) and body["source"] == "nasdaq" and body["ts"]
    assert client.get("/api/ingest/reference-price/ZZZZZ").status_code == 404
    chain = _chain(symbol=sym, delayed=True, contracts=[_contract(k, r) for k in (30.0, 35.0, 40.0) for r in ("C", "P")])
    chain["underlying"] = {"price": body["price"], "ts": body["ts"], "reference": True}
    assert client.post("/api/ingest/chain", json=chain).status_code == 200
    r = client.get(f"/api/symbol/{sym}/chain")
    assert r.status_code == 200, r.text
    q = quotes.get_quote(sym)                                             # none was stored: it stands in
    assert q.price == pytest.approx(last) and q.ts.isoformat() == "2026-11-18T21:00:00+00:00"
    quotes.upsert_quotes([Quote(sym, datetime(2026, 11, 19, 20, 0, tzinfo=timezone.utc), 36.0, None, None, None,
                                None, None, None, None, None, None, None)], "nasdaq")
    assert client.post("/api/ingest/chain", json=chain).status_code == 200
    assert quotes.get_quote(sym).price == 36.0                            # a stored quote is never replaced by it
