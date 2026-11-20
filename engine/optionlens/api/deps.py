"""Shared router helpers: run a blocking service call in a worker thread and wrap the result."""
from __future__ import annotations

import functools
import re
from typing import Any, Callable

import anyio.to_thread
from fastapi import Path

from ..models import normalize_symbol
from ..services.errors import BadRequest
from .json import ApiJSON

_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")


async def respond(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> ApiJSON:
    """Run fn(*args, **kwargs) in a worker thread (services block on DuckDB / on-demand fetches)."""
    return ApiJSON(await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs)))


def symbol_param(sym: str = Path(..., description="Ticker; indices as SPX or ^SPX")) -> str:
    """Upper-cased canonical symbol (422 when malformed)."""
    s = normalize_symbol(sym)
    if not _SYMBOL.match(s):
        raise BadRequest(f"malformed symbol {sym!r}")
    return s


def symbols_list(raw: str) -> list[str]:
    """Comma-separated symbols query param -> canonical symbols."""
    out = [symbol_param(s) for s in raw.split(",") if s.strip()]
    if not out:
        raise BadRequest("symbols required")
    return out
