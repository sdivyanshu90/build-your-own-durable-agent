from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from durable_agent.persistence.orm import Base
from durable_agent.persistence.schema_v1 import SchemaV1Base


def test_initial_revision_uses_frozen_schema_matching_current_v1() -> None:
    """Protect migration history from accidental coupling to the live ORM."""
    revision = Path("migrations/versions/0001_initial_schema.py").read_text()
    assert "durable_agent.persistence.orm" not in revision
    frozen = SchemaV1Base.metadata
    assert set(frozen.tables) == set(Base.metadata.tables)
    for name, table in Base.metadata.tables.items():
        assert tuple(frozen.tables[name].columns.keys()) == tuple(table.columns.keys())


def test_fresh_alembic_upgrade_contract() -> None:
    pytest.importorskip("alembic")
    completed = subprocess.run(
        [sys.executable, "scripts/validate_migrations.py"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "migration validation passed" in completed.stdout
