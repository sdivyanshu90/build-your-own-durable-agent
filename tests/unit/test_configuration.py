from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from durable_agent.configuration import Settings


def test_safe_development_defaults(tmp_path: Path) -> None:
    settings = Settings(repository_root=tmp_path, artifact_directory=tmp_path / "artifacts")
    assert not settings.network_access
    assert not settings.allow_shell
    assert not settings.allow_file_writes
    assert settings.available_context_tokens == 22_528
    settings.prepare_directories()
    assert settings.artifact_directory.is_dir()


def test_fingerprint_excludes_secret_and_observability(tmp_path: Path) -> None:
    common = {"repository_root": tmp_path, "api_auth_token": "one"}
    first = Settings(**common)
    second = Settings(**{**common, "api_auth_token": "two", "log_level": "DEBUG"})
    assert first.fingerprint() == second.fingerprint()
    assert len(first.fingerprint()) == 64


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"model_context_limit": 3_000}, "token budgets"),
        ({"lease_ttl_seconds": 20, "lease_renewal_seconds": 10}, "lease renewal"),
        ({"database_url": "sqlite:///sync.db"}, "async SQLAlchemy"),
        ({"allowed_commands": ("/bin/sh",)}, "plain executable"),
        ({"environment": "production"}, "PostgreSQL"),
    ],
)
def test_invalid_configuration_is_actionable(values: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(**values)


def test_production_auth_requires_token() -> None:
    with pytest.raises(ValidationError, match="auth token"):
        Settings(
            environment="production",
            database_url="postgresql+asyncpg://db/agent",
            sync_database_url="postgresql+psycopg://db/agent",
            require_api_authentication=True,
        )
