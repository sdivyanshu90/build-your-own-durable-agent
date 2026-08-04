"""Deterministic providers, clock, IDs, and failure injection for offline operation."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from pydantic import BaseModel

from durable_agent.domain.errors import ProviderRetryableError
from durable_agent.domain.models import RetrievalItem

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class DeterministicClock:
    """Manually advanced UTC clock; sleep advances without wall time."""

    def __init__(self, start: datetime | None = None) -> None:
        self._current = start or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._current

    async def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep cannot be negative")
        self._current += timedelta(seconds=seconds)
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        self._current += timedelta(seconds=seconds)


class DeterministicIdentifiers:
    """Stable monotonically increasing identifiers by prefix."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def new(self, prefix: str) -> str:
        count = self._counts.get(prefix, 0) + 1
        self._counts[prefix] = count
        return f"{prefix}-{count:06d}"


class HashEmbeddingProvider:
    """Small deterministic token hashing embeddings for tests/local fallback."""

    def __init__(self, dimensions: int = 32) -> None:
        if dimensions < 2:
            raise ValueError("dimensions must be at least two")
        self._dimensions = dimensions

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        vectors = []
        for text in texts:
            vector = [0.0] * self._dimensions
            for token in text.lower().split():
                digest = hashlib.sha256(token.encode()).digest()
                index = int.from_bytes(digest[:4], "big") % self._dimensions
                vector[index] += 1 if digest[4] % 2 else -1
            vectors.append(vector)
        return vectors


class FakeSearchProvider:
    def __init__(self, items: Sequence[RetrievalItem]) -> None:
        self._items = tuple(items)
        self.queries: list[str] = []

    async def search(self, query: str, *, limit: int) -> Sequence[RetrievalItem]:
        self.queries.append(query)
        terms = set(query.lower().split())
        ranked = sorted(
            self._items,
            key=lambda item: (-len(terms & set(item.content.lower().split())), item.item_id),
        )
        return ranked[:limit]


class FakeLLM:
    """Schema-keyed completion queue with optional transient failures."""

    def __init__(
        self, responses: Sequence[BaseModel | dict[str, Any]], *, fail_first: int = 0
    ) -> None:
        self._responses = list(responses)
        self._failures = fail_first
        self.calls = 0

    async def complete_structured(
        self,
        *,
        instructions: str,
        untrusted_content: str,
        output_schema: type[SchemaT],
    ) -> SchemaT:
        del instructions, untrusted_content
        self.calls += 1
        if self.calls <= self._failures:
            raise ProviderRetryableError("injected transient provider failure")
        if not self._responses:
            raise ValueError("fake LLM response queue is empty")
        response = self._responses.pop(0)
        if isinstance(response, BaseModel):
            return output_schema.model_validate(response.model_dump())
        return output_schema.model_validate(response)


class FailureInjector:
    """Deterministically fail named boundaries a configured number of times."""

    def __init__(self, failures: dict[str, int] | None = None) -> None:
        self._remaining = dict(failures or {})
        self.attempts: dict[str, int] = {}

    def hit(self, boundary: str) -> None:
        self.attempts[boundary] = self.attempts.get(boundary, 0) + 1
        remaining = self._remaining.get(boundary, 0)
        if remaining > 0:
            self._remaining[boundary] = remaining - 1
            raise ProviderRetryableError(f"injected transient failure at {boundary}")
