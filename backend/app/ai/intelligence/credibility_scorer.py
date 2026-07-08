"""
Credibility Scorer - Source Credibility Tracking
================================================
Tracks and scores source credibility based on historical accuracy.

Identity key
------------
Sources are keyed by **feed display name** (``ai_raw_events.source_name``,
e.g. "Economic Times Markets") everywhere: this table, the fake-news
detector's lookup, and the sentiment source weights. Never key by URL — the
three consumers would silently stop joining.

Score domain
------------
``credibility_score`` is stored **0–100** (``Numeric(5,2)``, default 50).
Consumers that need a 0–1 weight must rescale (see
``fake_news_detector._check_source_credibility`` and
``sentiment_analysis_service``).

Concurrency
-----------
``get_or_create_source`` is INSERT ... ON CONFLICT DO NOTHING + re-select —
safe under the per-feed concurrent sessions in rss_ingestion_loop.
``update_credibility`` takes a row lock (SELECT ... FOR UPDATE) so two
concurrent outcome updates can never lose a count.
"""
import logging
import time
from decimal import Decimal
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AISourceCredibility

logger = logging.getLogger(__name__)

# Seed scores by source type: official exchange feeds start trusted, general
# news starts neutral. Earned accuracy adjusts from there.
_SEED_SCORES: dict[str, Decimal] = {
    "exchange": Decimal("80.0"),
    "news": Decimal("50.0"),
}
_DEFAULT_SEED = Decimal("50.0")

# In-process TTL cache for batch score lookups — shared by the RAG retriever
# and the sentiment aggregator, both of which resolve the same handful of feed
# names on hot paths. 900s matches the sentiment L2 TTL, bounding staleness of
# ranking inputs to the same window as the sentiment aggregate itself.
_SCORE_CACHE_TTL_SECS = 900.0
_score_cache: dict[str, tuple[float, float]] = {}  # name -> (score_0_100, expires_at)


class CredibilityScorer:
    """Tracks and scores source credibility."""

    async def get_or_create_source(
        self,
        db: AsyncSession,
        source_name: str,
        source_type: str,
    ) -> AISourceCredibility:
        """
        Get existing source or create it with a type-appropriate seed score.

        Race-safe: concurrent creators (per-feed ingest sessions) both attempt
        the insert; ON CONFLICT DO NOTHING lets exactly one win, and the
        follow-up SELECT returns the surviving row either way.
        """
        stmt = (
            pg_insert(AISourceCredibility.__table__)
            .values(
                source_name=source_name,
                source_type=source_type,
                credibility_score=_SEED_SCORES.get(source_type, _DEFAULT_SEED),
                total_events=0,
                confirmed_events=0,
                contradicted_events=0,
            )
            .on_conflict_do_nothing(index_elements=["source_name"])
        )
        result = await db.execute(stmt)
        await db.commit()
        if result.rowcount:
            logger.info("Created credibility source: %s (%s)", source_name, source_type)

        row = await db.execute(
            select(AISourceCredibility).where(
                AISourceCredibility.source_name == source_name
            )
        )
        return row.scalar_one()

    async def update_credibility(
        self,
        db: AsyncSession,
        source_name: str,
        confirmed: bool,
    ) -> AISourceCredibility | None:
        """
        Update source credibility from an event outcome (row-locked).

        SELECT ... FOR UPDATE serializes concurrent read-modify-write cycles —
        without it, two simultaneous outcomes for the same source lose one
        count. Unknown sources are skipped (returns None), never auto-created:
        creation is the ingest registry's job, and auto-creating here would let
        arbitrary API-passed identifiers pollute the table.
        """
        result = await db.execute(
            select(AISourceCredibility)
            .where(AISourceCredibility.source_name == source_name)
            .with_for_update()
        )
        source = result.scalar_one_or_none()

        if source is None:
            logger.warning(
                "update_credibility: unknown source %r — skipped "
                "(sources are registered at ingest, keyed by feed display name)",
                source_name,
            )
            return None

        source.total_events += 1
        if confirmed:
            source.confirmed_events += 1
        else:
            source.contradicted_events += 1

        accuracy = source.confirmed_events / source.total_events
        source.credibility_score = Decimal(str(round(accuracy * 100, 2)))
        source.last_updated = func.now()

        await db.commit()
        await db.refresh(source)
        _score_cache.pop(source_name, None)  # invalidate in-process cache

        logger.info(
            "Updated credibility for %s: %s (%d/%d confirmed)",
            source_name,
            source.credibility_score,
            source.confirmed_events,
            source.total_events,
        )
        return source


async def load_credibility_scores(
    db: AsyncSession,
    source_names: Iterable[str],
) -> dict[str, float]:
    """
    Batch-load 0–100 credibility scores for the given feed display names.

    One SELECT for all cache misses; hits are served from an in-process TTL
    cache (900s). Names with no credibility row are absent from the result —
    callers apply their own default (unknown sources are deliberately not
    negative-cached so a newly registered source is picked up promptly).
    """
    now = time.monotonic()
    scores: dict[str, float] = {}
    missing: list[str] = []

    for name in set(source_names):
        cached = _score_cache.get(name)
        if cached is not None and cached[1] > now:
            scores[name] = cached[0]
        else:
            missing.append(name)

    if missing:
        rows = await db.execute(
            select(
                AISourceCredibility.source_name,
                AISourceCredibility.credibility_score,
            ).where(AISourceCredibility.source_name.in_(missing))
        )
        for name, score in rows.all():
            value = float(score)
            scores[name] = value
            _score_cache[name] = (value, now + _SCORE_CACHE_TTL_SECS)

    return scores
