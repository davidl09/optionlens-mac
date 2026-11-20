"""Alert evaluation, delivery and digests (SPEC §9).

Every AlertKind is evaluated against the universe frame (one row per symbol). Scope: the alert's
symbol, or every symbol of params.watchlist_id. Cooldown is per alert per symbol
(repo.user.alert_fired_at / record_alert_fire). Delivery: in-app notification always; ntfy POST to
NTFY_URL and SMTP email when configured (both in a background thread, failures logged).

`register_hooks()` wires evaluation, report snapshots and the digests into the job hooks and adds
the 08:00 morning digest job to the scheduler (call once at startup, before scheduler.start()).
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from typing import Callable

import httpx
import numpy as np

from ..analytics.calendar import now_ny
from ..config import get_settings
from ..jobs import hooks
from ..repo import report_snapshots as snaps_repo
from ..repo import user as user_repo
from .reports import registry, universe
from .reports.earnings import CAL_ID
from .reports.options import UOV_ID
from .reports.universe import Frame

log = logging.getLogger("optionlens.alerts")


# ============================================================================ conditions
@dataclass(frozen=True)
class Rule:
    column: str | None
    test: Callable[[float, float], bool]
    describe: Callable[[float, float], str]


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _vol(x: float) -> str:
    return f"{x * 100:.1f}"


RULES: dict[str, Rule] = {
    "price_above": Rule("price", lambda v, x: v > x, lambda v, x: f"price {v:.2f} above {x:g}"),
    "price_below": Rule("price", lambda v, x: v < x, lambda v, x: f"price {v:.2f} below {x:g}"),
    "pct_change_abs": Rule("change_pct", lambda v, x: abs(v) >= x, lambda v, x: f"moved {_pct(v)} today (|chg| ≥ {_pct(x)})"),
    "iv30_above": Rule("iv30", lambda v, x: v > x, lambda v, x: f"IV30 {_vol(v)} above {_vol(x)}"),
    "iv30_below": Rule("iv30", lambda v, x: v < x, lambda v, x: f"IV30 {_vol(v)} below {_vol(x)}"),
    "iv_rank_above": Rule("iv_rank", lambda v, x: v > x, lambda v, x: f"IV rank {_pct(v)} above {_pct(x)}"),
    "iv_rank_below": Rule("iv_rank", lambda v, x: v < x, lambda v, x: f"IV rank {_pct(v)} below {_pct(x)}"),
    "rel_option_volume_above": Rule("rel_option_volume", lambda v, x: v >= x,
                                    lambda v, x: f"option volume {v:.1f}× its 20-day average (≥ {x:g}×)"),
    "earnings_within_days": Rule("days_to_earnings", lambda v, x: 0 <= v <= x,
                                 lambda v, x: f"earnings in {v:.0f} day(s)"),
    "ex_div_within_days": Rule("days_to_ex_div", lambda v, x: 0 <= v <= x, lambda v, x: f"ex-dividend in {v:.0f} day(s)"),
    "unusual_volume": Rule(None, lambda v, x: True, lambda v, x: "listed in Unusual Option Volume"),
}


def _targets(alert: dict) -> list[str]:
    if alert.get("symbol"):
        return [alert["symbol"]]
    wid = (alert.get("params") or {}).get("watchlist_id")
    return user_repo.watchlist_symbols(int(wid)) if wid is not None else []


def _unusual_symbols() -> set[str]:
    return {r["symbol"] for r in registry.run_sync(UOV_ID)["rows"]}


def matches(alert: dict, f: Frame, unusual: set[str] | None = None) -> list[tuple[str, str]]:
    """[(symbol, description)] of the alert's scope symbols whose condition holds now."""
    rule = RULES[alert["kind"]]
    x = (alert.get("params") or {}).get("value")
    pos = f.index()
    out = []
    for sym in _targets(alert):
        if rule.column is None:
            if sym in (unusual if unusual is not None else _unusual_symbols()):
                out.append((sym, rule.describe(0, 0)))
            continue
        i = pos.get(sym)
        if i is None or x is None:
            continue
        v = float(f[rule.column][i])
        if np.isfinite(v) and rule.test(v, float(x)):
            out.append((sym, rule.describe(v, float(x))))
    return out


