"""Transactional SQL repositories for runs, checkpoints, tools, evidence, and indexes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from durable_agent.domain.base import canonical_json, sha256_digest
from durable_agent.domain.checkpoint import CheckpointEnvelope, ErrorCheckpoint
from durable_agent.domain.context import ContextItem, ContextSnapshot, SummaryRecord
from durable_agent.domain.enums import RunState, TaskState, ToolCallStatus
from durable_agent.domain.errors import (
    ConcurrencyConflictError,
    CorruptCheckpointError,
    DatabaseError,
    DurableAgentError,
    IdempotencyConflictError,
    NotFoundError,
    PlanValidationError,
    UnsupportedSchemaVersionError,
)
from durable_agent.domain.evidence import Claim, EvidenceRecord
from durable_agent.domain.models import (
    ArtifactRecord,
    LeaseRecord,
    LifecycleRequest,
    PlanSpec,
    RepositoryChunk,
    RepositoryFile,
    RepositorySnapshot,
    RunRecord,
    TaskRecord,
    ToolCall,
    ToolResult,
)
from durable_agent.domain.protocols import Clock, IdentifierGenerator
from durable_agent.persistence.database import Database
from durable_agent.persistence.orm import (
    ArtifactORM,
    CancellationRequestORM,
    CheckpointORM,
    ClaimEvidenceLinkORM,
    ClaimORM,
    ContextORM,
    ErrorORM,
    EventORM,
    EvidenceORM,
    IdempotencyKeyORM,
    LeaseORM,
    PauseRequestORM,
    PlanORM,
    PlanRevisionORM,
    ReportORM,
    RepositoryChunkORM,
    RepositoryFileORM,
    RepositorySnapshotORM,
    RunORM,
    SummaryORM,
    TaskAttemptORM,
    TaskDependencyORM,
    TaskORM,
    ToolCallORM,
    ToolResultORM,
)
from durable_agent.repository.models import FileSummary, RepositoryIndex

_TERMINAL_RUN_STATES = frozenset(
    {RunState.COMPLETED.value, RunState.FAILED.value, RunState.CANCELLED.value}
)
_PERMANENT_AUDIT_EVENTS = frozenset(
    {
        "run.created",
        "run.completed",
        "run.failed",
        "run.cancelled",
        "run.paused",
        "pause.requested",
        "cancellation.requested",
        "task.failed_retryable",
        "task.failed_terminal",
        "manual_review.required",
        "plan.created",
        "plan.revised",
        "recovery.completed",
        "recovery.configuration_incompatible",
        "recovery.abandoned_attempt",
        "checkpoint.recovered",
        "checkpoint.recovery_failed",
        "repository.drift_detected",
        "retention.events_compacted",
    }
)


class SqlStore:
    """Cohesive transaction boundary for the local application and worker."""

    def __init__(
        self, database: Database, *, identifiers: IdentifierGenerator, clock: Clock
    ) -> None:
        self.database = database
        self._ids = identifiers
        self._clock = clock

    async def schema_ready(self) -> bool:
        """Return whether the migrated core table can be queried."""
        try:
            async with self.database.sessions() as session:
                await session.scalar(select(func.count()).select_from(RunORM))
            return True
        except Exception:
            return False

    async def create_run(
        self, run: RunRecord, *, idempotency_key: str, request_hash: str
    ) -> RunRecord:
        key_pk = self._idempotency_pk(run.owner_id, "create_run", idempotency_key)
        try:
            async with self.database.sessions.begin() as session:
                existing = await session.get(IdempotencyKeyORM, key_pk)
                if existing:
                    if existing.request_hash != request_hash:
                        raise IdempotencyConflictError(
                            "idempotency key was reused with a different run request"
                        )
                    row = await session.get(RunORM, existing.resource_id)
                    if row is None:
                        raise DatabaseError("idempotency record references a missing run")
                    return self._run_from_row(row)
                session.add(self._run_to_row(run))
                session.add(
                    IdempotencyKeyORM(
                        idempotency_pk=key_pk,
                        owner_id=run.owner_id,
                        action="create_run",
                        idempotency_key=idempotency_key,
                        request_hash=request_hash,
                        resource_id=run.run_id,
                        created_at=self._clock.now(),
                    )
                )
            return run
        except IdempotencyConflictError:
            raise
        except IntegrityError as exc:
            # A concurrent identical request may win; reload through a clean transaction.
            async with self.database.sessions() as session:
                existing = await session.get(IdempotencyKeyORM, key_pk)
                if existing and existing.request_hash == request_hash:
                    row = await session.get(RunORM, existing.resource_id)
                    if row:
                        return self._run_from_row(row)
            raise ConcurrencyConflictError("concurrent run creation conflict") from exc
        except SQLAlchemyError as exc:
            raise DatabaseError("failed to create run") from exc

    async def find_idempotent_run(
        self,
        *,
        owner_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> RunRecord | None:
        """Resolve a prior create request before repeating indexing work."""
        key_pk = self._idempotency_pk(owner_id, "create_run", idempotency_key)
        async with self.database.sessions() as session:
            existing = await session.get(IdempotencyKeyORM, key_pk)
            if existing is None:
                return None
            if existing.request_hash != request_hash:
                raise IdempotencyConflictError(
                    "idempotency key was reused with a different run request"
                )
            row = await session.get(RunORM, existing.resource_id)
            if row is None:
                raise DatabaseError("idempotency record references a missing run")
            return self._run_from_row(row)

    async def claim_idempotent_action(
        self,
        *,
        owner_id: str,
        action: str,
        idempotency_key: str,
        request_hash: str,
        resource_id: str,
    ) -> bool:
        """Persist an operation intent; return false for an identical prior intent."""
        key_pk = self._idempotency_pk(owner_id, action, idempotency_key)
        try:
            async with self.database.sessions.begin() as session:
                existing = await session.get(IdempotencyKeyORM, key_pk)
                if existing is not None:
                    if existing.request_hash != request_hash:
                        raise IdempotencyConflictError(
                            "idempotency key was reused with different action input"
                        )
                    if existing.resource_id != resource_id:
                        raise DatabaseError("idempotency intent references another resource")
                    return False
                session.add(
                    IdempotencyKeyORM(
                        idempotency_pk=key_pk,
                        owner_id=owner_id,
                        action=action,
                        idempotency_key=idempotency_key,
                        request_hash=request_hash,
                        resource_id=resource_id,
                        created_at=self._clock.now(),
                    )
                )
            return True
        except (IdempotencyConflictError, DatabaseError):
            raise
        except IntegrityError as exc:
            async with self.database.sessions() as session:
                existing = await session.get(IdempotencyKeyORM, key_pk)
                if (
                    existing is not None
                    and existing.request_hash == request_hash
                    and existing.resource_id == resource_id
                ):
                    return False
            raise ConcurrencyConflictError("concurrent idempotency intent conflict") from exc

    async def get_run(self, run_id: str) -> RunRecord:
        async with self.database.sessions() as session:
            row = await session.get(RunORM, run_id)
            if row is None:
                raise NotFoundError(f"run not found: {run_id}")
            return self._run_from_row(row)

    async def list_runs(self, *, owner_id: str | None = None) -> Sequence[RunRecord]:
        statement = select(RunORM).order_by(RunORM.created_at.desc(), RunORM.run_id)
        if owner_id is not None:
            statement = statement.where(RunORM.owner_id == owner_id)
        async with self.database.sessions() as session:
            rows = (await session.scalars(statement)).all()
            return tuple(self._run_from_row(row) for row in rows)

    async def update_run(self, run: RunRecord, *, expected_version: int) -> RunRecord:
        values = run.model_dump(exclude={"run_id", "version"})
        values["state"] = run.state.value
        values["version"] = expected_version + 1
        statement = (
            update(RunORM)
            .where(RunORM.run_id == run.run_id, RunORM.version == expected_version)
            .values(**values)
        )
        try:
            async with self.database.sessions.begin() as session:
                result = await session.execute(statement)
                if result.rowcount != 1:
                    raise ConcurrencyConflictError(
                        f"run {run.run_id} version changed from {expected_version}"
                    )
            return run.model_copy(update={"version": expected_version + 1})
        except ConcurrencyConflictError:
            raise
        except SQLAlchemyError as exc:
            raise DatabaseError("failed to update run") from exc

    async def save_plan(self, plan: PlanSpec) -> None:
        payload = plan.model_dump(mode="json")
        try:
            async with self.database.sessions.begin() as session:
                session.add(
                    PlanORM(
                        plan_id=plan.plan_id,
                        run_id=plan.run_id,
                        version=plan.version,
                        payload=payload,
                        created_at=plan.created_at,
                    )
                )
                session.add(
                    PlanRevisionORM(
                        revision_id=f"revision-{plan.plan_id}",
                        plan_id=plan.plan_id,
                        run_id=plan.run_id,
                        version=plan.version,
                        previous_plan_id=plan.previous_plan_id,
                        reason=plan.revision_reason,
                        created_at=plan.created_at,
                    )
                )
        except IntegrityError as exc:
            raise PlanValidationError("plan ID or run version already exists") from exc
        except SQLAlchemyError as exc:
            raise DatabaseError("failed to save plan") from exc

    async def get_plan(self, run_id: str, *, version: int | None = None) -> PlanSpec:
        statement = select(PlanORM).where(PlanORM.run_id == run_id)
        if version is not None:
            statement = statement.where(PlanORM.version == version)
        else:
            statement = statement.order_by(PlanORM.version.desc()).limit(1)
        async with self.database.sessions() as session:
            row = await session.scalar(statement)
            if row is None:
                raise NotFoundError(f"plan not found for run: {run_id}")
            return PlanSpec.model_validate(row.payload)

    async def save_tasks(self, tasks: Sequence[TaskRecord]) -> None:
        try:
            async with self.database.sessions.begin() as session:
                for task in tasks:
                    session.add(self._task_to_row(task))
                    for dependency in task.spec.dependencies:
                        session.add(
                            TaskDependencyORM(
                                dependency_id=f"{task.run_id}:{task.plan_id}:{task.spec.task_id}:{dependency}",
                                run_id=task.run_id,
                                plan_id=task.plan_id,
                                task_id=task.spec.task_id,
                                depends_on_task_id=dependency,
                            )
                        )
        except IntegrityError as exc:
            raise PlanValidationError("duplicate task or task dependency") from exc
        except SQLAlchemyError as exc:
            raise DatabaseError("failed to save tasks") from exc

    async def get_tasks(self, run_id: str, *, plan_id: str | None = None) -> Sequence[TaskRecord]:
        """Get active tasks by default or a historical plan's task materialization."""
        async with self.database.sessions() as session:
            run = await session.get(RunORM, run_id)
            if run is None:
                raise NotFoundError(f"run not found: {run_id}")
            requested_plan_id = plan_id or run.active_plan_id
            if requested_plan_id is None:
                requested_plan_id = await session.scalar(
                    select(PlanORM.plan_id)
                    .where(PlanORM.run_id == run_id)
                    .order_by(PlanORM.version.desc())
                    .limit(1)
                )
            if requested_plan_id is None:
                return ()
            statement = (
                select(TaskORM)
                .where(TaskORM.run_id == run_id, TaskORM.plan_id == requested_plan_id)
                .order_by(TaskORM.task_id)
            )
            rows = (await session.scalars(statement)).all()
            return tuple(self._task_from_row(row) for row in rows)

    async def checkpoint_tip_sequence(self, run_id: str) -> int:
        """Return the persisted sequence tip even when its JSON is corrupt."""
        async with self.database.sessions() as session:
            value = await session.scalar(
                select(func.max(CheckpointORM.sequence)).where(CheckpointORM.run_id == run_id)
            )
            return int(value or 0)

    async def update_task(self, task: TaskRecord, *, expected_version: int) -> TaskRecord:
        values = {
            "state": task.state.value,
            "attempt_count": task.attempt_count,
            "last_error_id": task.last_error_id,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "version": expected_version + 1,
        }
        statement = (
            update(TaskORM)
            .where(
                TaskORM.task_pk == self._task_pk(task.run_id, task.plan_id, task.spec.task_id),
                TaskORM.version == expected_version,
            )
            .values(**values)
        )
        async with self.database.sessions.begin() as session:
            result = await session.execute(statement)
            if result.rowcount != 1:
                raise ConcurrencyConflictError(
                    f"task {task.spec.task_id} version changed from {expected_version}"
                )
        return task.model_copy(update={"version": expected_version + 1})

    async def start_task_attempt(self, *, run_id: str, task_id: str, attempt_number: int) -> str:
        """Persist the start of an attempt before invoking its worker."""
        attempt_id = self._ids.new("attempt")
        try:
            async with self.database.sessions.begin() as session:
                session.add(
                    TaskAttemptORM(
                        attempt_id=attempt_id,
                        run_id=run_id,
                        task_id=task_id,
                        attempt_number=attempt_number,
                        state=TaskState.RUNNING.value,
                        started_at=self._clock.now(),
                    )
                )
            return attempt_id
        except IntegrityError as exc:
            raise ConcurrencyConflictError("task attempt already exists") from exc

    async def finish_task_attempt(
        self,
        attempt_id: str,
        *,
        state: TaskState,
        error_id: str | None = None,
    ) -> None:
        """Close an attempt exactly once with its durable terminal/retry state."""
        async with self.database.sessions.begin() as session:
            result = await session.execute(
                update(TaskAttemptORM)
                .where(
                    TaskAttemptORM.attempt_id == attempt_id,
                    TaskAttemptORM.finished_at.is_(None),
                )
                .values(state=state.value, error_id=error_id, finished_at=self._clock.now())
            )
            if result.rowcount != 1:
                raise ConcurrencyConflictError("task attempt was already finished or is missing")

    async def update_task_and_finish_attempt(
        self,
        task: TaskRecord,
        *,
        expected_version: int,
        attempt_id: str,
        attempt_state: TaskState,
        error_id: str | None = None,
    ) -> TaskRecord:
        """Atomically materialize a task outcome and close its attempt."""
        task_values = {
            "state": task.state.value,
            "attempt_count": task.attempt_count,
            "last_error_id": task.last_error_id,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "version": expected_version + 1,
        }
        async with self.database.sessions.begin() as session:
            task_result = await session.execute(
                update(TaskORM)
                .where(
                    TaskORM.task_pk == self._task_pk(task.run_id, task.plan_id, task.spec.task_id),
                    TaskORM.version == expected_version,
                )
                .values(**task_values)
            )
            attempt_result = await session.execute(
                update(TaskAttemptORM)
                .where(
                    TaskAttemptORM.attempt_id == attempt_id,
                    TaskAttemptORM.finished_at.is_(None),
                )
                .values(
                    state=attempt_state.value,
                    error_id=error_id,
                    finished_at=self._clock.now(),
                )
            )
            if task_result.rowcount != 1 or attempt_result.rowcount != 1:
                raise ConcurrencyConflictError("task outcome or attempt changed concurrently")
        return task.model_copy(update={"version": expected_version + 1})

    async def finish_open_task_attempts(
        self,
        *,
        run_id: str,
        task_id: str,
        state: TaskState,
        error_id: str,
    ) -> int:
        """Close attempts abandoned by a dead worker during recovery."""
        async with self.database.sessions.begin() as session:
            result = await session.execute(
                update(TaskAttemptORM)
                .where(
                    TaskAttemptORM.run_id == run_id,
                    TaskAttemptORM.task_id == task_id,
                    TaskAttemptORM.finished_at.is_(None),
                )
                .values(state=state.value, error_id=error_id, finished_at=self._clock.now())
            )
            return int(result.rowcount or 0)

    async def cancel_task_and_open_attempts(
        self,
        task: TaskRecord,
        *,
        expected_version: int,
    ) -> TaskRecord:
        """Atomically cancel materialized task state and any crash-left open attempt."""
        task_values = {
            "state": task.state.value,
            "attempt_count": task.attempt_count,
            "last_error_id": task.last_error_id,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "version": expected_version + 1,
        }
        async with self.database.sessions.begin() as session:
            task_result = await session.execute(
                update(TaskORM)
                .where(
                    TaskORM.task_pk == self._task_pk(task.run_id, task.plan_id, task.spec.task_id),
                    TaskORM.version == expected_version,
                )
                .values(**task_values)
            )
            if task_result.rowcount != 1:
                raise ConcurrencyConflictError(
                    f"task {task.spec.task_id} changed during cancellation"
                )
            await session.execute(
                update(TaskAttemptORM)
                .where(
                    TaskAttemptORM.run_id == task.run_id,
                    TaskAttemptORM.task_id == task.spec.task_id,
                    TaskAttemptORM.finished_at.is_(None),
                )
                .values(
                    state=TaskState.CANCELLED.value,
                    finished_at=self._clock.now(),
                )
            )
        return task.model_copy(update={"version": expected_version + 1})

    async def record_error(
        self,
        *,
        run_id: str,
        task_id: str | None,
        error: DurableAgentError,
    ) -> str:
        """Persist a structured error without serializing exception objects."""
        error_id = self._ids.new("error")
        async with self.database.sessions.begin() as session:
            session.add(
                ErrorORM(
                    error_id=error_id,
                    run_id=run_id,
                    task_id=task_id,
                    category=error.category.value,
                    message=error.message,
                    details=error.details,
                    retryable=error.retryable,
                    stack_hash=None,
                    created_at=self._clock.now(),
                )
            )
        return error_id

    async def latest_error(self, run_id: str) -> ErrorCheckpoint | None:
        """Return the latest structured error for inclusion in a checkpoint."""
        async with self.database.sessions() as session:
            row = await session.scalar(
                select(ErrorORM)
                .where(ErrorORM.run_id == run_id)
                .order_by(ErrorORM.created_at.desc(), ErrorORM.error_id.desc())
                .limit(1)
            )
            if row is None:
                return None
            return ErrorCheckpoint(
                error_id=row.error_id,
                category=row.category,
                message=row.message,
                retryable=row.retryable,
            )

    async def list_artifact_ids(self, run_id: str) -> Sequence[str]:
        """List every artifact durably catalogued for a run."""
        async with self.database.sessions() as session:
            return tuple(
                (
                    await session.scalars(
                        select(ArtifactORM.artifact_id)
                        .where(ArtifactORM.run_id == run_id)
                        .order_by(ArtifactORM.artifact_id)
                    )
                ).all()
            )

    async def list_all_artifact_ids(self) -> frozenset[str]:
        """Return the catalogue snapshot used by singleton artifact maintenance."""
        async with self.database.sessions() as session:
            return frozenset((await session.scalars(select(ArtifactORM.artifact_id))).all())

    async def compact_terminal_events(
        self,
        *,
        older_than: datetime,
        dry_run: bool,
    ) -> tuple[int, tuple[str, ...]]:
        """Prune high-volume terminal-run events and retain a digest audit tombstone."""
        if older_than.tzinfo is None:
            raise ValueError("event retention cutoff must be timezone-aware")
        async with self.database.sessions() as session:
            run_ids = tuple(
                (
                    await session.scalars(
                        select(RunORM.run_id)
                        .where(RunORM.state.in_(_TERMINAL_RUN_STATES))
                        .order_by(RunORM.run_id)
                    )
                ).all()
            )
        eligible = 0
        compacted_runs: list[str] = []
        for run_id in run_ids:
            async with self.database.sessions.begin() as session:
                run_row = await session.scalar(
                    select(RunORM).where(RunORM.run_id == run_id).with_for_update()
                )
                if run_row is None or run_row.state not in _TERMINAL_RUN_STATES:
                    continue
                rows = (
                    await session.scalars(
                        select(EventORM)
                        .where(
                            EventORM.run_id == run_id,
                            EventORM.created_at < older_than,
                        )
                        .order_by(EventORM.sequence)
                    )
                ).all()
                candidates = tuple(
                    row
                    for row in rows
                    if row.event_type not in _PERMANENT_AUDIT_EVENTS
                    and not row.event_type.startswith(("security.", "retention."))
                )
                if not candidates:
                    continue
                eligible += len(candidates)
                compacted_runs.append(run_id)
                if dry_run:
                    continue
                archive_manifest = [
                    {
                        "event_id": row.event_id,
                        "sequence": row.sequence,
                        "event_type": row.event_type,
                        "task_id": row.task_id,
                        "payload": row.payload,
                        "created_at": self._aware(row.created_at).isoformat(),
                    }
                    for row in candidates
                ]
                digest = sha256_digest(canonical_json(archive_manifest))
                latest = await session.scalar(
                    select(func.max(EventORM.sequence)).where(EventORM.run_id == run_id)
                )
                session.add(
                    EventORM(
                        event_id=self._ids.new("event"),
                        run_id=run_id,
                        sequence=(latest or 0) + 1,
                        event_type="retention.events_compacted",
                        task_id=None,
                        payload={
                            "count": len(candidates),
                            "first_sequence": candidates[0].sequence,
                            "last_sequence": candidates[-1].sequence,
                            "archive_manifest_sha256": digest,
                            "cutoff": older_than.isoformat(),
                        },
                        created_at=self._clock.now(),
                    )
                )
                await session.execute(
                    delete(EventORM).where(
                        EventORM.event_id.in_(tuple(row.event_id for row in candidates))
                    )
                )
        return eligible, tuple(compacted_runs)

    async def get_artifacts(self, run_id: str) -> Sequence[ArtifactRecord]:
        """Return immutable artifact catalogue records for integrity verification."""
        async with self.database.sessions() as session:
            rows = (
                await session.scalars(
                    select(ArtifactORM)
                    .where(ArtifactORM.run_id == run_id)
                    .order_by(ArtifactORM.artifact_id)
                )
            ).all()
            return tuple(
                ArtifactRecord(
                    artifact_id=row.artifact_id,
                    run_id=row.run_id,
                    task_id=row.task_id,
                    uri=row.uri,
                    media_type=row.media_type,
                    content_hash=row.content_hash,
                    size_bytes=row.size_bytes,
                    created_at=self._aware(row.created_at),
                )
                for row in rows
            )

    async def record_artifact(
        self,
        *,
        artifact_id: str,
        run_id: str,
        task_id: str | None,
        uri: str,
        media_type: str,
        content_hash: str,
        size_bytes: int,
    ) -> None:
        """Catalog immutable artifact metadata after its bytes are durable."""
        try:
            async with self.database.sessions.begin() as session:
                existing = await session.get(ArtifactORM, artifact_id)
                if existing is not None:
                    if (
                        existing.run_id == run_id
                        and existing.content_hash == content_hash
                        and existing.uri == uri
                    ):
                        return
                    raise IdempotencyConflictError(
                        "artifact ID already references different metadata"
                    )
                session.add(
                    ArtifactORM(
                        artifact_id=artifact_id,
                        run_id=run_id,
                        task_id=task_id,
                        uri=uri,
                        media_type=media_type,
                        content_hash=content_hash,
                        size_bytes=size_bytes,
                        created_at=self._clock.now(),
                    )
                )
        except IdempotencyConflictError:
            raise
        except IntegrityError as exc:
            raise IdempotencyConflictError("artifact metadata already exists") from exc

    async def append_checkpoint(
        self, envelope: CheckpointEnvelope, *, expected_sequence: int
    ) -> None:
        envelope.verify()
        try:
            async with self.database.sessions.begin() as session:
                latest = await session.scalar(
                    select(CheckpointORM)
                    .where(CheckpointORM.run_id == envelope.run_id)
                    .order_by(CheckpointORM.sequence.desc())
                    .limit(1)
                )
                actual = latest.sequence if latest else 0
                if actual != expected_sequence or envelope.sequence != expected_sequence + 1:
                    raise ConcurrencyConflictError(
                        "checkpoint sequence conflict: "
                        f"expected {expected_sequence}, actual {actual}"
                    )
                if latest is not None:
                    previous_json = (
                        await session.scalars(
                            select(CheckpointORM.envelope_json)
                            .where(CheckpointORM.run_id == envelope.run_id)
                            .order_by(CheckpointORM.sequence.desc())
                        )
                    ).all()
                    parent_found = False
                    for value in previous_json:
                        try:
                            candidate = CheckpointEnvelope.from_untrusted_json(value)
                        except (
                            ValidationError,
                            CorruptCheckpointError,
                            UnsupportedSchemaVersionError,
                        ):
                            continue
                        if envelope.parent_hash == candidate.chain_hash():
                            parent_found = True
                            break
                    if not parent_found:
                        raise ConcurrencyConflictError(
                            "checkpoint parent hash does not match a valid retained checkpoint"
                        )
                elif envelope.parent_hash is not None:
                    raise ConcurrencyConflictError("first checkpoint cannot have a parent hash")
                session.add(
                    CheckpointORM(
                        checkpoint_id=envelope.checkpoint_id,
                        run_id=envelope.run_id,
                        schema_version=envelope.schema_version,
                        sequence=envelope.sequence,
                        parent_hash=envelope.parent_hash,
                        payload_hash=envelope.payload_hash,
                        envelope_json=envelope.model_dump_json(),
                        created_at=envelope.created_at,
                    )
                )
        except ConcurrencyConflictError:
            raise
        except IntegrityError as exc:
            raise ConcurrencyConflictError("concurrent checkpoint write") from exc
        except SQLAlchemyError as exc:
            raise DatabaseError("failed to append checkpoint") from exc

    async def list_checkpoints(self, run_id: str) -> Sequence[CheckpointEnvelope]:
        rows = await self.list_checkpoint_json(run_id)
        return tuple(CheckpointEnvelope.model_validate_json(value) for value in rows)

    async def list_checkpoint_json(self, run_id: str) -> Sequence[str]:
        statement = (
            select(CheckpointORM.envelope_json)
            .where(CheckpointORM.run_id == run_id)
            .order_by(CheckpointORM.sequence.desc())
        )
        async with self.database.sessions() as session:
            return tuple((await session.scalars(statement)).all())

    async def list_checkpoint_views(self, run_id: str) -> Sequence[Mapping[str, Any]]:
        """Return safe inspection records, including corrupt rows without their raw JSON."""
        statement = (
            select(CheckpointORM)
            .where(CheckpointORM.run_id == run_id)
            .order_by(CheckpointORM.sequence.desc())
        )
        async with self.database.sessions() as session:
            views: list[Mapping[str, Any]] = []
            for row in (await session.scalars(statement)).all():
                try:
                    envelope = CheckpointEnvelope.from_untrusted_json(row.envelope_json)
                    views.append(
                        {
                            **envelope.model_dump(mode="json"),
                            "integrity": "valid",
                        }
                    )
                except (
                    ValidationError,
                    CorruptCheckpointError,
                    UnsupportedSchemaVersionError,
                ) as exc:
                    views.append(
                        {
                            "checkpoint_id": row.checkpoint_id,
                            "run_id": row.run_id,
                            "sequence": row.sequence,
                            "schema_version": row.schema_version,
                            "created_at": self._aware(row.created_at).isoformat(),
                            "integrity": "invalid",
                            "error": str(exc)[:500],
                        }
                    )
            return tuple(views)

    async def corrupt_checkpoint_for_test(self, checkpoint_id: str, envelope_json: str) -> None:
        """Fault-injection hook intentionally unavailable through application interfaces."""
        async with self.database.sessions.begin() as session:
            result = await session.execute(
                update(CheckpointORM)
                .where(CheckpointORM.checkpoint_id == checkpoint_id)
                .values(envelope_json=envelope_json)
            )
            if result.rowcount != 1:
                raise NotFoundError(f"checkpoint not found: {checkpoint_id}")

    async def prune_checkpoints(self, run_id: str, *, retain: int) -> int:
        """Delete oldest checkpoints while preserving a usable recovery window."""
        if retain < 2:
            raise ValueError("retain must be at least two")
        async with self.database.sessions.begin() as session:
            cutoff = await session.scalar(
                select(CheckpointORM.sequence)
                .where(CheckpointORM.run_id == run_id)
                .order_by(CheckpointORM.sequence.desc())
                .offset(retain - 1)
                .limit(1)
            )
            if cutoff is None:
                return 0
            result = await session.execute(
                delete(CheckpointORM).where(
                    CheckpointORM.run_id == run_id, CheckpointORM.sequence < cutoff
                )
            )
            return int(result.rowcount or 0)

    async def publish(
        self,
        *,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        task_id: str | None = None,
    ) -> str:
        event_id = self._ids.new("event")
        event_payload = dict(payload)
        created_at = self._clock.now()
        last_error: SQLAlchemyError | None = None
        for attempt in range(8):
            try:
                async with self.database.sessions.begin() as session:
                    existing = await session.get(EventORM, event_id)
                    if existing is not None:
                        if (
                            existing.run_id == run_id
                            and existing.event_type == event_type
                            and existing.task_id == task_id
                            and existing.payload == event_payload
                        ):
                            return event_id
                        raise ConcurrencyConflictError("event ID already stores a different event")
                    latest = await session.scalar(
                        select(func.max(EventORM.sequence)).where(EventORM.run_id == run_id)
                    )
                    session.add(
                        EventORM(
                            event_id=event_id,
                            run_id=run_id,
                            sequence=(latest or 0) + 1,
                            event_type=event_type,
                            task_id=task_id,
                            payload=event_payload,
                            created_at=created_at,
                        )
                    )
                return event_id
            except ConcurrencyConflictError:
                raise
            except SQLAlchemyError as exc:
                last_error = exc
                if attempt == 7:
                    break
                await asyncio.sleep(0)
        raise ConcurrencyConflictError(
            "concurrent event append retry limit exceeded"
        ) from last_error

    async def stream(self, run_id: str) -> AsyncIterator[Mapping[str, Any]]:
        statement = select(EventORM).where(EventORM.run_id == run_id).order_by(EventORM.sequence)
        async with self.database.sessions() as session:
            for row in (await session.scalars(statement)).all():
                yield {
                    "event_id": row.event_id,
                    "sequence": row.sequence,
                    "event_type": row.event_type,
                    "task_id": row.task_id,
                    "payload": row.payload,
                    "created_at": row.created_at,
                }

    async def add_evidence(self, record: EvidenceRecord) -> None:
        try:
            async with self.database.sessions.begin() as session:
                existing = await session.get(EvidenceORM, record.evidence_id)
                if existing:
                    if self._evidence_from_row(existing) != record:
                        raise IdempotencyConflictError("evidence ID already stores different data")
                    return
                session.add(
                    EvidenceORM(
                        evidence_id=record.evidence_id,
                        run_id=record.run_id,
                        evidence_type=record.evidence_type.value,
                        source=record.source,
                        source_location=record.source_location,
                        content_hash=record.content_hash,
                        snapshot_id=record.snapshot_id,
                        related_task_id=record.related_task_id,
                        reliability=record.reliability,
                        excerpt=record.excerpt,
                        verification_status=record.verification_status.value,
                        metadata_json=record.metadata,
                        created_at=record.created_at,
                    )
                )
        except IdempotencyConflictError:
            raise
        except SQLAlchemyError as exc:
            raise DatabaseError("failed to add evidence") from exc

    async def get_evidence(self, run_id: str) -> Sequence[EvidenceRecord]:
        statement = (
            select(EvidenceORM)
            .where(EvidenceORM.run_id == run_id)
            .order_by(EvidenceORM.evidence_id)
        )
        async with self.database.sessions() as session:
            return tuple(
                self._evidence_from_row(row) for row in (await session.scalars(statement)).all()
            )

    async def add_claim(self, claim: Claim) -> None:
        try:
            async with self.database.sessions.begin() as session:
                if await session.get(ClaimORM, claim.claim_id):
                    raise IdempotencyConflictError(f"claim already exists: {claim.claim_id}")
                if claim.evidence_ids:
                    evidence = (
                        await session.scalars(
                            select(EvidenceORM).where(
                                EvidenceORM.evidence_id.in_(claim.evidence_ids)
                            )
                        )
                    ).all()
                    if {item.evidence_id for item in evidence} != set(claim.evidence_ids):
                        raise PlanValidationError("claim references missing evidence")
                    if any(item.run_id != claim.run_id for item in evidence):
                        raise PlanValidationError(
                            "claim cannot reference evidence from another run"
                        )
                session.add(
                    ClaimORM(
                        claim_id=claim.claim_id,
                        run_id=claim.run_id,
                        text=claim.text,
                        kind=claim.kind.value,
                        related_task_id=claim.related_task_id,
                    )
                )
                await session.flush()
                for evidence_id in claim.evidence_ids:
                    session.add(
                        ClaimEvidenceLinkORM(claim_id=claim.claim_id, evidence_id=evidence_id)
                    )
        except (IdempotencyConflictError, PlanValidationError):
            raise
        except SQLAlchemyError as exc:
            raise DatabaseError("failed to add claim") from exc

    async def get_claims(self, run_id: str) -> Sequence[Claim]:
        async with self.database.sessions() as session:
            rows = (
                await session.scalars(
                    select(ClaimORM).where(ClaimORM.run_id == run_id).order_by(ClaimORM.claim_id)
                )
            ).all()
            claims = []
            for row in rows:
                links = (
                    await session.scalars(
                        select(ClaimEvidenceLinkORM.evidence_id).where(
                            ClaimEvidenceLinkORM.claim_id == row.claim_id
                        )
                    )
                ).all()
                claims.append(
                    Claim(
                        claim_id=row.claim_id,
                        run_id=row.run_id,
                        text=row.text,
                        kind=row.kind,
                        evidence_ids=tuple(sorted(links)),
                        related_task_id=row.related_task_id,
                    )
                )
            return tuple(claims)

    async def get_by_idempotency_key(self, key: str) -> tuple[ToolCall, ToolResult | None] | None:
        async with self.database.sessions() as session:
            row = await session.scalar(
                select(ToolCallORM).where(ToolCallORM.idempotency_key == key)
            )
            if row is None:
                return None
            result = await session.scalar(
                select(ToolResultORM).where(ToolResultORM.tool_call_id == row.tool_call_id)
            )
            return self._tool_call_from_row(row), self._tool_result_from_row(
                result
            ) if result else None

    async def record_intent(self, call: ToolCall) -> None:
        try:
            async with self.database.sessions.begin() as session:
                session.add(
                    ToolCallORM(
                        tool_call_id=call.tool_call_id,
                        run_id=call.run_id,
                        task_id=call.task_id,
                        tool_name=call.tool_name,
                        arguments=call.arguments,
                        arguments_hash=call.arguments_hash,
                        idempotency_key=call.idempotency_key,
                        side_effect_class=call.side_effect_class.value,
                        status=call.status.value,
                        attempt=call.attempt,
                        created_at=call.created_at,
                        started_at=call.started_at,
                        finished_at=call.finished_at,
                    )
                )
        except IntegrityError as exc:
            raise IdempotencyConflictError("duplicate tool call or idempotency key") from exc

    async def update_status(self, call_id: str, status: ToolCallStatus) -> None:
        values: dict[str, Any] = {"status": status.value}
        if status == ToolCallStatus.RUNNING:
            values["started_at"] = self._clock.now()
        if status in {
            ToolCallStatus.SUCCEEDED,
            ToolCallStatus.FAILED,
            ToolCallStatus.NEEDS_REVIEW,
        }:
            values["finished_at"] = self._clock.now()
        async with self.database.sessions.begin() as session:
            result = await session.execute(
                update(ToolCallORM).where(ToolCallORM.tool_call_id == call_id).values(**values)
            )
            if result.rowcount != 1:
                raise NotFoundError(f"tool call not found: {call_id}")

    async def prepare_retry(self, call_id: str) -> None:
        """Atomically reopen a failed, result-less retry-safe tool intent."""
        async with self.database.sessions.begin() as session:
            has_result = await session.scalar(
                select(ToolResultORM.tool_result_id).where(ToolResultORM.tool_call_id == call_id)
            )
            if has_result is not None:
                raise ConcurrencyConflictError("completed tool calls cannot be retried")
            result = await session.execute(
                update(ToolCallORM)
                .where(
                    ToolCallORM.tool_call_id == call_id,
                    ToolCallORM.status == ToolCallStatus.FAILED.value,
                )
                .values(
                    status=ToolCallStatus.INTENT_RECORDED.value,
                    attempt=ToolCallORM.attempt + 1,
                    started_at=None,
                    finished_at=None,
                )
            )
            if result.rowcount != 1:
                raise ConcurrencyConflictError("tool call is not eligible for retry")

    async def record_result(self, result: ToolResult) -> None:
        try:
            async with self.database.sessions.begin() as session:
                session.add(
                    ToolResultORM(
                        tool_result_id=result.tool_result_id,
                        tool_call_id=result.tool_call_id,
                        success=result.success,
                        output=result.output,
                        output_hash=result.output_hash,
                        exit_code=result.exit_code,
                        truncated=result.truncated,
                        duration_seconds=result.duration_seconds,
                        created_at=result.created_at,
                    )
                )
                await session.execute(
                    update(ToolCallORM)
                    .where(ToolCallORM.tool_call_id == result.tool_call_id)
                    .values(
                        status=(
                            ToolCallStatus.SUCCEEDED.value
                            if result.success
                            else ToolCallStatus.FAILED.value
                        ),
                        finished_at=self._clock.now(),
                    )
                )
        except IntegrityError as exc:
            raise IdempotencyConflictError("tool call already has a result") from exc

    async def uncertain_calls(self, run_id: str) -> Sequence[ToolCall]:
        states = (
            ToolCallStatus.INTENT_RECORDED.value,
            ToolCallStatus.RUNNING.value,
            ToolCallStatus.UNCERTAIN.value,
        )
        statement = select(ToolCallORM).where(
            ToolCallORM.run_id == run_id, ToolCallORM.status.in_(states)
        )
        async with self.database.sessions() as session:
            return tuple(
                self._tool_call_from_row(row) for row in (await session.scalars(statement)).all()
            )

    async def list_tool_calls(self, run_id: str) -> Sequence[ToolCall]:
        """List all tool intents and their materialized statuses for a run."""
        statement = (
            select(ToolCallORM)
            .where(ToolCallORM.run_id == run_id)
            .order_by(ToolCallORM.created_at, ToolCallORM.tool_call_id)
        )
        async with self.database.sessions() as session:
            return tuple(
                self._tool_call_from_row(row) for row in (await session.scalars(statement)).all()
            )

    async def save_repository_index(self, index: RepositoryIndex) -> None:
        snapshot = index.snapshot
        payload = {
            "summaries": [item.model_dump(mode="json") for item in index.summaries],
            "repository_map": index.repository_map,
            "module_dependencies": index.module_dependencies,
            "warnings": index.warnings,
        }
        try:
            async with self.database.sessions.begin() as session:
                session.add(
                    RepositorySnapshotORM(
                        snapshot_id=snapshot.snapshot_id,
                        root=snapshot.root,
                        manifest_hash=snapshot.manifest_hash,
                        file_count=snapshot.file_count,
                        total_bytes=snapshot.total_bytes,
                        index_payload=payload,
                        created_at=snapshot.created_at,
                    )
                )
                await session.flush()
                for file in snapshot.files:
                    session.add(
                        RepositoryFileORM(
                            repository_file_pk=f"{snapshot.snapshot_id}:{file.file_id}",
                            file_id=file.file_id,
                            snapshot_id=snapshot.snapshot_id,
                            relative_path=file.relative_path,
                            content_hash=file.content_hash,
                            size_bytes=file.size_bytes,
                            media_type=file.media_type,
                            language=file.language,
                            change_kind=file.change_kind.value,
                            is_deleted=file.is_deleted,
                            indexed_at=file.indexed_at,
                        )
                    )
                for chunk in index.chunks:
                    session.add(
                        RepositoryChunkORM(
                            chunk_id=chunk.chunk_id,
                            file_id=chunk.file_id,
                            snapshot_id=chunk.snapshot_id,
                            relative_path=chunk.relative_path,
                            content=chunk.content,
                            content_hash=chunk.content_hash,
                            start_line=chunk.start_line,
                            end_line=chunk.end_line,
                            symbol_name=chunk.symbol_name,
                            language=chunk.language,
                            imports=list(chunk.imports),
                        )
                    )
        except IntegrityError as exc:
            raise IdempotencyConflictError("repository snapshot already exists") from exc

    async def get_repository_index(self, snapshot_id: str) -> RepositoryIndex:
        async with self.database.sessions() as session:
            row = await session.get(RepositorySnapshotORM, snapshot_id)
            if row is None:
                raise NotFoundError(f"repository snapshot not found: {snapshot_id}")
            file_rows = (
                await session.scalars(
                    select(RepositoryFileORM)
                    .where(RepositoryFileORM.snapshot_id == snapshot_id)
                    .order_by(RepositoryFileORM.relative_path)
                )
            ).all()
            chunk_rows = (
                await session.scalars(
                    select(RepositoryChunkORM)
                    .where(RepositoryChunkORM.snapshot_id == snapshot_id)
                    .order_by(RepositoryChunkORM.relative_path, RepositoryChunkORM.start_line)
                )
            ).all()
            files = tuple(self._repository_file_from_row(item) for item in file_rows)
            snapshot = RepositorySnapshot(
                snapshot_id=row.snapshot_id,
                root=row.root,
                manifest_hash=row.manifest_hash,
                file_count=row.file_count,
                total_bytes=row.total_bytes,
                files=files,
                created_at=self._aware(row.created_at),
            )
            return RepositoryIndex(
                snapshot=snapshot,
                chunks=tuple(self._repository_chunk_from_row(item) for item in chunk_rows),
                summaries=tuple(
                    FileSummary.model_validate(item) for item in row.index_payload["summaries"]
                ),
                repository_map=str(row.index_payload["repository_map"]),
                module_dependencies={
                    key: tuple(value)
                    for key, value in row.index_payload["module_dependencies"].items()
                },
                warnings=tuple(row.index_payload["warnings"]),
            )

    async def create_lifecycle_request(
        self,
        request: LifecycleRequest,
        *,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> LifecycleRequest:
        row_type = PauseRequestORM if request.kind == "pause" else CancellationRequestORM
        action = f"{request.kind}:{request.run_id}"
        key_pk = (
            self._idempotency_pk(request.requested_by, action, idempotency_key)
            if idempotency_key
            else None
        )
        async with self.database.sessions.begin() as session:
            if key_pk is not None:
                existing = await session.get(IdempotencyKeyORM, key_pk)
                if existing is not None:
                    if existing.request_hash != request_hash:
                        raise IdempotencyConflictError(
                            "lifecycle idempotency key was reused with different input"
                        )
                    existing_request = await session.get(row_type, existing.resource_id)
                    if existing_request is None:
                        raise DatabaseError("idempotency record references a missing request")
                    return self._lifecycle_from_row(existing_request, request.kind)
            session.add(
                row_type(
                    request_id=request.request_id,
                    run_id=request.run_id,
                    reason=request.reason,
                    requested_by=request.requested_by,
                    status=request.status,
                    created_at=request.created_at,
                    applied_at=request.applied_at,
                )
            )
            if key_pk is not None:
                if request_hash is None:
                    raise ValueError("request_hash is required with idempotency_key")
                session.add(
                    IdempotencyKeyORM(
                        idempotency_pk=key_pk,
                        owner_id=request.requested_by,
                        action=action,
                        idempotency_key=idempotency_key,
                        request_hash=request_hash,
                        resource_id=request.request_id,
                        created_at=self._clock.now(),
                    )
                )
        return request

    async def find_idempotent_lifecycle_request(
        self,
        *,
        owner_id: str,
        run_id: str,
        kind: str,
        idempotency_key: str,
        request_hash: str,
    ) -> LifecycleRequest | None:
        """Resolve an already accepted lifecycle mutation without state revalidation."""
        if kind not in {"pause", "cancel"}:
            raise ValueError("lifecycle kind must be pause or cancel")
        action = f"{kind}:{run_id}"
        key_pk = self._idempotency_pk(owner_id, action, idempotency_key)
        row_type = PauseRequestORM if kind == "pause" else CancellationRequestORM
        async with self.database.sessions() as session:
            existing = await session.get(IdempotencyKeyORM, key_pk)
            if existing is None:
                return None
            if existing.request_hash != request_hash:
                raise IdempotencyConflictError(
                    "lifecycle idempotency key was reused with different input"
                )
            row = await session.get(row_type, existing.resource_id)
            if row is None:
                raise DatabaseError("idempotency record references a missing request")
            return self._lifecycle_from_row(row, kind)

    async def pending_lifecycle_request(self, run_id: str) -> LifecycleRequest | None:
        async with self.database.sessions() as session:
            cancel = await session.scalar(
                select(CancellationRequestORM)
                .where(
                    CancellationRequestORM.run_id == run_id,
                    CancellationRequestORM.status == "PENDING",
                )
                .order_by(CancellationRequestORM.created_at)
                .limit(1)
            )
            if cancel:
                return self._lifecycle_from_row(cancel, "cancel")
            pause = await session.scalar(
                select(PauseRequestORM)
                .where(PauseRequestORM.run_id == run_id, PauseRequestORM.status == "PENDING")
                .order_by(PauseRequestORM.created_at)
                .limit(1)
            )
            return self._lifecycle_from_row(pause, "pause") if pause else None

    async def apply_lifecycle_request(self, request: LifecycleRequest) -> LifecycleRequest:
        row_type: Any = PauseRequestORM if request.kind == "pause" else CancellationRequestORM
        applied = self._clock.now()
        async with self.database.sessions.begin() as session:
            result = await session.execute(
                update(row_type)
                .where(row_type.request_id == request.request_id, row_type.status == "PENDING")
                .values(status="APPLIED", applied_at=applied)
            )
            if result.rowcount != 1:
                raise ConcurrencyConflictError("lifecycle request was already handled")
        return request.model_copy(update={"status": "APPLIED", "applied_at": applied})

    async def acquire_lease(self, run_id: str, owner_id: str, *, ttl_seconds: int) -> LeaseRecord:
        now = self._clock.now()
        expires = now + timedelta(seconds=ttl_seconds)
        try:
            async with self.database.sessions.begin() as session:
                row = await session.get(LeaseORM, run_id)
                if row is None:
                    lease = LeaseRecord(
                        run_id=run_id,
                        owner_id=owner_id,
                        expires_at=expires,
                        fencing_token=1,
                        version=1,
                        acquired_at=now,
                        renewed_at=now,
                    )
                    session.add(self._lease_to_row(lease))
                    return lease
                row_expiry = self._aware(row.expires_at)
                if row.owner_id != owner_id and row_expiry > now:
                    raise ConcurrencyConflictError(
                        f"run {run_id} is leased by {row.owner_id} until {row_expiry.isoformat()}"
                    )
                version = row.version
                fence = row.fencing_token if row.owner_id == owner_id else row.fencing_token + 1
                acquired = self._aware(row.acquired_at) if row.owner_id == owner_id else now
                result = await session.execute(
                    update(LeaseORM)
                    .where(LeaseORM.run_id == run_id, LeaseORM.version == version)
                    .values(
                        owner_id=owner_id,
                        expires_at=expires,
                        fencing_token=fence,
                        version=version + 1,
                        acquired_at=acquired,
                        renewed_at=now,
                    )
                )
                if result.rowcount != 1:
                    raise ConcurrencyConflictError("lease changed during acquisition")
                return LeaseRecord(
                    run_id=run_id,
                    owner_id=owner_id,
                    expires_at=expires,
                    fencing_token=fence,
                    version=version + 1,
                    acquired_at=acquired,
                    renewed_at=now,
                )
        except ConcurrencyConflictError:
            raise
        except IntegrityError as exc:
            raise ConcurrencyConflictError("concurrent lease acquisition") from exc

    async def renew_lease(self, lease: LeaseRecord, *, ttl_seconds: int) -> LeaseRecord:
        """Renew exactly the lease version held by a live worker."""
        now = self._clock.now()
        expires = now + timedelta(seconds=ttl_seconds)
        async with self.database.sessions.begin() as session:
            row = await session.get(LeaseORM, lease.run_id)
            if (
                row is None
                or row.owner_id != lease.owner_id
                or row.fencing_token != lease.fencing_token
                or row.version != lease.version
                or self._aware(row.expires_at) <= now
            ):
                raise ConcurrencyConflictError("lease expired or ownership changed before renewal")
            result = await session.execute(
                update(LeaseORM)
                .where(
                    LeaseORM.run_id == lease.run_id,
                    LeaseORM.owner_id == lease.owner_id,
                    LeaseORM.fencing_token == lease.fencing_token,
                    LeaseORM.version == lease.version,
                )
                .values(
                    expires_at=expires,
                    version=lease.version + 1,
                    renewed_at=now,
                )
            )
            if result.rowcount != 1:
                raise ConcurrencyConflictError("lease changed during renewal")
        return lease.model_copy(
            update={"expires_at": expires, "version": lease.version + 1, "renewed_at": now}
        )

    async def release_lease(self, lease: LeaseRecord) -> None:
        async with self.database.sessions.begin() as session:
            result = await session.execute(
                delete(LeaseORM).where(
                    LeaseORM.run_id == lease.run_id,
                    LeaseORM.owner_id == lease.owner_id,
                    LeaseORM.fencing_token == lease.fencing_token,
                    LeaseORM.version == lease.version,
                )
            )
            if result.rowcount != 1:
                raise ConcurrencyConflictError("lease ownership changed before release")

    async def invalidate_summaries_for_run(self, run_id: str, *, reason: str) -> int:
        """Invalidate context summaries after primary repository/source drift."""
        async with self.database.sessions.begin() as session:
            rows = (
                await session.scalars(
                    select(SummaryORM).where(
                        SummaryORM.run_id == run_id, SummaryORM.valid.is_(True)
                    )
                )
            ).all()
            for row in rows:
                payload = dict(row.payload)
                payload["valid"] = False
                payload["invalidated_reason"] = reason
                row.payload = payload
                row.valid = False
            return len(rows)

    async def save_context(
        self,
        snapshot: ContextSnapshot,
        *,
        items: Sequence[ContextItem],
        summary: SummaryRecord | None = None,
    ) -> None:
        """Persist selected raw items, the selection manifest, and optional compression."""
        payload = {
            "snapshot": snapshot.model_dump(mode="json"),
            "items": [item.model_dump(mode="json") for item in items],
        }
        async with self.database.sessions.begin() as session:
            session.add(
                ContextORM(
                    context_id=snapshot.context_id,
                    run_id=snapshot.run_id,
                    task_id=snapshot.task_id,
                    payload=payload,
                    created_at=snapshot.created_at,
                )
            )
            if summary is not None:
                session.add(
                    SummaryORM(
                        summary_id=summary.summary_id,
                        run_id=summary.run_id,
                        level=summary.level.value,
                        payload=summary.model_dump(mode="json"),
                        valid=summary.valid,
                        source_manifest_hash=sha256_digest(canonical_json(summary.source_hashes)),
                        created_at=summary.created_at,
                    )
                )

    async def latest_context(
        self, run_id: str
    ) -> tuple[ContextSnapshot, tuple[ContextItem, ...], SummaryRecord | None] | None:
        async with self.database.sessions() as session:
            row = await session.scalar(
                select(ContextORM)
                .where(ContextORM.run_id == run_id)
                .order_by(ContextORM.created_at.desc(), ContextORM.context_id.desc())
                .limit(1)
            )
            if row is None:
                return None
            snapshot = ContextSnapshot.model_validate(row.payload["snapshot"])
            items = tuple(ContextItem.model_validate(item) for item in row.payload["items"])
            summary = None
            if snapshot.summary_ids:
                summary_row = await session.get(SummaryORM, snapshot.summary_ids[-1])
                if summary_row is not None:
                    summary = SummaryRecord.model_validate(summary_row.payload)
            return snapshot, items, summary

    async def list_context_references(self, run_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return all context snapshots and currently valid summaries for resume."""
        async with self.database.sessions() as session:
            contexts = tuple(
                (
                    await session.scalars(
                        select(ContextORM.context_id)
                        .where(ContextORM.run_id == run_id)
                        .order_by(ContextORM.created_at, ContextORM.context_id)
                    )
                ).all()
            )
            summaries = tuple(
                (
                    await session.scalars(
                        select(SummaryORM.summary_id)
                        .where(SummaryORM.run_id == run_id, SummaryORM.valid.is_(True))
                        .order_by(SummaryORM.created_at, SummaryORM.summary_id)
                    )
                ).all()
            )
            return contexts, summaries

    async def save_report(
        self,
        *,
        report_id: str,
        run_id: str,
        format: str,
        content: bytes,
        content_hash: str,
        partial: bool,
    ) -> None:
        async with self.database.sessions.begin() as session:
            session.add(
                ReportORM(
                    report_id=f"{report_id}-{format}",
                    run_id=run_id,
                    format=format,
                    content=content,
                    content_hash=content_hash,
                    partial=partial,
                    created_at=self._clock.now(),
                )
            )

    async def get_report(self, run_id: str, *, format: str) -> tuple[bytes, str, bool]:
        async with self.database.sessions() as session:
            row = await session.scalar(
                select(ReportORM)
                .where(ReportORM.run_id == run_id, ReportORM.format == format)
                .order_by(ReportORM.created_at.desc(), ReportORM.report_id.desc())
                .limit(1)
            )
            if row is None:
                raise NotFoundError(f"{format} report not found for run: {run_id}")
            if sha256_digest(row.content) != row.content_hash:
                raise DatabaseError("stored report content hash mismatch")
            return row.content, row.content_hash, row.partial

    @staticmethod
    def _idempotency_pk(owner: str, action: str, key: str) -> str:
        return sha256_digest(f"{owner}:{action}:{key}")

    @staticmethod
    def _task_pk(run_id: str, plan_id: str, task_id: str) -> str:
        return f"{run_id}:{plan_id}:{task_id}"

    @staticmethod
    def _run_to_row(run: RunRecord) -> RunORM:
        return RunORM(
            run_id=run.run_id,
            owner_id=run.owner_id,
            objective=run.objective,
            state=run.state.value,
            active_plan_id=run.active_plan_id,
            active_task_id=run.active_task_id,
            repository_root=run.repository_root,
            repository_snapshot_id=run.repository_snapshot_id,
            configuration_fingerprint=run.configuration_fingerprint,
            version=run.version,
            created_at=run.created_at,
            updated_at=run.updated_at,
            finished_at=run.finished_at,
        )

    @classmethod
    def _run_from_row(cls, row: RunORM) -> RunRecord:
        return RunRecord(
            run_id=row.run_id,
            owner_id=row.owner_id,
            objective=row.objective,
            state=RunState(row.state),
            active_plan_id=row.active_plan_id,
            active_task_id=row.active_task_id,
            repository_root=row.repository_root,
            repository_snapshot_id=row.repository_snapshot_id,
            configuration_fingerprint=row.configuration_fingerprint,
            version=row.version,
            created_at=cls._aware(row.created_at),
            updated_at=cls._aware(row.updated_at),
            finished_at=cls._aware(row.finished_at) if row.finished_at else None,
        )

    @classmethod
    def _task_to_row(cls, task: TaskRecord) -> TaskORM:
        return TaskORM(
            task_pk=cls._task_pk(task.run_id, task.plan_id, task.spec.task_id),
            task_id=task.spec.task_id,
            run_id=task.run_id,
            plan_id=task.plan_id,
            spec=task.spec.model_dump(mode="json"),
            state=task.state.value,
            attempt_count=task.attempt_count,
            version=task.version,
            last_error_id=task.last_error_id,
            started_at=task.started_at,
            finished_at=task.finished_at,
        )

    @classmethod
    def _task_from_row(cls, row: TaskORM) -> TaskRecord:
        return TaskRecord(
            run_id=row.run_id,
            plan_id=row.plan_id,
            spec=row.spec,
            state=TaskState(row.state),
            attempt_count=row.attempt_count,
            version=row.version,
            last_error_id=row.last_error_id,
            started_at=cls._aware(row.started_at) if row.started_at else None,
            finished_at=cls._aware(row.finished_at) if row.finished_at else None,
        )

    @classmethod
    def _evidence_from_row(cls, row: EvidenceORM) -> EvidenceRecord:
        return EvidenceRecord(
            evidence_id=row.evidence_id,
            run_id=row.run_id,
            evidence_type=row.evidence_type,
            source=row.source,
            source_location=row.source_location,
            content_hash=row.content_hash,
            snapshot_id=row.snapshot_id,
            related_task_id=row.related_task_id,
            reliability=row.reliability,
            excerpt=row.excerpt,
            verification_status=row.verification_status,
            metadata=row.metadata_json,
            created_at=cls._aware(row.created_at),
        )

    @classmethod
    def _tool_call_from_row(cls, row: ToolCallORM) -> ToolCall:
        return ToolCall(
            tool_call_id=row.tool_call_id,
            run_id=row.run_id,
            task_id=row.task_id,
            tool_name=row.tool_name,
            arguments=row.arguments,
            arguments_hash=row.arguments_hash,
            idempotency_key=row.idempotency_key,
            side_effect_class=row.side_effect_class,
            status=row.status,
            attempt=row.attempt,
            created_at=cls._aware(row.created_at),
            started_at=cls._aware(row.started_at) if row.started_at else None,
            finished_at=cls._aware(row.finished_at) if row.finished_at else None,
        )

    @classmethod
    def _tool_result_from_row(cls, row: ToolResultORM) -> ToolResult:
        return ToolResult(
            tool_result_id=row.tool_result_id,
            tool_call_id=row.tool_call_id,
            success=row.success,
            output=row.output,
            output_hash=row.output_hash,
            exit_code=row.exit_code,
            truncated=row.truncated,
            duration_seconds=row.duration_seconds,
            created_at=cls._aware(row.created_at),
        )

    @classmethod
    def _repository_file_from_row(cls, row: RepositoryFileORM) -> RepositoryFile:
        return RepositoryFile(
            file_id=row.file_id,
            snapshot_id=row.snapshot_id,
            relative_path=row.relative_path,
            content_hash=row.content_hash,
            size_bytes=row.size_bytes,
            media_type=row.media_type,
            language=row.language,
            change_kind=row.change_kind,
            is_deleted=row.is_deleted,
            indexed_at=cls._aware(row.indexed_at),
        )

    @staticmethod
    def _repository_chunk_from_row(row: RepositoryChunkORM) -> RepositoryChunk:
        return RepositoryChunk(
            chunk_id=row.chunk_id,
            file_id=row.file_id,
            snapshot_id=row.snapshot_id,
            relative_path=row.relative_path,
            content=row.content,
            content_hash=row.content_hash,
            start_line=row.start_line,
            end_line=row.end_line,
            symbol_name=row.symbol_name,
            language=row.language,
            imports=tuple(row.imports),
        )

    @classmethod
    def _lifecycle_from_row(
        cls, row: PauseRequestORM | CancellationRequestORM, kind: str
    ) -> LifecycleRequest:
        return LifecycleRequest(
            request_id=row.request_id,
            run_id=row.run_id,
            kind=kind,
            reason=row.reason,
            requested_by=row.requested_by,
            status=row.status,
            created_at=cls._aware(row.created_at),
            applied_at=cls._aware(row.applied_at) if row.applied_at else None,
        )

    @staticmethod
    def _lease_to_row(lease: LeaseRecord) -> LeaseORM:
        return LeaseORM(**lease.model_dump())

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
