"""Suite-wide guards: tests never reach CBOE (its last fetcher, the index history CSV) and run without a
server-side chain source."""
import pytest

from optionlens.net import FetchError
from optionlens.sources import cboe, chain_source


@pytest.fixture(autouse=True)
def _offline_sources(monkeypatch):
    async def no_network(symbol):
        raise FetchError(f"test://cboe/history/{symbol}", None, "network disabled in tests")

    monkeypatch.setattr(cboe, "fetch_history", no_network)
    monkeypatch.setattr(chain_source, "REGISTRY", {})
