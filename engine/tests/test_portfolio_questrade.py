"""Questrade accounts (read-only): the account parsers on the documented shapes, the holdings they become (OSI codes,
currencies, masked accounts), the FX rate the combined balances imply, and a missing account permission (403 code
1016 on GET v1/accounts) classified on its own: it never flags the token, so market data keeps working."""
from __future__ import annotations

import pytest

from optionlens.portfolio.model import account_id, mask_account
from optionlens.sources.questrade import account_mapping as m
from optionlens.sources.questrade.accounts import AccountAccessMissing, QuestradeAccounts
from optionlens.sources.questrade.errors import ACCOUNT_ACCESS, QuestradeError, scope_missing
from optionlens.sources.questrade.source import QuestradeSource

from .questrade_fake import FakeQuestrade, fixture

NUMBER = "26598145"


# ----------------------------------------------------------------------------- parsers (doc-shaped fixtures)
def test_accounts_doc_sample():
    accts = m.parse_accounts(fixture("doc_accounts.json"))
    assert [(a.number, a.type, a.status) for a in accts] == [(NUMBER, "Margin", "Active")]
    assert m.parse_accounts({"accounts": [{"number": "1", "status": "Closed"}, {"type": "Cash"}]}) == []
    assert m.parse_accounts(None) == [] and m.parse_accounts({"accounts": None}) == []


def test_positions_doc_sample_and_spellings():
    doc = m.parse_positions(fixture("doc_accounts_positions.json"))
    assert [(p.symbol, p.symbol_id, p.qty, p.avg_price, p.current_price, p.day_pnl) for p in doc] == \
        [("THI.TO", 38738, 100.0, 60.23, 60.17, None)]       # the sample's "totalCost": false is not read
    us = m.parse_positions(fixture("accounts_positions_us.json"))
    assert [p.symbol for p in us] == ["XYZ", "XYZ18Dec26C110.00"]          # closed today (openQuantity 0): dropped
    assert us[0].day_pnl == 440 and us[1].qty == -2 and us[1].avg_price == 2.1


def test_balances_and_the_implied_fx_rate():
    per, combined = m.parse_balances(fixture("doc_accounts_balances.json"))
    assert [(b.currency, b.net_liquidation, b.cash, b.buying_power) for b in per] == [
        ("CAD", 249988.7, 243971.7, 496367.2), ("USD", 252004.05, 198259.05, 461013.3)]
    fx = m.implied_usdcad(combined, None)
    assert (fx.base, fx.quote) == ("USD", "CAD") and fx.rate == pytest.approx(597754.29 / 433155.28)
    assert "combined CAD ÷ combined USD" in fx.source
    assert m.implied_usdcad(per[:1], None) is None                       # one side only: no rate


def test_symbol_details_and_holdings():
    syms = m.parse_symbols(fixture("symbols_ids.json"))
    call = syms[2030220]
    assert (call.is_option, call.root, call.underlying, str(call.expiry), call.right, call.strike, call.multiplier) == \
        (True, "XYZ", "XYZ", "2026-12-18", "C", 110.0, 100.0)
    acct = m.parse_accounts(fixture("doc_accounts.json"))[0]
    pos = m.parse_positions(fixture("doc_accounts_positions.json")) + m.parse_positions(fixture("accounts_positions_us.json"))
    hs = {h.symbol: h for h in m.holdings(acct, pos, syms)}
    assert set(hs) == {"THI.TO", "XYZ", "XYZ261218C00110000"}
    opt = hs["XYZ261218C00110000"]
    assert (opt.kind, opt.underlying, opt.osi, opt.qty, opt.multiplier, opt.avg_cost, opt.currency, opt.prev_close) == \
        ("option", "XYZ", "XYZ261218C00110000", -2.0, 100.0, 2.1, "USD", 1.4)
    assert hs["THI.TO"].currency == "CAD" and hs["XYZ"].currency == "USD" and hs["XYZ"].broker_day_pnl == 440
    for h in hs.values():                                    # the number is masked; the id does not reveal it
        assert h.account == "Margin •••145" and NUMBER not in h.account_id and NUMBER not in h.account
        assert h.account_id == account_id("questrade", NUMBER) != account_id("ibkr", NUMBER)


