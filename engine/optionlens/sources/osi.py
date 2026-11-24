"""OSI option codes (e.g. AAPL261120C00100000) shared by every chain source. Pure functions."""
from __future__ import annotations

import re
from datetime import date

from ..models import Right

# OSI roots carrying standard 100-share / index-multiplier contracts. Everything else a venue lists in a
# chain (adjusted deliverables like AAPL1, B1SPY-style products, SPXO duplicates) is dropped at parse.
INDEX_ROOTS = {
    "SPX": frozenset({"SPX", "SPXW"}), "NDX": frozenset({"NDX", "NDXP"}), "RUT": frozenset({"RUT", "RUTW"}),
    "VIX": frozenset({"VIX", "VIXW"}),
}
# PM-settled roots of index weeklies: an option given only as (symbol, expiry, right, strike) gets this root.
PM_ROOTS = {"SPX": "SPXW", "NDX": "NDXP", "RUT": "RUTW"}
_OSI = re.compile(r"^([A-Z0-9.]+?)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


def standard_roots(symbol: str) -> frozenset[str]:
    """OSI roots of a symbol's standard contracts (BRK.B -> BRKB; SPX -> SPX, SPXW)."""
    return INDEX_ROOTS.get(symbol) or frozenset({symbol.replace(".", "")})


def parse_osi(osi: str) -> tuple[str, date, Right, float]:
    """OSI code -> (root, expiry, right, strike). Raises ValueError on malformed codes."""
    m = _OSI.match(osi)
    if not m:
        raise ValueError(f"bad OSI code: {osi!r}")
    root, yy, mm, dd, right, strike = m.groups()
    return root, date(2000 + int(yy), int(mm), int(dd)), right, int(strike) / 1000.0  # type: ignore[return-value]


def osi_code(root: str, expiry: date, right: str, strike: float) -> str:
    """OSI code of a contract of a known root: root + YYMMDD + C/P + round(strike x 1000) as 8 digits."""
    return f"{root}{expiry:%y%m%d}{right}{round(strike * 1000):08d}"


def format_osi(symbol: str, expiry: date, right: str, strike: float) -> str:
    """OSI code of a standard contract: root (PM root SPXW/NDXP/RUTW for SPX/NDX/RUT, else the symbol
    without dots) + YYMMDD + C/P + round(strike x 1000) as 8 digits."""
    return osi_code(PM_ROOTS.get(symbol, symbol.replace(".", "")), expiry, right, strike)
