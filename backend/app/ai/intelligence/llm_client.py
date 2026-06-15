"""
Cortex Intelligence Client
==========================
Single-provider LLM client backed by **Google Gemini** (``google-genai`` SDK).

This client serves the entire Cortex AI layer — text generation, JSON/structured
output, the news forecaster, explanations, sentiment scoring, event
classification, fake-news detection, and RAG embeddings — through one provider.

It replaces the former NVIDIA NIM → Groq → Ollama fallback chain (LiteLLM +
Instructor).  The public method surface is preserved so existing call sites
(``nlp_engine``, ``event_classifier``, ``fake_news_detector``, ``rag.embedder``,
``explanation_worker``) continue to work unchanged:

    initialize / get_intelligence_client / get_ollama_client
    generate / generate_json / generate_structured / generate_structured_with_usage
    embed / health_check
    LLMProviderError / LLMFallbackExhausted

Design invariants
-----------------
- One Gemini ``genai.Client`` is created at startup and reused (async via
  ``client.aio``).  No per-call client construction.
- Structured output uses Gemini's native JSON-schema mode
  (``response_mime_type='application/json'`` + ``response_schema=<PydanticModel>``)
  and reads the SDK-parsed instance from ``response.parsed`` — no Instructor.
- Latency: ``thinking_level`` defaults to ``"minimal"`` (extended thinking off)
  for fast, predictable replies; callers needing deeper reasoning override it.
- Resilience: transient failures (5xx / 429 / timeout) are retried with
  exponential backoff; permanent failures (400/401/403/404) propagate immediately
  as ``LLMFallbackExhausted`` — retrying cannot fix a misconfiguration.
- Embeddings use ``gemini-embedding-001`` at ``GEMINI_EMBED_DIM`` dimensions;
  sub-3072 vectors are L2-normalized (Gemini does not pre-normalize truncated
  vectors, and cosine/pgvector search assumes unit norm).

Usage
-----
    # FastAPI lifespan startup:
    from app.ai.intelligence.llm_client import CortexIntelligenceClient
    await CortexIntelligenceClient.initialize()

    # All other call sites:
    from app.ai.intelligence.llm_client import get_intelligence_client
    client  = get_intelligence_client()
    text    = await client.generate(prompt="…", system="…")
    typed   = await client.generate_structured(prompt="…", response_model=MyModel)
    vectors = await client.embed(["text a", "text b"])
    status  = await client.health_check()
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Type, TypeVar
from zoneinfo import ZoneInfo

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel
from tenacity import (
    AsyncRetrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.core.config import get_settings
from app.ai.intelligence import request_manager as _rm
from app.ai.intelligence.request_manager import GeminiRateLimitError, Priority  # noqa: F401

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Extract JSON from markdown code fences (defensive — Gemini JSON mode rarely
# fences, but generate_json() callers may not use schema mode).
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```")

# Gemini returns un-normalized vectors for output_dimensionality < 3072.  Cosine
# similarity / pgvector search assume unit norm, so anything below this is
# L2-normalized by embed().
_GEMINI_NATIVE_EMBED_DIM = 3072

# Map the RAG embedder's input_type to Gemini embedding task types.  Using the
# correct task type for corpus vs. query is required for retrieval quality.
_TASK_TYPE_BY_INPUT = {
    "passage": "RETRIEVAL_DOCUMENT",
    "query":   "RETRIEVAL_QUERY",
}

# Quota and retry-hint patterns parsed from Gemini error response bodies.
# Two patterns are needed because the response body format varies:
#
#   Full body (with structured details array):
#     "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
#     → matched by ``GenerateRequestsPerDay``
#
#   Minimal body (details array absent — truncated or simplified responses):
#     "* Quota exceeded for metric: .../generate_content_free_tier_requests, limit: 20"
#     → NOT matched by ``GenerateRequestsPerDay``; matched by ``free_tier_requests``
#
# Both forms are non-recoverable within the current day and must open the circuit
# rather than retry.  Per-minute rate limits use different metric names (e.g.
# ``GenerateRequestsPerMinute``) and do NOT match either pattern.
_DAILY_QUOTA_RE = re.compile(
    r"GenerateRequestsPerDay"   # quotaId in structured details (full body)
    r"|free_tier_requests",     # metric name in human-readable message (minimal body)
    re.IGNORECASE,
)
# Gemini embeds structured retry guidance in 429 bodies:
#   {"@type": "…RetryInfo", "retryDelay": "53s"}
_RETRY_DELAY_RE = re.compile(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"')

# Default estimated token budget for generate calls when the caller does not
# specify max_tokens.  The token buckets credit back the unused portion after
# release(), so over-estimating is safe; under-estimating risks burst overuse.
_DEFAULT_ESTIMATED_TOKENS: int = 1_400


# ── Custom exceptions (preserved for existing call sites) ───────────────────────

class LLMProviderError(Exception):
    """The LLM provider failed after exhausting its retry budget."""


class LLMFallbackExhausted(Exception):
    """The LLM call could not be completed (no key, or unrecoverable failure)."""


class GeminiQuotaExhausted(LLMFallbackExhausted):
    """
    Gemini daily generation quota exhausted.

    Raised instead of retrying when the 429 error body indicates a
    ``GenerateRequestsPerDay`` violation.  All LLM generate calls fast-fail with
    this exception until the circuit resets at midnight Pacific Time.

    Callers that catch ``LLMFallbackExhausted`` handle this automatically.
    Callers that need to distinguish quota exhaustion from other permanent
    failures can catch this subclass specifically.
    """


# ── Provider identity (single provider; retained for back-compat) ───────────────

class LLMProvider(str, Enum):
    GEMINI = "gemini"


# ── Error classification ───────────────────────────────────────────────────────

# Definitive client-side HTTP statuses: bad request, auth, forbidden, not-found.
# These indicate misconfiguration that will not self-heal — never retried.
# 429 (rate limit) is deliberately excluded; it is transient.
_DEFINITIVE_HTTP_STATUSES = frozenset({400, 401, 403, 404})


def _status_code(exc: Exception) -> int | None:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


def _is_permanent(exc: Exception) -> bool:
    """True if ``exc`` is a non-recoverable client/config error (do not retry)."""
    return _status_code(exc) in _DEFINITIVE_HTTP_STATUSES


def _is_transient(exc: Exception) -> bool:
    """True if ``exc`` is a recoverable blip worth retrying (5xx / 429 / network)."""
    if isinstance(exc, (asyncio.TimeoutError, genai_errors.ServerError)):
        return True
    code = _status_code(exc)
    if code == 429:
        return True
    if code is not None and 500 <= code < 600:
        return True
    # Connection/transport errors carry no HTTP status — treat as transient.
    return code is None and isinstance(exc, genai_errors.APIError)


class _TransientLLMError(Exception):
    """
    Internal marker so tenacity retries only transient failures.

    ``retry_delay_secs`` carries the server's own ``retryDelay`` hint when
    present.  ``_gemini_wait`` reads it to honour the server's recovery estimate
    instead of guessing with exponential jitter.
    """

    __slots__ = ("retry_delay_secs",)

    def __init__(self, msg: str, retry_delay_secs: float | None = None) -> None:
        super().__init__(msg)
        self.retry_delay_secs = retry_delay_secs


# ── Quota circuit — process-lifetime state ────────────────────────────────────
# Daily Gemini quota exhaustion opens this circuit until the next reset point
# (midnight Pacific Time).  While open, every LLM generate call fast-fails in
# under a millisecond rather than spending seconds on retries that are
# guaranteed to receive another 429.
#
# State is in-process memory — no Redis dependency — which is correct here:
# the asyncio event loop is single-threaded (no concurrent mutation), and a
# restart after quota exhaustion will yield at most one fresh 429 before the
# circuit reopens.  The benefit of simplicity outweighs the marginal cost.

_quota_open_until: datetime | None = None


def _quota_reset_at() -> datetime:
    """Return the next Gemini daily quota reset — midnight Pacific Time as UTC."""
    pt = ZoneInfo("America/Los_Angeles")
    now_pt = datetime.now(pt)
    midnight_tomorrow_pt = (now_pt + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return midnight_tomorrow_pt.astimezone(timezone.utc)


def _quota_exhausted() -> bool:
    """True if the quota circuit is currently open."""
    global _quota_open_until
    if _quota_open_until is None:
        return False
    if datetime.now(timezone.utc) >= _quota_open_until:
        _quota_open_until = None  # auto-close: quota has reset
        logger.info("llm: daily Gemini quota circuit auto-closed — quota has reset")
        return False
    return True


def _open_quota_circuit(op: str = _rm.Operation.GENERATE) -> None:
    """
    Open the in-process quota circuit for ``op`` until the next daily reset.

    Also delegates to the GeminiRequestManager (when initialized) so that:
    - All queued permits for ``op`` are cancelled immediately.
    - The circuit state is persisted to Redis for crash-restart recovery.

    Idempotent: a second call within the same day is a no-op unless the new
    reset time is later than the currently stored one (defensive — normally
    reset times within the same calendar day are identical).
    """
    global _quota_open_until
    reset = _quota_reset_at()
    if _quota_open_until is None or reset > _quota_open_until:
        _quota_open_until = reset
        logger.error(
            "llm: daily Gemini %s quota EXHAUSTED — circuit OPEN until %s UTC. "
            "All LLM %s calls will fast-fail until then. "
            "Provision a paid Gemini API key (GEMINI_API_KEY) to eliminate this limit.",
            op, reset.strftime("%Y-%m-%dT%H:%M:%SZ"), op,
        )
    manager = _try_get_request_manager()
    if manager is not None:
        manager.open_circuit(op)


def _check_quota_circuit(op: str) -> None:
    """Raise ``GeminiQuotaExhausted`` immediately if the daily quota circuit is open."""
    if _quota_exhausted():
        reset = _quota_open_until
        raise GeminiQuotaExhausted(
            f"Gemini {op} skipped — daily quota exhausted, circuit open until "
            f"{reset.strftime('%Y-%m-%dT%H:%M:%SZ') if reset else 'unknown'} UTC."
        )


def _try_get_request_manager() -> _rm.GeminiRequestManager | None:
    """Return the GeminiRequestManager singleton if initialized, None otherwise.

    Accessing the class variable directly avoids the RuntimeError raised by
    get_request_manager() when the manager hasn't been started — allowing
    _acall() and embed() to degrade gracefully in unit tests and early startup
    code paths without forcing the manager to be running.
    """
    return _rm.GeminiRequestManager._instance


def _is_daily_quota_exhausted(exc: Exception) -> bool:
    """
    True when *exc* carries a Gemini daily-quota violation.

    Matches ``GenerateRequestsPerDay`` in the error body, which is present in
    both the free-tier variant (``-FreeTier`` suffix) and paid-tier daily caps.
    Per-minute rate limits (``GenerateRequestsPerMinute``) do NOT match and are
    left to the normal transient-retry path.
    """
    return bool(_DAILY_QUOTA_RE.search(str(exc)))


def _extract_retry_delay_secs(exc: Exception) -> float | None:
    """
    Parse the ``retryDelay`` hint (seconds) from a Gemini 429 error body.

    Gemini embeds structured retry guidance in its 429 responses::

        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "53s"}

    Using this value instead of exponential jitter aligns the client's backoff
    with the server's own recovery estimate, preventing premature retries against
    a still-rate-limited endpoint.  Returns ``None`` when the hint is absent.
    """
    m = _RETRY_DELAY_RE.search(str(exc))
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


# Pre-instantiated default wait for non-hinted retries (5xx, network, timeout).
_DEFAULT_WAIT = wait_exponential_jitter(initial=1, max=20, jitter=1)


def _gemini_wait(retry_state: Any) -> float:
    """
    Tenacity wait strategy that honours Gemini's own ``retryDelay`` hint.

    When the active exception is a ``_TransientLLMError`` that carries an
    explicit server hint, that value is returned directly.  This keeps the
    client in sync with the server's recovery timeline rather than guessing
    with exponential jitter.  All other transient errors (5xx, network,
    timeout) fall back to ``wait_exponential_jitter``.
    """
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, _TransientLLMError) and exc.retry_delay_secs is not None:
        return float(exc.retry_delay_secs)
    return _DEFAULT_WAIT(retry_state)


# ── Client ─────────────────────────────────────────────────────────────────────

class CortexIntelligenceClient:
    """
    Gemini-backed LLM client for the Cortex Intelligence Layer.

    Obtain via ``get_intelligence_client()`` after calling
    ``await CortexIntelligenceClient.initialize()`` at startup.
    Do not instantiate directly.
    """

    def __init__(self) -> None:  # pragma: no cover
        raise RuntimeError(
            "Do not instantiate CortexIntelligenceClient directly. "
            "Call `await CortexIntelligenceClient.initialize()` from the FastAPI "
            "lifespan, then use get_intelligence_client()."
        )

    # ── Startup ────────────────────────────────────────────────────────────────

    @classmethod
    async def initialize(cls) -> None:
        """
        Create the singleton, build the Gemini client, and log the active model.
        Safe to call multiple times — subsequent calls are no-ops.
        """
        global _client
        if _client is not None:
            return

        settings = get_settings()
        instance: CortexIntelligenceClient = object.__new__(cls)
        instance._configure(settings)
        _client = instance

        if instance._genai is None:
            logger.warning(
                "LLM backend: Gemini NOT configured — set GEMINI_API_KEY in .env. "
                "All LLM/embedding calls will raise until a key is provided."
            )
        else:
            logger.info(
                "LLM backend: gemini/%s (embed=%s @ %d-dim, thinking=%s)",
                instance._model, instance._embed_model,
                instance._embed_dim, instance._thinking_level,
            )

    # ── Public API ─────────────────────────────────────────────────────────────

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        *,
        priority: Priority = Priority.MEDIUM,
    ) -> dict[str, Any]:
        """
        Unstructured text generation.

        Returns:
            {
                "content":           str,
                "model":             str,
                "provider":          "gemini",
                "prompt_tokens":     int,
                "completion_tokens": int,
                "latency_ms":        int,
            }
        """
        config = self._gen_config(
            system=system, temperature=temperature, max_tokens=max_tokens,
        )
        response, latency_ms = await self._acall(
            prompt, config,
            priority=priority,
            estimated_tokens=max_tokens or _DEFAULT_ESTIMATED_TOKENS,
        )
        in_tok, out_tok = _usage_tokens(response)

        logger.info(
            "llm.generate provider=gemini prompt_tokens=%d completion_tokens=%d latency_ms=%d",
            in_tok, out_tok, latency_ms,
        )
        return {
            "content":           response.text or "",
            "model":             getattr(response, "model_version", None) or self._model,
            "provider":          LLMProvider.GEMINI.value,
            "prompt_tokens":     in_tok,
            "completion_tokens": out_tok,
            "latency_ms":        latency_ms,
        }

    async def generate_json(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.3,
        *,
        priority: Priority = Priority.MEDIUM,
    ) -> dict[str, Any]:
        """
        Generate a JSON object (schema-free) and return it as a dict.

        Uses Gemini JSON mode (``response_mime_type='application/json'``).  For new
        code that owns the output schema, prefer ``generate_structured()``.
        """
        config = self._gen_config(
            system=system, temperature=temperature, json_mime=True,
        )
        response, _ = await self._acall(prompt, config, priority=priority)
        return _parse_json(response.text or "")

    async def generate_structured(
        self,
        prompt: str,
        response_model: Type[T],
        system: str | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
        *,
        priority: Priority = Priority.MEDIUM,
    ) -> T:
        """
        Typed structured output via Gemini's native JSON-schema mode.

        The validated Pydantic instance is read from ``response.parsed``; if the
        SDK leaves it unset, the raw JSON text is validated against
        ``response_model`` as a fallback.

        Args:
            response_model: Pydantic BaseModel subclass defining the output schema.
            temperature:    Low values (≤0.2) improve adherence.
            max_tokens:     Optional cap on completion tokens.
            priority:       GeminiRequestManager permit priority (keyword-only).
        """
        config = self._gen_config(
            system=system, temperature=temperature, max_tokens=max_tokens,
            response_schema=response_model,
        )
        response, latency_ms = await self._acall(
            prompt, config,
            priority=priority,
            estimated_tokens=max_tokens or _DEFAULT_ESTIMATED_TOKENS,
        )
        result = self._extract_parsed(response, response_model)

        logger.info(
            "llm.generate_structured provider=gemini response_model=%s latency_ms=%d",
            response_model.__name__, latency_ms,
        )
        return result

    async def generate_structured_with_usage(
        self,
        prompt: str,
        response_model: Type[T],
        system: str | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
        *,
        priority: Priority = Priority.HIGH,
    ) -> tuple[T, dict[str, Any]]:
        """
        Identical to ``generate_structured()`` but also returns token-usage
        metadata for ``ai_llm_audit_log`` writers.

        Returns:
            (model_instance, {
                "input_tokens":  int | None,
                "output_tokens": int | None,
                "provider":      "gemini",
                "model_id":      str,
                "latency_ms":    int,
            })
        """
        config = self._gen_config(
            system=system, temperature=temperature, max_tokens=max_tokens,
            response_schema=response_model,
        )
        response, latency_ms = await self._acall(
            prompt, config,
            priority=priority,
            estimated_tokens=max_tokens or _DEFAULT_ESTIMATED_TOKENS,
        )
        result = self._extract_parsed(response, response_model)
        in_tok, out_tok = _usage_tokens(response)

        usage_dict: dict[str, Any] = {
            "input_tokens":  in_tok,
            "output_tokens": out_tok,
            "provider":      LLMProvider.GEMINI.value,
            "model_id":      self._model,
            "latency_ms":    latency_ms,
        }
        logger.info(
            "llm.generate_structured provider=gemini response_model=%s "
            "input_tokens=%s output_tokens=%s latency_ms=%d",
            response_model.__name__, in_tok, out_tok, latency_ms,
        )
        return result, usage_dict

    async def embed(
        self,
        texts: list[str],
        input_type: str = "passage",
        *,
        priority: Priority = Priority.BACKGROUND,
    ) -> list[list[float]]:
        """
        Generate embeddings via ``gemini-embedding-001`` at GEMINI_EMBED_DIM dims.

        Args:
            texts:      Strings to embed.
            input_type: "passage" (RETRIEVAL_DOCUMENT) for indexing, "query"
                        (RETRIEVAL_QUERY) for retrieval.  Using the correct task
                        type for corpus vs. query is required for retrieval quality.
            priority:   GeminiRequestManager permit priority (keyword-only).
                        Defaults to BACKGROUND — embed work must not compete
                        with the live signal pipeline.

        Returns:
            One float vector per input, L2-normalized when the dimension < 3072.

        Raises:
            LLMFallbackExhausted: Gemini is not configured, or the call failed.
            GeminiQuotaExhausted: Daily embed quota exhausted.
            GeminiRateLimitError: Manager queue full or permit timeout elapsed.
        """
        if not texts:
            return []
        self._require_client()

        task_type = _TASK_TYPE_BY_INPUT.get(input_type, "RETRIEVAL_DOCUMENT")
        config = genai_types.EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=self._embed_dim,
            http_options=self._http_options(),
        )

        manager = _try_get_request_manager()
        permit = None
        if manager is not None:
            try:
                permit = await manager.acquire(
                    _rm.Operation.EMBED, priority, len(texts)
                )
            except _rm.GeminiQuotaExhausted as exc:
                raise GeminiQuotaExhausted(str(exc)) from exc
            # GeminiRateLimitError propagates as-is — callers should degrade gracefully.

        async def _do() -> list[list[float]]:
            resp = await self._genai.aio.models.embed_content(
                model=self._embed_model, contents=texts, config=config,
            )
            vectors = [list(e.values) for e in (resp.embeddings or [])]
            if self._embed_dim < _GEMINI_NATIVE_EMBED_DIM:
                vectors = [_l2_normalize(v) for v in vectors]
            return vectors

        try:
            vectors = await self._retry(_do, op="embed")
        except Exception:
            if permit is not None:
                manager.release(permit, outcome="error")
            raise

        if permit is not None:
            manager.release(permit, outcome="success")

        logger.debug(
            "llm.embed provider=gemini task_type=%s count=%d dim=%d",
            task_type, len(vectors), self._embed_dim,
        )
        return vectors

    @property
    def model_id(self) -> str:
        """Serving model identifier for audit/logging, e.g. ``gemini/gemini-2.5-flash``."""
        return f"{LLMProvider.GEMINI.value}/{self._model}"

    async def aclose(self) -> None:
        """Close the underlying Gemini transport.  Call once at application shutdown.

        The async httpx transport inside genai.Client is long-lived; closing it
        avoids leaked connections / ResourceWarnings on shutdown.
        """
        if self._genai is None:
            return
        try:
            await self._genai.aio.aclose()
        except Exception as exc:  # noqa: BLE001 — shutdown best-effort
            logger.debug("llm: transport close failed (non-fatal): %s", exc)

    async def health_check(self) -> dict[str, str]:
        """Probe Gemini with a 1-token completion.  Returns status + model info."""
        result: dict[str, str] = {
            "provider":    LLMProvider.GEMINI.value,
            "model":       self._model,
            "embed_model": self._embed_model,
            "embed_dim":   str(self._embed_dim),
        }
        if self._genai is None:
            result["gemini"] = "not_configured"
            return result
        try:
            await self._genai.aio.models.generate_content(
                model=self._model,
                contents="ping",
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=1,
                    thinking_config=self._thinking_config(),
                    http_options=self._http_options(),
                ),
            )
            result["gemini"] = "healthy"
        except Exception as exc:  # noqa: BLE001 — surfaced as a status string
            result["gemini"] = f"unhealthy ({type(exc).__name__})"
        return result

    # ── Private — configuration ────────────────────────────────────────────────

    def _configure(self, settings: Any) -> None:
        self._model: str = settings.GEMINI_MODEL
        self._embed_model: str = settings.GEMINI_EMBED_MODEL
        self._embed_dim: int = settings.GEMINI_EMBED_DIM
        self._thinking_level: str = settings.GEMINI_THINKING_LEVEL
        self._max_retries: int = settings.LLM_MAX_RETRIES
        self._timeout: float = settings.LLM_REQUEST_TIMEOUT
        self._api_key: str | None = settings.GEMINI_API_KEY

        self._genai: genai.Client | None = (
            genai.Client(api_key=self._api_key) if self._api_key else None
        )

    def _require_client(self) -> None:
        if self._genai is None:
            raise LLMFallbackExhausted(
                "Gemini is not configured. Set GEMINI_API_KEY in backend/.env "
                "and restart."
            )

    def _http_options(self) -> genai_types.HttpOptions:
        # google-genai timeout is in milliseconds.
        return genai_types.HttpOptions(timeout=int(self._timeout * 1000))

    def _thinking_config(self) -> genai_types.ThinkingConfig | None:
        """Build the thinking config appropriate to the model generation.

        Gemini 3.x exposes the ``thinking_level`` string enum
        ("minimal"/"low"/"medium"/"high").  Gemini 2.x predates it and only
        accepts the integer ``thinking_budget`` (it 400s on ``thinking_level``).
        For 2.x we map "minimal" → ``thinking_budget=0`` (thinking off) and leave
        deeper levels to the model default.  Returns None when no thinking config
        should be sent.
        """
        level = self._thinking_level
        if _model_uses_thinking_budget(self._model):
            return genai_types.ThinkingConfig(thinking_budget=0) if level == "minimal" else None
        return genai_types.ThinkingConfig(thinking_level=level)

    def _gen_config(
        self,
        *,
        system: str | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
        response_schema: Type[BaseModel] | None = None,
        json_mime: bool = False,
    ) -> genai_types.GenerateContentConfig:
        """Assemble a GenerateContentConfig from the common call parameters."""
        kwargs: dict[str, Any] = {
            "temperature":     temperature,
            "thinking_config": self._thinking_config(),
            "http_options":    self._http_options(),
            "safety_settings": _PERMISSIVE_SAFETY_SETTINGS,
        }
        if system:
            kwargs["system_instruction"] = system
        if max_tokens is not None:
            kwargs["max_output_tokens"] = max_tokens
        if response_schema is not None:
            kwargs["response_mime_type"] = "application/json"
            kwargs["response_schema"] = response_schema
        elif json_mime:
            kwargs["response_mime_type"] = "application/json"
        return genai_types.GenerateContentConfig(**kwargs)

    # ── Private — request execution ────────────────────────────────────────────

    async def _acall(
        self,
        contents: Any,
        config: genai_types.GenerateContentConfig,
        *,
        priority: Priority = Priority.MEDIUM,
        estimated_tokens: int = _DEFAULT_ESTIMATED_TOKENS,
    ) -> tuple[Any, int]:
        """Execute one generate_content with quota management and retry.  Returns (response, latency_ms)."""
        self._require_client()
        t0 = time.monotonic()

        manager = _try_get_request_manager()
        permit = None
        if manager is not None:
            try:
                permit = await manager.acquire(
                    _rm.Operation.GENERATE, priority, estimated_tokens
                )
            except _rm.GeminiQuotaExhausted as exc:
                # Translate so callers catching LLMFallbackExhausted or
                # llm_client.GeminiQuotaExhausted continue to work unchanged.
                raise GeminiQuotaExhausted(str(exc)) from exc
            # GeminiRateLimitError propagates as-is — callers should degrade gracefully.

        async def _do() -> Any:
            return await self._genai.aio.models.generate_content(
                model=self._model, contents=contents, config=config,
            )

        try:
            response = await self._retry(_do, op="generate")
        except Exception:
            if permit is not None:
                manager.release(permit, outcome="error")
            raise

        if permit is not None:
            manager.release(permit, outcome="success")

        return response, int((time.monotonic() - t0) * 1000)

    async def _retry(self, fn: Any, *, op: str) -> Any:
        """
        Run ``fn`` with bounded retries on transient errors only.

        Error routing
        -------------
        - **Circuit open** (daily quota known exhausted): raises
          ``GeminiQuotaExhausted`` immediately — zero network calls.
        - **Daily quota 429** (``GenerateRequestsPerDay`` in body): opens the
          circuit, raises ``GeminiQuotaExhausted`` — no retry.
        - **Transient 429 / 5xx / network / timeout**: retried up to
          ``LLM_MAX_RETRIES`` times.  The wait between attempts uses Gemini's
          own ``retryDelay`` hint when present; otherwise falls back to
          ``wait_exponential_jitter``.
        - **Permanent 4xx** (400/401/403/404): raises ``LLMFallbackExhausted``
          immediately — retrying cannot fix a misconfiguration.
        - **Unknown exception**: raises ``LLMFallbackExhausted`` immediately —
          silent retry of an unclassified error is unsafe.
        """
        # Fast-fail if the daily quota is already known exhausted.
        _check_quota_circuit(op)

        last_exc: Exception | None = None
        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type(_TransientLLMError),
                stop=stop_after_attempt(self._max_retries),
                wait=_gemini_wait,
                before_sleep=before_sleep_log(logger, logging.WARNING),
                reraise=True,
            ):
                with attempt:
                    try:
                        return await fn()
                    except Exception as exc:  # noqa: BLE001 — classified below
                        last_exc = exc
                        if _is_permanent(exc):
                            raise LLMFallbackExhausted(
                                f"Gemini {op} failed (permanent {_status_code(exc)}): "
                                f"{type(exc).__name__}: {exc}"
                            ) from exc
                        if _is_daily_quota_exhausted(exc):
                            _open_quota_circuit(op)
                            raise GeminiQuotaExhausted(
                                f"Gemini {op} aborted — daily quota exhausted. "
                                f"Resets at {_quota_reset_at().strftime('%Y-%m-%dT%H:%M:%SZ')} UTC. "
                                f"Provision a paid Gemini API key to eliminate this limit."
                            ) from exc
                        if _is_transient(exc):
                            delay = _extract_retry_delay_secs(exc)
                            raise _TransientLLMError(str(exc), retry_delay_secs=delay) from exc
                        # Unknown — do not silently retry an unclassified error.
                        raise LLMFallbackExhausted(
                            f"Gemini {op} failed: {type(exc).__name__}: {exc}"
                        ) from exc
        except _TransientLLMError as exc:
            raise LLMFallbackExhausted(
                f"Gemini {op} failed after {self._max_retries} attempts "
                f"(last: {type(last_exc).__name__}: {last_exc})"
            ) from (last_exc or exc)

    def _extract_parsed(self, response: Any, response_model: Type[T]) -> T:
        """Return the validated model from response.parsed, or validate the raw text."""
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, response_model):
            return parsed
        text = response.text or ""
        if not text.strip():
            raise LLMFallbackExhausted(
                f"Gemini returned no parseable output for {response_model.__name__} "
                f"(finish_reason may indicate a block or truncation)."
            )
        try:
            return response_model.model_validate_json(text)
        except Exception as exc:
            raise LLMFallbackExhausted(
                f"Gemini output failed {response_model.__name__} validation: "
                f"{type(exc).__name__}: {exc}. Preview: {text[:200]!r}"
            ) from exc


# ── Safety settings — permissive (avoid false-positive blocks on finance text) ──
# A trading-analysis tool must not have benign market commentary suppressed by
# the default content filters.  Output guardrails are enforced separately in the
# explanation worker.
_PERMISSIVE_SAFETY_SETTINGS = [
    genai_types.SafetySetting(category=cat, threshold="BLOCK_NONE")
    for cat in (
        "HARM_CATEGORY_HARASSMENT",
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_DANGEROUS_CONTENT",
    )
]


# ── Module-level helpers ───────────────────────────────────────────────────────

def _model_uses_thinking_budget(model: str) -> bool:
    """True for model generations that use integer thinking_budget, not thinking_level.

    Gemini 3.x introduced the ``thinking_level`` enum; 1.x/2.x only accept
    ``thinking_budget`` and 400 on ``thinking_level``.
    """
    m = model.lower()
    return any(m.startswith(p) for p in ("gemini-1.", "gemini-2."))


def _usage_tokens(response: Any) -> tuple[int, int]:
    """Extract (prompt_tokens, completion_tokens) from a Gemini response."""
    usage = getattr(response, "usage_metadata", None)
    in_tok = getattr(usage, "prompt_token_count", None) or 0
    out_tok = getattr(usage, "candidates_token_count", None) or 0
    return int(in_tok), int(out_tok)


def _l2_normalize(vector: list[float]) -> list[float]:
    """Return the unit-norm vector (no-op for a zero vector)."""
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        return vector
    return [x / norm for x in vector]


def _parse_json(content: str) -> dict[str, Any]:
    """Parse a JSON object from model output, tolerating markdown code fences."""
    fence = _CODE_FENCE_RE.search(content)
    raw = fence.group(1).strip() if fence else content.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("llm: JSON parse failed. preview=%r error=%s", raw[:300], exc)
        raise ValueError(
            f"LLM returned non-JSON output: {exc}. Content preview: {raw[:200]!r}"
        ) from exc


# ── Singleton registry ─────────────────────────────────────────────────────────

_client: CortexIntelligenceClient | None = None


def get_intelligence_client() -> CortexIntelligenceClient:
    """
    Return the CortexIntelligenceClient singleton.

    If initialize() was not called at startup, a lazy instance is created from
    settings.  A warning is logged — production usage always calls initialize().
    """
    global _client
    if _client is not None:
        return _client

    settings = get_settings()
    instance: CortexIntelligenceClient = object.__new__(CortexIntelligenceClient)
    instance._configure(settings)
    _client = instance
    logger.warning(
        "CortexIntelligenceClient lazy-initialized (gemini/%s). "
        "Call `await CortexIntelligenceClient.initialize()` at startup.",
        instance._model,
    )
    return _client


def get_ollama_client() -> CortexIntelligenceClient:
    """
    Backward-compatibility alias for get_intelligence_client().

    Retained so existing callers (EventClassifier, FakeNewsDetector) keep working;
    all calls route through the Gemini-backed client.
    """
    return get_intelligence_client()


async def close_intelligence_client() -> None:
    """Close the singleton client's Gemini transport at app shutdown (no-op if unset)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
