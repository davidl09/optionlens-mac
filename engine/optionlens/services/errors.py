"""Service-level errors the API maps to HTTP statuses (404 / 422 / 502)."""
from __future__ import annotations


class NotFound(LookupError):
    """A requested resource (expiry, contract, strategy, data) does not exist -> 404."""


class BadRequest(ValueError):
    """Request parameters are invalid -> 422 (with a machine-readable `code` where the UI acts on it)."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class NoChain(NotFound):
    """The symbol is known but no option chain is stored -> 404 with a machine-readable `code`
    (docs/INTEGRATION.md §5): "no_options" when a chain source confirmed the symbol lists no options
    (optionable false), else "no_chain" (not fetched yet)."""

    def __init__(self, symbol: str, optionable: bool | None):
        self.code = "no_options" if optionable is False else "no_chain"
        super().__init__(f"{symbol} lists no options" if optionable is False else f"no option chain for {symbol}")


class Unavailable(RuntimeError):
    """A data source could not be reached and nothing is stored to show instead -> 502; the message is a plain
    sentence for the user (no URLs or status codes)."""
