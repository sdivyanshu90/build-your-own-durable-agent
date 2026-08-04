"""Repository search, research search, and artifact recording tools."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from durable_agent.domain.base import canonical_json, sha256_digest
from durable_agent.domain.enums import SideEffectClass
from durable_agent.domain.errors import NotFoundError, SecurityPolicyError
from durable_agent.domain.models import ToolCall, ToolDefinition, ToolResult
from durable_agent.domain.protocols import (
    ArtifactStore,
    Clock,
    EmbeddingProvider,
    IdentifierGenerator,
    SearchProvider,
)
from durable_agent.repository import LocalRepositoryIndexer
from durable_agent.repository.models import RepositoryIndex
from durable_agent.retrieval import RetrievalEngine


class RepositoryIndexSource(Protocol):
    """Read immutable repository indexes by their durable snapshot identifier."""

    async def get_repository_index(self, snapshot_id: str) -> RepositoryIndex: ...


class RepositoryIndexStore(RepositoryIndexSource, Protocol):
    """Persist immutable repository indexes."""

    async def save_repository_index(self, index: RepositoryIndex) -> None: ...


class RepositoryIndexTool:
    """Build a named snapshot inside a fixed root with retry reconciliation."""

    def __init__(
        self,
        *,
        root: Path,
        indexer: LocalRepositoryIndexer,
        indexes: RepositoryIndexStore,
        identifiers: IdentifierGenerator,
        clock: Clock,
    ) -> None:
        self._root = root.resolve(strict=True)
        self._indexer = indexer
        self._indexes = indexes
        self._ids = identifiers
        self._clock = clock

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="repository.index",
            description="Create an immutable index for the configured repository root",
            input_schema={
                "type": "object",
                "properties": {
                    "snapshot_id": {"type": "string", "minLength": 1, "maxLength": 256},
                    "previous_snapshot_id": {
                        "type": ["string", "null"],
                        "minLength": 1,
                        "maxLength": 256,
                    },
                },
                "required": ["snapshot_id"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "snapshot_id": {"type": "string"},
                    "manifest_hash": {"type": "string"},
                    "file_count": {"type": "integer"},
                    "total_bytes": {"type": "integer"},
                    "warnings": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "snapshot_id",
                    "manifest_hash",
                    "file_count",
                    "total_bytes",
                    "warnings",
                ],
                "additionalProperties": False,
            },
            timeout_seconds=300,
            required_permissions=frozenset({"repository.read"}),
            side_effect_class=SideEffectClass.IDEMPOTENT,
            retry_safe=True,
            produces_evidence=True,
        )

    async def execute(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        snapshot_id = str(arguments["snapshot_id"])
        try:
            existing = await self._indexes.get_repository_index(snapshot_id)
        except NotFoundError:
            existing = None
        if existing is not None:
            if Path(existing.snapshot.root).resolve() != self._root:
                raise SecurityPolicyError("snapshot ID belongs to a different repository root")
            return self._output(existing)
        previous_id = arguments.get("previous_snapshot_id")
        previous = (
            await self._indexes.get_repository_index(str(previous_id))
            if previous_id is not None
            else None
        )
        index = await self._indexer.index(
            self._root,
            previous=previous,
            snapshot_id=snapshot_id,
        )
        await self._indexes.save_repository_index(index)
        return self._output(index)

    async def reconcile(self, call: ToolCall) -> ToolResult | None:
        snapshot_id = call.arguments.get("snapshot_id")
        if not isinstance(snapshot_id, str):
            return None
        try:
            index = await self._indexes.get_repository_index(snapshot_id)
        except NotFoundError:
            return None
        if Path(index.snapshot.root).resolve() != self._root:
            raise SecurityPolicyError("snapshot ID belongs to a different repository root")
        output = self._output(index)
        return ToolResult(
            tool_result_id=self._ids.new("toolresult"),
            tool_call_id=call.tool_call_id,
            success=True,
            output=output,
            output_hash=sha256_digest(canonical_json(output)),
            duration_seconds=0,
            created_at=self._clock.now(),
        )

    @staticmethod
    def _output(index: RepositoryIndex) -> dict[str, Any]:
        return {
            "snapshot_id": index.snapshot.snapshot_id,
            "manifest_hash": index.snapshot.manifest_hash,
            "file_count": index.snapshot.file_count,
            "total_bytes": index.snapshot.total_bytes,
            "warnings": list(index.warnings),
        }


class RepositorySearchTool:
    """Search one explicitly selected immutable repository snapshot."""

    def __init__(
        self,
        indexes: RepositoryIndexSource,
        *,
        embeddings: EmbeddingProvider | None = None,
    ) -> None:
        self._indexes = indexes
        self._embeddings = embeddings

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="repository.search",
            description="Hybrid-search the current repository snapshot",
            input_schema={
                "type": "object",
                "properties": {
                    "snapshot_id": {"type": "string", "minLength": 1, "maxLength": 256},
                    "query": {"type": "string", "minLength": 1, "maxLength": 10_000},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["snapshot_id", "query"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "snapshot_id": {"type": "string"},
                    "manifest_hash": {"type": "string"},
                    "items": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["snapshot_id", "manifest_hash", "items"],
                "additionalProperties": False,
            },
            timeout_seconds=30,
            required_permissions=frozenset({"repository.read"}),
            side_effect_class=SideEffectClass.NONE,
            retry_safe=True,
            produces_evidence=True,
        )

    async def execute(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        snapshot_id = str(arguments["snapshot_id"])
        index = await self._indexes.get_repository_index(snapshot_id)
        engine = RetrievalEngine(index.chunks, embeddings=self._embeddings)
        results = await engine.hybrid(
            str(arguments["query"]), limit=int(arguments.get("limit", 10))
        )
        return {
            "snapshot_id": index.snapshot.snapshot_id,
            "manifest_hash": index.snapshot.manifest_hash,
            "items": [item.model_dump(mode="json") for item in results],
        }

    async def reconcile(self, call: ToolCall) -> ToolResult | None:
        del call
        return None


class ResearchSearchTool:
    """Expose a provider-neutral search adapter as untrusted data."""

    def __init__(self, provider: SearchProvider) -> None:
        self._provider = provider

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="research.search",
            description="Search an approved research provider and return untrusted results",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 10_000},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "items": {"type": "array", "items": {"type": "object"}},
                    "untrusted": {"const": True},
                },
                "required": ["items", "untrusted"],
                "additionalProperties": False,
            },
            timeout_seconds=30,
            required_permissions=frozenset({"network.research"}),
            side_effect_class=SideEffectClass.NONE,
            retry_safe=True,
            produces_evidence=True,
        )

    async def execute(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        items = await self._provider.search(
            str(arguments["query"]), limit=int(arguments.get("limit", 10))
        )
        limit = int(arguments.get("limit", 10))
        return {
            "items": [item.model_dump(mode="json") for item in items[:limit]],
            "untrusted": True,
        }

    async def reconcile(self, call: ToolCall) -> ToolResult | None:
        del call
        return None


class ArtifactRecorderTool:
    def __init__(self, store: ArtifactStore) -> None:
        self._store = store

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="artifact.record",
            description="Store a bounded generated artifact by stable ID",
            input_schema={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                    "media_type": {"type": "string", "minLength": 1},
                },
                "required": ["artifact_id", "content", "media_type"],
                "additionalProperties": False,
            },
            output_schema={"type": "object", "required": ["artifact_id", "content_hash"]},
            timeout_seconds=30,
            required_permissions=frozenset({"artifact.write"}),
            side_effect_class=SideEffectClass.IDEMPOTENT,
            retry_safe=True,
            produces_evidence=True,
        )

    async def execute(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        artifact_id = str(arguments["artifact_id"])
        digest = await self._store.put(
            artifact_id,
            str(arguments["content"]).encode(),
            media_type=str(arguments["media_type"]),
        )
        return {"artifact_id": artifact_id, "content_hash": digest}

    async def reconcile(self, call: ToolCall) -> ToolResult | None:
        del call
        return None
