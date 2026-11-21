"""A known symbol without a stored chain: tri-state `optionable` (null until a chain source answers),
404s with a machine-readable code (no_chain / no_options) on every chain endpoint, the no-options
ingest, and a weekend-stamped quote counted as the previous session's bar."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from optionlens.models import Bar, Quote
from optionlens.repo import bars, quotes, symbols
from optionlens.services import context

from .test_engine_api import _chain

SYM = "HPQ"
CHAIN_PATHS = ("chain", "activity", "open-interest", "skew", "implied-moves", "straddle", "ideas",
               f"contract/{SYM}261218C00120000")


@pytest.fixture
def hpq(client):
    symbols.upsert_symbols([{"symbol": SYM, "name": "HP Inc.", "kind": "stock"}])
    aapl = bars.get_bars("AAPL")
    bars.upsert_bars([Bar(SYM, d, o / 10, h / 10, lo / 10, c / 10, 1_000_000) for d, o, h, lo, c in zip(
        aapl["date"].astype(object).tolist(), aapl["open"].tolist(), aapl["high"].tolist(), aapl["low"].tolist(),
        aapl["close"].tolist())])
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
