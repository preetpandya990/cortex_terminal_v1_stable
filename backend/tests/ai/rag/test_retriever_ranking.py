"""
Retriever ranking tests
=======================
WS3: source credibility joins BM25 + cosine as a third (half-weight) RRF
term, and ingest-tagged low-confidence docs are rank-demoted. These tests
exercise ``_credibility_ranks`` + ``_rrf_merge`` directly — the pure ranking
core — with hand-built candidates.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.ai.rag.retriever import (
    _Candidate,
    _credibility_ranks,
    _rrf_merge,
)

pytestmark = pytest.mark.unit


def _candidate(
    idx: int,
    source_name: str = "Feed",
    low_confidence: bool = False,
) -> _Candidate:
    return _Candidate(
        source_id=idx,
        content=f"doc {idx}",
        source_name=source_name,
        source_url="https://example.com",
        as_of_timestamp=datetime(2026, 7, 6, tzinfo=timezone.utc),
        embedding=[0.0],
        symbol=None,
        low_confidence=low_confidence,
    )


class TestCredibilityRanks:
    def test_dense_rank_shared_within_source(self):
        candidates = [
            _candidate(0, "Exchange"),   # 80
            _candidate(1, "Exchange"),   # 80
            _candidate(2, "Blog"),       # 20
        ]
        ranks = _credibility_ranks(candidates, {"Exchange": 80.0, "Blog": 20.0})
        assert ranks[0] == ranks[1] == 1  # same source → same dense rank
        assert ranks[2] == 2

    def test_unknown_source_gets_neutral_default(self):
        candidates = [_candidate(0, "Exchange"), _candidate(1, "Never-seen")]
        ranks = _credibility_ranks(candidates, {"Exchange": 80.0})
        # Unknown (50 default) ranks below the 80-score exchange feed.
        assert ranks[0] == 1
        assert ranks[1] == 2


class TestRrfMergeWithCredibility:
    def test_credibility_breaks_relevance_tie(self):
        """Two docs with symmetric relevance ranks: authority decides."""
        candidates = [_candidate(0, "TrustedExchange"), _candidate(1, "LowBlog")]
        # Symmetric relevance: doc0 wins vector, doc1 wins BM25 — RRF tie.
        chunks = _rrf_merge(
            vector_ranks=[0, 1],
            bm25_ranks=[1, 0],
            candidates=candidates,
            top_k=2,
            credibility_ranks=_credibility_ranks(
                candidates, {"TrustedExchange": 90.0, "LowBlog": 10.0}
            ),
        )
        assert [c.source_id for c in chunks] == [0, 1]

    def test_credibility_never_pulls_unranked_doc_into_results(self):
        """A doc absent from both relevance lists gets no credibility rescue."""
        candidates = [_candidate(0, "Blog"), _candidate(1, "TrustedExchange")]
        chunks = _rrf_merge(
            vector_ranks=[0],
            bm25_ranks=[0],
            candidates=candidates,
            top_k=2,
            credibility_ranks=_credibility_ranks(
                candidates, {"TrustedExchange": 99.0, "Blog": 10.0}
            ),
        )
        assert [c.source_id for c in chunks] == [0]  # doc1 not retrieved

    def test_relevance_dominates_credibility(self):
        """A clearly more relevant doc beats a more credible weaker one."""
        candidates = [_candidate(0, "Blog"), _candidate(1, "TrustedExchange")]
        # doc0 tops BOTH relevance lists; doc1 trails in both.
        chunks = _rrf_merge(
            vector_ranks=[0, 1],
            bm25_ranks=[0, 1],
            candidates=candidates,
            top_k=2,
            credibility_ranks=_credibility_ranks(
                candidates, {"TrustedExchange": 99.0, "Blog": 10.0}
            ),
        )
        assert [c.source_id for c in chunks] == [0, 1]

    def test_low_confidence_doc_demoted_behind_comparable_peer(self):
        candidates = [
            _candidate(0, "Feed", low_confidence=True),
            _candidate(1, "Feed"),
        ]
        # Symmetric relevance (tie) — demotion decides.
        chunks = _rrf_merge(
            vector_ranks=[0, 1],
            bm25_ranks=[1, 0],
            candidates=candidates,
            top_k=2,
            credibility_ranks=_credibility_ranks(candidates, {"Feed": 50.0}),
        )
        assert [c.source_id for c in chunks] == [1, 0]

    def test_low_confidence_doc_still_retrievable(self):
        candidates = [_candidate(0, "Feed", low_confidence=True)]
        chunks = _rrf_merge(
            vector_ranks=[0],
            bm25_ranks=[0],
            candidates=candidates,
            top_k=1,
            credibility_ranks=_credibility_ranks(candidates, {"Feed": 50.0}),
        )
        assert len(chunks) == 1  # demoted, never dropped

    def test_no_credibility_ranks_degrades_to_pure_relevance(self):
        candidates = [_candidate(0), _candidate(1)]
        chunks = _rrf_merge(
            vector_ranks=[1, 0],
            bm25_ranks=[1, 0],
            candidates=candidates,
            top_k=2,
            credibility_ranks=None,
        )
        assert [c.source_id for c in chunks] == [1, 0]
