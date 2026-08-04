from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from durable_agent.domain.base import canonical_json, sha256_digest
from durable_agent.domain.checkpoint import CheckpointEnvelope, CheckpointPayload
from durable_agent.domain.enums import (
    ClaimKind,
    EvidenceType,
    RunState,
    SideEffectClass,
    TaskState,
    ToolCallStatus,
    VerificationStatus,
)
from durable_agent.domain.errors import (
    ConcurrencyConflictError,
    IdempotencyConflictError,
    PlanValidationError,
    ProviderRetryableError,
)
from durable_agent.domain.evidence import Claim, EvidenceRecord
from durable_agent.domain.models import RunRecord, TaskRecord, ToolCall, ToolResult
from durable_agent.persistence import Database, SqlStore
from durable_agent.providers.fakes import DeterministicClock, DeterministicIdentifiers
from durable_agent.repository import LocalRepositoryIndexer


@pytest.fixture
async def sql_store(tmp_path: Path):  # type: ignore[no-untyped-def]
    # The managed execution host cannot reliably shut down aiosqlite worker threads;
    # the store's async contract is exercised through its deterministic sync-SQLite facade.
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    await database.create_schema_for_tests()
    clock = DeterministicClock()
    store = SqlStore(database, identifiers=DeterministicIdentifiers(), clock=clock)
    try:
        yield store, clock
    finally:
        await database.dispose()


def make_run(clock: DeterministicClock, run_id: str = "run-1") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        owner_id="owner-1",
        objective="Implement configurable retry behavior",
        configuration_fingerprint="f" * 64,
        created_at=clock.now(),
        updated_at=clock.now(),
    )


@pytest.mark.asyncio
async def test_run_idempotency_listing_and_optimistic_update(sql_store) -> None:  # type: ignore[no-untyped-def]
    store, clock = sql_store
    run = make_run(clock)
    request_hash = sha256_digest("request")
    assert await store.create_run(run, idempotency_key="key", request_hash=request_hash) == run
    assert await store.create_run(run, idempotency_key="key", request_hash=request_hash) == run
    with pytest.raises(IdempotencyConflictError):
        await store.create_run(run, idempotency_key="key", request_hash=sha256_digest("other"))
    assert [item.run_id for item in await store.list_runs(owner_id="owner-1")] == ["run-1"]
    updated = run.model_copy(update={"state": RunState.PLANNING, "updated_at": clock.now()})
    stored = await store.update_run(updated, expected_version=1)
    assert stored.version == 2
    assert (await store.get_run(run.run_id)).state == RunState.PLANNING
    with pytest.raises(ConcurrencyConflictError):
        await store.update_run(updated, expected_version=1)


@pytest.mark.asyncio
async def test_plan_tasks_and_checkpoint_conflicts(sql_store, plan) -> None:  # type: ignore[no-untyped-def]
    store, clock = sql_store
    await store.create_run(make_run(clock), idempotency_key="key", request_hash="a" * 64)
    await store.save_plan(plan)
    tasks = tuple(
        TaskRecord(run_id=plan.run_id, plan_id=plan.plan_id, spec=spec) for spec in plan.tasks
    )
    await store.save_tasks(tasks)
    assert await store.get_plan(plan.run_id) == plan
    loaded_tasks = await store.get_tasks(plan.run_id)
    assert [item.spec.task_id for item in loaded_tasks] == ["change", "inspect"]
    task = loaded_tasks[0].model_copy(update={"state": TaskState.READY})
    changed = await store.update_task(task, expected_version=1)
    assert changed.version == 2
    with pytest.raises(ConcurrencyConflictError):
        await store.update_task(task, expected_version=1)

    payload = CheckpointPayload(
        run_id=plan.run_id,
        run_state=RunState.RUNNING,
        active_task_id=None,
        task_states={item.spec.task_id: item.state for item in loaded_tasks},
        completed_task_ids=(),
        pending_task_ids=tuple(item.spec.task_id for item in loaded_tasks),
        plan_id=plan.plan_id,
        plan_version=plan.version,
        configuration_fingerprint="f" * 64,
    )
    first = CheckpointEnvelope.create(
        checkpoint_id="cp-1", sequence=1, payload=payload, created_at=clock.now()
    )
    await store.append_checkpoint(first, expected_sequence=0)
    with pytest.raises(ConcurrencyConflictError):
        await store.append_checkpoint(
            first.model_copy(update={"checkpoint_id": "duplicate"}), expected_sequence=0
        )
    second = CheckpointEnvelope.create(
        checkpoint_id="cp-2",
        sequence=2,
        payload=payload,
        parent_hash=first.chain_hash(),
        created_at=clock.now(),
    )
    await store.append_checkpoint(second, expected_sequence=1)
    assert [item.sequence for item in await store.list_checkpoints(plan.run_id)] == [2, 1]


