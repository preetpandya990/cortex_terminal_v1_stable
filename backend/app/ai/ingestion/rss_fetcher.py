"""
RSS Fetcher - Async RSS Feed Ingestion

Fetches RSS feeds from configured sources with rate limiting and error handling.

Deduplication layers (in order):
  1. Intra-batch: first occurrence wins within one feed's fetch, on both the
     exact content hash and the normalized near-duplicate hash.
  2. DB lookup: one batched SELECT per feed checks both hashes against
     ai_raw_events (replaces the old per-item SELECT loop).
  3. INSERT ... ON CONFLICT DO NOTHING: feeds ingest concurrently in separate
     sessions, so two feeds carrying the same syndicated article can both pass
     the SELECT check; the unique constraint on content_hash then silently
     drops the loser instead of aborting its whole feed batch.

Near-duplicate hashing: normalized_hash is SHA-256 over normalize_for_hash()
output (casefold, NFC, byline prefix stripped, punctuation removed, whitespace
collapsed) — catches syndicated wire copy that differs only in formatting.
Layer 3 does not cover it (the column is deliberately non-unique), so a
same-cycle cross-feed near-dup race can still slip one duplicate through;
steady-state cycles catch it at layer 2. Best-effort by design.
"""
import asyncio
import hashlib
import logging
import random
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

import feedparser
import httpx
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.fusion.models import AIRawEvent
from app.ai.intelligence.credibility_scorer import CredibilityScorer
from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Only web schemes may be persisted as source_url — anything else
# (javascript:, data:, file:, ...) is an XSS/SSRF surface once the URL is
# rendered as a clickable citation link in the frontend.
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})

# Leading byline prefix ("By Jane Doe: ...", "by Reuters - ...") — syndication
# noise that differs across feeds republishing the same wire copy.
_BYLINE_PREFIX_RE = re.compile(r"^by\s+[\w .,'&-]{2,80}?[:\-–—|]\s*", re.IGNORECASE)

# Everything that is not a word character or whitespace (unicode-aware).
_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_for_hash(text: str) -> str:
    """
    Canonicalize text for near-duplicate detection.

    Casefolds, applies Unicode NFC, strips a leading byline prefix, removes
    punctuation, and collapses all whitespace — so syndicated wire copy that
    differs only in formatting (byline, quote style, dashes, reflowed
    whitespace) normalizes to the same string.

    Mirrored inline in migration 0051's one-time backfill; changing this
    only affects rows ingested afterwards (near-dup detection is best-effort).
    """
    t = unicodedata.normalize("NFC", text).casefold()
    t = _BYLINE_PREFIX_RE.sub("", t)
    t = _NON_WORD_RE.sub(" ", t)
    return " ".join(t.split())


def normalized_content_hash(text: str) -> str:
    """SHA-256 hex digest of the normalized form of ``text``."""
    return hashlib.sha256(normalize_for_hash(text).encode("utf-8")).hexdigest()


def sanitize_source_url(url: str) -> str:
    """
    Return ``url`` stripped iff its scheme is http/https, else "".

    Feed <link> values are third-party content; a malicious feed serving a
    javascript:/data: URI must never reach the DB, where it would eventually
    be rendered as a clickable citation.
    """
    if not url:
        return ""
    candidate = url.strip()
    try:
        scheme = urlsplit(candidate).scheme.lower()
    except ValueError:
        return ""
    return candidate if scheme in _ALLOWED_URL_SCHEMES else ""


