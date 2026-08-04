from __future__ import annotations

import pytest
from pydantic import ValidationError

from durable_agent.domain.enums import ClaimKind, EvidenceType, VerificationStatus
from durable_agent.domain.errors import DomainValidationError
from durable_agent.domain.evidence import Claim, EvidenceRecord
from durable_agent.evidence import EvidenceLedger


def test_primary_evidence_requires_integrity_hash() -> None:
    with pytest.raises(ValidationError, match="content hash"):
        EvidenceRecord(
            evidence_id="EVID-0001",
            run_id="run-1",
            evidence_type=EvidenceType.TEST_RESULT,
            source="pytest",
            excerpt="passed",
        )


def test_supported_claim_requires_unique_evidence() -> None:
    with pytest.raises(ValidationError, match="Field required"):
        Claim(
            claim_id="CLAIM-0001",
            run_id="run-1",
            text="tests passed",
            kind=ClaimKind.TEST_SUPPORTED,
        )
    with pytest.raises(ValidationError, match="unique"):
        Claim(
            claim_id="CLAIM-0001",
            run_id="run-1",
            text="tests passed",
            kind=ClaimKind.TEST_SUPPORTED,
            evidence_ids=("EVID-1", "EVID-1"),
        )


def test_verified_evidence_and_inference_are_distinct() -> None:
    record = EvidenceRecord(
        evidence_id="EVID-0001",
        run_id="run-1",
        evidence_type=EvidenceType.REPOSITORY_FILE,
        source="service.py",
        source_location="service.py:1-3",
        content_hash="a" * 64,
        excerpt="def retry(): pass",
        verification_status=VerificationStatus.VERIFIED,
    )
    inference = Claim(
        claim_id="CLAIM-0002",
        run_id="run-1",
        text="The implementation is likely maintainable",
        kind=ClaimKind.INFERENCE,
        evidence_ids=(record.evidence_id,),
    )
    assert inference.kind is ClaimKind.INFERENCE


@pytest.mark.parametrize(
    ("record_updates", "message"),
    [
        ({"run_id": "another-run"}, "another run"),
        ({"verification_status": VerificationStatus.INVALID}, "invalid evidence"),
        ({"verification_status": VerificationStatus.UNVERIFIED}, "requires verified"),
    ],
)
def test_supported_claim_rejects_incompatible_evidence(
    record_updates: dict[str, object], message: str
) -> None:
    record = EvidenceRecord(
        evidence_id="EVID-0001",
        run_id="run-1",
        evidence_type=EvidenceType.USER_FACT,
        source="user",
        excerpt="provided fact",
        verification_status=VerificationStatus.VERIFIED,
    ).model_copy(update=record_updates)
    claim = Claim(
        claim_id="CLAIM-0001",
        run_id="run-1",
        text="Supported claim",
        kind=ClaimKind.VERIFIED_FACT,
        evidence_ids=(record.evidence_id,),
    )
    with pytest.raises(DomainValidationError, match=message):
        EvidenceLedger.verify_claim(claim, {record.evidence_id: record})


def test_claim_verification_rejects_missing_evidence() -> None:
    claim = Claim(
        claim_id="CLAIM-0001",
        run_id="run-1",
        text="Missing support",
        kind=ClaimKind.TEST_SUPPORTED,
        evidence_ids=("EVID-missing",),
    )
    with pytest.raises(DomainValidationError, match="missing evidence"):
        EvidenceLedger.verify_claim(claim, {})
