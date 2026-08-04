from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from durable_agent.domain.checkpoint import CheckpointEnvelope, CheckpointPayload
from durable_agent.domain.enums import RunState, TaskState
from durable_agent.domain.errors import InvalidTransitionError, SecurityPolicyError
from durable_agent.domain.state_machine import (
    RUN_TRANSITIONS,
    TASK_TRANSITIONS,
    transition_run,
    transition_task,
)
from durable_agent.security.paths import resolve_within_root


@given(st.sampled_from(list(RunState)), st.sampled_from(list(RunState)))
def test_only_declared_run_transitions_are_accepted(source: RunState, target: RunState) -> None:
    if source == target or target in RUN_TRANSITIONS[source]:
        assert transition_run(source, target) == target
    else:
        with pytest.raises(InvalidTransitionError):
            transition_run(source, target)


@given(st.sampled_from(list(TaskState)), st.sampled_from(list(TaskState)))
def test_only_declared_task_transitions_are_accepted(source: TaskState, target: TaskState) -> None:
    if source == target or target in TASK_TRANSITIONS[source]:
        assert transition_task(source, target) == target
    else:
        with pytest.raises(InvalidTransitionError):
            transition_task(source, target)


@given(
    st.integers(min_value=1, max_value=1_000),
    st.text(
        alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
        min_size=1,
        max_size=50,
    ),
)
def test_checkpoint_serialization_round_trips(sequence: int, run_id: str) -> None:
    payload = CheckpointPayload(
        run_id=run_id,
        run_state=RunState.RUNNING,
        active_task_id=None,
        task_states={"task": TaskState.PENDING},
        completed_task_ids=(),
        pending_task_ids=("task",),
        plan_id="plan",
        plan_version=1,
        configuration_fingerprint="f" * 64,
    )
    envelope = CheckpointEnvelope.create(
        checkpoint_id=f"cp-{sequence}", sequence=sequence, payload=payload
    )
    assert CheckpointEnvelope.from_untrusted_json(envelope.model_dump_json()) == envelope


@given(st.lists(st.sampled_from(["..", ".", "folder", "file"]), min_size=1, max_size=8))
def test_generated_paths_never_escape_root(parts: list[str]) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        candidate = "/".join(parts)
        try:
            resolved = resolve_within_root(root, candidate, must_exist=False)
        except SecurityPolicyError:
            return
        assert resolved.is_relative_to(root.resolve())
