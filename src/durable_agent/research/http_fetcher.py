"""Optional bounded HTTP document fetcher with redirect-by-redirect SSRF checks."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urljoin

import httpx

from durable_agent.domain.base import sha256_digest
from durable_agent.domain.errors import (
    ProviderRetryableError,
    SecurityPolicyError,
    ToolExecutionError,
)
from durable_agent.domain.models import RetrievalItem
from durable_agent.domain.protocols import Clock, IdentifierGenerator
from durable_agent.security.network import validate_public_http_url


class HttpDocumentFetcher:
    """Fetch public text documents only when explicit network policy permits it."""

    def __init__(
        self,
        *,
        enabled: bool,
        identifiers: IdentifierGenerator,
        clock: Clock,
        maximum_bytes: int = 2_000_000,
        timeout_seconds: float = 20,
        maximum_redirects: int = 3,
        resolver: Callable[[str, int], Sequence[tuple[Any, ...]]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if maximum_bytes < 1 or timeout_seconds <= 0 or maximum_redirects < 0:
            raise ValueError("fetch limits must be positive")
        self._enabled = enabled
        self._ids = identifiers
        self._clock = clock
        self._maximum_bytes = maximum_bytes
        self._timeout = timeout_seconds
        self._maximum_redirects = maximum_redirects
        self._resolver = resolver
        self._transport = transport

    async def fetch(self, url: str) -> RetrievalItem:
        """Fetch one document after validating every URL and bounding its body."""
        if not self._enabled:
            raise SecurityPolicyError("network access is disabled")
        current = url
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=self._timeout,
            transport=self._transport,
            trust_env=False,
            headers={"User-Agent": "durable-agent/0.1"},
        ) as client:
            for redirect_count in range(self._maximum_redirects + 1):
                validate_public_http_url(current, resolver=self._resolver)
                try:
                    async with client.stream("GET", current) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location or redirect_count >= self._maximum_redirects:
                                raise SecurityPolicyError("HTTP redirect limit exceeded")
                            current = urljoin(current, location)
                            continue
                        if response.status_code == 429 or response.status_code >= 500:
                            raise ProviderRetryableError(
                                f"HTTP source returned {response.status_code}"
                            )
                        if response.status_code >= 400:
                            raise ToolExecutionError(f"HTTP source returned {response.status_code}")
                        media_type = response.headers.get("content-type", "text/plain").split(
                            ";", maxsplit=1
                        )[0]
                        if not self._supported_media_type(media_type):
                            raise SecurityPolicyError(
                                f"unsupported fetched media type: {media_type}"
                            )
                        declared = response.headers.get("content-length")
                        if declared is not None:
                            try:
                                declared_size = int(declared)
                            except ValueError as exc:
                                raise SecurityPolicyError(
                                    "HTTP source returned an invalid content length"
                                ) from exc
                            if declared_size < 0 or declared_size > self._maximum_bytes:
                                raise SecurityPolicyError(
                                    "HTTP document exceeds configured size limit"
                                )
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > self._maximum_bytes:
                                raise SecurityPolicyError(
                                    "HTTP document exceeds configured size limit"
                                )
                        encoding = response.encoding or "utf-8"
                        content = bytes(body).decode(encoding, errors="replace")
                        return RetrievalItem(
                            item_id=self._ids.new("web"),
                            source_type="web",
                            source=current,
                            source_location=current,
                            content=content,
                            content_hash=sha256_digest(bytes(body)),
                            score=1.0,
                            metadata={
                                "media_type": media_type,
                                "retrieved_at": self._clock.now().isoformat(),
                                "status_code": response.status_code,
                            },
                        )
                except httpx.TimeoutException as exc:
                    raise ProviderRetryableError("HTTP source request timed out") from exc
                except httpx.NetworkError as exc:
                    raise ProviderRetryableError("HTTP source network failure") from exc
        raise SecurityPolicyError("HTTP redirect limit exceeded")

    @staticmethod
    def _supported_media_type(media_type: str) -> bool:
        return media_type.startswith("text/") or media_type in {
            "application/json",
            "application/xml",
            "application/xhtml+xml",
        }