def test_montreal_exchange_option_position_is_coded_under_its_canadian_underlying():
    syms = m.parse_symbols({"symbols": [{
        "symbol": "RY18Dec26C120.00.MX", "symbolId": 77, "currency": "CAD", "prevDayClosePrice": 3.1,
        "optionType": "Call", "optionRoot": "RY", "optionExpiryDate": "2026-12-18T00:00:00.000000-05:00",
        "optionStrikePrice": 120, "securityType": "Option",
        "optionContractDeliverables": {"underlyings": [{"multiplier": 100, "underlyingSymbol": "RY.TO"}]}}]})
    acct = m.QAccount("123456", "TFSA", "Active")
    (h,) = m.holdings(acct, [m.QPosition("RY18Dec26C120.00.MX", 77, 1, 3.0, 3.2, None)], syms)
    assert (h.underlying, h.osi, h.currency) == ("RY.TO", "RY.TO261218C00120000", "CAD")   # never OPRA's RY261218C...


def test_currency_without_symbol_details():
    acct = m.QAccount("123456", "TFSA", "Active")
    hs = m.holdings(acct, [m.QPosition("SHOP.TO", None, 10, 100.0, 110.0, None),
                           m.QPosition("AAPL", None, 5, 180.0, 190.0, None)], {})
    assert [(h.symbol, h.currency) for h in hs] == [("SHOP.TO", "CAD"), ("AAPL", "USD")]


def test_masking():
    assert mask_account("26598145") == "•••145" and mask_account("U1234567") == "•••567" and mask_account("12") == "•••"


# ----------------------------------------------------------------------------- calls and the missing permission
@pytest.fixture
def qt(tmp_path):
    fake = FakeQuestrade()
    src = QuestradeSource(tmp_path, 8, 0.25, fake.http)
    return fake, src, QuestradeAccounts(src.tokens, src.api, fake.http)


async def test_snapshot_reads_accounts_positions_balances_only_with_get(qt):
    fake, src, accounts = qt
    await src.tokens.connect("pasted-token")
    snap = await accounts.snapshot()
    assert snap.broker == "questrade" and len(snap.accounts) == 1 and len(snap.holdings) == 3
    assert snap.fx and snap.fx[0].quote == "CAD"
    account_calls = [r for r in fake.requests if r.url.path.startswith("/v1/accounts")]
    assert {r.method for r in account_calls} == {"GET"} and len(account_calls) == 3
    await accounts.snapshot()                                   # symbol details are cached for the day
    assert fake.path_count("/v1/symbols") == 1


async def test_missing_account_permission_does_not_flag_the_token(qt):
    fake, src, accounts = qt
    await src.tokens.connect("pasted-token")
    fake.account_scope_denied = True
    with pytest.raises(AccountAccessMissing) as e:
        await accounts.snapshot()
    assert str(e.value) == ACCOUNT_ACCESS and "Retrieve balances, positions" in str(e.value)
    assert src.tokens.state == "connected" and src.unavailable() is None      # not a market-data failure
    assert (await src.api.get("symbols/search", {"prefix": "XYZ"}))["symbols"]   # market data keeps working
    chain = await src.fetch_chain("XYZ")
    assert chain.contracts


async def test_a_market_data_403_still_flags(qt):
    fake, src, _ = qt
    await src.tokens.connect("pasted-token")
    fake.scope_denied = True
    with pytest.raises(QuestradeError):
        await src.api.get("symbols/search", {"prefix": "XYZ"})
    assert src.tokens.state == "needs_attention"


async def test_errors_never_name_the_account_number(qt):
    import httpx
    fake, src, accounts = qt
    await src.tokens.connect("pasted-token")
    fake.queue[f"/v1/accounts/{NUMBER}/positions"] = [httpx.Response(404, json={"code": 1018, "message": "nope"})]
    with pytest.raises(QuestradeError) as e:
        await accounts.snapshot()
    assert NUMBER not in str(e.value) and "•••145" in str(e.value)


def test_scope_classification():
    assert scope_missing(403, 1016) and scope_missing(403, None)
    assert not scope_missing(401, 1017) and not scope_missing(404, 1018)