@pytest.mark.asyncio
async def test_evidence_claim_links_enforce_run_and_existence(sql_store) -> None:  # type: ignore[no-untyped-def]
    store, clock = sql_store
    await store.create_run(make_run(clock), idempotency_key="key", request_hash="a" * 64)
    evidence = EvidenceRecord(
        evidence_id="EVID-0001",
        run_id="run-1",
        evidence_type=EvidenceType.TEST_RESULT,
        source="pytest",
        content_hash="a" * 64,
        excerpt="2 passed",
        verification_status=VerificationStatus.VERIFIED,
        created_at=clock.now(),
    )
    await store.add_evidence(evidence)
    await store.add_evidence(evidence)
    claim = Claim(
        claim_id="CLAIM-0001",
        run_id="run-1",
        text="Tests passed",
        kind=ClaimKind.TEST_SUPPORTED,
        evidence_ids=(evidence.evidence_id,),
    )
    await store.add_claim(claim)
    assert await store.get_evidence("run-1") == (evidence,)
    assert await store.get_claims("run-1") == (claim,)
    forged = claim.model_copy(update={"claim_id": "CLAIM-0002", "evidence_ids": ("EVID-missing",)})
    with pytest.raises(PlanValidationError, match="missing"):
        await store.add_claim(forged)


@pytest.mark.asyncio
async def test_tool_journal_round_trip_and_uncertain(sql_store) -> None:  # type: ignore[no-untyped-def]
    store, clock = sql_store
    await store.create_run(make_run(clock), idempotency_key="key", request_hash="a" * 64)
    call = ToolCall(
        tool_call_id="call-1",
        run_id="run-1",
        task_id="task-1",
        tool_name="repository.read",
        arguments={"path": "README.md"},
        arguments_hash=sha256_digest(canonical_json({"path": "README.md"})),
        idempotency_key="tool-key",
        side_effect_class=SideEffectClass.NONE,
        created_at=clock.now(),
    )
    await store.record_intent(call)
    assert await store.uncertain_calls("run-1") == (call,)
    await store.update_status(call.tool_call_id, ToolCallStatus.FAILED)
    await store.prepare_retry(call.tool_call_id)
    reopened = await store.get_by_idempotency_key("tool-key")
    assert reopened is not None
    assert reopened[0].attempt == 2
    assert reopened[0].status == ToolCallStatus.INTENT_RECORDED
    result = ToolResult(
        tool_result_id="result-1",
        tool_call_id=call.tool_call_id,
        success=True,
        output={"content": "read"},
        output_hash=sha256_digest(canonical_json({"content": "read"})),
        duration_seconds=0,
        created_at=clock.now(),
    )
    await store.record_result(result)
    loaded = await store.get_by_idempotency_key("tool-key")
    assert loaded is not None
    assert loaded[1] == result
    assert await store.uncertain_calls("run-1") == ()


@pytest.mark.asyncio
async def test_repository_index_round_trip(sql_store, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    store, _ = sql_store
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "module.py").write_text("import os\n\ndef work():\n    return 1\n")
    index = await LocalRepositoryIndexer(identifiers=DeterministicIdentifiers()).index(repository)
    await store.save_repository_index(index)
    loaded = await store.get_repository_index(index.snapshot.snapshot_id)
    assert loaded == index


@pytest.mark.asyncio
async def test_lease_expiry_fencing_and_release(sql_store) -> None:  # type: ignore[no-untyped-def]
    store, clock = sql_store
    await store.create_run(make_run(clock), idempotency_key="key", request_hash="a" * 64)
    first = await store.acquire_lease("run-1", "worker-a", ttl_seconds=10)
    with pytest.raises(ConcurrencyConflictError, match="leased"):
        await store.acquire_lease("run-1", "worker-b", ttl_seconds=10)
    renewed = await store.acquire_lease("run-1", "worker-a", ttl_seconds=10)
    assert renewed.fencing_token == first.fencing_token
    with pytest.raises(ConcurrencyConflictError, match="ownership"):
        await store.release_lease(first)
    renewed_again = await store.renew_lease(renewed, ttl_seconds=10)
    assert renewed_again.version == renewed.version + 1
    clock.advance(11)
    stolen = await store.acquire_lease("run-1", "worker-b", ttl_seconds=10)
    assert stolen.fencing_token == first.fencing_token + 1
    with pytest.raises(ConcurrencyConflictError, match="ownership"):
        await store.renew_lease(renewed_again, ttl_seconds=10)
    await store.release_lease(stolen)


