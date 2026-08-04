"""Dependency-inverted ports for providers and infrastructure."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from durable_agent.domain.checkpoint import CheckpointEnvelope
from durable_agent.domain.evidence import Claim, EvidenceRecord
from durable_agent.domain.models import (
    PlanSpec,
    RepositorySnapshot,
    RetrievalItem,
    RunRecord,
    TaskRecord,
    TaskSpec,
    ToolCall,
    ToolDefinition,
    ToolResult,
)

SchemaT = TypeVar("SchemaT", bound=BaseModel)


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


@runtime_checkable
class IdentifierGenerator(Protocol):
    def new(self, prefix: str) -> str: ...


class LLMCompletion(Protocol):
    async def complete_structured(
        self,
        *,
        instructions: str,
        untrusted_content: str,
        output_schema: type[SchemaT],
    ) -> SchemaT: ...


class EmbeddingProvider(Protocol):
    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


class SearchProvider(Protocol):
    async def search(self, query: str, *, limit: int) -> Sequence[RetrievalItem]: ...


class Planner(Protocol):
    async def plan(self, *, run_id: str, objective: str) -> PlanSpec: ...

    async def revise(
        self,
        plan: PlanSpec,
        *,
        reason: str,
        tasks: tuple[TaskSpec, ...] | None = None,
    ) -> PlanSpec: ...


class RepositoryIndexer(Protocol):
    async def index(
        self, root: Path, *, previous_snapshot_id: str | None = None
    ) -> RepositorySnapshot: ...

    async def search(
        self, snapshot_id: str, query: str, *, limit: int
    ) -> Sequence[RetrievalItem]: ...


class Tool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    async def execute(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]: ...

    async def reconcile(self, call: ToolCall) -> ToolResult | None: ...


class CheckpointStore(Protocol):
    async def append_checkpoint(
        self, envelope: CheckpointEnvelope, *, expected_sequence: int
    ) -> None: ...

    async def list_checkpoints(self, run_id: str) -> Sequence[CheckpointEnvelope]: ...


class ArtifactStore(Protocol):
    async def put(self, artifact_id: str, content: bytes, *, media_type: str) -> str: ...

    async def get(self, artifact_id: str, *, expected_hash: str | None = None) -> bytes: ...

    async def prune_orphans(
        self,
        *,
        known_artifact_ids: frozenset[str],
        older_than: datetime,
        dry_run: bool,
    ) -> tuple[str, ...]: ...


class EvidenceStore(Protocol):
    async def add_evidence(self, record: EvidenceRecord) -> None: ...

    async def get_evidence(self, run_id: str) -> Sequence[EvidenceRecord]: ...

    async def add_claim(self, claim: Claim) -> None: ...

    async def get_claims(self, run_id: str) -> Sequence[Claim]: ...


class RunStore(Protocol):
    async def create_run(
        self, run: RunRecord, *, idempotency_key: str, request_hash: str
    ) -> RunRecord: ...

    async def get_run(self, run_id: str) -> RunRecord: ...

    async def list_runs(self, *, owner_id: str | None = None) -> Sequence[RunRecord]: ...

    async def update_run(self, run: RunRecord, *, expected_version: int) -> RunRecord: ...

    async def save_plan(self, plan: PlanSpec) -> None: ...

    async def get_plan(self, run_id: str, *, version: int | None = None) -> PlanSpec: ...

    async def save_tasks(self, tasks: Sequence[TaskRecord]) -> None: ...

    async def get_tasks(
        self, run_id: str, *, plan_id: str | None = None
    ) -> Sequence[TaskRecord]: ...


class EventPublisher(Protocol):
    async def publish(
        self,
        *,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        task_id: str | None = None,
    ) -> str: ...

    async def stream(self, run_id: str) -> AsyncIterator[Mapping[str, Any]]: ...
