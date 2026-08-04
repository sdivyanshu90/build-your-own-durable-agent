"""SSRF-resistant URL validation for optional fetch adapters."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

from durable_agent.domain.errors import SecurityPolicyError


def validate_public_http_url(
    url: str,
    *,
    resolver: Callable[[str, int], Sequence[tuple[Any, ...]]] | None = None,
) -> str:
    """Allow only public HTTP(S) endpoints and reject credential-bearing URLs."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SecurityPolicyError("only absolute HTTP(S) URLs are allowed")
    if parsed.username or parsed.password:
        raise SecurityPolicyError("URL credentials are not allowed")
    if parsed.port is not None and parsed.port not in {80, 443}:
        raise SecurityPolicyError("non-standard network ports are not allowed")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    lookup = resolver or socket.getaddrinfo
    try:
        addresses = lookup(parsed.hostname, port)
    except OSError as exc:
        raise SecurityPolicyError("URL hostname could not be resolved") from exc
    if not addresses:
        raise SecurityPolicyError("URL hostname resolved to no addresses")
    for address in addresses:
        sockaddr = address[4]
        if not isinstance(sockaddr, tuple) or not sockaddr:
            raise SecurityPolicyError("URL resolver returned an invalid address")
        host = str(sockaddr[0])
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            raise SecurityPolicyError(f"URL resolves to a non-public address: {host}")
    return url
