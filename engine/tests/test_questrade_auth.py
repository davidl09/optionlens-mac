"""Questrade OAuth (sources/questrade/auth.py): redeeming a pasted refresh token, rotation persisted atomically with
0600 permissions before use, refresh on expiry, refused / transient failures, and no token in logs or status."""
from __future__ import annotations

import json
import logging
import os
import stat
import sys

import httpx
import pytest

from optionlens import fileio
from optionlens.sources.questrade import auth
from optionlens.sources.questrade.auth import TokenManager, TokenStore

from .questrade_fake import API, FakeQuestrade

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def manager(tmp_path, fake: FakeQuestrade, clock=None) -> TokenManager:
    return TokenManager(TokenStore(tmp_path / "secrets" / "questrade.json"), fake.http, clock or Clock())


def stored(tmp_path) -> dict:
    return json.loads((tmp_path / "secrets" / "questrade.json").read_text())


async def test_connect_redeems_and_persists_the_rotated_token(tmp_path):
    fake = FakeQuestrade()
    tm = manager(tmp_path, fake)
    assert tm.status()["state"] == "disconnected" and tm.unusable() == "not connected"
    st = await tm.connect("  pasted-token\n")
    assert st["state"] == "connected" and "token" not in json.dumps(st).replace("refreshed_at", "")
    login = fake.requests[0]
    assert login.method == "POST" and str(login.url) == auth.TOKEN_URL           # no token in the URL
    assert login.content.decode() == "grant_type=refresh_token&refresh_token=pasted-token"
    assert stored(tmp_path)["refresh_token"] == "rotated-1" and stored(tmp_path)["state"] == "connected"
    a = await tm.access()
    assert (a.token, a.api_server) == ("access-1", API) and len(fake.logins) == 1   # cached until expiry
    assert not [p for p in (tmp_path / "secrets").iterdir() if p.name != "questrade.json"]   # no temp files left


@POSIX
async def test_token_file_is_private(tmp_path):
    await manager(tmp_path, FakeQuestrade()).connect("pasted-token")
    assert stat.S_IMODE(os.stat(tmp_path / "secrets" / "questrade.json").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "secrets").st_mode) == 0o700


async def test_expiry_refreshes_with_the_rotated_token_and_a_restart_resumes(tmp_path):
    fake, clock = FakeQuestrade(expires_in=1800), Clock()
    tm = manager(tmp_path, fake, clock)
    await tm.connect("pasted-token")
    clock.t += 1800 - auth.EXPIRY_MARGIN_S + 1                                     # about to expire
    assert (await tm.access()).token == "access-2"
    assert fake.logins == ["pasted-token", "rotated-1"] and stored(tmp_path)["refresh_token"] == "rotated-2"
    again = manager(tmp_path, fake, clock)                                         # engine restart
    assert again.status()["state"] == "connected"
    assert (await again.access()).token == "access-3" and stored(tmp_path)["refresh_token"] == "rotated-3"


async def test_a_401_invalidates_the_access_token(tmp_path):
    fake = FakeQuestrade()
    tm = manager(tmp_path, fake)
    await tm.connect("pasted-token")
    a = await tm.access()
    tm.invalidate(a)
    assert (await tm.access()).token == "access-2"


async def test_a_refused_refresh_token_needs_a_new_paste(tmp_path):
    fake = FakeQuestrade()
    tm = manager(tmp_path, fake)
    await tm.connect("pasted-token")
    fake.valid.clear()                                                             # e.g. expired after 7 idle days
    tm.invalidate(await tm.access())
    with pytest.raises(auth.TokenRejected, match="paste"):
        await tm.access()
    assert tm.status()["state"] == "expired" and stored(tmp_path)["state"] == "expired"
    assert tm.unusable().startswith("the refresh token was refused")
    n = len(fake.logins)
    with pytest.raises(auth.TokenRejected):
        await tm.access()
    assert len(fake.logins) == n                                                   # no login storm
    fake.valid.add("new-paste")
    assert (await tm.connect("new-paste"))["state"] == "connected" and tm.unusable() is None


