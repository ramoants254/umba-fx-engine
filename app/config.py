"""Application configuration via environment variables."""
from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Configuration loaded from environment variables."""

    # Database
    database_url: str = "postgresql://fx_user:fx_password@localhost:5432/fx_engine"

    # Rate provider
    rate_api_base_url: str = "https://api.exchangeratesapi.io/v1"
    rate_api_key: str = ""
    rate_cache_ttl_seconds: int = 300  # 5 minutes
    rate_stale_max_seconds: int = 900  # 15 minutes
    rate_refresh_interval_seconds: int = 60
    rate_spread_bps: str = "0.005"  # 50 bps each side

    # Quotes
    quote_ttl_seconds: int = 60

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    model_config = {"env_prefix": "FX_", "case_sensitive": False}


settings = Settings()
