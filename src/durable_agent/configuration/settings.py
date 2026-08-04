"""Validated environment and file-backed application settings."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from durable_agent.domain.base import canonical_json, sha256_digest
from durable_agent.domain.enums import RepositoryDriftPolicy
from durable_agent.domain.retry import RetryPolicy


class Settings(BaseSettings):
    """Complete startup-validated configuration surface."""

    model_config = SettingsConfigDict(
        env_prefix="DURABLE_AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
        validate_default=True,
    )

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite+aiosqlite:///./durable-agent.db"
    sync_database_url: str = "sqlite:///./durable-agent.db"
    artifact_directory: Path = Path("artifacts")
    repository_root: Path = Path()
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = True

    llm_provider: str = "deterministic"
    model_name: str = "offline-baseline"
    model_context_limit: int = Field(default=32_768, ge=2_048, le=10_000_000)
    reserved_output_tokens: int = Field(default=4_096, ge=128)
    system_instruction_tokens: int = Field(default=2_048, ge=128)
    user_request_tokens: int = Field(default=4_096, ge=128)
    context_compression_threshold: float = Field(default=0.8, gt=0.2, le=0.95)

    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    tool_timeout_seconds: float = Field(default=60, gt=0, le=3_600)
    checkpoint_every_tasks: int = Field(default=1, ge=1, le=100)
    maximum_concurrency: int = Field(default=1, ge=1, le=128)
    lease_ttl_seconds: int = Field(default=60, ge=5, le=3_600)
    lease_renewal_seconds: int = Field(default=20, ge=1, le=1_800)

    allow_file_writes: bool = False
    allow_shell: bool = False
    allow_patch: bool = False
    network_access: bool = False
    allowed_commands: tuple[str, ...] = ("python", "python3", "pytest", "ruff", "mypy")
    allowed_environment_variables: tuple[str, ...] = ("PATH", "LANG", "LC_ALL", "TERM")
    maximum_tool_output_bytes: int = Field(default=1_000_000, ge=1_024, le=100_000_000)
    maximum_file_bytes: int = Field(default=2_000_000, ge=1_024, le=1_000_000_000)
    maximum_repository_bytes: int = Field(default=100_000_000, ge=1_024, le=100_000_000_000)
    indexing_exclusions: tuple[str, ...] = (
        ".git/",
        ".venv/",
        "__pycache__/",
        "node_modules/",
        "artifacts/",
    )
    repository_drift_policy: RepositoryDriftPolicy = RepositoryDriftPolicy.REINDEX

    checkpoint_retention_count: int = Field(default=100, ge=2, le=100_000)
    event_retention_days: int = Field(default=365, ge=1, le=3_650)
    artifact_retention_days: int = Field(default=90, ge=1, le=3_650)
    metrics_enabled: bool = True
    tracing_enabled: bool = True
    api_auth_token: SecretStr | None = None
    require_api_authentication: bool = False
    api_allowed_owners: tuple[str, ...] = ()

    @field_validator("database_url")
    @classmethod
    def async_database_driver(cls, value: str) -> str:
        allowed = ("sqlite+aiosqlite://", "postgresql+asyncpg://", "postgresql+psycopg://")
        if not value.startswith(allowed):
            raise ValueError("database_url must use a supported async SQLAlchemy driver")
        return value

    @field_validator("sync_database_url")
    @classmethod
    def sync_database_driver(cls, value: str) -> str:
        if not value.startswith(("sqlite://", "postgresql+psycopg://")):
            raise ValueError("sync_database_url must use sqlite or psycopg")
        return value

    @field_validator("allowed_commands")
    @classmethod
    def plain_command_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any("/" in command or "\\" in command or not command for command in value):
            raise ValueError("allowed commands must be plain executable names")
        return value

    @model_validator(mode="after")
    def validate_cross_field_constraints(self) -> Settings:
        reserved = (
            self.reserved_output_tokens + self.system_instruction_tokens + self.user_request_tokens
        )
        if reserved >= self.model_context_limit:
            raise ValueError("fixed token budgets must be below the model context limit")
        if self.lease_renewal_seconds * 2 >= self.lease_ttl_seconds:
            raise ValueError("lease renewal must be less than half the lease TTL")
        if (
            self.environment == "production"
            and self.require_api_authentication
            and self.api_auth_token is None
        ):
            raise ValueError("production API authentication requires an auth token")
        if self.environment == "production" and self.database_url.startswith("sqlite"):
            raise ValueError("production configuration must use PostgreSQL")
        return self

    @property
    def available_context_tokens(self) -> int:
        """Tokens available after fixed input/output reservations."""
        return self.model_context_limit - (
            self.reserved_output_tokens + self.system_instruction_tokens + self.user_request_tokens
        )

    def fingerprint(self) -> str:
        """Hash resume-sensitive settings without secrets or observability toggles."""
        keys = {
            "database_dialect": self.database_url.split(":", maxsplit=1)[0],
            "repository_root": str(self.repository_root.resolve()),
            "llm_provider": self.llm_provider,
            "model_name": self.model_name,
            "model_context_limit": self.model_context_limit,
            "reserved_output_tokens": self.reserved_output_tokens,
            "maximum_concurrency": self.maximum_concurrency,
            "tool_permissions": {
                "write": self.allow_file_writes,
                "shell": self.allow_shell,
                "patch": self.allow_patch,
                "network": self.network_access,
            },
            "maximum_file_bytes": self.maximum_file_bytes,
            "maximum_repository_bytes": self.maximum_repository_bytes,
            "repository_drift_policy": self.repository_drift_policy.value,
        }
        return sha256_digest(canonical_json(keys))

    def prepare_directories(self) -> None:
        """Create local writable directories with private permissions."""
        self.artifact_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
