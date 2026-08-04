from __future__ import annotations

import pytest

from durable_agent.domain.models import RepositoryChunk
from durable_agent.providers.fakes import HashEmbeddingProvider
from durable_agent.retrieval import RetrievalEngine


def chunks() -> tuple[RepositoryChunk, ...]:
    return (
        RepositoryChunk(
            chunk_id="retry",
            file_id="f1",
            snapshot_id="s1",
            relative_path="service/retry.py",
            content="configure retry limit attempts",
            content_hash="a" * 64,
            start_line=2,
            end_line=5,
            symbol_name="RetryPolicy",
        ),
        RepositoryChunk(
            chunk_id="docs",
            file_id="f2",
            snapshot_id="s1",
            relative_path="README.md",
            content="service documentation and examples",
            content_hash="b" * 64,
            start_line=1,
            end_line=3,
        ),
    )


def test_keyword_search_returns_line_provenance() -> None:
    results = RetrievalEngine(chunks()).keyword("retry limit")
    assert [item.item_id for item in results] == ["retry"]
    assert results[0].source_location == "service/retry.py:2-5"
    assert results[0].snapshot_id == "s1"


@pytest.mark.asyncio
async def test_semantic_and_hybrid_search() -> None:
    engine = RetrievalEngine(chunks(), embeddings=HashEmbeddingProvider())
    semantic = await engine.semantic("retry attempts")
    hybrid = await engine.hybrid("retry attempts")
    assert semantic
    assert hybrid[0].item_id == "retry"


@pytest.mark.asyncio
async def test_hybrid_without_embeddings_falls_back_to_keyword() -> None:
    engine = RetrievalEngine(chunks())
    assert await engine.hybrid("documentation") == engine.keyword("documentation")


def test_empty_query_and_invalid_limit() -> None:
    engine = RetrievalEngine(chunks())
    assert engine.keyword("!") == ()
    with pytest.raises(ValueError, match="positive"):
        engine.keyword("retry", limit=0)
