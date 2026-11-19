"""Service-level errors the API maps to HTTP statuses (404 / 422)."""
from __future__ import annotations


class NotFound(LookupError):
    """A requested resource (expiry, contract, strategy, data) does not exist -> 404."""


class BadRequest(ValueError):
    """Request parameters are invalid -> 422."""
