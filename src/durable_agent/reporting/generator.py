"""Render complete reports and verify every material claim mapping."""

from __future__ import annotations

import json
from collections.abc import Sequence

from pydantic import Field

from durable_agent.domain.base import DomainModel, sha256_digest
from durable_agent.domain.errors import DomainValidationError
from durable_agent.domain.evidence import Claim, EvidenceConflict, EvidenceRecord
from durable_agent.domain.models import PlanSpec, RunRecord, TaskRecord
from durable_agent.domain.protocols import Clock, IdentifierGenerator
from durable_agent.evidence.ledger import EvidenceLedger
from durable_agent.observability import METRICS, span
from durable_agent.reporting.models import ReportBundle, ReportDocument, VerificationEntry


class ReportInput(DomainModel):
    """Explicit inputs for every required report section."""

    run: RunRecord
    plan: PlanSpec
    tasks: tuple[TaskRecord, ...]
    evidence: tuple[EvidenceRecord, ...]
    claims: tuple[Claim, ...]
    executive_summary: str
    changed_artifacts: tuple[str, ...] = ()
    research_findings: tuple[str, ...] = ()
    verification_performed: tuple[VerificationEntry, ...] = ()
    test_results: tuple[str, ...] = ()
    conflicts: tuple[EvidenceConflict, ...] = ()
    failures_and_recoveries: tuple[str, ...] = ()
    remaining_risks: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    recommended_next_actions: tuple[str, ...] = ()
    reproduction_instructions: tuple[str, ...] = Field(min_length=1)
    partial: bool = False