async def test_a_refused_paste_keeps_the_current_connection(tmp_path):
    fake = FakeQuestrade()
    tm = manager(tmp_path, fake)
    await tm.connect("pasted-token")
    with pytest.raises(auth.TokenRejected):
        await tm.connect("wrong-token")
    with pytest.raises(auth.TokenRejected, match="does not look like"):
        await tm.connect("two words")
    assert tm.status()["state"] == "connected" and stored(tmp_path)["refresh_token"] == "rotated-1"


async def test_transient_login_failure_keeps_the_token_and_retries_later(tmp_path):
    fake, clock = FakeQuestrade(), Clock()
    tm = manager(tmp_path, fake, clock)
    await tm.connect("pasted-token")
    tm.invalidate(await tm.access())
    fake.queue["/oauth2/token"] = [httpx.Response(503)]
    with pytest.raises(auth.TokenError, match="HTTP 503"):
        await tm.access()
    assert tm.status()["state"] == "error" and stored(tmp_path)["refresh_token"] == "rotated-1"
    assert tm.unusable() is not None
    n = len(fake.logins)
    with pytest.raises(auth.TokenError):
        await tm.access()                                                          # fails fast for RETRY_AFTER_S
    assert len(fake.logins) == n
    clock.t += auth.RETRY_AFTER_S
    assert (await tm.access()).token == "access-2" and tm.status()["state"] == "connected"


async def test_unexpected_api_server_is_refused():
    assert auth.api_base("https://api01.iq.questrade.com/v1") == "https://api01.iq.questrade.com"
    assert auth.api_base("https://api05.iq.questrade.com/") == "https://api05.iq.questrade.com"
    for bad in ("http://api01.iq.questrade.com/", "https://evil.example.com/", "https://questrade.com.evil.io/"):
        with pytest.raises(auth.TokenError):
            auth.api_base(bad)


async def test_disconnect_forgets_the_token(tmp_path):
    tm = manager(tmp_path, FakeQuestrade())
    await tm.connect("pasted-token")
    tm.disconnect()
    assert not (tmp_path / "secrets" / "questrade.json").exists() and tm.status()["state"] == "disconnected"
    with pytest.raises(auth.NotConnected):
        await tm.access()


async def test_keepalive_refreshes_an_idle_token(tmp_path):
    fake, clock = FakeQuestrade(), Clock()
    tm = manager(tmp_path, fake, clock)
    await tm.connect("pasted-token")
    assert await tm.keepalive(86400) is False
    clock.t += 86400 + 1
    assert await tm.keepalive(86400) is True and stored(tmp_path)["refresh_token"] == "rotated-2"


async def test_a_failed_save_never_loses_the_old_file(tmp_path, monkeypatch):
    tm = manager(tmp_path, FakeQuestrade())
    await tm.connect("pasted-token")
    before = (tmp_path / "secrets" / "questrade.json").read_text()

    def boom(src, dst):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(fileio.os, "replace", boom)
    tm.invalidate(await tm.access())
    assert (await tm.access()).token == "access-2"                                 # keeps working in memory
    assert tm.status()["state"] == "error" and "No space left" in tm.status()["detail"]
    assert (tmp_path / "secrets" / "questrade.json").read_text() == before
    assert [p.name for p in (tmp_path / "secrets").iterdir()] == ["questrade.json"]


async def test_tokens_are_never_logged(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeQuestrade()
    tm = manager(tmp_path, fake)
    await tm.connect("pasted-token")
    tm.invalidate(await tm.access())
    await tm.access()
    fake.valid.clear()
    tm.invalidate(await tm.access())
    with pytest.raises(auth.TokenRejected):
        await tm.access()
    for secret in ("pasted-token", "rotated-", "access-"):
        assert secret not in caplog.text
