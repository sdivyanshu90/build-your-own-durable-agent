"""Lease-fenced agent loop with checkpointed safe boundaries and bounded retries."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from durable_agent.checkpoints import CheckpointManager
from durable_agent.configuration import Settings
from durable_agent.context import ContextManager
from durable_agent.domain.base import DomainModel
from durable_agent.domain.context import ContextItem, SummaryRecord
from durable_agent.domain.enums import FailurePolicy, RunState, SummaryLevel, TaskState
from durable_agent.domain.errors import DurableAgentError, ToolExecutionError
from durable_agent.domain.models import (
    LeaseRecord,
    LifecycleRequest,
    PlanSpec,
    RunRecord,
    TaskRecord,
)
from durable_agent.domain.plan import ready_tasks
from durable_agent.domain.protocols import Clock
from durable_agent.domain.state_machine import transition_run, transition_task
from durable_agent.observability import METRICS, get_logger
from durable_agent.orchestration.worker import TaskOutcome, TaskWorker
from durable_agent.persistence.store import SqlStore
from durable_agent.reporting import ReportGenerator, ReportInput
from durable_agent.reporting.models import VerificationEntry


class RunAdvanceResult(DomainModel):
    run: RunRecord
    tasks: tuple[TaskRecord, ...]
    tasks_executed: int
    checkpoint_id: str | None = None
    report_id: str | None = None


class AgentOrchestrator:
    """Advance a persisted plan without holding ambiguous state across boundaries."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: SqlStore,
        worker: TaskWorker,
        checkpoints: CheckpointManager,
        context: ContextManager,
        reports: ReportGenerator,
        clock: Clock,
    ) -> None:
        self._settings = settings
        self._store = store
        self._worker = worker
        self._checkpoints = checkpoints
        self._context = context
        self._reports = reports
        self._clock = clock
        self._log = get_logger("durable_agent.orchestrator")

    async def advance(
        self,
        run_id: str,
        *,
        worker_id: str,
        maximum_tasks: int | None = None,
        lease_already_held: bool = False,
    ) -> RunAdvanceResult:
        """Execute ready tasks until terminal, paused/cancelled, or the caller's bound."""
        if maximum_tasks is not None and maximum_tasks < 0:
            raise ValueError("maximum_tasks cannot be negative")
        lease = await self._store.acquire_lease(
            run_id, worker_id, ttl_seconds=self._settings.lease_ttl_seconds
        )
        lease_ref = [lease]
        del lease_already_held  # same-owner acquisition renews an existing recovery lease
        run = await self._store.get_run(run_id)
        if run.state not in {RunState.RUNNING, RunState.PAUSE_REQUESTED, RunState.PAUSED}:
            await self._store.release_lease(lease)
            return RunAdvanceResult(
                run=run, tasks=tuple(await self._store.get_tasks(run_id)), tasks_executed=0
            )
        if run.state == RunState.PAUSED:
            pending = await self._store.pending_lifecycle_request(run_id)
            if pending is None or pending.kind != "cancel":
                await self._store.release_lease(lease)
                return RunAdvanceResult(
                    run=run,
                    tasks=tuple(await self._store.get_tasks(run_id)),
                    tasks_executed=0,
                )
        plan = await self._store.get_plan(run_id)
        tasks = list(await self._store.get_tasks(run_id))
        latest_context = await self._store.latest_context(run_id)
        context_items: list[ContextItem] = []
        current_summary: SummaryRecord | None = None
        if latest_context is not None:
            latest_snapshot, latest_items, current_summary = latest_context
            selected_ids = set(latest_snapshot.item_ids)
            context_items = [item for item in latest_items if item.item_id in selected_ids]
            if current_summary is not None:
                context_items = self._append_summary_navigation(context_items, current_summary)
        outcomes: list[TaskOutcome] = []
        executed = 0
        checkpoint_id: str | None = None
        report_id: str | None = None
        try:
            while True:
                request = await self._store.pending_lifecycle_request(run_id)
                if request is not None:
                    if request.kind == "cancel":
                        run, tasks, checkpoint_id = await self._cancel(run, plan, tasks, request)
                        report_id = await self._generate_report(run, plan, tasks, partial=True)
                    else:
                        run, checkpoint_id = await self._pause(run, plan, tasks, request)
                    break
                if maximum_tasks is not None and executed >= maximum_tasks:
                    break

                tasks = await self._promote_ready(tasks)
                candidates = ready_tasks(tasks)
                if not candidates:
                    if await self._store.pending_lifecycle_request(run_id) is not None:
                        continue
                    run = await self._store.get_run(run_id)
                    if all(
                        task.state in {TaskState.SUCCEEDED, TaskState.SKIPPED} for task in tasks
                    ):
                        old_version = run.version
                        run = run.model_copy(
                            update={
                                "state": transition_run(run.state, RunState.COMPLETED),
                                "active_task_id": None,
                                "updated_at": self._clock.now(),
                                "finished_at": self._clock.now(),
                            }
                        )
                        run = await self._store.update_run(run, expected_version=old_version)
                        checkpoint = await self._checkpoints.write(run=run, plan=plan, tasks=tasks)
                        checkpoint_id = checkpoint.checkpoint_id
                        report_id = await self._generate_report(run, plan, tasks, partial=False)
                        await self._store.publish(
                            run_id=run_id,
                            event_type="run.completed",
                            payload={"report_id": report_id},
                        )
                        METRICS.runs_completed.inc()
                        self._log.info(
                            "run.completed",
                            event_type="run.completed",
                            run_id=run_id,
                            checkpoint_id=checkpoint_id,
                            report_id=report_id,
                        )
                    else:
                        run, checkpoint_id = await self._fail_stuck_run(run, plan, tasks)
                        report_id = await self._generate_report(run, plan, tasks, partial=True)
                    break

                batch = self._select_batch(candidates, executed, maximum_tasks)
                run, started, before_checkpoint_id = await self._start_batch(
                    run, plan, tasks, batch
                )
                if before_checkpoint_id is not None:
                    checkpoint_id = before_checkpoint_id
                results = await self._execute_batch_with_heartbeat(
                    run,
                    tuple(item[1] for item in started),
                    lease_ref,
                )
                failures: list[tuple[int, TaskRecord, str, DurableAgentError, bool]] = []
                for (task_index, task, attempt_id), (outcome, error) in zip(
                    started, results, strict=True
                ):
                    if error is not None:
                        if isinstance(error, DurableAgentError):
                            failures.append((task_index, task, attempt_id, error, False))
                        else:
                            failures.append(
                                (
                                    task_index,
                                    task,
                                    attempt_id,
                                    ToolExecutionError(f"unexpected task worker failure: {error}"),
                                    True,
                                )
                            )
                        continue
                    if outcome is None:
                        failures.append(
                            (
                                task_index,
                                task,
                                attempt_id,
                                ToolExecutionError("task worker returned no outcome"),
                                True,
                            )
                        )
                        continue

                    old_task_version = task.version
                    task = task.model_copy(
                        update={
                            "state": transition_task(task.state, TaskState.SUCCEEDED),
                            "finished_at": self._clock.now(),
                        }
                    )
                    task = await self._store.update_task_and_finish_attempt(
                        task,
                        expected_version=old_task_version,
                        attempt_id=attempt_id,
                        attempt_state=TaskState.SUCCEEDED,
                    )
                    if task.started_at is not None and task.finished_at is not None:
                        METRICS.task_duration.labels(task_kind="planned_task").observe(
                            max(0.0, (task.finished_at - task.started_at).total_seconds())
                        )
                    tasks[task_index] = task
                    current_run = await self._store.get_run(run_id)
                    running_ids = sorted(
                        item.spec.task_id for item in tasks if item.state == TaskState.RUNNING
                    )
                    run_updates: dict[str, object] = {
                        "active_task_id": running_ids[0] if len(running_ids) == 1 else None,
                        "updated_at": self._clock.now(),
                    }
                    if outcome.repository_snapshot_id is not None:
                        run_updates["repository_snapshot_id"] = outcome.repository_snapshot_id
                    run = current_run.model_copy(update=run_updates)
                    run = await self._store.update_run(run, expected_version=current_run.version)
                    outcomes.append(outcome)
                    context_items.append(
                        self._context.create_item(
                            category="task_state",
                            content=(
                                f"Task {task.spec.task_id} succeeded on attempt "
                                f"{task.attempt_count}."
                            ),
                            source_refs=(task.spec.task_id,),
                            evidence_ids=outcome.evidence_ids,
                            priority=10,
                        )
                    )
                    context_items.append(
                        self._context.create_item(
                            category="history",
                            content=outcome.context_note,
                            source_refs=(task.spec.task_id,),
                            evidence_ids=outcome.evidence_ids,
                            priority=100,
                        )
                    )
                    level, generation = self._next_summary_level(current_summary)
                    snapshot, summary = self._context.build(
                        run_id=run_id,
                        task_id=task.spec.task_id,
                        items=context_items,
                        level=level,
                        generation=generation,
                    )
                    await self._store.save_context(snapshot, items=context_items, summary=summary)
                    completed_count = sum(item.state == TaskState.SUCCEEDED for item in tasks)
                    task_forces_checkpoint = task.spec.checkpoint_policy.value in {
                        "AFTER",
                        "BEFORE_AND_AFTER",
                    }
                    frequency_checkpoint = (
                        completed_count % self._settings.checkpoint_every_tasks == 0
                    )
                    if task_forces_checkpoint or frequency_checkpoint:
                        checkpoint = await self._checkpoints.write(
                            run=run,
                            plan=plan,
                            tasks=tasks,
                            context_ids=(snapshot.context_id,),
                            summary_ids=snapshot.summary_ids,
                            artifact_ids=tuple(
                                item for value in outcomes for item in value.artifact_ids
                            ),
                            evidence_ids=tuple(
                                item for value in outcomes for item in value.evidence_ids
                            ),
                        )
                        checkpoint_id = checkpoint.checkpoint_id
                    await self._store.publish(
                        run_id=run_id,
                        task_id=task.spec.task_id,
                        event_type="task.succeeded",
                        payload={
                            "attempt": task.attempt_count,
                            "checkpoint_id": checkpoint_id,
                            "evidence_ids": outcome.evidence_ids,
                        },
                    )
                    self._log.info(
                        "task.succeeded",
                        event_type="task.succeeded",
                        run_id=run_id,
                        task_id=task.spec.task_id,
                        attempt_id=attempt_id,
                        checkpoint_id=checkpoint_id,
                    )
                    selected_ids = set(snapshot.item_ids)
                    context_items = [item for item in context_items if item.item_id in selected_ids]
                    if summary is not None:
                        current_summary = summary
                        context_items = self._append_summary_navigation(context_items, summary)
                    executed += 1

                if failures:
                    run, checkpoint_id, terminal, retry_tasks = await self._handle_batch_failures(
                        run, plan, tasks, failures
                    )
                    for task in retry_tasks:
                        await self._clock.sleep(
                            self._settings.retry.delay_for(
                                task.attempt_count,
                                jitter_key=f"{run_id}:{task.spec.task_id}",
                            )
                        )
                    if terminal:
                        report_id = await self._generate_report(run, plan, tasks, partial=True)
                        break
        finally:
            await self._store.release_lease(lease_ref[0])
        return RunAdvanceResult(
            run=run,
            tasks=tuple(tasks),
            tasks_executed=executed,
            checkpoint_id=checkpoint_id,
            report_id=report_id,
        )

    def _select_batch(
        self,
        candidates: Sequence[TaskRecord],
        executed: int,
        maximum_tasks: int | None,
    ) -> tuple[TaskRecord, ...]:
        """Select a stable, bounded prefix without bypassing a serial priority barrier."""
        limit = self._settings.maximum_concurrency
        if maximum_tasks is not None:
            limit = min(limit, maximum_tasks - executed)
        first = candidates[0]
        if limit <= 1 or not first.spec.parallelizable:
            return (first,)
        selected: list[TaskRecord] = []
        for candidate in candidates:
            if len(selected) >= limit or not candidate.spec.parallelizable:
                break
            selected.append(candidate)
        return tuple(selected)

    async def _start_batch(
        self,
        run: RunRecord,
        plan: PlanSpec,
        tasks: list[TaskRecord],
        batch: Sequence[TaskRecord],
    ) -> tuple[RunRecord, tuple[tuple[int, TaskRecord, str], ...], str | None]:
        """Persist every task attempt before allowing any batch worker to execute."""
        started: list[tuple[int, TaskRecord, str]] = []
        for candidate in batch:
            task_index = next(
                index
                for index, item in enumerate(tasks)
                if item.spec.task_id == candidate.spec.task_id
            )
            old_task_version = candidate.version
            task = candidate.model_copy(
                update={
                    "state": transition_task(candidate.state, TaskState.RUNNING),
                    "attempt_count": candidate.attempt_count + 1,
                    "started_at": self._clock.now(),
                }
            )
            task = await self._store.update_task(task, expected_version=old_task_version)
            tasks[task_index] = task
            attempt_id = await self._store.start_task_attempt(
                run_id=run.run_id,
                task_id=task.spec.task_id,
                attempt_number=task.attempt_count,
            )
            await self._store.publish(
                run_id=run.run_id,
                task_id=task.spec.task_id,
                event_type="task.started",
                payload={"attempt": task.attempt_count, "batch_size": len(batch)},
            )
            self._log.info(
                "task.started",
                event_type="task.started",
                run_id=run.run_id,
                task_id=task.spec.task_id,
                attempt_id=attempt_id,
                batch_size=len(batch),
            )
            started.append((task_index, task, attempt_id))

        current_run = await self._store.get_run(run.run_id)
        active_task_id = started[0][1].spec.task_id if len(started) == 1 else None
        run = current_run.model_copy(
            update={"active_task_id": active_task_id, "updated_at": self._clock.now()}
        )
        run = await self._store.update_run(run, expected_version=current_run.version)
        checkpoint_id: str | None = None
        if any(item[1].spec.checkpoint_policy.value == "BEFORE_AND_AFTER" for item in started):
            checkpoint = await self._checkpoints.write(run=run, plan=plan, tasks=tasks)
            checkpoint_id = checkpoint.checkpoint_id
        return run, tuple(started), checkpoint_id

    async def _execute_batch_with_heartbeat(
        self,
        run: RunRecord,
        tasks: Sequence[TaskRecord],
        lease_ref: list[LeaseRecord],
    ) -> tuple[tuple[TaskOutcome | None, Exception | None], ...]:
        """Run a batch concurrently under one renewable run lease and final fence."""
        stop = asyncio.Event()

        async def heartbeat() -> None:
            while True:
                try:
                    await asyncio.wait_for(
                        stop.wait(), timeout=float(self._settings.lease_renewal_seconds)
                    )
                    return
                except TimeoutError:
                    lease_ref[0] = await self._store.renew_lease(
                        lease_ref[0], ttl_seconds=self._settings.lease_ttl_seconds
                    )

        async def execute_one(
            task: TaskRecord,
        ) -> tuple[TaskOutcome | None, Exception | None]:
            try:
                return await self._worker.execute(run, task), None
            except Exception as exc:
                return None, exc

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            results = await asyncio.gather(*(execute_one(task) for task in tasks))
        finally:
            stop.set()
            await heartbeat_task
        lease_ref[0] = await self._store.renew_lease(
            lease_ref[0], ttl_seconds=self._settings.lease_ttl_seconds
        )
        return tuple(results)

    def _append_summary_navigation(
        self, items: list[ContextItem], summary: SummaryRecord
    ) -> list[ContextItem]:
        """Add a provenance-linked summary as navigation, never as primary evidence."""
        if any(summary.summary_id in item.source_refs for item in items):
            return items
        return [
            *items,
            self._context.create_item(
                category="summary",
                content=summary.content,
                source_refs=(summary.summary_id,),
                evidence_ids=summary.evidence_ids,
                priority=1_000,
                mandatory=summary.generation >= 3,
            ),
        ]

    @staticmethod
    def _next_summary_level(
        current: SummaryRecord | None,
    ) -> tuple[SummaryLevel, int]:
        """Progress task summaries to task-group and run summaries without generation >3."""
        if current is None or current.generation >= 3:
            return SummaryLevel.TASK, 1
        if current.generation == 1:
            return SummaryLevel.TASK_GROUP, 2
        return SummaryLevel.RUN, 3

    async def _promote_ready(self, tasks: list[TaskRecord]) -> list[TaskRecord]:
        candidates = ready_tasks(tasks)
        candidate_ids = {item.spec.task_id for item in candidates}
        result = list(tasks)
        for index, task in enumerate(result):
            if task.spec.task_id not in candidate_ids:
                continue
            old_version = task.version
            task = task.model_copy(update={"state": transition_task(task.state, TaskState.READY)})
            result[index] = await self._store.update_task(task, expected_version=old_version)
        return result

    async def _pause(
        self,
        run: RunRecord,
        plan: PlanSpec,
        tasks: Sequence[TaskRecord],
        request: LifecycleRequest,
    ) -> tuple[RunRecord, str]:
        run = await self._store.get_run(run.run_id)
        old_version = run.version
        state = run.state
        if state == RunState.RUNNING:
            state = transition_run(state, RunState.PAUSE_REQUESTED)
        state = transition_run(state, RunState.PAUSED)
        run = run.model_copy(
            update={"state": state, "active_task_id": None, "updated_at": self._clock.now()}
        )
        run = await self._store.update_run(run, expected_version=old_version)
        await self._store.apply_lifecycle_request(request)
        checkpoint = await self._checkpoints.write(run=run, plan=plan, tasks=tasks)
        await self._store.publish(
            run_id=run.run_id,
            event_type="run.paused",
            payload={"reason": request.reason, "checkpoint_id": checkpoint.checkpoint_id},
        )
        METRICS.runs_paused.inc()
        self._log.info(
            "run.paused",
            event_type="run.paused",
            run_id=run.run_id,
            checkpoint_id=checkpoint.checkpoint_id,
        )
        return run, checkpoint.checkpoint_id

    async def _cancel(
        self,
        run: RunRecord,
        plan: PlanSpec,
        tasks: Sequence[TaskRecord],
        request: LifecycleRequest,
    ) -> tuple[RunRecord, list[TaskRecord], str]:
        updated = list(tasks)
        for index, task in enumerate(updated):
            if task.state in {
                TaskState.SUCCEEDED,
                TaskState.FAILED_TERMINAL,
                TaskState.CANCELLED,
                TaskState.SKIPPED,
            }:
                continue
            old_version = task.version
            task = task.model_copy(
                update={
                    "state": transition_task(task.state, TaskState.CANCELLED),
                    "finished_at": self._clock.now(),
                }
            )
            updated[index] = await self._store.cancel_task_and_open_attempts(
                task, expected_version=old_version
            )
        run = await self._store.get_run(run.run_id)
        old_version = run.version
        run = run.model_copy(
            update={
                "state": transition_run(run.state, RunState.CANCELLED),
                "active_task_id": None,
                "updated_at": self._clock.now(),
                "finished_at": self._clock.now(),
            }
        )
        run = await self._store.update_run(run, expected_version=old_version)
        await self._store.apply_lifecycle_request(request)
        checkpoint = await self._checkpoints.write(run=run, plan=plan, tasks=updated)
        await self._store.publish(
            run_id=run.run_id,
            event_type="run.cancelled",
            payload={"reason": request.reason, "checkpoint_id": checkpoint.checkpoint_id},
        )
        return run, updated, checkpoint.checkpoint_id

    async def _handle_batch_failures(
        self,
        run: RunRecord,
        plan: PlanSpec,
        tasks: list[TaskRecord],
        failures: Sequence[tuple[int, TaskRecord, str, DurableAgentError, bool]],
    ) -> tuple[RunRecord, str, bool, tuple[TaskRecord, ...]]:
        """Commit every batch failure, then apply one deterministic run disposition."""
        retry_tasks: list[TaskRecord] = []
        skip_dependents: list[str] = []
        failure_logs: list[tuple[TaskRecord, str, str, DurableAgentError]] = []
        manual_review = False
        fail_run = False
        for task_index, task, attempt_id, error, force_terminal in failures:
            policy = task.spec.failure_policy
            error_id = await self._store.record_error(
                run_id=run.run_id,
                task_id=task.spec.task_id,
                error=error,
            )
            requires_review = policy == FailurePolicy.MANUAL_REVIEW and not force_terminal
            retry = (
                not force_terminal
                and policy == FailurePolicy.RETRY
                and self._settings.retry.should_retry(task.attempt_count, retryable=error.retryable)
                and task.attempt_count < task.spec.maximum_attempts
            )
            target = (
                TaskState.WAITING
                if requires_review
                else TaskState.FAILED_RETRYABLE
                if retry
                else TaskState.FAILED_TERMINAL
            )
            old_task_version = task.version
            task = task.model_copy(
                update={
                    "state": transition_task(task.state, target),
                    "finished_at": (
                        self._clock.now() if target == TaskState.FAILED_TERMINAL else None
                    ),
                    "last_error_id": error_id,
                }
            )
            task = await self._store.update_task_and_finish_attempt(
                task,
                expected_version=old_task_version,
                attempt_id=attempt_id,
                attempt_state=target,
                error_id=error_id,
            )
            tasks[task_index] = task
            if requires_review:
                manual_review = True
                event_type = "manual_review.required"
            else:
                event_type = "task.failed_retryable" if retry else "task.failed_terminal"
            await self._store.publish(
                run_id=run.run_id,
                task_id=task.spec.task_id,
                event_type=event_type,
                payload={
                    "attempt": task.attempt_count,
                    "error_id": error_id,
                    "category": error.category.value,
                    "message": error.message,
                    "retryable": retry,
                },
            )
            continue_independent = (
                policy == FailurePolicy.SKIP_DEPENDENTS and not force_terminal and not retry
            )
            if continue_independent:
                skip_dependents.append(task.spec.task_id)
            elif not retry and not requires_review:
                fail_run = True
            if retry:
                retry_tasks.append(task)
                METRICS.task_retries.labels(category=error.category.value).inc()
            if task.started_at is not None and task.finished_at is not None:
                METRICS.task_duration.labels(task_kind="planned_task").observe(
                    max(0.0, (task.finished_at - task.started_at).total_seconds())
                )
            failure_logs.append((task, attempt_id, error_id, error))

        for task_id in skip_dependents:
            await self._skip_transitive_dependents(tasks, failed_task_id=task_id)

        current_run = await self._store.get_run(run.run_id)
        run_updates: dict[str, object] = {
            "active_task_id": None,
            "updated_at": self._clock.now(),
        }
        if fail_run:
            run_updates.update(
                {
                    "state": transition_run(current_run.state, RunState.FAILED),
                    "finished_at": self._clock.now(),
                }
            )
        elif manual_review:
            paused_from = current_run.state
            if paused_from == RunState.RUNNING:
                paused_from = transition_run(paused_from, RunState.PAUSE_REQUESTED)
            run_updates["state"] = transition_run(paused_from, RunState.PAUSED)
        run = current_run.model_copy(update=run_updates)
        run = await self._store.update_run(run, expected_version=current_run.version)
        checkpoint = await self._checkpoints.write(run=run, plan=plan, tasks=tasks)
        if fail_run:
            METRICS.runs_failed.inc()
        elif manual_review:
            METRICS.runs_paused.inc()
        for task, attempt_id, error_id, error in failure_logs:
            self._log.warning(
                "task.failed",
                event_type=(
                    "manual_review.required"
                    if task.state == TaskState.WAITING
                    else "task.failed_retryable"
                    if task.state == TaskState.FAILED_RETRYABLE
                    else "task.failed_terminal"
                ),
                run_id=run.run_id,
                task_id=task.spec.task_id,
                attempt_id=attempt_id,
                checkpoint_id=checkpoint.checkpoint_id,
                error_id=error_id,
                error_category=error.category.value,
            )
        return (
            run,
            checkpoint.checkpoint_id,
            fail_run or manual_review,
            tuple(retry_tasks),
        )

    async def _skip_transitive_dependents(
        self, tasks: list[TaskRecord], *, failed_task_id: str
    ) -> None:
        """Skip every pending descendant while leaving independent work schedulable."""
        blocked = {failed_task_id}
        changed = True
        while changed:
            changed = False
            for task in tasks:
                if task.spec.task_id in blocked:
                    continue
                if blocked.intersection(task.spec.dependencies):
                    blocked.add(task.spec.task_id)
                    changed = True
        skipped: list[str] = []
        for index, task in enumerate(tasks):
            if task.spec.task_id not in blocked - {failed_task_id}:
                continue
            if task.state not in {TaskState.PENDING, TaskState.READY, TaskState.WAITING}:
                continue
            old_version = task.version
            task = task.model_copy(
                update={
                    "state": transition_task(task.state, TaskState.SKIPPED),
                    "finished_at": self._clock.now(),
                }
            )
            tasks[index] = await self._store.update_task(task, expected_version=old_version)
            skipped.append(task.spec.task_id)
        await self._store.publish(
            run_id=tasks[0].run_id,
            task_id=failed_task_id,
            event_type="task.dependents_skipped",
            payload={"skipped_task_ids": sorted(skipped)},
        )

    async def _fail_stuck_run(
        self, run: RunRecord, plan: PlanSpec, tasks: Sequence[TaskRecord]
    ) -> tuple[RunRecord, str]:
        run = await self._store.get_run(run.run_id)
        old_version = run.version
        run = run.model_copy(
            update={
                "state": transition_run(run.state, RunState.FAILED),
                "updated_at": self._clock.now(),
                "finished_at": self._clock.now(),
            }
        )
        run = await self._store.update_run(run, expected_version=old_version)
        await self._store.publish(
            run_id=run.run_id,
            event_type="run.failed",
            payload={"reason": "no ready tasks and plan is incomplete"},
        )
        checkpoint = await self._checkpoints.write(run=run, plan=plan, tasks=tasks)
        METRICS.runs_failed.inc()
        return run, checkpoint.checkpoint_id

    async def _generate_report(
        self, run: RunRecord, plan: PlanSpec, tasks: Sequence[TaskRecord], *, partial: bool
    ) -> str:
        evidence = tuple(await self._store.get_evidence(run.run_id))
        claims = tuple(await self._store.get_claims(run.run_id))
        audit_history_items: list[str] = []
        async for event in self._store.stream(run.run_id):
            entry = self._reportable_audit_entry(event)
            if entry is not None:
                audit_history_items.append(entry)
        audit_history = tuple(audit_history_items)
        test_evidence = [item for item in evidence if item.evidence_type.value == "TEST_RESULT"]
        bundle = self._reports.generate(
            ReportInput(
                run=run,
                plan=plan,
                tasks=tuple(tasks),
                evidence=evidence,
                claims=claims,
                executive_summary=(
                    "The run completed its validated plan and generated an evidence-linked report."
                    if not partial
                    else (
                        f"The run ended in {run.state.value}; durable partial results "
                        "are preserved."
                    )
                ),
                changed_artifacts=tuple(
                    sorted(
                        {
                            item.source
                            for item in evidence
                            if item.evidence_type.value in {"REPOSITORY_FILE", "ARTIFACT"}
                        }
                    )
                ),
                research_findings=tuple(
                    claim.text
                    for claim in claims
                    if claim.kind.value in {"VERIFIED_FACT", "INFERENCE"}
                ),
                verification_performed=tuple(
                    VerificationEntry(
                        command=item.source,
                        exit_code=(
                            item.metadata.get("exit_code")
                            if isinstance(item.metadata.get("exit_code"), int)
                            else None
                        ),
                        outcome=item.excerpt[:500],
                        evidence_ids=(item.evidence_id,),
                    )
                    for item in test_evidence
                ),
                test_results=tuple(
                    f"{item.excerpt} [{item.evidence_id}]" for item in test_evidence
                ),
                failures_and_recoveries=audit_history,
                remaining_risks=plan.risks,
                limitations=(
                    "Report conclusions are scoped to the recorded repository snapshot "
                    "and offline tests.",
                ),
                recommended_next_actions=(
                    "Review the changed files and deploy through the normal release process.",
                ),
                reproduction_instructions=(
                    "Run `durable-agent verify RUN_ID`.",
                    "Run the repository test command shown in Verification performed.",
                ),
                partial=partial,
            )
        )
        await self._store.save_report(
            report_id=bundle.report.report_id,
            run_id=run.run_id,
            format="markdown",
            content=bundle.markdown.encode(),
            content_hash=bundle.markdown_hash,
            partial=partial,
        )
        await self._store.save_report(
            report_id=bundle.report.report_id,
            run_id=run.run_id,
            format="json",
            content=bundle.json_text.encode(),
            content_hash=bundle.json_hash,
            partial=partial,
        )
        self._log.info(
            "report.generated",
            event_type="report.generated",
            run_id=run.run_id,
            report_id=bundle.report.report_id,
            partial=partial,
        )
        return bundle.report.report_id

    @staticmethod
    def _reportable_audit_entry(event: object) -> str | None:
        """Render bounded lifecycle/error history without copying arbitrary event data."""
        if not isinstance(event, dict):
            return None
        event_type = event.get("event_type")
        payload = event.get("payload")
        if not isinstance(event_type, str) or not isinstance(payload, dict):
            return None

        def bounded(value: object) -> str:
            return " ".join(str(value).split())[:300]

        task_id = bounded(event.get("task_id") or "run")
        if event_type in {"task.failed_retryable", "task.failed_terminal"}:
            return (
                f"{event_type}: {task_id}, attempt {bounded(payload.get('attempt'))}, "
                f"category {bounded(payload.get('category'))}: "
                f"{bounded(payload.get('message'))}"
            )
        if event_type in {"run.paused", "run.cancelled", "run.failed"}:
            return f"{event_type}: {bounded(payload.get('reason'))}"
        if event_type == "manual_review.required":
            return (
                f"manual_review.required: {task_id}, category "
                f"{bounded(payload.get('category'))}: {bounded(payload.get('message'))}"
            )
        if event_type == "recovery.completed":
            return (
                "recovery.completed: resumed from checkpoint "
                f"{bounded(payload.get('checkpoint_id'))}; repository drifted="
                f"{bounded(payload.get('repository_drifted'))}"
            )
        if event_type == "checkpoint.recovered":
            return (
                "checkpoint.recovered: selected sequence "
                f"{bounded(payload.get('selected_sequence'))} after rejecting "
                f"{bounded(payload.get('rejected_sequences'))}"
            )
        if event_type == "repository.drift_detected":
            return f"repository.drift_detected: policy {bounded(payload.get('policy'))}"
        return None
