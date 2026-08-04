"""Untrusted-content labelling and prompt-injection indicators."""

from __future__ import annotations

import re

from pydantic import Field

from durable_agent.domain.base import DomainModel, sha256_digest

_INJECTION_PATTERNS = (
    re.compile(r"(?i)ignore (?:all |the )?(?:previous|prior|system) instructions"),
    re.compile(r"(?i)(?:system|developer) (?:message|prompt|instructions?)"),
    re.compile(r"(?i)(?:reveal|exfiltrate|print|send).{0,30}(?:secret|token|password|credential)"),
    re.compile(r"(?i)(?:run|execute).{0,20}(?:shell|command|curl|wget)"),
)


class UntrustedContent(DomainModel):
    """Data-only wrapper that cannot carry authority or permission."""

    source: str
    content: str
    content_hash: str
    injection_indicators: tuple[str, ...] = ()
    authoritative: bool = False
    maximum_authority: str = Field(default="data", pattern="^data$")


def classify_untrusted(source: str, content: str) -> UntrustedContent:
    """Label suspicious patterns for evidence/inspection without obeying them."""
    indicators = tuple(
        pattern.pattern for pattern in _INJECTION_PATTERNS if pattern.search(content)
    )
    return UntrustedContent(
        source=source,
        content=content,
        content_hash=sha256_digest(content),
        injection_indicators=indicators,
    )
