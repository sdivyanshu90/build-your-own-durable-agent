"""Reproduce the complete offline pause/restart/retry/compression/report workflow."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
from pathlib import Path

from durable_agent.application import build_application
from durable_agent.configuration import Settings
from durable_agent.domain.enums import RunState
from durable_agent.observability import configure_logging
from durable_agent.providers.fakes import FailureInjector


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument(
        "--schema-mode",
        choices=("alembic", "metadata"),
        default="alembic",
        help="metadata is an isolated fallback for constrained test hosts only",
    )
    return parser.parse_args()


async def demonstrate(workspace: Path, *, schema_mode: str) -> dict[str, object]:
    workspace = workspace.resolve()
    if workspace.exists():
        raise ValueError(f"demo workspace already exists: {workspace}")
    workspace.mkdir(parents=True, mode=0o700)
    repository = workspace / "sample-service"
    shutil.copytree(Path("tests/fixtures/sample_service"), repository)
    database_path = workspace / "agent.db"
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        sync_database_url=f"sqlite:///{database_path}",
        repository_root=repository,
        artifact_directory=workspace / "artifacts",
        allow_file_writes=True,
        allow_shell=True,
        allowed_commands=("pytest", "python3"),
        model_context_limit=6_000,
        reserved_output_tokens=512,
        system_instruction_tokens=512,
        user_request_tokens=512,
        context_compression_threshold=0.5,
    )
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    if schema_mode == "alembic":
        try:
            from alembic import command
            from alembic.config import Config
        except ImportError as exc:
            raise RuntimeError(
                "Alembic is required for the normal demo; install .[dev] or use "
                "--schema-mode metadata only on a constrained verification host"
            ) from exc
        previous_database = os.environ.get("DURABLE_AGENT_DATABASE_URL")
        previous_sync = os.environ.get("DURABLE_AGENT_SYNC_DATABASE_URL")
        os.environ["DURABLE_AGENT_DATABASE_URL"] = settings.database_url
        os.environ["DURABLE_AGENT_SYNC_DATABASE_URL"] = settings.sync_database_url
        try:
            command.upgrade(Config("alembic.ini"), "head")
        finally:
            if previous_database is None:
                os.environ.pop("DURABLE_AGENT_DATABASE_URL", None)
            else:
                os.environ["DURABLE_AGENT_DATABASE_URL"] = previous_database
            if previous_sync is None:
                os.environ.pop("DURABLE_AGENT_SYNC_DATABASE_URL", None)
            else:
                os.environ["DURABLE_AGENT_SYNC_DATABASE_URL"] = previous_sync

    first_process = await build_application(
        settings,
        create_schema_for_tests=schema_mode == "metadata",
    )
    run = await first_process.service.create_run(
        objective=(
            "Add a configurable retry limit to the sample service, update its "
            "documentation, preserve backward compatibility, and prove the change "
            "using automated tests."
        ),
        idempotency_key="demo-create-v1",
    )
    first_boundary = await first_process.service.advance(run.run_id, maximum_tasks=1)
    await first_process.service.pause(
        run.run_id,
        reason="demonstrate process restart",
        idempotency_key="demo-pause-v1",
    )
    paused = await first_process.service.advance(run.run_id)
    await first_process.close()
    if paused.run.state != RunState.PAUSED:
        raise RuntimeError(f"expected PAUSED, got {paused.run.state}")

    injector = FailureInjector({"research_constraints": 1})
    second_process = await build_application(settings, failure_injector=injector)
    try:
        completed = await second_process.service.resume(run.run_id)
        if completed.run.state != RunState.COMPLETED:
            raise RuntimeError(f"demo failed in state {completed.run.state}")
        verification = await second_process.service.verify(run.run_id)
        markdown = await second_process.service.report(run.run_id)
        json_report = await second_process.service.report(run.run_id, format="json")
        markdown_path = workspace / "final-report.md"
        json_path = workspace / "final-report.json"
        markdown_path.write_text(markdown, encoding="utf-8")
        json_path.write_text(json_report, encoding="utf-8")
        contexts = await second_process.store.latest_context(run.run_id)
        events = [item async for item in second_process.store.stream(run.run_id)]
        return {
            "run_id": run.run_id,
            "initial_tasks_executed": first_boundary.tasks_executed,
            "paused_checkpoint_id": paused.checkpoint_id,
            "final_state": completed.run.state.value,
            "retry_attempts": injector.attempts,
            "context_compressed": bool(contexts and contexts[2]),
            "evidence_count": len(await second_process.store.get_evidence(run.run_id)),
            "checkpoint_count": len(await second_process.store.list_checkpoints(run.run_id)),
            "recovery_event": any(item["event_type"] == "recovery.completed" for item in events),
            "verification": verification,
            "markdown_report": str(markdown_path),
            "json_report": str(json_path),
        }
    finally:
        await second_process.close()


def main() -> int:
    options = arguments()
    try:
        result = asyncio.run(demonstrate(options.workspace, schema_mode=options.schema_mode))
    except (RuntimeError, ValueError) as exc:
        print(f"demo failed: {exc}")
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
