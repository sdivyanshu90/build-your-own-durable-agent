from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path

import pytest

from durable_agent.application import build_application
from durable_agent.configuration import Settings
from durable_agent.domain.enums import CheckpointPolicy, RunState, TaskState
from durable_agent.domain.errors import ProviderRetryableError
from durable_agent.domain.models import PlanSpec, TaskRecord, TaskSpec
from durable_agent.orchestration import TaskOutcome


class StaticParallelPlanner:
    def __init__(
        self,
        *,
        checkpoint_policy: CheckpointPolicy = CheckpointPolicy.BEFORE_AND_AFTER,
    ) -> None:
        self._tasks = (
            TaskSpec(
                task_id="parallel_a",
                title="Inspect first source",
                description="Inspect the first independent source safely",
                expected_outputs=("first result",),
                acceptance_criteria=("first inspection completed",),
                required_evidence=("first result",),
                parallelizable=True,
                checkpoint_policy=checkpoint_policy,
            ),
            TaskSpec(
                task_id="parallel_b",
                title="Inspect second source",
                description="Inspect the second independent source safely",
                expected_outputs=("second result",),
                acceptance_criteria=("second inspection completed",),
                required_evidence=("second result",),
                parallelizable=True,
                checkpoint_policy=checkpoint_policy,
            ),
            TaskSpec(
                task_id="join_results",
                title="Combine inspection results",
                description="Combine both completed inspection results deterministically",
                dependencies=("parallel_a", "parallel_b"),
                expected_outputs=("combined result",),
                acceptance_criteria=("both results were combined",),
                required_evidence=("combined result",),
                checkpoint_policy=checkpoint_policy,
            ),
        )

    async def plan(self, *, run_id: str, objective: str) -> PlanSpec:
        return PlanSpec(
            plan_id=f"plan-{run_id}",
            run_id=run_id,
            version=1,
            goal=objective,
            scope=("parallel scheduler fixture",),
            acceptance_criteria=("all three tasks succeed",),
            verification_steps=("observe overlapping task execution",),
            rollback_considerations=("no repository mutations occur",),
            tasks=self._tasks,
        )

    async def revise(
        self,
        plan: PlanSpec,
        *,
        reason: str,
        tasks: tuple[TaskSpec, ...] | None = None,
    ) -> PlanSpec:
        return plan.model_copy(
            update={
                "plan_id": f"{plan.plan_id}-revision",
                "version": plan.version + 1,
                "previous_plan_id": plan.plan_id,
                "revision_reason": reason,
                "tasks": tasks or plan.tasks,
            }
        )


class CoordinatedWorker:
    def __init__(self, *, fail_first_a: bool = False) -> None:
        self.release = asyncio.Event()
        self.parallel_started = asyncio.Event()
        self.counts: Counter[str] = Counter()
        self.active = 0
        self.maximum_active = 0
        self._fail_first_a = fail_first_a

    async def execute(self, run: object, task: TaskRecord) -> TaskOutcome:
        del run
        task_id = task.spec.task_id
        self.counts[task_id] += 1
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        if self.active >= 2:
            self.parallel_started.set()
        try:
            if task_id in {"parallel_a", "parallel_b"}:
                await self.release.wait()
            if task_id == "parallel_a" and self._fail_first_a and self.counts[task_id] == 1:
                raise ProviderRetryableError("injected parallel retry")
            return TaskOutcome(context_note=f"{task_id} completed")
        finally:
            self.active -= 1


