"""Restart recovery and repository/configuration compatibility semantics."""

from __future__ import annotations

from datetime import datetime, timedelta

from durable_agent.checkpoints import CheckpointManager
from durable_agent.configuration import Settings
from durable_agent.domain.base import DomainModel
from durable_agent.domain.checkpoint import CheckpointEnvelope
from durable_agent.domain.enums import RepositoryDriftPolicy, RunState, TaskState
from durable_agent.domain.errors import (
    ConcurrencyConflictError,
    DomainValidationError,
    ProviderRetryableError,
    RepositoryChangedError,
)
from durable_agent.domain.models import LeaseRecord, RunRecord, TaskRecord
from durable_agent.domain.protocols import Clock, Planner
from durable_agent.domain.state_machine import transition_run, transition_task
from durable_agent.observability import span
from durable_agent.persistence.store import SqlStore
from durable_agent.repository import LocalRepositoryIndexer
from durable_agent.tools.executor import ToolExecutor


class RecoveryResult(DomainModel):
    run: RunRecord
    tasks: tuple[TaskRecord, ...]
    checkpoint: CheckpointEnvelope
    lease: LeaseRecord
    repository_drifted: bool = False
    reconciled_tool_result_ids: tuple[str, ...] = ()


class CircuitBreaker:
    """Per-provider failure threshold with deterministic half-open recovery."""

    def __init__(
        self, *, failure_threshold: int = 5, recovery_seconds: int = 30, clock: Clock
    ) -> None:
        if failure_threshold < 1 or recovery_seconds < 1:
            raise ValueError("circuit breaker limits must be positive")
        self._threshold = failure_threshold
        self._recovery = timedelta(seconds=recovery_seconds)
        self._clock = clock
        self._failures = 0
        self._opened_at: datetime | None = None

    def allow(self) -> bool:
        if self._opened_at is None:
            return True
        return self._clock.now() >= self._opened_at + self._recovery

    def before_call(self) -> None:
        if not self.allow():
            raise ProviderRetryableError("provider circuit is open")

    def success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = self._clock.now()

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None and not self.allow()


