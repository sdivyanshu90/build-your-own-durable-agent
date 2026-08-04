"""Conservative secret redaction for logs and tool results."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


class SecretRedactor:
    """Redact named secrets and common credential formats recursively."""

    _KEY_PATTERN = re.compile(
        r"(?i)(api[_-]?key|authorization|password|passwd|secret|token|credential|private[_-]?key)"
    )
    _VALUE_PATTERNS = (
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
        re.compile(r"\b(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{12,}"),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        re.compile(r"(?i)(://[^:/\s]+:)([^@/\s]+)(@)"),
    )

    def __init__(self, explicit_secrets: Sequence[str] = ()) -> None:
        self._secrets = tuple(
            sorted((item for item in explicit_secrets if item), key=len, reverse=True)
        )

    def redact_text(self, value: str) -> str:
        """Replace recognizable credential values with a stable marker."""
        result = value.replace("\r", "\\r").replace("\x00", "")
        for secret in self._secrets:
            result = result.replace(secret, "[REDACTED]")
        for pattern in self._VALUE_PATTERNS:
            if pattern.groups == 3:
                result = pattern.sub(r"\1[REDACTED]\3", result)
            else:
                result = pattern.sub("[REDACTED]", result)
        return result

    def redact(self, value: Any) -> Any:
        """Recursively redact mappings, sequences, and text."""
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, Mapping):
            return {
                str(key): "[REDACTED]" if self._KEY_PATTERN.search(str(key)) else self.redact(item)
                for key, item in value.items()
            }
        if isinstance(value, tuple):
            return tuple(self.redact(item) for item in value)
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        return value
