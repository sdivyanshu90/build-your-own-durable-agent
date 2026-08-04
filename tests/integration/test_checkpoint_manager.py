from __future__ import annotations

import json
from pathlib import Path

import pytest

from durable_agent.checkpoints import CheckpointManager
from durable_agent.domain.enums import (
    EvidenceType,
    RunState,
    SideEffectClass,
    TaskState,
    ToolCallStatus,
    VerificationStatus,
)
from durable_agent.domain.errors import CorruptCheckpointError
from durable_agent.domain.evidence import EvidenceRecord
from durable_agent.domain.models import RunRecord, TaskRecord, ToolCall
from durable_agent.persistence import Database, SqlStore
from durable_agent.providers.fakes import DeterministicClock, DeterministicIdentifiers


@pytest.mark.asyncio
async def test_corrupt_newest_falls_back_and_records_event(tmp_path: Path, plan) -> None:
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    await database.create_schema_for_tests()
    clock = DeterministicClock()
    ids = DeterministicIdentifiers()
    store = SqlStore(database, identifiers=ids, clock=clock)
    run = RunRecord(
        run_id=plan.run_id,
        owner_id="owner",
        objective=plan.goal,
        state=RunState.RUNNING,
        active_plan_id=plan.plan_id,
        configuration_fingerprint="f" * 64,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    await store.create_run(run, idempotency_key="run-key", request_hash="a" * 64)
    await store.save_plan(plan)
    tasks = [TaskRecord(run_id=run.run_id, plan_id=plan.plan_id, spec=spec) for spec in plan.tasks]
    await store.save_tasks(tasks)
    manager = CheckpointManager(store=store, identifiers=ids, clock=clock, retention_count=10)
    first = await manager.write(run=run, plan=plan, tasks=tasks)
    tasks[0].state = TaskState.SUCCEEDED
    second = await manager.write(run=run, plan=plan, tasks=tasks)
    tampered = json.loads(second.model_dump_json())
    tampered["payload"]["active_task_id"] = "forged"
    await store.corrupt_checkpoint_for_test(second.checkpoint_id, json.dumps(tampered))
    views = await store.list_checkpoint_views(run.run_id)
    assert views[0]["checkpoint_id"] == second.checkpoint_id
    assert views[0]["integrity"] == "invalid"
    assert "payload" not in views[0]
    recovered = await manager.recover_latest(run.run_id)
    assert recovered.checkpoint_id == first.checkpoint_id
    events = [event async for event in store.stream(run.run_id)]
    recovery = next(event for event in events if event["event_type"] == "checkpoint.recovered")
    assert recovery["payload"]["selected_sequence"] == 1
    assert manager.inspect(recovered)["payload_hash"] == recovered.payload_hash
    continued = await manager.write(run=run, plan=plan, tasks=tasks)
    assert continued.sequence == second.sequence + 1
    assert (await manager.recover_latest(run.run_id)).checkpoint_id == continued.checkpoint_id
    await database.dispose()


@pytest.mark.asyncio
async def test_retention_keeps_configured_window(tmp_path: Path, plan) -> None:
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    await database.create_schema_for_tests()
    clock = DeterministicClock()
    ids = DeterministicIdentifiers()
    store = SqlStore(database, identifiers=ids, clock=clock)
    run = RunRecord(
        run_id=plan.run_id,
        owner_id="owner",
        objective=plan.goal,
        state=RunState.RUNNING,
        active_plan_id=plan.plan_id,
        configuration_fingerprint="f" * 64,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    await store.create_run(run, idempotency_key="key", request_hash="a" * 64)
    await store.save_plan(plan)
    tasks = [TaskRecord(run_id=run.run_id, plan_id=plan.plan_id, spec=spec) for spec in plan.tasks]
    await store.save_tasks(tasks)
    manager = CheckpointManager(store=store, identifiers=ids, clock=clock, retention_count=2)
    for _ in range(4):
        await manager.write(run=run, plan=plan, tasks=tasks)
    checkpoints = await store.list_checkpoints(run.run_id)
    assert [item.sequence for item in checkpoints] == [4, 3]
    await database.dispose()


@pytest.mark.asyncio
async def test_all_corrupt_checkpoints_fail_recovery(tmp_path: Path, plan) -> None:
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    await database.create_schema_for_tests()
    clock = DeterministicClock()
    ids = DeterministicIdentifiers()
    store = SqlStore(database, identifiers=ids, clock=clock)
    run = RunRecord(
        run_id=plan.run_id,
        owner_id="owner",
        objective=plan.goal,
        state=RunState.RUNNING,
        active_plan_id=plan.plan_id,
        configuration_fingerprint="f" * 64,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    await store.create_run(run, idempotency_key="key", request_hash="a" * 64)
    await store.save_plan(plan)
    tasks = [TaskRecord(run_id=run.run_id, plan_id=plan.plan_id, spec=spec) for spec in plan.tasks]
    await store.save_tasks(tasks)
    manager = CheckpointManager(store=store, identifiers=ids, clock=clock)
    checkpoint = await manager.write(run=run, plan=plan, tasks=tasks)
    await store.corrupt_checkpoint_for_test(checkpoint.checkpoint_id, "not-json")
    with pytest.raises(CorruptCheckpointError, match="no valid checkpoint"):
        await manager.recover_latest(run.run_id)
    events = [item async for item in store.stream(run.run_id)]
    assert any(item["event_type"] == "checkpoint.recovery_failed" for item in events)
    await database.dispose()


@pytest.mark.asyncio
async def test_checkpoint_manager_rejects_unsafe_retention_and_empty_history(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    await database.create_schema_for_tests()
    ids = DeterministicIdentifiers()
    clock = DeterministicClock()
    store = SqlStore(database, identifiers=ids, clock=clock)
    with pytest.raises(ValueError, match="at least two"):
        CheckpointManager(store=store, identifiers=ids, clock=clock, retention_count=1)
    manager = CheckpointManager(store=store, identifiers=ids, clock=clock)
    with pytest.raises(CorruptCheckpointError, match="no checkpoints"):
        await manager.recover_latest("missing-run")
    await database.dispose()


@pytest.mark.asyncio
async def test_valid_checkpoint_with_mismatched_parent_falls_back(tmp_path: Path, plan) -> None:
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    await database.create_schema_for_tests()
    clock = DeterministicClock()
    ids = DeterministicIdentifiers()
    store = SqlStore(database, identifiers=ids, clock=clock)
    run = RunRecord(
        run_id=plan.run_id,
        owner_id="owner",
        objective=plan.goal,
        state=RunState.RUNNING,
        active_plan_id=plan.plan_id,
        configuration_fingerprint="f" * 64,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    await store.create_run(run, idempotency_key="key", request_hash="a" * 64)
    await store.save_plan(plan)
    tasks = [TaskRecord(run_id=run.run_id, plan_id=plan.plan_id, spec=spec) for spec in plan.tasks]
    await store.save_tasks(tasks)
    manager = CheckpointManager(store=store, identifiers=ids, clock=clock)
    first = await manager.write(run=run, plan=plan, tasks=tasks)
    await manager.write(run=run, plan=plan, tasks=tasks)
    clock.advance(1)
    altered_first = first.model_copy(update={"created_at": clock.now()})
    altered_first.verify()
    await store.corrupt_checkpoint_for_test(first.checkpoint_id, altered_first.model_dump_json())
    recovered = await manager.recover_latest(run.run_id)
    assert recovered.checkpoint_id == first.checkpoint_id
    assert recovered.created_at == altered_first.created_at
    await database.dispose()


@pytest.mark.asyncio
async def test_checkpoint_reconstructs_complete_durable_manifest(tmp_path: Path, plan) -> None:
    database = Database(f"sqlite:///{tmp_path / 'manifest.db'}")
    await database.create_schema_for_tests()
    clock = DeterministicClock()
    ids = DeterministicIdentifiers()
    store = SqlStore(database, identifiers=ids, clock=clock)
    run = RunRecord(
        run_id=plan.run_id,
        owner_id="owner",
        objective=plan.goal,
        state=RunState.RUNNING,
        active_plan_id=plan.plan_id,
        configuration_fingerprint="f" * 64,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    await store.create_run(run, idempotency_key="key", request_hash="a" * 64)
    await store.save_plan(plan)
    tasks = [TaskRecord(run_id=run.run_id, plan_id=plan.plan_id, spec=spec) for spec in plan.tasks]
    await store.save_tasks(tasks)
    call = ToolCall(
        tool_call_id="call-1",
        run_id=run.run_id,
        task_id="inspect",
        tool_name="repository.search",
        arguments={"query": "retry"},
        arguments_hash="b" * 64,
        idempotency_key="tool-key",
        side_effect_class=SideEffectClass.NONE,
        status=ToolCallStatus.INTENT_RECORDED,
        created_at=clock.now(),
    )
    await store.record_intent(call)
    await store.record_artifact(
        artifact_id="artifact-1",
        run_id=run.run_id,
        task_id="inspect",
        uri="artifact-1",
        media_type="text/plain",
        content_hash="c" * 64,
        size_bytes=4,
    )
    evidence = EvidenceRecord(
        evidence_id="EVID-0001",
        run_id=run.run_id,
        evidence_type=EvidenceType.ARTIFACT,
        source="artifact-1",
        content_hash="c" * 64,
        related_task_id="inspect",
        reliability=1.0,
        excerpt="recorded artifact",
        verification_status=VerificationStatus.VERIFIED,
        created_at=clock.now(),
    )
    await store.add_evidence(evidence)
    manager = CheckpointManager(store=store, identifiers=ids, clock=clock)
    checkpoint = await manager.write(run=run, plan=plan, tasks=tasks)
    assert tuple(item.tool_call_id for item in checkpoint.payload.tool_calls) == ("call-1",)
    assert checkpoint.payload.artifact_ids == ("artifact-1",)
    assert checkpoint.payload.evidence_ids == ("EVID-0001",)
    await database.dispose()
