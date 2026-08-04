"""Structured exception taxonomy used across adapters."""

from __future__ import annotations

from typing import Any

from durable_agent.domain.enums import ErrorCategory


class DurableAgentError(Exception):
    """Base domain error with retry and operator metadata."""

    category = ErrorCategory.TERMINAL_DOMAIN
    retryable = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class DomainValidationError(DurableAgentError):
    category = ErrorCategory.VALIDATION


class InvalidTransitionError(DomainValidationError):
    """Raised when a state transition is not in the explicit table."""


class PlanValidationError(DomainValidationError):
    """Raised when a plan graph violates an invariant."""


class ProviderRetryableError(DurableAgentError):
    category = ErrorCategory.PROVIDER_RETRYABLE
    retryable = True


class RateLimitError(ProviderRetryableError):
    category = ErrorCategory.RATE_LIMIT


class OperationTimeoutError(ProviderRetryableError):
    category = ErrorCategory.TIMEOUT


class ToolExecutionError(DurableAgentError):
    category = ErrorCategory.TOOL_EXECUTION


class DatabaseError(DurableAgentError):
    category = ErrorCategory.DATABASE
    retryable = True


class ConcurrencyConflictError(DurableAgentError):
    category = ErrorCategory.CONCURRENCY
    retryable = True


class RepositoryChangedError(DurableAgentError):
    category = ErrorCategory.REPOSITORY_CHANGED


class CorruptCheckpointError(DurableAgentError):
    category = ErrorCategory.CORRUPT_CHECKPOINT


class ArtifactIntegrityError(DurableAgentError):
    """Stored artifact bytes no longer match their durable content hash."""


class UnsupportedSchemaVersionError(DurableAgentError):
    category = ErrorCategory.UNSUPPORTED_SCHEMA


class SecurityPolicyError(DurableAgentError):
    category = ErrorCategory.SECURITY_POLICY


class NotFoundError(DurableAgentError):
    """Requested domain entity does not exist."""


class IdempotencyConflictError(DomainValidationError):
    """An idempotency key was reused with a different request."""


class ManualReviewRequiredError(DurableAgentError):
    """An uncertain non-idempotent operation cannot be reconciled automatically."""
