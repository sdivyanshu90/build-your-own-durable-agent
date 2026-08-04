"""Immutable evidence and claim schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from durable_agent.domain.base import DomainModel, utc_now
from durable_agent.domain.enums import ClaimKind, EvidenceType, VerificationStatus


class EvidenceRecord(DomainModel):
    """One primary or explicitly inferred unit of support."""

    evidence_id: str = Field(pattern=r"^EVID-[A-Za-z0-9_-]+$")
    run_id: str
    evidence_type: EvidenceType
    source: str
    source_location: str | None = None
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    snapshot_id: str | None = None
    related_task_id: str | None = None
    reliability: float = Field(default=0.5, ge=0, le=1)
    excerpt: str = Field(max_length=4_000)
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def primary_evidence_has_hash(self) -> EvidenceRecord:
        primary = {
            EvidenceType.REPOSITORY_FILE,
            EvidenceType.REPOSITORY_SNAPSHOT,
            EvidenceType.TEST_RESULT,
            EvidenceType.COMMAND_RESULT,
            EvidenceType.ARTIFACT,
            EvidenceType.EXTERNAL_SOURCE,
        }
        if self.evidence_type in primary and self.content_hash is None:
            raise ValueError("primary evidence requires a content hash")
        return self


class Claim(DomainModel):
    """Report claim with explicit epistemic status and evidence links."""

    claim_id: str = Field(pattern=r"^CLAIM-[A-Za-z0-9_-]+$")
    run_id: str
    text: str = Field(min_length=1, max_length=10_000)
    kind: ClaimKind
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    related_task_id: str | None = None

    @model_validator(mode="after")
    def evidence_required_for_supported_kinds(self) -> Claim:
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("evidence IDs must be unique")
        return self


class EvidenceConflict(DomainModel):
    """Two or more records supporting incompatible assertions."""

    conflict_id: str
    run_id: str
    description: str
    evidence_ids: tuple[str, ...] = Field(min_length=2)
    resolved: bool = False
    resolution: str | None = None