def evaluate(day: date | None = None, symbols: list[str] | None = None, *, now: datetime | None = None,
             frame: Frame | None = None) -> list[tuple[int, str]]:
    """Evaluate every enabled alert; deliver and record fires outside their cooldown.
    Hook signature fn(day, symbols). Returns [(alert_id, symbol)] fired."""
    now = now or datetime.now(timezone.utc)
    alerts = user_repo.list_alerts(enabled=True)
    if not alerts:
        return []
    f = frame or universe.get_frame()
    unusual = _unusual_symbols() if any(a["kind"] == "unusual_volume" for a in alerts) else set()
    fired = []
    for a in alerts:
        last = user_repo.alert_fired_at(a["id"])
        cooldown = timedelta(minutes=int(a.get("cooldown_min") or 0))
        for sym, desc in matches(a, f, unusual):
            t = last.get(sym)
            if t is not None and now - t < cooldown:
                continue
            deliver(f"{sym}: {desc}", _body(sym, desc, f), f"/s/{sym}", a["id"], now)
            user_repo.record_alert_fire(a["id"], sym, now)
            fired.append((a["id"], sym))
    return fired


def _body(sym: str, desc: str, f: Frame) -> str:
    i = f.index().get(sym)
    if i is None:
        return desc
    px, ch, iv = f["price"][i], f["change_pct"][i], f["iv30"][i]
    bits = [desc]
    if np.isfinite(px):
        bits.append(f"last {px:.2f}" + (f" ({ch * 100:+.2f}%)" if np.isfinite(ch) else ""))
    if np.isfinite(iv):
        bits.append(f"IV30 {iv * 100:.1f}")
    return " · ".join(bits)


# ============================================================================ delivery
def deliver(title: str, body: str, link: str | None = None, alert_id: int | None = None,
            ts: datetime | None = None) -> int:
    """In-app notification (always) + ntfy / SMTP when configured (background thread). Returns the id."""
    nid = user_repo.add_notification(title, body, link, alert_id, ts)
    s = get_settings()
    if s.ntfy_url or (s.smtp_host and s.smtp_to):
        threading.Thread(target=_send_external, args=(title, body, link), daemon=True, name="alert-delivery").start()
    return nid


def _send_external(title: str, body: str, link: str | None) -> None:
    s = get_settings()
    if s.ntfy_url:
        try:
            headers = {"Title": title.encode("ascii", "replace").decode()}
            httpx.post(s.ntfy_url, content=body.encode(), headers=headers, timeout=10).raise_for_status()
        except Exception as e:  # noqa: BLE001 - delivery is best effort
            log.warning("ntfy delivery failed: %s", e)
    if s.smtp_host and s.smtp_to:
        try:
            msg = EmailMessage()
            msg["Subject"] = f"OptionLens: {title}"
            msg["From"] = s.smtp_from or s.smtp_user or "optionlens@localhost"
            msg["To"] = s.smtp_to
            msg.set_content(body + (f"\n\n{link}" if link else ""))
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
                if s.smtp_port in (587, 25):
                    try:
                        smtp.starttls()
                    except smtplib.SMTPException:
                        pass
                if s.smtp_user and s.smtp_password:
                    smtp.login(s.smtp_user, s.smtp_password)
                smtp.send_message(msg)
        except Exception as e:  # noqa: BLE001
            log.warning("smtp delivery failed: %s", e)


# ============================================================================ EOD: snapshots + digest
def store_snapshots(day: date, _symbols: list[str] | None = None) -> dict[str, int]:
    """report_snapshots for `day`: unusual-option-volume rows (lookback history) and earnings-calendar
    rows (implied moves before each event, used by earnings-results)."""
    out = {}
    for rid in (UOV_ID, CAL_ID):
        rows = [r for r in registry.run_sync(rid)["rows"] if r.get("symbol")]
        out[rid] = snaps_repo.write_snapshots(day, rid, rows)
    return out


def _fmt_rows(rows: list[dict], fn: Callable[[dict], str], n: int) -> str:
    return ", ".join(fn(r) for r in rows[:n]) or "none"


