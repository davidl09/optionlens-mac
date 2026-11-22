"""Settings from environment variables `OPTIONLENS_*` (read once, immutable, validated: a bad value
raises ValueError naming the variable at startup).

The engine runs as the Mac app's local sidecar (docs/INTEGRATION.md §1): it binds loopback only, the
app passes a per-launch API token, data dir, port (0 = pick a free one) and its own pid."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")

PACKAGE_DIR = Path(__file__).resolve().parent
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
MIN_TOKEN_LEN = 16


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(f"OPTIONLENS_{name}")
    return v if v not in (None, "") else default


def _num(name: str, default: str, cast: Callable[[str], T]) -> T:
    raw = _env(name, default)
    try:
        return cast(raw)
    except ValueError:
        raise ValueError(f"OPTIONLENS_{name}={raw!r} is not a valid {cast.__name__}") from None


def _opt_int(name: str) -> int | None:
    return _num(name, "", int) if _env(name) is not None else None


# numeric settings that schedules/limiters divide by or loop on: must be > 0
_POSITIVE = ("sweep_interval_min", "option_eod_retention_days", "cboe_concurrency", "cboe_rps",
             "nasdaq_rps", "smtp_port", "questrade_expiries", "questrade_moneyness")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    host: str
    port: int                              # 0: bind a free port (serve prints `LISTENING <port>`)
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
    api_token: str | None = None           # per-launch token from the app; None = auth off (development)
    cors_origins: tuple[str, ...] = ("app://optionlens",)
    parent_pid: int | None = None          # exit when this process (the app) disappears
    chain_universe_size: int = 150         # most-liquid symbols in the chain universe (besides watchlists)
    # Enabled chain sources (no order: no source is preferred, jobs/precedence.py):
    # "ibkr-bridge" = chains pushed by the app's IBKR bridge, any other name = a server-side ChainSource
    # (sources/chain_source.py REGISTRY) the engine sweeps itself once it is connected.
    chain_sources: tuple[str, ...] = ("ibkr-bridge", "questrade")
    questrade_expiries: int = 8            # Questrade sweeps: first N expiries ...
    questrade_moneyness: float = 0.25      # ... strikes within ±this fraction of the underlying price
    tz: str = "America/New_York"

    def __post_init__(self) -> None:
        bad = [f"OPTIONLENS_{n.upper()}={getattr(self, n)} must be > 0" for n in _POSITIVE if getattr(self, n) <= 0]
        if self.host not in LOOPBACK_HOSTS:
            bad.append(f"OPTIONLENS_HOST={self.host} must be loopback ({', '.join(LOOPBACK_HOSTS)}): "
                       "the engine only serves the local app")
        if not 0 <= self.port <= 65535:
            bad.append(f"OPTIONLENS_PORT={self.port} must be in 0..65535")
        if self.universe_stocks < 0:
            bad.append(f"OPTIONLENS_UNIVERSE_STOCKS={self.universe_stocks} must be >= 0")
        if self.chain_universe_size < 0:
            bad.append(f"OPTIONLENS_CHAIN_UNIVERSE_SIZE={self.chain_universe_size} must be >= 0")
        if not self.chain_sources or len(set(self.chain_sources)) != len(self.chain_sources):
            bad.append(f"OPTIONLENS_CHAIN_SOURCES={','.join(self.chain_sources)} must list distinct sources")
        if self.questrade_moneyness >= 1:
            bad.append(f"OPTIONLENS_QUESTRADE_MONEYNESS={self.questrade_moneyness} must be < 1")
        if self.api_token is not None and len(self.api_token) < MIN_TOKEN_LEN:
            bad.append(f"OPTIONLENS_API_TOKEN must be at least {MIN_TOKEN_LEN} characters")
        if self.parent_pid is not None and self.parent_pid <= 0:
            bad.append(f"OPTIONLENS_PARENT_PID={self.parent_pid} must be > 0")
        if bad:
            raise ValueError("invalid settings: " + "; ".join(bad))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "optionlens.duckdb"

    @property
    def auth_enabled(self) -> bool:
        return self.api_token is not None


def load_settings() -> Settings:
    """Build Settings from the current environment."""
    return Settings(
        data_dir=Path(_env("DATA_DIR", "~/.local/share/optionlens")).expanduser(),
        host=_env("HOST", "127.0.0.1"),
        port=_num("PORT", "8473", int),
        universe_stocks=_num("UNIVERSE_STOCKS", "1000", int),
        etf_file=Path(_env("ETF_FILE", str(PACKAGE_DIR / "etfs.txt"))).expanduser(),
        sweep_interval_min=_num("SWEEP_INTERVAL_MIN", "60", int),
        option_eod_retention_days=_num("OPTION_EOD_RETENTION_DAYS", "730", int),
        risk_free_rate=_num("RISK_FREE_RATE", "0.04", float),
        cboe_concurrency=_num("CBOE_CONCURRENCY", "2", int),
        cboe_rps=_num("CBOE_RPS", "1.5", float),
        nasdaq_rps=_num("NASDAQ_RPS", "4", float),
        ntfy_url=_env("NTFY_URL"),
        smtp_host=_env("SMTP_HOST"),
        smtp_port=_num("SMTP_PORT", "587", int),
        smtp_user=_env("SMTP_USER"),
        smtp_password=_env("SMTP_PASSWORD"),
        smtp_from=_env("SMTP_FROM"),
        smtp_to=_env("SMTP_TO"),
        api_token=_env("API_TOKEN"),
        cors_origins=tuple(o.strip() for o in _env("CORS_ORIGINS", "app://optionlens").split(",") if o.strip()),
        parent_pid=_opt_int("PARENT_PID"),
        chain_universe_size=_num("CHAIN_UNIVERSE_SIZE", "150", int),
        chain_sources=tuple(x.strip().lower() for x in _env("CHAIN_SOURCES", "ibkr-bridge,questrade").split(",")
                            if x.strip()),
        questrade_expiries=_num("QUESTRADE_EXPIRIES", "8", int),
        questrade_moneyness=_num("QUESTRADE_MONEYNESS", "0.25", float),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings (cached; tests call get_settings.cache_clear())."""
    return load_settings()
