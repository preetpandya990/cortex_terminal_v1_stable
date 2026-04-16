"""
Cortex AI — Unified Configuration
==================================
All settings loaded from environment variables or .env file.
Fails fast if required secrets are missing.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import (
    AnyHttpUrl,
    Field,
    PostgresDsn,
    RedisDsn,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ────────────────────────────────────────────────────────────
    APP_NAME: str = "Cortex AI Trading Platform"
    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: Literal["development", "staging", "production"] = "production"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"

    # ── Database ───────────────────────────────────────────────────────────────
    DATABASE_URL: PostgresDsn = Field(..., description="Async PostgreSQL URL (postgresql+asyncpg://...)")
    DB_POOL_SIZE: int = Field(20, ge=1, le=100)
    DB_MAX_OVERFLOW: int = Field(10, ge=0, le=100)
    DB_POOL_TIMEOUT: int = Field(30, ge=5, le=120)
    DB_POOL_RECYCLE: int = Field(1800, ge=300)

    # ── Redis ──────────────────────────────────────────────────────────────────
    REDIS_URL: RedisDsn = Field(..., description="Redis URL (redis://host:port/db)")
    REDIS_MAX_CONNECTIONS: int = Field(50, ge=10, le=500)

    # ── Security — JWT ─────────────────────────────────────────────────────────
    SECRET_KEY: str = Field(..., min_length=32, description="HMAC secret (openssl rand -hex 32)")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(30, ge=5, le=1440)
    REFRESH_TOKEN_EXPIRE_DAYS: int = Field(7, ge=1, le=30)

    # ── Security — CORS ────────────────────────────────────────────────────────
    CORS_ALLOWED_ORIGINS: list[AnyHttpUrl] = Field(..., description="Explicit allowlist of origins")
    CORS_ALLOW_CREDENTIALS: bool = True

    # ── Upstox ─────────────────────────────────────────────────────────────────
    UPSTOX_API_KEY: str = Field(..., min_length=8)
    UPSTOX_API_SECRET: str = Field(..., min_length=8)
    UPSTOX_REDIRECT_URI: AnyHttpUrl
    UPSTOX_ACCESS_TOKEN: str | None = None
    UPSTOX_BASE_URL: str = "https://api.upstox.com/v3"
    UPSTOX_WS_URL: str = "wss://api.upstox.com/v3/feed/market-data-feed"
    UPSTOX_INSTRUMENTS_URL: AnyHttpUrl = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"

    # ── Worker ─────────────────────────────────────────────────────────────────
    ENABLE_BACKGROUND_TASKS: bool = True
    WORKER_SHUTDOWN_TIMEOUT: int = Field(30, ge=5, le=120)

    # ── RSS Ingestion ──────────────────────────────────────────────────────────
    RSS_MIN_POLL_SECONDS: int = Field(300, ge=60)
    RSS_MAX_POLL_SECONDS: int = Field(900, ge=300)
    RSS_CONCURRENCY: int = Field(5, ge=1, le=20)
    RSS_POLL_JITTER_SECONDS: int = Field(60, ge=0, le=300)

    # ── Ollama ─────────────────────────────────────────────────────────────────
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "llama3.1:8b"
    OLLAMA_TIMEOUT: int = Field(30, ge=5, le=120)
    OPENAI_API_KEY: str | None = None

    # ── Safety Layer ───────────────────────────────────────────────────────────
    LOSS_LIMIT_THRESHOLD: float = Field(0.05, ge=0.01, le=0.2)
    VOLATILITY_SPIKE_MULTIPLIER: float = Field(3.0, ge=1.5, le=10.0)
    SIGNAL_FREQUENCY_THRESHOLD: int = Field(100, ge=10, le=1000)

    # ── Worker Loops ───────────────────────────────────────────────────────────
    REGIME_IDLE_SLEEP_SECONDS: int = Field(60, ge=30, le=300)
    DRIFT_CHECK_INTERVAL_SECONDS: int = Field(300, ge=60, le=3600)
    SAFETY_CHECK_INTERVAL_SECONDS: int = Field(30, ge=10, le=300)

    # ── ML System ──────────────────────────────────────────────────────────────
    ML_MODEL_ENCRYPTION_KEY: str | None = Field(None, description="Fernet key for model encryption")
    ML_MODEL_STORAGE_PATH: str = "backend/ml_models"
    ML_FEATURE_VERSION: str = "1.0.0"
    ML_EVALUATION_THRESHOLD: float = Field(0.85, ge=0.0, le=1.0)
    ML_LATENCY_TARGET_MS: int = Field(250, ge=50, le=1000)
    ML_BATCH_SIZE: int = Field(32, ge=1, le=128)
    ML_MAX_MODEL_VERSIONS: int = Field(10, ge=3, le=50)
    ML_DRIFT_THRESHOLD_SIGMA: float = Field(2.0, ge=1.0, le=5.0)
    ML_ACCURACY_ALERT_THRESHOLD: float = Field(0.83, ge=0.5, le=1.0)

    # ── Rate Limiting ──────────────────────────────────────────────────────────
    RATE_LIMIT_SCANNER: str = "10/minute"
    RATE_LIMIT_SEARCH: str = "60/minute"
    RATE_LIMIT_MARKET_DATA: str = "120/minute"
    RATE_LIMIT_ML_PREDICTION: str = "100/minute"

    # ── Caching TTLs ───────────────────────────────────────────────────────────
    CACHE_TTL_SCANNER: int = 60
    CACHE_TTL_INSTRUMENTS: int = 300
    CACHE_TTL_LIVE_QUOTE: int = 1
    CACHE_TTL_MARKET_STATUS: int = 30
    ML_CACHE_TTL_DAILY: int = 300
    ML_CACHE_TTL_WEEKLY: int = 3600

    # ── Observability ──────────────────────────────────────────────────────────
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    SENTRY_DSN: str | None = None

    # ── NSE Market ─────────────────────────────────────────────────────────────
    NSE_HOLIDAY_API_URL: str = "https://www.nseindia.com/api/holiday-master?type=trading"
    NSE_HOLIDAY_CACHE_FILE: str = "backend/.cache/nse_holidays.json"
    NSE_MARKET_OPEN_IST: str = "09:15"
    NSE_MARKET_CLOSE_IST: str = "15:30"

    # ── Computed Properties ────────────────────────────────────────────────────
    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def docs_url(self) -> str | None:
        return None if self.is_production else "/docs"

    @property
    def redoc_url(self) -> str | None:
        return None if self.is_production else "/redoc"

    @property
    def cors_origins_str(self) -> list[str]:
        return [str(origin).rstrip("/") for origin in self.CORS_ALLOWED_ORIGINS]

    # ── Validators ─────────────────────────────────────────────────────────────
    @field_validator("SECRET_KEY")
    @classmethod
    def secret_key_must_not_be_default(cls, v: str) -> str:
        forbidden = {"your-secret-key-change-in-production", "your-secret-key", "secret", "changeme"}
        if v.lower() in forbidden:
            raise ValueError("SECRET_KEY must not be a placeholder. Generate with: openssl rand -hex 32")
        return v

    @model_validator(mode="after")
    def wildcard_cors_is_forbidden(self) -> "Settings":
        for origin in self.cors_origins_str:
            if origin == "*":
                raise ValueError("Wildcard CORS origin '*' is forbidden. Specify explicit origins.")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings singleton."""
    return Settings()