def _uov_label(r: dict) -> str:
    """Relative option volume when an average exists, else the interim volume/OI criterion."""
    if r.get("rel_volume") is not None:
        return f"{r['symbol']} {r['rel_volume']:.1f}×"
    if r.get("vol_oi") is not None:
        return f"{r['symbol']} {r['vol_oi']:.2f}× OI"
    return r["symbol"]


def _iv_chg_label(r: dict) -> str:
    chg = r.get("iv30_chg_1d")
    return r["symbol"] if chg is None else f"{r['symbol']} {chg * 100:+.1f}"


def evening_digest(day: date, _symbols: list[str] | None = None) -> int:
    """16:45-style digest notification: unusual volume top 10, IV movers, watchlist summary."""
    uov = registry.run_sync(UOV_ID)["rows"]
    up = registry.run_sync("iv-movers", {"side": "up"})["rows"]
    down = registry.run_sync("iv-movers", {"side": "down"})["rows"]
    lines = [
        "Unusual option volume: " + _fmt_rows(uov, _uov_label, 10),
        "IV up: " + _fmt_rows(up, _iv_chg_label, 5),
        "IV down: " + _fmt_rows(down, _iv_chg_label, 5),
    ]
    f = universe.get_frame()
    pos = f.index()
    for w in user_repo.list_watchlists():
        parts = []
        for s in w["symbols"][:20]:
            i = pos.get(s)
            if i is not None and np.isfinite(f["change_pct"][i]):
                parts.append(f"{s} {f['change_pct'][i] * 100:+.1f}%")
        if parts:
            lines.append(f"{w['name']}: " + ", ".join(parts))
    return deliver(f"End-of-day digest {day.isoformat()}", "\n".join(lines), "/")


def on_eod(day: date, symbols: list[str]) -> None:
    """on_eod hook: fresh frame, snapshots, alerts, digest (each step isolated)."""
    universe.invalidate()
    for step in (store_snapshots, evaluate, evening_digest):
        try:
            step(day, symbols)
        except Exception:  # noqa: BLE001 - one failing step must not skip the others
            log.exception("eod step %s failed", step.__name__)


def on_sweep(day: date, symbols: list[str]) -> None:
    """on_sweep_complete hook: rebuild the frame, then evaluate alerts."""
    universe.invalidate()
    evaluate(day, symbols)


# ============================================================================ morning digest
async def morning_digest() -> str:
    """08:00 digest: today's earnings (with implied moves) and top pre-market movers from watchlists."""
    today = now_ny().date()
    cal = await registry.run_report(CAL_ID, {"start": today.isoformat(), "end": today.isoformat()})
    lines = ["Earnings today: " + _fmt_rows(
        cal["rows"], lambda r: f"{r['symbol']} {r['time']}" + (f" ±{r['implied_move'] * 100:.1f}%"
                                                                if r.get("implied_move") else ""), 15)]
    wl = await asyncio.to_thread(user_repo.watchlist_symbols)
    if wl:
        pre = await registry.run_report("premarket-movers", {"symbols": ",".join(wl)})
        lines.append("Pre-market (watchlists): " + _fmt_rows(
            [r for r in pre["rows"] if r.get("ext_change_pct") is not None],
            lambda r: f"{r['symbol']} {r['ext_change_pct'] * 100:+.1f}%", 8))
    await asyncio.to_thread(deliver, f"Morning digest {today.isoformat()}", "\n".join(lines),
                            "/reports/earnings-calendar")
    return f"{len(cal['rows'])} earnings, {len(wl)} watchlist symbols"


MORNING_DIGEST_AT = time(8, 0)
_registered = False


def register_hooks() -> None:
    """Wire alerts/snapshots/digests into jobs.hooks and add the 08:00 'digest_morning' scheduler job
    (idempotent; call before scheduler.start())."""
    global _registered
    if _registered:
        return
    _registered = True
    hooks.on_sweep_complete.append(on_sweep)
    hooks.on_eod.append(on_eod)
    from ..jobs import scheduler
    scheduler.JOBS.setdefault("digest_morning", scheduler.Job(
        "digest_morning", morning_digest, scheduler.daily(MORNING_DIGEST_AT, trading_only=True), catch_up=False))
