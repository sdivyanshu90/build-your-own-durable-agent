"""Core run, task, plan, repository, and tool schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, field_validator, model_validator

from durable_agent.domain.base import DomainModel, utc_now
from durable_agent.domain.enums import (
    CheckpointPolicy,
    FailurePolicy,
    FileChangeKind,
    RunState,
    SideEffectClass,
    TaskState,
    ToolCallStatus,
)


class TaskSpec(DomainModel):
    """Immutable intent for one executable node in a plan."""

    task_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=3, max_length=10_000)
    dependencies: tuple[str, ...] = ()
    priority: int = Field(default=100, ge=0, le=10_000)
    maximum_attempts: int = Field(default=3, ge=1, le=20)
    input_references: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = Field(min_length=1)
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    required_evidence: tuple[str, ...] = Field(min_length=1)
    required_tools: tuple[str, ...] = ()
    tool_permissions: frozenset[str] = frozenset()
    estimated_context_tokens: int = Field(default=2048, ge=128, le=1_000_000)
    checkpoint_policy: CheckpointPolicy = CheckpointPolicy.BEFORE_AND_AFTER
    failure_policy: FailurePolicy = FailurePolicy.RETRY
    parallelizable: bool = False

    @field_validator(
        "dependencies",
        "input_references",
        "expected_outputs",
        "acceptance_criteria",
        "required_evidence",
        "required_tools",
    )
    @classmethod
    def unique_ordered_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("values must be unique")
        if any(not item.strip() for item in value):
            raise ValueError("values must not be blank")
        return value

    @model_validator(mode="after")
    def reject_self_dependency(self) -> TaskSpec:
        if self.task_id in self.dependencies:
            raise ValueError("a task cannot depend on itself")
        return self


class PlanSpec(DomainModel):
    """A complete immutable task graph revision."""

    plan_id: str
    run_id: str
    version: int = Field(ge=1)
    goal: str = Field(min_length=3, max_length=20_000)
    scope: tuple[str, ...] = Field(min_length=1)
    assumptions: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    research_questions: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    expected_artifacts: tuple[str, ...] = ()
    verification_steps: tuple[str, ...] = Field(min_length=1)
    rollback_considerations: tuple[str, ...] = Field(min_length=1)
    tasks: tuple[TaskSpec, ...] = Field(min_length=1, max_length=100)
    revision_reason: str = Field(default="initial plan", min_length=3, max_length=2_000)
    previous_plan_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class RunRecord(DomainModel):
    """Materialized run state."""

    run_id: str
    owner_id: str
    objective: str = Field(min_length=1, max_length=100_000)
    state: RunState = RunState.CREATED
    active_plan_id: str | None = None
    active_task_id: str | None = None
    repository_root: str | None = None
    repository_snapshot_id: str | None = None
    configuration_fingerprint: str
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None


class TaskRecord(DomainModel):
    """Materialized execution state for a task spec."""

    run_id: str
    plan_id: str
    spec: TaskSpec
    state: TaskState = TaskState.PENDING
    attempt_count: int = Field(default=0, ge=0)
    version: int = Field(default=1, ge=1)
    last_error_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def attempts_within_bound(self) -> TaskRecord:
        if self.attempt_count > self.spec.maximum_attempts:
            raise ValueError("attempt_count exceeds maximum_attempts")
        return self


class RepositoryFile(DomainModel):
    """One file in a content-addressed repository snapshot."""

    file_id: str
    snapshot_id: str
    relative_path: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str
    language: str | None = None
    change_kind: FileChangeKind = FileChangeKind.NEW
    is_deleted: bool = False
    indexed_at: datetime = Field(default_factory=utc_now)


class RepositoryChunk(DomainModel):
    """Line-addressable repository content with provenance."""

    chunk_id: str
    file_id: str
    snapshot_id: str
    relative_path: str
    content: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    symbol_name: str | None = None
    language: str | None = None
    imports: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid_lines(self) -> RepositoryChunk:
        if self.end_line < self.start_line:
            raise ValueError("end_line must be at or after start_line")
        return self


class RepositorySnapshot(DomainModel):
    """Manifest identifying exactly which repository contents a run used."""

    snapshot_id: str
    root: str
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    files: tuple[RepositoryFile, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)


class RetrievalItem(DomainModel):
    """Retrieved content with primary provenance."""

    item_id: str
    source_type: str
    source: str
    source_location: str
    content: str
    content_hash: str
    score: float
    snapshot_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolDefinition(DomainModel):
    """Security and recovery contract declared by every tool."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    timeout_seconds: float = Field(gt=0, le=3_600)
    required_permissions: frozenset[str]
    side_effect_class: SideEffectClass
    retry_safe: bool
    produces_evidence: bool

    @model_validator(mode="after")
    def non_idempotent_not_retry_safe(self) -> ToolDefinition:
        if self.side_effect_class == SideEffectClass.NON_IDEMPOTENT and self.retry_safe:
            raise ValueError("non-idempotent tools cannot declare unconditional retry safety")
        return self


class ToolCall(DomainModel):
    """Durable tool intent and lifecycle."""

    tool_call_id: str
    run_id: str
    task_id: str
    tool_name: str
    arguments: dict[str, Any]
    arguments_hash: str
    idempotency_key: str
    side_effect_class: SideEffectClass
    status: ToolCallStatus = ToolCallStatus.INTENT_RECORDED
    attempt: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ToolResult(DomainModel):
    """Bounded and redacted durable result for a tool call."""

    tool_result_id: str
    tool_call_id: str
    success: bool
    output: dict[str, Any]
    output_hash: str
    exit_code: int | None = None
    truncated: bool = False
    duration_seconds: float = Field(ge=0)
    created_at: datetime = Field(default_factory=utc_now)


class LeaseRecord(DomainModel):
    """Renewable execution ownership with a monotonically increasing fence."""

    run_id: str
    owner_id: str
    expires_at: datetime
    fencing_token: int = Field(ge=1)
    version: int = Field(ge=1)
    acquired_at: datetime
    renewed_at: datetime


class LifecycleRequest(DomainModel):
    """Durable pause or cancellation request."""

    request_id: str
    run_id: str
    kind: str = Field(pattern=r"^(pause|cancel)$")
    reason: str = Field(min_length=1, max_length=4_000)
    requested_by: str
    status: str = Field(default="PENDING", pattern=r"^(PENDING|APPLIED|REJECTED)$")
    created_at: datetime = Field(default_factory=utc_now)
    applied_at: datetime | None = None


class ArtifactRecord(DomainModel):
    """Durable catalogue metadata for immutable artifact bytes."""

    artifact_id: str
    run_id: str
    task_id: str | None = None
    uri: str
    media_type: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    created_at: datetime = Field(default_factory=utc_now)
