"""Explicit tool registry with duplicate protection."""

from __future__ import annotations

from collections.abc import Iterable

from durable_agent.domain.errors import NotFoundError
from durable_agent.domain.protocols import Tool


class ToolRegistry:
    """Resolve tools only by their declared stable names."""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        name = tool.definition.name
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise NotFoundError(f"unknown tool: {name}") from exc

    def definitions(self):  # type: ignore[no-untyped-def]
        return tuple(self._tools[name].definition for name in sorted(self._tools))
