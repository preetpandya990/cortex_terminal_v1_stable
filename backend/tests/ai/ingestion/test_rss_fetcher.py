"""
RSS Fetcher tests
=================
Covers the WS2 ingestion rework: near-duplicate normalization, source-URL
scheme sanitization, byte-level feedparser handoff (charset detection), and
the layered dedup + conflict-safe insert in ingest_feed.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.ingestion.rss_fetcher import (
    RSSFetcher,
    build_event_rows,
    normalize_for_hash,
    normalized_content_hash,
    sanitize_source_url,
)

pytestmark = pytest.mark.unit


def _item(
    content: str,
    link: str = "https://example.com/article",
    title: str = "t",
) -> dict:
    return {
        "raw_content": content,
        "event_timestamp": datetime(2026, 7, 6, tzinfo=timezone.utc),
        "link": link,
        "metadata": {"title": title, "published": "", "author": "", "tags": []},
    }


# ── normalize_for_hash ─────────────────────────────────────────────────────────


class TestNormalizeForHash:
    def test_casefold_and_whitespace_collapse(self):
        assert normalize_for_hash("  RIL   Posts\n\nRecord  PROFIT ") == "ril posts record profit"

    def test_punctuation_stripped(self):
        assert normalize_for_hash('RIL posts "record" profit!') == normalize_for_hash(
            "RIL posts record profit"
        )

    def test_byline_prefix_stripped(self):
        assert normalize_for_hash("By Jane Doe: RIL posts record profit") == (
            "ril posts record profit"
        )

    def test_unicode_nfc_equivalence(self):
        composed = "Café results"          # é as one codepoint
        decomposed = "Café results"       # e + combining acute
        assert normalize_for_hash(composed) == normalize_for_hash(decomposed)

    def test_syndicated_wire_copy_variants_collide(self):
        original = "TCS Q4 net profit rises 12% — beats estimates"
        syndicated = 'by Reuters - TCS Q4 net profit rises 12%, "beats" estimates'
        assert normalized_content_hash(original) == normalized_content_hash(syndicated)

    def test_genuinely_different_content_does_not_collide(self):
        assert normalized_content_hash("TCS profit rises") != normalized_content_hash(
            "TCS profit falls"
        )


# ── sanitize_source_url ────────────────────────────────────────────────────────


class TestSanitizeSourceUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/a",
            "http://example.com/a",
            "  https://example.com/padded  ",
        ],
    )
    def test_web_schemes_accepted(self, url):
        assert sanitize_source_url(url) == url.strip()

    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "JAVASCRIPT:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "file:///etc/passwd",
            "ftp://example.com/x",
            "//scheme-relative.example.com/x",
            "not a url at all",
        ],
    )
    def test_non_web_schemes_rejected(self, url):
        assert sanitize_source_url(url) == ""

    def test_empty_and_none_like(self):
        assert sanitize_source_url("") == ""


# ── build_event_rows ───────────────────────────────────────────────────────────


class TestBuildEventRows:
    def test_intra_batch_near_duplicate_dropped_first_wins(self):
        items = [
            _item("TCS Q4 net profit rises 12% — beats estimates"),
            _item('by PTI - TCS Q4 net profit rises 12%, "beats" estimates'),
        ]
        rows = build_event_rows(items, "Feed", "https://feed.example", 0)
        assert len(rows) == 1
        assert rows[0]["raw_content"].startswith("TCS Q4")

    def test_exact_duplicate_dropped(self):
        items = [_item("same body"), _item("same body")]
        rows = build_event_rows(items, "Feed", "https://feed.example", 0)
        assert len(rows) == 1

    def test_low_confidence_tag_applied_below_floor(self):
        rows = build_event_rows([_item("short")], "Feed", "https://feed.example", 200)
        assert rows[0]["metadata"]["low_confidence_source"] is True

    def test_no_tag_at_or_above_floor(self):
        body = "x" * 200
        rows = build_event_rows([_item(body)], "Feed", "https://feed.example", 200)
        assert "low_confidence_source" not in rows[0]["metadata"]

    def test_malicious_link_falls_back_to_feed_url(self):
        rows = build_event_rows(
            [_item("body text", link="javascript:alert(1)")],
            "Feed",
            "https://feed.example/rss",
            0,
        )
        assert rows[0]["source_url"] == "https://feed.example/rss"

    def test_hashes_and_columns_populated(self):
        body = "TCS Q4 results announced"
        rows = build_event_rows([_item(body)], "Feed", "https://feed.example", 0)
        row = rows[0]
        assert row["content_hash"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
        assert row["normalized_hash"] == normalized_content_hash(body)
        assert row["source_type"] == "rss"
        assert row["source_name"] == "Feed"


# ── fetch_feed: bytes handoff / charset detection ──────────────────────────────


class TestFetchFeedEncoding:
    @pytest.mark.asyncio
    async def test_non_utf8_feed_decodes_via_declared_encoding(self):
        """feedparser gets bytes, so the XML-declared ISO-8859-1 wins."""
        xml = (
            '<?xml version="1.0" encoding="ISO-8859-1"?>'
            '<rss version="2.0"><channel><title>c</title>'
            "<item><title>Café results — record</title>"
            "<description>d</description>"
            "<link>https://example.com/a</link></item>"
            "</channel></rss>"
        )
        response = MagicMock()
        response.content = xml.encode("iso-8859-1", errors="replace")
        # No charset in Content-Type: detection must come from the XML declaration.
        response.headers = {"content-type": "application/xml"}
        response.raise_for_status = MagicMock()

        fetcher = RSSFetcher()
        fetcher.client.get = AsyncMock(return_value=response)
        try:
            entries = await fetcher.fetch_feed("https://feed.example/rss")
        finally:
            await fetcher.close()

        assert len(entries) == 1
        assert "Café results" in entries[0]["raw_content"]


# ── ingest_feed: layered dedup + conflict-safe insert ──────────────────────────


class TestIngestFeed:
    def _db(self, existing_rows: list[tuple], inserted_rowcount: int) -> AsyncMock:
        select_result = MagicMock()
        select_result.all.return_value = existing_rows
        insert_result = MagicMock()
        insert_result.rowcount = inserted_rowcount

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[select_result, insert_result])
        return db

    @pytest.mark.asyncio
    async def test_ingests_new_items_via_conflict_safe_insert(self):
        fetcher = RSSFetcher()
        fetcher.fetch_feed = AsyncMock(return_value=[_item("brand new article body")])
        db = self._db(existing_rows=[], inserted_rowcount=1)
        try:
            count = await fetcher.ingest_feed(db, "Feed", "https://feed.example")
        finally:
            await fetcher.close()

        assert count == 1
        assert db.execute.await_count == 2  # one batched SELECT + one INSERT
        db.commit.assert_awaited_once()

        insert_stmt = db.execute.await_args_list[1].args[0]
        compiled = str(
            insert_stmt.compile(
                dialect=__import__(
                    "sqlalchemy.dialects.postgresql", fromlist=["dialect"]
                ).dialect()
            )
        )
        assert "ON CONFLICT DO NOTHING" in compiled

    @pytest.mark.asyncio
    async def test_existing_exact_hash_skipped_without_insert(self):
        body = "already stored article"
        exact = hashlib.sha256(body.encode("utf-8")).hexdigest()
        fetcher = RSSFetcher()
        fetcher.fetch_feed = AsyncMock(return_value=[_item(body)])
        db = self._db(existing_rows=[(exact, normalized_content_hash(body))], inserted_rowcount=0)
        try:
            count = await fetcher.ingest_feed(db, "Feed", "https://feed.example")
        finally:
            await fetcher.close()

        assert count == 0
        assert db.execute.await_count == 1  # SELECT only — no INSERT issued
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_existing_normalized_hash_skips_near_duplicate(self):
        stored = "TCS Q4 net profit rises 12% — beats estimates"
        syndicated = 'by UNI - TCS Q4 net profit rises 12%, "beats" estimates'
        fetcher = RSSFetcher()
        fetcher.fetch_feed = AsyncMock(return_value=[_item(syndicated)])
        db = self._db(
            existing_rows=[
                (
                    hashlib.sha256(stored.encode("utf-8")).hexdigest(),
                    normalized_content_hash(stored),
                )
            ],
            inserted_rowcount=0,
        )
        try:
            count = await fetcher.ingest_feed(db, "Feed", "https://feed.example")
        finally:
            await fetcher.close()

        assert count == 0
        assert db.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_empty_feed_is_a_noop(self):
        fetcher = RSSFetcher()
        fetcher.fetch_feed = AsyncMock(return_value=[])
        db = AsyncMock()
        try:
            count = await fetcher.ingest_feed(db, "Feed", "https://feed.example")
        finally:
            await fetcher.close()

        assert count == 0
        db.execute.assert_not_awaited()
