"""
Cortex Intelligence Client
==========================
Provider-agnostic LLM client with an ordered N-provider fallback chain.

Provider chain (resolved at startup, highest priority first):
  1. NVIDIA NIM  — if NVIDIA_NIM_API_KEY is set and the endpoint responds.
  2. Groq        — if GROQ_API_KEY is set (60 RPM free tier, Qwen3-32B, ~300ms TTFT).
  3. Ollama      — always last; zero-cost local fallback, no API key required.

Providers absent from the chain (key not set or probe failed) are skipped.
Ollama is always appended so the chain is never empty.

Design invariants
-----------------
- Every startup emits one log line naming the full ordered chain — silent fallback
  is prohibited; operators always know which backend is serving requests.
- Only TRANSIENT failures (rate-limit, timeout, service-unavailable, connection)
  trigger a move to the next provider.  PERMANENT failures (auth, bad-request,
  context-window) propagate immediately — the next provider cannot fix these.
- Every successful fallback increments llm_fallbacks_total{provider_from, provider_to}.
- Structured output uses Instructor: Mode.TOOLS for NIM and Groq (both serve
  Qwen3 which supports native tool-calling); Mode.JSON for Ollama (llama3.1).
- Embeddings are NIM-only (nvidia/nv-embedqa-e5-v5, 1024-dim).  Groq and Ollama
  produce incompatible vector dimensions; they are not offered as embedding fallbacks.
- generate_structured() callers MUST pass max_tokens explicitly.  NIM's default
  (128 tokens) truncates any structured response, causing Instructor to exhaust
  its retry budget.  Sentinel: sentiment=256, explanation=1500.

Usage
-----
    # FastAPI lifespan startup:
    from app.ai.intelligence.llm_client import CortexIntelligenceClient
    await CortexIntelligenceClient.initialize()

    # All other call sites:
    from app.ai.intelligence.llm_client import get_intelligence_client
    client  = get_intelligence_client()
    text    = await client.generate(prompt="…", system="…")
    typed   = await client.generate_structured(prompt="…", response_model=MyModel,
                                               max_tokens=512)
    vectors = await client.embed(["text a", "text b"])
    status  = await client.health_check()
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from enum import Enum
from typing import Any, Type, TypeVar

import instructor
import litellm
from pydantic import BaseModel
from tenacity import (
    AsyncRetrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.core.config import get_settings

# ── LiteLLM global configuration ──────────────────────────────────────────────
litellm.suppress_debug_info = True
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

# Register models absent from LiteLLM's bundled cost map to suppress DEBUG noise.
# $0 pricing is accurate for the NIM and Groq free tiers.
litellm.register_model({
    # NVIDIA NIM — Qwen3 MoE (added to NIM after last litellm release)
    "nvidia_nim/qwen/qwen3.5-122b-a10b": {
        "max_tokens":            32768,
        "input_cost_per_token":  0.0,
        "output_cost_per_token": 0.0,
        "litellm_provider":      "nvidia_nim",
        "mode":                  "chat",
    },
    # Groq — Qwen3-32B (free tier, LPU-accelerated)
    "groq/qwen/qwen3-32b": {
        "max_tokens":            32768,
        "input_cost_per_token":  0.0,
        "output_cost_per_token": 0.0,
        "litellm_provider":      "groq",
        "mode":                  "chat",
    },
})

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Ollama fallback embedding model (incompatible with pgvector VECTOR(1024) column —
# kept here for reference only; embeddings always use NIM).
_OLLAMA_EMBED_FALLBACK = "ollama/nomic-embed-text"

# Strip Qwen3 chain-of-thought tags produced when thinking mode is active.
_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

# Extract JSON from markdown code fences (```json ... ``` or ``` ... ```).
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```")


# ── Custom exceptions ──────────────────────────────────────────────────────────

class LLMProviderError(Exception):
    """A specific provider failed after exhausting its retry budget."""


class LLMFallbackExhausted(Exception):
    """Every provider in the fallback chain failed."""


# ── Provider identity ──────────────────────────────────────────────────────────

class LLMProvider(str, Enum):
    NIM    = "nim"
    GROQ   = "groq"
    OLLAMA = "ollama"


# ── Error classification ───────────────────────────────────────────────────────

_TRANSIENT: tuple[type[Exception], ...] = (
    litellm.RateLimitError,
    litellm.Timeout,
    litellm.ServiceUnavailableError,
    litellm.APIConnectionError,
)

_PERMANENT: tuple[type[Exception], ...] = (
    litellm.AuthenticationError,
    litellm.BadRequestError,
    litellm.ContextWindowExceededError,
)


# ── Client ─────────────────────────────────────────────────────────────────────

class CortexIntelligenceClient:
    """
    Provider-agnostic LLM client for the Cortex Intelligence Layer.

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
        Create the singleton, configure providers, probe backends, and log the
        active provider chain.  Safe to call multiple times — subsequent calls
        are no-ops.
        """
        global _client
        if _client is not None:
            return

        settings = get_settings()
        instance: CortexIntelligenceClient = object.__new__(cls)
        instance._configure(settings)

        # Register singleton before probe so an unexpected probe failure never
        # leaves the global as None.
        _client = instance

        await instance._build_provider_chain(settings)

    # ── Public API ─────────────────────────────────────────────────────────────

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """
        Unstructured text generation via the provider chain.

        Returns:
            {
                "content":           str   — model output (think-tags stripped),
                "model":             str   — model identifier reported by the API,
                "provider":          str   — "nim" | "groq" | "ollama",
                "prompt_tokens":     int,
                "completion_tokens": int,
                "latency_ms":        int,
            }
        """
        messages = _build_messages(prompt, system)

        async def _call(provider: LLMProvider) -> Any:
            return await self._completion_with_retry(
                provider=provider,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )

        t0 = time.monotonic()
        provider, response = await self._try_providers(_call)
        latency_ms = int((time.monotonic() - t0) * 1000)

        content = _strip_thinking(response.choices[0].message.content or "")
        usage = response.usage

        logger.info(
            "llm.generate provider=%s prompt_tokens=%d completion_tokens=%d latency_ms=%d",
            provider.value,
            usage.prompt_tokens if usage else 0,
            usage.completion_tokens if usage else 0,
            latency_ms,
        )
        return {
            "content":           content,
            "model":             response.model or self._model_name(provider),
            "provider":          provider.value,
            "prompt_tokens":     usage.prompt_tokens if usage else 0,
            "completion_tokens": usage.completion_tokens if usage else 0,
            "latency_ms":        latency_ms,
        }

    async def generate_json(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.3,
    ) -> dict[str, Any]:
        """
        Generate a JSON-structured dict from the model.

        Backward-compatible with the former OllamaClient.generate_json() signature.
        A JSON-only instruction is appended to the system prompt; the output is
        parsed and returned as a dict.

        For new code that owns the output schema, prefer generate_structured()
        which adds Pydantic validation and Instructor retry logic.
        """
        json_system = (
            (system or "").rstrip()
            + "\n\nYou MUST respond with a valid JSON object only. "
              "No markdown fences, no explanation — raw JSON only."
        )
        response = await self.generate(
            prompt=prompt,
            system=json_system,
            temperature=temperature,
        )
        return _parse_json(response["content"])

    async def generate_structured(
        self,
        prompt: str,
        response_model: Type[T],
        system: str | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> T:
        """
        Typed structured output via Instructor + Pydantic validation.

        Uses Mode.TOOLS on NIM and Groq (Qwen3 supports tool-calling on both),
        and Mode.JSON on Ollama (llama3.1 does not).  Instructor retries up to
        2 times internally on structurally invalid output before propagating.

        Args:
            prompt:         User prompt.
            response_model: Pydantic BaseModel subclass defining the output schema.
            system:         Optional system prompt.
            temperature:    Low values (≤0.2) improve JSON adherence.
            max_tokens:     Maximum tokens in the completion.  Always set this
                            explicitly — NIM and Groq default to 128 tokens which
                            truncates any non-trivial structured response, causing
                            Instructor to exhaust its retry budget silently.
                            Sentinel values: sentiment=256, explanation=1500.

        Returns:
            A fully validated instance of ``response_model``.
        """
        messages = _build_messages(prompt, system)

        async def _call(provider: LLMProvider) -> T:
            extra: dict = {}
            if max_tokens is not None:
                extra["max_tokens"] = max_tokens
            return await self._instructor_for(provider).chat.completions.create(
                **self._provider_kwargs(provider),
                response_model=response_model,
                messages=messages,
                temperature=temperature,
                timeout=self._timeout,
                max_retries=2,
                **extra,
            )

        t0 = time.monotonic()
        provider, result = await self._try_providers(_call)
        latency_ms = int((time.monotonic() - t0) * 1000)

        logger.info(
            "llm.generate_structured provider=%s response_model=%s latency_ms=%d",
            provider.value,
            response_model.__name__,
            latency_ms,
        )
        return result

    async def generate_structured_with_usage(
        self,
        prompt: str,
        response_model: Type[T],
        system: str | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> tuple[T, dict[str, Any]]:
        """
        Identical to generate_structured() but also returns token-usage metadata.

        Uses Instructor's ``create_with_completion()`` which returns the raw API
        response alongside the validated Pydantic model, making prompt_tokens and
        completion_tokens available for audit logging and cost tracking.

        Returns:
            (model_instance, usage_dict) where usage_dict contains:
                {
                    "input_tokens":    int | None,
                    "output_tokens":   int | None,
                    "provider":        str,   — "nim" | "groq" | "ollama"
                    "model_id":        str,   — model name as reported by the API
                    "latency_ms":      int,
                }

        Use this variant in any call site that writes to ai_llm_audit_log.
        Use generate_structured() everywhere else (lighter return type).
        """
        messages = _build_messages(prompt, system)

        async def _call(provider: LLMProvider) -> tuple[T, Any]:
            extra: dict = {}
            if max_tokens is not None:
                extra["max_tokens"] = max_tokens
            return await self._instructor_for(provider).chat.completions.create_with_completion(
                **self._provider_kwargs(provider),
                response_model=response_model,
                messages=messages,
                temperature=temperature,
                timeout=self._timeout,
                max_retries=2,
                **extra,
            )

        t0 = time.monotonic()
        provider, (result, raw_completion) = await self._try_providers(_call)
        latency_ms = int((time.monotonic() - t0) * 1000)

        usage = getattr(raw_completion, "usage", None)
        usage_dict: dict[str, Any] = {
            "input_tokens":  getattr(usage, "prompt_tokens", None),
            "output_tokens": getattr(usage, "completion_tokens", None),
            "provider":      provider.value,
            "model_id":      self._model_name(provider),
            "latency_ms":    latency_ms,
        }

        logger.info(
            "llm.generate_structured provider=%s response_model=%s "
            "input_tokens=%s output_tokens=%s latency_ms=%d",
            provider.value,
            response_model.__name__,
            usage_dict["input_tokens"],
            usage_dict["output_tokens"],
            latency_ms,
        )
        return result, usage_dict

    async def embed(
        self,
        texts: list[str],
        input_type: str = "passage",
    ) -> list[list[float]]:
        """
        Generate embeddings via NVIDIA NIM (nvidia/nv-embedqa-e5-v5, 1024-dim).

        Groq and Ollama do not produce 1024-dim vectors compatible with the
        pgvector VECTOR(1024) column and are not offered as fallbacks.

        Args:
            texts:      Strings to embed.  Batch up to RAG_EMBED_BATCH_SIZE.
            input_type: "passage" for document indexing, "query" for retrieval.
                        nv-embedqa-e5-v5 requires correct input_type — the wrong
                        value degrades retrieval accuracy measurably.

        Raises:
            LLMFallbackExhausted: NIM is unavailable or NVIDIA_NIM_API_KEY is unset.
        """
        if not texts:
            return []

        if not self._nim_api_key:
            raise LLMFallbackExhausted(
                "Embeddings require NVIDIA NIM (1024-dim vectors). "
                "Set NVIDIA_NIM_API_KEY in backend/.env and restart."
            )

        last_exc: Exception | None = None
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(_TRANSIENT),
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential_jitter(initial=1, max=30, jitter=2),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=False,
        ):
            with attempt:
                try:
                    response = await litellm.aembedding(
                        model=self._nim_embed_model,
                        input=texts,
                        api_base=self._nim_base_url,
                        api_key=self._nim_api_key,
                        timeout=self._timeout,
                        input_type=input_type,
                    )
                    vectors: list[list[float]] = [
                        item["embedding"] for item in response["data"]
                    ]
                    logger.debug(
                        "llm.embed provider=nim input_type=%s count=%d",
                        input_type, len(vectors),
                    )
                    return vectors
                except _PERMANENT as exc:
                    raise LLMFallbackExhausted(
                        f"Embedding call failed (permanent error): "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                except Exception as exc:
                    last_exc = exc
                    raise

        raise LLMFallbackExhausted(
            f"Embedding failed after {self._max_retries} attempts. "
            f"Last error ({type(last_exc).__name__}): {last_exc}"
        ) from last_exc

    async def health_check(self) -> dict[str, str]:
        """
        Probe all known providers with a minimal completion call.

        Returns a dict with keys for each provider plus chain summary:
            primary, chain, nim, groq, ollama, nim_model, groq_model, ollama_model
        Values for provider keys: "healthy" | "not_configured" | "unhealthy (<ExcType>)"
        """
        result: dict[str, str] = {
            "primary":       self._primary.value,
            "chain":         " → ".join(p.value for p in self._provider_chain),
            "nim_model":     self._nim_model_name,
            "groq_model":    self._groq_model_name,
            "ollama_model":  self._ollama_model_name,
        }

        for provider in LLMProvider:
            configured = self._is_configured(provider)
            if not configured:
                result[provider.value] = "not_configured"
                continue
            try:
                await litellm.acompletion(
                    **self._provider_kwargs(provider),
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1,
                    timeout=10,
                )
                result[provider.value] = "healthy"
            except Exception as exc:
                result[provider.value] = f"unhealthy ({type(exc).__name__})"

        return result

    # ── Private — configuration ────────────────────────────────────────────────

    def _configure(self, settings: Any) -> None:
        """Populate instance attributes from application settings."""
        # NVIDIA NIM
        self._nim_api_key: str | None = settings.NVIDIA_NIM_API_KEY
        self._nim_base_url: str = settings.NVIDIA_NIM_BASE_URL
        self._nim_model: str = f"nvidia_nim/{settings.NVIDIA_NIM_MODEL}"
        self._nim_embed_model: str = f"nvidia_nim/{settings.NVIDIA_NIM_EMBED_MODEL}"
        self._nim_model_name: str = settings.NVIDIA_NIM_MODEL

        # Groq
        self._groq_api_key: str | None = settings.GROQ_API_KEY
        self._groq_base_url: str = settings.GROQ_BASE_URL
        self._groq_model: str = f"groq/{settings.GROQ_MODEL}"
        self._groq_model_name: str = settings.GROQ_MODEL

        # Ollama
        self._ollama_model: str = f"ollama/{settings.OLLAMA_MODEL}"
        self._ollama_base_url: str = settings.OLLAMA_BASE_URL
        self._ollama_model_name: str = settings.OLLAMA_MODEL

        # Behaviour
        self._max_retries: int = settings.LLM_MAX_RETRIES
        self._timeout: float = settings.LLM_REQUEST_TIMEOUT

        # Provider chain — will be set by _build_provider_chain(); initialised
        # to Ollama-only so any unexpected probe failure leaves a working chain.
        self._provider_chain: list[LLMProvider] = [LLMProvider.OLLAMA]
        self._primary: LLMProvider = LLMProvider.OLLAMA

        # Instructor clients
        # Mode.TOOLS: NIM and Groq both serve Qwen3, which supports tool-calling.
        # Mode.JSON: Ollama (llama3.1:8b) does not support tool-calling.
        self._nim_instructor: instructor.AsyncInstructor = instructor.from_litellm(
            litellm.acompletion,
            mode=instructor.Mode.TOOLS,
        )
        self._groq_instructor: instructor.AsyncInstructor = instructor.from_litellm(
            litellm.acompletion,
            mode=instructor.Mode.TOOLS,
        )
        self._ollama_instructor: instructor.AsyncInstructor = instructor.from_litellm(
            litellm.acompletion,
            mode=instructor.Mode.JSON,
        )

    async def _build_provider_chain(self, settings: Any) -> None:
        """
        Probe configured providers and build the ordered fallback chain.

        Ordering policy:
          1. NIM   — if key is set and endpoint responds (highest capability).
          2. Groq  — if key is set (always added when configured; not probed to
                     avoid consuming free-tier rate-limit quota at startup).
          3. Ollama — always last; no key required, no probe.

        Emits the mandatory startup log line naming the full chain.
        """
        chain: list[LLMProvider] = []

        # ── NIM ────────────────────────────────────────────────────────────────
        nim_status = "not_configured"
        if self._nim_api_key:
            try:
                await litellm.acompletion(
                    **self._provider_kwargs(LLMProvider.NIM),
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1,
                    timeout=10,
                )
                chain.append(LLMProvider.NIM)
                nim_status = "reachable"
            except Exception as exc:
                nim_status = f"unreachable ({type(exc).__name__})"
                logger.warning(
                    "llm: NIM probe failed (%s) — excluded from chain", nim_status
                )

        # ── Groq ───────────────────────────────────────────────────────────────
        # Not probed at startup to avoid burning free-tier RPM quota.
        # Groq is included in the chain if a key is present; failures surface at
        # inference time and trigger a move to Ollama.
        if self._groq_api_key:
            chain.append(LLMProvider.GROQ)

        # ── Ollama — always last ───────────────────────────────────────────────
        chain.append(LLMProvider.OLLAMA)

        self._provider_chain = chain
        self._primary = chain[0]

        # Mandatory startup log — operators must always see the active chain.
        chain_desc = " → ".join(
            f"{p.value}/{self._model_name(p)}" for p in chain
        )
        if self._primary == LLMProvider.OLLAMA and not self._nim_api_key and not self._groq_api_key:
            logger.warning(
                "LLM backend: %s  |  No cloud keys set — set NVIDIA_NIM_API_KEY "
                "or GROQ_API_KEY in .env for higher-capability inference.",
                chain_desc,
            )
        else:
            logger.info("LLM backend: %s", chain_desc)

        if nim_status not in ("not_configured", "reachable"):
            logger.warning("LLM: NIM status — %s", nim_status)

    # ── Private — request execution ────────────────────────────────────────────

    def _is_configured(self, provider: LLMProvider) -> bool:
        if provider == LLMProvider.NIM:
            return bool(self._nim_api_key)
        if provider == LLMProvider.GROQ:
            return bool(self._groq_api_key)
        return True  # Ollama requires no key

    def _provider_kwargs(self, provider: LLMProvider) -> dict[str, Any]:
        """Return LiteLLM call kwargs for the given provider."""
        if provider == LLMProvider.NIM:
            return {
                "model":    self._nim_model,
                "api_base": self._nim_base_url,
                "api_key":  self._nim_api_key,
            }
        if provider == LLMProvider.GROQ:
            return {
                "model":    self._groq_model,
                "api_base": self._groq_base_url,
                "api_key":  self._groq_api_key,
            }
        return {
            "model":    self._ollama_model,
            "api_base": self._ollama_base_url,
        }

    def _model_name(self, provider: LLMProvider) -> str:
        if provider == LLMProvider.NIM:
            return self._nim_model_name
        if provider == LLMProvider.GROQ:
            return self._groq_model_name
        return self._ollama_model_name

    def _instructor_for(self, provider: LLMProvider) -> instructor.AsyncInstructor:
        """Return the Instructor client configured for the given provider's mode."""
        if provider == LLMProvider.NIM:
            return self._nim_instructor
        if provider == LLMProvider.GROQ:
            return self._groq_instructor
        return self._ollama_instructor

    async def _completion_with_retry(
        self,
        provider: LLMProvider,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int | None,
    ) -> Any:
        """
        Execute a litellm.acompletion with provider-aware retry policy.

        Only TRANSIENT exceptions trigger retries.  PERMANENT exceptions propagate
        immediately.  Unrecognised exceptions are treated as transient and retried.
        """
        call_kwargs: dict[str, Any] = {
            **self._provider_kwargs(provider),
            "messages":    messages,
            "temperature": temperature,
            "timeout":     self._timeout,
        }
        if max_tokens is not None:
            call_kwargs["max_tokens"] = max_tokens

        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(_TRANSIENT),
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential_jitter(initial=1, max=10, jitter=1),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        ):
            with attempt:
                return await litellm.acompletion(**call_kwargs)

    async def _try_providers(
        self,
        fn: Any,  # Callable[[LLMProvider], Awaitable[Any]]
    ) -> tuple[LLMProvider, Any]:
        """
        Execute fn against each provider in the chain until one succeeds.

        - Permanent failures (auth, bad-request, context-window) propagate
          immediately — fallback cannot fix these.
        - Every transient failure that causes a chain advance is logged at WARNING
          and increments llm_fallbacks_total{provider_from, provider_to}.
        - If all providers fail, raises LLMFallbackExhausted with a full trace
          of each provider's error.

        Returns:
            (serving_provider, raw_result)
        """
        # Import here to avoid circular import at module load time.
        from app.core.metrics import llm_fallbacks_total

        failures: list[str] = []

        for idx, provider in enumerate(self._provider_chain):
            try:
                result = await fn(provider)
                if idx > 0:
                    logger.info(
                        "llm: request served by fallback provider %s (after %d failure(s))",
                        provider.value, idx,
                    )
                return provider, result

            except _PERMANENT:
                # Permanent error: retrying with another provider won't help.
                raise

            except Exception as exc:
                err_summary = f"{provider.value}: {type(exc).__name__}: {exc}"
                failures.append(err_summary)

                is_last = idx == len(self._provider_chain) - 1
                if is_last:
                    break

                next_provider = self._provider_chain[idx + 1]
                logger.warning(
                    "llm: provider %s failed (%s: %s) — advancing to %s",
                    provider.value,
                    type(exc).__name__,
                    exc,
                    next_provider.value,
                )
                try:
                    llm_fallbacks_total.labels(
                        provider_from=provider.value,
                        provider_to=next_provider.value,
                    ).inc()
                except Exception:
                    pass  # Metric emission must never abort the fallback logic.

        raise LLMFallbackExhausted(
            f"All {len(self._provider_chain)} provider(s) in chain exhausted. "
            f"Failures: {' | '.join(failures)}"
        )


