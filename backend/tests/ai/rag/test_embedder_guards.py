"""
Embedder guard tests
====================
WS2: ``embed_texts`` must fail loudly on a count mismatch (positional
misalignment risk) or a dimension mismatch (model/config drift), never return
a silently corrupted batch.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.rag.embedder import embed_texts
from app.core.config import get_settings

pytestmark = pytest.mark.unit

_DIM = get_settings().GEMINI_EMBED_DIM


def _client_returning(batches: list[list[list[float]]]) -> MagicMock:
    client = MagicMock()
    client.embed = AsyncMock(side_effect=batches)
    return client


class TestEmbedTextsGuards:
    @pytest.mark.asyncio
    async def test_count_mismatch_raises_runtime_error(self):
        client = _client_returning([[[0.0] * _DIM]])  # 2 texts in, 1 vector out
        with patch("app.ai.rag.embedder.get_intelligence_client", return_value=client):
            with pytest.raises(RuntimeError, match="count mismatch"):
                await embed_texts(["a", "b"])

    @pytest.mark.asyncio
    async def test_dimension_mismatch_raises_runtime_error(self):
        client = _client_returning([[[0.0] * (_DIM - 1)]])
        with patch("app.ai.rag.embedder.get_intelligence_client", return_value=client):
            with pytest.raises(RuntimeError, match="dimension mismatch"):
                await embed_texts(["a"])

    @pytest.mark.asyncio
    async def test_aligned_batches_concatenate_in_order(self):
        first = [[1.0] + [0.0] * (_DIM - 1), [2.0] + [0.0] * (_DIM - 1)]
        second = [[3.0] + [0.0] * (_DIM - 1)]
        client = _client_returning([first, second])
        with patch("app.ai.rag.embedder.get_intelligence_client", return_value=client):
            vectors = await embed_texts(["a", "b", "c"], batch_size=2)

        assert len(vectors) == 3
        assert [v[0] for v in vectors] == [1.0, 2.0, 3.0]
        assert client.embed.await_count == 2

    @pytest.mark.asyncio
    async def test_empty_input_short_circuits(self):
        client = _client_returning([])
        with patch("app.ai.rag.embedder.get_intelligence_client", return_value=client):
            assert await embed_texts([]) == []
        client.embed.assert_not_awaited()
