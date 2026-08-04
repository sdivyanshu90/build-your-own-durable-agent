"""Keyword, semantic, and reciprocal-rank hybrid retrieval."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence

from durable_agent.domain.models import RepositoryChunk, RetrievalItem
from durable_agent.domain.protocols import EmbeddingProvider
from durable_agent.observability import METRICS

_TERM = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")


class RetrievalEngine:
    """Search repository chunks while preserving exact line provenance."""

    def __init__(
        self, chunks: Sequence[RepositoryChunk], *, embeddings: EmbeddingProvider | None = None
    ) -> None:
        self._chunks = tuple(chunks)
        self._embeddings = embeddings

    def keyword(self, query: str, *, limit: int = 10) -> tuple[RetrievalItem, ...]:
        """Rank by normalized term frequency with path/symbol boosts."""
        if limit < 1:
            raise ValueError("limit must be positive")
        terms = Counter(item.lower() for item in _TERM.findall(query))
        if not terms:
            return ()
        ranked: list[tuple[float, RepositoryChunk]] = []
        for chunk in self._chunks:
            content_terms = Counter(item.lower() for item in _TERM.findall(chunk.content))
            path = chunk.relative_path.lower()
            symbol = (chunk.symbol_name or "").lower()
            score = sum(
                count * math.log1p(content_terms.get(term, 0))
                + (2 if term in path else 0)
                + (3 if term in symbol else 0)
                for term, count in terms.items()
            )
            if score:
                ranked.append((score / math.sqrt(max(len(content_terms), 1)), chunk))
        ranked.sort(key=lambda item: (-item[0], item[1].relative_path, item[1].start_line))
        result = tuple(self._item(chunk, score) for score, chunk in ranked[:limit])
        METRICS.retrievals.labels(strategy="keyword").inc(len(result))
        return result

    async def semantic(self, query: str, *, limit: int = 10) -> tuple[RetrievalItem, ...]:
        """Rank by cosine similarity using the configured embedding adapter."""
        if self._embeddings is None:
            return ()
        vectors = await self._embeddings.embed([query, *(chunk.content for chunk in self._chunks)])
        if len(vectors) != len(self._chunks) + 1:
            raise ValueError("embedding provider returned an unexpected vector count")
        query_vector = vectors[0]
        ranked = [
            (self._cosine(query_vector, vector), chunk)
            for vector, chunk in zip(vectors[1:], self._chunks, strict=True)
        ]
        ranked = [item for item in ranked if item[0] > 0]
        ranked.sort(key=lambda item: (-item[0], item[1].relative_path, item[1].start_line))
        result = tuple(self._item(chunk, score) for score, chunk in ranked[:limit])
        METRICS.retrievals.labels(strategy="semantic").inc(len(result))
        return result

    async def hybrid(self, query: str, *, limit: int = 10) -> tuple[RetrievalItem, ...]:
        """Fuse keyword and semantic ranks without mixing incomparable raw scores."""
        keyword = self.keyword(query, limit=max(limit * 3, 10))
        semantic = await self.semantic(query, limit=max(limit * 3, 10))
        if not semantic:
            return keyword[:limit]
        by_id = {item.item_id: item for item in (*keyword, *semantic)}
        fused: dict[str, float] = {}
        for results in (keyword, semantic):
            for rank, item in enumerate(results, start=1):
                fused[item.item_id] = fused.get(item.item_id, 0.0) + 1 / (60 + rank)
        ordered = sorted(fused, key=lambda item_id: (-fused[item_id], item_id))[:limit]
        result = tuple(
            by_id[item_id].model_copy(update={"score": fused[item_id]}) for item_id in ordered
        )
        METRICS.retrievals.labels(strategy="hybrid").inc(len(result))
        return result

    @staticmethod
    def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
        if len(left) != len(right) or not left:
            raise ValueError("embedding vectors must have equal non-zero dimensions")
        numerator = sum(a * b for a, b in zip(left, right, strict=True))
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if not left_norm or not right_norm:
            return 0.0
        return numerator / (left_norm * right_norm)

    @staticmethod
    def _item(chunk: RepositoryChunk, score: float) -> RetrievalItem:
        return RetrievalItem(
            item_id=chunk.chunk_id,
            source_type="repository_chunk",
            source=chunk.relative_path,
            source_location=f"{chunk.relative_path}:{chunk.start_line}-{chunk.end_line}",
            content=chunk.content,
            content_hash=chunk.content_hash,
            score=score,
            snapshot_id=chunk.snapshot_id,
            metadata={"symbol": chunk.symbol_name, "language": chunk.language},
        )
