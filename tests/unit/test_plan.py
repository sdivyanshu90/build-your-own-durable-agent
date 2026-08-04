from __future__ import annotations

import pytest
from pydantic import ValidationError

from durable_agent.domain.enums import TaskState
from durable_agent.domain.errors import PlanValidationError
from durable_agent.domain.models import PlanSpec, TaskRecord, TaskSpec
from durable_agent.domain.plan import (
    parallel_batches,
    ready_tasks,
    topological_order,
    validate_plan,
)


def test_valid_plan_and_parallel_batches(plan: PlanSpec) -> None:
    assert validate_plan(plan) is plan
    assert topological_order(plan.tasks) == ("inspect", "change")
    assert parallel_batches(plan.tasks) == (("inspect",), ("change",))


def test_missing_dependency_is_rejected(plan: PlanSpec) -> None:
    invalid = plan.model_copy(
        update={
            "tasks": (
                plan.tasks[0].model_copy(update={"dependencies": ("unknown",)}),
                plan.tasks[1],
            )
        }
    )
    with pytest.raises(PlanValidationError, match="missing dependencies"):
        validate_plan(invalid)


def test_cycle_is_rejected(plan: PlanSpec) -> None:
    cyclic = plan.model_copy(
        update={
            "tasks": (
                plan.tasks[0].model_copy(update={"dependencies": ("change",)}),
                plan.tasks[1],
            )
        }
    )
    with pytest.raises(PlanValidationError, match="cycle"):
        validate_plan(cyclic)


def test_duplicate_and_self_dependencies_are_rejected(plan: PlanSpec) -> None:
    with pytest.raises(ValidationError, match="unique"):
        TaskSpec(
            task_id="x",
            title="A valid title",
            description="A valid action description",
            dependencies=("a", "a"),
            expected_outputs=("out",),
            acceptance_criteria=("done",),
            required_evidence=("proof",),
        )
    with pytest.raises(ValidationError, match="itself"):
        TaskSpec(
            task_id="x",
            title="A valid title",
            description="A valid action description",
            dependencies=("x",),
            expected_outputs=("out",),
            acceptance_criteria=("done",),
            required_evidence=("proof",),
        )


def test_ready_tasks_respects_dependencies(plan: PlanSpec) -> None:
    inspect = TaskRecord(run_id=plan.run_id, plan_id=plan.plan_id, spec=plan.tasks[0])
    change = TaskRecord(run_id=plan.run_id, plan_id=plan.plan_id, spec=plan.tasks[1])
    assert [item.spec.task_id for item in ready_tasks((change, inspect))] == ["inspect"]
    inspect.state = TaskState.SUCCEEDED
    assert [item.spec.task_id for item in ready_tasks((change, inspect))] == ["change"]


def test_depth_limit_prevents_endless_decomposition(plan: PlanSpec) -> None:
    with pytest.raises(PlanValidationError, match="depth"):
        validate_plan(plan, maximum_depth=1)


def test_duplicate_tiny_and_overbroad_tasks_are_rejected(plan: PlanSpec) -> None:
    duplicate = plan.model_copy(update={"tasks": (plan.tasks[0], plan.tasks[0])})
    with pytest.raises(PlanValidationError, match="task IDs must be unique"):
        validate_plan(duplicate)
    tiny_task = plan.tasks[0].model_copy(update={"description": "do it"})
    with pytest.raises(PlanValidationError, match="too small"):
        validate_plan(plan.model_copy(update={"tasks": (tiny_task,)}))
    broad_task = plan.tasks[0].model_copy(update={"estimated_context_tokens": 250_001})
    with pytest.raises(PlanValidationError, match="too broad"):
        validate_plan(plan.model_copy(update={"tasks": (broad_task,)}))


def test_parallel_repository_mutation_is_rejected(plan: PlanSpec) -> None:
    unsafe = plan.tasks[0].model_copy(
        update={
            "parallelizable": True,
            "tool_permissions": frozenset({"repository.read", "repository.write"}),
        }
    )
    with pytest.raises(PlanValidationError, match="mutation permissions"):
        validate_plan(plan.model_copy(update={"tasks": (unsafe,)}))
