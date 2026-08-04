from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from durable_agent.domain.base import sha256_digest
from durable_agent.domain.enums import SideEffectClass, ToolCallStatus
from durable_agent.domain.errors import (
    ManualReviewRequiredError,
    NotFoundError,
    ProviderRetryableError,
    SecurityPolicyError,
    ToolExecutionError,
)
from durable_agent.domain.models import (
    RepositoryChunk,
    RetrievalItem,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from durable_agent.persistence.artifacts import LocalArtifactStore
from durable_agent.providers.fakes import (
    DeterministicClock,
    DeterministicIdentifiers,
    FakeSearchProvider,
)
from durable_agent.repository import LocalRepositoryIndexer
from durable_agent.repository.models import RepositoryIndex
from durable_agent.tools.executor import ToolExecutor, ToolPolicy
from durable_agent.tools.filesystem import (
    ControlledFileWriterTool,
    DocumentRetrieverTool,
    PatchApplicationTool,
    RepositoryFileReaderTool,
)
from durable_agent.tools.journal import InMemoryToolJournal
from durable_agent.tools.registry import ToolRegistry
from durable_agent.tools.search import (
    ArtifactRecorderTool,
    RepositoryIndexTool,
    RepositorySearchTool,
    ResearchSearchTool,
)


class InMemoryRepositoryIndexes:
    def __init__(self, index: RepositoryIndex) -> None:
        self._index = index

    async def get_repository_index(self, snapshot_id: str) -> RepositoryIndex:
        assert snapshot_id == self._index.snapshot.snapshot_id
        return self._index


class MutableRepositoryIndexes:
    def __init__(self) -> None:
        self.indexes: dict[str, RepositoryIndex] = {}

    async def get_repository_index(self, snapshot_id: str) -> RepositoryIndex:
        try:
            return self.indexes[snapshot_id]
        except KeyError as exc:
            raise NotFoundError(f"missing snapshot: {snapshot_id}") from exc

    async def save_repository_index(self, index: RepositoryIndex) -> None:
        self.indexes[index.snapshot.snapshot_id] = index


class EchoTool:
    def __init__(self, *, side_effect: SideEffectClass = SideEffectClass.NONE) -> None:
        self.calls = 0
        self._side_effect = side_effect

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="test.echo",
            description="echo test input",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            output_schema={"type": "object", "required": ["value"]},
            timeout_seconds=1,
            required_permissions=frozenset({"test.echo"}),
            side_effect_class=self._side_effect,
            retry_safe=self._side_effect != SideEffectClass.NON_IDEMPOTENT,
            produces_evidence=True,
        )

    async def execute(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls += 1
        return {"value": arguments["value"]}

    async def reconcile(self, call: ToolCall) -> ToolResult | None:
        del call
        return None


class RetryOnceTool(EchoTool):
    async def execute(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls += 1
        if self.calls == 1:
            raise ProviderRetryableError("transient provider failure")
        return {"value": arguments["value"]}


def executor(
    tool: EchoTool, journal: InMemoryToolJournal, *, output: int = 1_000_000
) -> ToolExecutor:
    return ToolExecutor(
        registry=ToolRegistry([tool]),
        journal=journal,
        policy=ToolPolicy(
            allowed_permissions=frozenset({"test.echo"}), maximum_output_bytes=output
        ),
        identifiers=DeterministicIdentifiers(),
        clock=DeterministicClock(),
    )


@pytest.mark.asyncio
async def test_tool_intent_result_and_idempotent_replay() -> None:
    tool = EchoTool()
    journal = InMemoryToolJournal()
    runner = executor(tool, journal)
    first = await runner.execute(
        run_id="run",
        task_id="task",
        tool_name="test.echo",
        arguments={"value": "x"},
        idempotency_key="key",
    )
    second = await runner.execute(
        run_id="run",
        task_id="task",
        tool_name="test.echo",
        arguments={"value": "x"},
        idempotency_key="key",
    )
    assert first == second
    assert tool.calls == 1
    assert next(iter(journal.calls.values())).status == ToolCallStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_idempotency_mismatch_and_uncertain_window() -> None:
    tool = EchoTool()
    journal = InMemoryToolJournal()
    runner = executor(tool, journal)
    call = ToolCall(
        tool_call_id="call",
        run_id="run",
        task_id="task",
        tool_name="test.echo",
        arguments={"value": "x"},
        arguments_hash=sha256_digest(b'{"value":"x"}'),
        idempotency_key="key",
        side_effect_class=SideEffectClass.NONE,
    )
    await journal.record_intent(call)
    with pytest.raises(ManualReviewRequiredError, match="uncertain"):
        await runner.execute(
            run_id="run",
            task_id="task",
            tool_name="test.echo",
            arguments={"value": "x"},
            idempotency_key="key",
        )
    with pytest.raises(SecurityPolicyError, match="different input"):
        await runner.execute(
            run_id="run",
            task_id="task",
            tool_name="test.echo",
            arguments={"value": "y"},
            idempotency_key="key",
        )


@pytest.mark.asyncio
async def test_retry_safe_failed_intent_reuses_idempotency_record() -> None:
    tool = RetryOnceTool()
    journal = InMemoryToolJournal()
    runner = executor(tool, journal)
    with pytest.raises(ProviderRetryableError, match="transient"):
        await runner.execute(
            run_id="run",
            task_id="task",
            tool_name="test.echo",
            arguments={"value": "x"},
            idempotency_key="retry-key",
        )
    result = await runner.execute(
        run_id="run",
        task_id="task",
        tool_name="test.echo",
        arguments={"value": "x"},
        idempotency_key="retry-key",
    )
    assert result.output == {"value": "x"}
    assert tool.calls == 2
    call = next(iter(journal.calls.values()))
    assert call.attempt == 2
    assert call.status == ToolCallStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_schema_permissions_approval_and_output_bound() -> None:
    tool = EchoTool()
    with pytest.raises(SecurityPolicyError, match="denied"):
        await ToolExecutor(
            registry=ToolRegistry([tool]),
            journal=InMemoryToolJournal(),
            policy=ToolPolicy(),
            identifiers=DeterministicIdentifiers(),
            clock=DeterministicClock(),
        ).execute(
            run_id="run",
            task_id="task",
            tool_name="test.echo",
            arguments={"value": "x"},
            idempotency_key="key",
        )
    runner = executor(tool, InMemoryToolJournal(), output=1_024)
    with pytest.raises(ToolExecutionError, match="input validation"):
        await runner.execute(
            run_id="run",
            task_id="task",
            tool_name="test.echo",
            arguments={"wrong": "x"},
            idempotency_key="bad",
        )
    result = await runner.execute(
        run_id="run",
        task_id="task",
        tool_name="test.echo",
        arguments={"value": "x" * 2_000},
        idempotency_key="large",
    )
    assert result.truncated

    dangerous = EchoTool(side_effect=SideEffectClass.NON_IDEMPOTENT)
    denied = ToolExecutor(
        registry=ToolRegistry([dangerous]),
        journal=InMemoryToolJournal(),
        policy=ToolPolicy(allowed_permissions=frozenset({"test.echo"})),
        identifiers=DeterministicIdentifiers(),
        clock=DeterministicClock(),
    )
    with pytest.raises(SecurityPolicyError, match="explicit approval"):
        await denied.execute(
            run_id="run",
            task_id="task",
            tool_name="test.echo",
            arguments={"value": "x"},
            idempotency_key="danger",
        )


@pytest.mark.asyncio
async def test_atomic_writer_reader_reconcile_and_artifact(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "file.txt"
    target.write_text("old")
    reader = RepositoryFileReaderTool(root)
    before = await reader.execute({"path": "file.txt"})
    writer = ControlledFileWriterTool(root)
    result = await writer.execute(
        {"path": "file.txt", "content": "new", "expected_hash": before["content_hash"]}
    )
    assert target.read_text() == "new"
    assert result["content_hash"] == sha256_digest("new")
    with pytest.raises(ToolExecutionError, match="changed"):
        await writer.execute({"path": "file.txt", "content": "again", "expected_hash": "0" * 64})

    call = ToolCall(
        tool_call_id="call",
        run_id="run",
        task_id="task",
        tool_name="repository.write",
        arguments={"path": "file.txt", "content": "new", "expected_hash": before["content_hash"]},
        arguments_hash="a" * 64,
        idempotency_key="key",
        side_effect_class=SideEffectClass.IDEMPOTENT,
    )
    assert await writer.reconcile(call) is not None

    store = LocalArtifactStore(tmp_path / "artifacts")
    artifact = await ArtifactRecorderTool(store).execute(
        {"artifact_id": "report", "content": "hello", "media_type": "text/plain"}
    )
    assert await store.get("report") == b"hello"
    assert artifact["content_hash"] == sha256_digest("hello")


@pytest.mark.asyncio
async def test_exact_patch_and_untrusted_document_retrieval(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "guide.md"
    before = "retry_limit: 3\n"
    after = "retry_limit: 5\n"
    target.write_text(before)
    patcher = PatchApplicationTool(root)
    result = await patcher.execute(
        {
            "path": "guide.md",
            "old": "3",
            "new": "5",
            "expected_hash": sha256_digest(before),
            "expected_result_hash": sha256_digest(after),
        }
    )
    assert result["replacements"] == 1
    assert target.read_text() == after
    replay = await patcher.execute(
        {
            "path": "guide.md",
            "old": "3",
            "new": "5",
            "expected_hash": sha256_digest(before),
            "expected_result_hash": sha256_digest(after),
        }
    )
    assert replay["replacements"] == 0
    document = await DocumentRetrieverTool(root).execute({"path": "guide.md"})
    assert document["untrusted"] is True
    assert document["content_hash"] == sha256_digest(after)


@pytest.mark.asyncio
async def test_filesystem_tools_reject_limits_and_reconcile_patch(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "file.txt"
    target.write_text("one one")
    with pytest.raises(ToolExecutionError, match="size limit"):
        await RepositoryFileReaderTool(root, maximum_bytes=2).execute({"path": "file.txt"})
    with pytest.raises(ToolExecutionError, match="size limit"):
        await ControlledFileWriterTool(root, maximum_bytes=2).execute(
            {"path": "new.txt", "content": "large", "expected_hash": None}
        )
    writer = ControlledFileWriterTool(root)
    with pytest.raises(ToolExecutionError, match="missing file"):
        await writer.execute({"path": "missing.txt", "content": "x", "expected_hash": "a" * 64})
    created = await writer.execute(
        {"path": "created.txt", "content": "created", "expected_hash": None}
    )
    assert created["bytes"] == 7
    patcher = PatchApplicationTool(root)
    with pytest.raises(ToolExecutionError, match="exactly once"):
        await patcher.execute(
            {
                "path": "file.txt",
                "old": "one",
                "new": "two",
                "expected_hash": sha256_digest("one one"),
                "expected_result_hash": sha256_digest("two one"),
            }
        )
    target.write_text("one")
    call = ToolCall(
        tool_call_id="patch-call",
        run_id="run",
        task_id="task",
        tool_name="repository.patch",
        arguments={
            "path": "file.txt",
            "old": "one",
            "new": "two",
            "expected_hash": sha256_digest("one"),
            "expected_result_hash": sha256_digest("two"),
        },
        arguments_hash="a" * 64,
        idempotency_key="patch",
        side_effect_class=SideEffectClass.IDEMPOTENT,
    )
    assert await patcher.reconcile(call) is None
    target.write_text("two")
    assert await patcher.reconcile(call) is not None


@pytest.mark.asyncio
async def test_repository_and_research_search_tool_adapters() -> None:
    chunk = RepositoryChunk(
        chunk_id="chunk",
        file_id="file",
        snapshot_id="snapshot",
        relative_path="service.py",
        content="configurable retry limit",
        content_hash=sha256_digest("configurable retry limit"),
        start_line=1,
        end_line=1,
    )
    index = RepositoryIndex(
        snapshot={
            "snapshot_id": "snapshot",
            "root": "/repo",
            "manifest_hash": "a" * 64,
            "file_count": 1,
            "total_bytes": len(chunk.content),
            "files": (),
        },
        chunks=(chunk,),
        summaries=(),
        repository_map="service.py",
    )
    repository_tool = RepositorySearchTool(InMemoryRepositoryIndexes(index))
    repository_result = await repository_tool.execute(
        {"snapshot_id": "snapshot", "query": "retry", "limit": 2}
    )
    assert repository_result["snapshot_id"] == "snapshot"
    assert repository_result["manifest_hash"] == "a" * 64
    assert repository_result["items"][0]["source"] == "service.py"
    external = RetrievalItem(
        item_id="source",
        source_type="external",
        source="fixture://source",
        source_location="fixture://source",
        content="retry limit is five",
        content_hash=sha256_digest("retry limit is five"),
        score=1,
    )
    research_tool = ResearchSearchTool(FakeSearchProvider((external,)))
    research_result = await research_tool.execute({"query": "retry", "limit": 1})
    assert research_result["untrusted"] is True
    assert research_result["items"][0]["item_id"] == "source"


@pytest.mark.asyncio
async def test_repository_index_tool_is_named_idempotent_and_reconcilable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "service.py").write_text("retry_limit = 3\n")
    ids = DeterministicIdentifiers()
    clock = DeterministicClock()
    indexes = MutableRepositoryIndexes()
    tool = RepositoryIndexTool(
        root=root,
        indexer=LocalRepositoryIndexer(identifiers=ids),
        indexes=indexes,
        identifiers=ids,
        clock=clock,
    )
    arguments = {"snapshot_id": "snapshot-fixed", "previous_snapshot_id": None}
    first = await tool.execute(arguments)
    second = await tool.execute(arguments)
    assert first == second
    assert first["snapshot_id"] == "snapshot-fixed"
    call = ToolCall(
        tool_call_id="call-index",
        run_id="run",
        task_id="task",
        tool_name="repository.index",
        arguments=arguments,
        arguments_hash="a" * 64,
        idempotency_key="index-key",
        side_effect_class=SideEffectClass.IDEMPOTENT,
    )
    reconciled = await tool.reconcile(call)
    assert reconciled is not None
    assert reconciled.output["manifest_hash"] == first["manifest_hash"]
