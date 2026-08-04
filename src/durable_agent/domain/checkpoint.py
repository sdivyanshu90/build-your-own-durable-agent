"""Explicit versioned checkpoint schemas and integrity envelope."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from durable_agent.domain.base import DomainModel, canonical_json, sha256_digest, utc_now
from durable_agent.domain.enums import RunState, TaskState, ToolCallStatus
from durable_agent.domain.errors import CorruptCheckpointError, UnsupportedSchemaVersionError

CURRENT_CHECKPOINT_SCHEMA_VERSION: Literal[1] = 1


class ToolCallCheckpoint(DomainModel):
    tool_call_id: str
    idempotency_key: str
    status: ToolCallStatus
    side_effect_class: str


class ErrorCheckpoint(DomainModel):
    error_id: str
    category: str
    message: str
    retryable: bool


class CheckpointPayload(DomainModel):
    """All durable references required for safe resume."""

    run_id: str
    run_state: RunState
    active_task_id: str | None
    task_states: dict[str, TaskState]
    completed_task_ids: tuple[str, ...]
    pending_task_ids: tuple[str, ...]
    plan_id: str
    plan_version: int = Field(ge=1)
    context_ids: tuple[str, ...] = ()
    summary_ids: tuple[str, ...] = ()
    tool_calls: tuple[ToolCallCheckpoint, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    retry_counters: dict[str, int] = Field(default_factory=dict)
    error: ErrorCheckpoint | None = None
    repository_snapshot_id: str | None = None
    repository_manifest_hash: str | None = None
    configuration_fingerprint: str

    @model_validator(mode="after")
    def task_sets_are_consistent(self) -> CheckpointPayload:
        completed = set(self.completed_task_ids)
        pending = set(self.pending_task_ids)
        if completed & pending:
            raise ValueError("completed and pending tasks must be disjoint")
        known = set(self.task_states)
        if not completed <= known or not pending <= known:
            raise ValueError("checkpoint task references must exist in task_states")
        succeeded = {
            task_id for task_id, state in self.task_states.items() if state == TaskState.SUCCEEDED
        }
        if completed != succeeded:
            raise ValueError("completed tasks must exactly match succeeded task states")
        if any(value < 0 for value in self.retry_counters.values()):
            raise ValueError("retry counters cannot be negative")
        return self


class CheckpointEnvelope(DomainModel):
    """Hash-chained persisted checkpoint document."""

    checkpoint_id: str
    run_id: str
    schema_version: Literal[1] = CURRENT_CHECKPOINT_SCHEMA_VERSION
    sequence: int = Field(ge=1)
    parent_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    payload: CheckpointPayload
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def identifiers_agree(self) -> CheckpointEnvelope:
        if self.run_id != self.payload.run_id:
            raise ValueError("envelope and payload run IDs differ")
        return self

    @classmethod
    def create(
        cls,
        *,
        checkpoint_id: str,
        sequence: int,
        payload: CheckpointPayload,
        parent_hash: str | None = None,
        created_at: datetime | None = None,
    ) -> CheckpointEnvelope:
        """Build an integrity-protected envelope."""
        return cls(
            checkpoint_id=checkpoint_id,
            run_id=payload.run_id,
            sequence=sequence,
            parent_hash=parent_hash,
            payload=payload,
            payload_hash=sha256_digest(canonical_json(payload.model_dump(mode="json"))),
            created_at=created_at or utc_now(),
        )

    def verify(self) -> None:
        """Raise when schema or payload integrity is invalid."""
        if self.schema_version != CURRENT_CHECKPOINT_SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                f"unsupported checkpoint schema {self.schema_version}"
            )
        actual = sha256_digest(canonical_json(self.payload.model_dump(mode="json")))
        if actual != self.payload_hash:
            raise CorruptCheckpointError(
                f"checkpoint {self.checkpoint_id} payload hash mismatch",
                details={"expected": self.payload_hash, "actual": actual},
            )

    def chain_hash(self) -> str:
        """Hash the complete stable envelope for the next parent link."""
        return sha256_digest(canonical_json(self.model_dump(mode="json")))

    @classmethod
    def from_untrusted_json(cls, value: str | bytes | dict[str, Any]) -> CheckpointEnvelope:
        """Parse JSON without unsafe deserialization and verify integrity."""
        if isinstance(value, dict):
            envelope = cls.model_validate(value)
        else:
            envelope = cls.model_validate_json(value)
        envelope.verify()
        return envelope
