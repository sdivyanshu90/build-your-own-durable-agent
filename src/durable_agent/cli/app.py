"""Operational CLI for indexing and durable run lifecycle control."""

from __future__ import annotations

import asyncio
import json
import sys
import sysconfig
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

import typer
from pydantic import ValidationError

from durable_agent.application import ApplicationContainer, build_application
from durable_agent.configuration import Settings
from durable_agent.domain.base import sha256_digest
from durable_agent.domain.errors import DurableAgentError
from durable_agent.observability import configure_logging

app = typer.Typer(
    name="durable-agent",
    help="Run checkpointed coding and research work with durable evidence.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
T = TypeVar("T")


def _run(awaitable: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(awaitable)


def _emit(value: Any, *, json_output: bool) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if json_output:
        typer.echo(json.dumps(value, sort_keys=True, indent=2, default=str))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            typer.echo(f"{key}: {item}")
    elif isinstance(value, list | tuple):
        for item in value:
            typer.echo(item if isinstance(item, str) else json.dumps(item, default=str))
    else:
        typer.echo(value)


def _settings_for_repository(repository: Path | None = None) -> Settings:
    settings = Settings()
    if repository is None:
        return settings
    resolved = repository.resolve(strict=True)
    return settings.model_copy(update={"repository_root": resolved})


def _alembic_configuration_path() -> Path:
    """Locate migration configuration in a checkout or installed wheel."""
    checkout = Path("alembic.ini").resolve()
    if checkout.is_file():
        return checkout
    installed = Path(sysconfig.get_path("data")) / "share" / "durable-agent" / "alembic.ini"
    if installed.is_file():
        return installed
    raise typer.BadParameter("Alembic migration assets are missing from this installation")


async def _with_application(
    settings: Settings,
    operation: Any,
) -> Any:
    configure_logging(
        level=settings.log_level,
        json_output=settings.log_json,
        secrets=(
            settings.api_auth_token.get_secret_value()
            if settings.api_auth_token is not None
            else "",
        ),
    )
    container = await build_application(settings)
    try:
        return await operation(container)
    finally:
        await container.close()


@app.command("init")
def initialize(json_output: bool = typer.Option(False, "--json", help="Emit JSON.")) -> None:
    """Apply all database migrations and create local artifact storage."""
    settings = Settings()
    settings.prepare_directories()
    try:
        from alembic import command
        from alembic.config import Config
    except ImportError as exc:
        raise typer.BadParameter(
            "Alembic is not installed; install the project dependencies first."
        ) from exc
    configuration = Config(str(_alembic_configuration_path()))
    command.upgrade(configuration, "head")
    _emit(
        {"status": "initialized", "database": settings.sync_database_url},
        json_output=json_output,
    )


@app.command()
def index(
    repository_path: Path = typer.Argument(..., exists=True, file_okay=False, resolve_path=True),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Create and persist an incremental repository snapshot."""
    settings = _settings_for_repository(repository_path)

    async def operation(container: ApplicationContainer) -> dict[str, str]:
        snapshot_id = await container.service.index_repository(settings.repository_root)
        return {"snapshot_id": snapshot_id, "repository": str(settings.repository_root)}

    _emit(_run(_with_application(settings, operation)), json_output=json_output)


@app.command("run")
def run_objective(
    objective: str | None = typer.Option(None, "--objective", help="Objective text."),
    objective_file: Path | None = typer.Option(
        None, "--objective-file", exists=True, dir_okay=False, resolve_path=True
    ),
    repository: Path | None = typer.Option(
        None, "--repository", exists=True, file_okay=False, resolve_path=True
    ),
    steps: int | None = typer.Option(None, min=0, help="Stop after this many task successes."),
    no_execute: bool = typer.Option(False, help="Create and checkpoint the plan only."),
    idempotency_key: str | None = typer.Option(None, help="Safe request replay key."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Create a planned run and execute it unless --no-execute is set."""
    if (objective is None) == (objective_file is None):
        raise typer.BadParameter("provide exactly one of --objective or --objective-file")
    if objective is not None:
        objective_text = objective
    elif objective_file is not None:
        objective_text = objective_file.read_text("utf-8")
    else:  # defensive narrowing if CLI validation behavior changes
        raise typer.BadParameter("objective file is required when objective text is absent")
    settings = _settings_for_repository(repository)
    request_key = idempotency_key or f"cli-{sha256_digest(objective_text)[:24]}"

    async def operation(container: ApplicationContainer) -> dict[str, Any]:
        created = await container.service.create_run(
            objective=objective_text,
            idempotency_key=request_key,
            repository_path=settings.repository_root,
        )
        if no_execute:
            return created.model_dump(mode="json")
        result = await container.service.advance(created.run_id, maximum_tasks=steps)
        return {
            **result.run.model_dump(mode="json"),
            "tasks_executed": result.tasks_executed,
            "checkpoint_id": result.checkpoint_id,
            "report_id": result.report_id,
        }

    _emit(_run(_with_application(settings, operation)), json_output=json_output)


@app.command()
def status(
    run_id: str,
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show current materialized run state."""
    settings = Settings()

    async def operation(container: ApplicationContainer) -> Any:
        return await container.service.status(run_id)

    _emit(_run(_with_application(settings, operation)), json_output=json_output)


@app.command("inspect-plan")
def inspect_plan(
    run_id: str,
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show the active plan and task graph."""
    settings = Settings()

    async def operation(container: ApplicationContainer) -> Any:
        await container.service.status(run_id)
        return await container.store.get_plan(run_id)

    _emit(_run(_with_application(settings, operation)), json_output=json_output)


@app.command("inspect-checkpoint")
def inspect_checkpoint(
    run_id: str,
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Inspect the newest checkpoint after integrity validation."""
    settings = Settings()

    async def operation(container: ApplicationContainer) -> Any:
        return await container.service.latest_checkpoint(run_id)

    _emit(_run(_with_application(settings, operation)), json_output=json_output)


@app.command()
def pause(
    run_id: str,
    reason: str = typer.Option(..., help="Auditable pause reason."),
    idempotency_key: str | None = typer.Option(None, help="Safe request replay key."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Request a safe-boundary pause and persist the pause checkpoint."""
    settings = Settings()

    async def operation(container: ApplicationContainer) -> Any:
        request = await container.service.pause(
            run_id,
            reason=reason,
            idempotency_key=idempotency_key or f"pause-{run_id}-{reason}",
        )
        result = await container.service.advance(run_id)
        return {
            "request": request.model_dump(mode="json"),
            "run": result.run.model_dump(mode="json"),
        }

    _emit(_run(_with_application(settings, operation)), json_output=json_output)


@app.command()
def resume(
    run_id: str,
    steps: int | None = typer.Option(None, min=0, help="Stop after this many task successes."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Validate and resume a paused or interrupted run."""
    settings = Settings()

    async def operation(container: ApplicationContainer) -> Any:
        return (await container.service.resume(run_id, maximum_tasks=steps)).run

    _emit(_run(_with_application(settings, operation)), json_output=json_output)


@app.command()
def cancel(
    run_id: str,
    reason: str = typer.Option(..., help="Auditable cancellation reason."),
    idempotency_key: str | None = typer.Option(None, help="Safe request replay key."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Cancel at a safe boundary and retain a partial report."""
    settings = Settings()

    async def operation(container: ApplicationContainer) -> Any:
        request = await container.service.cancel(
            run_id,
            reason=reason,
            idempotency_key=idempotency_key or f"cancel-{run_id}-{reason}",
        )
        result = await container.service.advance(run_id)
        return {
            "request": request.model_dump(mode="json"),
            "run": result.run.model_dump(mode="json"),
        }

    _emit(_run(_with_application(settings, operation)), json_output=json_output)


@app.command()
def report(
    run_id: str,
    format: str = typer.Option("markdown", help="markdown or json"),
    output: Path | None = typer.Option(None, dir_okay=False, resolve_path=True),
) -> None:
    """Render a stored report, optionally to a file."""
    settings = Settings()

    async def operation(container: ApplicationContainer) -> str:
        return await container.service.report(run_id, format=format)

    content = _run(_with_application(settings, operation))
    if output is None:
        typer.echo(content)
    else:
        output.write_text(content, encoding="utf-8")
        typer.echo(str(output))


@app.command()
def verify(
    run_id: str,
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Verify claim links and persisted report hashes."""
    settings = Settings()

    async def operation(container: ApplicationContainer) -> Any:
        return await container.service.verify(run_id)

    _emit(_run(_with_application(settings, operation)), json_output=json_output)


@app.command()
def cleanup(
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Apply retention; the default is a non-destructive dry run.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Compact eligible terminal events and remove old orphan artifact files."""
    settings = Settings()

    async def operation(container: ApplicationContainer) -> Any:
        return await container.service.cleanup_retention(dry_run=not execute)

    _emit(_run(_with_application(settings, operation)), json_output=json_output)


@app.command()
def doctor(json_output: bool = typer.Option(False, "--json", help="Emit JSON.")) -> None:
    """Check configuration, database connectivity, schema, and approved roots."""
    settings = Settings()

    async def operation(container: ApplicationContainer) -> dict[str, Any]:
        database = await container.database.ping()
        schema = await container.store.schema_ready()
        return {
            "python": sys.version.split()[0],
            "configuration": "valid",
            "database": "reachable" if database else "unreachable",
            "schema": "ready" if schema else "migration_required",
            "repository_root": str(settings.repository_root.resolve()),
            "network_access": settings.network_access,
            "file_writes": settings.allow_file_writes,
            "shell": settings.allow_shell,
        }

    result = _run(_with_application(settings, operation))
    _emit(result, json_output=json_output)
    if result["database"] != "reachable" or result["schema"] != "ready":
        raise typer.Exit(code=2)


def main() -> None:
    """Console-script entry point with stable error exit codes."""
    try:
        app()
    except (ValidationError, DurableAgentError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


if __name__ == "__main__":
    main()
