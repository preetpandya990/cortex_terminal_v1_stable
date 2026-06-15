"""
Centralized Logging Configuration
==================================
Production-grade logging setup with environment-based log levels and PII masking.
"""
import logging
import logging.config
import os
import re
from typing import Any

# Loggers whose DEBUG output forms the explanation-generation pipeline trace
# (worker orchestration + LLM provider/stream events + RAG retrieval/embedding).
# Routed to a dedicated file when EXPLANATION_PIPELINE_LOG_ENABLED is set.
_EXPLANATION_PIPELINE_LOGGERS = (
    "app.api.v1.ai_stream",                      # SSE: 3-stage decision + delivery
    "app.ai.intelligence.explanation_worker",
    "app.ai.intelligence.llm_client",
    "app.ai.rag",
)
_PIPELINE_LOG_MAX_BYTES = 10 * 1024 * 1024   # 10 MB per file before rotation
_PIPELINE_LOG_BACKUP_COUNT = 5               # retain 5 rotated files (~60 MB cap)


class WebSocketPingPongFilter(logging.Filter):
    """
    Filter out WebSocket ping/pong debug messages to reduce log noise.
    
    WebSocket keepalive messages generate 4 log entries per second per connection:
    - % sending keepalive ping
    - > PING [binary, 4 bytes]
    - < PONG [binary, 4 bytes]
    - % received keepalive pong
    
    With 10 connections, this creates 144,000 log entries/hour (50-100 MB/hour).
    This filter removes these messages while keeping ERROR and WARNING logs.
    """
    
    def filter(self, record: logging.LogRecord) -> bool:
        """Return False to filter out (drop) the log record."""
        # Only filter DEBUG level messages
        if record.levelno != logging.DEBUG:
            return True
        
        # Get the message
        message = record.getMessage()
        
        # Filter out WebSocket ping/pong messages
        ping_pong_keywords = [
            'keepalive ping',
            'keepalive pong',
            '> PING',
            '< PONG',
            '% sending keepalive',
            '% received keepalive',
        ]
        
        for keyword in ping_pong_keywords:
            if keyword in message:
                return False  # Drop this log
        
        return True  # Keep all other logs


def setup_logging(
    log_level: str = "INFO",
    environment: str = "production",
    *,
    explanation_log_enabled: bool = False,
    explanation_log_path: str = "logs/explanation_pipeline.log",
) -> None:
    """
    Configure structured JSON logging for the application.

    Args:
        log_level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        environment: Environment name (development, staging, production)
        explanation_log_enabled: When True, route the explanation-pipeline
            loggers (worker + llm_client + rag) at DEBUG to a dedicated rotating
            file, keeping console at INFO+ for them.  See _EXPLANATION_PIPELINE_LOGGERS.
        explanation_log_path: Destination path for that diagnostics log file.
    """
    # Determine log level based on environment
    if environment == "development":
        log_level = "DEBUG"
    elif environment == "staging":
        log_level = "INFO"
    else:  # production
        log_level = "INFO"
    
    logging_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "websocket_filter": {
                "()": WebSocketPingPongFilter,
            }
        },
        "formatters": {
            "json": {
                "()": "app.core.json_formatter.ContextualJsonFormatter",
                "format": "%(timestamp)s %(level)s %(name)s %(message)s",
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "json",
                "stream": "ext://sys.stdout",
            }
        },
        "root": {
            "level": log_level,
            "handlers": ["console"],
        },
        # Align uvicorn loggers
        "loggers": {
            "uvicorn": {
                "level": log_level,
                "handlers": ["console"],
                "propagate": False,
            },
            "uvicorn.error": {
                "level": "INFO",
                "handlers": ["console"],
                "filters": ["websocket_filter"],
                "propagate": False,
            },
            "uvicorn.access": {
                "level": "WARNING",
                "handlers": ["console"],
                "propagate": False,
            },
            "app": {
                "level": log_level,
                "handlers": ["console"],
                "propagate": False,
            },
            # httpx / httpcore log every HTTP request URL in full at INFO level.
            # With 500 URL-encoded instrument keys per Upstox batch call, each
            # log line is ~50 KB of unreadable noise.  Set to WARNING so only
            # genuine errors (timeouts, SSL failures) are surfaced.
            "httpx": {
                "level": "WARNING",
                "handlers": ["console"],
                "propagate": False,
            },
            "httpcore": {
                "level": "WARNING",
                "handlers": ["console"],
                "propagate": False,
            },
        },
    }
    
    # ── Dedicated explanation-pipeline diagnostics log (opt-in) ───────────────
    # Isolates the explanation worker + LLM client + RAG loggers (at DEBUG) into
    # a rotating file so their detailed timing trace is readable away from the
    # multi-WebSocket console.  A leveled console handler keeps INFO+ for these
    # loggers on stdout (so normal operation stays visible at a glance) while the
    # file additionally captures DEBUG.  propagate=False prevents duplication via
    # the parent "app" logger.
    if explanation_log_enabled:
        # RotatingFileHandler opens its file lazily (delay=True), but ensure the
        # parent directory exists so the first emit cannot fail on a fresh clone.
        os.makedirs(os.path.dirname(explanation_log_path) or ".", exist_ok=True)
        logging_config["handlers"]["explanation_console"] = {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "stream": "ext://sys.stdout",
            "level": "INFO",
            "filters": ["websocket_filter"],
        }
        logging_config["handlers"]["explanation_file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "json",
            "filename": explanation_log_path,
            "maxBytes": _PIPELINE_LOG_MAX_BYTES,
            "backupCount": _PIPELINE_LOG_BACKUP_COUNT,
            "encoding": "utf-8",
            "delay": True,
            "level": "DEBUG",
        }
        for logger_name in _EXPLANATION_PIPELINE_LOGGERS:
            logging_config["loggers"][logger_name] = {
                "level": "DEBUG",
                "handlers": ["explanation_console", "explanation_file"],
                "propagate": False,
            }

    logging.config.dictConfig(logging_config)
    logger = logging.getLogger("app")
    logger.info(
        "Logging configured",
        extra={
            "log_level": log_level,
            "environment": environment,
            "websocket_filter": "enabled",
            "explanation_pipeline_log": (
                explanation_log_path if explanation_log_enabled else "disabled"
            ),
        },
    )