def build_event_rows(
    items: list[dict[str, Any]],
    source_name: str,
    source_url: str,
    min_content_chars: int,
) -> list[dict[str, Any]]:
    """
    Transform parsed feed items into ai_raw_events insert dicts (column names,
    Core-insert compatible: ``extra_data`` maps to the "metadata" column).

    Pure function — applies intra-batch dedup (layer 1: first occurrence wins
    on both the exact and normalized hash), source_url sanitization with
    fallback to the feed URL, and the low_confidence_source quality tag.
    """
    rows: list[dict[str, Any]] = []
    seen_exact: set[str] = set()
    seen_normalized: set[str] = set()

    for item in items:
        content = item["raw_content"]
        exact_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        norm_hash = normalized_content_hash(content)
        if exact_hash in seen_exact or norm_hash in seen_normalized:
            continue
        seen_exact.add(exact_hash)
        seen_normalized.add(norm_hash)

        link = sanitize_source_url(item["link"])
        if item["link"] and not link:
            logger.warning(
                "Rejected non-http(s) link from %s: %.120s",
                source_name,
                item["link"],
            )

        extra_data = dict(item["metadata"])
        if len(content) < min_content_chars:
            # Tag, not reject: short disclosures stay searchable but
            # retrieval ranking can demote them (see RAG_MIN_CONTENT_CHARS).
            extra_data["low_confidence_source"] = True

        rows.append(
            {
                "event_timestamp": item["event_timestamp"],
                "source_type": "rss",
                "source_url": link or source_url,
                "source_name": source_name,
                "content_hash": exact_hash,
                "normalized_hash": norm_hash,
                "raw_content": content,
                "language": "en",
                "metadata": extra_data,
            }
        )

    return rows


