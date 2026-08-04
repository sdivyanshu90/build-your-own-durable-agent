from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from durable_agent.checkpoints import CheckpointManager
from durable_agent.domain.checkpoint import CheckpointEnvelope, CheckpointPayload
from durable_agent.domain.enums import RunState, TaskState
from durable_agent.domain.errors import CorruptCheckpointError


def payload() -> CheckpointPayload:
    return CheckpointPayload(
        run_id="run-1",
        run_state=RunState.RUNNING,
        active_task_id="change",
        task_states={"inspect": TaskState.SUCCEEDED, "change": TaskState.RUNNING},
        completed_task_ids=("inspect",),
        pending_task_ids=("change",),
        plan_id="plan-1",
        plan_version=1,
        configuration_fingerprint="f" * 64,
    )


def test_checkpoint_round_trip_and_chain() -> None:
    first = CheckpointEnvelope.create(checkpoint_id="cp-1", sequence=1, payload=payload())
    first.verify()
    loaded = CheckpointEnvelope.from_untrusted_json(first.model_dump_json())
    assert loaded == first
    second = CheckpointEnvelope.create(
        checkpoint_id="cp-2",
        sequence=2,
        payload=payload(),
        parent_hash=first.chain_hash(),
    )
    assert second.parent_hash == first.chain_hash()


def test_checkpoint_tampering_is_detected() -> None:
    checkpoint = CheckpointEnvelope.create(checkpoint_id="cp-1", sequence=1, payload=payload())
    data = json.loads(checkpoint.model_dump_json())
    data["payload"]["run_state"] = "PAUSED"
    with pytest.raises(CorruptCheckpointError, match="hash mismatch"):
        CheckpointEnvelope.from_untrusted_json(data)


def test_checkpoint_task_sets_are_consistent() -> None:
    with pytest.raises(ValidationError, match="disjoint"):
        CheckpointPayload(
            run_id="run-1",
            run_state=RunState.RUNNING,
            active_task_id=None,
            task_states={"x": TaskState.SUCCEEDED},
            completed_task_ids=("x",),
            pending_task_ids=("x",),
            plan_id="plan-1",
            plan_version=1,
            configuration_fingerprint="f" * 64,
        )


def test_invalid_json_is_rejected_without_deserialization() -> None:
    with pytest.raises(ValidationError):
        CheckpointEnvelope.from_untrusted_json('{"__reduce__": "evil"}')


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"completed_task_ids": ("missing",), "pending_task_ids": ()}, "must exist"),
        ({"completed_task_ids": (), "pending_task_ids": ("change",)}, "exactly match"),
        ({"retry_counters": {"change": -1}}, "cannot be negative"),
    ],
)
def test_checkpoint_payload_rejects_inconsistent_manifests(
    updates: dict[str, object], message: str
) -> None:
    data = payload().model_dump()
    data.update(updates)
    with pytest.raises(ValidationError, match=message):
        CheckpointPayload.model_validate(data)


def test_checkpoint_envelope_rejects_mismatched_run_identity() -> None:
    checkpoint = CheckpointEnvelope.create(
        checkpoint_id="checkpoint", sequence=1, payload=payload()
    )
    data = checkpoint.model_dump()
    data["run_id"] = "other-run"
    with pytest.raises(ValidationError, match="run IDs differ"):
        CheckpointEnvelope.model_validate(data)


def test_retained_chain_validation_skips_only_corrupt_rows_with_matching_hashes() -> None:
    first = CheckpointEnvelope.create(checkpoint_id="cp-1", sequence=1, payload=payload())
    second = CheckpointEnvelope.create(
        checkpoint_id="cp-2",
        sequence=2,
        payload=payload(),
        parent_hash=first.chain_hash(),
    )
    assert CheckpointManager._chain_is_valid((second, None, first), 0)
    assert not CheckpointManager._chain_is_valid((None,), 0)

    corrupt = second.model_copy(update={"payload_hash": "0" * 64})
    assert not CheckpointManager._chain_is_valid((corrupt, first), 0)
    parentless_second = second.model_copy(update={"parent_hash": None})
    assert not CheckpointManager._chain_is_valid((parentless_second, first), 0)
    orphan = second.model_copy(update={"parent_hash": "1" * 64})
    assert not CheckpointManager._chain_is_valid((orphan, None, corrupt, first), 0)

    same_sequence_parent = CheckpointEnvelope.create(
        checkpoint_id="cp-other",
        sequence=2,
        payload=payload(),
        parent_hash=first.chain_hash(),
    )
    invalid_order = second.model_copy(update={"parent_hash": same_sequence_parent.chain_hash()})
    assert not CheckpointManager._chain_is_valid((invalid_order, same_sequence_parent, first), 0)
