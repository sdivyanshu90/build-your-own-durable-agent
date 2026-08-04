from __future__ import annotations

import pytest

from durable_agent.domain.errors import ProviderRetryableError
from durable_agent.domain.models import PlanSpec
from durable_agent.domain.plan import parallel_batches
from durable_agent.planning import LLMAssistedPlanner, RuleBasedPlanner
from durable_agent.providers.fakes import DeterministicIdentifiers, FakeLLM


@pytest.mark.asyncio
async def test_rule_planner_has_complete_bounded_dag() -> None:
    planner = RuleBasedPlanner(DeterministicIdentifiers())
    plan = await planner.plan(run_id="run-1", objective="Add a configurable retry limit")
    assert plan.run_id == "run-1"
    assert len(plan.tasks) == 5
    assert all(task.acceptance_criteria for task in plan.tasks)
    assert all(task.required_evidence for task in plan.tasks)
    batches = parallel_batches(plan.tasks)
    assert batches[0] == ("inspect_repository",)
    assert batches[-1] == ("generate_report",)


@pytest.mark.asyncio
async def test_plan_revision_preserves_audit_link() -> None:
    planner = RuleBasedPlanner(DeterministicIdentifiers())
    first = await planner.plan(run_id="run", objective="Inspect retry behavior")
    second = await planner.revise(
        first, reason="Repository drift invalidated the source assumption"
    )
    assert second.version == 2
    assert second.previous_plan_id == first.plan_id
    assert "drift" in second.revision_reason
    assert first.version == 1


@pytest.mark.asyncio
async def test_llm_planner_validates_and_falls_back_after_bounded_invalid_output() -> None:
    fallback = RuleBasedPlanner(DeterministicIdentifiers())
    invalid = {
        "plan_id": "provider-plan",
        "run_id": "wrong-run",
        "version": 1,
        "goal": "valid goal",
        "scope": ["scope"],
        "acceptance_criteria": ["done"],
        "verification_steps": ["test"],
        "rollback_considerations": ["revert"],
        "tasks": [],
    }
    fake = FakeLLM([invalid, invalid])
    planner = LLMAssistedPlanner(fake, fallback=fallback, maximum_repairs=1)
    result = await planner.plan(run_id="run", objective="Implement retry behavior")
    assert isinstance(result, PlanSpec)
    assert result.run_id == "run"
    assert fake.calls == 2


def test_llm_planner_rejects_invalid_repair_bound() -> None:
    with pytest.raises(ValueError, match="between zero and five"):
        LLMAssistedPlanner(
            FakeLLM([]),
            fallback=RuleBasedPlanner(DeterministicIdentifiers()),
            maximum_repairs=6,
        )


@pytest.mark.asyncio
async def test_llm_planner_propagates_retryable_provider_failure() -> None:
    planner = LLMAssistedPlanner(
        FakeLLM([], fail_first=1),
        fallback=RuleBasedPlanner(DeterministicIdentifiers()),
    )
    with pytest.raises(ProviderRetryableError):
        await planner.plan(run_id="run", objective="Inspect safely")
