"""Questrade API errors, classified (docs/INTEGRATION.md §8 "Errors").

Error table of questrade.com/api/documentation/error-handling (read 2026-11-22), HTTP status / code / message:
404 1001 Invalid endpoint · 400 1002 Invalid or malformed argument · 400 1003 Argument length exceeds imposed limit ·
400 1004 Missing required argument · 413 1005 Request length exceeds imposed limit · 429 1006 Rate limit exceeded ·
500 1007 IQ servers responded with a business error · 500 1008 … technical error · 500 1009 … unexpected error ·
502 1010 IQ servers produced an invalid response · 503 1011 IQ servers did not produce a response before a timeout ·
405 1012 Method unsupported by endpoint · 400 1013 Requesting anything other than 'application/json' ·
401 1014 Missing authorization header · 400 1015 Malformed authorization header ·
403 1016 Request is out-of-allowed OAuth scopes · 401 1017 Access token is invalid · 404 1018 Account number not found ·
404 1019 Symbol not found · 404 1020 Order not found · 500 1021 Unexpected error (with undefined handling).

Classes (what the engine does):
- "auth": HTTP 401 (after one token refresh) or 403, or codes 1014-1017. Every further call would fail the same way:
  the source "needs attention" (sweeps and on-demand refreshes stop at once) until a new token is pasted.
- "rate": HTTP 429 / code 1006: wait for the limit's reset (ratelimit.RateGate), then retry.
- "transient": 5xx, timeouts, connection errors: retried (client backoff, then the sweep's retry pass).
- "other": the request itself was refused (e.g. 404 1019): that symbol fails, nothing is retried.
"""
from __future__ import annotations

from typing import Literal

from ...net import FetchError

Kind = Literal["auth", "rate", "transient", "other"]

AUTH_CODES = frozenset({1014, 1015, 1016, 1017})
RATE_CODE = 1006

# Short meaning of a code, for grouped failure reasons ("403 code 1016 (missing market-data permission)").
MEANING = {
    1001: "invalid endpoint", 1002: "invalid argument", 1003: "argument too long", 1004: "missing argument",
    1005: "request too long", 1006: "rate limit exceeded", 1007: "Questrade business error",
    1008: "Questrade technical error", 1009: "Questrade unexpected error", 1010: "invalid response from Questrade",
    1011: "Questrade timed out", 1012: "method not supported", 1013: "not JSON",
    1014: "missing authorization header", 1015: "malformed authorization header",
    1016: "missing market-data permission", 1017: "access token invalid", 1018: "account not found",
    1019: "symbol not found", 1020: "order not found", 1021: "Questrade unexpected error",
}

NEW_TOKEN = "generate a new manual token in Questrade's API Centre and paste it here."
# What the user has to do, per auth code (shown as the source's status and in refresh results).
ATTENTION = {
    1016: ("Your Questrade app lacks the 'Retrieve delayed and real-time market data' permission. In Questrade's "
           "API Centre enable it for your personal app, generate a new manual token, and paste it here."),
    1017: ("Questrade does not accept the access token, even right after a refresh (code 1017: access token is "
           f"invalid): {NEW_TOKEN}"),
    1014: ("Questrade received no authorization header (code 1014); a proxy or security software may be removing "
           f"it. If this persists, {NEW_TOKEN}"),
    1015: f"Questrade rejected the authorization header as malformed (code 1015): {NEW_TOKEN}",
}


def classify(status: int | None, code: int | None) -> Kind:
    if status in (401, 403) or code in AUTH_CODES:
        return "auth"
    if status == 429 or code == RATE_CODE:
        return "rate"
    if status is None or status >= 500:
        return "transient"
    return "other"


def reason(status: int | None, code: int | None, message: str | None = None) -> str:
    """The grouping key of a failure, e.g. "403 code 1016 (missing market-data permission)"."""
    head = f"{status}" if status is not None else "no response"
    if code is not None:
        head += f" code {code}"
    meaning = MEANING.get(code) if code is not None else None
    meaning = meaning or (message or "").strip().rstrip(".") or None
    return f"{head} ({meaning})" if meaning else head


def attention(status: int | None, code: int | None, message: str | None) -> str:
    """What the user has to do after an auth-class error."""
    if code in ATTENTION:
        return ATTENTION[code]
    return (f"Questrade refused access (HTTP {status}{f' code {code}' if code is not None else ''}"
            f"{f': {message}' if message else ''}). Check your personal app's permissions in Questrade's API Centre, "
            "generate a new manual token, and paste it here.")


class QuestradeError(FetchError):
    """A failed Questrade call: `code` / `message` as Questrade sent them, `kind` (see module doc) and `reason`
    (the grouping key of failure counts)."""

    def __init__(self, url: str, status: int | None, code: int | None = None, message: str | None = None,
                 kind: Kind | None = None, reason_text: str | None = None):
        detail = f"code {code}: {message}" if code is not None else (message or "")
        super().__init__(url, status, detail)
        self.code, self.message = code, message
        self.kind: Kind = kind or classify(status, code)
        self.reason = reason_text or reason(status, code, message)

    @property
    def attention(self) -> str | None:
        """What the user has to do (auth class), else None."""
        return attention(self.status, self.code, self.message) if self.kind == "auth" else None
