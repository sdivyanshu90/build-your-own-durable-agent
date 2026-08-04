"""Normalized research sources, extracted facts, and conflicts."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, HttpUrl

from durable_agent.domain.base import DomainModel, utc_now


class ResearchSource(DomainModel):
    source_id: str
    url: HttpUrl | None = None
    title: str
    normalized_content: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    author: str | None = None
    publisher: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=utc_now)
    media_type: str = "text/plain"
    quality_score: float = Field(default=0.5, ge=0, le=1)
    quality_notes: tuple[str, ...] = ()
    injection_indicators: tuple[str, ...] = ()


class ResearchFact(DomainModel):
    fact_id: str
    key: str
    value: str
    source_ids: tuple[str, ...]
    verified: bool = False
    agent_inference: bool = False


class ResearchConflict(DomainModel):
    conflict_id: str
    key: str
    values: dict[str, tuple[str, ...]]
    explanation: str
    resolved: bool = False
