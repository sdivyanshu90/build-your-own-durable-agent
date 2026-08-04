"""Vendor-neutral OpenTelemetry span hook."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace


@contextmanager
def span(
    name: str, attributes: Mapping[str, str | int | float | bool] | None = None
) -> Iterator[Any]:
    """Create a no-op or configured OpenTelemetry span without provider coupling."""
    tracer = trace.get_tracer("durable_agent")
    with tracer.start_as_current_span(name, attributes=dict(attributes or {})) as current:
        yield current
