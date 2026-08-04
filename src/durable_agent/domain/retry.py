"""Deterministic, bounded retry policy."""

from __future__ import annotations

import hashlib

from pydantic import Field, model_validator

from durable_agent.domain.base import DomainModel


class RetryPolicy(DomainModel):
    """Exponential backoff with deterministic key-derived jitter."""

    maximum_attempts: int = Field(default=3, ge=1, le=20)
    base_delay_seconds: float = Field(default=0.25, ge=0, le=300)
    maximum_delay_seconds: float = Field(default=30, ge=0, le=3_600)
    multiplier: float = Field(default=2, ge=1, le=10)
    jitter_ratio: float = Field(default=0.2, ge=0, le=1)

    @model_validator(mode="after")
    def valid_delay_bounds(self) -> RetryPolicy:
        if self.maximum_delay_seconds < self.base_delay_seconds:
            raise ValueError("maximum delay must be at least base delay")
        return self

    def should_retry(self, attempt: int, *, retryable: bool) -> bool:
        """Return whether another attempt may start."""
        return retryable and 1 <= attempt < self.maximum_attempts

    def delay_for(self, attempt: int, *, jitter_key: str = "default") -> float:
        """Calculate delay before the next attempt; attempt is one-based."""
        if attempt < 1:
            raise ValueError("attempt must be at least one")
        raw = min(
            self.maximum_delay_seconds,
            self.base_delay_seconds * (self.multiplier ** (attempt - 1)),
        )
        if raw == 0 or self.jitter_ratio == 0:
            return raw
        digest = hashlib.sha256(f"{jitter_key}:{attempt}".encode()).digest()
        unit = int.from_bytes(digest[:8], "big") / (2**64 - 1)
        factor = 1 - self.jitter_ratio + (2 * self.jitter_ratio * unit)
        return min(self.maximum_delay_seconds, raw * factor)