# ── Module-level helpers ───────────────────────────────────────────────────────

def _strip_thinking(text: str) -> str:
    """Remove Qwen3 chain-of-thought <think>…</think> blocks from output."""
    return _THINK_TAG_RE.sub("", text).strip()


def _build_messages(
    prompt: str,
    system: str | None,
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return messages


def _parse_json(content: str) -> dict[str, Any]:
    """
    Parse JSON from LLM output, handling think-tags and markdown code fences.

    Raises:
        ValueError: If the content cannot be parsed as a JSON object.
    """
    content = _strip_thinking(content)

    fence_match = _CODE_FENCE_RE.search(content)
    raw = fence_match.group(1).strip() if fence_match else content.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error(
            "llm: JSON parse failed. preview=%r error=%s",
            raw[:300],
            exc,
        )
        raise ValueError(
            f"LLM returned non-JSON output: {exc}. "
            f"Content preview: {raw[:200]!r}"
        ) from exc


# ── Singleton registry ─────────────────────────────────────────────────────────

_client: CortexIntelligenceClient | None = None


def get_intelligence_client() -> CortexIntelligenceClient:
    """
    Return the CortexIntelligenceClient singleton.

    If initialize() was not called at startup, a lazy instance is created from
    settings without a provider probe (Groq skipped since it wasn't probed).
    A warning is logged — proper production usage always calls initialize().
    """
    global _client
    if _client is not None:
        return _client

    settings = get_settings()
    instance: CortexIntelligenceClient = object.__new__(CortexIntelligenceClient)
    instance._configure(settings)

    # Build chain from key presence alone (no network probe).
    chain: list[LLMProvider] = []
    if settings.NVIDIA_NIM_API_KEY:
        chain.append(LLMProvider.NIM)
    if settings.GROQ_API_KEY:
        chain.append(LLMProvider.GROQ)
    chain.append(LLMProvider.OLLAMA)

    instance._provider_chain = chain
    instance._primary = chain[0]

    _client = instance
    logger.warning(
        "CortexIntelligenceClient lazy-initialized without provider probe. "
        "Chain: %s  |  Call `await CortexIntelligenceClient.initialize()` at startup.",
        " → ".join(p.value for p in chain),
    )
    return _client


def get_ollama_client() -> CortexIntelligenceClient:
    """
    Backward-compatibility alias for get_intelligence_client().

    Existing callers (EventClassifier, FakeNewsDetector) use this name.
    All calls are transparently routed through the provider-agnostic client.
    """
    return get_intelligence_client()
