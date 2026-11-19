"""Suite-wide guards: tests never reach the live CBOE quote endpoint."""
import pytest

from optionlens.net import FetchError
from optionlens.sources import cboe


@pytest.fixture(autouse=True)
def _offline_index_quotes(monkeypatch):
    async def no_network(symbol):
        raise FetchError(f"test://cboe/quotes/{symbol}", None, "network disabled in tests")

    monkeypatch.setattr(cboe, "fetch_quote", no_network)
