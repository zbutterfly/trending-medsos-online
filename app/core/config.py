"""Application configuration sourced from environment variables.

All secrets live in backend/.env (gitignored). The frontend never reads these
directly — all data flows through the FastAPI layer.
"""
from __future__ import annotations

from functools import lru_cache
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed settings loaded from environment.

    Defaults are safe for local development — only secrets need override.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # General
    app_env: str = "development"
    log_level: str = "INFO"
    fetch_interval_min: int = Field(30, ge=1, le=1440)
    port: int = 9000

    # Optional HTTP Basic Auth — set AUTH_PASSWORD to lock a deployed instance.
    auth_username: str = "admin"
    auth_password: Optional[str] = None

    # Fast lane interval (seconds) for wire/on-chain fetchers (Tree News,
    # Hyperliquid whales, IQPlus). Slow lane keeps fetch_interval_min.
    fast_interval_sec: int = Field(60, ge=10, le=3600)

    # Reddit (optional)
    reddit_client_id: Optional[str] = None
    reddit_client_secret: Optional[str] = None
    reddit_user_agent: str = "trending-medsos:v1.0 (by /u/anon)"
    reddit_username: Optional[str] = None
    reddit_password: Optional[str] = None

    # Threads (optional, unofficial)
    threads_username: Optional[str] = None
    threads_password: Optional[str] = None

    # Nitter / X
    nitter_instances: str = "https://nitter.privacydev.net\nhttps://nitter.poast.org\nhttps://nitter.cz"

    # Crypto APIs
    coingecko_api_key: Optional[str] = None
    cmc_api_key: Optional[str] = None

    # Curl stealth
    curl_impersonate: str = "chrome131"

    # Emerging chains RPC
    megaeth_rpc: str = "https://carrot.megaeth.com/rpc"
    monad_rpc: str = "https://testnet-rpc.monad.xyz"
    abstract_rpc: str = "https://api.mainnet.abs.xyz"

    # Signal layer
    # Hyperliquid whale watchlist: "0xaddr:Label,0xaddr2:Label2" (label optional).
    hyperliquid_watchlist: str = ""

    @field_validator("nitter_instances")
    @classmethod
    def _parse_nitter(cls, v: str) -> str:
        # Normalize: strip whitespace per line, drop blank lines, keep scheme.
        lines = [line.strip() for line in (v or "").splitlines() if line.strip()]
        if not lines:
            return ""
        return "\n".join(lines)

    @property
    def nitter_list(self) -> List[str]:
        return [x for x in self.nitter_instances.splitlines() if x]

    @property
    def reddit_enabled(self) -> bool:
        return bool(self.reddit_client_id and self.reddit_client_secret)

    @property
    def threads_enabled(self) -> bool:
        return bool(self.threads_username and self.threads_password)

    @property
    def coingecko_demo(self) -> bool:
        return bool(self.coingecko_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
