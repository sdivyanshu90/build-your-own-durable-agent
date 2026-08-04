"""Machine-readable final report schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from durable_agent.domain.base import DomainModel, utc_now
from durable_agent.domain.evidence import Claim, EvidenceConflict, EvidenceRecord


class VerificationEntry(DomainModel):
    command: str
    exit_code: int | None
    outcome: str
    evidence_ids: tuple[str, ...]


class ReportDocument(DomainModel):
    report_id: str
    run_id: str
    partial: bool = False
    executive_summary: str
    original_objective: str
    scope: tuple[str, ...]
    assumptions: tuple[str, ...]
    plan_summary: tuple[str, ...]
    work_completed: tuple[str, ...]
    changed_artifacts: tuple[str, ...]
    research_findings: tuple[str, ...]
    verification_performed: tuple[VerificationEntry, ...]
    test_results: tuple[str, ...]
    claims: tuple[Claim, ...]
    evidence: tuple[EvidenceRecord, ...]
    conflicts: tuple[EvidenceConflict, ...] = ()
    failures_and_recoveries: tuple[str, ...] = ()
    remaining_risks: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    recommended_next_actions: tuple[str, ...] = ()
    reproduction_instructions: tuple[str, ...]
    created_at: datetime = Field(default_factory=utc_now)


class ReportBundle(DomainModel):
    report: ReportDocument
    markdown: str
    json_text: str
    markdown_hash: str
    json_hash: str
