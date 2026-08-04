"""Offline deterministic coding worker for the bundled retry-limit demonstration."""

from __future__ import annotations

from pathlib import Path

from durable_agent.domain.base import sha256_digest
from durable_agent.domain.enums import ClaimKind, EvidenceType, VerificationStatus
from durable_agent.domain.errors import DomainValidationError, ToolExecutionError
from durable_agent.domain.models import RunRecord, TaskRecord
from durable_agent.evidence import EvidenceLedger
from durable_agent.orchestration.worker import TaskOutcome
from durable_agent.persistence.store import SqlStore
from durable_agent.providers.fakes import FailureInjector
from durable_agent.tools import ToolExecutor


class SampleRetryCodingWorker:
    """Implement the documented sample objective using only permissioned tools.

    This worker is deliberately specific to the fixture; it demonstrates the platform's
    worker plug-in boundary without pretending a rule engine can implement arbitrary code.
    """

    def __init__(
        self,
        *,
        root: Path,
        store: SqlStore,
        tools: ToolExecutor,
        evidence: EvidenceLedger,
        failures: FailureInjector | None = None,
    ) -> None:
        self._root = root
        self._store = store
        self._tools = tools
        self._evidence = evidence
        self._failures = failures or FailureInjector()

    async def execute(self, run: RunRecord, task: TaskRecord) -> TaskOutcome:
        handlers = {
            "inspect_repository": self._inspect,
            "research_constraints": self._research,
            "implement_change": self._implement,
            "verify_change": self._verify,
            "generate_report": self._report,
        }
        try:
            handler = handlers[task.spec.task_id]
        except KeyError as exc:
            raise DomainValidationError(f"unsupported demo task: {task.spec.task_id}") from exc
        return await handler(run, task)

    async def _inspect(self, run: RunRecord, task: TaskRecord) -> TaskOutcome:
        index_result = await self._tools.execute(
            run_id=run.run_id,
            task_id=task.spec.task_id,
            tool_name="repository.index",
            arguments={
                "snapshot_id": self._snapshot_id(run, task),
                "previous_snapshot_id": run.repository_snapshot_id,
            },
            idempotency_key=f"{run.run_id}:{task.spec.task_id}:repository-index",
        )
        index = await self._store.get_repository_index(str(index_result.output["snapshot_id"]))
        artifact_result = await self._tools.execute(
            run_id=run.run_id,
            task_id=task.spec.task_id,
            tool_name="artifact.record",
            arguments={
                "artifact_id": f"{run.run_id}-repository-map.md",
                "content": index.repository_map,
                "media_type": "text/markdown",
            },
            idempotency_key=f"{run.run_id}:{task.spec.task_id}:repository-map",
        )
        artifact_id = str(artifact_result.output["artifact_id"])
        await self._store.record_artifact(
            artifact_id=artifact_id,
            run_id=run.run_id,
            task_id=task.spec.task_id,
            uri=f"artifact://{artifact_id}",
            media_type="text/markdown",
            content_hash=str(artifact_result.output["content_hash"]),
            size_bytes=len(index.repository_map.encode("utf-8")),
        )
        artifact_evidence = await self._evidence.record(
            run_id=run.run_id,
            evidence_type=EvidenceType.ARTIFACT,
            source=f"{run.run_id}-repository-map.md",
            source_location=str(artifact_result.output["artifact_id"]),
            content=index.repository_map,
            excerpt="Generated repository map with file and symbol provenance.",
            task_id=task.spec.task_id,
            reliability=0.9,
            status=VerificationStatus.VERIFIED,
        )
        snapshot_evidence = await self._evidence.record(
            run_id=run.run_id,
            evidence_type=EvidenceType.REPOSITORY_SNAPSHOT,
            source=str(self._root),
            source_location=index.snapshot.snapshot_id,
            content={
                "manifest_hash": index.snapshot.manifest_hash,
                "files": [
                    {"path": item.relative_path, "hash": item.content_hash}
                    for item in index.snapshot.files
                    if not item.is_deleted
                ],
            },
            excerpt=(
                f"Indexed {index.snapshot.file_count} files ({index.snapshot.total_bytes} bytes)."
            ),
            snapshot_id=index.snapshot.snapshot_id,
            task_id=task.spec.task_id,
            reliability=1,
            status=VerificationStatus.VERIFIED,
        )
        search = await self._tools.execute(
            run_id=run.run_id,
            task_id=task.spec.task_id,
            tool_name="repository.search",
            arguments={
                "snapshot_id": index.snapshot.snapshot_id,
                "query": "retry limit attempts config tests",
                "limit": 6,
            },
            idempotency_key=f"{run.run_id}:{task.spec.task_id}:repository-search",
        )
        relevant_ids = self._retrieved_item_ids(search.output.get("items"))
        file_evidence = []
        for item_id in relevant_ids[:3]:
            chunk = next(chunk for chunk in index.chunks if chunk.chunk_id == item_id)
            file_evidence.append(
                await self._evidence.record_repository_chunk(
                    run_id=run.run_id, task_id=task.spec.task_id, chunk=chunk
                )
            )
        await self._evidence.claim(
            run_id=run.run_id,
            text=(
                f"The repository snapshot contains {index.snapshot.file_count} indexed text files."
            ),
            kind=ClaimKind.VERIFIED_FACT,
            evidence_ids=(snapshot_evidence.evidence_id,),
            task_id=task.spec.task_id,
        )
        return TaskOutcome(
            evidence_ids=(
                snapshot_evidence.evidence_id,
                artifact_evidence.evidence_id,
                *(item.evidence_id for item in file_evidence),
            ),
            artifact_ids=(str(artifact_result.output["artifact_id"]),),
            repository_snapshot_id=index.snapshot.snapshot_id,
            context_note=(index.repository_map + "\n" + ("indexed-context " * 500)),
        )

    async def _research(self, run: RunRecord, task: TaskRecord) -> TaskOutcome:
        self._failures.hit("research_constraints")
        if not run.repository_snapshot_id:
            raise DomainValidationError("research task requires a repository snapshot")
        index = await self._store.get_repository_index(run.repository_snapshot_id)
        search = await self._tools.execute(
            run_id=run.run_id,
            task_id=task.spec.task_id,
            tool_name="repository.search",
            arguments={
                "snapshot_id": run.repository_snapshot_id,
                "query": "historical three attempt retry compatibility",
                "limit": 10,
            },
            idempotency_key=f"{run.run_id}:{task.spec.task_id}:repository-search",
        )
        result_ids = self._retrieved_item_ids(search.output.get("items"))
        if not result_ids:
            raise DomainValidationError("could not locate retry compatibility evidence")
        records = []
        for item_id in result_ids[:3]:
            chunk = next(chunk for chunk in index.chunks if chunk.chunk_id == item_id)
            records.append(
                await self._evidence.record_repository_chunk(
                    run_id=run.run_id, task_id=task.spec.task_id, chunk=chunk
                )
            )
        await self._evidence.claim(
            run_id=run.run_id,
            text="The sample service's compatibility requirement is a three-attempt default.",
            kind=ClaimKind.VERIFIED_FACT,
            evidence_ids=tuple(item.evidence_id for item in records),
            task_id=task.spec.task_id,
        )
        return TaskOutcome(
            evidence_ids=tuple(item.evidence_id for item in records),
            context_note="Compatibility constraint preserved: default retry_limit is three. "
            + ("constraint-context " * 500),
        )

    async def _implement(self, run: RunRecord, task: TaskRecord) -> TaskOutcome:
        transformations = {
            "sample_service/config.py": self._transform_config,
            "sample_service/client.py": self._transform_client,
            "README.md": self._transform_readme,
            "tests/test_client.py": self._transform_tests,
        }
        evidence_ids = []
        for relative_path, transform in transformations.items():
            read = await self._tools.execute(
                run_id=run.run_id,
                task_id=task.spec.task_id,
                tool_name="repository.read",
                arguments={"path": relative_path},
                idempotency_key=f"{run.run_id}:{task.spec.task_id}:read:{relative_path}",
            )
            old_content = str(read.output["content"])
            new_content = transform(old_content)
            write = await self._tools.execute(
                run_id=run.run_id,
                task_id=task.spec.task_id,
                tool_name="repository.write",
                arguments={
                    "path": relative_path,
                    "content": new_content,
                    "expected_hash": read.output["content_hash"],
                },
                idempotency_key=f"{run.run_id}:{task.spec.task_id}:write:{relative_path}",
            )
            record = await self._evidence.record(
                run_id=run.run_id,
                evidence_type=EvidenceType.REPOSITORY_FILE,
                source=relative_path,
                source_location=f"{relative_path}:1-{len(new_content.splitlines())}",
                content=new_content,
                excerpt=f"Updated {relative_path}; hash {write.output['content_hash']}",
                task_id=task.spec.task_id,
                reliability=0.95,
                status=VerificationStatus.VERIFIED,
                metadata={"pre_change_hash": str(read.output["content_hash"])},
            )
            evidence_ids.append(record.evidence_id)
        index_result = await self._tools.execute(
            run_id=run.run_id,
            task_id=task.spec.task_id,
            tool_name="repository.index",
            arguments={
                "snapshot_id": self._snapshot_id(run, task),
                "previous_snapshot_id": run.repository_snapshot_id,
            },
            idempotency_key=f"{run.run_id}:{task.spec.task_id}:repository-index",
        )
        index = await self._store.get_repository_index(str(index_result.output["snapshot_id"]))
        await self._evidence.claim(
            run_id=run.run_id,
            text=(
                "Configuration, client behavior, documentation, and tests now support retry_limit."
            ),
            kind=ClaimKind.VERIFIED_FACT,
            evidence_ids=tuple(evidence_ids),
            task_id=task.spec.task_id,
        )
        return TaskOutcome(
            evidence_ids=tuple(evidence_ids),
            changed_files=tuple(transformations),
            repository_snapshot_id=index.snapshot.snapshot_id,
            context_note="Implemented retry_limit with default 3 and positive-value validation.",
        )

    async def _verify(self, run: RunRecord, task: TaskRecord) -> TaskOutcome:
        result = await self._tools.execute(
            run_id=run.run_id,
            task_id=task.spec.task_id,
            tool_name="test.run",
            arguments={"argv": ["python3", "-m", "pytest", "-q"], "cwd": "."},
            idempotency_key=f"{run.run_id}:{task.spec.task_id}:pytest",
        )
        if result.exit_code != 0:
            raise ToolExecutionError(
                f"fixture test suite failed with exit code {result.exit_code}: {result.output}"
            )
        evidence = await self._evidence.record_tool_result(
            run_id=run.run_id,
            task_id=task.spec.task_id,
            tool_name="pytest -q",
            result=result,
            test_result=True,
        )
        await self._evidence.claim(
            run_id=run.run_id,
            text="The modified sample service passes its complete automated test suite.",
            kind=ClaimKind.TEST_SUPPORTED,
            evidence_ids=(evidence.evidence_id,),
            task_id=task.spec.task_id,
        )
        return TaskOutcome(
            evidence_ids=(evidence.evidence_id,),
            test_results=(str(result.output.get("stdout", "tests passed")),),
            context_note=f"pytest completed successfully: {result.output}",
        )

    async def _report(self, run: RunRecord, task: TaskRecord) -> TaskOutcome:
        del task
        messages = await self._evidence.verify_run(run.run_id)
        return TaskOutcome(
            context_note="; ".join(messages),
        )

    @staticmethod
    def _retrieved_item_ids(value: object) -> tuple[str, ...]:
        if not isinstance(value, list):
            raise ToolExecutionError("repository search returned an invalid item list")
        item_ids: list[str] = []
        for item in value:
            if not isinstance(item, dict) or not isinstance(item.get("item_id"), str):
                raise ToolExecutionError("repository search returned an invalid item")
            item_ids.append(item["item_id"])
        return tuple(item_ids)

    @staticmethod
    def _snapshot_id(run: RunRecord, task: TaskRecord) -> str:
        return f"snap-{sha256_digest(f'{run.run_id}:{task.spec.task_id}')[:32]}"

    @staticmethod
    def _transform_config(content: str) -> str:
        if "retry_limit: int" not in content:
            content = content.replace(
                "    retry_delay_seconds: float = 0.01\n",
                "    retry_delay_seconds: float = 0.01\n    retry_limit: int = 3\n",
            )
        if "retry limit must be positive" not in content:
            content = content.replace(
                '            raise ValueError("retry delay cannot be negative")\n',
                '            raise ValueError("retry delay cannot be negative")\n'
                "        if self.retry_limit < 1:\n"
                '            raise ValueError("retry limit must be positive")\n',
            )
        return content

    @staticmethod
    def _transform_client(content: str) -> str:
        return content.replace(
            "for _attempt in range(3):", "for _attempt in range(self._config.retry_limit):"
        )

    @staticmethod
    def _transform_readme(content: str) -> str:
        addition = (
            "\n## Retry configuration\n\nSet `ServiceConfig(retry_limit=N)` to permit "
            "N delivery attempts. "
            "The default remains 3 for backward compatibility, and limits must be positive.\n"
        )
        return (
            content if "## Retry configuration" in content else content.rstrip() + "\n" + addition
        )

    @staticmethod
    def _transform_tests(content: str) -> str:
        if "test_configurable_retry_limit" in content:
            return content
        return (
            content.rstrip()
            + """


def test_configurable_retry_limit() -> None:
    transport = ScriptedTransport([TransientTransportError("one"), "sent"])
    client = NotificationClient(
        transport, ServiceConfig(retry_delay_seconds=0, retry_limit=2)
    )
    assert client.notify("hello") == "sent:hello"
    assert transport.calls == 2


def test_retry_limit_must_be_positive() -> None:
    try:
        ServiceConfig(retry_limit=0)
    except ValueError as error:
        assert str(error) == "retry limit must be positive"
    else:
        raise AssertionError("zero retry limit was accepted")
"""
        )
