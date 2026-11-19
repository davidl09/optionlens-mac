"""Settings from environment variables `OPTIONLENS_*` (read once, immutable)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(f"OPTIONLENS_{name}")
    return v if v not in (None, "") else default


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
    nasdaq_rps: float
    ntfy_url: str | None
    smtp_host: str | None
    smtp_port: int
    smtp_user: str | None
    smtp_password: str | None
    smtp_from: str | None
    smtp_to: str | None
    tz: str = "America/New_York"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "optionlens.duckdb"


def load_settings() -> Settings:
    """Build Settings from the current environment."""
    return Settings(
        data_dir=Path(_env("DATA_DIR", "~/.local/share/optionlens")).expanduser(),
        host=_env("HOST", "127.0.0.1"),
        port=int(_env("PORT", "8765")),
        universe_stocks=int(_env("UNIVERSE_STOCKS", "1000")),
        etf_file=Path(_env("ETF_FILE", str(PACKAGE_DIR / "etfs.txt"))).expanduser(),
        sweep_interval_min=int(_env("SWEEP_INTERVAL_MIN", "30")),
        option_eod_retention_days=int(_env("OPTION_EOD_RETENTION_DAYS", "730")),
        risk_free_rate=float(_env("RISK_FREE_RATE", "0.04")),
        cboe_concurrency=int(_env("CBOE_CONCURRENCY", "8")),
        nasdaq_rps=float(_env("NASDAQ_RPS", "4")),
        ntfy_url=_env("NTFY_URL"),
        smtp_host=_env("SMTP_HOST"),
        smtp_port=int(_env("SMTP_PORT", "587")),
        smtp_user=_env("SMTP_USER"),
        smtp_password=_env("SMTP_PASSWORD"),
        smtp_from=_env("SMTP_FROM"),
        smtp_to=_env("SMTP_TO"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings (cached; tests call get_settings.cache_clear())."""
    return load_settings()
