"""Hash-chained atomic checkpoint management."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import ValidationError

from durable_agent.domain.checkpoint import (
    CheckpointEnvelope,
    CheckpointPayload,
    ErrorCheckpoint,
    ToolCallCheckpoint,
)
from durable_agent.domain.enums import TaskState
from durable_agent.domain.errors import CorruptCheckpointError
from durable_agent.domain.models import PlanSpec, RunRecord, TaskRecord, ToolCall
from durable_agent.domain.protocols import Clock, IdentifierGenerator
from durable_agent.observability import METRICS, span
from durable_agent.persistence.store import SqlStore


class CheckpointManager:
    """Create complete resume records and recover newest valid state."""

    def __init__(
        self,
        *,
        store: SqlStore,
        identifiers: IdentifierGenerator,
        clock: Clock,
        retention_count: int = 100,
    ) -> None:
        if retention_count < 2:
            raise ValueError("at least two checkpoints must be retained")
        self._store = store
        self._ids = identifiers
        self._clock = clock
        self._retention_count = retention_count

    async def write(
        self,
        *,
        run: RunRecord,
        plan: PlanSpec,
        tasks: Sequence[TaskRecord],
        context_ids: Sequence[str] = (),
        summary_ids: Sequence[str] = (),
        tool_calls: Sequence[ToolCall] = (),
        artifact_ids: Sequence[str] = (),
        evidence_ids: Sequence[str] = (),
        error: ErrorCheckpoint | None = None,
        repository_manifest_hash: str | None = None,
    ) -> CheckpointEnvelope:
        """Append an integrity-protected checkpoint after verifying the current tip."""
        if repository_manifest_hash is None and run.repository_snapshot_id is not None:
            repository_index = await self._store.get_repository_index(run.repository_snapshot_id)
            repository_manifest_hash = repository_index.snapshot.manifest_hash
        persisted_contexts, persisted_summaries = await self._store.list_context_references(
            run.run_id
        )
        persisted_tools = await self._store.list_tool_calls(run.run_id)
        persisted_artifacts = await self._store.list_artifact_ids(run.run_id)
        persisted_evidence = tuple(
            item.evidence_id for item in await self._store.get_evidence(run.run_id)
        )
        if error is None:
            error = await self._store.latest_error(run.run_id)
        context_ids = self._merge_ids(persisted_contexts, context_ids)
        summary_ids = self._merge_ids(persisted_summaries, summary_ids)
        artifact_ids = self._merge_ids(persisted_artifacts, artifact_ids)
        evidence_ids = self._merge_ids(persisted_evidence, evidence_ids)
        tool_by_id = {call.tool_call_id: call for call in (*persisted_tools, *tool_calls)}
        tool_calls = tuple(tool_by_id[key] for key in sorted(tool_by_id))
        raw = await self._store.list_checkpoint_json(run.run_id)
        if raw:
            latest = await self.recover_latest(run.run_id)
            expected_sequence = await self._store.checkpoint_tip_sequence(run.run_id)
            parent_hash = latest.chain_hash()
        else:
            expected_sequence = 0
            parent_hash = None
        states = {task.spec.task_id: task.state for task in tasks}
        payload = CheckpointPayload(
            run_id=run.run_id,
            run_state=run.state,
            active_task_id=run.active_task_id,
            task_states=states,
            completed_task_ids=tuple(
                sorted(task_id for task_id, state in states.items() if state == TaskState.SUCCEEDED)
            ),
            pending_task_ids=tuple(
                sorted(task_id for task_id, state in states.items() if state != TaskState.SUCCEEDED)
            ),
            plan_id=plan.plan_id,
            plan_version=plan.version,
            context_ids=tuple(context_ids),
            summary_ids=tuple(summary_ids),
            tool_calls=tuple(
                ToolCallCheckpoint(
                    tool_call_id=call.tool_call_id,
                    idempotency_key=call.idempotency_key,
                    status=call.status,
                    side_effect_class=call.side_effect_class.value,
                )
                for call in tool_calls
            ),
            artifact_ids=tuple(artifact_ids),
            evidence_ids=tuple(evidence_ids),
            retry_counters={task.spec.task_id: task.attempt_count for task in tasks},
            error=error,
            repository_snapshot_id=run.repository_snapshot_id,
            repository_manifest_hash=repository_manifest_hash,
            configuration_fingerprint=run.configuration_fingerprint,
        )
        envelope = CheckpointEnvelope.create(
            checkpoint_id=self._ids.new("checkpoint"),
            sequence=expected_sequence + 1,
            payload=payload,
            parent_hash=parent_hash,
            created_at=self._clock.now(),
        )
        with span(
            "checkpoint.write",
            {"run.id": run.run_id, "checkpoint.sequence": envelope.sequence},
        ):
            await self._store.append_checkpoint(envelope, expected_sequence=expected_sequence)
        METRICS.checkpoint_writes.inc()
        await self._store.publish(
            run_id=run.run_id,
            event_type="checkpoint.written",
            payload={"checkpoint_id": envelope.checkpoint_id, "sequence": envelope.sequence},
            task_id=run.active_task_id,
        )
        await self._store.prune_checkpoints(run.run_id, retain=self._retention_count)
        return envelope

    @staticmethod
    def _merge_ids(persisted: Sequence[str], supplied: Sequence[str]) -> tuple[str, ...]:
        """Merge durable and caller-known references into stable, duplicate-free order."""
        return tuple(dict.fromkeys((*persisted, *supplied)))

    async def recover_latest(self, run_id: str) -> CheckpointEnvelope:
        """Return the newest self- and chain-valid checkpoint, recording each fallback."""
        raw = tuple(await self._store.list_checkpoint_json(run_id))
        if not raw:
            raise CorruptCheckpointError(f"run {run_id} has no checkpoints")
        parsed: list[CheckpointEnvelope | None] = []
        for value in raw:
            try:
                parsed.append(CheckpointEnvelope.model_validate_json(value))
            except ValidationError:
                parsed.append(None)
        failures: list[dict[str, object]] = []
        for index, candidate in enumerate(parsed):
            try:
                if candidate is None:
                    raise CorruptCheckpointError("checkpoint JSON/schema is invalid")
                if not self._chain_is_valid(parsed, index):
                    raise CorruptCheckpointError("checkpoint parent chain is invalid")
            except (CorruptCheckpointError, ValidationError) as exc:
                failures.append(
                    {
                        "position": index,
                        "checkpoint_id": candidate.checkpoint_id if candidate else None,
                        "reason": str(exc),
                    }
                )
                continue
            if failures:
                METRICS.checkpoint_recoveries.inc()
                await self._store.publish(
                    run_id=run_id,
                    event_type="checkpoint.recovered",
                    payload={
                        "selected_checkpoint_id": candidate.checkpoint_id,
                        "selected_sequence": candidate.sequence,
                        "rejected": failures,
                    },
                )
            return candidate
        await self._store.publish(
            run_id=run_id,
            event_type="checkpoint.recovery_failed",
            payload={"rejected": failures},
        )
        raise CorruptCheckpointError(f"run {run_id} has no valid checkpoint")

    @classmethod
    def _chain_is_valid(cls, parsed: Sequence[CheckpointEnvelope | None], index: int) -> bool:
        """Validate a retained chain, skipping corrupt rows but never hashes."""
        candidate = parsed[index]
        if candidate is None:
            return False
        try:
            candidate.verify()
        except CorruptCheckpointError:
            return False
        if index == len(parsed) - 1:
            # Retention may prune ancestors; the oldest retained self-valid row is the
            # local trust anchor for this recovery window.
            return True
        if candidate.parent_hash is None:
            return candidate.sequence == 1
        for older_index in range(index + 1, len(parsed)):
            older = parsed[older_index]
            if older is None:
                continue
            try:
                older.verify()
            except CorruptCheckpointError:
                continue
            if older.sequence >= candidate.sequence:
                continue
            if candidate.parent_hash == older.chain_hash():
                return cls._chain_is_valid(parsed, older_index)
        return False

    @staticmethod
    def inspect(envelope: CheckpointEnvelope) -> dict[str, object]:
        """Return a human-readable, non-opaque checkpoint view."""
        payload = envelope.payload
        return {
            "checkpoint_id": envelope.checkpoint_id,
            "sequence": envelope.sequence,
            "schema_version": envelope.schema_version,
            "created_at": envelope.created_at.isoformat(),
            "run_id": envelope.run_id,
            "run_state": payload.run_state.value,
            "active_task_id": payload.active_task_id,
            "completed_tasks": payload.completed_task_ids,
            "pending_tasks": payload.pending_task_ids,
            "plan": {"id": payload.plan_id, "version": payload.plan_version},
            "repository_snapshot_id": payload.repository_snapshot_id,
            "tool_calls": [item.model_dump(mode="json") for item in payload.tool_calls],
            "evidence_count": len(payload.evidence_ids),
            "payload_hash": envelope.payload_hash,
            "parent_hash": envelope.parent_hash,
        }
