"""The user's Questrade accounts, positions and balances (read-only; docs/INTEGRATION.md §8 "Accounts").

Calls (all GET; OptionLens has no order or trading call anywhere):
  GET v1/accounts                    the accounts (number, type, status)
  GET v1/accounts/:id/positions      open positions
  GET v1/accounts/:id/balances       per-currency and combined balances (FX rate)
  GET v1/symbols?ids=…               (market data) currency, previous close and option terms of the held symbols,
                                     once per symbol per New York day

They need the personal app's permission "Retrieve balances, positions, orders and executions". Without it Questrade
answers 403 code 1016 ("out of allowed OAuth scopes"): AccountAccessMissing, and — unlike a market-data 403 — the
token is NOT flagged (client.flags_forbidden=False), so chains and live quotes keep working. Account calls are paced
by their own documented limit (30/s, 30,000/h; 70% of it). Account numbers appear only in the request paths: errors
raised from here name the path with the number masked, and nothing here logs."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable

import httpx

from ...analytics.calendar import now_ny
from ...portfolio.model import BrokerSnapshot, mask_account
from . import account_mapping as m
from .auth import TokenManager
from .client import QuestradeClient
from .errors import ACCOUNT_ACCESS, QuestradeError, scope_missing
from .ratelimit import RateGate

ACCOUNT_PER_SECOND, ACCOUNT_PER_HOUR = 30, 30_000
SYMBOLS_PER_CALL = 100


class AccountAccessMissing(Exception):
    """The personal app lacks the account permission (403 code 1016 on GET v1/accounts)."""

    def __init__(self) -> None:
        super().__init__(ACCOUNT_ACCESS)


class QuestradeAccounts:
    def __init__(self, tokens: TokenManager, market: QuestradeClient, http: Callable[[], httpx.AsyncClient],
                 gate: RateGate | None = None):
        self.api = QuestradeClient(tokens, gate or RateGate(ACCOUNT_PER_SECOND, ACCOUNT_PER_HOUR), http,
                                   flags_forbidden=False)
        self.market = market
        self._symbols: dict[int, tuple[date, m.QSymbol]] = {}

    async def _get(self, path: str, label: str, params: dict | None = None, market: bool = False) -> Any:
        """An account call (or with `market`, a market-data call) whose errors name `label`, not the path."""
        try:
            return await (self.market if market else self.api).get(path, params)
        except QuestradeError as e:
            if not market and scope_missing(e.status, e.code):
                raise AccountAccessMissing() from None
            raise QuestradeError(f"questrade:{label}", e.status, e.code, e.message, e.kind, e.reason) from None

    async def check(self) -> None:
        """One GET v1/accounts: raises AccountAccessMissing when the permission is missing (QuestradeError /
        TokenError otherwise)."""
        await self._get("accounts", "accounts")

    async def snapshot(self) -> BrokerSnapshot:
        """Every open account's positions and balances. Raises AccountAccessMissing, QuestradeError, TokenError."""
        accounts = m.parse_accounts(await self._get("accounts", "accounts"))
        fetched = datetime.now(timezone.utc)
        out_accounts, raw, fx = [], [], None
        for a in accounts:
            masked = mask_account(a.number)
            positions = m.parse_positions(await self._get(f"accounts/{a.number}/positions",
                                                          f"accounts/{masked}/positions"))
            per, combined = m.parse_balances(await self._get(f"accounts/{a.number}/balances",
                                                             f"accounts/{masked}/balances"))
            out_accounts.append(m.account(a, per))
            raw.append((a, positions))
            fx = fx or m.implied_usdcad(combined, fetched)
        symbols = await self._symbol_info({p.symbol_id for _, ps in raw for p in ps if p.symbol_id is not None})
        holdings = tuple(h for a, ps in raw for h in m.holdings(a, ps, symbols))
        return BrokerSnapshot("questrade", fetched, tuple(out_accounts), holdings, (fx,) if fx else ())

    async def _symbol_info(self, ids: set[int]) -> dict[int, m.QSymbol]:
        today = now_ny().date()
        missing = sorted(i for i in ids if self._symbols.get(i, (None,))[0] != today)
        for k in range(0, len(missing), SYMBOLS_PER_CALL):
            chunk = missing[k:k + SYMBOLS_PER_CALL]
            got = m.parse_symbols(await self._get("symbols", "symbols", {"ids": ",".join(map(str, chunk))},
                                                  market=True))
            for sid, info in got.items():
                self._symbols[sid] = (today, info)
        return {i: self._symbols[i][1] for i in ids if i in self._symbols}
