"""
RAG ingester dedup + alignment tests
====================================
Covers WS2's positional-integrity guard: ``_build_embed_rows`` must refuse a
(events, vectors) length mismatch instead of silently mispairing embeddings —
a mispaired row is permanent corruption because the anti-join in
``_fetch_unembedded_events`` never re-selects rows that already have an
embedding.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.ai.rag.ingester import _build_embed_rows, _dedup_events

pytestmark = pytest.mark.unit


def _event(event_id: int, content: str, symbols: list | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=event_id,
        raw_content=content,
        source_name="Feed",
        source_url="https://example.com/a",
        event_timestamp=datetime(2026, 7, 6, tzinfo=timezone.utc),
        all_affected_symbols=symbols,
    )


class TestDedupEvents:
    def test_keeps_first_occurrence_of_identical_body(self):
        events = [_event(1, "same"), _event(2, "same"), _event(3, "other")]
        deduped = _dedup_events(events)
        assert [e.id for e in deduped] == [1, 3]

    def test_no_duplicates_passthrough(self):
        events = [_event(1, "a"), _event(2, "b")]
        assert _dedup_events(events) == events


class TestBuildEmbedRows:
    def test_count_mismatch_raises_instead_of_mispairing(self):
        events = [_event(1, "a"), _event(2, "b"), _event(3, "c")]
        vectors = [[0.1], [0.2]]  # provider dropped one
        with pytest.raises(ValueError):
            _build_embed_rows(events, vectors)

    def test_surplus_vectors_also_raise(self):
        events = [_event(1, "a")]
        vectors = [[0.1], [0.2]]
        with pytest.raises(ValueError):
            _build_embed_rows(events, vectors)

    def test_aligned_inputs_build_rows_positionally(self):
        events = [
            _event(1, "single-symbol event", symbols=[["RELIANCE"]]),
            _event(2, "multi-symbol event", symbols=[["TCS", "INFY"]]),
        ]
        vectors = [[0.1], [0.2]]
        rows = _build_embed_rows(events, vectors)

        assert [r["source_id"] for r in rows] == [1, 2]
        assert rows[0]["embedding"] == [0.1]
        assert rows[1]["embedding"] == [0.2]
        # Exactly one affected symbol → direct assignment; 2+ → NULL (general).
        assert rows[0]["symbol"] == "RELIANCE"
        assert rows[1]["symbol"] is None
        assert rows[1]["metadata"]["affected_symbols"] == ["TCS", "INFY"]
