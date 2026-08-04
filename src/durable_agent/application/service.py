"""Use-case facade shared by CLI and HTTP interfaces."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from durable_agent.checkpoints import CheckpointManager
from durable_agent.configuration import Settings
from durable_agent.domain.base import DomainModel, canonical_json, sha256_digest
from durable_agent.domain.checkpoint import CheckpointEnvelope
from durable_agent.domain.enums import RunState
from durable_agent.domain.errors import (
    ConcurrencyConflictError,
    DomainValidationError,
    NotFoundError,
    SecurityPolicyError,
)
from durable_agent.domain.models import LifecycleRequest, RunRecord, TaskRecord
from durable_agent.domain.protocols import ArtifactStore, Clock, IdentifierGenerator, Planner
from durable_agent.domain.state_machine import transition_run
from durable_agent.evidence import EvidenceLedger
from durable_agent.observability import METRICS, span
from durable_agent.orchestration import AgentOrchestrator, RunAdvanceResult
from durable_agent.persistence.store import SqlStore
from durable_agent.recovery import RecoveryManager
from durable_agent.repository import LocalRepositoryIndexer


class RetentionCleanupResult(DomainModel):
    """Auditable preview or result of one idempotent singleton maintenance pass."""

    dry_run: bool
    event_cutoff: datetime
    artifact_cutoff: datetime
    eligible_event_count: int
    deleted_event_count: int
    compacted_run_ids: tuple[str, ...]
    orphan_artifact_ids: tuple[str, ...]
    deleted_orphan_count: int


class AgentService:
    """Authorize ownership and coordinate durable application use cases."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: SqlStore,
        planner: Planner,
        indexer: LocalRepositoryIndexer,
        checkpoints: CheckpointManager,
        orchestrator: AgentOrchestrator,
        recovery: RecoveryManager,
        evidence: EvidenceLedger,
        identifiers: IdentifierGenerator,
        clock: Clock,
        artifacts: ArtifactStore,
    ) -> None:
        self.settings = settings
        self.store = store
        self._planner = planner
        self._indexer = indexer
        self._checkpoints = checkpoints
        self._orchestrator = orchestrator
        self._recovery = recovery
        self._evidence = evidence
        self._ids = identifiers
        self._clock = clock
        self._artifacts = artifacts

    async def index_repository(self, path: Path) -> str:
        """Index a repository and return its immutable snapshot ID."""
        result = await self._indexer.index(path)
        await self.store.save_repository_index(result)
        return result.snapshot.snapshot_id

    async def create_run(
        self,
        *,
        objective: str,
        owner_id: str = "local",
        idempotency_key: str,
        repository_path: Path | None = None,
    ) -> RunRecord:
        """Create, plan, checkpoint, and leave a run ready for execution."""
        objective = objective.strip()
        if not objective:
            raise DomainValidationError("objective must not be blank")
        if not idempotency_key.strip():
            raise DomainValidationError("idempotency key must not be blank")
        repository = (repository_path or self.settings.repository_root).resolve(strict=True)
        if not repository.is_dir():
            raise DomainValidationError("repository path must be a directory")
        configured_root = self.settings.repository_root.resolve(strict=True)
        if repository != configured_root:
            raise SecurityPolicyError("run repository must equal the configured tool sandbox root")
        request_hash = sha256_digest(
            canonical_json(
                {"owner_id": owner_id, "objective": objective, "repository": str(repository)}
            )
        )
        existing = await self.store.find_idempotent_run(
            owner_id=owner_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if existing is not None:
            return existing
        with span("repository.index", {"repository.root": str(repository)}):
            index = await self._indexer.index(repository)
        await self.store.save_repository_index(index)
        now = self._clock.now()
        run = RunRecord(
            run_id=self._ids.new("run"),
            owner_id=owner_id,
            objective=objective,
            repository_root=str(repository),
            repository_snapshot_id=index.snapshot.snapshot_id,
            configuration_fingerprint=self.settings.fingerprint(),
            created_at=now,
            updated_at=now,
        )
        run = await self.store.create_run(
            run, idempotency_key=idempotency_key, request_hash=request_hash
        )
        METRICS.runs_created.inc()
        if run.active_plan_id is not None:
            return run
        await self.store.publish(
            run_id=run.run_id,
            event_type="run.created",
            payload={
                "objective_hash": sha256_digest(objective),
                "snapshot_id": index.snapshot.snapshot_id,
            },
        )
        old_version = run.version
        run = run.model_copy(
            update={
                "state": transition_run(run.state, RunState.PLANNING),
                "updated_at": self._clock.now(),
            }
        )
        run = await self.store.update_run(run, expected_version=old_version)
        with span("planning", {"run.id": run.run_id}):
            plan = await self._planner.plan(run_id=run.run_id, objective=objective)
        await self.store.save_plan(plan)
        tasks = tuple(
            TaskRecord(run_id=run.run_id, plan_id=plan.plan_id, spec=spec) for spec in plan.tasks
        )
        await self.store.save_tasks(tasks)
        old_version = run.version
        run = run.model_copy(
            update={
                "state": transition_run(run.state, RunState.RUNNING),
                "active_plan_id": plan.plan_id,
                "updated_at": self._clock.now(),
            }
        )
        run = await self.store.update_run(run, expected_version=old_version)
        await self._checkpoints.write(
            run=run,
            plan=plan,
            tasks=tasks,
            repository_manifest_hash=index.snapshot.manifest_hash,
        )
        await self.store.publish(
            run_id=run.run_id,
            event_type="plan.created",
            payload={"plan_id": plan.plan_id, "version": plan.version, "task_count": len(tasks)},
        )
        return run

    async def advance(
        self, run_id: str, *, owner_id: str = "local", maximum_tasks: int | None = None
    ) -> RunAdvanceResult:
        await self._owned_run(run_id, owner_id)
        return await self._orchestrator.advance(
            run_id,
            worker_id=self._new_worker_id(owner_id),
            maximum_tasks=maximum_tasks,
        )

    async def pause(
        self,
        run_id: str,
        *,
        owner_id: str = "local",
        reason: str,
        idempotency_key: str,
    ) -> LifecycleRequest:
        run = await self._owned_run(run_id, owner_id)
        if not idempotency_key.strip():
            raise DomainValidationError("idempotency key must not be blank")
        request_hash = sha256_digest(canonical_json({"run_id": run_id, "reason": reason}))
        existing = await self.store.find_idempotent_lifecycle_request(
            owner_id=owner_id,
            run_id=run_id,
            kind="pause",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if existing is not None:
            return existing
        if run.state not in {RunState.RUNNING, RunState.PAUSE_REQUESTED}:
            raise DomainValidationError(f"run in state {run.state.value} cannot be paused")
        request = LifecycleRequest(
            request_id=self._ids.new("pause"),
            run_id=run_id,
            kind="pause",
            reason=reason,
            requested_by=owner_id,
            created_at=self._clock.now(),
        )
        request = await self.store.create_lifecycle_request(
            request, idempotency_key=idempotency_key, request_hash=request_hash
        )
        if run.state == RunState.RUNNING:
            old_version = run.version
            run = run.model_copy(
                update={
                    "state": transition_run(run.state, RunState.PAUSE_REQUESTED),
                    "updated_at": self._clock.now(),
                }
            )
            await self.store.update_run(run, expected_version=old_version)
        await self.store.publish(
            run_id=run_id,
            event_type="pause.requested",
            payload={"request_id": request.request_id, "reason": reason},
        )
        return request

    async def resume(
        self,
        run_id: str,
        *,
        owner_id: str = "local",
        maximum_tasks: int | None = None,
        idempotency_key: str | None = None,
    ) -> RunAdvanceResult:
        run = await self._owned_run(run_id, owner_id)
        replay = False
        if idempotency_key is not None:
            if not idempotency_key.strip():
                raise DomainValidationError("idempotency key must not be blank")
            request_hash = sha256_digest(
                canonical_json({"run_id": run_id, "maximum_tasks": maximum_tasks})
            )
            replay = not await self.store.claim_idempotent_action(
                owner_id=owner_id,
                action=f"resume:{run_id}",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                resource_id=run_id,
            )
            if replay and run.state not in {RunState.PAUSED, RunState.RECOVERING}:
                return RunAdvanceResult(
                    run=run,
                    tasks=tuple(await self.store.get_tasks(run_id)),
                    tasks_executed=0,
                )
        worker_id = self._new_worker_id(owner_id)
        try:
            await self._recovery.resume(run_id, worker_id=worker_id)
            return await self._orchestrator.advance(
                run_id,
                worker_id=worker_id,
                maximum_tasks=maximum_tasks,
                lease_already_held=True,
            )
        except ConcurrencyConflictError:
            if not replay:
                raise
            current = await self._owned_run(run_id, owner_id)
            return RunAdvanceResult(
                run=current,
                tasks=tuple(await self.store.get_tasks(run_id)),
                tasks_executed=0,
            )

    async def cancel(
        self,
        run_id: str,
        *,
        owner_id: str = "local",
        reason: str,
        idempotency_key: str,
    ) -> LifecycleRequest:
        run = await self._owned_run(run_id, owner_id)
        if not idempotency_key.strip():
            raise DomainValidationError("idempotency key must not be blank")
        request_hash = sha256_digest(canonical_json({"run_id": run_id, "reason": reason}))
        existing = await self.store.find_idempotent_lifecycle_request(
            owner_id=owner_id,
            run_id=run_id,
            kind="cancel",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if existing is not None:
            return existing
        if run.state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
            raise DomainValidationError(
                f"terminal run in state {run.state.value} cannot be cancelled"
            )
        request = LifecycleRequest(
            request_id=self._ids.new("cancel"),
            run_id=run_id,
            kind="cancel",
            reason=reason,
            requested_by=owner_id,
            created_at=self._clock.now(),
        )
        request = await self.store.create_lifecycle_request(
            request, idempotency_key=idempotency_key, request_hash=request_hash
        )
        await self.store.publish(
            run_id=run_id,
            event_type="cancellation.requested",
            payload={"request_id": request.request_id, "reason": reason},
        )
        return request

    async def status(self, run_id: str, *, owner_id: str = "local") -> RunRecord:
        return await self._owned_run(run_id, owner_id)

    async def latest_checkpoint(
        self, run_id: str, *, owner_id: str = "local"
    ) -> CheckpointEnvelope:
        """Return the latest valid retained checkpoint, falling back after corruption."""
        await self._owned_run(run_id, owner_id)
        return await self._checkpoints.recover_latest(run_id)

    async def verify(self, run_id: str, *, owner_id: str = "local") -> tuple[str, ...]:
        await self._owned_run(run_id, owner_id)
        messages = list(await self._evidence.verify_run(run_id))
        for artifact in await self.store.get_artifacts(run_id):
            artifact_key = artifact.uri.removeprefix("artifact://") or artifact.artifact_id
            await self._artifacts.get(artifact_key, expected_hash=artifact.content_hash)
            messages.append(
                f"verified artifact {artifact.artifact_id} hash {artifact.content_hash}"
            )
        for format in ("markdown", "json"):
            try:
                _, digest, partial = await self.store.get_report(run_id, format=format)
                messages.append(f"verified {format} report hash {digest} (partial={partial})")
            except NotFoundError:
                messages.append(f"no {format} report exists yet")
        return tuple(messages)

    async def report(
        self, run_id: str, *, owner_id: str = "local", format: str = "markdown"
    ) -> str:
        await self._owned_run(run_id, owner_id)
        if format not in {"markdown", "json"}:
            raise DomainValidationError("report format must be markdown or json")
        content, _, _ = await self.store.get_report(run_id, format=format)
        return content.decode("utf-8")

    async def cleanup_retention(self, *, dry_run: bool = True) -> RetentionCleanupResult:
        """Apply terminal-event and orphan-artifact retention without deleting evidence."""
        event_cutoff = self._clock.now() - timedelta(days=self.settings.event_retention_days)
        artifact_cutoff = self._clock.now() - timedelta(days=self.settings.artifact_retention_days)
        known_artifacts = await self.store.list_all_artifact_ids()
        orphan_ids = await self._artifacts.prune_orphans(
            known_artifact_ids=known_artifacts,
            older_than=artifact_cutoff,
            dry_run=dry_run,
        )
        eligible_events, compacted_runs = await self.store.compact_terminal_events(
            older_than=event_cutoff,
            dry_run=dry_run,
        )
        return RetentionCleanupResult(
            dry_run=dry_run,
            event_cutoff=event_cutoff,
            artifact_cutoff=artifact_cutoff,
            eligible_event_count=eligible_events,
            deleted_event_count=0 if dry_run else eligible_events,
            compacted_run_ids=compacted_runs,
            orphan_artifact_ids=orphan_ids,
            deleted_orphan_count=0 if dry_run else len(orphan_ids),
        )

    async def _owned_run(self, run_id: str, owner_id: str) -> RunRecord:
        run = await self.store.get_run(run_id)
        if run.owner_id != owner_id:
            raise SecurityPolicyError("run is owned by another principal")
        return run

    def _new_worker_id(self, owner_id: str) -> str:
        """Create a process-operation identity; principals are not lease owners."""
        return f"{self._ids.new('worker')}:{owner_id}"
