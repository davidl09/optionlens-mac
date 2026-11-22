"""Questrade as a server-side chain source (docs/INTEGRATION.md §8): auth (OAuth refresh-token rotation),
ratelimit (pacing from the documented limits and the response headers), client (REST + retries), mapping (JSON ->
models, pure) and source (QuestradeSource, the ChainSource)."""
from __future__ import annotations

from ... import net
from ...config import get_settings
from .auth import TokenError, TokenRejected
from .source import NAME, QuestradeSource

__all__ = ["NAME", "QuestradeSource", "TokenError", "TokenRejected", "instance"]

_source: QuestradeSource | None = None


def instance() -> QuestradeSource:
    """The process-wide QuestradeSource (one token manager and rate gate per engine)."""
    global _source
    if _source is None:
        s = get_settings()
        _source = QuestradeSource(s.data_dir, s.questrade_expiries, s.questrade_moneyness, net.client)
    return _source
