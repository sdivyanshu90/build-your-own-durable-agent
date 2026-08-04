from __future__ import annotations

import pytest

from durable_agent.domain.enums import RunState, TaskState
from durable_agent.domain.errors import InvalidTransitionError
from durable_agent.domain.state_machine import (
    RUN_TRANSITIONS,
    TASK_TRANSITIONS,
    transition_run,
    transition_task,
)


@pytest.mark.parametrize(
    ("source", "target"),
    [(source, target) for source, targets in RUN_TRANSITIONS.items() for target in targets],
)
def test_every_declared_run_transition_is_valid(source: RunState, target: RunState) -> None:
    assert transition_run(source, target) == target


@pytest.mark.parametrize(
    ("source", "target"),
    [(source, target) for source, targets in TASK_TRANSITIONS.items() for target in targets],
)
def test_every_declared_task_transition_is_valid(source: TaskState, target: TaskState) -> None:
    assert transition_task(source, target) == target


def test_self_transition_is_idempotent() -> None:
    assert transition_run(RunState.RUNNING, RunState.RUNNING) == RunState.RUNNING
    assert transition_task(TaskState.READY, TaskState.READY) == TaskState.READY


def test_terminal_states_cannot_reopen() -> None:
    for state in (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED):
        with pytest.raises(InvalidTransitionError):
            transition_run(state, RunState.RUNNING)
    for state in (
        TaskState.SUCCEEDED,
        TaskState.FAILED_TERMINAL,
        TaskState.CANCELLED,
        TaskState.SKIPPED,
    ):
        with pytest.raises(InvalidTransitionError):
            transition_task(state, TaskState.READY)


def test_undeclared_transition_has_reviewable_details() -> None:
    with pytest.raises(InvalidTransitionError) as caught:
        transition_run(RunState.CREATED, RunState.COMPLETED)
    assert caught.value.details == {
        "kind": "run",
        "from": "CREATED",
        "to": "COMPLETED",
    }
