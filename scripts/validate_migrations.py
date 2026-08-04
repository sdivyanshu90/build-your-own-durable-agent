"""Validate fresh schema shape and the one-head Alembic revision graph."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    try:
        import alembic  # noqa: F401
    except ImportError:
        print("ERROR: Alembic is not installed; install the project development dependencies.")
        return 2
    with tempfile.TemporaryDirectory(prefix="durable-agent-migration-") as directory:
        database = Path(directory) / "migration.db"
        environment = dict(os.environ)
        environment["DURABLE_AGENT_SYNC_DATABASE_URL"] = f"sqlite:///{database}"
        environment["DURABLE_AGENT_DATABASE_URL"] = f"sqlite+aiosqlite:///{database}"
        commands = (
            [sys.executable, "-m", "alembic", "heads"],
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            [sys.executable, "-m", "alembic", "current", "--check-heads"],
        )
        for command in commands:
            completed = subprocess.run(command, env=environment, check=False)
            if completed.returncode:
                return completed.returncode
        with sqlite3.connect(database) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
        required = {
            "runs",
            "plans",
            "plan_revisions",
            "tasks",
            "task_dependencies",
            "task_attempts",
            "checkpoints",
            "events",
            "tool_calls",
            "tool_results",
            "contexts",
            "summaries",
            "artifacts",
            "evidence",
            "claims",
            "claim_evidence_links",
            "reports",
            "repository_snapshots",
            "repository_files",
            "repository_chunks",
            "pause_requests",
            "cancellation_requests",
            "leases",
            "errors",
        }
        missing = required - tables
        if missing:
            print(f"ERROR: migration is missing tables: {sorted(missing)}")
            return 1
        print(f"migration validation passed: {len(tables)} tables, one head")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
