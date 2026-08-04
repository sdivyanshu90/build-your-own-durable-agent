from __future__ import annotations

import httpx
import pytest

from durable_agent.domain.errors import SecurityPolicyError
from durable_agent.providers.fakes import DeterministicClock, DeterministicIdentifiers
from durable_agent.research import HttpDocumentFetcher
from durable_agent.security.network import validate_public_http_url


def resolver_for(ip: str):  # type: ignore[no-untyped-def]
    def resolve(host: str, port: int) -> list[tuple[object, ...]]:
        return [(2, 1, 6, "", (ip, port))]

    return resolve


@pytest.mark.parametrize("ip", ["127.0.0.1", "169.254.169.254", "10.0.0.1", "::1"])
def test_ssrf_private_addresses_are_rejected(ip: str) -> None:
    with pytest.raises(SecurityPolicyError, match="non-public"):
        validate_public_http_url("https://example.test/path", resolver=resolver_for(ip))


def test_public_https_is_accepted() -> None:
    assert (
        validate_public_http_url(
            "https://example.test/path", resolver=resolver_for("93.184.216.34")
        )
        == "https://example.test/path"
    )


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "http://user:password@example.test", "https://example.test:8443"],
)
def test_unsafe_url_forms_are_rejected(url: str) -> None:
    with pytest.raises(SecurityPolicyError):
        validate_public_http_url(url, resolver=resolver_for("93.184.216.34"))


@pytest.mark.asyncio
async def test_http_fetcher_is_disabled_by_default_policy() -> None:
    fetcher = HttpDocumentFetcher(
        enabled=False,
        identifiers=DeterministicIdentifiers(),
        clock=DeterministicClock(),
    )
    with pytest.raises(SecurityPolicyError, match="disabled"):
        await fetcher.fetch("https://example.test/document")


@pytest.mark.asyncio
async def test_http_fetcher_revalidates_redirects_and_bounds_content() -> None:
    def redirect(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/internal"})

    fetcher = HttpDocumentFetcher(
        enabled=True,
        identifiers=DeterministicIdentifiers(),
        clock=DeterministicClock(),
        resolver=lambda host, port: resolver_for(
            "127.0.0.1" if host == "127.0.0.1" else "93.184.216.34"
        )(host, port),
        transport=httpx.MockTransport(redirect),
    )
    with pytest.raises(SecurityPolicyError, match="non-public"):
        await fetcher.fetch("https://example.test/document")

    oversized = HttpDocumentFetcher(
        enabled=True,
        identifiers=DeterministicIdentifiers(),
        clock=DeterministicClock(),
        maximum_bytes=4,
        resolver=resolver_for("93.184.216.34"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"Content-Type": "text/plain"},
                content=b"12345",
                request=request,
            )
        ),
    )
    with pytest.raises(SecurityPolicyError, match="size limit"):
        await oversized.fetch("https://example.test/document")


@pytest.mark.asyncio
async def test_http_fetcher_returns_hash_and_metadata_offline() -> None:
    fetcher = HttpDocumentFetcher(
        enabled=True,
        identifiers=DeterministicIdentifiers(),
        clock=DeterministicClock(),
        resolver=resolver_for("93.184.216.34"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"Content-Type": "text/html; charset=utf-8"},
                content=b"<p>FACT: retries = 3</p>",
                request=request,
            )
        ),
    )
    item = await fetcher.fetch("https://example.test/document")
    assert item.source == "https://example.test/document"
    assert item.content_hash
    assert item.metadata["media_type"] == "text/html"
