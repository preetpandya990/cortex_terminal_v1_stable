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
        env_prefix="",
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
    UPSTOX_BASE_URL: str = "https://api.upstox.com/v3/"
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

    # ── Data Ingestion Worker ──────────────────────────────────────────────────
    DATA_INGESTION_ENABLED: bool = Field(True, description="Enable automated OHLCV data ingestion")
    DATA_INGESTION_CHECK_INTERVAL: int = Field(3600, ge=60, description="Maintenance gap-check interval (seconds)")
    DATA_INGESTION_REQUESTS_PER_MINUTE: int = Field(40, ge=10, le=490, description="API rate limit budget — 40/min (1 req every 1.5s) stays comfortably below Cloudflare's burst threshold on the historical-candle endpoint")
    DATA_INGESTION_CONCURRENCY: int = Field(1, ge=1, le=20, description="Parallel in-flight API requests — 1 = fully serial, eliminates all request bursting, safest for Cloudflare-guarded endpoints")
    DATA_INGESTION_BACKFILL_ENABLED: bool = Field(True, description="Run Phase 1 historical backfill on startup")
    DATA_INGESTION_MAX_RETRIES: int = Field(3, ge=1, le=10, description="Max retries per failed chunk before DLQ")
    DATA_INGESTION_CIRCUIT_BREAKER_THRESHOLD: int = Field(5, ge=3, le=20, description="Consecutive failures before circuit opens")
    DATA_INGESTION_CIRCUIT_BREAKER_TIMEOUT: int = Field(300, ge=60, le=3600, description="Seconds before circuit attempts recovery")
    BULK_INSERT_BATCH_SIZE: int = Field(1000, ge=100, le=10000, description="Database bulk insert batch size")

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

    # ── Market Feed ────────────────────────────────────────────────────────────
    MARKET_FEED_THROTTLE_MS: int = Field(
        default=250,
        ge=100,
        le=5000,
        description="Per-instrument tick throttle (ms) before publishing to Redis",
    )

    # ── Rate Limiting ──────────────────────────────────────────────────────────
    RATE_LIMIT_SCANNER: str = "10/minute"
    RATE_LIMIT_SEARCH: str = "60/minute"
    RATE_LIMIT_MARKET_DATA: str = "120/minute"
    RATE_LIMIT_ML_PREDICTION: str = "100/minute"

    # ── Caching TTLs ───────────────────────────────────────────────────────────
    CACHE_TTL_SCANNER_OPEN:   int = 30    # seconds during market hours — prices update each tick
    CACHE_TTL_SCANNER_CLOSED: int = 900   # seconds off-hours — data is static between sessions
    CACHE_TTL_INSTRUMENTS: int = 300
    CACHE_TTL_LIVE_QUOTE: int = 1
    CACHE_TTL_MARKET_STATUS: int = 30
    ML_CACHE_TTL_DAILY: int = 300
    ML_CACHE_TTL_WEEKLY: int = 3600
    
    # API Response Caching
    CACHE_TTL_SUGGESTIONS_LIST: int = Field(30, ge=5, le=300, description="Cache TTL for suggestions list endpoint (seconds)")
    CACHE_TTL_SUGGESTIONS_DETAIL: int = Field(60, ge=5, le=300, description="Cache TTL for suggestions detail endpoint (seconds)")
    ENABLE_API_RESPONSE_CACHING: bool = Field(True, description="Enable API response caching")

    # ── Observability ──────────────────────────────────────────────────────────
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    SENTRY_DSN: str | None = None

    # ── NSE Market ─────────────────────────────────────────────────────────────
    NSE_HOLIDAY_API_URL: str = "https://www.nseindia.com/api/holiday-master?type=trading"
    NSE_HOLIDAY_CACHE_FILE: str = "backend/.cache/nse_holidays.json"
    NSE_MARKET_OPEN_IST: str = "09:15"
    NSE_MARKET_CLOSE_IST: str = "15:30"

    # ── Signal Scheduler ───────────────────────────────────────────────────────
    # Nifty 50 + Nifty Next 50 — scheduled signal generation every 15 minutes
    # during NSE market hours.  Override via env var as a comma-separated string.
    SIGNAL_SCHEDULED_UNIVERSE: list[str] = [
        # ── Nifty 50 ──────────────────────────────────────────────────────────
        "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
        "HINDUNILVR", "SBIN", "BAJFINANCE", "BHARTIARTL", "KOTAKBANK",
        "ITC", "LT", "HCLTECH", "AXISBANK", "ASIANPAINT",
        "MARUTI", "ULTRACEMCO", "NESTLEIND", "TITAN", "WIPRO",
        "SUNPHARMA", "BAJAJFINSV", "ONGC", "NTPC", "POWERGRID",
        "TECHM", "TATAMOTORS", "JSWSTEEL", "TATASTEEL", "HINDALCO",
        "ADANIENT", "ADANIPORTS", "DRREDDY", "DIVISLAB", "CIPLA",
        "BAJAJ-AUTO", "HEROMOTOCO", "COALINDIA", "BPCL", "INDUSINDBK",
        "EICHERMOT", "GRASIM", "BRITANNIA", "APOLLOHOSP", "TATACONSUM",
        "PIDILITIND", "SHREECEM", "HDFCLIFE", "SBILIFE", "UPL",
        # ── Nifty Next 50 ─────────────────────────────────────────────────────
        "HAVELLS", "GODREJCP", "BERGEPAINT", "COLPAL", "MARICO",
        "LUPIN", "BIOCON", "TORNTPHARM", "DABUR", "VOLTAS",
        "MPHASIS", "COFORGE", "LTIM", "PERSISTENT", "TATAELXSI",
        "CANBK", "BANKBARODA", "PNB", "FEDERALBNK", "IDFCFIRSTB",
        "LICI", "NAUKRI", "ZOMATO", "IRCTC", "HAL",
        "BEL", "TRENT", "VEDL", "NMDC", "SAIL",
        "GAIL", "IOC", "HPCL", "ATGL", "TORNTPOWER",
        "SIEMENS", "ABB", "CHOLAFIN", "BAJAJHLDNG", "PIIND",
        "CONCOR", "OFSS", "MUTHOOTFIN", "ABCAPITAL", "MFSL",
        "ICICIGI", "POLICYBZR", "PAYTM", "NYKAA", "DELHIVERY",
    ]
    SIGNAL_SCHEDULER_INTERVAL_MINUTES: int = Field(15, ge=5, le=60)
    SIGNAL_SCHEDULER_FEATURE_CONCURRENCY: int = Field(20, ge=5, le=50)
    SIGNAL_SCHEDULER_ASSEMBLY_CONCURRENCY: int = Field(10, ge=2, le=20)
    SIGNAL_ON_DEMAND_CACHE_TTL: int = Field(900, ge=60, le=3600)  # 15 minutes

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
