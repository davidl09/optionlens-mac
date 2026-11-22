"""/api/sources/questrade: connect with a pasted refresh token, status, disconnect; the token is never echoed (not in
responses, not in validation errors) and responses match types.ts QuestradeStatus."""
from __future__ import annotations

import pytest

from optionlens.config import get_settings
from optionlens.sources import chain_source
from optionlens.sources.questrade.source import QuestradeSource

from ..questrade_fake import FakeQuestrade
from .tsschema import load_types, mismatches

SECRETS = ("pasted-token", "rotated-", "access-")


@pytest.fixture
def qt(client, monkeypatch, tmp_path):
    fake = FakeQuestrade()
    src = QuestradeSource(tmp_path, get_settings().questrade_expiries, get_settings().questrade_moneyness, fake.http)
    monkeypatch.setitem(chain_source.REGISTRY, "questrade", lambda: src)
    return fake, src, tmp_path


def no_secret(text: str) -> None:
    for s in SECRETS:
        assert s not in text


def test_connect_status_disconnect(client, qt):
    fake, src, data = qt
    schema = load_types().parse("QuestradeStatus")
    r = client.get("/api/sources/questrade")
    assert r.status_code == 200 and r.json()["state"] == "disconnected" and mismatches(r.json(), schema) == []
    r = client.post("/api/sources/questrade/token", json={"refresh_token": "wrong-token"})
    assert r.status_code == 422 and r.json()["code"] == "token_rejected" and "API Centre" in r.json()["detail"]
    assert "wrong-token" not in r.text
    r = client.post("/api/sources/questrade/token", json={"refresh_token": "pasted-token"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "connected" and body["hour_budget"] == 10_500 and body["expiries"] == 8
    assert mismatches(body, schema) == []
    no_secret(r.text)
    assert (data / "secrets" / "questrade.json").exists()
    st = client.get("/api/status")
    entry = next(s for s in st.json()["sources"] if s["name"] == "questrade")
    assert entry["ok"] is True and "calls this hour" in entry["detail"]
    no_secret(st.text)
    r = client.delete("/api/sources/questrade/token")
    assert r.status_code == 200 and r.json()["state"] == "disconnected"
    assert not (data / "secrets" / "questrade.json").exists()


def test_token_is_not_echoed_in_validation_errors(client, qt):
    r = client.post("/api/sources/questrade/token", json={"refresh_token": "pasted-token" + "x" * 600})
    assert r.status_code == 422
    no_secret(r.text)
    r = client.post("/api/sources/questrade/token", json={"refresh_token": "pasted-token", "extra": 1})
    assert r.status_code == 422
    no_secret(r.text)


def test_login_unreachable_is_reported(client, qt):
    import httpx
    fake, _, _ = qt
    fake.queue["/oauth2/token"] = [httpx.Response(502)]
    r = client.post("/api/sources/questrade/token", json={"refresh_token": "pasted-token"})
    assert r.status_code == 422 and r.json()["code"] == "upstream"
    assert client.get("/api/sources/questrade").json()["state"] == "disconnected"     # a failed paste stores nothing


def test_not_enabled(client, monkeypatch):
    monkeypatch.setattr(chain_source, "get_settings", lambda: type("S", (), {"chain_sources": ("ibkr-bridge",)})())
    assert client.get("/api/sources/questrade").status_code == 404
    assert all(s["name"] != "questrade" for s in client.get("/api/status").json()["sources"])


def test_connecting_starts_collecting_when_the_scheduler_runs(client, qt, monkeypatch):
    from optionlens.jobs import scheduler
    from optionlens.services import sources
    asked = []
    monkeypatch.setattr(sources.scheduler, "run_soon", asked.append)
    monkeypatch.setattr(sources, "after_session", lambda now: True)
    assert client.post("/api/sources/questrade/token", json={"refresh_token": "pasted-token"}).status_code == 200
    assert asked == ["sweep_fill"]
    assert not scheduler.is_running()      # API tests run without the scheduler: run_soon is a no-op there
    scheduler.run_soon("sweep")            # no loop needed when not running
