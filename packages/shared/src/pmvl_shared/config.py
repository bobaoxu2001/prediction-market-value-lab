"""Runtime configuration.

Everything is environment-driven. Secrets are read from the process environment only
and are never logged, serialised into API responses, or written to the database.
"""

from __future__ import annotations

import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_SQLITE_PATH = REPO_ROOT / "data" / "pmvl.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- environment
    environment: str = Field(default="local")
    log_level: str = Field(default="INFO")

    #: When false, every write path that would persist provider data refuses to run
    #: against demo fixtures. See ``pmvl_shared.provenance``.
    allow_demo_data: bool = Field(default=True)

    # ---------------------------------------------------------------- database
    #: Defaults to a local SQLite file so `make dev` works with zero infrastructure.
    #: Set to a postgresql+psycopg:// URL (see docker-compose.yml) for production.
    database_url: str = Field(default="")

    # ---------------------------------------------------------------- providers
    kalshi_api_base: str = Field(default="https://api.elections.kalshi.com/trade-api/v2")
    #: Public market data needs no credentials. These are only required if a future
    #: execution service is enabled; the research system never uses them.
    kalshi_api_key_id: str = Field(default="")
    kalshi_private_key_pem: str = Field(default="")

    polymarket_gamma_base: str = Field(default="https://gamma-api.polymarket.com")
    polymarket_clob_base: str = Field(default="https://clob.polymarket.com")
    polymarket_data_base: str = Field(default="https://data-api.polymarket.com")
    polymarket_ws_base: str = Field(default="wss://ws-subscriptions-clob.polymarket.com/ws")

    coinbase_api_base: str = Field(default="https://api.exchange.coinbase.com")
    #: Yahoo's public chart endpoint. Keyless; backs the equity index model.
    yahoo_finance_base: str = Field(default="https://query1.finance.yahoo.com")
    nws_api_base: str = Field(default="https://api.weather.gov")
    #: NWS requires a self-identifying User-Agent per their API terms.
    http_user_agent: str = Field(
        default="prediction-market-value-lab/0.1 (research; contact: set HTTP_USER_AGENT)"
    )

    # ---------------------------------------------------------------- research LLM
    #: Optional. Absent => NullResearchProvider, which contributes zero evidence and
    #: zero probability weight rather than inventing narrative.
    anthropic_api_key: str = Field(default="")
    research_model: str = Field(default="claude-sonnet-5")
    research_enabled: bool = Field(default=False)

    # ---------------------------------------------------------------- http client
    http_timeout_seconds: float = Field(default=25.0)
    http_max_retries: int = Field(default=4)
    http_max_concurrency: int = Field(default=6)
    #: Circuit breaker: consecutive failures before a provider is short-circuited.
    circuit_breaker_threshold: int = Field(default=8)
    circuit_breaker_reset_seconds: int = Field(default=120)

    # ---------------------------------------------------------------- ingest scope
    ingest_market_limit: int = Field(default=1200)
    ingest_orderbook_limit: int = Field(default=250)
    orderbook_depth: int = Field(default=25)
    #: Markets below this 24h dollar volume are ingested but never ranked, because a
    #: quote nobody trades against is not an executable price.
    min_volume_24h_usd: Decimal = Field(default=Decimal("500"))
    min_orderbook_notional_usd: Decimal = Field(default=Decimal("25"))

    # ---------------------------------------------------------------- economics
    #: Extra adverse price movement assumed between quote capture and fill.
    slippage_ticks: int = Field(default=1)
    #: Annualised opportunity cost of locked capital, used for time-to-resolution drag.
    capital_cost_annual_rate: Decimal = Field(default=Decimal("0.05"))
    #: Flat per-leg cost assumed for moving value onto/around Polymarket (bridge/gas).
    polymarket_transfer_cost_usd: Decimal = Field(default=Decimal("0.50"))
    #: Fraction of full Kelly actually recommended.
    kelly_fraction: Decimal = Field(default=Decimal("0.25"))
    #: A recommendation must clear this conservative net EV per contract to rank.
    min_conservative_net_ev: Decimal = Field(default=Decimal("0.005"))
    #: Cross-platform legs cannot fill simultaneously; charged against arbitrage.
    cross_platform_execution_risk_usd: Decimal = Field(default=Decimal("0.01"))

    # ------------------------------------------------------- arbitrage margins
    #: Minimum NET edge, as a fraction of capital deployed, before an arbitrage is
    #: published. Held in configuration rather than scattered through the scanners so
    #: the risk appetite of the whole engine can be read - and changed - in one place.
    #:
    #: The tiers are not arbitrary. Same-platform legs settle against one another on
    #: one venue's books, so the residual risk is fill risk alone. Cross-platform legs
    #: add settlement-source divergence, two different close times, capital split
    #: across venues, and withdrawal cost, none of which are recoverable if one leg
    #: fills and the other does not - so they must clear a materially higher bar.
    min_edge_same_platform_liquid: Decimal = Field(default=Decimal("0.015"))
    min_edge_same_platform_normal: Decimal = Field(default=Decimal("0.03"))
    min_edge_cross_platform: Decimal = Field(default=Decimal("0.04"))
    min_edge_cross_platform_illiquid: Decimal = Field(default=Decimal("0.05"))
    #: Depth at or above this notional counts as "liquid" for tier selection.
    liquid_depth_threshold_usd: Decimal = Field(default=Decimal("2000"))
    #: Quotes older than this are "stale" and cannot back an executable claim.
    max_quote_age_seconds: int = Field(default=300)

    # ---------------------------------------------------------------- snapshots
    daily_snapshot_hour_utc: int = Field(default=13)
    daily_snapshot_minute_utc: int = Field(default=0)
    top_n: int = Field(default=10)

    # ---------------------------------------------------------------- compliance
    #: Advisory only. The research platform is read-only; this gates the *future*
    #: execution surface, which does not exist in this release.
    restricted_regions: str = Field(default="US-restricted-states,UK,FR,BE,SG,TH,PL,ON")
    trading_execution_enabled: bool = Field(default=False)

    api_cors_origins: str = Field(default="http://localhost:3000,http://127.0.0.1:3000")

    def min_arbitrage_edge(
        self, *, cross_platform: bool, depth_usd: Decimal | None
    ) -> Decimal:
        """Minimum net edge this opportunity must clear, by venue span and liquidity.

        Thin books are the harder case in both spans: the quoted edge is real only for
        the few contracts at the top of the ladder, and the rest of the intended size
        walks into worse prices.
        """
        liquid = depth_usd is not None and depth_usd >= self.liquid_depth_threshold_usd
        if cross_platform:
            return (
                self.min_edge_cross_platform
                if liquid
                else self.min_edge_cross_platform_illiquid
            )
        return (
            self.min_edge_same_platform_liquid
            if liquid
            else self.min_edge_same_platform_normal
        )

    @field_validator("database_url")
    @classmethod
    def _default_sqlite(cls, v: str) -> str:
        if v:
            return v
        # Creating the data directory is a convenience for local runs, NOT a
        # precondition. On a read-only serverless mount this mkdir raised OSError
        # during Settings construction, which happens at import time - so the entire
        # API failed to start with an opaque FUNCTION_INVOCATION_FAILED rather than a
        # message naming the real problem. Never let a convenience abort startup.
        try:
            DEFAULT_SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return f"sqlite+pysqlite:///{DEFAULT_SQLITE_PATH}"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]

    @property
    def restricted_region_list(self) -> list[str]:
        return [r.strip().upper() for r in self.restricted_regions.split(",") if r.strip()]

    def redacted(self) -> dict[str, object]:
        """Config safe to expose on /system - secrets become presence booleans."""
        secret_fields = {
            "kalshi_api_key_id",
            "kalshi_private_key_pem",
            "anthropic_api_key",
        }
        out: dict[str, object] = {}
        for name in self.model_fields:
            if name in secret_fields:
                out[name] = bool(getattr(self, name))
                continue
            value = getattr(self, name)
            if name == "database_url":
                value = _redact_dsn(str(value))
            out[name] = str(value) if isinstance(value, Decimal) else value
        return out


def _redact_dsn(dsn: str) -> str:
    """Strip credentials from a database URL before it is ever displayed."""
    if "@" not in dsn:
        return dsn
    scheme, _, rest = dsn.partition("://")
    creds, _, host = rest.rpartition("@")
    if not creds:
        return dsn
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Used by tests that manipulate the environment."""
    get_settings.cache_clear()
    os.environ.pop("PMVL_SETTINGS_CACHE", None)
