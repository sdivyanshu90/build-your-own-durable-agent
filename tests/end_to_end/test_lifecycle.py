from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from durable_agent.application import build_application
from durable_agent.configuration import Settings
from durable_agent.domain.enums import RunState, TaskState


def lifecycle_settings(tmp_path: Path, repository: Path) -> Settings:
    database_path = tmp_path / "state.db"
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        sync_database_url=f"sqlite:///{database_path}",
        repository_root=repository,
        artifact_directory=tmp_path / "artifacts",
        allow_file_writes=True,
        allow_shell=True,
        allowed_commands=("pytest", "python3"),
        model_context_limit=8_000,
        reserved_output_tokens=512,
        system_instruction_tokens=512,
        user_request_tokens=512,
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pause_process_restart_resume_does_not_repeat_completed_work(tmp_path: Path) -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "sample_service"
    repository = tmp_path / "repo"
    shutil.copytree(fixture, repository)
    settings = lifecycle_settings(tmp_path, repository)
    first_process = await build_application(settings, create_schema_for_tests=True)
    run = await first_process.service.create_run(
        objective="Add configurable retry limit with tests and documentation",
        idempotency_key="create",
    )
    progress = await first_process.service.advance(run.run_id, maximum_tasks=1)
    assert progress.run.state == RunState.RUNNING
    inspect = next(task for task in progress.tasks if task.spec.task_id == "inspect_repository")
    assert inspect.state == TaskState.SUCCEEDED
    await first_process.service.pause(
        run.run_id,
        reason="operator maintenance",
        idempotency_key="pause",
    )
    paused = await first_process.service.advance(run.run_id)
    assert paused.run.state == RunState.PAUSED
    paused_checkpoint = paused.checkpoint_id
    await first_process.close()

    second_process = await build_application(settings)
    try:
        completed = await second_process.service.resume(run.run_id)
        assert completed.run.state == RunState.COMPLETED
        assert completed.checkpoint_id != paused_checkpoint
        events = [item async for item in second_process.store.stream(run.run_id)]
        inspect_starts = [
            item
            for item in events
            if item["event_type"] == "task.started" and item["task_id"] == "inspect_repository"
        ]
        assert len(inspect_starts) == 1
        assert any(item["event_type"] == "recovery.completed" for item in events)
        assert "retry_limit: int = 3" in (repository / "sample_service" / "config.py").read_text()
    finally:
        await second_process.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_cancellation_preserves_artifact_and_generates_partial_report(tmp_path: Path) -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "sample_service"
    repository = tmp_path / "repo"
    shutil.copytree(fixture, repository)
    settings = lifecycle_settings(tmp_path, repository)
    application = await build_application(settings, create_schema_for_tests=True)
    try:
        run = await application.service.create_run(
            objective="Add configurable retry limit with tests and documentation",
            idempotency_key="create",
        )
        await application.service.advance(run.run_id, maximum_tasks=1)
        artifact = settings.artifact_directory / f"{run.run_id}-repository-map.md"
        assert artifact.exists()
        await application.service.cancel(
            run.run_id,
            reason="objective withdrawn",
            idempotency_key="cancel",
        )
        cancelled = await application.service.advance(run.run_id)
        assert cancelled.run.state == RunState.CANCELLED
        assert artifact.exists()
        assert any(task.state == TaskState.SUCCEEDED for task in cancelled.tasks)
        assert any(task.state == TaskState.CANCELLED for task in cancelled.tasks)
        markdown = await application.service.report(run.run_id)
        assert markdown.startswith(f"# Partial report: {run.run_id}")
        assert "run.cancelled: objective withdrawn" in markdown
    finally:
        await application.close()
