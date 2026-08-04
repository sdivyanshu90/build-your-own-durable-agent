from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from durable_agent.application import build_application
from durable_agent.configuration import Settings
from durable_agent.domain.enums import RunState, TaskState
from durable_agent.domain.errors import ArtifactIntegrityError
from durable_agent.providers.fakes import FailureInjector


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_successful_coding_task_retry_compression_and_report(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "fixtures" / "sample_service"
    repository = tmp_path / "sample_service"
    shutil.copytree(source, repository)
    database_path = tmp_path / "state.db"
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        sync_database_url=f"sqlite:///{database_path}",
        repository_root=repository,
        artifact_directory=tmp_path / "artifacts",
        allow_file_writes=True,
        allow_shell=True,
        allowed_commands=("pytest", "python3"),
        model_context_limit=6_000,
        reserved_output_tokens=512,
        system_instruction_tokens=512,
        user_request_tokens=512,
        context_compression_threshold=0.5,
    )
    application = await build_application(
        settings,
        create_schema_for_tests=True,
        failure_injector=FailureInjector({"research_constraints": 1}),
    )
    try:
        run = await application.service.create_run(
            objective=(
                "Add a configurable retry limit to the sample service, update documentation, "
                "preserve backward compatibility, and prove the change with tests."
            ),
            idempotency_key="create-demo",
        )
        result = await application.service.advance(run.run_id)
        assert result.run.state == RunState.COMPLETED
        assert all(task.state == TaskState.SUCCEEDED for task in result.tasks)
        attempts = {task.spec.task_id: task.attempt_count for task in result.tasks}
        assert attempts["research_constraints"] == 2
        assert result.report_id is not None

        config = (repository / "sample_service" / "config.py").read_text()
        client = (repository / "sample_service" / "client.py").read_text()
        tests = (repository / "tests" / "test_client.py").read_text()
        readme = (repository / "README.md").read_text()
        assert "retry_limit: int = 3" in config
        assert "retry limit must be positive" in config
        assert "range(self._config.retry_limit)" in client
        assert "test_configurable_retry_limit" in tests
        assert "## Retry configuration" in readme

        latest_context = await application.store.latest_context(run.run_id)
        assert latest_context is not None
        assert latest_context[2] is not None
        assert latest_context[0].used_tokens <= latest_context[0].budget_tokens
        assert latest_context[2].evidence_ids

        markdown = await application.service.report(run.run_id)
        json_report = await application.service.report(run.run_id, format="json")
        assert "## Claim-to-evidence mapping" in markdown
        assert "TEST_SUPPORTED" in markdown
        assert "task.failed_retryable" in markdown
        assert "injected transient failure" in markdown
        assert '"evidence_ids"' in json_report
        verification = await application.service.verify(run.run_id)
        assert any("claim mappings" in item for item in verification)
        assert any("markdown report hash" in item for item in verification)

        events = [item async for item in application.store.stream(run.run_id)]
        assert any(item["event_type"] == "task.failed_retryable" for item in events)
        assert any(item["event_type"] == "run.completed" for item in events)
        checkpoints = await application.store.list_checkpoints(run.run_id)
        assert len(checkpoints) >= 7
        artifact_path = settings.artifact_directory / f"{run.run_id}-repository-map.md"
        artifact_path.write_text("tampered", encoding="utf-8")
        with pytest.raises(ArtifactIntegrityError, match="hash mismatch"):
            await application.service.verify(run.run_id)
    finally:
        await application.close()
