"""Explicit dependency assembly for local, API, tests, and demonstrations."""

from __future__ import annotations

from dataclasses import dataclass

from durable_agent.application.demo_worker import SampleRetryCodingWorker
from durable_agent.application.service import AgentService
from durable_agent.checkpoints import CheckpointManager
from durable_agent.configuration import Settings
from durable_agent.context import ContextBudget, ContextManager
from durable_agent.domain.protocols import Planner, SearchProvider
from durable_agent.evidence import EvidenceLedger
from durable_agent.orchestration import AgentOrchestrator, TaskWorker
from durable_agent.persistence import Database, SqlStore
from durable_agent.persistence.artifacts import LocalArtifactStore
from durable_agent.planning import RuleBasedPlanner
from durable_agent.providers.fakes import FailureInjector
from durable_agent.providers.system import DisabledSearchProvider, SystemClock, UUIDIdentifiers
from durable_agent.recovery import RecoveryManager
from durable_agent.reporting import ReportGenerator
from durable_agent.repository import LocalRepositoryIndexer
from durable_agent.tools import ToolExecutor, ToolPolicy, ToolRegistry
from durable_agent.tools.filesystem import (
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


@dataclass(frozen=True)
class ApplicationContainer:
    settings: Settings
    database: Database
    store: SqlStore
    tool_registry: ToolRegistry
    service: AgentService

    async def close(self) -> None:
        await self.database.dispose()


async def build_application(
    settings: Settings | None = None,
    *,
    create_schema_for_tests: bool = False,
    failure_injector: FailureInjector | None = None,
    planner: Planner | None = None,
    worker: TaskWorker | None = None,
    research_provider: SearchProvider | None = None,
) -> ApplicationContainer:
    """Assemble all adapters without hidden global state."""
    settings = settings or Settings()
    settings.prepare_directories()
    # SQLite's synchronous driver avoids a managed-host aiosqlite shutdown defect while
    # retaining the async use-case contract. Production PostgreSQL uses the async URL.
    database_url = (
        settings.sync_database_url
        if settings.database_url.startswith("sqlite+aiosqlite")
        else settings.database_url
    )
    database = Database(database_url)
    if create_schema_for_tests:
        await database.create_schema_for_tests()
    clock = SystemClock()
    identifiers = UUIDIdentifiers()
    store = SqlStore(database, identifiers=identifiers, clock=clock)
    indexer = LocalRepositoryIndexer(
        identifiers=identifiers,
        maximum_file_bytes=settings.maximum_file_bytes,
        maximum_repository_bytes=settings.maximum_repository_bytes,
        exclusions=settings.indexing_exclusions,
    )
    artifact_store = LocalArtifactStore(settings.artifact_directory)
    registry = ToolRegistry(
        (
            RepositoryFileReaderTool(
                settings.repository_root, maximum_bytes=settings.maximum_file_bytes
            ),
            DocumentRetrieverTool(
                settings.repository_root, maximum_bytes=settings.maximum_file_bytes
            ),
            ControlledFileWriterTool(
                settings.repository_root, maximum_bytes=settings.maximum_file_bytes
            ),
            PatchApplicationTool(
                settings.repository_root, maximum_bytes=settings.maximum_file_bytes
            ),
            RepositoryIndexTool(
                root=settings.repository_root,
                indexer=indexer,
                indexes=store,
                identifiers=identifiers,
                clock=clock,
            ),
            RepositorySearchTool(store),
            ResearchSearchTool(research_provider or DisabledSearchProvider()),
            ShellCommandRunnerTool(
                settings.repository_root,
                allowed_commands=settings.allowed_commands,
                allowed_environment_variables=settings.allowed_environment_variables,
                timeout_seconds=settings.tool_timeout_seconds,
                maximum_output_bytes=settings.maximum_tool_output_bytes,
            ),
            TestRunnerTool(
                settings.repository_root,
                allowed_commands=settings.allowed_commands,
                allowed_environment_variables=settings.allowed_environment_variables,
                timeout_seconds=settings.tool_timeout_seconds,
                maximum_output_bytes=settings.maximum_tool_output_bytes,
            ),
            ArtifactRecorderTool(artifact_store),
        )
    )
    allowed = {"repository.read", "artifact.write"}
    if settings.allow_file_writes:
        allowed.add("repository.write")
    if settings.allow_patch:
        allowed.add("repository.patch")
    if settings.allow_shell:
        allowed.add("process.execute")
    if settings.network_access:
        allowed.add("network.research")
    tools = ToolExecutor(
        registry=registry,
        journal=store,
        policy=ToolPolicy(
            allowed_permissions=frozenset(allowed),
            maximum_output_bytes=settings.maximum_tool_output_bytes,
        ),
        identifiers=identifiers,
        clock=clock,
    )
    evidence = EvidenceLedger(store=store, identifiers=identifiers, clock=clock)
    checkpoints = CheckpointManager(
        store=store,
        identifiers=identifiers,
        clock=clock,
        retention_count=settings.checkpoint_retention_count,
    )
    context = ContextManager(
        budget=ContextBudget(
            context_limit=settings.model_context_limit,
            reserved_output=settings.reserved_output_tokens,
            system_instructions=settings.system_instruction_tokens,
            user_request=settings.user_request_tokens,
            compression_threshold=settings.context_compression_threshold,
        ),
        identifiers=identifiers,
        clock=clock,
    )
    planner_adapter = planner or RuleBasedPlanner(identifiers)
    worker_adapter = worker or SampleRetryCodingWorker(
        root=settings.repository_root,
        store=store,
        tools=tools,
        evidence=evidence,
        failures=failure_injector,
    )
    reports = ReportGenerator(identifiers=identifiers, clock=clock)
    orchestrator = AgentOrchestrator(
        settings=settings,
        store=store,
        worker=worker_adapter,
        checkpoints=checkpoints,
        context=context,
        reports=reports,
        clock=clock,
    )
    recovery = RecoveryManager(
        settings=settings,
        store=store,
        checkpoints=checkpoints,
        indexer=indexer,
        tools=tools,
        clock=clock,
        planner=planner_adapter,
    )
    service = AgentService(
        settings=settings,
        store=store,
        planner=planner_adapter,
        indexer=indexer,
        checkpoints=checkpoints,
        orchestrator=orchestrator,
        recovery=recovery,
        evidence=evidence,
        identifiers=identifiers,
        clock=clock,
        artifacts=artifact_store,
    )
    return ApplicationContainer(
        settings=settings,
        database=database,
        store=store,
        tool_registry=registry,
        service=service,
    )