class ReportGenerator:
    """Build Markdown and canonical JSON from the same validated document."""

    def __init__(self, *, identifiers: IdentifierGenerator, clock: Clock) -> None:
        self._ids = identifiers
        self._clock = clock

    def generate(self, data: ReportInput) -> ReportBundle:
        """Verify claim links, build all sections, and return integrity hashes."""
        evidence = {item.evidence_id: item for item in data.evidence}
        for claim in data.claims:
            EvidenceLedger.verify_claim(claim, evidence)
        work = tuple(
            f"{task.spec.title}: {task.state.value} after {task.attempt_count} attempt(s)"
            for task in data.tasks
        )
        document = ReportDocument(
            report_id=self._ids.new("report"),
            run_id=data.run.run_id,
            partial=data.partial,
            executive_summary=data.executive_summary,
            original_objective=data.run.objective,
            scope=data.plan.scope,
            assumptions=data.plan.assumptions,
            plan_summary=tuple(
                f"{task.task_id}: {task.title} "
                f"(depends on {', '.join(task.dependencies) or 'nothing'})"
                for task in data.plan.tasks
            ),
            work_completed=work,
            changed_artifacts=data.changed_artifacts,
            research_findings=data.research_findings,
            verification_performed=data.verification_performed,
            test_results=data.test_results,
            claims=data.claims,
            evidence=data.evidence,
            conflicts=data.conflicts,
            failures_and_recoveries=data.failures_and_recoveries,
            remaining_risks=data.remaining_risks,
            limitations=data.limitations,
            recommended_next_actions=data.recommended_next_actions,
            reproduction_instructions=data.reproduction_instructions,
            created_at=self._clock.now(),
        )
        with span("report.generate", {"run.id": data.run.run_id, "report.partial": data.partial}):
            markdown = self._markdown(document)
            json_text = (
                json.dumps(document.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
            )
        bundle = ReportBundle(
            report=document,
            markdown=markdown,
            json_text=json_text,
            markdown_hash=sha256_digest(markdown),
            json_hash=sha256_digest(json_text),
        )
        self.verify(bundle)
        METRICS.reports_generated.labels(partial=str(data.partial).lower()).inc()
        return bundle

    @staticmethod
    def verify(bundle: ReportBundle) -> None:
        """Verify hashes, identity, citations, and the full JSON relationship graph."""
        if sha256_digest(bundle.markdown) != bundle.markdown_hash:
            raise DomainValidationError("Markdown report hash mismatch")
        if sha256_digest(bundle.json_text) != bundle.json_hash:
            raise DomainValidationError("JSON report hash mismatch")
        parsed = json.loads(bundle.json_text)
        if parsed["report_id"] != bundle.report.report_id:
            raise DomainValidationError("JSON report identity mismatch")
        if parsed != bundle.report.model_dump(mode="json"):
            raise DomainValidationError("JSON report content does not match report document")
        evidence = {item.evidence_id: item for item in bundle.report.evidence}
        for claim in bundle.report.claims:
            EvidenceLedger.verify_claim(claim, evidence)
            for evidence_id in claim.evidence_ids:
                if f"[{evidence_id}]" not in bundle.markdown:
                    raise DomainValidationError(f"Markdown is missing citation [{evidence_id}]")
        for conflict in bundle.report.conflicts:
            if conflict.run_id != bundle.report.run_id:
                raise DomainValidationError("conflict belongs to another run")
            missing = set(conflict.evidence_ids) - evidence.keys()
            if missing:
                raise DomainValidationError(
                    f"conflict references missing evidence: {sorted(missing)}"
                )
            for evidence_id in conflict.evidence_ids:
                if f"[{evidence_id}]" not in bundle.markdown:
                    raise DomainValidationError(
                        f"Markdown is missing conflict citation [{evidence_id}]"
                    )

    @classmethod
    def _markdown(cls, report: ReportDocument) -> str:
        status = "Partial" if report.partial else "Final"
        lines = [
            f"# {status} report: {report.run_id}",
            "",
            "## Executive summary",
            "",
            report.executive_summary,
        ]
        cls._section(lines, "Original objective", (report.original_objective,))
        cls._section(lines, "Scope", report.scope)
        cls._section(lines, "Assumptions", report.assumptions)
        cls._section(lines, "Plan summary", report.plan_summary)
        cls._section(lines, "Work completed", report.work_completed)
        cls._section(lines, "Files or artifacts changed", report.changed_artifacts)
        cls._section(lines, "Research findings", report.research_findings)
        verification = tuple(
            f"`{entry.command}`: {entry.outcome} (exit {entry.exit_code}) "
            + " ".join(f"[{item}]" for item in entry.evidence_ids)
            for entry in report.verification_performed
        )
        cls._section(lines, "Verification performed", verification)
        cls._section(lines, "Test results", report.test_results)
        claim_lines = tuple(
            f"**{claim.kind.value}** — {claim.text} "
            + " ".join(f"[{item}]" for item in claim.evidence_ids)
            for claim in report.claims
        )
        cls._section(lines, "Claim-to-evidence mapping", claim_lines)
        conflict_lines = tuple(
            f"{conflict.description} " + " ".join(f"[{item}]" for item in conflict.evidence_ids)
            for conflict in report.conflicts
        )
        cls._section(lines, "Conflicting evidence", conflict_lines)
        cls._section(lines, "Failures and recoveries", report.failures_and_recoveries)
        cls._section(lines, "Remaining risks", report.remaining_risks)
        cls._section(lines, "Limitations", report.limitations)
        cls._section(lines, "Recommended next actions", report.recommended_next_actions)
        cls._section(
            lines, "Reproduction instructions", report.reproduction_instructions, ordered=True
        )
        lines.extend(["", "## Evidence ledger", ""])
        for evidence in report.evidence:
            location = f" at {evidence.source_location}" if evidence.source_location else ""
            lines.append(
                f"- **{evidence.evidence_id}** — {evidence.evidence_type.value}: "
                f"{evidence.source}{location}; {evidence.verification_status.value}; "
                f"hash `{evidence.content_hash or 'not-applicable'}`"
            )
        lines.extend(["", f"Generated: {report.created_at.isoformat()}", ""])
        return "\n".join(lines)

    @staticmethod
    def _section(
        lines: list[str], title: str, values: Sequence[str], *, ordered: bool = False
    ) -> None:
        lines.extend(["", f"## {title}", ""])
        if not values:
            lines.append("- None recorded.")
            return
        for index, value in enumerate(values, start=1):
            marker = f"{index}." if ordered else "-"
            lines.append(f"{marker} {value}")
