"""Production system clock and collision-resistant identifiers."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from durable_agent.domain.errors import SecurityPolicyError
from durable_agent.domain.models import RetrievalItem


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class UUIDIdentifiers:
    """Opaque stable identifiers without process-local counters."""

    def new(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex}"


class DisabledSearchProvider:
    """Fail closed when no operator-supplied research provider is configured."""

    async def search(self, query: str, *, limit: int) -> Sequence[RetrievalItem]:
        del query, limit
        raise SecurityPolicyError("no research search provider is configured")
