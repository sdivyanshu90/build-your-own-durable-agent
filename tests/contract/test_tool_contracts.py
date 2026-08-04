from __future__ import annotations

from pathlib import Path

import pytest

from durable_agent.application.factory import build_application
from durable_agent.configuration import Settings
from durable_agent.domain.base import sha256_digest
from durable_agent.domain.models import RepositoryChunk
from durable_agent.persistence.artifacts import LocalArtifactStore
from durable_agent.planning import RuleBasedPlanner
from durable_agent.providers.fakes import (
    DeterministicClock,
    DeterministicIdentifiers,
    FakeSearchProvider,
)
from durable_agent.repository import LocalRepositoryIndexer
from durable_agent.repository.models import RepositoryIndex
from durable_agent.tools import (
    ControlledFileWriterTool,
    DocumentRetrieverTool,
    PatchApplicationTool,
    RepositoryFileReaderTool,
)
from durable_agent.tools.search import (
    ArtifactRecorderTool,
    RepositoryIndexTool,
    RepositorySearchTool,
    ResearchSearchTool,
)
from durable_agent.tools.subprocess import ShellCommandRunnerTool, TestRunnerTool


class _IndexSource:
    def __init__(self) -> None:
        self.indexes: dict[str, RepositoryIndex] = {}

    async def get_repository_index(self, snapshot_id: str) -> RepositoryIndex:
        chunk = RepositoryChunk(
            chunk_id="chunk",
            file_id="file",
            snapshot_id=snapshot_id,
            relative_path="file.py",
            content="content",
            content_hash=sha256_digest("content"),
            start_line=1,
            end_line=1,
        )
        return self.indexes.get(snapshot_id) or RepositoryIndex(
            snapshot={
                "snapshot_id": snapshot_id,
                "root": "/repo",
                "manifest_hash": "a" * 64,
                "file_count": 1,
                "total_bytes": 7,
            },
            chunks=(chunk,),
            summaries=(),
            repository_map="file.py",
        )

    async def save_repository_index(self, index: RepositoryIndex) -> None:
        self.indexes[index.snapshot.snapshot_id] = index


def test_production_tool_definitions_are_complete_and_unique(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    indexes = _IndexSource()
    ids = DeterministicIdentifiers()
    tools = (
        RepositoryFileReaderTool(root),
        DocumentRetrieverTool(root),
        ControlledFileWriterTool(root),
        PatchApplicationTool(root),
        RepositoryIndexTool(
            root=root,
            indexer=LocalRepositoryIndexer(identifiers=ids),
            indexes=indexes,
            identifiers=ids,
            clock=DeterministicClock(),
        ),
        RepositorySearchTool(indexes),
        ResearchSearchTool(FakeSearchProvider(())),
        ShellCommandRunnerTool(root, allowed_commands=("python3",)),
        TestRunnerTool(root, allowed_commands=("pytest",)),
        ArtifactRecorderTool(LocalArtifactStore(tmp_path / "artifacts")),
    )
    definitions = [tool.definition for tool in tools]
    assert {item.name for item in definitions} == {
        "artifact.record",
        "document.retrieve",
        "repository.index",
        "repository.patch",
        "repository.read",
        "repository.search",
        "repository.write",
        "research.search",
        "shell.run",
        "test.run",
    }
    assert len({item.name for item in definitions}) == len(definitions)
    for definition in definitions:
        assert definition.description
        assert definition.input_schema["type"] == "object"
        assert definition.output_schema["type"] == "object"
        assert definition.timeout_seconds > 0
        assert definition.required_permissions


@pytest.mark.asyncio
async def test_factory_registers_every_tool_declared_by_baseline_plan(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    database_path = tmp_path / "agent.db"
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        sync_database_url=f"sqlite:///{database_path}",
        repository_root=root,
        artifact_directory=tmp_path / "artifacts",
    )
    container = await build_application(settings, create_schema_for_tests=True)
    try:
        plan = await RuleBasedPlanner(DeterministicIdentifiers()).plan(
            run_id="run", objective="Add a configurable retry limit"
        )
        declared = {name for task in plan.tasks for name in task.required_tools}
        registered = {item.name for item in container.tool_registry.definitions()}
        assert declared <= registered
    finally:
        await container.close()
