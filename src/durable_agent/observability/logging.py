"""Structured JSON logging with recursive secret and log-injection redaction."""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping, Sequence
from typing import Any, cast

import structlog

from durable_agent.security.redaction import SecretRedactor


def configure_logging(
    *, level: str = "INFO", json_output: bool = True, secrets: Sequence[str] = ()
) -> None:
    """Configure stdlib and structlog once for service/worker correlation fields."""
    redactor = SecretRedactor(secrets)

    def redact(
        logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        del logger, method_name
        return cast(MutableMapping[str, Any], redactor.redact(event_dict))

    renderer = cast(
        structlog.types.Processor,
        (
            structlog.processors.JSONRenderer(sort_keys=True)
            if json_output
            else structlog.dev.ConsoleRenderer()
        ),
    )
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact,
        renderer,
    ]
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=getattr(logging, level))
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "durable_agent"):  # type: ignore[no-untyped-def]
    """Return a structured bound logger."""
    return structlog.get_logger(name)
