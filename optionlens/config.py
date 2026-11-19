"""Settings from environment variables `OPTIONLENS_*` (read once, immutable, validated: a bad value
raises ValueError naming the variable at startup)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")

PACKAGE_DIR = Path(__file__).resolve().parent


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(f"OPTIONLENS_{name}")
    return v if v not in (None, "") else default


def _num(name: str, default: str, cast: Callable[[str], T]) -> T:
    raw = _env(name, default)
    try:
        return cast(raw)
    except ValueError:
        raise ValueError(f"OPTIONLENS_{name}={raw!r} is not a valid {cast.__name__}") from None


# numeric settings that schedules/limiters divide by or loop on: must be > 0
_POSITIVE = ("port", "sweep_interval_min", "option_eod_retention_days", "cboe_concurrency", "cboe_rps",
             "nasdaq_rps", "smtp_port")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    host: str
    port: int
    universe_stocks: int
    etf_file: Path
    sweep_interval_min: int
    option_eod_retention_days: int
    risk_free_rate: float
    cboe_concurrency: int
    cboe_rps: float
    nasdaq_rps: float
    ntfy_url: str | None
    smtp_host: str | None
    smtp_port: int
    smtp_user: str | None
    smtp_password: str | None
    smtp_from: str | None
    smtp_to: str | None
    tz: str = "America/New_York"

    def __post_init__(self) -> None:
        bad = [f"OPTIONLENS_{n.upper()}={getattr(self, n)} must be > 0" for n in _POSITIVE if getattr(self, n) <= 0]
        if self.universe_stocks < 0:
            bad.append(f"OPTIONLENS_UNIVERSE_STOCKS={self.universe_stocks} must be >= 0")
        if bad:
            raise ValueError("invalid settings: " + "; ".join(bad))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "optionlens.duckdb"


def load_settings() -> Settings:
    """Build Settings from the current environment."""
    return Settings(
        data_dir=Path(_env("DATA_DIR", "~/.local/share/optionlens")).expanduser(),
        host=_env("HOST", "127.0.0.1"),
        port=_num("PORT", "8473", int),
        universe_stocks=_num("UNIVERSE_STOCKS", "1000", int),
        etf_file=Path(_env("ETF_FILE", str(PACKAGE_DIR / "etfs.txt"))).expanduser(),
        sweep_interval_min=_num("SWEEP_INTERVAL_MIN", "30", int),
        option_eod_retention_days=_num("OPTION_EOD_RETENTION_DAYS", "730", int),
        risk_free_rate=_num("RISK_FREE_RATE", "0.04", float),
        cboe_concurrency=_num("CBOE_CONCURRENCY", "3", int),
        cboe_rps=_num("CBOE_RPS", "3", float),
        nasdaq_rps=_num("NASDAQ_RPS", "4", float),
        ntfy_url=_env("NTFY_URL"),
        smtp_host=_env("SMTP_HOST"),
        smtp_port=_num("SMTP_PORT", "587", int),
        smtp_user=_env("SMTP_USER"),
        smtp_password=_env("SMTP_PASSWORD"),
        smtp_from=_env("SMTP_FROM"),
        smtp_to=_env("SMTP_TO"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings (cached; tests call get_settings.cache_clear())."""
    return load_settings()
