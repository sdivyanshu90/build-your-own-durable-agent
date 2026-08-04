from __future__ import annotations

import json
from pathlib import Path

import pytest

from durable_agent.domain.base import sha256_digest
from durable_agent.domain.enums import (
    ClaimKind,
    EvidenceType,
    RunState,
    TaskState,
    VerificationStatus,
)
from durable_agent.domain.errors import DomainValidationError
from durable_agent.domain.evidence import EvidenceConflict
from durable_agent.domain.models import RunRecord, TaskRecord
from durable_agent.evidence import EvidenceLedger
from durable_agent.persistence import Database, SqlStore
from durable_agent.providers.fakes import DeterministicClock, DeterministicIdentifiers
from durable_agent.reporting import ReportGenerator, ReportInput
from durable_agent.reporting.models import VerificationEntry


@pytest.mark.asyncio
async def test_evidence_claim_report_and_tamper_verification(tmp_path: Path, plan) -> None:
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    await database.create_schema_for_tests()
    ids = DeterministicIdentifiers()
    clock = DeterministicClock()
    store = SqlStore(database, identifiers=ids, clock=clock)
    run = RunRecord(
        run_id=plan.run_id,
        owner_id="owner",
        objective=plan.goal,
        state=RunState.COMPLETED,
        active_plan_id=plan.plan_id,
        configuration_fingerprint="f" * 64,
        created_at=clock.now(),
        updated_at=clock.now(),
        finished_at=clock.now(),
    )
    await store.create_run(run, idempotency_key="key", request_hash="a" * 64)
    ledger = EvidenceLedger(store=store, identifiers=ids, clock=clock)
    result = await ledger.record(
        run_id=run.run_id,
        evidence_type=EvidenceType.TEST_RESULT,
        source="pytest",
        source_location="tests/test_retry.py",
        excerpt="3 passed",
        content={"exit_code": 0, "passed": 3},
        reliability=0.99,
        status=VerificationStatus.VERIFIED,
    )
    claim = await ledger.claim(
        run_id=run.run_id,
        text="The retry behavior passed three automated tests.",
        kind=ClaimKind.TEST_SUPPORTED,
        evidence_ids=(result.evidence_id,),
    )
    tasks = tuple(
        TaskRecord(
            run_id=run.run_id,
            plan_id=plan.plan_id,
            spec=spec,
            state=TaskState.SUCCEEDED,
            attempt_count=1,
        )
        for spec in plan.tasks
    )
    bundle = ReportGenerator(identifiers=ids, clock=clock).generate(
        ReportInput(
            run=run,
            plan=plan,
            tasks=tasks,
            evidence=(result,),
            claims=(claim,),
            executive_summary="The requested behavior was implemented and verified.",
            changed_artifacts=("sample_service/config.py",),
            verification_performed=(
                VerificationEntry(
                    command="pytest",
                    exit_code=0,
                    outcome="passed",
                    evidence_ids=(result.evidence_id,),
                ),
            ),
            test_results=(f"Three tests passed [{result.evidence_id}].",),
            reproduction_instructions=("Run pytest.",),
        )
    )
    assert "## Claim-to-evidence mapping" in bundle.markdown
    assert f"[{result.evidence_id}]" in bundle.markdown
    assert bundle.report.claims[0].evidence_ids == (result.evidence_id,)
    assert await ledger.verify_run(run.run_id) == (
        "verified 1 evidence records",
        "verified 1 claim mappings",
    )
    with pytest.raises(DomainValidationError, match="hash mismatch"):
        ReportGenerator.verify(bundle.model_copy(update={"markdown": bundle.markdown + "tampered"}))

    parsed = json.loads(bundle.json_text)
    parsed["claims"] = []
    forged_json = json.dumps(parsed, indent=2, sort_keys=True) + "\n"
    with pytest.raises(DomainValidationError, match="does not match"):
        ReportGenerator.verify(
            bundle.model_copy(
                update={"json_text": forged_json, "json_hash": sha256_digest(forged_json)}
            )
        )
    await database.dispose()


@pytest.mark.asyncio
async def test_verified_claim_rejects_conflicting_evidence(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    await database.create_schema_for_tests()
    ids = DeterministicIdentifiers()
    clock = DeterministicClock()
    store = SqlStore(database, identifiers=ids, clock=clock)
    run = RunRecord(
        run_id="run",
        owner_id="owner",
        objective="research",
        configuration_fingerprint="f" * 64,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    await store.create_run(run, idempotency_key="key", request_hash="a" * 64)
    ledger = EvidenceLedger(store=store, identifiers=ids, clock=clock)
    evidence = await ledger.record(
        run_id="run",
        evidence_type=EvidenceType.EXTERNAL_SOURCE,
        source="fixture",
        excerpt="conflicting assertion",
        content="fact",
        status=VerificationStatus.CONFLICTING,
    )
    with pytest.raises(DomainValidationError, match="verified"):
        await ledger.claim(
            run_id="run",
            text="This is undisputed.",
            kind=ClaimKind.VERIFIED_FACT,
            evidence_ids=(evidence.evidence_id,),
        )
    await database.dispose()


@pytest.mark.asyncio
async def test_conflicting_sources_are_visible_and_cannot_be_forged(tmp_path: Path, plan) -> None:
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    await database.create_schema_for_tests()
    ids = DeterministicIdentifiers()
    clock = DeterministicClock()
    store = SqlStore(database, identifiers=ids, clock=clock)
    run = RunRecord(
        run_id=plan.run_id,
        owner_id="owner",
        objective="Resolve conflicting fixture sources",
        state=RunState.COMPLETED,
        active_plan_id=plan.plan_id,
        configuration_fingerprint="f" * 64,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    await store.create_run(run, idempotency_key="conflict", request_hash="c" * 64)
    ledger = EvidenceLedger(store=store, identifiers=ids, clock=clock)
    first = await ledger.record(
        run_id=run.run_id,
        evidence_type=EvidenceType.EXTERNAL_SOURCE,
        source="fixture-a",
        excerpt="The setting defaults to three.",
        content="default=3",
        status=VerificationStatus.CONFLICTING,
    )
    second = await ledger.record(
        run_id=run.run_id,
        evidence_type=EvidenceType.EXTERNAL_SOURCE,
        source="fixture-b",
        excerpt="The setting defaults to five.",
        content="default=5",
        status=VerificationStatus.CONFLICTING,
    )
    conflict = EvidenceConflict(
        conflict_id="conflict-1",
        run_id=run.run_id,
        description="Fixture sources disagree about the default.",
        evidence_ids=(first.evidence_id, second.evidence_id),
    )
    generator = ReportGenerator(identifiers=ids, clock=clock)
    bundle = generator.generate(
        ReportInput(
            run=run,
            plan=plan,
            tasks=(),
            evidence=(first, second),
            claims=(),
            executive_summary="The disagreement remains unresolved.",
            conflicts=(conflict,),
            limitations=("No primary source resolved the conflict.",),
            reproduction_instructions=("Inspect both offline fixtures.",),
        )
    )
    assert "Fixture sources disagree" in bundle.markdown
    assert f"[{first.evidence_id}]" in bundle.markdown
    forged = bundle.model_copy(
        update={
            "report": bundle.report.model_copy(
                update={
                    "conflicts": (
                        conflict.model_copy(
                            update={"evidence_ids": (first.evidence_id, "EVID-forged")}
                        ),
                    )
                }
            )
        }
    )
    with pytest.raises(DomainValidationError, match="does not match"):
        generator.verify(forged)
    await database.dispose()
