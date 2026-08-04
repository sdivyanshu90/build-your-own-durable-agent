from __future__ import annotations

from pathlib import Path

import pytest

from durable_agent.checkpoints import CheckpointManager
from durable_agent.configuration import Settings
from durable_agent.domain.enums import RepositoryDriftPolicy, RunState, TaskState
from durable_agent.domain.errors import DomainValidationError, RepositoryChangedError
from durable_agent.domain.models import RunRecord, TaskRecord
from durable_agent.domain.state_machine import transition_task
from durable_agent.persistence import Database, SqlStore
from durable_agent.planning import RuleBasedPlanner
from durable_agent.providers.fakes import DeterministicClock, DeterministicIdentifiers
from durable_agent.recovery import RecoveryManager
from durable_agent.repository import LocalRepositoryIndexer
from durable_agent.tools import ToolExecutor, ToolPolicy, ToolRegistry


async def setup_paused_run(
    tmp_path: Path,
    *,
    drift_policy: RepositoryDriftPolicy = RepositoryDriftPolicy.REINDEX,
):  # type: ignore[no-untyped-def]
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "service.py").write_text("LIMIT = 3\n")
    settings = Settings(
        repository_root=repository,
        artifact_directory=tmp_path / "artifacts",
        repository_drift_policy=drift_policy,
    )
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    await database.create_schema_for_tests()
    clock = DeterministicClock()
    ids = DeterministicIdentifiers()
    store = SqlStore(database, identifiers=ids, clock=clock)
    indexer = LocalRepositoryIndexer(identifiers=ids)
    index = await indexer.index(repository)
    await store.save_repository_index(index)
    planner = RuleBasedPlanner(ids)
    plan = await planner.plan(run_id="run-1", objective="Change retry limit safely")
    run = RunRecord(
        run_id="run-1",
        owner_id="owner",
        objective=plan.goal,
        state=RunState.PAUSED,
        active_plan_id=plan.plan_id,
        repository_root=str(repository),
        repository_snapshot_id=index.snapshot.snapshot_id,
        configuration_fingerprint=settings.fingerprint(),
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    await store.create_run(run, idempotency_key="key", request_hash="a" * 64)
    await store.save_plan(plan)
    tasks = tuple(
        TaskRecord(run_id=run.run_id, plan_id=plan.plan_id, spec=spec) for spec in plan.tasks
    )
    await store.save_tasks(tasks)
    checkpoints = CheckpointManager(store=store, identifiers=ids, clock=clock)
    await checkpoints.write(
        run=run,
        plan=plan,
        tasks=tasks,
        repository_manifest_hash=index.snapshot.manifest_hash,
    )
    tools = ToolExecutor(
        registry=ToolRegistry(),
        journal=store,
        policy=ToolPolicy(),
        identifiers=ids,
        clock=clock,
    )
    recovery = RecoveryManager(
        settings=settings,
        store=store,
        checkpoints=checkpoints,
        indexer=indexer,
        tools=tools,
        clock=clock,
        planner=planner,
    )
    return repository, settings, database, store, recovery


@pytest.mark.asyncio
async def test_resume_detects_drift_reindexes_and_does_not_repeat_tasks(tmp_path: Path) -> None:
    repository, _, database, store, recovery = await setup_paused_run(tmp_path)
    (repository / "service.py").write_text("LIMIT = 4\n")
    result = await recovery.resume("run-1", worker_id="worker-new-process")
    assert result.run.state == RunState.RUNNING
    assert result.repository_drifted
    assert result.run.repository_snapshot_id != result.checkpoint.payload.repository_snapshot_id
    assert all(task.attempt_count == 0 for task in result.tasks)
    events = [item async for item in store.stream("run-1")]
    assert any(item["event_type"] == "repository.drift_detected" for item in events)
    assert any(item["event_type"] == "recovery.completed" for item in events)
    await store.release_lease(result.lease)
    await database.dispose()


@pytest.mark.asyncio
async def test_resume_rejects_incompatible_configuration(tmp_path: Path) -> None:
    repository, settings, database, store, recovery = await setup_paused_run(tmp_path)
    del recovery
    incompatible = settings.model_copy(update={"model_name": "different-model"})
    ids = DeterministicIdentifiers()
    clock = DeterministicClock()
    manager = RecoveryManager(
        settings=incompatible,
        store=store,
        checkpoints=CheckpointManager(store=store, identifiers=ids, clock=clock),
        indexer=LocalRepositoryIndexer(identifiers=ids),
        tools=ToolExecutor(
            registry=ToolRegistry(),
            journal=store,
            policy=ToolPolicy(),
            identifiers=ids,
            clock=clock,
        ),
        clock=clock,
        planner=RuleBasedPlanner(ids),
    )
    assert repository.exists()
    with pytest.raises(DomainValidationError, match="incompatible"):
        await manager.resume("run-1", worker_id="worker")
    replacement = await store.acquire_lease("run-1", "replacement", ttl_seconds=30)
    await store.release_lease(replacement)
    await database.dispose()


@pytest.mark.asyncio
async def test_resume_drift_rejection_releases_lease(tmp_path: Path) -> None:
    repository, _, database, store, recovery = await setup_paused_run(
        tmp_path, drift_policy=RepositoryDriftPolicy.FAIL
    )
    (repository / "service.py").write_text("LIMIT = 9\n")
    with pytest.raises(RepositoryChangedError, match="changed"):
        await recovery.resume("run-1", worker_id="rejected-worker")
    replacement = await store.acquire_lease("run-1", "replacement", ttl_seconds=30)
    await store.release_lease(replacement)
    await database.dispose()


@pytest.mark.asyncio
async def test_resume_replan_policy_creates_auditable_revision(tmp_path: Path) -> None:
    repository, _, database, store, recovery = await setup_paused_run(
        tmp_path, drift_policy=RepositoryDriftPolicy.REPLAN
    )
    (repository / "service.py").write_text("LIMIT = 7\n")
    result = await recovery.resume("run-1", worker_id="review-worker")
    events = [item async for item in store.stream("run-1")]
    revised = await store.get_plan("run-1")
    assert revised.version == 2
    assert revised.previous_plan_id is not None
    assert "repository drift" in revised.revision_reason
    assert result.run.active_plan_id == revised.plan_id
    assert all(task.plan_id == revised.plan_id for task in result.tasks)
    historical = await store.get_tasks("run-1", plan_id=revised.previous_plan_id)
    assert historical
    assert all(task.plan_id == revised.previous_plan_id for task in historical)
    assert any(item["event_type"] == "plan.revised" for item in events)
    await store.release_lease(result.lease)
    await database.dispose()


@pytest.mark.asyncio
async def test_resume_rejects_terminal_run_and_releases_lease(tmp_path: Path) -> None:
    _, _, database, store, recovery = await setup_paused_run(tmp_path)
    run = await store.get_run("run-1")
    run = run.model_copy(update={"state": RunState.CANCELLED})
    await store.update_run(run, expected_version=run.version)
    with pytest.raises(DomainValidationError, match="cannot be resumed"):
        await recovery.resume("run-1", worker_id="invalid-resume")
    replacement = await store.acquire_lease("run-1", "replacement", ttl_seconds=30)
    await store.release_lease(replacement)
    await database.dispose()


@pytest.mark.asyncio
async def test_resume_marks_exhausted_abandoned_attempt_terminal(tmp_path: Path) -> None:
    _, _, database, store, recovery = await setup_paused_run(tmp_path)
    task = (await store.get_tasks("run-1"))[0]
    task = task.model_copy(
        update={
            "state": TaskState.RUNNING,
            "attempt_count": task.spec.maximum_attempts,
        }
    )
    task = await store.update_task(task, expected_version=task.version)
    await store.start_task_attempt(
        run_id=task.run_id,
        task_id=task.spec.task_id,
        attempt_number=task.attempt_count,
    )
    result = await recovery.resume("run-1", worker_id="replacement")
    recovered = next(item for item in result.tasks if item.spec.task_id == task.spec.task_id)
    assert recovered.state == TaskState.FAILED_TERMINAL
    assert recovered.last_error_id is not None
    await store.release_lease(result.lease)
    await database.dispose()


@pytest.mark.asyncio
async def test_resume_closes_and_retries_abandoned_running_attempt(tmp_path: Path) -> None:
    _, _, database, store, recovery = await setup_paused_run(tmp_path)
    task = (await store.get_tasks("run-1"))[0]
    task = task.model_copy(update={"state": transition_task(task.state, TaskState.READY)})
    task = await store.update_task(task, expected_version=task.version)
    task = task.model_copy(
        update={
            "state": transition_task(task.state, TaskState.RUNNING),
            "attempt_count": 1,
        }
    )
    task = await store.update_task(task, expected_version=task.version)
    await store.start_task_attempt(run_id=task.run_id, task_id=task.spec.task_id, attempt_number=1)
    result = await recovery.resume("run-1", worker_id="replacement")
    recovered = next(item for item in result.tasks if item.spec.task_id == task.spec.task_id)
    assert recovered.state == TaskState.READY
    assert recovered.attempt_count == 1
    assert recovered.last_error_id is not None
    events = [item async for item in store.stream("run-1")]
    assert any(item["event_type"] == "recovery.abandoned_attempt" for item in events)
    await store.release_lease(result.lease)
    await database.dispose()
