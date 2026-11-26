"""Suite-wide guards: tests never reach CBOE (its last fetcher, the index history CSV), Questrade (the configured
server-side chain source, by default, is an offline stand-in that is not connected), TMX Money or Yahoo Finance."""
import pytest

from optionlens.jobs import chains, ondemand, source_health
from optionlens.net import FetchError
from optionlens.services import refresh
from optionlens.sources import cboe, chain_source, tmx, yahoo


class OfflineSource:
    """A configured server-side ChainSource that is not connected."""
    name = "questrade"

    def connected(self):
        return False

    def unavailable(self):
        return "not connected"

    def problem(self):
        return None

    async def keepalive(self):
        return None

    def status(self):
        return {"state": "disconnected", "detail": None, "refreshed_at": None, "calls_last_hour": 0,
                "hour_budget": 10500, "remaining": None, "reset_at": None, "paused_until": None, "delayed": None,
                "expiries": 8, "moneyness": 0.25, "verification": None}

    async def fetch_chain(self, symbol, final=False):
        raise AssertionError("an offline source is never asked for a chain")


@pytest.fixture(autouse=True)
def _offline_sources(monkeypatch):
    async def no_network(symbol):
        raise FetchError(f"test://cboe/history/{symbol}", None, "network disabled in tests")

    monkeypatch.setattr(cboe, "fetch_history", no_network)

    async def no_tmx(op, query, variables):
        raise FetchError(f"test://tmx/{op}", None, "network disabled in tests")

    async def no_yahoo(symbol):
        raise FetchError(f"test://yahoo/{symbol}", None, "network disabled in tests")

    monkeypatch.setattr(tmx, "query", no_tmx)
    monkeypatch.setattr(yahoo, "_summary", no_yahoo)
    offline = OfflineSource()
    monkeypatch.setattr(chain_source, "REGISTRY", {"questrade": lambda: offline})


@pytest.fixture(autouse=True)
def _fresh_source_state():
    """Fetch outcomes and refresh rounds are process-wide memory: every test starts without them."""
    source_health._by_source.clear()
    refresh._rounds.clear()
    chains._final_done.clear()     # closing snapshots taken (per market and session)
    ondemand._locks.clear()        # asyncio locks are bound to the loop of the test that first contended them
    yield
    source_health._by_source.clear()
    refresh._rounds.clear()
