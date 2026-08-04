from __future__ import annotations

from pathlib import Path

import pytest

from durable_agent.application import build_application
from durable_agent.configuration import Settings
from durable_agent.domain.enums import FailurePolicy, RunState, TaskState
from durable_agent.domain.errors import ToolExecutionError
from durable_agent.domain.models import PlanSpec, RunRecord, TaskRecord, TaskSpec
from durable_agent.domain.plan import validate_plan
from durable_agent.orchestration import TaskOutcome


class PolicyPlanner:
    def __init__(self, tasks: tuple[TaskSpec, ...]) -> None:
        self._tasks = tasks

    async def plan(self, *, run_id: str, objective: str) -> PlanSpec:
        return validate_plan(
            PlanSpec(
                plan_id=f"plan-{run_id}",
                run_id=run_id,
                version=1,
                goal=objective,
                scope=("failure policy contract",),
                acceptance_criteria=("declared failure policy is enforced",),
                verification_steps=("inspect durable task states",),
                rollback_considerations=("resume only after operator review",),
                tasks=self._tasks,
            )
        )

    async def revise(
        self,
        plan: PlanSpec,
        *,
        reason: str,
        tasks: tuple[TaskSpec, ...] | None = None,
    ) -> PlanSpec:
        return validate_plan(
            plan.model_copy(
                update={
                    "plan_id": f"{plan.plan_id}-revision-{plan.version + 1}",
                    "version": plan.version + 1,
                    "tasks": tasks or plan.tasks,
                    "previous_plan_id": plan.plan_id,
                    "revision_reason": reason,
                }
            )
        )


class PolicyWorker:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.calls: list[str] = []
        self._fail_once = fail_once

    async def execute(self, run: RunRecord, task: TaskRecord) -> TaskOutcome:
        del run
        self.calls.append(task.spec.task_id)
        if task.spec.task_id == "fail" and (not self._fail_once or self.calls.count("fail") == 1):
            raise ToolExecutionError("injected policy failure")
        return TaskOutcome(context_note=f"{task.spec.task_id} completed")


def policy_task(
    task_id: str,
    *,
    dependencies: tuple[str, ...] = (),
    failure_policy: FailurePolicy = FailurePolicy.RETRY,
    priority: int = 100,
) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        title=f"Execute {task_id} task",
        description=f"Execute the complete {task_id} policy test task",
        dependencies=dependencies,
        priority=priority,
        maximum_attempts=2,
        expected_outputs=(f"{task_id} result",),
        acceptance_criteria=(f"{task_id} reaches its expected state",),
        required_evidence=("durable task state",),
        failure_policy=failure_policy,
    )


def settings(tmp_path: Path) -> Settings:
    repository = tmp_path / "repository"
    repository.mkdir()
    database = tmp_path / "state.db"
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database}",
        sync_database_url=f"sqlite:///{database}",
        repository_root=repository,
        artifact_directory=tmp_path / "artifacts",
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_skip_dependents_continues_independent_work_then_fails_run(tmp_path: Path) -> None:
    tasks = (
        policy_task("fail", failure_policy=FailurePolicy.SKIP_DEPENDENTS, priority=1),
        policy_task("dependent", dependencies=("fail",)),
        policy_task("independent", priority=2),
    )
    worker = PolicyWorker()
    application = await build_application(
        settings(tmp_path),
        create_schema_for_tests=True,
        planner=PolicyPlanner(tasks),
        worker=worker,
    )
    try:
        run = await application.service.create_run(
            objective="exercise skip dependent policy", idempotency_key="create"
        )
        result = await application.service.advance(run.run_id)
        states = {task.spec.task_id: task.state for task in result.tasks}
        assert result.run.state == RunState.FAILED
        assert states == {
            "dependent": TaskState.SKIPPED,
            "fail": TaskState.FAILED_TERMINAL,
            "independent": TaskState.SUCCEEDED,
        }
        assert worker.calls == ["fail", "independent"]
        events = [event async for event in application.store.stream(run.run_id)]
        skipped = next(
            event for event in events if event["event_type"] == "task.dependents_skipped"
        )
        assert skipped["payload"]["skipped_task_ids"] == ["dependent"]
    finally:
        await application.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_manual_review_pauses_and_explicit_resume_retries_waiting_task(
    tmp_path: Path,
) -> None:
    tasks = (policy_task("fail", failure_policy=FailurePolicy.MANUAL_REVIEW),)
    worker = PolicyWorker(fail_once=True)
    application = await build_application(
        settings(tmp_path),
        create_schema_for_tests=True,
        planner=PolicyPlanner(tasks),
        worker=worker,
    )
    try:
        run = await application.service.create_run(
            objective="exercise manual review policy", idempotency_key="create"
        )
        paused = await application.service.advance(run.run_id)
        assert paused.run.state == RunState.PAUSED
        assert paused.tasks[0].state == TaskState.WAITING
        checkpoint = await application.store.list_checkpoints(run.run_id)
        assert checkpoint[0].payload.error is not None
        resumed = await application.service.resume(run.run_id)
        assert resumed.run.state == RunState.COMPLETED
        assert resumed.tasks[0].state == TaskState.SUCCEEDED
        assert worker.calls == ["fail", "fail"]
    finally:
        await application.close()
