from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from typer.testing import CliRunner

from durable_agent.cli.app import app
from durable_agent.persistence import Database

runner = CliRunner()


def prepare_cli_environment(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:  # type: ignore[no-untyped-def]
    fixture = Path(__file__).parents[1] / "fixtures" / "sample_service"
    repository = tmp_path / "repo"
    shutil.copytree(fixture, repository)
    database_path = tmp_path / "cli.db"
    monkeypatch.setenv("DURABLE_AGENT_DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    monkeypatch.setenv("DURABLE_AGENT_SYNC_DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("DURABLE_AGENT_REPOSITORY_ROOT", str(repository))
    monkeypatch.setenv("DURABLE_AGENT_ARTIFACT_DIRECTORY", str(tmp_path / "artifacts"))
    return repository, database_path


def test_cli_run_status_plan_checkpoint_and_doctor(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repository, database_path = prepare_cli_environment(tmp_path, monkeypatch)
    database = Database(f"sqlite:///{database_path}")
    import asyncio

    asyncio.run(database.create_schema_for_tests())
    asyncio.run(database.dispose())

    created = runner.invoke(
        app,
        [
            "run",
            "--objective",
            "Inspect the repository without executing tools",
            "--repository",
            str(repository),
            "--no-execute",
            "--json",
        ],
    )
    assert created.exit_code == 0, created.output
    run_id = json.loads(created.stdout)["run_id"]
    status = runner.invoke(app, ["status", run_id, "--json"])
    assert status.exit_code == 0
    assert json.loads(status.stdout)["state"] == "RUNNING"
    plan = runner.invoke(app, ["inspect-plan", run_id, "--json"])
    assert plan.exit_code == 0
    assert len(json.loads(plan.stdout)["tasks"]) == 5
    checkpoint = runner.invoke(app, ["inspect-checkpoint", run_id, "--json"])
    assert checkpoint.exit_code == 0
    assert json.loads(checkpoint.stdout)["sequence"] == 1
    doctor = runner.invoke(app, ["doctor", "--json"])
    assert doctor.exit_code == 0
    assert json.loads(doctor.stdout)["schema"] == "ready"


def test_cli_rejects_ambiguous_objective(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    prepare_cli_environment(tmp_path, monkeypatch)
    result = runner.invoke(app, ["run", "--objective", "one", "--objective-file", __file__])
    assert result.exit_code == 2
    assert "exactly one" in result.output


def test_cli_index_pause_resume_cancel_report_and_verify(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repository, database_path = prepare_cli_environment(tmp_path, monkeypatch)
    database = Database(f"sqlite:///{database_path}")
    import asyncio

    asyncio.run(database.create_schema_for_tests())
    asyncio.run(database.dispose())
    indexed = runner.invoke(app, ["index", str(repository), "--json"])
    assert indexed.exit_code == 0
    assert json.loads(indexed.stdout)["repository"] == str(repository)
    objective_file = tmp_path / "objective.md"
    objective_file.write_text("Inspect and preserve the sample service")
    created = runner.invoke(
        app,
        ["run", "--objective-file", str(objective_file), "--no-execute", "--json"],
    )
    assert created.exit_code == 0, created.output
    run_id = json.loads(created.stdout)["run_id"]
    paused = runner.invoke(app, ["pause", run_id, "--reason", "test boundary", "--json"])
    assert paused.exit_code == 0, paused.output
    assert json.loads(paused.stdout)["run"]["state"] == "PAUSED"
    resumed = runner.invoke(app, ["resume", run_id, "--steps", "0", "--json"])
    assert resumed.exit_code == 0, resumed.output
    assert json.loads(resumed.stdout)["state"] == "RUNNING"
    cancelled = runner.invoke(app, ["cancel", run_id, "--reason", "test complete", "--json"])
    assert cancelled.exit_code == 0, cancelled.output
    assert json.loads(cancelled.stdout)["run"]["state"] == "CANCELLED"
    report_path = tmp_path / "partial.json"
    report = runner.invoke(
        app,
        ["report", run_id, "--format", "json", "--output", str(report_path)],
    )
    assert report.exit_code == 0, report.output
    assert json.loads(report_path.read_text())["partial"] is True
    verified = runner.invoke(app, ["verify", run_id, "--json"])
    assert verified.exit_code == 0
    assert any("claim mappings" in item for item in json.loads(verified.stdout))

    orphan = tmp_path / "artifacts" / "orphan.txt"
    orphan.write_text("orphan")
    os.utime(orphan, (1, 1))
    preview = runner.invoke(app, ["cleanup", "--json"])
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.stdout)["orphan_artifact_ids"] == ["orphan.txt"]
    assert orphan.exists()
    cleanup = runner.invoke(app, ["cleanup", "--execute", "--json"])
    assert cleanup.exit_code == 0, cleanup.output
    assert json.loads(cleanup.stdout)["deleted_orphan_count"] == 1
    assert not orphan.exists()


def test_cli_doctor_returns_not_ready_exit_code(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    prepare_cli_environment(tmp_path, monkeypatch)
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["schema"] == "migration_required"
