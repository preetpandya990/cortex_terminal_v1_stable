"""
RSS Fetcher - Async RSS Feed Ingestion

Fetches RSS feeds from configured sources with rate limiting and error handling.
"""
import asyncio
import hashlib
import logging
import random
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.fusion.models import AIRawEvent
from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


# Default RSS feeds for Indian financial markets
DEFAULT_RSS_FEEDS = [
    ("Economic Times Markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Moneycontrol", "https://www.moneycontrol.com/rss/latestnews.xml"),
    ("Business Standard", "https://www.business-standard.com/rss/markets-106.rss"),
    ("LiveMint Markets", "https://www.livemint.com/rss/markets"),
    ("Reuters India Business", "https://feeds.reuters.com/reuters/INbusinessNews"),
]


class RSSFetcher:
    """Async RSS feed fetcher with rate limiting."""

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )

    async def fetch_feed(self, url: str) -> list[dict]:
        """Fetch and parse RSS feed."""
        try:
            response = await self.client.get(url)
            response.raise_for_status()
            
            # Parse RSS feed
            feed = feedparser.parse(response.text)
            
            if feed.bozo:
                logger.warning(f"Feed parsing warning for {url}: {feed.bozo_exception}")
            
            entries = []
            for entry in feed.entries:
                title = entry.get("title", "")
                summary = entry.get("summary", entry.get("description", ""))
                link = entry.get("link", "")
                published = entry.get("published", entry.get("updated", ""))
                
                raw_content = f"{title}\n\n{summary}".strip()
                if not raw_content:
                    continue
                
                # Parse published date
                time_tuple = entry.get("published_parsed") or entry.get("updated_parsed")
                if time_tuple:
                    try:
                        event_timestamp = datetime(*time_tuple[:6], tzinfo=timezone.utc)
                    except (ValueError, TypeError):
                        event_timestamp = datetime.now(timezone.utc)
                else:
                    event_timestamp = datetime.now(timezone.utc)
                
                entries.append({
                    "raw_content": raw_content,
                    "event_timestamp": event_timestamp,
                    "link": link,
                    "metadata": {
                        "title": title,
                        "published": published,
                        "author": entry.get("author", ""),
                        "tags": [tag.get("term", "") for tag in entry.get("tags", [])],
                    }
                })
            
            logger.debug(f"Parsed {len(entries)} entries from {url}")
            return entries
            
        except Exception as e:
            logger.error(f"Failed to fetch RSS feed {url}: {e}")
            return []

    async def ingest_feed(
        self,
        db: AsyncSession,
        source_name: str,
        source_url: str,
    ) -> int:
        """Ingest RSS feed into database with deduplication."""
        items = await self.fetch_feed(source_url)
        ingested_count = 0

        for item in items:
            content = item["raw_content"]
            content_hash = hashlib.sha256(content.encode()).hexdigest()
            
            # Check for duplicate
            stmt = select(AIRawEvent).where(AIRawEvent.content_hash == content_hash)
            result = await db.execute(stmt)
            if result.scalar_one_or_none():
                continue  # Skip duplicate
            
            event = AIRawEvent(
                event_timestamp=item["event_timestamp"],
                source_type="rss",
                source_url=item["link"] or source_url,
                source_name=source_name,
                content_hash=content_hash,
                raw_content=content,
                language="en",
                metadata=item["metadata"],
            )

            db.add(event)
            ingested_count += 1

        if ingested_count > 0:
            await db.commit()
            logger.info(f"Ingested {ingested_count} new events from {source_name}")
        
        return ingested_count

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()


async def rss_ingestion_loop(session_factory: async_sessionmaker) -> None:
    """
    RSS ingestion background loop.
    
    Fetches RSS feeds from configured sources with jittered polling.
    Runs until cancelled via asyncio.CancelledError.
    
    Args:
        session_factory: SQLAlchemy async session factory
    """
    logger.info("RSS ingestion loop started")
    fetcher = RSSFetcher()
    
    try:
        while True:
            try:
                async with session_factory() as db:
                    # Fetch all feeds concurrently
                    tasks = [
                        fetcher.ingest_feed(db, name, url)
                        for name, url in DEFAULT_RSS_FEEDS
                    ]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    
                    total_ingested = sum(r for r in results if isinstance(r, int))
                    errors = sum(1 for r in results if isinstance(r, Exception))
                    
                    logger.info(
                        f"RSS ingestion cycle complete: {total_ingested} events, {errors} errors"
                    )
            
            except Exception as e:
                logger.error(f"RSS ingestion error: {e}", exc_info=True)
            
            # Jittered sleep between RSS_MIN_POLL_SECONDS and RSS_MAX_POLL_SECONDS
            jitter = random.randint(0, settings.RSS_POLL_JITTER_SECONDS)
            sleep_time = random.randint(
                settings.RSS_MIN_POLL_SECONDS,
                settings.RSS_MAX_POLL_SECONDS
            ) + jitter
            
            logger.debug(f"Sleeping for {sleep_time}s until next RSS poll")
            await asyncio.sleep(sleep_time)
    
    except asyncio.CancelledError:
        logger.info("RSS ingestion loop cancelled")
        raise
    finally:
        await fetcher.close()
        logger.info("RSS ingestion loop stopped")
