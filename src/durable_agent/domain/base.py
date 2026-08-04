"""Shared domain model behavior."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def canonical_json(value: Any) -> bytes:
    """Serialize JSON-compatible data deterministically for hashing."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    ).encode("utf-8")


def sha256_digest(value: bytes | str) -> str:
    """Return a lowercase SHA-256 hex digest."""
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    raise TypeError(f"Unsupported canonical JSON value: {type(value).__name__}")


class DomainModel(BaseModel):
    """Strict base for persisted and provider-facing domain schemas."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, use_enum_values=False)

    @field_validator("*", mode="before")
    @classmethod
    def reject_nul_strings(cls, value: Any) -> Any:
        """Reject NULs at the validation boundary."""
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("NUL bytes are not allowed")
        return value