# Default RSS/XML feeds for Indian financial markets: (display name, url, type).
# The display name is the canonical source-identity key across ai_raw_events,
# ai_source_credibility, the fake-news detector, and sentiment source weights.
# The type seeds the initial credibility score (exchange 80, news 50) —
# official exchange feeds carry corporate announcements/results with higher
# baseline credibility than general financial news.
DEFAULT_RSS_FEEDS: list[tuple[str, str, str]] = [
    # ── Financial news sources ─────────────────────────────────────────────────
    ("Economic Times Markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms", "news"),
    ("Moneycontrol", "https://www.moneycontrol.com/rss/latestnews.xml", "news"),
    ("Business Standard", "https://www.business-standard.com/rss/markets-106.rss", "news"),
    ("LiveMint Markets", "https://www.livemint.com/rss/markets", "news"),
    ("Reuters India Business", "https://feeds.reuters.com/reuters/INbusinessNews", "news"),
    # ── Official exchange feeds ────────────────────────────────────────────────
    # NSE corporate filings: board meetings, results, insider trading disclosures
    ("NSE Corporate Filings", "https://www.nseindia.com/static/rss-feed", "exchange"),
    # BSE corporate announcements and circulars
    ("BSE Corporate Notices", "https://www.bseindia.com/data/xml/notices.xml", "exchange"),
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

            # feedparser must receive raw bytes so its own charset detection
            # (BOM, XML declaration, HTTP header) runs — pre-decoded text would
            # bake in httpx's guess and silently mojibake non-UTF-8 feeds.
            # The Content-Type header is forwarded so the HTTP charset hint
            # participates in that detection.
            feed = feedparser.parse(
                response.content,
                response_headers={
                    "content-type": response.headers.get("content-type", ""),
                },
            )

            if feed.bozo:
                # Includes CharacterEncodingOverride when detection disagreed
                # with the declared encoding — the exception type is the signal.
                logger.warning(
                    "Feed parsing warning for %s: %s: %s",
                    url,
                    type(feed.bozo_exception).__name__,
                    feed.bozo_exception,
                )
            
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
        """
        Ingest one RSS feed into ai_raw_events with layered deduplication.

        See the module docstring for the three dedup layers. The session is
        exclusively this feed's for the duration of the call — never share it
        across concurrently ingesting feeds.

        Returns the number of rows actually inserted (conflicts excluded).
        """
        items = await self.fetch_feed(source_url)
        if not items:
            return 0

        # Layer 1 — intra-batch dedup, sanitization, quality tagging.
        rows = build_event_rows(
            items, source_name, source_url, settings.RAG_MIN_CONTENT_CHARS
        )
        if not rows:
            return 0

        # Layer 2 — one batched existence check per feed on both hash layers
        # (replaces the old per-item SELECT loop: 2 round-trips per feed
        # instead of ~N).
        result = await db.execute(
            select(AIRawEvent.content_hash, AIRawEvent.normalized_hash).where(
                or_(
                    AIRawEvent.content_hash.in_([r["content_hash"] for r in rows]),
                    AIRawEvent.normalized_hash.in_([r["normalized_hash"] for r in rows]),
                )
            )
        )
        existing_exact: set[str] = set()
        existing_normalized: set[str] = set()
        for content_hash, norm_hash in result.all():
            existing_exact.add(content_hash)
            if norm_hash:
                existing_normalized.add(norm_hash)

        new_rows = [
            r
            for r in rows
            if r["content_hash"] not in existing_exact
            and r["normalized_hash"] not in existing_normalized
        ]
        if not new_rows:
            return 0

        # Layer 3 — conflict-safe insert. Feeds ingest concurrently in separate
        # sessions, so a cross-feed race on the same article is expected, not
        # exceptional; ON CONFLICT DO NOTHING drops the loser row instead of
        # raising IntegrityError and rolling back the entire feed batch.
        stmt = (
            pg_insert(AIRawEvent.__table__)
            .values(new_rows)
            .on_conflict_do_nothing()
        )
        insert_result = await db.execute(stmt)
        await db.commit()
        ingested_count = (
            insert_result.rowcount if insert_result.rowcount >= 0 else len(new_rows)
        )

        if ingested_count > 0:
            logger.info(
                "Ingested %d new events from %s (%d fetched, %d deduped)",
                ingested_count,
                source_name,
                len(items),
                len(items) - ingested_count,
            )
        return ingested_count

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()


async def rss_ingestion_loop(
    session_factory: async_sessionmaker,
    *,
    on_cycle: Callable[[], None] | None = None,
) -> None:
    """
    RSS ingestion background loop.

    Fetches RSS feeds from configured sources with jittered polling.
    Runs until cancelled via asyncio.CancelledError.

    Each feed ingests in its own AsyncSession: sessions are single-task
    objects (SQLAlchemy asyncio docs) — the old shared-session gather let one
    feed's commit() flush sibling feeds' pending rows mid-cycle and produced
    asyncpg "another operation is in progress" races that silently failed
    2+ feeds per cycle.

    Args:
        session_factory: SQLAlchemy async session factory
    """
    logger.info("RSS ingestion loop started")
    fetcher = RSSFetcher()
    scorer = CredibilityScorer()

    async def _ingest_one(name: str, url: str, source_type: str) -> int:
        async with session_factory() as db:
            # Keep the credibility registry current (idempotent upsert-style
            # check; one indexed SELECT per feed per cycle on the hot path).
            await scorer.get_or_create_source(db, name, source_type)
            return await fetcher.ingest_feed(db, name, url)

    try:
        while True:
            try:
                results = await asyncio.gather(
                    *(_ingest_one(name, url, source_type)
                      for name, url, source_type in DEFAULT_RSS_FEEDS),
                    return_exceptions=True,
                )

                total_ingested = 0
                errors = 0
                for (name, _url, _type), outcome in zip(DEFAULT_RSS_FEEDS, results):
                    if isinstance(outcome, BaseException):
                        errors += 1
                        logger.error(
                            "RSS feed %r ingestion failed: %s",
                            name,
                            outcome,
                            exc_info=outcome,
                        )
                    else:
                        total_ingested += outcome

                logger.info(
                    f"RSS ingestion cycle complete: {total_ingested} events, {errors} errors"
                )

            except Exception as e:
                logger.error(f"RSS ingestion error: {e}", exc_info=True)

            if on_cycle is not None:
                on_cycle()

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
