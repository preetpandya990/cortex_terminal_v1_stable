"""
Retriever ranking tests
=======================
WS3: source credibility joins BM25 + cosine as a third (half-weight) RRF
term, and ingest-tagged low-confidence docs are rank-demoted. These tests
exercise ``_credibility_ranks`` + ``_rrf_merge`` directly — the pure ranking
core — with hand-built candidates.

Relevance-gate tests (NEWS_CONTEXT_RELEVANCE_GAP_FINDING.md fix): exercise
``_cosine_similarities`` + ``_apply_relevance_gate`` directly, confirming
"exact"-tier candidates bypass the floor while "sector"/"generic" candidates
must clear it or are dropped from the pool entirely (not merely down-ranked).
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from app.ai.rag.retriever import (
    _apply_relevance_gate,
    _Candidate,
    _cosine_similarities,
    _credibility_ranks,
    _rrf_merge,
)

pytestmark = pytest.mark.unit


def _candidate(
    idx: int,
    source_name: str = "Feed",
    low_confidence: bool = False,
    tier: str = "generic",
    embedding: list[float] | None = None,
    symbol: str | None = None,
) -> _Candidate:
    return _Candidate(
        source_id=idx,
        content=f"doc {idx}",
        source_name=source_name,
        source_url="https://example.com",
        as_of_timestamp=datetime(2026, 7, 6, tzinfo=timezone.utc),
        embedding=embedding if embedding is not None else [0.0],
        symbol=symbol,
        low_confidence=low_confidence,
        tier=tier,
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


class TestCosineSimilarities:
    def test_identical_vector_scores_near_one(self):
        candidates = [_candidate(0, embedding=[1.0, 0.0, 0.0])]
        sims = _cosine_similarities(candidates, [1.0, 0.0, 0.0])
        assert sims.shape == (1,)
        assert sims[0] == pytest.approx(1.0)

    def test_orthogonal_vector_scores_near_zero(self):
        candidates = [_candidate(0, embedding=[1.0, 0.0])]
        sims = _cosine_similarities(candidates, [0.0, 1.0])
        assert sims[0] == pytest.approx(0.0)

    def test_opposite_vector_scores_near_negative_one(self):
        candidates = [_candidate(0, embedding=[1.0, 0.0])]
        sims = _cosine_similarities(candidates, [-1.0, 0.0])
        assert sims[0] == pytest.approx(-1.0)

    def test_zero_embedding_does_not_raise(self):
        """A zero-vector embedding must not divide-by-zero (avoid_zero_division guard)."""
        candidates = [_candidate(0, embedding=[0.0, 0.0])]
        sims = _cosine_similarities(candidates, [1.0, 0.0])
        assert np.isfinite(sims[0])


class TestRelevanceGate:
    """
    The relevance gate (retriever._apply_relevance_gate) is the core fix for
    NEWS_CONTEXT_RELEVANCE_GAP_FINDING.md: "exact"-tier candidates (already
    validated at ingestion by event_classifier) bypass the floor outright.
    "sector" and "generic" tier candidates must independently clear the
    floor or are removed from the candidate pool before either ranker (BM25,
    cosine) ever sees them.
    """

    def test_exact_tier_bypasses_floor_even_at_zero_similarity(self):
        # query vector orthogonal to the exact-tier candidate's embedding —
        # similarity is 0.0, which would fail any positive floor — yet it
        # must still be admitted because "exact" tier is trusted outright.
        candidates = [_candidate(0, tier="exact", embedding=[1.0, 0.0])]
        admitted, admitted_by_tier, filtered_by_tier = _apply_relevance_gate(
            candidates, query_vector=[0.0, 1.0], min_similarity=0.9,
        )
        assert [c.source_id for c in admitted] == [0]
        assert admitted_by_tier == {"exact": 1}
        assert filtered_by_tier == {}

    def test_generic_tier_below_floor_is_dropped_from_pool(self):
        candidates = [_candidate(0, tier="generic", embedding=[1.0, 0.0])]
        admitted, admitted_by_tier, filtered_by_tier = _apply_relevance_gate(
            candidates, query_vector=[0.0, 1.0], min_similarity=0.5,
        )
        assert admitted == []
        assert admitted_by_tier == {"exact": 0}
        assert filtered_by_tier == {"generic": 1}

    def test_generic_tier_above_floor_is_admitted(self):
        candidates = [_candidate(0, tier="generic", embedding=[1.0, 0.0])]
        admitted, admitted_by_tier, filtered_by_tier = _apply_relevance_gate(
            candidates, query_vector=[1.0, 0.0], min_similarity=0.5,
        )
        assert [c.source_id for c in admitted] == [0]
        assert admitted_by_tier == {"exact": 0, "generic": 1}
        assert filtered_by_tier == {}

    def test_sector_tier_follows_same_floor_as_generic_not_exempted(self):
        """
        Sector adjacency is a candidate-generation heuristic, not a
        relevance guarantee (two companies in the same sector aren't
        automatically mutually relevant) — unlike "exact", "sector" gets no
        exemption from the floor.
        """
        candidates = [_candidate(0, tier="sector", embedding=[1.0, 0.0])]
        admitted, _admitted_by_tier, filtered_by_tier = _apply_relevance_gate(
            candidates, query_vector=[0.0, 1.0], min_similarity=0.5,
        )
        assert admitted == []
        assert filtered_by_tier == {"sector": 1}

    def test_reproduces_bug_shape_empty_exact_tier_low_similarity_generic_only(self):
        """
        Regression test for the exact bug shape in
        NEWS_CONTEXT_RELEVANCE_GAP_FINDING.md: no exact-tier coverage, only a
        topically unrelated (low-similarity) generic-tier candidate (the
        SK Hynix-shaped doc for a COMSYN-shaped query) — must be fully
        filtered, leaving nothing for BM25/cosine/RRF to ever rank.
        """
        candidates = [_candidate(0, tier="generic", embedding=[1.0, 0.0])]
        admitted, admitted_by_tier, filtered_by_tier = _apply_relevance_gate(
            candidates, query_vector=[0.0, 1.0], min_similarity=0.5,
        )
        assert admitted == []
        assert sum(admitted_by_tier.values()) == 0
        assert filtered_by_tier == {"generic": 1}

    def test_sector_tier_candidate_above_floor_closes_missed_true_positive_gap(self):
        """
        Before this fix, a genuinely relevant sector-peer company's news
        (tagged with its own real symbol, never NULL) was never even queried
        for. This proves a sector-tier candidate that clears the floor is
        retrievable even though the exact tier is completely empty.
        """
        candidates = [
            _candidate(0, tier="sector", symbol="PEERCO", embedding=[1.0, 0.0]),
        ]
        admitted, admitted_by_tier, filtered_by_tier = _apply_relevance_gate(
            candidates, query_vector=[1.0, 0.0], min_similarity=0.5,
        )
        assert [c.source_id for c in admitted] == [0]
        assert admitted_by_tier == {"exact": 0, "sector": 1}
        assert filtered_by_tier == {}

    def test_mixed_tiers_exact_admitted_regardless_others_gated_independently(self):
        candidates = [
            _candidate(0, tier="exact", embedding=[0.0, 1.0]),       # bypasses floor
            _candidate(1, tier="sector", embedding=[0.0, 1.0]),      # fails floor
            _candidate(2, tier="generic", embedding=[0.9, 0.1]),     # clears floor
        ]
        admitted, admitted_by_tier, filtered_by_tier = _apply_relevance_gate(
            candidates, query_vector=[1.0, 0.0], min_similarity=0.5,
        )
        assert {c.source_id for c in admitted} == {0, 2}
        assert admitted_by_tier == {"exact": 1, "generic": 1}
        assert filtered_by_tier == {"sector": 1}
