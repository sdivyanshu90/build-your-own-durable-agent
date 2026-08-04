"""Context units and provenance-preserving summaries."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from durable_agent.domain.base import DomainModel, utc_now
from durable_agent.domain.enums import SummaryLevel


class ContextItem(DomainModel):
    item_id: str
    category: str
    content: str
    estimated_tokens: int = Field(ge=0)
    priority: int = Field(default=100, ge=0, le=1_000)
    source_refs: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    mandatory: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class SummaryRecord(DomainModel):
    summary_id: str
    run_id: str
    level: SummaryLevel
    content: str
    estimated_tokens: int = Field(ge=0)
    source_item_ids: tuple[str, ...] = Field(min_length=1)
    source_hashes: dict[str, str]
    source_summary_ids: tuple[str, ...] = ()
    retained_constraints: tuple[str, ...] = ()
    retained_questions: tuple[str, ...] = ()
    retained_decisions: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    removed_item_ids: tuple[str, ...] = ()
    generation: int = Field(default=1, ge=1, le=3)
    valid: bool = True
    invalidated_reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def source_manifest_complete(self) -> SummaryRecord:
        if set(self.source_hashes) != set(self.source_item_ids):
            raise ValueError("source hashes must exactly cover source items")
        if self.generation > 1 and not self.retained_constraints:
            raise ValueError("repeated summaries must explicitly retain constraints")
        return self


class ContextSnapshot(DomainModel):
    context_id: str
    run_id: str
    task_id: str | None = None
    budget_tokens: int = Field(gt=0)
    used_tokens: int = Field(ge=0)
    item_ids: tuple[str, ...]
    summary_ids: tuple[str, ...] = ()
    removed_item_ids: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def within_budget(self) -> ContextSnapshot:
        if self.used_tokens > self.budget_tokens:
            raise ValueError("context snapshot exceeds its budget")
        return self
