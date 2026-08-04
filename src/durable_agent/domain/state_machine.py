"""Explicit lifecycle state machines."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeVar

from durable_agent.domain.enums import RunState, TaskState
from durable_agent.domain.errors import InvalidTransitionError

StateT = TypeVar("StateT", RunState, TaskState)


RUN_TRANSITIONS: Mapping[RunState, frozenset[RunState]] = {
    RunState.CREATED: frozenset({RunState.PLANNING, RunState.CANCELLED, RunState.FAILED}),
    RunState.PLANNING: frozenset(
        {RunState.RUNNING, RunState.PAUSE_REQUESTED, RunState.CANCELLED, RunState.FAILED}
    ),
    RunState.RUNNING: frozenset(
        {
            RunState.PAUSE_REQUESTED,
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.RECOVERING,
        }
    ),
    RunState.PAUSE_REQUESTED: frozenset(
        {RunState.PAUSED, RunState.CANCELLED, RunState.FAILED, RunState.COMPLETED}
    ),
    RunState.PAUSED: frozenset({RunState.RECOVERING, RunState.CANCELLED, RunState.FAILED}),
    RunState.RECOVERING: frozenset(
        {RunState.RUNNING, RunState.PAUSED, RunState.CANCELLED, RunState.FAILED}
    ),
    RunState.COMPLETED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
}

TASK_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset(
        {TaskState.READY, TaskState.WAITING, TaskState.CANCELLED, TaskState.SKIPPED}
    ),
    TaskState.READY: frozenset(
        {TaskState.RUNNING, TaskState.PAUSE_REQUESTED, TaskState.CANCELLED, TaskState.SKIPPED}
    ),
    TaskState.RUNNING: frozenset(
        {
            TaskState.SUCCEEDED,
            TaskState.FAILED_RETRYABLE,
            TaskState.FAILED_TERMINAL,
            TaskState.WAITING,
            TaskState.PAUSE_REQUESTED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.WAITING: frozenset(
        {TaskState.READY, TaskState.CANCELLED, TaskState.SKIPPED, TaskState.FAILED_TERMINAL}
    ),
    TaskState.PAUSE_REQUESTED: frozenset(
        {TaskState.PAUSED, TaskState.SUCCEEDED, TaskState.CANCELLED, TaskState.FAILED_TERMINAL}
    ),
    TaskState.PAUSED: frozenset({TaskState.READY, TaskState.CANCELLED}),
    TaskState.FAILED_RETRYABLE: frozenset(
        {TaskState.READY, TaskState.FAILED_TERMINAL, TaskState.CANCELLED}
    ),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.FAILED_TERMINAL: frozenset(),
    TaskState.CANCELLED: frozenset(),
    TaskState.SKIPPED: frozenset(),
}


def transition_run(current: RunState, target: RunState) -> RunState:
    """Validate and return a run state transition."""
    return _transition(current, target, RUN_TRANSITIONS, "run")


def transition_task(current: TaskState, target: TaskState) -> TaskState:
    """Validate and return a task state transition."""
    return _transition(current, target, TASK_TRANSITIONS, "task")


def _transition(
    current: StateT,
    target: StateT,
    table: Mapping[StateT, frozenset[StateT]],
    kind: str,
) -> StateT:
    if target == current:
        return current
    if target not in table[current]:
        raise InvalidTransitionError(
            f"Invalid {kind} transition {current.value} -> {target.value}",
            details={"kind": kind, "from": current.value, "to": target.value},
        )
    return target