def parallel_settings(tmp_path: Path) -> Settings:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "README.md").write_text("parallel fixture\n")
    database = tmp_path / "parallel.db"
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database}",
        sync_database_url=f"sqlite:///{database}",
        repository_root=repository,
        artifact_directory=tmp_path / "artifacts",
        maximum_concurrency=2,
        lease_ttl_seconds=10,
        lease_renewal_seconds=2,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_independent_tasks_overlap_and_checkpoint_all_active_tasks(tmp_path: Path) -> None:
    worker = CoordinatedWorker()
    application = await build_application(
        parallel_settings(tmp_path),
        create_schema_for_tests=True,
        planner=StaticParallelPlanner(),
        worker=worker,
    )
    try:
        run = await application.service.create_run(
            objective="exercise bounded parallel scheduling",
            idempotency_key="parallel-create",
        )
        advancing = asyncio.create_task(application.service.advance(run.run_id))
        await asyncio.wait_for(worker.parallel_started.wait(), timeout=2)
        active = await application.store.get_tasks(run.run_id)
        assert {task.spec.task_id for task in active if task.state == TaskState.RUNNING} == {
            "parallel_a",
            "parallel_b",
        }
        checkpoint = await application.service.latest_checkpoint(run.run_id)
        assert checkpoint.payload.active_task_id is None
        assert {
            task_id
            for task_id, state in checkpoint.payload.task_states.items()
            if state == TaskState.RUNNING
        } == {"parallel_a", "parallel_b"}

        worker.release.set()
        result = await advancing
        assert result.run.state == RunState.COMPLETED
        assert result.tasks_executed == 3
        assert worker.maximum_active == 2
        assert worker.counts == Counter({"parallel_a": 1, "parallel_b": 1, "join_results": 1})
    finally:
        worker.release.set()
        await application.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_boundary_tasks_honor_configured_checkpoint_frequency(tmp_path: Path) -> None:
    worker = CoordinatedWorker()
    worker.release.set()
    settings = parallel_settings(tmp_path).model_copy(update={"checkpoint_every_tasks": 2})
    application = await build_application(
        settings,
        create_schema_for_tests=True,
        planner=StaticParallelPlanner(checkpoint_policy=CheckpointPolicy.RUN_BOUNDARY),
        worker=worker,
    )
    try:
        run = await application.service.create_run(
            objective="checkpoint every second completed task",
            idempotency_key="frequency-create",
        )
        completed = await application.service.advance(run.run_id)
        assert completed.run.state == RunState.COMPLETED
        checkpoints = await application.store.list_checkpoints(run.run_id)
        assert [item.sequence for item in checkpoints] == [3, 2, 1]
        assert checkpoints[1].payload.completed_task_ids == ("parallel_a", "parallel_b")
    finally:
        await application.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pause_requested_during_batch_waits_for_boundary_and_resume_does_not_repeat(
    tmp_path: Path,
) -> None:
    worker = CoordinatedWorker()
    application = await build_application(
        parallel_settings(tmp_path),
        create_schema_for_tests=True,
        planner=StaticParallelPlanner(),
        worker=worker,
    )
    try:
        run = await application.service.create_run(
            objective="pause a parallel batch safely",
            idempotency_key="pause-create",
        )
        advancing = asyncio.create_task(application.service.advance(run.run_id))
        await asyncio.wait_for(worker.parallel_started.wait(), timeout=2)
        await application.service.pause(
            run.run_id,
            reason="pause while tasks are in flight",
            idempotency_key="pause-parallel",
        )
        worker.release.set()
        paused = await advancing
        assert paused.run.state == RunState.PAUSED
        states = {task.spec.task_id: task.state for task in paused.tasks}
        assert states == {
            "parallel_a": TaskState.SUCCEEDED,
            "parallel_b": TaskState.SUCCEEDED,
            "join_results": TaskState.PENDING,
        }
        checkpoint = await application.service.latest_checkpoint(run.run_id)
        assert checkpoint.payload.run_state == RunState.PAUSED

        completed = await application.service.resume(
            run.run_id,
            idempotency_key="resume-parallel",
        )
        assert completed.run.state == RunState.COMPLETED
        assert worker.counts == Counter({"parallel_a": 1, "parallel_b": 1, "join_results": 1})
    finally:
        worker.release.set()
        await application.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_requested_during_batch_preserves_completed_outcomes(tmp_path: Path) -> None:
    worker = CoordinatedWorker()
    application = await build_application(
        parallel_settings(tmp_path),
        create_schema_for_tests=True,
        planner=StaticParallelPlanner(),
        worker=worker,
    )
    try:
        run = await application.service.create_run(
            objective="cancel a parallel batch safely",
            idempotency_key="cancel-create",
        )
        advancing = asyncio.create_task(application.service.advance(run.run_id))
        await asyncio.wait_for(worker.parallel_started.wait(), timeout=2)
        await application.service.cancel(
            run.run_id,
            reason="cancel while tasks are in flight",
            idempotency_key="cancel-parallel",
        )
        worker.release.set()
        cancelled = await advancing
        assert cancelled.run.state == RunState.CANCELLED
        assert {task.spec.task_id: task.state for task in cancelled.tasks} == {
            "parallel_a": TaskState.SUCCEEDED,
            "parallel_b": TaskState.SUCCEEDED,
            "join_results": TaskState.CANCELLED,
        }
        assert worker.counts == Counter({"parallel_a": 1, "parallel_b": 1})
        _, _, partial = await application.store.get_report(run.run_id, format="json")
        assert partial
    finally:
        worker.release.set()
        await application.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_after_worker_crash_closes_every_open_batch_attempt(tmp_path: Path) -> None:
    worker = CoordinatedWorker()
    application = await build_application(
        parallel_settings(tmp_path),
        create_schema_for_tests=True,
        planner=StaticParallelPlanner(),
        worker=worker,
    )
    try:
        run = await application.service.create_run(
            objective="cancel abandoned parallel attempts",
            idempotency_key="crash-cancel-create",
        )
        advancing = asyncio.create_task(application.service.advance(run.run_id))
        await asyncio.wait_for(worker.parallel_started.wait(), timeout=2)
        advancing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await advancing

        await application.service.cancel(
            run.run_id,
            reason="worker process terminated",
            idempotency_key="crash-cancel",
        )
        cancelled = await application.service.advance(run.run_id)
        assert cancelled.run.state == RunState.CANCELLED
        assert all(task.state == TaskState.CANCELLED for task in cancelled.tasks)
        for task_id in ("parallel_a", "parallel_b"):
            assert (
                await application.store.finish_open_task_attempts(
                    run_id=run.run_id,
                    task_id=task_id,
                    state=TaskState.CANCELLED,
                    error_id="already-closed",
                )
                == 0
            )
    finally:
        worker.release.set()
        await application.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_parallel_retry_preserves_success_and_retries_only_failed_task(
    tmp_path: Path,
) -> None:
    worker = CoordinatedWorker(fail_first_a=True)
    application = await build_application(
        parallel_settings(tmp_path),
        create_schema_for_tests=True,
        planner=StaticParallelPlanner(),
        worker=worker,
    )
    try:
        run = await application.service.create_run(
            objective="retry one parallel task",
            idempotency_key="retry-create",
        )
        advancing = asyncio.create_task(application.service.advance(run.run_id))
        await asyncio.wait_for(worker.parallel_started.wait(), timeout=2)
        worker.release.set()
        completed = await advancing
        assert completed.run.state == RunState.COMPLETED
        assert worker.counts == Counter({"parallel_a": 2, "parallel_b": 1, "join_results": 1})
        checkpoints = await application.store.list_checkpoints(run.run_id)
        assert any(
            item.payload.task_states["parallel_a"] == TaskState.FAILED_RETRYABLE
            and item.payload.task_states["parallel_b"] == TaskState.SUCCEEDED
            for item in checkpoints
        )
    finally:
        worker.release.set()
        await application.close()