class RecoveryManager:
    """Acquire ownership, validate checkpoint/config/repository, and reconcile calls."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: SqlStore,
        checkpoints: CheckpointManager,
        indexer: LocalRepositoryIndexer,
        tools: ToolExecutor,
        clock: Clock,
        planner: Planner,
    ) -> None:
        self._settings = settings
        self._store = store
        self._checkpoints = checkpoints
        self._indexer = indexer
        self._tools = tools
        self._clock = clock
        self._planner = planner

    async def resume(self, run_id: str, *, worker_id: str) -> RecoveryResult:
        """Resume after a pause/crash without replaying completed or uncertain effects."""
        lease = await self._store.acquire_lease(
            run_id, worker_id, ttl_seconds=self._settings.lease_ttl_seconds
        )
        try:
            run = await self._store.get_run(run_id)
            if run.state not in {RunState.PAUSED, RunState.RUNNING, RunState.RECOVERING}:
                raise DomainValidationError(f"run in state {run.state.value} cannot be resumed")
            if run.state != RunState.RECOVERING:
                previous_version = run.version
                run = run.model_copy(
                    update={
                        "state": transition_run(run.state, RunState.RECOVERING),
                        "updated_at": self._clock.now(),
                    }
                )
                run = await self._store.update_run(run, expected_version=previous_version)
            with span("recovery.checkpoint", {"run.id": run_id}):
                checkpoint = await self._checkpoints.recover_latest(run_id)
            if checkpoint.payload.configuration_fingerprint != self._settings.fingerprint():
                await self._store.publish(
                    run_id=run_id,
                    event_type="recovery.configuration_incompatible",
                    payload={
                        "checkpoint_fingerprint": checkpoint.payload.configuration_fingerprint,
                        "current_fingerprint": self._settings.fingerprint(),
                    },
                )
                raise DomainValidationError("current configuration is incompatible with checkpoint")

            with span("recovery.repository", {"run.id": run_id}):
                run, drifted = await self._validate_repository(run, checkpoint)
            if drifted and self._settings.repository_drift_policy == RepositoryDriftPolicy.REPLAN:
                run = await self._revise_plan_after_drift(run)
            with span("recovery.reconcile", {"run.id": run_id}):
                reconciled = await self._tools.reconcile_uncertain(run_id)
            tasks = list(await self._store.get_tasks(run_id))
            for index, task in enumerate(tasks):
                if task.state in {TaskState.PAUSED, TaskState.WAITING}:
                    old_version = task.version
                    task = task.model_copy(
                        update={"state": transition_task(task.state, TaskState.READY)}
                    )
                    tasks[index] = await self._store.update_task(task, expected_version=old_version)
                elif task.state == TaskState.RUNNING:
                    tasks[index] = await self._recover_abandoned_task(task)
            previous_version = run.version
            run = run.model_copy(
                update={
                    "state": transition_run(run.state, RunState.RUNNING),
                    "active_task_id": None,
                    "updated_at": self._clock.now(),
                }
            )
            run = await self._store.update_run(run, expected_version=previous_version)
            active_plan = await self._store.get_plan(run_id)
            post_recovery_checkpoint = await self._checkpoints.write(
                run=run,
                plan=active_plan,
                tasks=tasks,
            )
            await self._store.publish(
                run_id=run_id,
                event_type="recovery.completed",
                payload={
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "repository_drifted": drifted,
                    "reconciled_tool_results": [item.tool_result_id for item in reconciled],
                    "fencing_token": lease.fencing_token,
                    "post_recovery_checkpoint_id": post_recovery_checkpoint.checkpoint_id,
                },
            )
            return RecoveryResult(
                run=run,
                tasks=tuple(tasks),
                checkpoint=checkpoint,
                lease=lease,
                repository_drifted=drifted,
                reconciled_tool_result_ids=tuple(item.tool_result_id for item in reconciled),
            )
        except BaseException:
            await self._store.release_lease(lease)
            raise

    async def _revise_plan_after_drift(self, run: RunRecord) -> RunRecord:
        """Create an auditable plan revision and carry forward compatible task state."""
        previous = await self._store.get_plan(run.run_id)
        prior_tasks = {task.spec.task_id: task for task in await self._store.get_tasks(run.run_id)}
        revised = await self._planner.revise(
            previous,
            reason="repository drift invalidated indexed assumptions during recovery",
        )
        await self._store.save_plan(revised)
        revised_tasks: list[TaskRecord] = []
        for spec in revised.tasks:
            prior = prior_tasks.get(spec.task_id)
            if prior is not None and prior.spec == spec:
                revised_tasks.append(
                    prior.model_copy(update={"plan_id": revised.plan_id, "version": 1})
                )
            else:
                revised_tasks.append(
                    TaskRecord(run_id=run.run_id, plan_id=revised.plan_id, spec=spec)
                )
        await self._store.save_tasks(revised_tasks)
        previous_version = run.version
        run = run.model_copy(
            update={
                "active_plan_id": revised.plan_id,
                "updated_at": self._clock.now(),
            }
        )
        run = await self._store.update_run(run, expected_version=previous_version)
        await self._store.publish(
            run_id=run.run_id,
            event_type="plan.revised",
            payload={
                "plan_id": revised.plan_id,
                "previous_plan_id": previous.plan_id,
                "version": revised.version,
                "reason": revised.revision_reason,
            },
        )
        return run

    async def _recover_abandoned_task(self, task: TaskRecord) -> TaskRecord:
        """Close a dead worker's attempt and make retry disposition explicit."""
        error = ConcurrencyConflictError("worker ownership ended before the task attempt completed")
        error_id = await self._store.record_error(
            run_id=task.run_id,
            task_id=task.spec.task_id,
            error=error,
        )
        retryable = task.attempt_count < task.spec.maximum_attempts
        target = TaskState.FAILED_RETRYABLE if retryable else TaskState.FAILED_TERMINAL
        await self._store.finish_open_task_attempts(
            run_id=task.run_id,
            task_id=task.spec.task_id,
            state=target,
            error_id=error_id,
        )
        old_version = task.version
        task = task.model_copy(
            update={
                "state": transition_task(task.state, target),
                "last_error_id": error_id,
                "finished_at": None if retryable else self._clock.now(),
            }
        )
        task = await self._store.update_task(task, expected_version=old_version)
        if retryable:
            old_version = task.version
            task = task.model_copy(update={"state": transition_task(task.state, TaskState.READY)})
            task = await self._store.update_task(task, expected_version=old_version)
        await self._store.publish(
            run_id=task.run_id,
            task_id=task.spec.task_id,
            event_type="recovery.abandoned_attempt",
            payload={
                "error_id": error_id,
                "attempt": task.attempt_count,
                "retryable": retryable,
            },
        )
        return task

    async def _validate_repository(
        self, run: RunRecord, checkpoint: CheckpointEnvelope
    ) -> tuple[RunRecord, bool]:
        snapshot_id = checkpoint.payload.repository_snapshot_id
        manifest_hash = checkpoint.payload.repository_manifest_hash
        if not snapshot_id or not manifest_hash or not run.repository_root:
            return run, False
        previous = await self._store.get_repository_index(snapshot_id)
        current = await self._indexer.index(self._settings.repository_root, previous=previous)
        if current.snapshot.manifest_hash == manifest_hash:
            return run, False
        policy = self._settings.repository_drift_policy
        await self._store.publish(
            run_id=run.run_id,
            event_type="repository.drift_detected",
            payload={
                "previous_snapshot_id": snapshot_id,
                "previous_manifest_hash": manifest_hash,
                "current_manifest_hash": current.snapshot.manifest_hash,
                "policy": policy.value,
            },
        )
        if policy == RepositoryDriftPolicy.FAIL:
            raise RepositoryChangedError("repository changed since the checkpoint")
        await self._store.save_repository_index(current)
        await self._store.invalidate_summaries_for_run(
            run.run_id, reason="repository content changed during pause"
        )
        previous_version = run.version
        run = run.model_copy(
            update={
                "repository_snapshot_id": current.snapshot.snapshot_id,
                "updated_at": self._clock.now(),
            }
        )
        run = await self._store.update_run(run, expected_version=previous_version)
        return run, True
