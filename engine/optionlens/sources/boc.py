"""Bank of Canada Valet API -> the 3-month Government of Canada treasury bill yield (the CAD risk-free rate).

Series TB.CDN.90D.MID (verified 2026-11-24 at https://www.bankofcanada.ca/valet/series/TB.CDN.90D.MID/json): "Treasury
bills, 3-month — The mid-market yield for Government of Canada treasury bills with a 3-month maturity ... (V39065)",
unit Percent, business daily. Observations: `{"observations": [{"d": "2026-11-23", "TB.CDN.90D.MID": {"v": "2.65"}}]}`
(Valet terms: https://www.bankofcanada.ca/terms/, free to use with attribution)."""
from __future__ import annotations

from datetime import date
from typing import Any, Mapping

from .. import net

SERIES = "TB.CDN.90D.MID"
URL = f"https://www.bankofcanada.ca/valet/observations/{SERIES}/json"


def parse_observations(payload: Mapping[str, Any]) -> list[tuple[date, float]]:
    """Valet observations -> [(date, rate fraction)] ascending; blank / non-numeric values skipped. ValueError when
    the payload has no observations list."""
    obs = payload.get("observations")
    if not isinstance(obs, list):
        raise ValueError(f"Bank of Canada answer lacks observations: {list(payload)}")
    out = []
    for o in obs:
        v = (o.get(SERIES) or {}).get("v")
        try:
            out.append((date.fromisoformat(o["d"]), float(v) / 100.0))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out)


async def fetch_tbill_rates(start: date) -> list[tuple[date, float]]:
    """3-month T-bill yields from `start` to the latest published day."""
    return parse_observations(await net.get_json(URL, {"start_date": start.isoformat()}))
