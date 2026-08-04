"""Task graph validation and scheduling helpers."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from durable_agent.domain.enums import TaskState
from durable_agent.domain.errors import PlanValidationError
from durable_agent.domain.models import PlanSpec, TaskRecord, TaskSpec

_SERIAL_ONLY_PERMISSIONS = frozenset({"repository.write", "repository.patch"})


def validate_plan(plan: PlanSpec, *, maximum_depth: int = 20) -> PlanSpec:
    """Validate references, acyclicity, graph depth, and task granularity."""
    tasks = {task.task_id: task for task in plan.tasks}
    if len(tasks) != len(plan.tasks):
        raise PlanValidationError("task IDs must be unique")
    for task in plan.tasks:
        missing = set(task.dependencies) - tasks.keys()
        if missing:
            raise PlanValidationError(
                f"task {task.task_id} has missing dependencies: {sorted(missing)}"
            )
        if len(task.description.split()) < 3:
            raise PlanValidationError(f"task {task.task_id} is too small to be actionable")
        if task.estimated_context_tokens > 250_000:
            raise PlanValidationError(f"task {task.task_id} is too broad; decompose it")
        unsafe_parallel_permissions = task.tool_permissions & _SERIAL_ONLY_PERMISSIONS
        if task.parallelizable and unsafe_parallel_permissions:
            raise PlanValidationError(
                f"task {task.task_id} cannot run in parallel with mutation permissions: "
                f"{sorted(unsafe_parallel_permissions)}"
            )

    ordered = topological_order(plan.tasks)
    depth: dict[str, int] = {}
    for task_id in ordered:
        dependencies = tasks[task_id].dependencies
        depth[task_id] = 1 + max((depth[item] for item in dependencies), default=0)
    if max(depth.values(), default=0) > maximum_depth:
        raise PlanValidationError(f"plan depth exceeds maximum {maximum_depth}")
    return plan


def topological_order(tasks: Iterable[TaskSpec]) -> tuple[str, ...]:
    """Return stable topological order or raise with the cyclic nodes."""
    task_list = tuple(tasks)
    by_id = {task.task_id: task for task in task_list}
    indegree = {task.task_id: 0 for task in task_list}
    dependents: dict[str, list[str]] = {task.task_id: [] for task in task_list}
    for task in task_list:
        for dependency in task.dependencies:
            if dependency not in by_id:
                raise PlanValidationError(f"unknown dependency {dependency}")
            indegree[task.task_id] += 1
            dependents[dependency].append(task.task_id)

    queue = deque(sorted(task_id for task_id, count in indegree.items() if count == 0))
    result: list[str] = []
    while queue:
        task_id = queue.popleft()
        result.append(task_id)
        for dependent in sorted(dependents[task_id]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                queue.append(dependent)
    if len(result) != len(task_list):
        cyclic = sorted(task_id for task_id, count in indegree.items() if count > 0)
        raise PlanValidationError(f"task graph contains a cycle involving: {cyclic}")
    return tuple(result)


def ready_tasks(records: Iterable[TaskRecord]) -> tuple[TaskRecord, ...]:
    """Select pending/retryable nodes whose dependencies have succeeded."""
    task_records = tuple(records)
    by_id = {record.spec.task_id: record for record in task_records}
    ready = []
    for record in task_records:
        if record.state not in {
            TaskState.PENDING,
            TaskState.READY,
            TaskState.FAILED_RETRYABLE,
        }:
            continue
        if all(by_id[item].state == TaskState.SUCCEEDED for item in record.spec.dependencies):
            ready.append(record)
    return tuple(sorted(ready, key=lambda item: (item.spec.priority, item.spec.task_id)))


def parallel_batches(tasks: Iterable[TaskSpec]) -> tuple[tuple[str, ...], ...]:
    """Group graph nodes into dependency levels that may be scheduled together."""
    task_list = tuple(tasks)
    by_id = {task.task_id: task for task in task_list}
    ordered = topological_order(task_list)
    level: dict[str, int] = {}
    for task_id in ordered:
        level[task_id] = 1 + max((level[d] for d in by_id[task_id].dependencies), default=-1)
    return tuple(
        tuple(task_id for task_id in ordered if level[task_id] == current)
        for current in range(max(level.values(), default=-1) + 1)
    )
