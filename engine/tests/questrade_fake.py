"""An in-memory Questrade (login + the market calls OptionLens uses) behind httpx.MockTransport, with the JSON
shapes of questrade.com/api/documentation. One underlying, XYZ (symbolId 1000): expiries 2026-11-13 (expired),
11-20, 11-27, 12-18; strikes 70..130 step 10 on root XYZ, plus an adjusted root XYZ1 that must be dropped."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx

API = "https://api07.iq.questrade.com"
FIXTURES = Path(__file__).parent / "fixtures" / "questrade"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())
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
        self.stream_port = 0        # what a `stream=true` quote request answers (live/stream.py): one per session,
        self.option_stream_port: int | None = None   # for equities and options alike, unless this is set
        self.price = 100.2          # XYZ's last trade
        self.listed = {"XYZ": 1000, "SPY": 1001}   # symbols/search hits
        self.candle_cap = 2000      # candles per response (documented limit)
        self.candle_hours = (4, 20)  # ET hours candles exist (pre-market to after-hours)
        self.candle_until: datetime | None = None   # candles start before this only (the source's lag behind now)
        self.info_extra: dict = {}  # more GET v1/symbols/1000 fields (reference data)
        self.quote_extra: dict = {}  # more equity quote fields (VWAP, isHalted, lastTradeTime, ...)
        self.candle_vwap = False    # candles carry their own VWAP
        # Account calls (read-only): the doc samples, plus US positions on XYZ; without the app's account permission
        # every account call answers 403 code 1016 (market data unaffected).
        self.account_scope_denied = False
        self.accounts = fixture("doc_accounts.json")
        self.positions = {"26598145": {"positions": fixture("doc_accounts_positions.json")["positions"]
                                       + fixture("accounts_positions_us.json")["positions"]}}
        self.balances = fixture("doc_accounts_balances.json")
        self.symbol_details = {s["symbolId"]: s for s in fixture("symbols_ids.json")["symbols"]}

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
        if path.startswith("/v1/accounts"):
            return self._account(path, request.method)
        if path == "/v1/symbols":
            ids = [int(i) for i in request.url.params.get("ids", "").split(",") if i]
            return httpx.Response(200, json={"symbols": [self.symbol_details[i] for i in ids
                                                         if i in self.symbol_details]}, headers=h)
        if path == "/v1/symbols/search":
            prefix = request.url.params["prefix"]
            toronto = prefix.endswith(".TO")
            hits = [{"symbol": prefix, "symbolId": self.listed[prefix], "securityType": "Stock",
                     "currency": "CAD" if toronto else "USD", "listingExchange": "TSX" if toronto else "NYSE",
                     "isQuotable": True, "isTradable": True}] if prefix in self.listed else []
            return httpx.Response(200, json={"symbols": hits}, headers=h)
        if path == "/v1/symbols/1000":
            return httpx.Response(200, json={"symbols": [{"symbol": "XYZ", "symbolId": 1000, "hasOptions": True,
                                                          "prevDayClosePrice": 98.0, **self.info_extra}]}, headers=h)
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
                port = self.stream_port if self.option_stream_port is None else self.option_stream_port
                return httpx.Response(200, json={"streamPort": port}, headers=h)
            return httpx.Response(200, json={"optionQuotes": self._option_quotes(body)}, headers=h)
        if path.startswith("/v1/markets/candles/"):
            return httpx.Response(200, json={"candles": self.candles(request.url.params)}, headers=h)
        return httpx.Response(404, json={"code": 1001, "message": "Invalid endpoint."})

    def _account(self, path: str, method: str) -> httpx.Response:
        if method != "GET":
            return httpx.Response(405, json={"code": 1012, "message": "Method unsupported by endpoint"})
        if self.account_scope_denied:
            return httpx.Response(403, json={"code": 1016, "message": "Request is out of allowed OAuth scopes"})
        parts = path.split("/")          # ["", "v1", "accounts", number, what]
        if len(parts) == 3:
            return httpx.Response(200, json=self.accounts)
        number, what = parts[3], parts[4] if len(parts) > 4 else ""
        if number not in self.positions:
            return httpx.Response(404, json={"code": 1018, "message": "Account number not found"})
        if what == "positions":
            return httpx.Response(200, json=self.positions[number])
        if what == "balances":
            return httpx.Response(200, json=self.balances)
        return httpx.Response(404, json={"code": 1001, "message": "Invalid endpoint."})

    STEP = {"OneMinute": 60, "FiveMinutes": 300, "FifteenMinutes": 900, "OneHour": 3600, "OneDay": 86400}

    def candles(self, params) -> list[dict]:
        """Candles every interval from startTime to endTime on weekdays within candle_hours ET (EST, -05:00), at most
        candle_cap (the first ones), prices a slow ramp."""
        from optionlens.analytics.calendar import NY
        step = timedelta(seconds=self.STEP[params["interval"]])
        t, end = datetime.fromisoformat(params["startTime"]), datetime.fromisoformat(params["endTime"])
        t = datetime.fromtimestamp(-(-t.timestamp() // step.total_seconds()) * step.total_seconds(), timezone.utc)
        out = []
        if self.candle_until is not None:
            end = min(end, self.candle_until - timedelta(microseconds=1))
        daily = params["interval"] == "OneDay"      # one candle per weekday, starting at midnight ET
        if daily:
            t = datetime.fromisoformat(params["startTime"]).astimezone(NY).replace(hour=0, minute=0, second=0)
        while t <= end and len(out) < self.candle_cap:
            ny = t.astimezone(NY)
            if ny.weekday() < 5 and (daily or self.candle_hours[0] <= ny.hour < self.candle_hours[1]):
                p = 100 + (t.timestamp() % 86400) / 8640
                out.append({"start": ny.isoformat(timespec="microseconds"), "end": (ny + step).isoformat(timespec="microseconds"),
                            "low": round(p - 0.2, 2), "high": round(p + 0.3, 2), "open": round(p, 2),
                            "close": round(p + 0.1, 2), "volume": 1000,
                            **({"VWAP": round(p + 0.05, 2)} if self.candle_vwap else {})})
            t += step
        return out

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
                "lowPrice": 98.0, "delay": bool(self.delay), "isHalted": False, **self.quote_extra}

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
