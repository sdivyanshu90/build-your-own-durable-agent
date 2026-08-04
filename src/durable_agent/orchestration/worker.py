"""Task worker boundary and deterministic function router."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

from pydantic import Field

from durable_agent.domain.base import DomainModel
from durable_agent.domain.errors import NotFoundError
from durable_agent.domain.models import RunRecord, TaskRecord


class TaskOutcome(DomainModel):
    """Durable references produced by one successful task attempt."""

    evidence_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    test_results: tuple[str, ...] = ()
    repository_snapshot_id: str | None = None
    context_note: str = Field(default="task completed", max_length=100_000)


class TaskWorker(Protocol):
    async def execute(self, run: RunRecord, task: TaskRecord) -> TaskOutcome: ...


TaskHandler = Callable[[RunRecord, TaskRecord], Awaitable[TaskOutcome]]


class FunctionTaskWorker:
    """Route stable task IDs to injected async functions."""

    def __init__(self, handlers: Mapping[str, TaskHandler]) -> None:
        self._handlers = dict(handlers)

    async def execute(self, run: RunRecord, task: TaskRecord) -> TaskOutcome:
        try:
            handler = self._handlers[task.spec.task_id]
        except KeyError as exc:
            raise NotFoundError(f"no worker handler for task {task.spec.task_id}") from exc
        return await handler(run, task)
