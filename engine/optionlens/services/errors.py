"""Service-level errors the API maps to HTTP statuses (404 / 422)."""
from __future__ import annotations


class NotFound(LookupError):
    """A requested resource (expiry, contract, strategy, data) does not exist -> 404."""


class BadRequest(ValueError):
    """Request parameters are invalid -> 422."""


class NoChain(NotFound):
    """The symbol is known but no option chain is stored -> 404 with a machine-readable `code`
    (docs/INTEGRATION.md §5): "no_options" when a chain source confirmed the symbol lists no options
    (optionable false), else "no_chain" (not fetched yet)."""

    def __init__(self, symbol: str, optionable: bool | None):
        self.code = "no_options" if optionable is False else "no_chain"
        super().__init__(f"{symbol} lists no options" if optionable is False else f"no option chain for {symbol}")
