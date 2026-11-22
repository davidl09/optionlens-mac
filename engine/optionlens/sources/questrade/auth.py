"""Questrade OAuth for a personal app (docs/INTEGRATION.md §8): the user pastes a refresh token generated in the
API Centre; the engine redeems it at the token endpoint for an access token + API server URL and a NEW refresh
token, which replaces the old one at once (the old one is spent).

- The rotated refresh token is persisted before the access token is used: DATA_DIR/secrets/questrade.json,
  written atomically with mode 0600 (the directory 0700; on Windows the user profile's default ACL applies).
- Tokens are never logged, echoed by the API or put in a URL (the token request sends them as a form body).
- An access token is refreshed shortly before `expires_in` runs out, and after a 401 (TokenManager.invalidate).
- Questrade refusing the refresh token (expired, already used, revoked) -> state "expired": the user must paste
  a new one. Network or server trouble -> state "error"; the token is kept and the next call retries.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, NoReturn
from urllib.parse import urlsplit

import httpx

from ...fileio import read_json, write_json_atomic

log = logging.getLogger(__name__)

TOKEN_URL = "https://login.questrade.com/oauth2/token"
API_HOST_SUFFIX = ".questrade.com"
EXPIRY_MARGIN_S = 60.0          # refresh this long before the access token's expires_in runs out
RETRY_AFTER_S = 60.0            # after a transient refresh failure, calls fail fast this long (no login storm)
REJECTED = ("Questrade refused the refresh token (it expired, was already used or was revoked). "
            "Generate a new one in Questrade's API Centre and paste it here.")

State = Literal["disconnected", "connected", "expired", "error"]


class TokenError(Exception):
    """No usable access token. The message is for people and never contains a token."""


class TokenRejected(TokenError):
    """Questrade refused the refresh token: a new one must be pasted."""


class NotConnected(TokenError):
    """No refresh token stored."""


@dataclass(frozen=True)
class Access:
    token: str
    api_server: str        # e.g. https://api01.iq.questrade.com (no trailing slash, no /v1)
    expires_at: float      # time.time()


def api_base(raw: str) -> str:
    """The `api_server` of a token response as a base URL without a trailing '/' or '/v1' (the docs show both
    forms). Only https hosts under questrade.com are accepted: the access token is sent there."""
    url = urlsplit(str(raw).strip())
    host = url.hostname or ""
    if url.scheme != "https" or not (host == API_HOST_SUFFIX[1:] or host.endswith(API_HOST_SUFFIX)):
        raise TokenError("Questrade's token response named an unexpected API server")
    path = url.path.rstrip("/")
    path = path[:-3] if path.endswith("/v1") else path
    return f"https://{url.netloc}{path}"


class TokenStore:
    """The refresh token and connection state in one 0600 JSON file."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        rec = read_json(self.path)
        return rec if isinstance(rec, dict) else {}

    def save(self, rec: dict) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_json_atomic(self.path, rec, mode=0o600)

    def clear(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


class TokenManager:
    """Access tokens from the stored refresh token (one refresh at a time: a refresh token works once)."""

    def __init__(self, store: TokenStore, http: Callable[[], httpx.AsyncClient], clock: Callable[[], float] = time.time):
        self.store, self.http, self.clock = store, http, clock
        self._access: Access | None = None
        self._lock = asyncio.Lock()
        self._retry_at = 0.0
        rec = store.load()
        self._refresh_token: str | None = rec.get("refresh_token") or None
        self.state: State = rec.get("state", "connected") if self._refresh_token else "disconnected"
        self.detail: str | None = rec.get("detail")
        self.refreshed_at: str | None = rec.get("refreshed_at")

    def unusable(self) -> str | None:
        """Why no call can be made now (not connected, token refused, waiting after a failed refresh), else None."""
        if self._refresh_token is None:
            return "not connected"
        if self.state == "expired":
            return "the refresh token was refused: paste a new one"
        if self._access is None and self.clock() < self._retry_at:
            return self.detail or "waiting to retry the login"
        return None

    def status(self) -> dict:
        """{state, detail, refreshed_at}: never a token."""
        return {"state": self.state, "detail": self.detail, "refreshed_at": self.refreshed_at}

    async def access(self) -> Access:
        """A valid access token, refreshing when it is missing or about to expire. Raises TokenError."""
        async with self._lock:
            a = self._access
            if a is not None and a.expires_at - EXPIRY_MARGIN_S > self.clock():
                return a
            if self._refresh_token is None:
                raise NotConnected("Questrade is not connected: paste a refresh token in Settings → Data sources")
            if self.state == "expired":
                raise TokenRejected(REJECTED)
            if self.clock() < self._retry_at:
                raise TokenError(self.detail or "waiting to retry the Questrade login")
            return await self._refresh(self._refresh_token)

    def invalidate(self, used: Access) -> None:
        """The API answered 401 for `used`: the next access() refreshes."""
        if self._access is used:
            self._access = None

    async def connect(self, refresh_token: str) -> dict:
        """Redeem a pasted refresh token (replacing any stored one). Raises TokenRejected / TokenError."""
        token = refresh_token.strip()
        if not token or any(ch.isspace() for ch in token):
            raise TokenRejected("That does not look like a Questrade refresh token (copy it from the API Centre).")
        async with self._lock:
            self._access = None
            await self._refresh(token, pasted=True)
        return self.status()

    def disconnect(self) -> None:
        """Forget the stored token (Questrade's API Centre can revoke it)."""
        self._access, self._refresh_token = None, None
        self.store.clear()
        self.state, self.detail, self.refreshed_at = "disconnected", None, None

    async def keepalive(self, max_age_s: float) -> bool:
        """Refresh when the last refresh is older than `max_age_s`, so an idle refresh token does not lapse.
        Returns whether it refreshed; TokenError as access()."""
        last = datetime.fromisoformat(self.refreshed_at).timestamp() if self.refreshed_at else 0.0
        if self._refresh_token is None or self.state == "expired" or self.clock() - last < max_age_s:
            return False
        async with self._lock:
            await self._refresh(self._refresh_token)
        return True

    # ------------------------------------------------------------------ internals (caller holds the lock)
    def _record(self, state: State, detail: str | None) -> None:
        self.state, self.detail = state, detail
        if self._refresh_token is None:
            return
        try:
            self.store.save({"refresh_token": self._refresh_token, "state": state, "detail": detail,
                             "refreshed_at": self.refreshed_at})
        except OSError as e:
            self.state, self.detail = "error", f"Could not save the Questrade token: {e.strerror or type(e).__name__}"
            log.error("questrade: could not save the token file (%s)", e.strerror or type(e).__name__)

    async def _refresh(self, refresh_token: str, pasted: bool = False) -> Access:
        try:
            r = await self.http().post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                                       headers={"Accept": "application/json"})
        except httpx.HTTPError as e:
            self._fail(TokenError(f"Questrade login is unreachable ({type(e).__name__})"), pasted)
        if r.status_code in (400, 401, 403):
            if pasted:
                raise TokenRejected(REJECTED)
            self._access = None
            self._record("expired", REJECTED)
            log.warning("questrade: the refresh token was refused; a new one must be pasted")
            raise TokenRejected(REJECTED)
        if not r.is_success:
            self._fail(TokenError(f"Questrade login answered HTTP {r.status_code}"), pasted)
        try:
            body = r.json()
            access = Access(str(body["access_token"]), api_base(body["api_server"]),
                            self.clock() + float(body["expires_in"]))
            new_refresh = str(body["refresh_token"])
        except (ValueError, KeyError, TypeError, TokenError) as e:
            self._fail(e if isinstance(e, TokenError) else TokenError("Questrade's token response was not understood"),
                       pasted)
        # The old refresh token is spent now: persist the new one before anything else.
        self._refresh_token, self._access = new_refresh, access
        self.refreshed_at = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="seconds")
        self._record("connected", None)
        log.info("questrade: access token refreshed (valid %.0f min)", (access.expires_at - self.clock()) / 60)
        return access

    def _fail(self, err: TokenError, pasted: bool) -> NoReturn:
        """A transient refresh failure: a pasted token is not stored; a stored one is kept (state error)."""
        if not pasted:
            self._retry_at = self.clock() + RETRY_AFTER_S
            self._record("error", str(err))
        log.warning("questrade: token refresh failed: %s", err)
        raise err
