"""Suite-wide guards: tests never reach CBOE (its last fetcher, the index history CSV) or Questrade: the configured
server-side chain source (Questrade, by default) is an offline stand-in that is not connected."""
import pytest

from optionlens.net import FetchError
from optionlens.sources import cboe, chain_source


class OfflineSource:
    """A configured server-side ChainSource that is not connected."""
    name = "questrade"

    def unavailable(self):
        return "not connected"

    async def keepalive(self):
        return None

    def status(self):
        return {"state": "disconnected", "detail": None, "refreshed_at": None, "calls_last_hour": 0,
                "hour_budget": 10500, "remaining": None, "reset_at": None, "paused_until": None, "delayed": None,
                "expiries": 8, "moneyness": 0.25}

    async def fetch_chain(self, symbol, final=False):
        raise AssertionError("an offline source is never asked for a chain")


@pytest.fixture(autouse=True)
def _offline_sources(monkeypatch):
    async def no_network(symbol):
        raise FetchError(f"test://cboe/history/{symbol}", None, "network disabled in tests")

    monkeypatch.setattr(cboe, "fetch_history", no_network)
    offline = OfflineSource()
    monkeypatch.setattr(chain_source, "REGISTRY", {"questrade": lambda: offline})
