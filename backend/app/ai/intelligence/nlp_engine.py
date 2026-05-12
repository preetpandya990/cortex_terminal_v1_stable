"""
NLP Engine — FinBERT PyTorch Sentiment Analysis
================================================
Production-grade financial sentiment using FinBERT (ProsusAI/finbert) via the
standard HuggingFace transformers + PyTorch inference stack.

Architecture:
  - Class-level singleton: model loaded once at startup, shared across requests
  - PyTorch inference: GPU when available, CPU fallback — ~30-50ms per headline
  - Thread-pool offload: asyncio.to_thread() keeps the event loop non-blocking
  - Graceful degradation: returns neutral/0.0 if model fails to load
  - spaCy NER: entity extraction for companies, people, locations (non-critical)

Note on ONNX/GPU path:
  The previous implementation used optimum[onnxruntime] for ONNX export and
  CUDAExecutionProvider. That path broke with PyTorch 2.5 (missing .onnx.data
  file in temp dir) and is incompatible with the current CUDA 12.0.2 driver.
  Pure PyTorch inference is equivalent at this request volume (≤ 50 articles).
  Re-enable ONNX GPU path once the GPU driver is updated to ≥ 12.1.

Startup:
  Call `await NLPEngine.initialize()` once from the FastAPI lifespan.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import torch
import torch.nn.functional as F

logger = logging.getLogger(__name__)

# FinBERT label order as returned by ProsusAI/finbert
_FINBERT_LABELS = ["negative", "neutral", "positive"]

_FINBERT_MODEL_ID = "ProsusAI/finbert"


class NLPEngine:
    """
    Singleton NLP engine for financial sentiment analysis and entity extraction.

    Usage:
        # At startup:
        await NLPEngine.initialize()

        # Per request:
        engine = NLPEngine()
        result = await engine.analyze_sentiment("Company beats Q4 earnings by 15%")
    """

    # ── Class-level state (shared across all instances) ────────────────────────
    _model: Any = None          # AutoModelForSequenceClassification
    _tokenizer: Any = None      # AutoTokenizer
    _spacy_nlp: Any = None      # spaCy language model
    _device: str = "cpu"
    _initialized: bool = False

    # ── Startup initialization ─────────────────────────────────────────────────

    @classmethod
    async def initialize(cls) -> None:
        """
        Load FinBERT and spaCy models at application startup.
        Safe to call multiple times — no-op if already initialized.
        """
        if cls._initialized:
            return

        await asyncio.to_thread(cls._load_models)

    @classmethod
    def _load_models(cls) -> None:
        """Synchronous model loading — runs in thread pool once at startup."""
        try:
            from transformers import AutoModelForSequenceClassification, AutoTokenizer

            # Prefer GPU; fall back to CPU silently
            cls._device = "cuda" if torch.cuda.is_available() else "cpu"
            logger.info("Loading FinBERT: model=%s device=%s", _FINBERT_MODEL_ID, cls._device)

            cls._tokenizer = AutoTokenizer.from_pretrained(_FINBERT_MODEL_ID)
            cls._model = AutoModelForSequenceClassification.from_pretrained(_FINBERT_MODEL_ID)
            cls._model.eval()

            if cls._device == "cuda":
                cls._model = cls._model.cuda()

            logger.info("FinBERT loaded: device=%s", cls._device)

        except Exception as exc:
            logger.error("Failed to load FinBERT: %s", exc, exc_info=True)
            cls._model = None
            cls._tokenizer = None

        # spaCy NER — non-critical, graceful degradation if not installed
        try:
            import spacy
            cls._spacy_nlp = spacy.load("en_core_web_sm")
            logger.info("spaCy en_core_web_sm loaded")
        except Exception as exc:
            logger.warning("spaCy load failed (entity extraction disabled): %s", exc)
            cls._spacy_nlp = None

        cls._initialized = True

    # ── Public API ─────────────────────────────────────────────────────────────

    async def analyze_sentiment(self, text: str) -> dict[str, Any]:
        """
        Classify the financial sentiment of a text snippet.

        Args:
            text: News headline or article excerpt (max 512 tokens)

        Returns:
            {
                "label": "positive" | "negative" | "neutral",
                "score": float,       # -1.0 (very negative) to +1.0 (very positive)
                "confidence": float,  # 0.0 to 1.0
                "model": str,
            }
        """
        if not self._initialized or self._model is None:
            return {"label": "neutral", "score": 0.0, "confidence": 0.0, "model": "unavailable"}

        return await asyncio.to_thread(self._classify_sync, text)

    async def extract_entities(self, text: str) -> dict[str, list[str]]:
        """
        Extract named entities from text using spaCy.

        Returns:
            {"companies": [...], "people": [...], "locations": [...]}
        """
        if not self._spacy_nlp:
            return {"companies": [], "people": [], "locations": []}

        return await asyncio.to_thread(self._extract_entities_sync, text)

    async def process_event(
        self,
        db: Any,
        processed_event_id: int,
        content: str,
    ) -> Any:
        """
        Full NLP pipeline for a raw event: sentiment + entities → AINLPResult.
        Maintains backward-compatibility with the existing event processing flow.
        """
        from app.ai.fusion.models import AINLPResult

        sentiment = await self.analyze_sentiment(content)
        entities = await self.extract_entities(content)

        nlp_result = AINLPResult(
            processed_event_id=processed_event_id,
            sentiment_score=sentiment["score"],
            sentiment_label=sentiment["label"],
            named_entities=entities,
            keywords=[],
            model_used=sentiment["model"],
            confidence_score=sentiment["confidence"],
        )

        db.add(nlp_result)
        await db.commit()
        await db.refresh(nlp_result)

        logger.info(
            "NLP processed event %d: label=%s score=%.3f confidence=%.3f",
            processed_event_id, sentiment["label"], sentiment["score"], sentiment["confidence"],
        )
        return nlp_result

    # ── Internal synchronous routines (run in thread pool) ────────────────────

    @classmethod
    def _classify_sync(cls, text: str) -> dict[str, Any]:
        """FinBERT PyTorch inference — synchronous, runs in thread pool."""
        inputs = cls._tokenizer(
            text,
            return_tensors="pt",
            truncation=True,
            max_length=512,
            padding=True,
        )

        if cls._device == "cuda":
            inputs = {k: v.cuda() for k, v in inputs.items()}

        with torch.no_grad():
            outputs = cls._model(**inputs)
            probs = F.softmax(outputs.logits, dim=-1)[0]  # [negative, neutral, positive]

        probs_list = probs.cpu().tolist()
        neg, _, pos = probs_list
        label_idx = int(probs.argmax().item())
        label = _FINBERT_LABELS[label_idx]
        confidence = float(probs[label_idx].item())

        # Signed score in [-1, +1]: positive probability minus negative probability
        score = round(pos - neg, 4)

        return {
            "label": label,
            "score": score,
            "confidence": round(confidence, 4),
            "model": f"finbert-pt-{'gpu' if cls._device == 'cuda' else 'cpu'}",
        }

    @classmethod
    def _extract_entities_sync(cls, text: str) -> dict[str, list[str]]:
        """spaCy named entity recognition — synchronous, runs in thread pool."""
        doc = cls._spacy_nlp(text[:1000])  # cap input length for NER

        companies: list[str] = []
        people: list[str] = []
        locations: list[str] = []

        for ent in doc.ents:
            if ent.label_ in ("ORG", "PRODUCT"):
                companies.append(ent.text)
            elif ent.label_ == "PERSON":
                people.append(ent.text)
            elif ent.label_ in ("GPE", "LOC"):
                locations.append(ent.text)

        def dedup(lst: list[str]) -> list[str]:
            seen: set[str] = set()
            return [x for x in lst if not (x in seen or seen.add(x))]  # type: ignore[func-returns-value]

        return {
            "companies": dedup(companies)[:5],
            "people": dedup(people)[:3],
            "locations": dedup(locations)[:3],
        }