@pytest.mark.asyncio
async def test_terminal_event_retention_keeps_a_digest_tombstone(sql_store) -> None:  # type: ignore[no-untyped-def]
    store, clock = sql_store
    run = make_run(clock)
    await store.create_run(run, idempotency_key="key", request_hash="a" * 64)
    for event_type in (
        "run.created",
        "task.started",
        "task.succeeded",
        "task.failed_terminal",
        "run.cancelled",
    ):
        await store.publish(run_id=run.run_id, event_type=event_type, payload={"type": event_type})
    terminal = run.model_copy(
        update={
            "state": RunState.CANCELLED,
            "updated_at": clock.now(),
            "finished_at": clock.now(),
        }
    )
    await store.update_run(terminal, expected_version=run.version)
    clock.advance(366 * 24 * 60 * 60)
    cutoff = clock.now() - timedelta(days=365)

    preview_count, preview_runs = await store.compact_terminal_events(
        older_than=cutoff,
        dry_run=True,
    )
    assert preview_count == 2
    assert preview_runs == (run.run_id,)
    assert len([event async for event in store.stream(run.run_id)]) == 5

    deleted_count, compacted_runs = await store.compact_terminal_events(
        older_than=cutoff,
        dry_run=False,
    )
    assert deleted_count == preview_count
    assert compacted_runs == preview_runs
    events = [event async for event in store.stream(run.run_id)]
    assert {event["event_type"] for event in events} == {
        "run.created",
        "task.failed_terminal",
        "run.cancelled",
        "retention.events_compacted",
    }
    tombstone = next(
        event for event in events if event["event_type"] == "retention.events_compacted"
    )
    assert tombstone["payload"]["count"] == 2
    assert len(tombstone["payload"]["archive_manifest_sha256"]) == 64
    assert await store.compact_terminal_events(older_than=cutoff, dry_run=False) == (0, ())


@pytest.mark.asyncio
async def test_attempt_error_and_artifact_audit_rows_are_idempotent(sql_store, plan) -> None:  # type: ignore[no-untyped-def]
    store, clock = sql_store
    await store.create_run(make_run(clock), idempotency_key="key", request_hash="a" * 64)
    await store.save_plan(plan)
    task = TaskRecord(run_id=plan.run_id, plan_id=plan.plan_id, spec=plan.tasks[0])
    await store.save_tasks((task,))
    task = task.model_copy(update={"state": TaskState.READY})
    task = await store.update_task(task, expected_version=1)
    task = task.model_copy(update={"state": TaskState.RUNNING, "attempt_count": 1})
    task = await store.update_task(task, expected_version=2)
    attempt_id = await store.start_task_attempt(
        run_id=task.run_id, task_id=task.spec.task_id, attempt_number=1
    )
    error_id = await store.record_error(
        run_id=task.run_id,
        task_id=task.spec.task_id,
        error=ProviderRetryableError("temporary provider outage"),
    )
    failed = task.model_copy(
        update={"state": TaskState.FAILED_RETRYABLE, "last_error_id": error_id}
    )
    stored = await store.update_task_and_finish_attempt(
        failed,
        expected_version=3,
        attempt_id=attempt_id,
        attempt_state=TaskState.FAILED_RETRYABLE,
        error_id=error_id,
    )
    assert stored.last_error_id == error_id
    with pytest.raises(ConcurrencyConflictError, match="outcome or attempt"):
        await store.update_task_and_finish_attempt(
            failed,
            expected_version=3,
            attempt_id=attempt_id,
            attempt_state=TaskState.FAILED_RETRYABLE,
        )
    await store.record_artifact(
        artifact_id="artifact",
        run_id=task.run_id,
        task_id=task.spec.task_id,
        uri="artifact://artifact",
        media_type="text/plain",
        content_hash="b" * 64,
        size_bytes=4,
    )
    await store.record_artifact(
        artifact_id="artifact",
        run_id=task.run_id,
        task_id=task.spec.task_id,
        uri="artifact://artifact",
        media_type="text/plain",
        content_hash="b" * 64,
        size_bytes=4,
    )
    with pytest.raises(IdempotencyConflictError, match="different metadata"):
        await store.record_artifact(
            artifact_id="artifact",
            run_id=task.run_id,
            task_id=task.spec.task_id,
            uri="artifact://artifact",
            media_type="text/plain",
            content_hash="c" * 64,
            size_bytes=4,
        )
