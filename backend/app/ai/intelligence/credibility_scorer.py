"""
Credibility Scorer - Source Credibility Tracking

Tracks and scores source credibility based on historical accuracy.
"""
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AISourceCredibility

logger = logging.getLogger(__name__)


class CredibilityScorer:
    """Tracks and scores source credibility."""

    async def get_or_create_source(
        self,
        db: AsyncSession,
        source_name: str,
        source_type: str,
    ) -> AISourceCredibility:
        """Get existing source or create new one."""
        stmt = select(AISourceCredibility).where(AISourceCredibility.source_name == source_name)
        result = await db.execute(stmt)
        source = result.scalar_one_or_none()

        if not source:
            source = AISourceCredibility(
                source_name=source_name,
                source_type=source_type,
                credibility_score=Decimal("50.0"),
                total_events=0,
                confirmed_events=0,
                contradicted_events=0,
            )
            db.add(source)
            await db.commit()
            await db.refresh(source)
            logger.info(f"Created new source: {source_name}")

        return source

    async def update_credibility(
        self,
        db: AsyncSession,
        source_name: str,
        confirmed: bool,
    ) -> AISourceCredibility:
        """Update source credibility based on event outcome."""
        stmt = select(AISourceCredibility).where(AISourceCredibility.source_name == source_name)
        result = await db.execute(stmt)
        source = result.scalar_one_or_none()

        if not source:
            logger.warning(f"Source not found: {source_name}")
            return None

        source.total_events += 1
        if confirmed:
            source.confirmed_events += 1
        else:
            source.contradicted_events += 1

        # Calculate new credibility score
        if source.total_events > 0:
            accuracy = source.confirmed_events / source.total_events
            source.credibility_score = Decimal(str(accuracy * 100))

        await db.commit()
        await db.refresh(source)

        logger.info(f"Updated credibility for {source_name}: {source.credibility_score}")
        return source
