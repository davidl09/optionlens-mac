"""Alert evaluation for every AlertKind with a fixed clock (per-alert-per-symbol cooldown, watchlist
scope), delivery channels, EOD snapshots/digests and hook registration."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from optionlens import config
from optionlens.jobs import hooks, scheduler
from optionlens.repo import report_snapshots as snaps_repo
from optionlens.repo import user as user_repo
from optionlens.services import alerts
from optionlens.services.reports import registry, universe

from .seed_reports import api, seeded  # noqa: F401 - fixtures

T0 = datetime(2030, 1, 2, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def clean(seeded):
    for a in user_repo.list_alerts():
        user_repo.delete_alert(a["id"])
    user_repo.mark_read()
    yield
    for a in user_repo.list_alerts():
        user_repo.delete_alert(a["id"])


def _frame_val(sym: str, key: str) -> float:
    f = universe.get_frame()
    return float(f[key][f.index()[sym]])


def _cases(seeded) -> list[tuple[str, float, float]]:
    """(kind, firing value, non-firing value) for AAPL on the seeded DB."""
    px, iv = _frame_val("AAPL", "price"), _frame_val("AAPL", "iv30")
    chg, ivr = abs(_frame_val("AAPL", "change_pct")), _frame_val("AAPL", "iv_rank")
    rel = _frame_val("AAPL", "rel_option_volume")
    de = _frame_val("AAPL", "days_to_earnings")
    dx = _frame_val("AAPL", "days_to_ex_div")
    return [("price_above", px - 1, px + 1), ("price_below", px + 1, px - 1),
            ("pct_change_abs", chg / 2, chg * 2 + 0.01), ("iv30_above", iv - 0.01, iv + 0.01),
            ("iv30_below", iv + 0.01, iv - 0.01), ("iv_rank_above", ivr - 0.01, ivr + 0.01),
            ("iv_rank_below", ivr + 0.01, ivr - 0.01), ("rel_option_volume_above", rel - 0.1, rel + 0.1),
            ("earnings_within_days", de, de - 1), ("ex_div_within_days", dx, dx - 1)]


def test_frame_has_alert_inputs(seeded):
    for k in ("iv_rank", "rel_option_volume", "days_to_earnings", "days_to_ex_div"):
        assert _frame_val("AAPL", k) == _frame_val("AAPL", k), k     # not NaN on the seeded DB


def test_every_kind_fires_and_respects_threshold(clean, seeded):
    cases = _cases(seeded)
    assert {c[0] for c in cases} | {"unusual_volume"} == set(alerts.RULES)
    for kind, hit, miss in cases:
        a = user_repo.create_alert(kind, "AAPL", {"value": hit}, True, 60)
        b = user_repo.create_alert(kind, "AAPL", {"value": miss}, True, 60)
        fired = alerts.evaluate(now=T0)
        assert (a["id"], "AAPL") in fired, kind
        assert all(aid != b["id"] for aid, _ in fired), kind
        user_repo.delete_alert(a["id"])
        user_repo.delete_alert(b["id"])
    u = user_repo.create_alert("unusual_volume", "AAPL", {}, True, 60)
    assert "AAPL" in {r["symbol"] for r in registry.run_sync("unusual-option-volume")["rows"]}
    assert alerts.evaluate(now=T0) == [(u["id"], "AAPL")]
    n = user_repo.list_notifications(unread_only=True)[0]
    assert n["alert_id"] == u["id"] and n["link"] == "/s/AAPL" and "Unusual" in n["title"]


def test_halted_and_resumed_alerts_follow_the_halt_flags_with_cooldown(clean, seeded):
    from optionlens.live import persist
    from optionlens.repo import halts

    wid = user_repo.create_watchlist("Halts", ["ZZZ"])["id"]
    a = user_repo.create_alert("halted", "AAPL", {}, True, 30)
    w = user_repo.create_alert("resumed", None, {"watchlist_id": wid}, True, 30)
    assert alerts.evaluate(now=T0) == []                                             # no halt observed
    assert halts.record([("AAPL", False, T0), ("ZZZ", True, T0)], "questrade") == ["ZZZ"]   # a first halt is news
    assert alerts.evaluate(now=T0) == []                    # AAPL trades; ZZZ is halted (no `halted` alert for it)
    assert halts.record([("AAPL", True, T0 + timedelta(minutes=1))], "questrade") == ["AAPL"]
    assert alerts.evaluate(now=T0 + timedelta(minutes=2), kinds=alerts.HALT_KINDS) == [(a["id"], "AAPL")]
    n = user_repo.list_notifications(unread_only=True)[0]
    assert n["title"].startswith("AAPL: trading halted at ") and n["link"] == "/s/AAPL"
    assert alerts.evaluate(now=T0 + timedelta(minutes=3)) == []                      # cooling down
    # live quotes resume ZZZ: persisting them records the change and reports it for on_halt_change
    at = (T0 + timedelta(minutes=4)).timestamp()
    assert persist.store([{"symbol": "ZZZ", "halted": False, "price": 50.0, "_at": at, "source": "questrade"}], []) == ["ZZZ"]
    assert alerts.evaluate(now=T0 + timedelta(minutes=5)) == [(w["id"], "ZZZ")]
    assert "trading resumed" in user_repo.list_notifications(unread_only=True)[0]["title"]
    # 35 min on: ZZZ resumed too long ago; AAPL is still halted and its cooldown is over: it fires again
    assert alerts.evaluate(now=T0 + timedelta(minutes=4 + 31)) == [(a["id"], "AAPL")]
    assert halts.record([("AAPL", True, T0)], "questrade") == []                     # older than stored: ignored
    assert alerts.evaluate(now=T0 + timedelta(hours=3)) == []                        # flags stale: unknown
    from optionlens.services import user as user_svc
    assert user_svc.create_alert("resumed", "AAPL")["kind"] == "resumed"                # valueless kinds
    user_repo.delete_watchlist(wid)


def test_cooldown_per_alert_per_symbol(clean, seeded):
    wid = seeded["watchlist_id"]
    a = user_repo.create_alert("price_above", None, {"value": 0.01, "watchlist_id": wid}, True, 30)
    assert sorted(alerts.evaluate(now=T0)) == [(a["id"], "AAPL"), (a["id"], "XYZ")]
    assert alerts.evaluate(now=T0 + timedelta(minutes=10)) == []                    # cooling down
    user_repo.record_alert_fire(a["id"], "XYZ", T0 - timedelta(hours=1))            # XYZ cooldown elapsed
    assert alerts.evaluate(now=T0 + timedelta(minutes=10)) == [(a["id"], "XYZ")]
    assert sorted(alerts.evaluate(now=T0 + timedelta(minutes=41))) == [(a["id"], "AAPL"), (a["id"], "XYZ")]
    assert user_repo.get_alert(a["id"])["last_fired_at"] == T0 + timedelta(minutes=41)
    user_repo.update_alert(a["id"], enabled=False)
    assert alerts.evaluate(now=T0 + timedelta(days=1)) == []                         # disabled alerts ignored


def test_external_delivery(monkeypatch, clean):
    monkeypatch.setenv("OPTIONLENS_NTFY_URL", "https://ntfy.example/topic")
    monkeypatch.setenv("OPTIONLENS_SMTP_HOST", "smtp.example")
    monkeypatch.setenv("OPTIONLENS_SMTP_TO", "me@example.com")
    config.get_settings.cache_clear()
    posts, mails = [], []

    def fake_post(url, content=None, headers=None, timeout=None):
        posts.append((url, content, headers))

        class R:
            def raise_for_status(self):
                return None
        return R()

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host = host

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            return None

        def login(self, *a):
            return None

        def send_message(self, msg):
            mails.append(msg)

    monkeypatch.setattr(alerts.httpx, "post", fake_post)
    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    started = []
    monkeypatch.setattr(alerts.threading, "Thread",
                        lambda target, args, **kw: type("T", (), {"start": lambda self: started.append(target(*args))})())
    try:
        nid = alerts.deliver("AAPL: price above 1", "body text", "/s/AAPL", None, T0)
    finally:
        config.get_settings.cache_clear()
    assert started and posts[0][0] == "https://ntfy.example/topic" and posts[0][1] == b"body text"
    assert posts[0][2]["Title"] == "AAPL: price above 1"
    assert mails[0]["To"] == "me@example.com" and "price above" in mails[0]["Subject"]
    assert nid in {n["id"] for n in user_repo.list_notifications()}


def test_eod_snapshots_digest_and_lookback(api, clean, seeded):
    day = seeded["day"]
    alerts.on_eod(day, ["AAPL", "XYZ"])
    assert {s["symbol"] for s in snaps_repo.read_snapshots("unusual-option-volume", day)} >= {"AAPL"}
    assert {s["symbol"] for s in snaps_repo.read_snapshots("earnings-calendar", day)} >= {"AAPL"}
    digest = user_repo.list_notifications(unread_only=True)[0]
    assert digest["title"].startswith("End-of-day digest") and "Unusual option volume: AAPL" in digest["body"]
    assert "Core:" in digest["body"]
    r = api.get("/api/reports/unusual-option-volume?lookback_days=5").json()
    assert {x["record_date"] for x in r["rows"]} >= {seeded["prev"].isoformat()}
    hist = [x for x in r["rows"] if x["record_date"] == seeded["prev"].isoformat()][0]
    assert hist["appearances"] >= 1 and hist["close"] and hist["next_close"]


def test_morning_digest(clean, seeded):
    msg = asyncio.run(alerts.morning_digest())
    n = user_repo.list_notifications(unread_only=True)[0]
    assert n["title"].startswith("Morning digest") and "Pre-market (watchlists): " in n["body"]
    assert "earnings" in msg


def test_register_hooks_is_idempotent():
    before = (list(hooks.on_sweep_complete), list(hooks.on_eod), dict(scheduler.JOBS), list(hooks.on_halt_change))
    try:
        alerts._registered = False
        alerts.register_hooks()
        alerts.register_hooks()
        assert hooks.on_sweep_complete.count(alerts.on_sweep) == 1 and hooks.on_eod.count(alerts.on_eod) == 1
        assert hooks.on_halt_change.count(alerts.on_halt_change) == 1
        job = scheduler.JOBS["digest_morning"]
        assert job.fn is alerts.morning_digest and job.catch_up is False
        assert scheduler.next_run_at("digest_morning").hour == 8
    finally:
        hooks.on_sweep_complete[:] = before[0]
        hooks.on_eod[:] = before[1]
        hooks.on_halt_change[:] = before[3]
        scheduler.JOBS.clear()
        scheduler.JOBS.update(before[2])
        alerts._registered = False


def test_digest_labels_tolerate_missing_history():
    """Day-1 rows have no relative volume / IV change yet: labels fall back, never crash."""
    from optionlens.services.alerts import _iv_chg_label, _uov_label

    assert _uov_label({"symbol": "AAA", "rel_volume": 3.04, "vol_oi": 0.5}) == "AAA 3.0×"
    assert _uov_label({"symbol": "AAA", "rel_volume": None, "vol_oi": 1.234}) == "AAA 1.23× OI"
    assert _uov_label({"symbol": "AAA", "rel_volume": None, "vol_oi": None}) == "AAA"
    assert _iv_chg_label({"symbol": "AAA", "iv30_chg_1d": None}) == "AAA"
    assert _iv_chg_label({"symbol": "AAA", "iv30_chg_1d": 0.021}) == "AAA +2.1"
