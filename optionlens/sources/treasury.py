"""US Treasury daily bill rates CSV -> 13-week bill rate (coupon equivalent, as a fraction)."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime

from .. import net

URL = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
       "daily-treasury-rates.csv/{year}/all")
COLUMN = "13 WEEKS COUPON EQUIVALENT"


def parse_bill_csv(text: str) -> list[tuple[date, float]]:
    """CSV -> [(date, rate fraction)] ascending; rows with blank 13w values skipped. ValueError if column missing."""
    reader = csv.DictReader(io.StringIO(text))
    cols = {(c or "").strip().upper(): c for c in reader.fieldnames or []}
    if COLUMN not in cols or "DATE" not in cols:
        raise ValueError(f"treasury CSV lacks {COLUMN!r}: {list(cols)}")
    out = []
    for r in reader:
        v = (r.get(cols[COLUMN]) or "").strip()
        if not v or v.upper() == "N/A":
            continue
        out.append((datetime.strptime(r[cols["DATE"]].strip(), "%m/%d/%Y").date(), float(v) / 100.0))
    return sorted(out)


async def fetch_bill_rates(year: int) -> list[tuple[date, float]]:
    """13-week bill rates for a calendar year."""
    text = await net.get_text(URL.format(year=year), {
        "type": "daily_treasury_bill_rates", "field_tdr_date_value": year, "_format": "csv"})
    return parse_bill_csv(text)