# ── PII Masking Utilities ──────────────────────────────────────────────────────

def mask_email(email: str) -> str:
    """
    Mask email address for logging.
    
    Example: user@example.com -> u***@example.com
    """
    if not email or "@" not in email:
        return email
    
    local, domain = email.split("@", 1)
    if len(local) <= 1:
        return f"{local}***@{domain}"
    return f"{local[0]}***@{domain}"


def mask_token(token: str, visible_chars: int = 4) -> str:
    """
    Mask authentication token for logging.
    
    Example: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9... -> eyJh...
    """
    if not token or len(token) <= visible_chars:
        return "***"
    return f"{token[:visible_chars]}..."


def mask_phone(phone: str) -> str:
    """
    Mask phone number for logging.
    
    Example: +1234567890 -> +123***7890
    """
    if not phone or len(phone) < 4:
        return "***"
    return f"{phone[:4]}***{phone[-4:]}"


def mask_credit_card(card: str) -> str:
    """
    Mask credit card number for logging.
    
    Example: 1234567890123456 -> 1234********3456
    """
    if not card or len(card) < 8:
        return "***"
    return f"{card[:4]}{'*' * (len(card) - 8)}{card[-4:]}"


def mask_pii(data: Any) -> Any:
    """
    Recursively mask PII in data structures.
    
    Masks fields: email, password, token, access_token, refresh_token, 
                  phone, credit_card, ssn, api_key, secret
    
    Args:
        data: Dict, list, or primitive value
    
    Returns:
        Data with PII fields masked
    """
    if isinstance(data, dict):
        masked = {}
        for key, value in data.items():
            key_lower = key.lower()
            
            # Mask sensitive fields
            if key_lower in ("password", "secret", "api_key", "ssn"):
                masked[key] = "***"
            elif key_lower in ("token", "access_token", "refresh_token", "authorization"):
                masked[key] = mask_token(str(value)) if value else "***"
            elif key_lower == "email":
                masked[key] = mask_email(str(value)) if value else "***"
            elif key_lower in ("phone", "phone_number", "mobile"):
                masked[key] = mask_phone(str(value)) if value else "***"
            elif key_lower in ("credit_card", "card_number", "cc"):
                masked[key] = mask_credit_card(str(value)) if value else "***"
            else:
                # Recursively mask nested structures
                masked[key] = mask_pii(value)
        
        return masked
    
    elif isinstance(data, list):
        return [mask_pii(item) for item in data]
    
    else:
        return data


# ── Notes for Future Enhancements ──────────────────────────────────────────────
"""
LOG ROTATION (not implemented - handled by Docker/K8s):
- Docker: Use log drivers (json-file with max-size, max-file)
- Kubernetes: Use log aggregation (Fluentd, Loki, CloudWatch)
- Bare metal: Use logging.handlers.RotatingFileHandler or TimedRotatingFileHandler

Example for bare metal:
    "file": {
        "class": "logging.handlers.RotatingFileHandler",
        "filename": "/var/log/cortex/app.log",
        "maxBytes": 10485760,  # 10MB
        "backupCount": 5,
        "formatter": "json",
    }
"""
