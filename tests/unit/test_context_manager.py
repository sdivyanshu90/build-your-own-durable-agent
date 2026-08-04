from __future__ import annotations

import pytest

from durable_agent.context import ContextBudget, ContextManager
from durable_agent.domain.base import sha256_digest
from durable_agent.domain.enums import SummaryLevel
from durable_agent.domain.errors import DomainValidationError
from durable_agent.providers.fakes import DeterministicClock, DeterministicIdentifiers


def manager(limit: int = 2_048) -> ContextManager:
    return ContextManager(
        budget=ContextBudget(
            context_limit=limit,
            reserved_output=128,
            system_instructions=128,
            user_request=128,
            compression_threshold=0.5,
        ),
        identifiers=DeterministicIdentifiers(),
        clock=DeterministicClock(),
    )


def test_small_context_is_not_compressed() -> None:
    context = manager()
    items = [context.create_item(category="history", content="short event")]
    snapshot, summary = context.build(run_id="run", items=items)
    assert summary is None
    assert snapshot.item_ids == (items[0].item_id,)


def test_compression_preserves_constraints_questions_and_evidence() -> None:
    context = manager()
    constraint = context.create_item(
        category="negative_requirement", content="Never access the network", priority=0
    )
    question = context.create_item(
        category="question", content="Is compatibility preserved?", priority=0
    )
    history = [
        context.create_item(
            category="history",
            content=f"Long tool result {index} " + ("x" * 500),
            evidence_ids=(f"EVID-{index}",),
            priority=100 + index,
        )
        for index in range(20)
    ]
    snapshot, summary = context.build(
        run_id="run", items=[constraint, question, *history], level=SummaryLevel.TASK_GROUP
    )
    assert snapshot.used_tokens <= snapshot.budget_tokens
    assert constraint.item_id in snapshot.item_ids
    assert question.item_id in snapshot.item_ids
    assert summary is not None
    assert set(summary.evidence_ids) == {
        evidence
        for item in history
        if item.item_id in snapshot.removed_item_ids
        for evidence in item.evidence_ids
    }
    assert "verify conclusions" in summary.content


def test_summary_invalidation_uses_primary_source_hashes() -> None:
    context = manager()
    items = [
        context.create_item(category="history", content="x" * 500, evidence_ids=("EVID-1",))
        for _ in range(20)
    ]
    _, summary = context.build(run_id="run", items=items)
    assert summary is not None
    current = dict(summary.source_hashes)
    assert context.invalidate_if_stale(summary, current).valid
    current[summary.source_item_ids[0]] = sha256_digest("changed")
    stale = context.invalidate_if_stale(summary, current)
    assert not stale.valid
    assert "changed or disappeared" in (stale.invalidated_reason or "")


def test_mandatory_material_cannot_be_silently_dropped() -> None:
    context = manager()
    constraints = [
        context.create_item(category="constraint", content="x" * 2_000) for _ in range(5)
    ]
    with pytest.raises(DomainValidationError, match="mandatory"):
        context.build(run_id="run", items=constraints)


def test_repeated_summary_retains_constraint_sentinel() -> None:
    context = manager()
    items = [context.create_item(category="history", content="x" * 500) for _ in range(20)]
    _, summary = context.build(run_id="run", items=items, generation=2)
    assert summary is not None
    assert summary.generation == 2
    assert summary.retained_constraints


def test_summary_hierarchy_links_the_prior_summary() -> None:
    context = manager()
    raw = [context.create_item(category="history", content="x" * 500) for _ in range(20)]
    _, first = context.build(run_id="run", items=raw)
    assert first is not None
    navigation = context.create_item(
        category="summary",
        content=first.content,
        priority=1_000,
        source_refs=(first.summary_id,),
        evidence_ids=first.evidence_ids,
    )
    new_history = [context.create_item(category="history", content="y" * 500) for _ in range(20)]
    snapshot, second = context.build(
        run_id="run",
        items=[navigation, *new_history],
        level=SummaryLevel.TASK_GROUP,
        generation=2,
    )
    assert second is not None
    assert first.summary_id in second.source_summary_ids
    assert second.summary_id in snapshot.summary_ids
