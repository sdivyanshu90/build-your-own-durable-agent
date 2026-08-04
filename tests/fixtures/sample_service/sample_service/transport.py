"""Transport protocol and deterministic fixture implementation."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class TransientTransportError(Exception):
    """A retryable delivery failure."""


class Transport(Protocol):
    def send(self, message: str) -> str: ...


class ScriptedTransport:
    """Return or raise a preconfigured sequence for tests."""

    def __init__(self, outcomes: Sequence[str | Exception]) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    def send(self, message: str) -> str:
        self.calls += 1
        if not self._outcomes:
            raise RuntimeError("no scripted transport outcome")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return f"{outcome}:{message}"
