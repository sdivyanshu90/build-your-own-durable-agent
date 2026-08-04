"""Deterministic shared test fixtures."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from durable_agent.domain.models import PlanSpec, TaskSpec


@pytest.fixture
def task_specs() -> tuple[TaskSpec, ...]:
    return (
        TaskSpec(
            task_id="inspect",
            title="Inspect repository",
            description="Index and inspect the fixture repository",
            expected_outputs=("repository map",),
            acceptance_criteria=("repository is indexed",),
            required_evidence=("snapshot",),
            required_tools=("repository.index",),
        ),
        TaskSpec(
            task_id="change",
            title="Implement retry limit",
            description="Implement the requested configurable retry behavior",
            dependencies=("inspect",),
            expected_outputs=("source patch",),
            acceptance_criteria=("backward-compatible limit is enforced",),
            required_evidence=("changed file", "test result"),
            required_tools=("repository.write", "test.run"),
        ),
    )


@pytest.fixture
def plan(task_specs: tuple[TaskSpec, ...]) -> PlanSpec:
    return PlanSpec(
        plan_id="plan-1",
        run_id="run-1",
        version=1,
        goal="Add configurable retry behavior",
        scope=("sample service",),
        constraints=("preserve backward compatibility",),
        acceptance_criteria=("all tests pass",),
        verification_steps=("run the full test suite",),
        rollback_considerations=("revert the source patch",),
        tasks=task_specs,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
