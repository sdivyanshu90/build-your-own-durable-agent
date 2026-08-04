"""Immutable evidence recording and claim-link verification."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from durable_agent.domain.base import canonical_json, sha256_digest
from durable_agent.domain.enums import ClaimKind, EvidenceType, VerificationStatus
from durable_agent.domain.errors import DomainValidationError
from durable_agent.domain.evidence import Claim, EvidenceRecord
from durable_agent.domain.models import RepositoryChunk, ToolResult
from durable_agent.domain.protocols import Clock, EvidenceStore, IdentifierGenerator
from durable_agent.observability import METRICS


class EvidenceLedger:
    """Create immutable evidence IDs and reject unsupported claim relationships."""

    def __init__(
        self,
        *,
        store: EvidenceStore,
        identifiers: IdentifierGenerator,
        clock: Clock,
    ) -> None:
        self._store = store
        self._ids = identifiers
        self._clock = clock

    async def record(
        self,
        *,
        run_id: str,
        evidence_type: EvidenceType,
        source: str,
        excerpt: str,
        content: bytes | str | Mapping[str, Any] | None = None,
        source_location: str | None = None,
        snapshot_id: str | None = None,
        task_id: str | None = None,
        reliability: float = 0.5,
        status: VerificationStatus = VerificationStatus.UNVERIFIED,
        metadata: Mapping[str, str | int | float | bool | None] | None = None,
    ) -> EvidenceRecord:
        """Create and persist one evidence record with canonical integrity."""
        if isinstance(content, Mapping):
            serialized: bytes | str | None = canonical_json(dict(content))
        else:
            serialized = content
        digest = sha256_digest(serialized) if serialized is not None else None
        record = EvidenceRecord(
            evidence_id=f"EVID-{self._ids.new('evidence').split('-')[-1]}",
            run_id=run_id,
            evidence_type=evidence_type,
            source=source,
            source_location=source_location,
            content_hash=digest,
            snapshot_id=snapshot_id,
            related_task_id=task_id,
            reliability=reliability,
            excerpt=excerpt,
            verification_status=status,
            metadata=dict(metadata or {}),
            created_at=self._clock.now(),
        )
        await self._store.add_evidence(record)
        METRICS.evidence_records.labels(type=evidence_type.value).inc()
        return record

    async def record_repository_chunk(
        self, *, run_id: str, task_id: str, chunk: RepositoryChunk
    ) -> EvidenceRecord:
        """Record primary file/line evidence from an immutable snapshot chunk."""
        return await self.record(
            run_id=run_id,
            evidence_type=EvidenceType.REPOSITORY_FILE,
            source=chunk.relative_path,
            source_location=f"{chunk.relative_path}:{chunk.start_line}-{chunk.end_line}",
            content=chunk.content,
            excerpt=chunk.content[:1_000],
            snapshot_id=chunk.snapshot_id,
            task_id=task_id,
            reliability=0.9,
            status=VerificationStatus.VERIFIED,
        )

    async def record_tool_result(
        self,
        *,
        run_id: str,
        task_id: str,
        tool_name: str,
        result: ToolResult,
        test_result: bool = False,
    ) -> EvidenceRecord:
        """Record a bounded tool result, preserving exit status and output hash."""
        return await self.record(
            run_id=run_id,
            evidence_type=(
                EvidenceType.TEST_RESULT if test_result else EvidenceType.COMMAND_RESULT
            ),
            source=tool_name,
            source_location=result.tool_call_id,
            content=result.output,
            excerpt=str(result.output)[:1_000],
            task_id=task_id,
            reliability=0.95 if result.success else 0.8,
            status=VerificationStatus.VERIFIED,
            metadata={
                "success": result.success,
                "exit_code": result.exit_code,
                "truncated": result.truncated,
                "tool_output_hash": result.output_hash,
            },
        )

    async def claim(
        self,
        *,
        run_id: str,
        text: str,
        kind: ClaimKind,
        evidence_ids: Sequence[str] = (),
        task_id: str | None = None,
    ) -> Claim:
        """Persist a claim only after validating its current evidence links."""
        claim = Claim(
            claim_id=f"CLAIM-{self._ids.new('claim').split('-')[-1]}",
            run_id=run_id,
            text=text,
            kind=kind,
            evidence_ids=tuple(evidence_ids),
            related_task_id=task_id,
        )
        evidence = {item.evidence_id: item for item in await self._store.get_evidence(run_id)}
        self.verify_claim(claim, evidence)
        await self._store.add_claim(claim)
        return claim

    @staticmethod
    def verify_claim(claim: Claim, evidence: Mapping[str, EvidenceRecord]) -> None:
        """Reject missing, invalid, cross-run, or epistemically incompatible support."""
        missing = set(claim.evidence_ids) - evidence.keys()
        if missing:
            raise DomainValidationError(f"claim references missing evidence: {sorted(missing)}")
        records = [evidence[item] for item in claim.evidence_ids]
        if any(record.run_id != claim.run_id for record in records):
            raise DomainValidationError("claim references evidence from another run")
        if any(record.verification_status == VerificationStatus.INVALID for record in records):
            raise DomainValidationError("claim references invalid evidence")
        if claim.kind in {ClaimKind.VERIFIED_FACT, ClaimKind.TEST_SUPPORTED} and any(
            record.verification_status != VerificationStatus.VERIFIED for record in records
        ):
            raise DomainValidationError(
                "supported claim requires verified, non-conflicting evidence"
            )

    async def verify_run(self, run_id: str) -> tuple[str, ...]:
        """Verify all persisted claim links and return review messages."""
        evidence = {item.evidence_id: item for item in await self._store.get_evidence(run_id)}
        claims = await self._store.get_claims(run_id)
        for claim in claims:
            self.verify_claim(claim, evidence)
        return (
            f"verified {len(evidence)} evidence records",
            f"verified {len(claims)} claim mappings",
        )
