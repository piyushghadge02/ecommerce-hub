"""Pydantic-settings based application configuration.

Every secret / connection string comes from the environment (or a local
`.env` file during development). No credentials are hard-coded anywhere.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    POSTGRES_DSN: str = "postgresql+asyncpg://ecom:ecom@localhost:5432/ecommerce"
    MONGO_URI: str = "mongodb://localhost:27017"
    MONGO_DB: str = "ecommerce"
    ELASTICSEARCH_URL: str = "http://localhost:9200"
    ES_INDEX: str = "orders"

    SYNC_STRATEGY: Literal["dual_write", "outbox", "polling"] = "dual_write"
    OUTBOX_POLL_SECONDS: int = 2
    POLLING_INTERVAL_SECONDS: int = 15
    OUTBOX_MAX_ATTEMPTS: int = 10

    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000

    @field_validator("SYNC_STRATEGY", mode="before")
    @classmethod
    def _normalize_strategy(cls, value: str) -> str:
        return str(value).strip().lower()


@lru_cache
def get_settings() -> Settings:
    return Settings()
