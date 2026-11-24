"""An in-memory Questrade (login + the market calls OptionLens uses) behind httpx.MockTransport, with the JSON
shapes of questrade.com/api/documentation. One underlying, XYZ (symbolId 1000): expiries 2026-11-13 (expired),
11-20, 11-27, 12-18; strikes 70..130 step 10 on root XYZ, plus an adjusted root XYZ1 that must be dropped."""
from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx

API = "https://api07.iq.questrade.com"
EXPIRIES = ("2026-11-13", "2026-11-20", "2026-11-27", "2026-12-18")
STRIKES = tuple(range(70, 140, 10))


def option_id(expiry: str, strike: int, right: str) -> int:
    return 2_000_000 + EXPIRIES.index(expiry) * 10_000 + strike * 10 + (1 if right == "P" else 0)


def _chain() -> dict:
    def per_strike(e: str) -> list[dict]:
        return [{"strikePrice": k, "callSymbolId": option_id(e, k, "C"), "putSymbolId": option_id(e, k, "P")}
                for k in STRIKES]
    return {"optionChain": [
        {"expiryDate": f"{e}T00:00:00.000000-05:00", "description": "XYZ CORP", "listingExchange": "OPRA",
         "optionExerciseType": "American",
         "chainPerRoot": [{"optionRoot": "XYZ", "chainPerStrikePrice": per_strike(e), "multiplier": 100},
                          {"optionRoot": "XYZ1", "chainPerStrikePrice": [
                              {"strikePrice": 100, "callSymbolId": 9_000_001, "putSymbolId": 9_000_002}],
                           "multiplier": 100}]}
        for e in EXPIRIES]}


class FakeQuestrade:
    def __init__(self, valid_tokens: tuple[str, ...] = ("pasted-token",), delay: int = 0, expires_in: int = 1800):
        self.valid = set(valid_tokens)
        self.delay, self.expires_in = delay, expires_in
        self.n = 0
        self.access: str | None = None
        self.requests: list[httpx.Request] = []
        self.logins: list[str] = []            # refresh tokens presented to the token endpoint
        self.queue: dict[str, list[httpx.Response]] = {}   # path -> responses served before the normal one
        self.headers: dict[str, str] = {}
        self.unquoted = {option_id("2026-11-20", 100, "P")}   # listed, no quote
        self.no_vol = {option_id("2026-11-20", 90, "C")}      # quoted, volatility 0
        self.scope_denied = False   # the personal app lacks the market-data permission: 403 code 1016 on every call
        self.stream_port = 0        # what a `stream=true` quote request answers (live/stream.py)
        self.price = 100.2          # XYZ's last trade

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def path_count(self, path: str) -> int:
        return sum(1 for r in self.requests if r.url.path == path)

    # ------------------------------------------------------------------ handler
    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if self.queue.get(path):
            return self.queue[path].pop(0)
        if request.url.host == "login.questrade.com":
            return self._login(request)
        if request.headers.get("Authorization") != f"Bearer {self.access}":
            return httpx.Response(401, json={"code": 1017, "message": "Access token is invalid"})
        if self.scope_denied:
            return httpx.Response(403, json={"code": 1016, "message": "Request is out of allowed OAuth scopes"})
        h = self.headers
        if path == "/v1/symbols/search":
            prefix = request.url.params["prefix"]
            hits = [{"symbol": prefix, "symbolId": 1000 if prefix == "XYZ" else 1001, "securityType": "Stock",
                     "currency": "USD", "listingExchange": "NYSE", "isQuotable": True, "isTradable": True}] \
                if prefix in ("XYZ", "SPY") else []
            return httpx.Response(200, json={"symbols": hits}, headers=h)
        if path == "/v1/symbols/1000":
            return httpx.Response(200, json={"symbols": [{"symbol": "XYZ", "symbolId": 1000, "hasOptions": True,
                                                          "prevDayClosePrice": 98.0}]}, headers=h)
        if path == "/v1/markets/quotes":
            if request.url.params.get("stream") == "true":
                return httpx.Response(200, json={"streamPort": self.stream_port}, headers=h)
            ids = [int(i) for i in request.url.params.get("ids", "1000").split(",")]
            return httpx.Response(200, json={"quotes": [self.equity_quote()] if 1000 in ids else []}, headers=h)
        if path == "/v1/symbols/1000/options":
            return httpx.Response(200, json=_chain(), headers=h)
        if path == "/v1/markets/quotes/options" and request.method == "POST":
            body = json.loads(request.content)
            if body.get("stream"):
                return httpx.Response(200, json={"streamPort": self.stream_port}, headers=h)
            return httpx.Response(200, json={"optionQuotes": self._option_quotes(body)}, headers=h)
        return httpx.Response(404, json={"code": 1001, "message": "Invalid endpoint."})

    def _login(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        token = form.get("refresh_token", [""])[0]
        self.logins.append(token)
        if form.get("grant_type") != ["refresh_token"] or token not in self.valid:
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.valid.discard(token)                       # single use
        self.n += 1
        new = f"rotated-{self.n}"
        self.valid.add(new)
        self.access = f"access-{self.n}"
        return httpx.Response(200, json={"access_token": self.access, "token_type": "Bearer",
                                         "expires_in": self.expires_in, "refresh_token": new,
                                         "api_server": f"{API}/"})

    def equity_quote(self) -> dict:
        return {"symbol": "XYZ", "symbolId": 1000, "bidPrice": 99.9, "askPrice": 100.1, "lastTradePriceTrHrs": 100.0,
                "lastTradePrice": self.price, "volume": 1_000_000, "openPrice": 98.5, "highPrice": 101.0,
                "lowPrice": 98.0, "delay": bool(self.delay), "isHalted": False}

    def _option_quotes(self, body: dict) -> list[dict]:
        wanted = [(f["expiryDate"][:10], k, right) for f in body.get("filters") or [] for k in STRIKES
                  if f["minstrikePrice"] <= k <= f["maxstrikePrice"] for right in ("C", "P")]
        by_id = {option_id(e, k, r): (e, k, r) for e in EXPIRIES for k in STRIKES for r in ("C", "P")}
        wanted += [by_id[i] for i in body.get("optionIds") or [] if i in by_id]
        return [self.option_quote(*w) for w in wanted if option_id(*w) not in self.unquoted]

    def option_quote(self, expiry: str, k: int, right: str) -> dict:
        sid = option_id(expiry, k, right)
        itm = max(0.0, (100 - k) if right == "C" else (k - 100))
        return {"underlying": "XYZ", "underlyingId": 1000, "symbol": f"XYZ{expiry}{right}{k}", "symbolId": sid,
                "bidPrice": round(itm + 1.0, 2), "askPrice": round(itm + 1.2, 2), "lastTradePrice": round(itm + 1.1, 2),
                "lastTradeTime": "2026-11-19T15:00:00.000000-05:00", "volume": 10, "openInterest": 100,
                "volatility": 0 if sid in self.no_vol else 31.5, "delta": 0.5 if right == "C" else -0.5,
                "gamma": 0.02, "theta": -0.05, "vega": 0.1, "rho": 0.01, "delay": self.delay, "isHalted": False}
