from __future__ import annotations

import time
from pathlib import Path

import pytest

from durable_agent.checkpoints import CheckpointManager
from durable_agent.domain.enums import EvidenceType, RunState, VerificationStatus
from durable_agent.domain.models import PlanSpec, RunRecord, TaskRecord
from durable_agent.evidence import EvidenceLedger
from durable_agent.persistence import Database, SqlStore
from durable_agent.providers.fakes import DeterministicClock, DeterministicIdentifiers
from durable_agent.reporting import ReportGenerator, ReportInput
from durable_agent.repository import LocalRepositoryIndexer


@pytest.mark.performance
@pytest.mark.asyncio
async def test_index_and_incremental_index_reference_measurement(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    for number in range(200):
        (root / f"module_{number}.py").write_text(
            f"def value_{number}() -> int:\n    return {number}\n"
        )
    indexer = LocalRepositoryIndexer(identifiers=DeterministicIdentifiers())
    started = time.perf_counter()
    first = await indexer.index(root)
    full_seconds = time.perf_counter() - started
    (root / "module_50.py").write_text("def value_50() -> int:\n    return 500\n")
    started = time.perf_counter()
    second = await indexer.index(root, previous=first)
    incremental_seconds = time.perf_counter() - started
    assert first.snapshot.file_count == 200
    assert second.snapshot.file_count == 200
    assert full_seconds < 10
    assert incremental_seconds < 10


@pytest.mark.performance
@pytest.mark.asyncio
async def test_durable_storage_and_report_reference_measurements(
    tmp_path: Path, plan: PlanSpec
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'benchmark.db'}")
    await database.create_schema_for_tests()
    identifiers = DeterministicIdentifiers()
    clock = DeterministicClock()
    store = SqlStore(database, identifiers=identifiers, clock=clock)
    run = RunRecord(
        run_id=plan.run_id,
        owner_id="benchmark",
        objective=plan.goal,
        state=RunState.RUNNING,
        active_plan_id=plan.plan_id,
        configuration_fingerprint="f" * 64,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    await store.create_run(run, idempotency_key="benchmark", request_hash="b" * 64)
    await store.save_plan(plan)
    tasks = tuple(
        TaskRecord(run_id=run.run_id, plan_id=plan.plan_id, spec=spec) for spec in plan.tasks
    )
    await store.save_tasks(tasks)

    checkpoint_manager = CheckpointManager(store=store, identifiers=identifiers, clock=clock)
    started = time.perf_counter()
    await checkpoint_manager.write(run=run, plan=plan, tasks=tasks)
    await checkpoint_manager.recover_latest(run.run_id)
    checkpoint_seconds = time.perf_counter() - started

    started = time.perf_counter()
    for number in range(100):
        await store.publish(
            run_id=run.run_id,
            event_type="benchmark.event",
            payload={"sequence": number},
        )
    events = [item async for item in store.stream(run.run_id)]
    event_seconds = time.perf_counter() - started

    ledger = EvidenceLedger(store=store, identifiers=identifiers, clock=clock)
    started = time.perf_counter()
    for number in range(100):
        await ledger.record(
            run_id=run.run_id,
            evidence_type=EvidenceType.USER_FACT,
            source=f"fixture-{number}",
            excerpt=f"fact {number}",
            status=VerificationStatus.VERIFIED,
        )
    evidence = tuple(await store.get_evidence(run.run_id))
    evidence_seconds = time.perf_counter() - started

    started = time.perf_counter()
    bundle = ReportGenerator(identifiers=identifiers, clock=clock).generate(
        ReportInput(
            run=run,
            plan=plan,
            tasks=tasks,
            evidence=evidence,
            claims=(),
            executive_summary="Local reference benchmark.",
            reproduction_instructions=("Run the performance marker.",),
        )
    )
    report_seconds = time.perf_counter() - started

    assert len(events) >= 101
    assert len(evidence) == 100
    assert bundle.markdown
    assert checkpoint_seconds < 10
    assert event_seconds < 10
    assert evidence_seconds < 10
    assert report_seconds < 10
    await database.dispose()
